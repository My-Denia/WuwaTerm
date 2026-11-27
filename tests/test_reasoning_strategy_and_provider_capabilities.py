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
from wuwaterm.single_char_context import ContextEvaluation, EvidenceHit
from wuwaterm.term_preparation import AmbiguousMention, TermOccurrence, TermReference
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
    # Custom provider profile names are fingerprinted rather than passed verbatim to observers.
    assert records[0].provider_profile is not None
    assert records[0].provider_profile.startswith("custom:")
    assert "neon_custom_gateway" not in records[0].provider_profile
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
        # Case A (natural end-to-end matching): zh->en character context ("安可使用了共鸣技能。")
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

        # Case B1 (natural end-to-end matching): capitalized "Encore" in en->zh stays
        # non-locking contextual candidate AND escalates auto effort to high.
        exact_b1, plan_b1 = translator.plan_translation(
            "The audience shouted Encore after the show.", to_chinese=True
        )
        assert exact_b1 is None and plan_b1 is not None
        assert plan_b1.locks == ()
        assert plan_b1.has_ambiguous_candidates is True
        out_b1 = asyncio.run(
            translator.translate_async(
                "The audience shouted Encore after the show.", to_chinese=True
            )
        )
        assert out_b1 == "观众要求返场。"

        # Case B2 (explicitly constructed lowercase candidate plan): lowercase "encore"
        # in "The audience demanded an encore." is not auto-locked; when provided as a
        # contextual candidate reference, the planner treats it as ambiguous and escalates.
        exact_b2, plan_b2 = translator.plan_translation(
            "The audience demanded an encore.", to_chinese=True
        )
        assert exact_b2 is None and plan_b2 is not None
        assert plan_b2.locks == ()
        assert "__WUWA_TERM_" not in plan_b2.locked_text
        explicit_candidate_plan = TranslationPlan(
            source_text=plan_b2.source_text,
            prepared_text=plan_b2.prepared_text,
            locked_text=plan_b2.locked_text,
            locks=(),
            references=(
                TermReference(
                    source="encore", zh="安可", en="Encore", category="resonator"
                ),
            ),
            to_chinese=True,
            dictionary_identity=plan_b2.dictionary_identity,
        )
        meta_b2 = explicit_candidate_plan.term_planning_metadata()
        assert meta_b2.has_ambiguous_candidates is True
        assert meta_b2.guidance_mode == "contextual_candidates"
        assert meta_b2.lock_candidate_collisions == ()
        spec_b2 = translator.build_request_spec(explicit_candidate_plan)
        assert spec_b2.thinking_mode == "high"
        assert spec_b2.effective_effort == "high"
        assert "ordinary English word" in spec_b2.system_prompt
        assert "encore = 安可 (resonator)" in spec_b2.system_prompt
        assert "__WUWA_TERM_" not in spec_b2.user_prompt
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


# ---------------------------------------------------------------------------
# 4. Regression tests for review findings (budget/ceiling, cancel races,
#    strict config JSON, coordinate remapping, redaction, non-disable fallback)
# ---------------------------------------------------------------------------


