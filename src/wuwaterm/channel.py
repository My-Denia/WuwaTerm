"""Passive linked-channel auto-translation (HTML-preserving)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from telegram import Update
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from .channel_reply_index import ChannelReplyIndex, OriginalPostClaim
from .channel_runtime import ChannelRuntime
from .runtime_keys import (
    CHANNEL_CAPACITY_NOTIFIER_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
)
from .lookup import TermService, preferred_exact_entry
from .logging_utils import redact_id, safe_error_type, safe_text_len
from .normalize import count_cjk, count_latin
from .sentence import (
    LLMTranslationError,
    TRANSLATION_UNAVAILABLE_NOTICE,
    SentenceTranslator,
    llm_configured as _llm_configured,
)
from .settings import ChatSettings
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

if TYPE_CHECKING:
    from .bot import BotConfig


LOGGER = logging.getLogger(__name__)
LONG_OUTPUT_MODE_SUFFIX = "plain-split"
FLOOD_RETRY_MAX_SLEEP_SECONDS = 60.0
# Capacity rejections the owner should hear about. Content gates (language,
# length, staleness, authorization) stay silent by design - they are policy,
# not degradation.
CAPACITY_SKIP_REASONS = frozenset({"queue_full", "llm_budget"})
# One DM per window no matter how large the burst; the message carries the
# suppressed count instead.
CAPACITY_NOTIFY_COOLDOWN_SECONDS = 600.0
# After a failed DM attempt the next skip retries sooner than the full
# cooldown, so a transient Telegram blip cannot silence the alert for the
# whole window; still bounded so a persistent failure (e.g. owner never
# started the bot) does not cost one API call per dropped post.
CAPACITY_NOTIFY_FAILURE_RETRY_SECONDS = 60.0
# Headroom an edit must leave unused so a burst of edits cannot starve the
# next new post: a yielded edit only delays refreshing an already-delivered
# translation, a rejected new post means no translation appears at all.
EDIT_BUDGET_HEADROOM_CALLS = 2


class CapacitySkipNotifier:
    """Rate-limited owner DM bookkeeping for capacity-driven channel skips.

    A post dropped because the queue or LLM budget is full is invisible from
    the channel itself: nothing appears and nobody complains to the bot. The
    DM is capped at one per cooldown window; ``note_skip`` returns the
    per-reason counts since the previous notification so a burst produces one
    message that says how much was dropped instead of one message per dropped
    post. The cooldown and pending counts are committed only when the DM
    actually sends (``mark_result``): otherwise a transient send failure
    would both suppress alerts for the rest of the window and lose the failed
    counts.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = CAPACITY_NOTIFY_COOLDOWN_SECONDS,
        failure_retry_seconds: float = CAPACITY_NOTIFY_FAILURE_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._failure_retry_seconds = failure_retry_seconds
        self._clock = clock
        self._last_attempt_at: float | None = None
        self._last_attempt_ok = True
        self._pending_counts: Counter[str] = Counter()

    def note_skip(self, reason: str) -> tuple[bool, dict[str, int]]:
        """Record a skip; returns (should attempt a DM now, counts snapshot).

        The snapshot is a copy: skips accumulating per reason while the DM is
        in flight must not leak into it.
        """
        self._pending_counts[reason] += 1
        now = self._clock()
        if self._last_attempt_at is not None:
            cooldown = (
                self._cooldown_seconds
                if self._last_attempt_ok
                else self._failure_retry_seconds
            )
            if now - self._last_attempt_at < cooldown:
                return False, dict(self._pending_counts)
        self._last_attempt_at = now
        return True, dict(self._pending_counts)

    def mark_result(
        self, sent: bool, *, counted: dict[str, int] | None = None
    ) -> None:
        """Commit the outcome of a DM attempt.

        On success only the skips captured in that DM are cleared: skips that
        arrived while ``send_message`` was in flight stay pending and fold
        into the next notice instead of being silently zeroed.
        """
        self._last_attempt_ok = sent
        if sent and counted:
            for reason, amount in counted.items():
                remaining = self._pending_counts.get(reason, 0) - amount
                if remaining > 0:
                    self._pending_counts[reason] = remaining
                else:
                    self._pending_counts.pop(reason, None)


def _capacity_notifier(context: ContextTypes.DEFAULT_TYPE) -> CapacitySkipNotifier:
    notifier = context.application.bot_data.get(CHANNEL_CAPACITY_NOTIFIER_KEY)
    if isinstance(notifier, CapacitySkipNotifier):
        return notifier
    notifier = CapacitySkipNotifier()
    context.application.bot_data[CHANNEL_CAPACITY_NOTIFIER_KEY] = notifier
    return notifier


