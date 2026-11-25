"""Pure stdlib post-translation structural verification and regrouping helpers.

These checks operate purely on Unicode scalars, sentence/paragraph ranges from
``text_spans``, numeric tokens, and template/protocol placeholders. Keeping
them outside ``sentence`` and ``translation_plan`` ensures both pure-matching
validation and LLM execution share one deterministic implementation without
touching storage or network layers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import re
import statistics
import unicodedata

from .text_spans import WHITESPACE, paragraph_ranges, segment_core

# Absorbing one line into another and giving a third away as two leaves the
# count where it was, and shifts the content: from the absorbing line to the
# giving one, each answer line answers the source line before it, and after that
# they agree again. Regrouping such an answer would carry text across a break.
#
# Both events are read from a per-line share ratio,
# ``(answer line / answer total) / (source line / source total)``. The ratio
# cancels the translation's overall expansion and each source line's own
# length, so a line that absorbed a naturally long neighbour still reads as two
# lines' worth of text rather than as a local peak against short ones. It also
# does not dilute as the segment grows, which is why this guard is a per-line
# test rather than a check on the two sides' running shares: a one-line shift
# moves the running share by 1/N (5% at 20 lines, 2.2% at 45), while a faithful
# answer at +/-35% per-line length noise already reaches a 5% running-share gap
# at p99, so that signal cannot separate the two at any N.
_MERGED_LINE_SHARE = 1.4
# A merged line also stands this clear of the segment's median line, so a
# whole-answer length bias cannot lift an ordinary line over the bound above.
# The two bounds work together: the first keeps an ordinary line out, the second
# keeps a line out whose segment runs long or short as a whole. 1.3 rather than
# a rounder 1.5 because a merge of two unequal lines lands lower: a line of 40
# with a neighbour of 30 holds 1.75 lines' worth, and a 20% noise draw that
# comes out short leaves 1.42, which a 1.5 peer bound on a median of 1.0 denies,
# while the split bounds below are what keep faithful answers out.
_MERGED_LINE_PEER = 1.3
# The neighbour a merged line absorbed, as a fraction of its own length: a merge
# of two very unequal lines moves too little text to tell from variation.
_ABSORBED_NEIGHBOUR_SHARE = 0.75
# One line left with well under its own text. 0.60 rather than a tighter 0.55:
# a split whose two halves both come out long leaves the shorter half at about
# 0.59 (40, 30, 20, 30 answered as 56, 16, 18, 18 has one at 0.593), which a 0.55
# bound misses while 0.60 catches it, and no faithful line reaches 0.60 at the
# +/-35% per-line noise this guard is calibrated against.
_SPLIT_LINE_SHARE = 0.60
# Or the two halves of one line split in two: each half this small, and the two
# together holding one line's worth (a split conserves the line's text, so their
# ratios sum to about 1 however unevenly the model cut it).
_SPLIT_PAIR_LINE_SHARE = 0.65
_SPLIT_PAIR_TOTAL = (0.75, 1.25)

_NUMBER_RE = re.compile(r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)*")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_OPAQUE_HTML_PLACEHOLDER_RE = re.compile(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4,}__")
_PLACEHOLDER_RE = re.compile(
    r"__WUWA_(?:TERM|HTML)_[0-9a-f]{16}_[0-9]{4,}__"
    r"|\{\{[^{}\s]+\}\}"
    r"|\{[A-Za-z0-9_.:-]+\}"
    r"|(?<![A-Za-z0-9])%(?:\d+\$)?(?:[0+\-#]|\.\d|\d)*[diouxXeEfFgGcrs](?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class ParagraphStructureReport:
    """Comparison of paragraph and line layout between source and translation."""

    source_counts: tuple[int, ...]
    answer_counts: tuple[int, ...]
    source_paragraphs: int
    answer_paragraphs: int
    source_lines: int
    answer_lines: int
    paragraphs_match: bool
    lines_match: bool
    crossing_cuts: bool
    merged_and_split: bool
    regroupable: bool


def has_text(line: str) -> bool:
    """Whether ``line`` contains at least one non-WHITESPACE scalar."""
    return any(char not in WHITESPACE for char in line)


def paragraph_line_counts(text: str) -> list[int]:
    """Non-empty lines in each segmentation-v1 paragraph region of ``text``."""
    return [
        sum(1 for line in text[start:end].split("\n") if has_text(line))
        for start, end in paragraph_ranges(text)
    ]


def line_scalar_counts(text: str) -> list[int]:
    """Non-whitespace scalars of each non-empty line, in reading order."""
    return [
        sum(1 for char in line if char not in WHITESPACE)
        for line in text.split("\n")
        if has_text(line)
    ]


def cumulative_cuts(layout: Sequence[int]) -> frozenset[int]:
    """Where each paragraph ends, counted in non-empty lines."""
    total = 0
    cuts: set[int] = set()
    for count in layout[:-1]:
        total += count
        cuts.add(total)
    return frozenset(cuts)


def line_share_ratios(
    source_counts: Sequence[int], answer_counts: Sequence[int]
) -> list[float]:
    """How many source lines' worth of text each answer line carries."""
    source_total = sum(source_counts)
    answer_total = sum(answer_counts)
    if source_total <= 0 or answer_total <= 0:
        return []
    return [
        (answer / answer_total) / (source / source_total)
        for source, answer in zip(source_counts, answer_counts, strict=True)
    ]


