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
    _REFERENCE_RANGE_HEADER,
    _call_llm,
    _call_llm_async_with_client,
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


def _lock(translator, source):
    return translator._lock_terms_with_references(
        translator.prepare_text(source), to_chinese=False
    )


def _ranges(body):
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    payload = json.loads(system.split(_REFERENCE_RANGE_HEADER, 1)[1].splitlines()[0])
    for item in payload:
        for start, end in item["ranges"]:
            assert user[start:end] == item["source"]
    return user, payload


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")


def test_occurrence_scopes_preserve_ordinary_and_mixed_usage(tmp_path):
    translator = _translator(tmp_path)
    source = "舞曲的变奏引起共鸣。切换角色触发变奏入场。角色打出变奏伤害。"
    locked = _lock(translator, source)

    assert ("变奏", "变奏技能", "Intro Skill", "core_term") in locked.references
    first = source.index("变奏", source.index("。") + 1)
    second = source.rindex("变奏")
    assert locked.reference_spans == (
        ("变奏", first, first + 2, "Intro Skill"),
        ("变奏", second, second + 2, "Intro Skill"),
    )
    assert _lock(translator, "音乐家使用变奏曲并入场。").reference_spans == ()
    assert _lock(translator, "她用变奏来写音乐。").reference_spans == ()
    assert _lock(translator, "乐曲使用变奏表现角色的心情。").reference_spans == ()
    assert _lock(translator, "演奏者用变奏入场。").reference_spans == ()
    assert _lock(translator, "演奏者的变奏伤害了听众。").reference_spans == ()


def test_switching_an_official_character_is_local_game_evidence(tmp_path):
    speaker = TermRecord(
        category="speaker", source_file="fixture", source_id="speaker",
        text_key="speaker", zh="测试角色", en="Test Character",
    )
    translator = _translator(tmp_path, extra=(speaker,))
    locked = _lock(translator, "切测试角色变奏入场。")
    assert ("变奏", "变奏技能", "Intro Skill", "core_term") in locked.references
    assert len(locked.reference_spans) == 1


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("技能", "Resonance Skill"), ("解放", "Resonance Liberation"), ("回路", "Forte Circuit")],
)
def test_shared_prefix_requires_a_unique_local_sense(tmp_path, marker, expected):
    translator = _translator(tmp_path)
    locked = _lock(translator, f"释放{marker}后触发共鸣伤害。")
    assert [(item[0], item[2]) for item in locked.references if item[0] == "共鸣"] == [
        ("共鸣", expected)
    ]
    assert len(locked.reference_spans) == 1


def test_ambiguous_prefix_is_omitted_without_unique_evidence(tmp_path):
    translator = _translator(tmp_path)
    assert _lock(translator, "切换角色触发共鸣伤害。").reference_spans == ()
    assert _lock(translator, "释放技能和解放后触发共鸣伤害。").reference_spans == ()
    partial = _translator(tmp_path / "partial", records=(SKILLS[0],))
    assert _lock(partial, "切换角色触发共鸣伤害。").reference_spans == ()


def test_hints_require_the_official_row_and_never_enter_the_hard_lock_path(tmp_path):
    fake = TermRecord(
        category="core_term", source_file="fixture", source_id="5",
        text_key="SkillType_5_TypeName", zh="变奏技能", en="Intro Skill",
    )
    translator = _translator(tmp_path, records=(), extra=(fake,))
    assert _lock(translator, "角色触发变奏入场。").reference_spans == ()
    translator = _translator(tmp_path / "official")
    assert translator.lock_terms("角色触发变奏入场。").references == ()
    assert translator.lock_terms("角色触发变奏入场。").locked_text == "角色触发变奏入场。"
    assert _lock(translator, "角色触发变奏技能。").reference_spans == ()


@pytest.mark.parametrize("mode", ["sync", "async_context", "protected", "async_protected"])
def test_actual_request_contains_exact_ranges_after_placeholders(tmp_path, monkeypatch, mode):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]}
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
        category="", source_file="fixture", source_id="x", text_key="x",
        zh="固定字词", en="Fixed Term",
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
    user, payload = _ranges(bodies[0])
    assert "__WUWA_TERM_" in user
    if mode in {"protected", "async_protected"}:
        assert "__WUWA_HTML_" in user
    start = user.index("变奏")
    assert payload == [
        {"source": "变奏", "target": "Intro Skill", "ranges": [[start, start + 2]]}
    ]
    assert "变奏 = Intro Skill (core_term)" in bodies[0]["messages"][0]["content"]


