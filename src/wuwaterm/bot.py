"""Telegram bot handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from telegram import ChatMember, Message, MessageEntity, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .application import (
    MarkupTranslation,
    SlidingWindowRateLimiter as PerChatRateLimiter,
    TranslationJob,
    TranslationOutcome,
    error_code_for_llm_reason,
    translate_request,
    translate_request_async,
)

# Re-exported on purpose, not used inside this module: the fuzzy gate now has
# exactly one definition (in the application layer) and this import is the
# whole of bot.py's relationship with it. Keeping the name reachable here is
# what lets existing callers and tests keep importing it from wuwaterm.bot.
from .application import _fuzzy_dictionary_answer  # noqa: F401
from .channel import channel_post_handler, send_with_flood_retry
from .channel_reply_index import ChannelReplyIndex
from .channel_reply_schema import ChannelReplyPayloadError, parse_channel_reply_payload
from .channel_runtime import ChannelRuntime
from .lookup import TermService
from .logging_utils import redact_id, safe_error_type, safe_text_len
from .sentence import (
    DEFAULT_LLM_MAX_CONCURRENCY,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMTranslationError,
    SentenceTranslator,
    _llm_configured,
)
from .runtime_keys import (
    ADMIN_CACHE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
)
from .settings import ChatSettings, ChatSettingsDurabilityError, ChatSettingsError
from .telegram_html import strip_telegram_html, validate_telegram_html
from .telegram_text import (
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    split_telegram_text,
    telegram_text_units,
)
from .translation_policy import (
    HTML_CONTENT_FAILURE_REASONS,
    LLM_INPUT_CHAR_LIMIT,
)


LOGGER = logging.getLogger(__name__)

# User-facing operational notices are bilingual: Chinese line first, then an
# English line (single "\n"), so non-Chinese readers in the group can read them.
# Each notice is a single source-of-truth constant. Owner-set env overrides
# (WUWATERM_GROUP_TR_REJECT_TEXT / WUWATERM_PRIVATE_TR_REJECT_TEXT) still win
# verbatim and are deliberately NOT auto-bilingual.
THROTTLE_NOTICE = (
    "本群消息过于频繁，请一分钟后再试。\n"
    "Rate limit reached for this chat. Try again in a minute."
)
DEFAULT_GROUP_TR_REJECT_TEXT = "仅群管理员可用 /tr\nOnly group admins can use /tr"
DEFAULT_PRIVATE_TR_REJECT_TEXT = (
    "此 bot 仅限群内由管理员使用\n"
    "This bot can only be used by admins inside a group."
)
# Usage hints (bilingual; also point out the reply-to-translate shortcut).
TERM_USAGE_NOTICE = (
    "用法：/tr [--to en|zh] <中文或英文>（默认自动判向；回复消息后可只发 /tr [--to en|zh]）\n"
    "Usage: /tr [--to en|zh] <Chinese or English> (auto by default; reply to a message with /tr [--to en|zh])"
)
SENTENCE_USAGE_NOTICE = (
    "用法：/sentence [--to en|zh] <中文或英文句子>（默认自动判向；回复消息后可只发 /sentence [--to en|zh]）\n"
    "Usage: /sentence [--to en|zh] <Chinese or English sentence> (auto by default; reply with /sentence [--to en|zh])"
)
DIRECTION_USAGE_NOTICE = (
    "翻译方向参数只支持一次 en 或 zh；用法：--to en / --to zh。\n"
    "Translation direction can be set once to en or zh; usage: --to en / --to zh."
)
# Appended to a short query that misses the dictionary. The data version
# (pinned commit) lives in /about, never in this user-facing line.
DICT_MISS_FLAG = (
    "(词典外,机器直译)\n"
    "(Not in official dictionary; machine-translated)"
)
# /public toggles whether non-admin members of a group can use the translate
# commands. Default is admin-only. The /public command itself is always
# admin-only (never bypassed by public mode), so a public group cannot have
# its switch flipped by a non-admin.
PUBLIC_USAGE_NOTICE = (
    "用法：/public on | off | status（仅群管理员）\n"
    "Usage: /public on | off | status (group admins only)"
)
PUBLIC_ENABLED_NOTICE = (
    "已对所有群成员开放翻译命令（/tr 等）\n"
    "Translate commands (/tr etc.) are now open to all members in this chat."
)
PUBLIC_DISABLED_NOTICE = (
    "翻译命令已恢复为仅群管理员可用\n"
    "Translate commands restricted to group admins again."
)
PUBLIC_STATUS_ON = (
    "当前状态：公开（所有群成员可用 /tr 等）\n"
    "Current state: public (all members may use /tr etc.)."
)
PUBLIC_STATUS_OFF = (
    "当前状态：仅群管理员可用\n"
    "Current state: admins-only."
)
PUBLIC_ONLY_GROUPS_NOTICE = (
    "/public 仅在群里有效\n"
    "/public only works in groups."
)
PUBLIC_REJECT_NOTICE = (
    "仅群管理员可用 /public\n"
    "Only group admins can use /public"
)
SETTINGS_SAVE_FAILED_NOTICE = (
    "设置保存失败，请稍后再试（状态未更改）。\n"
    "Could not save the setting, please try again later (state unchanged)."
)
SETTINGS_DENY_NOT_PERSISTED_NOTICE = (
    "设置已在当前进程中关闭，但未能持久化；重启后可能恢复，请稍后重试。\n"
    "The setting is disabled in this process but was not persisted; it may return after restart, so retry."
)
SETTINGS_DURABILITY_UNCERTAIN_NOTICE = (
    "设置已应用，但无法确认存储持久性；请检查当前状态并重试确认。\n"
    "The setting was applied, but storage durability is uncertain; check the current state and retry."
)
# Group authorization gate: shown once before the bot leaves a chat it was
# added to that is not authorized.
UNAUTHORIZED_GROUP_NOTICE = (
    "本 bot 未获授权在此群使用，需由 bot 主人授权；现在自动退出本群。\n"
    "This bot is not authorized for this chat. It needs the owner's "
    "authorization to stay, so it is leaving now."
)
GRANT_USAGE = (
    "用法 / Usage\n"
    "/grant           授权本群 / this chat\n"
    "/grant <chat_id> 按 id / by id\n"
    "/grant list      查看名单 / list"
)
REVOKE_USAGE = (
    "用法（仅 bot 主人）：群内发 /revoke 撤销本群授权；私聊发 /revoke <chat_id> 按 id 撤销。\n"
    "Usage (owner only): /revoke in a group to remove it; /revoke <chat_id> in "
    "private to remove by id."
)
# Inline rich text: the /command prefix and leading direction flags are cut
# off the raw message text so the remaining tail's formatting entities can be
# preserved. Flags mirror _parse_translation_args (lowercase only).
COMMAND_PREFIX_RE = re.compile(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?\s*")
# No ^ anchor: matched with .match(text, pos), where ^ would still anchor to
# the string start and never match at pos > 0. The value is case-insensitive
# to mirror _parse_translation_args, which lowercases it; the flag itself is
# case-sensitive there and stays so here.
DIRECTION_FLAG_PREFIX_RE = re.compile(r"(?:--to|-to)\s+(?i:en|zh)\s+")
ADMIN_ALLOWED_STATUSES = frozenset({"creator", "administrator"})
ADMIN_STATUS_CACHE_TTL_SECONDS = 300.0
DEFAULT_CHANNEL_MAX_AGE_SECONDS = 24 * 60 * 60
ADMIN_CACHE_MAX_ENTRIES = 1024
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_ENV_VALUES = frozenset({"0", "false", "no", "off"})


class BotConfigError(ValueError):
    """An environment setting cannot be parsed safely."""


class StateMigrationError(RuntimeError):
    """Legacy runtime state could not be copied into the configured state dir."""


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BotConfigError(
            f"{name} must be an integer between {minimum} and {maximum}"
        ) from None
    if not minimum <= value <= maximum:
        raise BotConfigError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise BotConfigError(
            f"{name} must be a number between {minimum} and {maximum}"
        ) from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise BotConfigError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUTHY_ENV_VALUES:
        return True
    if normalized in _FALSY_ENV_VALUES:
        return False
    raise BotConfigError(
        f"{name} must be a boolean token such as true or false"
    )


@dataclass(frozen=True)
class BotConfig:
    rate_limit_per_minute: int = 10
    group_tr_reject_text: str = DEFAULT_GROUP_TR_REJECT_TEXT
    private_tr_reject_text: str = DEFAULT_PRIVATE_TR_REJECT_TEXT
    tr_reject_silent: bool = False
    owner_user_id: int | None = None
    channel_autotranslate: bool = True
    channel_min_cjk: int = 1
    channel_min_latin: int = 2
    channel_text_limit: int = 4096
    channel_caption_limit: int = 1024
    channel_max_age_seconds: int = DEFAULT_CHANNEL_MAX_AGE_SECONDS
    channel_max_pending: int = 16
    channel_llm_calls_per_minute: int = 60
    channel_reply_index_path: str | None = None
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            rate_limit_per_minute=_env_int(
                "WUWATERM_RATE_LIMIT_PER_MINUTE",
                10,
                minimum=1,
                maximum=10000,
            ),
            group_tr_reject_text=os.getenv(
                "WUWATERM_GROUP_TR_REJECT_TEXT", DEFAULT_GROUP_TR_REJECT_TEXT
            ),
            private_tr_reject_text=os.getenv(
                "WUWATERM_PRIVATE_TR_REJECT_TEXT", DEFAULT_PRIVATE_TR_REJECT_TEXT
            ),
            tr_reject_silent=_env_bool(
                "WUWATERM_TR_REJECT_SILENT",
                False,
            ),
            owner_user_id=_owner_user_id_from_env(),
            channel_autotranslate=_env_bool(
                "WUWATERM_CHANNEL_AUTOTRANSLATE",
                True,
            ),
            channel_min_cjk=_env_int(
                "WUWATERM_CHANNEL_MIN_CJK",
                1,
                minimum=1,
                maximum=4096,
            ),
            channel_min_latin=_env_int(
                "WUWATERM_CHANNEL_MIN_LATIN",
                2,
                minimum=1,
                maximum=4096,
            ),
            channel_text_limit=_env_int(
                "WUWATERM_CHANNEL_TEXT_LIMIT",
                4096,
                minimum=1,
                maximum=4096,
            ),
            channel_caption_limit=_env_int(
                "WUWATERM_CHANNEL_CAPTION_LIMIT",
                1024,
                minimum=1,
                maximum=1024,
            ),
            channel_max_age_seconds=_env_int(
                "WUWATERM_CHANNEL_MAX_AGE_SECONDS",
                DEFAULT_CHANNEL_MAX_AGE_SECONDS,
                minimum=1,
                maximum=2592000,
            ),
            channel_max_pending=_env_int(
                "WUWATERM_CHANNEL_MAX_PENDING",
                16,
                minimum=0,
                maximum=1024,
            ),
            channel_llm_calls_per_minute=_env_int(
                "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE",
                60,
                minimum=1,
                maximum=10000,
            ),
            channel_reply_index_path=(
                os.getenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "").strip() or None
            ),
            llm_timeout_seconds=_env_float(
                "WUWATERM_LLM_TIMEOUT_SECONDS",
                DEFAULT_LLM_TIMEOUT_SECONDS,
                minimum=0.1,
                maximum=300.0,
            ),
            llm_max_concurrency=_env_int(
                "WUWATERM_LLM_MAX_CONCURRENCY",
                DEFAULT_LLM_MAX_CONCURRENCY,
                minimum=1,
                maximum=64,
            ),
        )


def _owner_user_id_from_env() -> int | None:
    raw = os.getenv("OWNER_USER_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            # Never echo the raw value: the owner id is quasi-sensitive.
            LOGGER.warning(
                "OWNER_USER_ID is set but not a valid integer; "
                "private /tr will reject everyone"
            )
            return None
    LOGGER.warning(
        "OWNER_USER_ID is not configured; private /tr will reject everyone"
    )
    return None


def _validate_llm_config_env() -> None:
    values = {
        "WUWATERM_OPENAI_BASE_URL": (
            os.getenv("WUWATERM_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ""
        ).strip(),
        "WUWATERM_OPENAI_API_KEY": (
            os.getenv("WUWATERM_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        ).strip(),
        "WUWATERM_OPENAI_MODEL": os.getenv("WUWATERM_OPENAI_MODEL", "").strip(),
    }
    configured = [name for name, value in values.items() if value]
    if not configured:
        return
    if len(configured) != len(values):
        missing = ", ".join(name for name, value in values.items() if not value)
        raise BotConfigError(f"incomplete LLM configuration; missing: {missing}")
    parsed = urlparse(values["WUWATERM_OPENAI_BASE_URL"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BotConfigError(
            "WUWATERM_OPENAI_BASE_URL must be an absolute HTTP(S) URL"
        )


class AdminStatusCache:
    """Short-TTL cache of getChatMember admin verdicts per (chat, user)."""

    def __init__(
        self,
        ttl_seconds: float = ADMIN_STATUS_CACHE_TTL_SECONDS,
        *,
        max_entries: int = ADMIN_CACHE_MAX_ENTRIES,
    ):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[tuple[int, int], tuple[float, bool]] = {}

    def get(self, chat_id: int, user_id: int, now: float | None = None) -> bool | None:
        now = time.monotonic() if now is None else now
        key = (chat_id, user_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, verdict = entry
        if now >= expires_at:
            del self._entries[key]
            return None
        return verdict

    def put(self, chat_id: int, user_id: int, verdict: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._entries = {
            key: entry for key, entry in self._entries.items() if entry[0] > now
        }
        self._entries[(chat_id, user_id)] = (now + self.ttl_seconds, verdict)
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            oldest = sorted(
                self._entries,
                key=lambda key: (self._entries[key][0], key[0], key[1]),
            )
            for key in oldest[:overflow]:
                self._entries.pop(key, None)

    def entry_count(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._entries = {
            key: entry for key, entry in self._entries.items() if entry[0] > now
        }
        return len(self._entries)


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {"group", "supergroup"})


def chat_id_for(update: Update) -> int | None:
    chat = update.effective_chat
    return int(chat.id) if chat else None


@dataclass(frozen=True)
class TranslationRequest:
    text: str
    html: str | None = None
    forced_to_chinese: bool | None = None
    is_caption: bool = False


@dataclass(frozen=True)
class TranslationActor:
    tier: str
    trusted: bool
    rate_limited: bool


@dataclass(frozen=True)
class ParsedTranslationArgs:
    text: str
    forced_to_chinese: bool | None = None
    direction_error: bool = False


@dataclass(frozen=True)
class TranslationReply:
    text: str
    parse_mode: str | None = None


def _parse_translation_args(args: list[str]) -> ParsedTranslationArgs:
    """Parse LEADING direction flags; everything after the first non-flag
    token is literal text. A "--to" inside prose ("/tr how --to convert
    files") is therefore translated, not treated as a broken flag, and
    formatting entities can be preserved for the untouched tail."""
    forced_to_chinese: bool | None = None
    index = 0
    while index < len(args):
        arg = args[index].strip()
        if arg in {"--to", "-to"}:
            if forced_to_chinese is not None or index + 1 >= len(args):
                return ParsedTranslationArgs(text="", direction_error=True)
            value = args[index + 1].strip().lower()
            if value == "en":
                forced_to_chinese = False
            elif value == "zh":
                forced_to_chinese = True
            else:
                return ParsedTranslationArgs(text="", direction_error=True)
            index += 2
            continue
        break
    return ParsedTranslationArgs(
        text=" ".join(args[index:]).strip(),
        forced_to_chinese=forced_to_chinese,
    )


def _translation_request(
    update: Update, inline_text: str, forced_to_chinese: bool | None = None
) -> TranslationRequest:
    if inline_text:
        html = _inline_translation_html(update.effective_message, inline_text)
        return TranslationRequest(
            text=inline_text,
            html=html,
            forced_to_chinese=forced_to_chinese,
        )
    return _replied_translation_request(update, forced_to_chinese=forced_to_chinese)


def _inline_translation_html(message, inline_text: str) -> str | None:
    """Telegram-HTML for formatted text typed inline after /tr or /sentence.

    Preserves the sender's own formatting entities through translation. Fails
    safe: any mismatch between the raw command tail and the parsed inline text
    (mid-text direction flags, collapsed whitespace, multiline input joined by
    the args tokenizer) or an entity straddling the stripped prefix returns
    None, which keeps today's plain-text behavior.
    """
    text = getattr(message, "text", None)
    entities = getattr(message, "entities", None) or ()
    date = getattr(message, "date", None)
    chat = getattr(message, "chat", None)
    if not text or not inline_text or not entities or date is None or chat is None:
        return None
    prefix = COMMAND_PREFIX_RE.match(text)
    if prefix is None:
        return None
    start = prefix.end()
    while True:
        flag = DIRECTION_FLAG_PREFIX_RE.match(text, start)
        if flag is None:
            break
        start = flag.end()
    tail = text[start:]
    if tail.strip() != inline_text:
        return None
    # Telegram entity offsets/lengths are UTF-16 code units, not code points.
    start_units = len(text[:start].encode("utf-16-le")) // 2
    tail_units = len(tail.encode("utf-16-le")) // 2
    shifted: list[MessageEntity] = []
    for entity in entities:
        if entity.type == MessageEntity.BOT_COMMAND:
            continue
        if entity.offset + entity.length <= start_units:
            continue  # entirely inside the stripped command/flag prefix
        if entity.offset < start_units or (
            entity.offset + entity.length > start_units + tail_units
        ):
            return None  # straddles the prefix boundary; not preservable
        shifted.append(
            MessageEntity(
                type=entity.type,
                offset=entity.offset - start_units,
                length=entity.length,
                url=entity.url,
                user=entity.user,
                language=entity.language,
                custom_emoji_id=entity.custom_emoji_id,
            )
        )
    if not shifted:
        return None
    # A locally built Message renders entity HTML exactly like PTB does for
    # incoming messages (text_html), so the downstream pipeline is identical
    # to the reply-to-message path.
    rendered = Message(
        message_id=0,
        date=date,
        chat=chat,
        text=tail,
        entities=shifted,
    ).text_html
    if not isinstance(rendered, str):
        return None
    rendered = rendered.strip()
    if not rendered or not _telegram_html_has_tags(rendered):
        return None
    return rendered


def _replied_translation_request(
    update: Update, forced_to_chinese: bool | None = None
) -> TranslationRequest:
    """Content of the message a command replies to: text/caption plus HTML.

    Lets an authorized caller translate a message by replying to it with a bare
    command. Returns "" when there is no reply, or it carries nothing to
    translate (e.g. a sticker or an image without a caption).
    """
    message = update.effective_message
    if message is None:
        return TranslationRequest(text="", forced_to_chinese=forced_to_chinese)
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return TranslationRequest(text="", forced_to_chinese=forced_to_chinese)
    has_text = bool(getattr(replied, "text", None))
    content = getattr(replied, "text", None) or getattr(replied, "caption", None)
    if not content:
        return TranslationRequest(text="", forced_to_chinese=forced_to_chinese)
    entities = ()
    html = getattr(replied, "text_html", None) if getattr(replied, "text", None) else None
    if html is not None:
        entities = getattr(replied, "entities", None) or ()
    if html is None:
        html = (
            getattr(replied, "caption_html", None)
            if getattr(replied, "caption", None)
            else None
        )
        if html is not None:
            entities = getattr(replied, "caption_entities", None) or ()
    text = content.strip()
    html = html.strip() if isinstance(html, str) else None
    return TranslationRequest(
        text=text,
        html=html if html and entities and _telegram_html_has_tags(html) else None,
        forced_to_chinese=forced_to_chinese,
        is_caption=not has_text,
    )


def _telegram_html_has_tags(html: str) -> bool:
    return re.search(r"</?[A-Za-z][^>]*>", html) is not None


async def reply_to_user(
    update: Update, text: str, *, parse_mode: str | None = None, retry_gate=None
) -> None:
    message = update.effective_message
    if not message:
        return
    chat = update.effective_chat
    chat_type = chat.type if chat else "unknown"
    chunks, delivery_parse_mode = _reply_chunks(text, parse_mode)
    first_reply_to_message_id = message.message_id if is_group_chat(update) else None
    for index, chunk in enumerate(chunks):
        reply_to_message_id = first_reply_to_message_id if index == 0 else None
        try:
            sent_message, sent_text = await _send_reply_chunk_with_html_fallback(
                message,
                chunk,
                reply_to_message_id=reply_to_message_id,
                parse_mode=delivery_parse_mode,
                retry_gate=retry_gate,
            )
        except BadRequest as exc:
            if delivery_parse_mode is not None and _bad_request_is_html_parse_error(exc):
                # Final guard for future send paths; the chunk helper normally
                # strips invalid Telegram HTML before BadRequest escapes.
                LOGGER.warning(
                    "bot_reply fallback: HTML parse failed chat_type=%s "
                    "incoming_message=%s error_type=%s",
                    chat_type,
                    redact_id(message.message_id),
                    safe_error_type(exc),
                )
                await reply_to_user(
                    update, _strip_telegram_html(text), retry_gate=retry_gate
                )
                return
            if (
                reply_to_message_id is None
                or not _bad_request_is_missing_reply_target(exc)
            ):
                raise
            LOGGER.warning(
                "bot_reply fallback: reply target missing chat_type=%s "
                "incoming_message=%s error_type=%s",
                chat_type,
                redact_id(message.message_id),
                safe_error_type(exc),
            )
            reply_to_message_id = None
            sent_message, sent_text = await _send_reply_chunk_with_html_fallback(
                message,
                chunk,
                reply_to_message_id=None,
                parse_mode=delivery_parse_mode,
                retry_gate=retry_gate,
            )
        LOGGER.info(
            "bot_reply chat_type=%s incoming_message=%s reply_message=%s "
            "reply_to_message=%s chunk=%s/%s parse_mode=%s text_len=%s",
            chat_type,
            redact_id(message.message_id),
            redact_id(getattr(sent_message, "message_id", None)),
            redact_id(reply_to_message_id),
            index + 1,
            len(chunks),
            delivery_parse_mode,
            safe_text_len(sent_text),
        )


async def _send_reply_chunk_with_html_fallback(
    message,
    text: str,
    *,
    reply_to_message_id: int | None,
    parse_mode: str | None,
    retry_gate=None,
):
    try:
        sent_message = await _send_reply_chunk(
            message,
            text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
            retry_gate=retry_gate,
        )
        return sent_message, text
    except BadRequest as exc:
        if parse_mode is None or not _bad_request_is_html_parse_error(exc):
            raise
        plain = _strip_telegram_html(text)
        sent_message = await _send_reply_chunk(
            message,
            plain,
            reply_to_message_id=reply_to_message_id,
            parse_mode=None,
            retry_gate=retry_gate,
        )
        return sent_message, plain


async def _send_reply_chunk(
    message,
    text: str,
    *,
    reply_to_message_id: int | None,
    parse_mode: str | None,
    retry_gate=None,
):
    kwargs = {}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if reply_to_message_id is None:
        return await send_with_flood_retry(
            lambda: message.reply_text(text, do_quote=False, **kwargs),
            retry_gate=retry_gate,
        )
    return await send_with_flood_retry(
        lambda: message.reply_text(
            text, reply_to_message_id=reply_to_message_id, **kwargs
        ),
        retry_gate=retry_gate,
    )


def _bad_request_is_missing_reply_target(exc: BadRequest) -> bool:
    message = str(exc).casefold()
    return "not found" in message and (
        "replied" in message
        or "reply" in message
        or "reply_to_message" in message
    )


def _bad_request_is_html_parse_error(exc: BadRequest) -> bool:
    message = str(exc).casefold()
    return "parse" in message and (
        "entity" in message or "entities" in message or "html" in message
    )


def _reply_chunks(
    text: str, parse_mode: str | None, limit: int = TELEGRAM_TEXT_MESSAGE_LIMIT
) -> tuple[list[str], str | None]:
    if parse_mode == "HTML":
        if not _validate_telegram_html(text):
            return _telegram_text_chunks(_strip_telegram_html(text), limit), None
        visible = _strip_telegram_html(text)
        if telegram_text_units(visible) > limit:
            return _telegram_text_chunks(visible, limit), None
        return [text], parse_mode
    return _telegram_text_chunks(text, limit), parse_mode


def _telegram_text_chunks(text: str, limit: int = TELEGRAM_TEXT_MESSAGE_LIMIT) -> list[str]:
    """Split plain Telegram replies before Bot API rejects messages >4096 chars."""
    return split_telegram_text(text, limit=limit)


def _validate_telegram_html(text: str) -> bool:
    return validate_telegram_html(text)


def _strip_telegram_html(text: str) -> str:
    return strip_telegram_html(text)


def create_application(
    token: str,
    db_path: str | Path,
    config: BotConfig | None = None,
    chat_settings: ChatSettings | None = None,
) -> Application:
    config = config or BotConfig.from_env()
    _validate_llm_config_env()
    if chat_settings is None:
        chat_settings = ChatSettings(_chat_settings_path_from_env(db_path))
    app = (
        ApplicationBuilder()
        .token(token)
        .post_shutdown(_close_translator_on_shutdown)
        .build()
    )
    app.bot_data[SERVICE_KEY] = TermService(db_path)
    translator = SentenceTranslator(
        db_path,
        llm_timeout_seconds=config.llm_timeout_seconds,
        llm_max_concurrency=config.llm_max_concurrency,
    )
    app.bot_data[TRANSLATOR_KEY] = translator
    app.bot_data[CONFIG_KEY] = config
    app.bot_data[RATE_LIMITER_KEY] = PerChatRateLimiter(config.rate_limit_per_minute)
    app.bot_data[REJECT_LIMITER_KEY] = PerChatRateLimiter(config.rate_limit_per_minute)
    app.bot_data[ADMIN_CACHE_KEY] = AdminStatusCache()
    app.bot_data[CHANNEL_REPLY_INDEX_KEY] = ChannelReplyIndex(
        ttl_seconds=config.channel_max_age_seconds,
        storage_path=_channel_reply_index_path(config, db_path),
    )
    app.bot_data[CHANNEL_RUNTIME_KEY] = ChannelRuntime(
        max_active=config.llm_max_concurrency,
        max_pending=config.channel_max_pending,
        llm_calls_per_minute=config.channel_llm_calls_per_minute,
    )
    app.bot_data[CHAT_SETTINGS_KEY] = chat_settings
    app.add_handler(CommandHandler(["tr", "term"], term_command, block=False))
    app.add_handler(CommandHandler(["sentence", "sent"], sentence_command, block=False))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("public", public_command))
    app.add_handler(CommandHandler("grant", grant_command))
    app.add_handler(CommandHandler("revoke", revoke_command))
    app.add_handler(
        MessageHandler(
            filters.IS_AUTOMATIC_FORWARD & filters.SenderChat.CHANNEL,
            channel_post_handler,
            block=False,
        )
    )
    app.add_handler(
        ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    app.add_error_handler(_log_update_error)
    return app


async def _log_update_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Observe otherwise-silent handler failures (e.g. transient NetworkError).

    Log-only, and deliberately frames-only: exception MESSAGES can carry
    quasi-sensitive values (a chat-id-keyed KeyError, an API echo), so only
    the error type name and the traceback frames (code paths) are logged.
    """
    error = getattr(context, "error", None)
    frames = ""
    if error is not None and error.__traceback__ is not None:
        frames = "".join(traceback.format_tb(error.__traceback__))
    failed_message = getattr(update, "effective_message", None)
    LOGGER.error(
        "update processing failed error_type=%s incoming_message=%s\n%s",
        safe_error_type(error),
        redact_id(getattr(failed_message, "message_id", None)),
        frames,
    )


async def _close_translator_on_shutdown(application: Application) -> None:
    try:
        translator = application.bot_data.get(TRANSLATOR_KEY)
        if isinstance(translator, SentenceTranslator):
            await translator.aclose()
    finally:
        # Independent of the translator close above: flush queued snapshots
        # so replies remembered just before exit are durable; otherwise the
        # offloaded save task can be cancelled during loop teardown and
        # replayed posts would be retranslated on restart.
        reply_index = application.bot_data.get(CHANNEL_REPLY_INDEX_KEY)
        if isinstance(reply_index, ChannelReplyIndex):
            await reply_index.aflush()


def _chat_settings_path_from_env(db_path: str | Path) -> Path:
    """Return the settings path with an optional state-dir migration.

    Custom explicit file paths are still respected. When the supported Docker
    layout sets WUWATERM_STATE_DIR, old DB-adjacent explicit paths are treated
    as legacy deployment configuration and copied once into the writable state
    directory.
    """
    return _state_file_path(
        db_path,
        filename="chat_settings.json",
        explicit_path=os.getenv("WUWATERM_SETTINGS_PATH", "").strip() or None,
        label="chat settings",
    )


def _channel_reply_index_path(config: BotConfig, db_path: str | Path) -> Path:
    return _state_file_path(
        db_path,
        filename="channel_replies.json",
        explicit_path=config.channel_reply_index_path,
        label="channel reply index",
    )


def _state_file_path(
    db_path: str | Path,
    *,
    filename: str,
    explicit_path: str | Path | None,
    label: str,
) -> Path:
    db_parent = Path(db_path).resolve(strict=False).parent
    legacy = db_parent / filename
    state_dir = os.getenv("WUWATERM_STATE_DIR", "").strip()
    target = Path(state_dir).expanduser() / filename if state_dir else None
    if explicit_path:
        explicit = Path(explicit_path).expanduser()
        if target is not None and (
            _same_state_path(explicit, legacy)
            or _same_state_path(explicit, target)
        ):
            _migrate_legacy_state_file(legacy, target, label=label)
            return target
        return explicit
    if not state_dir:
        return legacy
    assert target is not None
    _migrate_legacy_state_file(legacy, target, label=label)
    return target


def _same_state_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _migrate_legacy_state_file(legacy: Path, target: Path, *, label: str) -> None:
    if target.exists():
        if legacy.exists():
            _validate_state_file(target, label=label)
        return
    if not legacy.exists():
        return
    if legacy.resolve(strict=False) == target.resolve(strict=False):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{target.name}.migrate.", dir=target.parent
    )
    tmp_path = Path(tmp)
    try:
        payload = legacy.read_bytes()
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        _validate_state_file(tmp_path, label=label)
        try:
            os.link(tmp_path, target)
        except FileExistsError:
            _validate_state_file(target, label=label)
            return
        _fsync_state_parent(target)
    except OSError as exc:
        raise StateMigrationError(f"could not migrate legacy {label}") from exc
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    LOGGER.info("migrated legacy %s into configured state dir", label)


