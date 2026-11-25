"""Tests for pure stdlib structural verification and regrouping helpers."""

from __future__ import annotations

import secrets

from wuwaterm.structural_checks import (
    ParagraphStructureReport,
    check_paragraph_structure,
    check_placeholder_integrity,
    cumulative_cuts,
    extract_numbers,
    extract_placeholders,
    has_text,
    line_scalar_counts,
    line_share_ratios,
    lines_merged_and_split,
    merged_line_indices,
    missing_numbers,
    paragraph_line_counts,
    regroup_lines_into_source_paragraphs,
    split_line_indices,
)


def test_paragraph_structure_report_and_regrouping():
    assert has_text("  a  ") is True
    assert has_text(" \u3000\t ") is False
    assert cumulative_cuts([2, 1, 3]) == frozenset({2, 3})
    assert line_scalar_counts("ab c\n\nde") == [3, 2]
    assert paragraph_line_counts("a\nb\n\nc") == [2, 1]

    source = "第一段。\n第二句。\n\n第三段。"
    collapsed = "First paragraph sentence.\nSecond sentence here.\nThird paragraph."
    report = check_paragraph_structure(source, collapsed)
    assert isinstance(report, ParagraphStructureReport)
    assert report.source_counts == (2, 1)
    assert report.answer_counts == (3,)
    assert report.paragraphs_match is False
    assert report.lines_match is True
    assert report.crossing_cuts is False
    assert report.merged_and_split is False
    assert report.regroupable is True

    regrouped = regroup_lines_into_source_paragraphs(source, collapsed)
    assert paragraph_line_counts(regrouped) == [2, 1]


def test_crossing_cuts_and_merge_split_block_regrouping():
    crossing = check_paragraph_structure("a1\n\na2\na3\n\nb1", "A1\nA2\n\nA3\nB1")
    assert crossing.crossing_cuts is True
    assert crossing.regroupable is False

    ratios = line_share_ratios([20, 20, 20, 20], [38, 8, 17, 17])
    assert merged_line_indices([20, 20, 20, 20], [38, 8, 17, 17], ratios) == [0]
    assert 1 in split_line_indices(ratios)
    assert (
        lines_merged_and_split(
            "a" * 20 + "\n" + "b" * 20 + "\n" + "c" * 20 + "\n" + "d" * 20,
            "A" * 38 + "\n" + "B" * 8 + "\n" + "C" * 17 + "\n" + "D" * 17,
        )
        is True
    )


def test_number_extraction_and_missing_numbers():
    assert extract_numbers("Version 3.7.8 gives 1,000 Astrite and 50% Crit") == (
        "3.7.8",
        "1,000",
        "50",
    )
    assert extract_numbers("1000,2000") == ("1000", "2000")
    assert extract_numbers("50,1000") == ("50", "1000")
    assert extract_numbers("100,200,3000") == ("100,200", "3000")
    assert missing_numbers("Cost 1,000 and 50% Crit", "消耗 1000 与 50% 暴击") == ()
    assert missing_numbers("HP: 50,1000", "HP：50、1000") == ()
    assert missing_numbers("Gain 250 and 250 or 3.7", "Gain 250") == ("250", "3.7")


def test_placeholder_extraction_and_integrity():
    nonce = secrets.token_hex(8)
    html_a = f"__WUWA_HTML_{nonce}_0000__"
    html_b = f"__WUWA_HTML_{nonce}_0001__"
    term_a = f"__WUWA_TERM_{nonce}_0002__"
    source = f"{html_a}{{player}} hits {term_a} for %1$s and 50%dmg{html_b}"
    reordered_template = f"{html_a}{term_a}被%1$s和{{player}}命中50%dmg{html_b}"
    reordered_html = f"{html_b}{{player}} hits {term_a} for %1$s{html_a}"

    assert extract_placeholders(source) == (html_a, "{player}", term_a, "%1$s", html_b)
    assert check_placeholder_integrity(source, reordered_template) is True
    assert check_placeholder_integrity(source, reordered_html) is False
    assert check_placeholder_integrity("Cost %s", "Cost") is False
