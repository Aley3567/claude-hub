"""Pure URL and connection-target policy for provider network calls.

This module never resolves DNS or opens a socket.  Callers inject resolver
snapshots and the connected peer address so the complete SSRF boundary can be
tested without network access.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urljoin, urlsplit, urlunsplit


class NetworkReasonCode(str, Enum):
    """Stable, non-sensitive rejection reasons."""

    INVALID_URL = "invalid_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    USERINFO_FORBIDDEN = "userinfo_forbidden"
    FRAGMENT_FORBIDDEN = "fragment_forbidden"
    MISSING_HOST = "missing_host"
    METADATA_TARGET_FORBIDDEN = "metadata_target_forbidden"
    DNS_RESOLUTION_EMPTY = "dns_resolution_empty"
    INVALID_RESOLVED_ADDRESS = "invalid_resolved_address"
    LITERAL_ADDRESS_MISMATCH = "literal_address_mismatch"
    LOCALHOST_RESOLUTION_INVALID = "localhost_resolution_invalid"
    RESTRICTED_ADDRESS_FORBIDDEN = "restricted_address_forbidden"
    CLEARTEXT_NON_LOOPBACK = "cleartext_non_loopback"
    PRIVATE_CONFIRMATION_REQUIRED = "private_confirmation_required"
    PRIVATE_CONFIRMATION_NOT_APPLICABLE = (
        "private_confirmation_not_applicable"
    )
    PRIVATE_CONFIRMATION_INVALID = "private_confirmation_invalid"
    PRIVATE_CONFIRMATION_ORIGIN_MISMATCH = (
        "private_confirmation_origin_mismatch"
    )
    PRIVATE_CONFIRMATION_ADDRESS_MISMATCH = (
        "private_confirmation_address_mismatch"
    )
    DNS_REBINDING_DETECTED = "dns_rebinding_detected"
    INVALID_PEER_ADDRESS = "invalid_peer_address"
    PEER_ADDRESS_MISMATCH = "peer_address_mismatch"
    INVALID_REDIRECT = "invalid_redirect"
    CROSS_ORIGIN_REDIRECT = "cross_origin_redirect"


class NetworkPolicyError(ValueError):
    """A policy rejection whose text never includes the rejected target."""

    def __init__(self, reason_code: NetworkReasonCode | str) -> None:
        code = (
            reason_code.value
            if isinstance(reason_code, NetworkReasonCode)
            else str(reason_code)
        )
        self.reason_code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class NormalizedTarget:
    """Canonical HTTP(S) target with a presentation-safe display value."""

    scheme: str
    host: str
    port: int
    path: str
    query: str
    origin: str
    request_url: str
    display_url: str

    @property
    def is_ip_literal(self) -> bool:
        return _ip_address_or_none(self.host) is not None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(display_url={self.display_url!r}, "
            f"origin={self.origin!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class PrivateNetworkConfirmation:
    """Single-attempt confirmation bound to one origin and DNS snapshot.

    Callers must create this value only in direct response to an explicit user
    confirmation and discard it after one ``authorize_target`` call.
    """

    origin: str
    addresses: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(origin={self.origin!r}, "
            f"address_count={len(self.addresses)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ConnectionPlan:
    """One connection attempt pinned to a validated resolver snapshot."""

    target: NormalizedTarget
    pinned_addresses: tuple[str, ...]
    private_network: bool

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(target={self.target.display_url!r}, "
            f"address_count={len(self.pinned_addresses)}, "
            f"private_network={self.private_network!r})"
        )


_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_METADATA_HOSTS = {
    "instance-data.ec2.internal",
    "metadata.azure.internal",
    "metadata.google.internal",
}
_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("168.63.129.16"),
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.170.23"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def _reject(reason_code: NetworkReasonCode) -> None:
    raise NetworkPolicyError(reason_code)


def _ip_address_or_none(
    value: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if "%" in value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _effective_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def _is_metadata_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return _effective_address(address) in _METADATA_ADDRESSES


def _normalize_host(raw_host: str) -> str:
    if "%" in raw_host or raw_host.endswith(".."):
        _reject(NetworkReasonCode.INVALID_URL)

    address = _ip_address_or_none(raw_host)
    if address is not None:
        return address.compressed.casefold()

    candidate = raw_host.rstrip(".")
    if not candidate:
        _reject(NetworkReasonCode.MISSING_HOST)
    try:
        ascii_host = candidate.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        _reject(NetworkReasonCode.INVALID_URL)
    if len(ascii_host) > 253:
        _reject(NetworkReasonCode.INVALID_URL)
    labels = ascii_host.split(".")
    if any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
        _reject(NetworkReasonCode.INVALID_URL)
    return ascii_host


def _authority(host: str, port: int, scheme: str) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        return rendered_host
    return f"{rendered_host}:{port}"


def _normalize_path(raw_path: str) -> str:
    if not raw_path:
        return "/"
    output: list[str] = []
    for segment in raw_path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1]:
                output.pop()
            continue
        output.append(segment)
    normalized = "/".join(output)
    if raw_path.startswith("/") and not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if raw_path.endswith(("/", "/.", "/..")) and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized or "/"


def normalize_provider_url(raw_url: str) -> NormalizedTarget:
    """Normalize an HTTP(S) URL without resolving or contacting its host."""

    if (
        not isinstance(raw_url, str)
        or not raw_url
        or raw_url != raw_url.strip()
        or _CONTROL_RE.search(raw_url)
        or "\\" in raw_url
    ):
        _reject(NetworkReasonCode.INVALID_URL)

    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        _reject(NetworkReasonCode.INVALID_URL)

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        _reject(NetworkReasonCode.UNSUPPORTED_SCHEME)
    if parsed.username is not None or parsed.password is not None:
        _reject(NetworkReasonCode.USERINFO_FORBIDDEN)
    if parsed.fragment or "#" in raw_url:
        _reject(NetworkReasonCode.FRAGMENT_FORBIDDEN)
    if not parsed.netloc or parsed.hostname is None:
        _reject(NetworkReasonCode.MISSING_HOST)

    host = _normalize_host(parsed.hostname)
    if host in _METADATA_HOSTS:
        _reject(NetworkReasonCode.METADATA_TARGET_FORBIDDEN)
    literal_address = _ip_address_or_none(host)
    if literal_address is not None and _is_metadata_address(literal_address):
        _reject(NetworkReasonCode.METADATA_TARGET_FORBIDDEN)

    try:
        parsed_port = parsed.port
    except ValueError:
        _reject(NetworkReasonCode.INVALID_URL)
    if parsed_port == 0 or (
        parsed_port is None and parsed.netloc.endswith(":")
    ):
        _reject(NetworkReasonCode.INVALID_URL)
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if scheme == "https" else 80)
    )
    authority = _authority(host, port, scheme)
    origin = f"{scheme}://{authority}"
    path = _normalize_path(parsed.path)
    request_url = urlunsplit((scheme, authority, path, parsed.query, ""))
    display_url = urlunsplit((scheme, authority, path, "", ""))
    return NormalizedTarget(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        query=parsed.query,
        origin=origin,
        request_url=request_url,
        display_url=display_url,
    )


def _coerce_target(target: str | NormalizedTarget) -> NormalizedTarget:
    if isinstance(target, NormalizedTarget):
        canonical = normalize_provider_url(target.request_url)
        if canonical != target:
            _reject(NetworkReasonCode.INVALID_URL)
        return canonical
    return normalize_provider_url(target)


def _address_sort_key(address: str) -> tuple[int, int]:
    parsed = ipaddress.ip_address(address)
    return parsed.version, int(parsed)


def _normalize_addresses(resolved_addresses: Iterable[object]) -> tuple[str, ...]:
    if isinstance(resolved_addresses, (str, bytes)):
        values: Iterable[object] = (resolved_addresses,)
    else:
        try:
            values = tuple(resolved_addresses)
        except TypeError:
            _reject(NetworkReasonCode.INVALID_RESOLVED_ADDRESS)

    normalized: set[str] = set()
    for raw_address in values:
        if isinstance(
            raw_address,
            (ipaddress.IPv4Address, ipaddress.IPv6Address),
        ):
            address = raw_address
        elif isinstance(raw_address, str) and "%" not in raw_address:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError:
                _reject(NetworkReasonCode.INVALID_RESOLVED_ADDRESS)
        else:
            _reject(NetworkReasonCode.INVALID_RESOLVED_ADDRESS)
        if "%" in str(address):
            _reject(NetworkReasonCode.INVALID_RESOLVED_ADDRESS)
        normalized.add(address.compressed.casefold())
    if not normalized:
        _reject(NetworkReasonCode.DNS_RESOLUTION_EMPTY)
    return tuple(sorted(normalized, key=_address_sort_key))


def _address_kind(address_text: str) -> str:
    address = ipaddress.ip_address(address_text)
    effective = _effective_address(address)
    if _is_metadata_address(address):
        _reject(NetworkReasonCode.METADATA_TARGET_FORBIDDEN)
    if effective.is_loopback:
        return "loopback"
    if effective.is_unspecified or effective.is_multicast or effective.is_reserved:
        _reject(NetworkReasonCode.RESTRICTED_ADDRESS_FORBIDDEN)
    if effective.is_link_local:
        return "private"
    if effective.is_private:
        return "private"
    if effective.is_global:
        return "public"
    _reject(NetworkReasonCode.RESTRICTED_ADDRESS_FORBIDDEN)


def _validated_snapshot(
    target: NormalizedTarget,
    resolved_addresses: Iterable[object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    addresses = _normalize_addresses(resolved_addresses)
    literal_address = _ip_address_or_none(target.host)
    if literal_address is not None:
        expected = literal_address.compressed.casefold()
        if addresses != (expected,):
            _reject(NetworkReasonCode.LITERAL_ADDRESS_MISMATCH)

    resolved_kinds = tuple(_address_kind(address) for address in addresses)
    is_localhost_name = (
        target.host == "localhost" or target.host.endswith(".localhost")
    )
    if is_localhost_name and any(
        kind != "loopback" for kind in resolved_kinds
    ):
        _reject(NetworkReasonCode.LOCALHOST_RESOLUTION_INVALID)
    if target.scheme == "http" and any(
        kind != "loopback" for kind in resolved_kinds
    ):
        _reject(NetworkReasonCode.CLEARTEXT_NON_LOOPBACK)

    is_loopback_literal = (
        literal_address is not None
        and _effective_address(literal_address).is_loopback
    )
    explicit_local_target = is_loopback_literal or is_localhost_name
    if explicit_local_target:
        policy_kinds = resolved_kinds
    else:
        # A remote-looking hostname that resolves to loopback is still an SSRF
        # target.  Treat loopback as private for confirmation purposes, while
        # retaining the raw kinds above for the "HTTP only to loopback" rule.
        policy_kinds = tuple(
            "private" if kind == "loopback" else kind
            for kind in resolved_kinds
        )
    return addresses, policy_kinds


def confirm_private_target(
    target: str | NormalizedTarget,
    resolved_addresses: Iterable[object],
    *,
    confirmed: bool,
) -> PrivateNetworkConfirmation:
    """Create a single-attempt confirmation bound to origin and IP set."""

    normalized_target = _coerce_target(target)
    addresses, kinds = _validated_snapshot(
        normalized_target,
        resolved_addresses,
    )
    if not any(kind == "private" for kind in kinds):
        _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_NOT_APPLICABLE)
    if confirmed is not True:
        _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_REQUIRED)
    return PrivateNetworkConfirmation(
        origin=normalized_target.origin,
        addresses=addresses,
    )


def authorize_target(
    target: str | NormalizedTarget,
    resolved_addresses: Iterable[object],
    *,
    private_confirmation: PrivateNetworkConfirmation | None = None,
) -> ConnectionPlan:
    """Authorize one connection attempt against an initial DNS snapshot."""

    normalized_target = _coerce_target(target)
    addresses, kinds = _validated_snapshot(
        normalized_target,
        resolved_addresses,
    )
    private_network = any(kind == "private" for kind in kinds)

    if private_network:
        if private_confirmation is None:
            _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_REQUIRED)
        if not isinstance(private_confirmation, PrivateNetworkConfirmation):
            _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_INVALID)
        if private_confirmation.origin != normalized_target.origin:
            _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_ORIGIN_MISMATCH)
        if private_confirmation.addresses != addresses:
            _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_ADDRESS_MISMATCH)
    elif private_confirmation is not None:
        _reject(NetworkReasonCode.PRIVATE_CONFIRMATION_NOT_APPLICABLE)

    return ConnectionPlan(
        target=normalized_target,
        pinned_addresses=addresses,
        private_network=private_network,
    )


def validate_pre_connect(
    plan: ConnectionPlan,
    resolved_addresses: Iterable[object],
) -> None:
    """Reject a changed resolver snapshot immediately before connecting."""

    if not isinstance(plan, ConnectionPlan):
        raise TypeError("plan must be a ConnectionPlan")
    current_addresses = _normalize_addresses(resolved_addresses)
    if current_addresses != plan.pinned_addresses:
        _reject(NetworkReasonCode.DNS_REBINDING_DETECTED)


def validate_peer(plan: ConnectionPlan, peer_address: object) -> None:
    """Verify that the connected peer is one of the pinned addresses."""

    if not isinstance(plan, ConnectionPlan):
        raise TypeError("plan must be a ConnectionPlan")
    try:
        peer_addresses = _normalize_addresses((peer_address,))
    except NetworkPolicyError as exc:
        if exc.reason_code in {
            NetworkReasonCode.DNS_RESOLUTION_EMPTY.value,
            NetworkReasonCode.INVALID_RESOLVED_ADDRESS.value,
        }:
            _reject(NetworkReasonCode.INVALID_PEER_ADDRESS)
        raise
    if peer_addresses[0] not in plan.pinned_addresses:
        _reject(NetworkReasonCode.PEER_ADDRESS_MISMATCH)


def validate_redirect(
    source: ConnectionPlan | NormalizedTarget,
    location: str,
) -> NormalizedTarget:
    """Resolve a redirect and require its canonical origin to stay unchanged."""

    target = source.target if isinstance(source, ConnectionPlan) else source
    if not isinstance(target, NormalizedTarget):
        raise TypeError("source must be a ConnectionPlan or NormalizedTarget")
    if (
        not isinstance(location, str)
        or not location
        or location != location.strip()
        or _CONTROL_RE.search(location)
        or "\\" in location
    ):
        _reject(NetworkReasonCode.INVALID_REDIRECT)
    try:
        redirected = normalize_provider_url(
            urljoin(target.request_url, location)
        )
    except NetworkPolicyError:
        raise
    except (TypeError, ValueError):
        _reject(NetworkReasonCode.INVALID_REDIRECT)
    if redirected.origin != target.origin:
        _reject(NetworkReasonCode.CROSS_ORIGIN_REDIRECT)
    return redirected


# Concise compatibility spellings for callers that use URL/connection wording.
normalize_url = normalize_provider_url
authorize_connection = authorize_target
validate_redirect_target = validate_redirect


__all__ = [
    "ConnectionPlan",
    "NetworkPolicyError",
    "NetworkReasonCode",
    "NormalizedTarget",
    "PrivateNetworkConfirmation",
    "authorize_connection",
    "authorize_target",
    "confirm_private_target",
    "normalize_provider_url",
    "normalize_url",
    "validate_peer",
    "validate_pre_connect",
    "validate_redirect",
    "validate_redirect_target",
]
