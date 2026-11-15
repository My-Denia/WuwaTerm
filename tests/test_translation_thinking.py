"""``WUWATERM_TRANSLATION_THINKING_ZH_EN`` / ``_EN_ZH``: the reasoning fields a request carries.

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

ZH_EN = "WUWATERM_TRANSLATION_THINKING_ZH_EN"  # requests translating Chinese into English
EN_ZH = "WUWATERM_TRANSLATION_THINKING_EN_ZH"  # requests translating English into Chinese
OLD_ENV = "WUWATERM_TRANSLATION_THINKING"  # the single variable; it never shipped
ENV_BY_DIRECTION = {False: ZH_EN, True: EN_ZH}  # keyed by to_chinese
LOCKS = (("__WUWA_TERM_t_0000__", "声骸", "Echo"),)
DISABLED = {"thinking": {"type": "disabled"}}
ENABLED = {"thinking": {"type": "enabled"}}
PREVIOUS_KEYS = ["model", "messages", "temperature"]  # the request before the setting existed


def _configure(monkeypatch, **modes: str | None) -> None:
    """Point the translator at the mock model and set only the given variables."""
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    for name in (ZH_EN, EN_ZH, OLD_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, mode in modes.items():
        if mode is not None:
            monkeypatch.setenv(name, mode)


def _body(monkeypatch, *, to_chinese: bool, html_mode: bool = False, **modes) -> dict:
    """The JSON body one translation request posts."""
    _configure(monkeypatch, **modes)
    seen: list[dict] = []

    def model(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    asyncio.run(
        _call_llm_async(
            "<b>__WUWA_TERM_t_0000__</b>" if html_mode else "__WUWA_TERM_t_0000__",
            LOCKS,
            html_mode=html_mode,
            to_chinese=to_chinese,
            transport=httpx.MockTransport(model),
        )
    )
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("to_chinese", [False, True])
@pytest.mark.parametrize("mode", [None, "", "omit"])
def test_default_sends_exactly_the_previous_request(monkeypatch, to_chinese, mode):
    body = _body(monkeypatch, to_chinese=to_chinese, **{ENV_BY_DIRECTION[to_chinese]: mode})

    assert list(body) == PREVIOUS_KEYS
    assert "thinking" not in body and "reasoning_effort" not in body
    assert "max_tokens" not in body


@pytest.mark.parametrize("to_chinese", [False, True])
@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("disabled", DISABLED),
        ("low", {**ENABLED, "reasoning_effort": "low"}),
        ("high", {**ENABLED, "reasoning_effort": "high"}),
    ],
)
def test_mode_adds_only_the_reasoning_fields(monkeypatch, to_chinese, mode, extra):
    baseline = _body(monkeypatch, to_chinese=to_chinese)
    assert list(baseline) == PREVIOUS_KEYS

    body = _body(monkeypatch, to_chinese=to_chinese, **{ENV_BY_DIRECTION[to_chinese]: mode})

    assert body == {**baseline, **extra}  # model, messages and temperature unchanged
    assert "max_tokens" not in body


def test_each_setting_affects_only_its_own_direction(monkeypatch):
    zh_en_base = _body(monkeypatch, to_chinese=False)
    en_zh_base = _body(monkeypatch, to_chinese=True)

    # ZH_EN=disabled: the zh->en body gains the field, the en->zh body does not.
    assert _body(monkeypatch, to_chinese=False, **{ZH_EN: "disabled"}) == {**zh_en_base, **DISABLED}
    assert _body(monkeypatch, to_chinese=True, **{ZH_EN: "disabled"}) == en_zh_base
    # EN_ZH=low: the reverse.
    low = {**ENABLED, "reasoning_effort": "low"}
    assert _body(monkeypatch, to_chinese=True, **{EN_ZH: "low"}) == {**en_zh_base, **low}
    assert _body(monkeypatch, to_chinese=False, **{EN_ZH: "low"}) == zh_en_base


@pytest.mark.parametrize("to_chinese", [False, True])
def test_the_old_single_variable_has_no_effect(monkeypatch, to_chinese):
    baseline = _body(monkeypatch, to_chinese=to_chinese)

    assert _body(monkeypatch, to_chinese=to_chinese, **{OLD_ENV: "disabled"}) == baseline


@pytest.mark.parametrize("to_chinese", [False, True])
def test_unknown_mode_warns_naming_the_variable_and_uses_omit(monkeypatch, caplog, to_chinese):
    env = ENV_BY_DIRECTION[to_chinese]
    baseline = _body(monkeypatch, to_chinese=to_chinese)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        body = _body(monkeypatch, to_chinese=to_chinese, **{env: "medium-ish"})

    assert body == baseline
    assert [r.getMessage() for r in caplog.records] == [f"unknown {env}; using omit"]


@pytest.mark.parametrize("to_chinese", [False, True])
@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("disabled", DISABLED),
        ("high", {**ENABLED, "reasoning_effort": "high"}),
    ],
)
def test_html_mode_request_carries_the_field(monkeypatch, to_chinese, mode, extra):
    body = _body(
        monkeypatch, to_chinese=to_chinese, html_mode=True, **{ENV_BY_DIRECTION[to_chinese]: mode}
    )

    assert {key: body[key] for key in extra} == extra
    assert "translate only the visible" in body["messages"][0]["content"]


def test_html_mode_default_sends_no_reasoning_field(monkeypatch):
    body = _body(monkeypatch, to_chinese=False, html_mode=True)

    assert list(body) == PREVIOUS_KEYS
    assert "translate only the visible" in body["messages"][0]["content"]


def test_extra_body_reaches_the_transport_call_only_when_a_field_is_set(monkeypatch):
    """An evaluation runner patches ``llm_transport.post_chat_completion`` and reads ``extra_body``."""
    calls: list[dict] = []

    async def fake(client, messages, **kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(llm_transport, "post_chat_completion", fake)
    _configure(monkeypatch, **{ZH_EN: "disabled"})
    asyncio.run(_call_llm_async("x", (), to_chinese=False))
    asyncio.run(_call_llm_async("x", (), to_chinese=True))
    _configure(monkeypatch)
    asyncio.run(_call_llm_async("x", (), to_chinese=False))

    assert calls == [
        {"purpose": "translation", "extra_body": DISABLED},
        {"purpose": "translation"},
        {"purpose": "translation"},
    ]


def test_a_model_read_timeout_answers_llm_unavailable(monkeypatch, tmp_path, sample_db):
    """The failure thinking mode caused: the model outlasts the timeout, the API answers 503."""
    _configure(monkeypatch, **{ZH_EN: "disabled"})

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
