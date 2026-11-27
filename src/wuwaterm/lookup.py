"""Dictionary-first term lookup, unified immutable snapshots, and fuzzy matching."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import sys
import threading
import weakref
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


def _build_exact_index(
    entries: tuple[TermEntry, ...],
    row_norms: tuple[tuple[str, str, int], ...],
) -> Mapping[str, tuple[LookupCandidate, ...]]:
    # Each bucket item carries (priority, len(zh), category, source_file,
    # source_id, match_rank, id, entry) where match_rank=0 for idx_terms_zh_norm
    # hits and match_rank=1 for idx_terms_en_norm-only hits, mirroring SQLite's
    # MULTI-INDEX OR scan order before ORDER BY.
    buckets: dict[
        str, list[tuple[int, int, str, str, str, int, int, TermEntry]]
    ] = {}
    for entry, (zh_norm, en_norm, priority) in zip(entries, row_norms, strict=True):
        buckets.setdefault(zh_norm, []).append(
            (
                priority,
                len(entry.zh),
                entry.category,
                entry.source_file,
                entry.source_id,
                0,
                entry.id,
                entry,
            )
        )
        if en_norm != zh_norm:
            buckets.setdefault(en_norm, []).append(
                (
                    priority,
                    len(entry.zh),
                    entry.category,
                    entry.source_file,
                    entry.source_id,
                    1,
                    entry.id,
                    entry,
                )
            )

    index: dict[str, tuple[LookupCandidate, ...]] = {}
    for norm, items in buckets.items():
        if len(items) > 1:
            items.sort(
                key=lambda row: (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                )
            )
        candidates: list[LookupCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        for (
            _prio,
            _zh_len,
            _cat,
            _src_file,
            _src_id,
            _match_rank,
            _entry_id,
            entry,
        ) in items:
            key = (entry.zh, entry.en, entry.category)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(LookupCandidate(entry=entry, score=100.0, reason="exact"))
        if len(candidates) > 1:
            candidates.sort(
                key=lambda c: (
                    CATEGORY_ORDER.get(c.entry.category, 999),
                    _source_priority(c.entry),
                    len(c.entry.zh),
                    c.entry.en,
                    c.entry.source_id,
                )
            )
        index[norm] = tuple(candidates)
    return MappingProxyType(index)


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
    :class:`ReviewSnapshot`, exact-lookup index, normalized fuzzy-scoring rows,
    and :class:`LockableSnapshot` (including the compiled Aho-Corasick
    :class:`TermSpanMatcher`) — are derived deterministically from this single
    atomic SQLite read and cached on the snapshot instance. In-flight requests
    holding a snapshot reference stay isolated while newer requests advance to
    a fresh snapshot on WAL commit or atomic file replacement.
    """

    __slots__ = (
        "_storage_stamp",
        "_metadata",
        "_entries",
        "_row_norms",
        "_derived_lock",
        "_dictionary_identity",
        "_exact_index",
        "_scoring_rows",
        "_lockable_snapshot",
    )

    def __init__(
        self,
        *,
        storage_stamp: tuple[object, ...],
        metadata: Mapping[str, str],
        entries: tuple[TermEntry, ...],
        row_norms: tuple[tuple[str, str, int], ...] | None = None,
    ) -> None:
        self._storage_stamp = storage_stamp
        self._metadata: Mapping[str, str] = MappingProxyType(dict(metadata))
        self._entries = entries
        if row_norms is None:
            self._row_norms = tuple(
                (
                    normalize_text(entry.zh),
                    normalize_text(entry.en),
                    CATEGORY_ORDER.get(entry.category, 999),
                )
                for entry in entries
            )
        else:
            self._row_norms = row_norms
        self._derived_lock = threading.Lock()
        self._dictionary_identity: DictionaryIdentity | None = None
        self._exact_index: Mapping[str, tuple[LookupCandidate, ...]] | None = None
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
    def exact_index(self) -> Mapping[str, tuple[LookupCandidate, ...]]:
        index = self._exact_index
        if index is not None:
            return index
        with self._derived_lock:
            if self._exact_index is None:
                self._exact_index = _build_exact_index(self._entries, self._row_norms)
            return self._exact_index

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
        _ = self.exact_index
        _ = self.scoring_rows
        _ = self.lockable_snapshot(logger=logger)
        return self


_SQLITE_WAL_INDEX_VERSION = 3007000
# SQLite WAL-index header (48 bytes, native byte order):
#   iVersion(u32), unused(u32), iChange(u32), isInit(u8), bigEndCksum(u8),
#   szPage(u16), mxFrame(u32), nPage(u32), aFrameCksum(8B), aSalt(8B), aCksum(8B)
_SHM_HEADER_STRUCT = struct.Struct("=IIIBBHII8s8s8s")


