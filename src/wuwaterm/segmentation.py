"""Reference implementation of segmentation-v1.

Cuts a long text into pieces that fit one model request and pairs a source
text with its translation for semantic review. The rules and their reasons
are in docs/segmentation-v1.md; the shared fixtures in
contracts/segmentation-v1.json pin this module's behaviour.

All offsets are Unicode scalar indices (Python str indices).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .review import _sentence_ranges_v2

SEGMENT_LIMIT = 1800
HARD_CUT_WINDOW = 200
BOUNDARY_PUNCTUATION = "，、；：,;:"

# Exactly the scalars of the spec; str.isspace() has a different set.
WHITESPACE: frozenset[str] = frozenset(
    "\t\n\x0b\x0c\r \u00a0\u1680"
    + "".join(chr(code) for code in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000"
)
_NATURAL = WHITESPACE | frozenset(BOUNDARY_PUNCTUATION)

STATUS_READY = "ready"
STATUS_NEEDS_MANUAL_SPLIT = "needs_manual_split"
STATUS_UNTRANSLATED = "untranslated"
STATUS_UNPAIRED_TARGET = "unpaired_target"


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    core_start: int
    core_end: int
    hard_cut: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "core_start": self.core_start,
            "core_end": self.core_end,
            "hard_cut": self.hard_cut,
        }


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    core_start: int
    core_end: int

    def to_dict(self) -> dict[str, int]:
        return {
            "start": self.start,
            "end": self.end,
            "core_start": self.core_start,
            "core_end": self.core_end,
        }


@dataclass(frozen=True)
class SegmentPair:
    index: int
    status: str
    hard_cut: bool
    source: Region
    target: Region

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "status": self.status,
            "hard_cut": self.hard_cut,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
        }


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _core(text: str, start: int, end: int) -> tuple[int, int]:
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


def _core_len(text: str, start: int, end: int) -> int:
    a, b = _core(text, start, end)
    return b - a


def _paragraph_ranges(text: str) -> list[tuple[int, int]]:
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
    if _core(text, 0, length)[0] == _core(text, 0, length)[1]:
        return []
    bounds = [0, *cuts, length]
    return list(zip(bounds, bounds[1:], strict=False))


def _sentence_pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split [start, end) at the first non-WS scalar of each later sentence."""
    cs, ce = _core(text, start, end)
    if cs == ce:
        return [(start, end)]
    cuts: list[int] = []
    for range_start, _range_end in _sentence_ranges_v2(text[cs:ce])[1:]:
        p = cs + range_start
        while p < ce and text[p] in WHITESPACE:
            p += 1
        if p < ce and p > (cuts[-1] if cuts else cs):
            cuts.append(p)
    bounds = [start, *cuts, end]
    return list(zip(bounds, bounds[1:], strict=False))


def _hard_cut_point(text: str, pos: int, limit: int) -> int:
    hi = pos + limit
    lo = max(pos + limit - HARD_CUT_WINDOW, pos + 1)
    for chars in (WHITESPACE, frozenset(BOUNDARY_PUNCTUATION)):
        for p in range(hi, lo - 1, -1):
            if text[p - 1] in chars:
                return p
    return hi


