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

    python scripts/verify_paragraph_regroup.py [--goal-runs PATH]

Exit status is 0 only when every case behaves as documented.
"""

from __future__ import annotations

import argparse
import glob
import json
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--goal-runs",
        type=Path,
        default=ROOT.parent / "goal-runs",
        help="goal-runs directory holding the stored answers",
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

    # Guards: a merge plus a split, and crossing paragraph breaks.
    guards = (
        (
            "merge and split inside one collapsed paragraph",
            "a1\na2\n\nb1\nb2",
            "A1 A2\nB1\nB2x\nB2y",
        ),
        (
            "crossing paragraph breaks",
            "a1\na2\n\nb1\nb2",
            "A1 A2\nB1\n\nB2x\nB2y",
        ),
    )
    for name, source, candidate in guards:
        checked += 1
        if _regroup_lines_into_source_paragraphs(source, candidate) != candidate:
            failures.append(f"{name}: a rejected layout was rewritten")

    print(
        f"source={DOC_ID} paragraphs={source_paragraphs} "
        f"answers={len(answers)} cases={checked} regrouped={regrouped}"
    )
    if failures:
        print("\n".join(f"FAIL {item}" for item in failures), file=sys.stderr)
        return 1
    print("paragraph regrouping replay ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
