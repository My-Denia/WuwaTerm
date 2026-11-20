#!/usr/bin/env python3
"""Replay the context-mode paragraph post-check on stored answers (MY-359).

The post-check in ``wuwaterm.sentence._regroup_lines_into_source_paragraphs``
rewrites the blank lines of a model answer so its paragraphs match the source.
MY-359 added two conservative guards to it, and this script replays the frozen
criteria (docs/eval/paragraph-layout-criteria.md) on stored artifacts so a
guard that rejects a genuine answer cannot pass unnoticed.

What it reads, all under ``goal-runs/`` and none of it sealed:

- the stored D-003 answers in
  ``names-critical-batch-20261116/evaluator/*/raw_outputs.json``,
- the D-003 source paragraph count recorded in the same file.

Every stored D-003 answer in this workspace already has the source's paragraph
count, so L4 (unchanged when the counts match) is checked directly. The
31-paragraph and 8-paragraph answers the frozen criteria describe are not
stored here; their layout is reproduced from a stored 45-paragraph answer by
removing blank lines, which keeps the text and the 45-line sequence real. L2 is
checked on that reproduction, and the guards are checked on the merge+split and
crossing-break cases the criteria name.

MY-359's amendment (2026-11-20) widened the merge+split guard to the whole
segment, so this script also replays the counterexamples that broke the earlier
six-line window - a merge in the first line and a split in the last, at 20 and
40 lines, in either order, with and without ordinary translation noise, and
with the merged line beside a naturally long source line - and measures the
guard's false-rejection rate on synthetic faithful answers at +/-35% per-line
length noise.

    python scripts/verify_paragraph_regroup.py [--goal-runs PATH] [--trials N]

Exit status is 0 only when every case behaves as documented.
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.sentence import (  # noqa: E402 - the path insert must come first
    _paragraph_line_counts,
    _regroup_lines_into_source_paragraphs,
)

DOC_ID = "D-003"
SOURCE_FILES = (
    "real-manuscript-eval-20261115/corpus/D/{doc}/source.txt",
    "real-manuscript-eval-20261115-D/corpus/{doc}/source.txt",
)
STORE_GLOB = "names-critical-batch-20261116/evaluator/*/raw_outputs.json"
# The R set: the stored answers of the second document family, with their
# sources in the batch's frozen inputs.
R_INPUTS_GLOB = "names-critical-batch-20261116/evaluator/inputs/full-R.json"
R_STORE_GLOB = "names-critical-batch-20261116/evaluator/*/raw_outputs.json"
R_SETS = frozenset({"R1", "R2"})


def _line_sequence(text: str) -> list[str]:
    """Non-empty lines with surrounding whitespace removed."""
    return [
        line.strip() for line in text.split("\n") if line.strip()
    ]


def _layout_answer(text: str, target_paragraphs: int) -> str:
    """Re-lay ``text``'s lines into ``target_paragraphs`` paragraphs.

    The lines and their order are kept; only the blank lines change. A
    paragraph break inside a paragraph is dropped, so the reproduction has the
    same text as the stored answer and a different paragraph count, which is
    exactly the shape the model produced in the frozen run.
    """
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    if not 1 <= target_paragraphs < len(paragraphs):
        raise ValueError("target paragraph count is not reachable")
    drop = len(paragraphs) - target_paragraphs
    # Drop the blank lines evenly, so the reproduction looks like a model that
    # kept some paragraphs and folded others instead of one giant paragraph.
    step = len(paragraphs) / drop
    boundaries = {int(index * step) + 1 for index in range(drop)}
    merged: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if merged and index in boundaries:
            merged[-1] = f"{merged[-1]}\n{paragraph}"
        else:
            merged.append(paragraph)
    while len(merged) > target_paragraphs:
        merged[-2] = f"{merged[-2]}\n{merged[-1]}"
        merged.pop()
    return "\n\n".join(merged)


def _load_r_set(goal_runs: Path) -> list[tuple[str, str, str]]:
    """Stored R answers as (label, source, answer) triples, deduplicated."""
    inputs_path = goal_runs / R_INPUTS_GLOB
    if not inputs_path.is_file():
        return []
    try:
        inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sources = {
        entry["doc_id"]: entry.get("text") or entry.get("source") or ""
        for entry in inputs.get("documents") or ()
        if entry.get("doc_id")
    }
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(glob.glob(str(goal_runs / R_STORE_GLOB))):
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in document.get("documents") or ():
            doc_id = entry.get("doc_id")
            text = entry.get("text")
            if doc_id not in sources or not text:
                continue
            if (doc_id, text) in seen:
                continue
            seen.add((doc_id, text))
            found.append((doc_id, sources[doc_id], text))
    return found


def _load_stored(goal_runs: Path) -> tuple[str, list[tuple[str, str]]]:
    """Return the stored source text and the (label, answer) pairs."""
    source_text = ""
    for template in SOURCE_FILES:
        path = goal_runs / template.format(doc=DOC_ID)
        if path.is_file():
            source_text = path.read_text(encoding="utf-8")
            break
    answers: list[tuple[str, str]] = []
    for path in sorted(glob.glob(str(goal_runs / STORE_GLOB))):
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in document.get("documents") or ():
            if entry.get("doc_id") != DOC_ID or not entry.get("text"):
                continue
            label = Path(path).relative_to(goal_runs).as_posix()
            if (label, entry["text"]) not in answers:
                answers.append((f"{label} [{document.get('arm')}]", entry["text"]))
    if not source_text:
        raise SystemExit(
            f"no stored {DOC_ID} source under {goal_runs}; nothing to replay"
        )
    return source_text, answers


def _rows(counts: list[int]) -> str:
    """One line per count, each ``count`` non-whitespace scalars long."""
    return "\n".join("\u5b57" * count for count in counts)


def _two_paragraph_source(
    rows: int, scalars: int = 41, long_row: int | None = None
) -> tuple[str, list[int]]:
    """``rows`` equal source lines in two paragraphs, one row optionally long."""
    counts = [scalars] * rows
    if long_row is not None:
        counts[long_row] = scalars * 3
    half = rows // 2
    return f"{_rows(counts[:half])}\n\n{_rows(counts[half:])}", counts


def _merged_and_split_answer(
    counts: list[int],
    merge_at: int,
    split_at: int,
    *,
    half: float = 0.5,
    noise: float = 0.0,
    rng: random.Random | None = None,
) -> str:
    """A one-paragraph answer that merged one line and split another.

    Source lines ``merge_at`` and ``merge_at + 1`` come back as one answer line,
    and the line answering source line ``split_at`` comes back as two lines that
    take ``half`` and ``1 - half`` of its text. The line count is unchanged, so
    only the per-line lengths show that entry k is not source entry k.
    """
    answer: list[int] = []
    index = 0
    while index < len(counts):
        if index == merge_at:
            answer.append(counts[index] + counts[index + 1])
            index += 2
        else:
            answer.append(counts[index])
            index += 1
    position = split_at if split_at < merge_at else split_at - 1
    whole = answer[position]
    first = int(whole * half)
    answer[position : position + 1] = [first, whole - first]
    if noise and rng is not None:
        answer = [
            max(2, round(count * (1 + rng.uniform(-noise, noise))))
            for count in answer
        ]
    return _rows(answer)


def _counterexamples() -> list[tuple[str, str, str]]:
    """The distant merge+split answers the 2026-11-20 amendment names.

    Each is (label, source, answer): the answer keeps the source's line count
    and loses its blank lines, so the post-check would regroup it into the
    source's paragraphs and move real text across a break if the guard missed.
    """
    rng = random.Random(11)
    cases: list[tuple[str, str, str]] = []
    for rows in (20, 40):
        source, counts = _two_paragraph_source(rows)
        for half, label in ((0.5, "equal halves"), (0.6, "60/40 halves")):
            cases.append(
                (
                    f"{rows} lines, merge first + split last, {label}",
                    source,
                    _merged_and_split_answer(counts, 0, rows - 1, half=half),
                )
            )
            cases.append(
                (
                    f"{rows} lines, {label}, 20% translation noise",
                    source,
                    _merged_and_split_answer(
                        counts, 0, rows - 1, half=half, noise=0.2, rng=rng
                    ),
                )
            )
        cases.append(
            (
                f"{rows} lines, split first + merge last",
                source,
                _merged_and_split_answer(counts, rows - 2, 0),
            )
        )
    long_row_source, long_row_counts = _two_paragraph_source(20, long_row=2)
    cases.append(
        (
            "20 lines, merged line beside a naturally long source line",
            long_row_source,
            _merged_and_split_answer(long_row_counts, 0, 19),
        )
    )
    long_absorbed_source, long_absorbed_counts = _two_paragraph_source(20, long_row=1)
    cases.append(
        (
            "20 lines, merge absorbs a naturally long source line",
            long_absorbed_source,
            _merged_and_split_answer(long_absorbed_counts, 0, 19),
        )
    )
    return cases


def _faithful_noise_rate(rows: int, trials: int) -> float:
    """Share of faithful line-by-line answers with noise that a guard rejects."""
    rng = random.Random(20261120)
    rejected = 0
    for _ in range(trials):
        counts = [rng.randint(6, 40) for _ in range(rows)]
        half = rows // 2
        source = f"{_rows(counts[:half])}\n\n{_rows(counts[half:])}"
        answer = _rows(
            [
                max(2, round(count * (1 + rng.uniform(-0.35, 0.35))))
                for count in counts
            ]
        )
        if _regroup_lines_into_source_paragraphs(source, answer) == answer:
            rejected += 1
    return rejected / trials if trials else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--goal-runs",
        type=Path,
        default=ROOT.parent / "goal-runs",
        help="goal-runs directory holding the stored answers",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=10000,
        help="faithful-answer trials per line count for the noise replay",
    )
    args = parser.parse_args(argv)
    source_text, answers = _load_stored(args.goal_runs)
    if not answers:
        raise SystemExit(
            f"no stored {DOC_ID} answer under {args.goal_runs}; nothing to replay"
        )
    source_layout = _paragraph_line_counts(source_text)
    source_paragraphs = len(source_layout)

    failures: list[str] = []
    checked = 0
    regrouped = 0

    # L4: an answer whose paragraph count already matches is byte-identical.
    for label, answer in answers:
        after_text = _regroup_lines_into_source_paragraphs(source_text, answer)
        checked += 1
        if after_text != answer:
            failures.append(f"{label}: matching layout was rewritten")
        if len(_paragraph_line_counts(answer)) != source_paragraphs:
            failures.append(
                f"{label}: stored answer has "
                f"{len(_paragraph_line_counts(answer))} paragraphs, "
                f"source has {source_paragraphs}"
            )

    # L2/L3: a real answer with the blank lines dropped comes back with the
    # source's paragraph count and its lines unchanged.
    label, answer = max(answers, key=lambda item: len(item[1]))
    if len(_paragraph_line_counts(answer)) != source_paragraphs:
        raise SystemExit(f"{label}: the chosen answer is not a layout baseline")
    original_lines = _line_sequence(answer)
    for target in (31, 8):
        collapsed = _layout_answer(answer, target)
        if len(_paragraph_line_counts(collapsed)) != target:
            failures.append(f"collapsed {target}: reproduction layout is wrong")
            continue
        restored = _regroup_lines_into_source_paragraphs(source_text, collapsed)
        checked += 1
        if _paragraph_line_counts(restored) != source_layout:
            failures.append(
                f"collapsed {target}: regrouped to "
                f"{_paragraph_line_counts(restored)}, expected {source_layout}"
            )
            continue
        if _line_sequence(restored) != original_lines:
            failures.append(f"collapsed {target}: line sequence changed")
            continue
        regrouped += 1

    # The R documents: a stored answer whose layout already matches, or whose
    # line count differs, must stay byte-identical; one that differs only in
    # blank lines must come back in its source's layout with its lines intact.
    r_regrouped = 0
    for label, r_source, r_answer in _load_r_set(args.goal_runs):
        source_layout_r = _paragraph_line_counts(r_source)
        answer_layout_r = _paragraph_line_counts(r_answer)
        restored = _regroup_lines_into_source_paragraphs(r_source, r_answer)
        checked += 1
        if len(answer_layout_r) == len(source_layout_r) or sum(
            answer_layout_r
        ) != sum(source_layout_r):
            if restored != r_answer:
                failures.append(f"R {label}: unchanged layout was rewritten")
            continue
        if _paragraph_line_counts(restored) != source_layout_r:
            failures.append(
                f"R {label}: regrouped to {_paragraph_line_counts(restored)}, "
                f"expected {source_layout_r}"
            )
            continue
        if _line_sequence(restored) != _line_sequence(r_answer):
            failures.append(f"R {label}: line sequence changed")
            continue
        r_regrouped += 1
    regrouped += r_regrouped

    # Guards: the counterexamples the 2026-11-20 amendment names, plus the
    # crossing-break sample kept from the earlier amendment.
    guards = list(_counterexamples()) + [
        (
            "crossing paragraph breaks",
            "a1\na2\n\nb1\nb2",
            "A1 A2\nB1\n\nB2x\nB2y",
        ),
    ]
    for name, source, candidate in guards:
        checked += 1
        if _regroup_lines_into_source_paragraphs(source, candidate) != candidate:
            failures.append(f"{name}: a rejected layout was rewritten")

    # The other direction: a faithful line-by-line answer with ordinary per-line
    # length noise must still regroup. A guard that fires here costs a real
    # answer its paragraph layout, so the rate is reported and a single hit
    # fails the replay.
    rates = []
    for rows in (10, 20, 45):
        rate = _faithful_noise_rate(rows, args.trials)
        rates.append(f"N={rows}:{rate:.4f}")
        checked += args.trials
        if rate > 0.0:
            failures.append(
                f"faithful answers at N={rows} were rejected in "
                f"{rate:.2%} of {args.trials} trials"
            )

    print(
        f"source={DOC_ID} paragraphs={source_paragraphs} "
        f"answers={len(answers)} cases={checked} regrouped={regrouped}"
    )
    print("faithful-noise rejection rate " + " ".join(rates))
    if failures:
        print("\n".join(f"FAIL {item}" for item in failures), file=sys.stderr)
        return 1
    print("paragraph regrouping replay ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
