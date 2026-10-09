from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wuwaterm.constants import (  # noqa: E402
    DEFAULT_SOURCE_PROFILE_NAME,
    FREE_TEXT_LOCK_EXCLUSIONS_KEY,
    FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY,
    get_source_profile,
    source_profile_choices,
)
from wuwaterm.db import SCHEMA_VERSION  # noqa: E402
from wuwaterm.normalize import normalize_text  # noqa: E402
from wuwaterm.sentence import (  # noqa: E402
    parse_free_text_lock_exclusions,
    parse_free_text_zh_lock_exclusions,
)


REQUIRED_CATEGORIES = (
    "core_term",
    "resonator",
    "weapon",
    "echo",
    "item",
    "skill",
    "sonata_effect",
    "location",
)

EXPECTED_COLUMNS = {
    "metadata": (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ),
    "terms": (
        ("id", "INTEGER", 0, 1),
        ("category", "TEXT", 1, 0),
        ("source_file", "TEXT", 1, 0),
        ("source_id", "TEXT", 1, 0),
        ("text_key", "TEXT", 1, 0),
        ("zh", "TEXT", 1, 0),
        ("en", "TEXT", 1, 0),
        ("zh_norm", "TEXT", 1, 0),
        ("en_norm", "TEXT", 1, 0),
        ("pinyin", "TEXT", 1, 0),
        ("pinyin_abbrev", "TEXT", 1, 0),
        ("priority", "INTEGER", 1, 0),
    ),
}

EXPECTED_INDEXES = {
    "metadata": {
        "sqlite_autoindex_metadata_1": (1, "pk"),
    },
    "terms": {
        "idx_terms_category": (0, "c"),
        "idx_terms_pinyin": (0, "c"),
        "idx_terms_en_norm": (0, "c"),
        "idx_terms_zh_norm": (0, "c"),
        "sqlite_autoindex_terms_1": (1, "u"),
    },
}


