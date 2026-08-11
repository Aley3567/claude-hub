"""Protocol translation used by claude-hub.

Claude Code speaks Anthropic Messages to a gateway.  Many GPT-compatible
providers expose either OpenAI Chat Completions or OpenAI Responses instead.
This module converts the request, non-streaming response, and the useful
streaming event subset without depending on an OpenAI SDK.

The translator deliberately keeps provider routing and credentials out of this
module.  It only handles JSON/SSE shapes, which makes it independently testable.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Iterable


API_FORMATS = {"anthropic", "openai_chat", "openai_responses"}
_GPT_MAX_OUTPUT_RE = re.compile(r"^(?:o[1-9](?:[-.]|$)|gpt-5(?:[-.]|$))", re.I)


class ProtocolTransformError(ValueError):
    """Raised when a provider response cannot be represented as Anthropic."""


def provider_api_format(
    *,
    meta: object = None,
    settings: object = None,
    provider_type: object = None,
    override: object = None,
) -> str:
    """Resolve the format using the same precedence as CC Switch.

    An explicit channel/launcher override wins. Codex OAuth is always Responses.
    Then prefer ``meta.apiFormat``, followed by legacy settings fields.
    Unknown values fail closed to Anthropic passthrough.
    """

    if isinstance(override, str) and override in API_FORMATS:
        return override
    meta = meta if isinstance(meta, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    effective_type = provider_type or meta.get("providerType")
    if effective_type == "codex_oauth":
        return "openai_responses"
    value = meta.get("apiFormat")
    if isinstance(value, str) and value in API_FORMATS:
        return value
    value = settings.get("api_format")
    if isinstance(value, str) and value in API_FORMATS:
        return value
    legacy = settings.get("openrouter_compat_mode")
    if legacy is True or legacy == 1 or (
        isinstance(legacy, str) and legacy.strip().casefold() in {"1", "true"}
    ):
        return "openai_chat"
    return "anthropic"


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _system_text(system: object) -> str:
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        return ""
    return "\n\n".join(
        block.get("text", "")
        for block in system
        if isinstance(block, dict)
        and block.get("type", "text") == "text"
        and isinstance(block.get("text"), str)
        and block["text"]
    )


def _clean_schema(value: object) -> object:
    if isinstance(value, list):
        return [_clean_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {
        key: _clean_schema(item)
        for key, item in value.items()
        if not (key == "format" and item == "uri")
    }
    return cleaned


def _chat_tool_choice(value: object) -> object:
    if isinstance(value, str):
        return "required" if value == "any" else value
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind == "any":
        return "required"
    if kind in {"auto", "none"}:
        return kind
    if kind == "tool":
        return {
            "type": "function",
            "function": {"name": str(value.get("name", ""))},
        }
    return value


def _responses_tool_choice(value: object) -> object:
    if isinstance(value, str):
        return "required" if value == "any" else value
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind == "any":
        return "required"
    if kind in {"auto", "none"}:
        return kind
    if kind == "tool":
        return {"type": "function", "name": str(value.get("name", ""))}
    return value


def _chat_content_and_tools(
    role: str, content: object
) -> tuple[object, list[dict], list[dict], str]:
    if isinstance(content, str):
        return content, [], [], ""
    if not isinstance(content, list):
        return content, [], [], ""

    parts: list[dict] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    reasoning: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append({"type": "text", "text": block["text"]})
        elif kind == "image":
            source = block.get("source")
            if isinstance(source, dict):
                media = source.get("media_type", "image/png")
                data = source.get("data", "")
                if isinstance(data, str) and data:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media};base64,{data}"},
                        }
                    )
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": source["url"]},
                        }
                    )
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": _json_text(block.get("input", {})),
                    },
                }
            )
        elif kind == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _json_text(block.get("content", "")),
                }
            )
        elif kind == "thinking" and isinstance(block.get("thinking"), str):
            reasoning.append(block["thinking"])

    if not parts:
        output: object = None
    elif len(parts) == 1 and parts[0].get("type") == "text":
        output = parts[0]["text"]
    else:
        output = parts
    return output, tool_calls, tool_results, "\n".join(reasoning)


def _validate_tool_result_causality(messages: object) -> None:
    """Reject results without one unique, earlier tool-use declaration."""
    if not isinstance(messages, list):
        return
    declared: set[str] = set()
    consumed: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        # Claude Code 2.1.220 can place machine-generated system context in the
        # messages array. Both OpenAI request formats support that role, while
        # tool causality still remains restricted to assistant/user below.
        if role not in {"system", "user", "assistant"}:
            raise ProtocolTransformError(
                f"message roles must be system, user, or assistant, got {role!r}"
            )
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                if role != "assistant":
                    raise ProtocolTransformError(
                        "tool_use blocks require the assistant role"
                    )
                call_id = block.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in declared:
                    raise ProtocolTransformError(
                        "tool_use ids must be non-empty and unique"
                    )
                declared.add(call_id)
            elif kind == "tool_result":
                if role != "user":
                    raise ProtocolTransformError(
                        "tool_result blocks require the user role"
                    )
                call_id = block.get("tool_use_id")
                if (
                    not isinstance(call_id, str)
                    or call_id not in declared
                    or call_id in consumed
                ):
                    raise ProtocolTransformError(
                        "tool_result must reference one earlier unconsumed tool_use"
                    )
                consumed.add(call_id)


def anthropic_to_chat(payload: dict) -> dict:
    _validate_tool_result_causality(payload.get("messages"))
    result: dict = {"model": payload.get("model"), "messages": []}
    system = _system_text(payload.get("system"))
    if system:
        result["messages"].append({"role": "system", "content": system})

    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content, tool_calls, tool_results, reasoning = _chat_content_and_tools(
            str(role), message.get("content")
        )
        # ``reasoning_content`` is part of the historical assistant turn for
        # reasoning-capable Chat providers.  A turn interrupted while thinking
        # has no regular content or tool call, but must not disappear from the
        # replayed conversation.
        if content is not None or tool_calls or (role == "assistant" and reasoning):
            converted: dict = {"role": role, "content": content}
            if tool_calls:
                converted["tool_calls"] = tool_calls
            # Several reasoning OpenAI-compatible providers require historical
            # reasoning_content on assistant messages. It is harmless to omit
            # when Claude did not supply a plain thinking block.
            if reasoning and role == "assistant":
                converted["reasoning_content"] = reasoning
            # Chat Completions requires a tool result to immediately follow the
            # assistant tool call.  Anthropic can combine it with later user
            # text in one block, so split that block while preserving causality.
            if role == "user":
                result["messages"].extend(tool_results)
                result["messages"].append(converted)
            else:
                result["messages"].append(converted)
                result["messages"].extend(tool_results)
        else:
            result["messages"].extend(tool_results)

    model = str(payload.get("model", ""))
    if "max_tokens" in payload:
        key = (
            "max_completion_tokens"
            if _GPT_MAX_OUTPUT_RE.match(model)
            else "max_tokens"
        )
        result[key] = payload["max_tokens"]
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
        ("stream", "stream"),
    ):
        if source in payload:
            result[target] = payload[source]

    tools = []
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type") == "BatchTool":
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name", "")),
                    "description": tool.get("description") or "",
                    "parameters": _clean_schema(tool.get("input_schema", {})),
                },
            }
        )
    if tools:
        result["tools"] = tools
    if "tool_choice" in payload:
        result["tool_choice"] = _chat_tool_choice(payload["tool_choice"])

    effort = _anthropic_effort(payload)
    if effort is not None:
        result["reasoning_effort"] = effort
    return result


def _anthropic_effort(payload: dict) -> str | None:
    """Read Claude Code's current effort field with legacy-thinking fallback."""
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = output_config.get("effort")
        if effort in {"low", "medium", "high", "xhigh"}:
            return effort

    thinking = payload.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") == "disabled":
        return None
    effort = thinking.get("effort")
    if effort in {"low", "medium", "high", "xhigh"}:
        return effort
    budget = thinking.get("budget_tokens")
    return (
        "low"
        if isinstance(budget, int) and budget < 4_000
        else "medium"
        if isinstance(budget, int) and budget < 16_000
        else "high"
    )


