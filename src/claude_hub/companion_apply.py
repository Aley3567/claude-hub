"""Approval-gated, fail-closed writes to inactive CC Switch providers.

The service in this module is intentionally narrow.  It changes only the
``models.*`` fields named by an immutable :class:`ChangePlan`, and it never
returns a database path, backup path, raw provider document, URL, or
credential.  Human approval is consumed exclusively by
:class:`CompanionPreflight` before any writer or backup is created.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .approval import ApprovalHandle, ApprovalRegistry
from .ccswitch import (
    CCSwitchProviderStore,
    MAX_SETTINGS_CONFIG_BYTES,
    WRITE_SCHEMA_VERSIONS,
)
from .change_plan import ChangePlan, MODEL_CHANGE_FIELDS
from .claude_models import (
    CLAUDE_MODEL_FIELD_ALIASES,
    CLAUDE_MODEL_FIELDS,
    ClaudeModelAdapter,
    ClaudeModelDocumentError,
)
from .companion_preflight import (
    CCSwitchProcessDetector,
    CompanionPreflight,
)
from .domain import ProviderInspection, ProviderRef, StoreCapability
from .store import ProviderStore


DEFAULT_BACKUP_TIMEOUT_SECONDS = 5.0
BACKUP_PAGES_PER_STEP = 16
_BACKUP_DIRECTORY_PREFIX = ".claude-hub-companion-"
_BACKUP_FILENAME = "cc-switch.db"
_BACKUP_DIRECTORY_ATTEMPTS = 16
_BACKUP_COPY_CHUNK_BYTES = 128 * 1024
_CLAUDE_APP_TYPE = "claude"
_MODEL_FIELD_BY_SLOT = dict(CLAUDE_MODEL_FIELDS)
_SLOT_BY_CHANGE_FIELD = {
    f"models.{slot}": slot for slot, _field in CLAUDE_MODEL_FIELDS
}
_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "providers": frozenset(
        {
            "id",
            "name",
            "settings_config",
            "app_type",
            "sort_index",
            "is_current",
        }
    ),
    "proxy_config": frozenset(
        {
            "app_type",
            "live_takeover_active",
        }
    ),
    "proxy_live_backup": frozenset(
        {
            "app_type",
            "original_config",
        }
    ),
}


class CompanionApplyStatus(str, Enum):
    """Public, redacted outcomes for one Companion apply attempt."""

    APPLIED = "applied"
    CONFLICT = "conflict"
    BACKUP_FAILED = "backup_failed"
    WRITE_FAILED = "write_failed"
    READBACK_FAILED = "readback_failed"
    COMMIT_STATE_UNKNOWN = "commit_state_unknown"

    # Descriptive aliases retained for callers that name the failing phase.
    APPLY_FAILED = "write_failed"
    TRANSACTION_READBACK_FAILED = "readback_failed"


_STATUS_MESSAGES = {
    CompanionApplyStatus.CONFLICT: (
        "The approved Companion plan conflicts with current storage state"
    ),
    CompanionApplyStatus.BACKUP_FAILED: (
        "The Companion backup could not be completed safely"
    ),
    CompanionApplyStatus.WRITE_FAILED: (
        "The Companion update could not be completed safely"
    ),
    CompanionApplyStatus.READBACK_FAILED: (
        "The Companion transaction readback could not be verified"
    ),
    CompanionApplyStatus.COMMIT_STATE_UNKNOWN: (
        "The Companion commit state is unknown"
    ),
}
_STATUS_GUIDANCE = {
    CompanionApplyStatus.CONFLICT: (
        "Refresh the target and create and approve a new plan."
    ),
    CompanionApplyStatus.BACKUP_FAILED: (
        "Resolve backup storage access, then create and approve a new plan."
    ),
    CompanionApplyStatus.WRITE_FAILED: (
        "Inspect CC Switch storage before creating and approving a new plan."
    ),
    CompanionApplyStatus.READBACK_FAILED: (
        "Inspect CC Switch storage before creating and approving a new plan."
    ),
    CompanionApplyStatus.COMMIT_STATE_UNKNOWN: (
        "Do not retry or restore automatically; inspect the provider and "
        "retained backup first."
    ),
}


class CompanionApplyError(RuntimeError):
    """Fixed, non-reflective apply failure safe for presentation."""

    __slots__ = ("status", "fields")

    def __init__(
        self,
        status: CompanionApplyStatus,
        *,
        fields: tuple[str, ...] = (),
    ) -> None:
        if (
            not isinstance(status, CompanionApplyStatus)
            or status is CompanionApplyStatus.APPLIED
            or status not in _STATUS_MESSAGES
        ):
            raise ValueError("Companion apply error status is invalid")
        normalized_fields = _normalize_public_fields(fields)
        self.status = status
        self.fields = normalized_fields
        RuntimeError.__init__(self, _STATUS_MESSAGES[status])

    @property
    def allowed(self) -> bool:
        return False

    @property
    def guidance(self) -> str:
        return _STATUS_GUIDANCE[self.status]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "fields": list(self.fields),
            "guidance": self.guidance,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status.value!r}, "
            f"fields={self.fields!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CompanionApplyResult:
    """Secret-free proof that one planned write passed both readbacks."""

    status: CompanionApplyStatus
    fields: tuple[str, ...]
    before_fingerprint: str
    after_fingerprint: str

    def __post_init__(self) -> None:
        if self.status is not CompanionApplyStatus.APPLIED:
            raise ValueError("Companion apply result status is invalid")
        object.__setattr__(
            self,
            "fields",
            _normalize_public_fields(self.fields),
        )
        for value in (
            self.before_fingerprint,
            self.after_fingerprint,
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("Companion apply fingerprint is invalid")

    @property
    def allowed(self) -> bool:
        return True

    @property
    def backup_created(self) -> bool:
        return True

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return self.fields

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "fields": list(self.fields),
            "beforeFingerprint": self.before_fingerprint,
            "afterFingerprint": self.after_fingerprint,
            "backupCreated": True,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status.value!r}, "
            f"fields={self.fields!r}, fingerprints=<redacted>, "
            "backup_created=True)"
        )


class _ApplyAbort(RuntimeError):
    """Internal control flow carrying only a public fixed status."""

    __slots__ = ("status",)

    def __init__(self, status: CompanionApplyStatus) -> None:
        self.status = status
        RuntimeError.__init__(self, status.value)


class _BackupDeadline(RuntimeError):
    """Internal fixed signal raised by the SQLite backup progress callback."""


@dataclass(frozen=True, slots=True, repr=False)
class _BackupArtifact:
    path: Path


@dataclass(slots=True, repr=False)
class _BackupLeafOwner:
    descriptor: int | None
    identity: tuple[int, int]
    linked: bool = True


@dataclass(slots=True, repr=False)
class _BackupWorkspace:
    path: Path
    name: str
    identity: tuple[int, int]
    root_path: Path
    root_identity: tuple[int, int]
    root_fd: int | None = None
    directory_fd: int | None = None
    backup_owner: _BackupLeafOwner | None = None


@dataclass(slots=True, repr=False)
class _StagingWorkspace:
    directory: Path
    directory_identity: tuple[int, int]
    database: Path
    database_owner: _BackupLeafOwner | None


@dataclass(frozen=True, slots=True, repr=False)
class _SourceFileSet:
    main: tuple[int, int]
    wal: tuple[int, int] | None
    shm: tuple[int, int] | None


@dataclass(frozen=True, slots=True, repr=False)
class _TransactionState:
    raw_settings: str
    document: dict[str, object]


def _normalize_public_fields(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError("Companion apply fields must be a tuple")
    if (
        not values
        or any(
            not isinstance(value, str)
            or value not in MODEL_CHANGE_FIELDS
            for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError("Companion apply fields are invalid")
    return values


def _settings_fingerprint(raw_settings: str) -> str:
    return hashlib.sha256(raw_settings.encode("utf-8")).hexdigest()


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_json(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _decode_settings(raw_settings: object) -> dict[str, object]:
    if not isinstance(raw_settings, str):
        raise ValueError("provider configuration is invalid")
    encoded = raw_settings.encode("utf-8")
    if (
        len(raw_settings) > MAX_SETTINGS_CONFIG_BYTES
        or len(encoded) > MAX_SETTINGS_CONFIG_BYTES
    ):
        raise ValueError("provider configuration is invalid")
    document = json.loads(
        raw_settings,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_non_finite_json,
    )
    if not isinstance(document, dict):
        raise ValueError("provider configuration is invalid")
    return document


def _encode_settings(document: dict[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[str]:
    return frozenset(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
        if len(row) > 1
    )


def _require_write_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if (
        version_row is None
        or len(version_row) != 1
        or type(version_row[0]) is not int
        or version_row[0] not in WRITE_SCHEMA_VERSIONS
    ):
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
    table_types = {
        str(row[1]): str(row[2])
        for row in connection.execute("PRAGMA table_list")
        if len(row) > 2
    }
    for table, required_columns in _REQUIRED_COLUMNS.items():
        if (
            table_types.get(table) != "table"
            or not required_columns.issubset(
                _table_columns(connection, table)
            )
        ):
            raise _ApplyAbort(CompanionApplyStatus.CONFLICT)


def _read_target_and_proxy(
    connection: sqlite3.Connection,
    provider_id: str,
) -> tuple[tuple[object, object], tuple[object]]:
    target_rows = connection.execute(
        "SELECT settings_config, is_current FROM providers "
        "WHERE id=? AND app_type='claude' LIMIT 2",
        (provider_id,),
    ).fetchall()
    proxy_rows = connection.execute(
        "SELECT live_takeover_active FROM proxy_config "
        "WHERE app_type='claude' LIMIT 2"
    ).fetchall()
    if len(target_rows) != 1 or len(proxy_rows) != 1:
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
    target_row = target_rows[0]
    proxy_row = proxy_rows[0]
    if (
        len(target_row) != 2
        or len(proxy_row) != 1
        or type(target_row[1]) is not int
        or target_row[1] != 0
        or type(proxy_row[0]) is not int
        or proxy_row[0] != 0
    ):
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
    return target_row, proxy_row


def _require_old_values(
    document: dict[str, object],
    plan: ChangePlan,
) -> None:
    try:
        models = ClaudeModelAdapter().project(document)
    except (ClaudeModelDocumentError, TypeError, ValueError):
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT) from None
    for change in plan.changes:
        slot = _SLOT_BY_CHANGE_FIELD.get(change.field)
        if slot is None or getattr(models, slot) != change.old:
            raise _ApplyAbort(CompanionApplyStatus.CONFLICT)


def _transaction_state(
    connection: sqlite3.Connection,
    plan: ChangePlan,
) -> _TransactionState:
    _require_write_schema(connection)
    target_row, _proxy_row = _read_target_and_proxy(
        connection,
        plan.target.provider_id,
    )
    raw_settings = target_row[0]
    try:
        document = _decode_settings(raw_settings)
    except (
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT) from None
    if _settings_fingerprint(raw_settings) != plan.store_fingerprint:
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
    _require_old_values(document, plan)
    return _TransactionState(
        raw_settings=raw_settings,
        document=document,
    )


def _candidate_fields(slot: str) -> tuple[str, ...]:
    canonical = _MODEL_FIELD_BY_SLOT[slot]
    return (canonical, *CLAUDE_MODEL_FIELD_ALIASES.get(slot, ()))


def _patched_settings(
    state: _TransactionState,
    plan: ChangePlan,
) -> str:
    document = state.document
    env_value = document.get("env")
    if env_value is None:
        env: dict[str, object] = {}
        document["env"] = env
    elif isinstance(env_value, dict):
        env = env_value
    else:
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT)

    operations: list[tuple[str, object]] = []
    for change in plan.changes:
        slot = _SLOT_BY_CHANGE_FIELD.get(change.field)
        if slot is None:
            raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
        candidates = _candidate_fields(slot)
        present = tuple(field for field in candidates if field in env)
        if len(present) > 1:
            # Any shadowed canonical/alias pair is ambiguous.  Updating only
            # one representation would preserve a stale value for consumers
            # with different precedence, while deletion could reveal it.
            raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
        if not present:
            if change.old is not None or change.new is None:
                raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
            target = candidates[0]
        else:
            target = present[0]
            if env[target] != change.old:
                raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
        operations.append((target, change.new))

    for field, value in operations:
        if value is None:
            del env[field]
        else:
            env[field] = value

    try:
        projected = ClaudeModelAdapter().project(document)
        for change in plan.changes:
            slot = _SLOT_BY_CHANGE_FIELD[change.field]
            if getattr(projected, slot) != change.new:
                raise ValueError
        encoded = _encode_settings(document)
        if len(encoded.encode("utf-8")) > MAX_SETTINGS_CONFIG_BYTES:
            raise ValueError
    except (
        ClaudeModelDocumentError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise _ApplyAbort(CompanionApplyStatus.CONFLICT) from None
    return encoded


def _regular_file_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("database source is invalid")
    return metadata.st_dev, metadata.st_ino


def _optional_source_sidecar_identity(
    path: Path,
) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("database sidecar is invalid")
    return metadata.st_dev, metadata.st_ino


def _capture_source_file_set(path: Path) -> _SourceFileSet:
    """Inspect source identities without opening a SQLite lock-domain file."""

    journal = path.with_name(path.name + "-journal")
    if _optional_source_sidecar_identity(journal) is not None:
        # Before the planned UPDATE no rollback journal is legitimate.  Its
        # existence means recovery or another writer may own source state.
        raise ValueError("database rollback state is invalid")
    return _SourceFileSet(
        main=_regular_file_identity(path),
        wal=_optional_source_sidecar_identity(
            path.with_name(path.name + "-wal")
        ),
        shm=_optional_source_sidecar_identity(
            path.with_name(path.name + "-shm")
        ),
    )


def _require_source_file_set(
    path: Path,
    expected: _SourceFileSet,
) -> None:
    if _capture_source_file_set(path) != expected:
        raise ValueError("database source changed")


def _require_writer_open_transition(
    before: _SourceFileSet,
    after: _SourceFileSet,
) -> None:
    """Allow only SQLite's normal WAL/shm appearance during writer open."""

    if before.main != after.main:
        raise ValueError("database source changed")
    for old_identity, new_identity in (
        (before.wal, after.wal),
        (before.shm, after.shm),
    ):
        if (
            old_identity is not None
            and new_identity != old_identity
        ):
            raise ValueError("database source changed")