def test_post_slot_budget_and_client_timeout_align_effort_and_recorded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1A: auto effort & recorded timeout must match actual post-slot budget."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")

    # Subcase A: translator configured with 18s client timeout must cap auto to low
    # and record timeout_seconds=18.0 (never 45.0).
    seen_bodies: list[dict[str, Any]] = []
    seen_read_timeouts: list[float] = []
    records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        seen_read_timeouts.append(float(request.extensions["timeout"]["read"]))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Encore used Resonance Skill."}}]}
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    short_translator = SentenceTranslator(
        db_path,
        llm_timeout_seconds=18.0,
        llm_transport=httpx.MockTransport(handler),
    )
    try:
        asyncio.run(
            short_translator.translate_async("安可使用了共鸣技能。", to_chinese=False)
        )
    finally:
        remove_obs()
        asyncio.run(short_translator.aclose())

    assert seen_bodies[0]["reasoning_effort"] == "low"
    assert seen_read_timeouts[0] == pytest.approx(18.0)
    assert records[0].requested_effort == "low"
    assert records[0].timeout_seconds == pytest.approx(18.0)

    # Subcase B: budget starts at 40s, but waiting in concurrency slot drains it to 18s;
    # post-slot resolution must pick low and send/record ~18s timeout.
    now = [100.0]
    budget = OperationExecutionBudget.for_segments(
        1, timeout_seconds=40.0, clock=lambda: now[0]
    )
    seen_bodies.clear()
    seen_read_timeouts.clear()
    records.clear()

    remove_obs = llm_transport.add_call_observer(records.append)
    queued_translator = SentenceTranslator(
        db_path,
        llm_timeout_seconds=45.0,
        llm_max_concurrency=1,
        llm_transport=httpx.MockTransport(handler),
    )

    async def run_queued() -> None:
        slots = queued_translator._current_llm_slots()
        await slots.acquire()
        task = asyncio.create_task(
            queued_translator.translate_async(
                "安可使用了共鸣技能。",
                to_chinese=False,
                execution_budget=budget,
            )
        )
        await asyncio.sleep(0)
        # Simulate 22s spent waiting in slot queue -> 18s remaining upon slot acquisition.
        now[0] = 122.0
        slots.release()
        await task

    try:
        asyncio.run(run_queued())
    finally:
        remove_obs()
        asyncio.run(queued_translator.aclose())

    assert seen_bodies[0]["reasoning_effort"] == "low"
    assert seen_read_timeouts[0] == pytest.approx(18.0)
    assert records[0].requested_effort == "low"
    assert records[0].timeout_seconds == pytest.approx(18.0)


def test_paragraph_repair_respects_auto_reasoning_ceiling() -> None:
    """Finding 1B: paragraph_repair_thinking_mode must never exceed auto_reasoning_ceiling."""
    policy_disabled_ceiling = RequestPolicy(
        configured_thinking_en_zh="auto",
        auto_reasoning_ceiling="disabled",
    )
    assert (
        policy_disabled_ceiling.paragraph_repair_thinking_mode(
            to_chinese=True,
            initial_thinking_mode="disabled",
            lock_count=1,
        )
        == "disabled"
    )
    policy_minimal_ceiling = RequestPolicy(
        configured_thinking_en_zh="auto",
        auto_reasoning_ceiling="minimal",
    )
    assert (
        policy_minimal_ceiling.paragraph_repair_thinking_mode(
            to_chinese=True,
            initial_thinking_mode="disabled",
            lock_count=1,
        )
        == "minimal"
    )


def test_cooperative_cancel_race_with_response_and_timeout_always_records_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: cooperative cancel racing with HTTP 200 or ReadTimeout never returns ok/timeout."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "disabled")

    # Race A: budget.cancel fires synchronously inside transport before returning 200 OK.
    budget_ok = OperationExecutionBudget.for_segments(1)
    records_ok: list[llm_transport.LlmCallRecord] = []

    def handler_cancel_then_ok(_request: httpx.Request) -> httpx.Response:
        budget_ok.cancel("user_cancelled")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "MUST NOT BE RETURNED"}}]}
        )

    remove_obs = llm_transport.add_call_observer(records_ok.append)
    translator_ok = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler_cancel_then_ok)
    )
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as exc_ok:
            asyncio.run(
                translator_ok.translate_async(
                    "你好世界。",
                    to_chinese=False,
                    propagate_errors=True,
                    execution_budget=budget_ok,
                )
            )
    finally:
        remove_obs()
        asyncio.run(translator_ok.aclose())

    assert exc_ok.value.reason == "cancelled"
    assert len(records_ok) == 1
    assert records_ok[0].outcome == "cancelled"
    assert records_ok[0].cancelled is True
    assert records_ok[0].cancel_reason == "user_cancelled"

    # Race B: budget.cancel fires concurrently with httpx.ReadTimeout.
    budget_to = OperationExecutionBudget.for_segments(1)
    records_to: list[llm_transport.LlmCallRecord] = []

    def handler_cancel_then_timeout(request: httpx.Request) -> httpx.Response:
        budget_to.cancel("client_disconnected")
        raise httpx.ReadTimeout("raced timeout", request=request)

    remove_obs = llm_transport.add_call_observer(records_to.append)
    translator_to = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler_cancel_then_timeout)
    )
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as exc_to:
            asyncio.run(
                translator_to.translate_async(
                    "你好世界。",
                    to_chinese=False,
                    propagate_errors=True,
                    execution_budget=budget_to,
                )
            )
    finally:
        remove_obs()
        asyncio.run(translator_to.aclose())

    assert exc_to.value.reason == "cancelled"
    assert len(records_to) == 1
    assert records_to[0].outcome == "cancelled"
    assert records_to[0].cancelled is True
    assert records_to[0].cancel_reason == "client_disconnected"


