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
    _SCOPED_REFERENCE_INSTRUCTION,
    _UNTRUSTED_SOURCE_INSTRUCTION,
    _call_llm_async_with_client,
)
from wuwaterm.term_preparation import (
    TermOccurrence,
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
    assert "Reference occurrence ranges (JSON; zero-based, end-exclusive):\n" in prompt
    assert '[{"source": "椿", "ranges": [[2, 3]]}]' in prompt
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