def test_actual_request_binds_each_ambiguous_prefix_to_its_own_target(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]}
        )

    translator = _translator(tmp_path, transport=httpx.MockTransport(handler))
    text = "释放技能后触发共鸣伤害。释放解放后触发共鸣伤害。"

    async def run():
        await translator.translate_async(text)
        await translator._close_llm_client()

    asyncio.run(run())
    assert len(bodies) == 1
    user, payload = _ranges(bodies[0])
    first = user.index("共鸣")
    second = user.rindex("共鸣")
    assert payload == [
        {"source": "共鸣", "target": "Resonance Skill", "ranges": [[first, first + 2]]},
        {"source": "共鸣", "target": "Resonance Liberation", "ranges": [[second, second + 2]]},
    ]
    assert "other alternatives for the same source do not apply at that range" in (
        bodies[0]["messages"][0]["content"]
    )


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_legacy_callers_never_receive_a_target_scoped_hint_without_ranges(
    tmp_path, monkeypatch, mode
):
    seen = []

    def fake_sync(locked_text, locks, references=()):
        seen.extend(references)
        return locked_text

    async def fake_async(locked_text, locks, references=()):
        seen.extend(references)
        return locked_text

    translator = _translator(tmp_path)
    if mode == "sync":
        monkeypatch.setattr("wuwaterm.sentence._call_llm", fake_sync)
        assert any(item[0] == "变奏" for item in _lock(translator, "切换角色触发变奏入场。").references)
        translator.translate("切换角色触发变奏入场。")
    else:
        monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_async)
        assert any(item[0] == "变奏" for item in _lock(translator, "切换角色触发变奏入场。").references)
        asyncio.run(translator.translate_async("切换角色触发变奏入场。"))
    assert not any(reference[0] == "变奏" for reference in seen)


def test_read_only_previous_context_is_not_skill_admission_evidence(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": body["messages"][1]["content"]}}]}
        )

    translator = _translator(tmp_path, transport=httpx.MockTransport(handler))
    before = type("Before", (), {"source": "角色触发", "target": "The character triggers"})()
    context = type("Context", (), {"before": before, "precedents": ()})()

    async def run():
        await translator.translate_async("她的变奏让听众共鸣。", context=context)
        await translator._close_llm_client()

    asyncio.run(run())
    assert len(bodies) == 1
    assert _REFERENCE_RANGE_HEADER not in bodies[0]["messages"][0]["content"]
    assert "变奏 = Intro Skill" not in bodies[0]["messages"][0]["content"]


def test_sync_wrapper_omits_scoped_hint_for_legacy_async_callee(monkeypatch):
    seen = []

    async def fake_async(
        locked_text, locks, html_mode=False, to_chinese=False,
        timeout_seconds=30.0, references=(),
    ):
        seen.extend(references)
        return locked_text

    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_async)
    _call_llm(
        "变奏", (), references=(("变奏", "变奏技能", "Intro Skill", "core_term"),),
        reference_spans=(("变奏", 0, 2, "Intro Skill"),),
    )
    assert seen == []


@pytest.mark.parametrize(
    "span",
    [
        ("变奏", 0, 1),
        ("变奏", -1, 1),
        ("变奏", 0, 3),
        ("别的", 0, 2),
        ("变奏", True, 2),
        ("变奏", 0, 2, "Outro Skill"),
        ("变奏", 0, 2, 4),
        ("变奏", 0, 2, "Intro Skill", "extra"),
    ],
)
def test_bad_range_fails_before_the_http_request(span):
    def handler(_request):
        pytest.fail("a malformed range must not reach the model")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="reference range"):
                await _call_llm_async_with_client(
                    client, "变奏", (), references=(("变奏", "变奏技能", "Intro Skill", "core_term"),),
                    reference_spans=(span,),
                )

    asyncio.run(run())


def test_protected_placeholder_width_is_per_placeholder():
    count = 10_001
    protected = ProtectedText(("",) * (count + 1), (ProtectedStructure("<b>"),) * count)
    assert protected.placeholder_lengths[-1] == protected.placeholder_lengths[0] + 1
