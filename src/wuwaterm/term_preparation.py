"""Prepare selected terms for translation without owning the model or database.

Plain and protected text share span decisions and placeholder numbering. The
caller supplies a dictionary snapshot and controls whether prepared plain text
may use its admissible NFKC spellings.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from .cjk_span import single_character_name_mention_at
from .protected_text import ProtectedText
from .term_matching import TermSpan, is_cjk_character, select_term_spans


# Names can be ordinary words in English; the model needs context to choose.
# In the other direction every term is a reference so English can inflect it.
CONTEXTUAL_REFERENCE_CATEGORIES = frozenset({"resonator", "speaker"})

Lockable = tuple[tuple[str, tuple[str, str]], ...]
Lock = tuple[str, str, str]


@dataclass(frozen=True)
class TermOccurrence:
    """One matched occurrence of a dictionary term inside a visible segment."""

    start: int
    end: int
    matched_text: str
    segment_index: int = 0


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


def _span_decision(
    span: TermSpan,
    categories: dict[str, str],
    reference_only: frozenset[str],
    to_chinese: bool | None,
) -> tuple[Literal["lock", "reference", "keep"], str]:
    zh, en = span.official
    # An inflected span is not a dictionary key; its official surface is.
    category = categories.get(span.source) or categories.get(en) or categories.get(zh) or ""
    if span.source in reference_only:
        return ("keep" if to_chinese is None else "reference"), category
    if to_chinese is not None and category and is_contextual_reference(
        category, to_chinese=to_chinese
    ):
        return "reference", category
    return "lock", category


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
        occurrences=existing.occurrences + (occurrence,),
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
    references: list[TermReference],
    index_by_key: dict[tuple[str, str, str, str], int],
    *,
    segment_index: int = 0,
) -> None:
    """Find single-character name mentions too loosely situated to lock.

    For example, 椿 can reach the model as a name in 欢迎椿, but not when it is
    covered by an ordinary word such as 香椿. A mention is guidance, not a
    forced substitution, and the selected spans keep their precedence.
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
                    ),
                    origin="dictionary",
                    reason="single_char_name_mention",
                )
            start = text.find(source, start + 1)


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
) -> PreparedTerms:
    """Prepare one visible segment; offsets refer to its full visible context."""
    selected = select_term_spans(
        text,
        lockable,
        context_text,
        context_offset,
        allow_plural_inflection=allow_plural_inflection,
        nfkc_spelling_sources=nfkc_spelling_sources,
    )
    parts: list[str] = []
    locks: list[Lock] = []
    references: list[TermReference] = []
    index_by_key: dict[tuple[str, str, str, str], int] = {}
    index = 0
    for span in selected:
        parts.append(text[index : span.start])
        zh, en = span.official
        decision, category = _span_decision(span, categories, reference_only, to_chinese)
        if decision == "lock":
            placeholder = f"{placeholder_prefix}{placeholder_offset + len(locks):04d}__"
            parts.append(placeholder)
            locks.append((placeholder, zh, en))
        else:
            parts.append(text[span.start : span.end])
            if decision == "reference":
                reason = "reference_only" if span.source in reference_only else "contextual"
                _append_reference_occurrence(
                    references,
                    index_by_key,
                    source=span.source,
                    zh=zh,
                    en=en,
                    category=category,
                    occurrence=TermOccurrence(
                        start=span.start,
                        end=span.end,
                        matched_text=text[span.start : span.end],
                        segment_index=segment_index,
                    ),
                    origin="dictionary",
                    reason=reason,
                )
        index = span.end
    if to_chinese is not None:
        _collect_additional_name_references(
            text,
            context_text,
            context_offset,
            lockable,
            categories,
            selected,
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
    locked_segments: list[str] = []
    locks: list[Lock] = []
    references: list[TermReference] = []
    index_by_key: dict[tuple[str, str, str, str], int] = {}
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
        )
        locked_segments.append(prepared.locked_text)
        locks.extend(prepared.locks)
        for reference in prepared.references:
            key = reference.dedupe_key()
            existing_index = index_by_key.get(key)
            if existing_index is None:
                index_by_key[key] = len(references)
                references.append(reference)
            else:
                existing = references[existing_index]
                references[existing_index] = TermReference(
                    source=existing.source,
                    zh=existing.zh,
                    en=existing.en,
                    category=existing.category,
                    occurrences=existing.occurrences + reference.occurrences,
                    origin=existing.origin,
                    reason=existing.reason,
                )
    return PreparedTerms(
        protected.for_model(tuple(locked_segments)), tuple(locks), tuple(references)
    )
