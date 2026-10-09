#!/bin/sh
set -eu

DB_PATH="${WUWATERM_DB_PATH:-/app/data/terms.db}"
DATA_DIR="${WUWATERM_DATA_DIR:-/app/data/wutheringdata}"
SOURCE_PROFILE="${WUWATERM_SOURCE_PROFILE:-arikatsu}"
COMMAND="${1:-verify-db}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$COMMAND" in
  refresh-data)
    exec python -m wuwaterm.cli refresh-data --dest "$DATA_DIR" --profile "$SOURCE_PROFILE" "$@"
    ;;
  build-db)
    exec python -m wuwaterm.cli build-db --data-dir "$DATA_DIR" --db "$DB_PATH" --profile "$SOURCE_PROFILE" "$@"
    ;;
  verify-db)
    exec python scripts/verify_db.py "$DB_PATH" \
      --profile "$SOURCE_PROFILE" \
      --min-category resonator \
      --min-category weapon \
      --min-category echo \
      --min-category item \
      --min-category skill \
      --min-category sonata_effect \
      --min-category location \
      --require-free-text-exclusions \
      "$@"
    ;;
  *)
    exec "$COMMAND" "$@"
    ;;
esac
