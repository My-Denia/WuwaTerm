#!/bin/sh
# WUWATERM-LEGACY-ADMISSION-FINGERPRINT: v2
set -eu

mode="full"
mode_arg=""
case "${1:-}" in
  "")
    ;;
  --runtime-only)
    mode="runtime-only"
    mode_arg="--runtime-only"
    shift
    ;;
  --recover-runtime)
    mode="recover-runtime"
    shift
    ;;
  --recover-full)
    mode="recover-full"
    shift
    ;;
  *)
    echo "unknown deployment option" >&2
    exit 64
    ;;
esac
if [ "$#" -ne 0 ]; then
  echo "deployment option does not accept extra arguments" >&2
  exit 64
fi

# A recovery entry runs the tools of the release that WROTE the record: a
# failed deployment deliberately leaves /opt/wuwaterm/current on the release
# that is serving, so the `current` default would hand recovery to the wrong
# release - a different tool fingerprint and a different HEAD - even when the
# operator invoked the failed release's own script by absolute path. `$0`
# therefore binds those two modes to the checkout they were invoked from,
# which is what makes the command the updater prints work in a fresh shell.
# An explicit WUWATERM_DEPLOY_ROOT still wins, and every other mode keeps the
# documented `current` entry.
case "$mode" in
  recover-full|recover-runtime)
    if [ -n "${WUWATERM_DEPLOY_ROOT:-}" ]; then
      DEPLOY_ROOT=$WUWATERM_DEPLOY_ROOT
    else
      script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || {
        echo "refusing recovery: the invoked script's directory cannot be resolved" >&2
        exit 1
      }
      DEPLOY_ROOT=$(CDPATH= cd -- "$script_dir/.." && pwd -P) || {
        echo "refusing recovery: the invoked script's checkout cannot be resolved" >&2
        exit 1
      }
    fi
    ;;
  *)
    DEPLOY_ROOT="${WUWATERM_DEPLOY_ROOT:-/opt/wuwaterm/current}"
    ;;
esac
FAIL_STEP="${WUWATERM_FAIL_STEP:-}"

cd "$DEPLOY_ROOT"

if [ "$mode" = recover-full ] || [ "$mode" = recover-runtime ]; then
  # Recovery changes bindings. A checkout without the tools to do that is not
  # the release that wrote the record, so it is refused before anything runs.
  for required in deploy/vps-update.sh deploy/docker-compose.yml \
    scripts/full_update.py scripts/runtime_update.py; do
    if [ ! -f "$required" ]; then
      echo "refusing recovery: $DEPLOY_ROOT is not a deployment checkout" >&2
      echo "($required is missing); run the release that wrote the record:" >&2
      echo "  sh <release>/deploy/vps-update.sh --recover-full" >&2
      exit 1
    fi
  done
fi

# Release layout: <base>/releases/rN is the checkout and <base> holds the
# state every release shares (data/, state/, state-api/, .env, .deployments/,
# .deploy_commit), so a release never carries state and can be removed whole.
# The updater then runs from <base>, which keeps each state path below in its
# relative form, and resolves repository paths (scripts/, deploy/, git) against
# the release. In any other checkout both are the current directory and
# nothing differs. See deploy/release-layout.sh.
code_root="$(pwd -P)"
code_prefix=""
compose_file="deploy/docker-compose.yml"
release_name=""
case "$code_root" in
  */releases/r[1-9]*)
    . "$code_root/deploy/release-layout.sh"
    if release_name_ok "${code_root##*/}"; then
      release_name="${code_root##*/}"
      state_root="${code_root%/*/*}"
      # A release run without its shared state would find no deployment
      # pointer and start a first deployment of empty state. Refuse first.
      if [ ! -f "$state_root/.env" ]; then
        echo "refusing deployment: $code_root is a release, but $state_root" >&2
        echo "holds no .env; run deploy/migrate-layout.sh first" >&2
        exit 1
      fi
      cd "$state_root"
      code_prefix="$code_root/"
      compose_file="$code_root/deploy/docker-compose.yml"
      WUWATERM_STATE_ROOT="$state_root"
      export WUWATERM_STATE_ROOT
      git() {
        command git -C "$code_root" "$@"
      }
    fi
    ;;
esac

