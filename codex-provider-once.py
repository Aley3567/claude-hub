#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run Codex CLI once with a specific CC Switch codex provider.

The channel is applied through a throwaway ``CODEX_HOME`` that symlinks the
real one and adds a single generated profile overlay, so the user's own
``~/.codex/config.toml`` and ``~/.codex/auth.json`` are never written to.

Usage:
  codex1                    # interactive menu (MRU first)
  codex1 acme               # match by name substring (case-insensitive)
  codex1 id:cdab769c...     # match by exact provider id
  codex1 --list             # list channels and exit
  codex1 acme exec "hi"     # everything after the hint is passed to codex
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

VERSION = "0.1.0"

# codex 保留了一批内置 provider 名(amazon-bedrock/openai/ollama/...),
# "codex1" 不在其中,可以安全地作为我们自己的 provider 段名与 profile 名。
PROFILE_NAME = "codex1"
PROFILE_FILE = f"{PROFILE_NAME}.config.toml"
CONFIG_FILE = "config.toml"
AUTH_FILE = "auth.json"
API_KEY_ENV = "CODEX1_API_KEY"
# 只放进影子 config 的段,绝不写进任何文件的凭证字段。
FORBIDDEN_PROVIDER_KEYS = ("experimental_bearer_token",)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


HOME = _env_path("CODEX1_HOME", Path.home())
DB_PATH = _env_path("CODEX1_DB_PATH", HOME / ".cc-switch" / "cc-switch.db")
MRU_PATH = _env_path("CODEX1_MRU_PATH", HOME / ".cc-switch" / "codex1-mru.json")


def real_codex_home() -> Path:
    """Locate the user's real ``CODEX_HOME``.

    继承来的 ``CODEX_HOME`` 不可信:本机上它可能指向一个不存在的伴生目录。
    只有它确实是个存在的目录时才采用,否则回到 ``~/.codex``。
    """
    override = os.environ.get("CODEX1_CODEX_HOME")
    if override:
        return Path(override).expanduser()
    inherited = os.environ.get("CODEX_HOME")
    if inherited:
        candidate = Path(inherited).expanduser()
        if candidate.is_dir():
            return candidate
    return HOME / ".codex"


# ---------------------------------------------------------------- MRU state


def load_mru() -> dict[str, float]:
    try:
        data = json.loads(MRU_PATH.read_text())
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError):
        return {}


def _atomic_private_write(path: Path, text: str) -> None:
    """Atomically replace a local state file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
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


def record_use(provider_id: str) -> None:
    # 只记 provider id 与时间戳,任何凭证都不进这个文件。
    mru = load_mru()
    mru[str(provider_id)] = time.time()
    try:
        _atomic_private_write(MRU_PATH, json.dumps(mru, ensure_ascii=False, indent=1))
    except OSError:
        pass  # MRU is best-effort; never block a launch on it


# ------------------------------------------------------------- provider data


def db_codex_rows() -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        raise RuntimeError(f"CC Switch DB 不存在: {DB_PATH}")
    db_uri = DB_PATH.resolve(strict=False).as_uri() + "?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, name, settings_config, sort_index "
            "FROM providers WHERE app_type='codex'"
        ).fetchall()
    finally:
        conn.close()


def list_providers() -> list[dict]:
    providers = [
        {
            "id": str(row["id"]),
            "name": str(row["name"] or ""),
            "settings_config": row["settings_config"],
            "sort_index": row["sort_index"],
        }
        for row in db_codex_rows()
    ]
    # sort_index 为 NULL 的排在后面,再按名字保证顺序稳定。
    providers.sort(
        key=lambda p: (
            p["sort_index"] is None,
            p["sort_index"] if p["sort_index"] is not None else 0,
            p["name"].casefold(),
        )
    )
    return providers


def order_by_mru(providers: list[dict], mru: dict[str, float]) -> list[dict]:
    """Most recently used first, everything else in its stable order."""
    indexed = list(enumerate(providers))
    indexed.sort(key=lambda item: (-mru.get(item[1]["id"], 0.0), item[0]))
    return [provider for _, provider in indexed]


def provider_settings(provider: dict) -> tuple[dict, str]:
    """Split a provider's persisted settings into its auth block and config text."""
    name = provider.get("name", "")
    raw = provider.get("settings_config")
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(f"渠道 '{name}' 没有 settings_config,无法启动")
    try:
        data = json.loads(raw)
    except ValueError:
        # 不带原始异常:settings_config 里含凭证,解析器可能回显片段。
        raise RuntimeError(f"渠道 '{name}' 的 settings_config 不是合法 JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"渠道 '{name}' 的 settings_config 不是 JSON 对象")
    auth = data.get("auth")
    config = data.get("config")
    if not isinstance(config, str) or not config.strip():
        raise RuntimeError(f"渠道 '{name}' 缺少 config 片段,无法生成 profile")
    return (auth if isinstance(auth, dict) else {}), config


def auth_kind(auth: dict) -> str:
    if auth.get("auth_mode") == "chatgpt" or isinstance(auth.get("tokens"), dict):
        return "chatgpt"
    return "api-key"


# ------------------------------------------------------------- toml emitting


def _toml_string(value: str) -> str:
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append("\\u%04X" % ord(char))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def toml_value(value: object) -> str:
    if isinstance(value, bool):  # bool 必须排在 int 前面
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("TOML 不支持 nan/inf")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"不支持写入 TOML 的类型: {type(value).__name__}")