def _sqlite_uri(path: Path, mode: str) -> str:
    resolved = path.resolve(strict=True)
    return f"{resolved.as_uri()}?mode={mode}"


def _open_writer(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        _sqlite_uri(path, "rw"),
        uri=True,
        isolation_level=None,
        timeout=0.0,
    )


def _open_backup_source(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        _sqlite_uri(path, "ro"),
        uri=True,
        isolation_level=None,
        timeout=0.0,
    )


def _open_backup_destination(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        _sqlite_uri(path, "rw"),
        uri=True,
        isolation_level=None,
        timeout=0.0,
    )


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    return (
        not _strict_posix_permissions()
        or metadata.st_uid == os.getuid()
    )


def _strict_posix_permissions() -> bool:
    return os.name == "posix"


def _require_backup_leaf_metadata(
    metadata: os.stat_result,
    expected_identity: tuple[int, int],
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (
            _strict_posix_permissions()
            and stat.S_IMODE(metadata.st_mode) != 0o600
        )
        or metadata.st_nlink != 1
        or not _owned_by_current_user(metadata)
        or (metadata.st_dev, metadata.st_ino) != expected_identity
    ):
        raise OSError("backup file is invalid")


def _require_directory_metadata(
    metadata: os.stat_result,
    expected_identity: tuple[int, int],
    *,
    private: bool,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (
            private
            and _strict_posix_permissions()
            and stat.S_IMODE(metadata.st_mode) != 0o700
        )
        or (metadata.st_dev, metadata.st_ino) != expected_identity
        or (
            private
            and not _owned_by_current_user(metadata)
        )
    ):
        raise OSError("backup directory is invalid")


