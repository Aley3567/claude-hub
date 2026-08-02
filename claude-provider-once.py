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

import json
import hmac
import math
import os
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
_hub_processes: list[subprocess.Popen] = []

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


def provider_by_name(name: str) -> dict | None:
    matches = [
        _provider_from_row(row)
        for row in db_claude_rows()
        if row["name"] == name
    ]
    if len(matches) > 1:
        selectors = "、".join(f"id:{provider['id']}" for provider in matches)
        raise RuntimeError(
            f"provider 名称 '{name}' 不唯一；请设置不同别名或使用 {selectors}"
        )
    return matches[0] if matches else None


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
        config = json.loads(provider.get("settings_config") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    env = config.get("env") if isinstance(config, dict) else None
    base_url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
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


def subagent_model_overrides() -> list[tuple[str, str]]:
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
    for provider_id, name, raw_settings in rows:
        settings = json.loads(raw_settings or "{}")
        env = settings.get("env") if isinstance(settings, dict) else None
        if isinstance(env, dict) and SUBAGENT_MODEL_KEY in env:
            overrides.append((str(provider_id), str(name)))
    return overrides


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


def build_settings(provider: dict) -> dict:
    """Return the provider settings_config from CC Switch DB with NO_PROXY applied."""
    cfg = json.loads(provider["settings_config"] or "{}")
    env = {
        k: str(v)
        for k, v in (cfg.get("env") or {}).items()
        if k != SUBAGENT_MODEL_KEY
    }

    if not any(k.startswith("ANTHROPIC_AUTH") or k.startswith("ANTHROPIC_API") for k in env):
        # A credential-less entry falls back to the currently stored Claude
        # login for this one session.
        print(
            f"[claude1] 注意: provider {provider['name']} 没有独立凭证，将使用当前已登录的凭证",
            file=sys.stderr,
        )
    host = urlparse(env.get("ANTHROPIC_BASE_URL", "")).hostname
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
                if p.strip() and p.strip() != host
            ]
            if parts:
                env[key] = ",".join(parts)
            else:
                env.pop(key, None)
    elif host:
        for key in ("NO_PROXY", "no_proxy"):
            parts = [p.strip() for p in env.get(key, "").split(",") if p.strip()]
            if host not in parts:
                parts.append(host)
            env[key] = ",".join(parts)
    _seal_model_slots(env)
    cfg["env"] = env

    # Drop the cc-switch-specific top-level "model" alias (e.g. "opus[1m]");
    # Claude Code --settings does not read it, and model selection is already
    # fully expressed by the ANTHROPIC_*_MODEL env vars above.
    cfg.pop("model", None)

    return cfg


def add_anyrouter_observer(settings: dict, provider_name: str) -> None:
    """Observe real Any router turn outcomes without reading conversation content."""
    if provider_name != "Any router" or not ANYROUTER_OBSERVER.is_file():
        return

    hooks = settings.setdefault("hooks", {})
    commands = {
        "Stop": f"{ANYROUTER_OBSERVER} success",
        "StopFailure": f"{ANYROUTER_OBSERVER} failure",
    }
    for event, command in commands.items():
        groups = hooks.setdefault(event, [])
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
    raw = os.environ.get("CLAUDE1_HUB_PORT", cfg.get("port"))
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"hub 端口无效: {raw!r}") from None
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


