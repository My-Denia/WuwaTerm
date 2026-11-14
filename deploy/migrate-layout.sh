#!/bin/sh
# One-time move of a host from an in-place checkout, <base>/current as a
# directory that holds the code and its own state, to the release layout in
# deploy/release-layout.sh. Safe to run again: a finished host is a no-op and
# an interrupted run continues where it stopped.
#
#   sh deploy/migrate-layout.sh [--dry-run] [BASE]        (BASE: /opt/wuwaterm)
#
# Every step is printed with the command that undoes it. Only renames inside
# one filesystem happen, so nothing is copied and no container is touched: a
# running container keeps the directory it mounted, wherever it is renamed.
# Containers are not restarted here. Until the first deploy recreates them,
# compatibility links under releases/r1 keep their old mount paths resolving;
# that first deploy (it restarts bot and API, a pause of a few seconds) must be
# a full one, because --runtime-only refuses containers that still mount the
# old paths.
set -eu

dry_run=0
base=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -*) echo "usage: sh deploy/migrate-layout.sh [--dry-run] [BASE]" >&2; exit 64 ;;
    *) base=$arg ;;
  esac
done
base="${base:-/opt/wuwaterm}"
here="$(cd "$(dirname "$0")" && pwd -P)"
. "$here/release-layout.sh"

say() { echo "migrate: $*"; }
refuse() { echo "migrate: refusing: $*" >&2; exit 1; }
# act DESCRIPTION UNDO-COMMAND COMMAND...: print, then run unless --dry-run.
act() {
  say "$1"
  echo "  undo: $2"
  shift 2
  echo "  run:  $*"
  if [ "$dry_run" -eq 0 ]; then "$@"; fi
}

[ "$dry_run" -eq 0 ] || say "DRY RUN: only reads; nothing below is executed"
[ -d "$base" ] || refuse "$base is not a directory"
base="$(cd "$base" && pwd -P)"
rel="$base/releases/r1"
state_dirs="data state state-api state-web"

# Where the checkout is now: still <base>/current, or already moved to r1.
if [ -L "$base/current" ]; then
  [ "$(readlink "$base/current")" = "releases/r1" ] || refuse "$base/current points at $(readlink "$base/current"), not releases/r1"
  code="$rel"
elif [ -d "$base/current" ]; then
  [ ! -e "$rel" ] || refuse "$rel and a real $base/current both exist"
  code="$base/current"
elif [ -d "$rel/.git" ]; then
  code="$rel"
else
  refuse "$base has neither a current checkout nor $rel"
fi
[ -d "$code/.git" ] || refuse "$code is not a git checkout"

