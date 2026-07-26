#!/usr/bin/env python3
"""Guard one opted-in claude1 provider against thinking-only Stop events."""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
from pathlib import Path


CONTINUE_REASON = (
    "上一轮响应只产生了 thinking，未返回正文或工具调用。"
    "请从中断处继续完成原任务；已执行过的工具调用不要重复执行。"
)
BREAKER_MESSAGE = (
    "claude1 守护：连续收到只含 thinking 的空结束，已停止自动续跑。"
    "建议用同一模型 /resume 恢复本会话，或 fork 会话重试。"
)
MAX_LOG_BYTES = 256 * 1024


def _state_dir() -> Path:
    configured = os.environ.get("CLAUDE1_TURN_GUARD_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "claude1" / "turn-guard"


def _record(status: str, message: str) -> None:
    try:
        state_dir = _state_dir()
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_info = state_dir.lstat()
        if not stat.S_ISDIR(state_info.st_mode):
            return
        if os.name == "posix":
            os.chmod(state_dir, 0o700)
        log_path = state_dir / "watch.log"
        try:
            log_info = log_path.lstat()
        except FileNotFoundError:
            log_info = None
        if log_info is not None:
            if not stat.S_ISREG(log_info.st_mode):
                return
            if log_info.st_size >= MAX_LOG_BYTES:
                rotated = log_path.with_name(f"{log_path.name}.1")
                os.replace(log_path, rotated)
                if os.name == "posix":
                    os.chmod(rotated, 0o600)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(log_path, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            timestamp = dt.datetime.now().astimezone().strftime("%F %T%z")
            line = f"[{timestamp}] {status} {message}\n".encode("utf-8")
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except OSError:
        return


def _last_assistant_shape(transcript_path: Path) -> tuple[str | None, set[str]]:
    message_id: str | None = None
    stop_reason: str | None = None
    content_types: set[str] = set()

    expected = transcript_path.lstat()
    if not stat.S_ISREG(expected.st_mode):
        raise OSError("transcript is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(transcript_path, flags)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
    ) != (
        expected.st_dev,
        expected.st_ino,
    ):
        os.close(descriptor)
        raise OSError("transcript changed while opening")

    with os.fdopen(descriptor, "r", encoding="utf-8") as transcript:
        for line in transcript:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message")
            if event.get("type") != "assistant" or not isinstance(message, dict):
                continue
            current_id = message.get("id")
            if not isinstance(current_id, str) or not current_id:
                continue
            if current_id != message_id:
                message_id = current_id
                stop_reason = None
                content_types = set()
            current_stop = message.get("stop_reason")
            if isinstance(current_stop, str):
                stop_reason = current_stop
            content = message.get("content")
            if isinstance(content, str):
                if content:
                    content_types.add("text")
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and not block.get("text"):
                    continue
                if isinstance(block_type, str):
                    content_types.add(block_type)

    return stop_reason, content_types


def _handle_stop(hook_input: dict) -> None:
    transcript_value = hook_input.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value:
        _record("LIVE_UNK", "transcript unavailable")
        return
    try:
        stop_reason, content_types = _last_assistant_shape(Path(transcript_value))
    except (OSError, UnicodeError):
        _record("LIVE_UNK", "transcript unavailable")
        return
    if not content_types and stop_reason is None:
        _record("LIVE_UNK", "transcript unavailable")
        return
    if (
        stop_reason == "end_turn"
        and "thinking" in content_types
        and "text" not in content_types
        and "tool_use" not in content_types
    ):
        if hook_input.get("stop_hook_active") is True:
            # 熔断上限刻意为 1：本守护逐轮无状态，只能靠 stop_hook_active
            # 判断"本次 Stop 已被某个 Stop hook 拦截过"。其他 Stop hook 拦截后
            # 也会落到这里，属于已知交互；方向是 fail-safe（宁可少续跑一次）。
            _record("LIVE_BROKEN", "repeated thinking-only end_turn")
            # 放行本次停止（不再 block），但通过 systemMessage 给用户可见的
            # 降级提示；不携带渠道名或 transcript 内容。
            print(
                json.dumps(
                    {"systemMessage": BREAKER_MESSAGE},
                    ensure_ascii=False,
                )
            )
            return
        _record("LIVE_EMPTY", "thinking-only end_turn")
        print(
            json.dumps(
                {"decision": "block", "reason": CONTINUE_REASON},
                ensure_ascii=False,
            )
        )
        return
    if content_types or stop_reason is not None:
        _record("LIVE_OK", "usable assistant response")


def _handle_failure(hook_input: dict) -> None:
    error_type = hook_input.get("error")
    if error_type in {
        "connection_failed",
        "connection_closed",
        "network_error",
        "response_stalled",
    }:
        _record("LIVE_DROP", "explicit API failure")
        return
    if error_type == "rate_limit":
        _record("LIVE_BUSY", "rate limited")
        return
    if error_type in {"overloaded", "server_error"}:
        _record("LIVE_DOWN", "upstream unavailable")
        return
    if error_type in {"authentication_failed", "oauth_org_not_allowed"}:
        _record("LIVE_AUTH", "authentication failed")
        return
    _record("LIVE_UNK", "unclassified API failure")


def main(argv: list[str]) -> int:
    if argv not in (["stop"], ["failure"]):
        return 0
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if isinstance(hook_input, dict):
        if argv == ["stop"]:
            _handle_stop(hook_input)
        else:
            _handle_failure(hook_input)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
