#!/bin/sh
set -eu

DB_PATH="${WUWATERM_DB_PATH:-/app/data/terms.db}"
COMMAND="${1:-api}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$COMMAND" in
  bot)
    echo "the legacy inline Telegram bot has been retired from WuwaTerm Core; use the standalone My-Denia/WuwaTerm-Telegram product instead (or roll back to v0.16.0)" >&2
    exit 64
    ;;
  api)
    # Serving surface, read-only database. It binds loopback by default
    # (WUWATERM_API_BIND) and never gains a data-build command.
    WUWATERM_DB_PATH="$DB_PATH" exec python -m wuwaterm_api.cli serve "$@"
    ;;
  device)
    # Operator-only credential management, run as a one-shot container.
    exec python -m wuwaterm_api.cli device "$@"
    ;;
  *)
    echo "runtime image only serves the api and device commands; use the builder image for data commands" >&2
    exit 64
    ;;
esac
