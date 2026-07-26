"""Provider storage boundary.

Concrete adapters may read CC Switch or a standalone profile store in later
tracer bullets.  Presentation layers depend only on this protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .domain import ProviderInspection, ProviderRef, StoreCapability


class ProviderStoreError(RuntimeError):
    """Base class for provider-store failures."""


class ProviderNotFoundError(ProviderStoreError):
    """Raised when a stable provider reference is unknown to the store."""


@runtime_checkable
class ProviderStore(Protocol):
    """Minimal read-only store contract for the first tracer bullet."""

    def detect(self) -> StoreCapability:
        """Return store availability and schema capability."""

    def list(self) -> tuple[ProviderRef, ...]:
        """Return stable provider references without provider configuration."""

    def inspect(self, reference: ProviderRef) -> ProviderInspection:
        """Return a redacted inspection for one stable reference."""


__all__ = [
    "ProviderNotFoundError",
    "ProviderStore",
    "ProviderStoreError",
]
