"""Review-v2 frozen coverage, alignment, and freshness contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from wuwaterm.application import project_review_report, review_pair
from wuwaterm.cjk_span import JIEBA_COMMIT, LEXICON_FILTER, _WORDS
from wuwaterm.db import connect, initialize, insert_records
from wuwaterm.lookup import TermService
from wuwaterm.models import TermRecord
from wuwaterm.review import (
    RULE_VERSION_V2,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
    ReviewRequestError,
    _sentence_ranges,
    _sentence_ranges_v2,
)

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


@pytest.mark.parametrize("case_id", ("merge", "split", "unmatched"))
def test_frozen_mismatched_counts_without_mapping_are_unassessed(v2_service, case_id):
    report = _v2(v2_service, case_id)
    findings = _term_findings(report)

    assert [item.verdict for item in findings] == [
        VERDICT_NOT_EVALUATED,
        VERDICT_NOT_EVALUATED,
    ]
    assert report.coverage.evaluated == 0
    assert report.coverage.not_evaluated == 3
    assert all(item.target_span is None for item in findings)


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
    assert _v2_surfaces(service, "欢迎椿") == set()
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
