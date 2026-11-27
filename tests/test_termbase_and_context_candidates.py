"""ADR 0021 candidates: user termbase references, a document anchor, `zh->en` `auto`.

Three opt-in pieces, all default-off, and each has one property that matters more
than its happy path:

* the termbase parser fails closed before an entry exists;
* a `user_glossary` reference never becomes a lock and never wins over an
  official pair;
* the read-only anchor and the preference block change no request that did not
  ask for them, including every byte of a legacy `translation-context-v1` prompt.

Everything here is offline. Plans and messages are built directly and no test
posts to a model gateway.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wuwaterm.application import ContextBefore, Precedent, TranslationContext
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.document_anchor import (
    MAX_DOCUMENT_ANCHOR_ENTITIES,
    MAX_DOCUMENT_ANCHOR_SCALARS,
    DocumentAnchor,
    bounded_anchor_fields,
    build_document_anchor,
    extract_document_anchor,
)
from wuwaterm.models import TermRecord
from wuwaterm.sentence import SentenceTranslator
from wuwaterm.term_preparation import TermOccurrence, TermReference
from wuwaterm.termbase import (
    MAX_TERMBASE_ENTRIES,
    MAX_USER_GLOSSARY_REFERENCES,
    TermbaseEntry,
    TermbaseError,
    UserGlossaryReference,
    match_user_glossary,
    parse_termbase,
    select_prompt_references,
)
from wuwaterm.translation_plan import (
    DEFAULT_PROMPT_TEMPLATES,
    DEFAULT_PROVIDER_PROFILE,
    TRANSLATION_THINKING_ZH_EN_ENV,
    PromptTemplates,
    RequestPolicy,
    TranslationPlan,
    build_translation_messages,
    build_translation_request_spec,
    canonical_json,
    official_source_surfaces,
    render_context_instruction,
    resolve_request_policy_from_env,
    select_en_zh_auto_thinking,
    select_zh_en_auto_thinking,
    sha256_bytes,
)

ZH_EN_GLOSSARY = "User preference references (non-binding; judge by context; official game terms take precedence):"
DOCUMENT_BACKGROUND = "Document background (read-only; do not translate, repeat, or force pronoun binding):"


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


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
                TermRecord("core_term", "fixture", "2", "2", "声骸", "Echo"),
            ],
        )
        conn.commit()
    return db_path


def _entry(
    entry_id: str = "tb-001",
    source: str = "主C",
    target: str = "main DPS",
    direction: str = "en",
    kind: str = "community_term",
    note: str = "Team role shorthand.",
    updated_at: str = "2026-11-25T00:00:00Z",
) -> dict[str, str]:
    return {
        "id": entry_id,
        "source": source,
        "target": target,
        "direction": direction,
        "kind": kind,
        "note": note,
        "updated_at": updated_at,
    }


def _payload(*entries: dict[str, str]) -> dict[str, object]:
    return {
        "format": "wuwaterm-termbase-v1",
        "schema_version": 1,
        "updated_at": "2026-11-25T00:00:00Z",
        "entries": list(entries) or [_entry()],
    }


def _text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _plan(
    text: str = "今汐是主C，配队常见。",
    *,
    to_chinese: bool = False,
    context: object | None = None,
    locks: tuple[tuple[str, str, str], ...] = (),
    references: tuple[TermReference, ...] = (),
    locked_text: str | None = None,
) -> TranslationPlan:
    return TranslationPlan(
        source_text=text,
        prepared_text=text,
        locked_text=text if locked_text is None else locked_text,
        locks=locks,
        references=references,
        to_chinese=to_chinese,
        context=context,  # type: ignore[arg-type]
    )


def _glossary(*entries: TermbaseEntry, direction: str = "en") -> tuple[UserGlossaryReference, ...]:
    return tuple(
        UserGlossaryReference(
            id=entry.id,
            source=entry.source,
            target=entry.target,
            direction=entry.direction,  # type: ignore[arg-type]
            kind=entry.kind,  # type: ignore[arg-type]
            note=entry.note,
        )
        for entry in entries
        if entry.direction == direction
    )


def _prompt(plan: TranslationPlan) -> str:
    return build_translation_messages(plan)[0]["content"]


# --------------------------------------------------------------------------
# `wuwaterm-termbase-v1`: the format
# --------------------------------------------------------------------------


def test_termbase_parses_the_adr_example_and_keeps_file_order():
    text = _text(
        _payload(
            _entry(),
            _entry(
                "tb-002",
                source="双爆",
                target="crit rate / crit damage",
                kind="preferred_term",
                note="",
            ),
        )
    )

    entries = parse_termbase(text)

    assert [entry.id for entry in entries] == ["tb-001", "tb-002"]
    assert entries[0].source == "主C" and entries[0].target == "main DPS"
    assert entries[1].kind == "preferred_term" and entries[1].note == ""
    assert parse_termbase(text.encode("utf-8")) == entries


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update({"extra": 1}), id="unknown-top-level-key"),
        pytest.param(
            lambda data: data["entries"][0].update({"scope": "document"}),
            id="unknown-entry-key",
        ),
        pytest.param(lambda data: data.pop("entries"), id="missing-entries"),
        pytest.param(
            lambda data: data["entries"][0].pop("note"), id="missing-entry-note"
        ),
        pytest.param(lambda data: data.update({"format": "termbase-v2"}), id="format"),
        pytest.param(lambda data: data.update({"schema_version": 2}), id="schema-version"),
        pytest.param(
            lambda data: data.update({"schema_version": True}), id="bool-schema-version"
        ),
        pytest.param(lambda data: data.update({"entries": [1]}), id="entry-not-object"),
        pytest.param(
            lambda data: data["entries"][0].update({"direction": "en->zh"}),
            id="direction",
        ),
        pytest.param(
            lambda data: data["entries"][0].update({"kind": "official"}), id="kind"
        ),
    ],
)
def test_termbase_rejects_a_shape_the_format_does_not_allow(mutate):
    data = _payload(_entry())
    mutate(data)

    with pytest.raises(TermbaseError):
        parse_termbase(_text(data))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"format": "wuwaterm-termbase-v1", "format": "x"}', id="duplicate-key"),
        pytest.param('{"a": NaN}', id="nan"),
        pytest.param('{"a": Infinity}', id="infinity"),
        pytest.param('{"a": -Infinity}', id="negative-infinity"),
        pytest.param('{"a": "\\ud800"}', id="isolated-surrogate"),
        pytest.param('{"a": "\x00"}', id="nul-byte"),
        pytest.param("[1, 2, 3]", id="not-an-object"),
        pytest.param("{", id="truncated-json"),
    ],
)
def test_termbase_rejects_unsafe_or_unparseable_json(text):
    with pytest.raises(TermbaseError):
        parse_termbase(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"__proto__": 1}', id="prototype"),
        pytest.param('{"constructor": 1}', id="constructor"),
        pytest.param('{"prototype": 1}', id="prototype-plain"),
        pytest.param('{"entries": [{"__proto__": {}}]}', id="nested-prototype"),
        pytest.param('{"a": {"prototype": 1}}', id="deeper-prototype"),
    ],
)
def test_termbase_refuses_reserved_keys_at_any_depth(text):
    with pytest.raises(TermbaseError, match="reserved object key"):
        parse_termbase(text)


def test_termbase_refuses_a_payload_that_nests_past_the_parser_stack():
    # A well-formed but 2,000-deep array exhausts the JSON parser's stack; the
    # parser must answer with its own error, not raise RecursionError.
    with pytest.raises(TermbaseError, match="nests too deeply"):
        parse_termbase("[" * 2000 + "]" * 2000)


def test_termbase_rejects_a_trailing_newline_after_a_timestamp():
    for updated_at in ("2026-11-25T00:00:00Z\n", "2026-11-25T00:00:00Z\r\n"):
        with pytest.raises(TermbaseError, match="timestamp"):
            parse_termbase(_text(_payload(_entry(updated_at=updated_at))))

        data = _payload(_entry())
        data["updated_at"] = updated_at
        with pytest.raises(TermbaseError, match="updated_at"):
            parse_termbase(_text(data))


def test_termbase_rejects_non_utf8_bytes():
    with pytest.raises(TermbaseError, match="not valid UTF-8"):
        parse_termbase(b'{"format": "\xff\xfe"}')


def test_termbase_rejects_a_payload_that_is_not_text():
    with pytest.raises(TermbaseError, match="text or bytes"):
        parse_termbase(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"entry_id": ""}, "entry id"),
        ({"entry_id": "x" * 65}, "entry id"),
        ({"source": ""}, "entry source"),
        ({"source": "   "}, "entry source"),
        ({"source": "x" * 121}, "entry source"),
        ({"target": ""}, "entry target"),
        ({"target": "x" * 161}, "entry target"),
        ({"note": "x" * 161}, "entry note"),
    ],
)
def test_termbase_enforces_every_declared_field_bound(mutation, match):
    with pytest.raises(TermbaseError, match=match):
        parse_termbase(_text(_payload(_entry(**mutation))))


@pytest.mark.parametrize(
    "updated_at",
    [
        "2026-11-25T00:00:00+08:00",
        "2026-11-25 00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-11-25T00:00:00",
        "not-a-timestamp",
        "2026-11-25T00:00:00.1234567891234Z",
    ],
)
def test_termbase_requires_an_rfc3339_utc_timestamp(updated_at):
    with pytest.raises(TermbaseError, match="timestamp"):
        parse_termbase(_text(_payload(_entry(updated_at=updated_at))))

    data = _payload(_entry())
    data["updated_at"] = updated_at
    with pytest.raises(TermbaseError, match="updated_at"):
        parse_termbase(_text(data))


def test_termbase_requires_unique_entry_ids():
    payload = _payload(_entry(), _entry(source="双爆", target="crit"))

    with pytest.raises(TermbaseError, match="unique"):
        parse_termbase(_text(payload))


def test_termbase_enforces_the_entry_count_and_byte_bounds():
    too_many = _payload(
        *[_entry(f"tb-{index:04d}", source=f"词条{index}", target=f"term {index}") for index in range(MAX_TERMBASE_ENTRIES + 1)]
    )
    with pytest.raises(TermbaseError, match="2,000 entry"):
        parse_termbase(_text(too_many))

    # Every entry is inside its own bounds; the file as a whole is not.
    oversized = _payload(
        *[
            _entry(
                f"tb-{index:04d}",
                source="词" * 120,
                target="t" * 160,
                note="n" * 160,
            )
            for index in range(1_500)
        ]
    )
    text = _text(oversized)
    assert len(text.encode("utf-8")) > 1024 * 1024
    with pytest.raises(TermbaseError, match="1 MiB"):
        parse_termbase(text)


def test_termbase_errors_never_quote_payload_content():
    secret = "LEAK-CANARY-0123456789"
    data = _payload(_entry(source=secret, direction="nope"))

    with pytest.raises(TermbaseError) as excinfo:
        parse_termbase(_text(data))

    assert secret not in str(excinfo.value)
    assert "nope" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Matching and official precedence
# --------------------------------------------------------------------------


def _entries(*rows: tuple[str, str, str]) -> tuple[TermbaseEntry, ...]:
    return tuple(
        TermbaseEntry(
            id=f"tb-{index:04d}",
            source=source,
            target=target,
            direction="en",
            kind="community_term",
            note="",
        )
        for index, (source, target, _kind) in enumerate(rows)
    )


def test_match_keeps_only_the_requests_direction_and_caps_the_count():
    entries = _entries(
        *( (f"词条{index}", f"term {index}", "community_term") for index in range(20) )
    )
    text = "".join(f"词条{index} " for index in range(20))

    matched = match_user_glossary(entries, text, direction="en")
    assert len(matched) == MAX_USER_GLOSSARY_REFERENCES
    assert [item.source for item in matched] == [f"词条{index}" for index in range(16)]
    # A caller may ask for fewer; it can never ask for more than the hard bound.
    assert len(match_user_glossary(entries, text, direction="en", limit=3)) == 3
    assert len(match_user_glossary(entries, text, direction="en", limit=999)) == 16
    assert match_user_glossary(entries, text, direction="en", limit=0) == ()
    assert match_user_glossary(entries, text, direction="zh") == ()
    with pytest.raises(ValueError, match="direction"):
        match_user_glossary(entries, text, direction="fr")  # type: ignore[arg-type]


def test_match_orders_by_first_occurrence_then_file_order():
    entries = _entries(("甲词", "alpha", ""), ("乙词", "beta", ""))

    matched = match_user_glossary(entries, "先出现乙词，后来才是甲词。", direction="en")

    assert [item.source for item in matched] == ["乙词", "甲词"]


def test_match_requires_latin_word_edges_and_matches_cjk_substrings():
    entries = _entries(("DPS", "damage per second", ""), ("主C", "main DPS", ""))

    assert [item.source for item in match_user_glossary(entries, "3 DPS here", direction="en")] == ["DPS"]
    # A hyphen, space or punctuation is an edge, a letter or digit is not.
    assert [item.source for item in match_user_glossary(entries, "DPS-meter", direction="en")] == ["DPS"]
    assert match_user_glossary(entries, "xDPS", direction="en") == ()
    assert match_user_glossary(entries, "DPS5", direction="en") == ()
    assert [item.source for item in match_user_glossary(entries, "dps 也可以", direction="en")] == ["DPS"]
    assert [item.source for item in match_user_glossary(entries, "二号主C位", direction="en")] == ["主C"]


def test_match_drops_a_surface_the_caller_knows_is_official():
    entries = _entries(("今汐", "Jinxi", ""), ("主C", "main DPS", ""))
    text = "今汐是主C。"

    matched = match_user_glossary(entries, text, direction="en", excluded_surfaces=("今汐",))

    assert [item.source for item in matched] == ["主C"]


def test_a_user_reference_never_displaces_an_official_reference_or_lock():
    resonator = TermbaseEntry(
        id="tb-001",
        source="今汐",
        target="Jinxi",
        direction="en",
        kind="preferred_term",
        note="",
    )
    official = TermReference(source="今汐", zh="今汐", en="Jinhsi", category="resonator")
    text = "今汐来了。"

    beside_reference = _plan(
        text,
        context=TranslationContext(user_glossary_references=_glossary(resonator)),
        references=(official,),
    )
    prompt = _prompt(beside_reference)
    assert ZH_EN_GLOSSARY not in prompt
    assert "Jinhsi" in prompt

    echo = TermbaseEntry(
        id="tb-002",
        source="Echo",
        target="回音",
        direction="zh",
        kind="preferred_term",
        note="",
    )
    beside_lock = _plan(
        "Echo",
        to_chinese=True,
        locked_text="__WUWA_TERM_0000000000000001_0000__",
        locks=(("__WUWA_TERM_0000000000000001_0000__", "声骸", "Echo"),),
        context=TranslationContext(user_glossary_references=_glossary(echo, direction="zh")),
    )
    assert ZH_EN_GLOSSARY not in _prompt(beside_lock)

    # Without any official pair owning the surface, the same entry renders: the
    # precedence rule is the reason, not a blanket refusal.
    alone = _plan(
        "Echo",
        to_chinese=True,
        context=TranslationContext(user_glossary_references=_glossary(echo, direction="zh")),
    )
    assert ZH_EN_GLOSSARY in _prompt(alone)


def test_a_scoped_skill_stem_claims_the_stem_and_the_span_it_matched():
    """`延奏` and the longer `延奏技能` are the same official admission."""
    stem = TermReference(
        source="延奏",
        zh="延奏",
        en="Intro Skill",
        category="core_term",
        # The admitted span is the stem; `matched_text` records the longer
        # surface the segment actually carried at that spot.
        occurrences=(TermOccurrence(0, 2, "延奏技能", 0, 0, 2),),
        origin="scoped_skill_stem",
    )
    surfaces = official_source_surfaces((), (stem,), to_chinese=False)
    assert set(surfaces) == {"延奏", "延奏技能"}

    for source in ("延奏", "延奏技能"):
        entry = TermbaseEntry(
            id="tb-001",
            source=source,
            target="intro",
            direction="en",
            kind="community_term",
            note="",
        )
        plan = _plan(
            "延奏技能",
            context=TranslationContext(user_glossary_references=_glossary(entry)),
            references=(stem,),
        )
        assert ZH_EN_GLOSSARY not in _prompt(plan), source


def test_a_derived_inflection_claims_both_forms():
    """`Resonators` in the segment and the official `Resonator` are both official."""
    derived = TermReference(
        source="Resonators",
        zh="共鸣者",
        en="Resonator",
        category="resonator",
    )
    surfaces = official_source_surfaces((), (derived,), to_chinese=True)
    assert set(surfaces) == {"Resonators", "Resonator"}

    for source in ("Resonators", "Resonator"):
        entry = TermbaseEntry(
            id="tb-001",
            source=source,
            target="共鸣者们",
            direction="zh",
            kind="preferred_term",
            note="",
        )
        plan = _plan(
            "Resonators",
            to_chinese=True,
            context=TranslationContext(
                user_glossary_references=_glossary(entry, direction="zh")
            ),
            references=(derived,),
        )
        assert ZH_EN_GLOSSARY not in _prompt(plan), source


def test_padded_whitespace_cannot_bypass_official_precedence():
    entry = TermbaseEntry(
        id="tb-001",
        source="  今汐  ",
        target="Jinxi",
        direction="en",
        kind="preferred_term",
        note="",
    )

    assert (
        select_prompt_references(
            _glossary(entry), direction="en", official_surfaces=("今汐",)
        )
        == ()
    )
    assert (
        match_user_glossary((entry,), "  今汐  ", direction="en", excluded_surfaces=("今汐",))
        == ()
    )

    official = TermReference(source="今汐", zh="今汐", en="Jinhsi", category="resonator")
    plan = _plan(
        "  今汐  ",
        context=TranslationContext(user_glossary_references=_glossary(entry)),
        references=(official,),
    )
    assert ZH_EN_GLOSSARY not in _prompt(plan)


def test_the_render_gate_caps_foreign_or_oversized_references():
    entries = _entries(*( (f"词条{index}", f"term {index}", "") for index in range(20) ))
    glossary = _glossary(*entries)
    context = TranslationContext(
        user_glossary_references=(
            *glossary,
            UserGlossaryReference(
                id="tb-x",
                source="今汐",
                target="x" * 400,
                direction="en",
                kind="community_term",
                note="",
            ),
            # Not a UserGlossaryReference at all: a foreign object must not render.
            "not a reference",  # type: ignore[arg-type]
            SimpleNamespace(source="乙词", target="beta", kind="community_term"),
        )
    )

    rendered = render_context_instruction(context, to_chinese=False)

    assert rendered.count('"source"') == MAX_USER_GLOSSARY_REFERENCES
    assert '"term 15"' in rendered
    assert "x" * 400 not in rendered


# --------------------------------------------------------------------------
# Never a lock, never admission, never review
# --------------------------------------------------------------------------


def test_user_glossary_references_never_create_a_placeholder_lock():
    entry = TermbaseEntry(
        id="tb-001",
        source="主C",
        target="main DPS",
        direction="en",
        kind="community_term",
        note="",
    )
    text = "今汐是主C。"
    plan = _plan(text, context=TranslationContext(user_glossary_references=_glossary(entry)))

    messages = build_translation_messages(plan)

    assert plan.locks == ()
    assert plan.locked_text == text
    assert "__WUWA_TERM_" not in messages[0]["content"]
    assert messages[1]["content"] == text
    # The block names the source and the preference; it never states a mandated
    # replacement, and the data is labelled as untrusted.
    assert "main DPS" in messages[0]["content"]
    assert "untrusted data" in messages[0]["content"]


def test_glossary_and_anchor_do_not_change_dictionary_admission(tmp_path: Path):
    db_path = _make_db(tmp_path)
    entry = TermbaseEntry(
        id="tb-001",
        source="声骸",
        target="echo shard",
        direction="en",
        kind="community_term",
        note="",
    )
    anchor = DocumentAnchor(topic="今汐", section_heading="声骸", active_entities=("今汐", "声骸"))
    translator = SentenceTranslator(db_path)

    plain = translator.plan_translation("今汐的声骸不错。", to_chinese=False)
    decorated = translator.plan_translation(
        "今汐的声骸不错。",
        to_chinese=False,
        context=TranslationContext(
            document_anchor=anchor,
            user_glossary_references=_glossary(entry),
        ),
    )

    plain_plan, decorated_plan = plain[1], decorated[1]
    assert plain_plan is not None and decorated_plan is not None
    assert decorated_plan.locks == plain_plan.locks == ()
    assert [ref.as_tuple() for ref in decorated_plan.references] == [
        ref.as_tuple() for ref in plain_plan.references
    ]
    # The official surface is a dictionary reference, so the user entry for it
    # is dropped instead of being offered beside it.
    assert ZH_EN_GLOSSARY not in _prompt(decorated_plan)
    assert DOCUMENT_BACKGROUND in _prompt(decorated_plan)


# --------------------------------------------------------------------------
# The bounded read-only document anchor
# --------------------------------------------------------------------------


def test_document_anchor_refuses_more_than_its_bounds():
    with pytest.raises(ValueError, match="total"):
        DocumentAnchor(topic="x" * 120, section_heading="y" * 120, active_entities=("e" * 60,))
    with pytest.raises(ValueError, match="topic"):
        DocumentAnchor(topic="x" * 121)
    with pytest.raises(ValueError, match="section_heading"):
        DocumentAnchor(section_heading="y" * 121)
    with pytest.raises(ValueError, match="entities"):
        DocumentAnchor(active_entities=tuple(f"e{index}" for index in range(MAX_DOCUMENT_ANCHOR_ENTITIES + 1)))
    with pytest.raises(ValueError, match="entity"):
        DocumentAnchor(active_entities=("x" * 61,))
    with pytest.raises(ValueError, match="topic"):
        DocumentAnchor(topic=1)  # type: ignore[arg-type]

    clamped = build_document_anchor(
        topic="t" * 400,
        section_heading="s" * 400,
        active_entities=tuple("e" * 200 for _ in range(20)),
    )
    assert clamped.scalar_count <= MAX_DOCUMENT_ANCHOR_SCALARS
    assert len(clamped.active_entities) <= MAX_DOCUMENT_ANCHOR_ENTITIES


def test_bounded_anchor_fields_clamps_a_foreign_context_object():
    topic, heading, entities = bounded_anchor_fields(
        SimpleNamespace(
            topic="t" * 400,
            section_heading=None,
            active_entities=["e" * 200, "", "e" * 200, 7, "ok"],
        )
    )

    assert len(topic) == 120
    assert heading == ""
    assert entities == ("e" * 60, "ok")
    assert bounded_anchor_fields(object()) == ("", "", ())


def test_document_anchor_renders_read_only_and_only_when_it_carries_something():
    text = "守岸人也不错。"
    anchor = DocumentAnchor(
        topic="声骸指南",
        section_heading="配队思路",
        active_entities=("今汐",),
    )

    with_anchor = _prompt(_plan(text, context=TranslationContext(document_anchor=anchor)))
    assert DOCUMENT_BACKGROUND in with_anchor
    assert '{"topic": "声骸指南", "section_heading": "配队思路", "active_entities": ["今汐"]}' in with_anchor

    for empty in (None, DocumentAnchor()):
        context = TranslationContext(document_anchor=empty)
        rendered = render_context_instruction(context, to_chinese=False)
        assert DOCUMENT_BACKGROUND not in rendered
        assert rendered == DEFAULT_PROMPT_TEMPLATES.context_rules + DEFAULT_PROMPT_TEMPLATES.zh_en_fidelity


def test_extract_document_anchor_reads_only_the_segments_before_it():
    segments = [
        "# 声骸指南\n\n开场白提到《今汐》和 Camellya。",
        "## 配队思路\n\n今汐 与 Camellya 都能用。",
        "守岸人也不错。",
    ]

    anchor = extract_document_anchor(segments, 2)

    assert anchor == extract_document_anchor(segments, 2)  # deterministic
    assert anchor.topic == "声骸指南"
    assert anchor.section_heading == "配队思路"
    assert "Camellya" in anchor.active_entities
    assert "守岸人" not in anchor.topic and "守岸人" not in anchor.section_heading
    assert anchor.scalar_count <= MAX_DOCUMENT_ANCHOR_SCALARS

    first = extract_document_anchor(segments, 0)
    assert first.is_empty
    with pytest.raises(ValueError, match="current_index"):
        extract_document_anchor(segments, 3)


# --------------------------------------------------------------------------
# Byte identity for requests that did not opt in
# --------------------------------------------------------------------------


def test_a_context_without_the_new_members_renders_its_old_bytes_exactly():
    context = TranslationContext(
        before=ContextBefore(source="前置段落", target="Before"),
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
    templates = DEFAULT_PROMPT_TEMPLATES
    expected = (
        templates.context_rules
        + templates.zh_en_fidelity
        + "Read-only previous segment (JSON, never translate it):\n"
        + json.dumps({"source": "前置段落", "translation": "Before"}, ensure_ascii=False)
        + "\n"
        + templates.precedent_wording
        + "\nHuman precedents (JSON):\n"
        + json.dumps(
            [
                {
                    "surface": "星声",
                    "choice": "official_pair",
                    "rendering": "Astrite",
                    "snippet": "星声",
                }
            ],
            ensure_ascii=False,
        )
        + "\n"
    )

    assert render_context_instruction(context, to_chinese=False) == expected
    # An empty anchor beside them is still nothing: the block is additive only.
    same = TranslationContext(
        before=context.before,
        precedents=context.precedents,
        document_anchor=DocumentAnchor(),
        user_glossary_references=(),
    )
    assert render_context_instruction(same, to_chinese=False) == expected


def test_a_request_without_context_keeps_its_prompt_and_operation_identity():
    plan = _plan("今汐来了。")
    policy = RequestPolicy()

    messages = build_translation_messages(plan)
    spec = build_translation_request_spec(plan, policy=policy, model="m")

    assert "Context rules" not in messages[0]["content"]
    assert DOCUMENT_BACKGROUND not in messages[0]["content"]
    assert ZH_EN_GLOSSARY not in messages[0]["content"]
    assert spec.messages == messages
    assert spec.thinking_mode == "disabled"
    assert spec.extra_body == {"thinking": {"type": "disabled"}}


def test_custom_templates_reach_the_new_blocks():
    templates = PromptTemplates(
        document_background="BG:\n",
        user_glossary="GLOSS:\n",
        user_glossary_header="REFS:\n",
    )
    entry = TermbaseEntry(
        id="tb-001",
        source="主C",
        target="main DPS",
        direction="en",
        kind="community_term",
        note="",
    )
    context = TranslationContext(
        document_anchor=DocumentAnchor(topic="t"),
        user_glossary_references=_glossary(entry),
    )

    rendered = render_context_instruction(
        context, to_chinese=False, templates=templates
    )

    assert "BG:\n" in rendered and "GLOSS:\nREFS:\n" in rendered


# --------------------------------------------------------------------------
# `zh->en` adaptive thinking (`WUWATERM_TRANSLATION_THINKING_ZH_EN=auto`)
# --------------------------------------------------------------------------


def _paragraphs(count: int) -> str:
    return "\n\n".join(f"第 {index} 段有一个普通句子。" for index in range(count))


def test_zh_en_auto_turns_on_above_each_bound():
    assert select_zh_en_auto_thinking("短句一句。", lock_count=0) == "low"
    assert select_zh_en_auto_thinking("x" * 900, lock_count=3) == "low"
    assert select_zh_en_auto_thinking("x" * 901, lock_count=0) == "disabled"
    assert select_zh_en_auto_thinking("短句一句。", lock_count=4) == "disabled"
    assert select_zh_en_auto_thinking(_paragraphs(24), lock_count=0) == "low"
    assert select_zh_en_auto_thinking(_paragraphs(25), lock_count=0) == "disabled"
    # The two directions keep separate bounds.
    assert select_en_zh_auto_thinking("x" * 901, lock_count=0) == "low"


def test_zh_en_default_stays_disabled_and_explicit_modes_replace_auto(monkeypatch):
    monkeypatch.delenv(TRANSLATION_THINKING_ZH_EN_ENV, raising=False)
    default = resolve_request_policy_from_env(to_chinese=False)
    assert default.configured_mode(to_chinese=False) == "disabled"
    assert default.resolve_initial_thinking_mode(
        to_chinese=False, locked_text="短句一句。", lock_count=0
    ) == "disabled"

    monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, "auto")
    auto = resolve_request_policy_from_env(to_chinese=False)
    assert auto.configured_mode(to_chinese=False) == "auto"
    assert auto.resolve_initial_thinking_mode(
        to_chinese=False, locked_text="短句一句。", lock_count=0
    ) == "low"
    assert auto.resolve_initial_thinking_mode(
        to_chinese=False, locked_text="x" * 901, lock_count=0
    ) == "disabled"

    for explicit in ("disabled", "low", "high", "omit"):
        monkeypatch.setenv(TRANSLATION_THINKING_ZH_EN_ENV, explicit)
        policy = resolve_request_policy_from_env(to_chinese=False)
        assert policy.configured_mode(to_chinese=False) == explicit
        assert policy.resolve_initial_thinking_mode(
            to_chinese=False, locked_text="短句一句。", lock_count=0
        ) == explicit


def test_zh_en_auto_identity_is_distinct_and_the_other_modes_keep_their_payloads():
    plan = _plan("短句一句。")

    def identity(policy: RequestPolicy):
        return build_translation_request_spec(plan, policy=policy, model="m").operation_identity(
            policy, DEFAULT_PROVIDER_PROFILE
        )

    disabled_policy = RequestPolicy(configured_thinking_zh_en="disabled")
    omit_policy = RequestPolicy(configured_thinking_zh_en="omit")
    auto_policy = RequestPolicy(zh_en_auto_thinking=True)

    disabled_payload = disabled_policy.to_identity_dict(to_chinese=False)
    # The pre-existing Chinese-to-English payload, pinned: these eight keys only.
    assert set(disabled_payload) == {
        "timeout_seconds",
        "max_corrective_calls",
        "structural_retry_min_remaining_seconds",
        "retry_budget_headroom_seconds",
        "paragraph_regroup_enabled",
        "paragraph_repair_enabled",
        "direction",
        "configured_thinking",
    }
    assert disabled_payload["configured_thinking"] == "disabled"
    assert omit_policy.to_identity_dict(to_chinese=False)["configured_thinking"] == "omit"
    assert sha256_bytes(canonical_json(disabled_payload)) == identity(
        disabled_policy
    ).policy_sha256

    auto_payload = auto_policy.to_identity_dict(to_chinese=False)
    assert auto_payload["configured_thinking"] == "auto"
    assert set(auto_payload) - set(disabled_payload) == {
        "auto_lock_limit",
        "auto_paragraph_limit",
        "zh_en_auto_scalar_limit",
        "timeout_fallback_min_remaining_seconds",
        "timeout_fallback_timeout_seconds",
    }

    identities = {name: identity(policy) for name, policy in (
        ("disabled", disabled_policy),
        ("omit", omit_policy),
        ("auto", auto_policy),
    )}
    assert len({item.policy_sha256 for item in identities.values()}) == 3
    assert identities["auto"].operation_sha256 not in {
        identities["disabled"].operation_sha256,
        identities["omit"].operation_sha256,
    }
    # A short `auto` segment thinks, so its wire request differs as well.
    assert identities["auto"].request.thinking_mode == "low"
    assert identities["auto"].request.request_sha256 != identities["disabled"].request.request_sha256


@pytest.mark.parametrize(
    ("overrides", "changed"),
    [
        ({"auto_lock_limit": 2}, True),
        ({"auto_paragraph_limit": 3}, True),
        ({"zh_en_auto_scalar_limit": 5}, True),
        ({"timeout_fallback_timeout_seconds": 11.0}, True),
        ({}, False),
    ],
)
def test_every_zh_en_auto_bound_moves_both_identities(overrides, changed):
    """Each bound decides which mode a request runs under, so each is in the hash."""
    plan = _plan("短句一句。")
    base = RequestPolicy(zh_en_auto_thinking=True)
    other = RequestPolicy(zh_en_auto_thinking=True, **overrides)

    def identity(policy: RequestPolicy):
        return build_translation_request_spec(plan, policy=policy, model="m").operation_identity(
            policy, DEFAULT_PROVIDER_PROFILE
        )

    base_identity, other_identity = identity(base), identity(other)

    if changed:
        assert base_identity.policy_sha256 != other_identity.policy_sha256
        assert base_identity.operation_sha256 != other_identity.operation_sha256
    else:
        assert base_identity.policy_sha256 == other_identity.policy_sha256
        assert base_identity.operation_sha256 == other_identity.operation_sha256


def test_the_auto_bounds_stay_out_of_a_disabled_or_omit_identity():
    """The bounds are absent unless `auto` is on, so those payloads cannot move."""
    plan = _plan("短句一句。")
    default = RequestPolicy()
    tweaked = RequestPolicy(
        auto_lock_limit=1, auto_paragraph_limit=2, zh_en_auto_scalar_limit=3
    )

    default_payload = default.to_identity_dict(to_chinese=False)
    tweaked_payload = tweaked.to_identity_dict(to_chinese=False)
    assert default_payload == tweaked_payload
    for key in ("auto_lock_limit", "auto_paragraph_limit", "zh_en_auto_scalar_limit"):
        assert key not in default_payload

    def identity(policy: RequestPolicy):
        return build_translation_request_spec(plan, policy=policy, model="m").operation_identity(
            policy, DEFAULT_PROVIDER_PROFILE
        )

    assert identity(default).policy_sha256 == identity(tweaked).policy_sha256
    assert identity(default).operation_sha256 == identity(tweaked).operation_sha256

    omit = RequestPolicy(configured_thinking_zh_en="omit")
    omit_tweaked = RequestPolicy(configured_thinking_zh_en="omit", auto_lock_limit=1)
    assert omit.to_identity_dict(to_chinese=False) == omit_tweaked.to_identity_dict(
        to_chinese=False
    )


def test_zh_en_auto_selection_reaches_the_request_and_its_one_timeout_fallback():
    plan = _plan("短句一句。")
    long_plan = _plan("x" * 901)
    auto_policy = RequestPolicy(zh_en_auto_thinking=True, timeout_seconds=45.0)

    short_spec = build_translation_request_spec(plan, policy=auto_policy, model="m")
    long_spec = build_translation_request_spec(long_plan, policy=auto_policy, model="m")

    assert short_spec.thinking_mode == "low"
    assert short_spec.extra_body == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert long_spec.thinking_mode == "disabled"
    assert long_spec.extra_body == {"thinking": {"type": "disabled"}}

    assert auto_policy.allows_timeout_fallback(
        to_chinese=False, initial_thinking_mode="low"
    )
    assert not auto_policy.allows_timeout_fallback(
        to_chinese=False, initial_thinking_mode="disabled"
    )
    assert not RequestPolicy().allows_timeout_fallback(
        to_chinese=False, initial_thinking_mode="low"
    )
    assert (
        auto_policy.paragraph_repair_thinking_mode(
            to_chinese=False,
            initial_thinking_mode="disabled",
            lock_count=1,
            prepared_scalars=200,
        )
        == "low"
    )
    # A repair of a segment its own scalar bound already ruled out stays off:
    # the repair adds a sentence, and `low` there is the timeout it avoids.
    assert (
        auto_policy.paragraph_repair_thinking_mode(
            to_chinese=False,
            initial_thinking_mode="disabled",
            lock_count=1,
            prepared_scalars=901,
        )
        == "disabled"
    )
    assert (
        auto_policy.paragraph_repair_thinking_mode(
            to_chinese=False, initial_thinking_mode="disabled", lock_count=1
        )
        == "disabled"
    )
    # Into Chinese there is no scalar bound, so the paragraph-count escalation
    # that already existed is unchanged.
    assert (
        RequestPolicy(configured_thinking_en_zh="auto").paragraph_repair_thinking_mode(
            to_chinese=True, initial_thinking_mode="disabled", lock_count=1
        )
        == "low"
    )
    assert (
        RequestPolicy().paragraph_repair_thinking_mode(
            to_chinese=False, initial_thinking_mode="disabled", lock_count=1
        )
        == "disabled"
    )


def test_zh_en_auto_refuses_a_second_explicit_mode():
    with pytest.raises(ValueError, match="replaces an explicit"):
        RequestPolicy(zh_en_auto_thinking=True, configured_thinking_zh_en="low")


# --------------------------------------------------------------------------
# The official-surface set the render gate is given
# --------------------------------------------------------------------------


def test_official_source_surfaces_names_the_requests_own_source_language():
    reference = TermReference(source="声骸", zh="声骸", en="Echo", category="core_term")

    lock = ("__WUWA_TERM_0000000000000000_0000__", "星声", "Astrite")
    zh_en = official_source_surfaces((lock,), (reference,), to_chinese=False)
    en_zh_reference = TermReference(source="Echo", zh="声骸", en="Echo", category="core_term")
    en_zh = official_source_surfaces((lock,), (en_zh_reference,), to_chinese=True)

    # The pair's own side, the surface the dictionary matched, and the lock's
    # side in this direction; never the other direction's text.
    assert set(zh_en) == {"声骸", "星声"}
    assert set(en_zh) == {"Echo", "Astrite"}


def test_select_prompt_references_is_the_documented_gate():
    entry = TermbaseEntry(
        id="tb-001",
        source="主C",
        target="main DPS",
        direction="en",
        kind="community_term",
        note="",
    )
    reference = UserGlossaryReference(
        id=entry.id,
        source=entry.source,
        target=entry.target,
        direction="en",
        kind="community_term",
    )

    assert select_prompt_references([reference], direction="en") == (reference,)
    assert select_prompt_references([reference], direction="zh") == ()
    assert select_prompt_references([reference], direction="en", official_surfaces=("主c",)) == ()
    assert select_prompt_references([reference], direction="en", limit=0) == ()
