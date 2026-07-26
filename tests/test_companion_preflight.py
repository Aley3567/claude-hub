from __future__ import annotations

import ast
import builtins
import dataclasses
import hashlib
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.approval import ApprovalRegistry  # noqa: E402
from claude_hub import companion_preflight  # noqa: E402
from claude_hub.ccswitch import CCSwitchProviderStore  # noqa: E402
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    STANDALONE_STORE_ID,
    build_change_plan,
)
from claude_hub.companion_preflight import (  # noqa: E402
    CC_SWITCH_PROCESS_NAMES,
    CCSwitchProcessState,
    CompanionPreflight,
    CompanionPreflightError,
    CompanionPreflightStatus,
    PGREP_EXECUTABLE,
    SystemCCSwitchProcessDetector,
)
from claude_hub.domain import (  # noqa: E402
    ProviderInspection,
    ProviderRef,
    RuntimeMode,
    StoreCapability,
)
from claude_hub.tui import request_tui_approval  # noqa: E402


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def write_schema(path: pathlib.Path, *, user_version: int = 16) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                app_type TEXT NOT NULL,
                sort_index INTEGER NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE proxy_config (
                app_type TEXT PRIMARY KEY,
                live_takeover_active INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE proxy_live_backup (
                app_type TEXT PRIMARY KEY,
                original_config TEXT NOT NULL,
                backed_up_at TEXT
            );
            """
        )
        connection.execute(f"PRAGMA user_version={user_version:d}")
        connection.commit()
    finally:
        connection.close()


class FixedProcessDetector:
    def __init__(self, *states: object) -> None:
        self.states = list(states)
        self.call_count = 0

    def detect(self) -> object:
        self.call_count += 1
        if not self.states:
            raise AssertionError("unexpected process probe")
        value = self.states.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ScriptedRunner:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), dict(kwargs)))
        if not self.results:
            raise AssertionError("unexpected process command")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ForbiddenStore:
    def detect(self):
        raise AssertionError("Store access is forbidden")

    def list(self):
        raise AssertionError("Store access is forbidden")

    def inspect(self, _reference):
        raise AssertionError("Store access is forbidden")


class ForbiddenProcessDetector:
    def detect(self):
        raise AssertionError("process access is forbidden")


class ReadyStore:
    def __init__(self, fingerprint: str = "a1" * 32) -> None:
        self.fingerprint = fingerprint

    def detect(self):
        return StoreCapability.COMPATIBLE

    def list(self):
        return ()

    def inspect(self, reference):
        return ProviderInspection(
            reference=reference,
            fingerprint=self.fingerprint,
            schema_capability=StoreCapability.COMPATIBLE,
        )


class InspectionStore:
    def __init__(self, inspection: object) -> None:
        self.inspection = inspection

    def detect(self):
        return StoreCapability.COMPATIBLE

    def list(self):
        return ()

    def inspect(self, _reference):
        return self.inspection


def plan_fixture(
    *,
    model_new: str = "model-new",
    fingerprint: str = "a1" * 32,
):
    return build_change_plan(
        mode=RuntimeMode.COMPANION,
        target=ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id="provider-public-id",
        ),
        store_fingerprint=fingerprint,
        changes={"models.default": ("model-old", model_new)},
    )


def approve(registry: ApprovalRegistry, plan: object):
    handle = request_tui_approval(
        plan,  # type: ignore[arg-type]
        registry,
        show_preview=lambda _preview: None,
        confirm=lambda: True,
    )
    if handle is None:
        raise AssertionError("fixture approval was not issued")
    return handle


class CompanionPreflightSuccessTests(unittest.TestCase):
    def test_supported_stopped_target_is_ready_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        (
                            '{"env":{"ANTHROPIC_MODEL":"model-old",'
                            '"ANTHROPIC_BASE_URL":'
                            '"https://private-result.invalid/v1",'
                            '"ANTHROPIC_AUTH_TOKEN":'
                            '"sk-live-fixture-preflight-result-canary-123456"}}'
                        ),
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                connection.commit()
            finally:
                connection.close()

            store = CCSwitchProviderStore(database)
            reference = ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
            )
            fingerprint = store.inspect(reference).fingerprint
            if fingerprint is None:
                raise AssertionError("fixture fingerprint is missing")
            plan = build_change_plan(
                mode=RuntimeMode.COMPANION,
                target=reference,
                store_fingerprint=fingerprint,
                changes={"models.default": ("model-old", "model-new")},
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            detector = FixedProcessDetector(
                CCSwitchProcessState.STOPPED
            )
            before = {
                path.name: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file()
            }

            result = CompanionPreflight(
                store=store,
                process_detector=detector,
            ).check(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

            after = {
                path.name: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file()
            }

        self.assertIs(result.status, CompanionPreflightStatus.READY)
        self.assertTrue(result.allowed)
        self.assertIs(result.process_state, CCSwitchProcessState.STOPPED)
        self.assertEqual(
            result.to_public_dict(),
            {
                "status": "ready",
                "processState": "stopped",
                "schemaCapability": "compatible",
                "guidance": (
                    "Proceed immediately with the approved Companion write."
                ),
            },
        )
        self.assertEqual(detector.call_count, 1)
        self.assertEqual(registry.active_count, 0)
        self.assertEqual(after, before)
        for surface in (repr(result), repr(result.to_public_dict())):
            for canary in (
                str(database),
                "Private provider label",
                "https://private-result.invalid/v1",
                "sk-live-fixture-preflight-result-canary-123456",
            ):
                self.assertNotIn(canary, surface)

    def test_orchestrator_has_no_home_network_write_or_kill_access(
        self,
    ) -> None:
        plan = plan_fixture()
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        preflight = CompanionPreflight(
            store=ReadyStore(),
            process_detector=FixedProcessDetector(
                CCSwitchProcessState.STOPPED
            ),
        )

        forbidden = AssertionError("forbidden side effect")
        with (
            mock.patch.object(pathlib.Path, "home", side_effect=forbidden),
            mock.patch.object(
                pathlib.Path,
                "write_text",
                side_effect=forbidden,
            ),
            mock.patch.object(
                pathlib.Path,
                "write_bytes",
                side_effect=forbidden,
            ),
            mock.patch.object(
                pathlib.Path,
                "touch",
                side_effect=forbidden,
            ),
            mock.patch.object(
                pathlib.Path,
                "mkdir",
                side_effect=forbidden,
            ),
            mock.patch.object(builtins, "open", side_effect=forbidden),
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=forbidden,
            ),
            mock.patch.object(os, "kill", side_effect=forbidden),
            mock.patch.object(
                subprocess,
                "Popen",
                side_effect=forbidden,
            ),
        ):
            result = preflight.check(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertTrue(result.allowed)
        self.assertEqual(registry.active_count, 0)

    def test_module_has_no_network_write_wait_or_kill_primitive(self) -> None:
        source = pathlib.Path(companion_preflight.__file__).read_text(
            encoding="utf-8"
        )
        parsed = ast.parse(source)
        imported_roots: set[str] = set()
        attributes: set[str] = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Attribute):
                attributes.add(node.attr)

        self.assertTrue(
            {
                "socket",
                "urllib",
                "http",
                "requests",
                "tempfile",
                "shutil",
            }.isdisjoint(imported_roots)
        )
        self.assertTrue(
            {
                "kill",
                "terminate",
                "wait",
                "write",
                "write_text",
                "write_bytes",
                "unlink",
                "replace",
                "rename",
            }.isdisjoint(attributes)
        )


class CompanionPreflightApprovalTests(unittest.TestCase):
    def test_missing_expired_and_wrong_plan_approval_fail_before_io(
        self,
    ) -> None:
        preflight = CompanionPreflight(
            store=ForbiddenStore(),
            process_detector=ForbiddenProcessDetector(),
        )
        plan = plan_fixture()

        missing_registry = ApprovalRegistry(clock=lambda: NOW)
        with self.assertRaises(CompanionPreflightError) as missing:
            preflight.check(
                plan=plan,
                approval_registry=missing_registry,
                approval_handle=None,  # type: ignore[arg-type]
            )

        clock = MutableClock()
        expired_registry = ApprovalRegistry(clock=clock)
        expired_handle = approve(expired_registry, plan)
        clock.value = NOW + timedelta(minutes=15)
        with self.assertRaises(CompanionPreflightError) as expired:
            preflight.check(
                plan=plan,
                approval_registry=expired_registry,
                approval_handle=expired_handle,
            )

        approved_plan = plan_fixture()
        changed_plan = plan_fixture(model_new="model-other")
        mismatch_registry = ApprovalRegistry(clock=lambda: NOW)
        mismatch_handle = approve(mismatch_registry, approved_plan)
        with self.assertRaises(CompanionPreflightError) as mismatch:
            preflight.check(
                plan=changed_plan,
                approval_registry=mismatch_registry,
                approval_handle=mismatch_handle,
            )
        with self.assertRaises(CompanionPreflightError):
            preflight.check(
                plan=approved_plan,
                approval_registry=mismatch_registry,
                approval_handle=mismatch_handle,
            )

        for caught in (missing, expired, mismatch):
            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )
            self.assertEqual(
                str(caught.exception),
                "Companion preflight requires a current approval",
            )
            self.assertIn("approve", caught.exception.guidance.casefold())
        self.assertEqual(expired_registry.active_count, 0)
        self.assertEqual(mismatch_registry.active_count, 0)

    def test_approved_non_companion_plan_is_consumed_then_rejected(self) -> None:
        plan = build_change_plan(
            mode=RuntimeMode.STANDALONE,
            target=ProviderRef(
                store=STANDALONE_STORE_ID,
                provider_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            ),
            store_fingerprint="a1" * 32,
            changes={"models.default": ("model-old", "model-new")},
        )
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        preflight = CompanionPreflight(
            store=ForbiddenStore(),
            process_detector=ForbiddenProcessDetector(),
        )

        with self.assertRaises(CompanionPreflightError) as caught:
            preflight.check(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionPreflightStatus.PLAN_INVALID,
        )
        self.assertEqual(registry.active_count, 0)
        self.assertIn("companion", caught.exception.guidance.casefold())

    def test_lookalike_registry_cannot_bypass_real_approval(self) -> None:
        class LookalikeRegistry:
            def consume(self, _handle, _plan):
                return object()

        with self.assertRaises(CompanionPreflightError) as caught:
            CompanionPreflight(
                store=ForbiddenStore(),
                process_detector=ForbiddenProcessDetector(),
            ).check(
                plan=plan_fixture(),
                approval_registry=LookalikeRegistry(),  # type: ignore[arg-type]
                approval_handle=object(),  # type: ignore[arg-type]
            )

        self.assertIs(
            caught.exception.status,
            CompanionPreflightStatus.APPROVAL_REQUIRED,
        )

    def test_foreign_registry_rejects_handle_before_any_io(self) -> None:
        plan = plan_fixture()
        issuing_registry = ApprovalRegistry(clock=lambda: NOW)
        foreign_registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(issuing_registry, plan)

        with self.assertRaises(CompanionPreflightError) as caught:
            CompanionPreflight(
                store=ForbiddenStore(),
                process_detector=ForbiddenProcessDetector(),
            ).check(
                plan=plan,
                approval_registry=foreign_registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionPreflightStatus.APPROVAL_REQUIRED,
        )
        self.assertEqual(foreign_registry.active_count, 0)
        self.assertEqual(issuing_registry.active_count, 1)
        issuing_registry.consume(handle, plan)
        self.assertEqual(issuing_registry.active_count, 0)


class CompanionPreflightProcessTests(unittest.TestCase):
    def test_running_requires_exit_and_every_retry_requires_new_approval(
        self,
    ) -> None:
        plan = plan_fixture()
        registry = ApprovalRegistry(clock=lambda: NOW)
        detector = FixedProcessDetector(
            CCSwitchProcessState.RUNNING,
            CCSwitchProcessState.RUNNING,
            CCSwitchProcessState.STOPPED,
        )
        preflight = CompanionPreflight(
            store=ReadyStore(),
            process_detector=detector,
        )

        first_handle = approve(registry, plan)
        with self.assertRaises(CompanionPreflightError) as first:
            preflight.check(
                plan=plan,
                approval_registry=registry,
                approval_handle=first_handle,
            )
        self.assertIs(
            first.exception.status,
            CompanionPreflightStatus.PROCESS_RUNNING,
        )
        self.assertIs(
            first.exception.process_state,
            CCSwitchProcessState.RUNNING,
        )
        self.assertIn(
            "exit cc switch",
            first.exception.guidance.casefold(),
        )

        with self.assertRaises(CompanionPreflightError) as replay:
            preflight.retry_after_exit(
                plan=plan,
                approval_registry=registry,
                approval_handle=first_handle,
            )
        self.assertIs(
            replay.exception.status,
            CompanionPreflightStatus.APPROVAL_REQUIRED,
        )
        self.assertEqual(detector.call_count, 1)

        still_running_handle = approve(registry, plan)
        with self.assertRaises(CompanionPreflightError) as still_running:
            preflight.retry_after_exit(
                plan=plan,
                approval_registry=registry,
                approval_handle=still_running_handle,
            )
        self.assertIs(
            still_running.exception.status,
            CompanionPreflightStatus.PROCESS_RUNNING,
        )

        stopped_handle = approve(registry, plan)
        result = preflight.retry_after_exit(
            plan=plan,
            approval_registry=registry,
            approval_handle=stopped_handle,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(detector.call_count, 3)
        self.assertEqual(registry.active_count, 0)

    def test_unknown_exception_and_invalid_detector_output_fail_closed(
        self,
    ) -> None:
        canary = "private-process-error-canary"
        plan = plan_fixture()
        for detector_value in (
            CCSwitchProcessState.UNKNOWN,
            RuntimeError(canary),
            "stopped",
        ):
            with self.subTest(detector_value=type(detector_value).__name__):
                registry = ApprovalRegistry(clock=lambda: NOW)
                handle = approve(registry, plan)
                preflight = CompanionPreflight(
                    store=ForbiddenStore(),
                    process_detector=FixedProcessDetector(detector_value),
                )

                with self.assertRaises(CompanionPreflightError) as caught:
                    preflight.check(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                self.assertIs(
                    caught.exception.status,
                    CompanionPreflightStatus.PROCESS_UNKNOWN,
                )
                self.assertIs(
                    caught.exception.process_state,
                    CCSwitchProcessState.UNKNOWN,
                )
                self.assertIn("verify", caught.exception.guidance.casefold())
                self.assertNotIn(canary, str(caught.exception))
                self.assertNotIn(canary, repr(caught.exception))
                self.assertEqual(registry.active_count, 0)


class SystemProcessDetectorTests(unittest.TestCase):
    def test_direct_fixed_name_probes_report_running_or_stopped(self) -> None:
        stopped_runner = ScriptedRunner(
            *(
                subprocess.CompletedProcess((), 1, "", "")
                for _name in CC_SWITCH_PROCESS_NAMES
            )
        )
        stopped = SystemCCSwitchProcessDetector(
            platform="posix",
            runner=stopped_runner,
        ).detect()

        self.assertIs(stopped, CCSwitchProcessState.STOPPED)
        self.assertEqual(
            tuple(call[0] for call in stopped_runner.calls),
            tuple(
                (PGREP_EXECUTABLE, "-x", name)
                for name in CC_SWITCH_PROCESS_NAMES
            ),
        )
        for command, kwargs in stopped_runner.calls:
            self.assertNotIn("-f", command)
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])

        running_runner = ScriptedRunner(
            subprocess.CompletedProcess((), 0, "123\n", "")
        )
        running = SystemCCSwitchProcessDetector(
            platform="posix",
            runner=running_runner,
        ).detect()
        self.assertIs(running, CCSwitchProcessState.RUNNING)
        self.assertEqual(len(running_runner.calls), 1)

    def test_probe_errors_and_unexpected_output_are_unknown(self) -> None:
        canary = "private-process-runner-canary"
        cases = (
            ScriptedRunner(RuntimeError(canary)),
            ScriptedRunner(
                subprocess.CompletedProcess((), 0, "private argv\n", "")
            ),
            ScriptedRunner(
                subprocess.CompletedProcess((), 2, "", canary)
            ),
        )
        for runner in cases:
            with self.subTest(result_type=type(runner.results[0]).__name__):
                detector = SystemCCSwitchProcessDetector(
                    platform="posix",
                    runner=runner,
                )
                self.assertIs(
                    detector.detect(),
                    CCSwitchProcessState.UNKNOWN,
                )
                self.assertNotIn(canary, repr(detector))

        unsupported = SystemCCSwitchProcessDetector(
            platform="unsupported",
            runner=ScriptedRunner(
                AssertionError("runner must not be called")
            ),
        )
        self.assertIs(
            unsupported.detect(),
            CCSwitchProcessState.UNKNOWN,
        )

    def test_windows_probe_reads_executable_names_not_argv(self) -> None:
        stopped = SystemCCSwitchProcessDetector(
            platform="nt",
            runner=ScriptedRunner(
                AssertionError("subprocess runner must not be called")
            ),
            windows_probe=lambda: ("python.exe", "explorer.exe"),
        )
        running = SystemCCSwitchProcessDetector(
            platform="nt",
            runner=ScriptedRunner(
                AssertionError("subprocess runner must not be called")
            ),
            windows_probe=lambda: ("CC-SWITCH.EXE",),
        )
        uncertain = SystemCCSwitchProcessDetector(
            platform="nt",
            runner=ScriptedRunner(
                AssertionError("subprocess runner must not be called")
            ),
            windows_probe=lambda: (_ for _ in ()).throw(
                RuntimeError("private windows error canary")
            ),
        )

        self.assertIs(stopped.detect(), CCSwitchProcessState.STOPPED)
        self.assertIs(running.detect(), CCSwitchProcessState.RUNNING)
        self.assertIs(uncertain.detect(), CCSwitchProcessState.UNKNOWN)


class CompanionPreflightStoreTests(unittest.TestCase):
    def test_hot_rollback_journal_is_store_unavailable_without_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            write_schema(database)
            raw_settings = (
                '{"env":{"ANTHROPIC_MODEL":"hot-journal-model"}}'
            )
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "PRAGMA journal_mode=DELETE"
                    ).fetchone(),
                    ("delete",),
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        raw_settings,
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                connection.execute(
                    "CREATE TABLE crash_pages ("
                    "id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO crash_pages(value) VALUES (?)",
                    (("a" * 3500,) for _index in range(600)),
                )
                connection.commit()
            finally:
                connection.close()

            crash_script = r"""
import os
import sqlite3
import sys

database = os.fsdecode(sys.stdin.buffer.read())
connection = sqlite3.connect(database)
connection.execute("PRAGMA journal_mode=DELETE")
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA cache_size=5")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "UPDATE crash_pages SET value=?",
    ("b" * 3500,),
)
os._exit(0)
"""
            subprocess.run(
                (sys.executable, "-I", "-c", crash_script),
                input=os.fsencode(database),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=10,
            )
            journal = database.with_name(database.name + "-journal")
            self.assertTrue(journal.is_file())
            self.assertGreater(journal.stat().st_size, 512)
            self.assertEqual(
                journal.read_bytes()[:8],
                bytes.fromhex("d9d505f920a163d7"),
            )

            def source_state() -> tuple[
                int,
                dict[str, tuple[bytes, int]],
            ]:
                return (
                    directory.stat().st_mtime_ns,
                    {
                        path.name: (
                            path.read_bytes(),
                            path.stat().st_mtime_ns,
                        )
                        for path in sorted(directory.iterdir())
                    },
                )

            before = source_state()
            plan = plan_fixture(
                fingerprint=hashlib.sha256(
                    raw_settings.encode("utf-8")
                ).hexdigest()
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)

            with self.assertRaises(CompanionPreflightError) as caught:
                CompanionPreflight(
                    store=CCSwitchProviderStore(database),
                    process_detector=FixedProcessDetector(
                        CCSwitchProcessState.STOPPED
                    ),
                ).check(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            )
            self.assertEqual(registry.active_count, 0)
            self.assertEqual(source_state(), before)

    def test_active_wal_writer_is_store_unavailable_without_source_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            write_schema(database)
            raw_settings = (
                '{"env":{"ANTHROPIC_MODEL":"wal-writer-model"}}'
            )
            writer = sqlite3.connect(database)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        raw_settings,
                        "claude",
                        1,
                        0,
                    ),
                )
                writer.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                writer.commit()
                wal = database.with_name(database.name + "-wal")
                shm = database.with_name(database.name + "-shm")
                self.assertTrue(wal.is_file())
                self.assertTrue(shm.is_file())

                writer.execute("BEGIN IMMEDIATE")

                def source_state() -> object:
                    script = r"""
