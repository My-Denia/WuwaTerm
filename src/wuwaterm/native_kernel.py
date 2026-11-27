"""Optional native Rust text-kernel loader with transparent pure-Python fallback.

This module provides a single pure-algorithm entry point for ``text_spans``,
``segmentation-v1``, Unicode scalar <-> UTF-16 coordinate conversion, and
multi-pattern Aho-Corasick term scanning.

If the optional compiled PyO3 extension ``_wuwaterm_text_kernel`` (built from
``crates/wuwaterm-text-kernel-py``) is installed and not disabled via
``WUWATERM_DISABLE_NATIVE_KERNEL=1``, calls delegate to the Rust kernel.
Otherwise every entry point transparently falls back to pure Python with
identical Unicode-scalar semantics and zero native build requirements.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
import importlib
import os
from types import ModuleType
from typing import Literal

from . import segmentation as _py_segmentation
from . import text_spans as _py_text_spans
from .segmentation import Region, SEGMENT_LIMIT, Segment, SegmentPair

BackendName = Literal["native", "python"]

_NATIVE_MODULE_NAME = "_wuwaterm_text_kernel"
_DISABLE_ENV = "WUWATERM_DISABLE_NATIVE_KERNEL"

_UNSET = object()
_native_module_cache: ModuleType | None | object = _UNSET


def _is_native_disabled() -> bool:
    raw = os.environ.get(_DISABLE_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _load_native_module() -> ModuleType | None:
    global _native_module_cache
    if _is_native_disabled():
        return None
    if _native_module_cache is _UNSET:
        try:
            _native_module_cache = importlib.import_module(_NATIVE_MODULE_NAME)
        except ImportError:
            _native_module_cache = None
    assert _native_module_cache is not _UNSET
    return _native_module_cache  # type: ignore[return-value]


def reset_native_kernel_cache() -> None:
    """Clear the cached native-module probe result (primarily for tests)."""
    global _native_module_cache
    _native_module_cache = _UNSET


def native_kernel_available() -> bool:
    """Return True when the optional Rust PyO3 extension is loaded and enabled."""
    return _load_native_module() is not None


def kernel_backend(*, prefer_native: bool = True) -> BackendName:
    """Return the active backend name (``'native'`` or ``'python'``)."""
    if prefer_native and _load_native_module() is not None:
        return "native"
    return "python"


# ---------------------------------------------------------------------------
# Coordinate conversion (Unicode scalar <-> UTF-16 code units)
# ---------------------------------------------------------------------------


def scalar_to_utf16(text: str, scalar: int, *, prefer_native: bool = True) -> int:
    """Convert a Unicode scalar offset in ``text`` to a UTF-16 code-unit offset."""
    if scalar < 0:
        raise ValueError("scalar offset must be non-negative")
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return int(mod.scalar_to_utf16(text, scalar))
    if text.isascii():
        return min(scalar, len(text))
    u16 = 0
    for ch in text[:scalar]:
        u16 += 2 if ord(ch) > 0xFFFF else 1
    return u16


def utf16_to_scalar(text: str, utf16: int, *, prefer_native: bool = True) -> int:
    """Convert a UTF-16 code-unit offset to the Unicode scalar offset at or before it."""
    if utf16 < 0:
        raise ValueError("utf16 offset must be non-negative")
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return int(mod.utf16_to_scalar(text, utf16))
    if text.isascii():
        return min(utf16, len(text))
    u16_pos = 0
    scalar_pos = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if u16_pos + width > utf16:
            break
        u16_pos += width
        scalar_pos += 1
    return scalar_pos


# ---------------------------------------------------------------------------
# text_spans & segmentation-v1 wrappers
# ---------------------------------------------------------------------------


def segment_core(
    text: str, start: int, end: int, *, prefer_native: bool = True
) -> tuple[int, int]:
    """Core of ``[start, end)`` with spec whitespace trimmed."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        a, b = mod.segment_core(text, start, end)
        return int(a), int(b)
    return _py_text_spans.segment_core(text, start, end)


def paragraph_ranges(text: str, *, prefer_native: bool = True) -> list[tuple[int, int]]:
    """Paragraph regions; empty when ``text`` has no non-WS scalar."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return [(int(a), int(b)) for a, b in mod.paragraph_ranges(text)]
    return _py_text_spans.paragraph_ranges(text)


def sentence_ranges(text: str, *, prefer_native: bool = True) -> list[tuple[int, int]]:
    """V1 sentence ranges splitting on terminators and newlines."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return [(int(a), int(b)) for a, b in mod.sentence_ranges(text)]
    return _py_text_spans.sentence_ranges(text)


def sentence_ranges_v2(
    text: str, *, prefer_native: bool = True
) -> list[tuple[int, int]]:
    """V2 sentence ranges (segmentation-v1 contract: preserves decimal dots)."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return [(int(a), int(b)) for a, b in mod.sentence_ranges_v2(text)]
    return _py_text_spans.sentence_ranges_v2(text)


def review_sentence_ranges_v2(
    text: str, *, prefer_native: bool = True
) -> list[tuple[int, int]]:
    """Review-v2 sentence ranges (also keeps list markers attached)."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return [(int(a), int(b)) for a, b in mod.review_sentence_ranges_v2(text)]
    return _py_text_spans.review_sentence_ranges_v2(text)


