from __future__ import annotations

import errno
import io
import json
import os
import pathlib
import select
import signal
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    import pty
except ImportError:  # pragma: no cover - Windows has no POSIX PTY module.
    pty = None


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.approval import ApprovalRegistry  # noqa: E402
from claude_hub.ccswitch import CC_SWITCH_DB_ENV  # noqa: E402
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    build_change_plan,
    tui_preview,
)
from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProviderInspection,
    ProviderRef,
    RuntimeMode,
    StoreCapability,
)
from claude_hub.service import ProviderApplicationService  # noqa: E402
from claude_hub.store import ProviderConfigCorruptError  # noqa: E402
from claude_hub.switchctl import (  # noqa: E402
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    EXIT_USAGE,
    main,
)
from claude_hub.testing import InMemoryProviderStore  # noqa: E402
from claude_hub.tui import (  # noqa: E402
    APPROVAL_CONFIRMATION_PHRASE,
    request_tui_approval,
)


SUCCESS_GOLDEN = (
    '{"schemaVersion":1,"ok":true,'
    '"data":{"capability":"compatible"},"error":null}\n'
)
FAILURE_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"runtime_error","message":"detect failed"}}\n'
)
USAGE_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"usage_error","message":"usage: switchctl detect"}}\n'
)
DEFAULT_GOLDEN = (
    '{"schemaVersion":1,"ok":true,'
    '"data":{"capability":"absent"},"error":null}\n'
)
HELP_GOLDEN = (
    '{"schemaVersion":1,"ok":true,"data":{"usage":['
    '"switchctl detect","switchctl list",'
    '"switchctl inspect <stable-id>",'
    '"switchctl mode [--store standalone]",'
    '"switchctl route [--store standalone]",'
    '"switchctl apply"]},"error":null}\n'
)
CONFIRMATION_REQUIRED_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"confirmation_required",'
    '"message":"in-process human confirmation is required"}}\n'
)
APPLY_NOT_IMPLEMENTED_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"apply_not_implemented",'
    '"message":"Store apply is not implemented"}}\n'
)
APPLY_RUNTIME_ERROR_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"runtime_error","message":"apply failed"}}\n'
)


class InteractiveStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def write_switchctl_fixture(
    database: pathlib.Path,
    *,
    raw_settings: str,
    provider_name: str = "Fixture Private Provider",
) -> None:
    connection = sqlite3.connect(database)
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
                original_config TEXT NOT NULL
            );
            PRAGMA user_version=16;
            """
        )
        connection.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
            (
                "fixture-provider",
                provider_name,
                raw_settings,
                "claude",
                1,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO proxy_config VALUES (?, ?)",
            ("claude", 1),
        )
        connection.commit()
    finally:
        connection.close()


class _ExplodingStore:
    def __init__(self, private_detail: str) -> None:
        self._private_detail = private_detail

    def detect(self) -> StoreCapability:
        raise RuntimeError(self._private_detail)

    def list(self):
        return ()

    def inspect(self, reference):
        raise RuntimeError(self._private_detail)


class SwitchctlDetectTests(unittest.TestCase):
    def _run(
        self,
        argv: list[str],
        *,
        service: ProviderApplicationService | None = None,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            argv,
            service=service,
            stdout=stdout,
            stderr=stderr,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_detect_success_matches_single_line_golden(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(capability=StoreCapability.COMPATIBLE)
        )

        exit_code, stdout, stderr = self._run(["detect"], service=service)

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, SUCCESS_GOLDEN)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(
            list(json.loads(stdout)),
            ["schemaVersion", "ok", "data", "error"],
        )

    def test_default_detect_reports_absent_for_injected_missing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            missing = pathlib.Path(raw_directory) / "missing.db"
            with mock.patch.dict(
                os.environ,
                {CC_SWITCH_DB_ENV: str(missing)},
            ):
                exit_code, stdout, stderr = self._run(["detect"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, DEFAULT_GOLDEN)
        self.assertEqual(stderr, "")

    def test_default_detect_uses_environment_override_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE providers (
                        id TEXT, name TEXT, settings_config TEXT,
                        app_type TEXT, sort_index INTEGER, is_current INTEGER
                    );
                    CREATE TABLE proxy_config (
                        app_type TEXT, live_takeover_active INTEGER
                    );
                    CREATE TABLE proxy_live_backup (
                        app_type TEXT, original_config TEXT
                    );
                    PRAGMA user_version=16;
                    """
                )
                connection.commit()
            finally:
                connection.close()
            with mock.patch.dict(
                os.environ,
                {CC_SWITCH_DB_ENV: str(database)},
            ):
                exit_code, stdout, stderr = self._run(["detect"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, SUCCESS_GOLDEN)
        self.assertEqual(stderr, "")

    def test_runtime_failure_matches_golden_and_redacts_exception(self) -> None:
        sensitive = "fixture-" + "secret" + "-material"
        private_path = str(pathlib.Path("/private") / "fixture" / "store.db")
        exception_detail = f"{private_path}: {sensitive}"
        service = ProviderApplicationService(_ExplodingStore(exception_detail))

        exit_code, stdout, stderr = self._run(["detect"], service=service)

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout, FAILURE_GOLDEN)
        self.assertEqual(stderr, "switchctl: runtime_error\n")
        combined = stdout + stderr
        self.assertNotIn(sensitive, combined)
        self.assertNotIn(private_path, combined)
        self.assertNotIn("RuntimeError", combined)
        self.assertNotIn("ExplodingStore", combined)

    def test_default_service_construction_failure_uses_runtime_envelope(self) -> None:
        sensitive = "fixture-" + "secret" + "-construction"
        private_path = str(pathlib.Path("/private") / "fixture" / "store.db")
        with mock.patch(
            "claude_hub.switchctl.build_default_service",
            side_effect=RuntimeError(f"{private_path}: {sensitive}"),
        ):
            exit_code, stdout, stderr = self._run(["detect"])

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout, FAILURE_GOLDEN)
        self.assertEqual(stderr, "switchctl: runtime_error\n")
        combined = stdout + stderr
        self.assertNotIn(sensitive, combined)
        self.assertNotIn(private_path, combined)
        self.assertNotIn("RuntimeError", combined)

    def test_service_construction_failures_are_redacted_for_new_commands(
        self,
    ) -> None:
        sensitive = "fixture-" + "secret" + "-construction"
        private_path = str(pathlib.Path("/private") / "fixture" / "store.db")
        for argv, message in (
            (["list"], "list failed"),
            (["inspect", "fixture-provider"], "inspect failed"),
            (["mode"], "mode failed"),
        ):
            with self.subTest(command=argv[0]), mock.patch(
                "claude_hub.switchctl.build_default_service",
                side_effect=RuntimeError(f"{private_path}: {sensitive}"),
            ):
                exit_code, stdout, stderr = self._run(argv)

            self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
            self.assertEqual(
                json.loads(stdout)["error"],
                {"code": "runtime_error", "message": message},
            )
            self.assertEqual(stderr, "switchctl: runtime_error\n")
            self.assertNotIn(sensitive, stdout + stderr)
            self.assertNotIn(private_path, stdout + stderr)

    def test_usage_error_is_stable_and_does_not_echo_argv(self) -> None:
        sensitive_argument = "fixture-" + "secret" + "-argument"

        exit_code, stdout, stderr = self._run(
            ["detect", sensitive_argument]
        )

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(stdout, USAGE_GOLDEN)
        self.assertEqual(stderr, "switchctl: usage_error\n")
        self.assertNotIn(sensitive_argument, stdout + stderr)

    def test_injected_argv_does_not_consult_process_argv(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(capability=StoreCapability.COMPATIBLE)
        )
        with mock.patch.object(
            sys,
            "argv",
            ["/private/fixture/switchctl", "unexpected-command"],
        ):
            exit_code, stdout, stderr = self._run(
                ["detect"],
                service=service,
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, SUCCESS_GOLDEN)
        self.assertEqual(stderr, "")
        self.assertNotIn("/private/fixture", stdout)

    def test_help_is_also_a_single_json_document(self) -> None:
        exit_code, stdout, stderr = self._run(["--help"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, HELP_GOLDEN)
        self.assertEqual(
            json.loads(stdout),
            {
                "schemaVersion": 1,
                "ok": True,
                "data": {
                    "usage": [
                        "switchctl detect",
                        "switchctl list",
                        "switchctl inspect <stable-id>",
                        "switchctl mode [--store standalone]",
                        "switchctl route [--store standalone]",
                        "switchctl apply",
                    ]
                },
                "error": None,
            },
        )
        self.assertEqual(stdout.count("\n"), 1)


class SwitchctlReadCommandsTests(unittest.TestCase):
    def _run(
        self,
        argv: list[str],
        *,
        service: ProviderApplicationService,
        standalone_exists: bool = False,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            argv,
            service=service,
            stdout=stdout,
            stderr=stderr,
            standalone_exists=standalone_exists,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_list_returns_stable_ids_and_current_without_names(self) -> None:
        references = (
            ProviderRef(
                store="memory",
                provider_id="fixture-one",
                is_current=False,
            ),
            ProviderRef(
                store="memory",
                provider_id="fixture-two",
                is_current=True,
            ),
        )
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.COMPATIBLE,
                providers=references,
            )
        )

        exit_code, stdout, stderr = self._run(["list"], service=service)

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schemaVersion": 1,
                "ok": True,
                "data": {
                    "providers": [
                        {"stableId": "fixture-one", "current": False},
                        {"stableId": "fixture-two", "current": True},
                    ]
                },
                "error": None,
            },
        )

    def test_inspect_returns_only_redacted_contract_fields(self) -> None:
        reference = ProviderRef(
            store="memory",
            provider_id="fixture-provider",
            is_current=True,
        )
        inspection = ProviderInspection(
            reference=reference,
            models=ModelMapping(
                default="fixture-default",
                fast="fixture-fast",
            ),
            is_current=True,
            fingerprint="a" * 64,
            proxy_takeover=True,
            schema_capability=StoreCapability.COMPATIBLE,
            unknown_field_count=3,
            unknown_fingerprint="b" * 64,
        )
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.COMPATIBLE,
                providers=(reference,),
                inspections=(inspection,),
            )
        )

        exit_code, stdout, stderr = self._run(
            ["inspect", "fixture-provider"],
            service=service,
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schemaVersion": 1,
                "ok": True,
                "data": {
                    "stableId": "fixture-provider",
                    "models": {
                        "default": "fixture-default",
                        "fast": "fixture-fast",
                    },
                    "configurationFingerprint": "a" * 64,
                    "current": True,
                    "proxyTakeover": True,
                    "schemaCapability": "compatible",
                    "unknownFields": {
                        "count": 3,
                        "fingerprint": "b" * 64,
                    },
                },
                "error": None,
            },
        )

    def test_mode_command_uses_shared_resolver_and_explicit_standalone(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.INCOMPATIBLE,
            )
        )

        exit_code, stdout, stderr = self._run(
            ["mode", "--store", "standalone"],
            service=service,
            standalone_exists=True,
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["data"],
            {"mode": "standalone", "firstScreen": "profile_list"},
        )

    def test_mode_command_exposes_incompatible_error_screen_without_fallback(
        self,
    ) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.CORRUPT,
            )
        )

        exit_code, stdout, stderr = self._run(
            ["mode"],
            service=service,
            standalone_exists=True,
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["data"],
            {"mode": "incompatible", "firstScreen": "incompatible_error"},
        )

    def test_mode_command_routes_all_four_modes(self) -> None:
        cases = (
            (
                StoreCapability.COMPATIBLE,
                False,
                {"mode": "companion", "firstScreen": "provider_list"},
            ),
            (
                StoreCapability.ABSENT,
                True,
                {"mode": "standalone", "firstScreen": "profile_list"},
            ),
            (
                StoreCapability.ABSENT,
                False,
                {"mode": "empty", "firstScreen": "quick_setup"},
            ),
            (
                StoreCapability.INCOMPATIBLE,
                True,
                {
                    "mode": "incompatible",
                    "firstScreen": "incompatible_error",
                },
            ),
        )

        for capability, standalone_exists, expected in cases:
            with self.subTest(capability=capability):
                service = ProviderApplicationService(
                    InMemoryProviderStore(capability=capability)
                )

                exit_code, stdout, stderr = self._run(
                    ["mode"],
                    service=service,
                    standalone_exists=standalone_exists,
                )

                self.assertEqual(exit_code, EXIT_OK)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["data"], expected)

    def test_real_inspect_output_excludes_private_provider_fields_and_paths(
        self,
    ) -> None:
        private_values = (
            "Fixture Private Provider",
            "fixture-auth-material",
            "https://fixture.invalid/private",
            "fixture-header-material",
            "fixture-error-detail",
        )
        raw_settings = json.dumps(
            {
                "env": {
                    "ANTHROPIC_MODEL": "fixture-model",
                    "ANTHROPIC_AUTH_TOKEN": private_values[1],
                    "ANTHROPIC_BASE_URL": private_values[2],
                    "HTTP_HEADERS": {
                        "Authorization": "Bearer " + private_values[3]
                    },
                },
                "last_error": private_values[4],
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "private" / "fixture.db"
            database.parent.mkdir()
            local_settings = directory / "settings.json"
            local_settings.write_bytes(b'{"fixture":"unchanged"}\n')
            write_switchctl_fixture(
                database,
                raw_settings=raw_settings,
                provider_name=private_values[0],
            )
            before = (database.read_bytes(), local_settings.read_bytes())
            with mock.patch.dict(
                os.environ,
                {CC_SWITCH_DB_ENV: str(database)},
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                exit_code = main(
                    ["inspect", "fixture-provider"],
                    stdout=stdout,
                    stderr=stderr,
                )
            after = (database.read_bytes(), local_settings.read_bytes())

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(before, after)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["data"]["models"], {"default": "fixture-model"})
        self.assertEqual(payload["data"]["unknownFields"]["count"], 4)
        combined = stdout.getvalue() + stderr.getvalue()
        for private_value in (*private_values, str(database)):
            self.assertNotIn(private_value, combined)
        self.assertNotIn(raw_settings, combined)

    def test_invalid_json_variants_use_stable_code_without_raw_detail(
        self,
    ) -> None:
        fixtures = (
            (
                "malformed",
                '{"env":{"ANTHROPIC_AUTH_TOKEN":"fixture-private-malformed"',
                ("fixture-private-malformed",),
            ),
            (
                "duplicate-key",
                (
                    '{"env":{"ANTHROPIC_MODEL":"fixture-private-first",'
                    '"ANTHROPIC_MODEL":"fixture-private-second"}}'
                ),
                ("fixture-private-first", "fixture-private-second"),
            ),
            (
                "non-finite",
                '{"env":{},"fixture_private":"fixture-private-nan","x":NaN}',
                ("fixture-private-nan", "NaN"),
            ),
            (
                "oversized",
                (
                    '{"fixture_private":"fixture-private-oversized",'
                    f'"padding":"{"x" * (4 * 1024 * 1024)}"}}'
                ),
                ("fixture-private-oversized",),
            ),
        )
        expected_error = {
            "schemaVersion": 1,
            "ok": False,
            "data": None,
            "error": {
                "code": "provider_config_corrupt",
                "message": "provider configuration is invalid",
            },
        }

        for label, raw_settings, private_values in fixtures:
            with self.subTest(fixture=label), tempfile.TemporaryDirectory() as raw_directory:
                database = pathlib.Path(raw_directory) / "fixture.db"
                write_switchctl_fixture(
                    database,
                    raw_settings=raw_settings,
                )
                before = database.read_bytes()
                with mock.patch.dict(
                    os.environ,
                    {CC_SWITCH_DB_ENV: str(database)},
                ):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    exit_code = main(
                        ["inspect", "fixture-provider"],
                        stdout=stdout,
                        stderr=stderr,
                    )
                self.assertEqual(database.read_bytes(), before)

                self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    expected_error,
                )
                self.assertEqual(
                    stderr.getvalue(),
                    "switchctl: provider_config_corrupt\n",
                )
                combined = stdout.getvalue() + stderr.getvalue()
                for private_value in private_values:
                    self.assertNotIn(private_value, combined)
                self.assertNotIn(str(database), combined)

    def test_inspect_usage_does_not_echo_extra_stable_id_material(self) -> None:
        service = ProviderApplicationService(InMemoryProviderStore())
        private_argument = "fixture-private-argument"

        exit_code, stdout, stderr = self._run(
            ["inspect", "fixture-provider", private_argument],
            service=service,
        )

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(
            json.loads(stdout)["error"],
            {
                "code": "usage_error",
                "message": "usage: switchctl inspect <stable-id>",
            },
        )
        self.assertEqual(stderr, "switchctl: usage_error\n")
        self.assertNotIn(private_argument, stdout + stderr)

    def test_unknown_stable_id_uses_redacted_not_found_error(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.COMPATIBLE,
            )
        )
        unknown = "fixture-unknown-provider"

        exit_code, stdout, stderr = self._run(
            ["inspect", unknown],
            service=service,
        )

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(
            json.loads(stdout)["error"],
            {
                "code": "provider_not_found",
                "message": "provider reference was not found",
            },
        )
        self.assertEqual(stderr, "switchctl: provider_not_found\n")
        self.assertNotIn(unknown, stdout + stderr)


