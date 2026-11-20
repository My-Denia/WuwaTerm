"""Pure dictionary-surface admission and term span selection.

The builder and sentence translation use the same exact default. Prepared
plain translation may opt into its existing unambiguous NFKC spellings.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .cjk_span import crosses_cjk_word_boundary, single_character_name_context_ok
from .english_plural import plural_suffix_candidates as _plural_suffix_candidates
from .models import TermEntry


@dataclass(frozen=True)
class _TermSpan:
    start: int
    end: int
    source: str
    official: tuple[str, str]
    order: int


def _is_cjk_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _ascii_word_boundaries_ok(text: str, start: int, end: int, source: str) -> bool:
    """Reject a term match glued to surrounding ASCII word characters.

    Without this, "New Echoes" locks the "Echo" span and restores to
    "New 声骸es". CJK terms use crosses_cjk_word_boundary instead: a
    multi-character term is dropped only when ordinary words cross both
    of its edges, as in 回声骸骨. Neighboring CJK text alone is not a veto.
    """
    if _is_ascii_word_char(source[0]) and start > 0 and _is_ascii_word_char(
        text[start - 1]
    ):
        return False
    if _is_ascii_word_char(source[-1]) and end < len(text) and _is_ascii_word_char(
        text[end]
    ):
        return False
    return True


def _is_eligible_lockable(source: str, official: tuple[str, str, str]) -> bool:
    return (
        (
            len(source) >= 2
            or (
                len(source) == 1
                and source == official[0]
                and official[2] == "resonator"
                and _is_cjk_character(source)
            )
        )
        and bool(official[0])
        and bool(official[1])
        and "\n" not in official[0]
        and "\n" not in official[1]
    )


def _nfkc_spelling(source: str) -> str:
    """Return the pure NFKC spelling of a dictionary surface.

    ``normalize_text`` also removes every space and casefolds, which would
    compare different text. Prepared plain input keeps its spacing and case,
    so only the NFKC step describes how a raw surface can occur there.
    """
    return unicodedata.normalize("NFKC", source)


def _admissible_nfkc_sources(
    sources: dict[str, tuple[str, str, str]],
    variants: list[tuple[str, tuple[str, str, str]]],
    excluded: frozenset[str],
) -> frozenset[str]:
    """Return raw surfaces whose pure-NFKC spelling is safe to compare too.

    Prepared plain text is NFKC while dictionary surfaces are stored raw, so
    a surface such as ``镜锁妄世，心照红尘`` never occurs in prepared text. The
    runtime may additionally locate its NFKC spelling, but only when that
    spelling is unambiguous and not excluded:

    - neither the raw surface nor the spelling is excluded, so a spelling
      change cannot bypass either exclusion metadata key;
    - every surface that already owns the spelling (another raw surface or a
      synthesized case variant) restores the same official pair. A
      different-official collision keeps the original exact behavior and
      adds no candidate instead of choosing a new winner by iteration order.

    Only the runtime's prepared plain-text paths consult this set; the
    selector's default, the builder's corpus measurement and protected text
    keep exact matching.
    """
    spellings = {source: _nfkc_spelling(source) for source in sources}
    changed = {source for source, spelling in spellings.items() if spelling != source}
    if not changed:
        return frozenset()
    owners: dict[str, set[tuple[str, str]]] = {}
    for source, official in sources.items():
        owners.setdefault(spellings[source], set()).add((official[0], official[1]))
    for surface, official in variants:
        owners.setdefault(surface, set()).add((official[0], official[1]))
    admitted: set[str] = set()
    for source in changed:
        spelling = spellings[source]
        if not spelling or source in excluded or spelling in excluded:
            continue
        if len(owners.get(spelling, ())) == 1:
            admitted.add(source)
    return frozenset(admitted)


# Categories whose English surfaces also lock when typed all-lowercase or
# all-caps. On the 3.7 dictionary resonator surfaces almost never appear in
# another casing in official English text, while speaker, item, skill and
# core_term surfaces often are ordinary words in other casings (It, Wish,
# Focus, Echo). Other categories keep exact-surface matching.
CASE_VARIANT_CATEGORIES = frozenset({"resonator"})


def _has_latin_letter(text: str) -> bool:
    return any(
        char.isalpha() and unicodedata.name(char, "").startswith("LATIN ")
        for char in text
    )


def _case_variant_sources(
    sources: dict[str, tuple[str, str, str]],
    excluded: frozenset[str] = frozenset(),
) -> list[tuple[str, tuple[str, str, str]]]:
    """Return lowercase/uppercase surfaces for case-variant category names.

    A variant is the exact Unicode lower() or upper() of an eligible English
    surface, never a mixed casing. ``sources`` maps each surface to the record
    it already locks to, so a variant inherits exactly the record its official
    casing restores; lower-priority rows sharing that same surface never lock
    and do not block it. Distinct surfaces in any category that share a
    casefold key but lock to different (zh, en) records get no variant, and a
    variant that is itself a dictionary source is left to that exact entry.
    A surface in ``excluded`` gets no variant but still counts for both checks.
    """
    records_by_key: dict[str, set[tuple[str, str]]] = {}
    for source, official in sources.items():
        if not _is_eligible_lockable(source, official):
            continue
        if not _has_latin_letter(source) or any(map(_is_cjk_character, source)):
            continue
        records_by_key.setdefault(source.casefold(), set()).add(
            (official[0], official[1])
        )

    variants: list[tuple[str, tuple[str, str, str]]] = []
    emitted: set[str] = set()
    for source, official in sources.items():
        if source != official[1] or official[2] not in CASE_VARIANT_CATEGORIES:
            continue
        if source in excluded:
            continue
        if len(records_by_key.get(source.casefold(), ())) != 1:
            continue
        for variant in (source.lower(), source.upper()):
            if (
                variant != source
                # Do not synthesize this ordinary word as a name alias.
                and variant != "encore"
                and len(variant) == len(source)
                and variant not in sources
                and variant not in emitted
            ):
                emitted.add(variant)
                variants.append((variant, official))
    return variants


def _is_latin_word_char(char: str) -> bool:
    return _is_ascii_word_char(char) or (
        char.isalpha() and unicodedata.name(char, "").startswith("LATIN ")
    )


def _case_variant_boundaries_ok(text: str, start: int, end: int) -> bool:
    """Reject a case-variant match glued to Latin letters or ASCII digits.

    Variants may start or end with non-ASCII Latin letters (jué, élodie),
    which the ASCII-only check does not cover, so xélodie or élodie ruès
    would otherwise lock. Adjacent CJK text stays allowed.
    """
    if start > 0 and _is_latin_word_char(text[start - 1]):
        return False
    if end < len(text) and _is_latin_word_char(text[end]):
        return False
    return True


def _matched_span_ok(
    context_text: str,
    context_offset: int,
    start: int,
    end: int,
    spelling: str,
    official: tuple[str, str],
) -> bool:
    """Apply the existing matching gates to the spelling actually found.

    ASCII word boundaries are decided against the CONTEXT the match lives in
    (the full visible text for HTML segments, the text itself for plain
    input): an invisible tag between "Echo" and "s" (visible "Echos") must
    not make the glued surface look word-bounded, and text before an opening
    tag must not either. Plain callers pass (text, 0), so their behavior is
    bit-identical.

    ``spelling`` is the matched text: for an exact surface it equals the
    dictionary surface; for the runtime's NFKC comparison candidate it is the
    prepared spelling, so the span length, ASCII edges and single-character
    context all follow the text that is actually present.
    """
    return (
        _ascii_word_boundaries_ok(
            context_text, context_offset + start, context_offset + end, spelling
        )
        and (
            spelling in official
            or _case_variant_boundaries_ok(
                context_text, context_offset + start, context_offset + end
            )
        )
        and (
            len(spelling) > 1
            or single_character_name_context_ok(
                context_text, context_offset + start, context_offset + end
            )
        )
        and not crosses_cjk_word_boundary(
            context_text, context_offset + start, context_offset + end
        )
    )


def select_term_spans(
    text: str,
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    context_text: str,
    context_offset: int,
    *,
    allow_plural_inflection: bool = False,
    nfkc_spelling_sources: frozenset[str] = frozenset(),
) -> list[_TermSpan]:
    """Return the term spans ``text`` locks, ordered by start position.

    ``context_text`` is the surrounding visible text that ``text`` starts at
    ``context_offset`` in; plain text passes itself with offset 0.

    ``allow_plural_inflection`` is the C1 opt-in, default off: when true, a
    bounded trailing English plural (``s``/``es`` per
    ``_plural_suffix_candidates``) is admitted past the exact-surface match
    for exact English surfaces ending in an ASCII letter, with the suffix
    consumed into the span so restore replaces the inflected form whole.
    It is enabled only by the translation paths that already know the target
    is Chinese; every other caller (public ``lock_terms``, the builder's
    exclusion mining) keeps exact current behavior.

    ``nfkc_spelling_sources`` is the runtime's explicit prepared-plain-text
    opt-in, default empty: for each raw surface in the set, its pure NFKC
    spelling is also located when that spelling differs, and the span covers
    the actual matched text while keeping the raw dictionary surface. The
    runtime supplies only surfaces whose spelling is unambiguous and not
    excluded; the builder measurement, protected text and every other caller
    stay exact.
    """
    spans: list[_TermSpan] = []
    for order, (source, official) in enumerate(lockable):
        start = text.find(source)
        while start != -1:
            end = start + len(source)
            if _matched_span_ok(
                context_text, context_offset, start, end, source, official
            ):
                spans.append(
                    _TermSpan(
                        start=start,
                        end=end,
                        source=source,
                        official=official,
                        order=order,
                    )
                )
            start = text.find(source, start + 1)

    # The runtime's prepared plain-text candidate: the dictionary surface is
    # raw, the text is NFKC. Only surfaces the runtime admitted are compared
    # through their other spelling, and the span still covers the text that
    # is actually there while keeping the raw source for reference identity.
    if nfkc_spelling_sources:
        # Preserve an existing exact match's source identity at the same bounds.
        exact_bounds = {(span.start, span.end) for span in spans}
        for order, (source, official) in enumerate(lockable):
            if source not in nfkc_spelling_sources:
                continue
            spelling = _nfkc_spelling(source)
            if not spelling or spelling == source:
                continue
            start = text.find(spelling)
            while start != -1:
                end = start + len(spelling)
                if (start, end) not in exact_bounds and _matched_span_ok(
                    context_text, context_offset, start, end, spelling, official
                ):
                    spans.append(
                        _TermSpan(
                            start=start,
                            end=end,
                            source=source,
                            official=official,
                            order=order,
                        )
                    )
                start = text.find(spelling, start + 1)

    if allow_plural_inflection:
        # Plural spans join the same global longest selection as the spans
        # selected above. Only exact English surfaces inflect, so
        # ``source in official`` holds and the case-variant boundary branch is
        # not needed here; the suffix is consumed into the span, so ASCII
        # word-glue right after the inflection ("Resonatorses") still vetoes
        # the match. A span selected before plural synthesis always wins a
        # bounds tie: when the synthesized plural of one
        # surface is itself another exact dictionary surface (``Star`` vs
        # ``Stars`` -> 星星/群星, ``Luca`` vs ``Lucas``), the exact entry must
        # keep the span regardless of dictionary iteration order.
        exact_bounds = {(span.start, span.end) for span in spans}
        for order, (source, official) in enumerate(lockable):
            for suffix in _plural_suffix_candidates(source, official):
                inflected = source + suffix
                start = text.find(inflected)
                while start != -1:
                    end = start + len(inflected)
                    if (start, end) not in exact_bounds and (
                        # Same context rule as exact spans: a plural suffix
                        # glued across an invisible tag ("Resonator</b>s",
                        # visible "Resonators") is neither an exact nor an
                        # inflected match of the segment alone — the surface
                        # stays unlocked instead of restoring a stranded
                        # suffix (共鸣者s).
                        _ascii_word_boundaries_ok(
                            context_text,
                            context_offset + start,
                            context_offset + end,
                            inflected,
                        )
                        and (
                            source in official
                            or _case_variant_boundaries_ok(
                                context_text,
                                context_offset + start,
                                context_offset + end,
                            )
                        )
                        and (
                            len(source) > 1
                            or single_character_name_context_ok(
                                context_text,
                                context_offset + start,
                                context_offset + end,
                            )
                        )
                        and not crosses_cjk_word_boundary(
                            context_text, context_offset + start, context_offset + end
                        )
                    ):
                        spans.append(
                            _TermSpan(
                                start=start,
                                end=end,
                                source=inflected,
                                official=official,
                                order=order,
                            )
                        )
                    start = text.find(inflected, start + 1)

    # Select global longest non-overlapping official term spans. This
    # intentionally prioritizes the longest single term, not maximum total
    # coverage. Equal-length overlaps follow dictionary iteration order,
    # then start position, then source text.
    selected: list[_TermSpan] = []
    occupied: list[tuple[int, int]] = []
    for span in sorted(
        spans,
        key=lambda item: (
            -(item.end - item.start),
            item.order,
            item.start,
            item.source,
        ),
    ):
        if any(span.start < end and start < span.end for start, end in occupied):
            continue
        selected.append(span)
        occupied.append((span.start, span.end))

    selected.sort(key=lambda item: item.start)
    return selected


def _exact_lockable_sources(
    entries: Iterable[TermEntry],
) -> dict[str, tuple[str, str, str]]:
    # Entries arrive in lookup priority order, so the first record carrying a
    # surface owns it.
    sources: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        if "\n" in entry.zh or "\n" in entry.en:
            continue
        official = (entry.zh, entry.en, entry.category)
        if entry.zh:
            sources.setdefault(entry.zh, official)
        if entry.en:
            sources.setdefault(entry.en, official)
    return sources


def exact_lockable_sources(
    entries: Iterable[TermEntry],
) -> tuple[tuple[str, tuple[str, str]], ...]:
    """Return the eligible exact dictionary surfaces free text can lock.

    This is the runtime lockable set before case variants and free-text
    exclusions, in runtime order, for the builder's corpus measurement.
    """
    return tuple(
        (source, (official[0], official[1]))
        for source, official in _exact_lockable_sources(entries).items()
        if _is_eligible_lockable(source, official)
    )