def _responses_input(messages: object) -> list[dict]:
    output: list[dict] = []
    if not isinstance(messages, list):
        return output
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content")
        if isinstance(content, str):
            output.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        message_parts: list[dict] = []

        def flush_message_parts() -> None:
            if message_parts:
                output.append({"role": role, "content": list(message_parts)})
                message_parts.clear()

        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                part_type = "output_text" if role == "assistant" else "input_text"
                message_parts.append({"type": part_type, "text": block["text"]})
            elif kind == "image" and role == "user":
                source = block.get("source")
                if isinstance(source, dict):
                    if isinstance(source.get("data"), str):
                        media = source.get("media_type", "image/png")
                        message_parts.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{media};base64,{source['data']}",
                            }
                        )
                    elif source.get("type") == "url" and isinstance(source.get("url"), str):
                        message_parts.append(
                            {"type": "input_image", "image_url": source["url"]}
                        )
            elif kind == "tool_use":
                flush_message_parts()
                output.append(
                    {
                        "type": "function_call",
                        "call_id": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        "arguments": _json_text(block.get("input", {})),
                    }
                )
            elif kind == "tool_result":
                flush_message_parts()
                output.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(block.get("tool_use_id", "")),
                        "output": _json_text(block.get("content", "")),
                    }
                )
            elif kind == "redacted_thinking" and isinstance(block.get("data"), str):
                flush_message_parts()
                output.append(
                    {
                        "type": "reasoning",
                        "encrypted_content": block["data"],
                        "summary": [],
                    }
                )
        flush_message_parts()
    return output


