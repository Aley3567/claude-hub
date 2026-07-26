from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import hashlib
import json
import os
import pathlib
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import UUID


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub import standalone, standalone_apply  # noqa: E402
from claude_hub.approval import (  # noqa: E402
    APPROVAL_TTL,
    ApprovalExpiredError,
    ApprovalRegistry,
    ApprovalUnavailableError,
)
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    STANDALONE_STORE_ID,
    PlanTarget,
    build_change_plan,
)
from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProtocolAdapter,
    ProviderRef,
    RuntimeMode,
    StandaloneProfile,
)
from claude_hub.standalone import (  # noqa: E402
    StandaloneProfileStore,
    StandaloneStoreError,
    StandaloneStoreSecurityError,
)
from claude_hub.standalone_apply import (  # noqa: E402
    StandaloneApplyConflictError,
    StandaloneApplyError,
    StandaloneApplyService,
    StandaloneApplyTargetError,
    StandaloneApplyWriteError,
    StandaloneCommitStateUnknownError,
)
from claude_hub.tui import request_tui_approval  # noqa: E402


PROFILE_ID = UUID("11111111-2222-4333-8444-555555555555")
SECRET_REF_ID = UUID("66666666-7777-4888-8999-000000000000")
CREATED_AT = datetime(2026, 7, 27, 8, 30, tzinfo=timezone.utc)
OTHER_PROFILE_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


class MutableClock:
    def __init__(self, value: datetime = CREATED_AT) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def profile_fixture(**overrides: object) -> StandaloneProfile:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "name": "Private fixture profile",
        "base_url": "https://private-fixture.invalid/v1",
        "adapter": ProtocolAdapter.ANTHROPIC,
        "models": ModelMapping(
            default="model-default-old",
            fast="model-fast-old",
        ),
        "purpose_tags": ("coding",),
        "secret_ref": SECRET_REF_ID,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }
    values.update(overrides)
    return StandaloneProfile(**values)  # type: ignore[arg-type]


def approve(registry: ApprovalRegistry, plan: object) -> object:
    return request_tui_approval(
        plan,  # type: ignore[arg-type]
        registry,
        show_preview=lambda _preview: None,
        confirm=lambda: True,
    )


class StandaloneApplyEndToEndTests(unittest.TestCase):
    def test_fake_inspect_plan_approve_apply_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            store.create(profile_fixture())
            registry = ApprovalRegistry()
            service = StandaloneApplyService(store, registry)

            inspection = service.inspect(PROFILE_ID)
            plan = service.create_plan(
                PROFILE_ID,
                changes={
                    "models.default": "model-default-new",
                    "models.reasoning": "model-reasoning-new",
                    "purpose_tags": ("review", "coding"),
                },
            )
            handle = approve(registry, plan)
            result = service.apply(plan, handle)

            self.assertEqual(
                inspection.store_fingerprint,
                plan.store_fingerprint,
            )
            self.assertNotEqual(
                result.new_fingerprint,
                plan.store_fingerprint,
            )
            self.assertEqual(
                result.new_fingerprint,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                tuple(change.field for change in result.redacted_diff),
                (
                    "models.default",
                    "models.reasoning",
                    "purpose_tags",
                ),
            )
            updated = store.read(PROFILE_ID)
            self.assertEqual(
                updated.models,
                ModelMapping(
                    default="model-default-new",
                    fast="model-fast-old",
                    reasoning="model-reasoning-new",
                ),
            )
            self.assertEqual(updated.purpose_tags, ("coding", "review"))
            self.assertEqual(updated.updated_at, CREATED_AT)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["profiles"][str(PROFILE_ID)]["secretRef"],
                str(SECRET_REF_ID),
            )


