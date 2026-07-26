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
import secrets
import shlex
import shutil
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
from pathlib import Path
from urllib.parse import urlparse

from claude1_provider import (
    CapabilityProfile,
    ProviderPolicyError,
    capability_summary,
    configured_credential,
    prepare_provider_settings,
    resolve_capability_profile,
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
CC_SETTINGS_PATH = _env_path(
    "CLAUDE1_CC_SETTINGS_PATH", HOME / ".cc-switch" / "settings.json"
)
LIVE_SETTINGS_PATH_OVERRIDE = (
    _env_path("CLAUDE1_LIVE_SETTINGS_PATH", HOME / ".claude" / "settings.json")
    if os.environ.get("CLAUDE1_LIVE_SETTINGS_PATH")
    else None
)
MODEL_BACKUP_DIR = _env_path(
    "CLAUDE1_MODEL_BACKUP_DIR",
    DB_PATH.parent / "backups" / "claude1-model-editor",
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
TURN_GUARD_SCRIPT = _env_path(
    "CLAUDE1_TURN_GUARD_SCRIPT",
    Path(__file__).resolve().with_name("claude1-turn-guard.py"),
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


FRECENCY_HALF_LIFE_SECONDS = 7 * 24 * 3600.0  # 一周衰减一半，久不用的渠道自然下沉
FIXED_TOP_SLOTS = 3  # 列表前三保持 CC Switch 顺序，数字键 1-3 的肌肉记忆不随排名漂移


def _coerce_stat_entry(value) -> dict[str, float] | None:
    """兼容旧版纯时间戳与新版 {"last", "score"} 两种存储格式。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {"last": float(value), "score": 1.0}
    if isinstance(value, dict):
        last = value.get("last")
        score = value.get("score")
        if (
            isinstance(last, (int, float))
            and not isinstance(last, bool)
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
            and float(score) >= 0.0
        ):
            return {"last": float(last), "score": float(score)}
    return None


def load_use_stats() -> dict[str, dict[str, float]]:
    try:
        data = json.loads(MRU_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    stats: dict[str, dict[str, float]] = {}
    for key, value in data.items():
        entry = _coerce_stat_entry(value)
        if isinstance(key, str) and entry is not None:
            stats[key] = entry
    return stats


def load_mru() -> dict[str, float]:
    return {name: entry["last"] for name, entry in load_use_stats().items()}


def frecency_score(entry: dict[str, float] | None, now: float) -> float:
    """zoxide 式 frecency：分数随距上次使用的时间指数衰减，命中一次 +1。"""
    if not entry:
        return 0.0
    age = max(0.0, now - entry["last"])
    return entry["score"] * 0.5 ** (age / FRECENCY_HALF_LIFE_SECONDS)


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
    stats = load_use_stats()
    now = time.time()
    stats[name] = {
        "last": now,
        "score": frecency_score(stats.get(name), now) + 1.0,
    }
    try:
        _atomic_private_write(
            MRU_PATH,
            json.dumps(stats, ensure_ascii=False, indent=1),
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
            return _provider_from_row(r)
    return None


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
THIRD_PARTY_AMBIENT_CREDENTIAL_PREFIXES = (
    "OPENAI_",
    "GEMINI_",
    "GOOGLE_",
    "AZURE_",
    "AWS_",
)


def managed_key(key: str) -> bool:
    folded = key.upper()
    return folded in MANAGED_ENV_KEYS or any(
        folded.startswith(prefix) for prefix in MANAGED_ENV_PREFIXES
    )


def claude_child_env(settings: dict | None = None) -> dict[str, str]:
    """Build a Claude process environment without inherited routing state."""
    settings_env = settings.get("env") if isinstance(settings, dict) else None
    third_party = (
        isinstance(settings_env, dict)
        and bool(settings_env.get("ANTHROPIC_BASE_URL"))
    )
    child = {
        key: value
        for key, value in os.environ.items()
        if not managed_key(key)
        and not (
            third_party
            and key.upper().startswith(
                THIRD_PARTY_AMBIENT_CREDENTIAL_PREFIXES
            )
        )
    }
    for key in CLAUDE_CHILD_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            child[key] = value

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
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(providers)")
        }
        selected = ["id", "name", "settings_config"]
        selected.extend(
            column for column in ("meta", "provider_type") if column in columns
        )
        return conn.execute(
            f"SELECT {', '.join(selected)} FROM providers "
            "WHERE app_type='claude' ORDER BY sort_index"
        ).fetchall()
    finally:
        conn.close()


def _provider_from_row(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "name": row["name"],
        "settings_config": row["settings_config"],
        "meta": row["meta"] if "meta" in keys else "{}",
        "provider_type": row["provider_type"] if "provider_type" in keys else None,
    }


def selected_provider_api_format(provider: dict) -> str:
    return str(selected_provider_capabilities(provider).get("protocol"))


def selected_provider_capabilities(provider: dict) -> CapabilityProfile:
    try:
        settings = json.loads(provider.get("settings_config") or "{}")
    except (json.JSONDecodeError, TypeError):
        settings = {}
    try:
        meta = json.loads(provider.get("meta") or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    provider_meta = load_config().get("providers", {}).get(provider.get("name"))
    capabilities = (
        provider_meta.get("capabilities")
        if isinstance(provider_meta, dict)
        else None
    )
    return resolve_capability_profile(
        meta=meta,
        settings=settings,
        provider_type=provider.get("provider_type"),
        override=capabilities,
    )


def list_providers() -> list[dict]:
    rows = db_claude_rows()
    by_name = {r["name"]: _provider_from_row(r) for r in rows}
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


MODEL_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_MODEL", "主模型"),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "Opus"),
    ("ANTHROPIC_DEFAULT_FABLE_MODEL", "Fable"),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "Sonnet"),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "Haiku"),
    ("ANTHROPIC_REASONING_MODEL", "Reasoning"),
)
MODEL_VALUE_MAX_LENGTH = 200
MODEL_LABEL_WIDTH = 10
MODEL_FIELD_COLORS = {
    "ANTHROPIC_MODEL": "orange",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "violet",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "pink",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "teal",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "lime",
    "ANTHROPIC_REASONING_MODEL": "gold",
}


class ModelEditorError(RuntimeError):
    """Base error for safe, user-facing model editor failures."""


class ModelConflictError(ModelEditorError):
    """The provider changed after the editor loaded its snapshot."""


class ModelValidationError(ModelEditorError):
    """The proposed model value is unsafe or invalid."""


@dataclass(frozen=True)
class ProviderModelField:
    key: str
    label: str
    value: str


@dataclass(frozen=True)
class ProviderModelSnapshot:
    provider_id: str
    provider_name: str
    raw_settings_config: str
    fields: tuple[ProviderModelField, ...]

    def value_for(self, key: str) -> str:
        for field in self.fields:
            if field.key == key:
                return field.value
        raise KeyError(key)


def _model_snapshot_from_raw(
    provider_id: str,
    provider_name: str,
    raw_settings_config: str,
) -> ProviderModelSnapshot:
    try:
        settings = json.loads(raw_settings_config)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ModelEditorError("Provider 配置 JSON 损坏，已停止编辑") from exc
    if not isinstance(settings, dict):
        raise ModelEditorError("Provider 配置不是 JSON object，已停止编辑")
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    fields = tuple(
        ProviderModelField(key=key, label=label, value=value)
        for key, label in MODEL_ENV_FIELDS
        if isinstance((value := env.get(key)), str)
    )
    return ProviderModelSnapshot(
        provider_id=provider_id,
        provider_name=provider_name,
        raw_settings_config=raw_settings_config,
        fields=fields,
    )


def load_provider_model_snapshot(provider_name: str) -> ProviderModelSnapshot:
    """Load one editable CC Switch Claude provider without exposing credentials."""
    if not DB_PATH.exists():
        raise ModelEditorError("CC Switch DB 不存在")
    db_uri = DB_PATH.resolve(strict=False).as_uri() + "?mode=ro"
    connection = sqlite3.connect(db_uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT id, name, settings_config FROM providers "
            "WHERE app_type='claude' AND name=?",
            (provider_name,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ModelEditorError("无法读取 CC Switch Provider") from exc
    finally:
        connection.close()
    if not rows:
        raise ModelEditorError("Provider 已不存在，请返回后重新加载")
    if len(rows) != 1:
        raise ModelEditorError("Provider 名称重复，无法安全定位目标记录")
    provider_id, name, raw_settings_config = rows[0]
    return _model_snapshot_from_raw(provider_id, name, raw_settings_config)


def _safe_model_summary(value: str) -> str:
    folded = value.casefold()
    if (
        "://" in value
        or folded.startswith(("sk-", "bearer ", "token "))
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        return "已配置"
    return _truncate_display(value, 28)


def provider_model_summaries() -> dict[str, str]:
    """Return only display-safe primary model values, never credentials."""
    summaries: dict[str, str] = {}
    for row in db_claude_rows():
        provider = _provider_from_row(row)
        try:
            snapshot = _model_snapshot_from_raw(
                provider["id"],
                provider["name"],
                provider["settings_config"],
            )
        except ModelEditorError:
            continue
        if snapshot.fields:
            primary = next(
                (
                    field.value
                    for field in snapshot.fields
                    if field.key == "ANTHROPIC_MODEL"
                ),
                snapshot.fields[0].value,
            )
            summaries[provider["name"]] = _safe_model_summary(primary)
    return summaries


def validate_model_value(value: str) -> str:
    if not isinstance(value, str):
        raise ModelValidationError("模型值必须是文本")
    if not value:
        raise ModelValidationError("模型值不能为空")
    if value != value.strip():
        raise ModelValidationError("模型值首尾不能包含空白")
    if len(value) > MODEL_VALUE_MAX_LENGTH:
        raise ModelValidationError(
            f"模型值不能超过 {MODEL_VALUE_MAX_LENGTH} 个字符"
        )
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ModelValidationError("模型值不能包含控制字符")
    return value


def _create_private_db_backup() -> Path:
    try:
        source_info = DB_PATH.lstat()
    except OSError as exc:
        raise ModelEditorError("无法读取 CC Switch DB") from exc
    if not stat.S_ISREG(source_info.st_mode):
        raise ModelEditorError("CC Switch DB 路径不是普通文件")
    backup_path: Path | None = None
    try:
        MODEL_BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_info = MODEL_BACKUP_DIR.lstat()
        if not stat.S_ISDIR(directory_info.st_mode):
            raise OSError("backup path is not a directory")
        if os.name == "posix":
            os.chmod(MODEL_BACKUP_DIR, 0o700)
        backup_path = MODEL_BACKUP_DIR / (
            f"cc-switch-before-model-edit-{time.time_ns()}-"
            f"{secrets.token_hex(4)}.db"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(backup_path, flags, 0o600)
        os.close(fd)
        source_uri = DB_PATH.resolve(strict=True).as_uri() + "?mode=ro"
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(source_uri, uri=True)
            destination = sqlite3.connect(backup_path)
            source.backup(destination)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        if os.name == "posix":
            os.chmod(backup_path, 0o600)
        return backup_path
    except (OSError, sqlite3.Error) as exc:
        if backup_path is not None:
            try:
                backup_path.unlink()
            except OSError:
                pass
        raise ModelEditorError("创建 CC Switch 备份失败，未执行保存") from exc


def _read_regular_text(path: Path, label: str) -> str:
    fd: int | None = None
    try:
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise OSError("path is not a regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("opened path is not a regular file")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("path changed while opening")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            return handle.read()
    except (OSError, UnicodeError) as exc:
        raise ModelEditorError(f"{label}无法安全读取，未执行保存") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _load_cc_switch_settings() -> dict:
    if not CC_SETTINGS_PATH.exists():
        return {}
    raw = _read_regular_text(CC_SETTINGS_PATH, "CC Switch 本地设置")
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelEditorError("CC Switch 本地设置 JSON 损坏，未执行保存") from exc
    if not isinstance(settings, dict):
        raise ModelEditorError("CC Switch 本地设置格式无效，未执行保存")
    return settings


def _effective_current_provider_id(
    connection: sqlite3.Connection,
    cc_settings: dict,
) -> str | None:
    local_id = cc_settings.get("currentProviderClaude")
    if isinstance(local_id, str) and local_id:
        exists = connection.execute(
            "SELECT 1 FROM providers WHERE id=? AND app_type='claude'",
            (local_id,),
        ).fetchone()
        if exists is not None:
            return local_id
    row = connection.execute(
        "SELECT id FROM providers "
        "WHERE app_type='claude' AND is_current=1 LIMIT 1"
    ).fetchone()
    return str(row[0]) if row is not None else None


def _claude_live_settings_path(cc_settings: dict) -> Path:
    if LIVE_SETTINGS_PATH_OVERRIDE is not None:
        return LIVE_SETTINGS_PATH_OVERRIDE
    configured = cc_settings.get("claudeConfigDir")
    config_dir = (
        Path(configured).expanduser()
        if isinstance(configured, str) and configured.strip()
        else HOME / ".claude"
    )
    settings_path = config_dir / "settings.json"
    legacy_path = config_dir / "claude.json"
    if settings_path.exists() or not legacy_path.exists():
        return settings_path
    return legacy_path


def _patch_model_in_json(
    raw: str,
    field_key: str,
    old_value: str,
    new_value: str,
    *,
    label: str,
) -> str:
    try:
        settings = json.loads(raw)
        env = settings["env"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ModelConflictError(f"{label}结构已变化，请重新加载") from exc
    if not isinstance(env, dict) or env.get(field_key) != old_value:
        raise ModelConflictError(f"{label}模型值已变化，请重新加载")
    env[field_key] = new_value
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


def save_provider_model(
    snapshot: ProviderModelSnapshot,
    field_key: str,
    new_value: str,
) -> ProviderModelSnapshot:
    """Compare-and-swap one model field in CC Switch's SQLite source of truth."""
    value = validate_model_value(new_value)
    if field_key not in {field.key for field in snapshot.fields}:
        raise ModelValidationError("该模型字段已不存在，请重新加载")
    if value == snapshot.value_for(field_key):
        return snapshot
    _create_private_db_backup()

    connection = sqlite3.connect(DB_PATH, timeout=3, isolation_level=None)
    live_restore: tuple[Path, str] | None = None
    live_written = False
    proxy_backup_update: tuple[str, str] | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT name, settings_config FROM providers "
            "WHERE id=? AND app_type='claude'",
            (snapshot.provider_id,),
        ).fetchone()
        if row is None:
            raise ModelConflictError("Provider 已被删除，请重新加载")
        current_name, current_raw = row
        if (
            current_name != snapshot.provider_name
            or current_raw != snapshot.raw_settings_config
        ):
            raise ModelConflictError(
                "CC Switch 中的 Provider 已被其他进程修改，请重新加载"
            )
        try:
            updated = json.loads(current_raw)
            env = updated["env"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ModelConflictError("Provider 配置结构已变化，请重新加载") from exc
        if not isinstance(env, dict) or field_key not in env:
            raise ModelConflictError("模型字段已变化，请重新加载")
        env[field_key] = value
        updated_raw = json.dumps(
            updated,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cc_settings = _load_cc_switch_settings()
        is_current = (
            _effective_current_provider_id(connection, cc_settings)
            == snapshot.provider_id
        )
        if is_current:
            takeover_row = connection.execute(
                "SELECT live_takeover_active FROM proxy_config "
                "WHERE app_type='claude'"
            ).fetchone()
            takeover_active = bool(takeover_row and takeover_row[0])
            backup_row = connection.execute(
                "SELECT original_config FROM proxy_live_backup "
                "WHERE app_type='claude'"
            ).fetchone()
            if takeover_active or backup_row is not None:
                if backup_row is None:
                    raise ModelEditorError(
                        "当前 Provider 正由 CC Switch 代理接管，"
                        "但恢复备份缺失，未执行保存"
                    )
                backup_raw = str(backup_row[0])
                backup_updated = _patch_model_in_json(
                    backup_raw,
                    field_key,
                    snapshot.value_for(field_key),
                    value,
                    label="CC Switch 代理恢复备份",
                )
                proxy_backup_update = (backup_raw, backup_updated)
            else:
                live_path = _claude_live_settings_path(cc_settings)
                live_raw = _read_regular_text(live_path, "Claude live 配置")
                live_updated = _patch_model_in_json(
                    live_raw,
                    field_key,
                    snapshot.value_for(field_key),
                    value,
                    label="Claude live 配置",
                )
                live_restore = (live_path, live_raw)

        changed = connection.execute(
            "UPDATE providers SET settings_config=? "
            "WHERE id=? AND app_type='claude' AND settings_config=?",
            (updated_raw, snapshot.provider_id, snapshot.raw_settings_config),
        ).rowcount
        if changed != 1:
            raise ModelConflictError(
                "CC Switch 中的 Provider 已被其他进程修改，请重新加载"
            )
        if proxy_backup_update is not None:
            backup_raw, backup_updated = proxy_backup_update
            backup_changed = connection.execute(
                "UPDATE proxy_live_backup "
                "SET original_config=?, backed_up_at=? "
                "WHERE app_type='claude' AND original_config=?",
                (
                    backup_updated,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    backup_raw,
                ),
            ).rowcount
            if backup_changed != 1:
                raise ModelConflictError(
                    "CC Switch 代理恢复备份已变化，请重新加载"
                )
        if live_restore is not None:
            _atomic_private_write(live_restore[0], live_updated)
            live_written = True
        connection.commit()
    except ModelEditorError:
        connection.rollback()
        if live_written and live_restore is not None:
            try:
                _atomic_private_write(live_restore[0], live_restore[1])
            except OSError as restore_exc:
                raise ModelEditorError(
                    "保存失败且 Claude live 配置回滚失败；"
                    "请从刚创建的私有备份恢复并重新打开 CC Switch"
                ) from restore_exc
        raise
    except (OSError, sqlite3.Error) as exc:
        connection.rollback()
        if live_written and live_restore is not None:
            try:
                _atomic_private_write(live_restore[0], live_restore[1])
            except OSError as restore_exc:
                raise ModelEditorError(
                    "保存失败且 Claude live 配置回滚失败；"
                    "请从刚创建的私有备份恢复并重新打开 CC Switch"
                ) from restore_exc
        raise ModelEditorError("无法保存到 CC Switch，原配置保持不变") from exc
    finally:
        connection.close()
    return _model_snapshot_from_raw(
        snapshot.provider_id,
        snapshot.provider_name,
        updated_raw,
    )


def build_settings(provider: dict) -> dict:
    """Return the provider settings_config from CC Switch DB with NO_PROXY applied."""
    cfg = json.loads(provider["settings_config"] or "{}")
    env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}

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


def add_turn_guard_hooks(settings: dict) -> None:
    """Inject the private Stop guard into one in-memory settings object."""
    if not TURN_GUARD_SCRIPT.is_file():
        raise RuntimeError("Turn Guard 已启用，但脚本不存在")
    hooks = settings.setdefault("hooks", {})
    command_prefix = (
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(TURN_GUARD_SCRIPT))}"
    )
    commands = {
        "Stop": f"{command_prefix} stop",
        "StopFailure": f"{command_prefix} failure",
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


def add_provider_turn_guard(settings: dict, provider_name: str) -> None:
    """Inject the Stop guard only for a locally opted-in provider."""
    provider_meta = load_config().get("providers", {}).get(provider_name)
    if (
        isinstance(provider_meta, dict)
        and provider_meta.get("turn_guard") is True
    ):
        add_turn_guard_hooks(settings)


def settings_backend_turn_guard_enabled(label: str) -> bool:
    backend_meta = load_config().get("backends", {}).get(label)
    return (
        isinstance(backend_meta, dict)
        and backend_meta.get("turn_guard") is True
    )


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
# 待机呼吸：交互后以低帧率继续流动一小段时间，然后回到零唤醒阻塞。
# 150ms/帧 ≈ 6.7fps，每帧只补画 logo 与顶线（curses 只重传变化的格子）。
IDLE_FRAME_MS = 150
IDLE_BREATH_SECONDS = 8.0
LOGO_BREATH_LEVELS = (
    0, 0, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 0, 0,
)
C: dict = {}

# Logo 与顶线共用全光谱流动渐变；列表行沿同一色相环按序取色，
# 相邻行相邻色相 —— 有序的渐变是设计，随机的轮换才是噪音。
LOGO_GRAD = [
    196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 48,
    50, 51, 45, 39, 33, 63, 99, 135, 171, 207, 201, 199,
]
_logo_pairs: list[int] = []
# 亮度一致的高饱和色环（红珊瑚 → 橙 → 金 → 绿 → 青 → 蓝 → 紫 → 玫红）。
RAINBOW = [203, 209, 215, 221, 155, 84, 49, 45, 75, 105, 135, 171, 205, 199]
_row_pairs: list[int] = []
_row_sel_pairs: list[int] = []


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


def _compose_row_segments(
    left: str,
    right: str,
    width: int,
) -> tuple[str, str, int]:
    """Split one row into left / right segments plus the gap between them."""
    if width <= 0:
        return ("", "", 0)
    if not right:
        return (_truncate_display(left, width), "", 0)
    right = _truncate_display(right, width)
    right_width = _dwidth(right)
    if right_width + 2 >= width:
        return (_truncate_display(left, width), "", 0)
    left = _truncate_display(left, width - right_width - 2)
    gap = max(2, width - _dwidth(left) - right_width)
    return (left, right, gap)


def _compose_row(left: str, right: str, width: int) -> str:
    """Fit one provider row, keeping its short status aligned when possible."""
    left_part, right_part, gap = _compose_row_segments(left, right, width)
    if not right_part:
        return left_part
    return _truncate_display(left_part + (" " * gap) + right_part, width)


def _draw_hints(win, row: int, x: int, parts) -> int:
    """键位提示：键名用主色、说明用暗灰，扫一眼就能找到键。

    parts 是 (key, desc) 序列；key 为空串时整段按说明文字渲染。
    返回绘制结束后的 x 列，便于调用方接排后续内容。
    """
    first = True
    for key, desc in parts:
        if not first:
            _addstr(win, row, x, " · ", C.get("dim", 0))
            x += 3
        first = False
        if key:
            _addstr(win, row, x, key, C.get("accent", 0))
            x += _dwidth(key)
            if desc:
                _addstr(win, row, x, f" {desc}", C.get("dim", 0))
                x += 1 + _dwidth(desc)
        elif desc:
            _addstr(win, row, x, desc, C.get("dim", 0))
            x += _dwidth(desc)
    return x


def _draw_top_rule(win, phase: int) -> None:
    """顶部渐变饰线 + 嵌入品牌名，是面板的第一眼轮廓。

    名牌逐字符取渐变色并随 phase 一起流动，与 logo 同源同动。
    """
    _h, w = win.getmaxyx()
    n = len(_logo_pairs) or 1
    for x in range(max(0, w - 1)):
        attr = _logo_pairs[(x + phase) % n]
        try:
            win.addstr(0, x, "━", attr)
        except curses.error:
            pass
    for i, chx in enumerate(" claude1 "):
        _addstr(
            win,
            0,
            3 + i,
            chx,
            _logo_pairs[(3 + i + phase) % n] | curses.A_BOLD,
        )


def _draw_bottom_rule(win, row: int, parts) -> None:
    """底部细线内嵌键位提示，收住面板的下缘。"""
    _h, w = win.getmaxyx()
    _addstr(win, row, 0, "─" * max(0, w - 1), C.get("dim", 0))
    _addstr(win, row, 2, " ", 0)
    end = _draw_hints(win, row, 3, parts)
    _addstr(win, row, end, " ", 0)


def _draw_section_label(win, row: int, text: str, width: int) -> None:
    """`─ 标题 ────` 式的细线分组，代替光秃秃的一行小字。"""
    fill = max(0, min(width, 40) - _dwidth(text) - 3)
    _addstr(win, row, 2, f"─ {text} " + "─" * fill, C.get("dim", 0))


def _safe_curs_set(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except (AttributeError, curses.error):
        pass


def _set_raw_input(enabled: bool) -> None:
    """Let INSERT receive Ctrl+C as a key, then restore normal cbreak mode."""
    try:
        if enabled:
            curses.raw()
        else:
            curses.cbreak()
    except (AttributeError, curses.error):
        pass


def _init_colors() -> dict:
    d = {
        "dim": 0,
        "base": 0,
        "accent": 0,
        "warning": 0,
        "error": 0,
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
    _row_sel_pairs.clear()
    try:
        has_colors = curses.has_colors()
    except curses.error:
        has_colors = False
    if not has_colors:
        _logo_pairs.append(0)
        return d
    try:
        curses.start_color()
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = 0

    def _pair(pid: int, fg: int, fallback: int = 0, pair_bg: int | None = None) -> int:
        max_pairs = int(getattr(curses, "COLOR_PAIRS", 0) or 0)
        if max_pairs and pid >= max_pairs:
            return fallback
        try:
            curses.init_pair(pid, fg, bg if pair_bg is None else pair_bg)
            return curses.color_pair(pid)
        except curses.error:
            return fallback

    cyan = _pair(1, curses.COLOR_CYAN)
    yellow = _pair(3, curses.COLOR_YELLOW)
    magenta = _pair(4, curses.COLOR_MAGENTA)
    red = _pair(5, curses.COLOR_RED)
    d.update(
        dim=curses.A_DIM,
        base=0,  # 渠道名默认前景兜底；256 色下由彩虹渐变接管
        title=curses.A_BOLD,
        accent=cyan,
        warning=yellow,
        error=red | curses.A_BOLD,
        brand=magenta | curses.A_BOLD,
        sel=cyan | curses.A_REVERSE | curses.A_BOLD,
    )

    has256 = getattr(curses, "COLORS", 0) >= 256

    if has256:
        # 角色分色：标题是结构，用亮白；键名是操作，用青；
        # 品牌与 Hub 入口直接用渐变本身 —— 高饱和粉只留在色环里。
        d["title"] = _pair(71, 231, 0) | curses.A_BOLD
        d["accent"] = _pair(64, 45, cyan)
        d["brand"] = _pair(60, 205, magenta) | curses.A_BOLD
        # 结构层永远是灰阶：rank、meta、分组线靠它衬出彩色主体。
        d["dim"] = _pair(67, 243, curses.A_DIM)
        # 状态色用亮版，耀眼但只在需要时出现。
        d["lime"] = _pair(62, 118, d["base"])
        d["warning"] = _pair(63, 220, yellow)
        d["error"] = _pair(68, 203, red) | curses.A_BOLD
        # 家族/字段辅色（信息色，保持鲜艳可辨）。
        d["pink"] = _pair(61, 205, magenta)
        d["gold"] = _pair(69, 220, yellow)
        d["teal"] = _pair(65, 45, cyan)
        d["violet"] = _pair(66, 135, magenta)
        d["orange"] = _pair(70, 209, yellow)
        # 列表行：沿色相环按序取色；选中行翻转为黑字彩底，
        # 底色继承该行自己的色相 —— 焦点耀眼且与整体渐变连续。
        for i, cidx in enumerate(RAINBOW):
            pair = _pair(80 + i, cidx, 0)
            if pair:
                _row_pairs.append(pair)
            sel_pair = _pair(110 + i, 16, 0, pair_bg=cidx)
            if sel_pair:
                _row_sel_pairs.append(sel_pair)
        if _row_sel_pairs:
            d["sel"] = _row_sel_pairs[0] | curses.A_BOLD
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


def _idle_breath_seconds() -> float:
    """待机呼吸时长；单色（无 256 色渐变）屏没有可见动画，保持零唤醒。

    CLAUDE1_BREATH_SECONDS 可调时长，设 0 只保留入场动画。
    """
    if not _animation_enabled() or len(_logo_pairs) <= 1:
        return 0.0
    raw = os.environ.get("CLAUDE1_BREATH_SECONDS", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return IDLE_BREATH_SECONDS


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
            _draw_top_rule(win, phase)
            typed = min(
                len("欢迎回来"),
                max(1, int((elapsed / 0.12) * len("欢迎回来"))),
            )
            _addstr(
                win,
                1,
                2,
                _pad_display("欢迎回来"[:typed], _dwidth("欢迎回来")),
                C.get("dim", 0),
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


def _build_view(cfg, db_names, stats, show_hidden):
    """前 FIXED_TOP_SLOTS 个保持 CC Switch 顺序，其余按 frecency 降序。

    stats 接受 load_use_stats() 的完整格式，也兼容旧的纯时间戳映射。
    平分时回落到 CC Switch 顺序，保证排序稳定。
    """
    meta = cfg["providers"]
    base = [
        n for n in meta
        if n in db_names and (show_hidden or not meta[n].get("hidden"))
    ]
    head = base[:FIXED_TOP_SLOTS]
    tail = base[FIXED_TOP_SLOTS:]
    now = time.time()
    config_order = {name: index for index, name in enumerate(base)}
    tail.sort(
        key=lambda name: (
            -frecency_score(_coerce_stat_entry(stats.get(name)), now),
            config_order[name],
        )
    )
    return head + tail


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
    notice: "str | tuple[str, str] | None" = None,
    logo_phase: int = 0,
    logo_breathing: bool = False,
    hub_status: "HubStatus | None" = None,
    hub_focus: bool = False,
    model_summaries: dict[str, str] | None = None,
) -> None:
    meta = cfg["providers"]
    model_summaries = model_summaries or {}
    win.erase()
    h, w = win.getmaxyx()
    big = _large_logo_supported(h, w)

    if show_brand and big:
        _draw_top_rule(win, logo_phase)
        _addstr(win, 1, 2, "欢迎回来", C.get("dim", 0))
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
            C.get("title", curses.A_BOLD),
        )
        head = 1

    heading = "选择本次渠道"
    _addstr(win, head + 1, 2, heading, C.get("title", curses.A_BOLD))
    if show_hidden:
        _addstr(
            win,
            head + 1,
            3 + _dwidth(heading),
            "· 含隐藏项",
            C.get("dim", 0),
        )
    guide = [("↑↓/jk", "移动"), ("→", "编辑模型"), ("Enter", "启动"), ("1-9", "直达")]
    if hub_status is not None:
        guide = [
            ("↑↓/jk", "移动"),
            ("→", "编辑模型"),
            ("Enter", "进入 Hub / 启动"),
            ("1-9", "直达"),
        ]
    _draw_hints(win, head + 2, 2, guide)
    row_cursor = head + 4
    if hub_status is not None:
        _draw_section_label(win, row_cursor, "多渠道会话", max(0, w - 4))
        row_cursor += 1
        entry_head = "◆ Claude-Hub"
        entry_tail = (
            f" · 多渠道会话 · {hub_status.summary}"
            " · 会话内 /model 切换"
        )
        entry_width = max(0, w - 4)
        if hub_focus:
            _addstr(
                win,
                row_cursor,
                2,
                _pad_display(entry_head + entry_tail, entry_width),
                C.get("sel", curses.A_REVERSE),
            )
        else:
            # 词头逐字符走渐变（Hub = 全部渠道的集合），说明文字退灰。
            n = len(_logo_pairs) or 1
            x = 2
            for i, chx in enumerate(_truncate_display(entry_head, entry_width)):
                _addstr(
                    win,
                    row_cursor,
                    x,
                    chx,
                    _logo_pairs[i % n] | curses.A_BOLD,
                )
                x += _dwidth(chx)
            tail_width = max(0, entry_width - _dwidth(entry_head))
            _addstr(
                win,
                row_cursor,
                x,
                _truncate_display(entry_tail, tail_width),
                C.get("dim", 0),
            )
        row_cursor += 2
        _draw_section_label(win, row_cursor, "单渠道直连", max(0, w - 4))
        row_cursor += 1
    list_top = row_cursor
    footer_row = max(0, h - 1)
    # Notice 独占 footer 上一行，不再挤掉键位提示。
    notice_row = footer_row - 1 if notice and footer_row - 1 >= list_top else None
    capacity = max(0, (notice_row if notice_row is not None else footer_row) - list_top)
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
        selected = (not hub_focus) and (i == idx)
        marker = "▌" if selected else " "
        label = f"{marker} {rank:>2}  {name}"
        status: list[str] = []
        if m.get("alias"):
            status.append(str(m["alias"]))
        if name == recent:
            status.append("最近")
        if hidden:
            status.append("已隐藏")
        if model_summaries.get(name):
            status.append(f"模型 {model_summaries[name]}")
        row = list_top + row_offset
        if selected:
            line = _compose_row(label, " · ".join(status), row_width)
            sel_attr = (
                _row_sel_pairs[i % len(_row_sel_pairs)] | curses.A_BOLD
                if _row_sel_pairs
                else C.get("sel", curses.A_REVERSE)
            )
            _addstr(
                win,
                row,
                2,
                _pad_display(line, row_width),
                sel_attr,
            )
        else:
            # 分段绘制建立层级：rank 与 meta 退灰，渠道名沿色相环渐变。
            left_part, right_part, gap = _compose_row_segments(
                label, " · ".join(status), row_width
            )
            prefix, name_part = left_part[:6], left_part[6:]
            if hidden:
                name_attr = C.get("dim", 0)
            elif _row_pairs:
                name_attr = _row_pairs[i % len(_row_pairs)] | curses.A_BOLD
            else:
                name_attr = C.get("base", 0)
            _addstr(win, row, 2, prefix, C.get("dim", 0))
            _addstr(win, row, 8, name_part, name_attr)
            if right_part:
                _addstr(
                    win,
                    row,
                    2 + _dwidth(left_part) + gap,
                    right_part,
                    C.get("dim", 0),
                )

    if notice_row is not None:
        kind, text = notice if isinstance(notice, tuple) else ("warn", notice)
        icon = {"ok": "✓", "error": "✗"}.get(kind, "!")
        notice_attr = {
            "ok": C.get("lime", 0),
            "error": C.get("error", 0),
        }.get(kind, C.get("warning", 0))
        _addstr(
            win,
            notice_row,
            2,
            _truncate_display(f"{icon} {text}", max(0, w - 4)),
            notice_attr,
        )
    if help_open:
        foot = [
            ("a", "设置别名"),
            ("x", "隐藏/显示"),
            ("h", "隐藏项"),
            ("→", "编辑模型"),
            ("Esc/q", "退出"),
            ("?", "返回"),
        ]
    else:
        visible_range = ""
        if start > 0 or end < len(view):
            visible_range = f" · {start + 1}–{end}/{len(view)}"
        foot = [
            ("", f"共 {len(view)} 个{visible_range}"),
            ("?", "更多操作"),
            ("q", "退出"),
        ]
    if big:
        _draw_bottom_rule(win, footer_row, foot)
    else:
        _draw_hints(win, footer_row, 2, foot)
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
    _draw_top_rule(win, 0)
    _addstr(
        win, 0, 3, " claude1 › Claude-Hub ", C.get("title", curses.A_BOLD)
    )
    _addstr(win, 1, 2, "选择 Hub 模型", C.get("title", curses.A_BOLD))
    if status.healthy is True:
        badge, badge_attr = "● 已就绪", C.get("lime", 0)
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
        marker = "▌" if i == idx else " "
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
            _addstr(win, row, 2, text, C.get(family_color, 0))
    _draw_bottom_rule(
        win,
        footer_row,
        [
            ("Esc", "返回 Claude1"),
            ("↑↓/jk", "选择"),
            ("Enter", "启动"),
            ("q", "退出"),
        ],
    )
    win.refresh()


def _model_input_view(
    value: str,
    cursor: int,
    width: int,
) -> tuple[str, int]:
    """Return a horizontally scrolling input viewport and cursor column."""
    if width <= 0:
        return ("", 0)
    cursor = max(0, min(cursor, len(value)))
    if width < 3:
        return (_truncate_display(value[cursor:], width), 0)

    inner_width = width - 2
    left_budget = max(1, inner_width // 2)
    start = cursor
    used = 0
    while start > 0:
        char_width = _char_width(value[start - 1])
        if used + char_width > left_budget:
            break
        start -= 1
        used += char_width

    end = cursor
    remaining = inner_width - used
    while end < len(value):
        char_width = _char_width(value[end])
        if char_width > remaining:
            break
        remaining -= char_width
        end += 1

    # Near the end, spend unused right-side space on more left-side context.
    while end == len(value) and start > 0 and remaining > 0:
        char_width = _char_width(value[start - 1])
        if char_width > remaining:
            break
        start -= 1
        used += char_width
        remaining -= char_width

    content = value[start:end]
    left = "‹" if start > 0 else " "
    right = "›" if end < len(value) else " "
    rendered = left + _pad_display(content, inner_width) + right
    cursor_column = 1 + _dwidth(value[start:cursor])
    return (rendered, min(cursor_column, max(0, width - 1)))


def _draw_model_editor(
    win,
    snapshot: ProviderModelSnapshot,
    idx: int,
    mode: str,
    buffer: str,
    cursor: int,
    notice: "str | tuple[str, str] | None",
    replace_on_type: bool = False,
) -> None:
    win.erase()
    h, w = win.getmaxyx()
    usable = max(0, w - 4)
    _addstr(
        win,
        0,
        2,
        _truncate_display(f"Provider 模型 · {snapshot.provider_name}", usable),
        C.get("title", curses.A_BOLD),
    )
    mode_attr = (
        C.get("warning", 0)
        if mode == "INSERT"
        else C.get("lime", 0)
    )
    _addstr(win, 2, 2, f"模式: {mode}", mode_attr | curses.A_BOLD)
    guide = (
        (
            "直接输入替换原值 · ←→ 定位保留旧值 · Enter 保存 · Esc 取消"
            if replace_on_type
            else "编辑文本 · Enter 保存 · Esc 取消 · ←→ 移动光标"
        )
        if mode == "INSERT"
        else "↑↓ / jk 选择 · Enter/i 编辑 · Esc/← 返回"
    )
    _addstr(win, 3, 2, _truncate_display(guide, usable), C.get("dim", 0))
    if notice:
        kind, text = notice if isinstance(notice, tuple) else ("warn", notice)
        notice_attr = {
            "ok": C.get("lime", 0),
            "error": C.get("error", 0),
        }.get(kind, C.get("warning", 0))
        _addstr(
            win,
            4,
            2,
            _truncate_display(text, usable),
            notice_attr | curses.A_BOLD,
        )

    input_row = 6
    if mode == "INSERT" and snapshot.fields:
        field = snapshot.fields[idx]
        edit_hint = f"编辑值 · {field.label}"
        if replace_on_type:
            edit_hint += " · 已选中原值"
        _addstr(
            win,
            5,
            2,
            _truncate_display(edit_hint, usable),
            C.get(MODEL_FIELD_COLORS.get(field.key, "accent"), 0)
            | curses.A_BOLD,
        )
        input_text, _cursor_column = _model_input_view(
            buffer,
            cursor,
            usable,
        )
        _addstr(
            win,
            input_row,
            2,
            _pad_display(input_text, usable),
            C.get(MODEL_FIELD_COLORS.get(field.key, "sel"), 0)
            | curses.A_REVERSE
            | curses.A_BOLD,
        )

    list_top = 8 if mode == "INSERT" else 6
    footer_row = max(0, h - 1)
    capacity = max(0, footer_row - list_top)
    start, end = _visible_window(len(snapshot.fields), idx, capacity)
    if not snapshot.fields:
        _addstr(
            win,
            list_top,
            2,
            _truncate_display(
                "此 Provider 没有可编辑的模型字段；Token 与地址不会显示。",
                usable,
            ),
            C.get("dim", 0),
        )
    for offset, field_index in enumerate(range(start, end)):
        field = snapshot.fields[field_index]
        selected = field_index == idx
        value = field.value
        label = (
            f"{'▸' if selected else ' '} "
            f"{_pad_display(field.label, MODEL_LABEL_WIDTH)}  {value}"
        )
        row = list_top + offset
        _addstr(
            win,
            row,
            2,
            _pad_display(_truncate_display(label, usable), usable),
            C.get("sel", curses.A_REVERSE)
            if selected and mode == "NORMAL"
            else (
                C.get(MODEL_FIELD_COLORS.get(field.key, "base"), 0)
                | curses.A_BOLD
            ),
        )

    footer = (
        f"{len(snapshot.fields)} 个模型字段 · 不显示 Token / 地址"
        if snapshot.fields
        else "← 返回 · q 返回"
    )
    _addstr(win, footer_row, 2, _truncate_display(footer, usable), C.get("dim", 0))

    if mode == "INSERT" and snapshot.fields and h > input_row:
        _input_text, cursor_column = _model_input_view(
            buffer,
            cursor,
            usable,
        )
        cursor_x = min(max(2, 2 + cursor_column), max(2, w - 2))
        if hasattr(win, "move"):
            try:
                win.move(input_row, cursor_x)
            except curses.error:
                pass
    win.refresh()


def _model_editor(win, provider_name: str) -> None:
    """Vim-style model editor for one CC Switch provider."""
    snapshot = load_provider_model_snapshot(provider_name)
    idx = 0
    mode = "NORMAL"
    buffer = ""
    cursor = 0
    replace_on_type = False
    notice: "str | tuple[str, str] | None" = None
    _safe_curs_set(0)
    while True:
        _draw_model_editor(
            win,
            snapshot,
            idx,
            mode,
            buffer,
            cursor,
            notice,
            replace_on_type,
        )
        ch = win.getch()
        if ch == -1:
            _set_raw_input(False)
            _safe_curs_set(0)
            return

        if mode == "NORMAL":
            if ch in (curses.KEY_UP, ord("k")) and snapshot.fields:
                idx = (idx - 1) % len(snapshot.fields)
            elif ch in (curses.KEY_DOWN, ord("j")) and snapshot.fields:
                idx = (idx + 1) % len(snapshot.fields)
            elif (
                ch in (ord("i"), 10, 13, curses.KEY_ENTER)
                and snapshot.fields
            ):
                mode = "INSERT"
                buffer = snapshot.fields[idx].value
                cursor = len(buffer)
                replace_on_type = True
                notice = None
                _set_raw_input(True)
                _safe_curs_set(1)
            elif ch in (curses.KEY_LEFT, ord("q"), 27):
                _set_raw_input(False)
                _safe_curs_set(0)
                return
            continue

        # INSERT: direction keys edit the buffer and never leave this page.
        if ch in (10, 13, curses.KEY_ENTER):
            try:
                snapshot = save_provider_model(
                    snapshot,
                    snapshot.fields[idx].key,
                    buffer,
                )
            except ModelConflictError as exc:
                try:
                    snapshot = load_provider_model_snapshot(provider_name)
                    idx = min(idx, max(0, len(snapshot.fields) - 1))
                except ModelEditorError:
                    pass
                mode = "NORMAL"
                replace_on_type = False
                notice = ("error", str(exc))
                _set_raw_input(False)
                _safe_curs_set(0)
            except ModelValidationError as exc:
                notice = ("error", str(exc))
            except ModelEditorError as exc:
                notice = ("error", str(exc))
            else:
                mode = "NORMAL"
                notice = ("ok", "已保存到 CC Switch")
                buffer = ""
                cursor = 0
                replace_on_type = False
                _set_raw_input(False)
                _safe_curs_set(0)
        elif ch in (27, 3):  # Esc / Ctrl+C: cancel, no save.
            mode = "NORMAL"
            notice = "已取消编辑"
            buffer = ""
            cursor = 0
            replace_on_type = False
            _set_raw_input(False)
            _safe_curs_set(0)
        elif ch == 21:  # Ctrl+U: clear the whole input buffer.
            buffer = ""
            cursor = 0
            replace_on_type = False
        elif ch in (curses.KEY_BACKSPACE, 8, 127):
            if replace_on_type:
                buffer = ""
                cursor = 0
                replace_on_type = False
            elif cursor > 0:
                buffer = buffer[: cursor - 1] + buffer[cursor:]
                cursor -= 1
        elif ch == curses.KEY_DC:
            if replace_on_type:
                buffer = ""
                cursor = 0
                replace_on_type = False
            elif cursor < len(buffer):
                buffer = buffer[:cursor] + buffer[cursor + 1 :]
        elif ch == curses.KEY_LEFT:
            replace_on_type = False
            cursor = max(0, cursor - 1)
        elif ch == curses.KEY_RIGHT:
            replace_on_type = False
            cursor = min(len(buffer), cursor + 1)
        elif ch == curses.KEY_HOME:
            replace_on_type = False
            cursor = 0
        elif ch == curses.KEY_END:
            replace_on_type = False
            cursor = len(buffer)
        elif isinstance(ch, int) and 32 <= ch < 127:
            char = chr(ch)
            if replace_on_type:
                buffer = char
                cursor = 1
                replace_on_type = False
                notice = None
            elif len(buffer) >= MODEL_VALUE_MAX_LENGTH:
                notice = (
                    f"模型值不能超过 {MODEL_VALUE_MAX_LENGTH} 个字符"
                )
            else:
                buffer = buffer[:cursor] + char + buffer[cursor:]
                cursor += 1


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
        # 其余按键（含 KEY_RESIZE）落到这里统一重绘，缩放终端不再花屏。
        _draw_hub_workspace(win, status, options, idx)


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
    stats = load_use_stats()
    mru = {name: entry["last"] for name, entry in stats.items()}
    meta = cfg["providers"]
    show_hidden = False
    view = _build_view(cfg, db_names, stats, show_hidden)
    idx = _initial_index(view, mru)
    hub_view = _load_hub_view()
    hub_status, hub_options = hub_view if hub_view is not None else (None, [])
    # 初始焦点跟随最近一次真实启动：只有上次走 hub 才聚焦 Hub 条目，
    # 否则落在 MRU 渠道上，保住「claude1 + Enter 启动最近渠道」的肌肉记忆。
    hub_focus = hub_view is not None and _last_backend_kind() == "hub"
    try:
        model_summaries = provider_model_summaries()
    except (ModelEditorError, RuntimeError, sqlite3.Error):
        model_summaries = {}
    help_open = False
    notice: "str | tuple[str, str] | None" = None
    rows, cols = win.getmaxyx()
    intro_animate = _animation_enabled() and _large_logo_supported(rows, cols)
    pending_key = _intro(win) if intro_animate else None
    # 待机呼吸是有窗口期的：交互后流动 IDLE_BREATH_SECONDS 秒，随后回到
    # 零唤醒阻塞等键，直到下一次按键再苏醒 —— 界面活着，空闲不耗电。
    breath_budget = _idle_breath_seconds()
    breathing_alive = breath_budget > 0
    phase = 0

    def _arm_breath() -> float:
        win.timeout(IDLE_FRAME_MS if breathing_alive else -1)
        return time.monotonic() + breath_budget

    breath_deadline = _arm_breath()
    animating = breathing_alive
    _draw_launcher(
        win,
        cfg,
        view,
        idx,
        show_hidden,
        mru,
        show_brand=True,
        logo_phase=phase,
        logo_breathing=animating,
        hub_status=hub_status,
        hub_focus=hub_focus,
        model_summaries=model_summaries,
    )
    while True:
        ch = pending_key if pending_key is not None else win.getch()
        pending_key = None
        if ch == -1:
            if not animating:
                # timeout(-1) returning -1 means the controlling terminal closed.
                return None
            if time.monotonic() >= breath_deadline:
                animating = False
                win.timeout(-1)
                continue
            # 呼吸帧：只补画 logo 与顶线，列表区一个字符都不重绘。
            phase += 1
            frame_rows, frame_cols = win.getmaxyx()
            if _large_logo_supported(frame_rows, frame_cols):
                _draw_top_rule(win, phase)
            _draw_logo(win, phase, breathing=True)
            win.refresh()
            continue
        # 真实按键：先恢复阻塞语义，让 Hub / 模型编辑 / 别名 / 确认等
        # 子界面在无定时器的环境里运行。
        if breathing_alive:
            win.timeout(-1)
        notice = None
        direct_index = _digit_index(ch)
        if direct_index is not None:
            if direct_index < len(view):
                return view[direct_index]
            notice = ("warn", f"没有第 {direct_index + 1} 个渠道")
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
                changed, alias_msg = _edit_alias(win, view[idx], meta)
                notice = ("ok" if changed else "warn", alias_msg)
                if changed:
                    save_config(cfg)
        elif ch == ord("x"):
            if not hub_focus and view:
                name = view[idx]
                nowh = meta[name].get("hidden")
                verb = "恢复显示" if nowh else "隐藏"
                ok = _confirm(win, f"{verb} {name}?")
                if ok:
                    meta[name]["hidden"] = not nowh
                    save_config(cfg)
                    preferred = name
                    view = _build_view(cfg, db_names, stats, show_hidden)
                    idx = _initial_index(view, mru, preferred)
        elif ch == ord("h"):  # 切换「显示隐藏项」
            preferred = view[idx] if view else None
            show_hidden = not show_hidden
            view = _build_view(cfg, db_names, stats, show_hidden)
            idx = _initial_index(view, mru, preferred)
        elif ch == ord("?"):
            help_open = not help_open
        elif ch == curses.KEY_RIGHT:
            if not hub_focus and view:
                try:
                    _model_editor(win, view[idx])
                except ModelEditorError as exc:
                    notice = ("error", str(exc))
                try:
                    model_summaries = provider_model_summaries()
                except (ModelEditorError, RuntimeError, sqlite3.Error):
                    pass
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
            logo_phase=phase,
            logo_breathing=breathing_alive,
            hub_status=hub_status,
            hub_focus=hub_focus,
            model_summaries=model_summaries,
        )
        # 每次交互都重新点亮一段呼吸窗口。
        breath_deadline = _arm_breath()
        animating = breathing_alive


def _launcher_session(win, cfg, db_names):
    """Run one chooser and remove its full-screen UI before curses restores."""
    try:
        return _launcher_main(win, cfg, db_names)
    finally:
        try:
            win.erase()
            win.refresh()
        except (AttributeError, curses.error):
            pass


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
        result = curses.wrapper(_launcher_session, cfg, db_names)
    except Exception as exc:
        print(f"[claude1] 图形界面无法启动({exc})", file=sys.stderr)
        return ("no-tui", None)
    if result is None:
        return ("quit", None)
    if isinstance(result, HubLaunch):
        return ("hub", result.option.selector)
    return ("launch", result)


def _last_backend_kind() -> str | None:
    """读取最近一次真实启动的后端类型；文件缺失或损坏时返回 None。"""
    try:
        payload = json.loads(BACKEND_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kind = payload.get("backend") if isinstance(payload, dict) else None
    return kind if isinstance(kind, str) else None


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
        elif low in ("--any", "--anyrouter"):
            backend = "anyrouter"
        elif low in ("--current", "--cc"):
            backend = "current"
        elif low == "--hub":
            backend = "hub"
        else:
            claude_args.append(arg)
    return backend, hint, claude_args


def exec_settings_backend(settings_path: Path, label: str, claude_args: list[str]) -> int:
    if not settings_path.exists():
        raise RuntimeError(f"{label} 配置不存在: {settings_path}")
    record_backend(label)
    print(f"[claude1] 后端: {label} ({settings_path.name})")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} 配置无法读取") from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"{label} 配置必须是 JSON object")
    backend_meta = load_config().get("backends", {}).get(label)
    capabilities = (
        backend_meta.get("capabilities")
        if isinstance(backend_meta, dict)
        else None
    )
    profile = resolve_capability_profile(
        settings=settings,
        override=capabilities,
    )
    if settings_backend_turn_guard_enabled(label):
        add_turn_guard_hooks(settings)
    # Always detach settings backends into a private temporary file.  Passing
    # the original file would let a missing third-party credential fall back to
    # the user's persisted Claude.ai login.
    return launch_with_settings(settings, claude_args, profile=profile)


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


def launch_with_settings(
    settings: dict,
    claude_args: list[str],
    *,
    profile: CapabilityProfile | None = None,
) -> int:
    """Launch Claude with a private settings file and always remove it."""
    profile = profile or resolve_capability_profile(settings=settings)
    env = settings.get("env") if isinstance(settings, dict) else None
    require_base_url = isinstance(env, dict) and bool(
        env.get("ANTHROPIC_BASE_URL")
    )
    settings = prepare_provider_settings(
        settings,
        profile,
        require_base_url=require_base_url,
    )
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
    profile: CapabilityProfile,
    claude_args: list[str],
) -> int:
    """Run one isolated Hub for a non-Anthropic provider, then remove it.

    This preserves claude1's session-isolation contract: the CC Switch current
    provider and its shared proxy are never changed, so concurrent sessions can
    select different wire formats safely.
    """
    api_format = str(profile.get("protocol"))
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
                    "provider": provider["name"],
                    "api_format": api_format,
                    "models": _provider_models(settings),
                    "capabilities": profile.as_dict(),
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
                if hub_healthy(port):
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
            return launch_with_settings(
                bridged,
                claude_args,
                profile=profile,
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


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


def _hub_channel_profiles(
    channels: dict,
) -> list[tuple[str, CapabilityProfile]]:
    """Resolve each hub channel (and its model overlays) to a profile."""
    channel_profiles: list[tuple[str, CapabilityProfile]] = []
    for raw_alias, raw_channel in channels.items():
        if not isinstance(raw_channel, dict):
            continue
        alias = str(raw_alias)
        try:
            provider_record = None
            provider_name = raw_channel.get("provider")
            if isinstance(provider_name, str) and DB_PATH.exists():
                try:
                    provider_record = provider_by_name(provider_name)
                except (RuntimeError, sqlite3.Error, OSError):
                    provider_record = None
            provider_settings = {}
            provider_meta = {}
            provider_type = None
            if provider_record is not None:
                try:
                    provider_settings = json.loads(
                        provider_record.get("settings_config") or "{}"
                    )
                except (json.JSONDecodeError, TypeError):
                    provider_settings = {}
                try:
                    provider_meta = json.loads(
                        provider_record.get("meta") or "{}"
                    )
                except (json.JSONDecodeError, TypeError):
                    provider_meta = {}
                provider_type = provider_record.get("provider_type")
            base_profile = resolve_capability_profile(
                meta=provider_meta,
                settings=provider_settings,
                provider_type=provider_type,
                override=raw_channel.get("capabilities"),
                protocol_override=raw_channel.get("api_format"),
            )
        except ProviderPolicyError as exc:
            raise RuntimeError("hub 渠道 capability profile 无效") from exc
        channel_profiles.append((alias, base_profile))
        for configured_model in raw_channel.get("models", []):
            if isinstance(configured_model, str):
                lookup = (
                    configured_model[:-4]
                    if configured_model.casefold().endswith("[1m]")
                    else configured_model
                )
                channel_profiles.append(
                    (alias, base_profile.for_model(lookup))
                )
    return channel_profiles


def _hub_union_profile(
    channel_profiles: list[tuple[str, CapabilityProfile]],
) -> CapabilityProfile:
    """Build the Claude-facing union profile from real channel declarations.

    Feature discovery fields stay a feature union so /model can still reach a
    channel that supports them, but safety fields take the most conservative
    declaration across channels instead of a hard-coded optimistic value.
    """
    # 与直连策略一致：任何渠道声明后台任务不安全，都拒绝经 hub 启动。
    # 错误里只出现渠道别名，绝不出现真实 provider 名称。
    unsafe_aliases = sorted(
        {
            alias
            for alias, item in channel_profiles
            if item.get("background_worker_safe") == "unsafe"
        }
    )
    if unsafe_aliases:
        raise RuntimeError(
            "hub 渠道 "
            + ", ".join(unsafe_aliases)
            + " 声明 background_worker_safe=unsafe；与直连策略一致，"
            "拒绝通过 hub 启动。请先在 hub 配置中移除或修复该渠道"
        )
    profiles = [item for _alias, item in channel_profiles]
    any_tool_search = any(
        item.get("tool_search") == "supported"
        for item in profiles
    )
    any_thinking = any(
        item.get("thinking") == "supported"
        for item in profiles
    )
    any_reasoning_round_trip = any(
        item.get("reasoning_round_trip") == "supported"
        for item in profiles
    )
    any_prompt_cache = any(
        item.get("prompt_cache") == "supported"
        for item in profiles
    )
    max_context = max(
        (
            int(context)
            for item in profiles
            if isinstance((context := item.get("context_window")), int)
        ),
        default=None,
    )
    count_values = {item.get("count_tokens") for item in profiles}
    if not profiles or "unsupported" in count_values:
        union_count_tokens = "unsupported"
    elif "estimated" in count_values:
        union_count_tokens = "estimated"
    else:
        union_count_tokens = "exact"
    all_stream_terminal_usage = bool(profiles) and all(
        item.get("stream_terminal_usage") == "supported"
        for item in profiles
    )
    all_workers_verified = bool(profiles) and all(
        item.get("background_worker_safe") == "verified"
        for item in profiles
    )
    return resolve_capability_profile(
        override={
            "protocol": "anthropic",
            "tool_search": (
                "supported" if any_tool_search else "unsupported"
            ),
            "count_tokens": union_count_tokens,
            "context_window": max_context or "unknown",
            "thinking": "supported" if any_thinking else "unsupported",
            "reasoning_round_trip": (
                "supported"
                if any_reasoning_round_trip
                else "unsupported"
            ),
            "prompt_cache": (
                "supported" if any_prompt_cache else "unknown"
            ),
            "stream_terminal_usage": (
                "supported" if all_stream_terminal_usage else "unsupported"
            ),
            "beta_policy": "passthrough",
            "background_worker_safe": (
                "verified" if all_workers_verified else "unverified"
            ),
            "model_id_strategy": "mapped",
        }
    )


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
    # Claude-facing feature discovery is the union of channel capabilities.
    # Hub still enforces the selected channel's own profile on every request.
    # Resolve before starting anything so an unsafe channel fails fast.
    hub_profile = _hub_union_profile(_hub_channel_profiles(channels))
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
    return launch_with_settings(settings, claude_args, profile=hub_profile)


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

默认启动只影响本次会话，不修改普通 claude 或 CC Switch 当前渠道。
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
    names = _build_view(cfg, by_name, load_use_stats(), show_all)
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
        for index, row in enumerate(rows, 1):
            try:
                raw_settings = json.loads(row["settings_config"] or "{}")
            except (json.JSONDecodeError, TypeError):
                raw_settings = {}
            raw_env = (
                raw_settings.get("env")
                if isinstance(raw_settings, dict)
                else {}
            )
            credential = (
                configured_credential(raw_env)
                if isinstance(raw_env, dict)
                else None
            )
            report(
                "INFO",
                (
                    f"Provider #{index} credential_source="
                    f"{credential[0] if credential else 'missing'}"
                ),
            )
            try:
                profile = selected_provider_capabilities(
                    _provider_from_row(row)
                )
            except ProviderPolicyError:
                report("FAIL", f"Provider #{index} capability profile 无效")
                continue
            if isinstance(raw_env, dict) and raw_env.get(
                "ANTHROPIC_BASE_URL"
            ):
                try:
                    prepare_provider_settings(
                        raw_settings,
                        profile,
                        require_base_url=True,
                    )
                except ProviderPolicyError:
                    report(
                        "FAIL",
                        f"Provider #{index} credential/isolation policy 拒绝",
                    )
            for line in capability_summary(profile):
                report("INFO", f"Provider #{index} capabilities: {line}")

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
                "[claude1] 用法: claude1 use <cc|any|direct|hub>",
                file=sys.stderr,
            )
            return 1
        return set_sticky(argv[1])
    # `config`/`--config` 现在就是无参数：直接进 TUI 启动器
    if argv and argv[0] in ("config", "--config"):
        argv = argv[1:]

    backend, hint, claude_args = parse_args(argv)

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
            selected = provider_by_name(payload)
            if selected is None:
                print(f"[claude1] 找不到 provider: {payload}", file=sys.stderr)
                return 1

    settings = build_settings(selected)
    profile = selected_provider_capabilities(selected)
    initial_model = settings.get("env", {}).get("ANTHROPIC_MODEL")
    if isinstance(initial_model, str) and initial_model:
        profile = profile.for_model(
            initial_model[:-4]
            if initial_model.casefold().endswith("[1m]")
            else initial_model
        )
    settings = prepare_provider_settings(settings, profile)
    add_provider_turn_guard(settings, selected["name"])
    record_use(selected["name"])
    record_backend("provider", selected["name"])
    print(f"[claude1] 本次使用 provider: {selected['name']}")
    api_format = str(profile.get("protocol"))
    if api_format != "anthropic":
        return launch_with_protocol_bridge(
            selected,
            settings,
            profile,
            claude_args,
        )
    ensure_local_gateway(settings.get("env", {}).get("ANTHROPIC_BASE_URL", ""))
    return launch_with_settings(settings, claude_args, profile=profile)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n[claude1] 已取消", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[claude1] 错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
