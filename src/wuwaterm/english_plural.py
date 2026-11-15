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

import re

# Orthodox English plural endings that take "es". A bare trailing "s" that
# is not part of "ss" (Bus, Titus, Mephis) is deliberately NOT a trigger:
# those singulars stay exact, so "Buses" never locks "Bus". Nouns ending in
# consonant+o (Echo, Hero) also take "es"; vowel+o (Milo, Romeo) takes "s".
_ES_PLURAL_LOWER_TRIGGERS = ("ss", "x", "z", "ch", "sh")
_VOWELS_LOWER = frozenset("aeiou")
_TRAILING_ASCII_WORD_RE = re.compile(r"[A-Za-z]+$")

# Trailing "es" is only a separable plural suffix after these endings. Every
# other "es" belongs to a silent final "e" that takes a bare "s" (Prize,
# Shoe, Toe), so stripping it would invent a stem: Prizes -> Priz,
# Shoes -> Sho, Toes -> To. "zes" is deliberately not listed: it would make
# Prizes lose "es", and the genuine "zz" words (Buzzes) are covered by
# "zzes".
_ES_PLURAL_STRIPPABLE_ENDINGS = ("sses", "xes", "zzes", "ches", "shes")

# Endings whose "s" is part of the stem, never a plural marker: double-s
# (Boss), the Latin -us/-is singulars (Status, Genesis, Solaris), and the
# -os/-as names and loanwords (Chaos, Kronos, Atlas, Elias, Nas) whose
# stripped form would be a different word or a bare fragment.
_NON_PLURAL_S_ENDINGS = ("ss", "us", "is", "os", "as")

# Words spelled the same in singular and plural. Stripping their trailing
# "s" (or "es") invents a word that is not in the dictionary, and "mars"
# would additionally turn the month abbreviation "Mar" into the speaker
# name "Mars".
_INVARIANT_S_WORDS = frozenset({"lens", "mars", "news", "series", "species"})


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


def _match_trailing_case(source_suffix: str, replacement_lower: str) -> str:
    return replacement_lower.upper() if source_suffix.isupper() else replacement_lower


def number_variants(surface: str) -> tuple[str, ...]:
    """Bidirectional English singular/plural spellings for alignment and review.

    Admits both singular->plural and plural->singular inflections on the
    trailing ASCII word of ``surface``:

    - Singular -> plural: ``+s`` / ``+es`` per ``_suffixes``, consonant+``y`` ->
      ``-ies`` (``Story`` -> ``Stories``), and the exact trailing classical word
      ``phenomenon`` -> ``phenomena`` (never a general ``-on`` -> ``-a`` rule,
      so ``Weapon``, ``Beacon`` and ``Dragon`` stay regular).
    - Plural -> singular, bounded to suffixes that cannot belong to the stem:
      trailing word ``phenomena`` -> ``phenomenon``; consonant+``ies`` ->
      ``-y`` when the stem before ``ies`` has two letters, the singular is at
      least four letters and the word is not invariant (``Stories`` ->
      ``Story``, but never ``Lies`` -> ``Ly``, ``Series`` -> ``Sery``);
      ``sses``/``xes``/``zzes``/``ches``/``shes`` dropping ``es`` (``Bosses``
      -> ``Boss``, ``Boxes`` -> ``Box``); and ordinary trailing ``s`` dropping
      ``s`` (``Prizes`` -> ``Prize``, ``Shoes`` -> ``Shoe``) while excluding
      ``ss``/``us``/``is``/``os``/``as`` endings (``Boss``, ``Status``,
      ``Genesis``, ``Chaos``, ``Atlas``), the invariant words
      ``lens``/``news``/``mars``/``series``/``species``, and words whose stem
      would be shorter than three letters (``TDs`` -> ``TD``).

    The reverse direction is a bounded guess, so it only claims a spelling no
    real word owns; a wrong reverse form (``Echoes`` -> ``Echoe``) stays inert
    because nothing else matches it, while a wrong short stem (``Toes`` ->
    ``To``) would silently align or verify an unrelated word everywhere.
    """
    if len(surface) < 2 or not (surface[-1].isascii() and surface[-1].isalpha()):
        return ()
    if any("\u3400" <= char <= "\u9fff" for char in surface):
        return ()
    match = _TRAILING_ASCII_WORD_RE.search(surface)
    if match is None:
        return ()
    if (
        match.start() > 0
        and surface[match.start() - 1].isascii()
        and surface[match.start() - 1].isalnum()
    ):
        return ()
    word_lower = match.group(0).lower()
    variants: list[str] = []

    def add(candidate: str) -> None:
        if candidate != surface and candidate not in variants:
            variants.append(candidate)

    if word_lower == "phenomenon":
        add(surface[:-2] + _match_trailing_case(surface[-2:], "a"))
        return tuple(variants)
    if word_lower == "phenomena":
        add(surface[:-1] + _match_trailing_case(surface[-1:], "on"))
        return tuple(variants)

    if (
        len(word_lower) >= 2
        and word_lower.endswith("y")
        and word_lower[-2] not in _VOWELS_LOWER
    ):
        add(surface[:-1] + _match_trailing_case(surface[-1:], "ies"))
    else:
        for suffix in _suffixes(surface):
            add(surface + _match_trailing_case(surface[-1:], suffix))

    if word_lower in _INVARIANT_S_WORDS:
        return tuple(variants)

    # "Stories" -> "Story": the stem before "ies" needs two letters and the
    # derived word needs four, so "Lies" and "Cries" never become "Ly"/"Cry".
    if (
        len(word_lower) >= 6
        and word_lower.endswith("ies")
        and word_lower[-4] not in _VOWELS_LOWER
    ):
        add(surface[:-3] + _match_trailing_case(surface[-3:], "y"))
    elif len(word_lower) >= 4 and word_lower.endswith(
        _ES_PLURAL_STRIPPABLE_ENDINGS
    ):
        add(surface[:-2])
    elif (
        len(word_lower) >= 4
        and word_lower.endswith("s")
        and not word_lower.endswith(_NON_PLURAL_S_ENDINGS)
    ):
        add(surface[:-1])

    return tuple(variants)
