"""Contract tests for the shared free-text lock exclusion metadata parsers.

F10 extracted the two strict parsers into ``wuwaterm.exclusion_metadata`` so
the sentence translator and the review engine read one validation contract.
This file pins that shared-module contract itself: the versioned value shape,
the exact-integer rule, the per-key script restrictions, and the frozen
exclusion-rule boundary. Runtime-locking behavior of the metadata is owned by
tests/test_sentence.py; the review-side fork is owned by
tests/test_review_autoterm_exclusions.py.
"""

from __future__ import annotations

import json

import pytest

from wuwaterm import exclusion_metadata, sentence
from wuwaterm.constants import (
    FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_MIN_OCCURRENCES,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
)

EN_BASE_ROWS = (("Discord", 12, 2), ("It", 12, 2))
ZH_BASE_ROWS = (("大人", 12, 2), ("祂", 12, 2))


def _en_value(rows, **overrides):
    value = {
        "version": FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
        "min_occurrences": FREE_TEXT_LOCK_MIN_OCCURRENCES,
        "max_aligned_ratio": list(FREE_TEXT_LOCK_MAX_ALIGNED_RATIO),
        "surfaces": [list(row) for row in rows],
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _zh_value(rows, **overrides):
    value = {
        "version": FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
        "min_occurrences": FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
        "max_aligned_ratio": list(FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO),
        "surfaces": [list(row) for row in rows],
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


EN_PARSER = exclusion_metadata.parse_free_text_lock_exclusions
ZH_PARSER = exclusion_metadata.parse_free_text_zh_lock_exclusions


def test_english_parser_returns_exact_sorted_rows():
    rows = EN_PARSER(_en_value((("Discord", 12, 2), ("Fish", 12, 2), ("It", 12, 2), ("Tea", 12, 2))))
    assert rows == (("Discord", 12, 2), ("Fish", 12, 2), ("It", 12, 2), ("Tea", 12, 2))
    assert all(
        type(row) is tuple
        and type(row[0]) is str
        and type(row[1]) is int
        and type(row[2]) is int
        for row in rows
    )


def test_zh_parser_returns_exact_sorted_rows():
    rows = ZH_PARSER(_zh_value(ZH_BASE_ROWS))
    assert rows == (("大人", 12, 2), ("祂", 12, 2))
    assert all(
        type(row) is tuple
        and type(row[0]) is str
        and type(row[1]) is int
        and type(row[2]) is int
        for row in rows
    )


def test_empty_surface_lists_are_valid_for_both_keys():
    assert EN_PARSER(_en_value(())) == ()
    assert ZH_PARSER(_zh_value(())) == ()


@pytest.mark.parametrize(
    "value",
    ("", "not json", "[]", "null", '"text"', "42"),
)
@pytest.mark.parametrize("parser", (EN_PARSER, ZH_PARSER), ids=("en", "zh"))
def test_unreadable_json_is_rejected(parser, value):
    with pytest.raises(ValueError):
        parser(value)


def test_english_parser_enforces_its_own_version_and_parameters():
    with pytest.raises(ValueError, match="unsupported exclusion version"):
        EN_PARSER(_en_value(EN_BASE_ROWS, version=FREE_TEXT_LOCK_EXCLUSIONS_VERSION + 1))
    with pytest.raises(ValueError, match="min_occurrences mismatch"):
        EN_PARSER(_en_value(EN_BASE_ROWS, min_occurrences=FREE_TEXT_LOCK_MIN_OCCURRENCES + 1))
    with pytest.raises(ValueError, match="max_aligned_ratio mismatch"):
        EN_PARSER(_en_value(EN_BASE_ROWS, max_aligned_ratio=[1, 4]))
    with pytest.raises(ValueError, match="max_aligned_ratio mismatch"):
        EN_PARSER(_en_value(EN_BASE_ROWS, max_aligned_ratio=[5, 1]))


def test_zh_parser_enforces_its_own_version_and_parameters():
    with pytest.raises(ValueError, match="unsupported zh exclusion version"):
        ZH_PARSER(_zh_value(ZH_BASE_ROWS, version=FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION + 1))
    with pytest.raises(ValueError, match="min_occurrences mismatch"):
        ZH_PARSER(_zh_value(ZH_BASE_ROWS, min_occurrences=FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES + 1))
    with pytest.raises(ValueError, match="max_aligned_ratio mismatch"):
        ZH_PARSER(_zh_value(ZH_BASE_ROWS, max_aligned_ratio=[1, 4]))
    with pytest.raises(ValueError, match="max_aligned_ratio mismatch"):
        ZH_PARSER(_zh_value(ZH_BASE_ROWS, max_aligned_ratio=[5, 1]))


EXACT_INT_REJECTIONS = (
    ("en-version-bool", EN_PARSER, _en_value(EN_BASE_ROWS, version=True)),
    ("en-version-float", EN_PARSER, _en_value(EN_BASE_ROWS, version=1.0)),
    ("en-min-occurrences-bool", EN_PARSER, _en_value(EN_BASE_ROWS, min_occurrences=True)),
    ("en-min-occurrences-float", EN_PARSER, _en_value(EN_BASE_ROWS, min_occurrences=10.0)),
    ("en-ratio-bool-numerator", EN_PARSER, _en_value(EN_BASE_ROWS, max_aligned_ratio=[True, 5])),
    ("en-ratio-float-numerator", EN_PARSER, _en_value(EN_BASE_ROWS, max_aligned_ratio=[1.0, 5])),
    ("en-row-occurrences-bool", EN_PARSER, _en_value([("Wren", True, 0)])),
    ("en-row-occurrences-float", EN_PARSER, _en_value([("Wren", 12.0, 0)])),
    ("en-row-aligned-bool", EN_PARSER, _en_value([("Wren", 12, True)])),
    ("en-row-aligned-float", EN_PARSER, _en_value([("Wren", 12, 0.0)])),
    ("zh-version-bool", ZH_PARSER, _zh_value(ZH_BASE_ROWS, version=True)),
    ("zh-version-float", ZH_PARSER, _zh_value(ZH_BASE_ROWS, version=1.0)),
    ("zh-min-occurrences-bool", ZH_PARSER, _zh_value(ZH_BASE_ROWS, min_occurrences=True)),
    ("zh-min-occurrences-float", ZH_PARSER, _zh_value(ZH_BASE_ROWS, min_occurrences=10.0)),
    ("zh-ratio-bool-numerator", ZH_PARSER, _zh_value(ZH_BASE_ROWS, max_aligned_ratio=[True, 5])),
    ("zh-ratio-float-numerator", ZH_PARSER, _zh_value(ZH_BASE_ROWS, max_aligned_ratio=[1.0, 5])),
    ("zh-row-occurrences-bool", ZH_PARSER, _zh_value([("大人", True, 0)])),
    ("zh-row-occurrences-float", ZH_PARSER, _zh_value([("大人", 12.0, 0)])),
    ("zh-row-aligned-bool", ZH_PARSER, _zh_value([("大人", 12, True)])),
    ("zh-row-aligned-float", ZH_PARSER, _zh_value([("大人", 12, 0.0)])),
)


@pytest.mark.parametrize(("label", "parser", "value"), EXACT_INT_REJECTIONS, ids=[item[0] for item in EXACT_INT_REJECTIONS])
def test_integer_slots_reject_bools_and_floats(label, parser, value):
    # bool is an int subclass; the metadata contract accepts exact ints only.
    with pytest.raises(ValueError):
        parser(value)


def test_english_surfaces_must_be_single_line_latin():
    with pytest.raises(ValueError, match="single-line Latin"):
        EN_PARSER(_en_value([("大人", 12, 0)]))  # CJK is never an English surface
    with pytest.raises(ValueError, match="single-line Latin"):
        EN_PARSER(_en_value([("Ω", 12, 0)]))  # non-Latin letters do not qualify
    with pytest.raises(ValueError, match="single-line Latin"):
        EN_PARSER(_en_value([("123", 12, 0)]))  # digits carry no letter
    with pytest.raises(ValueError, match="single-line Latin"):
        EN_PARSER(_en_value([("W\nX", 12, 0)]))  # single-line only
    assert EN_PARSER(_en_value([("Fish", 12, 2)])) == (("Fish", 12, 2),)
    assert EN_PARSER(_en_value([("café", 12, 0)])) == (("café", 12, 0),)  # accented Latin stays valid


def test_zh_surfaces_must_be_single_line_cjk_without_latin_requirement():
    with pytest.raises(ValueError, match="single-line CJK"):
        ZH_PARSER(_zh_value([("English", 12, 0)]))  # pure Latin is not a zh surface
    with pytest.raises(ValueError, match="single-line CJK"):
        ZH_PARSER(_zh_value([("···", 12, 0)]))  # punctuation alone carries no han character
    with pytest.raises(ValueError, match="single-line CJK"):
        ZH_PARSER(_zh_value([("大\n人", 12, 0)]))  # single-line only
    assert ZH_PARSER(_zh_value([("（大人）", 12, 2)])) == (("（大人）", 12, 2),)  # brackets around han stay valid
    assert ZH_PARSER(_zh_value([("3个大人", 12, 2)])) == (("3个大人", 12, 2),)  # digits around han stay valid


@pytest.mark.parametrize(
    ("builder", "parser", "surfaces"),
    (
        (_en_value, EN_PARSER, ["Wren"]),  # a row must be a list
        (_en_value, EN_PARSER, [["Wren", 12]]),  # short row
        (_en_value, EN_PARSER, [["Wren", 12, 0, "x"]]),  # long row
        (_en_value, EN_PARSER, [[12, 12, 0]]),  # surface must be a string
        (_en_value, EN_PARSER, "Wren"),  # surfaces must be a list
        (_zh_value, ZH_PARSER, ["大人"]),
        (_zh_value, ZH_PARSER, [["大人", 12]]),
        (_zh_value, ZH_PARSER, [["大人", 12, 0, "x"]]),
        (_zh_value, ZH_PARSER, [[12, 12, 0]]),
        (_zh_value, ZH_PARSER, "大人"),
    ),
    ids=(
        "en-row-not-list",
        "en-row-short",
        "en-row-long",
        "en-surface-not-str",
        "en-surfaces-not-list",
        "zh-row-not-list",
        "zh-row-short",
        "zh-row-long",
        "zh-surface-not-str",
        "zh-surfaces-not-list",
    ),
)
def test_row_and_object_shapes_are_strict(builder, parser, surfaces):
    with pytest.raises(ValueError):
        parser(builder((), surfaces=surfaces))


@pytest.mark.parametrize("parser", (EN_PARSER, ZH_PARSER), ids=("en", "zh"))
def test_object_with_missing_or_extra_keys_is_rejected(parser):
    with pytest.raises(ValueError):
        parser(json.dumps({"version": 1, "surfaces": []}))  # missing keys
    with pytest.raises(ValueError):
        parser(_en_value((), extra=1))  # extra key


def test_row_counts_must_meet_the_frozen_exclusion_rule():
    # The builder's rule is strict: occurrences >= 10 and aligned * 5 < occurrences.
    assert FREE_TEXT_LOCK_MIN_OCCURRENCES == 10
    assert FREE_TEXT_LOCK_MAX_ALIGNED_RATIO == (1, 5)
    with pytest.raises(ValueError, match="exclusion rule"):
        EN_PARSER(_en_value([("Wren", 9, 0)]))  # below the minimum count
    with pytest.raises(ValueError, match="exclusion rule"):
        EN_PARSER(_en_value([("Wren", 10, 2)]))  # 2 * 5 == 10: not strictly below
    with pytest.raises(ValueError, match="exclusion rule"):
        EN_PARSER(_en_value([("Wren", 10, -1)]))  # aligned is a non-negative count
    with pytest.raises(ValueError, match="exclusion rule"):
        EN_PARSER(_en_value([("Wren", 10, 10)]))  # aligned cannot exceed occurrences
    assert EN_PARSER(_en_value([("Wren", 10, 0)])) == (("Wren", 10, 0),)
    assert EN_PARSER(_en_value([("Wren", 10, 1)])) == (("Wren", 10, 1),)  # 5 < 10
    assert EN_PARSER(_en_value([("Wren", 11, 2)])) == (("Wren", 11, 2),)  # 10 < 11


def test_zh_row_counts_must_meet_the_frozen_exclusion_rule():
    with pytest.raises(ValueError, match="exclusion rule"):
        ZH_PARSER(_zh_value([("大人", 9, 0)]))
    with pytest.raises(ValueError, match="exclusion rule"):
        ZH_PARSER(_zh_value([("大人", 10, 2)]))  # 2 * 5 == 10: not strictly below
    assert ZH_PARSER(_zh_value([("大人", 10, 1)])) == (("大人", 10, 1),)
    assert ZH_PARSER(_zh_value([("大人", 11, 2)])) == (("大人", 11, 2),)


def test_surfaces_must_be_strictly_ascending_and_unique():
    with pytest.raises(ValueError, match="unique and sorted"):
        EN_PARSER(_en_value([("Tea", 12, 0), ("Fish", 12, 0)]))  # descending
    with pytest.raises(ValueError, match="unique and sorted"):
        EN_PARSER(_en_value([("Fish", 12, 0), ("Fish", 12, 0)]))  # duplicate
    with pytest.raises(ValueError, match="unique and sorted"):
        ZH_PARSER(_zh_value([("祂", 12, 0), ("大人", 12, 0)]))
    # Ordering is exact string order; "FISH" sorts before "Fish".
    assert EN_PARSER(_en_value([("FISH", 12, 0), ("Fish", 12, 0)])) == (
        ("FISH", 12, 0),
        ("Fish", 12, 0),
    )


def test_sentence_module_reexports_the_shared_parser_objects():
    assert sentence.parse_free_text_lock_exclusions is EN_PARSER
    assert sentence.parse_free_text_zh_lock_exclusions is ZH_PARSER
    assert sentence.parse_free_text_zh_lock_exclusions(_zh_value([("大人", 12, 2)])) == (
        ("大人", 12, 2),
    )
