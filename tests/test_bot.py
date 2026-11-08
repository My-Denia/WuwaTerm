from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from telegram import Chat, MessageEntity, Update, User
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ChatMemberHandler, CommandHandler, MessageHandler

from wuwaterm.logging_utils import REDACTION_SECRET_ENV, redact_id
from wuwaterm.bot import (
    ADMIN_CACHE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    DEFAULT_GROUP_TR_REJECT_TEXT,
    DEFAULT_PRIVATE_TR_REJECT_TEXT,
    DIRECTION_USAGE_NOTICE,
    LLM_INPUT_CHAR_LIMIT,
    PUBLIC_DISABLED_NOTICE,
    PUBLIC_ENABLED_NOTICE,
    PUBLIC_ONLY_GROUPS_NOTICE,
    PUBLIC_REJECT_NOTICE,
    SETTINGS_DENY_NOT_PERSISTED_NOTICE,
    SETTINGS_DURABILITY_UNCERTAIN_NOTICE,
    SETTINGS_SAVE_FAILED_NOTICE,
    PUBLIC_STATUS_OFF,
    PUBLIC_STATUS_ON,
    PUBLIC_USAGE_NOTICE,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    SENTENCE_USAGE_NOTICE,
    SERVICE_KEY,
    TERM_USAGE_NOTICE,
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    THROTTLE_NOTICE,
    TRANSLATOR_KEY,
    UNAUTHORIZED_GROUP_NOTICE,
    AdminStatusCache,
    BotConfig,
    BotConfigError,
    ChannelReplyIndex,
    PerChatRateLimiter,
    StateMigrationError,
    about_command,
    GRANT_USAGE,
    grant_command,
    create_application,
    my_chat_member_handler,
    public_command,
    reply_to_user,
    revoke_command,
    sentence_command,
    status_command,
    term_command,
    translate_query_async,
    _inline_translation_html,
    _log_update_error,
    _validate_llm_config_env,
)
from wuwaterm.lookup import TermService
from wuwaterm.channel_runtime import ChannelRuntime
from wuwaterm.sentence import (
    BUDGET_EXHAUSTED_NOTICE,
    LLMTranslationError,
    TRANSLATION_UNAVAILABLE_NOTICE,
    SentenceTranslator,
    _llm_error_from_response,
)
from wuwaterm.settings import ChatSettings, ChatSettingsDurabilityError
from wuwaterm.telegram_text import telegram_text_units


_BOT_CONFIG_ENV_NAMES = (
    "OWNER_USER_ID",
    "WUWATERM_RATE_LIMIT_PER_MINUTE",
    "WUWATERM_GROUP_TR_REJECT_TEXT",
    "WUWATERM_PRIVATE_TR_REJECT_TEXT",
    "WUWATERM_TR_REJECT_SILENT",
    "WUWATERM_CHANNEL_AUTOTRANSLATE",
    "WUWATERM_CHANNEL_MIN_CJK",
    "WUWATERM_CHANNEL_MIN_LATIN",
    "WUWATERM_CHANNEL_TEXT_LIMIT",
    "WUWATERM_CHANNEL_CAPTION_LIMIT",
    "WUWATERM_CHANNEL_MAX_AGE_SECONDS",
    "WUWATERM_CHANNEL_MAX_PENDING",
    "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE",
    "WUWATERM_CHANNEL_REPLY_INDEX_PATH",
    "WUWATERM_LLM_TIMEOUT_SECONDS",
    "WUWATERM_LLM_MAX_CONCURRENCY",
    "WUWATERM_OPENAI_BASE_URL",
    "WUWATERM_OPENAI_API_KEY",
    "WUWATERM_OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
)


def clear_bot_config_env(monkeypatch):
    for name in _BOT_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


class FakeMessage:
    def __init__(
        self,
        message_id: int = 101,
        sender_chat_id: int | None = None,
        reply_raises=None,
    ):
        self.message_id = message_id
        self.sender_chat = (
            SimpleNamespace(id=sender_chat_id) if sender_chat_id is not None else None
        )
        self.replies: list[tuple[str, int | None]] = []
        self.reply_kwargs: list[dict] = []
        self._reply_raises = reply_raises

    async def reply_text(self, text: str, **kwargs) -> None:
        if telegram_text_units(text) > TELEGRAM_TEXT_MESSAGE_LIMIT:
            raise TelegramError("Message is too long")
        self.reply_kwargs.append(dict(kwargs))
        if self._reply_raises is not None:
            exc = self._reply_raises(text, kwargs, len(self.reply_kwargs))
            if exc is not None:
                raise exc
        self.replies.append((text, kwargs.get("reply_to_message_id")))
        return SimpleNamespace(message_id=self.message_id + len(self.replies))


class FakeBot:
    def __init__(self, default_status: str = "member", overrides=None):
        self.default_status = default_status
        self.overrides = dict(overrides or {})
        self.member_calls: list[tuple[int, int]] = []
        self.sent_messages: list[tuple[int, str]] = []
        self.left_chats: list[int] = []

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.member_calls.append((chat_id, user_id))
        outcome = self.overrides.get((chat_id, user_id), self.default_status)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status=outcome)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent_messages.append((chat_id, text))
        return SimpleNamespace(message_id=999)

    async def leave_chat(self, chat_id: int):
        self.left_chats.append(chat_id)
        return True


def fake_update(
    chat_id: int = 1,
    chat_type: str = "private",
    message_id: int = 101,
    user_id: int | None = 11,
    sender_chat_id: int | None = None,
    reply_to=None,
    reply_raises=None,
):
    message = FakeMessage(
        message_id=message_id,
        sender_chat_id=sender_chat_id,
        reply_raises=reply_raises,
    )
    if reply_to is not None:
        message.reply_to_message = reply_to
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return (
        SimpleNamespace(effective_message=message, effective_chat=chat, effective_user=user),
        message,
    )


def test_reply_log_redacts_text_ids_and_secrets(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TELEGRAM_TOKEN")
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://secret.example/v1")
    monkeypatch.setenv(REDACTION_SECRET_ENV, "log-redaction-secret")
    update, message = fake_update(
        chat_id=-990011,
        chat_type="supergroup",
        message_id=778899,
        user_id=123456,
    )

    with caplog.at_level(logging.INFO, logger="wuwaterm.bot"):
        asyncio.run(reply_to_user(update, "SECRET_REPLY_TEXT"))

    assert message.replies == [("SECRET_REPLY_TEXT", 778899)]
    log_text = caplog.text
    assert "SECRET_REPLY_TEXT" not in log_text
    assert "-990011" not in log_text
    assert "778899" not in log_text
    assert "778900" not in log_text
    assert "123456" not in log_text
    assert "FAKE_TELEGRAM_TOKEN" not in log_text
    assert "secret.example" not in log_text
    assert "log-redaction-secret" not in log_text
    assert redact_id(778899) in log_text
    assert redact_id(778900) in log_text
    assert "chunk=1/1" in log_text
    assert "text_len=17" in log_text


def fake_member_update(
    *,
    chat_id: int = -2001,
    chat_type: str = "supergroup",
    old_status: str = "left",
    new_status: str = "member",
    from_id: int | None = 11,
    title: str = "g",
    old_is_member: bool | None = None,
    new_is_member: bool | None = None,
):
    """Build a my_chat_member update (the bot's OWN membership change).

    old_is_member / new_is_member set the ChatMemberRestricted.is_member flag
    when given (None = attribute absent, as for non-restricted statuses)."""
    chat = SimpleNamespace(id=chat_id, type=chat_type, title=title)
    from_user = SimpleNamespace(id=from_id) if from_id is not None else None
    old_member = SimpleNamespace(status=old_status)
    new_member = SimpleNamespace(status=new_status)
    if old_is_member is not None:
        old_member.is_member = old_is_member
    if new_is_member is not None:
        new_member.is_member = new_is_member
    cmu = SimpleNamespace(
        old_chat_member=old_member,
        new_chat_member=new_member,
        from_user=from_user,
        chat=chat,
    )
    return SimpleNamespace(
        my_chat_member=cmu,
        effective_chat=chat,
        effective_message=None,
        effective_user=from_user,
    )


def fake_context(
    sample_db,
    args,
    *,
    limit=10,
    member_status="administrator",
    member_overrides=None,
    config=None,
    chat_settings=None,
    allowlist=(-2001, -2002, -2003),
):
    config = config or BotConfig(rate_limit_per_minute=limit, owner_user_id=11)
    bot = FakeBot(default_status=member_status, overrides=member_overrides)
    if chat_settings is None:
        # Each test gets its own settings file under sample_db's per-test tmp dir.
        # The bot only serves allowlisted groups, so the common test group ids are
        # authorized by default; pass allowlist=() to exercise the unauthorized path.
        chat_settings = ChatSettings(sample_db.parent / "chat_settings.json")
        for _cid in allowlist:
            chat_settings.allow(_cid)
    return SimpleNamespace(
        args=args,
        bot=bot,
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
def raise_durability_uncertain(*_args):
    raise ChatSettingsDurabilityError("directory durability uncertain")




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
        references=(),
    ):
        calls.append((locked_text, locks))
        if len(inspect.signature(response_factory).parameters) >= 3:
            return response_factory(locked_text, locks, references)
        return response_factory(locked_text, locks)

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)


def placeholder_for(locks, official, references=(), *, to_chinese=True):
    """What the model would emit for one official term.

    A hard lock stays a placeholder the server restores; a contextual
    reference is written as the official term in the target language.
    """
    for placeholder, _source, en in locks:
        if en == official:
            return placeholder
    for _source, zh, en, _category in references:
        if en == official:
            return zh if to_chinese else en
    raise AssertionError(f"missing official term {official}")


def html_with_segments(locked_text: str, *segments: str) -> str:
    placeholders = re.findall(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4}__", locked_text)
    assert len(segments) == len(placeholders) + 1
    parts: list[str] = []
    for segment, placeholder in zip(segments, placeholders, strict=False):
        parts.extend((segment, placeholder))
    parts.append(segments[-1])
    return "".join(parts)


def test_term_command_uses_db_first(sample_db):
    update, message = fake_update()
    context = fake_context(sample_db, ["声骸"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", None)]


def test_sentence_command_locks_terms_without_llm(monkeypatch, sample_db):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    update, message = fake_update()
    context = fake_context(sample_db, ["今汐装备了声骸"])

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("Jinhsi装备了Echo", None)]


def test_tr_non_exact_text_uses_locked_llm(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, locks, references):
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=False)} says "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=False)} is strong."
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = fake_update()
    context = fake_context(sample_db, ["今汐说声骸很强"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Jinhsi says Echo is strong.", None)]
    assert len(calls) == 1
    locked_text, locks = calls[0]
    # Chinese source: every term is a contextual reference, so the model sees
    # the original words and nothing is replaced before it translates them.
    assert "今汐" in locked_text
    assert locks == ()


def test_group_term_replies_to_asking_message(sample_db):
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=555)
    context = fake_context(sample_db, ["声骸"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", 555)]


def test_group_reply_falls_back_when_reply_target_is_missing(sample_db, caplog):
    def fail_initial_reply(_text, kwargs, call_number):
        if call_number == 1:
            assert kwargs == {"reply_to_message_id": 561}
            return BadRequest("Message to be replied not found")
        return None

    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=561,
        reply_raises=fail_initial_reply,
    )
    context = fake_context(sample_db, ["声骸"])

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", None)]
    assert message.reply_kwargs == [
        {"reply_to_message_id": 561},
        {"do_quote": False},
    ]
    assert any("reply target missing" in record.getMessage() for record in caplog.records)


def test_group_reply_non_missing_bad_request_still_fails(sample_db):
    def fail_initial_reply(_text, _kwargs, call_number):
        if call_number == 1:
            return BadRequest("Message is too long")
        return None

    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=562,
        reply_raises=fail_initial_reply,
    )
    context = fake_context(sample_db, ["声骸"])

    with pytest.raises(BadRequest, match="Message is too long"):
        asyncio.run(term_command(update, context))

    assert message.replies == []
    assert message.reply_kwargs == [{"reply_to_message_id": 562}]


def test_group_html_reply_missing_target_then_parse_error_falls_back_plain(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        assert html_mode is True
        return html_with_segments(
            locked_text,
            "",
            placeholder_for(locks, "Jinhsi", references, to_chinese=to_chinese),
            " says hi",
        )

    def fail_then_parse_error(_text, kwargs, call_number):
        if call_number == 1:
            assert kwargs == {"reply_to_message_id": 563, "parse_mode": "HTML"}
            return BadRequest("Message to be replied not found")
        if call_number == 2:
            assert kwargs == {"do_quote": False, "parse_mode": "HTML"}
            return BadRequest("Can't parse entities: bad")
        assert kwargs == {"do_quote": False}
        return None

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text="今汐说你好",
        caption=None,
        text_html="<b>今汐</b>说你好",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=563,
        reply_to=replied,
        reply_raises=fail_then_parse_error,
    )
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Jinhsi says hi", None)]
    assert message.reply_kwargs == [
        {"reply_to_message_id": 563, "parse_mode": "HTML"},
        {"do_quote": False, "parse_mode": "HTML"},
        {"do_quote": False},
    ]


def test_group_tr_admin_sentence_uses_llm(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, locks, references):
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=False)} equips "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=False)}."
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=556)
    context = fake_context(sample_db, ["今汐装备了声骸"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Jinhsi equips Echo.", 556)]
    assert len(calls) == 1


def test_short_dictionary_miss_appends_bilingual_flag_without_hash(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "Unlisted term")
    update, message = fake_update()
    context = fake_context(sample_db, ["不存在词条"])

    asyncio.run(term_command(update, context))

    assert message.replies == [
        (
            "Unlisted term\n\n"
            "(词典外,机器直译)\n"
            "(Not in official dictionary; machine-translated)",
            None,
        )
    ]
    # The 40-char commit hash must never reach a user-facing reply.
    assert re.search(r"[0-9a-f]{40}", message.replies[0][0]) is None
    assert len(calls) == 1


def test_llm_path_allows_2000_chars(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "translated")
    update, message = fake_update()
    context = fake_context(sample_db, ["测" * LLM_INPUT_CHAR_LIMIT])

    asyncio.run(term_command(update, context))

    assert LLM_INPUT_CHAR_LIMIT == 2000
    assert message.replies == [("translated", None)]
    assert len(calls) == 1


def test_llm_output_over_telegram_limit_is_split(monkeypatch, sample_db):
    calls = []
    translated = "A" * (TELEGRAM_TEXT_MESSAGE_LIMIT + 17)
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: translated)
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=559)
    context = fake_context(sample_db, ["测" * LLM_INPUT_CHAR_LIMIT])

    asyncio.run(term_command(update, context))

    assert message.replies == [
        (translated[:TELEGRAM_TEXT_MESSAGE_LIMIT], 559),
        (translated[TELEGRAM_TEXT_MESSAGE_LIMIT:], None),
    ]
    assert message.reply_kwargs == [
        {"reply_to_message_id": 559},
        {"do_quote": False},
    ]
    assert all(len(reply) <= TELEGRAM_TEXT_MESSAGE_LIMIT for reply, _ in message.replies)
    assert "".join(reply for reply, _ in message.replies) == translated
    assert len(calls) == 1


