"""segmentation-v1: reference implementation against the shared fixtures."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from wuwaterm import segmentation as seg
from wuwaterm.segmentation import (
    SEGMENT_LIMIT,
    WHITESPACE,
    pair_segments,
    segment_text,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "segmentation-v1.json"
SCRIPT = ROOT / "scripts" / "build_segmentation_fixtures.py"
L = SEGMENT_LIMIT


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_segmentation_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DOC = json.loads(FIXTURES.read_text(encoding="utf-8"))
SEGMENT_CASES = {c["name"]: c for c in DOC["segment_cases"]}
PAIR_CASES = {c["name"]: c for c in DOC["pair_cases"]}


def _spans(segments):
    return [
        (s.start, s.end, s.core_start, s.core_end, s.hard_cut) for s in segments
    ]


def _pairs(pairs):
    return [
        (
            p.status,
            p.hard_cut,
            (p.source.start, p.source.end),
            (p.target.start, p.target.end),
        )
        for p in pairs
    ]


def _case_pairs(name):
    case = PAIR_CASES[name]
    return pair_segments(case["source"], case["target"], L, case["alignments"])


def _check_partition(regions, length):
    pos = 0
    for start, end in regions:
        assert start == pos and end >= start
        pos = end
    assert pos == length


# --- fixture file integrity -------------------------------------------------


def test_fixture_file_is_byte_identical_to_render():
    assert _load_builder().render() == FIXTURES.read_text(encoding="utf-8")


def test_fixture_header_matches_constants():
    assert DOC["version"] == "segmentation-v1"
    assert DOC["limit"] == 1800 == SEGMENT_LIMIT
    assert DOC["hard_cut_window"] == seg.HARD_CUT_WINDOW == 200
    assert DOC["boundary_punctuation"] == "，、；：,;:"
    expected_ws = (
        [0x9, 0xA, 0xB, 0xC, 0xD, 0x20, 0xA0, 0x1680]
        + list(range(0x2000, 0x200B))
        + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
    )
    assert DOC["whitespace"] == sorted(expected_ws)
    assert {ord(c) for c in WHITESPACE} == set(expected_ws)


@pytest.mark.parametrize("case", DOC["segment_cases"], ids=lambda c: c["name"])
def test_segment_case_matches_reference(case):
    got = [s.to_dict() for s in segment_text(case["text"], L)]
    assert got == case["segments"]


@pytest.mark.parametrize("case", DOC["pair_cases"], ids=lambda c: c["name"])
def test_pair_case_matches_reference(case):
    got = [
        p.to_dict()
        for p in pair_segments(case["source"], case["target"], L, case["alignments"])
    ]
    assert got == case["pairs"]


def test_fixture_texts_have_no_stray_control_characters():
    allowed = {"\t", "\r", "\n", "\u2028", "\u2029"}
    texts = [c["text"] for c in DOC["segment_cases"]]
    for c in DOC["pair_cases"]:
        texts += [c["source"], c["target"]]
    for text in texts:
        for ch in text:
            assert ch in allowed or ch.isprintable() or ch in WHITESPACE


# --- invariants over every fixture case --------------------------------------


@pytest.mark.parametrize("case", DOC["segment_cases"], ids=lambda c: c["name"])
def test_segment_invariants(case):
    text = case["text"]
    segments = segment_text(text, L)
    if not text:
        assert segments == []
        return
    _check_partition([(s.start, s.end) for s in segments], len(text))
    assert "".join(text[s.start : s.end] for s in segments) == text
    for s in segments:
        assert s.start <= s.core_start <= s.core_end <= s.end
        assert s.core_end - s.core_start <= L
        core = text[s.core_start : s.core_end]
        if core:
            assert core[0] not in WHITESPACE and core[-1] not in WHITESPACE


@pytest.mark.parametrize("case", DOC["pair_cases"], ids=lambda c: c["name"])
def test_pair_invariants(case):
    source, target = case["source"], case["target"]
    pairs = pair_segments(source, target, L, case["alignments"])
    if not pairs:
        assert not any(c not in WHITESPACE for c in source + target)
        return
    _check_partition([(p.source.start, p.source.end) for p in pairs], len(source))
    _check_partition([(p.target.start, p.target.end) for p in pairs], len(target))
    assert [p.index for p in pairs] == list(range(len(pairs)))
    for p in pairs:
        s_len = p.source.core_end - p.source.core_start
        t_len = p.target.core_end - p.target.core_start
        if p.status == seg.STATUS_READY:
            assert 0 < s_len <= L and 0 < t_len <= L
        elif p.status == seg.STATUS_UNTRANSLATED:
            assert s_len > 0 and t_len == 0
        elif p.status == seg.STATUS_UNPAIRED_TARGET:
            assert s_len == 0 and t_len > 0
        else:
            assert p.status == seg.STATUS_NEEDS_MANUAL_SPLIT


# --- hand-computed expectations ---------------------------------------------


def test_hand_whitespace_beats_punctuation_and_largest_ws():
    # WS at index 1650 and 1700, P at index 1751; all inside the window
    # [1600, 1800] for p. The cut is after the WS at 1700, p = 1701.
    segs = segment_text(SEGMENT_CASES["whitespace_beats_punctuation_largest_ws"]["text"], L)
    assert _spans(segs) == [(0, 1701, 0, 1700, True), (1701, 2001, 1701, 2001, True)]


def test_hand_only_punctuation_picks_largest_p():
    # P after indices 1500 (p=1501, below the window), 1700 and 1751. Largest
    # p in [1600, 1800] is 1752.
    segs = segment_text(SEGMENT_CASES["only_punctuation_largest_p"]["text"], L)
    assert _spans(segs) == [(0, 1752, 0, 1752, True), (1752, 2001, 1752, 2001, True)]


def test_hand_2001_scalars_cut_at_l():
    segs = segment_text("甲" * 2001, L)
    assert _spans(segs) == [(0, 1800, 0, 1800, True), (1800, 2001, 1800, 2001, True)]


def test_hand_limit_boundary():
    assert _spans(segment_text("甲" * 1800, L)) == [(0, 1800, 0, 1800, False)]


def test_hand_astral_cut_is_in_scalars():
    text = "\U0001f600" * 500 + "甲" * 1290 + "，" + "甲" * 210
    assert len(text) == 2001
    # The comma is scalar 1790, so p = 1791. Counted in UTF-16 the comma
    # would sit at 2290, outside the window, and the cut would land at 1800.
    assert _spans(segment_text(text, L)) == [
        (0, 1791, 0, 1791, True),
        (1791, 2001, 1791, 2001, True),
    ]


def test_hand_paragraph_attribution_and_merge():
    text = "  " + "甲" * 1000 + "\n\n" + "乙" * 1000 + "\n  "
    assert _spans(segment_text(text, L)) == [
        (0, 1004, 2, 1002, False),
        (1004, 2007, 1004, 2004, False),
    ]
    five = "\n\n".join(c * 500 for c in "甲乙丙丁戊")
    # Paragraphs 1-3 core 0..1504, adding the 4th would make 2006 > 1800.
    assert _spans(segment_text(five, L)) == [
        (0, 1506, 0, 1504, False),
        (1506, 2508, 1506, 2508, False),
    ]


def test_hand_ws_only_inputs():
    assert segment_text("", L) == []
    assert _spans(segment_text(" \n\t\u3000\r\n", L)) == [(0, 6, 0, 0, False)]
    assert _spans(segment_text("\n\n甲", L)) == [(0, 3, 2, 3, False)]


def test_hand_hard_cut_is_never_merged():
    text = "乙" * 10 + "\n\n" + "甲" * 2001 + "\n\n" + "丙" * 10
    assert _spans(segment_text(text, L)) == [
        (0, 12, 0, 10, False),
        (12, 1812, 12, 1812, True),
        (1812, 2015, 1812, 2013, True),
        (2015, 2025, 2015, 2025, False),
    ]


def test_hand_decimal_point_does_not_split():
    text = "甲" * 700 + "。" + "乙" * 600 + "3.7" + "乙" * 100 + "。" + "丙" * 700 + "。"
    # Sentences end at 701, 1405, 2106; the first two merge (core 1405).
    assert _spans(segment_text(text, L)) == [
        (0, 1405, 0, 1405, False),
        (1405, 2106, 1405, 2106, False),
    ]


def test_hand_pair_statuses():
    r = _pairs(_case_pairs("equal_paragraphs_in_order"))
    assert r == [
        ("ready", False, (0, 1002), (0, 1002)),
        ("ready", False, (1002, 2002), (1002, 2002)),
    ]
    assert [p[0] for p in _pairs(_case_pairs("unequal_paragraphs_short_one_pair"))] == ["ready"]
    assert [p[0] for p in _pairs(_case_pairs("unequal_paragraphs_long_needs_manual_split"))] == [
        "needs_manual_split"
    ]
    assert [p[0] for p in _pairs(_case_pairs("unequal_sentences_need_manual_split"))] == [
        "needs_manual_split"
    ]
    assert [p[0] for p in _pairs(_case_pairs("untranslated_blank_target"))] == ["untranslated"]
    assert [p[0] for p in _pairs(_case_pairs("unpaired_target_blank_source"))] == [
        "unpaired_target"
    ]
    assert _case_pairs("both_blank") == [] and _case_pairs("both_empty") == []
    # Three 700-scalar sentences each side: 1+2 merge (1400), the third stays.
    assert _pairs(_case_pairs("equal_sentences_pair_and_merge")) == [
        ("ready", False, (0, 1400), (0, 1400)),
        ("ready", False, (1400, 2100), (1400, 2100)),
    ]


def test_hand_alignments():
    pairs = _case_pairs("alignments_with_null_target")
    # Source regions [0,102) [102,204) [204,305); the target is resolved from
    # the right so the null alignment owns the empty region [102,102).
    assert _pairs(pairs) == [
        ("ready", False, (0, 102), (0, 102)),
        ("untranslated", False, (102, 204), (102, 102)),
        ("ready", False, (204, 305), (102, 203)),
    ]
    assert _pairs(_case_pairs("alignments_leave_text_outside")) == [
        ("needs_manual_split", False, (0, 305), (0, 203))
    ]
    assert [p[0] for p in _pairs(_case_pairs("alignments_leave_target_text_outside"))] == [
        "needs_manual_split"
    ]


def test_hand_fallback_tie_takes_smaller_offset():
    # Source core length 2000, k=2, t_1 = 1000. Commas after indices 990 and
    # 1008 give boundaries 991 and 1009, both 9 away: the smaller wins.
    # Target length 600, t_1 = 300, comma after index 300 gives p = 301.
    assert _pairs(_case_pairs("fallback_tie_takes_smaller_offset")) == [
        ("ready", True, (0, 991), (0, 301)),
        ("ready", True, (991, 2000), (301, 600)),
    ]


def test_hand_asymmetric_cases():
    for name in (
        "asymmetric_source_over_limit_no_boundary",
        "asymmetric_target_over_limit_no_boundary",
    ):
        pairs = _case_pairs(name)
        assert [p.status for p in pairs] == ["needs_manual_split"]
        assert not [p for p in pairs if p.status == "ready"]

    assert _pairs(_case_pairs("asymmetric_source_over_limit_with_commas")) == [
        ("ready", True, (0, 1001), (0, 401)),
        ("ready", True, (1001, 2001), (401, 800)),
    ]
    assert _pairs(_case_pairs("asymmetric_target_over_limit_with_commas")) == [
        ("ready", True, (0, 401), (0, 1001)),
        ("ready", True, (401, 800), (1001, 2001)),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "asymmetric_source_over_limit_no_boundary",
        "asymmetric_target_over_limit_no_boundary",
        "asymmetric_source_over_limit_with_commas",
        "asymmetric_target_over_limit_with_commas",
    ],
)
def test_asymmetric_ready_pairs_fit_both_sides(name):
    for p in _case_pairs(name):
        if p.status == "ready":
            assert p.source.core_end - p.source.core_start <= L
            assert p.target.core_end - p.target.core_start <= L


def test_hand_fallback_between_short_pairs_does_not_merge():
    pairs = _case_pairs("oversize_sentence_between_short_ones")
    assert [(p.status, p.hard_cut) for p in pairs] == [
        ("ready", False),
        ("ready", True),
        ("ready", True),
        ("ready", False),
    ]


# --- input validation and behaviour ------------------------------------------


def _al(source, target, s, t=None):
    return {
        "source": {"start": s[0], "end": s[1], "text": source[s[0] : s[1]]},
        "target": None
        if t is None
        else {"start": t[0], "end": t[1], "text": target[t[0] : t[1]]},
    }


def test_invalid_alignments_raise():
    src, tgt = "甲乙。丙丁。", "AB。CD。"
    ok = _al(src, tgt, (0, 3), (0, 3))
    bad = [
        "nope",
        [{"source": ok["source"]}],
        [{**ok, "extra": 1}],
        [{"source": {"start": 0, "end": 3}, "target": None}],
        [{"source": {"start": 0, "end": 99, "text": "x"}, "target": None}],
        [{"source": {"start": 2, "end": 2, "text": ""}, "target": None}],
        [{"source": {"start": 3, "end": 2, "text": ""}, "target": None}],
        [{"source": {"start": 0, "end": 3, "text": "错误。"}, "target": None}],
        [{"source": {"start": True, "end": 3, "text": src[1:3]}, "target": None}],
        [ok, _al(src, tgt, (2, 5), (3, 6))],  # source overlap
        [_al(src, tgt, (3, 6), (3, 6)), ok],  # source out of order
        [ok, _al(src, tgt, (3, 6), (2, 5))],  # target overlap
    ]
    for alignments in bad:
        with pytest.raises(ValueError):
            pair_segments(src, tgt, L, alignments)


def test_empty_alignments_fall_back_to_paragraphs():
    assert pair_segments("甲", "乙", L, []) == pair_segments("甲", "乙", L)


def test_final_assertion_survives_python_optimise():
    # The check is an explicit raise, not an assert statement.
    with pytest.raises(AssertionError):
        seg._check_partition([(0, 2), (3, 4)], 4, "x")
    with pytest.raises(AssertionError):
        seg._check_partition([(0, 2)], 4, "x")
    bad = seg.SegmentPair(
        0, "ready", False, seg.Region(0, 1, 0, 1), seg.Region(0, 1, 0, 0)
    )
    with pytest.raises(AssertionError):
        seg._validate_pairs("a", "b", [bad], L)


def test_to_dict_key_order():
    s = segment_text("甲", L)[0].to_dict()
    assert list(s) == ["start", "end", "core_start", "core_end", "hard_cut"]
    p = pair_segments("甲", "乙", L)[0].to_dict()
    assert list(p) == ["index", "status", "hard_cut", "source", "target"]
    assert list(p["source"]) == ["start", "end", "core_start", "core_end"]


def test_smaller_limit_parameter_is_honoured():
    segs = segment_text("甲" * 25, 10)
    assert all(s.core_end - s.core_start <= 10 for s in segs)
    assert segs[0].end == 10
