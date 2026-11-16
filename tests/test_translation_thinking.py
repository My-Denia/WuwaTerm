"""``WUWATERM_TRANSLATION_THINKING_ZH_EN`` / ``_EN_ZH``: the reasoning fields a request carries.

Every case reads the JSON body the real transport POSTs to an ``httpx.MockTransport`` model.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from tests.test_api_semantic_review import FakeModel, configure_llm
from wuwaterm import llm_transport
from wuwaterm.sentence import _call_llm_async

ZH_EN = "WUWATERM_TRANSLATION_THINKING_ZH_EN"  # requests translating Chinese into English
EN_ZH = "WUWATERM_TRANSLATION_THINKING_EN_ZH"  # requests translating English into Chinese
DISABLED = {"thinking": {"type": "disabled"}}
BODY_KEYS = ["model", "messages", "temperature"]  # the request before the setting existed


def _post(monkeypatch, to_chinese: bool, env: dict[str, str] | None = None) -> tuple[dict, dict]:
    """One translation: the JSON body the mock model received, and the kwargs the transport call got."""
    configure_llm(monkeypatch, semantic_enabled=False)
    for name in (ZH_EN, EN_ZH):
        monkeypatch.delenv(name, raising=False)
    for name, mode in (env or {}).items():
        monkeypatch.setenv(name, mode)
    model, kwargs, real = FakeModel("ok"), [], llm_transport.post_chat_completion

    async def spy(client, messages, **options):
        kwargs.append(options)
        return await real(client, messages, **options)  # still the real request path

    monkeypatch.setattr(llm_transport, "post_chat_completion", spy)
    asyncio.run(_call_llm_async("x", (), to_chinese=to_chinese, transport=httpx.MockTransport(model)))
    assert model.calls == len(kwargs) == 1
    return model.body(), kwargs[0]


@pytest.mark.parametrize("to_chinese", [False, True])
def test_defaults_send_no_reasoning_field(monkeypatch, to_chinese):
    body, kwargs = _post(monkeypatch, to_chinese)

    assert list(body) == BODY_KEYS
    assert kwargs == {"purpose": "translation"}  # no extra_body keyword at all


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("disabled", DISABLED),
        ("low", {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
        ("high", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ],
)
def test_a_mode_reaches_only_its_own_direction(monkeypatch, var, mode, extra):
    own = var == EN_ZH  # the setting's direction, as to_chinese
    default, _ = _post(monkeypatch, own)

    body, _ = _post(monkeypatch, own, {var: mode})
    other, other_kwargs = _post(monkeypatch, not own, {var: mode})

    assert body == {**default, **extra}  # model, messages and temperature unchanged
    assert list(other) == BODY_KEYS and "extra_body" not in other_kwargs


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_an_unknown_mode_warns_naming_the_variable_and_uses_the_default(monkeypatch, caplog, var):
    default, _ = _post(monkeypatch, var == EN_ZH)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        body, kwargs = _post(monkeypatch, var == EN_ZH, {var: "medium-ish"})

    assert body == default and "extra_body" not in kwargs
    assert [r.getMessage() for r in caplog.records] == [f"unknown {var}; using omit"]
