"""Verify a built companion ``knowledge.db`` artifact."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.domain_knowledge import TRUST_TIERS  # noqa: E402
from wuwaterm.knowledge_db import (  # noqa: E402
    EXPECTED_KB_ALL_TABLES,
    KB_SCHEMA_VERSION,
    KnowledgeBaseError,
    KnowledgeBaseReader,
    compute_terms_db_revision,
    connect_knowledge_db_read_only,
)


REQUIRED_METADATA_KEYS = (
    "kb_schema_version",
    "terms_db_revision",
    "source_commit",
    "source_game_version",
)

_LOCK_ELIGIBLE_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*lock_eligible\s*=\s*0\s*\)",
    re.IGNORECASE,
)


class VerificationError(RuntimeError):
    """Raised when ``knowledge.db`` fails any structural or semantic invariant."""


def _table_set_errors(conn: sqlite3.Connection) -> list[str]:
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    }
    if tables != EXPECTED_KB_ALL_TABLES:
        return [
            "table set mismatch: "
            f"expected {sorted(EXPECTED_KB_ALL_TABLES)}, got {sorted(tables)}"
        ]
    return []


def _schema_constraint_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    alias_ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_aliases'"
    ).fetchone()
    alias_ddl = str(alias_ddl_row["sql"]) if alias_ddl_row and alias_ddl_row["sql"] else ""
    if not _LOCK_ELIGIBLE_CHECK_RE.search(alias_ddl):
        errors.append("kb_aliases is missing CHECK(lock_eligible = 0) constraint")

    bad_lock_row = conn.execute(
        "SELECT COUNT(*) AS count FROM kb_aliases WHERE lock_eligible != 0"
    ).fetchone()
    bad_lock_count = int(bad_lock_row["count"]) if bad_lock_row is not None else 0
    if bad_lock_count != 0:
        errors.append(f"kb_aliases contains {bad_lock_count} row(s) with lock_eligible != 0")

    fts_ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'kb_fts_trigram'"
    ).fetchone()
    fts_ddl = str(fts_ddl_row["sql"]) if fts_ddl_row and fts_ddl_row["sql"] else ""
    if "fts5" not in fts_ddl.casefold() or "trigram" not in fts_ddl.casefold():
        errors.append("kb_fts_trigram is not an FTS5 trigram virtual table")

    return errors


def _foreign_key_errors(conn: sqlite3.Connection) -> list[str]:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        return [f"foreign key check failed with {len(violations)} violation(s)"]
    return []


def _metadata_errors(
    metadata: dict[str, str],
    *,
    expected_terms_db_revision: str | None,
) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_METADATA_KEYS:
        if not metadata.get(key):
            errors.append(f"kb_metadata {key} is missing or empty")
    if metadata.get("kb_schema_version") != KB_SCHEMA_VERSION:
        errors.append(
            f"kb_schema_version mismatch: expected {KB_SCHEMA_VERSION!r}, "
            f"got {metadata.get('kb_schema_version')!r}"
        )
    if (
        expected_terms_db_revision is not None
        and metadata.get("terms_db_revision") != expected_terms_db_revision
    ):
        errors.append(
            "terms_db_revision mismatch: "
            f"expected {expected_terms_db_revision!r}, "
            f"got {metadata.get('terms_db_revision')!r}"
        )
    return errors


def _index_queryability_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    bad_tier_rows = conn.execute(
        "SELECT DISTINCT trust_tier FROM kb_entities"
    ).fetchall()
    for row in bad_tier_rows:
        if str(row["trust_tier"]) not in TRUST_TIERS:
            errors.append(f"invalid trust_tier in kb_entities: {row['trust_tier']!r}")

    bad_ngram_len = conn.execute(
        "SELECT COUNT(*) AS count FROM kb_short_ngrams WHERE ngram_len NOT IN (1, 2)"
    ).fetchone()
    if bad_ngram_len is not None and int(bad_ngram_len["count"]) != 0:
        errors.append("kb_short_ngrams contains rows with ngram_len not in (1, 2)")

    sample_fts = conn.execute(
        "SELECT surface FROM kb_fts_trigram WHERE LENGTH(surface) >= 3 LIMIT 1"
    ).fetchone()
    if sample_fts is not None:
        sample_surface = str(sample_fts["surface"]).strip()
        probe = sample_surface[:3].replace('"', '""')
        try:
            hit = conn.execute(
                "SELECT entity_id FROM kb_fts_trigram "
                "WHERE kb_fts_trigram MATCH ? LIMIT 1",
                (f'"{probe}"',),
            ).fetchone()
        except sqlite3.Error as exc:
            errors.append(f"kb_fts_trigram query failed: {exc}")
        else:
            if hit is None:
                errors.append(
                    f"kb_fts_trigram returned no hit for indexed probe {probe!r}"
                )
    else:
        try:
            conn.execute(
                "SELECT entity_id FROM kb_fts_trigram "
                "WHERE kb_fts_trigram MATCH '\"abc\"' LIMIT 1"
            ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"kb_fts_trigram query failed: {exc}")

    sample_short = conn.execute(
        "SELECT ngram, ngram_len FROM kb_short_ngrams LIMIT 1"
    ).fetchone()
    if sample_short is not None:
        try:
            hit = conn.execute(
                "SELECT entity_id FROM kb_short_ngrams "
                "WHERE ngram = ? AND ngram_len = ? LIMIT 1",
                (str(sample_short["ngram"]), int(sample_short["ngram_len"])),
            ).fetchone()
        except sqlite3.Error as exc:
            errors.append(f"kb_short_ngrams query failed: {exc}")
        else:
            if hit is None:
                errors.append("kb_short_ngrams returned no hit for indexed probe")

    return errors


def verify_knowledge_database(
    path: str | Path,
    *,
    terms_db_path: str | Path | None = None,
    expected_terms_db_revision: str | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    """Verify structure, constraints, FKs, revision binding and lexical indexes."""
    resolved_expected = expected_terms_db_revision
    if terms_db_path is not None:
        try:
            computed = compute_terms_db_revision(terms_db_path)
        except KnowledgeBaseError as exc:
            raise VerificationError(str(exc)) from exc
        if resolved_expected is not None and resolved_expected != computed:
            raise VerificationError(
                "expected_terms_db_revision disagrees with computed terms.db revision"
            )
        resolved_expected = computed

    try:
        conn = connect_knowledge_db_read_only(path)
    except KnowledgeBaseError as exc:
        raise VerificationError(str(exc)) from exc

    errors: list[str] = []
    with closing(conn):
        try:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        except sqlite3.Error as exc:
            raise VerificationError(f"integrity check failed to run: {exc}") from exc
        if integrity != ["ok"]:
            errors.append(f"integrity check failed: {integrity!r}")

        errors.extend(_table_set_errors(conn))
        if errors:
            raise VerificationError("; ".join(errors))

        errors.extend(_schema_constraint_errors(conn))
        errors.extend(_foreign_key_errors(conn))

        metadata = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM kb_metadata ORDER BY key")
        }
        errors.extend(
            _metadata_errors(metadata, expected_terms_db_revision=resolved_expected)
        )

        counts = {
            str(row["entity_type"]): int(row["count"])
            for row in conn.execute(
                "SELECT entity_type, COUNT(*) AS count FROM kb_entities "
                "GROUP BY entity_type ORDER BY entity_type"
            )
        }
        if not counts:
            errors.append("kb_entities is empty")

        errors.extend(_index_queryability_errors(conn))

    if errors:
        raise VerificationError("; ".join(errors))

    try:
        with KnowledgeBaseReader(
            path, expected_terms_db_revision=metadata["terms_db_revision"]
        ):
            pass
    except KnowledgeBaseError as exc:
        raise VerificationError(f"reader validation failed: {exc}") from exc

    return counts, metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="path to companion knowledge.db")
    parser.add_argument(
        "--terms-db",
        default=None,
        help="path to companion terms.db whose revision must match kb_metadata",
    )
    parser.add_argument(
        "--expected-terms-db-revision",
        default=None,
        help="expected SHA-256 terms_db_revision value",
    )
    args = parser.parse_args(argv)

    try:
        counts, metadata = verify_knowledge_database(
            args.db,
            terms_db_path=args.terms_db,
            expected_terms_db_revision=args.expected_terms_db_revision,
        )
    except (VerificationError, sqlite3.Error) as exc:
        print(f"knowledge database verification failed: {exc}", file=sys.stderr)
        return 1

    for entity_type in sorted(counts):
        print(f"{entity_type}\t{counts[entity_type]}")
    for key in REQUIRED_METADATA_KEYS:
        print(f"{key}\t{metadata[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
