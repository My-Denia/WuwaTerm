"""Tests for pure stdlib structural verification and regrouping helpers."""

from __future__ import annotations

import secrets

import pytest

from wuwaterm.structural_checks import (
    ParagraphStructureReport,
    StructureLayout,
    check_paragraph_structure,
    check_segment_structure,
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


def findings(source: str, target: str) -> list[tuple[str, str | None]]:
    report = check_segment_structure(source, target)
    result = []
    for finding in report.findings:
        span = finding.source_span or finding.target_span
        side = source if finding.source_span else target
        if span is not None:
            assert side[span.start : span.end] == span.text
        result.append((finding.rule_id, None if span is None else span.text))
    return result


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("获得１,０００星声", "Obtain 1000 Astrite"),
        ("Costs 1,000.50", "花费1000.5"),
        ("获得1.5万星声", "Obtain 15,000 Astrite"),
        ("获得10万", "Get 100,000"),
        ("1.5 million", "150万"),
        ("获得三个", "Get 3"),
        ("二〇二四年", "in 2024"),
        ("百分之五十", "50%"),
        ("Get twenty-one items", "获得21个"),
        ("two hundred and five", "205"),
        ("Hit twice", "命中2次"),
        ("1. 第一项\n2. 第二项", "• First\n• Second"),
        ("(1) 第一项", "First item"),
        ("①攻击 ②防御", "Attack, defend"),
        ("面积10m²", "Area of 10 square meters"),
        ("100，200", "100, 200"),
        ("100,200", "100、200"),
        ("凌晨4:00刷新", "Resets at 4 AM"),
        ("<size=20>文本</size>", "<size=20>Text</size>"),
        ("<te href=123>链接</te>", "<te href=123>link</te>"),
        ("<color=#ffd700>金</color>", "<color=#ffd700>Gold</color>"),
        ("{0}个", "{0} items"),
        ("{Male=他;Female=她}来了", "{Male=He;Female=She} is here"),
        ("今汐", "Jinhsi"),
        ("", "Anything 5"),
    ],
)
def test_structure_checks_accept_equivalent_pairs(source, target):
    assert findings(source, target) == []


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (
            "造成300点伤害",
            "Deals 30 damage",
            [("number_missing", "300"), ("number_unexpected", "30")],
        ),
        ("Gain 250 and 250", "获得250", [("number_missing", "250")]),
        ("消耗三个", "Consume 4", [("number_unexpected", "4")]),
        ("提升１２％", "Increase by 13%", [("number_missing", "１２"), ("number_unexpected", "13")]),
        (
            "<color=Highlight>今汐</color>",
            "Jinhsi",
            [("placeholder_missing", "<color=Highlight>"), ("placeholder_missing", "</color>")],
        ),
        (
            "{PlayerName}来了",
            "{Player} is here",
            [("placeholder_missing", "{PlayerName}"), ("placeholder_unexpected", "{Player}")],
        ),
        ("%1$s获得%d", "%1$s gained", [("placeholder_missing", "%d")]),
        (
            "<b><i>x</i></b>",
            "<b><i>x</b></i>",
            [("tag_unbalanced", "<b>"), ("tag_unbalanced", "</b>")],
        ),
        ("第一段\n\n第二段", "One paragraph only.", [("paragraph_count_mismatch", None)]),
        ("一行\n两行", "One line.", [("line_count_mismatch", None)]),
        ("原文 5 {0}", " \u200b\u3000", [("empty_target", None)]),
        (
            "第一段 {0}\n\n第二段 5",
            "Only {1} paragraph 6",
            [
                ("paragraph_count_mismatch", None),
                ("placeholder_missing", "{0}"),
                ("placeholder_unexpected", "{1}"),
                ("number_missing", "5"),
                ("number_unexpected", "6"),
            ],
        ),
    ],
)
def test_structure_checks_report_breaks_in_a_fixed_order(source, target, expected):
    assert findings(source, target) == expected


def test_structure_layout_counts_paragraphs_and_lines():
    report = check_segment_structure("a\r\nb\r\n\r\nc", "A\nB\n\nC")
    assert report.findings == ()
    assert report.layout == StructureLayout(
        source_paragraphs=2, target_paragraphs=2, source_lines=3, target_lines=3
    )
