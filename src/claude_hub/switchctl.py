"""Versioned JSON command surface for agent callers."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import TextIO

from .domain import StoreCapability
from .service import ProviderApplicationService
from .testing import InMemoryProviderStore


SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2

_USAGE = "switchctl detect"


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
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run ``switchctl`` with injectable argv, service, and output streams."""

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

    if arguments != ("detect",):
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
