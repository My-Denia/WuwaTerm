"""A request that ends at its deadline must also STOP.

A 504 is a promise that this process is no longer working on that request. Until
this module existed, the promise was false: the deadline layer ended the
exchange while Starlette's ``BaseHTTPMiddleware`` child task kept running the
handler. A translation refused at the 90 s budget went on waiting for a model
concurrency slot, then spent a paid model call whose answer nobody would ever
receive — measured here, a request whose 504 was returned immediately still ran
its whole handler afterwards.

These tests are about the lifecycle rather than the record, so the transport is
a real ``httpx.MockTransport`` that counts every POST and can be told to block.
Nothing here reaches a provider, and nothing here asserts on a substituted
pipeline: the point of the bug is which layer actually stops, which a fake at
the wrong level would hide.

The four questions, in the order the layers own them:

* the concurrency slot is released (a leaked permit wedges every later request);
* nothing is STARTED after the deadline, retry or fallback paths included;
* an in-flight POST is cancelled rather than left to finish and be billed;
* the record still says where the time went, because that is what made the 504
  actionable in the first place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import anyio
import httpx
import pytest

from wuwaterm import llm_transport
from wuwaterm import request_trace
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import TOKEN_SCHEME, DeviceStore
from wuwaterm_api.settings import ApiSettings

API_KEY = "sk-timeout-cancellation-key"
TEST_SECRET = "unguessable-material-for-tests-0123456789abcdef"

# Long enough that "the handler is still running" is unmistakable, short enough
# that the whole file costs well under a second of real waiting.
HANG_SECONDS = 30.0
# How long a test waits after the 504 before deciding nothing else happened.
SETTLE_SECONDS = 0.3
# The request budget for tests that need the request to REACH a stage (slot
# queue, model POST) before the budget ends. The budget starts at the
# middleware, so authentication, body read and pipeline set-up all spend it
# first; under CPU contention that set-up alone has outlasted 100-200 ms and the
# request timed out at ``init`` before reaching the stage under test. One second
# keeps a wide margin over that set-up while the test still only waits for the
# one deadline it is about.
REQUEST_BUDGET_SECONDS = 1.0


def build_app(tmp_path, db_path, **overrides):
    defaults = dict(
        db_path=db_path,
        device_db_path=tmp_path / "api-state" / "devices.db",
        rate_limit_per_minute=100,
        llm_calls_per_minute=100,
        llm_max_concurrency=1,
        max_body_bytes=2048,
        request_timeout_seconds=30.0,
    )
    defaults.update(overrides)
    settings = ApiSettings(**defaults)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    return create_app(settings, device_store=store), store


def issue_token(store) -> str:
    device = store.issue("owner desktop", None, secret=TEST_SECRET)
    return f"{TOKEN_SCHEME}.{device.device_id}.{TEST_SECRET}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def llm_env(monkeypatch) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "timeout-cancellation-model")


async def call(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test"
    ) as client:
        return await client.request(method, url, **kwargs)


class Counter:
    """Every POST attempt that reached the transport, and how it ended.

    ``starts`` is appended BEFORE the handler waits, so a POST started after
    the test's assertion point cannot hide behind the wait.
    """

    def __init__(self, delay: float = HANG_SECONDS, content: str = "Translated."):
        self.delay = delay
        self.content = content
        self.starts: list[float] = []
        self.cancelled = 0
        self.finished = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.starts.append(asyncio.get_running_loop().time())
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": self.content}}]}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


@contextlib.contextmanager
def captured_records():
    recorder = Recorder()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(recorder)
    root.setLevel(logging.INFO)
    try:
        yield recorder
    finally:
        root.removeHandler(recorder)
        root.setLevel(previous_level)


def timeout_records(recorder: Recorder) -> list[dict[str, str]]:
    prefix = "request timed out "
    parsed: list[dict[str, str]] = []
    for message in recorder.messages:
        if not message.startswith(prefix):
            continue
        fields: dict[str, str] = {}
        for token in message[len(prefix) :].split(" "):
            key, _separator, value = token.partition("=")
            fields[key] = value
        parsed.append(fields)
    return parsed


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 1. A request that never got a slot must not spend one afterwards
# --------------------------------------------------------------------------


def test_a_request_timed_out_waiting_for_a_slot_never_starts_a_call(
    tmp_path, sample_db, monkeypatch
):
    """The budget expires while QUEUED, and the queue is where it must end.

    The slot is released a moment after the deadline, which is the exact window
    the old implementation exploited: the 504 was already on its way to the
    client, and the handler underneath went on to take the slot and translate.
    So the assertion is not only "zero POSTs at the 504" — it is "zero POSTs at
    all", checked after the slot has been free long enough for a leaked waiter
    to have used it.
    """
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path,
        sample_db,
        request_timeout_seconds=REQUEST_BUDGET_SECONDS,
        llm_max_concurrency=1,
    )
    counting = Counter()
    app.state.translator._llm_transport = counting.transport()
    token = issue_token(store)

    async def scenario():
        # Somebody else holds the only concurrency slot for the whole budget.
        slots = app.state.translator._current_llm_slots()
        await slots.acquire()
        try:
            response = await call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "这是一句会被超时打断的完整句子。"},
                headers=bearer(token),
            )
        finally:
            slots.release()
        # The waiter was cancelled, so nothing takes the slot it just freed.
        held = slots.locked()
        await asyncio.sleep(SETTLE_SECONDS)
        return response, held, slots.locked()

    with captured_records() as recorder:
        response, held_after_timeout, held_later = run(scenario())

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "internal"
    assert response.headers["X-Request-Id"] == response.json()["request_id"]
    # The slot was free the moment the 504 came back, and stayed free: no
    # cancelled waiter came back for it.
    assert held_after_timeout is False
    assert held_later is False
    assert counting.starts == [], counting.starts

    [record] = timeout_records(recorder)
    assert record["stage"] == "llm_slot_wait", record
    assert record["llm_calls"] == "0", record
    assert record["llm_post_ms"] == "0.0", record
    assert float(record["slot_wait_ms"]) > 0.0, record
    assert float(record["elapsed_ms"]) >= REQUEST_BUDGET_SECONDS * 1000 * 0.95, record


# --------------------------------------------------------------------------
# 2. An in-flight POST is cancelled, and its permit comes back
# --------------------------------------------------------------------------


def test_a_request_timed_out_during_the_post_cancels_it_and_frees_the_slot(
    tmp_path, sample_db, monkeypatch
):
    """The POST is in flight when the budget expires.

    Two failures mattered here and only one of them was visible from outside.
    The visible one: the provider keeps generating (and billing) an answer the
    client will never see. The other one: the concurrency slot stays held by
    the abandoned handler, so every later request queues behind work nobody is
    waiting for. Both are asserted, and the second one is asserted the honest
    way — by making a SECOND request and requiring it to be served.
    """
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path,
        sample_db,
        request_timeout_seconds=REQUEST_BUDGET_SECONDS,
        llm_max_concurrency=1,
    )
    counting = Counter()
    app.state.translator._llm_transport = counting.transport()
    token = issue_token(store)

    with captured_records() as recorder:
        timed_out = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "这是另一句绝不能出现在日志里的句子。"},
                headers=bearer(token),
            )
        )

    assert timed_out.status_code == 504, timed_out.text
    assert len(counting.starts) == 1, counting.starts
    assert counting.cancelled == 1, (counting.cancelled, counting.finished)

    records = timeout_records(recorder)
    assert len(records) == 1, recorder.messages
    record = records[0]
    assert record["stage"] == "llm_post", record
    assert record["llm_calls"] == "1", record
    assert float(record["llm_post_ms"]) > 0.0, record
    assert float(record["llm_post_ms"]) <= float(record["elapsed_ms"]), record
    assert float(record["elapsed_ms"]) >= REQUEST_BUDGET_SECONDS * 1000 * 0.95, record

    # The permit is back without anything else having to time out for it: the
    # next request acquires the slot and answers.
    counting.delay = 0.0
    recovered = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "这是恢复之后的一句完整句子。"},
            headers=bearer(token),
        )
    )
    assert recovered.status_code == 200, recovered.text


def test_a_post_still_running_when_the_deadline_arrives_is_not_left_to_finish(
    tmp_path, sample_db, monkeypatch
):
    """The deadline ends the work; it does not merely stop waiting for it.

    Primitive and physical: the transport's handler is still asleep when the
    deadline arrives, and it must observe a cancellation rather than wake up on
    its own. Checked by recording whether it was cancelled or ran to
    completion, because "the 504 already came back" says nothing about the task
    that produced it.
    """
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path,
        sample_db,
        request_timeout_seconds=REQUEST_BUDGET_SECONDS,
        llm_max_concurrency=1,
    )
    # Would wake up on its own well after the deadline.
    counting = Counter(delay=REQUEST_BUDGET_SECONDS + 0.4)
    app.state.translator._llm_transport = counting.transport()
    token = issue_token(store)

    async def scenario():
        response = await call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "超时之后不得继续等待模型的一段话。"},
            headers=bearer(token),
        )
        # Well past the point the transport would have woken up by itself.
        await asyncio.sleep(0.8)
        return response

    response = run(scenario())

    assert response.status_code == 504, response.text
    assert counting.cancelled == 1, (counting.cancelled, counting.finished)
    assert counting.finished == 0, "the cancelled POST ran to completion"


# --------------------------------------------------------------------------
# 3. Nothing new is started after the deadline, fallbacks included
# --------------------------------------------------------------------------


def test_a_failing_post_across_the_deadline_starts_no_retry_or_fallback(
    tmp_path, sample_db, monkeypatch
):
    """The failure the old code turned into a second, paid attempt.

    A transient transport failure is exactly what every retry and fallback path
    in this service is built to absorb, and one arriving at the deadline is the
    one case where absorbing it is wrong: the caller already has its answer. So
    the transport fails with a connection error AFTER the deadline has passed,
    and the only correct outcome is one start and no second one.
    """
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path,
        sample_db,
        request_timeout_seconds=REQUEST_BUDGET_SECONDS,
        llm_max_concurrency=1,
    )
    attempts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(asyncio.get_running_loop().time())
        # Outlives the deadline, then fails as a transient error would.
        await asyncio.sleep(REQUEST_BUDGET_SECONDS + 0.25)
        raise httpx.ConnectError("provider went away", request=request)

    app.state.translator._llm_transport = httpx.MockTransport(handler)
    token = issue_token(store)

    async def scenario():
        response = await call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "失败的调用也不得在超时后再来一次。"},
            headers=bearer(token),
        )
        await asyncio.sleep(SETTLE_SECONDS)
        return response

    response = run(scenario())

    assert response.status_code == 504, response.text
    assert len(attempts) == 1, attempts


def test_a_long_text_timed_out_mid_way_does_not_translate_its_next_segment(
    tmp_path, sample_db, monkeypatch
):
    """Long text is a LOOP of paid calls, and that loop is the fallback that hurts.

    Text over the per-call limit is cut into segments and each is translated in
    turn, so a request that dies after segment one is one cancellation away
    from spending segment two's call. This cannot be reached over HTTP — the
    route refuses input past the per-call limit, which is why the loop serves
    trusted in-process callers — so it is driven at the layer that has it, under
    the same deadline and the same trace.
    """
    llm_env(monkeypatch)
    from wuwaterm.application import TranslationJob, translate_request_async
    from wuwaterm.lookup import TermService
    from wuwaterm.sentence import SentenceTranslator

    counting = Counter()
    translator = SentenceTranslator(
        sample_db, llm_max_concurrency=1, llm_transport=counting.transport()
    )
    service = TermService(sample_db)
    source = "。".join(["这是一个需要分段翻译的很长的中文句子"] * 120) + "。"

    async def scenario():
        request_trace.start()
        try:
            request_trace.set_deadline(REQUEST_BUDGET_SECONDS)
            with pytest.raises(TimeoutError):
                with anyio.fail_after(REQUEST_BUDGET_SECONDS):
                    await translate_request_async(
                        service,
                        translator,
                        TranslationJob(text=source),
                        # Only a trusted in-process caller may exceed the
                        # published per-call limit; that is what reaches the
                        # segment loop.
                        input_limit=100_000,
                        offload=asyncio.to_thread,
                    )
            await asyncio.sleep(SETTLE_SECONDS)
        finally:
            request_trace.reset()

    run(scenario())

    assert len(counting.starts) == 1, counting.starts
    assert counting.finished == 0, "the cancelled POST ran to completion"


def test_a_cancelled_request_starts_no_call_even_if_the_cancel_is_swallowed(
    tmp_path, sample_db, monkeypatch
):
    """The guard lives at the choke point, not only in the cancel chain.

    Cancellation is a ``BaseException``, so ordinary ``except Exception`` does
    not catch it — but nothing about a library in between guarantees that, and
    this one is the boundary where the money is spent. The transport therefore
    consults the deadline itself. Simulated at the level that matters: the
    handler's task is cancelled, and the pipeline then tries to POST again well
    after the budget, which must be refused without a single request on the
    wire.
    """
    llm_env(monkeypatch)
    starts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append("post")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Translated."}}]}
        )

    run(_expired_then_attempt(httpx.MockTransport(handler), starts))

    assert starts == []


async def _expired_then_attempt(transport, starts):
    """Arm a budget, let it expire, then try to POST from a fresh task."""
    trace = request_trace.start()
    try:
        request_trace.set_deadline(0.05)
        await asyncio.sleep(0.08)
        assert request_trace.deadline_expired() is True
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(llm_transport.LLMTranslationError) as refused:
                await llm_transport.post_chat_completion(
                    client, [{"role": "user", "content": "x"}], purpose="translation"
                )
        assert refused.value.reason == "timeout"
        assert trace.llm_calls == 0, "a refused attempt must not be recorded"
        assert starts == []
    finally:
        request_trace.reset()


# --------------------------------------------------------------------------
# The record still says where the time went
# --------------------------------------------------------------------------


def test_a_timeout_before_the_response_starts_answers_exactly_once(
    tmp_path, sample_db, monkeypatch
):
    """The 504 is one status line and one complete body.

    A layer that synthesises a response has to know whether the exchange below
    it already started. Sending a second ``http.response.start`` corrupts the
    protocol for an ASGI server, and the previous stack made that state
    unreachable rather than impossible — so this asserts on the messages
    themselves, in order, rather than on the status code they happen to add up
    to.
    """
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=0.1, llm_max_concurrency=1
    )
    app.state.translator._llm_transport = Counter().transport()
    token = issue_token(store)

    messages: list[dict] = []
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/translations",
        "raw_path": b"/v1/translations",
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "server": ("api.test", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"api.test"),
            (b"content-type", b"application/json"),
            *[
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in bearer(token).items()
            ],
        ],
        "app": app,
    }
    body = b'{"text": "\xe8\xbf\x99\xe6\x98\xaf\xe4\xb8\x80\xe5\x8f\xa5\xe5\xae\x8c\xe6\x95\xb4\xe5\x8f\xa5\xe5\xad\x90\xe3\x80\x82"}'
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    async def scenario():
        slots = app.state.translator._current_llm_slots()
        await slots.acquire()
        try:
            await app(scope, receive, send)
        finally:
            slots.release()

    run(scenario())

    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert len(starts) == 1, messages
    assert starts[0]["status"] == 504
    headers = {name.lower(): value for name, value in starts[0]["headers"]}
    assert b"x-request-id" in headers
    bodies = [m for m in messages if m["type"] == "http.response.body"]
    assert bodies, messages
    assert bodies[-1].get("more_body", False) is False, bodies
    assert b'"code":"internal"' in bodies[-1]["body"].replace(b" ", b""), bodies


def test_a_status_line_already_in_flight_is_never_preceded_by_another():
    """The exchange that already started must keep the ONLY status line it has.

    An ASGI ``send`` commits the status line to the socket before it returns
    from the await: a real server drains, writes a TLS record, or waits for
    backpressure there, so the call can still be suspended when the deadline
    fires. Recording "a response has started" only once that call RETURNED
    therefore concluded that nothing had been sent, and the deadline layer wrote
    a second ``http.response.start`` — the server was handed ``[200, 504]`` for
    one request. Measured against a suspended ``send`` before the ordering was
    fixed, which is what this test pins.

    Asserted on the messages rather than the status code, because the defect is
    invisible in a status code: a client reads whichever status line reached it
    first and never learns the second was written.
    """

    async def handler(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # Outlives the budget, so the deadline arrives while the exchange above
        # is in flight.
        await asyncio.sleep(30)

    committed: list[str] = []
    released = asyncio.Event()

    async def suspended_send(message):
        if message["type"] == "http.response.start":
            # The await that made the old ordering wrong: the status line is on
            # the wire, and this call suspends anyway.
            committed.append(f"{message['type']}:{message['status']}")
            released.set()
            await asyncio.sleep(1.0)
            return
        committed.append(message["type"])

    from wuwaterm_api.app import TimeoutMiddleware

    app = TimeoutMiddleware(handler, timeout_seconds=0.1)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "server": ("api.test", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"api.test")],
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def scenario():
        await app(scope, receive, suspended_send)
        # Past the point the suspended send would have returned, so a status
        # line written after it cannot hide behind the wait.
        await asyncio.sleep(0.2)

    with captured_records() as recorder:
        run(scenario())

    starts = [entry for entry in committed if entry.startswith("http.response.start")]
    assert starts == ["http.response.start:200"], committed
    # One record, and it says what the caller actually got rather than claiming
    # a 504 it could not deliver.
    [record] = timeout_records(recorder)
    assert record["sent_status"] == "200", record
    # The body was completed rather than left dangling: a client reading the
    # response must not be left waiting for something that will never come.
    assert committed[-1] == "http.response.body", committed


def test_deadline_expired_during_prepare_terms_returns_504_not_503(
    tmp_path, sample_db, monkeypatch
):
    """Expired server time budget caught at pre-call guard still returns 504."""
    from wuwaterm import application as app_mod

    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=30.0, llm_max_concurrency=1
    )
    counting = Counter()
    app.state.translator._llm_transport = counting.transport()
    token = issue_token(store)

    orig_dictionary_stage = app_mod._dictionary_stage

    def expire_during_dictionary_stage(*args, **kwargs):
        request_trace.set_deadline(0.0)
        return orig_dictionary_stage(*args, **kwargs)

    monkeypatch.setattr(app_mod, "_dictionary_stage", expire_during_dictionary_stage)

    with captured_records() as recorder:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "需要翻译的一个句子"},
                headers=bearer(token),
            )
        )

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "internal"
    assert counting.starts == [], counting.starts
    [record] = timeout_records(recorder)
    assert record["stage"] == "prepare_terms", record
    assert record["llm_calls"] == "0", record
    assert app.state.metrics.snapshot()["requests"]["timed_out"] == 1


def test_semantic_review_deadline_expired_at_preflight_returns_504_not_503(
    tmp_path, sample_db, monkeypatch
):
    """Expired server time budget on /v1/semantic-reviews preflight returns 504."""
    from wuwaterm_api import app as api_app_mod

    llm_env(monkeypatch)
    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", "true")
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=30.0, llm_max_concurrency=1
    )
    counting = Counter()
    app.state.translator._llm_transport = counting.transport()
    token = issue_token(store)

    orig_set_stage = api_app_mod.set_stage

    def expire_on_prepare_terms(name: str) -> None:
        orig_set_stage(name)
        if name == api_app_mod.STAGE_PREPARE_TERMS:
            request_trace.set_deadline(0.0)

    monkeypatch.setattr(api_app_mod, "set_stage", expire_on_prepare_terms)

    with captured_records() as recorder:
        response = run(
            call(
                app,
                "POST",
                "/v1/semantic-reviews",
                json={
                    "source": "这是一句测试。",
                    "target": "This is a test.",
                    "direction": "en",
                    "review_version": "semantic-review-v1",
                    "explain_language": "zh",
                },
                headers=bearer(token),
            )
        )

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "internal"
    assert counting.starts == [], counting.starts
    [record] = timeout_records(recorder)
    assert record["stage"] == "prepare_terms", record
    assert record["llm_calls"] == "0", record
    assert app.state.metrics.snapshot()["requests"]["timed_out"] == 1


def test_expired_deadline_guard_preserves_503_for_unexpired_timeout_and_hostile_diagnostic(
    tmp_path, sample_db, monkeypatch
):
    """Unexpired upstream timeout and hostile diagnostic getter both keep 503."""
    from wuwaterm.application import ERROR_LLM_UNAVAILABLE, KIND_ERROR
    from wuwaterm_api import app as api_app_mod

    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=30.0, llm_max_concurrency=1
    )
    token = issue_token(store)

    # Sub-case A: upstream ReadTimeout while request deadline is still valid -> 503.
    async def upstream_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream slow", request=request)

    app.state.translator._llm_transport = httpx.MockTransport(upstream_timeout)
    with captured_records() as recorder_a:
        res_a = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "上游超时但服务端未超时。"},
                headers=bearer(token),
            )
        )

    assert res_a.status_code == 503, res_a.text
    assert res_a.json()["error"]["code"] == "llm_unavailable"
    assert timeout_records(recorder_a) == []
    assert app.state.metrics.snapshot()["requests"]["timed_out"] == 0

    # Sub-case B: expired deadline + hostile llm_failure property -> safe 503, no crash/leak.
    class HostileOutcome:
        kind = KIND_ERROR
        direction = "en"
        error_code = ERROR_LLM_UNAVAILABLE

        @property
        def llm_failure(self):
            raise RuntimeError(TEST_SECRET)

    async def hostile_outcome(*_args, **_kwargs):
        request_trace.set_deadline(0.0)
        return HostileOutcome()

    monkeypatch.setattr(api_app_mod, "translate_request_async", hostile_outcome)
    with captured_records() as recorder_b:
        res_b = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "超时且诊断属性抛异常。"},
                headers=bearer(token),
            )
        )

    assert res_b.status_code == 503, res_b.text
    assert res_b.json()["error"]["code"] == "llm_unavailable"
    assert TEST_SECRET not in res_b.text
    assert TEST_SECRET not in "\n".join(recorder_b.messages)
