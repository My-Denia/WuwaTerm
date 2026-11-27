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
    RetrievalChannel,
    TRUST_TIERS,
    TrustTier,
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
_WHITESPACE_ONLY_RE = re.compile(r"[ \t\r\n]+")


@dataclass(frozen=True)
class _RawChannelHit:
    """One unmerged retrieval hit carrying its exact evidence tier and spans."""

    entity_id: str
    surface: str
    channel: RetrievalChannel
    rank: int
    evidence_tier: int
    spans: tuple[tuple[int, int], ...]
    trust_tier: TrustTier | None = None


def _is_whitespace_gap(text: str, left_end: int, right_start: int) -> bool:
    """True only when ``text[left_end:right_start]`` is non-empty pure whitespace."""
    return (
        left_end < right_start
        and _WHITESPACE_ONLY_RE.fullmatch(text[left_end:right_start]) is not None
    )
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


def _trimmed_span(text: str) -> tuple[str, tuple[int, int]] | None:
    stripped = text.strip()
    if not stripped:
        return None
    start = len(text) - len(text.lstrip())
    return stripped, (start, start + len(stripped))


def _extract_short_ngram_probes(
    text: str,
) -> tuple[dict[str, set[tuple[int, int]]], dict[str, set[tuple[int, int]]]]:
    """Extract deduplicated ``{ngram: exact_source_spans}`` for bigrams and unigrams.

    Keeping 2-char and 1-char probes in separate high-ceiling buckets prevents
    long inputs from exhausting unigram or tail-position recall, and retaining
    extraction spans avoids false interior-substring matches on unrelated words.
    """
    bigrams: dict[str, set[tuple[int, int]]] = {}
    unigrams: dict[str, set[tuple[int, int]]] = {}

    def add_bi(value: str, span: tuple[int, int]) -> None:
        if len(value) != 2:
            return
        if value in bigrams:
            bigrams[value].add(span)
        elif len(bigrams) < _MAX_SHORT_BIGRAM_PROBES:
            bigrams[value] = {span}

    def add_uni(value: str, span: tuple[int, int]) -> None:
        if len(value) != 1:
            return
        if value in unigrams:
            unigrams[value].add(span)
        elif len(unigrams) < _MAX_SHORT_UNIGRAM_PROBES:
            unigrams[value] = {span}

    trimmed = _trimmed_span(text)
    if trimmed is not None:
        stripped, full_span = trimmed
        norm_full = normalize_text(stripped)
        if len(norm_full) == 2:
            add_bi(norm_full, full_span)
        elif len(norm_full) == 1:
            add_uni(norm_full, full_span)

    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        base = match.start()
        for idx in range(len(run) - 1):
            piece = normalize_text(run[idx : idx + 2])
            add_bi(piece, (base + idx, base + idx + 2))
        for idx, ch in enumerate(run):
            add_uni(normalize_text(ch), (base + idx, base + idx + 1))

    for match in _LATIN_TOKEN_RE.finditer(text):
        tok_norm = normalize_text(match.group(0))
        span = (match.start(), match.end())
        if len(tok_norm) == 2:
            add_bi(tok_norm, span)
        elif len(tok_norm) == 1:
            add_uni(tok_norm, span)

    return bigrams, unigrams


