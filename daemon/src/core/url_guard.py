"""Foundation remote-import URL validation and redirect policy.

The first importer permits HTTPS only. Every connection and every
redirect revalidates the destination: one strict parse, no embedded
credentials, full address resolution, a public-address check that
covers IPv4-mapped IPv6 forms and cloud metadata addresses, and one
pinned address for the actual connection, so DNS rebinding between
validation and connection changes nothing.

A redirect builds a new request. A cross-origin redirect never keeps
authorization, cookies, origin, referrer, or proxy authorization,
and caller-supplied security headers never forward. Environment proxy
settings stay ignored unless deployment policy approves them.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

    Resolver = Callable[[str], list[str]]

MAX_REDIRECTS = 5

# Cloud metadata destinations blocked by exact address.
METADATA_ADDRESSES = frozenset({
    "169.254.169.254",
    "fd00:ec2::254",
    "100.100.100.200",
})

SENSITIVE_REQUEST_HEADERS = frozenset({
    "authorization",
    "cookie",
    "origin",
    "referer",
    "proxy-authorization",
})

DEFAULT_LIMITS = {
    "connect_timeout_seconds": 10,
    "response_bytes": 50_000_000,
    "compressed_bytes": 50_000_000,
    "decompressed_bytes": 200_000_000,
}


class UrlValidationError(ValueError):
    """The destination failed the import network policy."""


@dataclass(frozen=True)
class PinnedDestination:
    """One validated destination with its pinned addresses.

    The connection must use one pinned address. It never resolves the
    host again, so a DNS answer that changes after validation cannot
    redirect the connection.
    """

    url: str
    scheme: str
    host: str
    port: int
    pinned_addresses: tuple[str, ...]
    origin: tuple[str, str, int]


@dataclass
class RedirectState:
    """The per-fetch redirect budget and origin history."""

    redirects_followed: int = 0
    origins: list[tuple[str, str, int]] = field(default_factory=list)


def _blocked_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address,
                    ) -> str | None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        # An IPv4-mapped IPv6 form cannot bypass the IPv4 rules.
        address = address.ipv4_mapped
    if str(address) in METADATA_ADDRESSES:
        return "metadata_address"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private_range"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if address.is_unspecified:
        return "unspecified"
    return None


def validate_url(
    url: str,
    *,
    resolver: Resolver,
    allowed_schemes: tuple[str, ...] = ("https",),
) -> PinnedDestination:
    """Validate one destination and pin its resolved addresses."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlValidationError(f"The URL does not parse: {exc}") from exc
    if parts.scheme not in allowed_schemes:
        raise UrlValidationError(
            f"The scheme {parts.scheme!r} is not permitted"
        )
    if parts.username is not None or parts.password is not None:
        raise UrlValidationError(
            "The URL embeds credentials; the importer strips and "
            "rejects them"
        )
    host = parts.hostname
    if not host:
        raise UrlValidationError("The URL names no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlValidationError(f"Invalid port: {exc}") from exc

    resolved = list(resolver(host))
    if not resolved:
        raise UrlValidationError(f"The host {host!r} resolves no address")
    for text in resolved:
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise UrlValidationError(
                f"The resolver returned an invalid address: {text!r}"
            ) from exc
        reason = _blocked_reason(address)
        if reason is not None:
            raise UrlValidationError(
                f"The destination {text} is blocked: {reason}"
            )
    return PinnedDestination(
        url=url,
        scheme=parts.scheme,
        host=host,
        port=port,
        pinned_addresses=tuple(resolved),
        origin=(parts.scheme, host, port),
    )


def connect_address(destination: PinnedDestination) -> str:
    """Return the one pinned address for the actual connection."""
    return destination.pinned_addresses[0]


def follow_redirect(
    current: PinnedDestination,
    location: str,
    *,
    resolver: Resolver,
    state: RedirectState,
) -> PinnedDestination:
    """Revalidate one redirect target and advance the redirect budget."""
    if state.redirects_followed >= MAX_REDIRECTS:
        raise UrlValidationError("The redirect limit was reached")
    state.redirects_followed += 1
    state.origins.append(current.origin)
    return validate_url(location, resolver=resolver)


def sanitized_headers(
    headers: dict[str, str],
    *,
    cross_origin: bool,
) -> dict[str, str]:
    """Build the header set for one new redirect request.

    The importer builds a new request after every redirect. A
    cross-origin redirect keeps no sensitive header, and
    caller-supplied security headers never forward to a redirect
    target.
    """
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in SENSITIVE_REQUEST_HEADERS:
            if cross_origin:
                continue
            if lowered in ("origin", "referer", "proxy-authorization"):
                continue
            sanitized[name] = value
            continue
        sanitized[name] = value
    return sanitized


def is_cross_origin(
    origin: tuple[str, str, int], destination: PinnedDestination,
) -> bool:
    """Report whether one redirect leaves its origin."""
    return origin != destination.origin


def request_settings(
    *, deployment_approves_proxies: bool = False,
) -> dict[str, object]:
    """Return the import HTTP client settings.

    Automatic redirects stay disabled so every hop revalidates, and
    environment proxy settings stay ignored without deployment
    approval.
    """
    return {
        "follow_redirects": False,
        "trust_environment_proxies": deployment_approves_proxies,
        "limits": dict(DEFAULT_LIMITS),
    }
