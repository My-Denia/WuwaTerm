"""Companion read-only SQLite knowledge base schema and Lexical RAG reader.

``knowledge.db`` is strictly separated from ``terms.db``:
- Bound cryptographically to ``terms_db_revision`` (fail-closed on mismatch).
- Hard-constrained at the SQLite layer so ``kb_aliases.lock_eligible = 0``.
- Opened strictly read-only (``mode=ro`` + ``PRAGMA query_only = ON``) by
  ``KnowledgeBaseReader``; never persists user text, TM, or manuscripts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Collection
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .domain_knowledge import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_SCALARS,
    DomainKnowledgeCandidate,
    NormalizedTextIndex,
    RetrievalChannel,
    TRUST_TIERS,
    TrustTier,
    build_normalized_index,
    find_normalized_spans,
    is_single_cjk_char,
    select_domain_knowledge_candidates,
)
from .normalize import has_cjk, normalize_text


KB_SCHEMA_VERSION = "1"
RRF_K = 60

EXPECTED_KB_LOGICAL_TABLES = frozenset(
    {
        "kb_metadata",
        "kb_entities",
        "kb_aliases",
        "kb_contexts",
        "kb_relations",
        "kb_fts_trigram",
        "kb_short_ngrams",
    }
)
EXPECTED_KB_FTS_SHADOW_TABLES = frozenset(
    {
        "kb_fts_trigram_data",
        "kb_fts_trigram_idx",
        "kb_fts_trigram_content",
        "kb_fts_trigram_docsize",
        "kb_fts_trigram_config",
    }
)
EXPECTED_KB_ALL_TABLES = EXPECTED_KB_LOGICAL_TABLES | EXPECTED_KB_FTS_SHADOW_TABLES

KB_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS kb_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_entities (
  entity_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  source_file TEXT NOT NULL,
  source_id TEXT NOT NULL,
  zh TEXT NOT NULL,
  en TEXT NOT NULL,
  zh_norm TEXT NOT NULL,
  en_norm TEXT NOT NULL,
  trust_tier TEXT NOT NULL CHECK(
    trust_tier IN ('official_authoritative', 'official_derived', 'community_curated')
  ),
  context_gloss TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_kb_entities_type ON kb_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_kb_entities_zh_norm ON kb_entities(zh_norm);
CREATE INDEX IF NOT EXISTS idx_kb_entities_en_norm ON kb_entities(en_norm);

CREATE TABLE IF NOT EXISTS kb_aliases (
  id INTEGER PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES kb_entities(entity_id) ON DELETE CASCADE,
  alias_surface TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  lang TEXT NOT NULL CHECK(lang IN ('zh', 'en', 'other')),
  alias_kind TEXT NOT NULL,
  trust_tier TEXT NOT NULL CHECK(
    trust_tier IN ('official_authoritative', 'official_derived', 'community_curated')
  ),
  lock_eligible INTEGER NOT NULL DEFAULT 0 CHECK(lock_eligible = 0),
  UNIQUE(entity_id, alias_surface, lang, alias_kind)
);

CREATE INDEX IF NOT EXISTS idx_kb_aliases_norm ON kb_aliases(alias_norm);
CREATE INDEX IF NOT EXISTS idx_kb_aliases_entity ON kb_aliases(entity_id);

CREATE TABLE IF NOT EXISTS kb_contexts (
  id INTEGER PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES kb_entities(entity_id) ON DELETE CASCADE,
  context_kind TEXT NOT NULL,
  source_file TEXT NOT NULL,
  text_key TEXT NOT NULL,
  zh_text TEXT NOT NULL,
  en_text TEXT NOT NULL,
  UNIQUE(entity_id, context_kind, text_key)
);

CREATE INDEX IF NOT EXISTS idx_kb_contexts_entity ON kb_contexts(entity_id);

CREATE TABLE IF NOT EXISTS kb_relations (
  id INTEGER PRIMARY KEY,
  src_entity_id TEXT NOT NULL REFERENCES kb_entities(entity_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  dst_entity_id TEXT NOT NULL REFERENCES kb_entities(entity_id) ON DELETE CASCADE,
  source_file TEXT NOT NULL,
  evidence_key TEXT NOT NULL DEFAULT '',
  UNIQUE(src_entity_id, relation_type, dst_entity_id, evidence_key)
);

CREATE INDEX IF NOT EXISTS idx_kb_relations_src ON kb_relations(src_entity_id);
CREATE INDEX IF NOT EXISTS idx_kb_relations_dst ON kb_relations(dst_entity_id);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts_trigram USING fts5(
  entity_id UNINDEXED,
  surface,
  zh,
  en,
  context_gloss,
  tokenize='trigram case_sensitive 0'
);

CREATE TABLE IF NOT EXISTS kb_short_ngrams (
  ngram TEXT NOT NULL,
  ngram_len INTEGER NOT NULL CHECK(ngram_len IN (1, 2)),
  entity_id TEXT NOT NULL REFERENCES kb_entities(entity_id) ON DELETE CASCADE,
  surface TEXT NOT NULL,
  lang TEXT NOT NULL CHECK(lang IN ('zh', 'en', 'other')),
  PRIMARY KEY (ngram, entity_id, surface)
);

CREATE INDEX IF NOT EXISTS idx_kb_short_ngrams_lookup
  ON kb_short_ngrams(ngram, ngram_len);
"""


