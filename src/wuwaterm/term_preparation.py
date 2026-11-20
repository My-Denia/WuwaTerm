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
from .term_matching import _is_cjk_character, _TermSpan, select_term_spans


# Names can be ordinary words in English; the model needs context to choose.
# In the other direction every term is a reference so English can inflect it.
CONTEXTUAL_REFERENCE_CATEGORIES = frozenset({"resonator", "speaker"})

Lockable = tuple[tuple[str, tuple[str, str]], ...]
Reference = tuple[str, str, str, str]
Lock = tuple[str, str, str]


@dataclass(frozen=True)
class PreparedTerms:
    locked_text: str
    locks: tuple[Lock, ...]
    references: tuple[Reference, ...]


def _new_placeholder_prefix(source_text: str) -> str:
    while True:
        prefix = f"__WUWA_TERM_{secrets.token_hex(8)}_"
        if prefix not in source_text:
            return prefix


def _is_contextual_reference(category: str, *, to_chinese: bool) -> bool:
    """Return whether a category reaches the model as a reference.

    English needs number and articles, so every category is a reference in
    that direction. In Chinese, names remain references because the same
    English surface can be an ordinary word; other categories are locked.
    """
    if not to_chinese:
        return True
    return category in CONTEXTUAL_REFERENCE_CATEGORIES


def _span_decision(
    span: _TermSpan,
    categories: dict[str, str],
    reference_only: frozenset[str],
    to_chinese: bool | None,
) -> tuple[Literal["lock", "reference", "keep"], str]:
    zh, en = span.official
    # An inflected span is not a dictionary key; its official surface is.
    category = categories.get(span.source) or categories.get(en) or categories.get(zh) or ""
    if span.source in reference_only:
        return ("keep" if to_chinese is None else "reference"), category
    if to_chinese is not None and category and _is_contextual_reference(
        category, to_chinese=to_chinese
    ):
        return "reference", category
    return "lock", category


def _additional_name_references(
    text: str,
    context_text: str,
    context_offset: int,
    lockable: Lockable,
    categories: dict[str, str],
    selected: list[_TermSpan],
    seen: set[Reference],
) -> list[Reference]:
    """Find single-character name mentions too loosely situated to lock.

    For example, 椿 can reach the model as a name in 欢迎椿, but not when it is
    covered by an ordinary word such as 香椿. A mention is guidance, not a
    forced substitution, and the selected spans keep their precedence.
    """
    references: list[Reference] = []
    for source, official in lockable:
        if len(source) != 1 or not _is_cjk_character(source):
            continue
        category = categories.get(source, "")
        if category not in CONTEXTUAL_REFERENCE_CATEGORIES:
            continue
        reference = (source, official[0], official[1], category)
        if reference in seen:
            continue
        start = text.find(source)
        while start != -1:
            end = start + 1
            if not any(span.start <= start < span.end for span in selected) and (
                single_character_name_mention_at(
                    context_text, context_offset + start, context_offset + end
                )
            ):
                seen.add(reference)
                references.append(reference)
                break
            start = text.find(source, start + 1)
    return references


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
    references: list[Reference] = []
    seen: set[Reference] = set()
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
            reference = (span.source, zh, en, category)
            if decision == "reference" and reference not in seen:
                seen.add(reference)
                references.append(reference)
        index = span.end
    if to_chinese is not None:
        references.extend(_additional_name_references(
            text, context_text, context_offset, lockable, categories, selected, seen
        ))
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
    references: list[Reference] = []
    seen: set[Reference] = set()
    for segment, offset in zip(segments, offsets, strict=True):
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
        )
        locked_segments.append(prepared.locked_text)
        locks.extend(prepared.locks)
        for reference in prepared.references:
            if reference not in seen:
                seen.add(reference)
                references.append(reference)
    return PreparedTerms(
        protected.for_model(tuple(locked_segments)), tuple(locks), tuple(references)
    )
