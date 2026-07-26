from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import UUID


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub import standalone  # noqa: E402
from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProtocolAdapter,
    StandaloneProfile,
)
from claude_hub.standalone import (  # noqa: E402
    SCHEMA_VERSION,
    StandaloneProfileConflictError,
    StandaloneProfileExistsError,
    StandaloneProfileNotFoundError,
    StandaloneProfileStore,
    StandaloneStoreError,
    StandaloneStoreSecurityError,
    UnsupportedStandaloneSchemaError,
    standalone_data_dir,
    standalone_store_path,
)


PROFILE_ID = UUID("11111111-2222-4333-8444-555555555555")
OTHER_PROFILE_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
SECRET_REF_ID = UUID("66666666-7777-4888-8999-000000000000")
UPDATED_SECRET_REF_ID = UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")
CREATED_AT = datetime(2026, 7, 26, 8, 30, tzinfo=timezone.utc)


def profile_fixture(**overrides: object) -> StandaloneProfile:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "name": "  Example Profile  ",
        "base_url": " HTTPS://EXAMPLE.INVALID:443/v1/ ",
        "adapter": ProtocolAdapter.ANTHROPIC,
        "models": ModelMapping(
            default="model-default",
            fast="model-fast",
        ),
        "purpose_tags": (" primary ", "coding", "primary"),
        "secret_ref": str(SECRET_REF_ID).upper(),
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }
    values.update(overrides)
    return StandaloneProfile(**values)  # type: ignore[arg-type]


