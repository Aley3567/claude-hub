#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.9", "certifi>=2024.2.2"]
# ///
"""claude-hub — local multi-channel Anthropic gateway.

The gateway routes ``/model channel,model`` selections inside one Claude Code
session. Provider endpoints and credentials are read from the CC Switch SQLite
database in read-only mode; this file never contains provider credentials.
"""

from __future__ import annotations

import asyncio
import codecs
import copy
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import ssl
import stat
import sys
import tempfile
import time
import zlib
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import certifi
from aiohttp import web
from multidict import CIMultiDict

from claude1_protocol import (
    AnthropicStreamBridge,
    ProtocolRequestError,
    ProtocolTransformError,
    SSEParser,
    prepare_request,
    prepare_response,
    protocol_capability_matrix,
    provider_api_format,
    sse_event,
    transform_error,
    transform_request,
    transform_response,
)
from claude1_account_pool import (
    AccountCandidate,
    AccountLease,
    AccountPool,
    AccountPoolError,
    PoolConfigError,
    PoolExhausted,
    PoolStateError,
    credential_fingerprint,
    normalize_account_endpoint,
)
from claude1_transport import (
    TransportConfigError,
    UpstreamExecutor,
    normalize_transport_config,
    resolve_transport_policy,
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
DEFAULT_USAGE_PATH = HOME / ".cc-switch" / "logs" / "claude-hub-usage.jsonl"
DEFAULT_ACCOUNT_POOL_CONFIG_PATH = (
    HOME / ".cc-switch" / "claude1-account-pools.json"
)
DEFAULT_ACCOUNT_POOL_STATE_PATH = (
    HOME / ".cc-switch" / "claude1-account-state.sqlite3"
)

ENV_CONFIG = "CLAUDE_HUB_CONFIG"
ENV_DB = "CLAUDE_HUB_DB"
ENV_LOG = "CLAUDE_HUB_LOG"
ENV_USAGE = "CLAUDE_HUB_USAGE"
ENV_PORT = "CLAUDE_HUB_PORT"
ENV_LOCAL_TOKEN = "CLAUDE_HUB_LOCAL_TOKEN"
ENV_LISTEN_FD = "CLAUDE_HUB_LISTEN_FD"
ENV_ACCOUNT_POOL_CONFIG = "CLAUDE1_ACCOUNT_POOL_CONFIG"
ENV_ACCOUNT_POOL_STATE = "CLAUDE1_ACCOUNT_POOL_STATE"

LOG_MAX_BYTES = 10 * 1024 * 1024
USAGE_LOG_MAX_BYTES = 10 * 1024 * 1024
MAX_UPSTREAM_BODY_BYTES = 64 * 1024 * 1024
UPSTREAM_KEEPALIVE_IDLE_SECONDS = 30
UPSTREAM_KEEPALIVE_INTERVAL_SECONDS = 15
UPSTREAM_KEEPALIVE_PROBES = 4
CONTEXT_1M_BETA = "context-1m-2025-08-07"
HUB_SLOT_ORDER = ("fable", "opus", "sonnet", "haiku")
HUB_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}
HUB_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
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

# Model selectors naming an explicit route group use the ``route:<name>``
# prefix. Only these pre-commit rejections may cross a provider boundary
# inside a route group (design doc section 4); every other failure is final.
ROUTE_GROUP_PREFIX = "route:"
ROUTE_FAILOVER_STATUSES = (401, 403, 429)

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
    "accept-encoding",
    "authorization",
    "x-api-key",
}
RESP_STRIP = HOP_BY_HOP | {"content-length"}

_log_fp = None
_log_stderr = False
_usage_fp = None


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def config_path() -> Path:
    return _env_path(ENV_CONFIG, DEFAULT_CONFIG_PATH)


def db_path() -> Path:
    return _env_path(ENV_DB, DEFAULT_DB_PATH)


def log_path() -> Path:
    return _env_path(ENV_LOG, DEFAULT_LOG_PATH)


def usage_path() -> Path:
    return _env_path(ENV_USAGE, DEFAULT_USAGE_PATH)


def account_pool_config_path() -> Path:
    return _env_path(ENV_ACCOUNT_POOL_CONFIG, DEFAULT_ACCOUNT_POOL_CONFIG_PATH)


def account_pool_state_path() -> Path:
    return _env_path(ENV_ACCOUNT_POOL_STATE, DEFAULT_ACCOUNT_POOL_STATE_PATH)


def log(msg: str) -> None:
    # Request-controlled model names and upstream error strings must stay on
    # one physical line, otherwise they can forge log entries.
    msg = str(msg).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    if _log_fp:
        _log_fp.write(line + "\n")
        _log_fp.flush()
        _rotate_open_log_if_needed()
    if _log_stderr or not _log_fp:
        print(line, file=sys.stderr)


def _rotate_open_log_if_needed() -> None:
    """Rotate an already-open log after a write crosses the size limit."""
    global _log_fp
    if _log_fp is None:
        return
    try:
        if os.fstat(_log_fp.fileno()).st_size <= LOG_MAX_BYTES:
            return
        _log_fp.close()
        _log_fp = None
        open_log()
    except (OSError, RuntimeError, ValueError):
        # Logging should never take down request forwarding.  A later write or
        # restart will retry normal rotation with open_log's path checks.
        pass


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


def _usage_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _open_usage_log():
    """Open the usage JSONL privately, rotating one bounded backup if needed."""
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    expected_current = current
    if current is not None:
        if not stat.S_ISREG(current.st_mode):
            raise RuntimeError("usage path is not a regular file")
        if current.st_size > USAGE_LOG_MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            path.replace(rotated)
            expected_current = None
            rotated_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                rotated_flags |= os.O_NOFOLLOW
            rotated_fd = os.open(rotated, rotated_flags)
            try:
                if not stat.S_ISREG(os.fstat(rotated_fd).st_mode):
                    raise RuntimeError("rotated usage path is not a regular file")
                if os.name == "posix":
                    os.fchmod(rotated_fd, 0o600)
            finally:
                os.close(rotated_fd)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("usage path is not a regular file")
        if expected_current is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != (
            expected_current.st_dev,
            expected_current.st_ino,
        ):
            raise RuntimeError("usage path changed while it was being opened")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def record_usage(
    alias: str,
    model_out: str,
    api_format: str,
    usage: dict | None,
    *,
    instance_id: str | None = None,
    account_id: str | None = None,
    source: str = "upstream",
) -> None:
    """把一条请求的 token 用量追加到 JSONL。统计绝不能搞挂转发主路径，全部异常静默。"""
    try:
        usage = usage if isinstance(usage, dict) else {}
        row = {
            "ts": int(time.time()),
            "channel": alias,
            "model": model_out,
            "format": api_format,
            "source": source,
        }
        for source_key, target_key in (
            ("input_tokens", "in"),
            ("output_tokens", "out"),
            ("cache_read_input_tokens", "cr"),
            ("cache_creation_input_tokens", "cw"),
        ):
            value = _usage_int(usage.get(source_key))
            if value is not None:
                row[target_key] = value
        for detail_key in ("cache_creation", "server_tool_use"):
            detail = usage.get(detail_key)
            if isinstance(detail, dict) and all(
                isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for key, value in detail.items()
            ):
                row[detail_key] = dict(detail)
        if instance_id is not None:
            row["hub"] = instance_id
        if account_id is not None:
            row["account"] = account_id
        global _usage_fp
        if _usage_fp is None:
            _usage_fp = _open_usage_log()
        _usage_fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        _usage_fp.flush()
        if os.fstat(_usage_fp.fileno()).st_size > USAGE_LOG_MAX_BYTES:
            _usage_fp.close()
            _usage_fp = None
            _usage_fp = _open_usage_log()
    except Exception:
        pass


def _usage_from_json_bytes(
    raw: bytes | bytearray,
    headers=None,
) -> dict | None:
    """Best-effort usage extraction, including a bounded shadow decode.

    Native Anthropic responses remain byte-for-byte transparent to the client.
    When the representation is compressed, this local copy is decoded only for
    usage telemetry; malformed or oversized representations simply make that
    telemetry unavailable.
    """
    if headers is not None:
        try:
            decoder = _SSEContentDecoder.from_headers(headers)
            decoded = bytearray()
            for part in decoder.feed(bytes(raw)):
                if len(decoded) + len(part) > MAX_UPSTREAM_BODY_BYTES:
                    return None
                decoded.extend(part)
            decoder.finish()
            raw = decoded
        except (TypeError, ValueError, zlib.error):
            return None
    try:
        body = json.loads(bytes(raw))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        return body["usage"]
    return None


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


def _normalize_base_url(value: object) -> str:
    # Forwarded paths already begin with /v1. Avoid /v1/v1/messages.
    return normalize_account_endpoint(value)


