"""Read-only CC Switch provider-store adapter.

Database paths and raw provider records remain inside this module.  The
presentation boundary receives only validated stable references and redacted
inspection DTOs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from .claude_models import ClaudeModelAdapter, ClaudeModelDocumentError
from .domain import ProviderInspection, ProviderRef, StoreCapability
from .store import (
    ProviderConfigCorruptError,
    ProviderNotFoundError,
    ProviderStoreError,
    ProviderStoreUnavailableError,
)


CC_SWITCH_DB_ENV = "CLAUDE_HUB_CC_SWITCH_DB"
CC_SWITCH_DB_ENV_ALIASES = ("CLAUDE_HUB_CC_SWITCH_DB_PATH", "CLAUDE1_DB_PATH")
MIN_READ_SCHEMA_VERSION = 13
MAX_READ_SCHEMA_VERSION = 16
WRITE_SCHEMA_VERSIONS = frozenset({16})
CC_SWITCH_STORE_ID = "cc-switch"
MAX_SETTINGS_CONFIG_BYTES = 4 * 1024 * 1024
DB_SNAPSHOT_RETRIES = 3
DB_LOCK_PROBE_TIMEOUT_SECONDS = 2.0

_LOCK_STATUS_UNLOCKED = b"UNLOCKED\n"
_LOCK_STATUS_LOCKED = b"LOCKED\n"
_LOCK_STATUS_UNKNOWN = b"UNKNOWN\n"
_LOCK_PROBE_SCRIPT = r"""
import errno
import os
import stat
import sys

LOCKED = "LOCKED\n"
UNKNOWN = "UNKNOWN\n"
UNLOCKED = "UNLOCKED\n"
PENDING_BYTE = 0x40000000
MAIN_LOCK_BYTES = 512
WAL_WRITE_LOCK_OFFSET = 120
WAL_LOCK_BYTES = 8
MAX_PATH_BYTES = 32768

def probe_posix(path, offset, length):
    import fcntl
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        try:
            fcntl.lockf(
                descriptor,
                fcntl.LOCK_SH | fcntl.LOCK_NB,
                length,
                offset,
                os.SEEK_SET,
            )
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return LOCKED
            return UNKNOWN
        try:
            fcntl.lockf(
                descriptor,
                fcntl.LOCK_UN,
                length,
                offset,
                os.SEEK_SET,
            )
        except OSError:
            return UNKNOWN
        return UNLOCKED
    finally:
        os.close(descriptor)

def probe_windows(path, offset, length):
    import msvcrt
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBRLCK, length)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return LOCKED
            return UNKNOWN
        os.lseek(descriptor, offset, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, length)
        except OSError:
            return UNKNOWN
        return UNLOCKED
    finally:
        os.close(descriptor)

def probe(path, offset, length):
    if os.name == "posix":
        return probe_posix(path, offset, length)
    if os.name == "nt":
        return probe_windows(path, offset, length)
    return UNKNOWN

def main():
    raw_path = sys.stdin.buffer.read(MAX_PATH_BYTES + 1)
    if (
        not raw_path
        or len(raw_path) > MAX_PATH_BYTES
        or b"\0" in raw_path
    ):
        sys.stdout.write(UNKNOWN)
        return
    try:
        path = os.fsdecode(raw_path)
        statuses = [probe(path, PENDING_BYTE, MAIN_LOCK_BYTES)]
        shm_path = path + "-shm"
        try:
            metadata = os.lstat(shm_path)
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not stat.S_ISREG(metadata.st_mode):
                statuses.append(UNKNOWN)
            else:
                statuses.append(
                    probe(shm_path, WAL_WRITE_LOCK_OFFSET, WAL_LOCK_BYTES)
                )
    except Exception:
        sys.stdout.write(UNKNOWN)
        return
    if LOCKED in statuses:
        sys.stdout.write(LOCKED)
    elif UNKNOWN in statuses:
        sys.stdout.write(UNKNOWN)
    else:
        sys.stdout.write(UNLOCKED)

