"""Protocol translation used by claude-hub.

Claude Code speaks Anthropic Messages to a gateway.  Many GPT-compatible
providers expose either OpenAI Chat Completions or OpenAI Responses instead.
This module converts the request, non-streaming response, and the useful
streaming event subset without depending on an OpenAI SDK.

The translator deliberately keeps provider routing and credentials out of this
module.  It only handles JSON/SSE shapes, which makes it independently testable.
"""

from __future__ import annotations

import copy
import base64
import binascii
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from claude1_protocol_types import (
    CapabilityDecision,
    CapabilityProfile,
    ContentBlockIR,
    ConversionPlan,
    MessageIR,
    OutputIR,
    PreparedRequest,
    ProtocolRequestError,
    ProtocolTransformError,
    RequestIR,
    SupportDisposition,
)
from claude1_protocol_usage import (
    UsageReceipt,
    _MISSING,
    _cache_creation_detail,
    _cache_read,
    _cache_write,
    _has_nested_cache_carrier,
    _has_official_cache_read,
    _server_tool_usage,
    _token_count,
    _upstream_usage_total,
    _usage_with_details,
    _validate_cache_creation_consistency,
    _validate_upstream_usage_fields,
)


@dataclass(frozen=True)
class PreparedResponse:
    payload: dict
    plan: ConversionPlan
    # The usage evidence the payload was rendered from. None for the native
    # pass-through, which never re-interprets the upstream body.
    receipt: UsageReceipt | None = None

    def usage_for_accounting(self) -> dict:
        """Export the accounting view of this response's usage.

        Mirrors AnthropicStreamBridge.usage_for_accounting: the ledger reads
        the same receipt the downstream payload was rendered from, so an
        unobserved counter is omitted here instead of being read back as the
        zero the schema-complete payload had to carry.
        """
        if self.receipt is None:
            return {}
        usage = self.receipt.as_anthropic()
        payload_usage = self.payload.get("usage")
        if isinstance(payload_usage, dict):
            for key in ("cache_creation", "server_tool_use"):
                detail = payload_usage.get(key)
                if isinstance(detail, dict):
                    usage[key] = copy.deepcopy(detail)
        return usage


API_FORMATS = {"anthropic", "openai_chat", "openai_responses"}
_GPT_MAX_OUTPUT_RE = re.compile(r"^(?:o[1-9](?:[-.]|$)|gpt-5(?:[-.]|$))", re.I)
_RESPONSES_REASONING_PREFIX = "hub:openai_responses:reasoning:v1:"


def _tag_responses_reasoning(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return _RESPONSES_REASONING_PREFIX + encoded


def _untag_responses_reasoning(value: str) -> str | None:
    if not value.startswith(_RESPONSES_REASONING_PREFIX):
        return None
    encoded = value[len(_RESPONSES_REASONING_PREFIX) :]
    try:
        decoded = base64.b64decode(
            encoded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (UnicodeEncodeError, binascii.Error, UnicodeDecodeError):
        return None
    return decoded


CAPABILITY_PROFILES = {
    "anthropic": CapabilityProfile("anthropic", "/v1/messages"),
    "openai_chat": CapabilityProfile("openai_chat", "/v1/chat/completions"),
    "openai_responses": CapabilityProfile("openai_responses", "/v1/responses"),
    # Reserved seam only. It is deliberately not in API_FORMATS or Hub config
    # until a real Gemini adapter, auth profile, endpoint and contract suite exist.
    "gemini_generate_content": CapabilityProfile(
        "gemini_generate_content", "", availability="reserved"
    ),
}

_PROTOCOL_CAPABILITY_MATRIX = {
    "system_text": ("exact", "exact", "exact"),
    "system_role_extension": (
        "observable_degradation",
        "exact",
        "exact",
    ),
    "system_metadata": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "text": ("exact", "exact", "exact"),
    "image": ("exact", "exact", "exact"),
    "document": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "search_result": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "citations": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "client_tool": ("exact", "exact", "exact"),
    "tool_strict": ("exact", "exact", "exact"),
    "tool_metadata": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "batch_tool": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    # The generic feature is conservative because nested content and
    # ``is_error`` require a visible envelope.  The plain-text subset below is
    # the lossless carrier.
    "tool_result": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "tool_result_text": ("exact", "exact", "exact"),
    "tool_result_nested": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "tool_result_is_error": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "thinking_text": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "thinking_control": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    # In the default visible-lossy mode a cross-protocol request drops a real
    # Anthropic signature with a stable warning. Strict mode rejects it.
    "thinking_signature": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    # Arbitrary opaque redaction data has no safe cross-protocol carrier. Only
    # values tagged by this Responses adapter can make the exact round trip.
    "redacted_thinking": ("exact", "reject", "reject"),
    "responses_provenanced_redacted_thinking": (
        "exact",
        "reject",
        "exact",
    ),
    "server_tool": ("exact", "reject", "reject"),
    "mcp": ("exact", "reject", "reject"),
    "tool_search": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "code_execution": ("exact", "reject", "reject"),
    "cache_control": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "metadata": (
        "exact",
        "observable_degradation",
        "exact",
    ),
    "service_tier": ("exact", "exact", "exact"),
    "top_k": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "stop_sequences": (
        "exact",
        "exact",
        "observable_degradation",
    ),
    "schema_uri_format": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "container": ("exact", "reject", "reject"),
    "inference_geo": ("exact", "reject", "reject"),
    # Missing upstream counters require schema placeholders and an observable
    # provenance warning; counters that are actually present map exactly.
    "usage_base": (
        "exact",
        "observable_degradation",
        "observable_degradation",
    ),
    "usage_base_present": ("exact", "exact", "exact"),
    "cache_usage": ("exact", "exact", "exact"),
    "server_usage": ("exact", "exact", "exact"),
    "count_tokens": (
        "observable_degradation",
        "observable_degradation",
        "observable_degradation",
    ),
    "count_tokens_upstream_endpoint": ("exact", "reject", "reject"),
    "count_tokens_estimate": (
        "observable_degradation",
        "observable_degradation",
        "observable_degradation",
    ),
}


def protocol_capability_matrix() -> dict[str, dict[str, str]]:
    """Return the public exact/degrade/reject contract for each adapter."""

    columns = ("anthropic", "openai_chat", "openai_responses")
    return {
        feature: {
            **dict(zip(columns, dispositions, strict=True)),
            "gemini_generate_content": "reject",
        }
        for feature, dispositions in _PROTOCOL_CAPABILITY_MATRIX.items()
    }


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


def _clean_schema(
    value: object,
    *,
    plan: ConversionPlan | None = None,
    compatibility_mode: str = "visible_lossy",
    path: str = "$.schema",
) -> object:
    if isinstance(value, list):
        return [
            _clean_schema(
                item,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    cleaned = {}
    for key, item in value.items():
        key_path = f"{path}.{key}"
        if key == "format" and item == "uri":
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code="HUB_DEGRADE_SCHEMA_NORMALIZED",
                reject_code="HUB_UNSUPPORTED_SCHEMA",
                path=key_path,
                feature="json_schema_format_uri",
                message="target schema profile does not accept format: uri",
            )
            continue
        cleaned[key] = _clean_schema(
            item,
            plan=plan,
            compatibility_mode=compatibility_mode,
            path=key_path,
        )
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


def _record_lossy(
    plan: ConversionPlan | None,
    *,
    compatibility_mode: str,
    code: str,
    reject_code: str,
    path: str,
    feature: str,
    message: str,
) -> None:
    if compatibility_mode == "strict":
        raise ProtocolRequestError(message, code=reject_code, path=path)
    if plan is not None:
        plan.add(SupportDisposition.DEGRADED, code, path, feature)


def _tool_result_output(
    block: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
    path: str,
) -> str:
    content = block.get("content", "")
    is_error = block.get("is_error", False)
    if not isinstance(is_error, bool):
        raise ProtocolRequestError(
            "tool_result.is_error must be a boolean",
            code="HUB_INVALID_TOOL_RESULT_CONTENT",
            path=f"{path}.is_error",
        )
    if isinstance(content, list):
        allowed = {"text", "image", "document", "search_result"}
        for index, part in enumerate(content):
            part_path = f"{path}.content[{index}]"
            if (
                not isinstance(part, dict)
                or not isinstance(part.get("type"), str)
                or part["type"] not in allowed
            ):
                raise ProtocolRequestError(
                    "tool_result contains an unsupported nested content block",
                    code="HUB_UNSUPPORTED_TOOL_RESULT_PART",
                    path=part_path,
                )
    if isinstance(content, str) and not is_error:
        return content
    if not isinstance(content, str):
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_TOOL_RESULT_CONTENT_ENVELOPED",
            reject_code="HUB_UNSUPPORTED_TOOL_RESULT_CONTENT",
            path=f"{path}.content",
            feature="nested_tool_result",
            message="nested tool_result content requires a visible text envelope",
        )
    if is_error:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_TOOL_RESULT_ERROR_ENVELOPED",
            reject_code="HUB_UNSUPPORTED_TOOL_RESULT_IS_ERROR",
            path=f"{path}.is_error",
            feature="tool_result_is_error",
            message="tool_result.is_error has no exact target-protocol field",
        )
    return _json_text(
        {
            "type": "anthropic_tool_result",
            "is_error": is_error,
            "content": content,
        }
    )


def _safe_label(value: object, default: str = "untitled") -> str:
    if not isinstance(value, str) or not value:
        return default
    return value.replace("\r", " ").replace("\n", " ").replace("]", "\\]")


def _record_content_metadata(
    block: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
    path: str,
) -> None:
    if "citations" in block:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_CITATION_METADATA_DROPPED",
            reject_code="HUB_UNSUPPORTED_CITATION",
            path=f"{path}.citations",
            feature="citation_metadata",
            message="citation metadata has no lossless target-protocol mapping",
        )
    if "cache_control" in block:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_CONTENT_METADATA_DROPPED",
            reject_code="HUB_UNSUPPORTED_CACHE_CONTROL",
            path=f"{path}.cache_control",
            feature="content_cache_control",
            message="content block cache_control cannot be preserved by this adapter",
        )


def _record_thinking_degradation(
    block: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
    path: str,
) -> None:
    signature = block.get("signature")
    if signature == "":
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_EMPTY_THINKING_SIGNATURE_IGNORED",
            reject_code="HUB_UNSUPPORTED_THINKING_SIGNATURE",
            path=f"{path}.signature",
            feature="anthropic_thinking_signature",
            message="empty thinking signature is a client placeholder, not a verifiable signature",
        )
    elif "signature" in block:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_THINKING_SIGNATURE_DROPPED",
            reject_code="HUB_UNSUPPORTED_THINKING_SIGNATURE",
            path=f"{path}.signature",
            feature="anthropic_thinking_signature",
            message="Anthropic thinking signatures cannot cross protocol boundaries",
        )
    _record_lossy(
        plan,
        compatibility_mode=compatibility_mode,
        code="HUB_DEGRADE_THINKING_TO_REASONING",
        reject_code="HUB_UNSUPPORTED_THINKING",
        path=path,
        feature="thinking_text",
        message="thinking text uses a target-specific reasoning carrier",
    )


def _document_text(
    block: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
    path: str,
) -> str:
    _record_content_metadata(
        block,
        plan=plan,
        compatibility_mode=compatibility_mode,
        path=path,
    )
    source = block.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("type"), str):
        raise ProtocolRequestError(
            "document.source must be a typed object",
            code="HUB_UNSUPPORTED_DOCUMENT_SOURCE",
            path=f"{path}.source",
        )
    source_type = source["type"]
    media_type = _safe_label(source.get("media_type"), "unknown")
    title = _safe_label(block.get("title"))
    context = block.get("context")
    context_text = ""
    if isinstance(context, str) and context:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_DOCUMENT_CONTEXT_TEXTIFIED",
            reject_code="HUB_UNSUPPORTED_DOCUMENT_CONTEXT",
            path=f"{path}.context",
            feature="document_context_textification",
            message="document context requires an explicit provenance text envelope",
        )
        context_text = f"\n[document context]\n{context}"
    extracted: str | None = None
    if source_type in {"text", "plain_text"} and isinstance(source.get("data"), str):
        extracted = source["data"]
    elif source_type == "content":
        content = source.get("content")
        if isinstance(content, str):
            extracted = content
        elif isinstance(content, list):
            texts: list[str] = []
            for index, part in enumerate(content):
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)
                ):
                    continue
                _record_content_metadata(
                    part,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=f"{path}.source.content[{index}]",
                )
                texts.append(part["text"])
            if len(texts) == len(content):
                extracted = "\n\n".join(texts)
    elif source_type == "base64" and media_type.startswith("text/"):
        data = source.get("data")
        if isinstance(data, str):
            try:
                extracted = base64.b64decode(data, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                extracted = None
    if extracted is not None:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_DOCUMENT_TEXT_EXTRACTED",
            reject_code="HUB_UNSUPPORTED_DOCUMENT_SOURCE",
            path=path,
            feature="document_text_extraction",
            message="document requires bounded text extraction for this adapter",
        )
        return (
            f"[document title={title} media_type={media_type}]"
            f"{context_text}\n{extracted}"
        )
    if source_type not in {"base64", "url"}:
        raise ProtocolRequestError(
            f"document source type {source_type!r} cannot be represented",
            code="HUB_UNSUPPORTED_DOCUMENT_SOURCE",
            path=f"{path}.source.type",
        )
    _record_lossy(
        plan,
        compatibility_mode=compatibility_mode,
        code="HUB_DEGRADE_DOCUMENT_PLACEHOLDER",
        reject_code="HUB_UNSUPPORTED_DOCUMENT_SOURCE",
        path=path,
        feature="document_placeholder",
        message="binary or remote document requires a visible placeholder",
    )
    return (
        f"[document placeholder title={title} media_type={media_type} "
        f"source={source_type}]{context_text}"
    )


def _search_result_text(
    block: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
    path: str,
) -> str:
    _record_content_metadata(
        block,
        plan=plan,
        compatibility_mode=compatibility_mode,
        path=path,
    )
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text_parts: list[str] = []
        for index, part in enumerate(content):
            if (
                not isinstance(part, dict)
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
            ):
                raise ProtocolRequestError(
                    "search_result content can contain only text blocks",
                    code="HUB_UNSUPPORTED_CONTENT_BLOCK",
                    path=f"{path}.content[{index}]",
                )
            _record_content_metadata(
                part,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=f"{path}.content[{index}]",
            )
            text_parts.append(part["text"])
        text = "\n\n".join(text_parts)
    else:
        raise ProtocolRequestError(
            "search_result.content must be text or text blocks",
            code="HUB_UNSUPPORTED_CONTENT_BLOCK",
            path=f"{path}.content",
        )
    _record_lossy(
        plan,
        compatibility_mode=compatibility_mode,
        code="HUB_DEGRADE_SEARCH_RESULT_TEXTIFIED",
        reject_code="HUB_UNSUPPORTED_CONTENT_BLOCK",
        path=path,
        feature="search_result_textification",
        message="search_result requires a visible provenance text envelope",
    )
    return (
        f"[search_result title={_safe_label(block.get('title'))} "
        f"source={_safe_label(block.get('source'), 'unknown')}]\n{text}"
    )