# These tools need Python 3.11 or newer, and a host's system python3 can be
# older (Debian 11 ships 3.9, which fails admission with a TypeError rather
# than a clear message). Choose the interpreter once, before anything runs:
# WUWATERM_DEPLOY_PYTHON when set, otherwise the first of python3, python3.13,
# python3.12 and python3.11 on PATH that is new enough. Every python3 call
# below, the exec'd helpers included, then runs that interpreter.
select_deploy_python() {
  if [ -n "${WUWATERM_DEPLOY_PYTHON:-}" ]; then
    set -- "$WUWATERM_DEPLOY_PYTHON"
  else
    set -- python3 python3.13 python3.12 python3.11
  fi
  for candidate in "$@"; do
    resolved=$(command -v "$candidate" 2>/dev/null) || continue
    if "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
        >/dev/null 2>&1; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 1
}
deploy_python=$(select_deploy_python) || {
  echo "refusing deployment: no Python 3.11 or newer found" \
    "(tried ${WUWATERM_DEPLOY_PYTHON:-python3 python3.13 python3.12 python3.11});" \
    "install one or set WUWATERM_DEPLOY_PYTHON" >&2
  exit 1
}
WUWATERM_DEPLOY_PYTHON=$deploy_python
export WUWATERM_DEPLOY_PYTHON
python3() {
  # Repository scripts resolve against the release; state paths stay relative.
  case "${1:-}" in
    scripts/*) py_script="$code_prefix$1"; shift; set -- "$py_script" "$@" ;;
    -B)
      case "${2:-}" in
        scripts/*) py_script="$code_prefix$2"; shift 2; set -- -B "$py_script" "$@" ;;
      esac
      ;;
  esac
  "$deploy_python" "$@"
}

# Read-only rejection avoids creating the lock directory when a newer
# deployment is already recorded. Repeat the check under FD 9 below.
python3 -B scripts/deployment_admission.py legacy-precheck

# One nonblocking lock covers both the legacy data deployment and the
# runtime-only transaction. File descriptor 9 stays open through exec and is
# explicitly inherited by the runtime helper's Docker children, so a parent
# death cannot release the lock while an external mutation is still running.
# Reject the metadata directory and lock path before opening either. `>>` then
# avoids truncating an existing regular lock if another process races a failed
# attempt; neither path may ever resolve through a symlink.
if [ -L .deployments ] || {
  [ -e .deployments ] && [ ! -d .deployments ]
}; then
  echo "refusing deployment: .deployments must be a regular directory" >&2
  exit 1
fi
mkdir -p .deployments
if [ -L .deployments ] || {
  [ ! -d .deployments ]
}; then
  echo "refusing deployment: .deployments must be a regular directory" >&2
  exit 1
fi
canonical_root="$(pwd -P)"
canonical_lock="$canonical_root/.deployments/.deployment.lock"
if [ -L "$canonical_lock" ] || {
  [ -e "$canonical_lock" ] && [ ! -f "$canonical_lock" ]
}; then
  echo "refusing deployment: deployment lock must be a regular file" >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "refusing deployment: flock is required for the shared deployment lock" >&2
  exit 1
fi

validate_lock_fd() {
  lock_fd_target="$(readlink /proc/self/fd/9 2>/dev/null)" || {
    echo "refusing deployment: inherited deployment lock is unreadable" >&2
    exit 1
  }
  if [ "$lock_fd_target" != "$canonical_lock" ]; then
    echo "refusing deployment: inherited deployment lock path mismatch" >&2
    exit 1
  fi
  if [ ! -f /proc/self/fd/9 ] || [ ! -f "$canonical_lock" ]; then
    echo "refusing deployment: deployment lock is not regular" >&2
    exit 1
  fi
  fd_identity="$(LC_ALL=C stat -Lc '%d:%i' /proc/self/fd/9 2>/dev/null)" || {
    echo "refusing deployment: inherited deployment lock identity is unreadable" >&2
    exit 1
  }
  path_identity="$(LC_ALL=C stat -c '%d:%i' "$canonical_lock" 2>/dev/null)" || {
    echo "refusing deployment: deployment lock identity is unreadable" >&2
    exit 1
  }
  if [ "$fd_identity" != "$path_identity" ]; then
    echo "refusing deployment: inherited deployment lock identity mismatch" >&2
    exit 1
  fi
}

if [ -L /proc/self/fd/9 ]; then
  # An inherited descriptor is already part of the caller's transaction.
  # Validate and preserve it; reopening here could detach the child from the
  # parent's lock handoff or accept a foreign descriptor.
  validate_lock_fd
else
  exec 9>>"$canonical_lock"
  validate_lock_fd
fi
if ! flock -n 9; then
  echo "deployment already in progress" >&2
  exit 75
fi

python3 -B scripts/deployment_admission.py legacy

# In a release layout an update first moves to a release at the commit it is
# about to deploy (this call may exec into that release's updater). Recovery
# must run from the release that wrote the journal, so it never does.
if [ -n "$release_name" ]; then
  case "$mode" in
    full|runtime-only) release_handoff "$code_root" ${mode_arg:+"$mode_arg"} ;;
  esac
fi

# A completed transaction and the `current` link are ONE binding: the pointer
# names a commit and `current` names the release whose code carries it. A kill
# between the commit and the link switch leaves the new deployment serving while
# `current` still names the old release, so recovery settles the link too.
#
# It settles from an OUTCOME, not from an exit status: the recovery command
# prints one line on stdout, only after it verified a binding and closed the
# record, naming the side this run actually settled (`old-verified`,
# `new-verified`) or that it found no record (`no-journal`, full mode only). An
# exit status cannot answer that - "needs no move" and "cannot be authenticated"
# both leave it 0 - and a recovery that exits 0 says nothing about which commit
# the link should name.
#
# Everything else is read here, and read strictly: the pointer (missing,
# present, unreadable) and, per release checkout, its marker, HEAD and
# `git status`. A failed read is never "no carrier", "clean" or "nothing
# deployed", and `current` is the source of a commit only when that release's
# marker AND HEAD are the commit from a clean tree. The steps run in order and
# none falls through, so a state they do not name leaves `current` where it was.

# settle_refuse LINE...: one refusal that also says where the link stays. Always
# ends with `|| return 1`; nothing below it runs for that state.
settle_refuse() {
  for settle_msg in "$@"; do echo "$settle_msg" >&2; done
  echo "  $state_root/current stays at ${settle_current:-nothing}" >&2
  return 1
}

# settle_unreadable NAME: evidence that could not be read is not an answer.
settle_unreadable() {
  settle_refuse "release: $1 cannot be read (marker, HEAD or git status);" \
    "  nothing is settled on evidence that could not be read"
}

# settle_parse LINE: 0 with $settle_kind/$settle_token only for one well-formed
# outcome line. An unknown kind, a missing or malformed commit or a second line
# is refused instead of guessed at.
settle_parse() {
  case "$1" in
    "settle-result v1 kind="*" pointer="*) ;;
    *) return 1 ;;
  esac
  settle_rest=${1#settle-result v1 kind=}
  settle_kind=${settle_rest%% pointer=*}
  settle_token=${settle_rest##* pointer=}
  case "$settle_kind" in
    old-verified|new-verified) ;;
    # A runtime record always exists when it is recovered; only the full entry
    # can report that there was nothing to settle.
    no-journal) [ "$mode" != recover-runtime ] || return 1 ;;
    *) return 1 ;;
  esac
  case "$settle_token" in
    absent|invalid) ;;
    present:*)
      settle_sha=${settle_token#present:}
      [ "${#settle_sha}" -eq 40 ] || return 1
      case "$settle_sha" in *[!0-9a-f]*) return 1 ;; esac ;;
    *) return 1 ;;
  esac
}

# release_probe BASE NAME: one release checkout's source evidence, in
# $pr_marker, $pr_head and $pr_dirty. 0 when every read succeeded, 1 when the
# directory is not a release checkout, 2 when a read failed - a failed read must
# never be mistaken for an absent marker or a clean tree.
release_probe() {
  pr_dir="$1/releases/$2"
  pr_marker=""
  pr_dirty=""
  [ -d "$pr_dir" ] && [ ! -L "$pr_dir" ] && [ -e "$pr_dir/.git" ] || return 1
  if [ -e "$pr_dir/.release-commit" ]; then
    pr_marker=$(cat "$pr_dir/.release-commit" 2>/dev/null) || return 2
  fi
  pr_head=$(command git -C "$pr_dir" rev-parse HEAD 2>/dev/null) || return 2
  pr_dirty=$(command git -C "$pr_dir" status --porcelain --untracked-files=all \
    2>/dev/null) || return 2
}

release_settle() {
  # (a) A recovery that did not finish never reaches here: the caller keeps its
  # exit status and no half transaction is settled from a guess.
  [ -n "$release_name" ] || return 0
  settle_current=""
  settle_current_name=""
  if [ -L "$state_root/current" ]; then
    settle_current=$(readlink "$state_root/current" 2>/dev/null) \
      || settle_refuse "release: $state_root/current is a link that cannot be read" \
      || return 1
    case "$settle_current" in
      releases/*) settle_current_name=${settle_current#releases/} ;;
    esac
  fi

  # (b) A record, an outcome or a pointer that cannot be read is not evidence.
  "$deploy_python" scripts/runtime_update.py --check-runtime-journal \
    >/dev/null 2>&1 || settle_refuse \
    "release: an open runtime-only transaction must be recovered first" || return 1
  settle_parse "${1:-}" || settle_refuse \
    "release: the recovery outcome is not a result this settlement can act on" \
    || return 1
  settle_present=0
  settle_pointer=""
  settle_pointer=$("$deploy_python" scripts/full_update.py pointer 2>/dev/null) || {
    settle_status=$?
    settle_pointer=""
    [ "$settle_status" -eq 10 ] || settle_refuse \
      "release: the deployment pointer cannot be read; nothing is settled from it" \
      || return 1
  }
  if [ -n "$settle_pointer" ]; then
    settle_present=1
    settle_named="present:$settle_pointer"
  else
    settle_named="absent"
  fi
  [ "$settle_token" = "$settle_named" ] || settle_refuse \
    "release: the recovery reported $settle_token, the pointer is now $settle_named;" \
    "  they disagree, so nothing is settled" || return 1

  # (c) Nothing is deployed. A certified new binding without a pointer is a
  # contradiction, not a link to move.
  if [ "$settle_present" -eq 0 ]; then
    if [ "$settle_kind" = new-verified ]; then
      settle_refuse \
        "release: the recovery certified a new binding, but no pointer exists" \
        || return 1
    fi
    if [ "$settle_kind" = old-verified ]; then
      echo "release: recovery restored the binding it recorded, which deployed" \
        "nothing; $state_root/current stays at ${settle_current:-nothing}"
    else
      echo "release: no deployment is recorded, so there is no commit to name;" \
        "$state_root/current stays at ${settle_current:-nothing}"
    fi
    return 0
  fi

  # (d)(g) The link's own answer, authenticated rather than labelled: only a
  # release whose marker AND HEAD are this commit, from a clean tree, is its
  # source. A read that fails refuses instead of counting as "not clean".
  settle_current_auth=0
  if [ -n "$settle_current_name" ]; then
    settle_rc=0
    release_probe "$state_root" "$settle_current_name" || settle_rc=$?
    [ "$settle_rc" -ne 2 ] || settle_unreadable "$settle_current" || return 1
    if [ "$settle_rc" -eq 0 ] && [ "$pr_marker" = "$settle_pointer" ] \
      && [ "$pr_head" = "$settle_pointer" ] && [ -z "$pr_dirty" ]; then
      settle_current_auth=1
    fi
  fi
  if [ "$settle_current_auth" -eq 1 ]; then
    case "$settle_kind" in
      old-verified)
        # The single-image prover cannot re-express a rollback whose surfaces
        # carried different old images, and standing still needs no proof: the
        # recovery verified that binding step by step.
        echo "release: recovery restored $settle_pointer; $settle_current is current" \
          "for it and the release link needs no change" ;;
      new-verified)
        echo "release: $settle_current is current for $settle_pointer" ;;
      *)
        # Nothing was pending and the link is already right: say exactly that.
        echo "release: no transaction is pending and $settle_current is current for" \
          "$settle_pointer; the release link needs no adjustment" ;;
    esac
    return 0
  fi

  # Does the release this recovery ran from carry the settled commit? Its marker
  # OR its HEAD saying so is a reason to prove and move, never proof on its own.
  settle_self_rc=0
  release_probe "$state_root" "$release_name" || settle_self_rc=$?
  [ "$settle_self_rc" -ne 2 ] || settle_unreadable "$release_name" || return 1
  if [ "$settle_self_rc" -eq 0 ] && { [ "$pr_marker" = "$settle_pointer" ] \
    || [ "$pr_head" = "$settle_pointer" ]; }; then
    # A restore of the OLD side puts back the binding the host already had, so
    # it certifies nothing about the candidate - not even when the pointer
    # names the candidate's own commit, as a retry of the deployed commit
    # records that commit on both sides. Switching the link here would make a
    # rollback the reason a release serves, so it is refused before the
    # published proof and never reaches release_publish.
    if [ "$settle_kind" = old-verified ]; then
      settle_refuse \
        "release: recovery restored the OLD binding $settle_pointer, so an" \
        "  old-side restore never authorizes switching $settle_current to" \
        "  $release_name, even though that release carries $settle_pointer." \
        "  Nothing is moved. The transaction record is closed, so the link is" \
        "  settled from the published binding by a repeated run:" \
        "  sh $code_root/deploy/vps-update.sh --recover-full" || return 1
    fi
    # (h) Move to this release, and only on the complete published proof plus
    # this checkout being the source of that commit. A failure here is never
    # re-read as "no release carries it".
    settle_proven=$("$deploy_python" scripts/full_update.py published) \
      || settle_proven=""
    [ -n "$settle_proven" ] || settle_refuse \
      "release: the published binding could not be proven from observation;" \
      "  recovery must run from the release that wrote the record, with its" \
      "  records intact: sh $code_root/deploy/vps-update.sh --$mode" || return 1
    [ "$settle_proven" = "$settle_pointer" ] || settle_refuse \
      "release: the published binding is $settle_proven, not the commit recovery" \
      "  settled ($settle_pointer)" || return 1
    if [ "$pr_marker" != "$settle_pointer" ] || [ "$pr_head" != "$settle_pointer" ] \
      || [ -n "$pr_dirty" ]; then
      settle_refuse \
        "release: $release_name carries $settle_pointer in its marker or at HEAD," \
        "  but that checkout is not its source: its marker, its HEAD and a clean" \
        "  tree must all say so, and no candidate release is switched to instead" \
        || return 1
    fi
    release_publish "$state_root" "$release_name" || settle_refuse \
      "release: the deployment is complete, but the link could not be switched" \
      "  to $release_name" || return 1
    echo "release: $release_name (current) for $settle_pointer"
    return 0
  fi

  # Another release carries it. A read that fails while looking refuses here; it
  # must not become "no release carries it" below.
  settle_other=""
  for settle_dir in "$state_root"/releases/r[1-9]*; do
    [ -d "$settle_dir" ] && [ ! -L "$settle_dir" ] || continue
    settle_scan=${settle_dir##*/}
    release_name_ok "$settle_scan" && [ "$settle_scan" != "$release_name" ] || continue
    settle_rc=0
    release_probe "$state_root" "$settle_scan" || settle_rc=$?
    [ "$settle_rc" -ne 2 ] || settle_unreadable "$settle_scan" || return 1
    [ "$settle_rc" -eq 0 ] || continue
    if [ "$pr_marker" = "$settle_pointer" ] || [ "$pr_head" = "$settle_pointer" ]; then
      settle_other=$settle_scan
    fi
  done
  if [ "$settle_other" = "$settle_current_name" ] && [ -n "$settle_other" ]; then
    # (f) The link is the source entry for this commit but does not hold it.
    settle_refuse \
      "release: $settle_pointer is deployed and $settle_current is the source" \
      "  entry for it, but that checkout is not authenticated for it" || return 1
  fi
  if [ -n "$settle_other" ]; then
    # (i) This entry is not the one to settle from.
    settle_refuse \
      "release: the deployment pointer names $settle_pointer, which $release_name" \
      "  does not carry; run recovery from $settle_other so that release settles" \
      "  the link:" \
      "  sh $state_root/releases/$settle_other/deploy/vps-update.sh --recover-full" \
      || return 1
  fi

  # (e)(j)(k) No release's source corresponds to the settled commit.
  case "$settle_kind" in
    old-verified)
      # A migrated host records the in-place HEAD as its release's commit while
      # the pointer keeps naming what that checkout deployed, so a rollback
      # restores a commit no release carries. Nothing can be moved to it, and no
      # checkout is invented for it.
      echo "release: recovery restored $settle_pointer, which no release's source" \
        "carries: a host migrated from the in-place checkout records its own HEAD" \
        "as its release's commit and keeps the deployed commit in the pointer." \
        "$state_root/current stays at ${settle_current:-nothing} and is not" \
        "switched on a guess." >&2
      return 0 ;;
    new-verified)
      settle_refuse \
        "release: the recovery certified $settle_pointer, but no release's source" \
        "  carries it" || return 1 ;;
    *)
      echo "release: no transaction is pending and no release's source corresponds" \
        "to $settle_pointer; no source entry was found to adjust, and" \
        "$state_root/current stays at ${settle_current:-nothing}" >&2
      return 0 ;;
  esac
}

