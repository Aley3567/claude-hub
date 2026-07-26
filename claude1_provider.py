"""Shared third-party Provider capability and isolation policy for claude1.

The launcher and Hub both use this module.  It deliberately contains no
network or database code so capability decisions remain deterministic,
auditable, and independently testable.
"""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from claude1_protocol import API_FORMATS, provider_api_format


class ProviderPolicyError(ValueError):
    """A Provider configuration cannot be used without an unsafe fallback."""


CAPABILITY_VALUES = {
    "protocol": API_FORMATS,
    "tool_search": {"supported", "unsupported", "probe"},
    "count_tokens": {"exact", "estimated", "unsupported"},
    "thinking": {"supported", "unsupported"},
    "reasoning_round_trip": {"supported", "unsupported"},
    "prompt_cache": {"supported", "unsupported", "unknown"},
    "stream_terminal_usage": {"supported", "unsupported"},
    "beta_policy": {"passthrough", "filtered", "mapped"},
    "background_worker_safe": {"verified", "unverified", "unsafe"},
    "model_id_strategy": {"canonical", "mapped", "opaque"},
}

CAPABILITY_FIELDS = (
    "protocol",
    "tool_search",
    "count_tokens",
    "context_window",
    "thinking",
    "reasoning_round_trip",
    "prompt_cache",
    "stream_terminal_usage",
    "beta_policy",
    "background_worker_safe",
    "model_id_strategy",
)
# Every env key whose value Claude Code treats as a model ID; a [1m] suffix on
# any of them must pass the same context-window gate as ANTHROPIC_MODEL.
MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)
CAPABILITY_SOURCES = {
    "user-config",
    "provider-settings",
    "provider-metadata",
    "built-in-rule",
    "safe-default",
    "auto-probe",
    "recent-verification",
}

# Unknown Provider behavior must not be advertised as verified support.
SAFE_DEFAULTS = {
    "tool_search": "unsupported",
    "count_tokens": "estimated",
    "context_window": None,
    "thinking": "unsupported",
    "reasoning_round_trip": "unsupported",
    "prompt_cache": "unknown",
    "stream_terminal_usage": "unsupported",
    "beta_policy": "filtered",
    "background_worker_safe": "unverified",
    "model_id_strategy": "opaque",
}

OFFICIAL_AUTH_STATE_KEYS = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
}
ALLOWED_PROVIDER_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "API_TIMEOUT_MS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "ENABLE_TOOL_SEARCH",
    "MAX_THINKING_TOKENS",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}


@dataclass(frozen=True)
class CapabilityProfile:
    values: dict[str, object]
    sources: dict[str, str]
    verification: dict[str, str]
    beta_allowlist: tuple[str, ...] = ()
    beta_map: tuple[tuple[str, str], ...] = ()
    model_overrides: tuple[
        tuple[
            str,
            tuple[tuple[str, object], ...],
            tuple[tuple[str, str], ...],
            tuple[tuple[str, str], ...],
        ],
        ...,
    ] = ()

    def get(self, field: str, default=None):
        return self.values.get(field, default)

    def source(self, field: str) -> str:
        return self.sources.get(field, "safe-default")

    def status(self, field: str) -> str:
        return self.verification.get(field, "unverified")

    def as_dict(self) -> dict:
        result = dict(self.values)
        result["sources"] = dict(self.sources)
        result["verification"] = dict(self.verification)
        if self.beta_allowlist:
            result["beta_allowlist"] = list(self.beta_allowlist)
        if self.beta_map:
            result["beta_map"] = dict(self.beta_map)
        if self.model_overrides:
            result["models"] = {
                model: {
                    field: {
                        "value": value,
                        "source": dict(sources).get(field, "user-config"),
                        "status": dict(verification).get(
                            field,
                            "unverified",
                        ),
                    }
                    for field, value in values
                }
                for model, values, sources, verification in self.model_overrides
            }
        return result

    def for_model(self, model: str) -> "CapabilityProfile":
        """Overlay an exact opaque model ID without guessing from its name."""
        for (
            configured_model,
            raw_values,
            raw_sources,
            raw_verification,
        ) in self.model_overrides:
            if configured_model != model:
                continue
            return CapabilityProfile(
                values={**self.values, **dict(raw_values)},
                sources={**self.sources, **dict(raw_sources)},
                verification={
                    **self.verification,
                    **dict(raw_verification),
                },
                beta_allowlist=self.beta_allowlist,
                beta_map=self.beta_map,
                model_overrides=self.model_overrides,
            )
        return self


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _profile_layer(container: object) -> dict:
    raw = _mapping(container)
    nested = _mapping(raw.get("claude1"))
    candidate = nested.get("capabilities")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = raw.get("claude1_capabilities")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = raw.get("claude1Capabilities")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = raw.get("capabilities")
    return dict(candidate) if isinstance(candidate, Mapping) else {}