def test_llm_output_split_counts_emoji_as_utf16_units(monkeypatch, sample_db):
    calls = []
    translated = "😀" * (TELEGRAM_TEXT_MESSAGE_LIMIT // 2 + 3)
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: translated)
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=560)
    context = fake_context(sample_db, ["测" * LLM_INPUT_CHAR_LIMIT])

    asyncio.run(term_command(update, context))

    assert [telegram_text_units(reply) for reply, _ in message.replies] == [
        TELEGRAM_TEXT_MESSAGE_LIMIT,
        6,
    ]
    assert message.replies[0][1] == 560
    assert message.replies[1][1] is None
    assert "".join(reply for reply, _ in message.replies) == translated
    assert len(calls) == 1


def test_llm_path_rejects_over_2000_chars_before_call(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=529, user_id=42
    )
    context = fake_context(
        sample_db, ["测" * (LLM_INPUT_CHAR_LIMIT + 1)], member_status="member"
    )
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)

    asyncio.run(term_command(update, context))

    assert LLM_INPUT_CHAR_LIMIT == 2000
    assert message.replies == [
        (
            f"Input is too long for translation ({LLM_INPUT_CHAR_LIMIT} character limit).",
            529,
        )
    ]
    assert calls == []


@pytest.mark.parametrize("llm_output", ["   ", "\n\n"])
def test_term_command_blank_llm_output_returns_unavailable_notice(
    monkeypatch, sample_db, llm_output
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: llm_output)
    update, message = fake_update()
    context = fake_context(
        sample_db, ["这是一个需要翻译的完整句子，请翻译一下。"]
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [(TRANSLATION_UNAVAILABLE_NOTICE, None)]
    assert len(calls) == 1


@pytest.mark.parametrize("llm_output", ["   ", "\n\n"])
def test_translate_query_blank_short_miss_does_not_append_dict_miss(
    monkeypatch, sample_db, llm_output
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: llm_output)
    service = TermService(sample_db)
    translator = SentenceTranslator(sample_db)

    result = asyncio.run(translate_query_async(service, translator, "foobar"))

    assert result == TRANSLATION_UNAVAILABLE_NOTICE
    assert "Not in official dictionary" not in result
    assert len(calls) == 1


def test_term_command_budget_exhaustion_returns_clean_bot_reply(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        locked_text,
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
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=558)
    context = fake_context(sample_db, ["这是一个需要翻译的句子。"])

    asyncio.run(term_command(update, context))

    assert message.replies == [(BUDGET_EXHAUSTED_NOTICE, 558)]
    reply = message.replies[0][0]
    assert reply == (
        "本月翻译额度已用完,请稍后再试。\n"
        "This month's translation quota is used up. Please try again later."
    )
    assert "429" not in reply
    assert "max_budget" not in reply
    assert "exceeded" not in reply


def test_screenshot_noise_speaker_prefix_and_quote_bars_are_normalized(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, locks, references):
        return (
            f"{placeholder_for(locks, 'Lucilla', references, to_chinese=False)}: "
            f"{placeholder_for(locks, 'Cartethyia', references, to_chinese=False)} found "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=False)}."
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=557)
    context = fake_context(
        sample_db,
        ["(WW 3.4)", "[spolier]", "> 洛瑟菈: Ｃａｒｔｅｔｈｙｉａ发现了声骸"],
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [("Lucilla: Cartethyia found Echo.", 557)]
    locked_text, locks = calls[0]
    assert "WW 3.4" not in locked_text
    assert "spolier" not in locked_text.casefold()
    assert ">" not in locked_text
    # The speaker prefix is resolved to the official English name; the body
    # keeps its Chinese terms as visible references.
    assert "洛瑟菈" not in locked_text
    assert "声骸" in locked_text
    assert locks == ()


def test_spoiler_spelling_and_version_tag_are_stripped(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda locked_text, _locks: locked_text)
    update, message = fake_update()
    context = fake_context(sample_db, ["[spoiler]", "(WW 3.4)", "这是一个测试。"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("这是一个测试。", None)]
    assert calls == [("这是一个测试。", ())]


def test_stylized_unicode_cartethyia_matches_ascii(sample_db):
    update, message = fake_update()
    context = fake_context(sample_db, ["Ｃａｒｔｅｔｈｙｉａ"])

    asyncio.run(term_command(update, context))

    # Stylized fullwidth English normalizes to "Cartethyia" and matches the
    # term's English side; an English query now auto-translates to the
    # official Chinese (EN->ZH direction).
    assert message.replies == [("卡提希娅", None)]


def test_tr_english_term_returns_official_chinese(sample_db):
    update, message = fake_update()
    context = fake_context(sample_db, ["Echo"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("声骸", None)]


def test_tr_english_sentence_translates_to_chinese(monkeypatch, sample_db):
    calls = []

    def response(_locked_text, locks, references):
        return (
            f"{placeholder_for(locks, 'Jinhsi', references)}装备了"
            f"{placeholder_for(locks, 'Echo', references)}"
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = fake_update()
    context = fake_context(sample_db, ["Jinhsi equips Echo"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("今汐装备了声骸", None)]
    assert len(calls) == 1


def test_tr_auto_chinese_sentence_defaults_to_english(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} equips "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["今汐装备了声骸"])

    asyncio.run(term_command(update, context))

    assert calls == [False]
    assert message.replies == [("Jinhsi equips Echo", None)]


def test_tr_auto_english_sentence_defaults_to_chinese(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)}装备了"
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["Jinhsi equips Echo"])

    asyncio.run(term_command(update, context))

    assert calls == [True]
    assert message.replies == [("今汐装备了声骸", None)]


def test_tr_to_en_flag_forces_english(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} equips "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["--to", "en", "Jinhsi equips Echo"])

    asyncio.run(term_command(update, context))

    assert calls == [False]
    assert message.replies == [("Jinhsi equips Echo", None)]


def test_tr_short_to_zh_flag_forces_chinese(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)}装备了"
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["-to", "zh", "今汐装备了声骸"])

    asyncio.run(term_command(update, context))

    assert calls == [True]
    assert message.replies == [("今汐装备了声骸", None)]


def test_sentence_direction_flag_uses_same_parser(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} equips "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["--to", "en", "Jinhsi equips Echo"])

    asyncio.run(sentence_command(update, context))

    assert calls == [False]
    assert message.replies == [("Jinhsi equips Echo", None)]


def test_invalid_direction_returns_usage_without_llm_or_rate_hit(
    monkeypatch, sample_db
):
    calls = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        calls.append(to_chinese)
        return "should not be used"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["--to", "jp", "今汐说声骸很强"], limit=1)

    asyncio.run(term_command(update, context))
    valid_update, valid_message = fake_update()
    valid_context = fake_context(sample_db, ["声骸"], limit=1)
    valid_context.application.bot_data[RATE_LIMITER_KEY] = context.application.bot_data[
        RATE_LIMITER_KEY
    ]

    asyncio.run(term_command(valid_update, valid_context))

    assert calls == []
    assert message.replies == [(DIRECTION_USAGE_NOTICE, None)]
    assert valid_message.replies == [("Echo", None)]


def test_duplicate_direction_returns_usage_without_llm(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        calls.append(to_chinese)
        return "should not be used"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["--to", "en", "--to", "zh", "声骸"])

    asyncio.run(term_command(update, context))

    assert calls == []
    assert message.replies == [(DIRECTION_USAGE_NOTICE, None)]


def test_public_invalid_direction_uses_reject_limiter_not_translation_budget(
    sample_db,
):
    admin_update, _admin_msg = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=671
    )
    context = fake_context(sample_db, ["on"], limit=1, member_status="administrator")
    asyncio.run(public_command(admin_update, context))
    context.bot.default_status = "member"
    context.application.bot_data[ADMIN_CACHE_KEY] = AdminStatusCache()

    context.args = ["--to", "jp", "今汐说声骸很强"]
    first_update, first_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=672, user_id=42
    )
    second_update, second_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=673, user_id=43
    )

    asyncio.run(term_command(first_update, context))
    asyncio.run(term_command(second_update, context))

    context.args = ["声骸"]
    valid_update, valid_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=674, user_id=44
    )
    asyncio.run(term_command(valid_update, context))

    assert first_message.replies == [(DIRECTION_USAGE_NOTICE, 672)]
    assert second_message.replies == []
    assert valid_message.replies == [("Echo", 674)]


def test_forced_direction_reply_to_message(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append(to_chinese)
        return (
            f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} equips "
            f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
        )

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update(
        reply_to=SimpleNamespace(text="Jinhsi equips Echo", caption=None)
    )
    context = fake_context(sample_db, ["--to", "en"])

    asyncio.run(term_command(update, context))

    assert calls == [False]
    assert message.replies == [("Jinhsi equips Echo", None)]


def test_forced_direction_exact_hit_does_not_call_llm(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        calls.append(to_chinese)
        return "should not be used"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    update, message = fake_update()
    context = fake_context(sample_db, ["--to", "en", "声骸"])

    asyncio.run(term_command(update, context))

    assert calls == []
    assert message.replies == [("Echo", None)]


def test_slow_llm_does_not_block_control_commands_or_other_chat(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_call(
            _locked_text,
            locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
            references=(),
        ):
            started.set()
            await release.wait()
            return (
                f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} says "
                f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
            )

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = fake_context(sample_db, ["今汐说声骸很强"], limit=10)
        slow_update, slow_message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=4300
        )
        slow_task = asyncio.create_task(term_command(slow_update, context))
        await asyncio.wait_for(started.wait(), timeout=0.2)

        context.args = ["off"]
        public_update, public_message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=4301
        )
        await asyncio.wait_for(public_command(public_update, context), timeout=0.05)
        assert public_message.replies == [(PUBLIC_DISABLED_NOTICE, 4301)]

        context.args = ["-2999"]
        authorize_update, authorize_message = fake_update(
            chat_id=1, chat_type="private", message_id=4302, user_id=11
        )
        await asyncio.wait_for(
            grant_command(authorize_update, context), timeout=0.05
        )
        assert authorize_message.replies == [
            ("已授权\nGranted.\n\n-2999", None)
        ]

        context.args = ["-2999"]
        revoke_update, revoke_message = fake_update(
            chat_id=1, chat_type="private", message_id=4303, user_id=11
        )
        await asyncio.wait_for(revoke_command(revoke_update, context), timeout=0.05)
        assert revoke_message.replies == [
            ("已撤销并退出 / Revoked and left chat_id=-2999", None)
        ]
        assert context.bot.left_chats == [-2999]

        context.args = ["声骸"]
        other_update, other_message = fake_update(
            chat_id=-2002, chat_type="supergroup", message_id=4304
        )
        await asyncio.wait_for(term_command(other_update, context), timeout=0.05)
        assert other_message.replies == [("Echo", 4304)]
        assert slow_message.replies == []

        release.set()
        await asyncio.wait_for(slow_task, timeout=0.2)
        assert slow_message.replies == [("Jinhsi says Echo", 4300)]

    asyncio.run(run())


def test_public_mode_reply_is_skipped_if_public_closes_during_llm(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_call(
            _locked_text,
            locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
            references=(),
        ):
            started.set()
            await release.wait()
            return (
                f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} says "
                f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
            )

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = fake_context(
            sample_db,
            ["今汐说声骸很强"],
            member_status="administrator",
            member_overrides={(-2001, 22): "member"},
        )
        settings = context.application.bot_data[CHAT_SETTINGS_KEY]
        settings.set_public(-2001, True)
        slow_update, slow_message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=4310, user_id=22
        )
        slow_task = asyncio.create_task(term_command(slow_update, context))
        await asyncio.wait_for(started.wait(), timeout=0.2)

        context.args = ["off"]
        public_update, public_message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=4311, user_id=11
        )
        await asyncio.wait_for(public_command(public_update, context), timeout=0.05)
        assert public_message.replies == [(PUBLIC_DISABLED_NOTICE, 4311)]

        release.set()
        await asyncio.wait_for(slow_task, timeout=0.2)

        assert slow_message.replies == []

    asyncio.run(run())


def test_group_reply_is_skipped_if_chat_revoked_during_llm(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_call(
            _locked_text,
            locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
            references=(),
        ):
            started.set()
            await release.wait()
            return (
                f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} says "
                f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
            )

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        context = fake_context(sample_db, ["今汐说声骸很强"])
        slow_update, slow_message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=4320, user_id=11
        )
        slow_task = asyncio.create_task(term_command(slow_update, context))
        await asyncio.wait_for(started.wait(), timeout=0.2)

        context.args = ["-2001"]
        revoke_update, revoke_message = fake_update(
            chat_id=1, chat_type="private", message_id=4321, user_id=11
        )
        await asyncio.wait_for(revoke_command(revoke_update, context), timeout=0.05)
        assert revoke_message.replies == [
            ("已撤销并退出 / Revoked and left chat_id=-2001", None)
        ]

        release.set()
        await asyncio.wait_for(slow_task, timeout=0.2)

        assert slow_message.replies == []

    asyncio.run(run())


def test_llm_concurrency_limit_bounds_inflight_calls(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def run():
        in_flight = 0
        max_in_flight = 0

        async def fake_call(
            _locked_text,
            locks,
            html_mode=False,
            to_chinese=False,
            timeout_seconds=30.0,
            transport=None,
            references=(),
        ):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
                return (
                    f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=to_chinese)} says "
                    f"{placeholder_for(locks, 'Echo', references, to_chinese=to_chinese)}"
                )
            finally:
                in_flight -= 1

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
        translator = SentenceTranslator(sample_db, llm_max_concurrency=2)
        service = TermService(sample_db)
        tasks = [
            asyncio.create_task(
                translate_query_async(service, translator, "今汐说声骸很强")
            )
            for _ in range(5)
        ]

        results = await asyncio.gather(*tasks)

        assert results == ["Jinhsi says Echo"] * 5
        assert max_in_flight == 2

    asyncio.run(run())


