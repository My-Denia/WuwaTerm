from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from wuwaterm import llm_transport
from wuwaterm.application import (
    KIND_ERROR,
    MarkupTranslation,
    OperationExecutionBudget,
    SemanticReviewJob,
    TranslationContext,
    TranslationJob,
    build_term_service,
    semantic_review_async,
    translate_request_async,
)
from wuwaterm.bot import (
    ADMIN_CACHE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    AdminStatusCache,
    BotConfig,
    ChannelReplyIndex,
    PerChatRateLimiter,
    REJECT_LIMITER_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
)
from wuwaterm.channel import _translate_channel_input, channel_post_handler
from wuwaterm.llm_transport import LLMTranslationError
from wuwaterm.lookup import TermService
from wuwaterm.request_trace import current as current_trace, reset as reset_trace, start as start_trace
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.settings import ChatSettings
from wuwaterm.telegram_text import split_telegram_text
from wuwaterm.translation_policy import (
    HTML_CONTENT_FAILURE_REASONS,
    LLM_INPUT_CHAR_LIMIT,
)


SMALL_PARAGRAPH_SOURCE = "The title line.\n\nFirst body line.\nSecond body line."
MERGED_PARAGRAPH_ANSWER = "标题行 第一行 合并了\n第二行"


def _set_llm_env(monkeypatch, *, thinking_en_zh: str = "auto") -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "https://budget.invalid/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "sk-budget-test")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "budget-test-model")
    monkeypatch.delenv("WUWATERM_TRANSLATION_THINKING_ZH_EN", raising=False)
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_EN_ZH", thinking_en_zh)


