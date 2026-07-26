"""Credential-free standalone profile metadata storage.

The store owns only routing metadata and an opaque reference to a credential
managed elsewhere.  It never accepts, resolves, or returns plaintext API keys.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .domain import ModelMapping, StandaloneProfile


APPLICATION_DIRECTORY = "claude-hub"
STORE_FILENAME = "standalone-profiles.json"
SCHEMA_VERSION = 1
MAX_STORE_BYTES = 4 * 1024 * 1024

_MODEL_FIELDS = tuple(field.name for field in fields(ModelMapping))


class StandaloneStoreError(RuntimeError):
    """Base class for sanitized standalone-store failures."""


class StandaloneStoreSecurityError(StandaloneStoreError):
    """Raised when a path, file type, owner, or mode is unsafe."""


class StandaloneStoreCorruptError(StandaloneStoreError):
    """Raised when the local document does not satisfy its declared schema."""


class UnsupportedStandaloneSchemaError(StandaloneStoreError):
    """Raised when a local schema cannot be safely interpreted."""


class StandaloneProfileNotFoundError(StandaloneStoreError):
    """Raised when a profile UUID is absent."""


class StandaloneProfileExistsError(StandaloneStoreError):
    """Raised when create would overwrite an existing profile."""


class StandaloneProfileConflictError(StandaloneStoreError):
    """Raised when update would regress immutable or monotonic metadata."""


def standalone_data_dir(
    platform: str,
    *,
    home: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the standard per-user data directory without reading process state.

    ``platform``, ``home``, and ``environment`` are explicit so callers and
    tests decide where values come from.  The function performs no filesystem
    access and never consults ``Path.home()`` or ``os.environ``.
    """

    if not isinstance(platform, str):
        raise TypeError("platform must be a string")
    if not isinstance(environment, (Mapping, type(None))):
        raise TypeError("environment must be a mapping")
    values = {} if environment is None else environment
    home_path = Path(home)
    normalized_platform = platform.casefold()

    if normalized_platform in {"darwin", "macos"}:
        base = home_path / "Library" / "Application Support"
    elif normalized_platform in {"win32", "windows"}:
        configured = values.get("LOCALAPPDATA")
        candidate = (
            Path(configured)
            if isinstance(configured, str) and configured
            else None
        )
        base = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else home_path / "AppData" / "Local"
        )
    elif normalized_platform.startswith("linux"):
        configured = values.get("XDG_DATA_HOME")
        candidate = (
            Path(configured)
            if isinstance(configured, str) and configured
            else None
        )
        base = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else home_path / ".local" / "share"
        )
    else:
        raise ValueError("platform is unsupported")
    return base / APPLICATION_DIRECTORY


