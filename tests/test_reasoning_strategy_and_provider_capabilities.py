"""Zero-cost MockTransport tests for provider capabilities, dynamic reasoning, and cancellation.

Every test runs strictly offline against ``httpx.MockTransport`` with socket
connections blocked, verifying:
1. DeepSeek official effort mapping and custom OpenAI-compatible / Neon gateway
   profiles, including exact outbound JSON wire bodies;
2. Semantic complexity routing in ``translation_plan.py`` (ambiguous /
   ordinary-word-overlapping candidates, multi-segment context, complex prose,
   ceiling bounds, explicit overrides, and timeout/shape guards);
3. Ambiguous term disambiguation context (e.g. ``安可使用了共鸣技能。`` vs
   ``The audience demanded an encore.``) keeping candidates non-locking while
   escalating reasoning effort cleanly;
4. Request metadata recording (``requested_effort``, ``effective_effort``,
   ``provider_profile``, timeout config, cancellation state, redacted with no
   secrets or user text), timeout fallback, explicit cancellation, and
   concurrency slot release.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from wuwaterm import llm_transport, request_trace
from wuwaterm.application import ContextBefore, Precedent, TranslationContext
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.document_anchor import DocumentAnchor
from wuwaterm.models import TermRecord
from wuwaterm.provider_capabilities import (
    DEEPSEEK_EFFORT_MAPPING,
    DEFAULT_PROVIDER_PROFILE,
    NEON_GATEWAY_PROVIDER_PROFILE,
    OPENAI_REASONING_PROVIDER_PROFILE,
    PROVIDER_CAPABILITIES_JSON_ENV,
    PROVIDER_PROFILE_ENV,
    ConcreteThinkingMode,
    build_custom_provider_profile,
    provider_profile_from_config,
    resolve_provider_profile_from_env,
)
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.term_preparation import TermOccurrence, TermReference
from wuwaterm.termbase import UserGlossaryReference
from wuwaterm.translation_plan import (
    TRANSLATION_THINKING_AUTO_CEILING_ENV,
    TRANSLATION_THINKING_EN_ZH_ENV,
    TRANSLATION_THINKING_ZH_EN_ENV,
    OperationExecutionBudget,
    RequestPolicy,
    TranslationPlan,
    analyze_term_planning,
    build_translation_request_spec,
    demote_ambiguous_locks,
    resolve_request_policy_from_env,
)

TEST_API_KEY = "sk-zero-cost-mock-secret-key-9999"
TEST_BASE_URL = "https://mock-gateway.invalid/v1"
TEST_MODEL = "mock-reasoning-model"


@pytest.fixture(autouse=True)
def block_network_and_configure_mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee zero outbound network calls in this entire test module."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled in zero-cost mock transport tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", TEST_BASE_URL)
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", TEST_MODEL)
    for var in (
        TRANSLATION_THINKING_ZH_EN_ENV,
        TRANSLATION_THINKING_EN_ZH_ENV,
        TRANSLATION_THINKING_AUTO_CEILING_ENV,
        PROVIDER_PROFILE_ENV,
        PROVIDER_CAPABILITIES_JSON_ENV,
        "WUWATERM_SEMANTIC_REVIEW_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_encore_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "encore_terms.db"
    with connect(db_path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "2"), ("source_commit", "mock-commit")),
        )
        insert_records(
            conn,
            [
                TermRecord("resonator", "fixture", "1", "1", "安可", "Encore"),
                TermRecord("core_term", "fixture", "2", "2", "共鸣技能", "Resonance Skill"),
                TermRecord("resonator", "fixture", "3", "3", "今汐", "Jinhsi"),
                TermRecord("item", "fixture", "4", "4", "星声", "Astrite"),
            ],
        )
        conn.commit()
    return db_path


# ---------------------------------------------------------------------------
# 1. Provider capability profiles & effort mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected_effective", "expected_omitted", "expected_passthrough", "expected_extra"),
    [
        (
            "disabled",
            "disabled",
            False,
            True,
            {"thinking": {"type": "disabled"}},
        ),
        (
            "minimal",
            "low",
            False,
            False,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        ),
        (
            "low",
            "low",
            False,
            True,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        ),
        (
            "medium",
            "high",
            False,
            False,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            "high",
            "high",
            False,
            True,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            "xhigh",
            "high",
            False,
            False,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            "max",
            "max",
            False,
            True,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        ),
        (
            "ultra",
            "max",
            False,
            False,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        ),
        (
            "omit",
            None,
            True,
            False,
            {},
        ),
    ],
)
def test_deepseek_official_effort_mapping_and_wire_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: ConcreteThinkingMode,
    expected_effective: str | None,
    expected_omitted: bool,
    expected_passthrough: bool,
    expected_extra: dict[str, Any],
) -> None:
    resolution = DEFAULT_PROVIDER_PROFILE.resolve_effort(requested)
    assert resolution.requested_effort == requested
    assert resolution.effective_effort == expected_effective
    assert resolution.omitted is expected_omitted
    assert resolution.passthrough is expected_passthrough
    assert resolution.provider_profile == "openai_compatible_deepseek"
    assert resolution.extra_body == expected_extra
    assert DEEPSEEK_EFFORT_MAPPING["minimal"] == "low"
    assert DEEPSEEK_EFFORT_MAPPING["medium"] == "high"
    assert DEEPSEEK_EFFORT_MAPPING["xhigh"] == "high"
    assert DEEPSEEK_EFFORT_MAPPING["max"] == "max"
    assert DEEPSEEK_EFFORT_MAPPING["ultra"] == "max"

    # Verify the exact outbound JSON body through SentenceTranslator + MockTransport.
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, requested)
    captured_bodies: list[dict[str, Any]] = []
    call_records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Hello."}}]}
        )

    remove_obs = llm_transport.add_call_observer(call_records.append)
    translator = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler)
    )
    try:
        out = asyncio.run(translator.translate_async("你好世界。", to_chinese=False))
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert out == "Hello."
    assert len(captured_bodies) == 1
    body = captured_bodies[0]
    reasoning_slice = {
        k: body[k] for k in ("thinking", "reasoning_effort") if k in body
    }
    assert reasoning_slice == expected_extra
    assert len(call_records) == 1
    assert call_records[0].requested_effort == requested
    assert call_records[0].effective_effort == expected_effective
    assert call_records[0].provider_profile == "openai_compatible_deepseek"
    assert call_records[0].cancelled is False


def test_custom_openai_compatible_and_neon_gateway_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Built-in OpenAI reasoning profile sends reasoning_effort without DeepSeek thinking block.
    openai_res = OPENAI_REASONING_PROVIDER_PROFILE.resolve_effort("medium")
    assert openai_res.extra_body == {"reasoning_effort": "medium"}
    assert openai_res.effective_effort == "medium"
    assert openai_res.passthrough is True
    assert OPENAI_REASONING_PROVIDER_PROFILE.resolve_effort("disabled").omitted is True

    # 2. Built-in Neon gateway preset supports disabled->none and medium/high/max.
    assert NEON_GATEWAY_PROVIDER_PROFILE.resolve_effort("disabled").extra_body == {
        "reasoning_effort": "none"
    }
    assert NEON_GATEWAY_PROVIDER_PROFILE.resolve_effort("medium").extra_body == {
        "reasoning_effort": "medium"
    }
    assert NEON_GATEWAY_PROVIDER_PROFILE.resolve_effort("max").extra_body == {
        "reasoning_effort": "max"
    }

    # 3. Declarative custom gateway via JSON env (e.g. Neon proxy with custom field & ceiling).
    custom_cfg = {
        "name": "neon_custom_gateway",
        "thinking_protocol": "custom_effort_mapping",
        "supports_structured_output": True,
        "supports_disable_thinking": True,
        "effort_field": "thought_level",
        "effort_mapping": {
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
            "max": "max",
            "ultra": "max",
        },
        "enabled_extra_body": {"reasoning_mode": "adaptive"},
        "disabled_extra_body": {"thought_level": "off"},
        "max_reasoning_effort": "high",
    }
    monkeypatch.setenv(PROVIDER_CAPABILITIES_JSON_ENV, json.dumps(custom_cfg))
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "max")

    resolved_profile = resolve_provider_profile_from_env()
    assert resolved_profile.name == "neon_custom_gateway"
    # Provider max_reasoning_effort="high" clamps requested "max" -> effective "high".
    res = resolved_profile.resolve_effort("max")
    assert res.requested_effort == "max"
    assert res.effective_effort == "high"
    assert res.passthrough is False
    assert res.extra_body == {"reasoning_mode": "adaptive", "thought_level": "high"}
    assert resolved_profile.to_identity_dict() != DEFAULT_PROVIDER_PROFILE.to_identity_dict()

    # Verify outbound wire body through SentenceTranslator (resolving profile from env).
    db_path = _make_encore_db(tmp_path)
    sent: list[dict[str, Any]] = []
    records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Translated."}}]}
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler)
    )
    try:
        asyncio.run(translator.translate_async("一段普通句子。", to_chinese=False))
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert len(sent) == 1
    assert sent[0]["thought_level"] == "high"
    assert sent[0]["reasoning_mode"] == "adaptive"
    assert "thinking" not in sent[0]
    assert "reasoning_effort" not in sent[0]
    assert records[0].provider_profile == "neon_custom_gateway"
    assert records[0].requested_effort == "max"
    assert records[0].effective_effort == "high"

    # Custom profile via helper and base_profile inheritance.
    inherited = provider_profile_from_config(
        {
            "base_profile": "neon_gateway",
            "name": "neon_tier_b",
            "effort_mapping": {"ultra": "xhigh"},
        }
    )
    assert inherited.resolve_effort("ultra").effective_effort == "xhigh"
    built = build_custom_provider_profile(
        name="custom_proxy",
        effort_mapping={"high": "deep"},
        disabled_extra_body={"reasoning_effort": "off"},
    )
    assert built.resolve_effort("high").extra_body == {"reasoning_effort": "deep"}
    assert built.resolve_effort("disabled").extra_body == {"reasoning_effort": "off"}


def test_invalid_provider_profile_env_falls_back_safely_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_garbage = "SUPER_SECRET_TOKEN_12345"
    monkeypatch.setenv(PROVIDER_CAPABILITIES_JSON_ENV, secret_garbage)
    with caplog.at_level(logging.WARNING, logger="wuwaterm.provider_capabilities"):
        profile = resolve_provider_profile_from_env()
    assert profile == DEFAULT_PROVIDER_PROFILE
    assert secret_garbage not in caplog.text


# ---------------------------------------------------------------------------
# 2. Dynamic reasoning planning & ambiguous candidate disambiguation
# ---------------------------------------------------------------------------


def test_dynamic_auto_reasoning_escalates_on_complexity_and_respects_ceiling_and_timeout() -> None:
    simple_plan = TranslationPlan(
        source_text="Good morning.",
        prepared_text="Good morning.",
        locked_text="Good morning.",
        locks=(),
        references=(),
        to_chinese=True,
    )
    policy_auto = RequestPolicy(configured_thinking_en_zh="auto", timeout_seconds=45.0)
    simple_spec = build_translation_request_spec(simple_plan, policy=policy_auto, model=TEST_MODEL)
    assert simple_spec.thinking_mode == "low"
    assert simple_spec.effective_effort == "low"

    # Competing-sense ambiguous candidate escalates to high.
    competing_plan = TranslationPlan(
        source_text="Tuner: pass me the Tuner.",
        prepared_text="Tuner: pass me the Tuner.",
        locked_text="Tuner: pass me the Tuner.",
        locks=(),
        references=(
            TermReference(
                source="Tuner",
                zh="调音师",
                en="Tuner",
                category="speaker",
                occurrences=(TermOccurrence(0, 5, "Tuner", 0, 0, 5),),
            ),
            TermReference(
                source="Tuner",
                zh="调谐器",
                en="Tuner",
                category="item",
                occurrences=(TermOccurrence(19, 24, "Tuner", 0, 19, 24),),
            ),
        ),
        to_chinese=True,
    )
    competing_spec = build_translation_request_spec(
        competing_plan, policy=policy_auto, model=TEST_MODEL
    )
    assert competing_spec.thinking_mode == "high"
    assert competing_spec.effective_effort == "high"
    assert competing_spec.reasoning_decision is not None
    assert "ambiguous_candidates" in competing_spec.reasoning_decision.reasons

    # Multi-segment context + ambiguous candidate + complex multi-clause prose:
    # under default ceiling ("high") clamps to "high"; under ceiling="max" reaches "max".
    rich_context = TranslationContext(
        before=ContextBefore(
            source="Earlier on stage, the concert ended.",
            target="此前在舞台上，演出刚刚结束。",
        ),
        precedents=(
            Precedent(
                surface="encore",
                choice="ordinary_word",
                rendering="返场",
                snippet="audience demanded an encore",
                segment_index=0,
                source_start=0,
                source_end=6,
            ),
        ),
        document_anchor=DocumentAnchor(
            topic="concert dialogue",
            section_heading="Finale",
            active_entities=("Encore",),
        ),
        user_glossary_references=(
            UserGlossaryReference(
                id="g1",
                source="standing ovation",
                target="起立鼓掌",
                direction="zh",
                kind="community_term",
            ),
        ),
    )
    complex_text = (
        'After the final note faded, the conductor asked: "Did the audience mean an encore, '
        'or were they calling out to Encore herself?" Nobody answered immediately; instead, '
        "whispered debates rippled across the balcony, because the double meaning—both a "
        "musical curtain call and a Resonator's name—changed the whole line."
    )
    complex_plan = TranslationPlan(
        source_text=complex_text,
        prepared_text=complex_text,
        locked_text=complex_text,
        locks=(),
        references=(
            TermReference(source="encore", zh="安可", en="Encore", category="resonator"),
        ),
        to_chinese=True,
        context=rich_context,
    )

    spec_high_ceiling = build_translation_request_spec(
        complex_plan, policy=policy_auto, model=TEST_MODEL
    )
    assert spec_high_ceiling.thinking_mode == "high"
    assert spec_high_ceiling.reasoning_decision is not None
    assert spec_high_ceiling.reasoning_decision.capped_by_ceiling is True
    assert "ceiling_clamped" in spec_high_ceiling.reasoning_decision.reasons

    policy_max_ceiling = RequestPolicy(
        configured_thinking_en_zh="auto",
        auto_reasoning_ceiling="max",
        timeout_seconds=45.0,
    )
    spec_max_ceiling = build_translation_request_spec(
        complex_plan, policy=policy_max_ceiling, model=TEST_MODEL
    )
    assert spec_max_ceiling.thinking_mode == "max"
    assert spec_max_ceiling.effective_effort == "max"
    assert spec_max_ceiling.extra_body == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }

    # Tight timeout budget caps high/max back down to low.
    tight_timeout_policy = RequestPolicy(
        configured_thinking_en_zh="auto",
        auto_reasoning_ceiling="max",
        timeout_seconds=18.0,
    )
    spec_tight = build_translation_request_spec(
        complex_plan, policy=tight_timeout_policy, model=TEST_MODEL
    )
    assert spec_tight.thinking_mode == "low"
    assert spec_tight.reasoning_decision is not None
    assert spec_tight.reasoning_decision.capped_by_timeout_or_budget is True
    assert "timeout_budget_guard" in spec_tight.reasoning_decision.reasons

    # Explicit operator override always wins regardless of complexity.
    explicit_disabled = RequestPolicy(configured_thinking_en_zh="disabled")
    spec_explicit = build_translation_request_spec(
        complex_plan, policy=explicit_disabled, model=TEST_MODEL
    )
    assert spec_explicit.thinking_mode == "disabled"
    assert spec_explicit.reasoning_decision is not None
    assert spec_explicit.reasoning_decision.reasons == ("explicit_override",)


def test_ambiguous_encore_contexts_provide_non_locking_candidates_and_escalate_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify '安可使用了共鸣技能。' vs 'The audience demanded an encore.' end-to-end."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")
    monkeypatch.setenv(TRANSLATION_THINKING_EN_ZH_ENV, "auto")

    captured: list[dict[str, Any]] = []
    records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        user_msg = body["messages"][-1]["content"]
        if "安可" in user_msg:
            reply = "Encore used her Resonance Skill."
        else:
            reply = "观众要求返场。"
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler)
    )
    try:
        # Case A: zh->en character context ("安可使用了共鸣技能。")
        exact_a, plan_a = translator.plan_translation(
            "安可使用了共鸣技能。", to_chinese=False
        )
        assert exact_a is None and plan_a is not None
        meta_a = plan_a.term_planning_metadata()
        assert plan_a.locks == ()  # Never force-locked as an opaque placeholder
        assert meta_a.has_ambiguous_candidates is True
        assert meta_a.lock_candidate_collisions == ()
        assert any(c.source == "安可" for c in meta_a.ambiguous_candidates)
        spec_a = translator.build_request_spec(plan_a)
        assert spec_a.thinking_mode == "high"
        assert spec_a.effective_effort == "high"
        assert "安可 = Encore (resonator)" in spec_a.system_prompt
        assert "__WUWA_TERM_" not in spec_a.user_prompt

        out_a = asyncio.run(
            translator.translate_async("安可使用了共鸣技能。", to_chinese=False)
        )
        assert out_a == "Encore used her Resonance Skill."

        # Case B: en->zh ordinary word context ("The audience demanded an encore.")
        # Even when 'encore' matches resonator candidate, it MUST be provided as a
        # non-locking contextual candidate so the model can translate it as 返场.
        exact_b, plan_b = translator.plan_translation(
            "The audience demanded an encore.", to_chinese=True
        )
        assert exact_b is None and plan_b is not None
        # Ensure 'encore' is NOT locked as a placeholder; if lowercase 'encore' is
        # not auto-matched by exact casing, verify both raw plan and candidate plan.
        assert plan_b.locks == ()
        assert "__WUWA_TERM_" not in plan_b.locked_text
        candidate_plan_b = (
            plan_b
            if plan_b.has_ambiguous_candidates
            else TranslationPlan(
                source_text=plan_b.source_text,
                prepared_text=plan_b.prepared_text,
                locked_text=plan_b.locked_text,
                locks=plan_b.locks,
                references=(
                    TermReference(
                        source="encore", zh="安可", en="Encore", category="resonator"
                    ),
                ),
                to_chinese=True,
                dictionary_identity=plan_b.dictionary_identity,
            )
        )
        meta_b = candidate_plan_b.term_planning_metadata()
        assert meta_b.has_ambiguous_candidates is True
        assert meta_b.guidance_mode == "contextual_candidates"
        assert meta_b.lock_candidate_collisions == ()
        spec_b = translator.build_request_spec(candidate_plan_b)
        assert spec_b.thinking_mode == "high"
        assert spec_b.effective_effort == "high"
        assert "ordinary English word" in spec_b.system_prompt
        assert "encore = 安可 (resonator)" in spec_b.system_prompt
        assert "__WUWA_TERM_" not in spec_b.user_prompt

        # Also test capitalized "Encore" in en->zh through full translator path:
        # resonator category stays non-locking contextual candidate AND escalates to high.
        out_b = asyncio.run(
            translator.translate_async(
                "The audience shouted Encore after the show.", to_chinese=True
            )
        )
        assert out_b == "观众要求返场。"
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert len(captured) == 2
    # Both real outbound payloads carried reasoning_effort="high" and no placeholder for Encore/安可.
    for body in captured:
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"
        assert "__WUWA_TERM_" not in body["messages"][-1]["content"]
    assert [(r.requested_effort, r.effective_effort) for r in records] == [
        ("high", "high"),
        ("high", "high"),
    ]


def test_demote_ambiguous_locks_rescues_erroneous_ambiguous_placeholder() -> None:
    placeholder = "__WUWA_TERM_1111111111111111_0000__"
    bad_plan = TranslationPlan(
        source_text="The audience demanded an Encore.",
        prepared_text="The audience demanded an Encore.",
        locked_text=f"The audience demanded an {placeholder}.",
        locks=((placeholder, "安可", "Encore"),),
        references=(),
        to_chinese=True,
    )
    safe_plan = bad_plan.without_ambiguous_locks()
    assert safe_plan.locks == ()
    assert safe_plan.locked_text == "The audience demanded an Encore."
    assert len(safe_plan.references) == 1
    assert safe_plan.references[0].source == "Encore"
    meta = safe_plan.term_planning_metadata()
    assert meta.has_ambiguous_candidates is True
    assert meta.lock_candidate_collisions == ()

    # Non-ambiguous lock stays deterministically locked while ambiguous one is demoted.
    p_astrite = "__WUWA_TERM_2222222222222222_0000__"
    text, locks, refs = demote_ambiguous_locks(
        f"{placeholder} collected {p_astrite}.",
        ((placeholder, "安可", "Encore"), (p_astrite, "星声", "Astrite")),
        (),
        to_chinese=True,
    )
    assert locks == ((p_astrite, "星声", "Astrite"),)
    assert text == f"Encore collected {p_astrite}."
    assert [r.source for r in refs] == ["Encore"]
    mixed_meta = analyze_term_planning(locks, refs, to_chinese=True)
    assert mixed_meta.guidance_mode == "mixed"
    assert mixed_meta.locked_surfaces == ("Astrite",)
    assert mixed_meta.candidate_only_surfaces == ("Encore",)


# ---------------------------------------------------------------------------
# 3. Timeout, cancellation propagation, slot release, and redacted metadata
# ---------------------------------------------------------------------------


def test_auto_high_timeout_falls_back_to_disabled_and_records_redacted_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")

    seen_bodies: list[dict[str, Any]] = []
    records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        if len(seen_bodies) == 1:
            raise httpx.ReadTimeout("first attempt timed out", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Encore used Resonance Skill."}}]},
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_timeout_seconds=45.0,
        llm_transport=httpx.MockTransport(handler),
    )
    trace = request_trace.start()
    try:
        with caplog.at_level(logging.INFO, logger="wuwaterm.llm_transport"):
            out = asyncio.run(
                translator.translate_async("安可使用了共鸣技能。", to_chinese=False)
            )
    finally:
        request_trace.reset()
        remove_obs()
        asyncio.run(translator.aclose())

    assert out == "Encore used Resonance Skill."
    assert len(seen_bodies) == 2
    assert seen_bodies[0]["reasoning_effort"] == "high"
    assert seen_bodies[1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in seen_bodies[1]

    assert len(records) == 2
    assert (records[0].outcome, records[0].requested_effort, records[0].effective_effort) == (
        "timeout",
        "high",
        "high",
    )
    assert records[0].attempt_kind == "initial"
    assert records[0].timeout_seconds == 45.0
    assert records[0].cancelled is False

    assert (records[1].outcome, records[1].requested_effort, records[1].effective_effort) == (
        "ok",
        "disabled",
        "disabled",
    )
    assert records[1].attempt_kind == "timeout_fallback"
    assert records[1].timeout_seconds == 20.0
    assert records[1].cancelled is False

    assert trace.configured_thinking == "auto"
    assert trace.initial_thinking_mode == "high"
    assert trace.provider_profile == "openai_compatible_deepseek"
    assert trace.timeout_fallback_attempts == 1
    assert trace.timeout_fallback_succeeded == 1

    # No secrets, base URL, model name, or user/model text in logs or metadata dicts.
    dumped = caplog.text + json.dumps([r.to_metadata_dict() for r in records])
    for forbidden in (TEST_API_KEY, "mock-gateway.invalid", "安可", "Encore used Resonance Skill"):
        assert forbidden not in dumped


def test_task_cancel_in_flight_propagates_releases_slot_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")

    entered = asyncio.Event()
    transport_cancelled = 0
    post_count = 0
    records: list[llm_transport.LlmCallRecord] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_cancelled, post_count
        post_count += 1
        if post_count == 1:
            entered.set()
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                transport_cancelled += 1
                raise
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Recovered ok."}}]}
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(handler),
    )

    async def scenario() -> str:
        trace = request_trace.start()
        try:
            task = asyncio.create_task(
                translator.translate_async(
                    "安可使用了共鸣技能。",
                    to_chinese=False,
                    propagate_errors=False,
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert trace.cancelled is True
            # Concurrency slot must be free immediately: a second request succeeds.
            assert translator._current_llm_slots().locked() is False
            return await translator.translate_async("你好世界。", to_chinese=False)
        finally:
            request_trace.reset()
            await translator.aclose()

    try:
        second_result = asyncio.run(scenario())
    finally:
        remove_obs()

    assert second_result == "Recovered ok."
    assert transport_cancelled == 1
    assert post_count == 2
    assert records[0].outcome == "cancelled"
    assert records[0].cancelled is True
    assert records[0].requested_effort == "high"
    assert records[0].effective_effort == "high"
    assert records[1].outcome == "ok"
    assert records[1].cancelled is False


def test_cooperative_budget_cancel_in_flight_cancels_post_releases_slot_and_skips_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")

    entered = asyncio.Event()
    transport_cancelled = 0
    post_count = 0
    records: list[llm_transport.LlmCallRecord] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transport_cancelled, post_count
        post_count += 1
        if post_count == 1:
            entered.set()
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                transport_cancelled += 1
                raise
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Next request ok."}}]}
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(handler),
    )

    async def scenario() -> tuple[str, str]:
        budget = OperationExecutionBudget.for_segments(1)
        task = asyncio.create_task(
            translator.translate_async(
                "安可使用了共鸣技能。",
                to_chinese=False,
                propagate_errors=True,
                execution_budget=budget,
            )
        )
        await entered.wait()
        budget.cancel("user_cancelled")
        with pytest.raises(llm_transport.LLMTranslationError) as caught:
            await task
        assert translator._current_llm_slots().locked() is False
        recovered = await translator.translate_async("你好世界。", to_chinese=False)
        return caught.value.reason, recovered

    try:
        reason, recovered = asyncio.run(scenario())
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert reason == "cancelled"
    assert recovered == "Next request ok."
    assert transport_cancelled == 1
    assert post_count == 2  # No retry after cooperative cancel
    assert records[0].outcome == "cancelled"
    assert records[0].cancelled is True
    assert records[0].cancel_reason == "user_cancelled"


def test_cancel_after_initial_timeout_does_not_retry_or_mask_cancel_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_EN_ZH_ENV, "auto")

    budget = OperationExecutionBudget.for_segments(1)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        budget.cancel("abort_during_timeout")
        raise httpx.ReadTimeout("slow", request=request)

    translator = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler)
    )
    trace = request_trace.start()
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as caught:
            asyncio.run(
                translator.translate_async(
                    "Short line.",
                    to_chinese=True,
                    propagate_errors=True,
                    execution_budget=budget,
                )
            )
    finally:
        request_trace.reset()
        asyncio.run(translator.aclose())

    assert posts == 1  # Never started a timeout-fallback retry after cancel
    assert caught.value.reason == "cancelled"
    assert budget.corrective_calls_used == 0
    assert trace.timeout_fallback_attempts == 0
    assert trace.cancelled is True


def test_env_auto_ceiling_and_execution_metadata_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRANSLATION_THINKING_EN_ZH_ENV, "auto")
    monkeypatch.setenv(TRANSLATION_THINKING_AUTO_CEILING_ENV, "max")
    policy = resolve_request_policy_from_env(to_chinese=True)
    assert policy.auto_reasoning_ceiling == "max"

    plan = TranslationPlan(
        source_text="The audience demanded an encore.",
        prepared_text="The audience demanded an encore.",
        locked_text="The audience demanded an encore.",
        locks=(),
        references=(
            TermReference(source="encore", zh="安可", en="Encore", category="resonator"),
        ),
        to_chinese=True,
    )
    spec = build_translation_request_spec(
        plan,
        policy=policy,
        provider_profile=NEON_GATEWAY_PROVIDER_PROFILE,
        model=TEST_MODEL,
        timeout_override=35.0,
    )
    meta = spec.execution_metadata_dict()
    assert meta["requested_effort"] == "high"
    assert meta["effective_effort"] == "high"
    assert meta["provider_profile"] == "neon_gateway"
    assert meta["timeout_seconds"] == 35.0
    assert meta["cancelled"] is False
    assert meta["term_planning"]["ambiguous_candidate_count"] == 1
    assert meta["term_planning"]["disambiguation_required"] is True
    assert "encore" not in json.dumps(meta).lower()
    assert "audience" not in json.dumps(meta).lower()
