"""Provider compatibility through real Core calls and a synthetic HTTP server."""

from __future__ import annotations

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest

from tests.test_api import bearer, build_client_app, issue_device
from wuwaterm.application import TranslationJob, translate_request, translate_request_async
from wuwaterm.db import connect, initialize
from wuwaterm import llm_transport as lt
from wuwaterm.sentence import SentenceTranslator
from wuwaterm_api.errors import MESSAGE_BY_CODE


SOURCE = "窗边的纸鹤正在旋转。"
ANSWER = "A paper crane turns by the window."
ZH_EN = "WUWATERM_TRANSLATION_THINKING_ZH_EN"
EN_ZH = "WUWATERM_TRANSLATION_THINKING_EN_ZH"
UNTRUSTED = "synthetic-source synthetic-secret https://private.example.invalid/path"


class Provider:
    def __init__(self):
        self.status = 400
        self.parameter = "thinking"
        self.error = {"error": {"code": "unsupported_parameter", "param": "thinking", "message": UNTRUSTED}}
        self.calls = []


@pytest.fixture
def provider(monkeypatch):
    state = Provider()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append((self.command, self.path, body))
            status = state.status
            if state.parameter is not None and state.parameter not in body:
                status = 200
            response = state.error if status != 200 else {"choices": [{"message": {"content": ANSWER}}]}
            raw = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "synthetic-placeholder")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "synthetic-model")
    for key in (ZH_EN, EN_ZH, "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert server.socket.fileno() == -1


@pytest.fixture
def empty_db(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with connect(path) as conn:
        initialize(conn)
    return path


def translate(db_path, entrypoint="raise"):
    translator = SentenceTranslator(db_path, llm_timeout_seconds=2)
    assert not translator.service.lookup_exact(SOURCE).exact
    job = TranslationJob(text=SOURCE, forced_to_chinese=False)
    if entrypoint == "sync":
        return translator.translate(SOURCE)
    if entrypoint == "application_sync":
        return translate_request(translator.service, translator, job)

    async def go():
        try:
            if entrypoint == "application_async":
                return await translate_request_async(translator.service, translator, job)
            return await translator.translate_async(SOURCE, propagate_errors=entrypoint == "raise")
        finally:
            await translator.aclose()

    return asyncio.run(go())


@pytest.mark.parametrize("parameter", ["thinking", "reasoning_effort"])
def test_structured_rejection_has_safe_actionable_diagnostic(
    provider, empty_db, monkeypatch, caplog, parameter
):
    caplog.set_level(logging.WARNING, logger="wuwaterm.llm_transport")
    provider.parameter = parameter
    provider.error["error"]["param"] = parameter
    if parameter == "reasoning_effort":
        monkeypatch.setenv(ZH_EN, "low")
    records = []
    remove = lt.add_call_observer(records.append)
    try:
        with pytest.raises(lt.LLMTranslationError) as caught:
            translate(empty_db)
    finally:
        remove()

    error = caught.value
    assert error.reason == error.diagnostic.reason == "http"
    assert error.diagnostic.detail == f"unsupported_{parameter}"
    assert error.user_message == lt.TRANSLATION_UNAVAILABLE_NOTICE
    assert [(r.outcome, r.status_code) for r in records] == [("http_error", 400)]
    assert len(provider.calls) == 1
    assert provider.calls[0][:2] == ("POST", "/v1/chat/completions")
    body = provider.calls[0][2]
    assert body["messages"][-1] == {"role": "user", "content": SOURCE}
    assert body["thinking"] == {"type": "enabled" if parameter == "reasoning_effort" else "disabled"}
    if parameter == "reasoning_effort":
        assert body["reasoning_effort"] == "low"
    assert f"parameter={parameter}" in caplog.text
    assert f"{ZH_EN}=omit" in caplog.text and f"{EN_ZH}=omit" in caplog.text
    assert "provider default" in caplog.text and "does not disable" in caplog.text
    for value in (UNTRUSTED, "synthetic-secret", "synthetic-source", "private.example.invalid"):
        assert value not in caplog.text + str(error) + repr(error.diagnostic)


@pytest.mark.parametrize("entrypoint", ["sync", "async", "application_sync", "application_async"])
def test_same_strict_endpoint_accepts_explicit_omit_and_supported_default(
    provider, empty_db, monkeypatch, entrypoint
):
    result = translate(empty_db, entrypoint)
    if isinstance(result, str):
        assert result == lt.TRANSLATION_UNAVAILABLE_NOTICE
    else:
        assert (result.kind, result.error_code) == ("error", "llm_unavailable")
        if result.llm_failure is not None:
            assert result.llm_failure.detail == "unsupported_thinking"
    assert len(provider.calls) == 1
    assert provider.calls[-1][2]["thinking"] == {"type": "disabled"}

    monkeypatch.setenv(ZH_EN, "omit")
    result = translate(empty_db, entrypoint)
    assert (result if isinstance(result, str) else result.text) == ANSWER
    if not isinstance(result, str):
        assert result.kind == "llm"
    assert len(provider.calls) == 2
    assert "thinking" not in provider.calls[-1][2]
    assert "reasoning_effort" not in provider.calls[-1][2]

    provider.status = 200
    monkeypatch.delenv(ZH_EN)
    result = translate(empty_db, entrypoint)
    assert (result if isinstance(result, str) else result.text) == ANSWER
    assert len(provider.calls) == 3
    assert provider.calls[-1][2]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("status,reason", [(400, "http"), (401, "http"), (429, "rate_limit"), (500, "upstream"), (503, "upstream")])
def test_ordinary_http_failures_keep_reason_and_one_post(provider, empty_db, status, reason, caplog):
    provider.parameter = None
    provider.status = status
    provider.error = {"error": {"code": "invalid_request_error", "param": "thinking", "message": UNTRUSTED}}
    with pytest.raises(lt.LLMTranslationError) as caught:
        translate(empty_db)
    assert caught.value.reason == reason
    assert caught.value.diagnostic.detail == "unspecified"
    assert len(provider.calls) == 1
    assert "provider default" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        {"code": "unsupported_parameter", "param": "temperature"},
        {"code": "unsupported_parameter", "param": "thinking.type"},
        {"code": "unsupported_parameter", "param": None},
        {"code": "unsupported_parameter", "param": ["thinking"]},
        {"code": "unsupported_parameter", "param": {"name": "thinking"}},
        {"code": "unsupported_parameter"},
        {"type": "unsupported_parameter", "param": "thinking"},
        {"code": "invalid_request_error", "param": "thinking"},
        {"message": "unsupported thinking and reasoning_effort"},
        "unsupported thinking and reasoning_effort",
    ],
)
def test_lookalikes_do_not_trigger_compatibility_diagnostic(provider, empty_db, error, caplog):
    provider.parameter = None
    provider.error = {"error": error}
    with pytest.raises(lt.LLMTranslationError) as caught:
        translate(empty_db)
    assert caught.value.reason == "http"
    assert caught.value.diagnostic.detail == "unspecified"
    assert len(provider.calls) == 1
    assert "provider default" not in caplog.text


@pytest.mark.parametrize("status,reason", [(401, "http"), (429, "rate_limit"), (500, "upstream")])
def test_auth_rate_and_server_status_take_precedence(provider, empty_db, status, reason):
    provider.status = status
    with pytest.raises(lt.LLMTranslationError) as caught:
        translate(empty_db)
    assert caught.value.reason == reason
    assert caught.value.diagnostic.detail == "unspecified"
    assert len(provider.calls) == 1


def test_strict_provider_preserves_api_error_envelope(provider, empty_db, tmp_path, caplog):
    app, store = build_client_app(tmp_path, empty_db)
    _, token = issue_device(store, "synthetic-provider-fixture")

    async def go():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as client:
                return await client.post("/v1/translations", json={"text": SOURCE, "to": "en"}, headers=bearer(token))

    response = asyncio.run(go())
    body = response.json()
    assert response.status_code == 503
    assert set(body) == {"error", "request_id"}
    assert body["error"] == {"code": "llm_unavailable", "message": MESSAGE_BY_CODE["llm_unavailable"]}
    assert "detail=unsupported_thinking" in caplog.text
    assert "synthetic-secret" not in caplog.text + response.text
    assert "private.example.invalid" not in caplog.text + response.text
    assert len(provider.calls) == 1
