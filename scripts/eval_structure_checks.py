#!/usr/bin/env python3
"""Offline benchmark of the structure-check-v1 number rule. No model is called.

Three measurements, all local and reproducible:

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

Exit codes: 0 done; 2 usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "structure-numeric-facts.json"
MODULE_PATH = "src/wuwaterm/structural_checks.py"
NUMBER_RULES = frozenset({"number_missing", "number_unexpected"})
MAX_SIDE = 2000

Check = Callable[[str, str], Any]


def _engine() -> Check:
    sys.path.insert(0, str(ROOT / "src"))
    from wuwaterm.structural_checks import check_segment_structure

    return check_segment_structure


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--textmap-root", type=Path)
    parser.add_argument("--baseline-ref", help="git ref whose number rule the corpus is compared with")
    parser.add_argument("--show-new", type=int, default=0, metavar="N")
    args = parser.parse_args(argv)
    if args.baseline_ref and not args.textmap_root:
        parser.error("--baseline-ref needs --textmap-root")

    check = _engine()
    document = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = {
        "fixture": evaluate_fixture(document["cases"], check, document["negative_buckets"],
                                    document["out_of_scope_buckets"]),
        "timing": timing(check, args.repeats),
    }
    if args.textmap_root:
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