def toml_supported(value: object) -> bool:
    if isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(toml_supported(item) and not isinstance(item, list) for item in value)
    return False


def _toml_key(key: str) -> str:
    bare = key and all(c.isalnum() and c.isascii() or c in "_-" for c in key)
    return key if bare else _toml_string(key)


def render_toml(scalars: dict, tables: dict[str, dict]) -> str:
    lines = [f"{_toml_key(k)} = {toml_value(v)}" for k, v in scalars.items()]
    for table_name, table in tables.items():
        lines.append("")
        lines.append(f"[{table_name}]")
        lines.extend(f"{_toml_key(k)} = {toml_value(v)}" for k, v in table.items())
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ profile build


def build_profile(config_text: str, auth: dict) -> dict:
    """Turn a CC Switch codex channel into a self-contained profile overlay.

    上游段被重命名为 ``codex1`` 并由 profile 顶层的 ``model_provider`` 选中,
    这样不管基础 config.toml 里当前是哪个渠道,层叠结果都一定是我们选的这个。
    """
    try:
        base = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"渠道 config 不是合法 TOML: {exc}") from None

    provider_key = base.get("model_provider")
    if not isinstance(provider_key, str) or not provider_key.strip():
        raise RuntimeError("渠道 config 缺少 model_provider,无法确定要启用的上游段")
    providers = base.get("model_providers")
    section = providers.get(provider_key) if isinstance(providers, dict) else None
    if not isinstance(section, dict):
        raise RuntimeError(
            f"渠道 config 里找不到 [model_providers.{provider_key}] 段;"
            "拒绝启动——否则 codex 会静默用你的默认渠道"
        )

    section = {k: v for k, v in section.items() if k not in FORBIDDEN_PROVIDER_KEYS}
    kind = auth_kind(auth)
    api_key = None
    if kind == "api-key":
        key = auth.get("OPENAI_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("渠道既不是 ChatGPT 登录态,也没有可用的 OPENAI_API_KEY")
        api_key = key
        # env_key 在 0.148 里优先级最高:设了它就只认环境变量,
        # 缺失时直接报错而不是悄悄回退到 auth.json。
        section["env_key"] = API_KEY_ENV
    else:
        section.pop("env_key", None)

    scalars: dict = {"model_provider": PROFILE_NAME}
    dropped: list[str] = []
    for key, value in base.items():
        if key in ("model_provider", "model_providers"):
            continue
        if toml_supported(value):
            scalars[key] = value
        else:
            # 嵌套表([tui]/[features]/[projects] 等)属于用户级设置,
            # 已经由 symlink 过来的基础 config.toml 提供,这里不重复搬运。
            dropped.append(key)

    unsupported = [k for k, v in section.items() if not toml_supported(v)]
    if unsupported:
        raise RuntimeError(
            f"[model_providers.{provider_key}] 含无法序列化的键: {', '.join(unsupported)}"
        )

    return {
        "toml": render_toml(scalars, {f"model_providers.{PROFILE_NAME}": section}),
        "kind": kind,
        "api_key": api_key,
        "auth_payload": dict(auth) if kind == "chatgpt" else {},
        "base_url": section.get("base_url") if isinstance(section.get("base_url"), str) else "",
        "dropped_keys": dropped,
    }


def provider_summary(provider: dict) -> tuple[str, str]:
    """Best-effort (auth kind, base_url) for listings; never raises."""
    try:
        auth, config_text = provider_settings(provider)
        base = tomllib.loads(config_text)
        kind = auth_kind(auth)
        providers = base.get("model_providers")
        section = providers.get(base.get("model_provider")) if isinstance(providers, dict) else None
        base_url = section.get("base_url") if isinstance(section, dict) else ""
        return kind, base_url if isinstance(base_url, str) else ""
    except (RuntimeError, tomllib.TOMLDecodeError, TypeError):
        return "?", ""


def provider_line(provider: dict, labels: dict[str, str]) -> str:
    kind, base_url = provider_summary(provider)
    suffix = f"  {base_url}" if base_url else ""
    return f"{labels[str(provider['id'])]}  [{kind}]{suffix}"


# ---------------------------------------------------------------- selection


def _short_provider_id(provider_id: object) -> str:
    raw = str(provider_id)
    return raw if len(raw) <= 12 else raw[:8]


def _provider_terms(provider: dict) -> list[str]:
    return [str(provider.get("name", "")), f"id:{provider.get('id', '')}"]


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
    """Return matches and whether they are exact.

    精确的渠道名与 ``id:<id>`` 优先于子串匹配;id 本身不参与模糊匹配,
    否则一个两字母的 hint 会撞上随机的 uuid 片段。
    """
    needle = hint.strip().casefold()
    if not needle:
        return ([], False)
    exact: list[dict] = []
    fuzzy: list[dict] = []
    for provider in providers:
        exact_terms = [term.casefold() for term in _provider_terms(provider)]
        fuzzy_terms = [str(provider.get("name", "")).casefold()]
        if any(term == needle for term in exact_terms):
            exact.append(provider)
        elif any(needle in term for term in fuzzy_terms):
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
            raise RuntimeError(f"名称 '{hint}' 存在冲突: {names};请用 id:<id> 指定")
        if len(matches) > 1:
            print("匹配到多个渠道，请选择:")
            providers = matches
        else:
            raise RuntimeError(f"找不到匹配 '{hint}' 的 codex 渠道")
    else:
        print("选择本次 Codex 渠道:")

    for index, provider in enumerate(providers, 1):
        print(f"{index}. {provider_line(provider, labels)}")
    try:
        choice = input("> ").strip()
    except EOFError:
        raise RuntimeError(
            "标准输入不可用，无法完成渠道选择；请指定渠道名或 id:ID"
        ) from None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(providers):
            return providers[idx]
    matches, exact = match_providers(providers, choice)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and exact:
        names = "、".join(labels[str(p["id"])] for p in matches)
        raise RuntimeError(f"名称 '{choice}' 存在冲突: {names}")
    raise RuntimeError("无效选择，已取消")


# -------------------------------------------------------------- shadow home


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        if os.name == "posix":
            os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def build_shadow_home(real_home: Path, profile_text: str, auth_text: str) -> Path:
    """Mirror the real CODEX_HOME by symlink, overriding only config + auth.

    sessions/history/日志等仍然写进真实 ``~/.codex``(symlink 指过去),
    只有 ``config.toml`` 的 profile 层和 ``auth.json`` 是本次会话私有的。
    """
    real_home = real_home.expanduser()
    if not real_home.is_dir():
        raise RuntimeError(f"真实 CODEX_HOME 不存在或不是目录: {real_home}")
    real_home = real_home.resolve()

    shadow = Path(tempfile.mkdtemp(prefix="codex1-"))
    os.chmod(shadow, 0o700)
    try:
        for entry in sorted(os.listdir(real_home)):
            if entry in (CONFIG_FILE, AUTH_FILE, PROFILE_FILE):
                continue
            os.symlink(real_home / entry, shadow / entry)
        real_config = real_home / CONFIG_FILE
        if real_config.exists():
            # 基础 config 仍然只读地继承:mcp_servers/agents/tui/features 都在里面。
            os.symlink(real_config, shadow / CONFIG_FILE)
        _write_private(shadow / PROFILE_FILE, profile_text)
        _write_private(shadow / AUTH_FILE, auth_text)
    except BaseException:
        shutil.rmtree(shadow, ignore_errors=True)
        raise

    profile_path = shadow / PROFILE_FILE
    # 自检:profile 文件缺失时 codex 不报错,会静默回落到基础 config——
    # 那正是"以为切了渠道其实没切"的事故,宁可在这里失败。
    if not profile_path.is_file() or not os.access(profile_path, os.R_OK):
        shutil.rmtree(shadow, ignore_errors=True)
        raise RuntimeError(f"profile 文件生成失败: {profile_path}")
    return shadow


# ------------------------------------------------------------------ launch


def _run_codex(command: list[str], *, env: dict[str, str]) -> int:
    """Run Codex in its own process group and forward Ctrl-C to that group.

    A wrapper process normally receives the terminal's SIGINT along with Codex,
    which would tear the child down before Codex handles the interrupt.  Keeping
    Codex in a separate session lets the wrapper relay every interrupt and keep
    waiting, so Codex retains its normal "cancel this turn" behavior.
    """
    proc = subprocess.Popen(command, env=env, start_new_session=(os.name == "posix"))
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


def codex_binary() -> str:
    configured = os.environ.get("CODEX1_CODEX_BIN")
    if configured:
        path = Path(configured).expanduser()
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"CODEX1_CODEX_BIN 不可执行: {path}")
        return str(path)
    found = shutil.which("codex")
    if not found:
        raise RuntimeError("找不到 codex 可执行文件；请安装 Codex CLI 或设置 CODEX1_CODEX_BIN")
    return found


