"""Pure domain-knowledge candidate contract and fail-closed isolation gate.

This module sits in ``PURE_MATCHING``: it has no storage or SQLite dependency
and enforces the invariant that semantic/lexical RAG knowledge candidates can
never silently turn into mandatory placeholder term locks.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from typing import Literal

from .cjk_span import SINGLE_CHAR_ORDINARY_WORD, classify_single_character_mention
from .normalize import SPACE_RE, normalize_text


TrustTier = Literal[
    "official_authoritative",
    "official_derived",
    "community_curated",
]
TRUST_TIERS: frozenset[str] = frozenset(
    {
        "official_authoritative",
        "official_derived",
        "community_curated",
    }
)
_TRUST_TIER_PRIORITY: dict[str, int] = {
    "official_authoritative": 0,
    "official_derived": 1,
    "community_curated": 2,
}

RetrievalChannel = Literal[
    "lexical_alias",
    "short_ngram",
    "fts5_trigram",
    "dense_semantic",
    "graph_neighbor",
]
RETRIEVAL_CHANNELS: frozenset[str] = frozenset(
    {
        "lexical_alias",
        "short_ngram",
        "fts5_trigram",
        "dense_semantic",
        "graph_neighbor",
    }
)

DEFAULT_MAX_CANDIDATES = 8
DEFAULT_MAX_SCALARS = 400


@dataclass(frozen=True)
class DomainKnowledgeCandidate:
    """Immutable soft-context candidate from the companion knowledge base.

    ``lock_eligible`` is typed and runtime-enforced as ``False`` so downstream
    translation planning can only consume these candidates as soft reference
    context, never as mandatory term placeholder locks.
    """

    entity_id: str
    source_surface: str
    zh: str
    en: str
    entity_type: str
    trust_tier: TrustTier
    retrieval_channel: RetrievalChannel
    score: float
    context_gloss: str = ""
    lock_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.lock_eligible is not False:
            raise ValueError(
                "DomainKnowledgeCandidate.lock_eligible must always be False"
            )
        if not self.entity_id or not self.source_surface:
            raise ValueError("entity_id and source_surface must be non-empty")
        if self.trust_tier not in TRUST_TIERS:
            raise ValueError(f"unsupported trust_tier: {self.trust_tier!r}")
        if self.retrieval_channel not in RETRIEVAL_CHANNELS:
            raise ValueError(
                f"unsupported retrieval_channel: {self.retrieval_channel!r}"
            )


def is_single_cjk_char(surface: str) -> bool:
    """Return True when ``surface`` is exactly one CJK unified ideograph."""
    return len(surface) == 1 and "\u3400" <= surface <= "\u9fff"


def candidate_scalar_length(candidate: DomainKnowledgeCandidate) -> int:
    """Return the Unicode scalar budget consumed by one candidate."""
    return (
        len(candidate.source_surface)
        + len(candidate.zh)
        + len(candidate.en)
        + len(candidate.context_gloss)
    )


@dataclass(frozen=True)
class _NormalizedTextIndex:
    """Normalized text aligned back to original ``(start, end)`` spans."""

    norm_text: str
    orig_spans: tuple[tuple[int, int], ...]


def _build_normalized_index(text: str) -> _NormalizedTextIndex:
    norm_chars: list[str] = []
    orig_spans: list[tuple[int, int]] = []
    for idx, ch in enumerate(text):
        piece = SPACE_RE.sub("", unicodedata.normalize("NFKC", ch).casefold())
        for norm_ch in piece:
            norm_chars.append(norm_ch)
            orig_spans.append((idx, idx + 1))
    return _NormalizedTextIndex(
        norm_text="".join(norm_chars),
        orig_spans=tuple(orig_spans),
    )


def _find_normalized_occurrences(
    index: _NormalizedTextIndex, needle: str
) -> list[tuple[int, int]]:
    norm_needle = normalize_text(needle)
    if not norm_needle or not index.norm_text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    needle_len = len(norm_needle)
    while True:
        pos = index.norm_text.find(norm_needle, start)
        if pos < 0:
            break
        orig_start = index.orig_spans[pos][0]
        orig_end = index.orig_spans[pos + needle_len - 1][1]
        spans.append((orig_start, orig_end))
        start = pos + 1
    return spans


def _occupied_spans(
    index: _NormalizedTextIndex, surfaces: Collection[str]
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for surface in surfaces:
        spans.extend(_find_normalized_occurrences(index, surface))
    return spans


def _is_span_covered(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(occ_start <= start and end <= occ_end for occ_start, occ_end in occupied)


def _has_uncovered_non_ordinary_single_cjk(
    text: str,
    index: _NormalizedTextIndex,
    char: str,
    occupied: list[tuple[int, int]],
) -> bool:
    occurrences = _find_normalized_occurrences(index, char)
    for start, end in occurrences:
        if _is_span_covered((start, end), occupied):
            continue
        if (
            end - start == 1
            and classify_single_character_mention(text, start, end)
            != SINGLE_CHAR_ORDINARY_WORD
        ):
            return True
    return False


def select_domain_knowledge_candidates(
    text: str,
    candidates: Iterable[DomainKnowledgeCandidate],
    *,
    locked_surfaces: Collection[str] = (),
    reference_surfaces: Collection[str] = (),
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_scalars: int = DEFAULT_MAX_SCALARS,
) -> tuple[DomainKnowledgeCandidate, ...]:
    """Filter and budget-truncate domain knowledge candidates deterministically.

    Enforcement rules:
    - Rejects any candidate conflicting with ``locked_surfaces`` or
      ``reference_surfaces`` (either by normalized surface/zh/en equality or
      when every normalized occurrence in ``text`` is already covered by a
      locked or referenced span).
    - Jointly evaluates span coverage and single-character CJK classification
      on the SAME uncovered source occurrence via
      ``cjk_span.classify_single_character_mention``: a 1-char CJK candidate
      must have at least one uncovered non-``ordinary_word`` occurrence in
      ``text`` (and a non-surface/gloss match for a 1-char CJK entity cannot
      bypass this gate when its ``source_surface`` is not directly matched in
      ``text``).
    - Deduplicates by ``entity_id`` and normalized ``(zh, en)`` pair in
      deterministic score/trust order.
    - Caps output at ``max_candidates`` entries and ``max_scalars`` total
      Unicode scalars across all returned candidates.
    """
    if not text or max_candidates <= 0 or max_scalars <= 0:
        return ()

    norm_index = _build_normalized_index(text)
    blocked_surfaces = tuple(s for s in (*locked_surfaces, *reference_surfaces) if s)
    blocked_norm = {
        norm for surface in blocked_surfaces if (norm := normalize_text(surface))
    }
    occupied = _occupied_spans(norm_index, blocked_surfaces)

    ordered = sorted(
        candidates,
        key=lambda item: (
            -float(item.score),
            _TRUST_TIER_PRIORITY[item.trust_tier],
            item.entity_id,
            item.source_surface,
        ),
    )

    selected: list[DomainKnowledgeCandidate] = []
    seen_entities: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    used_scalars = 0

    for candidate in ordered:
        if len(selected) >= max_candidates or used_scalars >= max_scalars:
            break
        if candidate.entity_id in seen_entities:
            continue

        surface_norm = normalize_text(candidate.source_surface)
        zh_norm = normalize_text(candidate.zh)
        en_norm = normalize_text(candidate.en)
        if (
            (surface_norm and surface_norm in blocked_norm)
            or (zh_norm and zh_norm in blocked_norm)
            or (en_norm and en_norm in blocked_norm)
        ):
            continue

        surface_occs = _find_normalized_occurrences(norm_index, candidate.source_surface)
        uncovered_surface_occs = [
            span for span in surface_occs if not _is_span_covered(span, occupied)
        ]
        if surface_occs and not uncovered_surface_occs:
            continue

        if is_single_cjk_char(candidate.source_surface):
            if not _has_uncovered_non_ordinary_single_cjk(
                text, norm_index, candidate.source_surface, occupied
            ):
                continue
        elif is_single_cjk_char(candidate.zh):
            # A 1-char CJK entity matched via a non-Chinese surface (e.g. its
            # English name) is allowed only when that surface itself directly
            # matches an uncovered span or substring of the input text.
            # Otherwise (e.g. gloss-only FTS hits), require an uncovered
            # non-ordinary occurrence of the 1-char CJK surface in ``text``.
            surface_directly_matched = bool(uncovered_surface_occs) or (
                len(norm_index.norm_text) >= 3
                and bool(surface_norm)
                and norm_index.norm_text in surface_norm
            )
            if not surface_directly_matched and not _has_uncovered_non_ordinary_single_cjk(
                text, norm_index, candidate.zh, occupied
            ):
                continue

        pair_key = (zh_norm, en_norm)
        if pair_key in seen_pairs:
            continue

        base_cost = (
            len(candidate.source_surface) + len(candidate.zh) + len(candidate.en)
        )
        full_cost = base_cost + len(candidate.context_gloss)
        remaining = max_scalars - used_scalars
        if base_cost > remaining:
            continue
        if full_cost > remaining:
            allowed_gloss = remaining - base_cost
            trimmed_gloss = (
                candidate.context_gloss[:allowed_gloss].rstrip()
                if allowed_gloss > 0
                else ""
            )
            candidate = replace(candidate, context_gloss=trimmed_gloss)
            full_cost = base_cost + len(candidate.context_gloss)

        selected.append(candidate)
        seen_entities.add(candidate.entity_id)
        seen_pairs.add(pair_key)
        used_scalars += full_cost

    return tuple(selected)
