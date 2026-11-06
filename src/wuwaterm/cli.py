"""Command line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from .builder import build_database, build_database_atomic
from .constants import DEFAULT_SOURCE_PROFILE_NAME, source_profile_choices
from .data_source import refresh_data
from .db import category_counts, connect
from .lookup import TermService
from .sentence import SentenceTranslator


def main(argv: list[str] | None = None) -> int:
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

    p_counts = sub.add_parser("counts")
    p_counts.add_argument("--db", required=True)

    p_lookup = sub.add_parser("lookup")
    p_lookup.add_argument("query")
    p_lookup.add_argument("--db", required=True)

    p_sentence = sub.add_parser("sentence")
    p_sentence.add_argument("text")
    p_sentence.add_argument("--db", required=True)

    p_bot = sub.add_parser("bot")
    p_bot.add_argument("--db", default="data/terms.db")

    args = parser.parse_args(argv)

    if args.command == "refresh-data":
        path = refresh_data(args.dest, profile_name=args.profile)
        print(path)
        return 0
    if args.command == "build-db":
        builder = build_database_atomic if args.atomic else build_database
        count = builder(args.data_dir, args.db, profile_name=args.profile)
        print(f"built {count} extracted records into {args.db}")
        return 0
    if args.command == "counts":
        with connect(args.db) as conn:
            for category, count in category_counts(conn).items():
                print(f"{category}\t{count}")
        return 0
    if args.command == "lookup":
        text = TermService(args.db).term_text(args.query)
        print(text or SentenceTranslator(args.db).translate(args.query))
        return 0
    if args.command == "sentence":
        print(SentenceTranslator(args.db).translate(args.text))
        return 0
    if args.command == "bot":
        try:
            from .bot import run_bot
        except ModuleNotFoundError as exc:
            if exc.name != "telegram":
                raise
            parser.error(
                "the bot command requires the Telegram extra; "
                "install wuwaterm[telegram]"
            )
        run_bot(Path(args.db))
        return 0
    parser.error("unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
