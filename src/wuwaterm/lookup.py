"""Dictionary-first term lookup and fuzzy matching."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .constants import CATEGORY_ORDER
from .db import connect, row_to_entry
from .models import LookupCandidate, LookupResult, TermEntry
from .normalize import normalize_ascii, normalize_text


@dataclass(frozen=True)
class ReviewSnapshot:
    """One transaction's complete dictionary basis for pair review."""

    metadata: dict[str, str]
    entries: tuple[TermEntry, ...]

    @property
    def term_count(self) -> int:
        return len(self.entries)


class TermService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def lookup(self, query: str, limit: int = 5) -> LookupResult:
        query = query.strip()
        if not query:
            return LookupResult(query=query, exact=False, candidates=())

        exact = self._exact(query)
        if exact:
            return LookupResult(query=query, exact=True, candidates=tuple(exact[:limit]))
        return LookupResult(query=query, exact=False, candidates=tuple(self._fuzzy(query, limit)))

    def lookup_exact(self, query: str, limit: int = 5) -> LookupResult:
        query = query.strip()
        if not query:
            return LookupResult(query=query, exact=False, candidates=())

        exact = self._exact(query)
        return LookupResult(query=query, exact=bool(exact), candidates=tuple(exact[:limit]))

    def term_text(self, query: str) -> str | None:
        result = self.lookup(query, limit=5)
        if not result.candidates:
            return None
        if result.exact:
            return result.candidates[0].entry.en
        return "\n".join(
            f"{candidate.entry.zh} -> {candidate.entry.en} [{candidate.entry.category}, {candidate.score:.0f}]"
            for candidate in result.candidates
        )

    def entries(self) -> list[TermEntry]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM terms
                ORDER BY priority, zh_norm, en_norm, category, source_file, source_id
                """
            ).fetchall()
        return [row_to_entry(row) for row in rows]

    def metadata(self) -> dict[str, str]:
        """Build-recorded key/value metadata (source repo, pinned commit, ...).

        Read-only; backs the /about diagnostics command.
        """
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM metadata").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def term_count(self) -> int:
        """Total number of dictionary term rows."""
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM terms").fetchone()
        return int(row["n"])

    def review_snapshot(self) -> ReviewSnapshot:
        """Read review metadata and terms from one SQLite snapshot.

        The explicit transaction keeps the metadata, entries, and derived count
        on one database view even if a database writer commits between queries.
        """
        with connect(self.db_path) as conn:
            conn.execute("BEGIN")
            metadata_rows = conn.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
            term_rows = conn.execute(
                """
                SELECT * FROM terms
                ORDER BY priority, zh_norm, en_norm, category, source_file, source_id
                """
            ).fetchall()
        return ReviewSnapshot(
            metadata={row["key"]: row["value"] for row in metadata_rows},
            entries=tuple(row_to_entry(row) for row in term_rows),
        )

    def _exact(self, query: str) -> list[LookupCandidate]:
        # One normalization serves both sides: the query is matched against
        # zh_norm and en_norm with the same normalized form.
        norm = normalize_text(query)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM terms
                WHERE zh_norm = ? OR en_norm = ?
                ORDER BY priority, length(zh), category, source_file, source_id
                """,
                (norm, norm),
            ).fetchall()
        candidates = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            entry = row_to_entry(row)
            key = (entry.zh, entry.en, entry.category)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(LookupCandidate(entry=entry, score=100.0, reason="exact"))
        candidates.sort(
            key=lambda c: (
                CATEGORY_ORDER.get(c.entry.category, 999),
                self._source_priority(c.entry),
                len(c.entry.zh),
                c.entry.en,
                c.entry.source_id,
            )
        )
        return candidates

    def _fuzzy(self, query: str, limit: int) -> list[LookupCandidate]:
        q_norm = normalize_text(query)
        q_ascii = normalize_ascii(query)
        scored: list[LookupCandidate] = []
        for entry in self.entries():
            score, reason = self._score(entry, q_norm, q_ascii)
            if score <= 0:
                continue
            scored.append(LookupCandidate(entry=entry, score=score, reason=reason))
        scored.sort(
            key=lambda c: (
                -c.score,
                CATEGORY_ORDER.get(c.entry.category, 999),
                self._source_priority(c.entry),
                len(c.entry.zh),
                c.entry.en,
                c.entry.source_id,
            )
        )
        deduped: list[LookupCandidate] = []
        seen: set[tuple[str, str]] = set()
        for candidate in scored:
            key = (candidate.entry.zh, candidate.entry.en)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
            if len(deduped) >= limit:
                break
        return deduped

    @staticmethod
    def _score(entry: TermEntry, q_norm: str, q_ascii: str) -> tuple[float, str]:
        if q_ascii:
            if entry.pinyin == q_ascii:
                return 100.0, "pinyin"
            if entry.pinyin.startswith(q_ascii):
                return max(80.0, 96.0 - (len(entry.pinyin) - len(q_ascii)) * 0.5), "pinyin-prefix"
            if entry.pinyin_abbrev == q_ascii:
                return 92.0, "pinyin-abbrev"
            if q_ascii in entry.pinyin:
                return 86.0, "pinyin-substring"
        zh_ratio = SequenceMatcher(None, q_norm, normalize_text(entry.zh)).ratio() * 80.0
        en_ratio = SequenceMatcher(None, q_norm, normalize_text(entry.en)).ratio() * 70.0
        py_ratio = SequenceMatcher(None, q_ascii, entry.pinyin).ratio() * 82.0 if q_ascii else 0.0
        score = max(zh_ratio, en_ratio, py_ratio)
        if score < 45.0:
            return 0.0, "low-score"
        return score, "fuzzy"

    @staticmethod
    def _source_priority(entry: TermEntry) -> int:
        if entry.source_id.startswith("OccupationConfig_") and entry.source_id.endswith("_Name"):
            return 0
        if "RoleInfo" in entry.source_file:
            return 1
        if entry.category == "speaker":
            return 8
        return 5