def test_rate_limit_is_per_chat(sample_db):
    context = fake_context(sample_db, ["声骸"], limit=10, member_status="member")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    settings.set_public(-2001, True)
    settings.set_public(-2002, True)
    for idx in range(10):
        update, _message = fake_update(
            chat_id=-2001,
            chat_type="supergroup",
            message_id=600 + idx,
            user_id=42,
        )
        asyncio.run(term_command(update, context))

    throttled_update, throttled_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=700, user_id=42
    )
    asyncio.run(term_command(throttled_update, context))

    other_update, other_message = fake_update(
        chat_id=-2002, chat_type="supergroup", message_id=800, user_id=43
    )
    asyncio.run(term_command(other_update, context))

    assert throttled_message.replies == [(THROTTLE_NOTICE, 700)]
    assert throttled_message.replies[0][0] == (
        "本群消息过于频繁，请一分钟后再试。\n"
        "Rate limit reached for this chat. Try again in a minute."
    )
    assert other_message.replies == [("Echo", 800)]


def test_group_owner_bypasses_member_status_rate_limit_and_2000_input(
    monkeypatch, sample_db
):
    calls = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        calls.append(_locked_text)
        return f"owner-chunk-{len(calls)}"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    context = fake_context(sample_db, ["测" * 2500], limit=1, member_status="member")

    long_update, long_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=601, user_id=11
    )
    asyncio.run(term_command(long_update, context))

    context.args = ["声骸"]
    second_update, second_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=602, user_id=11
    )
    asyncio.run(term_command(second_update, context))

    assert long_message.replies == [("owner-chunk-1\nowner-chunk-2", 601)]
    assert second_message.replies == [("Echo", 602)]
    assert len(calls) == 2
    assert context.bot.member_calls == []


def test_group_admin_bypasses_rate_limit_and_2000_input(monkeypatch, sample_db):
    calls = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        calls.append(_locked_text)
        return f"admin-chunk-{len(calls)}"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    context = fake_context(sample_db, ["测" * 2500], limit=1, member_status="administrator")

    long_update, long_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=603, user_id=42
    )
    asyncio.run(term_command(long_update, context))

    context.args = ["声骸"]
    second_update, second_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=604, user_id=42
    )
    asyncio.run(term_command(second_update, context))

    assert long_message.replies == [("admin-chunk-1\nadmin-chunk-2", 603)]
    assert second_message.replies == [("Echo", 604)]
    assert len(calls) == 2


def test_public_member_keeps_2000_input_limit(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    context = fake_context(sample_db, ["测" * (LLM_INPUT_CHAR_LIMIT + 1)], member_status="member")
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=605, user_id=42
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [
        (
            f"Input is too long for translation ({LLM_INPUT_CHAR_LIMIT} character limit).",
            605,
        )
    ]
    assert calls == []


def test_commandhandler_accepts_bot_username(sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    app.bot._bot_user = User(id=123, first_name="WuWa", is_bot=True, username="WuWaTermBot")
    handler = next(
        handler
        for group in app.handlers.values()
        for handler in group
        if isinstance(handler, CommandHandler)
    )
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": -2001, "type": "supergroup", "title": "g"},
                "text": "/tr@WuWaTermBot 声骸",
                "entities": [{"type": "bot_command", "offset": 0, "length": 15}],
            },
        },
        app.bot,
    )

    assert handler.check_update(update)


def test_create_application_wires_llm_timeout(sample_db):
    app = create_application(
        "123:ABC", sample_db, config=BotConfig(llm_timeout_seconds=12.5)
    )

    translator = app.bot_data[TRANSLATOR_KEY]

    assert translator.llm_timeout_seconds == 12.5


def test_reply_retries_once_after_flood_wait(caplog, sample_db):
    update, message = fake_update(
        reply_raises=lambda _text, _kwargs, attempt: (
            RetryAfter(0) if attempt == 1 else None
        )
    )
    with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
        asyncio.run(reply_to_user(update, "hello"))

    assert message.replies == [("hello", None)]
    assert "flood wait" in caplog.text


def test_reply_flood_wait_gives_up_after_one_retry(sample_db):
    update, message = fake_update(
        reply_raises=lambda _text, _kwargs, _attempt: RetryAfter(0)
    )
    with pytest.raises(RetryAfter):
        asyncio.run(reply_to_user(update, "hello"))

    assert message.replies == []
    assert len(message.reply_kwargs) == 2  # one attempt + one retry


def test_group_admin_check_retries_transient_failure(monkeypatch, caplog, sample_db):
    calls = []

    async def flaky_get_chat_member(chat_id, user_id):
        calls.append((chat_id, user_id))
        if len(calls) == 1:
            raise NetworkError("blip")
        return SimpleNamespace(status="administrator")

    update, message = fake_update(chat_id=-2001, chat_type="supergroup", user_id=42)
    context = fake_context(sample_db, ["声骸"])
    context.bot.get_chat_member = flaky_get_chat_member

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    # attempt + retry for the actor pre-check, then the (fresh) delivery gate.
    assert len(calls) == 3
    assert message.replies == [("Echo", 101)]
    assert "get_chat_member failed error_type=NetworkError; retrying once" in caplog.text


def test_group_admin_check_fails_closed_after_retry(monkeypatch, caplog, sample_db):
    calls = []

    async def failing_get_chat_member(chat_id, user_id):
        calls.append((chat_id, user_id))
        raise NetworkError("blip")

    update, message = fake_update(chat_id=-2001, chat_type="supergroup", user_id=42)
    context = fake_context(sample_db, ["声骸"])
    context.bot.get_chat_member = failing_get_chat_member

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert len(calls) == 2
    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 101)]
    assert "get_chat_member retry failed error_type=NetworkError" in caplog.text


def test_direction_usage_suppression_leaves_log_trace(monkeypatch, caplog, sample_db):
    monkeypatch.setenv(REDACTION_SECRET_ENV, "usage-log-secret")
    update, message = fake_update(chat_id=990022)
    context = fake_context(sample_db, ["--to", "jp", "x"], limit=1)

    with caplog.at_level(logging.INFO, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))  # consumes the reject budget
        asyncio.run(term_command(update, context))  # suppressed -> log line

    assert message.replies == [(DIRECTION_USAGE_NOTICE, None)]
    assert "direction usage notice suppressed (reject budget) chat=id:" in caplog.text
    assert "990022" not in caplog.text


def test_silent_tr_rejection_leaves_log_trace(caplog, sample_db):
    config = BotConfig(owner_user_id=11, tr_reject_silent=True)
    update, message = fake_update(
        chat_id=-990011, chat_type="supergroup", user_id=99
    )
    context = fake_context(
        sample_db, ["声骸"], member_status="member", config=config
    )

    with caplog.at_level(logging.INFO, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert message.replies == []
    assert "tr rejected without reply chat=id:" in caplog.text
    assert "silent_flag=True" in caplog.text
    assert "-990011" not in caplog.text


def test_tr_html_invalid_api_response_gets_notice_without_plain_retry(
    monkeypatch, caplog, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls = []

    async def fake_call(*_args, **_kwargs):
        calls.append(True)
        raise LLMTranslationError(
            TRANSLATION_UNAVAILABLE_NOTICE, reason="invalid_api_response"
        )

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text="这是一个需要翻译的句子。",
        caption=None,
        text_html="<b>这是一个需要翻译的句子。</b>",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    # Envelope/gateway outages fail identically on a retry: one call, notice.
    assert len(calls) == 1
    assert message.replies == [(TRANSLATION_UNAVAILABLE_NOTICE, None)]
    assert "tr html translation failed reason=invalid_api_response" in caplog.text


def test_tr_flood_retry_aborts_when_gate_closes_during_wait(
    monkeypatch, caplog, sample_db
):
    member_statuses = ["administrator", "administrator", "member"]
    calls = []

    async def get_chat_member(chat_id, user_id):
        calls.append((chat_id, user_id))
        return SimpleNamespace(status=member_statuses[min(len(calls) - 1, 2)])

    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        user_id=42,
        reply_raises=lambda _text, _kwargs, attempt: (
            RetryAfter(0) if attempt == 1 else None
        ),
    )
    context = fake_context(sample_db, ["声骸"])
    context.bot.get_chat_member = get_chat_member

    with pytest.raises(RetryAfter):
        with caplog.at_level(logging.WARNING, logger="wuwaterm.channel"):
            asyncio.run(term_command(update, context))

    # The user was demoted during the flood wait: the retry never sends.
    assert message.replies == []
    assert "flood-wait retry aborted: delivery gate closed" in caplog.text


def test_tr_html_transport_failure_logs_reason(monkeypatch, caplog, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(*_args, **_kwargs):
        raise LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="timeout")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text="这是一个需要翻译的句子。",
        caption=None,
        text_html="<b>这是一个需要翻译的句子。</b>",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert message.replies == [(TRANSLATION_UNAVAILABLE_NOTICE, None)]
    assert "tr html translation failed reason=timeout" in caplog.text


def test_swallowed_llm_failure_logs_reason(monkeypatch, caplog, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(*_args, **_kwargs):
        raise LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="upstream")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    translator = SentenceTranslator(sample_db)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        result = asyncio.run(translator.translate_async("这是一个需要翻译的句子。"))

    assert result == TRANSLATION_UNAVAILABLE_NOTICE
    assert "llm translation failed reason=upstream" in caplog.text


def test_delivery_gate_skip_logs_redacted_ids(monkeypatch, caplog, sample_db):
    monkeypatch.setenv(REDACTION_SECRET_ENV, "gate-log-secret")
    calls = []

    async def demoting_get_chat_member(chat_id, user_id):
        calls.append((chat_id, user_id))
        # Admin for the pre-check, demoted before the delivery gate.
        status = "administrator" if len(calls) == 1 else "member"
        return SimpleNamespace(status=status)

    update, message = fake_update(
        chat_id=-887766, chat_type="supergroup", message_id=445566, user_id=42
    )
    context = fake_context(sample_db, ["声骸"], allowlist=(-887766,))
    context.bot.get_chat_member = demoting_get_chat_member

    with caplog.at_level(logging.INFO, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert message.replies == []
    assert "authorization changed before delivery chat=id:" in caplog.text
    assert "-887766" not in caplog.text
    assert "445566" not in caplog.text


def test_create_application_registers_error_handler(sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())

    assert _log_update_error in app.error_handlers


def test_update_error_handler_logs_type_and_frames_only(caplog):
    # A dynamic value: frames show source lines, so a literal would leak via
    # the raise site itself; runtime values only ever appear in the message.
    leaked_chat_id = int("-990011")
    try:
        raise KeyError(leaked_chat_id)
    except KeyError as exc:
        error = exc
    context = SimpleNamespace(error=error)

    with caplog.at_level(logging.ERROR, logger="wuwaterm.bot"):
        asyncio.run(_log_update_error(None, context))

    assert "update processing failed error_type=KeyError" in caplog.text
    assert "-990011" not in caplog.text  # exception message never logged
    assert "test_bot.py" in caplog.text  # traceback frames are logged


def test_update_error_handler_logs_redacted_message_id(monkeypatch, caplog):
    monkeypatch.setenv(REDACTION_SECRET_ENV, "error-log-secret")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        error = exc
    update = SimpleNamespace(effective_message=SimpleNamespace(message_id=778899))

    with caplog.at_level(logging.ERROR, logger="wuwaterm.bot"):
        asyncio.run(_log_update_error(update, SimpleNamespace(error=error)))

    assert "incoming_message=id:" in caplog.text
    assert "778899" not in caplog.text


def test_update_error_handler_tolerates_missing_error(caplog):
    with caplog.at_level(logging.ERROR, logger="wuwaterm.bot"):
        asyncio.run(_log_update_error(None, SimpleNamespace()))

    assert "update processing failed error_type=NoneType" in caplog.text


def test_create_application_shutdown_closes_translator(monkeypatch, sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    translator = app.bot_data[TRANSLATOR_KEY]
    closed = []

    async def fake_aclose():
        closed.append(True)

    monkeypatch.setattr(translator, "aclose", fake_aclose)

    asyncio.run(app.post_shutdown(app))

    assert closed == [True]


def test_create_application_shutdown_flushes_reply_index(monkeypatch, sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]
    flushed = []

    async def fake_aflush():
        flushed.append(True)

    monkeypatch.setattr(reply_index, "aflush", fake_aflush)

    asyncio.run(app.post_shutdown(app))

    assert flushed == [True]


def test_create_application_shutdown_flushes_even_if_translator_close_fails(
    monkeypatch, sample_db
):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    translator = app.bot_data[TRANSLATOR_KEY]
    reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]
    flushed = []

    async def failing_aclose():
        raise RuntimeError("transport close failed")

    async def fake_aflush():
        flushed.append(True)

    monkeypatch.setattr(translator, "aclose", failing_aclose)
    monkeypatch.setattr(reply_index, "aflush", fake_aflush)

    with pytest.raises(RuntimeError):
        asyncio.run(app.post_shutdown(app))

    assert flushed == [True]


def test_state_dir_keeps_runtime_state_writable_when_db_parent_is_read_only(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))
    os.chmod(data_dir, 0o555)
    try:
        app = create_application("123:ABC", sample_db, config=BotConfig())
        settings = app.bot_data[CHAT_SETTINGS_KEY]
        reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]

        assert app.bot_data[SERVICE_KEY].term_text("声骸") == "Echo"
        assert settings.allow(-2001) is True
        reply_index.remember_many(-2001, 4001, (5001,))
    finally:
        os.chmod(data_dir, 0o755)

    assert (state_dir / "chat_settings.json").exists()
    assert (state_dir / "chat_settings.json.lock").exists()
    assert (state_dir / "channel_replies.json").exists()
    assert not (data_dir / "chat_settings.json").exists()
    assert not (data_dir / "chat_settings.json.lock").exists()
    assert not (data_dir / "channel_replies.json").exists()