class KnowledgeBaseError(RuntimeError):
    """Base error raised when opening or reading ``knowledge.db`` fails."""


class KnowledgeBaseRevisionMismatchError(KnowledgeBaseError):
    """Raised fail-closed when ``kb_metadata.terms_db_revision`` mismatches."""


@dataclass(frozen=True)
class KnowledgeRelation:
    """One 1-hop typed edge from ``kb_relations``."""

    src_entity_id: str
    relation_type: str
    dst_entity_id: str
    source_file: str
    evidence_key: str


_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
_CHANNEL_WEIGHTS: dict[RetrievalChannel, float] = {
    "lexical_alias": 1.35,
    "short_ngram": 1.15,
    "fts5_trigram": 1.0,
    "dense_semantic": 0.95,
    "graph_neighbor": 0.85,
}
_MAX_SHORT_UNIGRAM_PROBES = 4096
_MAX_SHORT_BIGRAM_PROBES = 4096
_MAX_ALIAS_PROBES = 4096
_MAX_FTS_PHRASES = 240
_FTS_BATCH_SIZE = 24
_SQL_IN_CHUNK = 200


def initialize_knowledge_db(conn: sqlite3.Connection) -> None:
    """Create the companion knowledge base schema on ``conn``."""
    conn.executescript(KB_SCHEMA)


