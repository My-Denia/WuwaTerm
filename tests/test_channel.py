from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, RetryAfter, TelegramError

from wuwaterm.logging_utils import REDACTION_SECRET_ENV, redact_id
from wuwaterm.bot import (
    ADMIN_CACHE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    SERVICE_KEY,
    LLM_INPUT_CHAR_LIMIT,
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    TRANSLATOR_KEY,
    AdminStatusCache,
    BotConfig,
    ChannelReplyIndex,
    PerChatRateLimiter,
    term_command,
)
from wuwaterm.channel import (
    CapacitySkipNotifier,
    channel_post_handler,
    count_cjk,
    send_with_flood_retry,
    strip_telegram_html,
    validate_telegram_html,
)
from wuwaterm.runtime_keys import CHANNEL_CAPACITY_NOTIFIER_KEY
from wuwaterm.lookup import TermService
from wuwaterm.sentence import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMTranslationError,
    SentenceTranslator,
    _llm_error_from_response,
)
from wuwaterm.settings import ChatSettings


CN_TEXT = "这是一段需要翻译的频道文本"
CN_TEXT_HTML = (
    '<b>这是一段</b><a href="https://example.com">需要翻译</a>的频道'
    "<tg-spoiler>文本</tg-spoiler>"
)
CN_LOCK_TEXT = "今汐说声骸很强"
CN_LOCK_TEXT_HTML = (
    '<b>今汐</b>说<a href="https://example.com">声骸</a>很'
    "<tg-spoiler>强</tg-spoiler>"
)


def telegram_text_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        text: str | None = None,
        caption: str | None = None,
        text_html: str | None = None,
        caption_html: str | None = None,
        sender_chat=None,
        is_automatic_forward: bool = False,
        date: datetime | None = None,
        edit_date: datetime | None = None,
    ):
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.text_html = text_html
        self.caption_html = caption_html
        self.sender_chat = sender_chat
        self.is_automatic_forward = is_automatic_forward
        self.date = date
        self.edit_date = edit_date
        self.replies: list[tuple[str, str | None, int | None]] = []
        self._reply_count = 0

    async def reply_text(self, text: str, **kwargs):
        if telegram_text_units(text) > TELEGRAM_TEXT_MESSAGE_LIMIT:
            raise BadRequest("Message is too long")
        self._reply_count += 1
        self.replies.append(
            (text, kwargs.get("parse_mode"), kwargs.get("reply_to_message_id"))
        )
        base_offset = 1001 if self.edit_date is not None else 1000
        return SimpleNamespace(
            message_id=self.message_id + base_offset + self._reply_count - 1
        )


class FakeBot:
    def __init__(
        self,
        default_status: str = "administrator",
        edit_raises=None,
        delete_raises=None,
        send_raises=None,
    ):
        self.default_status = default_status
        self.member_calls: list[tuple[int, int]] = []
        self.edits: list[tuple[str, str | None, int | None, int | None]] = []
        self.edit_attempts: list[tuple[str, str | None, int | None, int | None]] = []
        self.deleted_messages: list[tuple[int | None, int | None]] = []
        self.sent_messages: list[tuple[int | None, str | None]] = []
        # edit_raises(text, parse_mode, chat_id, message_id) -> Exception | None
        self._edit_raises = edit_raises
        # delete_raises(chat_id, message_id) -> Exception | None
        self._delete_raises = delete_raises
        # send_raises: exception instance raised by send_message
        self._send_raises = send_raises

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.member_calls.append((chat_id, user_id))
        return SimpleNamespace(status=self.default_status)

    async def send_message(self, chat_id=None, text=None, **kwargs):
        if self._send_raises is not None:
            raise self._send_raises
        self.sent_messages.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kwargs):
        parse_mode = kwargs.get("parse_mode")
        self.edit_attempts.append((text, parse_mode, chat_id, message_id))
        if text is not None and telegram_text_units(text) > TELEGRAM_TEXT_MESSAGE_LIMIT:
            raise BadRequest("Message is too long")
        if self._edit_raises is not None:
            exc = self._edit_raises(text, parse_mode, chat_id, message_id)
            if exc is not None:
                raise exc
        self.edits.append((text, parse_mode, chat_id, message_id))
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id=None, message_id=None):
        self.deleted_messages.append((chat_id, message_id))
        if self._delete_raises is not None:
            exc = self._delete_raises(chat_id, message_id)
            if exc is not None:
                raise exc
        return True


def channel_update(
    *,
    text: str | None = None,
    caption: str | None = None,
    text_html: str | None = None,
    caption_html: str | None = None,
    chat_id: int = -2001,
    message_id: int = 4001,
    update_id: int | None = None,
    date: datetime | None = None,
    edit_date: datetime | None = None,
):
    message = FakeMessage(
        message_id=message_id,
        text=text,
        caption=caption,
        text_html=text_html if text_html is not None else text,
        caption_html=caption_html if caption_html is not None else caption,
        sender_chat=SimpleNamespace(id=-3001, type="channel"),
        is_automatic_forward=True,
        date=date,
        edit_date=edit_date,
    )
    update = SimpleNamespace(
        update_id=update_id,
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=None,
    )
    return update, message


def command_update(*, chat_id: int = -2001, message_id: int = 4101, user_id: int = 11):
    message = FakeMessage(message_id=message_id)
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=SimpleNamespace(id=user_id),
    )
    return update, message


def make_context(sample_db, *, config=None, args=(), bot=None, allowlist=(-2001,)):
    config = config or BotConfig(rate_limit_per_minute=10, owner_user_id=11)
    chat_settings = ChatSettings(sample_db.parent / "chat_settings.json")
    for cid in allowlist:
        chat_settings.allow(cid)
    return SimpleNamespace(
        args=list(args),
        bot=bot or FakeBot(),
        application=SimpleNamespace(
            bot_data={
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: SentenceTranslator(
                    sample_db,
                    llm_timeout_seconds=config.llm_timeout_seconds,
                    llm_max_concurrency=config.llm_max_concurrency,
                ),
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=config.rate_limit_per_minute),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=config.rate_limit_per_minute),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(
                    ttl_seconds=config.channel_max_age_seconds
                ),
                CHAT_SETTINGS_KEY: chat_settings,
            }
        ),
    )


def enable_mock_llm(monkeypatch, calls, response_factory):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=30.0,
        transport=None,
    ):
        calls.append((locked_text, locks, html_mode))
        return response_factory(locked_text, locks)

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)


def test_channel_emit_log_redacts_text_ids_and_endpoint(monkeypatch, sample_db, caplog):
    monkeypatch.setenv(REDACTION_SECRET_ENV, "channel-redaction-secret")
    calls = []
    reply_text = "SECRET_CHANNEL_REPLY"
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: reply_text)
    update, message = channel_update(
        text=CN_TEXT,
        chat_id=-99112233,
        message_id=456789,
    )
    context = make_context(sample_db, allowlist=(-99112233,))

    with caplog.at_level(logging.INFO, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert message.replies == [(reply_text, "HTML", 456789)]
    assert calls
    log_text = caplog.text
    assert reply_text not in log_text
    assert "-99112233" not in log_text
    assert "456789" not in log_text
    assert "457789" not in log_text
    assert "-3001" not in log_text
    assert "127.0.0.1:4000" not in log_text
    assert "channel-redaction-secret" not in log_text
    assert redact_id(456789) in log_text
    assert redact_id(457789) in log_text
    assert "mode=HTML" in log_text
    assert "text_len=20" in log_text


def disable_llm(monkeypatch):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)


def forbid_llm_call(monkeypatch):
    calls = []

    async def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("LLM client should not be called")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    return calls


def placeholder_for(locks, official):
    for placeholder, _source, en in locks:
        if en == official:
            return placeholder
    raise AssertionError(f"missing official lock {official}")


def html_with_segments(locked_text: str, *segments: str) -> str:
    placeholders = re.findall(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4}__", locked_text)
    assert len(segments) == len(placeholders) + 1
    parts: list[str] = []
    for segment, placeholder in zip(segments, placeholders, strict=False):
        parts.extend((segment, placeholder))
    parts.append(segments[-1])
    return "".join(parts)


