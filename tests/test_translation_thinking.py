"""``WUWATERM_TRANSLATION_THINKING_ZH_EN`` / ``_EN_ZH``: the reasoning fields a request carries.

Every case reads the JSON body the real transport POSTs to an ``httpx.MockTransport`` model.

Into English the mode is an instruction and is sent as written. Into Chinese the
default is ``auto``: the request picks a mode for itself from its own shape, and
only a value an operator names explicitly replaces that — which is what makes
``WUWATERM_TRANSLATION_THINKING_EN_ZH=omit`` the documented rollback.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from tests.test_api_semantic_review import FakeModel, configure_llm
from wuwaterm import llm_transport
from wuwaterm.sentence import (
    TRANSLATION_THINKING_EN_ZH_DEFAULT,
    _call_llm_async,
    select_en_zh_auto_thinking,
)

ZH_EN = "WUWATERM_TRANSLATION_THINKING_ZH_EN"  # requests translating Chinese into English
EN_ZH = "WUWATERM_TRANSLATION_THINKING_EN_ZH"  # requests translating English into Chinese
DISABLED = {"thinking": {"type": "disabled"}}
LOW = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
HIGH = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
OMIT: dict = {}
BODY_KEYS = ["model", "messages", "temperature"]  # the request before the setting existed


def _reasoning_fields(mode: str) -> dict:
    """The two provider fields one mode sends, as ``extra_body``."""
    return {"disabled": DISABLED, "low": LOW, "high": HIGH, "omit": OMIT}[mode]


def _paragraphs(count: int) -> str:
    """A source with exactly ``count`` blank-line-separated paragraphs."""
    return "\n\n".join(
        f"The {index} synthetic paragraph carries one ordinary sentence."
        for index in range(count)
    )


def _locks(count: int) -> tuple[tuple[str, str, str], ...]:
    """``count`` locked terms, in the shape the prompt renders."""
    return tuple(
        (f"__WUWA_TERM_{index:04d}__", f"官方词条{index}", f"official term {index}")
        for index in range(count)
    )


def _post(
    monkeypatch,
    to_chinese: bool,
    env: dict[str, str] | None = None,
    *,
    text: str = "x",
    locks: tuple[tuple[str, str, str], ...] = (),
) -> tuple[dict, dict]:
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
    asyncio.run(
        _call_llm_async(text, locks, to_chinese=to_chinese, transport=httpx.MockTransport(model))
    )
    assert model.calls == len(kwargs) == 1
    return model.body(), kwargs[0]


def test_defaults_disable_thinking_into_english_and_follow_the_request_into_chinese(
    monkeypatch,
):
    zh_en, zh_en_kwargs = _post(monkeypatch, False)
    en_zh, en_zh_kwargs = _post(monkeypatch, True)

    assert list(zh_en) == [*BODY_KEYS, "thinking"] and zh_en["thinking"] == DISABLED["thinking"]
    assert "reasoning_effort" not in zh_en and "max_tokens" not in zh_en
    assert zh_en_kwargs == {"purpose": "translation", "extra_body": DISABLED}
    # A one-line, lock-free English source is the shape `auto` thinks on.
    assert TRANSLATION_THINKING_EN_ZH_DEFAULT == "auto"
    assert list(en_zh) == [*BODY_KEYS, "thinking", "reasoning_effort"]
    assert en_zh["reasoning_effort"] == "low" and en_zh["thinking"] == LOW["thinking"]
    assert en_zh_kwargs == {"purpose": "translation", "extra_body": LOW}


@pytest.mark.parametrize("raw_thinking", ["disabled", "low", "high", "omit"])
def test_an_explicit_en_zh_mode_bypasses_auto_and_is_sent_as_written(
    monkeypatch, raw_thinking
):
    """An operator's named mode is an instruction, not a hint.

    Read on the request shape that ``auto`` sends with thinking OFF (30
    paragraphs, 11 locks): the explicit value still decides, so
    ``WUWATERM_TRANSLATION_THINKING_EN_ZH`` stays the rollback switch.
    """
    explicit, explicit_kwargs = _post(
        monkeypatch,
        True,
        {EN_ZH: raw_thinking},
        text=_paragraphs(30),
        locks=_locks(11),
    )
    short, short_kwargs = _post(monkeypatch, True, {EN_ZH: raw_thinking})

    assert explicit_kwargs == short_kwargs == {
        "purpose": "translation",
        **({} if not _reasoning_fields(raw_thinking) else {
            "extra_body": _reasoning_fields(raw_thinking)
        }),
    }
    assert {key: value for key, value in explicit.items() if key != "messages"} == {
        key: value for key, value in short.items() if key != "messages"
    }
    assert {
        key: value
        for key, value in explicit.items()
        if key in ("thinking", "reasoning_effort")
    } == _reasoning_fields(raw_thinking)


def test_an_explicit_disabled_and_omit_send_what_they_always_sent(monkeypatch):
    disabled, disabled_kwargs = _post(monkeypatch, True, {EN_ZH: "disabled"})
    omitted, omitted_kwargs = _post(monkeypatch, True, {EN_ZH: "omit"})

    assert disabled_kwargs == {"purpose": "translation", "extra_body": DISABLED}
    assert list(disabled) == [*BODY_KEYS, "thinking"]
    assert omitted_kwargs == {"purpose": "translation"}  # no extra_body keyword at all
    assert list(omitted) == BODY_KEYS


@pytest.mark.parametrize(
    ("text", "locks", "expected"),
    [
        # Measured Phase 1 shapes: both timed out under thinking and answered in
        # 3.3 s with it off, so `auto` turns it off.
        ("D-009 shaped", 11, "disabled"),
        ("Plan B shaped", 5, "disabled"),
        # Measured: answered in 20.7 s under `low`, so `auto` keeps thinking.
        ("D-012 shaped", 2, "low"),
        # A two-paragraph title and a lock-free document are ordinary segments.
        ("D-010 shaped", 0, "low"),
        ("ordinary shaped", 2, "low"),
    ],
)
def test_auto_into_chinese_picks_the_mode_from_the_request_shape(
    monkeypatch, text, locks, expected
):
    paragraph_count = {
        "D-009 shaped": 30,
        "Plan B shaped": 10,
        "D-012 shaped": 23,
        "D-010 shaped": 2,
        "ordinary shaped": 6,
    }[text]
    source = _paragraphs(paragraph_count)

    assert select_en_zh_auto_thinking(source, lock_count=locks) == expected
    body, _ = _post(monkeypatch, True, text=source, locks=_locks(locks))
    assert {
        key: value
        for key, value in body.items()
        if key in ("thinking", "reasoning_effort")
    } == _reasoning_fields(expected)


def test_auto_switches_on_when_the_request_crosses_either_bound(monkeypatch):
    """EITHER bound is enough: four locks or twenty-five paragraphs."""
    assert select_en_zh_auto_thinking(_paragraphs(24), lock_count=3) == "low"
    assert select_en_zh_auto_thinking(_paragraphs(24), lock_count=4) == "disabled"
    assert select_en_zh_auto_thinking(_paragraphs(25), lock_count=3) == "disabled"
    assert select_en_zh_auto_thinking(_paragraphs(1), lock_count=9) == "disabled"


def test_auto_only_ever_sends_a_concrete_mode(monkeypatch):
    """The token itself never reaches the provider."""
    for source, locks, expected in (
        (_paragraphs(23), 2, "low"),
        (_paragraphs(30), 11, "disabled"),
    ):
        body, kwargs = _post(monkeypatch, True, text=source, locks=_locks(locks))
        assert body.get("reasoning_effort") != "auto"
        assert kwargs["extra_body"] == _reasoning_fields(expected)


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

    # model, messages and temperature unchanged; only the reasoning fields move,
    # and they are exactly the ones the named mode sends.
    reasoning = ("thinking", "reasoning_effort")
    assert {key: value for key, value in body.items() if key not in reasoning} == {
        key: value for key, value in default.items() if key not in reasoning
    }
    assert {key: value for key, value in body.items() if key in reasoning} == extra
    assert other == other_default  # the other direction's body and kwargs did not move


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_an_unknown_mode_warns_naming_the_variable_and_uses_that_directions_default(
    monkeypatch, caplog, var
):
    default = _post(monkeypatch, var == EN_ZH)
    fallback = TRANSLATION_THINKING_EN_ZH_DEFAULT if var == EN_ZH else "disabled"

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        unknown = _post(monkeypatch, var == EN_ZH, {var: "medium-ish"})

    assert unknown == default  # body and transport kwargs both
    assert [r.getMessage() for r in caplog.records] == [f"unknown {var}; using {fallback}"]


def test_auto_is_unknown_into_english_and_warns(monkeypatch, caplog):
    """There is no request shape into English to adapt, so `auto` is a typo there."""
    default = _post(monkeypatch, False)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        typo = _post(monkeypatch, False, {ZH_EN: "auto"})

    assert typo == default
    assert [r.getMessage() for r in caplog.records] == [f"unknown {ZH_EN}; using disabled"]


@pytest.mark.parametrize("var", [ZH_EN, EN_ZH])
def test_a_blank_mode_is_that_directions_default(monkeypatch, var):
    assert _post(monkeypatch, var == EN_ZH, {var: "  "}) == _post(monkeypatch, var == EN_ZH)
