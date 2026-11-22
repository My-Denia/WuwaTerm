"""Reference admission and prompt scoping on TermReference/TermOccurrence (MY-18 / MY-380).

The dictionary can offer several official pairs for one surface, a soft casing
of a derived name, and occurrence ranges for an admitted one-character name.
All three are opt-in per request: a sentence that needs none of them must reach
the model carrying the exact prompt bytes it carried ahead of these mechanisms.
"""

from __future__ import annotations

import asyncio
import json

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
    _CONTEXTUAL_REFERENCE_INSTRUCTION,
    _SCOPED_REFERENCE_HEADER,
    _SCOPED_REFERENCE_INSTRUCTION,
    _UNTRUSTED_SOURCE_INSTRUCTION,
    _call_llm_async_with_client,
)
from wuwaterm.term_preparation import (
    TermOccurrence,
    format_scoped_reference_lines,
    references_need_scope_instruction,
    validated_reference_ranges,
)


def _service(tmp_path, records, metadata=()):
    path = tmp_path / "sentence-refs.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture"), *metadata),
        )
        insert_records(conn, records)
        conn.commit()
    return SentenceTranslator(path)


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


def test_mixed_valid_mention_and_embedded_compound_scopes_only_the_valid_mention(tmp_path):
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "椿的红椿蕊很强。", to_chinese=False)

    assert len(locked.references) == 1
    ref = locked.references[0]
    assert ref.as_tuple() == ("椿", "椿", "Camellya", "resonator")
    assert ref.occurrences == (
        TermOccurrence(
            start=0, end=1, matched_text="椿", segment_index=0, model_start=0, model_end=1
        ),
    )
    assert validated_reference_ranges(
        locked.locked_text, locked.references, to_chinese=False
    ) == [{"source": "椿", "ranges": [[0, 1]]}]
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert "Scoped term references:\n" in prompt
    assert "椿 = Camellya (resonator) — ONLY at: 「椿」的红椿蕊很强 | NOT at: 椿的红「椿」蕊很强。" in prompt


def test_single_char_heart_separates_only_at_from_ordinary_not_at_paragraphs(tmp_path):
    translator = _service(tmp_path, (_record("心", "Hsin"),))
    source = "心很重要。\n\n她的心很痛。\n\n心加入队伍。"
    locked = _lock(translator, source, to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert "Dictionary term references:\n" not in prompt
    assert (
        "Scoped term references:\n"
        "心 = Hsin (resonator) — ONLY at: 「心」加入队伍。 | NOT at: 「心」很重要。; 她的「心」很痛。"
    ) in prompt


def test_many_admitted_occurrences_compress_only_when_zero_raw_other_occurrences(tmp_path):
    translator = _service(tmp_path, (_record("椿", "Camellya"),))
    all_valid = "欢迎椿，找椿，看椿，带椿，选椿。"
    locked = _lock(translator, all_valid, to_chinese=False)
    assert format_scoped_reference_lines(
        locked.locked_text, locked.references, to_chinese=False
    ) == ("椿 = Camellya (resonator) — (all occurrences in this text)",)

    # Whenever any unadmitted occurrence exists (raw_other_count > 0), never
    # compress to "(all occurrences in this text)"; keep ONLY at / NOT at.
    mixed_many = "欢迎椿，找椿，看椿，带椿，选椿。红椿蕊很强。"
    locked_mixed = _lock(translator, mixed_many, to_chinese=False)
    mixed_lines = format_scoped_reference_lines(
        locked_mixed.locked_text, locked_mixed.references, to_chinese=False
    )
    assert len(mixed_lines) == 1
    assert "(all occurrences in this text)" not in mixed_lines[0]
    assert "ONLY at:" in mixed_lines[0]
    assert "| NOT at: 椿,选椿。红「椿」蕊很强。" in mixed_lines[0]


def test_long_term_covered_occurrence_prevents_all_occurrences_compression(tmp_path):
    # Finding #3 regression: 5 standalone 心 + 1 「心」 covered by global speaker
    # 「心」 = "Hsin". Even though 「心」 is excluded from NOT at:, raw_other_count
    # is 1, so 心 must keep ONLY at: instead of compressing to
    # "(all occurrences in this text)" and must NOT add 「心」 to NOT at:.
    translator = _service(
        tmp_path,
        (
            _record("「心」", '"Hsin"', category="speaker", table="sp"),
            _record("心", "Hsin", category="resonator", table="res"),
        ),
    )
    source = "「心」出现了。心加入，心离开，心登场，心使用，心说。"
    locked = _lock(translator, source, to_chinese=False)
    scoped_lines = format_scoped_reference_lines(
        locked.locked_text, locked.references, to_chinese=False
    )
    assert len(scoped_lines) == 1
    line = scoped_lines[0]
    assert line.startswith("心 = Hsin (resonator) — ONLY at: ")
    assert "(all occurrences in this text)" not in line
    assert "NOT at:" not in line
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
    )
    assert 'Dictionary term references:\n「心」 = "Hsin" (speaker)' in prompt
    assert f"Scoped term references:\n{line}" in prompt


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
    assert "Reference occurrence ranges" not in prompt


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