def _verification_layer(container: object) -> dict:
    raw = _mapping(container)
    nested = _mapping(raw.get("claude1"))
    candidate = nested.get("capability_verification")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = raw.get("capability_verification")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    candidate = raw.get("verification")
    return dict(candidate) if isinstance(candidate, Mapping) else {}


def _normalize_context_window(value: object) -> int | None:
    if value in (None, "unknown"):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderPolicyError(
            "capabilities.context_window must be a positive integer or unknown"
        )
    return value


def _validate_capability(field: str, value: object) -> object:
    if field == "context_window":
        return _normalize_context_window(value)
    allowed = CAPABILITY_VALUES[field]
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ProviderPolicyError(
            f"capabilities.{field} must be one of: {choices}"
        )
    return value


def _status_for(field: str, source: str, raw_status: object) -> str:
    if isinstance(raw_status, str) and raw_status in {
        "verified",
        "declared",
        "unverified",
        "unsafe",
    }:
        return raw_status
    if source in {"user-config", "provider-settings", "provider-metadata"}:
        return "declared"
    return "unverified"


def resolve_capability_profile(
    *,
    meta: object = None,
    settings: object = None,
    provider_type: object = None,
    override: object = None,
    protocol_override: object = None,
) -> CapabilityProfile:
    """Resolve a Provider profile with per-field provenance.

    Precedence is explicit user/channel override, Provider settings, Provider
    metadata, then conservative defaults.  Verification metadata never changes
    a capability value; it only describes how much confidence callers may place
    in the selected declaration.
    """

    meta_map = _mapping(meta)
    settings_map = _mapping(settings)
    explicit = _mapping(override)
    protocol = provider_api_format(
        meta=meta_map,
        settings=settings_map,
        provider_type=provider_type,
        override=protocol_override,
    )
    values: dict[str, object] = {"protocol": protocol, **SAFE_DEFAULTS}
    sources = {field: "safe-default" for field in CAPABILITY_FIELDS}
    settings_api_format = settings_map.get("api_format")
    meta_api_format = meta_map.get("apiFormat")
    if isinstance(protocol_override, str) and protocol_override in API_FORMATS:
        sources["protocol"] = "user-config"
    elif (
        provider_type == "codex_oauth"
        or meta_map.get("providerType") == "codex_oauth"
        or (
            isinstance(meta_api_format, str)
            and meta_api_format in API_FORMATS
        )
    ):
        sources["protocol"] = "provider-metadata"
    elif (
        isinstance(settings_api_format, str)
        and settings_api_format in API_FORMATS
    ) or (
        (legacy := settings_map.get("openrouter_compat_mode")) is True
        or legacy == 1
        or (
            isinstance(legacy, str)
            and legacy.strip().casefold() in {"1", "true"}
        )
    ):
        sources["protocol"] = "provider-settings"
    else:
        sources["protocol"] = "built-in-rule"

    verification_raw: dict[str, object] = {}
    layers = (
        (_profile_layer(meta_map), "provider-metadata"),
        (_profile_layer(settings_map), "provider-settings"),
        (explicit, "user-config"),
    )
    for layer, source in layers:
        for field in CAPABILITY_FIELDS:
            if field not in layer:
                continue
            raw_value = layer[field]
            if isinstance(raw_value, Mapping):
                raw_entry = dict(raw_value)
                raw_value = raw_entry.get("value")
                if "status" in raw_entry:
                    verification_raw[field] = raw_entry["status"]
            values[field] = _validate_capability(field, raw_value)
            sources[field] = source

    for container in (meta_map, settings_map, explicit):
        verification_raw.update(_verification_layer(container))
    declared_sources = explicit.get("sources")
    if isinstance(declared_sources, Mapping):
        for field, source in declared_sources.items():
            if (
                field in CAPABILITY_FIELDS
                and isinstance(source, str)
                and source in CAPABILITY_SOURCES
            ):
                sources[field] = source

    # protocol_override describes the wire format and is authoritative even if a
    # stale nested profile also contains protocol.
    if isinstance(protocol_override, str) and protocol_override in API_FORMATS:
        values["protocol"] = protocol_override
        sources["protocol"] = "user-config"

    verification = {
        field: _status_for(field, sources[field], verification_raw.get(field))
        for field in CAPABILITY_FIELDS
    }

    policy_layer = {}
    for layer, _source in layers:
        policy_layer.update(layer)
    allowlist = policy_layer.get("beta_allowlist", ())
    if not isinstance(allowlist, list) or any(
        not isinstance(item, str) or not item.strip() for item in allowlist
    ):
        if allowlist not in (None, ()):
            raise ProviderPolicyError(
                "capabilities.beta_allowlist must be a list of non-empty strings"
            )
        allowlist = []
    beta_map = policy_layer.get("beta_map", {})
    if not isinstance(beta_map, Mapping) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in beta_map.items()
    ):
        raise ProviderPolicyError(
            "capabilities.beta_map must map non-empty strings to non-empty strings"
        )

    if values["beta_policy"] == "mapped" and not beta_map:
        raise ProviderPolicyError(
            "capabilities.beta_map is required when beta_policy is mapped"
        )
    if (
        values["protocol"] != "anthropic"
        and values["count_tokens"] == "exact"
    ):
        raise ProviderPolicyError(
            "count_tokens=exact is only valid for anthropic protocol"
        )
    model_values: dict[str, dict[str, object]] = {}
    model_sources: dict[str, dict[str, str]] = {}
    model_verification: dict[str, dict[str, str]] = {}
    for layer, source in layers:
        models = layer.get("models")
        if models is None:
            continue
        if not isinstance(models, Mapping):
            raise ProviderPolicyError(
                "capabilities.models must be an object"
            )
        for model, raw_profile in models.items():
            if (
                not isinstance(model, str)
                or not model.strip()
                or not isinstance(raw_profile, Mapping)
            ):
                raise ProviderPolicyError(
                    "capabilities.models must map model IDs to objects"
                )
            target_values = model_values.setdefault(model, {})
            target_sources = model_sources.setdefault(model, {})
            target_verification = model_verification.setdefault(model, {})
            for field, raw_value in raw_profile.items():
                if field not in CAPABILITY_FIELDS or field == "protocol":
                    continue
                raw_status = None
                raw_source = source
                if isinstance(raw_value, Mapping):
                    entry = dict(raw_value)
                    raw_status = entry.get("status")
                    candidate_source = entry.get("source")
                    if (
                        isinstance(candidate_source, str)
                        and candidate_source in CAPABILITY_SOURCES
                    ):
                        raw_source = candidate_source
                    raw_value = entry.get("value")
                target_values[field] = _validate_capability(field, raw_value)
                target_sources[field] = raw_source
                target_verification[field] = _status_for(
                    field,
                    raw_source,
                    raw_status,
                )
            if (
                values["protocol"] != "anthropic"
                and target_values.get("count_tokens") == "exact"
            ):
                raise ProviderPolicyError(
                    "model count_tokens=exact is only valid for "
                    "anthropic protocol"
                )
            if (
                target_values.get("beta_policy") == "mapped"
                and not beta_map
            ):
                raise ProviderPolicyError(
                    "model beta_policy=mapped requires capabilities.beta_map"
                )
    return CapabilityProfile(
        values=values,
        sources=sources,
        verification=verification,
        beta_allowlist=tuple(item.strip() for item in allowlist),
        beta_map=tuple(
            (str(key).strip(), str(value).strip())
            for key, value in beta_map.items()
        ),
        model_overrides=tuple(
            (
                model,
                tuple(model_values[model].items()),
                tuple(model_sources[model].items()),
                tuple(model_verification[model].items()),
            )
            for model in model_values
        ),
    )


