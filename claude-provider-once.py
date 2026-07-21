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
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


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
RECLAUDE_ISOLATED = _env_path(
    "CLAUDE1_RECLAUDE_BIN", HOME / ".local" / "bin" / "reclaude-isolated"
)
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
    "re": "reclaude",
    "reclaude": "reclaude",
    "rec": "reclaude",
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


def sync_config(cfg: dict, db_names: list[str]) -> bool:
    """Seed from CC Switch order, then append newly discovered providers.

    Returns True if cfg was modified (caller decides whether to persist).
    """
    providers = cfg.setdefault("providers", {})
    changed = False
    if not providers:
        for name in db_names:
            providers[name] = {"hidden": False}
        changed = True
    for name in db_names:
        if name not in providers:
            providers[name] = {"hidden": False}  # 新号默认出现在列表(opt-out)
            changed = True
    return changed


def migrate_hidden(cfg: dict) -> bool:
    """v1(enabled) → v2(hidden)。旧的 enabled=false 视为「隐藏」，保持原菜单不变。"""
    if cfg.get("version", 1) >= 2:
        return False
    for m in cfg.get("providers", {}).values():
        if "hidden" not in m:
            m["hidden"] = (m.get("enabled") is False)
        m.pop("enabled", None)
    cfg["version"] = 2
    return True


def provider_by_name(name: str) -> dict | None:
    for r in db_claude_rows():
        if r["name"] == name:
            return {"id": r["id"], "name": r["name"], "settings_config": r["settings_config"]}
    return None


MANAGED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "RECLAUDE_",
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


def db_claude_rows() -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise RuntimeError(f"CC Switch DB 不存在: {DB_PATH}")
    db_uri = DB_PATH.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, name, settings_config FROM providers "
            "WHERE app_type='claude' ORDER BY sort_index"
        ).fetchall()
    finally:
        conn.close()


def list_providers() -> list[dict]:
    rows = db_claude_rows()
    by_name = {
        r["name"]: {"id": r["id"], "name": r["name"], "settings_config": r["settings_config"]}
        for r in rows
    }
    cfg = load_config()
    changed = sync_config(cfg, list(by_name.keys()))
    changed |= migrate_hidden(cfg)
    if changed:
        save_config(cfg)
    visible = [n for n, meta in cfg["providers"].items() if not meta.get("hidden")]
    ordered = []
    for n in visible:
        if n in by_name:
            entry = dict(by_name[n])
            alias = cfg["providers"][n].get("alias")
            if alias:
                entry["alias"] = alias
            ordered.append(entry)
    if not ordered:
        # 全被隐藏(或配置为空) —— 别把人困住，回退到全部。
        ordered = list(by_name.values())
    return ordered


def build_settings(provider: dict) -> dict:
    """Return the provider settings_config from CC Switch DB with NO_PROXY applied."""
    cfg = json.loads(provider["settings_config"] or "{}")
    env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}

    if not any(k.startswith("ANTHROPIC_AUTH") or k.startswith("ANTHROPIC_API") for k in env):
        # A credential-less entry falls back to the currently stored Claude
        # login for this one session.
        print(
            f"[claude1] 注意: provider {provider['name']} 没有独立凭证，将使用当前已登录的凭证",
            file=sys.stderr,
        )
    host = urlparse(env.get("ANTHROPIC_BASE_URL", "")).hostname
    if host:
        for key in ("NO_PROXY", "no_proxy"):
            parts = [p.strip() for p in env.get(key, "").split(",") if p.strip()]
            if host not in parts:
                parts.append(host)
            env[key] = ",".join(parts)
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
    try:
        req = urllib.request.Request(GATEWAY_URL + "/", method="GET")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True  # any HTTP response means the gateway is up
    except OSError:
        return False