def _close_backup_leaf_descriptor(descriptor: int) -> None:
    os.close(descriptor)


def _linked_backup_leaf_metadata(
    path: Path,
    *,
    directory_fd: int | None,
    name: str,
) -> os.stat_result:
    if directory_fd is not None:
        return os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    return path.lstat()


def _require_owned_backup_leaf_linked(
    owner: _BackupLeafOwner | None,
    path: Path,
    *,
    directory_fd: int | None = None,
    name: str = _BACKUP_FILENAME,
) -> _BackupLeafOwner:
    if owner is None or owner.descriptor is None or not owner.linked:
        raise OSError("backup file is invalid")
    _require_backup_leaf_metadata(
        os.fstat(owner.descriptor),
        owner.identity,
    )
    linked = _linked_backup_leaf_metadata(
        path,
        directory_fd=directory_fd,
        name=name,
    )
    _require_backup_leaf_metadata(linked, owner.identity)
    return owner


def _unlink_owned_backup_leaf(
    owner: _BackupLeafOwner,
    path: Path,
    *,
    directory_fd: int | None = None,
    name: str = _BACKUP_FILENAME,
) -> bool:
    """Try one identity-bound unlink while the inode remains pinned."""

    if not owner.linked:
        return True
    unlinked = False
    try:
        if owner.descriptor is None:
            return False
        _require_backup_leaf_metadata(
            os.fstat(owner.descriptor),
            owner.identity,
        )
        linked = _linked_backup_leaf_metadata(
            path,
            directory_fd=directory_fd,
            name=name,
        )
        _require_backup_leaf_metadata(linked, owner.identity)
        if directory_fd is not None:
            os.unlink(name, dir_fd=directory_fd)
        else:
            path.unlink()
        unlinked = True
    except BaseException:
        pass
    finally:
        # An unlink attempt consumes path ownership even when validation or
        # unlink failed.  Once the pin is closed, an inode number can be
        # reused, so no later cleanup may retry by stale identity.
        owner.linked = False
    return unlinked


