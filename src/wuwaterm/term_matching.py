"""Pure dictionary-surface admission and term span selection.

The builder and sentence translation use the same exact default. Prepared
plain translation may opt into its existing unambiguous NFKC spellings.
"""

from __future__ import annotations

import threading
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass

from .cjk_span import (
    SINGLE_CHAR_NAME_MENTION,
    classify_single_character_mention,
    crosses_cjk_word_boundary,
    single_character_name_context_ok,
)
from .constants import CATEGORY_ORDER
from .english_plural import plural_suffix_candidates as _plural_suffix_candidates
from .models import TermEntry
from .term_classification import is_derived_name_entry


ReferenceCandidates = dict[str, tuple[tuple[str, str, str], ...]]


@dataclass(frozen=True)
class TermSpan:
    start: int
    end: int
    source: str
    official: tuple[str, str]
    order: int


def is_cjk_character(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff"


def reference_span_admitted(
    span: TermSpan, context_text: str, context_offset: int
) -> bool:
    """Apply review's one-character admission to an already selected span.

    A one-character dictionary surface is eligible, but only the surrounding
    text can say whether this occurrence names someone: 欢迎椿 does, the 椿
    inside 红椿蕊 does not. Translation and review consult the same helper, so
    whichever name reaches the model is also the name review reports.
    """
    return (
        len(span.source) != 1
        or not is_cjk_character(span.source)
        or classify_single_character_mention(
            context_text, context_offset + span.start, context_offset + span.end
        )
        == SINGLE_CHAR_NAME_MENTION
    )


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


def is_eligible_lockable(source: str, official: tuple[str, str, str]) -> bool:
    return (
        (
            len(source) >= 2
            or (
                len(source) == 1
                and source == official[0]
                and official[2] == "resonator"
                and is_cjk_character(source)
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


def admissible_nfkc_sources(
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


def case_variant_sources(
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
        if not is_eligible_lockable(source, official):
            continue
        if not _has_latin_letter(source) or any(map(is_cjk_character, source)):
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


def has_latin_word_boundaries(text: str, start: int, end: int) -> bool:
    """Reject a Latin-script match glued to surrounding Latin letters or digits.

    Used by case-variant matching and by occurrence scanning of derived
    soft-casing surfaces so ``tuner`` does not match inside ``retuner`` or
    ``tuners``. Adjacent CJK text stays allowed.
    """
    if start > 0 and _is_latin_word_char(text[start - 1]):
        return False
    if end < len(text) and _is_latin_word_char(text[end]):
        return False
    return True


def _case_variant_boundaries_ok(text: str, start: int, end: int) -> bool:
    """Reject a case-variant match glued to Latin letters or ASCII digits.

    Variants may start or end with non-ASCII Latin letters (jué, élodie),
    which the ASCII-only check does not cover, so xélodie or élodie ruès
    would otherwise lock. Adjacent CJK text stays allowed.
    """
    return has_latin_word_boundaries(text, start, end)


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


_KIND_EXACT = 0
_KIND_NFKC = 1
_KIND_PLURAL = 2
_EMPTY_CHILDREN: dict[str, int] = {}

# (kind, order, match_len, span_source, official, gate_text)
# - EXACT:  span_source == surface, gate_text == surface
# - NFKC:   span_source == raw surface, gate_text == NFKC spelling
# - PLURAL: span_source == inflected surface, gate_text == base surface
_ACPattern = tuple[int, int, int, str, tuple[str, str], str]


class TermSpanMatcher:
    """Immutable pure-Python Unicode-scalar Aho-Corasick term span matcher.

    Compiled once per dictionary snapshot (or cached by lockable tuple
    identity for standalone callers) to scan text in a single O(len(text)) pass
    while preserving 100% of the exact, NFKC-spelling, plural-inflection,
    ``exact_bounds`` priority, boundary-gate, and global longest non-overlapping
    selection semantics of :func:`select_term_spans`.
    """

    __slots__ = (
        "_children",
        "_fail",
        "_outputs",
        "lockable",
        "nfkc_spelling_sources",
        "include_plural",
        "pattern_count",
        "state_count",
    )

    def __init__(
        self,
        *,
        lockable: tuple[tuple[str, tuple[str, str]], ...],
        nfkc_spelling_sources: frozenset[str],
        include_plural: bool,
        children: tuple[dict[str, int], ...],
        fail: tuple[int, ...],
        outputs: tuple[tuple[_ACPattern, ...] | None, ...],
        pattern_count: int,
    ) -> None:
        self.lockable = lockable
        self.nfkc_spelling_sources = nfkc_spelling_sources
        self.include_plural = include_plural
        self._children = children
        self._fail = fail
        self._outputs = outputs
        self.pattern_count = pattern_count
        self.state_count = len(children)

    @classmethod
    def build(
        cls,
        lockable: tuple[tuple[str, tuple[str, str]], ...],
        *,
        nfkc_spelling_sources: frozenset[str] = frozenset(),
        include_plural: bool = True,
    ) -> TermSpanMatcher:
        children: list[dict[str, int]] = [{}]
        fail: list[int] = [0]
        raw_outputs: list[list[_ACPattern] | None] = [None]
        pattern_count = 0

        def add_pattern(pattern: str, entry: _ACPattern) -> None:
            nonlocal pattern_count
            if not pattern:
                return
            pattern_count += 1
            cur = 0
            for ch in pattern:
                cur_map = children[cur]
                nxt = cur_map.get(ch)
                if nxt is None:
                    if cur_map is _EMPTY_CHILDREN:
                        cur_map = {}
                        children[cur] = cur_map
                    nxt = len(children)
                    cur_map[ch] = nxt
                    children.append(_EMPTY_CHILDREN)
                    fail.append(0)
                    raw_outputs.append(None)
                cur = nxt
            bucket = raw_outputs[cur]
            if bucket is None:
                raw_outputs[cur] = [entry]
            else:
                bucket.append(entry)

        for order, (source, official) in enumerate(lockable):
            if source:
                add_pattern(
                    source,
                    (_KIND_EXACT, order, len(source), source, official, source),
                )

        if nfkc_spelling_sources:
            for order, (source, official) in enumerate(lockable):
                if source not in nfkc_spelling_sources:
                    continue
                spelling = _nfkc_spelling(source)
                if not spelling or spelling == source:
                    continue
                add_pattern(
                    spelling,
                    (_KIND_NFKC, order, len(spelling), source, official, spelling),
                )

        if include_plural:
            for order, (source, official) in enumerate(lockable):
                for suffix in _plural_suffix_candidates(source, official):
                    inflected = source + suffix
                    add_pattern(
                        inflected,
                        (
                            _KIND_PLURAL,
                            order,
                            len(inflected),
                            inflected,
                            official,
                            source,
                        ),
                    )

        outputs: list[tuple[_ACPattern, ...] | None] = [None] * len(children)
        queue: deque[int] = deque()
        for child in children[0].values():
            fail[child] = 0
            child_raw = raw_outputs[child]
            if child_raw is not None:
                outputs[child] = tuple(child_raw)
            queue.append(child)

        while queue:
            state = queue.popleft()
            state_children = children[state]
            if not state_children:
                continue
            state_fail = fail[state]
            for ch, child in state_children.items():
                f = state_fail
                while f != 0 and ch not in children[f]:
                    f = fail[f]
                target = children[f].get(ch, 0)
                fail[child] = target
                child_raw = raw_outputs[child]
                target_out = outputs[target]
                if child_raw is None:
                    if target_out is not None:
                        outputs[child] = target_out
                elif target_out is None:
                    outputs[child] = tuple(child_raw)
                else:
                    outputs[child] = (*child_raw, *target_out)
                queue.append(child)

        return cls(
            lockable=lockable,
            nfkc_spelling_sources=nfkc_spelling_sources,
            include_plural=include_plural,
            children=tuple(children),
            fail=tuple(fail),
            outputs=tuple(outputs),
            pattern_count=pattern_count,
        )

    def can_serve(
        self,
        *,
        allow_plural_inflection: bool,
        nfkc_spelling_sources: frozenset[str],
    ) -> bool:
        if allow_plural_inflection and not self.include_plural:
            return False
        if not nfkc_spelling_sources:
            return True
        if (
            nfkc_spelling_sources is self.nfkc_spelling_sources
            or nfkc_spelling_sources == self.nfkc_spelling_sources
        ):
            return True
        return nfkc_spelling_sources <= self.nfkc_spelling_sources

    def select_spans(
        self,
        text: str,
        context_text: str,
        context_offset: int,
        *,
        allow_plural_inflection: bool = False,
        nfkc_spelling_sources: frozenset[str] = frozenset(),
    ) -> list[TermSpan]:
        if not text or self.pattern_count == 0:
            return []

        want_nfkc = bool(nfkc_spelling_sources)
        nfkc_filter = want_nfkc and (
            nfkc_spelling_sources is not self.nfkc_spelling_sources
            and nfkc_spelling_sources != self.nfkc_spelling_sources
        )
        want_plural = allow_plural_inflection

        children = self._children
        fail = self._fail
        outputs = self._outputs

        exact_hits: list[tuple[int, int, int, str, tuple[str, str]]] = []
        nfkc_hits: list[tuple[int, int, int, str, tuple[str, str], str]] = []
        plural_hits: list[tuple[int, int, int, str, tuple[str, str], str]] = []

        state = 0
        cur_children = children[0]
        for pos, ch in enumerate(text):
            nxt = cur_children.get(ch)
            while nxt is None and state != 0:
                state = fail[state]
                cur_children = children[state]
                nxt = cur_children.get(ch)
            if nxt is not None:
                state = nxt
                cur_children = children[state]
                outs = outputs[state]
                if outs is not None:
                    end = pos + 1
                    for kind, order, length, span_source, official, gate_text in outs:
                        if kind == _KIND_EXACT:
                            exact_hits.append(
                                (end - length, end, order, span_source, official)
                            )
                        elif kind == _KIND_NFKC:
                            if want_nfkc and (
                                not nfkc_filter
                                or span_source in nfkc_spelling_sources
                            ):
                                nfkc_hits.append(
                                    (
                                        end - length,
                                        end,
                                        order,
                                        span_source,
                                        official,
                                        gate_text,
                                    )
                                )
                        elif want_plural:
                            plural_hits.append(
                                (
                                    end - length,
                                    end,
                                    order,
                                    span_source,
                                    official,
                                    gate_text,
                                )
                            )

        spans: list[TermSpan] = []
        for start, end, order, source, official in exact_hits:
            if _matched_span_ok(
                context_text, context_offset, start, end, source, official
            ):
                spans.append(
                    TermSpan(
                        start=start,
                        end=end,
                        source=source,
                        official=official,
                        order=order,
                    )
                )

        if nfkc_hits:
            # Preserve an existing exact match's source identity at the same bounds.
            exact_bounds = {(span.start, span.end) for span in spans}
            for start, end, order, source, official, spelling in nfkc_hits:
                if (start, end) not in exact_bounds and _matched_span_ok(
                    context_text, context_offset, start, end, spelling, official
                ):
                    spans.append(
                        TermSpan(
                            start=start,
                            end=end,
                            source=source,
                            official=official,
                            order=order,
                        )
                    )

        if plural_hits:
            # Plural spans join the same global longest selection as the spans
            # selected above. A span selected before plural synthesis always
            # wins a bounds tie.
            exact_bounds = {(span.start, span.end) for span in spans}
            for start, end, order, inflected, official, base_source in plural_hits:
                if (start, end) not in exact_bounds and (
                    _ascii_word_boundaries_ok(
                        context_text,
                        context_offset + start,
                        context_offset + end,
                        inflected,
                    )
                    and (
                        base_source in official
                        or _case_variant_boundaries_ok(
                            context_text,
                            context_offset + start,
                            context_offset + end,
                        )
                    )
                    and (
                        len(base_source) > 1
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
                        TermSpan(
                            start=start,
                            end=end,
                            source=inflected,
                            official=official,
                            order=order,
                        )
                    )

        if not spans:
            return []

        # Select global longest non-overlapping official term spans. This
        # intentionally prioritizes the longest single term, not maximum total
        # coverage. Equal-length overlaps follow dictionary iteration order,
        # then start position, then source text.
        selected: list[TermSpan] = []
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


_MATCHER_CACHE_LOCK = threading.Lock()
_MATCHER_CACHE: OrderedDict[tuple[int, frozenset[str], bool], TermSpanMatcher] = (
    OrderedDict()
)
_MATCHER_CACHE_MAXSIZE = 8


def _get_or_build_matcher(
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    *,
    allow_plural_inflection: bool,
    nfkc_spelling_sources: frozenset[str],
) -> TermSpanMatcher:
    lockable_id = id(lockable)
    exact_key = (lockable_id, nfkc_spelling_sources, allow_plural_inflection)
    with _MATCHER_CACHE_LOCK:
        cached = _MATCHER_CACHE.get(exact_key)
        if cached is not None and cached.lockable is lockable:
            _MATCHER_CACHE.move_to_end(exact_key)
            return cached
        # Reuse a superset matcher already compiled for the same lockable tuple
        # (for example, one compiled with plural support serving a non-plural call).
        for key, candidate in reversed(_MATCHER_CACHE.items()):
            if (
                key[0] == lockable_id
                and candidate.lockable is lockable
                and candidate.can_serve(
                    allow_plural_inflection=allow_plural_inflection,
                    nfkc_spelling_sources=nfkc_spelling_sources,
                )
            ):
                _MATCHER_CACHE.move_to_end(key)
                return candidate

    built = TermSpanMatcher.build(
        lockable,
        nfkc_spelling_sources=nfkc_spelling_sources,
        include_plural=allow_plural_inflection,
    )
    with _MATCHER_CACHE_LOCK:
        _MATCHER_CACHE[exact_key] = built
        _MATCHER_CACHE.move_to_end(exact_key)
        while len(_MATCHER_CACHE) > _MATCHER_CACHE_MAXSIZE:
            _MATCHER_CACHE.popitem(last=False)
    return built


def select_term_spans(
    text: str,
    lockable: tuple[tuple[str, tuple[str, str]], ...],
    context_text: str,
    context_offset: int,
    *,
    allow_plural_inflection: bool = False,
    nfkc_spelling_sources: frozenset[str] = frozenset(),
    matcher: TermSpanMatcher | None = None,
) -> list[TermSpan]:
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

    ``matcher`` optionally supplies a pre-compiled :class:`TermSpanMatcher`
    owned by the caller's immutable dictionary snapshot, avoiding repeated
    automaton lookup or construction.
    """
    if not text or not lockable:
        return []
    active = (
        matcher
        if matcher is not None
        and matcher.can_serve(
            allow_plural_inflection=allow_plural_inflection,
            nfkc_spelling_sources=nfkc_spelling_sources,
        )
        else _get_or_build_matcher(
            lockable,
            allow_plural_inflection=allow_plural_inflection,
            nfkc_spelling_sources=nfkc_spelling_sources,
        )
    )
    return active.select_spans(
        text,
        context_text,
        context_offset,
        allow_plural_inflection=allow_plural_inflection,
        nfkc_spelling_sources=nfkc_spelling_sources,
    )


def exact_lockable_source_records(
    entries: Iterable[TermEntry],
) -> dict[str, tuple[str, str, str]]:
    # Preserve an established surface's primary when derived candidates share
    # it. A derived row never displaces the row the dictionary already had;
    # the model path exposes competing pairs separately.
    sources: dict[str, tuple[str, str, str]] = {}
    for entry in sorted(entries, key=is_derived_name_entry):
        if "\n" in entry.zh or "\n" in entry.en:
            continue
        official = (entry.zh, entry.en, entry.category)
        if entry.zh:
            sources.setdefault(entry.zh, official)
        if entry.en:
            sources.setdefault(entry.en, official)
    return sources


def model_reference_candidates(
    entries: Iterable[TermEntry],
    sources: dict[str, tuple[str, str, str]],
    excluded: frozenset[str],
    pre_exclusion_sources: dict[str, tuple[str, str, str]] | None = None,
) -> ReferenceCandidates:
    """Keep competing official pairs and soft casing of derived names.

    Exact owners and both exclusion lists still win. Only derived names gain
    additional spellings here; they never enter deterministic locking.

    ``pre_exclusion_sources`` carries the pre-exclusion exact surface map so
    excluding one cased surface in a shared casefold key continues to block
    derived soft-spelling synthesis while keeping excluded surfaces out of
    lockable sources and reference candidates.

    A surface whose rows agree on ``(zh, en)`` has one meaning however many
    tables carry it: 今汐 appears in several tables and yields one reference
    line, keeping the highest ``CATEGORY_ORDER`` category. Different pairs
    stay separate candidates so the model chooses by context instead of a hard
    replacement.
    """
    ordered_entries = sorted(entries, key=is_derived_name_entry)
    folded_sources = (
        exact_lockable_source_records(ordered_entries)
        if pre_exclusion_sources is None
        else pre_exclusion_sources
    )
    # (zh, en) -> category; the pair is the meaning, the category is its rank.
    groups: dict[str, dict[tuple[str, str], str]] = {}
    folded_owners: dict[str, set[tuple[str, str]]] = {}
    for mapping in (folded_sources, sources):
        for surface, official in mapping.items():
            folded_owners.setdefault(surface.casefold(), set()).add(
                (official[0], official[1])
            )

    def add(surface: str, official: tuple[str, str, str]) -> None:
        if surface in excluded or not is_eligible_lockable(surface, official):
            return
        pair = (official[0], official[1])
        bucket = groups.setdefault(surface, {})
        current = bucket.get(pair)
        if current is None or CATEGORY_ORDER.get(
            official[2], 999
        ) < CATEGORY_ORDER.get(current, 999):
            bucket[pair] = official[2]

    for entry in ordered_entries:
        official = (entry.zh, entry.en, entry.category)
        for surface in (entry.zh, entry.en):
            if surface in sources:
                add(surface, official)
    for entry in ordered_entries:
        if not is_derived_name_entry(entry) or entry.en in excluded:
            continue
        if not _has_latin_letter(entry.en) or any(map(is_cjk_character, entry.en)):
            continue
        if len(folded_owners.get(entry.en.casefold(), ())) > 1:
            continue
        forms = [entry.en] + [
            entry.en + suffix
            for suffix in _plural_suffix_candidates(entry.en, (entry.zh, entry.en))
        ]
        for form in forms:
            for spelling in (form.lower(), form.upper()):
                if (
                    spelling == form
                    or spelling in sources
                    or spelling in folded_sources
                    or len(spelling) != len(form)
                ):
                    continue
                # Include established meanings with the same original surface
                # (Tuner the speaker and Tuner the material), not one winner.
                for (zh, en), category in groups.get(
                    entry.en, {(entry.zh, entry.en): entry.category}
                ).items():
                    add(spelling, (zh, en, category))
    return {
        surface: tuple((zh, en, category) for (zh, en), category in bucket.items())
        for surface, bucket in groups.items()
    }


def exact_lockable_sources(
    entries: Iterable[TermEntry],
) -> tuple[tuple[str, tuple[str, str]], ...]:
    """Return the eligible exact dictionary surfaces free text can lock.

    This is the runtime lockable set before case variants and free-text
    exclusions, in runtime order, for the builder's corpus measurement.
    """
    return tuple(
        (source, (official[0], official[1]))
        for source, official in exact_lockable_source_records(entries).items()
        if is_eligible_lockable(source, official)
    )