def test_provider_config_strict_json_validation_and_lossless_structure_thaw() -> None:
    """Finding 3: reject string bools / null effort values and preserve {} vs [[k, v]]."""
    with pytest.raises(ValueError, match="supports_structured_output must be a boolean"):
        provider_profile_from_config({"supports_structured_output": "false"})
    with pytest.raises(ValueError, match="supports_disable_thinking must be a boolean"):
        provider_profile_from_config({"supports_disable_thinking": "false"})
    with pytest.raises(ValueError, match="effort_mapping"):
        provider_profile_from_config({"effort_mapping": {"high": None}})
    with pytest.raises(ValueError, match="effort_mapping"):
        provider_profile_from_config({"effort_mapping": {"high": 123}})

    # Lossless JSON round-trip: nested empty dict stays {}, pair list stays list.
    profile = provider_profile_from_config(
        {
            "name": "nested_json_gateway",
            "thinking_protocol": "custom_effort_mapping",
            "enabled_extra_body": {
                "options": {},
                "pairs": [["k1", 1], ["k2", 2]],
                "empty_list": [],
            },
            "disabled_extra_body": {"options": {}},
        }
    )
    res_high = profile.resolve_effort("high")
    assert res_high.extra_body == {
        "options": {},
        "pairs": [["k1", 1], ["k2", 2]],
        "empty_list": [],
        "reasoning_effort": "high",
    }
    assert isinstance(res_high.extra_body["options"], dict)
    assert isinstance(res_high.extra_body["pairs"], list)
    assert isinstance(res_high.extra_body["empty_list"], list)
    assert profile.resolve_effort("disabled").extra_body == {"options": {}}


def test_without_ambiguous_locks_remaps_scoped_reference_and_ambiguous_mention_offsets() -> None:
    """Finding 4: demoting placeholders remaps downstream reference and mention coordinates."""
    placeholder = "__WUWA_TERM_1111111111111111_0000__"
    locked_text = f"{placeholder}和椿以及秧在战斗。"
    camellya_start = locked_text.index("椿")
    yang_start = locked_text.index("秧")

    plan = TranslationPlan(
        source_text="安可和椿以及秧在战斗。",
        prepared_text="安可和椿以及秧在战斗。",
        locked_text=locked_text,
        locks=((placeholder, "安可", "Encore"),),
        references=(
            TermReference(
                source="椿",
                zh="椿",
                en="Camellya",
                category="resonator",
                occurrences=(
                    TermOccurrence(
                        start=3,
                        end=4,
                        matched_text="椿",
                        segment_index=0,
                        model_start=camellya_start,
                        model_end=camellya_start + 1,
                    ),
                ),
                reason="single_char_name_mention",
            ),
        ),
        ambiguous=(
            AmbiguousMention(
                source="秧",
                zh="秧",
                en="Yangyang",
                category="resonator",
                occurrence=TermOccurrence(
                    start=6,
                    end=7,
                    matched_text="秧",
                    segment_index=0,
                    model_start=yang_start,
                    model_end=yang_start + 1,
                ),
                evaluation=ContextEvaluation(
                    hits=(
                        EvidenceHit("same_paragraph_entity", ((3, 4),)),
                        EvidenceHit("position_context", ((0, 2),)),
                    )
                ),
            ),
        ),
        to_chinese=False,
    )

    safe_plan = plan.without_ambiguous_locks()
    assert safe_plan.locked_text == "安可和椿以及秧在战斗。"
    camellya_ref = next(ref for ref in safe_plan.references if ref.source == "椿")
    occ = camellya_ref.occurrences[0]
    assert occ.model_start == 3 and occ.model_end == 4
    assert safe_plan.locked_text[occ.model_start : occ.model_end] == "椿"
    amb_occ = safe_plan.ambiguous[0].occurrence
    assert amb_occ.model_start == 6 and amb_occ.model_end == 7
    assert safe_plan.locked_text[amb_occ.model_start : amb_occ.model_end] == "秧"

    # Building request spec with scoped_alternative exposure validates ranges cleanly.
    spec = build_translation_request_spec(
        safe_plan,
        policy=RequestPolicy(ambiguous_mention_exposure="scoped_alternative"),
        model=TEST_MODEL,
    )
    assert "Scoped term references:" in spec.system_prompt
    assert "Low-confidence name candidates:" in spec.system_prompt