def connect_knowledge_db_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing ``knowledge.db`` strictly read-only."""
    path = Path(db_path)
    if not path.is_file():
        raise KnowledgeBaseError(
            f"knowledge database does not exist or is not a file: {path}"
        )
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise KnowledgeBaseError(
            f"cannot open knowledge database read-only: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def compute_terms_db_revision(terms_db_path: str | Path) -> str:
    """Compute the canonical SHA-256 revision of a ``terms.db`` file.

    Matches ``wuwaterm.lookup.dictionary_revision`` bit-for-bit without
    coupling the companion knowledge DB module to ``lookup.py``.
    """
    path = Path(terms_db_path)
    if not path.is_file():
        raise KnowledgeBaseError(f"terms database does not exist: {path}")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise KnowledgeBaseError(f"cannot open terms database: {exc}") from exc
    with closing(conn):
        conn.row_factory = sqlite3.Row
        rows = sorted(
            (
                str(row["category"]),
                str(row["source_file"]),
                str(row["source_id"]),
                str(row["text_key"]),
                str(row["zh"]),
                str(row["en"]),
                str(row["pinyin"]),
                str(row["pinyin_abbrev"]),
            )
            for row in conn.execute(
                "SELECT category, source_file, source_id, text_key, zh, en, "
                "pinyin, pinyin_abbrev FROM terms"
            )
        )
    payload = json.dumps(
        ("wuwaterm-review-dictionary-v1", rows),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_short_ngram_probes(text: str) -> tuple[list[str], list[str]]:
    """Extract deduplicated (bigrams, unigrams) in separate buckets.

    Keeping 2-char and 1-char probes in separate high-ceiling buckets prevents
    long inputs from exhausting unigram or tail-position recall.
    """
    bigrams: list[str] = []
    unigrams: list[str] = []
    seen_bi: set[str] = set()
    seen_uni: set[str] = set()

    def add_bi(value: str) -> None:
        if len(value) == 2 and value not in seen_bi and len(bigrams) < _MAX_SHORT_BIGRAM_PROBES:
            seen_bi.add(value)
            bigrams.append(value)

    def add_uni(value: str) -> None:
        if len(value) == 1 and value not in seen_uni and len(unigrams) < _MAX_SHORT_UNIGRAM_PROBES:
            seen_uni.add(value)
            unigrams.append(value)

    norm_full = normalize_text(text)
    if len(norm_full) == 2:
        add_bi(norm_full)
    elif len(norm_full) == 1:
        add_uni(norm_full)

    for run in _CJK_RUN_RE.findall(text):
        run_norm = normalize_text(run)
        for idx in range(len(run_norm) - 1):
            add_bi(run_norm[idx : idx + 2])
        for ch in run_norm:
            add_uni(ch)

    for token in _LATIN_TOKEN_RE.findall(text):
        tok_norm = normalize_text(token)
        if len(tok_norm) == 2:
            add_bi(tok_norm)
        elif len(tok_norm) == 1:
            add_uni(tok_norm)

    return bigrams, unigrams


def _extract_alias_probes(text: str) -> list[str]:
    probes: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen and len(probes) < _MAX_ALIAS_PROBES:
            seen.add(value)
            probes.append(value)

    norm_full = normalize_text(text)
    if norm_full:
        add(norm_full)

    for run in _CJK_RUN_RE.findall(text):
        run_norm = normalize_text(run)
        if run_norm:
            add(run_norm)
        max_n = min(len(run_norm), 8)
        for length in range(max_n, 1, -1):
            for idx in range(len(run_norm) - length + 1):
                add(run_norm[idx : idx + length])

    latin_tokens = _LATIN_TOKEN_RE.findall(text)
    for token in latin_tokens:
        add(normalize_text(token))
    for idx in range(len(latin_tokens) - 1):
        add(normalize_text(f"{latin_tokens[idx]} {latin_tokens[idx + 1]}"))
    for idx in range(len(latin_tokens) - 2):
        add(
            normalize_text(
                f"{latin_tokens[idx]} {latin_tokens[idx + 1]} {latin_tokens[idx + 2]}"
            )
        )

    return probes


def _quote_fts5_phrase(term: str) -> str:
    escaped = term.replace('"', '""')
    return f'"{escaped}"'


def _extract_fts_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip()
        if len(cleaned) >= 3 and cleaned not in seen and len(phrases) < _MAX_FTS_PHRASES:
            seen.add(cleaned)
            phrases.append(cleaned)

    stripped = text.strip()
    if 3 <= len(stripped) <= 24:
        add(stripped)

    for run in _CJK_RUN_RE.findall(stripped):
        if len(run) >= 3:
            if len(run) <= 16:
                add(run)
            for idx in range(0, len(run) - 2):
                add(run[idx : idx + 3])

    latin_tokens = _LATIN_TOKEN_RE.findall(stripped)
    for idx in range(len(latin_tokens) - 1):
        bigram = f"{latin_tokens[idx]} {latin_tokens[idx + 1]}"
        if len(bigram) >= 3:
            add(bigram)
    for token in latin_tokens:
        if len(token) >= 3:
            add(token)

    return phrases


def _as_trust_tier(value: str) -> TrustTier:
    if value == "official_authoritative":
        return "official_authoritative"
    if value == "official_derived":
        return "official_derived"
    if value == "community_curated":
        return "community_curated"
    raise KnowledgeBaseError(f"invalid trust_tier in knowledge.db: {value!r}")


def _matched_probe_spans_for_field(
    norm_index: NormalizedTextIndex,
    field_text: str,
    field_norm: str,
    phrases: list[tuple[str, str]],
) -> list[tuple[int, int]]:
    """Return source spans in ``norm_index`` that actually matched ``field_norm``."""
    if not field_norm:
        return []
    spans: list[tuple[int, int]] = []
    if field_norm in norm_index.norm_text:
        spans.extend(find_normalized_spans(norm_index, field_text))
    for phrase, phrase_norm in phrases:
        if len(phrase_norm) >= 3 and phrase_norm in field_norm:
            spans.extend(find_normalized_spans(norm_index, phrase))
    return spans


class KnowledgeBaseReader:
    """Read-only Lexical RAG retriever bound to a specific ``terms.db`` revision."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        expected_terms_db_revision: str,
    ) -> None:
        if not expected_terms_db_revision or not expected_terms_db_revision.strip():
            raise KnowledgeBaseError("expected_terms_db_revision must be non-empty")
        self._db_path = Path(db_path)
        self._expected_terms_db_revision = expected_terms_db_revision.strip()
        self._conn = connect_knowledge_db_read_only(self._db_path)
        try:
            self._metadata = self._load_and_validate_metadata()
        except Exception:
            self._conn.close()
            raise

    def _load_and_validate_metadata(self) -> dict[str, str]:
        try:
            rows = self._conn.execute(
                "SELECT key, value FROM kb_metadata ORDER BY key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise KnowledgeBaseError(
                f"failed reading kb_metadata from {self._db_path}: {exc}"
            ) from exc
        metadata = {str(row["key"]): str(row["value"]) for row in rows}
        schema_version = metadata.get("kb_schema_version")
        if schema_version != KB_SCHEMA_VERSION:
            raise KnowledgeBaseError(
                f"unsupported kb_schema_version: expected {KB_SCHEMA_VERSION!r}, "
                f"got {schema_version!r}"
            )
        actual_revision = metadata.get("terms_db_revision")
        if actual_revision != self._expected_terms_db_revision:
            raise KnowledgeBaseRevisionMismatchError(
                "terms_db_revision mismatch (fail-closed): "
                f"expected {self._expected_terms_db_revision!r}, "
                f"got {actual_revision!r}"
            )
        return metadata

    @property
    def metadata(self) -> dict[str, str]:
        return dict(self._metadata)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> KnowledgeBaseReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def entity_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM kb_entities").fetchone()
        return int(row["count"]) if row is not None else 0

    def neighbors(self, entity_id: str) -> tuple[KnowledgeRelation, ...]:
        """Return 1-hop graph relations incident to ``entity_id``."""
        rows = self._conn.execute(
            """
            SELECT src_entity_id, relation_type, dst_entity_id, source_file, evidence_key
            FROM kb_relations
            WHERE src_entity_id = ? OR dst_entity_id = ?
            ORDER BY src_entity_id, relation_type, dst_entity_id, evidence_key
            """,
            (entity_id, entity_id),
        ).fetchall()
        return tuple(
            KnowledgeRelation(
                src_entity_id=str(row["src_entity_id"]),
                relation_type=str(row["relation_type"]),
                dst_entity_id=str(row["dst_entity_id"]),
                source_file=str(row["source_file"]),
                evidence_key=str(row["evidence_key"]),
            )
            for row in rows
        )

    def _query_exact_aliases(
        self, text: str, norm_index: NormalizedTextIndex
    ) -> list[tuple[str, str, TrustTier, tuple[tuple[int, int], ...]]]:
        probes = _extract_alias_probes(text)
        if not probes:
            return []
        hits: list[tuple[str, str, TrustTier, tuple[tuple[int, int], ...]]] = []
        seen: set[tuple[str, str]] = set()
        for start in range(0, len(probes), _SQL_IN_CHUNK):
            chunk = probes[start : start + _SQL_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            alias_rows = self._conn.execute(
                f"""
                SELECT entity_id, alias_surface, trust_tier
                FROM kb_aliases
                WHERE alias_norm IN ({placeholders})
                  AND lock_eligible = 0
                ORDER BY LENGTH(alias_surface) DESC, entity_id, alias_surface
                """,
                chunk,
            ).fetchall()
            for row in alias_rows:
                surface = str(row["alias_surface"])
                key = (str(row["entity_id"]), surface)
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        (
                            key[0],
                            surface,
                            _as_trust_tier(str(row["trust_tier"])),
                            find_normalized_spans(norm_index, surface),
                        )
                    )

            entity_rows = self._conn.execute(
                f"""
                SELECT entity_id, zh, en, zh_norm, en_norm, trust_tier
                FROM kb_entities
                WHERE zh_norm IN ({placeholders}) OR en_norm IN ({placeholders})
                ORDER BY entity_id
                """,
                [*chunk, *chunk],
            ).fetchall()
            chunk_set = set(chunk)
            for row in entity_rows:
                surface = (
                    str(row["zh"])
                    if str(row["zh_norm"]) in chunk_set
                    else str(row["en"])
                )
                key = (str(row["entity_id"]), surface)
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        (
                            key[0],
                            surface,
                            _as_trust_tier(str(row["trust_tier"])),
                            find_normalized_spans(norm_index, surface),
                        )
                    )
        return hits

    def _query_short_ngrams(
        self, text: str, norm_index: NormalizedTextIndex
    ) -> list[tuple[str, str, tuple[tuple[int, int], ...]]]:
        bigrams, unigrams = _extract_short_ngram_probes(text)
        hits: list[tuple[str, str, tuple[tuple[int, int], ...]]] = []
        seen: set[tuple[str, str]] = set()
        for ngram_len, bucket in ((2, bigrams), (1, unigrams)):
            for start in range(0, len(bucket), _SQL_IN_CHUNK):
                chunk = bucket[start : start + _SQL_IN_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT entity_id, surface
                    FROM kb_short_ngrams
                    WHERE ngram_len = ? AND ngram IN ({placeholders})
                    ORDER BY entity_id, surface
                    """,
                    [ngram_len, *chunk],
                ).fetchall()
                for row in rows:
                    surface = str(row["surface"])
                    key = (str(row["entity_id"]), surface)
                    if key not in seen:
                        seen.add(key)
                        hits.append(
                            (key[0], surface, find_normalized_spans(norm_index, surface))
                        )
        return hits

    def _query_fts_trigram(
        self, text: str, norm_index: NormalizedTextIndex, *, limit: int
    ) -> list[tuple[str, str, tuple[tuple[int, int], ...]]]:
        phrases = _extract_fts_phrases(text)
        if not phrases:
            return []
        phrase_pairs = [
            (phrase, norm)
            for phrase in phrases
            if (norm := normalize_text(phrase))
        ]
        cjk_query = has_cjk(text)

        scored: dict[tuple[str, str], tuple[int, float]] = {}
        hit_spans: dict[tuple[str, str], set[tuple[int, int]]] = {}
        batch_limit = max(limit * 4, 32)
        for start in range(0, len(phrases), _FTS_BATCH_SIZE):
            batch = phrases[start : start + _FTS_BATCH_SIZE]
            fts_query = " OR ".join(_quote_fts5_phrase(phrase) for phrase in batch)
            try:
                rows = self._conn.execute(
                    """
                    SELECT entity_id, surface, zh, en, context_gloss,
                           bm25(kb_fts_trigram) AS rank_score
                    FROM kb_fts_trigram
                    WHERE kb_fts_trigram MATCH ?
                    ORDER BY rank_score ASC, entity_id ASC
                    LIMIT ?
                    """,
                    (fts_query, batch_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue

            for row in rows:
                entity_id = str(row["entity_id"])
                surface = str(row["surface"])
                zh = str(row["zh"])
                en = str(row["en"])
                gloss = str(row["context_gloss"])
                surf_norm = normalize_text(surface)
                zh_norm = normalize_text(zh)
                en_norm = normalize_text(en)

                surface_spans = _matched_probe_spans_for_field(
                    norm_index, surface, surf_norm, phrase_pairs
                )
                zh_spans = _matched_probe_spans_for_field(
                    norm_index, zh, zh_norm, phrase_pairs
                )
                en_spans = _matched_probe_spans_for_field(
                    norm_index, en, en_norm, phrase_pairs
                )

                if surface_spans or zh_spans or en_spans:
                    matched_surface = (
                        surface if surface_spans else (zh if zh_spans else en)
                    )
                    matched_spans = [*surface_spans, *zh_spans, *en_spans]
                    tier_rank = 0
                else:
                    # Gloss-only match: never report the entity's English name as
                    # ``source_surface`` for a 1-char CJK entity, because that
                    # would bypass single-character CJK ordinary-word filtering.
                    if is_single_cjk_char(zh):
                        matched_surface = zh
                    else:
                        matched_surface = zh if (cjk_query and zh) else (en or surface)
                    matched_spans = _matched_probe_spans_for_field(
                        norm_index, gloss, normalize_text(gloss), phrase_pairs
                    )
                    tier_rank = 1

                key = (entity_id, matched_surface)
                cand_rank = (tier_rank, float(row["rank_score"]))
                prev = scored.get(key)
                if prev is None or cand_rank < prev:
                    scored[key] = cand_rank
                hit_spans.setdefault(key, set()).update(matched_spans)

        ordered = sorted(
            scored.items(),
            key=lambda item: (item[1][0], item[1][1], item[0][0], item[0][1]),
        )
        return [
            (key[0], key[1], tuple(sorted(hit_spans.get(key, ()))))
            for key, _ in ordered[: max(limit * 2, 16)]
        ]

    def _load_entities(self, entity_ids: Collection[str]) -> dict[str, sqlite3.Row]:
        if not entity_ids:
            return {}
        ordered = sorted(set(entity_ids))
        entities: dict[str, sqlite3.Row] = {}
        for start in range(0, len(ordered), _SQL_IN_CHUNK):
            chunk = ordered[start : start + _SQL_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""
                SELECT entity_id, entity_type, zh, en, trust_tier, context_gloss
                FROM kb_entities
                WHERE entity_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                entities[str(row["entity_id"])] = row
        return entities

    def query(
        self,
        text: str,
        *,
        locked_surfaces: Collection[str] = (),
        reference_surfaces: Collection[str] = (),
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_scalars: int = DEFAULT_MAX_SCALARS,
    ) -> tuple[DomainKnowledgeCandidate, ...]:
        """Run hybrid Lexical RAG retrieval (exact alias + short-ngram + FTS5 trigram) with RRF fusion.

        Every returned item is an immutable ``DomainKnowledgeCandidate`` with
        ``lock_eligible=False`` and has passed the ``select_domain_knowledge_candidates``
        isolation and budget gate.
        """
        if not text or not text.strip():
            return ()

        norm_index = build_normalized_index(text)
        alias_hits = self._query_exact_aliases(text, norm_index)
        short_hits = self._query_short_ngrams(text, norm_index)
        fts_hits = self._query_fts_trigram(
            text, norm_index, limit=max(max_candidates, 8)
        )

        fused_scores: dict[tuple[str, str], float] = {}
        best_channel: dict[tuple[str, str], tuple[float, RetrievalChannel]] = {}
        tier_overrides: dict[tuple[str, str], TrustTier] = {}
        matched_spans_by_key: dict[tuple[str, str], set[tuple[int, int]]] = {}

        def record_channel(
            channel: RetrievalChannel,
            items: list[tuple[str, str, tuple[tuple[int, int], ...]]],
        ) -> None:
            weight = _CHANNEL_WEIGHTS[channel]
            for rank, (entity_id, surface, spans) in enumerate(items, start=1):
                key = (entity_id, surface)
                contribution = weight / (RRF_K + rank)
                fused_scores[key] = fused_scores.get(key, 0.0) + contribution
                prev = best_channel.get(key)
                if prev is None or contribution > prev[0]:
                    best_channel[key] = (contribution, channel)
                if spans:
                    matched_spans_by_key.setdefault(key, set()).update(spans)

        for entity_id, surface, tier, _spans in alias_hits:
            tier_overrides[(entity_id, surface)] = tier
        record_channel("lexical_alias", [(e, s, sp) for e, s, _, sp in alias_hits])
        record_channel("short_ngram", short_hits)
        record_channel("fts5_trigram", fts_hits)

        if not fused_scores:
            return ()

        entities = self._load_entities({entity_id for entity_id, _ in fused_scores})
        candidates: list[DomainKnowledgeCandidate] = []
        for (entity_id, surface), score in fused_scores.items():
            row = entities.get(entity_id)
            if row is None:
                continue
            tier_raw = tier_overrides.get((entity_id, surface), str(row["trust_tier"]))
            if tier_raw not in TRUST_TIERS:
                continue
            candidates.append(
                DomainKnowledgeCandidate(
                    entity_id=entity_id,
                    source_surface=surface,
                    zh=str(row["zh"]),
                    en=str(row["en"]),
                    entity_type=str(row["entity_type"]),
                    trust_tier=_as_trust_tier(tier_raw),
                    retrieval_channel=best_channel[(entity_id, surface)][1],
                    score=score,
                    context_gloss=str(row["context_gloss"]),
                    lock_eligible=False,
                    matched_source_spans=tuple(
                        sorted(matched_spans_by_key.get((entity_id, surface), ()))
                    ),
                )
            )

        return select_domain_knowledge_candidates(
            text,
            candidates,
            locked_surfaces=locked_surfaces,
            reference_surfaces=reference_surfaces,
            max_candidates=max_candidates,
            max_scalars=max_scalars,
        )
