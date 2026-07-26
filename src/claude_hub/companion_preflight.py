"""Fail-closed checks that must pass before a Companion write.

The boundary consumes one process-local approval before consulting process or
Store state.  It is deliberately read-only: backup, mutation, process
termination, and waiting belong to later orchestration layers.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from .approval import ApprovalHandle, ApprovalRegistry
from .change_plan import COMPANION_STORE_ID, ChangePlan
from .domain import (
    ProviderInspection,
    ProviderRef,
    RuntimeMode,
    StoreCapability,
)
from .store import ProviderNotFoundError, ProviderStore


CC_SWITCH_PROCESS_NAMES = (
    "cc-switch",
    "CC Switch",
)
CC_SWITCH_WINDOWS_PROCESS_NAMES = tuple(
    f"{name}.exe" for name in CC_SWITCH_PROCESS_NAMES
)
PROCESS_PROBE_TIMEOUT_SECONDS = 2.0
MAX_PROCESS_PROBE_OUTPUT_CHARS = 65_536
PGREP_EXECUTABLE = "/usr/bin/pgrep"
_MAX_WINDOWS_PROCESSES = 65_536


def _windows_process_names() -> tuple[str, ...]:
    """Return executable names through Toolhelp, never process argv."""

    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("process enumeration failed")

    names: list[str] = []
    closed = False
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        has_entry = bool(process_first(snapshot, ctypes.byref(entry)))
        if not has_entry and ctypes.get_last_error() != 18:
            raise OSError("process enumeration failed")
        while has_entry:
            if len(names) >= _MAX_WINDOWS_PROCESSES:
                raise OSError("process enumeration failed")
            names.append(str(entry.szExeFile))
            has_entry = bool(process_next(snapshot, ctypes.byref(entry)))
        if ctypes.get_last_error() not in (0, 18):
            raise OSError("process enumeration failed")
    finally:
        closed = bool(close_handle(snapshot))
    if not closed:
        raise OSError("process enumeration failed")
    return tuple(names)


class CCSwitchProcessState(str, Enum):
    """Tri-state lifecycle result; uncertainty never means stopped."""

    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class CompanionPreflightStatus(str, Enum):
    """Public, redacted outcome codes for Companion preflight."""

    READY = "ready"
    APPROVAL_REQUIRED = "approval_required"
    PLAN_INVALID = "plan_invalid"
    PROCESS_RUNNING = "process_running"
    PROCESS_UNKNOWN = "process_unknown"
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    TARGET_NOT_FOUND = "target_not_found"
    PROXY_TAKEOVER_ACTIVE = "proxy_takeover_active"
    PLAN_STALE = "plan_stale"
    STORE_UNAVAILABLE = "store_unavailable"


_STATUS_MESSAGES = {
    CompanionPreflightStatus.APPROVAL_REQUIRED: (
        "Companion preflight requires a current approval"
    ),
    CompanionPreflightStatus.PLAN_INVALID: (
        "The approved plan is not a Companion write plan"
    ),
    CompanionPreflightStatus.PROCESS_RUNNING: (
        "CC Switch is still running"
    ),
    CompanionPreflightStatus.PROCESS_UNKNOWN: (
        "CC Switch process state could not be verified"
    ),
    CompanionPreflightStatus.SCHEMA_UNSUPPORTED: (
        "CC Switch schema does not allow Companion writes"
    ),
    CompanionPreflightStatus.TARGET_NOT_FOUND: (
        "The approved Companion target no longer exists"
    ),
    CompanionPreflightStatus.PROXY_TAKEOVER_ACTIVE: (
        "CC Switch proxy takeover is active"
    ),
    CompanionPreflightStatus.PLAN_STALE: (
        "The approved plan no longer matches the target"
    ),
    CompanionPreflightStatus.STORE_UNAVAILABLE: (
        "CC Switch storage could not be verified"
    ),
}
_STATUS_GUIDANCE = {
    CompanionPreflightStatus.APPROVAL_REQUIRED: (
        "Review the current plan and approve it again."
    ),
    CompanionPreflightStatus.PLAN_INVALID: (
        "Create and approve a Companion plan for the selected target."
    ),
    CompanionPreflightStatus.PROCESS_RUNNING: (
        "Exit CC Switch completely, then retry with a newly approved plan."
    ),
    CompanionPreflightStatus.PROCESS_UNKNOWN: (
        "Verify that process detection is available, then retry with a "
        "newly approved plan; do not modify the database."
    ),
    CompanionPreflightStatus.SCHEMA_UNSUPPORTED: (
        "Use a supported CC Switch schema before creating and approving "
        "a new plan."
    ),
    CompanionPreflightStatus.TARGET_NOT_FOUND: (
        "Refresh the provider list, then create and approve a new plan."
    ),
    CompanionPreflightStatus.PROXY_TAKEOVER_ACTIVE: (
        "In CC Switch, turn off proxy takeover for Claude, exit CC Switch, "
        "then create and approve a new plan."
    ),
    CompanionPreflightStatus.PLAN_STALE: (
        "Refresh the target, then create and approve a new plan."
    ),
    CompanionPreflightStatus.STORE_UNAVAILABLE: (
        "Ensure CC Switch is fully exited and its database is readable, "
        "then create and approve a new plan before retrying."
    ),
}


@runtime_checkable
class CCSwitchProcessDetector(Protocol):
    """Narrow process-state boundary used by production and tests."""

    def detect(self) -> CCSwitchProcessState:
        """Return whether CC Switch is running, stopped, or unknown."""


class SystemCCSwitchProcessDetector:
    """Probe fixed executable names without reading command-line arguments."""

    __slots__ = (
        "_platform",
        "_runner",
        "_timeout_seconds",
        "_windows_probe",
    )

    def __init__(
        self,
        *,
        platform: str | None = None,
        runner: Callable[..., object] | None = None,
        windows_probe: Callable[[], object] | None = None,
        timeout_seconds: float = PROCESS_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        selected_platform = os.name if platform is None else platform
        selected_runner = subprocess.run if runner is None else runner
        selected_windows_probe = (
            _windows_process_names
            if windows_probe is None
            else windows_probe
        )
        if not isinstance(selected_platform, str):
            raise TypeError("process detector platform is invalid")
        if not callable(selected_runner):
            raise TypeError("process detector runner is invalid")
        if not callable(selected_windows_probe):
            raise TypeError("Windows process detector is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 10
        ):
            raise ValueError("process detector timeout is invalid")
        self._platform = selected_platform
        self._runner = selected_runner
        self._windows_probe = selected_windows_probe
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(probe=<fixed-name-only>)"

    @staticmethod
    def _classify(result: object) -> CCSwitchProcessState:
        if not isinstance(result, subprocess.CompletedProcess):
            return CCSwitchProcessState.UNKNOWN
        if (
            not isinstance(result.returncode, int)
            or isinstance(result.returncode, bool)
            or not isinstance(result.stdout, str)
            or not isinstance(result.stderr, str)
            or len(result.stdout) > MAX_PROCESS_PROBE_OUTPUT_CHARS
            or len(result.stderr) > MAX_PROCESS_PROBE_OUTPUT_CHARS
        ):
            return CCSwitchProcessState.UNKNOWN
        if result.stderr:
            return CCSwitchProcessState.UNKNOWN
        if result.returncode == 1:
            return (
                CCSwitchProcessState.STOPPED
                if result.stdout == ""
                else CCSwitchProcessState.UNKNOWN
            )
        if result.returncode != 0:
            return CCSwitchProcessState.UNKNOWN
        lines = result.stdout.splitlines()
        if (
            not lines
            or any(
                not line.isascii()
                or not line.isdecimal()
                or len(line) > 20
                or int(line) <= 0
                for line in lines
            )
        ):
            return CCSwitchProcessState.UNKNOWN
        return CCSwitchProcessState.RUNNING

    def detect(self) -> CCSwitchProcessState:
        if self._platform == "nt":
            try:
                names = self._windows_probe()
            except Exception:
                return CCSwitchProcessState.UNKNOWN
            if (
                isinstance(names, (str, bytes))
                or not isinstance(names, (tuple, list))
                or len(names) > _MAX_WINDOWS_PROCESSES
                or any(
                    not isinstance(name, str)
                    or not name
                    or len(name) > 260
                    or "/" in name
                    or "\\" in name
                    or any(ord(character) < 32 for character in name)
                    for name in names
                )
            ):
                return CCSwitchProcessState.UNKNOWN
            allowed = {
                name.casefold()
                for name in CC_SWITCH_WINDOWS_PROCESS_NAMES
            }
            return (
                CCSwitchProcessState.RUNNING
                if any(name.casefold() in allowed for name in names)
                else CCSwitchProcessState.STOPPED
            )
        if self._platform != "posix":
            return CCSwitchProcessState.UNKNOWN

        for program_name in CC_SWITCH_PROCESS_NAMES:
            try:
                result = self._runner(
                    (PGREP_EXECUTABLE, "-x", program_name),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except Exception:
                return CCSwitchProcessState.UNKNOWN
            state = self._classify(result)
            if state is CCSwitchProcessState.RUNNING:
                return state
            if state is CCSwitchProcessState.UNKNOWN:
                return state
        return CCSwitchProcessState.STOPPED


@dataclass(frozen=True, slots=True, repr=False)
class CompanionPreflightError(RuntimeError):
    """A fixed, non-reflective preflight failure safe for presentation."""

    status: CompanionPreflightStatus
    process_state: CCSwitchProcessState | None = None
    schema_capability: StoreCapability | None = None

    def __post_init__(self) -> None:
        if self.status not in _STATUS_MESSAGES:
            raise ValueError("preflight error status is invalid")
        if self.process_state is not None and not isinstance(
            self.process_state,
            CCSwitchProcessState,
        ):
            raise TypeError("preflight process state is invalid")
        if self.schema_capability is not None and not isinstance(
            self.schema_capability,
            StoreCapability,
        ):
            raise TypeError("preflight schema capability is invalid")
        RuntimeError.__init__(self, _STATUS_MESSAGES[self.status])

    @property
    def guidance(self) -> str:
        return _STATUS_GUIDANCE[self.status]

    @property
    def allowed(self) -> bool:
        return False

    def to_public_dict(self) -> dict[str, str]:
        result = {
            "status": self.status.value,
            "guidance": self.guidance,
        }
        if self.process_state is not None:
            result["processState"] = self.process_state.value
        if self.schema_capability is not None:
            result["schemaCapability"] = self.schema_capability.value
        return result

    def __repr__(self) -> str:
        process_state = (
            None
            if self.process_state is None
            else self.process_state.value
        )
        schema_capability = (
            None
            if self.schema_capability is None
            else self.schema_capability.value
        )
        return (
            f"{type(self).__name__}("
            f"status={self.status.value!r}, "
            f"process_state={process_state!r}, "
            f"schema_capability={schema_capability!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CompanionPreflightResult:
    """A redacted point-in-time success result."""

    status: CompanionPreflightStatus
    process_state: CCSwitchProcessState
    schema_capability: StoreCapability

    def __post_init__(self) -> None:
        if self.status is not CompanionPreflightStatus.READY:
            raise ValueError("preflight result status is invalid")
        if self.process_state is not CCSwitchProcessState.STOPPED:
            raise ValueError("preflight process state is invalid")
        if self.schema_capability is not StoreCapability.COMPATIBLE:
            raise ValueError("preflight schema capability is invalid")

    @property
    def allowed(self) -> bool:
        return True

    @property
    def guidance(self) -> str:
        return "Proceed immediately with the approved Companion write."

    def to_public_dict(self) -> dict[str, str]:
        return {
            "status": self.status.value,
            "processState": self.process_state.value,
            "schemaCapability": self.schema_capability.value,
            "guidance": self.guidance,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"status={self.status.value!r}, "
            f"process_state={self.process_state.value!r}, "
            f"schema_capability={self.schema_capability.value!r})"
        )


class CompanionPreflight:
    """Run one read-only, approval-gated Companion preflight attempt."""

    __slots__ = ("_store", "_process_detector")

    def __init__(
        self,
        store: ProviderStore,
        process_detector: CCSwitchProcessDetector | None = None,
    ) -> None:
        self._store = store
        self._process_detector = (
            SystemCCSwitchProcessDetector()
            if process_detector is None
            else process_detector
        )

    def check(
        self,
        *,
        plan: ChangePlan,
        approval_registry: ApprovalRegistry,
        approval_handle: ApprovalHandle,
    ) -> CompanionPreflightResult:
        """Consume approval, then prove the current Store is safe to modify."""

        if type(approval_registry) is not ApprovalRegistry:
            raise CompanionPreflightError(
                CompanionPreflightStatus.APPROVAL_REQUIRED
            )
        try:
            approval_registry.consume(approval_handle, plan)
        except Exception:
            raise CompanionPreflightError(
                CompanionPreflightStatus.APPROVAL_REQUIRED
            ) from None
        if (
            type(plan) is not ChangePlan
            or plan.mode is not RuntimeMode.COMPANION
            or plan.target.store != COMPANION_STORE_ID
        ):
            raise CompanionPreflightError(
                CompanionPreflightStatus.PLAN_INVALID
            )
        try:
            process_state = self._process_detector.detect()
        except Exception:
            process_state = CCSwitchProcessState.UNKNOWN
        if not isinstance(process_state, CCSwitchProcessState):
            process_state = CCSwitchProcessState.UNKNOWN
        if process_state is CCSwitchProcessState.RUNNING:
            raise CompanionPreflightError(
                CompanionPreflightStatus.PROCESS_RUNNING,
                process_state=process_state,
            )
        if process_state is not CCSwitchProcessState.STOPPED:
            raise CompanionPreflightError(
                CompanionPreflightStatus.PROCESS_UNKNOWN,
                process_state=CCSwitchProcessState.UNKNOWN,
            )
        try:
            capability = self._store.detect()
        except Exception:
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
            ) from None
        if not isinstance(capability, StoreCapability):
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
            )
        if not capability.schema_allows_write:
            if capability in {
                StoreCapability.READ_ONLY,
                StoreCapability.INCOMPATIBLE,
            }:
                raise CompanionPreflightError(
                    CompanionPreflightStatus.SCHEMA_UNSUPPORTED,
                    process_state=process_state,
                    schema_capability=capability,
                )
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
                schema_capability=capability,
            )
        target = ProviderRef(
            store=plan.target.store,
            provider_id=plan.target.provider_id,
        )
        try:
            inspection = self._store.inspect(
                target
            )
        except ProviderNotFoundError:
            raise CompanionPreflightError(
                CompanionPreflightStatus.TARGET_NOT_FOUND,
                process_state=process_state,
                schema_capability=capability,
            ) from None
        except Exception:
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
                schema_capability=capability,
            ) from None
        if (
            type(inspection) is not ProviderInspection
            or inspection.reference != target
        ):
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
                schema_capability=capability,
            )
        inspection_capability = inspection.schema_capability
        if (
            inspection_capability is None
            or not inspection_capability.schema_allows_write
        ):
            if inspection_capability in {
                StoreCapability.READ_ONLY,
                StoreCapability.INCOMPATIBLE,
            }:
                raise CompanionPreflightError(
                    CompanionPreflightStatus.SCHEMA_UNSUPPORTED,
                    process_state=process_state,
                    schema_capability=inspection_capability,
                )
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
                schema_capability=capability,
            )
        if inspection.fingerprint is None:
            raise CompanionPreflightError(
                CompanionPreflightStatus.STORE_UNAVAILABLE,
                process_state=process_state,
                schema_capability=capability,
            )
        if inspection.proxy_takeover:
            raise CompanionPreflightError(
                CompanionPreflightStatus.PROXY_TAKEOVER_ACTIVE,
                process_state=process_state,
                schema_capability=capability,
            )
        if inspection.fingerprint != plan.store_fingerprint:
            raise CompanionPreflightError(
                CompanionPreflightStatus.PLAN_STALE,
                process_state=process_state,
                schema_capability=capability,
            )
        return CompanionPreflightResult(
            status=CompanionPreflightStatus.READY,
            process_state=process_state,
            schema_capability=capability,
        )

    def retry_after_exit(
        self,
        *,
        plan: ChangePlan,
        approval_registry: ApprovalRegistry,
        approval_handle: ApprovalHandle,
    ) -> CompanionPreflightResult:
        """Re-run every check after the user exits CC Switch.

        A fresh approval is required because the prior attempt consumed its
        handle before reporting the lifecycle failure.
        """

        return self.check(
            plan=plan,
            approval_registry=approval_registry,
            approval_handle=approval_handle,
        )


def run_companion_preflight(
    *,
    store: ProviderStore,
    plan: ChangePlan,
    approval_registry: ApprovalRegistry,
    approval_handle: ApprovalHandle,
    process_detector: CCSwitchProcessDetector | None = None,
) -> CompanionPreflightResult:
    """Convenience entry point for one production or injected attempt."""

    return CompanionPreflight(
        store,
        process_detector,
    ).check(
        plan=plan,
        approval_registry=approval_registry,
        approval_handle=approval_handle,
    )


# Short spelling for callers that do not need the CC Switch qualifier.
ProcessState = CCSwitchProcessState


__all__ = [
    "CC_SWITCH_PROCESS_NAMES",
    "CC_SWITCH_WINDOWS_PROCESS_NAMES",
    "CCSwitchProcessDetector",
    "CCSwitchProcessState",
    "CompanionPreflight",
    "CompanionPreflightError",
    "CompanionPreflightResult",
    "CompanionPreflightStatus",
    "MAX_PROCESS_PROBE_OUTPUT_CHARS",
    "PROCESS_PROBE_TIMEOUT_SECONDS",
    "PGREP_EXECUTABLE",
    "ProcessState",
    "SystemCCSwitchProcessDetector",
    "run_companion_preflight",
]
