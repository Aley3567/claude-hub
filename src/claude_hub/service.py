"""Application services shared by CLI, TUI, and GUI adapters."""

from __future__ import annotations

from .domain import ProviderInspection, ProviderRef, StoreCapability
from .store import ProviderStore


class ProviderApplicationService:
    """Minimal detect/list/inspect use cases over an injected store."""

    __slots__ = ("_store",)

    def __init__(self, store: ProviderStore) -> None:
        if not isinstance(store, ProviderStore):
            raise TypeError("store must implement ProviderStore")
        self._store = store

    def detect(self) -> StoreCapability:
        capability = self._store.detect()
        if not isinstance(capability, StoreCapability):
            raise TypeError("ProviderStore.detect() returned an invalid capability")
        return capability

    def list(self) -> tuple[ProviderRef, ...]:
        references = tuple(self._store.list())
        if any(not isinstance(reference, ProviderRef) for reference in references):
            raise TypeError("ProviderStore.list() returned an invalid reference")
        return references

    def inspect(self, reference: ProviderRef) -> ProviderInspection:
        if not isinstance(reference, ProviderRef):
            raise TypeError("reference must be a ProviderRef")
        inspection = self._store.inspect(reference)
        if not isinstance(inspection, ProviderInspection):
            raise TypeError("ProviderStore.inspect() returned an invalid inspection")
        if inspection.reference != reference:
            raise ValueError("ProviderStore.inspect() returned a different reference")
        return inspection

    def list_providers(self) -> tuple[ProviderRef, ...]:
        """Descriptive alias for presentation adapters."""

        return self.list()

    def inspect_provider(self, reference: ProviderRef) -> ProviderInspection:
        """Descriptive alias for presentation adapters."""

        return self.inspect(reference)


__all__ = ["ProviderApplicationService"]
