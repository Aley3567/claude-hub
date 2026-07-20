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
import hmac
import ipaddress
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web


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

HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
REQ_STRIP = HOP_BY_HOP | {"content-length", "authorization", "x-api-key"}
RESP_STRIP = HOP_BY_HOP | {"content-length", "content-encoding"}

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
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    _log_fp = path.open("a", encoding="utf-8")


# ---------------------------------------------------------------- config / DB


class ConfigError(ValueError):
    """The hub configuration is missing or unsafe to use."""


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
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid config {path}: {exc}") from exc
        _cfg_cache.update(
            {"path": path, "mtime_ns": st.st_mtime_ns, "size": st.st_size, "raw": raw}
        )
    return validate_config(_cfg_cache["raw"])


_db_cache: dict[str, object] = {
    "path": None,
    "mtime_ns": None,
    "size": None,
    "rows": {},
}


def _normalize_base_url(value: object) -> str:
    base = value.strip().rstrip("/") if isinstance(value, str) else ""
    # Forwarded paths already begin with /v1. Avoid /v1/v1/messages.
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def get_providers() -> dict:
    """Return provider data from CC Switch, opening SQLite with ``mode=ro``."""
    path = db_path()
    try:
        st = path.stat()
        cache_key = (path, st.st_mtime_ns, st.st_size)
        old_key = (
            _db_cache["path"],
            _db_cache["mtime_ns"],
            _db_cache["size"],
        )
        if cache_key == old_key:
            return _db_cache["rows"]

        db_uri = path.resolve(strict=False).as_uri() + "?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        try:
            cursor = conn.execute(
                "SELECT name, settings_config FROM providers WHERE app_type='claude'"
            )
            rows = {}
            for name, settings_config in cursor.fetchall():
                try:
                    env = json.loads(settings_config).get("env") or {}
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                base = _normalize_base_url(env.get("ANTHROPIC_BASE_URL"))
                if not base:
                    continue
                token = (
                    env.get("ANTHROPIC_AUTH_TOKEN")
                    or env.get("ANTHROPIC_API_KEY")
                    or ""
                )
                rows[name] = {
                    "base_url": base,
                    "token": token,
                    "model_map": {
                        tier: env.get(f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL")
                        for tier in ("opus", "sonnet", "haiku")
                    },
                }
        finally:
            conn.close()

        _db_cache.update(
            {
                "path": path,
                "mtime_ns": st.st_mtime_ns,
                "size": st.st_size,
                "rows": rows,
            }
        )
    except (sqlite3.Error, OSError) as exc:
        log(f"db read failed, using cached providers: {exc}")
        if _db_cache["path"] != path:
            return {}
    return _db_cache["rows"]


def reset_caches() -> None:
    """Clear file caches. Primarily useful for isolated diagnostics and tests."""
    _cfg_cache.update({"path": None, "mtime_ns": None, "size": None, "raw": None})
    _db_cache.update(
        {"path": None, "mtime_ns": None, "size": None, "rows": {}}
    )


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


def route(model_in: str, cfg: dict) -> tuple[str, str]:
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

    alias = cfg["default_channel"]
    channel = cfg["channels"].get(alias)
    if not channel:
        raise RouteError(500, f"default_channel '{alias}' not in channels config")
    provider = get_providers().get(channel["provider"])
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
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (
        _is_loopback(parsed.hostname) or allow_insecure_http
    ):
        return
    if parsed.scheme == "http":
        raise RouteError(
            502,
            f"channel '{alias}' uses remote HTTP; set allow_insecure_http=true "
            "for this channel only if it is intentional",
        )
    raise RouteError(502, f"channel '{alias}' upstream must use http or https")


def resolve_provider(alias: str, cfg: dict) -> dict:
    channel = cfg["channels"].get(alias)
    if not channel:
        raise RouteError(400, f"unknown channel alias '{alias}'")
    provider = get_providers().get(channel["provider"])
    if not provider:
        raise RouteError(
            502,
            f"channel '{alias}' provider was not found in the CC Switch database "
            f"(check {config_path().name})",
        )
    validate_upstream_url(
        provider["base_url"],
        alias,
        channel.get("allow_insecure_http", False),
    )
    return provider


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


def ensure_1m_beta(headers: dict, model_out: str) -> None:
    """Ensure and de-duplicate the context-1m beta marker for ``[1m]`` models."""
    if "[1m]" not in model_out.lower():
        return

    beta_values = []
    beta_keys = []
    for key, value in list(headers.items()):
        if key.lower() == "anthropic-beta":
            beta_keys.append(key)
            beta_values.extend(part.strip() for part in value.split(",") if part.strip())

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

    for key in beta_keys:
        headers.pop(key, None)
    headers["anthropic-beta"] = ",".join(unique_values)


def upstream_headers(request: web.Request, token: str) -> dict:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in REQ_STRIP
    }
    headers["authorization"] = f"Bearer {token}"
    headers["x-api-key"] = token
    return headers


