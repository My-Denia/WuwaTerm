"""Prepare selected terms for translation without owning the model or database.

Plain and protected text share span decisions and placeholder numbering. The
caller supplies a dictionary snapshot and controls whether prepared plain text
may use its admissible NFKC spellings.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .cjk_span import single_character_name_mention_at
from .protected_text import ProtectedText
from .skill_references import SkillReference, skill_occurrences
from .term_matching import (
    ReferenceCandidates,
    TermSpan,
    is_cjk_character,
    reference_span_admitted,
    select_term_spans,
)


# Names can be ordinary words in English; the model needs context to choose.
# In the other direction every term is a reference so English can inflect it.
CONTEXTUAL_REFERENCE_CATEGORIES = frozenset({"resonator", "speaker"})

Lockable = tuple[tuple[str, tuple[str, str]], ...]
Lock = tuple[str, str, str]

_SCOPED_ORIGINS = frozenset({"scoped_skill_stem", "derived_soft_casing"})
_SCOPED_REASONS = frozenset(
    {
        "single_char_name_mention",
        "ambiguous_alternative",
        "soft_casing",
        "local_skill_action",
    }
)


@dataclass(frozen=True)
class TermOccurrence:
    """One matched occurrence of a dictionary term inside a visible segment.

    ``start`` and ``end`` index the visible segment at ``segment_index``.
    ``model_start`` and ``model_end`` index the final ``locked_text`` handed to
    the model (after term and structural placeholder substitution).
    """

    start: int
    end: int
    matched_text: str
    segment_index: int = 0
    model_start: int | None = None
    model_end: int | None = None

    def shifted_model(self, delta: int) -> TermOccurrence:
        if self.model_start is None or self.model_end is None:
            return self
        return TermOccurrence(
            start=self.start,
            end=self.end,
            matched_text=self.matched_text,
            segment_index=self.segment_index,
            model_start=self.model_start + delta,
            model_end=self.model_end + delta,
        )


@dataclass(frozen=True)
class TermReference:
    """Structured dictionary reference handed to the LLM as contextual guidance."""

    source: str
    zh: str
    en: str
    category: str
    occurrences: tuple[TermOccurrence, ...] = ()
    origin: str = "dictionary"
    reason: str = ""

    def dedupe_key(self) -> tuple[str, str, str, str]:
        return (self.source, self.zh, self.en, self.category)

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.source, self.zh, self.en, self.category)


Reference = TermReference


@dataclass(frozen=True)
class PreparedTerms:
    locked_text: str
    locks: tuple[Lock, ...]
    references: tuple[TermReference, ...]


def new_placeholder_prefix(source_text: str) -> str:
    while True:
        prefix = f"__WUWA_TERM_{secrets.token_hex(8)}_"
        if prefix not in source_text:
            return prefix


def is_contextual_reference(category: str, *, to_chinese: bool) -> bool:
    """Return whether a category reaches the model as a reference.

    English needs number and articles, so every category is a reference in
    that direction. In Chinese, names remain references because the same
    English surface can be an ordinary word; other categories are locked.
    """
    if not to_chinese:
        return True
    return category in CONTEXTUAL_REFERENCE_CATEGORIES


def references_need_scope_instruction(references: Sequence[TermReference]) -> bool:
    """Return whether any reference needs the scoped-reference explanation.

    Plain single-meaning multi-character references keep the pre-admission
    prompt bytes unchanged. Three mechanisms opt into the scope explanation:
    an admitted one-character name mention, a surface with competing official
    pairs, a soft-cased derived name, or a bounded local skill stem.
    """
    seen_pairs_by_source: dict[str, set[tuple[str, str]]] = {}
    for ref in references:
        if ref.origin in _SCOPED_ORIGINS or ref.reason in _SCOPED_REASONS:
            return True
        if len(ref.source) == 1 and is_cjk_character(ref.source) and ref.occurrences:
            return True
        pairs = seen_pairs_by_source.setdefault(ref.source, set())
        pairs.add((ref.zh, ref.en))
        if len(pairs) > 1:
            return True
    return False


def validated_reference_ranges(
    text: str,
    references: Sequence[TermReference],
    *,
    to_chinese: bool,
) -> list[dict[str, object]]:
    """Build and validate JSON occurrence ranges against the exact model input.

    Only occurrence-scoped references (admitted single-character CJK names and
    ``origin == "scoped_skill_stem"``) emit ranges. Every emitted coordinate is
    checked against ``text`` (the actual user message after placeholders) before
    any request leaves the process.
    """
    raw_spans: list[tuple[int, int, str, str | None]] = []
    for ref in references:
        is_skill_stem = ref.origin == "scoped_skill_stem"
        is_single_char = len(ref.source) == 1 and is_cjk_character(ref.source)
        if not is_skill_stem and not (is_single_char and ref.occurrences):
            continue
        if not isinstance(ref.source, str) or not ref.source:
            raise ValueError("reference range does not match the model input")
        target: str | None = None
        if is_skill_stem:
            target = ref.zh if to_chinese else ref.en
            if not isinstance(target, str) or not target or not ref.occurrences:
                raise ValueError("reference range does not match the model input")
        for occ in ref.occurrences:
            start = occ.model_start
            end = occ.model_end
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(text)
                or text[start:end] != ref.source
            ):
                raise ValueError("reference range does not match the model input")
            raw_spans.append((start, end, ref.source, target))

    if not raw_spans:
        return []

    ordered = sorted(
        set(raw_spans),
        key=lambda item: (item[0], item[1], item[2], item[3] or ""),
    )
    grouped: dict[tuple[str, str | None], list[list[int]]] = {}
    for start, end, source, target in ordered:
        grouped.setdefault((source, target), []).append([start, end])

    payload: list[dict[str, object]] = []
    for (source, target), ranges in grouped.items():
        entry: dict[str, object] = {"source": source, "ranges": ranges}
        if target is not None:
            entry["target"] = target
        payload.append(entry)
    return payload


def _span_decision(
    span: TermSpan,
    categories: dict[str, str],
    reference_only: frozenset[str],
    to_chinese: bool | None,
    *,
    choices: tuple[tuple[str, str, str], ...] = (),
    soft_sources: frozenset[str] = frozenset(),
) -> tuple[Literal["lock", "reference", "keep"], str, bool, bool]:
    zh, en = span.official
    # An inflected span is not a dictionary key; its official surface is.
    category = (
        categories.get(span.source)
        or categories.get(en)
        or categories.get(zh)
        or (choices[0][2] if choices else "")
        or ""
    )
    ambiguous = len({(pair_zh, pair_en) for pair_zh, pair_en, _c in choices}) > 1
    is_soft = span.source in soft_sources
    if span.source in reference_only or is_soft or ambiguous:
        return ("keep" if to_chinese is None else "reference"), category, ambiguous, is_soft
    if to_chinese is not None and category and is_contextual_reference(
        category, to_chinese=to_chinese
    ):
        return "reference", category, ambiguous, is_soft
    return "lock", category, ambiguous, is_soft


def _merge_occurrences(
    existing: tuple[TermOccurrence, ...],
    added: tuple[TermOccurrence, ...],
) -> tuple[TermOccurrence, ...]:
    merged: list[TermOccurrence] = list(existing)
    seen = set(existing)
    for occ in added:
        if occ not in seen:
            seen.add(occ)
            merged.append(occ)
    merged.sort(key=lambda item: (item.segment_index, item.start, item.end))
    return tuple(merged)


def _append_reference_occurrence(
    references: list[TermReference],
    index_by_key: dict[tuple[str, str, str, str], int],
    *,
    source: str,
    zh: str,
    en: str,
    category: str,
    occurrence: TermOccurrence,
    origin: str = "dictionary",
    reason: str = "",
) -> None:
    key = (source, zh, en, category)
    existing_index = index_by_key.get(key)
    if existing_index is None:
        index_by_key[key] = len(references)
        references.append(
            TermReference(
                source=source,
                zh=zh,
                en=en,
                category=category,
                occurrences=(occurrence,),
                origin=origin,
                reason=reason,
            )
        )
        return
    existing = references[existing_index]
    references[existing_index] = TermReference(
        source=existing.source,
        zh=existing.zh,
        en=existing.en,
        category=existing.category,
        occurrences=_merge_occurrences(existing.occurrences, (occurrence,)),
        origin=existing.origin,
        reason=existing.reason,
    )


def _collect_additional_name_references(
    text: str,
    context_text: str,
    context_offset: int,
    lockable: Lockable,
    categories: dict[str, str],
    selected: list[TermSpan],
    replacements: list[tuple[int, int]],
    references: list[TermReference],
    index_by_key: dict[tuple[str, str, str, str], int],
    *,
    segment_index: int = 0,
) -> None:
    """Find single-character name mentions too loosely situated to lock.

    For example, 椿 can reach the model as a name in 欢迎椿, but not when it is
    covered by an ordinary word such as 香椿 or an unrecognized compound such
    as 红椿蕊 or 红椿·蕾. A mention is guidance, not a forced substitution, and
    the selected spans keep their precedence.
    """
    for source, official in lockable:
        if len(source) != 1 or not is_cjk_character(source):
            continue
        category = categories.get(source, "")
        if category not in CONTEXTUAL_REFERENCE_CATEGORIES:
            continue
        start = text.find(source)
        while start != -1:
            end = start + 1
            if not any(span.start <= start < span.end for span in selected) and (
                single_character_name_mention_at(
                    context_text, context_offset + start, context_offset + end
                )
            ):
                model_pos = start + sum(
                    delta for edge, delta in replacements if edge <= start
                )
                _append_reference_occurrence(
                    references,
                    index_by_key,
                    source=source,
                    zh=official[0],
                    en=official[1],
                    category=category,
                    occurrence=TermOccurrence(
                        start=start,
                        end=end,
                        matched_text=text[start:end],
                        segment_index=segment_index,
                        model_start=model_pos,
                        model_end=model_pos + 1,
                    ),
                    origin="dictionary",
                    reason="single_char_name_mention",
                )
            start = text.find(source, start + 1)


def _collect_scoped_skill_references(
    text: str,
    context_text: str,
    context_offset: int,
    selected: list[TermSpan],
    categories: dict[str, str],
    replacements: list[tuple[int, int]],
    skill_refs: tuple[SkillReference, ...],
    references: list[TermReference],
    index_by_key: dict[tuple[str, str, str, str], int],
    *,
    segment_index: int = 0,
) -> None:
    """Attach bounded closed-class 2-char skill-stem references on zh->en."""
    if not skill_refs:
        return
    game_actions = tuple(
        (context_offset + span.start - 1, context_offset + span.end)
        for span in selected
        if span.start > 0
        and text[span.start - 1] == "切"
        and categories.get(span.source) in CONTEXTUAL_REFERENCE_CATEGORIES
    )
    for occurrence in skill_occurrences(
        text, context_text, context_offset, skill_refs, game_actions
    ):
        if any(
            span.start < occurrence.end and occurrence.start < span.end
            for span in selected
        ):
            continue
        candidate = occurrence.reference
        model_pos = occurrence.start + sum(
            delta for edge, delta in replacements if edge <= occurrence.start
        )
        _append_reference_occurrence(
            references,
            index_by_key,
            source=candidate.surface,
            zh=candidate.zh,
            en=candidate.en,
            category=candidate.category,
            occurrence=TermOccurrence(
                start=occurrence.start,
                end=occurrence.end,
                matched_text=text[occurrence.start : occurrence.end],
                segment_index=segment_index,
                model_start=model_pos,
                model_end=model_pos + len(candidate.surface),
            ),
            origin="scoped_skill_stem",
            reason="local_skill_action",
        )


def prepare_term_segment(
    text: str,
    *,
    lockable: Lockable,
    context_text: str,
    context_offset: int,
    categories: dict[str, str],
    reference_only: frozenset[str],
    to_chinese: bool | None,
    allow_plural_inflection: bool,
    nfkc_spelling_sources: frozenset[str],
    placeholder_prefix: str,
    placeholder_offset: int = 0,
    segment_index: int = 0,
    reference_candidates: ReferenceCandidates | None = None,
    soft_sources: frozenset[str] | None = None,
    context_has_terms: bool | None = None,
    skill_refs: tuple[SkillReference, ...] = (),
) -> PreparedTerms:
    """Prepare one visible segment; offsets refer to its full visible context."""
    candidates = reference_candidates if (to_chinese is not None and reference_candidates) else {}
    if soft_sources is None:
        if to_chinese is not None and candidates:
            soft_sources = frozenset(
                candidates.keys() - {source for source, _official in lockable}
            )
        else:
            soft_sources = frozenset()

    selected = select_term_spans(
        text,
        lockable,
        context_text,
        context_offset,
        allow_plural_inflection=allow_plural_inflection,
        nfkc_spelling_sources=nfkc_spelling_sources,
    )
    if to_chinese is not None:
        selected = [
            span
            for span in selected
            if reference_span_admitted(span, context_text, context_offset)
        ]

    # A derived soft-casing hint needs actual dictionary term context in the
    # submitted segment. Isolated ordinary prose such as "the energy core of a
    # star" must not gain game hints merely because a derived base shares that
    # spelling.
    has_term_context = bool(selected)
    if soft_sources and not has_term_context and context_text != text:
        if context_has_terms is not None:
            has_term_context = context_has_terms
        else:
            has_term_context = any(
                reference_span_admitted(span, context_text, 0)
                for span in select_term_spans(
                    context_text,
                    lockable,
                    context_text,
                    0,
                    allow_plural_inflection=allow_plural_inflection,
                )
            )
    if (
        to_chinese is not None
        and soft_sources
        and has_term_context
        and any(source in text for source in soft_sources)
    ):
        extended_lockable = lockable + tuple(
            (source, (choices[0][0], choices[0][1]))
            for source, choices in candidates.items()
            if source in soft_sources and choices
        )
        selected = [
            span
            for span in select_term_spans(
                text,
                extended_lockable,
                context_text,
                context_offset,
                allow_plural_inflection=allow_plural_inflection,
                nfkc_spelling_sources=nfkc_spelling_sources,
            )
            if reference_span_admitted(span, context_text, context_offset)
        ]

    parts: list[str] = []
    locks: list[Lock] = []
    references: list[TermReference] = []
    index_by_key: dict[tuple[str, str, str, str], int] = {}
    # Source end offsets and length deltas of spans turned into placeholders so
    # occurrences added later carry exact model-text coordinates.
    replacements: list[tuple[int, int]] = []
    output_length = 0
    index = 0
    for span in selected:
        parts.append(text[index : span.start])
        output_length += span.start - index
        zh, en = span.official
        choices = candidates.get(span.source) or candidates.get(en) or ()
        decision, category, ambiguous, is_soft = _span_decision(
            span,
            categories,
            reference_only,
            to_chinese,
            choices=choices,
            soft_sources=soft_sources,
        )
        if decision == "lock":
            placeholder = f"{placeholder_prefix}{placeholder_offset + len(locks):04d}__"
            parts.append(placeholder)
            locks.append((placeholder, zh, en))
            replacements.append(
                (span.end, len(placeholder) - (span.end - span.start))
            )
            output_length += len(placeholder)
        else:
            parts.append(text[span.start : span.end])
            if decision == "reference":
                is_single_char_name = len(span.source) == 1 and is_cjk_character(
                    span.source
                )
                origin = "derived_soft_casing" if is_soft else "dictionary"
                if is_single_char_name:
                    reason = "single_char_name_mention"
                elif is_soft:
                    reason = "soft_casing"
                elif ambiguous:
                    reason = "ambiguous_alternative"
                elif span.source in reference_only:
                    reason = "reference_only"
                else:
                    reason = "contextual"
                occ = TermOccurrence(
                    start=span.start,
                    end=span.end,
                    matched_text=text[span.start : span.end],
                    segment_index=segment_index,
                    model_start=output_length,
                    model_end=output_length + (span.end - span.start),
                )
                for choice_zh, choice_en, choice_category in choices or (
                    (zh, en, category),
                ):
                    _append_reference_occurrence(
                        references,
                        index_by_key,
                        source=span.source,
                        zh=choice_zh,
                        en=choice_en,
                        category=choice_category,
                        occurrence=occ,
                        origin=origin,
                        reason=reason,
                    )
            output_length += span.end - span.start
        index = span.end

    if to_chinese is not None:
        _collect_additional_name_references(
            text,
            context_text,
            context_offset,
            lockable,
            categories,
            selected,
            replacements,
            references,
            index_by_key,
            segment_index=segment_index,
        )
    if to_chinese is False and skill_refs:
        _collect_scoped_skill_references(
            text,
            context_text,
            context_offset,
            selected,
            categories,
            replacements,
            skill_refs,
            references,
            index_by_key,
            segment_index=segment_index,
        )
    parts.append(text[index:])
    return PreparedTerms("".join(parts), tuple(locks), tuple(references))


def prepare_protected_terms(
    protected: ProtectedText,
    *,
    lockable: Lockable,
    categories: dict[str, str],
    reference_only: frozenset[str],
    to_chinese: bool | None,
    allow_plural_inflection: bool,
    placeholder_prefix: str,
    reference_candidates: ReferenceCandidates | None = None,
    skill_refs: tuple[SkillReference, ...] = (),
) -> PreparedTerms:
    """Merge segment results in source order with one placeholder namespace."""
    segments = protected.visible_segments
    context_parts: list[str] = []
    offsets: list[int] = []
    offset = 0
    for index, segment in enumerate(segments):
        offsets.append(offset)
        context_parts.append(segment)
        offset += len(segment)
        if index < len(protected.structures):
            context = protected.structures[index].visible_context
            context_parts.append(context)
            offset += len(context)
    context_text = "".join(context_parts)

    candidates = reference_candidates if (to_chinese is not None and reference_candidates) else {}
    soft_sources: frozenset[str] = frozenset()
    context_has_terms: bool | None = None
    if to_chinese is not None and candidates:
        primary_sources = {source for source, _official in lockable}
        soft_sources = frozenset(candidates.keys() - primary_sources)
        if soft_sources and len(segments) > 1:
            context_has_terms = any(
                reference_span_admitted(span, context_text, 0)
                for span in select_term_spans(
                    context_text,
                    lockable,
                    context_text,
                    0,
                    allow_plural_inflection=allow_plural_inflection,
                )
            )

    locked_segments: list[str] = []
    locks: list[Lock] = []
    references: list[TermReference] = []
    index_by_key: dict[tuple[str, str, str, str], int] = {}
    model_offset = 0
    placeholder_lengths = protected.placeholder_lengths
    for segment_index, (segment, offset) in enumerate(zip(segments, offsets, strict=True)):
        # Protected segments are not normalized like prepared plain text.
        prepared = prepare_term_segment(
            segment,
            lockable=lockable,
            context_text=context_text,
            context_offset=offset,
            categories=categories,
            reference_only=reference_only,
            to_chinese=to_chinese,
            allow_plural_inflection=allow_plural_inflection,
            nfkc_spelling_sources=frozenset(),
            placeholder_prefix=placeholder_prefix,
            placeholder_offset=len(locks),
            segment_index=segment_index,
            reference_candidates=candidates,
            soft_sources=soft_sources,
            context_has_terms=context_has_terms,
            skill_refs=skill_refs,
        )
        locked_segments.append(prepared.locked_text)
        locks.extend(prepared.locks)
        for reference in prepared.references:
            shifted = tuple(occ.shifted_model(model_offset) for occ in reference.occurrences)
            key = reference.dedupe_key()
            existing_index = index_by_key.get(key)
            if existing_index is None:
                index_by_key[key] = len(references)
                references.append(
                    TermReference(
                        source=reference.source,
                        zh=reference.zh,
                        en=reference.en,
                        category=reference.category,
                        occurrences=shifted,
                        origin=reference.origin,
                        reason=reference.reason,
                    )
                )
            else:
                existing = references[existing_index]
                references[existing_index] = TermReference(
                    source=existing.source,
                    zh=existing.zh,
                    en=existing.en,
                    category=existing.category,
                    occurrences=_merge_occurrences(existing.occurrences, shifted),
                    origin=existing.origin,
                    reason=existing.reason,
                )
        model_offset += len(prepared.locked_text)
        if segment_index < len(protected.structures):
            model_offset += placeholder_lengths[segment_index]

    return PreparedTerms(
        protected.for_model(tuple(locked_segments)), tuple(locks), tuple(references)
    )