def _hard_cut(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Hard-cut one oversized sentence region into pieces."""
    s, e = _core(text, start, end)
    cuts: list[int] = []
    pos = s
    while e - pos > limit:
        pos = _hard_cut_point(text, pos, limit)
        cuts.append(pos)
    bounds = [start, *cuts, end]
    pieces: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if pieces and _core_len(text, a, b) == 0:
            # A piece of pure WS is a separator of the piece before it.
            pieces[-1] = (pieces[-1][0], b)
        else:
            pieces.append((a, b))
    return pieces


def _check_partition(regions: Sequence[tuple[int, int]], length: int, name: str) -> None:
    pos = 0
    for start, end in regions:
        if start != pos or end < start:
            raise AssertionError(f"{name} regions do not partition the text")
        pos = end
    if pos != length:
        raise AssertionError(f"{name} regions do not cover the text")


# ---------------------------------------------------------------------------
# Single side
# ---------------------------------------------------------------------------


def segment_text(text: str, limit: int = SEGMENT_LIMIT) -> list[Segment]:
    """Cut `text` into segments of at most `limit` scalars of core."""
    length = len(text)
    if length == 0:
        return []
    paragraphs = _paragraph_ranges(text)
    if not paragraphs:
        return [Segment(0, length, 0, 0, False)]

    units: list[tuple[int, int, bool]] = []
    for p_start, p_end in paragraphs:
        if _core_len(text, p_start, p_end) <= limit:
            units.append((p_start, p_end, False))
            continue
        for s_start, s_end in _sentence_pieces(text, p_start, p_end):
            if _core_len(text, s_start, s_end) <= limit:
                units.append((s_start, s_end, False))
            else:
                for h_start, h_end in _hard_cut(text, s_start, s_end, limit):
                    units.append((h_start, h_end, True))

    segments: list[Segment] = []
    cur: Segment | None = None
    for start, end, hard in units:
        core_start, core_end = _core(text, start, end)
        nxt = Segment(start, end, core_start, core_end, hard)
        if cur is None:
            cur = nxt
        elif (
            not cur.hard_cut
            and not nxt.hard_cut
            and nxt.core_end - cur.core_start <= limit
        ):
            cur = Segment(cur.start, nxt.end, cur.core_start, nxt.core_end, False)
        else:
            segments.append(cur)
            cur = nxt
    if cur is not None:
        segments.append(cur)

    _check_partition([(s.start, s.end) for s in segments], length, "segment")
    return segments


# ---------------------------------------------------------------------------
# Bilingual pairing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pair:
    status: str
    hard_cut: bool
    s_start: int
    s_end: int
    t_start: int
    t_end: int


def _coerce_span(name: str, item: object, text: str) -> tuple[int, int]:
    if not isinstance(item, Mapping) or set(item) != {"start", "end", "text"}:
        raise ValueError(f"{name} span is not valid")
    start = item["start"]
    end = item["end"]
    if type(start) is not int or type(end) is not int:
        raise ValueError(f"{name} offsets must be integers")
    if start < 0 or start >= end or end > len(text):
        raise ValueError(f"{name} span is out of bounds")
    if not isinstance(item["text"], str) or text[start:end] != item["text"]:
        raise ValueError(f"{name} span text does not match the text")
    return start, end


def _has_non_ws(text: str, start: int, end: int) -> bool:
    return any(ch not in WHITESPACE for ch in text[start:end])


def _alignment_region_pairs(
    source: str, target: str, alignments: Sequence[Mapping]
) -> list[tuple[int, int, int, int]] | None:
    """Region pairs from alignments; None when non-WS text lies outside them."""
    src_spans: list[tuple[int, int]] = []
    tgt_spans: list[tuple[int, int] | None] = []
    prev_s = 0
    prev_t = 0
    for item in alignments:
        if not isinstance(item, Mapping) or set(item) != {"source", "target"}:
            raise ValueError("alignment keys are not valid")
        s_span = _coerce_span("source", item["source"], source)
        t_span = (
            None if item["target"] is None else _coerce_span("target", item["target"], target)
        )
        if s_span[0] < prev_s:
            raise ValueError("source spans are out of order or overlap")
        prev_s = s_span[1]
        if t_span is not None:
            if t_span[0] < prev_t:
                raise ValueError("target spans are out of order or overlap")
            prev_t = t_span[1]
        src_spans.append(s_span)
        tgt_spans.append(t_span)

    # Non-WS scalars outside every span make the whole alignment untrustworthy.
    pos = 0
    for start, end in src_spans:
        if _has_non_ws(source, pos, start):
            return None
        pos = end
    if _has_non_ws(source, pos, len(source)):
        return None
    pos = 0
    for t_span in tgt_spans:
        if t_span is None:
            continue
        if _has_non_ws(target, pos, t_span[0]):
            return None
        pos = t_span[1]
    if _has_non_ws(target, pos, len(target)):
        return None

    n = len(alignments)
    s_bounds = [0] + [src_spans[i][0] for i in range(1, n)] + [len(source)]
    t_bounds = [0] * (n + 1)
    t_bounds[n] = len(target)
    for i in range(n - 1, 0, -1):
        t_span = tgt_spans[i]
        t_bounds[i] = t_span[0] if t_span is not None else t_bounds[i + 1]
    return [(s_bounds[i], s_bounds[i + 1], t_bounds[i], t_bounds[i + 1]) for i in range(n)]


def _drop_empty_pairs(
    source: str, target: str, regions: list[tuple[int, int, int, int]]
) -> list[tuple[int, int, int, int]]:
    """Fold region pairs with two empty cores into the neighbouring pair.

    The spec does not cover them; they can only come from WS-only alignment
    spans. They are separators, so the previous pair (or the next, for a
    leading one) absorbs them and the partitions stay intact.
    """
    kept = [
        r
        for r in regions
        if _core_len(source, r[0], r[1]) or _core_len(target, r[2], r[3])
    ]
    out: list[tuple[int, int, int, int]] = []
    for k, r in enumerate(kept):
        s_start = 0 if k == 0 else r[0]
        t_start = 0 if k == 0 else r[2]
        if k + 1 < len(kept):
            s_end, t_end = kept[k + 1][0], kept[k + 1][2]
        else:
            s_end, t_end = len(source), len(target)
        out.append((s_start, s_end, t_start, t_end))
    return out


def _fallback_cuts(text: str, a: int, b: int, k: int) -> list[int] | None:
    cuts: list[int] = []
    prev = a
    n = b - a
    for i in range(1, k):
        t_i = a + (i * n) // k
        found = None
        for d in range(HARD_CUT_WINDOW + 1):
            for p in (t_i - d, t_i + d):  # smaller offset first on a tie
                if a < p < b and p > prev and text[p - 1] in _NATURAL:
                    found = p
                    break
            if found is not None:
                break
        if found is None:
            return None
        cuts.append(found)
        prev = found
    return cuts


def _fallback_pairs(
    source: str, target: str, s_range: tuple[int, int], t_range: tuple[int, int], limit: int
) -> list[_Pair]:
    """Cut one oversized sentence pair proportionally, or flag it for manual split."""
    s_a, s_b = _core(source, *s_range)
    t_a, t_b = _core(target, *t_range)
    k = max(-(-(s_b - s_a) // limit), -(-(t_b - t_a) // limit))
    manual = [_Pair(STATUS_NEEDS_MANUAL_SPLIT, False, *s_range, *t_range)]
    s_cuts = _fallback_cuts(source, s_a, s_b, k)
    t_cuts = _fallback_cuts(target, t_a, t_b, k)
    if s_cuts is None or t_cuts is None:
        return manual
    s_bounds = [s_range[0], *s_cuts, s_range[1]]
    t_bounds = [t_range[0], *t_cuts, t_range[1]]
    pairs: list[_Pair] = []
    for i in range(k):
        s_piece = (s_bounds[i], s_bounds[i + 1])
        t_piece = (t_bounds[i], t_bounds[i + 1])
        s_len = _core_len(source, *s_piece)
        t_len = _core_len(target, *t_piece)
        if not (0 < s_len <= limit and 0 < t_len <= limit):
            return manual
        pairs.append(_Pair(STATUS_READY, True, *s_piece, *t_piece))
    return pairs


def _classify_region_pair(
    source: str,
    target: str,
    r: tuple[int, int, int, int],
    limit: int,
    aligned: bool,
) -> list[_Pair]:
    s_start, s_end, t_start, t_end = r
    s_len = _core_len(source, s_start, s_end)
    t_len = _core_len(target, t_start, t_end)
    if s_len and not t_len:
        return [_Pair(STATUS_UNTRANSLATED, False, *r)]
    if t_len and not s_len:
        return [_Pair(STATUS_UNPAIRED_TARGET, False, *r)]
    if s_len <= limit and t_len <= limit:
        return [_Pair(STATUS_READY, False, *r)]
    if not aligned:
        # Whole-document pair without matching paragraph counts: nothing to
        # align the sentences on.
        return [_Pair(STATUS_NEEDS_MANUAL_SPLIT, False, *r)]
    s_sents = _sentence_pieces(source, s_start, s_end)
    t_sents = _sentence_pieces(target, t_start, t_end)
    if len(s_sents) != len(t_sents):
        return [_Pair(STATUS_NEEDS_MANUAL_SPLIT, False, *r)]
    pairs: list[_Pair] = []
    for s_piece, t_piece in zip(s_sents, t_sents, strict=True):
        if (
            _core_len(source, *s_piece) <= limit
            and _core_len(target, *t_piece) <= limit
        ):
            pairs.append(_Pair(STATUS_READY, False, *s_piece, *t_piece))
        else:
            pairs.extend(_fallback_pairs(source, target, s_piece, t_piece, limit))
    return pairs


def _validate_pairs(
    source: str, target: str, pairs: Sequence[SegmentPair], limit: int
) -> None:
    for pair in pairs:
        if pair.status != STATUS_READY:
            continue
        for region in (pair.source, pair.target):
            if not 0 < region.core_end - region.core_start <= limit:
                raise AssertionError("ready pair core is empty or over the limit")
    _check_partition([(p.source.start, p.source.end) for p in pairs], len(source), "source")
    _check_partition([(p.target.start, p.target.end) for p in pairs], len(target), "target")


def pair_segments(
    source: str,
    target: str,
    limit: int = SEGMENT_LIMIT,
    alignments: Sequence[Mapping] | None = None,
) -> list[SegmentPair]:
    """Pair source and target text for semantic review."""
    if alignments is not None and (
        not isinstance(alignments, Sequence)
        or isinstance(alignments, (str, bytes, bytearray))
    ):
        raise ValueError("alignments must be an array")

    if not _has_non_ws(source, 0, len(source)) and not _has_non_ws(target, 0, len(target)):
        if alignments:
            # Still reject malformed input rather than ignore it silently.
            _alignment_region_pairs(source, target, alignments)
        return []

    pairs: list[_Pair] = []
    whole = (0, len(source), 0, len(target))
    aligned = True
    if alignments:
        regions = _alignment_region_pairs(source, target, alignments)
        if regions is None:
            pairs = [_Pair(STATUS_NEEDS_MANUAL_SPLIT, False, *whole)]
        else:
            for r in _drop_empty_pairs(source, target, regions):
                pairs.extend(_classify_region_pair(source, target, r, limit, True))
    else:
        s_paras = _paragraph_ranges(source)
        t_paras = _paragraph_ranges(target)
        if len(s_paras) == len(t_paras) and s_paras:
            for (a, b), (c, d) in zip(s_paras, t_paras, strict=True):
                pairs.extend(_classify_region_pair(source, target, (a, b, c, d), limit, True))
        else:
            aligned = False
            pairs = _classify_region_pair(source, target, whole, limit, aligned)

    # Greedy merge of adjacent ready, non-hard pairs.
    merged: list[_Pair] = []
    for pair in pairs:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.status == STATUS_READY
            and pair.status == STATUS_READY
            and not prev.hard_cut
            and not pair.hard_cut
        ):
            s_a, _ = _core(source, prev.s_start, prev.s_end)
            _, s_b = _core(source, pair.s_start, pair.s_end)
            t_a, _ = _core(target, prev.t_start, prev.t_end)
            _, t_b = _core(target, pair.t_start, pair.t_end)
            if s_b - s_a <= limit and t_b - t_a <= limit:
                merged[-1] = _Pair(
                    STATUS_READY, False, prev.s_start, pair.s_end, prev.t_start, pair.t_end
                )
                continue
        merged.append(pair)

    result: list[SegmentPair] = []
    for index, p in enumerate(merged):
        s_core = _core(source, p.s_start, p.s_end)
        t_core = _core(target, p.t_start, p.t_end)
        result.append(
            SegmentPair(
                index=index,
                status=p.status,
                hard_cut=p.hard_cut,
                source=Region(p.s_start, p.s_end, *s_core),
                target=Region(p.t_start, p.t_end, *t_core),
            )
        )
    _validate_pairs(source, target, result, limit)
    return result
