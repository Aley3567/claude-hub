"""Immutable, deterministic, and secret-free provider change plans.

This module is deliberately a pure domain boundary.  It accepts no Store,
filesystem, network, keyring, or process-environment object and exposes no
approval or apply operation.  A plan can therefore be constructed and
previewed without changing either Companion or Standalone state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from .domain import ModelMapping, ProviderRef, RuntimeMode


PLAN_SCHEMA_VERSION = 1
COMPANION_STORE_ID = "cc-switch"
STANDALONE_STORE_ID = "standalone"
PURPOSE_TAGS_FIELD = "purpose_tags"
MODEL_CHANGE_FIELDS = (
    "models.default",
    "models.fast",
    "models.reasoning",
    "models.coding",
    "models.long_context",
    "models.fallback",
)
ALLOWED_CHANGE_FIELDS = MODEL_CHANGE_FIELDS + (PURPOSE_TAGS_FIELD,)
UNCHANGED_FIELD_NAMES = (
    "baseUrl",
    "credential",
    "current",
    "proxyTakeover",
)

_FIELD_ORDER = {
    field_name: index
    for index, field_name in enumerate(ALLOWED_CHANGE_FIELDS)
}
_PURPOSE_TAG_ALIASES = frozenset(
    {
        PURPOSE_TAGS_FIELD,
        "purposeTags",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_PURPOSE_TAGS = 32
_MAX_PURPOSE_TAG_LENGTH = 64


class ChangePlanError(ValueError):
    """Base class for fixed, non-reflective change-plan failures."""


class InvalidChangePlanError(ChangePlanError):
    """Raised when a plan value violates the public plan contract."""


class EmptyChangePlanError(ChangePlanError):
    """Raised when no semantic field change remains after normalization."""


def _is_structured_json(value: str) -> bool:
    if not value.startswith(("{", "[")):
        return False
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(decoded, (dict, list))


def _canonical_field_name(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidChangePlanError("change field is not allowed")
    if value in _PURPOSE_TAG_ALIASES:
        return PURPOSE_TAGS_FIELD
    if value not in MODEL_CHANGE_FIELDS:
        raise InvalidChangePlanError("change field is not allowed")
    return value


def _normalize_model_value(field_name: str, value: object) -> str | None:
    if value is None:
        return None
    slot_name = field_name.removeprefix("models.")
    try:
        mapping = ModelMapping(**{slot_name: value})
    except (TypeError, ValueError):
        raise InvalidChangePlanError(
            "model change value is not a public identifier"
        ) from None
    normalized = getattr(mapping, slot_name)
    if not isinstance(normalized, str):
        raise InvalidChangePlanError(
            "model change value is not a public identifier"
        )
    if _is_structured_json(normalized):
        raise InvalidChangePlanError(
            "model change value is not a public identifier"
        )
    return normalized


def _normalize_purpose_tags(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise InvalidChangePlanError("purpose tags are invalid")
    try:
        raw_tags = tuple(value)  # type: ignore[arg-type]
    except Exception:
        raise InvalidChangePlanError("purpose tags are invalid") from None
    if len(raw_tags) > _MAX_PURPOSE_TAGS:
        raise InvalidChangePlanError("purpose tags are invalid")

    normalized: set[str] = set()
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str):
            raise InvalidChangePlanError("purpose tags are invalid")
        tag = raw_tag.strip()
        if not tag or len(tag) > _MAX_PURPOSE_TAG_LENGTH:
            raise InvalidChangePlanError("purpose tags are invalid")
        if _is_structured_json(tag):
            raise InvalidChangePlanError("purpose tags are invalid")
        try:
            # ModelMapping applies the shared public-boundary checks for
            # controls, URLs, absolute paths, and secret-like material.
            ModelMapping(default=tag)
        except (TypeError, ValueError):
            raise InvalidChangePlanError("purpose tags are invalid") from None
        normalized.add(tag)
    return tuple(sorted(normalized))


def _normalize_change_value(field_name: str, value: object) -> object:
    if field_name == PURPOSE_TAGS_FIELD:
        return _normalize_purpose_tags(value)
    return _normalize_model_value(field_name, value)


@dataclass(frozen=True, slots=True, repr=False)
class PlanTarget:
    """Stable public identity, intentionally excluding presentation metadata."""

    store: str
    provider_id: str

    def __post_init__(self) -> None:
        try:
            reference = ProviderRef(
                store=self.store,
                provider_id=self.provider_id,
            )
        except (TypeError, ValueError):
            raise InvalidChangePlanError(
                "change plan target is invalid"
            ) from None
        object.__setattr__(self, "store", reference.store)
        object.__setattr__(self, "provider_id", reference.provider_id)

    @classmethod
    def from_reference(cls, reference: ProviderRef) -> PlanTarget:
        if not isinstance(reference, ProviderRef):
            raise InvalidChangePlanError("change plan target is invalid")
        return cls(
            store=reference.store,
            provider_id=reference.provider_id,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(identity=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class FieldChange:
    """One whitelisted field-level old/new transition."""

    field: str
    old: object
    new: object

    def __post_init__(self) -> None:
        field_name = _canonical_field_name(self.field)
        old_value = _normalize_change_value(field_name, self.old)
        new_value = _normalize_change_value(field_name, self.new)
        if old_value == new_value:
            raise InvalidChangePlanError(
                "change entry must describe a semantic change"
            )
        object.__setattr__(self, "field", field_name)
        object.__setattr__(self, "old", old_value)
        object.__setattr__(self, "new", new_value)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(field={self.field!r}, "
            "old=<redacted>, new=<redacted>)"
        )


def _change_pair(value: object) -> tuple[object, object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidChangePlanError("change entry must contain old and new")
    if len(value) != 2:
        raise InvalidChangePlanError("change entry must contain old and new")
    return value[0], value[1]


def _changes_from_mapping(
    values: Mapping[object, object],
) -> tuple[FieldChange, ...]:
    normalized: list[FieldChange] = []
    seen: set[str] = set()
    try:
        raw_items = tuple(values.items())
    except Exception:
        raise InvalidChangePlanError("changes are invalid") from None
    for raw_field, raw_pair in raw_items:
        field_name = _canonical_field_name(raw_field)
        if field_name in seen:
            raise InvalidChangePlanError("change fields must be unique")
        seen.add(field_name)
        old_value, new_value = _change_pair(raw_pair)
        normalized_old = _normalize_change_value(field_name, old_value)
        normalized_new = _normalize_change_value(field_name, new_value)
        if normalized_old == normalized_new:
            continue
        normalized.append(
            FieldChange(
                field=field_name,
                old=normalized_old,
                new=normalized_new,
            )
        )
    return tuple(normalized)


def _normalize_changes(
    values: object,
) -> tuple[FieldChange, ...]:
    if isinstance(values, Mapping):
        normalized = _changes_from_mapping(values)
    else:
        if isinstance(values, (str, bytes)):
            raise InvalidChangePlanError("changes are invalid")
        try:
            raw_changes = tuple(values)  # type: ignore[arg-type]
        except Exception:
            raise InvalidChangePlanError("changes are invalid") from None
        if not all(isinstance(item, FieldChange) for item in raw_changes):
            raise InvalidChangePlanError("changes are invalid")
        normalized = raw_changes

    if not normalized:
        raise EmptyChangePlanError("change plan must not be empty")

    seen: set[str] = set()
    for change in normalized:
        if change.field in seen:
            raise InvalidChangePlanError("change fields must be unique")
        seen.add(change.field)
    return tuple(
        sorted(
            normalized,
            key=lambda change: _FIELD_ORDER[change.field],
        )
    )


def _normalize_mode(value: object) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        mode = value
    elif isinstance(value, str):
        try:
            mode = RuntimeMode(value)
        except ValueError:
            raise InvalidChangePlanError(
                "change plan mode is unsupported"
            ) from None
    else:
        raise InvalidChangePlanError("change plan mode is unsupported")
    if mode not in {RuntimeMode.COMPANION, RuntimeMode.STANDALONE}:
        raise InvalidChangePlanError("change plan mode is unsupported")
    return mode


def _normalize_target(value: object) -> PlanTarget:
    if isinstance(value, PlanTarget):
        return value
    if isinstance(value, ProviderRef):
        return PlanTarget.from_reference(value)
    raise InvalidChangePlanError("change plan target is invalid")


def _require_canonical_standalone_target(target: PlanTarget) -> None:
    try:
        canonical_id = str(UUID(target.provider_id))
    except (AttributeError, ValueError):
        raise InvalidChangePlanError(
            "standalone target must use a canonical UUID"
        ) from None
    if canonical_id != target.provider_id:
        raise InvalidChangePlanError(
            "standalone target must use a canonical UUID"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ChangePlan:
    """Versioned, immutable description of an allowed provider adjustment."""

    mode: RuntimeMode
    target: PlanTarget
    store_fingerprint: str
    changes: tuple[FieldChange, ...]
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        mode = _normalize_mode(self.mode)
        target = _normalize_target(self.target)
        fingerprint = self.store_fingerprint
        if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(
            fingerprint
        ):
            raise InvalidChangePlanError(
                "store fingerprint must be a SHA-256 digest"
            )
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != PLAN_SCHEMA_VERSION
        ):
            raise InvalidChangePlanError(
                "change plan schema version is unsupported"
            )
        changes = _normalize_changes(self.changes)
        expected_store = (
            COMPANION_STORE_ID
            if mode is RuntimeMode.COMPANION
            else STANDALONE_STORE_ID
        )
        if target.store != expected_store:
            raise InvalidChangePlanError(
                "change plan target does not match runtime mode"
            )
        if mode is RuntimeMode.STANDALONE:
            _require_canonical_standalone_target(target)
        if (
            mode is not RuntimeMode.STANDALONE
            and any(
                change.field == PURPOSE_TAGS_FIELD
                for change in changes
            )
        ):
            raise InvalidChangePlanError(
                "purpose tags require standalone mode"
            )

        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "store_fingerprint", fingerprint)
        object.__setattr__(self, "changes", changes)

    @property
    def digest(self) -> str:
        return change_plan_digest(self)

    def to_canonical_json(self) -> str:
        return canonical_change_plan_json(self)

    def __repr__(self) -> str:
        fields = tuple(change.field for change in self.changes)
        return (
            f"{type(self).__name__}(schema_version={self.schema_version!r}, "
            f"mode={self.mode.value!r}, target=<redacted>, "
            f"fields={fields!r}, store_fingerprint=<redacted>, "
            f"digest={self.digest!r})"
        )


def build_change_plan(
    *,
    mode: RuntimeMode | str,
    target: PlanTarget | ProviderRef,
    store_fingerprint: str,
    changes: Mapping[str, object] | Iterable[FieldChange],
    schema_version: int = PLAN_SCHEMA_VERSION,
) -> ChangePlan:
    """Construct a normalized plan from mapping or field-change input."""

    return ChangePlan(
        mode=mode,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        store_fingerprint=store_fingerprint,
        changes=changes,  # type: ignore[arg-type]
        schema_version=schema_version,
    )


def _serialized_change_value(value: object) -> object:
    if isinstance(value, tuple):
        return list(value)
    return value


def _canonical_payload(plan: ChangePlan) -> dict[str, object]:
    if not isinstance(plan, ChangePlan):
        raise TypeError("plan must be a ChangePlan")
    return {
        "schemaVersion": plan.schema_version,
        "mode": plan.mode.value,
        "target": {
            "store": plan.target.store,
            "providerId": plan.target.provider_id,
        },
        "storeFingerprint": plan.store_fingerprint,
        "changes": [
            {
                "field": change.field,
                "old": _serialized_change_value(change.old),
                "new": _serialized_change_value(change.new),
            }
            for change in plan.changes
        ],
        "unchanged": {
            field_name: "unchanged"
            for field_name in UNCHANGED_FIELD_NAMES
        },
    }


def canonical_change_plan_json(plan: ChangePlan) -> str:
    """Return the byte-stable UTF-8 JSON representation of ``plan``."""

    return json.dumps(
        _canonical_payload(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def change_plan_digest(plan: ChangePlan) -> str:
    """Return the SHA-256 digest of the canonical UTF-8 JSON bytes."""

    payload = canonical_change_plan_json(plan).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def json_preview(plan: ChangePlan) -> str:
    """Narrow JSON adapter: the canonical, already-redacted plan only."""

    return canonical_change_plan_json(plan)


def tui_preview(plan: ChangePlan) -> str:
    """Narrow deterministic text adapter for a review-only TUI surface."""

    if not isinstance(plan, ChangePlan):
        raise TypeError("plan must be a ChangePlan")
    lines = [
        f"Change plan v{plan.schema_version}",
        f"Mode: {plan.mode.value}",
        (
            "Target: "
            f"{plan.target.store}/{plan.target.provider_id}"
        ),
        f"Store fingerprint: {plan.store_fingerprint}",
    ]
    for change in plan.changes:
        old_value = json.dumps(
            _serialized_change_value(change.old),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        new_value = json.dumps(
            _serialized_change_value(change.new),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        lines.append(f"{change.field}: {old_value} -> {new_value}")
    lines.extend(
        f"{field_name}: unchanged"
        for field_name in UNCHANGED_FIELD_NAMES
    )
    lines.append(f"Digest: {plan.digest}")
    return "\n".join(lines)


# Descriptive alias for callers that prefer the longer type name.
ChangePlanTarget = PlanTarget


__all__ = [
    "ALLOWED_CHANGE_FIELDS",
    "COMPANION_STORE_ID",
    "ChangePlan",
    "ChangePlanError",
    "ChangePlanTarget",
    "EmptyChangePlanError",
    "FieldChange",
    "InvalidChangePlanError",
    "MODEL_CHANGE_FIELDS",
    "PLAN_SCHEMA_VERSION",
    "PURPOSE_TAGS_FIELD",
    "PlanTarget",
    "STANDALONE_STORE_ID",
    "UNCHANGED_FIELD_NAMES",
    "build_change_plan",
    "canonical_change_plan_json",
    "change_plan_digest",
    "json_preview",
    "tui_preview",
]
