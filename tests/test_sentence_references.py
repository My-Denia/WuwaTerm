"""Reference admission and prompt scoping (MY-18 / MY-380).

The dictionary can now offer several official pairs for one surface, a soft
casing of a derived name, and occurrence ranges for a one-character name. All
three are opt-in per request: a sentence that needs none of them must reach the
model carrying the exact prompt bytes it carried ahead of these mechanisms.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.models import TermRecord
from wuwaterm.sentence import (
    SentenceTranslator,
    _CONTEXT_RULES_INSTRUCTION,
    _CONTEXTUAL_REFERENCE_INSTRUCTION,
    _SCOPED_REFERENCE_INSTRUCTION,
    _UNTRUSTED_SOURCE_INSTRUCTION,
    _call_llm_async_with_client,
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

    assert locked.references == (("今汐", "今汐", "Jinhsi", "resonator"),)
    assert locked.reference_spans == ()
    assert locked.reference_scope is False
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
        reference_spans=locked.reference_spans,
        reference_scope=locked.reference_scope,
    )
    assert prompt == _PLAIN_PROMPT_ZH_EN


def test_one_character_name_scopes_its_occurrences(tmp_path):
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "欢迎椿来到。", to_chinese=False)

    assert locked.references == (("椿", "椿", "Camellya", "resonator"),)
    assert locked.reference_spans == (("椿", 2, 3),)
    assert locked.reference_scope is True
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
        reference_spans=locked.reference_spans,
        reference_scope=locked.reference_scope,
    )
    assert _SCOPED_REFERENCE_INSTRUCTION in prompt
    assert "Reference occurrence ranges (JSON; zero-based, end-exclusive):\n" in prompt
    assert '[{"source": "椿", "ranges": [[2, 3]]}]' in prompt
    # The ranges follow the placeholders, so the model reads the user message
    # coordinates, not the pre-lock ones.
    assert locked.locked_text == "欢迎椿来到。"


def test_embedded_character_compound_is_not_a_reference(tmp_path):
    # matcher v8: the 椿 of 红椿蕊 is a component of another word, so it is not
    # a mention at all, and the segment keeps the prompt it would have without
    # any dictionary hit.
    translator = _service(tmp_path, (_record("椿", "Camellya"),))

    locked = _lock(translator, "红椿蕊很强。", to_chinese=False)

    assert locked.references == ()
    assert locked.reference_spans == ()
    assert locked.reference_scope is False
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
    assert locked.references == (
        ("Tuner", "调音师", "Tuner", "speaker"),
        ("Tuner", "调谐器", "Tuner", "item"),
    )
    assert locked.reference_scope is True
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
        reference_spans=locked.reference_spans,
        reference_scope=locked.reference_scope,
    )
    assert _SCOPED_REFERENCE_INSTRUCTION in prompt
    assert "Reference occurrence ranges" not in prompt


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
    assert ("sealed tubes", "密音筒", "Sealed Tube", "item") in with_context.references
    assert with_context.reference_scope is True

    plain = _lock(translator, "The energy core of a star.", to_chinese=True)
    assert plain.references == ()
    assert plain.reference_scope is False


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
    assert locked.references == (
        ("Test Title Delta", "测试标题丁", "Test Title Delta", "core_term"),
    )
    assert locked.reference_scope is False


def test_context_rules_carry_the_actor_and_clause_rules():
    # The opt-in context prompt states how to resolve an omitted actor and how
    # to keep clauses complete; it does not add the idiom sentence the earlier
    # candidate carried.
    assert "Resolve an omitted actor from the complete clause" in _CONTEXT_RULES_INSTRUCTION
    assert "numeric shorthand such as 3+1 or S0R1" in _CONTEXT_RULES_INSTRUCTION
    assert "Translate every clause" in _CONTEXT_RULES_INSTRUCTION
    assert "idioms, irony" not in _CONTEXT_RULES_INSTRUCTION


def test_context_request_without_reference_scope_omits_the_scope_block(tmp_path):
    translator = _service(tmp_path, (_record("今汐", "Jinhsi", table="a"),))

    locked = _lock(translator, "今汐登场。", to_chinese=False)
    prompt = _system_prompt(
        locked_text=locked.locked_text,
        locks=locked.locks,
        references=locked.references,
        reference_spans=locked.reference_spans,
        reference_scope=locked.reference_scope,
        context=_context(),
    )

    assert _SCOPED_REFERENCE_INSTRUCTION not in prompt
    assert "Reference occurrence ranges" not in prompt
    assert "Context rules" in prompt


class _Context:
    before = None
    precedents = ()


def _context():
    return _Context()
