"""Privacy-preserving, best-effort GitHub Release update checks.

The update checker is deliberately outside the provider application service:
it consumes only the installed package version and release channel.  Provider
state, device identifiers, credentials, and local paths never enter a request.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


RELEASES_API_URL = (
    "https://api.github.com/repos/Aley3567/claude-hub/releases?per_page=20"
)
RELEASE_PAGE_PREFIX = "https://github.com/Aley3567/claude-hub/releases/tag/"
PIP_UPGRADE_COMMAND = "python -m pip install --upgrade claude-hub-kit"

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CACHE_BYTES = 4096
MAX_RESPONSE_BYTES = 128 * 1024
DEFAULT_TIMEOUT_SECONDS = 3.0

_CACHE_SCHEMA = 1
_SEMVER_RE = re.compile(
    r"""
    \A
    v?
    (?P<major>0|[1-9][0-9]*)
    \.
    (?P<minor>0|[1-9][0-9]*)
    \.
    (?P<patch>0|[1-9][0-9]*)
    (?:
        -
        (?P<prerelease>
            (?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)
            (?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*
        )
    )?
    (?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?
    \Z
    """,
    re.VERBOSE,
)


class ReleaseChannel(str, Enum):
    """Release streams understood by the checker."""

    STABLE = "stable"
    PREVIEW = "preview"


class InstallKind(str, Enum):
    """How update guidance should be presented."""

    PIP = "pip"
    PACKAGE = "package"


class UpdateStatus(str, Enum):
    """Public, non-exceptional outcomes of an update check."""

    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"


class AdviceKind(str, Enum):
    """The type of safe user action returned for an available update."""

    COMMAND = "command"
    RELEASE_PAGE = "release_page"


@dataclass(frozen=True)
class UpdateCheckSettings:
    """Configuration that is safe to construct without provider state."""

    enabled: bool = True
    cache_enabled: bool = True
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    channel: ReleaseChannel = ReleaseChannel.STABLE
    install_kind: InstallKind = InstallKind.PIP
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.cache_enabled, bool):
            raise ValueError("cache_enabled must be a boolean")
        if isinstance(self.cache_ttl_seconds, bool) or not isinstance(
            self.cache_ttl_seconds, int
        ):
            raise ValueError("cache_ttl_seconds must be an integer")
        if not 0 <= self.cache_ttl_seconds <= MAX_CACHE_TTL_SECONDS:
            raise ValueError("cache_ttl_seconds is outside the supported range")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be numeric")
        if not 0 < float(self.timeout_seconds) <= 30:
            raise ValueError("timeout_seconds is outside the supported range")
        if not isinstance(self.channel, ReleaseChannel):
            raise ValueError("channel must be a ReleaseChannel")
        if not isinstance(self.install_kind, InstallKind):
            raise ValueError("install_kind must be an InstallKind")


@dataclass(frozen=True)
class UpdateAdvice:
    kind: AdviceKind
    value: str

    def to_public_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "value": self.value}


@dataclass(frozen=True)
class UpdateCheckResult:
    """Sanitized result suitable for the versioned Agent JSON envelope."""

    status: UpdateStatus
    current_version: str | None
    channel: ReleaseChannel
    latest_version: str | None = None
    advice: UpdateAdvice | None = None
    from_cache: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "currentVersion": self.current_version,
            "latestVersion": self.latest_version,
            "channel": self.channel.value,
            "advice": (
                None if self.advice is None else self.advice.to_public_dict()
            ),
            "fromCache": self.from_cache,
        }


@dataclass(frozen=True)
class HttpRequest:
    """A complete, inspectable anonymous request passed to an HTTP client."""

    url: str
    headers: tuple[tuple[str, str], ...]
    timeout_seconds: float
    method: str = "GET"
    body: None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class HttpClient(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse:
        """Send one request and return its bounded response."""


class UpdateCache(Protocol):
    def load(self) -> Mapping[str, object] | None:
        """Load one bounded cache entry."""

    def store(self, entry: Mapping[str, object]) -> None:
        """Replace the single bounded cache entry."""


class UrllibHttpClient:
    """Small stdlib HTTP adapter with a hard response-size limit."""

    def __init__(self, *, max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._max_response_bytes = max_response_bytes

    def send(self, request: HttpRequest) -> HttpResponse:
        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(
                raw_request,
                timeout=request.timeout_seconds,
            ) as response:
                body = response.read(self._max_response_bytes + 1)
                if len(body) > self._max_response_bytes:
                    return HttpResponse(status=0, body=b"")
                return HttpResponse(
                    status=int(response.status),
                    body=body,
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(status=int(error.code), body=b"")


class FileUpdateCache:
    """A one-entry JSON cache with private permissions and bounded I/O."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._path = path
        self._max_bytes = max_bytes

    def load(self) -> Mapping[str, object] | None:
        try:
            metadata = self._path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if os.name != "nt" and metadata.st_mode & 0o077:
                return None
            if metadata.st_size > self._max_bytes:
                return None

            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._path, flags)
            try:
                opened_metadata = os.fstat(descriptor)
                if not stat.S_ISREG(opened_metadata.st_mode):
                    return None
                if os.name != "nt" and opened_metadata.st_mode & 0o077:
                    return None
                if opened_metadata.st_size > self._max_bytes:
                    return None
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(self._max_bytes + 1)
            finally:
                os.close(descriptor)
            if len(raw) > self._max_bytes:
                return None
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeError, ValueError):
            return None

    def store(self, entry: Mapping[str, object]) -> None:
        raw = json.dumps(
            dict(entry),
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
        if len(raw) > self._max_bytes:
            raise ValueError("cache entry exceeds the size limit")

        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise OSError("cache parent is not a directory")
        if os.name != "nt":
            parent.chmod(0o700)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".update-check-",
            dir=parent,
        )
        temporary_path = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            if os.name != "nt":
                self._path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class _SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...]

    @classmethod
    def parse(cls, raw: object) -> _SemanticVersion | None:
        if not isinstance(raw, str) or len(raw) > 128:
            return None
        match = _SEMVER_RE.fullmatch(raw)
        if match is None:
            return None
        prerelease: list[int | str] = []
        raw_prerelease = match.group("prerelease")
        if raw_prerelease:
            for identifier in raw_prerelease.split("."):
                prerelease.append(
                    int(identifier) if identifier.isdecimal() else identifier
                )
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(prerelease),
        )

    @property
    def normalized(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if not self.prerelease:
            return base
        suffix = ".".join(str(identifier) for identifier in self.prerelease)
        return f"{base}-{suffix}"

    def __lt__(self, other: _SemanticVersion) -> bool:
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return left_core < right_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if isinstance(left, int) and isinstance(right, str):
                return True
            if isinstance(left, str) and isinstance(right, int):
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class _Release:
    version: _SemanticVersion
    tag: str
    is_prerelease: bool


class UpdateChecker:
    """Check for a release without ever making the main flow fail."""

    def __init__(
        self,
        *,
        http_client: HttpClient,
        clock: Callable[[], float] = time.time,
        cache: UpdateCache | None = None,
        settings: UpdateCheckSettings | None = None,
    ) -> None:
        self._http_client = http_client
        self._clock = clock
        self._cache = cache
        self._settings = settings or UpdateCheckSettings()

    @property
    def settings(self) -> UpdateCheckSettings:
        return self._settings

    def check(self, current_version: str) -> UpdateCheckResult:
        settings = self._settings
        current = _SemanticVersion.parse(current_version)
        normalized_current = None if current is None else current.normalized

        if not settings.enabled:
            return UpdateCheckResult(
                status=UpdateStatus.DISABLED,
                current_version=normalized_current,
                channel=settings.channel,
            )
        if current is None:
            return UpdateCheckResult(
                status=UpdateStatus.UNAVAILABLE,
                current_version=None,
                channel=settings.channel,
            )

        now = self._safe_time()
        cache_key = (
            f"{current.normalized}|{settings.channel.value}|"
            f"{settings.install_kind.value}"
        )
        cached = self._load_cache(cache_key, now, current)
        if cached is not None:
            return cached

        request = HttpRequest(
            url=RELEASES_API_URL,
            headers=(
                ("Accept", "application/vnd.github+json"),
                ("User-Agent", "claude-hub-update-check"),
            ),
            timeout_seconds=float(settings.timeout_seconds),
        )
        try:
            response = self._http_client.send(request)
            result = self._result_from_response(response, current)
        except Exception:
            result = UpdateCheckResult(
                status=UpdateStatus.UNAVAILABLE,
                current_version=current.normalized,
                channel=settings.channel,
            )

        if result.status in {
            UpdateStatus.AVAILABLE,
            UpdateStatus.UP_TO_DATE,
        }:
            self._store_cache(cache_key, now, result)
        return result

    def _safe_time(self) -> float:
        try:
            value = float(self._clock())
            if value < 0 or not math.isfinite(value):
                raise ValueError
            return value
        except Exception:
            return 0.0

    def _result_from_response(
        self,
        response: HttpResponse,
        current: _SemanticVersion,
    ) -> UpdateCheckResult:
        if response.status != 200:
            return self._unavailable(current)
        if (
            not isinstance(response.body, bytes)
            or len(response.body) > MAX_RESPONSE_BYTES
        ):
            return self._unavailable(current)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return self._unavailable(current)
        if not isinstance(payload, list):
            return self._unavailable(current)

        releases: list[_Release] = []
        valid_release_seen = False
        for item in payload:
            release = _parse_release(item)
            if release is None:
                continue
            valid_release_seen = True
            if (
                self._settings.channel is ReleaseChannel.STABLE
                and release.is_prerelease
            ):
                continue
            releases.append(release)

        if not releases:
            if payload and not valid_release_seen:
                return self._unavailable(current)
            return UpdateCheckResult(
                status=UpdateStatus.UP_TO_DATE,
                current_version=current.normalized,
                channel=self._settings.channel,
            )

        latest = max(releases, key=lambda release: release.version)
        if not current < latest.version:
            return UpdateCheckResult(
                status=UpdateStatus.UP_TO_DATE,
                current_version=current.normalized,
                latest_version=latest.version.normalized,
                channel=self._settings.channel,
            )

        if self._settings.install_kind is InstallKind.PIP:
            advice = UpdateAdvice(
                kind=AdviceKind.COMMAND,
                value=PIP_UPGRADE_COMMAND,
            )
        else:
            advice = UpdateAdvice(
                kind=AdviceKind.RELEASE_PAGE,
                value=RELEASE_PAGE_PREFIX + quote(latest.tag, safe=""),
            )
        return UpdateCheckResult(
            status=UpdateStatus.AVAILABLE,
            current_version=current.normalized,
            latest_version=latest.version.normalized,
            channel=self._settings.channel,
            advice=advice,
        )

    def _unavailable(
        self,
        current: _SemanticVersion,
    ) -> UpdateCheckResult:
        return UpdateCheckResult(
            status=UpdateStatus.UNAVAILABLE,
            current_version=current.normalized,
            channel=self._settings.channel,
        )

    def _load_cache(
        self,
        expected_key: str,
        now: float,
        expected_current: _SemanticVersion,
    ) -> UpdateCheckResult | None:
        if (
            not self._settings.cache_enabled
            or self._settings.cache_ttl_seconds == 0
            or self._cache is None
        ):
            return None
        try:
            entry = self._cache.load()
            if not isinstance(entry, Mapping):
                return None
            schema = entry.get("schema")
            if (
                isinstance(schema, bool)
                or not isinstance(schema, int)
                or schema != _CACHE_SCHEMA
            ):
                return None
            if entry.get("key") != expected_key:
                return None
            stored_at = entry.get("storedAt")
            if isinstance(stored_at, bool) or not isinstance(
                stored_at, (int, float)
            ):
                return None
            stored_at_value = float(stored_at)
            if not math.isfinite(stored_at_value):
                return None
            age = now - stored_at_value
            if age < 0 or age >= self._settings.cache_ttl_seconds:
                return None
            return _result_from_cache(
                entry.get("result"),
                expected_current=expected_current,
                settings=self._settings,
            )
        except Exception:
            return None

    def _store_cache(
        self,
        key: str,
        now: float,
        result: UpdateCheckResult,
    ) -> None:
        if (
            not self._settings.cache_enabled
            or self._settings.cache_ttl_seconds == 0
            or self._cache is None
        ):
            return
        entry: dict[str, object] = {
            "schema": _CACHE_SCHEMA,
            "key": key,
            "storedAt": now,
            "result": result.to_public_dict(),
        }
        try:
            self._cache.store(entry)
        except Exception:
            pass


def _parse_release(raw: object) -> _Release | None:
    if not isinstance(raw, dict):
        return None
    tag = raw.get("tag_name")
    prerelease = raw.get("prerelease")
    draft = raw.get("draft")
    if (
        not isinstance(tag, str)
        or len(tag) > 128
        or not isinstance(prerelease, bool)
        or not isinstance(draft, bool)
        or draft
    ):
        return None
    version = _SemanticVersion.parse(tag)
    if version is None:
        return None
    return _Release(
        version=version,
        tag=tag,
        is_prerelease=prerelease or bool(version.prerelease),
    )


def _result_from_cache(
    raw: object,
    *,
    expected_current: _SemanticVersion,
    settings: UpdateCheckSettings,
) -> UpdateCheckResult | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        status = UpdateStatus(raw.get("status"))
    except (TypeError, ValueError):
        return None
    if status not in {UpdateStatus.AVAILABLE, UpdateStatus.UP_TO_DATE}:
        return None
    if raw.get("channel") != settings.channel.value:
        return None

    current = _SemanticVersion.parse(raw.get("currentVersion"))
    latest_raw = raw.get("latestVersion")
    latest = None if latest_raw is None else _SemanticVersion.parse(latest_raw)
    if (
        current is None
        or current != expected_current
        or (latest_raw is not None and latest is None)
    ):
        return None

    advice_raw = raw.get("advice")
    advice: UpdateAdvice | None = None
    if status is UpdateStatus.AVAILABLE:
        if (
            latest is None
            or not current < latest
            or not isinstance(advice_raw, Mapping)
        ):
            return None
        try:
            advice_kind = AdviceKind(advice_raw.get("kind"))
        except (TypeError, ValueError):
            return None
        advice_value = advice_raw.get("value")
        if not isinstance(advice_value, str):
            return None
        if settings.install_kind is InstallKind.PIP:
            if (
                advice_kind is not AdviceKind.COMMAND
                or advice_value != PIP_UPGRADE_COMMAND
            ):
                return None
        else:
            allowed_release_pages = {
                RELEASE_PAGE_PREFIX + latest.normalized,
                RELEASE_PAGE_PREFIX + "v" + latest.normalized,
            }
            if (
                advice_kind is not AdviceKind.RELEASE_PAGE
                or advice_value not in allowed_release_pages
            ):
                return None
        advice = UpdateAdvice(kind=advice_kind, value=advice_value)
    else:
        if advice_raw is not None:
            return None
        if latest is not None and current < latest:
            return None

    return UpdateCheckResult(
        status=status,
        current_version=current.normalized,
        latest_version=None if latest is None else latest.normalized,
        channel=settings.channel,
        advice=advice,
        from_cache=True,
    )


def default_cache_path() -> Path:
    """Return a private cache location without exposing it to the request."""

    return Path.home() / ".cache" / "claude-hub" / "update-check.json"


def build_default_checker(
    *,
    settings: UpdateCheckSettings | None = None,
    cache: UpdateCache | None = None,
) -> UpdateChecker:
    """Build the production adapter while preserving dependency injection."""

    selected_settings = settings or UpdateCheckSettings()
    selected_cache = cache
    if (
        selected_cache is None
        and selected_settings.enabled
        and selected_settings.cache_enabled
    ):
        selected_cache = FileUpdateCache(default_cache_path())
    return UpdateChecker(
        http_client=UrllibHttpClient(),
        cache=selected_cache,
        settings=selected_settings,
    )


__all__ = [
    "AdviceKind",
    "DEFAULT_CACHE_TTL_SECONDS",
    "FileUpdateCache",
    "HttpClient",
    "HttpRequest",
    "HttpResponse",
    "InstallKind",
    "PIP_UPGRADE_COMMAND",
    "RELEASES_API_URL",
    "RELEASE_PAGE_PREFIX",
    "ReleaseChannel",
    "UpdateAdvice",
    "UpdateCache",
    "UpdateCheckResult",
    "UpdateCheckSettings",
    "UpdateChecker",
    "UpdateStatus",
    "UrllibHttpClient",
    "build_default_checker",
    "default_cache_path",
]
