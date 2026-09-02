"""The isolated egress broker for remote source imports.

The broker implements the complete remote import policy: HTTPS only,
no embedded credentials, every hostname resolved through the
controlled resolver with every answer validated, IPv4-mapped IPv6
forms normalized and blocked, one pinned address per connection with
TLS hostname checks preserved, revalidation on every redirect with a
bounded budget and no scheme downgrade, authorization and cookies
stripped across authority changes, ambient proxies denied without
deployment approval, the peer address revalidated after connection,
exact connection, response, content, and decompression limits, and
archive content rejected. The broker holds no runtime secret and no
task credential, and it never executes remote code.
"""

from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from core.url_guard import (
    DEFAULT_LIMITS,
    PinnedDestination,
    RedirectState,
    UrlValidationError,
    connect_address,
    follow_redirect,
    is_cross_origin,
    request_settings,
    sanitized_headers,
    validate_url,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# Archive containers stay rejected for the first import formats. A
# gzip TRANSFER encoding decompresses under the declared cap; a gzip
# FILE is an archive and rejects.
_ARCHIVE_MAGIC = (
    (b"PK\x03\x04", "zip_archive"),
    (b"PK\x05\x06", "zip_archive"),
    (b"\x1f\x8b", "gzip_file"),
    (b"7z\xbc\xaf", "seven_zip_archive"),
    (b"\x28\xb5\x2f\xfd", "zstd_archive"),
)
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"


class ImportFetchError(ValueError):
    """The remote fetch violated the import policy or its limits."""


@dataclass(frozen=True)
class FetchRequest:
    """One outbound hop the broker sends.

    The connection dials the pinned address while the TLS handshake
    and the Host header keep the validated hostname, so certificate
    verification stays intact and rebinding between validation and
    connection changes nothing.
    """

    scheme: str
    host: str
    port: int
    pinned_address: str
    path_query: str
    headers: dict[str, str]


@dataclass(frozen=True)
class FetchResponse:
    """One response the transport returns to the broker."""

    status_code: int
    headers: dict[str, str]
    content: bytes
    # The connected peer address when the transport can report it.
    peer_address: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """One completed safe fetch with its provenance."""

    url: str
    final_url: str
    content: bytes
    media_type: str
    redirects_followed: int
    pinned_addresses: tuple[str, ...] = field(default_factory=tuple)


def default_resolver(host: str) -> list[str]:
    """Resolve every IPv4 and IPv6 answer for one hostname."""
    import socket

    answers: list[str] = []
    for entry in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
        address = str(entry[4][0])
        if address not in answers:
            answers.append(address)
    return answers


async def default_send(request: FetchRequest) -> FetchResponse:
    """Send one hop with the pinned address and preserved TLS checks."""
    import httpx

    settings = request_settings()
    url = (
        f"{request.scheme}://{request.pinned_address}:{request.port}"
        f"{request.path_query}"
    )
    async with httpx.AsyncClient(
        follow_redirects=False,
        trust_env=bool(settings["trust_environment_proxies"]),
        timeout=httpx.Timeout(
            DEFAULT_LIMITS["connect_timeout_seconds"],
        ),
    ) as client:
        built = client.build_request(
            "GET",
            url,
            headers={**request.headers, "Host": request.host},
        )
        # The TLS handshake verifies the certificate against the
        # validated hostname, not the pinned address literal.
        built.extensions["sni_hostname"] = request.host
        response = await client.send(built)
        content = await response.aread()
        peer: str | None = None
        stream = response.extensions.get("network_stream")
        if stream is not None:
            info = stream.get_extra_info("server_addr")
            if info:
                peer = str(info[0])
        return FetchResponse(
            status_code=response.status_code,
            headers={
                name.lower(): value
                for name, value in response.headers.items()
            },
            content=content,
            peer_address=peer,
        )


def _reject_archives(content: bytes) -> None:
    for magic, reason in _ARCHIVE_MAGIC:
        if content.startswith(magic):
            raise ImportFetchError(
                f"The import rejects archive content: {reason}"
            )
    if content[_TAR_MAGIC_OFFSET:_TAR_MAGIC_OFFSET + 5] == _TAR_MAGIC:
        raise ImportFetchError(
            "The import rejects archive content: tar_archive"
        )


def _bounded_decompress(content: bytes, limit: int) -> bytes:
    """Decompress one gzip body under the declared expansion cap."""
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        expanded = decompressor.decompress(content, limit + 1)
    except zlib.error as error:
        raise ImportFetchError(
            f"The compressed response does not decompress: {error}"
        ) from error
    if len(expanded) > limit or decompressor.unconsumed_tail:
        raise ImportFetchError(
            "The response exceeds the decompressed size limit"
        )
    return expanded


class SafeFetcher:
    """Fetch one remote resource under the complete import policy."""

    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]] | None = None,
        send: Callable[[FetchRequest], Awaitable[FetchResponse]]
        | None = None,
        limits: dict[str, int] | None = None,
    ) -> None:
        # The broker receives no secret and no task credential; it
        # holds only a resolver, a transport, and its limits.
        self._resolver = resolver or default_resolver
        self._send = send or default_send
        self._limits = {**DEFAULT_LIMITS, **(limits or {})}

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch one resource with validation on every hop."""
        destination = validate_url(url, resolver=self._resolver)
        state = RedirectState()
        request_headers = dict(headers or {})
        pinned: list[str] = []
        while True:
            pinned.append(connect_address(destination))
            response = await self._hop(destination, request_headers)
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise ImportFetchError(
                        "The redirect response names no location"
                    )
                previous = destination
                destination = follow_redirect(
                    previous,
                    self._absolute_location(previous, location),
                    resolver=self._resolver,
                    state=state,
                )
                # Authorization, cookies, and source credentials strip
                # across every authority change.
                request_headers = sanitized_headers(
                    request_headers,
                    cross_origin=is_cross_origin(
                        previous.origin, destination,
                    ),
                )
                continue
            if response.status_code != 200:
                raise ImportFetchError(
                    f"The source responded {response.status_code}"
                )
            content = self._validated_content(response)
            return FetchResult(
                url=url,
                final_url=destination.url,
                content=content,
                media_type=str(
                    response.headers.get("content-type") or "",
                ).split(";")[0].strip(),
                redirects_followed=state.redirects_followed,
                pinned_addresses=tuple(pinned),
            )

    async def _hop(
        self,
        destination: PinnedDestination,
        headers: dict[str, str],
    ) -> FetchResponse:
        pinned_address = connect_address(destination)
        parts = urlsplit(destination.url)
        path_query = parts.path or "/"
        if parts.query:
            path_query = f"{path_query}?{parts.query}"
        response = await self._send(
            FetchRequest(
                scheme=destination.scheme,
                host=destination.host,
                port=destination.port,
                pinned_address=pinned_address,
                path_query=path_query,
                headers=dict(headers),
            ),
        )
        # The peer revalidates after connection, so a resolver answer
        # that changed underneath the transport cannot stand.
        if response.peer_address is not None and (
            response.peer_address not in destination.pinned_addresses
        ):
            raise ImportFetchError(
                "The connected peer address differs from the validated "
                f"address: {response.peer_address}"
            )
        return response

    def _validated_content(self, response: FetchResponse) -> bytes:
        content = response.content
        if len(content) > self._limits["compressed_bytes"]:
            raise ImportFetchError(
                "The response exceeds the transfer size limit"
            )
        encoding = str(
            response.headers.get("content-encoding") or "",
        ).lower()
        if encoding in ("gzip", "x-gzip"):
            content = _bounded_decompress(
                content, self._limits["decompressed_bytes"],
            )
        elif encoding and encoding != "identity":
            raise ImportFetchError(
                f"The content encoding {encoding!r} is not permitted"
            )
        if len(content) > self._limits["response_bytes"]:
            raise ImportFetchError(
                "The response exceeds the content size limit"
            )
        _reject_archives(content)
        return content

    @staticmethod
    def _absolute_location(
        destination: PinnedDestination, location: str,
    ) -> str:
        if "://" in location:
            return location
        if location.startswith("/"):
            return (
                f"{destination.scheme}://{destination.host}:"
                f"{destination.port}{location}"
            )
        raise UrlValidationError(
            f"The redirect location does not resolve: {location!r}"
        )


def gzip_bomb_fixture(expanded_bytes: int) -> bytes:
    """Build one deterministic compressed body for the bomb tests."""
    return gzip.compress(b"\x00" * expanded_bytes)


__all__ = [
    "FetchRequest",
    "FetchResponse",
    "FetchResult",
    "ImportFetchError",
    "SafeFetcher",
    "default_resolver",
    "default_send",
    "gzip_bomb_fixture",
]
