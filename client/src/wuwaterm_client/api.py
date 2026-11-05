"""Async HTTP client for the wuwaterm API.

This module only calls the API and parses what comes back. It never
re-implements dictionary lookup, direction detection, or any other
translation pipeline step; every field below is a direct pass-through of the
service's response (docs/api/openapi.json).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import ClientConfig, usable_base_url
from .credentials import CredentialStoreUnavailable, read_token
from .errors import (
    ERROR_CANCELLED,
    ERROR_INSECURE_ENDPOINT,
    ERROR_NOT_CONFIGURED,
    ERROR_OFFLINE,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UNKNOWN,
    ClientError,
)
from .manuscript import valid_report

TokenProvider = Callable[[], "str | None"]

_BEARER_PREFIX = "Bearer "

# The HTTP API versions this client release speaks.
#
# The contract, stated once and in full: client 0.2.x speaks HTTP API `v1` -
# the `/v1/*` paths every method below calls, and the `api_version` field of
# `GET /v1/meta` - as served by wuwaterm >= 0.3.0. The constant lives in this
# module because this module is the one that owns the protocol: it builds the
# paths and it parses the replies, so a version the client no longer speaks
# would be a change HERE, not in a view.
#
# It is checked on the `/v1/meta` reply the status view ALREADY fetches when
# the owner presses 刷新. Nothing about this check sends a request - not at
# startup, not on a timer, not anywhere else. The unconfigured client still
# sends nothing at all, which is a tested invariant of this application
# (issues #68 / #80) and not something a compatibility check may spend.
#
# A mismatch is a WARNING, never a refusal: see
# StatusView._apply_api_version, reached from _show_meta.
SUPPORTED_API_VERSIONS: tuple[str, ...] = ("v1",)


def default_token_provider() -> str | None:
    return read_token()


# -- Wire-type checks ------------------------------------------------------
#
# Annotations are not runtime validation. A 2xx body can carry every required
# key with the wrong primitive underneath - `"text": []` - and the failure then
# surfaces inside a Qt call rather than as the client's own error state. These
# raise ValueError, which the parse wrapper turns into a ClientError.


def _as_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not a string")
    return value


def _as_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} is not a boolean")
    return value


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} is not an integer")
    return value


def _as_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is not a number")
    number = float(value)
    # NaN and the infinities are not numbers this client can render. Python's
    # json accepts all three (they are not JSON, but the parser emits them),
    # and they pass every check above - then reach a widget, where `round()`
    # raises deep inside a paint or a layout, OUTSIDE the wrapper that exists
    # to turn an unusable body into a reported error. Refusing here is what
    # keeps that promise: the same ValueError every other malformed field
    # raises, which `_parsed` turns into ClientError(unknown).
    if not math.isfinite(number):
        raise ValueError(f"{field} is not a finite number")
    return number


def _as_optional_str(value: Any, field: str) -> "str | None":
    if value is None:
        return None
    return _as_str(value, field)


# -- Response models (direct pass-through of the wire schema) --------------


@dataclass(frozen=True)
class TranslationResult:
    kind: str
    text: str
    direction: str
    dictionary_miss: bool
    request_id: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TranslationResult":
        return cls(
            kind=_as_str(data["kind"], "kind"),
            text=_as_str(data["text"], "text"),
            direction=_as_str(data["direction"], "direction"),
            dictionary_miss=_as_bool(data["dictionary_miss"], "dictionary_miss"),
            request_id=_as_str(data["request_id"], "request_id"),
        )


@dataclass(frozen=True)
class TermMatch:
    zh: str
    en: str
    category: str
    score: float
    reason: str


@dataclass(frozen=True)
class TermsResult:
    query: str
    matches: tuple[TermMatch, ...]
    request_id: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TermsResult":
        # A string or an object is iterable too, and would produce nonsense
        # matches instead of an error.
        if not isinstance(data["matches"], list):
            raise ValueError("matches is not a list")
        return cls(
            query=_as_str(data["query"], "query"),
            matches=tuple(
                TermMatch(
                    zh=_as_str(match["zh"], "zh"),
                    en=_as_str(match["en"], "en"),
                    category=_as_str(match["category"], "category"),
                    score=_as_float(match["score"], "score"),
                    reason=_as_str(match["reason"], "reason"),
                )
                for match in data["matches"]
            ),
            request_id=_as_str(data["request_id"], "request_id"),
        )


@dataclass(frozen=True)
class MetaResult:
    service_version: str
    api_version: str
    schema_version: str | None
    source_profile: str | None
    source_commit: str | None
    term_count: int
    llm_configured: bool
    request_id: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "MetaResult":
        return cls(
            service_version=_as_str(data["service_version"], "service_version"),
            api_version=_as_str(data["api_version"], "api_version"),
            # Nullable, but REQUIRED by the contract: a body that omits them
            # is not this service's, and .get() would have made it look like
            # one with unknown provenance.
            schema_version=_as_optional_str(data["schema_version"], "schema_version"),
            source_profile=_as_optional_str(data["source_profile"], "source_profile"),
            source_commit=_as_optional_str(data["source_commit"], "source_commit"),
            term_count=_as_int(data["term_count"], "term_count"),
            llm_configured=_as_bool(data["llm_configured"], "llm_configured"),
            request_id=_as_str(data["request_id"], "request_id"),
        )


def _review_report(data: Any, source: str, target: str) -> dict[str, Any]:
    """Accept a report only for the exact text and locations submitted.

    A successful HTTP response alone does not prove that a report belongs to
    the current draft. Offsets are Unicode scalar indices, not Qt positions.
    """
    if not valid_report(data, source, target):
        raise ValueError("review is not a supported report")
    for side, text in (("source", source), ("target", target)):
        revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if data[f"{side}_revision"] != revision:
            raise ValueError(f"{side} revision does not match")
    if data["rule_version"] != "review-v2":
        raise ValueError("unexpected review version")
    # The v2 matcher basis is essential to bind a report to the algorithm
    # that produced it, and the requested capability guarantees this field.
    if "matcher_revision" not in data:
        raise ValueError("missing matcher revision")
    return data


def _normalized(base_url: str | None) -> str | None:
    """The exact string that is validated is the string that is used.

    `usable_base_url` strips before parsing, so a value with surrounding
    whitespace is approved in its stripped form; handing httpx the raw one
    would mean the approved address and the configured address are not the
    same address.

    `None` - an unconfigured client - passes straight through, and so does
    anything that is not a string at all: annotations are not runtime
    validation, and returning such a value unchanged is what lets the checks
    below refuse it as an unusable ADDRESS rather than mistaking it for an
    absent one.
    """
    return base_url.strip() if isinstance(base_url, str) else base_url


def _require_confidential_endpoint(base_url: str) -> None:
    """Refuse an address that would put the device token on the wire in the
    clear, before the transport exists and before any request is built.

    The check is unconditional: it does not consult a setting, and there is
    no parameter anywhere in this client that turns it off. It runs here as
    well as in the settings dialog because this constructor is reachable
    without that dialog - a hand-edited configuration file, a future caller,
    a test - and a refusal that only lives in the UI is not a transport
    guarantee.

    It applies `usable_base_url`, the SAME predicate the settings dialog and
    the on-disk loader use, rather than the narrower confidentiality rule it
    contains. The layer closest to the network must not be the most
    permissive one: embedded credentials, a query or a fragment are refused
    here too, so an address cannot become acceptable merely by arriving
    through a different door.
    """
    if not usable_base_url(base_url):
        raise ClientError(ERROR_INSECURE_ENDPOINT)


class ApiClient:
    """Bearer-authenticated async client for the wuwaterm HTTP API.

    ``base_url=None`` builds the client in an explicitly UNCONFIGURED state.
    It is a real state of this application, not a degenerate one: the owner's
    ``config.json`` disappeared across a reboot once, and every launch after
    that has to produce a window the owner can open Settings from. So the
    object is constructible, and every request path refuses with
    ``ERROR_NOT_CONFIGURED`` - a code of its own, whose message names
    Settings - instead of the client silently talking to a development
    address that had been standing in for "no setting".

    Nothing is relaxed to make that state constructible. An address that IS
    supplied goes through exactly the same `usable_base_url` check as before,
    the underlying httpx client is given no origin at all until one is
    configured, and `_request` refuses before it builds anything - so even if
    that refusal were removed, `_guard_request_target` would still refuse the
    empty origin below it.
    """

    def __init__(
        self,
        base_url: str | None,
        *,
        token_provider: TokenProvider = default_token_provider,
        timeout: float = 10.0,
        translate_timeout: float | None = None,
        _test_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_url = _normalized(base_url)
        if base_url is not None:
            _require_confidential_endpoint(base_url)
        self._configured = base_url is not None
        self._token_provider = token_provider
        self._timeout = timeout
        self._translate_timeout = translate_timeout if translate_timeout is not None else timeout
        self._client = httpx.AsyncClient(
            # No origin while unconfigured. Every request path refuses first,
            # and this is the second refusal underneath it: an empty origin
            # cannot pass `_guard_request_target`, so there is no arrangement
            # of this object that sends the device token anywhere.
            base_url=base_url if base_url is not None else "",
            timeout=timeout,
            transport=_test_transport,
            # Configure certificate and hostname verification on the normal
            # transport. The private test seam supplies its own transport.
            verify=True,
            # httpx trusts HTTP_PROXY by default, so a machine with a proxy
            # configured and no NO_PROXY entry for the configured host would
            # send every request - bearer credential included - to that proxy
            # instead of to the service. The address the owner configured is
            # the address this client talks to.
            trust_env=False,
        )

    @property
    def is_configured(self) -> bool:
        """Whether this client has a server address it may send requests to."""
        return self._configured

    @classmethod
    def from_config(cls, config: ClientConfig) -> "ApiClient":
        """Build a client for a stored configuration.

        An unconfigured configuration produces an unconfigured client, and
        that client refuses every request with ``ERROR_NOT_CONFIGURED``
        rather than sending one. It does NOT quietly acquire an address of
        its own: `ClientConfig.load` no longer has one to give it.
        """
        return cls(
            config.base_url,
            timeout=config.request_timeout_seconds,
            translate_timeout=config.translate_timeout_seconds,
        )

    def update_base_url(self, base_url: str | None) -> None:
        """Point the live client at a new address, or refuse and keep the old.

        Raises ClientError(ERROR_INSECURE_ENDPOINT) rather than switching to
        an address that is not protected in transit, and
        ClientError(ERROR_NOT_CONFIGURED) for no address at all. The previous
        address stays in effect either way, so a refusal leaves a working
        client rather than a half-configured one - and this is also how an
        unconfigured client becomes a configured one when the owner sets an
        address in Settings.

        The address is normalised the same way it was validated. They used to
        differ: the check strips before parsing, so `" https://host "` passed,
        and the RAW string then went to httpx - which reads a leading space as
        the start of a relative URL and silently produced a base address that
        was not the one approved.
        """
        base_url = _normalized(base_url)
        if base_url is None:
            raise ClientError(ERROR_NOT_CONFIGURED)
        _require_confidential_endpoint(base_url)
        self._client.base_url = httpx.URL(base_url)
        self._configured = True

    def update_timeouts(self, timeout: float, translate_timeout: float) -> None:
        """Apply edited timeouts to the live client.

        Both are captured per call rather than held only on the underlying
        httpx client, so a value changed in Settings has to be pushed here or
        the saved configuration and the running client silently disagree until
        the next launch.
        """
        self._timeout = timeout
        self._translate_timeout = translate_timeout

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token_provider()
        except CredentialStoreUnavailable:
            # Reported like any other unusable credential rather than escaping
            # into whichever view happens to be running, which would leave its
            # status line saying the work is still in progress.
            raise ClientError(ERROR_UNAUTHORIZED) from None
        if not token:
            raise ClientError(ERROR_UNAUTHORIZED)
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            # A pasted credential can carry a character that cannot go in a
            # header at all, and httpx would raise from inside whichever
            # coroutine happened to be running. The service refuses such a
            # secret at registration, so a stored one is a paste error: report
            # it as the credential being unusable, which is also the message
            # that tells the owner where to fix it.
            raise ClientError(ERROR_UNAUTHORIZED) from None
        return {"Authorization": f"{_BEARER_PREFIX}{token}"}

    def _guard_request_target(self, url: str) -> None:
        """Refuse to send this request anywhere but the configured origin.

        `httpx` resolves the request `url` against `base_url`, and an ABSOLUTE
        url overrides the base entirely - origin and all. Every caller in this
        client passes a fixed relative path, but the Bearer credential is
        attached unconditionally a line below, so an absolute (or otherwise
        origin-changing) url would put the token on the wire to another host,
        in the clear if it were plain http. The resolved target is therefore
        re-validated against the SAME policy the constructor used - the whole
        of `usable_base_url`, not the narrower confidentiality rule inside it -
        and required to be the origin the client was built for, BEFORE any
        header is attached.

        Embedded credentials are why the two must be the same policy. The guard
        used to apply only `endpoint_is_confidential`, and it read the origin
        back out of `netloc`, which httpx reports WITHOUT the userinfo. So
        `https://user:pw@same-host/v1/x` passed every check here - same scheme,
        same host, same port, an https origin - and then httpx turned that
        userinfo into a `Basic` credential and OVERWROTE the `Authorization`
        header a line below, silently replacing the device token with someone
        else's. The userinfo is now refused explicitly, on the target, where it
        is still visible; a query or fragment goes with it, because the
        constructor refuses those on an address too.
        """
        target = self._client.base_url.join(url)
        base = self._client.base_url
        if target.userinfo or target.query or target.fragment:
            raise ClientError(ERROR_INSECURE_ENDPOINT)
        same_origin = (
            target.scheme == base.scheme
            and target.host == base.host
            and target.port == base.port
        )
        origin = f"{target.scheme}://{target.netloc.decode('ascii')}"
        if not same_origin or not usable_base_url(origin):
            raise ClientError(ERROR_INSECURE_ENDPOINT)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        matcher_basis: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        # Before the target is resolved, before a header is attached, before
        # anything reaches the network: a client with no configured address
        # has nowhere legitimate to send this, and saying so is the whole
        # point of the unconfigured state.
        if not self._configured:
            raise ClientError(ERROR_NOT_CONFIGURED)
        self._guard_request_target(url)
        try:
            headers = self._headers()
            if matcher_basis:
                headers["X-WuwaTerm-Matcher-Basis"] = "1"
            response = await self._client.request(
                method,
                url,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
                **kwargs,
            )
        except asyncio.CancelledError:
            # A caller cancelled the in-flight task (e.g. the Cancel button).
            # Report it as a distinct, non-alarming state rather than a
            # generic transport failure.
            #
            # NOTE for future callers: the cancellation is CONSUMED here, not
            # re-raised. A task awaiting this method therefore completes with
            # a ClientError instead of being cancelled, so wrapping these
            # calls in asyncio.wait_for or gather(...) will not see normal
            # cancel semantics. That is deliberate - the UI needs a rendered
            # outcome - but it has to be known before it is relied on.
            raise ClientError(ERROR_CANCELLED) from None
        except httpx.TimeoutException as exc:
            raise ClientError(ERROR_TIMEOUT) from exc
        except httpx.TransportError as exc:
            # Includes connect-refused and every other network-level failure.
            raise ClientError(ERROR_OFFLINE) from exc
        except httpx.HTTPError as exc:
            # Everything else httpx can raise, notably a body this client
            # cannot decode because the service or something in front of it
            # produced a malformed compressed response. It is not a transport
            # failure and not a timeout; it is simply unusable.
            raise ClientError(ERROR_UNKNOWN) from exc
        if response.status_code >= 400:
            raise self._error_from_response(response)
        return response

    def _error_from_response(self, response: httpx.Response) -> ClientError:
        try:
            payload = response.json()
            code = payload["error"]["code"]
            request_id = payload.get("request_id")
        except (ValueError, KeyError, TypeError):
            return ClientError(
                self._code_for_status(response.status_code, ERROR_UNKNOWN),
                status_code=response.status_code,
            )
        if not isinstance(code, str):
            return ClientError(
                self._code_for_status(response.status_code, ERROR_UNKNOWN),
                status_code=response.status_code,
            )
        return ClientError(
            self._code_for_status(response.status_code, code),
            request_id=request_id if isinstance(request_id, str) else None,
            status_code=response.status_code,
        )

    @staticmethod
    def _code_for_status(status_code: int, code: str) -> str:
        """The service reports its own deadline as 504 with code `internal`.

        The contract says so deliberately: the status is what distinguishes a
        server-side timeout from a genuine failure, and reusing `internal`
        keeps the code enumeration closed. Rendering "something went wrong on
        the server" for a timeout would misdescribe it to the one person who
        can act on it, so the status wins here.
        """
        if status_code == 504:
            return ERROR_TIMEOUT
        return code

    @staticmethod
    def _parsed(build, payload):
        """Build a result from a body that is only supposed to be ours.

        A 2xx from an unrelated local service - a proxy, another app on the
        port - parses as JSON or not at all, and either way it must arrive as
        the client's own error state rather than as a KeyError from inside a
        view's coroutine.
        """
        try:
            return build(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ClientError(ERROR_UNKNOWN) from exc

    @staticmethod
    def _json(response: httpx.Response):
        try:
            return response.json()
        except (ValueError, httpx.HTTPError) as exc:
            raise ClientError(ERROR_UNKNOWN) from exc

    async def translate(
        self, text: str, *, to: str | None = None
    ) -> TranslationResult:
        body: dict[str, Any] = {"text": text}
        if to is not None:
            body["to"] = to
        response = await self._request(
            "POST",
            "/v1/translations",
            json=body,
            timeout=self._translate_timeout,
        )
        return self._parsed(TranslationResult.from_json, self._json(response))

    async def lookup_terms(self, query: str) -> TermsResult:
        response = await self._request("GET", "/v1/terms", params={"q": query})
        return self._parsed(TermsResult.from_json, self._json(response))

    async def review_pair(
        self, source: str, target: str, direction: str, *,
        alignments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source": source, "target": target, "direction": direction,
            "review_version": "review-v2",
        }
        if alignments is not None:
            body["alignments"] = alignments
        response = await self._request(
            "POST", "/v1/reviews", json=body, matcher_basis=True,
        )
        return self._parsed(
            lambda data: _review_report(data, source, target), self._json(response)
        )

    async def get_meta(self) -> MetaResult:
        response = await self._request("GET", "/v1/meta")
        return self._parsed(MetaResult.from_json, self._json(response))

