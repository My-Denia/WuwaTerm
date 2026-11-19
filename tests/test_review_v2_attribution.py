"""Review-v2 attribution fixes (MY-355 items 1-7).

Each test pins one wrong attribution: which mention an occurrence answers, which
occurrence a longer official form owns, and when the review must say it cannot
tell instead of crediting the first mention a scan reaches.
"""

from __future__ import annotations

import json

import pytest

from wuwaterm.constants import (
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    ARIKATSU_MULTITEXT_SOURCE_FILE,
)
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.review import (
    REASON_AMBIGUOUS_REPEAT,
    REASON_FOUND_OUTSIDE_REGION,
    REASON_ORDINARY_WORD,
    RULE_VERSION_V2,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    _sentence_ranges_v2,
    review_pair,
)
from wuwaterm.semantic_review import term_facts


def _item(zh: str, en: str, table: str = "items") -> TermRecord:
    return TermRecord(
        category="item",
        source_file=f"{table}.json",
        source_id=f"{table}:{zh}:{en}",
        text_key=f"{table}:{zh}:{en}",
        zh=zh,
        en=en,
    )


_RECORDS = (
    TermRecord("resonator", "chars.json", "jinhsi", "jinhsi", "今汐", "Jinhsi"),
    _item("声骸", "Echo"),
    _item("声骸套装", "Echo Set"),
    _item("升级", "Upgrade"),
    _item("核心", "Core"),
    _item("红回声", "Red Echo"),
    _item("回声红", "Echo Red"),
    _item("波纹", "Wave", table="a"),
    _item("波纹", "Waves", table="b"),
    _item("回音", "Echo"),
    _item("回音", "Echo Set"),
    # Derived names: an Area title no area row references, and a tier base.
    TermRecord(
        "location",
        ARIKATSU_MULTITEXT_SOURCE_FILE,
        "Area_41043_Title",
        "Area_41043_Title",
        "教学楼",
        "Academia Hall",
    ),
    TermRecord(
        "item",
        "BinData/item/iteminfo.json",
        "tier-base:42100022",
        "tier-base:42100022",
        "能量袋",
        "Energy Bag",
    ),
)