class VerificationError(RuntimeError):
    pass


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise VerificationError(f"database does not exist or is not a file: {path}")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot open database read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _schema_errors(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    }
    expected_tables = set(EXPECTED_COLUMNS)
    if tables != expected_tables:
        errors.append(
            "table set mismatch: "
            f"expected {sorted(expected_tables)}, got {sorted(tables)}"
        )
        return errors

    for table, expected_columns in EXPECTED_COLUMNS.items():
        actual_columns = tuple(
            (str(row["name"]), str(row["type"]), int(row["notnull"]), int(row["pk"]))
            for row in conn.execute(f"PRAGMA table_info({table})")
        )
        if actual_columns != expected_columns:
            errors.append(f"column mismatch for {table}: got {actual_columns!r}")

        actual_indexes = {
            str(row["name"]): (int(row["unique"]), str(row["origin"]))
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
        if actual_indexes != EXPECTED_INDEXES[table]:
            errors.append(f"index mismatch for {table}: got {actual_indexes!r}")
    return errors


def _metadata_errors(
    metadata: dict[str, str], profile_name: str
) -> list[str]:
    profile = get_source_profile(profile_name)
    expected = {
        "schema_version": SCHEMA_VERSION,
        "source_profile": profile.name,
        "source_repo_url": profile.repo_url,
        "source_commit": profile.pinned_commit,
        "source_game_version": profile.expected_game_version,
        "source_resource_version": profile.expected_resource_version,
        "source_changelist": profile.expected_changelist,
        "wutheringdata_commit": profile.pinned_commit,
    }
    errors: list[str] = []
    for key, expected_value in expected.items():
        actual = metadata.get(key)
        if expected_value is None:
            if not actual:
                errors.append(f"metadata {key} is missing or empty")
        elif actual != expected_value:
            errors.append(
                f"metadata {key} mismatch: expected {expected_value!r}, got {actual!r}"
            )
    return errors


def _exact_hit_errors(
    conn: sqlite3.Connection, exact_hits: tuple[tuple[str, str], ...]
) -> list[str]:
    errors: list[str] = []
    for zh, en in exact_hits:
        zh_targets = {
            str(row["en"])
            for row in conn.execute(
                "SELECT DISTINCT en FROM terms WHERE zh_norm = ? ORDER BY en",
                (normalize_text(zh),),
            )
        }
        en_targets = {
            str(row["zh"])
            for row in conn.execute(
                "SELECT DISTINCT zh FROM terms WHERE en_norm = ? ORDER BY zh",
                (normalize_text(en),),
            )
        }
        if zh_targets != {en}:
            errors.append(
                f"exact hit {zh!r} expected only {en!r}, got {sorted(zh_targets)!r}"
            )
        if en_targets != {zh}:
            errors.append(
                f"reverse exact hit {en!r} expected only {zh!r}, got {sorted(en_targets)!r}"
            )
    return errors


def _free_text_exclusion_errors(
    conn: sqlite3.Connection, metadata: dict[str, str], *, required: bool
) -> list[str]:
    """Check the builder-written free-text lock exclusions, if present.

    A missing key passes unless ``required``: databases built before the key
    existed, or without the official corpus, keep every surface lockable.
    A present key must parse under the current version and parameters, and
    every excluded surface must be an English surface of a term row.
    """
    value = metadata.get(FREE_TEXT_LOCK_EXCLUSIONS_KEY)
    if value is None:
        if required:
            return [f"metadata {FREE_TEXT_LOCK_EXCLUSIONS_KEY} is missing"]
        return []
    try:
        rows = parse_free_text_lock_exclusions(value)
    except (TypeError, ValueError) as exc:
        return [f"metadata {FREE_TEXT_LOCK_EXCLUSIONS_KEY} is invalid: {exc}"]
    missing = [
        surface
        for surface, _occurrences, _aligned in rows
        if conn.execute("SELECT 1 FROM terms WHERE en = ? LIMIT 1", (surface,)).fetchone()
        is None
    ]
    if missing:
        return [
            f"metadata {FREE_TEXT_LOCK_EXCLUSIONS_KEY} names {len(missing)} "
            f"surface(s) with no term row, first {missing[0]!r}"
        ]
    return []


def _free_text_zh_exclusion_errors(
    conn: sqlite3.Connection, metadata: dict[str, str], *, required: bool
) -> list[str]:
    """Check the builder-written Chinese-surface exclusions, if present.

    Same contract as the English key, mirrored: a missing key passes unless
    ``required`` (databases built before the key existed, or without the
    official corpus, keep every surface lockable). A present key must parse
    under the current zh version and parameters, and every excluded surface
    must be a Chinese surface of a term row.
    """
    value = metadata.get(FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY)
    if value is None:
        if required:
            return [f"metadata {FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY} is missing"]
        return []
    try:
        rows = parse_free_text_zh_lock_exclusions(value)
    except (TypeError, ValueError) as exc:
        return [f"metadata {FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY} is invalid: {exc}"]
    missing = [
        surface
        for surface, _occurrences, _aligned in rows
        if conn.execute("SELECT 1 FROM terms WHERE zh = ? LIMIT 1", (surface,)).fetchone()
        is None
    ]
    if missing:
        return [
            f"metadata {FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY} names {len(missing)} "
            f"surface(s) with no term row, first {missing[0]!r}"
        ]
    return []


def verify_database(
    path: str | Path,
    *,
    profile_name: str = DEFAULT_SOURCE_PROFILE_NAME,
    required_categories: tuple[str, ...] = REQUIRED_CATEGORIES,
    exact_hits: tuple[tuple[str, str], ...] | None = None,
    require_free_text_exclusions: bool = False,
    require_free_text_zh_exclusions: bool = False,
) -> tuple[dict[str, int], dict[str, str]]:
    profile = get_source_profile(profile_name)
    checks = profile.representative_exact_hits if exact_hits is None else exact_hits
    errors: list[str] = []
    with closing(_connect_read_only(Path(path))) as conn:
        try:
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        except sqlite3.Error as exc:
            raise VerificationError(f"integrity check failed to run: {exc}") from exc
        if integrity != ["ok"]:
            errors.append(f"integrity check failed: {integrity!r}")
        errors.extend(_schema_errors(conn))
        if errors:
            raise VerificationError("; ".join(errors))

        metadata = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM metadata ORDER BY key")
        }
        errors.extend(_metadata_errors(metadata, profile.name))
        counts = {
            str(row["category"]): int(row["count"])
            for row in conn.execute(
                "SELECT category, COUNT(*) AS count FROM terms "
                "GROUP BY category ORDER BY category"
            )
        }
        missing = [name for name in required_categories if counts.get(name, 0) <= 0]
        if missing:
            errors.append(f"missing or empty categories: {', '.join(missing)}")
        errors.extend(_exact_hit_errors(conn, checks))
        errors.extend(
            _free_text_exclusion_errors(
                conn, metadata, required=require_free_text_exclusions
            )
        )
        errors.extend(
            _free_text_zh_exclusion_errors(
                conn, metadata, required=require_free_text_zh_exclusions
            )
        )

    if errors:
        raise VerificationError("; ".join(errors))
    return counts, metadata


