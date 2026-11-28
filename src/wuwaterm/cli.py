"""Command line interface."""

from __future__ import annotations

import argparse
import sys

from .builder import build_database, build_database_atomic
from .constants import DEFAULT_SOURCE_PROFILE_NAME, source_profile_choices
from .data_source import refresh_data
from .db import category_counts, connect
from .knowledge_builder import (
    build_knowledge_database,
    build_knowledge_database_atomic,
)
from .lookup import TermService
from .sentence import SentenceTranslator, resolve_provider_profile_from_env

RETIRED_BOT_MESSAGE = (
    "error: the legacy inline Telegram bot (`wuwaterm bot`) has been retired "
    "from WuwaTerm Core; use the standalone My-Denia/WuwaTerm-Telegram "
    "product instead (or roll back to v0.16.0 if you still need the inline bot)"
)


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if raw_argv and raw_argv[0] == "bot":
        print(RETIRED_BOT_MESSAGE, file=sys.stderr)
        return 64

    parser = argparse.ArgumentParser(prog="wuwaterm")
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh-data")
    p_refresh.add_argument("--dest", required=True)
    p_refresh.add_argument(
        "--profile",
        choices=source_profile_choices(),
        default=DEFAULT_SOURCE_PROFILE_NAME,
    )

    p_build = sub.add_parser("build-db")
    p_build.add_argument("--data-dir", required=True)
    p_build.add_argument("--db", required=True)
    p_build.add_argument("--profile", choices=source_profile_choices())
    p_build.add_argument(
        "--atomic",
        action="store_true",
        help="build in a same-directory temp file, then replace the target db",
    )

    p_build_kb = sub.add_parser("build-kb")
    p_build_kb.add_argument("--data-dir", required=True)
    p_build_kb.add_argument("--db", required=True)
    p_build_kb.add_argument("--terms-db", required=True)
    p_build_kb.add_argument("--profile", choices=source_profile_choices())
    p_build_kb.add_argument(
        "--atomic",
        action="store_true",
        help="build in a same-directory temp file, then replace the target knowledge db",
    )

    p_counts = sub.add_parser("counts")
    p_counts.add_argument("--db", required=True)

    p_lookup = sub.add_parser("lookup")
    p_lookup.add_argument("query")
    p_lookup.add_argument("--db", required=True)

    p_sentence = sub.add_parser("sentence")
    p_sentence.add_argument("text")
    p_sentence.add_argument("--db", required=True)

    args = parser.parse_args(raw_argv)

    if args.command == "refresh-data":
        path = refresh_data(args.dest, profile_name=args.profile)
        print(path)
        return 0
    if args.command == "build-db":
        builder = build_database_atomic if args.atomic else build_database
        count = builder(args.data_dir, args.db, profile_name=args.profile)
        print(f"built {count} extracted records into {args.db}")
        return 0
    if args.command == "build-kb":
        kb_builder = (
            build_knowledge_database_atomic if args.atomic else build_knowledge_database
        )
        stats = kb_builder(
            args.data_dir,
            args.db,
            terms_db_path=args.terms_db,
            profile_name=args.profile,
        )
        print(
            f"built {stats.entities} entities "
            f"({stats.aliases} aliases, {stats.contexts} contexts, "
            f"{stats.relations} relations, {stats.short_ngrams} short_ngrams, "
            f"{stats.fts_rows} fts_rows) into {args.db}"
        )
        return 0
    if args.command == "counts":
        with connect(args.db) as conn:
            for category, count in category_counts(conn).items():
                print(f"{category}\t{count}")
        return 0
    if args.command == "lookup":
        text = TermService(args.db).term_text(args.query)
        print(
            text
            or SentenceTranslator(
                args.db,
                provider_profile=resolve_provider_profile_from_env(),
            ).translate(args.query)
        )
        return 0
    if args.command == "sentence":
        print(
            SentenceTranslator(
                args.db,
                provider_profile=resolve_provider_profile_from_env(),
            ).translate(args.text)
        )
        return 0
    parser.error("unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
