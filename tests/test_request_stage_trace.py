"""Where a translation request spent its time, asserted on the records.

Two questions an operator could not answer from the existing records, and this
module pins the answers:

* a request refused at the time budget said only that it *was* refused. The
  trace records the stage it died in, so a 504 distinguishes "waiting for this
  process' model slot" from "waiting for the provider" from "our own work".
* a completed request said only how long it took. For one that reached the
  model, the completion record now carries the same split.

The numbers are asserted twice over: once against the code path that produces
them (a real semaphore, a real POST through ``llm_transport``), and once through
the whole ASGI stack, where the trace crosses Starlette's middleware task
boundary and ``asyncio.to_thread`` — the two places a `contextvars` design can
silently stop working.

The privacy half is asserted on captured records rather than by reading the
source: every field added here is a number or a token from a fixed vocabulary,
so the failure worth catching is a field that starts carrying something else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import httpx
import pytest

import wuwaterm.request_trace as request_trace
from wuwaterm import llm_transport
from wuwaterm.sentence import SentenceTranslator
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import TOKEN_SCHEME, DeviceStore
from wuwaterm_api.settings import ApiSettings

LOGGER_NAME = "wuwaterm_api"
COMPLETION_PREFIX = "request complete "
TIMEOUT_PREFIX = "request timed out "

# A secret the operator supplies; the service never mints one.
TEST_SECRET = "unguessable-material-for-tests-0123456789abcdef"
API_KEY = "sk-stage-trace-test-key"

BASE_FIELDS = {"request_id", "method", "route", "status", "duration_ms", "device"}
STAGE_FIELDS = {"slot_wait_ms", "llm_post_ms", "llm_calls", "direction"}
TIMEOUT_FIELDS = (
    {"route", "request_id", "elapsed_ms", "stage", "sent_status"}
    | {"slot_wait_ms", "llm_post_ms", "llm_calls", "direction"}
)

# Long enough to be unmistakable in a millisecond field, short enough that the
# whole file stays well under a second of real waiting.
HOLD_SECONDS = 0.15
POST_SECONDS = 0.03


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def build_app(tmp_path: Path, db_path: Path, **overrides):
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


def issue_device(store, name: str = "owner desktop", scopes=None):
    device = store.issue(name, scopes, secret=TEST_SECRET)
    return device, f"{TOKEN_SCHEME}.{device.device_id}.{TEST_SECRET}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def run(coro):
    return asyncio.run(coro)


async def call(app, method: str, url: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test"
    ) as client:
        return await client.request(method, url, **kwargs)


def llm_env(monkeypatch) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "stage-trace-model")


def answering_transport(delay: float = 0.0, content: str = "Translated sentence."):
    """A real transport round trip: the trace instruments the real POST path."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


def hanging_transport(delay: float = 30.0):
    """A POST the provider does not answer inside the request's budget."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Translated sentence."}}]}
        )

    return httpx.MockTransport(handler)


class Recorder(logging.Handler):
    """Captures what a harm would actually write, ``Handler.format`` included."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [self.format(record) for record in self.records]


@contextlib.contextmanager
def captured_records():
    """Attach a handler to the root logger, which is where ``serve`` writes."""
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


def parse_record(message: str, prefix: str) -> dict[str, str]:
    """Split a record the way a collector does: whitespace, then the first `=`."""
    assert message.startswith(prefix), message
    parsed: dict[str, str] = {}
    for token in message[len(prefix) :].split(" "):
        key, separator, value = token.partition("=")
        assert separator == "=", token
        assert key not in parsed, token
        parsed[key] = value
    return parsed


def completions(recorder: Recorder) -> list[dict[str, str]]:
    return [
        parse_record(message, COMPLETION_PREFIX)
        for message in recorder.messages
        if message.startswith(COMPLETION_PREFIX)
    ]


def timeouts(recorder: Recorder) -> list[dict[str, str]]:
    return [
        parse_record(message, TIMEOUT_PREFIX)
        for message in recorder.messages
        if message.startswith(TIMEOUT_PREFIX)
    ]


# --------------------------------------------------------------------------
# The split itself
# --------------------------------------------------------------------------


