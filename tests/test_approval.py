from __future__ import annotations

import ast
import base64
import builtins
import dataclasses
import io
import json
import os
import pathlib
import pickle
import socket
import sqlite3
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub import approval  # noqa: E402
from claude_hub.approval import (  # noqa: E402
    APPROVAL_SCHEMA_VERSION,
    APPROVAL_TTL,
    ApprovalBindingError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalHandle,
    ApprovalRecord,
    ApprovalRegistry,
    ApprovalUnavailableError,
    HumanConfirmationError,
    InvalidApprovalRecordError,
    UnsafeApprovalClockError,
    approval_record_preview,
)
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    STANDALONE_STORE_ID,
    ChangePlan,
    FieldChange,
    PlanTarget,
    build_change_plan,
    canonical_change_plan_json,
    tui_preview,
)
from claude_hub.domain import ProviderRef, RuntimeMode  # noqa: E402
from claude_hub.tui import (  # noqa: E402
    APPROVAL_CONFIRMATION_PHRASE,
    request_terminal_approval,
    request_tui_approval,
)


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
FINGERPRINT = "a1" * 32
OTHER_FINGERPRINT = "b2" * 32
STANDALONE_PROFILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class MutableClock:
    def __init__(self, value: object = NOW) -> None:
        self.value = value

    def __call__(self) -> object:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class InteractiveStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def plan_fixture(
    *,
    target_id: str = "provider-public-id",
    fingerprint: str = FINGERPRINT,
    model_new: str = "model-new",
):
    return build_change_plan(
        mode=RuntimeMode.COMPANION,
        target=ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id=target_id,
        ),
        store_fingerprint=fingerprint,
        changes={
            "models.default": (
                "model-old",
                model_new,
            )
        },
    )


def approve(
    registry: ApprovalRegistry,
    plan,
) -> ApprovalHandle:
    handle = request_tui_approval(
        plan,
        registry,
        show_preview=lambda _preview: None,
        confirm=lambda: True,
    )
    if not isinstance(handle, ApprovalHandle):
        raise AssertionError("fixture approval was not issued")
    return handle


