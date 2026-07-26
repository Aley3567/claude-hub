"""Immutable domain values shared by every application surface.

Presentation DTOs deliberately have no credential, URL, header, raw
configuration, database-path, or exception fields.  Standalone profile
metadata is an internal persistence value: it may contain a normalized base
URL and an opaque secret reference, but never credential material, and its
``repr`` redacts all routing metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


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


class ProtocolAdapter(str, Enum):
    """Wire-protocol adapter selected by a standalone profile."""

    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"


_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:api[-_]?key|access[-_]?token|auth[-_]?token|bearer|credential|"
    r"password|passwd|private[-_]?key|secret|session[-_]?token|"
    r"authorization|cookie|header)"
)
_SECRET_VALUE_RE = re.compile(r"(?i)^(?:sk-|key-|token-)")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ABSOLUTE_PATH_RE = re.compile(r"(?:[/\\]|[A-Za-z]:[/\\])")
_MAX_BASE_URL_LENGTH = 2048
_MAX_PROFILE_NAME_LENGTH = 200
_MAX_PURPOSE_TAGS = 32
_MAX_PURPOSE_TAG_LENGTH = 64


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


def _trim_profile_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("profile name must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_PROFILE_NAME_LENGTH
        or _CONTROL_CHARACTER_RE.search(normalized)
    ):
        raise ValueError("profile name is invalid")
    return normalized


def normalize_base_url(value: object) -> str:
    """Return a stable HTTP(S) base URL without disclosing it in failures."""

    if not isinstance(value, str):
        raise TypeError("base URL must be a string")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _MAX_BASE_URL_LENGTH
        or _CONTROL_CHARACTER_RE.search(candidate)
        or any(character.isspace() for character in candidate)
    ):
        raise ValueError("base URL is invalid")
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        raise ValueError("base URL is invalid") from None
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL is invalid")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        raise ValueError("base URL is invalid") from None
    if not normalized_host:
        raise ValueError("base URL is invalid")

    host_for_netloc = (
        f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    )
    default_port = 80 if scheme == "http" else 443
    netloc = (
        host_for_netloc
        if port is None or port == default_port
        else f"{host_for_netloc}:{port}"
    )
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, normalized_path, "", ""))


def _normalize_profile_id(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("profile ID must be a UUID")
    try:
        return UUID(value)
    except (AttributeError, ValueError):
        raise ValueError("profile ID must be a UUID") from None


def _normalize_adapter(value: object) -> ProtocolAdapter:
    if isinstance(value, ProtocolAdapter):
        return value
    if not isinstance(value, str):
        raise TypeError("adapter must be a ProtocolAdapter")
    try:
        return ProtocolAdapter(value)
    except ValueError:
        raise ValueError("adapter is unsupported") from None


def _normalize_secret_reference(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("secret reference must be a UUID")
    try:
        return UUID(value)
    except (AttributeError, ValueError):
        raise ValueError("secret reference must be a UUID") from None


def _normalize_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError(f"{field_name} must be timezone-aware") from None
    if offset is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize_purpose_tags(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("purpose_tags must be an iterable of strings")
    try:
        raw_tags = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise TypeError("purpose_tags must be an iterable of strings") from None
    if len(raw_tags) > _MAX_PURPOSE_TAGS:
        raise ValueError("purpose_tags contains too many values")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str):
            raise TypeError("purpose_tags must contain only strings")
        tag = raw_tag.strip()
        if (
            not tag
            or len(tag) > _MAX_PURPOSE_TAG_LENGTH
            or _CONTROL_CHARACTER_RE.search(tag)
        ):
            raise ValueError("purpose_tags contains an invalid value")
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
    return tuple(normalized)


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


@dataclass(frozen=True, slots=True, repr=False)
class StandaloneProfile:
    """Credential-free metadata for one standalone provider profile."""

    profile_id: UUID
    name: str
    base_url: str
    adapter: ProtocolAdapter
    secret_ref: UUID
    created_at: datetime
    updated_at: datetime
    models: ModelMapping = ModelMapping()
    purpose_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _normalize_profile_id(self.profile_id),
        )
        object.__setattr__(self, "name", _trim_profile_name(self.name))
        object.__setattr__(
            self,
            "base_url",
            normalize_base_url(self.base_url),
        )
        object.__setattr__(
            self,
            "adapter",
            _normalize_adapter(self.adapter),
        )
        object.__setattr__(
            self,
            "secret_ref",
            _normalize_secret_reference(self.secret_ref),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _normalize_timestamp(self.updated_at, field_name="updated_at"),
        )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if not isinstance(self.models, ModelMapping):
            raise TypeError("models must be a ModelMapping")
        object.__setattr__(
            self,
            "purpose_tags",
            _normalize_purpose_tags(self.purpose_tags),
        )

    @property
    def protocol_adapter(self) -> ProtocolAdapter:
        """Descriptive alias used by storage and launch adapters."""

        return self.adapter

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_id=<redacted>, "
            f"adapter={self.adapter.value!r}, models={self.models!r}, "
            f"purpose_tag_count={len(self.purpose_tags)}, "
            "routing_metadata=<redacted>)"
        )


# Names kept explicit for callers that prefer the longer DTO terminology.
ProviderReference = ProviderRef
ProviderInspect = ProviderInspection


__all__ = [
    "ModelMapping",
    "ProviderInspect",
    "ProviderInspection",
    "ProviderRef",
    "ProviderReference",
    "ProtocolAdapter",
    "RuntimeMode",
    "StandaloneProfile",
    "StoreCapability",
    "normalize_base_url",
]
