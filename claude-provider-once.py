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
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


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

# Local protocol-translation gateway (cliproxyapi). Providers whose base URL
# points here (e.g. AIHub, which only speaks the OpenAI protocol upstream)
# need the gateway alive before Claude Code starts.
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


# Which providers show in the menu is no longer hardcoded — it lives in
# CONFIG_PATH and is edited with `claude1 config`. This list is only the
# FIRST-RUN seed: providers enabled by default when no config file exists yet.
# New CC Switch providers discovered later are added disabled (opt-in via
# `claude1 config`). Baibei / 火山Agentplan are kept even though currently
# absent from the DB — they light up automatically if they return.
SEED_ENABLED = [
    "Any router",
    "N1nEAPI",
    "Baibei",
    "Codex",
    "Unity2.Ai",
    "Xiaomi MiMo api",
    "火山Agentplan",
    "火山-key2-备用",
    "Bailian",
    "AIHub",
    "My Claude",
    "supertoken",
]


def load_mru() -> dict[str, float]:
    try:
        data = json.loads(MRU_PATH.read_text())
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError):
        return {}


def record_use(name: str) -> None:
    mru = load_mru()
    mru[name] = time.time()
    try:
        MRU_PATH.parent.mkdir(parents=True, exist_ok=True)
        MRU_PATH.write_text(json.dumps(mru, ensure_ascii=False, indent=1))
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
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    except OSError:
        return False


def sync_config(cfg: dict, db_names: list[str]) -> bool:
    """首跑播种，之后自动发现新 provider（默认可见，不想要就在 TUI 里 x 隐藏）。

    Returns True if cfg was modified (caller decides whether to persist).
    """
    providers = cfg.setdefault("providers", {})
    changed = False
    if not providers:
        for name in SEED_ENABLED:
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
    conn = sqlite3.connect(str(DB_PATH))
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
    # 使用率最高的靠前；其余按配置顺序（不分页，一次列全部）。
    mru = load_mru()
    ordered.sort(key=lambda p: -mru.get(p["name"], 0.0))
    return ordered


def build_settings(provider: dict) -> dict:
    """Return the provider settings_config from CC Switch DB with NO_PROXY applied."""
    cfg = json.loads(provider["settings_config"] or "{}")
    env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}

    if not any(k.startswith("ANTHROPIC_AUTH") or k.startswith("ANTHROPIC_API") for k in env):
        # e.g. "My Claude": no dedicated credentials in the DB — Claude Code
        # falls back to the currently stored login for this one session.
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


def ensure_hub(port: int) -> None:
    """Start the isolated claude-hub process unless its strict health check passes."""
    if hub_healthy(port):
        return
    if not HUB_SCRIPT.is_file():
        raise RuntimeError(f"hub 脚本不存在: {HUB_SCRIPT}")
    print("[claude1] claude-hub 未运行，正在启动 ...", file=sys.stderr)
    HUB_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HUB_LOG, "ab") as log:
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
    for _ in range(20):
        time.sleep(0.25)
        if hub_healthy(port):
            return
    raise RuntimeError(f"claude-hub 启动失败，查看日志: {HUB_LOG}")


def choose(providers: list[dict], hint: str | None) -> dict:
    if hint:
        matches = [p for p in providers if hint.lower() in p["name"].lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"匹配到多个 provider，请选择:")
            for i, p in enumerate(matches, 1):
                print(f"{i}. {p['name']}")
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
    for p in providers:
        if choice.lower() == p["name"].lower():
            return p
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

_HEADER_H = len(LOGO) + 6  # welcome + blank + logo + blank + heading + hint
_LOGO_TOP = 2              # 欢迎语占 row0，空一行后从 row2 起画 logo
C: dict = {}

# 流动的七彩色带（256 色全光谱）：红→橙→黄→绿→青→蓝→紫→品红→回到红，首尾相接无缝循环。
LOGO_GRAD = [196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 48, 50, 51, 45, 39, 33, 63, 99, 135, 171, 207, 201, 199]
_logo_pairs: list[int] = []

# 列表用的七彩色带（256 色）：红→橙→金→黄绿→绿→青→蓝→紫→品红，逐行轮转。
RAINBOW = [203, 208, 214, 220, 148, 46, 42, 51, 45, 75, 99, 141, 207, 205]
_row_pairs: list[int] = []