def split_args(argv: list[str], providers: list[dict]) -> tuple[str | None, list[str]]:
    """Take the first positional argument as a channel hint when it matches."""
    if argv and not argv[0].startswith("-"):
        matches, _ = match_providers(providers, argv[0])
        if matches:
            return argv[0], argv[1:]
    return None, list(argv)


def print_listing(providers: list[dict]) -> None:
    print(f"codex1 {VERSION} — CC Switch codex 渠道 ({len(providers)}):")
    labels = _provider_labels(providers)
    for index, provider in enumerate(providers, 1):
        print(f"{index}. {provider_line(provider, labels)}  id:{provider['id']}")


def main(argv: list[str]) -> int:
    providers = list_providers()
    if not providers:
        raise RuntimeError("CC Switch 里没有任何 codex 渠道；请先在 CC Switch 添加一个")

    if argv and argv[0] in ("--list", "-l"):
        print_listing(providers)
        return 0

    ordered = order_by_mru(providers, load_mru())
    hint, passthrough = split_args(argv, providers)
    if hint is None and passthrough and not passthrough[0].startswith("-"):
        # 首个位置参数没匹配上任何渠道,它会原样成为 codex 的 prompt。
        # 打错渠道名和"就是要发这句话"长得一样,说破以免用户以为选中了渠道。
        print(
            f"[codex1] '{passthrough[0]}' 不是渠道名，将作为 prompt 传给 codex；"
            "渠道请在下面选。",
            file=sys.stderr,
        )
    provider = choose(ordered, hint)

    auth, config_text = provider_settings(provider)
    profile = build_profile(config_text, auth)
    binary = codex_binary()

    auth_text = json.dumps(profile["auth_payload"], ensure_ascii=False)
    shadow = build_shadow_home(real_codex_home(), profile["toml"], auth_text)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env.pop(API_KEY_ENV, None)
        env["CODEX_HOME"] = str(shadow)
        if profile["api_key"]:
            # 真实 key 只活在子进程环境里:不落盘、不进 argv、不进日志。
            env[API_KEY_ENV] = profile["api_key"]

        label = _provider_labels(providers)[provider["id"]]
        target = profile["base_url"] or "(继承基础 config)"
        print(f"[codex1] 渠道: {label} | 认证: {profile['kind']} | base_url: {target}",
              file=sys.stderr)
        if profile["dropped_keys"]:
            print(
                "[codex1] 以下渠道设置为嵌套表，改由基础 config.toml 提供: "
                + ", ".join(profile["dropped_keys"]),
                file=sys.stderr,
            )
        record_use(provider["id"])
        return _run_codex([binary, "-p", PROFILE_NAME, *passthrough], env=env)
    finally:
        shutil.rmtree(shadow, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as exc:
        print(f"[codex1] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