def test_cn_formatted_post_gets_html_reply_with_tags_and_locks(monkeypatch, sample_db):
    calls = []

    def response(locked_text, locks):
        jinhsi = placeholder_for(locks, "Jinhsi")
        echo = placeholder_for(locks, "Echo")
        return html_with_segments(
            locked_text,
            "",
            jinhsi,
            " says ",
            echo,
            " is ",
            "strong",
            "",
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(text=CN_LOCK_TEXT, text_html=CN_LOCK_TEXT_HTML, message_id=4001)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert len(calls) == 1
    locked_text, locks, html_mode = calls[0]
    assert html_mode is True
    assert "今汐" not in locked_text
    assert "声骸" not in locked_text
    assert {lock[2] for lock in locks} >= {"Jinhsi", "Echo"}
    assert message.replies == [
        (
            '<b>Jinhsi</b> says <a href="https://example.com">Echo</a>'
            " is <tg-spoiler>strong</tg-spoiler>",
            "HTML",
            4001,
        )
    ]


def test_channel_autotranslate_uses_default_llm_timeout(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    seen: list[float] = []

    async def fake_call(
        locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=30.0,
        transport=None,
    ):
        seen.append(timeout_seconds)
        return html_with_segments(
            locked_text, "", "translated", "", "", "", "", ""
        )

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = channel_update(text=CN_TEXT, text_html=CN_TEXT_HTML)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert seen == [DEFAULT_LLM_TIMEOUT_SECONDS]
    assert DEFAULT_LLM_TIMEOUT_SECONDS == 45.0
    assert message.replies == [
        (
            '<b>translated</b><a href="https://example.com"></a>'
            "<tg-spoiler></tg-spoiler>",
            "HTML",
            4001,
        )
    ]


def test_non_translatable_post_is_silent_and_consumes_no_throttle(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=1, owner_user_id=11)
    )
    # No Chinese and no Latin letters -> nothing worth translating -> silent,
    # and the gate returns before any throttle slot is consumed.
    noise_update, noise_message = channel_update(
        text="🔥🔥🔥 123 !!!", message_id=4010
    )

    asyncio.run(channel_post_handler(noise_update, context))

    assert noise_message.replies == []
    assert calls == []

    cn_update, cn_message = channel_update(text=CN_TEXT, message_id=4011)
    asyncio.run(channel_post_handler(cn_update, context))

    assert cn_message.replies == [("translated", "HTML", 4011)]
    assert len(calls) == 1


def test_english_post_translates_to_chinese_html(monkeypatch, sample_db):
    calls = []

    def response(locked_text, locks):
        jinhsi = placeholder_for(locks, "Jinhsi")
        echo = placeholder_for(locks, "Echo")
        return html_with_segments(
            locked_text, "", jinhsi, "装备了", echo, ""
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(
        text="Jinhsi equips Echo",
        text_html="<b>Jinhsi</b> equips <tg-spoiler>Echo</tg-spoiler>",
        message_id=4012,
    )
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == [
        ("<b>今汐</b>装备了<tg-spoiler>声骸</tg-spoiler>", "HTML", 4012)
    ]
    assert len(calls) == 1
    assert calls[0][2] is True  # HTML pipeline used


def test_english_exact_term_post_emits_official_chinese(monkeypatch, sample_db):
    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    update, message = channel_update(text="Echo", text_html="Echo", message_id=4013)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    # Dictionary-first, zero LLM: English term -> official Chinese, plain.
    assert message.replies == [("声骸", None, 4013)]
    assert llm_calls == []


def test_blank_llm_html_channel_post_is_silent(monkeypatch, sample_db, caplog):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "   ")
    update, message = channel_update(text=CN_TEXT, text_html=CN_TEXT_HTML, message_id=4018)
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    # Blank output triggers the plain fallback call; when that is blank too,
    # the post stays silent (both attempts are content failures). The failure
    # event names the stage that failed.
    assert message.replies == []
    assert context.bot.edits == []
    assert len(calls) == 2
    assert "mode=plain-fallback" in caplog.text


def test_structurally_changed_llm_html_falls_back_to_plain(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, locks):
        jinhsi = placeholder_for(locks, "Jinhsi")
        echo = placeholder_for(locks, "Echo")
        return f"<div><b>{jinhsi} says {echo} is strong</div>"

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(text=CN_LOCK_TEXT, text_html=CN_LOCK_TEXT_HTML, message_id=4020)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    # HTML structure was broken by the model, so the second (plain) call's
    # output is delivered as plain text instead of dropping the post. The
    # formatting never reaches Telegram as HTML.
    assert len(calls) == 2
    assert message.replies == [
        ("<div><b>Jinhsi says Echo is strong</div>", None, 4020)
    ]


class FloodingFakeMessage(FakeMessage):
    """FakeMessage whose reply_text raises RetryAfter on chosen attempts."""

    def __init__(self, *args, flood_attempts=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.flood_attempts = set(flood_attempts)
        self.attempts = 0

    async def reply_text(self, text: str, **kwargs):
        self.attempts += 1
        if self.attempts in self.flood_attempts:
            raise RetryAfter(0)
        return await super().reply_text(text, **kwargs)


def test_channel_no_html_falls_back_to_plain_pipeline(monkeypatch, sample_db, caplog):
    calls = []
    enable_mock_llm(
        monkeypatch, calls, lambda _t, _l: "This channel text needs translating."
    )
    update, message = channel_update(text=CN_TEXT, text_html="", message_id=4025)
    context = make_context(sample_db)

    with caplog.at_level(logging.INFO, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert "reason=no_html_plain_fallback" in caplog.text
    assert [html_mode for _t, _l, html_mode in calls] == [False]
    assert message.replies == [
        ("This channel text needs translating.", None, 4025)
    ]
    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    # Plain route reserves exactly one call token; the consumed token stays
    # in the rolling budget window.
    assert len(runtime._budget_tokens) == 1


def test_channel_no_html_dictionary_hit_still_exact(monkeypatch, sample_db):
    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    update, message = channel_update(text="声骸", text_html="", message_id=4026)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == [("Echo", None, 4026)]
    assert llm_calls == []


def test_channel_send_retries_once_after_flood_wait(monkeypatch, sample_db, caplog):
    calls = []

    def response(locked_text, _locks):
        return html_with_segments(locked_text, "", "Translated text", "")

    enable_mock_llm(monkeypatch, calls, response)
    message = FloodingFakeMessage(
        message_id=4027,
        text=CN_TEXT,
        text_html="<b>这是一段需要翻译的频道文本</b>",
        sender_chat=SimpleNamespace(id=-3001, type="channel"),
        is_automatic_forward=True,
        flood_attempts={1},
    )
    update = SimpleNamespace(
        update_id=None,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert "flood wait" in caplog.text
    assert message.replies == [("<b>Translated text</b>", "HTML", 4027)]


def test_channel_multichunk_flood_wait_never_duplicates(monkeypatch, sample_db):
    calls = []
    long_visible = "x" * 5000

    def response(locked_text, _locks):
        return html_with_segments(locked_text, "", long_visible, "")

    enable_mock_llm(monkeypatch, calls, response)
    # Flood on the SECOND chunk send: the retry must resend only chunk 2.
    message = FloodingFakeMessage(
        message_id=4028,
        text=CN_TEXT,
        text_html="<b>这是一段需要翻译的频道文本</b>",
        sender_chat=SimpleNamespace(id=-3001, type="channel"),
        is_automatic_forward=True,
        flood_attempts={2},
    )
    update = SimpleNamespace(
        update_id=None,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    # Long HTML output downgrades to plain split chunks; each chunk exactly once.
    assert len(message.replies) == 2
    delivered = [text for text, _mode, _reply_to in message.replies]
    assert "".join(delivered) == long_visible
    assert message.attempts == 3  # chunk1 ok, chunk2 flood, chunk2 retry


def test_budget_of_one_still_translates_short_posts(monkeypatch, sample_db):
    calls = []

    def response(locked_text, _locks):
        return html_with_segments(locked_text, "", "Translated", "")

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(
        text=CN_TEXT, text_html="<b>这是一段需要翻译的频道文本</b>", message_id=4040
    )
    config = BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1)
    context = make_context(sample_db, config=config)

    asyncio.run(channel_post_handler(update, context))

    assert len(calls) == 1
    assert message.replies == [("<b>Translated</b>", "HTML", 4040)]


def test_budget_of_one_content_failure_skips_fallback_silently(
    monkeypatch, sample_db, caplog
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _t, _l: "placeholders all gone")
    update, message = channel_update(
        text=CN_TEXT, text_html="<b>这是一段需要翻译的频道文本</b>", message_id=4041
    )
    config = BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1)
    context = make_context(sample_db, config=config)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    # No fallback token was reserved: exactly one call, silent skip, no
    # RuntimeError escaping to the error handler.
    assert len(calls) == 1
    assert message.replies == []
    assert "mode=html-attempt" in caplog.text


def test_invalid_api_response_does_not_trigger_plain_fallback(
    monkeypatch, sample_db, caplog
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls = []

    async def fake_call(_locked_text, _locks, html_mode=False, **_kwargs):
        calls.append(html_mode)
        raise LLMTranslationError(
            "unavailable", reason="invalid_api_response"
        )

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = channel_update(
        text=CN_TEXT, text_html=CN_TEXT_HTML, message_id=4042
    )
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    # A gateway/schema outage would fail the plain retry identically; do not
    # burn the second call.
    assert calls == [True]
    assert message.replies == []
    assert "reason=invalid_api_response" in caplog.text


def test_flood_retry_aborts_when_authorization_revoked_during_wait(
    monkeypatch, sample_db, caplog
):
    calls = []

    def response(locked_text, _locks):
        return html_with_segments(locked_text, "", "Translated", "")

    enable_mock_llm(monkeypatch, calls, response)
    context = make_context(sample_db)
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    class RevokingFloodMessage(FakeMessage):
        async def reply_text(self, text: str, **kwargs):
            if not self.replies and settings.is_allowed(-2001):
                # First attempt: flood-wait AND the chat gets revoked while
                # the sender sleeps it out.
                settings.disallow(-2001)
                raise RetryAfter(0)
            return await super().reply_text(text, **kwargs)

    message = RevokingFloodMessage(
        message_id=4043,
        text=CN_TEXT,
        text_html="<b>这是一段需要翻译的频道文本</b>",
        sender_chat=SimpleNamespace(id=-3001, type="channel"),
        is_automatic_forward=True,
    )
    update = SimpleNamespace(
        update_id=None,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert message.replies == []
    assert "flood-wait retry aborted: delivery gate closed" in caplog.text


def test_flood_retry_accepts_timedelta_retry_after(monkeypatch):
    # PTB >= 23 reports retry_after as datetime.timedelta (opt in early with
    # PTB_TIMEDELTA=1); the flood retry must treat both shapes identically.
    # A zero delay keeps the test instant.
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    attempts = []

    async def send():
        attempts.append(1)
        if len(attempts) == 1:
            raise RetryAfter(timedelta(seconds=0))
        return "sent"

    assert asyncio.run(send_with_flood_retry(send)) == "sent"
    assert len(attempts) == 2


def test_html_content_failure_falls_back_to_plain_delivery(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, _locks):
        if len(calls) == 1:
            # First (HTML) call loses every structural placeholder.
            return "placeholders all gone"
        return "This channel text needs translating."

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(
        text=CN_TEXT, text_html=CN_TEXT_HTML, message_id=4022
    )
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert [html_mode for _text, _locks, html_mode in calls] == [True, False]
    assert message.replies == [
        ("This channel text needs translating.", None, 4022)
    ]


def test_html_transport_failure_stays_silent_without_fallback(
    monkeypatch, sample_db, caplog
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls = []

    async def fake_call(_locked_text, _locks, html_mode=False, **_kwargs):
        calls.append(html_mode)
        raise LLMTranslationError("unavailable", reason="timeout")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = channel_update(
        text=CN_TEXT, text_html=CN_TEXT_HTML, message_id=4023
    )
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    # Transport-shaped failures would fail again immediately; no plain retry.
    assert calls == [True]
    assert message.replies == []
    assert "mode=html-attempt" in caplog.text


def test_invalid_html_result_is_delivered_as_plain(monkeypatch, sample_db):
    calls = []

    def response(locked_text, _locks):
        return html_with_segments(locked_text, "", "Translated channel text", "")

    enable_mock_llm(monkeypatch, calls, response)
    # Force the post-restore validation to reject the (otherwise valid) HTML
    # to exercise the belt-and-suspenders branch.
    monkeypatch.setattr(
        "wuwaterm.channel.validate_telegram_html", lambda _html: False
    )
    update, message = channel_update(
        text=CN_TEXT,
        text_html="<b>这是一段需要翻译的频道文本</b>",
        message_id=4024,
    )
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert len(calls) == 1
    assert message.replies == [("Translated channel text", None, 4024)]


def test_caption_post_uses_same_html_pipeline(monkeypatch, sample_db):
    calls = []

    def response(locked_text, locks):
        jinhsi = placeholder_for(locks, "Jinhsi")
        return html_with_segments(locked_text, "", jinhsi, " arrives")

    enable_mock_llm(monkeypatch, calls, response)
    update, message = channel_update(
        caption="今汐登场", caption_html="<b>今汐</b>登场", message_id=4030
    )
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert len(calls) == 1
    assert calls[0][2] is True
    assert message.replies == [("<b>Jinhsi</b> arrives", "HTML", 4030)]


def test_channel_autotranslate_bypasses_public_command_throttle(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db,
        config=BotConfig(rate_limit_per_minute=1, owner_user_id=11),
        args=["声骸"],
    )

    messages = []
    for idx in range(3):
        update, message = channel_update(text=CN_TEXT, message_id=4040 + idx)
        asyncio.run(channel_post_handler(update, context))
        messages.append(message)

    assert len(messages[0].replies) == 1
    assert len(messages[1].replies) == 1
    assert len(messages[2].replies) == 1
    assert len(calls) == 3

    tr_update, tr_message = command_update(message_id=4050, user_id=42)
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)
    asyncio.run(term_command(tr_update, context))

    assert tr_message.replies == [("Echo", None, 4050)]


def test_channel_post_skipped_when_chat_not_allowlisted(monkeypatch, sample_db):
    # Finding B: the linked-channel auto-translate path is also gated on the
    # allowlist, so an unauthorized or revoked group (e.g. one where leave_chat
    # failed) cannot keep getting posts translated and burning LLM budget.
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db, allowlist=())  # -2001 NOT allowlisted
    update, message = channel_update(text=CN_TEXT, message_id=4600)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == []  # nothing translated/posted
    assert calls == []  # zero LLM calls


def test_channel_reply_is_skipped_if_chat_revoked_during_llm(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "translated"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        update, message = channel_update(text=CN_TEXT, message_id=4601)
        task = asyncio.create_task(channel_post_handler(update, context))
        await asyncio.wait_for(started.wait(), timeout=0.2)

        settings = context.application.bot_data[CHAT_SETTINGS_KEY]
        settings.disallow(-2001)
        release.set()
        await asyncio.wait_for(task, timeout=0.2)

        assert calls == 1
        assert message.replies == []

    asyncio.run(run())


def test_budget_exhaustion_is_silent_with_one_clean_warning(
    monkeypatch, sample_db, caplog
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=30.0,
        transport=None,
    ):
        raise _llm_error_from_response(
            httpx.Response(429, text='{"error":"max_budget exceeded"}')
        )

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = channel_update(text=CN_TEXT, message_id=4060)
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert message.replies == []
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "wuwaterm.channel"
    ]
    assert len(warnings) == 1
    warning_text = warnings[0].getMessage()
    assert "max_budget" not in warning_text
    assert "429" not in warning_text
    assert CN_TEXT not in warning_text
    assert "127.0.0.1" not in warning_text
    assert "test-key" not in warning_text
    assert "stage=llm" in warning_text


def test_long_channel_failure_keeps_granular_reason(monkeypatch, sample_db, caplog):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(*_args, **_kwargs):
        raise LLMTranslationError("unavailable", reason="rate_limit")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = channel_update(text=CN_TEXT * 160, message_id=4061)
    context = make_context(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    assert message.replies == []
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "wuwaterm.channel"
    ]
    assert len(warnings) == 1
    assert "stage=llm" in warnings[0]
    assert "reason=rate_limit" in warnings[0]
    assert CN_TEXT not in warnings[0]


def test_channel_llm_burst_is_bounded_and_queue_full_is_observable(
    monkeypatch, sample_db
):
    async def run() -> None:
        monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1")
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        release = asyncio.Event()
        started = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal started
            started += 1
            await release.wait()
            return "translated"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        config = BotConfig(
            owner_user_id=11,
            llm_max_concurrency=1,
            channel_max_pending=1,
            channel_llm_calls_per_minute=10,
        )
        context = make_context(sample_db, config=config)
        updates = [channel_update(text=CN_TEXT, message_id=4700 + index) for index in range(3)]
        tasks = [
            asyncio.create_task(channel_post_handler(update, context))
            for update, _message in updates
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
        snapshot = runtime.snapshot()
        assert started == 1
        assert snapshot.active == 1
        assert snapshot.pending == 1
        assert snapshot.outcomes["skipped:queue_full"] == 1

        release.set()
        await asyncio.gather(*tasks)
        assert started == 2
        assert updates[2][1].replies == []
        assert runtime.snapshot().active == 0
        assert runtime.snapshot().pending == 0

    asyncio.run(run())


def test_edit_yields_to_new_posts_near_budget_exhaustion(monkeypatch, sample_db):
    async def run() -> None:
        calls = []
        enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=4),
        )
        now = datetime.now(timezone.utc)

        original, original_message = channel_update(
            text=CN_TEXT, message_id=4760, date=now
        )
        await channel_post_handler(original, context)
        assert original_message.replies == [("translated", "HTML", 4760)]
        # The original consumed 1 started call of the 4/min budget; an edit
        # needing 2 calls would leave less than the new-post headroom.
        edit, edit_message = channel_update(
            text=f"{CN_TEXT}更新",
            message_id=4760,
            update_id=300,
            date=now,
            edit_date=now,
        )
        await channel_post_handler(edit, context)
        runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
        assert runtime.snapshot().outcomes["skipped:edit_yield"] == 1
        assert context.bot.edits == []
        assert edit_message.replies == []

        # A new post may still spend what the edit was not allowed to.
        fresh, fresh_message = channel_update(
            text=CN_TEXT, message_id=4761, date=now
        )
        await channel_post_handler(fresh, context)
        assert fresh_message.replies == [("translated", "HTML", 4761)]

    asyncio.run(run())


def test_capacity_skip_dm_reaches_owner_once_per_cooldown(monkeypatch, sample_db):
    async def run() -> None:
        calls = []
        enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
        bot = FakeBot()
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1),
            bot=bot,
        )
        now = datetime.now(timezone.utc)
        first, first_message = channel_update(
            text=CN_TEXT, message_id=4770, date=now
        )
        await channel_post_handler(first, context)
        assert first_message.replies == [("translated", "HTML", 4770)]

        second, second_message = channel_update(
            text=CN_TEXT, message_id=4771, date=now
        )
        await channel_post_handler(second, context)
        assert second_message.replies == []
        assert len(bot.sent_messages) == 1
        chat_id, text = bot.sent_messages[0]
        assert chat_id == 11
        assert "llm_budget" in text
        # The notice must never carry channel content.
        assert CN_TEXT not in text

        # Cooldown: a further skip within the window is counted, not sent.
        third, third_message = channel_update(
            text=CN_TEXT, message_id=4772, date=now
        )
        await channel_post_handler(third, context)
        assert third_message.replies == []
        assert len(bot.sent_messages) == 1

    asyncio.run(run())


def test_capacity_notice_failure_stays_silent(monkeypatch, sample_db):
    async def run() -> None:
        calls = []
        enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
        bot = FakeBot(send_raises=TelegramError("owner blocked the bot"))
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1),
            bot=bot,
        )
        now = datetime.now(timezone.utc)
        first, _first_message = channel_update(
            text=CN_TEXT, message_id=4780, date=now
        )
        await channel_post_handler(first, context)
        second, second_message = channel_update(
            text=CN_TEXT, message_id=4781, date=now
        )
        # A rejected post whose owner notice fails must not raise or reply.
        await channel_post_handler(second, context)
        assert second_message.replies == []

    asyncio.run(run())