main()
"""

_T = TypeVar("_T")

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
    "proxy_config": frozenset({"app_type", "live_takeover_active"}),
    "proxy_live_backup": frozenset({"app_type", "original_config"}),
}


def resolve_ccswitch_database_path(
    database_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit, environment, then platform-home database candidates."""

    if database_path is not None:
        return Path(database_path).expanduser()

    environment = os.environ if environ is None else environ
    for key in (CC_SWITCH_DB_ENV, *CC_SWITCH_DB_ENV_ALIASES):
        value = environment.get(key)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".cc-switch" / "cc-switch.db"


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    # Lifecycle gates must not wait out SQLite's five-second default when
    # another owner holds the database.  A lock is uncertainty, so fail closed
    # immediately and let the caller ask the user to retry.
    connection = sqlite3.connect(uri, uri=True, timeout=0.0)
    try:
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[str]:
    # Table names are fixed module constants, never caller input.
    return frozenset(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _is_sqlite_boolean(value: object) -> bool:
    return type(value) is int and value in (0, 1)


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_json(value: str) -> object:
    raise ValueError("non-finite JSON number")


def _candidate_capability(path: Path) -> StoreCapability | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return StoreCapability.ABSENT
    except (OSError, ValueError):
        return StoreCapability.CORRUPT
    if not stat.S_ISREG(metadata.st_mode):
        return StoreCapability.INCOMPATIBLE
    if not os.access(path, os.R_OK):
        return StoreCapability.CORRUPT
    return None


class _SnapshotUnavailable(RuntimeError):
    """Internal fixed failure for an unstable or locked source database."""


def _snapshot_fingerprint(path: Path) -> tuple[int, ...] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise _SnapshotUnavailable(
            "provider snapshot source is unavailable"
        ) from None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _database_snapshot_state(
    path: Path,
) -> tuple[
    tuple[int, ...] | None,
    tuple[int, ...] | None,
    tuple[int, ...] | None,
    tuple[int, ...] | None,
]:
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    journal_path = path.with_name(path.name + "-journal")
    return (
        _snapshot_fingerprint(path),
        _snapshot_fingerprint(wal_path),
        _snapshot_fingerprint(shm_path),
        _snapshot_fingerprint(journal_path),
    )


def _require_snapshot_source(
    path: Path,
    fingerprint: tuple[int, ...] | None,
) -> None:
    if (
        fingerprint is None
        or not stat.S_ISREG(fingerprint[2])
        or not os.access(path, os.R_OK)
    ):
        raise _SnapshotUnavailable(
            "provider snapshot source is unavailable"
        )


def _source_lock_status(path: Path) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        completed = subprocess.run(
            (sys.executable, "-I", "-c", _LOCK_PROBE_SCRIPT),
            input=os.fsencode(resolved),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=DB_LOCK_PROBE_TIMEOUT_SECONDS,
            close_fds=True,
        )
    except Exception:
        return _LOCK_STATUS_UNKNOWN
    if completed.returncode != 0:
        return _LOCK_STATUS_UNKNOWN
    if completed.stdout in {
        _LOCK_STATUS_UNLOCKED,
        _LOCK_STATUS_LOCKED,
        _LOCK_STATUS_UNKNOWN,
    }:
        return completed.stdout
    return _LOCK_STATUS_UNKNOWN


def _copy_snapshot_file(source: Path, destination: Path) -> None:
    destination.touch(mode=0o600, exist_ok=False)
    destination.chmod(0o600)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def _read_stable_snapshot(
    path: Path,
    reader: Callable[[sqlite3.Connection], _T],
) -> _T:
    wal_path = path.with_name(path.name + "-wal")
    shm_path = path.with_name(path.name + "-shm")
    for _attempt in range(DB_SNAPSHOT_RETRIES):
        try:
            before = _database_snapshot_state(path)
            _require_snapshot_source(path, before[0])
            # Never open, copy, or recover a rollback journal.  Its mere
            # existence means the main database may require source mutation
            # before it can be interpreted safely.
            if before[3] is not None:
                raise _SnapshotUnavailable(
                    "provider rollback journal state is uncertain"
                )
            if before[1] is not None:
                _require_snapshot_source(wal_path, before[1])
            if before[2] is not None:
                _require_snapshot_source(shm_path, before[2])
            # This is a bounded capture protocol, not a cross-return lock:
            # main/WAL/shm/journal identity and metadata must stay fixed
            # between two full lock probes.  A later writer must still
            # re-check and CAS.
            if _source_lock_status(path) != _LOCK_STATUS_UNLOCKED:
                raise _SnapshotUnavailable(
                    "provider snapshot source is locked"
                )
            with tempfile.TemporaryDirectory(
                prefix="claude-hub-ccswitch-"
            ) as raw_directory:
                directory = Path(raw_directory)
                directory.chmod(0o700)
                snapshot = directory / "cc-switch.db"
                _copy_snapshot_file(path, snapshot)
                if before[1] is not None:
                    snapshot_wal = snapshot.with_name(
                        snapshot.name + "-wal"
                    )
                    _copy_snapshot_file(wal_path, snapshot_wal)
                after = _database_snapshot_state(path)
                if after[3] is not None:
                    raise _SnapshotUnavailable(
                        "provider rollback journal state is uncertain"
                    )
                if before != after:
                    continue
                if _source_lock_status(path) != _LOCK_STATUS_UNLOCKED:
                    raise _SnapshotUnavailable(
                        "provider snapshot source is locked"
                    )
                connection = _readonly_connection(snapshot)
                try:
                    return reader(connection)
                finally:
                    connection.close()
        except _SnapshotUnavailable:
            raise
        except (OSError, ValueError, sqlite3.DatabaseError):
            continue
    raise _SnapshotUnavailable(
        "provider snapshot source changed during capture"
    )


def _schema_capability(connection: sqlite3.Connection) -> StoreCapability:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None or not isinstance(version_row[0], int):
        return StoreCapability.CORRUPT
    version = int(version_row[0])
    if not MIN_READ_SCHEMA_VERSION <= version <= MAX_READ_SCHEMA_VERSION:
        return StoreCapability.INCOMPATIBLE

    table_types = {
        str(row[1]): str(row[2])
        for row in connection.execute("PRAGMA table_list")
    }
    for table, required in _REQUIRED_COLUMNS.items():
        if (
            table_types.get(table) != "table"
            or not required.issubset(_table_columns(connection, table))
        ):
            return StoreCapability.INCOMPATIBLE
    if version in WRITE_SCHEMA_VERSIONS:
        return StoreCapability.COMPATIBLE
    return StoreCapability.READ_ONLY


def _read_probed(
    path: Path,
    reader: Callable[[sqlite3.Connection], _T] | None = None,
) -> tuple[StoreCapability, _T | None]:
    candidate = _candidate_capability(path)
    if candidate is not None:
        return candidate, None

    def probe(
        connection: sqlite3.Connection,
    ) -> tuple[StoreCapability, _T | None]:
        capability = _schema_capability(connection)
        if reader is None or not capability.can_read:
            return capability, None
        return capability, reader(connection)

    try:
        return _read_stable_snapshot(path, probe)
    except _SnapshotUnavailable:
        return StoreCapability.CORRUPT, None


class CCSwitchProviderStore:
    """CC Switch's SQLite schema exposed through a strictly read-only seam."""

    __slots__ = ("_database_path", "_local_interactive")

    def __init__(
        self,
        database_path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        local_interactive: bool = False,
    ) -> None:
        if not isinstance(local_interactive, bool):
            raise TypeError("local_interactive must be a bool")
        self._database_path = resolve_ccswitch_database_path(
            database_path,
            environ=environ,
        )
        self._local_interactive = local_interactive

    def detect(self) -> StoreCapability:
        capability, _payload = _read_probed(self._database_path)
        return capability

    def list(self) -> tuple[ProviderRef, ...]:
        selected = ["id", "is_current"]
        if self._local_interactive:
            selected.append("name")
        query = (
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type=? ORDER BY sort_index, id"
        )
        capability, rows = _read_probed(
            self._database_path,
            lambda connection: connection.execute(
                query,
                ("claude",),
            ).fetchall(),
        )
        if not capability.can_read or rows is None:
            raise ProviderStoreUnavailableError("provider store is not readable")

        references: list[ProviderRef] = []
        try:
            for row in rows:
                provider_id = row[0]
                current_raw = row[1]
                if (
                    not isinstance(provider_id, str)
                    or not _is_sqlite_boolean(current_raw)
                ):
                    raise ValueError
                display_name = row[2] if self._local_interactive else None
                references.append(
                    ProviderRef(
                        store=CC_SWITCH_STORE_ID,
                        provider_id=provider_id,
                        is_current=bool(current_raw),
                        display_name=display_name,
                    )
                )
        except (TypeError, ValueError):
            raise ProviderStoreError("provider row is invalid") from None
        return tuple(references)

    def inspect(self, reference: ProviderRef) -> ProviderInspection:
        if not isinstance(reference, ProviderRef):
            raise TypeError("reference must be a ProviderRef")
        if reference.store != CC_SWITCH_STORE_ID:
            raise ProviderNotFoundError("provider reference was not found")

        def read_rows(
            connection: sqlite3.Connection,
        ) -> tuple[object, object]:
            return (
                connection.execute(
                    "SELECT settings_config, is_current FROM providers "
                    "WHERE id=? AND app_type=?",
                    (reference.provider_id, "claude"),
                ).fetchone(),
                connection.execute(
                    "SELECT live_takeover_active FROM proxy_config "
                    "WHERE app_type=?",
                    ("claude",),
                ).fetchone(),
            )

        capability, payload = _read_probed(
            self._database_path,
            read_rows,
        )
        if not capability.can_read or payload is None:
            raise ProviderStoreUnavailableError("provider store is not readable")
        row, takeover_row = payload

        if row is None:
            raise ProviderNotFoundError("provider reference was not found")
        raw_settings, current_raw = row
        if (
            not isinstance(raw_settings, str)
            or not _is_sqlite_boolean(current_raw)
            or takeover_row is None
            or not _is_sqlite_boolean(takeover_row[0])
        ):
            raise ProviderConfigCorruptError(
                "provider configuration is invalid"
            )
        try:
            if len(raw_settings) > MAX_SETTINGS_CONFIG_BYTES:
                raise ValueError
            raw_settings_bytes = raw_settings.encode("utf-8")
            if len(raw_settings_bytes) > MAX_SETTINGS_CONFIG_BYTES:
                raise ValueError
            document = json.loads(
                raw_settings,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_non_finite_json,
            )
            if not isinstance(document, dict):
                raise ValueError
            adapter = ClaudeModelAdapter()
            models = adapter.project(document)
            unknown = adapter.summarize_unknown(document)
            fingerprint = hashlib.sha256(
                raw_settings_bytes
            ).hexdigest()
        except (
            json.JSONDecodeError,
            RecursionError,
            UnicodeError,
            ValueError,
            ClaudeModelDocumentError,
        ):
            raise ProviderConfigCorruptError(
                "provider configuration is invalid"
            ) from None

        return ProviderInspection(
            reference=ProviderRef(
                store=reference.store,
                provider_id=reference.provider_id,
                is_current=bool(current_raw),
                display_name=reference.display_name,
            ),
            models=models,
            is_current=bool(current_raw),
            fingerprint=fingerprint,
            proxy_takeover=bool(takeover_row[0]),
            schema_capability=capability,
            unknown_field_count=unknown.count,
            unknown_fingerprint=unknown.fingerprint,
        )


__all__ = [
    "CC_SWITCH_DB_ENV",
    "CC_SWITCH_DB_ENV_ALIASES",
    "CC_SWITCH_STORE_ID",
    "CCSwitchProviderStore",
    "MAX_READ_SCHEMA_VERSION",
    "MAX_SETTINGS_CONFIG_BYTES",
    "MIN_READ_SCHEMA_VERSION",
    "WRITE_SCHEMA_VERSIONS",
    "resolve_ccswitch_database_path",
]