def test_state_dir_migrates_legacy_db_adjacent_state_once(monkeypatch, sample_db):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    (data_dir / "channel_replies.json").write_text(
        '{"version":1,"entries":[{"chat_id":-2001,"message_id":4001,'
        '"expires_at":9999999999,"reply_message_ids":[5001]}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))

    app = create_application("123:ABC", sample_db, config=BotConfig())
    settings = app.bot_data[CHAT_SETTINGS_KEY]
    reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]

    assert settings.is_allowed(-2001) is True
    assert settings.is_public(-2001) is True
    assert reply_index.get_many(-2001, 4001) == (5001,)
    assert (state_dir / "chat_settings.json").read_text(encoding="utf-8") == (
        data_dir / "chat_settings.json"
    ).read_text(encoding="utf-8")
    assert (state_dir / "channel_replies.json").read_text(encoding="utf-8") == (
        data_dir / "channel_replies.json"
    ).read_text(encoding="utf-8")

    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-3001":true},"allowed":[-3001]}',
        encoding="utf-8",
    )
    (data_dir / "channel_replies.json").write_text(
        '{"version":1,"entries":[{"chat_id":-3001,"message_id":6001,'
        '"expires_at":9999999999,"reply_message_ids":[7001]}]}',
        encoding="utf-8",
    )

    restarted = create_application("123:ABC", sample_db, config=BotConfig())
    restarted_settings = restarted.bot_data[CHAT_SETTINGS_KEY]
    restarted_replies = restarted.bot_data[CHANNEL_REPLY_INDEX_KEY]

    assert restarted_settings.is_allowed(-2001) is True
    assert restarted_settings.is_allowed(-3001) is False
    assert restarted_replies.get_many(-2001, 4001) == (5001,)
    assert restarted_replies.get_many(-3001, 6001) == ()


def test_state_dir_migrates_legacy_explicit_db_adjacent_paths(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    legacy_settings = data_dir / "chat_settings.json"
    legacy_replies = data_dir / "channel_replies.json"
    legacy_settings.write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    legacy_replies.write_text(
        '{"version":1,"entries":[{"chat_id":-2001,"message_id":4001,'
        '"expires_at":9999999999,"reply_message_ids":[5001]}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", str(legacy_settings))

    app = create_application(
        "123:ABC",
        sample_db,
        config=BotConfig(channel_reply_index_path=str(legacy_replies)),
    )
    settings = app.bot_data[CHAT_SETTINGS_KEY]
    reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]

    assert settings.path == (state_dir / "chat_settings.json").resolve(strict=False)
    assert reply_index.storage_path == state_dir / "channel_replies.json"
    assert settings.is_allowed(-2001) is True
    assert settings.is_public(-2001) is True
    assert reply_index.get_many(-2001, 4001) == (5001,)


def test_state_dir_migrates_legacy_when_explicit_paths_are_state_targets(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    (data_dir / "channel_replies.json").write_text(
        '{"version":1,"entries":[{"chat_id":-2001,"message_id":4001,'
        '"expires_at":9999999999,"reply_message_ids":[5001]}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))
    monkeypatch.setenv(
        "WUWATERM_SETTINGS_PATH", str(state_dir / "chat_settings.json")
    )

    app = create_application(
        "123:ABC",
        sample_db,
        config=BotConfig(
            channel_reply_index_path=str(state_dir / "channel_replies.json")
        ),
    )

    assert app.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is True
    assert app.bot_data[CHANNEL_REPLY_INDEX_KEY].get_many(-2001, 4001) == (5001,)


def test_state_dir_migration_never_overwrites_existing_state(monkeypatch, sample_db):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    state_dir.mkdir()
    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    existing = '{"public":{},"allowed":[]}'
    (state_dir / "chat_settings.json").write_text(existing, encoding="utf-8")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))

    app = create_application("123:ABC", sample_db, config=BotConfig())
    settings = app.bot_data[CHAT_SETTINGS_KEY]

    assert settings.is_allowed(-2001) is False
    assert (state_dir / "chat_settings.json").read_text(encoding="utf-8") == existing


def test_state_dir_invalid_existing_target_with_legacy_fails_fast(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    state_dir.mkdir()
    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    (state_dir / "chat_settings.json").write_text("", encoding="utf-8")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))

    with pytest.raises(StateMigrationError):
        create_application("123:ABC", sample_db, config=BotConfig())


def test_state_dir_invalid_chat_settings_schema_with_legacy_fails_fast(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    state_dir.mkdir()
    (data_dir / "chat_settings.json").write_text(
        '{"public":{"-2001":true},"allowed":[-2001]}',
        encoding="utf-8",
    )
    (state_dir / "chat_settings.json").write_text(
        '{"public":{"01":true},"allowed":[]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))

    with pytest.raises(StateMigrationError):
        create_application("123:ABC", sample_db, config=BotConfig())


def test_state_dir_invalid_channel_reply_target_with_legacy_fails_fast(
    monkeypatch, sample_db
):
    data_dir = sample_db.parent
    state_dir = data_dir.with_name(f"{data_dir.name}-state")
    state_dir.mkdir()
    (data_dir / "channel_replies.json").write_text(
        '{"version":1,"entries":[{"chat_id":-2001,"message_id":4001,'
        '"expires_at":9999999999,"reply_message_ids":[5001]}]}',
        encoding="utf-8",
    )
    (state_dir / "channel_replies.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))

    with pytest.raises(StateMigrationError):
        create_application("123:ABC", sample_db, config=BotConfig())


def test_handler_set_is_exactly_commands_plus_channel_listener(sample_db):
    """Pin evolution (deliberate): seven command handlers (/tr+/term,
    /sentence+/sent, /about, /status, /public, /grant, /revoke), exactly one passive
    channel listener (the linked-channel hard boundary), and exactly one
    MY_CHAT_MEMBER handler (the group-authorization gate)."""
    app = create_application(
        "123:ABC",
        sample_db,
        config=BotConfig(),
        chat_settings=ChatSettings(sample_db.parent / "chat_settings.json"),
    )
    handlers = [handler for group in app.handlers.values() for handler in group]
    command_handlers = [h for h in handlers if isinstance(h, CommandHandler)]
    message_handlers = [h for h in handlers if isinstance(h, MessageHandler)]

    chat_member_handlers = [h for h in handlers if isinstance(h, ChatMemberHandler)]
    assert len(handlers) == 9  # 7 command + 1 message + 1 chat-member
    assert len(command_handlers) == 7
    assert len(message_handlers) == 1
    assert len(chat_member_handlers) == 1
    # Repr verified against the installed PTB in this venv.
    assert str(message_handlers[0].filters) == (
        "<filters.IS_AUTOMATIC_FORWARD and filters.SenderChat.CHANNEL>"
    )


def test_data_plane_handlers_are_non_blocking_control_handlers_block(sample_db):
    app = create_application(
        "123:ABC",
        sample_db,
        config=BotConfig(),
        chat_settings=ChatSettings(sample_db.parent / "chat_settings.json"),
    )
    handlers = [handler for group in app.handlers.values() for handler in group]
    command_handlers = [h for h in handlers if isinstance(h, CommandHandler)]
    message_handlers = [h for h in handlers if isinstance(h, MessageHandler)]

    by_commands = {frozenset(handler.commands): handler for handler in command_handlers}

    assert by_commands[frozenset({"tr", "term"})].block is False
    assert by_commands[frozenset({"sentence", "sent"})].block is False
    assert message_handlers[0].block is False
    assert bool(by_commands[frozenset({"public"})].block) is True
    assert bool(by_commands[frozenset({"status"})].block) is True
    assert bool(by_commands[frozenset({"grant"})].block) is True
    assert frozenset({"authorize"}) not in by_commands
    assert bool(by_commands[frozenset({"revoke"})].block) is True


def test_ordinary_member_message_never_triggers_any_handler(sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    app.bot._bot_user = User(id=123, first_name="WuWa", is_bot=True, username="WuWaTermBot")
    update = Update.de_json(
        {
            "update_id": 2,
            "message": {
                "message_id": 50,
                "date": 1,
                "chat": {"id": -2001, "type": "supergroup", "title": "g"},
                "from": {"id": 42, "is_bot": False, "first_name": "member"},
                "text": "今汐说声骸很强",
            },
        },
        app.bot,
    )

    matched = [
        handler
        for group in app.handlers.values()
        for handler in group
        if handler.check_update(update)
    ]

    assert matched == []


def test_automatic_channel_forward_matches_only_the_message_handler(sample_db):
    app = create_application("123:ABC", sample_db, config=BotConfig())
    app.bot._bot_user = User(id=123, first_name="WuWa", is_bot=True, username="WuWaTermBot")
    update = Update.de_json(
        {
            "update_id": 3,
            "message": {
                "message_id": 51,
                "date": 1,
                "chat": {"id": -2001, "type": "supergroup", "title": "g"},
                "from": {"id": 777000, "is_bot": False, "first_name": "Telegram"},
                "sender_chat": {"id": -1003001, "type": "channel", "title": "c"},
                "is_automatic_forward": True,
                "text": "公告：今汐上线",
            },
        },
        app.bot,
    )

    matched = [
        handler
        for group in app.handlers.values()
        for handler in group
        if handler.check_update(update)
    ]

    assert len(matched) == 1
    assert isinstance(matched[0], MessageHandler)


def test_group_tr_member_gets_one_line_rejection_and_zero_llm(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=900, user_id=42
    )
    context = fake_context(sample_db, ["今汐说声骸很强"], member_status="member")

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 900)]
    assert message.replies[0][0] == "仅群管理员可用 /tr\nOnly group admins can use /tr"
    assert calls == []
    assert context.bot.member_calls == [(-2001, 42)]


@pytest.mark.parametrize("status", ["restricted", "left", "kicked"])
def test_group_tr_non_admin_statuses_are_rejected(status, sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=901, user_id=42
    )
    context = fake_context(sample_db, ["声骸"], member_status=status)

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 901)]


@pytest.mark.parametrize("status", ["creator", "administrator"])
def test_group_tr_admin_statuses_translate(status, sample_db):
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=902)
    context = fake_context(sample_db, ["声骸"], member_status=status)

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", 902)]


def test_private_tr_owner_translates_without_member_lookup(sample_db):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["声骸"], member_status="member")

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", None)]
    assert context.bot.member_calls == []


def test_group_tr_anonymous_admin_translates_without_member_lookup(sample_db):
    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=903,
        user_id=None,
        sender_chat_id=-2001,
    )
    context = fake_context(sample_db, ["声骸"], member_status="member")

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", 903)]
    assert context.bot.member_calls == []


def test_group_tr_linked_channel_sender_is_rejected(sample_db):
    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=904,
        user_id=None,
        sender_chat_id=-3001,
    )
    context = fake_context(sample_db, ["声骸"], member_status="administrator")

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 904)]
    assert context.bot.member_calls == []


def test_group_tr_member_verdict_is_cached_across_calls(sample_db):
    context = fake_context(sample_db, ["声骸"], member_status="member")
    first_update, first_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=905, user_id=42
    )
    second_update, second_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=906, user_id=42
    )

    asyncio.run(term_command(first_update, context))
    asyncio.run(term_command(second_update, context))

    assert first_message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 905)]
    assert second_message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 906)]
    assert context.bot.member_calls == [(-2001, 42)]


def test_admin_status_cache_expires_after_ttl():
    cache = AdminStatusCache(ttl_seconds=300.0)
    cache.put(-2001, 11, False, now=0.0)
    cache.put(-2001, 12, True, now=0.0)

    assert cache.get(-2001, 11, now=299.9) is False
    assert cache.get(-2001, 12, now=299.9) is True
    assert cache.get(-2001, 11, now=300.0) is None
    assert cache.get(-2001, 99, now=0.0) is None


def test_admin_status_cache_enforces_stable_capacity_and_prunes_expired():
    cache = AdminStatusCache(ttl_seconds=10.0, max_entries=2)
    cache.put(2, 1, False, now=0.0)
    cache.put(1, 2, True, now=0.0)
    cache.put(3, 1, True, now=0.0)

    assert cache.entry_count(now=0.0) == 2
    assert cache.get(1, 2, now=0.0) is None
    assert cache.get(2, 1, now=0.0) is False
    assert cache.get(3, 1, now=0.0) is True

    cache.put(4, 1, True, now=10.0)
    assert cache.entry_count(now=10.0) == 1
    assert cache.get(4, 1, now=10.0) is True


def test_group_tr_silent_flag_suppresses_rejection_reply(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=907, user_id=42
    )
    context = fake_context(
        sample_db,
        ["今汐说声骸很强"],
        member_status="member",
        config=BotConfig(rate_limit_per_minute=10, tr_reject_silent=True, owner_user_id=11),
    )

    asyncio.run(term_command(update, context))

    assert message.replies == []
    assert calls == []


def test_group_tr_rejection_text_is_configurable(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=908, user_id=42
    )
    context = fake_context(
        sample_db,
        ["声骸"],
        member_status="member",
        config=BotConfig(rate_limit_per_minute=10, group_tr_reject_text="管理员专用。"),
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [("管理员专用。", 908)]


def test_group_tr_reject_replies_are_capped_then_silent(sample_db):
    # Rejections ride their own per-chat budget; beyond it they go silent and
    # never emit THROTTLE_NOTICE (which would leak throttle state to non-admins).
    context = fake_context(sample_db, ["声骸"], limit=2, member_status="member")
    replies = []
    for idx in range(3):
        update, message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=909 + idx, user_id=42
        )
        asyncio.run(term_command(update, context))
        replies.append(message.replies)

    assert replies[0] == [(DEFAULT_GROUP_TR_REJECT_TEXT, 909)]
    assert replies[1] == [(DEFAULT_GROUP_TR_REJECT_TEXT, 910)]
    assert replies[2] == []
    assert context.bot.member_calls == [(-2001, 42)]


def test_group_nonadmin_spam_does_not_starve_admin(sample_db):
    # A non-admin floods rejected /tr past the limit; an admin in the SAME chat
    # must still translate. On the old throttle-before-auth code the admin would
    # have been throttled by the non-admin's rejections draining the one bucket.
    context = fake_context(
        sample_db,
        ["声骸"],
        limit=2,
        member_status="administrator",
        member_overrides={(-2001, 22): "member"},
    )
    spam_replies = []
    for idx in range(3):  # limit + 1: drains the reject budget
        update, message = fake_update(
            chat_id=-2001, chat_type="supergroup", message_id=950 + idx, user_id=22
        )
        asyncio.run(term_command(update, context))
        spam_replies.append(message.replies)

    # Reject budget (=2) exhausted by the 3rd call: silent, never THROTTLE_NOTICE.
    assert spam_replies[2] == []

    admin_update, admin_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=960, user_id=11
    )
    asyncio.run(term_command(admin_update, context))
    assert admin_message.replies == [("Echo", 960)]


def test_group_tr_member_lookup_failure_fails_closed_and_uncached(sample_db):
    context = fake_context(
        sample_db,
        ["声骸"],
        member_overrides={(-2001, 42): TelegramError("temporarily unavailable")},
    )
    first_update, first_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=912, user_id=42
    )
    second_update, second_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=913, user_id=42
    )

    asyncio.run(term_command(first_update, context))
    asyncio.run(term_command(second_update, context))

    assert first_message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 912)]
    assert second_message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 913)]
    assert context.bot.member_calls == [(-2001, 42), (-2001, 42)]


