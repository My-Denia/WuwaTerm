"""Exact-parity candidate-pruning index for :meth:`TermService.lookup`.

Every pruning rule here is a mathematically necessary condition for a
dictionary row to reach the fuzzy publish floor (or dynamic top-K cutoff)
under ``TermService._score``. No heuristic approximations are used: for any
dictionary, query, and limit, ``FuzzyIndex.search`` returns the exact same
``(entry, score, reason)`` candidates and tie-break order as a full-table scan.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher

from .constants import CATEGORY_ORDER
from .models import LookupCandidate, TermEntry
from .normalize import normalize_ascii, normalize_text

FUZZY_FLOOR = 45.0
ZH_WEIGHT = 80.0
EN_WEIGHT = 70.0
PY_WEIGHT = 82.0

_ZH_BITSET_THRESHOLD = 128
_OCC_BITSET_THRESHOLD = 64
_BG_BITSET_THRESHOLD = 64
_DIRECT_UNIV_THRESHOLD = 32
_BOUND_EPS = 1e-9

Posting = tuple[int, ...] | int
SortKey = tuple[float, int, int, int, str, str, int]


def source_priority(entry: TermEntry) -> int:
    """Return tie-breaking priority bucket for a dictionary entry."""
    if entry.source_id.startswith("OccupationConfig_") and entry.source_id.endswith(
        "_Name"
    ):
        return 0
    if "RoleInfo" in entry.source_file:
        return 1
    if entry.category == "speaker":
        return 8
    return 5


def _posting_to_mask(posting: Posting) -> int:
    if isinstance(posting, int):
        return posting
    mask = 0
    for idx in posting:
        mask |= 1 << idx
    return mask


def _pack_postings(
    buckets: dict[str, list[int]] | dict[tuple[str, int], list[int]],
    *,
    threshold: int,
    nbytes: int,
) -> dict[object, Posting]:
    out: dict[object, Posting] = {}
    for key, indices in buckets.items():
        if len(indices) < threshold:
            out[key] = tuple(indices)
        else:
            buf = bytearray(nbytes)
            for idx in indices:
                buf[idx >> 3] |= 1 << (idx & 7)
            out[key] = int.from_bytes(buf, "little")
    return out


def _pack_len_masks(
    buckets: dict[int, list[int]],
    *,
    nbytes: int,
) -> dict[int, int]:
    out: dict[int, int] = {}
    for length, indices in buckets.items():
        if len(indices) < _OCC_BITSET_THRESHOLD:
            mask = 0
            for idx in indices:
                mask |= 1 << idx
            out[length] = mask
        else:
            buf = bytearray(nbytes)
            for idx in indices:
                buf[idx >> 3] |= 1 << (idx & 7)
            out[length] = int.from_bytes(buf, "little")
    return out


def _bit_add_all(bitsets: list[int]) -> list[int]:
    """Sum a list of N-row bitsets in bit-parallel, returning bit-planes."""
    planes: list[int] = []
    for bs in bitsets:
        carry = bs
        for i in range(len(planes)):
            plane = planes[i]
            planes[i] = plane ^ carry
            carry = plane & carry
            if not carry:
                break
        else:
            if carry:
                planes.append(carry)
    return planes


def _eq_masks_min(
    planes: list[int],
    universe: int,
    min_overlap: int,
) -> dict[int, int]:
    """Group rows in ``universe`` by exact bit-plane sum ``>= min_overlap``."""
    out: dict[int, int] = {}
    if not universe:
        return out
    n_planes = len(planes)
    if n_planes == 0:
        if min_overlap <= 0:
            out[0] = universe
        return out

    def rec(bit_idx: int, cur_val: int, mask: int) -> None:
        if not mask:
            return
        if bit_idx < 0:
            if cur_val >= min_overlap:
                out[cur_val] = mask
            return
        max_rem = (1 << (bit_idx + 1)) - 1
        if cur_val + max_rem < min_overlap:
            return
        plane = planes[bit_idx]
        hi = mask & plane
        lo = mask ^ hi
        if hi:
            rec(bit_idx - 1, cur_val | (1 << bit_idx), hi)
        if lo and cur_val + ((1 << bit_idx) - 1) >= min_overlap:
            rec(bit_idx - 1, cur_val, lo)

    rec(n_planes - 1, 0, universe)
    return out


def _intersect_postings(postings: list[Posting]) -> list[int]:
    """Return sorted row indices present in every posting list/bitset."""
    if not postings:
        return []
    tuples: list[tuple[int, ...]] = []
    bitset_mask: int | None = None
    for posting in postings:
        if isinstance(posting, tuple):
            tuples.append(posting)
        else:
            bitset_mask = posting if bitset_mask is None else (bitset_mask & posting)
            if bitset_mask == 0:
                return []
    if tuples:
        tuples.sort(key=len)
        result = list(tuples[0])
        for tup in tuples[1:]:
            tup_set = set(tup)
            result = [idx for idx in result if idx in tup_set]
            if not result:
                return []
        if bitset_mask is not None:
            result = [idx for idx in result if (bitset_mask >> idx) & 1]
        return result
    assert bitset_mask is not None
    result = []
    mask = bitset_mask
    while mask:
        lsb = mask & -mask
        result.append(lsb.bit_length() - 1)
        mask ^= lsb
    return result


class FuzzyIndex:
    """Immutable candidate-pruning index over a snapshot's term entries."""

    __slots__ = (
        "_entries",
        "_zh_norms",
        "_en_norms",
        "_pinyins",
        "_zh_lengths",
        "_en_lengths",
        "_py_lengths",
        "_pinyin_abbrev_map",
        "_pinyin_2gram",
        "_zh_char_index",
        "_zh_char_counts",
        "_zh_len_masks",
        "_en_occ",
        "_en_len_masks",
        "_py_occ",
        "_py_len_masks",
    )

    def __init__(
        self,
        *,
        entries: tuple[TermEntry, ...],
        zh_norms: tuple[str, ...],
        en_norms: tuple[str, ...],
        pinyins: tuple[str, ...],
        zh_lengths: tuple[int, ...],
        en_lengths: tuple[int, ...],
        py_lengths: tuple[int, ...],
        pinyin_abbrev_map: dict[str, tuple[int, ...]],
        pinyin_2gram: dict[str, Posting],
        zh_char_index: dict[str, Posting],
        zh_char_counts: dict[str, int],
        zh_len_masks: dict[int, int],
        en_occ: dict[tuple[str, int], Posting],
        en_len_masks: dict[int, int],
        py_occ: dict[tuple[str, int], Posting],
        py_len_masks: dict[int, int],
    ) -> None:
        self._entries = entries
        self._zh_norms = zh_norms
        self._en_norms = en_norms
        self._pinyins = pinyins
        self._zh_lengths = zh_lengths
        self._en_lengths = en_lengths
        self._py_lengths = py_lengths
        self._pinyin_abbrev_map = pinyin_abbrev_map
        self._pinyin_2gram = pinyin_2gram
        self._zh_char_index = zh_char_index
        self._zh_char_counts = zh_char_counts
        self._zh_len_masks = zh_len_masks
        self._en_occ = en_occ
        self._en_len_masks = en_len_masks
        self._py_occ = py_occ
        self._py_len_masks = py_len_masks

    @classmethod
    def build(cls, entries: Sequence[TermEntry]) -> FuzzyIndex:
        """Build a :class:`FuzzyIndex` from an ordered sequence of entries."""
        entry_tuple = tuple(entries)
        n = len(entry_tuple)
        nbytes = (n + 7) >> 3

        zh_norms: list[str] = [""] * n
        en_norms: list[str] = [""] * n
        pinyins: list[str] = [""] * n
        zh_lengths: list[int] = [0] * n
        en_lengths: list[int] = [0] * n
        py_lengths: list[int] = [0] * n

        abbrev_lists: dict[str, list[int]] = defaultdict(list)
        bg_lists: dict[str, list[int]] = defaultdict(list)
        zh_lists: dict[str, list[int]] = defaultdict(list)
        zh_len_lists: dict[int, list[int]] = defaultdict(list)
        en_occ_lists: dict[tuple[str, int], list[int]] = defaultdict(list)
        en_len_lists: dict[int, list[int]] = defaultdict(list)
        py_occ_lists: dict[tuple[str, int], list[int]] = defaultdict(list)
        py_len_lists: dict[int, list[int]] = defaultdict(list)

        for idx, entry in enumerate(entry_tuple):
            zh_norm = normalize_text(entry.zh)
            en_norm = normalize_text(entry.en)
            py = entry.pinyin
            zh_norms[idx] = zh_norm
            en_norms[idx] = en_norm
            pinyins[idx] = py

            if zh_norm:
                zh_lengths[idx] = len(zh_norm)
                zh_len_lists[len(zh_norm)].append(idx)
                for ch in set(zh_norm):
                    zh_lists[ch].append(idx)

            if en_norm:
                en_lengths[idx] = len(en_norm)
                en_len_lists[len(en_norm)].append(idx)
                seen_en: dict[str, int] = {}
                for ch in en_norm:
                    occ = seen_en.get(ch, 0) + 1
                    seen_en[ch] = occ
                    en_occ_lists[(ch, occ)].append(idx)

            if py:
                py_lengths[idx] = len(py)
                py_len_lists[len(py)].append(idx)
                seen_py: dict[str, int] = {}
                for ch in py:
                    occ = seen_py.get(ch, 0) + 1
                    seen_py[ch] = occ
                    py_occ_lists[(ch, occ)].append(idx)
                if len(py) >= 2:
                    for bg in {py[i : i + 2] for i in range(len(py) - 1)}:
                        bg_lists[bg].append(idx)

            if entry.pinyin_abbrev:
                abbrev_lists[entry.pinyin_abbrev].append(idx)

        zh_char_counts = {ch: len(indices) for ch, indices in zh_lists.items()}
        zh_char_index_raw = _pack_postings(
            zh_lists, threshold=_ZH_BITSET_THRESHOLD, nbytes=nbytes
        )
        en_occ_raw = _pack_postings(
            en_occ_lists, threshold=_OCC_BITSET_THRESHOLD, nbytes=nbytes
        )
        py_occ_raw = _pack_postings(
            py_occ_lists, threshold=_OCC_BITSET_THRESHOLD, nbytes=nbytes
        )
        bg_raw = _pack_postings(
            bg_lists, threshold=_BG_BITSET_THRESHOLD, nbytes=nbytes
        )

        return cls(
            entries=entry_tuple,
            zh_norms=tuple(zh_norms),
            en_norms=tuple(en_norms),
            pinyins=tuple(pinyins),
            zh_lengths=tuple(zh_lengths),
            en_lengths=tuple(en_lengths),
            py_lengths=tuple(py_lengths),
            pinyin_abbrev_map={k: tuple(v) for k, v in abbrev_lists.items()},
            pinyin_2gram=bg_raw,  # type: ignore[arg-type]
            zh_char_index=zh_char_index_raw,  # type: ignore[arg-type]
            zh_char_counts=zh_char_counts,
            zh_len_masks=_pack_len_masks(zh_len_lists, nbytes=nbytes),
            en_occ=en_occ_raw,  # type: ignore[arg-type]
            en_len_masks=_pack_len_masks(en_len_lists, nbytes=nbytes),
            py_occ=py_occ_raw,  # type: ignore[arg-type]
            py_len_masks=_pack_len_masks(py_len_lists, nbytes=nbytes),
        )

    def _fast_pinyin_candidates(self, q_ascii: str) -> list[int]:
        """Return all rows that might hit exact/prefix/abbrev/substring pinyin."""
        cands: set[int] = set(self._pinyin_abbrev_map.get(q_ascii, ()))
        if len(q_ascii) == 1:
            posting = self._py_occ.get((q_ascii, 1))
            if isinstance(posting, tuple):
                cands.update(posting)
            elif isinstance(posting, int):
                mask = posting
                while mask:
                    lsb = mask & -mask
                    cands.add(lsb.bit_length() - 1)
                    mask ^= lsb
        else:
            bigrams = {q_ascii[i : i + 2] for i in range(len(q_ascii) - 1)}
            postings: list[Posting] = []
            missing = False
            for bg in bigrams:
                p = self._pinyin_2gram.get(bg)
                if p is None:
                    missing = True
                    break
                postings.append(p)
            if not missing:
                cands.update(_intersect_postings(postings))
        return sorted(cands)

    def search(self, query: str, *, limit: int = 5) -> list[LookupCandidate]:
        """Return fuzzy candidates matching ``TermService._fuzzy`` exactly."""
        q_norm = normalize_text(query)
        q_ascii = normalize_ascii(query)
        if not q_norm and not q_ascii:
            return []

        entries = self._entries
        if not entries:
            return []

        effective_limit = max(1, limit)
        floor = FUZZY_FLOOR
        best_by_pair: dict[tuple[str, str], tuple[SortKey, LookupCandidate]] = {}

        def record(idx: int, score: float, reason: str) -> float:
            nonlocal floor
            entry = entries[idx]
            sort_key: SortKey = (
                -score,
                CATEGORY_ORDER.get(entry.category, 999),
                source_priority(entry),
                len(entry.zh),
                entry.en,
                entry.source_id,
                idx,
            )
            pair = (entry.zh, entry.en)
            prev = best_by_pair.get(pair)
            if prev is None or sort_key < prev[0]:
                best_by_pair[pair] = (
                    sort_key,
                    LookupCandidate(entry=entry, score=score, reason=reason),
                )
                if len(best_by_pair) >= effective_limit and score > floor:
                    kth_neg = heapq.nsmallest(
                        effective_limit,
                        (item[0][0] for item in best_by_pair.values()),
                    )[-1]
                    candidate_floor = -kth_neg
                    if candidate_floor > floor:
                        floor = candidate_floor
            return floor

        fast_pinyin_rows: set[int] = set()
        if q_ascii:
            q_ascii_len = len(q_ascii)
            for idx in self._fast_pinyin_candidates(q_ascii):
                entry = entries[idx]
                py = entry.pinyin
                if py == q_ascii:
                    fast_pinyin_rows.add(idx)
                    record(idx, 100.0, "pinyin")
                elif py.startswith(q_ascii):
                    fast_pinyin_rows.add(idx)
                    score = max(80.0, 96.0 - (len(py) - q_ascii_len) * 0.5)
                    record(idx, score, "pinyin-prefix")
                elif entry.pinyin_abbrev == q_ascii:
                    fast_pinyin_rows.add(idx)
                    record(idx, 92.0, "pinyin-abbrev")
                elif q_ascii in py:
                    fast_pinyin_rows.add(idx)
                    record(idx, 86.0, "pinyin-substring")

        if q_norm and floor <= ZH_WEIGHT:
            floor = self._search_zh(
                q_norm, floor, fast_pinyin_rows, record
            )

        if q_ascii and floor <= PY_WEIGHT:
            floor = self._search_bitsliced(
                q=q_ascii,
                weight=PY_WEIGHT,
                occ_index=self._py_occ,
                len_masks=self._py_len_masks,
                targets=self._pinyins,
                target_lengths=self._py_lengths,
                floor=floor,
                excluded=fast_pinyin_rows,
                record=record,
            )

        if q_norm and floor <= EN_WEIGHT:
            floor = self._search_bitsliced(
                q=q_norm,
                weight=EN_WEIGHT,
                occ_index=self._en_occ,
                len_masks=self._en_len_masks,
                targets=self._en_norms,
                target_lengths=self._en_lengths,
                floor=floor,
                excluded=fast_pinyin_rows,
                record=record,
            )

        if not best_by_pair:
            return []

        ordered = sorted(best_by_pair.values(), key=lambda item: item[0])
        deduped: list[LookupCandidate] = []
        for _sort_key, candidate in ordered:
            deduped.append(candidate)
            if len(deduped) >= limit:
                break
        return deduped

    def _search_zh(
        self,
        q_norm: str,
        floor: float,
        excluded: set[int],
        record: Callable[[int, float, str], float],
    ) -> float:
        lq = len(q_norm)
        r_min = floor / ZH_WEIGHT
        denom = 2.0 - r_min
        if denom <= 0.0:
            return floor
        m_min = max(1, math.ceil(r_min * lq / denom - _BOUND_EPS))
        lt_min = m_min
        lt_max = math.floor(denom * lq / r_min + _BOUND_EPS)
        if lt_min > lt_max:
            return floor

        len_mask = 0
        zh_len_masks = self._zh_len_masks
        for lt in range(lt_min, lt_max + 1):
            lm = zh_len_masks.get(lt, 0)
            if lm:
                len_mask |= lm
        if len_mask == 0:
            return floor

        zh_char_counts = self._zh_char_counts
        zh_char_index = self._zh_char_index
        k = lq - m_min + 1
        ranked = sorted(
            ((zh_char_counts.get(ch, 0), ch) for ch in q_norm),
            key=lambda item: item[0],
        )
        prefix_chars = {ch for count, ch in ranked[:k] if count > 0}
        if not prefix_chars:
            return floor

        zh_lengths = self._zh_lengths
        cands: set[int] = set()
        bitset_union = 0
        for ch in prefix_chars:
            posting = zh_char_index[ch]
            if isinstance(posting, tuple):
                for idx in posting:
                    if lt_min <= zh_lengths[idx] <= lt_max and idx not in excluded:
                        cands.add(idx)
            else:
                bitset_union |= posting

        if bitset_union:
            masked = bitset_union & len_mask
            while masked:
                lsb = masked & -masked
                idx = lsb.bit_length() - 1
                masked ^= lsb
                if idx not in excluded:
                    cands.add(idx)

        if not cands:
            return floor

        q_items = tuple(Counter(q_norm).items())
        zh_norms = self._zh_norms
        scored_cands: list[tuple[float, int]] = []
        for idx in cands:
            target = zh_norms[idx]
            lt = zh_lengths[idx]
            ov = 0
            for ch, q_cnt in q_items:
                t_cnt = target.count(ch)
                if t_cnt:
                    ov += q_cnt if q_cnt < t_cnt else t_cnt
            ub = (2.0 * ov / (lq + lt)) * ZH_WEIGHT
            if ub + _BOUND_EPS >= floor:
                scored_cands.append((ub, idx))

        if not scored_cands:
            return floor

        scored_cands.sort(key=lambda item: -item[0])
        for ub, idx in scored_cands:
            if ub + _BOUND_EPS < floor:
                break
            score = SequenceMatcher(None, q_norm, zh_norms[idx]).ratio() * ZH_WEIGHT
            if score >= floor:
                floor = record(idx, score, "fuzzy")
        return floor

    def _search_bitsliced(
        self,
        *,
        q: str,
        weight: float,
        occ_index: dict[tuple[str, int], Posting],
        len_masks: dict[int, int],
        targets: tuple[str, ...],
        target_lengths: tuple[int, ...],
        floor: float,
        excluded: set[int],
        record: Callable[[int, float, str], float],
    ) -> float:
        lq = len(q)
        if lq == 0 or floor > weight:
            return floor

        r_min = floor / weight
        denom = 2.0 - r_min
        if denom <= 0.0:
            return floor
        lt_min = max(1, math.ceil(r_min * lq / denom - _BOUND_EPS))
        lt_max = math.floor(denom * lq / r_min + _BOUND_EPS)
        if lt_min > lt_max:
            return floor

        univ = 0
        for lt in range(lt_min, lt_max + 1):
            lm = len_masks.get(lt, 0)
            if lm:
                univ |= lm
        if univ == 0:
            return floor

        q_items = tuple(Counter(q).items())

        # Direct path when very few dictionary entries fall in [lt_min, lt_max].
        if univ.bit_count() <= _DIRECT_UNIV_THRESHOLD:
            direct: list[tuple[float, int]] = []
            m = univ
            while m:
                lsb = m & -m
                idx = lsb.bit_length() - 1
                m ^= lsb
                if idx in excluded:
                    continue
                target = targets[idx]
                lt = target_lengths[idx]
                ov = 0
                for ch, q_cnt in q_items:
                    t_cnt = target.count(ch)
                    if t_cnt:
                        ov += q_cnt if q_cnt < t_cnt else t_cnt
                ub = (2.0 * ov / (lq + lt)) * weight
                if ub + _BOUND_EPS >= floor:
                    direct.append((ub, idx))
            direct.sort(key=lambda item: -item[0])
            for ub, idx in direct:
                if ub + _BOUND_EPS < floor:
                    break
                score = SequenceMatcher(None, q, targets[idx]).ratio() * weight
                if score >= floor:
                    floor = record(idx, score, "fuzzy")
            return floor

        q_bitsets: list[int] = []
        seen: dict[str, int] = {}
        for ch in q:
            occ = seen.get(ch, 0) + 1
            seen[ch] = occ
            posting = occ_index.get((ch, occ))
            if posting is None:
                continue
            bs = _posting_to_mask(posting) & univ
            if bs:
                q_bitsets.append(bs)

        min_req = math.ceil(r_min * (lq + lt_min) / 2.0 - _BOUND_EPS)
        if len(q_bitsets) < min_req:
            return floor

        planes = _bit_add_all(q_bitsets)
        ov_masks = _eq_masks_min(planes, univ, max(1, min_req))
        if not ov_masks:
            return floor

        buckets: list[tuple[float, int]] = []
        for ov, omask in ov_masks.items():
            if ov == 0:
                continue
            if (2.0 * ov / (lq + lt_min)) * weight + _BOUND_EPS < floor:
                continue
            for lt in range(lt_min, lt_max + 1):
                lm = len_masks.get(lt, 0)
                if not lm:
                    continue
                ub = (2.0 * ov / (lq + lt)) * weight
                if ub + _BOUND_EPS < floor:
                    break
                mask = omask & lm
                if mask:
                    buckets.append((ub, mask))

        if not buckets:
            return floor

        buckets.sort(key=lambda item: -item[0])
        for ub, mask in buckets:
            if ub + _BOUND_EPS < floor:
                break
            m = mask
            while m:
                lsb = m & -m
                idx = lsb.bit_length() - 1
                m ^= lsb
                if idx in excluded:
                    continue
                score = SequenceMatcher(None, q, targets[idx]).ratio() * weight
                if score >= floor:
                    floor = record(idx, score, "fuzzy")
        return floor
