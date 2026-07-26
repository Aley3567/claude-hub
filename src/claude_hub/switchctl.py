"""Versioned JSON command surface for agent callers."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import TextIO

from .ccswitch import CCSwitchProviderStore
from .service import ProviderApplicationService
from .store import ProviderConfigCorruptError, ProviderNotFoundError


SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2

_USAGE = "switchctl detect"
_HELP_USAGE = (
    _USAGE,
    "switchctl list",
    "switchctl inspect <stable-id>",
    "switchctl mode [--store standalone]",
    "switchctl route [--store standalone]",
)
_COMMAND_USAGE = {
    "detect": _USAGE,
    "list": "switchctl list",
    "inspect": "switchctl inspect <stable-id>",
    "mode": "switchctl mode [--store standalone]",
    "route": "switchctl route [--store standalone]",
}


def build_default_service() -> ProviderApplicationService:
    """Build the read-only CC Switch service for installed commands."""

    return ProviderApplicationService(CCSwitchProviderStore())


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


def _write_error(
    output: TextIO,
    diagnostics: TextIO,
    *,
    code: str,
    message: str,
) -> None:
    _write_json(
        output,
        _envelope(
            ok=False,
            data=None,
            error={"code": code, "message": message},
        ),
    )
    diagnostics.write(f"switchctl: {code}\n")


def _usage_for(arguments: tuple[object, ...]) -> str:
    if arguments and isinstance(arguments[0], str):
        return _COMMAND_USAGE.get(arguments[0], _USAGE)
    return _USAGE


def main(
    argv: Sequence[str] | None = None,
    *,
    service: ProviderApplicationService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    standalone_exists: bool = False,
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
                data={"usage": _HELP_USAGE},
                error=None,
            ),
        )
        return EXIT_OK

    command: str
    store_override: str | None = None
    stable_id: str | None = None
    if arguments == ("detect",):
        command = "detect"
    elif arguments == ("list",):
        command = "list"
    elif len(arguments) == 2 and arguments[0] == "inspect":
        command = "inspect"
        stable_id = arguments[1]
    elif arguments in {("mode",), ("route",)}:
        command = "mode"
    elif arguments in {
        ("mode", "--store", "standalone"),
        ("route", "--store", "standalone"),
        ("--store", "standalone", "mode"),
        ("--store", "standalone", "route"),
    }:
        command = "mode"
        store_override = "standalone"
    else:
        _write_error(
            output,
            diagnostics,
            code="usage_error",
            message=f"usage: {_usage_for(arguments)}",
        )
        return EXIT_USAGE

    try:
        application = build_default_service() if service is None else service
        if command == "detect":
            capability = application.detect()
            data: dict[str, object] = {"capability": capability.value}
        elif command == "list":
            providers: list[dict[str, object]] = []
            for reference in application.list():
                item: dict[str, object] = {
                    "stableId": reference.provider_id,
                    "current": reference.is_current,
                }
                if reference.display_name is not None:
                    item["displayName"] = reference.display_name
                providers.append(item)
            data = {"providers": providers}
        elif command == "inspect":
            if stable_id is None:
                raise ValueError("stable id is missing")
            inspection = application.inspect_stable_id(stable_id)
            if (
                inspection.fingerprint is None
                or inspection.schema_capability is None
                or inspection.unknown_fingerprint is None
            ):
                raise ValueError("inspection summary is incomplete")
            data = {
                "stableId": inspection.reference.provider_id,
                "models": inspection.models.to_public_dict(),
                "configurationFingerprint": inspection.fingerprint,
                "current": inspection.is_current,
                "proxyTakeover": inspection.proxy_takeover,
                "schemaCapability": inspection.schema_capability.value,
                "unknownFields": {
                    "count": inspection.unknown_field_count,
                    "fingerprint": inspection.unknown_fingerprint,
                },
            }
        else:
            route = application.resolve_startup(
                standalone_exists=standalone_exists,
                store_override=store_override,
            )
            data = {
                "mode": route.mode.value,
                "firstScreen": route.first_screen.value,
            }
    except ProviderConfigCorruptError:
        _write_error(
            output,
            diagnostics,
            code="provider_config_corrupt",
            message="provider configuration is invalid",
        )
        return EXIT_RUNTIME_ERROR
    except ProviderNotFoundError:
        _write_error(
            output,
            diagnostics,
            code="provider_not_found",
            message="provider reference was not found",
        )
        return EXIT_RUNTIME_ERROR
    except Exception:
        _write_error(
            output,
            diagnostics,
            code="runtime_error",
            message=(
                "detect failed"
                if command == "detect"
                else f"{command} failed"
            ),
        )
        return EXIT_RUNTIME_ERROR

    _write_json(
        output,
        _envelope(
            ok=True,
            data=data,
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