def _chat_content_and_tools(
    role: str,
    content: object,
    *,
    plan: ConversionPlan | None = None,
    compatibility_mode: str = "visible_lossy",
    message_path: str = "$.messages[]",
) -> tuple[object, list[dict], list[dict], str]:
    if isinstance(content, str):
        return content, [], [], ""
    if not isinstance(content, list):
        return content, [], [], ""

    parts: list[dict] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    reasoning: list[str] = []
    for block_index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        block_path = f"{message_path}.content[{block_index}]"
        if kind == "text" and isinstance(block.get("text"), str):
            _record_content_metadata(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
            parts.append({"type": "text", "text": block["text"]})
        elif kind == "image":
            _record_content_metadata(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
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
            _record_content_metadata(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
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
            _record_content_metadata(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _tool_result_output(
                        block,
                        plan=plan,
                        compatibility_mode=compatibility_mode,
                        path=block_path,
                    ),
                }
            )
        elif kind == "thinking" and isinstance(block.get("thinking"), str):
            _record_content_metadata(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
            _record_thinking_degradation(
                block,
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=block_path,
            )
            reasoning.append(block["thinking"])
        elif kind == "redacted_thinking":
            raise ProtocolRequestError(
                "redacted thinking has no provenance-preserving Chat carrier",
                code="HUB_UNSUPPORTED_REDACTED_THINKING",
                path=block_path,
            )
        elif kind == "document":
            parts.append(
                {
                    "type": "text",
                    "text": _document_text(
                        block,
                        plan=plan,
                        compatibility_mode=compatibility_mode,
                        path=block_path,
                    ),
                }
            )
        elif kind == "search_result":
            parts.append(
                {
                    "type": "text",
                    "text": _search_result_text(
                        block,
                        plan=plan,
                        compatibility_mode=compatibility_mode,
                        path=block_path,
                    ),
                }
            )

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
            raise ProtocolRequestError(
                f"message roles must be system, user, or assistant, got {role!r}",
                code="HUB_INVALID_TOOL_CAUSALITY",
                path="$.messages[].role",
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
                    raise ProtocolRequestError(
                        "tool_use blocks require the assistant role",
                        code="HUB_INVALID_TOOL_CAUSALITY",
                        path="$.messages[].content[]",
                    )
                call_id = block.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in declared:
                    raise ProtocolRequestError(
                        "tool_use ids must be non-empty and unique",
                        code="HUB_INVALID_TOOL_CAUSALITY",
                        path="$.messages[].content[].id",
                    )
                declared.add(call_id)
            elif kind == "tool_result":
                if role != "user":
                    raise ProtocolRequestError(
                        "tool_result blocks require the user role",
                        code="HUB_INVALID_TOOL_CAUSALITY",
                        path="$.messages[].content[]",
                    )
                call_id = block.get("tool_use_id")
                if (
                    not isinstance(call_id, str)
                    or call_id not in declared
                    or call_id in consumed
                ):
                    raise ProtocolRequestError(
                        "tool_result must reference one earlier unconsumed tool_use",
                        code="HUB_INVALID_TOOL_CAUSALITY",
                        path="$.messages[].content[].tool_use_id",
                    )
                consumed.add(call_id)


def anthropic_to_chat(
    payload: dict,
    *,
    plan: ConversionPlan | None = None,
    compatibility_mode: str = "visible_lossy",
) -> dict:
    """Internal request adapter; call only via ``prepare_request``.

    The payload must already have passed request-IR validation; calling this
    directly skips the fail-closed request checks.
    """
    _validate_tool_result_causality(payload.get("messages"))
    result: dict = {"model": payload.get("model"), "messages": []}
    system = _system_text(payload.get("system"))
    if system:
        result["messages"].append({"role": "system", "content": system})

    for message_index, message in enumerate(payload.get("messages", [])):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content, tool_calls, tool_results, reasoning = _chat_content_and_tools(
            str(role),
            message.get("content"),
            plan=plan,
            compatibility_mode=compatibility_mode,
            message_path=f"$.messages[{message_index}]",
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
    for tool_index, tool in enumerate(payload.get("tools", [])):
        if not isinstance(tool, dict) or tool.get("type") == "BatchTool":
            continue
        function = {
            "name": str(tool.get("name", "")),
            "description": tool.get("description") or "",
            "parameters": _clean_schema(
                tool.get("input_schema", {}),
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=f"$.tools[{tool_index}].input_schema",
            ),
        }
        if "strict" in tool:
            function["strict"] = tool["strict"]
        tools.append({"type": "function", "function": function})
    if tools:
        result["tools"] = tools
    if "tool_choice" in payload:
        result["tool_choice"] = _chat_tool_choice(payload["tool_choice"])

    effort = _anthropic_effort(
        payload,
        plan=plan,
        compatibility_mode=compatibility_mode,
    )
    if effort is not None:
        result["reasoning_effort"] = effort
    _apply_cross_request_controls(
        payload,
        result,
        api_format="openai_chat",
        plan=plan,
        compatibility_mode=compatibility_mode,
    )
    return result


def _anthropic_effort(
    payload: dict,
    *,
    plan: ConversionPlan | None,
    compatibility_mode: str,
) -> str | None:
    """Read Claude Code's current effort field with legacy-thinking fallback."""
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = output_config.get("effort")
        if "effort" in output_config:
            if effort not in {"low", "medium", "high", "xhigh"}:
                raise ProtocolRequestError(
                    "output_config.effort is invalid",
                    code="HUB_INVALID_OUTPUT_CONFIG",
                    path="$.output_config.effort",
                )
            return str(effort)

    thinking = payload.get("thinking")
    if thinking is None:
        return None
    if not isinstance(thinking, dict):
        raise ProtocolRequestError(
            "thinking must be an object",
            code="HUB_INVALID_THINKING_CONFIG",
            path="$.thinking",
        )
    _reject_unknown_fields(
        thinking,
        {"type", "budget_tokens", "effort"},
        path="$.thinking",
        code="HUB_INVALID_THINKING_CONFIG",
        label="thinking",
    )
    thinking_type = thinking.get("type")
    if thinking_type == "disabled":
        if set(thinking) - {"type"}:
            raise ProtocolRequestError(
                "disabled thinking cannot include budget or effort",
                code="HUB_INVALID_THINKING_CONFIG",
                path="$.thinking",
            )
        return None
    if thinking_type not in {"enabled", "adaptive"}:
        raise ProtocolRequestError(
            "thinking.type must be enabled, adaptive, or disabled",
            code="HUB_INVALID_THINKING_CONFIG",
            path="$.thinking.type",
        )
    effort = thinking.get("effort")
    if "effort" in thinking:
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise ProtocolRequestError(
                "thinking.effort is invalid",
                code="HUB_INVALID_THINKING_CONFIG",
                path="$.thinking.effort",
            )
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_THINKING_TO_EFFORT",
            reject_code="HUB_UNSUPPORTED_THINKING",
            path="$.thinking",
            feature="thinking_control",
            message="Anthropic thinking controls use a target reasoning effort carrier",
        )
        return str(effort)
    budget = thinking.get("budget_tokens")
    if budget is not None and (
        not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0
    ):
        raise ProtocolRequestError(
            "thinking.budget_tokens must be a positive integer",
            code="HUB_INVALID_THINKING_CONFIG",
            path="$.thinking.budget_tokens",
        )
    if budget is None and thinking_type == "enabled":
        raise ProtocolRequestError(
            "enabled thinking requires budget_tokens or effort",
            code="HUB_INVALID_THINKING_CONFIG",
            path="$.thinking.budget_tokens",
        )
    warning_code = (
        "HUB_DEGRADE_THINKING_BUDGET_TO_EFFORT"
        if budget is not None
        else "HUB_DEGRADE_ADAPTIVE_THINKING_TO_EFFORT"
    )
    _record_lossy(
        plan,
        compatibility_mode=compatibility_mode,
        code=warning_code,
        reject_code="HUB_UNSUPPORTED_THINKING",
        path="$.thinking",
        feature="thinking_control",
        message="Anthropic thinking controls require a lossy target effort mapping",
    )
    return (
        "low"
        if isinstance(budget, int) and budget < 4_000
        else "medium"
        if isinstance(budget, int) and budget < 16_000
        else "high"
    )


def _apply_cross_request_controls(
    payload: dict,
    result: dict,
    *,
    api_format: str,
    plan: ConversionPlan | None,
    compatibility_mode: str,
) -> None:
    if "metadata" in payload:
        metadata = payload["metadata"]
        if not isinstance(metadata, dict):
            raise ProtocolRequestError(
                "metadata must be an object",
                code="HUB_UNSUPPORTED_REQUEST_FIELD",
                path="$.metadata",
            )
        if api_format == "openai_responses":
            result["metadata"] = copy.deepcopy(metadata)
        else:
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code="HUB_DEGRADE_METADATA_DROPPED",
                reject_code="HUB_UNSUPPORTED_METADATA",
                path="$.metadata",
                feature="metadata",
                message="request metadata is not portable across Chat-compatible providers",
            )
    if "service_tier" in payload:
        service_tier = payload["service_tier"]
        if not isinstance(service_tier, str) or not service_tier:
            raise ProtocolRequestError(
                "service_tier must be a non-empty string",
                code="HUB_UNSUPPORTED_REQUEST_FIELD",
                path="$.service_tier",
            )
        result["service_tier"] = service_tier
    parallel = payload.get("parallel_tool_calls", _MISSING)
    if parallel is not _MISSING and not isinstance(parallel, bool):
        raise ProtocolRequestError(
            "parallel_tool_calls must be a boolean",
            code="HUB_INVALID_TOOL_CHOICE",
            path="$.parallel_tool_calls",
        )
    tool_choice = payload.get("tool_choice", _MISSING)
    if tool_choice is not _MISSING:
        if isinstance(tool_choice, str):
            if tool_choice not in {"any", "auto", "none"}:
                raise ProtocolRequestError(
                    "tool_choice string is invalid",
                    code="HUB_INVALID_TOOL_CHOICE",
                    path="$.tool_choice",
                )
        elif isinstance(tool_choice, dict):
            _reject_unknown_fields(
                tool_choice,
                {"type", "name", "disable_parallel_tool_use"},
                path="$.tool_choice",
                code="HUB_INVALID_TOOL_CHOICE",
                label="tool_choice",
            )
            choice_type = tool_choice.get("type")
            if choice_type not in {"any", "auto", "none", "tool"}:
                raise ProtocolRequestError(
                    "tool_choice.type is invalid",
                    code="HUB_INVALID_TOOL_CHOICE",
                    path="$.tool_choice.type",
                )
            if choice_type == "tool":
                if not isinstance(tool_choice.get("name"), str) or not tool_choice["name"]:
                    raise ProtocolRequestError(
                        "tool_choice type tool requires a non-empty name",
                        code="HUB_INVALID_TOOL_CHOICE",
                        path="$.tool_choice.name",
                    )
            elif "name" in tool_choice:
                raise ProtocolRequestError(
                    "tool_choice.name is valid only for type tool",
                    code="HUB_INVALID_TOOL_CHOICE",
                    path="$.tool_choice.name",
                )
            if "disable_parallel_tool_use" in tool_choice:
                disabled = tool_choice["disable_parallel_tool_use"]
                if not isinstance(disabled, bool):
                    raise ProtocolRequestError(
                        "disable_parallel_tool_use must be a boolean",
                        code="HUB_INVALID_TOOL_CHOICE",
                        path="$.tool_choice.disable_parallel_tool_use",
                    )
                choice_parallel = not disabled
                if parallel is not _MISSING and parallel != choice_parallel:
                    raise ProtocolRequestError(
                        "parallel tool controls conflict",
                        code="HUB_INVALID_TOOL_CHOICE",
                        path="$.tool_choice.disable_parallel_tool_use",
                    )
                parallel = choice_parallel
        else:
            raise ProtocolRequestError(
                "tool_choice must be a string or object",
                code="HUB_INVALID_TOOL_CHOICE",
                path="$.tool_choice",
            )
    if parallel is not _MISSING:
        result["parallel_tool_calls"] = parallel

    output_config = payload.get("output_config")
    if output_config is None:
        return
    if not isinstance(output_config, dict):
        raise ProtocolRequestError(
            "output_config must be an object",
            code="HUB_UNSUPPORTED_REQUEST_FIELD",
            path="$.output_config",
        )
    unknown = set(output_config) - {"effort", "format"}
    if unknown:
        key = sorted(unknown)[0]
        raise ProtocolRequestError(
            f"output_config field {key!r} is unsupported",
            code="HUB_UNSUPPORTED_REQUEST_FIELD",
            path=f"$.output_config.{key}",
        )
    format_config = output_config.get("format")
    if format_config is None:
        return
    if (
        not isinstance(format_config, dict)
        or format_config.get("type") != "json_schema"
        or not isinstance(format_config.get("schema"), dict)
    ):
        raise ProtocolRequestError(
            "only output_config.format type json_schema is supported",
            code="HUB_UNSUPPORTED_OUTPUT_FORMAT",
            path="$.output_config.format",
        )
    _reject_unknown_fields(
        format_config,
        {"type", "schema", "name", "strict"},
        path="$.output_config.format",
        code="HUB_UNSUPPORTED_OUTPUT_FORMAT",
        label="output_config.format",
    )
    schema = _clean_schema(
        format_config["schema"],
        plan=plan,
        compatibility_mode=compatibility_mode,
        path="$.output_config.format.schema",
    )
    name = format_config.get("name")
    if name is None:
        name = "response"
    elif not isinstance(name, str) or not name:
        raise ProtocolRequestError(
            "output_config.format.name must be a non-empty string",
            code="HUB_UNSUPPORTED_OUTPUT_FORMAT",
            path="$.output_config.format.name",
        )
    strict_value = format_config.get("strict", False)
    if not isinstance(strict_value, bool):
        raise ProtocolRequestError(
            "output_config.format.strict must be a boolean",
            code="HUB_UNSUPPORTED_OUTPUT_FORMAT",
            path="$.output_config.format.strict",
        )
    strict = strict_value
    if api_format == "openai_chat":
        result["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": strict},
        }
    elif api_format == "openai_responses":
        result["text"] = {
            "format": {
                "type": "json_schema",
                "name": name,
                "schema": schema,
                "strict": strict,
            }
        }


def _responses_input(
    messages: object,
    *,
    plan: ConversionPlan | None = None,
    compatibility_mode: str = "visible_lossy",
) -> list[dict]:
    output: list[dict] = []
    if not isinstance(messages, list):
        return output
    for message_index, message in enumerate(messages):
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

        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            block_path = f"$.messages[{message_index}].content[{block_index}]"
            if kind == "text" and isinstance(block.get("text"), str):
                _record_content_metadata(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
                part_type = "output_text" if role == "assistant" else "input_text"
                message_parts.append({"type": part_type, "text": block["text"]})
            elif kind == "image" and role == "user":
                _record_content_metadata(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
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
                _record_content_metadata(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
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
                _record_content_metadata(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
                flush_message_parts()
                output.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(block.get("tool_use_id", "")),
                        "output": _tool_result_output(
                            block,
                            plan=plan,
                            compatibility_mode=compatibility_mode,
                            path=block_path,
                        ),
                    }
                )
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                _record_content_metadata(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
                _record_thinking_degradation(
                    block,
                    plan=plan,
                    compatibility_mode=compatibility_mode,
                    path=block_path,
                )
                flush_message_parts()
                output.append(
                    {
                        "type": "reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": block["thinking"],
                            }
                        ],
                    }
                )
            elif kind == "redacted_thinking":
                data = block.get("data")
                opaque = (
                    _untag_responses_reasoning(data)
                    if isinstance(data, str)
                    else None
                )
                if opaque is None:
                    raise ProtocolRequestError(
                        "redacted thinking lacks same-adapter provenance",
                        code="HUB_UNSUPPORTED_REDACTED_THINKING",
                        path=block_path,
                    )
                flush_message_parts()
                output.append(
                    {
                        "type": "reasoning",
                        "encrypted_content": opaque,
                        "summary": [],
                    }
                )
                if plan is not None:
                    plan.add(
                        SupportDisposition.EXACT,
                        "HUB_EXACT_REDACTED_THINKING_ROUNDTRIP",
                        block_path,
                        "responses_encrypted_reasoning",
                    )
            elif kind == "document":
                part_type = "output_text" if role == "assistant" else "input_text"
                message_parts.append(
                    {
                        "type": part_type,
                        "text": _document_text(
                            block,
                            plan=plan,
                            compatibility_mode=compatibility_mode,
                            path=block_path,
                        ),
                    }
                )
            elif kind == "search_result":
                part_type = "output_text" if role == "assistant" else "input_text"
                message_parts.append(
                    {
                        "type": part_type,
                        "text": _search_result_text(
                            block,
                            plan=plan,
                            compatibility_mode=compatibility_mode,
                            path=block_path,
                        ),
                    }
                )
        flush_message_parts()
    return output


def anthropic_to_responses(
    payload: dict,
    *,
    codex_oauth: bool = False,
    plan: ConversionPlan | None = None,
    compatibility_mode: str = "visible_lossy",
) -> dict:
    """Internal request adapter; call only via ``prepare_request``.

    The payload must already have passed request-IR validation; calling this
    directly skips the fail-closed request checks.
    """
    _validate_tool_result_causality(payload.get("messages"))
    result: dict = {
        "model": payload.get("model"),
        "input": _responses_input(
            payload.get("messages"),
            plan=plan,
            compatibility_mode=compatibility_mode,
        ),
        "store": False,
    }
    instructions = _system_text(payload.get("system"))
    if instructions:
        result["instructions"] = instructions
    if "max_tokens" in payload:
        result["max_output_tokens"] = payload["max_tokens"]
    if "stop_sequences" in payload:
        _record_lossy(
            plan,
            compatibility_mode=compatibility_mode,
            code="HUB_DEGRADE_STOP_SEQUENCES_DROPPED",
            reject_code="HUB_UNSUPPORTED_STOP_SEQUENCES",
            path="$.stop_sequences",
            feature="stop_sequences",
            message="Responses has no exact stop-sequence carrier",
        )
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if key in payload:
            result[key] = payload[key]

    tools = []
    for tool_index, tool in enumerate(payload.get("tools", [])):
        if not isinstance(tool, dict) or tool.get("type") == "BatchTool":
            continue
        converted_tool = {
            "type": "function",
            "name": str(tool.get("name", "")),
            "description": tool.get("description") or "",
            "parameters": _clean_schema(
                tool.get("input_schema", {}),
                plan=plan,
                compatibility_mode=compatibility_mode,
                path=f"$.tools[{tool_index}].input_schema",
            ),
        }
        if "strict" in tool:
            converted_tool["strict"] = tool["strict"]
        tools.append(converted_tool)
    if tools:
        result["tools"] = tools
    if "tool_choice" in payload:
        result["tool_choice"] = _responses_tool_choice(payload["tool_choice"])

    effort = _anthropic_effort(
        payload,
        plan=plan,
        compatibility_mode=compatibility_mode,
    )
    if effort is not None:
        result["reasoning"] = {"effort": effort, "summary": "auto"}
    if codex_oauth:
        result["include"] = ["reasoning.encrypted_content"]
    _apply_cross_request_controls(
        payload,
        result,
        api_format="openai_responses",
        plan=plan,
        compatibility_mode=compatibility_mode,
    )
    return result


def _payload_from_request_ir(request: RequestIR) -> dict:
    """Materialize only canonical, validated values for a target adapter."""

    payload = copy.deepcopy(request.controls)
    if request.system_blocks:
        payload["system"] = [
            copy.deepcopy(block.value) for block in request.system_blocks
        ]
    payload["messages"] = []
    for message in request.messages:
        if (
            message.content_was_string
            and len(message.blocks) == 1
            and message.blocks[0].kind == "text"
        ):
            content: object = message.blocks[0].value["text"]
        else:
            content = [copy.deepcopy(block.value) for block in message.blocks]
        payload["messages"].append({"role": message.role, "content": content})
    if request.tools:
        payload["tools"] = [copy.deepcopy(tool) for tool in request.tools]
    return payload


class RequestAdapter:
    """Target-specific encoder fed only by the canonical request IR."""

    api_format: str

    def encode(
        self,
        request: RequestIR,
        plan: ConversionPlan,
        *,
        provider_type: str | None,
        compatibility_mode: str,
    ) -> dict:
        raise NotImplementedError


class ChatRequestAdapter(RequestAdapter):
    api_format = "openai_chat"

    def encode(
        self,
        request: RequestIR,
        plan: ConversionPlan,
        *,
        provider_type: str | None,
        compatibility_mode: str,
    ) -> dict:
        return anthropic_to_chat(
            _payload_from_request_ir(request),
            plan=plan,
            compatibility_mode=compatibility_mode,
        )


class ResponsesRequestAdapter(RequestAdapter):
    api_format = "openai_responses"

    def encode(
        self,
        request: RequestIR,
        plan: ConversionPlan,
        *,
        provider_type: str | None,
        compatibility_mode: str,
    ) -> dict:
        return anthropic_to_responses(
            _payload_from_request_ir(request),
            codex_oauth=provider_type == "codex_oauth",
            plan=plan,
            compatibility_mode=compatibility_mode,
        )


REQUEST_ADAPTERS: dict[str, RequestAdapter] = {
    "openai_chat": ChatRequestAdapter(),
    "openai_responses": ResponsesRequestAdapter(),
}


_CROSS_REQUEST_FIELDS = {
    "cache_control",
    "container",
    "inference_geo",
    "max_tokens",
    "mcp_servers",
    "messages",
    "metadata",
    "model",
    "output_config",
    "parallel_tool_calls",
    "service_tier",
    "stop_sequences",
    "stream",
    "system",
    "temperature",
    "thinking",
    "tool_choice",
    "tools",
    "top_k",
    "top_p",
}

_REJECTED_REQUEST_FIELDS = {
    "container": ("HUB_UNSUPPORTED_CONTAINER", "container state requires a dedicated adapter"),
    "inference_geo": (
        "HUB_UNSUPPORTED_INFERENCE_GEO",
        "inference_geo has no lossless target-protocol mapping",
    ),
    "mcp_servers": ("HUB_UNSUPPORTED_MCP", "MCP requires a native MCP adapter"),
}

_DROPPED_REQUEST_FIELDS = {
    "context_management": (
        "HUB_DEGRADE_CONTEXT_MANAGEMENT_DROPPED",
        "HUB_UNSUPPORTED_CONTEXT_MANAGEMENT",
        "context_management",
        "context_management has no equivalent target-protocol mapping",
    ),
}

_CLIENT_BLOCK_TYPES = {
    "document",
    "image",
    "redacted_thinking",
    "search_result",
    "text",
    "thinking",
    "tool_result",
    "tool_use",
}

_CONTENT_BLOCK_FIELDS = {
    "text": {"type", "text", "citations", "cache_control"},
    "image": {"type", "source", "cache_control"},
    "document": {
        "type",
        "source",
        "title",
        "context",
        "citations",
        "cache_control",
    },
    "search_result": {
        "type",
        "source",
        "title",
        "content",
        "citations",
        "cache_control",
    },
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "tool_use": {"type", "id", "name", "input", "caller", "cache_control"},
    "tool_result": {
        "type",
        "tool_use_id",
        "content",
        "is_error",
        "cache_control",
    },
}

_CLIENT_TOOL_FIELDS = {
    "allowed_callers",
    "cache_control",
    "defer_loading",
    "description",
    "input_examples",
    "input_schema",
    "name",
    "strict",
    "type",
}


def _reject_unknown_fields(
    value: dict,
    allowed: set[str],
    *,
    path: str,
    code: str,
    label: str,
) -> None:
    unknown = set(value) - allowed
    if not unknown:
        return
    field_name = sorted(unknown)[0]
    raise ProtocolRequestError(
        f"{label} field {field_name!r} is not supported",
        code=code,
        path=f"{path}.{field_name}",
    )


def _validate_source_fields(
    source: object,
    *,
    path: str,
    document: bool,
) -> None:
    code = (
        "HUB_UNSUPPORTED_DOCUMENT_SOURCE"
        if document
        else "HUB_UNSUPPORTED_IMAGE_SOURCE"
    )
    if not isinstance(source, dict) or not isinstance(source.get("type"), str):
        raise ProtocolRequestError(
            "content source must be a typed object",
            code=code,
            path=path,
        )
    source_type = source["type"]
    allowed_by_type = (
        {
            "base64": {"type", "media_type", "data"},
            "content": {"type", "media_type", "content"},
            "plain_text": {"type", "media_type", "data"},
            "text": {"type", "media_type", "data"},
            "url": {"type", "url"},
        }
        if document
        else {
            "base64": {"type", "media_type", "data"},
            "url": {"type", "url"},
        }
    )
    allowed = allowed_by_type.get(source_type)
    if allowed is None:
        raise ProtocolRequestError(
            f"content source type {source_type!r} is unsupported",
            code=code,
            path=f"{path}.type",
        )
    _reject_unknown_fields(
        source,
        allowed,
        path=path,
        code="HUB_UNSUPPORTED_CONTENT_FIELD",
        label="content source",
    )
    if source_type == "url":
        if not isinstance(source.get("url"), str) or not source["url"]:
            raise ProtocolRequestError(
                "URL content sources require a non-empty url",
                code=code,
                path=f"{path}.url",
            )
        return
    content_key = "content" if source_type == "content" else "data"
    content = source.get(content_key)
    if source_type == "content":
        if not isinstance(content, (str, list)):
            raise ProtocolRequestError(
                "document content sources require text or text blocks",
                code=code,
                path=f"{path}.content",
            )
        if isinstance(content, list):
            for index, part in enumerate(content):
                part_path = f"{path}.content[{index}]"
                if not isinstance(part, dict) or part.get("type") != "text":
                    raise ProtocolRequestError(
                        "document content sources can contain only text blocks",
                        code="HUB_UNSUPPORTED_CONTENT_BLOCK",
                        path=part_path,
                    )
                _reject_unknown_fields(
                    part,
                    _CONTENT_BLOCK_FIELDS["text"],
                    path=part_path,
                    code="HUB_UNSUPPORTED_CONTENT_FIELD",
                    label="nested text block",
                )
                _validate_content_block_details(part, path=part_path)
    elif not isinstance(content, str) or not content:
        raise ProtocolRequestError(
            "inline content sources require non-empty string data",
            code=code,
            path=f"{path}.data",
        )
    if "media_type" in source and not isinstance(source["media_type"], str):
        raise ProtocolRequestError(
            "content source media_type must be a string",
            code=code,
            path=f"{path}.media_type",
        )


def _validate_content_block_details(block: dict, *, path: str) -> None:
    kind = block["type"]
    if kind == "text":
        if not isinstance(block.get("text"), str):
            raise ProtocolRequestError(
                "text blocks require string text",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{path}.text",
            )
        return
    if kind == "image":
        _validate_source_fields(block.get("source"), path=f"{path}.source", document=False)
        return
    if kind == "document":
        _validate_source_fields(block.get("source"), path=f"{path}.source", document=True)
        for field_name in ("title", "context"):
            if field_name in block and not isinstance(block[field_name], str):
                raise ProtocolRequestError(
                    f"document.{field_name} must be a string",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=f"{path}.{field_name}",
                )
        return
    if kind == "search_result":
        for field_name in ("source", "title"):
            if field_name in block and not isinstance(block[field_name], str):
                raise ProtocolRequestError(
                    f"search_result.{field_name} must be a string",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=f"{path}.{field_name}",
                )
        content = block.get("content")
        if not isinstance(content, (str, list)):
            raise ProtocolRequestError(
                "search_result.content must be text or text blocks",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{path}.content",
            )
        if isinstance(content, list):
            for index, part in enumerate(content):
                part_path = f"{path}.content[{index}]"
                if not isinstance(part, dict) or part.get("type") != "text":
                    raise ProtocolRequestError(
                        "search_result content can contain only text blocks",
                        code="HUB_UNSUPPORTED_CONTENT_BLOCK",
                        path=part_path,
                    )
                _reject_unknown_fields(
                    part,
                    _CONTENT_BLOCK_FIELDS["text"],
                    path=part_path,
                    code="HUB_UNSUPPORTED_CONTENT_FIELD",
                    label="nested text block",
                )
                _validate_content_block_details(part, path=part_path)
        return
    if kind == "thinking":
        if not isinstance(block.get("thinking"), str):
            raise ProtocolRequestError(
                "thinking blocks require string thinking",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{path}.thinking",
            )
        if "signature" in block and not isinstance(block["signature"], str):
            raise ProtocolRequestError(
                "thinking signatures must be strings",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{path}.signature",
            )
        return
    if kind == "redacted_thinking":
        if not isinstance(block.get("data"), str) or not block["data"]:
            raise ProtocolRequestError(
                "redacted_thinking requires non-empty opaque data",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{path}.data",
            )
        return
    if kind == "tool_use":
        for field_name in ("id", "name"):
            if not isinstance(block.get(field_name), str) or not block[field_name]:
                raise ProtocolRequestError(
                    f"tool_use.{field_name} must be a non-empty string",
                    code="HUB_INVALID_TOOL_CAUSALITY",
                    path=f"{path}.{field_name}",
                )
        if not isinstance(block.get("input"), dict):
            raise ProtocolRequestError(
                "tool_use.input must be an object",
                code="HUB_INVALID_TOOL_CAUSALITY",
                path=f"{path}.input",
            )
        if "caller" in block:
            raise ProtocolRequestError(
                "tool_use.caller requires a native server-tool adapter",
                code="HUB_UNSUPPORTED_SERVER_TOOL",
                path=f"{path}.caller",
            )
        return
    if kind == "tool_result":
        if not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]:
            raise ProtocolRequestError(
                "tool_result.tool_use_id must be a non-empty string",
                code="HUB_INVALID_TOOL_CAUSALITY",
                path=f"{path}.tool_use_id",
            )
        if "is_error" in block and not isinstance(block["is_error"], bool):
            raise ProtocolRequestError(
                "tool_result.is_error must be a boolean",
                code="HUB_INVALID_TOOL_RESULT_CONTENT",
                path=f"{path}.is_error",
            )
        content = block.get("content", "")
        if isinstance(content, list):
            allowed_nested = {"text", "image", "document", "search_result"}
            for index, part in enumerate(content):
                part_path = f"{path}.content[{index}]"
                if (
                    not isinstance(part, dict)
                    or not isinstance(part.get("type"), str)
                    or part["type"] not in allowed_nested
                ):
                    raise ProtocolRequestError(
                        "tool_result contains an unsupported nested content block",
                        code="HUB_UNSUPPORTED_TOOL_RESULT_PART",
                        path=part_path,
                    )
                _reject_unknown_fields(
                    part,
                    _CONTENT_BLOCK_FIELDS[part["type"]],
                    path=part_path,
                    code="HUB_UNSUPPORTED_CONTENT_FIELD",
                    label="nested content block",
                )
                _validate_content_block_details(part, path=part_path)
        elif not isinstance(content, str):
            raise ProtocolRequestError(
                "tool_result.content must be text or an array of content blocks",
                code="HUB_INVALID_TOOL_RESULT_CONTENT",
                path=f"{path}.content",
            )

_SERVER_BLOCK_CODES = {
    "bash_code_execution_tool_result": "HUB_UNSUPPORTED_SERVER_TOOL_RESULT",
    "code_execution_tool_result": "HUB_UNSUPPORTED_SERVER_TOOL_RESULT",
    "container_upload": "HUB_UNSUPPORTED_CONTENT_BLOCK",
    "mcp_tool_result": "HUB_UNSUPPORTED_MCP",
    "mcp_tool_use": "HUB_UNSUPPORTED_MCP",
    "server_tool_use": "HUB_UNSUPPORTED_SERVER_TOOL",
    "text_editor_code_execution_tool_result": "HUB_UNSUPPORTED_SERVER_TOOL_RESULT",
    "tool_search_tool_result": "HUB_UNSUPPORTED_TOOL_SEARCH",
    "web_fetch_tool_result": "HUB_UNSUPPORTED_SERVER_TOOL_RESULT",
    "web_search_tool_result": "HUB_UNSUPPORTED_SERVER_TOOL_RESULT",
}


def _canonical_system_blocks(
    system: object,
    *,
    path: str = "$.system",
    require_nonempty: bool = False,
) -> list[dict]:
    if system is None:
        blocks: list[dict] = []
        if require_nonempty:
            raise ProtocolRequestError(
                "embedded system context requires non-empty text content",
                code="HUB_INVALID_SYSTEM_BLOCK",
                path=path,
            )
        return blocks
    if isinstance(system, str):
        if require_nonempty and not system:
            raise ProtocolRequestError(
                "embedded system context requires non-empty text content",
                code="HUB_INVALID_SYSTEM_BLOCK",
                path=path,
            )
        return [{"type": "text", "text": system}]
    if not isinstance(system, list):
        raise ProtocolRequestError(
            "system must be a string or an array of text blocks",
            code="HUB_INVALID_SYSTEM_BLOCK",
            path=path,
        )
    blocks: list[dict] = []
    for index, block in enumerate(system):
        block_path = f"{path}[{index}]"
        if (
            not isinstance(block, dict)
            or block.get("type", "text") != "text"
            or not isinstance(block.get("text"), str)
            or (require_nonempty and not block.get("text"))
        ):
            raise ProtocolRequestError(
                "embedded system context can contain only text blocks",
                code="HUB_INVALID_SYSTEM_BLOCK",
                path=block_path,
            )
        copied = copy.deepcopy(block)
        copied.setdefault("type", "text")
        blocks.append(copied)
    if require_nonempty and not blocks:
        raise ProtocolRequestError(
            "embedded system context requires non-empty text content",
            code="HUB_INVALID_SYSTEM_BLOCK",
            path=path,
        )
    return blocks


def _parse_request_ir(
    payload: dict,
    plan: ConversionPlan,
    *,
    compatibility_mode: str,
) -> RequestIR:
    if not isinstance(payload, dict):
        raise ProtocolRequestError(
            "request body must be an object",
            code="HUB_INVALID_REQUEST",
            path="$",
        )
    for key in payload:
        if key in _REJECTED_REQUEST_FIELDS:
            code, message = _REJECTED_REQUEST_FIELDS[key]
            raise ProtocolRequestError(message, code=code, path=f"$.{key}")
        if key in _DROPPED_REQUEST_FIELDS:
            warning_code, reject_code, feature, message = _DROPPED_REQUEST_FIELDS[key]
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code=warning_code,
                reject_code=reject_code,
                path=f"$.{key}",
                feature=feature,
                message=message,
            )
            continue
        if key not in _CROSS_REQUEST_FIELDS:
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code="HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                reject_code="HUB_UNSUPPORTED_REQUEST_FIELD",
                path=f"$.{key}",
                feature="unknown_request_extension",
                message=f"request field {key!r} is not supported by this adapter",
            )
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ProtocolRequestError(
            "model must be a non-empty string",
            code="HUB_INVALID_MODEL",
            path="$.model",
        )
    if "max_tokens" in payload and (
        not isinstance(payload["max_tokens"], int)
        or isinstance(payload["max_tokens"], bool)
        or payload["max_tokens"] <= 0
    ):
        raise ProtocolRequestError(
            "max_tokens must be a positive integer",
            code="HUB_INVALID_MAX_TOKENS",
            path="$.max_tokens",
        )
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise ProtocolRequestError(
            "stream must be a boolean",
            code="HUB_INVALID_STREAM",
            path="$.stream",
        )
    for numeric_field in ("temperature", "top_p"):
        if numeric_field in payload:
            value = payload[numeric_field]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise ProtocolRequestError(
                    f"{numeric_field} must be a finite number",
                    code="HUB_INVALID_REQUEST_FIELD",
                    path=f"$.{numeric_field}",
                )
    if "top_k" in payload and (
        not isinstance(payload["top_k"], int)
        or isinstance(payload["top_k"], bool)
        or payload["top_k"] <= 0
    ):
        raise ProtocolRequestError(
            "top_k must be a positive integer",
            code="HUB_INVALID_REQUEST_FIELD",
            path="$.top_k",
        )
    if "stop_sequences" in payload:
        sequences = payload["stop_sequences"]
        if not isinstance(sequences, list) or any(
            not isinstance(sequence, str) or not sequence for sequence in sequences
        ):
            raise ProtocolRequestError(
                "stop_sequences must be an array of non-empty strings",
                code="HUB_INVALID_STOP_SEQUENCES",
                path="$.stop_sequences",
            )
    for field_name, warning_code, reject_code in (
        (
            "cache_control",
            "HUB_DEGRADE_CACHE_CONTROL_DROPPED",
            "HUB_UNSUPPORTED_CACHE_CONTROL",
        ),
        ("top_k", "HUB_DEGRADE_TOP_K_DROPPED", "HUB_UNSUPPORTED_TOP_K"),
    ):
        if field_name in payload:
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code=warning_code,
                reject_code=reject_code,
                path=f"$.{field_name}",
                feature=field_name,
                message=f"{field_name} has no exact target-protocol mapping",
            )
    system_values = _canonical_system_blocks(payload.get("system"))
    system_blocks = tuple(
        ContentBlockIR("text", block, f"$.system[{index}]")
        for index, block in enumerate(system_values)
    )
    for block in system_blocks:
        for key in sorted(set(block.value) - {"type", "text"}):
            metadata_path = f"{block.path}.{key}"
            if compatibility_mode == "strict":
                raise ProtocolRequestError(
                    "system block metadata cannot be represented by this adapter",
                    code="HUB_UNSUPPORTED_SYSTEM_METADATA",
                    path=metadata_path,
                )
            plan.add(
                SupportDisposition.DEGRADED,
                "HUB_DEGRADE_SYSTEM_METADATA_DROPPED",
                metadata_path,
                "system_block_metadata",
            )
    raw_messages = payload.get("messages")
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list):
        raise ProtocolRequestError(
            "messages must be an array",
            code="HUB_INVALID_MESSAGES",
            path="$.messages",
        )
    messages: list[MessageIR] = []
    for message_index, raw_message in enumerate(raw_messages):
        message_path = f"$.messages[{message_index}]"
        if not isinstance(raw_message, dict):
            raise ProtocolRequestError(
                "each message must be an object",
                code="HUB_INVALID_MESSAGES",
                path=message_path,
            )
        _reject_unknown_fields(
            raw_message,
            {"role", "content"},
            path=message_path,
            code="HUB_UNSUPPORTED_MESSAGE_FIELD",
            label="message",
        )
        role = raw_message.get("role")
        if role not in {"system", "user", "assistant"}:
            raise ProtocolRequestError(
                f"message roles must be system, user, or assistant, got {role!r}",
                code="HUB_INVALID_MESSAGE_ROLE",
                path=f"{message_path}.role",
            )
        content = raw_message.get("content")
        if role == "system":
            _canonical_system_blocks(
                content,
                path=f"{message_path}.content",
                require_nonempty=True,
            )
        content_was_string = isinstance(content, str)
        if content_was_string:
            raw_blocks: list[object] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            if not content:
                raise ProtocolRequestError(
                    "message content must contain at least one block",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=f"{message_path}.content",
                )
            raw_blocks = content
        else:
            raise ProtocolRequestError(
                "message content must be a string or content-block array",
                code="HUB_INVALID_CONTENT_BLOCK",
                path=f"{message_path}.content",
            )
        blocks: list[ContentBlockIR] = []
        for block_index, raw_block in enumerate(raw_blocks):
            block_path = f"{message_path}.content[{block_index}]"
            if not isinstance(raw_block, dict) or not isinstance(
                raw_block.get("type"), str
            ):
                raise ProtocolRequestError(
                    "content blocks must be typed objects",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=block_path,
                )
            kind = raw_block["type"]
            if kind in _SERVER_BLOCK_CODES:
                raise ProtocolRequestError(
                    f"content block {kind!r} requires a native server-tool adapter",
                    code=_SERVER_BLOCK_CODES[kind],
                    path=block_path,
                )
            if kind not in _CLIENT_BLOCK_TYPES:
                raise ProtocolRequestError(
                    f"content block {kind!r} is not supported by this adapter",
                    code="HUB_UNSUPPORTED_CONTENT_BLOCK",
                    path=block_path,
                )
            unknown_fields = set(raw_block) - _CONTENT_BLOCK_FIELDS[kind]
            if unknown_fields:
                field_name = sorted(unknown_fields)[0]
                raise ProtocolRequestError(
                    f"content block field {field_name!r} is not supported",
                    code="HUB_UNSUPPORTED_CONTENT_FIELD",
                    path=f"{block_path}.{field_name}",
                )
            _validate_content_block_details(raw_block, path=block_path)
            if role == "system" and kind != "text":
                raise ProtocolRequestError(
                    "embedded system context can contain only text blocks",
                    code="HUB_INVALID_SYSTEM_BLOCK",
                    path=block_path,
                )
            if kind in {"thinking", "redacted_thinking", "tool_use"} and role != "assistant":
                raise ProtocolRequestError(
                    f"{kind} blocks require the assistant role",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=block_path,
                )
            if kind in {"image", "document", "search_result", "tool_result"} and role != "user":
                raise ProtocolRequestError(
                    f"{kind} blocks require the user role",
                    code="HUB_INVALID_CONTENT_BLOCK",
                    path=block_path,
                )
            blocks.append(ContentBlockIR(kind, copy.deepcopy(raw_block), block_path))
        messages.append(
            MessageIR(
                str(role),
                tuple(blocks),
                content_was_string,
                message_path,
            )
        )
    controls = {
        key: copy.deepcopy(payload[key])
        for key in _CROSS_REQUEST_FIELDS
        if key in payload and key not in {"messages", "system", "tools"}
    }
    tools = payload.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise ProtocolRequestError(
            "tools must be an array",
            code="HUB_UNSUPPORTED_TOOL_TYPE",
            path="$.tools",
        )
    canonical_tools: list[dict] = []
    for index, tool in enumerate(tools):
        tool_path = f"$.tools[{index}]"
        if not isinstance(tool, dict):
            raise ProtocolRequestError(
                "each tool must be an object",
                code="HUB_UNSUPPORTED_TOOL_TYPE",
                path=tool_path,
            )
        tool_type = tool.get("type")
        if tool_type in (None, "custom"):
            _reject_unknown_fields(
                tool,
                _CLIENT_TOOL_FIELDS,
                path=tool_path,
                code="HUB_UNSUPPORTED_TOOL_FIELD",
                label="client tool",
            )
            if not isinstance(tool.get("name"), str) or not tool.get("name"):
                raise ProtocolRequestError(
                    "client tools require a non-empty name",
                    code="HUB_UNSUPPORTED_TOOL_TYPE",
                    path=f"{tool_path}.name",
                )
            if not isinstance(tool.get("input_schema"), dict):
                raise ProtocolRequestError(
                    "client tools require an object input_schema",
                    code="HUB_UNSUPPORTED_SCHEMA",
                    path=f"{tool_path}.input_schema",
                )
            if "description" in tool and not isinstance(tool["description"], str):
                raise ProtocolRequestError(
                    "client tool description must be a string",
                    code="HUB_UNSUPPORTED_TOOL_FIELD",
                    path=f"{tool_path}.description",
                )
            if "strict" in tool and not isinstance(tool["strict"], bool):
                raise ProtocolRequestError(
                    "client tool strict must be a boolean",
                    code="HUB_UNSUPPORTED_TOOL_FIELD",
                    path=f"{tool_path}.strict",
                )
            defer_loading = tool.get("defer_loading", False)
            if not isinstance(defer_loading, bool):
                raise ProtocolRequestError(
                    "client tool defer_loading must be a boolean",
                    code="HUB_UNSUPPORTED_TOOL_FIELD",
                    path=f"{tool_path}.defer_loading",
                )
            if defer_loading:
                _record_lossy(
                    plan,
                    compatibility_mode=compatibility_mode,
                    code="HUB_DEGRADE_DEFERRED_TOOL_EAGERLY_LOADED",
                    reject_code="HUB_UNSUPPORTED_TOOL_SEARCH",
                    path=f"{tool_path}.defer_loading",
                    feature="tool_search",
                    message="deferred client tool must be eagerly loaded by this adapter",
                )
            if "allowed_callers" in tool:
                raise ProtocolRequestError(
                    "allowed_callers requires native server-tool execution",
                    code="HUB_UNSUPPORTED_SERVER_TOOL",
                    path=tool_path,
                )
            for metadata_key in ("cache_control", "input_examples"):
                if metadata_key in tool:
                    _record_lossy(
                        plan,
                        compatibility_mode=compatibility_mode,
                        code="HUB_DEGRADE_TOOL_METADATA_DROPPED",
                        reject_code="HUB_UNSUPPORTED_TOOL_TYPE",
                        path=f"{tool_path}.{metadata_key}",
                        feature="tool_metadata",
                        message="tool metadata has no exact target-protocol mapping",
                    )
            canonical_tool = copy.deepcopy(tool)
            canonical_tool.pop("defer_loading", None)
            canonical_tools.append(canonical_tool)
            continue
        if tool_type == "BatchTool":
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code="HUB_DEGRADE_BATCH_TOOL_OMITTED",
                reject_code="HUB_UNSUPPORTED_TOOL_TYPE",
                path=tool_path,
                feature="claude_code_batch_tool",
                message="BatchTool is a Claude Code compatibility-only tool",
            )
            continue
        if not isinstance(tool_type, str):
            code = "HUB_UNSUPPORTED_TOOL_TYPE"
        elif "mcp" in tool_type:
            code = "HUB_UNSUPPORTED_MCP"
        elif tool_type.startswith("tool_search"):
            _record_lossy(
                plan,
                compatibility_mode=compatibility_mode,
                code="HUB_DEGRADE_TOOL_SEARCH_OMITTED",
                reject_code="HUB_UNSUPPORTED_TOOL_SEARCH",
                path=tool_path,
                feature="tool_search",
                message="native tool-search control is unnecessary after eager tool loading",
            )
            continue
        elif tool_type.startswith(
            (
                "bash_code_execution",
                "code_execution",
                "computer",
                "memory",
                "text_editor",
                "web_fetch",
                "web_search",
            )
        ):
            code = "HUB_UNSUPPORTED_SERVER_TOOL"
        else:
            code = "HUB_UNSUPPORTED_TOOL_TYPE"
        raise ProtocolRequestError(
            f"tool type {tool_type!r} requires a dedicated capability adapter",
            code=code,
            path=tool_path,
        )
    return RequestIR(
        copy.deepcopy(payload),
        system_blocks,
        tuple(messages),
        tuple(canonical_tools),
        controls,
    )