import hashlib
import json
import os
import sys

directory = os.fsdecode(sys.stdin.buffer.read())
files = {}
with os.scandir(directory) as entries:
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.is_file(follow_symlinks=False):
            continue
        digest = hashlib.sha256()
        with open(entry.path, "rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        metadata = entry.stat(follow_symlinks=False)
        files[entry.name] = [
            digest.hexdigest(),
            metadata.st_size,
            metadata.st_mtime_ns,
        ]
result = [os.stat(directory).st_mtime_ns, files]
sys.stdout.write(json.dumps(result, sort_keys=True))
"""
                    completed = subprocess.run(
                        (sys.executable, "-I", "-c", script),
                        input=os.fsencode(directory),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=True,
                        timeout=5,
                    )
                    return json.loads(completed.stdout)

                before = source_state()
                plan = plan_fixture(
                    fingerprint=hashlib.sha256(
                        raw_settings.encode("utf-8")
                    ).hexdigest()
                )
                registry = ApprovalRegistry(clock=lambda: NOW)
                handle = approve(registry, plan)

                with self.assertRaises(CompanionPreflightError) as caught:
                    CompanionPreflight(
                        store=CCSwitchProviderStore(database),
                        process_detector=FixedProcessDetector(
                            CCSwitchProcessState.STOPPED
                        ),
                    ).check(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                self.assertIs(
                    caught.exception.status,
                    CompanionPreflightStatus.STORE_UNAVAILABLE,
                )
                self.assertEqual(source_state(), before)
                self.assertEqual(registry.active_count, 0)
            finally:
                writer.rollback()
                writer.close()

    def test_wal_without_shm_is_read_from_private_snapshot_without_source_writes(
        self,
    ) -> None:
        for version, expected_capability in (
            (13, StoreCapability.READ_ONLY),
            (16, StoreCapability.COMPATIBLE),
        ):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = pathlib.Path(raw_directory)
                database = directory / "fixture.db"
                writer: sqlite3.Connection | None = sqlite3.connect(database)
                try:
                    writer.execute("PRAGMA journal_mode=WAL")
                    writer.execute("PRAGMA wal_autocheckpoint=0")
                    writer.executescript(
                        """
                        CREATE TABLE providers (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            settings_config TEXT NOT NULL,
                            app_type TEXT NOT NULL,
                            sort_index INTEGER NOT NULL,
                            is_current INTEGER NOT NULL DEFAULT 0
                        );
                        CREATE TABLE proxy_config (
                            app_type TEXT PRIMARY KEY,
                            live_takeover_active INTEGER NOT NULL DEFAULT 0
                        );
                        CREATE TABLE proxy_live_backup (
                            app_type TEXT PRIMARY KEY,
                            original_config TEXT NOT NULL,
                            backed_up_at TEXT
                        );
                        """
                    )
                    writer.execute(f"PRAGMA user_version={version:d}")
                    writer.commit()
                    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    main_before_wal_commit = database.read_bytes()
                    writer.execute(
                        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "provider-public-id",
                            "Private provider label",
                            '{"env":{"ANTHROPIC_MODEL":"wal-visible-model"}}',
                            "claude",
                            1,
                            0,
                        ),
                    )
                    writer.execute(
                        "INSERT INTO proxy_config VALUES (?, ?)",
                        ("claude", 0),
                    )
                    writer.commit()
                    wal = database.with_name(database.name + "-wal")
                    shm = database.with_name(database.name + "-shm")
                    self.assertTrue(wal.is_file())
                    wal_commit = wal.read_bytes()
                    writer.close()
                    writer = None
                    # Recreate the valid crash-state pair: the pre-commit main
                    # database plus the committed WAL frames, with no SHM.
                    database.write_bytes(main_before_wal_commit)
                    wal.write_bytes(wal_commit)
                    shm.unlink(missing_ok=True)
                    self.assertFalse(shm.exists())
                    fixed_directory_mtime = 1_700_000_000_000_000_000
                    os.utime(
                        directory,
                        ns=(
                            fixed_directory_mtime,
                            fixed_directory_mtime,
                        ),
                    )

                    def source_state() -> tuple[
                        int,
                        dict[str, tuple[bytes, int]],
                    ]:
                        return (
                            directory.stat().st_mtime_ns,
                            {
                                path.name: (
                                    path.read_bytes(),
                                    path.stat().st_mtime_ns,
                                )
                                for path in sorted(directory.iterdir())
                                if path.is_file()
                            },
                        )

                    before = source_state()
                    store = CCSwitchProviderStore(database)
                    self.assertIs(store.detect(), expected_capability)
                    reference = ProviderRef(
                        store=COMPANION_STORE_ID,
                        provider_id="provider-public-id",
                    )
                    inspection = store.inspect(reference)
                    self.assertEqual(
                        inspection.models.default,
                        "wal-visible-model",
                    )
                    self.assertIs(
                        inspection.schema_capability,
                        expected_capability,
                    )
                    if inspection.fingerprint is None:
                        raise AssertionError(
                            "fixture fingerprint is missing"
                        )
                    plan = plan_fixture(
                        fingerprint=inspection.fingerprint
                    )
                    registry = ApprovalRegistry(clock=lambda: NOW)
                    handle = approve(registry, plan)
                    preflight = CompanionPreflight(
                        store=store,
                        process_detector=FixedProcessDetector(
                            CCSwitchProcessState.STOPPED
                        ),
                    )
                    if version == 13:
                        with self.assertRaises(
                            CompanionPreflightError
                        ) as caught:
                            preflight.check(
                                plan=plan,
                                approval_registry=registry,
                                approval_handle=handle,
                            )
                        self.assertIs(
                            caught.exception.status,
                            CompanionPreflightStatus.SCHEMA_UNSUPPORTED,
                        )
                    else:
                        self.assertTrue(
                            preflight.check(
                                plan=plan,
                                approval_registry=registry,
                                approval_handle=handle,
                            ).allowed
                        )
                    self.assertEqual(source_state(), before)
                    self.assertFalse(shm.exists())
                finally:
                    if writer is not None:
                        writer.close()

    def test_unknown_and_read_only_schema_fail_closed_without_writing(
        self,
    ) -> None:
        for version, expected_capability in (
            (17, StoreCapability.INCOMPATIBLE),
            (13, StoreCapability.READ_ONLY),
        ):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                database = pathlib.Path(raw_directory) / "fixture.db"
                write_schema(database, user_version=version)
                before = database.read_bytes()
                plan = plan_fixture()
                registry = ApprovalRegistry(clock=lambda: NOW)
                handle = approve(registry, plan)

                with self.assertRaises(CompanionPreflightError) as caught:
                    CompanionPreflight(
                        store=CCSwitchProviderStore(database),
                        process_detector=FixedProcessDetector(
                            CCSwitchProcessState.STOPPED
                        ),
                    ).check(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                self.assertIs(
                    caught.exception.status,
                    CompanionPreflightStatus.SCHEMA_UNSUPPORTED,
                )
                self.assertIs(
                    caught.exception.schema_capability,
                    expected_capability,
                )
                self.assertIn(
                    "supported",
                    caught.exception.guidance.casefold(),
                )
                self.assertEqual(database.read_bytes(), before)
                self.assertEqual(registry.active_count, 0)

    def test_missing_target_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                connection.commit()
            finally:
                connection.close()
            before = database.read_bytes()
            plan = plan_fixture()
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)

            with self.assertRaises(CompanionPreflightError) as caught:
                CompanionPreflight(
                    store=CCSwitchProviderStore(database),
                    process_detector=FixedProcessDetector(
                        CCSwitchProcessState.STOPPED
                    ),
                ).check(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.TARGET_NOT_FOUND,
            )
            self.assertIn("refresh", caught.exception.guidance.casefold())
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(registry.active_count, 0)

    def test_active_proxy_takeover_is_blocked_with_close_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        '{"env":{"ANTHROPIC_MODEL":"model-old"}}',
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 1),
                )
                connection.commit()
            finally:
                connection.close()
            store = CCSwitchProviderStore(database)
            reference = ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
            )
            fingerprint = store.inspect(reference).fingerprint
            if fingerprint is None:
                raise AssertionError("fixture fingerprint is missing")
            plan = plan_fixture(fingerprint=fingerprint)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database.read_bytes()

            with self.assertRaises(CompanionPreflightError) as caught:
                CompanionPreflight(
                    store=store,
                    process_detector=FixedProcessDetector(
                        CCSwitchProcessState.STOPPED
                    ),
                ).check(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.PROXY_TAKEOVER_ACTIVE,
            )
            self.assertIn(
                "turn off proxy takeover",
                caught.exception.guidance.casefold(),
            )
            self.assertIn(
                "exit cc switch",
                caught.exception.guidance.casefold(),
            )
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(registry.active_count, 0)

    def test_stale_plan_fingerprint_requires_a_new_plan(self) -> None:
        plan = plan_fixture(fingerprint="a1" * 32)
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)

        with self.assertRaises(CompanionPreflightError) as caught:
            CompanionPreflight(
                store=ReadyStore(fingerprint="b2" * 32),
                process_detector=FixedProcessDetector(
                    CCSwitchProcessState.STOPPED
                ),
            ).check(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionPreflightStatus.PLAN_STALE,
        )
        self.assertIn("new plan", caught.exception.guidance.casefold())
        self.assertEqual(registry.active_count, 0)

    def test_locked_database_is_unavailable_and_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        '{"env":{"ANTHROPIC_MODEL":"model-old"}}',
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                connection.commit()
            finally:
                connection.close()
            store = CCSwitchProviderStore(database)
            reference = ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
            )
            fingerprint = store.inspect(reference).fingerprint
            if fingerprint is None:
                raise AssertionError("fixture fingerprint is missing")
            plan = plan_fixture(fingerprint=fingerprint)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database.read_bytes()
            locker = sqlite3.connect(database, timeout=0)
            try:
                locker.execute("BEGIN EXCLUSIVE")

                with self.assertRaises(CompanionPreflightError) as caught:
                    CompanionPreflight(
                        store=store,
                        process_detector=FixedProcessDetector(
                            CCSwitchProcessState.STOPPED
                        ),
                    ).check(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )
            finally:
                locker.rollback()
                locker.close()

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            )
            self.assertIn("retry", caught.exception.guidance.casefold())
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(registry.active_count, 0)

    def test_missing_proxy_state_is_not_treated_as_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            raw_settings = '{"env":{"ANTHROPIC_MODEL":"model-old"}}'
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "provider-public-id",
                        "Private provider label",
                        raw_settings,
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            before = database.read_bytes()
            # The exact value is irrelevant: inspect must reject the unknown
            # proxy state before preflight can compare the plan fingerprint.
            plan = plan_fixture()
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)

            with self.assertRaises(CompanionPreflightError) as caught:
                CompanionPreflight(
                    store=CCSwitchProviderStore(database),
                    process_detector=FixedProcessDetector(
                        CCSwitchProcessState.STOPPED
                    ),
                ).check(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            )
            self.assertEqual(database.read_bytes(), before)

    def test_store_exception_and_public_objects_never_echo_canaries(
        self,
    ) -> None:
        canaries = (
            "/private/home/customer/.cc-switch/cc-switch.db",
            "Private customer provider label",
            "https://private-preflight.invalid/v1",
            "sk-live-preflight-fixture-123456789",
            "raw sqlite failure canary",
        )

        class ExplodingStore:
            def detect(self):
                raise RuntimeError(" ".join(canaries))

            def list(self):
                return ()

            def inspect(self, _reference):
                raise AssertionError("inspect must not run")

        plan = plan_fixture()
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        with self.assertRaises(CompanionPreflightError) as caught:
            CompanionPreflight(
                store=ExplodingStore(),
                process_detector=FixedProcessDetector(
                    CCSwitchProcessState.STOPPED
                ),
            ).check(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        surfaces = (
            str(caught.exception),
            repr(caught.exception),
            caught.exception.guidance,
            repr(caught.exception.to_public_dict()),
        )
        for surface in surfaces:
            for canary in canaries:
                self.assertNotIn(canary, surface)
        self.assertEqual(
            tuple(
                field.name
                for field in dataclasses.fields(caught.exception)
            ),
            ("status", "process_state", "schema_capability"),
        )

    def test_invalid_or_downgraded_inspection_fails_closed(self) -> None:
        plan = plan_fixture()
        target = ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id="provider-public-id",
        )
        cases = (
            (object(), CompanionPreflightStatus.STORE_UNAVAILABLE),
            (
                ProviderInspection(
                    reference=target,
                    fingerprint="a1" * 32,
                    schema_capability=StoreCapability.READ_ONLY,
                ),
                CompanionPreflightStatus.SCHEMA_UNSUPPORTED,
            ),
            (
                ProviderInspection(
                    reference=ProviderRef(
                        store=COMPANION_STORE_ID,
                        provider_id="another-public-id",
                    ),
                    fingerprint="a1" * 32,
                    schema_capability=StoreCapability.COMPATIBLE,
                ),
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            ),
            (
                ProviderInspection(
                    reference=target,
                    fingerprint=None,
                    schema_capability=StoreCapability.COMPATIBLE,
                ),
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            ),
        )
        for inspection, expected in cases:
            with self.subTest(expected=expected.value):
                registry = ApprovalRegistry(clock=lambda: NOW)
                handle = approve(registry, plan)
                with self.assertRaises(CompanionPreflightError) as caught:
                    CompanionPreflight(
                        store=InspectionStore(inspection),
                        process_detector=FixedProcessDetector(
                            CCSwitchProcessState.STOPPED
                        ),
                    ).check(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )
                self.assertIs(caught.exception.status, expected)
                self.assertEqual(registry.active_count, 0)


if __name__ == "__main__":
    unittest.main()
