"""Tests for wuwaterm_client.api against a mocked httpx transport.

No network access, no running server, no Qt event loop.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wuwaterm_client import errors, strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.errors import ERROR_TIMEOUT, ERROR_UNKNOWN, ClientError


def _client(handler, **kwargs) -> ApiClient:
    transport = httpx.MockTransport(handler)
    token_provider = kwargs.pop("token_provider", lambda: "wtd1.deadbeef.secret")
    return ApiClient(
        "https://test",
        token_provider=token_provider,
        _test_transport=transport,
        **kwargs,
    )


def _translation_payload(**overrides) -> dict:
    payload = {
        "kind": "exact",
        "text": "Rover",
        "direction": "en",
        "dictionary_miss": False,
        "request_id": "req-1",
    }
    payload.update(overrides)
    return payload


def test_bearer_header_sent_exactly() -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=_translation_payload())

    client = _client(handler, token_provider=lambda: "wtd1.abc123.supersecret")

    async def scenario() -> None:
        await client.translate("test text")
        await client.aclose()

    asyncio.run(scenario())
    assert captured["authorization"] == "Bearer wtd1.abc123.supersecret"


def test_to_param_sent_for_forced_direction() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_translation_payload())

    client = _client(handler)

    async def scenario() -> None:
        await client.translate("test text", to="en")
        await client.aclose()

    asyncio.run(scenario())
    assert captured["body"] == {"text": "test text", "to": "en"}


def test_to_param_omitted_for_auto_direction() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_translation_payload())

    client = _client(handler)

    async def scenario() -> None:
        await client.translate("test text", to=None)
        await client.aclose()

    asyncio.run(scenario())
    assert "to" not in captured["body"]
    assert captured["body"] == {"text": "test text"}


SERVER_ERROR_CODES = [
    (errors.ERROR_UNAUTHORIZED, 401, strings.ERROR_MSG_UNAUTHORIZED),
    (errors.ERROR_FORBIDDEN, 403, strings.ERROR_MSG_FORBIDDEN),
    (errors.ERROR_RATE_LIMITED, 429, strings.ERROR_MSG_RATE_LIMITED),
    (errors.ERROR_PAYLOAD_TOO_LARGE, 413, strings.ERROR_MSG_PAYLOAD_TOO_LARGE),
    (errors.ERROR_INVALID_REQUEST, 400, strings.ERROR_MSG_INVALID_REQUEST),
    (errors.ERROR_INPUT_TOO_LONG, 422, strings.ERROR_MSG_INPUT_TOO_LONG),
    (errors.ERROR_LLM_UNAVAILABLE, 503, strings.ERROR_MSG_LLM_UNAVAILABLE),
    (errors.ERROR_LLM_BUDGET_EXHAUSTED, 503, strings.ERROR_MSG_LLM_BUDGET_EXHAUSTED),
    (errors.ERROR_INTERNAL, 500, strings.ERROR_MSG_INTERNAL),
]


@pytest.mark.parametrize("code,status,expected_message", SERVER_ERROR_CODES)
def test_each_stable_error_code_renders_mapped_message(
    code: str, status: int, expected_message: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {"code": code, "message": "server-side text, not used"},
                "request_id": "req-err",
            },
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.translate("test text")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ClientError")

    exc = asyncio.run(scenario())
    assert exc.code == code
    assert exc.message == expected_message
    assert exc.request_id == "req-err"
    assert exc.status_code == status


def test_unrecognized_error_code_falls_back_to_unknown_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            418,
            json={
                "error": {"code": "a_future_code_this_client_does_not_know", "message": "x"},
                "request_id": "req-future",
            },
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.translate("test text")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ClientError")

    exc = asyncio.run(scenario())
    assert exc.code == "a_future_code_this_client_does_not_know"
    assert exc.message == strings.ERROR_MSG_UNKNOWN


def test_cancel_reports_cancellation_not_a_generic_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_translation_payload())

    client = _client(handler)

    async def scenario() -> ClientError:
        task = asyncio.ensure_future(client.translate("test text"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ClientError")

    exc = asyncio.run(scenario())
    assert exc.code == errors.ERROR_CANCELLED
    assert exc.message == strings.STATUS_CANCELLED
    assert exc.request_id is None


def test_connect_refused_renders_offline_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.translate("test text")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ClientError")

    exc = asyncio.run(scenario())
    assert exc.code == errors.ERROR_OFFLINE
    assert exc.message == strings.ERROR_MSG_OFFLINE


def test_timeout_renders_timeout_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.translate("test text")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ClientError")

    exc = asyncio.run(scenario())
    assert exc.code == errors.ERROR_TIMEOUT
    assert exc.message == strings.ERROR_MSG_TIMEOUT


def test_lookup_terms_round_trip() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "test query"
        return httpx.Response(
            200,
            json={
                "query": "test query",
                "matches": [
                    {
                        "zh": "test-zh",
                        "en": "Rover",
                        "category": "character",
                        "score": 1.0,
                        "reason": "exact",
                    }
                ],
                "request_id": "req-terms",
            },
        )

    client = _client(handler)

    async def scenario():
        result = await client.lookup_terms("test query")
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.query == "test query"
    assert result.matches[0].en == "Rover"
    assert result.request_id == "req-terms"


def test_get_meta_round_trip() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service_version": "0.2.1",
                "api_version": "v1",
                "schema_version": "1",
                "source_profile": "official",
                "source_commit": "abc123",
                "term_count": 42,
                "llm_configured": True,
                "request_id": "req-meta",
            },
        )

    client = _client(handler)

    async def scenario():
        result = await client.get_meta()
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.term_count == 42
    assert result.llm_configured is True
    assert result.request_id == "req-meta"


def test_get_meta_keeps_known_fields_when_the_server_adds_fields() -> None:
    """0.2.x parses the fields it knows and does not become an exact-key parser."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service_version": "0.2.1",
                "api_version": "v1",
                "schema_version": "1",
                "source_profile": "official",
                "source_commit": "abc123",
                "source_game_version": "3.7.0",
                "source_resource_version": "3.7.8",
                "source_changelist": "8975829",
                "future_field": "not-read",
                "term_count": 42,
                "llm_configured": True,
                "request_id": "req-meta",
            },
        )

    client = _client(handler)

    async def scenario():
        result = await client.get_meta()
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.service_version == "0.2.1"
    assert result.schema_version == "1"
    assert result.source_profile == "official"
    assert result.source_commit == "abc123"
    assert result.term_count == 42
    assert result.llm_configured is True
    assert result.request_id == "req-meta"
    assert "source_game_version" not in result.__dataclass_fields__


