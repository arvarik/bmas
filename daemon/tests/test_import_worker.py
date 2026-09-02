"""The egress broker blocks every documented SSRF and credential case.

Every fetch validates the destination through the controlled
resolver, pins one address for the connection, revalidates on every
redirect, strips authorization across authority changes, ignores
ambient proxies, revalidates the connected peer, and enforces the
transfer, content, and decompression limits. The broker holds no
secret and rejects archive content.
"""

from __future__ import annotations

import pytest

from benchmarks.import_worker import (
    FetchRequest,
    FetchResponse,
    ImportFetchError,
    SafeFetcher,
    gzip_bomb_fixture,
)
from core.url_guard import UrlValidationError, request_settings

PUBLIC_ADDRESS = "93.184.216.34"
OTHER_PUBLIC_ADDRESS = "151.101.1.140"


def resolver_for(answers: dict[str, list[str]]):
    def resolver(host: str) -> list[str]:
        if host not in answers:
            raise UrlValidationError(f"unknown host {host}")
        return list(answers[host])

    return resolver


class ScriptedTransport:
    """Return scripted responses and record every outbound hop."""

    def __init__(self, responses: list[FetchResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[FetchRequest] = []

    async def __call__(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("The transport ran out of responses")
        return self.responses.pop(0)


def ok(content: bytes = b"answer,42\n", **headers: str) -> FetchResponse:
    return FetchResponse(
        status_code=200,
        headers={"content-type": "text/csv", **headers},
        content=content,
    )


def redirect(location: str) -> FetchResponse:
    return FetchResponse(
        status_code=302, headers={"location": location}, content=b"",
    )


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("10.9.8.7", "private_range"),
        ("169.254.169.254", "metadata_address"),
        ("::ffff:10.0.0.5", "private_range"),
        # A link-local answer classifies as private first; either
        # reason blocks it.
        ("fe80::1", "private_range|link_local"),
    ],
)
@pytest.mark.asyncio
async def test_blocked_destinations_reject_before_any_connection(
    address, reason,
):
    transport = ScriptedTransport([ok()])
    fetcher = SafeFetcher(
        resolver=resolver_for({"blocked.example": [address]}),
        send=transport,
    )
    with pytest.raises(UrlValidationError, match=reason):
        await fetcher.fetch("https://blocked.example/data.csv")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_mixed_public_and_private_answers_reject():
    transport = ScriptedTransport([ok()])
    fetcher = SafeFetcher(
        resolver=resolver_for(
            {"mixed.example": [PUBLIC_ADDRESS, "192.168.1.10"]},
        ),
        send=transport,
    )
    with pytest.raises(UrlValidationError, match="private_range"):
        await fetcher.fetch("https://mixed.example/data.csv")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_the_connection_uses_the_pinned_address():
    # The resolver answers once; a later answer change cannot move the
    # connection, because the transport dials the pinned address.
    answers = {"pinned.example": [PUBLIC_ADDRESS]}
    transport = ScriptedTransport([ok()])
    fetcher = SafeFetcher(
        resolver=resolver_for(answers), send=transport,
    )
    result = await fetcher.fetch("https://pinned.example/data.csv")
    answers["pinned.example"] = ["10.0.0.9"]
    assert transport.requests[0].pinned_address == PUBLIC_ADDRESS
    assert transport.requests[0].host == "pinned.example"
    assert result.pinned_addresses == (PUBLIC_ADDRESS,)


@pytest.mark.asyncio
async def test_a_differing_connected_peer_rejects():
    response = FetchResponse(
        status_code=200,
        headers={"content-type": "text/csv"},
        content=b"answer,42\n",
        peer_address="10.0.0.9",
    )
    fetcher = SafeFetcher(
        resolver=resolver_for({"rebind.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([response]),
    )
    with pytest.raises(ImportFetchError, match="peer address"):
        await fetcher.fetch("https://rebind.example/data.csv")


@pytest.mark.asyncio
async def test_too_many_redirects_reject():
    hops = [redirect("https://loop.example/next") for _ in range(6)]
    fetcher = SafeFetcher(
        resolver=resolver_for({"loop.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport(hops),
    )
    with pytest.raises(UrlValidationError, match="redirect limit"):
        await fetcher.fetch("https://loop.example/start")


@pytest.mark.asyncio
async def test_a_redirect_to_a_blocked_destination_rejects():
    fetcher = SafeFetcher(
        resolver=resolver_for({
            "public.example": [PUBLIC_ADDRESS],
            "internal.example": ["192.168.7.7"],
        }),
        send=ScriptedTransport([
            redirect("https://internal.example/secret"),
        ]),
    )
    with pytest.raises(UrlValidationError, match="private_range"):
        await fetcher.fetch("https://public.example/data.csv")


@pytest.mark.asyncio
async def test_a_scheme_downgrade_redirect_rejects():
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([redirect("http://public.example/data")]),
    )
    with pytest.raises(UrlValidationError, match="scheme"):
        await fetcher.fetch("https://public.example/data.csv")


@pytest.mark.asyncio
async def test_a_cross_origin_redirect_strips_credentials():
    transport = ScriptedTransport([
        redirect("https://elsewhere.example/data.csv"),
        ok(),
    ])
    fetcher = SafeFetcher(
        resolver=resolver_for({
            "public.example": [PUBLIC_ADDRESS],
            "elsewhere.example": [OTHER_PUBLIC_ADDRESS],
        }),
        send=transport,
    )
    result = await fetcher.fetch(
        "https://public.example/data.csv",
        headers={
            "Authorization": "Bearer token-a",
            "Cookie": "session=a",
            "Accept": "text/csv",
        },
    )
    assert result.redirects_followed == 1
    first, second = transport.requests
    assert "Authorization" in first.headers
    # Authorization, cookies, and source credentials never cross an
    # authority change.
    assert "Authorization" not in second.headers
    assert "Cookie" not in second.headers
    assert second.headers.get("Accept") == "text/csv"


@pytest.mark.asyncio
async def test_embedded_credentials_reject():
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([ok()]),
    )
    with pytest.raises(UrlValidationError, match="credentials"):
        await fetcher.fetch("https://user:secret@public.example/data.csv")


def test_ambient_proxies_stay_denied_without_deployment_approval(
    monkeypatch,
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    settings = request_settings()
    assert settings["trust_environment_proxies"] is False
    assert settings["follow_redirects"] is False
    approved = request_settings(deployment_approves_proxies=True)
    assert approved["trust_environment_proxies"] is True


@pytest.mark.asyncio
async def test_an_oversized_response_rejects():
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([ok(content=b"x" * 2_000)]),
        limits={"response_bytes": 1_000, "compressed_bytes": 10_000},
    )
    with pytest.raises(ImportFetchError, match="content size"):
        await fetcher.fetch("https://public.example/data.csv")


@pytest.mark.asyncio
async def test_a_decompression_bomb_rejects():
    bomb = gzip_bomb_fixture(expanded_bytes=1_000_000)
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([
            ok(content=bomb, **{"content-encoding": "gzip"}),
        ]),
        limits={"decompressed_bytes": 100_000},
    )
    with pytest.raises(ImportFetchError, match="decompressed"):
        await fetcher.fetch("https://public.example/data.csv")


@pytest.mark.asyncio
async def test_archive_content_rejects():
    zip_bytes = b"PK\x03\x04" + b"\x00" * 64
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([ok(content=zip_bytes)]),
    )
    with pytest.raises(ImportFetchError, match="zip_archive"):
        await fetcher.fetch("https://public.example/data.zip")


@pytest.mark.asyncio
async def test_a_bounded_gzip_transfer_decompresses():
    import gzip as gzip_module

    body = gzip_module.compress(b"answer,42\n")
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=ScriptedTransport([
            ok(content=body, **{"content-encoding": "gzip"}),
        ]),
    )
    result = await fetcher.fetch("https://public.example/data.csv")
    assert result.content == b"answer,42\n"


@pytest.mark.asyncio
async def test_the_broker_carries_no_secret_by_default():
    transport = ScriptedTransport([ok()])
    fetcher = SafeFetcher(
        resolver=resolver_for({"public.example": [PUBLIC_ADDRESS]}),
        send=transport,
    )
    await fetcher.fetch("https://public.example/data.csv")
    sent = transport.requests[0].headers
    assert "Authorization" not in sent
    assert "Cookie" not in sent
