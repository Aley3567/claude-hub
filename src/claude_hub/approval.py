"""Process-local, one-shot human approvals for immutable change plans.

Approval handles are opaque capabilities.  They cannot be serialized, parsed
from command-line text, or used with a different registry.  The registry owns
no Store and performs no filesystem, network, credential, or apply operation.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from .change_plan import (
    COMPANION_STORE_ID,
    STANDALONE_STORE_ID,
    ChangePlan,
    FieldChange,
    PlanTarget,
    change_plan_digest,
)
from .domain import RuntimeMode


APPROVAL_SCHEMA_VERSION = 1
APPROVAL_TTL = timedelta(minutes=15)
_HANDLE_BYTES = 32
_MAX_TOKEN_ATTEMPTS = 8
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HANDLE_CONSTRUCTION_GUARD = object()
_MISSING = object()


class ApprovalError(RuntimeError):
    """Base class for fixed, non-reflective approval failures."""


class InvalidApprovalRecordError(ApprovalError):
    """Raised when an approval record cannot be safely represented."""


class UnsafeApprovalClockError(ApprovalError):
    """Raised when injected time is naive, invalid, or moves backwards."""


class ApprovalUnavailableError(ApprovalError):
    """Raised when a handle is unknown, foreign, or already consumed."""


class ApprovalExpiredError(ApprovalError):
    """Raised after atomically consuming an expired approval."""


class ApprovalBindingError(ApprovalError):
    """Raised after atomically consuming an approval for another plan."""


class HumanConfirmationError(ApprovalError):
    """Raised when a human-confirmation adapter fails closed."""


def _normalize_aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidApprovalRecordError("approval timestamp is invalid")
    try:
        offset = value.utcoffset()
    except Exception:
        raise InvalidApprovalRecordError(
            "approval timestamp is invalid"
        ) from None
    if offset is None:
        raise InvalidApprovalRecordError("approval timestamp is invalid")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise InvalidApprovalRecordError(
            "approval timestamp is invalid"
        ) from None


def _validate_record_target(mode: RuntimeMode, target: PlanTarget) -> None:
    expected_store = (
        COMPANION_STORE_ID
        if mode is RuntimeMode.COMPANION
        else STANDALONE_STORE_ID
    )
    if target.store != expected_store:
        raise InvalidApprovalRecordError("approval target is invalid")
    if mode is RuntimeMode.STANDALONE:
        try:
            canonical_id = str(UUID(target.provider_id))
        except (AttributeError, ValueError):
            raise InvalidApprovalRecordError(
                "approval target is invalid"
            ) from None
        if canonical_id != target.provider_id:
            raise InvalidApprovalRecordError("approval target is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalRecord:
    """Secret-free binding metadata for one human-approved plan."""

    plan_digest: str
    mode: RuntimeMode
    target: PlanTarget
    store_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    schema_version: int = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan_digest, str)
            or not _SHA256_RE.fullmatch(self.plan_digest)
        ):
            raise InvalidApprovalRecordError(
                "approval plan digest is invalid"
            )
        if not isinstance(self.mode, RuntimeMode) or self.mode not in {
            RuntimeMode.COMPANION,
            RuntimeMode.STANDALONE,
        }:
            raise InvalidApprovalRecordError("approval mode is invalid")
        if type(self.target) is not PlanTarget:
            raise InvalidApprovalRecordError("approval target is invalid")
        _validate_record_target(self.mode, self.target)
        if (
            not isinstance(self.store_fingerprint, str)
            or not _SHA256_RE.fullmatch(self.store_fingerprint)
        ):
            raise InvalidApprovalRecordError(
                "approval Store fingerprint is invalid"
            )
        approved_at = _normalize_aware_datetime(self.approved_at)
        expires_at = _normalize_aware_datetime(self.expires_at)
        if expires_at - approved_at != APPROVAL_TTL:
            raise InvalidApprovalRecordError(
                "approval expiration is invalid"
            )
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != APPROVAL_SCHEMA_VERSION
        ):
            raise InvalidApprovalRecordError(
                "approval schema version is unsupported"
            )
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "expires_at", expires_at)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"schema_version={self.schema_version!r}, "
            f"mode={self.mode.value!r}, binding=<redacted>, "
            f"approved_at={self.approved_at.isoformat()!r}, "
            f"expires_at={self.expires_at.isoformat()!r})"
        )


class ApprovalHandle:
    """Opaque, non-serializable capability issued by one registry."""

    __slots__ = ("__registry_marker", "__token", "__sealed")

    def __init__(
        self,
        *,
        token: bytes,
        registry_marker: object,
        construction_guard: object,
    ) -> None:
        if construction_guard is not _HANDLE_CONSTRUCTION_GUARD:
            raise TypeError("approval handles are registry-issued")
        if not isinstance(token, bytes) or len(token) != _HANDLE_BYTES:
            raise TypeError("approval handle material is invalid")
        object.__setattr__(self, "_ApprovalHandle__token", token)
        object.__setattr__(
            self,
            "_ApprovalHandle__registry_marker",
            registry_marker,
        )
        object.__setattr__(self, "_ApprovalHandle__sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_ApprovalHandle__sealed", False):
            raise AttributeError("approval handles are immutable")
        object.__setattr__(self, name, value)

    def __copy__(self) -> ApprovalHandle:
        return self

    def __deepcopy__(self, _memo: object) -> ApprovalHandle:
        return self

    def __reduce__(self) -> object:
        raise TypeError("approval handles are process-local")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("approval handles are process-local")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    __str__ = __repr__


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def approval_record_preview(record: ApprovalRecord) -> str:
    """Render binding metadata without a raw plan or capability handle."""

    if not isinstance(record, ApprovalRecord):
        raise TypeError("record must be an ApprovalRecord")
    return "\n".join(
        (
            f"Approval record v{record.schema_version}",
            f"Mode: {record.mode.value}",
            (
                "Target: "
                f"{record.target.store}/{record.target.provider_id}"
            ),
            f"Plan digest: {record.plan_digest}",
            f"Store fingerprint: {record.store_fingerprint}",
            f"Approved at: {_format_timestamp(record.approved_at)}",
            f"Expires at: {_format_timestamp(record.expires_at)}",
        )
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_exact_change_plan(plan: object) -> bool:
    """Return whether a plan uses only the sealed v1 domain value types."""

    return (
        type(plan) is ChangePlan
        and type(plan.target) is PlanTarget
        and type(plan.changes) is tuple
        and all(type(change) is FieldChange for change in plan.changes)
    )


class ApprovalRegistry:
    """Thread-safe in-memory registry of one-shot approval capabilities."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock):
            raise TypeError("clock must be callable")
        self._clock = selected_clock
        self._lock = threading.RLock()
        self._process_id = os.getpid()
        self._registry_marker = object()
        self._records: dict[
            bytes,
            tuple[ApprovalHandle, ApprovalRecord],
        ] = {}
        self._last_clock_value: datetime | None = None

    def __repr__(self) -> str:
        if os.getpid() != self._process_id:
            return f"{type(self).__name__}(active=<unavailable>)"
        with self._lock:
            active_count = len(self._records)
        return f"{type(self).__name__}(active={active_count})"

    @property
    def active_count(self) -> int:
        if os.getpid() != self._process_id:
            raise ApprovalUnavailableError("approval registry is unavailable")
        with self._lock:
            return len(self._records)

    def _read_clock_locked(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise UnsafeApprovalClockError(
                "approval clock is unsafe"
            ) from None
        try:
            normalized = _normalize_aware_datetime(value)
        except InvalidApprovalRecordError:
            raise UnsafeApprovalClockError(
                "approval clock is unsafe"
            ) from None
        if (
            self._last_clock_value is not None
            and normalized < self._last_clock_value
        ):
            raise UnsafeApprovalClockError(
                "approval clock is unsafe"
            )
        self._last_clock_value = normalized
        return normalized

    def _new_token_locked(self) -> bytes:
        for _attempt in range(_MAX_TOKEN_ATTEMPTS):
            try:
                token = secrets.token_bytes(_HANDLE_BYTES)
            except Exception:
                raise ApprovalError(
                    "approval handle generation failed"
                ) from None
            if (
                isinstance(token, bytes)
                and len(token) == _HANDLE_BYTES
                and token not in self._records
            ):
                return token
        raise ApprovalError("approval handle generation failed")

    def _purge_expired_locked(self, now: datetime) -> None:
        expired_tokens = tuple(
            token
            for token, entry in self._records.items()
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[1], ApprovalRecord)
                or now >= entry[1].expires_at
            )
        )
        for token in expired_tokens:
            self._records.pop(token, None)

    def _grant_after_human_confirmation(
        self,
        plan: ChangePlan,
    ) -> ApprovalHandle:
        """Issue only after the TUI adapter receives explicit confirmation."""

        if not _is_exact_change_plan(plan):
            raise InvalidApprovalRecordError("approval plan is invalid")
        if os.getpid() != self._process_id:
            raise ApprovalUnavailableError("approval registry is unavailable")
        with self._lock:
            approved_at = self._read_clock_locked()
            try:
                expires_at = approved_at + APPROVAL_TTL
            except OverflowError:
                raise UnsafeApprovalClockError(
                    "approval clock is unsafe"
                ) from None
            self._purge_expired_locked(approved_at)
            record = ApprovalRecord(
                plan_digest=change_plan_digest(plan),
                mode=plan.mode,
                target=plan.target,
                store_fingerprint=plan.store_fingerprint,
                approved_at=approved_at,
                expires_at=expires_at,
            )
            token = self._new_token_locked()
            handle = ApprovalHandle(
                token=token,
                registry_marker=self._registry_marker,
                construction_guard=_HANDLE_CONSTRUCTION_GUARD,
            )
            self._records[token] = (handle, record)
            return handle

    def consume(
        self,
        handle: object,
        plan: object,
    ) -> ApprovalRecord:
        """Atomically remove and validate one approval binding."""

        if os.getpid() != self._process_id:
            raise ApprovalUnavailableError("approval is unavailable")
        with self._lock:
            if not isinstance(handle, ApprovalHandle):
                raise ApprovalUnavailableError("approval is unavailable")
            try:
                registry_marker = handle._ApprovalHandle__registry_marker
                token = handle._ApprovalHandle__token
            except AttributeError:
                raise ApprovalUnavailableError(
                    "approval is unavailable"
                ) from None
            if registry_marker is not self._registry_marker:
                raise ApprovalUnavailableError("approval is unavailable")

            entry = self._records.get(token, _MISSING)
            if (
                entry is _MISSING
                or not isinstance(entry, tuple)
                or len(entry) != 2
            ):
                raise ApprovalUnavailableError("approval is unavailable")
            issued_handle, record = entry
            if issued_handle is not handle:
                # A retired handle must not consume a newer approval even if
                # the cryptographically random token is ever generated again.
                raise ApprovalUnavailableError("approval is unavailable")
            self._records.pop(token, None)
            if not isinstance(record, ApprovalRecord):
                raise ApprovalUnavailableError("approval is unavailable")

            now = self._read_clock_locked()
            if now >= record.expires_at:
                raise ApprovalExpiredError("approval has expired")
            if not self._matches(record, plan):
                raise ApprovalBindingError(
                    "approval binding does not match plan"
                )
            return record

    @staticmethod
    def _matches(record: ApprovalRecord, plan: object) -> bool:
        if not _is_exact_change_plan(plan):
            return False
        try:
            return (
                hmac.compare_digest(
                    record.plan_digest,
                    change_plan_digest(plan),
                )
                and record.mode is plan.mode
                and record.target == plan.target
                and hmac.compare_digest(
                    record.store_fingerprint,
                    plan.store_fingerprint,
                )
            )
        except Exception:
            return False


__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "APPROVAL_TTL",
    "ApprovalBindingError",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalHandle",
    "ApprovalRecord",
    "ApprovalRegistry",
    "ApprovalUnavailableError",
    "HumanConfirmationError",
    "InvalidApprovalRecordError",
    "UnsafeApprovalClockError",
    "approval_record_preview",
]