def _parse_exact_hit(value: str) -> tuple[str, str]:
    try:
        zh, en = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("exact hit must be ZH=EN") from exc
    if not zh or not en:
        raise argparse.ArgumentTypeError("exact hit must have non-empty ZH and EN")
    return zh, en


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument(
        "--profile", choices=source_profile_choices(), default=DEFAULT_SOURCE_PROFILE_NAME
    )
    parser.add_argument("--min-category", action="append", default=[])
    parser.add_argument("--exact-hit", action="append", type=_parse_exact_hit, default=[])
    parser.add_argument(
        "--require-free-text-exclusions",
        action="store_true",
        help=(
            f"fail when the {FREE_TEXT_LOCK_EXCLUSIONS_KEY} metadata key is "
            "missing; a full build-db writes it"
        ),
    )
    parser.add_argument(
        "--require-free-text-zh-exclusions",
        action="store_true",
        help=(
            f"fail when the {FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY} metadata key "
            "is missing; a full build-db writes it"
        ),
    )
    args = parser.parse_args()

    profile = get_source_profile(args.profile)
    categories = tuple(dict.fromkeys((*REQUIRED_CATEGORIES, *args.min_category)))
    exact_hits = tuple(args.exact_hit) or profile.representative_exact_hits
    try:
        counts, metadata = verify_database(
            args.db,
            profile_name=profile.name,
            required_categories=categories,
            exact_hits=exact_hits,
            require_free_text_exclusions=args.require_free_text_exclusions,
            require_free_text_zh_exclusions=args.require_free_text_zh_exclusions,
        )
    except (VerificationError, sqlite3.Error) as exc:
        print(f"database verification failed: {exc}", file=sys.stderr)
        return 1

    for category in sorted(counts):
        print(f"{category}\t{counts[category]}")
    for key in (
        "schema_version",
        "source_profile",
        "source_repo_url",
        "source_commit",
        "source_game_version",
        "source_resource_version",
        "source_changelist",
    ):
        print(f"{key}\t{metadata[key]}")
    exclusions = metadata.get(FREE_TEXT_LOCK_EXCLUSIONS_KEY)
    if exclusions is None:
        print(f"{FREE_TEXT_LOCK_EXCLUSIONS_KEY}\tabsent")
    else:
        count = len(parse_free_text_lock_exclusions(exclusions))
        print(f"{FREE_TEXT_LOCK_EXCLUSIONS_KEY}\t{count} surfaces")
    zh_exclusions = metadata.get(FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY)
    if zh_exclusions is None:
        print(f"{FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY}\tabsent")
    else:
        zh_count = len(parse_free_text_zh_lock_exclusions(zh_exclusions))
        print(f"{FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY}\t{zh_count} surfaces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
