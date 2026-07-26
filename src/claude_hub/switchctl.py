"""Versioned JSON command surface for agent callers."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import TextIO

from . import __version__
from .domain import StoreCapability
from .service import ProviderApplicationService
from .testing import InMemoryProviderStore
from .update_check import (
    ReleaseChannel,
    UpdateChecker,
    UpdateCheckResult,
    UpdateCheckSettings,
    UpdateStatus,
    build_default_checker,
)


SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2

_USAGE = "switchctl detect | switchctl check-update [--disabled]"


def build_default_service() -> ProviderApplicationService:
    """Build the temporary TB-02 service boundary.

    Real CC Switch location and schema probing belongs to issue #8.  Until that
    adapter exists, the installed command deliberately reports an absent store
    through the read-only in-memory fake and performs no environment or disk
    discovery.
    """

    return ProviderApplicationService(
        InMemoryProviderStore(capability=StoreCapability.ABSENT)
    )


def _envelope(
    *,
    ok: bool,
    data: dict[str, object] | None,
    error: dict[str, str] | None,
) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "error": error,
    }


def _write_json(stream: TextIO, payload: dict[str, object]) -> None:
    json.dump(
        payload,
        stream,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    stream.write("\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    service: ProviderApplicationService | None = None,
    update_checker: UpdateChecker | None = None,
    update_settings: UpdateCheckSettings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run ``switchctl`` with injectable services and output streams."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    diagnostics = sys.stderr if stderr is None else stderr

    if arguments in {("-h",), ("--help",)}:
        _write_json(
            output,
            _envelope(
                ok=True,
                data={"usage": _USAGE},
                error=None,
            ),
        )
        return EXIT_OK

    if arguments not in {
        ("detect",),
        ("check-update",),
        ("check-update", "--disabled"),
    }:
        _write_json(
            output,
            _envelope(
                ok=False,
                data=None,
                error={
                    "code": "usage_error",
                    "message": f"usage: {_USAGE}",
                },
            ),
        )
        diagnostics.write("switchctl: usage_error\n")
        return EXIT_USAGE

    if arguments == ("check-update", "--disabled"):
        update_result = UpdateCheckResult(
            status=UpdateStatus.DISABLED,
            current_version=__version__,
            channel=(
                ReleaseChannel.STABLE
                if update_settings is None
                else update_settings.channel
            ),
        )
        _write_json(
            output,
            _envelope(
                ok=True,
                data={"update": update_result.to_public_dict()},
                error=None,
            ),
        )
        return EXIT_OK

    if arguments == ("check-update",):
        try:
            checker = (
                build_default_checker(settings=update_settings)
                if update_checker is None
                else update_checker
            )
            update_result = checker.check(__version__)
        except Exception:
            update_result = UpdateCheckResult(
                status=UpdateStatus.UNAVAILABLE,
                current_version=__version__,
                channel=(
                    ReleaseChannel.STABLE
                    if update_settings is None
                    else update_settings.channel
                ),
            )
        _write_json(
            output,
            _envelope(
                ok=True,
                data={"update": update_result.to_public_dict()},
                error=None,
            ),
        )
        return EXIT_OK

    try:
        application = build_default_service() if service is None else service
        capability = application.detect()
    except Exception:
        _write_json(
            output,
            _envelope(
                ok=False,
                data=None,
                error={
                    "code": "runtime_error",
                    "message": "detect failed",
                },
            ),
        )
        diagnostics.write("switchctl: runtime_error\n")
        return EXIT_RUNTIME_ERROR

    _write_json(
        output,
        _envelope(
            ok=True,
            data={"capability": capability.value},
            error=None,
        ),
    )
    return EXIT_OK


run = main


__all__ = [
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "EXIT_USAGE",
    "SCHEMA_VERSION",
    "build_default_service",
    "main",
    "run",
]
