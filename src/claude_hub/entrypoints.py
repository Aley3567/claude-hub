"""Packaged command entry points.

``claude-hub gui`` is the first packaged presentation adapter.  Other
operations and the ``claude1`` implementation remain in repository-root
scripts until later tracer-bullet migrations.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, TextIO

from . import __version__
from .domain import StoreCapability
from .service import ProviderApplicationService
from .testing import InMemoryProviderStore

if TYPE_CHECKING:
    from .desktop import QtFacade


EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2
EXIT_DESKTOP_DEPENDENCY = 3


def _parser(program: str, legacy_script: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program,
        description=(
            f"Installable {program} command entry point "
            "(operational behavior is not packaged yet)."
        ),
        epilog=(
            "This release only establishes the packaged command surface. "
            f"The existing implementation remains in the repository-root "
            f"{legacy_script} script."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{program} {__version__}",
    )
    return parser


def _hub_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-hub",
        description="Installable claude-hub command entry point.",
        epilog=(
            "Other operational behavior remains in the repository-root "
            "claude-hub.py script while it is migrated."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"claude-hub {__version__}",
    )
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser(
        "gui",
        help="start the Qt Widgets desktop shell",
        description="Start the Qt Widgets desktop shell.",
    )
    return parser


def _placeholder_main(
    program: str,
    legacy_script: str,
    argv: Sequence[str] | None,
) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    parser = _parser(program, legacy_script)

    if args == ("help",) or any(arg in {"-h", "--help"} for arg in args):
        parser.print_help()
        return 0
    if args in {("--version",), ("version",)}:
        print(f"{program} {__version__}")
        return 0

    parser.print_usage(sys.stderr)
    print(
        f"{program}: packaged operational behavior is not implemented yet.",
        file=sys.stderr,
    )
    print(
        f"{program}: the existing implementation remains in "
        f"the repository-root {legacy_script} script.",
        file=sys.stderr,
    )
    return 2


def _build_default_hub_service() -> ProviderApplicationService:
    """Build the temporary fake service until store selection is packaged."""

    return ProviderApplicationService(
        InMemoryProviderStore(capability=StoreCapability.ABSENT)
    )


def _print_hub_placeholder_error(
    parser: argparse.ArgumentParser,
    diagnostics: TextIO,
) -> int:
    parser.print_usage(diagnostics)
    print(
        "claude-hub: packaged operational behavior is not implemented yet.",
        file=diagnostics,
    )
    print(
        "claude-hub: the existing implementation remains in "
        "the repository-root claude-hub.py script.",
        file=diagnostics,
    )
    return EXIT_USAGE


def hub_main(
    argv: Sequence[str] | None = None,
    *,
    service: ProviderApplicationService | None = None,
    qt: QtFacade | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the packaged ``claude-hub`` command."""

    args = tuple(sys.argv[1:] if argv is None else argv)
    parser = _hub_parser()
    diagnostics = sys.stderr if stderr is None else stderr

    if args == ("help",) or any(arg in {"-h", "--help"} for arg in args):
        parser.print_help()
        return EXIT_OK
    if args in {("--version",), ("version",)}:
        print(f"claude-hub {__version__}")
        return EXIT_OK
    if args != ("gui",):
        return _print_hub_placeholder_error(parser, diagnostics)

    from . import desktop

    try:
        selected_service = (
            _build_default_hub_service() if service is None else service
        )
        return desktop.run_desktop(
            selected_service,
            qt=qt,
            argv=("claude-hub",),
        )
    except desktop.DesktopDependencyError:
        print(
            "claude-hub: desktop dependencies are not installed.",
            file=diagnostics,
        )
        print(
            "claude-hub: install them with: "
            f"{desktop.DESKTOP_INSTALL_COMMAND}",
            file=diagnostics,
        )
        return EXIT_DESKTOP_DEPENDENCY
    except Exception:
        print(
            "claude-hub: desktop failed to start.",
            file=diagnostics,
        )
        return EXIT_RUNTIME_ERROR


def claude1_main(argv: Sequence[str] | None = None) -> int:
    """Run the installable ``claude1`` placeholder."""

    return _placeholder_main("claude1", "claude-provider-once.py", argv)


__all__ = [
    "EXIT_DESKTOP_DEPENDENCY",
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "EXIT_USAGE",
    "claude1_main",
    "hub_main",
]