def ensure_local_gateway(base_url: str) -> None:
    """Start the local cliproxyapi gateway if the provider routes through it."""
    parsed = urlparse(base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != 18317:
        return
    if gateway_healthy():
        return
    print("[claude1] 本地网关未运行，正在启动 cliproxyapi ...", file=sys.stderr)
    GATEWAY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(GATEWAY_LOG, "ab") as log:
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


def _hub_local_token(cfg: dict) -> str:
    # Match claude-hub itself: an explicit environment secret safely overrides
    # a legacy config token, while old live configs remain supported.
    token = os.environ.get("CLAUDE_HUB_LOCAL_TOKEN") or cfg.get("local_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(
            "hub 本地凭证缺失：请在配置中设置 local_token，"
            "或设置 CLAUDE_HUB_LOCAL_TOKEN"
        )
    return token


def hub_healthy(port: int) -> bool:
    """Only accept the versioned claude-hub health contract on loopback."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/healthz", method="GET"
        )
        # A loopback health probe must never follow the user's proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=2) as response:
            if getattr(response, "status", response.getcode()) != 200:
                return False
            payload = json.loads(response.read(65537).decode("utf-8"))
        protocol = payload.get("protocol") if isinstance(payload, dict) else None
        return (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("service") == "claude-hub"
            and isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and protocol == 1
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ValueError,
    ):
        return False


def _hub_start_env(port: int) -> dict[str, str]:
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
    local_token = os.environ.get("CLAUDE_HUB_LOCAL_TOKEN")
    if local_token:
        child["CLAUDE_HUB_LOCAL_TOKEN"] = local_token
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


def ensure_hub(port: int) -> None:
    """Start the isolated claude-hub process unless its strict health check passes."""
    if hub_healthy(port):
        return
    if not HUB_SCRIPT.is_file():
        raise RuntimeError(f"hub 脚本不存在: {HUB_SCRIPT}")
    print("[claude1] claude-hub 未运行，正在启动 ...", file=sys.stderr)
    with _open_private_append(HUB_LOG) as log:
        process = subprocess.Popen(
            [str(HUB_SCRIPT), "serve"],
            stdout=log,
            stderr=log,
            env=_hub_start_env(port),
            close_fds=True,
            start_new_session=True,
        )
    # Keep detached children referenced so Popen can reap them without emitting
    # ResourceWarning; a later start prunes processes that have already exited.
    _hub_processes[:] = [child for child in _hub_processes if child.poll() is None]
    _hub_processes.append(process)
    deadline = time.monotonic() + _hub_start_timeout()
    while time.monotonic() < deadline:
        if hub_healthy(port):
            return
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"claude-hub 启动进程提前退出（状态 {return_code}），"
                f"查看日志: {HUB_LOG}"
            )
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    raise RuntimeError(f"claude-hub 启动失败，查看日志: {HUB_LOG}")


def _provider_terms(provider: dict) -> list[str]:
    terms = [str(provider.get("name", ""))]
    alias = provider.get("alias")
    if isinstance(alias, str) and alias.strip():
        terms.append(alias.strip())
    return terms


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
    if hint:
        matches, exact = match_providers(providers, hint)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and exact:
            names = "、".join(str(p["name"]) for p in matches)
            raise RuntimeError(
                f"名称或别名 '{hint}' 存在冲突: {names}；请修改其中一个别名"
            )
        if len(matches) > 1:
            print("匹配到多个 provider，请选择:")
            for i, p in enumerate(matches, 1):
                alias = f" ({p['alias']})" if p.get("alias") else ""
                print(f"{i}. {p['name']}{alias}")
            choice = input("> ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(matches):
                    return matches[idx]
            raise RuntimeError("无效选择，已取消")
        raise RuntimeError(f"找不到匹配 '{hint}' 的 provider")

    print("选择本次 Claude Code provider:")
    for i, p in enumerate(providers, 1):
        print(f"{i}. {p['name']}")
    choice = input("> ").strip()
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(providers):
            return providers[idx]
    matches, exact = match_providers(providers, choice)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and exact:
        names = "、".join(str(p["name"]) for p in matches)
        raise RuntimeError(f"名称或别名 '{choice}' 存在冲突: {names}")
    raise RuntimeError("无效选择，已取消")


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
# Ten terminal frames per second feels fluid without wasting work; phase is
# derived from elapsed time, so a slow refresh skips stale frames instead of
# slowing the flow itself. After 15s the chooser blocks with zero wakeups.
ANIMATION_FRAME_MS = 100
ANIMATION_IDLE_SECONDS = 15.0
ANIMATION_PHASE_PERIOD = 240
# A closed controlling terminal makes getch() return -1 immediately instead of
# waiting out its timeout. Bail after this many back-to-back sub-frame empty
# polls so a detached session never spins a CPU core on invisible animation.
ANIMATION_DEAD_TTY_POLLS = 4
_ANIMATION_POLL_FLOOR_SECONDS = (ANIMATION_FRAME_MS / 1000.0) * 0.5
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


def _animation_phase(started: float, now: float) -> int:
    elapsed = max(0.0, now - started)
    step = ANIMATION_FRAME_MS / 1000.0
    return int(elapsed / step) % ANIMATION_PHASE_PERIOD


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


def _build_view(cfg, db_names, mru, show_hidden):
    """Keep config order stable; MRU only affects the initial cursor."""
    meta = cfg["providers"]
    return [
        n for n in meta
        if n in db_names and (show_hidden or not meta[n].get("hidden"))
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
    current_name: str,
    candidate: str,
) -> str | None:
    folded = candidate.strip().casefold()
    if not folded:
        return None
    for name, provider_meta in meta.items():
        if name == current_name:
            continue
        terms = [name]
        alias = provider_meta.get("alias") if isinstance(provider_meta, dict) else None
        if isinstance(alias, str) and alias.strip():
            terms.append(alias.strip())
        if any(term.casefold() == folded for term in terms):
            return name
    return None


def _set_alias(meta: dict, name: str, candidate: str) -> tuple[bool, str]:
    candidate = candidate.strip()
    if not candidate:
        changed = bool(meta[name].pop("alias", None))
        return (changed, "别名已清除" if changed else "未设置别名")
    if candidate.startswith("-"):
        return (False, "别名不能以 “-” 开头，否则会与命令参数冲突")
    if candidate.casefold() in RESERVED_SELECTOR_WORDS:
        return (False, f"“{candidate}”是 claude1 保留命令，请换一个别名")
    conflict = _alias_conflict(meta, name, candidate)
    if conflict:
        return (False, f"别名“{candidate}”已被 {conflict} 使用")
    if meta[name].get("alias") == candidate:
        return (False, f"别名仍为 {candidate}")
    meta[name]["alias"] = candidate
    return (True, f"别名已设为 {candidate}")


def _edit_alias(win, name, meta) -> tuple[bool, str]:
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
    return _set_alias(meta, name, s)


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
) -> None:
    meta = cfg["providers"]
    win.erase()
    h, w = win.getmaxyx()
    big = _large_logo_supported(h, w)

    if big:
        if show_brand:
            _addstr(win, 0, 2, "欢迎回来", C.get("pink", 0) | curses.A_BOLD)
            _draw_logo(win, logo_phase, breathing=logo_breathing)
        head = _LOGO_TOP + len(LOGO)
    else:
        if show_brand:
            _draw_logo(win, logo_phase, breathing=logo_breathing)
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
    guide_attr = C.get("warning", 0) if notice else C.get("dim", 0)
    _addstr(win, head + 2, 2, guide, guide_attr)
    list_top = head + 4
    footer_row = max(0, h - 1)
    capacity = max(0, footer_row - list_top)
    start, end = _visible_window(len(view), idx, capacity)
    recent = _recent_name(view, mru)

    if not view:
        _addstr(win, list_top, 2, "没有可用渠道", C.get("warning", 0))
    row_width = max(0, w - 4)
    for row_offset, i in enumerate(range(start, end)):
        name = view[i]
        m = meta[name]
        hidden = m.get("hidden")
        rank = i + 1
        selected = i == idx
        marker = "▸" if selected else " "
        label = f"{marker} {rank:>2}  {name}"
        status: list[str] = []
        if m.get("alias"):
            status.append(str(m["alias"]))
        if name == recent:
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


def _launcher_main(win, cfg, db_names):
    """返回要启动的 provider 名，或 None(退出不启动)。hide/alias 即时落盘。"""
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
    view = _build_view(cfg, db_names, mru, show_hidden)
    idx = _initial_index(view, mru)
    help_open = False
    notice: str | None = None
    rows, cols = win.getmaxyx()
    intro_animate = _animation_enabled() and _large_logo_supported(rows, cols)
    animate = intro_animate and len(_logo_pairs) > 1
    _draw_launcher(
        win,
        cfg,
        view,
        idx,
        show_hidden,
        mru,
        show_brand=not intro_animate,
    )
    pending_key = _intro(win) if intro_animate else None
    phase = 0
    paused = not animate
    blocking = not animate
    fast_empty = 0
    animation_started = time.monotonic()
    last_active = animation_started
    last_poll = animation_started
    win.timeout(ANIMATION_FRAME_MS if animate else -1)
    _draw_launcher(
        win,
        cfg,
        view,
        idx,
        show_hidden,
        mru,
        logo_phase=phase,
        logo_breathing=animate,
    )
    while True:
        ch = pending_key if pending_key is not None else win.getch()
        pending_key = None
        now = time.monotonic()
        poll_elapsed = now - last_poll
        last_poll = now
        if ch == -1:
            # A blocking read (timeout == -1) that returns -1 means the input
            # stream reached EOF — typically the controlling terminal closed.
            if blocking:
                return None
            # While animating we only poll, so a lone -1 is a normal frame
            # timeout; but a burst returning far quicker than the frame budget
            # is the same EOF, and must not burn a CPU core on frames nobody
            # can see.
            if poll_elapsed < _ANIMATION_POLL_FLOOR_SECONDS:
                fast_empty += 1
                if fast_empty >= ANIMATION_DEAD_TTY_POLLS:
                    return None
            else:
                fast_empty = 0
            if (now - last_active) >= ANIMATION_IDLE_SECONDS:
                _draw_logo(win, phase, breathing=False)
                win.refresh()
                win.timeout(-1)
                blocking = True
                paused = True
                continue
            next_phase = _animation_phase(animation_started, now)
            if next_phase == phase:
                continue
            phase = next_phase
            _draw_logo(win, phase, breathing=True)
            win.refresh()
            continue

        fast_empty = 0
        last_active = now
        if paused and animate:
            win.timeout(ANIMATION_FRAME_MS)
            blocking = False
            paused = False
        notice = None
        direct_index = _digit_index(ch)
        if direct_index is not None:
            if direct_index < len(view):
                return view[direct_index]
            notice = f"没有第 {direct_index + 1} 个渠道"
        if ch in (curses.KEY_UP, ord("k")):
            if view:
                idx = (idx - 1) % len(view)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if view:
                idx = (idx + 1) % len(view)
        elif ch == ord("a"):
            if view:
                if animate:
                    win.timeout(-1)
                try:
                    changed, notice = _edit_alias(win, view[idx], meta)
                finally:
                    if animate:
                        last_active = time.monotonic()
                        win.timeout(ANIMATION_FRAME_MS)
                if changed:
                    save_config(cfg)
        elif ch == ord("x"):
            if view:
                name = view[idx]
                nowh = meta[name].get("hidden")
                verb = "恢复显示" if nowh else "隐藏"
                if animate:
                    win.timeout(-1)
                try:
                    ok = _confirm(win, f"{verb} {name}?")
                finally:
                    if animate:
                        last_active = time.monotonic()
                        win.timeout(ANIMATION_FRAME_MS)
                if ok:
                    meta[name]["hidden"] = not nowh
                    save_config(cfg)
                    preferred = name
                    view = _build_view(cfg, db_names, mru, show_hidden)
                    idx = _initial_index(view, mru, preferred)
        elif ch == ord("h"):  # 切换「显示隐藏项」
            preferred = view[idx] if view else None
            show_hidden = not show_hidden
            view = _build_view(cfg, db_names, mru, show_hidden)
            idx = _initial_index(view, mru, preferred)
        elif ch == ord("?"):
            help_open = not help_open
        elif ch in (10, 13, curses.KEY_ENTER):
            if view:
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
            help_open=help_open,
            notice=notice,
            logo_phase=phase,
            logo_breathing=animate,
        )


def run_tui_launcher():
    """打开 TUI 启动器。返回 ('launch', name) | ('quit', None) | ('no-tui', None)。"""
    rows = db_claude_rows()
    db_names = {r["name"] for r in rows}
    cfg = load_config()
    changed = sync_config(cfg, [r["name"] for r in rows])
    changed |= migrate_hidden(cfg)
    if changed:
        save_config(cfg)
    if curses is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        return ("no-tui", None)
    terminal = shutil.get_terminal_size(fallback=(80, 24))
    if not _tui_size_supported(terminal.lines, terminal.columns):
        return ("no-tui", None)
    try:
        name = curses.wrapper(_launcher_main, cfg, db_names)
    except Exception as exc:
        print(f"[claude1] 图形界面无法启动({exc})", file=sys.stderr)
        return ("no-tui", None)
    return ("launch", name) if name else ("quit", None)


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
    if kind not in ("reclaude", "anyrouter", "current", "direct", "hub"):
        print(
            f"[claude1] 未知后端: {word}"
            "（可用: re/reclaude · any · cc/current · direct · hub）",
            file=sys.stderr,
        )
        return 1
    try:
        _atomic_write_sticky(kind)
    except OSError as exc:
        print(f"[claude1] 无法写入粘性后端: {exc}", file=sys.stderr)
        return 1
    if kind == "reclaude":
        print("[claude1] 粘性后端 = reclaude —— 之后普通 claude 都走 reclaude，直到再切")
    elif kind == "hub":
        print(
            "[claude1] 粘性后端 = hub —— 之后普通 claude 走多渠道网关，"
            "直到再次显式切换"
        )
    else:
        print(f"[claude1] 粘性后端 = {kind} —— 普通 claude 走 CC-Switch（{kind}），直到再切")
    return 0


def parse_args(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """拆成 (backend, provider_hint, claude_args)。

    backend: 'reclaude' | 'anyrouter' | 'current' | 'direct' | 'hub' | None
    provider_hint: 匹配 CC-Switch provider 的子串（None => 弹菜单）
    claude_args: 展开后的 overlay + 其余原样透传给 claude
    """
    backend: str | None = None
    hint: str | None = None
    claude_args: list[str] = []
    first_positional = True
    for arg in argv:
        low = arg.lower()
        if not arg.startswith("-"):
            if first_positional:
                first_positional = False
                if low in BACKEND_ALIASES:
                    backend = BACKEND_ALIASES[low]
                elif backend is None:
                    hint = arg
                else:
                    claude_args.append(arg)
                continue
            claude_args.append(arg)
            continue
        # claude1 自己理解的 overlay 开关
        if low == "--notion":
            if not NOTION_MCP.exists():
                print(f"[claude1] 警告: notion 配置不存在 {NOTION_MCP}", file=sys.stderr)
            claude_args += ["--mcp-config", str(NOTION_MCP)]
        elif low in ("--reclaude", "--re"):
            backend = "reclaude"
        elif low in ("--any", "--anyrouter"):
            backend = "anyrouter"
        elif low in ("--current", "--cc"):
            backend = "current"
        elif low == "--hub":
            backend = "hub"
        else:
            claude_args.append(arg)
    return backend, hint, claude_args


def exec_reclaude(claude_args: list[str]) -> int:
    if not RECLAUDE_ISOLATED.exists():
        raise RuntimeError(f"reclaude 未安装: {RECLAUDE_ISOLATED}")
    record_backend("reclaude")
    print("[claude1] 后端: reclaude（独立入口，不改 CC Switch）")
    return int(subprocess.run([str(RECLAUDE_ISOLATED), *claude_args]).returncode)


def exec_settings_backend(settings_path: Path, label: str, claude_args: list[str]) -> int:
    if not settings_path.exists():
        raise RuntimeError(f"{label} 配置不存在: {settings_path}")
    record_backend(label)
    print(f"[claude1] 后端: {label} ({settings_path.name})")
    claude_bin = resolve_claude_bin()
    return int(
        subprocess.run(
            [str(claude_bin), "--settings", str(settings_path), *claude_args],
            env=claude_child_env(),
        ).returncode
    )


def exec_plain_claude(label: str, claude_args: list[str]) -> int:
    record_backend(label)
    note = "CC-Switch 当前 provider" if label == "current" else "裸 claude"
    print(f"[claude1] 后端: {label} ({note})")
    claude_bin = resolve_claude_bin()
    return int(
        subprocess.run(
            [str(claude_bin), *claude_args],
            env=claude_child_env(),
        ).returncode
    )


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
        proc = subprocess.run(
            [str(claude_bin), "--settings", tmp_path, *claude_args],
            env=claude_child_env(settings),
        )
        return int(proc.returncode)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _extract_hub_model(claude_args: list[str]) -> tuple[str | None, list[str]]:
    """Consume claude1's Hub-only --model option without touching other args."""
    requested: str | None = None
    forwarded: list[str] = []
    index = 0
    while index < len(claude_args):
        arg = claude_args[index]
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

    main_model = (
        _normalize_hub_model(requested_model, channels)
        if requested_model is not None
        else f"{default_channel},{models[0]}"
    )
    ensure_hub(port)
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": main_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": main_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": main_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": main_model,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    }
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
  claude1 <名称或别名> [Claude 参数]   直接启动一个渠道
  claude1 hub [--model 渠道,模型]      进入可用 /model 热切换的 Hub
  claude1 list [--all]                 查看渠道，不启动 Claude
  claude1 doctor                       做本机只读检查，不连接上游
  claude1 use <backend>                显式设置普通 claude 的粘性后端
  claude1 --help                       显示本帮助

