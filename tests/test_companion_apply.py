from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub import companion_apply  # noqa: E402
from claude_hub.approval import ApprovalRegistry  # noqa: E402
from claude_hub.ccswitch import CCSwitchProviderStore  # noqa: E402
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    build_change_plan,
)
from claude_hub.companion_apply import (  # noqa: E402
    BACKUP_PAGES_PER_STEP,
    CompanionApplyError,
    CompanionApplyService,
    CompanionApplyStatus,
)
from claude_hub.companion_preflight import (  # noqa: E402
    CCSwitchProcessState,
    CompanionPreflightError,
    CompanionPreflightStatus,
)
from claude_hub.domain import ProviderRef, RuntimeMode  # noqa: E402
from claude_hub.tui import request_tui_approval  # noqa: E402


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
PROVIDER_ID = "provider-public-id"
OTHER_PROVIDER_ID = "other-provider-public-id"
PRIVATE_URL = "https://private-apply-result.invalid/v1"
PRIVATE_KEY = "sk-live-fixture-apply-result-canary-123456"


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def settings_document(
    *,
    default: object = "model-old",
    fast: object = "fast-old",
    use_fast_alias: bool = False,
    extra_env: dict[str, object] | None = None,
) -> dict[str, object]:
    env: dict[str, object] = {
        "ANTHROPIC_BASE_URL": PRIVATE_URL,
        "ANTHROPIC_AUTH_TOKEN": PRIVATE_KEY,
        "UNRECOGNIZED_NESTED": {"preserve": [1, True, None]},
    }
    if default is not _ABSENT:
        env["ANTHROPIC_MODEL"] = default
    if fast is not _ABSENT:
        env[
            (
                "ANTHROPIC_SMALL_FAST_MODEL"
                if use_fast_alias
                else "ANTHROPIC_DEFAULT_HAIKU_MODEL"
            )
        ] = fast
    if extra_env:
        env.update(extra_env)
    return {
        "env": env,
        "unknownTopLevel": {
            "nested": ["keep", {"all": "metadata"}],
        },
    }


_ABSENT = object()


def write_schema(
    path: pathlib.Path,
    *,
    user_version: int = 16,
    journal_mode: str = "DELETE",
    target_document: dict[str, object] | None = None,
) -> sqlite3.Connection | None:
    connection = sqlite3.connect(path)
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
    selected_mode = connection.execute(
        f"PRAGMA journal_mode={journal_mode}"
    ).fetchone()
    if selected_mode != (journal_mode.lower(),):
        connection.close()
        raise AssertionError("fixture journal mode was not selected")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    target = canonical_json(
        settings_document()
        if target_document is None
        else target_document
    )
    other = canonical_json(
        {
            "env": {
                "ANTHROPIC_MODEL": "other-model",
                "ANTHROPIC_AUTH_TOKEN": "other-private-key",  # secret-guard: allow
            },
            "other": True,
        }
    )
    connection.executemany(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
        (
            (
                PROVIDER_ID,
                "Private target label",
                target,
                "claude",
                1,
                0,
            ),
            (
                OTHER_PROVIDER_ID,
                "Private other label",
                other,
                "claude",
                2,
                0,
            ),
            (
                "openai-row",
                "Private OpenAI label",
                '{"apiKey":"private-openai-key"}',  # secret-guard: allow
                "openai",
                3,
                0,
            ),
        ),
    )
    connection.execute(
        "INSERT INTO proxy_config VALUES (?, ?)",
        ("claude", 0),
    )
    connection.execute(
        "INSERT INTO proxy_live_backup VALUES (?, ?, ?)",
        ("claude", '{"private":"proxy-backup"}', "private-time"),
    )
    connection.commit()
    if journal_mode == "WAL":
        return connection
    connection.close()
    return None


def database_rows(path: pathlib.Path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    try:
        return {
            "providers": connection.execute(
                "SELECT id, name, settings_config, app_type, sort_index, "
                "is_current FROM providers ORDER BY id"
            ).fetchall(),
            "proxy": connection.execute(
                "SELECT * FROM proxy_config ORDER BY app_type"
            ).fetchall(),
            "proxyBackup": connection.execute(
                "SELECT * FROM proxy_live_backup ORDER BY app_type"
            ).fetchall(),
            "version": connection.execute(
                "PRAGMA user_version"
            ).fetchone(),
        }
    finally:
        connection.close()


def target_raw(path: pathlib.Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT settings_config FROM providers "
            "WHERE id=? AND app_type='claude'",
            (PROVIDER_ID,),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise AssertionError("target fixture is missing")
        return row[0]
    finally:
        connection.close()


def target_document(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(target_raw(path))
    if not isinstance(value, dict):
        raise AssertionError("target fixture is invalid")
    return value


def backup_path(root: pathlib.Path) -> pathlib.Path:
    directories = tuple(root.iterdir())
    if len(directories) != 1:
        raise AssertionError(f"expected one backup directory, got {len(directories)}")
    files = tuple(directories[0].iterdir())
    if len(files) != 1:
        raise AssertionError(f"expected one backup file, got {len(files)}")
    return files[0]


class StoppedProcessDetector:
    def detect(self) -> CCSwitchProcessState:
        return CCSwitchProcessState.STOPPED


class RunningProcessDetector:
    def detect(self) -> CCSwitchProcessState:
        return CCSwitchProcessState.RUNNING


class ConnectionProxy:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        update_rowcount: int | None = None,
        fail_update: bool = False,
        fail_commit: bool = False,
        fail_rollback: bool = False,
        fail_close: bool = False,
        commit_exception: BaseException | None = None,
        on_update=None,
    ) -> None:
        self.connection = connection
        self.update_rowcount = update_rowcount
        self.fail_update = fail_update
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_close = fail_close
        self.commit_exception = commit_exception
        self.on_update = on_update
        self.closed = False
        self.rolled_back = False
        self.update_calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ):
        if sql.startswith("UPDATE providers SET settings_config="):
            if self.on_update is not None:
                self.on_update()
            self.update_calls.append((sql, tuple(parameters)))
            if self.fail_update:
                raise sqlite3.DatabaseError(
                    f"private update failure {PRIVATE_URL} {PRIVATE_KEY}"
                )
            if self.update_rowcount is not None:
                return CursorWithRowcount(self.update_rowcount)
        return self.connection.execute(sql, parameters)

    def commit(self) -> None:
        if self.commit_exception is not None:
            raise self.commit_exception
        if self.fail_commit:
            raise sqlite3.DatabaseError(
                f"private commit failure {PRIVATE_URL} {PRIVATE_KEY}"
            )
        self.connection.commit()

    def set_authorizer(self, callback) -> None:
        self.connection.set_authorizer(callback)

    def rollback(self) -> None:
        self.rolled_back = True
        if self.fail_rollback:
            raise sqlite3.DatabaseError(
                f"private rollback failure {PRIVATE_URL} {PRIVATE_KEY}"
            )
        self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()
        if self.fail_close:
            raise sqlite3.DatabaseError(
                f"private close failure {PRIVATE_URL} {PRIVATE_KEY}"
            )


class CursorWithRowcount:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FailingBackupSource:
    def __init__(
        self,
        failure: BaseException | None = None,
    ) -> None:
        self.closed = False
        self.calls: list[dict[str, object]] = []
        self.failure = (
            sqlite3.DatabaseError(
                f"private backup failure {PRIVATE_URL} {PRIVATE_KEY}"
            )
            if failure is None
            else failure
        )

    def backup(self, _destination, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))
        raise self.failure

    def close(self) -> None:
        self.closed = True