def hub_healthy(port: int, token: str | None = None) -> bool:
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
        if not contract_ok or token is None:
            return contract_ok
        proof = payload.get("proof")
        proof_message = f"claude-hub-ready:v1:{port}:{challenge}".encode("ascii")
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
    port: int, *, token_env: str = DEFAULT_HUB_TOKEN_ENV
) -> dict[str, str]:
    """Build a minimal child environment instead of inheriting secrets/proxies."""
    child: dict[str, str] = {
        "HOME": str(HOME),
        "PATH": os.environ.get("PATH", os.defpath),
        "CLAUDE_HUB_CONFIG": str(HUB_CONFIG),
        "CLAUDE_HUB_DB": str(HUB_DB),
        "CLAUDE_HUB_LOG": str(HUB_LOG),
        "CLAUDE_HUB_PORT": str(port),
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
def _hub_start_lock():
    """Serialize Hub startup between independently launched claude1 processes."""
    if os.name != "posix":
        yield
        return
    import fcntl

    lock_path = HUB_LOG.parent / "claude-hub.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def ensure_hub(
    port: int,
    *,
    token: str | None = None,
    token_env: str = DEFAULT_HUB_TOKEN_ENV,
) -> None:
    """Start the isolated claude-hub process unless its strict health check passes."""
    if hub_healthy(port, token):
        return
    with _hub_start_lock():
        # Another claude1 may have completed startup while this process waited
        # for the inter-process lock.
        if hub_healthy(port, token):
            return
        if not HUB_SCRIPT.is_file():
            raise RuntimeError(f"hub 脚本不存在: {HUB_SCRIPT}")
        print("[claude1] claude-hub 未运行，正在启动 ...", file=sys.stderr)
        with _open_private_append(HUB_LOG) as log:
            process = subprocess.Popen(
                [str(HUB_SCRIPT), "serve"],
                stdout=log,
                stderr=log,
                env=_hub_start_env(port, token_env=token_env),
                close_fds=True,
                start_new_session=True,
            )
        # Keep detached children referenced so Popen can reap them without emitting
        # ResourceWarning; a later start prunes processes that have already exited.
        _hub_processes[:] = [child for child in _hub_processes if child.poll() is None]
        _hub_processes.append(process)
        deadline = time.monotonic() + _hub_start_timeout()
        while time.monotonic() < deadline:
            if hub_healthy(port, token):
                return
            return_code = process.poll()
            if return_code is not None:
                _stop_spawned_process(process)
                raise RuntimeError(
                    f"claude-hub 启动进程提前退出（状态 {return_code}），"
                    f"查看日志: {HUB_LOG}"
                )
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        _stop_spawned_process(process)
        raise RuntimeError(f"claude-hub 启动失败，查看日志: {HUB_LOG}")


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
            return "默认"
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

    @property
    def summary(self) -> str:
        """Main-screen one-liner: `N 渠道 · M 模型`."""
        return f"{self.channel_count} 渠道 · {self.model_count} 模型"


@dataclass(frozen=True)
class HubLaunch:
    """Launcher result signalling the user picked a hub model to start."""

    option: HubModelOption


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

    status = HubStatus(
        port=_hub_port(hub_cfg),
        default_channel=default_alias,
        default_model=default_model,
        channel_count=len(channels),
        model_count=sum(len(channel.models) for channel in channels),
    )
    return status, ordered


def _load_hub_view() -> tuple[HubStatus, list[HubModelOption]] | None:
    """Read HUB_CONFIG for the launcher; return None when hub is unavailable.

    A missing/invalid config (or missing uv) must never break plain provider
    selection, so any failure degrades silently to “no hub entry”.
    """
    if not HUB_CONFIG.is_file():
        return None
    try:
        hub_cfg = json.loads(HUB_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(hub_cfg, dict):
            return None
        return build_hub_view(hub_cfg)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError):
        return None


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


def _wide_enough(win) -> bool:
    logo_width = max((_dwidth(line) for line in LOGO), default=0)
    return win.getmaxyx()[1] >= logo_width + 4


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


