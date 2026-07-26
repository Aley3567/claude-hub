"""Installed-package Qt smoke harness.

This module stays dependency-free at import time so its scheduling adapter can
also be exercised by the core test environment where PySide6 is absent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


class _QtApplication(Protocol):
    def exec(self) -> int: ...

    def quit(self) -> None: ...


class _QtWindow(Protocol):
    def show(self) -> None: ...


class _QtFacade(Protocol):
    def create_application(self, argv: Sequence[str]) -> _QtApplication: ...

    def create_main_window(self, *, capability: object) -> _QtWindow: ...


class ScheduledExecApplication:
    """Schedule a callback immediately before entering the real Qt loop."""

    def __init__(
        self,
        application: _QtApplication,
        single_shot: Callable[[int, Callable[[], None]], None],
        *,
        delay_ms: int = 25,
    ) -> None:
        self._application = application
        self._single_shot = single_shot
        self._delay_ms = delay_ms

    def exec(self) -> int:
        # Qt's QCoreApplication docs say quit()/exit() is ineffective before
        # the main event loop starts. Keep this timer registration directly
        # beside exec() so it cannot drift ahead of window creation and show().
        self._single_shot(self._delay_ms, self._application.quit)
        return int(self._application.exec())


class ScheduledExecQtFacade:
    """Delegate real widget creation while wrapping only application.exec()."""

    def __init__(
        self,
        facade: _QtFacade,
        single_shot: Callable[[int, Callable[[], None]], None],
    ) -> None:
        self._facade = facade
        self._single_shot = single_shot

    def create_application(
        self,
        argv: Sequence[str],
    ) -> ScheduledExecApplication:
        return ScheduledExecApplication(
            self._facade.create_application(argv),
            self._single_shot,
        )

    def create_main_window(self, *, capability: object) -> _QtWindow:
        return self._facade.create_main_window(capability=capability)


def main() -> int:
    import claude_hub
    from PySide6.QtCore import QTimer

    from claude_hub.desktop import load_default_qt_facade, run_desktop
    from claude_hub.domain import StoreCapability
    from claude_hub.service import ProviderApplicationService
    from claude_hub.testing import InMemoryProviderStore

    source_package = Path(__file__).resolve().parents[1] / "src" / "claude_hub"
    loaded_package = Path(claude_hub.__file__).resolve().parent
    if loaded_package == source_package:
        raise RuntimeError(
            "desktop smoke loaded the source tree instead of the installed package"
        )

    real_facade = load_default_qt_facade()
    smoke_facade = ScheduledExecQtFacade(real_facade, QTimer.singleShot)
    service = ProviderApplicationService(
        InMemoryProviderStore(
            capability=StoreCapability.COMPATIBLE,
        )
    )
    return run_desktop(
        service,
        qt=smoke_facade,
        argv=("claude-hub-smoke",),
    )


if __name__ == "__main__":
    raise SystemExit(main())