def test_custom_profile_name_and_free_text_cancel_reason_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 5: custom profile names and free-text cancel reasons never leak to logs/observers."""
    db_path = _make_encore_db(tmp_path)
    secret_profile_name = "sk-secret-gateway-name-777"
    secret_cancel_reason = "abort-token-secret-888"
    secret_effort_wire = "sk-secret-wire-effort-999"

    custom_profile = build_custom_provider_profile(
        name=secret_profile_name,
        effort_mapping={"high": secret_effort_wire},
    )
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "high")

    budget = OperationExecutionBudget.for_segments(1)
    records: list[llm_transport.LlmCallRecord] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        budget.cancel(secret_cancel_reason)
        return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_transport=httpx.MockTransport(handler),
        provider_profile=custom_profile,
    )
    trace = request_trace.start()
    try:
        with caplog.at_level(logging.INFO, logger="wuwaterm.llm_transport"):
            with pytest.raises(llm_transport.LLMTranslationError) as caught:
                asyncio.run(
                    translator.translate_async(
                        "你好世界。",
                        to_chinese=False,
                        propagate_errors=True,
                        execution_budget=budget,
                    )
                )
    finally:
        request_trace.reset()
        remove_obs()
        asyncio.run(translator.aclose())

    assert caught.value.reason == "cancelled"
    assert len(records) == 1
    assert records[0].provider_profile is not None
    assert records[0].provider_profile.startswith("custom:")
    assert records[0].effective_effort is not None
    assert records[0].effective_effort.startswith("custom:")
    assert records[0].cancel_reason == "custom"

    plan = TranslationPlan(
        source_text="你好世界。",
        prepared_text="你好世界。",
        locked_text="你好世界。",
        locks=(),
        references=(),
        to_chinese=False,
    )
    spec = build_translation_request_spec(
        plan,
        policy=RequestPolicy(configured_thinking_zh_en="high"),
        provider_profile=custom_profile,
        model=TEST_MODEL,
    )
    exec_meta = spec.execution_metadata_dict(
        cancelled=True, cancel_reason=secret_cancel_reason
    )
    assert exec_meta["provider_profile"].startswith("custom:")
    assert exec_meta["cancel_reason"] == "custom"

    combined = (
        caplog.text
        + json.dumps(records[0].to_metadata_dict())
        + json.dumps(exec_meta)
        + trace.provider_profile
        + trace.effective_effort
    )
    for forbidden in (secret_profile_name, secret_cancel_reason, secret_effort_wire):
        assert forbidden not in combined


def test_profile_without_disable_thinking_skips_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 6: profiles with supports_disable_thinking=False must not trigger disabled retry."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_EN_ZH_ENV, "auto")

    policy = RequestPolicy(configured_thinking_en_zh="auto", timeout_seconds=45.0)
    assert (
        policy.allows_timeout_fallback(
            to_chinese=True,
            initial_thinking_mode="low",
            provider_profile=OPENAI_REASONING_PROVIDER_PROFILE,
        )
        is False
    )

    calls = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    translator = SentenceTranslator(
        db_path,
        llm_transport=httpx.MockTransport(timeout_handler),
        provider_profile=OPENAI_REASONING_PROVIDER_PROFILE,
    )
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as caught:
            asyncio.run(
                translator.translate_async(
                    "Short line that times out.",
                    to_chinese=True,
                    propagate_errors=True,
                )
            )
    finally:
        asyncio.run(translator.aclose())

    assert caught.value.reason == "timeout"
    assert calls == 1


def test_paragraph_repair_downgrades_high_to_low_when_remaining_window_is_tight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up 1: paragraph repair applies its own post-slot remaining timeout window."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")

    now = [100.0]
    budget = OperationExecutionBudget.for_segments(
        1, timeout_seconds=45.0, clock=lambda: now[0]
    )
    seen_bodies: list[dict[str, Any]] = []
    seen_timeouts: list[float] = []
    records: list[llm_transport.LlmCallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        seen_timeouts.append(float(request.extensions["timeout"]["read"]))
        if len(seen_bodies) == 1:
            # First call took 25s -> 20s remaining (repair timeout becomes 18s < 30s).
            now[0] = 125.0
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Encore used Resonance Skill in one merged paragraph."}}]},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Encore arrived.\n\nShe used Resonance Skill."}}]},
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_timeout_seconds=45.0,
        llm_transport=httpx.MockTransport(handler),
    )
    try:
        out = asyncio.run(
            translator.translate_async(
                "安可来了。\n\n她使用了共鸣技能。",
                to_chinese=False,
                context=TranslationContext(),
                execution_budget=budget,
            )
        )
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert out == "Encore arrived.\n\nShe used Resonance Skill."
    assert len(seen_bodies) == 2
    assert seen_bodies[0]["reasoning_effort"] == "high"
    assert seen_bodies[1]["reasoning_effort"] == "low"
    assert seen_timeouts[1] == pytest.approx(18.0)
    assert records[0].attempt_kind == "initial"
    assert records[0].requested_effort == "high"
    assert records[1].attempt_kind == "paragraph_repair"
    assert records[1].requested_effort == "low"
    assert records[1].timeout_seconds == pytest.approx(18.0)


def test_cooperative_cancel_during_truncated_response_records_cancelled_in_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up 2: finish_reason=length racing with cancel records cancelled everywhere."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "disabled")

    budget = OperationExecutionBudget.for_segments(1)
    records: list[llm_transport.LlmCallRecord] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        budget.cancel("user_cancelled")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "partial"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path, llm_transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as caught:
            asyncio.run(
                translator.translate_async(
                    "你好世界。",
                    to_chinese=False,
                    propagate_errors=True,
                    execution_budget=budget,
                )
            )
    finally:
        remove_obs()
        asyncio.run(translator.aclose())

    assert caught.value.reason == "cancelled"
    assert len(records) == 1
    assert records[0].outcome == "cancelled"
    assert records[0].cancelled is True
    assert records[0].cancel_reason == "user_cancelled"


def test_provider_config_rejects_non_finite_floats_and_lone_surrogates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow-up 3: reject NaN/Infinity and lone Unicode surrogates in config and env."""
    for bad_float in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            provider_profile_from_config(
                {
                    "name": "bad_float_gw",
                    "enabled_extra_body": {"scale": bad_float},
                }
            )

    monkeypatch.setenv(
        PROVIDER_CAPABILITIES_JSON_ENV,
        '{"name": "nan_gw", "enabled_extra_body": {"scale": NaN}}',
    )
    assert resolve_provider_profile_from_env() == DEFAULT_PROVIDER_PROFILE

    monkeypatch.setenv(
        PROVIDER_CAPABILITIES_JSON_ENV,
        '{"name": "inf_gw", "enabled_extra_body": {"scale": Infinity}}',
    )
    assert resolve_provider_profile_from_env() == DEFAULT_PROVIDER_PROFILE

    with pytest.raises(ValueError, match="UTF-8"):
        provider_profile_from_config({"name": "bad-\ud800-name"})
    with pytest.raises(ValueError, match="UTF-8"):
        provider_profile_from_config(
            {"name": "bad_body", "enabled_extra_body": {"tag": "bad-\udfff"}}
        )
    monkeypatch.setenv(
        PROVIDER_CAPABILITIES_JSON_ENV,
        '{"name": "surrogate_\\ud800_gw"}',
    )
    assert resolve_provider_profile_from_env() == DEFAULT_PROVIDER_PROFILE


