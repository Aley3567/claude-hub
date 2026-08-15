"""Usage evidence normalization for the Anthropic protocol translator.

The public interface is ``UsageReceipt``.  The underscored helpers form an
internal seam used by complete-response and stream adapters so both paths
apply the same counter, cache, and provenance rules.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from claude1_protocol_types import ProtocolTransformError


_MISSING = object()

_COMMON_UPSTREAM_USAGE_FIELDS = {
    "total_tokens",
    "cache_read_input_tokens",
    "cache_read_tokens",
    "cache_creation_input_tokens",
    "cache_creation_tokens",
    "cache_creation",
    "server_tool_use",
}

_UPSTREAM_USAGE_FIELDS = {
    "openai_chat": _COMMON_UPSTREAM_USAGE_FIELDS
    | {
        "prompt_tokens",
        "completion_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
    },
    "openai_responses": _COMMON_UPSTREAM_USAGE_FIELDS
    | {
        "input_tokens",
        "output_tokens",
        "input_tokens_details",
        "output_tokens_details",
    },
}

_UPSTREAM_USAGE_DETAIL_FIELDS = {
    "openai_chat": {
        "prompt_tokens_details": {"cached_tokens", "audio_tokens"},
        "completion_tokens_details": {
            "accepted_prediction_tokens",
            "audio_tokens",
            "reasoning_tokens",
            "rejected_prediction_tokens",
        },
    },
    "openai_responses": {
        "input_tokens_details": {"cached_tokens"},
        "output_tokens_details": {"reasoning_tokens"},
    },
}


def _token_count(value: object, default: int = 0) -> int:
    """Accept protocol-compatible integer strings without trusting others."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _validate_upstream_usage_fields(
    raw_usage: dict,
    api_format: str,
) -> tuple[str, ...]:
    degraded_paths: list[str] = []
    allowed_fields = _UPSTREAM_USAGE_FIELDS[api_format]
    unknown_fields = set(raw_usage) - allowed_fields
    # Extra statistics from a compatible upstream are harmless: the adapter
    # copies only registered counters and never fabricates values, so unknown
    # usage fields are dropped with a warning instead of aborting a downstream
    # stream that may already have started.
    degraded_paths.extend(
        f"$.usage.{field_name}" for field_name in sorted(unknown_fields)
    )

    for detail_key, detail_fields in _UPSTREAM_USAGE_DETAIL_FIELDS[
        api_format
    ].items():
        if detail_key not in raw_usage:
            continue
        detail = raw_usage[detail_key]
        if not isinstance(detail, dict):
            raise ProtocolTransformError(
                f"upstream usage field {detail_key!r} must be an object",
                code="HUB_UPSTREAM_USAGE_INVALID",
                path=f"$.usage.{detail_key}",
            )
        # Same policy as unknown top-level usage fields: only malformed values
        # of registered counters stay fatal.
        degraded_paths.extend(
            f"$.usage.{detail_key}.{field_name}"
            for field_name in sorted(set(detail) - detail_fields)
        )
        for field_name in sorted(set(detail) & detail_fields):
            counter = detail[field_name]
            if _token_count(counter, -1) < 0:
                raise ProtocolTransformError(
                    f"upstream usage counter {detail_key}.{field_name} is invalid",
                    code="HUB_UPSTREAM_USAGE_INVALID",
                    path=f"$.usage.{detail_key}.{field_name}",
                )
            if field_name != "cached_tokens":
                degraded_paths.append(f"$.usage.{detail_key}.{field_name}")
    return tuple(degraded_paths)


def _upstream_usage_total(
    raw_usage: dict,
    *,
    input_key: str,
    output_key: str,
) -> object:
    if "total_tokens" not in raw_usage:
        return _MISSING
    total_tokens = _token_count(raw_usage["total_tokens"], -1)
    if total_tokens < 0:
        raise ProtocolTransformError(
            "upstream usage counter 'total_tokens' is invalid",
            code="HUB_UPSTREAM_USAGE_INVALID",
            path="$.usage.total_tokens",
        )
    if input_key in raw_usage and output_key in raw_usage:
        base_values = (
            _token_count(raw_usage[input_key], -1),
            _token_count(raw_usage[output_key], -1),
        )
        if min(base_values) >= 0 and total_tokens != sum(base_values):
            raise ProtocolTransformError(
                "upstream total_tokens conflicts with its base usage counters",
                code="HUB_UPSTREAM_USAGE_INVALID",
                path="$.usage.total_tokens",
            )
    return total_tokens


