"""Tests for 1-hop EntityGraph disambiguation and TranslationPlan/SentenceTranslator KB wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import tracemalloc
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.test_api_semantic_review import configure_llm
from wuwaterm.application import (
    ContextBefore,
    DocumentAnchor,
    TranslationContext,
    build_translator,
)
from wuwaterm.data_source import SourceProvenance
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.domain_knowledge import (
    DEFAULT_MAX_CANDIDATES,
    DomainKnowledgeCandidate,
    prepare_prompt_domain_knowledge_candidates,
)
from wuwaterm.entity_graph import EntityGraph, EntityNode
from wuwaterm.knowledge_builder import (
    AliasSpec,
    EntitySpec,
    RelationSpec,
    create_knowledge_database,
)
from wuwaterm.knowledge_db import KnowledgeBaseReader, compute_terms_db_revision
from wuwaterm.models import TermRecord
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.term_preparation import TermReference
from wuwaterm.translation_plan import (
    DEFAULT_PROVIDER_PROFILE,
    DOMAIN_KNOWLEDGE_HEADER,
    DOMAIN_KNOWLEDGE_INSTRUCTION,
    RequestPolicy,
    TranslationPlan,
    build_translation_messages,
    build_translation_request_spec,
    render_context_instruction,
)


def _provenance() -> SourceProvenance:
    return SourceProvenance(
        profile="arikatsu",
        repo_url="https://example.invalid/wutheringdata.git",
        commit="deadbeef1234567890",
        game_version="3.7.0",
        resource_version="3.7.8",
        changelist="8975829",
    )


def _make_terms_and_kb(
    tmp_path: Path,
    *,
    mismatch_revision: bool = False,
) -> tuple[Path, Path, str]:
    terms_path = tmp_path / "terms.db"
    with closing(connect(terms_path)) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "2"), ("source_commit", "test-commit")),
        )
        insert_records(
            conn,
            [
                TermRecord("resonator", "fixture", "1", "1", "今汐", "Jinhsi"),
                TermRecord("location", "fixture", "2", "2", "黑海岸", "Black Shores"),
            ],
        )
        conn.commit()

    rev = compute_terms_db_revision(terms_path)
    kb_rev = ("0" * 64) if mismatch_revision else rev
    kb_path = tmp_path / "knowledge.db"
    create_knowledge_database(
        kb_path,
        entities=[
            EntitySpec(
                "resonator:1603",
                "resonator",
                "BinData/role/roleinfo.json",
                "1603",
                "椿",
                "Camellya",
                context_gloss="黑海岸执花",
            ),
            EntitySpec(
                "influence:10",
                "influence",
                "BinData/influence/influence.json",
                "10",
                "黑海岸",
                "Black Shores",
            ),
            EntitySpec(
                "skill:160301",
                "skill",
                "BinData/skill/skill.json",
                "160301",
                "执花",
                "Bloom Bearer",
            ),
            EntitySpec(
                "element:1",
                "element",
                "BinData/element_info/elementinfo.json",
                "1",
                "冷凝",
                "Glacio",
                context_gloss="冷凝属性",
            ),
            EntitySpec(
                "term:850086",
                "term",
                "BinData/Term/termconfig.json",
                "850086",
                "夜归",
                "Midnight Rangers",
                context_gloss="守卫今州边疆的部队",
            ),
            EntitySpec(
                "resonator:1",
                "resonator",
                "BinData/role/roleinfo.json",
                "1",
                "今汐",
                "Jinhsi",
            ),
        ],
        aliases=[
            AliasSpec("term:850086", "夜归军团", "zh", "te_anchor", "official_derived"),
        ],
        contexts=[],
        relations=[
            RelationSpec(
                "resonator:1603",
                "belongs_to_influence",
                "influence:10",
                "BinData/role/roleinfo.json",
                "PartyId:10",
            ),
            RelationSpec(
                "resonator:1603",
                "has_skill",
                "skill:160301",
                "BinData/skill/skill.json",
                "SkillId:160301",
            ),
        ],
        terms_db_revision=kb_rev,
        source_provenance=_provenance(),
    )
    return terms_path, kb_path, rev


def test_entity_graph_one_hop_boosts_anchored_candidate_and_ignores_everyday_words(
    tmp_path: Path,
):
    _terms_path, kb_path, rev = _make_terms_and_kb(tmp_path)

    with KnowledgeBaseReader(kb_path, expected_terms_db_revision=rev) as reader:
        graph = reader.entity_graph
        assert graph.node_count == 3
        assert graph.edge_count == 2
        assert {
            neighbor.entity_id for neighbor, _edge in graph.one_hop_neighbors("resonator:1603")
        } == {"influence:10", "skill:160301"}

        # Positive 1-hop disambiguation: 黑海岸 + 执花 anchor boosts 椿 (Camellya)
        sentence = "黑海岸的椿施放了执花。"
        no_graph, no_graph_out = reader.query_with_diagnostics(
            sentence,
            locked_surfaces=("黑海岸",),
            use_graph=False,
        )
        with_graph, with_graph_out = reader.query_with_diagnostics(
            sentence,
            locked_surfaces=("黑海岸",),
            use_graph=True,
        )
        assert no_graph_out.triggered_count == 0
        assert with_graph_out.triggered_count >= 1
        assert "resonator:1603" in with_graph_out.boosted_entity_ids

        no_graph_camellya = next(c for c in no_graph if c.entity_id == "resonator:1603")
        with_graph_camellya = next(
            c for c in with_graph if c.entity_id == "resonator:1603"
        )
        assert with_graph_camellya.score > no_graph_camellya.score
        assert with_graph_camellya.lock_eligible is False
        assert "[1-hop:" in with_graph_camellya.context_gloss

        # Negative everyday sentence: 香椿炒蛋 has 0 graph triggers and empty candidates
        everyday_cands, everyday_out = reader.query_with_diagnostics(
            "今天晚饭吃香椿炒蛋。",
            use_graph=True,
        )
        assert everyday_out.triggered_count == 0
        assert everyday_cands == ()

        # Even if an unrelated anchor surface is passed externally, ordinary-word
        # single-char CJK ("香椿炒蛋") is NEVER boosted and NEVER returned.
        anchored_everyday_cands, anchored_everyday_out = reader.query_with_diagnostics(
            "今天晚饭吃香椿炒蛋。",
            anchor_surfaces=("黑海岸",),
            use_graph=True,
        )
        assert anchored_everyday_out.triggered_count == 0
        assert anchored_everyday_cands == ()


def test_entity_graph_resident_memory_stays_under_3_mib():
    """Synthetic 5,000-edge graph must stay well below the 3 MiB memory ceiling."""
    nodes = [
        EntityNode(
            entity_id=f"entity:{idx}",
            entity_type="resonator" if idx % 2 == 0 else "skill",
            zh=f"实体{idx}",
            en=f"Entity {idx}",
            trust_tier="official_authoritative",
        )
        for idx in range(3000)
    ]
    edges = [
        (f"entity:{idx % 1500}", "has_skill", f"entity:{1500 + (idx % 1500)}")
        for idx in range(5000)
    ]

    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        graph = EntityGraph.from_edges(nodes=nodes, edges=edges)
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    assert graph.node_count == 3000
    assert graph.edge_count > 1000
    alloc_bytes = sum(max(0, s.size_diff) for s in after.compare_to(before, "lineno"))
    alloc_mib = alloc_bytes / (1024.0 * 1024.0)
    assert alloc_mib < 3.0, f"EntityGraph allocated {alloc_mib:.3f} MiB (>= 3.0 MiB)"


def test_default_empty_domain_knowledge_keeps_prompt_and_operation_identity_byte_identical():
    policy = RequestPolicy()
    base_plan = TranslationPlan(
        source_text="今汐使用冷凝攻击。",
        prepared_text="今汐使用冷凝攻击。",
        locked_text="今汐使用冷凝攻击。",
        locks=(),
        references=(TermReference(source="今汐", zh="今汐", en="Jinhsi", category="resonator"),),
        to_chinese=False,
        context=TranslationContext(
            before=ContextBefore(source="前文", target="Previous"),
        ),
        dictionary_identity="dict-rev-1",
    )
    explicit_empty_plan = TranslationPlan(
        source_text="今汐使用冷凝攻击。",
        prepared_text="今汐使用冷凝攻击。",
        locked_text="今汐使用冷凝攻击。",
        locks=(),
        references=(TermReference(source="今汐", zh="今汐", en="Jinhsi", category="resonator"),),
        to_chinese=False,
        context=TranslationContext(
            before=ContextBefore(source="前文", target="Previous"),
            domain_knowledge_candidates=(),
        ),
        dictionary_identity="dict-rev-1",
        domain_knowledge_candidates=(),
    )

    spec_base = build_translation_request_spec(base_plan, policy=policy, model="m")
    spec_empty = build_translation_request_spec(
        explicit_empty_plan, policy=policy, model="m"
    )
    op_base = spec_base.operation_identity(policy, DEFAULT_PROVIDER_PROFILE)
    op_empty = spec_empty.operation_identity(policy, DEFAULT_PROVIDER_PROFILE)

    assert spec_base.messages == spec_empty.messages
    assert spec_base.wire_payload() == spec_empty.wire_payload()
    assert op_base.request.request_sha256 == op_empty.request.request_sha256
    assert op_base.operation_sha256 == op_empty.operation_sha256
    assert DOMAIN_KNOWLEDGE_HEADER not in spec_base.system_prompt
    assert DOMAIN_KNOWLEDGE_INSTRUCTION not in spec_base.system_prompt


def test_domain_knowledge_prompt_injection_enforces_secondary_gate_and_lock_isolation():
    glacio = DomainKnowledgeCandidate(
        entity_id="element:1",
        source_surface="冷凝",
        zh="冷凝",
        en="Glacio",
        entity_type="element",
        trust_tier="official_authoritative",
        retrieval_channel="short_ngram",
        score=0.05,
        context_gloss="冷凝属性",
        lock_eligible=False,
    )
    locked_collision = DomainKnowledgeCandidate(
        entity_id="resonator:1",
        source_surface="今汐",
        zh="今汐",
        en="Jinhsi",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="lexical_alias",
        score=0.09,
        lock_eligible=False,
    )
    ordinary_chun = DomainKnowledgeCandidate(
        entity_id="resonator:1603",
        source_surface="椿",
        zh="椿",
        en="Camellya",
        entity_type="resonator",
        trust_tier="official_authoritative",
        retrieval_channel="short_ngram",
        score=0.08,
        lock_eligible=False,
    )
    spoofed = SimpleNamespace(
        entity_id="fake:1",
        source_surface="冷凝",
        zh="冷凝",
        en="Fake",
        entity_type="element",
        trust_tier="official_authoritative",
        retrieval_channel="short_ngram",
        score=9.9,
        context_gloss="",
        lock_eligible=True,
    )

    plan = TranslationPlan(
        source_text="今汐吃香椿并释放冷凝攻击。",
        prepared_text="今汐吃香椿并释放冷凝攻击。",
        locked_text="__WUWA_TERM_1111111111111111_0000__吃香椿并释放冷凝攻击。",
        locks=(("__WUWA_TERM_1111111111111111_0000__", "今汐", "Jinhsi"),),
        references=(),
        to_chinese=False,
        context=TranslationContext(
            domain_knowledge_candidates=(
                glacio,
                locked_collision,
                ordinary_chun,
                spoofed,  # type: ignore[arg-type]
            ),
        ),
    )

    messages = build_translation_messages(plan)
    sys_prompt = messages[0]["content"]
    user_prompt = messages[1]["content"]

    # Locks stay untouched; no candidate ever becomes a placeholder lock.
    assert len(plan.locks) == 1
    assert user_prompt == "__WUWA_TERM_1111111111111111_0000__吃香椿并释放冷凝攻击。"
    assert DOMAIN_KNOWLEDGE_INSTRUCTION in sys_prompt
    assert DOMAIN_KNOWLEDGE_HEADER in sys_prompt
    assert '"Glacio"' in sys_prompt
    # Locked collision, ordinary-word 椿 in 香椿, and spoofed lock_eligible=True are all dropped.
    kb_section = sys_prompt.split(DOMAIN_KNOWLEDGE_HEADER, 1)[1]
    assert '"Jinhsi"' not in kb_section
    assert '"Camellya"' not in kb_section
    assert '"Fake"' not in kb_section

    # Direct render_context_instruction without source_text still caps and drops spoofed entries.
    many = tuple(
        DomainKnowledgeCandidate(
            entity_id=f"term:{i}",
            source_surface=f"词{i}",
            zh=f"词{i}",
            en=f"T{i}",
            entity_type="term",
            trust_tier="official_authoritative",
            retrieval_channel="lexical_alias",
            score=1.0 - i * 0.01,
            lock_eligible=False,
        )
        for i in range(15)
    )
    rendered = render_context_instruction(
        TranslationContext(domain_knowledge_candidates=(*many, spoofed)),  # type: ignore[arg-type]
        to_chinese=False,
    )
    assert rendered.count('"entity_type"') == DEFAULT_MAX_CANDIDATES
    assert '"Fake"' not in rendered

    # prepare_prompt_domain_knowledge_candidates rejects foreign objects cleanly.
    assert prepare_prompt_domain_knowledge_candidates("冷凝", [spoofed]) == ()


def test_sentence_translator_optional_kb_mount_and_safe_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    terms_path, kb_path, _rev = _make_terms_and_kb(tmp_path)
    configure_llm(monkeypatch, semantic_enabled=False)
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "kb-wiring-test-model")
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_ZH_EN", "disabled")

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Translated output."}}]}
        )

    # 1. Default translator (knowledge_db_path=None): zero KB injection.
    default_tr = SentenceTranslator(
        terms_path, llm_transport=httpx.MockTransport(handler)
    )
    # 2. Opt-in translator with matching knowledge.db mounted.
    kb_tr = build_translator(terms_path, knowledge_db_path=kb_path)
    kb_tr._llm_transport = httpx.MockTransport(handler)
    try:
        _e0, default_plan = default_tr.plan_translation(
            "夜归释放了冷凝技能。", to_chinese=False
        )
        _e1, kb_plan = kb_tr.plan_translation(
            "夜归释放了冷凝技能。", to_chinese=False
        )
        assert default_plan is not None and kb_plan is not None
        assert default_plan.domain_knowledge_candidates == ()
        assert default_plan.locks == kb_plan.locks
        assert len(kb_plan.domain_knowledge_candidates) >= 2
        assert all(
            c.lock_eligible is False for c in kb_plan.domain_knowledge_candidates
        )
        kb_spec = kb_tr.build_request_spec(kb_plan)
        assert DOMAIN_KNOWLEDGE_HEADER in kb_spec.system_prompt
        assert "Glacio" in kb_spec.system_prompt
        assert "Midnight Rangers" in kb_spec.system_prompt

        # End-to-end async wire call sends soft KB context without turning it into locks.
        out = asyncio.run(
            kb_tr.translate_async(
                "黑海岸的椿施放了执花。",
                to_chinese=False,
                context=TranslationContext(
                    document_anchor=DocumentAnchor(
                        topic="黑海岸", active_entities=("黑海岸",)
                    )
                ),
            )
        )
        assert out == "Translated output."
        assert len(captured) == 1
        wire_sys = captured[0]["messages"][0]["content"]
        assert DOMAIN_KNOWLEDGE_HEADER in wire_sys
        assert "Camellya" in wire_sys
        assert "[1-hop:" in wire_sys
    finally:
        asyncio.run(default_tr.aclose())
        asyncio.run(kb_tr.aclose())

    # 3. Missing knowledge.db degrades safely without blocking translation or leaking path.
    missing_kb = tmp_path / "does_not_exist_secret_dir" / "knowledge.db"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        missing_tr = SentenceTranslator(
            terms_path,
            llm_transport=httpx.MockTransport(handler),
            knowledge_db_path=missing_kb,
        )
        try:
            _ex, plan_missing = missing_tr.plan_translation(
                "夜归释放了冷凝技能。", to_chinese=False
            )
            assert plan_missing is not None
            assert plan_missing.domain_knowledge_candidates == ()
            out_missing = asyncio.run(
                missing_tr.translate_async("夜归释放了冷凝技能。", to_chinese=False)
            )
            assert out_missing == "Translated output."
        finally:
            asyncio.run(missing_tr.aclose())
    assert any("missing" in rec.message for rec in caplog.records)
    assert all("does_not_exist_secret_dir" not in rec.message for rec in caplog.records)

    # 4. Revision-mismatched knowledge.db degrades safely with sanitized warning.
    bad_dir = tmp_path / "mismatch_case"
    bad_dir.mkdir()
    bad_terms, bad_kb, _ = _make_terms_and_kb(bad_dir, mismatch_revision=True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wuwaterm.sentence"):
        mismatch_tr = SentenceTranslator(
            bad_terms,
            llm_transport=httpx.MockTransport(handler),
            knowledge_db_path=bad_kb,
        )
        try:
            _ex, plan_bad = mismatch_tr.plan_translation(
                "夜归释放了冷凝技能。", to_chinese=False
            )
            assert plan_bad is not None
            assert plan_bad.domain_knowledge_candidates == ()
        finally:
            asyncio.run(mismatch_tr.aclose())
    assert any("terms_db_revision mismatch" in rec.message for rec in caplog.records)
    assert all(str(bad_kb) not in rec.message for rec in caplog.records)
