#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run Claude Code once with a specific CC Switch Claude provider.

Injects provider credentials as process-level environment variables only —
no settings files are modified, no lock needed, any number of concurrent
claude1 sessions are safe.

Usage:
  claude1              # interactive menu
  claude1 deepseek     # match by name substring (case-insensitive)
  claude1 mimo         # matches the first provider whose name contains "mimo"
"""

from __future__ import annotations

import errno
import json
import hmac
import math
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from claude1_protocol import provider_api_format
import claude_hub_catalog as hub_catalog
from claude1_account_pool import (
    AccountCandidate,
    AccountPool,
    AccountPoolError,
    PoolConfigError,
    PoolConfigStore,
    PoolExhausted,
    credential_fingerprint,
    normalize_account_endpoint,
)
from claude1_transport import (
    TransportConfigError,
    diagnose_transport_policy,
    normalize_transport_config,
    resolve_transport_policy,
)


VERSION = "0.1.0"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = _env_path("CLAUDE1_HOME", Path.home())
DB_PATH = _env_path("CLAUDE1_DB_PATH", HOME / ".cc-switch" / "cc-switch.db")
DEFAULT_CLAUDE_BIN = _env_path(
    "CLAUDE1_DEFAULT_CLAUDE_BIN", HOME / ".local" / "bin" / "claude"
)
MRU_PATH = _env_path("CLAUDE1_MRU_PATH", HOME / ".cc-switch" / "claude1-mru.json")
CONFIG_PATH = _env_path(
    "CLAUDE1_CONFIG_PATH", HOME / ".cc-switch" / "claude1-config.json"
)
ACCOUNT_POOL_CONFIG = _env_path(
    "CLAUDE1_ACCOUNT_POOL_CONFIG",
    HOME / ".cc-switch" / "claude1-account-pools.json",
)
ACCOUNT_POOL_STATE = _env_path(
    "CLAUDE1_ACCOUNT_POOL_STATE",
    HOME / ".cc-switch" / "claude1-account-state.sqlite3",
)
# 最近一次实际启动记录；普通启动只能写这里，不能改变粘性入口。
BACKEND_STATE = _env_path(
    "CLAUDE1_BACKEND_STATE", HOME / ".cc-switch" / "claude1-backend.json"
)
# 纯文本一行的「粘性后端」，只有 `claude1 use` 可以原子写入。
BACKEND_STICKY = _env_path(
    "CLAUDE1_BACKEND_STICKY", HOME / ".cc-switch" / "claude1-backend"
)
ANYROUTER_OBSERVER = _env_path(
    "CLAUDE1_ANYROUTER_OBSERVER", HOME / "anyrouter-tools" / "observe-claude1.sh"
)

# Composable backends & overlays (claude1 [backend] [overlay...] -- <claude args>)
ANYROUTER_SETTINGS = _env_path(
    "CLAUDE1_ANYROUTER_SETTINGS", HOME / ".claude" / "settings.anyrouter.json"
)
NOTION_MCP = _env_path(
    "CLAUDE1_NOTION_MCP", HOME / ".claude" / "mcp-notion.json"
)
TEMP_DIR = (
    _env_path("CLAUDE1_TMP_DIR", HOME / ".cache" / "claude1")
    if os.environ.get("CLAUDE1_TMP_DIR")
    else None
)

# First positional token → a backend instead of a provider-name hint.
BACKEND_ALIASES = {
    "any": "anyrouter",
    "anyrouter": "anyrouter",
    "cc": "current",
    "current": "current",
    "direct": "direct",
    "hub": "hub",
}
RESERVED_SELECTOR_WORDS = set(BACKEND_ALIASES) | {
    "config",
    "doctor",
    "help",
    "list",
    "usage",
    "accounts",
    "use",
    "version",
}

# claude-hub: 本地多渠道路由网关（会话内 /model 别名,模型 热切换渠道）。
HUB_SCRIPT = _env_path(
    "CLAUDE1_HUB_SCRIPT", HOME / ".claude" / "scripts" / "claude-hub.py"
)
HUB_CONFIG = _env_path(
    "CLAUDE1_HUB_CONFIG", HOME / ".cc-switch" / "claude-hub.json"
)
HUB_DB = _env_path("CLAUDE1_HUB_DB", DB_PATH)
HUB_LOG = _env_path(
    "CLAUDE1_HUB_LOG", HOME / ".cc-switch" / "logs" / "claude-hub.log"
)
HUB_USAGE = _env_path(
    "CLAUDE_HUB_USAGE", HOME / ".cc-switch" / "logs" / "claude-hub-usage.jsonl"
)
HUB_LISTEN_FD_ENV = "CLAUDE_HUB_LISTEN_FD"
HUB_CATALOG = _env_path(
    "CLAUDE1_HUB_CATALOG", HOME / ".cc-switch" / "claude-hubs.json"
)
# Existing single-Hub path/port overrides retain their legacy contract unless
# the caller explicitly opts into a catalog.
_LEGACY_HUB_OVERRIDE_KEYS = {
    "CLAUDE1_HUB_CONFIG",
    "CLAUDE1_HUB_PORT",
    "CLAUDE1_HUB_LOG",
    "CLAUDE_HUB_USAGE",
}
HUB_CATALOG_ENABLED = (
    "CLAUDE1_HUB_CATALOG" in os.environ
    or not any(key in os.environ for key in _LEGACY_HUB_OVERRIDE_KEYS)
)
_hub_processes: list[subprocess.Popen] = []


@dataclass(frozen=True)
class HubRef:
    """Stable identity plus the isolated filesystem paths for one Hub."""

    hub_id: str
    name: str
    config_path: Path
    log_path: Path
    usage_path: Path
    legacy: bool = False
    state: str = "ready"
    draft_path: Path | None = None

# Optional local protocol-translation gateway (cliproxyapi). Providers whose
# configured base URL points here need the gateway alive before Claude starts.
GATEWAY_URL = os.environ.get("CLAUDE1_GATEWAY_URL", "http://127.0.0.1:18317")
GATEWAY_BIN = _env_path(
    "CLAUDE1_GATEWAY_BIN", Path("/opt/homebrew/bin/cliproxyapi")
)
GATEWAY_CONFIG = _env_path(
    "CLAUDE1_GATEWAY_CONFIG",
    HOME / ".config" / "cliproxyapi-claudex" / "config.yaml",
)
GATEWAY_LOG = _env_path(
    "CLAUDE1_GATEWAY_LOG",
    HOME / ".local" / "state" / "cliproxyapi-claudex" / "proxy.log",
)


def resolve_claude_bin() -> Path:
    """Locate the claude executable so node/nvm version upgrades don't break us.

    Priority:
      1. CLAUDE1_CLAUDE_BIN env var (explicit override).
      2. `claude` found on PATH (shutil.which) — resolves the live nvm/npm symlink,
         so changing the active node version keeps working.
      3. DEFAULT_CLAUDE_BIN fallback.
    """
    override = os.environ.get("CLAUDE1_CLAUDE_BIN")
    if override:
        return Path(override)
    found = shutil.which("claude")
    if found:
        return Path(found)
    return DEFAULT_CLAUDE_BIN


def load_mru() -> dict[str, float]:
    try:
        data = json.loads(MRU_PATH.read_text())
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError):
        return {}


def _atomic_private_write(path: Path, text: str) -> None:
    """Atomically replace a local state file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _open_private_append(path: Path):
    """Open an append-only runtime log without a world-readable creation race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        expected = path.lstat()
    except FileNotFoundError:
        expected = None
    if expected is not None and not stat.S_ISREG(expected.st_mode):
        raise OSError("runtime log path is not a regular file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("runtime log path is not a regular file")
        if expected is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise OSError("runtime log path changed while it was being opened")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


def record_use(name: str) -> None:
    mru = load_mru()
    mru[name] = time.time()
    try:
        _atomic_private_write(
            MRU_PATH,
            json.dumps(mru, ensure_ascii=False, indent=1),
        )
    except OSError:
        pass  # MRU is best-effort; never block a launch on it


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text())
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            for k, v in list(data["providers"].items()):
                if not isinstance(v, dict):
                    data["providers"][k] = {"enabled": bool(v)}
            data.setdefault("version", 1)
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "providers": {}}


def save_config(cfg: dict) -> bool:
    try:
        _atomic_private_write(
            CONFIG_PATH,
            json.dumps(cfg, ensure_ascii=False, indent=2),
        )
        return True
    except OSError:
        return False


def sync_config(cfg: dict, db_providers: list[dict]) -> bool:
    """Keep presentation config keyed by stable CC Switch provider ids.

    v1/v2 used provider names as keys, which made duplicate names collapse to
    one row. v3 migrates the old metadata onto the first matching id, retains
    hidden state for every duplicate, and requires separate aliases thereafter.
    """
    old_providers = cfg.get("providers")
    if not isinstance(old_providers, dict):
        old_providers = {}
    changed = False

    if cfg.get("version", 1) < 3:
        providers: dict[str, dict] = {}
        seen_names: set[str] = set()
        for provider in db_providers:
            provider_id = str(provider["id"])
            name = str(provider["name"])
            legacy = old_providers.get(name)
            meta = dict(legacy) if isinstance(legacy, dict) else {}
            if "hidden" not in meta:
                meta["hidden"] = meta.get("enabled") is False
            meta.pop("enabled", None)
            # One legacy name-level alias cannot safely identify two rows.
            if name in seen_names:
                meta.pop("alias", None)
            meta["name"] = name
            providers[provider_id] = meta
            seen_names.add(name)
        cfg["providers"] = providers
        cfg["version"] = 3
        return True

    providers = old_providers
    cfg["providers"] = providers
    for provider in db_providers:
        provider_id = str(provider["id"])
        name = str(provider["name"])
        meta = providers.get(provider_id)
        if not isinstance(meta, dict):
            providers[provider_id] = {"name": name, "hidden": False}
            changed = True
            continue
        if meta.get("name") != name:
            meta["name"] = name
            changed = True
        if "hidden" not in meta:
            meta["hidden"] = False
            changed = True
        if "enabled" in meta:
            meta.pop("enabled", None)
            changed = True
    return changed


def provider_by_id(provider_id: str) -> dict | None:
    for row in db_claude_rows():
        if str(row["id"]) == provider_id:
            return _provider_from_row(row)
    return None


def current_provider() -> dict:
    matches = [
        _provider_from_row(row)
        for row in db_claude_rows()
        if "is_current" in row.keys() and bool(row["is_current"])
    ]
    if not matches:
        raise RuntimeError("CC Switch DB 中没有 is_current=1 的 Claude provider")
    if len(matches) > 1:
        ids = "、".join(str(provider["id"]) for provider in matches)
        raise RuntimeError(
            f"CC Switch DB 中有多个 is_current=1 的 Claude provider: {ids}"
        )
    return matches[0]


MANAGED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
)
MANAGED_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_HUB_LOCAL_TOKEN",
}
# This is a presentation preference, not routing or session identity. The
# default shell integration intentionally sets it and it is safe to retain.
CLAUDE_CHILD_PASSTHROUGH = {
    "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN",
}


def managed_key(key: str) -> bool:
    folded = key.upper()
    return folded in MANAGED_ENV_KEYS or any(
        folded.startswith(prefix) for prefix in MANAGED_ENV_PREFIXES
    )


def claude_child_env(settings: dict | None = None) -> dict[str, str]:
    """Build a Claude process environment without inherited routing state."""
    child = {
        key: value
        for key, value in os.environ.items()
        if not managed_key(key)
    }
    for key in CLAUDE_CHILD_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            child[key] = value

    settings_env = settings.get("env") if isinstance(settings, dict) else None
    if isinstance(settings_env, dict):
        for key, value in settings_env.items():
            if isinstance(key, str) and value is not None:
                child[key] = str(value)
    return child


def _run_claude(command: list[str], *, env: dict[str, str]) -> int:
    """Run Claude in its own process group and forward Ctrl-C to that group.

    A wrapper process normally receives the terminal's SIGINT along with Claude.
    ``subprocess.run`` then turns that into ``KeyboardInterrupt`` in the wrapper,
    which can tear the child down before Claude handles the interrupt.  Keeping
    Claude in a separate session lets the wrapper relay every interrupt and keep
    waiting, so Claude retains its normal "cancel this turn" behavior.
    """
    proc = subprocess.Popen(
        command,
        env=env,
        start_new_session=(os.name == "posix"),
    )
    while True:
        try:
            return int(proc.wait())
        except KeyboardInterrupt:
            if proc.poll() is not None:
                continue
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGINT)
                else:
                    proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass


def _local_gateway_url(base_url: str) -> bool:
    """Whether a provider needs this launcher's local cliproxyapi gateway."""
    try:
        parsed = urlparse(base_url)
        return parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == 18317
    except ValueError:
        return False


def _provider_uses_local_gateway(provider: dict) -> bool:
    """Best-effort doctor check without mutating a provider's settings."""
    try:
        config = _provider_settings(provider)
        env = _provider_environment(provider, config)
    except RuntimeError:
        return False
    base_url = env.get("ANTHROPIC_BASE_URL")
    return isinstance(base_url, str) and _local_gateway_url(base_url)


def db_claude_rows() -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise RuntimeError(f"CC Switch DB 不存在: {DB_PATH}")
    db_uri = DB_PATH.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(providers)")
        }
        selected = ["id", "name", "settings_config"]
        selected.extend(
            column
            for column in ("meta", "provider_type", "is_current")
            if column in columns
        )
        return conn.execute(
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type='claude' ORDER BY sort_index"
        ).fetchall()
    finally:
        conn.close()


def subagent_model_overrides() -> tuple[list[tuple[str, str]], list[str]]:
    """List providers whose persisted settings pin every Claude subagent."""
    if not DB_PATH.exists():
        raise RuntimeError(f"CC Switch DB 不存在: {DB_PATH}")
    db_uri = DB_PATH.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT id, name, settings_config FROM providers ORDER BY app_type, sort_index"
        ).fetchall()
    finally:
        conn.close()

    overrides: list[tuple[str, str]] = []
    invalid: list[str] = []
    for provider_id, name, raw_settings in rows:
        provider = {
            "id": provider_id,
            "name": name,
            "settings_config": raw_settings,
        }
        try:
            settings = _provider_settings(provider)
            env = _provider_environment(provider, settings)
        except RuntimeError:
            invalid.append(str(name))
            continue
        if SUBAGENT_MODEL_KEY in env:
            overrides.append((str(provider_id), str(name)))
    return overrides, invalid


def _provider_from_row(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "name": row["name"],
        "settings_config": row["settings_config"],
        "meta": row["meta"] if "meta" in keys else "{}",
        "provider_type": row["provider_type"] if "provider_type" in keys else None,
        "is_current": bool(row["is_current"]) if "is_current" in keys else False,
    }


def selected_provider_api_format(provider: dict) -> str:
    try:
        settings = json.loads(provider.get("settings_config") or "{}")
    except (json.JSONDecodeError, TypeError):
        settings = {}
    try:
        meta = json.loads(provider.get("meta") or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return provider_api_format(
        meta=meta,
        settings=settings,
        provider_type=provider.get("provider_type"),
    )


def provider_transport_config(provider: dict, settings: dict | None = None) -> dict:
    """Return normalized transport intent for one CC Switch provider."""
    if settings is None:
        try:
            settings = json.loads(provider.get("settings_config") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"provider {provider.get('name', '<unknown>')} 的 settings_config 无效"
            ) from exc
    if not isinstance(settings, dict):
        raise RuntimeError(
            f"provider {provider.get('name', '<unknown>')} 的 settings_config 必须是 JSON 对象"
        )
    env = settings.get("env") or {}
    if not isinstance(env, dict):
        raise RuntimeError(
            f"provider {provider.get('name', '<unknown>')} 的 env 必须是 JSON 对象"
        )
    raw = settings.get("transport")
    if raw is None:
        folded = {str(key).upper(): value for key, value in env.items()}
        explicit = (
            folded.get("HTTPS_PROXY")
            or folded.get("HTTP_PROXY")
            or folded.get("ALL_PROXY")
        )
        raw = (
            {"mode": "proxy", "proxies": [explicit]}
            if isinstance(explicit, str) and explicit.strip()
            else {"mode": "auto", "proxies": ["system"]}
        )
    try:
        return normalize_transport_config(raw)
    except TransportConfigError as exc:
        raise RuntimeError(
            f"provider {provider.get('name', '<unknown>')} 的 transport 无效: {exc}"
        ) from exc


def list_providers() -> list[dict]:
    rows = db_claude_rows()
    providers = [_provider_from_row(row) for row in rows]
    by_id = {str(provider["id"]): provider for provider in providers}
    cfg = load_config()
    changed = sync_config(cfg, providers)
    if changed:
        save_config(cfg)
    visible = [
        provider_id
        for provider_id, meta in cfg["providers"].items()
        if not meta.get("hidden")
    ]
    ordered = []
    for provider_id in visible:
        if provider_id in by_id:
            entry = dict(by_id[provider_id])
            alias = cfg["providers"][provider_id].get("alias")
            if alias:
                entry["alias"] = alias
            ordered.append(entry)
    if not ordered:
        # 全被隐藏(或配置为空) —— 别把人困住，回退到全部。
        ordered = providers
    return ordered


# Model-slot env keys Claude Code renders in /model. The private settings
# file only overrides the keys it defines; anything absent still falls through
# from ~/.claude/settings.json (synced from the CC Switch current provider)
# and would relabel or repoint this provider's slots.
MODEL_SLOT_TIERS = ("OPUS", "SONNET", "HAIKU", "FABLE")
SUBAGENT_MODEL_KEY = "CLAUDE_CODE_SUBAGENT_MODEL"


def _seal_model_slots(env: dict[str, str]) -> None:
    """Seal provider model slots against user-settings leftovers.

    Claude Code merges the user settings env under this private file, so a
    slot key the provider does not define (e.g. a Haiku ``_NAME``) leaks in
    from the CC Switch current provider and shows a foreign model in /model.
    Slots the provider owns get honest fallbacks because the menu reads
    ``_NAME``/``_DESCRIPTION`` with ``??`` (an empty string would render a
    blank label); slots it does not serve are blanked entirely — Claude Code
    treats an empty slot model as "no custom slot" and never reads the
    sibling keys then.
    """
    groups = [
        (f"ANTHROPIC_DEFAULT_{tier}_MODEL", f"Custom {tier.title()} model")
        for tier in MODEL_SLOT_TIERS
    ]
    groups.append(("ANTHROPIC_CUSTOM_MODEL_OPTION", ""))
    for model_key, fallback_description in groups:
        name_key = f"{model_key}_NAME"
        description_key = f"{model_key}_DESCRIPTION"
        capabilities_key = f"{model_key}_SUPPORTED_CAPABILITIES"
        model_value = env.get(model_key) or ""
        if model_value:
            env.setdefault(name_key, model_value)
            if model_key == "ANTHROPIC_CUSTOM_MODEL_OPTION":
                fallback_description = f"Custom model ({model_value})"
            env.setdefault(description_key, fallback_description)
            env.setdefault(capabilities_key, "")
        else:
            env[model_key] = ""
            env[name_key] = ""
            env[description_key] = ""
            env[capabilities_key] = ""
    env[SUBAGENT_MODEL_KEY] = ""


def _provider_settings(provider: dict) -> dict:
    name = str(provider.get("name") or provider.get("id") or "<unknown>")
    try:
        settings = json.loads(provider.get("settings_config") or "{}")
    except (json.JSONDecodeError, TypeError, UnicodeError, RecursionError) as exc:
        raise RuntimeError(
            f"provider {name} 的 settings_config 无效"
        ) from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"provider {name} 的 settings_config 必须是 JSON 对象")
    return settings


def _provider_environment(provider: dict, settings: dict) -> dict:
    raw_env = settings.get("env")
    if raw_env is None:
        return {}
    if not isinstance(raw_env, dict):
        name = str(provider.get("name") or provider.get("id") or "<unknown>")
        raise RuntimeError(f"provider {name} 的 env 必须是 JSON 对象")
    return raw_env


def build_settings(provider: dict) -> dict:
    """Return the provider settings_config from CC Switch DB with NO_PROXY applied."""
    cfg = _provider_settings(provider)
    raw_env = _provider_environment(provider, cfg)
    env = {
        k: str(v)
        for k, v in raw_env.items()
        if k != SUBAGENT_MODEL_KEY
    }

    if not any(k.startswith("ANTHROPIC_AUTH") or k.startswith("ANTHROPIC_API") for k in env):
        # A credential-less entry falls back to the currently stored Claude
        # login for this one session.
        print(
            f"[claude1] 注意: provider {provider['name']} 没有独立凭证，将使用当前已登录的凭证",
            file=sys.stderr,
        )
    try:
        host = urlparse(env.get("ANTHROPIC_BASE_URL", "")).hostname
    except ValueError as exc:
        name = str(provider.get("name") or provider.get("id") or "<unknown>")
        raise RuntimeError(f"provider {name} 的 URL 无效") from exc
    explicit_proxy = any(
        k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for k in env
    )
    if host and explicit_proxy:
        # Provider explicitly routes through a proxy (e.g. anyrouter.top needs
        # the Clash node because Cloudflare refuses direct TLS from CN). Keep
        # the API host OUT of NO_PROXY or the bypass would silently defeat it.
        for key in ("NO_PROXY", "no_proxy"):
            parts = [
                p.strip()
                for p in env.get(key, "").split(",")
                if p.strip() and p.strip().casefold() != host.casefold()
            ]
            if parts:
                env[key] = ",".join(parts)
            else:
                env.pop(key, None)
    elif host:
        for key in ("NO_PROXY", "no_proxy"):
            parts = [p.strip() for p in env.get(key, "").split(",") if p.strip()]
            if all(part.casefold() != host.casefold() for part in parts):
                parts.append(host)
            env[key] = ",".join(parts)
    _seal_model_slots(env)
    cfg["env"] = env

    # Drop the cc-switch-specific top-level "model" alias (e.g. "opus[1m]");
    # Claude Code --settings does not read it, and model selection is already
    # fully expressed by the ANTHROPIC_*_MODEL env vars above.
    cfg.pop("model", None)

    return cfg


def _provider_account_credential(provider: dict) -> tuple[str, str, str]:
    """Return ``(env key, token, normalized base URL)`` without logging secrets."""
    settings = _provider_settings(provider)
    env = _provider_environment(provider, settings)
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    api_key = env.get("ANTHROPIC_API_KEY")
    if isinstance(auth_token, str) and auth_token:
        credential_key, token = "ANTHROPIC_AUTH_TOKEN", auth_token
    elif isinstance(api_key, str) and api_key:
        credential_key, token = "ANTHROPIC_API_KEY", api_key
    else:
        credential_key, token = "", ""
    raw_base = env.get("ANTHROPIC_BASE_URL")
    try:
        meta = json.loads(provider.get("meta") or "{}")
    except (json.JSONDecodeError, TypeError, UnicodeError, RecursionError):
        meta = {}
    is_full_url = isinstance(meta, dict) and meta.get("isFullUrl") is True
    base_url = normalize_account_endpoint(
        raw_base,
        is_full_url=is_full_url,
    )
    return credential_key, token, base_url


def _account_pool_directory(
    primary_provider: dict,
    definition,
    providers: list[dict] | None = None,
) -> tuple[dict[str, dict], dict[str, AccountCandidate], dict[str, tuple[str, str]]]:
    """Build one credential-only pool view from a single CC Switch snapshot."""
    primary = f"id:{primary_provider['id']}"
    if providers is None:
        providers = [_provider_from_row(row) for row in db_claude_rows()]
    records = {f"id:{provider['id']}": provider for provider in providers}
    records[primary] = primary_provider
    candidates: dict[str, AccountCandidate] = {}
    credentials: dict[str, tuple[str, str]] = {}
    for member in definition.members:
        record = records.get(member.selector)
        if record is None:
            candidates[member.selector] = AccountCandidate("")
            continue
        credential_key, token, base_url = _provider_account_credential(record)
        credentials[member.selector] = (credential_key, token)
        candidates[member.selector] = AccountCandidate(
            credential_fingerprint(token),
            endpoint=base_url,
            credential_type=credential_key,
        )
    return records, candidates, credentials


def apply_native_account_pool(provider: dict, settings: dict) -> tuple[dict, str | None]:
    """Choose one account for an entire native Claude session.

    Member rows contribute only their credential.  Endpoint, model, proxy and
    all other settings remain owned by the provider the user selected.
    """
    primary = f"id:{provider['id']}"
    scheduler = AccountPool(ACCOUNT_POOL_CONFIG, ACCOUNT_POOL_STATE)
    try:
        definition = scheduler.definition(primary)
    except AccountPoolError as exc:
        raise RuntimeError(f"账号池配置不可用: {exc}") from exc
    if definition is None:
        return settings, None

    records, candidates, credentials = _account_pool_directory(provider, definition)

    try:
        lease = scheduler.acquire(primary, candidates)
    except PoolExhausted as exc:
        retry = f"，约 {exc.retry_after} 秒后可重试" if exc.retry_after else ""
        raise RuntimeError(f"该 provider 的所有账号当前都不可用{retry}") from exc
    except AccountPoolError as exc:
        raise RuntimeError(f"账号池不可用: {exc}") from exc

    credential_key, token = credentials[lease.member]
    env = settings.setdefault("env", {})
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env[credential_key] = token
    member = records[lease.member]
    label = f"{member.get('name') or lease.member} [{str(member.get('id'))[:8]}]"
    return settings, label