def configured_credential(env: Mapping[str, object]) -> tuple[str, str] | None:
    """Return one explicitly configured Provider credential, never ambient auth."""
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        value = env.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    return None


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderPolicyError(
            "third-party Provider must define a non-empty ANTHROPIC_BASE_URL"
        )
    base_url = value.strip()
    try:
        parsed = urlparse(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ProviderPolicyError("Provider base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ProviderPolicyError("Provider base URL is invalid")
    # Match the Hub's loopback-or-HTTPS policy: credentials must never travel
    # over cleartext HTTP to a remote host.  Never echo the URL itself because
    # channel hosts are private.
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ProviderPolicyError(
            "Provider base URL uses cleartext http to a non-loopback host; "
            "only https or loopback http is allowed"
        )
    return base_url


def prepare_provider_settings(
    settings: object,
    profile: CapabilityProfile,
    *,
    require_base_url: bool = True,
) -> dict:
    """Return detached settings that cannot fall back to official credentials."""
    if not isinstance(settings, Mapping):
        raise ProviderPolicyError("Provider settings must be a JSON object")
    prepared = copy.deepcopy(dict(settings))
    prepared.pop("claude1_capabilities", None)
    prepared.pop("capabilities", None)
    prepared.pop("capability_verification", None)
    prepared.pop("api_format", None)
    prepared.pop("openrouter_compat_mode", None)
    internal = prepared.get("claude1")
    if isinstance(internal, dict):
        internal.pop("capabilities", None)
        internal.pop("capability_verification", None)
        if not internal:
            prepared.pop("claude1", None)
    raw_env = prepared.get("env")
    if not isinstance(raw_env, Mapping):
        raw_env = {}
    env = {
        str(key): str(value)
        for key, value in raw_env.items()
        if isinstance(key, str)
        and value is not None
        and (
            key.startswith("ANTHROPIC_")
            or key in ALLOWED_PROVIDER_ENV_KEYS
        )
    }
    if require_base_url or env.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = _validate_base_url(
            env.get("ANTHROPIC_BASE_URL")
        )
        if configured_credential(env) is None:
            raise ProviderPolicyError(
                "third-party Provider has no explicit ANTHROPIC_AUTH_TOKEN "
                "or ANTHROPIC_API_KEY; refusing official credential fallback"
            )
        auth_token = env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        api_key = env.get("ANTHROPIC_API_KEY", "").strip()
        if auth_token and api_key and auth_token != api_key:
            raise ProviderPolicyError(
                "third-party Provider config contains two different "
                "credentials; choose one explicit credential source"
            )

    for key in OFFICIAL_AUTH_STATE_KEYS:
        env.pop(key, None)
    prepared.pop("forceLoginMethod", None)

    # Claude Code applies these to resume, compact, subagents and background
    # workers spawned inside this session.  Internal requests keep the selected
    # URL/token while tool subprocesses do not inherit credentials.
    env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    tool_search = profile.get("tool_search")
    env["ENABLE_TOOL_SEARCH"] = (
        "true" if tool_search == "supported" else "false"
    )
    context_window = profile.get("context_window")
    has_1m_window = (
        isinstance(context_window, int) and context_window >= 1_000_000
    )
    for model_key in MODEL_ENV_KEYS:
        configured_model = env.get(model_key, "")
        if (
            isinstance(configured_model, str)
            and configured_model.casefold().endswith("[1m]")
            and not has_1m_window
        ):
            raise ProviderPolicyError(
                f"{model_key} requests [1m] but the Provider/model "
                "context window is unknown or smaller than 1M"
            )
    # An explicit user-provided CLAUDE_CODE_DISABLE_1M_CONTEXT always wins;
    # only manage the flag when the Provider config did not set it.
    if "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in env and not has_1m_window:
        env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
    if profile.get("thinking") != "supported":
        env["MAX_THINKING_TOKENS"] = "0"
    if profile.get("background_worker_safe") == "unsafe":
        raise ProviderPolicyError(
            "Provider capability profile marks background workers unsafe"
        )

    if profile.get("beta_policy") != "passthrough":
        env.pop("ANTHROPIC_BETAS", None)
    prepared["env"] = env
    return prepared


def capability_summary(profile: CapabilityProfile) -> list[str]:
    """Return redaction-safe, stable doctor lines for one profile."""
    lines = []
    for field in CAPABILITY_FIELDS:
        value = profile.get(field)
        rendered = "unknown" if field == "context_window" and value is None else value
        lines.append(
            f"{field}={rendered} "
            f"[{profile.source(field)}/{profile.status(field)}]"
        )
    if profile.model_overrides:
        lines.append(
            f"model_overrides={len(profile.model_overrides)} "
            "[exact IDs redacted]"
        )
    return lines