async def _notify_owner_capacity_skip(
    context: ContextTypes.DEFAULT_TYPE, config: "BotConfig", reason: str
) -> None:
    """DM the owner when a channel post is dropped for capacity reasons.

    The notice carries counts and internal reason vocabulary only - never
    post text - and its own delivery failure (owner never started the bot,
    network error) must not break the handler, so it is swallowed into the
    log with the same redaction rules as everything else.
    """
    if reason not in CAPACITY_SKIP_REASONS:
        return
    owner_user_id = getattr(config, "owner_user_id", None)
    if not owner_user_id:
        return
    notifier = _capacity_notifier(context)
    should_notify, counts = notifier.note_skip(reason)
    if not should_notify:
        return
    total = sum(counts.values())
    # The aggregate may span several reasons (cooldown or failure-retry
    # windows); attribute each count to its own reason instead of labelling
    # everything with the reason that happened to trigger this notice.
    breakdown = "、".join(f"{name} ×{n}" for name, n in sorted(counts.items()))
    text = (
        f"频道自动翻译因容量限制跳过了 {total} 条帖子（{breakdown}）。"
        "频道内不会显示任何提示，请关注交付情况；"
        "可用 /status 查看计数，需要更高吞吐时调大 "
        "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE 或 WUWATERM_CHANNEL_MAX_PENDING。"
    )
    try:
        await context.bot.send_message(chat_id=owner_user_id, text=text)
    except Exception as exc:
        # Counts and short retry re-arm stay uncommitted so the next skip
        # retries with the failed skips folded into the eventual message.
        notifier.mark_result(False)
        LOGGER.info(
            "channel capacity notice to owner failed: %s", safe_error_type(exc)
        )
        return
    notifier.mark_result(True, counted=counts)


async def send_with_flood_retry(send_callable, *, retry_gate=None):
    """Run one Telegram send/edit; on RetryAfter, wait once and retry.

    A 429 flood-wait guarantees the request was NOT executed, so a single
    retry cannot duplicate (unlike TimedOut/NetworkError, which may have
    succeeded server-side and are deliberately not retried). Granularity is
    ONE API call: retrying a whole multi-chunk emit would resend chunks that
    were already delivered. A second RetryAfter propagates to the caller's
    normal error handling.

    ``retry_gate`` (sync or async, returning truthy to proceed) re-checks
    volatile policy after the sleep: authorization can be revoked and posts
    can age out during a flood-wait, and the retry would otherwise be the
    first actual API call. A closed gate re-raises the original RetryAfter,
    which flows into the caller's existing delivery-failure handling —
    logged, silent, and never delivered.
    """
    try:
        return await send_callable()
    except RetryAfter as exc:
        raw_delay = getattr(exc, "retry_after", None)
        total_seconds = getattr(raw_delay, "total_seconds", None)
        if callable(total_seconds):  # PTB >=23 returns timedelta
            raw_delay = total_seconds()
        requested = max(
            float(raw_delay)
            if isinstance(raw_delay, (int, float))
            else 1.0,
            0.0,
        )
        # Bounded-latency trade-off: a wait beyond the cap retries early and
        # will likely re-flood (the second RetryAfter then propagates). Log
        # both values so a log review can tell "waited fully" from "gave up".
        delay = min(requested, FLOOD_RETRY_MAX_SLEEP_SECONDS)
        LOGGER.warning(
            "telegram flood wait; retrying send in %.1fs (requested %.1fs)",
            delay,
            requested,
        )
        await asyncio.sleep(delay)
        if retry_gate is not None:
            allowed = retry_gate()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                LOGGER.warning("flood-wait retry aborted: delivery gate closed")
                raise
        return await send_callable()


def _channel_runtime(
    context: ContextTypes.DEFAULT_TYPE, config: "BotConfig"
) -> ChannelRuntime:
    runtime = context.application.bot_data.get(CHANNEL_RUNTIME_KEY)
    if isinstance(runtime, ChannelRuntime):
        return runtime
    runtime = ChannelRuntime(
        max_active=config.llm_max_concurrency,
        max_pending=config.channel_max_pending,
        llm_calls_per_minute=config.channel_llm_calls_per_minute,
    )
    context.application.bot_data[CHANNEL_RUNTIME_KEY] = runtime
    return runtime


def _channel_event(
    runtime: ChannelRuntime,
    *,
    stage: str,
    reason: str,
    message=None,
    chat_id: int | None = None,
    is_edit: bool = False,
    direction: str = "unknown",
    text_len: int = 0,
    mode: str = "none",
) -> None:
    runtime.record(stage, reason)
    snapshot = runtime.snapshot()
    log = (
        LOGGER.warning
        if stage in {"llm", "delivery"} and reason not in {"started", "success"}
        else LOGGER.info
    )
    log(
        "channel_translation stage=%s reason=%s chat=%s incoming_message=%s "
        "edit=%s direction=%s text_len=%s mode=%s active=%s pending=%s high_water=%s",
        stage,
        reason,
        redact_id(chat_id),
        redact_id(getattr(message, "message_id", None)),
        is_edit,
        direction,
        text_len,
        mode,
        snapshot.active,
        snapshot.pending,
        snapshot.high_water,
    )


