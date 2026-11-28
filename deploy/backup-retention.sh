# Retention for data/deployment-backups. POSIX sh, sourced by deploy/vps-update.sh.
#
# A full deployment writes up to four files there, each named by its deployment
# id, <UTC time>-<pid> (YYYYmmddTHHMMSSZ-<pid>; `deployment_id` in vps-update.sh):
#   terms.<id>.<sha256>.db      the terminology database it replaced
#   knowledge.<id>.<sha256>.db  the companion knowledge database it replaced (when active)
#   db-diff.<id>.json           what its candidate changed in that database
#   <id>.deploy_commit          the commit pointer it replaced
# A snapshot is every such file that shares one id. Any other name, a symlink and
# a directory are not snapshot files: they are never counted and never touched.

# backup_snapshot_id NAME: print the deployment id a snapshot file name carries,
# or fail when NAME is not one.
backup_snapshot_id() {
  case "$1" in
    terms.*.db|knowledge.*.db)
      bs_rest=${1#*.}
      bs_rest=${bs_rest%.db}
      bs_id=${bs_rest%.*}
      bs_sha=${bs_rest##*.}
      case "$bs_sha" in *[!0-9a-f]*) return 1 ;; esac
      [ "${#bs_sha}" -eq 64 ] || return 1
      ;;
    db-diff.*.json) bs_id=${1#db-diff.}; bs_id=${bs_id%.json} ;;
    *.deploy_commit) bs_id=${1%.deploy_commit} ;;
    *) return 1 ;;
  esac
  case "$bs_id" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-*) ;;
    *) return 1 ;;
  esac
  case "${bs_id#*Z-}" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$bs_id"
}

# backup_retention_prune DIR KEEP [PROTECTED_ID]: keep the KEEP newest snapshots
# in DIR and PROTECTED_ID (the snapshot the caller just wrote), remove every file
# of the others, and print one line per file removed. Newest means the later
# time in the id, then the larger pid taken as a number; mtimes are not read. A
# missing DIR, or one with no snapshot, is left as it is. Fails only when a file
# could not be removed or KEEP is not a positive number.
backup_retention_prune() (
  LC_ALL=C
  export LC_ALL
  brp_dir=$1
  brp_keep=$2
  brp_protect=${3:-}
  case "$brp_keep" in ''|0*|*[!0-9]*) echo "backup retention: KEEP must be a positive number" >&2; exit 2 ;; esac
  [ -d "$brp_dir" ] || exit 0
  brp_ids=""
  for brp_path in "$brp_dir"/*; do
    { [ -f "$brp_path" ] && [ ! -L "$brp_path" ]; } || continue
    brp_id=$(backup_snapshot_id "${brp_path##*/}") || continue
    brp_ids="$brp_ids$brp_id
"
  done
  [ -n "$brp_ids" ] || exit 0
  # Newest first: time as text (fixed width), then pid as a number.
  brp_ordered=$(printf '%s' "$brp_ids" | sort -u | sort -t- -k1,1r -k2,2nr)
  brp_keepset=" $brp_protect "
  brp_count=0
  for brp_id in $brp_ordered; do
    brp_count=$((brp_count + 1))
    if [ "$brp_count" -le "$brp_keep" ]; then brp_keepset="$brp_keepset$brp_id "; fi
  done
  brp_failed=0
  for brp_path in "$brp_dir"/*; do
    { [ -f "$brp_path" ] && [ ! -L "$brp_path" ]; } || continue
    brp_id=$(backup_snapshot_id "${brp_path##*/}") || continue
    case "$brp_keepset" in *" $brp_id "*) continue ;; esac
    if rm -f -- "$brp_path"; then
      echo "backup retention: removed $brp_path"
    else
      echo "backup retention: could not remove $brp_path" >&2
      brp_failed=1
    fi
  done
  exit "$brp_failed"
)