def anthropic_to_responses(payload: dict, *, codex_oauth: bool = False) -> dict:
    _validate_tool_result_causality(payload.get("messages"))
    result: dict = {
        "model": payload.get("model"),
        "input": _responses_input(payload.get("messages")),
        "store": False,
    }
    instructions = _system_text(payload.get("system"))
    if instructions:
        result["instructions"] = instructions
    if "max_tokens" in payload:
        result["max_output_tokens"] = payload["max_tokens"]
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if key in payload:
            result[key] = payload[key]

    tools = []
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type") == "BatchTool":
            continue
        tools.append(
            {
                "type": "function",
                "name": str(tool.get("name", "")),
                "description": tool.get("description") or "",
                "parameters": _clean_schema(tool.get("input_schema", {})),
                "strict": False,
            }
        )
    if tools:
        result["tools"] = tools
    if "tool_choice" in payload:
        result["tool_choice"] = _responses_tool_choice(payload["tool_choice"])

    effort = _anthropic_effort(payload)
    if effort is not None:
        result["reasoning"] = {"effort": effort, "summary": "auto"}
    if codex_oauth:
        result["include"] = ["reasoning.encrypted_content"]
    return result


def transform_request(
    payload: dict, api_format: str, *, provider_type: str | None = None
) -> tuple[str, dict]:
    if api_format == "openai_chat":
        return "/v1/chat/completions", anthropic_to_chat(payload)
    if api_format == "openai_responses":
        return (
            "/v1/responses",
            anthropic_to_responses(
                payload, codex_oauth=provider_type == "codex_oauth"
            ),
        )
    return "/v1/messages", payload


def _usage(
    input_tokens: object = 0,
    output_tokens: object = 0,
    cache_read: object = 0,
    cache_write: object = 0,
) -> dict:
    return {
        "input_tokens": _token_count(input_tokens),
        "output_tokens": _token_count(output_tokens),
        "cache_read_input_tokens": _token_count(cache_read),
        "cache_creation_input_tokens": _token_count(cache_write),
    }