def merged_line_indices(
    source_counts: Sequence[int],
    answer_counts: Sequence[int],
    ratios: Sequence[float],
) -> list[int]:
    """Answer lines that carry their own source line and a neighbour."""
    source_total = sum(source_counts)
    answer_total = sum(answer_counts)
    if source_total <= 0 or answer_total <= 0 or not ratios:
        return []
    source_shares = [count / source_total for count in source_counts]
    answer_shares = [count / answer_total for count in answer_counts]
    peer = statistics.median(ratios)
    merged: list[int] = []
    for index, ratio in enumerate(ratios):
        if ratio >= _MERGED_LINE_SHARE and ratio >= peer * _MERGED_LINE_PEER:
            merged.append(index)
            continue
        own = source_shares[index]
        excess = abs(answer_shares[index] - own)
        if excess <= 0:
            continue
        for other in (index + 1, index - 1):
            if not 0 <= other < len(source_counts):
                continue
            if source_shares[other] < own * _ABSORBED_NEIGHBOUR_SHARE:
                continue
            if abs(answer_shares[index] - (own + source_shares[other])) <= excess / 2:
                merged.append(index)
                break
    return merged


def split_line_indices(ratios: Sequence[float]) -> list[int]:
    """Answer lines that gave text away, on their own or as two halves."""
    split = [index for index, ratio in enumerate(ratios) if ratio <= _SPLIT_LINE_SHARE]
    for index in range(len(ratios) - 1):
        left, right = ratios[index], ratios[index + 1]
        if (
            left <= _SPLIT_PAIR_LINE_SHARE
            and right <= _SPLIT_PAIR_LINE_SHARE
            and _SPLIT_PAIR_TOTAL[0] <= left + right <= _SPLIT_PAIR_TOTAL[1]
        ):
            split.append(index)
    return split


def lines_merged_and_split(source: str, answer: str) -> bool:
    """Whether ``answer`` merged one line and split another."""
    source_counts = line_scalar_counts(source)
    answer_counts = line_scalar_counts(answer)
    if len(source_counts) != len(answer_counts) or len(source_counts) < 2:
        return False
    ratios = line_share_ratios(source_counts, answer_counts)
    if not ratios:
        return False
    if not merged_line_indices(source_counts, answer_counts, ratios):
        return False
    return bool(split_line_indices(ratios))


