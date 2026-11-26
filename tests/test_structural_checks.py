"""Tests for pure stdlib structural verification and regrouping helpers."""

from __future__ import annotations

import secrets
import time

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
        ("１００,２００．５", "100、200.5"),
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
        (
            "提升１２％",
            "Increase by 13%",
            [("number_missing", "１２％"), ("number_unexpected", "13%")],
        ),
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
        (
            "<a>x</a><a name='n'/>",
            "</a>x<a><a name='n'/>",
            [("tag_unbalanced", "</a>"), ("tag_unbalanced", "<a>")],
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


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        # The four blind spots of the 2026-11-26 audit, and their variants.
        ("攻击力提高 50%", "ATK increases by 50", [("number_missing", "50%"), ("number_unexpected", "50")]),
        ("伤害提升至1.5倍", "DMG is increased to 1.5", [("number_missing", "1.5倍"), ("number_unexpected", "1.5")]),
        ("Deals 1.5× DMG", "造成1.5点伤害", [("number_missing", "1.5×"), ("number_unexpected", "1.5")]),
        ("暴击率 +20%", "CRIT Rate −20%", [("number_missing", "+20%"), ("number_unexpected", "−20%")]),
        ("暴击率 +20%", "CRIT Rate -20%", [("number_missing", "+20%"), ("number_unexpected", "-20%")]),
        ("暴击率 +20%", "CRIT Rate –20%", [("number_missing", "+20%"), ("number_unexpected", "–20%")]),
        ("攻击力+100", "ATK-100", [("number_missing", "+100"), ("number_unexpected", "-100")]),
        (
            "需要大于 150%",
            "Requires less than 150%",
            [("number_missing", "大于 150%"), ("number_unexpected", "less than 150%")],
        ),
        ("至少 3 层", "at most 3 stacks", [("number_missing", "至少 3"), ("number_unexpected", "at most 3")]),
        ("≥ 150%", "< 150%", [("number_missing", "≥ 150%"), ("number_unexpected", "< 150%")]),
        ("50%以下", "over 50%", [("number_missing", "50%以下"), ("number_unexpected", "over 50%")]),
        ("不低于30%", "below 30%", [("number_missing", "不低于30%"), ("number_unexpected", "below 30%")]),
        # Build markers are one fact each.
        ("S0R1即可", "S0R5 is enough", [("number_missing", "S0R1"), ("number_unexpected", "S0R5")]),
        ("S0R1即可", "S1R0 is enough", [("number_missing", "S0R1"), ("number_unexpected", "S1R0")]),
        ("推荐3+1", "2+1 is recommended", [("number_missing", "3+1"), ("number_unexpected", "2+1")]),
        ("推荐3+1配置", "This setup is recommended", [("number_missing", "3+1")]),
        ("6链后", "At S5", [("number_missing", "6链"), ("number_unexpected", "S5")]),
        # Two units written on both sides that differ.
        ("持续20秒", "Lasts 20m", [("number_missing", "20秒"), ("number_unexpected", "20m")]),
        ("冷却2分钟", "Cooldown: 2s", [("number_missing", "2分钟"), ("number_unexpected", "2s")]),
    ],
)
def test_structure_checks_report_changed_numeric_facts(source, target, expected):
    assert findings(source, target) == expected
    # The rule is symmetric: swapped sides report the same spans.
    swapped = {"number_missing": "number_unexpected", "number_unexpected": "number_missing"}
    assert sorted(findings(target, source)) == sorted(
        (swapped[rule], text) for rule, text in expected
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # A sign, unit or comparison written on one side only.
        ("受到的伤害降低15%", "DMG taken -15%"),
        ("暴击率+20%", "Crit Rate increases by 20%"),
        ("至少需要3层", "Requires 3 stacks"),
        ("冷却时间：12秒", "Cooldown: 12"),
        # The same fact written differently.
        ("50%", "50 percent"),
        ("50%", "50 per cent"),
        ("百分之50", "50%"),
        ("1.5倍", "1.5 times"),
        ("1.5倍", "x1.5"),
        ("2倍伤害", "double DMG"),
        ("暴击伤害-10%", "Crit DMG −10%"),
        ("防御力–20%", "DEF -20%"),
        ("150%以上", "over 150%"),
        ("不超过3次", "no more than 3 times"),
        ("至少3层", "3 or more stacks"),
        ("至少3层", "3 stacks or more"),
        ("最多2次", "up to 2 times"),
        ("10米内", "within 10m"),
        ("20分钟", "20m"),
        ("0.5秒", "0.5s"),
        # Counts that English writes as `×N` or `N times`.
        ("获得100星声", "Obtain Astrite ×100"),
        ("攻击3次", "Attacks 3 times"),
        # Ranges and lists: the dash is not a sign, the last unit covers all.
        ("100–150", "100-150"),
        ("攻击提升30%-45%", "ATK +30-45%"),
        ("技能倍率50%/60%/70%", "Multipliers: 50/60/70%"),
        ("持续3至5秒", "Lasts 3-5s"),
        ("1.5~2倍", "1.5x-2x"),
        ("第1-2关", "Stages 1-2"),
        ("2025年5月22日", "2025-05-22"),
        ("4-3-3-1-1", "4-3-3-1-1"),
        ("Type-2无人机", "Type 2 drone"),
        # Comparisons and units read across markup, never across placeholders.
        ("生命值低于<color=Red>50%</color>时", "When HP is <color=Red>below 50%</color>"),
        ("获得<color=Highlight>1.1</color>倍积分", "Yields <color=Highlight>1.1x</color> points"),
        ("提升{0}%，持续5秒", "+{0}% for 5s"),
        # Build markers in their other spellings, and arithmetic left alone.
        ("C0R1的椿", "An S0R1 Camellya"),
        ("0链1精", "S0R1"),
        ("0+1配置", "S0R1 setup"),
        ("3+1的赞妮", "Zani at Sequence 3 with Refinement 1"),
        ("S1赛季第12层", "Floor 12 in Season 1"),
        ("1+1=2", "1+1=2"),
        # Month names account for the month of a date.
        ("11月26日06:00", "November 26, 06:00"),
        ("4月{0}日开启", "Available Apr {0}"),
        # Which number goes with which attribute is not checked (Layer 2).
        ("攻击力 20%，暴击率 10%", "ATK 10%, CRIT Rate 20%"),
    ],
)
def test_structure_checks_accept_the_same_numeric_facts(source, target):
    assert findings(source, target) == []
    assert findings(target, source) == []