def _draw_logo(win, phase: int, *, breathing: bool = False) -> None:
    """Flow the logo palette and optionally pulse its brightness."""
    n = len(_logo_pairs) or 1
    h, w = win.getmaxyx()
    intensity = _logo_intensity(phase, breathing)
    if _large_logo_supported(h, w):
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
    hub_status: "HubStatus | None" = None,
    hub_focus: bool = False,
) -> None:
    meta = cfg["providers"]
    win.erase()
    h, w = win.getmaxyx()
    big = _large_logo_supported(h, w)

    if show_brand and big:
        _addstr(win, 0, 2, "欢迎回来", C.get("pink", 0) | curses.A_BOLD)
        _draw_logo(win, logo_phase, breathing=logo_breathing)
        head = _LOGO_TOP + len(LOGO)
    elif show_brand:
        _draw_logo(win, logo_phase, breathing=logo_breathing)
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
    if hub_status is not None and not notice:
        guide = "↑↓ / jk 移动 · Enter 进入 Hub / 启动渠道 · 数字直达渠道"
    guide_attr = C.get("warning", 0) if notice else C.get("dim", 0)
    _addstr(win, head + 2, 2, guide, guide_attr)
    row_cursor = head + 4
    if hub_status is not None:
        _addstr(win, row_cursor, 2, "多渠道会话", C.get("dim", 0))
        row_cursor += 1
        entry = (
            f"◆ Claude-Hub · 多渠道会话 · {hub_status.summary}"
            " · 会话内 /model 切换"
        )
        entry_width = max(0, w - 4)
        if hub_focus:
            _addstr(
                win,
                row_cursor,
                2,
                _pad_display(entry, entry_width),
                C.get("sel", curses.A_REVERSE),
            )
        else:
            _addstr(
                win,
                row_cursor,
                2,
                _truncate_display(entry, entry_width),
                C.get("orange", 0) | curses.A_BOLD,
            )
        row_cursor += 2
        _addstr(win, row_cursor, 2, "单渠道直连", C.get("dim", 0))
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

    if help_open:
        foot = "a 设置别名 · x 隐藏/显示 · h 隐藏项 · ? 返回 · q 退出"
    else:
        visible_range = ""
        if start > 0 or end < len(view):
            visible_range = f" · {start + 1}–{end}/{len(view)}"
        foot = f"共 {len(view)} 个{visible_range} · ? 更多操作 · q 退出"
    _addstr(win, footer_row, 2, foot, C.get("dim", 0))
    win.refresh()


def _hub_columns(width: int) -> tuple[int, int, int, int]:
    """Fixed column widths for 类型 / 渠道 / 模型 / 状态; model takes the slack."""
    usable = max(0, width - 4)
    family = 14
    channel = 10
    status = 8
    model = max(6, usable - family - channel - status - 3)
    return (family, channel, model, status)


def _hub_row_text(values: tuple[str, str, str, str], cols: tuple[int, int, int, int]) -> str:
    return " ".join(_pad_display(value, width) for value, width in zip(values, cols))


