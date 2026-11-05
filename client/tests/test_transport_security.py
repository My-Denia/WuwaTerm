"""Transport-hardening gates for the desktop client.

Two properties concern what the production client does before a request:

(b) an address that would carry the token to another machine in the clear is
    refused by the transport itself, with a stable error code; and
(c) its HTTPS transport verifies certificates and hostnames.

No Qt, no network, no running server.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig, endpoint_is_confidential, usable_base_url
from wuwaterm_client.errors import (
    ERROR_INSECURE_ENDPOINT,
    ERROR_NOT_CONFIGURED,
    ClientError,
)

LOOPBACK = "http://127.0.0.1:8787"

REFUSED_ADDRESSES = [
    "http://198.51.100.7:8787",
    "http://api.example.com",
    "http://api.example.com:8787/wuwaterm-api",
    "http://[2001:db8::1]:8787",
    "http://127.0.0.1.example.com:8787",  # loopback-looking, not loopback
    "ftp://example.com",
    "//example.com/wuwaterm-api",  # no scheme at all
    "example.com:8787",
]

ACCEPTED_ADDRESSES = [
    "https://api.example.com",
    "https://api.example.com:8443/wuwaterm-api",
    LOOPBACK,
    "http://localhost:8787",
    "http://[::1]:8787",
]


def _close(client: ApiClient) -> None:
    asyncio.run(client.aclose())


def _parts(url: httpx.URL) -> tuple[str, str, int, str]:
    """Scheme, host, port and path prefix - everything an address decides.

    The path is part of it on purpose: an endpoint may be published under a
    prefix, and a comparison that ignored it would stay green while every
    request went somewhere else. httpx normalises a base path with a trailing
    slash, so both sides are normalised here.
    """
    port = url.port or (443 if url.scheme == "https" else 80)
    path = url.path if url.path.endswith("/") else url.path + "/"
    return (url.scheme, url.host, port, path)


@pytest.mark.parametrize("address", REFUSED_ADDRESSES)
def test_the_transport_refuses_an_address_it_cannot_protect(address: str) -> None:
    """Refused where the transport is built, not only where it is typed.

    The settings dialog validates too, but this constructor is reachable
    without it - a hand-edited configuration file, a future caller - and a
    check that lives only in the dialog is not a transport guarantee.
    """
    with pytest.raises(ClientError) as raised:
        ApiClient(address)

    assert raised.value.code == ERROR_INSECURE_ENDPOINT
    assert raised.value.message == strings.ERROR_MSG_INSECURE_ENDPOINT
    # The same address is refused by the setting validator and by the policy
    # predicate, so the three cannot drift apart silently.
    assert usable_base_url(address) is False
    assert endpoint_is_confidential(address) is False


@pytest.mark.parametrize(
    "address",
    [
        "https://device:secret@api.example.com",       # a second, unmanaged credential
        "https://api.example.com?token=secret",        # silently dropped when joined
        "https://api.example.com#fragment",
        "https://api.example.com:notaport",
    ],
)
def test_the_transport_is_no_more_permissive_than_the_settings_field(address: str) -> None:
    """The layer closest to the network must not be the weakest one.

    Each of these is confidential in transit and still unusable as a stored
    address; the settings dialog and the on-disk loader have always refused
    them, and the constructor now applies the same predicate rather than the
    narrower confidentiality rule inside it.
    """
    # Confidential in transit, and still not a usable stored address: this is
    # exactly the gap between the two predicates.
    assert endpoint_is_confidential(address) is True
    assert usable_base_url(address) is False
    with pytest.raises(ClientError) as raised:
        ApiClient(address)
    assert raised.value.code == ERROR_INSECURE_ENDPOINT


@pytest.mark.parametrize("address", ACCEPTED_ADDRESSES)
def test_https_anywhere_and_plain_http_to_this_machine_are_accepted(address: str) -> None:
    """The rule must not be so strict that it refuses the supported forms:
    a gate that rejects everything proves nothing about the one case."""
    assert endpoint_is_confidential(address) is True
    client = ApiClient(address, token_provider=lambda: None)
    try:
        assert _parts(client._client.base_url) == _parts(httpx.URL(address))
    finally:
        _close(client)


def test_the_address_that_is_validated_is_the_address_that_is_used() -> None:
    """Whitespace made the two differ.

    `usable_base_url` strips before parsing, so `" https://api.example.com "`
    is approved in its stripped form; handing httpx the raw string meant the
    approved address and the configured one were not the same address - httpx
    reads a leading space as the start of a relative URL.
    """
    padded = "  https://api.example.com  "
    client = ApiClient(padded, token_provider=lambda: None)
    try:
        assert _parts(client._client.base_url) == ("https", "api.example.com", 443, "/")
        client.update_base_url("\thttps://other.example.com\n")
        assert _parts(client._client.base_url) == (
            "https",
            "other.example.com",
            443,
            "/",
        )
    finally:
        _close(client)


def test_a_refused_address_leaves_the_running_client_where_it_was() -> None:
    """A rejected settings change must not half-apply: the previous address
    stays in effect, so the owner keeps a working client."""
    client = ApiClient(LOOPBACK, token_provider=lambda: None)
    try:
        with pytest.raises(ClientError) as raised:
            client.update_base_url("http://198.51.100.7:8787")
        assert raised.value.code == ERROR_INSECURE_ENDPOINT
        # Compared per component rather than by string prefix: a prefix test
        # on a URL is the classic incomplete check, and it would also pass
        # for an address that merely starts the same way.
        assert _parts(client._client.base_url) == _parts(httpx.URL(LOOPBACK))

        client.update_base_url("https://api.example.com")
        assert _parts(client._client.base_url) == ("https", "api.example.com", 443, "/")
    finally:
        _close(client)


def test_a_stored_configuration_can_never_carry_a_refused_address(tmp_path) -> None:
    """End to end for the path a real launch takes: whatever is on disk,
    ClientConfig.load never hands the transport an address it will refuse.

    What it hands over instead is nothing at all. The loader used to
    substitute a machine-local development address here, which the transport
    accepted - so a hand-edited or corrupted file produced a client that was
    confidently pointed at the wrong place. The refused address now leaves
    the client unconfigured, and an unconfigured client sends nothing.
    """
    (tmp_path / "config.json").write_text(
        '{"base_url": "http://198.51.100.7:8787"}', encoding="utf-8"
    )
    config = ClientConfig.load(tmp_path)

    assert config.base_url is None
    client = ApiClient.from_config(config)
    try:
        assert client.is_configured is False
        with pytest.raises(ClientError) as raised:
            asyncio.run(client.get_meta())
        assert raised.value.code == ERROR_NOT_CONFIGURED
    finally:
        _close(client)


def test_certificate_verification_is_on_for_the_client_it_actually_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect the real SSL context built for the normal HTTPX transport."""
    original = ssl.create_default_context
    contexts: list[ssl.SSLContext] = []

    def record_context(*args, **kwargs):
        context = original(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(ssl, "create_default_context", record_context)
    client = ApiClient("https://api.example.com", token_provider=lambda: None)
    try:
        assert contexts
        assert all(context.verify_mode is ssl.CERT_REQUIRED for context in contexts)
        assert all(context.check_hostname for context in contexts)
    finally:
        _close(client)


def test_the_client_asks_for_verification_explicitly() -> None:
    """The httpx default is verification, and this pins that the client does
    not rely on that default staying what it is today."""
    recorded: dict[str, object] = {}
    original = httpx.AsyncClient

    class _Recording(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            recorded.update(kwargs)
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = _Recording  # type: ignore[misc]
    try:
        client = ApiClient("https://api.example.com", token_provider=lambda: None)
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    _close(client)

    assert recorded["verify"] is True
    assert recorded["trust_env"] is False
    assert recorded["transport"] is None


def test_the_client_exposes_no_public_verification_toggle() -> None:
    """A constructor keyword is how an insecure toggle usually arrives."""
    import inspect

    parameters = inspect.signature(ApiClient.__init__).parameters
    for suspicious in ("verify", "insecure", "allow_insecure", "ssl_verify", "no_verify"):
        assert suspicious not in parameters, suspicious
    # A transport supplied by test code controls its own TLS behavior.
    assert "transport" not in parameters
    assert "_test_transport" in parameters


def test_custom_test_transport_receives_the_request() -> None:
    """The test seam accepts the public HTTPX transport interface."""
    received: list[str] = []

    class TestTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            received.append(str(request.url))
            return httpx.Response(200)

    client = ApiClient(
        "https://api.example.com", _test_transport=TestTransport(),
        token_provider=lambda: "example-token",
    )

    async def scenario() -> None:
        try:
            response = await client._request("GET", "/v1/meta")
            assert response.status_code == 200
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert received == ["https://api.example.com/v1/meta"]
