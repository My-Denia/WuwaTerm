"""Pure Unicode-scalar sentence, core, and paragraph span helpers.

Stdlib-only text structure primitives shared across ``segmentation``,
``review``, ``translation_plan``, and ``sentence`` so pure text matching and
plan construction never depend on SQLite-backed review or dictionary storage.
All offsets are Unicode scalar indices (Python ``str`` indices).
"""

from __future__ import annotations

from collections.abc import Callable
import re

_SENTENCE_END = re.compile(r"[。．.！？!?\n]")
# Numbering such as "1. ", "12. " or "IV. " opens an item, and the dot closing
# its label belongs to the numbering rather than to a sentence, so it may not
# split one.
_ROMAN_NUMERAL = re.compile(r"[IVXLCDM]+")

# Exactly the scalars of the spec (docs/segmentation-v1.md); str.isspace() has a
# different set.
WHITESPACE: frozenset[str] = frozenset(
    "\t\n\x0b\x0c\r \u00a0\u1680"
    + "".join(chr(code) for code in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000"
)


def _separator_only(text: str, start: int, end: int) -> bool:
    return start < end and all(
        _SENTENCE_END.match(char) or char.isspace() for char in text[start:end]
    )


def sentence_ranges(text: str) -> list[tuple[int, int]]:
    """V1 sentence ranges splitting on terminators and newlines."""
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if _SENTENCE_END.match(text[index]):
            end = index + 1
            while end < length and _SENTENCE_END.match(text[end]):
                end += 1
            if start < end and not _separator_only(text, start, end):
                ranges.append((start, end))
            start = end
            index = end
            continue
        index += 1
    if start < length and not _separator_only(text, start, length):
        ranges.append((start, length))
    if not ranges and text and not _separator_only(text, 0, length):
        ranges.append((0, length))
    return ranges


def _is_sentence_end_v2(text: str, index: int) -> bool:
    """segmentation-v1 sentence end: only a decimal dot is not a break.

    This is the boundary the published contract pins (docs/segmentation-v1.md)
    and ``segmentation`` imports ``sentence_ranges_v2`` from here, so it stays
    exactly the spec rule. The review-only list-label rule is a separate
    predicate (``review_sentence_ranges_v2``) that never reaches this one.
    """
    char = text[index]
    if not _SENTENCE_END.match(char):
        return False
    if (
        char in {".", "．"}
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    ):
        return False
    return True


def _is_list_marker_dot(text: str, index: int) -> bool:
    """Whether the dot at ``index`` ends a list label, not a sentence.

    A translation often writes "1." where the source wrote "一、", so the
    marker is not part of either sentence's content. Counting it as a sentence
    end would shift every following sentence pair by one, which turns a
    genuinely aligned translation into a paragraph guard that cannot say which
    mention answers which occurrence.

    Only a label that opens the text, opens a line (leading spaces or tabs
    allowed), or follows an item that already ended qualifies; the label is
    ASCII digits or Roman numerals followed by a space or tab. A sentence that
    merely ends after a number ("There are 3. Next") keeps its break, because
    the text before that number is not a terminator.
    """
    if index + 1 >= len(text) or text[index + 1] not in " \t":
        return False
    start = index
    while start > 0 and index - start < 8:
        char = text[start - 1]
        if not (char.isascii() and (char.isdigit() or char in "IVXLCDM")):
            break
        start -= 1
    label = text[start:index]
    if not label or not (label.isdigit() or _ROMAN_NUMERAL.fullmatch(label)):
        return False
    # Indentation before the label is layout, not content.
    before = text[:start].rstrip(" \t")
    if not before or before.endswith("\n"):
        return True
    return before[-1] in "。．！？!?."


def _is_review_sentence_end_v2(text: str, index: int) -> bool:
    """Review-v2 sentence end: a list label is not a break either.

    Only the review's own sentence pairing uses this predicate; the shared
    segmentation-v1 boundary keeps ``_is_sentence_end_v2``.
    """
    if not _is_sentence_end_v2(text, index):
        return False
    return not (text[index] in {".", "．"} and _is_list_marker_dot(text, index))


def _scan_sentence_ranges_v2(
    text: str, is_end: Callable[[str, int], bool]
) -> list[tuple[int, int]]:
    """Cut ``text`` at the scalar a sentence-end predicate accepts.

    One loop for both boundaries: the shared segmentation-v1 one and the
    review's own, which differ only in the predicate.
    """
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if is_end(text, index):
            end = index + 1
            while end < length and is_end(text, end):
                end += 1
            if start < end and not _separator_only(text, start, end):
                ranges.append((start, end))
            start = end
            index = end
            continue
        index += 1
    if start < length and not _separator_only(text, start, length):
        ranges.append((start, length))
    if not ranges and text and not _separator_only(text, 0, length):
        ranges.append((0, length))
    return ranges


def sentence_ranges_v2(text: str) -> list[tuple[int, int]]:
    """V2 sentence ranges, keeping decimal punctuation inside a sentence.

    These are the segmentation-v1 boundaries, and that rule alone: a list
    label such as "1." stops a sentence here, because the published contract
    says so and other repositories consume it.
    """
    return _scan_sentence_ranges_v2(text, _is_sentence_end_v2)


def review_sentence_ranges_v2(text: str) -> list[tuple[int, int]]:
    """Review-v2 sentence ranges: a list label does not end a sentence."""
    return _scan_sentence_ranges_v2(text, _is_review_sentence_end_v2)


def segment_core(text: str, start: int, end: int) -> tuple[int, int]:
    """Core of [start, end): WS trimmed; an all-WS region has core (start, start)."""
    a = start
    while a < end and text[a] in WHITESPACE:
        a += 1
    if a == end:
        return start, start
    b = end
    while text[b - 1] in WHITESPACE:
        b -= 1
    return a, b


def paragraph_ranges(text: str) -> list[tuple[int, int]]:
    """Paragraph regions; empty when the text has no non-WS scalar."""
    length = len(text)
    cuts: list[int] = []
    i = 0
    while i < length:
        if text[i] not in WHITESPACE:
            i += 1
            continue
        j = i
        newlines = 0
        while j < length and text[j] in WHITESPACE:
            if text[j] == "\n":
                newlines += 1
            j += 1
        # A leading run belongs to the first region and a trailing run to the
        # last one, so neither produces a cut.
        if newlines >= 2 and i > 0 and j < length:
            cuts.append(j)
        i = j
    if segment_core(text, 0, length)[0] == segment_core(text, 0, length)[1]:
        return []
    bounds = [0, *cuts, length]
    return list(zip(bounds, bounds[1:], strict=False))