class ApprovalPresentationTests(unittest.TestCase):
    def test_tui_displays_full_plan_before_explicit_confirmation(self) -> None:
        clock = MutableClock()
        registry = ApprovalRegistry(clock=clock)
        plan = plan_fixture()
        events: list[tuple[str, object]] = []

        def show_preview(preview: str) -> None:
            events.append(("preview", preview))

        def confirm() -> bool:
            events.append(("confirm", True))
            return True

        handle = request_tui_approval(
            plan,
            registry,
            show_preview=show_preview,
            confirm=confirm,
        )

        self.assertIsInstance(handle, ApprovalHandle)
        self.assertEqual(
            events,
            [
                ("preview", tui_preview(plan)),
                ("confirm", True),
            ],
        )
        self.assertEqual(registry.active_count, 1)
        record = registry.consume(handle, plan)
        self.assertEqual(record.plan_digest, plan.digest)
        self.assertIs(record.mode, plan.mode)
        self.assertEqual(record.target, plan.target)
        self.assertEqual(
            record.store_fingerprint,
            plan.store_fingerprint,
        )
        self.assertEqual(record.approved_at, NOW)
        self.assertEqual(record.expires_at, NOW + timedelta(minutes=15))
        self.assertEqual(record.expires_at - record.approved_at, APPROVAL_TTL)
        self.assertEqual(record.schema_version, APPROVAL_SCHEMA_VERSION)

    def test_reject_cancel_and_invalid_decision_issue_no_handle(self) -> None:
        plan = plan_fixture()
        for decision in (False, None):
            with self.subTest(decision=decision):
                registry = ApprovalRegistry(clock=MutableClock())
                displayed: list[str] = []

                handle = request_tui_approval(
                    plan,
                    registry,
                    show_preview=displayed.append,
                    confirm=lambda decision=decision: decision,
                )

                self.assertIsNone(handle)
                self.assertEqual(displayed, [tui_preview(plan)])
                self.assertEqual(registry.active_count, 0)

        invalid_registry = ApprovalRegistry(clock=MutableClock())
        with self.assertRaisesRegex(
            HumanConfirmationError,
            "^human confirmation returned an invalid decision$",
        ):
            request_tui_approval(
                plan,
                invalid_registry,
                show_preview=lambda _preview: None,
                confirm=lambda: "yes",  # type: ignore[return-value]
            )
        self.assertEqual(invalid_registry.active_count, 0)

    def test_real_terminal_adapter_requires_exact_phrase_after_preview(
        self,
    ) -> None:
        plan = plan_fixture()
        registry = ApprovalRegistry(clock=MutableClock())
        terminal_output = InteractiveStringIO()
        handle = request_terminal_approval(
            plan,
            registry,
            input_stream=InteractiveStringIO(
                f"{APPROVAL_CONFIRMATION_PHRASE}\n"
            ),
            output_stream=terminal_output,
        )

        self.assertIsInstance(handle, ApprovalHandle)
        rendered = terminal_output.getvalue()
        self.assertTrue(rendered.startswith(tui_preview(plan)))
        self.assertIn(APPROVAL_CONFIRMATION_PHRASE, rendered)
        self.assertTrue(rendered.endswith("\n> "))
        registry.consume(handle, plan)

        cancelled_registry = ApprovalRegistry(clock=MutableClock())
        cancelled = request_terminal_approval(
            plan,
            cancelled_registry,
            input_stream=InteractiveStringIO("yes\n"),
            output_stream=InteractiveStringIO(),
        )
        self.assertIsNone(cancelled)
        self.assertEqual(cancelled_registry.active_count, 0)

    def test_terminal_adapter_fails_closed_without_a_real_tty(self) -> None:
        registry = ApprovalRegistry(clock=MutableClock())
        with self.assertRaisesRegex(
            HumanConfirmationError,
            "^interactive terminal is unavailable$",
        ):
            request_terminal_approval(
                plan_fixture(),
                registry,
                input_stream=io.StringIO(
                    f"{APPROVAL_CONFIRMATION_PHRASE}\n"
                ),
                output_stream=io.StringIO(),
            )
        self.assertEqual(registry.active_count, 0)

    def test_preview_and_confirmation_callback_failures_are_sanitized(
        self,
    ) -> None:
        canary = "private-callback-fixture-canary"
        plan = plan_fixture()

        preview_registry = ApprovalRegistry(clock=MutableClock())
        with self.assertRaisesRegex(
            HumanConfirmationError,
            "^change plan preview failed$",
        ) as preview_error:
            request_tui_approval(
                plan,
                preview_registry,
                show_preview=lambda _preview: (_ for _ in ()).throw(
                    RuntimeError(canary)
                ),
                confirm=lambda: True,
            )
        self.assertEqual(preview_registry.active_count, 0)

        confirm_registry = ApprovalRegistry(clock=MutableClock())
        displayed: list[str] = []
        with self.assertRaisesRegex(
            HumanConfirmationError,
            "^human confirmation failed$",
        ) as confirm_error:
            request_tui_approval(
                plan,
                confirm_registry,
                show_preview=displayed.append,
                confirm=lambda: (_ for _ in ()).throw(
                    RuntimeError(canary)
                ),
            )
        self.assertEqual(displayed, [tui_preview(plan)])
        self.assertEqual(confirm_registry.active_count, 0)

        for captured in (preview_error, confirm_error):
            self.assertNotIn(canary, str(captured.exception))
            self.assertNotIn(canary, repr(captured.exception))

    def test_record_handle_repr_and_preview_exclude_every_canary(self) -> None:
        private_name = "Private customer provider label"
        plaintext_key = "sk-live-fixture-approval-canary-123456"
        full_url = "https://private-approval-fixture.invalid/private/v1"
        raw_plan = '{"privateRawPlan":"raw-plan-fixture-canary"}'
        token_bytes = (
            b"capability-token-fixture-canary-" + b"x" * 32
        )[:32]
        plan = build_change_plan(
            mode=RuntimeMode.COMPANION,
            target=ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
                is_current=True,
                display_name=private_name,
            ),
            store_fingerprint=FINGERPRINT,
            changes={
                "models.default": (
                    "model-old",
                    "model-new",
                )
            },
        )
        registry = ApprovalRegistry(clock=MutableClock())
        with mock.patch.object(
            approval.secrets,
            "token_bytes",
            return_value=token_bytes,
        ) as token_factory:
            handle = approve(registry, plan)
        token_factory.assert_called_once_with(32)
        record = registry.consume(handle, plan)

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(record)),
            (
                "plan_digest",
                "mode",
                "target",
                "store_fingerprint",
                "approved_at",
                "expires_at",
                "schema_version",
            ),
        )
        surfaces = (
            repr(handle),
            str(handle),
            repr(record),
            approval_record_preview(record),
            repr(registry),
        )
        encoded_token_forms = (
            token_bytes.decode("ascii"),
            token_bytes.hex(),
            base64.urlsafe_b64encode(token_bytes).decode("ascii"),
        )
        for surface in surfaces:
            for canary in (
                private_name,
                plaintext_key,
                full_url,
                raw_plan,
                canonical_change_plan_json(plan),
                *encoded_token_forms,
            ):
                self.assertNotIn(canary, surface)
        self.assertEqual(repr(handle), "ApprovalHandle(<opaque>)")
        self.assertNotIn("token", repr(handle).casefold())
        self.assertNotIn("handle", approval_record_preview(record).casefold())

    def test_handle_cannot_be_publicly_constructed_or_serialized(self) -> None:
        token_canary = b"x" * 32
        with self.assertRaisesRegex(
            TypeError,
            "^approval handles are registry-issued$",
        ):
            ApprovalHandle(
                token=token_canary,
                registry_marker=object(),
                construction_guard=object(),
            )

        registry = ApprovalRegistry(clock=MutableClock())
        with mock.patch.object(
            approval.secrets,
            "token_bytes",
            return_value=token_canary,
        ):
            handle = approve(registry, plan_fixture())
        with self.assertRaisesRegex(
            TypeError,
            "^approval handles are process-local$",
        ):
            handle.__reduce__()
        with self.assertRaisesRegex(
            AttributeError,
            "^approval handles are immutable$",
        ):
            handle.fixture = "replacement"  # type: ignore[attr-defined]

        serialization_errors: list[TypeError] = []
        for serializer in (pickle.dumps, json.dumps):
            with self.subTest(serializer=serializer.__module__):
                with self.assertRaises(TypeError) as captured:
                    serializer(handle)
                serialization_errors.append(captured.exception)
        for error in serialization_errors:
            rendered = f"{error!s} {error!r}"
            for token_form in (
                token_canary.decode("ascii"),
                token_canary.hex(),
                base64.urlsafe_b64encode(token_canary).decode("ascii"),
            ):
                self.assertNotIn(token_form, rendered)

    def test_change_plan_subclasses_cannot_override_the_reviewed_digest(
        self,
    ) -> None:
        reviewed_plan = plan_fixture()

        class ForgedDigestPlan(ChangePlan):
            @property
            def digest(self) -> str:
                return reviewed_plan.digest

        forged_plan = ForgedDigestPlan(
            mode=reviewed_plan.mode,
            target=reviewed_plan.target,
            store_fingerprint=reviewed_plan.store_fingerprint,
            changes=(
                FieldChange(
                    field="models.default",
                    old="model-old",
                    new="model-forged",
                ),
            ),
        )
        registry = ApprovalRegistry(clock=MutableClock())
        preview_calls: list[str] = []
        confirmation_calls: list[bool] = []

        with self.assertRaisesRegex(TypeError, "^plan must be a ChangePlan$"):
            request_tui_approval(
                forged_plan,
                registry,
                show_preview=preview_calls.append,
                confirm=lambda: confirmation_calls.append(True) or True,
            )

        self.assertEqual(preview_calls, [])
        self.assertEqual(confirmation_calls, [])
        self.assertEqual(registry.active_count, 0)

        handle = approve(registry, reviewed_plan)
        with self.assertRaisesRegex(
            ApprovalBindingError,
            "^approval binding does not match plan$",
        ):
            registry.consume(handle, forged_plan)
        self.assertEqual(registry.active_count, 0)


