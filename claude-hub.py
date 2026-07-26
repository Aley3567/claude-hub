#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9"]
# ///
"""claude-hub — local multi-channel Anthropic gateway.

The gateway routes ``/model channel,model`` selections inside one Claude Code
session. Provider endpoints and credentials are read from the CC Switch SQLite
database in read-only mode; this file never contains provider credentials.
"""

from __future__ import annotations

import asyncio
import codecs
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from claude1_protocol import (
    AnthropicStreamBridge,
    ProtocolTransformError,
    SSEParser,
    provider_api_format,
    transform_error,
    transform_request,
    transform_response,
)
from claude1_provider import (
    CapabilityProfile,
    ProviderPolicyError,
    capability_summary,
    resolve_capability_profile,
)


SERVICE_NAME = "claude-hub"
PROTOCOL_VERSION = 1
VERSION = "0.1.0"
HEALTH_PAYLOAD = {
    "ok": True,
    "service": SERVICE_NAME,
    "protocol": PROTOCOL_VERSION,
    "version": VERSION,
}

HOME = Path.home()
DEFAULT_CONFIG_PATH = HOME / ".cc-switch" / "claude-hub.json"
DEFAULT_DB_PATH = HOME / ".cc-switch" / "cc-switch.db"
DEFAULT_LOG_PATH = HOME / ".cc-switch" / "logs" / "claude-hub.log"

ENV_CONFIG = "CLAUDE_HUB_CONFIG"
ENV_DB = "CLAUDE_HUB_DB"
ENV_LOG = "CLAUDE_HUB_LOG"
ENV_PORT = "CLAUDE_HUB_PORT"
ENV_LOCAL_TOKEN = "CLAUDE_HUB_LOCAL_TOKEN"

LOG_MAX_BYTES = 10 * 1024 * 1024
CONTEXT_1M_BETA = "context-1m-2025-08-07"
UPSTREAM_SESSION_KEY = web.AppKey("upstream_session", aiohttp.ClientSession)
DB_SNAPSHOT_RETRIES = 5
SSE_LINE_LIMIT = 64 * 1024
SSE_DECODE_CHUNK = 64 * 1024
SSE_DECODE_SLACK = 1 * 1024 * 1024
SSE_DECODE_RATIO_LIMIT = 100
SSE_DECODE_TOTAL_LIMIT = 256 * 1024 * 1024
SSE_GZIP_MEMBER_LIMIT = 16
SSE_NEWLINE_RE = re.compile(br"\r\n|[\r\n]")
REPRESENTATION_HEADERS = {"content-type", "content-encoding"}

HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
REQ_STRIP = HOP_BY_HOP | {
    "content-length",
    "content-encoding",
    "authorization",
    "x-api-key",
}
RESP_STRIP = HOP_BY_HOP | {"content-length"}

_log_fp = None
_log_stderr = False


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def config_path() -> Path:
    return _env_path(ENV_CONFIG, DEFAULT_CONFIG_PATH)


def db_path() -> Path:
    return _env_path(ENV_DB, DEFAULT_DB_PATH)


def log_path() -> Path:
    return _env_path(ENV_LOG, DEFAULT_LOG_PATH)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()
    if _log_stderr or not _log_fp:
        print(line, file=sys.stderr)


def open_log() -> None:
    global _log_fp
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    expected_current = current
    if current is not None:
        if not stat.S_ISREG(current.st_mode):
            raise RuntimeError("log path is not a regular file")
        if current.st_size > LOG_MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            path.replace(rotated)
            expected_current = None
            rotated_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                rotated_flags |= os.O_NOFOLLOW
            rotated_fd = os.open(rotated, rotated_flags)
            try:
                if not stat.S_ISREG(os.fstat(rotated_fd).st_mode):
                    raise RuntimeError("rotated log path is not a regular file")
                if os.name == "posix":
                    os.fchmod(rotated_fd, 0o600)
            finally:
                os.close(rotated_fd)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("log path is not a regular file")
        if expected_current is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != (
            expected_current.st_dev,
            expected_current.st_ino,
        ):
            raise RuntimeError("log path changed while it was being opened")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        _log_fp = os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


# ---------------------------------------------------------------- config / DB


class ConfigError(ValueError):
    """The hub configuration is missing or unsafe to use."""


class ProviderDatabaseError(RuntimeError):
    """The CC Switch provider database is missing, unreadable or malformed."""


class UpstreamStreamAborted(RuntimeError):
    """An upstream stream failed after downstream headers were committed."""


_permission_warning_emitted = False


def _private_file_issue(path: Path, label: str) -> str | None:
    """Return a local file safety issue without exposing file contents."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return f"{label} file is missing"
    except OSError as exc:
        return f"{label} file cannot be inspected: {exc}"
    if not stat.S_ISREG(st.st_mode):
        return f"{label} path is not a regular file"
    if os.name == "posix":
        mode = stat.S_IMODE(st.st_mode)
        if mode & ~0o600:
            return f"{label} permissions {mode:04o} exceed 0600"
    return None


def _require_private_file(
    path: Path,
    label: str,
    error_type: type[ConfigError] | type[ProviderDatabaseError],
) -> None:
    """Fail closed for credential-bearing files on POSIX systems."""
    global _permission_warning_emitted
    issue = _private_file_issue(path, label)
    if issue:
        raise error_type(issue)
    if os.name != "posix" and not _permission_warning_emitted:
        log(
            "WARNING: POSIX file-permission checks are unavailable on this "
            "platform; continuing with existence and regular-file checks only"
        )
        _permission_warning_emitted = True


_cfg_cache: dict[str, object] = {
    "path": None,
    "mtime_ns": None,
    "size": None,
    "raw": None,
}


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _config_port(raw: object) -> int:
    override = os.environ.get(ENV_PORT)
    value = override if override not in (None, "") else raw
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError("port must be an integer between 1 and 65535")
    if isinstance(value, str) and not value.strip().isdigit():
        raise ConfigError("port must be an integer between 1 and 65535")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("port must be an integer between 1 and 65535")
    return port


def validate_config(raw: object) -> dict:
    """Validate and return a normalized, detached configuration dictionary."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    channels_raw = raw.get("channels")
    if not isinstance(channels_raw, dict) or not channels_raw:
        raise ConfigError("channels must be a non-empty object")

    channels: dict[str, dict] = {}
    for alias, channel_raw in channels_raw.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ConfigError("channel aliases must be non-empty strings")
        if alias != alias.strip().lower():
            raise ConfigError(f"channel alias '{alias}' must be lowercase without outer spaces")
        if not isinstance(channel_raw, dict):
            raise ConfigError(f"channels.{alias} must be an object")

        provider = _require_nonempty_string(
            channel_raw.get("provider"), f"channels.{alias}.provider"
        )
        models_raw = channel_raw.get("models", [])
        if not isinstance(models_raw, list) or any(
            not isinstance(model, str) or not model.strip() for model in models_raw
        ):
            raise ConfigError(f"channels.{alias}.models must be a list of non-empty strings")

        allow_insecure = channel_raw.get("allow_insecure_http", False)
        if not isinstance(allow_insecure, bool):
            raise ConfigError(f"channels.{alias}.allow_insecure_http must be a boolean")

        channel = {
            "provider": provider,
            "models": [model.strip() for model in models_raw],
            "allow_insecure_http": allow_insecure,
        }
        api_format = channel_raw.get("api_format")
        if api_format is not None:
            if api_format not in {
                "anthropic",
                "openai_chat",
                "openai_responses",
            }:
                raise ConfigError(
                    f"channels.{alias}.api_format must be anthropic, "
                    "openai_chat, or openai_responses"
                )
            channel["api_format"] = api_format
        capabilities = channel_raw.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                raise ConfigError(
                    f"channels.{alias}.capabilities must be an object"
                )
            if (
                isinstance(api_format, str)
                and isinstance(capabilities.get("protocol"), str)
                and capabilities["protocol"] != api_format
            ):
                raise ConfigError(
                    f"channels.{alias}.api_format conflicts with "
                    "capabilities.protocol"
                )
            try:
                # Normalize once for validation.  Provider-derived values are
                # merged later when the channel is resolved.
                resolve_capability_profile(
                    override=capabilities,
                    protocol_override=api_format,
                )
            except ProviderPolicyError as exc:
                raise ConfigError(
                    f"channels.{alias}.{exc}"
                ) from exc
            channel["capabilities"] = json.loads(json.dumps(capabilities))
        if "proxy" in channel_raw:
            proxy = channel_raw["proxy"]
            if proxy is not None and (not isinstance(proxy, str) or not proxy.strip()):
                raise ConfigError(f"channels.{alias}.proxy must be a non-empty string or null")
            channel["proxy"] = proxy.strip() if isinstance(proxy, str) else None
        channels[alias] = channel

    default_channel = _require_nonempty_string(
        raw.get("default_channel"), "default_channel"
    ).lower()
    if default_channel not in channels:
        raise ConfigError(f"default_channel '{default_channel}' is not present in channels")

    token_env = raw.get("local_token_env", ENV_LOCAL_TOKEN)
    token_env = _require_nonempty_string(token_env, "local_token_env")
    local_token = os.environ.get(token_env) or raw.get("local_token")
    local_token = _require_nonempty_string(
        local_token,
        f"local_token (or environment variable {token_env})",
    )

    proxy = raw.get("proxy")
    if proxy is not None and (not isinstance(proxy, str) or not proxy.strip()):
        raise ConfigError("proxy must be a non-empty string or null")

    return {
        "port": _config_port(raw.get("port")),
        "local_token": local_token,
        "default_channel": default_channel,
        "channels": channels,
        "proxy": proxy.strip() if isinstance(proxy, str) else None,
    }


