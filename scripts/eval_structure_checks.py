#!/usr/bin/env python3
"""Offline benchmark of the structure-check-v1 number rule. No model is called.

Four measurements, all local and reproducible:

``fixture``  the committed pairs in ``tests/fixtures/structure-numeric-facts.json``,
             each run in both directions (as written, then with source and
             target swapped). A negative case is a false positive when it
             raises any finding. A positive case is detected when a number
             finding overlaps one of its marks. The
             ``attribution`` bucket is outside what Layer 1 claims to find and
             is reported apart from the miss rate.
``timing``   adversarial 2,000-scalar sides (both sides at once), best of N runs
             each; the slowest input is reported.
``corpus``   with ``--textmap-root``: every official zh-Hans/en pair of the
             local game data that holds a digit, read in both directions.
             Official translations almost never change a number, so pairs
             flagged by this engine and not by ``--baseline-ref`` are read as
             false positives of the change. Only counts and identifiers are
             printed; ``--show-new`` prints that many newly flagged pairs in
             full for local reading. No corpus text is written anywhere.
``attribution``  the offline candidate signal for number attribution (which
             number belongs to which stat): the synthetic pairs in
             ``tests/fixtures/structure-attribution.json``, the 2,000-scalar
             adversarial sides, and with ``--textmap-root`` the same official
             pairs as ``corpus``. A candidate is not a finding: it is a
             structured hint for the Layer 2 semantic review. Only counts are
             printed; no corpus text is printed at all.

Exit codes: 0 done; 2 usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "structure-numeric-facts.json"
ATTRIBUTION_FIXTURE = ROOT / "tests" / "fixtures" / "structure-attribution.json"
MODULE_PATH = "src/wuwaterm/structural_checks.py"
NUMBER_RULES = frozenset({"number_missing", "number_unexpected"})
MAX_SIDE = 2000

Check = Callable[[str, str], Any]


_MODULE: Any | None = None


def _structural_checks() -> Any:
    """The server's own reader, imported once so ``sys.path`` does not grow per call."""
    global _MODULE
    if _MODULE is None:
        sys.path.insert(0, str(ROOT / "src"))
        from wuwaterm import structural_checks

        _MODULE = structural_checks
    return _MODULE


def _engine() -> Check:
    return _structural_checks().check_segment_structure


