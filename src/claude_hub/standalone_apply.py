"""Approved compare-and-swap updates for standalone profile routing metadata.

The service is the only write seam for approved model and purpose-tag plans.
It never resolves ``secretRef`` values, reads process configuration, contacts
the network, or writes a CC Switch store.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .approval import ApprovalRegistry
from .change_plan import (
    ALLOWED_CHANGE_FIELDS,
    PURPOSE_TAGS_FIELD,
    STANDALONE_STORE_ID,
    ChangePlan,
    FieldChange,
    InvalidChangePlanError,
    PlanTarget,
    _normalize_purpose_tags as _normalize_public_purpose_tags,
    build_change_plan,
)
from .domain import ModelMapping, ProviderRef, RuntimeMode, StandaloneProfile
from .standalone import (
    StandaloneProfileNotFoundError,
    StandaloneProfileStore,
    _decode_profile,
    _load_snapshot,
    _profile_key,
    _serialize_document,
    _store_lock,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UNREADABLE = object()


class StandaloneApplyError(RuntimeError):
    """Base class for fixed, non-reflective standalone-apply failures."""


class StandaloneApplyInspectionError(StandaloneApplyError):
    """Raised when stored metadata is unsafe for a public inspection."""


class StandaloneApplyTargetError(StandaloneApplyError):
    """Raised after consuming approval for a non-standalone or missing target."""


class StandaloneApplyConflictError(StandaloneApplyError):
    """Raised when the whole standalone document no longer matches the plan."""


class StandaloneApplyWriteError(StandaloneApplyError):
    """Raised when a failure is known to have occurred before replacement."""


class StandaloneCommitStateUnknownError(StandaloneApplyError):
    """Raised when failure occurs at or after the atomic replacement boundary."""

    code = "commit_state_unknown"


# Short compatibility spelling for callers that key off the public error code.
CommitStateUnknownError = StandaloneCommitStateUnknownError


@dataclass(frozen=True, slots=True, repr=False)
class StandaloneApplyInspection:
    """Secret-free planning snapshot for one standalone profile."""

    reference: ProviderRef
    models: ModelMapping
    purpose_tags: tuple[str, ...]
    store_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ProviderRef):
            raise TypeError("reference must be a ProviderRef")
        if self.reference.store != STANDALONE_STORE_ID:
            raise ValueError("inspection target is invalid")
        if not isinstance(self.models, ModelMapping):
            raise TypeError("models must be a ModelMapping")
        if not isinstance(self.purpose_tags, tuple) or not all(
            isinstance(tag, str) for tag in self.purpose_tags
        ):
            raise TypeError("purpose_tags must be a tuple of strings")
        try:
            # Validation only: retain the Store's stable tag order rather than
            # adopting ChangePlan's sorted diff representation.
            _normalize_public_purpose_tags(self.purpose_tags)
        except InvalidChangePlanError:
            raise StandaloneApplyInspectionError(
                "standalone inspection contains non-public purpose tags"
            ) from None
        if not isinstance(self.store_fingerprint, str) or not _SHA256_RE.fullmatch(
            self.store_fingerprint
        ):
            raise ValueError("store_fingerprint must be a SHA-256 digest")

    @property
    def fingerprint(self) -> str:
        return self.store_fingerprint

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(target=<redacted>, "
            f"models={self.models!r}, "
            f"purpose_tag_count={len(self.purpose_tags)}, "
            "store_fingerprint=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class StandaloneApplyResult:
    """Verified post-commit fingerprint and the approved public-field diff."""

    new_fingerprint: str
    redacted_diff: tuple[FieldChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.new_fingerprint, str) or not _SHA256_RE.fullmatch(
            self.new_fingerprint
        ):
            raise ValueError("new_fingerprint must be a SHA-256 digest")
        if not isinstance(self.redacted_diff, tuple) or not all(
            type(change) is FieldChange for change in self.redacted_diff
        ):
            raise TypeError("redacted_diff must contain field changes")

    @property
    def fingerprint(self) -> str:
        return self.new_fingerprint

    @property
    def changes(self) -> tuple[FieldChange, ...]:
        return self.redacted_diff

    def __repr__(self) -> str:
        fields = tuple(change.field for change in self.redacted_diff)
        return (
            f"{type(self).__name__}(new_fingerprint=<redacted>, "
            f"fields={fields!r})"
        )


def _fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _snapshot_profile(
    store: StandaloneProfileStore,
    profile_id: UUID | str,
) -> tuple[bytes, dict[str, Any], StandaloneProfile]:
    profile_key = _profile_key(profile_id)
    payload, document = _load_snapshot(store.path)
    if payload is None:
        raise StandaloneProfileNotFoundError(
            "standalone profile was not found"
        )
    profiles = document["profiles"]
    try:
        raw_profile = profiles[profile_key]
    except KeyError:
        raise StandaloneProfileNotFoundError(
            "standalone profile was not found"
        ) from None
    profile = _decode_profile(profile_key, raw_profile)
    return payload, document, profile


def _canonical_requested_field(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidChangePlanError("change field is not allowed")
    if value == "purposeTags":
        return PURPOSE_TAGS_FIELD
    if value in ALLOWED_CHANGE_FIELDS:
        return value
    raise InvalidChangePlanError("change field is not allowed")


def _current_value(
    profile: StandaloneProfile,
    field_name: str,
) -> object:
    if field_name == PURPOSE_TAGS_FIELD:
        return profile.purpose_tags
    slot = field_name.removeprefix("models.")
    return getattr(profile.models, slot)


def _strict_fsync_directory(parent: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _readback(path: Path) -> bytes:
    payload, _document = _load_snapshot(path)
    if payload is None:
        raise OSError
    return payload


def _observed_payload(path: Path) -> bytes | None | object:
    try:
        payload, _document = _load_snapshot(path)
        return payload
    except Exception:
        return _UNREADABLE


def _atomic_commit(
    path: Path,
    *,
    original: bytes,
    candidate: bytes,
) -> str:
    descriptor = -1
    temporary_path: str | None = None
    replaced = False
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary_path, 0o600)
        handle = os.fdopen(descriptor, "w+b")
        descriptor = -1
        with handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if handle.read(len(candidate) + 1) != candidate:
                raise OSError

        try:
            os.replace(temporary_path, path)
            replaced = True
            temporary_path = None
            _strict_fsync_directory(path.parent)
            readback = _readback(path)
            if readback != candidate:
                raise OSError
        except (KeyboardInterrupt, SystemExit):
            observed = _observed_payload(path)
            if replaced or observed != original:
                raise StandaloneCommitStateUnknownError(
                    "standalone apply commit state is unknown"
                ) from None
            raise
        except Exception:
            if replaced:
                raise StandaloneCommitStateUnknownError(
                    "standalone apply commit state is unknown"
                ) from None
            observed = _observed_payload(path)
            if observed != original:
                raise StandaloneCommitStateUnknownError(
                    "standalone apply commit state is unknown"
                ) from None
            raise StandaloneApplyWriteError(
                "standalone apply write failed"
            ) from None
        return _fingerprint(readback)
    except StandaloneApplyError:
        raise
    except (OSError, ValueError):
        if replaced:
            raise StandaloneCommitStateUnknownError(
                "standalone apply commit state is unknown"
            ) from None
        raise StandaloneApplyWriteError(
            "standalone apply write failed"
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except (FileNotFoundError, OSError):
                pass


class StandaloneApplyService:
    """Shared inspect, plan, and one-shot approved apply service."""

    __slots__ = ("_store", "_approvals")

    def __init__(
        self,
        store: StandaloneProfileStore,
        approvals: ApprovalRegistry,
    ) -> None:
        if not isinstance(store, StandaloneProfileStore):
            raise TypeError("store must be a StandaloneProfileStore")
        if not isinstance(approvals, ApprovalRegistry):
            raise TypeError("approvals must be an ApprovalRegistry")
        self._store = store
        self._approvals = approvals

    def inspect(
        self,
        profile_id: UUID | str,
    ) -> StandaloneApplyInspection:
        payload, _document, profile = _snapshot_profile(
            self._store,
            profile_id,
        )
        return StandaloneApplyInspection(
            reference=ProviderRef(
                store=STANDALONE_STORE_ID,
                provider_id=str(profile.profile_id),
            ),
            models=profile.models,
            purpose_tags=profile.purpose_tags,
            store_fingerprint=_fingerprint(payload),
        )

    def create_plan(
        self,
        profile_id: UUID | str,
        *,
        changes: Mapping[str, object],
    ) -> ChangePlan:
        if not isinstance(changes, Mapping):
            raise InvalidChangePlanError("changes are invalid")
        payload, _document, profile = _snapshot_profile(
            self._store,
            profile_id,
        )
        planned: dict[str, tuple[object, object]] = {}
        try:
            items = tuple(changes.items())
        except Exception:
            raise InvalidChangePlanError("changes are invalid") from None
        for raw_field, new_value in items:
            field_name = _canonical_requested_field(raw_field)
            if field_name in planned:
                raise InvalidChangePlanError(
                    "change fields must be unique"
                )
            planned[field_name] = (
                _current_value(profile, field_name),
                new_value,
            )
        return build_change_plan(
            mode=RuntimeMode.STANDALONE,
            target=PlanTarget(
                store=STANDALONE_STORE_ID,
                provider_id=str(profile.profile_id),
            ),
            store_fingerprint=_fingerprint(payload),
            changes=planned,
        )

    # Concise spelling for application surfaces that already say "plan".
    plan = create_plan

    def apply(
        self,
        plan: ChangePlan,
        approval: object,
    ) -> StandaloneApplyResult:
        # Consumption is deliberately first.  Every later target, CAS, parse,
        # fsync, replace, and readback failure retires the capability.
        self._approvals.consume(approval, plan)

        if (
            type(plan) is not ChangePlan
            or plan.mode is not RuntimeMode.STANDALONE
            or plan.target.store != STANDALONE_STORE_ID
        ):
            raise StandaloneApplyTargetError(
                "standalone apply target is invalid"
            )

        with _store_lock(self._store.path):
            try:
                payload, document, profile = _snapshot_profile(
                    self._store,
                    plan.target.provider_id,
                )
            except StandaloneProfileNotFoundError:
                raise StandaloneApplyTargetError(
                    "standalone apply target is invalid"
                ) from None
            if not hmac.compare_digest(
                _fingerprint(payload),
                plan.store_fingerprint,
            ):
                raise StandaloneApplyConflictError(
                    "standalone store changed after planning"
                )

            raw_profile = document["profiles"][plan.target.provider_id]
            models = raw_profile["models"]
            for change in plan.changes:
                current = _current_value(profile, change.field)
                if change.field == PURPOSE_TAGS_FIELD:
                    current = tuple(sorted(current))
                if current != change.old:
                    raise StandaloneApplyConflictError(
                        "standalone plan no longer matches target"
                    )
                if change.field == PURPOSE_TAGS_FIELD:
                    raw_profile["purposeTags"] = list(change.new)
                    continue
                slot = change.field.removeprefix("models.")
                if change.new is None:
                    models.pop(slot, None)
                else:
                    models[slot] = change.new

            candidate = _serialize_document(document)
            new_fingerprint = _atomic_commit(
                self._store.path,
                original=payload,
                candidate=candidate,
            )
        return StandaloneApplyResult(
            new_fingerprint=new_fingerprint,
            redacted_diff=plan.changes,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(store=<redacted>)"


# Concise name for callers that are already within a standalone context.
StandaloneInspection = StandaloneApplyInspection


__all__ = [
    "CommitStateUnknownError",
    "StandaloneApplyConflictError",
    "StandaloneApplyError",
    "StandaloneApplyInspectionError",
    "StandaloneApplyInspection",
    "StandaloneApplyResult",
    "StandaloneApplyService",
    "StandaloneApplyTargetError",
    "StandaloneApplyWriteError",
    "StandaloneCommitStateUnknownError",
    "StandaloneInspection",
]