case "$mode" in
  runtime-only)
    if [ -n "$release_name" ]; then
      # Not exec'd: the release is switched and pruned only after this succeeds.
      cd "$code_root"
      "$deploy_python" scripts/runtime_update.py --runtime-only
      release_finish "$state_root" "$release_name"
      exit 0
    fi
    exec "$deploy_python" scripts/runtime_update.py --runtime-only
    ;;
  recover-runtime)
    cd "$code_root"
    settle_out=$("$deploy_python" scripts/runtime_update.py --recover-runtime \
      --settle-result)
    release_settle "$settle_out"
    exit 0
    ;;
  recover-full)
    cd "$code_root"
    settle_out=$("$deploy_python" scripts/full_update.py recover --settle-result)
    release_settle "$settle_out"
    exit 0
    ;;
esac

# A full data deployment must never run across an unresolved runtime-only
# transaction, nor across an unresolved FULL transaction journal left by an
# interrupted full-data update (kill/power-loss: the EXIT trap never ran and
# the database/pointer pair may be inconsistent). Recovery for the latter is
# `sh deploy/vps-update.sh --recover-full`; an unresolved journal refuses
# every new deployment until it is recovered.
if ! python3 scripts/runtime_update.py --check-runtime-journal >/dev/null 2>&1; then
  echo "refusing full deployment: a transaction journal requires recovery" >&2
  exit 1
