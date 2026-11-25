"""Prepare selected terms for translation without owning the model or database.

Plain and protected text share span decisions and placeholder numbering. The
caller supplies a dictionary snapshot and controls whether prepared plain text
may use its admissible NFKC spellings.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .cjk_span import SINGLE_CHAR_NAME_MENTION, classify_single_character_mention
from .protected_text import ProtectedText
from .skill_references import SkillReference, skill_occurrences
from .term_matching import (
    ReferenceCandidates,
    TermSpan,
    has_latin_word_boundaries,
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
_PARAGRAPH_SPLIT_RE = re.compile(r"\n[^\S\n]*\n")
_OPAQUE_PLACEHOLDER_RE = re.compile(r"__WUWA_(?:TERM|HTML)_[0-9a-f]{16}_[0-9]{4,}__")
SCOPED_SNIPPET_RADIUS = 6
SCOPED_ALL_OCCURRENCES_THRESHOLD = 4


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


def is_occurrence_scoped_reference(ref: TermReference) -> bool:
    """Return whether ``ref`` applies only to specific admitted occurrences.

    Two mechanisms are unconditionally occurrence-scoped: single-character CJK
    resonator/speaker mentions and bounded closed-class 2-character skill stems.
    Legacy tuple references without occurrences remain global references.
    """
    return (
        ref.origin == "scoped_skill_stem"
        or ref.reason == "single_char_name_mention"
        or (len(ref.source) == 1 and is_cjk_character(ref.source) and bool(ref.occurrences))
    )


def _is_paragraph_gated_derived_reference(ref: TermReference) -> bool:
    """Return whether ``ref`` came from paragraph-gated derived admission."""
    return ref.origin == "derived_soft_casing" or ref.reason in {
        "soft_casing",
        "reference_only",
    }


def _has_valid_model_occurrences(ref: TermReference) -> bool:
    return bool(ref.occurrences) and all(
        type(occ.model_start) is int and type(occ.model_end) is int
        for occ in ref.occurrences
    )


def _raw_surface_spans(text: str, surface: str) -> list[tuple[int, int]]:
    """Find every raw occurrence of ``surface`` in ``text``.

    Latin-only surfaces require Latin word boundaries so ``tuner`` does not
    collide with ``retuner`` or ``tuners``; CJK surfaces match substrings.
    """
    if not surface:
        return []
    is_latin_only = not any(is_cjk_character(ch) for ch in surface) and any(
        ch.isalpha() for ch in surface
    )
    spans: list[tuple[int, int]] = []
    pos = text.find(surface)
    while pos != -1:
        end = pos + len(surface)
        if not is_latin_only or has_latin_word_boundaries(text, pos, end):
            spans.append((pos, end))
        pos = text.find(surface, pos + 1)
    return spans


def _is_strictly_enclosed(
    span: tuple[int, int], global_occ_spans: Sequence[tuple[int, int]]
) -> bool:
    start, end = span
    return any(
        g_start <= start and end <= g_end and (g_start < start or end < g_end)
        for g_start, g_end in global_occ_spans
    )


def partition_references_for_prompt(
    locked_text: str,
    references: Sequence[TermReference],
) -> tuple[tuple[TermReference, ...], tuple[TermReference, ...]]:
    """Split references into global vs. occurrence-scoped prompt blocks.

    Unconditionally occurrence-scoped references (single-char CJK names and
    ``scoped_skill_stem``) always enter the scoped block. Paragraph-gated
    derived references (``derived_soft_casing`` and ``reference_only``) are
    promoted to the scoped block whenever ``locked_text`` contains at least
    one unadmitted occurrence not strictly enclosed by a global reference;
    when all occurrences in ``locked_text`` are admitted they remain global.
    """
    scoped_indices: set[int] = {
        idx
        for idx, ref in enumerate(references)
        if is_occurrence_scoped_reference(ref)
    }

    changed = True
    while changed:
        changed = False
        global_occ_spans: list[tuple[int, int]] = []
        for idx, ref in enumerate(references):
            if idx in scoped_indices:
                continue
            for occ in ref.occurrences:
                if type(occ.model_start) is int and type(occ.model_end) is int:
                    global_occ_spans.append((occ.model_start, occ.model_end))

        for idx, ref in enumerate(references):
            if idx in scoped_indices:
                continue
            if not (
                _is_paragraph_gated_derived_reference(ref)
                and _has_valid_model_occurrences(ref)
            ):
                continue
            admitted_set = {
                (occ.model_start, occ.model_end)
                for occ in ref.occurrences
                if type(occ.model_start) is int and type(occ.model_end) is int
            }
            has_unadmitted = any(
                span not in admitted_set
                and not _is_strictly_enclosed(span, global_occ_spans)
                for span in _raw_surface_spans(locked_text, ref.source)
            )
            if has_unadmitted:
                scoped_indices.add(idx)
                changed = True

    global_refs = tuple(
        ref for idx, ref in enumerate(references) if idx not in scoped_indices
    )
    scoped_refs = tuple(
        ref for idx, ref in enumerate(references) if idx in scoped_indices
    )
    return global_refs, scoped_refs


def references_need_scope_instruction(references: Sequence[TermReference]) -> bool:
    """Return whether any reference needs the scoped-reference explanation.

    Plain single-meaning multi-character references keep the pre-admission
    prompt bytes unchanged. Scoped mechanisms opt into the explanation:
    an admitted one-character name mention, a surface with competing official
    pairs, a soft-cased derived name, or a bounded local skill stem.
    """
    seen_pairs_by_source: dict[str, set[tuple[str, str]]] = {}
    for ref in references:
        if ref.origin in _SCOPED_ORIGINS or ref.reason in _SCOPED_REASONS:
            return True
        if is_occurrence_scoped_reference(ref):
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
    extra_scoped: Sequence[TermReference] = (),
) -> list[dict[str, object]]:
    """Build and validate occurrence ranges against the exact model input.

    Validates every occurrence-scoped reference (admitted single-character CJK
    names, ``origin == "scoped_skill_stem"``, and any dynamically promoted
    scoped references in ``extra_scoped``). Every emitted coordinate is checked
    against ``text`` (the actual user message after placeholders) before any
    request leaves the process.
    """
    extra_ids = {id(ref) for ref in extra_scoped}
    raw_spans: list[tuple[int, int, str, str | None]] = []
    for ref in references:
        if not is_occurrence_scoped_reference(ref) and id(ref) not in extra_ids:
            continue
        is_skill_stem = ref.origin == "scoped_skill_stem"
        if not isinstance(ref.source, str) or not ref.source or not ref.occurrences:
            raise ValueError("reference range does not match the model input")
        target: str | None = None
        if is_skill_stem:
            target = ref.zh if to_chinese else ref.en
            if not isinstance(target, str) or not target:
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


def _local_context_snippet(
    text: str,
    start: int,
    end: int,
    *,
    radius: int | None = SCOPED_SNIPPET_RADIUS,
    window_bounds: tuple[int, int] | None = None,
) -> str:
    """Extract a bounded single-line local context snippet marking ``text[start:end]``.

    ``radius=None`` uses the full line (or ``window_bounds`` when given for a
    whole-paragraph escalation). Any internal newlines are collapsed to spaces
    so every scoped reference remains one line.
    """
    if window_bounds is None:
        line_start = text.rfind("\n", 0, start) + 1
        nl_after = text.find("\n", end)
        line_end = len(text) if nl_after == -1 else nl_after
    else:
        line_start, line_end = window_bounds

    left_bound = line_start
    right_bound = line_end
    for match in _OPAQUE_PLACEHOLDER_RE.finditer(text, line_start, line_end):
        if match.end() <= start and match.end() > left_bound:
            left_bound = match.end()
        elif match.start() >= end and match.start() < right_bound:
            right_bound = match.start()
            break

    lo = left_bound if radius is None else max(left_bound, start - radius)
    hi = right_bound if radius is None else min(right_bound, end + radius)
    left = text[lo:start].replace("\n", " ")
    right = text[end:hi].replace("\n", " ")
    return f"{left}「{text[start:end]}」{right}".strip()


def _disambiguate_scoped_snippets(
    locked_text: str,
    admitted_spans: Sequence[tuple[int, int]],
    not_at_spans: Sequence[tuple[int, int]],
    *,
    radius: int = SCOPED_SNIPPET_RADIUS,
) -> tuple[list[str], list[str]]:
    """Build ``ONLY at:`` and ``NOT at:`` snippets guaranteed never to collide.

    Starts at ``radius`` on the same line. If any snippet appears in both sets,
    escalates only colliding spans through:
    1. wider same-line windows (12, 24, 48, 96, full line);
    2. full natural paragraph;
    3. 1-based paragraph prefix ``[P{n}]`` when colliding spans cross paragraphs;
    4. exact scalar range ``[start,end)`` when identical text recurs in one
       paragraph.
    """
    only_map = {
        span: _local_context_snippet(locked_text, *span, radius=radius)
        for span in admitted_spans
    }
    not_map = {
        span: _local_context_snippet(locked_text, *span, radius=radius)
        for span in not_at_spans
    }

    collisions = set(only_map.values()) & set(not_map.values())
    if collisions:
        for step_radius in (12, 24, 48, 96, None):
            if step_radius is not None and step_radius <= radius:
                continue
            for mapping in (only_map, not_map):
                for span, text_val in list(mapping.items()):
                    if text_val in collisions:
                        mapping[span] = _local_context_snippet(
                            locked_text, *span, radius=step_radius
                        )
            collisions = set(only_map.values()) & set(not_map.values())
            if not collisions:
                break

    if collisions:
        para_spans = _paragraph_spans(locked_text)
        for mapping in (only_map, not_map):
            for span, text_val in list(mapping.items()):
                if text_val in collisions:
                    p_idx = _paragraph_index(para_spans, span[0])
                    mapping[span] = _local_context_snippet(
                        locked_text,
                        *span,
                        radius=None,
                        window_bounds=para_spans[p_idx],
                    )
        collisions = set(only_map.values()) & set(not_map.values())

    if collisions:
        para_spans = _paragraph_spans(locked_text)
        for text_val in list(collisions):
            only_matching = [s for s, v in only_map.items() if v == text_val]
            not_matching = [s for s, v in not_map.items() if v == text_val]
            p_nums = {
                _paragraph_index(para_spans, s[0]) + 1
                for s in (*only_matching, *not_matching)
            }
            if len(p_nums) > 1:
                for span in only_matching:
                    p_num = _paragraph_index(para_spans, span[0]) + 1
                    only_map[span] = f"[P{p_num}] {only_map[span]}"
                for span in not_matching:
                    p_num = _paragraph_index(para_spans, span[0]) + 1
                    not_map[span] = f"[P{p_num}] {not_map[span]}"
        collisions = set(only_map.values()) & set(not_map.values())

    if collisions:
        for mapping in (only_map, not_map):
            for span, text_val in list(mapping.items()):
                if text_val in collisions:
                    mapping[span] = f"{text_val} [{span[0]},{span[1]})"

    only_snippets = list(dict.fromkeys(only_map[span] for span in admitted_spans))
    not_snippets = list(dict.fromkeys(not_map[span] for span in not_at_spans))
    return only_snippets, not_snippets


def format_scoped_reference_lines(
    locked_text: str,
    references: Sequence[TermReference],
    *,
    to_chinese: bool,
    radius: int = SCOPED_SNIPPET_RADIUS,
    compress_threshold: int = SCOPED_ALL_OCCURRENCES_THRESHOLD,
) -> tuple[str, ...]:
    """Render occurrence-scoped references with local context snippets.

    Validates all scoped occurrence coordinates fail-closed via
    ``validated_reference_ranges`` first. Includes both unconditionally
    occurrence-scoped references and paragraph-gated derived references that
    have unadmitted occurrences in ``locked_text``.

    Compression to ``(all occurrences in this text)`` is allowed ONLY when
    ``locked_text`` contains zero other raw occurrences of ``ref.source``
    (``raw_other_count == 0``, including occurrences covered by longer global
    terms). Whenever any other raw occurrence exists — even if covered by a
    longer global term — ``ONLY at:`` snippets are preserved without adding
    global-term spans into ``NOT at:``.
    """
    global_refs, scoped_refs = partition_references_for_prompt(locked_text, references)
    validated_reference_ranges(
        locked_text,
        references,
        to_chinese=to_chinese,
        extra_scoped=scoped_refs,
    )

    global_occ_spans: list[tuple[int, int]] = []
    for ref in global_refs:
        for occ in ref.occurrences:
            if type(occ.model_start) is int and type(occ.model_end) is int:
                global_occ_spans.append((occ.model_start, occ.model_end))

    lines: list[str] = []
    for ref in scoped_refs:
        target = ref.zh if to_chinese else ref.en
        admitted_spans = sorted(
            {
                (occ.model_start, occ.model_end)
                for occ in ref.occurrences
                if type(occ.model_start) is int and type(occ.model_end) is int
            }
        )
        admitted_set = set(admitted_spans)
        raw_other_count = 0
        not_at_spans: list[tuple[int, int]] = []
        for span in _raw_surface_spans(locked_text, ref.source):
            if span in admitted_set:
                continue
            raw_other_count += 1
            if not _is_strictly_enclosed(span, global_occ_spans):
                not_at_spans.append(span)

        header = f"{ref.source} = {target} ({ref.category})"
        if raw_other_count == 0 and len(admitted_spans) > compress_threshold:
            lines.append(f"{header} — (all occurrences in this text)")
        elif not not_at_spans:
            only_snippets = list(
                dict.fromkeys(
                    _local_context_snippet(locked_text, s, e, radius=radius)
                    for s, e in admitted_spans
                )
            )
            lines.append(f"{header} — ONLY at: {'; '.join(only_snippets)}")
        else:
            only_snippets, not_snippets = _disambiguate_scoped_snippets(
                locked_text, admitted_spans, not_at_spans, radius=radius
            )
            lines.append(
                f"{header} — ONLY at: {'; '.join(only_snippets)}"
                f" | NOT at: {'; '.join(not_snippets)}"
            )
    return tuple(lines)


def _paragraph_spans(
    text: str,
    *,
    single_newline_paragraphs: bool = False,
) -> tuple[tuple[int, int], ...]:
    """Return ``(start, end)`` ranges of paragraphs separated by blank lines.

    Under ``translation-context-v1`` (``single_newline_paragraphs=False``, the
    default), only blank lines (``\\n\\n``) split paragraphs and a single ``\\n``
    remains an intra-paragraph line break. Only non-context folded-newline
    paths (``context is None``, where ``prepare_text()`` collapsed blank lines
    to single ``\\n``) opt into ``single_newline_paragraphs=True`` to split
    non-empty lines when no blank line is present.
    """
    spans: list[tuple[int, int]] = []
    if _PARAGRAPH_SPLIT_RE.search(text) is not None:
        pos = 0
        for match in _PARAGRAPH_SPLIT_RE.finditer(text):
            spans.append((pos, match.start()))
            pos = match.end()
        spans.append((pos, len(text)))
        return tuple(spans)
    if single_newline_paragraphs and "\n" in text:
        pos = 0
        for line in text.split("\n"):
            end = pos + len(line)
            if line.strip():
                spans.append((pos, end))
            pos = end + 1
        return tuple(spans) if spans else ((0, len(text)),)
    return ((0, len(text)),)


def _paragraph_index(spans: Sequence[tuple[int, int]], offset: int) -> int:
    for idx, (start, end) in enumerate(spans):
        if start <= offset <= end:
            return idx
    return max(0, len(spans) - 1)


def _paragraphs_with_official_terms(
    context_text: str,
    para_spans: Sequence[tuple[int, int]],
    *,
    lockable: Lockable,
    categories: dict[str, str],
    reference_only: frozenset[str],
    allow_plural_inflection: bool,
    nfkc_spelling_sources: frozenset[str],
    preselected: Sequence[TermSpan] | None = None,
    preselected_offset: int = 0,
) -> frozenset[int]:
    """Return paragraph indices containing >=1 non-derived official dictionary hit.

    Counts both primary non-``reference_only`` dictionary spans and admitted
    single-character resonator/speaker mentions so derived common-noun and
    soft-casing references are gated at natural-paragraph granularity.
    """
    if preselected is not None and preselected_offset == 0:
        primary_spans = list(preselected)
    else:
        primary_spans = [
            span
            for span in select_term_spans(
                context_text,
                lockable,
                context_text,
                0,
                allow_plural_inflection=allow_plural_inflection,
                nfkc_spelling_sources=nfkc_spelling_sources,
            )
            if reference_span_admitted(span, context_text, 0)
        ]

    qualifying: set[int] = {
        _paragraph_index(para_spans, span.start)
        for span in primary_spans
        if span.source not in reference_only
    }
    for source, _official in lockable:
        if len(source) != 1 or not is_cjk_character(source):
            continue
        if categories.get(source, "") not in CONTEXTUAL_REFERENCE_CATEGORIES:
            continue
        start = context_text.find(source)
        while start != -1:
            end = start + 1
            if not any(span.start <= start < span.end for span in primary_spans) and (
                classify_single_character_mention(context_text, start, end)
                == SINGLE_CHAR_NAME_MENTION
            ):
                qualifying.add(_paragraph_index(para_spans, start))
            start = context_text.find(source, start + 1)
    return frozenset(qualifying)


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
                classify_single_character_mention(
                    context_text, context_offset + start, context_offset + end
                )
                == SINGLE_CHAR_NAME_MENTION
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


def _longer_reference_shares_official_target(
    ref: TermReference, global_refs: Sequence[TermReference]
) -> bool:
    """Return whether a longer global reference already names ``ref``'s target.

    ``心`` also opens ``心月狐 = Hsin the Moon Fox``, and ``Hsin`` is the
    official English of both. Hard-locking the one-character mention puts that
    official word in the prompt twice -- once as a locked placeholder's target
    and once as part of the longer term's own line -- and the model then writes
    the placeholder where the longer term's name belongs, which is exactly the
    ``duplicate_placeholder`` rejection this avoids. Leaving the mention as an
    occurrence-scoped reference keeps its official translation in the prompt
    without asking the model to hold the same word in two unrelated forms.
    """
    if not ref.en or not ref.source:
        return False
    pattern = re.compile(rf"\b{re.escape(ref.en)}\b", re.IGNORECASE)
    return any(
        len(other.source) > 1
        and ref.source in other.source
        and bool(other.en)
        and pattern.search(other.en) is not None
        for other in global_refs
    )


def _lock_mixed_single_char_names(
    locked_text: str,
    locks: list[Lock],
    references: list[TermReference],
    *,
    placeholder_prefix: str,
    placeholder_offset: int = 0,
) -> tuple[str, tuple[Lock, ...], tuple[TermReference, ...]]:
    """Selectively lock admitted single-char CJK names when mixed with unadmitted ones.

    On ``zh->en``, when an admitted single-character resonator/speaker name
    coexists in ``locked_text`` with at least one unadmitted raw occurrence of
    the same character that no longer *global* reference encloses
    (``raw_other_count > 0`` — ordinary prose such as ``心很重要``, a novel
    compound such as ``红椿蕊``, or a quoted ``「心」``), keeping ``心 = Hsin``
    in the system prompt triggers negative priming under
    ``thinking: disabled``. Replacing only the admitted spans with opaque
    placeholders (whose ``Locked terms:`` lines expose only the English
    target, never the Chinese source character) and removing the reference
    eliminates leakage while leaving unmixed names (``raw_other_count == 0``,
    which now includes characters that appear only inside longer global
    references such as ``心月狐`` and ``核心``) as contextual references.

    A mixed name is also left as a reference when a longer global reference
    already carries its official target beside the same source character
    (``心`` next to ``心月狐 = Hsin the Moon Fox``): locking it would show
    ``Hsin`` both as a placeholder target and inside the longer term, which is
    the shape that made the model reuse one placeholder across the longer
    term's mentions.
    """
    pairs_by_source: dict[str, set[tuple[str, str]]] = {}
    for ref in references:
        pairs_by_source.setdefault(ref.source, set()).add((ref.zh, ref.en))

    # A ``心`` inside ``心月狐`` / ``应世心`` / ``核心`` is already accounted for
    # by that longer term's own prompt line, so it is not evidence of the
    # negative priming this locking exists for. Counting it would hard-lock a
    # one-character name in a document that never uses the character on its
    # own. Only a raw occurrence that a longer term the model actually reads as
    # a reference encloses counts: a longer surface that
    # ``partition_references_for_prompt()`` routes into the scoped block (a
    # paragraph-gated derived name, a bounded skill stem) is shown as a local
    # snippet keyed to its own admitted occurrences, so it does not make the
    # character behind it unambiguous and must not suppress this lock.
    global_refs, _scoped_refs = partition_references_for_prompt(locked_text, references)
    longer_reference_spans: list[tuple[int, int]] = [
        (occ.model_start, occ.model_end)
        for other in global_refs
        if len(other.source) > 1
        for occ in other.occurrences
        if type(occ.model_start) is int and type(occ.model_end) is int
    ]

    spans_to_lock: list[tuple[int, int, str, str]] = []
    removed_indices: set[int] = set()
    for idx, ref in enumerate(references):
        if (
            ref.reason != "single_char_name_mention"
            or ref.category not in CONTEXTUAL_REFERENCE_CATEGORIES
            or len(ref.source) != 1
            or not is_cjk_character(ref.source)
            or len(pairs_by_source.get(ref.source, ())) != 1
            or not _has_valid_model_occurrences(ref)
            or _longer_reference_shares_official_target(ref, global_refs)
        ):
            continue
        admitted_spans = sorted(
            {
                (occ.model_start, occ.model_end)
                for occ in ref.occurrences
                if type(occ.model_start) is int and type(occ.model_end) is int
            }
        )
        admitted_set = set(admitted_spans)
        raw_other_count = sum(
            1
            for span in _raw_surface_spans(locked_text, ref.source)
            if span not in admitted_set
            and not _is_strictly_enclosed(span, longer_reference_spans)
        )
        if raw_other_count == 0:
            continue
        removed_indices.add(idx)
        for start, end in admitted_spans:
            spans_to_lock.append((start, end, ref.zh, ref.en))

    if not spans_to_lock:
        return locked_text, tuple(locks), tuple(references)

    spans_to_lock.sort(key=lambda item: (item[0], item[1]))
    new_parts: list[str] = []
    new_locks: list[Lock] = list(locks)
    deltas: list[tuple[int, int]] = []
    cursor = 0
    for start, end, zh, en in spans_to_lock:
        if start < cursor:
            continue
        new_parts.append(locked_text[cursor:start])
        placeholder = f"{placeholder_prefix}{placeholder_offset + len(new_locks):04d}__"
        new_parts.append(placeholder)
        new_locks.append((placeholder, zh, en))
        deltas.append((end, len(placeholder) - (end - start)))
        cursor = end
    new_parts.append(locked_text[cursor:])
    updated_text = "".join(new_parts)

    updated_refs: list[TermReference] = []
    for idx, ref in enumerate(references):
        if idx in removed_indices:
            continue
        shifted_occs: list[TermOccurrence] = []
        for occ in ref.occurrences:
            if type(occ.model_start) is int and type(occ.model_end) is int:
                shift = sum(delta for edge, delta in deltas if edge <= occ.model_start)
                shifted_occs.append(
                    TermOccurrence(
                        start=occ.start,
                        end=occ.end,
                        matched_text=occ.matched_text,
                        segment_index=occ.segment_index,
                        model_start=occ.model_start + shift,
                        model_end=occ.model_end + shift,
                    )
                )
            else:
                shifted_occs.append(occ)
        updated_refs.append(
            TermReference(
                source=ref.source,
                zh=ref.zh,
                en=ref.en,
                category=ref.category,
                occurrences=tuple(shifted_occs),
                origin=ref.origin,
                reason=ref.reason,
            )
        )
    return updated_text, tuple(new_locks), tuple(updated_refs)


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
    context_gated_reference_only: frozenset[str] = frozenset(),
    official_context_paragraphs: frozenset[int] | None = None,
    paragraph_spans: tuple[tuple[int, int], ...] | None = None,
    single_newline_paragraphs: bool = False,
    skill_refs: tuple[SkillReference, ...] = (),
    defer_mixed_single_char_locking: bool = False,
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

    # Paragraph-level non-derived official term context gates both derived
    # common-noun references (context_gated_reference_only) and derived
    # soft-casing hints (soft_sources). Isolated ordinary paragraphs such as
    # "我们学校的教学楼很旧。" or "The energy core of a star." stay free of
    # game hints even inside a multi-paragraph request containing game terms
    # in other paragraphs.
    needs_para_gate = to_chinese is not None and (
        (bool(soft_sources) and any(source in text for source in soft_sources))
        or (
            bool(context_gated_reference_only)
            and any(span.source in context_gated_reference_only for span in selected)
        )
    )
    para_spans: tuple[tuple[int, int], ...] = ()
    official_paragraphs: frozenset[int] = frozenset()
    if needs_para_gate:
        para_spans = (
            paragraph_spans
            if paragraph_spans is not None
            else _paragraph_spans(
                context_text,
                single_newline_paragraphs=single_newline_paragraphs,
            )
        )
        if official_context_paragraphs is not None:
            official_paragraphs = official_context_paragraphs
        else:
            official_paragraphs = _paragraphs_with_official_terms(
                context_text,
                para_spans,
                lockable=lockable,
                categories=categories,
                reference_only=reference_only,
                allow_plural_inflection=allow_plural_inflection,
                nfkc_spelling_sources=(
                    nfkc_spelling_sources if context_text == text else frozenset()
                ),
                preselected=selected if context_text == text else None,
                preselected_offset=context_offset,
            )

    if (
        to_chinese is not None
        and soft_sources
        and official_paragraphs
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
            and (
                span.source not in soft_sources
                or _paragraph_index(para_spans, context_offset + span.start)
                in official_paragraphs
            )
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
            suppressed_derived = (
                decision == "reference"
                and span.source in context_gated_reference_only
                and _paragraph_index(para_spans, context_offset + span.start)
                not in official_paragraphs
            )
            if decision == "reference" and not suppressed_derived:
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
    assembled_text = "".join(parts)
    if to_chinese is False and not defer_mixed_single_char_locking:
        final_text, final_locks, final_refs = _lock_mixed_single_char_names(
            assembled_text,
            locks,
            references,
            placeholder_prefix=placeholder_prefix,
            placeholder_offset=placeholder_offset,
        )
        return PreparedTerms(final_text, final_locks, final_refs)
    return PreparedTerms(assembled_text, tuple(locks), tuple(references))


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
    context_gated_reference_only: frozenset[str] = frozenset(),
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
    para_spans: tuple[tuple[int, int], ...] | None = None
    official_paragraphs: frozenset[int] | None = None
    if to_chinese is not None and (candidates or context_gated_reference_only):
        primary_sources = {source for source, _official in lockable}
        soft_sources = frozenset(candidates.keys() - primary_sources) if candidates else frozenset()
        if (
            (soft_sources and any(source in context_text for source in soft_sources))
            or (
                context_gated_reference_only
                and any(source in context_text for source in context_gated_reference_only)
            )
        ):
            para_spans = _paragraph_spans(context_text)
            official_paragraphs = _paragraphs_with_official_terms(
                context_text,
                para_spans,
                lockable=lockable,
                categories=categories,
                reference_only=reference_only,
                allow_plural_inflection=allow_plural_inflection,
                nfkc_spelling_sources=frozenset(),
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
            context_gated_reference_only=context_gated_reference_only,
            official_context_paragraphs=official_paragraphs,
            paragraph_spans=para_spans,
            skill_refs=skill_refs,
            defer_mixed_single_char_locking=True,
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

    assembled = protected.for_model(tuple(locked_segments))
    if to_chinese is False:
        final_text, final_locks, final_refs = _lock_mixed_single_char_names(
            assembled,
            locks,
            references,
            placeholder_prefix=placeholder_prefix,
            placeholder_offset=0,
        )
        return PreparedTerms(final_text, final_locks, final_refs)
    return PreparedTerms(assembled, tuple(locks), tuple(references))