def _addstr(win, y, x, text, attr=0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    text = text[: max(0, w - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _dwidth(text: str) -> int:
    """终端显示宽度：CJK 全角字符按 2 列计。"""
    width = 0
    for ch in text:
        o = ord(ch)
        wide = (
            0x1100 <= o <= 0x115f
            or 0x2e80 <= o <= 0xa4cf
            or 0xac00 <= o <= 0xd7a3
            or 0xf900 <= o <= 0xfaff
            or 0xfe30 <= o <= 0xfe4f
            or 0xff00 <= o <= 0xff60
            or 0xffe0 <= o <= 0xffe6
        )
        width += 2 if wide else 1
    return width


def _init_colors() -> dict:
    d = {
        "dim": 0, "green": 0, "cyan": 0, "blue": 0, "mag": 0, "yellow": 0, "sel": 0,
        "orange": 0, "pink": 0, "lime": 0, "gold": 0, "teal": 0, "violet": 0,
    }
    if not curses.has_colors():
        d["sel"] = curses.A_REVERSE
        _row_pairs.clear()
        return d
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = 0
    curses.init_pair(1, curses.COLOR_CYAN, bg)
    curses.init_pair(2, curses.COLOR_GREEN, bg)
    curses.init_pair(3, curses.COLOR_MAGENTA, bg)
    curses.init_pair(4, curses.COLOR_YELLOW, bg)
    curses.init_pair(5, curses.COLOR_BLUE, bg)
    d.update(
        cyan=curses.color_pair(1),
        green=curses.color_pair(2),
        mag=curses.color_pair(3),
        yellow=curses.color_pair(4),
        blue=curses.color_pair(5),
        dim=curses.A_DIM,
        sel=curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD,
    )

    has256 = getattr(curses, "COLORS", 0) >= 256

    def _named(pid: int, idx: int, fallback: int) -> int:
        if not has256:
            return fallback
        try:
            curses.init_pair(pid, idx, bg)
            return curses.color_pair(pid)
        except curses.error:
            return fallback

    # 命名鲜色（橙/粉/青柠/金/水鸭/紫罗兰），非 256 色时回退到基础色。
    d["orange"] = _named(60, 208, d["yellow"])
    d["pink"] = _named(61, 205, d["mag"])
    d["lime"] = _named(62, 118, d["green"])
    d["gold"] = _named(63, 220, d["yellow"])
    d["teal"] = _named(64, 44, d["cyan"])
    d["violet"] = _named(65, 141, d["mag"])
    # 更醒目的选中条：亮橙底黑字加粗（256 色）。
    if has256:
        try:
            curses.init_pair(66, 16, 208)
            d["sel"] = curses.color_pair(66) | curses.A_BOLD
        except curses.error:
            pass

    # 列表七彩色带：256 色逐行轮转，否则退化成基础多色循环。
    _row_pairs.clear()
    if has256:
        for i, cidx in enumerate(RAINBOW):
            pid = 40 + i
            try:
                curses.init_pair(pid, cidx, bg)
                _row_pairs.append(curses.color_pair(pid))
            except curses.error:
                pass
    if not _row_pairs:
        _row_pairs.extend([d["green"], d["cyan"], d["mag"], d["yellow"], d["blue"]])

    # 流动 logo 色带：256 色可用就上平滑渐变，否则退化成三色循环。
    _logo_pairs.clear()
    if has256:
        for i, cidx in enumerate(LOGO_GRAD):
            pid = 20 + i
            try:
                curses.init_pair(pid, cidx, bg)
                _logo_pairs.append(curses.color_pair(pid))
            except curses.error:
                pass
    if not _logo_pairs:
        _logo_pairs.extend([d["cyan"], d["blue"], d["mag"]])
    return d


def _wide_enough(win) -> bool:
    return win.getmaxyx()[1] >= 56


def _draw_logo(win, phase: int) -> None:
    """按 (列+行+phase) 取渐变色，逐字上色 → 横向流动。"""
    n = len(_logo_pairs) or 1
    h = win.getmaxyx()[0]
    if _wide_enough(win) and h > _HEADER_H + _LOGO_MIN_LIST:
        for r, line in enumerate(LOGO):
            for x, chx in enumerate(line):
                if chx == " ":
                    continue
                attr = _logo_pairs[(x + r + phase) % n] | curses.A_BOLD
                _addstr(win, _LOGO_TOP + r, 2 + x, chx, attr)
    else:
        _addstr(win, 0, 2, "◤ claude1 ◢", (_logo_pairs[phase % n]) | curses.A_BOLD)


def _intro(win) -> None:
    """开场：欢迎语常驻，logo 自左向右逐列点亮，七彩色带同时横向流动，按任意键跳过。"""
    if not _wide_enough(win):
        return
    win.nodelay(True)
    n = len(_logo_pairs) or 1
    width = max((len(line) for line in LOGO), default=0)
    try:
        phase = 0
        col = 1
        while col <= width:
            _addstr(win, 0, 2, "欢迎回来", C.get("pink", 0) | curses.A_BOLD)
            for r, line in enumerate(LOGO):
                for x in range(min(col, len(line))):
                    chx = line[x]
                    if chx == " ":
                        continue
                    attr = _logo_pairs[(x + r + phase) % n] | curses.A_BOLD
                    if x >= col - 2:  # 领先扫描列：最新点亮的两列反白，形成一道流动的光
                        attr |= curses.A_REVERSE
                    _addstr(win, _LOGO_TOP + r, 2 + x, chx, attr)
            win.refresh()
            if win.getch() != -1:
                break
            time.sleep(0.02)
            col += 4
            phase += 1
    finally:
        win.nodelay(False)


# 画大 ASCII logo 所需的列表最小高度（窗口更矮则退化成小 logo）。
_LOGO_MIN_LIST = 5


def _build_view(cfg, db_names, mru, show_hidden):
    """当前视图的 provider 名列表：只含真实存在于 CC-Switch 的号，按使用率排序。"""
    meta = cfg["providers"]
    names = [
        n for n in meta
        if n in db_names and (show_hidden or not meta[n].get("hidden"))
    ]
    names.sort(key=lambda n: -mru.get(n, 0.0))
    return names


def _edit_alias(win, name, meta) -> None:
    h, _ = win.getmaxyx()
    prompt = f"别名 {name} (空=清除): "
    curses.curs_set(1)
    curses.echo()
    _addstr(win, h - 1, 2, prompt, C.get("cyan", 0))
    win.refresh()
    try:
        raw = win.getstr(h - 1, 2 + len(prompt), 40)
        s = raw.decode("utf-8", "ignore").strip()
    except Exception:
        s = ""
    finally:
        curses.noecho()
        curses.curs_set(0)
    if s:
        meta[name]["alias"] = s
    else:
        meta[name].pop("alias", None)


def _confirm(win, msg) -> bool:
    """底部 y/n 选择条：←→ 或 y/n 切换，回车确认，Esc 取消。默认 n。"""
    h, _ = win.getmaxyx()
    choice = False
    while True:
        _addstr(win, h - 1, 0, " " * (win.getmaxyx()[1] - 1))
        _addstr(win, h - 1, 2, msg + "  ", C.get("yellow", 0) | curses.A_BOLD)
        base = 2 + len(msg) + 2
        _addstr(win, h - 1, base, " y ", C.get("sel", curses.A_REVERSE) if choice else C.get("dim", 0))
        _addstr(win, h - 1, base + 4, " n ", C.get("sel", curses.A_REVERSE) if not choice else C.get("dim", 0))
        win.refresh()
        ch = win.getch()
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


def _draw_launcher(win, cfg, view, idx, show_hidden, mru, phase=0) -> None:
    meta = cfg["providers"]
    win.erase()
    h, w = win.getmaxyx()
    big = _wide_enough(win) and h > _HEADER_H + _LOGO_MIN_LIST

    if big:
        greet = "欢迎回来"
        _addstr(win, 0, 2, greet, C.get("pink", 0) | curses.A_BOLD)
        _addstr(win, 0, 3 + _dwidth(greet), "· 选择一个渠道开始使用 Claude 1", C.get("dim", 0))
        _draw_logo(win, phase)
        head = _LOGO_TOP + len(LOGO)
    else:
        _draw_logo(win, phase)
        head = 1

    hx = 2
    _addstr(win, head + 1, hx, "选择渠道", C.get("lime", 0) | curses.A_BOLD)
    hx += _dwidth("选择渠道") + 1
    if view:
        cur = view[idx]
        _addstr(win, head + 1, hx, "· 当前", C.get("dim", 0))
        hx += _dwidth("· 当前") + 1
        badge = f"› {cur}"
        _addstr(win, head + 1, hx, badge, C.get("orange", 0) | curses.A_BOLD)
        hx += _dwidth(badge) + 1
    if show_hidden:
        _addstr(win, head + 1, hx, "· 含隐藏项", C.get("dim", 0))
    _addstr(win, head + 2, 2, "↑↓ 选择 · Enter 启动", C.get("dim", 0))
    list_top = head + 4

    if not view:
        _addstr(win, list_top, 2, "（没有可用渠道）", C.get("yellow", 0))
    for i in range(len(view)):
        name = view[i]
        m = meta[name]
        hidden = m.get("hidden")
        rank = i + 1
        selected = i == idx
        marker = "▸" if selected else " "
        row_col = _row_pairs[i % len(_row_pairs)] | curses.A_BOLD if _row_pairs else C.get("green", 0) | curses.A_BOLD
        if hidden:
            dot, dot_attr = "·", C.get("dim", 0)
        elif mru.get(name):
            dot, dot_attr = "★", row_col
        else:
            dot, dot_attr = "◆", row_col
        label = name
        if m.get("alias"):
            label += f"  «{m['alias']}»"
        if hidden:
            label += "  (已隐藏)"
        row = list_top + i
        if selected:
            line = f"{marker} {rank:>2}. {dot} {label}"
            _addstr(win, row, 2, line.ljust(w - 4), C.get("sel", curses.A_REVERSE))
        else:
            name_attr = C.get("dim", 0) if hidden else row_col
            _addstr(win, row, 2, marker, C.get("dim", 0))
            _addstr(win, row, 4, f"{rank:>2}.", C.get("dim", 0))
            _addstr(win, row, 8, dot, dot_attr)
            _addstr(win, row, 10, label, name_attr)

    foot = f"共 {len(view)} 个   ·   a 别名 · x 隐藏 · h 隐藏项 · q 退出"
    _addstr(win, list_top + len(view) + 1, 2, foot, C.get("dim", 0))
    win.refresh()


def _launcher_main(win, cfg, db_names):
    """返回要启动的 provider 名，或 None(退出不启动)。hide/alias 即时落盘。"""
    curses.curs_set(0)
    win.keypad(True)
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    global C
    C = _init_colors()
    _intro(win)
    mru = load_mru()
    meta = cfg["providers"]
    show_hidden = False
    view = _build_view(cfg, db_names, mru, show_hidden)
    idx = 0
    phase = 0
    anim = len(_logo_pairs) > 1  # 有多色才动画
    win.timeout(110 if anim else -1)  # 110ms 无输入即返回 -1 → 推进流动
    _draw_launcher(win, cfg, view, idx, show_hidden, mru, phase)
    while True:
        ch = win.getch()
        if ch == -1:  # 动画节拍：只重画 logo，避免整屏闪
            phase += 1
            _draw_logo(win, phase)
            win.refresh()
            continue
        if ch in (curses.KEY_UP, ord("k")):
            if view:
                idx = (idx - 1) % len(view)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if view:
                idx = (idx + 1) % len(view)
        elif ch == ord("a"):
            if view:
                win.timeout(-1)
                _edit_alias(win, view[idx], meta)
                win.timeout(110 if anim else -1)
                save_config(cfg)
        elif ch == ord("x"):
            if view:
                name = view[idx]
                nowh = meta[name].get("hidden")
                verb = "恢复显示" if nowh else "隐藏"
                win.timeout(-1)
                ok = _confirm(win, f"{verb} {name}?")
                win.timeout(110 if anim else -1)
                if ok:
                    meta[name]["hidden"] = not nowh
                    save_config(cfg)
                    view = _build_view(cfg, db_names, mru, show_hidden)
        elif ch == ord("h"):  # 切换「显示隐藏项」
            show_hidden = not show_hidden
            view = _build_view(cfg, db_names, mru, show_hidden)
            idx = 0
        elif ch in (10, 13, curses.KEY_ENTER):
            if view:
                return view[idx]
        elif ch in (27, ord("q")):
            return None
        idx = 0 if not view else max(0, min(idx, len(view) - 1))
        _draw_launcher(win, cfg, view, idx, show_hidden, mru, phase)


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
        BACKEND_STATE.parent.mkdir(parents=True, exist_ok=True)
        BACKEND_STATE.write_text(json.dumps(payload, ensure_ascii=False))
    except OSError:
        pass


def _atomic_write_sticky(kind: str) -> None:
    BACKEND_STICKY.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{BACKEND_STICKY.name}.",
        dir=str(BACKEND_STICKY.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(kind + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, BACKEND_STICKY)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
                else:
                    hint = arg
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
    print("[claude1] 后端: reclaude (yufeng 中转，隔离 CC-Switch)")
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


def exec_hub(claude_args: list[str]) -> int:
    """Launch one Claude session through the isolated multi-channel hub."""
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

    ensure_hub(port)
    main_model = f"{default_channel},{models[0]}"
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


def main(argv: list[str]) -> int:
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