def get_config() -> dict:
    path = config_path()
    _require_private_file(path, "config", ConfigError)
    try:
        st = path.stat()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    cache_key = (path, st.st_mtime_ns, st.st_size)
    old_key = (
        _cfg_cache["path"],
        _cfg_cache["mtime_ns"],
        _cfg_cache["size"],
    )
    if cache_key != old_key:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid config {path}: {exc}") from exc
        _cfg_cache.update(
            {"path": path, "mtime_ns": st.st_mtime_ns, "size": st.st_size, "raw": raw}
        )
    return validate_config(_cfg_cache["raw"])


def _normalize_base_url(value: object) -> str:
    base = value.strip().rstrip("/") if isinstance(value, str) else ""
    # Forwarded paths already begin with /v1. Avoid /v1/v1/messages.
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _read_provider_rows(path: Path) -> dict:
    """Read provider rows without mutating the database or contacting providers."""
    db_uri = path.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()
        }
        selected = ["name", "settings_config"]
        selected.extend(
            column for column in ("meta", "provider_type") if column in columns
        )
        cursor = conn.execute(
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type='claude'"
        )
        rows = {}
        for raw_row in cursor.fetchall():
            values = dict(zip(selected, raw_row))
            name = values["name"]
            settings_config = values["settings_config"]
            try:
                settings = json.loads(settings_config)
            except (json.JSONDecodeError, UnicodeError, TypeError):
                continue
            if not isinstance(settings, dict):
                continue
            try:
                meta = json.loads(values.get("meta") or "{}")
            except (json.JSONDecodeError, UnicodeError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            env = settings.get("env") or {}
            if not isinstance(env, dict):
                continue
            is_full_url = meta.get("isFullUrl") is True
            raw_base = env.get("ANTHROPIC_BASE_URL")
            base = (
                raw_base.strip().rstrip("/")
                if is_full_url and isinstance(raw_base, str)
                else _normalize_base_url(raw_base)
            )
            if not base:
                continue
            auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
            api_key = env.get("ANTHROPIC_API_KEY")
            credential_ambiguous = (
                isinstance(auth_token, str)
                and auth_token.strip()
                and isinstance(api_key, str)
                and api_key.strip()
                and auth_token != api_key
            )
            token = auth_token or api_key or ""
            if not isinstance(token, str):
                token = ""
            try:
                capability_profile = resolve_capability_profile(
                    meta=meta,
                    settings=settings,
                    provider_type=values.get("provider_type"),
                )
                capability_error = None
            except ProviderPolicyError as exc:
                # Degrade only this provider: healthy channels keep serving
                # while requests routed here fail closed with a redacted
                # reason that doctor/list can also surface.
                capability_profile = None
                capability_error = str(exc)
            rows[name] = {
                "base_url": base,
                "token": token,
                "credential_source": (
                    "ANTHROPIC_AUTH_TOKEN"
                    if isinstance(auth_token, str) and auth_token.strip()
                    else "ANTHROPIC_API_KEY"
                    if isinstance(api_key, str) and api_key.strip()
                    else "missing"
                ),
                "credential_ambiguous": bool(credential_ambiguous),
                "api_format": provider_api_format(
                    meta=meta,
                    settings=settings,
                    provider_type=values.get("provider_type"),
                ),
                "provider_type": (
                    values.get("provider_type") or meta.get("providerType")
                ),
                "capability_profile": capability_profile,
                "capability_error": capability_error,
                "_capability_meta": meta,
                "_capability_settings": settings,
                "is_full_url": is_full_url,
                "model_map": {
                    tier: (
                        value.strip()
                        if isinstance(
                            value := env.get(
                                f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL"
                            ),
                            str,
                        )
                        and value.strip()
                        else None
                    )
                    for tier in ("opus", "sonnet", "haiku")
                },
            }
        return rows
    finally:
        conn.close()


def _sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    return (
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    )


def _resolve_database_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ProviderDatabaseError(
            "provider database path cannot be resolved"
        ) from exc


def _require_private_database(path: Path) -> None:
    _require_private_file(path, "provider database", ProviderDatabaseError)
    for sidecar in _sqlite_sidecars(path):
        if sidecar.exists():
            label = f"provider database {sidecar.name.removeprefix(path.name)}"
            _require_private_file(sidecar, label, ProviderDatabaseError)


def _snapshot_fingerprint(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _database_snapshot_state(path: Path) -> tuple:
    wal_path, _shm_path = _sqlite_sidecars(path)
    return (_snapshot_fingerprint(path), _snapshot_fingerprint(wal_path))


def _read_provider_snapshot(path: Path) -> dict:
    """Read a stable private main+WAL copy without opening the source SQLite DB."""
    wal_path, _shm_path = _sqlite_sidecars(path)
    last_error = None
    for _attempt in range(DB_SNAPSHOT_RETRIES):
        before = _database_snapshot_state(path)
        if before[0] is None:
            raise ProviderDatabaseError("provider database file is missing")
        try:
            with tempfile.TemporaryDirectory(prefix="claude-hub-db-") as temp_dir:
                snapshot = Path(temp_dir) / "providers.db"
                shutil.copyfile(path, snapshot)
                snapshot.chmod(0o600)
                if before[1] is not None:
                    snapshot_wal = snapshot.with_name(snapshot.name + "-wal")
                    shutil.copyfile(wal_path, snapshot_wal)
                    snapshot_wal.chmod(0o600)
                after = _database_snapshot_state(path)
                if before != after:
                    continue
                try:
                    return _read_provider_rows(snapshot)
                except sqlite3.Error as exc:
                    last_error = exc
        except (FileNotFoundError, OSError) as exc:
            last_error = exc
            continue
    raise ProviderDatabaseError(
        "provider database changed while taking a read-only snapshot"
    ) from last_error


def get_providers() -> dict:
    """Read current provider data from CC Switch using SQLite ``mode=ro``."""
    path = _resolve_database_path(db_path())
    _require_private_database(path)
    try:
        providers = _read_provider_snapshot(path)
        # Recheck because a writer can create WAL sidecars while the read is open.
        _require_private_database(path)
        return providers
    except (sqlite3.Error, OSError, ProviderPolicyError) as exc:
        raise ProviderDatabaseError(
            "provider database could not be read"
        ) from exc


def reset_caches() -> None:
    """Clear file caches. Primarily useful for isolated diagnostics and tests."""
    _cfg_cache.update({"path": None, "mtime_ns": None, "size": None, "raw": None})


# ---------------------------------------------------------------- routing


class RouteError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def anthropic_error(
    status: int, message: str, etype: str = "invalid_request_error"
) -> web.Response:
    return web.json_response(
        {"type": "error", "error": {"type": etype, "message": message}},
        status=status,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the supported finite range")
    return parsed


def _validate_json_unicode(value: object) -> None:
    """Reject lone surrogate code points before UTF-8 re-encoding."""
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, list):
        for item in value:
            _validate_json_unicode(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_json_unicode(key)
            _validate_json_unicode(item)


def route(
    model_in: str,
    cfg: dict,
    providers: dict | None = None,
) -> tuple[str, str]:
    """Map an incoming model field to ``(channel alias, upstream model)``."""
    model = (model_in or "").strip()
    if model.startswith("anthropic/"):
        model = model[len("anthropic/") :]
    if "," in model:
        alias, _, upstream_model = model.partition(",")
        alias, upstream_model = alias.strip().lower(), upstream_model.strip()
        if alias not in cfg["channels"]:
            raise RouteError(
                400, "unknown channel alias"
            )
        if not upstream_model:
            raise RouteError(400, "empty model after channel alias")
        return alias, upstream_model

    alias = cfg["default_channel"]
    channel = cfg["channels"].get(alias)
    if not channel:
        raise RouteError(500, "default channel is not present in config")
    if providers is None:
        providers = get_providers()
    provider = providers.get(channel["provider"])
    model_lower = model.lower()
    if provider:
        for tier in ("opus", "sonnet", "haiku"):
            mapped = provider["model_map"].get(tier)
            if tier in model_lower and mapped:
                return alias, mapped
    return alias, model


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_upstream_url(base_url: str, alias: str, allow_insecure_http: bool) -> None:
    """Reject unsafe remote upstream URLs before any network request is made."""
    try:
        parsed = urlparse(base_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise RouteError(502, "selected channel has an invalid upstream URL") from exc
    if (
        not parsed.hostname
        or parsed_port is not None
        and not 1 <= parsed_port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RouteError(502, "selected channel has an invalid upstream URL")
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (
        _is_loopback(parsed.hostname) or allow_insecure_http
    ):
        return
    if parsed.scheme == "http":
        raise RouteError(
            502,
            "selected channel uses remote HTTP; set allow_insecure_http=true "
            "only if it is intentional",
        )
    raise RouteError(502, "selected channel upstream must use http or https")


def resolve_provider(
    alias: str,
    cfg: dict,
    providers: dict | None = None,
) -> dict:
    channel = cfg["channels"].get(alias)
    if not channel:
        raise RouteError(400, "unknown channel alias")
    if providers is None:
        providers = get_providers()
    provider = providers.get(channel["provider"])
    if not provider:
        raise RouteError(
            502,
            "selected channel provider was not found in the CC Switch database "
            f"(check {config_path().name})",
        )
    if not provider.get("token"):
        raise RouteError(
            502,
            "selected channel provider has no explicit credential",
        )
    if provider.get("credential_ambiguous"):
        raise RouteError(
            502,
            "selected channel provider has ambiguous credential sources",
        )
    if provider.get("capability_error"):
        raise RouteError(
            502,
            f"channel '{alias}' has an invalid capability declaration; "
            "run `claude-hub doctor` for the redacted reason",
        )
    validate_upstream_url(
        provider["base_url"],
        alias,
        channel.get("allow_insecure_http", False),
    )
    resolved = dict(provider)
    try:
        profile = resolve_capability_profile(
            meta=provider.get("_capability_meta"),
            settings=provider.get("_capability_settings"),
            provider_type=provider.get("provider_type"),
            override=channel.get("capabilities"),
            protocol_override=channel.get("api_format"),
        )
    except ProviderPolicyError as exc:
        raise RouteError(
            502,
            "selected channel has an invalid capability profile",
        ) from exc
    resolved["capability_profile"] = profile
    resolved["api_format"] = str(profile.get("protocol"))
    return resolved


# ---------------------------------------------------------------- forwarding


def check_local_auth(request: web.Request, cfg: dict) -> bool:
    token = cfg["local_token"]
    authorization = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")
    return (
        hmac.compare_digest(authorization, f"Bearer {token}")
        or hmac.compare_digest(authorization, token)
        or hmac.compare_digest(api_key, token)
    )


def channel_proxy(alias: str, cfg: dict) -> str | None:
    """A channel proxy overrides the optional global proxy."""
    return cfg["channels"].get(alias, {}).get("proxy") or cfg.get("proxy") or None


def _header_values(headers, name: str) -> list[str]:
    if hasattr(headers, "getall"):
        return list(headers.getall(name, []))
    return [
        value
        for key, value in headers.items()
        if key.casefold() == name.casefold()
    ]


def _connection_header_tokens(headers) -> set[str]:
    tokens = set()
    for value in _header_values(headers, "connection"):
        tokens.update(
            part.strip().casefold()
            for part in value.split(",")
            if part.strip()
        )
    return tokens


def _has_unquoted_comma(value: str) -> bool:
    """Detect a folded singleton header without rejecting quoted parameters."""
    quoted = False
    escaped = False
    for character in value:
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "," and not quoted:
            return True
    return quoted or escaped


def ensure_1m_beta(headers, model_out: str) -> None:
    """Ensure and de-duplicate the context-1m beta marker for ``[1m]`` models."""
    if "[1m]" not in model_out.lower():
        return

    beta_values = []
    for value in _header_values(headers, "anthropic-beta"):
        beta_values.extend(
            part.strip() for part in value.split(",") if part.strip()
        )

    unique_values = []
    seen = set()
    has_context_1m = False
    for value in beta_values:
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        unique_values.append(value)
        if folded == CONTEXT_1M_BETA.casefold():
            has_context_1m = True
    if not has_context_1m:
        unique_values.append(CONTEXT_1M_BETA)

    if hasattr(headers, "popall"):
        headers.popall("anthropic-beta", None)
    else:
        for key in list(headers):
            if key.casefold() == "anthropic-beta":
                headers.pop(key, None)
    headers["anthropic-beta"] = ",".join(unique_values)


def apply_beta_policy(
    headers,
    profile: CapabilityProfile,
    model_out: str,
) -> None:
    """Apply one Provider's beta policy without leaking another route's result."""
    values = [
        part.strip()
        for value in _header_values(headers, "anthropic-beta")
        for part in value.split(",")
        if part.strip()
    ]
    policy = profile.get("beta_policy")
    context_window = profile.get("context_window")
    has_1m = (
        "[1m]" in model_out.casefold()
        and isinstance(context_window, int)
        and context_window >= 1_000_000
    )
    if policy == "passthrough":
        if has_1m:
            ensure_1m_beta(headers, model_out)
        return
    if policy == "filtered":
        allowed = {item.casefold() for item in profile.beta_allowlist}
        values = [item for item in values if item.casefold() in allowed]
    elif policy == "mapped":
        mapping = {key.casefold(): value for key, value in profile.beta_map}
        values = [
            mapping[item.casefold()]
            for item in values
            if item.casefold() in mapping
        ]

    if hasattr(headers, "popall"):
        headers.popall("anthropic-beta", None)
    else:
        for key in list(headers):
            if key.casefold() == "anthropic-beta":
                headers.pop(key, None)
    if values:
        headers["anthropic-beta"] = ",".join(dict.fromkeys(values))

    if has_1m:
        ensure_1m_beta(headers, model_out)


def _contains_tool_search(value: object) -> bool:
    if isinstance(value, list):
        return any(_contains_tool_search(item) for item in value)
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if isinstance(kind, str) and (
        kind.casefold().startswith("tool_search")
        or kind.casefold() == "tool_reference"
    ):
        return True
    name = value.get("name")
    if isinstance(name, str) and name.casefold() == "toolsearch":
        return True
    return any(_contains_tool_search(item) for item in value.values())


def _remove_cache_controls(value: object) -> bool:
    removed = False
    if isinstance(value, list):
        for item in value:
            removed |= _remove_cache_controls(item)
    elif isinstance(value, dict):
        if "cache_control" in value:
            value.pop("cache_control", None)
            removed = True
        for item in value.values():
            removed |= _remove_cache_controls(item)
    return removed


def _remove_reasoning_history(value: object) -> bool:
    removed = False
    if isinstance(value, list):
        kept = []
        for item in value:
            if (
                isinstance(item, dict)
                and item.get("type") in {"thinking", "redacted_thinking"}
            ):
                removed = True
                continue
            removed |= _remove_reasoning_history(item)
            kept.append(item)
        if removed:
            value[:] = kept
    elif isinstance(value, dict):
        for item in value.values():
            removed |= _remove_reasoning_history(item)
    return removed


def apply_request_capabilities(
    payload: dict,
    profile: CapabilityProfile,
) -> list[str]:
    """Apply safe request degradation and return non-sensitive reason codes."""
    # Direct launch refuses background_worker_safe=unsafe providers; the hub
    # must not serve them either, or the launcher policy could be bypassed.
    if profile.get("background_worker_safe") == "unsafe":
        raise RouteError(
            400,
            "the selected channel declares background workers unsafe; "
            "claude-hub refuses to serve it",
        )
    tools = payload.get("tools")
    if (
        profile.get("protocol") != "anthropic"
        and isinstance(tools, list)
        and any(
            isinstance(tool, dict) and tool.get("type") == "BatchTool"
            for tool in tools
        )
    ):
        raise RouteError(
            400,
            "the selected protocol bridge does not support this "
            "server-side tool schema",
        )
    if (
        profile.get("tool_search") != "supported"
        and _contains_tool_search(payload)
    ):
        raise RouteError(
            400,
            "tool search is disabled for this channel capability profile",
        )

    degraded: list[str] = []
    if profile.get("thinking") != "supported" and "thinking" in payload:
        payload.pop("thinking", None)
        degraded.append("thinking-disabled")
    if (
        profile.get("reasoning_round_trip") != "supported"
        and _remove_reasoning_history(payload.get("messages"))
    ):
        degraded.append("reasoning-history-disabled")
    if (
        profile.get("prompt_cache") != "supported"
        and _remove_cache_controls(payload)
    ):
        degraded.append("prompt-cache-disabled")
    return degraded


def apply_model_capabilities(
    model: str,
    profile: CapabilityProfile,
) -> str:
    if not model.casefold().endswith("[1m]"):
        return model
    context_window = profile.get("context_window")
    if not isinstance(context_window, int) or context_window < 1_000_000:
        raise RouteError(
            400,
            "the selected channel does not declare a verified 1M context window",
        )
    # Claude Code normally strips this marker.  Hub also strips it so opaque
    # third-party model IDs never receive a Claude-specific suffix.
    return model[:-4]


def capability_response_headers(
    profile: CapabilityProfile,
    degraded: list[str] | None = None,
) -> dict[str, str]:
    context = profile.get("context_window")
    headers = {
        "x-hub-context-window": (
            str(context) if isinstance(context, int) else "unknown"
        ),
        "x-hub-context-source": profile.source("context_window"),
    }
    if degraded:
        headers["x-hub-capability-degraded"] = ",".join(sorted(set(degraded)))
    return headers


def upstream_headers(request: web.Request, token: str) -> CIMultiDict:
    strip = REQ_STRIP | _connection_header_tokens(request.headers)
    headers = CIMultiDict()
    for key, value in request.headers.items():
        if key.casefold() not in strip:
            headers.add(key, value)
    headers["authorization"] = f"Bearer {token}"
    headers["x-api-key"] = token
    return headers


class _SSETerminalTracker:
    """Track bounded SSE lines without buffering the streamed response."""

    def __init__(self) -> None:
        self.terminal = False
        self.protocol_error = False
        self._line = bytearray()
        self._discarding_line = False
        self._event_type: bytes | None = None
        self._event_has_data = False
        self._skip_leading_lf = False
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="strict"
        )
        self._utf8_failed = False
        self._at_stream_start = True
        self._bom_prefix = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not self._utf8_failed:
            try:
                self._utf8_decoder.decode(chunk, final=False)
            except UnicodeDecodeError:
                self._utf8_failed = True
                self.protocol_error = True
        for parsed_chunk in self._without_initial_bom(chunk):
            self._feed_lines(parsed_chunk)

    def _without_initial_bom(self, chunk: bytes) -> tuple[bytes, ...]:
        if not self._at_stream_start or not chunk:
            return (chunk,) if chunk else ()
        needed = len(codecs.BOM_UTF8) - len(self._bom_prefix)
        take = min(needed, len(chunk))
        self._bom_prefix.extend(chunk[:take])
        prefix = bytes(self._bom_prefix)
        remainder = chunk[take:]
        if codecs.BOM_UTF8.startswith(prefix) and len(prefix) < 3:
            return ()
        self._at_stream_start = False
        self._bom_prefix.clear()
        if prefix == codecs.BOM_UTF8:
            return (remainder,) if remainder else ()
        parts = [prefix]
        if remainder:
            parts.append(remainder)
        return tuple(parts)

    def _feed_lines(self, chunk: bytes) -> None:
        start = 0
        if self._skip_leading_lf and chunk:
            if chunk[0] == 0x0A:
                start = 1
            self._skip_leading_lf = False
        for match in SSE_NEWLINE_RE.finditer(chunk, start):
            self._append_segment(chunk, start, match.start())
            if not self._discarding_line:
                self._consume_line(bytes(self._line))
            self._line.clear()
            self._discarding_line = False
            start = match.end()
            if match.group() == b"\r" and start == len(chunk):
                self._skip_leading_lf = True
        self._append_segment(chunk, start, len(chunk))

    @property
    def complete(self) -> bool:
        return self.terminal and not self.protocol_error

    def finish(self) -> None:
        if self._utf8_failed:
            return
        try:
            self._utf8_decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            self._utf8_failed = True
            self.protocol_error = True

    def _append_segment(self, chunk: bytes, start: int, end: int) -> None:
        length = end - start
        if length <= 0:
            return
        if self.terminal and not self._line and chunk[start] != 0x3A:
            # After message_stop/error, only blank lines and SSE comments are safe.
            self.protocol_error = True
        if self._discarding_line:
            return
        room = SSE_LINE_LIMIT - len(self._line)
        if length <= room:
            self._line.extend(memoryview(chunk)[start:end])
            return
        if room:
            self._line.extend(memoryview(chunk)[start : start + room])
        # A later event field overrides earlier event fields in the same event.
        # Preserve that semantic even when its value is too large to retain.
        if self._line.startswith(b"event:"):
            self._event_type = None
        elif self._line.startswith(b"data:"):
            self._event_has_data = True
        if self.terminal and not self._line.startswith(b":"):
            self.protocol_error = True
        self._line.clear()
        self._discarding_line = True

    def _consume_line(self, line: bytes) -> None:
        if self.terminal:
            if line and not line.startswith(b":"):
                self.protocol_error = True
            return
        if not line:
            if (
                self._event_has_data
                and self._event_type in (b"message_stop", b"error")
            ):
                self.terminal = True
            self._event_type = None
            self._event_has_data = False
            return
        field, separator, value = line.partition(b":")
        if field == b"data":
            self._event_has_data = True
            return
        if field != b"event":
            return
        if separator and value.startswith(b" "):
            value = value[1:]
        self._event_type = value if separator else b""


class _SSEContentDecoder:
    """Decode one SSE content-coding for validation while forwarding raw bytes."""

    _WBITS = {
        "gzip": 16 + zlib.MAX_WBITS,
        "x-gzip": 16 + zlib.MAX_WBITS,
        "deflate": zlib.MAX_WBITS,
    }

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self._decompressor = (
            None
            if encoding == "identity"
            else zlib.decompressobj(self._WBITS[encoding])
        )
        self._member_complete = False
        self._member_count = 1 if self._decompressor is not None else 0
        self._encoded_bytes = 0
        self._decoded_bytes = 0

    @classmethod
    def from_headers(cls, headers) -> "_SSEContentDecoder":
        encodings = [
            part.strip().casefold()
            for value in _header_values(headers, "content-encoding")
            for part in value.split(",")
            if part.strip() and part.strip().casefold() != "identity"
        ]
        if not encodings:
            return cls("identity")
        if len(encodings) != 1 or encodings[0] not in cls._WBITS:
            raise ValueError("unsupported SSE content encoding")
        return cls(encodings[0])

    def _new_gzip_member(self) -> None:
        if self._member_count >= SSE_GZIP_MEMBER_LIMIT:
            raise zlib.error("too many gzip members in SSE stream")
        self._decompressor = zlib.decompressobj(self._WBITS[self.encoding])
        self._member_complete = False
        self._member_count += 1

    def _count_decoded(self, length: int) -> None:
        self._decoded_bytes += length
        if self._decoded_bytes > SSE_DECODE_TOTAL_LIMIT:
            raise zlib.error("decoded SSE stream exceeds size limit")
        expansion_limit = (
            SSE_DECODE_SLACK
            + self._encoded_bytes * SSE_DECODE_RATIO_LIMIT
        )
        if self._decoded_bytes > expansion_limit:
            raise zlib.error("compressed SSE stream exceeds expansion limit")

    def feed(self, chunk: bytes):
        if self._decompressor is None:
            for start in range(0, len(chunk), SSE_DECODE_CHUNK):
                yield chunk[start : start + SSE_DECODE_CHUNK]
            return
        self._encoded_bytes += len(chunk)
        pending = chunk
        while True:
            if self._member_complete:
                if not pending:
                    return
                if self.encoding not in ("gzip", "x-gzip"):
                    raise zlib.error("data follows the deflate stream")
                self._new_gzip_member()
            decoded = self._decompressor.decompress(
                pending,
                SSE_DECODE_CHUNK,
            )
            if decoded:
                self._count_decoded(len(decoded))
                yield decoded
            if self._decompressor.eof:
                self._member_complete = True
                pending = self._decompressor.unused_data
                if pending:
                    continue
                return
            pending = self._decompressor.unconsumed_tail
            if pending:
                continue
            if len(decoded) == SSE_DECODE_CHUNK:
                pending = b""
                continue
            return

    def finish(self) -> None:
        if self._decompressor is None:
            return
        if not self._member_complete:
            raise zlib.error("incomplete compressed SSE stream")


def _estimated_input_tokens(payload: dict) -> int:
    """Bounded local fallback for formats without an Anthropic count endpoint."""
    relevant = {
        "messages": payload.get("messages"),
        "system": payload.get("system"),
        "tools": payload.get("tools"),
    }
    return max(
        1,
        len(json.dumps(relevant, ensure_ascii=False, separators=(",", ":"))) // 4,
    )


def _transformed_headers(token: str, streaming: bool) -> CIMultiDict:
    """Use only OpenAI-compatible headers; Anthropic beta headers are invalid here."""
    headers = CIMultiDict()
    headers["authorization"] = f"Bearer {token}"
    headers["content-type"] = "application/json"
    headers["accept"] = "text/event-stream" if streaming else "application/json"
    # Ask intermediaries not to compress translated SSE. A non-compliant upstream
    # is still handled by _SSEContentDecoder.
    headers["accept-encoding"] = "identity"
    return headers


async def _read_upstream_body(upstream, limit: int = 64 * 1024 * 1024) -> bytes:
    body = bytearray()
    async for chunk in upstream.content.iter_any():
        body.extend(chunk)
        if len(body) > limit:
            raise ProtocolTransformError("upstream response exceeds size limit")
    return bytes(body)


async def _handle_transformed_messages(
    request: web.Request,
    *,
    cfg: dict,
    provider: dict,
    payload: dict,
    alias: str,
    model_in: str,
    model_out: str,
    is_count: bool,
    started: float,
    degraded: list[str],
) -> web.StreamResponse:
    api_format = provider["api_format"]
    profile = provider["capability_profile"]
    if is_count:
        if profile.get("count_tokens") == "unsupported":
            return anthropic_error(
                501,
                "count_tokens is unsupported for this channel",
                "not_found_error",
            )
        estimate = _estimated_input_tokens(payload)
        log(
            f"{request.path} {api_format} locally estimated input tokens"
        )
        return web.json_response(
            {"input_tokens": estimate},
            headers={
                "x-hub-estimated": "1",
                **capability_response_headers(profile, degraded),
            },
        )

    endpoint, upstream_payload = transform_request(
        payload,
        api_format,
        provider_type=provider.get("provider_type"),
    )
    data = json.dumps(
        upstream_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    streaming = upstream_payload.get("stream") is True
    url = (
        provider["base_url"]
        if provider.get("is_full_url")
        else provider["base_url"] + endpoint
    )
    session = request.app.get(UPSTREAM_SESSION_KEY)
    if session is None:
        session = request.app["session"]
    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)

    try:
        async with session.post(
            url,
            data=data,
            headers=_transformed_headers(provider["token"], streaming),
            timeout=timeout,
            proxy=channel_proxy(alias, cfg),
            allow_redirects=False,
        ) as upstream:
            content_type = (
                upstream.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            is_sse = (
                upstream.status == 200
                and streaming
                and content_type == "text/event-stream"
            )
            if not is_sse:
                raw = await _read_upstream_body(upstream)
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = raw.decode("utf-8", "replace")
                if upstream.status >= 400:
                    body = transform_error(decoded, upstream.status)
                    log(
                        f"{request.path} {api_format} upstream "
                        f"{upstream.status}"
                    )
                    return web.json_response(body, status=upstream.status)
                if not isinstance(decoded, dict):
                    raise ProtocolTransformError(
                        "upstream returned a non-object JSON response"
                    )
                body = transform_response(decoded, api_format)
                log(
                    f"{request.path} {api_format} {upstream.status} json "
                    f"{time.monotonic() - started:.1f}s {len(raw)}B"
                )
                return web.json_response(
                    body,
                    status=upstream.status,
                    headers={
                        "x-hub-channel": alias,
                        "x-hub-model": model_out,
                        "x-hub-upstream-format": api_format,
                        **capability_response_headers(profile, degraded),
                    },
                )

            try:
                decoder = _SSEContentDecoder.from_headers(upstream.headers)
            except ValueError as exc:
                raise ProtocolTransformError(
                    "upstream SSE uses an unsupported content encoding"
                ) from exc
            parser = SSEParser()
            bridge = AnthropicStreamBridge(api_format)
            response = web.StreamResponse(
                status=200,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                    "x-hub-channel": alias,
                    "x-hub-model": model_out,
                    "x-hub-upstream-format": api_format,
                    **capability_response_headers(profile, degraded),
                },
            )
            await response.prepare(request)
            byte_count = 0
            try:
                async for chunk in upstream.content.iter_any():
                    for decoded_chunk in decoder.feed(chunk):
                        for event, event_data in parser.feed(decoded_chunk):
                            for translated in bridge.feed(event, event_data):
                                await response.write(translated)
                                byte_count += len(translated)
                    await asyncio.sleep(0)
                decoder.finish()
                parser.finish()
                for translated in bridge.finish():
                    await response.write(translated)
                    byte_count += len(translated)
                await response.write_eof()
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                UnicodeDecodeError,
                ProtocolTransformError,
                zlib.error,
            ) as exc:
                log(
                    f"{request.path} {api_format} stream failed after "
                    f"{byte_count}B: "
                    f"{type(exc).__name__}"
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "translated upstream stream ended after response started"
                ) from exc
            log(
                f"{request.path} {api_format} 200 stream "
                f"{time.monotonic() - started:.1f}s "
                f"{byte_count}B"
            )
            return response
    except UpstreamStreamAborted:
        raise
    except ProtocolTransformError as exc:
        log(
            f"{request.path} {api_format} TRANSFORM FAIL: "
            f"{type(exc).__name__}"
        )
        return anthropic_error(
            502,
            f"hub: selected channel returned an incompatible {api_format} response",
            "api_error",
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log(
            f"{request.path} {api_format} CONNECT FAIL: "
            f"{type(exc).__name__}"
        )
        return anthropic_error(
            502,
            "hub: cannot reach selected channel",
            "api_error",
        )


async def handle_messages(request: web.Request) -> web.StreamResponse:
    cfg = get_config()
    if not check_local_auth(request, cfg):
        return anthropic_error(
            401, "invalid local hub token", "authentication_error"
        )
    if _connection_header_tokens(request.headers) & REPRESENTATION_HEADERS:
        return anthropic_error(
            400,
            "Connection must not name request representation headers",
        )
    content_encodings = [
        part.strip().casefold()
        for value in _header_values(request.headers, "content-encoding")
        for part in value.split(",")
        if part.strip()
    ]
    if any(value != "identity" for value in content_encodings):
        return anthropic_error(
            415,
            "compressed request bodies are not supported by claude-hub",
        )

    body = await request.read()
    is_count = request.path.endswith("/count_tokens")
    try:
        payload = json.loads(
            body,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
        _validate_json_unicode(payload)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
    ):
        return anthropic_error(400, "request body must be valid JSON")
    if not isinstance(payload, dict):
        return anthropic_error(400, "request body must be a JSON object")
    model_in = payload.get("model")
    if not isinstance(model_in, str) or not model_in.strip():
        return anthropic_error(400, "model must be a non-empty string")

    started = time.monotonic()
    try:
        providers = await asyncio.to_thread(get_providers)
        alias, model_out = route(model_in, cfg, providers)
        provider = resolve_provider(alias, cfg, providers)
        profile = provider["capability_profile"]
        model_with_context_marker = model_out
        model_lookup = (
            model_out[:-4]
            if model_out.casefold().endswith("[1m]")
            else model_out
        )
        profile = profile.for_model(model_lookup)
        provider["capability_profile"] = profile
        model_out = apply_model_capabilities(model_out, profile)
        payload["model"] = model_out
        degraded = apply_request_capabilities(payload, profile)
    except RouteError as exc:
        log(f"{request.path} ROUTE ERROR {exc.status}")
        return anthropic_error(
            exc.status,
            exc.message,
            "api_error" if exc.status >= 500 else "invalid_request_error",
        )

    if provider.get("api_format", "anthropic") != "anthropic":
        return await _handle_transformed_messages(
            request,
            cfg=cfg,
            provider=provider,
            payload=payload,
            alias=alias,
            model_in=model_in,
            model_out=model_out,
            is_count=is_count,
            started=started,
            degraded=degraded,
        )
    count_mode = profile.get("count_tokens")
    if is_count and count_mode == "unsupported":
        return anthropic_error(
            501,
            "count_tokens is unsupported for this channel",
            "not_found_error",
        )
    if is_count and count_mode == "estimated":
        estimate = _estimated_input_tokens(payload)
        return web.json_response(
            {"input_tokens": estimate},
            headers={
                "x-hub-estimated": "1",
                **capability_response_headers(profile, degraded),
            },
        )
    try:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (UnicodeEncodeError, ValueError):
        return anthropic_error(400, "request body contains unsupported JSON values")

    path_and_query = request.path_qs
    url = provider["base_url"] + path_and_query
    headers = upstream_headers(request, provider["token"])
    apply_beta_policy(headers, profile, model_with_context_marker)
    proxy = channel_proxy(alias, cfg)
    session = request.app.get(UPSTREAM_SESSION_KEY)
    if session is None:
        # Kept for small fake-request fixtures that do not construct an aiohttp app.
        session = request.app["session"]
    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)

    try:
        async with session.post(
            url,
            data=data,
            headers=headers,
            timeout=timeout,
            proxy=proxy,
            allow_redirects=False,
        ) as upstream:
            response_connection_tokens = _connection_header_tokens(
                upstream.headers
            )
            content_types = _header_values(upstream.headers, "content-type")
            if (
                len(content_types) > 1
                or any(_has_unquoted_comma(value) for value in content_types)
                or response_connection_tokens & REPRESENTATION_HEADERS
            ):
                log(
                    f"{request.path} upstream returned ambiguous "
                    "representation headers"
                )
                return anthropic_error(
                    502,
                    "hub: selected channel returned invalid response headers",
                    "api_error",
                )
            content_type = content_types[0] if content_types else ""
            streamed = (
                upstream.status == 200
                and content_type.split(";", 1)[0].strip().casefold()
                == "text/event-stream"
            )
            if upstream.status >= 400:
                # Consume and discard the bounded upstream error body.  It may
                # echo private URLs, credentials or Provider account details.
                await _read_upstream_body(upstream)
                sanitized = transform_error({}, upstream.status)
                log(f"{request.path} upstream {upstream.status}")
                return web.json_response(
                    sanitized,
                    status=upstream.status,
                    headers=capability_response_headers(profile, degraded),
                )
            try:
                sse_decoder = (
                    _SSEContentDecoder.from_headers(upstream.headers)
                    if streamed
                    else None
                )
            except ValueError:
                log(
                    f"{request.path} upstream SSE used an unsupported "
                    "content encoding"
                )
                return anthropic_error(
                    502,
                    "hub: selected channel returned an unsupported SSE encoding",
                    "api_error",
                )

            response = web.StreamResponse(status=upstream.status)
            response.headers.update(
                capability_response_headers(profile, degraded)
            )
            response_strip = RESP_STRIP | response_connection_tokens
            for key, value in upstream.headers.items():
                if key.casefold() not in response_strip:
                    response.headers.add(key, value)
            response.headers["x-hub-channel"] = alias
            response.headers["x-hub-model"] = model_out
            await response.prepare(request)

            byte_count = 0
            sse_tracker = _SSETerminalTracker() if streamed else None
            try:
                async for chunk in upstream.content.iter_any():
                    if sse_tracker is not None:
                        for decoded in sse_decoder.feed(chunk):
                            sse_tracker.feed(decoded)
                            # Keep one highly compressible response from
                            # monopolizing the local gateway event loop.
                            await asyncio.sleep(0)
                    await response.write(chunk)
                    byte_count += len(chunk)
                if sse_tracker is not None:
                    sse_decoder.finish()
                    sse_tracker.finish()
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                zlib.error,
            ) as exc:
                log(
                    f"{request.path} upstream broke or was invalid after "
                    f"{byte_count}B: "
                    f"{type(exc).__name__}"
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "upstream stream ended after downstream response started"
                ) from exc

            if sse_tracker is not None and not sse_tracker.complete:
                log(
                    f"{request.path} upstream SSE ended without a valid "
                    f"terminal event after "
                    f"{byte_count}B"
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "upstream SSE ended without a valid message_stop or error"
                )

            await response.write_eof()
            log(
                f"{request.path} {upstream.status} "
                f"{'stream' if streamed else 'json'} "
                f"{time.monotonic() - started:.1f}s {byte_count}B"
            )
            return response
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log(
            f"{request.path} CONNECT FAIL: {type(exc).__name__}"
        )
        return anthropic_error(
            502,
            "hub: cannot reach selected channel",
            "api_error",
        )


async def handle_models(request: web.Request) -> web.Response:
    cfg = get_config()
    if not check_local_auth(request, cfg):
        return anthropic_error(
            401, "invalid local hub token", "authentication_error"
        )
    data = []
    for alias, channel in cfg["channels"].items():
        for model in channel.get("models", []):
            data.append(
                {
                    "id": f"anthropic/{alias},{model}",
                    "type": "model",
                    "display_name": f"[{alias}] {model}",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )
    return web.json_response(
        {
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }
    )


async def handle_healthz(_request: web.Request | None) -> web.Response:
    # Public and deliberately static: never expose channels, providers, hosts or tokens.
    return web.json_response(dict(HEALTH_PAYLOAD))


async def handle_fallback(request: web.Request) -> web.Response:
    return anthropic_error(
        404,
        f"claude-hub: no route for {request.method} {request.path}",
        "not_found_error",
    )


# ---------------------------------------------------------------- server


@web.middleware
async def controlled_error_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    """Keep local configuration/DB failures out of aiohttp HTML responses."""
    try:
        return await handler(request)
    except UpstreamStreamAborted as exc:
        # The downstream transport is already aborted. Cancellation prevents
        # aiohttp from attempting a normal write_eof or rendering a second body.
        raise asyncio.CancelledError from exc
    except ConfigError:
        log(f"{request.method} {request.path}: configuration unavailable")
        return anthropic_error(
            503,
            "claude-hub configuration is unavailable; run `claude-hub doctor`",
            "api_error",
        )
    except (ProviderDatabaseError, sqlite3.Error) as exc:
        log(
            f"{request.method} {request.path}: provider database unavailable: "
            f"{type(exc).__name__}"
        )
        return anthropic_error(
            503,
            "claude-hub provider database is unavailable; run `claude-hub doctor`",
            "api_error",
        )


async def _client_session_context(app: web.Application):
    session = aiohttp.ClientSession(
        auto_decompress=False,
        skip_auto_headers={"Accept-Encoding"},
    )
    app[UPSTREAM_SESSION_KEY] = session
    try:
        yield
    finally:
        await session.close()


def create_app() -> web.Application:
    app = web.Application(
        client_max_size=64 * 1024 * 1024,
        middlewares=[controlled_error_middleware],
    )
    app.cleanup_ctx.append(_client_session_context)
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_post("/v1/messages/count_tokens", handle_messages)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_route("*", "/{tail:.*}", handle_fallback)
    return app


async def run_server(fg: bool) -> None:
    global _log_stderr
    _log_stderr = fg
    open_log()
    cfg = get_config()
    # Fail before binding when the credential-bearing DB is unreadable or unsafe.
    get_providers()
    app = create_app()

    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", cfg["port"])
            await site.start()
        except OSError as exc:
            log(f"FATAL: cannot bind 127.0.0.1:{cfg['port']}: {exc}")
            raise

        log(
            f"claude-hub listening on 127.0.0.1:{cfg['port']} "
            f"({len(cfg['channels'])} configured channels)"
        )
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------- CLI


def cli_list() -> None:
    cfg = get_config()
    providers = get_providers()
    print(f"claude-hub channels (port {cfg['port']}, default *):\n")
    width = max(len(alias) for alias in cfg["channels"])
    for alias, channel in cfg["channels"].items():
        provider = providers.get(channel["provider"])
        marker = "*" if alias == cfg["default_channel"] else " "
        if provider is None:
            status = "provider missing from DB"
        elif provider.get("capability_error"):
            status = "capability declaration invalid"
        else:
            status = "ready"
        print(
            f" {marker}{alias:<{width}}  {channel['provider']:<16} "
            f"{status:<24} {', '.join(channel.get('models', []))}"
        )
    default_models = cfg["channels"][cfg["default_channel"]].get("models", [])
    if default_models:
        print(
            f"\nUsage: /model alias,model   Example: "
            f"/model {cfg['default_channel']},{default_models[0]}"
        )


def cli_doctor() -> int:
    """Run read-only local readiness checks without contacting any provider."""
    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        if level == "FAIL":
            failures += 1
        print(f"  {level:<4} {message}")

    print("claude-hub doctor (local, read-only)\n")

    cfg_path = config_path()
    cfg_issue = _private_file_issue(cfg_path, "config")
    if cfg_issue:
        report("FAIL", cfg_issue)
    elif os.name == "posix":
        report("OK", "config exists and permissions do not exceed 0600")
    else:
        report("SKIP", "config permission mode unavailable on this platform")

    cfg = None
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg = validate_config(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigError):
        report("FAIL", "config cannot be parsed or is incomplete")
    else:
        report("OK", "config parses and local authentication is ready")
        report("OK", f"listen address is 127.0.0.1:{cfg['port']}")

    provider_path = None
    try:
        provider_path = _resolve_database_path(db_path())
    except ProviderDatabaseError:
        report("FAIL", "provider database path cannot be resolved")
    if provider_path is not None:
        db_issue = _private_file_issue(provider_path, "provider database")
        if db_issue:
            report("FAIL", db_issue)
        elif os.name == "posix":
            report(
                "OK",
                "provider database exists and permissions do not exceed 0600",
            )
        else:
            report(
                "SKIP",
                "provider database permission mode unavailable on this platform",
            )
        for sidecar in _sqlite_sidecars(provider_path):
            if not sidecar.exists():
                continue
            suffix = sidecar.name.removeprefix(provider_path.name)
            sidecar_issue = _private_file_issue(
                sidecar,
                f"provider database {suffix}",
            )
            if sidecar_issue:
                report("FAIL", sidecar_issue)
            elif os.name == "posix":
                report("OK", f"provider database {suffix} permissions are private")
            else:
                report(
                    "SKIP",
                    f"provider database {suffix} permission mode unavailable",
                )

    providers = None
    if provider_path is not None:
        try:
            providers = _read_provider_snapshot(provider_path)
        except (sqlite3.Error, OSError, ProviderDatabaseError):
            report("FAIL", "provider database cannot be read")
        else:
            report("OK", "provider database opens read-only")

    if cfg is not None and providers is not None:
        for index, (alias, channel) in enumerate(cfg["channels"].items(), 1):
            channel_label = f"channel #{index}"
            problems = []
            models = channel.get("models", [])
            if not models:
                problems.append("no selectable models")
            provider = providers.get(channel["provider"])
            if provider is None:
                problems.append("provider missing")
            else:
                if not provider.get("token"):
                    problems.append("credential missing")
                if provider.get("credential_ambiguous"):
                    problems.append("credential sources ambiguous")
                if provider.get("capability_error"):
                    problems.append(
                        "capability declaration invalid "
                        f"({provider['capability_error']})"
                    )
                try:
                    validate_upstream_url(
                        provider.get("base_url", ""),
                        alias,
                        channel.get("allow_insecure_http", False),
                    )
                except RouteError:
                    problems.append("upstream policy invalid")
            if problems:
                report("FAIL", f"{channel_label}: {', '.join(problems)}")
            else:
                try:
                    resolved = resolve_provider(alias, cfg, providers)
                    profile = resolved["capability_profile"]
                except (RouteError, ProviderPolicyError):
                    report("FAIL", f"{channel_label}: capability profile invalid")
                    continue
                report("OK", f"{channel_label} ready ({len(models)} models)")
                report(
                    "INFO",
                    f"{channel_label} credential_source="
                    f"{resolved.get('credential_source', 'missing')}",
                )
                for line in capability_summary(profile):
                    report("INFO", f"{channel_label} capabilities: {line}")

    print(
        "\nResult: "
        + ("ready" if failures == 0 else f"not ready ({failures} failed checks)")
    )
    print("No provider connection was attempted. Use `claude-hub check` explicitly.")
    return 0 if failures == 0 else 1


async def cli_check(target: str | None) -> None:
    cfg = get_config()
    aliases = [target] if target else list(cfg["channels"])
    if target and target not in cfg["channels"]:
        print(f"unknown channel alias: {target}")
        raise SystemExit(1)

    async def one(session: aiohttp.ClientSession, alias: str) -> tuple[str, str]:
        channel = cfg["channels"][alias]
        try:
            provider = resolve_provider(alias, cfg)
        except RouteError as exc:
            return alias, f"✗ {exc.message}"
        model = (
            channel["models"][0]
            if channel.get("models")
            else "claude-sonnet-4"
        )
        profile = provider["capability_profile"]
        model_with_context_marker = model
        model_lookup = (
            model[:-4] if model.casefold().endswith("[1m]") else model
        )
        profile = profile.for_model(model_lookup)
        try:
            model = apply_model_capabilities(model, profile)
        except RouteError as exc:
            return alias, f"✗ {exc.message}"
        headers = {
            "authorization": f"Bearer {provider['token']}",
            "x-api-key": provider["token"],
            "anthropic-version": "2023-06-01",
            "user-agent": "claude-cli/2.1.0 (external, cli)",
            "x-app": "cli",
            "anthropic-beta": "claude-code-20250219",
        }
        apply_beta_policy(headers, profile, model_with_context_marker)
        test_payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
        api_format = str(profile.get("protocol"))
        if api_format == "anthropic":
            upstream_url = provider["base_url"] + "/v1/messages"
            upstream_payload = test_payload
            upstream_headers_value = headers
        else:
            endpoint, upstream_payload = transform_request(
                test_payload,
                api_format,
                provider_type=provider.get("provider_type"),
            )
            upstream_url = (
                provider["base_url"]
                if provider.get("is_full_url")
                else provider["base_url"] + endpoint
            )
            upstream_headers_value = _transformed_headers(
                provider["token"],
                False,
            )
        started = time.monotonic()
        try:
            async with session.post(
                upstream_url,
                json=upstream_payload,
                headers=upstream_headers_value,
                proxy=channel_proxy(alias, cfg),
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                duration = time.monotonic() - started
                if response.status == 200:
                    return alias, f"✓ 200 {duration:.1f}s"
                await response.read()
                return alias, f"✗ {response.status} {duration:.1f}s"
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return alias, f"✗ {type(error).__name__}"

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(one(session, alias) for alias in aliases))
    for index, (_alias, message) in enumerate(results, 1):
        print(f"  channel #{index:<2}  {message}")


def cli_logs(args: list[str]) -> None:
    path = log_path()
    if not path.exists():
        print(f"no log yet: {path}")
        return
    if "-f" in args:
        os.execvp("tail", ["tail", "-f", str(path)])
    lines = "20"
    if "-n" in args:
        try:
            lines = args[args.index("-n") + 1]
        except IndexError:
            pass
    os.execvp("tail", ["tail", "-n", lines, str(path)])


USAGE = """claude-hub — local multi-channel Anthropic gateway

Usage:
  claude-hub serve [--fg]     start the gateway
  claude-hub list             show configured channels and models
  claude-hub doctor           run local read-only readiness checks
  claude-hub check [alias]    make a real one-token connectivity check
  claude-hub logs [-n N|-f]   show routing logs

Environment:
  CLAUDE_HUB_CONFIG           config JSON path
  CLAUDE_HUB_DB               CC Switch SQLite path
  CLAUDE_HUB_LOG              log path
  CLAUDE_HUB_PORT             listen-port override
  CLAUDE_HUB_LOCAL_TOKEN      local auth-token override
"""


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else None
    try:
        if command == "serve":
            asyncio.run(run_server(fg="--fg" in args))
        elif command == "list":
            cli_list()
        elif command == "doctor":
            status = cli_doctor()
            if status:
                raise SystemExit(status)
        elif command == "check":
            asyncio.run(cli_check(args[1] if len(args) > 1 else None))
        elif command == "logs":
            cli_logs(args[1:])
        else:
            print(USAGE)
            raise SystemExit(
                0 if command in (None, "help", "-h", "--help") else 1
            )
    except ConfigError as exc:
        print(f"claude-hub: config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except ProviderDatabaseError as exc:
        print(
            "claude-hub: provider database error; run `claude-hub doctor`",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
