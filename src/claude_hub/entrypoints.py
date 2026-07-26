"""Console entry points that reserve the packaged command names.

The existing repository scripts intentionally remain at the repository root
until their behavior is migrated in a later tracer-bullet step.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__


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


def hub_main(argv: Sequence[str] | None = None) -> int:
    """Run the installable ``claude-hub`` placeholder."""

    return _placeholder_main("claude-hub", "claude-hub.py", argv)


def claude1_main(argv: Sequence[str] | None = None) -> int:
    """Run the installable ``claude1`` placeholder."""

    return _placeholder_main("claude1", "claude-provider-once.py", argv)