fi
if ! python3 scripts/full_update.py check >/dev/null 2>&1; then
  echo "refusing full deployment: a transaction journal requires recovery" >&2
  exit 1
fi

compose() {
  SOURCE_COMMIT="${SOURCE_COMMIT:-unknown}" \
    WUWATERM_RUNTIME_IMAGE="${WUWATERM_RUNTIME_IMAGE:-wuwaterm-runtime:local}" \
    docker compose -f "$compose_file" "$@"
}

fail_if() {
  if [ "$FAIL_STEP" = "$1" ]; then
    echo "injected deployment failure at step: $1" >&2
    return 97
  fi
}

# Loaded before anything is changed, so a missing file refuses here rather than
# after the deployment has committed.
. "$code_root/deploy/backup-retention.sh"

env_mode="$(stat -c '%a' .env)"
if [ "$env_mode" != "600" ]; then
  echo "refusing to use $canonical_root/.env with mode $env_mode; expected 600" >&2
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "refusing to deploy a source checkout with modifications or untracked files" >&2
  exit 1
fi
git fetch --quiet origin main:refs/remotes/origin/main
source_commit="$(git rev-parse HEAD)"
origin_main="$(git rev-parse refs/remotes/origin/main)"
if [ "$source_commit" != "$origin_main" ]; then
  echo "refusing deployment: HEAD does not match freshly fetched origin/main" >&2
  exit 1
fi
case "$source_commit" in
  *[!0-9a-f]*|'')
    echo "refusing invalid source commit: $source_commit" >&2
    exit 1
    ;;
esac
if [ "${#source_commit}" -ne 40 ]; then
  echo "refusing source commit that is not a full SHA: $source_commit" >&2
  exit 1
fi

# The device store's default moved from state/api/ - inside the directory the
# bot mounts read-write in full - to the sibling state-api/. The library
# refuses to create an empty store next to an old one, but the API container
# only ever sees state-api/, so inside the container the old path does not
# exist and that guard cannot fire. The host is the only place that can see
# both, which makes this the only place the upgrade can be caught.
# The presence of the old file is enough to refuse, whether or not a new one
# exists: an earlier attempt may already have created an empty store at the new
# path, and then having both is precisely the state where nobody can tell which
# one holds the live verifiers.
if [ -f "state/api/devices.db" ]; then
  echo "refusing deployment: a device store still exists at" >&2
  echo "state/api/devices.db, the path the API used before its state" >&2
  echo "directory moved out of the bot's writable mount. The API now reads" >&2
  echo "state-api/devices.db. Move that file, with any -wal and -shm" >&2
  echo "sidecars, to state-api/ if it holds the live verifiers, or delete it" >&2
  echo "if state-api/devices.db already does. Deploying with both in place" >&2
  echo "could start an API that refuses every device ever registered." >&2
  exit 1
fi

deployment_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
deployment_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
candidate_path="data/candidates/terms.$deployment_id.db"
candidate_container_path="/app/data/candidates/terms.$deployment_id.db"
db_path="data/terms.db"
backup_dir="data/deployment-backups"
manifest_dir=".deployments"
manifest_path="$manifest_dir/$source_commit.json"
pointer_path=".deploy_commit"
new_image_ref="wuwaterm-runtime:$source_commit"
rollback_image_ref="wuwaterm-runtime:rollback-$deployment_id"
# Each surface gets its own rollback tag. They normally run the same image, but
# a host recovered by hand can have them on different ones, and restoring both
# from a single tag would move a surface onto an image it was never running.
rollback_api_image_ref="wuwaterm-runtime:rollback-api-$deployment_id"

mkdir -p data/candidates "$backup_dir" "$manifest_dir" state
if [ -e "$candidate_path" ]; then
  echo "refusing to overwrite existing candidate: $candidate_path" >&2
  exit 1
fi

kb_path="$(python3 scripts/full_update.py resolve-kb)"
kb_enabled=0
kb_candidate_path=""
kb_candidate_container_path=""
old_kb_present=0
old_kb_hash=""
kb_backup="none"
if [ -n "$kb_path" ]; then
  kb_enabled=1
  kb_candidate_path="data/candidates/knowledge.$deployment_id.db"
  kb_candidate_container_path="/app/data/candidates/knowledge.$deployment_id.db"
  if [ -e "$kb_candidate_path" ]; then
    echo "refusing to overwrite existing knowledge candidate: $kb_candidate_path" >&2
    exit 1
  fi
  if [ -f "$kb_path" ]; then
    old_kb_present=1
  fi
fi

old_pointer_present=0
old_pointer=""
pointer_backup=""
if [ -e "$pointer_path" ]; then
  old_pointer="$(tr -d '\n' < "$pointer_path")"
  case "$old_pointer" in
    *[!0-9a-f]*|'')
      echo "refusing invalid existing deployment pointer" >&2
      exit 1
      ;;
  esac
  if [ "${#old_pointer}" -ne 40 ]; then
    echo "refusing existing deployment pointer that is not a full SHA" >&2
    exit 1
  fi
  old_pointer_present=1
  pointer_backup="$backup_dir/$deployment_id.deploy_commit"
  cp -p "$pointer_path" "$pointer_backup"