class SwitchctlApplyTests(unittest.TestCase):
    def _plan(
        self,
        *,
        target_id: str = "provider-public-id",
        model_new: str = "model-new",
        display_name: str | None = None,
    ):
        return build_change_plan(
            mode=RuntimeMode.COMPANION,
            target=ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id=target_id,
                display_name=display_name,
            ),
            store_fingerprint="a1" * 32,
            changes={
                "models.default": (
                    "model-old",
                    model_new,
                )
            },
        )

    def _registry(self) -> ApprovalRegistry:
        return ApprovalRegistry(
            clock=lambda: datetime(
                2026,
                7,
                27,
                8,
                0,
                tzinfo=timezone.utc,
            )
        )

    def _approve(self, registry: ApprovalRegistry, plan):
        handle = request_tui_approval(
            plan,
            registry,
            show_preview=lambda _preview: None,
            confirm=lambda: True,
        )
        self.assertIsNotNone(handle)
        return handle

    def _run(
        self,
        argv: list[str],
        **kwargs: object,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            **kwargs,  # type: ignore[arg-type]
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_apply_without_process_local_approval_fails_closed_without_io(
        self,
    ) -> None:
        with (
            mock.patch(
                "claude_hub.switchctl.build_default_service",
                side_effect=AssertionError("Store access is forbidden"),
            ),
            mock.patch.object(
                pathlib.Path,
                "home",
                side_effect=AssertionError("HOME access is forbidden"),
            ),
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network access is forbidden"),
            ),
            mock.patch.object(
                sqlite3,
                "connect",
                side_effect=AssertionError("Store access is forbidden"),
            ),
        ):
            exit_code, stdout, stderr = self._run(["apply"])

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout, CONFIRMATION_REQUIRED_GOLDEN)
        self.assertEqual(stderr, "switchctl: confirmation_required\n")

    def test_in_process_plan_uses_real_terminal_adapter_and_keeps_json_clean(
        self,
    ) -> None:
        private_name = "Private interactive provider label"
        plan = self._plan(display_name=private_name)
        stdout = io.StringIO()
        stderr = InteractiveStringIO()

        exit_code = main(
            ["apply"],
            stdin=InteractiveStringIO(
                f"{APPROVAL_CONFIRMATION_PHRASE}\n"
            ),
            stdout=stdout,
            stderr=stderr,
            apply_plan=plan,
        )

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout.getvalue(), APPLY_NOT_IMPLEMENTED_GOLDEN)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "apply_not_implemented",
        )
        terminal_text = stderr.getvalue()
        self.assertTrue(terminal_text.startswith(tui_preview(plan)))
        self.assertIn(APPROVAL_CONFIRMATION_PHRASE, terminal_text)
        self.assertTrue(
            terminal_text.endswith("switchctl: apply_not_implemented\n")
        )
        self.assertNotIn(private_name, stdout.getvalue() + terminal_text)
        self.assertNotIn('{"schemaVersion"', terminal_text)

    def test_half_injected_approval_never_silently_resigns(self) -> None:
        plan = self._plan()
        registry = self._registry()
        handle = self._approve(registry, plan)
        cases = (
            {
                "approval_registry": registry,
                "approval_handle": None,
            },
            {
                "approval_registry": None,
                "approval_handle": handle,
            },
        )

        with mock.patch(
            "claude_hub.switchctl.request_terminal_approval",
            side_effect=AssertionError("terminal adapter must not run"),
        ):
            for injected in cases:
                with self.subTest(
                    has_registry=injected["approval_registry"] is not None
                ):
                    result = self._run(
                        ["apply"],
                        apply_plan=plan,
                        **injected,
                    )
                    self.assertEqual(
                        result,
                        (
                            EXIT_RUNTIME_ERROR,
                            CONFIRMATION_REQUIRED_GOLDEN,
                            "switchctl: confirmation_required\n",
                        ),
                    )

        self.assertEqual(registry.active_count, 1)
        registry.consume(handle, plan)

    def test_interactive_rejection_shapes_require_new_confirmation(
        self,
    ) -> None:
        plan = self._plan()
        rejected_inputs = (
            "",
            "yes\n",
            "拒绝\n",
            f"{APPROVAL_CONFIRMATION_PHRASE.upper()}\n",
            f" {APPROVAL_CONFIRMATION_PHRASE}\n",
            f"{APPROVAL_CONFIRMATION_PHRASE} \n",
            f"{APPROVAL_CONFIRMATION_PHRASE}{'x' * 160}\n",
        )

        for response in rejected_inputs:
            with self.subTest(response_length=len(response)):
                stdout = io.StringIO()
                stderr = InteractiveStringIO()
                exit_code = main(
                    ["apply"],
                    stdin=InteractiveStringIO(response),
                    stdout=stdout,
                    stderr=stderr,
                    apply_plan=plan,
                )

                self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
                self.assertEqual(
                    stdout.getvalue(),
                    CONFIRMATION_REQUIRED_GOLDEN,
                )
                self.assertTrue(stderr.getvalue().startswith(tui_preview(plan)))
                self.assertTrue(
                    stderr.getvalue().endswith(
                        "switchctl: confirmation_required\n"
                    )
                )

        non_tty = self._run(
            ["apply"],
            stdin=io.StringIO(
                f"{APPROVAL_CONFIRMATION_PHRASE}\n"
            ),
            apply_plan=plan,
        )
        self.assertEqual(
            non_tty,
            (
                EXIT_RUNTIME_ERROR,
                CONFIRMATION_REQUIRED_GOLDEN,
                "switchctl: confirmation_required\n",
            ),
        )

    def test_clock_and_unknown_interactive_failures_are_runtime_errors(
        self,
    ) -> None:
        plan = self._plan()
        canary = "private-approval-infrastructure-canary"

        for patcher in (
            mock.patch(
                "claude_hub.switchctl.ApprovalRegistry",
                side_effect=RuntimeError(canary),
            ),
            mock.patch(
                "claude_hub.approval._utc_now",
                side_effect=RuntimeError(canary),
            ),
            mock.patch(
                "claude_hub.switchctl.request_terminal_approval",
                side_effect=RuntimeError(canary),
            ),
        ):
            with patcher:
                stdout = io.StringIO()
                stderr = InteractiveStringIO()
                exit_code = main(
                    ["apply"],
                    stdin=InteractiveStringIO(
                        f"{APPROVAL_CONFIRMATION_PHRASE}\n"
                    ),
                    stdout=stdout,
                    stderr=stderr,
                    apply_plan=plan,
                )

            self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
            self.assertEqual(stdout.getvalue(), APPLY_RUNTIME_ERROR_GOLDEN)
            self.assertTrue(
                stderr.getvalue().endswith("switchctl: runtime_error\n")
            )
            self.assertNotIn(canary, stdout.getvalue() + stderr.getvalue())

    @unittest.skipUnless(
        pty is not None and hasattr(os, "fork"),
        "requires a POSIX pseudo-terminal",
    )
    def test_real_pty_shows_full_preview_before_accepting_confirmation(
        self,
    ) -> None:
        plan = self._plan()
        master_descriptor, slave_descriptor = pty.openpty()
        json_read, json_write = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(master_descriptor)
            os.close(json_read)
            exit_code = 97
            try:
                terminal_input = os.fdopen(
                    os.dup(slave_descriptor),
                    "r",
                    encoding="utf-8",
                )
                terminal_output = os.fdopen(
                    os.dup(slave_descriptor),
                    "w",
                    encoding="utf-8",
                    buffering=1,
                )
                json_output = os.fdopen(
                    json_write,
                    "w",
                    encoding="utf-8",
                    buffering=1,
                )
                os.close(slave_descriptor)
                with terminal_input, terminal_output, json_output:
                    exit_code = main(
                        ["apply"],
                        stdin=terminal_input,
                        stdout=json_output,
                        stderr=terminal_output,
                        apply_plan=plan,
                    )
            except BaseException:
                exit_code = 98
            os._exit(exit_code)

        os.close(slave_descriptor)
        os.close(json_write)
        child_reaped = False
        terminal_bytes = bytearray()
        try:
            expected_preview = tui_preview(plan).replace(
                "\n",
                "\r\n",
            ).encode("utf-8")
            expected_prompt = (
                f'"{APPROVAL_CONFIRMATION_PHRASE}" '
                "and press Enter to approve once.\r\n> "
            ).encode("utf-8")
            deadline = time.monotonic() + 5
            while expected_prompt not in terminal_bytes:
                remaining = deadline - time.monotonic()
                self.assertGreater(
                    remaining,
                    0,
                    msg=terminal_bytes.decode("utf-8", errors="replace"),
                )
                readable, _, _ = select.select(
                    [master_descriptor],
                    [],
                    [],
                    remaining,
                )
                self.assertEqual(readable, [master_descriptor])
                terminal_bytes.extend(
                    os.read(master_descriptor, 4096)
                )

            self.assertIn(expected_preview, terminal_bytes)
            os.write(
                master_descriptor,
                f"{APPROVAL_CONFIRMATION_PHRASE}\n".encode("utf-8"),
            )

            deadline = time.monotonic() + 5
            status = 0
            terminal_closed = False
            while True:
                if not terminal_closed:
                    readable, _, _ = select.select(
                        [master_descriptor],
                        [],
                        [],
                        0.01,
                    )
                    if readable:
                        try:
                            chunk = os.read(master_descriptor, 4096)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            terminal_closed = True
                        else:
                            terminal_bytes.extend(chunk)
                waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    child_reaped = True
                    break
                self.assertLess(time.monotonic(), deadline)

            while not terminal_closed:
                readable, _, _ = select.select(
                    [master_descriptor],
                    [],
                    [],
                    0,
                )
                if not readable:
                    break
                try:
                    chunk = os.read(master_descriptor, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                terminal_bytes.extend(chunk)
            json_bytes = os.read(json_read, 8192)

            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), EXIT_RUNTIME_ERROR)
            self.assertEqual(
                json_bytes.decode("utf-8"),
                APPLY_NOT_IMPLEMENTED_GOLDEN,
            )
            terminal_text = terminal_bytes.decode(
                "utf-8",
                errors="strict",
            )
            normalized_terminal = terminal_text.replace("\r\n", "\n")
            self.assertIn(tui_preview(plan), normalized_terminal)
            self.assertIn(
                APPROVAL_CONFIRMATION_PHRASE,
                normalized_terminal,
            )
            self.assertIn(
                "switchctl: apply_not_implemented\n",
                normalized_terminal,
            )
            self.assertNotIn('{"schemaVersion"', normalized_terminal)
        finally:
            if not child_reaped:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            os.close(master_descriptor)
            os.close(json_read)

    def test_process_local_approval_is_consumed_without_store_write(
        self,
    ) -> None:
        private_name = "Private local provider label"
        plan = self._plan(display_name=private_name)
        registry = self._registry()
        handle = self._approve(registry, plan)

        with mock.patch(
            "claude_hub.switchctl.build_default_service",
            side_effect=AssertionError("Store access is forbidden"),
        ):
            first = self._run(
                ["apply"],
                approval_registry=registry,
                approval_handle=handle,
                apply_plan=plan,
            )
            replay = self._run(
                ["apply"],
                approval_registry=registry,
                approval_handle=handle,
                apply_plan=plan,
            )

        self.assertEqual(
            first,
            (
                EXIT_RUNTIME_ERROR,
                APPLY_NOT_IMPLEMENTED_GOLDEN,
                "switchctl: apply_not_implemented\n",
            ),
        )
        self.assertEqual(
            replay,
            (
                EXIT_RUNTIME_ERROR,
                CONFIRMATION_REQUIRED_GOLDEN,
                "switchctl: confirmation_required\n",
            ),
        )
        self.assertEqual(registry.active_count, 0)
        combined = first[1] + first[2] + replay[1] + replay[2]
        self.assertNotIn(private_name, combined)

    def test_mismatched_injected_plan_consumes_approval(self) -> None:
        approved_plan = self._plan()
        changed_plan = self._plan(model_new="model-changed-again")
        registry = self._registry()
        handle = self._approve(registry, approved_plan)

        mismatch = self._run(
            ["apply"],
            approval_registry=registry,
            approval_handle=handle,
            apply_plan=changed_plan,
        )
        retry = self._run(
            ["apply"],
            approval_registry=registry,
            approval_handle=handle,
            apply_plan=approved_plan,
        )

        expected = (
            EXIT_RUNTIME_ERROR,
            CONFIRMATION_REQUIRED_GOLDEN,
            "switchctl: confirmation_required\n",
        )
        self.assertEqual(mismatch, expected)
        self.assertEqual(retry, expected)
        self.assertEqual(registry.active_count, 0)

    def test_cli_arguments_cannot_forge_or_bypass_human_approval(self) -> None:
        token_canary = "opaque-capability-fixture-canary"
        bypass_attempts = (
            ["apply", "--yes"],
            ["--yes", "apply"],
            ["apply", "--token", token_canary],
            ["apply", "--approval", token_canary],
            ["apply", "--allow-always"],
            ["apply", token_canary],
        )
        for argv in bypass_attempts:
            with self.subTest(argv_shape=len(argv)):
                exit_code, stdout, stderr = self._run(argv)

                self.assertEqual(exit_code, EXIT_USAGE)
                expected_usage = (
                    "switchctl apply"
                    if argv[0] == "apply"
                    else "switchctl detect"
                )
                self.assertEqual(
                    json.loads(stdout)["error"],
                    {
                        "code": "usage_error",
                        "message": f"usage: {expected_usage}",
                    },
                )
                self.assertEqual(stderr, "switchctl: usage_error\n")
                self.assertNotIn(token_canary, stdout + stderr)
                self.assertNotIn("--yes", stdout + stderr)
                self.assertNotIn("--allow-always", stdout + stderr)

    def test_handle_repr_as_cli_text_cannot_authorize_apply(self) -> None:
        plan = self._plan()
        registry = self._registry()
        handle = self._approve(registry, plan)
        handle_text = repr(handle)

        forged = self._run(["apply", handle_text])

        self.assertEqual(forged[0], EXIT_USAGE)
        self.assertEqual(
            json.loads(forged[1])["error"],
            {
                "code": "usage_error",
                "message": "usage: switchctl apply",
            },
        )
        self.assertNotIn(handle_text, forged[1] + forged[2])
        self.assertEqual(registry.active_count, 1)

        valid = self._run(
            ["apply"],
            approval_registry=registry,
            approval_handle=handle,
            apply_plan=plan,
        )
        self.assertEqual(
            valid,
            (
                EXIT_RUNTIME_ERROR,
                APPLY_NOT_IMPLEMENTED_GOLDEN,
                "switchctl: apply_not_implemented\n",
            ),
        )


if __name__ == "__main__":
    unittest.main()
