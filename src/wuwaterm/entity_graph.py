"""Lightweight read-only 1-hop entity relation graph for soft disambiguation.

Loads ``kb_relations`` and active participating nodes/aliases from
``knowledge.db`` into compact interned in-memory adjacency tables (designed to
stay well under 3 MiB). This module sits in ``DOMAIN_CORE`` (not
``PURE_MATCHING``) because it can open ``knowledge.db`` read-only via SQLite.

Invariants:
- Strictly read-only; never mutates ``knowledge.db`` or ``terms.db``.
- Never produces mandatory term locks (every boosted candidate keeps
  ``lock_eligible=False``).
- Only boosts an existing candidate when a high-confidence anchor entity in the
  source text (or explicit document anchor) has a 1-hop relation to that
  candidate; ordinary everyday sentences (e.g. ``香椿炒蛋``) trigger 0 boosts.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Collection, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .cjk_span import SINGLE_CHAR_ORDINARY_WORD, classify_single_character_mention
from .domain_knowledge import (
    DomainKnowledgeCandidate,
    TRUST_TIERS,
    TrustTier,
    build_normalized_index,
    find_normalized_spans,
    is_single_cjk_char,
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
    """Single-character CJK surfaces are never high-confidence anchors on their own."""
    stripped = surface.strip()
    if not stripped or is_single_cjk_char(stripped):
        return False
    norm = normalize_text(stripped)
    return len(norm) >= 2


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
    """Read-only in-memory 1-hop relation graph backed by ``kb_relations``."""

    __slots__ = ("_nodes", "_adjacency", "_surface_to_entities", "_edge_count")

    def __init__(
        self,
        *,
        nodes: dict[str, EntityNode],
        adjacency: dict[str, tuple[RelationEdge, ...]],
        surface_to_entities: dict[str, tuple[str, ...]],
        edge_count: int,
    ) -> None:
        self._nodes = nodes
        self._adjacency = adjacency
        self._surface_to_entities = surface_to_entities
        self._edge_count = edge_count

    @classmethod
    def empty(cls) -> EntityGraph:
        return cls(nodes={}, adjacency={}, surface_to_entities={}, edge_count=0)

    @classmethod
    def from_edges(
        cls,
        *,
        nodes: Iterable[EntityNode],
        edges: Iterable[tuple[str, str, str]],
        aliases: Iterable[tuple[str, str]] = (),
    ) -> EntityGraph:
        """Build an immutable ``EntityGraph`` from in-memory nodes, edges, and aliases.

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

        adj_sets: dict[str, set[RelationEdge]] = {}
        seen_directed: set[tuple[str, str, str]] = set()
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
            adj_sets.setdefault(src, set()).add(
                RelationEdge(neighbor_id=dst, relation_type=rel, direction="out")
            )
            adj_sets.setdefault(dst, set()).add(
                RelationEdge(neighbor_id=src, relation_type=rel, direction="in")
            )

        active_nodes: dict[str, EntityNode] = {
            eid: raw_nodes[eid] for eid in adj_sets if eid in raw_nodes
        }
        adjacency: dict[str, tuple[RelationEdge, ...]] = {
            eid: tuple(
                sorted(
                    edge_set,
                    key=lambda edge: (
                        edge.neighbor_id,
                        edge.relation_type,
                        edge.direction,
                    ),
                )
            )
            for eid, edge_set in adj_sets.items()
        }

        surface_sets: dict[str, set[str]] = {}

        def _index_surface(entity_id: str, surface: str) -> None:
            if not _is_high_confidence_anchor_surface(surface):
                return
            norm = normalize_text(surface)
            if len(norm) >= 2:
                surface_sets.setdefault(norm, set()).add(entity_id)

        for eid, node in active_nodes.items():
            _index_surface(eid, node.zh)
            _index_surface(eid, node.en)
        for raw_eid, surface in aliases:
            eid = _intern(raw_eid)
            if eid in active_nodes:
                _index_surface(eid, surface)

        surface_to_entities = {
            norm: tuple(sorted(eids)) for norm, eids in surface_sets.items()
        }
        return cls(
            nodes=active_nodes,
            adjacency=adjacency,
            surface_to_entities=surface_to_entities,
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

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return self._edge_count

    def node(self, entity_id: str) -> EntityNode | None:
        return self._nodes.get(entity_id)

    def one_hop_edges(self, entity_id: str) -> tuple[RelationEdge, ...]:
        return self._adjacency.get(entity_id, ())

    def one_hop_neighbors(
        self, entity_id: str
    ) -> tuple[tuple[EntityNode, RelationEdge], ...]:
        """Return deterministic ``(neighbor_node, edge)`` pairs 1 hop away."""
        edges = self._adjacency.get(entity_id)
        if not edges:
            return ()
        results: list[tuple[EntityNode, RelationEdge]] = []
        for edge in edges:
            neighbor = self._nodes.get(edge.neighbor_id)
            if neighbor is not None:
                results.append((neighbor, edge))
        return tuple(results)

    def resolve_anchor_entity_ids(
        self,
        *,
        anchor_surfaces: Collection[str] = (),
        anchor_entity_ids: Collection[str] = (),
    ) -> frozenset[str]:
        """Resolve high-confidence anchor entity IDs participating in the graph.

        Single-character CJK surfaces are ignored as standalone anchors to
        prevent everyday words from falsely seeding 1-hop disambiguation.
        """
        if not self._nodes:
            return frozenset()
        resolved: set[str] = set()
        for eid in anchor_entity_ids:
            if eid in self._nodes:
                resolved.add(eid)
        for surface in anchor_surfaces:
            if not _is_high_confidence_anchor_surface(surface):
                continue
            norm = normalize_text(surface)
            for eid in self._surface_to_entities.get(norm, ()):
                resolved.add(eid)
        return frozenset(resolved)

    def connecting_anchors(
        self,
        candidate_entity_id: str,
        anchor_entity_ids: Collection[str],
    ) -> tuple[tuple[EntityNode, RelationEdge], ...]:
        """Return 1-hop edges connecting ``candidate_entity_id`` to ``anchor_entity_ids``."""
        if not anchor_entity_ids:
            return ()
        edges = self._adjacency.get(candidate_entity_id)
        if not edges:
            return ()
        anchor_set = (
            anchor_entity_ids
            if isinstance(anchor_entity_ids, (set, frozenset))
            else set(anchor_entity_ids)
        )
        matched: list[tuple[EntityNode, RelationEdge]] = []
        seen_anchors: set[str] = set()
        for edge in edges:
            if edge.neighbor_id == candidate_entity_id:
                continue
            if edge.neighbor_id in anchor_set and edge.neighbor_id not in seen_anchors:
                neighbor = self._nodes.get(edge.neighbor_id)
                if neighbor is not None:
                    seen_anchors.add(edge.neighbor_id)
                    matched.append((neighbor, edge))
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
        - Ordinary-word single-character CJK occurrences (e.g. ``香椿炒蛋``) are
          never boosted even if another anchor exists in the same passage.
        - Every returned candidate retains ``lock_eligible=False``.
        """
        candidate_list = tuple(candidates)
        if (
            not text
            or not candidate_list
            or not self._nodes
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
            if (
                candidate.entity_id in self._nodes
                and candidate.retrieval_channel in _DIRECT_ANCHOR_CHANNELS
                and _is_high_confidence_anchor_surface(candidate.source_surface)
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