def test_capacity_notice_failure_retries_and_preserves_count(monkeypatch, sample_db):
    async def run() -> None:
        calls = []
        enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
        bot = FakeBot(send_raises=TelegramError("transient network error"))
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1),
            bot=bot,
        )
        # Controllable clock so the short failure-retry window can be crossed.
        now = [1000.0]
        context.application.bot_data[CHANNEL_CAPACITY_NOTIFIER_KEY] = (
            CapacitySkipNotifier(clock=lambda: now[0])
        )
        stamp = datetime.now(timezone.utc)
        first, _ = channel_update(text=CN_TEXT, message_id=4790, date=stamp)
        await channel_post_handler(first, context)

        # First rejection: the DM attempt fails; the failed count is kept.
        second, _ = channel_update(text=CN_TEXT, message_id=4791, date=stamp)
        await channel_post_handler(second, context)
        assert bot.sent_messages == []

        # Inside the failure retry window the next skip is aggregated, not sent.
        third, _ = channel_update(text=CN_TEXT, message_id=4792, date=stamp)
        await channel_post_handler(third, context)
        assert bot.sent_messages == []

        # Past the retry window the alert goes out with all three skips folded in.
        bot._send_raises = None
        now[0] += 61.0
        fourth, _ = channel_update(text=CN_TEXT, message_id=4793, date=stamp)
        await channel_post_handler(fourth, context)
        assert len(bot.sent_messages) == 1
        chat_id, text = bot.sent_messages[0]
        assert chat_id == 11
        assert "3" in text
        assert CN_TEXT not in text

    asyncio.run(run())


def test_channel_multichunk_budget_rejects_before_first_llm_call(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls = []

    async def fake_call(*_args, **_kwargs):
        calls.append(True)
        return "translated"

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    context = make_context(
        sample_db,
        config=BotConfig(
            owner_user_id=11,
            channel_text_limit=4096,
            channel_llm_calls_per_minute=2,
        ),
    )
    update, message = channel_update(text="中" * 4096, message_id=4710)

    asyncio.run(channel_post_handler(update, context))

    assert calls == []
    assert message.replies == []
    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    assert runtime.snapshot().outcomes["skipped:llm_budget"] == 1


def test_channel_rechecks_freshness_after_waiting_for_llm_slot(
    monkeypatch, sample_db
):
    async def run() -> None:
        monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1")
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class FakeDateTime:
            @classmethod
            def now(cls, _tz=None):
                return current

        monkeypatch.setattr("wuwaterm.channel.datetime", FakeDateTime)
        release = asyncio.Event()
        calls = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            return "translated"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(
            sample_db,
            config=BotConfig(
                owner_user_id=11,
                llm_max_concurrency=1,
                channel_max_pending=1,
                channel_max_age_seconds=1,
            ),
        )
        first, _ = channel_update(text=CN_TEXT, message_id=4720, date=current)
        second, second_message = channel_update(
            text=CN_TEXT, message_id=4721, date=current
        )
        first_task = asyncio.create_task(channel_post_handler(first, context))
        await asyncio.sleep(0)
        second_task = asyncio.create_task(channel_post_handler(second, context))
        await asyncio.sleep(0)
        current += timedelta(seconds=2)
        release.set()
        await asyncio.gather(first_task, second_task)

        assert calls == 1
        assert second_message.replies == []
        runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
        assert runtime.snapshot().outcomes["skipped:stale_after_wait"] == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("policy_change", "expected_reason"),
    (
        ("stale", "stale_before_llm"),
        ("revoked", "authorization_changed_before_llm"),
    ),
)
def test_channel_rechecks_policy_after_shared_llm_semaphore_wait(
    monkeypatch, sample_db, policy_change, expected_reason
):
    async def run() -> None:
        monkeypatch.setenv(
            "WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1"
        )
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class FakeDateTime:
            @classmethod
            def now(cls, _tz=None):
                return current

        monkeypatch.setattr("wuwaterm.channel.datetime", FakeDateTime)
        command_entered = asyncio.Event()
        release_command = asyncio.Event()
        calls: list[str] = []

        async def fake_call(locked_text, *_args, **_kwargs):
            calls.append(locked_text)
            if len(calls) == 1:
                command_entered.set()
                await release_command.wait()
            return "translated"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(
            sample_db,
            config=BotConfig(
                owner_user_id=11,
                llm_max_concurrency=1,
                channel_max_age_seconds=1,
            ),
        )
        translator = context.application.bot_data[TRANSLATOR_KEY]
        command_task = asyncio.create_task(
            translator.translate_async("命令翻译占用共享并发槽", propagate_errors=True)
        )
        await asyncio.wait_for(command_entered.wait(), timeout=0.2)

        update, message = channel_update(
            text=CN_TEXT, message_id=4722, date=current
        )
        channel_task = asyncio.create_task(channel_post_handler(update, context))
        await asyncio.sleep(0)
        assert not channel_task.done()

        if policy_change == "stale":
            current += timedelta(seconds=2)
        else:
            context.application.bot_data[CHAT_SETTINGS_KEY].disallow(-2001)
        release_command.set()
        await asyncio.gather(command_task, channel_task)

        assert len(calls) == 1
        assert message.replies == []
        runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
        snapshot = runtime.snapshot()
        assert snapshot.outcomes[f"skipped:{expected_reason}"] == 1
        assert snapshot.outcomes.get("llm:started", 0) == 0
        assert snapshot.active == 0
        assert snapshot.pending == 0

    asyncio.run(run())