def _message_is_fresh(message, max_age_seconds: int) -> bool:
    message_date = getattr(message, "date", None)
    if message_date is None:
        return True
    return (
        datetime.now(timezone.utc) - message_date
    ).total_seconds() <= max_age_seconds


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate automatic forwards from the linked channel, in-thread.

    Every early exit is silent: this path never posts notices under
    channel posts (a notice comment under every post would be spam).

    An edited post updates the bot's existing reply for that post in place
    (tracked per (chat, post) in memory). If a fresh edit has no tracked reply,
    handle it as the first translatable version of that post; Telegram can
    deliver media posts before their caption/text is available.
    """
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    message = update.effective_message
    if message is None:
        _channel_event(runtime, stage="skipped", reason="no_message")
        return
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    chat_type = chat.type if chat else "unknown"
    is_edit = getattr(message, "edit_date", None) is not None
    initial_text = message.text or message.caption or ""
    _channel_event(
        runtime,
        stage="received",
        reason="update",
        message=message,
        chat_id=chat_id,
        is_edit=is_edit,
        text_len=safe_text_len(initial_text),
    )
    if not config.channel_autotranslate:
        _channel_event(
            runtime,
            stage="skipped",
            reason="disabled",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return
    # Freshness gate: Telegram replays updates (restart backlog, admin
    # promotion, queue delivery). Linked-channel content is trusted, so the
    # default window is deliberately broad and can accept a bounded restart or
    # admin-promotion backlog; the gate only blocks posts outside the configured
    # channel replay horizon.
    message_date = getattr(message, "date", None)
    if not _message_is_fresh(message, config.channel_max_age_seconds):
        _channel_event(
            runtime,
            stage="skipped",
            reason="stale",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return

    # Authorization gate (fail-closed): only translate for groups on the
    # allowlist. Slash commands are gated in _is_authorized_group_sender; this
    # mirrors it for the auto-forward path so an unauthorized or revoked group
    # the bot has not yet left (e.g. a leave_chat that failed) cannot keep
    # getting its linked-channel posts translated and burn LLM budget.
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    if chat_id is None or not settings.is_allowed(chat_id):
        _channel_event(
            runtime,
            stage="skipped",
            reason="not_authorized",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return

    # Edit dedup: an edited post (edit_date set) updates the reply already
    # sent for it. If this process already observed the post without replying,
    # a fresh edit can be the first translatable version; this covers Telegram
    # media forwards whose caption/text arrives only on the edited update.
    reply_index: ChannelReplyIndex = context.application.bot_data[
        CHANNEL_REPLY_INDEX_KEY
    ]
    resume_observed = False
    existing_reply_id: int | None = None
    if is_edit:
        existing_reply_id = (
            reply_index.get(chat_id, message.message_id)
            if chat_id is not None
            else None
        )
        if chat_id is not None:
            waited_for_original = await reply_index.wait_in_flight(
                chat_id, message.message_id
            )
            if not waited_for_original and existing_reply_id is None:
                # With a non-blocking handler, PTB may schedule an edit task
                # before the original post task has run far enough to mark
                # itself in-flight. Yield once so that already-scheduled
                # original task can register; true restart/no-entry edits still
                # skip without reaching throttle or LLM work.
                await asyncio.sleep(0)
                waited_for_original = await reply_index.wait_in_flight(
                    chat_id, message.message_id
                )
            if waited_for_original:
                existing_reply_id = reply_index.get(chat_id, message.message_id)
        if existing_reply_id is None:
            if message_date is None:
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="edit_untracked_no_date",
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text_len=safe_text_len(initial_text),
                )
                return
            if chat_id is None or not reply_index.was_observed_without_reply(
                chat_id, message.message_id
            ):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="edit_untracked",
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text_len=safe_text_len(initial_text),
                )
                return
            # message.date is the original post date, not edit_date, so the
            # freshness gate above still blocks old replayed posts. This accepts
            # a fresh edit only when this process saw the post without replying;
            # after a restart, the sentinel is gone and the edit stays skipped.
            LOGGER.info(
                "channel autotranslate treating fresh edit as new post "
                "incoming_message=%s",
                redact_id(message.message_id),
            )
            is_edit = False
            resume_observed = True
    # Edit-token registration is deferred to the moment the edit is actually
    # admitted to a delivery path (the dictionary fast path, or right after
    # the budget-yield check on the LLM path). Registering here would let an
    # edit that later yields or is content-gated supersede the token of an
    # admitted in-flight edit, whose delivery would then be dropped as stale.
    edit_work_token: int | None = None
    original_claim: OriginalPostClaim | None = None
    if not is_edit and chat_id is not None:
        claim = reply_index.claim_original(
            chat_id,
            message.message_id,
            resume_observed=resume_observed,
        )
        if claim.role == "done":
            _channel_event(
                runtime,
                stage="skipped",
                reason="duplicate",
                message=message,
                chat_id=chat_id,
                text_len=safe_text_len(initial_text),
            )
            return
        if claim.role == "waiter":
            await reply_index.wait_for_original(claim)
            _channel_event(
                runtime,
                stage="skipped",
                reason="duplicate_in_flight",
                message=message,
                chat_id=chat_id,
                text_len=safe_text_len(initial_text),
            )
            return
        original_claim = claim
    try:
        html_text: str | None
        if message.text:
            plain = message.text
            html_text = message.text_html
            length_limit = config.channel_text_limit
        elif message.caption:
            plain = message.caption
            html_text = message.caption_html
            length_limit = config.channel_caption_limit
        else:
            _channel_event(
                runtime,
                stage="skipped",
                reason="no_text",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
            )
            return
        if not html_text:
            # PTB renders non-empty HTML for any non-empty text; if that ever
            # stops holding, degrade to the plain pipeline instead of dropping
            # the post (the dictionary and LLM paths only need `plain`).
            _channel_event(
                runtime,
                stage="received",
                reason="no_html_plain_fallback",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(plain),
            )
            html_text = None
        # Direction by script: enough Chinese -> translate to English (default);
        # no Chinese but enough Latin letters -> translate to Chinese. Anything
        # else (emoji / links / numbers only) is not worth translating -> skip.
        cjk = count_cjk(plain)
        if cjk >= config.channel_min_cjk:
            to_chinese = False
        elif cjk == 0 and count_latin(plain) >= config.channel_min_latin:
            to_chinese = True
        else:
            # Free check: no LLM call and no throttle consumption.
            _channel_event(
                runtime,
                stage="skipped",
                reason="language_threshold",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(plain),
            )
            return
        direction = "to_zh" if to_chinese else "to_en"
        if len(plain) > length_limit:
            _channel_event(
                runtime,
                stage="skipped",
                reason="too_long",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return
        service: TermService = context.application.bot_data[SERVICE_KEY]
        exact = preferred_exact_entry(service, plain)
        official = None
        if exact is not None:
            official = exact.zh if to_chinese else exact.en
        if not official and not _llm_configured():
            _channel_event(
                runtime,
                stage="skipped",
                reason="llm_not_configured",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return
        if official:
            # Dictionary-first invariant: official text byte-for-byte,
            # plain, trumps formatting. Zero LLM.
            if not _passes_channel_delivery_gate(context, chat_id):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="authorization_changed",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return
            if is_edit and chat_id is not None:
                update_id = getattr(update, "update_id", None)
                edit_work_token = reply_index.begin_edit(
                    chat_id,
                    message.message_id,
                    update_id if isinstance(update_id, int) else None,
                )
            _channel_event(
                runtime,
                stage="dictionary",
                reason="exact_hit",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
                mode="dictionary",
            )
            await _emit(
                context,
                reply_index=reply_index,
                message=message,
                chat_id=chat_id,
                chat_type=chat_type,
                is_edit=is_edit,
                existing_reply_id=existing_reply_id,
                edit_work_token=edit_work_token,
                text=official,
                parse_mode=None,
                mode="dictionary",
            )
            return

        # The HTML route reserves a second call so a plain-text retry after a
        # content-shape failure (broken placeholders / blank output) can run
        # without hitting "no unused call reservation"; the unused token is
        # released when the admission exits. A per-minute budget of 1 must
        # still translate (reserve(2) would reject everything), so the
        # fallback token is only reserved when the budget allows it and the
        # fallback only runs when its token was actually reserved. The plain
        # routes need exactly one call per chunk.
        if len(plain) <= LLM_INPUT_CHAR_LIMIT:
            required_calls = (
                min(2, runtime.llm_calls_per_minute) if html_text else 1
            )
        else:
            required_calls = len(
                split_telegram_text(plain, limit=LLM_INPUT_CHAR_LIMIT)
            )
        allow_plain_fallback = bool(html_text) and required_calls >= 2
        # New posts outrank edits near budget exhaustion. The check and the
        # reserve below are both synchronous on the single event loop, so the
        # headroom reading cannot race the reservation it gates. The gate is
        # capped by the configured capacity itself: with a per-minute budget
        # of 1-3 the required+headroom sum is unreachable even on a
        # completely fresh window, which would yield every edit forever.
        if is_edit and (
            runtime.budget_remaining()
            < min(
                required_calls + EDIT_BUDGET_HEADROOM_CALLS,
                runtime.llm_calls_per_minute,
            )
        ):
            _channel_event(
                runtime,
                stage="skipped",
                reason="edit_yield",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return
        admission, rejection_reason = runtime.reserve(required_calls)
        if admission is None:
            _channel_event(
                runtime,
                stage="skipped",
                reason=rejection_reason or "admission_rejected",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            await _notify_owner_capacity_skip(
                context, config, rejection_reason or "admission_rejected"
            )
            return
        translator: SentenceTranslator = context.application.bot_data[TRANSLATOR_KEY]
        async with admission:
            if not _message_is_fresh(message, config.channel_max_age_seconds):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="stale_after_wait",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return
            if not _passes_channel_delivery_gate(context, chat_id):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="authorization_changed_after_wait",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return
            started_calls = 0

            def mark_channel_llm_call_started() -> None:
                """Recheck volatile policy after the shared LLM-slot wait."""

                nonlocal started_calls, edit_work_token

                if not _message_is_fresh(
                    message, config.channel_max_age_seconds
                ):
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE,
                        reason="stale_before_llm",
                    )
                if not _passes_channel_delivery_gate(context, chat_id):
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE,
                        reason="authorization_changed_before_llm",
                    )
                admission.mark_call_started()
                if started_calls == 0 and is_edit and chat_id is not None:
                    # The edit is now committed to its first LLM call. This is
                    # the earliest point at which registering the supersede
                    # token is safe: every earlier bail path (edit_yield,
                    # queue_full, llm_budget, stale, authorization) must leave
                    # an admitted in-flight edit's token untouched, or the
                    # admitted edit's completed translation is dropped as
                    # stale with nothing replacing it. Registration order
                    # still matches arrival order: on the single event loop
                    # the older edit's task reaches every FIFO primitive
                    # (admission semaphore, LLM slot) first.
                    update_id = getattr(update, "update_id", None)
                    edit_work_token = reply_index.begin_edit(
                        chat_id,
                        message.message_id,
                        update_id if isinstance(update_id, int) else None,
                    )
                started_calls += 1
                _channel_event(
                    runtime,
                    stage="llm",
                    reason="started",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                    mode=f"call:{started_calls}/{required_calls}",
                )

            try:
                (
                    translated,
                    translated_parse_mode,
                    translated_mode,
                ) = await _translate_channel_input(
                    translator,
                    plain,
                    html_text,
                    to_chinese=to_chinese,
                    before_llm_call=mark_channel_llm_call_started,
                    allow_plain_fallback=allow_plain_fallback,
                )
            except LLMTranslationError as exc:
                reason = getattr(exc, "reason", "translation_unavailable")
                _channel_event(
                    runtime,
                    stage=(
                        "skipped"
                        if reason
                        in {
                            "stale_before_llm",
                            "authorization_changed_before_llm",
                        }
                        else "llm"
                    ),
                    reason=reason,
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                    mode=getattr(exc, "failure_stage", "none"),
                )
                return
            _channel_event(
                runtime,
                stage="llm",
                reason="success",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
                mode=translated_mode,
            )

        if translated_parse_mode == "HTML" and not validate_telegram_html(translated):
            # The translation itself succeeded; only the markup is unusable.
            # Deliver the stripped plain text instead of dropping the reply.
            _channel_event(
                runtime,
                stage="llm",
                reason="invalid_html",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            translated = strip_telegram_html(translated)
            translated_parse_mode = None
            translated_mode = "plain-after-invalid-html"
        if translated_parse_mode == "HTML":
            plain_translated = strip_telegram_html(translated)
            if telegram_text_units(plain_translated) > TELEGRAM_TEXT_MESSAGE_LIMIT:
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return
                await _emit(
                    context,
                    reply_index=reply_index,
                    message=message,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    is_edit=is_edit,
                    existing_reply_id=existing_reply_id,
                    edit_work_token=edit_work_token,
                    text=plain_translated,
                    parse_mode=None,
                    mode=translated_mode,
                )
                return
            try:
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return
                await _emit(
                    context,
                    reply_index=reply_index,
                    message=message,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    is_edit=is_edit,
                    existing_reply_id=existing_reply_id,
                    edit_work_token=edit_work_token,
                    text=translated,
                    parse_mode="HTML",
                    mode=translated_mode,
                )
                return
            except BadRequest:
                # Safety net: never let formatting fail the reply.
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return
                await _emit(
                    context,
                    reply_index=reply_index,
                    message=message,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    is_edit=is_edit,
                    existing_reply_id=existing_reply_id,
                    edit_work_token=edit_work_token,
                    text=plain_translated,
                    parse_mode=None,
                    mode="plain-after-badrequest",
                )
                return
        if not _passes_channel_delivery_gate(context, chat_id):
            _channel_event(
                runtime,
                stage="skipped",
                reason="authorization_changed_before_delivery",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return
        await _emit(
            context,
            reply_index=reply_index,
            message=message,
            chat_id=chat_id,
            chat_type=chat_type,
            is_edit=is_edit,
            existing_reply_id=existing_reply_id,
            edit_work_token=edit_work_token,
            text=strip_telegram_html(translated)
            if translated_parse_mode == "HTML"
            else translated,
            parse_mode=None,
            mode="plain" if translated_parse_mode == "HTML" else translated_mode,
        )
    finally:
        if original_claim is not None and chat_id is not None:
            try:
                if reply_index.get(chat_id, message.message_id) is None:
                    reply_index.remember_observed_without_reply(
                        chat_id, message.message_id
                    )
            finally:
                reply_index.finish_original(original_claim)


def _passes_channel_delivery_gate(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | None
) -> bool:
    if chat_id is None:
        return False
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    return settings.is_allowed(chat_id)


def _passes_final_delivery_gate(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_id: int | None,
    is_edit: bool,
    text: str,
    mode: str,
) -> bool:
    """Recheck volatile policy immediately before a Telegram API call."""

    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    if not _message_is_fresh(message, config.channel_max_age_seconds):
        _channel_event(
            runtime,
            stage="skipped",
            reason="stale_before_delivery",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        return False
    if not _passes_channel_delivery_gate(context, chat_id):
        _channel_event(
            runtime,
            stage="skipped",
            reason="authorization_changed_before_delivery",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        return False
    return True


async def _translate_channel_input(
    translator: SentenceTranslator,
    plain: str,
    html_text: str | None,
    *,
    to_chinese: bool,
    before_llm_call,
    allow_plain_fallback: bool = True,
) -> tuple[str, str | None, str]:
    """Translate one post; LLMTranslationError carries a ``failure_stage``
    attribute (html-attempt / plain-fallback / plain-no-html /
    plain-input-split) so the caller's log event can say which call failed."""
    if len(plain) <= LLM_INPUT_CHAR_LIMIT:
        if not html_text:
            translated = await _translate_plain_stage(
                translator,
                plain,
                stage="plain-no-html",
                to_chinese=to_chinese,
                before_llm_call=before_llm_call,
            )
            return translated, None, "plain-no-html"
        try:
            return (
                await translator.translate_html_async(
                    html_text,
                    to_chinese=to_chinese,
                    before_llm_call=before_llm_call,
                ),
                "HTML",
                "HTML",
            )
        except LLMTranslationError as exc:
            if (
                getattr(exc, "reason", "") not in HTML_CONTENT_FAILURE_REASONS
                or not allow_plain_fallback
            ):
                # Either not a content failure, or no fallback token was
                # reserved (per-minute budget of 1): running the fallback
                # anyway would blow the call reservation with a RuntimeError.
                exc.failure_stage = "html-attempt"
                raise
            # The model broke the placeholder structure (or returned nothing).
            # Retrying as plain drops formatting but keeps the reply delivered;
            # before_llm_call keeps the budget/staleness/authorization rechecks.
            translated = await _translate_plain_stage(
                translator,
                plain,
                stage="plain-fallback",
                to_chinese=to_chinese,
                before_llm_call=before_llm_call,
            )
            return translated, None, "plain-after-html-failure"
    translated_chunks: list[str] = []
    for chunk in split_telegram_text(plain, limit=LLM_INPUT_CHAR_LIMIT):
        translated = await _translate_plain_stage(
            translator,
            chunk,
            stage="plain-input-split",
            to_chinese=to_chinese,
            before_llm_call=before_llm_call,
        )
        translated_chunks.append(translated)
    return "\n".join(translated_chunks), None, "plain-input-split"