def _extract_alias_probes(text: str) -> dict[str, set[tuple[int, int]]]:
    """Extract deduplicated ``{alias_norm: exact_source_spans}`` from ``text``."""
    probes: dict[str, set[tuple[int, int]]] = {}

    def add(value: str, span: tuple[int, int]) -> None:
        if not value:
            return
        if value in probes:
            probes[value].add(span)
        elif len(probes) < _MAX_ALIAS_PROBES:
            probes[value] = {span}

    trimmed = _trimmed_span(text)
    if trimmed is not None:
        stripped, full_span = trimmed
        add(normalize_text(stripped), full_span)

    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        base = match.start()
        add(normalize_text(run), (base, match.end()))
        max_n = min(len(run), 8)
        for length in range(max_n, 1, -1):
            for idx in range(len(run) - length + 1):
                add(
                    normalize_text(run[idx : idx + length]),
                    (base + idx, base + idx + length),
                )

    latin_matches = list(_LATIN_TOKEN_RE.finditer(text))
    for match in latin_matches:
        add(normalize_text(match.group(0)), (match.start(), match.end()))
    for idx in range(len(latin_matches) - 1):
        m1, m2 = latin_matches[idx], latin_matches[idx + 1]
        if _is_whitespace_gap(text, m1.end(), m2.start()):
            add(normalize_text(text[m1.start() : m2.end()]), (m1.start(), m2.end()))
    for idx in range(len(latin_matches) - 2):
        m1, m2, m3 = (
            latin_matches[idx],
            latin_matches[idx + 1],
            latin_matches[idx + 2],
        )
        if _is_whitespace_gap(text, m1.end(), m2.start()) and _is_whitespace_gap(
            text, m2.end(), m3.start()
        ):
            add(normalize_text(text[m1.start() : m3.end()]), (m1.start(), m3.end()))

    return probes


def _quote_fts5_phrase(term: str) -> str:
    escaped = term.replace('"', '""')
    return f'"{escaped}"'


def _extract_fts_probes(
    text: str,
) -> list[tuple[str, tuple[tuple[int, int], ...]]]:
    """Extract FTS5 trigram probes paired with their exact original ``(start, end)`` spans.

    Every probe satisfies ``text[start:end] == probe`` and multi-word Latin
    probes only span tokens separated by pure whitespace.
    """
    probes: dict[str, set[tuple[int, int]]] = {}

    def add(value: str, span: tuple[int, int]) -> None:
        cleaned = value.strip()
        if len(cleaned) < 3:
            return
        if cleaned in probes:
            probes[cleaned].add(span)
        elif len(probes) < _MAX_FTS_PHRASES:
            probes[cleaned] = {span}

    trimmed = _trimmed_span(text)
    if trimmed is not None:
        stripped, full_span = trimmed
        if 3 <= len(stripped) <= 24:
            add(stripped, full_span)

    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        base = match.start()
        if len(run) >= 3:
            if len(run) <= 16:
                add(run, (base, match.end()))
            for idx in range(0, len(run) - 2):
                add(run[idx : idx + 3], (base + idx, base + idx + 3))

    latin_matches = list(_LATIN_TOKEN_RE.finditer(text))
    for idx in range(len(latin_matches) - 1):
        m1, m2 = latin_matches[idx], latin_matches[idx + 1]
        if not _is_whitespace_gap(text, m1.end(), m2.start()):
            continue
        bigram = text[m1.start() : m2.end()]
        if len(bigram) >= 3:
            add(bigram, (m1.start(), m2.end()))
    for match in latin_matches:
        token = match.group(0)
        if len(token) >= 3:
            add(token, (match.start(), match.end()))

    return [(probe, tuple(sorted(spans))) for probe, spans in probes.items()]