def test_channel_rechecks_policy_before_each_multichunk_llm_call(
    monkeypatch, sample_db
):
    monkeypatch.setenv(
        "WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1"
    )
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class FakeDateTime:
        @classmethod
        def now(cls, _tz=None):
            return current

    monkeypatch.setattr("wuwaterm.channel.datetime", FakeDateTime)
    calls: list[str] = []

    async def fake_call(locked_text, *_args, **_kwargs):
        nonlocal current
        calls.append(locked_text)
        current += timedelta(seconds=2)
        return "translated"

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    context = make_context(
        sample_db,
        config=BotConfig(
            owner_user_id=11,
            channel_max_age_seconds=1,
            channel_llm_calls_per_minute=2,
        ),
    )
    update, message = channel_update(
        text="测" * (LLM_INPUT_CHAR_LIMIT + 10),
        message_id=4723,
        date=current,
    )

    asyncio.run(channel_post_handler(update, context))

    assert len(calls) == 1
    assert message.replies == []
    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    snapshot = runtime.snapshot()
    assert snapshot.outcomes["skipped:stale_before_llm"] == 1
    assert snapshot.outcomes["llm:started"] == 1
    replacement, rejection = runtime.reserve(1)
    assert replacement is not None
    assert rejection is None

    async def release_replacement() -> None:
        async with replacement:
            pass

    asyncio.run(release_replacement())


def test_channel_rechecks_freshness_after_slow_llm_before_delivery(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class FakeDateTime:
        @classmethod
        def now(cls, _tz=None):
            return current

    monkeypatch.setattr("wuwaterm.channel.datetime", FakeDateTime)
    calls = 0

    async def slow_call(*_args, **_kwargs):
        nonlocal calls, current
        calls += 1
        current += timedelta(seconds=2)
        return "translated"

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", slow_call)
    context = make_context(
        sample_db,
        config=BotConfig(owner_user_id=11, channel_max_age_seconds=1),
    )
    update, message = channel_update(text=CN_TEXT, message_id=4725, date=current)

    asyncio.run(channel_post_handler(update, context))

    assert calls == 1
    assert message.replies == []
    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    assert runtime.snapshot().outcomes["skipped:stale_before_delivery"] == 1


def test_queued_edit_rechecks_freshness_after_delivery_lock(
    monkeypatch, sample_db
):
    async def run() -> None:
        monkeypatch.setenv(
            "WUWATERM_OPENAI_BASE_URL", "https://gateway.example/v1"
        )
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)

        class FakeDateTime:
            @classmethod
            def now(cls, _tz=None):
                return current

        monkeypatch.setattr("wuwaterm.channel.datetime", FakeDateTime)
        llm_completed = asyncio.Event()

        async def fake_call(*_args, **_kwargs):
            llm_completed.set()
            return "translated"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_max_age_seconds=1),
        )
        original, original_message = channel_update(
            text=CN_TEXT, message_id=4726, date=current
        )
        await channel_post_handler(original, context)
        assert original_message.replies == [("translated", "HTML", 4726)]

        reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
        lock = reply_index.edit_delivery_lock(-2001, 4726)
        await lock.acquire()
        llm_completed.clear()
        edit, edit_message = channel_update(
            text=f"{CN_TEXT}更新",
            message_id=4726,
            update_id=200,
            date=current,
            edit_date=current,
        )
        task = asyncio.create_task(channel_post_handler(edit, context))
        await asyncio.wait_for(llm_completed.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not task.done()

        current += timedelta(seconds=2)
        lock.release()
        await asyncio.wait_for(task, timeout=0.2)

        assert edit_message.replies == []
        assert context.bot.edits == []
        runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
        assert runtime.snapshot().outcomes["skipped:stale_before_delivery"] == 1

    asyncio.run(run())


def test_channel_delivery_failure_is_safe_and_observable(monkeypatch, sample_db, caplog):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)
    update, message = channel_update(text=CN_TEXT, message_id=4730)

    async def fail_delivery(_text: str, **_kwargs):
        raise TelegramError("sensitive upstream detail")

    message.reply_text = fail_delivery
    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(channel_post_handler(update, context))

    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    outcomes = runtime.snapshot().outcomes
    assert outcomes["delivery:TelegramError"] == 1
    assert "sensitive upstream detail" not in caplog.text
    assert CN_TEXT not in caplog.text


def test_kill_switch_disables_listener_without_llm(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = channel_update(text=CN_TEXT, message_id=4070)
    context = make_context(
        sample_db,
        config=BotConfig(
            rate_limit_per_minute=10, owner_user_id=11, channel_autotranslate=False
        ),
    )

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == []
    assert calls == []


def test_min_cjk_threshold_gates_short_posts(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db,
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=11, channel_min_cjk=2),
    )

    one_update, one_message = channel_update(text="强! New patch", message_id=4080)
    asyncio.run(channel_post_handler(one_update, context))

    assert one_message.replies == []
    assert calls == []

    two_update, two_message = channel_update(text="很强! New patch", message_id=4081)
    asyncio.run(channel_post_handler(two_update, context))

    assert two_message.replies == [("translated", "HTML", 4081)]
    assert len(calls) == 1


def test_exact_dictionary_hit_replies_official_plain_with_zero_llm(
    monkeypatch, sample_db
):
    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    update, message = channel_update(text="声骸", message_id=4090)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == [("Echo", None, 4090)]
    assert llm_calls == []


def test_non_exact_channel_post_skips_without_llm_config(monkeypatch, sample_db):
    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    update, message = channel_update(text=CN_TEXT, message_id=4095)
    context = make_context(
        sample_db,
        config=BotConfig(rate_limit_per_minute=1, owner_user_id=11),
        args=["声骸"],
    )

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == []
    assert context.bot.edits == []
    assert llm_calls == []

    tr_update, tr_message = command_update(message_id=4096)
    asyncio.run(term_command(tr_update, context))

    assert tr_message.replies == [("Echo", None, 4096)]


def test_channel_posts_ignore_public_command_throttle(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=1, owner_user_id=11)
    )

    first_update, first_message = channel_update(text=CN_TEXT, message_id=4097)
    asyncio.run(channel_post_handler(first_update, context))
    assert first_message.replies == [("translated", "HTML", 4097)]
    assert len(calls) == 1

    second_update, second_message = channel_update(text=f"{CN_TEXT}。", message_id=4098)
    asyncio.run(channel_post_handler(second_update, context))

    assert second_message.replies == [("translated", "HTML", 4098)]
    assert len(calls) == 2


def test_stale_post_is_silent_with_zero_llm_and_zero_throttle(monkeypatch, sample_db):
    """Replayed history (restart backlog, admin-promotion replay) must never
    be translated: a post older than channel_max_age_seconds is skipped
    before the throttle, so a follow-up fresh post still translates."""
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=1, owner_user_id=11)
    )
    stale_update, stale_message = channel_update(
        text=CN_TEXT,
        message_id=4100,
        date=datetime.now(timezone.utc) - timedelta(days=2),
    )

    asyncio.run(channel_post_handler(stale_update, context))

    assert stale_message.replies == []
    assert calls == []

    fresh_update, fresh_message = channel_update(
        text=CN_TEXT, message_id=4101, date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(fresh_update, context))

    assert fresh_message.replies == [("translated", "HTML", 4101)]
    assert len(calls) == 1