class StandaloneApplyAuthorizationTests(unittest.TestCase):
    def test_missing_and_expired_approval_are_one_shot_zero_write_failures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            store.create(profile_fixture())
            clock = MutableClock()
            registry = ApprovalRegistry(clock=clock)
            service = StandaloneApplyService(store, registry)
            plan = service.create_plan(
                PROFILE_ID,
                changes={"models.default": "model-default-new"},
            )
            original = path.read_bytes()

            with self.assertRaisesRegex(
                ApprovalUnavailableError,
                "^approval is unavailable$",
            ):
                service.apply(plan, None)
            self.assertEqual(path.read_bytes(), original)

            handle = approve(registry, plan)
            clock.value += APPROVAL_TTL
            with self.assertRaisesRegex(
                ApprovalExpiredError,
                "^approval has expired$",
            ):
                service.apply(plan, handle)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ApprovalUnavailableError):
                service.apply(plan, handle)
            self.assertEqual(path.read_bytes(), original)

    def test_wrong_targets_consume_approval_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            store.create(profile_fixture())
            registry = ApprovalRegistry(clock=MutableClock())
            service = StandaloneApplyService(store, registry)
            original = path.read_bytes()
            fingerprint = service.inspect(PROFILE_ID).store_fingerprint

            companion_plan = build_change_plan(
                mode=RuntimeMode.COMPANION,
                target=ProviderRef(
                    store=COMPANION_STORE_ID,
                    provider_id="provider-public-id",
                ),
                store_fingerprint=fingerprint,
                changes={
                    "models.default": (
                        "model-default-old",
                        "model-default-new",
                    )
                },
            )
            companion_handle = approve(registry, companion_plan)
            with self.assertRaisesRegex(
                StandaloneApplyTargetError,
                "^standalone apply target is invalid$",
            ):
                service.apply(companion_plan, companion_handle)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ApprovalUnavailableError):
                service.apply(companion_plan, companion_handle)

            missing_plan = build_change_plan(
                mode=RuntimeMode.STANDALONE,
                target=PlanTarget(
                    store=STANDALONE_STORE_ID,
                    provider_id=str(OTHER_PROFILE_ID),
                ),
                store_fingerprint=fingerprint,
                changes={
                    "models.default": (
                        "model-default-old",
                        "model-default-new",
                    )
                },
            )
            missing_handle = approve(registry, missing_plan)
            with self.assertRaisesRegex(
                StandaloneApplyTargetError,
                "^standalone apply target is invalid$",
            ):
                service.apply(missing_plan, missing_handle)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ApprovalUnavailableError):
                service.apply(missing_plan, missing_handle)

    def test_stale_whole_file_fingerprint_consumes_approval_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            registry = ApprovalRegistry(clock=MutableClock())
            service = StandaloneApplyService(store, registry)
            stale_plan = service.create_plan(
                PROFILE_ID,
                changes={"models.default": "model-default-new"},
            )
            handle = approve(registry, stale_plan)

            store.update(
                dataclasses.replace(
                    profile,
                    purpose_tags=("coding", "external-edit"),
                    updated_at=CREATED_AT + timedelta(seconds=1),
                )
            )
            externally_changed = path.read_bytes()

            with self.assertRaisesRegex(
                StandaloneApplyConflictError,
                "^standalone store changed after planning$",
            ):
                service.apply(stale_plan, handle)
            self.assertEqual(path.read_bytes(), externally_changed)
            with self.assertRaises(ApprovalUnavailableError):
                service.apply(stale_plan, handle)
            self.assertEqual(path.read_bytes(), externally_changed)

    def test_forged_old_value_is_consumed_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            store.create(profile_fixture())
            registry = ApprovalRegistry(clock=MutableClock())
            service = StandaloneApplyService(store, registry)
            original = path.read_bytes()
            fingerprint = service.inspect(PROFILE_ID).store_fingerprint
            forged = build_change_plan(
                mode=RuntimeMode.STANDALONE,
                target=PlanTarget(
                    store=STANDALONE_STORE_ID,
                    provider_id=str(PROFILE_ID),
                ),
                store_fingerprint=fingerprint,
                changes={
                    "models.default": (
                        "model-forged-old",
                        "model-default-new",
                    )
                },
            )
            handle = approve(registry, forged)

            with self.assertRaisesRegex(
                StandaloneApplyConflictError,
                "^standalone plan no longer matches target$",
            ):
                service.apply(forged, handle)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ApprovalUnavailableError):
                service.apply(forged, handle)