def test_a_request_without_a_stored_credential_sends_no_auth_header() -> None:
    """No credential must mean no header, not an empty or literal one."""
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "service_version": "0.2.1",
                "api_version": "v1",
                "schema_version": "2",
                "source_profile": "p",
                "source_commit": "c",
                "term_count": 1,
                "llm_configured": False,
                "request_id": "req-meta",
            },
        )

    client = _client(handler, token_provider=lambda: None)

    async def scenario() -> None:
        await client.get_meta()
        await client.aclose()

    asyncio.run(scenario())
    assert captured["authorization"] is None


def test_a_server_side_deadline_is_reported_as_a_timeout() -> None:
    """The service reports its own deadline as 504 with code `internal`.

    The contract says the status is what distinguishes that from a genuine
    failure, so rendering "something went wrong on the server" would
    misdescribe it to the only person who can act on it.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            504,
            json={
                "error": {"code": "internal", "message": "timed out"},
                "request_id": "req-504",
            },
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    error = asyncio.run(scenario())
    assert error.code == ERROR_TIMEOUT
    assert error.request_id == "req-504"


def test_a_successful_body_that_is_not_ours_becomes_a_client_error() -> None:
    """A proxy or an unrelated local service can answer 200 with anything."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello from something else</html>")

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_a_successful_body_missing_fields_becomes_a_client_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.lookup_terms("x")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_the_client_does_not_trust_environment_proxies() -> None:
    """Requests go to the address the owner configured, and nowhere else.

    httpx trusts HTTP_PROXY by default, so a machine with a proxy configured
    and no NO_PROXY entry for the configured host would send every request -
    bearer credential included - to that proxy instead of to the service.
    """
    client = ApiClient("http://127.0.0.1:8787")
    try:
        assert client._client.trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_a_credential_that_cannot_be_a_header_is_reported_as_unusable() -> None:
    """A pasted character outside ASCII would raise from inside a coroutine."""

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be attempted")

    client = _client(handler, token_provider=lambda: "wtd1.device.sécret")

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == errors.ERROR_UNAUTHORIZED