def test_default_channel_age_allows_delayed_channel_posts(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=10, owner_user_id=11)
    )

    delayed_update, delayed_message = channel_update(
        text=CN_TEXT,
        message_id=4102,
        date=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    asyncio.run(channel_post_handler(delayed_update, context))

    assert delayed_message.replies == [("translated", "HTML", 4102)]
    assert len(calls) == 1


def test_long_channel_input_is_split_instead_of_rejected(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(
        monkeypatch,
        calls,
        lambda _locked_text, _locks: f"chunk-{len(calls)}",
    )
    long_text = "测" * (LLM_INPUT_CHAR_LIMIT + 500)
    context = make_context(sample_db)
    update, message = channel_update(text=long_text, message_id=4103)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == [("chunk-1\nchunk-2", None, 4103)]
    assert len(calls) == 2
    assert all(len(locked_text) <= LLM_INPUT_CHAR_LIMIT for locked_text, *_ in calls)
    assert [html_mode for *_rest, html_mode in calls] == [False, False]


def test_post_age_boundary_respects_configured_max_age(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db,
        config=BotConfig(
            rate_limit_per_minute=10, owner_user_id=11, channel_max_age_seconds=60
        ),
    )

    old_update, old_message = channel_update(
        text=CN_TEXT,
        message_id=4110,
        date=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    asyncio.run(channel_post_handler(old_update, context))

    assert old_message.replies == []
    assert calls == []

    recent_update, recent_message = channel_update(
        text=CN_TEXT,
        message_id=4111,
        date=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    asyncio.run(channel_post_handler(recent_update, context))

    assert recent_message.replies == [("translated", "HTML", 4111)]
    assert len(calls) == 1


def test_missing_date_is_treated_as_fresh(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    update, message = channel_update(text=CN_TEXT, message_id=4120, date=None)
    context = make_context(sample_db)

    asyncio.run(channel_post_handler(update, context))

    assert message.replies == [("translated", "HTML", 4120)]
    assert len(calls) == 1


def test_from_env_parses_channel_max_age_seconds(monkeypatch):
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv("WUWATERM_CHANNEL_MAX_AGE_SECONDS", "60")

    config = BotConfig.from_env()

    assert config.channel_max_age_seconds == 60


def test_count_cjk_counts_ideographs_only():
    assert count_cjk("今汐说声骸很强") == 7
    assert count_cjk("Patch notes are live now!") == 0
    assert count_cjk("强! New patch") == 1


@pytest.mark.parametrize(
    "snippet",
    [
        "<b>x</b>",
        "<strong>x</strong>",
        "<i>x</i>",
        "<em>x</em>",
        "<u>x</u>",
        "<ins>x</ins>",
        "<s>x</s>",
        "<strike>x</strike>",
        "<del>x</del>",
        '<a href="https://example.com">x</a>',
        "<code>x</code>",
        '<code class="language-python">x</code>',
        "<pre>x</pre>",
        "<blockquote>x</blockquote>",
        "<blockquote expandable>x</blockquote>",
        '<span class="tg-spoiler">x</span>',
        "<tg-spoiler>x</tg-spoiler>",
        '<tg-emoji emoji-id="5368324170671202286">x</tg-emoji>',
        "plain text with &amp; entity",
        "<b>nested <i>tags</i></b>",
    ],
)
def test_validator_accepts_telegram_subset(snippet):
    assert validate_telegram_html(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "<div>x</div>",  # disallowed tag
        '<b class="x">y</b>',  # attribute not allowed on b
        "<a>x</a>",  # missing required href
        '<a href="">x</a>',  # empty href
        '<a href="https://example.com" onclick="x">y</a>',  # extra attribute
        '<span class="other">x</span>',  # wrong span class
        "<tg-emoji>x</tg-emoji>",  # missing emoji-id
        '<code class="python">x</code>',  # class without language- prefix
        "<b><i>x</b></i>",  # mismatched nesting
        "<b>x",  # unclosed tag
        "x</b>",  # stray close tag
        "<b/>",  # self-closing (no void tags in the subset)
        "<!-- c -->",  # comment
    ],
)
def test_validator_rejects_invalid_html(snippet):
    assert not validate_telegram_html(snippet)


def test_strip_telegram_html_unescapes_and_never_raises():
    assert strip_telegram_html("<b>Jinhsi</b> &amp; <i>Echo</i>") == "Jinhsi & Echo"
    assert strip_telegram_html("&lt;b&gt;") == "<b>"
    for nasty in ("<b>x", "<<<>>>", "<a href='x", "", "<![CDATA[x]]>", "&#x4e00;"):
        assert isinstance(strip_telegram_html(nasty), str)


# --- channel-post edit dedup (Option B: edit-in-place, update-only) ---
#
# FakeMessage.reply_text returns message_id + 1000, so the bot's reply to a
# post with id N is tracked as reply id N + 1000.


def test_edit_updates_existing_reply_in_place(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)

    # New post -> normal in-thread HTML reply, remembered.
    new_update, new_message = channel_update(text=CN_TEXT, message_id=4200)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("translated", "HTML", 4200)]

    # Same post edited (same message_id, edit_date set) arrives as its own
    # update -> the tracked reply (5200) is edited in place; no new reply.
    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4200, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == [("translated", "HTML", -2001, 5200)]
    assert len(calls) == 2  # the edit re-translated


def test_concurrent_duplicate_original_has_one_producer(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "translated once"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        first_update, first_message = channel_update(text=CN_TEXT, message_id=4190)
        second_update, second_message = channel_update(text=CN_TEXT, message_id=4190)

        producer = asyncio.create_task(channel_post_handler(first_update, context))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter = asyncio.create_task(channel_post_handler(second_update, context))
        await asyncio.sleep(0)
        assert not waiter.done()

        release.set()
        await asyncio.wait_for(producer, timeout=1)
        await asyncio.wait_for(waiter, timeout=1)

        assert calls == 1
        assert first_message.replies == [("translated once", "HTML", 4190)]
        assert second_message.replies == []

    asyncio.run(run())


def test_sequential_duplicate_original_is_done_without_translation(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)

    first_update, first_message = channel_update(text=CN_TEXT, message_id=4191)
    second_update, second_message = channel_update(text=CN_TEXT, message_id=4191)
    asyncio.run(channel_post_handler(first_update, context))
    asyncio.run(channel_post_handler(second_update, context))

    assert len(calls) == 1
    assert first_message.replies == [("translated", "HTML", 4191)]
    assert second_message.replies == []


def test_duplicate_waiter_released_when_producer_translation_fails(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            raise LLMTranslationError("unavailable")

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        first_update, first_message = channel_update(text=CN_TEXT, message_id=4192)
        second_update, second_message = channel_update(text=CN_TEXT, message_id=4192)

        producer = asyncio.create_task(channel_post_handler(first_update, context))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter = asyncio.create_task(channel_post_handler(second_update, context))
        await asyncio.sleep(0)
        release.set()

        await asyncio.wait_for(producer, timeout=1)
        await asyncio.wait_for(waiter, timeout=1)
        reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]

        assert calls == 1
        assert first_message.replies == []
        assert second_message.replies == []
        assert reply_index.was_observed_without_reply(-2001, 4192) is True

    asyncio.run(run())


def test_cancelled_producer_releases_waiter_and_fresh_edit_can_resume(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        never_release = asyncio.Event()
        calls = 0

        async def fake_call(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await never_release.wait()
            return "resumed translation"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        first_update, first_message = channel_update(text=CN_TEXT, message_id=4193)
        second_update, second_message = channel_update(text=CN_TEXT, message_id=4193)

        producer = asyncio.create_task(channel_post_handler(first_update, context))
        await asyncio.wait_for(started.wait(), timeout=1)
        waiter = asyncio.create_task(channel_post_handler(second_update, context))
        await asyncio.sleep(0)

        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        await asyncio.wait_for(waiter, timeout=1)

        reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
        assert first_message.replies == []
        assert second_message.replies == []
        assert reply_index.was_observed_without_reply(-2001, 4193) is True

        now = datetime.now(timezone.utc)
        edit_update, edit_message = channel_update(
            text=CN_TEXT,
            message_id=4193,
            date=now,
            edit_date=now,
        )
        await asyncio.wait_for(channel_post_handler(edit_update, context), timeout=1)

        assert calls == 2
        assert edit_message.replies == [("resumed translation", "HTML", 4193)]

    asyncio.run(run())


def test_edit_waits_for_original_inflight_reply(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
                return "original translation"
            return "edited translation"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        new_update, new_message = channel_update(text=CN_TEXT, message_id=4205)
        new_task = asyncio.create_task(channel_post_handler(new_update, context))
        edit_update, edit_message = channel_update(
            text=CN_TEXT, message_id=4205, edit_date=datetime.now(timezone.utc)
        )
        edit_task = asyncio.create_task(channel_post_handler(edit_update, context))
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not edit_task.done()
        assert edit_message.replies == []

        release_first.set()
        await asyncio.wait_for(new_task, timeout=0.2)
        await asyncio.wait_for(edit_task, timeout=0.2)

        assert new_message.replies == [("original translation", "HTML", 4205)]
        assert edit_message.replies == []
        assert context.bot.edits == [("edited translation", "HTML", -2001, 5205)]
        assert calls == 2

    asyncio.run(run())


def test_concurrent_edits_do_not_overwrite_newer_translation(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        first_edit_started = asyncio.Event()
        release_first_edit = asyncio.Event()
        calls = 0

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            nonlocal calls
            calls += 1
            if calls == 1:
                return "original translation"
            if calls == 2:
                first_edit_started.set()
                await release_first_edit.wait()
                return "older edit translation"
            return "newer edit translation"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        new_update, new_message = channel_update(
            text=CN_TEXT, message_id=4215, update_id=100
        )
        await channel_post_handler(new_update, context)
        assert new_message.replies == [("original translation", "HTML", 4215)]

        older_update, _ = channel_update(
            text=f"{CN_TEXT}旧",
            message_id=4215,
            update_id=101,
            edit_date=datetime.now(timezone.utc),
        )
        newer_update, _ = channel_update(
            text=f"{CN_TEXT}新",
            message_id=4215,
            update_id=102,
            edit_date=datetime.now(timezone.utc),
        )
        older_task = asyncio.create_task(channel_post_handler(older_update, context))
        await asyncio.wait_for(first_edit_started.wait(), timeout=0.2)

        newer_task = asyncio.create_task(channel_post_handler(newer_update, context))
        await asyncio.wait_for(newer_task, timeout=0.2)
        assert context.bot.edits == [
            ("newer edit translation", "HTML", -2001, 5215)
        ]

        release_first_edit.set()
        await asyncio.wait_for(older_task, timeout=0.2)

        assert context.bot.edits == [
            ("newer edit translation", "HTML", -2001, 5215)
        ]
        assert calls == 3

    asyncio.run(run())


def test_yielded_edit_does_not_supersede_admitted_edit(monkeypatch, sample_db):
    async def run() -> None:
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        calls = 0

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            nonlocal calls
            calls += 1
            if calls == 2:
                edit_started.set()
                await release_edit.wait()
                return "admitted edit translation"
            return "original translation"

        monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(
            sample_db,
            config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=6),
        )
        now = datetime.now(timezone.utc)
        original, original_message = channel_update(
            text=CN_TEXT, message_id=4225, date=now
        )
        await channel_post_handler(original, context)
        assert original_message.replies == [("original translation", "HTML", 4225)]

        # Edit A is admitted (remaining 4 >= required 2 + headroom 2) and
        # blocks inside its LLM call; it reserves 2, leaving 2.
        edit_a, _ = channel_update(
            text=CN_TEXT + "（第一次编辑）",
            message_id=4225,
            update_id=201,
            date=now,
            edit_date=now,
        )
        task_a = asyncio.create_task(channel_post_handler(edit_a, context))
        await asyncio.wait_for(edit_started.wait(), timeout=1)

        # Edit B arrives while A is in flight: remaining 2 < required 2 +
        # headroom 2, so B yields - and must not supersede A's edit token.
        edit_b, _ = channel_update(
            text=CN_TEXT + "（第二次编辑）",
            message_id=4225,
            update_id=202,
            date=now,
            edit_date=now,
        )
        await channel_post_handler(edit_b, context)
        assert calls == 2
        assert context.bot.edits == []

        release_edit.set()
        await asyncio.wait_for(task_a, timeout=1)
        # A's completed translation is delivered; with the old early
        # begin_edit, B's token superseded A's and A was dropped as stale.
        assert context.bot.edits == [
            ("admitted edit translation", "HTML", -2001, 5225)
        ]
        assert calls == 2

    asyncio.run(run())


def test_queue_rejected_edit_does_not_supersede_admitted_edit(monkeypatch, sample_db):
    async def run() -> None:
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        calls = 0

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            nonlocal calls
            calls += 1
            if calls == 2:
                edit_started.set()
                await release_edit.wait()
                return "admitted edit translation"
            return "original translation"

        monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        # One active slot, no pending: edit B passes the budget-yield check
        # (plenty of budget) but loses reserve() to queue_full.
        context = make_context(
            sample_db,
            config=BotConfig(
                owner_user_id=11,
                llm_max_concurrency=1,
                channel_max_pending=0,
                channel_llm_calls_per_minute=10,
            ),
        )
        now = datetime.now(timezone.utc)
        original, original_message = channel_update(
            text=CN_TEXT, message_id=4235, date=now
        )
        await channel_post_handler(original, context)
        assert original_message.replies == [("original translation", "HTML", 4235)]

        edit_a, _ = channel_update(
            text=CN_TEXT + "（第一次编辑）",
            message_id=4235,
            update_id=301,
            date=now,
            edit_date=now,
        )
        task_a = asyncio.create_task(channel_post_handler(edit_a, context))
        await asyncio.wait_for(edit_started.wait(), timeout=1)

        edit_b, _ = channel_update(
            text=CN_TEXT + "（第二次编辑）",
            message_id=4235,
            update_id=302,
            date=now,
            edit_date=now,
        )
        await channel_post_handler(edit_b, context)
        assert calls == 2

        release_edit.set()
        await asyncio.wait_for(task_a, timeout=1)
        # queue_full rejection must not have superseded A's edit token.
        assert context.bot.edits == [
            ("admitted edit translation", "HTML", -2001, 5235)
        ]
        assert calls == 2

    asyncio.run(run())


def test_capacity_notifier_keeps_skips_arriving_during_send():
    now = [100.0]
    notifier = CapacitySkipNotifier(clock=lambda: now[0])
    should, counts = notifier.note_skip("llm_budget")
    assert (should, counts) == (True, {"llm_budget": 1})
    # A skip lands while the first DM is still in flight.
    should, counts = notifier.note_skip("llm_budget")
    assert should is False and counts == {"llm_budget": 2}
    notifier.mark_result(True, counted={"llm_budget": 1})
    # Only the count captured by the sent DM is cleared; the concurrent skip
    # is carried into the next window's notice.
    now[0] += 601.0
    should, counts = notifier.note_skip("llm_budget")
    assert (should, counts) == (True, {"llm_budget": 2})


def test_capacity_notifier_reports_mixed_reasons_separately():
    now = [100.0]
    notifier = CapacitySkipNotifier(clock=lambda: now[0])
    should, counts = notifier.note_skip("queue_full")
    assert (should, counts) == (True, {"queue_full": 1})
    notifier.mark_result(True, counted=counts)
    # queue_full and llm_budget skips accumulate together under cooldown...
    notifier.note_skip("queue_full")
    notifier.note_skip("llm_budget")
    now[0] += 601.0
    should, counts = notifier.note_skip("llm_budget")
    # ...and the next notice attributes each count to its own reason.
    assert should is True
    assert counts == {"queue_full": 1, "llm_budget": 2}


def test_edit_with_no_tracked_reply_and_no_date_is_silent(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)

    # An edit with nothing remembered and no message date cannot be bounded by
    # the stale-post gate, so it remains a safe silent skip.
    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4210, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == []
    assert calls == []


def test_fresh_edit_without_observed_post_is_silent(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)
    now = datetime.now(timezone.utc)

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4210, date=now, edit_date=now
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == []
    assert calls == []


def test_fresh_edit_for_observed_untranslated_post_translates_once(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(sample_db)
    now = datetime.now(timezone.utc)

    original_update, original_message = channel_update(message_id=4210, date=now)
    asyncio.run(channel_post_handler(original_update, context))
    assert original_message.replies == []

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4210, date=now, edit_date=now
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == [("translated", "HTML", 4210)]
    assert context.bot.edits == []
    assert len(calls) == 1
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get(-2001, 4210) == 5211


def test_media_caption_added_by_fresh_edit_gets_first_reply(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(
        monkeypatch, calls, lambda _locked_text, _locks: "New loading screen"
    )
    context = make_context(sample_db)
    now = datetime.now(timezone.utc)

    original_update, original_message = channel_update(message_id=4211, date=now)
    asyncio.run(channel_post_handler(original_update, context))
    assert original_message.replies == []
    assert calls == []

    edit_update, edit_message = channel_update(
        caption="新加载图",
        caption_html="新加载图",
        message_id=4211,
        date=now,
        edit_date=now,
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == [("New loading screen", "HTML", 4211)]
    assert context.bot.edits == []
    assert len(calls) == 1


def test_channel_edit_bypasses_public_command_throttle(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=1, owner_user_id=11)
    )

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4220)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("translated", "HTML", 4220)]

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4220, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))
    assert context.bot.edits == [("translated", "HTML", -2001, 5220)]
    assert len(calls) == 2

    third_update, third_message = channel_update(text=CN_TEXT, message_id=4221)
    asyncio.run(channel_post_handler(third_update, context))
    assert third_message.replies == [("translated", "HTML", 4221)]
    assert len(calls) == 3


def test_edit_dictionary_hit_updates_reply_in_place(monkeypatch, sample_db):
    # An exact-dictionary post replies with the official term and is remembered;
    # editing it updates that same reply in place, without requiring LLM config.
    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    context = make_context(sample_db)

    new_update, new_message = channel_update(text="声骸", message_id=4270)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("Echo", None, 4270)]

    edit_update, edit_message = channel_update(
        text="声骸", message_id=4270, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == [("Echo", None, -2001, 5270)]
    assert llm_calls == []


def test_edit_from_non_exact_to_exact_updates_dictionary_reply_without_llm_config(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "old translation")
    context = make_context(sample_db)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4275)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("old translation", "HTML", 4275)]
    assert len(calls) == 1

    disable_llm(monkeypatch)
    llm_calls = forbid_llm_call(monkeypatch)
    edit_update, edit_message = channel_update(
        text="声骸", message_id=4275, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == [("Echo", None, -2001, 5275)]
    assert llm_calls == []


def test_long_channel_html_reply_is_split_to_plain_chunks(monkeypatch, sample_db):
    calls = []
    long_visible_text = "A" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 17)
    enable_mock_llm(
        monkeypatch,
        calls,
        lambda _locked_text, _locks: long_visible_text,
    )
    context = make_context(sample_db)
    update, message = channel_update(text=CN_TEXT, message_id=4280)

    asyncio.run(channel_post_handler(update, context))

    assert len(message.replies) == 2
    assert all(len(text) <= TELEGRAM_TEXT_MESSAGE_LIMIT for text, _, _ in message.replies)
    assert [parse_mode for _text, parse_mode, _reply_to in message.replies] == [
        None,
        None,
    ]
    assert [reply_to for _text, _parse_mode, reply_to in message.replies] == [
        4280,
        4280,
    ]
    assert "".join(text for text, _parse_mode, _reply_to in message.replies) == (
        long_visible_text
    )
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4280) == (5280, 5281)
    assert len(calls) == 1


def test_long_channel_emoji_reply_is_split_by_telegram_utf16_units(
    monkeypatch, sample_db
):
    calls = []
    long_visible_text = "😀" * (TELEGRAM_TEXT_MESSAGE_LIMIT // 2 + 1)
    enable_mock_llm(
        monkeypatch,
        calls,
        lambda _locked_text, _locks: long_visible_text,
    )
    context = make_context(sample_db)
    update, message = channel_update(text=CN_TEXT, message_id=4285)

    asyncio.run(channel_post_handler(update, context))

    assert len(message.replies) == 2
    assert all(
        telegram_text_units(text) <= TELEGRAM_TEXT_MESSAGE_LIMIT
        for text, _parse_mode, _reply_to in message.replies
    )
    assert [parse_mode for _text, parse_mode, _reply_to in message.replies] == [
        None,
        None,
    ]
    assert "".join(text for text, _parse_mode, _reply_to in message.replies) == (
        long_visible_text
    )
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4285) == (5285, 5286)
    assert len(calls) == 1


def test_long_channel_reply_remembers_sent_chunks_after_mid_send_failure(
    monkeypatch, sample_db
):
    calls = []
    long_visible_text = "E" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 12)
    enable_mock_llm(
        monkeypatch,
        calls,
        lambda _locked_text, _locks: long_visible_text,
    )
    context = make_context(sample_db)
    update, message = channel_update(text=CN_TEXT, message_id=4288)

    original_reply_text = message.reply_text
    send_attempts = []

    async def flaky_reply_text(text: str, **kwargs):
        send_attempts.append(text)
        if len(send_attempts) == 2:
            raise BadRequest("temporary send failed")
        return await original_reply_text(text, **kwargs)

    message.reply_text = flaky_reply_text

    asyncio.run(channel_post_handler(update, context))

    assert len(message.replies) == 1
    assert send_attempts == ["E" * TELEGRAM_TEXT_MESSAGE_LIMIT, "E" * 12]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4288) == (5288,)
    assert len(calls) == 1


