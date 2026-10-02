"""FastAPI application for the wuwaterm HTTP adapter.

Design constraints that show up all over this module:

* Plain text only. The Telegram HTML path is an adapter extension injected by
  the bot; this adapter injects no markup translator, so rich-text markup can
  never leak into the HTTP contract.
* Every failure renders the same envelope with an enumerated code
  (see :mod:`wuwaterm_api.errors`), so a client can branch on ``code`` and
  never on prose.
* Budgets are per process. This adapter has its own translator instance,
  its own LLM concurrency slot count and its own per-minute call budget; they
  are never described as global.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect

from wuwaterm.application import (
    ERROR_FORBIDDEN,
    ERROR_LLM_UNAVAILABLE,
    ERROR_INTERNAL,
    ERROR_INVALID_REQUEST,
    ERROR_PAYLOAD_TOO_LARGE,
    ERROR_RATE_LIMITED,
    ERROR_UNAUTHORIZED,
    KIND_ERROR,
    KIND_LLM,
    REVIEW_MAX_SIDE_SCALARS,
    LLMFailureDiagnostic,
    LlmCallBudget,
    LlmCallGuard,
    ReviewRequestError,
    SlidingWindowRateLimiter,
    TranslationJob,
    build_term_service,
    build_translator,
    llm_configured,
    lookup_terms,
    probe_database,
    project_review_report,
    review_pair,
    service_metadata,
    translate_request_async,
)
from wuwaterm.logging_utils import redact_id
from wuwaterm.translation_policy import LLM_INPUT_CHAR_LIMIT

from . import API_VERSION, TERM_QUERY_MAX_LENGTH
from .auth import SCOPE_META, SCOPE_TRANSLATE, TOKEN_SCHEME, Device, DeviceStore
from .errors import MESSAGE_BY_CODE, ApiError, error_body

# Framework-raised routing failures mapped onto the published vocabulary.
_ROUTING_ERROR_CODES = {
    400: ERROR_INVALID_REQUEST,
    401: ERROR_UNAUTHORIZED,
    403: ERROR_FORBIDDEN,
    404: ERROR_INVALID_REQUEST,
    405: ERROR_INVALID_REQUEST,
    413: ERROR_PAYLOAD_TOO_LARGE,
    429: ERROR_RATE_LIMITED,
}

LOGGER = logging.getLogger("wuwaterm_api")

REQUEST_ID_HEADER = "X-Request-Id"

# --------------------------------------------------------------------------
# Log field rendering
#
# A log line is an output channel with the same rules as a response body. One
# value in a request record is chosen by the caller — the target of a request
# that matched no route — and it is read by an operator in a terminal and by
# whatever collects the stream, so it is rendered rather than interpolated.
#
# Two distinct hazards, and `repr` alone covers only the first:
#
# 1. control sequences. A percent-encoded ESC arrives decoded, and
#    `\x1b]0;…\x07` retitles the window `docker logs` is being read in. `repr`
#    escapes every character that is not printable, which is all of C0 and C1,
#    the bidi overrides, the line/paragraph separators, and every space
#    character except U+0020.
# 2. FORGED FIELDS. The record is whitespace-delimited `key=value`, and `repr`
#    leaves U+0020 alone: a target of `/x status=200 device=id:spoofed` would
#    put a caller's own `status=` and `device=` into the line ahead of the real
#    ones. Quotes do not help — nothing splitting on whitespace respects them.
#    So BOTH halves of what makes a field are escaped: the one whitespace
#    character `repr` keeps, which stops a whitespace tokenizer seeing two
#    fields, and `=`, which stops anything scanning for `status=` anywhere in a
#    line finding a caller's copy first. A rendered value can then contain no
#    field at all. The escapes are unambiguous: `repr` has already doubled any
#    backslash in the input, so a single-backslash `\x20` or `\x3d` can only be
#    one these lines introduced.
# --------------------------------------------------------------------------

# Characters that cannot carry an escape sequence and cannot forge a field
# boundary. `=` is excluded for the reason above: `/status=200` needs no space
# to fool a scanner. Anything outside this set is escaped rather than
# enumerated as dangerous.
_PLAIN_LOG_FIELD = re.compile(r"[A-Za-z0-9._:/@+-]+")
# Source characters of a raw target that are considered at all. This is not
# only a display width: it is also the input bound on the credential check's
# fixed-point decoding, which is quadratic in the length it is given (see
# _route_label). Raising it to see more of a scanner's URL raises that work
# roughly with the square, on the event loop, for an unauthenticated caller.
RAW_TARGET_LOG_LIMIT = 80
# The method is recorded from a CLOSED set rather than as it arrived. A method
# is a caller-chosen token, and this service publishes exactly GET and POST —
# every other verb is already refused with a 405, so the exact spelling of one
# has no operational value, while a free-text field that a caller fills is a
# place for anything at all to end up. Recording membership instead of content
# leaves `route`'s fallback as the only caller-influenced value in the record.
KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)
OTHER_METHOD = "other"
# And a cap on what is actually WRITTEN. Clipping the source alone does not
# bound the line: one character can render as ten (`\U000e0001`), so eighty
# source characters can become eight hundred and push the fields that matter
# off the end of a terminal. Both bounds are needed — the first keeps the value
# meaningful, the second keeps the record readable.
RENDERED_LOG_LIMIT = 160
# Marks a rendering that hit that cap. Not a space and not `=`, so it cannot
# become a field boundary of its own.
TRUNCATION_MARK = "~"
# What a record names when no device was authenticated. Not `redact_id(None)`:
# that renders as a digest and would read like a principal.
NO_PRINCIPAL = "-"
# Recorded when the caller went away before there was a response. This service
# never SENDS it — nothing was sent — but a record has to say something, and
# saying 500 would send an operator looking for a server fault that did not
# happen. 499 is the long-standing convention for exactly this, so it reads
# correctly to anyone who has met it and is obviously not one of ours.
CLIENT_GONE_STATUS = 499

# A request target is caller-supplied and can therefore carry a CREDENTIAL: a
# client or a proxy that puts a token in the URL instead of the Authorization
# header produces a target that is otherwise perfectly ordinary to look at, and
# escaping is reversible, so recording it escaped would still write the secret
# down. Every token of this service begins with the scheme, so a target that
# contains it is not recorded at all. The query string is never recorded on any
# path, so nothing there needs the same treatment.
#
# Recorded as a bare label with NO digest of the target. A digest would group
# repeats, which is mildly useful, but `redact_id` falls back to an unkeyed
# SHA-256 prefix when no redaction secret is configured — and the API container
# blanks that variable on purpose, because it keys the BOT's redaction. That
# would turn a leaked line into a cheap offline check against guessed secrets,
# sidestepping the deliberately expensive derivation the credential store uses.
# The actionable fact is that a credential reached a URL at all; nothing about
# WHICH one belongs in a log.
_CREDENTIAL_MARKER = f"{TOKEN_SCHEME}."
CREDENTIAL_SHAPED_TARGET = "credential-shaped"
# Percent-decoding is the one transform between the wire and the decoded path,
# and the server applies exactly one round of it — so `%2577td1.` arrives as
# `%77td1.` and a literal search for the marker misses it. The check therefore
# decodes to a FIXED POINT, with no round cap: a cap is just a deeper spelling
# to encode past, which is the failure this whole check exists to stop
# repeating. Termination is structural rather than budgeted — a decode that
# changes anything replaces at least one three-character escape with one
# character, so the string strictly shortens until it stops changing.


def service_version() -> str:
    try:
        return package_version("wuwaterm")
    except PackageNotFoundError:  # pragma: no cover - only in odd installs
        return "unknown"


# --------------------------------------------------------------------------
# Wire models (field names ARE the wire names; no renaming layer anywhere)
# --------------------------------------------------------------------------


class TranslationRequestBody(BaseModel):
    text: str = Field(
        description=(
            "Source text to translate. Plain text only. Clients should send "
            "at most 2,000 characters. The server applies its authoritative "
            "limit after text preparation, so a normalized superset may be "
            "accepted; callers must not depend on that permissiveness."
        ),
    )
    to: Literal["en", "zh"] | None = Field(
        default=None,
        description=(
            "Force the target language. Omit or send null to auto-detect from "
            "the source text."
        ),
    )


class TranslationResponseBody(BaseModel):
    kind: Literal["noop", "exact", "fuzzy", "llm"] = Field(
        description=(
            "Which stage answered: noop (nothing translatable), exact "
            "(official dictionary hit), fuzzy (trusted pinyin hit) or llm."
        )
    )
    text: str = Field(description="Translated text.")
    direction: Literal["en", "zh"] = Field(description="Target language used.")
    dictionary_miss: bool = Field(
        description=(
            "True when a short query was answered by the model with no "
            "official term locked, so the answer is not authoritative."
        )
    )
    request_id: str


class TermMatchBody(BaseModel):
    zh: str
    en: str
    category: str
    score: float
    reason: str


class TermsResponseBody(BaseModel):
    query: str
    matches: list[TermMatchBody]
    request_id: str


class MetaResponseBody(BaseModel):
    service_version: str
    api_version: str
    schema_version: str | None
    source_profile: str | None
    source_commit: str | None
    term_count: int
    llm_configured: bool
    request_id: str


class HealthResponseBody(BaseModel):
    status: Literal["ok", "ready"]


class OfficialPairResolutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mention_id: str
    choice: Literal["official_pair"]
    zh: str
    en: str


class NotATermResolutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mention_id: str
    choice: Literal["not_a_term"]


class V2OfficialPairResolutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mention_id: str
    choice: Literal["official_pair"]
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewAlignmentSpanInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)
    text: str


class ReviewAlignmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: ReviewAlignmentSpanInputBody
    target: ReviewAlignmentSpanInputBody | None


class ReviewResolutionContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_version: Literal["review-v2"]
    dictionary_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_version: Literal["review-v1", "review-v2"] = "review-v1"
    source: str = Field(
        min_length=1,
        description="Submitted source text. At most 2,000 Unicode scalars.",
    )
    target: str = Field(
        min_length=1,
        description="Submitted translation. At most 2,000 Unicode scalars.",
    )
    direction: Literal["en", "zh"] = Field(
        description="Target language of the submitted translation (same meaning as translations.to)."
    )
    resolutions: (
        list[
            OfficialPairResolutionBody
            | V2OfficialPairResolutionBody
            | NotATermResolutionBody
        ]
        | None
    ) = None
    alignments: list[ReviewAlignmentBody] | None = Field(default=None, max_length=64)
    resolution_context: ReviewResolutionContextBody | None = None

    @model_validator(mode="after")
    def validate_versioned_shape(self) -> "ReviewRequestBody":
        resolutions = self.resolutions or []
        if self.review_version == "review-v1":
            if {"alignments", "resolution_context"} & self.model_fields_set:
                raise ValueError("review-v1 does not accept v2 request fields")
            if any(
                isinstance(item, V2OfficialPairResolutionBody) for item in resolutions
            ):
                raise ValueError("review-v1 official_pair requires zh and en")
            return self

        for field in ("alignments", "resolution_context", "resolutions"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"review-v2 {field} must not be null")
        if any(isinstance(item, OfficialPairResolutionBody) for item in resolutions):
            raise ValueError("review-v2 official_pair requires candidate_id")
        if resolutions and self.resolution_context is None:
            raise ValueError("review-v2 resolutions require resolution_context")

        previous_source_end = -1
        previous_target_end = -1
        for alignment in self.alignments or []:
            source_span = alignment.source
            if (
                source_span.start >= source_span.end
                or source_span.end > len(self.source)
                or self.source[source_span.start : source_span.end] != source_span.text
                or source_span.start < previous_source_end
            ):
                raise ValueError("invalid source alignment span")
            previous_source_end = source_span.end
            target_span = alignment.target
            if target_span is None:
                continue
            if (
                target_span.start >= target_span.end
                or target_span.end > len(self.target)
                or self.target[target_span.start : target_span.end] != target_span.text
                or target_span.start < previous_target_end
            ):
                raise ValueError("invalid target alignment span")
            previous_target_end = target_span.end
        return self


class ReviewSpanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(
        description="Half-open start offset in Unicode scalars of the submitted side."
    )
    end: int = Field(
        description="Half-open end offset in Unicode scalars of the submitted side."
    )
    text: str = Field(
        description="Exact substring covering [start, end) on the submitted side."
    )


class ReviewSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_file: str
    source_id: str


class ReviewCandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zh: str
    en: str
    category: str
    sources: list[ReviewSourceBody]
    candidate_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewFindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    verdict: Literal[
        "verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated"
    ]
    rule_id: str
    source_span: ReviewSpanBody | None
    target_span: ReviewSpanBody | None
    candidates: list[ReviewCandidateBody]
    candidates_truncated: bool


class ReviewDictionaryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str | None
    source_commit: str | None
    term_count: int
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewCoverageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluated: int
    not_evaluated: int
    rules: list[str]


class ReviewResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    source_revision: str
    target_revision: str
    rule_version: str
    dictionary: ReviewDictionaryBody
    coverage: ReviewCoverageBody
    findings: list[ReviewFindingBody]
    truncated: bool


# The wire vocabulary, spelled out so a generated client can model every
# branch exhaustively and so the contract gate notices a code being added or
# removed. It must stay equal to the application layer's set.
ErrorCode = Literal[
    "unauthorized",
    "forbidden",
    "rate_limited",
    "payload_too_large",
    "invalid_request",
    "input_too_long",
    "llm_unavailable",
    "llm_budget_exhausted",
    "internal",
]


class ErrorDetailBody(BaseModel):
    code: ErrorCode = Field(
        description="Enumerated, stable failure classification."
    )
    message: str = Field(description="Short operator-facing text.")


class ErrorResponseBody(BaseModel):
    error: ErrorDetailBody
    request_id: str


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponseBody, "description": "Invalid request"},
    401: {"model": ErrorResponseBody, "description": "Missing or invalid credential"},
    403: {"model": ErrorResponseBody, "description": "Scope not granted"},
    413: {"model": ErrorResponseBody, "description": "Body too large"},
    422: {"model": ErrorResponseBody, "description": "Input rejected"},
    429: {"model": ErrorResponseBody, "description": "Rate limited"},
    500: {"model": ErrorResponseBody, "description": "Internal error"},
    503: {
        "model": ErrorResponseBody,
        "description": (
            "A dependency this request needs is temporarily unavailable: the"
            " translation model, the credential store, or the post-admission"
            " device re-check. Classified as `llm_unavailable` or `internal`;"
            " the 503 status is what distinguishes it from a genuine 500."
            " Retryable."
        ),
    },
    504: {
        "model": ErrorResponseBody,
        "description": (
            "Request exceeded the server time budget. Classified as `internal`:"
            " the enumerated code set is closed, so the HTTP status is what"
            " distinguishes a timeout from a genuine 500."
        ),
    },
}

# The bearer scheme is declared so a generated client can configure the
# credential. auto_error is off because this API answers with its own envelope
# instead of the framework's default body.
BEARER_SCHEME = HTTPBearer(auto_error=False, description="Device token.")


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _log_field(value: object, limit: int) -> str:
    """Render a caller-influenced value as one inert, unsplittable token."""
    text = str(value)
    clipped = text[:limit]
    if clipped == text and _PLAIN_LOG_FIELD.fullmatch(text):
        return text
    # The two replacements are not cosmetic: see the forged-fields hazard above.
    rendered = repr(clipped).replace(" ", "\\x20").replace("=", "\\x3d")
    # Marked when EITHER bound bit. Escaping alone does not say "shortened" —
    # a value can be escaped for having a control character in it and still be
    # complete — so without this a clipped target reads as the whole of what
    # arrived, which is the one thing the bound exists to prevent.
    if clipped != text or len(rendered) > RENDERED_LOG_LIMIT:
        rendered = rendered[:RENDERED_LOG_LIMIT] + TRUNCATION_MARK
    return rendered


def _could_carry_a_credential(path: str) -> bool:
    """Whether the part of a target that could be LOGGED could be a token.

    Decoded to a fixed point first. The server percent-decodes once, so a
    caller (or a proxy tidying a URL) that encodes the scheme twice hands us
    `%77td1.` — which a literal search does not see, while one more decode
    recovers a working credential from the recorded line. Matching the family
    rather than one spelling of it is the whole point.

    Called with the prefix that could be WRITTEN, never the whole target: see
    _route_label for why that is both sufficient and necessary.
    """
    seen = path
    while True:
        if _CREDENTIAL_MARKER in seen.lower():
            return True
        decoded = unquote(seen)
        if decoded == seen:
            return False
        seen = decoded


def _route_label(request: Request) -> str:
    """Three cases, in this order.

    1. the request matched a route → its TEMPLATE. Repository text: it cannot
       carry anything, and it gives one record shape per endpoint instead of
       one per spelling a caller invents. An unsupported METHOD on a known
       route is one of these — ``APIRoute.matches`` records a partial match
       before the method is refused, so a 405 is named by its template.
    2. it matched nothing and could be a credential → a fixed label, below.
    3. anything else → the decoded target, which is the one identifying thing
       left, rendered so that it cannot carry an escape sequence, forge a
       field, or run past the line. A target that needs none of that is
       recorded as it stands; the guarantee is about the FORM reaching the
       record, not about hiding what a caller wrote in its own request.

    The framework's automatic trailing-slash redirect is case 3, not case 1: it
    is produced without a route ever being matched. So is ``/openapi.json``,
    which is registered as a plain route with no ``matches`` override — its
    fallback rendering is byte-identical to its template, so the record reads
    the same either way.
    """
    template = getattr(request.scope.get("route"), "path", None)
    if isinstance(template, str) and template:
        return template
    path = str(request.scope.get("path", ""))
    # The credential check reads only the prefix that can reach the record, and
    # that is exactly right in both directions. SUFFICIENT: what a reader can
    # recover from the rendered value is this prefix and nothing else, so a
    # credential outside it is already unrecoverable from the log. NECESSARY:
    # decoding the whole target is quadratic — one nested layer is removed per
    # pass while every pass rescans the rest — and this runs on the event loop
    # for an unauthenticated request, so a padded target would be a way to stall
    # the process rather than merely to be logged oddly.
    if _could_carry_a_credential(path[:RAW_TARGET_LOG_LIMIT]):
        return CREDENTIAL_SHAPED_TARGET
    return _log_field(path, RAW_TARGET_LOG_LIMIT)


def _method_label(request: Request) -> str:
    """The request method when it is one, else that it was not one of them."""
    method = str(request.method)
    return method if method in KNOWN_METHODS else OTHER_METHOD


def _log_principal(request: Request) -> str:
    """The redacted device principal, or ``-`` when none was authenticated.

    Always the redaction helper's output. A raw device id is a stable
    identifier for a person's machine and belongs in the credential store, not
    in an operations log.

    This is a rule about what the SERVICE writes down of what it knows. It is
    not, and cannot be, a rule about byte sequences: a caller may put anything
    in its own request target, including sixteen hexadecimal characters, and a
    record of that discloses nothing the caller did not already have. Trying to
    recognise identifier-shaped substrings in caller data would trade real
    diagnostic value for an enumeration that never closes.
    """
    device = getattr(request.state, "device", None)
    if device is None:
        return NO_PRINCIPAL
    return redact_id(device.device_id)


def _log_request_completed(
    request: Request, request_id: str, status_code: int, elapsed_seconds: float
) -> None:
    """The one record per request an operator correlates a client report with.

    Fields, in order: the server-minted correlation id, the method, the route,
    the status, how long it took, and which device asked. That is deliberately
    everything and nothing more — no request text, no credential, no header a
    caller controls.

    ``duration_ms`` is measured to the point the response is ready, not to the
    last byte the client receives: the middleware that times it hands the
    response on to be sent. Every response this service produces is a small
    complete JSON document, so the difference is transmission time, and what
    the number is useful for — which stage of the pipeline the request spent
    its time in — is on this side of it either way.
    """
    LOGGER.info(
        "request complete request_id=%s method=%s route=%s status=%s "
        "duration_ms=%.1f device=%s",
        request_id,
        _method_label(request),
        _route_label(request),
        status_code,
        elapsed_seconds * 1000.0,
        _log_principal(request),
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Mint the correlation id, and record how the request ended.

    A caller-supplied ``X-Request-Id`` used to be accepted when it matched a
    charset that ALSO matches the token shape ``wtd1.<id>.<secret>``, and it was
    then echoed into the auth-reject log line and into the HTTP error envelope.
    A request could therefore route its own credential straight into the logs
    and into a response body. The correlation id is now always generated
    server-side and the inbound header is ignored entirely, so nothing a caller
    sends can be logged or echoed. The generated id is still returned in the
    response header for correlation.

    The completion record lives HERE rather than in a middleware of its own for
    two reasons. This is the outermost layer, so a request refused by the body
    cap or the time budget — before anything is routed — is recorded exactly
    like one that reached a handler; and the id being recorded is the same
    object this method minted, so a client's report and the server's record
    cannot drift apart through a second lookup.

    The record is emitted from ``finally`` because the application's own
    ``Exception`` handler runs OUTSIDE every user middleware: an unhandled
    failure passes straight through this method, and the request most worth
    having a record of would otherwise be the one that produces none.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = _new_request_id()
        request.state.request_id = request_id
        started = time.perf_counter()
        # What the caller gets when the exception escapes to the handler
        # outside this middleware; overwritten as soon as a response exists.
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        except (ClientDisconnect, asyncio.CancelledError):
            # The caller went away, or the work was cancelled out from under
            # this task at shutdown. Either way nothing was sent, so the seeded
            # 500 would be a lie that costs an operator a hunt for a fault that
            # never happened. Measured rather than assumed: a socket hang-up
            # during the body read surfaces here as ClientDisconnect, not as
            # cancellation. Re-raised untouched — this changes what the record
            # says, not what the request does.
            status_code = CLIENT_GONE_STATUS
            raise
        finally:
            try:
                _log_request_completed(
                    request, request_id, status_code, time.perf_counter() - started
                )
            except Exception:
                # An exception raised from a `finally` REPLACES the one
                # propagating through it, so a fault while describing a request
                # would become the fault reported for it — the traceback the
                # operator needs, swapped for the one about writing it down.
                # The record is additive by construction: it can be missing, it
                # can never be the thing that goes wrong.
                #
                # Reported WITH the id and the cause: without the id this line
                # says only that some request went unrecorded, and the cause is
                # what makes it actionable. Neither can leak — every value in
                # the record is sanitised by _log_field/_log_principal BEFORE
                # LOGGER.info is reached, so nothing a caller supplied is in
                # flight by the time an exception can carry it.
                #
                # And suppressed, because the fallback goes through the SAME
                # handler chain that just failed. `logging.Handler.handle` does
                # not wrap `filter` or `emit`; it is the stdlib handler
                # implementations that catch their own errors. A handler this
                # service did not install — configure_logging deliberately does
                # not force one — can therefore raise from both calls, and the
                # second one would escape the finally and become the request's
                # outcome. Suppressing Exception and not BaseException keeps
                # cancellation propagating.
                with contextlib.suppress(Exception):
                    LOGGER.warning(
                        "request record could not be written request_id=%s",
                        request_id,
                        exc_info=True,
                    )


def _error_response(exc: ApiError, request: Request) -> JSONResponse:
    """Render the stable envelope from middleware.

    Middleware sits OUTSIDE the exception-handler middleware, so a raised
    ApiError would surface as a bare 500 here; middleware returns the response
    itself instead.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, _request_id(request)),
    )


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Bound both the size and the arrival time of a request body.

    Size alone is not enough: a caller can stay under the cap and still hold a
    request open by trickling bytes. The read therefore carries its own
    deadline, because the body arrives before the handler timeout can apply to
    it.
    """

    def __init__(self, app, max_body_bytes: int, read_timeout_seconds: float):
        super().__init__(app)
        self.max_body_bytes = max_body_bytes
        self.read_timeout_seconds = read_timeout_seconds

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                return _error_response(
                    ApiError(ERROR_INVALID_REQUEST, "content-length is not a number"),
                    request,
                )
            if declared_length < 0:
                return _error_response(
                    ApiError(ERROR_INVALID_REQUEST, "content-length is negative"),
                    request,
                )
            if declared_length > self.max_body_bytes:
                return _error_response(ApiError(ERROR_PAYLOAD_TOO_LARGE), request)
        # A caller can omit content-length or use chunked encoding, so the
        # header is a hint, not the enforcement point. Read incrementally and
        # abort at the cap, so an unauthenticated caller can never make this
        # process hold more than max_body_bytes of request payload.
        received = bytearray()
        try:
            async with asyncio.timeout(self.read_timeout_seconds):
                async for chunk in request.stream():
                    received.extend(chunk)
                    if len(received) > self.max_body_bytes:
                        return _error_response(
                            ApiError(ERROR_PAYLOAD_TOO_LARGE), request
                        )
        except (asyncio.TimeoutError, TimeoutError):
            LOGGER.warning(
                "request body read timed out route=%s request_id=%s",
                _route_label(request),
                _request_id(request),
            )
            return _error_response(
                ApiError(
                    ERROR_INTERNAL,
                    "request body did not arrive within the server time budget",
                    status_code=504,
                ),
                request,
            )
        # Hand the bytes we consumed back to the cached request so the route
        # still sees its own body.
        request._body = bytes(received)
        return await call_next(request)


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Bound the wall-clock cost of a single request."""

    def __init__(self, app, timeout_seconds: float):
        super().__init__(app)
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, request: Request, call_next):
        try:
            return await asyncio.wait_for(call_next(request), self.timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            LOGGER.warning(
                "request timed out route=%s request_id=%s",
                _route_label(request),
                _request_id(request),
            )
            return _error_response(
                ApiError(
                    ERROR_INTERNAL,
                    "request exceeded the server time budget",
                    status_code=504,
                ),
                request,
            )


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unset")


def _log_llm_failure_diagnostic(request: Request, outcome: object) -> None:
    """Add safe failure detail without allowing logging to replace the outcome."""
    try:
        diagnostic = getattr(outcome, "llm_failure", None)
        if diagnostic is None:
            return
        # Do not interpolate arbitrary outcome metadata or its repr. The core
        # record validates fixed categories and bounded counts on construction.
        if type(diagnostic) is not LLMFailureDiagnostic:
            diagnostic = LLMFailureDiagnostic(reason="unknown")
        LOGGER.warning(
            "llm translation diagnostic request_id=%s reason=%s detail=%s "
            "expected_count=%s actual_count=%s",
            _request_id(request),
            diagnostic.reason,
            diagnostic.detail,
            diagnostic.expected_count if diagnostic.expected_count is not None else "-",
            diagnostic.actual_count if diagnostic.actual_count is not None else "-",
        )
    except Exception:
        # A failing logging sink must not turn an existing 503 into a 500, or
        # emit exception text that could contain a sink's sensitive context.
        pass


class CredentialPoolClosed(RuntimeError):
    """The credential-store worker pool has been shut down (app teardown).

    Grouped with ``sqlite3.Error``/``OSError`` at every call site: from the
    request path's point of view it is one more way for the credential store to
    be momentarily unusable, which is 503 — never 401, and never a bare 500.
    """


def _new_credential_pool(max_workers: int) -> concurrent.futures.ThreadPoolExecutor:
    """A pool whose WIDTH is the bound on concurrent credential-store work."""
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="wuwaterm-credential"
    )