def _mock_transport(script, recorded: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        recorded.append(
            {
                "system": body["messages"][0]["content"],
                "user": body["messages"][-1]["content"],
                "reasoning": {
                    key: body[key]
                    for key in ("thinking", "reasoning_effort")
                    if key in body
                },
            }
        )
        reply = script[min(len(recorded) - 1, len(script) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return httpx.MockTransport(handler)


def test_operation_execution_budget_bounds_and_transport_no_double_count(monkeypatch):
    _set_llm_env(monkeypatch)
    single = OperationExecutionBudget.for_segments(1)
    assert single.max_total_calls == 2
    assert single.max_corrective_calls == 1
    assert single.max_corrective_calls_per_segment == 1

    multi = OperationExecutionBudget.for_segments(3)
    assert multi.max_total_calls == 6
    assert multi.max_corrective_calls == 3

    now = [100.0]
    budget = OperationExecutionBudget(
        max_total_calls=2,
        max_corrective_calls=1,
        timeout_seconds=10.0,
        clock=lambda: now[0],
    )
    assert budget.remaining_calls == 2
    assert budget.own_remaining_seconds() == pytest.approx(10.0)

    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with budget.activate():
                with budget.call_attempt():
                    await llm_transport.post_chat_completion(
                        client,
                        [{"role": "user", "content": "one"}],
                        purpose="translation",
                    )
                assert budget.calls_used == 1
                assert budget.remaining_calls == 1

                # Direct transport call without call_attempt still counts once.
                await llm_transport.post_chat_completion(
                    client,
                    [{"role": "user", "content": "two"}],
                    purpose="translation",
                )
                assert budget.calls_used == 2
                assert budget.remaining_calls == 0

                with pytest.raises(LLMTranslationError) as exhausted:
                    await llm_transport.post_chat_completion(
                        client,
                        [{"role": "user", "content": "three"}],
                        purpose="translation",
                    )
                assert exhausted.value.reason == "operation_budget"

    asyncio.run(scenario())
    assert posts == 2

    # Cancellation & deadline checks
    cancel_budget = OperationExecutionBudget(max_total_calls=3)
    cancel_budget.cancel("user_stop")
    assert cancel_budget.is_cancelled is True
    assert cancel_budget.cancel_reason == "user_stop"
    with pytest.raises(LLMTranslationError) as cancelled:
        cancel_budget.check_can_continue()
    assert cancelled.value.reason == "cancelled"

    now[0] = 115.0
    assert budget.deadline_expired() is True
    with pytest.raises(LLMTranslationError) as timed_out:
        budget.check_can_continue()
    assert timed_out.value.reason == "timeout"


def test_my483_translate_request_html_fallback_plus_en_zh_auto_timeout_caps_at_two_posts(
    monkeypatch, sample_db
):
    _set_llm_env(monkeypatch, thinking_en_zh="auto")
    service = build_term_service(sample_db)
    posts: list[dict] = []
    # Call 1 (HTML) breaks tag structure -> html_integrity; Call 2 (plain fallback
    # under auto=low) times out -> must NOT issue a 3rd disabled retry call.
    translator = SentenceTranslator(
        sample_db,
        llm_transport=_mock_transport(
            ["broken plain output without placeholders", httpx.ReadTimeout("slow")],
            posts,
        ),
    )

    async def markup_translator(
        markup: str, *, to_chinese: bool, before_llm_call=None
    ) -> MarkupTranslation:
        try:
            translated = await translator.translate_html_async(
                markup, to_chinese=to_chinese, before_llm_call=before_llm_call
            )
        except LLMTranslationError as exc:
            if exc.reason in HTML_CONTENT_FAILURE_REASONS:
                return MarkupTranslation(fallback_to_plain=True)
            return MarkupTranslation(message=exc.user_message, error_code="llm_unavailable")
        return MarkupTranslation(text=translated)

    start_trace()
    try:
        outcome = asyncio.run(
            translate_request_async(
                service,
                translator,
                TranslationJob(
                    text="One short English line.",
                    markup="<b>One short English line.</b>",
                ),
                markup_translator=markup_translator,
            )
        )
        trace = current_trace()
        assert trace is not None
        assert trace.html_fallback_attempts == 1
        assert trace.timeout_fallback_attempts == 0
        assert trace.budget_exhausted == 1
    finally:
        reset_trace()
        asyncio.run(translator.aclose())

    assert len(posts) == 2
    assert outcome.kind == KIND_ERROR
    assert outcome.llm_failure is not None
    assert outcome.llm_failure.reason == "timeout"


def test_my483_shared_budget_stops_paragraph_repair_after_html_fallback(
    monkeypatch, sample_db
):
    _set_llm_env(monkeypatch, thinking_en_zh="auto")
    posts: list[dict] = []
    translator = SentenceTranslator(
        sample_db,
        llm_transport=_mock_transport(
            [MERGED_PARAGRAPH_ANSWER, "should never run"],
            posts,
        ),
    )
    budget = OperationExecutionBudget.for_segments(1)

    start_trace()
    try:
        assert budget.try_consume_html_fallback() is True
        result = asyncio.run(
            translator.translate_async(
                SMALL_PARAGRAPH_SOURCE,
                to_chinese=True,
                context=TranslationContext(),
                execution_budget=budget,
            )
        )
        trace = current_trace()
        assert trace is not None
        assert trace.html_fallback_attempts == 1
        assert trace.paragraph_repair_attempts == 0
        assert trace.budget_exhausted == 1
    finally:
        reset_trace()
        asyncio.run(translator.aclose())

    assert len(posts) == 1
    assert budget.calls_used == 1
    assert result == MERGED_PARAGRAPH_ANSWER


def test_my483_channel_html_fallback_and_plain_reservation_alignment(
    monkeypatch, sample_db
):
    _set_llm_env(monkeypatch, thinking_en_zh="auto")

    # 1. Direct _translate_channel_input: HTML fail -> plain fallback -> auto timeout <= 2 POSTs.
    html_posts: list[dict] = []
    html_translator = SentenceTranslator(
        sample_db,
        llm_transport=_mock_transport(
            ["broken without tags", httpx.ReadTimeout("slow")],
            html_posts,
        ),
    )
    try:
        with pytest.raises(LLMTranslationError) as caught:
            asyncio.run(
                _translate_channel_input(
                    html_translator,
                    "Short English channel text.",
                    "<b>Short English channel text.</b>",
                    to_chinese=True,
                    before_llm_call=None,
                    allow_plain_fallback=True,
                )
            )
        assert caught.value.reason == "timeout"
        assert caught.value.failure_stage == "plain-fallback"
        assert len(html_posts) == 2
    finally:
        asyncio.run(html_translator.aclose())

    # 2. channel_post_handler plain text (required_calls=1) + auto timeout:
    # must stay at 1 POST and never raise ChannelAdmission RuntimeError.
    plain_posts: list[dict] = []
    plain_translator = SentenceTranslator(
        sample_db,
        llm_transport=_mock_transport([httpx.ReadTimeout("slow")], plain_posts),
    )
    config = BotConfig(
        rate_limit_per_minute=10,
        owner_user_id=11,
        channel_autotranslate=True,
        channel_llm_calls_per_minute=10,
    )
    chat_settings = ChatSettings(sample_db.parent / "chat_settings.json")
    chat_settings.allow(-2001)
    message = SimpleNamespace(
        message_id=5010,
        text="This is a plain English channel announcement.",
        caption=None,
        text_html="",
        caption_html="",
        sender_chat=SimpleNamespace(id=-3001, type="channel"),
        is_automatic_forward=True,
        date=datetime.now(timezone.utc),
        edit_date=None,
        replies=[],
    )
    update = SimpleNamespace(
        update_id=9001,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = SimpleNamespace(
        args=[],
        bot=SimpleNamespace(),
        application=SimpleNamespace(
            bot_data={
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: plain_translator,
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=10),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=10),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(ttl_seconds=3600),
                CHAT_SETTINGS_KEY: chat_settings,
            }
        ),
    )

    try:
        asyncio.run(channel_post_handler(update, context))
    finally:
        asyncio.run(plain_translator.aclose())

    assert len(plain_posts) == 1


def test_long_split_channel_post_with_html_text_blocks_timeout_fallback_leak(
    monkeypatch, sample_db, caplog
):
    _set_llm_env(monkeypatch, thinking_en_zh="auto")
    part1 = ("Chunk one English paragraph. " * 45).strip()
    part2 = ("Chunk two English paragraph. " * 45).strip()
    long_plain = f"{part1}\n\n{part2}"
    assert len(long_plain) > LLM_INPUT_CHAR_LIMIT
    assert len(split_telegram_text(long_plain, limit=LLM_INPUT_CHAR_LIMIT)) == 2

    posts: list[dict] = []
    translator = SentenceTranslator(
        sample_db,
        llm_transport=_mock_transport(
            [httpx.ReadTimeout("slow"), "should never steal chunk 2 token"],
            posts,
        ),
    )
    config = BotConfig(
        rate_limit_per_minute=10,
        owner_user_id=11,
        channel_autotranslate=True,
        channel_llm_calls_per_minute=10,
    )
    chat_settings = ChatSettings(sample_db.parent / "chat_settings.json")
    chat_settings.allow(-2002)
    message = SimpleNamespace(
        message_id=5011,
        text=long_plain,
        caption=None,
        text_html=f"<b>{long_plain}</b>",
        caption_html="",
        sender_chat=SimpleNamespace(id=-3002, type="channel"),
        is_automatic_forward=True,
        date=datetime.now(timezone.utc),
        edit_date=None,
        replies=[],
    )
    update = SimpleNamespace(
        update_id=9002,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2002, type="supergroup"),
        effective_user=None,
    )
    context = SimpleNamespace(
        args=[],
        bot=SimpleNamespace(),
        application=SimpleNamespace(
            bot_data={
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: translator,
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=10),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=10),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(ttl_seconds=3600),
                CHAT_SETTINGS_KEY: chat_settings,
            }
        ),
    )

    with caplog.at_level(logging.INFO):
        try:
            asyncio.run(channel_post_handler(update, context))
        finally:
            asyncio.run(translator.aclose())

    assert len(posts) == 1
    assert any(
        "stage=llm reason=timeout" in rec.message for rec in caplog.records
    )
    assert not any(
        "reason=operation_budget" in rec.message for rec in caplog.records
    )