def _validate_routes(
    raw: object,
    channels: dict[str, dict],
    providers: dict | None = None,
) -> dict[str, dict]:
    """Validate explicit provider route groups against declared channels.

    Each route group is an ordered list of ``{"channel", "model"}`` targets;
    every target must reference an existing channel and a model that channel
    declares, so model IDs are never guessed across providers. An optional
    ``requires`` list names protocol capabilities that must not be rejected
    by the target's effective API format. ``providers`` supplies the CC
    Switch snapshot used to resolve that format exactly the way runtime
    dispatch does; without it only channel-declared overrides are checked.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("routes must be an object")
    matrix = protocol_capability_matrix()
    routes: dict[str, dict] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("route names must be non-empty strings")
        if name != name.strip().lower():
            raise ConfigError(
                f"route name '{name}' must be lowercase without outer spaces"
            )
        requires_raw: object = []
        targets_raw: object = entry
        if isinstance(entry, dict):
            targets_raw = entry.get("targets")
            requires_raw = entry.get("requires", [])
        if not isinstance(targets_raw, list) or not targets_raw:
            raise ConfigError(f"routes.{name} must be a non-empty list of targets")
        if not isinstance(requires_raw, list) or any(
            not isinstance(capability, str) for capability in requires_raw
        ):
            raise ConfigError(f"routes.{name}.requires must be a list of strings")
        requires = []
        for capability in requires_raw:
            if capability not in matrix:
                raise ConfigError(
                    f"routes.{name}.requires names unknown capability "
                    f"'{capability}'"
                )
            requires.append(capability)
        targets: list[dict] = []
        for index, target_raw in enumerate(targets_raw):
            where = f"routes.{name}[{index}]"
            if not isinstance(target_raw, dict):
                raise ConfigError(f"{where} must be an object")
            alias = target_raw.get("channel")
            if not isinstance(alias, str) or not alias.strip():
                raise ConfigError(f"{where}.channel must be a non-empty string")
            alias = alias.strip().lower()
            channel = channels.get(alias)
            if channel is None:
                raise ConfigError(
                    f"{where}.channel references unknown channel '{alias}'"
                )
            model = target_raw.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ConfigError(f"{where}.model must be a non-empty string")
            model = model.strip()
            if model not in channel["models"]:
                raise ConfigError(
                    f"{where}.model '{model}' is not declared in "
                    f"channels.{alias}.models"
                )
            # Resolve the target's effective API format the same way runtime
            # dispatch does: an explicit channel override wins, otherwise the
            # provider record carries the format that provider_api_format()
            # derived from the database meta when the snapshot was read.
            api_format = channel.get("api_format")
            if not api_format and providers:
                provider = _match_channel_provider(channel, providers)
                if provider is not None:
                    api_format = provider.get("api_format")
            api_format = api_format or "anthropic"
            for capability in requires:
                if matrix[capability].get(api_format) == "reject":
                    raise ConfigError(
                        f"{where} cannot satisfy required capability "
                        f"'{capability}': the {api_format} adapter rejects it"
                    )
            targets.append({"channel": alias, "model": model})
        routes[name] = {"targets": targets, "requires": requires}
    return routes


def validate_config(raw: object, providers: dict | None = None) -> dict:
    """Validate and return a normalized, detached configuration dictionary."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    version = raw.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ConfigError("version must be 1 or 2")

    instance_id = None
    if "instance_id" in raw:
        instance_id_raw = raw["instance_id"]
        if not isinstance(instance_id_raw, str) or not HUB_INSTANCE_ID_RE.fullmatch(
            instance_id_raw
        ):
            raise ConfigError(
                "instance_id must be 1-128 ASCII letters, digits, dots, underscores, "
                "or hyphens, beginning with a letter or digit"
            )
        instance_id = instance_id_raw

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

        provider_raw = channel_raw.get("provider")
        provider = (
            provider_raw.strip()
            if isinstance(provider_raw, str) and provider_raw.strip()
            else ""
        )
        provider_base_url = _normalize_base_url(channel_raw.get("base_url"))
        if not provider and not provider_base_url:
            raise ConfigError(
                f"channels.{alias}.provider or base_url must be a non-empty string"
            )
        models_raw = channel_raw.get("models", [])
        if not isinstance(models_raw, list) or any(
            not isinstance(model, str) or not model.strip() for model in models_raw
        ):
            raise ConfigError(f"channels.{alias}.models must be a list of non-empty strings")

        allow_insecure = channel_raw.get("allow_insecure_http", False)
        if not isinstance(allow_insecure, bool):
            raise ConfigError(f"channels.{alias}.allow_insecure_http must be a boolean")

        route_unknown = channel_raw.get("route_unknown_to_default", False)
        if not isinstance(route_unknown, bool):
            raise ConfigError(
                f"channels.{alias}.route_unknown_to_default must be a boolean"
            )

        channel = {
            "provider": provider,
            "provider_base_url": provider_base_url,
            "models": [model.strip() for model in models_raw],
            "allow_insecure_http": allow_insecure,
            "route_unknown_to_default": route_unknown,
        }
        if "transport" in channel_raw:
            try:
                channel["transport"] = normalize_transport_config(
                    channel_raw["transport"]
                )
            except TransportConfigError as exc:
                raise ConfigError(f"channels.{alias}.{exc}") from exc
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

    routes = _validate_routes(raw.get("routes"), channels, providers)

    launch_slot = None
    model_slots = None
    effort_by_slot = None
    if version == 2:
        launch_slot = raw.get("launch_slot")
        if launch_slot not in HUB_SLOT_ORDER:
            raise ConfigError("launch_slot must be fable, opus, sonnet, or haiku")
        model_slots = raw.get("model_slots")
        if not isinstance(model_slots, dict):
            raise ConfigError("model_slots must be an object")
        normalized_slots: dict[str, str] = {}
        for slot in HUB_SLOT_ORDER:
            selector = model_slots.get(slot)
            if not isinstance(selector, str):
                raise ConfigError(f"model_slots.{slot} must be channel,model")
            alias, separator, model = selector.strip().partition(",")
            alias, model = alias.strip().lower(), model.strip()
            if (
                not separator
                or alias not in channels
                or model not in channels[alias]["models"]
            ):
                raise ConfigError(
                    f"model_slots.{slot} must reference a declared channel model"
                )
            normalized_slots[slot] = f"{alias},{model}"
        model_slots = normalized_slots
        effort_by_slot = raw.get("effort_by_slot")
        if not isinstance(effort_by_slot, dict):
            raise ConfigError("effort_by_slot must be an object")
        normalized_efforts: dict[str, str] = {}
        for slot in HUB_SLOT_ORDER:
            effort = effort_by_slot.get(slot)
            if effort not in HUB_EFFORT_LEVELS:
                raise ConfigError(
                    f"effort_by_slot.{slot} must be low, medium, high, or xhigh"
                )
            normalized_efforts[slot] = effort
        effort_by_slot = normalized_efforts

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

    try:
        transport = normalize_transport_config(raw.get("transport"))
    except TransportConfigError as exc:
        raise ConfigError(str(exc)) from exc

    return {
        "version": version,
        "instance_id": instance_id,
        "port": _config_port(raw.get("port")),
        "local_token": local_token,
        "default_channel": default_channel,
        "channels": channels,
        "routes": routes,
        "proxy": proxy.strip() if isinstance(proxy, str) else None,
        "transport": transport,
        "launch_slot": launch_slot,
        "model_slots": model_slots,
        "effort_by_slot": effort_by_slot,
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
    raw = _cfg_cache["raw"]
    providers = None
    routes_raw = raw.get("routes") if isinstance(raw, dict) else None
    if isinstance(routes_raw, dict) and any(
        isinstance(entry, dict) and entry.get("requires")
        for entry in routes_raw.values()
    ):
        # ``requires`` capability checks must resolve each target's effective
        # API format from the provider database, not just channel overrides.
        providers = get_providers()
    return validate_config(raw, providers)


def _read_provider_rows(path: Path) -> dict:
    """Read provider rows without mutating the database or contacting providers."""
    db_uri = path.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(providers)").fetchall()
        }
        selected = ["name", "settings_config"]
        if "id" in columns:
            selected.insert(0, "id")
        selected.extend(
            column for column in ("meta", "provider_type") if column in columns
        )
        cursor = conn.execute(
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type='claude'"
        )
        records: list[tuple[str, str, dict]] = []
        for raw_row in cursor.fetchall():
            values = dict(zip(selected, raw_row))
            name = values["name"]
            provider_id = str(values.get("id") or name)
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
            base = normalize_account_endpoint(
                raw_base,
                is_full_url=is_full_url,
            )
            if not base:
                continue
            auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
            api_key = env.get("ANTHROPIC_API_KEY")
            if isinstance(auth_token, str) and auth_token:
                token = auth_token
                credential_type = "ANTHROPIC_AUTH_TOKEN"
            elif isinstance(api_key, str) and api_key:
                token = api_key
                credential_type = "ANTHROPIC_API_KEY"
            else:
                token = ""
                credential_type = ""
            folded_env = {
                str(key).upper(): value for key, value in env.items()
            }
            proxy_key = "HTTPS_PROXY" if base.startswith("https://") else "HTTP_PROXY"
            raw_proxy = folded_env.get(proxy_key) or folded_env.get("ALL_PROXY")
            provider_proxy = (
                raw_proxy.strip()
                if isinstance(raw_proxy, str) and raw_proxy.strip()
                else None
            )
            provider_transport = None
            transport_error = None
            if "transport" in settings:
                try:
                    provider_transport = normalize_transport_config(
                        settings["transport"]
                    )
                except TransportConfigError as exc:
                    transport_error = str(exc)
            record = {
                "selector": f"id:{provider_id}",
                "name": name,
                "base_url": base,
                "token": token,
                "credential_type": credential_type,
                "proxy": provider_proxy,
                "transport": provider_transport,
                "transport_error": transport_error,
                "api_format": provider_api_format(
                    meta=meta,
                    settings=settings,
                    provider_type=values.get("provider_type"),
                ),
                "provider_type": (
                    values.get("provider_type") or meta.get("providerType")
                ),
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
                    for tier in ("opus", "sonnet", "haiku", "fable")
                },
            }
            records.append((provider_id, name, record))
        name_counts: dict[str, int] = {}
        for _provider_id, name, _record in records:
            name_counts[name] = name_counts.get(name, 0) + 1
        rows = {}
        for provider_id, name, record in records:
            rows[f"id:{provider_id}"] = record
            if name_counts[name] == 1:
                rows[name] = record
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
    except (sqlite3.Error, OSError) as exc:
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


