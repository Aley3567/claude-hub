"""Claude-specific projection of generic model-purpose slots.

Only this adapter knows Claude's environment field names.  Callers exchange
the generic :class:`~claude_hub.domain.ModelMapping` DTO, while patching works
on a deep copy so unrelated provider settings survive a round trip.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .domain import ModelMapping


CLAUDE_MODEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("default", "ANTHROPIC_MODEL"),
    ("fast", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("reasoning", "ANTHROPIC_REASONING_MODEL"),
    ("coding", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("long_context", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("fallback", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
)
CLAUDE_MODEL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "fast": ("ANTHROPIC_SMALL_FAST_MODEL",),
}


def _candidate_fields(slot: str, canonical: str) -> tuple[str, ...]:
    return (canonical, *CLAUDE_MODEL_FIELD_ALIASES.get(slot, ()))


class ClaudeModelDocumentError(ValueError):
    """Raised when a Claude settings document cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class UnknownFieldSummary:
    """Irreversible summary of fields outside the model adapter's contract."""

    count: int
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.count, int)
            or isinstance(self.count, bool)
            or self.count < 0
        ):
            raise TypeError("count must be a non-negative int")
        if (
            not isinstance(self.fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None
        ):
            raise ValueError("fingerprint must be a SHA-256 digest")


class ClaudeModelAdapter:
    """Project and patch Claude model fields without exposing their paths."""

    __slots__ = ()

    def project(self, document: Mapping[str, Any]) -> ModelMapping:
        if not isinstance(document, Mapping):
            raise ClaudeModelDocumentError("model document must be an object")
        env = document.get("env")
        if env is None:
            env = {}
        if not isinstance(env, Mapping):
            raise ClaudeModelDocumentError("model environment must be an object")

        projected: dict[str, str | None] = {}
        for slot, field_name in CLAUDE_MODEL_FIELDS:
            value = None
            for candidate in _candidate_fields(slot, field_name):
                candidate_value = env.get(candidate)
                if candidate_value is not None:
                    value = candidate_value
                    break
            if value is not None and not isinstance(value, str):
                raise ClaudeModelDocumentError("model value must be a string")
            projected[slot] = value
        try:
            return ModelMapping(**projected)
        except (TypeError, ValueError) as exc:
            raise ClaudeModelDocumentError(
                "model value is not a public identifier"
            ) from exc

    def patch(
        self,
        document: Mapping[str, Any],
        models: ModelMapping,
    ) -> dict[str, Any]:
        """Return a copy with configured slots patched.

        ``None`` means that a role is absent from the projection and is a
        no-op; it never invents or deletes an optional Claude field.
        """

        if not isinstance(document, Mapping):
            raise ClaudeModelDocumentError("model document must be an object")
        if not isinstance(models, ModelMapping):
            raise TypeError("models must be a ModelMapping")

        patched = deepcopy(dict(document))
        env = patched.get("env")
        configured = models.to_public_dict()
        if env is None and not configured:
            return patched
        if env is None:
            env = {}
            patched["env"] = env
        if not isinstance(env, dict):
            raise ClaudeModelDocumentError("model environment must be an object")

        for slot, field_name in CLAUDE_MODEL_FIELDS:
            value = configured.get(slot)
            if value is not None:
                target = field_name
                if field_name not in env:
                    for alias in CLAUDE_MODEL_FIELD_ALIASES.get(slot, ()):
                        if alias in env:
                            target = alias
                            break
                env[target] = value
        return patched

    # Descriptive aliases for callers that prefer adapter vocabulary.
    extract = project
    apply = patch

    def summarize_unknown(
        self,
        document: Mapping[str, Any],
    ) -> UnknownFieldSummary:
        if not isinstance(document, Mapping):
            raise ClaudeModelDocumentError("model document must be an object")
        env = document.get("env")
        if env is None:
            env = {}
        if not isinstance(env, Mapping):
            raise ClaudeModelDocumentError("model environment must be an object")

        known_env_fields = {
            candidate
            for slot, field_name in CLAUDE_MODEL_FIELDS
            for candidate in _candidate_fields(slot, field_name)
        }
        unknown_env = {
            key: value
            for key, value in env.items()
            if key not in known_env_fields
        }
        unknown_top_level = {
            key: value
            for key, value in document.items()
            if key != "env"
        }
        unknown = {
            "env": unknown_env,
            "topLevel": unknown_top_level,
        }
        try:
            canonical = json.dumps(
                unknown,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise ClaudeModelDocumentError(
                "unknown fields are not valid JSON"
            ) from exc
        return UnknownFieldSummary(
            count=len(unknown_env) + len(unknown_top_level),
            fingerprint=hashlib.sha256(canonical).hexdigest(),
        )


__all__ = [
    "CLAUDE_MODEL_FIELD_ALIASES",
    "CLAUDE_MODEL_FIELDS",
    "ClaudeModelAdapter",
    "ClaudeModelDocumentError",
    "UnknownFieldSummary",
]