def test_long_plain_text_respects_call_cap_cancel_deadline_and_task_cancel(
    monkeypatch, sample_db
):
    _set_llm_env(monkeypatch, thinking_en_zh="disabled")
    service = build_term_service(sample_db)
    long_text = ("A" * 1100) + "\n\n" + ("B" * 1100)

    # 1. Explicit max_total_calls=1 stops segment 2 with operation_budget.
    cap_posts: list[dict] = []
    cap_translator = SentenceTranslator(
        sample_db, llm_transport=_mock_transport(["seg1", "seg2"], cap_posts)
    )
    try:
        cap_outcome = asyncio.run(
            translate_request_async(
                service,
                cap_translator,
                TranslationJob(text=long_text, forced_to_chinese=True),
                input_limit=5000,
                execution_budget=OperationExecutionBudget(
                    max_total_calls=1, max_corrective_calls=0
                ),
            )
        )
    finally:
        asyncio.run(cap_translator.aclose())
    assert len(cap_posts) == 1
    assert cap_outcome.kind == KIND_ERROR
    assert cap_outcome.llm_failure is not None
    assert cap_outcome.llm_failure.reason == "operation_budget"

    # 2. Cooperative cancellation inside long-text loop stops segment 2.
    from wuwaterm import application as app_mod

    cancel_posts: list[dict] = []
    cancel_translator = SentenceTranslator(
        sample_db, llm_transport=_mock_transport(["seg1", "seg2"], cancel_posts)
    )
    cancel_budget = OperationExecutionBudget.for_segments(2)

    try:
        cancel_outcome = asyncio.run(
            app_mod._translate_long_plain_text(
                cancel_translator,
                long_text,
                to_chinese=True,
                on_segment=lambda _seg, _tr: cancel_budget.cancel("stop_now"),
                execution_budget=cancel_budget,
            )
        )
    finally:
        asyncio.run(cancel_translator.aclose())
    assert len(cancel_posts) == 1
    assert cancel_outcome.kind == KIND_ERROR
    assert cancel_outcome.llm_failure is not None
    assert cancel_outcome.llm_failure.reason == "cancelled"

    # 3. Deadline expiry via monotonic clock stops before segment 2.
    now = [10.0]
    deadline_posts: list[dict] = []
    deadline_translator = SentenceTranslator(
        sample_db, llm_transport=_mock_transport(["seg1", "seg2"], deadline_posts)
    )
    deadline_budget = OperationExecutionBudget.for_segments(
        2, timeout_seconds=5.0, clock=lambda: now[0]
    )

    def advance(_seg, _tr) -> None:
        now[0] = 20.0

    try:
        deadline_outcome = asyncio.run(
            app_mod._translate_long_plain_text(
                deadline_translator,
                long_text,
                to_chinese=True,
                on_segment=advance,
                execution_budget=deadline_budget,
            )
        )
    finally:
        asyncio.run(deadline_translator.aclose())
    assert len(deadline_posts) == 1
    assert deadline_outcome.kind == KIND_ERROR
    assert deadline_outcome.llm_failure is not None
    assert deadline_outcome.llm_failure.reason == "timeout"

    # 4. Real asyncio.Task.cancel() must propagate CancelledError un-swallowed.
    async def run_task_cancel() -> None:
        entered = asyncio.Event()

        async def slow_handler(_request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.sleep(5.0)
            return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

        task_translator = SentenceTranslator(
            sample_db, llm_transport=httpx.MockTransport(slow_handler)
        )
        task = asyncio.create_task(
            translate_request_async(
                service,
                task_translator,
                TranslationJob(text="Cancel me in flight.", forced_to_chinese=True),
            )
        )
        await entered.wait()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await task_translator.aclose()

    asyncio.run(run_task_cancel())


def test_semantic_review_async_stops_on_cancelled_or_expired_budget(
    monkeypatch, sample_db
):
    _set_llm_env(monkeypatch, thinking_en_zh="disabled")
    monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", "true")
    service = build_term_service(sample_db)
    posts: list[dict] = []
    translator = SentenceTranslator(
        sample_db, llm_transport=_mock_transport(['{"status":"ok","issues":[]}'], posts)
    )
    job = SemanticReviewJob(
        source="这是测试。",
        target="This is a test.",
        direction="en",
        explain_language="zh",
    )

    cancelled_budget = OperationExecutionBudget(max_total_calls=1, max_corrective_calls=0)
    cancelled_budget.cancel("abort")
    now = [50.0]
    expired_budget = OperationExecutionBudget(
        max_total_calls=1,
        max_corrective_calls=0,
        timeout_seconds=1.0,
        clock=lambda: now[0],
    )
    now[0] = 55.0
    pre_used_budget = OperationExecutionBudget(
        max_total_calls=1, max_corrective_calls=0
    )
    with pre_used_budget.call_attempt():
        pass

    try:
        res_cancel = asyncio.run(
            semantic_review_async(
                service,
                translator,
                job,
                request_id="req-cancel",
                execution_budget=cancelled_budget,
            )
        )
        res_expired = asyncio.run(
            semantic_review_async(
                service,
                translator,
                job,
                request_id="req-expired",
                execution_budget=expired_budget,
            )
        )
        res_exhausted = asyncio.run(
            semantic_review_async(
                service,
                translator,
                job,
                request_id="req-exhausted",
                execution_budget=pre_used_budget,
            )
        )
    finally:
        asyncio.run(translator.aclose())

    assert len(posts) == 0
    assert res_cancel.model_calls == 0
    assert res_cancel.error_code == "llm_unavailable"
    assert res_cancel.llm_failure is not None
    assert res_cancel.llm_failure.reason == "cancelled"
    assert res_expired.model_calls == 0
    assert res_expired.error_code == "llm_unavailable"
    assert res_expired.llm_failure is not None
    assert res_expired.llm_failure.reason == "timeout"
    assert res_exhausted.model_calls == 0
    assert res_exhausted.error_code == "llm_unavailable"
    assert res_exhausted.llm_failure is not None
    assert res_exhausted.llm_failure.reason == "operation_budget"
