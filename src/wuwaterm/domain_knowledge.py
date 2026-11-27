"""Pure domain-knowledge candidate contract and fail-closed isolation gate.

This module sits in ``PURE_MATCHING``: it has no storage or SQLite dependency
and enforces the invariant that semantic/lexical RAG knowledge candidates can
never silently turn into mandatory placeholder term locks.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from typing import Literal

from .cjk_span import SINGLE_CHAR_ORDINARY_WORD, classify_single_character_mention
from .normalize import normalize_text


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


def _find_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    step = len(needle)
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            break
        spans.append((idx, idx + step))
        start = idx + 1
    return spans


def _occupied_spans(text: str, surfaces: Collection[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for surface in surfaces:
        spans.extend(_find_occurrences(text, surface))
    return spans


def _all_occurrences_covered(
    text: str, surface: str, occupied: list[tuple[int, int]]
) -> bool:
    if not occupied:
        return False
    occurrences = _find_occurrences(text, surface)
    if not occurrences:
        return False
    for start, end in occurrences:
        if not any(occ_start <= start and end <= occ_end for occ_start, occ_end in occupied):
            return False
    return True


def _single_cjk_allowed_in_text(text: str, char: str) -> bool:
    occurrences = _find_occurrences(text, char)
    if not occurrences:
        return False
    return any(
        classify_single_character_mention(text, start, end) != SINGLE_CHAR_ORDINARY_WORD
        for start, end in occurrences
    )


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
      when every occurrence in ``text`` is already covered by a locked or
      referenced span).
    - Hard-blocks 1-char CJK candidates when every occurrence in ``text`` is
      classified as ``ordinary_word`` by
      ``cjk_span.classify_single_character_mention`` (or when the 1-char CJK
      surface does not appear in ``text``).
    - Deduplicates by ``entity_id`` and normalized ``(zh, en)`` pair in
      deterministic score/trust order.
    - Caps output at ``max_candidates`` entries and ``max_scalars`` total
      Unicode scalars across all returned candidates.
    """
    if not text or max_candidates <= 0 or max_scalars <= 0:
        return ()

    blocked_surfaces = tuple(s for s in (*locked_surfaces, *reference_surfaces) if s)
    blocked_norm = {
        norm for surface in blocked_surfaces if (norm := normalize_text(surface))
    }
    occupied = _occupied_spans(text, blocked_surfaces)

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
        if _all_occurrences_covered(text, candidate.source_surface, occupied):
            continue

        if is_single_cjk_char(candidate.source_surface):
            if not _single_cjk_allowed_in_text(text, candidate.source_surface):
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
