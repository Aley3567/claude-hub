"""Shared transport routing for Claude Hub upstream requests.

The public seam is intentionally small: resolve one immutable policy, then
open one request through :class:`UpstreamExecutor`.  Callers do not implement
their own direct/proxy fallback loops.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_PROXY_SCHEMES = {"http", "https"}
TRANSPORT_MODES = {"auto", "direct", "proxy"}


class TransportConfigError(ValueError):
    pass


class TransportUnavailable(ConnectionError):
    def __init__(self, message: str, *, errors: tuple[BaseException, ...] = ()):
        super().__init__(message)
        self.errors = errors


class TransportCandidate(NamedTuple):
    proxy: str | None
    identity: str


class TransportPolicy(NamedTuple):
    mode: str
    candidates: tuple[TransportCandidate, ...]


class OpenAttempt(NamedTuple):
    response: object
    identity: str


class TransportProbe(NamedTuple):
    identity: str
    ok: bool
    detail: str
    elapsed: float


def normalize_transport_config(
    config: object,
    *,
    default_mode: str = "auto",
) -> dict:
    """Validate and detach the user-visible transport configuration."""
    if config is None:
        return {"mode": default_mode, "proxies": ["system"]}
    if not isinstance(config, dict):
        raise TransportConfigError("transport must be an object")
    mode = str(config.get("mode", default_mode)).casefold()
    if mode not in TRANSPORT_MODES:
        raise TransportConfigError("transport mode must be auto, direct, or proxy")
    raw_proxies = config.get("proxies", ["system"] if mode != "direct" else [])
    if isinstance(raw_proxies, str):
        raw_proxies = [raw_proxies]
    if not isinstance(raw_proxies, list):
        raise TransportConfigError("transport proxies must be a list")
    proxies: list[str] = []
    for item in raw_proxies:
        if item == "system":
            value = item
        else:
            value = _normalize_proxy(item)
        if value not in proxies:
            proxies.append(value)
    if mode == "proxy" and not proxies:
        raise TransportConfigError("proxy mode requires at least one proxy source")
    return {"mode": mode, "proxies": proxies}


def _proxy_identity(proxy: str) -> str:
    parsed = urlsplit(proxy)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return "proxy:" + urlunsplit((parsed.scheme, netloc, "", "", ""))


def _normalize_proxy(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransportConfigError("proxy URL must be a non-empty string")
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        raise TransportConfigError(
            "proxy URL must use http:// or https:// and include a host"
        )
    return raw


def resolve_transport_policy(
    endpoint: str,
    config: dict | None = None,
    *,
    environ: dict[str, str] | None = None,
    system_proxies: dict[str, str] | None = None,
    bypass=None,
) -> TransportPolicy:
    """Resolve direct/proxy candidates without performing network I/O.

    ``config`` accepts ``mode`` (auto/direct/proxy) and ``proxies``.  The
    special proxy value ``system`` expands the OS/environment proxy for the
    endpoint scheme.  Candidate order is deterministic and credentials are
    never included in identities.
    """

    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TransportConfigError("endpoint must be an absolute HTTP(S) URL")
    raw_config = normalize_transport_config(config)
    mode = raw_config["mode"]
    raw_proxies = raw_config["proxies"]

    env = dict(os.environ if environ is None else environ)
    discovered = dict(
        urllib.request.getproxies() if system_proxies is None else system_proxies
    )
    # An injected environment is part of the deterministic resolver interface.
    # This also covers platforms where getproxies() does not inspect env vars.
    for key in (f"{parsed.scheme}_proxy", "all_proxy"):
        value = env.get(key) or env.get(key.upper())
        if value:
            discovered.setdefault(key.removesuffix("_proxy"), value)

    bypass_fn = urllib.request.proxy_bypass if bypass is None else bypass
    system_bypassed = bool(bypass_fn(parsed.hostname))
    proxies: list[str] = []
    for item in raw_proxies:
        if item == "system":
            if system_bypassed:
                continue
            value = discovered.get(parsed.scheme) or discovered.get("all")
            if not value:
                continue
            proxy = _normalize_proxy(value)
        else:
            proxy = _normalize_proxy(item)
        if proxy not in proxies:
            proxies.append(proxy)

    candidates: list[TransportCandidate] = []
    if mode in {"auto", "direct"}:
        candidates.append(TransportCandidate(None, "direct"))
    if mode in {"auto", "proxy"}:
        candidates.extend(
            TransportCandidate(proxy, _proxy_identity(proxy)) for proxy in proxies
        )
    if not candidates:
        raise TransportConfigError("proxy mode requires at least one usable proxy")
    return TransportPolicy(mode, tuple(candidates))


def _probe_candidate(
    candidate: TransportCandidate,
    endpoint: str,
    timeout: float,
) -> tuple[bool, str]:
    try:
        import certifi

        tls_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        tls_context = ssl.create_default_context()
    proxy_handler = (
        urllib.request.ProxyHandler({})
        if candidate.proxy is None
        else urllib.request.ProxyHandler(
            {"http": candidate.proxy, "https": candidate.proxy}
        )
    )
    handlers = [proxy_handler, urllib.request.HTTPSHandler(context=tls_context)]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        endpoint,
        method="HEAD",
        headers={"User-Agent": "claude1-doctor/0.1"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # Any HTTP response proves DNS, TCP and TLS reached the endpoint.
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{type(reason).__name__}: {str(reason)[:160]}"


def diagnose_transport_policy(
    endpoint: str,
    policy: TransportPolicy,
    *,
    timeout: float = 5.0,
    probe=None,
) -> tuple[TransportProbe, ...]:
    """Probe every allowed candidate without credentials or request bodies."""
    probe_fn = _probe_candidate if probe is None else probe
    report: list[TransportProbe] = []
    for candidate in policy.candidates:
        started = time.monotonic()
        ok, detail = probe_fn(candidate, endpoint, timeout)
        report.append(
            TransportProbe(
                candidate.identity,
                bool(ok),
                str(detail),
                time.monotonic() - started,
            )
        )
    return tuple(report)


class UpstreamExecutor:
    """Open an upstream response with safe pre-response transport fallback."""

    def __init__(self, *, log=None):
        self._log = log

    @staticmethod
    def _retryable(exc: BaseException) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, OSError)):
            return True
        try:
            import aiohttp
        except ImportError:
            return False
        return isinstance(
            exc,
            (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError),
        )

    @asynccontextmanager
    async def open(
        self,
        *,
        session,
        method: str,
        url: str,
        policy: TransportPolicy,
        request_kwargs: dict | None = None,
        retry_response=None,
    ):
        errors: list[BaseException] = []
        for index, candidate in enumerate(policy.candidates):
            kwargs = dict(request_kwargs or {})
            kwargs["proxy"] = candidate.proxy
            request = getattr(session, "request", None)
            if request is None and method.upper() == "POST":
                request = session.post
            context = request(method, url, **kwargs) if getattr(session, "request", None) is not None else request(url, **kwargs)
            try:
                response = await context.__aenter__()
            except BaseException as exc:
                if not self._retryable(exc):
                    raise
                errors.append(exc)
                if self._log is not None:
                    self._log(
                        f"transport {candidate.identity} failed before response: "
                        f"{type(exc).__name__}"
                    )
                continue
            if (
                retry_response is not None
                and retry_response(response)
                and index + 1 < len(policy.candidates)
            ):
                if self._log is not None:
                    self._log(
                        f"transport {candidate.identity} rejected before commit: "
                        f"HTTP {getattr(response, 'status', '?')}"
                    )
                await context.__aexit__(None, None, None)
                continue
            try:
                yield OpenAttempt(response, candidate.identity)
            finally:
                await context.__aexit__(None, None, None)
            return
        detail = ", ".join(type(exc).__name__ for exc in errors) or "no candidates"
        raise TransportUnavailable(
            f"all transports failed before response ({detail})",
            errors=tuple(errors),
        )