def _close_backup_leaf_owner(owner: _BackupLeafOwner) -> bool:
    """Move out and close the owned descriptor exactly once."""

    descriptor = owner.descriptor
    owner.descriptor = None
    if descriptor is None:
        return True
    try:
        _close_backup_leaf_descriptor(descriptor)
    except BaseException:
        return False
    return True


def _cleanup_backup_leaf_owner(
    owner: _BackupLeafOwner,
    path: Path,
    *,
    directory_fd: int | None = None,
    name: str = _BACKUP_FILENAME,
) -> bool:
    unlinked = _unlink_owned_backup_leaf(
        owner,
        path,
        directory_fd=directory_fd,
        name=name,
    )
    closed = _close_backup_leaf_owner(owner)
    return unlinked and closed


def _release_backup_leaf_owner(owner: _BackupLeafOwner) -> bool:
    """Leave the linked backup in place and relinquish cleanup ownership."""

    owner.linked = False
    return _close_backup_leaf_owner(owner)


def _secure_create_file(path: Path) -> tuple[int, int]:
    """Create and validate one private leaf, then release its inode pin."""

    opened = _secure_open_file(path)
    identity = opened.identity
    if not _release_backup_leaf_owner(opened):
        raise OSError("backup file is invalid")
    return identity


def _secure_open_file(path: Path) -> _BackupLeafOwner:
    """Create one private leaf while returning its still-open descriptor."""

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    identity: tuple[int, int] | None = None
    owner: _BackupLeafOwner | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        if _strict_posix_permissions():
            os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        owner = _BackupLeafOwner(
            descriptor=descriptor,
            identity=identity,
        )
        _require_backup_leaf_metadata(opened, identity)
        _require_backup_leaf_metadata(path.lstat(), identity)
        return owner
    except BaseException:
        if descriptor is not None and identity is None:
            try:
                opened = os.fstat(descriptor)
                identity = (opened.st_dev, opened.st_ino)
            except BaseException:
                pass
        if owner is None and descriptor is not None and identity is not None:
            owner = _BackupLeafOwner(
                descriptor=descriptor,
                identity=identity,
            )
        if owner is not None:
            _cleanup_backup_leaf_owner(owner, path)
        elif descriptor is not None:
            try:
                _close_backup_leaf_descriptor(descriptor)
            except BaseException:
                pass
        raise OSError("backup file is invalid")


def _secure_open_file_at(
    directory_fd: int,
    name: str,
) -> _BackupLeafOwner:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    identity: tuple[int, int] | None = None
    owner: _BackupLeafOwner | None = None
    try:
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        owner = _BackupLeafOwner(
            descriptor=descriptor,
            identity=identity,
        )
        _require_backup_leaf_metadata(opened, identity)
        metadata = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        _require_backup_leaf_metadata(metadata, identity)
        return owner
    except BaseException:
        if descriptor is not None and identity is None:
            try:
                opened = os.fstat(descriptor)
                identity = (opened.st_dev, opened.st_ino)
            except BaseException:
                pass
        if owner is None and descriptor is not None and identity is not None:
            owner = _BackupLeafOwner(
                descriptor=descriptor,
                identity=identity,
            )
        if owner is not None:
            _cleanup_backup_leaf_owner(
                owner,
                Path(name),
                directory_fd=directory_fd,
                name=name,
            )
        elif descriptor is not None:
            try:
                _close_backup_leaf_descriptor(descriptor)
            except BaseException:
                pass
        raise OSError("backup file is invalid")


def _workspace_path_intact(workspace: _BackupWorkspace) -> None:
    root_metadata = workspace.root_path.lstat()
    _require_directory_metadata(
        root_metadata,
        workspace.root_identity,
        private=False,
    )
    operation_metadata = workspace.path.lstat()
    _require_directory_metadata(
        operation_metadata,
        workspace.identity,
        private=True,
    )
    if workspace.root_fd is not None:
        _require_directory_metadata(
            os.fstat(workspace.root_fd),
            workspace.root_identity,
            private=False,
        )
    if workspace.directory_fd is not None:
        _require_directory_metadata(
            os.fstat(workspace.directory_fd),
            workspace.identity,
            private=True,
        )


def _workspace_entries(workspace: _BackupWorkspace) -> frozenset[str]:
    if workspace.directory_fd is not None:
        return frozenset(os.listdir(workspace.directory_fd))
    return frozenset(entry.name for entry in workspace.path.iterdir())


def _close_workspace_directory_fds(
    workspace: _BackupWorkspace,
) -> bool:
    for attribute in ("directory_fd", "root_fd"):
        descriptor = getattr(workspace, attribute)
        setattr(workspace, attribute, None)
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except BaseException:
            return False
    return True


def _cleanup_backup_workspace(
    workspace: _BackupWorkspace | None,
) -> None:
    """Delete only the exact main leaf and operation directory we created."""

    if workspace is None:
        return
    _unlink_workspace_backup(workspace)

    directory_fd = workspace.directory_fd
    workspace.directory_fd = None
    if directory_fd is not None:
        try:
            os.close(directory_fd)
        except BaseException:
            pass

    root_fd = workspace.root_fd
    workspace.root_fd = None
    if root_fd is not None:
        try:
            metadata = os.stat(
                workspace.name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if (
                stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino)
                == workspace.identity
            ):
                # rmdir succeeds only when no unknown/replaced entry remains.
                # Sidecar names are never treated as ownership.
                os.rmdir(workspace.name, dir_fd=root_fd)
        except BaseException:
            pass
        try:
            os.close(root_fd)
        except BaseException:
            pass
        return

    try:
        directory_metadata = workspace.path.lstat()
        _require_directory_metadata(
            directory_metadata,
            workspace.identity,
            private=True,
        )
    except BaseException:
        return
    try:
        workspace.path.rmdir()
    except BaseException:
        pass


def _unlink_workspace_backup(workspace: _BackupWorkspace) -> None:
    owner = workspace.backup_owner
    workspace.backup_owner = None
    if owner is None:
        return
    _cleanup_backup_leaf_owner(
        owner,
        workspace.path / _BACKUP_FILENAME,
        directory_fd=workspace.directory_fd,
        name=_BACKUP_FILENAME,
    )


