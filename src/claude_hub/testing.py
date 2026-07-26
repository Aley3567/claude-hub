"""Deterministic in-memory test doubles for the core provider contract."""

from __future__ import annotations

from collections.abc import Iterable

from .domain import ProviderInspection, ProviderRef, StoreCapability
from .store import ProviderNotFoundError


class InMemoryProviderStore:
    """Read-only fake with no HOME, network, filesystem, or database access."""

    __slots__ = ("_capability", "_providers", "_inspections")

    def __init__(
        self,
        *,
        capability: StoreCapability = StoreCapability.ABSENT,
        providers: Iterable[ProviderRef] = (),
        inspections: Iterable[ProviderInspection] = (),
    ) -> None:
        if not isinstance(capability, StoreCapability):
            raise TypeError("capability must be a StoreCapability")

        provider_tuple = tuple(providers)
        if any(not isinstance(provider, ProviderRef) for provider in provider_tuple):
            raise TypeError("providers must contain only ProviderRef values")
        if len(set(provider_tuple)) != len(provider_tuple):
            raise ValueError("providers must not contain duplicates")

        inspection_map: dict[ProviderRef, ProviderInspection] = {}
        for inspection in inspections:
            if not isinstance(inspection, ProviderInspection):
                raise TypeError(
                    "inspections must contain only ProviderInspection values"
                )
            if inspection.reference in inspection_map:
                raise ValueError("inspections must not contain duplicate references")
            inspection_map[inspection.reference] = inspection

        if not set(inspection_map).issubset(provider_tuple):
            raise ValueError("every inspection must reference a listed provider")

        self._capability = capability
        self._providers = provider_tuple
        self._inspections = inspection_map

    def detect(self) -> StoreCapability:
        return self._capability

    def list(self) -> tuple[ProviderRef, ...]:
        return self._providers

    def inspect(self, reference: ProviderRef) -> ProviderInspection:
        try:
            return self._inspections[reference]
        except KeyError:
            raise ProviderNotFoundError("provider reference was not found") from None


FakeProviderStore = InMemoryProviderStore


__all__ = ["FakeProviderStore", "InMemoryProviderStore"]
