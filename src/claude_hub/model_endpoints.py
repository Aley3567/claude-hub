"""Deterministic model-discovery request descriptions.

The adapter only builds same-origin GET descriptions.  It never sends a
request, reads credentials, or interprets a response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .network_policy import (
    NetworkPolicyError,
    NormalizedTarget,
    normalize_provider_url,
)


MAX_MODEL_ENDPOINT_CANDIDATES = 2


class ModelEndpointReasonCode(str, Enum):
    """Stable rejection reasons owned by the endpoint adapter."""

    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    INVALID_BASE_URL = "invalid_base_url"
    BASE_QUERY_FORBIDDEN = "base_query_forbidden"
    CROSS_ORIGIN_CANDIDATE = "cross_origin_candidate"


class ModelEndpointError(ValueError):
    """Endpoint generation failed without echoing the input URL."""

    def __init__(
        self,
        reason_code: ModelEndpointReasonCode | str,
        *,
        cause_code: str | None = None,
    ) -> None:
        code = (
            reason_code.value
            if isinstance(reason_code, ModelEndpointReasonCode)
            else str(reason_code)
        )
        self.reason_code = code
        self.cause_code = cause_code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ModelEndpointRequest:
    """A credential-free description for one possible model-list request."""

    adapter: str
    method: str
    origin: str
    url: str
    display_url: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter={self.adapter!r}, "
            f"method={self.method!r}, display_url={self.display_url!r})"
        )


_PROTOCOL_ADAPTERS = {
    "openai": "openai",
    "openai_chat": "openai",
    "openai_responses": "openai",
    "anthropic": "anthropic-compatible",
    "anthropic-compatible": "anthropic-compatible",
    "anthropic_compatible": "anthropic-compatible",
}
_VERSION_SEGMENT_RE = re.compile(r"^v[0-9]+[a-z0-9._-]*$", re.IGNORECASE)


def _reject(
    reason_code: ModelEndpointReasonCode,
    *,
    cause_code: str | None = None,
) -> None:
    raise ModelEndpointError(reason_code, cause_code=cause_code)


def _coerce_base(base_url: str | NormalizedTarget) -> NormalizedTarget:
    if isinstance(base_url, NormalizedTarget):
        try:
            canonical = normalize_provider_url(base_url.request_url)
        except NetworkPolicyError as exc:
            _reject(
                ModelEndpointReasonCode.INVALID_BASE_URL,
                cause_code=exc.reason_code,
            )
        if canonical != base_url:
            _reject(ModelEndpointReasonCode.INVALID_BASE_URL)
        return canonical
    try:
        return normalize_provider_url(base_url)
    except NetworkPolicyError as exc:
        _reject(
            ModelEndpointReasonCode.INVALID_BASE_URL,
            cause_code=exc.reason_code,
        )


def _join_path(base_path: str, *segments: str) -> str:
    prefix = base_path.rstrip("/")
    if not prefix:
        prefix = ""
    suffix = "/".join(segment.strip("/") for segment in segments)
    return f"{prefix}/{suffix}" if suffix else (prefix or "/")


def _candidate_paths(base_path: str) -> tuple[str, ...]:
    stripped = base_path.rstrip("/")
    last_segment = stripped.rsplit("/", 1)[-1] if stripped else ""
    if last_segment.casefold() == "models":
        return (stripped or "/models",)
    if _VERSION_SEGMENT_RE.fullmatch(last_segment):
        return (_join_path(stripped, "models"),)
    return (
        _join_path(stripped, "v1", "models"),
        _join_path(stripped, "models"),
    )


def model_endpoint_candidates(
    base_url: str | NormalizedTarget,
    protocol: str,
) -> tuple[ModelEndpointRequest, ...]:
    """Return at most two ordered, same-origin model-list GET descriptions."""

    if not isinstance(protocol, str):
        _reject(ModelEndpointReasonCode.UNSUPPORTED_PROTOCOL)
    adapter = _PROTOCOL_ADAPTERS.get(protocol.strip().casefold())
    if adapter is None:
        _reject(ModelEndpointReasonCode.UNSUPPORTED_PROTOCOL)

    base = _coerce_base(base_url)
    if base.query:
        _reject(ModelEndpointReasonCode.BASE_QUERY_FORBIDDEN)

    requests: list[ModelEndpointRequest] = []
    for path in _candidate_paths(base.path):
        try:
            candidate = normalize_provider_url(f"{base.origin}{path}")
        except NetworkPolicyError as exc:
            _reject(
                ModelEndpointReasonCode.INVALID_BASE_URL,
                cause_code=exc.reason_code,
            )
        if candidate.origin != base.origin:
            _reject(ModelEndpointReasonCode.CROSS_ORIGIN_CANDIDATE)
        requests.append(
            ModelEndpointRequest(
                adapter=adapter,
                method="GET",
                origin=base.origin,
                url=candidate.request_url,
                display_url=candidate.display_url,
            )
        )

    deduplicated = tuple(dict.fromkeys(requests))
    if len(deduplicated) > MAX_MODEL_ENDPOINT_CANDIDATES:
        raise AssertionError("model endpoint candidate limit exceeded")
    return deduplicated


generate_model_endpoints = model_endpoint_candidates


__all__ = [
    "MAX_MODEL_ENDPOINT_CANDIDATES",
    "ModelEndpointError",
    "ModelEndpointReasonCode",
    "ModelEndpointRequest",
    "generate_model_endpoints",
    "model_endpoint_candidates",
]
