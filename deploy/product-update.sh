#!/bin/sh
# Keep the deployment lock through every admitted child process.
set -eu
PATH=/usr/local/bin:/usr/bin:/bin
export PATH

code_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
runtime="$code_root/scripts/component_runtime.py"

# Read-only commands and parser help must not create deployment metadata.
root=
command=
case "${1:-}" in
  --root) root=${2:-}; command=${3:-} ;;
  --root=*) root=${1#--root=}; command=${2:-} ;;
esac
for argument in "$@"; do
  case "$argument" in -h|--help) exec python3 -I -B "$runtime" "$@" ;; esac
done
case "$command" in
  adopt|build|runtime-update|update|full-update|recover|recover-probe|cancel-build) ;;
  *) exec python3 -I -B "$runtime" "$@" ;;
esac

case "$root" in /*) ;; *) echo "deployment root must be an absolute physical directory" >&2; exit 1 ;; esac
physical=$(CDPATH= cd -- "$root" 2>/dev/null && pwd -P) || {
  echo "deployment root is unavailable" >&2; exit 1;
}
if [ "$root" != "$physical" ]; then
  echo "deployment root must be an absolute physical directory" >&2
  exit 1
fi
# A live builder owns FD9. Its verified saved source must first contact the
# attempt-bound supervisor; waiting for the same lock here prevents cancel.
if [ "$command" = cancel-build ]; then
  exec python3 -I -B "$runtime" "$@"
fi
directory="$root/.deployments"
lock="$directory/.deployment.lock"
if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
  echo "deployment metadata directory is not a regular directory" >&2
  exit 1
fi
if [ ! -d "$directory" ]; then
  if [ -L /proc/self/fd/9 ]; then
    echo "inherited deployment lock does not match this root" >&2
    exit 1
  fi
  (umask 077; mkdir "$directory") || exit 1
fi
if [ -L "$directory" ] || [ ! -d "$directory" ] || [ -L "$lock" ] || {
  [ -e "$lock" ] && [ ! -f "$lock" ];
}; then
  echo "deployment lock path is unsafe" >&2
  exit 1
fi
if [ ! -L /proc/self/fd/9 ]; then
  (umask 077; : >>"$lock")
  exec 9>>"$lock"
fi
target=$(readlink /proc/self/fd/9 2>/dev/null) || exit 1
fd_id=$(LC_ALL=C stat -Lc '%d:%i' /proc/self/fd/9 2>/dev/null) || exit 1
path_id=$(LC_ALL=C stat -c '%d:%i' "$lock" 2>/dev/null) || exit 1
if [ "$target" != "$lock" ] || [ ! -f /proc/self/fd/9 ] || [ -L "$lock" ] || [ "$fd_id" != "$path_id" ]; then
  echo "inherited deployment lock does not match this root" >&2
  exit 1
fi
if ! flock -n 9; then
  echo "deployment already in progress" >&2
  exit 75
fi
exec python3 -I -B "$runtime" "$@"