def add_anyrouter_observer(settings: dict, provider_name: str) -> None:
    """Observe real Any router turn outcomes without reading conversation content."""
    if provider_name != "Any router" or not ANYROUTER_OBSERVER.is_file():
        return

    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
        settings["hooks"] = hooks
    elif not isinstance(hooks, dict):
        return
    commands = {
        "Stop": f"{ANYROUTER_OBSERVER} success",
        "StopFailure": f"{ANYROUTER_OBSERVER} failure",
    }
    for event, command in commands.items():
        groups = hooks.get(event)
        if groups is None:
            groups = []
            hooks[event] = groups
        elif not isinstance(groups, list):
            continue
        already_present = any(
            handler.get("command") == command
            for group in groups
            if isinstance(group, dict)
            for handler in group.get("hooks", [])
            if isinstance(handler, dict)
        )
        if not already_present:
            groups.append({
                "hooks": [{
                    "type": "command",
                    "command": command,
                    "timeout": 5,
                }]
            })


def gateway_healthy() -> bool:
    """Return whether the configured local gateway accepts a successful request.

    cliproxyapi exposes no versioned health contract that this launcher can
    authenticate, so this is deliberately only a liveness check: a completed
    2xx response at its configured endpoint.  It cannot prove that another
    service has not bound the same local port.
    """
    try:
        req = urllib.request.Request(GATEWAY_URL + "/", method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            status = getattr(response, "status", response.getcode())
            return 200 <= status < 300
    except urllib.error.HTTPError:
        return False
    except OSError:
        return False


def ensure_local_gateway(base_url: str) -> None:
    """Start the local cliproxyapi gateway if the provider routes through it."""
    if not _local_gateway_url(base_url):
        return
    if gateway_healthy():
        return
    if not (GATEWAY_BIN.is_file() and os.access(GATEWAY_BIN, os.X_OK)):
        raise RuntimeError(
            "本地网关可执行文件不存在或不可执行: "
            f"{GATEWAY_BIN}（请安装 cliproxyapi 或设置 CLAUDE1_GATEWAY_BIN）"
        )
    print("[claude1] 本地网关未运行，正在启动 cliproxyapi ...", file=sys.stderr)
    with _open_private_append(GATEWAY_LOG) as log:
        subprocess.Popen(
            [GATEWAY_BIN, "-config", str(GATEWAY_CONFIG)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    for _ in range(20):
        time.sleep(0.25)
        if gateway_healthy():
            return
    raise RuntimeError(f"本地网关启动失败，查看日志: {GATEWAY_LOG}")


def _hub_port(cfg: dict) -> int:
    raw = (
        cfg.get("port")
        if HUB_CATALOG_ENABLED
        else os.environ.get("CLAUDE1_HUB_PORT", cfg.get("port"))
    )
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise RuntimeError(f"hub 端口无效: {raw!r}")
    if isinstance(raw, str) and not raw.strip().isdigit():
        raise RuntimeError(f"hub 端口无效: {raw!r}")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise RuntimeError(f"hub 端口超出范围: {port}")
    return port


DEFAULT_HUB_TOKEN_ENV = "CLAUDE_HUB_LOCAL_TOKEN"


def _hub_token_env_name(cfg: dict) -> str:
    """Return the validated auth environment key used by claude-hub."""
    token_env = cfg.get("local_token_env", DEFAULT_HUB_TOKEN_ENV)
    if not isinstance(token_env, str) or not token_env.strip():
        raise RuntimeError("hub 配置中的 local_token_env 必须是非空字符串")
    return token_env.strip()


def _hub_local_token(cfg: dict) -> str:
    # Keep the launcher's auth token selection identical to claude-hub's
    # get_config(): a configured environment name takes precedence, then the
    # legacy config value preserves existing installations.
    token_env = _hub_token_env_name(cfg)
    token = os.environ.get(token_env) or cfg.get("local_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            "hub 本地凭证缺失：请在配置中设置 local_token，"
            f"或设置 {token_env}"
        )
    return token.strip()


def hub_healthy(
    port: int,
    token: str | None = None,
    instance_id: str | None = None,
) -> bool:
    """Accept public liveness, or verify a token-bound Hub identity proof."""
    try:
        path = "/readyz" if token else "/healthz"
        challenge = secrets.token_urlsafe(24) if token else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"X-Claude-Hub-Challenge": challenge} if challenge else {},
            method="GET",
        )
        # A loopback health probe must never follow the user's proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2) as response:
            if getattr(response, "status", response.getcode()) != 200:
                return False
            payload = json.loads(response.read(65537).decode("utf-8"))
        protocol = payload.get("protocol") if isinstance(payload, dict) else None
        contract_ok = (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("service") == "claude-hub"
            and isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and protocol == 1
        )
        if instance_id is not None:
            contract_ok = contract_ok and payload.get("identity_protocol") == 2
        if not contract_ok or token is None:
            return contract_ok
        proof = payload.get("proof")
        proof_version = (
            f"v2:{instance_id}:{port}" if instance_id is not None else f"v1:{port}"
        )
        proof_message = (
            f"claude-hub-ready:{proof_version}:{challenge}".encode("ascii")
        )
        expected = hmac.digest(token.encode("utf-8"), proof_message, "sha256").hex()
        return isinstance(proof, str) and hmac.compare_digest(proof, expected)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ValueError,
    ):
        return False


def _hub_start_env(
    port: int,
    *,
    token_env: str = DEFAULT_HUB_TOKEN_ENV,
    hub: HubRef | None = None,
) -> dict[str, str]:
    """Build a minimal child environment instead of inheriting secrets/proxies."""
    child: dict[str, str] = {
        "HOME": str(HOME),
        "PATH": os.environ.get("PATH", os.defpath),
        "CLAUDE_HUB_CONFIG": str(_hub_config_path(hub)),
        "CLAUDE_HUB_DB": str(HUB_DB),
        "CLAUDE_HUB_LOG": str(hub.log_path if hub is not None else HUB_LOG),
        "CLAUDE_HUB_USAGE": str(
            hub.usage_path if hub is not None else HUB_USAGE
        ),
        "CLAUDE_HUB_PORT": str(port),
        "CLAUDE1_ACCOUNT_POOL_CONFIG": str(ACCOUNT_POOL_CONFIG),
        "CLAUDE1_ACCOUNT_POOL_STATE": str(ACCOUNT_POOL_STATE),
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            child[key] = value
    # The Hub validates this configured name itself.  Passing only this one
    # secret preserves the scrubbed environment while supporting custom
    # local_token_env values used by existing hub configurations.
    local_token = os.environ.get(token_env)
    if local_token:
        child[token_env] = local_token
    return child


def _hub_start_timeout() -> float:
    raw = os.environ.get("CLAUDE1_HUB_START_TIMEOUT", "20")
    try:
        timeout = float(raw)
    except ValueError:
        raise RuntimeError(f"CLAUDE1_HUB_START_TIMEOUT 无效: {raw!r}") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise RuntimeError("CLAUDE1_HUB_START_TIMEOUT 应在 1–120 秒之间")
    return timeout


def _reserve_loopback_port(port: int = 0) -> socket.socket:
    """Own one listening port until the Hub inherits this exact socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        return listener
    except OSError:
        listener.close()
        raise


def _spawn_hub_process(
    log_path: Path,
    child_env: dict[str, str],
    listener: socket.socket,
) -> subprocess.Popen:
    """Start the Hub with ownership of an already-listening loopback socket."""
    listen_fd = listener.fileno()
    env = {**child_env, HUB_LISTEN_FD_ENV: str(listen_fd)}
    with _open_private_append(log_path) as log:
        return subprocess.Popen(
            [str(HUB_SCRIPT), "serve"],
            stdout=log,
            stderr=log,
            env=env,
            close_fds=True,
            pass_fds=(listen_fd,),
            start_new_session=True,
        )


def _stop_spawned_process(process: subprocess.Popen, timeout: float = 3) -> None:
    """Terminate only the child this launcher spawned, with a kill fallback."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
    if process in _hub_processes:
        _hub_processes.remove(process)


@contextmanager
def _hub_start_lock(hub: HubRef | None = None):
    """Serialize Hub startup between independently launched claude1 processes."""
    log_path = hub.log_path if hub is not None else HUB_LOG
    lock_name = (
        f"claude-hub-{hub.hub_id}.lock" if hub is not None else "claude-hub.lock"
    )
    lock_path = log_path.parent / lock_name
    with _private_file_lock(lock_path, "hub 启动"):
        yield


def ensure_hub(
    port: int,
    *,
    token: str | None = None,
    token_env: str = DEFAULT_HUB_TOKEN_ENV,
    hub: HubRef | None = None,
    instance_id: str | None = None,
) -> int:
    """Start the isolated claude-hub process unless its strict health check passes."""
    log_path = hub.log_path if hub is not None else HUB_LOG
    if hub_healthy(port, token, instance_id):
        return port
    with _hub_start_lock(hub):
        if hub is not None:
            latest = load_hub_config(hub=hub)
            port = _hub_port(latest)
            latest_instance = latest.get("instance_id")
            if isinstance(latest_instance, str):
                instance_id = latest_instance
        # Another claude1 may have completed startup while this process waited
        # for the inter-process lock.
        if hub_healthy(port, token, instance_id):
            return port
        if not HUB_SCRIPT.is_file():
            raise RuntimeError(f"hub 脚本不存在: {HUB_SCRIPT}")
        display_name = hub.name if hub is not None else "claude-hub"
        try:
            listener = _reserve_loopback_port(port)
        except OSError as exc:
            if hub_healthy(port, token, instance_id):
                return port
            if exc.errno != errno.EADDRINUSE:
                raise RuntimeError(
                    f"{display_name} 无法监听 127.0.0.1:{port}: {exc}"
                ) from exc
            if hub is None or not HUB_CATALOG_ENABLED:
                raise RuntimeError(
                    f"{display_name} 端口 {port} 已被占用"
                ) from exc
            previous_port = port
            port, listener = _reserve_reassigned_hub_port(hub, previous_port)
            print(
                f"[claude1] {display_name} 端口 {previous_port} 已被占用，"
                f"改用 {port}",
                file=sys.stderr,
            )
        print(f"[claude1] {display_name} 未运行，正在启动 ...", file=sys.stderr)
        with listener:
            process = _spawn_hub_process(
                log_path,
                _hub_start_env(port, token_env=token_env, hub=hub),
                listener,
            )
            # Keep detached children referenced so Popen can reap them without
            # emitting ResourceWarning; later starts prune completed children.
            _hub_processes[:] = [
                child for child in _hub_processes if child.poll() is None
            ]
            _hub_processes.append(process)
            deadline = time.monotonic() + _hub_start_timeout()
            while time.monotonic() < deadline:
                if hub_healthy(port, token, instance_id):
                    return port
                return_code = process.poll()
                if return_code is not None:
                    _stop_spawned_process(process)
                    raise RuntimeError(
                        f"claude-hub 启动进程提前退出（状态 {return_code}），"
                        f"查看日志: {log_path}"
                    )
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            _stop_spawned_process(process)
            raise RuntimeError(
                f"{display_name} 启动失败，查看日志: {log_path}"
            )


def _provider_terms(provider: dict) -> list[str]:
    terms = [
        str(provider.get("name", "")),
        f"id:{provider.get('id', '')}",
    ]
    alias = provider.get("alias")
    if isinstance(alias, str) and alias.strip():
        terms.append(alias.strip())
    return terms


def _provider_labels(providers: list[dict]) -> dict[str, str]:
    counts: dict[str, int] = {}
    for provider in providers:
        name = str(provider.get("name", ""))
        counts[name] = counts.get(name, 0) + 1
    labels: dict[str, str] = {}
    for provider in providers:
        provider_id = str(provider.get("id", ""))
        name = str(provider.get("name", ""))
        labels[provider_id] = (
            f"{name} [{_short_provider_id(provider_id)}]"
            if counts.get(name, 0) > 1
            else name
        )
    return labels


def match_providers(providers: list[dict], hint: str) -> tuple[list[dict], bool]:
    """Return de-duplicated matches and whether they are exact.

    Exact provider names and aliases win over substring matches.  `casefold`
    keeps matching predictable for non-ASCII names as well as English aliases.
    """
    needle = hint.strip().casefold()
    if not needle:
        return ([], False)
    exact: list[dict] = []
    fuzzy: list[dict] = []
    for provider in providers:
        terms = [term.casefold() for term in _provider_terms(provider)]
        if any(term == needle for term in terms):
            exact.append(provider)
        elif any(needle in term for term in terms):
            fuzzy.append(provider)
    return (exact, True) if exact else (fuzzy, False)


def choose(providers: list[dict], hint: str | None) -> dict:
    labels = _provider_labels(providers)
    if hint:
        matches, exact = match_providers(providers, hint)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and exact:
            names = "、".join(labels[str(p["id"])] for p in matches)
            raise RuntimeError(
                f"名称或别名 '{hint}' 存在冲突: {names}；请修改其中一个别名"
            )
        if len(matches) > 1:
            print("匹配到多个 provider，请选择:")
            for i, p in enumerate(matches, 1):
                alias = f" ({p['alias']})" if p.get("alias") else ""
                print(f"{i}. {labels[str(p['id'])]}{alias}")
            try:
                choice = input("> ").strip()
            except EOFError:
                raise RuntimeError("标准输入不可用，无法完成渠道选择；请指定唯一 provider") from None
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(matches):
                    return matches[idx]
            raise RuntimeError("无效选择，已取消")
        raise RuntimeError(f"找不到匹配 '{hint}' 的 provider")

    print("选择本次 Claude Code provider:")
    for i, p in enumerate(providers, 1):
        print(f"{i}. {labels[str(p['id'])]}")
    try:
        choice = input("> ").strip()
    except EOFError:
        raise RuntimeError("标准输入不可用，无法完成渠道选择；请指定 provider 名称、别名或 id:ID") from None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(providers):
            return providers[idx]
    matches, exact = match_providers(providers, choice)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and exact:
        names = "、".join(labels[str(p["id"])] for p in matches)
        raise RuntimeError(f"名称或别名 '{choice}' 存在冲突: {names}")
    raise RuntimeError("无效选择，已取消")


def _reserved_backend_provider(provider_word: str) -> dict | None:
    """Find an exact provider name shadowed by a positional backend command."""
    if not DB_PATH.is_file():
        return None
    try:
        providers = list_providers()
    except (OSError, RuntimeError, sqlite3.Error):
        return None
    folded = provider_word.casefold()
    return next(
        (
            provider
            for provider in providers
            if str(provider.get("name", "")).strip().casefold() == folded
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Claude-Hub 内置工作区数据模型
#
# 之前 hub 只是一堆配置字典即席画在屏幕上。这里把它固化成三个明确类型，
# 供 Claude1 主界面的 Hub 入口与二级工作区共用；均为只读快照，不改配置、
# 不启动进程。
# ---------------------------------------------------------------------------

# hub 配置只存 provider + 模型串，界面上的“类型”只能从模型名推导。
_HUB_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    ("claude", "Claude"),
    ("glm", "GLM"),
    ("grok", "Grok"),
    ("kimi", "Kimi"),
    ("k3", "Kimi"),
    ("deepseek", "DeepSeek"),
    ("mimo", "MiMo"),
    ("codex", "GPT / Codex"),
    ("gpt", "GPT / Codex"),
)
# 类型 → curses 调色板(C) 的键名，纯展示用途。
_HUB_FAMILY_COLOR = {
    "Claude": "orange",
    "GPT / Codex": "teal",
    "GLM": "accent",
    "Grok": "violet",
    "Kimi": "violet",
    "DeepSeek": "violet",
    "MiMo": "violet",
    "其他": "violet",
}

HUB_CONFIG_VERSION = 2
HUB_SLOT_ORDER = ("fable", "opus", "sonnet", "haiku")
HUB_EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
_HUB_API_FORMAT_CHOICES = (
    ("anthropic", "Anthropic Messages", "原生 Claude / Anthropic 兼容接口"),
    ("openai_chat", "OpenAI Chat Completions", "兼容 /chat/completions"),
    ("openai_responses", "OpenAI Responses", "兼容 /responses"),
)
_HUB_API_FORMAT_SHORTCUTS = {
    "a": 0,
    "c": 1,
    "r": 2,
}
HUB_DEFAULT_EFFORTS = {
    "fable": "xhigh",
    "opus": "high",
    "sonnet": "high",
    "haiku": "high",
}
HUB_MODEL_SLOT_CAPABILITIES = (
    "thinking,adaptive_thinking,effort,xhigh_effort"
)


def _legacy_hub_ref() -> HubRef:
    return HubRef(
        hub_id=hub_catalog.LEGACY_HUB_ID,
        name="Claude-Hub",
        config_path=HUB_CONFIG,
        log_path=HUB_LOG,
        usage_path=HUB_USAGE,
        legacy=True,
    )


def _hub_config_path(hub: HubRef | None) -> Path:
    return hub.config_path if hub is not None else HUB_CONFIG


def _read_regular_text(path: Path, label: str) -> str:
    """Read one regular file without following a swapped symlink."""
    expected = path.lstat()
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError(f"{label}必须是普通文件")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise OSError(f"{label}在读取期间发生变化")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def _private_file_lock(lock_path: Path, label: str):
    if os.name != "posix":
        yield
        return
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"{label}锁必须是普通文件")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _hub_config_lock(hub: HubRef | None = None):
    """Serialize Hub config migrations and interactive edits."""
    config_path = _hub_config_path(hub)
    with _private_file_lock(
        config_path.with_name(config_path.name + ".lock"),
        "hub 配置",
    ):
        yield


def _hub_config_text(config: dict) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _read_hub_config_text(hub: HubRef | None = None) -> str:
    """Read the config without following a swapped symlink or special file."""
    return _read_regular_text(_hub_config_path(hub), "hub 配置")


def _validate_hub_slot_selector(
    selector: object,
    channels: dict,
    field: str,
) -> str:
    if not isinstance(selector, str):
        raise ValueError(f"{field} 必须是 <渠道,模型>")
    alias, separator, model = selector.strip().partition(",")
    alias, model = alias.strip().lower(), model.strip()
    channel = channels.get(alias)
    models = channel.get("models") if isinstance(channel, dict) else None
    if (
        not separator
        or not alias
        or not model
        or not isinstance(models, list)
        or model not in models
    ):
        raise ValueError(f"{field} 必须引用 channels 中已声明的模型")
    return f"{alias},{model}"


def normalize_hub_config(raw: object) -> dict:
    """Normalize a v1/v2 Hub document without performing I/O.

    ``default_channel`` remains the gateway's bare-model fallback route;
    ``launch_slot`` independently controls which native Claude slot the
    launcher starts. Unknown fields are preserved for forward compatibility.
    """
    if not isinstance(raw, dict):
        raise ValueError("hub 配置根必须是对象")
    config = json.loads(json.dumps(raw))
    version = config.get("version", 1)
    if type(version) is not int or version not in (1, HUB_CONFIG_VERSION):
        raise ValueError(f"不支持的 hub 配置版本: {version}")
    channels = config.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("hub 配置缺少 channels")
    for alias, channel in channels.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or alias != alias.strip().lower()
        ):
            raise ValueError(f"hub 渠道别名无效: {alias!r}")
        models = channel.get("models") if isinstance(channel, dict) else None
        if (
            not isinstance(models, list)
            or any(not isinstance(model, str) or not model.strip() for model in models)
        ):
            raise ValueError(f"channels.{alias}.models 必须是非空模型字符串列表")
        channel["models"] = [model.strip() for model in models]
    default_channel = config.get("default_channel")
    if not isinstance(default_channel, str):
        raise ValueError("hub 配置缺少 default_channel")
    default_channel = default_channel.strip().lower()
    default = channels.get(default_channel)
    default_models = default.get("models") if isinstance(default, dict) else None
    if (
        not isinstance(default_models, list)
        or not default_models
        or not isinstance(default_models[0], str)
        or not default_models[0].strip()
    ):
        raise ValueError("hub default_channel 必须引用有模型的渠道")
    config["default_channel"] = default_channel
    fallback_selector = f"{default_channel},{default_models[0].strip()}"

    raw_slots = config.get("model_slots")
    if raw_slots is None:
        raw_slots = {}
    if not isinstance(raw_slots, dict):
        raise ValueError("hub model_slots 必须是对象")
    slots: dict[str, str] = {}
    for slot in HUB_SLOT_ORDER:
        slots[slot] = _validate_hub_slot_selector(
            raw_slots.get(slot, fallback_selector),
            channels,
            f"model_slots.{slot}",
        )
    config["model_slots"] = slots

    launch_slot = config.get("launch_slot", "fable")
    if not isinstance(launch_slot, str) or launch_slot.casefold() not in HUB_SLOT_ORDER:
        raise ValueError("hub launch_slot 必须是 fable、opus、sonnet 或 haiku")
    config["launch_slot"] = launch_slot.casefold()

    raw_efforts = config.get("effort_by_slot")
    if raw_efforts is None:
        raw_efforts = {}
    if not isinstance(raw_efforts, dict):
        raise ValueError("hub effort_by_slot 必须是对象")
    efforts: dict[str, str] = {}
    for slot in HUB_SLOT_ORDER:
        effort = raw_efforts.get(slot, HUB_DEFAULT_EFFORTS[slot])
        if effort not in HUB_EFFORT_LEVELS:
            raise ValueError(
                f"effort_by_slot.{slot} 必须是 low、medium、high 或 xhigh"
            )
        efforts[slot] = effort
    config["effort_by_slot"] = efforts
    config["version"] = HUB_CONFIG_VERSION
    config.pop("effort_level", None)
    return config


def _hub_migration_backup_path(hub: HubRef | None = None) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    config_path = _hub_config_path(hub)
    base = config_path.with_name(f"{config_path.name}.bak-migrate-{timestamp}")
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    return candidate


def load_hub_config(
    *,
    migrate: bool = False,
    hub: HubRef | None = None,
) -> dict:
    """Load and validate Hub config, optionally migrating it atomically once."""
    config_path = _hub_config_path(hub)
    if not config_path.is_file():
        raise ValueError(f"hub 配置不存在: {config_path}")
    if not migrate:
        return normalize_hub_config(json.loads(_read_hub_config_text(hub)))
    with _hub_config_lock(hub):
        original_text = _read_hub_config_text(hub)
        raw = json.loads(original_text)
        normalized = normalize_hub_config(raw)
        if normalized != raw:
            _atomic_private_write(_hub_migration_backup_path(hub), original_text)
            _atomic_private_write(config_path, _hub_config_text(normalized))
        return normalized


def mutate_hub_config(mutator, *, hub: HubRef | None = None) -> dict:
    """Apply one config mutation against the latest on-disk v2 document."""
    config_path = _hub_config_path(hub)
    with _hub_config_lock(hub):
        original_text = _read_hub_config_text(hub)
        raw = json.loads(original_text)
        config = normalize_hub_config(raw)
        migration_needed = config != raw
        mutator(config)
        normalized = normalize_hub_config(config)
        if normalized != raw:
            if migration_needed:
                _atomic_private_write(_hub_migration_backup_path(hub), original_text)
            _atomic_private_write(config_path, _hub_config_text(normalized))
        return normalized


def _hub_catalog_text(catalog: dict) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


@contextmanager
def _hub_catalog_lock():
    with _private_file_lock(
        HUB_CATALOG.with_name(HUB_CATALOG.name + ".lock"),
        "hub catalog",
    ):
        yield


def _legacy_hub_catalog() -> dict:
    if HUB_CATALOG_ENABLED:
        catalog_root = HUB_CATALOG.parent.absolute()

        def relative(path: Path, label: str) -> str:
            try:
                return path.absolute().relative_to(catalog_root).as_posix()
            except ValueError:
                raise ValueError(
                    f"{label} 必须位于 hub catalog 目录内: {catalog_root}"
                ) from None

        entry = hub_catalog.legacy_hub_entry(
            config=relative(HUB_CONFIG, "旧 Hub 配置"),
            log=relative(HUB_LOG, "旧 Hub 日志"),
            usage=relative(HUB_USAGE, "旧 Hub usage"),
        )
    else:
        entry = hub_catalog.legacy_hub_entry()
    return hub_catalog.normalize_hub_catalog(
        {
            "version": hub_catalog.CATALOG_VERSION,
            "default_hub": hub_catalog.LEGACY_HUB_ID,
            "order": [hub_catalog.LEGACY_HUB_ID],
            "hubs": {
                hub_catalog.LEGACY_HUB_ID: entry,
            },
        }
    )


def load_hub_catalog(*, migrate: bool = False) -> dict:
    """Load the named-Hub catalog, optionally registering the legacy Hub."""
    if not HUB_CATALOG_ENABLED:
        return _legacy_hub_catalog()
    if HUB_CATALOG.is_file():
        return hub_catalog.load_hub_catalog(
            json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
        )
    if not migrate:
        raise ValueError(f"hub catalog 不存在: {HUB_CATALOG}")
    if not HUB_CONFIG.is_file():
        raise ValueError(f"hub 配置不存在: {HUB_CONFIG}")
    with _hub_catalog_lock():
        if HUB_CATALOG.is_file():
            return hub_catalog.load_hub_catalog(
                json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
            )
        catalog = _legacy_hub_catalog()
        _atomic_private_write(HUB_CATALOG, _hub_catalog_text(catalog))
        return catalog


def mutate_hub_catalog(mutator) -> dict:
    """Apply one catalog mutation under its private inter-process lock."""
    if not HUB_CATALOG_ENABLED:
        raise ValueError("显式单 Hub 配置模式不支持多 Hub 管理")
    with _hub_catalog_lock():
        if HUB_CATALOG.is_file():
            raw = json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
            catalog = hub_catalog.normalize_hub_catalog(raw)
        elif HUB_CONFIG.is_file():
            catalog = _legacy_hub_catalog()
        else:
            raise ValueError("没有可迁移的 Hub 配置")
        mutator(catalog)
        normalized = hub_catalog.normalize_hub_catalog(catalog)
        _atomic_private_write(HUB_CATALOG, _hub_catalog_text(normalized))
        return normalized


