"""``WUWATERM_TRANSLATION_THINKING``: the reasoning fields a translation request carries.

Every case captures the JSON body the real transport posts to an
``httpx.MockTransport`` model; nothing replaces the function under test.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from tests.test_api import bearer, build_settings, call, issue_device, run
from wuwaterm import llm_transport
from wuwaterm.sentence import SentenceTranslator, _call_llm_async
from wuwaterm_api.app import create_app
from wuwaterm_api.auth import DeviceStore

ENV = "WUWATERM_TRANSLATION_THINKING"
LOCKS = (("__WUWA_TERM_t_0000__", "声骸", "Echo"),)
DISABLED = {"thinking": {"type": "disabled"}}
ENABLED = {"thinking": {"type": "enabled"}}


def _configure(monkeypatch, mode: str | None) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    if mode is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, mode)


def _body(monkeypatch, mode: str | None, *, html_mode: bool = False) -> dict:
    """The JSON body one translation request posts under ``mode``."""
    _configure(monkeypatch, mode)
    seen: list[dict] = []

    def model(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    asyncio.run(
        _call_llm_async(
            "<b>__WUWA_TERM_t_0000__</b>" if html_mode else "__WUWA_TERM_t_0000__",
            LOCKS,
            html_mode=html_mode,
            transport=httpx.MockTransport(model),
        )
    )
    assert len(seen) == 1
    return seen[0]


PREVIOUS_KEYS = ["model", "messages", "temperature"]  # the request before the setting existed


@pytest.mark.parametrize("mode", [None, "", "omit"])
def test_default_sends_exactly_the_previous_request(monkeypatch, mode):
    body = _body(monkeypatch, mode)

    assert list(body) == PREVIOUS_KEYS
    assert "thinking" not in body and "reasoning_effort" not in body
    assert "max_tokens" not in body


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("disabled", DISABLED),
        ("low", {**ENABLED, "reasoning_effort": "low"}),
        ("high", {**ENABLED, "reasoning_effort": "high"}),
    ],
)
def test_mode_adds_only_the_reasoning_fields(monkeypatch, mode, extra):
    baseline = _body(monkeypatch, "omit")
    assert list(baseline) == PREVIOUS_KEYS

    body = _body(monkeypatch, mode)

    assert body == {**baseline, **extra}  # model, messages and temperature unchanged
    assert "max_tokens" not in body


def test_unknown_mode_warns_and_uses_omit(monkeypatch, caplog):
    baseline = _body(monkeypatch, "omit")

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        body = _body(monkeypatch, "medium-ish")

    assert body == baseline
    assert list(body) == PREVIOUS_KEYS
    assert [r.getMessage() for r in caplog.records] == [f"unknown {ENV}; using omit"]


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("disabled", DISABLED),
        ("high", {**ENABLED, "reasoning_effort": "high"}),
    ],
)
def test_html_mode_request_carries_the_field(monkeypatch, mode, extra):
    body = _body(monkeypatch, mode, html_mode=True)

    assert {key: body[key] for key in extra} == extra
    assert "translate only the visible" in body["messages"][0]["content"]


def test_html_mode_default_sends_no_reasoning_field(monkeypatch):
    body = _body(monkeypatch, None, html_mode=True)

    assert list(body) == PREVIOUS_KEYS
    assert "translate only the visible" in body["messages"][0]["content"]


def test_default_passes_no_extra_body_to_the_transport_call(monkeypatch):
    """An evaluation runner patches ``llm_transport.post_chat_completion`` and reads ``extra_body``."""
    calls: list[dict] = []

    async def fake(client, messages, **kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(llm_transport, "post_chat_completion", fake)
    monkeypatch.delenv(ENV, raising=False)
    asyncio.run(_call_llm_async("x", ()))
    for mode in ("omit", "disabled"):
        monkeypatch.setenv(ENV, mode)
        asyncio.run(_call_llm_async("x", ()))

    assert calls == [
        {"purpose": "translation"},
        {"purpose": "translation"},
        {"purpose": "translation", "extra_body": DISABLED},
    ]


def test_a_model_read_timeout_answers_llm_unavailable(monkeypatch, tmp_path, sample_db):
    """The failure thinking mode caused: the model outlasts the timeout, the API answers 503."""
    _configure(monkeypatch, "disabled")

    posted: list[dict] = []

    def model(request):
        posted.append(json.loads(request.content))
        raise httpx.ReadTimeout("slow", request=request)

    settings = build_settings(tmp_path, sample_db)
    store = DeviceStore(settings.device_db_path)
    store.initialize()
    translator = SentenceTranslator(sample_db, llm_transport=httpx.MockTransport(model))
    app = create_app(settings, device_store=store, translator=translator)
    _, token = issue_device(store, "owner desktop")

    response = run(
        call(
            app,
            "POST",
            "/v1/translations",
            json={"text": "今汐装备了声骸。"},
            headers=bearer(token),
        )
    )

    assert [body["thinking"] for body in posted] == [DISABLED["thinking"]]  # reached the model once
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"