def _normalize_native_system_roles(
    payload: dict,
    plan: ConversionPlan,
    *,
    compatibility_mode: str,
) -> dict:
    """Promote Claude Code's system-role extension for strict Anthropic servers.

    All unrelated fields remain byte-for-byte-equivalent JSON values, including
    unknown native extensions. The input object itself is never mutated.
    """

    result = copy.deepcopy(payload)
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    promoted: list[dict] = []
    retained: list[object] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            retained.append(message)
            continue
        path = f"$.messages[{index}]"
        system_blocks = _canonical_system_blocks(
            message.get("content"),
            path=f"{path}.content",
            require_nonempty=True,
        )
        if compatibility_mode == "strict":
            raise ProtocolRequestError(
                "messages role 'system' is a client compatibility extension",
                code="HUB_INVALID_MESSAGE_ROLE",
                path=f"{path}.role",
            )
        extra_keys = set(message) - {"role", "content"}
        if extra_keys:
            raise ProtocolRequestError(
                "system message metadata cannot be promoted losslessly",
                code="HUB_INVALID_SYSTEM_BLOCK",
                path=path,
            )
        promoted.extend(system_blocks)
        plan.add(
            SupportDisposition.DEGRADED,
            "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED",
            f"{path}.role",
            "system_role_extension",
        )
    if not promoted:
        return result
    existing = _canonical_system_blocks(result.get("system"))
    result["system"] = [*existing, *promoted]
    result["messages"] = retained
    return result


def prepare_request(
    payload: dict,
    api_format: str,
    *,
    provider_type: str | None = None,
    compatibility_mode: str = "visible_lossy",
) -> PreparedRequest:
    """Parse, plan and encode one Anthropic Messages request.

    ``transform_request`` remains the compatibility facade; callers that need
    observable degradation metadata use this richer seam.
    """

    if compatibility_mode not in {"visible_lossy", "strict"}:
        raise ValueError("compatibility_mode must be visible_lossy or strict")
    profile = CAPABILITY_PROFILES.get(api_format)
    if profile is None:
        raise ProtocolRequestError(
            f"unsupported protocol adapter {api_format!r}",
            code="HUB_API_FORMAT_UNSUPPORTED",
            path="$.model",
        )
    if profile.availability != "available":
        raise ProtocolRequestError(
            f"protocol adapter {api_format!r} is not available",
            code="HUB_ADAPTER_UNAVAILABLE",
            path="$.model",
        )
    plan = ConversionPlan(adapter=profile.name)
    if api_format == "anthropic":
        body = _normalize_native_system_roles(
            payload,
            plan,
            compatibility_mode=compatibility_mode,
        )
    elif api_format in REQUEST_ADAPTERS:
        request_ir = _parse_request_ir(
            payload,
            plan,
            compatibility_mode=compatibility_mode,
        )
        body = REQUEST_ADAPTERS[api_format].encode(
            request_ir,
            plan,
            provider_type=provider_type,
            compatibility_mode=compatibility_mode,
        )
    else:  # pragma: no cover - registry availability is checked above.
        raise AssertionError(api_format)
    return PreparedRequest(profile.endpoint, body, plan)


def transform_request(
    payload: dict, api_format: str, *, provider_type: str | None = None
) -> tuple[str, dict]:
    prepared = prepare_request(
        payload,
        api_format,
        provider_type=provider_type,
    )
    return prepared.endpoint, prepared.payload


def _stream_identifier(*values: object) -> str | None:
    for value in values:
        if value is not None and value != "":
            return str(value)
    return None