def test_slot_wait_and_the_post_are_attributed_to_different_fields(
    sample_db, monkeypatch
):
    """Contention is slot wait; the POST is post time; neither is the other.

    Driven through the real ``_call_llm_async_limited`` and the real
    ``llm_transport.post_chat_completion`` path, because that is where the two
    spans are opened: a test that replaced either would prove nothing about the
    wiring.
    """
    llm_env(monkeypatch)
    translator = SentenceTranslator(
        sample_db,
        llm_max_concurrency=1,
        llm_transport=answering_transport(POST_SECONDS),
    )
    traces: dict[str, request_trace.RequestTrace] = {}

    async def one(name: str) -> None:
        traces[name] = request_trace.start()
        try:
            await translator._call_llm_async_limited(
                "Padded source.", (), to_chinese=True
            )
        finally:
            request_trace.reset()

    async def scenario() -> None:
        # Uncontended: the slot is free, so only the POST can account for time.
        await one("solo")
        # Contended: one request holds the only slot while another queues.
        slots = translator._current_llm_slots()
        await slots.acquire()
        queued = asyncio.create_task(one("queued"))
        try:
            await asyncio.sleep(HOLD_SECONDS)
        finally:
            slots.release()
        await queued

    run(scenario())

    solo, queued_trace = traces["solo"], traces["queued"]
    assert solo.llm_calls == queued_trace.llm_calls == 1

    # An uncontended request waited for nothing, and its post time is the POST.
    assert solo.slot_wait_ms < HOLD_SECONDS * 1000 * 0.5, solo.slot_wait_ms
    assert solo.llm_post_ms >= POST_SECONDS * 1000 * 0.8, solo.llm_post_ms

    # The queued one waited about as long as the slot was held, and that time is
    # in slot_wait_ms only: its own POST is the same short POST as anyone's.
    assert queued_trace.slot_wait_ms >= HOLD_SECONDS * 1000 * 0.8, (
        queued_trace.slot_wait_ms
    )
    assert queued_trace.slot_wait_ms > queued_trace.llm_post_ms
    assert queued_trace.llm_post_ms < HOLD_SECONDS * 1000, queued_trace.llm_post_ms

    # The direction is recorded by the layer that decided it.
    assert solo.direction == request_trace.DIRECTION_EN_TO_ZH


def test_outside_a_request_every_mark_is_a_no_op(sample_db, monkeypatch):
    """The bot, the channel adapter and the CLI run with no trace at all."""
    llm_env(monkeypatch)
    assert request_trace.current() is None

    # Marks must be safe to call from those paths, and must record nothing.
    request_trace.set_stage(request_trace.STAGE_LLM_POST)
    request_trace.set_direction(request_trace.DIRECTION_ZH_TO_EN)
    request_trace.begin_llm_post()
    request_trace.end_llm_post()
    request_trace.reset()
    assert request_trace.current() is None
    assert request_trace.completion_fields(None) == ""

    translator = SentenceTranslator(
        sample_db, llm_max_concurrency=1, llm_transport=answering_transport()
    )

    async def scenario() -> str:
        # ``llm_slot`` must be exactly ``async with semaphore`` with no trace:
        # acquired while inside, released on the way out, and nothing recorded.
        independent = asyncio.Semaphore(1)
        async with request_trace.llm_slot(independent):
            assert independent.locked()
        assert not independent.locked()
        assert not independent._waiters
        return await translator._call_llm_async_limited(
            "Padded source.", (), to_chinese=False
        )

    assert run(scenario()) == "Translated sentence."
    assert request_trace.current() is None

    # And the transport alone, which is what a Telegram translation does.
    async def once() -> str:
        async with httpx.AsyncClient(
            transport=answering_transport()
        ) as client:
            return await llm_transport.post_chat_completion(
                client, [{"role": "user", "content": "x"}], purpose="translation"
            )

    assert run(once()) == "Translated sentence."


# --------------------------------------------------------------------------
# The 504 record
# --------------------------------------------------------------------------