@dataclass(frozen=True)
class UsageReceipt:
    """Canonical usage evidence shared by complete and stream conversions.

    One entry point takes whatever usage shape the upstream reported and owns
    every interpretation rule: which cache-read carriers prove an inclusive
    base, which are ambiguous compat fields, and which counters were never
    observed. Callers export either shape below and never fabricate a zero
    for an unobserved counter.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None

    @classmethod
    def from_upstream(
        cls,
        raw_usage: object,
        *,
        input_key: str = "input_tokens",
        output_key: str = "output_tokens",
    ) -> "UsageReceipt":
        if raw_usage is None:
            raw_usage = {}
        if not isinstance(raw_usage, dict):
            raise ProtocolTransformError(
                "upstream usage must be an object",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        values: dict[str, int | None] = {}
        for field, key in (
            ("input_tokens", input_key),
            ("output_tokens", output_key),
        ):
            value = raw_usage.get(key, _MISSING)
            if value is _MISSING:
                values[field] = None
                continue
            parsed = _token_count(value, -1)
            if parsed < 0:
                raise ProtocolTransformError(
                    f"upstream usage counter {key!r} is invalid",
                    code="HUB_UPSTREAM_USAGE_INVALID",
                )
            values[field] = parsed
        cache_read = _cache_read(raw_usage)
        cache_write = _cache_write(raw_usage)
        return cls.from_evidence(
            input_tokens=values["input_tokens"],
            output_tokens=values["output_tokens"],
            cache_read=cache_read if isinstance(cache_read, int) else None,
            cache_write=cache_write if isinstance(cache_write, int) else None,
            nested_cache_evidence=_has_nested_cache_carrier(raw_usage),
            official_cache_read=_has_official_cache_read(raw_usage),
        )

    @classmethod
    def from_evidence(
        cls,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read: int | None,
        cache_write: int | None,
        nested_cache_evidence: bool,
        official_cache_read: bool,
    ) -> "UsageReceipt":
        """Apply the shared cache-read evidence rule to aggregated counters.

        A nested carrier proves that the base input counter includes cached
        tokens; the Anthropic-native key proves exclusive semantics; a bare
        top-level compatibility field proves neither. When nested and native
        carriers coexist, their values must agree before the nested reading
        can be subtracted from the inclusive base.
        """
        if nested_cache_evidence:
            if isinstance(cache_read, int) and input_tokens is not None:
                adjusted = input_tokens - cache_read
                if adjusted < 0:
                    raise ProtocolTransformError(
                        "upstream input usage is smaller than its cache-read counter",
                        code="HUB_UPSTREAM_USAGE_INVALID",
                    )
                input_tokens = adjusted
        elif not official_cache_read:
            cache_read = None
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
        )

    @property
    def source(self) -> str:
        observed = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read,
            self.cache_write,
        )
        return "upstream" if any(v is not None for v in observed) else "unavailable"

    def as_anthropic(self, *, schema_complete: bool = False) -> dict:
        """Export the Anthropic usage shape.

        Complete responses require the base counters, so an unobserved value
        is represented as 0 while its provenance degradation remains in the
        conversion plan. Stream events omit unobserved counters because no
        schema requires them mid-stream.
        """
        usage: dict = {}
        if self.input_tokens is not None:
            usage["input_tokens"] = self.input_tokens
        elif schema_complete:
            usage["input_tokens"] = 0
        if self.output_tokens is not None:
            usage["output_tokens"] = self.output_tokens
        elif schema_complete:
            usage["output_tokens"] = 0
        if self.cache_read is not None:
            usage["cache_read_input_tokens"] = self.cache_read
        if self.cache_write is not None:
            usage["cache_creation_input_tokens"] = self.cache_write
        return usage


def _usage_with_details(
    receipt: UsageReceipt,
    cache_creation_detail: object,
    server_tool_use: object,
) -> dict:
    usage = receipt.as_anthropic(schema_complete=True)
    if cache_creation_detail is not _MISSING:
        usage["cache_creation"] = copy.deepcopy(cache_creation_detail)
    if server_tool_use is not _MISSING:
        usage["server_tool_use"] = copy.deepcopy(server_tool_use)
    return usage


def _coalesce_usage_counters(
    carriers: list[tuple[str, object]],
    *,
    label: str,
) -> object:
    if not carriers:
        return _MISSING
    normalized: list[int] = []
    for path, value in carriers:
        parsed = _token_count(value, -1)
        if parsed < 0:
            raise ProtocolTransformError(
                f"upstream {label} usage counter {path!r} is invalid",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        normalized.append(parsed)
    if len(set(normalized)) > 1:
        raise ProtocolTransformError(
            f"upstream {label} usage counters conflict",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )
    return normalized[0]


def _has_nested_cache_carrier(raw_usage: dict) -> bool:
    """True only when a standard nested cache-read carrier is present."""
    return any(
        isinstance(raw_usage.get(key), dict) and "cached_tokens" in raw_usage[key]
        for key in ("prompt_tokens_details", "input_tokens_details")
    )


def _has_official_cache_read(raw_usage: dict) -> bool:
    """True when the Anthropic-native exclusive cache-read key is present."""
    return "cache_read_input_tokens" in raw_usage


def _cache_read(raw_usage: dict) -> object:
    """Validate/coalesce cache-read carriers without fabricating a value."""
    carriers: list[tuple[str, object]] = []
    for key in ("prompt_tokens_details", "input_tokens_details"):
        if key not in raw_usage:
            continue
        details = raw_usage[key]
        if not isinstance(details, dict):
            raise ProtocolTransformError(
                f"upstream cache-read usage field {key!r} must be an object",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        if "cached_tokens" in details:
            carriers.append((f"{key}.cached_tokens", details["cached_tokens"]))
    for key in ("cache_read_input_tokens", "cache_read_tokens"):
        if key in raw_usage:
            carriers.append((key, raw_usage[key]))
    return _coalesce_usage_counters(carriers, label="cache-read")


def _cache_write(raw_usage: dict) -> object:
    carriers = [
        (key, raw_usage[key])
        for key in ("cache_creation_input_tokens", "cache_creation_tokens")
        if key in raw_usage
    ]
    return _coalesce_usage_counters(carriers, label="cache-write")


def _usage_detail(
    raw_usage: dict,
    key: str,
    allowed_fields: set[str],
) -> object:
    if key not in raw_usage:
        return _MISSING
    value = raw_usage[key]
    if not isinstance(value, dict) or set(value) - allowed_fields:
        raise ProtocolTransformError(
            f"upstream usage field {key!r} has an unsupported shape",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )
    normalized: dict[str, int] = {}
    for field_name, counter in value.items():
        parsed = _token_count(counter, -1)
        if parsed < 0:
            raise ProtocolTransformError(
                f"upstream usage counter {key}.{field_name} is invalid",
                code="HUB_UPSTREAM_USAGE_INVALID",
            )
        normalized[field_name] = parsed
    return normalized


def _cache_creation_detail(raw_usage: dict) -> object:
    return _usage_detail(
        raw_usage,
        "cache_creation",
        {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"},
    )


def _validate_cache_creation_consistency(
    total: object,
    detail: object,
) -> None:
    if detail is _MISSING or detail is None:
        return
    if total is _MISSING or total is None:
        raise ProtocolTransformError(
            "upstream cache-creation detail arrived without its total counter",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )
    if not isinstance(total, int) or not isinstance(detail, dict):
        raise ProtocolTransformError(
            "upstream cache-creation usage has an invalid shape",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )
    if total != sum(detail.values()):
        raise ProtocolTransformError(
            "upstream cache-creation total conflicts with its split detail",
            code="HUB_UPSTREAM_USAGE_INVALID",
        )


def _server_tool_usage(raw_usage: dict) -> object:
    return _usage_detail(
        raw_usage,
        "server_tool_use",
        {"web_search_requests", "web_fetch_requests", "code_execution_requests"},
    )