def write_document(path: pathlib.Path, document: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(mode)


class StandaloneDomainTests(unittest.TestCase):
    def test_profile_normalizes_metadata_and_redacts_routing_values(self) -> None:
        profile = profile_fixture()

        self.assertEqual(profile.profile_id, PROFILE_ID)
        self.assertEqual(profile.name, "Example Profile")
        self.assertEqual(profile.base_url, "https://example.invalid/v1")
        self.assertIs(profile.adapter, ProtocolAdapter.ANTHROPIC)
        self.assertIs(profile.protocol_adapter, ProtocolAdapter.ANTHROPIC)
        self.assertEqual(profile.purpose_tags, ("primary", "coding"))
        self.assertEqual(profile.secret_ref, SECRET_REF_ID)
        self.assertEqual(profile.created_at.tzinfo, timezone.utc)

        representation = repr(profile)
        self.assertNotIn(profile.name, representation)
        self.assertNotIn(profile.base_url, representation)
        self.assertNotIn(str(profile.secret_ref), representation)
        self.assertIn("routing_metadata=<redacted>", representation)

    def test_profile_rejects_invalid_values_without_echoing_them(self) -> None:
        canary = "fixture-" + "credential" + "-canary"
        invalid_cases = (
            (
                "base-url",
                {
                    "base_url": (
                        f"https://{canary}.invalid/path?query=forbidden"
                    ),
                },
            ),
            ("secret-ref-key", {"secret_ref": f"sk-{canary}"}),
            ("secret-ref-arbitrary", {"secret_ref": canary}),
            ("adapter", {"adapter": canary}),
        )

        for label, overrides in invalid_cases:
            with self.subTest(field=label):
                with self.assertRaises((TypeError, ValueError)) as captured:
                    profile_fixture(**overrides)
                self.assertNotIn(canary, str(captured.exception))
                self.assertNotIn(canary, repr(captured.exception))

        with self.assertRaisesRegex(
            ValueError,
            "^updated_at must not precede created_at$",
        ):
            profile_fixture(updated_at=CREATED_AT - timedelta(seconds=1))

    def test_plaintext_key_is_not_part_of_the_profile_or_store_api(self) -> None:
        canary = "fixture-" + "credential" + "-canary"
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "profiles.json"
            store = StandaloneProfileStore(
                path
            )
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()
            with self.assertRaises(TypeError) as captured:
                store.create(profile, api_key=canary)  # type: ignore[call-arg]

            tampered = dataclasses.replace(profile)
            object.__setattr__(tampered, "secret_ref", f"sk-{canary}")
            with self.assertRaises(TypeError) as store_error:
                store.update(tampered)

            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn(canary.encode(), path.read_bytes())
            self.assertNotIn(canary, repr(tampered))

        self.assertNotIn(canary, str(captured.exception))
        self.assertNotIn(canary, repr(captured.exception))
        self.assertNotIn(canary, str(store_error.exception))
        self.assertNotIn(canary, repr(store_error.exception))


class StandaloneDataDirectoryTests(unittest.TestCase):
    def test_platform_data_directories_are_pure_and_fully_injected(self) -> None:
        injected_home = pathlib.Path("/injected/home")
        poisoned_environment = {
            "HOME": "/must-not-be-read",
            "LOCALAPPDATA": "/must-not-be-read",
            "XDG_DATA_HOME": "/must-not-be-read",
        }
        with (
            mock.patch.object(
                pathlib.Path,
                "home",
                side_effect=AssertionError("real HOME access is forbidden"),
            ),
            mock.patch.dict(os.environ, poisoned_environment, clear=True),
        ):
            self.assertEqual(
                standalone_data_dir(
                    "darwin",
                    home=injected_home,
                    environment={},
                ),
                injected_home
                / "Library"
                / "Application Support"
                / "claude-hub",
            )
            self.assertEqual(
                standalone_data_dir(
                    "win32",
                    home=injected_home,
                    environment={"LOCALAPPDATA": "/injected/local-data"},
                ),
                pathlib.Path("/injected/local-data/claude-hub"),
            )
            self.assertEqual(
                standalone_data_dir(
                    "windows",
                    home=injected_home,
                    environment={},
                ),
                injected_home / "AppData" / "Local" / "claude-hub",
            )
            self.assertEqual(
                standalone_data_dir(
                    "win32",
                    home=injected_home,
                    environment={"LOCALAPPDATA": "relative-is-invalid"},
                ),
                injected_home / "AppData" / "Local" / "claude-hub",
            )
            self.assertEqual(
                standalone_data_dir(
                    "linux",
                    home=injected_home,
                    environment={"XDG_DATA_HOME": "/injected/xdg-data"},
                ),
                pathlib.Path("/injected/xdg-data/claude-hub"),
            )
            self.assertEqual(
                standalone_data_dir(
                    "linux",
                    home=injected_home,
                    environment={"XDG_DATA_HOME": "relative-is-invalid"},
                ),
                injected_home / ".local" / "share" / "claude-hub",
            )
            self.assertEqual(
                standalone_store_path(
                    "linux",
                    home=injected_home,
                    environment={},
                ),
                injected_home
                / ".local"
                / "share"
                / "claude-hub"
                / "standalone-profiles.json",
            )


class StandaloneProfileStoreTests(unittest.TestCase):
    def test_create_read_update_round_trip_is_atomic_private_and_secret_free(
        self,
    ) -> None:
        plaintext_canary = "fixture-" + "credential" + "-canary"
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "data" / "profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            real_replace = os.replace
            real_fsync = os.fsync

            with (
                mock.patch.object(
                    standalone.os,
                    "replace",
                    wraps=real_replace,
                ) as replace_call,
                mock.patch.object(
                    standalone.os,
                    "fsync",
                    wraps=real_fsync,
                ) as fsync_call,
            ):
                self.assertEqual(store.create(profile), profile)

            self.assertEqual(store.read(PROFILE_ID), profile)
            self.assertEqual(store.read(str(PROFILE_ID)), profile)
            self.assertGreaterEqual(fsync_call.call_count, 1)
            source, destination = replace_call.call_args.args
            self.assertEqual(pathlib.Path(source).parent, path.parent)
            self.assertEqual(pathlib.Path(destination), path)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(plaintext_canary.encode(), path.read_bytes())
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["profiles"][str(PROFILE_ID)]["secretRef"],
                str(SECRET_REF_ID),
            )

            updated = dataclasses.replace(
                profile,
                name="Updated Profile",
                adapter=ProtocolAdapter.OPENAI_CHAT,
                models=ModelMapping(
                    default="model-next",
                    reasoning="model-reasoning",
                ),
                purpose_tags=("reasoning",),
                secret_ref=UPDATED_SECRET_REF_ID,
                updated_at=CREATED_AT + timedelta(minutes=5),
            )
            self.assertEqual(store.update(updated), updated)
            self.assertEqual(store.read(PROFILE_ID), updated)
            self.assertEqual(store.read(PROFILE_ID).created_at, CREATED_AT)
            self.assertNotIn(plaintext_canary.encode(), path.read_bytes())

            with self.assertRaisesRegex(
                StandaloneProfileExistsError,
                "^standalone profile already exists$",
            ):
                store.create(updated)
            with self.assertRaisesRegex(
                StandaloneProfileNotFoundError,
                "^standalone profile was not found$",
            ):
                store.read(OTHER_PROFILE_ID)

    def test_update_preserves_unknown_root_profile_and_model_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            future_root = {
                "nested": ["preserve", {"revision": 2}],
            }
            write_document(
                path,
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "profiles": {},
                    "futureRoot": future_root,
                },
            )
            store.create(profile)

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["futureRoot"], future_root)
            raw_profile = document["profiles"][str(PROFILE_ID)]
            raw_profile["futureProfile"] = {
                "mode": "preserve",
            }
            raw_profile["models"]["future_role"] = "model-future"
            write_document(path, document)

            updated = dataclasses.replace(
                profile,
                models=ModelMapping(default="model-updated"),
                updated_at=CREATED_AT + timedelta(minutes=1),
            )
            store.update(updated)

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["futureRoot"],
                document["futureRoot"],
            )
            persisted_profile = persisted["profiles"][str(PROFILE_ID)]
            self.assertEqual(
                persisted_profile["futureProfile"],
                raw_profile["futureProfile"],
            )
            self.assertEqual(
                persisted_profile["models"]["future_role"],
                "model-future",
            )
            self.assertNotIn("fast", persisted_profile["models"])
            self.assertEqual(store.read(PROFILE_ID), updated)

    def test_higher_schema_fails_closed_without_changing_existing_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "profiles.json"
            write_document(
                path,
                {
                    "schemaVersion": SCHEMA_VERSION + 1,
                    "profiles": {},
                    "futureRoot": True,
                },
            )
            original = path.read_bytes()
            store = StandaloneProfileStore(path)

            operations = (
                lambda: store.read(PROFILE_ID),
                lambda: store.create(profile_fixture()),
                lambda: store.update(profile_fixture()),
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(
                        UnsupportedStandaloneSchemaError,
                        "^standalone store schema is unsupported$",
                    ):
                        operation()
                    self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "POSIX mode and symlink checks")
    def test_symlinks_non_regular_files_and_open_permissions_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            safe_path = directory / "safe.json"
            safe_store = StandaloneProfileStore(safe_path)
            safe_store.create(profile_fixture())

            symlink_path = directory / "linked.json"
            symlink_path.symlink_to(safe_path)
            with self.assertRaises(StandaloneStoreSecurityError):
                StandaloneProfileStore(symlink_path).read(PROFILE_ID)
            self.assertTrue(symlink_path.is_symlink())

            hardlink_path = directory / "hardlinked.json"
            os.link(safe_path, hardlink_path)
            with self.assertRaisesRegex(
                StandaloneStoreSecurityError,
                "^standalone store path is unsafe$",
            ):
                StandaloneProfileStore(hardlink_path).read(PROFILE_ID)
            hardlink_path.unlink()

            directory_path = directory / "not-a-file"
            directory_path.mkdir()
            with self.assertRaises(StandaloneStoreSecurityError):
                StandaloneProfileStore(directory_path).create(profile_fixture())

            original = safe_path.read_bytes()
            safe_path.chmod(0o644)
            with self.assertRaisesRegex(
                StandaloneStoreSecurityError,
                "^standalone store permissions are unsafe$",
            ):
                safe_store.read(PROFILE_ID)
            self.assertEqual(safe_path.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "POSIX parent symlink check")
    def test_parent_directory_symlink_is_rejected_before_create(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            target = directory / "real-data"
            target.mkdir()
            linked_parent = directory / "linked-data"
            linked_parent.symlink_to(target, target_is_directory=True)
            path = linked_parent / "profiles.json"

            with self.assertRaisesRegex(
                StandaloneStoreSecurityError,
                "^standalone store directory is unsafe$",
            ):
                StandaloneProfileStore(path).create(profile_fixture())
            self.assertFalse((target / "profiles.json").exists())

    def test_replace_and_fsync_failures_leave_old_bytes_and_no_temp_file(
        self,
    ) -> None:
        canary = "fixture-" + "credential" + "-canary"
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()
            updated = dataclasses.replace(
                profile,
                name="Updated Profile",
                updated_at=CREATED_AT + timedelta(minutes=1),
            )

            failure_points = (
                ("replace", standalone.os, "replace"),
                ("fsync", standalone.os, "fsync"),
            )
            for label, owner, attribute in failure_points:
                with self.subTest(failure=label):
                    with mock.patch.object(
                        owner,
                        attribute,
                        side_effect=OSError(canary),
                    ):
                        with self.assertRaises(StandaloneStoreError) as captured:
                            store.update(updated)
                    self.assertNotIn(canary, str(captured.exception))
                    self.assertNotIn(canary, repr(captured.exception))
                    self.assertEqual(path.read_bytes(), original)
                    leftovers = [
                        candidate
                        for candidate in directory.iterdir()
                        if candidate.name.startswith(f".{path.name}.")
                    ]
                    self.assertEqual(leftovers, [])

            new_path = directory / "new-profiles.json"
            with mock.patch.object(
                standalone.os,
                "replace",
                side_effect=OSError(canary),
            ):
                with self.assertRaises(StandaloneStoreError):
                    StandaloneProfileStore(new_path).create(profile)
            self.assertFalse(new_path.exists())
            self.assertFalse(
                any(
                    candidate.name.startswith(f".{new_path.name}.")
                    for candidate in directory.iterdir()
                )
            )

    def test_update_rejects_creation_time_changes_and_timestamp_regression(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()

            changed_creation = dataclasses.replace(
                profile,
                created_at=CREATED_AT - timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                StandaloneProfileConflictError,
                "^standalone profile creation time is immutable$",
            ):
                store.update(changed_creation)

            newer = dataclasses.replace(
                profile,
                updated_at=CREATED_AT + timedelta(minutes=2),
            )
            store.update(newer)
            newer_bytes = path.read_bytes()
            regressed = dataclasses.replace(
                profile,
                updated_at=CREATED_AT + timedelta(minutes=1),
            )
            with self.assertRaisesRegex(
                StandaloneProfileConflictError,
                "^standalone profile update time regressed$",
            ):
                store.update(regressed)

            self.assertNotEqual(newer_bytes, original)
            self.assertEqual(path.read_bytes(), newer_bytes)


if __name__ == "__main__":
    unittest.main()
