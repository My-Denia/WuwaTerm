"""Server-side request records for the HTTP adapter.

Two separate properties are under test here and they fail in different ways:

* the service EMITS a record at all. Until this module existed the adapter
  called ``LOGGER.info`` in a dozen places and configured no handler anywhere,
  so a deployed process produced no per-request evidence — a request id handed
  to a client could not be matched to anything on the server.
* what the record CONTAINS. A log line is an output channel like any other, so
  the same rules that govern the response body govern it. The rule is about what
  the SERVICE knows: no credential, no device id behind an authenticated
  principal, no request text. A caller's own bytes are a different question —
  they may appear, bounded and escaped, because a record of them tells a reader
  only what the writer of the request already had; what they may never do is
  reach an operator's terminal unescaped, forge a field, or be a credential.

The privacy half is asserted against captured records rather than by reading
the source, because the interesting failures (a field added later, a framework
that starts logging on its own) are invisible to inspection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import httpx
import pytest
from starlette.requests import ClientDisconnect

from wuwaterm_api.app import create_app
from wuwaterm_api.auth import TOKEN_SCHEME, DeviceStore
from wuwaterm_api.settings import ApiConfigError, ApiSettings, validate_log_level

ROOT = Path(__file__).resolve().parents[1]

LOGGER_NAME = "wuwaterm_api"
COMPLETION_PREFIX = "request complete "

# A secret the operator supplies; the service never mints one.
TEST_SECRET = "unguessable-material-for-tests-0123456789abcdef"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def build_app(tmp_path: Path, db_path: Path, **overrides):
    defaults = dict(
        db_path=db_path,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        max_body_bytes=2048,
        request_timeout_seconds=30.0,
    )
    defaults.update(overrides)
    settings = ApiSettings(**defaults)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    return create_app(settings, device_store=store), store


def issue_device(store, name: str = "owner desktop", scopes=None):
    device = store.issue(name, scopes, secret=TEST_SECRET)
    return device, f"{TOKEN_SCHEME}.{device.device_id}.{TEST_SECRET}"


def run(coro):
    return asyncio.run(coro)


async def call(app, method: str, url: str, *, raise_app_exceptions=True, **kwargs):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test"
    ) as client:
        return await client.request(method, url, **kwargs)


class RecordingHandler(logging.Handler):
    """Captures what a handler would actually emit, formatting included."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        """What a real handler would WRITE, not just the interpolated message.

        ``Handler.format`` appends the exception text when a record carries
        ``exc_info``. Scanning ``getMessage()`` alone would therefore let a
        traceback reach the deployed stream carrying submitted text or
        credential material while the privacy assertions below stayed green.
        """
        return [self.format(record) for record in self.records]

    @property
    def completions(self) -> list[str]:
        return [
            message
            for message in self.messages
            if message.startswith(COMPLETION_PREFIX)
        ]


@contextlib.contextmanager
def captured_records():
    """Attach a handler to the ROOT logger for the duration of a block.

    Root, not ``wuwaterm_api``: what the serve path installs is a root handler,
    so the deployed output carries records from the shared application layer and
    from every library in the process too. A privacy claim about "the log" is a
    claim about that whole surface — pinning only this package's namespace would
    let a request-dependent record added anywhere else carry a credential past a
    green test.

    The serve path's quieting of the HTTP client library is applied here for the
    same reason, from the same list: it is part of the deployed arrangement, and
    without it this harness would be capturing a surface the deployed service
    does not have. That library logs the full URL of every request it MAKES at
    INFO, which in a test process includes the test client's own calls — a
    leak this service could not produce and which would make the assertions
    below about the wrong program.

    Deliberately not ``caplog``: the point is what an installed handler sees,
    and pytest's own capture level is global state other tests can move.
    """
    from wuwaterm_api.cli import NOISY_LOGGERS

    logger = logging.getLogger()
    handler = RecordingHandler()
    previous_level = logger.level
    previous_noisy = {
        name: logging.getLogger(name).level for name in NOISY_LOGGERS
    }
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        for name, level in previous_noisy.items():
            logging.getLogger(name).setLevel(level)


EXPECTED_FIELDS = {
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
    "device",
}

DIAGNOSTIC_PREFIX = "llm translation diagnostic "
DIAGNOSTIC_FIELDS = {
    "request_id", "reason", "detail", "expected_count", "actual_count"
}


def diagnostic_fields(captured):
    result = []
    for message in captured.messages:
        if not message.startswith(DIAGNOSTIC_PREFIX):
            continue
        pairs = [token.split("=", 1) for token in message[len(DIAGNOSTIC_PREFIX):].split()]
        assert all(len(pair) == 2 for pair in pairs)
        assert len(pairs) == len(DIAGNOSTIC_FIELDS)
        record = dict(pairs)
        assert set(record) == DIAGNOSTIC_FIELDS
        result.append(record)
    return result


def enable_diagnostic_llm(monkeypatch, answer):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://diagnostic-provider.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", TEST_SECRET)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "diagnostic-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", answer)


@pytest.mark.parametrize(
    "mode,detail,expected,actual",
    [
        ("empty", "empty_output", "-", "-"),
        ("missing", "missing_placeholder", "1", "0"),
        ("duplicate", "duplicate_placeholder", "1", "2"),
        ("nontext", "non_text_output", "-", "-"),
    ],
)
def test_llm_diagnostic_correlates_without_exposing_content(
    tmp_path, sample_db, monkeypatch, mode, detail, expected, actual
):
    app, store = build_app(tmp_path, sample_db)
    device, token = issue_device(store)
    source = "Echo replies in this diagnostic example."
    output_marker = "synthetic-output-must-not-be-logged"
    calls = []

    async def answer(locked_text, locks, **kwargs):
        calls.append(1)
        assert locks
        if mode == "empty":
            return " \n "
        if mode == "nontext":
            return None
        if mode == "missing":
            return locked_text.replace(locks[0][0], "") + output_marker
        return locked_text + locks[0][0] + output_marker

    enable_diagnostic_llm(monkeypatch, answer)
    with captured_records() as captured:
        response = run(call(app, "POST", "/v1/translations", json={"text": source},
                            headers={"Authorization": f"Bearer {token}",
                                     "X-Request-Id": "caller-forged-diagnostic-id"}))
    assert response.status_code == 503
    assert set(response.json()) == {"error", "request_id"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert response.json()["error"]["code"] == "llm_unavailable"
    request_id = response.json()["request_id"]
    assert response.headers["X-Request-Id"] == request_id
    assert diagnostic_fields(captured) == [{
        "request_id": request_id, "reason": "invalid_response", "detail": detail,
        "expected_count": expected, "actual_count": actual,
    }]
    assert fields(captured.completions[0])["request_id"] == request_id
    assert calls == [1]
    stream = "\n".join(captured.messages)
    for forbidden in (source, output_marker, TEST_SECRET, token, device.device_id,
                      "diagnostic-provider.invalid", "caller-forged-diagnostic-id",
                      "__WUWA_TERM_"):
        assert forbidden not in stream
        assert forbidden not in response.text


def test_concurrent_llm_diagnostics_stay_with_their_request(
    tmp_path, sample_db, monkeypatch
):
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)
    arrived = 0
    ready = None

    async def answer(locked_text, locks, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return " " if "first" in locked_text else locked_text + locks[0][0]

    enable_diagnostic_llm(monkeypatch, answer)

    async def exercise():
        nonlocal ready
        ready = asyncio.Event()
        return await asyncio.gather(*[
            call(app, "POST", "/v1/translations", json={"text": text},
                 headers={"Authorization": f"Bearer {token}"})
            for text in ("Echo says the first diagnostic sentence.",
                         "Echo says the second diagnostic sentence.")
        ])

    with captured_records() as captured:
        empty, duplicate = run(exercise())
    assert empty.status_code == duplicate.status_code == 503
    assert arrived == 2
    by_id = {record["request_id"]: record for record in diagnostic_fields(captured)}
    assert len(by_id) == 2
    assert by_id[empty.json()["request_id"]]["detail"] == "empty_output"
    assert by_id[duplicate.json()["request_id"]]["detail"] == "duplicate_placeholder"
    assert set(by_id) == {fields(line)["request_id"] for line in captured.completions}


def test_malformed_llm_diagnostic_is_not_a_log_or_response_channel(
    tmp_path, sample_db, monkeypatch
):
    from types import SimpleNamespace
    from wuwaterm.application import KIND_ERROR, ERROR_LLM_UNAVAILABLE

    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)
    marker = "sensitive-marker\nrequest_id=forged"

    async def outcome(*args, **kwargs):
        return SimpleNamespace(kind=KIND_ERROR, direction="en", text=marker,
                               error_code=ERROR_LLM_UNAVAILABLE, llm_failure=marker)

    monkeypatch.setattr("wuwaterm_api.app.translate_request_async", outcome)
    with captured_records() as captured:
        response = run(call(app, "POST", "/v1/translations", json={"text": "diagnostic"},
                            headers={"Authorization": f"Bearer {token}"}))
    assert response.status_code == 503
    assert diagnostic_fields(captured) == [{
        "request_id": response.json()["request_id"], "reason": "unknown",
        "detail": "unspecified", "expected_count": "-", "actual_count": "-",
    }]
    assert marker not in "\n".join(captured.messages) + response.text


def test_failed_llm_diagnostic_write_cannot_change_the_api_error(
    tmp_path, sample_db, monkeypatch
):
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)

    async def answer(*args, **kwargs):
        return " "

    enable_diagnostic_llm(monkeypatch, answer)

    class HostileDiagnostic(logging.Handler):
        attempts = 0

        def emit(self, record):
            if record.getMessage().startswith(DIAGNOSTIC_PREFIX):
                self.attempts += 1
                raise RuntimeError(TEST_SECRET)

    handler = HostileDiagnostic()
    logging.getLogger().addHandler(handler)
    try:
        with captured_records() as captured:
            response = run(call(app, "POST", "/v1/translations", json={"text": "空输出诊断句子。"},
                                headers={"Authorization": f"Bearer {token}"}))
    finally:
        logging.getLogger().removeHandler(handler)
    assert handler.attempts == 1
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert TEST_SECRET not in "\n".join(captured.messages) + response.text


def test_raising_llm_diagnostic_getter_cannot_replace_the_original_503(
    tmp_path, sample_db, monkeypatch
):
    from wuwaterm.application import KIND_ERROR, ERROR_LLM_UNAVAILABLE

    class HostileOutcome:
        kind = KIND_ERROR
        direction = "en"
        error_code = ERROR_LLM_UNAVAILABLE

        @property
        def llm_failure(self):
            raise RuntimeError(TEST_SECRET)

    async def outcome(*args, **kwargs):
        return HostileOutcome()

    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)
    monkeypatch.setattr("wuwaterm_api.app.translate_request_async", outcome)
    with captured_records() as captured:
        response = run(call(
            app, "POST", "/v1/translations", json={"text": "diagnostic getter"},
            headers={"Authorization": f"Bearer {token}"}, raise_app_exceptions=False,
        ))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
    assert set(response.json()) == {"error", "request_id"}
    assert diagnostic_fields(captured) == []
    assert fields(captured.completions[0])["request_id"] == response.json()["request_id"]
    assert TEST_SECRET not in "\n".join(captured.messages) + response.text


@pytest.mark.parametrize(
    "case,expected_code",
    [
        ("dict", "llm_unavailable"),
        ("list", "llm_unavailable"),
        ("object", "llm_unavailable"),
        ("hostile_budget", "llm_budget_exhausted"),
        ("hostile_upstream", "llm_unavailable"),
        ("raising_reason_getter", "llm_unavailable"),
    ],
)
def test_hostile_business_reason_is_classified_without_calling_magic_methods(
    tmp_path, sample_db, monkeypatch, case, expected_code
):
    from wuwaterm.sentence import LLMTranslationError, TRANSLATION_UNAVAILABLE_NOTICE

    calls = []

    class HostileReason(str):
        def __hash__(self):
            calls.append("hash")
            raise RuntimeError(TEST_SECRET)

        def __eq__(self, other):
            calls.append("eq")
            raise RuntimeError(TEST_SECRET)

        def __str__(self):
            calls.append("str")
            raise RuntimeError(TEST_SECRET)

    class HostileObject:
        __hash__ = HostileReason.__hash__
        __eq__ = HostileReason.__eq__
        __str__ = HostileReason.__str__

    class RaisingReasonError(LLMTranslationError):
        def __init__(self):
            RuntimeError.__init__(self, TRANSLATION_UNAVAILABLE_NOTICE)
            self.user_message = TRANSLATION_UNAVAILABLE_NOTICE

        @property
        def reason(self):
            raise RuntimeError(TEST_SECRET)

    async def answer(*args, **kwargs):
        if case == "raising_reason_getter":
            raise RaisingReasonError()
        if case == "dict":
            reason = {"marker": TEST_SECRET}
        elif case == "list":
            reason = [TEST_SECRET]
        elif case == "object":
            reason = HostileObject()
        else:
            reason = HostileReason("budget" if case == "hostile_budget" else "upstream")
        raise LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason=reason)

    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)
    enable_diagnostic_llm(monkeypatch, answer)
    with captured_records() as captured:
        response = run(call(
            app, "POST", "/v1/translations", json={"text": "一个业务分类诊断句子。"},
            headers={"Authorization": f"Bearer {token}"}, raise_app_exceptions=False,
        ))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == expected_code
    assert set(response.json()) == {"error", "request_id"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert calls == []
    assert TEST_SECRET not in "\n".join(captured.messages) + response.text


def fields(message: str) -> dict[str, str]:
    """Parse a record the way a log collector would: split, then partition.

    Deliberately NOT a per-key regex. A regex that looks for `status=` anywhere
    in the line finds a caller's copy of it if one is allowed to survive into a
    value, and the parser used to check the record must not be more forgiving
    than the one an operator's tooling will be. Duplicate keys are refused here
    for the same reason: that is what a forged field looks like.
    """
    assert message.startswith(COMPLETION_PREFIX), message
    parsed: dict[str, str] = {}
    for token in message[len(COMPLETION_PREFIX):].split(" "):
        key, separator, value = token.partition("=")
        assert separator, f"{token!r} is not a field in {message!r}"
        assert key not in parsed, f"duplicate field {key!r} in {message!r}"
        parsed[key] = value
    assert set(parsed) == EXPECTED_FIELDS, parsed
    return parsed


# --------------------------------------------------------------------------
# One record per request, on every outcome
# --------------------------------------------------------------------------


def test_an_authenticated_success_produces_one_completion_record(tmp_path, sample_db):
    app, store = build_app(tmp_path, sample_db)
    device, token = issue_device(store)

    with captured_records() as captured:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )

    assert response.status_code == 200, response.text
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["request_id"] == response.headers["X-Request-Id"]
    assert record["request_id"] == response.json()["request_id"]
    assert record["method"] == "POST"
    assert record["route"] == "/v1/translations"
    assert record["status"] == "200"
    assert float(record["duration_ms"]) >= 0.0
    # The principal is present and is the redaction helper's output, never the
    # device id it was derived from.
    assert record["device"].startswith("id:")
    assert device.device_id not in captured.completions[0]


def test_an_unauthenticated_request_produces_one_completion_record(tmp_path, sample_db):
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        response = run(call(app, "POST", "/v1/translations", json={"text": "声骸"}))

    assert response.status_code == 401
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["request_id"] == response.headers["X-Request-Id"]
    assert record["request_id"] == response.json()["request_id"]
    assert record["status"] == "401"
    assert record["route"] == "/v1/translations"
    # No device was authenticated, so there is no principal to name.
    assert record["device"] == "-"


def test_an_error_path_produces_one_completion_record(tmp_path, sample_db, monkeypatch):
    """An unhandled failure still leaves exactly one record behind.

    This is the case a naive implementation loses: the application's own
    ``Exception`` handler runs OUTSIDE the request-id middleware, so the
    exception passes straight through it. Without a ``finally`` the request
    that most needs a server-side record is the one that produces none.
    """
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)

    def boom(_service):
        raise RuntimeError("metadata read failed")

    monkeypatch.setattr("wuwaterm_api.app.service_metadata", boom)

    with captured_records() as captured:
        response = run(
            call(
                app,
                "GET",
                "/v1/meta",
                headers={"Authorization": f"Bearer {token}"},
                raise_app_exceptions=False,
            )
        )

    assert response.status_code == 500
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["status"] == "500"
    assert record["route"] == "/v1/meta"
    # The envelope is the only place the id is published on this path, and it
    # is the same id the server recorded.
    assert record["request_id"] == response.json()["request_id"]
    assert record["device"].startswith("id:")


def test_an_automatic_redirect_is_recorded_and_correlates_by_header_only(
    tmp_path, sample_db
):
    """A trailing slash is answered by the framework with an empty body.

    Pinned because the runbook tells an operator where to find the correlation
    id per response kind, and this is the one that has no body to put it in.
    """
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        response = run(call(app, "GET", "/v1/meta/"))

    assert response.status_code == 307
    assert response.text == ""
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["status"] == "307"
    assert record["request_id"] == response.headers["X-Request-Id"]
    # The redirect is produced without matching a route, so the target takes
    # the escaped-value branch rather than being named by a template. Pinned
    # because the runbook orders those cases and puts this one in the third.
    assert record["route"] == "/v1/meta/"


@pytest.mark.parametrize("gone", [ClientDisconnect, asyncio.CancelledError])
def test_a_caller_that_goes_away_is_not_recorded_as_a_server_fault(
    tmp_path, sample_db, gone
):
    """A hang-up produces no response, so the seeded status would be a lie.

    Recording it as `500` sends an operator looking for a server fault that did
    not happen. Both shapes are covered because the one that actually occurs is
    not the one to guess at: a socket hang-up during the body read surfaces as
    ``ClientDisconnect`` (measured against a real server), while cancellation is
    what a shutdown mid-request raises. Both must still propagate afterwards —
    this changes the record, not the request.
    """
    from starlette.requests import Request

    from wuwaterm_api.app import CLIENT_GONE_STATUS, RequestIdMiddleware

    app, _ = build_app(tmp_path, sample_db)
    middleware = RequestIdMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/translations",
        "headers": [],
        "query_string": b"",
        "app": app,
    }

    async def hung_up(_request):
        raise gone()

    async def scenario():
        with captured_records() as captured:
            with pytest.raises(gone):
                await middleware.dispatch(Request(scope), hung_up)
        return captured

    captured = run(scenario())

    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["status"] == str(CLIENT_GONE_STATUS)
    assert record["route"] == "/v1/translations"


def test_a_request_refused_before_routing_still_produces_a_record(tmp_path, sample_db):
    """The record comes from the outermost layer, not from the route handler.

    A body over the cap is rejected by middleware that runs before anything is
    matched, so a record attached to the handler would silently miss it — and
    an oversized-body flood is exactly the traffic an operator needs to see.
    """
    app, _ = build_app(tmp_path, sample_db, max_body_bytes=64)

    with captured_records() as captured:
        response = run(
            call(app, "POST", "/v1/translations", json={"text": "x" * 4096})
        )

    assert response.status_code == 413
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["status"] == "413"
    assert record["request_id"] == response.headers["X-Request-Id"]


def test_a_failure_while_recording_cannot_replace_the_requests_own_outcome(
    tmp_path, sample_db, monkeypatch
):
    """The record is additive: it may be missing, never the thing that failed.

    An exception raised from a ``finally`` replaces the one propagating through
    it. Since the record is written from exactly there, a fault while
    describing a request would otherwise become the fault reported for it —
    swapping the traceback the operator needs for one about writing it down.
    """
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)

    def unwritable(*_args, **_kwargs):
        raise RuntimeError("the record itself failed")

    monkeypatch.setattr("wuwaterm_api.app._log_request_completed", unwritable)

    with captured_records() as captured:
        served = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )

    assert served.status_code == 200, served.text
    assert captured.completions == [], "the record was supposed to fail"
    # The loss is reported, with enough to act on: which request, and why.
    lost = [
        record
        for record in captured.records
        if record.getMessage().startswith("request record could not be written")
    ]
    assert len(lost) == 1, captured.messages
    assert served.headers["X-Request-Id"] in lost[0].getMessage()
    assert "the record itself failed" in str(lost[0].exc_info[1])

    def boom(_service):
        raise RuntimeError("metadata read failed")

    monkeypatch.setattr("wuwaterm_api.app.service_metadata", boom)
    with captured_records() as captured:
        failed = run(
            call(
                app,
                "GET",
                "/v1/meta",
                headers={"Authorization": f"Bearer {token}"},
                raise_app_exceptions=False,
            )
        )

    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal"
    # The status and the envelope are NOT the discriminating assertion: the
    # substituted exception would be caught by the same handler and rendered
    # identically. What distinguishes the two worlds is which exception
    # actually arrived there.
    unhandled = [
        record
        for record in captured.records
        if record.getMessage().startswith("unhandled error")
    ]
    assert len(unhandled) == 1, captured.messages
    raised = str(unhandled[0].exc_info[1])
    assert "metadata read failed" in raised
    assert "the record itself failed" not in raised


def test_a_handler_that_always_raises_cannot_take_the_request_with_it(
    tmp_path, sample_db
):
    """The failure mode the guard is FOR, driven through a real handler.

    ``logging.Handler.handle`` wraps neither ``filter`` nor ``emit`` — it is the
    standard library's own handler implementations that catch their errors. So
    an embedder's handler can raise, and it raises again for the fallback that
    reports the loss, from inside the same ``finally``. Both have to be
    contained or the record becomes the request's outcome.

    The handler is scoped to those two calls on purpose. Every other logging
    call in this service is unguarded, as it was before this change, and a
    process whose handler raises is broken in those places too — that is a
    pre-existing property and not what this guard claims. What it claims is
    narrow and is what is asserted here: the completion record cannot be the
    thing that goes wrong.
    """
    guarded = ("request complete", "request record could not be written")

    class Hostile(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.attempts = 0

        def emit(self, record: logging.LogRecord) -> None:
            if not record.getMessage().startswith(guarded):
                return
            self.attempts += 1
            raise RuntimeError("this handler always fails")

    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)

    hostile = Hostile()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(hostile)
    root.setLevel(logging.DEBUG)
    try:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "声骸"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    finally:
        root.removeHandler(hostile)
        root.setLevel(previous_level)

    assert response.status_code == 200, response.text
    # Both the record and the report of its loss were attempted and contained.
    assert hostile.attempts >= 2


# --------------------------------------------------------------------------
# What a record may never contain
# --------------------------------------------------------------------------


def test_no_credential_device_id_or_request_text_reaches_any_record(
    tmp_path, sample_db
):
    """The privacy contract, asserted against emitted records.

    The caller here does everything it can to get its own material written
    down: it presents a real credential AND sends a token-shaped
    ``X-Request-Id``, which is the exact shape that used to be echoed into the
    auth-reject line. Nothing it controls may appear; the server-minted id and
    the redacted principal must.
    """
    app, store = build_app(tmp_path, sample_db)
    device, token = issue_device(store)
    body_marker = "MARKER-4f3c1d-request-body-text"

    with captured_records() as captured:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": body_marker},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Request-Id": token,
                },
            )
        )

    assert response.status_code in (200, 503), response.text
    assert len(captured.completions) == 1, captured.messages
    emitted = "\n".join(captured.messages)

    assert token not in emitted
    assert TEST_SECRET not in emitted
    assert device.device_id not in emitted
    assert body_marker not in emitted

    minted = response.headers["X-Request-Id"]
    assert minted != token
    assert minted in emitted
    assert "device=id:" in emitted


def test_the_privacy_scan_covers_the_exception_text_a_handler_would_write(
    tmp_path, sample_db, monkeypatch
):
    """A traceback is part of what gets written, so it is part of what is checked.

    ``Handler.format`` appends the exception text; the interpolated message does
    not contain it. A scan of messages alone would let a failing request write a
    traceback to the deployed stream while the privacy assertions stayed green,
    so the harness scans what a handler would emit.
    """
    app, store = build_app(tmp_path, sample_db)
    device, token = issue_device(store)
    body_marker = "MARKER-9a2b7e-request-body-text"

    def boom(*_args, **_kwargs):
        raise RuntimeError("dictionary stage failed")

    monkeypatch.setattr("wuwaterm_api.app.translate_request_async", boom)

    with captured_records() as captured:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": body_marker},
                headers={"Authorization": f"Bearer {token}"},
                raise_app_exceptions=False,
            )
        )

    assert response.status_code == 500
    emitted = "\n".join(captured.messages)
    # The scan really does reach the traceback...
    assert "Traceback (most recent call last)" in emitted
    assert "dictionary stage failed" in emitted
    # ...and the traceback carries nothing the caller supplied.
    assert body_marker not in emitted
    assert token not in emitted
    assert TEST_SECRET not in emitted
    assert device.device_id not in emitted


def test_an_unmatched_path_reaches_the_record_only_as_inert_text(
    tmp_path, sample_db
):
    """An unauthenticated caller controls the request target.

    When nothing matched there is no route template to name, so the raw path is
    the only thing left to record — and it can carry terminal escape sequences.
    It is rendered escaped, so what an operator's terminal receives is inert
    text, and it is truncated so a long target cannot push the rest of the
    record off a line.

    The property is about the FORM, which is why the name is: a target that
    needs no escaping is recorded as it stands, and that is not a failure of
    anything — recording bytes a caller chose for its own request discloses
    nothing to the reader of the line.
    """
    app, _ = build_app(tmp_path, sample_db)
    # Percent-encoded on the wire, exactly as a caller would have to send it;
    # the server decodes it back into control characters before anything sees
    # the path, which is what makes the decoded form unsafe to record.
    hostile = "/%1B%5B31mred%1B%5D0;title%07" + "A" * 300

    with captured_records() as captured:
        response = run(call(app, "GET", hostile))

    assert response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    message = captured.completions[0]
    assert "\x1b" not in message
    assert "\x07" not in message
    assert "\\x1b" in message
    assert len(fields(message)["route"]) <= 100


def test_a_padded_target_cannot_spend_the_event_loop_on_being_examined(
    tmp_path, sample_db, monkeypatch
):
    """The credential check must not become the thing worth attacking.

    Decoding nested escapes to a fixed point removes one layer per pass and
    rescans the rest each time, so reading the WHOLE target is quadratic — an
    unauthenticated caller could then buy tens of milliseconds of event loop per
    request with a padded path. The check reads only the prefix that could reach
    the record, which is also the only part a reader could recover anything
    from.

    The assertion counts decode passes rather than milliseconds: the work is
    what is bounded, and a wall-clock threshold on a shared runner measures the
    runner.
    """
    from wuwaterm_api import app as app_module

    passes: list[int] = []
    real_unquote = app_module.unquote

    def counting_unquote(value):
        passes.append(len(value))
        return real_unquote(value)

    monkeypatch.setattr(app_module, "unquote", counting_unquote)
    app, _ = build_app(tmp_path, sample_db)
    # As long as a request line is realistically allowed to be, and nested so
    # that every layer costs a full pass over what is left.
    padded = "/%" + "25" * 8000 + "77td1.x.y"

    with captured_records() as captured:
        response = run(call(app, "GET", padded))

    assert response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    # Both the number of passes and the size of each are bounded by the prefix
    # that could be written, not by what the caller sent.
    assert len(passes) <= app_module.RAW_TARGET_LOG_LIMIT, len(passes)
    assert max(passes) <= app_module.RAW_TARGET_LOG_LIMIT, max(passes)
    # And the other half of the same bound, which is what makes the first half
    # safe rather than merely cheap: this target ends in a credential marker
    # beyond the prefix, the check does not see it — and neither does the
    # record, because the same cut produced both.
    record = fields(captured.completions[0])
    assert record["route"] != app_module.CREDENTIAL_SHAPED_TARGET
    assert "td1" not in record["route"]


def test_a_rendered_target_is_bounded_by_what_is_written_not_what_arrived(
    tmp_path, sample_db
):
    """Both bounds are needed, and only the second one bounds the LINE.

    Clipping the source alone lets one character render as ten, so a target
    inside the source limit can still produce hundreds of columns and push the
    status, the duration and the principal off the end of a terminal — which is
    exactly what the limit was introduced to prevent.
    """
    app, _ = build_app(tmp_path, sample_db)
    # Each of these renders as a ten-character escape.
    expensive = "/" + "%F3%A0%80%81" * 79

    with captured_records() as captured:
        response = run(call(app, "GET", expensive))

    assert response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["route"].endswith("~")
    assert len(record["route"]) <= 161
    assert record["status"] == "404"


def test_a_target_shortened_by_either_bound_says_so(tmp_path, sample_db):
    """The mark is about being shortened, not about being escaped.

    A long PLAIN target renders well inside the written bound, so only the
    source bound applies to it — and without a mark there it reads as the whole
    of what arrived, which is the thing the bound exists to prevent. Escaping is
    not the signal either way: a value can be escaped for holding one control
    character and still be complete.
    """
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        shortened_response = run(call(app, "GET", "/" + "a" * 120))
    assert shortened_response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    shortened = fields(captured.completions[0])["route"]

    with captured_records() as captured:
        whole_response = run(call(app, "GET", "/" + "a" * 40))
    assert whole_response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    whole = fields(captured.completions[0])["route"]

    assert shortened.endswith("~"), shortened
    assert not whole.endswith("~"), whole
    assert "a" * 40 in whole


def test_an_unsupported_method_on_a_known_route_is_named_by_its_template(
    tmp_path, sample_db
):
    """A 405 is a MATCH, not a miss.

    The router records a partial match before refusing the method, so the
    template is available and the escaped fallback does not apply. Pinned
    because the runbook tells an operator which of the two a record contains.
    """
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        response = run(call(app, "DELETE", "/healthz"))

    assert response.status_code == 405
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["route"] == "/healthz"
    assert record["method"] == "DELETE"


def test_a_method_outside_the_known_set_is_recorded_by_membership_only(
    tmp_path, sample_db
):
    """The method field records which verb arrived, not what a caller wrote.

    A method is a caller-chosen token, so a free-text field fed from it is a
    place for anything to end up — a device id, for instance, which is sixteen
    hexadecimal characters and would pass every escaping rule untouched. This
    service publishes GET and POST and refuses everything else, so the exact
    spelling of a refused verb carries nothing an operator can act on, and the
    field records membership instead of content.
    """
    app, _ = build_app(tmp_path, sample_db)
    device_shaped = "0123456789abcdef"

    with captured_records() as captured:
        response = run(call(app, device_shaped, "/healthz"))

    assert response.status_code == 405
    assert len(captured.completions) == 1, captured.messages
    record = fields(captured.completions[0])
    assert record["method"] == "other"
    assert device_shaped not in "\n".join(captured.messages)


def test_an_unmatched_target_cannot_forge_a_field_in_the_record(
    tmp_path, sample_db
):
    """The record is whitespace-delimited, and the target may contain spaces.

    A caller who sends `/x status=200 device=id:spoofed` would otherwise put its
    own status and principal into the line AHEAD of the real ones, and anything
    reading the stream by splitting on whitespace would believe the first pair
    it meets. Quoting does not help: nothing that splits on whitespace respects
    quotes. The rendered value has to be one token.
    """
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        response = run(
            call(app, "GET", "/missing%20status=200%20device=id:spoofed")
        )

    assert response.status_code == 404
    assert len(captured.completions) == 1, captured.messages
    message = captured.completions[0]
    # Neither a whitespace tokenizer nor a scan-anywhere reader can find a
    # second copy of either key.
    assert message.count("status=") == 1, message
    assert message.count("device=") == 1, message
    record = fields(message)
    assert record["status"] == "404"
    assert record["device"] == "-"
    assert " " not in record["route"]
    assert "\\x20" in record["route"]
    assert "\\x3d" in record["route"]


def test_a_credential_in_the_request_target_is_never_written_down(
    tmp_path, sample_db
):
    """The target is caller-supplied, so it can carry the caller's own token.

    A client or a proxy that puts the credential in the URL instead of the
    Authorization header produces a path that looks entirely ordinary and is
    well under the truncation limit, and escaping is reversible — so recording
    it escaped would still write the secret into the log. Such a target is not
    recorded at all.

    Not even a digest of it: the redaction helper falls back to an unkeyed
    hash when no secret is configured, and the API container blanks that
    variable deliberately, so a digest would be a cheap offline check against
    guessed secrets. The actionable fact is that a credential reached a URL,
    not which one.

    The second spelling is the one a literal search misses. The server
    percent-decodes once, so a doubly-encoded scheme arrives still encoded and
    one further decode of the recorded line would recover a working token.
    """
    app, store = build_app(tmp_path, sample_db)
    device, token = issue_device(store)
    # Plain, then the scheme's first character written as an escape under one
    # more layer of encoding each time. The server decodes the target exactly
    # once, so layer k needs k further decodes before the marker appears —
    # past any fixed round budget.
    # Encoding "w" once is %77; encoding THAT is %2577, then %252577, and so
    # on — each layer wraps the whole of the previous one.
    spellings = ["/" + token] + [
        "/%" + "25" * layers + "77" + token[1:] for layers in range(7)
    ]

    for target in spellings:
        with captured_records() as captured:
            response = run(call(app, "GET", target))

        assert response.status_code == 404, target
        emitted = "\n".join(captured.messages)
        assert token not in emitted, target
        assert TEST_SECRET not in emitted, target
        assert device.device_id not in emitted, target
        assert len(captured.completions) == 1, captured.messages
        assert fields(captured.completions[0])["route"] == "credential-shaped"


def test_a_hostile_path_that_matched_a_route_is_recorded_as_the_template(
    tmp_path, sample_db
):
    """A matched request is named by the template, not by what arrived.

    The template is repository text, so it cannot carry anything; recording it
    also means one record shape per endpoint instead of one per caller-chosen
    spelling.
    """
    app, _ = build_app(tmp_path, sample_db)

    with captured_records() as captured:
        response = run(call(app, "GET", "/v1/meta?q=%1b%5b31m"))

    assert response.status_code == 401
    assert len(captured.completions) == 1, captured.messages
    assert fields(captured.completions[0])["route"] == "/v1/meta"


# --------------------------------------------------------------------------
# Where the handler comes from
# --------------------------------------------------------------------------


def test_serving_installs_the_process_log_handler_at_the_configured_level(
    tmp_path, sample_db, monkeypatch
):
    from wuwaterm_api import cli

    configured: list[dict] = []
    served: list[dict] = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: configured.append(kwargs)
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))
    monkeypatch.setenv("WUWATERM_API_LOG_LEVEL", "debug")

    assert cli.main(["serve"]) == 0

    assert len(configured) == 1, configured
    assert configured[0]["level"] == logging.DEBUG
    assert "%(levelname)s" in configured[0]["format"]
    assert "%(message)s" in configured[0]["format"]
    assert served, "the server was never started"


def test_the_log_level_defaults_to_info(tmp_path, sample_db, monkeypatch):
    from wuwaterm_api import cli

    configured: list[dict] = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: configured.append(kwargs)
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))
    monkeypatch.delenv("WUWATERM_API_LOG_LEVEL", raising=False)

    assert cli.main(["serve"]) == 0

    assert configured[0]["level"] == logging.INFO


def test_quieting_the_http_client_never_relaxes_a_stricter_level(
    tmp_path, sample_db, monkeypatch
):
    """The carve-out is a floor, not a pin.

    Setting a level on a logger makes that level effective for it, and
    propagation does not re-check the ancestors it passes through. Pinning these
    at WARNING under a configured ERROR would therefore emit warnings the
    operator explicitly asked not to see — quieting turned into amplifying.
    """
    from wuwaterm_api import cli

    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))

    previous = {name: logging.getLogger(name).level for name in cli.NOISY_LOGGERS}
    try:
        monkeypatch.setenv("WUWATERM_API_LOG_LEVEL", "ERROR")
        assert cli.main(["serve"]) == 0
        for name in cli.NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.ERROR, name

        monkeypatch.setenv("WUWATERM_API_LOG_LEVEL", "DEBUG")
        assert cli.main(["serve"]) == 0
        for name in cli.NOISY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING, name
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


def test_operator_subcommands_never_touch_the_process_log_handler(
    tmp_path, sample_db, monkeypatch
):
    """`device list` is run inside an operator's shell session.

    Installing a root handler there would put this service's records into the
    middle of the operator's own command output, and the subcommands print
    what they have to say themselves.
    """
    from wuwaterm_api import cli

    configured: list[dict] = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: configured.append(kwargs)
    )
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))
    monkeypatch.setenv("WUWATERM_API_LOG_LEVEL", "DEBUG")

    assert cli.main(["device", "list"]) == 0

    assert configured == []


def test_importing_or_building_the_application_installs_no_handler(
    tmp_path, sample_db, monkeypatch
):
    """Library and test users get no global logging side effect."""
    configured: list[dict] = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: configured.append(kwargs)
    )

    build_app(tmp_path, sample_db)

    assert configured == []


def test_an_unusable_log_level_stops_serving_but_not_credential_commands(
    tmp_path, sample_db, monkeypatch
):
    """Same rule the bind already follows.

    A serve-time setting is validated on the serve path only: a typo in it must
    never be able to block `device revoke`, which is the one operation that has
    to work on a machine whose environment is wrong.
    """
    from wuwaterm_api import cli

    served: list[dict] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setenv("WUWATERM_DB_PATH", str(sample_db))
    monkeypatch.setenv("WUWATERM_API_STATE_DIR", str(tmp_path / "state-api"))
    monkeypatch.setenv("WUWATERM_API_LOG_LEVEL", "chatty")

    assert cli.main(["serve"]) == 2
    assert served == []
    assert cli.main(["device", "list"]) == 0


def test_the_log_level_guard_accepts_only_the_standard_names():
    for accepted in ("critical", "ERROR", " warning ", "info", "Debug"):
        assert validate_log_level(accepted) == accepted.strip().upper()
    for refused in ("", "trace", "verbose", "10", "INFO;DEBUG"):
        with pytest.raises(ApiConfigError):
            validate_log_level(refused)


def test_the_refusal_never_echoes_the_configured_value():
    """Settings never reflect a raw environment value back."""
    with pytest.raises(ApiConfigError) as raised:
        validate_log_level("s3cr3t-looking-value")

    assert "s3cr3t-looking-value" not in str(raised.value)


def test_the_api_example_environment_documents_the_log_level():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "WUWATERM_API_LOG_LEVEL=INFO" in text


WEBSOCKET_LIBRARIES = {"websockets", "wsproto"}


def test_api_dependencies_do_not_directly_enable_websockets():
    """Keep the API extra HTTP-only; the image check covers installed packages."""
    import tomllib

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    api = pyproject["project"]["optional-dependencies"]["api"]
    declared = [Requirement(item) for item in api]

    # Named directly, because this is the one an author would reach for.
    assert any(canonicalize_name(item.name) == "uvicorn" for item in declared), api
    assert not any(
        canonicalize_name(item.name) in WEBSOCKET_LIBRARIES for item in declared
    ), api
    assert not any(
        canonicalize_name(extra) in WEBSOCKET_LIBRARIES
        or canonicalize_name(extra) == "standard"
        for item in declared
        for extra in item.extras
    ), api

def test_review_completion_log_does_not_contain_source_or_target(tmp_path, sample_db):
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)
    source = "今汐拿到了声骸-UNIQUE-SOURCE-MARKER。"
    target = "Jinhsi got an Echo UNIQUE-TARGET-MARKER."

    with captured_records() as captured:
        response = run(
            call(
                app,
                "POST",
                "/v1/reviews",
                json={"source": source, "target": target, "direction": "en"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )

    assert response.status_code == 200, response.text
    stream = "\n".join(captured.messages)
    assert source not in stream
    assert target not in stream
    assert "UNIQUE-SOURCE-MARKER" not in stream
    assert "UNIQUE-TARGET-MARKER" not in stream
    assert captured.completions
    assert "/v1/reviews" in captured.completions[0]
