"""Review-v2 frozen coverage, alignment, and freshness contract."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from wuwaterm.application import project_review_report, review_pair
from wuwaterm.cjk_span import JIEBA_COMMIT, LEXICON_FILTER, _WORDS
from wuwaterm.constants import (
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
)
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.review import (
    REASON_AMBIGUOUS_REPEAT,
    REASON_FOUND_OUTSIDE_REGION,
    REASON_NO_TARGET_REGION,
    REASON_NOT_A_TERM,
    REASON_ORDINARY_WORD,
    RULE_VERSION_V2,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    ReviewRequestError,
    _collect_mentions,
    _paragraph_ranges,
    _sentence_ranges,
    _sentence_ranges_v2,
    _surface_index,
)
from wuwaterm.sentence import exact_lockable_sources, select_term_spans

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
    item["id"]: item
    for item in json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "review-v2-cases.json"
        ).read_text(encoding="utf-8")
    )
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_WORD_CASE = {
    "source": "倍率为1.5，今汐登场。",
    "target": "With a multiplier of one and a half, Jinhsi appears.",
    "direction": "en",
}


@pytest.fixture()
def v2_service(tmp_path: Path) -> TermService:
    path = tmp_path / "review-v2.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture-commit")),
        )
        insert_records(
            conn,
            (
                TermRecord(
                    category="character",
                    source_file="SyntheticCharacters.json",
                    source_id="character-jinhsi",
                    text_key="character-jinhsi",
                    zh="今汐",
                    en="Jinhsi",
                ),
                TermRecord(
                    category="item",
                    source_file="SyntheticItems.json",
                    source_id="item-echo",
                    text_key="item-echo",
                    zh="声骸",
                    en="Echo",
                ),
            ),
        )
        conn.commit()
    return TermService(path)


def _v2(service: TermService, case_id: str, **overrides):
    case = FROZEN[case_id]
    request = {
        "source": case["source"],
        "target": case["target"],
        "direction": case["direction"],
        "review_version": RULE_VERSION_V2,
    }
    request.update(overrides)
    return review_pair(service, **request)


def _term_findings(report):
    return list(report.findings)


def _whole_alignment(case_id: str) -> list[dict[str, object]]:
    case = FROZEN[case_id]
    return [
        {
            "source": {
                "start": 0,
                "end": len(case["source"]),
                "text": case["source"],
            },
            "target": {
                "start": 0,
                "end": len(case["target"]),
                "text": case["target"],
            },
        }
    ]


@pytest.mark.parametrize(
    ("case_id", "verdicts", "evaluated", "not_evaluated", "target_spans"),
    (
        ("basic-zh-en", [VERDICT_VERIFIED], 1, 1, [(0, 6, "Jinhsi")]),
        ("basic-en-zh", [VERDICT_VERIFIED], 1, 1, [(0, 2, "今汐")]),
        ("decimal", [VERDICT_VERIFIED], 1, 1, [(8, 14, "Jinhsi")]),
        ("nonbmp", [VERDICT_VERIFIED], 1, 1, [(1, 7, "Jinhsi")]),
    ),
)
def test_frozen_single_region_oracles(
    v2_service,
    case_id,
    verdicts,
    evaluated,
    not_evaluated,
    target_spans,
):
    report = _v2(v2_service, case_id)
    findings = _term_findings(report)

    assert [item.verdict for item in findings] == verdicts
    assert report.coverage.evaluated == evaluated
    assert report.coverage.not_evaluated == not_evaluated
    assert [
        (item.target_span.start, item.target_span.end, item.target_span.text)
        for item in findings
        if item.target_span is not None
    ] == target_spans


def test_v2_decimal_segmentation_does_not_split_between_digits():
    text = FROZEN["decimal"]["target"]
    assert len(_sentence_ranges(text)) == 2
    assert _sentence_ranges_v2(text) == [(0, len(text))]


def test_decimal_word_target_demonstrates_v2_coverage_gain(v2_service):
    v1 = review_pair(v2_service, **DECIMAL_WORD_CASE)
    v2 = review_pair(
        v2_service,
        **DECIMAL_WORD_CASE,
        review_version=RULE_VERSION_V2,
    )

    assert [item.verdict for item in v1.findings] == [VERDICT_NOT_EVALUATED]
    assert v1.coverage.evaluated == 0
    assert v1.coverage.not_evaluated == 2
    assert [item.verdict for item in v2.findings] == [VERDICT_VERIFIED]
    assert v2.coverage.evaluated == 1
    assert v2.coverage.not_evaluated == 1
    assert v2.findings[0].target_span is not None
    assert v2.findings[0].target_span.text == "Jinhsi"


@pytest.mark.parametrize("case_id", ("merge", "split"))
def test_frozen_explicit_merge_and_split_cover_both_terms(v2_service, case_id):
    report = _v2(v2_service, case_id, alignments=_whole_alignment(case_id))
    findings = _term_findings(report)

    assert [item.verdict for item in findings] == [VERDICT_VERIFIED, VERDICT_VERIFIED]
    assert report.coverage.evaluated == 2
    assert report.coverage.not_evaluated == 1
    assert {item.target_span.text for item in findings if item.target_span} == {
        "Jinhsi",
        "Echo",
    }
    assert len({item.target_span.start for item in findings if item.target_span}) == 2


@pytest.mark.parametrize(
    ("case_id", "verdict", "spans"),
    (
        ("merge", VERDICT_VERIFIED, ["Jinhsi", "Echo"]),
        ("split", VERDICT_VERIFIED, ["Jinhsi", "Echo"]),
        ("unmatched", VERDICT_NEEDS_REVIEW, [None, None]),
    ),
)
def test_frozen_mismatched_counts_pair_the_single_paragraph(
    v2_service, case_id, verdict, spans
):
    """Unequal sentence counts in one paragraph pair that paragraph as one region."""
    report = _v2(v2_service, case_id)
    findings = _term_findings(report)

    assert [item.verdict for item in findings] == [verdict, verdict]
    assert [item.target_span.text if item.target_span else None for item in findings] == spans
    assert report.coverage.evaluated == 2
    assert report.coverage.not_evaluated == 1
    starts = {item.target_span.start for item in findings if item.target_span}
    assert len(starts) == len([span for span in spans if span])


def test_frozen_repeat_shortage_does_not_reuse_target_span(v2_service):
    report = _v2(v2_service, "repeat-shortage")
    findings = _term_findings(report)

    assert [item.verdict for item in findings] == [
        VERDICT_VERIFIED,
        VERDICT_NEEDS_REVIEW,
    ]
    assert report.coverage.evaluated == 2
    assert report.coverage.not_evaluated == 1
    assert findings[0].target_span is not None
    assert findings[1].target_span is None


def test_frozen_swapped_default_regions_do_not_verify_elsewhere(v2_service):
    report = _v2(v2_service, "swapped")

    assert [item.verdict for item in report.findings] == [
        VERDICT_NOT_EVALUATED,
        VERDICT_NOT_EVALUATED,
    ]
    assert report.coverage.evaluated == 0
    assert report.coverage.not_evaluated == 3
    assert all(item.target_span is None for item in report.findings)


def test_frozen_omitted_map_only_evaluates_the_mapped_source(v2_service):
    case = FROZEN["omitted-map"]
    alignments = [
        {
            "source": {"start": 0, "end": 5, "text": case["source"][:5]},
            "target": {"start": 0, "end": 15, "text": case["target"][:15]},
        }
    ]
    report = _v2(v2_service, "omitted-map", alignments=alignments)

    assert [item.verdict for item in report.findings] == [
        VERDICT_VERIFIED,
        VERDICT_NOT_EVALUATED,
    ]
    assert report.coverage.evaluated == 1
    assert report.coverage.not_evaluated == 2
    assert report.findings[1].target_span is None


def test_explicit_empty_alignments_map_nothing(v2_service):
    report = _v2(v2_service, "basic-zh-en", alignments=[])
    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED]
    assert report.coverage.evaluated == 0
    assert report.coverage.not_evaluated == 2


def test_explicit_null_target_is_unassessed(v2_service):
    case = FROZEN["basic-zh-en"]
    report = _v2(
        v2_service,
        "basic-zh-en",
        alignments=[
            {
                "source": _span(case["source"], 0, len(case["source"])),
                "target": None,
            }
        ],
    )
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED
    assert report.findings[0].target_span is None


def _pair(service: TermService, source: str, target: str, **overrides):
    return review_pair(
        service,
        source,
        target,
        "en",
        review_version=RULE_VERSION_V2,
        **overrides,
    )


def _official_pair_for(report, index: int) -> dict[str, object]:
    finding = report.findings[index]
    return {
        "resolutions": (
            {
                "mention_id": finding.id,
                "choice": "official_pair",
                "candidate_id": finding.candidates[0].candidate_id,
            },
        ),
        "resolution_context": {
            "source_revision": report.source_revision,
            "rule_version": report.rule_version,
            "dictionary_revision": report.dictionary.revision,
            "matcher_revision": report.matcher_revision,
        },
    }


def _differing_counts(source: str, target: str) -> None:
    """The pair reaches paragraph pairing: sentence counts differ."""
    assert len(_sentence_ranges_v2(source)) != len(_sentence_ranges_v2(target))


def test_paragraph_pairing_survives_a_list_marker_the_source_lacks(v2_service):
    source = "一、机制\n\n1. 今汐登场。\n\n声骸出现。"
    target = "I. Mechanics\n\n1. Jinhsi appears.\n\nAn Echo appears."
    _differing_counts(source, target)
    source_paragraphs = _paragraph_ranges(source)
    target_paragraphs = _paragraph_ranges(target)
    assert len(source_paragraphs) == len(target_paragraphs) == 3

    report = _pair(v2_service, source, target)

    assert [item.verdict for item in report.findings] == [
        VERDICT_VERIFIED,
        VERDICT_VERIFIED,
    ]
    for finding, paragraph in zip(report.findings, target_paragraphs[1:], strict=True):
        assert finding.target_span is not None
        assert paragraph[0] <= finding.target_span.start
        assert finding.target_span.end <= paragraph[1]
    assert [item.target_span.text for item in report.findings] == ["Jinhsi", "Echo"]
    assert report.coverage.evaluated == 2


@pytest.mark.parametrize(
    ("source", "target"),
    (
        # Strict region pairs: the sentence counts agree inside each paragraph.
        ("一、机制\n\n今汐登场。\n\n声骸出现。", "I. Mechanics\n\nAn Echo appears.\n\nJinhsi appears."),
        # The first paragraph pair is guarded, its form is in the other paragraph.
        ("今汐登场。\n\n声骸出现。", "An Echo appears. Truly.\n\nJinhsi appears."),
    ),
)
def test_form_found_only_in_another_paragraph_is_not_credited(v2_service, source, target):
    _differing_counts(source, target)

    report = _pair(v2_service, source, target)

    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_FOUND_OUTSIDE_REGION] * 2
    assert all(item.target_span is None for item in report.findings)


def test_unequal_paragraph_counts_map_nothing(v2_service):
    source = "今汐登场。\n\n声骸出现。"
    target = "Jinhsi appears. An Echo appears. Truly."
    _differing_counts(source, target)
    assert len(_paragraph_ranges(source)) != len(_paragraph_ranges(target))

    report = _pair(v2_service, source, target)

    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_NO_TARGET_REGION] * 2
    assert report.coverage.evaluated == 0
    assert report.coverage.not_evaluated == 3


def test_guarded_paragraph_with_fewer_occurrences_than_mentions_is_ambiguous(v2_service):
    source, target = "今汐与今汐。", "Jinhsi. Indeed."
    _differing_counts(source, target)

    report = _pair(v2_service, source, target)
    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_AMBIGUOUS_REPEAT] * 2
    assert all(item.target_span is None for item in report.findings)
    assert all(item.candidates for item in report.findings)

    # A confirmed pair on the second mention would otherwise be a conflict that
    # the correct translation does not deserve: the first mention took the only
    # occurrence.
    resolved = _pair(v2_service, source, target, **_official_pair_for(report, 1))
    assert [item.verdict for item in resolved.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in resolved.findings] == [REASON_AMBIGUOUS_REPEAT] * 2

    # The same shortage inside agreeing sentence counts keeps its old verdicts.
    equal = _v2(v2_service, "repeat-shortage")
    assert [item.verdict for item in equal.findings] == [
        VERDICT_VERIFIED,
        VERDICT_NEEDS_REVIEW,
    ]


def test_guarded_mentions_compete_for_an_inflected_occurrence(tmp_path):
    """Different forms that can answer with the same target span compete for it."""
    service = _plural_service(
        tmp_path, TermRecord("item", "fixture", "echoes", "echoes", "今汐", "Echoes")
    )
    # "Echoes" is 今汐's exact form and 声骸's ("Echo") plural: one span, two claims.
    source, target = "今汐与声骸。", "Echoes. Indeed."
    _differing_counts(source, target)

    report = _pair(service, source, target)
    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2
    assert [item.reason for item in report.findings] == [REASON_AMBIGUOUS_REPEAT] * 2

    resolved = _pair(service, source, target, **_official_pair_for(report, 1))
    assert [item.verdict for item in resolved.findings] == [VERDICT_NOT_EVALUATED] * 2

    # 今汐 can take only "Echoes", which 声骸 may also take: the whole group
    # stays unassessed rather than crediting 声骸 with 今汐's occurrence.
    report = _pair(service, source, "Echoes and an Echo. Indeed.")
    assert [item.verdict for item in report.findings] == [VERDICT_NOT_EVALUATED] * 2

    # Each mention can take two spans: both are credited, on distinct spans.
    report = _pair(service, source, "Echoes and Echoes. Indeed.")
    assert [item.verdict for item in report.findings] == [VERDICT_VERIFIED] * 2
    assert len({item.target_span.start for item in report.findings}) == 2


def test_guarded_mentions_with_unrelated_forms_do_not_compete(v2_service):
    source, target = "今汐与声骸。", "Jinhsi and an Echo. Indeed."
    _differing_counts(source, target)

    report = _pair(v2_service, source, target)
    assert [item.verdict for item in report.findings] == [VERDICT_VERIFIED] * 2
    assert [item.target_span.text for item in report.findings] == ["Jinhsi", "Echo"]


_V, _NR = VERDICT_VERIFIED, VERDICT_NEEDS_REVIEW
_OUTSIDE = (VERDICT_NOT_EVALUATED, REASON_FOUND_OUTSIDE_REGION)
_AMBIGUOUS = (VERDICT_NOT_EVALUATED, REASON_AMBIGUOUS_REPEAT)
# (case, source, target, expected per mention). A verified entry names the
# target text it is credited with. Each case runs on the equal sentence count
# path as written, and on the guarded paragraph path with a list marker the
# source writes as "一、" and the target as "I.".
_OCCURRENCE_CASES = (
    ("exact", "今汐登场。", "Jinhsi appears.", [(_V, "Jinhsi")], None),
    ("inflected", "声骸出现。", "Echoes appear.", [(_V, "Echoes")], None),
    (
        "inflection competes",
        "回响与声骸。",
        "Echoes.",
        # Equal counts keep the positional greedy of repeat-shortage.
        [(_V, "Echoes"), (_NR, None)],
        [_AMBIGUOUS, _AMBIGUOUS],
    ),
    (
        "nested, both present",
        "声骸与声骸套装。",
        "Echo and Echo Set.",
        [(_V, "Echo"), (_V, "Echo Set")],
        None,
    ),
    (
        "nested, order swapped",
        "声骸与声骸套装。",
        "Echo Set and Echo.",
        [(_V, "Echo"), (_V, "Echo Set")],
        None,
    ),
    (
        "nested, shorter missing",
        "声骸与声骸套装。",
        "Echo Set.",
        [(_NR, None), (_V, "Echo Set")],
        None,
    ),
    (
        "repeated, one occurrence",
        "今汐与今汐。",
        "Jinhsi.",
        [(_V, "Jinhsi"), (_NR, None)],
        [_AMBIGUOUS, _AMBIGUOUS],
    ),
    (
        "shifted to the other paragraph",
        "今汐登场。\n\n声骸出现。",
        "An Echo appears.\n\nJinhsi appears.",
        [_OUTSIDE, _OUTSIDE],
        None,
    ),
    ("missing entirely", "今汐登场。", "Someone appears.", [(_NR, None)], None),
    (
        "unrelated terms",
        "今汐与声骸。",
        "Jinhsi and an Echo.",
        [(_V, "Jinhsi"), (_V, "Echo")],
        None,
    ),
)


_NA = (VERDICT_NOT_EVALUATED, REASON_NOT_A_TERM)
_ORDINARY = (VERDICT_NOT_EVALUATED, REASON_ORDINARY_WORD)
_LONGER_ONLY = ("声骸与声骸套装。", "Echo Set.")
# User actions and ordinary-word exclusions on the nested pair. Only a mention
# that still asserts a constraint reserves its span for the longest match, so
# a dismissed or excluded "Echo Set" leaves its "Echo" to 声骸, and a confirmed
# 声骸 is then credited instead of blamed. (case, source, target, equal,
# guarded, actions by mention index, zh surfaces excluded as ordinary words)
_OCCURRENCE_ACTIONS = (
    ("longer dismissed", *_LONGER_ONLY, [(_V, "Echo"), _NA], None, {1: "not_a_term"}, ()),
    (
        "longer dismissed, shorter confirmed",
        *_LONGER_ONLY,
        [(_V, "Echo"), _NA],
        None,
        {0: "official_pair", 1: "not_a_term"},
        (),
    ),
    ("longer excluded", *_LONGER_ONLY, [(_V, "Echo"), _ORDINARY], None, {}, ("声骸套装",)),
    (
        "longer excluded, shorter confirmed",
        *_LONGER_ONLY,
        [(_V, "Echo"), _ORDINARY],
        None,
        {0: "official_pair"},
        ("声骸套装",),
    ),
    (
        "longer excluded but confirmed",
        *_LONGER_ONLY,
        [(_NR, None), (_V, "Echo Set")],
        None,
        {1: "official_pair"},
        ("声骸套装",),
    ),
    (
        "shorter confirmed, longer a term",
        *_LONGER_ONLY,
        [(VERDICT_CONFLICT, None), (_V, "Echo Set")],
        None,
        {0: "official_pair"},
        (),
    ),
)
_C = (VERDICT_CONFLICT, None)
# The resolution dimension: 星辉 has two candidates, "Echo Set" and
# "Starlight", and 声骸 ("Echo") is confirmed in every row, so a wrong
# reservation shows as a confirmed conflict. 星辉 is unresolved, resolved to
# either candidate, dismissed, or an unresolved ordinary word; only the forms
# it still asserts may reserve "Echo Set" and hide the "Echo" inside it.
_SHORT_CONFIRMED = {0: ("official_pair", "Echo")}
_RESOLUTION_STATES = (
    ("unresolved", {}, ()),
    ("selected Echo Set", {1: ("official_pair", "Echo Set")}, ()),
    ("selected Starlight", {1: ("official_pair", "Starlight")}, ()),
    ("dismissed", {1: ("not_a_term", None)}, ()),
    ("excluded", {}, ("星辉",)),
)
_RESOLUTION_STRUCTURES = (
    (
        "nested present",
        "Echo and Echo Set.",
        {
            "unresolved": [(_V, "Echo"), (_V, "Echo Set")],
            "selected Echo Set": [(_V, "Echo"), (_V, "Echo Set")],
            "selected Starlight": [(_V, "Echo"), _C],
            "dismissed": [(_V, "Echo"), _NA],
            "excluded": [(_V, "Echo"), _ORDINARY],
        },
    ),
    (
        "nested shorter missing",
        "Echo Set.",
        {
            "unresolved": [_C, (_V, "Echo Set")],
            "selected Echo Set": [_C, (_V, "Echo Set")],
            "selected Starlight": [(_V, "Echo"), _C],
            "dismissed": [(_V, "Echo"), _NA],
            "excluded": [(_V, "Echo"), _ORDINARY],
        },
    ),
    (
        "inflected",
        "Echoes and Echo Set.",
        {
            "unresolved": [(_V, "Echoes"), (_V, "Echo Set")],
            "selected Echo Set": [(_V, "Echoes"), (_V, "Echo Set")],
            "selected Starlight": [(_V, "Echoes"), _C],
            "dismissed": [(_V, "Echoes"), _NA],
            "excluded": [(_V, "Echoes"), _ORDINARY],
        },
    ),
    (
        "unrelated",
        "Echo and Starlight.",
        {
            "unresolved": [(_V, "Echo"), (_V, "Starlight")],
            "selected Echo Set": [(_V, "Echo"), _C],
            "selected Starlight": [(_V, "Echo"), (_V, "Starlight")],
            "dismissed": [(_V, "Echo"), _NA],
            "excluded": [(_V, "Echo"), _ORDINARY],
        },
    ),
)
_OCCURRENCE_RESOLUTIONS = tuple(
    (
        f"{structure}, 星辉 {state}",
        "声骸与星辉。",
        target,
        expected[state],
        None,
        {**_SHORT_CONFIRMED, **actions},
        excluded,
    )
    for structure, target, expected in _RESOLUTION_STRUCTURES
    for state, actions, excluded in _RESOLUTION_STATES
)
# 回音 has two candidates of its own, "Echo" and "Echo Set". The longest
# match holds among one mention's own candidates whatever is selected: the
# "Echo" inside "Echo Set" is never an Echo of 回音. A verified entry may name
# the target start when the text alone would not tell two spans apart.
_OWN_CANDIDATES = (
    ("own longer candidate, unresolved", "回音出现。", "Echo Set appears.", [(_V, "Echo Set")], None, {}, ()),
    (
        "own longer candidate, Echo confirmed, only Echo Set",
        "回音出现。",
        "Echo Set appears.",
        [_C],
        None,
        {0: ("official_pair", "Echo")},
        (),
    ),
    (
        "own longer candidate, Echo confirmed, both",
        "回音出现。",
        "Echo Set and Echo appear.",
        [(_V, "Echo", 13)],
        None,
        {0: ("official_pair", "Echo")},
        (),
    ),
)
_OCCURRENCE_MATRIX = (
    tuple((*row, {}, ()) for row in _OCCURRENCE_CASES)
    + _OCCURRENCE_ACTIONS
    + _OCCURRENCE_RESOLUTIONS
    + _OWN_CANDIDATES
)


def _matrix_service(tmp_path: Path, excluded: tuple[str, ...]) -> TermService:
    path = tmp_path / "occurrence-matrix.db"
    metadata = [("schema_version", "synthetic-v1"), ("source_commit", "fixture-commit")]
    if excluded:
        metadata.append(
            (
                FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
                json.dumps(
                    {
                        "version": FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
                        "min_occurrences": FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
                        "max_aligned_ratio": list(FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO),
                        "surfaces": [[surface, 12, 2] for surface in sorted(excluded)],
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
        insert_records(
            conn,
            (
                *_PLURAL_RECORDS,
                TermRecord("character", "fixture", "jinhsi", "jinhsi", "今汐", "Jinhsi"),
                TermRecord("item", "fixture", "echo-set", "echo-set", "声骸套装", "Echo Set"),
                TermRecord("item", "fixture", "echoes", "echoes", "回响", "Echoes"),
                TermRecord("item", "fixture", "xinghui-set", "xinghui-set", "星辉", "Echo Set"),
                TermRecord("character", "fixture", "xinghui", "xinghui", "星辉", "Starlight"),
                TermRecord("item", "fixture", "huiyin", "huiyin", "回音", "Echo"),
                TermRecord("item", "fixture", "huiyin-set", "huiyin-set", "回音", "Echo Set"),
            ),
        )
        conn.commit()
    return TermService(path)


@pytest.mark.parametrize("path", ("equal", "guarded"))
@pytest.mark.parametrize(
    ("case", "source", "target", "equal", "guarded", "actions", "excluded"),
    _OCCURRENCE_MATRIX,
    ids=[row[0] for row in _OCCURRENCE_MATRIX],
)
def test_occurrence_matrix(
    tmp_path, path, case, source, target, equal, guarded, actions, excluded
):
    """The judge and the guard read one occurrence model on both mapping paths."""
    service = _matrix_service(tmp_path, excluded)
    expected = equal
    if path == "guarded":
        source, target = "一、" + source, "I. " + target
        _differing_counts(source, target)
        assert len(_paragraph_ranges(source)) == len(_paragraph_ranges(target))
        expected = guarded or equal
    else:
        assert len(_sentence_ranges_v2(source)) == len(_sentence_ranges_v2(target))

    report = _pair(service, source, target)
    if actions:
        resolutions = []
        for index, action in sorted(actions.items()):
            choice, form = action if isinstance(action, tuple) else (action, None)
            finding = report.findings[index]
            resolution = {"mention_id": finding.id, "choice": choice}
            if choice == "official_pair":
                resolution["candidate_id"] = next(
                    candidate.candidate_id
                    for candidate in finding.candidates
                    if form is None or candidate.en == form
                )
            resolutions.append(resolution)
        report = _pair(
            service,
            source,
            target,
            resolutions=tuple(resolutions),
            resolution_context={
                "source_revision": report.source_revision,
                "rule_version": report.rule_version,
                "dictionary_revision": report.dictionary.revision,
                "matcher_revision": report.matcher_revision,
            },
        )

    offset = 3 if path == "guarded" else 0
    actual = [
        (item.verdict, item.reason)
        if item.verdict == VERDICT_NOT_EVALUATED
        else (item.verdict, item.target_span.text if item.target_span else None)
        if len(want) == 2
        else (item.verdict, item.target_span.text, item.target_span.start - offset)
        for item, want in zip(report.findings, expected, strict=True)
    ]
    assert actual == expected, case


def test_guarded_paragraph_with_many_mentions_stays_fast(v2_service):
    # Hundreds of mentions competing for hundreds of spans in one paragraph,
    # within the 2,000-scalar limit on each side.
    source = "、".join(["今汐", "声骸"] * 200) + "。"
    target = ", ".join(["Jinhsi", "Echo"] * 140) + ". Indeed."
    assert len(source) <= 2000 and len(target) <= 2000
    _differing_counts(source, target)

    started = time.perf_counter()
    report = _pair(v2_service, source, target)
    elapsed = time.perf_counter() - started

    assert {item.reason for item in report.findings} == {REASON_AMBIGUOUS_REPEAT}
    assert elapsed < 1.0


def test_guarded_paragraph_with_no_form_anywhere_keeps_the_ordinary_verdicts(v2_service):
    source = "今汐登场。\n\n声骸出现。"
    target = "Someone appears. Truly.\n\nSomething appears."
    _differing_counts(source, target)

    report = _pair(v2_service, source, target)
    assert [item.verdict for item in report.findings] == [VERDICT_NEEDS_REVIEW] * 2
    assert all(item.target_span is None for item in report.findings)

    resolved = _pair(v2_service, source, target, **_official_pair_for(report, 0))
    assert [item.verdict for item in resolved.findings] == [
        VERDICT_CONFLICT,
        VERDICT_NEEDS_REVIEW,
    ]


def test_v2_reason_is_internal_and_only_on_not_evaluated_findings(v2_service):
    reasons = {
        REASON_NOT_A_TERM,
        REASON_ORDINARY_WORD,
        REASON_NO_TARGET_REGION,
        REASON_FOUND_OUTSIDE_REGION,
        REASON_AMBIGUOUS_REPEAT,
    }
    baseline = _v2(v2_service, "basic-zh-en")
    case = FROZEN["basic-zh-en"]
    by_reason = {
        REASON_NOT_A_TERM: _v2(
            v2_service,
            "basic-zh-en",
            resolutions=(
                {"mention_id": baseline.findings[0].id, "choice": "not_a_term"},
            ),
            resolution_context=_official_pair_for(baseline, 0)["resolution_context"],
        ),
        REASON_NO_TARGET_REGION: _v2(
            v2_service,
            "basic-zh-en",
            alignments=[
                {"source": _span(case["source"], 0, len(case["source"])), "target": None}
            ],
        ),
        REASON_FOUND_OUTSIDE_REGION: _v2(v2_service, "swapped"),
        REASON_AMBIGUOUS_REPEAT: _pair(v2_service, "今汐与今汐。", "Jinhsi. Indeed."),
    }  # ordinary_word is pinned with the exclusion fixtures
    for reason, report in by_reason.items():
        assert {item.reason for item in report.findings} == {reason}

    unmatched = _v2(v2_service, "unmatched")
    conflict = _v2(v2_service, "unmatched", **_official_pair_for(unmatched, 0))
    assert VERDICT_CONFLICT in {item.verdict for item in conflict.findings}
    for report in (
        *by_reason.values(),
        baseline,
        unmatched,
        conflict,
        _v2(v2_service, "repeat-shortage"),
    ):
        for item in report.findings:
            if item.verdict == VERDICT_NOT_EVALUATED:
                assert item.reason in reasons
            else:
                assert item.reason is None

    v1 = review_pair(v2_service, **DECIMAL_WORD_CASE)
    assert [item.verdict for item in v1.findings] == [VERDICT_NOT_EVALUATED]
    assert v1.findings[0].reason is None

    body = project_review_report(by_reason[REASON_FOUND_OUTSIDE_REGION], "request-reason")
    assert body["findings"]
    for finding in body["findings"]:
        assert set(finding) == {
            "candidates",
            "candidates_truncated",
            "id",
            "rule_id",
            "source_span",
            "target_span",
            "verdict",
        }


def test_v2_wire_additions_are_raw_hex_and_top_level_is_unchanged(v2_service):
    report = _v2(v2_service, "basic-zh-en")
    body = project_review_report(report, "request-v2")

    assert set(body) == {
        "coverage",
        "dictionary",
        "findings",
        "matcher_revision",
        "request_id",
        "rule_version",
        "source_revision",
        "target_revision",
        "truncated",
    }
    assert body["rule_version"] == RULE_VERSION_V2
    assert HEX64.fullmatch(body["matcher_revision"])
    legacy = project_review_report(
        review_pair(v2_service, "今汐。", "Jinhsi.", "en"),
        "request-v1",
    )
    assert legacy["rule_version"] == "review-v1"
    assert "matcher_revision" not in legacy
    assert set(body["dictionary"]) == {
        "schema_version",
        "source_commit",
        "term_count",
        "revision",
    }
    assert HEX64.fullmatch(body["dictionary"]["revision"])
    candidates = body["findings"][0]["candidates"]
    assert candidates
    assert all(
        set(item) == {"candidate_id", "category", "en", "sources", "zh"}
        for item in candidates
    )
    assert all(HEX64.fullmatch(item["candidate_id"]) for item in candidates)


def test_snapshot_revision_and_candidate_id_detect_same_count_provenance_change(
    v2_service,
):
    first = _v2(v2_service, "basic-zh-en")
    first_candidate = first.findings[0].candidates[0].candidate_id

    with connect(v2_service.db_path) as conn:
        conn.execute(
            "UPDATE terms SET source_id = ? WHERE source_id = ?",
            ("character-jinhsi-changed", "character-jinhsi"),
        )
        conn.commit()

    second = _v2(v2_service, "basic-zh-en")
    assert first.dictionary.term_count == second.dictionary.term_count == 2
    assert first.dictionary.revision != second.dictionary.revision
    assert first_candidate != second.findings[0].candidates[0].candidate_id


def test_v2_uses_the_bounded_review_snapshot_method(v2_service, monkeypatch):
    monkeypatch.setattr(
        v2_service,
        "metadata",
        lambda: pytest.fail("v2 must not make a separate metadata read"),
    )
    monkeypatch.setattr(
        v2_service,
        "term_count",
        lambda: pytest.fail("v2 must not make a separate count read"),
    )
    monkeypatch.setattr(
        v2_service,
        "entries",
        lambda: pytest.fail("v2 must not make a separate entries read"),
    )

    report = _v2(v2_service, "basic-zh-en")
    assert report.dictionary.term_count == 2
    assert HEX64.fullmatch(report.dictionary.revision or "")


def test_current_candidate_resolution_requires_and_accepts_exact_context(v2_service):
    baseline = _v2(v2_service, "basic-zh-en")
    finding = baseline.findings[0]
    candidate_id = finding.candidates[0].candidate_id
    resolutions = (
        {
            "mention_id": finding.id,
            "choice": "official_pair",
            "candidate_id": candidate_id,
        },
    )
    context = {
        "source_revision": baseline.source_revision,
        "rule_version": baseline.rule_version,
        "dictionary_revision": baseline.dictionary.revision,
        "matcher_revision": baseline.matcher_revision,
    }

    resolved = _v2(
        v2_service,
        "basic-zh-en",
        resolutions=resolutions,
        resolution_context=context,
    )
    assert resolved.findings[0].verdict == VERDICT_VERIFIED

    with pytest.raises(ReviewRequestError, match="resolution_context") as missing:
        _v2(v2_service, "basic-zh-en", resolutions=resolutions)
    assert missing.value.code == "invalid_request"

    stale = dict(context)
    stale["dictionary_revision"] = "0" * 64
    with pytest.raises(ReviewRequestError, match="stale") as mismatch:
        _v2(
            v2_service,
            "basic-zh-en",
            resolutions=resolutions,
            resolution_context=stale,
        )
    assert mismatch.value.code == "invalid_request"

    wrong_candidate = ({**resolutions[0], "candidate_id": "f" * 64},)
    with pytest.raises(ReviewRequestError, match="not current") as candidate:
        _v2(
            v2_service,
            "basic-zh-en",
            resolutions=wrong_candidate,
            resolution_context=context,
        )
    assert candidate.value.code == "invalid_request"


def test_v2_not_a_term_keeps_its_two_field_shape_but_requires_context(v2_service):
    baseline = _v2(v2_service, "basic-zh-en")
    resolution = {
        "mention_id": baseline.findings[0].id,
        "choice": "not_a_term",
    }
    context = {
        "source_revision": baseline.source_revision,
        "rule_version": baseline.rule_version,
        "dictionary_revision": baseline.dictionary.revision,
        "matcher_revision": baseline.matcher_revision,
    }
    report = _v2(
        v2_service,
        "basic-zh-en",
        resolutions=(resolution,),
        resolution_context=context,
    )
    assert report.findings[0].verdict == VERDICT_NOT_EVALUATED


def _span(text: str, start: int, end: int) -> dict[str, object]:
    return {"start": start, "end": end, "text": text[start:end]}


@pytest.mark.parametrize(
    "mutate",
    (
        lambda source, target: [
            {"source": _span(source, 0, 2), "target": _span(target, 0, 6)},
            {"source": _span(source, 1, 3), "target": _span(target, 7, 11)},
        ],
        lambda source, target: [
            {"source": _span(source, 0, 2), "target": _span(target, 7, 11)},
            {"source": _span(source, 3, 5), "target": _span(target, 0, 6)},
        ],
        lambda source, target: [
            {
                "source": {"start": 0, "end": len(source) + 1, "text": source},
                "target": _span(target, 0, len(target)),
            }
        ],
        lambda source, target: [
            {
                "source": {"start": True, "end": 2, "text": source[:2]},
                "target": _span(target, 0, 6),
            }
        ],
        lambda source, target: [
            {
                "source": {"start": 0, "end": 2, "text": "stale"},
                "target": _span(target, 0, 6),
            }
        ],
        lambda source, target: [
            {
                "source": _span(source, 1, len(source)),
                "target": _span(target, 0, len(target)),
            }
        ],
        lambda source, target: [
            {
                "source": _span(source, 0, len(source)),
                "target": _span(target, 0, len(target)),
                "unknown": True,
            }
        ],
    ),
    ids=(
        "source-overlap",
        "target-unordered",
        "out-of-bounds",
        "boolean-offset",
        "stale-text",
        "term-cut",
        "unknown-key",
    ),
)
def test_invalid_alignment_shapes_fail_closed(v2_service, mutate):
    source = "今汐。声骸。"
    target = "Jinhsi. Echo."
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(
            v2_service,
            source,
            target,
            "en",
            review_version=RULE_VERSION_V2,
            alignments=mutate(source, target),
        )
    assert caught.value.code == "invalid_request"


def test_more_than_64_alignments_is_invalid(v2_service):
    source = "x" * 65
    target = "y" * 65
    alignments = [
        {
            "source": _span(source, index, index + 1),
            "target": _span(target, index, index + 1),
        }
        for index in range(65)
    ]
    with pytest.raises(ReviewRequestError) as caught:
        review_pair(
            v2_service,
            source,
            target,
            "en",
            review_version=RULE_VERSION_V2,
            alignments=alignments,
        )
    assert caught.value.code == "invalid_request"


def test_v1_rejects_v2_only_fields_and_unknown_version(v2_service):
    with pytest.raises(ReviewRequestError) as fields:
        review_pair(
            v2_service,
            "今汐。",
            "Jinhsi.",
            "en",
            alignments=[],
        )
    assert fields.value.code == "invalid_request"

    with pytest.raises(ReviewRequestError) as version:
        review_pair(
            v2_service,
            "今汐。",
            "Jinhsi.",
            "en",
            review_version="review-v3",
        )
    assert version.value.code == "invalid_request"


def test_frozen_before_after_measurement(v2_service):
    """Emit the frozen v1/v2 matrix while pinning its safety properties."""

    def summarize(report):
        return {
            "evaluated": report.coverage.evaluated,
            "not_evaluated": report.coverage.not_evaluated,
            "verdicts": [item.verdict for item in report.findings],
            "target_spans": [
                (
                    None
                    if item.target_span is None
                    else {
                        "start": item.target_span.start,
                        "end": item.target_span.end,
                        "text": item.target_span.text,
                    }
                )
                for item in report.findings
            ],
        }

    matrix = {}
    for case_id in (
        "basic-zh-en",
        "basic-en-zh",
        "decimal",
        "decimal-word",
        "merge",
        "split",
        "repeat-shortage",
        "swapped",
        "unmatched",
        "omitted-map",
        "nonbmp",
    ):
        case = DECIMAL_WORD_CASE if case_id == "decimal-word" else FROZEN[case_id]
        v1 = review_pair(
            v2_service,
            case["source"],
            case["target"],
            case["direction"],
        )
        overrides = {}
        if case_id in {"merge", "split"}:
            overrides["alignments"] = _whole_alignment(case_id)
        elif case_id == "omitted-map":
            overrides["alignments"] = [
                {
                    "source": {
                        "start": 0,
                        "end": 5,
                        "text": case["source"][:5],
                    },
                    "target": {
                        "start": 0,
                        "end": 15,
                        "text": case["target"][:15],
                    },
                }
            ]
        v2 = (
            review_pair(
                v2_service,
                **case,
                review_version=RULE_VERSION_V2,
                **overrides,
            )
            if case_id == "decimal-word"
            else _v2(v2_service, case_id, **overrides)
        )
        matrix[case_id] = {"v1": summarize(v1), "v2": summarize(v2)}

    assert matrix["decimal"]["v2"]["verdicts"] == [VERDICT_VERIFIED]
    assert matrix["decimal-word"]["v1"]["verdicts"] == [VERDICT_NOT_EVALUATED]
    assert matrix["decimal-word"]["v2"]["verdicts"] == [VERDICT_VERIFIED]
    for case_id in ("merge", "split"):
        assert matrix[case_id]["v2"]["verdicts"].count(VERDICT_VERIFIED) == 2
        assert matrix[case_id]["v1"]["verdicts"].count(VERDICT_VERIFIED) < 2
    assert matrix["swapped"]["v2"]["verdicts"].count(VERDICT_VERIFIED) == 0
    assert matrix["unmatched"]["v2"]["verdicts"].count(VERDICT_VERIFIED) == 0
    assert matrix["omitted-map"]["v2"]["verdicts"].count(VERDICT_VERIFIED) == 1
    assert matrix["repeat-shortage"]["v2"]["verdicts"].count(VERDICT_VERIFIED) == 1

    print("FROZEN_REVIEW_METRICS=" + json.dumps(matrix, ensure_ascii=False, sort_keys=True))


_ONE_CHARACTER_RECORDS = (
    TermRecord("speaker", "fixture", "wo", "wo", "我", "Rover"),
    TermRecord("core_term", "fixture", "rover", "rover", "漂泊者", "Rover"),
    TermRecord("resonator", "fixture", "xin", "xin", "心", "Hsin"),
    TermRecord("speaker", "fixture", "xin-speaker", "xin-speaker", "心", "Hsin"),
    TermRecord("resonator", "fixture", "chun", "chun", "椿", "Camellya"),
    TermRecord("echo", "fixture", "jiao", "jiao", "角", "Jué"),
    TermRecord("item", "fixture", "yan", "yan", "盐", "Salt"),
    TermRecord("item", "fixture", "mi", "mi", "米", "Rice"),
    TermRecord("resonator", "fixture", "jinhsi", "jinhsi", "今汐", "Jinhsi"),
    TermRecord("core_term", "fixture", "echo", "echo", "声骸", "Echo"),
    TermRecord("resonator", "fixture", "jia", "jia", "甲", "A"),
)


def _one_character_service(tmp_path: Path) -> TermService:
    path = tmp_path / "one-character-v2.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture-commit")),
        )
        insert_records(conn, _ONE_CHARACTER_RECORDS)
        conn.commit()
    return TermService(path)


def _v2_surfaces(service: TermService, source: str) -> set[str]:
    report = review_pair(
        service, source, "placeholder", "en", review_version=RULE_VERSION_V2
    )
    assert report.rule_version == RULE_VERSION_V2
    return {item.source_span.text for item in report.findings}


def test_review_v2_one_character_mentions_follow_sentence_locking(tmp_path):
    # empty: item category rejects 盐 and 米 even though 和 would pass context.
    # admitted-name: 椿 stays in a name-like context, including a permitted left edge.
    # kept-multichar: 今汐 and 声骸 are untouched by the one-character filter.
    service = _one_character_service(tmp_path)
    assert _v2_surfaces(service, "盐和米不是一句术语。") == set()
    assert _v2_surfaces(service, "椿加入队伍。") == {"椿"}
    assert _v2_surfaces(service, "和椿加入队伍。") == {"椿"}
    assert _v2_surfaces(service, "请让椿加入队伍。") == {"椿"}
    assert _v2_surfaces(service, "今汐装备了声骸。") == {"今汐", "声骸"}
    assert _v2_surfaces(service, "我是漂泊者。") == {"漂泊者"}
    assert _v2_surfaces(service, "心里想着今汐。") == {"今汐"}
    assert _v2_surfaces(service, "角色提到香椿和声骸。") == {"声骸"}
    assert _v2_surfaces(service, "香椿") == set()
    assert _v2_surfaces(service, "椿树") == set()
    # A single-character name is a mention unless an ordinary word covers it:
    # these are the same contexts sentence.py hands the model as references.
    assert _v2_surfaces(service, "欢迎椿") == {"椿"}
    assert _v2_surfaces(service, "快找椿") == {"椿"}
    assert _v2_surfaces(service, "给椿。") == {"椿"}
    assert _v2_surfaces(service, "A joins the party.") == set()


def test_review_v2_unknown_mention_id_stays_invalid_request(tmp_path):
    # error-path: a resolution for a span the new collector did not emit is
    # still rejected. The fresh report is what makes the context current.
    service = _one_character_service(tmp_path)
    source = "椿加入队伍。"
    baseline = review_pair(
        service, source, "placeholder", "en", review_version=RULE_VERSION_V2
    )
    context = {
        "source_revision": baseline.source_revision,
        "rule_version": baseline.rule_version,
        "dictionary_revision": baseline.dictionary.revision,
        "matcher_revision": baseline.matcher_revision,
    }
    with pytest.raises(ReviewRequestError, match="does not match a finding") as caught:
        review_pair(
            service,
            source,
            "placeholder",
            "en",
            resolutions=({"mention_id": "0:1:nope", "choice": "not_a_term"},),
            review_version=RULE_VERSION_V2,
            resolution_context=context,
        )
    assert caught.value.code == "invalid_request"


def test_review_v2_drops_cross_word_spans_and_rejects_their_old_ids(sample_db):
    service = TermService(sample_db)
    source = "回声骸骨"
    current = review_pair(
        service, source, "placeholder", "en", review_version=RULE_VERSION_V2
    )
    assert current.rule_version == RULE_VERSION_V2
    assert {item.source_span.text for item in current.findings} == set()
    historical = review_pair(service, source, "placeholder", "en")
    removed = next(item for item in historical.findings if item.source_span.text == "声骸")
    context = {
        "source_revision": current.source_revision,
        "rule_version": current.rule_version,
        "dictionary_revision": current.dictionary.revision,
        "matcher_revision": current.matcher_revision,
    }
    with pytest.raises(ReviewRequestError, match="does not match a finding") as caught:
        review_pair(
            service,
            source,
            "placeholder",
            "en",
            resolutions=({"mention_id": removed.id, "choice": "not_a_term"},),
            review_version=RULE_VERSION_V2,
            resolution_context=context,
        )
    assert caught.value.code == "invalid_request"
    kept = review_pair(
        service,
        "给安可装备声骸。",
        "placeholder",
        "en",
        review_version=RULE_VERSION_V2,
    )
    assert {item.source_span.text for item in kept.findings} == {"安可", "声骸"}


def test_kept_span_without_current_matcher_revision_is_stale(sample_db):
    # A span both the old and new matchers keep is still a different rule
    # basis. Omitting matcher_revision is stale, not a missing finding.
    service = TermService(sample_db)
    source = "给安可装备声骸。"
    report = review_pair(
        service, source, "placeholder", "en", review_version=RULE_VERSION_V2
    )
    assert report.rule_version == RULE_VERSION_V2
    assert HEX64.fullmatch(report.matcher_revision or "")
    assert {item.source_span.text for item in report.findings} == {"安可", "声骸"}
    finding = next(item for item in report.findings if item.source_span.text == "安可")
    resolutions = ({"mention_id": finding.id, "choice": "not_a_term"},)
    old = {
        "source_revision": report.source_revision,
        "rule_version": report.rule_version,
        "dictionary_revision": report.dictionary.revision,
    }
    with pytest.raises(ReviewRequestError, match="stale") as stale:
        review_pair(
            service,
            source,
            "placeholder",
            "en",
            resolutions=resolutions,
            review_version=RULE_VERSION_V2,
            resolution_context=old,
        )
    assert stale.value.code == "invalid_request"
    with pytest.raises(ReviewRequestError, match="not valid") as blank:
        review_pair(
            service,
            source,
            "placeholder",
            "en",
            resolutions=resolutions,
            review_version=RULE_VERSION_V2,
            resolution_context={**old, "matcher_revision": ""},
        )
    assert blank.value.code == "invalid_request"
    with pytest.raises(ReviewRequestError, match="stale") as other:
        review_pair(
            service,
            source,
            "placeholder",
            "en",
            resolutions=resolutions,
            review_version=RULE_VERSION_V2,
            resolution_context={**old, "matcher_revision": "0" * 64},
        )
    assert other.value.code == "invalid_request"
    accepted = review_pair(
        service,
        source,
        "placeholder",
        "en",
        resolutions=resolutions,
        review_version=RULE_VERSION_V2,
        resolution_context={**old, "matcher_revision": report.matcher_revision},
    )
    assert accepted.rule_version == RULE_VERSION_V2
    assert {item.source_span.text for item in accepted.findings} == {"安可", "声骸"}
    kept_finding = next(item for item in accepted.findings if item.id == finding.id)
    assert kept_finding.verdict == VERDICT_NOT_EVALUATED


def _finding_for(report, term):
    return next(item for item in report.findings if item.source_span.text == term)


def test_review_v2_target_cross_word_span_is_not_verified(sample_db):
    service = TermService(sample_db)
    report = review_pair(
        service, "Echo.", "回声骸骨。", "zh", review_version=RULE_VERSION_V2
    )
    finding = _finding_for(report, "Echo")
    assert finding.verdict == VERDICT_NEEDS_REVIEW
    assert finding.target_span is None

    report = review_pair(
        service, "Encore.", "平安可贵。", "zh", review_version=RULE_VERSION_V2
    )
    finding = _finding_for(report, "Encore")
    assert finding.verdict == VERDICT_NEEDS_REVIEW
    assert finding.target_span is None


def test_review_v2_repeated_term_does_not_reuse_cross_word_span(sample_db):
    service = TermService(sample_db)
    report = review_pair(
        service, "Echo and Echo.", "声骸与回声骸骨。", "zh", review_version=RULE_VERSION_V2
    )
    echoes = [item for item in report.findings if item.source_span.text == "Echo"]
    assert len(echoes) == 2
    first, second = echoes
    assert first.verdict == VERDICT_VERIFIED
    assert first.target_span is not None
    assert (first.target_span.start, first.target_span.end, first.target_span.text) == (
        0,
        2,
        "声骸",
    )
    assert second.verdict == VERDICT_NEEDS_REVIEW
    assert second.target_span is None


@pytest.mark.parametrize(
    ("source", "target", "term", "zh"),
    [
        ("Echo.", "回声骸骨。", "Echo", "声骸"),
        ("Encore.", "平安可贵。", "Encore", "安可"),
    ],
)
def test_review_v2_official_pair_on_cross_word_span_is_conflict(
    sample_db, source, target, term, zh
):
    service = TermService(sample_db)
    baseline = review_pair(
        service, source, target, "zh", review_version=RULE_VERSION_V2
    )
    finding = _finding_for(baseline, term)
    candidate = next(item for item in finding.candidates if item.zh == zh)
    context = {
        "source_revision": baseline.source_revision,
        "rule_version": baseline.rule_version,
        "dictionary_revision": baseline.dictionary.revision,
        "matcher_revision": baseline.matcher_revision,
    }
    resolved = review_pair(
        service,
        source,
        target,
        "zh",
        resolutions=(
            {
                "mention_id": finding.id,
                "choice": "official_pair",
                "candidate_id": candidate.candidate_id,
            },
        ),
        review_version=RULE_VERSION_V2,
        resolution_context=context,
    )
    chosen = next(item for item in resolved.findings if item.id == finding.id)
    assert chosen.verdict == VERDICT_CONFLICT
    assert chosen.target_span is None


@pytest.mark.parametrize(
    ("source", "target", "term", "zh", "span"),
    [
        ("Use Echo.", "使用声骸。", "Echo", "声骸", (2, 4)),
        ("Encore joins.", "安可加入队伍。", "Encore", "安可", (0, 2)),
        ("Echo.", "一声骸。", "Echo", "声骸", (1, 3)),
        ("Echo.", "强大声骸。", "Echo", "声骸", (2, 4)),
        ("Echo.", "声骸。", "Echo", "声骸", (0, 2)),
    ],
)
def test_review_v2_keeps_continuous_target_hits(
    sample_db, source, target, term, zh, span
):
    service = TermService(sample_db)
    report = review_pair(service, source, target, "zh", review_version=RULE_VERSION_V2)
    finding = _finding_for(report, term)
    assert finding.verdict == VERDICT_VERIFIED
    assert finding.target_span is not None
    assert (finding.target_span.start, finding.target_span.end) == span
    assert finding.target_span.text == zh


def test_review_v2_keeps_both_terms_in_one_sentence(sample_db):
    service = TermService(sample_db)
    report = review_pair(
        service, "Encore uses Echo.", "给安可装备声骸。", "zh", review_version=RULE_VERSION_V2
    )
    by_term = {item.source_span.text: item for item in report.findings}
    assert set(by_term) == {"Encore", "Echo"}
    for term, zh in (("Encore", "安可"), ("Echo", "声骸")):
        finding = by_term[term]
        assert finding.verdict == VERDICT_VERIFIED
        assert finding.target_span is not None
        assert finding.target_span.text == zh


def test_review_v2_clipped_region_still_vetoes_on_full_target(sample_db):
    service = TermService(sample_db)
    report = review_pair(
        service,
        "Echo.",
        "回声骸骨。",
        "zh",
        review_version=RULE_VERSION_V2,
        alignments=(
            {
                "source": {"start": 0, "end": 4, "text": "Echo"},
                "target": {"start": 1, "end": 5, "text": "声骸骨。"},
            },
        ),
    )
    finding = _finding_for(report, "Echo")
    assert finding.verdict == VERDICT_NEEDS_REVIEW
    assert finding.target_span is None


def test_review_v2_vetoed_elsewhere_span_does_not_block_review(sample_db):
    service = TermService(sample_db)
    report = review_pair(
        service,
        "Echo.",
        "平安。回声骸骨。",
        "zh",
        review_version=RULE_VERSION_V2,
        alignments=(
            {
                "source": {"start": 0, "end": 4, "text": "Echo"},
                "target": {"start": 0, "end": 3, "text": "平安。"},
            },
        ),
    )
    finding = _finding_for(report, "Echo")
    assert finding.verdict == VERDICT_NEEDS_REVIEW
    assert finding.target_span is None


def test_matcher_revision_changed_and_v1_label_context_is_stale(sample_db):
    legacy_payload = json.dumps(
        ["wuwaterm-cjk-matcher-v1", JIEBA_COMMIT, LEXICON_FILTER, sorted(_WORDS)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    old_hash = hashlib.sha256(legacy_payload).hexdigest()

    service = TermService(sample_db)
    baseline = review_pair(
        service, "Echo.", "使用声骸。", "zh", review_version=RULE_VERSION_V2
    )
    assert HEX64.fullmatch(baseline.matcher_revision or "")
    assert baseline.matcher_revision != old_hash

    finding = _finding_for(baseline, "Echo")
    resolutions = ({"mention_id": finding.id, "choice": "not_a_term"},)
    three_key = {
        "source_revision": baseline.source_revision,
        "rule_version": baseline.rule_version,
        "dictionary_revision": baseline.dictionary.revision,
    }
    with pytest.raises(ReviewRequestError, match="stale") as stale:
        review_pair(
            service,
            "Echo.",
            "使用声骸。",
            "zh",
            resolutions=resolutions,
            review_version=RULE_VERSION_V2,
            resolution_context={**three_key, "matcher_revision": old_hash},
        )
    assert stale.value.code == "invalid_request"

    legacy = review_pair(
        service,
        "Echo.",
        "使用声骸。",
        "zh",
        resolutions=resolutions,
        review_version=RULE_VERSION_V2,
        resolution_context=three_key,
        require_matcher_revision=False,
    )
    kept = next(item for item in legacy.findings if item.id == finding.id)
    assert kept.verdict == VERDICT_NOT_EVALUATED


@pytest.mark.parametrize(
    ("target", "valid"),
    [
        ("香椿加入队伍。", False),
        ("椿树很好看。", False),
        ("这是一棵臭椿。", False),
        ("椿加入队伍。", True),
        ("与椿组队。", True),
        ("椿", True),
    ],
)
def test_review_v2_single_character_target_uses_compound_context(tmp_path, target, valid):
    service = _one_character_service(tmp_path)
    source = "Camellya joins the team."
    automatic = review_pair(service, source, target, "zh", review_version=RULE_VERSION_V2)
    finding = _finding_for(automatic, "Camellya")
    assert finding.verdict == (VERDICT_VERIFIED if valid else VERDICT_NEEDS_REVIEW)
    assert (finding.target_span.text if finding.target_span else None) == ("椿" if valid else None)
    selected = review_pair(
        service, source, target, "zh", review_version=RULE_VERSION_V2,
        resolutions=({"mention_id": finding.id, "choice": "official_pair",
                      "candidate_id": finding.candidates[0].candidate_id},),
        resolution_context={"source_revision": automatic.source_revision,
                            "rule_version": automatic.rule_version,
                            "dictionary_revision": automatic.dictionary.revision,
                            "matcher_revision": automatic.matcher_revision},
    )
    chosen = _finding_for(selected, "Camellya")
    assert chosen.verdict == (VERDICT_VERIFIED if valid else VERDICT_CONFLICT)
    assert (chosen.target_span.text if chosen.target_span else None) == ("椿" if valid else None)
    # Legacy v1 remains frozen, rather than silently acquiring v2 semantics.
    legacy = review_pair(service, source, target, "zh")
    assert _finding_for(legacy, "Camellya").verdict == VERDICT_VERIFIED


def test_review_v2_single_character_compound_elsewhere_is_not_an_occurrence(tmp_path):
    service = _one_character_service(tmp_path)
    source = "Camellya joins the team."
    for target, expected in [
        ("无关。香椿加入队伍。", VERDICT_NEEDS_REVIEW),
        ("无关。椿加入队伍。", VERDICT_NOT_EVALUATED),
    ]:
        report = review_pair(
            service, source, target, "zh", review_version=RULE_VERSION_V2,
            alignments=({"source": {"start": 0, "end": len(source), "text": source},
                         "target": {"start": 0, "end": 3, "text": "无关。"}},),
        )
        assert _finding_for(report, "Camellya").verdict == expected
        assert _finding_for(report, "Camellya").target_span is None


@pytest.mark.parametrize(
    ("target", "start", "end", "valid"),
    [("xEcho", 1, 5, False), ("Echox", 0, 4, False),
     ("xEchoz", 1, 5, False), ("xEchoes", 1, 5, False),
     ("Echo", 0, 4, True), ("x Echo", 2, 6, True),
     ("Echo and more", 0, 4, True)],
)
def test_review_v2_alignment_filters_full_word_matches_without_banning_ranges(
    sample_db, target, start, end, valid
):
    service = TermService(sample_db)
    alignment = ({"source": {"start": 0, "end": 2, "text": "声骸"},
                  "target": {"start": start, "end": end, "text": target[start:end]}},)
    report = review_pair(service, "声骸", target, "en", review_version=RULE_VERSION_V2,
                         alignments=alignment)
    finding = _finding_for(report, "声骸")
    # A substring range is VALID input; it just cannot make Echo a bounded
    # occurrence inside xEcho/Echoes. No invalid_request workaround.
    assert finding.verdict == (VERDICT_VERIFIED if valid else VERDICT_NEEDS_REVIEW)
    assert (finding.target_span.text if finding.target_span else None) == ("Echo" if valid else None)
    selected = review_pair(
        service, "声骸", target, "en", review_version=RULE_VERSION_V2,
        alignments=alignment,
        resolutions=({"mention_id": finding.id, "choice": "official_pair",
                      "candidate_id": finding.candidates[0].candidate_id},),
        resolution_context={"source_revision": report.source_revision,
                            "rule_version": report.rule_version,
                            "dictionary_revision": report.dictionary.revision,
                            "matcher_revision": report.matcher_revision},
    )
    assert _finding_for(selected, "声骸").verdict == (VERDICT_VERIFIED if valid else VERDICT_CONFLICT)


_PLURAL_RECORDS = (
    TermRecord("core_term", "fixture", "resonator", "resonator", "共鸣者", "Resonator"),
    TermRecord("item", "fixture", "echo", "echo", "声骸", "Echo"),
    TermRecord("item", "fixture", "restorative", "restorative", "S型还原素", "Type-S Restorative"),
)


def _plural_service(tmp_path: Path, *extra: TermRecord) -> TermService:
    path = tmp_path / "review-v2-plural.db"
    with connect(path) as conn:
        initialize(conn)
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (("schema_version", "synthetic-v1"), ("source_commit", "fixture-commit")),
        )
        insert_records(conn, (*_PLURAL_RECORDS, *extra))
        conn.commit()
    return TermService(path)


def test_review_v2_english_target_accepts_bounded_plural(tmp_path):
    # The translator spells the official English term in its correct plural, so
    # the term is verified over the whole inflected word, multi-word ones too.
    service = _plural_service(tmp_path)
    report = review_pair(
        service, "交付共鸣者、声骸与S型还原素。",
        "Deliver Resonators, Echoes and Type-S Restoratives.", "en",
        review_version=RULE_VERSION_V2,
    )
    assert {(item.source_span.text, item.verdict, item.target_span.text)
            for item in report.findings} == {
        ("共鸣者", VERDICT_VERIFIED, "Resonators"),
        ("声骸", VERDICT_VERIFIED, "Echoes"),
        ("S型还原素", VERDICT_VERIFIED, "Type-S Restoratives"),
    }


def test_review_v2_english_plural_source_mention_is_verified(tmp_path):
    # The English source term is plural and the Chinese target carries the
    # official pair; the mention spans the inflected word it was read from.
    service = _plural_service(tmp_path)
    source = "Collect and deliver Type-S Restoratives to progress the restoration."
    target = "收集并交付S型还原素以推进修复。"
    report = review_pair(service, source, target, "zh", review_version=RULE_VERSION_V2)
    finding = _finding_for(report, "Type-S Restoratives")
    assert finding.source_span.text == "Type-S Restoratives"
    # The id is the span the report shows, never the dictionary surface: both
    # clients rebuild a mention id from source_span before submitting a
    # resolution, so a plural span must carry the inflected text in its id.
    assert finding.id == (
        f"{finding.source_span.start}:{finding.source_span.end}:"
        f"{finding.source_span.text}"
    )
    assert finding.verdict == VERDICT_VERIFIED
    assert finding.target_span.text == "S型还原素"
    resolved = review_pair(
        service, source, target, "zh",
        review_version=RULE_VERSION_V2,
        resolutions=({"mention_id": finding.id, "choice": "not_a_term"},),
        resolution_context={
            "source_revision": report.source_revision,
            "rule_version": report.rule_version,
            "dictionary_revision": report.dictionary.revision,
            "matcher_revision": report.matcher_revision,
        },
    )
    assert _finding_for(resolved, "Type-S Restoratives").verdict == (
        VERDICT_NOT_EVALUATED
    )


def test_review_v2_plural_dictionary_entries_accept_singular_target(
    tmp_path,
):
    # A Chinese source term is just as often translated in the singular, so a
    # dictionary entry stored in the plural also answers with its singular.
    service = _plural_service(
        tmp_path,
        TermRecord("core_term", "fixture", "waveworn", "waveworn", "海蚀", "Waveworn Phenomena"),
        TermRecord("core_term", "fixture", "main_quest", "main_quest", "潮汐任务", "Main Quests"),
        TermRecord("core_term", "fixture", "companion", "companion", "伴星任务", "Companion Stories"),
    )
    report = review_pair(
        service,
        "完成潮汐任务与伴星任务，调查海蚀。",
        "Complete the Main Quest and Companion Story to investigate the Waveworn Phenomenon.",
        "en",
        review_version=RULE_VERSION_V2,
    )
    assert {
        (item.source_span.text, item.verdict, item.target_span.text)
        for item in report.findings
    } == {
        ("潮汐任务", VERDICT_VERIFIED, "Main Quest"),
        ("伴星任务", VERDICT_VERIFIED, "Companion Story"),
        ("海蚀", VERDICT_VERIFIED, "Waveworn Phenomenon"),
    }


def test_review_v2_english_source_mentions_are_the_sentence_lock_set(tmp_path):
    # The en->zh source is read with exactly the spellings sentence.py locks: a
    # forward plural only. Reversing a plural here invented mentions the
    # translator never saw, so review spent findings on untouched text.
    service = _plural_service(
        tmp_path,
        TermRecord("speaker", "fixture", "mars", "mars", "玛尔斯", "Mars"),
        TermRecord("item", "fixture", "messages", "messages", "讯息", "Messages"),
    )
    source = (
        "Mars briefed the Resonators, then read every Message on the Mar 15 board."
    )
    entries = service.entries()
    locked = select_term_spans(
        source,
        exact_lockable_sources(entries),
        source,
        0,
        allow_plural_inflection=True,
    )
    mentioned = _collect_mentions(
        source, _surface_index(entries), allow_plural_inflection=True
    )
    # Same bounds. The lock span carries the matched text because restore
    # replaces the inflected word whole, while the review mention keeps the
    # dictionary surface so its candidates are the singular entry.
    assert [(span.start, span.end) for span in mentioned] == [
        (span.start, span.end) for span in locked
    ]
    assert [span.source for span in mentioned] == ["Mars", "Resonator"]
    assert [span.source for span in locked] == ["Mars", "Resonators"]
    # The forward plural the lock set admits is a mention; the reversed
    # singulars "Mar" and "Message" the lock set never produces are not.
    assert {span.source for span in mentioned} == {"Mars", "Resonator"}
    assert mentioned[-1].end - mentioned[-1].start == len("Resonators")

    report = review_pair(
        service,
        source,
        "玛尔斯向共鸣者做了简述，然后查看了3月15日板上的讯息。",
        "zh",
        review_version=RULE_VERSION_V2,
    )
    # Two findings, one per locked spelling. "Mar 15" is not a mention of
    # 玛尔斯 and "Message" is not a mention of 讯息, so neither spends one.
    assert {
        (item.source_span.text, item.verdict, item.target_span.text)
        for item in report.findings
    } == {
        ("Mars", VERDICT_VERIFIED, "玛尔斯"),
        ("Resonators", VERDICT_VERIFIED, "共鸣者"),
    }


def test_review_v2_exact_dictionary_plural_beats_synthesized_plural(tmp_path):
    # An independent plural entry keeps the span; another entry's synthesized
    # plural never shadows it.
    service = _plural_service(
        tmp_path,
        TermRecord("item", "fixture", "chorus", "chorus", "群鸣", "Echoes"),
    )
    report = review_pair(
        service, "New Echoes appear.", "新的群鸣出现了。", "zh",
        review_version=RULE_VERSION_V2,
    )
    finding = _finding_for(report, "Echoes")
    assert finding.source_span.text == "Echoes"
    assert finding.candidates[0].zh == "群鸣"
    assert not any(item.source_span.text == "Echo" for item in report.findings)


def test_review_v1_ignores_bounded_english_plurals(tmp_path):
    service = _plural_service(tmp_path)
    target = review_pair(service, "交付共鸣者。", "Deliver Resonators.", "en")
    assert _finding_for(target, "共鸣者").verdict == VERDICT_NEEDS_REVIEW
    assert _finding_for(target, "共鸣者").target_span is None
    source = review_pair(
        service, "Collect Type-S Restoratives.", "收集并交付。", "zh"
    )
    assert source.findings == ()


def test_review_v2_single_character_compounds_are_not_source_mentions(tmp_path):
    # The alignment that makes 欢迎椿 a mention must not turn every occurrence of
    # the character into one: an ordinary word covering it still wins, including
    # the plant names the jieba frequency slice leaves out.
    service = _one_character_service(tmp_path)
    for source in (
        "香椿拌豆腐。",
        "院子里的臭椿长高了。",
        "老椿树发芽了。",
        "春椿茶庄开业了。",
        "椿芽很香。",
        "椿木很硬。",
        "椿皮可以入药。",
    ):
        assert _v2_surfaces(service, source) == set(), source


def test_review_v2_everyday_heart_is_not_a_resonator_finding(tmp_path):
    # 心 is a 3.7 resonator (Hsin) and the ordinary word for heart, mind or
    # core, so widening one-character admission must not turn everyday prose
    # into term constraints. These sentences name nothing, so review reports
    # nothing at all.
    service = _one_character_service(tmp_path)
    for source in ("心很重要。", "她的心很痛。", "和心一起。"):
        report = review_pair(
            service, source, "placeholder", "en", review_version=RULE_VERSION_V2
        )
        assert report.findings == (), source
    # 心情 is an ordinary compound and 心里 is one too; only 今汐 is a mention.
    assert _v2_surfaces(service, "心里想着今汐。") == {"今汐"}
    assert _v2_surfaces(service, "心情很好。") == set()


def test_review_v2_bounded_heart_name_is_still_reviewed(tmp_path):
    # The narrow context rule is a context test, not a ban on the character: a
    # bounded mention of Hsin is still a term constraint with both verdicts.
    service = _one_character_service(tmp_path)
    missing = _finding_for(
        review_pair(
            service, "心加入队伍。", "A joins the party.", "en",
            review_version=RULE_VERSION_V2,
        ),
        "心",
    )
    assert missing.verdict == VERDICT_NEEDS_REVIEW
    assert missing.target_span is None

    found = _finding_for(
        review_pair(
            service, "心加入队伍。", "Hsin joins the party.", "en",
            review_version=RULE_VERSION_V2,
        ),
        "心",
    )
    assert found.verdict == VERDICT_VERIFIED
    assert found.target_span.text == "Hsin"


@pytest.mark.parametrize(
    ("source", "translated", "mistranslated"),
    [
        ("欢迎椿来到黑海岸。", "Welcome Camellya to the Black Shores.",
         "Welcome Chun to the Black Shores."),
        ("快找椿商量对策。", "Find Camellya quickly to discuss the plan.",
         "Find Chun quickly to discuss the plan."),
        ("给椿。", "Give it to Camellya.", "Give it to Chun."),
    ],
)
def test_review_v2_single_character_name_mentions_are_reviewed(
    tmp_path, source, translated, mistranslated
):
    # These are the contexts sentence.py already referenced for the model, while
    # review-v2's narrower whitelist dropped them: a mistranslation produced no
    # finding at all. Both verdicts are asserted so the fix cannot pass by
    # flagging everything.
    service = _one_character_service(tmp_path)
    missing = _finding_for(
        review_pair(service, source, mistranslated, "en", review_version=RULE_VERSION_V2),
        "椿",
    )
    assert missing.verdict == VERDICT_NEEDS_REVIEW
    assert missing.target_span is None

    found = _finding_for(
        review_pair(service, source, translated, "en", review_version=RULE_VERSION_V2),
        "椿",
    )
    assert found.verdict == VERDICT_VERIFIED
    assert found.target_span.text == "Camellya"