def _validate_catalog_path_chain(path: Path) -> None:
    """Reject symlinked/non-directory parents beneath the catalog root."""
    root = HUB_CATALOG.parent.absolute()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        root_info = None
    if root_info is not None:
        if stat.S_ISLNK(root_info.st_mode):
            raise ValueError(f"Hub catalog 根目录不能是符号链接: {root}")
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"Hub catalog 根路径必须是目录: {root}")
    target = path.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ValueError(f"Hub catalog 路径越界: {path}") from None
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Hub catalog 路径父目录不能是符号链接: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Hub catalog 路径父节点必须是目录: {current}")


def _hub_ref_from_catalog(catalog: dict, hub_id: str) -> HubRef:
    hubs = catalog["hubs"]
    if hub_id not in hubs:
        raise ValueError(f"Hub {hub_id} 已被移除")
    entry = hubs[hub_id]
    ref = HubRef(
        hub_id=hub_id,
        name=entry["name"],
        config_path=hub_catalog.resolve_catalog_path(
            HUB_CATALOG, entry["config"]
        ),
        log_path=hub_catalog.resolve_catalog_path(HUB_CATALOG, entry["log"]),
        usage_path=hub_catalog.resolve_catalog_path(
            HUB_CATALOG, entry["usage"]
        ),
        legacy=(hub_id == hub_catalog.LEGACY_HUB_ID),
        state=entry.get("state", "ready"),
        draft_path=(
            hub_catalog.resolve_catalog_path(HUB_CATALOG, entry["draft"])
            if entry.get("state") == "setup"
            else None
        ),
    )
    for path in (ref.config_path, ref.log_path, ref.usage_path, ref.draft_path):
        if path is None:
            continue
        _validate_catalog_path_chain(path)
    return ref


def list_hub_refs(*, migrate: bool = False) -> list[HubRef]:
    """Resolve all named Hubs and validate only the runnable instances."""
    if not HUB_CATALOG_ENABLED:
        return [_legacy_hub_ref()] if HUB_CONFIG.is_file() else []
    try:
        catalog = load_hub_catalog(migrate=migrate)
    except ValueError:
        if not HUB_CATALOG.is_file() and not HUB_CONFIG.is_file():
            return []
        raise
    refs = [
        _hub_ref_from_catalog(catalog, hub_id)
        for hub_id in catalog["order"]
    ]
    ready_refs = [ref for ref in refs if ref.state == "ready"]
    configs = {
        ref.hub_id: load_hub_config(migrate=migrate, hub=ref)
        for ref in ready_refs
    }
    hub_catalog.validate_unique_hub_ports(configs)
    for ref in ready_refs:
        instance_id = configs[ref.hub_id].get("instance_id")
        if not ref.legacy and instance_id != ref.hub_id:
            raise ValueError(
                f"Hub {ref.name} 的 instance_id 必须等于 {ref.hub_id}"
            )
    return refs


def resolve_hub_ref(
    value: str | None = None,
    *,
    migrate: bool = True,
) -> HubRef:
    refs = list_hub_refs(migrate=migrate)
    if not refs:
        raise ValueError("没有可用的 Hub")
    if not HUB_CATALOG_ENABLED:
        if value not in (None, refs[0].hub_id, refs[0].name):
            raise ValueError(f"Hub 不存在: {value}")
        return refs[0]
    catalog = load_hub_catalog(migrate=migrate)
    hub_id = hub_catalog.resolve_hub_id(catalog, value)
    return _hub_ref_from_catalog(catalog, hub_id)


def _next_hub_port(configs: dict[str, dict], preferred: int) -> int:
    with _reserve_next_hub_port(configs, preferred) as listener:
        return int(listener.getsockname()[1])


def _reserve_next_hub_port(
    configs: dict[str, dict],
    preferred: int,
) -> socket.socket:
    """Reserve the next port not claimed by a config or listening process."""
    used = set(hub_catalog.validate_unique_hub_ports(configs).values())
    candidate = preferred if 1 <= preferred <= 65535 else 18787
    for _ in range(65535):
        if candidate not in used:
            try:
                return _reserve_loopback_port(candidate)
            except OSError as exc:
                if exc.errno not in {errno.EADDRINUSE, errno.EACCES}:
                    raise
        candidate = 1024 if candidate >= 65535 else candidate + 1
    raise ValueError("没有可分配的 Hub 端口")


def _reserve_reassigned_hub_port(
    hub: HubRef,
    expected_port: int,
) -> tuple[int, socket.socket]:
    """Persist a replacement named-Hub port while retaining its listener."""
    with _hub_catalog_lock():
        catalog = hub_catalog.load_hub_catalog(
            json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
        )
        current = _hub_ref_from_catalog(catalog, hub.hub_id)
        configs = {
            existing_id: load_hub_config(
                hub=_hub_ref_from_catalog(catalog, existing_id)
            )
            for existing_id in catalog["order"]
            if existing_id != hub.hub_id
            and catalog["hubs"][existing_id].get("state", "ready") == "ready"
        }
        listener = _reserve_next_hub_port(configs, 18787)
        replacement = int(listener.getsockname()[1])

        def reassign(config: dict) -> None:
            if _hub_port(config) != expected_port:
                raise RuntimeError(f"Hub {hub.name} 的端口配置已变化，请重试")
            config["port"] = replacement

        try:
            mutate_hub_config(reassign, hub=current)
        except BaseException:
            listener.close()
            raise
    return replacement, listener


HUB_SETUP_DRAFT_VERSION = 1


def normalize_hub_setup_draft(raw: object) -> dict:
    """Validate the launcher-only mapping draft for one unconfigured Hub."""
    if not isinstance(raw, dict) or raw.get("version") != HUB_SETUP_DRAFT_VERSION:
        raise ValueError("不支持的 Hub 首次设置草稿")
    raw_mappings = raw.get("mappings")
    if not isinstance(raw_mappings, dict):
        raise ValueError("Hub 首次设置草稿缺少 mappings")
    mappings: dict[str, dict] = {}
    for slot in HUB_SLOT_ORDER:
        if slot not in raw_mappings:
            continue
        mapping = raw_mappings[slot]
        if not isinstance(mapping, dict):
            raise ValueError(f"Hub {slot} 映射无效")
        provider_id = mapping.get("provider_id")
        alias = mapping.get("alias")
        model = mapping.get("model")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError(f"Hub {slot} 映射缺少 provider_id")
        if not isinstance(alias, str) or re.fullmatch(
            r"[a-z][a-z0-9_-]*", alias
        ) is None:
            raise ValueError(f"Hub {slot} 映射 alias 无效")
        if (
            not isinstance(model, str)
            or not model.strip()
            or "," in model
        ):
            raise ValueError(f"Hub {slot} 映射 model 无效")
        normalized = {
            "provider_id": provider_id.strip(),
            "provider_name": str(mapping.get("provider_name") or provider_id).strip(),
            "alias": alias,
            "model": model.strip(),
        }
        api_format = mapping.get("api_format")
        if api_format is not None:
            if api_format not in {"anthropic", "openai_chat", "openai_responses"}:
                raise ValueError(f"Hub {slot} 映射 api_format 无效")
            normalized["api_format"] = api_format
        mappings[slot] = normalized
    extra_slots = set(raw_mappings) - set(HUB_SLOT_ORDER)
    if extra_slots:
        raise ValueError("Hub 首次设置草稿包含未知槽位")
    return {"version": HUB_SETUP_DRAFT_VERSION, "mappings": mappings}


def _hub_setup_draft_text(draft: dict) -> str:
    return json.dumps(draft, ensure_ascii=False, indent=2) + "\n"


def load_hub_setup_draft(hub: HubRef) -> dict:
    if hub.state != "setup" or hub.draft_path is None:
        raise ValueError(f"Hub {hub.name} 不是待配置状态")
    _validate_catalog_path_chain(hub.draft_path)
    return normalize_hub_setup_draft(
        json.loads(_read_regular_text(hub.draft_path, "Hub 首次设置草稿"))
    )


@contextmanager
def _hub_setup_draft_lock(hub: HubRef):
    if hub.draft_path is None:
        raise ValueError(f"Hub {hub.name} 缺少首次设置草稿路径")
    with _private_file_lock(
        hub.draft_path.with_name(hub.draft_path.name + ".lock"),
        "Hub 首次设置草稿",
    ):
        yield


def create_named_hub(name: str) -> HubRef:
    """Create a named setup draft without inheriting models or credentials."""
    if not HUB_CATALOG_ENABLED:
        raise ValueError("显式单 Hub 配置模式不支持新增 Hub")
    display_name = hub_catalog.validate_display_name(name)
    with _hub_catalog_lock():
        if HUB_CATALOG.is_file():
            catalog = hub_catalog.load_hub_catalog(
                json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
            )
        elif HUB_CONFIG.is_file():
            catalog = _legacy_hub_catalog()
        else:
            catalog = {
                "version": hub_catalog.CATALOG_VERSION,
                "default_hub": "",
                "order": [],
                "hubs": {},
            }
        hub_id = hub_catalog.unique_hub_id(display_name, catalog["hubs"])
        entry = {
            "name": display_name,
            "state": "setup",
            "config": f"hubs/{hub_id}.json",
            "draft": f"hubs/{hub_id}.setup.json",
            "log": f"logs/hubs/{hub_id}.log",
            "usage": f"logs/hubs/{hub_id}-usage.jsonl",
        }
        preview_catalog = json.loads(json.dumps(catalog))
        preview_catalog["hubs"][hub_id] = entry
        preview_catalog["order"].append(hub_id)
        if not preview_catalog["default_hub"]:
            preview_catalog["default_hub"] = hub_id
        normalized = hub_catalog.normalize_hub_catalog(preview_catalog)
        new_ref = _hub_ref_from_catalog(normalized, hub_id)
        assert new_ref.draft_path is not None
        if new_ref.config_path.exists():
            raise ValueError(f"Hub 路径已存在: {new_ref.config_path}")
        empty_draft = {"version": HUB_SETUP_DRAFT_VERSION, "mappings": {}}
        created_draft = not new_ref.draft_path.exists()
        if created_draft:
            _validate_catalog_path_chain(new_ref.draft_path)
            _atomic_private_write(
                new_ref.draft_path,
                _hub_setup_draft_text(empty_draft),
            )
        elif load_hub_setup_draft(new_ref) != empty_draft:
            raise ValueError(f"Hub 草稿路径已存在: {new_ref.draft_path}")
        try:
            _atomic_private_write(HUB_CATALOG, _hub_catalog_text(normalized))
        except BaseException:
            if created_draft:
                try:
                    new_ref.draft_path.unlink()
                except OSError:
                    pass
            raise
        return new_ref


def rename_named_hub(hub_id: str, name: str) -> HubRef:
    display_name = hub_catalog.validate_display_name(name)

    def rename(catalog: dict) -> None:
        if hub_id not in catalog["hubs"]:
            raise ValueError(f"Hub 不存在: {hub_id}")
        catalog["hubs"][hub_id]["name"] = display_name

    catalog = mutate_hub_catalog(rename)
    return _hub_ref_from_catalog(catalog, hub_id)


def cycle_hub_slot_effort(
    slot: str,
    expected_effort: str,
    direction: int,
    *,
    hub: HubRef | None = None,
) -> dict:
    """Cycle one slot with compare-and-swap protection across TUI surfaces."""
    if slot not in HUB_SLOT_ORDER or expected_effort not in HUB_EFFORT_LEVELS:
        raise ValueError("hub 槽位 effort 无效")
    if direction not in (-1, 1):
        raise ValueError("effort direction 必须是 -1 或 1")
    current_index = HUB_EFFORT_LEVELS.index(expected_effort)
    next_effort = HUB_EFFORT_LEVELS[
        (current_index + direction) % len(HUB_EFFORT_LEVELS)
    ]

    def set_effort(latest: dict) -> None:
        if latest["effort_by_slot"][slot] != expected_effort:
            raise ValueError("槽位 effort 已被另一窗口修改，请重试")
        latest["effort_by_slot"][slot] = next_effort

    if hub is None:
        return mutate_hub_config(set_effort)
    return mutate_hub_config(set_effort, hub=hub)


def add_hub_channel(
    provider: dict,
    *,
    alias: str,
    models: list[str],
    api_format: str | None = None,
    hub: HubRef | None = None,
) -> dict:
    """Add one credential-free, multi-model channel without changing slots."""
    alias = alias.strip().casefold()
    provider_id = str(provider.get("id") or "").strip()
    if re.fullmatch(r"[a-z][a-z0-9_-]*", alias) is None:
        raise ValueError("渠道 alias 必须匹配 [a-z][a-z0-9_-]*")
    if not provider_id:
        raise ValueError("provider 缺少稳定 id")
    if not isinstance(models, list) or not models:
        raise ValueError("models 必须是非空模型列表")
    normalized_models: list[str] = []
    for raw_model in models:
        model = raw_model.strip() if isinstance(raw_model, str) else ""
        if not model or "," in model:
            raise ValueError("model 必须是非空且不能包含逗号")
        if model not in normalized_models:
            normalized_models.append(model)
    if api_format is not None and api_format not in {
        "anthropic",
        "openai_chat",
        "openai_responses",
    }:
        raise ValueError("api_format 无效")

    def add(latest: dict) -> None:
        if alias in latest["channels"]:
            raise ValueError(f"hub 渠道已存在: {alias}")
        latest["channels"][alias] = {
            "provider": f"id:{provider_id}",
            "models": normalized_models,
        }
        if api_format is not None:
            latest["channels"][alias]["api_format"] = api_format

    if hub is None:
        return mutate_hub_config(add)
    return mutate_hub_config(add, hub=hub)


def remove_hub_channel(alias: str, *, hub: HubRef | None = None) -> dict:
    """Remove an unreferenced channel while preserving routing invariants."""
    alias = alias.strip().lower()

    def remove(latest: dict) -> None:
        if alias not in latest["channels"]:
            raise ValueError(f"hub 渠道不存在: {alias}")
        if len(latest["channels"]) <= 1:
            raise ValueError("不能删除最后一个 hub 渠道")
        if alias == latest["default_channel"]:
            raise ValueError("不能删除 gateway fallback 渠道")
        referenced = [
            slot
            for slot, selector in latest["model_slots"].items()
            if selector.partition(",")[0] == alias
        ]
        if referenced:
            raise ValueError(f"渠道仍被槽位引用: {', '.join(referenced)}")
        del latest["channels"][alias]

    if hub is None:
        return mutate_hub_config(remove)
    return mutate_hub_config(remove, hub=hub)


def _hub_model_family(model: str) -> str:
    """Derive the display family (Claude / GPT / GLM / …) from a model name."""
    lowered = model.casefold()
    for needle, family in _HUB_FAMILY_RULES:
        if needle in lowered:
            return family
    return "其他"


@dataclass(frozen=True)
class HubModelOption:
    """One selectable (channel, model) row inside the Hub workspace."""

    family: str
    channel: str
    model: str
    is_default: bool
    via_proxy: bool
    is_1m: bool

    @property
    def selector(self) -> str:
        """The `渠道,模型` string consumed by `exec_hub --model`."""
        return f"{self.channel},{self.model}"

    @property
    def status_label(self) -> str:
        if self.is_default:
            return "回退"
        if self.is_1m:
            return "1M"
        if self.via_proxy:
            return "代理"
        return "可用"


@dataclass(frozen=True)
class HubChannel:
    """One configured hub channel (alias → provider + models)."""

    alias: str
    provider: str
    models: tuple[str, ...]
    via_proxy: bool


@dataclass(frozen=True)
class HubStatus:
    """A read-only snapshot of the hub for the launcher UI."""

    port: int
    default_channel: str
    default_model: str
    channel_count: int
    model_count: int
    healthy: bool | None = None
    launch_slot: str = "fable"
    launch_selector: str = ""
    launch_effort: str = "high"
    slot_summary: str = ""

    @property
    def summary(self) -> str:
        """Main-screen one-liner for the slot-aware Hub entry."""
        return (
            f"{len(HUB_SLOT_ORDER)} 槽 · {self.channel_count} 渠道"
            f" · 启动 {self.launch_selector} · {self.launch_effort}"
        )


@dataclass(frozen=True)
class HubLaunch:
    """Launcher result signalling the user picked a hub model to start."""

    option: HubModelOption | None = None
    slot: str | None = None
    hub_id: str | None = None


@dataclass(frozen=True)
class HubSlotOption:
    """One native Claude model slot with its persisted startup effort."""

    slot: str
    selector: str
    effort: str
    option: HubModelOption


@dataclass(frozen=True)
class HubLauncherState:
    """One coherent config snapshot shared by the home and workspace views."""

    status: HubStatus
    options: tuple[HubModelOption, ...]
    slots: tuple[HubSlotOption, ...]


@dataclass(frozen=True)
class NamedHubLauncherState:
    """One named Hub, including setup entries that have no runnable state."""

    hub: HubRef
    state: HubLauncherState | None
    setup_count: int = 0
    error: str | None = None


def build_hub_workspace(
    hub_cfg: dict,
) -> tuple[list[HubSlotOption], list[HubModelOption]]:
    """Build the four native slots and the unbound channel-model pool."""
    config = normalize_hub_config(hub_cfg)
    _status, options = build_hub_view(config)
    by_selector = {option.selector: option for option in options}
    slots: list[HubSlotOption] = []
    bound: set[str] = set()
    for slot in HUB_SLOT_ORDER:
        selector = config["model_slots"][slot]
        option = by_selector.get(selector)
        if option is None:  # normalize_hub_config should make this unreachable.
            raise ValueError(f"model_slots.{slot} 未出现在 channels")
        slots.append(
            HubSlotOption(
                slot=slot,
                selector=selector,
                effort=config["effort_by_slot"][slot],
                option=option,
            )
        )
        bound.add(selector)
    return slots, [option for option in options if option.selector not in bound]


def build_hub_channels(hub_cfg: dict) -> list[HubChannel]:
    """Parse the raw hub config into channels, skipping malformed entries."""
    channels_raw = hub_cfg.get("channels")
    if not isinstance(channels_raw, dict) or not channels_raw:
        raise ValueError("hub 配置缺少 channels")
    channels: list[HubChannel] = []
    for alias, channel_raw in channels_raw.items():
        if not isinstance(alias, str) or not alias.strip():
            continue
        if not isinstance(channel_raw, dict):
            continue
        models = tuple(
            model.strip()
            for model in channel_raw.get("models", [])
            if isinstance(model, str) and model.strip()
        )
        if not models:
            continue
        proxy = channel_raw.get("proxy")
        channels.append(
            HubChannel(
                alias=alias,
                provider=str(channel_raw.get("provider", "")),
                models=models,
                via_proxy=bool(isinstance(proxy, str) and proxy.strip()),
            )
        )
    if not channels:
        raise ValueError("hub 配置没有可用的渠道模型")
    return channels


def build_hub_view(hub_cfg: dict) -> tuple[HubStatus, list[HubModelOption]]:
    """Turn a raw hub config dict into a status snapshot + ordered options.

    The default model is listed first; remaining models follow in channel then
    model order. Families/statuses are derived, never read from config.
    """
    hub_cfg = normalize_hub_config(hub_cfg)
    channels = build_hub_channels(hub_cfg)
    by_alias = {channel.alias: channel for channel in channels}
    requested_default = hub_cfg.get("default_channel")
    default_alias = (
        requested_default if requested_default in by_alias else channels[0].alias
    )
    default_model = by_alias[default_alias].models[0]

    def make_option(channel: HubChannel, model: str) -> HubModelOption:
        return HubModelOption(
            family=_hub_model_family(model),
            channel=channel.alias,
            model=model,
            is_default=(channel.alias == default_alias and model == default_model),
            via_proxy=channel.via_proxy,
            is_1m="[1m]" in model.casefold(),
        )

    ordered: list[HubModelOption] = [
        make_option(by_alias[default_alias], default_model)
    ]
    for channel in channels:
        for model in channel.models:
            if channel.alias == default_alias and model == default_model:
                continue
            ordered.append(make_option(channel, model))

    launch_slot = hub_cfg["launch_slot"]
    launch_selector = hub_cfg["model_slots"][launch_slot]
    labels = {"fable": "F", "opus": "O", "sonnet": "S", "haiku": "H"}
    slot_summary = " · ".join(
        f"{labels[slot]} {hub_cfg['model_slots'][slot].partition(',')[2]}"
        for slot in HUB_SLOT_ORDER
    )
    status = HubStatus(
        port=_hub_port(hub_cfg),
        default_channel=default_alias,
        default_model=default_model,
        channel_count=len(channels),
        model_count=sum(len(channel.models) for channel in channels),
        launch_slot=launch_slot,
        launch_selector=launch_selector,
        launch_effort=hub_cfg["effort_by_slot"][launch_slot],
        slot_summary=slot_summary,
    )
    return status, ordered


def build_hub_launcher_state(hub_cfg: dict) -> HubLauncherState:
    """Build all launcher-facing Hub rows from one normalized config snapshot."""
    config = normalize_hub_config(hub_cfg)
    status, options = build_hub_view(config)
    slots, _pool = build_hub_workspace(config)
    return HubLauncherState(status, tuple(options), tuple(slots))


def _load_hub_launcher_state() -> HubLauncherState | None:
    try:
        hub = resolve_hub_ref(migrate=True)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    try:
        return build_hub_launcher_state(load_hub_config(migrate=True, hub=hub))
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError):
        return None


def _load_named_hub_launcher_states() -> list[NamedHubLauncherState]:
    """Load all valid named Hubs while keeping ordinary providers available."""
    try:
        refs = list_hub_refs(migrate=True)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    states: list[NamedHubLauncherState] = []
    for hub in refs:
        if hub.state == "setup":
            try:
                draft = load_hub_setup_draft(hub)
            except (OSError, ValueError, json.JSONDecodeError, RuntimeError):
                states.append(
                    NamedHubLauncherState(hub, None, error="配置异常")
                )
            else:
                states.append(
                    NamedHubLauncherState(hub, None, len(draft["mappings"]))
                )
            continue
        try:
            config = load_hub_config(migrate=True, hub=hub)
            states.append(
                NamedHubLauncherState(hub, build_hub_launcher_state(config))
            )
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError):
            continue
    return states


def _load_hub_view() -> tuple[HubStatus, list[HubModelOption]] | None:
    """Read HUB_CONFIG for the launcher; return None when hub is unavailable.

    A missing/invalid config (or missing uv) must never break plain provider
    selection, so any failure degrades silently to “no hub entry”.
    """
    state = _load_hub_launcher_state()
    if state is None:
        return None
    return state.status, list(state.options)


# ---------------------------------------------------------------------------
# `claude1 config` — interactive provider on/off editor (TUI + text fallback)
# ---------------------------------------------------------------------------
try:
    import curses
except ImportError:  # pragma: no cover - curses ships with CPython on macOS/Linux
    curses = None

LOGO = [
    "  ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗ ██╗",
    " ██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝███║",
    " ██║     ██║     ███████║██║   ██║██║  ██║█████╗  ╚██║",
    " ██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝   ██║",
    " ╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗ ██║",
    "  ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝ ╚═╝",
]
_LOGO_CELLS = [
    (row, column, char)
    for row, line in enumerate(LOGO)
    for column, char in enumerate(line)
    if char != " "
]

_HEADER_H = len(LOGO) + 6
_LOGO_TOP = 2
_LOGO_MIN_LIST = 5
_MIN_TUI_ROWS = 8
_MIN_TUI_COLS = 32
INTRO_DURATION_SECONDS = 0.24
INTRO_FRAME_SECONDS = 0.016
INTRO_FLOW_STEP_SECONDS = 0.04
LOGO_BREATH_LEVELS = (
    0, 0, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 0, 0,
)
C: dict = {}

# Logo and provider rows share a vivid full-spectrum identity.
LOGO_GRAD = [
    196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 48,
    50, 51, 45, 39, 33, 63, 99, 135, 171, 207, 201, 199,
]
_logo_pairs: list[int] = []
RAINBOW = [203, 208, 214, 220, 148, 46, 42, 51, 45, 75, 99, 141, 207, 205]
_row_pairs: list[int] = []