async def _translate_plain_stage(
    translator: SentenceTranslator,
    text: str,
    *,
    stage: str,
    to_chinese: bool,
    before_llm_call,
) -> str:
    try:
        return await translator.translate_async(
            text,
            to_chinese=to_chinese,
            before_llm_call=before_llm_call,
            propagate_errors=True,
        )
    except LLMTranslationError as exc:
        exc.failure_stage = stage
        raise


async def _emit(
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
) -> None:
    message = kwargs["message"]
    chat_id = kwargs.get("chat_id")
    is_edit = bool(kwargs.get("is_edit"))
    text = kwargs["text"]
    mode = kwargs["mode"]
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    if not _passes_final_delivery_gate(
        context,
        message=message,
        chat_id=chat_id,
        is_edit=is_edit,
        text=text,
        mode=mode,
    ):
        return
    try:
        outcome = await _emit_unchecked(context, **kwargs)
    except BadRequest as exc:
        _channel_event(
            runtime,
            stage="delivery",
            reason=safe_error_type(exc),
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        if kwargs.get("parse_mode") == "HTML":
            raise
    except TelegramError as exc:
        _channel_event(
            runtime,
            stage="delivery",
            reason=safe_error_type(exc),
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
    else:
        if outcome == "gated":
            return
        if outcome == "stale_edit":
            _channel_event(
                runtime,
                stage="skipped",
                reason="stale_edit_before_delivery",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(text),
                mode=mode,
            )
            return
        _channel_event(
            runtime,
            stage="delivery",
            reason=outcome,
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )


async def _emit_unchecked(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_index: ChannelReplyIndex,
    message,
    chat_id: int | None,
    chat_type: str,
    is_edit: bool,
    existing_reply_id: int | None,
    edit_work_token: int | None,
    text: str,
    parse_mode: str | None,
    mode: str,
) -> str:
    """Deliver one translation, then remember its reply chunks for later edits.

    New post: send one or more in-thread replies and remember their IDs.
    Edit: update those remembered replies in place, add continuation chunks,
    or delete stale extras. On the edit path, a "message is not modified"
    error is the dedup ideal (identical re-translation) and is a silent no-op.
    An HTML edit Telegram rejects re-raises so the caller's plain fallback
    runs. A failed plain edit (reply deleted / uneditable) is swallowed; an
    edit must never crash the listener or leave a duplicate.
    """
    chunks, delivery_parse_mode, delivery_mode = _channel_delivery_chunks(
        text, parse_mode, mode
    )
    if is_edit:
        if chat_id is not None and edit_work_token is not None:
            async with reply_index.edit_delivery_lock(chat_id, message.message_id):
                if not reply_index.is_latest_edit(
                    chat_id, message.message_id, edit_work_token
                ):
                    LOGGER.info(
                        "channel edit skipped: stale translation "
                        "incoming_message=%s reply_message=%s",
                        redact_id(message.message_id),
                        redact_id(existing_reply_id),
                    )
                    return "stale_edit"
                if not _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text=text,
                    mode=delivery_mode,
                ):
                    return "gated"
                existing_reply_ids = reply_index.get_many(chat_id, message.message_id)
                if not existing_reply_ids and existing_reply_id is not None:
                    existing_reply_ids = (existing_reply_id,)
                return await _edit_reply_chunks(
                    context,
                    reply_index=reply_index,
                    message=message,
                    chat_type=chat_type,
                    existing_reply_ids=existing_reply_ids,
                    chunks=chunks,
                    chat_id=chat_id,
                    parse_mode=delivery_parse_mode,
                    mode=delivery_mode,
                )
        existing_reply_ids = (
            reply_index.get_many(chat_id, message.message_id)
            if chat_id is not None
            else ()
        )
        if not existing_reply_ids and existing_reply_id is not None:
            existing_reply_ids = (existing_reply_id,)
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=True,
            text=text,
            mode=delivery_mode,
        ):
            return "gated"
        return await _edit_reply_chunks(
            context,
            reply_index=reply_index,
            message=message,
            chat_type=chat_type,
            existing_reply_ids=existing_reply_ids,
            chunks=chunks,
            chat_id=chat_id,
            parse_mode=delivery_parse_mode,
            mode=delivery_mode,
        )

    reply_message_ids = []
    for chunk in chunks:
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=False,
            text=chunk,
            mode=delivery_mode,
        ):
            return "gated"
        sent_message = await send_with_flood_retry(
            lambda: message.reply_text(
                chunk,
                parse_mode=delivery_parse_mode,
                reply_to_message_id=message.message_id,
            ),
            retry_gate=lambda: _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=False,
                text=chunk,
                mode=delivery_mode,
            ),
        )
        reply_message_id = getattr(sent_message, "message_id", None)
        if reply_message_id is not None:
            reply_message_ids.append(reply_message_id)
            if chat_id is not None:
                reply_index.remember_many(
                    chat_id, message.message_id, tuple(reply_message_ids)
                )
        _log_emit(
            chat_type,
            message,
            reply_message_id,
            delivery_mode,
            edited=False,
            text=chunk,
        )
    return "success"


