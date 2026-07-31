#!/usr/bin/env python3
"""Resolve the real model for a Claude Code statusLine payload.

Reads one statusLine JSON object from stdin and prints one raw model id/name.
The resolver is intentionally layout-free so an existing statusline can use it
without handing its colors, context meter, or cost display to claude1.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse


MODEL_TIERS = ("FABLE", "OPUS", "SONNET", "HAIKU")
META_TYPES = {
    "attachment",
    "mode",
    "permission-mode",
    "last-prompt",
}


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_meta_entry(entry: dict) -> bool:
    kind = str(entry.get("type") or "")
    if kind in META_TYPES or kind.startswith("file-history-"):
        return True
    message = entry.get("message")
    return bool(
        entry.get("sourceToolAssistantUUID")
        or entry.get("toolUseResult") is not None
        or entry.get("isMeta")
        or entry.get("is_meta")
        or (
            isinstance(message, dict)
            and (message.get("isMeta") or message.get("is_meta"))
        )
    )


def latest_response_model(
    transcript_path: object,
    *,
    now: float | None = None,
    max_age_seconds: float = 1800,
) -> str:
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return ""
    assistant: tuple[float, str] | None = None
    latest_semantic: float | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return ""
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        stamp = _timestamp(entry.get("timestamp"))
        if stamp is None:
            continue
        kind = entry.get("type")
        if kind == "assistant":
            message = entry.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model and model != "<synthetic>":
                assistant = (stamp, model)
        elif kind == "user" and not _is_meta_entry(entry):
            latest_semantic = stamp
    if assistant is None:
        return ""
    stamp, model = assistant
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if latest_semantic is not None and stamp < latest_semantic:
        return ""
    if current - stamp >= max_age_seconds:
        return ""
    return model


def _string_env(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and value is not None
    }


def _model_from_env(model_id: str, env: dict[str, str]) -> str:
    if not model_id:
        return env.get("ANTHROPIC_MODEL", "")
    for tier in MODEL_TIERS:
        model_key = f"ANTHROPIC_DEFAULT_{tier}_MODEL"
        if env.get(model_key) == model_id:
            return env.get(f"{model_key}_NAME") or model_id
    if env.get("ANTHROPIC_CUSTOM_MODEL_OPTION") == model_id:
        return env.get("ANTHROPIC_CUSTOM_MODEL_OPTION_NAME") or model_id
    return model_id


def _current_provider_env(db_path: Path) -> dict[str, str]:
    if not db_path.is_file():
        return {}
    uri = db_path.resolve(strict=False).as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "SELECT settings_config FROM providers "
            "WHERE app_type='claude' AND is_current=1"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    if len(rows) != 1:
        return {}
    try:
        settings = json.loads(rows[0][0] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return _string_env(settings.get("env") if isinstance(settings, dict) else None)


def mapped_model(payload: dict, process_env: dict[str, str]) -> str:
    model = payload.get("model")
    model_id = str(model.get("id") or "") if isinstance(model, dict) else ""
    mapped = _model_from_env(model_id, process_env)
    if mapped != model_id or not model_id:
        return mapped

    base_url = process_env.get("ANTHROPIC_BASE_URL", "")
    if not base_url:
        settings_path = Path.home() / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            settings = {}
        env = settings.get("env") if isinstance(settings, dict) else None
        settings_env = _string_env(env)
        base_url = settings_env.get("ANTHROPIC_BASE_URL", "")
    try:
        host = urlparse(base_url).hostname or ""
        is_loopback = host == "localhost" or ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        return mapped

    db_path = Path(
        process_env.get(
            "CLAUDE1_DB_PATH",
            str(Path.home() / ".cc-switch" / "cc-switch.db"),
        )
    ).expanduser()
    return _model_from_env(model_id, _current_provider_env(db_path))


def _without_1m(model: str) -> str:
    lowered = model.lower()
    return model[:-4] if lowered.endswith("[1m]") else model


def resolve_model(
    payload: dict,
    process_env: dict[str, str] | None = None,
    *,
    now: float | None = None,
) -> str:
    env = dict(os.environ if process_env is None else process_env)
    model = payload.get("model")
    ui_name = str(model.get("display_name") or "?") if isinstance(model, dict) else "?"
    model_id = str(model.get("id") or "") if isinstance(model, dict) else ""
    mapped = mapped_model(payload, env)
    actual = latest_response_model(payload.get("transcript_path"), now=now)
    if actual:
        if mapped and _without_1m(mapped) == _without_1m(actual):
            return mapped
        return actual
    return mapped or model_id or ui_name


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        print("?", end="")
        return 1
    if not isinstance(payload, dict):
        print("?", end="")
        return 1
    print(resolve_model(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