def test_custom_prefix_profile_name_cannot_bypass_hashing_and_matches_across_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Follow-up 4: custom:<8-hex>-shaped raw names still hash once identically everywhere."""
    db_path = _make_encore_db(tmp_path)
    spoofed_name = "custom:deadbeef"
    spoofed_effort = "custom:01234567"
    profile = build_custom_provider_profile(
        name=spoofed_name,
        effort_mapping={"high": spoofed_effort},
    )
    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "high")

    records: list[llm_transport.LlmCallRecord] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hi."}}]})

    remove_obs = llm_transport.add_call_observer(records.append)
    translator = SentenceTranslator(
        db_path,
        llm_transport=httpx.MockTransport(handler),
        provider_profile=profile,
    )
    trace = request_trace.start()
    try:
        with caplog.at_level(logging.INFO, logger="wuwaterm.llm_transport"):
            asyncio.run(translator.translate_async("你好世界。", to_chinese=False))
    finally:
        request_trace.reset()
        remove_obs()
        asyncio.run(translator.aclose())

    plan = TranslationPlan(
        source_text="你好世界。",
        prepared_text="你好世界。",
        locked_text="你好世界。",
        locks=(),
        references=(),
        to_chinese=False,
    )
    spec = build_translation_request_spec(
        plan,
        policy=RequestPolicy(configured_thinking_zh_en="high"),
        provider_profile=profile,
        model=TEST_MODEL,
    )
    exec_meta = spec.execution_metadata_dict()
    effort_meta = profile.resolve_effort("high").to_metadata_dict()

    # Must NOT pass through the raw spoofed strings verbatim.
    assert records[0].provider_profile != spoofed_name
    assert records[0].effective_effort != spoofed_effort
    assert spoofed_name not in caplog.text
    assert spoofed_effort not in caplog.text

    # Single shared fingerprint across effort_meta, exec_meta, LlmCallRecord, and RequestTrace (no double hashing).
    assert (
        effort_meta["provider_profile"]
        == exec_meta["provider_profile"]
        == records[0].provider_profile
        == trace.provider_profile
    )
    assert (
        effort_meta["effective_effort"]
        == exec_meta["effective_effort"]
        == records[0].effective_effort
        == trace.effective_effort
    )


def test_custom_profile_without_explicit_disable_body_skips_timeout_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up 5: custom profile with only name + effort_mapping skips disabled timeout retry."""
    db_path = _make_encore_db(tmp_path)
    monkeypatch.setenv(TRANSLATION_THINKING_EN_ZH_ENV, "auto")

    custom_no_disable = provider_profile_from_config(
        {
            "name": "effort_only_proxy",
            "effort_mapping": {"low": "low", "high": "high"},
        }
    )
    assert custom_no_disable.resolve_effort("disabled").omitted is True
    assert custom_no_disable.has_explicit_disable_thinking() is False

    policy = RequestPolicy(configured_thinking_en_zh="auto", timeout_seconds=45.0)
    assert (
        policy.allows_timeout_fallback(
            to_chinese=True,
            initial_thinking_mode="low",
            provider_profile=custom_no_disable,
        )
        is False
    )

    calls = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    translator = SentenceTranslator(
        db_path,
        llm_transport=httpx.MockTransport(timeout_handler),
        provider_profile=custom_no_disable,
    )
    try:
        with pytest.raises(llm_transport.LLMTranslationError) as caught:
            asyncio.run(
                translator.translate_async(
                    "Short line that times out.",
                    to_chinese=True,
                    propagate_errors=True,
                )
            )
    finally:
        asyncio.run(translator.aclose())

    assert caught.value.reason == "timeout"
    assert calls == 1