fi

old_db_present=0
db_backup=""
if [ -f "$db_path" ]; then
  old_db_present=1
fi

# Each surface is recorded separately, so a host on a pre-api deployment
# (empty value) is handled without pretending the container ever existed.
# Existence is not enough either: a container left stopped by an earlier failed
# upgrade, or stopped deliberately by the operator, still answers
# `docker inspect`. Rollback restores the state this deployment found, so what
# it needs to know is whether each surface was RUNNING.
#
# A failed running-state inspect is not "known stopped". Proven absence is the
# daemon's own "No such object" answer. Anything else is retried, and if it
# stays unconfirmed the update refuses here, before prepare or stop, and does
# not substitute false.
capture_running_state() {
  cap_name="$1"
  cap_attempt=0
  while [ "$cap_attempt" -lt 3 ]; do
    cap_attempt=$((cap_attempt + 1))
    cap_err="/tmp/wuwaterm-running-inspect.$$"
    set +e
    cap_observed="$(docker inspect --format '{{.State.Running}}' "$cap_name" 2>"$cap_err")"
    cap_status="$?"
    set -e
    if [ "$cap_status" -eq 0 ]; then
      rm -f "$cap_err"
      case "$cap_observed" in
        true|false)
          printf '%s' "$cap_observed"
          return 0
          ;;
      esac
      echo "refusing deployment: $cap_name running state is not exactly true or false" >&2
      exit 1
    fi
    cap_err_text="$(cat "$cap_err" 2>/dev/null || true)"
    rm -f "$cap_err"
    case "$cap_err_text" in
      *"No such object"*)
        printf '%s' false
        return 0
        ;;
    esac
  done
  echo "refusing deployment: $cap_name running state could not be confirmed; not recording it as stopped" >&2
  exit 1
}

old_bot_running="$(capture_running_state wuwaterm-bot)"
old_image_id="$(docker inspect --format '{{.Image}}' wuwaterm-bot 2>/dev/null || true)"
if [ "$old_bot_running" = "true" ]; then
  echo "refusing deployment: legacy wuwaterm-bot container is running; stop it" >&2
  echo "and migrate to standalone WuwaTerm-Telegram" >&2
  echo "(https://github.com/My-Denia/WuwaTerm-Telegram) before running this updater." >&2
  exit 1
fi
if [ -n "$old_image_id" ]; then
  echo "note: stopped legacy wuwaterm-bot container detected; leaving it untouched" >&2
  echo "while upgrading wuwaterm-api (you can remove it manually with: docker rm wuwaterm-bot)" >&2
fi
old_api_image_id="$(docker inspect --format '{{.Image}}' wuwaterm-api 2>/dev/null || true)"
old_api_running="$(capture_running_state wuwaterm-api)"
old_api_ref="$(docker inspect --format '{{.Config.Image}}' wuwaterm-api 2>/dev/null || true)"

# A deployment publishes code and data for wuwaterm-api; the legacy in-Core
# bot is no longer managed here. Every refusal below happens before the
# transaction is armed, before the builder runs and before any database or
# service is touched.
if [ "$old_pointer_present" -eq 1 ] && [ "$old_api_running" != "true" ]; then
  echo "refusing deployment: wuwaterm-api is not running, but $pointer_path" >&2
  echo "records a deployment. A host with no serving surface cannot be updated" >&2
  echo "in place; start wuwaterm-api, then deploy again." >&2
  exit 1
fi
if [ "$old_pointer_present" -eq 1 ]; then
  target_api_running="$old_api_running"
else
  # First deployment from a host with no recorded pointer: wuwaterm-api serves.
  target_api_running=true
fi
if [ -n "$old_api_image_id" ]; then
  docker image tag "$old_api_image_id" "$rollback_api_image_ref"
fi

# The builder tag is intentionally local and mutable, so rebuild it from the
# verified clean source checkout before any data command. Reusing a builder
# left by an older deployment could refresh/build/verify with the wrong pin or
# schema even though the runtime image is built from the new source.
compose build wuwaterm-builder
fail_if builder_image

# The builder has no runtime env_file. This smoke checks secret presence only
# through exit status and never prints values.
compose run --rm --entrypoint sh wuwaterm-builder -c \
  'test -z "${TELEGRAM_BOT_TOKEN+x}${TELEGRAM_TEST_CHAT_ID+x}${OWNER_USER_ID+x}${OPENAI_API_KEY+x}${OPENAI_BASE_URL+x}${WUWATERM_OPENAI_API_KEY+x}${WUWATERM_OPENAI_BASE_URL+x}${WUWATERM_OPENAI_MODEL+x}${WUWATERM_REDACTION_SECRET+x}"'

compose run --rm wuwaterm-builder refresh-data
fail_if refresh
compose run --rm -e "WUWATERM_DB_PATH=$candidate_container_path" \
  wuwaterm-builder build-db --atomic
fail_if build
compose run --rm -e "WUWATERM_DB_PATH=$candidate_container_path" \
  wuwaterm-builder verify-db
compose run --rm wuwaterm-builder python scripts/verify_exact_hits.py \
  "$candidate_container_path" --sample-size 500
compose run --rm wuwaterm-builder python scripts/verify_seed_terms.py \
  "$candidate_container_path" \
  --discrepancies "/app/data/candidates/seed-discrepancies.$deployment_id.json"
if [ "$old_db_present" -eq 1 ]; then
  compose run --rm wuwaterm-builder python scripts/diff_terms_db.py \
    /app/data/terms.db "$candidate_container_path" --json \
    > "$backup_dir/db-diff.$deployment_id.json"
fi
if [ "$kb_enabled" -eq 1 ]; then
  compose run --rm \
    -e "WUWATERM_DB_PATH=$candidate_container_path" \
    -e "WUWATERM_KNOWLEDGE_DB_PATH=$kb_candidate_container_path" \
    wuwaterm-builder build-kb --atomic
  fail_if build_kb
  compose run --rm \
    -e "WUWATERM_DB_PATH=$candidate_container_path" \
    -e "WUWATERM_KNOWLEDGE_DB_PATH=$kb_candidate_container_path" \
    wuwaterm-builder verify-kb
  fail_if verify_kb
fi
fail_if verify

SOURCE_COMMIT="$source_commit" WUWATERM_RUNTIME_IMAGE="$new_image_ref" \
  compose build wuwaterm-api
image_id="$(docker image inspect --format '{{.Id}}' "$new_image_ref")"
image_digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$new_image_ref")"
image_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$new_image_ref")"
if [ -z "$image_id" ] || [ -z "$image_digest" ] || [ "$image_revision" != "$source_commit" ]; then
  echo "runtime image identity or revision label validation failed" >&2
  exit 1
fi
fail_if image