def _response_base_usage(
    body: dict,
    *,
    api_format: str,
    input_key: str,
    output_key: str,
    plan: ConversionPlan | None,
) -> tuple[dict, int, int]:
    raw = body.get("usage", _MISSING)
    if raw is _MISSING or raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        raise ProtocolTransformError(
            "upstream usage must be an object",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )
    degraded_paths = _validate_upstream_usage_fields(raw, api_format)
    for path in degraded_paths:
        _record_response_metadata_degradation(plan, path)
    _upstream_usage_total(
        raw,
        input_key=input_key,
        output_key=output_key,
    )
    values: list[int] = []
    for key, feature in (
        (input_key, "input_usage"),
        (output_key, "output_usage"),
    ):
        if key not in raw:
            values.append(0)
            if plan is not None:
                plan.add(
                    SupportDisposition.DEGRADED,
                    "HUB_USAGE_PROVENANCE_UNAVAILABLE",
                    f"$.usage.{key}",
                    feature,
                )
            continue
        parsed = _token_count(raw[key], -1)
        if parsed < 0:
            raise ProtocolTransformError(
                f"upstream usage counter {key!r} is invalid",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        values.append(parsed)
    return raw, values[0], values[1]


def _stop_reason(
    reason: object,
    *,
    has_tool: bool = False,
    refused: bool = False,
) -> str:
    tool_reasons = {"tool_calls", "function_call", "tool_use"}
    neutral_reasons = {None, "", "stop", "completed", "end_turn"}
    refusal_reasons = {"content_filter", "refusal"}
    if has_tool:
        if refused or reason not in tool_reasons | neutral_reasons:
            raise ProtocolTransformError(
                "upstream terminal reason conflicts with completed tool output",
                code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            )
        return "tool_use"
    if reason in tool_reasons:
        raise ProtocolTransformError(
            "upstream tool stop reason has no completed tool output",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
        )
    if refused:
        if reason not in refusal_reasons | neutral_reasons:
            raise ProtocolTransformError(
                "upstream terminal reason conflicts with refusal output",
                code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            )
        return "refusal"
    if reason in refusal_reasons:
        return "refusal"
    if reason in {"length", "max_output_tokens", "max_tokens"}:
        return "max_tokens"
    if reason == "stop_sequence":
        return "stop_sequence"
    if reason in neutral_reasons:
        return "end_turn"
    if reason in {"pause_turn", "model_context_window_exceeded"}:
        return str(reason)
    raise ProtocolTransformError(
        f"upstream stop reason {reason!r} cannot be represented",
        code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
    )


def _responses_stop_reason(
    body: dict,
    *,
    has_tool: bool,
    refused: bool,
) -> str:
    raw_details = body.get("incomplete_details", _MISSING)
    if raw_details is _MISSING or raw_details is None:
        incomplete_details: dict = {}
    elif isinstance(raw_details, dict):
        incomplete_details = raw_details
    else:
        raise ProtocolTransformError(
            "OpenAI Responses incomplete_details must be an object or null",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.incomplete_details",
        )
    unknown_detail_fields = set(incomplete_details) - {"reason"}
    if unknown_detail_fields:
        field_name = sorted(unknown_detail_fields)[0]
        raise ProtocolTransformError(
            f"OpenAI Responses incomplete_details field {field_name!r} is unsupported",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path=f"$.incomplete_details.{field_name}",
        )

    status = body.get("status", _MISSING)
    if status is not _MISSING and (
        not isinstance(status, str) or not status
    ):
        raise ProtocolTransformError(
            "OpenAI Responses status must be a non-empty string",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.status",
        )
    incomplete_reason = incomplete_details.get("reason", _MISSING)
    if incomplete_reason is not _MISSING and (
        not isinstance(incomplete_reason, str) or not incomplete_reason
    ):
        raise ProtocolTransformError(
            "OpenAI Responses incomplete reason must be a non-empty string",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.incomplete_details.reason",
        )
    if status is _MISSING and incomplete_reason is _MISSING:
        raise ProtocolTransformError(
            "OpenAI Responses response has no explicit terminal reason",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.status",
        )
    if status == "incomplete" and incomplete_reason is _MISSING:
        raise ProtocolTransformError(
            "OpenAI Responses incomplete status requires a reason",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.incomplete_details.reason",
        )
    if (
        status is not _MISSING
        and status != "incomplete"
        and incomplete_reason is not _MISSING
    ):
        raise ProtocolTransformError(
            "OpenAI Responses status conflicts with incomplete_details.reason",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.incomplete_details.reason",
        )

    # Validate every supplied terminal carrier before output-derived semantics
    # can override the final Anthropic stop reason.
    if status is not _MISSING and status != "incomplete":
        _stop_reason(status)
    if incomplete_reason is not _MISSING:
        _stop_reason(incomplete_reason)
    reason = incomplete_reason if incomplete_reason is not _MISSING else status
    return _stop_reason(reason, has_tool=has_tool, refused=refused)


def _parse_upstream_tool_arguments(arguments: object) -> dict:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError as exc:
        raise ProtocolTransformError(
            "upstream tool arguments are not valid JSON",
            code="HUB_UPSTREAM_TOOL_ARGUMENTS_INVALID",
        ) from exc
    if not isinstance(parsed, dict):
        raise ProtocolTransformError(
            "upstream tool arguments must be a JSON object",
            code="HUB_UPSTREAM_TOOL_ARGUMENTS_INVALID",
        )
    return parsed


def _record_response_citation_degradation(
    plan: ConversionPlan | None,
    path: str,
) -> None:
    if plan is not None:
        plan.add(
            SupportDisposition.DEGRADED,
            "HUB_DEGRADE_CITATION_METADATA_DROPPED",
            path,
            "upstream_citation_metadata",
        )


def _record_response_metadata_degradation(
    plan: ConversionPlan | None,
    path: str,
) -> None:
    if plan is not None:
        plan.add(
            SupportDisposition.DEGRADED,
            "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            path,
            "upstream_response_metadata",
        )


def _validate_response_item_id(
    item: dict,
    *,
    path: str,
    code: str,
    plan: ConversionPlan | None,
    record_metadata: bool = True,
) -> None:
    if "id" not in item:
        return
    if not isinstance(item["id"], str) or not item["id"]:
        raise ProtocolTransformError(
            "OpenAI Responses output item id must be non-empty text",
            code=code,
            path=f"{path}.id",
        )
    if record_metadata:
        _record_response_metadata_degradation(plan, f"{path}.id")


def _require_upstream_response_allowlist(
    value: dict,
    allowed_fields: set[str],
    *,
    path: str,
    label: str,
) -> None:
    unknown_fields = set(value) - allowed_fields
    if unknown_fields:
        field_name = sorted(unknown_fields)[0]
        raise ProtocolTransformError(
            f"{label} field {field_name!r} is unsupported",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path=f"{path}.{field_name}",
        )


def _require_upstream_field_allowlist(
    value: dict,
    allowed_fields: set[str],
    *,
    path: str,
    label: str,
) -> None:
    unknown_fields = set(value) - allowed_fields
    if unknown_fields:
        field_name = sorted(unknown_fields)[0]
        raise ProtocolTransformError(
            f"{label} field {field_name!r} is unsupported",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            path=f"{path}.{field_name}",
        )


def chat_to_anthropic(
    body: dict,
    *,
    plan: ConversionPlan | None = None,
) -> tuple[dict, UsageReceipt]:
    _require_upstream_response_allowlist(
        body,
        {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "service_tier",
            "system_fingerprint",
        },
        path="$",
        label="OpenAI Chat response",
    )
    for field_name in ("id", "model"):
        if field_name in body and (
            not isinstance(body[field_name], str) or not body[field_name]
        ):
            raise ProtocolTransformError(
                f"OpenAI Chat response {field_name} must be non-empty text",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path=f"$.{field_name}",
            )
    if "object" in body and (
        not isinstance(body["object"], str) or not body["object"]
    ):
        raise ProtocolTransformError(
            "OpenAI Chat response object must be non-empty text",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.object",
        )
    if "created" in body and (
        not isinstance(body["created"], int)
        or isinstance(body["created"], bool)
        or body["created"] < 0
    ):
        raise ProtocolTransformError(
            "OpenAI Chat response created must be a non-negative integer",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.created",
        )
    for field_name in ("service_tier", "system_fingerprint"):
        if field_name in body and body[field_name] is not None and not isinstance(
            body[field_name], str
        ):
            raise ProtocolTransformError(
                f"OpenAI Chat response {field_name} must be text or null",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path=f"$.{field_name}",
            )
    for field_name in ("object", "created", "service_tier", "system_fingerprint"):
        if field_name in body:
            _record_response_metadata_degradation(plan, f"$.{field_name}")

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProtocolTransformError(
            "OpenAI Chat response has no choices",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.choices",
        )
    if len(choices) != 1:
        raise ProtocolTransformError(
            "OpenAI Chat multiple choices cannot be represented",
            code="HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProtocolTransformError(
            "OpenAI Chat response choice must be an object",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.choices[0]",
        )
    _require_upstream_response_allowlist(
        choice,
        {"index", "message", "finish_reason", "logprobs"},
        path="$.choices[0]",
        label="OpenAI Chat response choice",
    )
    if "index" in choice and (
        not isinstance(choice["index"], int)
        or isinstance(choice["index"], bool)
        or choice["index"] < 0
    ):
        raise ProtocolTransformError(
            "OpenAI Chat response choice index must be a non-negative integer",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.choices[0].index",
        )
    if choice.get("index", 0) != 0:
        raise ProtocolTransformError(
            "OpenAI Chat response choice index must be zero",
            code="HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
            path="$.choices[0].index",
        )
    if "logprobs" in choice:
        if choice["logprobs"] is not None and not isinstance(
            choice["logprobs"], dict
        ):
            raise ProtocolTransformError(
                "OpenAI Chat response logprobs must be an object or null",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path="$.choices[0].logprobs",
            )
        _record_response_metadata_degradation(plan, "$.choices[0].logprobs")
    if not isinstance(choice.get("message"), dict):
        raise ProtocolTransformError(
            "OpenAI Chat response has no message",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.choices[0].message",
        )
    finish_reason = choice.get("finish_reason", _MISSING)
    if not isinstance(finish_reason, str) or not finish_reason:
        raise ProtocolTransformError(
            "OpenAI Chat finish_reason must be a non-empty string",
            code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            path="$.choices[0].finish_reason",
        )
    # Validate the upstream terminal reason independently of output-derived
    # tool/refusal semantics so those blocks cannot mask a future reason.
    _stop_reason(
        finish_reason,
        has_tool=finish_reason in {"tool_calls", "function_call"},
    )
    message = choice["message"]
    _require_upstream_field_allowlist(
        message,
        {
            "role",
            "content",
            "reasoning_content",
            "refusal",
            "tool_calls",
            "annotations",
            "citations",
            "audio",
            "function_call",
            "reasoning_signature",
            "signature",
        },
        path="$.choices[0].message",
        label="OpenAI Chat message",
    )
    if "role" in message and message["role"] != "assistant":
        raise ProtocolTransformError(
            "OpenAI Chat response message role must be assistant",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            path="$.choices[0].message.role",
        )
    for field_name in ("reasoning_signature", "signature"):
        if field_name in message:
            raise ProtocolTransformError(
                f"OpenAI Chat message field {field_name!r} is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path=f"$.choices[0].message.{field_name}",
            )
    if "annotations" in message or "citations" in message:
        _record_response_citation_degradation(plan, "$.choices[0].message")
    for field_name in ("audio", "function_call"):
        if message.get(field_name) is not None:
            raise ProtocolTransformError(
                f"OpenAI Chat message field {field_name!r} is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
    content: list[dict] = []
    reasoning = message.get("reasoning_content", _MISSING)
    if isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
        if plan is not None:
            plan.add(
                SupportDisposition.DEGRADED,
                "HUB_DEGRADE_UNSIGNED_THINKING",
                "$.choices[0].message.reasoning_content",
                "unsigned_thinking",
            )
    elif reasoning is not _MISSING and reasoning is not None and reasoning != "":
        raise ProtocolTransformError(
            "OpenAI Chat reasoning content must be text",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )
    text = message.get("content", None)
    refused = False
    if isinstance(text, str):
        if text:
            content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part_index, part in enumerate(text):
            if not isinstance(part, dict):
                raise ProtocolTransformError(
                    "OpenAI Chat content part must be an object",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            part_path = f"$.choices[0].message.content[{part_index}]"
            _require_upstream_field_allowlist(
                part,
                {"type", "text", "refusal", "annotations", "citations"},
                path=part_path,
                label="OpenAI Chat content part",
            )
            part_type = part.get("type")
            if part_type not in {None, "text", "output_text", "refusal"}:
                raise ProtocolTransformError(
                    f"OpenAI Chat content part {part_type!r} is unsupported",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            if "annotations" in part or "citations" in part:
                _record_response_citation_degradation(
                    plan,
                    part_path,
                )
            part_text = part.get("text")
            refusal_value = part.get("refusal")
            if part_text is not None and not isinstance(part_text, str):
                raise ProtocolTransformError(
                    "OpenAI Chat content-part text must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            if refusal_value is not None and not isinstance(refusal_value, str):
                raise ProtocolTransformError(
                    "OpenAI Chat refusal content must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            if part_text and refusal_value:
                raise ProtocolTransformError(
                    "OpenAI Chat content part has conflicting text carriers",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=part_path,
                )
            if part_type == "refusal":
                refused = True
            if isinstance(refusal_value, str) and refusal_value:
                refused = True
            value = part_text or refusal_value
            if isinstance(value, str) and value:
                content.append({"type": "text", "text": value})
    elif text is not None:
        raise ProtocolTransformError(
            "OpenAI Chat message content must be text, null, or content parts",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )
    refusal = message.get("refusal", _MISSING)
    if isinstance(refusal, str) and refusal:
        refused = True
        content.append({"type": "text", "text": refusal})
    elif refusal is not _MISSING and refusal is not None and refusal != "":
        raise ProtocolTransformError(
            "OpenAI Chat refusal must be text",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )
    has_tool = False
    seen_call_ids: set[str] = set()
    raw_tool_calls = message.get("tool_calls", [])
    if raw_tool_calls is None:
        raw_tool_calls = []
    if not isinstance(raw_tool_calls, list):
        raise ProtocolTransformError(
            "OpenAI Chat tool_calls must be an array",
            code="HUB_UPSTREAM_TOOL_CALL_INVALID",
        )
    for call_index, call in enumerate(raw_tool_calls):
        call_path = f"$.choices[0].message.tool_calls[{call_index}]"
        if not isinstance(call, dict):
            raise ProtocolTransformError(
                "OpenAI Chat tool calls must be objects",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
            )
        _require_upstream_field_allowlist(
            call,
            {"id", "type", "index", "function"},
            path=call_path,
            label="OpenAI Chat tool call",
        )
        if "type" in call and call["type"] != "function":
            raise ProtocolTransformError(
                "OpenAI Chat tool call type must be function",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path=f"{call_path}.type",
            )
        if "index" in call:
            call_position = call["index"]
            if (
                not isinstance(call_position, int)
                or isinstance(call_position, bool)
                or call_position < 0
            ):
                raise ProtocolTransformError(
                    "OpenAI Chat tool call index must be a non-negative integer",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                    path=f"{call_path}.index",
                )
            if call_position != call_index:
                raise ProtocolTransformError(
                    "OpenAI Chat completed tool call index conflicts with array order",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                    path=f"{call_path}.index",
                )
        function = call.get("function")
        if not isinstance(function, dict):
            raise ProtocolTransformError(
                "OpenAI Chat tool calls require a function object",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
            )
        _require_upstream_field_allowlist(
            function,
            {"name", "arguments"},
            path=f"{call_path}.function",
            label="OpenAI Chat tool call function",
        )
        call_id = call.get("id")
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id:
            raise ProtocolTransformError(
                "OpenAI Chat tool calls require a non-empty id",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
            )
        if call_id in seen_call_ids:
            raise ProtocolTransformError(
                "OpenAI Chat tool call ids must be unique",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                path=f"{call_path}.id",
            )
        seen_call_ids.add(call_id)
        if not isinstance(name, str) or not name:
            raise ProtocolTransformError(
                "OpenAI Chat tool calls require a non-empty function name",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
            )
        arguments = function.get("arguments", _MISSING)
        if not isinstance(arguments, str):
            raise ProtocolTransformError(
                "OpenAI Chat completed tool calls require string arguments",
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                path=f"{call_path}.function.arguments",
            )
        parsed = _parse_upstream_tool_arguments(arguments)
        content.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": parsed,
            }
        )
        has_tool = True
    raw_usage, _base_input, _base_output = _response_base_usage(
        body,
        api_format="openai_chat",
        input_key="prompt_tokens",
        output_key="completion_tokens",
        plan=plan,
    )
    receipt = UsageReceipt.from_upstream(
        raw_usage,
        input_key="prompt_tokens",
        output_key="completion_tokens",
    )
    cache_creation_detail = _cache_creation_detail(raw_usage)
    _validate_cache_creation_consistency(
        receipt.cache_write, cache_creation_detail
    )
    if "model" not in body:
        _record_response_metadata_degradation(plan, "$.model")
    payload = {
        "id": body.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", ""),
        "content": content,
        "stop_reason": _stop_reason(
            finish_reason,
            has_tool=has_tool,
            refused=refused,
        ),
        "stop_sequence": None,
        "usage": _usage_with_details(
            receipt,
            cache_creation_detail,
            _server_tool_usage(raw_usage),
        ),
    }
    return payload, receipt


def responses_to_anthropic(
    body: dict,
    *,
    plan: ConversionPlan | None = None,
) -> tuple[dict, UsageReceipt]:
    _require_upstream_response_allowlist(
        body,
        _RESPONSES_STREAM_RESPONSE_FIELDS,
        path="$",
        label="OpenAI Responses response",
    )
    for field_name in ("id", "model"):
        if field_name in body and (
            not isinstance(body[field_name], str) or not body[field_name]
        ):
            raise ProtocolTransformError(
                f"OpenAI Responses response {field_name} must be non-empty text",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path=f"$.{field_name}",
            )
    if "error" in body and body["error"] is not None:
        if not isinstance(body["error"], dict):
            message = "OpenAI Responses error must be an object or null"
        else:
            message = "OpenAI Responses error cannot be returned as a successful message"
        raise ProtocolTransformError(
            message,
            code="HUB_UPSTREAM_RESPONSE_INVALID",
            path="$.error",
        )
    consumed_fields = {
        "id",
        "model",
        "status",
        "output",
        "usage",
        "incomplete_details",
        "error",
    }
    for field_name in sorted(set(body) - consumed_fields):
        _record_response_metadata_degradation(plan, f"$.{field_name}")

    content: list[dict] = []
    has_tool = False
    refused = False
    output = body.get("output")
    if not isinstance(output, list):
        raise ProtocolTransformError(
            "OpenAI Responses output must be an array",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
        )
    seen_call_ids: set[str] = set()
    for item_index, item in enumerate(output):
        if not isinstance(item, dict):
            raise ProtocolTransformError(
                "OpenAI Responses output items must be objects",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
            )
        kind = item.get("type")
        item_path = f"$.output[{item_index}]"
        if kind == "message":
            _require_upstream_field_allowlist(
                item,
                {"type", "id", "role", "content", "status"},
                path=item_path,
                label="Responses message item",
            )
            _validate_response_item_id(
                item,
                path=item_path,
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                plan=plan,
            )
            if "role" in item and item["role"] != "assistant":
                raise ProtocolTransformError(
                    "Responses output message role must be assistant",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.role",
                )
            if "status" in item and item["status"] != "completed":
                raise ProtocolTransformError(
                    "Responses completed message item must have completed status",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.status",
                )
            message_content = item.get("content", _MISSING)
            if not isinstance(message_content, list):
                raise ProtocolTransformError(
                    "Responses output message content must be an array",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.content",
                )
            for part_index, part in enumerate(message_content):
                if not isinstance(part, dict):
                    raise ProtocolTransformError(
                        "Responses message content part must be an object",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    )
                part_type = part.get("type")
                if part_type not in {"output_text", "refusal"}:
                    raise ProtocolTransformError(
                        f"Responses content part {part_type!r} is unsupported",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    )
                part_path = f"{item_path}.content[{part_index}]"
                allowed_fields = (
                    {"type", "text", "annotations", "citations"}
                    if part_type == "output_text"
                    else {"type", "refusal"}
                )
                _require_upstream_field_allowlist(
                    part,
                    allowed_fields,
                    path=part_path,
                    label="Responses message content part",
                )
                if part_type == "output_text" and (
                    "annotations" in part or "citations" in part
                ):
                    _record_response_citation_degradation(
                        plan,
                        part_path,
                    )
                text_field = "text" if part_type == "output_text" else "refusal"
                text = part.get(text_field, _MISSING)
                if not isinstance(text, str):
                    raise ProtocolTransformError(
                        f"Responses {part_type} {text_field} must be a string",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=f"{part_path}.{text_field}",
                    )
                if part_type == "refusal":
                    refused = True
                if text:
                    content.append({"type": "text", "text": text})
        elif kind == "function_call":
            _require_upstream_field_allowlist(
                item,
                {
                    "type",
                    "id",
                    "call_id",
                    "name",
                    "arguments",
                    "input",
                    "status",
                },
                path=item_path,
                label="Responses function_call item",
            )
            if "call_id" in item and (
                not isinstance(item["call_id"], str) or not item["call_id"]
            ):
                raise ProtocolTransformError(
                    "Responses function calls require a non-empty call id",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                    path=f"{item_path}.call_id",
                )
            _validate_response_item_id(
                item,
                path=item_path,
                code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                plan=plan,
                record_metadata="call_id" in item,
            )
            if "arguments" in item and "input" in item:
                raise ProtocolTransformError(
                    "Responses function_call cannot contain both arguments and input",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.input",
                )
            if "status" in item and item["status"] != "completed":
                raise ProtocolTransformError(
                    "Responses completed function call must have completed status",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.status",
                )
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise ProtocolTransformError(
                    "Responses function calls require a non-empty call id",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                )
            if call_id in seen_call_ids:
                raise ProtocolTransformError(
                    "Responses function call ids must be unique",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                    path=f"{item_path}.call_id",
                )
            seen_call_ids.add(call_id)
            if not isinstance(name, str) or not name:
                raise ProtocolTransformError(
                    "Responses function calls require a non-empty name",
                    code="HUB_UPSTREAM_TOOL_CALL_INVALID",
                )
            arguments = item.get("arguments", item.get("input", _MISSING))
            if arguments is _MISSING:
                raise ProtocolTransformError(
                    "Responses completed function call requires arguments or input",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.arguments",
                )
            parsed = _parse_upstream_tool_arguments(arguments)
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": parsed,
                }
            )
            has_tool = True
        elif kind == "reasoning":
            _require_upstream_field_allowlist(
                item,
                {
                    "type",
                    "id",
                    "summary",
                    "encrypted_content",
                    "content",
                    "status",
                },
                path=item_path,
                label="Responses reasoning item",
            )
            _validate_response_item_id(
                item,
                path=item_path,
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                plan=plan,
            )
            if "status" in item and item["status"] != "completed":
                raise ProtocolTransformError(
                    "Responses completed reasoning item must have completed status",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.status",
                )
            reasoning_content = item.get("content", _MISSING)
            if reasoning_content is not _MISSING and reasoning_content not in (
                None,
                [],
            ):
                raise ProtocolTransformError(
                    "Responses reasoning content is unsupported",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.content",
                )
            encrypted = item.get("encrypted_content", _MISSING)
            if encrypted is not _MISSING and encrypted is not None and not isinstance(
                encrypted, str
            ):
                raise ProtocolTransformError(
                    "Responses reasoning encrypted_content must be a string or null",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{item_path}.encrypted_content",
                )
            if isinstance(encrypted, str) and encrypted:
                content.append(
                    {
                        "type": "redacted_thinking",
                        "data": _tag_responses_reasoning(encrypted),
                    }
                )
            summary = item.get("summary")
            if summary is not None and not isinstance(summary, list):
                raise ProtocolTransformError(
                    "Responses reasoning summary must be an array",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            if isinstance(summary, list):
                texts: list[str] = []
                for summary_index, part in enumerate(summary):
                    summary_path = f"{item_path}.summary[{summary_index}]"
                    if (
                        not isinstance(part, dict)
                        or part.get("type") not in {None, "summary_text"}
                        or not isinstance(part.get("text"), str)
                    ):
                        raise ProtocolTransformError(
                            "Responses reasoning summary part is unsupported",
                            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        )
                    _require_upstream_field_allowlist(
                        part,
                        {"type", "text"},
                        path=summary_path,
                        label="Responses reasoning summary part",
                    )
                    texts.append(part["text"])
                text = "\n".join(value for value in texts if value)
                if text:
                    content.append({"type": "thinking", "thinking": text})
                    if plan is not None:
                        plan.add(
                            SupportDisposition.DEGRADED,
                            "HUB_DEGRADE_UNSIGNED_THINKING",
                            f"$.output[{item_index}].summary",
                            "unsigned_thinking",
                        )
        else:
            raise ProtocolTransformError(
                f"Responses output item {kind!r} is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
    raw_usage, _base_input, _base_output = _response_base_usage(
        body,
        api_format="openai_responses",
        input_key="input_tokens",
        output_key="output_tokens",
        plan=plan,
    )
    receipt = UsageReceipt.from_upstream(
        raw_usage,
        input_key="input_tokens",
        output_key="output_tokens",
    )
    cache_creation_detail = _cache_creation_detail(raw_usage)
    _validate_cache_creation_consistency(
        receipt.cache_write, cache_creation_detail
    )
    if "model" not in body:
        _record_response_metadata_degradation(plan, "$.model")
    payload = {
        "id": body.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", ""),
        "content": content,
        "stop_reason": _responses_stop_reason(
            body,
            has_tool=has_tool,
            refused=refused,
        ),
        "stop_sequence": None,
        "usage": _usage_with_details(
            receipt,
            cache_creation_detail,
            _server_tool_usage(raw_usage),
        ),
    }
    return payload, receipt


class ResponseAdapter:
    api_format: str

    def decode(self, output: OutputIR, plan: ConversionPlan) -> PreparedResponse:
        raise NotImplementedError


class ChatResponseAdapter(ResponseAdapter):
    api_format = "openai_chat"

    def decode(self, output: OutputIR, plan: ConversionPlan) -> PreparedResponse:
        payload, receipt = chat_to_anthropic(output.source, plan=plan)
        return PreparedResponse(payload, plan, receipt)


class ResponsesResponseAdapter(ResponseAdapter):
    api_format = "openai_responses"

    def decode(self, output: OutputIR, plan: ConversionPlan) -> PreparedResponse:
        payload, receipt = responses_to_anthropic(output.source, plan=plan)
        return PreparedResponse(payload, plan, receipt)


RESPONSE_ADAPTERS: dict[str, ResponseAdapter] = {
    "openai_chat": ChatResponseAdapter(),
    "openai_responses": ResponsesResponseAdapter(),
}


def prepare_response(body: dict, api_format: str) -> PreparedResponse:
    if not isinstance(body, dict):
        raise ProtocolTransformError(
            "upstream response must be an object",
            code="HUB_UPSTREAM_RESPONSE_INVALID",
        )
    profile = CAPABILITY_PROFILES.get(api_format)
    if profile is None:
        raise ProtocolTransformError(
            f"unsupported protocol adapter {api_format!r}",
            code="HUB_API_FORMAT_UNSUPPORTED",
        )
    if profile.availability != "available":
        raise ProtocolTransformError(
            f"protocol adapter {api_format!r} is not available",
            code="HUB_ADAPTER_UNAVAILABLE",
        )
    plan = ConversionPlan(adapter=profile.name)
    if api_format == "anthropic":
        # Native pass-through stays byte-for-byte: the body is never
        # re-interpreted here, so there is no receipt to account from.
        return PreparedResponse(copy.deepcopy(body), plan)
    adapter = RESPONSE_ADAPTERS.get(api_format)
    if adapter is None:
        raise ProtocolTransformError(
            f"no response adapter is registered for {api_format!r}",
            code="HUB_ADAPTER_UNAVAILABLE",
        )
    return adapter.decode(OutputIR(copy.deepcopy(body), api_format), plan)


def transform_response(body: dict, api_format: str) -> dict:
    return prepare_response(body, api_format).payload


def upstream_error_evidence(body: object) -> tuple[str | None, str | None]:
    """Extract sanitized (code, message) evidence from an upstream error body.

    The same rules serve both the downstream Anthropic error shell and the
    hub's logging/response headers, so no caller re-parses raw upstream
    bodies. Some OpenAI-compatible gateways JSON-encode their JSON error
    object a second time; decode that wrapper once so a structured error does
    not collapse to an opaque status-only fallback. Numeric codes (common for
    OpenAI-compatible providers) are accepted alongside strings; anything that
    is not a short identifier becomes None rather than being forwarded.
    """
    if isinstance(body, str):
        try:
            decoded_body = json.loads(body)
        except json.JSONDecodeError:
            decoded_body = None
        if isinstance(decoded_body, dict):
            body = decoded_body
    error = body.get("error") if isinstance(body, dict) else None
    source_code = error.get("code") if isinstance(error, dict) else None
    source_message = error.get("message") if isinstance(error, dict) else None
    if isinstance(source_message, str):
        try:
            nested_body = json.loads(source_message)
        except json.JSONDecodeError:
            nested_body = None
        nested_error = (
            nested_body.get("error") if isinstance(nested_body, dict) else None
        )
        if isinstance(nested_error, dict):
            nested_code = nested_error.get("code")
            nested_message = nested_error.get("message")
            if nested_code not in (None, ""):
                source_code = nested_code
            if isinstance(nested_message, str) and nested_message.strip():
                source_message = nested_message
    if isinstance(source_code, bool) or not isinstance(source_code, (str, int)):
        source_code = None
    safe_code = (
        str(source_code)
        if source_code is not None
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(source_code))
        else None
    )
    safe_message = None
    if isinstance(source_message, str) and source_message.strip():
        candidate = source_message.strip()[:512]
        candidate = re.sub(
            r"(?i)\bBearer\s+\S+",
            "Bearer [redacted-token]",
            candidate,
        )
        candidate = re.sub(r"(?i)https?://\S+", "[redacted-url]", candidate)
        candidate = re.sub(r"[\x00-\x1f\x7f]+", " ", candidate).strip()
        if candidate:
            safe_message = candidate
    return safe_code, safe_message


def transform_error(body: object, status: int) -> dict:
    safe_code, safe_message = upstream_error_evidence(body)
    message = f"upstream HTTP {status}"
    if safe_code:
        message += f" ({safe_code})"
    if safe_message:
        message += f": {safe_message}"
    if status in {401, 403}:
        kind = "authentication_error" if status == 401 else "permission_error"
    elif status == 429:
        kind = "rate_limit_error"
    elif status == 404:
        kind = "not_found_error"
    elif status in {408, 504}:
        kind = "timeout_error"
    elif status == 529:
        kind = "overloaded_error"
    elif status >= 500:
        kind = "api_error"
    else:
        kind = "invalid_request_error"
    return {"type": "error", "error": {"type": kind, "message": str(message)}}


def sse_event(event: str, payload: dict) -> bytes:
    return (
        f"event: {event}\ndata: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


# Two consecutive line terminators (CRLF/LF/CR) end an SSE event.  The bare
# CR alternative must not eat the CR of a CRLF pair, so it refuses a
# following LF instead of relying on backtracking.
_SSE_BOUNDARY = re.compile(br"(?:\r\n|\r(?!\n)|\n)(?:\r\n|\r(?!\n)|\n)")
_SSE_BOM = b"\xef\xbb\xbf"


class SSEParser:
    """Incrementally parse upstream SSE without assuming chunk boundaries."""

    def __init__(self, *, max_buffer: int = 2 * 1024 * 1024):
        self.buffer = bytearray()
        self.max_buffer = max_buffer
        # Resume scanning where the previous feed stopped, so parsing stays
        # O(n) for pathologically small chunks.
        self._scan_offset = 0
        self._at_stream_start = True

    def feed(self, chunk: bytes) -> list[tuple[str, str]]:
        if self._at_stream_start:
            self._at_stream_start = False
            # A leading UTF-8 BOM is framing noise, not event content.
            if chunk.startswith(_SSE_BOM):
                chunk = chunk[len(_SSE_BOM) :]
        self.buffer.extend(chunk)
        events: list[tuple[str, str]] = []
        while True:
            if self._scan_offset > self.max_buffer:
                raise ProtocolTransformError(
                    "upstream SSE event exceeds size limit",
                    code="HUB_SSE_EVENT_TOO_LARGE",
                )
            match = _SSE_BOUNDARY.search(self.buffer, self._scan_offset)
            if match is None:
                # A boundary may straddle the tail, so the next feed resumes
                # three bytes back instead of rescanning the whole buffer.
                self._scan_offset = max(0, len(self.buffer) - 3)
                break
            if match.start() > self.max_buffer:
                raise ProtocolTransformError(
                    "upstream SSE event exceeds size limit",
                    code="HUB_SSE_EVENT_TOO_LARGE",
                )
            raw = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            self._scan_offset = 0
            event = "message"
            data: list[str] = []
            try:
                for line in re.split(br"\r\n|\n|\r", raw):
                    if line.startswith(b"event:"):
                        value = line[6:]
                        if value.startswith(b" "):
                            value = value[1:]
                        event = value.decode("utf-8", "strict")
                    elif line.startswith(b"data:"):
                        value = line[5:]
                        if value.startswith(b" "):
                            value = value[1:]
                        data.append(value.decode("utf-8", "strict"))
                    elif line == b"data":
                        data.append("")
            except UnicodeDecodeError as exc:
                raise ProtocolTransformError(
                    "upstream SSE event is not valid UTF-8",
                    code="HUB_SSE_UTF8_INVALID",
                ) from exc
            if data:
                events.append((event, "\n".join(data)))
        if len(self.buffer) > self.max_buffer:
            raise ProtocolTransformError(
                "upstream SSE event exceeds size limit",
                code="HUB_SSE_EVENT_TOO_LARGE",
            )
        return events

    def finish(self) -> None:
        if self.buffer.strip():
            raise ProtocolTransformError(
                "upstream SSE ended with an incomplete event",
                code="HUB_SSE_INCOMPLETE_EVENT",
            )


class StreamPhase(str, Enum):
    INIT = "init"
    ACTIVE = "active"
    SUCCESS_TERMINAL = "success_terminal"
    ERROR_TERMINAL = "error_terminal"
    CLOSED = "closed"


_STREAM_DELTA_TYPES = {
    "text": {"text_delta", "citations_delta"},
    "thinking": {"thinking_delta", "signature_delta"},
    "tool_use": {"input_json_delta"},
}

MAX_STREAM_TOOL_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_STREAM_TEXT_BYTES = 2 * 1024 * 1024
MAX_STREAM_TEXT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_STREAM_BLOCKS = 4096
MAX_STREAM_ITEM_ID_CHARS = 1024

_RESPONSES_SSE_EVENT_FIELDS = {
    "response.output_text.delta": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "delta", "logprobs", "obfuscation",
    },
    "response.refusal.delta": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "delta", "obfuscation",
    },
    "response.output_text.done": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "text", "logprobs",
    },
    "response.refusal.done": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "refusal",
    },
    "response.reasoning_summary_text.delta": {
        "type", "sequence_number", "item_id", "output_index", "summary_index",
        "delta", "obfuscation",
    },
    "response.reasoning_summary_text.done": {
        "type", "sequence_number", "item_id", "output_index", "summary_index",
        "text",
    },
    "response.content_part.added": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "part",
    },
    "response.content_part.done": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "part",
    },
    "response.reasoning_summary_part.added": {
        "type", "sequence_number", "item_id", "output_index", "summary_index",
        "part",
    },
    "response.reasoning_summary_part.done": {
        "type", "sequence_number", "item_id", "output_index", "summary_index",
        "part",
    },
    "response.output_text.annotation.added": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "annotation_index", "annotation",
    },
    "response.output_text.annotation.delta": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "annotation_index", "annotation", "delta",
    },
    "response.output_text.citation.added": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "citation_index", "citation",
    },
    "response.output_text.citation.delta": {
        "type", "sequence_number", "item_id", "output_index", "content_index",
        "citation_index", "citation", "delta",
    },
    "response.output_item.added": {
        "type", "sequence_number", "output_index", "item",
    },
    "response.output_item.done": {
        "type", "sequence_number", "output_index", "item",
    },
    "response.function_call_arguments.delta": {
        "type", "sequence_number", "item_id", "output_index", "call_id", "delta",
        "obfuscation",
    },
    "response.function_call_arguments.done": {
        "type", "sequence_number", "item_id", "output_index", "call_id",
        "arguments",
    },
    "response.completed": {"type", "sequence_number", "response"},
    "response.incomplete": {"type", "sequence_number", "response"},
    "response.created": {"type", "sequence_number", "response"},
    "response.in_progress": {"type", "sequence_number", "response"},
    "response.queued": {"type", "sequence_number", "response"},
    "response.failed": {"type", "sequence_number", "response", "error"},
    "error": {"type", "sequence_number", "error"},
}