class RouteTargetExhausted(Exception):
    """One route target ended with a safe pre-commit rejection.

    Raised only before any downstream byte was prepared, so the request may
    still be replayed against the next route target (design doc section 4).
    """

    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"route target exhausted after upstream {status}")
        self.status = status
        self.retry_after = retry_after


def route_group_name(model_in: str, cfg: dict) -> str | None:
    """Return the route group named by a ``route:<name>`` model selector."""
    model = (model_in or "").strip()
    if model.startswith("anthropic/"):
        model = model[len("anthropic/") :]
    if not model.startswith(ROUTE_GROUP_PREFIX):
        return None
    if not cfg.get("routes"):
        # Without route groups the prefix carries no routing meaning; leave
        # the model name to the legacy routing path, which may still forward
        # it unchanged (e.g. route_unknown_to_default channels).
        return None
    name = model[len(ROUTE_GROUP_PREFIX) :].strip().lower()
    known = sorted(cfg.get("routes", {}))
    if not name or name not in cfg.get("routes", {}):
        raise RouteError(400, f"unknown route '{name}'; known: {known}")
    return name


def anthropic_error(
    status: int, message: str, etype: str = "invalid_request_error"
) -> web.Response:
    return web.json_response(
        {"type": "error", "error": {"type": etype, "message": message}},
        status=status,
    )