def _addstr(win, y, x, text, attr=0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        text = _drop_display_prefix(text, -x)
        x = 0
    text = _truncate_display(text, max(0, w - x - 1))
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Cc"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _dwidth(text: str) -> int:
    """终端显示宽度：CJK 全角字符按 2 列计。"""
    return sum(_char_width(char) for char in text)


def _truncate_display(text: str, max_width: int) -> str:
    """Clip text without placing half of a wide character outside the window."""
    if max_width <= 0:
        return ""
    used = 0
    result: list[str] = []
    for char in text:
        char_width = _char_width(char)
        if used + char_width > max_width:
            break
        result.append(char)
        used += char_width
    return "".join(result)


def _drop_display_prefix(text: str, width: int) -> str:
    if width <= 0:
        return text
    used = 0
    for index, char in enumerate(text):
        used += _char_width(char)
        if used >= width:
            return text[index + 1 :]
    return ""


def _pad_display(text: str, width: int) -> str:
    clipped = _truncate_display(text, width)
    return clipped + (" " * max(0, width - _dwidth(clipped)))


def _compose_row(left: str, right: str, width: int) -> str:
    """Fit one provider row, keeping its short status aligned when possible."""
    if width <= 0:
        return ""
    if not right:
        return _truncate_display(left, width)
    right = _truncate_display(right, width)
    right_width = _dwidth(right)
    if right_width + 2 >= width:
        return _truncate_display(left, width)
    left = _truncate_display(left, width - right_width - 2)
    gap = max(2, width - _dwidth(left) - right_width)
    return _truncate_display(left + (" " * gap) + right, width)


def _safe_curs_set(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except (AttributeError, curses.error):
        pass


def _init_colors() -> dict:
    d = {
        "dim": 0,
        "base": 0,
        "accent": 0,
        "warning": 0,
        "brand": 0,
        "sel": curses.A_REVERSE,
        "orange": 0,
        "pink": 0,
        "lime": 0,
        "gold": 0,
        "teal": 0,
        "violet": 0,
    }
    _logo_pairs.clear()
    _row_pairs.clear()
    try:
        has_colors = curses.has_colors()
    except curses.error:
        has_colors = False
    if not has_colors:
        _logo_pairs.append(0)
        _row_pairs.extend([d["base"], d["accent"], d["brand"], d["warning"]])
        return d
    try:
        curses.start_color()
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = 0

    def _pair(pid: int, fg: int, fallback: int = 0) -> int:
        max_pairs = int(getattr(curses, "COLOR_PAIRS", 0) or 0)
        if max_pairs and pid >= max_pairs:
            return fallback
        try:
            curses.init_pair(pid, fg, bg)
            return curses.color_pair(pid)
        except curses.error:
            return fallback

    cyan = _pair(1, curses.COLOR_CYAN)
    green = _pair(2, curses.COLOR_GREEN)
    yellow = _pair(3, curses.COLOR_YELLOW)
    magenta = _pair(4, curses.COLOR_MAGENTA)
    d.update(
        dim=curses.A_DIM,
        base=green,
        accent=cyan | curses.A_BOLD,
        warning=yellow,
        brand=magenta | curses.A_BOLD,
        sel=cyan | curses.A_REVERSE | curses.A_BOLD,
    )

    has256 = getattr(curses, "COLORS", 0) >= 256

    # Named vivid accents with safe basic-color fallbacks.
    d["orange"] = _pair(60, 208, d["warning"]) if has256 else d["warning"]
    d["pink"] = _pair(61, 205, d["brand"]) if has256 else d["brand"]
    d["lime"] = _pair(62, 118, d["base"]) if has256 else d["base"]
    d["gold"] = _pair(63, 220, d["warning"]) if has256 else d["warning"]
    d["teal"] = _pair(64, 44, d["accent"]) if has256 else d["accent"]
    d["violet"] = _pair(65, 141, d["brand"]) if has256 else d["brand"]

    # Selected row: bold black text on a vivid orange background.
    if has256:
        try:
            curses.init_pair(66, 16, 208)
            d["sel"] = curses.color_pair(66) | curses.A_BOLD
        except curses.error:
            pass

    # Rotate provider rows through a full spectrum; basic terminals keep
    # a compact four-color fallback.
    if has256:
        for i, cidx in enumerate(RAINBOW):
            pair = _pair(40 + i, cidx, 0)
            if pair:
                _row_pairs.append(pair)
    if not _row_pairs:
        _row_pairs.extend([d["base"], d["accent"], d["brand"], d["warning"]])

    if has256:
        for i, cidx in enumerate(LOGO_GRAD):
            _logo_pairs.append(_pair(10 + i, cidx, d["brand"]))
    if not _logo_pairs:
        _logo_pairs.extend([cyan, magenta])
    return d


def _large_logo_supported(rows: int, cols: int) -> bool:
    logo_width = max((_dwidth(line) for line in LOGO), default=0)
    return cols >= logo_width + 4 and rows > _HEADER_H + _LOGO_MIN_LIST


def _tui_size_supported(rows: int, cols: int) -> bool:
    return rows >= _MIN_TUI_ROWS and cols >= _MIN_TUI_COLS


def _animation_enabled() -> bool:
    disabled = os.environ.get("CLAUDE1_NO_ANIMATION", "").strip().casefold()
    return disabled not in {"1", "true", "yes", "on"}


def _logo_intensity(phase: int, breathing: bool) -> int:
    if not breathing:
        return curses.A_BOLD
    level = LOGO_BREATH_LEVELS[phase % len(LOGO_BREATH_LEVELS)]
    if level > 0:
        return curses.A_BOLD
    return 0


def _draw_logo(
    win,
    phase: int,
    *,
    breathing: bool = False,
    force_compact: bool = False,
) -> None:
    """Flow the logo palette and optionally pulse its brightness."""
    n = len(_logo_pairs) or 1
    h, w = win.getmaxyx()
    intensity = _logo_intensity(phase, breathing)
    if not force_compact and _large_logo_supported(h, w):
        limit = w - 1
        for row, column, char in _LOGO_CELLS:
            x = 2 + column
            if x >= limit:
                continue
            attr = _logo_pairs[(column + row + phase) % n] | intensity
            try:
                win.addstr(_LOGO_TOP + row, x, char, attr)
            except curses.error:
                pass
    else:
        _addstr(win, 0, 2, "◤ claude1 ◢", _logo_pairs[phase % n] | intensity)


def _intro(win) -> int | None:
    """Animate for at most 240ms and return, rather than consume, any key."""
    rows, cols = win.getmaxyx()
    if not _animation_enabled() or not _large_logo_supported(rows, cols):
        return None
    win.erase()
    win.nodelay(True)
    n = len(_logo_pairs) or 1
    width = max((len(line) for line in LOGO), default=0)
    started = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= INTRO_DURATION_SECONDS:
                return None
            progress = min(1.0, elapsed / INTRO_DURATION_SECONDS)
            phase = int(elapsed / INTRO_FLOW_STEP_SECONDS)
            typed = min(
                len("欢迎回来"),
                max(1, int((elapsed / 0.12) * len("欢迎回来"))),
            )
            _addstr(
                win,
                0,
                2,
                _pad_display("欢迎回来"[:typed], _dwidth("欢迎回来")),
                C.get("pink", 0) | curses.A_BOLD,
            )
            col = max(1, int(width * progress))
            for r, line in enumerate(LOGO):
                for x in range(min(col, len(line))):
                    chx = line[x]
                    if chx == " ":
                        continue
                    attr = (
                        _logo_pairs[(x + r + phase) % n]
                        | _logo_intensity(phase, True)
                    )
                    if x >= col - 2:
                        attr |= curses.A_REVERSE | curses.A_BOLD
                    _addstr(win, _LOGO_TOP + r, 2 + x, chx, attr)
            win.refresh()
            key = win.getch()
            if key != -1:
                return key
            remaining = INTRO_DURATION_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(INTRO_FRAME_SECONDS, remaining))
    finally:
        win.nodelay(False)


def _build_view(cfg, db_ids, mru, show_hidden):
    """Keep config order stable; MRU only affects the initial cursor."""
    meta = cfg["providers"]
    return [
        provider_id
        for provider_id in meta
        if provider_id in db_ids
        and (show_hidden or not meta[provider_id].get("hidden"))
    ]


def _recent_name(view: list[str], mru: dict[str, float]) -> str | None:
    candidates = [
        name
        for name in view
        if isinstance(mru.get(name), (int, float))
        and not isinstance(mru.get(name), bool)
    ]
    return max(candidates, key=lambda name: mru[name], default=None)


def _initial_index(
    view: list[str],
    mru: dict[str, float],
    preferred: str | None = None,
) -> int:
    if preferred in view:
        return view.index(preferred)
    recent = _recent_name(view, mru)
    return view.index(recent) if recent in view else 0


def _visible_window(total: int, selected: int, capacity: int) -> tuple[int, int]:
    if total <= 0 or capacity <= 0:
        return (0, 0)
    capacity = min(total, capacity)
    start = max(0, min(selected - (capacity // 2), total - capacity))
    return (start, start + capacity)


def _digit_index(key: int) -> int | None:
    if ord("1") <= key <= ord("9"):
        return key - ord("1")
    if key == ord("0"):
        return 9
    return None


def _alias_conflict(
    meta: dict,
    current_id: str,
    candidate: str,
) -> str | None:
    folded = candidate.strip().casefold()
    if not folded:
        return None
    for provider_id, provider_meta in meta.items():
        if provider_id == current_id:
            continue
        if not isinstance(provider_meta, dict):
            continue
        name = str(provider_meta.get("name", provider_id))
        terms = [name]
        alias = provider_meta.get("alias") if isinstance(provider_meta, dict) else None
        if isinstance(alias, str) and alias.strip():
            terms.append(alias.strip())
        if any(term.casefold() == folded for term in terms):
            return name
    return None


def _set_alias(meta: dict, provider_id: str, candidate: str) -> tuple[bool, str]:
    candidate = candidate.strip()
    if not candidate:
        changed = bool(meta[provider_id].pop("alias", None))
        return (changed, "别名已清除" if changed else "未设置别名")
    if candidate.startswith("-"):
        return (False, "别名不能以 “-” 开头，否则会与命令参数冲突")
    if candidate.casefold() in RESERVED_SELECTOR_WORDS:
        return (False, f"“{candidate}”是 claude1 保留命令，请换一个别名")
    conflict = _alias_conflict(meta, provider_id, candidate)
    if conflict:
        return (False, f"别名“{candidate}”已被 {conflict} 使用")
    if meta[provider_id].get("alias") == candidate:
        return (False, f"别名仍为 {candidate}")
    meta[provider_id]["alias"] = candidate
    return (True, f"别名已设为 {candidate}")


def _edit_alias(win, provider_id, meta) -> tuple[bool, str]:
    h, w = win.getmaxyx()
    prompt = "别名（留空清除）: "
    _safe_curs_set(1)
    try:
        curses.echo()
    except curses.error:
        pass
    _addstr(win, h - 1, 0, " " * max(0, w - 1))
    _addstr(win, h - 1, 2, prompt, C.get("accent", 0))
    win.refresh()
    input_x = min(max(2, 2 + _dwidth(prompt)), max(2, w - 2))
    input_limit = max(1, min(40, w - input_x - 1))
    try:
        raw = win.getstr(h - 1, input_x, input_limit)
        s = raw.decode("utf-8", "ignore").strip()
    except Exception:
        return (False, "别名未修改")
    finally:
        try:
            curses.noecho()
        except curses.error:
            pass
        _safe_curs_set(0)
    return _set_alias(meta, provider_id, s)


def _confirm(win, msg) -> bool:
    """底部 y/n 选择条：←→ 或 y/n 切换，回车确认，Esc 取消。默认 n。"""
    h, _ = win.getmaxyx()
    choice = False
    while True:
        _addstr(win, h - 1, 0, " " * (win.getmaxyx()[1] - 1))
        _addstr(win, h - 1, 2, msg + "  ", C.get("warning", 0) | curses.A_BOLD)
        base = 2 + _dwidth(msg) + 2
        _addstr(win, h - 1, base, " y ", C.get("sel", curses.A_REVERSE) if choice else C.get("dim", 0))
        _addstr(win, h - 1, base + 4, " n ", C.get("sel", curses.A_REVERSE) if not choice else C.get("dim", 0))
        win.refresh()
        ch = win.getch()
        if ch == -1:
            return False
        if ch in (ord("y"), ord("Y")):
            choice = True
        elif ch in (ord("n"), ord("N")):
            choice = False
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            choice = not choice
        elif ch in (10, 13, curses.KEY_ENTER):
            return choice
        elif ch == 27:
            return False


def _short_provider_id(provider_id: object) -> str:
    raw = str(provider_id)
    return raw if len(raw) <= 12 else raw[:8]


def _provider_meta_label(meta: dict, provider_id: str) -> str:
    provider_meta = meta.get(provider_id)
    if not isinstance(provider_meta, dict):
        return provider_id
    name = str(provider_meta.get("name") or provider_id)
    duplicates = sum(
        1
        for other in meta.values()
        if isinstance(other, dict) and str(other.get("name") or "") == name
    )
    if duplicates > 1:
        return f"{name} [{_short_provider_id(provider_id)}]"
    return name


def _home_hubs_expanded(
    rows: int,
    cols: int,
    hubs: tuple[NamedHubLauncherState, ...] | list[NamedHubLauncherState],
) -> bool:
    """Use section headings when a Hub row and direct-provider row both fit."""
    if not hubs:
        return False
    large_logo = _large_logo_supported(rows, cols) and rows >= 21
    head = _LOGO_TOP + len(LOGO) if large_logo else 1
    list_top = head + 4 + 3  # Hub heading + one Hub + provider heading
    return max(0, rows - 1 - list_top) >= 1


def _hub_home_text(named: NamedHubLauncherState, width: int) -> str:
    left = f"◆ {named.hub.name}"
    if named.error is not None:
        right = named.error
    elif named.state is None:
        right = f"待配置 · {named.setup_count}/{len(HUB_SLOT_ORDER)} 映射"
    else:
        status = named.state.status
        right = (
            f"{len(HUB_SLOT_ORDER)} 槽 · {status.channel_count} 渠道"
            f" · 启动 {status.launch_slot.title()} · {status.launch_effort}"
        )
    return _compose_row(left, right, width)


_HUB_IDENTITY_COLORS = ("orange", "teal", "violet", "pink", "lime")


def _hub_identity_color(index: int) -> str:
    return _HUB_IDENTITY_COLORS[index % len(_HUB_IDENTITY_COLORS)]


def _draw_launcher(
    win,
    cfg,
    view,
    idx,
    show_hidden,
    mru,
    *,
    show_brand: bool = True,
    help_open: bool = False,
    notice: str | None = None,
    logo_phase: int = 0,
    logo_breathing: bool = False,
    hub_focus: bool = False,
    hubs: tuple[NamedHubLauncherState, ...] | list[NamedHubLauncherState] = (),
    hub_idx: int = 0,
) -> None:
    meta = cfg["providers"]
    win.erase()
    h, w = win.getmaxyx()
    expanded_hub = _home_hubs_expanded(h, w, hubs)
    big = _large_logo_supported(h, w) and (not expanded_hub or h >= 21)

    if show_brand and big:
        _addstr(win, 0, 2, "欢迎回来", C.get("pink", 0) | curses.A_BOLD)
        _draw_logo(win, logo_phase, breathing=logo_breathing)
        head = _LOGO_TOP + len(LOGO)
    elif show_brand:
        _draw_logo(
            win,
            logo_phase,
            breathing=logo_breathing,
            force_compact=True,
        )
        head = 1
    else:
        _addstr(
            win,
            0,
            2,
            "欢迎使用 claude1",
            C.get("pink", 0) | curses.A_BOLD,
        )
        head = 1

    heading = "选择本次渠道"
    _addstr(win, head + 1, 2, heading, C.get("lime", 0) | curses.A_BOLD)
    if show_hidden:
        _addstr(
            win,
            head + 1,
            3 + _dwidth(heading),
            "· 含隐藏项",
            C.get("dim", 0),
        )
    guide = notice or "↑↓ / jk 移动 · Enter 启动 · 数字直达"
    selected_hub = (
        hubs[max(0, min(hub_idx, len(hubs) - 1))]
        if hub_focus and hubs
        else None
    )
    if hubs and not notice:
        if selected_hub is not None and selected_hub.state is None:
            guide = (
                "↑↓ 选择 Hub · Enter 配置 · m/→ 配置 · n 新建 Hub"
                " · r 重命名"
            )
        else:
            guide = (
                "↑↓ 选择 Hub · Enter 启动 · m/→ 管理 · n 新建 Hub"
                " · r 重命名"
            )
    elif HUB_CATALOG_ENABLED and not notice:
        guide = "↑↓ / jk 移动 · Enter 启动 · n 新建空白 Hub · 数字直达"
    guide_attr = C.get("warning", 0) if notice else C.get("dim", 0)
    _addstr(win, head + 2, 2, guide, guide_attr)
    row_cursor = head + 4
    if hubs:
        if expanded_hub:
            _addstr(
                win,
                row_cursor,
                2,
                f"Hub 工作区 · {len(hubs)} 个",
                C.get("teal", 0) | curses.A_BOLD,
            )
            row_cursor += 1
            row_width = max(0, w - 4)
            footer_row = max(0, h - 1)
            hub_capacity = max(0, footer_row - row_cursor - 2)
            hub_start, hub_end = _visible_window(len(hubs), hub_idx, hub_capacity)
            for row_offset, hub_index in enumerate(range(hub_start, hub_end)):
                named = hubs[hub_index]
                selected = hub_focus and hub_index == hub_idx
                marker = "▸" if selected else " "
                text = marker + " " + _hub_home_text(
                    named, max(0, row_width - 2)
                )
                attr = (
                    C.get("sel", curses.A_REVERSE)
                    if selected
                    else C.get(
                        _hub_identity_color(hub_index % len(_HUB_IDENTITY_COLORS)),
                        0,
                    )
                    | curses.A_BOLD
                )
                _addstr(
                    win,
                    row_cursor + row_offset,
                    2,
                    _pad_display(text, row_width) if selected else text,
                    attr,
                )
            row_cursor += hub_end - hub_start
            _addstr(win, row_cursor, 2, "单渠道直连", C.get("dim", 0))
            row_cursor += 1
        else:
            named = hubs[max(0, min(hub_idx, len(hubs) - 1))]
            entry = _hub_home_text(named, max(0, w - 4))
            entry_width = max(0, w - 4)
            attr = (
                C.get("sel", curses.A_REVERSE)
                if hub_focus
                else C.get(
                    _hub_identity_color(hub_idx % len(_HUB_IDENTITY_COLORS)),
                    0,
                )
                | curses.A_BOLD
            )
            rendered = (
                _pad_display(entry, entry_width)
                if hub_focus
                else _truncate_display(entry, entry_width)
            )
            _addstr(win, row_cursor, 2, rendered, attr)
            row_cursor += 1
    list_top = row_cursor
    footer_row = max(0, h - 1)
    capacity = max(0, footer_row - list_top)
    start, end = _visible_window(len(view), idx, capacity)
    recent = _recent_name(view, mru)

    if not view:
        _addstr(win, list_top, 2, "没有可用渠道", C.get("warning", 0))
    row_width = max(0, w - 4)
    for row_offset, i in enumerate(range(start, end)):
        provider_id = view[i]
        m = meta[provider_id]
        name = _provider_meta_label(meta, provider_id)
        hidden = m.get("hidden")
        rank = i + 1
        selected = (not hub_focus) and (i == idx)
        marker = "▸" if selected else " "
        label = f"{marker} {rank:>2}  {name}"
        status: list[str] = []
        if m.get("alias"):
            status.append(str(m["alias"]))
        if provider_id == recent:
            status.append("最近")
        if hidden:
            status.append("已隐藏")
        line = _compose_row(label, " · ".join(status), row_width)
        row = list_top + row_offset
        if selected:
            _addstr(
                win,
                row,
                2,
                _pad_display(line, row_width),
                C.get("sel", curses.A_REVERSE),
            )
        else:
            row_attr = (
                _row_pairs[i % len(_row_pairs)]
                if _row_pairs
                else C.get("base", 0)
            )
            attr = (
                C.get("dim", 0)
                if hidden
                else row_attr | curses.A_BOLD
            )
            _addstr(win, row, 2, line, attr)

    pending_selected = selected_hub is not None and selected_hub.state is None
    if help_open and hub_focus and hubs and pending_selected:
        foot = "Hub：首页 Enter/m/→ 继续配置 · n 新建 · r 重命名 · ? 返回"
    elif help_open and hub_focus and hubs:
        foot = "Hub：首页 Enter 启动 · m/→ 管理 · n 新建 · r 重命名 · ? 返回"
    elif help_open:
        foot = "a 设置别名 · x 隐藏/显示 · h 隐藏项 · ? 返回 · q 退出"
    elif hub_focus and hubs and pending_selected:
        foot = "Hub · Enter/m/→ 继续配置 · n 新建 · r 重命名 · q 退出"
    elif hub_focus and hubs:
        foot = "Hub · Enter 启动 · m/→ 槽位与渠道 · n 新建 · r 重命名 · q 退出"
    else:
        visible_range = ""
        if start > 0 or end < len(view):
            visible_range = f" · {start + 1}–{end}/{len(view)}"
        create_hint = " · n 新建 Hub" if HUB_CATALOG_ENABLED else ""
        foot = (
            f"共 {len(view)} 个{visible_range}{create_hint}"
            " · ? 更多操作 · q 退出"
        )
    _addstr(win, footer_row, 2, foot, C.get("dim", 0))
    win.refresh()


def _hub_columns(width: int) -> tuple[int, int, int, int, int]:
    """Responsive widths for slot / channel / model / effort / status."""
    usable = max(0, width - 4)
    family, channel, model, effort, status = 6, 5, 4, 5, 4
    extra = max(0, usable - 4 - sum((family, channel, model, effort, status)))
    growth = min(10 - channel, extra)
    channel, extra = channel + growth, extra - growth
    growth = min(10 - family, extra)
    family, extra = family + growth, extra - growth
    growth = min(8 - effort, extra)
    effort, extra = effort + growth, extra - growth
    growth = min(8 - status, extra)
    status, extra = status + growth, extra - growth
    model += extra
    return (family, channel, model, effort, status)


def _hub_row_text(values: tuple[str, ...], cols: tuple[int, ...]) -> str:
    return " ".join(_pad_display(value, width) for value, width in zip(values, cols))


def _draw_hub_workspace(
    win,
    status: "HubStatus",
    rows: list["HubSlotOption | HubModelOption | HubChannel"],
    idx: int,
    tab: str = "slots",
    notice: str | None = None,
    hub_name: str = "Claude-Hub",
) -> None:
    """Render native slots followed by the unbound model pool."""
    win.erase()
    h, w = win.getmaxyx()
    _addstr(win, 0, 2, f"Claude1  ›  {hub_name}", C.get("dim", 0))
    tabs = "[Slots]   Channels" if tab == "slots" else " Slots   [Channels]"
    _addstr(win, 1, 2, tabs, C.get("lime", 0) | curses.A_BOLD)
    if status.healthy is True:
        badge, badge_attr = "● 已就绪", C.get("lime", 0) | curses.A_BOLD
    elif status.healthy is False:
        badge, badge_attr = "● 未就绪（选择后自动拉起）", C.get("warning", 0)
    else:
        badge, badge_attr = "● 探测中…", C.get("dim", 0)
    _addstr(win, 2, 2, badge, badge_attr)
    meta = (
        f"127.0.0.1:{status.port} · {status.channel_count} 渠道"
        f" · {status.model_count} 模型 · 启动 {status.launch_selector}"
        f" · {status.launch_effort}"
    )
    _addstr(win, 2, 4 + _dwidth(badge), meta, C.get("dim", 0))

    cols = _hub_columns(w)
    header = _hub_row_text(("  槽位", "渠道", "模型", "effort", "状态"), cols)
    _addstr(win, 4, 2, header, C.get("dim", 0))
    list_top = 5
    footer_row = max(0, h - 1)
    capacity = max(0, footer_row - list_top)
    start, end = _visible_window(len(rows), idx, capacity)
    for offset, i in enumerate(range(start, end)):
        item = rows[i]
        if isinstance(item, HubSlotOption):
            option = item.option
            kind = item.slot.title()
            effort = item.effort
            state = "启动" if item.slot == status.launch_slot else "已绑定"
        elif isinstance(item, HubChannel):
            is_fallback = (
                item.alias == status.default_channel
                and item.models[0] == status.default_model
            )
            option = HubModelOption(
                family=_hub_model_family(item.models[0]),
                channel=item.alias,
                model=item.models[0],
                is_default=is_fallback,
                via_proxy=item.via_proxy,
                is_1m="[1m]" in item.models[0].casefold(),
            )
            kind = "渠道"
            effort = "—"
            state = "回退" if option.is_default else "可用"
        else:
            option = item
            kind = "池"
            effort = "—"
            state = option.status_label
        marker = "▸" if i == idx else " "
        text = _hub_row_text(
            (
                f"{marker} {kind}",
                option.channel,
                option.model,
                effort,
                state,
            ),
            cols,
        )
        row = list_top + offset
        if i == idx:
            _addstr(
                win,
                row,
                2,
                _pad_display(text, max(0, w - 4)),
                C.get("sel", curses.A_REVERSE),
            )
        else:
            family_color = _HUB_FAMILY_COLOR.get(option.family, "violet")
            _addstr(win, row, 2, text, C.get(family_color, 0) | curses.A_BOLD)
    foot = notice or (
        "Esc 返回 · ↑↓/jk 选择 · a 添加 · d 删除 · Enter 启动 · Tab 槽位"
        if tab == "channels"
        else "Esc 返回 · ↑↓/jk 选择 · ←/→/e effort · b 绑定 · Enter 启动 · Tab 渠道"
    )
    _addstr(
        win,
        footer_row,
        2,
        foot,
        C.get("warning", 0) if notice else C.get("dim", 0),
    )
    win.refresh()


def _choose_hub_slot(win, prompt: str) -> str | None:
    """Read one native slot shortcut for a binding operation."""
    h, _w = win.getmaxyx()
    _addstr(
        win,
        max(0, h - 1),
        2,
        f"{prompt} · f Fable / o Opus / s Sonnet / h Haiku · Esc 取消",
        C.get("warning", 0),
    )
    win.refresh()
    shortcuts = {"f": "fable", "o": "opus", "s": "sonnet", "h": "haiku"}
    while True:
        ch = win.getch()
        if ch in (-1, 27):
            return None
        slot = shortcuts.get(chr(ch).casefold()) if 0 <= ch <= 0x10FFFF else None
        if slot is not None:
            return slot


_HUB_WIZARD_STAGES = ("渠道", "模型", "设置", "确认")


def _draw_hub_wizard_shell(
    win,
    stage: int,
    title: str,
    *,
    detail: str = "",
    footer: str = "Esc 取消 · ↑↓/jk 选择 · Enter 继续",
) -> int:
    """Draw the shared four-stage add-channel surface and return its list top."""
    win.erase()
    h, w = win.getmaxyx()
    width = max(0, w - 4)
    _addstr(
        win,
        0,
        2,
        _truncate_display("Claude1  ›  Claude-Hub  ›  新增 Hub 渠道", width),
        C.get("dim", 0),
    )
    compact = h < 10
    progress_row = 1 if compact else 2
    title_row = 2 if compact else 4
    progress_x = 2
    for index, label in enumerate(_HUB_WIZARD_STAGES):
        marker = "✓" if index < stage else "●" if index == stage else "○"
        token = f"{marker} {label}"
        if index < stage:
            attr = C.get("lime", 0)
        elif index == stage:
            attr = C.get("orange", C.get("accent", 0)) | curses.A_BOLD
        else:
            attr = C.get("dim", 0)
        _addstr(win, progress_row, progress_x, token, attr)
        progress_x += _dwidth(token)
        if index < len(_HUB_WIZARD_STAGES) - 1:
            separator = "  ─  "
            _addstr(win, progress_row, progress_x, separator, C.get("dim", 0))
            progress_x += _dwidth(separator)
    _addstr(
        win,
        title_row,
        2,
        _truncate_display(title, width),
        C.get("accent", 0) | curses.A_BOLD,
    )
    if detail and h >= 8:
        _addstr(
            win,
            title_row + 1,
            2,
            _truncate_display(detail, width),
            C.get("dim", 0),
        )
    _addstr(
        win,
        max(0, h - 1),
        2,
        _truncate_display(footer, width),
        C.get("dim", 0),
    )
    if not compact:
        return 7
    return 4 if h >= 8 else 3


def _draw_hub_wizard_options(
    win,
    options: list[tuple[str, str]],
    idx: int,
    list_top: int,
    color_keys: list[str] | None = None,
) -> None:
    """Render one selectable option list inside the add-channel surface."""
    h, w = win.getmaxyx()
    row_width = max(0, w - 4)
    capacity = max(1, h - 1 - list_top)
    start, end = _visible_window(len(options), idx, capacity)
    for offset, option_index in enumerate(range(start, end)):
        primary, secondary = options[option_index]
        selected = option_index == idx
        marker = "▸" if selected else " "
        line = marker + " " + _compose_row(
            primary,
            secondary,
            max(0, row_width - 2),
        )
        if color_keys and option_index < len(color_keys):
            row_attr = C.get(color_keys[option_index], 0)
        elif _row_pairs:
            row_attr = _row_pairs[option_index % len(_row_pairs)]
        else:
            row_attr = C.get("base", 0)
        _addstr(
            win,
            list_top + offset,
            2,
            _pad_display(line, row_width) if selected else line,
            C.get("sel", curses.A_REVERSE)
            if selected
            else row_attr | curses.A_BOLD,
        )


def _prompt_hub_text(
    win,
    label: str,
    initial: str,
    *,
    stage: int,
    title: str,
    detail: str = "",
) -> str | None:
    """Full-surface text field that works with the launcher's getch test seam."""
    value = initial
    while True:
        list_top = _draw_hub_wizard_shell(
            win,
            stage,
            title,
            detail=detail,
            footer="Esc 取消 · 输入文字 · Backspace 删除 · Enter 继续",
        )
        _addstr(win, list_top, 2, label, C.get("dim", 0))
        display_value = value or "请输入…"
        row_width = max(0, win.getmaxyx()[1] - 4)
        _addstr(
            win,
            min(list_top + 2, max(0, win.getmaxyx()[0] - 2)),
            2,
            _pad_display(f"› {display_value}", row_width),
            C.get("sel", curses.A_REVERSE),
        )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        if ch in (10, 13, curses.KEY_ENTER):
            return value.strip()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif 32 <= ch <= 126:
            value += chr(ch)


def _prompt_named_hub(
    win,
    title: str,
    initial: str,
    detail: str,
) -> str | None:
    """Edit a top-level Hub display name on a dedicated visual surface."""
    value = initial
    while True:
        win.erase()
        h, w = win.getmaxyx()
        width = max(0, w - 4)
        _addstr(win, 0, 2, "Claude1  ›  Hub 工作区", C.get("dim", 0))
        _addstr(win, 2, 2, title, C.get("teal", 0) | curses.A_BOLD)
        _addstr(win, 3, 2, detail, C.get("dim", 0))
        _addstr(win, 5, 2, "Hub 名称", C.get("dim", 0))
        _addstr(
            win,
            min(7, max(0, h - 2)),
            2,
            _pad_display(f"› {value or '请输入…'}", width),
            C.get("sel", curses.A_REVERSE),
        )
        _addstr(
            win,
            max(0, h - 1),
            2,
            "Esc 取消 · Backspace 删除 · Enter 确认",
            C.get("dim", 0),
        )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        if ch in (10, 13, curses.KEY_ENTER):
            return value.strip()
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif 32 <= ch <= 0x10FFFF:
            char = chr(ch)
            if not unicodedata.category(char).startswith("C"):
                value += char


def _navigate_hub_choices(
    win,
    option_count: int,
    render,
    *,
    initial_index: int = 0,
    shortcuts: dict[str, int] | None = None,
) -> int | None:
    """Run the shared choice key contract while the caller owns rendering."""
    idx = initial_index
    while True:
        render(idx)
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % option_count
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % option_count
        elif ch in (10, 13, curses.KEY_ENTER):
            return idx
        elif shortcuts is not None and 0 <= ch <= 0x10FFFF:
            shortcut_index = shortcuts.get(chr(ch).casefold())
            if shortcut_index is not None:
                return shortcut_index


def _choose_hub_provider(win, providers: list[dict]) -> dict | None:
    if not providers:
        return None

    options = [
        (
            str(provider.get("name") or provider.get("id")),
            str(provider.get("alias") or "CC Switch"),
        )
        for provider in providers
    ]

    def render(idx: int) -> None:
        list_top = _draw_hub_wizard_shell(
            win,
            0,
            "选择 CC Switch 渠道",
            detail="选择凭据与上游配置的来源，不会修改 CC Switch 当前渠道",
        )
        _draw_hub_wizard_options(win, options, idx, list_top)

    selected = _navigate_hub_choices(win, len(providers), render)
    return providers[selected] if selected is not None else None


def _choose_hub_models(
    win,
    provider: dict,
    models: list[str],
) -> list[str] | None:
    """Choose one or more provider models; declared models start selected."""
    candidates = list(models)
    selected = set(candidates)
    idx = 0
    notice = ""
    while True:
        item_count = len(candidates) + 1
        idx = max(0, min(idx, item_count - 1))
        provider_name = provider.get("name") or provider.get("id")
        selection_summary = f"已选 {len(selected)} 个"
        if notice:
            selection_summary += f" · {notice}"
        list_top = _draw_hub_wizard_shell(
            win,
            1,
            "选择要加入 Hub 的模型",
            detail=f"渠道 · {provider_name} · {selection_summary}",
            footer="Esc 取消 · ↑↓/jk 移动 · Space 勾选 · a 手动添加 · Enter 继续",
        )
        options = [
            (
                f"[{'✓' if model in selected else ' '}] {model}",
                _hub_model_family(model),
            )
            for model in candidates
        ]
        options.append(("＋ 手动添加模型 ID", "候选中没有时使用"))
        color_keys = [
            _HUB_FAMILY_COLOR.get(_hub_model_family(model), "violet")
            for model in candidates
        ]
        color_keys.append("gold")
        _draw_hub_wizard_options(
            win,
            options,
            idx,
            list_top,
            color_keys=color_keys,
        )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        notice = ""
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % item_count
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % item_count
        elif ch == ord(" ") and idx < len(candidates):
            model = candidates[idx]
            if model in selected:
                selected.remove(model)
            else:
                selected.add(model)
        elif ch in (ord("a"), ord("A")) or (
            ch in (10, 13, curses.KEY_ENTER) and idx == len(candidates)
        ):
            custom = _prompt_hub_text(
                win,
                "模型 ID",
                "",
                stage=1,
                title="手动添加模型 ID",
                detail=f"渠道 · {provider_name}",
            )
            if custom:
                if custom not in candidates:
                    candidates.append(custom)
                selected.add(custom)
                idx = candidates.index(custom)
        elif ch in (10, 13, curses.KEY_ENTER):
            if selected:
                return [model for model in candidates if model in selected]
            notice = "请至少勾选一个模型"


def _hub_alias_slug(name: object) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name).casefold()).strip("-_")
    if not slug or not slug[0].isalpha():
        slug = f"channel-{slug}".rstrip("-")
    return slug or "channel"


def set_hub_setup_mapping(
    hub: HubRef,
    slot: str,
    provider: dict,
    model: str,
    *,
    api_format: str | None = None,
) -> dict:
    """Persist one exact provider/model choice in a setup-only draft."""
    if slot not in HUB_SLOT_ORDER:
        raise ValueError("Hub 首次设置槽位无效")
    provider_id = str(provider.get("id") or "").strip()
    provider_name = str(
        provider.get("alias") or provider.get("name") or provider_id
    ).strip()
    model = model.strip() if isinstance(model, str) else ""
    if not provider_id:
        raise ValueError("provider 缺少稳定 id")
    if not model or "," in model:
        raise ValueError("model 必须是非空且不能包含逗号")
    if api_format is not None and api_format not in {
        "anthropic",
        "openai_chat",
        "openai_responses",
    }:
        raise ValueError("api_format 无效")
    with _hub_setup_draft_lock(hub):
        draft = load_hub_setup_draft(hub)
        mappings = draft["mappings"]
        alias = next(
            (
                mapping["alias"]
                for other_slot, mapping in mappings.items()
                if other_slot != slot
                and mapping["provider_id"] == provider_id
                and mapping.get("api_format") == api_format
            ),
            None,
        )
        if alias is None:
            base = _hub_alias_slug(provider_name)
            used_by_other = {
                mapping["alias"]
                for other_slot, mapping in mappings.items()
                if other_slot != slot
            }
            alias = base
            suffix = 2
            while alias in used_by_other:
                alias = f"{base}-{suffix}"
                suffix += 1
        mapping = {
            "provider_id": provider_id,
            "provider_name": provider_name,
            "alias": alias,
            "model": model,
        }
        if api_format is not None:
            mapping["api_format"] = api_format
        mappings[slot] = mapping
        normalized = normalize_hub_setup_draft(draft)
        assert hub.draft_path is not None
        _validate_catalog_path_chain(hub.draft_path)
        _atomic_private_write(
            hub.draft_path,
            _hub_setup_draft_text(normalized),
        )
        return normalized


def clear_hub_setup_mapping(hub: HubRef, slot: str) -> dict:
    if slot not in HUB_SLOT_ORDER:
        raise ValueError("Hub 首次设置槽位无效")
    with _hub_setup_draft_lock(hub):
        draft = load_hub_setup_draft(hub)
        draft["mappings"].pop(slot, None)
        normalized = normalize_hub_setup_draft(draft)
        assert hub.draft_path is not None
        _validate_catalog_path_chain(hub.draft_path)
        _atomic_private_write(
            hub.draft_path,
            _hub_setup_draft_text(normalized),
        )
        return normalized


def _hub_config_from_setup_draft(
    hub: HubRef,
    draft: dict,
    port: int,
) -> dict:
    draft = normalize_hub_setup_draft(draft)
    missing = [slot for slot in HUB_SLOT_ORDER if slot not in draft["mappings"]]
    if missing:
        raise ValueError(
            "还需配置 " + "、".join(slot.title() for slot in missing)
        )
    channels: dict[str, dict] = {}
    model_slots: dict[str, str] = {}
    for slot in HUB_SLOT_ORDER:
        mapping = draft["mappings"][slot]
        alias = mapping["alias"]
        expected_format = mapping.get("api_format")
        channel = channels.get(alias)
        if channel is None:
            channel = {
                "provider": f"id:{mapping['provider_id']}",
                "models": [],
            }
            if expected_format is not None:
                channel["api_format"] = expected_format
            channels[alias] = channel
        elif channel["provider"] != f"id:{mapping['provider_id']}" or (
            channel.get("api_format") != expected_format
            and ("api_format" in channel or expected_format is not None)
        ):
            raise ValueError(f"Hub 渠道 alias 冲突: {alias}")
        if mapping["model"] not in channel["models"]:
            channel["models"].append(mapping["model"])
        model_slots[slot] = f"{alias},{mapping['model']}"
    return normalize_hub_config(
        {
            "version": HUB_CONFIG_VERSION,
            "instance_id": hub.hub_id,
            "port": port,
            "local_token": secrets.token_urlsafe(32),
            "default_channel": draft["mappings"]["fable"]["alias"],
            "channels": channels,
            "model_slots": model_slots,
            "launch_slot": "fable",
            "effort_by_slot": dict(HUB_DEFAULT_EFFORTS),
        }
    )


def _recover_setup_config(
    hub: HubRef,
    draft: dict,
    ready_configs: dict[str, dict],
) -> dict:
    """Validate a final config left by an interrupted setup promotion."""
    actual = load_hub_config(hub=hub)
    port = _hub_port(actual)
    expected = _hub_config_from_setup_draft(hub, draft, port)
    compared_fields = (
        "version",
        "instance_id",
        "default_channel",
        "channels",
        "model_slots",
        "launch_slot",
        "effort_by_slot",
    )
    if any(actual.get(field) != expected.get(field) for field in compared_fields):
        raise ValueError(f"Hub {hub.name} 存在无法恢复的未登记配置")
    local_token = actual.get("local_token")
    if not isinstance(local_token, str) or not local_token.strip():
        raise ValueError(f"Hub {hub.name} 的恢复配置缺少本地凭证")
    port_available = True
    try:
        hub_catalog.validate_unique_hub_ports(
            {**ready_configs, hub.hub_id: actual}
        )
    except ValueError:
        port_available = False
    try:
        with _reserve_loopback_port(port):
            pass
    except OSError:
        port_available = False
    if not port_available:
        actual["port"] = _next_hub_port(ready_configs, 18787)
        actual = normalize_hub_config(actual)
        _validate_catalog_path_chain(hub.config_path)
        _atomic_private_write(hub.config_path, _hub_config_text(actual))
    return actual


def complete_hub_setup(hub: HubRef) -> tuple[HubRef, dict]:
    """Atomically promote a four-slot draft to one runnable Hub v2 config."""
    if not HUB_CATALOG_ENABLED:
        raise ValueError("显式单 Hub 配置模式不支持首次设置")
    with _hub_catalog_lock():
        catalog = hub_catalog.load_hub_catalog(
            json.loads(_read_regular_text(HUB_CATALOG, "hub catalog"))
        )
        current = _hub_ref_from_catalog(catalog, hub.hub_id)
        if current.state != "setup" or current.draft_path is None:
            raise ValueError(f"Hub {current.name} 已完成配置")
        with _hub_setup_draft_lock(current):
            draft = load_hub_setup_draft(current)
            ready_configs = {
                existing_id: load_hub_config(
                    hub=_hub_ref_from_catalog(catalog, existing_id)
                )
                for existing_id in catalog["order"]
                if catalog["hubs"][existing_id].get("state", "ready") == "ready"
            }
            created_config = not current.config_path.exists()
            if created_config:
                port = _next_hub_port(ready_configs, 18787)
                config = _hub_config_from_setup_draft(current, draft, port)
            else:
                config = _recover_setup_config(current, draft, ready_configs)
            updated_catalog = json.loads(json.dumps(catalog))
            updated_entry = updated_catalog["hubs"][current.hub_id]
            updated_entry["state"] = "ready"
            updated_entry.pop("draft", None)
            normalized_catalog = hub_catalog.normalize_hub_catalog(updated_catalog)
            if created_config:
                _validate_catalog_path_chain(current.config_path)
                _atomic_private_write(current.config_path, _hub_config_text(config))
            try:
                _atomic_private_write(
                    HUB_CATALOG,
                    _hub_catalog_text(normalized_catalog),
                )
            except BaseException:
                if created_config:
                    try:
                        current.config_path.unlink()
                    except OSError:
                        pass
                raise
            try:
                current.draft_path.unlink()
            except OSError:
                pass
    ready_ref = _hub_ref_from_catalog(normalized_catalog, hub.hub_id)
    return ready_ref, config


def _infer_hub_provider_api_format(provider: dict) -> str | None:
    """Return a metadata-backed format, or None when passthrough is uncertain."""
    try:
        settings = json.loads(provider.get("settings_config") or "{}")
    except (TypeError, json.JSONDecodeError):
        settings = {}
    try:
        meta = json.loads(provider.get("meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(settings, dict):
        settings = {}
    if not isinstance(meta, dict):
        meta = {}
    provider_type = provider.get("provider_type") or meta.get("providerType")
    if provider_type == "codex_oauth":
        return "openai_responses"
    for value in (meta.get("apiFormat"), settings.get("api_format")):
        if value in {"anthropic", "openai_chat", "openai_responses"}:
            return str(value)
    legacy = settings.get("openrouter_compat_mode")
    if legacy is True or legacy == 1 or (
        isinstance(legacy, str) and legacy.strip().casefold() in {"1", "true"}
    ):
        return "openai_chat"
    return None


def _choose_hub_api_format(win, detail: str = "") -> str | None:
    """Ask only when CC Switch metadata cannot determine the upstream protocol."""

    def render(idx: int) -> None:
        list_top = _draw_hub_wizard_shell(
            win,
            2,
            "选择上游协议",
            detail=detail or "CC Switch 中没有可确认的协议元数据",
            footer="Esc 取消 · ↑↓/jk 选择 · Enter 继续 · a/c/r 快捷选择",
        )
        _draw_hub_wizard_options(
            win,
            [
                (label, description)
                for _value, label, description in _HUB_API_FORMAT_CHOICES
            ],
            idx,
            list_top,
            color_keys=["orange", "teal", "violet"],
        )

    selected = _navigate_hub_choices(
        win,
        len(_HUB_API_FORMAT_CHOICES),
        render,
        shortcuts=_HUB_API_FORMAT_SHORTCUTS,
    )
    return (
        _HUB_API_FORMAT_CHOICES[selected][0]
        if selected is not None
        else None
    )


def _draw_hub_setup_choice_shell(
    win,
    hub_name: str,
    slot: str,
    title: str,
    *,
    detail: str = "",
    footer: str = "Esc 返回四槽页 · ↑↓/jk 选择 · Enter 继续",
) -> int:
    win.erase()
    h, w = win.getmaxyx()
    width = max(0, w - 4)
    slot_index = HUB_SLOT_ORDER.index(slot) + 1
    _addstr(
        win,
        0,
        2,
        _truncate_display(f"Claude1  ›  {hub_name}  ›  首次设置", width),
        C.get("dim", 0),
    )
    compact = h < 12
    progress_row = 1 if compact else 2
    title_row = 2 if compact else 4
    _addstr(
        win,
        progress_row,
        2,
        f"映射 {slot.title()} · 第 {slot_index}/{len(HUB_SLOT_ORDER)} 槽",
        C.get(_hub_identity_color(slot_index - 1), 0) | curses.A_BOLD,
    )
    _addstr(win, title_row, 2, title, C.get("lime", 0) | curses.A_BOLD)
    show_detail = bool(detail) and (not compact or h >= 10)
    if show_detail:
        _addstr(
            win,
            title_row + 1,
            2,
            _truncate_display(detail, width),
            C.get("dim", 0),
        )
    _addstr(win, max(0, h - 1), 2, footer, C.get("dim", 0))
    if not compact:
        return 7
    return title_row + (2 if show_detail else 1)


def _choose_hub_setup_provider(
    win,
    hub_name: str,
    slot: str,
    providers: list[dict],
    initial_provider_id: str | None = None,
) -> dict | None:
    if not providers:
        return None
    initial_index = next(
        (
            index
            for index, provider in enumerate(providers)
            if str(provider.get("id")) == initial_provider_id
        ),
        0,
    )
    options = [
        (
            str(provider.get("name") or provider.get("id")),
            str(provider.get("alias") or "CC Switch"),
        )
        for provider in providers
    ]

    def render(idx: int) -> None:
        list_top = _draw_hub_setup_choice_shell(
            win,
            hub_name,
            slot,
            "选择渠道",
            detail="来自 CC Switch；不会切换它当前使用的渠道",
        )
        _draw_hub_wizard_options(
            win,
            options,
            idx,
            list_top,
        )

    selected = _navigate_hub_choices(
        win,
        len(providers),
        render,
        initial_index=initial_index,
    )
    return providers[selected] if selected is not None else None


def _prompt_hub_setup_model(
    win,
    hub_name: str,
    slot: str,
    provider_name: str,
) -> str | None:
    value = ""
    notice: str | None = None
    while True:
        list_top = _draw_hub_setup_choice_shell(
            win,
            hub_name,
            slot,
            "手动输入模型 ID",
            detail=f"渠道 · {provider_name}",
            footer=(
                notice
                or "Esc 返回四槽页 · 输入模型 ID · Backspace 删除 · Enter 继续"
            ),
        )
        notice = None
        row_width = max(0, win.getmaxyx()[1] - 4)
        _addstr(
            win,
            list_top,
            2,
            _pad_display(f"› {value or '请输入…'}", row_width),
            C.get("sel", curses.A_REVERSE),
        )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        if ch in (10, 13, curses.KEY_ENTER):
            value = value.strip()
            if not value:
                notice = "模型 ID 不能为空"
            elif "," in value:
                notice = "模型 ID 不能包含逗号"
            else:
                return value
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif 32 <= ch <= 0x10FFFF:
            char = chr(ch)
            if not unicodedata.category(char).startswith("C"):
                value += char


def _choose_hub_setup_model(
    win,
    hub_name: str,
    slot: str,
    provider: dict,
    models: list[str],
    initial_model: str | None = None,
) -> str | None:
    candidates = list(dict.fromkeys(models))
    idx = candidates.index(initial_model) if initial_model in candidates else 0
    provider_name = str(provider.get("name") or provider.get("id"))
    while True:
        item_count = len(candidates) + 1
        list_top = _draw_hub_setup_choice_shell(
            win,
            hub_name,
            slot,
            "选择一个模型",
            detail=f"渠道 · {provider_name}",
            footer="Esc 返回四槽页 · ↑↓/jk 选择 · a 手动输入 · Enter 确认",
        )
        options = [
            (model, _hub_model_family(model)) for model in candidates
        ] + [("＋ 手动输入模型 ID", "候选中没有时使用")]
        colors = [
            _HUB_FAMILY_COLOR.get(_hub_model_family(model), "violet")
            for model in candidates
        ] + ["gold"]
        _draw_hub_wizard_options(
            win,
            options,
            idx,
            list_top,
            color_keys=colors,
        )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % item_count
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % item_count
        elif ch in (ord("a"), ord("A")) or (
            ch in (10, 13, curses.KEY_ENTER) and idx == len(candidates)
        ):
            custom = _prompt_hub_setup_model(
                win,
                hub_name,
                slot,
                provider_name,
            )
            if custom:
                return custom
        elif ch in (10, 13, curses.KEY_ENTER):
            return candidates[idx]


def _choose_hub_setup_api_format(
    win,
    hub_name: str,
    slot: str,
    detail: str,
) -> str | None:

    def render(idx: int) -> None:
        list_top = _draw_hub_setup_choice_shell(
            win,
            hub_name,
            slot,
            "选择上游协议",
            detail=detail,
            footer=(
                "Esc 返回四槽页 · ↑↓/jk 选择 · Enter 继续 · "
                "a/c/r 快捷选择"
            ),
        )
        _draw_hub_wizard_options(
            win,
            [
                (label, description)
                for _value, label, description in _HUB_API_FORMAT_CHOICES
            ],
            idx,
            list_top,
            color_keys=["orange", "teal", "violet"],
        )

    selected = _navigate_hub_choices(
        win,
        len(_HUB_API_FORMAT_CHOICES),
        render,
        shortcuts=_HUB_API_FORMAT_SHORTCUTS,
    )
    return (
        _HUB_API_FORMAT_CHOICES[selected][0]
        if selected is not None
        else None
    )


def _draw_hub_setup(
    win,
    hub: HubRef,
    draft: dict,
    idx: int,
    notice: str | None = None,
) -> None:
    win.erase()
    h, w = win.getmaxyx()
    width = max(0, w - 4)
    mappings = draft["mappings"]
    complete_count = len(mappings)
    slot_count = len(HUB_SLOT_ORDER)
    _addstr(
        win,
        0,
        2,
        _truncate_display(f"Claude1  ›  {hub.name}  ›  首次设置", width),
        C.get("dim", 0),
    )
    compact = h < 12
    if compact:
        _addstr(
            win,
            1,
            2,
            _compose_row(
                "选择四槽映射",
                f"已完成 {complete_count}/{slot_count}",
                width,
            ),
            C.get("lime", 0) | curses.A_BOLD,
        )
        list_top = 2
    else:
        _addstr(win, 2, 2, "选择四槽映射", C.get("lime", 0) | curses.A_BOLD)
        _addstr(
            win,
            3,
            2,
            _compose_row(
                "每个槽位选择一个渠道和模型",
                f"已完成 {complete_count}/{slot_count}",
                width,
            ),
            C.get("dim", 0),
        )
        list_top = 5
    for row_index, slot in enumerate(HUB_SLOT_ORDER):
        mapping = mappings.get(slot)
        marker = "▸" if idx == row_index else " "
        if mapping is None:
            right = "未设置"
        else:
            right = f"{mapping['alias']},{mapping['model']}"
        text = _compose_row(f"{marker} ◆ {slot.title()}", right, width)
        attr = (
            C.get("sel", curses.A_REVERSE)
            if idx == row_index
            else C.get(_hub_identity_color(row_index), 0) | curses.A_BOLD
        )
        _addstr(
            win,
            list_top + row_index,
            2,
            _pad_display(text, width) if idx == row_index else text,
            attr,
        )
    finish_index = len(HUB_SLOT_ORDER)
    finish_marker = "▸" if idx == finish_index else " "
    finish_right = "可以完成" if complete_count == 4 else f"还差 {4 - complete_count} 槽"
    finish = _compose_row(f"{finish_marker} ✓ 完成配置", finish_right, width)
    finish_row = list_top + finish_index + (0 if compact else 1)
    _addstr(
        win,
        finish_row,
        2,
        _pad_display(finish, width) if idx == finish_index else finish,
        (
            C.get("sel", curses.A_REVERSE)
            if idx == finish_index
            else C.get("lime" if complete_count == 4 else "dim", 0)
            | (curses.A_BOLD if complete_count == 4 else 0)
        ),
    )
    if notice:
        footer = notice
    elif idx == finish_index:
        footer = (
            "Esc 稍后设置 · Enter 完成配置"
            if complete_count == 4
            else f"Esc 稍后设置 · 还需配置 {4 - complete_count} 槽"
        )
    else:
        footer = "Esc 稍后设置 · ↑↓/jk 选择 · Enter 设置映射 · x 清除"
    _addstr(
        win,
        max(0, h - 1),
        2,
        footer,
        C.get("warning", 0) if notice else C.get("dim", 0),
    )
    win.refresh()


def _hub_setup_wizard(win, hub: HubRef) -> str:
    """Configure four exact mappings; never launch directly from setup."""
    draft = load_hub_setup_draft(hub)
    idx = next(
        (i for i, slot in enumerate(HUB_SLOT_ORDER) if slot not in draft["mappings"]),
        len(HUB_SLOT_ORDER),
    )
    notice: str | None = None
    while True:
        _draw_hub_setup(win, hub, draft, idx, notice)
        notice = None
        ch = win.getch()
        if ch == -1 or ch == ord("q"):
            return "quit"
        if ch == 27:
            return "back"
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % (len(HUB_SLOT_ORDER) + 1)
            continue
        if ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % (len(HUB_SLOT_ORDER) + 1)
            continue
        if ch == ord("x") and idx < len(HUB_SLOT_ORDER):
            try:
                draft = clear_hub_setup_mapping(hub, HUB_SLOT_ORDER[idx])
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                notice = str(exc)
            continue
        if ch not in (10, 13, curses.KEY_ENTER):
            continue
        if idx == len(HUB_SLOT_ORDER):
            try:
                complete_hub_setup(hub)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                notice = str(exc)
                continue
            return "completed"
        slot = HUB_SLOT_ORDER[idx]
        current_mapping = draft["mappings"].get(slot)
        try:
            providers = list_providers()
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            providers = []
        if not providers:
            notice = "CC Switch 中没有可映射的渠道"
            continue
        provider = _choose_hub_setup_provider(
            win,
            hub.name,
            slot,
            providers,
            (
                current_mapping["provider_id"]
                if current_mapping is not None
                else None
            ),
        )
        if provider is None:
            continue
        try:
            settings = json.loads(provider.get("settings_config") or "{}")
        except (TypeError, json.JSONDecodeError):
            settings = {}
        model = _choose_hub_setup_model(
            win,
            hub.name,
            slot,
            provider,
            _provider_models(settings, include_placeholder=False),
            (
                current_mapping["model"]
                if current_mapping is not None
                and current_mapping["provider_id"] == str(provider.get("id"))
                else None
            ),
        )
        if model is None:
            continue
        api_format = _infer_hub_provider_api_format(provider)
        if api_format is None:
            api_format = _choose_hub_setup_api_format(
                win,
                hub.name,
                slot,
                f"渠道 · {provider.get('name') or provider.get('id')}",
            )
            if api_format is None:
                continue
        try:
            draft = set_hub_setup_mapping(
                hub,
                slot,
                provider,
                model,
                api_format=api_format,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            notice = str(exc)
            continue
        idx = next(
            (
                i
                for i, candidate in enumerate(HUB_SLOT_ORDER)
                if candidate not in draft["mappings"]
            ),
            len(HUB_SLOT_ORDER),
        )


def _confirm_hub_channel_add(win, alias: str, models: list[str]) -> bool:
    """Confirm a pure Hub channel addition without offering slot replacement."""
    while True:
        list_top = _draw_hub_wizard_shell(
            win,
            3,
            "确认新增渠道",
            detail="只新增到 Claude-Hub，不修改 Fable / Opus / Sonnet / Haiku",
            footer="Esc 取消 · Enter 添加到 Hub",
        )
        h, w = win.getmaxyx()
        row_width = max(0, w - 4)
        _addstr(
            win,
            list_top,
            2,
            _compose_row("渠道别名", alias, row_width),
            C.get("teal", 0) | curses.A_BOLD,
        )
        _addstr(
            win,
            list_top + 1,
            2,
            f"模型 · {len(models)} 个",
            C.get("gold", 0) | curses.A_BOLD,
        )
        model_capacity = max(0, h - 1 - (list_top + 2))
        visible_count = model_capacity
        if len(models) > model_capacity and model_capacity > 0:
            visible_count -= 1
        visible_models = models[:visible_count]
        for offset, model in enumerate(visible_models):
            family = _hub_model_family(model)
            _addstr(
                win,
                list_top + 2 + offset,
                2,
                _truncate_display(f"  • {model}", row_width),
                C.get(_HUB_FAMILY_COLOR.get(family, "violet"), 0)
                | curses.A_BOLD,
            )
        hidden_count = len(models) - len(visible_models)
        if hidden_count > 0 and model_capacity > 0:
            _addstr(
                win,
                list_top + 2 + len(visible_models),
                2,
                f"  … 另 {hidden_count} 个模型",
                C.get("dim", 0),
            )
        win.refresh()
        ch = win.getch()
        if ch in (-1, 27):
            return False
        if ch in (10, 13, curses.KEY_ENTER):
            return True


def _hub_add_channel_wizard(
    win,
    *,
    hub: HubRef | None = None,
) -> dict | None:
    """Run the pure provider/models/settings/confirmation add flow."""
    provider = _choose_hub_provider(win, list_providers())
    if provider is None:
        return None
    try:
        settings = json.loads(provider.get("settings_config") or "{}")
    except (TypeError, json.JSONDecodeError):
        settings = {}
    inferred_api_format = _infer_hub_provider_api_format(provider)
    models = _choose_hub_models(
        win,
        provider,
        _provider_models(settings, include_placeholder=False),
    )
    if models is None:
        return None
    alias = _prompt_hub_text(
        win,
        "渠道别名",
        _hub_alias_slug(provider.get("name")),
        stage=2,
        title="设置渠道",
        detail=f"{provider.get('name') or provider.get('id')} · {len(models)} 个模型",
    )
    if alias is None:
        return None
    api_format = None
    if inferred_api_format is None:
        api_format = _choose_hub_api_format(
            win,
            detail=f"{alias} · {len(models)} 个模型",
        )
        if api_format is None:
            return None
    if not _confirm_hub_channel_add(win, alias, models):
        return None
    return add_hub_channel(
        provider,
        alias=alias,
        models=models,
        api_format=api_format,
        hub=hub,
    )


def _hub_workspace(
    win,
    status: "HubStatus",
    options: list["HubModelOption"],
    initial_slot: str | None = None,
    hub: HubRef | None = None,
) -> tuple[str, "HubLaunch | None"]:
    """Run the Hub model picker loop.

    Returns (outcome, option) where outcome is:
      "launch" — start the returned option through the hub,
      "back"   — Esc, return to the Claude1 home screen,
      "quit"   — q or terminal EOF, exit the launcher entirely.
    """
    config = load_hub_config(migrate=True, hub=hub)
    hub_name = hub.name if hub is not None else "Claude-Hub"
    hub_id = hub.hub_id if hub is not None else None
    slots, pool = build_hub_workspace(config)
    channels = build_hub_channels(config)
    tab = "slots"
    rows: list[HubSlotOption | HubModelOption | HubChannel] = [*slots, *pool]
    initial_slot = initial_slot if initial_slot in HUB_SLOT_ORDER else status.launch_slot
    idx = next(
        (index for index, item in enumerate(slots) if item.slot == initial_slot),
        0,
    )
    tab_indices = {"slots": idx, "channels": 0}
    notice: str | None = None
    # Draw once while probing so a down hub does not freeze the screen silently.
    _draw_hub_workspace(win, status, rows, idx, tab, hub_name=hub_name)
    instance_id = config.get("instance_id")
    health_token = (
        _hub_local_token(config) if isinstance(instance_id, str) else None
    )
    status = replace(
        status,
        healthy=hub_healthy(
            status.port,
            health_token,
            instance_id=instance_id if isinstance(instance_id, str) else None,
        ),
    )
    _draw_hub_workspace(win, status, rows, idx, tab, hub_name=hub_name)
    while True:
        ch = win.getch()
        notice = None
        if ch == -1:
            return ("quit", None)
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(rows)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(rows)
        elif ch == ord("\t"):
            tab_indices[tab] = idx
            tab = "channels" if tab == "slots" else "slots"
            rows = list(channels) if tab == "channels" else [*slots, *pool]
            idx = max(0, min(tab_indices[tab], len(rows) - 1))
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("e")):
            item = rows[idx]
            if not isinstance(item, HubSlotOption):
                continue
            direction = -1 if ch == curses.KEY_LEFT else 1
            try:
                config = cycle_hub_slot_effort(
                    item.slot,
                    item.effort,
                    direction,
                    hub=hub,
                )
            except ValueError as exc:
                notice = str(exc)
                try:
                    config = load_hub_config(hub=hub)
                    slots, pool = build_hub_workspace(config)
                    channels = build_hub_channels(config)
                    rows = [*slots, *pool]
                    idx = next(
                        index
                        for index, slot_item in enumerate(slots)
                        if slot_item.slot == item.slot
                    )
                    refreshed, _unused = build_hub_view(config)
                    status = replace(refreshed, healthy=status.healthy)
                except (OSError, ValueError, json.JSONDecodeError):
                    notice = "Hub 配置已变化且重新加载失败"
            except (OSError, json.JSONDecodeError):
                notice = "Hub 配置保存失败"
            else:
                slots, pool = build_hub_workspace(config)
                channels = build_hub_channels(config)
                rows = [*slots, *pool]
                refreshed, _unused = build_hub_view(config)
                status = replace(refreshed, healthy=status.healthy)
        elif ch == ord("b") and tab == "slots":
            item = rows[idx]
            if isinstance(item, (HubSlotOption, HubChannel)):
                continue
            target_slot = _choose_hub_slot(win, f"绑定 {item.selector}")
            if target_slot is None:
                continue
            current_selector = config["model_slots"][target_slot]
            if current_selector != item.selector and not _confirm(
                win,
                f"用 {item.selector} 替换 {target_slot} 的 {current_selector}?",
            ):
                continue

            def bind_slot(latest: dict) -> None:
                if latest["model_slots"][target_slot] != current_selector:
                    raise ValueError("槽位已被另一窗口修改，请重新确认")
                latest["model_slots"][target_slot] = item.selector

            try:
                config = (
                    mutate_hub_config(bind_slot)
                    if hub is None
                    else mutate_hub_config(bind_slot, hub=hub)
                )
            except ValueError as exc:
                notice = str(exc)
                try:
                    config = load_hub_config(hub=hub)
                    slots, pool = build_hub_workspace(config)
                    channels = build_hub_channels(config)
                    rows = [*slots, *pool]
                    idx = next(
                        (
                            index
                            for index, pool_item in enumerate(rows)
                            if isinstance(pool_item, HubModelOption)
                            and pool_item.selector == item.selector
                        ),
                        max(0, min(idx, len(rows) - 1)),
                    )
                    refreshed, _unused = build_hub_view(config)
                    status = replace(refreshed, healthy=status.healthy)
                except (OSError, ValueError, json.JSONDecodeError):
                    notice = "Hub 配置已变化且重新加载失败"
            except (OSError, json.JSONDecodeError):
                notice = "Hub 配置保存失败"
            else:
                slots, pool = build_hub_workspace(config)
                channels = build_hub_channels(config)
                rows = [*slots, *pool]
                idx = next(
                    index for index, slot_item in enumerate(slots)
                    if slot_item.slot == target_slot
                )
                refreshed, _unused = build_hub_view(config)
                status = replace(refreshed, healthy=status.healthy)
        elif ch == ord("a") and tab == "channels":
            try:
                updated = _hub_add_channel_wizard(win, hub=hub)
            except ValueError as exc:
                notice = str(exc)
                updated = None
            except (OSError, RuntimeError, json.JSONDecodeError, sqlite3.Error):
                notice = "Hub 渠道添加失败"
                updated = None
            if updated is not None:
                config = updated
                slots, pool = build_hub_workspace(config)
                channels = build_hub_channels(config)
                rows = list(channels)
                idx = max(0, len(rows) - 1)
                refreshed, _unused = build_hub_view(config)
                status = replace(refreshed, healthy=status.healthy)
        elif ch == ord("d") and tab == "channels":
            item = rows[idx]
            if not isinstance(item, HubChannel):
                continue
            if not _confirm(win, f"删除 Hub 渠道 {item.alias}?"):
                continue
            try:
                config = remove_hub_channel(item.alias, hub=hub)
            except ValueError as exc:
                notice = str(exc)
            except (OSError, json.JSONDecodeError):
                notice = "Hub 渠道删除失败"
            else:
                slots, pool = build_hub_workspace(config)
                channels = build_hub_channels(config)
                rows = list(channels)
                idx = max(0, min(idx, len(rows) - 1))
                refreshed, _unused = build_hub_view(config)
                status = replace(refreshed, healthy=status.healthy)
        elif ch in (10, 13, curses.KEY_ENTER):
            item = rows[idx]
            if isinstance(item, HubSlotOption):
                return ("launch", HubLaunch(slot=item.slot, hub_id=hub_id))
            if isinstance(item, HubChannel):
                _status, all_options = build_hub_view(config)
                selector = f"{item.alias},{item.models[0]}"
                option = next(
                    option for option in all_options if option.selector == selector
                )
                return (
                    "launch",
                    HubLaunch(option=option, hub_id=hub_id),
                )
            return ("launch", HubLaunch(option=item, hub_id=hub_id))
        elif ch == 27:
            return ("back", None)
        elif ch == ord("q"):
            return ("quit", None)
        else:
            continue
        tab_indices[tab] = idx
        _draw_hub_workspace(
            win,
            status,
            rows,
            idx,
            tab,
            notice,
            hub_name=hub_name,
        )


def _launcher_main(win, cfg, db_ids):
    """返回要启动的 provider id，或 None(退出不启动)。hide/alias 即时落盘。"""
    _safe_curs_set(0)
    win.keypad(True)
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    global C
    C = _init_colors()
    mru = load_mru()
    meta = cfg["providers"]
    show_hidden = False
    view = _build_view(cfg, db_ids, mru, show_hidden)
    idx = _initial_index(view, mru)
    hubs = _load_named_hub_launcher_states()
    hub_focus = bool(hubs)
    hub_idx = 0
    if HUB_CATALOG_ENABLED and hubs:
        try:
            default_id = load_hub_catalog(migrate=True)["default_hub"]
            hub_idx = next(
                index
                for index, named in enumerate(hubs)
                if named.hub.hub_id == default_id
            )
        except (OSError, ValueError, json.JSONDecodeError, StopIteration):
            hub_idx = 0
    help_open = False
    notice: str | None = None
    rows, cols = win.getmaxyx()
    intro_animate = (
        _animation_enabled()
        and _large_logo_supported(rows, cols)
        and (not hubs or rows >= 21)
    )
    pending_key = _intro(win) if intro_animate else None
    # The logo motion is a finite entrance, not a background task. Keep the
    # finished logo visible while blocking indefinitely with zero timer wakeups.
    win.timeout(-1)
    _draw_launcher(
        win,
        cfg,
        view,
        idx,
        show_hidden,
        mru,
        show_brand=True,
        hub_focus=hub_focus,
        hubs=hubs,
        hub_idx=hub_idx,
    )
    while True:
        ch = pending_key if pending_key is not None else win.getch()
        pending_key = None
        if ch == -1:
            # timeout(-1) returning -1 means the controlling terminal closed.
            return None
        notice = None
        direct_index = _digit_index(ch)
        if direct_index is not None:
            if direct_index < len(view):
                return view[direct_index]
            notice = f"没有第 {direct_index + 1} 个渠道"
        if ch in (curses.KEY_UP, ord("k")):
            if hub_focus:
                if hub_idx > 0:
                    hub_idx -= 1
            elif hubs:
                if idx <= 0:
                    hub_focus = True
                    hub_idx = len(hubs) - 1
                else:
                    idx -= 1
            elif view:
                idx = (idx - 1) % len(view)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if hub_focus:
                if hub_idx < len(hubs) - 1:
                    hub_idx += 1
                else:
                    hub_focus = False
                    idx = 0
            elif hubs:
                if view and idx < len(view) - 1:
                    idx += 1
            elif view:
                idx = (idx + 1) % len(view)
        elif (
            ch == ord("a")
            and hub_focus
            and hubs
            and not HUB_CATALOG_ENABLED
        ):
            try:
                updated = _hub_add_channel_wizard(win)
            except ValueError as exc:
                notice = str(exc)
                updated = None
            except (
                OSError,
                RuntimeError,
                json.JSONDecodeError,
                sqlite3.Error,
            ):
                notice = "Hub 渠道添加失败"
                updated = None
            if updated is not None:
                hubs = _load_named_hub_launcher_states()
                hub_idx = 0
                notice = "Hub 渠道已添加"
        elif (
            ch == ord("n") and HUB_CATALOG_ENABLED
        ) or (
            ch == ord("a") and hub_focus and bool(hubs)
        ):
            name = _prompt_named_hub(
                win,
                "新增 Hub 工作区",
                "",
                "创建空白 Hub；下一步配置 Fable / Opus / Sonnet / Haiku",
            )
            if name:
                try:
                    created = create_named_hub(name)
                    setup_outcome = _hub_setup_wizard(win, created)
                    if setup_outcome == "quit":
                        return None
                    hubs = _load_named_hub_launcher_states()
                    hub_focus = True
                    hub_idx = next(
                        index
                        for index, named in enumerate(hubs)
                        if named.hub.hub_id == created.hub_id
                    )
                    notice = (
                        f"{created.name} 配置完成，Enter 启动"
                        if setup_outcome == "completed"
                        else f"已创建 {created.name}，尚未配置"
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    StopIteration,
                ) as exc:
                    notice = str(exc)
        elif ch == ord("r") and hub_focus and hubs:
            selected = hubs[hub_idx].hub
            name = _prompt_named_hub(
                win,
                "重命名 Hub",
                selected.name,
                "只修改显示名；稳定 ID、配置、端口和运行身份保持不变",
            )
            if name and name != selected.name:
                try:
                    renamed = rename_named_hub(selected.hub_id, name)
                    hubs = _load_named_hub_launcher_states()
                    hub_idx = next(
                        index
                        for index, named in enumerate(hubs)
                        if named.hub.hub_id == renamed.hub_id
                    )
                    notice = f"已重命名为 {renamed.name}"
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    StopIteration,
                ) as exc:
                    notice = str(exc)
        elif ch == ord("a") and not hub_focus:
            if view:
                provider_id = view[idx]
                previous_alias = meta[provider_id].get("alias")
                had_alias = "alias" in meta[provider_id]
                changed, notice = _edit_alias(win, provider_id, meta)
                if changed and not save_config(cfg):
                    if had_alias:
                        meta[provider_id]["alias"] = previous_alias
                    else:
                        meta[provider_id].pop("alias", None)
                    notice = "无法保存别名，已撤销本次修改"
        elif ch == ord("x"):
            if not hub_focus and view:
                provider_id = view[idx]
                name = _provider_meta_label(meta, provider_id)
                nowh = meta[provider_id].get("hidden")
                verb = "恢复显示" if nowh else "隐藏"
                ok = _confirm(win, f"{verb} {name}?")
                if ok:
                    meta[provider_id]["hidden"] = not nowh
                    if save_config(cfg):
                        preferred = provider_id
                        view = _build_view(cfg, db_ids, mru, show_hidden)
                        idx = _initial_index(view, mru, preferred)
                    else:
                        meta[provider_id]["hidden"] = nowh
                        notice = "无法保存隐藏状态，已撤销本次修改"
        elif ch == ord("h") and not hub_focus:  # 切换「显示隐藏项」
            preferred = view[idx] if view else None
            show_hidden = not show_hidden
            view = _build_view(cfg, db_ids, mru, show_hidden)
            idx = _initial_index(view, mru, preferred)
        elif ch == ord("?"):
            help_open = not help_open
        elif ch in (10, 13, curses.KEY_ENTER):
            if hub_focus and hubs:
                named = hubs[hub_idx]
                if named.error is not None:
                    notice = f"{named.hub.name}: {named.error}"
                elif named.state is None:
                    setup_outcome = _hub_setup_wizard(win, named.hub)
                    if setup_outcome == "quit":
                        return None
                    selected_id = named.hub.hub_id
                    hubs = _load_named_hub_launcher_states()
                    hub_idx = next(
                        (
                            index
                            for index, item in enumerate(hubs)
                            if item.hub.hub_id == selected_id
                        ),
                        0,
                    )
                    notice = (
                        "配置完成，Enter 启动"
                        if setup_outcome == "completed"
                        else "Hub 尚未配置"
                    )
                else:
                    return HubLaunch(
                        slot=named.state.status.launch_slot,
                        hub_id=(
                            named.hub.hub_id if HUB_CATALOG_ENABLED else None
                        ),
                    )
            elif view:
                return view[idx]
        elif ch in (ord("\t"), ord("m"), curses.KEY_RIGHT) and hub_focus and hubs:
            named = hubs[hub_idx]
            if named.error is not None:
                notice = f"{named.hub.name}: {named.error}"
            elif named.state is None:
                setup_outcome = _hub_setup_wizard(win, named.hub)
                if setup_outcome == "quit":
                    return None
                selected_id = named.hub.hub_id
                hubs = _load_named_hub_launcher_states()
                hub_idx = next(
                    (
                        index
                        for index, item in enumerate(hubs)
                        if item.hub.hub_id == selected_id
                    ),
                    0,
                )
                notice = (
                    "配置完成，Enter 启动"
                    if setup_outcome == "completed"
                    else "Hub 尚未配置"
                )
                continue
            outcome, launch = _hub_workspace(
                win,
                named.state.status,
                list(named.state.options),
                initial_slot=named.state.status.launch_slot,
                hub=named.hub if HUB_CATALOG_ENABLED else None,
            )
            if outcome == "launch" and launch is not None:
                return launch
            if outcome == "quit":
                return None
            # "back": refresh mutations made in the workspace before redraw.
            selected_id = named.hub.hub_id
            hubs = _load_named_hub_launcher_states()
            hub_idx = next(
                (
                    index
                    for index, item in enumerate(hubs)
                    if item.hub.hub_id == selected_id
                ),
                0,
            )
        elif ch in (27, ord("q")):
            return None
        idx = 0 if not view else max(0, min(idx, len(view) - 1))
        _draw_launcher(
            win,
            cfg,
            view,
            idx,
            show_hidden,
            mru,
            show_brand=True,
            help_open=help_open,
            notice=notice,
            hub_focus=hub_focus,
            hubs=hubs,
            hub_idx=hub_idx,
        )


def _launcher_session(win, cfg, db_ids):
    """Run one chooser and remove its full-screen UI before curses restores."""
    try:
        return _launcher_main(win, cfg, db_ids)
    finally:
        try:
            win.erase()
            win.refresh()
        except (AttributeError, curses.error):
            pass


def run_tui_launcher():
    """打开 TUI 启动器。返回 ('launch', id) | ('quit', None) | ('no-tui', None)。"""
    rows = db_claude_rows()
    providers = [_provider_from_row(row) for row in rows]
    db_ids = {str(provider["id"]) for provider in providers}
    cfg = load_config()
    changed = sync_config(cfg, providers)
    if changed:
        save_config(cfg)
    if curses is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return ("no-tui", None)
    terminal = shutil.get_terminal_size(fallback=(80, 24))
    if not _tui_size_supported(terminal.lines, terminal.columns):
        return ("no-tui", None)
    try:
        result = curses.wrapper(_launcher_session, cfg, db_ids)
    except Exception as exc:
        print(f"[claude1] 图形界面无法启动({exc})", file=sys.stderr)
        return ("no-tui", None)
    if result is None:
        return ("quit", None)
    if isinstance(result, HubLaunch):
        if result.slot is not None:
            payload = (
                (result.hub_id, result.slot)
                if result.hub_id is not None
                else result.slot
            )
            return ("hub-slot", payload)
        if result.option is None:
            raise RuntimeError("Hub 启动结果缺少槽位或模型")
        payload = (
            (result.hub_id, result.option.selector)
            if result.hub_id is not None
            else result.option.selector
        )
        return ("hub", payload)
    return ("launch", result)


def record_backend(kind: str, provider: str | None = None) -> None:
    """Record the last actual launch without changing the sticky shell route."""
    try:
        payload: dict = {"backend": kind, "at": time.time()}
        if provider:
            payload["provider"] = provider
        _atomic_private_write(
            BACKEND_STATE,
            json.dumps(payload, ensure_ascii=False),
        )
    except OSError:
        pass


def _atomic_write_sticky(kind: str) -> None:
    _atomic_private_write(BACKEND_STICKY, kind + "\n")


def set_sticky(word: str) -> int:
    """`claude1 use <后端>`：只设置粘性后端、不启动会话。"""
    kind = BACKEND_ALIASES.get(word.lower(), word.lower())
    if kind not in ("anyrouter", "current", "direct", "hub"):
        print(
            f"[claude1] 未知后端: {word}"
            "（可用: any · cc/current · direct · hub）",
            file=sys.stderr,
        )
        return 1
    try:
        _atomic_write_sticky(kind)
    except OSError as exc:
        print(f"[claude1] 无法写入粘性后端: {exc}", file=sys.stderr)
        return 1
    if os.environ.get("CLAUDE1_STICKY_INTEGRATION") != "1":
        print(
            f"[claude1] 已保存粘性后端 = {kind}，但当前 shell 未启用普通 claude 路由。"
        )
        print(
            "[claude1] 如需让普通 claude 读取该选择，请运行 "
            "`./install.sh --enable-sticky` 后重新 source ~/.zshrc。"
        )
        return 0
    if kind == "hub":
        print(
            "[claude1] 粘性后端 = hub —— 之后普通 claude 走多渠道网关，"
            "直到再次显式切换"
        )
    else:
        print(f"[claude1] 粘性后端 = {kind} —— 普通 claude 走 CC-Switch（{kind}），直到再切")
    return 0


def parse_args(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """拆成 (backend, provider_hint, claude_args)。

    backend: 'anyrouter' | 'current' | 'direct' | 'hub' | None
    provider_hint: 匹配 CC-Switch provider 的名称、别名或 id（None => 弹菜单）
    claude_args: 展开后的 overlay + 其余原样透传给 claude
    """
    backend: str | None = None
    hint: str | None = None
    claude_args: list[str] = []
    first_positional = True
    passthrough = False
    ambiguous_hub_positional = False

    def select_backend(requested: str) -> None:
        nonlocal backend
        if backend is not None and backend != requested:
            raise RuntimeError(
                f"不能同时指定后端 {backend} 与 {requested}；请二选一"
            )
        backend = requested

    for arg in argv:
        if passthrough:
            claude_args.append(arg)
            continue
        if ambiguous_hub_positional:
            ambiguous_hub_positional = False
            if not arg.startswith("-"):
                raise RuntimeError(
                    "--hub 后的位置参数含义不明确；提示词请放在 -- 后"
                )
        if arg == "--":
            claude_args.append(arg)
            passthrough = True
            continue
        low = arg.lower()
        if not arg.startswith("-"):
            if first_positional:
                first_positional = False
                if low in BACKEND_ALIASES:
                    select_backend(BACKEND_ALIASES[low])
                elif backend is None:
                    hint = arg
                else:
                    claude_args.append(arg)
                continue
            claude_args.append(arg)
            continue
        # claude1 自己理解的 overlay 开关
        if low == "--notion":
            if NOTION_MCP.exists():
                claude_args += ["--mcp-config", str(NOTION_MCP)]
            else:
                print(f"[claude1] 警告: notion 配置不存在 {NOTION_MCP}", file=sys.stderr)
        elif low in ("--any", "--anyrouter"):
            if hint is not None:
                raise RuntimeError("不能同时指定 provider 与 --any；请二选一")
            select_backend("anyrouter")
        elif low in ("--current", "--cc"):
            if hint is not None:
                raise RuntimeError("不能同时指定 provider 与 --current；请二选一")
            select_backend("current")
        elif low == "--direct":
            if hint is not None:
                raise RuntimeError("不能同时指定 provider 与 --direct；请二选一")
            select_backend("direct")
        elif low == "--hub":
            if hint is not None:
                raise RuntimeError("不能同时指定 provider 与 --hub；请二选一")
            select_backend("hub")
            ambiguous_hub_positional = True
        else:
            claude_args.append(arg)
    return backend, hint, claude_args


def exec_settings_backend(settings_path: Path, label: str, claude_args: list[str]) -> int:
    if not settings_path.exists():
        raise RuntimeError(f"{label} 配置不存在: {settings_path}")
    record_backend(label)
    print(f"[claude1] 后端: {label} ({settings_path.name})")
    claude_bin = resolve_claude_bin()
    return _run_claude(
        [str(claude_bin), "--settings", str(settings_path), *claude_args],
        env=claude_child_env(),
    )


def exec_plain_claude(label: str, claude_args: list[str]) -> int:
    record_backend(label)
    note = "CC-Switch 当前 provider" if label == "current" else "裸 claude"
    print(f"[claude1] 后端: {label} ({note})")
    claude_bin = resolve_claude_bin()
    # ``direct`` deliberately is bare Claude: do not discard an explicitly
    # inherited ANTHROPIC_* credential or other caller-selected environment.
    return _run_claude([str(claude_bin), *claude_args], env=os.environ.copy())


def launch_with_settings(settings: dict, claude_args: list[str]) -> int:
    """Launch Claude with one private settings overlay and a matching child env.

    Claude applies user settings after its inherited process environment, so the
    selected credential must also remain in this higher-precedence ``--settings``
    overlay.  Otherwise CC Switch's global current credential can replace it.
    """
    if TEMP_DIR is not None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="claude1_",
        suffix=".json",
        dir=str(TEMP_DIR) if TEMP_DIR is not None else None,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        os.chmod(tmp_path, 0o600)
        claude_bin = resolve_claude_bin()
        return _run_claude(
            [str(claude_bin), "--settings", tmp_path, *claude_args],
            env=claude_child_env(settings),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _provider_models(
    settings: dict,
    *,
    include_placeholder: bool = True,
) -> list[str]:
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    names: list[str] = []
    for key in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        value = env.get(key)
        if isinstance(value, str) and value and value not in names:
            names.append(value)
    if names or not include_placeholder:
        return names
    return ["claude1-provider-model"]


def _bridge_child_env(
    *, config: Path, log: Path, port: int, local_token: str
) -> dict[str, str]:
    child = {
        "HOME": str(HOME),
        "PATH": os.environ.get("PATH", os.defpath),
        "CLAUDE_HUB_CONFIG": str(config),
        "CLAUDE_HUB_DB": str(DB_PATH),
        "CLAUDE_HUB_LOG": str(log),
        "CLAUDE_HUB_PORT": str(port),
        "CLAUDE_HUB_LOCAL_TOKEN": local_token,
        "CLAUDE1_ACCOUNT_POOL_CONFIG": str(ACCOUNT_POOL_CONFIG),
        "CLAUDE1_ACCOUNT_POOL_STATE": str(ACCOUNT_POOL_STATE),
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            child[key] = value
    return child


def launch_with_protocol_bridge(
    provider: dict,
    settings: dict,
    api_format: str,
    claude_args: list[str],
    *,
    transport: dict | None = None,
) -> int:
    """Run one isolated Hub for protocol or transport routing, then remove it.

    This preserves claude1's session-isolation contract: the CC Switch current
    provider and its shared proxy are never changed, so concurrent sessions can
    select different wire formats safely.
    """
    if not HUB_SCRIPT.is_file():
        raise RuntimeError(
            f"{api_format} 渠道需要协议桥，但 Hub 脚本不存在: {HUB_SCRIPT}"
        )
    if TEMP_DIR is not None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="claude1-bridge-",
        dir=str(TEMP_DIR) if TEMP_DIR is not None else None,
    ) as raw_dir:
        runtime = Path(raw_dir)
        config_path = runtime / "hub.json"
        log_path = runtime / "hub.log"
        listener = _reserve_loopback_port()
        port = int(listener.getsockname()[1])
        local_token = secrets.token_urlsafe(32)
        if transport is None:
            transport = provider_transport_config(provider, settings)
        config = {
            "version": 1,
            "port": port,
            "local_token_env": "CLAUDE_HUB_LOCAL_TOKEN",
            "default_channel": "direct",
            "transport": transport,
            "channels": {
                "direct": {
                    "provider": f"id:{provider['id']}",
                    "api_format": api_format,
                    "models": _provider_models(settings),
                }
            },
        }
        _atomic_private_write(
            config_path,
            json.dumps(config, ensure_ascii=False, separators=(",", ":")),
        )

        with listener:
            process = _spawn_hub_process(
                log_path,
                _bridge_child_env(
                    config=config_path,
                    log=log_path,
                    port=port,
                    local_token=local_token,
                ),
                listener,
            )
            try:
                deadline = time.monotonic() + _hub_start_timeout()
                while time.monotonic() < deadline:
                    if hub_healthy(port, local_token):
                        break
                    return_code = process.poll()
                    if return_code is not None:
                        raise RuntimeError(
                            f"协议桥提前退出（状态 {return_code}），"
                            f"日志: {log_path}"
                        )
                    time.sleep(
                        min(0.25, max(0.0, deadline - time.monotonic()))
                    )
                else:
                    raise RuntimeError(f"协议桥启动超时，日志: {log_path}")

                # The child now accepts on the inherited descriptor; retaining
                # the parent duplicate would mask an unexpected child exit.
                listener.close()
                bridged = json.loads(json.dumps(settings))
                env = bridged.setdefault("env", {})
                env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
                env["ANTHROPIC_AUTH_TOKEN"] = local_token
                env.pop("ANTHROPIC_API_KEY", None)
                env["NO_PROXY"] = "127.0.0.1,localhost"
                env["no_proxy"] = "127.0.0.1,localhost"
                reason = (
                    f"协议适配: Anthropic Messages ↔ {api_format}"
                    if api_format != "anthropic"
                    else f"传输路由: {transport['mode']}"
                )
                print(f"[claude1] {reason} (隔离端口 {port})")
                return launch_with_settings(bridged, claude_args)
            finally:
                _stop_spawned_process(process)


def _extract_hub_model(claude_args: list[str]) -> tuple[str | None, list[str]]:
    """Consume claude1's Hub-only --model option without touching other args."""
    requested: str | None = None
    forwarded: list[str] = []
    index = 0
    while index < len(claude_args):
        arg = claude_args[index]
        if arg == "--":
            forwarded.extend(claude_args[index:])
            break
        if arg == "--model":
            if (
                index + 1 >= len(claude_args)
                or claude_args[index + 1].startswith("-")
            ):
                raise RuntimeError("hub --model 后需要 <渠道,模型>")
            value = claude_args[index + 1]
            index += 2
        elif arg.startswith("--model="):
            value = arg.split("=", 1)[1]
            index += 1
        else:
            forwarded.append(arg)
            index += 1
            continue
        if requested is not None:
            raise RuntimeError("hub --model 只能指定一次")
        requested = value
    return requested, forwarded


def _extract_hub_slot(claude_args: list[str]) -> tuple[str | None, list[str]]:
    """Consume claude1's Hub-only --slot option without touching other args."""
    requested: str | None = None
    forwarded: list[str] = []
    index = 0
    while index < len(claude_args):
        arg = claude_args[index]
        if arg == "--":
            forwarded.extend(claude_args[index:])
            break
        if arg == "--slot":
            if (
                index + 1 >= len(claude_args)
                or claude_args[index + 1].startswith("-")
            ):
                raise RuntimeError("hub --slot 后需要 fable、opus、sonnet 或 haiku")
            value = claude_args[index + 1]
            index += 2
        elif arg.startswith("--slot="):
            value = arg.split("=", 1)[1]
            index += 1
        else:
            forwarded.append(arg)
            index += 1
            continue
        if requested is not None:
            raise RuntimeError("hub --slot 只能指定一次")
        requested = value.strip().casefold()
    return requested, forwarded


def _normalize_hub_model(value: str, channels: dict) -> str:
    raw = value.strip()
    if raw.startswith("anthropic/"):
        raw = raw[len("anthropic/") :]
    alias, separator, model = raw.partition(",")
    alias, model = alias.strip().lower(), model.strip()
    if not separator or not alias or not model:
        raise RuntimeError("hub 模型格式应为 <渠道,模型>，例如 fast,sonnet")
    if alias not in channels:
        available = "、".join(str(item) for item in channels)
        raise RuntimeError(f"hub 中没有渠道 '{alias}'；可用渠道: {available}")
    channel = channels[alias]
    models = channel.get("models") if isinstance(channel, dict) else None
    if not isinstance(models, list) or model not in models:
        raise RuntimeError(f"hub 渠道 '{alias}' 中没有模型 '{model}'")
    return f"{alias},{model}"


def _resume_session_selector(
    claude_args: list[str],
    channels: dict,
) -> str | None:
    """Return the Hub selector recorded by a resumed local session."""
    session_id: str | None = None
    use_latest = False
    index = 0
    while index < len(claude_args):
        arg = claude_args[index]
        if arg == "--":
            break
        if arg in ("--continue", "-c"):
            use_latest = True
        elif arg in ("--resume", "-r"):
            if index + 1 < len(claude_args) and not claude_args[index + 1].startswith("-"):
                session_id = claude_args[index + 1]
                index += 1
        elif arg.startswith("--resume="):
            session_id = arg.split("=", 1)[1]
        index += 1

    if session_id is None and not use_latest:
        return None

    project_key = str(Path.cwd().resolve()).replace("/", "-")
    transcript_dir = HOME / ".claude" / "projects" / project_key
    if session_id is not None:
        transcript = transcript_dir / f"{session_id}.jsonl"
        if not transcript.is_file():
            return None
    else:
        transcripts = list(transcript_dir.glob("*.jsonl"))
        if not transcripts:
            return None
        transcript = max(transcripts, key=lambda path: path.stat().st_mtime)

    last_model: str | None = None
    with transcript.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            message = entry.get("message") if entry.get("type") == "assistant" else None
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model:
                last_model = model
    if last_model is None:
        return None

    alias, separator, model = last_model.partition(",")
    alias, model = alias.strip().lower(), model.strip()
    if separator and alias in channels and model:
        return f"{alias},{model}"

    matches = [
        f"{channel_alias},{last_model}"
        for channel_alias, channel in channels.items()
        if isinstance(channel, dict)
        and isinstance(channel.get("models"), list)
        and last_model in channel["models"]
    ]
    return matches[0] if len(matches) == 1 else None


def _hub_model_slots(
    hub_cfg: dict,
    channels: dict,
    fallback_model: str,
) -> dict[str, str]:
    """Resolve native Claude model slots to Hub selectors."""
    raw_slots = hub_cfg.get("model_slots")
    if raw_slots is None:
        return {tier: fallback_model for tier in MODEL_SLOT_TIERS}
    if not isinstance(raw_slots, dict):
        raise RuntimeError("hub 配置中的 model_slots 必须是对象")
    slots: dict[str, str] = {}
    for tier in MODEL_SLOT_TIERS:
        raw = raw_slots.get(tier.casefold(), fallback_model)
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"hub model_slots.{tier.casefold()} 无效")
        slots[tier] = _normalize_hub_model(raw, channels)
    return slots


def _unique_slot_for_selector(selector: str, slot_models: dict[str, str]) -> str | None:
    matches = [slot.casefold() for slot, model in slot_models.items() if model == selector]
    return matches[0] if len(matches) == 1 else None


def exec_hub(
    claude_args: list[str],
    *,
    hub_id: str | None = None,
) -> int:
    """Launch one Claude session through the isolated multi-channel hub."""
    requested_model, claude_args = _extract_hub_model(claude_args)
    requested_slot, claude_args = _extract_hub_slot(claude_args)
    if requested_model is not None and requested_slot is not None:
        raise RuntimeError("hub --model 与 --slot 不能同时指定")
    try:
        hub_ref = resolve_hub_ref(hub_id, migrate=True)
        if hub_ref.state == "setup":
            raise RuntimeError(
                f"Hub {hub_ref.name} 尚未配置；请打开 claude1 完成四槽映射"
            )
        hub_cfg = load_hub_config(migrate=True, hub=hub_ref)
    except RuntimeError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"hub 配置无法读取: {exc}") from exc

    port = _hub_port(hub_cfg)
    token_env = _hub_token_env_name(hub_cfg)
    token = _hub_local_token(hub_cfg)
    channels = hub_cfg.get("channels")
    default_channel = hub_cfg.get("default_channel")
    if not isinstance(channels, dict) or not channels:
        raise RuntimeError("hub 配置缺少 channels")
    channel = channels.get(default_channel)
    models = channel.get("models") if isinstance(channel, dict) else None
    if (
        not isinstance(default_channel, str)
        or not isinstance(models, list)
        or not models
        or not isinstance(models[0], str)
        or not models[0]
    ):
        raise RuntimeError("hub 配置中的 default_channel 或默认模型无效")

    fallback_model = f"{default_channel},{models[0]}"
    slot_models = _hub_model_slots(hub_cfg, channels, fallback_model)
    launch_slot = hub_cfg["launch_slot"]
    efforts = hub_cfg["effort_by_slot"]
    selected_slot: str | None = None
    if requested_slot is not None:
        if requested_slot not in HUB_SLOT_ORDER:
            raise RuntimeError("hub --slot 必须是 fable、opus、sonnet 或 haiku")
        selected_slot = requested_slot
        main_model = slot_models[requested_slot.upper()]
    elif requested_model is not None:
        main_model = _normalize_hub_model(requested_model, channels)
        selected_slot = _unique_slot_for_selector(main_model, slot_models)
    else:
        resume_model = _resume_session_selector(claude_args, channels)
        if resume_model is not None:
            main_model = resume_model
            if slot_models[launch_slot.upper()] == main_model:
                selected_slot = launch_slot
            else:
                selected_slot = _unique_slot_for_selector(main_model, slot_models)
        else:
            selected_slot = launch_slot
            main_model = slot_models[launch_slot.upper()]
    effort_level = efforts[selected_slot] if selected_slot is not None else "high"
    instance_id = hub_cfg.get("instance_id")
    runtime_hub = hub_ref if HUB_CATALOG_ENABLED else None
    if runtime_hub is None and not isinstance(instance_id, str):
        port = ensure_hub(port, token=token, token_env=token_env)
    else:
        port = ensure_hub(
            port,
            token=token,
            token_env=token_env,
            hub=runtime_hub,
            instance_id=instance_id if isinstance(instance_id, str) else None,
        )
    settings_env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": main_model,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for tier, selector in slot_models.items():
        model_key = f"ANTHROPIC_DEFAULT_{tier}_MODEL"
        alias, _, upstream_model = selector.partition(",")
        settings_env[model_key] = selector
        settings_env[f"{model_key}_NAME"] = upstream_model
        settings_env[f"{model_key}_DESCRIPTION"] = f"Claude-Hub · {alias}"
        # Capabilities describe what a custom model slot can run. The persisted
        # effort controls only this session's starting level, not the /model UI.
        settings_env[f"{model_key}_SUPPORTED_CAPABILITIES"] = (
            HUB_MODEL_SLOT_CAPABILITIES
        )
    _seal_model_slots(settings_env)
    settings = {"env": settings_env}
    settings["effortLevel"] = effort_level
    record_backend("hub", hub_ref.hub_id)
    aliases = ", ".join(str(alias) for alias in channels)
    print(
        f"[claude1] 后端: {hub_ref.name} (127.0.0.1:{port}, 默认 {main_model})"
    )
    print(f"[claude1] 会话内切渠道: /model 别名,模型   渠道: {aliases}")
    return launch_with_settings(settings, claude_args)


CLAUDE1_USAGE = f"""claude1 {VERSION} — 为本次 Claude Code 会话选择渠道

用法:
  claude1                              打开渠道选择器
  claude1 <名称/别名/id:ID> [Claude 参数] 直接启动一个渠道
  claude1 hub [--slot 槽位 | --model 渠道,模型]
                                       进入可用 /model 热切换的 Hub
  claude1 list [--all]                 查看渠道，不启动 Claude
  claude1 accounts ...                 将同一上游的多个 CC Switch key 组成账号池
  claude1 doctor [--fix]               检查本机配置；--fix 清理子代理模型固定值
  claude1 usage [--day|--week|--month] 查看 token 用量与缓存命中率曲线
  claude1 use <backend>                显式设置普通 claude 的粘性后端
  claude1 --help                       显示本帮助

快捷键:
  ↑↓ / jk 移动 · Enter 启动 · 1–9/0 数字直达 · ? 更多操作 · q 退出
  Hub 首页：←/→/e effort · a 新增渠道 · Tab/m 完整管理

默认启动只影响本次会话，不修改普通 claude 或 CC Switch 当前渠道。
"""


def cli_list_providers(show_all: bool = False) -> int:
    rows = db_claude_rows()
    providers = [_provider_from_row(row) for row in rows]
    by_id = {str(provider["id"]): provider for provider in providers}
    labels = _provider_labels(providers)
    cfg = load_config()
    changed = sync_config(cfg, providers)
    if changed:
        save_config(cfg)
    provider_ids = [
        provider_id
        for provider_id, meta in cfg["providers"].items()
        if provider_id in by_id and (show_all or not meta.get("hidden"))
    ]
    if not provider_ids:
        if providers and not show_all:
            print("claude1: 所有 Claude 渠道都已隐藏；运行 `claude1 list --all` 查看")
        else:
            print("claude1: 没有可显示的 CC Switch Claude 渠道")
        return 1

    recent = _recent_name(provider_ids, load_mru())
    print("claude1 渠道（顺序与选择器一致）\n")
    for index, provider_id in enumerate(provider_ids, 1):
        meta = cfg["providers"][provider_id]
        details: list[str] = []
        if meta.get("alias"):
            details.append(f"别名 {meta['alias']}")
        if provider_id == recent:
            details.append("最近")
        if meta.get("hidden"):
            details.append("已隐藏")
        suffix = f"  {' · '.join(details)}" if details else ""
        print(f"  {index:>2}  {labels[provider_id]}{suffix}")
    print(
        f"\n共 {len(provider_ids)} 个；运行 `claude1 <名称、别名或 id:ID>` 可直接启动。"
    )
    return 0


def _account_provider_by_hint(providers: list[dict], hint: str) -> dict:
    matches, exact = match_providers(providers, hint)
    if len(matches) == 1:
        return matches[0]
    labels = _provider_labels(providers)
    if len(matches) > 1:
        rendered = "、".join(labels[str(provider["id"])] for provider in matches)
        qualifier = "名称冲突" if exact else "匹配不唯一"
        raise RuntimeError(f"账号 provider {qualifier}: {rendered}；请使用 id:ID")
    raise RuntimeError(f"找不到账号 provider: {hint}")


def _account_ref_by_hint(
    providers: list[dict],
    hint: str,
) -> tuple[str, dict | None]:
    """Resolve a live provider, or retain an explicit orphaned stable id."""
    try:
        provider = _account_provider_by_hint(providers, hint)
    except RuntimeError:
        if (
            isinstance(hint, str)
            and hint.startswith("id:")
            and len(hint) > 3
            and not any(ord(char) < 32 or ord(char) == 127 for char in hint)
        ):
            return hint, None
        raise
    return f"id:{provider['id']}", provider


def _account_add_options(args: list[str]) -> tuple[str, str, int, int]:
    if len(args) < 2:
        raise RuntimeError(
            "用法: claude1 accounts add <主provider> <账号provider> "
            "[--weight N] [--priority N]"
        )
    primary, member = args[0], args[1]
    weight, priority = 1, 0
    index = 2
    while index < len(args):
        option = args[index]
        if option not in ("--weight", "--priority") or index + 1 >= len(args):
            raise RuntimeError(f"accounts add 不支持参数: {option}")
        try:
            value = int(args[index + 1])
        except ValueError:
            raise RuntimeError(f"{option} 必须是整数") from None
        if option == "--weight":
            weight = value
        else:
            priority = value
        index += 2
    return primary, member, weight, priority


def cli_accounts(args: list[str]) -> int:
    """Manage non-secret groups of existing CC Switch provider accounts."""
    action = args[0].casefold() if args else "list"
    providers = [_provider_from_row(row) for row in db_claude_rows()]
    by_selector = {f"id:{provider['id']}": provider for provider in providers}
    labels = _provider_labels(providers)
    store = PoolConfigStore(ACCOUNT_POOL_CONFIG)
    scheduler = AccountPool(ACCOUNT_POOL_CONFIG, ACCOUNT_POOL_STATE)

    if action == "list":
        if len(args) > 2:
            raise RuntimeError("用法: claude1 accounts list [provider]")
        definitions = scheduler.definitions()
        if len(args) == 2:
            selector, _selected = _account_ref_by_hint(providers, args[1])
            definitions = {
                selector: definitions[selector]
            } if selector in definitions else {}
        if not definitions:
            print(
                "claude1: 尚未配置账号池。先在 CC Switch 为每个 key 建独立 "
                "provider，再运行 `claude1 accounts add <主provider> <账号provider>`。"
            )
            return 0
        for primary, definition in definitions.items():
            primary_provider = by_selector.get(primary)
            title = (
                labels.get(str(primary_provider["id"]), primary)
                if primary_provider is not None
                else primary
            )
            print(f"{title}  · {definition.strategy}")
            try:
                records, candidates, _credentials = _account_pool_directory(
                    primary_provider or {"id": primary.removeprefix("id:")},
                    definition,
                    providers,
                )
                statuses = {
                    item.member: item
                    for item in scheduler.inspect(primary, candidates)
                }
            except (RuntimeError, AccountPoolError):
                records, statuses = by_selector, {}
            for member in definition.members:
                record = records.get(member.selector)
                name = (
                    labels.get(str(record["id"]), member.selector)
                    if isinstance(record, dict) and "id" in record
                    else member.selector
                )
                state = statuses.get(member.selector)
                state_text = state.state if state is not None else "配置不可用"
                if state is not None and state.retry_after is not None:
                    state_text += f" {state.retry_after}s"
                print(
                    f"  - {name} · priority={member.priority} "
                    f"weight={member.weight} · {state_text}"
                )
        return 0

    if action in ("add", "set"):
        primary_hint, member_hint, weight, priority = _account_add_options(args[1:])
        primary_provider = _account_provider_by_hint(providers, primary_hint)
        member_provider = _account_provider_by_hint(providers, member_hint)
        primary = f"id:{primary_provider['id']}"
        member = f"id:{member_provider['id']}"
        if primary == member and action == "add":
            raise RuntimeError("账号池成员必须是另一个 CC Switch provider")
        primary_key, primary_token, primary_base = _provider_account_credential(
            primary_provider
        )
        member_key, member_token, member_base = _provider_account_credential(
            member_provider
        )
        if not primary_token or not member_token:
            raise RuntimeError("主 provider 与账号 provider 都必须有独立凭证")
        if primary_base != member_base or primary_key != member_key:
            raise RuntimeError("两个账号必须使用相同上游 URL 和相同凭证类型")
        member_fingerprint = credential_fingerprint(member_token)
        definition = scheduler.definition(primary)
        existing_members = definition.members if definition is not None else ()
        for existing in existing_members:
            if not existing.enabled or existing.selector == member:
                continue
            existing_provider = by_selector.get(existing.selector)
            if existing_provider is None:
                continue
            _existing_key, existing_token, _existing_base = (
                _provider_account_credential(existing_provider)
            )
            if (
                existing_token
                and credential_fingerprint(existing_token) == member_fingerprint
            ):
                raise RuntimeError("两个账号 provider 实际使用了同一个凭证")
        if (
            definition is None
            and primary != member
            and credential_fingerprint(primary_token) == member_fingerprint
        ):
            raise RuntimeError("两个账号 provider 实际使用了同一个凭证")
        store.upsert_member(
            primary,
            member,
            weight=weight,
            priority=priority,
        )
        print(
            f"[claude1] 已加入账号池: {primary_provider['name']} ← "
            f"{member_provider['name']} (priority={priority}, weight={weight})"
        )
        return 0

    if action == "policy":
        if len(args) != 3:
            raise RuntimeError(
                "用法: claude1 accounts policy <主provider> <round-robin|weighted>"
            )
        primary, _primary_provider = _account_ref_by_hint(providers, args[1])
        strategy = args[2].replace("-", "_").casefold()
        store.set_strategy(primary, strategy)
        print(f"[claude1] 账号池策略已设为 {strategy}")
        return 0

    if action == "remove":
        if len(args) != 3:
            raise RuntimeError("用法: claude1 accounts remove <主provider> <账号provider>")
        primary, _primary_provider = _account_ref_by_hint(providers, args[1])
        member, member_provider = _account_ref_by_hint(providers, args[2])
        store.remove_member(primary, member)
        scheduler.reset(primary, member)
        member_label = member_provider["name"] if member_provider is not None else member
        print(f"[claude1] 已从账号池移除: {member_label}")
        return 0

    if action == "delete":
        if len(args) != 2:
            raise RuntimeError("用法: claude1 accounts delete <主provider>")
        primary, _primary_provider = _account_ref_by_hint(providers, args[1])
        deleted = store.delete_pool(primary)
        scheduler.reset(primary)
        print("[claude1] 账号池已删除" if deleted else "[claude1] 账号池不存在")
        return 0

    if action == "reset":
        if len(args) not in (2, 3):
            raise RuntimeError("用法: claude1 accounts reset <主provider> [账号provider]")
        primary, _primary_provider = _account_ref_by_hint(providers, args[1])
        member = None
        if len(args) == 3:
            member, _member_provider = _account_ref_by_hint(providers, args[2])
        changed = scheduler.reset(primary, member)
        print(f"[claude1] 已重置 {changed} 条账号运行状态")
        return 0

    raise RuntimeError(
        "accounts 子命令: list、add、set、policy、remove、delete、reset"
    )


def _usage_window(args: list[str]) -> tuple[str, int] | None:
    """解析 --day/--week/--month，返回 (模式, 秒数)；参数非法返回 None。"""
    mode = "day"
    for arg in args:
        if arg in ("--day", "--week", "--month"):
            mode = arg[2:]
        else:
            return None
    span = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}[mode]
    return mode, span


def _load_usage_rows(path: Path, since: float) -> list[dict]:
    """Load the current and one rotated usage file without following special paths."""
    rows: list[dict] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        fd: int | None = None
        try:
            expected = candidate.lstat()
            if not stat.S_ISREG(expected.st_mode):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(candidate, flags)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (
                expected.st_dev,
                expected.st_ino,
            ):
                continue
            with os.fdopen(fd, encoding="utf-8") as fp:
                fd = None
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    timestamp = row.get("ts")
                    if (
                        isinstance(timestamp, (int, float))
                        and not isinstance(timestamp, bool)
                        and math.isfinite(timestamp)
                        and timestamp >= since
                    ):
                        rows.append(row)
        except (OSError, UnicodeError):
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return rows


def _num(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _sum_field(rows: list[dict], key: str) -> int:
    return sum(_num(row.get(key)) for row in rows)


def _cache_hit_rate(rows: list[dict]) -> float | None:
    """缓存命中率 = 缓存读 / (输入 + 缓存读)。无任何输入返回 None。"""
    cache_read = _sum_field(rows, "cr")
    input_tokens = _sum_field(rows, "in")
    denom = input_tokens + cache_read
    if denom <= 0:
        return None
    return cache_read / denom


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _bucket_rows(mode: str, rows: list[dict], now: float) -> list[tuple[str, list[dict]]]:
    """按模式分桶：day→24 小时，week→7 天，month→30 天。返回 (标签, 行) 有序列表。"""
    if mode == "day":
        count, step, fmt = 24, 3600, "%H:00"
        start = now - 24 * 3600
    elif mode == "week":
        count, step, fmt = 7, 86400, "%m-%d"
        start = now - 7 * 86400
    else:
        count, step, fmt = 30, 86400, "%m-%d"
        start = now - 30 * 86400
    buckets: list[list[dict]] = [[] for _ in range(count)]
    for row in rows:
        idx = int((row.get("ts", 0) - start) // step)
        if 0 <= idx < count:
            buckets[idx].append(row)
    labels = [
        time.strftime(fmt, time.localtime(start + (i + 1) * step))
        for i in range(count)
    ]
    return list(zip(labels, buckets))


# Braille 点阵：每字符 2 列 × 4 行子像素，可画平滑曲线。
_BRAILLE_DOTS = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (0, 3, 0x40),
                 (1, 0, 0x08), (1, 1, 0x10), (1, 2, 0x20), (1, 3, 0x80))
_ANSI = {"reset": "\x1b[0m", "dim": "\x1b[2m",
         "rate": "\x1b[38;5;114m", "tok": "\x1b[38;5;81m", "axis": "\x1b[2m"}


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _scale_chart_index(index: float, count: int, extent: int) -> int:
    """Map one bucket index onto an inclusive chart coordinate range."""
    return round(index * (extent - 1) / max(1, count - 1))


def _braille_chart(
    series: list[tuple[list[float], str]],
    width: int,
    height: int,
) -> tuple[list[list[str]], float, float]:
    """把多条 0-100 归一化曲线画到 Braille 画布。

    返回 (字符网格, ymin, ymax)。series 为 (值列表, 颜色键)。空值用 None 跳过。"""
    ymin, ymax = 0.0, 100.0
    cols, rows_px = width * 2, height * 4
    # grid[y][x] = 颜色键（后到覆盖先到，token 先画、命中率后画盖在上面）
    grid: list[list[str | None]] = [[None] * cols for _ in range(rows_px)]
    span = ymax - ymin or 1.0

    def to_px(idx: int, v: float) -> tuple[int, int]:
        x = _scale_chart_index(idx, len_vals, cols)
        frac = (v - ymin) / span
        y = rows_px - 1 - round(frac * (rows_px - 1))
        return x, y

    for values, key in series:
        len_vals = len(values)
        # 相邻有效点之间线性插值连线，形成连续曲线而非孤立点
        prev: tuple[int, int] | None = None
        for i, v in enumerate(values):
            if v is None:
                prev = None
                continue
            x, y = to_px(i, v)
            if prev is not None:
                px, py = prev
                steps = max(abs(x - px), abs(y - py), 1)
                for s in range(steps + 1):
                    ix = round(px + (x - px) * s / steps)
                    iy = round(py + (y - py) * s / steps)
                    if 0 <= ix < cols and 0 <= iy < rows_px:
                        grid[iy][ix] = key
            elif 0 <= x < cols and 0 <= y < rows_px:
                grid[y][x] = key
            prev = (x, y)
    # 转成 Braille 字符
    out: list[list[str]] = []
    for r in range(height):
        line = []
        for c in range(width):
            bits = 0
            color = None
            for dx, dy, bit in _BRAILLE_DOTS:
                cell = grid[r * 4 + dy][c * 2 + dx]
                if cell is not None:
                    bits |= bit
                    color = cell
            line.append((color, chr(0x2800 + bits) if bits else " "))
        out.append(line)
    return out, ymin, ymax


def _ascii_chart(
    buckets: list[tuple[str, list[dict]]],
    mode: str = "day",
    now: float | None = None,
) -> list[str]:
    """渲染缓存命中率 + token 量双曲线（Braille 平滑线、双 Y 轴、时间刻度）。"""
    now = now if now is not None else time.time()
    color = _supports_color()
    labels = [label for label, _ in buckets]
    n = len(buckets)
    if n == 0:
        return []

    rates = [(_cache_hit_rate(rows) or 0.0) * 100 for _, rows in buckets]
    has_rate = [_cache_hit_rate(rows) is not None for _, rows in buckets]
    totals = [
        _sum_field(rows, "in") + _sum_field(rows, "out")
        + _sum_field(rows, "cr") + _sum_field(rows, "cw")
        for _, rows in buckets
    ]
    max_total = max(totals) or 0

    # 少桶模式（week=7）水平拉伸，曲线更平滑；桶多则一桶一列
    per = max(1, min(4, 28 // n)) if n < 28 else 1
    width = n * per
    height = 9
    tok_series = [(t / max_total * 100) if max_total else 0.0 for t in totals]
    rate_series = [r if h else None for r, h in zip(rates, has_rate)]

    grid, _, _ = _braille_chart(
        [(tok_series, "tok"), (rate_series, "rate")], width, height
    )

    def paint(cell: tuple[str | None, str]) -> str:
        key, ch = cell
        if not color or key is None:
            return ch
        return _ANSI[key] + ch + _ANSI["reset"]

    axis = _ANSI["axis"] if color else ""
    reset = _ANSI["reset"] if color else ""
    lines: list[str] = []

    # 图例
    if color:
        legend = (
            f"  {_ANSI['rate']}⣿{reset} 缓存命中率(右轴 %)   "
            f"{_ANSI['tok']}⣿{reset} token 量(左轴, 峰值 {_fmt_tokens(max_total)})"
        )
    else:
        legend = (f"  * 缓存命中率(右轴 %)   o token 量(左轴, 峰值 "
                  f"{_fmt_tokens(max_total)})")
    lines.append(legend)

    # 图体：左 token 刻度 + 右命中率刻度
    for r in range(height):
        tok_val = max_total * (height - r) / height
        rate_val = 100 * (height - r) / height
        left = f"{_fmt_tokens(int(tok_val)):>6}"
        right = f"{int(rate_val):>3}%"
        body = "".join(paint(c) for c in grid[r])
        lines.append(f"{axis}{left}{reset} │{body}│{axis}{right}{reset}")
    lines.append(f"       └{'─' * width}┘")

    # 时间刻度：标签中心对齐到对应列，最后一个贴右端
    if mode == "day":
        fmt, span = "%H:%M", 24 * 3600
    elif mode == "week":
        fmt, span = "%m-%d", 7 * 86400
    else:
        fmt, span = "%m-%d", 30 * 86400
    start = now - span
    n_ticks = max(2, min(6, width // 9))
    axis_row = list(" " * width)
    marks: list[tuple[int, str]] = []
    for k in range(n_ticks + 1):
        t = start + span * k / n_ticks
        bucket_index = (t - start) / span * (n - 1)
        # Curves use a 2× horizontal Braille grid; map with the same formula,
        # then select the character that owns that subpixel.
        x = _scale_chart_index(bucket_index, n, width * 2) // 2
        # day 窗口首尾都是同一时刻（差 24h），用相对标签区分
        if mode == "day" and k == 0:
            lab = "-24h"
        elif mode == "day" and k == n_ticks:
            lab = "现在"
        else:
            lab = time.strftime(fmt, time.localtime(t))
        marks.append((x, lab))
    cursor = -99
    for idx, (x, lab) in enumerate(marks):
        # 中心对齐；首标签左对齐、末标签右对齐到边界
        if idx == 0:
            pos = 0
        elif idx == len(marks) - 1:
            pos = max(0, width - len(lab))
        else:
            pos = max(0, x - len(lab) // 2)
        if pos < cursor + 1:
            continue
        for j, ch in enumerate(lab):
            if pos + j < width:
                axis_row[pos + j] = ch
        cursor = pos + len(lab)
    lines.append("        " + "".join(axis_row).rstrip())
    return lines


def cli_usage(args: list[str]) -> int:
    parsed = _usage_window(args)
    if parsed is None:
        print(
            "[claude1] usage 用法: claude1 usage [--day|--week|--month]",
            file=sys.stderr,
        )
        return 2
    mode, span = parsed
    now = time.time()
    usage_paths = [HUB_USAGE]
    if HUB_CATALOG_ENABLED and HUB_CATALOG.is_file():
        try:
            usage_paths = [
                hub.usage_path for hub in list_hub_refs(migrate=False)
            ]
        except (OSError, ValueError, json.JSONDecodeError):
            usage_paths = [HUB_USAGE]
    rows = [
        row
        for usage_path in usage_paths
        for row in _load_usage_rows(usage_path, now - span)
    ]
    rows.sort(key=lambda row: float(row.get("ts", 0)))
    if not rows:
        print(
            "claude1: 还没有经过 hub 的用量记录。\n"
            "用量只在请求经过 claude-hub 网关时统计；"
            "先用 `claude1 hub` 跑几个请求再来看。"
        )
        return 0

    total_in = _sum_field(rows, "in")
    total_out = _sum_field(rows, "out")
    total_cr = _sum_field(rows, "cr")
    total_cw = _sum_field(rows, "cw")
    rate = _cache_hit_rate(rows)
    mode_label = {"day": "最近 24 小时", "week": "最近 7 天", "month": "最近 30 天"}[mode]

    print(f"claude1 用量（{mode_label}）\n")
    print(f"  请求数        {len(rows)}")
    print(f"  输入 token    {_fmt_tokens(total_in)}  ({total_in})")
    print(f"  输出 token    {_fmt_tokens(total_out)}  ({total_out})")
    print(f"  缓存读 token  {_fmt_tokens(total_cr)}  ({total_cr})")
    print(f"  缓存写 token  {_fmt_tokens(total_cw)}  ({total_cw})")
    if rate is None:
        print("  缓存命中率    无数据（输入为 0）")
    else:
        print(f"  缓存命中率    {rate * 100:.1f}%")
    print()
    for line in _ascii_chart(_bucket_rows(mode, rows, now), mode=mode, now=now):
        print(line)
    return 0


def fix_subagent_model_overrides() -> tuple[list[str], list[str], Path]:
    """Back up the CC Switch DB, then remove persisted subagent model pins."""
    backup_path = DB_PATH.with_name(
        f"{DB_PATH.name}.bak-doctor-fix-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(DB_PATH, backup_path)

    changed: list[str] = []
    invalid: list[str] = []
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, name, settings_config FROM providers ORDER BY app_type, sort_index"
            ).fetchall()
            for provider_id, name, raw_settings in rows:
                provider = {
                    "id": provider_id,
                    "name": name,
                    "settings_config": raw_settings,
                }
                try:
                    settings = _provider_settings(provider)
                    env = _provider_environment(provider, settings)
                except RuntimeError:
                    invalid.append(str(name))
                    continue
                if SUBAGENT_MODEL_KEY not in env:
                    continue
                env.pop(SUBAGENT_MODEL_KEY)
                conn.execute(
                    "UPDATE providers SET settings_config = ? WHERE id = ?",
                    (json.dumps(settings, ensure_ascii=False), provider_id),
                )
                changed.append(str(name))
    finally:
        conn.close()
    return changed, invalid, backup_path


def cli_doctor(*, fix: bool = False) -> int:
    """Check local state and optionally remove persisted subagent model pins."""
    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        if level == "FAIL":
            failures += 1
        print(f"  {level:<4} {message}")

    invalid_fixed: list[str] = []
    if fix:
        changed, invalid_fixed, backup_path = fix_subagent_model_overrides()
        print("claude1 doctor --fix（不连接上游）\n")
        print(f"  BACKUP {backup_path}")
        for name in changed:
            print(f"  FIX  {name}: 已移除 {SUBAGENT_MODEL_KEY}")
        for name in invalid_fixed:
            report("FAIL", f"{name}: settings_config 无效，已跳过")
        print()
    else:
        print("claude1 doctor（只读配置与传输检查）\n")
    if sys.version_info >= (3, 11):
        report("OK", f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        report("FAIL", "需要 Python 3.11 或更高版本")

    claude_bin = resolve_claude_bin()
    if claude_bin.exists() and os.access(claude_bin, os.X_OK):
        report("OK", "Claude Code 可执行文件已找到")
    else:
        report("FAIL", "未找到 Claude Code；请安装 claude 或设置 CLAUDE1_CLAUDE_BIN")

    rows: list[dict] = []
    try:
        rows = db_claude_rows()
    except (RuntimeError, sqlite3.Error, OSError):
        report("FAIL", "CC Switch 数据库不存在、不可读或结构不兼容")
    else:
        report("OK", f"CC Switch 数据库只读打开，发现 {len(rows)} 个 Claude 渠道")
        if os.name == "posix" and (DB_PATH.stat().st_mode & 0o077):
            report("FAIL", "CC Switch 数据库含凭证，文件权限应为 0600")
        overrides, invalid_settings = subagent_model_overrides()
        for name in invalid_settings:
            if name not in invalid_fixed:
                report("FAIL", f"{name}: settings_config 无效")
        if overrides:
            report(
                "INFO",
                f"{len(overrides)} 个 provider 固定了子代理模型；"
                "运行 `claude1 doctor --fix` 清理",
            )
        else:
            report("OK", "provider 未固定子代理模型")

    if any(_provider_uses_local_gateway(_provider_from_row(row)) for row in rows):
        if GATEWAY_BIN.is_file() and os.access(GATEWAY_BIN, os.X_OK):
            report("OK", "本地网关可执行文件已找到")
        else:
            report(
                "FAIL",
                "有渠道需要本地网关，但未找到可执行 cliproxyapi；"
                "请安装它或设置 CLAUDE1_GATEWAY_BIN",
            )

    if not fix:
        current_row = next(
            (
                row
                for row in rows
                if "is_current" in row.keys() and bool(row["is_current"])
            ),
            None,
        )
        if current_row is not None:
            current = _provider_from_row(current_row)
            try:
                settings = _provider_settings(current)
                env = _provider_environment(current, settings)
                endpoint = normalize_account_endpoint(
                    env.get("ANTHROPIC_BASE_URL")
                )
                if endpoint:
                    transport = provider_transport_config(current, settings)
                    policy = resolve_transport_policy(endpoint, transport)
                    probes = diagnose_transport_policy(
                        endpoint,
                        policy,
                        timeout=4.0,
                    )
                    any_ok = any(probe.ok for probe in probes)
                    for probe in probes:
                        level = "OK" if probe.ok else ("INFO" if any_ok else "FAIL")
                        report(level, f"{probe.identity}: {probe.detail}")
                    if any_ok:
                        report("OK", f"当前 provider {current['name']} 至少一条传输可用")
            except (RuntimeError, TransportConfigError, OSError, ValueError) as exc:
                report(
                    "FAIL",
                    f"当前 provider 传输诊断失败: {type(exc).__name__}: {exc}",
                )

    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            valid = isinstance(raw, dict) and isinstance(raw.get("providers"), dict)
        except (OSError, UnicodeError, json.JSONDecodeError):
            valid = False
        report("OK" if valid else "FAIL", "claude1 渠道配置可读" if valid else "claude1 渠道配置无效")
    else:
        report("INFO", "首次启动时将按 CC Switch 顺序创建渠道配置")

    if HUB_CONFIG.exists():
        if not HUB_SCRIPT.is_file():
            report("FAIL", "Hub 已配置，但 claude-hub.py 不存在")
        elif shutil.which("uv") is None:
            report("FAIL", "Hub 已配置，但未找到 uv")
        else:
            report("OK", "Hub 脚本与运行器已就绪")
        if os.name == "posix" and (HUB_CONFIG.stat().st_mode & 0o077):
            report("FAIL", "Hub 配置可能含本地凭证，文件权限应为 0600")
    else:
        report("INFO", "Hub 尚未配置；普通 provider 选择仍可使用")

    print(
        "\n结果: "
        + ("可以使用" if failures == 0 else f"需要处理 {failures} 项")
    )
    return 0 if failures == 0 else 1


def launch_provider(
    selected: dict,
    claude_args: list[str],
    *,
    backend_kind: str = "provider",
) -> int:
    settings = build_settings(selected)
    api_format = selected_provider_api_format(selected)
    transport = provider_transport_config(selected, settings)
    account_label = None
    if api_format == "anthropic" and transport["mode"] == "direct":
        settings, account_label = apply_native_account_pool(selected, settings)
    add_anyrouter_observer(settings, selected["name"])
    record_use(str(selected["id"]))
    record_backend(backend_kind, selected["name"])
    if backend_kind == "current":
        print(f"[claude1] 本次使用 CC Switch 当前 provider: {selected['name']}")
    else:
        print(f"[claude1] 本次使用 provider: {selected['name']}")
    if account_label is not None:
        print(f"[claude1] 账号池本次选择: {account_label}")
    if api_format != "anthropic" or transport["mode"] != "direct":
        return launch_with_protocol_bridge(
            selected,
            settings,
            api_format,
            claude_args,
            transport=transport,
        )
    ensure_local_gateway(settings.get("env", {}).get("ANTHROPIC_BASE_URL", ""))
    return launch_with_settings(settings, claude_args)


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("help", "-h", "--help"):
        print(CLAUDE1_USAGE)
        return 0
    if argv and argv[0] in ("version", "--version"):
        print(f"claude1 {VERSION}")
        return 0
    if argv and argv[0] == "list":
        unknown = [arg for arg in argv[1:] if arg != "--all"]
        if unknown:
            print(f"[claude1] list 不支持参数: {' '.join(unknown)}", file=sys.stderr)
            return 2
        return cli_list_providers(show_all="--all" in argv[1:])
    if argv and argv[0] == "doctor":
        doctor_args = argv[1:]
        if doctor_args not in ([], ["--fix"]):
            print("[claude1] doctor 仅支持 --fix", file=sys.stderr)
            return 2
        return cli_doctor(fix=doctor_args == ["--fix"])
    if argv and argv[0] == "usage":
        return cli_usage(argv[1:])
    if argv and argv[0] == "accounts":
        return cli_accounts(argv[1:])
    if argv and argv[0] == "use":
        if len(argv) < 2:
            print(
                "[claude1] 用法: claude1 use <cc|any|direct|hub>",
                file=sys.stderr,
            )
            return 1
        return set_sticky(argv[1])
    # `config`/`--config` 现在就是无参数：直接进 TUI 启动器
    if argv and argv[0] in ("config", "--config"):
        argv = argv[1:]

    # A CC Switch provider may legally have the same name as a positional
    # backend command.  Never silently reinterpret that provider as a backend:
    # require an explicit backend flag or the provider's stable id instead.
    if argv and argv[0].casefold() in BACKEND_ALIASES:
        shadowed = _reserved_backend_provider(argv[0])
        if shadowed is not None:
            raise RuntimeError(
                f"provider 名称“{shadowed['name']}”与 claude1 后端命令冲突；"
                f"使用 --{argv[0].casefold()} 选择后端，或 "
                f"id:{shadowed['id']} 选择该 provider"
            )

    backend, hint, claude_args = parse_args(argv)

    if backend == "anyrouter":
        return exec_settings_backend(ANYROUTER_SETTINGS, "anyrouter", claude_args)
    if backend == "current":
        return launch_provider(
            current_provider(),
            claude_args,
            backend_kind="current",
        )
    if backend == "direct":
        return exec_plain_claude("direct", claude_args)
    if backend == "hub":
        return exec_hub(claude_args)

    # 默认路径：给了名字就直接匹配启动；没给名字就进 TUI 启动器选一个
    if hint is not None:
        providers = list_providers()
        if not providers:
            print("[claude1] CC Switch 中没有 Claude provider", file=sys.stderr)
            return 1
        selected = choose(providers, hint)
    else:
        action, payload = run_tui_launcher()
        if action == "no-tui":
            providers = list_providers()
            if not providers:
                print("[claude1] CC Switch 中没有 Claude provider", file=sys.stderr)
                return 1
            selected = choose(providers, None)
        elif action == "quit":
            print("Bye，欢迎下次使用 claude1。")
            return 0
        elif action == "hub":
            if isinstance(payload, tuple):
                selected_hub, selector = payload
                return exec_hub(
                    ["--model", selector, *claude_args],
                    hub_id=selected_hub,
                )
            return exec_hub(["--model", payload, *claude_args])
        elif action == "hub-slot":
            if isinstance(payload, tuple):
                selected_hub, slot = payload
                return exec_hub(
                    ["--slot", slot, *claude_args],
                    hub_id=selected_hub,
                )
            return exec_hub(["--slot", payload, *claude_args])
        else:
            selected = provider_by_id(payload)
            if selected is None:
                print(f"[claude1] 找不到 provider id: {payload}", file=sys.stderr)
                return 1

    return launch_provider(selected, claude_args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n[claude1] 已取消", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[claude1] 错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