if [ "$old_db_present" -eq 1 ]; then
  old_db_hash="$(sha256sum "$db_path" | awk '{print $1}')"
  db_backup="$backup_dir/terms.$deployment_id.$old_db_hash.db"
  db_backup_tmp="$db_path.backup.$deployment_id"
  if [ -e "$db_backup" ] || [ -e "$db_backup_tmp" ]; then
    echo "refusing to overwrite an existing database backup" >&2
    exit 1
  fi
  cp -p "$db_path" "$db_backup_tmp"
  # The source temp lives in data/ while the destination lives in the backup
  # directory. durable-replace therefore fsyncs the backup file, the backup
  # directory, and data/ (which also makes a newly created backup directory
  # durable) before the live database can be promoted.
  python3 scripts/deployment_manifest.py durable-replace \
    --source "$db_backup_tmp" --destination "$db_backup"
  if [ "$(sha256sum "$db_backup" | awk '{print $1}')" != "$old_db_hash" ]; then
    echo "database backup verification failed" >&2
    exit 1
  fi
else
  db_backup="none"
fi
if [ "$kb_enabled" -eq 1 ]; then
  if [ "$old_kb_present" -eq 1 ]; then
    old_kb_hash="$(sha256sum "$kb_path" | awk '{print $1}')"
    kb_backup="$backup_dir/knowledge.$deployment_id.$old_kb_hash.db"
    kb_backup_tmp="$kb_path.backup.$deployment_id"
    if [ -e "$kb_backup" ] || [ -e "$kb_backup_tmp" ]; then
      echo "refusing to overwrite an existing knowledge database backup" >&2
      exit 1
    fi
    cp -p "$kb_path" "$kb_backup_tmp"
    python3 scripts/deployment_manifest.py durable-replace \
      --source "$kb_backup_tmp" --destination "$kb_backup"
    if [ "$(sha256sum "$kb_backup" | awk '{print $1}')" != "$old_kb_hash" ]; then
      echo "knowledge database backup verification failed" >&2
      exit 1
    fi
  else
    kb_backup="none"
  fi
fi
fail_if backup

runtime_stopped=0
db_promoted=0
pointer_published=0

# --- full-transaction journal (scripts/full_update.py) -------------------
# Durable phase record so an interrupted full update (SIGKILL, power loss:
# the EXIT rollback trap never ran) can be recovered deterministically from
# the recorded preimages. The module writes it atomically BEFORE the first
# serving-binding mutation; it is dropped only through the module's
# observed-binding verifiers (`confirm` on success, `rollback-clear` in the
# EXIT trap). Any uncertain binding keeps the record for `--recover-full`.

# A rollback reference is mutable. Resolve it immediately before each restart,
# just as explicit recovery does; an unknown or unavailable image stays down.
rollback_image_matches() {
  rollback_resolved="$(docker image inspect --format '{{.Id}}' "$1" 2>/dev/null)" || return 1
  [ "$rollback_resolved" = "$2" ]
}

rollback_on_failure() {
  status=$?
  trap - EXIT
  if [ "$status" -eq 0 ]; then
    exit 0
  fi

  echo "deployment failed; restoring previous database, image and pointer" >&2
  rollback_failed=0
  db_binding_restored=1

  # Nothing may serve while the binding is being restored. If the replacement
  # containers were started, they are running NEW code against a database that
  # is about to be rolled back, which is the mixed binding this rollback exists
  # to prevent. Both surfaces therefore go down before the database is touched;
  # only what was running before the deployment comes back afterwards.
  containers_quiesced=1
  if [ "$runtime_stopped" -eq 1 ]; then
    if ! compose stop wuwaterm-api >/dev/null 2>&1; then
      echo "warning: replacement containers could not be stopped before rollback" >&2
      rollback_failed=1
      containers_quiesced=0
    fi
  fi

  # If a container may still be serving, rolling the database back underneath
  # it would CREATE the mixed binding instead of preventing it. Whatever the
  # database and pointer currently are, they match the code that is running, so
  # they are left exactly as they are and the operator is told to intervene.
  if [ "$containers_quiesced" -eq 0 ]; then
    echo "warning: a serving container could not be stopped; the database and" >&2
    echo "commit pointer are left exactly as they are and nothing is" >&2
    echo "restarted. Stop both containers by hand, then roll back." >&2
    echo "warning: rollback completed with errors; manual recovery is required" >&2
    echo "warning: the full-transaction journal is KEPT for recovery:" >&2
    echo "  sh $code_root/deploy/vps-update.sh --recover-full" >&2
    exit "$status"
  fi

  # The database returns through the SAME verified restoration explicit
  # recovery uses (`full_update.py restore-db`): the recorded backup and
  # the copied bytes are hashed BEFORE the live file can be replaced, so a
  # backup modified after preparation leaves the known live database
  # untouched. The journal keeps the record for `--recover-full` whenever
  # this fails.
  if [ "$db_promoted" -eq 1 ]; then
    if ! python3 scripts/full_update.py restore-db; then
      echo "warning: verified database restoration failed or its durability could not be confirmed" >&2
      rollback_failed=1
      db_binding_restored=0
    fi
    if [ "$old_db_present" -eq 1 ]; then
      if [ ! -f "$db_path" ] || \
        [ "$(sha256sum "$db_path" | awk '{print $1}')" != "$old_db_hash" ]; then
        echo "warning: old database content was not restored" >&2
        rollback_failed=1
        db_binding_restored=0
      fi
    else
      if [ -e "$db_path" ]; then
        echo "warning: promoted database still exists after rollback" >&2
        rollback_failed=1
        db_binding_restored=0
      fi
    fi
    if [ "$kb_enabled" -eq 1 ]; then
      if [ "$old_kb_present" -eq 1 ]; then
        if [ ! -f "$kb_path" ] || \
          [ "$(sha256sum "$kb_path" | awk '{print $1}')" != "$old_kb_hash" ]; then
          echo "warning: old knowledge database content was not restored" >&2
          rollback_failed=1
          db_binding_restored=0
        fi
      else
        if [ -e "$kb_path" ] || [ -L "$kb_path" ]; then
          echo "warning: promoted knowledge database still exists after rollback" >&2
          rollback_failed=1
          db_binding_restored=0
        fi
      fi
    fi
  fi

  if [ "$pointer_published" -eq 1 ]; then
    if [ "$old_pointer_present" -eq 1 ]; then
      if ! python3 scripts/deployment_manifest.py publish-pointer \
        --path "$pointer_path" --source-commit "$old_pointer"; then
        echo "warning: restored pointer durability could not be confirmed" >&2
        rollback_failed=1
      fi
    else
      if ! python3 scripts/deployment_manifest.py durable-remove \
        --path "$pointer_path"; then
        echo "warning: pointer removal durability could not be confirmed" >&2
        rollback_failed=1
      fi
    fi
  fi

  # Everything the deployment started is already stopped by this point, so this
  # section only decides what may come BACK. Anything not started here stays
  # down, which is the correct outcome for a host that was not running it.
  if [ "$runtime_stopped" -eq 1 ]; then
    if [ "$db_binding_restored" -eq 1 ]; then
      # Only bring the api surface back if this host was actually RUNNING it
      # when the deployment started. On a first upgrade there is nothing to
      # restore, and a container that existed but was stopped (an earlier
      # failed upgrade, or an operator decision) must not be started here.
      if [ "$old_api_running" = "true" ] && [ -n "$old_api_image_id" ]; then
        if ! rollback_image_matches "$rollback_api_image_ref" "$old_api_image_id"; then
          echo "warning: old api rollback image binding changed; refusing restart" >&2
          rollback_failed=1
        elif ! WUWATERM_RUNTIME_IMAGE="$rollback_api_image_ref" \
          compose up -d --no-build --force-recreate wuwaterm-api; then
          echo "warning: old api image could not be restarted" >&2
          rollback_failed=1
        fi
      elif [ -n "$old_api_image_id" ]; then
        if ! rollback_image_matches "$rollback_api_image_ref" "$old_api_image_id"; then
          echo "warning: old stopped api rollback image binding changed; refusing restore" >&2
          rollback_failed=1
        elif ! WUWATERM_RUNTIME_IMAGE="$rollback_api_image_ref" \
          compose create --no-build --force-recreate --pull never wuwaterm-api; then
          echo "warning: stopped api object could not be restored" >&2
          rollback_failed=1
        fi
      fi
    else
      echo "warning: refusing to restart old runtime without its database" >&2
      rollback_failed=1
    fi
  fi

  # Operational errors above still prevent clearance. Otherwise the shared
  # observed-binding verifier below is the sole certification authority; it
  # uses the manifest's own image and every recorded surface, not bot-first.
  if [ "$rollback_failed" -ne 0 ]; then
    echo "warning: rollback completed with errors; manual recovery is required" >&2
    echo "warning: the full-transaction journal is KEPT for recovery:" >&2
    echo "  sh $code_root/deploy/vps-update.sh --recover-full" >&2
  else
    # The rollback restored the previous binding completely, so the armed
    # journal no longer describes an open transaction. It may be dropped
    # ONLY through the module's observed old-binding verifier - the same
    # check explicit recovery runs - so a rollback that silently restored
    # the wrong thing keeps the record instead of certifying it. Isolated
    # fault injection tests take this same path: nothing here says
    # production was damaged — the transaction rolled back to the previous
    # binding.
    if ! python3 scripts/full_update.py rollback-clear; then
      echo "warning: the restored binding failed observed verification; manual recovery is required" >&2
      echo "warning: the full-transaction journal is KEPT for recovery:" >&2
      echo "  sh $code_root/deploy/vps-update.sh --recover-full" >&2
    fi
  fi
  exit "$status"
}
trap rollback_on_failure EXIT

