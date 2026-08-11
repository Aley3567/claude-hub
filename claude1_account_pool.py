"""Non-secret scheduling for multiple CC Switch accounts of one provider.

Each account remains an ordinary CC Switch provider row and therefore keeps its
credential in CC Switch.  This module stores only stable ``id:`` selectors,
scheduling rules and runtime health state.  Tokens never enter the config or
SQLite state managed here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping


CONFIG_VERSION = 1
STATE_VERSION = 2
MAX_CONFIG_BYTES = 1024 * 1024
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_MAX_COOLDOWN_SECONDS = 3600
MAX_MEMBERS = 64
SELECTOR_RE = re.compile(r"id:[^\x00-\x1f\x7f]{1,256}\Z")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
STRATEGIES = {"round_robin", "weighted"}

_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[str, threading.RLock] = {}


def _state_process_lock(path: Path) -> threading.RLock:
    key = os.path.abspath(os.fspath(path))
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STATE_LOCKS[key] = lock
        return lock


class AccountPoolError(RuntimeError):
    """Base error for account-pool configuration or runtime state."""


class PoolConfigError(AccountPoolError):
    """The non-secret pool definition is invalid or unsafe to read."""


class PoolStateError(AccountPoolError):
    """The shared scheduler state is unavailable or unsafe to use."""


class PoolExhausted(AccountPoolError):
    """No eligible account remains for this logical request."""

    def __init__(
        self,
        retry_after: int | None = None,
        *,
        reason: str = "unavailable",
    ):
        self.retry_after = retry_after
        self.reason = reason
        message = "all provider accounts are unavailable"
        if retry_after is not None:
            message += f"; retry in {retry_after}s"
        super().__init__(message)


@dataclass(frozen=True)
class AccountCandidate:
    """Non-secret facts supplied by the provider-directory adapter."""

    fingerprint: str
    endpoint: str = ""
    credential_type: str = ""

    def __post_init__(self) -> None:
        if self.fingerprint and not FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise PoolConfigError("account credential fingerprint is invalid")


@dataclass(frozen=True)
class PoolMember:
    selector: str
    weight: int = 1
    priority: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class PoolDefinition:
    primary: str
    strategy: str
    members: tuple[PoolMember, ...]
    cooldown_seconds: int
    max_cooldown_seconds: int
    config_hash: str


@dataclass(frozen=True)
class AccountLease:
    """One immutable account choice for a request or native session."""

    primary: str
    member: str
    fingerprint: str = field(repr=False)
    managed: bool = False

    def __post_init__(self) -> None:
        if not FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise PoolStateError("account lease fingerprint is invalid")


@dataclass(frozen=True)
class AccountStatus:
    member: str
    enabled: bool
    state: str
    retry_after: int | None
    last_status: int | None


def credential_fingerprint(token: str) -> str:
    """Return a non-reversible scheduler revision for one high-entropy token."""
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_account_endpoint(
    value: object,
    *,
    is_full_url: bool = False,
) -> str:
    """Normalize the endpoint identity shared by Hub, native and CLI adapters."""
    endpoint = value.strip().rstrip("/") if isinstance(value, str) else ""
    if not is_full_url and endpoint.endswith("/v1"):
        endpoint = endpoint[:-3].rstrip("/")
    return endpoint


def _canonical_selector(value: object, label: str) -> str:
    if not isinstance(value, str) or not SELECTOR_RE.fullmatch(value):
        raise PoolConfigError(f"{label} must be a stable id: selector")
    return value


def _bounded_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PoolConfigError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise PoolConfigError(f"{label} must be between {minimum} and {maximum}")
    return value


def _require_private_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PoolConfigError(f"cannot inspect {label}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PoolConfigError(f"{label} must be a regular file")
    if os.name == "posix" and info.st_mode & 0o077:
        raise PoolConfigError(f"{label} permissions must be 0600")


def _read_config_object(path: Path) -> dict:
    if not path.exists():
        return {"version": CONFIG_VERSION, "providers": {}}
    _require_private_regular_file(path, "account-pool config")
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise PoolConfigError("account-pool config is too large")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except PoolConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PoolConfigError("account-pool config is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise PoolConfigError("account-pool config must be an object")
    return raw


def _normalize_config(raw: dict) -> tuple[dict, dict[str, PoolDefinition]]:
    if raw.get("version") != CONFIG_VERSION:
        raise PoolConfigError(f"account-pool config version must be {CONFIG_VERSION}")
    if set(raw) - {"version", "providers"}:
        raise PoolConfigError("account-pool config contains unknown fields")
    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict):
        raise PoolConfigError("account-pool providers must be an object")

    normalized: dict = {"version": CONFIG_VERSION, "providers": {}}
    definitions: dict[str, PoolDefinition] = {}
    for raw_primary, pool_raw in providers_raw.items():
        primary = _canonical_selector(raw_primary, "pool provider")
        if not isinstance(pool_raw, dict):
            raise PoolConfigError(f"pool {primary} must be an object")
        allowed = {
            "strategy",
            "members",
            "cooldown_seconds",
            "max_cooldown_seconds",
        }
        if set(pool_raw) - allowed:
            raise PoolConfigError(f"pool {primary} contains unknown fields")
        strategy = pool_raw.get("strategy", "round_robin")
        if strategy not in STRATEGIES:
            raise PoolConfigError(
                f"pool {primary} strategy must be round_robin or weighted"
            )
        cooldown = _bounded_int(
            pool_raw.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
            label=f"pool {primary} cooldown_seconds",
            minimum=1,
            maximum=86400,
        )
        max_cooldown = _bounded_int(
            pool_raw.get("max_cooldown_seconds", DEFAULT_MAX_COOLDOWN_SECONDS),
            label=f"pool {primary} max_cooldown_seconds",
            minimum=cooldown,
            maximum=7 * 86400,
        )
        members_raw = pool_raw.get("members")
        if not isinstance(members_raw, list) or not members_raw:
            raise PoolConfigError(f"pool {primary} members must be a non-empty list")
        if len(members_raw) > MAX_MEMBERS:
            raise PoolConfigError(f"pool {primary} has too many members")
        members: list[PoolMember] = []
        seen: set[str] = set()
        for index, member_raw in enumerate(members_raw):
            if isinstance(member_raw, str):
                member_raw = {"provider": member_raw}
            if not isinstance(member_raw, dict):
                raise PoolConfigError(f"pool {primary} member {index} must be an object")
            if set(member_raw) - {"provider", "weight", "priority", "enabled"}:
                raise PoolConfigError(f"pool {primary} member {index} has unknown fields")
            selector = _canonical_selector(
                member_raw.get("provider"), f"pool {primary} member {index}"
            )
            if selector in seen:
                raise PoolConfigError(f"pool {primary} contains duplicate member {selector}")
            seen.add(selector)
            enabled = member_raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise PoolConfigError(
                    f"pool {primary} member {selector} enabled must be a boolean"
                )
            member = PoolMember(
                selector=selector,
                weight=_bounded_int(
                    member_raw.get("weight", 1),
                    label=f"pool {primary} member {selector} weight",
                    minimum=1,
                    maximum=100,
                ),
                priority=_bounded_int(
                    member_raw.get("priority", 0),
                    label=f"pool {primary} member {selector} priority",
                    minimum=0,
                    maximum=1000,
                ),
                enabled=enabled,
            )
            members.append(member)
        if primary not in seen:
            raise PoolConfigError(f"pool {primary} must include its primary provider")

        pool_normalized = {
            "strategy": strategy,
            "cooldown_seconds": cooldown,
            "max_cooldown_seconds": max_cooldown,
            "members": [
                {
                    "provider": member.selector,
                    "weight": member.weight,
                    "priority": member.priority,
                    "enabled": member.enabled,
                }
                for member in members
            ],
        }
        normalized["providers"][primary] = pool_normalized
        config_hash = hashlib.sha256(
            json.dumps(
                pool_normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        definitions[primary] = PoolDefinition(
            primary=primary,
            strategy=strategy,
            members=tuple(members),
            cooldown_seconds=cooldown,
            max_cooldown_seconds=max_cooldown,
            config_hash=config_hash,
        )
    return normalized, definitions


def load_pool_definitions(path: Path) -> dict[str, PoolDefinition]:
    """Load and validate all non-secret pool definitions."""
    _normalized, definitions = _normalize_config(_read_config_object(path))
    return definitions


def _ensure_private_parent(path: Path) -> None:
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def _prepare_state_file(path: Path) -> None:
    _ensure_private_parent(path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PoolStateError("cannot open account-pool state") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PoolStateError("account-pool state must be a regular file")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


_POOL_CURSOR_V1 = {"pool": 1, "config_hash": 0, "cursor": 0}
_MEMBER_STATE_V1 = {
    "pool": 1,
    "member": 2,
    "fingerprint": 0,
    "disabled": 0,
    "cooldown_until": 0,
    "last_status": 0,
    "updated_at": 0,
}
_POOL_CURSOR_V2 = {
    "pool": 1,
    "priority": 2,
    "config_hash": 0,
    "cursor": 0,
}
_MEMBER_STATE_V2 = {
    "pool": 1,
    "member": 2,
    "fingerprint": 3,
    "disabled": 0,
    "cooldown_until": 0,
    "last_status": 0,
}


def _state_table_shape(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row[1]): int(row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _require_state_table(
    conn: sqlite3.Connection,
    table: str,
    expected: Mapping[str, int],
) -> None:
    if _state_table_shape(conn, table) != dict(expected):
        raise PoolStateError(f"account-pool state table {table} is incompatible")


def _create_state_v2(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE pool_cursor ("
        "pool TEXT NOT NULL, priority INTEGER NOT NULL, "
        "config_hash TEXT NOT NULL, cursor INTEGER NOT NULL, "
        "PRIMARY KEY (pool, priority))"
    )
    conn.execute(
        "CREATE TABLE member_state ("
        "pool TEXT NOT NULL, member TEXT NOT NULL, fingerprint TEXT NOT NULL, "
        "disabled INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0, "
        "last_status INTEGER, "
        "PRIMARY KEY (pool, member, fingerprint))"
    )


def _validate_state_v2(conn: sqlite3.Connection) -> None:
    _require_state_table(conn, "pool_cursor", _POOL_CURSOR_V2)
    _require_state_table(conn, "member_state", _MEMBER_STATE_V2)


def _ensure_state_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row is not None else 0
    if version == STATE_VERSION:
        _validate_state_v2(conn)
        return
    if version not in (0, 1):
        raise PoolStateError(
            f"account-pool state version {version} is unsupported"
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        if version == STATE_VERSION:
            _validate_state_v2(conn)
            conn.commit()
            return
        if version == 0:
            tables = {
                str(item[0])
                for item in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables:
                raise PoolStateError(
                    "unversioned account-pool state is not empty"
                )
            _create_state_v2(conn)
        elif version == 1:
            _require_state_table(conn, "pool_cursor", _POOL_CURSOR_V1)
            _require_state_table(conn, "member_state", _MEMBER_STATE_V1)
            conn.execute("ALTER TABLE pool_cursor RENAME TO pool_cursor_v1")
            conn.execute("ALTER TABLE member_state RENAME TO member_state_v1")
            _create_state_v2(conn)
            conn.execute(
                "INSERT INTO pool_cursor(pool, priority, config_hash, cursor) "
                "SELECT pool, 0, config_hash, cursor FROM pool_cursor_v1"
            )
            conn.execute(
                "INSERT INTO member_state("
                "pool, member, fingerprint, disabled, cooldown_until, "
                "last_status) "
                "SELECT pool, member, fingerprint, disabled, cooldown_until, "
                "last_status FROM member_state_v1"
            )
            conn.execute("DROP TABLE pool_cursor_v1")
            conn.execute("DROP TABLE member_state_v1")
        else:
            raise PoolStateError(
                f"account-pool state version {version} is unsupported"
            )
        conn.execute(f"PRAGMA user_version={STATE_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class AccountPool:
    """Select accounts and persist non-secret health state across processes."""

    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = 2_000,
    ) -> None:
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self._clock = clock
        self._busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._definitions_cache: dict[str, PoolDefinition] | None = None
        self._state_ready = False
        self._process_lock = _state_process_lock(self.state_path)

    def definitions(self) -> dict[str, PoolDefinition]:
        if self._definitions_cache is None:
            self._definitions_cache = load_pool_definitions(self.config_path)
        return self._definitions_cache

    def definition(self, primary: str) -> PoolDefinition | None:
        return self.definitions().get(primary)

    @contextmanager
    def _connection(self):
        with self._process_lock:
            with self._open_connection() as conn:
                yield conn

    @contextmanager
    def _open_connection(self):
        _prepare_state_file(self.state_path)
        try:
            conn = sqlite3.connect(
                self.state_path,
                timeout=self._busy_timeout_ms / 1000,
            )
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            if not self._state_ready:
                version_row = conn.execute("PRAGMA user_version").fetchone()
                version = int(version_row[0]) if version_row is not None else 0
                if version != STATE_VERSION:
                    _ensure_state_schema(conn)
                self._state_ready = True
            yield conn
        except sqlite3.Error as exc:
            raise PoolStateError("account-pool state is unavailable") from exc
        finally:
            if "conn" in locals():
                conn.close()

    @staticmethod
    def _validate_candidates(
        definition: PoolDefinition,
        candidates: Mapping[str, AccountCandidate],
    ) -> None:
        primary_candidate = candidates.get(definition.primary)
        if (
            primary_candidate is None
            or not primary_candidate.endpoint
            or not primary_candidate.credential_type
        ):
            raise PoolConfigError(
                f"pool {definition.primary} primary provider is unavailable"
            )
        fingerprints: dict[str, str] = {}
        for member in definition.members:
            if not member.enabled:
                continue
            candidate = candidates.get(member.selector)
            if candidate is None or not candidate.fingerprint:
                raise PoolConfigError(
                    f"pool {definition.primary} member {member.selector} has no credential"
                )
            if (
                candidate.endpoint != primary_candidate.endpoint
                or candidate.credential_type != primary_candidate.credential_type
            ):
                raise PoolConfigError(
                    f"pool {definition.primary} member {member.selector} is incompatible"
                )
            duplicate = fingerprints.get(candidate.fingerprint)
            if duplicate is not None:
                raise PoolConfigError(
                    f"pool {definition.primary} members {duplicate} and "
                    f"{member.selector} use the same credential"
                )
            fingerprints[candidate.fingerprint] = member.selector

    def acquire(
        self,
        primary: str,
        candidates: Mapping[str, AccountCandidate],
        *,
        exclude: Iterable[str] = (),
    ) -> AccountLease:
        """Atomically choose one eligible account without exposing its token."""
        primary = _canonical_selector(primary, "provider")
        definition = self.definition(primary)
        if definition is None:
            if primary in set(exclude):
                raise PoolExhausted(reason="excluded")
            candidate = candidates.get(primary)
            if candidate is None or not candidate.fingerprint:
                raise PoolConfigError(f"provider {primary} has no usable credential")
            return AccountLease(primary, primary, candidate.fingerprint)

        self._validate_candidates(definition, candidates)
        excluded = set(exclude)
        now = float(self._clock())
        if not math.isfinite(now):
            raise PoolStateError("account-pool clock is invalid")

        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                states = {
                    (str(member), str(fingerprint)): (
                        bool(disabled),
                        float(cooldown_until or 0),
                    )
                    for member, fingerprint, disabled, cooldown_until in conn.execute(
                        "SELECT member, fingerprint, disabled, cooldown_until "
                        "FROM member_state WHERE pool=?",
                        (primary,),
                    )
                }

                available: list[PoolMember] = []
                cooldowns: list[float] = []
                auth_disabled = 0
                config_disabled = 0
                for member in definition.members:
                    if not member.enabled:
                        config_disabled += 1
                        continue
                    if member.selector in excluded:
                        continue
                    candidate = candidates[member.selector]
                    state = states.get((member.selector, candidate.fingerprint))
                    if state is None:
                        available.append(member)
                        continue
                    if state[0]:
                        auth_disabled += 1
                        continue
                    cooldown_until = state[1]
                    if cooldown_until > now:
                        cooldowns.append(cooldown_until)
                        continue
                    available.append(member)

                if not available:
                    conn.rollback()
                    retry_after = (
                        max(1, math.ceil(min(cooldowns) - now)) if cooldowns else None
                    )
                    if cooldowns:
                        reason = "cooldown"
                    elif auth_disabled:
                        reason = "auth_disabled"
                    elif config_disabled:
                        reason = "config_disabled"
                    else:
                        reason = "excluded"
                    raise PoolExhausted(retry_after, reason=reason)

                lowest_priority = min(member.priority for member in available)
                group = [
                    member for member in available if member.priority == lowest_priority
                ]
                cursor_row = conn.execute(
                    "SELECT config_hash, cursor FROM pool_cursor "
                    "WHERE pool=? AND priority=?",
                    (primary, lowest_priority),
                ).fetchone()
                cursor = (
                    int(cursor_row[1])
                    if cursor_row is not None and cursor_row[0] == definition.config_hash
                    else 0
                )
                if definition.strategy == "weighted":
                    slot = cursor % sum(member.weight for member in group)
                    selected = group[-1]
                    for member in group:
                        if slot < member.weight:
                            selected = member
                            break
                        slot -= member.weight
                else:
                    selected = group[cursor % len(group)]

                conn.execute(
                    "INSERT INTO pool_cursor("
                    "pool, priority, config_hash, cursor) VALUES(?,?,?,?) "
                    "ON CONFLICT(pool, priority) DO UPDATE SET "
                    "config_hash=excluded.config_hash, "
                    "cursor=excluded.cursor",
                    (primary, lowest_priority, definition.config_hash, cursor + 1),
                )
                conn.commit()
            except PoolExhausted:
                raise
            except sqlite3.Error:
                conn.rollback()
                raise

        return AccountLease(
            primary,
            selected.selector,
            candidates[selected.selector].fingerprint,
            managed=True,
        )

    @staticmethod
    def _retry_delay(
        value: str | None,
        *,
        now: float,
        default: int,
        maximum: int,
    ) -> int:
        delay: float | None = None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                digits = stripped.lstrip("0") or "0"
                maximum_digits = str(maximum)
                if (
                    len(digits) > len(maximum_digits)
                    or (
                        len(digits) == len(maximum_digits)
                        and digits > maximum_digits
                    )
                ):
                    delay = float(maximum)
                else:
                    try:
                        delay = float(int(digits))
                    except (ValueError, OverflowError):
                        delay = float(maximum)
            elif stripped:
                try:
                    parsed = parsedate_to_datetime(stripped)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    delay = parsed.timestamp() - now
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None or not math.isfinite(delay):
            delay = float(default)
        return max(1, min(maximum, math.ceil(delay)))

    def report(
        self,
        lease: AccountLease,
        status: int,
        retry_after: str | None = None,
    ) -> None:
        """Persist one pre-commit upstream outcome for future selections."""
        if not lease.managed:
            return
        definition = self.definition(lease.primary)
        if definition is None:
            return
        now = float(self._clock())
        if not math.isfinite(now):
            raise PoolStateError("account-pool clock is invalid")
        disabled = status in (401, 403)
        cooldown_until = 0.0
        if status == 429:
            cooldown_until = now + self._retry_delay(
                retry_after,
                now=now,
                default=definition.cooldown_seconds,
                maximum=definition.max_cooldown_seconds,
            )
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    "SELECT disabled, cooldown_until FROM member_state "
                    "WHERE pool=? AND member=? AND fingerprint=?",
                    (lease.primary, lease.member, lease.fingerprint),
                ).fetchone()
                if previous is not None:
                    # Outcomes from concurrent requests must merge monotonically:
                    # a late success cannot erase another request's durable auth
                    # disable or active rate-limit cooldown for the same key.
                    disabled = disabled or bool(previous[0])
                    cooldown_until = max(cooldown_until, float(previous[1] or 0))
                conn.execute(
                    "INSERT INTO member_state("
                    "pool, member, fingerprint, disabled, cooldown_until, last_status"
                    ") VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(pool, member, fingerprint) DO UPDATE SET "
                    "disabled=excluded.disabled, "
                    "cooldown_until=excluded.cooldown_until, "
                    "last_status=excluded.last_status",
                    (
                        lease.primary,
                        lease.member,
                        lease.fingerprint,
                        int(disabled),
                        cooldown_until,
                        int(status),
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise

    def reset(self, primary: str, member: str | None = None) -> int:
        """Clear durable disable/cooldown state without changing pool config."""
        primary = _canonical_selector(primary, "provider")
        if member is not None:
            member = _canonical_selector(member, "account")
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if member is None:
                    cursor = conn.execute(
                        "DELETE FROM member_state WHERE pool=?", (primary,)
                    )
                    conn.execute("DELETE FROM pool_cursor WHERE pool=?", (primary,))
                else:
                    cursor = conn.execute(
                        "DELETE FROM member_state WHERE pool=? AND member=?",
                        (primary, member),
                    )
                changed = max(0, int(cursor.rowcount))
                conn.commit()
                return changed
            except sqlite3.Error:
                conn.rollback()
                raise

    def inspect(
        self,
        primary: str,
        candidates: Mapping[str, AccountCandidate],
    ) -> tuple[AccountStatus, ...]:
        definition = self.definition(primary)
        if definition is None:
            return ()
        self._validate_candidates(definition, candidates)
        now = float(self._clock())
        with self._connection() as conn:
            rows = {
                (member, fingerprint): (
                    bool(disabled),
                    float(cooldown),
                    last_status,
                )
                for member, fingerprint, disabled, cooldown, last_status in conn.execute(
                    "SELECT member, fingerprint, disabled, cooldown_until, last_status "
                    "FROM member_state WHERE pool=?",
                    (primary,),
                )
            }
        statuses: list[AccountStatus] = []
        for member in definition.members:
            candidate = candidates.get(member.selector)
            state = (
                rows.get((member.selector, candidate.fingerprint))
                if candidate is not None and candidate.fingerprint
                else None
            )
            status = "ready"
            retry = None
            last_status = None
            if not member.enabled:
                status = "disabled_by_config"
            elif state is not None:
                last_status = state[2]
                if state[0]:
                    status = "auth_disabled"
                elif state[1] > now:
                    status = "cooldown"
                    retry = max(1, math.ceil(state[1] - now))
            statuses.append(
                AccountStatus(
                    member=member.selector,
                    enabled=member.enabled,
                    state=status,
                    retry_after=retry,
                    last_status=last_status,
                )
            )
        return tuple(statuses)


@contextmanager
def _config_lock(path: Path):
    _ensure_private_parent(path)
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise PoolConfigError("cannot open account-pool config lock") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PoolConfigError("account-pool config lock must be a regular file")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_private_write(path: Path, data: str) -> None:
    _ensure_private_parent(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class PoolConfigStore:
    """Small admin interface for non-secret pool definitions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def normalized(self) -> dict:
        normalized, _definitions = _normalize_config(_read_config_object(self.path))
        return normalized

    def _mutate(self, mutator: Callable[[dict], None]) -> dict:
        with _config_lock(self.path):
            normalized = self.normalized()
            mutator(normalized)
            normalized, _definitions = _normalize_config(normalized)
            _atomic_private_write(
                self.path,
                json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            )
            return normalized

    def upsert_member(
        self,
        primary: str,
        member: str,
        *,
        weight: int = 1,
        priority: int = 0,
        enabled: bool = True,
    ) -> dict:
        primary = _canonical_selector(primary, "provider")
        member = _canonical_selector(member, "account")

        def mutate(config: dict) -> None:
            providers = config["providers"]
            pool = providers.setdefault(
                primary,
                {
                    "strategy": "round_robin",
                    "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
                    "max_cooldown_seconds": DEFAULT_MAX_COOLDOWN_SECONDS,
                    "members": [
                        {
                            "provider": primary,
                            "weight": 1,
                            "priority": 0,
                            "enabled": True,
                        }
                    ],
                },
            )
            replacement = {
                "provider": member,
                "weight": weight,
                "priority": priority,
                "enabled": enabled,
            }
            for index, existing in enumerate(pool["members"]):
                if existing["provider"] == member:
                    pool["members"][index] = replacement
                    break
            else:
                pool["members"].append(replacement)

        return self._mutate(mutate)

    def remove_member(self, primary: str, member: str) -> dict:
        primary = _canonical_selector(primary, "provider")
        member = _canonical_selector(member, "account")
        if primary == member:
            raise PoolConfigError("remove the pool instead of its primary provider")

        def mutate(config: dict) -> None:
            pool = config["providers"].get(primary)
            if pool is None:
                raise PoolConfigError(f"pool {primary} does not exist")
            pool["members"] = [
                item for item in pool["members"] if item["provider"] != member
            ]

        return self._mutate(mutate)

    def set_strategy(self, primary: str, strategy: str) -> dict:
        primary = _canonical_selector(primary, "provider")
        if strategy not in STRATEGIES:
            raise PoolConfigError("strategy must be round_robin or weighted")

        def mutate(config: dict) -> None:
            pool = config["providers"].get(primary)
            if pool is None:
                raise PoolConfigError(f"pool {primary} does not exist")
            pool["strategy"] = strategy

        return self._mutate(mutate)

    def delete_pool(self, primary: str) -> bool:
        primary = _canonical_selector(primary, "provider")
        deleted = False

        def mutate(config: dict) -> None:
            nonlocal deleted
            deleted = config["providers"].pop(primary, None) is not None

        self._mutate(mutate)
        return deleted