def test_long_channel_edit_adds_tracked_continuation_chunks(monkeypatch, sample_db):
    calls = []
    responses = iter(["short translation", "B" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 9)])
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    context = make_context(sample_db)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4290)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("short translation", "HTML", 4290)]

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4290, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert context.bot.edits == [
        ("B" * TELEGRAM_TEXT_MESSAGE_LIMIT, None, -2001, 5290)
    ]
    assert edit_message.replies == [("B" * 9, None, 4290)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4290) == (5290, 5291)
    assert len(calls) == 2


def test_long_channel_edit_remembers_continuations_after_mid_send_failure(
    monkeypatch, sample_db
):
    calls = []
    long_visible_text = "F" * (TELEGRAM_TEXT_MESSAGE_LIMIT * 2 + 7)
    responses = iter(["short translation", long_visible_text])
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    context = make_context(sample_db)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4291)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("short translation", "HTML", 4291)]

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4291, edit_date=datetime.now(timezone.utc)
    )
    original_reply_text = edit_message.reply_text

    async def flaky_reply_text(text: str, **kwargs):
        if len(edit_message.replies) == 1:
            raise BadRequest("temporary continuation send failed")
        return await original_reply_text(text, **kwargs)

    edit_message.reply_text = flaky_reply_text

    asyncio.run(channel_post_handler(edit_update, context))

    assert context.bot.edits == [
        ("F" * TELEGRAM_TEXT_MESSAGE_LIMIT, None, -2001, 5291)
    ]
    assert edit_message.replies == [("F" * TELEGRAM_TEXT_MESSAGE_LIMIT, None, 4291)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4291) == (5291, 5292)
    assert len(calls) == 2


def test_channel_edit_reloads_tracked_chunks_after_waiting_for_delivery_lock(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        responses = iter(
            [
                "original translation",
                "A" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 5),
                "newer short translation",
            ]
        )

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            return next(responses)

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)

        new_update, new_message = channel_update(
            text=CN_TEXT, message_id=4292, update_id=100
        )
        await channel_post_handler(new_update, context)
        assert new_message.replies == [("original translation", "HTML", 4292)]

        original_edit_message_text = context.bot.edit_message_text
        first_edit_started = asyncio.Event()
        release_first_edit = asyncio.Event()
        edit_calls = 0

        async def blocking_edit_message_text(*args, **kwargs):
            nonlocal edit_calls
            edit_calls += 1
            if edit_calls == 1:
                first_edit_started.set()
                await release_first_edit.wait()
            return await original_edit_message_text(*args, **kwargs)

        context.bot.edit_message_text = blocking_edit_message_text

        older_update, older_message = channel_update(
            text=f"{CN_TEXT}旧",
            message_id=4292,
            update_id=101,
            edit_date=datetime.now(timezone.utc),
        )
        newer_update, _ = channel_update(
            text=f"{CN_TEXT}新",
            message_id=4292,
            update_id=102,
            edit_date=datetime.now(timezone.utc),
        )
        older_task = asyncio.create_task(channel_post_handler(older_update, context))
        await asyncio.wait_for(first_edit_started.wait(), timeout=0.2)

        newer_task = asyncio.create_task(channel_post_handler(newer_update, context))
        await asyncio.sleep(0.05)

        release_first_edit.set()
        await asyncio.wait_for(older_task, timeout=0.2)
        await asyncio.wait_for(newer_task, timeout=0.2)

        assert older_message.replies == [("A" * 5, None, 4292)]
        assert context.bot.deleted_messages == [(-2001, 5293)]
        reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
        assert reply_index.get_many(-2001, 4292) == (5292,)

    asyncio.run(run())


def test_channel_edit_deletes_stale_extra_chunks_when_translation_shrinks(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(["C" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 11), "shorter translation"])
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    context = make_context(sample_db)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4295)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 2

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4295, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert context.bot.edits == [("shorter translation", "HTML", -2001, 5295)]
    assert context.bot.deleted_messages == [(-2001, 5296)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4295) == (5295,)
    assert len(calls) == 2


def test_long_channel_edit_reply_gone_does_not_send_continuation_chunks(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(["short translation", "D" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 13)])
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    bot = FakeBot(edit_raises=lambda *_: BadRequest("Message to edit not found"))
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4298)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("short translation", "HTML", 4298)]

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4298, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert bot.edits == []
    assert len(bot.edit_attempts) == 1
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4298) == ()
    outcomes = context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes
    assert outcomes["delivery:edit_target_unavailable"] == 1
    assert outcomes["delivery:success"] == 1  # original delivery only
    assert len(calls) == 2


def test_channel_edit_reply_gone_prunes_tracked_continuation_chunks(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(["H" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 6), "shorter translation"])
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    bot = FakeBot(edit_raises=lambda *_: BadRequest("Message to edit not found"))
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4299)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 2

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4299, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert bot.edits == []
    assert bot.deleted_messages == [(-2001, 5300)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4299) == ()
    assert len(calls) == 2