def _create_backup_directory(root: Path) -> _BackupWorkspace:
    if _strict_posix_permissions():
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        root_fd: int | None = None
        directory_fd: int | None = None
        name: str | None = None
        identity: tuple[int, int] | None = None
        workspace: _BackupWorkspace | None = None
        try:
            root_fd = os.open(root, flags)
            root_metadata = os.fstat(root_fd)
            root_identity = (
                root_metadata.st_dev,
                root_metadata.st_ino,
            )
            _require_directory_metadata(
                root_metadata,
                root_identity,
                private=False,
            )
            _require_directory_metadata(
                root.lstat(),
                root_identity,
                private=False,
            )
            for _attempt in range(_BACKUP_DIRECTORY_ATTEMPTS):
                candidate = (
                    _BACKUP_DIRECTORY_PREFIX
                    + secrets.token_hex(16)
                )
                try:
                    os.mkdir(
                        candidate,
                        0o700,
                        dir_fd=root_fd,
                    )
                except FileExistsError:
                    continue
                name = candidate
                break
            if name is None:
                raise OSError("backup directory is unavailable")
            created = os.stat(
                name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            identity = (created.st_dev, created.st_ino)
            directory_fd = os.open(
                name,
                flags,
                dir_fd=root_fd,
            )
            os.fchmod(directory_fd, 0o700)
            _require_directory_metadata(
                os.fstat(directory_fd),
                identity,
                private=True,
            )
            workspace = _BackupWorkspace(
                path=root / name,
                name=name,
                identity=identity,
                root_path=root,
                root_identity=root_identity,
                root_fd=root_fd,
                directory_fd=directory_fd,
            )
            _workspace_path_intact(workspace)
            return workspace
        except BaseException:
            if workspace is not None:
                _cleanup_backup_workspace(workspace)
            else:
                if directory_fd is not None:
                    try:
                        os.close(directory_fd)
                    except BaseException:
                        pass
                if root_fd is not None and name is not None:
                    try:
                        metadata = os.stat(
                            name,
                            dir_fd=root_fd,
                            follow_symlinks=False,
                        )
                        if (
                            identity is not None
                            and stat.S_ISDIR(metadata.st_mode)
                            and (metadata.st_dev, metadata.st_ino)
                            == identity
                        ):
                            os.rmdir(name, dir_fd=root_fd)
                    except BaseException:
                        pass
                if root_fd is not None:
                    try:
                        os.close(root_fd)
                    except BaseException:
                        pass
            raise OSError("backup directory is invalid")

    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OSError("backup root is invalid")
    root_identity = (metadata.st_dev, metadata.st_ino)
    raw_directory = tempfile.mkdtemp(
        prefix=_BACKUP_DIRECTORY_PREFIX,
        dir=root,
    )
    directory = Path(raw_directory)
    workspace: _BackupWorkspace | None = None
    try:
        created = directory.lstat()
        identity = (created.st_dev, created.st_ino)
        workspace = _BackupWorkspace(
            path=directory,
            name=directory.name,
            identity=identity,
            root_path=root,
            root_identity=root_identity,
        )
        _require_directory_metadata(
            created,
            identity,
            private=True,
        )
        _workspace_path_intact(workspace)
        return workspace
    except BaseException:
        _cleanup_backup_workspace(workspace)
        raise OSError("backup directory is invalid")


def _create_staging_workspace() -> _StagingWorkspace:
    directory = Path(
        tempfile.mkdtemp(prefix="claude-hub-companion-stage-")
    )
    directory_identity: tuple[int, int] | None = None
    database_owner: _BackupLeafOwner | None = None
    database = directory / _BACKUP_FILENAME
    try:
        if _strict_posix_permissions():
            directory.chmod(0o700)
        metadata = directory.lstat()
        directory_identity = (metadata.st_dev, metadata.st_ino)
        _require_directory_metadata(
            metadata,
            directory_identity,
            private=True,
        )
        database_owner = _secure_open_file(database)
        return _StagingWorkspace(
            directory=directory,
            directory_identity=directory_identity,
            database=database,
            database_owner=database_owner,
        )
    except BaseException:
        if database_owner is not None:
            _cleanup_backup_leaf_owner(
                database_owner,
                database,
            )
        try:
            metadata = directory.lstat()
            if (
                directory_identity is not None
                and stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino)
                == directory_identity
            ):
                directory.rmdir()
        except BaseException:
            pass
        raise OSError("backup staging is invalid")


def _cleanup_staging_workspace(
    staging: _StagingWorkspace | None,
) -> bool:
    if staging is None:
        return True
    owner = staging.database_owner
    staging.database_owner = None
    leaf_cleaned = (
        True
        if owner is None
        else _cleanup_backup_leaf_owner(
            owner,
            staging.database,
        )
    )
    try:
        metadata = staging.directory.lstat()
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino)
            == staging.directory_identity
        ):
            staging.directory.rmdir()
    except FileNotFoundError:
        return leaf_cleaned
    except BaseException:
        return False
    return leaf_cleaned and not staging.directory.exists()


