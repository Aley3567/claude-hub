"""Immutable, presentation-safe values shared by every application surface.

The DTOs in this module deliberately have no credential, URL, header, raw
configuration, database-path, or exception fields.  Arbitrary public
identifiers are also validated before they can enter a DTO, and ``repr`` never
includes provider or model values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from enum import Enum


class RuntimeMode(str, Enum):
    """Top-level application modes shared by startup surfaces."""

    COMPANION = "companion"
    STANDALONE = "standalone"
    EMPTY = "empty"
    INCOMPATIBLE = "incompatible"


class StoreCapability(str, Enum):
    """Read-only result of probing a provider store."""

    ABSENT = "absent"
    READ_ONLY = "read_only"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    CORRUPT = "corrupt"

    @property
    def can_read(self) -> bool:
        return self in {StoreCapability.READ_ONLY, StoreCapability.COMPATIBLE}

    @property
    def schema_allows_write(self) -> bool:
        """Whether the schema alone permits a later guarded write.

        This is not authorization to write.  Lifecycle checks and an approved
        plan are separate requirements introduced by later tracer bullets.
        """

        return self is StoreCapability.COMPATIBLE


_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:api[-_]?key|access[-_]?token|auth[-_]?token|bearer|credential|"
    r"password|passwd|private[-_]?key|secret|session[-_]?token|"
    r"authorization|cookie|header)"
)
_SECRET_VALUE_RE = re.compile(r"(?i)^(?:sk-|key-|token-)")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ABSOLUTE_PATH_RE = re.compile(r"(?:[/\\]|[A-Za-z]:[/\\])")


def _require_public_identifier(value: object, *, field_name: str) -> str:
    """Validate a value that is allowed to cross a presentation boundary."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if len(value) > 512:
        raise ValueError(f"{field_name} is too long")
    if _CONTROL_CHARACTER_RE.search(value):
        raise ValueError(f"{field_name} contains a control character")
    if (
        "://" in value
        or _SENSITIVE_TEXT_RE.search(value)
        or _SECRET_VALUE_RE.match(value)
        or _ABSOLUTE_PATH_RE.match(value)
    ):
        raise ValueError(f"{field_name} is not a public identifier")
    return value


def _require_local_display_name(value: object) -> str:
    """Validate a private, local-only label without treating it as public."""

    if not isinstance(value, str):
        raise TypeError("display_name must be a string")
    if not value or value != value.strip():
        raise ValueError("display_name must be a non-empty trimmed string")
    if len(value) > 512:
        raise ValueError("display_name is too long")
    if _CONTROL_CHARACTER_RE.search(value):
        raise ValueError("display_name contains a control character")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ProviderRef:
    """Stable identity with an optional, explicitly local-only display label."""

    store: str
    provider_id: str
    is_current: bool = field(default=False, compare=False)
    display_name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require_public_identifier(self.store, field_name="store")
        _require_public_identifier(self.provider_id, field_name="provider_id")
        if not isinstance(self.is_current, bool):
            raise TypeError("is_current must be a bool")
        if self.display_name is not None:
            _require_local_display_name(self.display_name)

    @property
    def source(self) -> str:
        """Compatibility spelling for callers that call the store a source."""

        return self.store

    @property
    def current(self) -> bool:
        """Presentation spelling used by list surfaces."""

        return self.is_current

    def __repr__(self) -> str:
        return f"{type(self).__name__}(store={self.store!r}, provider_id=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ModelMapping:
    """Generic purpose-to-model mapping, independent of Claude JSON fields."""

    default: str | None = None
    fast: str | None = None
    reasoning: str | None = None
    coding: str | None = None
    long_context: str | None = None
    fallback: str | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None:
                _require_public_identifier(
                    value,
                    field_name=f"models.{item.name}",
                )

    @property
    def configured_slots(self) -> tuple[str, ...]:
        return tuple(
            item.name
            for item in fields(self)
            if getattr(self, item.name) is not None
        )

    def to_public_dict(self) -> dict[str, str]:
        """Return only configured, validated model identifiers."""

        return {
            item.name: value
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(slots={self.configured_slots!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInspection:
    """Read-only, redacted provider details used by CLI, TUI, and GUI."""

    reference: ProviderRef
    models: ModelMapping = ModelMapping()
    is_current: bool = False
    fingerprint: str | None = None
    proxy_takeover: bool = False
    schema_capability: StoreCapability | None = None
    unknown_field_count: int = 0
    unknown_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ProviderRef):
            raise TypeError("reference must be a ProviderRef")
        if not isinstance(self.models, ModelMapping):
            raise TypeError("models must be a ModelMapping")
        if not isinstance(self.is_current, bool):
            raise TypeError("is_current must be a bool")
        if self.reference.is_current != self.is_current:
            raise ValueError(
                "reference and inspection current markers must match"
            )
        if self.fingerprint is not None and not _SHA256_RE.fullmatch(
            self.fingerprint
        ):
            raise ValueError("fingerprint must be a SHA-256 digest")
        if not isinstance(self.proxy_takeover, bool):
            raise TypeError("proxy_takeover must be a bool")
        if self.schema_capability is not None and not isinstance(
            self.schema_capability,
            StoreCapability,
        ):
            raise TypeError("schema_capability must be a StoreCapability")
        if (
            not isinstance(self.unknown_field_count, int)
            or isinstance(self.unknown_field_count, bool)
            or self.unknown_field_count < 0
        ):
            raise TypeError("unknown_field_count must be a non-negative int")
        if (
            self.unknown_fingerprint is not None
            and not _SHA256_RE.fullmatch(self.unknown_fingerprint)
        ):
            raise ValueError("unknown_fingerprint must be a SHA-256 digest")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(reference={self.reference!r}, "
            f"models={self.models!r}, is_current={self.is_current!r}, "
            f"proxy_takeover={self.proxy_takeover!r}, "
            f"schema_capability={self.schema_capability!r}, "
            f"unknown_field_count={self.unknown_field_count!r})"
        )

    @property
    def current(self) -> bool:
        return self.is_current


# Names kept explicit for callers that prefer the longer DTO terminology.
ProviderReference = ProviderRef
ProviderInspect = ProviderInspection


__all__ = [
    "ModelMapping",
    "ProviderInspect",
    "ProviderInspection",
    "ProviderRef",
    "ProviderReference",
    "RuntimeMode",
    "StoreCapability",
]
