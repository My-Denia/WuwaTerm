"""Strict parsers for the builder's free-text lock exclusion metadata.

This module is shared policy infrastructure (SHARED layer): it owns the
validation contract for the two versioned metadata keys the builder writes
and the sentence translator and the review engine both read. It must stay
importable by every layer that consumes exclusion metadata without dragging
translator, provider, lookup, storage, Telegram or builder dependencies —
standard-library modules plus ``constants`` only.

The parse contract is frozen: signatures, return values, validation
predicates and error messages are byte-stable. ``sentence`` re-exports both
parsers for existing callers; the bodies live here once.
"""

from __future__ import annotations

import json
import unicodedata

from .constants import (
    FREE_TEXT_LOCK_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_MIN_OCCURRENCES,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION,
    FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO,
    FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES,
)


def _is_exact_int(value: object) -> bool:
    # bool is an int subclass; the metadata contract accepts exact ints only.
    return type(value) is int


def _is_cjk_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


def _has_latin_letter(text: str) -> bool:
    return any(
        char.isalpha() and unicodedata.name(char, "").startswith("LATIN ")
        for char in text
    )


def parse_free_text_lock_exclusions(value: str) -> tuple[tuple[str, int, int], ...]:
    """Parse the builder's free-text lock exclusion metadata value.

    Returns ``(surface, occurrences, aligned)`` rows. Raises ValueError unless
    the value has the current version and parameters, surfaces are unique and
    sorted, and every row meets the exclusion rule it was built with.
    """
    data = json.loads(value)
    if not isinstance(data, dict) or set(data) != {
        "version",
        "min_occurrences",
        "max_aligned_ratio",
        "surfaces",
    }:
        raise ValueError("unexpected exclusion object")
    if not _is_exact_int(data["version"]) or (
        data["version"] != FREE_TEXT_LOCK_EXCLUSIONS_VERSION
    ):
        raise ValueError("unsupported exclusion version")
    if not _is_exact_int(data["min_occurrences"]) or (
        data["min_occurrences"] != FREE_TEXT_LOCK_MIN_OCCURRENCES
    ):
        raise ValueError("exclusion min_occurrences mismatch")
    ratio = data["max_aligned_ratio"]
    if (
        not isinstance(ratio, list)
        or not all(map(_is_exact_int, ratio))
        or tuple(ratio) != FREE_TEXT_LOCK_MAX_ALIGNED_RATIO
    ):
        raise ValueError("exclusion max_aligned_ratio mismatch")
    numerator, denominator = FREE_TEXT_LOCK_MAX_ALIGNED_RATIO
    surfaces = data["surfaces"]
    if not isinstance(surfaces, list):
        raise ValueError("exclusion surfaces must be a list")
    rows: list[tuple[str, int, int]] = []
    for row in surfaces:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("exclusion row must be [surface, occurrences, aligned]")
        surface, occurrences, aligned = row
        if (
            type(surface) is not str
            or not _has_latin_letter(surface)
            or any(map(_is_cjk_character, surface))
            or "\n" in surface
        ):
            raise ValueError("exclusion surface must be single-line Latin text")
        if not _is_exact_int(occurrences) or not _is_exact_int(aligned):
            raise ValueError("exclusion counts must be integers")
        if not (
            occurrences >= FREE_TEXT_LOCK_MIN_OCCURRENCES
            and 0 <= aligned <= occurrences
            and aligned * denominator < occurrences * numerator
        ):
            raise ValueError("exclusion row does not meet the exclusion rule")
        if rows and surface <= rows[-1][0]:
            raise ValueError("exclusion surfaces must be unique and sorted")
        rows.append((surface, occurrences, aligned))
    return tuple(rows)


def parse_free_text_zh_lock_exclusions(value: str) -> tuple[tuple[str, int, int], ...]:
    """Parse the builder's Chinese-surface lock exclusion metadata value.

    Returns ``(surface, occurrences, aligned)`` rows under the same strict
    contract as the English key: current zh version and parameters, surfaces
    unique and sorted, and every row meeting the exclusion rule it was built
    with. The script check is the mirror image: each surface must be
    single-line and contain at least one CJK character (U+3400-U+9FFF);
    non-CJK characters around the han characters (brackets, digits) stay
    allowed, and unlike the English parser there is no Latin requirement.
    """
    data = json.loads(value)
    if not isinstance(data, dict) or set(data) != {
        "version",
        "min_occurrences",
        "max_aligned_ratio",
        "surfaces",
    }:
        raise ValueError("unexpected zh exclusion object")
    if not _is_exact_int(data["version"]) or (
        data["version"] != FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION
    ):
        raise ValueError("unsupported zh exclusion version")
    if not _is_exact_int(data["min_occurrences"]) or (
        data["min_occurrences"] != FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES
    ):
        raise ValueError("zh exclusion min_occurrences mismatch")
    ratio = data["max_aligned_ratio"]
    if (
        not isinstance(ratio, list)
        or not all(map(_is_exact_int, ratio))
        or tuple(ratio) != FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO
    ):
        raise ValueError("zh exclusion max_aligned_ratio mismatch")
    numerator, denominator = FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO
    surfaces = data["surfaces"]
    if not isinstance(surfaces, list):
        raise ValueError("zh exclusion surfaces must be a list")
    rows: list[tuple[str, int, int]] = []
    for row in surfaces:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("zh exclusion row must be [surface, occurrences, aligned]")
        surface, occurrences, aligned = row
        if (
            type(surface) is not str
            or not any(map(_is_cjk_character, surface))
            or "\n" in surface
        ):
            raise ValueError("zh exclusion surface must be single-line CJK text")
        if not _is_exact_int(occurrences) or not _is_exact_int(aligned):
            raise ValueError("zh exclusion counts must be integers")
        if not (
            occurrences >= FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES
            and 0 <= aligned <= occurrences
            and aligned * denominator < occurrences * numerator
        ):
            raise ValueError("zh exclusion row does not meet the exclusion rule")
        if rows and surface <= rows[-1][0]:
            raise ValueError("zh exclusion surfaces must be unique and sorted")
        rows.append((surface, occurrences, aligned))
    return tuple(rows)