def check_paragraph_structure(source: str, answer: str) -> ParagraphStructureReport:
    """Analyze paragraph counts, line counts, crossing breaks, and merge/splits."""
    source_counts = tuple(paragraph_line_counts(source))
    answer_counts = tuple(paragraph_line_counts(answer))
    source_paragraphs = len(source_counts)
    answer_paragraphs = len(answer_counts)
    source_lines = sum(source_counts)
    answer_lines = sum(answer_counts)
    paragraphs_match = source_paragraphs == answer_paragraphs
    lines_match = source_lines == answer_lines
    source_cuts = cumulative_cuts(source_counts)
    answer_cuts = cumulative_cuts(answer_counts)
    crossing_cuts = (
        not paragraphs_match
        and lines_match
        and not (answer_cuts <= source_cuts or source_cuts <= answer_cuts)
    )
    merged_split = (
        not paragraphs_match
        and lines_match
        and not crossing_cuts
        and lines_merged_and_split(source, answer)
    )
    regroupable = (
        not paragraphs_match
        and lines_match
        and not crossing_cuts
        and not merged_split
    )
    return ParagraphStructureReport(
        source_counts=source_counts,
        answer_counts=answer_counts,
        source_paragraphs=source_paragraphs,
        answer_paragraphs=answer_paragraphs,
        source_lines=source_lines,
        answer_lines=answer_lines,
        paragraphs_match=paragraphs_match,
        lines_match=lines_match,
        crossing_cuts=crossing_cuts,
        merged_and_split=merged_split,
        regroupable=regroupable,
    )


def regroup_lines_into_source_paragraphs(source: str, answer: str) -> str:
    """Put the lines of ``answer`` back into the paragraphs of ``source`` when safe."""
    report = check_paragraph_structure(source, answer)
    if not report.regroupable:
        return answer
    start, end = segment_core(answer, 0, len(answer))
    lines = iter(line for line in answer[start:end].split("\n") if has_text(line))
    paragraphs = (
        "\n".join(next(lines) for _ in range(count))
        for count in report.source_counts
    )
    return answer[:start] + "\n\n".join(paragraphs) + answer[end:]


def _normalize_number_token(token: str) -> str:
    if _THOUSANDS_RE.match(token):
        return token.replace(",", "")
    return token


def extract_numbers(text: str) -> tuple[str, ...]:
    """Return Arabic/NFKC numeric tokens from ``text`` in reading order."""
    normalized = unicodedata.normalize("NFKC", text)
    return tuple(_NUMBER_RE.findall(normalized))


def missing_numbers(source: str, answer: str) -> tuple[str, ...]:
    """Return numeric tokens present in ``source`` that are missing in ``answer``."""
    available = Counter(_normalize_number_token(num) for num in extract_numbers(answer))
    missing: list[str] = []
    for raw in extract_numbers(source):
        norm = _normalize_number_token(raw)
        if available[norm] > 0:
            available[norm] -= 1
        else:
            missing.append(raw)
    return tuple(missing)


def extract_placeholders(text: str) -> tuple[str, ...]:
    """Return opaque WuwaTerm and template placeholders in reading order."""
    return tuple(_PLACEHOLDER_RE.findall(text))


def check_placeholder_integrity(source: str, answer: str) -> bool:
    """Check whether ``answer`` preserves all placeholders from ``source``.

    All placeholder tokens must match in multiset multiplicity, and opaque HTML
    structural placeholders must also retain their exact relative order.
    """
    src_tokens = extract_placeholders(source)
    ans_tokens = extract_placeholders(answer)
    if Counter(src_tokens) != Counter(ans_tokens):
        return False
    src_html = [t for t in src_tokens if _OPAQUE_HTML_PLACEHOLDER_RE.fullmatch(t)]
    ans_html = [t for t in ans_tokens if _OPAQUE_HTML_PLACEHOLDER_RE.fullmatch(t)]
    return src_html == ans_html