class StandaloneApplyPreservationTests(unittest.TestCase):
    def test_only_planned_raw_fields_change_and_private_values_never_return(
        self,
    ) -> None:
        private_url = "https://" + "private-preserve.invalid/opaque/v1"
        plaintext_key = "sk-" + "private-preserve-fixture-canary"
        private_name = "Private preservation fixture name"
        unknown_value = {
            "nested": [private_url, {"opaque": plaintext_key}],
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            target = profile_fixture(
                name=private_name,
                base_url=private_url,
            )
            other = profile_fixture(
                profile_id=OTHER_PROFILE_ID,
                name="Other private profile",
                base_url="https://other-private.invalid/v2",
                models=ModelMapping(default="other-default"),
            )
            store.create(target)
            store.create(other)

            document = json.loads(path.read_text(encoding="utf-8"))
            document["futureRoot"] = copy.deepcopy(unknown_value)
            raw_target = document["profiles"][str(PROFILE_ID)]
            raw_target["futureProfile"] = copy.deepcopy(unknown_value)
            raw_target["models"]["future_role"] = plaintext_key
            raw_target["createdAt"] = "2026-07-27T08:30:00+00:00"
            raw_target["updatedAt"] = "2026-07-27T08:30:00+00:00"
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            path.chmod(0o600)

            before = json.loads(path.read_text(encoding="utf-8"))
            before_target = copy.deepcopy(
                before["profiles"][str(PROFILE_ID)]
            )
            before_other = copy.deepcopy(
                before["profiles"][str(OTHER_PROFILE_ID)]
            )
            registry = ApprovalRegistry(clock=MutableClock())
            service = StandaloneApplyService(store, registry)
            inspection = service.inspect(PROFILE_ID)
            plan = service.create_plan(
                PROFILE_ID,
                changes={
                    "models.default": "model-default-new",
                    "models.fast": None,
                    "purposeTags": ("review", "coding"),
                },
            )
            result = service.apply(plan, approve(registry, plan))

            after = json.loads(path.read_text(encoding="utf-8"))
            after_target = after["profiles"][str(PROFILE_ID)]
            self.assertEqual(after["futureRoot"], before["futureRoot"])
            self.assertEqual(
                after["profiles"][str(OTHER_PROFILE_ID)],
                before_other,
            )
            for field in (
                "id",
                "name",
                "baseUrl",
                "adapter",
                "secretRef",
                "createdAt",
                "updatedAt",
                "futureProfile",
            ):
                with self.subTest(field=field):
                    self.assertEqual(
                        after_target[field],
                        before_target[field],
                    )
            self.assertEqual(
                after_target["models"]["future_role"],
                before_target["models"]["future_role"],
            )
            self.assertEqual(
                after_target["models"]["default"],
                "model-default-new",
            )
            self.assertNotIn("fast", after_target["models"])
            self.assertEqual(
                after_target["purposeTags"],
                ["coding", "review"],
            )
            self.assertEqual(
                result.new_fingerprint,
                service.inspect(PROFILE_ID).store_fingerprint,
            )

            public_surfaces = (
                repr(inspection),
                repr(service),
                repr(result),
                str(result),
            )
            for surface in public_surfaces:
                for canary in (
                    private_url,
                    plaintext_key,
                    private_name,
                    str(SECRET_REF_ID),
                ):
                    self.assertNotIn(canary, surface)

    def test_inspect_rejects_non_public_purpose_tags_without_disclosure(
        self,
    ) -> None:
        invalid_tags = (
            "https://" + "private-tag.invalid/v1",
            "/private/" + "tag/path",
            "C:\\" + "private\\tag\\path",
            "secret-" + "private-tag-canary",
        )
        for invalid_tag in invalid_tags:
            with self.subTest(tag_kind=invalid_tags.index(invalid_tag)):
                with tempfile.TemporaryDirectory() as raw_directory:
                    path = (
                        pathlib.Path(raw_directory)
                        / "standalone-profiles.json"
                    )
                    store = StandaloneProfileStore(path)
                    store.create(
                        profile_fixture(purpose_tags=(invalid_tag,))
                    )
                    service = StandaloneApplyService(
                        store,
                        ApprovalRegistry(clock=MutableClock()),
                    )

                    with self.assertRaisesRegex(
                        StandaloneApplyError,
                        "^standalone inspection contains non-public "
                        "purpose tags$",
                    ) as captured:
                        service.inspect(PROFILE_ID)

                    rendered = (
                        f"{captured.exception!s} "
                        f"{captured.exception!r}"
                    )
                    self.assertNotIn(invalid_tag, rendered)
                    self.assertEqual(
                        store.read(PROFILE_ID).purpose_tags,
                        (invalid_tag,),
                    )


class StandaloneApplyFaultTests(unittest.TestCase):
    def _prepared_apply(
        self,
        directory: pathlib.Path,
    ) -> tuple[
        pathlib.Path,
        ApprovalRegistry,
        StandaloneApplyService,
        object,
        object,
    ]:
        path = directory / "standalone-profiles.json"
        store = StandaloneProfileStore(path)
        store.create(profile_fixture())
        registry = ApprovalRegistry(clock=MutableClock())
        service = StandaloneApplyService(store, registry)
        plan = service.create_plan(
            PROFILE_ID,
            changes={"models.default": "model-default-new"},
        )
        return path, registry, service, plan, approve(registry, plan)

    def _assert_no_temporary_file(
        self,
        directory: pathlib.Path,
        path: pathlib.Path,
    ) -> None:
        self.assertFalse(
            any(
                candidate.name.startswith(f".{path.name}.")
                for candidate in directory.iterdir()
            )
        )

    def test_pre_replace_fsync_replace_and_interrupt_fail_without_write(
        self,
    ) -> None:
        failure_cases = (
            (
                "file-fsync",
                lambda: mock.patch.object(
                    standalone_apply.os,
                    "fsync",
                    side_effect=OSError("private-fsync-canary"),
                ),
                StandaloneApplyWriteError,
            ),
            (
                "replace-error",
                lambda: mock.patch.object(
                    standalone_apply.os,
                    "replace",
                    side_effect=OSError("private-replace-canary"),
                ),
                StandaloneApplyWriteError,
            ),
            (
                "replace-interrupt",
                lambda: mock.patch.object(
                    standalone_apply.os,
                    "replace",
                    side_effect=KeyboardInterrupt,
                ),
                KeyboardInterrupt,
            ),
        )
        for label, patch_factory, expected_error in failure_cases:
            with self.subTest(failure=label):
                with tempfile.TemporaryDirectory() as raw_directory:
                    directory = pathlib.Path(raw_directory)
                    path, registry, service, plan, handle = (
                        self._prepared_apply(directory)
                    )
                    original = path.read_bytes()
                    with patch_factory():
                        with self.assertRaises(expected_error):
                            service.apply(plan, handle)

                    self.assertEqual(path.read_bytes(), original)
                    self._assert_no_temporary_file(directory, path)
                    with self.assertRaises(ApprovalUnavailableError):
                        service.apply(plan, handle)
                    self.assertEqual(path.read_bytes(), original)

    def test_replace_completed_then_error_or_interrupt_is_commit_unknown(
        self,
    ) -> None:
        for failure in ("error", "interrupt"):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory() as raw_directory:
                    directory = pathlib.Path(raw_directory)
                    path, registry, service, plan, handle = (
                        self._prepared_apply(directory)
                    )
                    original = path.read_bytes()
                    real_replace = standalone_apply.os.replace

                    def replace_then_fail(
                        source: object,
                        destination: object,
                    ) -> None:
                        real_replace(source, destination)
                        if failure == "interrupt":
                            raise KeyboardInterrupt
                        raise OSError("private-post-replace-canary")

                    with mock.patch.object(
                        standalone_apply.os,
                        "replace",
                        side_effect=replace_then_fail,
                    ):
                        with self.assertRaisesRegex(
                            StandaloneCommitStateUnknownError,
                            "^standalone apply commit state is unknown$",
                        ) as captured:
                            service.apply(plan, handle)

                    self.assertEqual(
                        captured.exception.code,
                        "commit_state_unknown",
                    )
                    self.assertNotEqual(path.read_bytes(), original)
                    self.assertEqual(
                        StandaloneProfileStore(path)
                        .read(PROFILE_ID)
                        .models.default,
                        "model-default-new",
                    )
                    self._assert_no_temporary_file(directory, path)
                    with self.assertRaises(ApprovalUnavailableError):
                        service.apply(plan, handle)

    def test_post_replace_fsync_and_readback_failures_are_commit_unknown(
        self,
    ) -> None:
        failure_patches = (
            lambda: mock.patch.object(
                standalone_apply,
                "_strict_fsync_directory",
                side_effect=OSError("private-directory-fsync-canary"),
            ),
            lambda: mock.patch.object(
                standalone_apply,
                "_readback",
                side_effect=OSError("private-readback-canary"),
            ),
            lambda: mock.patch.object(
                standalone_apply,
                "_readback",
                return_value=b"{\"unexpected\":true}\n",
            ),
        )
        for patch_factory in failure_patches:
            with self.subTest(failure=repr(patch_factory)):
                with tempfile.TemporaryDirectory() as raw_directory:
                    directory = pathlib.Path(raw_directory)
                    path, registry, service, plan, handle = (
                        self._prepared_apply(directory)
                    )
                    original = path.read_bytes()

                    with patch_factory():
                        with self.assertRaisesRegex(
                            StandaloneCommitStateUnknownError,
                            "^standalone apply commit state is unknown$",
                        ):
                            service.apply(plan, handle)

                    self.assertNotEqual(path.read_bytes(), original)
                    self.assertEqual(
                        StandaloneProfileStore(path)
                        .read(PROFILE_ID)
                        .models.default,
                        "model-default-new",
                    )
                    self._assert_no_temporary_file(directory, path)
                    with self.assertRaises(ApprovalUnavailableError):
                        service.apply(plan, handle)


class StandaloneApplyConcurrencyTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name in {"posix", "nt"},
        "requires a supported cross-process lock backend",
    )
    def test_real_subprocesses_allow_only_one_old_fingerprint_apply(
        self,
    ) -> None:
        worker_source = """
import os
import pathlib
import sys
import time

sys.path.insert(0, sys.argv[5])

from claude_hub import standalone
from claude_hub.approval import ApprovalRegistry
from claude_hub.standalone import StandaloneProfileStore
from claude_hub.standalone_apply import (
    StandaloneApplyConflictError,
    StandaloneApplyService,
)
from claude_hub.tui import request_tui_approval

path = pathlib.Path(sys.argv[1])
model = sys.argv[2]
ready = pathlib.Path(sys.argv[3])
go = pathlib.Path(sys.argv[4])
worker_index = int(sys.argv[6])
lock_path = path.with_name(f"{path.name}.lock")
open_marker = path.parent / f"lock-open-{worker_index}"
other_open_marker = path.parent / f"lock-open-{1 - worker_index}"
real_open = standalone.os.open
open_synchronized = False

def synchronized_open(target, flags, mode=0o777):
    global open_synchronized
    if (
        not open_synchronized
        and pathlib.Path(target) == lock_path
        and flags & os.O_EXCL
    ):
        open_synchronized = True
        open_marker.write_text("ready", encoding="utf-8")
        open_deadline = time.monotonic() + 15
        while not other_open_marker.exists():
            if time.monotonic() >= open_deadline:
                raise RuntimeError("lock open coordination timeout")
            time.sleep(0.01)
    return real_open(target, flags, mode)

standalone.os.open = synchronized_open
registry = ApprovalRegistry()
service = StandaloneApplyService(StandaloneProfileStore(path), registry)
plan = service.create_plan(
    "11111111-2222-4333-8444-555555555555",
    changes={"models.default": model},
)
handle = request_tui_approval(
    plan,
    registry,
    show_preview=lambda _preview: None,
    confirm=lambda: True,
)
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not go.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("coordination timeout")
    time.sleep(0.01)
try:
    service.apply(plan, handle)
except StandaloneApplyConflictError:
    print("conflict")
else:
    print("success")
"""
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "standalone-profiles.json"
            StandaloneProfileStore(path).create(profile_fixture())
            path.with_name(f"{path.name}.lock").unlink()
            go = directory / "go"
            processes: list[subprocess.Popen[str]] = []
            ready_paths: list[pathlib.Path] = []
            for index, model in enumerate(("model-race-a", "model-race-b")):
                ready = directory / f"ready-{index}"
                ready_paths.append(ready)
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            worker_source,
                            str(path),
                            model,
                            str(ready),
                            str(go),
                            str(SOURCE_ROOT),
                            str(index),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )

            deadline = time.monotonic() + 15
            while not all(ready.exists() for ready in ready_paths):
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "subprocess planning timed out",
                )
                if any(process.poll() is not None for process in processes):
                    break
                time.sleep(0.01)
            self.assertTrue(all(ready.exists() for ready in ready_paths))
            go.write_text("go", encoding="utf-8")

            results: list[str] = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, "")
                results.append(stdout.strip())

            self.assertEqual(sorted(results), ["conflict", "success"])
            final_default = (
                StandaloneProfileStore(path).read(PROFILE_ID).models.default
            )
            self.assertIn(final_default, {"model-race-a", "model-race-b"})
            json.loads(path.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        os.name == "posix" and pathlib.Path("/dev/fd").is_dir(),
        "requires observable POSIX descriptors",
    )
    def test_unsafe_lock_validation_does_not_leak_file_descriptors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()
            lock_path = path.with_name(f"{path.name}.lock")
            lock_path.chmod(0o644)
            before = len(tuple(pathlib.Path("/dev/fd").iterdir()))

            for _attempt in range(32):
                with self.assertRaisesRegex(
                    StandaloneStoreSecurityError,
                    "^standalone store permissions are unsafe$",
                ):
                    store.update(profile)

            after = len(tuple(pathlib.Path("/dev/fd").iterdir()))
            self.assertLessEqual(after, before + 1)
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and pathlib.Path("/dev/fd").is_dir(),
        "requires POSIX symlinks and observable descriptors",
    )
    def test_lock_symlink_is_rejected_without_o_nofollow(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original_store = path.read_bytes()
            lock_path = path.with_name(f"{path.name}.lock")
            lock_path.unlink()
            victim = directory / "victim"
            victim.write_bytes(b"")
            victim.chmod(0o600)
            lock_path.symlink_to(victim)
            before_descriptors = len(
                tuple(pathlib.Path("/dev/fd").iterdir())
            )
            nofollow_flag = os.O_NOFOLLOW

            delattr(standalone.os, "O_NOFOLLOW")
            try:
                for _attempt in range(16):
                    with self.assertRaisesRegex(
                        StandaloneStoreSecurityError,
                        "^standalone store path is unsafe$",
                    ):
                        store.update(
                            dataclasses.replace(
                                profile,
                                updated_at=CREATED_AT
                                + timedelta(seconds=1),
                            )
                        )
            finally:
                setattr(standalone.os, "O_NOFOLLOW", nofollow_flag)

            after_descriptors = len(
                tuple(pathlib.Path("/dev/fd").iterdir())
            )
            self.assertLessEqual(
                after_descriptors,
                before_descriptors + 1,
            )
            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(victim.read_bytes(), b"")
            self.assertEqual(path.read_bytes(), original_store)

    @unittest.skipUnless(
        os.name == "posix" and pathlib.Path("/dev/fd").is_dir(),
        "requires POSIX symlinks and observable descriptors",
    )
    def test_lock_creation_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            path = directory / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original_store = path.read_bytes()
            lock_path = path.with_name(f"{path.name}.lock")
            lock_path.unlink()
            victim = directory / "victim"
            victim.write_bytes(b"")
            victim.chmod(0o600)
            before_descriptors = len(
                tuple(pathlib.Path("/dev/fd").iterdir())
            )
            real_open = standalone.os.open
            raced = False

            def racing_open(
                target: object,
                flags: int,
                mode: int = 0o777,
            ) -> int:
                nonlocal raced
                if (
                    not raced
                    and pathlib.Path(target) == lock_path
                    and flags & os.O_EXCL
                ):
                    raced = True
                    lock_path.symlink_to(victim)
                return real_open(target, flags, mode)

            with mock.patch.object(
                standalone.os,
                "open",
                side_effect=racing_open,
            ):
                with self.assertRaisesRegex(
                    StandaloneStoreSecurityError,
                    "^standalone store path is unsafe$",
                ):
                    store.update(
                        dataclasses.replace(
                            profile,
                            updated_at=CREATED_AT
                            + timedelta(seconds=1),
                        )
                    )

            after_descriptors = len(
                tuple(pathlib.Path("/dev/fd").iterdir())
            )
            self.assertTrue(raced)
            self.assertLessEqual(
                after_descriptors,
                before_descriptors + 1,
            )
            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(victim.read_bytes(), b"")
            self.assertEqual(path.read_bytes(), original_store)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_overlapping_writer_returns_and_child_fails_closed(
        self,
    ) -> None:
        worker_source = """
import dataclasses
import os
import pathlib
import signal
import sys
import threading
import time
from datetime import timedelta

sys.path.insert(0, sys.argv[2])

from claude_hub import standalone
from claude_hub.standalone import (
    StandaloneProfileStore,
    StandaloneStoreError,
)

path = pathlib.Path(sys.argv[1])
store = StandaloneProfileStore(path)
profile = store.read("11111111-2222-4333-8444-555555555555")
entered = threading.Event()
release = threading.Event()

def hold_store_lock():
    with standalone._store_lock(path):
        entered.set()
        release.wait()

holder = threading.Thread(target=hold_store_lock)
holder.start()
if not entered.wait(timeout=2):
    raise RuntimeError("holder did not enter")

child_pid = -1
try:
    child_pid = os.fork()
    if child_pid == 0:
        def rejected():
            try:
                store.update(
                    dataclasses.replace(
                        profile,
                        purpose_tags=("must-not-write",),
                        updated_at=profile.updated_at + timedelta(seconds=1),
                    )
                )
            except StandaloneStoreError as error:
                return str(error) == (
                    "standalone store is unavailable after overlapping fork"
                )
            except BaseException:
                return False
            return False

        if not rejected():
            os._exit(3)
        grandchild_pid = os.fork()
        if grandchild_pid == 0:
            os._exit(0 if rejected() else 4)
        grandchild_deadline = time.monotonic() + 2
        while time.monotonic() < grandchild_deadline:
            waited_pid, grandchild_status = os.waitpid(
                grandchild_pid,
                os.WNOHANG,
            )
            if waited_pid == grandchild_pid:
                if (
                    os.WIFEXITED(grandchild_status)
                    and os.WEXITSTATUS(grandchild_status) == 0
                ):
                    os._exit(0)
                os._exit(5)
            time.sleep(0.01)
        os.kill(grandchild_pid, signal.SIGKILL)
        os.waitpid(grandchild_pid, 0)
        os._exit(6)

    release.set()
    holder.join(timeout=2)
    if holder.is_alive():
        raise RuntimeError("holder did not release")

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == child_pid:
            child_pid = -1
            if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
                print("overlap_rejected")
                raise SystemExit(0)
            raise RuntimeError("child returned wrong status")
        time.sleep(0.01)
    raise RuntimeError("child did not fail closed")
finally:
    release.set()
    holder.join(timeout=2)
    if child_pid > 0:
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
"""
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    worker_source,
                    str(path),
                    str(SOURCE_ROOT),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=2)
                self.fail(
                    "fork probe timed out before returning from os.fork"
                )

            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "overlap_rejected")
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_child_with_inherited_active_file_lock_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            original = path.read_bytes()
            child_pid = -1

            try:
                with standalone._store_lock(path):
                    child_pid = os.fork()
                    if child_pid == 0:
                        try:
                            store.update(
                                dataclasses.replace(
                                    profile,
                                    purpose_tags=("fork-active-fd",),
                                    updated_at=CREATED_AT
                                    + timedelta(seconds=1),
                                )
                            )
                        except StandaloneStoreError as error:
                            if str(error) == (
                                "standalone store is unavailable after "
                                "overlapping fork"
                            ):
                                os._exit(0)
                            os._exit(3)
                        except BaseException:
                            os._exit(2)
                        os._exit(4)

                deadline = time.monotonic() + 2
                status: int | None = None
                while time.monotonic() < deadline:
                    waited_pid, candidate_status = os.waitpid(
                        child_pid,
                        os.WNOHANG,
                    )
                    if waited_pid == child_pid:
                        status = candidate_status
                        child_pid = -1
                        break
                    time.sleep(0.01)

                self.assertIsNotNone(
                    status,
                    "fork child did not fail closed on an inherited file lock",
                )
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)
                self.assertEqual(path.read_bytes(), original)
            finally:
                if child_pid > 0:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_fork_without_active_writer_allows_child_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            profile = profile_fixture()
            store.create(profile)
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    store.update(
                        dataclasses.replace(
                            profile,
                            purpose_tags=("fork-safe",),
                            updated_at=CREATED_AT + timedelta(seconds=1),
                        )
                    )
                except BaseException:
                    os._exit(2)
                os._exit(0)

            try:
                deadline = time.monotonic() + 2
                status: int | None = None
                while time.monotonic() < deadline:
                    waited_pid, candidate_status = os.waitpid(
                        child_pid,
                        os.WNOHANG,
                    )
                    if waited_pid == child_pid:
                        status = candidate_status
                        child_pid = -1
                        break
                    time.sleep(0.01)

                self.assertIsNotNone(
                    status,
                    "safe fork child did not finish Store update",
                )
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)
                self.assertEqual(
                    store.read(PROFILE_ID).purpose_tags,
                    ("fork-safe",),
                )
            finally:
                if child_pid > 0:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)


