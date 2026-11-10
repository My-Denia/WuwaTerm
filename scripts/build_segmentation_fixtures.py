#!/usr/bin/env python3
"""Build the segmentation-v1 shared fixtures and byte-compare them.

`contracts/segmentation-v1.json` is the cross-implementation contract for
`docs/segmentation-v1.md`. Every text in it is synthetic. The expected values
are produced by the reference implementation `wuwaterm.segmentation`;
`tests/test_segmentation.py` also pins the key cases by hand.

Usage:
    python scripts/build_segmentation_fixtures.py            # verify
    python scripts/build_segmentation_fixtures.py --write    # refresh
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "contracts" / "segmentation-v1.json"
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.segmentation import (  # noqa: E402
    BOUNDARY_PUNCTUATION,
    HARD_CUT_WINDOW,
    SEGMENT_LIMIT,
    WHITESPACE,
    pair_segments,
    segment_text,
)


def _seg(name: str, description: str, text: str) -> dict:
    return {
        "name": name,
        "description": description,
        "text": text,
        "segments": [s.to_dict() for s in segment_text(text, SEGMENT_LIMIT)],
    }


def _pair(
    name: str, description: str, source: str, target: str, alignments: list | None = None
) -> dict:
    return {
        "name": name,
        "description": description,
        "source": source,
        "target": target,
        "alignments": alignments,
        "pairs": [
            p.to_dict() for p in pair_segments(source, target, SEGMENT_LIMIT, alignments)
        ],
    }


def _span(text: str, start: int, end: int) -> dict:
    return {"start": start, "end": end, "text": text[start:end]}


def _segment_cases() -> list[dict]:
    cases: list[dict] = []

    def add(name: str, description: str, text: str) -> None:
        cases.append(_seg(name, description, text))

    add("empty", "Empty text yields no segments.", "")
    add(
        "all_whitespace",
        "All-WS text is one segment with an empty core at its start.",
        " \n\t\u3000\r\n",
    )
    add(
        "leading_trailing_whitespace_kept",
        "Leading WS belongs to the first segment and the blank line to the segment before it.",
        "  " + "甲" * 1000 + "\n\n" + "乙" * 1000 + "\n  ",
    )
    add(
        "crlf_is_not_a_paragraph_break",
        "A single CRLF is no paragraph break; it only ends a sentence, and the halves cannot merge.",
        "甲" * 1000 + "\r\n" + "乙" * 1000,
    )
    add(
        "crlf_paragraph",
        "CRLF CRLF is a paragraph break and the break stays with the first segment.",
        "甲" * 1000 + "\r\n\r\n" + "乙" * 1000,
    )
    add(
        "ideographic_space_paragraph",
        "Newline, U+3000, newline is a paragraph break.",
        "甲" * 1000 + "\n\u3000\n" + "乙" * 1000,
    )
    add(
        "line_separator_paragraph",
        "Newline, U+2029, newline is a paragraph break because U+2029 is WS.",
        "甲" * 1000 + "\n\u2029\n" + "乙" * 1000,
    )
    add(
        "greedy_merge_paragraphs",
        "Short paragraphs merge left to right while the joined core stays within L.",
        "\n\n".join(c * 500 for c in "甲乙丙丁戊"),
    )
    add(
        "sentence_split_keeps_decimal",
        "An oversized paragraph is cut by sentence and 3.7 does not end a sentence.",
        "甲" * 700 + "。" + "乙" * 600 + "3.7" + "乙" * 100 + "。" + "丙" * 700 + "。",
    )
    add(
        "exactly_limit_is_one_segment",
        "A core of exactly L scalars is not cut.",
        "甲" * 1800,
    )
    add(
        "one_over_limit_hard_cut",
        "L+1 scalars without any boundary are cut at the L-th scalar.",
        "甲" * 1801,
    )
    add(
        "no_boundary_2001",
        "2001 scalars with no boundary are cut at scalar 1800 and both pieces are hard cuts.",
        "甲" * 2001,
    )
    add(
        "whitespace_beats_punctuation_largest_ws",
        "Whitespace wins over a later punctuation mark and the largest WS in the window is used.",
        "甲" * 1650 + " " + "甲" * 49 + " " + "甲" * 50 + "，" + "甲" * 249,
    )
    add(
        "only_punctuation_largest_p",
        "Without WS in the window the largest P in the window is used.",
        "甲" * 1500 + "；" + "甲" * 199 + "，" + "甲" * 50 + "、" + "甲" * 249,
    )
    add(
        "ideographic_space_is_whitespace",
        "U+3000 counts as WS for the hard-cut window.",
        "甲" * 1700 + "\u3000" + "甲" * 300,
    )
    add(
        "astral_characters_count_as_one",
        "Astral scalars before the cut count once; a UTF-16 implementation lands elsewhere.",
        "\U0001f600" * 500 + "甲" * 1290 + "，" + "甲" * 210,
    )
    add(
        "hard_cut_never_merges",
        "Hard-cut pieces are not joined to a short neighbour on either side.",
        "乙" * 10 + "\n\n" + "甲" * 2001 + "\n\n" + "丙" * 10,
    )
    add(
        "trailing_whitespace_run_not_a_segment",
        "A trailing blank-line run has no non-WS after it, so it stays in the last segment.",
        "甲" * 10 + "\n\n   \n\n",
    )
    add(
        "leading_blank_lines_not_a_segment",
        "A leading blank-line run stays in the first segment.",
        "\n\n甲",
    )
    add(
        "empty_core_piece_joins_previous",
        "A hard-cut piece made only of WS becomes a separator of the piece before it.",
        "甲" + " " * 4000 + "乙",
    )
    return cases


def _pair_cases() -> list[dict]:
    cases: list[dict] = []

    def add(
        name: str,
        description: str,
        source: str,
        target: str,
        alignments: list | None = None,
    ) -> None:
        cases.append(_pair(name, description, source, target, alignments))

    add(
        "equal_paragraphs_in_order",
        "Equal paragraph counts pair in order; the pairs are too long to merge.",
        "甲" * 1000 + "\n\n" + "乙" * 1000,
        "丙" * 1000 + "\n\n" + "丁" * 1000,
    )
    add(
        "unequal_paragraphs_short_one_pair",
        "Unequal paragraph counts with both sides within L give one ready pair.",
        "甲" * 300 + "\n\n" + "乙" * 300,
        "丙" * 200 + "\n\n" + "丁" * 200 + "\n\n" + "戊" * 200,
    )
    add(
        "unequal_paragraphs_long_needs_manual_split",
        "Unequal paragraph counts with a side over L give one needs_manual_split pair.",
        "甲" * 1000 + "\n\n" + "乙" * 1000 + "\n\n" + "丙" * 100,
        "丁" * 1500,
    )
    add(
        "equal_sentences_pair_and_merge",
        "Equal sentence counts pair by index, then ready pairs merge up to L.",
        ("甲" * 699 + "。") * 3,
        ("乙" * 699 + "。") * 3,
    )
    add(
        "unequal_sentences_need_manual_split",
        "Unequal sentence counts give one needs_manual_split pair for the paragraph.",
        ("甲" * 699 + "。") * 3,
        ("乙" * 1049 + "。") * 2,
    )
    add(
        "untranslated_blank_target",
        "A blank target gives an untranslated pair.",
        "甲" * 10,
        "  \n",
    )
    add(
        "unpaired_target_blank_source",
        "A blank source with a non-blank target gives an unpaired_target pair.",
        " ",
        "乙" * 10,
    )
    add("both_blank", "Both sides blank give no pairs.", "  ", "\n")
    add("both_empty", "Both sides empty give no pairs.", "", "")

    source = "甲" * 100 + "。" + " " + "乙" * 100 + "。" + " " + "丙" * 100 + "。"
    target = "子" * 100 + "。" + " " + "寅" * 100 + "。"
    alignments = [
        {"source": _span(source, 0, 101), "target": _span(target, 0, 101)},
        {"source": _span(source, 102, 203), "target": None},
        {"source": _span(source, 204, 305), "target": _span(target, 102, 203)},
    ]
    add(
        "alignments_with_null_target",
        "Alignments define the pairs; a null target owns an empty region and is untranslated.",
        source,
        target,
        alignments,
    )
    add(
        "alignments_leave_text_outside",
        "Non-WS source text outside every alignment span makes the whole document one manual split.",
        source,
        target,
        alignments[:2],
    )
    add(
        "alignments_leave_target_text_outside",
        "Non-WS target text outside every alignment span makes the whole document one manual split.",
        source,
        target,
        [
            {"source": _span(source, 0, 101), "target": _span(target, 0, 101)},
            {"source": _span(source, 102, 203), "target": None},
            {"source": _span(source, 204, 305), "target": None},
        ],
    )
    long_source = ("甲" * 699 + "。") * 3
    long_target = ("乙" * 699 + "。") * 3
    add(
        "alignment_region_over_limit_splits_by_sentence",
        "An oversized aligned region pair is cut by sentence when the counts are equal.",
        long_source,
        long_target,
        [
            {
                "source": _span(long_source, 0, len(long_source)),
                "target": _span(long_target, 0, len(long_target)),
            }
        ],
    )

    add(
        "fallback_tie_takes_smaller_offset",
        "Two source boundaries equidistant from t_1: the smaller offset wins.",
        "甲" * 990 + "，" + "甲" * 17 + "，" + "甲" * 991,
        "乙" * 300 + "，" + "乙" * 299,
    )
    add(
        "asymmetric_source_over_limit_no_boundary",
        "Source 2001 and target 1000 without boundaries: needs_manual_split, nothing ready.",
        "甲" * 2001,
        "乙" * 1000,
    )
    add(
        "asymmetric_target_over_limit_no_boundary",
        "Source 1000 and target 2001 without boundaries: needs_manual_split, nothing ready.",
        "甲" * 1000,
        "乙" * 2001,
    )
    add(
        "asymmetric_source_over_limit_with_commas",
        "Source over L and a shorter target, both with a comma near the middle: two hard-cut ready pairs.",
        "甲" * 1000 + "，" + "甲" * 1000,
        "乙" * 400 + "，" + "乙" * 399,
    )
    add(
        "asymmetric_target_over_limit_with_commas",
        "Target over L and a shorter source, both with a comma near the middle: two hard-cut ready pairs.",
        "甲" * 400 + "，" + "甲" * 399,
        "乙" * 1000 + "，" + "乙" * 1000,
    )
    add(
        "fallback_one_side_without_boundary",
        "Source has a boundary near t_1 but the target has none: needs_manual_split.",
        "甲" * 1000 + "，" + "甲" * 1000,
        "乙" * 800,
    )
    add(
        "oversize_sentence_between_short_ones",
        "A fallback-cut sentence pair sits between short pairs and none of them merges across it.",
        "甲" * 100 + "。" + "甲" * 1000 + "，" + "甲" * 998 + "。" + "甲" * 100 + "。",
        "乙" * 100 + "。" + "乙" * 350 + "，" + "乙" * 348 + "。" + "乙" * 100 + "。",
    )
    return cases


def render() -> str:
    doc = {
        "version": "segmentation-v1",
        "rules": "docs/segmentation-v1.md",
        "limit": SEGMENT_LIMIT,
        "whitespace": sorted(ord(ch) for ch in WHITESPACE),
        "boundary_punctuation": BOUNDARY_PUNCTUATION,
        "hard_cut_window": HARD_CUT_WINDOW,
        "segment_cases": _segment_cases(),
        "pair_cases": _pair_cases(),
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the fixtures")
    args = parser.parse_args(argv)

    generated = render()
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(generated, encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(ROOT).as_posix()}")
        return 0
    if not SNAPSHOT.is_file():
        print(f"missing fixtures: {SNAPSHOT}", file=sys.stderr)
        return 1
    if SNAPSHOT.read_text(encoding="utf-8") != generated:
        print(
            "segmentation fixtures drift: run `python "
            "scripts/build_segmentation_fixtures.py --write` and review the diff.",
            file=sys.stderr,
        )
        return 1
    print("segmentation fixtures ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