def _validate_backup_snapshot(
    destination: sqlite3.Connection,
    plan: ChangePlan,
    expected_raw_settings: str,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    def progress() -> int:
        return 1 if monotonic() >= deadline else 0

    destination.set_progress_handler(progress, 100)
    try:
        if monotonic() >= deadline:
            raise _BackupDeadline("backup deadline exceeded")
        _require_write_schema(destination)
        target_row, _proxy_row = _read_target_and_proxy(
            destination,
            plan.target.provider_id,
        )
        if target_row[0] != expected_raw_settings:
            raise _ApplyAbort(CompanionApplyStatus.BACKUP_FAILED)
        quick_check = destination.execute("PRAGMA quick_check").fetchall()
        if monotonic() >= deadline:
            raise _BackupDeadline("backup deadline exceeded")
        if quick_check != [("ok",)]:
            raise _ApplyAbort(CompanionApplyStatus.BACKUP_FAILED)
    finally:
        destination.set_progress_handler(None, 0)


def _fsync_backup_leaf(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    descriptor: int,
    directory_fd: int | None = None,
) -> None:
    """Durably flush the still-pinned regular backup inode."""

    failed = False
    try:
        _require_backup_leaf_metadata(
            os.fstat(descriptor),
            expected_identity,
        )
        linked = (
            _linked_backup_leaf_metadata(
                path,
                directory_fd=directory_fd,
                name=_BACKUP_FILENAME,
            )
        )
        _require_backup_leaf_metadata(linked, expected_identity)
        os.fsync(descriptor)
        _require_backup_leaf_metadata(
            os.fstat(descriptor),
            expected_identity,
        )
        linked = (
            _linked_backup_leaf_metadata(
                path,
                directory_fd=directory_fd,
                name=_BACKUP_FILENAME,
            )
        )
        _require_backup_leaf_metadata(linked, expected_identity)
    except BaseException:
        failed = True
    if failed:
        raise OSError("backup durability could not be verified")


def _fsync_directory(
    path: Path,
    expected_identity: tuple[int, int],
    *,
    private: bool,
    descriptor: int | None = None,
) -> None:
    """Durably flush one identity-bound directory on POSIX."""

    if not _strict_posix_permissions():
        _require_directory_metadata(
            path.lstat(),
            expected_identity,
            private=private,
        )
        return
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    owns_descriptor = descriptor is None
    selected_descriptor = (
        os.open(path, flags)
        if descriptor is None
        else descriptor
    )
    failed = False
    try:
        _require_directory_metadata(
            os.fstat(selected_descriptor),
            expected_identity,
            private=private,
        )
        _require_directory_metadata(
            path.lstat(),
            expected_identity,
            private=private,
        )
        os.fsync(selected_descriptor)
        _require_directory_metadata(
            os.fstat(selected_descriptor),
            expected_identity,
            private=private,
        )
        _require_directory_metadata(
            path.lstat(),
            expected_identity,
            private=private,
        )
    except BaseException:
        failed = True
    finally:
        if owns_descriptor:
            try:
                os.close(selected_descriptor)
            except BaseException:
                failed = True
    if failed:
        raise OSError("backup durability could not be verified")


def _durability_barrier(workspace: _BackupWorkspace) -> None:
    backup_path = workspace.path / _BACKUP_FILENAME
    owner = _require_owned_backup_leaf_linked(
        workspace.backup_owner,
        backup_path,
        directory_fd=workspace.directory_fd,
    )
    _workspace_path_intact(workspace)
    if _workspace_entries(workspace) != {_BACKUP_FILENAME}:
        raise OSError("backup directory is invalid")
    _fsync_backup_leaf(
        backup_path,
        owner.identity,
        descriptor=owner.descriptor,
        directory_fd=workspace.directory_fd,
    )
    _workspace_path_intact(workspace)
    if _workspace_entries(workspace) != {_BACKUP_FILENAME}:
        raise OSError("backup directory is invalid")
    _fsync_directory(
        workspace.path,
        workspace.identity,
        private=True,
        descriptor=workspace.directory_fd,
    )
    _workspace_path_intact(workspace)
    if _workspace_entries(workspace) != {_BACKUP_FILENAME}:
        raise OSError("backup directory is invalid")
    _fsync_directory(
        workspace.root_path,
        workspace.root_identity,
        private=False,
        descriptor=workspace.root_fd,
    )
    _workspace_path_intact(workspace)


def _read_backup_chunk(descriptor: int) -> bytes:
    return os.read(descriptor, _BACKUP_COPY_CHUNK_BYTES)


def _write_backup_chunk(
    descriptor: int,
    value: memoryview,
) -> int:
    return os.write(descriptor, value)


def _close_backup_copy_descriptor(descriptor: int) -> None:
    os.close(descriptor)


def _copy_staging_to_workspace(
    staging: _StagingWorkspace,
    workspace: _BackupWorkspace,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> _BackupWorkspace:
    _workspace_path_intact(workspace)
    if _workspace_entries(workspace):
        raise OSError("backup directory is not empty")
    source_fd: int | None = None
    failed = False
    try:
        if workspace.directory_fd is not None:
            owner = _secure_open_file_at(
                workspace.directory_fd,
                _BACKUP_FILENAME,
            )
        else:
            owner = _secure_open_file(
                workspace.path / _BACKUP_FILENAME
            )
        workspace.backup_owner = owner
        owner = _require_owned_backup_leaf_linked(
            workspace.backup_owner,
            workspace.path / _BACKUP_FILENAME,
            directory_fd=workspace.directory_fd,
        )
        destination_fd = owner.descriptor
        _workspace_path_intact(workspace)
        if _workspace_entries(workspace) != {_BACKUP_FILENAME}:
            raise OSError("backup directory is invalid")
        _workspace_path_intact(workspace)
        owner = _require_owned_backup_leaf_linked(
            workspace.backup_owner,
            workspace.path / _BACKUP_FILENAME,
            directory_fd=workspace.directory_fd,
        )
        destination_fd = owner.descriptor

        # Both paths write through the descriptor returned by creation.
        # All path identities are checked before this source is even opened,
        # so a root replacement cannot redirect database bytes through a
        # reopened or inode-reused leaf.
        staging_owner = _require_owned_backup_leaf_linked(
            staging.database_owner,
            staging.database,
        )
        source_flags = os.O_RDONLY
        source_flags |= getattr(os, "O_BINARY", 0)
        source_flags |= getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(staging.database, source_flags)
        _require_backup_leaf_metadata(
            os.fstat(source_fd),
            staging_owner.identity,
        )
        while True:
            if monotonic() >= deadline:
                raise _BackupDeadline("backup deadline exceeded")
            chunk = _read_backup_chunk(source_fd)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                if monotonic() >= deadline:
                    raise _BackupDeadline("backup deadline exceeded")
                written = _write_backup_chunk(destination_fd, view)
                if written <= 0:
                    raise OSError("backup copy failed")
                view = view[written:]
        _workspace_path_intact(workspace)
        _require_owned_backup_leaf_linked(
            workspace.backup_owner,
            workspace.path / _BACKUP_FILENAME,
            directory_fd=workspace.directory_fd,
        )
    except BaseException:
        failed = True
    finally:
        if source_fd is not None:
            try:
                _close_backup_copy_descriptor(source_fd)
            except BaseException:
                failed = True
    if failed:
        _unlink_workspace_backup(workspace)
        raise OSError("backup copy failed")
    return workspace


def _online_backup(
    *,
    database_path: Path,
    backup_root: Path,
    plan: ChangePlan,
    expected_raw_settings: str,
    expected_source_files: _SourceFileSet,
    timeout_seconds: float,
    monotonic: Callable[[], float],
) -> _BackupArtifact:
    workspace: _BackupWorkspace | None = None
    staging: _StagingWorkspace | None = None
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    artifact: _BackupArtifact | None = None
    failed = False
    try:
        _require_source_file_set(
            database_path,
            expected_source_files,
        )
        workspace = _create_backup_directory(backup_root)
        staging = _create_staging_workspace()
        source = _open_backup_source(database_path)
        destination = _open_backup_destination(staging.database)
        deadline = monotonic() + timeout_seconds

        def progress(
            _status: int,
            _remaining: int,
            _total: int,
        ) -> None:
            if monotonic() >= deadline:
                raise _BackupDeadline("backup deadline exceeded")

        source.backup(
            destination,
            pages=BACKUP_PAGES_PER_STEP,
            progress=progress,
            sleep=0.0,
        )
        if monotonic() >= deadline:
            raise _BackupDeadline("backup deadline exceeded")
        _validate_backup_snapshot(
            destination,
            plan,
            expected_raw_settings,
            deadline=deadline,
            monotonic=monotonic,
        )
        _require_source_file_set(
            database_path,
            expected_source_files,
        )
    except BaseException:
        failed = True
    finally:
        for connection in (destination, source):
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    failed = True
    if not failed:
        try:
            _require_source_file_set(
                database_path,
                expected_source_files,
            )
        except BaseException:
            failed = True
    if not failed and workspace is not None and staging is not None:
        try:
            _require_owned_backup_leaf_linked(
                staging.database_owner,
                staging.database,
            )
            workspace = _copy_staging_to_workspace(
                staging,
                workspace,
                deadline=deadline,
                monotonic=monotonic,
            )
            if not _cleanup_staging_workspace(staging):
                raise OSError("backup staging cleanup failed")
            staging = None
            _durability_barrier(workspace)
            _require_source_file_set(
                database_path,
                expected_source_files,
            )
            _workspace_path_intact(workspace)
            if _workspace_entries(workspace) != {_BACKUP_FILENAME}:
                raise OSError("backup directory is invalid")
            _require_owned_backup_leaf_linked(
                workspace.backup_owner,
                workspace.path / _BACKUP_FILENAME,
                directory_fd=workspace.directory_fd,
            )
            if not _close_workspace_directory_fds(workspace):
                raise OSError("backup directory close failed")
            owner = workspace.backup_owner
            workspace.backup_owner = None
            if (
                owner is None
                or not _release_backup_leaf_owner(owner)
            ):
                raise OSError("backup file close failed")
            artifact = _BackupArtifact(
                path=workspace.path / _BACKUP_FILENAME
            )
        except BaseException:
            failed = True
    if staging is not None and not _cleanup_staging_workspace(staging):
        failed = True
    if failed or artifact is None:
        _cleanup_backup_workspace(workspace)
        raise _ApplyAbort(CompanionApplyStatus.BACKUP_FAILED)
    return artifact


def _write_authorizer(
    action: int,
    table_or_pragma: str | None,
    column_or_value: str | None,
    _database: str | None,
    trigger_or_view: str | None,
) -> int:
    """Allow the top-level CAS and reject every triggered write."""

    if trigger_or_view is not None:
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_UPDATE and (
        table_or_pragma != "providers"
        or column_or_value != "settings_config"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _verify_transaction_readback(
    connection: sqlite3.Connection,
    plan: ChangePlan,
    expected_raw_settings: str,
) -> None:
    _require_write_schema(connection)
    target_row, _proxy_row = _read_target_and_proxy(
        connection,
        plan.target.provider_id,
    )
    if target_row[0] != expected_raw_settings:
        raise _ApplyAbort(CompanionApplyStatus.READBACK_FAILED)
    try:
        document = _decode_settings(target_row[0])
        models = ClaudeModelAdapter().project(document)
        for change in plan.changes:
            if getattr(
                models,
                _SLOT_BY_CHANGE_FIELD[change.field],
            ) != change.new:
                raise ValueError
    except (
        ClaudeModelDocumentError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise _ApplyAbort(CompanionApplyStatus.READBACK_FAILED) from None


def _verify_fresh_readback(
    database_path: Path,
    plan: ChangePlan,
    expected_fingerprint: str,
) -> None:
    reference = ProviderRef(
        store=plan.target.store,
        provider_id=plan.target.provider_id,
    )
    inspection = CCSwitchProviderStore(database_path).inspect(reference)
    if (
        type(inspection) is not ProviderInspection
        or inspection.reference != reference
        or inspection.is_current
        or inspection.proxy_takeover
        or inspection.schema_capability is not StoreCapability.COMPATIBLE
        or inspection.fingerprint != expected_fingerprint
    ):
        raise ValueError("fresh readback is invalid")
    for change in plan.changes:
        if getattr(
            inspection.models,
            _SLOT_BY_CHANGE_FIELD[change.field],
        ) != change.new:
            raise ValueError("fresh readback is invalid")


class CompanionApplyService:
    """Perform one plan -> approval -> guarded Companion write."""

    __slots__ = (
        "_backup_root",
        "_backup_timeout_seconds",
        "_database_path",
        "_monotonic",
        "_preflight",
    )

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        backup_root: str | os.PathLike[str],
        store: ProviderStore | None = None,
        process_detector: CCSwitchProcessDetector | None = None,
        backup_timeout_seconds: float = DEFAULT_BACKUP_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            selected_database = Path(database_path)
            selected_backup_root = Path(backup_root)
        except (TypeError, ValueError):
            raise TypeError("Companion storage locations are invalid") from None
        if (
            isinstance(backup_timeout_seconds, bool)
            or not isinstance(backup_timeout_seconds, (int, float))
            or not 0 < backup_timeout_seconds <= 60
        ):
            raise ValueError("Companion backup timeout is invalid")
        if not callable(monotonic):
            raise TypeError("Companion backup clock is invalid")
        selected_store = (
            CCSwitchProviderStore(selected_database)
            if store is None
            else store
        )
        self._database_path = selected_database
        self._backup_root = selected_backup_root
        self._backup_timeout_seconds = float(backup_timeout_seconds)
        self._monotonic = monotonic
        self._preflight = CompanionPreflight(
            selected_store,
            process_detector,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(database=<redacted>, "
            "backup_root=<redacted>)"
        )

    def apply(
        self,
        *,
        plan: ChangePlan,
        approval_registry: ApprovalRegistry,
        approval_handle: ApprovalHandle,
    ) -> CompanionApplyResult:
        """Consume approval, back up, CAS update, and verify both readbacks."""

        # This is deliberately the only approval consumer in the apply path.
        # A failed attempt cannot reuse its handle.
        self._preflight.check(
            plan=plan,
            approval_registry=approval_registry,
            approval_handle=approval_handle,
        )
        fields = tuple(change.field for change in plan.changes)

        writer: sqlite3.Connection | None = None
        backup: _BackupArtifact | None = None
        before_fingerprint = plan.store_fingerprint
        after_fingerprint: str | None = None
        status: CompanionApplyStatus | None = None
        transaction_open = False
        commit_attempted = False
        stage = CompanionApplyStatus.CONFLICT
        try:
            source_before_open = _capture_source_file_set(
                self._database_path
            )
            writer = _open_writer(self._database_path)
            writer.execute("BEGIN IMMEDIATE")
            transaction_open = True
            source_in_transaction = _capture_source_file_set(
                self._database_path
            )
            _require_writer_open_transition(
                source_before_open,
                source_in_transaction,
            )

            state = _transaction_state(writer, plan)
            new_raw_settings = _patched_settings(state, plan)
            if new_raw_settings == state.raw_settings:
                raise _ApplyAbort(CompanionApplyStatus.CONFLICT)
            _require_source_file_set(
                self._database_path,
                source_in_transaction,
            )

            stage = CompanionApplyStatus.BACKUP_FAILED
            backup = _online_backup(
                database_path=self._database_path,
                backup_root=self._backup_root,
                plan=plan,
                expected_raw_settings=state.raw_settings,
                expected_source_files=source_in_transaction,
                timeout_seconds=self._backup_timeout_seconds,
                monotonic=self._monotonic,
            )

            stage = CompanionApplyStatus.CONFLICT
            _require_source_file_set(
                self._database_path,
                source_in_transaction,
            )
            stage = CompanionApplyStatus.WRITE_FAILED
            writer.set_authorizer(_write_authorizer)
            cursor = writer.execute(
                "UPDATE providers SET settings_config=? "
                "WHERE id=? AND app_type='claude' AND is_current=0 "
                "AND settings_config=?",
                (
                    new_raw_settings,
                    plan.target.provider_id,
                    state.raw_settings,
                ),
            )
            if cursor.rowcount != 1:
                raise _ApplyAbort(CompanionApplyStatus.CONFLICT)

            stage = CompanionApplyStatus.READBACK_FAILED
            _verify_transaction_readback(
                writer,
                plan,
                new_raw_settings,
            )
            after_fingerprint = _settings_fingerprint(new_raw_settings)

            stage = CompanionApplyStatus.COMMIT_STATE_UNKNOWN
            commit_attempted = True
            writer.commit()
            transaction_open = False
        except _ApplyAbort as error:
            status = error.status
        except BaseException:
            status = stage
        finally:
            if (
                writer is not None
                and transaction_open
                and not commit_attempted
            ):
                try:
                    writer.rollback()
                    transaction_open = False
                except BaseException:
                    status = CompanionApplyStatus.COMMIT_STATE_UNKNOWN
            if writer is not None:
                try:
                    writer.close()
                except BaseException:
                    status = CompanionApplyStatus.COMMIT_STATE_UNKNOWN

        if status is not None:
            raise CompanionApplyError(status, fields=fields)
        if backup is None or after_fingerprint is None:
            raise CompanionApplyError(
                CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
                fields=fields,
            )

        postcommit_failed = False
        try:
            _verify_fresh_readback(
                self._database_path,
                plan,
                after_fingerprint,
            )
        except BaseException:
            postcommit_failed = True
        if postcommit_failed:
            raise CompanionApplyError(
                CompanionApplyStatus.COMMIT_STATE_UNKNOWN,
                fields=fields,
            )

        return CompanionApplyResult(
            status=CompanionApplyStatus.APPLIED,
            fields=fields,
            before_fingerprint=before_fingerprint,
            after_fingerprint=after_fingerprint,
        )


def apply_companion_plan(
    *,
    database_path: str | os.PathLike[str],
    backup_root: str | os.PathLike[str],
    plan: ChangePlan,
    approval_registry: ApprovalRegistry,
    approval_handle: ApprovalHandle,
    store: ProviderStore | None = None,
    process_detector: CCSwitchProcessDetector | None = None,
    backup_timeout_seconds: float = DEFAULT_BACKUP_TIMEOUT_SECONDS,
) -> CompanionApplyResult:
    """Convenience entry point for one Companion apply attempt."""

    return CompanionApplyService(
        database_path,
        backup_root=backup_root,
        store=store,
        process_detector=process_detector,
        backup_timeout_seconds=backup_timeout_seconds,
    ).apply(
        plan=plan,
        approval_registry=approval_registry,
        approval_handle=approval_handle,
    )


__all__ = [
    "BACKUP_PAGES_PER_STEP",
    "DEFAULT_BACKUP_TIMEOUT_SECONDS",
    "CompanionApplyError",
    "CompanionApplyResult",
    "CompanionApplyService",
    "CompanionApplyStatus",
    "apply_companion_plan",
]
