from __future__ import annotations

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

from claude_hub.ccswitch import (  # noqa: E402
    CC_SWITCH_DB_ENV,
    CCSwitchProviderStore,
    resolve_ccswitch_database_path,
)
from claude_hub.domain import ProviderRef, StoreCapability  # noqa: E402
from claude_hub.store import (  # noqa: E402
    ProviderConfigCorruptError,
    ProviderNotFoundError,
)


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


class CCSwitchDetectionTests(unittest.TestCase):
    def test_read_commands_leave_directory_snapshot_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            live_settings = directory / "live-settings.json"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "fixture-provider",
                        "Fixture Private Name",
                        '{"env":{"ANTHROPIC_MODEL":"fixture-model"}}',
                        "claude",
                        1,
                        1,
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_config VALUES (?, ?)",
                    ("claude", 0),
                )
                connection.commit()
            finally:
                connection.close()
            live_settings.write_bytes(
                b'{"fixture":"live-settings-unchanged"}\n'
            )

            def snapshot() -> dict[str, bytes]:
                return {
                    path.name: path.read_bytes()
                    for path in sorted(directory.iterdir())
                    if path.is_file()
                }

            before = snapshot()
            self.assertFalse(
                any(
                    name.endswith(("-wal", "-shm", "-journal"))
                    for name in before
                )
            )
            store = CCSwitchProviderStore(database)

            self.assertIs(store.detect(), StoreCapability.COMPATIBLE)
            self.assertEqual(snapshot(), before)

            references = store.list()
            self.assertEqual(snapshot(), before)

            inspection = store.inspect(references[0])
            self.assertEqual(snapshot(), before)
            self.assertTrue(inspection.is_current)

    def test_known_current_schema_with_required_structure_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database, user_version=16)

            capability = CCSwitchProviderStore(database).detect()

        self.assertIs(capability, StoreCapability.COMPATIBLE)

    def test_detection_fixtures_fail_closed_without_mutating_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            absent = directory / "absent.db"
            non_regular = directory / "not-a-database"
            non_regular.mkdir()
            corrupt = directory / "corrupt.db"
            corrupt.write_bytes(b"fixture-not-sqlite")
            unknown = directory / "unknown.db"
            write_schema(unknown, user_version=17)
            read_only = directory / "known-read-only.db"
            write_schema(read_only, user_version=13)
            missing_structure = directory / "missing-structure.db"
            connection = sqlite3.connect(missing_structure)
            try:
                connection.execute("CREATE TABLE providers (id TEXT)")
                connection.execute("PRAGMA user_version=16")
                connection.commit()
            finally:
                connection.close()

            fixtures = (
                (absent, StoreCapability.ABSENT),
                (non_regular, StoreCapability.INCOMPATIBLE),
                (corrupt, StoreCapability.CORRUPT),
                (unknown, StoreCapability.INCOMPATIBLE),
                (read_only, StoreCapability.READ_ONLY),
                (missing_structure, StoreCapability.INCOMPATIBLE),
            )
            before = {
                path: path.read_bytes()
                for path, _ in fixtures
                if path.is_file()
            }

            for path, expected in fixtures:
                with self.subTest(expected=expected):
                    actual = CCSwitchProviderStore(path).detect()
                    self.assertIs(actual, expected)
                    if actual is not StoreCapability.COMPATIBLE:
                        self.assertFalse(actual.schema_allows_write)

            self.assertEqual(
                {
                    path: path.read_bytes()
                    for path in before
                },
                before,
            )

    def test_candidate_resolution_prefers_explicit_environment_then_home(
        self,
    ) -> None:
        explicit = pathlib.Path("fixture-explicit.db")
        environment = pathlib.Path("fixture-environment.db")
        fixture_home = pathlib.Path("/fixture-home")

        with mock.patch.object(pathlib.Path, "home", return_value=fixture_home):
            self.assertEqual(
                resolve_ccswitch_database_path(
                    explicit,
                    environ={CC_SWITCH_DB_ENV: str(environment)},
                ),
                explicit,
            )
            self.assertEqual(
                resolve_ccswitch_database_path(
                    environ={CC_SWITCH_DB_ENV: str(environment)}
                ),
                environment,
            )
            self.assertEqual(
                resolve_ccswitch_database_path(environ={}),
                fixture_home / ".cc-switch" / "cc-switch.db",
            )

    def test_detect_reads_only_pragma_metadata_not_provider_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "fixture-id",
                        "fixture-private-name",
                        json.dumps({"env": {"FIXTURE_PRIVATE": "not-read"}}),
                        "claude",
                        1,
                        1,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            statements: list[str] = []

            def traced_connection(path: pathlib.Path) -> sqlite3.Connection:
                opened = sqlite3.connect(
                    path.resolve(strict=True).as_uri() + "?mode=ro",
                    uri=True,
                )
                opened.set_trace_callback(statements.append)
                return opened

            with mock.patch(
                "claude_hub.ccswitch._readonly_connection",
                side_effect=traced_connection,
            ):
                self.assertIs(
                    CCSwitchProviderStore(database).detect(),
                    StoreCapability.COMPATIBLE,
                )

        self.assertTrue(statements)
        self.assertTrue(
            all(
                statement.lstrip().upper().startswith("PRAGMA")
                for statement in statements
            )
        )


