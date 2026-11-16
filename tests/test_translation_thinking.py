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


def test_defaults_disable_thinking_into_english_and_send_nothing_into_chinese(monkeypatch):
    zh_en, zh_en_kwargs = _post(monkeypatch, False)
    en_zh, en_zh_kwargs = _post(monkeypatch, True)

    assert list(zh_en) == [*BODY_KEYS, "thinking"] and zh_en["thinking"] == DISABLED["thinking"]
    assert "reasoning_effort" not in zh_en and "max_tokens" not in zh_en
    assert zh_en_kwargs == {"purpose": "translation", "extra_body": DISABLED}
    assert list(en_zh) == BODY_KEYS
    assert en_zh_kwargs == {"purpose": "translation"}  # no extra_body keyword at all


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_omit_sends_no_reasoning_field_in_either_direction(monkeypatch, var):
    body, kwargs = _post(monkeypatch, var == EN_ZH, {var: "omit"})

    assert list(body) == BODY_KEYS
    assert kwargs == {"purpose": "translation"}


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
    other_default = _post(monkeypatch, not own)

    body, _ = _post(monkeypatch, own, {var: mode})
    other = _post(monkeypatch, not own, {var: mode})

    assert body == {**default, **extra}  # model, messages and temperature unchanged
    assert other == other_default  # the other direction's body and kwargs did not move


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_an_unknown_mode_warns_naming_the_variable_and_uses_that_directions_default(
    monkeypatch, caplog, var
):
    default = _post(monkeypatch, var == EN_ZH)
    fallback = "omit" if var == EN_ZH else "disabled"

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        unknown = _post(monkeypatch, var == EN_ZH, {var: "medium-ish"})

    assert unknown == default  # body and transport kwargs both
    assert [r.getMessage() for r in caplog.records] == [f"unknown {var}; using {fallback}"]


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_a_blank_mode_is_that_directions_default(monkeypatch, var):
    assert _post(monkeypatch, var == EN_ZH, {var: "  "}) == _post(monkeypatch, var == EN_ZH)