def test_group_sentence_member_gets_one_line_rejection_and_zero_llm(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=914, user_id=42
    )
    context = fake_context(sample_db, ["今汐说声骸很强"], member_status="member")

    asyncio.run(sentence_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 914)]
    assert calls == []
    assert context.bot.member_calls == [(-2001, 42)]


@pytest.mark.parametrize("status", ["creator", "administrator"])
def test_group_sentence_admin_statuses_translate(status, monkeypatch, sample_db):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=915)
    context = fake_context(sample_db, ["今汐装备了声骸"], member_status=status)

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("Jinhsi装备了Echo", 915)]


def test_group_sentence_anonymous_admin_translates_without_member_lookup(
    monkeypatch, sample_db
):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=916,
        user_id=None,
        sender_chat_id=-2001,
    )
    context = fake_context(sample_db, ["今汐装备了声骸"], member_status="member")

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("Jinhsi装备了Echo", 916)]
    assert context.bot.member_calls == []


def test_private_sentence_owner_translates_without_member_lookup(monkeypatch, sample_db):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["今汐装备了声骸"], member_status="member")

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("Jinhsi装备了Echo", None)]
    assert context.bot.member_calls == []


def test_private_sentence_rejects_everyone_when_owner_unset(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(
        sample_db,
        ["今汐说声骸很强"],
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=None),
    )

    asyncio.run(sentence_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert calls == []
    assert context.bot.member_calls == []


@pytest.mark.parametrize("user_id", [22, 11])
def test_channel_sentence_is_rejected_even_for_owner(user_id, sample_db):
    update, message = fake_update(
        chat_id=-5005, chat_type="channel", message_id=917, user_id=user_id
    )
    context = fake_context(sample_db, ["声骸"])

    asyncio.run(sentence_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert context.bot.member_calls == []


def test_private_sentence_reject_replies_are_capped_then_silent(sample_db):
    context = fake_context(
        sample_db,
        ["声骸"],
        config=BotConfig(rate_limit_per_minute=2, owner_user_id=11),
    )
    replies = []
    for idx in range(3):
        update, message = fake_update(
            chat_id=2, chat_type="private", user_id=22, message_id=940 + idx
        )
        asyncio.run(sentence_command(update, context))
        replies.append(message.replies)

    assert replies[0] == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert replies[1] == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert replies[2] == []
    assert context.bot.member_calls == []


def test_private_tr_stranger_gets_one_line_rejection_and_zero_llm(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(chat_id=2, chat_type="private", user_id=22)
    context = fake_context(sample_db, ["今汐说声骸很强"])

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert message.replies[0][0] == (
        "此 bot 仅限群内由管理员使用\n"
        "This bot can only be used by admins inside a group."
    )
    assert calls == []
    assert context.bot.member_calls == []


def test_private_tr_rejects_everyone_when_owner_unset(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(
        sample_db,
        ["今汐说声骸很强"],
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=None),
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert calls == []
    assert context.bot.member_calls == []


@pytest.mark.parametrize("raw", [None, "", "   ", "not-an-int"])
def test_from_env_owner_missing_or_invalid_warns_once_without_digits(
    raw, monkeypatch, caplog
):
    monkeypatch.delenv("WUWATERM_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("WUWATERM_GROUP_TR_REJECT_TEXT", raising=False)
    monkeypatch.delenv("WUWATERM_PRIVATE_TR_REJECT_TEXT", raising=False)
    monkeypatch.delenv("WUWATERM_TR_REJECT_SILENT", raising=False)
    if raw is None:
        monkeypatch.delenv("OWNER_USER_ID", raising=False)
    else:
        monkeypatch.setenv("OWNER_USER_ID", raw)

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        config = BotConfig.from_env()

    assert config.owner_user_id is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "private /tr will reject everyone" in warnings[0].getMessage()
    assert re.search(r"\d", warnings[0].getMessage()) is None


def test_from_env_owner_valid_int_no_warning(monkeypatch, caplog):
    monkeypatch.delenv("WUWATERM_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.setenv("OWNER_USER_ID", " 654321 ")

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        config = BotConfig.from_env()

    assert config.owner_user_id == 654321
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_default_llm_timeout_has_gateway_margin(monkeypatch):
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.delenv("WUWATERM_LLM_TIMEOUT_SECONDS", raising=False)

    config = BotConfig.from_env()

    assert BotConfig().llm_timeout_seconds == 45.0
    assert config.llm_timeout_seconds == 45.0


def test_from_env_parses_llm_timeout_and_concurrency(monkeypatch):
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv("WUWATERM_LLM_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("WUWATERM_LLM_MAX_CONCURRENCY", "2")

    config = BotConfig.from_env()

    assert config.llm_timeout_seconds == 7.5
    assert config.llm_max_concurrency == 2


def test_long_query_never_enters_fuzzy_table_scan(sample_db, monkeypatch):
    service = TermService(sample_db)
    translator = SentenceTranslator(sample_db)

    def fail_fuzzy(*_args, **_kwargs):
        raise AssertionError("long translation input must not run fuzzy lookup")

    monkeypatch.setattr(service, "_fuzzy", fail_fuzzy)

    result = asyncio.run(
        translate_query_async(service, translator, "中" * (LLM_INPUT_CHAR_LIMIT + 1))
    )

    assert "too long" in result


def test_llm_config_requires_complete_stripped_http_values(monkeypatch):
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", " https://gateway.example/v1 ")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", " secret ")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", " model ")

    _validate_llm_config_env()


@pytest.mark.parametrize(
    "values",
    [
        {"WUWATERM_OPENAI_BASE_URL": "https://gateway.example/v1"},
        {
            "WUWATERM_OPENAI_BASE_URL": "gateway.example/v1",
            "WUWATERM_OPENAI_API_KEY": "secret",
            "WUWATERM_OPENAI_MODEL": "model",
        },
        {
            "WUWATERM_OPENAI_BASE_URL": "   ",
            "WUWATERM_OPENAI_API_KEY": "secret",
            "WUWATERM_OPENAI_MODEL": "model",
        },
    ],
)
def test_llm_config_rejects_partial_or_invalid_without_leaking_values(
    monkeypatch, values
):
    clear_bot_config_env(monkeypatch)
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(BotConfigError) as caught:
        _validate_llm_config_env()

    formatted = str(caught.value)
    assert "secret" not in formatted
    assert "gateway.example/v1" not in formatted


def test_from_env_defaults_match_programmatic_defaults(monkeypatch):
    clear_bot_config_env(monkeypatch)

    assert BotConfig.from_env() == BotConfig()


@pytest.mark.parametrize(
    ("name", "attribute"),
    [
        ("WUWATERM_RATE_LIMIT_PER_MINUTE", "rate_limit_per_minute"),
        ("WUWATERM_CHANNEL_MIN_CJK", "channel_min_cjk"),
        ("WUWATERM_CHANNEL_MIN_LATIN", "channel_min_latin"),
        ("WUWATERM_CHANNEL_TEXT_LIMIT", "channel_text_limit"),
        ("WUWATERM_CHANNEL_CAPTION_LIMIT", "channel_caption_limit"),
        ("WUWATERM_CHANNEL_MAX_AGE_SECONDS", "channel_max_age_seconds"),
        ("WUWATERM_CHANNEL_MAX_PENDING", "channel_max_pending"),
        (
            "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE",
            "channel_llm_calls_per_minute",
        ),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "llm_timeout_seconds"),
        ("WUWATERM_LLM_MAX_CONCURRENCY", "llm_max_concurrency"),
    ],
)
def test_from_env_empty_numeric_values_use_defaults(monkeypatch, name, attribute):
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv(name, "  ")

    config = BotConfig.from_env()

    assert getattr(config, attribute) == getattr(BotConfig(), attribute)


@pytest.mark.parametrize(
    ("name", "attribute", "lower", "upper"),
    [
        ("WUWATERM_RATE_LIMIT_PER_MINUTE", "rate_limit_per_minute", "1", "10000"),
        ("WUWATERM_CHANNEL_MIN_CJK", "channel_min_cjk", "1", "4096"),
        ("WUWATERM_CHANNEL_MIN_LATIN", "channel_min_latin", "1", "4096"),
        ("WUWATERM_CHANNEL_TEXT_LIMIT", "channel_text_limit", "1", "4096"),
        ("WUWATERM_CHANNEL_CAPTION_LIMIT", "channel_caption_limit", "1", "1024"),
        ("WUWATERM_CHANNEL_MAX_AGE_SECONDS", "channel_max_age_seconds", "1", "2592000"),
        ("WUWATERM_CHANNEL_MAX_PENDING", "channel_max_pending", "0", "1024"),
        (
            "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE",
            "channel_llm_calls_per_minute",
            "1",
            "10000",
        ),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "llm_timeout_seconds", "0.1", "300"),
        ("WUWATERM_LLM_MAX_CONCURRENCY", "llm_max_concurrency", "1", "64"),
    ],
)
def test_from_env_accepts_numeric_boundaries(
    monkeypatch, name, attribute, lower, upper
):
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("OWNER_USER_ID", "11")

    for raw in (lower, upper):
        monkeypatch.setenv(name, f" {raw} ")
        assert getattr(BotConfig.from_env(), attribute) == float(raw)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("WUWATERM_RATE_LIMIT_PER_MINUTE", "0"),
        ("WUWATERM_RATE_LIMIT_PER_MINUTE", "10001"),
        ("WUWATERM_RATE_LIMIT_PER_MINUTE", "1.5"),
        ("WUWATERM_CHANNEL_MIN_CJK", "0"),
        ("WUWATERM_CHANNEL_MIN_LATIN", "4097"),
        ("WUWATERM_CHANNEL_TEXT_LIMIT", "4097"),
        ("WUWATERM_CHANNEL_CAPTION_LIMIT", "1025"),
        ("WUWATERM_CHANNEL_MAX_AGE_SECONDS", "2592001"),
        ("WUWATERM_CHANNEL_MAX_PENDING", "-1"),
        ("WUWATERM_CHANNEL_MAX_PENDING", "1025"),
        ("WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE", "0"),
        ("WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE", "10001"),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "nan"),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "inf"),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "-inf"),
        ("WUWATERM_LLM_TIMEOUT_SECONDS", "300.1"),
        ("WUWATERM_LLM_MAX_CONCURRENCY", "0"),
        ("WUWATERM_LLM_MAX_CONCURRENCY", "65"),
    ],
)
def test_from_env_rejects_invalid_numeric_values(monkeypatch, name, raw):
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv(name, raw)

    with pytest.raises(BotConfigError) as caught:
        BotConfig.from_env()

    assert name in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        (" off ", False),
    ],
)
def test_from_env_accepts_explicit_boolean_tokens(monkeypatch, raw, expected):
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv("WUWATERM_TR_REJECT_SILENT", raw)
    monkeypatch.setenv("WUWATERM_CHANNEL_AUTOTRANSLATE", raw)

    config = BotConfig.from_env()

    assert config.tr_reject_silent is expected
    assert config.channel_autotranslate is expected


@pytest.mark.parametrize(
    "name",
    [
        "WUWATERM_TR_REJECT_SILENT",
        "WUWATERM_CHANNEL_AUTOTRANSLATE",
    ],
)
def test_from_env_invalid_value_does_not_leak_raw_marker(monkeypatch, name):
    marker = "enabled-sensitive-marker"
    clear_bot_config_env(monkeypatch)
    monkeypatch.setenv("OWNER_USER_ID", "11")
    monkeypatch.setenv(name, marker)

    with pytest.raises(BotConfigError) as caught:
        BotConfig.from_env()

    formatted = "".join(
        traceback.format_exception(caught.type, caught.value, caught.tb)
    )
    assert name in formatted
    assert marker not in formatted


def test_from_env_reject_overrides_win_verbatim_and_are_not_auto_bilingual(monkeypatch):
    monkeypatch.delenv("WUWATERM_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.setenv("WUWATERM_GROUP_TR_REJECT_TEXT", "管理员专用。")
    monkeypatch.setenv("WUWATERM_PRIVATE_TR_REJECT_TEXT", "老板专用。")

    config = BotConfig.from_env()

    # Owner-set override wins exactly as written; the bot never appends a second
    # (English) line to it — owner text stays the owner's responsibility.
    assert config.group_tr_reject_text == "管理员专用。"
    assert config.private_tr_reject_text == "老板专用。"
    assert "\n" not in config.group_tr_reject_text
    assert "\n" not in config.private_tr_reject_text


@pytest.mark.parametrize("user_id", [22, 11])
def test_channel_chat_is_rejected_even_for_owner(user_id, sample_db):
    update, message = fake_update(
        chat_id=-5005, chat_type="channel", message_id=920, user_id=user_id
    )
    context = fake_context(sample_db, ["声骸"])

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert context.bot.member_calls == []


def test_private_tr_silent_flag_suppresses_rejection_reply(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(chat_id=2, chat_type="private", user_id=22)
    context = fake_context(
        sample_db,
        ["今汐说声骸很强"],
        config=BotConfig(rate_limit_per_minute=10, tr_reject_silent=True, owner_user_id=11),
    )

    asyncio.run(term_command(update, context))

    assert message.replies == []
    assert calls == []


def test_private_tr_rejection_text_is_configurable(sample_db):
    update, message = fake_update(chat_id=2, chat_type="private", user_id=22)
    context = fake_context(
        sample_db,
        ["声骸"],
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=11, private_tr_reject_text="老板专用。"),
    )

    asyncio.run(term_command(update, context))

    assert message.replies == [("老板专用。", None)]


def test_private_tr_reject_replies_are_capped_then_silent(sample_db):
    context = fake_context(
        sample_db,
        ["声骸"],
        config=BotConfig(rate_limit_per_minute=2, owner_user_id=11),
    )
    replies = []
    for idx in range(3):
        update, message = fake_update(chat_id=2, chat_type="private", user_id=22, message_id=930 + idx)
        asyncio.run(term_command(update, context))
        replies.append(message.replies)

    assert replies[0] == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert replies[1] == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert replies[2] == []
    assert context.bot.member_calls == []


def test_private_sentence_stranger_gets_one_line_rejection_and_zero_llm(
    monkeypatch, sample_db
):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update(chat_id=2, chat_type="private", user_id=22)
    context = fake_context(sample_db, ["今汐说声骸很强"])

    asyncio.run(sentence_command(update, context))

    assert message.replies == [(DEFAULT_PRIVATE_TR_REJECT_TEXT, None)]
    assert calls == []
    assert context.bot.member_calls == []


def test_member_lookup_failure_log_carries_no_chat_or_user_ids(caplog, sample_db):
    chat_id, user_id = -778899001, 445566
    context = fake_context(
        sample_db,
        ["声骸"],
        member_overrides={(chat_id, user_id): TelegramError("temporarily unavailable")},
        allowlist=(chat_id,),
    )
    update, message = fake_update(
        chat_id=chat_id, chat_type="supergroup", message_id=321, user_id=user_id
    )

    with caplog.at_level(logging.WARNING, logger="wuwaterm.bot"):
        asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 321)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "get_chat_member failed" in warnings[0].getMessage()
    assert "445566" not in caplog.text
    assert "778899001" not in caplog.text