def _as_trust_tier(value: str) -> TrustTier:
    if value == "official_authoritative":
        return "official_authoritative"
    if value == "official_derived":
        return "official_derived"
    if value == "community_curated":
        return "community_curated"
    raise KnowledgeBaseError(f"invalid trust_tier in knowledge.db: {value!r}")


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
        self, text: str
    ) -> list[tuple[str, str, TrustTier, tuple[tuple[int, int], ...]]]:
        probe_spans = _extract_alias_probes(text)
        probes = list(probe_spans)
        if not probes:
            return []
        hits: list[tuple[str, str, TrustTier, tuple[tuple[int, int], ...]]] = []
        seen: set[tuple[str, str]] = set()
        for start in range(0, len(probes), _SQL_IN_CHUNK):
            chunk = probes[start : start + _SQL_IN_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            alias_rows = self._conn.execute(
                f"""
                SELECT entity_id, alias_surface, alias_norm, trust_tier
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
                            tuple(sorted(probe_spans.get(str(row["alias_norm"]), ()))),
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
                zh_norm = str(row["zh_norm"])
                if zh_norm in chunk_set:
                    surface = str(row["zh"])
                    matched_norm = zh_norm
                else:
                    surface = str(row["en"])
                    matched_norm = str(row["en_norm"])
                key = (str(row["entity_id"]), surface)
                if key not in seen:
                    seen.add(key)
                    hits.append(
                        (
                            key[0],
                            surface,
                            _as_trust_tier(str(row["trust_tier"])),
                            tuple(sorted(probe_spans.get(matched_norm, ()))),
                        )
                    )
        return hits

    def _query_short_ngrams(
        self, text: str
    ) -> list[tuple[str, str, tuple[tuple[int, int], ...]]]:
        bigrams, unigrams = _extract_short_ngram_probes(text)
        hits: list[tuple[str, str, tuple[tuple[int, int], ...]]] = []
        seen: set[tuple[str, str]] = set()
        for ngram_len, bucket in ((2, bigrams), (1, unigrams)):
            keys = list(bucket)
            for start in range(0, len(keys), _SQL_IN_CHUNK):
                chunk = keys[start : start + _SQL_IN_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT ngram, entity_id, surface
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
                            (
                                key[0],
                                surface,
                                tuple(sorted(bucket.get(str(row["ngram"]), ()))),
                            )
                        )
        return hits

    def _query_fts_trigram(
        self, text: str, *, limit: int
    ) -> list[tuple[str, str, int, tuple[tuple[int, int], ...]]]:
        extracted = _extract_fts_probes(text)
        if not extracted:
            return []
        cjk_query = has_cjk(text)

        # Query SQLite FTS5 per probe with explicit column filters so SQLite
        # itself decides every probe match and column tier (zero Python-side
        # casefold/substring guessing, immune to Unicode folding differences
        # such as German ß vs ss).
        scored: dict[tuple[str, str, int], float] = {}
        hit_spans: dict[tuple[str, str, int], set[tuple[int, int]]] = {}
        per_probe_limit = max(limit * 4, 32)

        # Tier 0: direct matches on name/alias columns {surface zh en}.
        for probe_raw, probe_spans in extracted:
            fts_direct_query = f"{{surface zh en}} : {_quote_fts5_phrase(probe_raw)}"
            try:
                rows = self._conn.execute(
                    """
                    SELECT entity_id, surface, zh, en,
                           highlight(kb_fts_trigram, 1, '[', ']') AS h_surface,
                           highlight(kb_fts_trigram, 2, '[', ']') AS h_zh,
                           highlight(kb_fts_trigram, 3, '[', ']') AS h_en,
                           bm25(kb_fts_trigram) AS rank_score
                    FROM kb_fts_trigram
                    WHERE kb_fts_trigram MATCH ?
                    ORDER BY rank_score ASC, entity_id ASC
                    LIMIT ?
                    """,
                    (fts_direct_query, per_probe_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue

            for row in rows:
                entity_id = str(row["entity_id"])
                surface = str(row["surface"])
                zh = str(row["zh"])
                en = str(row["en"])
                if len(str(row["h_surface"])) > len(surface):
                    matched_surface = surface
                elif len(str(row["h_zh"])) > len(zh):
                    matched_surface = zh
                elif len(str(row["h_en"])) > len(en):
                    matched_surface = en
                else:
                    matched_surface = surface

                key = (entity_id, matched_surface, 0)
                rank_score = float(row["rank_score"])
                prev = scored.get(key)
                if prev is None or rank_score < prev:
                    scored[key] = rank_score
                hit_spans.setdefault(key, set()).update(probe_spans)

        direct_entities = {entity_id for entity_id, _surface, _tier in scored}

        # Tier 1: gloss-only matches on context_gloss, skipping entities that
        # already have direct tier-0 evidence.
        for probe_raw, probe_spans in extracted:
            fts_gloss_query = f"context_gloss : {_quote_fts5_phrase(probe_raw)}"
            try:
                rows = self._conn.execute(
                    """
                    SELECT entity_id, surface, zh, en,
                           bm25(kb_fts_trigram) AS rank_score
                    FROM kb_fts_trigram
                    WHERE kb_fts_trigram MATCH ?
                    ORDER BY rank_score ASC, entity_id ASC
                    LIMIT ?
                    """,
                    (fts_gloss_query, per_probe_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue

            for row in rows:
                entity_id = str(row["entity_id"])
                if entity_id in direct_entities:
                    continue
                surface = str(row["surface"])
                zh = str(row["zh"])
                en = str(row["en"])
                if is_single_cjk_char(zh):
                    matched_surface = zh
                else:
                    matched_surface = zh if (cjk_query and zh) else (en or surface)

                key = (entity_id, matched_surface, 1)
                rank_score = float(row["rank_score"])
                prev = scored.get(key)
                if prev is None or rank_score < prev:
                    scored[key] = rank_score
                hit_spans.setdefault(key, set()).update(probe_spans)

        ordered = sorted(
            scored.items(),
            key=lambda item: (item[0][2], item[1], item[0][0], item[0][1]),
        )
        return [
            (
                entity_id,
                surface,
                tier_rank,
                tuple(sorted(hit_spans.get((entity_id, surface, tier_rank), ()))),
            )
            for (entity_id, surface, tier_rank), _score in ordered[: max(limit * 2, 16)]
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

        # Stage 1: Collect raw hits across all channels without cross-tier merging.
        raw_hits: list[_RawChannelHit] = []
        for rank, (entity_id, surface, trust_tier, spans) in enumerate(
            self._query_exact_aliases(text), start=1
        ):
            raw_hits.append(
                _RawChannelHit(
                    entity_id=entity_id,
                    surface=surface,
                    channel="lexical_alias",
                    rank=rank,
                    evidence_tier=0,
                    spans=spans,
                    trust_tier=trust_tier,
                )
            )
        for rank, (entity_id, surface, spans) in enumerate(
            self._query_short_ngrams(text), start=1
        ):
            raw_hits.append(
                _RawChannelHit(
                    entity_id=entity_id,
                    surface=surface,
                    channel="short_ngram",
                    rank=rank,
                    evidence_tier=0,
                    spans=spans,
                )
            )
        for rank, (entity_id, surface, evidence_tier, spans) in enumerate(
            self._query_fts_trigram(text, limit=max(max_candidates, 8)), start=1
        ):
            raw_hits.append(
                _RawChannelHit(
                    entity_id=entity_id,
                    surface=surface,
                    channel="fts5_trigram",
                    rank=rank,
                    evidence_tier=evidence_tier,
                    spans=spans,
                )
            )

        if not raw_hits:
            return ()

        # Stage 2: Entity-level evidence-tier truncation BEFORE any span/score merge.
        best_tier_by_entity: dict[str, int] = {}
        for hit in raw_hits:
            prev_tier = best_tier_by_entity.get(hit.entity_id)
            if prev_tier is None or hit.evidence_tier < prev_tier:
                best_tier_by_entity[hit.entity_id] = hit.evidence_tier

        retained_hits = [
            hit
            for hit in raw_hits
            if hit.evidence_tier == best_tier_by_entity[hit.entity_id]
        ]

        # Stage 3: Merge RRF scores and source spans ONLY within retained same-tier hits.
        fused_scores: dict[tuple[str, str], float] = {}
        best_channel: dict[tuple[str, str], tuple[float, RetrievalChannel]] = {}
        tier_overrides: dict[tuple[str, str], TrustTier] = {}
        matched_spans_by_key: dict[tuple[str, str], set[tuple[int, int]]] = {}

        for hit in retained_hits:
            key = (hit.entity_id, hit.surface)
            contribution = _CHANNEL_WEIGHTS[hit.channel] / (RRF_K + hit.rank)
            fused_scores[key] = fused_scores.get(key, 0.0) + contribution
            prev_ch = best_channel.get(key)
            if prev_ch is None or contribution > prev_ch[0]:
                best_channel[key] = (contribution, hit.channel)
            if hit.trust_tier is not None and key not in tier_overrides:
                tier_overrides[key] = hit.trust_tier
            if hit.spans:
                matched_spans_by_key.setdefault(key, set()).update(hit.spans)

        # Stage 4: Build candidates and run lock-coverage / single-char gating.
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