快捷键:
  ↑↓ / jk 移动 · Enter 启动 · 1–9/0 数字直达 · ? 更多操作 · q 退出

默认启动只影响本次会话，不修改普通 claude、CC Switch 当前渠道或 ReClaude。
"""


def cli_list_providers(show_all: bool = False) -> int:
    rows = db_claude_rows()
    by_name = {
        row["name"]: {
            "name": row["name"],
            "settings_config": row["settings_config"],
        }
        for row in rows
    }
    cfg = load_config()
    changed = sync_config(cfg, list(by_name))
    changed |= migrate_hidden(cfg)
    if changed:
        save_config(cfg)
    names = [
        name
        for name, meta in cfg["providers"].items()
        if name in by_name and (show_all or not meta.get("hidden"))
    ]
    if not names:
        print("claude1: 没有可显示的 CC Switch Claude 渠道")
        return 1

    recent = _recent_name(names, load_mru())
    print("claude1 渠道（顺序与选择器一致）\n")
    for index, name in enumerate(names, 1):
        meta = cfg["providers"][name]
        details: list[str] = []
        if meta.get("alias"):
            details.append(f"别名 {meta['alias']}")
        if name == recent:
            details.append("最近")
        if meta.get("hidden"):
            details.append("已隐藏")
        suffix = f"  {' · '.join(details)}" if details else ""
        print(f"  {index:>2}  {name}{suffix}")
    print(f"\n共 {len(names)} 个；运行 `claude1 <名称或别名>` 可直接启动。")
    return 0


def cli_doctor() -> int:
    """Run local read-only checks; never start Claude or contact a provider."""
    failures = 0

    def report(level: str, message: str) -> None:
        nonlocal failures
        if level == "FAIL":
            failures += 1
        print(f"  {level:<4} {message}")

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

    try:
        rows = db_claude_rows()
    except (RuntimeError, sqlite3.Error, OSError):
        report("FAIL", "CC Switch 数据库不存在、不可读或结构不兼容")
    else:
        report("OK", f"CC Switch 数据库只读打开，发现 {len(rows)} 个 Claude 渠道")
        if os.name == "posix" and (DB_PATH.stat().st_mode & 0o077):
            report("FAIL", "CC Switch 数据库含凭证，文件权限应为 0600")

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
        if len(argv) != 1:
            print("[claude1] doctor 不接收额外参数", file=sys.stderr)
            return 2
        return cli_doctor()
    if argv and argv[0] == "use":
        if len(argv) < 2:
            print(
                "[claude1] 用法: claude1 use <re|cc|any|direct|hub>",
                file=sys.stderr,
            )
            return 1
        return set_sticky(argv[1])
    # `config`/`--config` 现在就是无参数：直接进 TUI 启动器
    if argv and argv[0] in ("config", "--config"):
        argv = argv[1:]

    backend, hint, claude_args = parse_args(argv)

    if backend == "reclaude":
        return exec_reclaude(claude_args)
    if backend == "anyrouter":
        return exec_settings_backend(ANYROUTER_SETTINGS, "anyrouter", claude_args)
    if backend == "current":
        return exec_plain_claude("current", claude_args)
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
        action, name = run_tui_launcher()
        if action == "no-tui":
            providers = list_providers()
            if not providers:
                print("[claude1] CC Switch 中没有 Claude provider", file=sys.stderr)
                return 1
            selected = choose(providers, None)
        elif action == "quit":
            return 0
        else:
            selected = provider_by_name(name)
            if selected is None:
                print(f"[claude1] 找不到 provider: {name}", file=sys.stderr)
                return 1

    settings = build_settings(selected)
    add_anyrouter_observer(settings, selected["name"])
    ensure_local_gateway(settings.get("env", {}).get("ANTHROPIC_BASE_URL", ""))
    record_use(selected["name"])
    record_backend("provider", selected["name"])
    print(f"[claude1] 本次使用 provider: {selected['name']}")
    return launch_with_settings(settings, claude_args)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n[claude1] 已取消", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[claude1] 错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