def _channel_delivery_chunks(
    text: str, parse_mode: str | None, mode: str
) -> tuple[list[str], str | None, str]:
    if parse_mode == "HTML":
        visible_text = strip_telegram_html(text)
        if telegram_text_units(visible_text) > TELEGRAM_TEXT_MESSAGE_LIMIT:
            return (
                split_telegram_text(visible_text),
                None,
                f"{mode}-{LONG_OUTPUT_MODE_SUFFIX}",
            )
        return [text], parse_mode, mode
    chunks = split_telegram_text(text)
    if len(chunks) > 1:
        return chunks, parse_mode, f"{mode}-{LONG_OUTPUT_MODE_SUFFIX}"
    return chunks, parse_mode, mode


async def _edit_reply_chunks(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_index: ChannelReplyIndex,
    message,
    chat_type: str,
    existing_reply_ids: tuple[int, ...],
    chunks: list[str],
    chat_id: int | None,
    parse_mode: str | None,
    mode: str,
) -> str:
    if chat_id is None or not existing_reply_ids:
        return "edit_target_unavailable"
    remembered_reply_ids: list[int] = []
    # Ids kept tracked only because their delete failed. Collected apart from
    # freshly updated/sent chunk ids and appended at the very end: splicing
    # them in at the position where the delete failed would map the next
    # edit's chunk onto the stale, still-uneditable reply and treat the real
    # replacement as an extra chunk to delete.
    stale_extra_ids: list[int] = []
    remaining_reply_ids = list(existing_reply_ids)
    for chunk in chunks:
        while remaining_reply_ids:
            if not _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=chunk,
                mode=mode,
            ):
                return "gated"
            reply_message_id = remaining_reply_ids.pop(0)
            edit_result = await _edit_existing_reply(
                context,
                message=message,
                chat_type=chat_type,
                existing_reply_id=reply_message_id,
                text=chunk,
                chat_id=chat_id,
                parse_mode=parse_mode,
                mode=mode,
            )
            if edit_result == "applied":
                remembered_reply_ids.append(reply_message_id)
                break
            if edit_result == "uneditable" and remembered_reply_ids:
                # A later-chunk reply that rejects edits would otherwise be
                # dropped from the rebuilt index while staying visible - the
                # orphan this path exists to prevent. Delete it whatever the
                # chunk position; keep it tracked only if the delete fails.
                failed_delete_ids = await _delete_reply_chunks(
                    context,
                    message=message,
                    chat_id=chat_id,
                    reply_message_ids=(reply_message_id,),
                )
                stale_extra_ids.extend(failed_delete_ids)
                continue
            if not remembered_reply_ids:
                # "gone" needs no delete of the failed id (it no longer
                # exists); "uneditable" must include it, or the post keeps a
                # stale translation that is no longer tracked - every later
                # edit would skip as untracked and it would never update.
                prune_ids = (
                    tuple(remaining_reply_ids)
                    if edit_result == "gone"
                    else (reply_message_id, *remaining_reply_ids)
                )
                failed_delete_ids = await _delete_reply_chunks(
                    context,
                    message=message,
                    chat_id=chat_id,
                    reply_message_ids=prune_ids,
                )
                if failed_delete_ids:
                    reply_index.remember_many(
                        chat_id, message.message_id, failed_delete_ids
                    )
                else:
                    reply_index.forget(chat_id, message.message_id)
                return "edit_target_unavailable"
        else:
            if not _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=chunk,
                mode=mode,
            ):
                return "gated"
            sent_message = await send_with_flood_retry(
                lambda: message.reply_text(
                    chunk,
                    parse_mode=parse_mode,
                    reply_to_message_id=message.message_id,
                ),
                retry_gate=lambda: _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text=chunk,
                    mode=mode,
                ),
            )
            sent_message_id: int | None = getattr(sent_message, "message_id", None)
            if sent_message_id is not None:
                remembered_reply_ids.append(sent_message_id)
                reply_index.remember_many(
                    chat_id, message.message_id, tuple(remembered_reply_ids)
                )
            _log_emit(
                chat_type,
                message,
                sent_message_id,
                mode,
                edited=False,
                text=chunk,
            )
    if remaining_reply_ids:
        failed_delete_ids = await _delete_reply_chunks(
            context,
            message=message,
            chat_id=chat_id,
            reply_message_ids=tuple(remaining_reply_ids),
        )
        stale_extra_ids.extend(failed_delete_ids)
    if remembered_reply_ids or stale_extra_ids:
        reply_index.remember_many(
            chat_id,
            message.message_id,
            tuple(remembered_reply_ids) + tuple(stale_extra_ids),
        )
    else:
        reply_index.forget(chat_id, message.message_id)
    if stale_extra_ids:
        return "stale_chunks_retained"
    return "success"