def test_a_timed_out_request_reports_the_stage_it_was_in(
    tmp_path, sample_db, monkeypatch
):
    """Lowest budget, marker text: the record names the stage, not the text."""
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=0.2, llm_max_concurrency=1
    )
    app.state.translator._llm_transport = answering_transport()
    _, token = issue_device(store)
    source = "这是一句标记为绝不能出现在日志里的句子。"

    async def scenario():
        # The only slot is held by somebody else, so the request under test can
        # only be QUEUED. The slot is released just after the budget expires:
        # ASGITransport does not return until the whole application call has
        # unwound, and a handler queued for a slot that is never released would
        # keep this test waiting for it.
        slots = app.state.translator._current_llm_slots()
        await slots.acquire()
        task = asyncio.create_task(
            call(
                app, "POST", "/v1/translations", json={"text": source},
                headers=bearer(token),
            )
        )
        try:
            await asyncio.sleep(0.35)
        finally:
            slots.release()
        try:
            return await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    with captured_records() as recorder:
        response = run(scenario())

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "internal"
    records = timeouts(recorder)
    assert len(records) == 1, recorder.messages
    record = records[0]
    assert set(record) == TIMEOUT_FIELDS, record
    assert record["request_id"] == response.json()["request_id"]
    assert record["route"] == "/v1/translations"
    assert record["stage"] == "llm_slot_wait"
    assert record["llm_calls"] == "0"
    assert record["llm_post_ms"] == "0.0"
    assert record["direction"] == "zh->en"
    assert float(record["elapsed_ms"]) >= 190
    # A wait with no acquisition still records the time waited, and it is inside
    # the elapsed time rather than beside it.
    waited = float(record["slot_wait_ms"])
    assert waited > 0.0, record
    assert waited <= float(record["elapsed_ms"]), record

    stream = "\n".join(recorder.messages)
    for forbidden in (source, TEST_SECRET, token, API_KEY, "stage-trace-model"):
        assert forbidden not in stream
        assert forbidden not in response.text


def test_a_request_waiting_for_the_provider_reports_the_post_stage(
    tmp_path, sample_db, monkeypatch
):
    """The in-flight POST counts, so the record says where the time is."""
    llm_env(monkeypatch)
    app, store = build_app(
        tmp_path, sample_db, request_timeout_seconds=0.2, llm_max_concurrency=1
    )
    # Answered late, not never: the handler keeps running after the 504 is
    # produced, and an unanswered POST would outlive the test.
    app.state.translator._llm_transport = hanging_transport(0.6)
    _, token = issue_device(store)
    source = "这是另一句绝不能出现在日志里的句子。"

    with captured_records() as recorder:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": source},
                headers=bearer(token),
            )
        )

    assert response.status_code == 504, response.text
    records = timeouts(recorder)
    assert len(records) == 1, recorder.messages
    record = records[0]
    assert set(record) == TIMEOUT_FIELDS, record
    assert record["stage"] == "llm_post"
    assert record["llm_calls"] == "1"
    assert record["direction"] == "zh->en"
    # The slot was free, so the budget went into the unanswered POST: the
    # stage is the POST and its time is reported even though the span never
    # closed. Zero would be the bug — a request that died in the provider wait
    # would look like it never reached the provider.
    assert float(record["slot_wait_ms"]) == 0.0, record
    assert float(record["llm_post_ms"]) > 0.0, record
    assert float(record["llm_post_ms"]) <= float(record["elapsed_ms"]), record
    assert float(record["elapsed_ms"]) >= 190

    stream = "\n".join(recorder.messages)
    for forbidden in (source, TEST_SECRET, token, API_KEY):
        assert forbidden not in stream


# --------------------------------------------------------------------------
# The completion record
# --------------------------------------------------------------------------


def test_a_completed_model_request_carries_the_split(tmp_path, sample_db, monkeypatch):
    llm_env(monkeypatch)
    app, store = build_app(tmp_path, sample_db, llm_max_concurrency=1)
    app.state.translator._llm_transport = answering_transport(POST_SECONDS)
    _, token = issue_device(store)

    with captured_records() as recorder:
        response = run(
            call(
                app,
                "POST",
                "/v1/translations",
                json={"text": "这是一句没有词典命中的完整句子。"},
                headers=bearer(token),
            )
        )

    assert response.status_code == 200, response.text
    [record] = completions(recorder)
    assert set(record) == BASE_FIELDS | STAGE_FIELDS, record
    assert record["llm_calls"] == "1"
    assert record["direction"] == "zh->en"
    assert float(record["llm_post_ms"]) >= POST_SECONDS * 1000 * 0.8
    assert float(record["slot_wait_ms"]) < float(record["duration_ms"])


def test_a_request_that_never_reached_the_model_keeps_the_old_shape(
    tmp_path, sample_db, monkeypatch
):
    """A record for a dictionary or metadata read is byte-for-byte what it was."""
    llm_env(monkeypatch)
    app, store = build_app(tmp_path, sample_db)
    _, token = issue_device(store)

    with captured_records() as recorder:
        response = run(call(app, "GET", "/v1/meta", headers=bearer(token)))

    assert response.status_code == 200, response.text
    [record] = completions(recorder)
    assert set(record) == BASE_FIELDS, record


