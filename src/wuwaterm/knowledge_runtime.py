"""Read-only runtime lifecycle, revision checks, and query helpers for ``knowledge.db``.

This module sits in ``DOMAIN_CORE`` (storage-coupled, forbidden to ``PURE_MATCHING``)
so ``sentence``, ``application``, and indirectly ``wuwaterm_api`` share one
thread-safe, fail-closed implementation for companion knowledge-base validation
and read-only queries without leaking local filesystem paths into logs or errors.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domain_knowledge import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_SCALARS,
    DomainKnowledgeCandidate,
    prepare_prompt_domain_knowledge_candidates,
)
from .knowledge_db import (
    KnowledgeBaseError,
    KnowledgeBaseReader,
    KnowledgeBaseRevisionMismatchError,
)
from .lookup import TermService


LOGGER = logging.getLogger(__name__)

KnowledgeStatusReason = Literal[
    "ok",
    "unconfigured",
    "missing",
    "revision_mismatch",
    "unreadable",
    "terms_unavailable",
]
KnowledgeQueryMode = Literal["lexical", "hybrid"]

_HASH_CHUNK_BYTES = 64 * 1024
_QUERY_MAX_TOP_K = 10


class KnowledgeUnavailableError(RuntimeError):
    """Raised when the companion ``knowledge.db`` is unconfigured or unusable.

    Messages never contain local filesystem paths or raw SQLite error text.
    """


@dataclass(frozen=True)
class KnowledgeBaseStatus:
    """Validated runtime availability of the companion ``knowledge.db``."""

    available: bool
    knowledge_revision: str | None = None
    terms_db_revision: str | None = None
    reason: KnowledgeStatusReason = "unconfigured"


@dataclass(frozen=True)
class DomainKnowledgeQueryResult:
    """Read-only response for ``POST /v1/knowledge/query`` and application callers."""

    knowledge_revision: str
    mode_used: KnowledgeQueryMode
    hits: tuple[DomainKnowledgeCandidate, ...] = ()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class KnowledgeRuntime:
    """Thread-safe, revision-checked read-only runtime wrapper for ``knowledge.db``.

    Automatically re-validates whenever either ``terms.db`` changes its
    cryptographic revision or the companion ``knowledge.db`` file stat changes.
    Any missing file, corrupt SQLite database, or ``terms_db_revision`` mismatch
    logs one sanitized warning (never echoing paths) and degrades safely.
    """

    def __init__(
        self,
        knowledge_db_path: str | Path | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._path: Path | None = (
            Path(knowledge_db_path) if knowledge_db_path is not None else None
        )
        self._logger = LOGGER if logger is None else logger
        self._lock = threading.RLock()
        self._reader: KnowledgeBaseReader | None = None
        self._reader_revision: str | None = None
        self._reader_stat: tuple[int, int, int, int, int] | None = None
        self._knowledge_revision: str | None = None
        self._failed_key: tuple[str, tuple[int, int, int, int, int]] | None = None
        self._warned_key: tuple[object, ...] | None = None

    @property
    def path(self) -> Path | None:
        return self._path

    def close(self) -> None:
        """Close any cached read-only ``KnowledgeBaseReader``."""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        reader = self._reader
        self._reader = None
        self._reader_revision = None
        self._reader_stat = None
        self._knowledge_revision = None
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass

    def __del__(self) -> None:
        reader = getattr(self, "_reader", None)
        if reader is not None:
            self._reader = None
            try:
                reader.close()
            except Exception:
                pass

    def _warn_once_locked(self, key: tuple[object, ...], message: str) -> None:
        if self._warned_key != key:
            self._warned_key = key
            self._logger.warning(message)

    def _mark_query_failed_locked(self) -> None:
        revision = self._reader_revision
        stat_key = self._reader_stat
        self._close_locked()
        if revision is not None and stat_key is not None:
            self._failed_key = (revision, stat_key)

    def _resolve_reader_locked(
        self, service: TermService
    ) -> tuple[KnowledgeBaseReader | None, KnowledgeBaseStatus]:
        kb_path = self._path
        if kb_path is None:
            return None, KnowledgeBaseStatus(available=False, reason="unconfigured")
        try:
            expected_revision = service.snapshot().dictionary_identity.revision
        except Exception:
            self._close_locked()
            return None, KnowledgeBaseStatus(available=False, reason="terms_unavailable")
        try:
            st = os.stat(kb_path)
            if not kb_path.is_file():
                raise FileNotFoundError("not a regular file")
            stat_key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except OSError:
            self._close_locked()
            self._failed_key = None
            self._warn_once_locked(
                ("missing", expected_revision),
                "companion knowledge database is missing; degrading to dictionary-only mode",
            )
            return None, KnowledgeBaseStatus(
                available=False,
                terms_db_revision=expected_revision,
                reason="missing",
            )
        if self._failed_key == (expected_revision, stat_key):
            return None, KnowledgeBaseStatus(
                available=False,
                terms_db_revision=expected_revision,
                reason="unreadable",
            )
        if (
            self._reader is not None
            and self._reader_revision == expected_revision
            and self._reader_stat == stat_key
            and self._knowledge_revision is not None
        ):
            return self._reader, KnowledgeBaseStatus(
                available=True,
                knowledge_revision=self._knowledge_revision,
                terms_db_revision=expected_revision,
                reason="ok",
            )
        self._close_locked()
        reader: KnowledgeBaseReader | None = None
        try:
            reader = KnowledgeBaseReader(
                kb_path, expected_terms_db_revision=expected_revision
            )
            kb_revision = _file_sha256(kb_path)
        except KnowledgeBaseRevisionMismatchError:
            self._failed_key = None
            self._warn_once_locked(
                ("revision_mismatch", expected_revision, stat_key),
                "companion knowledge database terms_db_revision mismatch; "
                "degrading to dictionary-only mode",
            )
            return None, KnowledgeBaseStatus(
                available=False,
                terms_db_revision=expected_revision,
                reason="revision_mismatch",
            )
        except (KnowledgeBaseError, sqlite3.Error, OSError):
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            self._failed_key = None
            self._warn_once_locked(
                ("unreadable", expected_revision, stat_key),
                "companion knowledge database could not be opened read-only; "
                "degrading to dictionary-only mode",
            )
            return None, KnowledgeBaseStatus(
                available=False,
                terms_db_revision=expected_revision,
                reason="unreadable",
            )
        self._reader = reader
        self._reader_revision = expected_revision
        self._reader_stat = stat_key
        self._knowledge_revision = kb_revision
        self._failed_key = None
        self._warned_key = None
        return reader, KnowledgeBaseStatus(
            available=True,
            knowledge_revision=kb_revision,
            terms_db_revision=expected_revision,
            reason="ok",
        )

    def status(self, service: TermService) -> KnowledgeBaseStatus:
        """Return live revision-verified availability status of ``knowledge.db``."""
        with self._lock:
            _reader, kb_status = self._resolve_reader_locked(service)
            return kb_status

    def query_candidates(
        self,
        service: TermService,
        text: str,
        *,
        locked_surfaces: Collection[str] = (),
        reference_surfaces: Collection[str] = (),
        anchor_surfaces: Collection[str] = (),
        use_graph: bool = True,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        max_scalars: int = DEFAULT_MAX_SCALARS,
    ) -> tuple[DomainKnowledgeCandidate, ...]:
        """Retrieve soft read-only candidates for translation prompts.

        Degrades safely to ``()`` on any unavailability or SQLite error without
        blocking translation or leaking filesystem paths.
        """
        if self._path is None or not text or not text.strip():
            return ()
        with self._lock:
            reader, _status = self._resolve_reader_locked(service)
            if reader is None:
                return ()
            try:
                raw = reader.query(
                    text,
                    locked_surfaces=locked_surfaces,
                    reference_surfaces=reference_surfaces,
                    anchor_surfaces=anchor_surfaces,
                    use_graph=use_graph,
                    max_candidates=max_candidates,
                    max_scalars=max_scalars,
                )
            except (KnowledgeBaseError, sqlite3.Error):
                failed_rev = self._reader_revision
                self._mark_query_failed_locked()
                self._warn_once_locked(
                    ("query_error", failed_rev),
                    "companion knowledge database query failed; degrading to dictionary-only mode",
                )
                return ()
            return prepare_prompt_domain_knowledge_candidates(
                text,
                raw,
                locked_surfaces=locked_surfaces,
                reference_surfaces=reference_surfaces,
                max_candidates=max_candidates,
                max_scalars=max_scalars,
            )

    def query_domain_knowledge(
        self,
        service: TermService,
        query: str,
        *,
        top_k: int = 5,
        mode: KnowledgeQueryMode = "lexical",
        categories: Sequence[str] | None = None,
        hybrid_rag_available: bool = False,
    ) -> DomainKnowledgeQueryResult:
        """Run a read-only domain knowledge query with strict fail-closed semantics.

        Raises ``ValueError`` for invalid parameters or unsupported ``hybrid`` mode,
        and ``KnowledgeUnavailableError`` when ``knowledge.db`` is unconfigured,
        missing, unreadable, or mismatched against ``terms.db``.
        """
        cleaned = (query or "").strip()
        if not cleaned or len(cleaned) > 500:
            raise ValueError("query must be between 1 and 500 characters")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= _QUERY_MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {_QUERY_MAX_TOP_K}")
        if mode not in ("lexical", "hybrid"):
            raise ValueError("mode must be 'lexical' or 'hybrid'")
        if mode == "hybrid" and not hybrid_rag_available:
            raise ValueError("hybrid-rag-v1 capability is not enabled")

        category_filter: frozenset[str] | None = None
        if categories is not None:
            normalized_categories = [item.strip() for item in categories if item.strip()]
            if len(normalized_categories) != len(categories):
                raise ValueError("categories must not contain blank entries")
            if normalized_categories:
                category_filter = frozenset(normalized_categories)

        with self._lock:
            reader, kb_status = self._resolve_reader_locked(service)
            if reader is None or not kb_status.available or kb_status.knowledge_revision is None:
                raise KnowledgeUnavailableError("domain knowledge base is unavailable")
            try:
                raw_hits = reader.query(
                    cleaned,
                    categories=category_filter,
                    use_graph=True,
                    max_candidates=top_k,
                    max_scalars=max(top_k * 240, 1200),
                )
            except (KnowledgeBaseError, sqlite3.Error):
                failed_rev = self._reader_revision
                self._mark_query_failed_locked()
                self._warn_once_locked(
                    ("query_error", failed_rev),
                    "companion knowledge database query failed; degrading to dictionary-only mode",
                )
                raise KnowledgeUnavailableError(
                    "domain knowledge base is unavailable"
                ) from None

            filtered: list[DomainKnowledgeCandidate] = []
            for hit in raw_hits:
                if hit.lock_eligible is not False:
                    continue
                if category_filter is not None and hit.entity_type not in category_filter:
                    continue
                filtered.append(hit)
                if len(filtered) >= top_k:
                    break

            return DomainKnowledgeQueryResult(
                knowledge_revision=kb_status.knowledge_revision,
                mode_used="lexical",
                hits=tuple(filtered),
            )
