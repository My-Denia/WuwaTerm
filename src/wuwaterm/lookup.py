"""Dictionary-first term lookup and fuzzy matching."""

from __future__ import annotations

from collections import Counter
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


@dataclass(frozen=True)
class _ScoringRow:
    """One dictionary row with its normalized forms precomputed.

    Fuzzy scoring used to normalize both sides of every row on every query;
    at tens of thousands of rows the repeated NFKC/casefold work dominates.
    The snapshot is cached on file identity (see TermService._identity), so
    the normalization cost is paid once per dictionary, not once per query.
    """

    entry: TermEntry
    zh_norm: str
    en_norm: str
    zh_chars: frozenset[str]
    en_chars: frozenset[str]
    pinyin_chars: frozenset[str]
    zh_counter: Counter[str]
    en_counter: Counter[str]
    pinyin_counter: Counter[str]


@dataclass(frozen=True)
class _FuzzyQuery:
    """Per-query precomputations for fuzzy scoring."""

    q_norm: str
    q_ascii: str
    q_norm_chars: frozenset[str]
    q_ascii_chars: frozenset[str]
    q_norm_counter: Counter[str]
    q_ascii_counter: Counter[str]
    # Best possible SequenceMatcher ratio given character multisets alone
    # (2 * sum(min-counts) / total length). A side whose upper bound cannot
    # reach the publish floor of 45.0 can never be the published max: either
    # another side reaches the floor (max unaffected) or none does (the row
    # is rejected as low-score either way).
    floor: float = 45.0


class TermService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._scoring_rows_key: tuple[object, ...] | None = None
        self._scoring_rows_cache: tuple[_ScoringRow, ...] = ()

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

    def _identity(self) -> tuple[object, ...]:
        """Filesystem identity plus build provenance for cache validation.

        Mirrors SentenceTranslator._lockable_sources_identity: a promoted
        candidate database is a new file, so dev/ino/size/mtime detect the
        replacement; the metadata guards the corner case of two files with
        coincidentally equal stats.
        """
        path = self.db_path.resolve(strict=False)
        stat = path.stat()
        provenance = tuple(sorted(self.metadata().items()))
        return (
            str(path),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            provenance,
        )

    def _scoring_rows(self) -> tuple[_ScoringRow, ...]:
        """Identity-cached rows with normalized forms, paid once per dictionary."""
        for _attempt in range(2):
            identity_before = self._identity()
            if identity_before == self._scoring_rows_key:
                return self._scoring_rows_cache
            rows = []
            for entry in self.entries():
                zh_norm = normalize_text(entry.zh)
                en_norm = normalize_text(entry.en)
                rows.append(
                    _ScoringRow(
                        entry=entry,
                        zh_norm=zh_norm,
                        en_norm=en_norm,
                        zh_chars=frozenset(zh_norm),
                        en_chars=frozenset(en_norm),
                        pinyin_chars=frozenset(entry.pinyin),
                        zh_counter=Counter(zh_norm),
                        en_counter=Counter(en_norm),
                        pinyin_counter=Counter(entry.pinyin),
                    )
                )
            snapshot = tuple(rows)
            identity_after = self._identity()
            if identity_before == identity_after:
                self._scoring_rows_key = identity_after
                self._scoring_rows_cache = snapshot
                return snapshot
        # A dictionary that changed twice while being read is not safe to
        # cache: serve the fresh snapshot and retry caching next query.
        return tuple(rows)

    def _fuzzy(self, query: str, limit: int) -> list[LookupCandidate]:
        q_norm = normalize_text(query)
        q_ascii = normalize_ascii(query)
        fuzzy_query = _FuzzyQuery(
            q_norm=q_norm,
            q_ascii=q_ascii,
            q_norm_chars=frozenset(q_norm),
            q_ascii_chars=frozenset(q_ascii),
            q_norm_counter=Counter(q_norm),
            q_ascii_counter=Counter(q_ascii),
        )
        scored: list[LookupCandidate] = []
        for row in self._scoring_rows():
            score, reason = self._score(row, fuzzy_query)
            if score <= 0:
                continue
            scored.append(
                LookupCandidate(entry=row.entry, score=score, reason=reason)
            )
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
    def _bounded_ratio(
        numerator_counter: Counter[str],
        denominator_counter: Counter[str],
        left_len: int,
        right_len: int,
    ) -> float:
        """Upper bound for 2*M/T over shared character multisets.

        SequenceMatcher's match count M can never exceed the sum of per-char
        minima, so this bounds ratio() without running it.
        """
        total = left_len + right_len
        if total == 0:
            return 0.0
        matched = sum(
            min(count, denominator_counter.get(char, 0))
            for char, count in numerator_counter.items()
        )
        return 2.0 * matched / total

    @classmethod
    def _score(cls, row: _ScoringRow, query: _FuzzyQuery) -> tuple[float, str]:
        entry = row.entry
        q_norm = query.q_norm
        q_ascii = query.q_ascii
        if q_ascii:
            # normalize_ascii strips to [0-9A-Za-z] and casefolds, the same
            # shape pinyin columns are stored in, so raw comparison and
            # character sets are sound on this side.
            if entry.pinyin == q_ascii:
                return 100.0, "pinyin"
            if entry.pinyin.startswith(q_ascii):
                return max(80.0, 96.0 - (len(entry.pinyin) - len(q_ascii)) * 0.5), "pinyin-prefix"
            if entry.pinyin_abbrev == q_ascii:
                return 92.0, "pinyin-abbrev"
            if q_ascii in entry.pinyin:
                return 86.0, "pinyin-substring"
        # A ratio is 0 when the sides share no characters, and cannot reach
        # the publish floor when the multiset upper bound says so; both gates
        # skip SequenceMatcher, which dominates the per-row cost.
        if not query.q_norm_chars & row.zh_chars or (
            cls._bounded_ratio(
                query.q_norm_counter,
                row.zh_counter,
                len(q_norm),
                len(row.zh_norm),
            )
            * 80.0
            < query.floor
        ):
            zh_ratio = 0.0
        else:
            zh_ratio = SequenceMatcher(None, q_norm, row.zh_norm).ratio() * 80.0
        if not query.q_norm_chars & row.en_chars or (
            cls._bounded_ratio(
                query.q_norm_counter,
                row.en_counter,
                len(q_norm),
                len(row.en_norm),
            )
            * 70.0
            < query.floor
        ):
            en_ratio = 0.0
        else:
            en_ratio = SequenceMatcher(None, q_norm, row.en_norm).ratio() * 70.0
        if not q_ascii or not query.q_ascii_chars & row.pinyin_chars:
            py_ratio = 0.0
        elif (
            cls._bounded_ratio(
                query.q_ascii_counter,
                row.pinyin_counter,
                len(q_ascii),
                len(entry.pinyin),
            )
            * 82.0
            < query.floor
        ):
            py_ratio = 0.0
        else:
            py_ratio = SequenceMatcher(None, q_ascii, entry.pinyin).ratio() * 82.0
        score = max(zh_ratio, en_ratio, py_ratio)
        if score < query.floor:
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
