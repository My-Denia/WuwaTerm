# Release layout helpers, sourced by deploy/vps-update.sh and
# deploy/migrate-layout.sh. POSIX sh; the host is Linux, so GNU `mv -T` is used.
#
#   <base>/releases/rN   one checkout per release (N counts up, never reused)
#   <base>/current       symlink to releases/rN, switched only after the health check
#   <base>/data, state, state-api, .env, .deployments, .deploy_commit
#                        mutable state, shared by every release and never pruned
#
# Each release records its own commit in .release-commit and the release it
# replaced in .release-previous. Retention keeps `current` and that previous
# release, so a failed attempt is never mistaken for the one to roll back to.

release_name_ok() {
  case "$1" in
    r[1-9]*[!0-9]*) return 1 ;;
    r[1-9]*) return 0 ;;
  esac
  return 1
}

# Highest rN under $1, plus one.
release_next_number() {
  rn_max=0
  for rn_dir in "$1"/r[1-9]*; do
    [ -d "$rn_dir" ] && [ ! -L "$rn_dir" ] || continue
    rn_name=${rn_dir##*/}
    release_name_ok "$rn_name" || continue
    rn_n=${rn_name#r}
    if [ "$rn_n" -gt "$rn_max" ]; then rn_max=$rn_n; fi
  done
  echo $((rn_max + 1))
}

# Ignored by git so the clean-checkout gate keeps passing. Further patterns
# (the migration's compatibility symlinks) are passed after the directory.
release_exclude() {
  re_dir=$1
  shift
  mkdir -p "$re_dir/.git/info"
  {
    echo "/.release-commit"
    echo "/.release-previous"
    for re_extra in "$@"; do echo "$re_extra"; done
  } >> "$re_dir/.git/info/exclude"
}

# release_create SOURCE_RELEASE RELEASES_DIR COMMIT ORIGIN_URL: an independent
# clone (hardlinked objects, no dependency on SOURCE_RELEASE) checked out at
# COMMIT, moved into place only when complete. Prints the new release name.
release_create() {
  rc_source=$1; rc_dir=$2; rc_commit=$3; rc_origin=$4
  rc_name="r$(release_next_number "$rc_dir")"
  rc_tmp="$rc_dir/.incoming-$rc_name"
  rm -rf "$rc_tmp"
  command git clone --quiet --no-checkout "$rc_source" "$rc_tmp"
  command git -C "$rc_tmp" remote set-url origin "$rc_origin"
  command git -C "$rc_tmp" fetch --quiet "$rc_source" \
    "+refs/remotes/origin/main:refs/remotes/origin/main"
  command git -C "$rc_tmp" -c advice.detachedHead=false checkout --quiet --detach "$rc_commit"
  [ "$(command git -C "$rc_tmp" rev-parse HEAD)" = "$rc_commit" ]
  printf '%s\n' "$rc_commit" > "$rc_tmp/.release-commit"
  release_exclude "$rc_tmp"
  mv -T "$rc_tmp" "$rc_dir/$rc_name"
  echo "$rc_name"
}

# release_publish BASE NAME: record the release it replaces, then switch
# `current` with a rename, so no reader ever sees it missing.
release_publish() {
  rp_base=$1; rp_name=$2
  release_name_ok "$rp_name" && [ -d "$rp_base/releases/$rp_name" ] || return 1
  rp_old=$(readlink "$rp_base/current" 2>/dev/null || true)
  rp_old=${rp_old#releases/}
  if [ "$rp_old" != "$rp_name" ] && release_name_ok "$rp_old" \
      && [ -d "$rp_base/releases/$rp_old" ]; then
    printf '%s\n' "$rp_old" > "$rp_base/releases/$rp_name/.release-previous.new"
    mv -T "$rp_base/releases/$rp_name/.release-previous.new" \
      "$rp_base/releases/$rp_name/.release-previous"
  fi
  ln -s "releases/$rp_name" "$rp_base/.current.new.$$"
  mv -T "$rp_base/.current.new.$$" "$rp_base/current" || {
    rm -f "$rp_base/.current.new.$$"
    return 1
  }
  [ "$(readlink "$rp_base/current")" = "releases/$rp_name" ]
}

# release_prune BASE: remove every release except the one `current` names and
# the one it recorded as previous, plus leftovers of this script's own
# unfinished clones. A directory that holds real state is never removed.
release_prune() {
  rr_base=$1
  rr_current=$(readlink "$rr_base/current")
  rr_current=${rr_current#releases/}
  release_name_ok "$rr_current" || return 1
  rr_previous=$(cat "$rr_base/releases/$rr_current/.release-previous" 2>/dev/null || true)
  for rr_dir in "$rr_base"/releases/r[1-9]* "$rr_base"/releases/.incoming-r[1-9]*; do
    [ -d "$rr_dir" ] && [ ! -L "$rr_dir" ] || continue
    rr_name=${rr_dir##*/}
    case "$rr_name" in .incoming-*) rr_check=${rr_name#.incoming-} ;; *) rr_check=$rr_name ;; esac
    release_name_ok "$rr_check" || continue
    if [ "$rr_name" = "$rr_current" ] || [ "$rr_name" = "$rr_previous" ]; then
      continue
    fi
    rr_keep=""
    for rr_state in data state state-api state-web .env .deployments; do
      if [ -e "$rr_dir/$rr_state" ] && [ ! -L "$rr_dir/$rr_state" ]; then rr_keep=$rr_state; fi
    done
    if [ -n "$rr_keep" ]; then
      echo "release: keeping $rr_name, it holds real $rr_keep" >&2
      continue
    fi
    echo "release: removing $rr_name"
    rm -rf "$rr_dir"
  done
}

# release_handoff CODE_ROOT [UPDATER_ARG]: an update deploys the commit origin/main
# has now. When CODE_ROOT is already at it, return. Otherwise build the next
# release at that commit and exec its updater, so the new release's own tools
# run the update, as they did when the checkout was advanced in place. The
# caller's deployment lock (FD 9) is inherited, not released.
release_handoff() {
  rh_root=$1
  shift
  if [ -n "$(command git -C "$rh_root" status --porcelain --untracked-files=all)" ]; then
    echo "refusing to deploy a source checkout with modifications or untracked files" >&2
    exit 1
  fi
  command git -C "$rh_root" fetch --quiet origin main:refs/remotes/origin/main
  rh_head=$(command git -C "$rh_root" rev-parse HEAD)
  rh_origin=$(command git -C "$rh_root" rev-parse refs/remotes/origin/main)
  case "$rh_origin" in
    *[!0-9a-f]*|'')
      echo "refusing invalid source commit: $rh_origin" >&2
      exit 1
      ;;
  esac
  if [ "${#rh_origin}" -ne 40 ]; then
    echo "refusing source commit that is not a full SHA: $rh_origin" >&2
    exit 1
  fi
  if [ "$rh_head" = "$rh_origin" ]; then
    return 0
  fi
  if [ -n "${WUWATERM_RELEASE_HANDOFF:-}" ]; then
    echo "refusing deployment: origin/main moved while the release was being prepared" >&2
    exit 1
  fi
  rh_dir=${rh_root%/*}
  rh_url=$(command git -C "$rh_root" remote get-url origin)
  rh_name=$(release_create "$rh_root" "$rh_dir" "$rh_origin" "$rh_url")
  echo "release: created $rh_name at $rh_origin; continuing from it"
  WUWATERM_DEPLOY_ROOT="$rh_dir/$rh_name"
  WUWATERM_RELEASE_HANDOFF=$rh_origin
  export WUWATERM_DEPLOY_ROOT WUWATERM_RELEASE_HANDOFF
  exec sh "$rh_dir/$rh_name/deploy/vps-update.sh" "$@"
}

# The migration leaves data, state and state-api in releases/r1 as links to the
# shared directories, so containers created at the old paths keep resolving.
# An update that passed recreated them on the shared paths.
release_drop_compat() {
  for rd_dir in "$1"/releases/r[1-9]*; do
    for rd_name in data state state-api; do
      if [ -L "$rd_dir/$rd_name" ] && [ "$(readlink "$rd_dir/$rd_name")" = "../../$rd_name" ]; then
        rm -f "$rd_dir/$rd_name"
      fi
    done
  done
}

# release_finish BASE NAME: called only after the updater's health check has
# passed. Switch `current`, then prune. A failed prune is reported, not fatal:
# the deployment is already committed.
release_finish() {
  if ! release_publish "$1" "$2"; then
    echo "deployment committed, but $1/current could not be switched to $2" >&2
    return 1
  fi
  release_drop_compat "$1"
  echo "deployment release: $2 (current), previous: $(cat "$1/releases/$2/.release-previous" 2>/dev/null || echo none)"
  release_prune "$1" || echo "warning: release pruning failed; releases were left in place" >&2
}