def _token_count(value: object, default: int = 0) -> int:
    """Accept protocol-compatible integer strings without trusting other values."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _stream_identifier(*values: object) -> str | None:
    for value in values:
        if value is not None and value != "":
            return str(value)
    return None


def _cache_read(raw_usage: dict) -> object:
    """从 OpenAI 两种 usage 形状里取缓存读 token 数（多数上游不返回则为 0）。"""
    details = raw_usage.get("prompt_tokens_details")
    if isinstance(details, dict) and _token_count(details.get("cached_tokens")):
        return _token_count(details["cached_tokens"])
    details = raw_usage.get("input_tokens_details")
    if isinstance(details, dict) and _token_count(details.get("cached_tokens")):
        return _token_count(details["cached_tokens"])
    for key in ("cache_read_input_tokens", "cache_read_tokens"):
        if _token_count(raw_usage.get(key)):
            return _token_count(raw_usage[key])
    return 0


def _cache_write(raw_usage: dict) -> object:
    for key in ("cache_creation_input_tokens", "cache_creation_tokens"):
        if _token_count(raw_usage.get(key)):
            return _token_count(raw_usage[key])
    return 0


def _stop_reason(reason: object, *, has_tool: bool = False) -> str:
    if has_tool or reason in {"tool_calls", "function_call"}:
        return "tool_use"
    if reason in {"length", "max_output_tokens", "content_filter"}:
        return "max_tokens"
    if reason == "stop_sequence":
        return "stop_sequence"
    return "end_turn"


def chat_to_anthropic(body: dict) -> dict:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProtocolTransformError("OpenAI Chat response has no choices")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ProtocolTransformError("OpenAI Chat response has no message")
    message = choice["message"]
    content: list[dict] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part in text:
            if not isinstance(part, dict):
                continue
            value = part.get("text") or part.get("refusal")
            if isinstance(value, str) and value:
                content.append({"type": "text", "text": value})
    has_tool = False
    for call in message.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments", "{}")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed = {"_raw": arguments}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id", "")),
                "name": str(function.get("name", "")),
                "input": parsed if isinstance(parsed, dict) else {"value": parsed},
            }
        )
        has_tool = True
    raw_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {
        "id": str(body.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": str(body.get("model", "")),
        "content": content,
        "stop_reason": _stop_reason(choice.get("finish_reason"), has_tool=has_tool),
        "stop_sequence": None,
        "usage": _usage(
            raw_usage.get("prompt_tokens"),
            raw_usage.get("completion_tokens"),
            _cache_read(raw_usage),
            _cache_write(raw_usage),
        ),
    }


def responses_to_anthropic(body: dict) -> dict:
    content: list[dict] = []
    has_tool = False
    output = body.get("output")
    if not isinstance(output, list):
        output = []
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content", []) or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text") or part.get("refusal")
                if isinstance(text, str) and text:
                    content.append({"type": "text", "text": text})
        elif kind == "function_call":
            arguments = item.get("arguments", item.get("input", "{}"))
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed = {"_raw": arguments}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name", "")),
                    "input": parsed if isinstance(parsed, dict) else {"value": parsed},
                }
            )
            has_tool = True
        elif kind == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(
                    str(part.get("text", ""))
                    for part in summary
                    if isinstance(part, dict) and part.get("text")
                )
                if text:
                    content.append({"type": "thinking", "thinking": text})
    raw_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    incomplete = (
        body.get("incomplete_details")
        if isinstance(body.get("incomplete_details"), dict)
        else {}
    )
    return {
        "id": str(body.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": str(body.get("model", "")),
        "content": content,
        "stop_reason": _stop_reason(
            incomplete.get("reason") or body.get("status"), has_tool=has_tool
        ),
        "stop_sequence": None,
        "usage": _usage(
            raw_usage.get("input_tokens"),
            raw_usage.get("output_tokens"),
            _cache_read(raw_usage),
            _cache_write(raw_usage),
        ),
    }


def transform_response(body: dict, api_format: str) -> dict:
    if api_format == "openai_chat":
        return chat_to_anthropic(body)
    if api_format == "openai_responses":
        return responses_to_anthropic(body)
    return body


def transform_error(body: object, status: int) -> dict:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or f"upstream HTTP {status}"
        source_type = error.get("type")
    else:
        message = body if isinstance(body, str) and body else f"upstream HTTP {status}"
        source_type = None
    if status in {401, 403}:
        kind = "authentication_error" if status == 401 else "permission_error"
    elif status == 429:
        kind = "rate_limit_error"
    elif status >= 500:
        kind = "api_error"
    else:
        kind = str(source_type or "invalid_request_error")
    return {"type": "error", "error": {"type": kind, "message": str(message)}}


def sse_event(event: str, payload: dict) -> bytes:
    return (
        f"event: {event}\ndata: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


class SSEParser:
    """Incrementally parse upstream SSE without assuming chunk boundaries."""

    def __init__(self, *, max_buffer: int = 2 * 1024 * 1024):
        self.buffer = bytearray()
        self.max_buffer = max_buffer

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        self.buffer.extend(chunk)
        events: list[tuple[str, str]] = []
        while True:
            match = re.search(br"\r\n\r\n|\n\n|\r\r", self.buffer)
            if match is None:
                break
            if match.start() > self.max_buffer:
                raise ProtocolTransformError("upstream SSE event exceeds size limit")
            raw = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            event = "message"
            data: list[str] = []
            for line in re.split(br"\r\n|\n|\r", raw):
                if line.startswith(b"event:"):
                    event = line[6:].lstrip().decode("utf-8", "strict")
                elif line.startswith(b"data:"):
                    data.append(line[5:].lstrip().decode("utf-8", "strict"))
            if data:
                events.append((event, "\n".join(data)))
        if len(self.buffer) > self.max_buffer:
            raise ProtocolTransformError("upstream SSE event exceeds size limit")
        return events

    def finish(self) -> None:
        if self.buffer.strip():
            raise ProtocolTransformError("upstream SSE ended with an incomplete event")


@dataclass
class AnthropicStreamBridge:
    api_format: str
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    model: str = ""
    started: bool = False
    stopped: bool = False
    upstream_terminal: bool = False
    next_index: int = 0
    text_index: int | None = None
    thinking_index: int | None = None
    tool_indices: dict[str, int] = field(default_factory=dict)
    next_response_tool_key: int = 0
    anonymous_response_tool_keys: set[str] = field(default_factory=set)
    response_text_deltas: set[tuple[str, str, str]] = field(default_factory=set)
    response_text_done: set[tuple[str, str, str]] = field(default_factory=set)
    response_tool_argument_deltas: set[int] = field(default_factory=set)
    open_indices: set[int] = field(default_factory=set)
    has_tool: bool = False
    stop: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [
            sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": self.model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": _usage(
                            self.input_tokens, 0, self.cache_read, self.cache_write
                        ),
                    },
                },
            )
        ]

    def _open(self, kind: str, block: dict, *, key: str | None = None) -> tuple[int, list[bytes]]:
        if key is not None and key in self.tool_indices:
            return self.tool_indices[key], []
        index = self.next_index
        self.next_index += 1
        if key is not None:
            self.tool_indices[key] = index
        self.open_indices.add(index)
        return index, [
            *self._start(),
            sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": kind, **block},
                },
            ),
        ]

    def _close(self, index: int | None) -> list[bytes]:
        if index is None or index not in self.open_indices:
            return []
        self.open_indices.remove(index)
        return [
            sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": index},
            )
        ]

    def _text(self, text: str) -> list[bytes]:
        chunks: list[bytes] = []
        if self.text_index is None:
            chunks.extend(self._close_open_tools())
            chunks.extend(self._close(self.thinking_index))
            self.thinking_index = None
            self.text_index, opened = self._open("text", {"text": ""})
            chunks.extend(opened)
        chunks.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return chunks

    def _thinking(self, text: str) -> list[bytes]:
        chunks: list[bytes] = []
        if self.thinking_index is None:
            chunks.extend(self._close_open_tools())
            self.thinking_index, opened = self._open(
                "thinking", {"thinking": "", "signature": ""}
            )
            chunks.extend(opened)
        chunks.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.thinking_index,
                    "delta": {"type": "thinking_delta", "thinking": text},
                },
            )
        )
        return chunks

    def _tool_start(self, key: str, call_id: str, name: str) -> list[bytes]:
        self.has_tool = True
        _, chunks = self._open(
            "tool_use",
            {"id": call_id, "name": name, "input": {}},
            key=key,
        )
        index = self.tool_indices[key]
        if call_id:
            self.tool_indices.setdefault(call_id, index)
        closed = [
            *self._close(self.text_index),
            *self._close(self.thinking_index),
        ]
        self.text_index = None
        self.thinking_index = None
        return [*closed, *chunks]

    def _close_open_tools(self) -> list[bytes]:
        """Close preceding tool blocks before starting a later text block."""
        chunks: list[bytes] = []
        for index in sorted(set(self.tool_indices.values()) & self.open_indices):
            chunks.extend(self._close(index))
        return chunks

    def _tool_delta(self, key: str, value: str) -> list[bytes]:
        index = self.tool_indices.get(key)
        if index is None or index not in self.open_indices:
            return []
        return [
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": value},
                },
            )
        ]

    def _single_anonymous_response_tool_key(self) -> str | None:
        keys = {
            key
            for key in self.anonymous_response_tool_keys
            if self.tool_indices.get(key) in self.open_indices
        }
        return next(iter(keys)) if len(keys) == 1 else None

    def feed(self, event: str, data: str) -> list[bytes]:
        if self.stopped:
            return []
        if data == "[DONE]":
            self.upstream_terminal = True
            return self.finish()
        if self.upstream_terminal and self.api_format != "openai_chat":
            return []
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProtocolTransformError("upstream SSE contains invalid JSON") from exc
        if not isinstance(payload, dict):
            return []
        return (
            self._feed_chat(payload)
            if self.api_format == "openai_chat"
            else self._feed_responses(event, payload)
        )

    def _feed_chat(self, payload: dict) -> list[bytes]:
        if isinstance(payload.get("id"), str):
            self.message_id = payload["id"]
        if isinstance(payload.get("model"), str):
            self.model = payload["model"]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self.input_tokens = _token_count(
                usage.get("prompt_tokens"), self.input_tokens
            )
            self.output_tokens = _token_count(
                usage.get("completion_tokens"), self.output_tokens
            )
            self.cache_read = _token_count(_cache_read(usage), self.cache_read)
            self.cache_write = _token_count(_cache_write(usage), self.cache_write)
        # Chat providers commonly send one choices=[] usage event after the
        # finish reason. Preserve that metadata, while rejecting late content.
        if self.upstream_terminal:
            if payload.get("choices") not in (None, []):
                raise ProtocolTransformError(
                    "upstream Chat emitted choices after finish_reason"
                )
            return []
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        chunks: list[bytes] = []
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            chunks.extend(self._thinking(reasoning))
        text = delta.get("content")
        if isinstance(text, str) and text:
            chunks.extend(self._text(text))
        for position, call in enumerate(delta.get("tool_calls", []) or []):
            if not isinstance(call, dict):
                continue
            raw_index = call.get("index", position)
            key = str(raw_index)
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            if key not in self.tool_indices:
                chunks.extend(
                    self._tool_start(
                        key,
                        str(call.get("id", "")),
                        str(function.get("name", "")),
                    )
                )
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                chunks.extend(self._tool_delta(key, arguments))
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str) or not finish_reason:
                raise ProtocolTransformError(
                    "upstream Chat finish_reason must be a non-empty string or null"
                )
            self.upstream_terminal = True
            self.stop = _stop_reason(
                finish_reason, has_tool=self.has_tool
            )
        return chunks

    def _feed_responses(self, event: str, payload: dict) -> list[bytes]:
        kind = payload.get("type") if isinstance(payload.get("type"), str) else event
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        if isinstance(response.get("id"), str):
            self.message_id = response["id"]
        if isinstance(response.get("model"), str):
            self.model = response["model"]
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        if usage:
            self.input_tokens = _token_count(
                usage.get("input_tokens"), self.input_tokens
            )
            self.output_tokens = _token_count(
                usage.get("output_tokens"), self.output_tokens
            )
            self.cache_read = _token_count(_cache_read(usage), self.cache_read)
            self.cache_write = _token_count(_cache_write(usage), self.cache_write)
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            text_kind = kind.removesuffix(".delta")
            key = (
                text_kind,
                str(payload.get("output_index", "")),
                str(payload.get("content_index", "")),
            )
            if key in self.response_text_done:
                return []
            self.response_text_deltas.add(key)
            delta = payload.get("delta")
            return self._text(delta) if isinstance(delta, str) and delta else []
        if kind in {"response.output_text.done", "response.refusal.done"}:
            text_kind = kind.removesuffix(".done")
            key = (
                text_kind,
                str(payload.get("output_index", "")),
                str(payload.get("content_index", "")),
            )
            if key in self.response_text_done:
                return []
            self.response_text_done.add(key)
            # Normal Responses streams send the full text again in ``.done``
            # after deltas.  Some compatible upstreams send only ``.done``;
            # emit that text exactly once rather than losing or duplicating it.
            if key in self.response_text_deltas:
                self.response_text_deltas.remove(key)
                return []
            text_key = "text" if text_kind == "response.output_text" else "refusal"
            text = payload.get(text_key, payload.get("delta"))
            return self._text(text) if isinstance(text, str) and text else []
        if kind == "response.reasoning_summary_text.delta":
            delta = payload.get("delta")
            return self._thinking(delta) if isinstance(delta, str) and delta else []
        if kind == "response.output_item.added":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "function_call":
                key = _stream_identifier(
                    item.get("id"), item.get("call_id"), payload.get("output_index")
                )
                if key is None:
                    key = f"response_function_call_{self.next_response_tool_key}"
                    self.next_response_tool_key += 1
                    self.anonymous_response_tool_keys.add(key)
                chunks = self._tool_start(
                    key,
                    _stream_identifier(item.get("call_id"), item.get("id")) or key,
                    str(item.get("name", "")),
                )
                arguments = item.get("arguments")
                if isinstance(arguments, str) and arguments:
                    self.response_tool_argument_deltas.add(self.tool_indices[key])
                    chunks.extend(self._tool_delta(key, arguments))
                return chunks
        if kind == "response.function_call_arguments.delta":
            key = _stream_identifier(
                payload.get("item_id"),
                payload.get("call_id"),
                payload.get("output_index"),
            )
            if key is None:
                key = self._single_anonymous_response_tool_key()
            if key is None:
                return []
            index = self.tool_indices.get(key)
            if index is not None:
                self.response_tool_argument_deltas.add(index)
            delta = payload.get("delta", payload.get("arguments"))
            return self._tool_delta(key, delta) if isinstance(delta, str) else []
        if kind == "response.function_call_arguments.done":
            key = _stream_identifier(
                payload.get("item_id"),
                payload.get("call_id"),
                payload.get("output_index"),
            )
            if key is None:
                key = self._single_anonymous_response_tool_key()
            if key is None:
                return []
            chunks: list[bytes] = []
            index = self.tool_indices.get(key)
            if index is not None and index not in self.response_tool_argument_deltas:
                arguments = payload.get("arguments", payload.get("delta"))
                if isinstance(arguments, str) and arguments:
                    chunks.extend(self._tool_delta(key, arguments))
            if index is not None:
                self.response_tool_argument_deltas.discard(index)
                self.anonymous_response_tool_keys.discard(key)
            return [*chunks, *self._close(index)]
        if kind == "response.output_item.done":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "function_call":
                key = _stream_identifier(
                    item.get("id"), item.get("call_id"), payload.get("output_index")
                )
                if key is None:
                    key = self._single_anonymous_response_tool_key()
                if key is None:
                    return []
                chunks: list[bytes] = []
                index = self.tool_indices.get(key)
                if index is not None and index not in self.response_tool_argument_deltas:
                    arguments = item.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        chunks.extend(self._tool_delta(key, arguments))
                if index is not None:
                    self.response_tool_argument_deltas.discard(index)
                    self.anonymous_response_tool_keys.discard(key)
                return [*chunks, *self._close(index)]
        if kind in {"response.completed", "response.incomplete"}:
            self.upstream_terminal = True
            incomplete = (
                response.get("incomplete_details")
                if isinstance(response.get("incomplete_details"), dict)
                else {}
            )
            self.stop = _stop_reason(
                incomplete.get("reason") or response.get("status"),
                has_tool=self.has_tool,
            )
        if kind in {"response.failed", "error"}:
            self.upstream_terminal = True
            error = payload.get("error") or response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            self.stopped = True
            return [
                sse_event(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": message or "upstream failed"},
                    },
                )
            ]
        return []

    def finish(self) -> list[bytes]:
        if self.stopped:
            return []
        self.stopped = True
        chunks: list[bytes] = [*self._start()]
        for index in sorted(self.open_indices):
            chunks.extend(self._close(index))
        chunks.extend(
            [
                sse_event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": _stop_reason(
                                self.stop, has_tool=self.has_tool
                            ),
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": self.output_tokens},
                    },
                ),
                sse_event("message_stop", {"type": "message_stop"}),
            ]
        )
        return chunks


def translate_sse_chunks(
    api_format: str, chunks: Iterable[bytes]
) -> Iterable[bytes]:
    parser = SSEParser()
    bridge = AnthropicStreamBridge(api_format)
    for chunk in chunks:
        for event, data in parser.feed(chunk):
            yield from bridge.feed(event, data)
    parser.finish()
    if not bridge.upstream_terminal:
        raise ProtocolTransformError("upstream SSE ended without a terminal event")
    yield from bridge.finish()
