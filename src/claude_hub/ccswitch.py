"""Read-only CC Switch provider-store adapter.

Database paths and raw provider records remain inside this module.  The
presentation boundary receives only validated stable references and redacted
inspection DTOs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path

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
    connection = sqlite3.connect(uri, uri=True)
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


def _open_probed(
    path: Path,
) -> tuple[StoreCapability, sqlite3.Connection | None]:
    candidate = _candidate_capability(path)
    if candidate is not None:
        return candidate, None
    connection: sqlite3.Connection | None = None
    try:
        connection = _readonly_connection(path)
        capability = _schema_capability(connection)
    except (OSError, ValueError, sqlite3.DatabaseError):
        if connection is not None:
            connection.close()
        return StoreCapability.CORRUPT, None
    if not capability.can_read:
        connection.close()
        return capability, None
    return capability, connection


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
        capability, connection = _open_probed(self._database_path)
        if connection is not None:
            connection.close()
        return capability

    def list(self) -> tuple[ProviderRef, ...]:
        capability, connection = _open_probed(self._database_path)
        if not capability.can_read or connection is None:
            raise ProviderStoreUnavailableError("provider store is not readable")

        selected = ["id", "is_current"]
        if self._local_interactive:
            selected.append("name")
        query = (
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type=? ORDER BY sort_index, id"
        )
        try:
            try:
                rows = connection.execute(query, ("claude",)).fetchall()
            finally:
                connection.close()
        except (OSError, ValueError, sqlite3.DatabaseError):
            raise ProviderStoreError("provider list could not be read") from None

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

        capability, connection = _open_probed(self._database_path)
        if not capability.can_read or connection is None:
            raise ProviderStoreUnavailableError("provider store is not readable")

        try:
            try:
                row = connection.execute(
                    "SELECT settings_config, is_current FROM providers "
                    "WHERE id=? AND app_type=?",
                    (reference.provider_id, "claude"),
                ).fetchone()
                takeover_row = connection.execute(
                    "SELECT live_takeover_active FROM proxy_config "
                    "WHERE app_type=?",
                    ("claude",),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, ValueError, sqlite3.DatabaseError):
            raise ProviderStoreError("provider inspection could not be read") from None

        if row is None:
            raise ProviderNotFoundError("provider reference was not found")
        raw_settings, current_raw = row
        if (
            not isinstance(raw_settings, str)
            or not _is_sqlite_boolean(current_raw)
            or (
                takeover_row is not None
                and not _is_sqlite_boolean(takeover_row[0])
            )
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
            proxy_takeover=bool(takeover_row and takeover_row[0]),
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