async def _in_credential_pool(app: FastAPI, func, *args):
    """Run a credential-store call on this app's OWN bounded thread pool.

    Every blocking call in this process used to go to asyncio's process-wide
    DEFAULT executor, which is also where the UNAUTHENTICATED ``/readyz`` probe
    and the dictionary stage run. Two unauthenticated probes were enough to
    occupy it, and credential verifications then sat in its queue holding
    admission slots — so unauthenticated traffic made the owner's own valid
    token answer 429. Isolating the credential store fixes that at the root:
    nothing an unauthenticated caller can schedule shares these workers.

    The pool ALSO carries the bound. The admission semaphore alone could not:
    when an awaiting task is cancelled its slot is released immediately (it has
    to be, or a verification cancelled while still queued would strand the slot
    forever), while the worker it started keeps running — so under a flood of
    cancellations the number of scrypt derivations actually running at once
    drifted well above the configured maximum. ``max_workers`` is not
    releasable by anything the caller does, so the real bound now holds under
    cancellation. The semaphore keeps its other job: non-queuing ADMISSION, so
    a saturated verifier sheds with 429 before scheduling any scrypt at all.
    """
    loop = asyncio.get_running_loop()
    try:
        pending = loop.run_in_executor(app.state.auth_pool, func, *args)
    except RuntimeError as exc:
        # The pool has been shut down: this request arrived during or after
        # teardown. To every caller here that is the credential store being
        # momentarily unusable — the same class as a locked database — so it
        # is re-raised as something their existing handling already covers,
        # rather than escaping as an unhandled 500. Submission is SYNCHRONOUS,
        # so this catches only the submit; a RuntimeError raised inside `func`
        # surfaces at the await below and is deliberately left alone.
        raise CredentialPoolClosed(str(exc)) from None
    return await pending