class BusyBackupSource:
    def __init__(self) -> None:
        self.closed = False
        self.progress_calls = 0
        self.kwargs: dict[str, object] = {}

    def backup(self, _destination, **kwargs: object) -> None:
        self.kwargs = dict(kwargs)
        progress = kwargs["progress"]
        while True:
            self.progress_calls += 1
            progress(sqlite3.SQLITE_BUSY, 1, 1)

    def close(self) -> None:
        self.closed = True


class IncrementingMonotonic:
    def __init__(self, step: float = 0.05) -> None:
        self.value = 0.0
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        current = self.value
        self.value += self.step
        return current


class ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class TrackingSQLiteConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_observed = False

    def close(self) -> None:
        self.close_observed = True
        super().close()


class DeadlineQuickCheckConnection(TrackingSQLiteConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.clock: ManualMonotonic | None = None
        self.progress_callback = None
        self.progress_cleared = False
        self.interrupt_sql = "PRAGMA quick_check"

    def set_progress_handler(self, callback, instructions: int) -> None:
        self.progress_callback = callback
        if callback is None and instructions == 0:
            self.progress_cleared = True

    def execute(self, sql: str, parameters=()):
        if sql == self.interrupt_sql:
            if self.clock is None or self.progress_callback is None:
                raise AssertionError("quick_check deadline was not installed")
            self.clock.value = 10.0
            if self.progress_callback():
                raise sqlite3.OperationalError("interrupted")
        return super().execute(sql, parameters)


def make_plan(
    database: pathlib.Path,
    *,
    changes: dict[str, tuple[object, object]] | None = None,
):
    raw = target_raw(database)
    return build_change_plan(
        mode=RuntimeMode.COMPANION,
        target=ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id=PROVIDER_ID,
        ),
        store_fingerprint=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        changes=(
            {"models.default": ("model-old", "model-new")}
            if changes is None
            else changes
        ),
    )


def approve(registry: ApprovalRegistry, plan):
    handle = request_tui_approval(
        plan,
        registry,
        show_preview=lambda _preview: None,
        confirm=lambda: True,
    )
    if handle is None:
        raise AssertionError("fixture approval was not issued")
    return handle


def service(
    database: pathlib.Path,
    backups: pathlib.Path,
    **kwargs: object,
) -> CompanionApplyService:
    return CompanionApplyService(
        database,
        backup_root=backups,
        process_detector=StoppedProcessDetector(),
        **kwargs,
    )


class CompanionApplySuccessTests(unittest.TestCase):
    def test_delete_and_wal_backups_are_pre_update_logical_snapshots(
        self,
    ) -> None:
        for journal_mode in ("DELETE", "WAL"):
            with (
                self.subTest(journal_mode=journal_mode),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = pathlib.Path(raw_directory)
                database = directory / "fixture.db"
                backups = directory / "backups"
                backups.mkdir()
                keeper = write_schema(
                    database,
                    journal_mode=journal_mode,
                )
                try:
                    before_rows = database_rows(database)
                    before_target = target_document(database)
                    plan = make_plan(database)
                    registry = ApprovalRegistry(clock=lambda: NOW)
                    handle = approve(registry, plan)

                    result = service(database, backups).apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                    stored_backup = backup_path(backups)
                    backup_rows = database_rows(stored_backup)
                    after_rows = database_rows(database)
                    after_target = target_document(database)
                finally:
                    if keeper is not None:
                        keeper.close()

                self.assertIs(result.status, CompanionApplyStatus.APPLIED)
                self.assertEqual(result.fields, ("models.default",))
                self.assertTrue(result.allowed)
                self.assertTrue(result.backup_created)
                self.assertEqual(result.before_fingerprint, plan.store_fingerprint)
                self.assertNotEqual(
                    result.after_fingerprint,
                    result.before_fingerprint,
                )
                self.assertNotIn(
                    hashlib.sha256(stored_backup.read_bytes()).hexdigest(),
                    json.dumps(result.to_public_dict(), sort_keys=True),
                )
                self.assertEqual(
                    stat.S_IMODE(stored_backup.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(
                    stat.S_IMODE(stored_backup.stat().st_mode),
                    0o600,
                )
                self.assertEqual(backup_rows, before_rows)
                self.assertEqual(
                    after_target["env"]["ANTHROPIC_MODEL"],
                    "model-new",
                )
                expected_target = json.loads(json.dumps(before_target))
                expected_target["env"]["ANTHROPIC_MODEL"] = "model-new"
                self.assertEqual(after_target, expected_target)

                before_by_id = {
                    row[0]: row for row in before_rows["providers"]
                }
                after_by_id = {
                    row[0]: row for row in after_rows["providers"]
                }
                self.assertEqual(
                    before_by_id[OTHER_PROVIDER_ID],
                    after_by_id[OTHER_PROVIDER_ID],
                )
                self.assertEqual(
                    before_by_id["openai-row"],
                    after_by_id["openai-row"],
                )
                self.assertEqual(
                    before_rows["proxy"],
                    after_rows["proxy"],
                )
                self.assertEqual(
                    before_rows["proxyBackup"],
                    after_rows["proxyBackup"],
                )

                public_text = repr(result) + json.dumps(
                    result.to_public_dict(),
                    sort_keys=True,
                )
                for private_value in (
                    str(database),
                    str(stored_backup),
                    PRIVATE_URL,
                    PRIVATE_KEY,
                    canonical_json(before_target),
                ):
                    self.assertNotIn(private_value, public_text)

    def test_apply_is_one_shot_and_second_use_creates_no_backup_or_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            apply_service = service(database, backups)

            first = apply_service.apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )
            after_first = database_rows(database)
            backup_names = tuple(path.name for path in backups.iterdir())

            with self.assertRaises(CompanionPreflightError) as caught:
                apply_service.apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(first.status, CompanionApplyStatus.APPLIED)
            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )
            self.assertEqual(database_rows(database), after_first)
            self.assertEqual(
                tuple(path.name for path in backups.iterdir()),
                backup_names,
            )

    def test_wal_without_shm_is_backed_up_through_sqlite_api(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            keeper = write_schema(database, journal_mode="WAL")
            if keeper is None:
                raise AssertionError("WAL fixture keeper is missing")
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            wal = pathlib.Path(f"{database}-wal")
            shm = pathlib.Path(f"{database}-shm")
            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())
            shm.unlink()
            self.assertTrue(wal.is_file())
            self.assertFalse(shm.exists())
            try:
                result = service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )
            finally:
                keeper.close()

            self.assertIs(result.status, CompanionApplyStatus.APPLIED)
            self.assertEqual(
                target_document(backup_path(backups))["env"][
                    "ANTHROPIC_MODEL"
                ],
                "model-old",
            )
            self.assertEqual(
                target_document(database)["env"]["ANTHROPIC_MODEL"],
                "model-new",
            )