def _draw_hub_workspace(
    win,
    status: "HubStatus",
    options: list["HubModelOption"],
    idx: int,
) -> None:
    """Render the second-level Hub model picker (类型 / 渠道 / 模型 / 状态)."""
    win.erase()
    h, w = win.getmaxyx()
    _addstr(win, 0, 2, "Claude1  ›  Claude-Hub", C.get("dim", 0))
    _addstr(win, 1, 2, "选择 Hub 模型", C.get("lime", 0) | curses.A_BOLD)
    if status.healthy is True:
        badge, badge_attr = "● 已就绪", C.get("lime", 0) | curses.A_BOLD
    elif status.healthy is False:
        badge, badge_attr = "● 未就绪（选择后自动拉起）", C.get("warning", 0)
    else:
        badge, badge_attr = "● 探测中…", C.get("dim", 0)
    _addstr(win, 2, 2, badge, badge_attr)
    meta = (
        f"127.0.0.1:{status.port} · {status.channel_count} 渠道"
        f" · {status.model_count} 模型 · 默认 {status.default_channel}"
    )
    _addstr(win, 2, 4 + _dwidth(badge), meta, C.get("dim", 0))

    cols = _hub_columns(w)
    header = _hub_row_text(("  类型", "渠道", "模型", "状态"), cols)
    _addstr(win, 4, 2, header, C.get("dim", 0))
    list_top = 5
    footer_row = max(0, h - 1)
    capacity = max(0, footer_row - list_top)
    start, end = _visible_window(len(options), idx, capacity)
    for offset, i in enumerate(range(start, end)):
        option = options[i]
        marker = "▸" if i == idx else " "
        text = _hub_row_text(
            (
                f"{marker} {option.family}",
                option.channel,
                option.model,
                option.status_label,
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
    foot = "Esc 返回 Claude1 · ↑↓ / jk 选择 · Enter 启动 · q 退出"
    _addstr(win, footer_row, 2, foot, C.get("dim", 0))
    win.refresh()


def _hub_workspace(
    win,
    status: "HubStatus",
    options: list["HubModelOption"],
) -> tuple[str, "HubModelOption | None"]:
    """Run the Hub model picker loop.

    Returns (outcome, option) where outcome is:
      "launch" — start the returned option through the hub,
      "back"   — Esc, return to the Claude1 home screen,
      "quit"   — q or terminal EOF, exit the launcher entirely.
    """
    idx = 0
    # Draw once while probing so a down hub does not freeze the screen silently.
    _draw_hub_workspace(win, status, options, idx)
    status = replace(status, healthy=hub_healthy(status.port))
    _draw_hub_workspace(win, status, options, idx)
    while True:
        ch = win.getch()
        if ch == -1:
            return ("quit", None)
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif ch in (10, 13, curses.KEY_ENTER):
            return ("launch", options[idx])
        elif ch == 27:
            return ("back", None)
        elif ch == ord("q"):
            return ("quit", None)
        else:
            continue
        _draw_hub_workspace(win, status, options, idx)


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
    hub_view = _load_hub_view()
    hub_status, hub_options = hub_view if hub_view is not None else (None, [])
    hub_focus = hub_view is not None
    help_open = False
    notice: str | None = None
    rows, cols = win.getmaxyx()
    intro_animate = _animation_enabled() and _large_logo_supported(rows, cols)
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
        hub_status=hub_status,
        hub_focus=hub_focus,
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
                pass
            elif hub_status is not None:
                if idx <= 0:
                    hub_focus = True
                else:
                    idx -= 1
            elif view:
                idx = (idx - 1) % len(view)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if hub_focus:
                hub_focus = False
                idx = 0
            elif hub_status is not None:
                if view and idx < len(view) - 1:
                    idx += 1
            elif view:
                idx = (idx + 1) % len(view)
        elif ch == ord("a"):
            if not hub_focus and view:
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
        elif ch == ord("h"):  # 切换「显示隐藏项」
            preferred = view[idx] if view else None
            show_hidden = not show_hidden
            view = _build_view(cfg, db_ids, mru, show_hidden)
            idx = _initial_index(view, mru, preferred)
        elif ch == ord("?"):
            help_open = not help_open
        elif ch in (10, 13, curses.KEY_ENTER):
            if hub_focus and hub_status is not None:
                outcome, option = _hub_workspace(win, hub_status, hub_options)
                if outcome == "launch" and option is not None:
                    return HubLaunch(option)
                if outcome == "quit":
                    return None
                # "back": stay on the home screen and redraw below.
            elif view:
                return view[idx]
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
            hub_status=hub_status,
            hub_focus=hub_focus,
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
        return ("hub", result.option.selector)
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
    """Launch Claude with a private settings file and always remove it."""
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


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _provider_models(settings: dict) -> list[str]:
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
    return names or ["claude1-provider-model"]


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
) -> int:
    """Run one isolated Hub for a non-Anthropic provider, then remove it.

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
        port = _free_loopback_port()
        local_token = secrets.token_urlsafe(32)
        config = {
            "version": 1,
            "port": port,
            "local_token_env": "CLAUDE_HUB_LOCAL_TOKEN",
            "default_channel": "direct",
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

        with _open_private_append(log_path) as log:
            process = subprocess.Popen(
                [str(HUB_SCRIPT), "serve"],
                stdout=log,
                stderr=log,
                env=_bridge_child_env(
                    config=config_path,
                    log=log_path,
                    port=port,
                    local_token=local_token,
                ),
                close_fds=True,
                start_new_session=True,
            )
        try:
            deadline = time.monotonic() + _hub_start_timeout()
            while time.monotonic() < deadline:
                if hub_healthy(port, local_token):
                    break
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"协议桥提前退出（状态 {return_code}），日志: {log_path}"
                    )
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            else:
                raise RuntimeError(f"协议桥启动超时，日志: {log_path}")

            bridged = json.loads(json.dumps(settings))
            env = bridged.setdefault("env", {})
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
            env["ANTHROPIC_AUTH_TOKEN"] = local_token
            env.pop("ANTHROPIC_API_KEY", None)
            env["NO_PROXY"] = "127.0.0.1,localhost"
            env["no_proxy"] = "127.0.0.1,localhost"
            print(
                f"[claude1] 协议适配: Anthropic Messages ↔ {api_format} "
                f"(隔离端口 {port})"
            )
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
            if index + 1 >= len(claude_args):
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


def _normalize_hub_model(value: str, channels: dict) -> str:
    raw = value.strip()
    if raw.startswith("anthropic/"):
        raw = raw[len("anthropic/") :]
    alias, separator, model = raw.partition(",")
    alias, model = alias.strip().casefold(), model.strip()
    if not separator or not alias or not model:
        raise RuntimeError("hub 模型格式应为 <渠道,模型>，例如 fast,sonnet")
    if alias not in channels:
        available = "、".join(str(item) for item in channels)
        raise RuntimeError(f"hub 中没有渠道 '{alias}'；可用渠道: {available}")
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
            entry = json.loads(line)
            message = entry.get("message") if entry.get("type") == "assistant" else None
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model:
                last_model = model
    if last_model is None:
        return None

    alias, separator, model = last_model.partition(",")
    alias, model = alias.strip().casefold(), model.strip()
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


def _hub_effort_level(hub_cfg: dict) -> str | None:
    """Return the optional Claude Code effort pin for a Hub session."""
    raw = hub_cfg.get("effort_level")
    if raw is None:
        return None
    if not isinstance(raw, str) or raw not in {"low", "medium", "high", "xhigh"}:
        raise RuntimeError(
            "hub 配置中的 effort_level 必须是 low、medium、high 或 xhigh"
        )
    return raw


def _hub_effort_capabilities(effort_level: str | None) -> str:
    """Advertise the thinking features needed for custom Hub model slots.

    Claude Code treats custom model ids conservatively unless their slot's
    ``SUPPORTED_CAPABILITIES`` explicitly opts in.  Without these flags an
    xhigh session can still display xhigh while silently omitting or lowering
    the API-side thinking request.
    """
    if effort_level is None:
        return ""
    capabilities = ["thinking", "adaptive_thinking", "effort"]
    if effort_level == "xhigh":
        capabilities.append("xhigh_effort")
    return ",".join(capabilities)


def exec_hub(claude_args: list[str]) -> int:
    """Launch one Claude session through the isolated multi-channel hub."""
    requested_model, claude_args = _extract_hub_model(claude_args)
    if not HUB_CONFIG.is_file():
        raise RuntimeError(f"hub 配置不存在: {HUB_CONFIG}")
    try:
        hub_cfg = json.loads(HUB_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"hub 配置无法读取: {HUB_CONFIG}: {exc}") from exc
    if not isinstance(hub_cfg, dict):
        raise RuntimeError(f"hub 配置格式无效: {HUB_CONFIG}")

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

    resume_model = (
        _resume_session_selector(claude_args, channels)
        if requested_model is None
        else None
    )
    main_model = (
        _normalize_hub_model(requested_model, channels)
        if requested_model is not None
        else resume_model or f"{default_channel},{models[0]}"
    )
    slot_models = _hub_model_slots(hub_cfg, channels, main_model)
    effort_level = _hub_effort_level(hub_cfg)
    effort_capabilities = _hub_effort_capabilities(effort_level)
    ensure_hub(port, token=token, token_env=token_env)
    settings_env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": main_model,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    if effort_level is not None:
        # The environment pin also reaches ordinary subagents/workflow agents;
        # the top-level setting keeps Claude Code's UI and persisted session
        # metadata aligned with the effective value.
        settings_env["CLAUDE_CODE_EFFORT_LEVEL"] = effort_level
    for tier, selector in slot_models.items():
        model_key = f"ANTHROPIC_DEFAULT_{tier}_MODEL"
        alias, _, upstream_model = selector.partition(",")
        settings_env[model_key] = selector
        settings_env[f"{model_key}_NAME"] = upstream_model
        settings_env[f"{model_key}_DESCRIPTION"] = f"Claude-Hub · {alias}"
        settings_env[f"{model_key}_SUPPORTED_CAPABILITIES"] = effort_capabilities
    _seal_model_slots(settings_env)
    settings = {"env": settings_env}
    if effort_level is not None:
        settings["effortLevel"] = effort_level
    record_backend("hub")
    aliases = ", ".join(str(alias) for alias in channels)
    print(
        f"[claude1] 后端: hub (127.0.0.1:{port}, 默认 {main_model})"
    )
    print(f"[claude1] 会话内切渠道: /model 别名,模型   渠道: {aliases}")
    return launch_with_settings(settings, claude_args)


CLAUDE1_USAGE = f"""claude1 {VERSION} — 为本次 Claude Code 会话选择渠道

用法:
  claude1                              打开渠道选择器
  claude1 <名称/别名/id:ID> [Claude 参数] 直接启动一个渠道
  claude1 hub [--model 渠道,模型]      进入可用 /model 热切换的 Hub
  claude1 list [--all]                 查看渠道，不启动 Claude
  claude1 doctor [--fix]               检查本机配置；--fix 清理子代理模型固定值
  claude1 usage [--day|--week|--month] 查看 token 用量与缓存命中率曲线
  claude1 use <backend>                显式设置普通 claude 的粘性后端
  claude1 --help                       显示本帮助

快捷键:
  ↑↓ / jk 移动 · Enter 启动 · 1–9/0 数字直达 · ? 更多操作 · q 退出

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
        x = round(idx * (cols - 1) / max(1, len_vals - 1))
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
        # 与数据点同一映射：时间 t 落在桶索引 [0, n-1]，再按 per 拉伸到画布列
        x = round((t - start) / span * (n - 1)) * per
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
    rows = _load_usage_rows(HUB_USAGE, now - span)
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


def fix_subagent_model_overrides() -> tuple[list[str], Path]:
    """Back up the CC Switch DB, then remove persisted subagent model pins."""
    backup_path = DB_PATH.with_name(
        f"{DB_PATH.name}.bak-doctor-fix-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(DB_PATH, backup_path)

    changed: list[str] = []
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, name, settings_config FROM providers ORDER BY app_type, sort_index"
            ).fetchall()
            for provider_id, name, raw_settings in rows:
                settings = json.loads(raw_settings or "{}")
                env = settings.get("env") if isinstance(settings, dict) else None
                if not isinstance(env, dict) or SUBAGENT_MODEL_KEY not in env:
                    continue
                env.pop(SUBAGENT_MODEL_KEY)
                conn.execute(
                    "UPDATE providers SET settings_config = ? WHERE id = ?",
                    (json.dumps(settings, ensure_ascii=False), provider_id),
                )
                changed.append(str(name))
    finally:
        conn.close()
    return changed, backup_path


def cli_doctor(*, fix: bool = False) -> int:
    """Check local state and optionally remove persisted subagent model pins."""
    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        if level == "FAIL":
            failures += 1
        print(f"  {level:<4} {message}")

    if fix:
        changed, backup_path = fix_subagent_model_overrides()
        print("claude1 doctor --fix（不连接上游）\n")
        print(f"  BACKUP {backup_path}")
        for name in changed:
            print(f"  FIX  {name}: 已移除 {SUBAGENT_MODEL_KEY}")
        print()
    else:
        print("claude1 doctor（本机只读，不连接上游）\n")
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
        overrides = subagent_model_overrides()
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
    add_anyrouter_observer(settings, selected["name"])
    record_use(str(selected["id"]))
    record_backend(backend_kind, selected["name"])
    if backend_kind == "current":
        print(f"[claude1] 本次使用 CC Switch 当前 provider: {selected['name']}")
    else:
        print(f"[claude1] 本次使用 provider: {selected['name']}")
    api_format = selected_provider_api_format(selected)
    if api_format != "anthropic":
        return launch_with_protocol_bridge(
            selected,
            settings,
            api_format,
            claude_args,
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
            return exec_hub(["--model", payload, *claude_args])
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