def test_a_result_field_of_the_wrong_type_becomes_a_client_error() -> None:
    """Every key present, one of them the wrong shape underneath.

    Without a check the value reaches a Qt call and raises there instead of
    arriving as the client's own rendered error state.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_translation_payload(text=[]))

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.translate("x")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_a_matches_field_that_is_not_a_list_becomes_a_client_error() -> None:
    """A string or an object is iterable too, and would produce nonsense."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"query": "x", "matches": "not-a-list", "request_id": "r"}
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.lookup_terms("x")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_metadata_the_contract_requires_must_be_present() -> None:
    """Nullable is not optional: those fields are required by the contract."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "service_version": "0.2.1",
                "api_version": "v1",
                "schema_version": "2",
                # source_profile omitted
                "source_commit": "c",
                "term_count": 1,
                "llm_configured": False,
                "request_id": "req-meta",
            },
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_a_body_that_cannot_be_decoded_becomes_a_client_error() -> None:
    """A malformed compressed body is neither a transport failure nor a
    timeout; it is simply unusable, and must arrive as such."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=b"this is not gzip at all",
        )

    client = _client(handler)

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == ERROR_UNKNOWN


def test_a_credential_store_failure_becomes_a_client_error() -> None:
    """It must not escape into whichever view happens to be running."""
    from wuwaterm_client.credentials import CredentialStoreUnavailable

    def unavailable():
        raise CredentialStoreUnavailable("vault unavailable")

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be attempted")

    client = _client(handler, token_provider=unavailable)

    async def scenario() -> ClientError:
        try:
            await client.get_meta()
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    assert asyncio.run(scenario()).code == errors.ERROR_UNAUTHORIZED


def test_an_absolute_url_is_refused_before_the_token_is_attached() -> None:
    """A per-request absolute URL cannot override the configured origin and
    carry the Bearer credential off to another host.

    httpx resolves the request url against base_url, and an absolute url wins
    origin and all. The client attaches the device token unconditionally, so
    without the origin guard an absolute (or plain-http, off-host) url would put
    the credential on the wire to somewhere the confidential-endpoint policy
    never approved. The request must be refused before it leaves the client.
    """
    seen = {"called": False, "authorization": None}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=_translation_payload())

    client = _client(handler, token_provider=lambda: "wtd1.abc123.supersecret")

    async def scenario() -> ClientError:
        try:
            await client._request("GET", "http://198.51.100.7:9/steal")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    error = asyncio.run(scenario())

    assert error.code == errors.ERROR_INSECURE_ENDPOINT
    # The request never left the client, so the token went nowhere.
    assert seen["called"] is False
    assert seen["authorization"] is None