class CCSwitchListTests(unittest.TestCase):
    def test_list_returns_only_claude_references_and_current_markers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            "fixture-claude-current",
                            "Fixture Current Name",
                            "{}",
                            "claude",
                            20,
                            1,
                        ),
                        (
                            "fixture-claude-other",
                            "Fixture Other Name",
                            "{}",
                            "claude",
                            10,
                            0,
                        ),
                        (
                            "fixture-codex",
                            "Fixture Non-Claude Name",
                            "{}",
                            "codex",
                            0,
                            1,
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            references = CCSwitchProviderStore(database).list()

        self.assertEqual(
            tuple(reference.provider_id for reference in references),
            ("fixture-claude-other", "fixture-claude-current"),
        )
        self.assertEqual(
            tuple(reference.is_current for reference in references),
            (False, True),
        )
        self.assertTrue(
            all(reference.display_name is None for reference in references)
        )

    def test_names_require_explicit_local_interactive_mode_and_reads_are_immutable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database = directory / "fixture.db"
            settings = directory / "settings.json"
            write_schema(database)
            settings.write_bytes(b'{"fixture":"unchanged"}\n')
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "fixture-provider",
                        "Fixture Private Display Name",
                        "{}",
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            before = (database.read_bytes(), settings.read_bytes())

            private_by_default = CCSwitchProviderStore(database).list()
            local_only = CCSwitchProviderStore(
                database,
                local_interactive=True,
            ).list()

            self.assertIsNone(private_by_default[0].display_name)
            self.assertEqual(
                local_only[0].display_name,
                "Fixture Private Display Name",
            )
            self.assertEqual(
                (database.read_bytes(), settings.read_bytes()),
                before,
            )

    def test_default_list_query_does_not_read_display_name_column(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "fixture-provider",
                        "Fixture Private Display Name",
                        "{}",
                        "claude",
                        1,
                        0,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            statements: list[str] = []

            def traced_connection(path: pathlib.Path) -> sqlite3.Connection:
                opened = sqlite3.connect(
                    path.resolve(strict=True).as_uri() + "?mode=ro",
                    uri=True,
                )
                opened.set_trace_callback(statements.append)
                return opened

            with mock.patch(
                "claude_hub.ccswitch._readonly_connection",
                side_effect=traced_connection,
            ):
                references = CCSwitchProviderStore(database).list()

        self.assertIsNone(references[0].display_name)
        provider_selects = tuple(
            statement.upper()
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        )
        self.assertEqual(len(provider_selects), 1)
        self.assertNotIn("NAME", provider_selects[0].split("FROM", 1)[0])

    def test_empty_claude_fixture_returns_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    ("fixture-other", "Fixture Other", "{}", "codex", 1, 0),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(CCSwitchProviderStore(database).list(), ())


class CCSwitchInspectTests(unittest.TestCase):
    def test_ambiguous_non_finite_and_oversized_json_use_stable_error(
        self,
    ) -> None:
        fixtures = (
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
                (
                    '{"env":{},"fixture_private":"fixture-private-nan",'
                    '"score":NaN}'
                ),
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

        for label, raw_settings, private_values in fixtures:
            with self.subTest(fixture=label), tempfile.TemporaryDirectory() as raw_directory:
                database = pathlib.Path(raw_directory) / "fixture.db"
                write_schema(database)
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "fixture-provider",
                            "Fixture Private Name",
                            raw_settings,
                            "claude",
                            1,
                            0,
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(
                    ProviderConfigCorruptError,
                    "^provider configuration is invalid$",
                ) as caught:
                    CCSwitchProviderStore(database).inspect(
                        ProviderRef(
                            store="cc-switch",
                            provider_id="fixture-provider",
                        )
                    )

                for private_value in private_values:
                    self.assertNotIn(private_value, str(caught.exception))

    def test_inspect_returns_only_redacted_model_and_state_summary(self) -> None:
        settings_document = {
            "env": {
                "ANTHROPIC_MODEL": "fixture-default",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fixture-fast",
                "ANTHROPIC_REASONING_MODEL": "fixture-reasoning",
                "ANTHROPIC_AUTH_TOKEN": "fixture-private-value",
                "FIXTURE_EXTENSION": {"enabled": True},
            },
            "api_format": "fixture-format",
            "fixture_metadata": {"nested": ["kept", 7]},
        }
        raw_settings = json.dumps(settings_document, separators=(",", ":"))
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "fixture-provider",
                        "Fixture Private Name",
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
            before = database.read_bytes()

            inspection = CCSwitchProviderStore(database).inspect(
                ProviderRef(
                    store="cc-switch",
                    provider_id="fixture-provider",
                )
            )

            self.assertEqual(database.read_bytes(), before)

        self.assertEqual(
            inspection.models.to_public_dict(),
            {
                "default": "fixture-default",
                "fast": "fixture-fast",
                "reasoning": "fixture-reasoning",
            },
        )
        self.assertTrue(inspection.is_current)
        self.assertTrue(inspection.reference.current)
        self.assertTrue(inspection.proxy_takeover)
        self.assertIs(
            inspection.schema_capability,
            StoreCapability.COMPATIBLE,
        )
        self.assertEqual(
            inspection.fingerprint,
            "bfcd01c2c2dbac70bf019e82c7e4ddc90529a5d29b7780d85fc69695c51c7108",
        )
        self.assertEqual(inspection.unknown_field_count, 4)
        self.assertEqual(
            inspection.unknown_fingerprint,
            "ba7d88d491f982418e2f4d704c0360fdb193968ff2295261f2a7015130ab7353",
        )
        representation = repr(inspection)
        for private_value in (
            "Fixture Private Name",
            "fixture-private-value",
            "fixture-format",
        ):
            self.assertNotIn(private_value, representation)

    def test_corrupt_json_and_non_claude_ids_use_stable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            database = pathlib.Path(raw_directory) / "fixture.db"
            write_schema(database, user_version=13)
            connection = sqlite3.connect(database)
            try:
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        (
                            "fixture-corrupt",
                            "Fixture Private Name",
                            '{"env":',
                            "claude",
                            1,
                            0,
                        ),
                        (
                            "fixture-other-app",
                            "Fixture Other App",
                            "{}",
                            "codex",
                            2,
                            0,
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            store = CCSwitchProviderStore(database)

            with self.assertRaisesRegex(
                ProviderConfigCorruptError,
                "^provider configuration is invalid$",
            ):
                store.inspect(
                    ProviderRef(
                        store="cc-switch",
                        provider_id="fixture-corrupt",
                    )
                )
            with self.assertRaisesRegex(
                ProviderNotFoundError,
                "^provider reference was not found$",
            ):
                store.inspect(
                    ProviderRef(
                        store="cc-switch",
                        provider_id="fixture-other-app",
                    )
                )


if __name__ == "__main__":
    unittest.main()
