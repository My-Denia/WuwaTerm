"""Closed-class 2-char skill stem references backed by TermReference/TermOccurrence."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.models import TermRecord
from wuwaterm.protected_text import ProtectedStructure, ProtectedText
from wuwaterm.sentence import (
    SentenceTranslator,
    _SCOPED_REFERENCE_HEADER,
    _call_llm_async_with_client,
)
from wuwaterm.term_preparation import (
    TermOccurrence,
    TermReference,
    validated_reference_ranges,
)


SKILLS = (
    ("2", "共鸣技能", "Resonance Skill"),
    ("3", "共鸣解放", "Resonance Liberation"),
    ("5", "变奏技能", "Intro Skill"),
    ("6", "共鸣回路", "Forte Circuit"),
    ("11", "延奏技能", "Outro Skill"),
)


def _skill(number, zh, en):
    return TermRecord(
        category="core_term",
        source_file="BinData/skill/skilltype.json",
        source_id=number,
        text_key=f"SkillType_{number}_TypeName",
        zh=zh,
        en=en,
    )


def _translator(tmp_path, records=SKILLS, extra=(), transport=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "skill-references.db"
    with connect(path) as conn:
        initialize(conn)
        insert_records(conn, [*(_skill(*record) for record in records), *extra])
        conn.commit()
    return SentenceTranslator(path, llm_transport=transport)


def _lock(translator, source, *, to_chinese: bool = False):
    return translator._lock_terms_with_references(
        translator.prepare_text(source), to_chinese=to_chinese
    )


def _scoped_section(body):
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    section = system.split(_SCOPED_REFERENCE_HEADER, 1)[1].split("\nContext rules:", 1)[0]
    return user, section.splitlines()


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")


def test_occurrence_scopes_preserve_ordinary_and_mixed_usage(tmp_path):
    translator = _translator(tmp_path)
    source = "舞曲的变奏引起共鸣。切换角色触发变奏入场。角色打出变奏伤害。"
    locked = _lock(translator, source)

    skill_refs = [ref for ref in locked.references if ref.origin == "scoped_skill_stem"]
    assert len(skill_refs) == 1
    ref = skill_refs[0]
    assert ref.as_tuple() == ("变奏", "变奏技能", "Intro Skill", "core_term")
    assert ref.reason == "local_skill_action"
    first = source.index("变奏", source.index("。") + 1)
    second = source.rindex("变奏")
    assert ref.occurrences == (
        TermOccurrence(
            start=first,
            end=first + 2,
            matched_text="变奏",
            segment_index=0,
            model_start=first,
            model_end=first + 2,
        ),
        TermOccurrence(
            start=second,
            end=second + 2,
            matched_text="变奏",
            segment_index=0,
            model_start=second,
            model_end=second + 2,
        ),
    )
    assert validated_reference_ranges(
        locked.locked_text, locked.references, to_chinese=False
    ) == [
        {
            "source": "变奏",
            "target": "Intro Skill",
            "ranges": [[first, first + 2], [second, second + 2]],
        }
    ]
    for ordinary in (
        "音乐家使用变奏曲并入场。",
        "她用变奏来写音乐。",
        "乐曲使用变奏表现角色的心情。",
        "演奏者用变奏入场。",
        "演奏者的变奏伤害了听众。",
    ):
        assert _lock(translator, ordinary).references == ()


def test_en_zh_direction_never_triggers_skill_stem_references(tmp_path):
    translator = _translator(tmp_path)
    # Zero bloat on en->zh even when CJK skill text appears in a mixed request.
    locked = _lock(
        translator,
        "Switch Resonator: 切换角色触发变奏入场。",
        to_chinese=True,
    )
    assert not any(ref.origin == "scoped_skill_stem" for ref in locked.references)


def test_switching_an_official_character_is_local_game_evidence(tmp_path):
    speaker = TermRecord(
        category="speaker",
        source_file="fixture",
        source_id="speaker",
        text_key="speaker",
        zh="测试角色",
        en="Test Character",
    )
    translator = _translator(tmp_path, extra=(speaker,))
    locked = _lock(translator, "切测试角色变奏入场。")
    skill_refs = [ref for ref in locked.references if ref.origin == "scoped_skill_stem"]
    assert len(skill_refs) == 1
    assert skill_refs[0].as_tuple() == ("变奏", "变奏技能", "Intro Skill", "core_term")
    assert len(skill_refs[0].occurrences) == 1


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("技能", "Resonance Skill"),
        ("解放", "Resonance Liberation"),
        ("回路", "Forte Circuit"),
    ],
)
def test_shared_prefix_requires_a_unique_local_sense(tmp_path, marker, expected):
    translator = _translator(tmp_path)
    locked = _lock(translator, f"释放{marker}后触发共鸣伤害。")
    skill_refs = [ref for ref in locked.references if ref.origin == "scoped_skill_stem"]
    assert [(ref.source, ref.en) for ref in skill_refs] == [("共鸣", expected)]
    assert len(skill_refs[0].occurrences) == 1


def test_ambiguous_prefix_is_omitted_without_unique_evidence(tmp_path):
    translator = _translator(tmp_path)
    assert _lock(translator, "切换角色触发共鸣伤害。").references == ()
    assert _lock(translator, "释放技能和解放后触发共鸣伤害。").references == ()
    partial = _translator(tmp_path / "partial", records=(SKILLS[0],))
    assert _lock(partial, "切换角色触发共鸣伤害。").references == ()


def test_hints_require_the_official_row_and_never_enter_the_hard_lock_path(tmp_path):
    fake = TermRecord(
        category="core_term",
        source_file="fixture",
        source_id="5",
        text_key="SkillType_5_TypeName",
        zh="变奏技能",
        en="Intro Skill",
    )
    translator = _translator(tmp_path, records=(), extra=(fake,))
    assert _lock(translator, "角色触发变奏入场。").references == ()

    official = _translator(tmp_path / "official")
    assert official.lock_terms("角色触发变奏入场。").references == ()
    assert official.lock_terms("角色触发变奏入场。").locked_text == "角色触发变奏入场。"
    # Covered by longer official term 变奏技能 -> no scoped_skill_stem emitted.
    covered = _lock(official, "角色触发变奏技能。")
    assert not any(ref.origin == "scoped_skill_stem" for ref in covered.references)
    assert tuple(ref.as_tuple() for ref in covered.references) == (
        ("变奏技能", "变奏技能", "Intro Skill", "core_term"),
    )


@pytest.mark.parametrize("mode", ["sync", "async_context", "protected", "async_protected"])
def test_actual_request_contains_exact_ranges_after_placeholders(tmp_path, monkeypatch, mode):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]},
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    # An unknown category exercises a preceding hard term placeholder on the
    # zh-en path. Protected mode also adds opaque structural placeholders.
    hard_term = TermRecord(
        category="",
        source_file="fixture",
        source_id="x",
        text_key="x",
        zh="固定字词",
        en="Fixed Term",
    )
    translator = _translator(tmp_path, extra=(hard_term,), transport=transport)
    text = "固定字词，切换角色触发变奏入场。"
    if mode == "sync":
        translator.translate(text)
    elif mode == "async_context":

        async def translate_context():
            context = type("Context", (), {"before": None, "precedents": ()})()
            await translator.translate_async(text, context=context)
            await translator._close_llm_client()

        asyncio.run(translate_context())
    else:
        protected = ProtectedText(
            ("固定字词，切换角色", "触发变奏入场", "。"),
            (ProtectedStructure("<b>"), ProtectedStructure("</b>")),
        )
        if mode == "protected":
            translator.translate_protected(protected)
        else:

            async def translate_protected():
                await translator.translate_protected_async(protected)
                await translator._close_llm_client()

            asyncio.run(translate_protected())

    assert len(bodies) == 1
    user, scoped_lines = _scoped_section(bodies[0])
    assert "__WUWA_TERM_" in user
    if mode in {"protected", "async_protected"}:
        assert "__WUWA_HTML_" in user
    assert len(scoped_lines) == 1
    assert scoped_lines[0].startswith("变奏 = Intro Skill (core_term) — ONLY at: ")
    assert "「变奏」入场" in scoped_lines[0]
    assert "__WUWA_" not in scoped_lines[0]
    assert "Dictionary term references:\n" not in bodies[0]["messages"][0]["content"]


def test_actual_request_binds_each_ambiguous_prefix_to_its_own_target(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]},
        )

    translator = _translator(tmp_path, transport=httpx.MockTransport(handler))
    text = "释放技能后触发共鸣伤害。释放解放后触发共鸣伤害。"

    async def run():
        await translator.translate_async(text)
        await translator._close_llm_client()

    asyncio.run(run())
    assert len(bodies) == 1
    _user, scoped_lines = _scoped_section(bodies[0])
    assert scoped_lines == [
        "共鸣 = Resonance Skill (core_term) — ONLY at: 放技能后触发「共鸣」伤害。释放解 | NOT at: 放解放后触发「共鸣」伤害。",
        "共鸣 = Resonance Liberation (core_term) — ONLY at: 放解放后触发「共鸣」伤害。 | NOT at: 放技能后触发「共鸣」伤害。释放解",
    ]


def test_read_only_previous_context_is_not_skill_admission_evidence(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]},
        )

    translator = _translator(tmp_path, transport=httpx.MockTransport(handler))
    before = type("Before", (), {"source": "角色触发", "target": "The character triggers"})()
    context = type("Context", (), {"before": before, "precedents": ()})()

    async def run():
        await translator.translate_async("她的变奏让听众共鸣。", context=context)
        await translator._close_llm_client()

    asyncio.run(run())
    assert len(bodies) == 1
    assert _SCOPED_REFERENCE_HEADER not in bodies[0]["messages"][0]["content"]
    assert "变奏 = Intro Skill" not in bodies[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "bad_ref",
    [
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(),
            origin="scoped_skill_stem",
        ),
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 1, "变", 0, 0, 1),),
            origin="scoped_skill_stem",
        ),
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 2, "变奏", 0, -1, 1),),
            origin="scoped_skill_stem",
        ),
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 2, "变奏", 0, 0, 3),),
            origin="scoped_skill_stem",
        ),
        TermReference(
            "别的",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 2, "别的", 0, 0, 2),),
            origin="scoped_skill_stem",
        ),
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 2, "变奏", 0, True, 2),),  # type: ignore[arg-type]
            origin="scoped_skill_stem",
        ),
        TermReference(
            "变奏",
            "变奏技能",
            "Intro Skill",
            "core_term",
            occurrences=(TermOccurrence(0, 2, "变奏", 0, None, None),),
            origin="scoped_skill_stem",
        ),
    ],
)
def test_bad_range_fails_before_the_http_request(bad_ref):
    def handler(_request):
        pytest.fail("a malformed range must not reach the model")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="reference range"):
                await _call_llm_async_with_client(
                    client, "变奏", (), references=(bad_ref,)
                )

    asyncio.run(run())


def test_protected_placeholder_width_is_per_placeholder():
    count = 10_001
    protected = ProtectedText(("",) * (count + 1), (ProtectedStructure("<b>"),) * count)
    assert protected.placeholder_lengths[-1] == protected.placeholder_lengths[0] + 1