def _engine_at(ref: str) -> Check:
    """``check_segment_structure`` as committed at ``ref``, beside the current package."""
    source = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{ref}:{MODULE_PATH}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _engine()  # the package must be importable for the module's relative imports
    spec = importlib.util.spec_from_loader("wuwaterm._structural_checks_baseline", loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "wuwaterm"
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    exec(compile(source, f"{ref}:{MODULE_PATH}", "exec"), module.__dict__)
    return module.check_segment_structure


def _spans(report: Any, rules: frozenset[str] | None = None) -> list[tuple[str, int, int]]:
    out = []
    for finding in report.findings:
        if rules is not None and finding.rule_id not in rules:
            continue
        for side, span in (("source", finding.source_span), ("target", finding.target_span)):
            if span is not None:
                out.append((side, span.start, span.end))
    return out


def _orient(case: dict[str, Any], reverse: bool) -> tuple[str, str, list[tuple[str, int, int]]]:
    source, target = case["source"], case["target"]
    marks = []
    for mark in case.get("marks", ()):
        text = case[mark["side"]]
        start = text.index(mark["text"])
        side = mark["side"]
        if reverse:
            side = "target" if side == "source" else "source"
        marks.append((side, start, start + len(mark["text"])))
    if reverse:
        source, target = target, source
    return source, target, marks


def _overlaps(span: tuple[str, int, int], marks: Iterable[tuple[str, int, int]]) -> bool:
    side, start, end = span
    return any(side == m_side and start < m_end and m_start < end for m_side, m_start, m_end in marks)


def evaluate_fixture(cases: Sequence[dict[str, Any]], check: Check, negative: Iterable[str],
                     out_of_scope: Iterable[str]) -> dict[str, Any]:
    negative = frozenset(negative)
    out_of_scope = frozenset(out_of_scope)
    buckets: dict[str, Counter] = {}
    false_positives: list[str] = []
    misses: list[str] = []
    for case in cases:
        bucket = case["bucket"]
        tally = buckets.setdefault(bucket, Counter())
        for reverse in (False, True):
            label = f"{case['id']}{'-rev' if reverse else ''}"
            source, target, marks = _orient(case, reverse)
            report = check(source, target)
            tally["runs"] += 1
            if bucket in negative:
                if report.findings:
                    tally["flagged"] += 1
                    false_positives.append(label)
                continue
            numbers = _spans(report, NUMBER_RULES)
            if any(_overlaps(span, marks) for span in numbers):
                tally["detected"] += 1
            elif bucket not in out_of_scope:
                misses.append(label)
    negative_runs = sum(buckets[b]["runs"] for b in buckets if b in negative)
    positive = [b for b in buckets if b not in negative and b not in out_of_scope]
    positive_runs = sum(buckets[b]["runs"] for b in positive)
    detected = sum(buckets[b]["detected"] for b in positive)
    scope_runs = sum(buckets[b]["runs"] for b in buckets if b in out_of_scope)
    scope_detected = sum(buckets[b]["detected"] for b in buckets if b in out_of_scope)
    return {
        "cases": len(cases),
        "negative_runs": negative_runs,
        "false_positive_runs": len(false_positives),
        "false_positive_rate": len(false_positives) / negative_runs if negative_runs else 0.0,
        "positive_runs": positive_runs,
        "detected_runs": detected,
        "miss_rate": (positive_runs - detected) / positive_runs if positive_runs else 0.0,
        "out_of_scope_runs": scope_runs,
        "out_of_scope_detected": scope_detected,
        "buckets": {name: dict(tally) for name, tally in sorted(buckets.items())},
        "false_positives": false_positives,
        "misses": misses,
    }


def adversarial_pairs() -> list[tuple[str, str, str]]:
    """Inputs aimed at each reader of the number rule, both sides at the side limit."""

    def fill(unit: str, tail: str = "") -> str:
        body = unit * ((MAX_SIDE - len(tail)) // len(unit))
        return (body + tail)[:MAX_SIDE]

    ones = fill("1 ")
    return [
        ("bare numbers", ones, fill("2 ")),
        ("signed", fill("+1"), fill("-1")),
        ("unicode minus", fill("−1"), fill("+1")),
        ("percent vs bare", fill("1%"), ones),
        ("multiplier", fill("1.5倍"), fill("1.5x ")),
        ("build markers", fill("S0R1"), fill("S1R0")),
        ("plus pairs", fill("3+1"), fill("1+3")),
        ("cn negations", fill("不", "低于50%"), fill("less ", "than 50%")),
        ("en comparators", fill("at most "), fill("at least ")),
        ("cn comparators", fill("大于3"), fill("小于3")),
        ("units", fill("20秒"), fill("20m ")),
        ("ranges", fill("1-2 "), fill("1–2 ")),
        ("grouped", fill("1,000"), fill("1000 ")),
        ("long digits", "9" * MAX_SIDE, "8" * MAX_SIDE),
        ("cn myriads", "亿" * MAX_SIDE, ones),
        ("markup", fill("{0}1%"), fill("<b>1</b>")),
        ("closing tags", fill("<b>1</b>倍"), fill("<i>1</i>%")),
        ("ways per value", fill("+1% 1秒 >1 x1 1m "), fill("-1% 1s <1 1x 1h ")),
        ("many ways", fill("".join(f"{s}1{u} " for s in "+-" for u in ("%", "x", "s", "m", "h", "秒", ""))),
         fill("".join(f"{p}1{u} " for p in ("大于", "<", "") for u in ("%", "x", " times", "s", ""))),),
        ("distinct values", "".join(f"+{n}% {n}秒 >{n} " for n in range(1000))[:MAX_SIDE],
         "".join(f"-{n}% {n}m <{n} " for n in range(1000))[:MAX_SIDE]),
        ("cus templates", fill("{Cus:Sap,S=stack P=stacks SapTag=0} "),
         fill("{Cus:Sap,S=time P=times SapTag=1} ")),
        ("roman numerals", fill("XXXVIII IV I have "), fill("38 4 1 ")),
        ("letter glued", fill("SOL3 T-G0NG HP50% "), fill("3 0 50% ")),
    ]


def timing(check: Check, repeats: int = 5) -> dict[str, Any]:
    results = {}
    for name, source, target in adversarial_pairs():
        best = float("inf")
        for _ in range(repeats):
            started = time.perf_counter()
            check(source, target)
            best = min(best, time.perf_counter() - started)
        results[name] = round(best * 1000, 2)
    slowest = max(results, key=results.__getitem__)
    return {"repeats": repeats, "best_ms": results, "slowest": slowest, "slowest_ms": results[slowest]}


def _textmap(root: Path, language: str) -> dict[str, str]:
    path = root / language / "multi_text" / "MultiText.json"
    with path.open(encoding="utf-8") as handle:
        return {row["Id"]: row["Content"] for row in json.load(handle) if row.get("Content")}


def corpus_pairs(root: Path) -> list[tuple[str, str, str]]:
    zh = _textmap(root, "zh-Hans")
    en = _textmap(root, "en")
    pairs = []
    for key in sorted(zh.keys() & en.keys()):
        source, target = zh[key], en[key]
        if len(source) > MAX_SIDE or len(target) > MAX_SIDE:
            continue
        if any(char.isdigit() for char in source + target):
            pairs.append((key, source, target))
    return pairs


def _number_findings(report: Any) -> list[Any]:
    return [finding for finding in report.findings if finding.rule_id in NUMBER_RULES]


def evaluate_corpus(pairs: Sequence[tuple[str, str, str]], check: Check,
                    baseline: Check | None) -> tuple[dict[str, Any], list[tuple[str, str, str, str]]]:
    flagged = Counter()
    findings = Counter()
    new: list[tuple[str, str, str, str]] = []
    for key, zh, en in pairs:
        for direction, source, target in (("zh-en", zh, en), ("en-zh", en, zh)):
            current = _number_findings(check(source, target))
            if current:
                flagged[direction] += 1
                findings[direction] += len(current)
            if baseline is None:
                continue
            before = _number_findings(baseline(source, target))
            if before:
                flagged[f"{direction} baseline"] += 1
                findings[f"{direction} baseline"] += len(before)
            if current and not before:
                new.append((direction, key, source, target))
            elif len(current) > len(before):
                flagged[f"{direction} more findings"] += 1
    summary: dict[str, Any] = {"pairs": len(pairs), "flagged_pairs": dict(flagged),
                               "number_findings": dict(findings)}
    if baseline is not None:
        summary["newly_flagged"] = Counter(direction for direction, *_ in new)
    return summary, new


# --------------------------------------------------------------------------
# Offline prototype: number-attribution candidates for Layer 2
#
# Layer 1 deliberately does not check which number belongs to which stat: the
# two sides name attributes in different words and orders, so pairing by
# position raises a false alarm on every reordered sentence (ADR 0022). This
# prototype measures the narrow case that is still mechanical: both sides name
# the same two or more stat slots, both sides write the same set of numbers,
# and the numbers sit on different slots. Nothing here is wired into the
# server: the output is a candidate and a structured hint, never a finding.
# --------------------------------------------------------------------------

ATTRIBUTION_VERSION = "attribution-candidates-v1"
ATTRIBUTION_POSITIVE = frozenset({"swapped"})
ATTRIBUTION_NEGATIVE = frozenset({"natural_reorder", "clean_multi_stat"})
MIN_SHARED_SLOTS = 2

# One language-neutral slot per quantity a game sentence attributes a number
# to: ``(slot, zh pattern, en pattern)``. Order is precedence, longest form
# first within a slot, so `暴击伤害` reads as `crit_dmg` and never as the
# `crit_rate` word plus prose, and `CRIT DMG` never falls back to `dmg`.
# Slots name a stat a number is the value of, not a mechanism word that counts
# things (`每层`, `每 2 秒`): those words sit beside a number without owning it.
ATTRIBUTION_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("crit_dmg", "暴击伤害|暴伤", r"CRIT\s*DMG|Crit\.?\s*DMG|CRIT\s*Damage|Crit\.?\s*Damage"),
    ("crit_rate", "暴击率|暴击几率|暴击概率|暴击", r"CRIT\s*Rate|Crit\.?\s*Rate|CRIT\s*Hit"),
    ("energy_regen", "共鸣效率", r"Energy\s+Regen(?:eration)?"),
    ("cooldown", "冷却时间|冷却", r"Cooldown"),
    ("duration", "持续时间|持续", r"Duration|Lasts|Lasting"),
    ("heal", "治疗加成|治疗效果|治疗量|治疗|回复|回血",
     r"Healing(?:\s+Bonus)?|Heal|Restores?|Restoring|Restoration"),
    ("shield", "护盾值|护盾效果|护盾", r"Shield(?:s|ing)?"),
    ("dmg", "伤害倍率|伤害加深|伤害", r"DMG|Damage"),
    ("hp", "生命值上限|生命值|生命|血量", r"HP|Health"),
    ("def", "防御力|防御", r"DEF|Defen[cs]e"),
    ("atk", "攻击力|攻击", r"ATK"),
    ("stamina", "耐力值|耐力", r"Stamina"),
    ("energy", "共鸣能量|能量", r"Energy"),
    ("chance", "概率|几率", r"Chance|Probability"),
    ("range", "范围", r"Range"),
    ("resist", "抗性", r"RES|Resistance"),
    ("cost", "消耗", r"Consum(?:e|es|ed|ing|ption)|Cost"),
    ("stacks", "层数", r"Stacks?"),
)


def _slot_reader(patterns: Sequence[tuple[str, str]], *, words: bool) -> re.Pattern[str]:
    """One pass over a masked side, yielding the slot of each written match."""
    body = "|".join(f"(?P<{slot}>{pattern})" for slot, pattern in patterns)
    if words:
        body = rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"
        return re.compile(body, re.IGNORECASE)
    return re.compile(body)


_SLOT_READERS = (
    _slot_reader([(slot, zh) for slot, zh, _ in ATTRIBUTION_SLOTS], words=False),
    _slot_reader([(slot, en) for slot, _, en in ATTRIBUTION_SLOTS], words=True),
)
# `{Cus:Sap,S=stack P=stacks SapTag=0}` carries digits that are arguments, not
# quantities. The server masks the same shapes before its number rule
# (`structural_checks._CUS_TEMPLATE_RE`); the reader here stays a local copy
# because this script may not change the server module.
_ATTRIBUTION_TEMPLATE_RE = re.compile(r"\{Cus:[A-Za-z]{1,16},[^{}<>{}\n]{0,160}\}")
# Resonance Chain / refinement markers (ADR 0022): one fact each, never a
# quantity a stat slot owns. The shapes follow `structural_checks._BUILD_RE`.
_ATTRIBUTION_BUILD_RE = re.compile(
    r"(?<![A-Za-z0-9])[SsCc][0-9](?:[ \t]?[Rr][0-9])?(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[Rr][0-9](?![A-Za-z0-9])"
    r"|(?<![0-9.+\-×*/=<>])[0-6]\+[1-5](?![0-9.+\-×*/=<>%倍xX])"
    r"|(?<![0-9.])[0-6]链(?:[0-9]精)?(?![接条])"
)
# Digits as the number rule reads them, minus the letter-glued ones (`SOL3`,
# `T-G0NG`, `HP50%`): this prototype only pairs a number with a slot, so it can
# afford to skip the ambiguous spellings.
_ATTRIBUTION_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])|[0-9]+)(?:\.[0-9]+)*"
)
# Clause ends. An ASCII `.` or `,` between digits is a decimal point or a
# thousands separator, not a clause end.
_CLAUSE_END_RE = re.compile(r"(?<![0-9])[.,](?![0-9])|[，;；。！!？?、\n\r]")