def _validate_state_file(path: Path, *, label: str) -> None:
    if label == "chat settings":
        try:
            ChatSettings(path)._read_state(strict=True)
        except ChatSettingsError as exc:
            raise StateMigrationError(f"invalid {label} state file") from exc
        return
    try:
        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateMigrationError(f"invalid {label} state file") from exc
    if label != "channel reply index":
        if not isinstance(payload, dict):
            raise StateMigrationError(f"invalid {label} state file")
        return
    try:
        parse_channel_reply_payload(payload)
    except ChannelReplyPayloadError as exc:
        raise StateMigrationError(f"invalid {label} state file") from exc


def _fsync_state_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_bot(db_path: str | Path, token: str | None = None) -> None:
    logging.basicConfig(
        level=os.getenv("WUWATERM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    app = create_application(token, db_path)
    app.run_polling()


async def term_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _translation_command(update, context, usage_notice=TERM_USAGE_NOTICE)


async def sentence_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _translation_command(update, context, usage_notice=SENTENCE_USAGE_NOTICE)


async def _translation_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    usage_notice: str,
) -> None:
    message = update.effective_message
    if not message:
        return
    # Inline text wins; otherwise fall back to the replied-to message's content.
    parsed = _parse_translation_args(context.args)
    request = _translation_request(
        update, parsed.text, forced_to_chinese=parsed.forced_to_chinese
    )
    actor = await _translation_actor_or_reject(update, context)
    if actor is None:
        return
    if parsed.direction_error:
        await _reply_direction_usage(update, context)
        return
    if actor.rate_limited and not _consume_rate_limit(update, context):
        await reply_to_user(update, THROTTLE_NOTICE)
        return
    if not request.text:
        await reply_to_user(update, usage_notice)
        return
    service: TermService = context.application.bot_data[SERVICE_KEY]
    translator: SentenceTranslator = context.application.bot_data[TRANSLATOR_KEY]
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    translated = await telegram_translation_reply(
        service,
        translator,
        request,
        input_limit=_translation_input_limit(config, request, actor),
    )
    if not await _passes_delivery_gate(update, context):
        LOGGER.info(
            "translation reply skipped: authorization changed before delivery "
            "chat=%s incoming_message=%s",
            redact_id(chat_id_for(update)),
            redact_id(getattr(message, "message_id", None)),
        )
        return
    await reply_to_user(
        update,
        translated.text,
        parse_mode=translated.parse_mode,
        # A flood-wait can outlive a /revoke or /public off; re-check before
        # the retry actually delivers the translation.
        retry_gate=lambda: _passes_delivery_gate(update, context),
    )


async def public_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only switch: open translate commands to all group members, or
    restrict them back to admins. Works in groups only.

    Subcommands: on | off | status (empty = status). The /public command
    itself ALWAYS requires admin — public mode does not unlock it, so a
    non-admin can never flip it back off.
    """
    message = update.effective_message
    if not message:
        return
    if not is_group_chat(update):
        await reply_to_user(update, PUBLIC_ONLY_GROUPS_NOTICE)
        return
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    # Fresh admin check (no cache): a control-plane toggle must not honor a
    # stale positive verdict left by a just-demoted/removed admin.
    if not await _is_group_admin(update, context, use_cache=False):
        # Re-use the reject limiter so non-admin /public spam can't flood the
        # chat or starve translation budget; respect the silent override.
        if not config.tr_reject_silent and _consume_reject_limit(update, context):
            await reply_to_user(update, PUBLIC_REJECT_NOTICE)
        return
    # Deliberately NOT gated by the translation rate limiter: an admin must
    # always be able to close a busy/spammed public chat even when the per-chat
    # translation budget is exhausted.
    chat = update.effective_chat
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    arg = context.args[0].strip().lower() if context.args else ""
    if arg in {"on", "open", "enable"}:
        if await _persist_public(update, settings, chat.id, True):
            await reply_to_user(update, PUBLIC_ENABLED_NOTICE)
    elif arg in {"off", "close", "disable"}:
        if await _persist_public(update, settings, chat.id, False):
            await reply_to_user(update, PUBLIC_DISABLED_NOTICE)
    elif arg in {"", "status"}:
        await reply_to_user(
            update,
            PUBLIC_STATUS_ON if settings.is_public(chat.id) else PUBLIC_STATUS_OFF,
        )
    else:
        await reply_to_user(update, PUBLIC_USAGE_NOTICE)


async def _persist_public(
    update: Update, settings: ChatSettings, chat_id: int, value: bool
) -> bool:
    """Persist a public-mode change and surface storage failures.

    ChatSettings applies operation-specific fail-closed memory semantics before
    an error reaches this boundary; the command reports failure either way.
    """
    try:
        settings.set_public(chat_id, value)
        return True
    except ChatSettingsDurabilityError as exc:
        LOGGER.warning(
            "settings durability uncertain on /public error_type=%s",
            safe_error_type(exc),
        )
        await reply_to_user(update, SETTINGS_DURABILITY_UNCERTAIN_NOTICE)
        return False
    except OSError as exc:
        LOGGER.warning(
            "settings save failed on /public error_type=%s", safe_error_type(exc)
        )
        await reply_to_user(
            update,
            SETTINGS_SAVE_FAILED_NOTICE
            if value
            else SETTINGS_DENY_NOT_PERSISTED_NOTICE,
        )
        return False


def _chat_member_is_in(member) -> bool:
    """Whether a ChatMember object means the bot is actually IN the chat.

    RESTRICTED can be is_member True (in the chat) or False (not in it), so it
    must be classified by is_member rather than status alone; the other "in"
    statuses imply membership and left/kicked imply non-membership.
    """
    status = getattr(member, "status", None)
    if status in {ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER}:
        return True
    if status == ChatMember.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


def _is_owner(update: Update, config: BotConfig) -> bool:
    """True iff the sender is the configured bot owner (fail-closed when unset)."""
    user = update.effective_user
    return (
        user is not None
        and config.owner_user_id is not None
        and user.id == config.owner_user_id
    )


def _bot_added_to_chat(update: Update) -> bool:
    """True only on the genuine 'added' edge: the bot went from not-a-member to
    a-member.

    Membership is computed with is_member (a RESTRICTED member can be a
    non-member), so a restricted-non-member -> member transition counts as an
    add and a left -> restricted-non-member transition does not. Promotions,
    demotions, and removals inside a chat the bot already belongs to do NOT
    count — this protects an already-joined authorized group from being
    auto-left on an unrelated status change.
    """
    cmu = getattr(update, "my_chat_member", None)
    if cmu is None:
        return False
    return not _chat_member_is_in(cmu.old_chat_member) and _chat_member_is_in(
        cmu.new_chat_member
    )


async def _leave_chat_quietly(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    """leave_chat that never raises — the bot may already be absent from the
    target chat (e.g. /revoke <id> for a chat it is not currently in)."""
    try:
        await context.bot.leave_chat(chat_id)
    except TelegramError as exc:
        LOGGER.warning(
            "leave_chat failed chat=%s error_type=%s",
            redact_id(chat_id),
            safe_error_type(exc),
        )


class _PersistenceOutcome(Enum):
    PERSISTED = "persisted"
    DURABILITY_UNCERTAIN = "durability-uncertain"
    FAILED = "failed"


def _try_persist(action, chat_id: int) -> _PersistenceOutcome:
    """Run a settings mutation and classify its commit visibility.

    A durability error means replace succeeded and the candidate is visible,
    while a normal OSError means the mutation did not reach that guarantee.
    """
    try:
        action(chat_id)
        return _PersistenceOutcome.PERSISTED
    except ChatSettingsDurabilityError as exc:
        LOGGER.warning(
            "settings durability uncertain chat=%s error_type=%s",
            redact_id(chat_id),
            safe_error_type(exc),
        )
        return _PersistenceOutcome.DURABILITY_UNCERTAIN
    except OSError as exc:
        LOGGER.warning(
            "settings write failed chat=%s error_type=%s",
            redact_id(chat_id),
            safe_error_type(exc),
        )
        return _PersistenceOutcome.FAILED


async def my_chat_member_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Group authorization gate. When the bot is added to a chat that is not on
    the allowlist and was not added by the owner, send a bilingual notice then
    leave. Owner-added chats auto-authorize and stay; already-authorized chats
    stay silently."""
    if not _bot_added_to_chat(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in {"group", "supergroup", "channel"}:
        return
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    adder = getattr(update.my_chat_member, "from_user", None)
    if (
        config.owner_user_id is not None
        and adder is not None
        and adder.id == config.owner_user_id
    ):
        outcome = _try_persist(settings.allow, chat.id)
        if outcome is _PersistenceOutcome.FAILED:
            # Could not persist the authorization (disk full / read-only). Fail
            # closed: leave rather than stay in a chat we cannot remember
            # authorizing. The owner can re-add once the file is writable.
            LOGGER.warning(
                "owner-add authorization could not be persisted, leaving chat=%s",
                redact_id(chat.id),
            )
            await _leave_chat_quietly(context, chat.id)
            return
        if outcome is _PersistenceOutcome.DURABILITY_UNCERTAIN:
            LOGGER.warning(
                "owner-add authorization visible but durability uncertain chat=%s",
                redact_id(chat.id),
            )
            try:
                await context.bot.send_message(
                    chat.id, SETTINGS_DURABILITY_UNCERTAIN_NOTICE
                )
            except TelegramError as exc:
                LOGGER.warning(
                    "owner-add durability notice failed error_type=%s",
                    safe_error_type(exc),
                )
        LOGGER.info(
            "added by owner; chat authorized chat=%s type=%s",
            redact_id(chat.id),
            chat.type,
        )
        return
    if settings.is_allowed(chat.id):
        LOGGER.info(
            "added to authorized chat chat=%s type=%s", redact_id(chat.id), chat.type
        )
        return
    LOGGER.info(
        "added to unauthorized chat, leaving chat=%s type=%s",
        redact_id(chat.id),
        chat.type,
    )
    try:
        await context.bot.send_message(chat.id, UNAUTHORIZED_GROUP_NOTICE)
    except TelegramError as exc:
        LOGGER.warning(
            "unauthorized-leave notice failed error_type=%s", safe_error_type(exc)
        )
    await _leave_chat_quietly(context, chat.id)


def _grant_success_text(chat_id: int, *, current_chat: bool) -> str:
    heading = (
        "已授权本群\nThis chat is granted."
        if current_chat
        else "已授权\nGranted."
    )
    return f"{heading}\n\n{chat_id}"


def _grant_list_text(chat_ids: list[int]) -> str:
    body = "\n".join(str(cid) for cid in chat_ids) if chat_ids else "（空）"
    return (
        f"授权名单 · {len(chat_ids)}\n"
        f"Granted chats\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"{GRANT_USAGE}"
    )


async def grant_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: add a chat to the group authorization allowlist. In a group,
    /grant allows the current chat; in private, /grant <chat_id> allows
    by id and /grant (or 'list') shows the list. Non-owners get no reply."""
    message = update.effective_message
    if not message:
        return
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    if not _is_owner(update, config):
        return  # owner-only; does not advertise itself to others
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    arg = context.args[0].strip().lower() if context.args else ""
    if is_group_chat(update) and not arg:
        cid = update.effective_chat.id
        outcome = _try_persist(settings.allow, cid)
        if outcome is _PersistenceOutcome.FAILED:
            await reply_to_user(update, SETTINGS_SAVE_FAILED_NOTICE)
            return
        if outcome is _PersistenceOutcome.DURABILITY_UNCERTAIN:
            await reply_to_user(update, SETTINGS_DURABILITY_UNCERTAIN_NOTICE)
            return
        await reply_to_user(update, _grant_success_text(cid, current_chat=True))
        return
    if arg in {"", "list"}:
        await reply_to_user(update, _grant_list_text(settings.allowed_chats()))
        return
    try:
        target = int(arg)
    except ValueError:
        await reply_to_user(update, GRANT_USAGE)
        return
    outcome = _try_persist(settings.allow, target)
    if outcome is _PersistenceOutcome.FAILED:
        await reply_to_user(update, SETTINGS_SAVE_FAILED_NOTICE)
        return
    if outcome is _PersistenceOutcome.DURABILITY_UNCERTAIN:
        await reply_to_user(update, SETTINGS_DURABILITY_UNCERTAIN_NOTICE)
        return
    await reply_to_user(update, _grant_success_text(target, current_chat=False))


async def revoke_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Owner-only: remove a chat from the allowlist AND make the bot leave it,
    so revoking actually stops service (the allowlist otherwise only gates
    joining). In a group, /revoke targets the current chat; in private,
    /revoke <chat_id> targets by id. Non-owners get no reply."""
    message = update.effective_message
    if not message:
        return
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    if not _is_owner(update, config):
        return
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    arg = context.args[0].strip().lower() if context.args else ""
    if is_group_chat(update) and not arg:
        cid = update.effective_chat.id
        outcome = _try_persist(settings.disallow, cid)
        if outcome is _PersistenceOutcome.PERSISTED:
            note = f"已撤销本群授权并退出本群（chat_id={cid}）\nRevoked; leaving this chat (chat_id={cid})."
        elif outcome is _PersistenceOutcome.DURABILITY_UNCERTAIN:
            note = (
                f"撤销已应用但持久性不确定，仍退出本群（chat_id={cid}）；请稍后重试确认。\n"
                f"Revocation is visible but durability is uncertain; leaving anyway (chat_id={cid}) — retry later."
            )
        else:
            note = (
                f"未能保存撤销，仍退出本群（chat_id={cid}）；请稍后重试 /revoke。\n"
                f"Couldn't persist the de-authorization; leaving anyway (chat_id={cid}) — please re-run /revoke later."
            )
        # Best-effort reply BEFORE leaving (the bot cannot post once it leaves);
        # the leave MUST still run even if the reply fails.
        try:
            await reply_to_user(update, note)
        except TelegramError as exc:
            LOGGER.warning(
                "revoke confirmation reply failed chat=%s error_type=%s",
                redact_id(cid),
                safe_error_type(exc),
            )
        await _leave_chat_quietly(context, cid)
        return
    try:
        target = int(arg)
    except ValueError:
        await reply_to_user(update, REVOKE_USAGE)
        return
    outcome = _try_persist(settings.disallow, target)
    await _leave_chat_quietly(context, target)
    if outcome is _PersistenceOutcome.PERSISTED:
        note = f"已撤销并退出 / Revoked and left chat_id={target}"
    elif outcome is _PersistenceOutcome.DURABILITY_UNCERTAIN:
        note = (
            "撤销已应用但持久性不确定；已退出，请稍后重试确认 / "
            f"Revocation is visible but durability is uncertain; left chat_id={target}, retry later."
        )
    else:
        note = (
            "已退出但未能保存撤销，请稍后重试 / "
            f"Left, but couldn't persist; re-run /revoke later (chat_id={target})."
        )
    await reply_to_user(update, note)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only diagnostics: data source, pinned commit, term count, limits.

    No auth gate (anyone may ask), no throttle, zero LLM calls.
    """
    message = update.effective_message
    if not message:
        return
    service: TermService = context.application.bot_data[SERVICE_KEY]
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    await reply_to_user(update, _about_text(service, config))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only runtime status. Counts only; no chat ids or secrets."""
    message = update.effective_message
    if not message:
        return
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    if not _is_owner(update, config):
        return
    service: TermService = context.application.bot_data[SERVICE_KEY]
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    reply_index: ChannelReplyIndex = context.application.bot_data[
        CHANNEL_REPLY_INDEX_KEY
    ]
    channel_runtime = context.application.bot_data.get(CHANNEL_RUNTIME_KEY)
    if not isinstance(channel_runtime, ChannelRuntime):
        channel_runtime = ChannelRuntime(
            max_active=config.llm_max_concurrency,
            max_pending=config.channel_max_pending,
            llm_calls_per_minute=config.channel_llm_calls_per_minute,
        )
        context.application.bot_data[CHANNEL_RUNTIME_KEY] = channel_runtime
    await reply_to_user(
        update,
        _status_text(service, config, settings, reply_index, channel_runtime),
    )


def _about_text(service: TermService, config: BotConfig) -> str:
    meta = service.metadata()
    profile = meta.get("source_profile") or "unknown"
    repo = meta.get("source_repo_url")
    commit = meta.get("wutheringdata_commit") or "unknown (not recorded in DB)"
    source = f"{profile} ({repo})" if repo else profile
    llm = "configured" if _llm_configured() else "not configured"
    return (
        "wuwaterm /about\n"
        f"Data source: {source}\n"
        f"Pinned commit: {commit}\n"
        f"Dictionary terms: {service.term_count()}\n"
        f"Rate limit: {config.rate_limit_per_minute}/min per chat\n"
        f"LLM: {llm}"
    )


def _status_text(
    service: TermService,
    config: BotConfig,
    settings: ChatSettings,
    reply_index: ChannelReplyIndex,
    channel_runtime: ChannelRuntime,
) -> str:
    meta = service.metadata()
    commit = meta.get("wutheringdata_commit") or "unknown"
    short_commit = commit[:12] if re.fullmatch(r"[0-9a-f]{40}", commit) else commit
    profile = meta.get("source_profile") or "unknown"
    llm = "yes" if _llm_configured() else "no"
    channel_auto = "on" if config.channel_autotranslate else "off"
    # Counting prunes expired entries; if that triggers a persistence write,
    # the health fields below report the refreshed state.
    tracked_channel_posts = reply_index.entry_count()
    reply_index_persistence = "on" if reply_index.persistence_enabled() else "off"
    channel_snapshot = channel_runtime.snapshot()
    channel_outcomes = ",".join(
        f"{key}={value}" for key, value in channel_snapshot.outcomes.items()
    ) or "none"
    return (
        "wuwaterm /status\n"
        f"Dictionary terms: {service.term_count()}\n"
        f"Data profile: {profile}\n"
        f"Data commit: {short_commit}\n"
        f"LLM configured: {llm}\n"
        f"Channel autotranslate: {channel_auto}\n"
        f"Tracked channel posts: {tracked_channel_posts}\n"
        f"Channel reply persistence: {reply_index_persistence}\n"
        f"Channel reply load failures: {reply_index.load_failure_count()}\n"
        f"Channel reply last load: {_reply_index_last_load_status(reply_index)}\n"
        f"Channel reply save failures: {reply_index.save_failure_count()}\n"
        f"Channel reply last save: {_reply_index_last_save_status(reply_index)}\n"
        f"Channel translation active: {channel_snapshot.active}\n"
        f"Channel translation pending: {channel_snapshot.pending}\n"
        f"Channel translation high-water: {channel_snapshot.high_water}\n"
        f"Channel translation outcomes: {channel_outcomes}\n"
        f"Channel pending cap: {config.channel_max_pending}\n"
        f"Channel LLM call budget: {config.channel_llm_calls_per_minute}/min\n"
        f"Authorized chats: {settings.allowed_count()}\n"
        f"Public chats: {settings.public_count()}\n"
        f"Rate limit: {config.rate_limit_per_minute}/min per chat\n"
        f"Public LLM input limit: {LLM_INPUT_CHAR_LIMIT}\n"
        f"Trusted/channel text limit: {config.channel_text_limit}\n"
        f"Trusted/channel caption limit: {config.channel_caption_limit}\n"
        f"Telegram reply limit: {TELEGRAM_TEXT_MESSAGE_LIMIT}"
    )


def _reply_index_last_load_status(reply_index: ChannelReplyIndex) -> str:
    if not reply_index.persistence_enabled():
        return "not configured"
    last_load = reply_index.last_load_succeeded()
    if last_load is None:
        return "not attempted"
    return "ok" if last_load else "failed"


def _reply_index_last_save_status(reply_index: ChannelReplyIndex) -> str:
    if not reply_index.persistence_enabled():
        return "not configured"
    last_save = reply_index.last_save_succeeded()
    if last_save is None:
        return "not attempted"
    if last_save and reply_index.last_save_durable() is False:
        return "durability uncertain"
    return "ok" if last_save else "failed"


def _telegram_text(outcome: TranslationOutcome) -> str:
    """Telegram wording for a protocol-neutral outcome.

    The bilingual dictionary-miss flag is Telegram's published wording, so the
    application layer only reports the boolean and the flag text stays here.
    """
    if outcome.dictionary_miss:
        return f"{outcome.text}\n\n{DICT_MISS_FLAG}"
    return outcome.text


def _telegram_reply(outcome: TranslationOutcome) -> TranslationReply:
    text = _telegram_text(outcome)
    if outcome.markup_used:
        if _validate_telegram_html(text):
            return TranslationReply(text, parse_mode="HTML")
        return TranslationReply(_strip_telegram_html(text))
    return TranslationReply(text)


def _telegram_llm_splitter(text: str, limit: int) -> list[str]:
    """UTF-16 aware splitter so Telegram length semantics stay unchanged."""
    return split_telegram_text(text, limit=limit)


def _telegram_markup_translator(translator: SentenceTranslator):
    """Adapter hook: translate Telegram HTML with its markup preserved."""

    async def translate_markup(markup: str, *, to_chinese: bool) -> MarkupTranslation:
        if not _llm_configured():
            # Without an LLM there is no markup-preserving call to make; the
            # plain path yields exactly the same term-locked text.
            return MarkupTranslation(fallback_to_plain=True)
        try:
            translated = await translator.translate_html_async(
                markup, to_chinese=to_chinese
            )
        except LLMTranslationError as exc:
            reason = getattr(exc, "reason", "translation_unavailable")
            LOGGER.warning("tr html translation failed reason=%s", reason)
            if reason not in HTML_CONTENT_FAILURE_REASONS:
                return MarkupTranslation(
                    message=exc.user_message,
                    error_code=error_code_for_llm_reason(reason),
                )
            # The model broke the placeholder structure (or returned nothing).
            # A plain retry drops formatting but still answers the command.
            return MarkupTranslation(fallback_to_plain=True)
        return MarkupTranslation(text=translated)

    return translate_markup


def translate_query(
    service: TermService,
    translator: SentenceTranslator,
    query: str,
    *,
    forced_to_chinese: bool | None = None,
) -> str:
    """Telegram-facing plain-text wrapper over the shared pipeline."""
    outcome = translate_request(
        service,
        translator,
        TranslationJob(text=query, forced_to_chinese=forced_to_chinese),
    )
    return _telegram_text(outcome)


async def translate_query_async(
    service: TermService,
    translator: SentenceTranslator,
    query: str,
    *,
    forced_to_chinese: bool | None = None,
) -> str:
    """Async Telegram-facing plain-text wrapper over the shared pipeline."""
    outcome = await translate_request_async(
        service,
        translator,
        TranslationJob(text=query, forced_to_chinese=forced_to_chinese),
        splitter=_telegram_llm_splitter,
        # The dictionary stage (exact + full-table fuzzy over sqlite) is
        # synchronous; keep it off the event loop like the API does.
        offload=asyncio.to_thread,
    )
    return _telegram_text(outcome)


async def telegram_translation_reply(
    service: TermService,
    translator: SentenceTranslator,
    request: TranslationRequest,
    *,
    input_limit: int = LLM_INPUT_CHAR_LIMIT,
) -> TranslationReply:
    """Run the shared pipeline with the Telegram markup and splitter hooks."""
    outcome = await translate_request_async(
        service,
        translator,
        TranslationJob(
            text=request.text,
            markup=request.html,
            forced_to_chinese=request.forced_to_chinese,
        ),
        input_limit=input_limit,
        markup_translator=_telegram_markup_translator(translator),
        splitter=_telegram_llm_splitter,
        offload=asyncio.to_thread,
    )
    return _telegram_reply(outcome)


def _translation_input_limit(
    config: BotConfig, request: TranslationRequest, actor: TranslationActor
) -> int:
    if actor.trusted:
        return (
            config.channel_caption_limit
            if request.is_caption
            else config.channel_text_limit
        )
    return LLM_INPUT_CHAR_LIMIT


async def _reply_direction_usage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # Invalid flag replies are user-visible but should not spend translation
    # budget. Use the reject limiter so public chats cannot flood usage notices.
    if _consume_reject_limit(update, context):
        await reply_to_user(update, DIRECTION_USAGE_NOTICE)
    else:
        LOGGER.info(
            "direction usage notice suppressed (reject budget) chat=%s",
            redact_id(chat_id_for(update)),
        )


async def _translation_actor_or_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> TranslationActor | None:
    actor = await _translation_actor(update, context)
    if actor is not None:
        return actor
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    # Rejection replies ride a SEPARATE per-chat budget: non-admin /tr spam can
    # neither starve the translation budget authorized callers depend on, nor
    # flood the chat. Within budget -> reply (respecting tr_reject_silent);
    # beyond budget -> silent. The translation limiter is never touched here.
    if not config.tr_reject_silent and _consume_reject_limit(update, context):
        reject_text = (
            config.group_tr_reject_text
            if is_group_chat(update)
            else config.private_tr_reject_text
        )
        await reply_to_user(update, reject_text)
    else:
        # Suppressed rejection (silent flag or reject budget exhausted): leave
        # a trace so rejection floods are visible in a log review.
        chat = update.effective_chat
        LOGGER.info(
            "tr rejected without reply chat=%s chat_type=%s silent_flag=%s",
            redact_id(chat_id_for(update)),
            chat.type if chat else "unknown",
            config.tr_reject_silent,
        )
    return None


async def _translation_actor(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> TranslationActor | None:
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    if is_group_chat(update):
        chat = update.effective_chat
        if chat is None:
            return None
        settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
        if not settings.is_allowed(chat.id):
            return None
        if _is_owner(update, config):
            return TranslationActor("owner", trusted=True, rate_limited=False)
        if await _is_group_admin(update, context):
            return TranslationActor("group_admin", trusted=True, rate_limited=False)
        message = update.effective_message
        if (
            update.effective_user is None
            or getattr(message, "sender_chat", None) is not None
        ):
            return None
        if settings.is_public(chat.id):
            return TranslationActor("public_member", trusted=False, rate_limited=True)
        return None
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        # Channels and any other chat type are outside the command surface.
        return None
    if _is_owner(update, config):
        return TranslationActor("owner", trusted=True, rate_limited=False)
    return None


async def _passes_delivery_gate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Silent pre-send auth check for non-blocking translation tasks.

    A slow LLM call can finish after /public off or /revoke has already changed
    the chat policy. Initial rejection remains user-visible; this late gate is
    deliberately silent and consumes no reject or translation budget.
    """
    if is_group_chat(update):
        chat = update.effective_chat
        if chat is None:
            return False
        settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
        if not settings.is_allowed(chat.id):
            return False
        config: BotConfig = context.application.bot_data[CONFIG_KEY]
        if _is_owner(update, config):
            return True
        if await _is_group_admin(update, context, use_cache=False):
            return True
        message = update.effective_message
        if (
            update.effective_user is None
            or getattr(message, "sender_chat", None) is not None
        ):
            return False
        return settings.is_public(chat.id)
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return False
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    return _is_owner(update, config)


async def _is_authorized_sender(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if is_group_chat(update):
        return await _is_authorized_group_sender(update, context)
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        # Channels and any other chat type are outside the service surface.
        return False
    user = update.effective_user
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    return (
        user is not None
        and config.owner_user_id is not None
        and user.id == config.owner_user_id
    )


async def _is_group_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    use_cache: bool = True,
) -> bool:
    """Pure admin check (no public-mode bypass).

    Used by /public itself — public mode must never let a non-admin flip
    the switch back off. Also the base layer of _is_authorized_group_sender.

    Control-plane callers (the /public toggle) pass ``use_cache=False`` so a
    just-demoted admin cannot keep changing chat-wide access on a stale
    5-minute cache verdict. The data plane (/tr) keeps the cache for cost.
    """
    chat = update.effective_chat
    message = update.effective_message
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None and chat is not None and sender_chat.id == chat.id:
        # Anonymous group admins post as the group itself.
        return True
    user = update.effective_user
    if chat is None or user is None:
        return False
    cache: AdminStatusCache = context.application.bot_data[ADMIN_CACHE_KEY]
    if use_cache:
        cached = cache.get(chat.id, user.id)
        if cached is not None:
            return cached
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except NetworkError as exc:
        # A transient blip must not reject a real admin (or silently discard
        # an already-computed translation at the delivery gate): retry once,
        # then fail closed without caching. TimedOut subclasses NetworkError.
        LOGGER.warning(
            "get_chat_member failed error_type=%s; retrying once",
            safe_error_type(exc),
        )
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except TelegramError as retry_exc:
            LOGGER.warning(
                "get_chat_member retry failed error_type=%s",
                safe_error_type(retry_exc),
            )
            return False
    except TelegramError as exc:
        # Fail closed without caching; ids stay out of logs (quasi-sensitive).
        LOGGER.warning("get_chat_member failed error_type=%s", safe_error_type(exc))
        return False
    verdict = getattr(member, "status", None) in ADMIN_ALLOWED_STATUSES
    cache.put(chat.id, user.id, verdict)
    return verdict


async def _is_authorized_group_sender(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Authorization for translate commands in a group.

    The chat must be on the allowlist at all: the bot only serves groups it is
    authorized for, so any chat it should have left (an unauthorized add, a
    /revoke, or a leave that failed) stops being served even before the bot is
    actually removed. Within an allowlisted chat: admins always; non-admins only
    when an admin has flipped /public on.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    if not settings.is_allowed(chat.id):
        return False
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    if _is_owner(update, config):
        return True
    if await _is_group_admin(update, context):
        return True
    # Public mode opens the door only for ORDINARY member messages — never for
    # a foreign sender_chat identity (a channel posting as itself, or a
    # linked-channel auto-forward whose text happens to start with /tr). The
    # anonymous group-admin case (sender_chat == chat.id) is allowed above.
    message = update.effective_message
    if (
        update.effective_user is None
        or getattr(message, "sender_chat", None) is not None
    ):
        return False
    return settings.is_public(chat.id)


def _consume_rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = chat_id_for(update)
    if chat_id is None:
        return True
    limiter: PerChatRateLimiter = context.application.bot_data[RATE_LIMITER_KEY]
    return limiter.allow(chat_id)


def _consume_reject_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Per-chat budget for unauthorized-rejection replies, separate from the
    translation budget so reject spam cannot starve authorized translations."""
    chat_id = chat_id_for(update)
    if chat_id is None:
        return True
    limiter: PerChatRateLimiter = context.application.bot_data[REJECT_LIMITER_KEY]
    return limiter.allow(chat_id)