async def _require_active_device(
    request: Request, device: Device, *, serve_on_store_error: bool = False
) -> None:
    """Refuse if the device was revoked while this request was in flight.

    Verification takes a snapshot of the device; a revocation that commits
    after it but before an expensive call or before the response would
    otherwise be served on a credential that is no longer valid. This is a
    cheap read run at the TOCTOU seams: before the model call and before
    returning. It NARROWS the window best-effort; it does not close it — a
    revocation that commits after this read is still served.

    ``serve_on_store_error`` controls what a TRANSIENT store failure (database
    locked, disk I/O) means at each seam. Before the model call it is False:
    fail closed with 503, since nothing has been spent yet. After the model
    call it is True: the LLM budget slot and the round trip are already spent,
    so a store read hiccup must not discard a completed translation and invite
    a second paid retry — log it and serve. Either way a DEFINITIVE ``False``
    (a real revocation) still rejects with 401.
    """
    try:
        active = await _in_credential_pool(
            request.app,
            request.app.state.device_store.is_active,
            device.device_id,
        )
    except (sqlite3.Error, OSError, CredentialPoolClosed):
        # A transient store failure on the POST-auth re-check (database is
        # locked, a disk I/O error) is infrastructure, not a credential
        # rejection: answering 401 here would tell a valid device to re-pair.
        LOGGER.warning(
            "device re-check store error route=%s request_id=%s serve=%s",
            _route_label(request),
            _request_id(request),
            serve_on_store_error,
        )
        if serve_on_store_error:
            return
        raise ApiError(
            ERROR_INTERNAL,
            "device re-check is temporarily unavailable",
            status_code=503,
        )
    if not active:
        LOGGER.info(
            "device revoked in flight route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(ERROR_UNAUTHORIZED)


def _device_llm_guard(request: Request, device: Device) -> LlmCallGuard:
    """Recheck the device, then spend the shared budget inside the model slot."""
    async def before_llm_call() -> None:
        await _require_active_device(request, device)
        request.app.state.llm_budget()

    return before_llm_call


async def authenticated_device(
    request: Request,
    presented: Annotated[
        HTTPAuthorizationCredentials | None, Depends(BEARER_SCHEME)
    ] = None,
) -> Device:
    if presented is None or presented.scheme.lower() != "bearer":
        raise ApiError(ERROR_UNAUTHORIZED)
    store: DeviceStore = request.app.state.device_store
    slots = request.app.state.auth_slots
    token = presented.credentials.strip()
    if not slots.acquire(blocking=False):
        # The bounded verification pool is full. Shed load HERE, before
        # scheduling another expensive scrypt derivation: an unauthenticated
        # caller must not be able to make the credential check itself the load,
        # and queuing these requests behind the semaphore would do exactly
        # that. Non-queuing admission — the rate-limited code tells the caller
        # to back off, and no worker thread is ever left blocked on the slot.
        LOGGER.info(
            "auth admission shed route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(ERROR_RATE_LIMITED)
    # The slot is owned by THIS coroutine and released here, on the loop, not
    # inside the worker: if the awaiting task is cancelled (time budget, client
    # disconnect) while the verification job is still QUEUED, the worker never
    # runs — so a worker-side release would leak the slot permanently and wedge
    # every later request into 429. Releasing early does relax the ADMISSION
    # count while a started worker finishes; the pool's max_workers is what
    # actually bounds concurrent derivations, and that cannot be released.
    try:
        device = await _in_credential_pool(request.app, store.authenticate, token)
    except (sqlite3.Error, OSError, CredentialPoolClosed):
        # The verification READ itself failed (database is locked, a disk I/O
        # error). That is the store being momentarily unusable, not the
        # credential being wrong — answer 503 rather than telling a valid device
        # to re-pair. It leaks nothing probeable: an unreadable store is
        # device-independent, so the response does not vary with the token.
        LOGGER.warning(
            "credential store error on verification route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(
            ERROR_INTERNAL,
            "credential store is temporarily unavailable",
            status_code=503,
        )
    finally:
        slots.release()
    if device is None:
        LOGGER.info(
            "auth rejected route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(ERROR_UNAUTHORIZED)
    # The credential is now verified, so the principal is known. Recording it
    # HERE rather than after admission is what lets the completion record name
    # the device on the outcomes an operator most wants attributed — the
    # per-device rate limit below, a 403 outside the granted scopes, a
    # revocation caught in flight. The admission shed above is NOT one of them:
    # it fires before anything is verified, so there is no principal to name
    # and it is recorded with none. Nothing branches on this attribute; it
    # exists to be logged.
    request.state.device = device
    limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    if not limiter.allow(device.device_id):
        LOGGER.info(
            "rate limited device=%s request_id=%s",
            redact_id(device.device_id),
            _request_id(request),
        )
        # Deliberately BEFORE record_use: a refused request must not be able to
        # drive an unbounded stream of writes into the credential store.
        raise ApiError(ERROR_RATE_LIMITED)
    # record_use only stamps a row that is still active. A zero count means the
    # device was revoked between verification and admission: reject it now
    # rather than serve a request for a credential that is no longer valid. A
    # transient store failure on this write is infrastructure, not a credential
    # problem, so it becomes 503 rather than a misleading unauthorized/500.
    try:
        updated = await _in_credential_pool(
            request.app, store.record_use, device.device_id
        )
    except (sqlite3.Error, OSError, CredentialPoolClosed):
        LOGGER.warning(
            "credential store error on admission route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(
            ERROR_INTERNAL,
            "credential store is temporarily unavailable",
            status_code=503,
        )
    if updated != 1:
        LOGGER.info(
            "auth rejected: device revoked in flight route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        raise ApiError(ERROR_UNAUTHORIZED)
    return device


def require_scope(scope: str):
    async def dependency(
        device: Annotated[Device, Depends(authenticated_device)],
    ) -> Device:
        if not device.has_scope(scope):
            raise ApiError(ERROR_FORBIDDEN)
        return device

    return dependency


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------


OPENAPI_DESCRIPTION = """
Read-only translation surface for the Wuthering Waves terminology service.

This API uses the application pipeline also used by Telegram command routes
and the in-process private web layer: official dictionary hit first, then a
trusted pinyin hit, then a term-locked model call. Linked-channel
auto-translation has specialized channel orchestration and is not part of this
API contract. Responses are plain text; rich-text markup is never part of this
contract.

Authentication is a bearer device token issued by the operator. There is no
registration endpoint: tokens are created out of band and can be revoked.
"""


def _apply_openapi_client_limits(document: dict[str, Any]) -> dict[str, Any]:
    """Publish schema-only limits after either Pydantic generation path."""
    text_schema = document["components"]["schemas"]["TranslationRequestBody"][
        "properties"
    ]["text"]
    review_properties = document["components"]["schemas"]["ReviewRequestBody"][
        "properties"
    ]
    # None is an internal omitted-field sentinel, not an accepted wire value.
    for field in ("alignments", "resolution_context"):
        schema = review_properties.get(field)
        if not isinstance(schema, dict):
            continue
        variants = schema.pop("anyOf", [])
        non_null = [variant for variant in variants if variant.get("type") != "null"]
        if len(non_null) == 1:
            schema.update(non_null[0])
        schema.pop("default", None)
    parameters = document["paths"][f"/{API_VERSION}/terms"]["get"]["parameters"]
    try:
        query_schema = next(
            parameter["schema"] for parameter in parameters if parameter["name"] == "q"
        )
    except StopIteration as exc:
        raise KeyError("OpenAPI terms query parameter q is missing") from exc

    for schema, limit in (
        (text_schema, LLM_INPUT_CHAR_LIMIT),
        (query_schema, TERM_QUERY_MAX_LENGTH),
        (review_properties["source"], REVIEW_MAX_SIDE_SCALARS),
        (review_properties["target"], REVIEW_MAX_SIDE_SCALARS),
    ):
        nested = schema.get("json_schema_extra")
        if isinstance(nested, dict) and "maxLength" in nested:
            remaining = {key: value for key, value in nested.items() if key != "maxLength"}
            if remaining:
                schema["json_schema_extra"] = remaining
            else:
                schema.pop("json_schema_extra")
        schema["maxLength"] = limit
    return document


def _install_openapi_client_limits(app: FastAPI) -> None:
    render_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _apply_openapi_client_limits(render_openapi())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def create_app(
    settings=None,
    *,
    device_store: DeviceStore | None = None,
    term_service=None,
    translator=None,
) -> FastAPI:
    from .settings import ApiSettings

    resolved = settings or ApiSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A pool is created in create_app so an application that never runs a
        # lifespan (the OpenAPI renderer, in-process tests) still has one — but
        # shutdown() is PERMANENT, so the pool cannot also be per-application.
        # An app started twice (two sequential TestClient contexts, an
        # embedding that cycles the ASGI lifespan) would reach a dead executor
        # and answer every credentialed request with a 500. Each cycle gets its
        # own; the one being replaced may have started workers (an app driven
        # through ASGITransport with no lifespan uses it), and is ended without
        # waiting because startup must not block on a join.
        previous = getattr(app.state, "auth_pool", None)
        app.state.auth_pool = _new_credential_pool(resolved.auth_max_concurrency)
        if previous is not None:
            previous.shutdown(wait=False, cancel_futures=True)
        # try/finally, not a bare yield: asynccontextmanager THROWS into the
        # generator when the surrounding context exits with an exception, so a
        # server that fails during its lifespan would otherwise skip teardown
        # entirely and leave the pool's worker threads (and their SQLite
        # handles) to the interpreter's atexit hook. The pool shutdown is
        # nested in its own finally so a translator that raises on close cannot
        # take it with it.
        try:
            yield
        finally:
            closer = getattr(app.state.translator, "aclose", None)
            try:
                if closer is not None:
                    await closer()
            finally:
                # Drop anything still queued and join the workers that already
                # started, off the loop so the shutdown does not block it.
                await asyncio.to_thread(
                    app.state.auth_pool.shutdown, True, cancel_futures=True
                )

    app = FastAPI(
        title="wuwaterm API",
        version=service_version(),
        description=OPENAPI_DESCRIPTION.strip(),
        openapi_url="/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.device_store = device_store or DeviceStore(
        resolved.device_db_path,
        guard_legacy_default=resolved.device_db_is_default,
    )
    app.state.term_service = term_service or build_term_service(resolved.db_path)
    app.state.translator = translator or build_translator(
        resolved.db_path,
        timeout=resolved.llm_timeout_seconds,
        max_concurrency=resolved.llm_max_concurrency,
        en_zh_protocol=True,
    )
    app.state.rate_limiter = SlidingWindowRateLimiter(
        limit=resolved.rate_limit_per_minute
    )
    app.state.llm_budget = LlmCallBudget(resolved.llm_calls_per_minute)
    # Two halves of one mechanism, both sized by auth_max_concurrency:
    # `auth_slots` decides ADMISSION without queuing (full -> 429, and no
    # scrypt is ever scheduled), `auth_pool` is the BOUND on how many
    # credential-store calls actually run at once. See _in_credential_pool for
    # why neither alone is enough. Threads are created on demand, so an app
    # that never authenticates never starts one.
    app.state.auth_slots = threading.BoundedSemaphore(resolved.auth_max_concurrency)
    app.state.auth_pool = _new_credential_pool(resolved.auth_max_concurrency)

    # Added last == outermost: the request id wraps everything, so even a
    # failure produced by an inner middleware carries it. The body read and the
    # handler each carry the same time budget, applied where each of them
    # actually runs.
    app.add_middleware(TimeoutMiddleware, timeout_seconds=resolved.request_timeout_seconds)
    app.add_middleware(
        BodyLimitMiddleware,
        max_body_bytes=resolved.max_body_bytes,
        read_timeout_seconds=resolved.request_timeout_seconds,
    )
    app.add_middleware(RequestIdMiddleware)

    _register_error_handlers(app)
    _register_routes(app)
    # The owner-private web presentation layer, mounted LAST and only when it
    # is switched on. Default off (see settings.DEFAULT_WEB_ENABLED): with the
    # switch off nothing is mounted at all, so the route table, the OpenAPI
    # document and the behaviour of every existing endpoint are byte-for-byte
    # what they were before this layer existed. That is the property that makes
    # "no regression to the API" checkable rather than merely asserted.
    #
    # Imported here rather than at module scope because the sub-application
    # imports helpers from THIS module; deferring it to call time keeps the
    # dependency one-directional.
    if resolved.web_enabled:
        from starlette.routing import Route as _PlainRoute

        from .web.app import WebSurfaceEnvelope, bare_mount_guard, create_web_app
        from .settings import WEB_MOUNT_PATH

        # OUTERMOST, so it wraps the body-limit and timeout middleware and can
        # see the responses THEY synthesise without entering the sub-app. A
        # strict no-op for every path outside the mount, so the existing API's
        # behaviour is unchanged; only added when the layer is switched on, so
        # the default deployment does not gain a middleware at all.
        app.add_middleware(WebSurfaceEnvelope)

        app.mount(WEB_MOUNT_PATH, create_web_app(app))
        # The mount path WITHOUT its trailing slash, registered FIRST so it
        # matches before this router's own slash redirect can answer it. A
        # Mount matches only the slash-prefixed remainder, so `/wuwaterm-web`
        # would otherwise be handled by the parent's redirect_slashes and
        # answer 307 to a caller that never presented the edge marker —
        # re-opening, one level up, exactly the existence oracle the
        # sub-application closes. A plain Route, not an APIRoute, so the
        # published OpenAPI document is still untouched.
        # No `methods=`: the endpoint is an ASGI callable, so Starlette applies
        # NO method filter and this matches every verb. Passing a method list
        # here is what left the first version of this fix answering 405 (from
        # the parent, unhardened) to an off-edge POST while GET was correctly
        # refused — the same oracle reached by a different verb.
        app.router.routes.insert(
            0, _PlainRoute(WEB_MOUNT_PATH, bare_mount_guard(app))
        )
    _install_openapi_client_limits(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> Response:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # The raw pydantic report can echo the submitted text back; the stable
        # envelope deliberately does not.
        return JSONResponse(
            status_code=400,
            content=error_body(
                ERROR_INVALID_REQUEST,
                "request payload is not valid",
                _request_id(request),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # Unknown paths and unsupported methods are routing failures raised by
        # the framework, not by this application. Without this handler they
        # would answer with the framework's default body and break the one
        # documented non-2xx shape.
        code = _ROUTING_ERROR_CODES.get(exc.status_code, ERROR_INTERNAL)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                code, MESSAGE_BY_CODE.get(code, "internal error"), _request_id(request)
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        LOGGER.exception(
            "unhandled error route=%s request_id=%s",
            _route_label(request),
            _request_id(request),
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ERROR_INTERNAL, "internal error", _request_id(request)),
        )


def _register_routes(app: FastAPI) -> None:
    prefix = f"/{API_VERSION}"

    @app.get("/healthz", response_model=HealthResponseBody, tags=["health"])
    async def healthz() -> HealthResponseBody:
        """Liveness. Answers as long as the process is serving; no auth."""
        return HealthResponseBody(status="ok")

    @app.get(
        "/readyz",
        response_model=HealthResponseBody,
        tags=["health"],
        responses={
            503: {
                "model": ErrorResponseBody,
                "description": (
                    "The terminology database is not readable. Classified as"
                    " `internal`; the 503 status is what distinguishes it."
                ),
            }
        },
    )
    async def readyz(request: Request) -> HealthResponseBody:
        """Readiness: the terminology database is readable right now. No auth."""
        ok = await asyncio.to_thread(probe_database, request.app.state.term_service)
        if not ok:
            raise ApiError(ERROR_INTERNAL, "dictionary is not readable", status_code=503)
        return HealthResponseBody(status="ready")

    @app.post(
        f"{prefix}/translations",
        response_model=TranslationResponseBody,
        tags=["translation"],
        responses=ERROR_RESPONSES,
    )
    async def create_translation(
        request: Request,
        body: TranslationRequestBody,
        device: Annotated[Device, Depends(require_scope(SCOPE_TRANSLATE))],
    ) -> TranslationResponseBody:
        """Translate plain text through the shared dictionary-first pipeline."""
        state = request.app.state
        forced_to_chinese = None if body.to is None else body.to == "zh"
        # Before the expensive model call: a device revoked since admission
        # must not spend an LLM budget slot or a model round trip.
        await _require_active_device(request, device)

        outcome = await translate_request_async(
            state.term_service,
            state.translator,
            TranslationJob(text=body.text, forced_to_chinese=forced_to_chinese),
            before_llm_call=_device_llm_guard(request, device),
            # The dictionary stage opens SQLite and can score every term row.
            # This process serves many requests on one loop, so that work runs
            # on a worker thread instead of blocking every other request (and
            # the request time budget) while it runs.
            offload=asyncio.to_thread,
        )
        LOGGER.info(
            "translation device=%s kind=%s direction=%s request_id=%s",
            redact_id(device.device_id),
            outcome.kind,
            outcome.direction,
            _request_id(request),
        )
        if outcome.kind == KIND_ERROR:
            _log_llm_failure_diagnostic(request, outcome)
            raise ApiError(outcome.error_code or ERROR_INTERNAL)
        if outcome.kind == KIND_LLM and not llm_configured():
            # With no model configured the pipeline returns the source text
            # with official terms substituted. That is a reasonable chat
            # fallback, but over HTTP it would look like a successful
            # translation, so this surface refuses instead of pretending.
            LOGGER.info(
                "translation refused: no model configured request_id=%s",
                _request_id(request),
            )
            raise ApiError(
                ERROR_LLM_UNAVAILABLE, "no translation model is configured"
            )
        # Before returning: close the window between the model call and the
        # response, so a revocation that commits mid-flight is not served. The
        # work is already paid for here, so a transient store read error serves
        # the completed translation (a definitive revocation still 401s).
        await _require_active_device(request, device, serve_on_store_error=True)
        return TranslationResponseBody(
            kind=outcome.kind,
            text=outcome.text,
            direction=outcome.direction,
            dictionary_miss=outcome.dictionary_miss,
            request_id=_request_id(request),
        )

    @app.get(
        f"{prefix}/terms",
        response_model=TermsResponseBody,
        tags=["dictionary"],
        responses=ERROR_RESPONSES,
    )
    async def read_terms(
        request: Request,
        device: Annotated[Device, Depends(require_scope(SCOPE_META))],
        q: str = Query(
            description=(
                "Term to look up. Clients should send at most 200 characters. "
                "The server strips surrounding whitespace before its "
                "authoritative limit check; callers must not depend on the "
                "permissive normalized superset."
            ),
        ),
    ) -> TermsResponseBody:
        """Backend-ranked exact or fuzzy dictionary candidates for a query."""
        query = q.strip()
        if not query:
            raise ApiError(ERROR_INVALID_REQUEST, "query parameter q is required")
        if len(query) > TERM_QUERY_MAX_LENGTH:
            raise ApiError(ERROR_INVALID_REQUEST, "query parameter q is too long")
        matches = await asyncio.to_thread(
            lookup_terms, request.app.state.term_service, query
        )
        return TermsResponseBody(
            query=query,
            matches=[
                TermMatchBody(
                    zh=match.zh,
                    en=match.en,
                    category=match.category,
                    score=match.score,
                    reason=match.reason,
                )
                for match in matches
            ],
            request_id=_request_id(request),
        )

    @app.get(
        f"{prefix}/meta",
        response_model=MetaResponseBody,
        tags=["dictionary"],
        responses=ERROR_RESPONSES,
    )
    async def read_meta(
        request: Request,
        device: Annotated[Device, Depends(require_scope(SCOPE_META))],
    ) -> MetaResponseBody:
        """Service and data provenance. No paths, no secrets, no chat ids."""
        meta = await asyncio.to_thread(
            service_metadata, request.app.state.term_service
        )
        return MetaResponseBody(
            service_version=service_version(),
            api_version=API_VERSION,
            schema_version=meta.schema_version,
            source_profile=meta.source_profile,
            source_commit=meta.source_commit,
            term_count=meta.term_count,
            llm_configured=llm_configured(),
            request_id=_request_id(request),
        )

    @app.post(
        f"{prefix}/reviews",
        response_model=ReviewResponseBody,
        tags=["review"],
        responses=ERROR_RESPONSES,
    )
    async def create_review(
        request: Request,
        body: ReviewRequestBody,
        device: Annotated[Device, Depends(require_scope(SCOPE_TRANSLATE))],
    ) -> JSONResponse:
        """Review a submitted source/target pair against dictionary constraints."""
        await _require_active_device(request, device)
        resolutions = []
        for item in body.resolutions or ():
            dumped = item.model_dump()
            if dumped.get("choice") == "not_a_term":
                dumped = {
                    "mention_id": dumped["mention_id"],
                    "choice": dumped["choice"],
                }
            resolutions.append(dumped)
        alignments = (
            [item.model_dump() for item in body.alignments]
            if body.alignments is not None
            else None
        )
        resolution_context = (
            body.resolution_context.model_dump()
            if body.resolution_context is not None
            else None
        )
        try:
            report = await asyncio.to_thread(
                review_pair,
                request.app.state.term_service,
                body.source,
                body.target,
                body.direction,
                tuple(resolutions),
                review_version=body.review_version,
                alignments=alignments,
                resolution_context=resolution_context,
            )
        except ReviewRequestError as exc:
            raise ApiError(exc.code, exc.message)
        projected = project_review_report(report, _request_id(request))
        LOGGER.info(
            "review device=%s findings=%s truncated=%s request_id=%s",
            redact_id(device.device_id),
            len(projected["findings"]),
            projected["truncated"],
            _request_id(request),
        )
        await _require_active_device(request, device, serve_on_store_error=True)
        return JSONResponse(content=projected)