def _attribution_hidden(text: str) -> list[tuple[int, int]]:
    """Ranges of ``text`` that no slot or number may be read from."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for token in _structural_checks().extract_placeholders(text):
        start = text.find(token, cursor)
        if start < 0:
            continue
        cursor = start + len(token)
        spans.append((start, cursor))
    spans += [match.span() for match in _ATTRIBUTION_TEMPLATE_RE.finditer(text)]
    spans += [match.span() for match in _ATTRIBUTION_BUILD_RE.finditer(text)]
    return spans


def _masked(text: str) -> str:
    """``text`` with masked ranges blanked, offsets preserved."""
    hidden = [False] * len(text)
    for start, end in _attribution_hidden(text):
        for index in range(start, end):
            hidden[index] = True
    return "".join(" " if hidden[index] else char for index, char in enumerate(text))


def _attribution_value(token: str) -> str:
    """One value per number, whichever way it is written: `0.80` and `0.8` agree."""
    try:
        return format(Decimal(token.replace(",", "")).normalize(), "f")
    except (ArithmeticError, ValueError):
        return token


def _clauses(masked: str, slots: Sequence[tuple[str, int, int]]) -> list[tuple[int, int]]:
    """Clause bounds, never cutting through a slot name (`Crit. Rate`)."""
    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_END_RE.finditer(masked):
        position = match.start()
        if any(slot_start <= position < slot_end for _, slot_start, slot_end in slots):
            continue
        bounds.append((start, position))
        start = match.end()
    bounds.append((start, len(masked)))
    return bounds


def _slots_of(masked: str) -> list[tuple[str, int, int]]:
    """Every written slot occurrence from either reader, left to right."""
    found: list[tuple[str, int, int]] = []
    for reader in _SLOT_READERS:
        for match in reader.finditer(masked):
            slot = match.lastgroup
            if slot is not None:
                found.append((slot, match.start(), match.end()))
    return sorted(found, key=lambda entry: (entry[1], -entry[2]))


def _rank(slot_start: int, slot_end: int, number_start: int, number_end: int) -> tuple[int, int]:
    """Reading order of a number for a slot: the one written after it, else the one before.

    Both languages write the value after the name it belongs to (`攻击力提升
    20%`, `ATK +20%`), and a clause keeps several stats in one sentence, so a
    number further on is a better reading than a closer one already passed.
    The second element breaks ties among numbers on the same side.
    """
    if number_start >= slot_end:
        return 0, number_start - slot_end
    return 1, slot_start - number_end


def _attribution_pairing(text: str) -> dict[str, set[str]]:
    """The ``slot -> values`` reading of one side, clause by clause.

    A slot is dropped when its own clause leaves no single closest number (two
    numbers the same distance apart) or when two different slots claim the
    same number. Guessing there is what makes positional pairing raise a false
    alarm on natural translations, so the prototype declines to pair.
    """
    masked = _masked(text)
    numbers = [
        (match.start(), match.end(), _attribution_value(match.group()))
        for match in _ATTRIBUTION_NUMBER_RE.finditer(masked)
    ]
    slots = _slots_of(masked)
    pairing: dict[str, set[str]] = {}
    for clause_start, clause_end in _clauses(masked, slots):
        clause_numbers = [n for n in numbers if clause_start <= n[0] and n[1] <= clause_end]
        clause_slots = [s for s in slots if clause_start <= s[1] and s[2] <= clause_end]
        if not clause_numbers or not clause_slots:
            continue
        chosen: list[tuple[str, int]] = []
        for slot, slot_start, slot_end in clause_slots:
            ranked = sorted(
                (_rank(slot_start, slot_end, n[0], n[1]), index)
                for index, n in enumerate(clause_numbers)
            )
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                continue
            chosen.append((slot, ranked[0][1]))
        owners: dict[int, set[str]] = {}
        for slot, index in chosen:
            owners.setdefault(index, set()).add(slot)
        for slot, index in chosen:
            if len(owners[index]) > 1:
                continue
            pairing.setdefault(slot, set()).add(clause_numbers[index][2])
    return pairing


def attribution_candidate(source: str, target: str) -> dict[str, Any] | None:
    """A Layer 2 hint when both sides write the same values on different slots.

    ``None`` unless all of: at least two stat slots are named on both sides,
    the values written on those slots are the same set, and at least one slot
    carries a different value than its counterpart. A number the other side
    does not account for at all is the Layer 1 ``number_missing`` rule's, so it
    is not a candidate here.
    """
    source_slots = _attribution_pairing(source)
    target_slots = _attribution_pairing(target)
    shared = sorted(set(source_slots) & set(target_slots))
    if len(shared) < MIN_SHARED_SLOTS:
        return None
    source_values = sorted(value for slot in shared for value in source_slots[slot])
    target_values = sorted(value for slot in shared for value in target_slots[slot])
    if source_values != target_values:
        return None
    differing = [slot for slot in shared if source_slots[slot] != target_slots[slot]]
    if not differing:
        return None
    return {
        "version": ATTRIBUTION_VERSION,
        "shared_slots": len(shared),
        "slots": [
            {
                "slot": slot,
                "source": sorted(source_slots[slot]),
                "target": sorted(target_slots[slot]),
            }
            for slot in differing
        ],
    }


def evaluate_attribution(cases: Sequence[dict[str, Any]], detect: Check) -> dict[str, Any]:
    """Run the synthetic attribution pairs as written, in their own direction."""
    buckets: dict[str, Counter] = {}
    misses: list[str] = []
    false_positives: dict[str, list[str]] = {}
    for case in cases:
        bucket = case["bucket"]
        tally = buckets.setdefault(bucket, Counter())
        tally["runs"] += 1
        hint = detect(case["source"], case["target"])
        if hint is not None:
            tally["candidates"] += 1
        if bucket in ATTRIBUTION_POSITIVE:
            if hint is None:
                misses.append(case["id"])
        elif bucket in ATTRIBUTION_NEGATIVE and hint is not None:
            false_positives.setdefault(bucket, []).append(case["id"])
    positive = sum(buckets[b]["runs"] for b in buckets if b in ATTRIBUTION_POSITIVE)
    detected = sum(buckets[b]["candidates"] for b in buckets if b in ATTRIBUTION_POSITIVE)
    negative = sum(buckets[b]["runs"] for b in buckets if b in ATTRIBUTION_NEGATIVE)
    flagged = sum(len(names) for names in false_positives.values())
    return {
        "cases": len(cases),
        "swapped_runs": positive,
        "swapped_candidates": detected,
        "swapped_recall": detected / positive if positive else 0.0,
        "negative_runs": negative,
        "negative_candidates": flagged,
        "negative_rate": flagged / negative if negative else 0.0,
        "buckets": {name: dict(tally) for name, tally in sorted(buckets.items())},
        "misses": misses,
        "false_positives": false_positives,
    }


def evaluate_attribution_corpus(pairs: Sequence[tuple[str, str, str]], detect: Check,
                                check: Check | None = None) -> dict[str, Any]:
    """Candidate rate of the prototype over official pairs, both directions.

    Counts only. Candidate text is never printed, so a corpus run cannot put
    game text into a log or a document.
    """
    candidates = Counter()
    overlap = Counter()
    runs = 0
    for _key, zh, en in pairs:
        for direction, source, target in (("zh-en", zh, en), ("en-zh", en, zh)):
            runs += 1
            if detect(source, target) is None:
                continue
            candidates[direction] += 1
            if check is not None and check(source, target).findings:
                overlap[direction] += 1
    total = sum(candidates.values())
    return {
        "pairs": len(pairs),
        "runs": runs,
        "candidates": dict(candidates),
        "candidate_rate": total / runs if runs else 0.0,
        "also_structure_finding": dict(overlap),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("fixture", "timing", "corpus", "attribution"),
        help="default: fixture and timing, plus corpus with --textmap-root",
    )
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--textmap-root", type=Path)
    parser.add_argument("--baseline-ref", help="git ref whose number rule the corpus is compared with")
    parser.add_argument("--show-new", type=int, default=0, metavar="N")
    args = parser.parse_args(argv)
    if args.baseline_ref and not args.textmap_root:
        parser.error("--baseline-ref needs --textmap-root")
    if args.mode == "corpus" and not args.textmap_root:
        parser.error("corpus mode needs --textmap-root")

    if args.mode == "attribution":
        document = json.loads(ATTRIBUTION_FIXTURE.read_text(encoding="utf-8"))
        report: dict[str, Any] = {
            "attribution": {
                **evaluate_attribution(document["cases"], attribution_candidate),
                "timing": timing(attribution_candidate, args.repeats),
            }
        }
        if args.textmap_root:
            report["attribution"]["corpus"] = evaluate_attribution_corpus(
                corpus_pairs(args.textmap_root), attribution_candidate, _engine()
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=dict))
        return 0

    check = _engine()
    report = {}
    if args.mode in (None, "fixture"):
        document = json.loads(args.fixture.read_text(encoding="utf-8"))
        report["fixture"] = evaluate_fixture(
            document["cases"], check, document["negative_buckets"],
            document["out_of_scope_buckets"],
        )
    if args.mode in (None, "timing"):
        report["timing"] = timing(check, args.repeats)
    if args.textmap_root and args.mode in (None, "corpus"):
        baseline = _engine_at(args.baseline_ref) if args.baseline_ref else None
        summary, new = evaluate_corpus(corpus_pairs(args.textmap_root), check, baseline)
        report["corpus"] = summary
        for direction, key, source, target in new[: args.show_new]:
            print(f"[new {direction}] {key}\n  S: {source}\n  T: {target}", file=sys.stderr)
            for finding in _number_findings(check(source, target)):
                span = finding.source_span or finding.target_span
                print(f"  -> {finding.rule_id} {span.text!r}", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