_RESPONSES_STREAM_RESPONSE_FIELDS = {
    "id",
    "object",
    "created_at",
    "status",
    "background",
    "error",
    "incomplete_details",
    "instructions",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "model",
    "output",
    "parallel_tool_calls",
    "previous_response_id",
    "prompt",
    "prompt_cache_key",
    "prompt_cache_retention",
    "reasoning",
    "safety_identifier",
    "service_tier",
    "store",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_logprobs",
    "top_p",
    "truncation",
    "usage",
    "user",
}


@dataclass
class StreamBlockState:
    index: int
    kind: str
    source_key: str | None
    open: bool = True
    delta_count: int = 0


@dataclass
class ResponseStreamPartState:
    item_id: str
    part_type: str
    added_snapshot: str
    open: bool = True
    done_snapshot: str | None = None


@dataclass
class StreamStateMachine:
    """Protocol-neutral lifecycle validator used by every stream adapter."""

    phase: StreamPhase = StreamPhase.INIT
    message_started: bool = False
    next_index: int = 0
    blocks: dict[int, StreamBlockState] = field(default_factory=dict)
    keys: dict[str, int] = field(default_factory=dict)

    def start_message(self) -> None:
        if self.message_started:
            return
        if self.phase not in {
            StreamPhase.INIT,
            StreamPhase.ACTIVE,
            StreamPhase.SUCCESS_TERMINAL,
        }:
            raise ProtocolTransformError(
                "message_start is not valid in the current stream phase",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        self.message_started = True
        if self.phase is StreamPhase.INIT:
            self.phase = StreamPhase.ACTIVE

    def open_block(self, kind: str, source_key: str | None) -> StreamBlockState:
        if self.phase is StreamPhase.INIT:
            self.start_message()
        if self.phase is not StreamPhase.ACTIVE:
            raise ProtocolTransformError(
                "content block opened outside the active stream phase",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if source_key is not None and source_key in self.keys:
            existing = self.blocks[self.keys[source_key]]
            if existing.open and existing.kind == kind:
                return existing
            raise ProtocolTransformError(
                "content block source key was reused",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if self.next_index >= MAX_STREAM_BLOCKS:
            raise ProtocolTransformError(
                "stream opened too many content blocks",
                code="HUB_SSE_TOO_MANY_BLOCKS",
            )
        state = StreamBlockState(self.next_index, kind, source_key)
        self.next_index += 1
        self.blocks[state.index] = state
        if source_key is not None:
            self.keys[source_key] = state.index
        return state

    def add_delta(self, index: int, delta_type: str) -> None:
        state = self.blocks.get(index)
        if state is None or not state.open:
            raise ProtocolTransformError(
                "content delta targeted a block that is not open",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if delta_type not in _STREAM_DELTA_TYPES.get(state.kind, set()):
            raise ProtocolTransformError(
                f"delta {delta_type!r} is invalid for block {state.kind!r}",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        state.delta_count += 1

    def close_block(self, index: int) -> None:
        state = self.blocks.get(index)
        if state is None or not state.open:
            raise ProtocolTransformError(
                "content block was stopped without one matching open block",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        state.open = False

    def mark_success(self) -> None:
        if self.phase not in {StreamPhase.INIT, StreamPhase.ACTIVE}:
            raise ProtocolTransformError(
                "stream received a duplicate or conflicting terminal",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        self.phase = StreamPhase.SUCCESS_TERMINAL

    def mark_error(self) -> None:
        if self.phase not in {StreamPhase.INIT, StreamPhase.ACTIVE}:
            raise ProtocolTransformError(
                "stream received a duplicate or conflicting terminal",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        self.phase = StreamPhase.ERROR_TERMINAL
        for block in self.blocks.values():
            block.open = False

    def close_stream(self) -> None:
        if self.phase not in {
            StreamPhase.SUCCESS_TERMINAL,
            StreamPhase.ERROR_TERMINAL,
        }:
            raise ProtocolTransformError(
                "stream closed without a terminal transition",
                code="HUB_SSE_MISSING_TERMINAL",
            )
        if any(block.open for block in self.blocks.values()):
            raise ProtocolTransformError(
                "stream closed while a content block remained open",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        self.phase = StreamPhase.CLOSED


@dataclass
class AnthropicStreamBridge:
    api_format: str
    compatibility_mode: str = "visible_lossy"
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    model: str = ""
    upstream_message_id: str | None = None
    upstream_model: str | None = None
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
    response_text_fragments: dict[tuple[str, str, str], list[str]] = field(
        default_factory=dict
    )
    response_text_snapshots: dict[tuple[str, str, str], str] = field(
        default_factory=dict
    )
    response_text_bytes: dict[tuple[str, str, str], int] = field(
        default_factory=dict
    )
    response_content_parts: dict[tuple[str, str], ResponseStreamPartState] = field(
        default_factory=dict
    )
    response_reasoning_fragments: dict[tuple[str, str], list[str]] = field(
        default_factory=dict
    )
    response_reasoning_snapshots: dict[tuple[str, str], str] = field(
        default_factory=dict
    )
    response_reasoning_bytes: dict[tuple[str, str], int] = field(
        default_factory=dict
    )
    response_reasoning_done: set[tuple[str, str]] = field(default_factory=set)
    response_redacted_snapshots: dict[str, str] = field(default_factory=dict)
    response_output_items: dict[str, tuple[str, str | None]] = field(
        default_factory=dict
    )
    response_output_item_ids: dict[str, str] = field(default_factory=dict)
    response_summary_parts: dict[tuple[str, str], ResponseStreamPartState] = field(
        default_factory=dict
    )
    response_text_total_bytes: int = 0
    response_tool_done_snapshots: dict[int, str] = field(default_factory=dict)
    nested_cache_evidence: bool = False
    official_cache_read: bool = False
    tool_argument_fragments: dict[int, list[str]] = field(default_factory=dict)
    tool_argument_bytes: dict[int, int] = field(default_factory=dict)
    tool_descriptors: dict[str, tuple[str, str]] = field(default_factory=dict)
    open_indices: set[int] = field(default_factory=set)
    has_tool: bool = False
    refused: bool = False
    stop: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    saw_input_usage: bool = False
    saw_output_usage: bool = False
    late_input_usage: bool = False
    cache_read: int | None = None
    cache_write: int | None = None
    cache_creation_detail: dict[str, int] | None = None
    server_tool_usage_detail: dict[str, int] | None = None
    thinking_signature: str | None = None
    thinking_has_delta: bool = False
    observations: list[str] = field(default_factory=list)
    fsm: StreamStateMachine = field(default_factory=StreamStateMachine)

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.observations))

    def _observe(self, code: str) -> None:
        if code not in self.observations:
            self.observations.append(code)

    def _update_stream_identity(
        self,
        field_name: str,
        value: str,
        *,
        path: str,
    ) -> None:
        observed_attribute = (
            "upstream_message_id" if field_name == "id" else "upstream_model"
        )
        output_attribute = "message_id" if field_name == "id" else "model"
        observed = getattr(self, observed_attribute)
        if observed is not None and observed != value:
            raise ProtocolTransformError(
                f"upstream stream {field_name} changed between events",
                code="HUB_SSE_DUPLICATE_CONFLICT",
                path=path,
            )
        if observed is None:
            emitted_value = getattr(self, output_attribute)
            if self.started and emitted_value != value:
                raise ProtocolTransformError(
                    f"upstream stream {field_name} arrived after message_start",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path=path,
                )
            setattr(self, observed_attribute, value)
            setattr(self, output_attribute, value)

    def _observe_stream_degradation(self, code: str, message: str) -> None:
        if self.compatibility_mode == "strict":
            raise ProtocolTransformError(
                message,
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        self._observe(code)

    def _validate_responses_event_envelope(
        self,
        kind: str,
        payload: dict,
    ) -> None:
        allowed_fields = _RESPONSES_SSE_EVENT_FIELDS.get(kind)
        if allowed_fields is None:
            return
        if set(payload) - allowed_fields:
            raise ProtocolTransformError(
                f"Responses SSE event {kind!r} contains unsupported fields",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        if "sequence_number" in payload:
            if self._response_stream_index(payload["sequence_number"]) is None:
                raise ProtocolTransformError(
                    "Responses SSE sequence_number must be non-negative",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.sequence_number",
                )
            self._observe_stream_degradation(
                "HUB_DEGRADE_STREAM_SEQUENCE_METADATA_DROPPED",
                "Responses stream sequence metadata has no Anthropic event carrier",
            )
        if "logprobs" in payload or "obfuscation" in payload:
            self._observe_stream_degradation(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                "Responses stream metadata has no Anthropic event carrier",
            )

    def _update_counter(self, attribute: str, value: object) -> None:
        if value is _MISSING:
            return
        parsed = _token_count(value, -1)
        if parsed < 0:
            raise ProtocolTransformError(
                f"upstream usage counter {attribute} is invalid",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        current = getattr(self, attribute)
        if current is not None and parsed < current:
            raise ProtocolTransformError(
                f"upstream usage counter {attribute} regressed",
                code="HUB_SSE_USAGE_REGRESSION",
            )
        setattr(self, attribute, parsed)

    def _update_base_usage(self, attribute: str, value: object) -> None:
        flag = f"saw_{attribute.removesuffix('_tokens')}_usage"
        first_observation = not getattr(self, flag)
        self._update_counter(attribute, value)
        if first_observation:
            if attribute == "input_tokens" and self.started:
                self._observe("HUB_DEGRADE_LATE_INPUT_USAGE")
                self.late_input_usage = True
            setattr(self, flag, True)

    def _update_usage_detail(self, attribute: str, value: object) -> None:
        if value is _MISSING:
            return
        if not isinstance(value, dict):
            raise ProtocolTransformError(
                f"upstream usage detail {attribute} is invalid",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        current = getattr(self, attribute)
        if current is None:
            current = {}
        merged = dict(current)
        for key, counter in value.items():
            previous = merged.get(key)
            if previous is not None and counter < previous:
                raise ProtocolTransformError(
                    f"upstream usage detail {attribute}.{key} regressed",
                    code="HUB_SSE_USAGE_REGRESSION",
                )
            merged[key] = counter
        setattr(self, attribute, merged)

    def _consume_stream_usage(
        self,
        raw_usage: object,
        *,
        input_key: str,
        output_key: str,
    ) -> None:
        if raw_usage is _MISSING or raw_usage is None:
            return
        if not isinstance(raw_usage, dict):
            raise ProtocolTransformError(
                "upstream stream usage must be an object or null",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        degraded_paths = _validate_upstream_usage_fields(
            raw_usage,
            self.api_format,
        )
        if degraded_paths:
            self._observe_stream_degradation(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                "upstream usage detail metadata has no Anthropic event carrier: "
                + ", ".join(degraded_paths),
            )
        _upstream_usage_total(
            raw_usage,
            input_key=input_key,
            output_key=output_key,
        )
        if input_key in raw_usage:
            self._update_base_usage("input_tokens", raw_usage[input_key])
        if output_key in raw_usage:
            self._update_base_usage("output_tokens", raw_usage[output_key])
        # Cache-read evidence is observed per upstream usage event and
        # applied once at emission time via UsageReceipt.from_evidence, so
        # stream and complete share the same interpretation rule.
        if _has_nested_cache_carrier(raw_usage):
            self.nested_cache_evidence = True
        if _has_official_cache_read(raw_usage):
            self.official_cache_read = True
        self._update_counter("cache_read", _cache_read(raw_usage))
        self._update_counter("cache_write", _cache_write(raw_usage))
        self._update_usage_detail(
            "cache_creation_detail", _cache_creation_detail(raw_usage)
        )
        self._update_usage_detail(
            "server_tool_usage_detail", _server_tool_usage(raw_usage)
        )

    @staticmethod
    def _optional_stream_text(value: object, *, field_name: str) -> str | None:
        if value is _MISSING or value is None:
            return None
        if not isinstance(value, str):
            raise ProtocolTransformError(
                f"upstream stream field {field_name} must be a string or null",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path=f"$.{field_name}",
            )
        return value

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.fsm.start_message()
        self.started = True
        # Usage fields are only emitted when the upstream actually reported
        # them; a fabricated zero would hide the unobserved state.
        receipt = self.usage_receipt()
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
                        "usage": receipt.as_anthropic(),
                    },
                },
            )
        ]

    def usage_receipt(self) -> UsageReceipt:
        """Convert aggregated counters through the shared evidence rule."""
        return UsageReceipt.from_evidence(
            input_tokens=self.input_tokens if self.saw_input_usage else None,
            output_tokens=self.output_tokens if self.saw_output_usage else None,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            nested_cache_evidence=self.nested_cache_evidence,
            official_cache_read=self.official_cache_read,
        )

    def usage_for_accounting(self) -> dict:
        """Export the accounting view of this stream's usage.

        Accounting used to read the aggregated counters directly, which
        bypassed the cache-read evidence rule and let one stream report a
        subtracted base downstream while the ledger kept the inclusive one.
        Both views now derive from the same receipt, so an unobserved
        counter is omitted here instead of being fabricated as a zero.
        """
        usage = self.usage_receipt().as_anthropic()
        if self.cache_creation_detail is not None:
            usage["cache_creation"] = copy.deepcopy(self.cache_creation_detail)
        if self.server_tool_usage_detail is not None:
            usage["server_tool_use"] = copy.deepcopy(self.server_tool_usage_detail)
        return usage

    def _open(self, kind: str, block: dict, *, key: str | None = None) -> tuple[int, list[bytes]]:
        if key is not None and key in self.tool_indices:
            return self.tool_indices[key], []
        started = self._start()
        state = self.fsm.open_block(kind, key)
        index = state.index
        self.next_index = self.fsm.next_index
        if key is not None:
            self.tool_indices[key] = index
        self.open_indices.add(index)
        return index, [
            *started,
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
        if index == self.thinking_index and self.thinking_signature is None:
            if self.compatibility_mode == "strict":
                raise ProtocolTransformError(
                    "thinking block ended without a real upstream signature",
                    code="HUB_SSE_SIGNATURE_ORDER",
                )
            self._observe("HUB_DEGRADE_UNSIGNED_THINKING")
        if index in set(self.tool_indices.values()):
            fragments = self.tool_argument_fragments.get(index, [])
            try:
                arguments = json.loads("".join(fragments))
            except json.JSONDecodeError as exc:
                raise ProtocolTransformError(
                    "streamed tool arguments did not form valid JSON",
                    code="HUB_SSE_TOOL_ARGUMENTS_INVALID",
                ) from exc
            if not isinstance(arguments, dict):
                raise ProtocolTransformError(
                    "streamed tool arguments must form a JSON object",
                    code="HUB_SSE_TOOL_ARGUMENTS_INVALID",
                )
            self.tool_argument_fragments.pop(index, None)
            self.tool_argument_bytes.pop(index, None)
        self.fsm.close_block(index)
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
            # The FSM validates block type compatibility before emission.
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        self.fsm.add_delta(self.text_index, "text_delta")
        return chunks

    def _thinking(self, text: str) -> list[bytes]:
        chunks: list[bytes] = []
        if self.thinking_index is None:
            chunks.extend(self._close_open_tools())
            chunks.extend(self._close(self.text_index))
            self.text_index = None
            self.thinking_index, opened = self._open(
                "thinking", {"thinking": ""}
            )
            self.thinking_signature = None
            self.thinking_has_delta = False
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
        self.fsm.add_delta(self.thinking_index, "thinking_delta")
        self.thinking_has_delta = True
        return chunks

    def _signature(self, signature: str) -> list[bytes]:
        if (
            self.thinking_index is None
            or self.thinking_index not in self.open_indices
            or not self.thinking_has_delta
        ):
            raise ProtocolTransformError(
                "thinking signature arrived without an open thinking block",
                code="HUB_SSE_SIGNATURE_ORDER",
            )
        if self.thinking_signature is not None:
            if self.thinking_signature == signature:
                return []
            raise ProtocolTransformError(
                "thinking block received conflicting signatures",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        self.thinking_signature = signature
        self.fsm.add_delta(self.thinking_index, "signature_delta")
        return [
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.thinking_index,
                    "delta": {
                        "type": "signature_delta",
                        "signature": signature,
                    },
                },
            )
        ]

    def _redacted_thinking(self, opaque: str) -> list[bytes]:
        chunks = [
            *self._close(self.text_index),
            *self._close(self.thinking_index),
            *self._close_open_tools(),
        ]
        self.text_index = None
        self.thinking_index = None
        index, opened = self._open(
            "redacted_thinking",
            {"data": _tag_responses_reasoning(opaque)},
        )
        chunks.extend(opened)
        chunks.extend(self._close(index))
        return chunks

    def _tool_start(self, key: str, call_id: str, name: str) -> list[bytes]:
        existing = self.tool_descriptors.get(key)
        if existing is not None:
            existing_id, existing_name = existing
            if (call_id and call_id != existing_id) or (name and name != existing_name):
                raise ProtocolTransformError(
                    "streamed tool call identity changed for one source key",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            return []
        if not call_id or not name:
            raise ProtocolTransformError(
                "streamed tool calls require an id and name before arguments",
                code="HUB_SSE_TOOL_CALL_INVALID",
            )
        self.tool_descriptors[key] = (call_id, name)
        self.has_tool = True
        _, chunks = self._open(
            "tool_use",
            {"id": call_id, "name": name, "input": {}},
            key=key,
        )
        index = self.tool_indices[key]
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
            raise ProtocolTransformError(
                "tool argument delta targeted a block that is not open",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        fragment_bytes = len(value.encode("utf-8"))
        total_bytes = self.tool_argument_bytes.get(index, 0) + fragment_bytes
        if total_bytes > MAX_STREAM_TOOL_ARGUMENT_BYTES:
            raise ProtocolTransformError(
                "streamed tool arguments exceeded the aggregate byte limit",
                code="HUB_SSE_TOOL_ARGUMENTS_TOO_LARGE",
            )
        self.tool_argument_bytes[index] = total_bytes
        self.tool_argument_fragments.setdefault(index, []).append(value)
        self.fsm.add_delta(index, "input_json_delta")
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

    def _tool_snapshot(self, key: str, snapshot: str) -> list[bytes]:
        index = self.tool_indices.get(key)
        if index is None or index not in self.open_indices:
            raise ProtocolTransformError(
                "tool argument snapshot targeted a block that is not open",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        current = "".join(self.tool_argument_fragments.get(index, []))
        if current == snapshot:
            return []
        if snapshot.startswith(current):
            suffix = snapshot[len(current) :]
            return self._tool_delta(key, suffix) if suffix else []
        raise ProtocolTransformError(
            "tool argument snapshot conflicts with earlier deltas",
            code="HUB_SSE_DUPLICATE_CONFLICT",
        )

    @staticmethod
    def _response_stream_index(value: object) -> str | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return str(value)
        if isinstance(value, str) and value.isdecimal():
            return str(int(value))
        return None

    def _response_part_coordinates(
        self,
        payload: dict,
        index_field: str,
    ) -> tuple[str, tuple[str, str]]:
        item_id = payload.get("item_id")
        output_index = self._response_stream_index(payload.get("output_index"))
        part_index = self._response_stream_index(payload.get(index_field))
        if (
            not isinstance(item_id, str)
            or not item_id
            or output_index is None
            or part_index is None
        ):
            raise ProtocolTransformError(
                "Responses part event could not be matched to one output position",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        return item_id, (output_index, part_index)

    def _response_event_coordinates(
        self,
        payload: dict,
        index_field: str,
    ) -> tuple[str, str]:
        output_index = self._response_stream_index(payload.get("output_index"))
        part_index = self._response_stream_index(payload.get(index_field))
        if output_index is None or part_index is None:
            raise ProtocolTransformError(
                "Responses text event requires valid output and part coordinates",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        return output_index, part_index

    def _response_text_key_seen(self, key: tuple[str, str, str]) -> bool:
        return (
            key in self.response_text_deltas
            or key in self.response_text_done
            or key in self.response_text_fragments
            or key in self.response_text_snapshots
        )

    def _response_text_event_key(
        self,
        text_kind: str,
        payload: dict,
    ) -> tuple[str, str, str]:
        if not self.response_content_parts:
            return (
                text_kind,
                *self._response_event_coordinates(payload, "content_index"),
            )
        item_id, coordinates = self._response_part_coordinates(
            payload,
            "content_index",
        )
        state = self.response_content_parts.get(coordinates)
        if state is None:
            raise ProtocolTransformError(
                "Responses text event arrived without a matching content part",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        expected_type = (
            "refusal" if text_kind == "response.refusal" else "output_text"
        )
        if state.item_id != item_id or state.part_type != expected_type:
            raise ProtocolTransformError(
                "Responses text event conflicts with its content part identity",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if not state.open:
            raise ProtocolTransformError(
                "Responses text event arrived after its content part was done",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        return (text_kind, *coordinates)

    def _validate_response_citation_event(
        self,
        kind: str,
        payload: dict,
    ) -> None:
        metadata_field = "annotation" if ".annotation." in kind else "citation"
        metadata_value = payload.get(metadata_field, _MISSING)
        delta_value = payload.get("delta", _MISSING)
        if kind.endswith(".added"):
            if not isinstance(metadata_value, dict):
                raise ProtocolTransformError(
                    f"Responses {metadata_field} event requires an object",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"$.{metadata_field}",
                )
        else:
            if metadata_value is _MISSING and delta_value is _MISSING:
                raise ProtocolTransformError(
                    f"Responses {metadata_field} delta has no metadata carrier",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            for field_name, field_value in (
                (metadata_field, metadata_value),
                ("delta", delta_value),
            ):
                if field_value is not _MISSING and not isinstance(field_value, dict):
                    raise ProtocolTransformError(
                        f"Responses {field_name} metadata must be an object",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=f"$.{field_name}",
                    )
        if self.response_content_parts:
            item_id, coordinates = self._response_part_coordinates(
                payload,
                "content_index",
            )
            state = self.response_content_parts.get(coordinates)
            if state is None:
                raise ProtocolTransformError(
                    "Responses citation metadata has no matching content part",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if state.item_id != item_id or state.part_type != "output_text":
                raise ProtocolTransformError(
                    "Responses citation metadata conflicts with its text part identity",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            if not state.open:
                raise ProtocolTransformError(
                    "Responses citation metadata arrived after its text part was done",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
        else:
            coordinates = self._response_event_coordinates(
                payload,
                "content_index",
            )
            text_key = ("response.output_text", *coordinates)
            if not self._response_text_key_seen(text_key):
                raise ProtocolTransformError(
                    "Responses citation metadata has no matching output text",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if text_key in self.response_text_done:
                raise ProtocolTransformError(
                    "Responses citation metadata arrived after output text was done",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if "item_id" in payload:
                item_id = payload["item_id"]
                if not isinstance(item_id, str) or not item_id:
                    raise ProtocolTransformError(
                        "Responses citation item_id must be non-empty text",
                        code="HUB_SSE_ORDER_VIOLATION",
                        path="$.item_id",
                    )
                descriptor = self.response_output_items.get(coordinates[0])
                if (
                    descriptor is not None
                    and descriptor[1] is not None
                    and descriptor[1] != item_id
                ):
                    raise ProtocolTransformError(
                        "Responses citation item_id conflicts with its output item",
                        code="HUB_SSE_DUPLICATE_CONFLICT",
                        path="$.item_id",
                    )
        metadata_index_field = (
            "annotation_index" if ".annotation." in kind else "citation_index"
        )
        if metadata_index_field in payload and self._response_stream_index(
            payload[metadata_index_field]
        ) is None:
            raise ProtocolTransformError(
                f"Responses {metadata_index_field} must be non-negative",
                code="HUB_SSE_ORDER_VIOLATION",
                path=f"$.{metadata_index_field}",
            )
        if self.text_index is None or self.text_index not in self.open_indices:
            raise ProtocolTransformError(
                "citation metadata arrived without an open text block",
                code="HUB_SSE_ORDER_VIOLATION",
            )

    def _set_response_text_size(
        self,
        byte_map: dict,
        key: tuple,
        new_size: int,
    ) -> None:
        if (
            key not in byte_map
            and len(self.response_text_bytes) + len(self.response_reasoning_bytes)
            >= MAX_STREAM_BLOCKS
        ):
            raise ProtocolTransformError(
                "stream tracked too many response text parts",
                code="HUB_SSE_TOO_MANY_BLOCKS",
            )
        previous_size = byte_map.get(key, 0)
        new_total = self.response_text_total_bytes - previous_size + new_size
        if new_total > MAX_STREAM_TEXT_TOTAL_BYTES:
            raise ProtocolTransformError(
                "streamed response text exceeded the aggregate byte limit",
                code="HUB_SSE_TEXT_TOO_LARGE",
            )
        byte_map[key] = new_size
        self.response_text_total_bytes = new_total

    def _response_text_delta(
        self,
        key: tuple[str, str, str],
        delta: str,
    ) -> list[bytes]:
        if key in self.response_text_done:
            raise ProtocolTransformError(
                "Responses emitted text after its done snapshot",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        total_bytes = self.response_text_bytes.get(key, 0) + len(
            delta.encode("utf-8")
        )
        if total_bytes > MAX_STREAM_TEXT_BYTES:
            raise ProtocolTransformError(
                "streamed response text exceeded the per-part byte limit",
                code="HUB_SSE_TEXT_TOO_LARGE",
            )
        self._set_response_text_size(self.response_text_bytes, key, total_bytes)
        self.response_text_deltas.add(key)
        fragments = self.response_text_fragments.setdefault(key, [])
        if delta:
            fragments.append(delta)
        return self._text(delta) if delta else []

    def _response_text_snapshot(
        self,
        key: tuple[str, str, str],
        snapshot: str,
    ) -> list[bytes]:
        snapshot_bytes = len(snapshot.encode("utf-8"))
        if snapshot_bytes > MAX_STREAM_TEXT_BYTES:
            raise ProtocolTransformError(
                "streamed response text exceeded the per-part byte limit",
                code="HUB_SSE_TEXT_TOO_LARGE",
            )
        if key in self.response_text_done:
            if snapshot == self.response_text_snapshots.get(key):
                return []
            raise ProtocolTransformError(
                "Responses emitted conflicting text done snapshots",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        current = "".join(self.response_text_fragments.get(key, []))
        if current and not snapshot.startswith(current):
            raise ProtocolTransformError(
                "Responses text snapshot conflicts with earlier deltas",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        suffix = snapshot[len(current) :]
        chunks = self._response_text_delta(key, suffix) if suffix else []
        self.response_text_done.add(key)
        self.response_text_snapshots[key] = snapshot
        self._set_response_text_size(self.response_text_bytes, key, snapshot_bytes)
        self.response_text_deltas.discard(key)
        self.response_text_fragments.pop(key, None)
        return chunks

    def _response_reasoning_event_key(self, payload: dict) -> tuple[str, str]:
        if not self.response_summary_parts:
            return self._response_event_coordinates(payload, "summary_index")
        item_id, coordinates = self._response_part_coordinates(
            payload,
            "summary_index",
        )
        state = self.response_summary_parts.get(coordinates)
        if state is None:
            raise ProtocolTransformError(
                "Responses summary text arrived without a matching summary part",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if state.item_id != item_id or state.part_type != "summary_text":
            raise ProtocolTransformError(
                "Responses summary text conflicts with its summary part identity",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if not state.open:
            raise ProtocolTransformError(
                "Responses summary text arrived after its part was done",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        return coordinates

    def _response_reasoning_delta(
        self,
        key: tuple[str, str],
        delta: str,
    ) -> list[bytes]:
        if key in self.response_reasoning_done:
            raise ProtocolTransformError(
                "Responses emitted reasoning after its done snapshot",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        total_bytes = self.response_reasoning_bytes.get(key, 0) + len(
            delta.encode("utf-8")
        )
        if total_bytes > MAX_STREAM_TEXT_BYTES:
            raise ProtocolTransformError(
                "streamed reasoning summary exceeded the per-part byte limit",
                code="HUB_SSE_TEXT_TOO_LARGE",
            )
        self._set_response_text_size(
            self.response_reasoning_bytes,
            key,
            total_bytes,
        )
        fragments = self.response_reasoning_fragments.setdefault(key, [])
        if delta:
            fragments.append(delta)
        return self._thinking(delta) if delta else []

    def _response_reasoning_snapshot(
        self,
        key: tuple[str, str],
        snapshot: str,
    ) -> list[bytes]:
        snapshot_bytes = len(snapshot.encode("utf-8"))
        if snapshot_bytes > MAX_STREAM_TEXT_BYTES:
            raise ProtocolTransformError(
                "streamed reasoning summary exceeded the per-part byte limit",
                code="HUB_SSE_TEXT_TOO_LARGE",
            )
        if key in self.response_reasoning_done:
            if snapshot == self.response_reasoning_snapshots.get(key):
                return []
            raise ProtocolTransformError(
                "Responses emitted conflicting reasoning done snapshots",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        current = "".join(self.response_reasoning_fragments.get(key, []))
        if current and not snapshot.startswith(current):
            raise ProtocolTransformError(
                "Responses reasoning snapshot conflicts with earlier deltas",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        suffix = snapshot[len(current) :]
        chunks = self._response_reasoning_delta(key, suffix) if suffix else []
        self.response_reasoning_done.add(key)
        self.response_reasoning_snapshots[key] = snapshot
        self._set_response_text_size(
            self.response_reasoning_bytes,
            key,
            snapshot_bytes,
        )
        self.response_reasoning_fragments.pop(key, None)
        return chunks

    def _response_content_part(self, payload: dict, *, done: bool) -> list[bytes]:
        part = payload.get("part")
        if not isinstance(part, dict):
            raise ProtocolTransformError(
                "Responses content part must be an object",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        part_type = part.get("type")
        text_field = {"output_text": "text", "refusal": "refusal"}.get(
            part_type
        )
        if text_field is None:
            raise ProtocolTransformError(
                f"Responses content part {part_type!r} is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        allowed_fields = (
            {"type", "text", "annotations", "citations"}
            if part_type == "output_text"
            else {"type", "refusal"}
        )
        if set(part) - allowed_fields:
            raise ProtocolTransformError(
                "Responses content part contains unsupported fields",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        snapshot = part.get(text_field)
        if not isinstance(snapshot, str):
            raise ProtocolTransformError(
                "Responses content part text must be a string",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        item_id, coordinates = self._response_part_coordinates(
            payload,
            "content_index",
        )
        text_kind = (
            "response.refusal" if part_type == "refusal" else "response.output_text"
        )
        text_key = (text_kind, *coordinates)
        state = self.response_content_parts.get(coordinates)
        if part_type == "refusal":
            self.refused = True
        if part.get("annotations") or part.get("citations"):
            self._observe("HUB_DEGRADE_CITATION_METADATA_DROPPED")

        if not done:
            if state is not None:
                if not state.open:
                    raise ProtocolTransformError(
                        "Responses content part was added after it was done",
                        code="HUB_SSE_ORDER_VIOLATION",
                    )
                if (
                    state.item_id == item_id
                    and state.part_type == part_type
                    and state.added_snapshot == snapshot
                ):
                    return []
                raise ProtocolTransformError(
                    "Responses emitted conflicting content part snapshots",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            sibling_keys = (
                ("response.output_text", *coordinates),
                ("response.refusal", *coordinates),
            )
            if any(self._response_text_key_seen(key) for key in sibling_keys):
                raise ProtocolTransformError(
                    "Responses content part was added after its text began",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if (
                len(self.response_content_parts) + len(self.response_summary_parts)
                >= MAX_STREAM_BLOCKS
            ):
                raise ProtocolTransformError(
                    "stream tracked too many structural response parts",
                    code="HUB_SSE_TOO_MANY_BLOCKS",
                )
            self.response_content_parts[coordinates] = ResponseStreamPartState(
                item_id=item_id,
                part_type=part_type,
                added_snapshot=snapshot,
            )
            return self._response_text_delta(text_key, snapshot) if snapshot else []

        if state is None:
            raise ProtocolTransformError(
                "Responses content part was done without one matching added event",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if state.item_id != item_id or state.part_type != part_type:
            raise ProtocolTransformError(
                "Responses completed content part conflicts with its added snapshot",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if not state.open:
            if state.done_snapshot == snapshot:
                return []
            raise ProtocolTransformError(
                "Responses emitted conflicting content part done snapshots",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        chunks = self._response_text_snapshot(text_key, snapshot)
        state.open = False
        state.done_snapshot = snapshot
        return chunks

    def _response_summary_part(self, payload: dict, *, done: bool) -> list[bytes]:
        part = payload.get("part")
        if not isinstance(part, dict) or part.get("type") != "summary_text":
            part_type = part.get("type") if isinstance(part, dict) else None
            raise ProtocolTransformError(
                f"Responses reasoning summary part {part_type!r} is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        if set(part) - {"type", "text"}:
            raise ProtocolTransformError(
                "Responses reasoning summary part contains unsupported fields",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        snapshot = part.get("text")
        if not isinstance(snapshot, str):
            raise ProtocolTransformError(
                "Responses reasoning summary text must be a string",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        item_id, coordinates = self._response_part_coordinates(
            payload,
            "summary_index",
        )
        state = self.response_summary_parts.get(coordinates)

        if not done:
            if state is not None:
                if not state.open:
                    raise ProtocolTransformError(
                        "Responses summary part was added after it was done",
                        code="HUB_SSE_ORDER_VIOLATION",
                    )
                if (
                    state.item_id == item_id
                    and state.part_type == "summary_text"
                    and state.added_snapshot == snapshot
                ):
                    return []
                raise ProtocolTransformError(
                    "Responses emitted conflicting summary part snapshots",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            if (
                coordinates in self.response_reasoning_fragments
                or coordinates in self.response_reasoning_done
                or coordinates in self.response_reasoning_snapshots
            ):
                raise ProtocolTransformError(
                    "Responses summary part was added after its text began",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if (
                len(self.response_content_parts) + len(self.response_summary_parts)
                >= MAX_STREAM_BLOCKS
            ):
                raise ProtocolTransformError(
                    "stream tracked too many structural response parts",
                    code="HUB_SSE_TOO_MANY_BLOCKS",
                )
            self.response_summary_parts[coordinates] = ResponseStreamPartState(
                item_id=item_id,
                part_type="summary_text",
                added_snapshot=snapshot,
            )
            return (
                self._response_reasoning_delta(coordinates, snapshot)
                if snapshot
                else []
            )

        if state is None:
            raise ProtocolTransformError(
                "Responses summary part was done without one matching added event",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if state.item_id != item_id or state.part_type != "summary_text":
            raise ProtocolTransformError(
                "Responses completed summary part conflicts with its added snapshot",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if not state.open:
            if state.done_snapshot == snapshot:
                return []
            raise ProtocolTransformError(
                "Responses emitted conflicting summary part done snapshots",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        chunks = self._response_reasoning_snapshot(coordinates, snapshot)
        state.open = False
        state.done_snapshot = snapshot
        return chunks

    def _response_reasoning_item_snapshot(
        self,
        payload: dict,
        item: dict,
    ) -> list[bytes]:
        _require_upstream_field_allowlist(
            item,
            {
                "type",
                "id",
                "summary",
                "encrypted_content",
                "content",
                "status",
            },
            path="$.item",
            label="Responses reasoning item",
        )
        if "id" in item and (
            not isinstance(item["id"], str) or not item["id"]
        ):
            raise ProtocolTransformError(
                "Responses reasoning item id must be a non-empty string",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.id",
            )
        if "status" in item and item["status"] != "completed":
            raise ProtocolTransformError(
                "Responses reasoning done snapshot must be completed",
                code="HUB_SSE_DUPLICATE_CONFLICT",
                path="$.item.status",
            )
        reasoning_content = item.get("content", _MISSING)
        if reasoning_content is not _MISSING and reasoning_content not in (None, []):
            raise ProtocolTransformError(
                "Responses reasoning content is unsupported",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.content",
            )
        encrypted = item.get("encrypted_content", _MISSING)
        if encrypted is not _MISSING and encrypted is not None and not isinstance(
            encrypted, str
        ):
            raise ProtocolTransformError(
                "Responses reasoning encrypted_content must be a string or null",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.encrypted_content",
            )
        summary = item.get("summary", _MISSING)
        if summary is not _MISSING and not isinstance(summary, list):
            raise ProtocolTransformError(
                "Responses reasoning summary must be an array",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.summary",
            )
        output_index = self._response_stream_index(payload.get("output_index"))
        if output_index is None:
            raise ProtocolTransformError(
                "Responses reasoning snapshot requires a valid output_index",
                code="HUB_SSE_ORDER_VIOLATION",
                path="$.output_index",
            )

        chunks: list[bytes] = []
        if isinstance(encrypted, str) and encrypted:
            redaction_key = f"output:{output_index}"
            prior_encrypted = self.response_redacted_snapshots.get(redaction_key)
            if prior_encrypted is not None and prior_encrypted != encrypted:
                raise ProtocolTransformError(
                    "Responses reasoning snapshot contains conflicting encrypted content",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item.encrypted_content",
                )
            if prior_encrypted is None:
                if len(self.response_redacted_snapshots) >= MAX_STREAM_BLOCKS:
                    raise ProtocolTransformError(
                        "stream tracked too many redacted reasoning snapshots",
                        code="HUB_SSE_TOO_MANY_BLOCKS",
                        path="$.item.encrypted_content",
                    )
                self.response_redacted_snapshots[redaction_key] = encrypted
                chunks.extend(self._redacted_thinking(encrypted))
        if isinstance(summary, list):
            for summary_index, part in enumerate(summary):
                part_path = f"$.item.summary[{summary_index}]"
                if not isinstance(part, dict) or part.get("type") != "summary_text":
                    raise ProtocolTransformError(
                        "Responses reasoning summary part is unsupported",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=part_path,
                    )
                _require_upstream_field_allowlist(
                    part,
                    {"type", "text"},
                    path=part_path,
                    label="Responses reasoning summary part",
                )
                text = part.get("text")
                if not isinstance(text, str):
                    raise ProtocolTransformError(
                        "Responses reasoning summary text must be a string",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=f"{part_path}.text",
                    )
                key = (output_index, str(summary_index))
                state = self.response_summary_parts.get(key)
                item_id = item.get("id")
                if state is not None and (
                    not isinstance(item_id, str) or state.item_id != item_id
                ):
                    raise ProtocolTransformError(
                        "Responses reasoning snapshot conflicts with its summary part identity",
                        code="HUB_SSE_DUPLICATE_CONFLICT",
                        path="$.item.id",
                    )
                chunks.extend(self._response_reasoning_snapshot(key, text))
                if state is not None:
                    # See _response_message_item_snapshot: the terminal item
                    # snapshot stands in for the omitted summary_part.done.
                    state.open = False
                    state.done_snapshot = text
        return chunks

    def _response_message_item_snapshot(
        self,
        payload: dict,
        item: dict,
    ) -> list[bytes]:
        _require_upstream_field_allowlist(
            item,
            {"type", "id", "role", "content", "status"},
            path="$.item",
            label="Responses message item",
        )
        item_id = item.get("id")
        if "id" in item and (
            not isinstance(item_id, str) or not item_id
        ):
            raise ProtocolTransformError(
                "Responses message item id must be a non-empty string",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.id",
            )
        if "role" in item and item["role"] != "assistant":
            raise ProtocolTransformError(
                "Responses output message role must be assistant",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.role",
            )
        if "status" in item and item["status"] != "completed":
            raise ProtocolTransformError(
                "Responses message done snapshot must be completed",
                code="HUB_SSE_DUPLICATE_CONFLICT",
                path="$.item.status",
            )
        content = item.get("content")
        if not isinstance(content, list):
            raise ProtocolTransformError(
                "Responses message snapshot content must be an array",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.content",
            )
        output_index = self._response_stream_index(payload.get("output_index"))
        if output_index is None:
            raise ProtocolTransformError(
                "Responses message snapshot requires a valid output_index",
                code="HUB_SSE_ORDER_VIOLATION",
                path="$.output_index",
            )

        chunks: list[bytes] = []
        for content_index, part in enumerate(content):
            part_path = f"$.item.content[{content_index}]"
            if not isinstance(part, dict):
                raise ProtocolTransformError(
                    "Responses message snapshot parts must be objects",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=part_path,
                )
            part_type = part.get("type")
            if part_type not in {"output_text", "refusal"}:
                raise ProtocolTransformError(
                    f"Responses message snapshot part {part_type!r} is unsupported",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{part_path}.type",
                )
            allowed_fields = (
                {"type", "text", "annotations", "citations"}
                if part_type == "output_text"
                else {"type", "refusal"}
            )
            _require_upstream_field_allowlist(
                part,
                allowed_fields,
                path=part_path,
                label="Responses message snapshot part",
            )
            text_kind = (
                "response.refusal"
                if part_type == "refusal"
                else "response.output_text"
            )
            if part_type == "refusal":
                self.refused = True
            coordinates = (output_index, str(content_index))
            state = self.response_content_parts.get(coordinates)
            if state is not None:
                if not isinstance(item_id, str) or state.item_id != item_id:
                    raise ProtocolTransformError(
                        "Responses message snapshot conflicts with its content part identity",
                        code="HUB_SSE_DUPLICATE_CONFLICT",
                        path="$.item.id",
                    )
                if state.part_type != part_type:
                    raise ProtocolTransformError(
                        "Responses message snapshot conflicts with its content part type",
                        code="HUB_SSE_DUPLICATE_CONFLICT",
                        path=f"{part_path}.type",
                    )
            text_key = "refusal" if part_type == "refusal" else "text"
            text = part.get(text_key)
            if not isinstance(text, str):
                raise ProtocolTransformError(
                    "Responses message snapshot text must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"{part_path}.{text_key}",
                )
            chunks.extend(
                self._response_text_snapshot((text_kind, *coordinates), text)
            )
            if state is not None:
                # A terminal item snapshot completes the part even when the
                # upstream omitted the matching content_part.done event.
                state.open = False
                state.done_snapshot = text
            if part.get("annotations") or part.get("citations"):
                self._observe("HUB_DEGRADE_CITATION_METADATA_DROPPED")
        return chunks

    def _register_response_output_item(
        self,
        payload: dict,
        item: dict,
    ) -> str | None:
        if "output_index" not in payload:
            return None
        output_index = self._response_stream_index(payload["output_index"])
        if output_index is None:
            raise ProtocolTransformError(
                "Responses output item requires a valid output_index",
                code="HUB_SSE_ORDER_VIOLATION",
                path="$.output_index",
            )
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type:
            raise ProtocolTransformError(
                "Responses output item type must be non-empty text",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.type",
            )
        item_id = item.get("id")
        if "id" in item and (
            not isinstance(item_id, str) or not item_id
        ):
            raise ProtocolTransformError(
                "Responses output item id must be non-empty text",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.id",
            )
        if isinstance(item_id, str) and len(item_id) > MAX_STREAM_ITEM_ID_CHARS:
            raise ProtocolTransformError(
                "Responses output item id exceeds the length limit",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.item.id",
            )
        existing = self.response_output_items.get(output_index)
        if existing is not None:
            existing_type, existing_id = existing
            if existing_type != item_type or (
                existing_id is not None
                and item_id is not None
                and existing_id != item_id
            ):
                raise ProtocolTransformError(
                    "Responses output item identity conflicts with an earlier snapshot",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item",
                )
            if existing_id is None and isinstance(item_id, str):
                self.response_output_items[output_index] = (item_type, item_id)
        else:
            if len(self.response_output_items) >= MAX_STREAM_BLOCKS:
                raise ProtocolTransformError(
                    "stream tracked too many response output items",
                    code="HUB_SSE_TOO_MANY_BLOCKS",
                    path="$.output_index",
                )
            self.response_output_items[output_index] = (item_type, item_id)
        if isinstance(item_id, str):
            previous_index = self.response_output_item_ids.get(item_id)
            if previous_index is not None and previous_index != output_index:
                raise ProtocolTransformError(
                    "Responses output item id moved to another output position",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item.id",
                )
            if (
                previous_index is None
                and len(self.response_output_item_ids) >= MAX_STREAM_BLOCKS
            ):
                raise ProtocolTransformError(
                    "stream tracked too many response output item ids",
                    code="HUB_SSE_TOO_MANY_BLOCKS",
                    path="$.item.id",
                )
            self.response_output_item_ids[item_id] = output_index
        return output_index

    def _validate_response_lifecycle_snapshot(
        self,
        kind: str,
        payload: dict,
    ) -> None:
        response = payload.get("response", _MISSING)
        if not isinstance(response, dict):
            raise ProtocolTransformError(
                "Responses lifecycle event requires a response object",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path="$.response",
            )
        self._validate_response_snapshot_fields(response)
        for field_name in ("id", "model"):
            if field_name in response and (
                not isinstance(response[field_name], str) or not response[field_name]
            ):
                raise ProtocolTransformError(
                    f"Responses lifecycle {field_name} must be non-empty text",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                    path=f"$.response.{field_name}",
                )
        expected_status = {
            "response.created": "in_progress",
            "response.in_progress": "in_progress",
            "response.queued": "queued",
        }[kind]
        if "status" in response and response["status"] != expected_status:
            raise ProtocolTransformError(
                "Responses lifecycle status conflicts with its event",
                code="HUB_SSE_DUPLICATE_CONFLICT",
                path="$.response.status",
            )
        if response.get("error") is not None:
            raise ProtocolTransformError(
                "Responses lifecycle response cannot contain an error",
                code="HUB_SSE_DUPLICATE_CONFLICT",
                path="$.response.error",
            )

    def _validate_response_snapshot_fields(self, response: dict) -> None:
        unknown_fields = set(response) - _RESPONSES_STREAM_RESPONSE_FIELDS
        if unknown_fields:
            field_name = sorted(unknown_fields)[0]
            raise ProtocolTransformError(
                f"Responses response snapshot field {field_name!r} is unsupported",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path=f"$.response.{field_name}",
            )
        for field_name in ("id", "model"):
            if field_name in response and (
                not isinstance(response[field_name], str) or not response[field_name]
            ):
                raise ProtocolTransformError(
                    f"Responses response snapshot {field_name} must be non-empty text",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                    path=f"$.response.{field_name}",
                )
        if "error" in response and response["error"] is not None and not isinstance(
            response["error"], dict
        ):
            raise ProtocolTransformError(
                "Responses response snapshot error must be an object or null",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path="$.response.error",
            )
        mapped_fields = {
            "id",
            "model",
            "status",
            "incomplete_details",
            "usage",
            "output",
            "error",
        }
        if set(response) - mapped_fields:
            self._observe_stream_degradation(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                "Responses response metadata has no Anthropic event carrier",
            )

    def _validate_response_output_item_added(self, item: dict) -> None:
        item_type = item.get("type")
        if item_type == "message":
            _require_upstream_field_allowlist(
                item,
                {"type", "id", "role", "content", "status"},
                path="$.item",
                label="Responses message item",
            )
            if "id" in item and (
                not isinstance(item["id"], str) or not item["id"]
            ):
                raise ProtocolTransformError(
                    "Responses message item id must be non-empty text",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.item.id",
                )
            if "role" in item and item["role"] != "assistant":
                raise ProtocolTransformError(
                    "Responses output message role must be assistant",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.item.role",
                )
            if "status" in item and item["status"] != "in_progress":
                raise ProtocolTransformError(
                    "Responses added message item must be in progress",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item.status",
                )
            content = item.get("content", [])
            if not isinstance(content, list) or content:
                raise ProtocolTransformError(
                    "Responses added message item cannot contain completed content",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.item.content",
                )
            return
        if item_type == "reasoning":
            _require_upstream_field_allowlist(
                item,
                {
                    "type",
                    "id",
                    "summary",
                    "encrypted_content",
                    "content",
                    "status",
                },
                path="$.item",
                label="Responses reasoning item",
            )
            if "id" in item and (
                not isinstance(item["id"], str) or not item["id"]
            ):
                raise ProtocolTransformError(
                    "Responses reasoning item id must be non-empty text",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.item.id",
                )
            if "status" in item and item["status"] != "in_progress":
                raise ProtocolTransformError(
                    "Responses added reasoning item must be in progress",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item.status",
                )
            for field_name in ("summary", "content"):
                value = item.get(field_name, [])
                if not isinstance(value, list) or value:
                    raise ProtocolTransformError(
                        "Responses added reasoning item cannot contain completed content",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=f"$.item.{field_name}",
                    )
            encrypted = item.get("encrypted_content", None)
            if encrypted not in (None, ""):
                raise ProtocolTransformError(
                    "Responses added reasoning item cannot contain encrypted final content",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path="$.item.encrypted_content",
                )
            return
        raise ProtocolTransformError(
            f"Responses output item {item_type!r} is unsupported",
            code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

    def _single_anonymous_response_tool_key(self) -> str | None:
        keys = {
            key
            for key in self.anonymous_response_tool_keys
            if self.tool_indices.get(key) in self.open_indices
        }
        return next(iter(keys)) if len(keys) == 1 else None

    @staticmethod
    def _response_tool_alias(kind: str, value: str) -> str:
        return f"responses:{kind}:{value}"

    def _register_response_tool_aliases(
        self,
        index: int,
        *,
        item_id: str | None,
        call_id: str | None,
        output_index: str | None,
    ) -> None:
        for kind, value in (
            ("item", item_id),
            ("call", call_id),
            ("output", output_index),
        ):
            if value is None:
                continue
            alias = self._response_tool_alias(kind, value)
            existing_index = self.tool_indices.get(alias)
            if existing_index is not None and existing_index != index:
                raise ProtocolTransformError(
                    "Responses function call identity conflicts with an earlier call",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            self.tool_indices[alias] = index

    def _observe_synthetic_response_tool_id(self) -> None:
        if self.compatibility_mode == "strict":
            raise ProtocolTransformError(
                "Responses function call requires a real upstream id",
                code="HUB_SSE_TOOL_CALL_INVALID",
            )
        self._observe("HUB_DEGRADE_SYNTHETIC_TOOL_ID")

    def _response_function_call_item(
        self,
        item: dict,
        *,
        done: bool,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        allowed_fields = {
            "type",
            "id",
            "call_id",
            "name",
            "arguments",
            "input",
            "status",
        }
        if set(item) - allowed_fields:
            raise ProtocolTransformError(
                "Responses function call item has unsupported fields",
                code="HUB_SSE_TOOL_CALL_INVALID",
            )
        if item.get("type") != "function_call":
            raise ProtocolTransformError(
                "Responses streamed tool call type must be function_call",
                code="HUB_SSE_TOOL_CALL_INVALID",
                path="$.item.type",
            )
        id_value = item.get("id")
        call_id = item.get("call_id")
        for field_name, value in (("id", id_value), ("call_id", call_id)):
            if field_name in item and (
                not isinstance(value, str) or not value
            ):
                raise ProtocolTransformError(
                    f"Responses function call {field_name} must be non-empty text",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path=f"$.item.{field_name}",
                )
        name = item.get("name")
        if (not done or "name" in item) and (
            not isinstance(name, str) or not name
        ):
            raise ProtocolTransformError(
                "Responses function call name must be non-empty text",
                code="HUB_SSE_TOOL_CALL_INVALID",
                path="$.item.name",
            )
        if "status" in item:
            expected_status = "completed" if done else "in_progress"
            if item["status"] != expected_status:
                raise ProtocolTransformError(
                    "Responses function call status conflicts with its event",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                    path="$.item.status",
                )
        if "arguments" in item and "input" in item:
            raise ProtocolTransformError(
                "Responses function call cannot contain both arguments and input",
                code="HUB_SSE_TOOL_CALL_INVALID",
            )
        arguments: str | None = None
        if "arguments" in item:
            if not isinstance(item["arguments"], str):
                raise ProtocolTransformError(
                    "Responses function call arguments must be text",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path="$.item.arguments",
                )
            arguments = item["arguments"]
        elif "input" in item:
            parsed_input = _parse_upstream_tool_arguments(item["input"])
            arguments = _json_text(parsed_input)
        return id_value, call_id, name, arguments

    def _response_function_call_item_snapshot(
        self,
        payload: dict,
        item: dict,
        *,
        allow_create: bool = False,
    ) -> list[bytes]:
        id_value, call_id, name, arguments = self._response_function_call_item(
            item,
            done=True,
        )
        identity_payload: dict[str, object] = {}
        if id_value is not None:
            identity_payload["item_id"] = id_value
        if call_id is not None:
            identity_payload["call_id"] = call_id
        if "output_index" in payload:
            identity_payload["output_index"] = payload["output_index"]
        output_index = (
            self._response_stream_index(payload["output_index"])
            if "output_index" in payload
            else None
        )
        aliases = [
            self._response_tool_alias(kind, value)
            for kind, value in (
                ("item", id_value),
                ("call", call_id),
                ("output", output_index),
            )
            if value is not None
        ]
        if allow_create and not any(alias in self.tool_indices for alias in aliases):
            if name is None or arguments is None:
                raise ProtocolTransformError(
                    "terminal-only Responses function calls require name and arguments",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            if id_value is None and call_id is None:
                self._observe_synthetic_response_tool_id()
            key = aliases[0]
            chunks = self._tool_start(
                key,
                _stream_identifier(call_id, id_value, output_index) or key,
                name,
            )
            index = self.tool_indices[key]
            self._register_response_tool_aliases(
                index,
                item_id=id_value,
                call_id=call_id,
                output_index=output_index,
            )
            chunks.extend(self._tool_snapshot(key, arguments))
            self.response_tool_done_snapshots[index] = arguments
            return [*chunks, *self._close(index)]
        key = self._response_tool_argument_key(
            identity_payload,
            require_open=False,
        )
        index = self.tool_indices[key]
        descriptor = next(
            (
                value
                for descriptor_key, value in self.tool_descriptors.items()
                if self.tool_indices.get(descriptor_key) == index
            ),
            None,
        )
        if descriptor is None:
            raise ProtocolTransformError(
                "Responses function call snapshot has no descriptor",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        descriptor_id, descriptor_name = descriptor
        if name is not None and name != descriptor_name:
            raise ProtocolTransformError(
                "Responses function call name conflicts with its added event",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if (
            call_id is not None
            and call_id != descriptor_id
            and self.tool_indices.get(self._response_tool_alias("call", call_id))
            != index
        ):
            raise ProtocolTransformError(
                "Responses function call id conflicts with its added event",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        prior_snapshot = self.response_tool_done_snapshots.get(index)
        if index not in self.open_indices:
            if prior_snapshot is None:
                raise ProtocolTransformError(
                    "Responses function call snapshot has no matching open call",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            if arguments is not None and arguments != prior_snapshot:
                raise ProtocolTransformError(
                    "Responses function call snapshot conflicts with completed arguments",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
            return []

        chunks: list[bytes] = []
        if arguments is not None:
            chunks.extend(self._tool_snapshot(key, arguments))
        snapshot = arguments
        if snapshot is None:
            snapshot = "".join(self.tool_argument_fragments.get(index, []))
        self.response_tool_done_snapshots[index] = snapshot
        self.anonymous_response_tool_keys.discard(key)
        return [*chunks, *self._close(index)]

    def _response_output_snapshot(self, response: dict) -> list[bytes]:
        output = response.get("output", _MISSING)
        if output is _MISSING:
            return []
        if not isinstance(output, list):
            raise ProtocolTransformError(
                "Responses response output snapshot must be an array",
                code="HUB_UPSTREAM_RESPONSE_INVALID",
                path="$.response.output",
            )
        chunks: list[bytes] = []
        for output_index, item in enumerate(output):
            if not isinstance(item, dict):
                raise ProtocolTransformError(
                    "Responses response output items must be objects",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                    path=f"$.response.output[{output_index}]",
                )
            item_payload = {"output_index": output_index}
            item_type = item.get("type")
            self._register_response_output_item(item_payload, item)
            if item_type == "message":
                chunks.extend(self._response_message_item_snapshot(item_payload, item))
            elif item_type == "reasoning":
                chunks.extend(self._response_reasoning_item_snapshot(item_payload, item))
            elif item_type == "function_call":
                chunks.extend(
                    self._response_function_call_item_snapshot(
                        item_payload,
                        item,
                        allow_create=True,
                    )
                )
            else:
                raise ProtocolTransformError(
                    f"Responses output item {item_type!r} is unsupported",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"$.response.output[{output_index}].type",
                )
        return chunks

    def _response_tool_argument_key(
        self,
        payload: dict,
        *,
        require_open: bool = True,
    ) -> str:
        identifiers: list[str] = []
        for field_name in ("item_id", "call_id"):
            if field_name not in payload:
                continue
            value = payload[field_name]
            if not isinstance(value, str) or not value:
                raise ProtocolTransformError(
                    f"Responses tool event {field_name} must be non-empty text",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path=f"$.{field_name}",
                )
            alias_kind = "item" if field_name == "item_id" else "call"
            identifiers.append(self._response_tool_alias(alias_kind, value))
        if "output_index" in payload:
            output_index = self._response_stream_index(payload["output_index"])
            if output_index is None:
                raise ProtocolTransformError(
                    "Responses tool event output_index must be non-negative",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path="$.output_index",
                )
            identifiers.append(self._response_tool_alias("output", output_index))
        if not identifiers:
            anonymous = self._single_anonymous_response_tool_key()
            if anonymous is not None:
                identifiers.append(anonymous)
        if not identifiers:
            raise ProtocolTransformError(
                "Responses tool arguments could not be matched to an open tool call",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        indices: set[int] = set()
        for identifier in identifiers:
            index = self.tool_indices.get(identifier)
            if index is None or (require_open and index not in self.open_indices):
                raise ProtocolTransformError(
                    "Responses tool identity does not match an open tool call",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            indices.add(index)
        if len(indices) != 1:
            raise ProtocolTransformError(
                "Responses tool identifiers refer to conflicting calls",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        return identifiers[0]

    def feed(self, event: str, data: str) -> list[bytes]:
        if self.stopped:
            raise ProtocolTransformError(
                "upstream emitted data after the stream was closed",
                code="HUB_SSE_LATE_EVENT",
            )
        if data == "[DONE]":
            if not self.upstream_terminal:
                raise ProtocolTransformError(
                    "upstream SSE transport ended before a semantic terminal event",
                    code="HUB_SSE_MISSING_TERMINAL",
                )
            return self.finish()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProtocolTransformError(
                "upstream SSE contains invalid JSON",
                code="HUB_SSE_JSON_INVALID",
            ) from exc
        if not isinstance(payload, dict):
            raise ProtocolTransformError(
                "upstream SSE payload must be an object",
                code="HUB_SSE_UNKNOWN_EVENT",
            )
        if self.api_format == "openai_chat" and event != "message":
            raise ProtocolTransformError(
                f"OpenAI Chat SSE event {event!r} is unsupported",
                code="HUB_SSE_UNKNOWN_EVENT",
            )
        if self.api_format == "openai_responses":
            payload_type = payload.get("type", _MISSING)
            if payload_type is not _MISSING and (
                not isinstance(payload_type, str) or not payload_type
            ):
                raise ProtocolTransformError(
                    "Responses SSE payload type must be a non-empty string",
                    code="HUB_SSE_UNKNOWN_EVENT",
                    path="$.type",
                )
            if (
                event != "message"
                and isinstance(payload_type, str)
                and payload_type != event
            ):
                raise ProtocolTransformError(
                    "Responses SSE event name conflicts with payload type",
                    code="HUB_SSE_DUPLICATE_CONFLICT",
                )
        if self.upstream_terminal and self.api_format != "openai_chat":
            kind = payload.get("type") if isinstance(payload.get("type"), str) else event
            code = (
                "HUB_SSE_DUPLICATE_CONFLICT"
                if kind
                in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                    "error",
                }
                else "HUB_SSE_LATE_EVENT"
            )
            raise ProtocolTransformError(
                "upstream Responses emitted an event after terminal",
                code=code,
            )
        return (
            self._feed_chat(payload)
            if self.api_format == "openai_chat"
            else self._feed_responses(event, payload)
        )

    def _feed_chat(self, payload: dict) -> list[bytes]:
        allowed_payload_fields = {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "service_tier",
            "system_fingerprint",
        }
        if set(payload) - allowed_payload_fields:
            raise ProtocolTransformError(
                "OpenAI Chat stream payload has unsupported fields",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        for field_name in ("id", "model"):
            if field_name in payload:
                value = payload[field_name]
                if not isinstance(value, str) or not value:
                    raise ProtocolTransformError(
                        f"OpenAI Chat stream {field_name} must be non-empty text",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path=f"$.{field_name}",
                    )
                self._update_stream_identity(
                    field_name,
                    value,
                    path=f"$.{field_name}",
                )
        if "created" in payload and (
            not isinstance(payload["created"], int)
            or isinstance(payload["created"], bool)
            or payload["created"] < 0
        ):
            raise ProtocolTransformError(
                "OpenAI Chat stream created must be a non-negative integer",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.created",
            )
        if "object" in payload and (
            not isinstance(payload["object"], str) or not payload["object"]
        ):
            raise ProtocolTransformError(
                "OpenAI Chat stream object must be non-empty text",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.object",
            )
        for field_name in ("service_tier", "system_fingerprint"):
            if field_name in payload and payload[field_name] is not None and not isinstance(
                payload[field_name], str
            ):
                raise ProtocolTransformError(
                    f"OpenAI Chat stream {field_name} must be text or null",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    path=f"$.{field_name}",
                )
        if any(
            field_name in payload
            for field_name in (
                "object",
                "created",
                "service_tier",
                "system_fingerprint",
            )
        ):
            self._observe_stream_degradation(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                "OpenAI Chat stream metadata has no Anthropic event carrier",
            )
        usage = payload.get("usage", _MISSING)
        self._consume_stream_usage(
            usage,
            input_key="prompt_tokens",
            output_key="completion_tokens",
        )
        # Chat providers commonly send one choices=[] usage event after the
        # finish reason. Preserve that metadata, while rejecting late content.
        if self.upstream_terminal:
            if payload.get("choices") not in (None, []):
                raise ProtocolTransformError(
                    "upstream Chat emitted choices after finish_reason",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            return []
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise ProtocolTransformError(
                "OpenAI Chat SSE choices must be an array",
                code="HUB_SSE_UNKNOWN_EVENT",
            )
        if not choices:
            if isinstance(usage, dict):
                return []
            raise ProtocolTransformError(
                "OpenAI Chat emitted an empty event without usage",
                code="HUB_SSE_UNKNOWN_EVENT",
            )
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProtocolTransformError(
                "OpenAI Chat SSE requires exactly one choice object",
                code="HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
            )
        choice = choices[0]
        unknown_choice_fields = set(choice) - {
            "index",
            "delta",
            "finish_reason",
            "logprobs",
        }
        if unknown_choice_fields:
            raise ProtocolTransformError(
                "OpenAI Chat stream choice has unsupported fields",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        if "index" in choice and self._response_stream_index(choice["index"]) is None:
            raise ProtocolTransformError(
                "OpenAI Chat stream choice index must be non-negative",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.choices[0].index",
            )
        if self._response_stream_index(choice.get("index", 0)) != "0":
            raise ProtocolTransformError(
                "OpenAI Chat stream choice index must be zero",
                code="HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
                path="$.choices[0].index",
            )
        if "logprobs" in choice:
            self._observe_stream_degradation(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                "OpenAI Chat logprobs have no Anthropic event carrier",
            )
        raw_delta = choice.get("delta", {})
        if not isinstance(raw_delta, dict):
            raise ProtocolTransformError(
                "OpenAI Chat SSE delta must be an object",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        delta = raw_delta
        unknown_delta_fields = set(delta) - {
            "role",
            "content",
            "reasoning_content",
            "reasoning_signature",
            "signature",
            "refusal",
            "tool_calls",
            "annotations",
            "citations",
        }
        if unknown_delta_fields:
            raise ProtocolTransformError(
                "OpenAI Chat emitted an unsupported delta field",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        chunks: list[bytes] = []
        role = delta.get("role", _MISSING)
        if role is not _MISSING and role is not None and role != "assistant":
            raise ProtocolTransformError(
                "OpenAI Chat streamed role must be assistant",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                path="$.choices[0].delta.role",
            )
        reasoning = self._optional_stream_text(
            delta.get("reasoning_content", _MISSING),
            field_name="choices[0].delta.reasoning_content",
        )
        if reasoning:
            chunks.extend(self._thinking(reasoning))
        signatures: list[str] = []
        for signature_field in ("reasoning_signature", "signature"):
            if signature_field not in delta:
                continue
            signature_value = delta[signature_field]
            if not isinstance(signature_value, str) or not signature_value:
                raise ProtocolTransformError(
                    "upstream thinking signature must be a non-empty string",
                    code="HUB_SSE_SIGNATURE_ORDER",
                )
            signatures.append(signature_value)
        if len(set(signatures)) > 1:
            raise ProtocolTransformError(
                "upstream thinking signature aliases conflict",
                code="HUB_SSE_DUPLICATE_CONFLICT",
            )
        if signatures:
            chunks.extend(self._signature(signatures[0]))
        text = self._optional_stream_text(
            delta.get("content", _MISSING),
            field_name="choices[0].delta.content",
        )
        if text:
            chunks.extend(self._text(text))
        refusal = self._optional_stream_text(
            delta.get("refusal", _MISSING),
            field_name="choices[0].delta.refusal",
        )
        if refusal:
            self.refused = True
            chunks.extend(self._text(refusal))
        if "annotations" in delta or "citations" in delta:
            if self.text_index is None or self.text_index not in self.open_indices:
                raise ProtocolTransformError(
                    "citation metadata arrived without an open text block",
                    code="HUB_SSE_ORDER_VIOLATION",
                )
            self._observe("HUB_DEGRADE_CITATION_METADATA_DROPPED")
        raw_tool_calls = delta.get("tool_calls", [])
        if raw_tool_calls is None:
            raw_tool_calls = []
        if not isinstance(raw_tool_calls, list):
            raise ProtocolTransformError(
                "OpenAI Chat streamed tool_calls must be an array",
                code="HUB_SSE_TOOL_CALL_INVALID",
            )
        for position, call in enumerate(raw_tool_calls):
            if not isinstance(call, dict):
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool calls must be objects",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            unknown_call_fields = set(call) - {"index", "id", "type", "function"}
            if unknown_call_fields:
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool call has unsupported fields",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            if "type" in call and call["type"] != "function":
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool call type must be function",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path=f"$.choices[0].delta.tool_calls[{position}].type",
                )
            raw_index = call.get("index", position)
            key = self._response_stream_index(raw_index)
            if key is None:
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool call index must be non-negative",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path=f"$.choices[0].delta.tool_calls[{position}].index",
                )
            raw_function = call.get("function", {})
            if not isinstance(raw_function, dict):
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool function must be an object",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            function = raw_function
            if set(function) - {"name", "arguments"}:
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool function has unsupported fields",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            call_id_value = call.get("id", "")
            name_value = function.get("name", "")
            if not isinstance(call_id_value, str) or not isinstance(name_value, str):
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool identity must be text",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            if key not in self.tool_indices:
                chunks.extend(
                    self._tool_start(
                        key,
                        call_id_value,
                        name_value,
                    )
                )
            elif call_id_value or name_value:
                self._tool_start(key, call_id_value, name_value)
            arguments = function.get("arguments")
            if arguments is not None and not isinstance(arguments, str):
                raise ProtocolTransformError(
                    "OpenAI Chat streamed tool arguments must be text fragments",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                )
            if isinstance(arguments, str) and arguments:
                chunks.extend(self._tool_delta(key, arguments))
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str) or not finish_reason:
                raise ProtocolTransformError(
                    "upstream Chat finish_reason must be a non-empty string or null",
                    code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
                )
            self.upstream_terminal = True
            self.stop = _stop_reason(
                finish_reason,
                has_tool=self.has_tool,
                refused=self.refused,
            )
            self.fsm.mark_success()
        return chunks

    def _feed_responses(self, event: str, payload: dict) -> list[bytes]:
        kind = payload.get("type") if isinstance(payload.get("type"), str) else event
        self._validate_responses_event_envelope(kind, payload)
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        if isinstance(response.get("id"), str):
            self._update_stream_identity(
                "id",
                response["id"],
                path="$.response.id",
            )
        if isinstance(response.get("model"), str):
            self._update_stream_identity(
                "model",
                response["model"],
                path="$.response.model",
            )
        usage = response.get("usage", _MISSING)
        self._consume_stream_usage(
            usage,
            input_key="input_tokens",
            output_key="output_tokens",
        )
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            text_kind = kind.removesuffix(".delta")
            if text_kind == "response.refusal":
                self.refused = True
            key = self._response_text_event_key(text_kind, payload)
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise ProtocolTransformError(
                    "Responses text delta must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            return self._response_text_delta(key, delta)
        if kind in {"response.output_text.done", "response.refusal.done"}:
            text_kind = kind.removesuffix(".done")
            if text_kind == "response.refusal":
                self.refused = True
            key = self._response_text_event_key(text_kind, payload)
            text_key = "text" if text_kind == "response.output_text" else "refusal"
            text = payload.get(text_key)
            if not isinstance(text, str):
                raise ProtocolTransformError(
                    "Responses text done snapshot must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            return self._response_text_snapshot(key, text)
        if kind == "response.reasoning_summary_text.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise ProtocolTransformError(
                    "Responses reasoning summary delta must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            key = self._response_reasoning_event_key(payload)
            return self._response_reasoning_delta(key, delta)
        if kind == "response.reasoning_summary_text.done":
            text = payload.get("text")
            if not isinstance(text, str):
                raise ProtocolTransformError(
                    "Responses reasoning summary done snapshot must be a string",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
            key = self._response_reasoning_event_key(payload)
            return self._response_reasoning_snapshot(key, text)
        if kind == "response.content_part.added":
            return self._response_content_part(payload, done=False)
        if kind == "response.content_part.done":
            return self._response_content_part(payload, done=True)
        if kind == "response.reasoning_summary_part.added":
            return self._response_summary_part(payload, done=False)
        if kind == "response.reasoning_summary_part.done":
            return self._response_summary_part(payload, done=True)
        if kind in {
            "response.output_text.annotation.added",
            "response.output_text.annotation.delta",
            "response.output_text.citation.added",
            "response.output_text.citation.delta",
        }:
            self._validate_response_citation_event(kind, payload)
            self._observe_stream_degradation(
                "HUB_DEGRADE_CITATION_METADATA_DROPPED",
                "Responses citation metadata has no exact Anthropic event carrier",
            )
            return []
        if kind == "response.output_item.added":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "function_call":
                id_value, call_id, name, arguments = self._response_function_call_item(
                    item,
                    done=False,
                )
                output_index = (
                    self._response_stream_index(payload["output_index"])
                    if "output_index" in payload
                    else None
                )
                if "output_index" in payload and output_index is None:
                    raise ProtocolTransformError(
                        "Responses function call output_index must be non-negative",
                        code="HUB_SSE_TOOL_CALL_INVALID",
                        path="$.output_index",
                    )
                self._register_response_output_item(payload, item)
                key = next(
                    (
                        self._response_tool_alias(kind, value)
                        for kind, value in (
                            ("item", id_value),
                            ("call", call_id),
                            ("output", output_index),
                        )
                        if value is not None
                    ),
                    None,
                )
                if key is None:
                    key = f"response_function_call_{self.next_response_tool_key}"
                    self.next_response_tool_key += 1
                    self.anonymous_response_tool_keys.add(key)
                if id_value is None and call_id is None:
                    self._observe_synthetic_response_tool_id()
                chunks = self._tool_start(
                    key,
                    _stream_identifier(call_id, id_value, output_index) or key,
                    name or "",
                )
                index = self.tool_indices[key]
                self._register_response_tool_aliases(
                    index,
                    item_id=id_value,
                    call_id=call_id,
                    output_index=output_index,
                )
                if arguments:
                    chunks.extend(self._tool_delta(key, arguments))
                return chunks
            self._validate_response_output_item_added(item)
            if self._register_response_output_item(payload, item) is None:
                raise ProtocolTransformError(
                    "Responses non-tool output item requires an output_index",
                    code="HUB_SSE_ORDER_VIOLATION",
                    path="$.output_index",
                )
        if kind == "response.function_call_arguments.delta":
            key = self._response_tool_argument_key(payload)
            index = self.tool_indices[key]
            delta = payload.get("delta", _MISSING)
            if not isinstance(delta, str):
                raise ProtocolTransformError(
                    "Responses tool argument delta must be a string",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path="$.delta",
                )
            return self._tool_delta(key, delta)
        if kind == "response.function_call_arguments.done":
            key = self._response_tool_argument_key(payload)
            chunks: list[bytes] = []
            index = self.tool_indices[key]
            arguments = payload.get("arguments", _MISSING)
            if not isinstance(arguments, str):
                raise ProtocolTransformError(
                    "Responses tool argument done snapshot must be a string",
                    code="HUB_SSE_TOOL_CALL_INVALID",
                    path="$.arguments",
                )
            if arguments:
                chunks.extend(self._tool_snapshot(key, arguments))
            self.response_tool_done_snapshots[index] = arguments
            self.anonymous_response_tool_keys.discard(key)
            return [*chunks, *self._close(index)]
        if kind == "response.output_item.done":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            self._register_response_output_item(payload, item)
            if item.get("type") == "function_call":
                return self._response_function_call_item_snapshot(payload, item)
            if item.get("type") == "reasoning":
                return self._response_reasoning_item_snapshot(payload, item)
            if item.get("type") == "message":
                return self._response_message_item_snapshot(payload, item)
            if item.get("type") not in {"message", "reasoning"}:
                raise ProtocolTransformError(
                    f"Responses output item {item.get('type')!r} is unsupported",
                    code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )
        if kind in {"response.completed", "response.incomplete"}:
            raw_response = payload.get("response", _MISSING)
            if not isinstance(raw_response, dict):
                raise ProtocolTransformError(
                    "Responses terminal event requires a response object",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                    path="$.response",
                )
            expected_status = (
                "completed" if kind == "response.completed" else "incomplete"
            )
            if raw_response.get("status") != expected_status:
                raise ProtocolTransformError(
                    f"Responses {kind} event requires status {expected_status!r}",
                    code="HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
                    path="$.response.status",
                )
            self._validate_response_snapshot_fields(raw_response)
            if raw_response.get("error") is not None:
                raise ProtocolTransformError(
                    "successful Responses terminal cannot contain an error",
                    code="HUB_UPSTREAM_RESPONSE_INVALID",
                    path="$.response.error",
                )
            chunks = self._response_output_snapshot(raw_response)
            self.stop = _responses_stop_reason(
                raw_response,
                has_tool=self.has_tool,
                refused=self.refused,
            )
            self.fsm.mark_success()
            self.upstream_terminal = True
            return chunks
        if kind in {"response.created", "response.in_progress", "response.queued"}:
            self._validate_response_lifecycle_snapshot(kind, payload)
        if kind in {"response.failed", "error"}:
            if kind == "response.failed" and "response" in payload:
                failed_response = payload["response"]
                if not isinstance(failed_response, dict):
                    raise ProtocolTransformError(
                        "Responses failure event response must be an object",
                        code="HUB_UPSTREAM_RESPONSE_INVALID",
                        path="$.response",
                    )
                self._validate_response_snapshot_fields(failed_response)
                if "status" in failed_response and failed_response["status"] != "failed":
                    raise ProtocolTransformError(
                        "Responses failure status conflicts with its event",
                        code="HUB_SSE_DUPLICATE_CONFLICT",
                        path="$.response.status",
                    )
                if failed_response.get("output") not in (_MISSING, None, []):
                    raise ProtocolTransformError(
                        "Responses failure output snapshot cannot be represented safely",
                        code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                        path="$.response.output",
                    )
            error_values: list[tuple[str, object]] = []
            if "error" in payload:
                error_values.append(("$.error", payload["error"]))
            if kind == "response.failed" and isinstance(
                payload.get("response"), dict
            ) and "error" in payload["response"]:
                error_values.append(
                    ("$.response.error", payload["response"]["error"])
                )
            for error_path, error_value in error_values:
                if error_value is not None and not isinstance(error_value, dict):
                    raise ProtocolTransformError(
                        "Responses failure error detail must be an object or null",
                        code="HUB_UPSTREAM_RESPONSE_INVALID",
                        path=error_path,
                    )
            if any(error_value is not None for _, error_value in error_values):
                self._observe_stream_degradation(
                    "HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED",
                    "upstream error detail has no exact Anthropic streaming carrier",
                )
            self.fsm.mark_error()
            self.fsm.close_stream()
            self.upstream_terminal = True
            self.stopped = True
            return [
                sse_event(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": "upstream request failed",
                        },
                    },
                )
            ]
        if kind.startswith("response.content_part.") or kind.startswith(
            "response.reasoning_summary_part."
        ):
            raise ProtocolTransformError(
                f"Responses SSE event {kind!r} cannot be represented safely",
                code="HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            )
        if kind in {
            "response.completed",
            "response.created",
            "response.in_progress",
            "response.incomplete",
            "response.output_item.added",
            "response.output_item.done",
            "response.queued",
        }:
            return []
        raise ProtocolTransformError(
            f"Responses SSE event {kind!r} is unsupported",
            code="HUB_SSE_UNKNOWN_EVENT",
        )

    def finish(self) -> list[bytes]:
        if self.stopped:
            return []
        if not self.upstream_terminal:
            raise ProtocolTransformError(
                "upstream SSE ended without a terminal event",
                code="HUB_SSE_MISSING_TERMINAL",
            )
        if any(state.open for state in self.response_content_parts.values()) or any(
            state.open for state in self.response_summary_parts.values()
        ):
            raise ProtocolTransformError(
                "Responses stream ended while a structural part remained open",
                code="HUB_SSE_ORDER_VIOLATION",
            )
        if not self.saw_input_usage or not self.saw_output_usage:
            self._observe("HUB_USAGE_PROVENANCE_UNAVAILABLE")
        _validate_cache_creation_consistency(
            self.cache_write,
            self.cache_creation_detail,
        )
        self.stopped = True
        chunks: list[bytes] = [*self._start()]
        for index in sorted(self.open_indices):
            chunks.extend(self._close(index))
        final_usage: dict = {}
        receipt = self.usage_receipt()
        if self.saw_output_usage:
            final_usage["output_tokens"] = receipt.output_tokens
        if self.late_input_usage:
            # Input usage arrived after message_start; message_delta can
            # still carry it truthfully, and the late arrival stays
            # observable via HUB_DEGRADE_LATE_INPUT_USAGE.
            final_usage["input_tokens"] = receipt.input_tokens
        if receipt.cache_read is not None:
            final_usage["cache_read_input_tokens"] = receipt.cache_read
        if receipt.cache_write is not None:
            final_usage["cache_creation_input_tokens"] = receipt.cache_write
        if self.cache_creation_detail is not None:
            final_usage["cache_creation"] = copy.deepcopy(
                self.cache_creation_detail
            )
        if self.server_tool_usage_detail is not None:
            final_usage["server_tool_use"] = copy.deepcopy(
                self.server_tool_usage_detail
            )
        message_delta = {
            "type": "message_delta",
            "delta": {
                "stop_reason": _stop_reason(
                    self.stop,
                    has_tool=self.has_tool,
                    refused=self.refused,
                ),
                "stop_sequence": None,
            },
        }
        if final_usage:
            # An empty object would read as an observed-but-empty report;
            # omit the key when no counter was ever observed.
            message_delta["usage"] = final_usage
        chunks.extend(
            [
                sse_event("message_delta", message_delta),
                sse_event("message_stop", {"type": "message_stop"}),
            ]
        )
        self.fsm.close_stream()
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
        raise ProtocolTransformError(
            "upstream SSE ended without a terminal event",
            code="HUB_SSE_MISSING_TERMINAL",
        )
    yield from bridge.finish()