# --- reply-target translation (reply to a message + a bare command) ---


def test_tr_reply_to_chinese_message_translates_replied_content(sample_db):
    # Bare /tr (no inline text) while replying to a Chinese message translates
    # that replied message's content. (The screenshot scenario.)
    update, message = fake_update(reply_to=SimpleNamespace(text="声骸", caption=None))
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", None)]


def test_tr_reply_to_formatted_message_preserves_html(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    calls = []

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        calls.append((html_mode, to_chinese))
        jinhsi = placeholder_for(locks, "Jinhsi", references, to_chinese=to_chinese)
        echo = placeholder_for(locks, "Echo", references, to_chinese=to_chinese)
        return html_with_segments(
            locked_text, "", jinhsi, " says ", echo, " is strong"
        )

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text="今汐说声骸很强",
        caption=None,
        text_html='<b>今汐</b>说<a href="https://example.com">声骸</a>很强',
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert calls == [(True, False)]
    assert message.replies == [
        (
            '<b>Jinhsi</b> says '
            '<a href="https://example.com">Echo</a> is strong',
            None,
        )
    ]
    assert message.reply_kwargs == [{"do_quote": False, "parse_mode": "HTML"}]


def test_sentence_reply_to_formatted_caption_preserves_html(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_call(
        locked_text,
        locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
        references=(),
    ):
        assert html_mode is True
        assert to_chinese is False
        jinhsi = placeholder_for(locks, "Jinhsi", references, to_chinese=to_chinese)
        return html_with_segments(locked_text, "", jinhsi, " arrives")

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text=None,
        caption="今汐登场",
        text_html=None,
        caption_html="<tg-spoiler>今汐</tg-spoiler>登场",
        caption_entities=[SimpleNamespace(type="spoiler")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("<tg-spoiler>Jinhsi</tg-spoiler> arrives", None)]
    assert message.reply_kwargs == [{"do_quote": False, "parse_mode": "HTML"}]


def test_formatted_reply_exact_dictionary_hit_stays_plain(sample_db):
    replied = SimpleNamespace(
        text="声骸",
        caption=None,
        text_html="<b>声骸</b>",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", None)]
    assert message.reply_kwargs == [{"do_quote": False}]


def test_formatted_reply_without_llm_uses_plain_fallback(monkeypatch, sample_db):
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    replied = SimpleNamespace(
        text="不存在词条",
        caption=None,
        text_html="<b>不存在词条</b>",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [
        (
            "不存在词条\n\n"
            "(词典外,机器直译)\n"
            "(Not in official dictionary; machine-translated)",
            None,
        )
    ]
    assert message.reply_kwargs == [{"do_quote": False}]


def test_formatted_reply_structural_drift_falls_back_to_plain(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    modes = []

    async def fake_call(
        _locked_text,
        _locks,
        html_mode=False,
        to_chinese=False,
        timeout_seconds=45.0,
        transport=None,
    ):
        modes.append(html_mode)
        if html_mode:
            # Broken structure: the HTML placeholders are gone entirely.
            return "<script>translated</script>"
        return "This sentence needs translating."

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    replied = SimpleNamespace(
        text="这是一个需要翻译的句子。",
        caption=None,
        text_html="<b>这是一个需要翻译的句子。</b>",
        caption_html=None,
        entities=[SimpleNamespace(type="bold")],
    )
    update, message = fake_update(reply_to=replied)
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    # The broken HTML attempt is followed by a plain retry whose output is
    # delivered, instead of replying with the unavailable notice.
    assert modes == [True, False]
    assert message.replies == [("This sentence needs translating.", None)]
    assert message.reply_kwargs == [{"do_quote": False}]


def make_inline_message(text: str, entities):
    """Command message carrying its own formatting entities (inline /tr)."""
    return SimpleNamespace(
        text=text,
        entities=list(entities),
        date=datetime(2026, 7, 22, tzinfo=timezone.utc),
        chat=Chat(id=1, type="private"),
    )


def test_inline_html_renders_bold_tail():
    message = make_inline_message(
        "/tr 这是一个需要翻译的句子。",
        [MessageEntity(type=MessageEntity.BOLD, offset=4, length=12)],
    )
    assert (
        _inline_translation_html(message, "这是一个需要翻译的句子。")
        == "<b>这是一个需要翻译的句子。</b>"
    )


def test_inline_html_shifts_utf16_offsets_after_emoji():
    # "/tr " = 4 UTF-16 units, "👋" = 2 units, " " = 1 unit -> bold at 7.
    message = make_inline_message(
        "/tr 👋 加粗文字",
        [MessageEntity(type=MessageEntity.BOLD, offset=7, length=4)],
    )
    assert _inline_translation_html(message, "👋 加粗文字") == "👋 <b>加粗文字</b>"


def test_inline_html_skips_leading_direction_flag():
    message = make_inline_message(
        "/tr --to zh bold words",
        [MessageEntity(type=MessageEntity.BOLD, offset=12, length=10)],
    )
    assert _inline_translation_html(message, "bold words") == "<b>bold words</b>"


def test_inline_html_skips_uppercase_direction_value():
    # _parse_translation_args lowercases the value, so "--to EN" is a valid
    # command; the prefix stripper must accept it too or formatting is lost.
    message = make_inline_message(
        "/tr --to EN bold words",
        [MessageEntity(type=MessageEntity.BOLD, offset=12, length=10)],
    )
    assert _inline_translation_html(message, "bold words") == "<b>bold words</b>"


def test_inline_html_midtext_flag_is_literal_text():
    # Direction flags are leading-only: a mid-text "--to zh" stays literal
    # text, the raw tail matches the parsed text, and formatting survives.
    message = make_inline_message(
        "/tr bold --to zh words",
        [MessageEntity(type=MessageEntity.BOLD, offset=4, length=4)],
    )
    assert (
        _inline_translation_html(message, "bold --to zh words")
        == "<b>bold</b> --to zh words"
    )


def test_inline_html_whitespace_mismatch_falls_back_to_plain():
    # The args tokenizer collapses runs of whitespace, so the raw tail no
    # longer matches the parsed text; formatting is dropped rather than
    # guessed against a shifted offset base.
    message = make_inline_message(
        "/tr bold  words",
        [MessageEntity(type=MessageEntity.BOLD, offset=4, length=4)],
    )
    assert _inline_translation_html(message, "bold words") is None


def test_inline_html_entity_straddling_prefix_falls_back_to_plain():
    message = make_inline_message(
        "/tr 词条文本",
        [MessageEntity(type=MessageEntity.BOLD, offset=2, length=6)],
    )
    assert _inline_translation_html(message, "词条文本") is None


def test_inline_html_bot_command_entity_alone_is_plain():
    message = make_inline_message(
        "/tr 普通文本",
        [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=3)],
    )
    assert _inline_translation_html(message, "普通文本") is None


def test_parse_translation_args_leading_flags_only():
    from wuwaterm.bot import _parse_translation_args

    leading = _parse_translation_args(["--to", "en", "你好"])
    assert (leading.text, leading.forced_to_chinese) == ("你好", False)
    trailing = _parse_translation_args(["how", "--to", "convert", "files"])
    assert trailing.direction_error is False
    assert trailing.text == "how --to convert files"
    assert trailing.forced_to_chinese is None
    doubled = _parse_translation_args(["--to", "en", "--to", "zh", "声骸"])
    assert doubled.direction_error is True
    bad_value = _parse_translation_args(["--to", "jp", "x"])
    assert bad_value.direction_error is True


def test_short_english_word_is_not_hijacked_by_pinyin_fuzzy(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _t, _l: "他")
    # "he" is a substring of pinyin "shenghai" (Echo); it must go to the LLM
    # instead of answering with an unrelated dictionary term.
    update, message = fake_update()
    context = fake_context(sample_db, ["he"])

    asyncio.run(term_command(update, context))

    assert len(calls) == 1
    assert message.replies
    assert "Echo" not in message.replies[0][0]
    assert "声骸" not in message.replies[0][0]


def test_full_pinyin_and_abbrev_queries_still_answer_from_dictionary(sample_db):
    service = TermService(sample_db)
    from wuwaterm.bot import _fuzzy_dictionary_answer

    assert _fuzzy_dictionary_answer(service, "shenghai", to_chinese=False) == "Echo"
    assert _fuzzy_dictionary_answer(service, "sh", to_chinese=False) == "Echo"
    assert _fuzzy_dictionary_answer(service, "he", to_chinese=False) is None
    assert _fuzzy_dictionary_answer(service, "eng", to_chinese=False) is None


def test_tr_inline_formatted_text_uses_html_pipeline(monkeypatch, sample_db):
    calls = []

    def response(locked_text, _locks):
        return html_with_segments(
            locked_text, "", "This sentence needs translating.", ""
        )

    enable_mock_llm(monkeypatch, calls, response)
    update, message = fake_update()
    message.text = "/tr 这是一个需要翻译的句子。"
    message.entities = [
        MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=3),
        MessageEntity(type=MessageEntity.BOLD, offset=4, length=12),
    ]
    message.date = datetime(2026, 7, 22, tzinfo=timezone.utc)
    message.chat = Chat(id=1, type="private")
    context = fake_context(sample_db, ["这是一个需要翻译的句子。"])

    asyncio.run(term_command(update, context))

    assert message.replies == [("<b>This sentence needs translating.</b>", None)]
    assert message.reply_kwargs == [{"do_quote": False, "parse_mode": "HTML"}]
    assert len(calls) == 1


def test_tr_inline_text_wins_over_replied_content(sample_db):
    # Inline args take precedence; the replied-to content is ignored.
    update, message = fake_update(
        reply_to=SimpleNamespace(
            text="守岸人",
            caption=None,
            text_html="<b>守岸人</b>",
            caption_html=None,
            entities=[SimpleNamespace(type="bold")],
        )
    )
    context = fake_context(sample_db, ["声骸"])

    asyncio.run(term_command(update, context))

    # 声骸 -> Echo (inline), NOT 守岸人 -> Shorekeeper (replied).
    assert message.replies == [("Echo", None)]
    assert message.reply_kwargs == [{"do_quote": False}]


def test_tr_reply_to_image_only_message_falls_through_to_usage(sample_db):
    # Replying to a sticker/image with no text and no caption -> Usage hint.
    update, message = fake_update(reply_to=SimpleNamespace(text=None, caption=None))
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [(TERM_USAGE_NOTICE, None)]


def test_tr_reply_to_caption_media_translates_caption(sample_db):
    # Media message with a caption (no text) -> the caption is the input.
    update, message = fake_update(reply_to=SimpleNamespace(text=None, caption="守岸人"))
    context = fake_context(sample_db, [])

    asyncio.run(term_command(update, context))

    assert message.replies == [("Shorekeeper", None)]


def test_sentence_reply_to_chinese_message_translates_replied_content(monkeypatch, sample_db):
    # The sentence handler (/sentence, /sent) honors the same reply fallback.
    monkeypatch.delenv("WUWATERM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WUWATERM_OPENAI_MODEL", raising=False)
    update, message = fake_update(
        reply_to=SimpleNamespace(text="今汐装备了声骸", caption=None)
    )
    context = fake_context(sample_db, [])

    asyncio.run(sentence_command(update, context))

    assert message.replies == [("Jinhsi装备了Echo", None)]


def test_usage_notices_are_bilingual(sample_db):
    assert TERM_USAGE_NOTICE == (
        "用法：/tr [--to en|zh] <中文或英文>（默认自动判向；回复消息后可只发 /tr [--to en|zh]）\n"
        "Usage: /tr [--to en|zh] <Chinese or English> (auto by default; reply to a message with /tr [--to en|zh])"
    )
    assert SENTENCE_USAGE_NOTICE == (
        "用法：/sentence [--to en|zh] <中文或英文句子>（默认自动判向；回复消息后可只发 /sentence [--to en|zh]）\n"
        "Usage: /sentence [--to en|zh] <Chinese or English sentence> (auto by default; reply with /sentence [--to en|zh])"
    )
    assert DIRECTION_USAGE_NOTICE == (
        "翻译方向参数只支持一次 en 或 zh；用法：--to en / --to zh。\n"
        "Translation direction can be set once to en or zh; usage: --to en / --to zh."
    )
    # Bare command, no inline text and no reply -> the bilingual Usage hint.
    update, message = fake_update()
    context = fake_context(sample_db, [])
    asyncio.run(sentence_command(update, context))
    assert message.replies == [(SENTENCE_USAGE_NOTICE, None)]


@pytest.mark.parametrize(
    ("handler", "usage_notice"),
    [
        (term_command, TERM_USAGE_NOTICE),
        (sentence_command, SENTENCE_USAGE_NOTICE),
    ],
)
def test_translation_command_wrappers_keep_distinct_usage_notice(
    handler, usage_notice, sample_db
):
    update, message = fake_update()
    context = fake_context(sample_db, [])

    asyncio.run(handler(update, context))

    assert message.replies == [(usage_notice, None)]


# --- /about diagnostics command ---


def test_term_service_metadata_and_count(sample_db):
    service = TermService(sample_db)
    meta = service.metadata()
    assert meta["wutheringdata_commit"] == "e9234ffe094b2d944d16b222d31102e8ab32d954"
    assert meta["source_profile"] == "dimbreath_legacy"
    assert service.term_count() > 0


def test_about_returns_expected_fields(monkeypatch, sample_db):
    update, message = fake_update()
    context = fake_context(sample_db, [])
    service = context.application.bot_data[SERVICE_KEY]
    monkeypatch.setattr(
        service,
        "metadata",
        lambda: {
            "source_profile": "arikatsu",
            "source_repo_url": "https://example.test/repo.git",
            "wutheringdata_commit": "abc123def456",
        },
    )
    monkeypatch.setattr(service, "term_count", lambda: 4242)

    asyncio.run(about_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert "arikatsu" in reply
    assert "https://example.test/repo.git" in reply
    assert "abc123def456" in reply
    assert "4242" in reply
    assert "10/min" in reply  # fake_context default rate limit


def test_about_makes_zero_llm_calls(monkeypatch, sample_db):
    calls = []
    enable_mock_llm(monkeypatch, calls, lambda _locked_text, _locks: "should not run")
    update, message = fake_update()
    context = fake_context(sample_db, [])

    asyncio.run(about_command(update, context))

    assert len(message.replies) == 1
    assert calls == []


def test_about_has_no_auth_gate(sample_db):
    # A non-owner in private still gets /about (informational, no auth gate).
    update, message = fake_update(chat_id=2, chat_type="private", user_id=22)
    context = fake_context(
        sample_db, [], config=BotConfig(rate_limit_per_minute=10, owner_user_id=11)
    )

    asyncio.run(about_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert reply != DEFAULT_PRIVATE_TR_REJECT_TEXT
    assert "wuwaterm /about" in reply
    assert context.bot.member_calls == []


def test_about_flags_missing_commit_metadata(monkeypatch, sample_db):
    update, message = fake_update()
    context = fake_context(sample_db, [])
    service = context.application.bot_data[SERVICE_KEY]
    monkeypatch.setattr(service, "metadata", lambda: {})
    monkeypatch.setattr(service, "term_count", lambda: 0)

    asyncio.run(about_command(update, context))

    reply = message.replies[0][0]
    assert "unknown" in reply  # the missing commit field flags the gap


def test_about_contains_full_commit_from_db(sample_db):
    # With the real sample DB, /about is the one reply allowed to carry the
    # full pinned commit hash (read from DB metadata).
    update, message = fake_update()
    context = fake_context(sample_db, [])

    asyncio.run(about_command(update, context))

    reply = message.replies[0][0]
    assert "e9234ffe094b2d944d16b222d31102e8ab32d954" in reply
    assert re.search(r"[0-9a-f]{40}", reply) is not None


# --- /status owner diagnostics ---


def test_status_owner_gets_sanitized_counts(monkeypatch, sample_db):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    settings.set_public(-2001, True)
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    reply_index.remember_many(-2001, 4001, (5001, 5002))

    asyncio.run(status_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert "wuwaterm /status" in reply
    assert "LLM configured: yes" in reply
    assert "Tracked channel posts: 1" in reply
    assert "Channel reply persistence: off" in reply
    assert "Channel reply load failures: 0" in reply
    assert "Channel reply last load: not configured" in reply
    assert "Channel reply save failures: 0" in reply
    assert "Channel reply last save: not configured" in reply
    assert "Authorized chats: 3" in reply
    assert "Public chats: 1" in reply
    assert "Public LLM input limit: 2000" in reply
    assert "Trusted/channel text limit: 4096" in reply
    assert "Trusted/channel caption limit: 1024" in reply
    assert "-2001" not in reply
    assert "5001" not in reply
    assert "test-key" not in reply
    assert re.search(r"[0-9a-f]{40}", reply) is None


def test_status_reports_channel_reply_persistence_health_without_paths(
    sample_db, tmp_path
):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("x", encoding="utf-8")
    storage_path = blocked_parent / "channel_replies.json"
    reply_index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=storage_path)
    context.application.bot_data[CHANNEL_REPLY_INDEX_KEY] = reply_index

    reply_index.remember_many(-2001, 4001, (5001,))
    asyncio.run(status_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert "Channel reply persistence: on" in reply
    assert "Channel reply load failures: 0" in reply
    assert "Channel reply last load: not attempted" in reply
    assert "Channel reply save failures: 1" in reply
    assert "Channel reply last save: failed" in reply
    assert str(tmp_path) not in reply
    assert "-2001" not in reply
    assert "4001" not in reply
    assert "5001" not in reply


def test_status_reports_channel_runtime_counters_without_message_content(sample_db):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])
    runtime = ChannelRuntime(
        max_active=2,
        max_pending=3,
        llm_calls_per_minute=7,
    )
    runtime.record("received", "update")
    runtime.record("skipped", "queue_full")
    runtime.record("delivery", "success")
    context.application.bot_data[CHANNEL_RUNTIME_KEY] = runtime

    asyncio.run(status_command(update, context))

    reply = message.replies[0][0]
    assert "Channel translation active: 0" in reply
    assert "Channel translation pending: 0" in reply
    assert "delivery:success=1" in reply
    assert "received:update=1" in reply
    assert "skipped:queue_full=1" in reply
    assert "sensitive message body" not in reply


def test_status_reports_healthy_channel_reply_persistence_without_paths(
    sample_db, tmp_path
):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])
    storage_path = tmp_path / "channel_replies.json"
    reply_index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=storage_path)
    context.application.bot_data[CHANNEL_REPLY_INDEX_KEY] = reply_index

    reply_index.remember_many(-2001, 4001, (5001,))
    asyncio.run(status_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert "Channel reply persistence: on" in reply
    assert "Channel reply load failures: 0" in reply
    assert "Channel reply last load: not attempted" in reply
    assert "Channel reply save failures: 0" in reply
    assert "Channel reply last save: ok" in reply
    assert str(tmp_path) not in reply
    assert "-2001" not in reply
    assert "4001" not in reply
    assert "5001" not in reply


def test_status_reports_channel_reply_load_failure_without_details(sample_db, tmp_path):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])
    storage_path = tmp_path / "channel_replies.json"
    storage_path.write_text("{not json", encoding="utf-8")
    reply_index = ChannelReplyIndex(ttl_seconds=60.0, storage_path=storage_path)
    context.application.bot_data[CHANNEL_REPLY_INDEX_KEY] = reply_index

    asyncio.run(status_command(update, context))

    assert len(message.replies) == 1
    reply = message.replies[0][0]
    assert "Channel reply persistence: on" in reply
    assert "Channel reply load failures: 1" in reply
    assert "Channel reply last load: failed" in reply
    assert "Channel reply save failures: 0" in reply
    assert "Channel reply last save: not attempted" in reply
    assert str(tmp_path) not in reply
    assert storage_path.name not in reply
    assert "-2001" not in reply
    assert "4001" not in reply
    assert "5001" not in reply
    assert "test-key" not in reply
    assert "JSONDecodeError" not in reply
    assert "Expecting" not in reply


