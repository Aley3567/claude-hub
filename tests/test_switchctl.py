from __future__ import annotations

import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.ccswitch import CC_SWITCH_DB_ENV  # noqa: E402
from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProviderInspection,
    ProviderRef,
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
    '"switchctl route [--store standalone]"]},"error":null}\n'
)


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


if __name__ == "__main__":
    unittest.main()