def test_channel_edit_uneditable_reply_is_deleted_not_orphaned(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
    bot = FakeBot(edit_raises=lambda *_: BadRequest("Message can't be edited"))
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4300)
    asyncio.run(channel_post_handler(new_update, context))
    assert new_message.replies == [("translated", "HTML", 4300)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    tracked = reply_index.get_many(-2001, 4300)
    assert len(tracked) == 1

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4300, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    # The uneditable reply is deleted rather than left as an untracked orphan,
    # and the index forgets the post (a fresh edit starts clean).
    assert context.bot.deleted_messages == [(-2001, tracked[0])]
    assert reply_index.get_many(-2001, 4300) == ()
    assert edit_message.replies == []
    outcomes = context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes
    assert outcomes["delivery:edit_target_unavailable"] == 1


def test_channel_edit_uneditable_later_chunk_is_deleted_not_orphaned(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(
        [
            "I" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 4),  # original: 2 chunks
            "J" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 6),  # edit: 2 chunks
        ]
    )
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    # Chunk 1 edits fine; the second tracked reply rejects edits.
    bot = FakeBot(
        edit_raises=lambda _text, _parse_mode, _chat_id, message_id: (
            BadRequest("Message can't be edited") if message_id == 5297 else None
        )
    )
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4296)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 2
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4296) == (5296, 5297)

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4296, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    # Chunk 1 applied in place; the uneditable chunk-2 reply is deleted
    # rather than dropped from the rebuilt index while staying visible, and
    # its replacement chunk goes out as a new reply (the fake reuses the id).
    assert [edit[3] for edit in bot.edits] == [5296]
    assert bot.deleted_messages == [(-2001, 5297)]
    assert len(edit_message.replies) == 1
    assert reply_index.get_many(-2001, 4296) == (5296, 5297)


def test_channel_edit_failed_later_chunk_delete_is_tracked_as_stale_extra(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(
        [
            "I" * (TELEGRAM_TEXT_MESSAGE_LIMIT * 2 + 8),  # original: 3 chunks
            "J" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 6),  # edit: 2 chunks
        ]
    )
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    # Chunk 2's tracked reply rejects edits, and deleting it fails too.
    bot = FakeBot(
        edit_raises=lambda _text, _parse_mode, _chat_id, message_id: (
            BadRequest("Message can't be edited") if message_id == 5306 else None
        ),
        delete_raises=lambda _chat_id, message_id: (
            TelegramError("transient delete failure") if message_id == 5306 else None
        ),
    )
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4305)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 3
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4305) == (5305, 5306, 5307)

    edit_update, _ = channel_update(
        text=CN_TEXT, message_id=4305, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    # Chunk 2 lands on the third tracked reply; the stale uneditable reply
    # whose delete failed is appended as an extra, NOT at the chunk position
    # it failed at - otherwise the next edit would map chunk 2 onto it.
    assert [edit[3] for edit in bot.edits] == [5305, 5307]
    assert reply_index.get_many(-2001, 4305) == (5305, 5307, 5306)
    outcomes = context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes
    assert outcomes["delivery:stale_chunks_retained"] == 1


def test_edit_admitted_on_fresh_window_under_low_budget(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _t, _l: "translated")
    context = make_context(
        sample_db,
        config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1),
    )
    # A dictionary exact-hit original: a tracked reply without spending budget.
    original, original_message = channel_update(
        text="Echo", text_html="Echo", message_id=4320
    )
    asyncio.run(channel_post_handler(original, context))
    assert original_message.replies == [("声骸", None, 4320)]
    assert calls == []

    edit_update, _ = channel_update(
        text=CN_TEXT, message_id=4320, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))

    # Capacity 1: required(1) + headroom(2) is unreachable, but a completely
    # unused window must still admit one edit instead of yielding forever.
    assert context.bot.edits == [("translated", "HTML", -2001, 5320)]
    assert len(calls) == 1


def test_channel_reply_index_edit_tokens_expire_with_ttl():
    now = 500.0
    index = ChannelReplyIndex(ttl_seconds=30.0, clock=lambda: now)
    token = index.begin_edit(1, 1, update_id=10)
    assert index.is_latest_edit(1, 1, token) is True

    now = 531.0
    assert index.is_latest_edit(1, 1, token) is False
    # After expiry a fresh edit for the same post is admitted again.
    new_token = index.begin_edit(1, 1, update_id=11)
    assert index.is_latest_edit(1, 1, new_token) is True


def test_channel_reply_index_save_offloaded_still_persists(tmp_path):
    async def run() -> None:
        path = tmp_path / "channel_replies.json"
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        index.remember_many(-2001, 4001, (5001, 5002))
        # The write is offloaded on a running loop; flush it before asserting.
        task = index._save_task
        assert task is not None
        await task
        reloaded = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        assert reloaded.get_many(-2001, 4001) == (5001, 5002)

    asyncio.run(run())


def test_channel_reply_index_aflush_drains_queued_snapshot(tmp_path):
    async def run() -> None:
        path = tmp_path / "channel_replies.json"
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        write_started = threading.Event()
        release_write = threading.Event()
        original_write = index._write_payload_recording

        def blocking_write(payload):
            write_started.set()
            release_write.wait(timeout=5)
            original_write(payload)

        index._write_payload_recording = blocking_write
        index.remember_many(-2001, 4001, (5001,))
        while not write_started.is_set():
            await asyncio.sleep(0.001)
        # A second remember lands while the executor holds the first write:
        # its payload sits queued in _pending_save_payload.
        index.remember_many(-2001, 4002, (5002,))
        release_write.set()

        await index.aflush()

        reloaded = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        assert reloaded.get_many(-2001, 4001) == (5001,)
        assert reloaded.get_many(-2001, 4002) == (5002,)

    asyncio.run(run())


def test_channel_reply_index_aflush_survives_cancelled_save_task(tmp_path):
    async def run() -> None:
        path = tmp_path / "channel_replies.json"
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        index.remember_many(-2001, 4001, (5001,))
        task = index._save_task
        assert task is not None
        # Shutdown tears the loop down before the offloaded write completes.
        task.cancel()

        await index.aflush()

        reloaded = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        assert reloaded.get_many(-2001, 4001) == (5001,)

    asyncio.run(run())


def test_channel_reply_index_aflush_outlives_cancelled_executor_write(tmp_path):
    async def run() -> None:
        path = tmp_path / "channel_replies.json"
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        write_started = threading.Event()
        release_write = threading.Event()
        original_write = index._write_payload_recording

        def blocking_write(payload):
            write_started.set()
            release_write.wait(timeout=5)
            original_write(payload)

        index._write_payload_recording = blocking_write
        index.remember_many(-2001, 4001, (5001,))
        while not write_started.is_set():
            await asyncio.sleep(0.001)
        # A newer remember queues behind the blocked write; teardown then
        # cancels the save task, but the executor thread keeps running.
        index.remember_many(-2001, 4002, (5002,))
        task = index._save_task
        assert task is not None
        task.cancel()

        flusher = asyncio.create_task(index.aflush())
        await asyncio.sleep(0.05)  # let aflush reach the in-flight wait
        release_write.set()  # the stale snapshot completes first
        await flusher  # then the final inline write must land after it

        reloaded = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)
        assert reloaded.get_many(-2001, 4001) == (5001,)
        assert reloaded.get_many(-2001, 4002) == (5002,)

    asyncio.run(run())


def test_channel_edit_missing_continuation_chunk_replaces_and_drops_bad_id(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(
        [
            "I" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 4),
            "J" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 6),
        ]
    )
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    bot = FakeBot(
        edit_raises=lambda _text, _parse_mode, _chat_id, message_id: (
            BadRequest("Message to edit not found") if message_id == 5302 else None
        )
    )
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4301)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 2

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4301, edit_date=datetime.now(timezone.utc)
    )

    async def replacement_reply_text(text: str, **kwargs):
        edit_message.replies.append(
            (text, kwargs.get("parse_mode"), kwargs.get("reply_to_message_id"))
        )
        return SimpleNamespace(message_id=6302)

    edit_message.reply_text = replacement_reply_text
    asyncio.run(channel_post_handler(edit_update, context))

    assert bot.edits == [("J" * TELEGRAM_TEXT_MESSAGE_LIMIT, None, -2001, 5301)]
    assert bot.edit_attempts == [
        ("J" * TELEGRAM_TEXT_MESSAGE_LIMIT, None, -2001, 5301),
        ("J" * 6, None, -2001, 5302),
    ]
    assert edit_message.replies == [("J" * 6, None, 4301)]
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get_many(-2001, 4301) == (5301, 6302)
    assert len(calls) == 2


def test_channel_edit_keeps_failed_delete_tracked_for_retry(
    monkeypatch, sample_db
):
    calls = []
    responses = iter(
        ["K" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 8), "short one", "short two"]
    )
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: next(responses))
    failed_once = True

    def delete_raises(_chat_id, message_id):
        nonlocal failed_once
        if message_id == 5304 and failed_once:
            failed_once = False
            return TelegramError("temporary delete failed")
        return None

    bot = FakeBot(delete_raises=delete_raises)
    context = make_context(sample_db, bot=bot)

    new_update, new_message = channel_update(text=CN_TEXT, message_id=4303)
    asyncio.run(channel_post_handler(new_update, context))
    assert len(new_message.replies) == 2

    first_edit_update, _ = channel_update(
        text=CN_TEXT, message_id=4303, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(first_edit_update, context))

    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert bot.deleted_messages == [(-2001, 5304)]
    assert reply_index.get_many(-2001, 4303) == (5303, 5304)
    outcomes = context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes
    assert outcomes["delivery:stale_chunks_retained"] == 1

    second_edit_update, _ = channel_update(
        text=CN_TEXT, message_id=4303, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(second_edit_update, context))

    assert bot.deleted_messages == [(-2001, 5304), (-2001, 5304)]
    assert reply_index.get_many(-2001, 4303) == (5303,)
    assert len(calls) == 3


def test_edit_waits_for_long_original_delivery_after_first_chunk_remembered(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        calls = []

        async def fake_call(
            _locked_text,
            _locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
        ):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return "G" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 5)
            return "edited short translation"

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = make_context(sample_db)
        new_update, new_message = channel_update(text=CN_TEXT, message_id=4300)

        second_chunk_started = asyncio.Event()
        release_second_chunk = asyncio.Event()
        original_reply_text = new_message.reply_text

        async def blocking_reply_text(text: str, **kwargs):
            if len(new_message.replies) == 1:
                second_chunk_started.set()
                await release_second_chunk.wait()
            return await original_reply_text(text, **kwargs)

        new_message.reply_text = blocking_reply_text
        new_task = asyncio.create_task(channel_post_handler(new_update, context))
        await asyncio.wait_for(second_chunk_started.wait(), timeout=0.2)

        reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
        assert reply_index.get_many(-2001, 4300) == (5300,)

        edit_update, edit_message = channel_update(
            text=f"{CN_TEXT}新",
            message_id=4300,
            update_id=102,
            edit_date=datetime.now(timezone.utc),
        )
        edit_task = asyncio.create_task(channel_post_handler(edit_update, context))
        await asyncio.sleep(0.05)

        assert not edit_task.done()
        assert calls == [1]

        release_second_chunk.set()
        await asyncio.wait_for(new_task, timeout=0.2)
        await asyncio.wait_for(edit_task, timeout=0.2)

        assert edit_message.replies == []
        assert context.bot.edits == [
            ("edited short translation", "HTML", -2001, 5300)
        ]
        assert context.bot.deleted_messages == [(-2001, 5301)]
        assert reply_index.get_many(-2001, 4300) == (5300,)
        assert calls == [1, 2]

    asyncio.run(run())