def test_status_non_owner_is_silent(sample_db):
    update, message = fake_update(chat_id=1, chat_type="private", user_id=22)
    context = fake_context(sample_db, [])

    asyncio.run(status_command(update, context))

    assert message.replies == []


# --- /public toggle ---


def test_public_on_enables_and_persists_for_admin(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=600
    )
    context = fake_context(sample_db, ["on"], member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    assert settings.is_public(-2001) is False  # default

    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_ENABLED_NOTICE, 600)]
    assert settings.is_public(-2001) is True


def test_public_off_disables_for_admin(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=601
    )
    context = fake_context(sample_db, ["off"], member_status="administrator")
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)

    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_DISABLED_NOTICE, 601)]
    assert (
        context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is False
    )


def test_public_status_reports_current_state(sample_db):
    update_off, msg_off = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=602
    )
    context = fake_context(sample_db, [], member_status="administrator")
    asyncio.run(public_command(update_off, context))
    assert msg_off.replies == [(PUBLIC_STATUS_OFF, 602)]

    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)
    update_on, msg_on = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=603
    )
    context.args = ["status"]
    asyncio.run(public_command(update_on, context))
    assert msg_on.replies == [(PUBLIC_STATUS_ON, 603)]


def test_public_bad_arg_returns_usage_notice(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=604
    )
    context = fake_context(sample_db, ["maybe"], member_status="administrator")

    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_USAGE_NOTICE, 604)]
    # Bad arg must not silently flip the state.
    assert (
        context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is False
    )


def test_public_non_admin_rejected_with_bilingual_notice(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=605, user_id=42
    )
    context = fake_context(sample_db, ["on"], member_status="member")

    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_REJECT_NOTICE, 605)]
    # Non-admin attempt must not flip the state.
    assert (
        context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is False
    )


def test_public_non_admin_silent_when_reject_silent(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=606, user_id=42
    )
    context = fake_context(
        sample_db,
        ["on"],
        member_status="member",
        config=BotConfig(
            rate_limit_per_minute=10, owner_user_id=11, tr_reject_silent=True
        ),
    )

    asyncio.run(public_command(update, context))

    assert message.replies == []
    assert (
        context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is False
    )


def test_public_in_private_chat_returns_groups_only_notice(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["on"])

    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_ONLY_GROUPS_NOTICE, None)]


def test_public_mode_allows_non_admin_translate_commands(sample_db):
    # An admin flips the chat to public.
    admin_update, _admin_msg = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=610
    )
    context = fake_context(sample_db, ["on"], member_status="administrator")
    asyncio.run(public_command(admin_update, context))

    # Now a non-admin's /tr works (still throttled, but not rejected).
    context.args = ["声骸"]
    member_update, member_msg = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=611, user_id=42
    )
    # Override the bot to return 'member' for this user; admin status is cached
    # per (chat, user), so the new user is a fresh lookup and returns 'member'
    # from the default. To make the cache return 'member' for user 42, swap the
    # FakeBot default.
    context.bot.default_status = "member"
    # Clear admin cache so the new user isn't poisoned by the prior admin lookup.
    context.application.bot_data[ADMIN_CACHE_KEY] = AdminStatusCache()

    asyncio.run(term_command(member_update, context))

    assert member_msg.replies == [("Echo", 611)]


def test_public_command_is_admin_only_even_when_public_mode_is_on(sample_db):
    # Public mode must NOT let a non-admin flip the switch back off — otherwise
    # any group member could disable a public chat. This is the central
    # invariant of the /public design.
    context = fake_context(sample_db, ["off"], member_status="member")
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)

    member_update, member_msg = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=620, user_id=42
    )

    asyncio.run(public_command(member_update, context))

    assert member_msg.replies == [(PUBLIC_REJECT_NOTICE, 620)]
    # The chat must still be public.
    assert (
        context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is True
    )


def test_public_off_in_one_chat_does_not_affect_another(sample_db):
    context = fake_context(sample_db, ["on"], member_status="administrator")
    chat_a, chat_b = -2001, -2002

    update_a, _ = fake_update(
        chat_id=chat_a, chat_type="supergroup", message_id=700
    )
    asyncio.run(public_command(update_a, context))

    context.args = ["on"]
    update_b, _ = fake_update(
        chat_id=chat_b, chat_type="supergroup", message_id=701
    )
    asyncio.run(public_command(update_b, context))

    # Turning chat A off must leave chat B public.
    context.args = ["off"]
    update_a_off, _ = fake_update(
        chat_id=chat_a, chat_type="supergroup", message_id=702
    )
    asyncio.run(public_command(update_a_off, context))

    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    assert settings.is_public(chat_a) is False
    assert settings.is_public(chat_b) is True


def test_public_off_works_when_translation_limiter_exhausted(sample_db):
    # Finding 1: an admin must be able to close a spammed public chat. Drain the
    # TRANSLATION limiter via /tr (limit=1), then /public off must still flip.
    context = fake_context(sample_db, ["声骸"], limit=1, member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    settings.set_public(-2001, True)

    tr_update, _ = fake_update(chat_id=-2001, chat_type="supergroup", message_id=800)
    asyncio.run(term_command(tr_update, context))  # consumes the single slot

    context.args = ["off"]
    off_update, off_msg = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=801
    )
    asyncio.run(public_command(off_update, context))

    assert off_msg.replies == [(PUBLIC_DISABLED_NOTICE, 801)]
    assert settings.is_public(-2001) is False


def test_public_toggle_ignores_stale_admin_cache(sample_db):
    # Finding 2: a just-demoted admin (cached positive, now a plain member) must
    # NOT be able to flip /public — the control-plane toggle checks fresh.
    context = fake_context(sample_db, ["on"], member_status="member")
    cache = context.application.bot_data[ADMIN_CACHE_KEY]
    cache.put(-2001, 11, True)  # stale positive verdict

    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=810, user_id=11
    )
    asyncio.run(public_command(update, context))

    assert message.replies == [(PUBLIC_REJECT_NOTICE, 810)]
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_public(-2001) is False
    # The stale cache was bypassed: a fresh getChatMember actually ran.
    assert (-2001, 11) in context.bot.member_calls


def test_public_mode_does_not_authorize_foreign_sender_chat(sample_db):
    # Finding 4: public mode opens the door for ordinary members only. A message
    # posted by a foreign sender_chat (a channel identity) is NOT authorized.
    context = fake_context(sample_db, ["声骸"], member_status="member")
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)

    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=820,
        user_id=None,
        sender_chat_id=-9999,  # a DIFFERENT id than the chat (not anon-admin)
    )
    asyncio.run(term_command(update, context))

    # Rejected, not translated.
    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 820)]


def test_anonymous_admin_still_authorized_in_public_chat(sample_db):
    # Finding 4 (other direction): the anonymous-admin case (sender_chat == chat)
    # must STILL be authorized — no regression from the sender_chat restriction.
    context = fake_context(sample_db, ["声骸"], member_status="member")
    context.application.bot_data[CHAT_SETTINGS_KEY].set_public(-2001, True)

    update, message = fake_update(
        chat_id=-2001,
        chat_type="supergroup",
        message_id=821,
        user_id=None,
        sender_chat_id=-2001,  # posts AS the group itself
    )
    asyncio.run(term_command(update, context))

    assert message.replies == [("Echo", 821)]


def test_public_on_replies_notice_when_save_fails(monkeypatch, sample_db):
    # Finding 3 (command layer): a settings save failure replies a notice and
    # does not raise an unhandled exception.
    context = fake_context(sample_db, ["on"], member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    def boom(chat_id, value):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "set_public", boom)
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=830
    )
    asyncio.run(public_command(update, context))

    assert message.replies == [(SETTINGS_SAVE_FAILED_NOTICE, 830)]


def test_public_off_write_failure_reports_memory_only_disable(
    monkeypatch, sample_db
):
    context = fake_context(sample_db, ["off"], member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    settings.set_public(-2001, True)

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "_save_state", boom)
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=833
    )

    asyncio.run(public_command(update, context))

    assert settings.is_public(-2001) is False
    assert ChatSettings(settings.path).is_public(-2001) is True
    assert message.replies == [(SETTINGS_DENY_NOT_PERSISTED_NOTICE, 833)]
    assert "state unchanged" not in message.replies[0][0]


def test_public_on_reports_durability_uncertainty(monkeypatch, sample_db):
    context = fake_context(sample_db, ["on"], member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    monkeypatch.setattr(settings, "_save_state", raise_durability_uncertain)
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=831
    )

    asyncio.run(public_command(update, context))

    assert settings.is_public(-2001) is True
    assert message.replies == [(SETTINGS_DURABILITY_UNCERTAIN_NOTICE, 831)]