def test_two_concurrent_requests_keep_their_own_split(tmp_path, sample_db, monkeypatch):
    """The trace is per request: a neighbour's direction never lands here.

    The middleware starts the trace and the handler mutates it, and those run in
    different tasks (and, for the dictionary stage, a different thread). This
    asserts the two requests that share a process and a semaphore are still told
    apart in their own records.
    """
    llm_env(monkeypatch)
    app, store = build_app(tmp_path, sample_db, llm_max_concurrency=2)
    app.state.translator._llm_transport = answering_transport(POST_SECONDS / 3)
    _, token = issue_device(store)
    to_chinese = "需要翻译的一个中文句子"
    to_english = "Echo replies in this concurrent example sentence."

    async def scenario():
        return await asyncio.gather(
            call(app, "POST", "/v1/translations", json={"text": to_chinese},
                 headers=bearer(token)),
            call(app, "POST", "/v1/translations", json={"text": to_english},
                 headers=bearer(token)),
        )

    with captured_records() as recorder:
        responses = run(scenario())

    by_id: dict[str, dict[str, str]] = {}
    for message in recorder.messages:
        if not message.startswith(COMPLETION_PREFIX):
            continue
        record = parse_record(message, COMPLETION_PREFIX)
        by_id[record["request_id"]] = record
    assert len(by_id) == 2, recorder.messages
    for response in responses:
        record = by_id[response.json()["request_id"]]
        assert record["llm_calls"] == "1", record
        assert float(record["llm_post_ms"]) > 0.0, record
    assert by_id[responses[0].json()["request_id"]]["direction"] == "zh->en"
    assert by_id[responses[1].json()["request_id"]]["direction"] == "en->zh"


# --------------------------------------------------------------------------
# Fields cannot carry anything else
# --------------------------------------------------------------------------


def test_the_post_hooks_pair_once_per_attempt():
    """An end without a begin, or a second end, is a no-op rather than a fault."""
    request_trace.start()
    try:
        request_trace.end_llm_post()
        trace = request_trace.current()
        assert trace is not None
        assert (trace.llm_calls, trace.llm_post_ms) == (0, 0.0)

        request_trace.begin_llm_post()
        request_trace.end_llm_post()
        request_trace.end_llm_post()
        assert trace.llm_calls == 1
        assert trace.llm_post_ms >= 0.0
        assert trace.stage == request_trace.STAGE_INIT
    finally:
        request_trace.reset()


def test_the_vocabulary_is_closed():
    """Every value is a number this module measured or one of its own tokens."""
    trace = request_trace.RequestTrace()

    request_trace.start()
    try:
        # A caller's text cannot reach a field even if one is passed by mistake.
        request_trace.set_direction("秘密 en->zh 秘密")
        request_trace.set_stage("秘密")
        current = request_trace.current()
        assert current is not None
        assert current.direction == request_trace.DIRECTION_UNKNOWN
        assert current.stage == request_trace.STAGE_INIT
    finally:
        request_trace.reset()

    rendered = request_trace.timing_fields(
        trace, include_elapsed=True, include_stage=True
    )
    assert rendered.startswith(" ")
    keys = {token.partition("=")[0] for token in rendered.split()}
    assert keys == {"elapsed_ms", "stage"} | {
        "slot_wait_ms",
        "llm_post_ms",
        "llm_calls",
        "direction",
    }
    assert "秘密" not in rendered


def test_a_record_is_written_for_every_request_not_only_traced_ones(
    tmp_path, sample_db
):
    """Without a trace the timeout record still has the fields, as `-`."""
    assert request_trace.timing_fields(
        None, include_elapsed=True, include_stage=True
    ) == (
        " elapsed_ms=- stage=- slot_wait_ms=- llm_post_ms=- llm_calls=- direction=-"
    )


@pytest.mark.parametrize(
    "target, token",
    [("zh", request_trace.DIRECTION_EN_TO_ZH),
     ("en", request_trace.DIRECTION_ZH_TO_EN),
     (None, request_trace.DIRECTION_UNKNOWN),
     ("秘密", request_trace.DIRECTION_UNKNOWN)],
)
def test_direction_from_target_is_closed(target, token):
    assert request_trace.direction_from_target(target) == token