def segment_text(
    text: str, limit: int = SEGMENT_LIMIT, *, prefer_native: bool = True
) -> list[Segment]:
    """Cut ``text`` into ``segmentation-v1`` segments of at most ``limit`` core scalars."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        return [
            Segment(int(s), int(e), int(cs), int(ce), bool(hc))
            for s, e, cs, ce, hc in mod.segment_text(text, limit)
        ]
    return _py_segmentation.segment_text(text, limit)


def pair_segments(
    source: str,
    target: str,
    limit: int = SEGMENT_LIMIT,
    alignments: Sequence[Mapping[str, object]] | None = None,
    *,
    prefer_native: bool = True,
) -> list[SegmentPair]:
    """Pair ``source`` and ``target`` text per ``segmentation-v1``."""
    mod = _load_native_module() if prefer_native else None
    if mod is not None:
        rows = mod.pair_segments(source, target, limit, alignments)
        return [
            SegmentPair(
                index=int(idx),
                status=str(status),
                hard_cut=bool(hard_cut),
                source=Region(int(src[0]), int(src[1]), int(src[2]), int(src[3])),
                target=Region(int(tgt[0]), int(tgt[1]), int(tgt[2]), int(tgt[3])),
            )
            for idx, status, hard_cut, src, tgt in rows
        ]
    return _py_segmentation.pair_segments(source, target, limit, alignments)


# ---------------------------------------------------------------------------
# Aho-Corasick multi-pattern term scanner + WuwaTerm interval selection
# ---------------------------------------------------------------------------


class _PyTermAutomaton:
    """Pure-Python Unicode-scalar Aho-Corasick fallback matching Rust semantics."""

    def __init__(self, surfaces: Sequence[str]) -> None:
        self._surfaces = tuple(surfaces)
        self._children: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._outputs: list[list[int]] = [[]]

        for idx, pat in enumerate(self._surfaces):
            if not pat:
                continue
            cur = 0
            for ch in pat:
                nxt = self._children[cur].get(ch)
                if nxt is None:
                    nxt = len(self._children)
                    self._children[cur][ch] = nxt
                    self._children.append({})
                    self._fail.append(0)
                    self._outputs.append([])
                cur = nxt
            self._outputs[cur].append(idx)

        q: deque[int] = deque()
        for child in self._children[0].values():
            self._fail[child] = 0
            q.append(child)

        while q:
            state = q.popleft()
            for ch, child in self._children[state].items():
                f = self._fail[state]
                while f != 0 and ch not in self._children[f]:
                    f = self._fail[f]
                target = self._children[f].get(ch, 0)
                self._fail[child] = target
                if self._outputs[target]:
                    self._outputs[child].extend(self._outputs[target])
                q.append(child)

    @property
    def pattern_count(self) -> int:
        return len(self._surfaces)

    @property
    def state_count(self) -> int:
        return len(self._children)

    def find_all(self, text: str) -> list[tuple[int, int, int]]:
        if not text or not self._surfaces:
            return []
        matches: list[tuple[int, int, int]] = []
        state = 0
        children = self._children
        fail = self._fail
        outputs = self._outputs
        surfaces = self._surfaces
        for pos, ch in enumerate(text):
            while state != 0 and ch not in children[state]:
                state = fail[state]
            state = children[state].get(ch, 0)
            outs = outputs[state]
            if outs:
                end = pos + 1
                for pat_idx in outs:
                    pat = surfaces[pat_idx]
                    matches.append((end - len(pat), end, pat_idx))
        return matches

    def scan_and_select(self, text: str) -> list[tuple[int, int, int]]:
        raw = self.find_all(text)
        return select_longest_non_overlapping(raw, self._surfaces)


def select_longest_non_overlapping(
    matches: Sequence[tuple[int, int, int]],
    surfaces: Sequence[str],
) -> list[tuple[int, int, int]]:
    """WuwaTerm global longest non-overlapping interval selection.

    Matches ``term_matching.select_term_spans`` tie-breaking:
    ``(-(end - start), order, start, surface)`` then sorted by ``start``.
    """
    ordered = sorted(
        matches,
        key=lambda item: (
            -(item[1] - item[0]),
            item[2],
            item[0],
            surfaces[item[2]],
        ),
    )
    selected: list[tuple[int, int, int]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, order in ordered:
        if any(start < oe and os < end for os, oe in occupied):
            continue
        occupied.append((start, end))
        selected.append((start, end, order))
    selected.sort(key=lambda item: item[0])
    return selected


class TermScanner:
    """Multi-pattern exact term scanner backed by Rust PyO3 or pure Python.

    Preserves exact overlapping emissions, dictionary surface order priority,
    and Unicode scalar ``[start, end)`` coordinates across both backends.
    """

    def __init__(
        self, surfaces: Sequence[str], *, prefer_native: bool = True
    ) -> None:
        self._surfaces = list(surfaces)
        mod = _load_native_module() if prefer_native else None
        if mod is not None:
            self._backend: BackendName = "native"
            self._impl: object = mod.TermAutomaton(self._surfaces)
        else:
            self._backend = "python"
            self._impl = _PyTermAutomaton(self._surfaces)

    @property
    def backend(self) -> BackendName:
        return self._backend

    @property
    def pattern_count(self) -> int:
        return int(getattr(self._impl, "pattern_count"))

    @property
    def state_count(self) -> int:
        return int(getattr(self._impl, "state_count"))

    def find_all(self, text: str) -> list[tuple[int, int, int]]:
        """Return every exact overlapping occurrence as ``(start, end, order)``."""
        raw = getattr(self._impl, "find_all")(text)
        return [(int(a), int(b), int(o)) for a, b, o in raw]

    def scan_and_select(self, text: str) -> list[tuple[int, int, int]]:
        """Return globally selected longest non-overlapping ``(start, end, order)`` spans."""
        selected = getattr(self._impl, "scan_and_select")(text)
        return [(int(a), int(b), int(o)) for a, b, o in selected]
