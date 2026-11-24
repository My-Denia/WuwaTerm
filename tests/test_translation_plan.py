"""Tests for TranslationPlan, RequestPolicy, ProviderProfile, and request/operation identities."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.test_api_semantic_review import configure_llm
from wuwaterm import llm_transport
from wuwaterm.application import ContextBefore, Precedent, TranslationContext
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.models import TermRecord
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.term_preparation import TermOccurrence, TermReference
from wuwaterm.translation_plan import (
    DEFAULT_PROVIDER_PROFILE,
    ProviderProfile,
    RequestPolicy,
    SegmentExecutionBudget,
    TranslationPlan,
    build_translation_request_spec,
)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "terms.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "2"), ("source_commit", "test-commit")),
        )
        insert_records(
            conn,
            [
                TermRecord("resonator", "fixture", "1", "1", "今汐", "Jinhsi"),
                TermRecord("item", "fixture", "2", "2", "星声", "Astrite"),
            ],
        )
        conn.commit()
    return db_path


def test_provider_profile_wire_mapping_and_transport_options():
    deepseek = ProviderProfile()
    assert deepseek.reasoning_extra_body("omit") == {}
    assert deepseek.reasoning_extra_body("disabled") == {"thinking": {"type": "disabled"}}
    assert deepseek.reasoning_extra_body("low") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert deepseek.reasoning_extra_body("high") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }

    omit_all = ProviderProfile(name="no_reasoning_gateway", thinking_protocol="omit_all")
    assert omit_all.reasoning_extra_body("disabled") == {}
    assert omit_all.reasoning_extra_body("low") == {}

    plan = TranslationPlan(
        source_text="你好",
        prepared_text="你好",
        locked_text="你好",
        locks=(),
        references=(),
        to_chinese=False,
    )
    omit_spec = build_translation_request_spec(
        plan,
        policy=RequestPolicy(configured_thinking_zh_en="omit"),
        provider_profile=deepseek,
        model="m",
    )
    assert omit_spec.transport_options() == {}
    assert omit_spec.wire_payload() == {
        "model": "m",
        "messages": [
            {"role": "system", "content": omit_spec.system_prompt},
            {"role": "user", "content": "你好"},
        ],
        "temperature": 0,
    }

    timed_spec = build_translation_request_spec(
        plan,
        policy=RequestPolicy(configured_thinking_zh_en="disabled"),
        provider_profile=deepseek,
        model="m",
        timeout_override=12.5,
    )
    assert timed_spec.transport_options() == {
        "extra_body": {"thinking": {"type": "disabled"}},
        "timeout_seconds": 12.5,
    }


def test_wire_payload_rejects_extra_body_collision():
    plan = TranslationPlan(
        source_text="你好",
        prepared_text="你好",
        locked_text="你好",
        locks=(),
        references=(),
        to_chinese=False,
    )
    spec = build_translation_request_spec(
        plan,
        policy=RequestPolicy(),
        model="m",
    )
    bad = type(spec)(
        model=spec.model,
        messages=spec.messages,
        temperature=spec.temperature,
        extra_body={"model": "override"},
        timeout_seconds=None,
        direction=spec.direction,
        thinking_mode=spec.thinking_mode,
        configured_thinking_mode=spec.configured_thinking_mode,
    )
    with pytest.raises(ValueError, match="extra_body may not replace model"):
        bad.wire_payload()


def test_request_policy_fallback_repair_and_segment_budget():
    auto_policy = RequestPolicy(configured_thinking_en_zh="auto", timeout_seconds=45.0)
    assert auto_policy.resolve_initial_thinking_mode(
        to_chinese=True, locked_text="Short line.", lock_count=1
    ) == "low"
    assert auto_policy.resolve_initial_thinking_mode(
        to_chinese=True, locked_text="Short line.", lock_count=4
    ) == "disabled"
    assert auto_policy.allows_timeout_fallback(to_chinese=True, initial_thinking_mode="low")
    assert not auto_policy.allows_timeout_fallback(
        to_chinese=True, initial_thinking_mode="disabled"
    )
    assert not auto_policy.allows_timeout_fallback(to_chinese=False, initial_thinking_mode="low")

    assert auto_policy.timeout_fallback_seconds(None) == 20.0
    assert auto_policy.timeout_fallback_seconds(19.0) == 17.0
    assert auto_policy.timeout_fallback_seconds(9.5) is None

    assert (
        auto_policy.paragraph_repair_thinking_mode(
            to_chinese=True, initial_thinking_mode="disabled", lock_count=2
        )
        == "low"
    )
    assert (
        auto_policy.paragraph_repair_thinking_mode(
            to_chinese=True, initial_thinking_mode="disabled", lock_count=4
        )
        == "disabled"
    )
    explicit_disabled = RequestPolicy(configured_thinking_en_zh="disabled")
    assert (
        explicit_disabled.paragraph_repair_thinking_mode(
            to_chinese=True, initial_thinking_mode="disabled", lock_count=0
        )
        == "disabled"
    )
    assert auto_policy.paragraph_repair_timeout_seconds(14.9) == (False, None)
    assert auto_policy.paragraph_repair_timeout_seconds(20.0) == (True, 18.0)

    budget = SegmentExecutionBudget(max_corrective_calls=1)
    assert budget.can_use_corrective_call
    budget.consume_corrective_call()
    assert not budget.can_use_corrective_call
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.consume_corrective_call()


def test_request_vs_operation_identity_separation_and_random_lock_rules():
    plan = TranslationPlan(
        source_text="你好",
        prepared_text="你好",
        locked_text="你好",
        locks=(),
        references=(TermReference(source="声骸", zh="声骸", en="Echo", category="core_term"),),
        to_chinese=False,
        dictionary_identity="db-digest-1",
        matcher_revision="matcher-1",
    )
    policy_a = RequestPolicy(
        configured_thinking_zh_en="disabled",
        max_corrective_calls=1,
        paragraph_regroup_enabled=True,
    )
    policy_b = RequestPolicy(
        configured_thinking_zh_en="disabled",
        max_corrective_calls=0,
        paragraph_regroup_enabled=False,
    )
    spec_a = build_translation_request_spec(plan, policy=policy_a, model="m")
    spec_b = build_translation_request_spec(plan, policy=policy_b, model="m")

    op_a = spec_a.operation_identity(policy_a, DEFAULT_PROVIDER_PROFILE)
    op_b = spec_b.operation_identity(policy_b, DEFAULT_PROVIDER_PROFILE)
    assert op_a.request.request_sha256 == op_b.request.request_sha256
    assert op_a.request.exact_reuse_allowed is True
    assert op_a.policy_sha256 != op_b.policy_sha256
    assert op_a.operation_sha256 != op_b.operation_sha256

    # Changing dictionary identity or matcher revision keeps request_sha256 equal
    # when the wire request is unchanged, while separating operation_sha256.
    plan_other_db = TranslationPlan(
        source_text="你好",
        prepared_text="你好",
        locked_text="你好",
        locks=(),
        references=(TermReference(source="声骸", zh="声骸", en="Echo", category="core_term"),),
        to_chinese=False,
        dictionary_identity="db-digest-2",
        matcher_revision="matcher-1",
    )
    spec_other_db = build_translation_request_spec(
        plan_other_db, policy=policy_a, model="m"
    )
    op_other_db = spec_other_db.operation_identity(policy_a, DEFAULT_PROVIDER_PROFILE)
    assert op_other_db.request.request_sha256 == op_a.request.request_sha256
    assert op_other_db.operation_sha256 != op_a.operation_sha256

    # Random placeholder nonces keep normalized_request_sha256 equal while keeping
    # exact request_sha256 distinct and forbidding exact reuse.
    locked_1 = TranslationPlan(
        source_text="Astrite",
        prepared_text="Astrite",
        locked_text="__WUWA_TERM_1111111111111111_0000__",
        locks=(("__WUWA_TERM_1111111111111111_0000__", "星声", "Astrite"),),
        references=(),
        to_chinese=True,
    )
    locked_2 = TranslationPlan(
        source_text="Astrite",
        prepared_text="Astrite",
        locked_text="__WUWA_TERM_2222222222222222_0000__",
        locks=(("__WUWA_TERM_2222222222222222_0000__", "星声", "Astrite"),),
        references=(),
        to_chinese=True,
    )
    id_1 = build_translation_request_spec(
        locked_1, policy=policy_a, model="m"
    ).request_identity(effective_timeout_seconds=45.0)
    id_2 = build_translation_request_spec(
        locked_2, policy=policy_a, model="m"
    ).request_identity(effective_timeout_seconds=45.0)
    assert id_1.exact_reuse_allowed is False
    assert id_1.request_sha256 != id_2.request_sha256
    assert id_1.normalized_request_sha256 == id_2.normalized_request_sha256


def test_translator_plan_and_spec_match_wire_transport(tmp_path: Path, monkeypatch):
    db_path = _make_db(tmp_path)
    configure_llm(monkeypatch, semantic_enabled=False)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "plan-test-model")
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_ZH_EN", "disabled")

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Jinhsi arrived."}}]}
        )

    translator = SentenceTranslator(
        db_path,
        llm_transport=httpx.MockTransport(handler),
        en_zh_protocol=True,
    )
    try:
        # Exact dictionary hit skips planning an LLM call.
        exact, exact_plan = translator.plan_translation("今汐", to_chinese=False)
        assert exact == "Jinhsi"
        assert exact_plan is None

        ctx = TranslationContext(
            before=ContextBefore(source="前置", target="Before"),
            precedents=(
                Precedent(
                    surface="星声",
                    choice="official_pair",
                    rendering="Astrite",
                    snippet="星声",
                    segment_index=0,
                    source_start=0,
                    source_end=2,
                ),
            ),
        )
        ans, plan = translator.plan_translation(
            "今汐来了。", to_chinese=False, context=ctx
        )
        assert ans is None
        assert plan is not None
        spec = translator.build_request_spec(plan)

        out = asyncio.run(
            translator.translate_async(
                "今汐来了。", to_chinese=False, context=ctx
            )
        )
        assert out == "Jinhsi arrived."
        assert len(captured) == 1
        assert captured[0] == spec.wire_payload()
    finally:
        asyncio.run(translator.aclose())
        assert llm_transport.resolve_llm_config()[2] == "plan-test-model"


def test_scoped_speaker_disambiguation_in_plan_messages():
    plan = TranslationPlan(
        source_text="椿和Tuner",
        prepared_text="椿和Tuner",
        locked_text="椿和Tuner",
        locks=(),
        references=(
            TermReference(
                source="椿",
                zh="椿",
                en="Camellya",
                category="resonator",
                occurrences=(TermOccurrence(0, 1, "椿", 0, 0, 1),),
                reason="single_char_name_mention",
            ),
            TermReference(
                source="Tuner",
                zh="调音师",
                en="Tuner",
                category="speaker",
                occurrences=(TermOccurrence(2, 7, "Tuner", 0, 2, 7),),
            ),
            TermReference(
                source="Tuner",
                zh="调谐器",
                en="Tuner",
                category="item",
                occurrences=(TermOccurrence(2, 7, "Tuner", 0, 2, 7),),
            ),
        ),
        to_chinese=False,
    )
    spec = build_translation_request_spec(plan, policy=RequestPolicy(), model="m")
    assert "Scoped term references:" in spec.system_prompt
    assert "(speaker)" in spec.system_prompt


def test_direction_scoped_operation_identity():
    zh_en_plan = TranslationPlan(
        source_text="你好",
        prepared_text="你好",
        locked_text="你好",
        locks=(),
        references=(),
        to_chinese=False,
    )
    en_zh_plan = TranslationPlan(
        source_text="Hello.",
        prepared_text="Hello.",
        locked_text="Hello.",
        locks=(),
        references=(),
        to_chinese=True,
    )
    policy_omit = RequestPolicy(configured_thinking_en_zh="omit")
    policy_auto = RequestPolicy(configured_thinking_en_zh="auto")

    zh_spec_omit = build_translation_request_spec(zh_en_plan, policy=policy_omit, model="m")
    zh_spec_auto = build_translation_request_spec(zh_en_plan, policy=policy_auto, model="m")
    zh_op_omit = zh_spec_omit.operation_identity(policy_omit, DEFAULT_PROVIDER_PROFILE)
    zh_op_auto = zh_spec_auto.operation_identity(policy_auto, DEFAULT_PROVIDER_PROFILE)
    assert zh_op_omit.request.request_sha256 == zh_op_auto.request.request_sha256
    assert zh_op_omit.policy_sha256 == zh_op_auto.policy_sha256
    assert zh_op_omit.operation_sha256 == zh_op_auto.operation_sha256

    en_spec_omit = build_translation_request_spec(
        en_zh_plan,
        policy=policy_omit,
        model="m",
        thinking_mode_override="disabled",
    )
    en_spec_auto = build_translation_request_spec(
        en_zh_plan,
        policy=policy_auto,
        model="m",
        thinking_mode_override="disabled",
    )
    en_op_omit = en_spec_omit.operation_identity(policy_omit, DEFAULT_PROVIDER_PROFILE)
    en_op_auto = en_spec_auto.operation_identity(policy_auto, DEFAULT_PROVIDER_PROFILE)
    assert en_op_omit.request.request_sha256 == en_op_auto.request.request_sha256
    assert en_op_omit.policy_sha256 != en_op_auto.policy_sha256
    assert en_op_omit.operation_sha256 != en_op_auto.operation_sha256


def test_en_zh_auto_retry_ignores_hostile_failure_reason(tmp_path: Path, monkeypatch):
    db_path = _make_db(tmp_path)
    configure_llm(monkeypatch, semantic_enabled=False)
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_EN_ZH", "auto")

    class HostileObject:
        def __hash__(self) -> int:
            raise RuntimeError("boom")

        def __eq__(self, other: object) -> bool:
            raise RuntimeError("boom")

        def __str__(self) -> str:
            raise RuntimeError("boom")

    class RaisingReasonError(llm_transport.LLMTranslationError):
        def __init__(self) -> None:
            RuntimeError.__init__(self, "unavailable")
            self.user_message = "unavailable"

        @property
        def reason(self) -> str:
            raise RuntimeError("boom")

    async def fail_with_hostile(*args, **kwargs):
        raise RaisingReasonError()

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fail_with_hostile)
    translator = SentenceTranslator(db_path)
    try:
        with pytest.raises(RaisingReasonError):
            asyncio.run(
                translator.translate_async(
                    "Should this short line retry safely?",
                    to_chinese=True,
                    propagate_errors=True,
                )
            )

        async def fail_with_object(*args, **kwargs):
            raise llm_transport.LLMTranslationError(
                "unavailable", reason=HostileObject()  # type: ignore[arg-type]
            )

        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fail_with_object)
        with pytest.raises(llm_transport.LLMTranslationError):
            asyncio.run(
                translator.translate_async(
                    "Should this short line retry safely?",
                    to_chinese=True,
                    propagate_errors=True,
                )
            )
    finally:
        asyncio.run(translator.aclose())
