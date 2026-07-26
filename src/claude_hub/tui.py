"""Thin startup-routing seam for a future terminal presentation layer."""

from __future__ import annotations

from .routing import StartupRoute
from .service import ProviderApplicationService


def resolve_tui_startup(
    service: ProviderApplicationService,
    *,
    standalone_exists: bool,
    store_override: str | None = None,
) -> StartupRoute:
    """Resolve the TUI's first screen through the shared application service."""

    if not isinstance(service, ProviderApplicationService):
        raise TypeError("service must be a ProviderApplicationService")
    return service.resolve_startup(
        standalone_exists=standalone_exists,
        store_override=store_override,
    )


__all__ = ["resolve_tui_startup"]