async def handle_messages(request: web.Request) -> web.StreamResponse:
    cfg = get_config()
    if not check_local_auth(request, cfg):
        return anthropic_error(
            401, "invalid local hub token", "authentication_error"
        )

    body = await request.read()
    is_count = request.path.endswith("/count_tokens")
    try:
        payload = json.loads(body)
        model_in = payload.get("model", "")
    except (json.JSONDecodeError, AttributeError):
        payload, model_in = None, ""

    started = time.monotonic()
    try:
        alias, model_out = route(model_in, cfg)
        provider = resolve_provider(alias, cfg)
    except RouteError as exc:
        log(f"{request.path} '{model_in}' -> ROUTE ERROR {exc.status}: {exc.message}")
        return anthropic_error(
            exc.status,
            exc.message,
            "api_error" if exc.status >= 500 else "invalid_request_error",
        )

    if payload is not None:
        payload["model"] = model_out
        data = json.dumps(payload, ensure_ascii=False).encode()
    else:
        data = body

    path_and_query = request.path_qs
    url = provider["base_url"] + path_and_query
    headers = upstream_headers(request, provider["token"])
    ensure_1m_beta(headers, model_out)
    proxy = channel_proxy(alias, cfg)
    session: aiohttp.ClientSession = request.app["session"]
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
            if is_count and upstream.status in (404, 405, 501):
                estimate = max(
                    1,
                    len(
                        json.dumps(
                            {
                                "m": (payload or {}).get("messages"),
                                "s": (payload or {}).get("system"),
                            },
                            ensure_ascii=False,
                        )
                    )
                    // 4,
                )
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"upstream {upstream.status}, estimated {estimate} tokens"
                )
                return web.json_response(
                    {"input_tokens": estimate},
                    headers={"x-hub-estimated": "1"},
                )

            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in RESP_STRIP:
                    response.headers[key] = value
            response.headers["x-hub-channel"] = alias
            response.headers["x-hub-model"] = model_out
            await response.prepare(request)

            byte_count = 0
            streamed = upstream.headers.get("content-type", "").startswith(
                "text/event-stream"
            )
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                    byte_count += len(chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log(
                    f"{request.path} '{model_in}' -> {alias}/{model_out} "
                    f"upstream broke mid-stream after {byte_count}B: {exc}"
                )
                return response

            await response.write_eof()
            log(
                f"{request.path} '{model_in}' -> {alias}/{model_out} "
                f"{upstream.status} {'stream' if streamed else 'json'} "
                f"{time.monotonic() - started:.1f}s {byte_count}B"
            )
            return response
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


async def handle_fallback(request: web.Request) -> web.Response:
    return anthropic_error(
        404,
        f"claude-hub: no route for {request.method} {request.path}",
        "not_found_error",
    )


# ---------------------------------------------------------------- server


async def _close_session(app: web.Application) -> None:
    await app["session"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["session"] = aiohttp.ClientSession(auto_decompress=True)
    app.on_cleanup.append(_close_session)
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
    app = create_app()

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", cfg["port"])
        await site.start()
    except OSError as exc:
        log(f"FATAL: cannot bind 127.0.0.1:{cfg['port']}: {exc}")
        await runner.cleanup()
        raise

    log(
        f"claude-hub listening on 127.0.0.1:{cfg['port']} "
        f"(channels: {', '.join(cfg['channels'])}; default: {cfg['default_channel']})"
    )
    try:
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
        status = "ready" if provider else "provider missing from DB"
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
        headers = {
            "authorization": f"Bearer {provider['token']}",
            "x-api-key": provider["token"],
            "anthropic-version": "2023-06-01",
            "user-agent": "claude-cli/2.1.0 (external, cli)",
            "x-app": "cli",
            "anthropic-beta": "claude-code-20250219",
        }
        ensure_1m_beta(headers, model)
        started = time.monotonic()
        try:
            async with session.post(
                provider["base_url"] + "/v1/messages",
                json={
                    "model": model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=headers,
                proxy=channel_proxy(alias, cfg),
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

    async with aiohttp.ClientSession() as session:
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


if __name__ == "__main__":
    main()