async def _delete_reply_chunks(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_id: int,
    reply_message_ids: tuple[int, ...],
) -> tuple[int, ...]:
    failed_reply_message_ids: list[int] = []
    for index, reply_message_id in enumerate(reply_message_ids):
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=True,
            text="",
            mode="edit-delete",
        ):
            failed_reply_message_ids.extend(reply_message_ids[index:])
            break
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=reply_message_id,
            )
        except BadRequest as exc:
            if _delete_error_means_already_gone(exc):
                LOGGER.info(
                    "channel edit extra chunk already gone "
                    "incoming_message=%s reply_message=%s",
                    redact_id(message.message_id),
                    redact_id(reply_message_id),
                )
                continue
            failed_reply_message_ids.append(reply_message_id)
            LOGGER.info(
                "channel edit extra chunk delete skipped "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(reply_message_id),
            )
        except TelegramError:
            failed_reply_message_ids.append(reply_message_id)
            LOGGER.info(
                "channel edit extra chunk delete skipped "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(reply_message_id),
            )
    return tuple(failed_reply_message_ids)


def _delete_error_means_already_gone(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "not found" in text or "message to delete not found" in text


async def _edit_existing_reply(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_type: str,
    existing_reply_id: int | None,
    text: str,
    chat_id: int | None,
    parse_mode: str | None,
    mode: str,
) -> Literal["applied", "gone", "uneditable"]:
    """Edit one tracked reply in place.

    The caller needs the failure shape, not just success/failure: "gone" the
    message no longer exists (nothing to clean up), "uneditable" it still
    exists but rejects the edit (it must be deleted, or it stays visible
    with stale text while no longer tracked - an orphan).
    """
    try:
        # The flood sleep here runs inside edit_delivery_lock; a newer edit
        # queues behind it and re-checks is_latest_edit after acquiring, so
        # only the volatile freshness/authorization gates need rechecking.
        await send_with_flood_retry(
            lambda: context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=existing_reply_id,
                parse_mode=parse_mode,
            ),
            retry_gate=lambda: _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=text,
                mode=mode,
            ),
        )
    except BadRequest as exc:
        lowered = str(exc).lower()
        if "not modified" in lowered:
            LOGGER.info(
                "channel edit no-op (unchanged translation) "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(existing_reply_id),
            )
            return "applied"
        if parse_mode is not None:
            # HTML edit rejected -> let the caller retry as plain.
            raise
        if "not found" in lowered:
            LOGGER.info(
                "channel edit skipped: reply already gone "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(existing_reply_id),
            )
            return "gone"
        LOGGER.info(
            "channel edit skipped: reply not updatable "
            "incoming_message=%s reply_message=%s",
            redact_id(message.message_id),
            redact_id(existing_reply_id),
        )
        return "uneditable"
    _log_emit(chat_type, message, existing_reply_id, mode, edited=True, text=text)
    return "applied"


def _log_emit(
    chat_type: str,
    message,
    reply_message_id,
    mode: str,
    *,
    edited: bool,
    text: str,
) -> None:
    LOGGER.info(
        "bot_reply chat_type=%s incoming_message=%s reply_message=%s "
        "reply_to_message=%s mode=%s edited=%s text_len=%s",
        chat_type,
        redact_id(message.message_id),
        redact_id(reply_message_id),
        redact_id(message.message_id),
        mode,
        edited,
        safe_text_len(text),
    )
