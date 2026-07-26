"""Qt Widgets desktop shell over the shared application service."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from typing import Protocol

from .domain import StoreCapability
from .service import ProviderApplicationService


def build_desktop_install_command(
    python_executable: str | None = None,
) -> str:
    """Return a shell-appropriate command for this Python environment."""

    selected_python = python_executable or sys.executable or "python"
    arguments = [
        selected_python,
        "-m",
        "pip",
        "install",
        "claude-hub-kit[desktop]",
    ]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


DESKTOP_INSTALL_COMMAND = build_desktop_install_command()


class DesktopDependencyError(RuntimeError):
    """Raised when the optional Qt desktop dependency is unavailable."""


class QtApplication(Protocol):
    """Minimum QApplication behavior used by the presentation shell."""

    def exec(self) -> int:
        """Run the Qt event loop and return its exit code."""


class QtWindow(Protocol):
    """Minimum top-level window behavior used by the presentation shell."""

    def show(self) -> None:
        """Show the window."""


class QtFacade(Protocol):
    """Injectable Qt boundary used by tests and the real adapter."""

    def create_application(self, argv: Sequence[str]) -> QtApplication:
        """Return the process-wide Qt Widgets application."""

    def create_main_window(
        self,
        *,
        capability: StoreCapability,
    ) -> QtWindow:
        """Create the desktop shell for public application-service state."""


class PySide6QtFacade:
    """Small real Qt Widgets adapter loaded only for ``claude-hub gui``."""

    __slots__ = ("_widgets",)

    def __init__(self, widgets: object) -> None:
        self._widgets = widgets

    def create_application(self, argv: Sequence[str]) -> QtApplication:
        application_type = self._widgets.QApplication
        application = application_type.instance()
        if application is None:
            application = application_type(list(argv))
        elif not isinstance(application, application_type):
            raise RuntimeError(
                "an incompatible Qt application already exists"
            )
        return application

    def create_main_window(
        self,
        *,
        capability: StoreCapability,
    ) -> QtWindow:
        if not isinstance(capability, StoreCapability):
            raise TypeError("capability must be a StoreCapability")

        window = self._widgets.QMainWindow()
        window.setWindowTitle("claude-hub")
        window.resize(640, 360)

        central_widget = self._widgets.QWidget(window)
        layout = self._widgets.QVBoxLayout(central_widget)

        heading = self._widgets.QLabel("claude-hub", central_widget)
        heading.setObjectName("desktopHeading")
        layout.addWidget(heading)

        summary = self._widgets.QLabel(
            "Desktop shell is ready.",
            central_widget,
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        capability_label = self._widgets.QLabel(
            f"Provider store: {capability.value}",
            central_widget,
        )
        capability_label.setObjectName("storeCapability")
        layout.addWidget(capability_label)
        layout.addStretch()

        window.setCentralWidget(central_widget)
        return window


def load_default_qt_facade() -> QtFacade:
    """Load the optional Qt dependency without affecting non-GUI commands."""

    try:
        from PySide6 import QtWidgets
    except (ImportError, OSError):
        raise DesktopDependencyError(
            "the optional desktop dependency is unavailable"
        ) from None
    return PySide6QtFacade(QtWidgets)


def run_desktop(
    service: ProviderApplicationService,
    *,
    qt: QtFacade | None = None,
    argv: Sequence[str] = ("claude-hub",),
) -> int:
    """Run the empty desktop shell against public service methods only."""

    facade = load_default_qt_facade() if qt is None else qt
    application = facade.create_application(tuple(argv))
    capability = service.detect()
    if not isinstance(capability, StoreCapability):
        raise TypeError("service.detect() returned an invalid capability")
    window = facade.create_main_window(capability=capability)
    window.show()
    return int(application.exec())


__all__ = [
    "DESKTOP_INSTALL_COMMAND",
    "DesktopDependencyError",
    "PySide6QtFacade",
    "QtApplication",
    "QtFacade",
    "QtWindow",
    "build_desktop_install_command",
    "load_default_qt_facade",
    "run_desktop",
]
