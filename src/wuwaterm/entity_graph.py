"""Lightweight read-only 1-hop entity relation graph for soft disambiguation.

Loads ``kb_relations`` and active participating nodes/aliases from
``knowledge.db`` into compact interned CSR (Compressed Sparse Row) arrays so
resident memory stays under the 3 MiB hard budget (about 2.1 MiB tracemalloc on
full WutheringData scale). This module sits in ``DOMAIN_CORE`` (not
``PURE_MATCHING``) because it can open ``knowledge.db`` read-only via SQLite.

Invariants:
- Strictly read-only; never mutates ``knowledge.db`` or ``terms.db``.
- Never produces mandatory term locks (every boosted candidate keeps
  ``lock_eligible=False``).
- Only boosts an existing candidate when a high-confidence anchor entity in the
  source text (or explicit document anchor) has a 1-hop relation to that
  candidate; ordinary everyday sentences (e.g. ``香椿炒蛋`` or lowercase
  ``encore``/``echo``/``tuner``) trigger 0 boosts and never seed graph anchors.
"""

from __future__ import annotations

import sqlite3
import sys
from array import array
from collections.abc import Collection, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .cjk_span import SINGLE_CHAR_ORDINARY_WORD, classify_single_character_mention
from .domain_knowledge import (
    DomainKnowledgeCandidate,
    ORDINARY_ENGLISH_OVERLAP_WORDS,
    TRUST_TIERS,
    TrustTier,
    build_normalized_index,
    find_normalized_spans,
    is_rejected_lowercase_single_latin_candidate,
    is_single_cjk_char,
    is_single_latin_word,
)
from .normalize import normalize_text


EdgeDirection = Literal["out", "in"]

DEFAULT_GRAPH_BOOST_PER_ANCHOR = 0.012
DEFAULT_MAX_GRAPH_BOOST = 0.036
_MAX_GLOSS_SCALARS = 180
_MAX_ANCHOR_LABELS_IN_GLOSS = 2
_DIRECT_ANCHOR_CHANNELS = frozenset({"lexical_alias", "short_ngram"})


@dataclass(frozen=True, slots=True)
class RelationEdge:
    """One directed 1-hop incidence edge from a node to its neighbor."""

    neighbor_id: str
    relation_type: str
    direction: EdgeDirection


@dataclass(frozen=True, slots=True)
class EntityNode:
    """Compact metadata for one entity participating in ``kb_relations``."""

    entity_id: str
    entity_type: str
    zh: str
    en: str
    trust_tier: TrustTier


@dataclass(frozen=True)
class GraphBoostOutcome:
    """Result and diagnostics of one 1-hop disambiguation pass."""

    candidates: tuple[DomainKnowledgeCandidate, ...]
    anchor_entity_ids: frozenset[str]
    boosted_entity_ids: frozenset[str]
    triggered_count: int


def _intern(value: str) -> str:
    return sys.intern(value)


def _as_trust_tier(value: str) -> TrustTier:
    if value == "official_authoritative":
        return "official_authoritative"
    if value == "official_derived":
        return "official_derived"
    if value == "community_curated":
        return "community_curated"
    return "official_derived"


def _has_non_ordinary_single_cjk_occurrence(text: str, char: str) -> bool:
    if not text or not is_single_cjk_char(char):
        return False
    index = build_normalized_index(text)
    for start, end in find_normalized_spans(index, char):
        if (
            end - start == 1
            and classify_single_character_mention(text, start, end)
            != SINGLE_CHAR_ORDINARY_WORD
        ):
            return True
    return False


def _is_high_confidence_anchor_surface(surface: str) -> bool:
    """Reject single-char CJK and lowercase single Latin words as standalone anchors."""
    stripped = surface.strip()
    if not stripped or is_single_cjk_char(stripped):
        return False
    if is_single_latin_word(stripped) and stripped.islower():
        return False
    norm = normalize_text(stripped)
    return len(norm) >= 2


def _is_high_confidence_candidate_anchor(
    text: str, candidate: DomainKnowledgeCandidate
) -> bool:
    """Return True only when ``candidate`` is safe to self-seed as a 1-hop anchor."""
    if candidate.retrieval_channel not in _DIRECT_ANCHOR_CHANNELS:
        return False
    if candidate.trust_tier != "official_authoritative":
        return False
    if not _is_high_confidence_anchor_surface(candidate.source_surface):
        return False
    if is_rejected_lowercase_single_latin_candidate(
        text, candidate, uncovered_spans=candidate.matched_source_spans
    ):
        return False
    surf = candidate.source_surface.strip()
    if is_single_latin_word(surf) and surf.casefold() in ORDINARY_ENGLISH_OVERLAP_WORDS:
        # Even for capitalized overlap words (e.g. "Encore", "Echo"), require an
        # explicit uppercase matched span in ``text`` before seeding the graph.
        spans = candidate.matched_source_spans
        if not spans:
            spans = find_normalized_spans(build_normalized_index(text), surf)
        if not any(
            0 <= s < e <= len(text) and any(ch.isupper() for ch in text[s:e])
            for s, e in spans
        ):
            return False
    return True


