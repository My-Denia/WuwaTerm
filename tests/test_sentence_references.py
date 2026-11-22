"""Reference admission and prompt scoping on TermReference/TermOccurrence (MY-18 / MY-380).

The dictionary can offer several official pairs for one surface, a soft casing
of a derived name, and occurrence ranges for an admitted one-character name.
All three are opt-in per request: a sentence that needs none of them must reach
the model carrying the exact prompt bytes it carried ahead of these mechanisms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import pytest

from wuwaterm.application import (
    KIND_EXACT,
    TranslationJob,
    lookup_exact_terms,
    translate_request,
    translate_request_async,
)
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.models import TermRecord
from wuwaterm.sentence import (
    SentenceTranslator,
    TRANSLATION_UNAVAILABLE_NOTICE,
    LLMTranslationError,
    _CONTEXTUAL_REFERENCE_INSTRUCTION,
    _SCOPED_REFERENCE_HEADER,
    _SCOPED_REFERENCE_INSTRUCTION,
    _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION,
    _UNTRUSTED_SOURCE_INSTRUCTION,
    _call_llm_async_with_client,
)
from wuwaterm.term_preparation import (
    TermOccurrence,
    format_scoped_reference_lines,
    references_need_scope_instruction,
    validated_reference_ranges,
)


def _service(tmp_path, records, metadata=(), transport=None):
    path = tmp_path / "sentence-refs.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture"), *metadata),
        )
        insert_records(conn, records)
        conn.commit()
    return SentenceTranslator(path, llm_transport=transport)


def _record(zh: str, en: str, category: str = "resonator", table: str = "a") -> TermRecord:
    return TermRecord(
        category=category,
        source_file=f"{table}.json",
        source_id=f"{table}:{zh}:{en}",
        text_key=f"{table}:{zh}:{en}",
        zh=zh,
        en=en,
    )


def _lock(translator, text: str, *, to_chinese: bool):
    return translator._lock_terms_with_references(
        translator.prepare_text(text),
        to_chinese=to_chinese,
        allow_plural_inflection=to_chinese,
        allow_nfkc_spelling=True,
    )


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    # The transport takes its endpoint from the environment. No request here
    # leaves the process: a mock transport replies to each one.
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")


def _system_prompt(**kwargs) -> str:
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": " ok "}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await _call_llm_async_with_client(client, **kwargs)
        return seen["body"]["messages"][0]["content"]

    return asyncio.run(go())


# The pre-admission prompt, byte for byte: the four blocks every model request
# already carried, with no scope explanation and no occurrence ranges.
_PLAIN_PROMPT_ZH_EN = (
    "Translate Chinese Wuthering Waves text into English. "
    + _UNTRUSTED_SOURCE_INSTRUCTION
    + "Keep all placeholders exactly unchanged. "
    "Do not paraphrase locked official terms. Return English only.\n"
    "Locked terms:\n\n"
    + _CONTEXTUAL_REFERENCE_INSTRUCTION
    + "Dictionary term references:\n今汐 = Jinhsi (resonator)"
)


def test_plain_references_keep_the_prompt_bytes_unchanged(tmp_path):
    # One surface, one meaning, two tables: the reference list is one line and
    # the prompt gains neither the scope explanation nor occurrence ranges.
    translator = _service(
        tmp_path,
        (
            _record("今汐", "Jinhsi", table="a"),
            _record("今汐", "Jinhsi", table="b"),
            _record("今汐", "Jinhsi", category="speaker", table="c"),
        ),
    )

    locked = _lock(translator, "今汐登场。", to_chinese=False)

    assert tuple(ref.as_tuple() for ref in locked.references) == (
        ("今汐", "今汐", "Jinhsi", "resonator"),
    )
    assert references_need_scope_instruction(locked.references) is False
    assert (
        validated_reference_ranges(
            locked.locked_text, locked.references, to_chinese=False
        )
        == []
    )
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert prompt == _PLAIN_PROMPT_ZH_EN


def test_one_character_name_scopes_its_occurrences(tmp_path):
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "欢迎椿来到。", to_chinese=False)

    assert tuple(ref.as_tuple() for ref in locked.references) == (
        ("椿", "椿", "Camellya", "resonator"),
    )
    ref = locked.references[0]
    assert ref.origin == "dictionary"
    assert ref.reason == "single_char_name_mention"
    assert ref.occurrences == (
        TermOccurrence(
            start=2, end=3, matched_text="椿", segment_index=0, model_start=2, model_end=3
        ),
    )
    assert references_need_scope_instruction(locked.references) is True
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert _SCOPED_REFERENCE_INSTRUCTION in prompt
    assert _SCOPED_REFERENCE_HEADER in prompt
    assert "Dictionary term references:\n" not in prompt
    assert "椿 = Camellya (resonator) — ONLY at: 欢迎「椿」来到。" in prompt
    assert "Reference occurrence ranges" not in prompt
    assert locked.locked_text == "欢迎椿来到。"


def test_mixed_valid_mention_and_embedded_compound_selectively_locks_only_valid_mention(
    tmp_path,
):
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "椿的红椿蕊很强。", to_chinese=False)

    # Admitted 椿 is selectively locked in place; unadmitted 红椿蕊 stays plain
    # text and 椿 = Camellya is completely absent from the system prompt.
    assert locked.references == ()
    assert [(zh, en) for _, zh, en in locked.locks] == [("椿", "Camellya")]
    assert locked.locked_text.endswith("的红椿蕊很强。")
    assert locked.restore(locked.locked_text, to_en=True) == "Camellya的红椿蕊很强。"
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert "椿" not in prompt
    assert f"{locked.locks[0][0]} = Camellya" in prompt
    assert "Dictionary term references:\n" not in prompt
    assert "Scoped term references:\n" not in prompt


def test_single_char_heart_mixed_with_ordinary_paragraphs_locks_only_admitted_mention(
    tmp_path,
):
    translator = _service(tmp_path, (_record("心", "Hsin"),))
    source = "心很重要。\n\n她的心很痛。\n\n心加入队伍。"
    locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(source),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    assert locked.references == ()
    assert [(zh, en) for _, zh, en in locked.locks] == [("心", "Hsin")]
    assert locked.locked_text.startswith("心很重要。\n\n她的心很痛。\n\n__WUWA_TERM_")
    assert locked.restore(locked.locked_text, to_en=True) == (
        "心很重要。\n\n她的心很痛。\n\nHsin加入队伍。"
    )
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert "心" not in prompt
    assert f"{locked.locks[0][0]} = Hsin" in prompt
    assert "Dictionary term references:\n" not in prompt
    assert "Scoped term references:\n" not in prompt


def test_mixed_single_char_locking_realigns_remaining_scoped_skill_coordinates(tmp_path):
    # D-003 shape: mixed 心 (admitted + unadmitted) coexists with a scoped skill
    # stem 变奏. Locking 心 must shift 变奏's model_start/model_end coordinates so
    # validated_reference_ranges and Scoped term references stay exact.
    translator = _service(
        tmp_path,
        (
            _record("心", "Hsin", category="resonator", table="res"),
            TermRecord(
                category="core_term",
                source_file="BinData/skill/skilltype.json",
                source_id="5",
                text_key="SkillType_5_TypeName",
                zh="变奏技能",
                en="Intro Skill",
            ),
        ),
    )
    source = "心很重要。\n\n心加入队伍，切换角色触发变奏入场。舞曲的变奏。"
    locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(source),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    assert [(zh, en) for _, zh, en in locked.locks] == [("心", "Hsin")]
    assert [ref.source for ref in locked.references] == ["变奏"]
    skill_ref = locked.references[0]
    occ = skill_ref.occurrences[0]
    assert occ.model_start is not None and occ.model_end is not None
    assert locked.locked_text[occ.model_start : occ.model_end] == "变奏"
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert "心 = Hsin" not in prompt
    assert f"{locked.locks[0][0]} = Hsin" in prompt
    assert "Scoped term references:\n变奏 = Intro Skill (core_term) — ONLY at:" in prompt
    assert "| NOT at:" in prompt


def test_many_admitted_occurrences_compress_only_when_zero_raw_other_occurrences(tmp_path):
    from wuwaterm.term_preparation import TermReference

    translator = _service(tmp_path, (_record("椿", "Camellya"),))
    all_valid = "欢迎椿，找椿，看椿，带椿，选椿。"
    locked = _lock(translator, all_valid, to_chinese=False)
    # Unmixed (raw_other_count == 0): stays a contextual reference (0 locks)
    # and compresses to "(all occurrences in this text)".
    assert locked.locks == ()
    assert format_scoped_reference_lines(
        locked.locked_text, locked.references, to_chinese=False
    ) == ("椿 = Camellya (resonator) — (all occurrences in this text)",)

    # Mixed single-char (raw_other_count > 0): selectively locks all 5 admitted
    # mentions and leaves 红椿蕊 untouched with zero prompt reference leakage.
    mixed_many = "欢迎椿，找椿，看椿，带椿，选椿。红椿蕊很强。"
    locked_mixed = _lock(translator, mixed_many, to_chinese=False)
    assert len(locked_mixed.locks) == 5
    assert locked_mixed.references == ()
    assert locked_mixed.restore(locked_mixed.locked_text, to_en=True) == (
        "欢迎Camellya,找Camellya,看Camellya,带Camellya,选Camellya。红椿蕊很强。"
    )

    # Direct compression guard on format_scoped_reference_lines when a scoped
    # reference has >4 admitted occurrences plus 1 unadmitted occurrence.
    skill_text = "打变奏，打变奏，打变奏，打变奏，打变奏。舞曲的变奏。"
    positions = []
    pos = skill_text.find("变奏")
    while pos != -1:
        positions.append(pos)
        pos = skill_text.find("变奏", pos + 1)
    skill_ref = TermReference(
        "变奏",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=tuple(
            TermOccurrence(p, p + 2, "变奏", 0, p, p + 2) for p in positions[:-1]
        ),
        origin="scoped_skill_stem",
        reason="local_skill_action",
    )
    [skill_line] = format_scoped_reference_lines(
        skill_text, (skill_ref,), to_chinese=False
    )
    assert "(all occurrences in this text)" not in skill_line
    assert "ONLY at:" in skill_line
    assert "| NOT at:" in skill_line


def test_long_term_covered_occurrence_prevents_all_occurrences_compression():
    # Finding #3 regression on format_scoped_reference_lines: 5 admitted 变奏
    # skill stems + 1 变奏 covered by global 变奏技能 = Intro Skill. Even though
    # 变奏技能 is excluded from NOT at:, raw_other_count is 1, so 变奏 must keep
    # ONLY at: instead of compressing to "(all occurrences in this text)" and
    # must NOT add 变奏技能 to NOT at:.
    from wuwaterm.term_preparation import TermReference

    text = "先用变奏技能。切人变奏1，切人变奏2，切人变奏3，切人变奏4，切人变奏5。"
    global_pos = text.index("变奏技能")
    global_ref = TermReference(
        "变奏技能",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=(
            TermOccurrence(
                global_pos, global_pos + 4, "变奏技能", 0, global_pos, global_pos + 4
            ),
        ),
        origin="dictionary",
        reason="contextual",
    )
    stem_positions = []
    pos = text.find("变奏", global_pos + 4)
    while pos != -1:
        stem_positions.append(pos)
        pos = text.find("变奏", pos + 1)
    assert len(stem_positions) == 5
    scoped_ref = TermReference(
        "变奏",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=tuple(
            TermOccurrence(p, p + 2, "变奏", 0, p, p + 2) for p in stem_positions
        ),
        origin="scoped_skill_stem",
        reason="local_skill_action",
    )
    scoped_lines = format_scoped_reference_lines(
        text, (global_ref, scoped_ref), to_chinese=False
    )
    assert len(scoped_lines) == 1
    line = scoped_lines[0]
    assert line.startswith("变奏 = Intro Skill (core_term) — ONLY at: ")
    assert "(all occurrences in this text)" not in line
    assert "NOT at:" not in line


def test_single_newline_non_context_text_uses_line_level_derived_context_gate(tmp_path):
    translator = _service(
        tmp_path,
        (
            _record("共鸣者", "Resonator", category="core_term"),
            TermRecord(
                category="location",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Area_41043_Title",
                text_key="Area_41043_Title",
                zh="教学楼",
                en="Academia Hall",
            ),
            TermRecord(
                category="location",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Area_41037_Title",
                text_key="Area_41037_Title",
                zh="拉贝尔学部",
                en="Rabelle College",
            ),
        ),
    )
    # prepare_text() collapses \n\n to single \n on non-context requests;
    # line 1 (with 共鸣者) must still gate independently from line 2 (ordinary)
    # when single_newline_paragraphs=True (non-context path), while context
    # mode (single_newline_paragraphs=False) keeps single \n in one paragraph.
    non_context = translator.prepare_text("共鸣者前往拉贝尔学部。\n\n我们学校的教学楼很旧。")
    assert "\n\n" not in non_context and "\n" in non_context
    locked = translator._lock_terms_with_references(
        non_context,
        to_chinese=False,
        allow_nfkc_spelling=True,
        single_newline_paragraphs=True,
    )
    sources = [ref.source for ref in locked.references]
    assert "共鸣者" in sources
    assert "拉贝尔学部" in sources
    assert "教学楼" not in sources

    # Under translation-context-v1 (single_newline_paragraphs=False), an
    # intra-paragraph single \n does NOT split the paragraph.
    intra_para = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs("共鸣者前往\n教学楼。"),
        to_chinese=False,
        allow_nfkc_spelling=True,
        single_newline_paragraphs=False,
    )
    assert "教学楼" in [ref.source for ref in intra_para.references]


def test_paragraph_gated_derived_ref_with_unadmitted_occurrence_routes_to_scoped(
    tmp_path,
):
    # Finding #1 regression: when the same derived common-noun surface (or
    # soft-cased derived surface) passes paragraph gating in one paragraph and
    # fails it in another, it MUST route to Scoped term references with ONLY at
    # and NOT at — never into global Dictionary term references.
    translator = _service(
        tmp_path,
        (
            _record("共鸣者", "Resonator", category="core_term"),
            TermRecord(
                category="location",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Area_41043_Title",
                text_key="Area_41043_Title",
                zh="教学楼",
                en="Academia Hall",
            ),
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:43020001",
                text_key="tier-base:43020001",
                zh="能源核心",
                en="Energy Core",
            ),
        ),
    )

    zh_mixed = "共鸣者前往教学楼。\n\n我们学校的教学楼很旧。"
    zh_locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(zh_mixed),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    zh_prompt = _system_prompt(
        locked_text=zh_locked.locked_text,
        locks=zh_locked.locks,
        references=zh_locked.references,
    )
    global_block = zh_prompt.split("Dictionary term references:\n", 1)[1].split(
        "\nScoped term references:\n", 1
    )[0]
    scoped_block = zh_prompt.split("\nScoped term references:\n", 1)[1]
    assert "共鸣者 = Resonator (core_term)" in global_block
    assert "教学楼" not in global_block
    assert (
        "教学楼 = Academia Hall (location) — ONLY at: 共鸣者前往「教学楼」。 | NOT at: 我们学校的「教学楼」很旧。"
        in scoped_block
    )

    # When every occurrence of 教学楼 in the text is admitted, it stays global.
    zh_all_admitted = "共鸣者前往教学楼。"
    zh_all_locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(zh_all_admitted),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    zh_all_prompt = _system_prompt(
        locked_text=zh_all_locked.locked_text,
        locks=zh_all_locked.locks,
        references=zh_all_locked.references,
    )
    assert "Scoped term references:\n" not in zh_all_prompt
    assert "教学楼 = Academia Hall (location)" in zh_all_prompt

    # Same rule for derived_soft_casing in en->zh.
    en_mixed = "The energy core of a star.\n\nA Resonator upgrades the energy core."
    en_locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(en_mixed),
        to_chinese=True,
        allow_plural_inflection=True,
        allow_nfkc_spelling=True,
    )
    en_prompt = _system_prompt(
        locked_text=en_locked.locked_text,
        locks=en_locked.locks,
        to_chinese=True,
        references=en_locked.references,
    )
    assert "Dictionary term references:\n" not in en_prompt
    assert "Scoped term references:\n" in en_prompt
    assert "energy core = 能源核心 (item) — ONLY at:" in en_prompt
    assert "| NOT at:" in en_prompt


def test_colliding_scoped_snippets_escalate_until_strictly_disjoint():
    # Finding #2 regression: identical 6-char left/right windows must expand,
    # cross-paragraph identical texts must add [P1]/[P2], and same-paragraph
    # identical texts must add [start,end) so ONLY at and NOT at never collide.
    from wuwaterm.term_preparation import TermReference

    # 1. Same 6-char context on both sides, different full line -> window expands.
    line_text = (
        "共鸣者先释放再衔接变奏入场并打满循环。\n\n"
        "乐队先释放再衔接变奏入场并打满循环。"
    )
    pos_admitted = line_text.index("变奏")
    ref_line = TermReference(
        "变奏",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=(
            TermOccurrence(
                start=pos_admitted,
                end=pos_admitted + 2,
                matched_text="变奏",
                segment_index=0,
                model_start=pos_admitted,
                model_end=pos_admitted + 2,
            ),
        ),
        origin="scoped_skill_stem",
        reason="local_skill_action",
    )
    [line_out] = format_scoped_reference_lines(line_text, (ref_line,), to_chinese=False)
    only_part, not_part = line_out.split(" — ONLY at: ", 1)[1].split(" | NOT at: ", 1)
    assert only_part != not_part
    assert "共鸣者" in only_part
    assert "乐队" in not_part

    # 2. Identical text across two paragraphs -> [P1] / [P2] prefixes disambiguate.
    para_text = "直接触发变奏入场。\n\n直接触发变奏入场。"
    p1_pos = para_text.index("变奏")
    ref_para = TermReference(
        "变奏",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=(
            TermOccurrence(p1_pos, p1_pos + 2, "变奏", 0, p1_pos, p1_pos + 2),
        ),
        origin="scoped_skill_stem",
        reason="local_skill_action",
    )
    [para_out] = format_scoped_reference_lines(para_text, (ref_para,), to_chinese=False)
    p_only, p_not = para_out.split(" — ONLY at: ", 1)[1].split(" | NOT at: ", 1)
    assert p_only != p_not
    assert p_only.startswith("[P1] ")
    assert p_not.startswith("[P2] ")

    # 3. Identical text inside the same paragraph (e.g. separated by an opaque
    # placeholder) -> exact [start,end) coordinates disambiguate.
    ph = "__WUWA_HTML_0123456789abcdef_0000__"
    same_p_collide = f"触发变奏入场{ph}触发变奏入场"
    c1 = same_p_collide.index("变奏")
    c2 = same_p_collide.rindex("变奏")
    ref_c = TermReference(
        "变奏",
        "变奏技能",
        "Intro Skill",
        "core_term",
        occurrences=(TermOccurrence(c1, c1 + 2, "变奏", 0, c1, c1 + 2),),
        origin="scoped_skill_stem",
        reason="local_skill_action",
    )
    [c_out] = format_scoped_reference_lines(same_p_collide, (ref_c,), to_chinese=False)
    c_only, c_not = c_out.split(" — ONLY at: ", 1)[1].split(" | NOT at: ", 1)
    assert c_only != c_not
    assert f"[{c1},{c1 + 2})" in c_only
    assert f"[{c2},{c2 + 2})" in c_not


def test_paragraph_level_official_term_context_gates_derived_and_soft_casing_references(
    tmp_path,
):
    translator = _service(
        tmp_path,
        (
            _record("共鸣者", "Resonator", category="core_term"),
            TermRecord(
                category="location",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Area_41043_Title",
                text_key="Area_41043_Title",
                zh="教学楼",
                en="Academia Hall",
            ),
            TermRecord(
                category="location",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Area_41037_Title",
                text_key="Area_41037_Title",
                zh="拉贝尔学部",
                en="Rabelle College",
            ),
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:43020001",
                text_key="tier-base:43020001",
                zh="能源核心",
                en="Energy Core",
            ),
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:36000001",
                text_key="tier-base:36000001",
                zh="密音筒",
                en="Sealed Tube",
            ),
        ),
    )

    # zh->en multi-paragraph: p0 has official 共鸣者 -> 拉贝尔学部 admitted;
    # p1 is ordinary prose with no official term -> 教学楼 dropped.
    zh_multi = "共鸣者前往拉贝尔学部。\n\n我们学校的教学楼很旧。"
    zh_locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(zh_multi),
        to_chinese=False,
        allow_nfkc_spelling=True,
    )
    zh_sources = [ref.source for ref in zh_locked.references]
    assert "共鸣者" in zh_sources
    assert "拉贝尔学部" in zh_sources
    assert "教学楼" not in zh_sources

    # en->zh multi-paragraph: p0 is ordinary prose -> energy core dropped;
    # p1 has official Resonator -> sealed tubes admitted.
    en_multi = "The energy core of a star.\n\nA Resonator uses sealed tubes."
    en_locked = translator._lock_terms_with_references(
        translator.prepare_text_keeping_paragraphs(en_multi),
        to_chinese=True,
        allow_plural_inflection=True,
        allow_nfkc_spelling=True,
    )
    en_sources = [ref.source for ref in en_locked.references]
    assert "sealed tubes" in en_sources
    assert "energy core" not in en_sources


def test_embedded_character_compound_is_not_a_reference(tmp_path):
    # matcher v9: the 椿 of 红椿蕊 is a component of another word, so it is not
    # a mention at all, and the segment keeps the prompt it would have without
    # any dictionary hit.
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "红椿蕊很强。", to_chinese=False)

    assert locked.references == ()
    assert references_need_scope_instruction(locked.references) is False
    assert locked.locked_text == "红椿蕊很强。"


def test_competing_official_pairs_are_alternatives_not_a_lock(tmp_path):
    # The speaker 调音师 and the derived material 调谐器 share "Tuner". Both pairs
    # reach the model and neither is forced.
    translator = _service(
        tmp_path,
        (
            _record("调音师", "Tuner", category="speaker", table="speaker"),
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:36000011",
                text_key="tier-base:36000011",
                zh="调谐器",
                en="Tuner",
            ),
        ),
    )

    locked = _lock(translator, "Tuner: hold that note.", to_chinese=True)

    assert locked.locks == ()
    assert tuple(ref.as_tuple() for ref in locked.references) == (
        ("Tuner", "调音师", "Tuner", "speaker"),
        ("Tuner", "调谐器", "Tuner", "item"),
    )
    assert all(ref.reason == "ambiguous_alternative" for ref in locked.references)
    assert references_need_scope_instruction(locked.references) is True
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        to_chinese=True,
        references=locked.references,
    )
    assert _SCOPED_REFERENCE_INSTRUCTION in prompt
    assert _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION in prompt
    assert "Reference occurrence ranges" not in prompt


def test_competing_speaker_pair_gets_the_speaker_label_rule(tmp_path):
    # ordinary-en-zh#p1: "Use a Basic Tuner first, then the Tuner." came back as
    # 调音师 because the (speaker) pair reached the model first with nothing
    # saying a speaker label is not an item. Only a surface that carries both a
    # (speaker) entry and another kind of entry earns the rule.
    translator = _service(
        tmp_path,
        (
            _record("调音师", "Tuner", category="speaker", table="speaker"),
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:36000011",
                text_key="tier-base:36000011",
                zh="调谐器",
                en="Tuner",
            ),
        ),
    )

    locked = _lock(translator, "Use a Tuner first, then the Tuner.", to_chinese=True)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        to_chinese=True,
        references=locked.references,
    )

    assert "Tuner = 调音师 (speaker)" in prompt
    assert "Tuner = 调谐器 (item)" in prompt
    assert _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION in prompt
    # The rule rides immediately after the competing-pairs instruction, so a
    # reader of the prompt sees the exception in the same block.
    assert (
        _SCOPED_REFERENCE_INSTRUCTION + _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION
    ) in prompt


def test_plain_references_keep_the_prompt_without_the_speaker_label_rule(tmp_path):
    # No competing (speaker) pair: a single-meaning reference set must reach the
    # model with the prompt bytes it carried before the rule existed.
    translator = _service(
        tmp_path,
        (
            _record("今汐", "Jinhsi"),
            _record("调音师", "Tuner", category="speaker", table="speaker"),
        ),
    )

    locked = _lock(translator, "今汐登场，调音师到场。", to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )

    assert _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION not in prompt
    assert _SCOPED_REFERENCE_INSTRUCTION not in prompt
    assert prompt == (
        "Translate Chinese Wuthering Waves text into English. "
        + _UNTRUSTED_SOURCE_INSTRUCTION
        + "Keep all placeholders exactly unchanged. "
        "Do not paraphrase locked official terms. Return English only.\n"
        "Locked terms:\n\n"
        + _CONTEXTUAL_REFERENCE_INSTRUCTION
        + "Dictionary term references:\n"
        "今汐 = Jinhsi (resonator)\n"
        "调音师 = Tuner (speaker)"
    )


def test_competing_pairs_without_a_speaker_keep_the_prompt_without_the_rule(tmp_path):
    # 守岸人 carries two resonator pairs (Shorekeeper / The Shorekeeper). That is
    # a competing-pair request and still gets the alternatives instruction, but
    # no speaker is involved, so the label rule must not appear: it talks about
    # (speaker) meanings that this request does not offer.
    translator = _service(
        tmp_path,
        (
            _record("守岸人", "Shorekeeper"),
            _record("守岸人", "The Shorekeeper"),
        ),
    )

    locked = _lock(translator, "守岸人登场。", to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )

    assert references_need_scope_instruction(locked.references) is True
    assert _SCOPED_REFERENCE_INSTRUCTION in prompt
    assert _SPEAKER_LABEL_DISAMBIGUATION_INSTRUCTION not in prompt


@pytest.mark.parametrize("derived_count", [1, 6])
def test_exact_translation_keeps_established_owner_across_entry_points(
    tmp_path, monkeypatch, derived_count
):
    records = [_record("调音师", "Tuner", category="speaker", table="speaker")]
    records.extend(
        TermRecord(
            category="item",
            source_file="BinData/item/iteminfo.json",
            source_id=f"tier-base:{number}",
            text_key=f"tier-base:{number}",
            zh=f"调谐器{number}",
            en="Tuner",
        )
        for number in range(derived_count)
    )
    translator = _service(tmp_path, records)
    service = translator.service
    assert service.lookup_exact("Tuner").best.entry.category == "item"

    def unexpected_model(*_args, **_kwargs):
        pytest.fail("exact translations must not call the model")

    monkeypatch.setattr("wuwaterm.sentence._call_llm", unexpected_model)
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", unexpected_model)

    assert translator.translate("Tuner", to_chinese=True) == "调音师"
    assert asyncio.run(translator.translate_async("Tuner", to_chinese=True)) == "调音师"
    sync_outcome = translate_request(service, translator, TranslationJob(text="Tuner"))
    async_outcome = asyncio.run(
        translate_request_async(service, translator, TranslationJob(text="Tuner"))
    )
    assert (sync_outcome.kind, sync_outcome.text) == (KIND_EXACT, "调音师")
    assert (async_outcome.kind, async_outcome.text) == (KIND_EXACT, "调音师")
    results = lookup_exact_terms(service, "Tuner", limit=derived_count + 1)
    assert {result.zh for result in results} == {record.zh for record in records}


def test_exact_translation_keeps_a_derived_entry_when_it_is_the_only_hit(tmp_path):
    translator = _service(
        tmp_path,
        (
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:1",
                text_key="tier-base:1",
                zh="调谐器",
                en="Tuner",
            ),
        ),
    )
    assert translator.translate("Tuner", to_chinese=True) == "调谐器"


def test_soft_casing_needs_term_context_in_the_segment(tmp_path):
    # A derived base gains lowercase/uppercase spellings only where the segment
    # already references a term. Isolated ordinary prose stays prose.
    translator = _service(
        tmp_path,
        (
            TermRecord(
                category="item",
                source_file="BinData/item/iteminfo.json",
                source_id="tier-base:36000001",
                text_key="tier-base:36000001",
                zh="密音筒",
                en="Sealed Tube",
            ),
            _record("声骸", "Echo", category="item"),
        ),
    )

    with_context = _lock(translator, "Use the Echo to open sealed tubes.", to_chinese=True)
    assert ("sealed tubes", "密音筒", "Sealed Tube", "item") in {
        ref.as_tuple() for ref in with_context.references
    }
    soft_ref = next(ref for ref in with_context.references if ref.source == "sealed tubes")
    assert soft_ref.origin == "derived_soft_casing"
    assert soft_ref.reason == "soft_casing"
    assert references_need_scope_instruction(with_context.references) is True

    plain = _lock(translator, "The energy core of a star.", to_chinese=True)
    assert plain.references == ()
    assert references_need_scope_instruction(plain.references) is False


def test_derived_version_title_is_a_reference_not_a_lock(tmp_path):
    translator = _service(
        tmp_path,
        (
            TermRecord(
                category="core_term",
                source_file="Textmaps/multi_text/MultiText.json",
                source_id="Advertising_VersionTitle_34",
                text_key="Advertising_VersionTitle_34",
                zh="测试标题丁",
                en="Test Title Delta",
            ),
        ),
    )

    locked = _lock(translator, "Test Title Delta is here.", to_chinese=True)

    assert locked.locks == ()
    assert tuple(ref.as_tuple() for ref in locked.references) == (
        ("Test Title Delta", "测试标题丁", "Test Title Delta", "core_term"),
    )
    assert locked.references[0].reason == "reference_only"
    assert references_need_scope_instruction(locked.references) is False


def test_context_request_without_reference_scope_omits_the_scope_block(tmp_path):
    translator = _service(tmp_path, (_record("今汐", "Jinhsi", table="a"),))

    locked = _lock(translator, "今汐登场。", to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
        context=_context(),
    )

    assert _SCOPED_REFERENCE_INSTRUCTION not in prompt
    assert "Reference occurrence ranges" not in prompt
    assert "Context rules" in prompt
    assert "3+1 or S0R1" in prompt


class _Context:
    before = None
    precedents = ()


def _context():
    return _Context()


# The zh->en counterpart of the en->zh protocol sentence: a locked span is a
# placeholder the model has to hand back untouched, and saying so only as
# "keep all placeholders exactly unchanged" sat next to a `Locked terms:` line
# that spells out the same term's English.
_ZH_EN_PLACEHOLDER_PROTOCOL = (
    "Placeholder tokens (such as __WUWA_TERM_...__) are mandatory protocol "
    "syntax, not natural-language output. Copy every placeholder token "
    "byte-for-byte exactly once (including inside parentheses or formulas like "
    "'(TOKEN+A+B)'). Never replace a placeholder with the official term shown "
    "under Locked terms; the server validates and restores official terms after "
    "your response.\n"
)
_PLAIN_PLACEHOLDER_SENTENCE = "Keep all placeholders exactly unchanged. "
_PLACEHOLDER_TOKEN_RE = re.compile(r"__WUWA_TERM_[0-9a-f]{16}_[0-9]{4,}__")


def test_zh_en_locked_spans_carry_the_placeholder_protocol(tmp_path):
    # 椿的红椿蕊很强: the admitted 椿 is locked in place, so the request now
    # carries a token the model could be tempted to write as `Camellya` --
    # exactly the shape that made D-003's parenthesised team formulas fail
    # fail-closed restore on a 200 OK answer.
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "椿的红椿蕊很强。", to_chinese=False)
    assert len(locked.locks) == 1
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )

    assert _ZH_EN_PLACEHOLDER_PROTOCOL in prompt
    assert _PLAIN_PLACEHOLDER_SENTENCE not in prompt
    assert prompt.startswith(
        "Translate Chinese Wuthering Waves text into English. "
        + _UNTRUSTED_SOURCE_INSTRUCTION
        + _ZH_EN_PLACEHOLDER_PROTOCOL
        + "Do not paraphrase locked official terms. Return English only.\n"
    )


def test_zh_en_without_locked_spans_keeps_the_plain_prompt(tmp_path):
    # The protocol sentence is paid for only by a request that carries a
    # placeholder. A plain non-locked zh->en request keeps the pre-admission
    # prompt byte for byte, which is what makes the change auditable.
    translator = _service(tmp_path, (_record("今汐", "Jinhsi"),))

    locked = _lock(translator, "今汐登场。", to_chinese=False)
    assert locked.locks == ()
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )

    assert prompt == _PLAIN_PROMPT_ZH_EN
    assert _ZH_EN_PLACEHOLDER_PROTOCOL not in prompt
    assert _PLAIN_PLACEHOLDER_SENTENCE in prompt


def test_zh_en_html_mode_keeps_its_structural_placeholder_instruction(tmp_path):
    # Markup requests already carry the structural-placeholder sentence; the
    # plain-text protocol sentence would only compete with it.
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "椿的红椿蕊很强。", to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        html_mode=True,
    )

    assert _ZH_EN_PLACEHOLDER_PROTOCOL not in prompt
    assert _PLAIN_PLACEHOLDER_SENTENCE in prompt
    assert "copy every structural placeholder exactly once" in prompt


def test_restore_rejection_carries_its_detail_and_logs_no_source_text(
    tmp_path, caplog
):
    # D-003's actual failure: the model answered 200 OK with the official
    # English term where the token was. The answer is rejected fail-closed and
    # the diagnostic has to say which placeholder rule broke -- and how many
    # tokens it saw -- so an offline harness can tell this apart from a
    # provider timeout. Neither the log nor the diagnostic carries text.
    def expand_token(request):
        user_text = json.loads(request.content.decode())["messages"][-1]["content"]
        content = _PLACEHOLDER_TOKEN_RE.sub("Camellya", user_text)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    translator = _service(
        tmp_path,
        (_record("椿", "Camellya"),),
        transport=httpx.MockTransport(expand_token),
    )
    locked = _lock(translator, "椿的红椿蕊很强。", to_chinese=False)
    placeholder = locked.locks[0][0]
    with pytest.raises(LLMTranslationError) as excinfo:
        locked.restore("Camellya的红椿蕊很强。", to_en=True)
    assert excinfo.value.reason == "invalid_response"
    assert excinfo.value.diagnostic.detail == "missing_placeholder"
    assert excinfo.value.diagnostic.expected_count == 1
    assert excinfo.value.diagnostic.actual_count == 0

    with caplog.at_level(logging.WARNING, logger="wuwaterm.llm_transport"):
        swallowed = asyncio.run(translator.translate_async("椿的红椿蕊很强。"))

    assert swallowed == TRANSLATION_UNAVAILABLE_NOTICE
    assert "reason=invalid_response detail=missing_placeholder" in caplog.text
    assert "expected=1 actual=0" in caplog.text
    assert "椿的红椿蕊很强" not in caplog.text
    assert "Camellya的红椿蕊很强" not in caplog.text
    assert placeholder not in caplog.text