# Candidate DB and immutable image are both verified before this stop.
# Both surfaces mount the same terminology database read-only, so both must
# be down while it is replaced. The old DB snapshot is durable and every
# preimage is recorded BEFORE the first serving-binding mutation, exactly as
# the journal's design requires.
if [ "$kb_enabled" -eq 1 ]; then
  new_kb_hash="$(sha256sum "$kb_candidate_path" | awk '{print $1}')"
  python3 scripts/full_update.py prepare \
    --transaction-id "$deployment_id" \
    --source-commit "$source_commit" \
    --old-pointer-present "$old_pointer_present" \
    --old-pointer "$old_pointer" \
    --db-backup "$db_backup" \
    --old-db-sha "${old_db_hash:-none}" \
    --candidate-path "$candidate_path" \
    --kb-path "$kb_path" \
    --kb-backup "$kb_backup" \
    --old-kb-sha "${old_kb_hash:-none}" \
    --kb-candidate-path "$kb_candidate_path" \
    --old-image-id none \
    --old-api-image-id "${old_api_image_id:-none}" \
    --old-ref none \
    --old-api-ref "${old_api_ref:-none}" \
    --old-bot-running false \
    --old-api-running "$old_api_running" \
    --rollback-image-ref "$rollback_image_ref" \
    --rollback-api-image-ref "$rollback_api_image_ref" \
    --target-image-ref "$new_image_ref" \
    --target-image-id "$image_id" \
    --target-image-digest "$image_digest" \
    --target-image-revision "$image_revision"
else
  python3 scripts/full_update.py prepare \
    --transaction-id "$deployment_id" \
    --source-commit "$source_commit" \
    --old-pointer-present "$old_pointer_present" \
    --old-pointer "$old_pointer" \
    --db-backup "$db_backup" \
    --old-db-sha "${old_db_hash:-none}" \
    --candidate-path "$candidate_path" \
    --old-image-id none \
    --old-api-image-id "${old_api_image_id:-none}" \
    --old-ref none \
    --old-api-ref "${old_api_ref:-none}" \
    --old-bot-running false \
    --old-api-running "$old_api_running" \
    --rollback-image-ref "$rollback_image_ref" \
    --rollback-api-image-ref "$rollback_api_image_ref" \
    --target-image-ref "$new_image_ref" \
    --target-image-id "$image_id" \
    --target-image-digest "$image_digest" \
    --target-image-revision "$image_revision"
fi
# The transition is recorded BEFORE the command, for the same reason
# db_promoted is: a stop that fails partway must still arm rollback.
runtime_stopped=1
compose stop wuwaterm-api
db_promoted=1
python3 scripts/deployment_manifest.py durable-replace \
  --source "$candidate_path" --destination "$db_path"
if [ "$kb_enabled" -eq 1 ]; then
  mkdir -p "$(dirname "$kb_path")"
  python3 scripts/deployment_manifest.py durable-replace \
    --source "$kb_candidate_path" --destination "$kb_path"
fi
python3 scripts/full_update.py phase promoted

# Freeze and validate the legacy state only while the old runtime is stopped.
for state_file in chat_settings.json channel_replies.json; do
  if [ -f "data/$state_file" ] && [ -e "state/$state_file" ]; then
    python3 scripts/validate_state_file.py "state/$state_file" "$state_file" >/dev/null || {
      echo "refusing invalid state/$state_file while data/$state_file exists" >&2
      exit 1
    }
  fi
  if [ -f "data/$state_file" ] && [ ! -e "state/$state_file" ]; then
    tmp="state/.$state_file.migrate.$$"
    cp -p "data/$state_file" "$tmp"
    chmod 600 "$tmp"
    python3 scripts/validate_state_file.py "$tmp" "$state_file" >/dev/null || {
      echo "refusing to migrate invalid data/$state_file" >&2
      rm -f "$tmp"
      exit 1
    }
    if ! ln "$tmp" "state/$state_file" 2>/dev/null; then
      python3 scripts/validate_state_file.py "state/$state_file" "$state_file" >/dev/null || {
        echo "refusing invalid state/$state_file" >&2
        rm -f "$tmp"
        exit 1
      }
    fi
    rm -f "$tmp"
  fi
done
fail_if state

# Start wuwaterm-api on the validated image; the retired legacy wuwaterm-bot
# container is never created, stopped or started by this updater.
if [ "$target_api_running" = "true" ]; then
  WUWATERM_RUNTIME_IMAGE="$new_image_ref" compose up -d --no-build \
    --force-recreate wuwaterm-api
elif [ -n "$old_api_image_id" ]; then
  WUWATERM_RUNTIME_IMAGE="$new_image_ref" compose create --no-build \
    --force-recreate --pull never wuwaterm-api