def _append_relation_gloss(existing_gloss: str, anchor_labels: tuple[str, ...]) -> str:
    if not anchor_labels:
        return existing_gloss
    note = f"[1-hop: {', '.join(anchor_labels[:_MAX_ANCHOR_LABELS_IN_GLOSS])}]"
    if note in existing_gloss:
        return existing_gloss
    combined = f"{existing_gloss} {note}".strip() if existing_gloss else note
    if len(combined) <= _MAX_GLOSS_SCALARS:
        return combined
    if len(note) >= _MAX_GLOSS_SCALARS:
        return note[:_MAX_GLOSS_SCALARS].rstrip()
    prefix_budget = _MAX_GLOSS_SCALARS - len(note) - 1
    if prefix_budget <= 0:
        return note
    return f"{existing_gloss[:prefix_budget].rstrip()} {note}"


class EntityGraph:
    """Read-only in-memory 1-hop relation graph backed by compact CSR arrays."""

    __slots__ = (
        "_entity_ids",
        "_id_to_idx",
        "_node_types",
        "_node_zh",
        "_node_en",
        "_node_tiers",
        "_rel_types",
        "_adj_offsets",
        "_adj_neighbors",
        "_adj_rel_idx",
        "_adj_dir_out",
        "_surface_to_idx",
        "_edge_count",
    )

    def __init__(
        self,
        *,
        entity_ids: tuple[str, ...],
        id_to_idx: dict[str, int],
        node_types: tuple[str, ...],
        node_zh: tuple[str, ...],
        node_en: tuple[str, ...],
        node_tiers: tuple[TrustTier, ...],
        rel_types: tuple[str, ...],
        adj_offsets: array,
        adj_neighbors: array,
        adj_rel_idx: array,
        adj_dir_out: bytearray,
        surface_to_idx: dict[str, int | tuple[int, ...]],
        edge_count: int,
    ) -> None:
        self._entity_ids = entity_ids
        self._id_to_idx = id_to_idx
        self._node_types = node_types
        self._node_zh = node_zh
        self._node_en = node_en
        self._node_tiers = node_tiers
        self._rel_types = rel_types
        self._adj_offsets = adj_offsets
        self._adj_neighbors = adj_neighbors
        self._adj_rel_idx = adj_rel_idx
        self._adj_dir_out = adj_dir_out
        self._surface_to_idx = surface_to_idx
        self._edge_count = edge_count

    @classmethod
    def empty(cls) -> EntityGraph:
        return cls(
            entity_ids=(),
            id_to_idx={},
            node_types=(),
            node_zh=(),
            node_en=(),
            node_tiers=(),
            rel_types=(),
            adj_offsets=array("I", [0]),
            adj_neighbors=array("I"),
            adj_rel_idx=array("H"),
            adj_dir_out=bytearray(),
            surface_to_idx={},
            edge_count=0,
        )

    @classmethod
    def from_edges(
        cls,
        *,
        nodes: Iterable[EntityNode],
        edges: Iterable[tuple[str, str, str]],
        aliases: Iterable[tuple[str, str]] = (),
    ) -> EntityGraph:
        """Build an immutable CSR ``EntityGraph`` from in-memory nodes, edges, and aliases.

        ``edges`` yields ``(src_entity_id, relation_type, dst_entity_id)``.
        ``aliases`` yields ``(entity_id, surface)``.
        Only entities participating in at least one valid edge are retained so
        the resident footprint stays compact.
        """
        raw_nodes: dict[str, EntityNode] = {}
        for node in nodes:
            eid = _intern(node.entity_id)
            raw_nodes[eid] = EntityNode(
                entity_id=eid,
                entity_type=_intern(node.entity_type),
                zh=node.zh,
                en=node.en,
                trust_tier=node.trust_tier
                if node.trust_tier in TRUST_TIERS
                else "official_derived",
            )

        seen_directed: set[tuple[str, str, str]] = set()
        active_id_set: set[str] = set()
        for raw_src, raw_rel, raw_dst in edges:
            src = _intern(raw_src)
            dst = _intern(raw_dst)
            if src == dst or src not in raw_nodes or dst not in raw_nodes:
                continue
            rel = _intern(raw_rel)
            key = (src, rel, dst)
            if key in seen_directed:
                continue
            seen_directed.add(key)
            active_id_set.add(src)
            active_id_set.add(dst)

        if not seen_directed:
            return cls.empty()

        entity_ids = tuple(sorted(active_id_set))
        id_to_idx = {eid: idx for idx, eid in enumerate(entity_ids)}
        node_types = tuple(_intern(raw_nodes[eid].entity_type) for eid in entity_ids)
        node_zh = tuple(raw_nodes[eid].zh for eid in entity_ids)
        node_en = tuple(raw_nodes[eid].en for eid in entity_ids)
        node_tiers = tuple(raw_nodes[eid].trust_tier for eid in entity_ids)

        rel_types = tuple(sorted({rel for _s, rel, _d in seen_directed}))
        rel_to_idx = {rel: idx for idx, rel in enumerate(rel_types)}

        # Build sorted per-node incidence lists, then pack into compact CSR arrays.
        inc: list[list[tuple[int, int, int]]] = [[] for _ in range(len(entity_ids))]
        for src, rel, dst in seen_directed:
            s_idx = id_to_idx[src]
            d_idx = id_to_idx[dst]
            r_idx = rel_to_idx[rel]
            # Third element: 1 == "out", 0 == "in"
            inc[s_idx].append((d_idx, r_idx, 1))
            inc[d_idx].append((s_idx, r_idx, 0))

        adj_offsets = array("I", [0])
        adj_neighbors = array("I")
        adj_rel_idx = array("H")
        adj_dir_out = bytearray()
        for items in inc:
            # Deterministic order matching (neighbor_id, relation_type, direction),
            # where "in" < "out" (0 < 1) and entity_ids/rel_types are sorted.
            items.sort()
            for nb_idx, r_idx, is_out in items:
                adj_neighbors.append(nb_idx)
                adj_rel_idx.append(r_idx)
                adj_dir_out.append(is_out)
            adj_offsets.append(len(adj_neighbors))

        # Compact surface -> entity index map (plain int for unique surfaces).
        surface_to_idx: dict[str, int | tuple[int, ...]] = {}

        def _index_surface(idx: int, surface: str) -> None:
            if not _is_high_confidence_anchor_surface(surface):
                return
            norm = normalize_text(surface)
            if len(norm) < 2:
                return
            existing = surface_to_idx.get(norm)
            if existing is None:
                surface_to_idx[norm] = idx
            elif type(existing) is int:
                if existing != idx:
                    surface_to_idx[norm] = (
                        (existing, idx) if existing < idx else (idx, existing)
                    )
            else:
                assert isinstance(existing, tuple)
                if idx not in existing:
                    surface_to_idx[norm] = tuple(sorted((*existing, idx)))

        for idx in range(len(entity_ids)):
            _index_surface(idx, node_zh[idx])
            _index_surface(idx, node_en[idx])
        for raw_eid, surface in aliases:
            idx_opt = id_to_idx.get(raw_eid)
            if idx_opt is not None:
                _index_surface(idx_opt, surface)

        return cls(
            entity_ids=entity_ids,
            id_to_idx=id_to_idx,
            node_types=node_types,
            node_zh=node_zh,
            node_en=node_en,
            node_tiers=node_tiers,
            rel_types=rel_types,
            adj_offsets=adj_offsets,
            adj_neighbors=adj_neighbors,
            adj_rel_idx=adj_rel_idx,
            adj_dir_out=adj_dir_out,
            surface_to_idx=surface_to_idx,
            edge_count=len(seen_directed),
        )

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> EntityGraph:
        """Load the 1-hop relation graph from an open ``knowledge.db`` connection."""
        edge_rows = conn.execute(
            "SELECT DISTINCT src_entity_id, relation_type, dst_entity_id "
            "FROM kb_relations"
        ).fetchall()
        if not edge_rows:
            return cls.empty()

        edges: list[tuple[str, str, str]] = []
        active_ids: set[str] = set()
        for row in edge_rows:
            src = str(row[0])
            rel = str(row[1])
            dst = str(row[2])
            if src and dst and src != dst:
                edges.append((src, rel, dst))
                active_ids.add(src)
                active_ids.add(dst)

        if not active_ids:
            return cls.empty()

        ordered_ids = sorted(active_ids)
        nodes: list[EntityNode] = []
        aliases: list[tuple[str, str]] = []
        chunk_size = 400
        for start in range(0, len(ordered_ids), chunk_size):
            chunk = ordered_ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            entity_rows = conn.execute(
                f"SELECT entity_id, entity_type, zh, en, trust_tier "
                f"FROM kb_entities WHERE entity_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in entity_rows:
                nodes.append(
                    EntityNode(
                        entity_id=str(row[0]),
                        entity_type=str(row[1]),
                        zh=str(row[2]),
                        en=str(row[3]),
                        trust_tier=_as_trust_tier(str(row[4])),
                    )
                )
            alias_rows = conn.execute(
                f"SELECT entity_id, alias_surface "
                f"FROM kb_aliases WHERE lock_eligible = 0 "
                f"AND entity_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in alias_rows:
                aliases.append((str(row[0]), str(row[1])))

        return cls.from_edges(nodes=nodes, edges=edges, aliases=aliases)

    @classmethod
    def from_knowledge_db(cls, db_path: str | Path) -> EntityGraph:
        """Open ``knowledge.db`` strictly read-only and load its 1-hop graph."""
        path = Path(db_path)
        if not path.is_file():
            raise FileNotFoundError(f"knowledge database does not exist: {path}")
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        with closing(conn):
            conn.execute("PRAGMA query_only = ON;")
            return cls.from_connection(conn)

    def _node_at(self, idx: int) -> EntityNode:
        return EntityNode(
            entity_id=self._entity_ids[idx],
            entity_type=self._node_types[idx],
            zh=self._node_zh[idx],
            en=self._node_en[idx],
            trust_tier=self._node_tiers[idx],
        )

    @property
    def node_count(self) -> int:
        return len(self._entity_ids)

    @property
    def edge_count(self) -> int:
        return self._edge_count

    def node(self, entity_id: str) -> EntityNode | None:
        idx = self._id_to_idx.get(entity_id)
        return None if idx is None else self._node_at(idx)

    def one_hop_edges(self, entity_id: str) -> tuple[RelationEdge, ...]:
        idx = self._id_to_idx.get(entity_id)
        if idx is None:
            return ()
        start = self._adj_offsets[idx]
        end = self._adj_offsets[idx + 1]
        return tuple(
            RelationEdge(
                neighbor_id=self._entity_ids[self._adj_neighbors[pos]],
                relation_type=self._rel_types[self._adj_rel_idx[pos]],
                direction="out" if self._adj_dir_out[pos] else "in",
            )
            for pos in range(start, end)
        )

    def one_hop_neighbors(
        self, entity_id: str
    ) -> tuple[tuple[EntityNode, RelationEdge], ...]:
        """Return deterministic ``(neighbor_node, edge)`` pairs 1 hop away."""
        idx = self._id_to_idx.get(entity_id)
        if idx is None:
            return ()
        start = self._adj_offsets[idx]
        end = self._adj_offsets[idx + 1]
        return tuple(
            (
                self._node_at(self._adj_neighbors[pos]),
                RelationEdge(
                    neighbor_id=self._entity_ids[self._adj_neighbors[pos]],
                    relation_type=self._rel_types[self._adj_rel_idx[pos]],
                    direction="out" if self._adj_dir_out[pos] else "in",
                ),
            )
            for pos in range(start, end)
        )

    def resolve_anchor_entity_ids(
        self,
        *,
        anchor_surfaces: Collection[str] = (),
        anchor_entity_ids: Collection[str] = (),
    ) -> frozenset[str]:
        """Resolve high-confidence anchor entity IDs participating in the graph.

        Single-character CJK surfaces and all-lowercase single Latin words are
        ignored as standalone anchors so everyday prose cannot seed 1-hop boosts.
        """
        if not self._entity_ids:
            return frozenset()
        resolved: set[str] = set()
        for eid in anchor_entity_ids:
            if eid in self._id_to_idx:
                resolved.add(eid)
        for surface in anchor_surfaces:
            if not _is_high_confidence_anchor_surface(surface):
                continue
            norm = normalize_text(surface)
            mapped = self._surface_to_idx.get(norm)
            if mapped is None:
                continue
            if type(mapped) is int:
                resolved.add(self._entity_ids[mapped])
            else:
                assert isinstance(mapped, tuple)
                for idx in mapped:
                    resolved.add(self._entity_ids[idx])
        return frozenset(resolved)

    def connecting_anchors(
        self,
        candidate_entity_id: str,
        anchor_entity_ids: Collection[str],
    ) -> tuple[tuple[EntityNode, RelationEdge], ...]:
        """Return 1-hop edges connecting ``candidate_entity_id`` to ``anchor_entity_ids``."""
        if not anchor_entity_ids:
            return ()
        idx = self._id_to_idx.get(candidate_entity_id)
        if idx is None:
            return ()
        anchor_indices = {
            a_idx
            for eid in anchor_entity_ids
            if (a_idx := self._id_to_idx.get(eid)) is not None and a_idx != idx
        }
        if not anchor_indices:
            return ()
        start = self._adj_offsets[idx]
        end = self._adj_offsets[idx + 1]
        matched: list[tuple[EntityNode, RelationEdge]] = []
        seen_anchors: set[int] = set()
        for pos in range(start, end):
            nb_idx = self._adj_neighbors[pos]
            if nb_idx in anchor_indices and nb_idx not in seen_anchors:
                seen_anchors.add(nb_idx)
                matched.append(
                    (
                        self._node_at(nb_idx),
                        RelationEdge(
                            neighbor_id=self._entity_ids[nb_idx],
                            relation_type=self._rel_types[self._adj_rel_idx[pos]],
                            direction="out" if self._adj_dir_out[pos] else "in",
                        ),
                    )
                )
        return tuple(matched)

    def boost_candidates(
        self,
        text: str,
        candidates: Iterable[DomainKnowledgeCandidate],
        *,
        anchor_surfaces: Collection[str] = (),
        anchor_entity_ids: Collection[str] = (),
        boost_per_anchor: float = DEFAULT_GRAPH_BOOST_PER_ANCHOR,
        max_boost: float = DEFAULT_MAX_GRAPH_BOOST,
    ) -> GraphBoostOutcome:
        """Moderately boost candidates that have a 1-hop link to a high-confidence anchor.

        Guarantees:
        - Everyday sentences with no anchored entities trigger 0 boosts.
        - Ordinary-word single-character CJK occurrences (e.g. ``香椿炒蛋``) and
          lowercase ordinary single Latin words (e.g. ``encore``/``echo``/``tuner``)
          never seed graph anchors and are never boosted.
        - Every returned candidate retains ``lock_eligible=False``.
        """
        candidate_list = tuple(candidates)
        if (
            not text
            or not candidate_list
            or not self._entity_ids
            or boost_per_anchor <= 0.0
            or max_boost <= 0.0
        ):
            return GraphBoostOutcome(
                candidates=candidate_list,
                anchor_entity_ids=frozenset(),
                boosted_entity_ids=frozenset(),
                triggered_count=0,
            )

        derived_anchor_ids: set[str] = set(anchor_entity_ids)
        for candidate in candidate_list:
            if candidate.entity_id in self._id_to_idx and _is_high_confidence_candidate_anchor(
                text, candidate
            ):
                derived_anchor_ids.add(candidate.entity_id)

        resolved_anchors = self.resolve_anchor_entity_ids(
            anchor_surfaces=anchor_surfaces,
            anchor_entity_ids=derived_anchor_ids,
        )
        if not resolved_anchors:
            return GraphBoostOutcome(
                candidates=candidate_list,
                anchor_entity_ids=frozenset(),
                boosted_entity_ids=frozenset(),
                triggered_count=0,
            )

        boosted: list[DomainKnowledgeCandidate] = []
        boosted_ids: set[str] = set()
        triggered_count = 0

        for candidate in candidate_list:
            if candidate.lock_eligible is not False:
                continue
            if is_single_cjk_char(
                candidate.source_surface
            ) and not _has_non_ordinary_single_cjk_occurrence(
                text, candidate.source_surface
            ):
                boosted.append(candidate)
                continue
            if is_rejected_lowercase_single_latin_candidate(
                text, candidate, uncovered_spans=candidate.matched_source_spans
            ):
                boosted.append(candidate)
                continue

            links = self.connecting_anchors(candidate.entity_id, resolved_anchors)
            if not links:
                boosted.append(candidate)
                continue

            boost = min(len(links) * float(boost_per_anchor), float(max_boost))
            anchor_labels = tuple(
                node.zh or node.en for node, _edge in links if (node.zh or node.en)
            )
            enriched_gloss = _append_relation_gloss(
                candidate.context_gloss, anchor_labels
            )
            boosted.append(
                replace(
                    candidate,
                    score=float(candidate.score) + boost,
                    context_gloss=enriched_gloss,
                    lock_eligible=False,
                )
            )
            boosted_ids.add(candidate.entity_id)
            triggered_count += 1

        return GraphBoostOutcome(
            candidates=tuple(boosted),
            anchor_entity_ids=resolved_anchors,
            boosted_entity_ids=frozenset(boosted_ids),
            triggered_count=triggered_count,
        )