class ApprovalConsumptionTests(unittest.TestCase):
    def test_success_is_one_shot_and_text_cannot_recreate_handle(self) -> None:
        registry = ApprovalRegistry(clock=MutableClock())
        plan = plan_fixture()
        handle = approve(registry, plan)

        record = registry.consume(handle, plan)

        self.assertEqual(record.plan_digest, plan.digest)
        self.assertEqual(registry.active_count, 0)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            registry.consume(handle, plan)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            registry.consume(repr(handle), plan)

    def test_binding_mismatch_consumes_target_field_and_fingerprint_cases(
        self,
    ) -> None:
        original = plan_fixture()
        mismatches = (
            plan_fixture(target_id="provider-other-id"),
            plan_fixture(model_new="model-changed-again"),
            plan_fixture(fingerprint=OTHER_FINGERPRINT),
            build_change_plan(
                mode=RuntimeMode.STANDALONE,
                target=PlanTarget(
                    store=STANDALONE_STORE_ID,
                    provider_id=STANDALONE_PROFILE_ID,
                ),
                store_fingerprint=FINGERPRINT,
                changes={
                    "models.default": (
                        "model-old",
                        "model-new",
                    )
                },
            ),
        )

        for mismatch in mismatches:
            with self.subTest(mode=mismatch.mode.value):
                registry = ApprovalRegistry(clock=MutableClock())
                handle = approve(registry, original)

                with self.assertRaisesRegex(
                    ApprovalBindingError,
                    "^approval binding does not match plan$",
                ):
                    registry.consume(handle, mismatch)
                self.assertEqual(registry.active_count, 0)
                with self.assertRaisesRegex(
                    ApprovalUnavailableError,
                    "^approval is unavailable$",
                ):
                    registry.consume(handle, original)

    def test_exact_expiration_boundary_is_rejected_and_consumed(self) -> None:
        plan = plan_fixture()

        valid_clock = MutableClock()
        valid_registry = ApprovalRegistry(clock=valid_clock)
        valid_handle = approve(valid_registry, plan)
        valid_clock.value = NOW + APPROVAL_TTL - timedelta(microseconds=1)
        valid_registry.consume(valid_handle, plan)

        expired_clock = MutableClock()
        expired_registry = ApprovalRegistry(clock=expired_clock)
        expired_handle = approve(expired_registry, plan)
        expired_clock.value = NOW + APPROVAL_TTL
        with self.assertRaisesRegex(
            ApprovalExpiredError,
            "^approval has expired$",
        ):
            expired_registry.consume(expired_handle, plan)
        self.assertEqual(expired_registry.active_count, 0)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            expired_registry.consume(expired_handle, plan)

    def test_new_grant_purges_abandoned_expired_records(self) -> None:
        plan = plan_fixture()
        clock = MutableClock()
        registry = ApprovalRegistry(clock=clock)
        expired_handle = approve(registry, plan)

        clock.value = NOW + APPROVAL_TTL
        current_handle = approve(registry, plan)

        self.assertEqual(registry.active_count, 1)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            registry.consume(expired_handle, plan)
        registry.consume(current_handle, plan)
        self.assertEqual(registry.active_count, 0)

    def test_handle_is_valid_only_in_its_issuing_registry(self) -> None:
        plan = plan_fixture()
        first = ApprovalRegistry(clock=MutableClock())
        second = ApprovalRegistry(clock=MutableClock())
        token_bytes = b"same-token-fixture-" + b"x" * 13
        self.assertEqual(len(token_bytes), 32)
        with mock.patch.object(
            approval.secrets,
            "token_bytes",
            return_value=token_bytes,
        ):
            first_handle = approve(first, plan)
            second_handle = approve(second, plan)

        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            second.consume(first_handle, plan)
        self.assertEqual(first.active_count, 1)
        self.assertEqual(second.active_count, 1)
        first.consume(first_handle, plan)
        second.consume(second_handle, plan)

    def test_token_reuse_cannot_reactivate_or_consume_with_old_handle(
        self,
    ) -> None:
        plan = plan_fixture()
        registry = ApprovalRegistry(clock=MutableClock())
        token_bytes = b"retired-token-fixture-" + b"x" * 10
        self.assertEqual(len(token_bytes), 32)
        with mock.patch.object(
            approval.secrets,
            "token_bytes",
            return_value=token_bytes,
        ):
            old_handle = approve(registry, plan)
            with self.assertRaisesRegex(
                ApprovalError,
                "^approval handle generation failed$",
            ):
                approve(registry, plan)

            registry.consume(old_handle, plan)
            new_handle = approve(registry, plan)

            with self.assertRaisesRegex(
                ApprovalUnavailableError,
                "^approval is unavailable$",
            ):
                registry.consume(old_handle, plan)
            self.assertEqual(registry.active_count, 1)
            registry.consume(new_handle, plan)

        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            registry.consume(old_handle, plan)
        self.assertEqual(registry.active_count, 0)

    def test_concurrent_consumers_allow_exactly_one_success(self) -> None:
        registry = ApprovalRegistry(clock=MutableClock())
        plan = plan_fixture()
        handle = approve(registry, plan)
        worker_count = 16
        barrier = threading.Barrier(worker_count)

        def consume_once() -> str:
            barrier.wait()
            try:
                registry.consume(handle, plan)
            except ApprovalUnavailableError:
                return "unavailable"
            return "success"

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = tuple(
                executor.map(
                    lambda _index: consume_once(),
                    range(worker_count),
                )
            )

        self.assertEqual(results.count("success"), 1)
        self.assertEqual(results.count("unavailable"), worker_count - 1)
        self.assertEqual(registry.active_count, 0)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_forked_child_cannot_consume_parent_process_approval(
        self,
    ) -> None:
        registry = ApprovalRegistry(clock=MutableClock())
        plan = plan_fixture()
        handle = approve(registry, plan)
        read_descriptor, write_descriptor = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_descriptor)
            result = b"unexpected"
            try:
                registry.consume(handle, plan)
            except ApprovalUnavailableError:
                result = b"rejected"
            except BaseException:
                result = b"wrong-error"
            try:
                os.write(write_descriptor, result)
            finally:
                os.close(write_descriptor)
            os._exit(0)

        os.close(write_descriptor)
        try:
            child_result = os.read(read_descriptor, 64)
        finally:
            os.close(read_descriptor)
        waited_pid, status = os.waitpid(child_pid, 0)

        self.assertEqual(waited_pid, child_pid)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(child_result, b"rejected")
        self.assertEqual(registry.active_count, 1)
        registry.consume(handle, plan)
        self.assertEqual(registry.active_count, 0)

    def test_later_apply_failure_cannot_restore_consumed_approval(self) -> None:
        registry = ApprovalRegistry(clock=MutableClock())
        plan = plan_fixture()
        handle = approve(registry, plan)
        apply_failure_canary = "private-apply-failure-fixture"

        registry.consume(handle, plan)
        with self.assertRaisesRegex(RuntimeError, apply_failure_canary):
            raise RuntimeError(apply_failure_canary)

        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            registry.consume(handle, plan)