fi
python3 scripts/full_update.py phase started
fail_if start
# The api surface binds loopback only, so its smoke runs inside its own
# container and needs nothing exposed to the host. Readiness, not liveness:
# /healthz answers while the terminology database is missing or mounted at the
# wrong path, and publishing a deployment in that state is exactly what this
# check exists to prevent.
if [ "$target_api_running" = "true" ]; then
  WUWATERM_RUNTIME_IMAGE="$new_image_ref" compose exec -T wuwaterm-api \
    python -c "import os, sys, time, urllib.error, urllib.request; port = os.environ.get('WUWATERM_API_PORT', '8788'); url = 'http://127.0.0.1:' + port + '/readyz'; deadline = time.monotonic() + 60.0; last = 'no attempt'
while time.monotonic() < deadline:
    try:
        response = urllib.request.urlopen(url, timeout=5)
    except (urllib.error.URLError, OSError) as exc:
        last = type(exc).__name__
        time.sleep(1.0)
        continue
    if response.status == 200:
        sys.exit(0)
    last = 'status ' + str(response.status)
    time.sleep(1.0)
sys.exit('api readiness never reported ok: ' + last)"
fi
if [ "$kb_enabled" -eq 1 ]; then
  live_kb_hash="$(sha256sum "$kb_path" | awk '{print $1}')"
  if [ "$live_kb_hash" != "$new_kb_hash" ]; then
    echo "live knowledge database hash mismatch after promotion" >&2
    exit 1
  fi
  python3 scripts/verify_knowledge_db.py "$kb_path" --terms-db "$db_path" >/dev/null
fi
fail_if smoke

# Read back what the host actually has before anything is certified. A
# surface this deployment left stopped must be stopped AND on the validated
# image; one it left absent must be absent; a running one must run exactly the
# validated image id. Anything else is an unverified binding: say so and let
# the EXIT trap roll back instead of publishing it.
surface_state() {
  ss_name="$1"
  ss_expected="$2"
  ss_err="/tmp/wuwaterm-surface-inspect.$$"
  set +e
  ss_running="$(docker inspect --format '{{.State.Running}}' "$ss_name" 2>"$ss_err")"
  ss_status="$?"
  ss_error="$(cat "$ss_err" 2>/dev/null || true)"
  rm -f "$ss_err"
  set -e
  if [ "$ss_status" -ne 0 ]; then
    case "$ss_error" in
      *"No such object"*)
        if [ "$ss_expected" = "absent" ]; then
          printf '%s' absent
          return 0
        fi
        echo "refusing to certify: $ss_name is absent; this deployment" >&2
        echo "expected it $ss_expected" >&2
        exit 1
        ;;
    esac
    echo "refusing to certify: $ss_name state could not be read" >&2
    exit 1
  fi
  ss_image="$(docker inspect --format '{{.Image}}' "$ss_name")"
  if [ "$ss_image" != "$image_id" ]; then
    echo "refusing to certify: $ss_name does not carry the validated image" >&2
    exit 1
  fi
  case "$ss_expected:$ss_running" in
    running:true) printf '%s' running ;;
    stopped:false) printf '%s' stopped ;;
    *)
      echo "refusing to certify: $ss_name is running=$ss_running; this" >&2
      echo "deployment expected it $ss_expected" >&2
      exit 1
      ;;
  esac
}

api_expected=absent
if [ "$target_api_running" = "true" ]; then
  api_expected=running
elif [ -n "$old_api_image_id" ]; then
  api_expected=stopped
fi
api_surface_state="$(surface_state wuwaterm-api "$api_expected")"

if [ "$kb_enabled" -eq 1 ]; then
  python3 scripts/deployment_manifest.py create \
    --path "$manifest_path" \
    --source-commit "$source_commit" \
    --image-ref "$new_image_ref" \
    --image-id "$image_id" \
    --image-digest "$image_digest" \
    --image-revision "$image_revision" \
    --db "$db_path" \
    --backup-path "$db_backup" \
    --kb "$kb_path" \
    --kb-display-path "$kb_path" \
    --kb-backup-path "$kb_backup" \
    --deployment-utc "$deployment_utc"
else
  python3 scripts/deployment_manifest.py create \
    --path "$manifest_path" \
    --source-commit "$source_commit" \
    --image-ref "$new_image_ref" \
    --image-id "$image_id" \
    --image-digest "$image_digest" \
    --image-revision "$image_revision" \
    --db "$db_path" \
    --backup-path "$db_backup" \
    --deployment-utc "$deployment_utc"
fi
fail_if manifest
# The image id certified here is the one every running surface was just read
# back on; `$image_id` IS that value, and the read-back above proved it.
if [ "$kb_enabled" -eq 1 ]; then
  python3 scripts/deployment_manifest.py verify \
    --path "$manifest_path" \
    --source-commit "$source_commit" \
    --image-id "$image_id" \
    --image-digest "$image_digest" \
    --db "$db_path" \
    --kb "$kb_path"
else
  python3 scripts/deployment_manifest.py verify \
    --path "$manifest_path" \
    --source-commit "$source_commit" \
    --image-id "$image_id" \
    --image-digest "$image_digest" \
    --db "$db_path"
fi
python3 scripts/full_update.py phase published

pointer_published=1
python3 scripts/deployment_manifest.py publish-pointer \
  --path "$pointer_path" --source-commit "$source_commit"
python3 scripts/deployment_manifest.py verify-pointer \
  --path "$pointer_path" --source-commit "$source_commit"
python3 scripts/full_update.py phase committed
fail_if pointer

db_hash="$(sha256sum "$db_path" | awk '{print $1}')"
kb_hash=""
if [ "$kb_enabled" -eq 1 ]; then
  kb_hash="$(sha256sum "$kb_path" | awk '{print $1}')"
fi
runtime_stopped=0
trap - EXIT
# Success drops the durable record only through the module's observed
# new-binding verifier: pointer, database hash, manifest and BOTH surfaces'
# image ids/refs/running states must match the new binding before the
# journal (and the now-consumed confined candidate) is removed.
python3 scripts/full_update.py confirm

# The deployment is committed, so retention is housekeeping and never fails it.
# The newest 5 snapshots stay, and this deployment's own is always one of them.
backup_retention_prune "$backup_dir" 5 "$deployment_id" ||
  echo "warning: backup retention did not finish; older snapshots were left in place" >&2

echo "deployment source commit: $source_commit"
echo "deployment image id: $image_id"
echo "deployment api surface: $api_surface_state"
if [ -n "$old_image_id" ]; then
  echo "deployment legacy bot: stopped (untouched; remove manually with: docker rm wuwaterm-bot)"
fi
echo "deployment image digest: $image_digest"
echo "deployment database sha256: $db_hash"
if [ "$kb_enabled" -eq 1 ]; then
  echo "deployment knowledge database sha256: $kb_hash"
fi
echo "deployment manifest: $manifest_path"
echo "deployment timestamp: $deployment_utc"
if [ -n "$release_name" ]; then
  release_finish "$state_root" "$release_name"
fi