def test_public_off_reports_durability_uncertainty(monkeypatch, sample_db):
    context = fake_context(sample_db, ["off"], member_status="administrator")
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    settings.set_public(-2001, True)
    monkeypatch.setattr(settings, "_save_state", raise_durability_uncertain)
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=832
    )

    asyncio.run(public_command(update, context))

    assert settings.is_public(-2001) is False
    assert message.replies == [(SETTINGS_DURABILITY_UNCERTAIN_NOTICE, 832)]


# --- group authorization gate: my_chat_member ---


def test_owner_added_group_is_authorized_and_kept(sample_db):
    # owner_user_id defaults to 11 in fake_context; the owner adds the bot.
    update = fake_member_update(chat_id=-2001, from_id=11)
    context = fake_context(sample_db, [])

    asyncio.run(my_chat_member_handler(update, context))

    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    assert settings.is_allowed(-2001) is True
    assert context.bot.left_chats == []
    assert context.bot.sent_messages == []


def test_nonowner_added_unauthorized_group_gets_notice_then_leaves(sample_db):
    update = fake_member_update(chat_id=-2001, from_id=42)  # not the owner
    context = fake_context(sample_db, [], allowlist=())

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.sent_messages == [(-2001, UNAUTHORIZED_GROUP_NOTICE)]
    assert context.bot.left_chats == [-2001]
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is False


def test_added_to_preauthorized_group_stays(sample_db):
    context = fake_context(sample_db, [])
    context.application.bot_data[CHAT_SETTINGS_KEY].allow(-2001)
    update = fake_member_update(chat_id=-2001, from_id=42)  # non-owner, but allowed

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == []
    assert context.bot.sent_messages == []


def test_promotion_in_unauthorized_joined_chat_does_not_leave(sample_db):
    # The live-group protector: a promotion (member->administrator) is NOT an
    # "added" edge, so the gate must not fire even when the chat is unallowed.
    update = fake_member_update(
        chat_id=-2001, from_id=42, old_status="member", new_status="administrator"
    )
    context = fake_context(sample_db, [])

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == []
    assert context.bot.sent_messages == []


def test_removal_event_does_not_leave_or_notify(sample_db):
    update = fake_member_update(
        chat_id=-2001, from_id=42, old_status="member", new_status="left"
    )
    context = fake_context(sample_db, [])

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == []
    assert context.bot.sent_messages == []


def test_owner_unset_leaves_unauthorized_group(sample_db):
    # Fail-closed: with no owner configured, owner-add can never match, so a
    # newly-added unauthorized group is left.
    context = fake_context(
        sample_db,
        [],
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=None),
        allowlist=(),
    )
    update = fake_member_update(chat_id=-2001, from_id=11)

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == [-2001]
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is False


def test_left_to_restricted_nonmember_is_not_added(sample_db):
    # left -> restricted(is_member=False): the bot is NOT actually in the chat,
    # so this is not an "added" edge and the gate must not fire.
    context = fake_context(sample_db, [])
    update = fake_member_update(
        chat_id=-2001,
        from_id=42,
        old_status="left",
        new_status="restricted",
        new_is_member=False,
    )

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == []
    assert context.bot.sent_messages == []


def test_restricted_nonmember_to_member_is_added_and_gated(sample_db):
    # restricted(is_member=False) -> member: the bot just BECAME a member, so a
    # non-owner cannot use this transition to sneak the bot past the gate.
    context = fake_context(sample_db, [], allowlist=())
    update = fake_member_update(
        chat_id=-2001,
        from_id=42,
        old_status="restricted",
        old_is_member=False,
        new_status="member",
    )

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == [-2001]


def test_restricted_member_to_admin_is_not_added(sample_db):
    # restricted(is_member=True) -> administrator: already a member, not an add.
    context = fake_context(sample_db, [])
    update = fake_member_update(
        chat_id=-2001,
        from_id=42,
        old_status="restricted",
        old_is_member=True,
        new_status="administrator",
    )

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == []


def test_owner_add_that_cannot_persist_leaves_fail_closed(monkeypatch, sample_db):
    # If owner-add authorization cannot be written (disk full), fail closed:
    # leave rather than stay in a chat we cannot remember authorizing.
    context = fake_context(sample_db, [])
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    def boom(chat_id):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "allow", boom)
    update = fake_member_update(chat_id=-2001, from_id=11)  # owner adds

    asyncio.run(my_chat_member_handler(update, context))

    assert context.bot.left_chats == [-2001]


def test_owner_add_durability_uncertain_stays_and_notifies(
    monkeypatch, sample_db
):
    context = fake_context(sample_db, [], allowlist=())
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    monkeypatch.setattr(settings, "_save_state", raise_durability_uncertain)
    update = fake_member_update(chat_id=-7001, from_id=11)

    asyncio.run(my_chat_member_handler(update, context))

    assert settings.is_allowed(-7001) is True
    assert context.bot.left_chats == []
    assert context.bot.sent_messages == [
        (-7001, SETTINGS_DURABILITY_UNCERTAIN_NOTICE)
    ]


# --- /grant, /revoke (owner-only) ---


def test_grant_in_group_allows_current_chat(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=900, user_id=11
    )
    context = fake_context(sample_db, [])  # no args -> current chat

    asyncio.run(grant_command(update, context))

    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    assert settings.is_allowed(-2001) is True
    assert message.replies == [
        ("已授权本群\nThis chat is granted.\n\n-2001", 900)
    ]


def test_grant_by_id_in_private(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["-2002"])

    asyncio.run(grant_command(update, context))

    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2002) is True
    assert message.replies == [("已授权\nGranted.\n\n-2002", None)]


def test_grant_lists_allowed_chats_in_private(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, [])

    asyncio.run(grant_command(update, context))

    assert message.replies == [
        (
            "授权名单 · 3\n"
            "Granted chats\n"
            "\n"
            "-2003\n"
            "-2002\n"
            "-2001\n"
            "\n"
            f"{GRANT_USAGE}",
            None,
        )
    ]


def test_grant_list_empty_allowlist(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["list"], allowlist=())

    asyncio.run(grant_command(update, context))

    assert message.replies == [
        (
            "授权名单 · 0\n"
            "Granted chats\n"
            "\n"
            "（空）\n"
            "\n"
            f"{GRANT_USAGE}",
            None,
        )
    ]


def test_grant_invalid_arg_shows_usage(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["not-an-id"])

    asyncio.run(grant_command(update, context))

    assert message.replies == [(GRANT_USAGE, None)]


def test_grant_non_owner_is_silent(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=901, user_id=42
    )
    context = fake_context(sample_db, [], allowlist=())

    asyncio.run(grant_command(update, context))

    assert message.replies == []
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is False


def test_grant_owner_unset_is_silent_for_everyone(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=903, user_id=11
    )
    context = fake_context(
        sample_db,
        [],
        config=BotConfig(rate_limit_per_minute=10, owner_user_id=None),
        allowlist=(),
    )

    asyncio.run(grant_command(update, context))

    assert message.replies == []
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is False


def test_revoke_in_group_leaves_and_removes_current_chat(sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=902, user_id=11
    )
    context = fake_context(sample_db, [])
    context.application.bot_data[CHAT_SETTINGS_KEY].allow(-2001)

    asyncio.run(revoke_command(update, context))

    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    assert settings.is_allowed(-2001) is False
    assert context.bot.left_chats == [-2001]  # /revoke actually stops service
    assert message.replies == [
        (
            "已撤销本群授权并退出本群（chat_id=-2001）\nRevoked; leaving this chat (chat_id=-2001).",
            902,
        )
    ]


def test_revoke_by_id_in_private_leaves_target(sample_db):
    update, message = fake_update(chat_id=11, chat_type="private", user_id=11)
    context = fake_context(sample_db, ["-2002"])
    context.application.bot_data[CHAT_SETTINGS_KEY].allow(-2002)

    asyncio.run(revoke_command(update, context))

    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2002) is False
    assert context.bot.left_chats == [-2002]
    assert message.replies == [("已撤销并退出 / Revoked and left chat_id=-2002", None)]


def test_revoke_leaves_even_if_confirmation_reply_fails(sample_db):
    # A TelegramError from the confirmation reply must NOT abort the leave —
    # otherwise a revoked group could keep being served.
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=905, user_id=11
    )

    async def _raise_reply(*args, **kwargs):
        raise TelegramError("send failed")

    message.reply_text = _raise_reply
    context = fake_context(sample_db, [])
    context.application.bot_data[CHAT_SETTINGS_KEY].allow(-2001)

    asyncio.run(revoke_command(update, context))

    assert context.bot.left_chats == [-2001]
    assert context.application.bot_data[CHAT_SETTINGS_KEY].is_allowed(-2001) is False


def test_tr_rejected_in_non_allowlisted_group(sample_db):
    # Option B: the bot serves only allowlisted groups. Even an admin in a chat
    # not on the allowlist is not served — so a revoked/unauthorized chat, or one
    # where leave_chat failed, stops being served even before the bot is removed.
    update, message = fake_update(
        chat_id=-7001, chat_type="supergroup", message_id=950
    )
    context = fake_context(sample_db, ["声骸"], member_status="administrator")  # -7001 not allowlisted

    asyncio.run(term_command(update, context))

    assert message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 950)]


def test_grant_durability_uncertain_reports_applied_state(
    monkeypatch, sample_db
):
    update, message = fake_update(
        chat_id=-7001, chat_type="supergroup", message_id=949, user_id=11
    )
    context = fake_context(sample_db, [], allowlist=())
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    monkeypatch.setattr(settings, "_save_state", raise_durability_uncertain)

    asyncio.run(grant_command(update, context))

    assert settings.is_allowed(-7001) is True
    assert message.replies == [(SETTINGS_DURABILITY_UNCERTAIN_NOTICE, 949)]


def test_grant_save_failure_notifies_and_does_not_claim_success(monkeypatch, sample_db):
    update, message = fake_update(
        chat_id=-7001, chat_type="supergroup", message_id=951, user_id=11
    )
    context = fake_context(sample_db, [], allowlist=())
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    def boom(chat_id):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "allow", boom)
    asyncio.run(grant_command(update, context))

    assert message.replies == [(SETTINGS_SAVE_FAILED_NOTICE, 951)]
    assert settings.is_allowed(-7001) is False  # not claimed authorized


def test_revoke_durability_uncertain_reports_visible_revoke(
    monkeypatch, sample_db
):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=950, user_id=11
    )
    context = fake_context(sample_db, [])
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]
    monkeypatch.setattr(settings, "_save_state", raise_durability_uncertain)

    asyncio.run(revoke_command(update, context))

    assert settings.is_allowed(-2001) is False
    assert context.bot.left_chats == [-2001]
    assert "durability is uncertain" in message.replies[0][0]
    assert "Couldn't persist" not in message.replies[0][0]


def test_revoke_save_failure_notifies_but_still_leaves(monkeypatch, sample_db):
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=952, user_id=11
    )
    context = fake_context(sample_db, [])  # -2001 allowlisted by default
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    def boom(chat_id):
        raise OSError("disk full")

    monkeypatch.setattr(settings, "disallow", boom)
    asyncio.run(revoke_command(update, context))

    assert context.bot.left_chats == [-2001]  # leaves regardless of the write failure
    assert "未能保存撤销" in message.replies[0][0]  # surfaces, does not claim success


def test_revoke_save_failure_keeps_chat_denied_and_unserved(monkeypatch, sample_db):
    # End-to-end fail-closed proof (Codex finding: "Fail closed when revoke
    # cannot persist"). When the disk WRITE fails, the real disallow() keeps the
    # chat removed in memory rather than rolling it back, so the chat is denied
    # AND a follow-up admin /tr there is rejected. A continued admin or a re-add
    # cannot keep using the bot after a failed-persist revoke.
    update, message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=953, user_id=11
    )
    context = fake_context(sample_db, [])  # -2001 allowlisted by default
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(
        settings, "_save_state", boom
    )  # the WRITE fails; real disallow runs
    asyncio.run(revoke_command(update, context))

    assert settings.is_allowed(-2001) is False  # deny kept despite the write failure
    assert context.bot.left_chats == [-2001]  # still leaves
    assert "未能保存撤销" in message.replies[0][0]  # surfaces the failure

    # Serving is actually denied now: an admin /tr in the same chat is rejected
    # because the gate consults is_allowed, which the failed revoke left False.
    tr_update, tr_message = fake_update(
        chat_id=-2001, chat_type="supergroup", message_id=954, user_id=11
    )
    tr_context = fake_context(sample_db, ["声骸"], member_status="administrator")
    tr_context.application.bot_data[CHAT_SETTINGS_KEY] = settings  # the now-denied state
    asyncio.run(term_command(tr_update, tr_context))

    assert tr_message.replies == [(DEFAULT_GROUP_TR_REJECT_TEXT, 954)]


def test_telegram_reply_renders_outcomes_at_the_adapter_seam():
    """Wording, parse mode and the strip fallback live in bot.py, not below."""
    from wuwaterm.application import KIND_LLM, TranslationOutcome
    from wuwaterm.bot import (
        DICT_MISS_FLAG,
        TranslationReply,
        _telegram_reply,
        _telegram_text,
    )

    plain = _telegram_reply(TranslationOutcome(kind=KIND_LLM, text="Echo"))
    assert plain == TranslationReply("Echo")

    flagged = _telegram_reply(
        TranslationOutcome(kind=KIND_LLM, text="Echo", dictionary_miss=True)
    )
    assert flagged.text == f"Echo\n\n{DICT_MISS_FLAG}"
    assert flagged.parse_mode is None

    rich = _telegram_reply(
        TranslationOutcome(kind=KIND_LLM, text="<b>Echo</b>", markup_used=True)
    )
    assert rich == TranslationReply("<b>Echo</b>", parse_mode="HTML")

    # Structurally broken markup must degrade to plain text, never be sent
    # with parse_mode=HTML (Telegram would reject the whole message).
    broken = _telegram_reply(
        TranslationOutcome(kind=KIND_LLM, text="Echo <b> broken", markup_used=True)
    )
    assert broken.parse_mode is None
    assert "<b>" not in broken.text

    # The flag is appended BEFORE validation, so a flagged rich reply that
    # still validates keeps HTML mode.
    flagged_rich = _telegram_reply(
        TranslationOutcome(
            kind=KIND_LLM,
            text="<b>Echo</b>",
            markup_used=True,
            dictionary_miss=True,
        )
    )
    assert flagged_rich.parse_mode == "HTML"
    assert flagged_rich.text.endswith(DICT_MISS_FLAG)
    assert _telegram_text(TranslationOutcome(kind=KIND_LLM, text="x")) == "x"