class ApprovalClockAndRecordTests(unittest.TestCase):
    def test_default_clock_is_utc_aware_and_remains_injectable(self) -> None:
        plan = plan_fixture()
        with mock.patch.object(
            approval,
            "_utc_now",
            return_value=NOW,
        ) as default_clock:
            registry = ApprovalRegistry()
            handle = approve(registry, plan)
            record = registry.consume(handle, plan)

        self.assertEqual(default_clock.call_count, 2)
        self.assertEqual(record.approved_at, NOW)
        self.assertEqual(record.approved_at.tzinfo, timezone.utc)
        self.assertEqual(record.expires_at, NOW + APPROVAL_TTL)

        with self.assertRaisesRegex(TypeError, "^clock must be callable$"):
            ApprovalRegistry(clock=object())  # type: ignore[arg-type]

    def test_naive_exception_and_backwards_clocks_fail_closed(self) -> None:
        plan = plan_fixture()

        naive_registry = ApprovalRegistry(
            clock=MutableClock(NOW.replace(tzinfo=None))
        )
        with self.assertRaisesRegex(
            UnsafeApprovalClockError,
            "^approval clock is unsafe$",
        ):
            approve(naive_registry, plan)
        self.assertEqual(naive_registry.active_count, 0)

        clock_canary = "private-clock-exception-fixture"
        exploding_registry = ApprovalRegistry(
            clock=MutableClock(RuntimeError(clock_canary))
        )
        with self.assertRaisesRegex(
            UnsafeApprovalClockError,
            "^approval clock is unsafe$",
        ) as clock_error:
            approve(exploding_registry, plan)
        self.assertNotIn(clock_canary, str(clock_error.exception))
        self.assertNotIn(clock_canary, repr(clock_error.exception))
        self.assertEqual(exploding_registry.active_count, 0)

        backwards_clock = MutableClock()
        backwards_registry = ApprovalRegistry(clock=backwards_clock)
        handle = approve(backwards_registry, plan)
        backwards_clock.value = NOW - timedelta(microseconds=1)
        with self.assertRaisesRegex(
            UnsafeApprovalClockError,
            "^approval clock is unsafe$",
        ):
            backwards_registry.consume(handle, plan)
        self.assertEqual(backwards_registry.active_count, 0)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            backwards_registry.consume(handle, plan)

        consume_naive_clock = MutableClock()
        consume_naive_registry = ApprovalRegistry(clock=consume_naive_clock)
        handle = approve(consume_naive_registry, plan)
        consume_naive_clock.value = NOW.replace(tzinfo=None)
        with self.assertRaisesRegex(
            UnsafeApprovalClockError,
            "^approval clock is unsafe$",
        ):
            consume_naive_registry.consume(handle, plan)
        self.assertEqual(consume_naive_registry.active_count, 0)
        with self.assertRaisesRegex(
            ApprovalUnavailableError,
            "^approval is unavailable$",
        ):
            consume_naive_registry.consume(handle, plan)

    def test_record_rejects_invalid_timestamps_ttl_and_binding(self) -> None:
        plan = plan_fixture()
        valid = {
            "plan_digest": plan.digest,
            "mode": plan.mode,
            "target": plan.target,
            "store_fingerprint": plan.store_fingerprint,
            "approved_at": NOW,
            "expires_at": NOW + APPROVAL_TTL,
        }
        invalid_overrides = (
            {"approved_at": NOW.replace(tzinfo=None)},
            {"expires_at": NOW + APPROVAL_TTL + timedelta(microseconds=1)},
            {"mode": RuntimeMode.EMPTY},
            {
                "target": PlanTarget(
                    store=STANDALONE_STORE_ID,
                    provider_id=STANDALONE_PROFILE_ID,
                )
            },
            {"plan_digest": "not-a-digest"},
            {"store_fingerprint": "not-a-fingerprint"},
            {"schema_version": 2},
            {"schema_version": 1.0},
        )
        for overrides in invalid_overrides:
            with self.subTest(field=next(iter(overrides))):
                values = dict(valid)
                values.update(overrides)
                with self.assertRaises(InvalidApprovalRecordError):
                    ApprovalRecord(**values)  # type: ignore[arg-type]

    def test_approval_flow_has_no_home_network_file_or_store_access(
        self,
    ) -> None:
        plan = plan_fixture()
        registry = ApprovalRegistry(clock=MutableClock())
        with (
            mock.patch.object(
                pathlib.Path,
                "home",
                side_effect=AssertionError("HOME access is forbidden"),
            ),
            mock.patch.object(
                pathlib.Path,
                "write_text",
                side_effect=AssertionError("file write is forbidden"),
            ),
            mock.patch.object(
                pathlib.Path,
                "write_bytes",
                side_effect=AssertionError("file write is forbidden"),
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
            mock.patch.object(
                builtins,
                "open",
                side_effect=AssertionError("file access is forbidden"),
            ),
        ):
            previews: list[str] = []
            handle = request_tui_approval(
                plan,
                registry,
                show_preview=previews.append,
                confirm=lambda: True,
            )
            record = registry.consume(handle, plan)
            rendered = approval_record_preview(record)

        self.assertEqual(previews, [tui_preview(plan)])
        self.assertIn("Approval record v1", rendered)

    def test_module_has_no_store_persistence_or_network_dependency(self) -> None:
        source_path = pathlib.Path(approval.__file__)
        parsed = ast.parse(source_path.read_text(encoding="utf-8"))
        direct_imports: set[str] = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                direct_imports.update(
                    alias.name.split(".", maxsplit=1)[0]
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                direct_imports.add(node.module.split(".", maxsplit=1)[0])

        self.assertTrue(
            {
                "pathlib",
                "socket",
                "sqlite3",
                "subprocess",
                "keyring",
                "standalone",
                "store",
            }.isdisjoint(direct_imports)
        )
        os_attributes = {
            node.attr
            for node in ast.walk(parsed)
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            )
        }
        self.assertEqual(os_attributes, {"getpid"})


if __name__ == "__main__":
    unittest.main()