def protocol_request_error(exc: ProtocolRequestError) -> web.Response:
    location = f" at {exc.path}" if exc.path else ""
    response = anthropic_error(
        exc.http_status,
        f"{exc.code}{location}: {exc}",
        "invalid_request_error",
    )
    response.headers["x-hub-protocol-code"] = exc.code
    return response


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
                400, f"unknown channel alias '{alias}'; known: {sorted(cfg['channels'])}"
            )
        if not upstream_model:
            raise RouteError(400, f"empty model after channel alias '{alias}'")
        return alias, upstream_model

    channels = cfg["channels"]
    # Hub v2 owns the four native Claude model slots.  Resolve those mappings
    # before looking for a coincidentally named bare upstream model so callers
    # such as Workflow ``agent(..., {model: "haiku"})`` get the exact same
    # route as Claude Code's /model picker and launcher settings.
    model_slots = cfg.get("model_slots")
    slot = model.casefold()
    if slot in HUB_SLOT_ORDER and isinstance(model_slots, dict):
        selector = model_slots.get(slot)
        if isinstance(selector, str):
            slot_alias, separator, slot_model = selector.partition(",")
            slot_alias, slot_model = slot_alias.strip().lower(), slot_model.strip()
            if separator and slot_alias in channels and slot_model:
                return slot_alias, slot_model

    aliases = [
        alias
        for alias, channel in channels.items()
        if model in channel.get("models", [])
    ]
    if len(aliases) == 1:
        return aliases[0], model
    if len(aliases) > 1:
        raise RouteError(
            400,
            f"ambiguous model '{model}'; use channel,model",
        )

    # Treat an undeclared official-style Claude model ID as a request for the
    # matching claude1 slot.  This keeps generated Workflow code portable while
    # the Hub remains authoritative about the actual upstream model.
    official_slot = re.fullmatch(
        r"claude-(fable|opus|sonnet|haiku)(?:-.+)?",
        model,
        re.IGNORECASE,
    )
    if official_slot and isinstance(model_slots, dict):
        selector = model_slots.get(official_slot.group(1).casefold())
        if isinstance(selector, str):
            slot_alias, _, slot_model = selector.partition(",")
            return slot_alias, slot_model

    alias = cfg["default_channel"]
    channel = channels.get(alias)
    if not channel:
        raise RouteError(500, f"default_channel '{alias}' not in channels config")
    if providers is None:
        providers = get_providers()
    provider = _match_channel_provider(channel, providers)
    model_lower = model.lower()
    if provider:
        for tier in HUB_SLOT_ORDER:
            mapped = provider["model_map"].get(tier)
            if model_lower == tier and mapped:
                return alias, mapped
    if channel.get("route_unknown_to_default"):
        # Explicit opt-in for isolated single-channel protocol bridges:
        # unlisted models are forwarded to the default channel unchanged.
        return alias, model
    available_slots = (
        ", ".join(slot for slot in HUB_SLOT_ORDER if slot in model_slots)
        if isinstance(model_slots, dict)
        else "fable, opus, sonnet, haiku"
    )
    raise RouteError(
        400,
        f"unknown model '{model}'; use channel,model or an available model slot: "
        f"{available_slots}",
    )


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    hostname = _canonical_hostname(hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_private_or_special_host(hostname: str | None) -> bool:
    """Return whether an IP literal or localhost targets local address space."""
    if not hostname:
        return False
    hostname = _canonical_hostname(hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        # ``is_global`` excludes RFC1918, link-local metadata addresses,
        # loopback, multicast and other non-public address space.
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        # libc accepts historical numeric IPv4 spellings that ipaddress
        # intentionally rejects (127.1, a single 32-bit integer, hexadecimal,
        # octal components). Treat numeric-looking failures as unsafe rather
        # than letting getaddrinfo reinterpret them as a private destination.
        component = r"(?:0x[0-9a-f]+|[0-9]+)"
        return re.fullmatch(rf"{component}(?:\.{component})*", hostname) is not None


def _canonical_hostname(hostname: str) -> str:
    """Apply the IDNA mapping used by network resolvers before policy checks."""
    try:
        canonical = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid IDNA hostname") from exc
    if not canonical or len(canonical) > 253:
        raise ValueError("invalid hostname")
    return canonical


def validate_upstream_url(base_url: str, alias: str, allow_insecure_http: bool) -> None:
    """Reject unsafe remote upstream URLs before any network request is made."""
    try:
        parsed = urlparse(base_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise RouteError(502, f"channel '{alias}' has an invalid upstream URL") from exc
    if (
        not parsed.hostname
        or parsed_port is not None
        and not 1 <= parsed_port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RouteError(502, f"channel '{alias}' has an invalid upstream URL")
    try:
        canonical_hostname = _canonical_hostname(parsed.hostname)
    except ValueError as exc:
        raise RouteError(
            502, f"channel '{alias}' has an invalid upstream URL"
        ) from exc
    if parsed.scheme == "https":
        if _is_private_or_special_host(canonical_hostname):
            raise RouteError(
                502,
                f"channel '{alias}' HTTPS upstream must not target a private address",
            )
        return
    if parsed.scheme == "http" and (
        _is_loopback(canonical_hostname) or allow_insecure_http
    ):
        return
    if parsed.scheme == "http":
        raise RouteError(
            502,
            f"channel '{alias}' uses remote HTTP; set allow_insecure_http=true "
            "for this channel only if it is intentional",
        )
    raise RouteError(502, f"channel '{alias}' upstream must use http or https")


def _match_channel_provider(channel: dict, providers: dict) -> dict | None:
    """Resolve a channel without mutating provider or gateway configuration.

    Current channels use a CC Switch provider selector. Legacy local-gateway
    channels used ``base_url`` instead; for those, match exactly one existing
    CC Switch provider by normalized URL and continue sourcing its credential
    from the read-only database.
    """
    selector = channel.get("provider")
    if isinstance(selector, str) and selector:
        return providers.get(selector)
    base_url = _normalize_base_url(channel.get("provider_base_url"))
    if not base_url:
        return None
    matches: list[dict] = []
    seen: set[int] = set()
    for candidate in providers.values():
        marker = id(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        if _normalize_base_url(candidate.get("base_url")) == base_url:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def resolve_provider(
    alias: str,
    cfg: dict,
    providers: dict | None = None,
) -> dict:
    channel = cfg["channels"].get(alias)
    if not channel:
        raise RouteError(400, f"unknown channel alias '{alias}'")
    if providers is None:
        providers = get_providers()
    provider = _match_channel_provider(channel, providers)
    if not provider:
        selector = channel.get("provider") or "base_url"
        raise RouteError(
            502,
            f"channel '{alias}' provider selector '{selector}' was not found "
            f"or was ambiguous in the CC Switch database "
            f"(check {config_path().name})",
        )
    if not provider.get("token"):
        raise RouteError(
            502,
            f"channel '{alias}' provider has no Anthropic credential",
        )
    if provider.get("transport_error"):
        raise RouteError(
            502,
            f"channel '{alias}' provider transport is invalid",
        )
    validate_upstream_url(
        provider["base_url"],
        alias,
        channel.get("allow_insecure_http", False),
    )
    resolved = dict(provider)
    if channel.get("api_format"):
        resolved["api_format"] = channel["api_format"]
    return resolved


class _AccountAttempt:
    __slots__ = ("provider", "lease")

    def __init__(self, provider: dict, lease: AccountLease) -> None:
        self.provider = provider
        self.lease = lease


class _AccountCandidateDirectory(Mapping):
    """Resolve and fingerprint only pool members requested by the scheduler."""

    def __init__(self, primary: dict, providers: dict) -> None:
        self.primary = primary
        self.primary_selector = str(primary.get("selector") or "")
        self.providers = providers
        self._cache: dict[str, AccountCandidate] = {}

    def record(self, selector: str) -> dict | None:
        if selector == self.primary_selector:
            return self.primary
        record = self.providers.get(selector)
        return record if isinstance(record, dict) else None

    def __getitem__(self, selector: str) -> AccountCandidate:
        cached = self._cache.get(selector)
        if cached is not None:
            return cached
        record = self.record(selector)
        if record is None:
            raise KeyError(selector)
        candidate = AccountCandidate(
            credential_fingerprint(str(record.get("token") or "")),
            endpoint=str(record.get("base_url") or ""),
            credential_type=str(record.get("credential_type") or ""),
        )
        self._cache[selector] = candidate
        return candidate

    def __iter__(self):
        seen: set[str] = set()
        if self.primary_selector:
            seen.add(self.primary_selector)
            yield self.primary_selector
        for record in self.providers.values():
            if not isinstance(record, dict):
                continue
            selector = record.get("selector")
            if isinstance(selector, str) and selector not in seen:
                seen.add(selector)
                yield selector

    def __len__(self) -> int:
        return sum(1 for _selector in self)


class _RequestAccountPool:
    """Bind one provider snapshot to the shared non-secret account scheduler."""

    def __init__(self, primary: dict, providers: dict) -> None:
        self.primary = dict(primary)
        self.primary_selector = str(primary.get("selector") or "")
        self.scheduler = AccountPool(
            account_pool_config_path(),
            account_pool_state_path(),
        )
        self.directory = _AccountCandidateDirectory(self.primary, providers)

    def acquire(self, *, exclude: set[str] | None = None) -> _AccountAttempt:
        if not self.primary_selector:
            token = str(self.primary.get("token") or "")
            if not token:
                raise PoolConfigError("provider has no credential")
            return _AccountAttempt(
                provider=dict(self.primary),
                lease=AccountLease(
                    "id:legacy",
                    "id:legacy",
                    credential_fingerprint(token),
                ),
            )
        lease = self.scheduler.acquire(
            self.primary_selector,
            self.directory,
            exclude=exclude or (),
        )
        account = self.directory.record(lease.member)
        if account is None or not account.get("token"):
            raise PoolConfigError("selected account has no credential")
        provider = dict(self.primary)
        provider["token"] = account["token"]
        provider["account"] = lease.member
        return _AccountAttempt(provider=provider, lease=lease)

    def report(
        self,
        attempt: _AccountAttempt,
        status: int,
        retry_after: str | None,
    ) -> None:
        self.scheduler.report(attempt.lease, status, retry_after)


def _account_pool_error(exc: AccountPoolError) -> web.Response:
    if isinstance(exc, PoolExhausted):
        if exc.reason != "cooldown" or exc.retry_after is None:
            if exc.reason == "auth_disabled":
                message = (
                    "hub: all account credentials for this provider are disabled; "
                    "update the credentials or reset the account pool"
                )
            elif exc.reason == "config_disabled":
                message = "hub: all accounts for this provider are disabled"
            else:
                message = "hub: no provider account is currently available"
            return anthropic_error(503, message, "api_error")
        response = anthropic_error(
            429,
            "hub: all accounts for this provider are temporarily unavailable",
            "rate_limit_error",
        )
        if exc.retry_after is not None:
            response.headers["retry-after"] = str(exc.retry_after)
        return response
    if isinstance(exc, PoolConfigError):
        return anthropic_error(
            502,
            "hub: provider account pool configuration is invalid",
            "api_error",
        )
    return anthropic_error(
        503,
        "hub: provider account pool state is unavailable",
        "api_error",
    )


# ---------------------------------------------------------------- forwarding


def _upstream_url(provider: dict, path: str) -> str:
    """Resolve a provider base URL against one protocol endpoint path."""
    if provider.get("is_full_url"):
        return provider["base_url"]
    return provider["base_url"] + path


def _upstream_session(request: web.Request):
    session = request.app.get(UPSTREAM_SESSION_KEY)
    if session is None:
        session = request.app.get("session")
    if session is None:
        raise ConfigError("upstream client session is unavailable")
    return session


def _full_endpoint_matches_format(provider: dict, api_format: str) -> bool:
    """Check recognized standard endpoint suffixes without rejecting custom paths."""
    if not provider.get("is_full_url"):
        return True
    path = urlparse(provider["base_url"]).path.rstrip("/")
    formats = {
        "/v1/messages": "anthropic",
        "/v1/chat/completions": "openai_chat",
        "/v1/responses": "openai_responses",
    }
    expected = next(
        (value for suffix, value in formats.items() if path.endswith(suffix)),
        None,
    )
    return expected is None or expected == api_format


def check_local_auth(request: web.Request, cfg: dict) -> bool:
    token = cfg["local_token"]
    authorization = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")
    return (
        hmac.compare_digest(authorization, f"Bearer {token}")
        or hmac.compare_digest(authorization, token)
        or hmac.compare_digest(api_key, token)
    )


def channel_proxy(alias: str, cfg: dict, provider: dict | None = None) -> str | None:
    """A channel proxy overrides provider and optional global proxies."""
    return (
        cfg["channels"].get(alias, {}).get("proxy")
        or (provider or {}).get("proxy")
        or cfg.get("proxy")
        or None
    )


def channel_transport_policy(
    alias: str,
    cfg: dict,
    provider: dict | None,
    endpoint: str,
):
    """Resolve one channel's transport while preserving legacy proxy intent."""
    channel = cfg["channels"].get(alias, {})
    if channel.get("transport") is not None:
        transport = channel["transport"]
    elif channel.get("proxy"):
        transport = {"mode": "proxy", "proxies": [channel["proxy"]]}
    elif (provider or {}).get("transport") is not None:
        transport = provider["transport"]
    elif (provider or {}).get("proxy"):
        transport = {"mode": "proxy", "proxies": [provider["proxy"]]}
    elif cfg.get("proxy"):
        transport = {"mode": "proxy", "proxies": [cfg["proxy"]]}
    else:
        transport = cfg.get("transport")
    return resolve_transport_policy(endpoint, transport)


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


def upstream_headers(request: web.Request, token: str) -> CIMultiDict:
    strip = REQ_STRIP | _connection_header_tokens(request.headers)
    headers = CIMultiDict()
    for key, value in request.headers.items():
        if key.casefold() not in strip:
            headers.add(key, value)
    headers["authorization"] = f"Bearer {token}"
    headers["x-api-key"] = token
    # Ask upstreams not to compress SSE: clients may advertise br/zstd, which
    # _SSEContentDecoder cannot validate while forwarding raw bytes. identity
    # is always safe to forward to any client. A non-compliant upstream that
    # still answers gzip/deflate is handled by _SSEContentDecoder.
    headers["accept-encoding"] = "identity"
    return headers


class StreamTelemetry:
    """Collect content-free timing and byte metrics for one upstream stream."""

    __slots__ = (
        "_clock",
        "_started_at",
        "_headers_at",
        "_first_chunk_at",
        "_last_chunk_at",
        "_max_gap",
        "_chunks",
        "_upstream_bytes",
    )

    def __init__(self, *, started_at: float, clock=time.monotonic) -> None:
        self._clock = clock
        self._started_at = started_at
        self._headers_at = clock()
        self._first_chunk_at = None
        self._last_chunk_at = None
        self._max_gap = 0.0
        self._chunks = 0
        self._upstream_bytes = 0

    def observe(self, chunk: bytes) -> None:
        if not chunk:
            return
        now = self._clock()
        if self._first_chunk_at is None:
            self._first_chunk_at = now
        elif self._last_chunk_at is not None:
            self._max_gap = max(self._max_gap, now - self._last_chunk_at)
        self._last_chunk_at = now
        self._chunks += 1
        self._upstream_bytes += len(chunk)

    @staticmethod
    def _milliseconds(seconds: float) -> int:
        return int(round(seconds * 1000))

    def snapshot(self) -> dict:
        first_chunk_ms = None
        if self._first_chunk_at is not None:
            first_chunk_ms = self._milliseconds(
                self._first_chunk_at - self._started_at
            )
        return {
            "headers_ms": self._milliseconds(
                self._headers_at - self._started_at
            ),
            "first_chunk_ms": first_chunk_ms,
            "max_gap_ms": self._milliseconds(self._max_gap),
            "chunks": self._chunks,
            "upstream_bytes": self._upstream_bytes,
        }


def stream_telemetry_fields(
    telemetry: StreamTelemetry,
    *,
    terminal: str,
    error: str | None = None,
    downstream_bytes: int | None = None,
) -> str:
    metrics = telemetry.snapshot()
    first_chunk_ms = metrics["first_chunk_ms"]
    fields = [
        f"headers_ms={metrics['headers_ms']}",
        f"first_chunk_ms={first_chunk_ms if first_chunk_ms is not None else 'none'}",
        f"max_gap_ms={metrics['max_gap_ms']}",
        f"chunks={metrics['chunks']}",
        f"upstream_bytes={metrics['upstream_bytes']}",
    ]
    if downstream_bytes is not None:
        fields.append(f"downstream_bytes={downstream_bytes}")
    fields.append(f"terminal={terminal}")
    if error is not None:
        fields.append(f"error={error}")
    return " ".join(fields)


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


class _SSEUsageTracker:
    """轻量解析透传 SSE 里的 usage（message_start / message_delta），用于落盘统计。

    跨 chunk 缓冲不完整行，best-effort：解析失败静默，不影响转发正确性。"""

    def __init__(self) -> None:
        self.usage: dict = {}
        self._pending = bytearray()
        self._discarding_line = False

    def feed(self, chunk: bytes) -> None:
        start = 0
        for match in SSE_NEWLINE_RE.finditer(chunk):
            segment = chunk[start : match.start()]
            if not self._discarding_line:
                if len(self._pending) + len(segment) <= SSE_LINE_LIMIT:
                    self._pending.extend(segment)
                    self._consume_line(bytes(self._pending))
                self._pending.clear()
            self._discarding_line = False
            start = match.end()
        tail = chunk[start:]
        if self._discarding_line:
            return
        if len(self._pending) + len(tail) > SSE_LINE_LIMIT:
            self._pending.clear()
            self._discarding_line = True
            return
        self._pending.extend(tail)

    def finish(self) -> None:
        if self._pending and not self._discarding_line:
            self._consume_line(bytes(self._pending))
        self._pending.clear()
        self._discarding_line = False

    def _consume_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        self._absorb(event)

    def _absorb(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                self._merge(message["usage"])
        elif kind == "message_delta":
            if isinstance(event.get("usage"), dict):
                self._merge(event["usage"])

    def _merge(self, usage: dict) -> None:
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and value > self.usage.get(key, 0):
                self.usage[key] = value
        for detail_key in ("cache_creation", "server_tool_use"):
            detail = usage.get(detail_key)
            if not isinstance(detail, dict):
                continue
            merged = self.usage.get(detail_key)
            if not isinstance(merged, dict):
                merged = {}
            merged = dict(merged)
            for key, value in detail.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= merged.get(key, 0)
                ):
                    merged[key] = value
            if merged:
                self.usage[detail_key] = merged


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
        len(
            json.dumps(
                relevant,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        // 4,
    )


def _token_count_estimate_headers() -> dict[str, str]:
    return {
        "x-hub-estimated": "1",
        "x-hub-token-count-source": "estimate",
        "x-hub-token-count-method": "json_utf8_bytes_div_4",
        "x-hub-token-count-exact": "0",
        "x-hub-token-count-error-bound": "unbounded",
    }


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


@asynccontextmanager
async def _post_with_account_failover(
    *,
    session,
    account_pool: _RequestAccountPool,
    url: str,
    data: bytes,
    headers_for_token,
    timeout: aiohttp.ClientTimeout,
    transport_policy,
    log_context: str,
):
    """Open one upstream response, retrying only explicit pre-commit rejects."""
    attempted: set[str] = set()
    attempt = await asyncio.to_thread(account_pool.acquire)
    executor = UpstreamExecutor(log=log)
    while True:
        async with executor.open(
            session=session,
            method="POST",
            url=url,
            policy=transport_policy,
            request_kwargs={
                "data": data,
                "headers": headers_for_token(attempt.provider["token"]),
                "timeout": timeout,
                "allow_redirects": False,
            },
            retry_response=lambda response: response.status == 403,
        ) as opened:
            upstream = opened.response
            retryable = upstream.status in (401, 403, 429)
            if retryable and attempt.lease.managed:
                retry_after = upstream.headers.get("retry-after")
                await asyncio.to_thread(
                    account_pool.report,
                    attempt,
                    upstream.status,
                    retry_after,
                )
                attempted.add(attempt.lease.member)
                try:
                    replacement = await asyncio.to_thread(
                        account_pool.acquire,
                        exclude=attempted,
                    )
                except PoolExhausted:
                    # No account remains. Preserve the final upstream status,
                    # headers and body rather than manufacturing a success.
                    yield upstream, attempt
                    return
                log(
                    f"{log_context} account failover "
                    f"{attempt.lease.member} -> {replacement.lease.member} "
                    f"after upstream {upstream.status}"
                )
                attempt = replacement
                continue
            yield upstream, attempt
            return


async def _read_decoded_upstream_body(
    upstream, limit: int = MAX_UPSTREAM_BODY_BYTES
) -> bytes:
    """Decode one bounded HTTP content-coding for transformed JSON responses."""
    try:
        decoder = _SSEContentDecoder.from_headers(upstream.headers)
    except ValueError as exc:
        raise ProtocolTransformError(
            "upstream JSON uses an unsupported content encoding"
        ) from exc
    body = bytearray()
    try:
        async for chunk in upstream.content.iter_any():
            for decoded in decoder.feed(chunk):
                body.extend(decoded)
                if len(body) > limit:
                    raise ProtocolTransformError(
                        "upstream response exceeds size limit"
                    )
                await asyncio.sleep(0)
        decoder.finish()
    except zlib.error as exc:
        raise ProtocolTransformError(
            "upstream JSON has an invalid content encoding"
        ) from exc
    return bytes(body)


def _append_bounded_json_buffer(
    buffer: bytearray | None, chunk: bytes, limit: int = MAX_UPSTREAM_BODY_BYTES
) -> bytearray | None:
    """Keep at most ``limit`` bytes for optional direct-response usage parsing."""
    if buffer is None or len(buffer) + len(chunk) > limit:
        return None
    buffer.extend(chunk)
    return buffer


def _synthesize_message_stream(body: dict, usage: dict) -> bytes:
    """Render one complete Anthropic message as a one-shot SSE stream."""
    content = body.get("content")
    if not isinstance(content, list):
        raise ProtocolTransformError(
            "upstream message content must be an array",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
        )
    message = {
        "id": body.get("id"),
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": body.get("model"),
        "stop_reason": None,
        "stop_sequence": None,
    }
    if usage:
        # Only counters actually observed upstream are emitted; the caller
        # passes the receipt-derived accounting view, which has no
        # schema-complete zero placeholders to strip.
        message["usage"] = usage
    events = [
        sse_event("message_start", {"type": "message_start", "message": message})
    ]
    for index, block in enumerate(content):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ProtocolTransformError(
                "upstream message content blocks must be typed objects",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
            )
        kind = block["type"]
        if kind == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ProtocolTransformError(
                    "upstream text block text must be a string",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                )
            start_block = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": text}]
        elif kind == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise ProtocolTransformError(
                    "upstream thinking block text must be a string",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                )
            start_block = {"type": "thinking", "thinking": ""}
            deltas = [{"type": "thinking_delta", "thinking": thinking}]
            signature = block.get("signature")
            if signature is not None:
                if not isinstance(signature, str):
                    raise ProtocolTransformError(
                        "upstream thinking block signature must be a string",
                        code="HUB_UPSTREAM_RESPONSE_INVALID",
                    )
                deltas.append(
                    {"type": "signature_delta", "signature": signature}
                )
        elif kind == "tool_use":
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                raise ProtocolTransformError(
                    "upstream tool_use block input must be an object",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                )
            try:
                partial_json = json.dumps(
                    tool_input,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise ProtocolTransformError(
                    "upstream tool_use block input is not JSON serializable",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                ) from exc
            start_block = {
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": {},
            }
            deltas = [
                {"type": "input_json_delta", "partial_json": partial_json}
            ]
        else:
            # Blocks without a delta encoding (redacted_thinking, future
            # server-side blocks) are delivered whole so nothing is dropped.
            start_block = dict(block)
            deltas = []
        events.append(
            sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": start_block,
                },
            )
        )
        for delta in deltas:
            events.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": delta,
                    },
                )
            )
        events.append(
            sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        )
    message_delta = {
        "type": "message_delta",
        "delta": {
            "stop_reason": body.get("stop_reason"),
            "stop_sequence": body.get("stop_sequence"),
        },
    }
    trailing_usage = {
        key: usage[key]
        for key in ("output_tokens", "cache_creation", "server_tool_use")
        if key in usage
    }
    if trailing_usage:
        message_delta["usage"] = trailing_usage
    events.append(sse_event("message_delta", message_delta))
    events.append(sse_event("message_stop", {"type": "message_stop"}))
    return b"".join(events)


async def _handle_transformed_messages(
    request: web.Request,
    *,
    cfg: dict,
    provider: dict,
    payload: dict,
    alias: str,
    model_in: str,
    model_out: str,
    started: float,
    account_pool: _RequestAccountPool | None = None,
    route_failover: bool = False,
    route_name: str | None = None,
) -> web.StreamResponse:
    if account_pool is None:
        account_pool = _RequestAccountPool(provider, {})
    route_headers = {"x-hub-route": route_name} if route_name else {}
    api_format = provider["api_format"]
    try:
        prepared_request = prepare_request(
            payload,
            api_format,
            provider_type=provider.get("provider_type"),
        )
    except ProtocolRequestError as exc:
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"REQUEST REJECT {exc.code} {exc.path or '$'}"
        )
        return protocol_request_error(exc)
    endpoint = prepared_request.endpoint
    upstream_payload = prepared_request.payload
    request_warning_codes = prepared_request.plan.warning_codes
    if request_warning_codes:
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"protocol warnings {','.join(request_warning_codes)}"
        )
    data = json.dumps(
        upstream_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    streaming = upstream_payload.get("stream") is True
    url = _upstream_url(provider, endpoint)
    session = _upstream_session(request)
    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)

    try:
        async with _post_with_account_failover(
            session=session,
            account_pool=account_pool,
            url=url,
            data=data,
            headers_for_token=lambda token: _transformed_headers(token, streaming),
            timeout=timeout,
            transport_policy=channel_transport_policy(alias, cfg, provider, url),
            log_context=f"{request.path} '{model_in}' -> {alias}/{model_out}",
        ) as (upstream, account_attempt):
            if route_failover and upstream.status in ROUTE_FAILOVER_STATUSES:
                # Every account and transport of this target rejected the
                # request before any content was generated; the body is still
                # safely replayable against the next route target.
                raise RouteTargetExhausted(
                    upstream.status,
                    upstream.headers.get("retry-after"),
                )
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
                raw = await _read_decoded_upstream_body(upstream)
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = raw.decode("utf-8", "replace")
                if upstream.status >= 400:
                    body = transform_error(decoded, upstream.status)
                    log(
                        f"{request.path} '{model_in}' -> {alias}/{model_out} "
                        f"{api_format} upstream {upstream.status}"
                    )
                    error_headers = {
                        "x-hub-channel": alias,
                        "x-hub-model": model_out,
                        "x-hub-upstream-format": api_format,
                        "x-hub-account": account_attempt.lease.member,
                        **route_headers,
                    }
                    if request_warning_codes:
                        error_headers["x-hub-protocol-warnings"] = ",".join(
                            request_warning_codes
                        )
                    retry_after = upstream.headers.get("retry-after")
                    if upstream.status == 429 and retry_after:
                        error_headers["retry-after"] = retry_after
                    return web.json_response(
                        body,
                        status=upstream.status,
                        headers=error_headers,
                    )
                if not isinstance(decoded, dict):
                    raise ProtocolTransformError(
                        "upstream returned a non-object JSON response"
                    )
                prepared_response = prepare_response(decoded, api_format)
                body = prepared_response.payload
                warning_codes = tuple(
                    dict.fromkeys(
                        (*request_warning_codes, *prepared_response.plan.warning_codes)
                    )
                )
                # The ledger reads the same receipt the downstream payload
                # was rendered from, so schema-complete zero placeholders
                # never reach accounting.
                usage_view = prepared_response.usage_for_accounting()
                record_usage(
                    alias,
                    model_out,
                    api_format,
                    usage_view,
                    instance_id=cfg.get("instance_id"),
                    account_id=account_attempt.lease.member,
                    source=("upstream" if usage_view else "unavailable"),
                )
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"{api_format} {upstream.status} json "
                    f"{time.monotonic() - started:.1f}s {len(raw)}B"
                )
                response_headers = {
                    "x-hub-channel": alias,
                    "x-hub-model": model_out,
                    "x-hub-upstream-format": api_format,
                    "x-hub-account": account_attempt.lease.member,
                    **route_headers,
                }
                if warning_codes:
                    response_headers["x-hub-protocol-warnings"] = ",".join(
                        warning_codes
                    )
                if streaming:
                    # The client asked for a stream but the upstream answered
                    # with a complete JSON message; synthesize the one-shot
                    # Anthropic SSE sequence Claude Code expects.
                    response_headers["cache-control"] = "no-cache"
                    return web.Response(
                        body=_synthesize_message_stream(body, usage_view),
                        status=upstream.status,
                        content_type="text/event-stream",
                        headers=response_headers,
                    )
                return web.json_response(
                    body,
                    status=upstream.status,
                    headers=response_headers,
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
                    "x-hub-account": account_attempt.lease.member,
                    **route_headers,
                },
            )
            if request_warning_codes:
                response.headers["x-hub-protocol-warnings"] = ",".join(
                    request_warning_codes
                )
            await response.prepare(request)
            byte_count = 0
            stream_telemetry = StreamTelemetry(started_at=started)
            try:
                async for chunk in upstream.content.iter_any():
                    stream_telemetry.observe(chunk)
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
                runtime_warning_codes = tuple(
                    getattr(bridge, "warning_codes", ())
                )
                if runtime_warning_codes:
                    log(
                        f"{request.path} '{model_in}' -> {alias}/{model_out} "
                        f"protocol warnings {','.join(runtime_warning_codes)}"
                    )
                await response.write_eof()
                # Same receipt the downstream stream was built from, so the
                # ledger cannot disagree with what the client was told.
                stream_usage = bridge.usage_for_accounting()
                record_usage(
                    alias,
                    model_out,
                    api_format,
                    stream_usage,
                    instance_id=cfg.get("instance_id"),
                    account_id=account_attempt.lease.member,
                    source=("upstream" if stream_usage else "unavailable"),
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                UnicodeDecodeError,
                ProtocolTransformError,
                TypeError,
                ValueError,
                zlib.error,
            ) as exc:
                protocol_code = (
                    f" {exc.code} {exc.path or '$'}"
                    if isinstance(exc, ProtocolTransformError)
                    else ""
                )
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"{api_format} stream failed after {byte_count}B: "
                    f"{type(exc).__name__}{protocol_code} "
                    + stream_telemetry_fields(
                        stream_telemetry,
                        terminal="error",
                        error=type(exc).__name__,
                        downstream_bytes=byte_count,
                    )
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "translated upstream stream ended after response started"
                ) from exc
            log(
                f"{request.path} '{model_in}' -> {alias}/{model_out} "
                f"{api_format} 200 stream {time.monotonic() - started:.1f}s "
                f"{byte_count}B "
                + stream_telemetry_fields(
                    stream_telemetry,
                    terminal="complete",
                    downstream_bytes=byte_count,
                )
            )
            return response
    except UpstreamStreamAborted:
        raise
    except AccountPoolError as exc:
        if route_failover and isinstance(exc, PoolExhausted):
            # The pool denied a credential before anything was sent upstream,
            # so replaying the body against the next route target is safe.
            # Local mapping beyond design doc section 4: a pool-wide cooldown
            # surfaces as 429 with the remaining cooldown as retry-after,
            # while disabled pools surface as 503 without one.
            raise RouteTargetExhausted(
                429 if exc.reason == "cooldown" else 503,
                str(exc.retry_after) if exc.retry_after is not None else None,
            ) from exc
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"ACCOUNT POOL FAIL: {type(exc).__name__}: {exc}"
        )
        return _account_pool_error(exc)
    except ProtocolTransformError as exc:
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"{api_format} TRANSFORM FAIL {exc.code} {exc.path or '$'}"
        )
        return anthropic_error(
            502,
            f"hub: channel '{alias}' returned an incompatible {api_format} response",
            "api_error",
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"{api_format} CONNECT FAIL: {type(exc).__name__}"
        )
        return anthropic_error(
            502,
            f"hub: cannot reach channel '{alias}'",
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
        RecursionError,
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
        route_name = route_group_name(model_in, cfg)
        if route_name is None:
            targets = [route(model_in, cfg, providers)]
        else:
            targets = [
                (target["channel"], target["model"])
                for target in cfg["routes"][route_name]["targets"]
            ]
        last_exhausted: RouteTargetExhausted | None = None
        for index, (alias, model_out) in enumerate(targets):
            try:
                return await _forward_to_channel(
                    request,
                    cfg=cfg,
                    providers=providers,
                    alias=alias,
                    model_in=model_in,
                    model_out=model_out,
                    payload=copy.deepcopy(payload),
                    started=started,
                    is_count=is_count,
                    route_failover=route_name is not None,
                    route_name=route_name,
                )
            except RouteTargetExhausted as exc:
                last_exhausted = exc
                remaining = len(targets) - index - 1
                log(
                    f"{request.path} '{model_in}' route '{route_name}' target "
                    f"{alias}/{model_out} exhausted after upstream {exc.status}"
                    + ("; trying next target" if remaining else "; no targets remain")
                )
        # Every route target ended in a safe pre-commit rejection; surface
        # the last one instead of manufacturing a success. Last error wins:
        # an earlier target's Retry-After is intentionally dropped when a
        # later target rejects differently (design doc section 4).
        if last_exhausted is None:
            raise RouteError(500, f"route '{route_name}' produced no outcome")
        status = last_exhausted.status
        error_type = {
            401: "authentication_error",
            403: "permission_error",
            429: "rate_limit_error",
        }.get(status, "api_error")
        response = anthropic_error(
            status,
            f"hub: route '{route_name}' exhausted all {len(targets)} targets",
            error_type,
        )
        if route_name is not None:
            response.headers["x-hub-route"] = route_name
        if last_exhausted.retry_after:
            response.headers["retry-after"] = last_exhausted.retry_after
        return response
    except RouteError as exc:
        log(f"{request.path} '{model_in}' -> ROUTE ERROR {exc.status}: {exc.message}")
        return anthropic_error(
            exc.status,
            exc.message,
            "api_error" if exc.status >= 500 else "invalid_request_error",
        )


async def _forward_to_channel(
    request: web.Request,
    *,
    cfg: dict,
    providers: dict,
    alias: str,
    model_in: str,
    model_out: str,
    payload: dict,
    started: float,
    is_count: bool,
    route_failover: bool = False,
    route_name: str | None = None,
) -> web.StreamResponse:
    """Forward one validated request to a single channel target.

    With ``route_failover`` armed, safe pre-commit rejections raise
    ``RouteTargetExhausted`` instead of returning an error response, so the
    route dispatcher can replay the still-buffered body against the next
    target. Once any downstream byte is prepared the outcome is final.
    """
    provider = resolve_provider(alias, cfg, providers)
    account_pool = _RequestAccountPool(provider, providers)
    route_headers = {"x-hub-route": route_name} if route_name else {}

    payload["model"] = model_out
    api_format = provider.get("api_format", "anthropic")
    if provider.get("is_full_url") and request.query_string:
        return anthropic_error(
            400,
            "full-url channels do not accept a request query string",
        )
    protocol_warning_codes: tuple[str, ...] = ()
    if api_format == "anthropic":
        try:
            prepared = prepare_request(payload, "anthropic")
        except ProtocolRequestError as exc:
            log(
                f"{request.path} '{model_in}' -> {alias}/{model_out} "
                f"REQUEST REJECT {exc.code} {exc.path or '$'}"
            )
            return protocol_request_error(exc)
        payload = prepared.payload
        protocol_warning_codes = prepared.plan.warning_codes
        if protocol_warning_codes:
            log(
                f"{request.path} '{model_in}' -> {alias}/{model_out} "
                f"protocol warnings {','.join(protocol_warning_codes)}"
            )
    if is_count and (
        api_format != "anthropic" or provider.get("is_full_url")
    ):
        if api_format != "anthropic":
            # Estimation shares the messages-path inbound validation so a
            # malformed payload is rejected instead of estimated.
            try:
                prepare_request(
                    payload,
                    api_format,
                    provider_type=provider.get("provider_type"),
                )
            except ProtocolRequestError as exc:
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"REQUEST REJECT {exc.code} {exc.path or '$'}"
                )
                return protocol_request_error(exc)
        estimate = _estimated_input_tokens(payload)
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"{api_format} locally estimated {estimate} tokens"
        )
        return web.json_response(
            {"input_tokens": estimate},
            headers={
                "x-hub-channel": alias,
                "x-hub-model": model_out,
                **route_headers,
                **_token_count_estimate_headers(),
            },
        )
    if api_format != "anthropic":
        return await _handle_transformed_messages(
            request,
            cfg=cfg,
            provider=provider,
            account_pool=account_pool,
            payload=payload,
            alias=alias,
            model_in=model_in,
            model_out=model_out,
            started=started,
            route_failover=route_failover,
            route_name=route_name,
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
    url = _upstream_url(provider, path_and_query)
    session = _upstream_session(request)
    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)

    def headers_for_token(token: str) -> CIMultiDict:
        headers = upstream_headers(request, token)
        ensure_1m_beta(headers, model_out)
        return headers

    try:
        async with _post_with_account_failover(
            session=session,
            account_pool=account_pool,
            url=url,
            data=data,
            headers_for_token=headers_for_token,
            timeout=timeout,
            transport_policy=channel_transport_policy(alias, cfg, provider, url),
            log_context=f"{request.path} '{model_in}' -> {alias}/{model_out}",
        ) as (upstream, account_attempt):
            if route_failover and upstream.status in ROUTE_FAILOVER_STATUSES:
                # Every account and transport of this target rejected the
                # request before any content was generated; the buffered body
                # is still safely replayable against the next route target.
                raise RouteTargetExhausted(
                    upstream.status,
                    upstream.headers.get("retry-after"),
                )
            if is_count and upstream.status in (404, 405, 501):
                estimate = _estimated_input_tokens(payload)
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"upstream {upstream.status}, estimated {estimate} tokens"
                )
                return web.json_response(
                    {"input_tokens": estimate},
                    headers={
                        "x-hub-channel": alias,
                        "x-hub-model": model_out,
                        **route_headers,
                        **_token_count_estimate_headers(),
                    },
                )

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
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    "upstream returned ambiguous representation headers"
                )
                return anthropic_error(
                    502,
                    f"hub: channel '{alias}' returned invalid response headers",
                    "api_error",
                )
            content_type = content_types[0] if content_types else ""
            streamed = (
                upstream.status == 200
                and content_type.split(";", 1)[0].strip().casefold()
                == "text/event-stream"
            )
            try:
                sse_decoder = (
                    _SSEContentDecoder.from_headers(upstream.headers)
                    if streamed
                    else None
                )
            except ValueError:
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    "upstream SSE used an unsupported content encoding"
                )
                return anthropic_error(
                    502,
                    f"hub: channel '{alias}' returned an unsupported SSE encoding",
                    "api_error",
                )

            response = web.StreamResponse(status=upstream.status)
            response_strip = RESP_STRIP | response_connection_tokens
            for key, value in upstream.headers.items():
                if key.casefold() not in response_strip:
                    response.headers.add(key, value)
            response.headers["x-hub-channel"] = alias
            response.headers["x-hub-model"] = model_out
            response.headers["x-hub-account"] = account_attempt.lease.member
            if route_name is not None:
                response.headers["x-hub-route"] = route_name
            if is_count and upstream.status == 200:
                response.headers["x-hub-token-count-source"] = "upstream"
                response.headers[
                    "x-hub-token-count-method"
                ] = "anthropic_count_tokens"
                response.headers["x-hub-token-count-exact"] = "1"
            if protocol_warning_codes:
                response.headers["x-hub-protocol-warnings"] = ",".join(
                    protocol_warning_codes
                )
            await response.prepare(request)

            byte_count = 0
            stream_telemetry = StreamTelemetry(started_at=started)
            sse_tracker = _SSETerminalTracker() if streamed else None
            usage_tracker = _SSEUsageTracker() if streamed else None
            json_buf = bytearray() if not streamed and upstream.status == 200 else None
            try:
                async for chunk in upstream.content.iter_any():
                    stream_telemetry.observe(chunk)
                    if sse_tracker is not None:
                        for decoded in sse_decoder.feed(chunk):
                            sse_tracker.feed(decoded)
                            usage_tracker.feed(decoded)
                            if sse_tracker.protocol_error:
                                raise ProtocolTransformError(
                                    "native Anthropic SSE violated terminal or UTF-8 ordering",
                                    code="HUB_SSE_ORDER_VIOLATION",
                                )
                            # Keep one highly compressible response from
                            # monopolizing the local gateway event loop.
                            await asyncio.sleep(0)
                    json_buf = _append_bounded_json_buffer(json_buf, chunk)
                    await response.write(chunk)
                    byte_count += len(chunk)
                if sse_tracker is not None:
                    sse_decoder.finish()
                    sse_tracker.finish()
                    usage_tracker.finish()
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                zlib.error,
                ProtocolTransformError,
            ) as exc:
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"upstream broke or was invalid after {byte_count}B: "
                    f"{type(exc).__name__} "
                    + stream_telemetry_fields(
                        stream_telemetry,
                        terminal="error",
                        error=type(exc).__name__,
                        downstream_bytes=byte_count,
                    )
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "upstream stream ended after downstream response started"
                ) from exc

            if sse_tracker is not None and not sse_tracker.complete:
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"upstream SSE ended without a valid terminal event after "
                    f"{byte_count}B "
                    + stream_telemetry_fields(
                        stream_telemetry,
                        terminal="missing",
                        downstream_bytes=byte_count,
                    )
                )
                transport = request.transport
                if transport is not None:
                    transport.abort()
                raise UpstreamStreamAborted(
                    "upstream SSE ended without a valid message_stop or error"
                )

            await response.write_eof()
            if usage_tracker is not None:
                native_stream_usage = usage_tracker.usage
                record_usage(
                    alias,
                    model_out,
                    "anthropic",
                    native_stream_usage,
                    instance_id=cfg.get("instance_id"),
                    account_id=account_attempt.lease.member,
                    source=(
                        "upstream"
                        if native_stream_usage
                        else "unavailable"
                    ),
                )
            elif not streamed and upstream.status == 200:
                native_usage = (
                    _usage_from_json_bytes(json_buf, upstream.headers)
                    if json_buf is not None
                    else None
                )
                record_usage(
                    alias,
                    model_out,
                    "anthropic",
                    native_usage,
                    instance_id=cfg.get("instance_id"),
                    account_id=account_attempt.lease.member,
                    # An empty usage object carries no observed counter, so
                    # it is not upstream evidence — same rule as the other
                    # three accounting exits.
                    source=(
                        "upstream"
                        if native_usage
                        else "unavailable"
                    ),
                )
            log(
                f"{request.path} '{model_in}' -> {alias}/{model_out} "
                f"{upstream.status} {'stream' if streamed else 'json'} "
                f"{time.monotonic() - started:.1f}s {byte_count}B"
                + (
                    " "
                    + stream_telemetry_fields(
                        stream_telemetry,
                        terminal="complete",
                        downstream_bytes=byte_count,
                    )
                    if streamed
                    else ""
                )
            )
            return response
    except AccountPoolError as exc:
        if route_failover and isinstance(exc, PoolExhausted):
            # The pool denied a credential before anything was sent upstream,
            # so replaying the body against the next route target is safe.
            # Local mapping beyond design doc section 4: a pool-wide cooldown
            # surfaces as 429 with the remaining cooldown as retry-after,
            # while disabled pools surface as 503 without one.
            raise RouteTargetExhausted(
                429 if exc.reason == "cooldown" else 503,
                str(exc.retry_after) if exc.retry_after is not None else None,
            ) from exc
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"ACCOUNT POOL FAIL: {type(exc).__name__}: {exc}"
        )
        return _account_pool_error(exc)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log(
            f"{request.path} '{model_in}' -> {alias}/{model_out} "
            f"CONNECT FAIL: {type(exc).__name__}"
        )
        return anthropic_error(
            502,
            f"hub: cannot reach channel '{alias}'",
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


async def handle_readyz(request: web.Request) -> web.Response:
    """Return a challenge response proving possession of the local token."""
    challenge = request.headers.get("x-claude-hub-challenge", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", challenge):
        return anthropic_error(
            400, "invalid readiness challenge", "invalid_request_error"
        )
    cfg = get_config()
    instance_id = cfg.get("instance_id")
    if instance_id is None:
        proof_message = (
            f"claude-hub-ready:v1:{cfg['port']}:{challenge}".encode("ascii")
        )
    else:
        proof_message = (
            f"claude-hub-ready:v2:{instance_id}:{cfg['port']}:{challenge}".encode(
                "ascii"
            )
        )
    proof = hmac.digest(
        cfg["local_token"].encode("utf-8"), proof_message, "sha256"
    ).hex()
    payload = {**HEALTH_PAYLOAD, "proof": proof}
    if instance_id is not None:
        payload["identity_protocol"] = 2
    return web.json_response(payload)


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
    except web.HTTPRequestEntityTooLarge:
        return anthropic_error(
            413,
            "request body exceeds claude-hub's 64 MiB limit",
            "invalid_request_error",
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


def _upstream_ssl_context() -> ssl.SSLContext:
    """Use the platform trust plus certifi's Mozilla CA bundle.

    Python.org macOS builds can have no populated default OpenSSL CA file;
    adding certifi keeps public HTTPS providers verifiable without disabling TLS.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def upstream_socket_factory(address_info: tuple) -> socket.socket:
    """Create an upstream TCP socket with kernel keepalive enabled."""
    family, socket_type, protocol, _canonical_name, _address = address_info
    sock = socket.socket(family, socket_type, protocol)

    def set_option(level: int, option: int, value: int) -> None:
        try:
            sock.setsockopt(level, option, value)
        except OSError:
            # Keepalive tuning varies across kernels and container runtimes.
            # A missing option must not make the gateway unable to connect.
            pass

    set_option(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle_option = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None
    )
    if idle_option is not None:
        set_option(
            socket.IPPROTO_TCP,
            idle_option,
            UPSTREAM_KEEPALIVE_IDLE_SECONDS,
        )
    if hasattr(socket, "TCP_KEEPINTVL"):
        set_option(
            socket.IPPROTO_TCP,
            socket.TCP_KEEPINTVL,
            UPSTREAM_KEEPALIVE_INTERVAL_SECONDS,
        )
    if hasattr(socket, "TCP_KEEPCNT"):
        set_option(
            socket.IPPROTO_TCP,
            socket.TCP_KEEPCNT,
            UPSTREAM_KEEPALIVE_PROBES,
        )
    return sock


def _upstream_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        ssl=_upstream_ssl_context(),
        socket_factory=upstream_socket_factory,
    )


async def _client_session_context(app: web.Application):
    session = aiohttp.ClientSession(
        auto_decompress=False,
        skip_auto_headers={"Accept-Encoding"},
        connector=_upstream_connector(),
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
    app.router.add_get("/readyz", handle_readyz)
    app.router.add_route("*", "/{tail:.*}", handle_fallback)
    return app


def _inherited_loopback_listener(cfg: dict) -> socket.socket | None:
    raw_fd = os.environ.get(ENV_LISTEN_FD)
    if raw_fd is None:
        return None
    try:
        listener = socket.socket(fileno=int(raw_fd))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{ENV_LISTEN_FD} is invalid") from exc
    address = listener.getsockname()
    if (
        listener.family != socket.AF_INET
        or address[:2] != ("127.0.0.1", cfg["port"])
    ):
        listener.close()
        raise ConfigError(
            f"{ENV_LISTEN_FD} must be a listening socket for "
            f"127.0.0.1:{cfg['port']}"
        )
    listener.setblocking(False)
    return listener


async def run_server(fg: bool) -> None:
    global _log_stderr
    _log_stderr = fg
    open_log()
    cfg = get_config()
    # Fail before binding when the credential-bearing DB is unreadable or unsafe.
    get_providers()
    listener = _inherited_loopback_listener(cfg)
    app = create_app()

    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        try:
            site = (
                web.SockSite(runner, listener)
                if listener is not None
                else web.TCPSite(runner, "127.0.0.1", cfg["port"])
            )
            await site.start()
        except OSError as exc:
            log(f"FATAL: cannot listen on 127.0.0.1:{cfg['port']}: {exc}")
            raise

        log(
            f"claude-hub listening on 127.0.0.1:{cfg['port']} "
            f"(channels: {', '.join(cfg['channels'])}; "
            f"default: {cfg['default_channel']})"
        )
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        if listener is not None:
            listener.close()


# ---------------------------------------------------------------- CLI


def cli_list() -> None:
    cfg = get_config()
    providers = get_providers()
    print(f"claude-hub channels (port {cfg['port']}, default *):\n")
    width = max(len(alias) for alias in cfg["channels"])
    for alias, channel in cfg["channels"].items():
        provider = _match_channel_provider(channel, providers)
        provider_label = channel.get("provider") or "CC Switch URL match"
        marker = "*" if alias == cfg["default_channel"] else " "
        status = "ready" if provider else "provider missing from DB"
        print(
            f" {marker}{alias:<{width}}  {provider_label:<16} "
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
        for alias, channel in cfg["channels"].items():
            problems = []
            models = channel.get("models", [])
            if not models:
                problems.append("no selectable models")
            provider = _match_channel_provider(channel, providers)
            if provider is None:
                problems.append("provider missing")
            else:
                if not provider.get("token"):
                    problems.append("credential missing")
                try:
                    validate_upstream_url(
                        provider.get("base_url", ""),
                        alias,
                        channel.get("allow_insecure_http", False),
                    )
                except RouteError:
                    problems.append("upstream policy invalid")
                else:
                    api_format = channel.get("api_format") or provider.get(
                        "api_format", "anthropic"
                    )
                    if not _full_endpoint_matches_format(provider, api_format):
                        problems.append("full endpoint format mismatch")
            if problems:
                report("FAIL", f"channel '{alias}': {', '.join(problems)}")
            else:
                report("OK", f"channel '{alias}' ready ({len(models)} models)")

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
        probe = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
        api_format = provider.get("api_format", "anthropic")
        endpoint, upstream_payload = transform_request(
            probe,
            api_format,
            provider_type=provider.get("provider_type"),
        )
        if api_format == "anthropic":
            headers = CIMultiDict(
                {
                    "authorization": f"Bearer {provider['token']}",
                    "x-api-key": provider["token"],
                    "anthropic-version": "2023-06-01",
                    "user-agent": "claude-cli/2.1.0 (external, cli)",
                    "x-app": "cli",
                    "anthropic-beta": "claude-code-20250219",
                }
            )
            ensure_1m_beta(headers, model)
        else:
            headers = _transformed_headers(provider["token"], False)
        started = time.monotonic()
        try:
            async with session.post(
                _upstream_url(provider, endpoint),
                json=upstream_payload,
                headers=headers,
                proxy=channel_proxy(alias, cfg, provider),
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                duration = time.monotonic() - started
                if response.status == 200:
                    return alias, f"✓ 200 {duration:.1f}s ({model})"
                body = (await response.text())[:120].replace("\n", " ")
                return (
                    alias,
                    f"✗ {response.status} {duration:.1f}s ({model}) {body}",
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return alias, f"✗ {type(exc).__name__}: {exc}"

    async with aiohttp.ClientSession(connector=_upstream_connector()) as session:
        results = await asyncio.gather(*(one(session, alias) for alias in aliases))
    width = max(len(alias) for alias in aliases)
    for alias, message in results:
        print(f"  {alias:<{width}}  {message}")


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
