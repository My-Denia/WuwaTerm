"""Dictionary-first term lookup, unified immutable snapshots, and fuzzy matching."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from types import MappingProxyType

from .constants import (
    CATEGORY_ORDER,
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
)
from .db import row_to_entry
from .exclusion_metadata import (
    parse_free_text_lock_exclusions,
    parse_free_text_zh_lock_exclusions,
)
from .models import LookupCandidate, LookupResult, TermEntry
from .normalize import normalize_ascii, normalize_text
from .skill_references import SkillReference, skill_references
from .term_classification import (
    derived_context_gated_sources,
    derived_reference_only_sources as derived_reference_only_sources,
    is_derived_name_entry as is_derived_name_entry,
    select_preferred_exact_entry,
)
from .term_matching import (
    ReferenceCandidates,
    TermSpanMatcher,
    admissible_nfkc_sources,
    case_variant_sources,
    exact_lockable_source_records,
    is_eligible_lockable,
    model_reference_candidates,
)

LOGGER = logging.getLogger(__name__)


def _source_priority(entry: TermEntry) -> int:
    if entry.source_id.startswith("OccupationConfig_") and entry.source_id.endswith(
        "_Name"
    ):
        return 0
    if "RoleInfo" in entry.source_file:
        return 1
    if entry.category == "speaker":
        return 8
    return 5


def preferred_exact_entry(service: TermService, query: str) -> TermEntry | None:
    """Return the exact hit a fixed substitution may restore, if any.

    An established row wins over a derived one, so a name the builder derived
    never displaces the meaning the dictionary already paired. Remaining
    candidates stay available to dictionary lookup; only deterministic
    whole-input translation picks one.
    """
    result = service.lookup_exact(query, limit=sys.maxsize)
    if not result.exact or result.best is None:
        return None
    return select_preferred_exact_entry(result.candidates)


def free_text_exclusions(
    metadata: Mapping[str, str],
    *,
    logger: logging.Logger | None = None,
) -> frozenset[str]:
    """Return the union of both exclusion keys, failing open per key.

    The English and Chinese metadata keys are read independently: a missing
    key is the normal state of a database built without the official corpus
    or before that key existed, and an invalid value is logged with one
    warning naming that key and excludes nothing from it, while the other
    key still applies.
    """
    active_logger = LOGGER if logger is None else logger
    surfaces: set[str] = set()
    for key, parser in (
        (FREE_TEXT_LOCK_EXCLUSIONS_KEY, parse_free_text_lock_exclusions),
        (FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY, parse_free_text_zh_lock_exclusions),
    ):
        value = metadata.get(key)
        if value is None:
            continue
        try:
            rows = parser(value)
        except (TypeError, ValueError):
            active_logger.warning(
                "ignoring invalid %s metadata; every dictionary surface stays lockable",
                key,
            )
            continue
        surfaces.update(surface for surface, _occurrences, _aligned in rows)
    return frozenset(surfaces)


@dataclass(frozen=True)
class ReviewSnapshot:
    """One transaction's complete dictionary basis for pair review."""

    metadata: dict[str, str]
    entries: tuple[TermEntry, ...]
    revision: str | None = None

    @property
    def term_count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class DictionaryIdentity:
    """Authoritative cryptographic dictionary identity and matching provenance.

    Deliberately excludes filesystem paths, credentials and chat identifiers.
    """

    revision: str
    schema_version: str | None
    source_commit: str | None
    source_game_version: str | None
    term_count: int