class CompanionApplyPreflightTests(unittest.TestCase):
    def test_preflight_failure_consumes_approval_without_backup_or_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)
            apply_service = CompanionApplyService(
                database,
                backup_root=backups,
                process_detector=RunningProcessDetector(),
            )

            with self.assertRaises(CompanionPreflightError) as first:
                apply_service.apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )
            with self.assertRaises(CompanionPreflightError) as second:
                apply_service.apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                first.exception.status,
                CompanionPreflightStatus.PROCESS_RUNNING,
            )
            self.assertIs(
                second.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())


class CompanionApplyConflictTests(unittest.TestCase):
    def _assert_race_conflict(
        self,
        mutator,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            real_open_writer = companion_apply._open_writer
            raced_rows: dict[str, object] | None = None

            def open_after_race(path: pathlib.Path):
                nonlocal raced_rows
                connection = sqlite3.connect(database)
                try:
                    mutator(connection)
                    connection.commit()
                finally:
                    connection.close()
                raced_rows = database_rows(database)
                return real_open_writer(path)

            with (
                mock.patch.object(
                    companion_apply,
                    "_open_writer",
                    side_effect=open_after_race,
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), raced_rows)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_target_becoming_current_conflicts(self) -> None:
        self._assert_race_conflict(
            lambda connection: connection.execute(
                "UPDATE providers SET is_current=1 WHERE id=?",
                (PROVIDER_ID,),
            )
        )

    def test_target_configuration_change_conflicts(self) -> None:
        self._assert_race_conflict(
            lambda connection: connection.execute(
                "UPDATE providers SET settings_config=? WHERE id=?",
                (
                    canonical_json(
                        settings_document(default="raced-model")
                    ),
                    PROVIDER_ID,
                ),
            )
        )

    def test_target_deletion_conflicts(self) -> None:
        self._assert_race_conflict(
            lambda connection: connection.execute(
                "DELETE FROM providers WHERE id=?",
                (PROVIDER_ID,),
            )
        )

    def test_proxy_takeover_conflicts(self) -> None:
        self._assert_race_conflict(
            lambda connection: connection.execute(
                "UPDATE proxy_config SET live_takeover_active=1 "
                "WHERE app_type='claude'"
            )
        )

    def test_schema_change_conflicts(self) -> None:
        self._assert_race_conflict(
            lambda connection: connection.execute(
                "PRAGMA user_version=15"
            )
        )

    def test_change_old_is_checked_even_when_fingerprint_matches(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(
                database,
                changes={
                    "models.default": (
                        "not-the-stored-old-model",
                        "model-new",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_begin_immediate_lock_conflicts_without_waiting_or_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            real_open_writer = companion_apply._open_writer
            lock_holder: sqlite3.Connection | None = None

            def open_while_locked(path: pathlib.Path):
                nonlocal lock_holder
                lock_holder = sqlite3.connect(
                    database,
                    isolation_level=None,
                )
                lock_holder.execute("BEGIN IMMEDIATE")
                return real_open_writer(path)

            try:
                with (
                    mock.patch.object(
                        companion_apply,
                        "_open_writer",
                        side_effect=open_while_locked,
                    ),
                    self.assertRaises(CompanionApplyError) as caught,
                ):
                    service(database, backups).apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )
            finally:
                if lock_holder is not None:
                    lock_holder.rollback()
                    lock_holder.close()

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_failed_transaction_cannot_reuse_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(
                database,
                changes={
                    "models.default": (
                        "wrong-old",
                        "model-new",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            apply_service = service(database, backups)

            with self.assertRaises(CompanionApplyError):
                apply_service.apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )
            with self.assertRaises(CompanionPreflightError) as caught:
                apply_service.apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )


class CompanionApplyBackupFailureTests(unittest.TestCase):
    def test_backup_create_failure_rolls_back_without_residual_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with (
                mock.patch.object(
                    companion_apply,
                    "_secure_create_file",
                    side_effect=OSError(
                        f"private create failure {database} {PRIVATE_KEY}"
                    ),
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())
            public_text = (
                str(caught.exception)
                + repr(caught.exception)
                + json.dumps(caught.exception.to_public_dict())
            )
            self.assertNotIn(str(database), public_text)
            self.assertNotIn(PRIVATE_KEY, public_text)
            self.assertIsNone(caught.exception.__context__)

    def test_backup_api_failure_closes_source_and_removes_partial_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            source = FailingBackupSource()
            before = database_rows(database)

            with (
                mock.patch.object(
                    companion_apply,
                    "_open_backup_source",
                    return_value=source,
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertTrue(source.closed)
            self.assertEqual(len(source.calls), 1)
            self.assertEqual(source.calls[0]["pages"], BACKUP_PAGES_PER_STEP)
            self.assertEqual(source.calls[0]["sleep"], 0.0)
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_busy_backup_is_cancelled_by_deadline_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            source = BusyBackupSource()
            clock = IncrementingMonotonic(step=0.05)
            before = database_rows(database)

            with (
                mock.patch.object(
                    companion_apply,
                    "_open_backup_source",
                    return_value=source,
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(
                    database,
                    backups,
                    backup_timeout_seconds=0.1,
                    monotonic=clock,
                ).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertTrue(source.closed)
            self.assertGreaterEqual(source.progress_calls, 1)
            self.assertLess(source.progress_calls, 10)
            self.assertEqual(source.kwargs["pages"], BACKUP_PAGES_PER_STEP)
            self.assertEqual(source.kwargs["sleep"], 0.0)
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_keyboard_interrupt_during_backup_is_redacted_and_cleaned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            source = FailingBackupSource(
                KeyboardInterrupt(
                    f"private interrupt {database} {PRIVATE_KEY}"
                )
            )
            before = database_rows(database)

            with (
                mock.patch.object(
                    companion_apply,
                    "_open_backup_source",
                    return_value=source,
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertTrue(source.closed)
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())
            public = (
                str(caught.exception)
                + repr(caught.exception)
                + json.dumps(caught.exception.to_public_dict())
            )
            self.assertNotIn(str(database), public)
            self.assertNotIn(PRIVATE_KEY, public)
            self.assertIsNone(caught.exception.__context__)


class CompanionApplyTransactionFailureTests(unittest.TestCase):
    def _fixture(
        self,
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        pathlib.Path,
        pathlib.Path,
        object,
        ApprovalRegistry,
        object,
    ]:
        temporary = tempfile.TemporaryDirectory()
        directory = pathlib.Path(temporary.name)
        database = directory / "fixture.db"
        backups = directory / "backups"
        backups.mkdir()
        write_schema(database)
        plan = make_plan(database)
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        return temporary, database, backups, plan, registry, handle

    def test_cas_requires_exact_guard_and_rowcount_one(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                update_rowcount=0,
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if proxy is None:
            raise AssertionError("writer proxy was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.CONFLICT,
        )
        self.assertTrue(proxy.rolled_back)
        self.assertTrue(proxy.closed)
        self.assertEqual(len(proxy.update_calls), 1)
        sql, parameters = proxy.update_calls[0]
        self.assertEqual(
            sql,
            "UPDATE providers SET settings_config=? "
            "WHERE id=? AND app_type='claude' AND is_current=0 "
            "AND settings_config=?",
        )
        self.assertEqual(parameters[1], PROVIDER_ID)
        self.assertEqual(parameters[2], target_raw(database))
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)

    def test_update_failure_rolls_back_and_retains_valid_backup(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                fail_update=True,
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if proxy is None:
            raise AssertionError("writer proxy was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.WRITE_FAILED,
        )
        self.assertTrue(proxy.rolled_back)
        self.assertTrue(proxy.closed)
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)
        self.assertNotIn(PRIVATE_URL, repr(caught.exception))
        self.assertNotIn(PRIVATE_KEY, str(caught.exception))

    def test_transaction_readback_failure_rolls_back_update(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)

        with (
            mock.patch.object(
                companion_apply,
                "_verify_transaction_readback",
                side_effect=sqlite3.DatabaseError(
                    f"private readback {database} {PRIVATE_KEY}"
                ),
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.READBACK_FAILED,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)
        self.assertIsNone(caught.exception.__context__)

    def test_commit_failure_is_unknown_and_never_restores(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                fail_commit=True,
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if proxy is None:
            raise AssertionError("writer proxy was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
        )
        self.assertFalse(proxy.rolled_back)
        self.assertTrue(proxy.closed)
        # This injected failure occurs before the delegated commit, so close
        # rolls back.  The public service still correctly treats it as unknown.
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)

    def test_postcommit_readback_failure_is_unknown_and_keeps_commit(
        self,
    ) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)

        with (
            mock.patch.object(
                companion_apply,
                "_verify_fresh_readback",
                side_effect=sqlite3.DatabaseError(
                    f"private postcommit {database} {PRIVATE_KEY}"
                ),
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
        )
        self.assertEqual(
            target_document(database)["env"]["ANTHROPIC_MODEL"],
            "model-new",
        )
        self.assertEqual(database_rows(backup_path(backups)), before)

    def test_keyboard_interrupt_during_commit_is_fixed_unknown(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                commit_exception=KeyboardInterrupt(
                    f"private commit interrupt {database} {PRIVATE_KEY}"
                ),
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if proxy is None:
            raise AssertionError("writer proxy was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
        )
        self.assertFalse(proxy.rolled_back)
        self.assertTrue(proxy.closed)
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)
        public = (
            str(caught.exception)
            + repr(caught.exception)
            + json.dumps(caught.exception.to_public_dict())
        )
        self.assertNotIn(str(database), public)
        self.assertNotIn(PRIVATE_KEY, public)
        self.assertIsNone(caught.exception.__context__)

    def test_rollback_failure_escalates_to_commit_state_unknown(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                fail_update=True,
                fail_rollback=True,
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)

    def test_close_failure_escalates_to_commit_state_unknown(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                fail_update=True,
                fail_close=True,
            )
            return proxy

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
        )
        if proxy is None:
            raise AssertionError("writer proxy was not opened")
        self.assertTrue(proxy.closed)

    def test_provider_trigger_is_denied_and_every_logical_row_rolls_back(
        self,
    ) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                f"""
                CREATE TRIGGER malicious_provider_trigger
                AFTER UPDATE OF settings_config ON providers
                BEGIN
                    UPDATE providers
                    SET settings_config='{{"env":{{"ANTHROPIC_MODEL":"evil"}}}}'
                    WHERE id='{OTHER_PROVIDER_ID}';
                    UPDATE proxy_config
                    SET live_takeover_active=1
                    WHERE app_type='claude';
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()
        before = database_rows(database)

        with self.assertRaises(CompanionApplyError) as caught:
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.WRITE_FAILED,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(database_rows(backup_path(backups)), before)


class CompanionApplyDeletionTests(unittest.TestCase):
    def _apply_deletion(
        self,
        document: dict[str, object],
        *,
        field: str,
        old: object,
    ) -> tuple[dict[str, object], pathlib.Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = pathlib.Path(temporary.name)
        database = directory / "fixture.db"
        backups = directory / "backups"
        backups.mkdir()
        write_schema(database, target_document=document)
        plan = make_plan(
            database,
            changes={field: (old, None)},
        )
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        result = service(database, backups).apply(
            plan=plan,
            approval_registry=registry,
            approval_handle=handle,
        )
        self.assertIs(result.status, CompanionApplyStatus.APPLIED)
        return target_document(database), backups

    def test_none_deletes_the_canonical_field_instead_of_noop(self) -> None:
        after, backups = self._apply_deletion(
            settings_document(),
            field="models.default",
            old="model-old",
        )

        self.assertNotIn("ANTHROPIC_MODEL", after["env"])
        self.assertEqual(
            target_document(backup_path(backups))["env"][
                "ANTHROPIC_MODEL"
            ],
            "model-old",
        )

    def test_none_deletes_the_sole_alias_representation(self) -> None:
        after, _backups = self._apply_deletion(
            settings_document(use_fast_alias=True),
            field="models.fast",
            old="fast-old",
        )

        self.assertNotIn("ANTHROPIC_SMALL_FAST_MODEL", after["env"])
        self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", after["env"])

    def test_none_fails_closed_when_deletion_would_reveal_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            document = settings_document(
                extra_env={
                    "ANTHROPIC_SMALL_FAST_MODEL": "revived-fast-model",
                }
            )
            write_schema(database, target_document=document)
            plan = make_plan(
                database,
                changes={"models.fast": ("fast-old", None)},
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_none_fails_closed_for_null_canonical_plus_live_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            document = settings_document(
                use_fast_alias=True,
                extra_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": None},
            )
            write_schema(database, target_document=document)
            plan = make_plan(
                database,
                changes={"models.fast": ("fast-old", None)},
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_update_fails_for_null_canonical_plus_live_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(
                database,
                target_document=settings_document(
                    use_fast_alias=True,
                    extra_env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": None},
                ),
            )
            plan = make_plan(
                database,
                changes={
                    "models.fast": (
                        "fast-old",
                        "fast-new",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_update_fails_for_canonical_and_alias_pair(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(
                database,
                target_document=settings_document(
                    extra_env={
                        "ANTHROPIC_SMALL_FAST_MODEL": "shadow-fast",
                    }
                ),
            )
            plan = make_plan(
                database,
                changes={
                    "models.fast": (
                        "fast-old",
                        "fast-new",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_update_preserves_the_sole_alias_representation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(
                database,
                target_document=settings_document(use_fast_alias=True),
            )
            plan = make_plan(
                database,
                changes={
                    "models.fast": (
                        "fast-old",
                        "fast-new",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)

            result = service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

            self.assertIs(result.status, CompanionApplyStatus.APPLIED)
            env = target_document(database)["env"]
            self.assertEqual(
                env["ANTHROPIC_SMALL_FAST_MODEL"],
                "fast-new",
            )
            self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)

    def test_absent_slot_can_be_added_when_old_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(
                database,
                target_document=settings_document(default=_ABSENT),
            )
            plan = make_plan(
                database,
                changes={
                    "models.default": (
                        None,
                        "newly-added-model",
                    )
                },
            )
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)

            result = service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

            self.assertIs(result.status, CompanionApplyStatus.APPLIED)
            self.assertEqual(
                target_document(database)["env"]["ANTHROPIC_MODEL"],
                "newly-added-model",
            )


class CompanionApplyDurabilityTests(unittest.TestCase):
    def _fixture(
        self,
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        pathlib.Path,
        pathlib.Path,
        object,
        ApprovalRegistry,
        object,
    ]:
        temporary = tempfile.TemporaryDirectory()
        directory = pathlib.Path(temporary.name)
        database = directory / "fixture.db"
        backups = directory / "backups"
        backups.mkdir()
        write_schema(database)
        plan = make_plan(database)
        registry = ApprovalRegistry(clock=lambda: NOW)
        handle = approve(registry, plan)
        return temporary, database, backups, plan, registry, handle

    def test_leaf_operation_directory_and_root_fsync_precede_update(
        self,
    ) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        events: list[str] = []
        proxy: ConnectionProxy | None = None
        real_open = companion_apply._open_writer
        real_leaf_fsync = companion_apply._fsync_backup_leaf
        real_directory_fsync = companion_apply._fsync_directory

        def open_proxy(path: pathlib.Path) -> ConnectionProxy:
            nonlocal proxy
            proxy = ConnectionProxy(
                real_open(path),
                on_update=lambda: events.append("update"),
            )
            return proxy

        def observe_leaf(*args, **kwargs) -> None:
            if proxy is None:
                raise AssertionError("writer must already hold the transaction")
            self.assertEqual(proxy.update_calls, [])
            events.append("leaf")
            real_leaf_fsync(*args, **kwargs)

        def observe_directory(
            path: pathlib.Path,
            *args,
            **kwargs,
        ) -> None:
            if proxy is None:
                raise AssertionError("writer must already hold the transaction")
            self.assertEqual(proxy.update_calls, [])
            events.append(
                "root" if path == backups else "operation-directory"
            )
            real_directory_fsync(path, *args, **kwargs)

        with (
            mock.patch.object(
                companion_apply,
                "_open_writer",
                side_effect=open_proxy,
            ),
            mock.patch.object(
                companion_apply,
                "_fsync_backup_leaf",
                side_effect=observe_leaf,
            ),
            mock.patch.object(
                companion_apply,
                "_fsync_directory",
                side_effect=observe_directory,
            ),
        ):
            result = service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(result.status, CompanionApplyStatus.APPLIED)
        self.assertEqual(
            events,
            [
                "leaf",
                "operation-directory",
                "root",
                "update",
            ],
        )

    def test_each_durability_failure_prevents_update_and_is_consumed(
        self,
    ) -> None:
        for failed_step in ("leaf", "operation-directory", "root"):
            with self.subTest(failed_step=failed_step):
                (
                    temporary,
                    database,
                    backups,
                    plan,
                    registry,
                    handle,
                ) = self._fixture()
                self.addCleanup(temporary.cleanup)
                before = database_rows(database)
                events: list[str] = []
                proxy: ConnectionProxy | None = None
                real_open = companion_apply._open_writer
                real_leaf_fsync = companion_apply._fsync_backup_leaf
                real_directory_fsync = companion_apply._fsync_directory

                def open_proxy(path: pathlib.Path) -> ConnectionProxy:
                    nonlocal proxy
                    proxy = ConnectionProxy(
                        real_open(path),
                        on_update=lambda: events.append("update"),
                    )
                    return proxy

                def maybe_fail_leaf(*args, **kwargs) -> None:
                    events.append("leaf")
                    if failed_step == "leaf":
                        raise OSError(
                            f"private leaf fsync {database} {PRIVATE_KEY}"
                        )
                    real_leaf_fsync(*args, **kwargs)

                def maybe_fail_directory(
                    path: pathlib.Path,
                    *args,
                    **kwargs,
                ) -> None:
                    step = (
                        "root"
                        if path == backups
                        else "operation-directory"
                    )
                    events.append(step)
                    if failed_step == step:
                        raise OSError(
                            f"private directory fsync {database} {PRIVATE_KEY}"
                        )
                    real_directory_fsync(path, *args, **kwargs)

                apply_service = service(database, backups)
                with (
                    mock.patch.object(
                        companion_apply,
                        "_open_writer",
                        side_effect=open_proxy,
                    ),
                    mock.patch.object(
                        companion_apply,
                        "_fsync_backup_leaf",
                        side_effect=maybe_fail_leaf,
                    ),
                    mock.patch.object(
                        companion_apply,
                        "_fsync_directory",
                        side_effect=maybe_fail_directory,
                    ),
                    self.assertRaises(CompanionApplyError) as caught,
                ):
                    apply_service.apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                self.assertIs(
                    caught.exception.status,
                    CompanionApplyStatus.BACKUP_FAILED,
                )
                self.assertNotIn("update", events)
                if proxy is None:
                    raise AssertionError("writer proxy was not opened")
                self.assertEqual(proxy.update_calls, [])
                self.assertTrue(proxy.rolled_back)
                self.assertTrue(proxy.closed)
                self.assertEqual(database_rows(database), before)
                self.assertEqual(tuple(backups.iterdir()), ())
                public = (
                    str(caught.exception)
                    + repr(caught.exception)
                    + json.dumps(caught.exception.to_public_dict())
                )
                self.assertNotIn(str(database), public)
                self.assertNotIn(PRIVATE_KEY, public)

                with self.assertRaises(CompanionPreflightError) as reused:
                    apply_service.apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )
                self.assertIs(
                    reused.exception.status,
                    CompanionPreflightStatus.APPROVAL_REQUIRED,
                )

    def test_quick_check_uses_backup_deadline_and_clears_handler(
        self,
    ) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        clock = ManualMonotonic()
        destination: DeadlineQuickCheckConnection | None = None

        def open_destination(
            path: pathlib.Path,
        ) -> DeadlineQuickCheckConnection:
            nonlocal destination
            destination = sqlite3.connect(
                companion_apply._sqlite_uri(path, "rw"),
                uri=True,
                isolation_level=None,
                timeout=0.0,
                factory=DeadlineQuickCheckConnection,
            )
            destination.clock = clock
            return destination

        with (
            mock.patch.object(
                companion_apply,
                "_open_backup_destination",
                side_effect=open_destination,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(
                database,
                backups,
                backup_timeout_seconds=1.0,
                monotonic=clock,
            ).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if destination is None:
            raise AssertionError("backup destination was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.BACKUP_FAILED,
        )
        self.assertTrue(destination.progress_cleared)
        self.assertTrue(destination.close_observed)
        self.assertEqual(database_rows(database), before)
        self.assertEqual(tuple(backups.iterdir()), ())

    def test_backup_deadline_covers_schema_validation_sql(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        clock = ManualMonotonic()
        destination: DeadlineQuickCheckConnection | None = None

        def open_destination(
            path: pathlib.Path,
        ) -> DeadlineQuickCheckConnection:
            nonlocal destination
            destination = sqlite3.connect(
                companion_apply._sqlite_uri(path, "rw"),
                uri=True,
                isolation_level=None,
                timeout=0.0,
                factory=DeadlineQuickCheckConnection,
            )
            destination.clock = clock
            destination.interrupt_sql = "PRAGMA user_version"
            return destination

        with (
            mock.patch.object(
                companion_apply,
                "_open_backup_destination",
                side_effect=open_destination,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(
                database,
                backups,
                backup_timeout_seconds=1.0,
                monotonic=clock,
            ).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        if destination is None:
            raise AssertionError("backup destination was not opened")
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.BACKUP_FAILED,
        )
        self.assertTrue(destination.progress_cleared)
        self.assertTrue(destination.close_observed)
        self.assertEqual(database_rows(database), before)
        self.assertEqual(tuple(backups.iterdir()), ())

    def test_copy_write_failure_removes_partial_final_leaf(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        staging_directories: list[pathlib.Path] = []
        real_create_staging = companion_apply._create_staging_workspace

        def observe_staging():
            staging = real_create_staging()
            staging_directories.append(staging.directory)
            return staging

        with (
            mock.patch.object(
                companion_apply,
                "_create_staging_workspace",
                side_effect=observe_staging,
            ),
            mock.patch.object(
                companion_apply,
                "_write_backup_chunk",
                side_effect=OSError(
                    f"private copy write {database} {PRIVATE_KEY}"
                ),
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.BACKUP_FAILED,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(tuple(backups.iterdir()), ())
        self.assertTrue(staging_directories)
        self.assertTrue(
            all(not path.exists() for path in staging_directories)
        )

    def test_copy_deadline_removes_partial_final_leaf(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        clock = ManualMonotonic()
        real_read = companion_apply._read_backup_chunk

        def read_then_expire(*args, **kwargs):
            chunk = real_read(*args, **kwargs)
            clock.value = 10.0
            return chunk

        with (
            mock.patch.object(
                companion_apply,
                "_read_backup_chunk",
                side_effect=read_then_expire,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(
                database,
                backups,
                backup_timeout_seconds=1.0,
                monotonic=clock,
            ).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.BACKUP_FAILED,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(tuple(backups.iterdir()), ())

    def test_copy_close_failure_removes_partial_final_leaf(self) -> None:
        (
            temporary,
            database,
            backups,
            plan,
            registry,
            handle,
        ) = self._fixture()
        self.addCleanup(temporary.cleanup)
        before = database_rows(database)
        real_write = companion_apply._write_backup_chunk
        real_close = companion_apply._close_backup_copy_descriptor
        copy_started = False
        close_failed = False

        def observe_write(*args, **kwargs):
            nonlocal copy_started
            copy_started = True
            return real_write(*args, **kwargs)

        def fail_first_copy_close(descriptor: int) -> None:
            nonlocal close_failed
            if copy_started and not close_failed:
                close_failed = True
                real_close(descriptor)
                raise OSError(
                    f"private copy close {database} {PRIVATE_KEY}"
                )
            real_close(descriptor)

        with (
            mock.patch.object(
                companion_apply,
                "_write_backup_chunk",
                side_effect=observe_write,
            ),
            mock.patch.object(
                companion_apply,
                "_close_backup_copy_descriptor",
                side_effect=fail_first_copy_close,
            ),
            self.assertRaises(CompanionApplyError) as caught,
        ):
            service(database, backups).apply(
                plan=plan,
                approval_registry=registry,
                approval_handle=handle,
            )

        self.assertTrue(close_failed)
        self.assertIs(
            caught.exception.status,
            CompanionApplyStatus.BACKUP_FAILED,
        )
        self.assertEqual(database_rows(database), before)
        self.assertEqual(tuple(backups.iterdir()), ())

    def test_replaced_leaf_is_never_fsynced(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            backup = directory / "backup.db"
            identity = companion_apply._secure_create_file(backup)
            backup.unlink()
            backup.write_bytes(b"replacement")
            backup.chmod(0o600)

            with (
                mock.patch.object(companion_apply.os, "fsync") as fsync,
                self.assertRaises(OSError),
            ):
                companion_apply._fsync_backup_leaf(backup, identity)

            fsync.assert_not_called()
            self.assertEqual(backup.read_bytes(), b"replacement")

    def test_unknown_operation_entry_is_not_fsynced_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = pathlib.Path(raw_directory)
            operation = root / "operation"
            operation.mkdir(mode=0o700)
            operation.chmod(0o700)
            backup = operation / "cc-switch.db"
            backup_identity = companion_apply._secure_create_file(backup)
            unknown = operation / "unknown-user-file"
            unknown.write_text("preserve", encoding="utf-8")
            operation_stat = operation.stat()
            root_stat = root.stat()
            workspace = companion_apply._BackupWorkspace(
                path=operation,
                name=operation.name,
                identity=(
                    operation_stat.st_dev,
                    operation_stat.st_ino,
                ),
                root_path=root,
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                backup_identity=backup_identity,
            )

            with (
                mock.patch.object(
                    companion_apply,
                    "_fsync_backup_leaf",
                ) as leaf_fsync,
                mock.patch.object(
                    companion_apply,
                    "_fsync_directory",
                ) as directory_fsync,
                self.assertRaises(OSError),
            ):
                companion_apply._durability_barrier(workspace)

            leaf_fsync.assert_not_called()
            directory_fsync.assert_not_called()
            companion_apply._cleanup_backup_workspace(workspace)
            self.assertEqual(
                unknown.read_text(encoding="utf-8"),
                "preserve",
            )


class CompanionApplyHardeningTests(unittest.TestCase):
    def test_cleanup_never_deletes_unknown_or_replaced_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = pathlib.Path(raw_directory)
            operation = root / "operation"
            operation.mkdir(mode=0o700)
            operation.chmod(0o700)
            backup = operation / "cc-switch.db"
            backup.write_bytes(b"original")
            backup.chmod(0o600)
            directory_stat = operation.stat()
            backup_stat = backup.stat()
            workspace = companion_apply._BackupWorkspace(
                path=operation,
                name=operation.name,
                identity=(directory_stat.st_dev, directory_stat.st_ino),
                root_path=root,
                root_identity=(root.stat().st_dev, root.stat().st_ino),
                backup_identity=(backup_stat.st_dev, backup_stat.st_ino),
            )
            unknown = operation / "unknown-user-file"
            unknown.write_text("preserve", encoding="utf-8")

            companion_apply._cleanup_backup_workspace(workspace)

            self.assertTrue(operation.is_dir())
            self.assertTrue(unknown.is_file())
            self.assertFalse(backup.exists())

            replacement = operation / "cc-switch.db"
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o600)
            companion_apply._cleanup_backup_workspace(workspace)
            self.assertEqual(replacement.read_bytes(), b"replacement")
            self.assertEqual(unknown.read_text(encoding="utf-8"), "preserve")

    def test_cleanup_never_claims_injected_sqlite_sidecar_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = pathlib.Path(raw_directory)
            operation = root / "operation"
            operation.mkdir(mode=0o700)
            operation.chmod(0o700)
            backup = operation / "cc-switch.db"
            backup_identity = companion_apply._secure_create_file(backup)
            operation_stat = operation.stat()
            root_stat = root.stat()
            workspace = companion_apply._BackupWorkspace(
                path=operation,
                name=operation.name,
                identity=(
                    operation_stat.st_dev,
                    operation_stat.st_ino,
                ),
                root_path=root,
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                backup_identity=backup_identity,
            )
            sidecars = tuple(
                operation / f"cc-switch.db-{suffix}"
                for suffix in ("journal", "wal", "shm")
            )
            for sidecar in sidecars:
                sidecar.write_text("injected", encoding="utf-8")

            companion_apply._cleanup_backup_workspace(workspace)

            self.assertFalse(backup.exists())
            self.assertTrue(operation.is_dir())
            for sidecar in sidecars:
                self.assertEqual(
                    sidecar.read_text(encoding="utf-8"),
                    "injected",
                )

    @unittest.skipUnless(
        os.name == "posix",
        "dirfd race protection requires POSIX",
    )
    def test_backup_root_rename_and_symlink_race_never_redirects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            moved_backups = directory / "moved-backups"
            redirect = directory / "redirect"
            backups.mkdir()
            redirect.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)
            real_mkdir = companion_apply.os.mkdir
            raced = False

            def mkdir_after_replacement(
                path,
                mode=0o777,
                *,
                dir_fd=None,
            ):
                nonlocal raced
                if dir_fd is not None and not raced:
                    raced = True
                    backups.rename(moved_backups)
                    backups.symlink_to(
                        redirect,
                        target_is_directory=True,
                    )
                return real_mkdir(
                    path,
                    mode,
                    dir_fd=dir_fd,
                )

            with (
                mock.patch.object(
                    companion_apply.os,
                    "mkdir",
                    side_effect=mkdir_after_replacement,
                ),
                self.assertRaises(CompanionApplyError) as caught,
            ):
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertTrue(raced)
            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(redirect.iterdir()), ())
            self.assertEqual(tuple(moved_backups.iterdir()), ())
            with self.assertRaises(CompanionPreflightError) as reused:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )
            self.assertIs(
                reused.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )

    def test_non_posix_root_replacement_never_receives_database_bytes(
        self,
    ) -> None:
        for replacement_phase in ("before-create", "after-create"):
            with (
                self.subTest(replacement_phase=replacement_phase),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = pathlib.Path(raw_directory)
                database = directory / "fixture.db"
                backups = directory / "backups"
                moved_backups = directory / "moved-backups"
                backups.mkdir()
                write_schema(database)
                plan = make_plan(database)
                registry = ApprovalRegistry(clock=lambda: NOW)
                handle = approve(registry, plan)
                before = database_rows(database)
                real_open_leaf = companion_apply._secure_open_file
                real_write = companion_apply._write_backup_chunk
                raced = False
                write_observations: list[bytes] = []

                def replace_root(operation_name: str) -> None:
                    backups.rename(moved_backups)
                    backups.mkdir()
                    (backups / operation_name).mkdir()

                def open_during_replacement(path: pathlib.Path):
                    nonlocal raced
                    is_final_leaf = (
                        not raced
                        and path.name == "cc-switch.db"
                        and path.parent.parent == backups
                    )
                    if (
                        is_final_leaf
                        and replacement_phase == "before-create"
                    ):
                        replace_root(path.parent.name)
                        raced = True
                    opened = real_open_leaf(path)
                    if (
                        is_final_leaf
                        and replacement_phase == "after-create"
                    ):
                        replace_root(path.parent.name)
                        raced = True
                    return opened

                def observe_write(
                    descriptor: int,
                    value: memoryview,
                ) -> int:
                    replacement_bytes = b"".join(
                        candidate.read_bytes()
                        for candidate in backups.rglob("*")
                        if candidate.is_file()
                    )
                    write_observations.append(replacement_bytes)
                    return real_write(descriptor, value)

                with (
                    mock.patch.object(
                        companion_apply,
                        "_strict_posix_permissions",
                        return_value=False,
                    ),
                    mock.patch.object(
                        companion_apply,
                        "_secure_open_file",
                        side_effect=open_during_replacement,
                    ),
                    mock.patch.object(
                        companion_apply,
                        "_write_backup_chunk",
                        side_effect=observe_write,
                    ),
                    self.assertRaises(CompanionApplyError) as caught,
                ):
                    service(database, backups).apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )

                self.assertTrue(raced)
                self.assertIs(
                    caught.exception.status,
                    CompanionApplyStatus.BACKUP_FAILED,
                )
                self.assertEqual(write_observations, [])
                self.assertEqual(database_rows(database), before)
                for backup_root in (backups, moved_backups):
                    for candidate in backup_root.rglob("*"):
                        if not candidate.is_file():
                            continue
                        value = candidate.read_bytes()
                        self.assertEqual(value, b"")
                        self.assertNotIn(PRIVATE_KEY.encode(), value)
                        self.assertNotIn(PRIVATE_URL.encode(), value)
                        self.assertFalse(
                            value.startswith(b"SQLite format 3")
                        )
                with self.assertRaises(
                    CompanionPreflightError
                ) as reused:
                    service(database, backups).apply(
                        plan=plan,
                        approval_registry=registry,
                        approval_handle=handle,
                    )
                self.assertIs(
                    reused.exception.status,
                    CompanionPreflightStatus.APPROVAL_REQUIRED,
                )

    @unittest.skipUnless(
        os.name == "posix",
        "POSIX fchmod/fstat injection",
    )
    def test_partial_secure_create_cleans_its_owned_inode(self) -> None:
        for failed_step in ("fchmod", "fstat", "close"):
            with (
                self.subTest(failed_step=failed_step),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                path = pathlib.Path(raw_directory) / "backup.db"
                with contextlib.ExitStack() as stack:
                    if failed_step == "fchmod":
                        stack.enter_context(
                            mock.patch.object(
                                companion_apply.os,
                                "fchmod",
                                side_effect=OSError("private fchmod"),
                            )
                        )
                    elif failed_step == "fstat":
                        real_fstat = companion_apply.os.fstat
                        calls = 0

                        def fail_first_fstat(descriptor: int):
                            nonlocal calls
                            calls += 1
                            if calls == 1:
                                raise OSError("private fstat")
                            return real_fstat(descriptor)

                        stack.enter_context(
                            mock.patch.object(
                                companion_apply.os,
                                "fstat",
                                side_effect=fail_first_fstat,
                            )
                        )
                    else:
                        real_close = companion_apply.os.close
                        failed = False

                        def close_then_fail(descriptor: int):
                            nonlocal failed
                            real_close(descriptor)
                            if not failed:
                                failed = True
                                raise OSError("private close")

                        stack.enter_context(
                            mock.patch.object(
                                companion_apply.os,
                                "close",
                                side_effect=close_then_fail,
                            )
                        )

                    with self.assertRaises(OSError):
                        companion_apply._secure_create_file(path)

                self.assertFalse(path.exists())

    def test_non_posix_secure_file_does_not_require_fchmod_or_mode_bits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "backup.db"
            with (
                mock.patch.object(
                    companion_apply,
                    "_strict_posix_permissions",
                    return_value=False,
                ),
                mock.patch.object(
                    companion_apply.os,
                    "fchmod",
                    side_effect=AssertionError("fchmod must not be used"),
                    create=True,
                ),
            ):
                identity = companion_apply._secure_create_file(path)

            metadata = path.stat()
            self.assertEqual(
                identity,
                (metadata.st_dev, metadata.st_ino),
            )
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(metadata.st_nlink, 1)

    def test_symlink_backup_root_fails_without_touching_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are unavailable")
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            real_backups = directory / "real-backups"
            real_backups.mkdir()
            backup_link = directory / "backup-link"
            backup_link.symlink_to(real_backups, target_is_directory=True)
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                CompanionApplyService(
                    database,
                    backup_root=backup_link,
                    process_detector=StoppedProcessDetector(),
                ).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.BACKUP_FAILED,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(real_backups.iterdir()), ())

    def test_hardlinked_source_database_fails_closed(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links are unavailable")
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            hardlink = directory / "fixture-hardlink.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            os.link(database, hardlink)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            before = database_rows(database)

            with self.assertRaises(CompanionApplyError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionApplyStatus.CONFLICT,
            )
            self.assertEqual(database_rows(database), before)
            self.assertEqual(tuple(backups.iterdir()), ())

    def test_hardlinked_wal_and_shm_fail_before_writer_open(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links are unavailable")
        for suffix in ("-wal", "-shm"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                directory = pathlib.Path(raw_directory)
                database = directory / "fixture.db"
                backups = directory / "backups"
                backups.mkdir()
                keeper = write_schema(database, journal_mode="WAL")
                if keeper is None:
                    raise AssertionError("WAL fixture keeper is missing")
                try:
                    plan = make_plan(database)
                    registry = ApprovalRegistry(clock=lambda: NOW)
                    handle = approve(registry, plan)
                    target_before = target_raw(database)
                    source_sidecar = pathlib.Path(f"{database}{suffix}")
                    outside = directory / f"outside{suffix}"
                    self.assertTrue(source_sidecar.is_file())
                    os.link(source_sidecar, outside)
                    outside_before = outside.read_bytes()

                    with self.assertRaises(CompanionApplyError) as caught:
                        service(database, backups).apply(
                            plan=plan,
                            approval_registry=registry,
                            approval_handle=handle,
                        )

                    # Check the external inode before any subsequent SQLite
                    # operation can legitimately touch WAL coordination state.
                    self.assertEqual(outside.read_bytes(), outside_before)
                    self.assertIs(
                        caught.exception.status,
                        CompanionApplyStatus.CONFLICT,
                    )
                    self.assertEqual(tuple(backups.iterdir()), ())
                    self.assertEqual(target_raw(database), target_before)
                    with self.assertRaises(
                        CompanionPreflightError
                    ) as reused:
                        service(database, backups).apply(
                            plan=plan,
                            approval_registry=registry,
                            approval_handle=handle,
                        )
                    self.assertIs(
                        reused.exception.status,
                        CompanionPreflightStatus.APPROVAL_REQUIRED,
                    )
                finally:
                    keeper.close()

    def test_hardlinked_rollback_journal_is_never_opened(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links are unavailable")
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            target_before = target_raw(database)
            journal = pathlib.Path(f"{database}-journal")
            journal.write_bytes(b"fixture rollback state")
            outside = directory / "outside-journal"
            os.link(journal, outside)
            outside_before = outside.read_bytes()

            with self.assertRaises(CompanionPreflightError) as caught:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(
                caught.exception.status,
                CompanionPreflightStatus.STORE_UNAVAILABLE,
            )
            self.assertEqual(outside.read_bytes(), outside_before)
            self.assertEqual(target_raw(database), target_before)
            self.assertEqual(tuple(backups.iterdir()), ())
            with self.assertRaises(CompanionPreflightError) as reused:
                service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )
            self.assertIs(
                reused.exception.status,
                CompanionPreflightStatus.APPROVAL_REQUIRED,
            )

    def test_writer_source_and_destination_connections_all_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            opened: list[TrackingSQLiteConnection] = []

            def tracking_connection(path: pathlib.Path, mode: str):
                connection = sqlite3.connect(
                    companion_apply._sqlite_uri(path, mode),
                    uri=True,
                    isolation_level=None,
                    timeout=0.0,
                    factory=TrackingSQLiteConnection,
                )
                opened.append(connection)
                return connection

            with (
                mock.patch.object(
                    companion_apply,
                    "_open_writer",
                    side_effect=lambda path: tracking_connection(path, "rw"),
                ),
                mock.patch.object(
                    companion_apply,
                    "_open_backup_source",
                    side_effect=lambda path: tracking_connection(path, "ro"),
                ),
                mock.patch.object(
                    companion_apply,
                    "_open_backup_destination",
                    side_effect=lambda path: tracking_connection(path, "rw"),
                ),
            ):
                result = service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(result.status, CompanionApplyStatus.APPLIED)
            self.assertEqual(len(opened), 3)
            self.assertTrue(
                all(connection.close_observed for connection in opened)
            )

    @unittest.skipUnless(
        os.name == "posix",
        "external SQLite lock observation requires POSIX",
    )
    def test_closing_backup_source_does_not_release_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            backups = directory / "backups"
            backups.mkdir()
            write_schema(database)
            plan = make_plan(database)
            registry = ApprovalRegistry(clock=lambda: NOW)
            handle = approve(registry, plan)
            real_backup = companion_apply._online_backup
            observations: list[str] = []
            script = (
                "import sqlite3,sys\n"
                "connection=sqlite3.connect("
                "sys.argv[1],isolation_level=None,timeout=0)\n"
                "try:\n"
                " connection.execute('BEGIN IMMEDIATE')\n"
                "except sqlite3.OperationalError:\n"
                " print('BUSY')\n"
                "else:\n"
                " print('UNLOCKED')\n"
                "connection.close()\n"
            )

            def backup_then_probe(**kwargs):
                artifact = real_backup(**kwargs)
                completed = subprocess.run(
                    (sys.executable, "-I", "-c", script, str(database)),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                observations.append(completed.stdout.strip())
                return artifact

            with mock.patch.object(
                companion_apply,
                "_online_backup",
                side_effect=backup_then_probe,
            ):
                result = service(database, backups).apply(
                    plan=plan,
                    approval_registry=registry,
                    approval_handle=handle,
                )

            self.assertIs(result.status, CompanionApplyStatus.APPLIED)
            self.assertEqual(observations, ["BUSY"])


if __name__ == "__main__":
    unittest.main()
