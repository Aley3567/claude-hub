"""Concrete terminal presentation adapters over shared application services."""

from __future__ import annotations

import hmac
import sys
from collections.abc import Callable
from typing import TextIO

from .approval import (
    ApprovalHandle,
    ApprovalRegistry,
    HumanConfirmationError,
    _is_exact_change_plan,
)
from .change_plan import ChangePlan, tui_preview
from .routing import StartupRoute
from .service import ProviderApplicationService


APPROVAL_CONFIRMATION_PHRASE = "approve this plan once"
_MAX_CONFIRMATION_INPUT = 128


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


def request_tui_approval(
    plan: ChangePlan,
    registry: ApprovalRegistry,
    *,
    show_preview: Callable[[str], object],
    confirm: Callable[[], bool | None],
) -> ApprovalHandle | None:
    """Display the complete redacted plan before requesting human consent."""

    if not _is_exact_change_plan(plan):
        raise TypeError("plan must be a ChangePlan")
    if not isinstance(registry, ApprovalRegistry):
        raise TypeError("registry must be an ApprovalRegistry")
    if not callable(show_preview):
        raise TypeError("show_preview must be callable")
    if not callable(confirm):
        raise TypeError("confirm must be callable")

    preview = tui_preview(plan)
    try:
        show_preview(preview)
    except Exception:
        raise HumanConfirmationError(
            "change plan preview failed"
        ) from None
    try:
        decision = confirm()
    except Exception:
        raise HumanConfirmationError(
            "human confirmation failed"
        ) from None

    if decision is True:
        return registry._grant_after_human_confirmation(plan)
    if decision is False or decision is None:
        return None
    raise HumanConfirmationError(
        "human confirmation returned an invalid decision"
    )


def request_terminal_approval(
    plan: ChangePlan,
    registry: ApprovalRegistry,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> ApprovalHandle | None:
    """Run the real TTY preview-and-confirm adapter in the current process."""

    terminal_input = sys.stdin if input_stream is None else input_stream
    terminal_output = sys.stdout if output_stream is None else output_stream
    try:
        if not terminal_input.isatty() or not terminal_output.isatty():
            raise HumanConfirmationError(
                "interactive terminal is unavailable"
            )
    except HumanConfirmationError:
        raise
    except Exception:
        raise HumanConfirmationError(
            "interactive terminal is unavailable"
        ) from None

    def show_preview(preview: str) -> None:
        terminal_output.write(preview)
        terminal_output.write("\n")
        terminal_output.write(
            "Type exactly "
            f'"{APPROVAL_CONFIRMATION_PHRASE}" '
            "and press Enter to approve once.\n> "
        )
        terminal_output.flush()

    def confirm() -> bool:
        response = terminal_input.readline(_MAX_CONFIRMATION_INPUT)
        if not response.endswith(("\n", "\r")):
            return False
        candidate = response.rstrip("\r\n")
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            APPROVAL_CONFIRMATION_PHRASE.encode("ascii"),
        )

    return request_tui_approval(
        plan,
        registry,
        show_preview=show_preview,
        confirm=confirm,
    )


__all__ = [
    "APPROVAL_CONFIRMATION_PHRASE",
    "request_terminal_approval",
    "request_tui_approval",
    "resolve_tui_startup",
]