def dictionary_revision(entries: Sequence[TermEntry]) -> str:
    """Return the deterministic SHA-256 identity of the dictionary entries."""
    rows = sorted(
        (
            entry.category,
            entry.source_file,
            entry.source_id,
            entry.text_key,
            entry.zh,
            entry.en,
            entry.pinyin,
            entry.pinyin_abbrev,
        )
        for entry in entries
    )
    payload = json.dumps(
        ("wuwaterm-review-dictionary-v1", rows),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class _ScoringRow:
    """One dictionary row with its normalized forms precomputed.

    Fuzzy scoring used to normalize both sides of every row on every query;
    at tens of thousands of rows the repeated NFKC/casefold work dominates.
    The rows live on the immutable :class:`DictionarySnapshot`, so the
    normalization cost is paid once per dictionary revision, not once per query.
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


@dataclass(frozen=True)
class LockableSnapshot:
    """Immutable term-locking, reference, and Aho-Corasick bundle for one revision."""

    rows: tuple[tuple[str, tuple[str, str, str]], ...]
    eligible_lockable: tuple[tuple[str, tuple[str, str]], ...]
    source_categories: Mapping[str, str]
    nfkc_spelling_sources: frozenset[str]
    reference_only_sources: frozenset[str]
    context_gated_reference_only_sources: frozenset[str]
    reference_candidates: ReferenceCandidates
    skill_references: tuple[SkillReference, ...]
    matcher: TermSpanMatcher
    soft_sources: frozenset[str]
    extended_lockable: tuple[tuple[str, tuple[str, str]], ...]
    _extended_matcher_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    _extended_matcher_cache: list[TermSpanMatcher] = field(
        default_factory=list, repr=False, compare=False
    )

    def as_source_tuple(
        self,
    ) -> tuple[
        tuple[tuple[str, tuple[str, str, str]], ...],
        frozenset[str],
        frozenset[str],
        frozenset[str],
        ReferenceCandidates,
        tuple[SkillReference, ...],
    ]:
        return (
            self.rows,
            self.nfkc_spelling_sources,
            self.reference_only_sources,
            self.context_gated_reference_only_sources,
            self.reference_candidates,
            self.skill_references,
        )

    def extended_matcher(self) -> TermSpanMatcher:
        """Lazily build and cache the Aho-Corasick matcher including soft sources."""
        if not self.soft_sources:
            return self.matcher
        if self._extended_matcher_cache:
            return self._extended_matcher_cache[0]
        with self._extended_matcher_lock:
            if not self._extended_matcher_cache:
                self._extended_matcher_cache.append(
                    TermSpanMatcher.build(
                        self.extended_lockable,
                        nfkc_spelling_sources=self.nfkc_spelling_sources,
                        include_plural=True,
                    )
                )
            return self._extended_matcher_cache[0]


def build_lockable_snapshot(
    metadata: Mapping[str, str],
    entries: Sequence[TermEntry],
    *,
    logger: logging.Logger | None = None,
) -> LockableSnapshot:
    """Build an immutable :class:`LockableSnapshot` from one dictionary view."""
    excluded = free_text_exclusions(metadata, logger=logger)
    pre_exclusion_sources = exact_lockable_source_records(entries)
    sources = dict(pre_exclusion_sources)
    # Case variants come after every exact source, so an exact surface
    # always keeps its own record and wins equal-length overlaps. They are
    # computed before exclusions are removed, so an excluded surface still
    # blocks a variant equal to it and still counts for casefold ambiguity.
    variants = case_variant_sources(pre_exclusion_sources, excluded)
    # Same pre-exclusion snapshot: a spelling collision or exclusion still
    # counts even when one of the colliding surfaces is later removed.
    nfkc_sources = admissible_nfkc_sources(
        pre_exclusion_sources, variants, excluded
    )
    for surface in excluded:
        sources.pop(surface, None)
    for variant, official in variants:
        sources.setdefault(variant, official)

    rows = tuple(sources.items())
    eligible_lockable = tuple(
        (source, (official[0], official[1]))
        for source, official in rows
        if is_eligible_lockable(source, official)
    )
    source_categories = MappingProxyType(
        {source: official[2] for source, official in rows}
    )
    reference_only = derived_reference_only_sources(entries)
    context_gated = derived_context_gated_sources(entries)
    candidates = model_reference_candidates(
        entries,
        sources,
        excluded,
        pre_exclusion_sources=pre_exclusion_sources,
    )
    skills = skill_references(entries, set(pre_exclusion_sources), excluded)
    matcher = TermSpanMatcher.build(
        eligible_lockable,
        nfkc_spelling_sources=nfkc_sources,
        include_plural=True,
    )
    eligible_keys = {source for source, _official in eligible_lockable}
    soft_sources = frozenset(candidates.keys() - eligible_keys) if candidates else frozenset()
    if soft_sources:
        extended_lockable = eligible_lockable + tuple(
            (source, (choices[0][0], choices[0][1]))
            for source, choices in candidates.items()
            if source in soft_sources and choices
        )
    else:
        extended_lockable = eligible_lockable
    return LockableSnapshot(
        rows=rows,
        eligible_lockable=eligible_lockable,
        source_categories=source_categories,
        nfkc_spelling_sources=nfkc_sources,
        reference_only_sources=reference_only,
        context_gated_reference_only_sources=context_gated,
        reference_candidates=candidates,
        skill_references=skills,
        matcher=matcher,
        soft_sources=soft_sources,
        extended_lockable=extended_lockable,
    )


def _build_scoring_rows(entries: tuple[TermEntry, ...]) -> tuple[_ScoringRow, ...]:
    rows: list[_ScoringRow] = []
    for entry in entries:
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
    return tuple(rows)


class DictionarySnapshot:
    """Unified immutable snapshot for a single committed dictionary revision.

    All derived views — cryptographic :class:`DictionaryIdentity`,
    :class:`ReviewSnapshot`, normalized fuzzy-scoring rows, and
    :class:`LockableSnapshot` (including the compiled Aho-Corasick
    :class:`TermSpanMatcher`) — are derived deterministically from this single
    atomic SQLite read and cached on the snapshot instance. In-flight requests
    holding a snapshot reference stay isolated while newer requests advance to
    a fresh snapshot on WAL commit or atomic file replacement.
    """

    __slots__ = (
        "_storage_stamp",
        "_metadata",
        "_entries",
        "_derived_lock",
        "_dictionary_identity",
        "_scoring_rows",
        "_lockable_snapshot",
    )

    def __init__(
        self,
        *,
        storage_stamp: tuple[object, ...],
        metadata: Mapping[str, str],
        entries: tuple[TermEntry, ...],
    ) -> None:
        self._storage_stamp = storage_stamp
        self._metadata: Mapping[str, str] = MappingProxyType(dict(metadata))
        self._entries = entries
        self._derived_lock = threading.Lock()
        self._dictionary_identity: DictionaryIdentity | None = None
        self._scoring_rows: tuple[_ScoringRow, ...] | None = None
        self._lockable_snapshot: LockableSnapshot | None = None

    @property
    def storage_stamp(self) -> tuple[object, ...]:
        return self._storage_stamp

    @property
    def metadata(self) -> Mapping[str, str]:
        return self._metadata

    @property
    def entries(self) -> tuple[TermEntry, ...]:
        return self._entries

    @property
    def term_count(self) -> int:
        return len(self._entries)

    @property
    def dictionary_identity(self) -> DictionaryIdentity:
        identity = self._dictionary_identity
        if identity is not None:
            return identity
        with self._derived_lock:
            if self._dictionary_identity is None:
                self._dictionary_identity = DictionaryIdentity(
                    revision=dictionary_revision(self._entries),
                    schema_version=self._metadata.get("schema_version"),
                    source_commit=self._metadata.get("source_commit"),
                    source_game_version=self._metadata.get("source_game_version"),
                    term_count=len(self._entries),
                )
            return self._dictionary_identity

    @property
    def review_snapshot(self) -> ReviewSnapshot:
        """Return a ReviewSnapshot with an isolated copy of metadata."""
        identity = self.dictionary_identity
        return ReviewSnapshot(
            metadata=dict(self._metadata),
            entries=self._entries,
            revision=identity.revision,
        )

    @property
    def scoring_rows(self) -> tuple[_ScoringRow, ...]:
        rows = self._scoring_rows
        if rows is not None:
            return rows
        with self._derived_lock:
            if self._scoring_rows is None:
                self._scoring_rows = _build_scoring_rows(self._entries)
            return self._scoring_rows

    def lockable_snapshot(
        self, *, logger: logging.Logger | None = None
    ) -> LockableSnapshot:
        bundle = self._lockable_snapshot
        if bundle is not None:
            return bundle
        with self._derived_lock:
            if self._lockable_snapshot is None:
                self._lockable_snapshot = build_lockable_snapshot(
                    self._metadata, self._entries, logger=logger
                )
            return self._lockable_snapshot

    def warm(self, *, logger: logging.Logger | None = None) -> DictionarySnapshot:
        """Materialize all derived indices and matchers on this snapshot."""
        _ = self.dictionary_identity
        _ = self.review_snapshot
        _ = self.scoring_rows
        _ = self.lockable_snapshot(logger=logger)
        return self


def _stat_tuple(path: Path) -> tuple[int, int, int, int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _optional_stat_tuple(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _is_coarse_timestamp(stat: tuple[int, int, int, int, int]) -> bool:
    """Return True when file mtime/ctime has whole-second (coarse) granularity."""
    return (stat[3] % 1_000_000_000 == 0) or (stat[4] % 1_000_000_000 == 0)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a ``?mode=ro`` SQLite connection.

    Reads the main database file without writing to it (in WAL mode SQLite may
    still open or initialize the ``-wal``/``-shm`` sidecar files when reading).
    """
    resolved = path.resolve(strict=False)
    conn = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _read_validation_probe(
    path: Path,
) -> tuple[tuple[tuple[str, str], ...], int, int]:
    """Read metadata provenance and terms (count, max(id)) in one ``?mode=ro`` snapshot."""
    conn = _connect_readonly(path)
    try:
        conn.execute("BEGIN DEFERRED")
        try:
            metadata_rows = conn.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
            stats_row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM terms"
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    provenance = tuple((row["key"], row["value"]) for row in metadata_rows)
    count = int(stats_row[0]) if stats_row is not None else 0
    max_id = int(stats_row[1]) if stats_row is not None else 0
    return provenance, count, max_id


class TermService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._snapshot_stamp: tuple[object, ...] | None = None
        self._snapshot_cache: DictionarySnapshot | None = None
        self._version_conn: sqlite3.Connection | None = None
        self._version_inode: tuple[int, int] | None = None
        self._version_main_stat: tuple[int, int, int, int, int] | None = None
        self._version_data_version: int | None = None

    def _close_version_conn(self) -> None:
        conn = self._version_conn
        self._version_conn = None
        self._version_inode = None
        self._version_main_stat = None
        self._version_data_version = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        """Release the cached snapshot and any idle DELETE-mode version handle."""
        with self._lock:
            self._snapshot_stamp = None
            self._snapshot_cache = None
            self._close_version_conn()

    def __del__(self) -> None:
        conn = getattr(self, "_version_conn", None)
        if conn is not None:
            self._version_conn = None
            try:
                conn.close()
            except Exception:
                pass

    def _sidecar_stats(
        self, path: Path
    ) -> tuple[
        tuple[int, int, int, int, int] | None,
        tuple[int, int, int, int, int] | None,
        tuple[int, int, int, int, int] | None,
        tuple[int, int, int, int, int] | None,
        bool,
    ]:
        wal_resolved = Path(f"{path}-wal")
        shm_resolved = Path(f"{path}-shm")
        wal_direct = Path(f"{self.db_path}-wal")
        shm_direct = Path(f"{self.db_path}-shm")
        wal_stat = _optional_stat_tuple(wal_resolved)
        shm_stat = _optional_stat_tuple(shm_resolved)
        wal_direct_stat = (
            _optional_stat_tuple(wal_direct) if wal_direct != wal_resolved else None
        )
        shm_direct_stat = (
            _optional_stat_tuple(shm_direct) if shm_direct != shm_resolved else None
        )
        has_wal_or_shm = (
            wal_stat is not None
            or shm_stat is not None
            or wal_direct_stat is not None
            or shm_direct_stat is not None
        )
        return wal_stat, shm_stat, wal_direct_stat, shm_direct_stat, has_wal_or_shm

    def _identity(self) -> tuple[object, ...]:
        """Storage identity for cache validation, matching provenance + WAL awareness."""
        with self._lock:
            path = self.db_path.resolve(strict=False)
            main_stat = _stat_tuple(path)
            wal_stat, shm_stat, wal_direct_stat, shm_direct_stat, has_wal_or_shm = (
                self._sidecar_stats(path)
            )
            if has_wal_or_shm:
                self._close_version_conn()
            dv: int | None = None
            if (
                not has_wal_or_shm
                and self._version_conn is not None
                and self._version_inode == (main_stat[0], main_stat[1])
            ):
                try:
                    dv_row = self._version_conn.execute(
                        "PRAGMA data_version"
                    ).fetchone()
                    dv = int(dv_row[0]) if dv_row is not None else None
                except sqlite3.Error:
                    self._close_version_conn()
            probe = _read_validation_probe(path)
            return (
                str(path),
                main_stat,
                wal_stat,
                shm_stat,
                wal_direct_stat,
                shm_direct_stat,
                dv,
                probe,
            )

    def _read_snapshot_on_conn(
        self, conn: sqlite3.Connection, stamp: tuple[object, ...]
    ) -> tuple[DictionarySnapshot, str, int, tuple[tuple[tuple[str, str], ...], int, int]]:
        """Read journal_mode, metadata, terms, and data_version in one read transaction."""
        conn.execute("BEGIN DEFERRED")
        try:
            mode_row = conn.execute("PRAGMA journal_mode").fetchone()
            journal_mode = (
                str(mode_row[0]).lower() if mode_row is not None else "delete"
            )
            metadata_rows = conn.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
            term_rows = conn.execute(
                """
                SELECT * FROM terms
                ORDER BY priority, zh_norm, en_norm, category, source_file, source_id
                """
            ).fetchall()
            dv_row = conn.execute("PRAGMA data_version").fetchone()
            data_version = int(dv_row[0]) if dv_row is not None else 0
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        provenance = tuple((row["key"], row["value"]) for row in metadata_rows)
        max_id = max((int(row["id"]) for row in term_rows), default=0)
        probe = (provenance, len(term_rows), max_id)
        metadata = {row["key"]: row["value"] for row in metadata_rows}
        entries = tuple(row_to_entry(row) for row in term_rows)
        return (
            DictionarySnapshot(
                storage_stamp=stamp,
                metadata=metadata,
                entries=entries,
            ),
            journal_mode,
            data_version,
            probe,
        )

    def _read_snapshot_ephemeral(
        self, path: Path, stamp: tuple[object, ...]
    ) -> DictionarySnapshot:
        conn = _connect_readonly(path)
        try:
            snapshot, _mode, _dv, _probe = self._read_snapshot_on_conn(conn, stamp)
            return snapshot
        finally:
            conn.close()

    def snapshot(self) -> DictionarySnapshot:
        """Return the current immutable :class:`DictionarySnapshot`.

        In DELETE mode (no ``-wal``/``-shm`` sidecars and sub-second file
        timestamps), keeps an idle ``?mode=ro`` handle to validate cache hits
        via ``PRAGMA data_version``, catching even same-subsecond-tick
        equal-length in-place ``UPDATE`` statements without holding any file
        locks. Whenever ``-wal`` or ``-shm`` exists (or timestamps are coarse
        whole-second), any idle handle is closed immediately and reads bypass
        caching through a short-lived ``?mode=ro`` connection.
        """
        with self._lock:
            snapshot: DictionarySnapshot | None = None
            for _attempt in range(2):
                path = self.db_path.resolve(strict=False)
                main_stat = _stat_tuple(path)
                (
                    wal_stat,
                    shm_stat,
                    wal_direct_stat,
                    shm_direct_stat,
                    has_wal_or_shm,
                ) = self._sidecar_stats(path)

                if has_wal_or_shm or _is_coarse_timestamp(main_stat):
                    self._close_version_conn()
                    self._snapshot_cache = None
                    self._snapshot_stamp = None
                    ephemeral_stamp: tuple[object, ...] = (
                        str(path),
                        main_stat,
                        wal_stat,
                        shm_stat,
                        wal_direct_stat,
                        shm_direct_stat,
                    )
                    return self._read_snapshot_ephemeral(path, ephemeral_stamp)

                inode = (main_stat[0], main_stat[1])
                if self._version_conn is not None:
                    if self._version_inode != inode:
                        self._close_version_conn()
                    else:
                        try:
                            dv_row = self._version_conn.execute(
                                "PRAGMA data_version"
                            ).fetchone()
                        except sqlite3.Error:
                            self._close_version_conn()
                        else:
                            dv = int(dv_row[0]) if dv_row is not None else -1
                            if (
                                dv == self._version_data_version
                                and main_stat == self._version_main_stat
                                and self._snapshot_cache is not None
                            ):
                                return self._snapshot_cache
                elif (
                    self._snapshot_cache is not None
                    and self._snapshot_stamp is not None
                ):
                    self._snapshot_cache = None
                    self._snapshot_stamp = None

                reused_conn = (
                    self._version_conn is not None
                    and self._version_inode == inode
                )
                conn = (
                    self._version_conn
                    if reused_conn and self._version_conn is not None
                    else _connect_readonly(path)
                )
                stamp_seed: tuple[object, ...] = (str(path), main_stat)
                try:
                    snapshot, journal_mode, dv, _probe = self._read_snapshot_on_conn(
                        conn, stamp_seed
                    )
                except sqlite3.Error:
                    if reused_conn:
                        self._close_version_conn()
                        conn = _connect_readonly(path)
                        reused_conn = False
                        try:
                            snapshot, journal_mode, dv, _probe = (
                                self._read_snapshot_on_conn(conn, stamp_seed)
                            )
                        except BaseException:
                            conn.close()
                            raise
                    else:
                        conn.close()
                        raise
                except BaseException:
                    if reused_conn:
                        self._close_version_conn()
                    else:
                        conn.close()
                    raise

                main_stat_after = _stat_tuple(path)
                (
                    _wal_a,
                    _shm_a,
                    _wal_da,
                    _shm_da,
                    has_wal_or_shm_after,
                ) = self._sidecar_stats(path)

                if (
                    journal_mode == "delete"
                    and not has_wal_or_shm_after
                    and not _is_coarse_timestamp(main_stat_after)
                    and main_stat_after == main_stat
                ):
                    try:
                        dv_after_row = conn.execute(
                            "PRAGMA data_version"
                        ).fetchone()
                        dv_after = (
                            int(dv_after_row[0]) if dv_after_row is not None else -1
                        )
                    except sqlite3.Error:
                        if reused_conn:
                            self._close_version_conn()
                        else:
                            conn.close()
                        continue
                    if dv_after == dv:
                        self._version_conn = conn
                        self._version_inode = inode
                        self._version_main_stat = main_stat_after
                        self._version_data_version = dv
                        self._snapshot_stamp = (str(path), main_stat_after, "delete", dv)
                        self._snapshot_cache = snapshot
                        return snapshot
                    if not reused_conn:
                        conn.close()
                    continue

                # Non-DELETE mode (e.g. WAL) or sidecars appeared: NEVER retain handle
                # and never cache so WAL mode stays 100% fresh even across checkpoints.
                if reused_conn:
                    self._close_version_conn()
                else:
                    conn.close()
                self._snapshot_cache = None
                self._snapshot_stamp = None
                if (
                    journal_mode != "delete"
                    or has_wal_or_shm_after
                    or _is_coarse_timestamp(main_stat_after)
                ):
                    return snapshot

            assert snapshot is not None
            return snapshot

    def lookup(self, query: str, limit: int = 5) -> LookupResult:
        query = query.strip()
        if not query:
            return LookupResult(query=query, exact=False, candidates=())

        exact = self._exact(query)
        if exact:
            return LookupResult(
                query=query, exact=True, candidates=tuple(exact[:limit])
            )
        return LookupResult(
            query=query, exact=False, candidates=tuple(self._fuzzy(query, limit))
        )

    def lookup_exact(self, query: str, limit: int = 5) -> LookupResult:
        query = query.strip()
        if not query:
            return LookupResult(query=query, exact=False, candidates=())

        exact = self._exact(query)
        return LookupResult(
            query=query, exact=bool(exact), candidates=tuple(exact[:limit])
        )

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
        return list(self.snapshot().entries)

    def metadata(self) -> dict[str, str]:
        """Build-recorded key/value metadata (source repo, pinned commit, ...).

        Read-only; backs the /about diagnostics command.
        """
        return dict(self.snapshot().metadata)

    def term_count(self) -> int:
        """Total number of dictionary term rows."""
        return self.snapshot().term_count

    def review_snapshot(self) -> ReviewSnapshot:
        """Read review metadata and terms from one cached SQLite snapshot.

        The explicit transaction keeps the metadata, entries, revision, and
        derived count on one database view even if a database writer commits
        between queries.
        """
        return self.snapshot().review_snapshot

    def dictionary_identity(self) -> DictionaryIdentity:
        """Return the cached cryptographic dictionary identity for the current snapshot."""
        return self.snapshot().dictionary_identity

    def _exact(self, query: str) -> list[LookupCandidate]:
        # One normalization serves both sides: the query is matched against
        # zh_norm and en_norm with the same normalized form via SQLite's
        # indexed query (short-lived ?mode=ro connection), preserving exact
        # tie-breaking across both ANALYZE and non-ANALYZE query plans.
        norm = normalize_text(query)
        conn = _connect_readonly(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT * FROM terms
                WHERE zh_norm = ? OR en_norm = ?
                ORDER BY priority, length(zh), category, source_file, source_id
                """,
                (norm, norm),
            ).fetchall()
        finally:
            conn.close()
        candidates: list[LookupCandidate] = []
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

    def _scoring_rows(self) -> tuple[_ScoringRow, ...]:
        """Snapshot-cached rows with normalized forms, paid once per dictionary."""
        return self.snapshot().scoring_rows

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
        return _source_priority(entry)