def test_a_url_carrying_embedded_credentials_is_refused_before_the_token() -> None:
    """Userinfo on the target REPLACES the device token; the guard must refuse.

    A url like `https://user:pw@<the configured host>/v1/x` keeps the scheme,
    the host and the port, so the origin check passed - and httpx reports
    `netloc` WITHOUT the userinfo, so rebuilding the origin from it hid the
    credentials from the confidentiality check too. httpx then derived a `Basic`
    credential from that userinfo and OVERWROTE the `Authorization` header,
    sending an attacker-chosen credential instead of the device token, to the
    right host, over https, with nothing to see in the logs.

    The token provider must not even be CONSULTED: refusal precedes header
    construction, which is what makes it a transport guarantee rather than a
    header-scrubbing exercise.
    """
    seen = {"called": False, "authorization": None}
    tokens = {"reads": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["called"] = True
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=_translation_payload())

    def token_provider() -> str:
        tokens["reads"] += 1
        return "wtd1.abc123.supersecret"

    client = _client(handler, token_provider=token_provider)

    async def scenario() -> ClientError:
        try:
            await client._request("GET", "https://attacker:pw@test/v1/terms")
        except ClientError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected a ClientError")

    error = asyncio.run(scenario())

    assert error.code == errors.ERROR_INSECURE_ENDPOINT
    assert seen["called"] is False
    assert seen["authorization"] is None
    # Refused BEFORE the credential was read, not after.
    assert tokens["reads"] == 0


def test_the_request_guard_and_the_constructor_apply_one_policy() -> None:
    """Everything the constructor refuses in an address, the guard refuses in a
    target. The layer closest to the network must not be the permissive one.

    The guard's docstring claimed this before it was true: it applied only the
    confidentiality half while the constructor applied all of `usable_base_url`.
    """
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    client = _client(handler)

    async def scenario() -> list[str]:
        codes = []
        for target in (
            "https://user:pw@test/v1/terms",  # embedded credentials
            "https://user@test/v1/terms",  # username only
            "https://test/v1/terms?q=x",  # query on the target
            "https://test/v1/terms#frag",  # fragment on the target
            "http://test/v1/terms",  # scheme downgrade, off-loopback
            "https://test:8443/v1/terms",  # different port
        ):
            try:
                await client._request("GET", target)
            except ClientError as exc:
                codes.append(exc.code)
            else:  # pragma: no cover - only on a guard regression
                codes.append(f"SENT {target}")
        await client.aclose()
        return codes

    codes = asyncio.run(scenario())

    assert codes == [errors.ERROR_INSECURE_ENDPOINT] * 6, codes


def test_a_base_address_with_a_path_prefix_keeps_it_on_every_route() -> None:
    """The deployed topology publishes this service under a path prefix.

    `docs/deployment.md` routes the API at `<site>/wuwaterm-api`, so the base
    address the owner configures carries a path and every request has to land
    under it. That is httpx's `_merge_url` behaviour rather than RFC-3986
    joining, and the two disagree: `URL.join` on an absolute path DROPS the
    base path. The client's origin guard uses `join`, which stays correct here
    because it only compares scheme, host and port - but nothing pinned what
    the outgoing request actually looks like, and a 404 against a real reverse
    proxy is an expensive way to find that out. This asserts the sent URL for
    all three routes, with and without a trailing slash on the base.
    """
    sent: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if request.url.path.endswith("/v1/translations"):
            return httpx.Response(200, json=_translation_payload())
        if request.url.path.endswith("/v1/terms"):
            return httpx.Response(
                200, json={"query": "x", "matches": [], "request_id": "r"}
            )
        return httpx.Response(
            200,
            json={
                "service_version": "0",
                "api_version": "v1",
                "schema_version": None,
                "source_profile": None,
                "source_commit": None,
                "term_count": 0,
                "llm_configured": False,
                "request_id": "r",
            },
        )

    async def scenario(base: str) -> None:
        client = ApiClient(
            base,
            token_provider=lambda: "wtd1.abc123.supersecret",
            _test_transport=httpx.MockTransport(handler),
        )
        await client.translate("test text")
        await client.lookup_terms("x")
        await client.get_meta()
        await client.aclose()

    asyncio.run(scenario("https://site.example/wuwaterm-api"))
    asyncio.run(scenario("https://site.example/wuwaterm-api/"))

    assert sent == [
        "https://site.example/wuwaterm-api/v1/translations",
        "https://site.example/wuwaterm-api/v1/terms?q=x",
        "https://site.example/wuwaterm-api/v1/meta",
    ] * 2, sent