def test_numbers_of_one_value_pair_as_many_as_their_facets_allow():
    # Reading order alone would give the unsigned 70 to `More than 70` and
    # leave `<=70` with nothing; a maximum matching pairs all four.
    source = "70<数量：A。30<数量<=70：B。"
    target = "More than 70: A. 30 to 70: B."
    assert findings(source, target) == []
    assert findings("+20% 和 20%", "20% and −20%") == []
    assert findings("+20% 和 +20%", "20% and −20%") == [
        ("number_missing", "+20%"),
        ("number_unexpected", "−20%"),
    ]


def test_extract_numbers_keeps_reading_digits_only():
    assert extract_numbers("+20% 与 S0R1 和 3+1") == ("20", "0", "1", "3", "1")


@pytest.mark.parametrize(
    "pair",
    [
        ("+1" * 1000, "-1" * 1000),
        ("1%" * 1000, "1 " * 1000),
        ("S0R1" * 500, "S1R0" * 500),
        ("不" * 1990 + "低于50%", "less " * 395 + "than 5"),
        ("大于3" * 666, "小于3" * 666),
        ("".join(f"+{n}% {n}秒 >{n} " for n in range(1000))[:2000],
         "".join(f"-{n}% {n}m <{n} " for n in range(1000))[:2000]),
        ("9" * 2000, "亿" * 2000),
    ],
)
def test_numeric_facts_stay_linear_on_adversarial_sides(pair):
    # Measured best of 5 is under 11 ms locally (scripts/eval_structure_checks.py);
    # the bound here only has to catch a superlinear path on a slow runner.
    source, target = pair
    best = min(_timed(source, target) for _ in range(5))
    assert best < 0.1


def _timed(source: str, target: str) -> float:
    started = time.perf_counter()
    check_segment_structure(source, target)
    return time.perf_counter() - started


def test_printf_zero_run_is_read_in_linear_time():
    # An ambiguous printf pattern took about 1 s at 24 zeros and doubled per zero.
    text = "%" + "0" * 40 + "z"
    started = time.perf_counter()
    assert check_segment_structure(text, text).findings == ()
    assert time.perf_counter() - started < 1.0


def test_structure_layout_counts_paragraphs_and_lines():
    report = check_segment_structure("a\r\nb\r\n\r\nc", "A\nB\n\nC")
    assert report.findings == ()
    assert report.layout == StructureLayout(
        source_paragraphs=2, target_paragraphs=2, source_lines=3, target_lines=3
    )