def _stat_tuple(path: Path) -> tuple[int, int, int, int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _optional_stat_tuple(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _close_fd_holder(holder: list[int | None]) -> None:
    for idx in range(len(holder)):
        fd = holder[idx]
        holder[idx] = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _parse_shm_wal_index(
    raw: bytes,
) -> tuple[tuple[object, ...] | None, bool]:
    """Parse the mirrored 48-byte SQLite WAL-index headers from ``-shm``.

    Returns ``(fingerprint, cacheable)``. When the two mirrored headers
    disagree (a writer is mid-commit updating ``-shm``) or ``isInit != 1``,
    ``cacheable`` is ``False`` so callers never pin a snapshot to a transient
    WAL visibility window.
    """
    if len(raw) < 96:
        return None, False
    hdr1 = raw[:48]
    hdr2 = raw[48:96]
    if hdr1 != hdr2:
        return None, False
    (
        i_version,
        _unused,
        i_change,
        is_init,
        _big_end_cksum,
        sz_page,
        mx_frame,
        n_page,
        a_frame_cksum,
        a_salt,
        a_cksum,
    ) = _SHM_HEADER_STRUCT.unpack(hdr1)
    if i_version != _SQLITE_WAL_INDEX_VERSION or is_init != 1:
        return None, False
    return (
        i_version,
        i_change,
        is_init,
        sz_page,
        mx_frame,
        n_page,
        a_frame_cksum,
        a_salt,
        a_cksum,
    ), True


def _connect_for_snapshot(path: Path) -> sqlite3.Connection:
    """Open a short-lived SQLite connection for reading a snapshot.

    Uses a standard read-write connection when the database file and its parent
    directory are writable so that closing the last connection on a WAL
    database triggers SQLite's normal automatic checkpoint and ``-wal``/``-shm``
    cleanup (matching ``main``). Falls back to ``?mode=ro`` for read-only files
    or mounts.
    """
    resolved = path.resolve(strict=False)
    conn: sqlite3.Connection | None = None
    if os.access(resolved, os.W_OK) and os.access(resolved.parent, os.W_OK):
        try:
            conn = sqlite3.connect(resolved, isolation_level=None)
        except sqlite3.OperationalError:
            conn = None
    if conn is None:
        conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
    conn.row_factory = sqlite3.Row
    return conn


class TermService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        # Raw O_RDONLY file descriptors for pread() header checks only (index 0:
        # main DB header bytes 24..44; index 1: optional -shm WAL-index header
        # bytes 0..96). Keeping them open per inode avoids open()+close() on
        # every read so in-process POSIX fcntl locks held by SQLite connections
        # are never disturbed, while holding zero SQLite connections or locks.
        self._fd_holder: list[int | None] = [None, None]
        self._fd_inodes: list[tuple[int, int] | None] = [None, None]
        self._finalizer = weakref.finalize(self, _close_fd_holder, self._fd_holder)
        self._snapshot_stamp: tuple[object, ...] | None = None
        self._snapshot_cache: DictionarySnapshot | None = None

    def close(self) -> None:
        """Release cached snapshot and raw read-only header descriptors."""
        with self._lock:
            self._close_fds_locked()
            self._snapshot_stamp = None
            self._snapshot_cache = None

    def _close_fd_slot_locked(self, slot: int) -> None:
        fd = self._fd_holder[slot]
        self._fd_holder[slot] = None
        self._fd_inodes[slot] = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _close_fds_locked(self) -> None:
        self._close_fd_slot_locked(0)
        self._close_fd_slot_locked(1)

    def _pread_slot_locked(
        self,
        slot: int,
        path: Path,
        inode: tuple[int, int],
        size: int,
        offset: int = 0,
    ) -> bytes:
        fd = self._fd_holder[slot]
        if fd is None or self._fd_inodes[slot] != inode:
            self._close_fd_slot_locked(slot)
            fd = os.open(path, os.O_RDONLY)
            self._fd_holder[slot] = fd
            self._fd_inodes[slot] = inode
        return os.pread(fd, size, offset)

    def _storage_stamp_locked(self) -> tuple[tuple[object, ...], bool]:
        """Return ``(storage_stamp, cacheable)`` without persistent SQLite connections.

        - Main DB: ``os.stat`` + SQLite header bytes ``24..44`` (``file_change_counter``,
          ``db_size_pages``, freelist header, ``schema_cookie``) read via
          ``os.pread`` on a cached ``O_RDONLY`` fd, detecting same-second
          same-size DELETE-mode commits even on coarse-grained filesystems while
          preserving in-process POSIX ``fcntl`` locks.
        - WAL visibility: reads the mirrored 48-byte WAL-index headers (bytes
          ``0..96``) from ``-shm`` via ``os.pread`` on a cached ``O_RDONLY`` fd
          and requires ``hdr1 == hdr2`` and ``isInit == 1``. Raw uncommitted
          ``-wal`` frames written before ``-shm`` advances are NOT used as cache
          keys, and transient states (``hdr1 != hdr2``, ``isInit != 1``, or a
          non-empty ``-wal`` without a valid ``-shm``) return ``cacheable=False``
          so stale snapshots are never cached.
        """
        path = self.db_path.resolve(strict=False)
        try:
            main_stat = _stat_tuple(path)
        except OSError:
            self._close_fds_locked()
            raise
        main_inode = (main_stat[0], main_stat[1])
        try:
            main_header = self._pread_slot_locked(0, path, main_inode, 44, 0)
            main_header_slice = (
                main_header[24:44] if len(main_header) >= 44 else main_header
            )
        except OSError:
            self._close_fd_slot_locked(0)
            main_header_slice = b""

        shm_path = Path(f"{path}-shm")
        wal_path = Path(f"{path}-wal")
        shm_stat = _optional_stat_tuple(shm_path)
        wal_stat = _optional_stat_tuple(wal_path)
        # Also detect symlink-adjacent -shm/-wal if db_path itself is a symlink.
        if shm_stat is None and Path(f"{self.db_path}-shm") != shm_path:
            alt_shm = Path(f"{self.db_path}-shm")
            shm_stat = _optional_stat_tuple(alt_shm)
            if shm_stat is not None:
                shm_path = alt_shm
        if wal_stat is None and Path(f"{self.db_path}-wal") != wal_path:
            wal_stat = _optional_stat_tuple(Path(f"{self.db_path}-wal"))

        cacheable = True
        shm_key: tuple[object, ...] | None = None
        if shm_stat is not None:
            shm_inode = (shm_stat[0], shm_stat[1])
            try:
                shm_raw = self._pread_slot_locked(1, shm_path, shm_inode, 96, 0)
                parsed_shm, shm_ok = _parse_shm_wal_index(shm_raw)
                if shm_ok and parsed_shm is not None:
                    shm_key = (shm_inode, parsed_shm)
                else:
                    cacheable = False
            except OSError:
                self._close_fd_slot_locked(1)
                cacheable = False
        else:
            self._close_fd_slot_locked(1)
            # Non-empty -wal without a valid -shm means WAL has not yet been
            # indexed/recovered; never cache against that transient state.
            if wal_stat is not None and wal_stat[2] > 0:
                cacheable = False

        wal_present = wal_stat is not None and wal_stat[2] > 0
        stamp = (
            str(path),
            main_stat,
            main_header_slice,
            shm_key,
            wal_present,
        )
        return stamp, cacheable

    def _identity(self) -> tuple[object, ...]:
        """Storage identity for cache validation, aware of SQLite WAL and DELETE commits."""
        with self._lock:
            stamp, _cacheable = self._storage_stamp_locked()
            return stamp

    def _read_snapshot(
        self, path: Path, stamp: tuple[object, ...]
    ) -> DictionarySnapshot:
        conn = _connect_for_snapshot(path)
        try:
            conn.execute("BEGIN")
            try:
                metadata_rows = conn.execute(
                    "SELECT key, value FROM metadata ORDER BY key"
                ).fetchall()
                term_rows = conn.execute(
                    """
                    SELECT * FROM terms
                    ORDER BY priority, zh_norm, en_norm, category, source_file, source_id
                    """
                ).fetchall()
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        metadata = {row["key"]: row["value"] for row in metadata_rows}
        entries = tuple(row_to_entry(row) for row in term_rows)
        row_norms = tuple(
            (row["zh_norm"], row["en_norm"], int(row["priority"]))
            for row in term_rows
        )
        return DictionarySnapshot(
            storage_stamp=stamp,
            metadata=metadata,
            entries=entries,
            row_norms=row_norms,
        )

    def snapshot(self) -> DictionarySnapshot:
        """Return the current immutable :class:`DictionarySnapshot`.

        Validates the cached snapshot against main-file header counters and
        ``-shm`` WAL-index headers without keeping any persistent SQLite
        connection open. Caches only when ``stamp_before == stamp_after`` and
        the WAL-index header is consistent (never during a mid-commit window).
        """
        with self._lock:
            snapshot: DictionarySnapshot | None = None
            for _attempt in range(2):
                stamp_before, cacheable_before = self._storage_stamp_locked()
                if (
                    cacheable_before
                    and self._snapshot_cache is not None
                    and stamp_before == self._snapshot_stamp
                ):
                    return self._snapshot_cache
                resolved_path = self.db_path.resolve(strict=False)
                snapshot = self._read_snapshot(resolved_path, stamp_before)
                stamp_after, cacheable_after = self._storage_stamp_locked()
                if (
                    cacheable_before
                    and cacheable_after
                    and stamp_before == stamp_after
                ):
                    self._snapshot_stamp = stamp_after
                    self._snapshot_cache = snapshot
                    return snapshot
            # Transient/in-flight write state (or DB changed twice while read):
            # return the freshly loaded snapshot without pinning it in cache.
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
        # zh_norm and en_norm with the same normalized form.
        norm = normalize_text(query)
        return list(self.snapshot().exact_index.get(norm, ()))

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
