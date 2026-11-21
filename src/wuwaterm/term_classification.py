"""Pure classification of derived vs. established dictionary entries.

This module sits in domain core with no database or lookup dependency so both
pure matching (``term_matching``) and dictionary lookup (``lookup``) share one
definition of derived name provenance and exact-hit preference without pulling
``db`` into the matching layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from .constants import (
    ADVERTISING_VERSION_TITLE_RE,
    AREA_TITLE_KEY_RE,
    ARIKATSU_MULTITEXT_SOURCE_FILE,
    CORE_TERM_KEYS,
    ITEM_TIER_BASE_SOURCE_PREFIX,
)
from .models import LookupCandidate, TermEntry
from .normalize import normalize_text


def _is_cjk_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


@lru_cache(maxsize=8192)
def _normalized_surface(surface: str) -> str:
    """``normalize_text`` with a small cache for per-request surface checks."""
    return normalize_text(surface)


def is_derived_name_entry(entry: TermEntry) -> bool:
    """Return whether the builder derived ``entry`` instead of reading a pair.

    Three mechanisms do: an Area_<n>_Title key no area/area.json row
    references, imported from the TextMap as a location; an uncurated
    Advertising_VersionTitle_<n> key from the same file, which is a version
    title rather than a glossary pair; and the base pair of a complete
    four-tier item family (``tier-base:<id>``). A curated CORE_TERM_KEYS row
    shares the TextMap source file and stays an ordinary row.

    Term matching, sentence preparation, lookup and pair review all read this
    one predicate, so a surface they must not force is identical across paths.
    """
    if entry.source_id.startswith(ITEM_TIER_BASE_SOURCE_PREFIX):
        return True
    if entry.source_file != ARIKATSU_MULTITEXT_SOURCE_FILE:
        return False
    if entry.text_key in CORE_TERM_KEYS:
        return False
    if ADVERTISING_VERSION_TITLE_RE.fullmatch(entry.text_key) is not None:
        return True
    return entry.category == "location" and AREA_TITLE_KEY_RE.fullmatch(
        entry.text_key
    ) is not None


def derived_reference_only_sources(entries: Iterable[TermEntry]) -> frozenset[str]:
    """Return the surfaces free text references but never locks.

    A derived name row's Chinese surface is often also an everyday word
    (教学楼 is any school's teaching building, 能量袋 any energy bag), and no
    official row pairs it in a context that tells the two apart. It stays in
    span selection, so it still owns its span and the model receives it as a
    contextual reference, but it never becomes a placeholder: without a model
    the text is left as written. A surface any other row also carries, also
    under lookup normalization, keeps that row's behavior. Derived version
    titles are references on both sides because a title is never a fixed
    substitution; other English surfaces are not affected (en->zh still locks
    the exact "Academia Hall").
    """
    derived: set[str] = set()
    established: set[str] = set()
    version_titles: set[str] = set()
    for entry in entries:
        is_derived = is_derived_name_entry(entry)
        surfaces = derived if is_derived else established
        for surface in (entry.zh, entry.en):
            if surface:
                surfaces.add(surface)
                if is_derived and ADVERTISING_VERSION_TITLE_RE.fullmatch(
                    entry.text_key
                ):
                    version_titles.add(surface)
    established_norms = {_normalized_surface(surface) for surface in established}
    return frozenset(
        surface
        for surface in derived
        if (any(map(_is_cjk_character, surface)) or surface in version_titles)
        and normalize_text(surface) not in established_norms
    )


def select_preferred_exact_entry(
    candidates: Iterable[LookupCandidate],
) -> TermEntry | None:
    """Choose the exact hit a fixed substitution may restore, if any.

    An established row wins over a derived one so a builder-derived name never
    displaces an established dictionary pair. If every exact hit is derived,
    the first (highest-ranked) candidate is returned.
    """
    first: TermEntry | None = None
    for candidate in candidates:
        if first is None:
            first = candidate.entry
        if not is_derived_name_entry(candidate.entry):
            return candidate.entry
    return first