def standalone_store_path(
    platform: str,
    *,
    home: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the default standalone metadata file for injected platform data."""

    return (
        standalone_data_dir(
            platform,
            home=home,
            environment=environment,
        )
        / STORE_FILENAME
    )


def _profile_key(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise TypeError("profile ID must be a UUID")
    try:
        return str(UUID(value))
    except (AttributeError, ValueError):
        raise ValueError("profile ID must be a UUID") from None


def _require_profile(value: object) -> StandaloneProfile:
    if not isinstance(value, StandaloneProfile):
        raise TypeError("profile must be a StandaloneProfile")
    if not isinstance(value.secret_ref, UUID):
        raise TypeError("profile secret reference must be a UUID")
    return value


def _format_timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise StandaloneStoreCorruptError(
            "standalone profile metadata is invalid"
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        raise StandaloneStoreCorruptError(
            "standalone profile metadata is invalid"
        ) from None


def _decode_profile(profile_key: str, raw: object) -> StandaloneProfile:
    if not isinstance(raw, dict):
        raise StandaloneStoreCorruptError(
            "standalone profile metadata is invalid"
        )
    try:
        if raw["id"] != profile_key:
            raise StandaloneStoreCorruptError(
                "standalone profile metadata is invalid"
            )
        models_raw = raw["models"]
        purpose_tags = raw["purposeTags"]
        if not isinstance(models_raw, dict) or not isinstance(purpose_tags, list):
            raise StandaloneStoreCorruptError(
                "standalone profile metadata is invalid"
            )
        model_values = {
            field_name: models_raw[field_name]
            for field_name in _MODEL_FIELDS
            if field_name in models_raw
        }
        profile = StandaloneProfile(
            profile_id=profile_key,
            name=raw["name"],
            base_url=raw["baseUrl"],
            adapter=raw["adapter"],
            models=ModelMapping(**model_values),
            purpose_tags=tuple(purpose_tags),
            secret_ref=raw["secretRef"],
            created_at=_parse_timestamp(raw["createdAt"]),
            updated_at=_parse_timestamp(raw["updatedAt"]),
        )
    except StandaloneStoreCorruptError:
        raise
    except (KeyError, TypeError, ValueError):
        raise StandaloneStoreCorruptError(
            "standalone profile metadata is invalid"
        ) from None
    if str(profile.profile_id) != profile_key:
        raise StandaloneStoreCorruptError(
            "standalone profile metadata is invalid"
        )
    return profile


def _encode_profile(
    profile: StandaloneProfile,
    *,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    encoded = dict(existing or {})
    existing_models = encoded.get("models")
    models = dict(existing_models) if isinstance(existing_models, dict) else {}
    for field_name in _MODEL_FIELDS:
        models.pop(field_name, None)
    models.update(profile.models.to_public_dict())
    encoded.update(
        {
            "id": str(profile.profile_id),
            "name": profile.name,
            "baseUrl": profile.base_url,
            "adapter": profile.adapter.value,
            "models": models,
            "purposeTags": list(profile.purpose_tags),
            "secretRef": str(profile.secret_ref),
            "createdAt": _format_timestamp(profile.created_at),
            "updatedAt": _format_timestamp(profile.updated_at),
        }
    )
    return encoded


def _empty_document() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "profiles": {}}


def _validate_document(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StandaloneStoreCorruptError("standalone store is invalid")
    version = document.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StandaloneStoreCorruptError("standalone store is invalid")
    if version != SCHEMA_VERSION:
        raise UnsupportedStandaloneSchemaError(
            "standalone store schema is unsupported"
        )
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        raise StandaloneStoreCorruptError("standalone store is invalid")
    for profile_key, raw_profile in profiles.items():
        if not isinstance(profile_key, str):
            raise StandaloneStoreCorruptError("standalone store is invalid")
        try:
            canonical_key = _profile_key(profile_key)
        except (TypeError, ValueError):
            raise StandaloneStoreCorruptError(
                "standalone store is invalid"
            ) from None
        if canonical_key != profile_key:
            raise StandaloneStoreCorruptError("standalone store is invalid")
        _decode_profile(profile_key, raw_profile)
    return document


def _reject_non_finite(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _validate_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise StandaloneStoreSecurityError(
            "standalone store path is unsafe"
        )
    if os.name == "posix":
        if file_stat.st_mode & 0o077:
            raise StandaloneStoreSecurityError(
                "standalone store permissions are unsafe"
            )
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise StandaloneStoreSecurityError(
                "standalone store owner is unsafe"
            )


def _safe_file_stat(path: Path) -> os.stat_result | None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StandaloneStoreError("standalone store inspection failed") from None
    _validate_file_stat(file_stat)
    return file_stat


def _read_bytes(path: Path) -> bytes | None:
    expected = _safe_file_stat(path)
    if expected is None:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _validate_file_stat(opened)
        if (
            opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise StandaloneStoreSecurityError(
                "standalone store changed during open"
            )
        if opened.st_size > MAX_STORE_BYTES:
            raise StandaloneStoreCorruptError("standalone store is too large")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        with handle:
            payload = handle.read(MAX_STORE_BYTES + 1)
        if len(payload) > MAX_STORE_BYTES:
            raise StandaloneStoreCorruptError("standalone store is too large")
        return payload
    except StandaloneStoreError:
        raise
    except OSError:
        raise StandaloneStoreError("standalone store read failed") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_document(path: Path) -> dict[str, Any]:
    payload = _read_bytes(path)
    if payload is None:
        return _empty_document()
    try:
        document = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_non_finite,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeError, ValueError):
        raise StandaloneStoreCorruptError("standalone store is invalid") from None
    return _validate_document(document)


def _ensure_parent_directory(parent: Path) -> None:
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = parent.lstat()
    except OSError:
        raise StandaloneStoreError(
            "standalone store directory is unavailable"
        ) from None
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise StandaloneStoreSecurityError(
            "standalone store directory is unsafe"
        )
    if os.name == "posix":
        if parent_stat.st_mode & 0o022:
            raise StandaloneStoreSecurityError(
                "standalone store directory permissions are unsafe"
            )
        if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
            raise StandaloneStoreSecurityError(
                "standalone store directory owner is unsafe"
            )


def _fsync_directory(parent: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = -1
    try:
        descriptor = os.open(parent, flags)
        os.fsync(descriptor)
    except OSError:
        # The file itself was already flushed before replace.  Some supported
        # platforms do not permit directory fsync, so this is best effort.
        return
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _serialize_document(document: dict[str, Any]) -> bytes:
    _validate_document(document)
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        raise StandaloneStoreCorruptError("standalone store is invalid") from None
    if len(payload) > MAX_STORE_BYTES:
        raise StandaloneStoreCorruptError("standalone store is too large")
    return payload


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    payload = _serialize_document(document)
    _ensure_parent_directory(path.parent)
    _safe_file_stat(path)

    descriptor = -1
    temporary_path: str | None = None
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
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    except StandaloneStoreError:
        raise
    except OSError:
        raise StandaloneStoreError("standalone store write failed") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass


class StandaloneProfileStore:
    """Versioned JSON store for credential-free standalone profile metadata."""

    __slots__ = ("_path",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def in_data_dir(
        cls,
        data_dir: str | os.PathLike[str],
    ) -> StandaloneProfileStore:
        return cls(Path(data_dir) / STORE_FILENAME)

    def create(self, profile: StandaloneProfile) -> StandaloneProfile:
        profile = _require_profile(profile)
        document = _load_document(self._path)
        profiles = document["profiles"]
        profile_key = str(profile.profile_id)
        if profile_key in profiles:
            raise StandaloneProfileExistsError("standalone profile already exists")
        profiles[profile_key] = _encode_profile(profile)
        _atomic_write(self._path, document)
        return profile

    def read(self, profile_id: UUID | str) -> StandaloneProfile:
        profile_key = _profile_key(profile_id)
        document = _load_document(self._path)
        try:
            raw_profile = document["profiles"][profile_key]
        except KeyError:
            raise StandaloneProfileNotFoundError(
                "standalone profile was not found"
            ) from None
        return _decode_profile(profile_key, raw_profile)

    def update(self, profile: StandaloneProfile) -> StandaloneProfile:
        profile = _require_profile(profile)
        document = _load_document(self._path)
        profiles = document["profiles"]
        profile_key = str(profile.profile_id)
        try:
            raw_profile = profiles[profile_key]
        except KeyError:
            raise StandaloneProfileNotFoundError(
                "standalone profile was not found"
            ) from None
        existing = _decode_profile(profile_key, raw_profile)
        if profile.created_at != existing.created_at:
            raise StandaloneProfileConflictError(
                "standalone profile creation time is immutable"
            )
        if profile.updated_at < existing.updated_at:
            raise StandaloneProfileConflictError(
                "standalone profile update time regressed"
            )
        profiles[profile_key] = _encode_profile(
            profile,
            existing=raw_profile,
        )
        _atomic_write(self._path, document)
        return profile

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path=<redacted>)"


# Concise alias for callers that do not need the profile qualifier.
StandaloneStore = StandaloneProfileStore


__all__ = [
    "APPLICATION_DIRECTORY",
    "MAX_STORE_BYTES",
    "SCHEMA_VERSION",
    "STORE_FILENAME",
    "StandaloneProfileConflictError",
    "StandaloneProfileExistsError",
    "StandaloneProfileNotFoundError",
    "StandaloneProfileStore",
    "StandaloneStore",
    "StandaloneStoreCorruptError",
    "StandaloneStoreError",
    "StandaloneStoreSecurityError",
    "UnsupportedStandaloneSchemaError",
    "standalone_data_dir",
    "standalone_store_path",
]