def test_edit_identical_translation_is_silent_noop(monkeypatch, sample_db):
    # Telegram rejects an unchanged edit with "message is not modified"; that is
    # the dedup ideal and must be a silent no-op, never a crash.
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    bot = FakeBot(edit_raises=lambda *_: BadRequest("Message is not modified"))
    context = make_context(sample_db, bot=bot)

    new_update, _ = channel_update(text=CN_TEXT, message_id=4230)
    asyncio.run(channel_post_handler(new_update, context))

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4230, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))  # must not raise

    assert edit_message.replies == []
    assert len(bot.edit_attempts) == 1  # attempted then swallowed
    assert bot.edits == []  # nothing recorded as applied


def test_edit_invalid_html_falls_back_to_plain_edit(monkeypatch, sample_db):
    # An HTML edit Telegram rejects re-raises into the plain-edit fallback,
    # mirroring the new-post fallback — formatting never fails the update.
    def response(locked_text, locks):
        jinhsi = placeholder_for(locks, "Jinhsi")
        echo = placeholder_for(locks, "Echo")
        return html_with_segments(
            locked_text, "", jinhsi, " says ", echo, " is strong", "", ""
        )

    calls = []
    enable_mock_llm(monkeypatch, calls, response)

    def raise_on_html(text, parse_mode, chat_id, message_id):
        return BadRequest("Can't parse entities: bad") if parse_mode == "HTML" else None

    bot = FakeBot(edit_raises=raise_on_html)
    context = make_context(sample_db, bot=bot)

    new_update, _ = channel_update(
        text=CN_LOCK_TEXT, text_html=CN_LOCK_TEXT_HTML, message_id=4240
    )
    asyncio.run(channel_post_handler(new_update, context))

    edit_update, edit_message = channel_update(
        text=CN_LOCK_TEXT,
        text_html=CN_LOCK_TEXT_HTML,
        message_id=4240,
        edit_date=datetime.now(timezone.utc),
    )
    asyncio.run(channel_post_handler(edit_update, context))

    assert edit_message.replies == []
    assert bot.edit_attempts[0][1] == "HTML"  # HTML attempted first
    assert bot.edits == [("Jinhsi says Echo is strong", None, -2001, 5240)]


def test_edit_reply_gone_is_skipped_silently(monkeypatch, sample_db):
    # If the tracked reply was deleted, the edit fails ("not found"); swallow it
    # — never crash the listener, never post a new reply.
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    bot = FakeBot(edit_raises=lambda *_: BadRequest("Message to edit not found"))
    context = make_context(sample_db, bot=bot)

    new_update, _ = channel_update(text=CN_TEXT, message_id=4250)
    asyncio.run(channel_post_handler(new_update, context))

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4250, edit_date=datetime.now(timezone.utc)
    )
    asyncio.run(channel_post_handler(edit_update, context))  # must not raise

    assert edit_message.replies == []
    assert bot.edits == []
    assert len(bot.edit_attempts) >= 1


def test_fresh_edit_with_no_tracked_reply_bypasses_public_command_throttle(
    monkeypatch, sample_db
):
    # A fresh edit for a post this process already observed without replying is
    # treated as the first translatable version of the trusted channel post.
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    context = make_context(
        sample_db, config=BotConfig(rate_limit_per_minute=1, owner_user_id=11)
    )
    now = datetime.now(timezone.utc)

    original_update, original_message = channel_update(message_id=4260, date=now)
    asyncio.run(channel_post_handler(original_update, context))
    assert original_message.replies == []

    edit_update, edit_message = channel_update(
        text=CN_TEXT, message_id=4260, date=now, edit_date=now
    )
    asyncio.run(channel_post_handler(edit_update, context))
    assert edit_message.replies == [("translated", "HTML", 4260)]
    assert context.bot.edits == []
    assert len(calls) == 1

    fresh_update, fresh_message = channel_update(text=CN_TEXT, message_id=4261)
    asyncio.run(channel_post_handler(fresh_update, context))
    assert fresh_message.replies == [("translated", "HTML", 4261)]
    assert len(calls) == 2


def test_channel_reply_index_remembers_gets_and_expires():
    index = ChannelReplyIndex(ttl_seconds=300.0)
    index.remember(-2001, 4001, 5001, now=0.0)

    assert index.get(-2001, 4001, now=299.9) == 5001
    assert index.get(-2001, 4001, now=300.0) is None  # expired at TTL
    assert index.get(-2001, 9999, now=0.0) is None  # unknown key


def test_channel_reply_index_observed_without_reply_expires():
    index = ChannelReplyIndex(ttl_seconds=300.0)
    index.remember_observed_without_reply(-2001, 4001, now=0.0)

    assert index.was_observed_without_reply(-2001, 4001, now=299.9) is True
    assert index.was_observed_without_reply(-2001, 4001, now=300.0) is False


def test_channel_reply_index_enforces_stable_capacity_for_entries_and_sentinels():
    index = ChannelReplyIndex(ttl_seconds=10.0, max_entries=2)
    index.remember(2, 1, 201, now=0.0)
    index.remember(1, 2, 102, now=0.0)
    index.remember(3, 1, 301, now=0.0)

    assert index.entry_count(now=0.0) == 2
    assert index.get(1, 2, now=0.0) is None
    assert index.get(2, 1, now=0.0) == 201
    assert index.get(3, 1, now=0.0) == 301

    index.remember_observed_without_reply(2, 1, now=0.0)
    index.remember_observed_without_reply(1, 2, now=0.0)
    index.remember_observed_without_reply(3, 1, now=0.0)

    assert index.observed_count(now=0.0) == 2
    assert index.was_observed_without_reply(1, 2, now=0.0) is False
    assert index.was_observed_without_reply(2, 1, now=0.0) is True
    assert index.was_observed_without_reply(3, 1, now=0.0) is True


def test_channel_reply_index_trims_loaded_state_and_persists_survivors(tmp_path):
    path = tmp_path / "channel_replies.json"
    payload = {
        "version": 1,
        "entries": [
            {
                "chat_id": chat_id,
                "message_id": 1,
                "expires_at": 1100.0,
                "reply_message_ids": [chat_id + 100],
            }
            for chat_id in (1, 2, 3)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    index = ChannelReplyIndex(
        ttl_seconds=60.0,
        storage_path=path,
        clock=lambda: 1000.0,
        max_entries=2,
    )

    assert index.entry_count() == 2
    assert index.get_many(1, 1) == ()
    assert index.get_many(2, 1) == (102,)
    assert index.get_many(3, 1) == (103,)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert {row["chat_id"] for row in persisted["entries"]} == {2, 3}


def test_channel_reply_index_active_edit_eviction_fails_closed_and_cleans_lock():
    async def exercise():
        index = ChannelReplyIndex(ttl_seconds=10.0, max_entries=1)
        index.remember(1, 1, 101, now=0.0)
        token = index.begin_edit(1, 1, update_id=10)
        lock = index.edit_delivery_lock(1, 1)

        async with lock:
            index.remember(2, 1, 201, now=1.0)
            assert index.is_latest_edit(1, 1, token) is False
            assert (1, 1) in index._edit_delivery_locks

        index.prune(now=1.0)
        assert (1, 1) not in index._edit_delivery_locks

    asyncio.run(exercise())


def test_channel_reply_index_persists_and_loads_with_wall_clock_ttl(tmp_path):
    now = 1000.0

    def clock():
        return now

    path = tmp_path / "channel_replies.json"
    index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path, clock=clock)
    index.remember_many(-2001, 4001, (5001, 5002))

    reloaded = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path, clock=clock)
    assert reloaded.get_many(-2001, 4001) == (5001, 5002)

    now = 1060.0
    assert reloaded.get_many(-2001, 4001) == ()

    expired = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path, clock=clock)
    assert expired.get_many(-2001, 4001) == ()


def test_channel_reply_index_expired_read_does_not_rewrite_storage(tmp_path):
    now = 1000.0

    def clock():
        return now

    path = tmp_path / "channel_replies.json"
    index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path, clock=clock)
    index.remember_many(-2001, 4001, (5001,))
    persisted = path.read_text(encoding="utf-8")

    now = 1060.0
    assert index.get_many(-2001, 4001) == ()
    assert path.read_text(encoding="utf-8") == persisted


def test_channel_reply_index_ignores_corrupt_persistence_file(tmp_path, caplog):
    path = tmp_path / "channel_replies.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)

    assert index.entry_count() == 0
    assert index.persistence_enabled() is True
    assert index.load_failure_count() == 1
    assert index.last_load_succeeded() is False
    assert "channel reply index unreadable" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "-2001" not in caplog.text
    assert "JSONDecodeError" not in caplog.text
    assert "Expecting" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"entries": {}}',
    ],
)
def test_channel_reply_index_counts_malformed_persistence_payload(
    tmp_path, caplog, payload
):
    path = tmp_path / "channel_replies.json"
    path.write_text(payload, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)

    assert index.entry_count() == 0
    assert index.persistence_enabled() is True
    assert index.load_failure_count() == 1
    assert index.last_load_succeeded() is False
    assert "channel reply index unreadable" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "entries" not in caplog.text


@pytest.mark.parametrize(
    "malformed_row",
    [
        [],
        {"chat_id": -2001, "message_id": 4002, "expires_at": 1100.0},
        {
            "chat_id": -2001,
            "message_id": 4002,
            "expires_at": 1100.0,
            "reply_message_ids": None,
        },
        {
            "chat_id": -2001,
            "message_id": 4002,
            "expires_at": 1100.0,
            "reply_message_ids": [],
        },
    ],
)
def test_channel_reply_index_counts_partially_malformed_rows(
    tmp_path, caplog, malformed_row
):
    path = tmp_path / "channel_replies.json"
    payload = {
        "version": 1,
        "entries": [
            {
                "chat_id": -2001,
                "message_id": 4001,
                "expires_at": 1100.0,
                "reply_message_ids": [5001, 5002],
            },
            malformed_row,
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        index = ChannelReplyIndex(
            ttl_seconds=60.0, storage_path=path, clock=lambda: 1000.0
        )

    assert index.get_many(-2001, 4001) == (5001, 5002)
    assert index.load_failure_count() == 1
    assert index.last_load_succeeded() is False
    assert "channel reply index contained malformed rows" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "-2001" not in caplog.text
    assert "4002" not in caplog.text
    assert "reply_message_ids" not in caplog.text


def test_channel_reply_index_counts_unreadable_persistence_path(tmp_path, caplog):
    path = tmp_path / "channel_replies.json"
    path.mkdir()

    with caplog.at_level(logging.WARNING):
        index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)

    assert index.entry_count() == 0
    assert index.persistence_enabled() is True
    assert index.load_failure_count() == 1
    assert index.last_load_succeeded() is False
    assert "channel reply index unreadable" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "IsADirectoryError" not in caplog.text


def test_channel_reply_index_save_failure_does_not_block_memory(tmp_path, caplog):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("x", encoding="utf-8")
    path = blocked_parent / "channel_replies.json"
    index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=path)

    with caplog.at_level(logging.WARNING):
        index.remember_many(-2001, 4001, (5001,))

    assert index.get_many(-2001, 4001) == (5001,)
    assert index.persistence_enabled() is True
    assert index.save_failure_count() == 1
    assert index.last_save_succeeded() is False
    assert "channel reply index save failed" in caplog.text
    assert "-2001" not in caplog.text