def _service(tmp_path, records=_RECORDS, zh_exclusions=()):
    path = tmp_path / "attribution.db"
    metadata = [
        ("schema_version", "synthetic-v1"),
        ("source_commit", "fixture-commit"),
    ]
    if zh_exclusions:
        metadata.append(
            (
                FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
                json.dumps(
                    {
                        "version": FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
                        "min_occurrences": FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
                        "max_aligned_ratio": list(
                            FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO
                        ),
                        "surfaces": [
                            [surface, 12, 2] for surface in sorted(zh_exclusions)
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
    with connect(path) as conn:
        initialize(conn)
        conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata)
        insert_records(conn, records)
        conn.commit()
    return TermService(path)


def _pair(service, source: str, target: str, direction: str = "en", **extra):
    return review_pair(
        service, source, target, direction, review_version=RULE_VERSION_V2, **extra
    )


def _by_surface(report):
    return {item.source_span.text: item for item in report.findings}


def _official_pair_for(report, index: int, *, form: str | None = None):
    finding = report.findings[index]
    candidate = next(
        item
        for item in finding.candidates
        if form is None or item.en == form
    )
    return {
        "resolutions": (
            {
                "mention_id": finding.id,
                "choice": "official_pair",
                "candidate_id": candidate.candidate_id,
            },
        ),
        "resolution_context": {
            "source_revision": report.source_revision,
            "rule_version": report.rule_version,
            "dictionary_revision": report.dictionary.revision,
            "matcher_revision": report.matcher_revision,
        },
    }


# --------------------------------------------------------------------------
# Item 3: a variant is never an inflection of a competing official form
# --------------------------------------------------------------------------


def test_selected_form_does_not_inflect_into_a_competing_candidate(tmp_path):
    service = _service(tmp_path)
    # 波纹 carries both "Wave" and "Waves". Unresolved, the exact form "Waves"
    # verifies. Once the reviewer selects "Wave", the competing exact form must
    # not answer as that selection's plural.
    base = _pair(service, "波纹。", "Waves.", "en")
    assert _by_surface(base)["波纹"].verdict == VERDICT_VERIFIED

    resolved = _pair(
        service, "波纹。", "Waves.", "en", **_official_pair_for(base, 0, form="Wave")
    )

    finding = _by_surface(resolved)["波纹"]
    assert finding.verdict != VERDICT_VERIFIED
    assert finding.target_span is None


def test_selected_form_still_accepts_its_own_plural(tmp_path):
    service = _service(tmp_path)
    base = _pair(service, "声骸。", "Echoes.", "en")
    resolved = _pair(service, "声骸。", "Echoes.", "en", **_official_pair_for(base, 0))
    assert _by_surface(resolved)["声骸"].verdict == VERDICT_VERIFIED


# --------------------------------------------------------------------------
# Item 2: a longer official dictionary form owns its span
# --------------------------------------------------------------------------


def test_inner_occurrence_of_a_longer_dictionary_form_is_not_credited(tmp_path):
    service = _service(tmp_path)
    # The source only says 声骸, and the target spells the longer official
    # form "Echo Set". The inner "Echo" is that longer term, not a standalone
    # 声骸 hit, so the mention is left for a reader instead of verified.
    report = _pair(service, "声骸。", "Echo Set.", "en")
    finding = _by_surface(report)["声骸"]
    assert finding.verdict == VERDICT_NEEDS_REVIEW
    assert finding.target_span is None


def test_longer_dictionary_form_matches_a_lowercase_compound(tmp_path):
    service = _service(tmp_path)
    # "Echo set" is the very same compound with its second word in lower case,
    # which changes no term and only the model's capitalisation.
    report = _pair(service, "声骸。", "Echo set.", "en")
    assert _by_surface(report)["声骸"].verdict == VERDICT_NEEDS_REVIEW


def test_dismissed_longer_term_stops_reserving_its_span(tmp_path):
    service = _service(tmp_path)
    source, target = "声骸与声骸套装。", "Echo Set."
    base = _pair(service, source, target)
    assert _by_surface(base)["声骸套装"].verdict == VERDICT_VERIFIED

    longer = next(
        item for item in base.findings if item.source_span.text == "声骸套装"
    )
    resolved = _pair(
        service,
        source,
        target,
        resolutions=({"mention_id": longer.id, "choice": "not_a_term"},),
        resolution_context={
            "source_revision": base.source_revision,
            "rule_version": base.rule_version,
            "dictionary_revision": base.dictionary.revision,
            "matcher_revision": base.matcher_revision,
        },
    )
    # The reviewer said 声骸套装 is prose here, so the "Echo" inside the target
    # "Echo Set" is 声骸's own occurrence again.
    assert _by_surface(resolved)["声骸"].verdict == VERDICT_VERIFIED


# --------------------------------------------------------------------------
# Item 4: a numbering dot is not a sentence end
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        "1. 今汐登场。2. 声骸出现。",
        "1. 今汐登场。\n2. 声骸出现。",
        "I. Jinhsi appears. II. An Echo appears.",
        "IV. The Echo. 12. Jinhsi.",
        # Indented items are still items: the whitespace before the label is
        # layout, not content.
        "\n  1. Echo appears.\n  2. Jinhsi.\n",
        "今汐登场。\n\t12. 声骸出现。",
    ),
)
def test_list_numbering_does_not_split_a_sentence(text):
    assert len(_sentence_ranges_v2(text)) <= 2


def test_a_sentence_ending_after_a_number_keeps_its_break():
    text = "There are 3. Next comes the Echo."
    assert len(_sentence_ranges_v2(text)) == 2


def test_numbered_items_map_one_sentence_to_one_sentence(tmp_path):
    service = _service(tmp_path)
    source = "1. 今汐登场。2. 声骸出现。"
    target = "1. Jinhsi appears. 2. An Echo appears."

    report = _pair(service, source, target)
    assert [item.verdict for item in report.findings] == [
        VERDICT_VERIFIED,
        VERDICT_VERIFIED,
    ]
    # The occurrence stays inside its own numbered item.
    assert _by_surface(report)["今汐"].target_span.text == "Jinhsi"
    assert _by_surface(report)["声骸"].target_span.text == "Echo"


def test_an_occurrence_outside_its_own_numbered_item_is_not_credited(tmp_path):
    service = _service(tmp_path)
    source = "1. 今汐登场。2. 声骸出现。"
    target = "1. An Echo appears. 2. Jinhsi appears."

    report = _pair(service, source, target)
    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_FOUND_OUTSIDE_REGION] * 2


# --------------------------------------------------------------------------
# Item 5: a lowercase spelling of a capitalized term is an ordinary word
# --------------------------------------------------------------------------


def test_lowercase_official_term_is_an_ordinary_word(tmp_path):
    service = _service(tmp_path)
    # 升级 / "Upgrade" is a capitalized official form; the translation uses the
    # ordinary noun/verb, so the mention asserts no terminology constraint.
    report = _pair(service, "升级很快。", "The upgrade is fast.", "en")
    finding = _by_surface(report)["升级"]
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.reason == REASON_ORDINARY_WORD


def test_lowercase_plural_of_an_official_term_is_an_ordinary_word(tmp_path):
    service = _service(tmp_path)
    report = _pair(service, "核心坏了。", "The planet's cores are broken.", "en")
    finding = _by_surface(report)["核心"]
    assert finding.reason == REASON_ORDINARY_WORD


def test_exact_capitalized_form_is_still_verified(tmp_path):
    service = _service(tmp_path)
    report = _pair(service, "升级很快。", "Upgrade is fast.", "en")
    assert _by_surface(report)["升级"].verdict == VERDICT_VERIFIED


def test_lowercase_rule_leaves_names_alone(tmp_path):
    service = _service(tmp_path)
    # A name in lowercase is a casing decision, not an ordinary word.
    report = _pair(service, "今汐登场。", "jinhsi appears.", "en")
    assert _by_surface(report)["今汐"].verdict != VERDICT_NOT_EVALUATED


# --------------------------------------------------------------------------
# Item 6: a derived reference-only surface asserts no constraint
# --------------------------------------------------------------------------


def _derived_records():
    areas = [
        TermRecord(
            "location",
            ARIKATSU_MULTITEXT_SOURCE_FILE,
            "Area_41043_Title",
            "Area_41043_Title",
            "教学楼",
            "Academia Hall",
        )
    ]
    return (*_RECORDS, *areas)


def test_derived_reference_only_surface_is_an_ordinary_word(tmp_path):
    service = _service(tmp_path, records=_derived_records())
    report = _pair(service, "我们学校的教学楼很旧。", "Our school building is old.", "en")

    finding = _by_surface(report)["教学楼"]
    assert finding.verdict == VERDICT_NOT_EVALUATED
    assert finding.reason == REASON_ORDINARY_WORD
    # The semantic review reads the same report: an unassessed mention yields
    # no term fact, so it cannot steer the model toward the official name.
    assert term_facts(report, "我们学校的教学楼很旧。", "en") == []


def test_derived_reference_only_surface_still_evaluates_when_selected(tmp_path):
    service = _service(tmp_path, records=_derived_records())
    source, target = "我们学校的教学楼很旧。", "Our school building is old."
    base = _pair(service, source, target, "en")

    resolved = _pair(service, source, target, "en", **_official_pair_for(base, 0))

    # A forced official pair that the translation does not contain is a real
    # omission, so the authoritative check reports a conflict.
    assert _by_surface(resolved)["教学楼"].verdict == VERDICT_CONFLICT


def test_ordinary_word_exclusion_and_derived_names_agree(tmp_path):
    service = _service(tmp_path, zh_exclusions=("能量袋",))
    report = _pair(service, "能量袋满了。", "The bag is full.", "en")
    assert _by_surface(report)["能量袋"].reason == REASON_ORDINARY_WORD


# --------------------------------------------------------------------------
# Items 1 and 7: region-scoped claims, allocation and shortage
# --------------------------------------------------------------------------


def test_multi_meaning_mention_does_not_take_a_single_meaning_occurrence(tmp_path):
    service = _service(tmp_path)
    # 回音 has two official pairs and reports needs_review either way, so it
    # must not hold the only "Echo" that 声骸 needs.
    report = _pair(service, "回音与声骸。", "Echo and Echo Set.", "en")

    findings = _by_surface(report)
    assert findings["回音"].verdict == VERDICT_NEEDS_REVIEW
    assert findings["声骸"].verdict == VERDICT_VERIFIED
    assert findings["声骸"].target_span.text == "Echo"


def test_interleaved_official_forms_find_a_compatible_assignment(tmp_path):
    service = _service(tmp_path)
    # "Red Echo" and "Echo Red" interleave; a left-to-right sweep would spend
    # the span the second mention needs, although both can be placed.
    report = _pair(service, "红回声与回声红。", "Echo Red Echo Red. Red Echo.", "en")

    findings = _by_surface(report)
    assert findings["红回声"].verdict == VERDICT_VERIFIED
    assert findings["回声红"].verdict == VERDICT_VERIFIED
    spans = [findings["红回声"].target_span, findings["回声红"].target_span]
    assert not (
        spans[0].start < spans[1].end and spans[1].start < spans[0].end
    )
    assert {span.text for span in spans} == {"Red Echo", "Echo Red"}


def test_a_shortage_credits_neither_mention(tmp_path):
    service = _service(tmp_path)
    report = _pair(service, "今汐与今汐。", "Jinhsi.", "en")

    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_AMBIGUOUS_REPEAT] * 2
    assert all(item.target_span is None for item in report.findings)


def test_two_mentions_each_get_their_own_occurrence(tmp_path):
    service = _service(tmp_path)
    report = _pair(service, "今汐与声骸。", "Jinhsi and an Echo.", "en")

    findings = _by_surface(report)
    assert findings["今汐"].verdict == VERDICT_VERIFIED
    assert findings["声骸"].verdict == VERDICT_VERIFIED
    assert findings["今汐"].target_span.text == "Jinhsi"
    assert findings["声骸"].target_span.text == "Echo"


def test_many_competing_mentions_stay_bounded(tmp_path):
    # The allocation search must give up at its node budget instead of running
    # long: 60 mentions claim the same handful of occurrences.
    service = _service(tmp_path)
    source = "、".join(["今汐", "声骸"] * 30) + "。"
    target = ", ".join(["Jinhsi", "Echo"] * 5) + "."

    report = _pair(service, source, target)

    assert len(report.findings) == 60
    assert all(item.verdict == VERDICT_NOT_EVALUATED for item in report.findings)
