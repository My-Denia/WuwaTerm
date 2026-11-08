"""Bounded English plural admission shared by locking and review.

A legitimate plural of an official English term is one term, not a stranded
suffix: sentence locking must restore ``共鸣者`` for ``Resonators`` instead of
leaving ``共鸣者s``, and review must judge ``Resonators`` as the official form
and ``Type-S Restoratives`` as a mention of ``Type-S Restorative``. Both paths
therefore consume this one rule.

This is a bounded heuristic, not a morphological engine: an exact English
dictionary surface ending in at least two letters admits at most one orthodox
trailing suffix, ``s`` or ``es``.
"""

from __future__ import annotations

# Orthodox English plural endings that take "es". A bare trailing "s" that
# is not part of "ss" (Bus, Titus, Mephis) is deliberately NOT a trigger:
# those singulars stay exact, so "Buses" never locks "Bus". Nouns ending in
# consonant+o (Echo, Hero) also take "es"; vowel+o (Milo, Romeo) takes "s".
_ES_PLURAL_LOWER_TRIGGERS = ("ss", "x", "z", "ch", "sh")
_VOWELS_LOWER = frozenset("aeiou")


def _suffixes(surface: str) -> tuple[str, ...]:
    # A single-letter surface never inflects: "A"+"s" is not a plural, and
    # sentence locking already requires eligible surfaces to have two letters.
    if len(surface) < 2 or not (surface[-1].isascii() and surface[-1].isalpha()):
        return ()
    if any("\u3400" <= char <= "\u9fff" for char in surface):
        return ()
    lowered = surface.lower()
    if lowered.endswith("s"):
        return ("es",) if lowered.endswith("ss") else ()
    if lowered.endswith(_ES_PLURAL_LOWER_TRIGGERS):
        return ("es",)
    if lowered.endswith("o") and lowered[-2] not in _VOWELS_LOWER:
        return ("es",)
    return ("s",)


def plural_suffix_candidates(
    source: str, official: tuple[str, str]
) -> tuple[str, ...]:
    """Bounded trailing inflections admitted for an exact English surface.

    Only exact English official surfaces qualify (``source == official[1]``):
    Chinese surfaces and lowercase/all-caps case variants stay exact. The
    surface must end in an ASCII letter and carry no CJK character. At most
    ONE suffix is ever returned: plain "s" for ordinary endings, "es" for
    orthodox triggers (ss/x/z/ch/sh and consonant+o). Surfaces already
    ending in "s" get no plain "+s" — "Boss"+s is a double-s, not a plural —
    and no "es" unless the ending is "ss".
    """
    return _suffixes(source) if source == official[1] else ()


def plural_forms(surface: str) -> tuple[str, ...]:
    """The inflected spellings an already-official English surface admits.

    For callers that hold the official English surface itself and therefore
    need no source/official guard; case variants must go through
    ``plural_suffix_candidates`` instead.
    """
    return tuple(surface + suffix for suffix in _suffixes(surface))