class StandaloneApplyIsolationTests(unittest.TestCase):
    def test_flow_does_not_read_home_network_credentials_or_cc_switch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "standalone-profiles.json"
            store = StandaloneProfileStore(path)
            store.create(profile_fixture())
            registry = ApprovalRegistry(clock=MutableClock())
            service = StandaloneApplyService(store, registry)
            poisoned_environment = {
                "HOME": "/must-not-be-read",
                "CC_SWITCH_DB": "/must-not-be-read",
                "API_KEY": "must-not-be-read",  # secret-guard: allow
            }

            with (
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
                    side_effect=AssertionError("CC Switch access is forbidden"),
                ),
                mock.patch.object(
                    subprocess,
                    "Popen",
                    side_effect=AssertionError("process access is forbidden"),
                ),
                mock.patch.object(
                    builtins,
                    "open",
                    side_effect=AssertionError("credential access is forbidden"),
                ),
                mock.patch.dict(
                    os.environ,
                    poisoned_environment,
                    clear=True,
                ),
            ):
                inspection = service.inspect(PROFILE_ID)
                plan = service.create_plan(
                    PROFILE_ID,
                    changes={"models.default": "model-isolated-new"},
                )
                result = service.apply(plan, approve(registry, plan))

            self.assertNotEqual(
                inspection.store_fingerprint,
                result.new_fingerprint,
            )
            self.assertEqual(
                store.read(PROFILE_ID).models.default,
                "model-isolated-new",
            )

    def test_module_dependencies_exclude_external_and_credential_stores(
        self,
    ) -> None:
        source_path = pathlib.Path(standalone_apply.__file__)
        parsed = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.split(".", maxsplit=1)[0]
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".", maxsplit=1)[0])

        self.assertTrue(
            {
                "ccswitch",
                "keyring",
                "requests",
                "socket",
                "sqlite3",
                "subprocess",
            }.isdisjoint(imported_modules)
        )
        self.assertNotIn("environ", source_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