# Items of mutable state still sitting in the checkout.
in_pending() { case " $pending " in *" $1 "*) return 0 ;; esac; return 1; }
pending=""
for item in $state_dirs .deployments .env .deploy_commit "$code"/.env.*; do
  item=${item##*/}
  [ "$item" != ".env.*" ] && [ "$item" != ".env.example" ] || continue
  if [ -e "$code/$item" ] && [ ! -L "$code/$item" ]; then
    [ ! -e "$base/$item" ] || refuse "$item exists in both $code and $base"
    pending="$pending $item"
  fi
done
if [ "$code" = "$rel" ] && [ -z "$pending" ] && [ -L "$base/current" ] && [ -d "$base/.deployments" ] \
    && [ -f "$base/.env" ]; then
  say "already migrated: $base/current -> releases/r1, state in $base; nothing to do"
  exit 0
fi

# Proofs. The lock is the one the updater takes, so no deploy can run now.
lock_dir="$code/.deployments"
[ -d "$lock_dir" ] || lock_dir="$base/.deployments"
lock="$lock_dir/.deployment.lock"
[ -f "$lock" ] && [ ! -L "$lock" ] || refuse "$lock is not a regular file"
command -v flock >/dev/null 2>&1 || refuse "flock is required"
exec 9>>"$lock"
flock -n 9 || refuse "a deployment holds $lock"
say "deployment lock is free ($lock)"
python=""
for candidate in "${WUWATERM_DEPLOY_PYTHON:-}" python3 python3.13 python3.12 python3.11; do
  [ -n "$candidate" ] || continue
  resolved=$(command -v "$candidate" 2>/dev/null) || continue
  if "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    python=$resolved
    break
  fi
done
[ -n "$python" ] || refuse "no Python 3.11 or newer found"
# The checkout's own checks, run under the lock held above: an open runtime or
# full transaction (an unresolved journal, not a closed record) refuses here.
if [ "$code" = "$rel" ] && [ -d "$rel/.deployments" ]; then
  # An earlier run was interrupted after moving the checkout and before moving
  # its state, so the tools cannot find the state they check. That run passed
  # these checks, and nothing can deploy while current does not exist.
  say "resuming an interrupted run: transaction checks were made before it moved the checkout"
else
  (cd "$code" && "$python" -B scripts/runtime_update.py --check-runtime-journal) >/dev/null \
    || refuse "an unresolved runtime transaction journal needs recovery first"
  (cd "$code" && "$python" -B scripts/full_update.py check) >/dev/null \
    || refuse "an unresolved full transaction journal needs recovery first"
  say "no open runtime or full transaction"
fi
[ -z "$(GIT_OPTIONAL_LOCKS=0 command git -C "$code" status --porcelain --untracked-files=all)" ] \
  || refuse "$code has modifications or untracked files"
say "checkout is clean at $(command git -C "$code" rev-parse HEAD)"
base_dev=$(stat -c %d "$base")
for item in $pending; do
  [ "$(stat -c %d "$code/$item")" = "$base_dev" ] || refuse "$item is on another filesystem than $base; a rename would copy it"
done
[ "$(stat -c %d "$code")" = "$base_dev" ] || refuse "$code is on another filesystem than $base"
say "everything is on one filesystem; moves are renames, nothing is copied"

# The checkout moves first. From here until `current` is back, a deploy that
# starts finds no such directory and stops; running containers are unaffected.
if [ "$code" != "$rel" ]; then
  [ -d "$base/releases" ] || act "create $base/releases" "rmdir $base/releases" mkdir -p "$base/releases"
  act "move the checkout to releases/r1" "mv $rel $base/current" mv -T "$base/current" "$rel"
  code=$rel
fi
for item in $pending; do
  act "move shared state $item out of the release" "mv $base/$item $rel/$item" \
    mv -T "$rel/$item" "$base/$item"
done
for item in $state_dirs; do
  if { in_pending "$item" || [ -d "$base/$item" ]; } && [ ! -L "$rel/$item" ]; then
    act "leave $item resolvable at its old path until the containers are recreated" \
      "rm $rel/$item" ln -s "../../$item" "$rel/$item"
  fi
done
say "record the commit in $rel/.release-commit and keep the links out of git status"
if [ "$dry_run" -eq 0 ]; then
  [ -f "$rel/.release-commit" ] || command git -C "$rel" rev-parse HEAD > "$rel/.release-commit"
  grep -qx '/.release-commit' "$rel/.git/info/exclude" 2>/dev/null \
    || release_exclude "$rel" "/data" "/state" "/state-api" "/state-web"
fi
if [ ! -L "$base/current" ]; then
  act "build the current link beside it" "rm $base/.current.new" ln -s releases/r1 "$base/.current.new"
  act "point current at the release" "rm $base/current && mv $rel $base/current" \
    mv -T "$base/.current.new" "$base/current"
fi
if [ -L "$base/telegram/data" ] && [ "$(readlink "$base/telegram/data")" = "$base/current/data" ]; then
  act "build telegram's data link beside it" "rm $base/telegram/.data.new" \
    ln -s "$base/data" "$base/telegram/.data.new"
  act "point telegram/data at the shared data directory (its container keeps its mount)" \
    "ln -sfn $base/current/data $base/telegram/data" \
    mv -T "$base/telegram/.data.new" "$base/telegram/data"
fi

if [ "$dry_run" -eq 0 ]; then
  [ "$(readlink "$base/current")" = "releases/r1" ] || refuse "current was not switched"
  [ -z "$(command git -C "$rel" status --porcelain --untracked-files=all)" ] \
    || refuse "$rel is not clean after the move"
  for item in $pending .deployments .env; do
    [ -e "$base/$item" ] || refuse "$base/$item is missing after the move"
  done
  say "done: $base/current -> releases/r1; state in $base"
else
  say "dry run complete"
fi
if [ -d "$code/.git" ]; then
  left=$(GIT_OPTIONAL_LOCKS=0 command git -C "$code" status --porcelain --ignored | sed -n 's/^!! //p' \
    | grep -vxE 'data|state|state-api|state-web|\.release-commit' | tr '\n' ' ' || true)
  [ -z "$left" ] || say "ignored files that stay in $code and go when it is pruned: $left"
fi
say "next: run a full deploy (sh $base/current/deploy/vps-update.sh). It recreates the containers on the shared paths; --runtime-only refuses until then"
