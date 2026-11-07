#!/usr/bin/env bash
# Build a local-source Core bundle: exact Git archive -> plain docker build
# (no tag, --iidfile, daemon default builder) -> nameless image save -> seal.
#
# This never holds or inherits the deployment lock FD9, never creates a
# container itself and never uses a BuildKit container driver. The result is a
# sealed delivery attempt; nothing is registered or deployed here.
set -euo pipefail
exec 9>&-
if [ -e /proc/self/fd/9 ]; then
    echo "build-delivery: file descriptor 9 is still open" >&2
    exit 2
fi

usage() {
    echo "usage: build-delivery.sh --product api --source REPO --out DELIVERY_ROOT [--commit REV] [--tag NAME]" >&2
    exit 2
}

product="" source="" out="" rev="HEAD" tag=""
while [ $# -gt 0 ]; do
    case "$1" in
        --product) product="${2-}"; shift 2 ;;
        --source) source="${2-}"; shift 2 ;;
        --out) out="${2-}"; shift 2 ;;
        --commit) rev="${2-}"; shift 2 ;;
        --tag) tag="${2-}"; shift 2 ;;
        *) usage ;;
    esac
done
[ "$product" = "api" ] || usage
[ -n "$source" ] && [ -n "$out" ] || usage

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
tool="$script_dir/component_artifact.py"
python="${PYTHON:-python3}"
script_sha="$(sha256sum "${BASH_SOURCE[0]}" | cut -d' ' -f1)"

# A container-driver builder would start a BuildKit container; refuse it.
if docker buildx version >/dev/null 2>&1; then
    driver="$(docker buildx inspect 2>/dev/null | awk -F': *' '$1=="Driver"{print $2; exit}')"
    if [ "$driver" != "docker" ]; then
        echo "build-delivery: the current builder driver is '$driver'; only the daemon's docker driver is allowed" >&2
        exit 2
    fi
fi

commit="$(git -C "$source" rev-parse --verify "$rev^{commit}")"
tree="$(git -C "$source" rev-parse --verify "$commit^{tree}")"
tag_args=()
if [ -n "$tag" ]; then
    tag_object="$(git -C "$source" rev-parse --verify "refs/tags/$tag")"
    if [ "$(git -C "$source" rev-parse --verify "refs/tags/$tag^{commit}")" != "$commit" ]; then
        echo "build-delivery: tag $tag does not peel to $commit" >&2
        exit 2
    fi
    tag_args=(--tag-object "$tag_object")
fi

attempt_id="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
mkdir -p "$out"
"$python" -I -B "$tool" begin --delivery "$out" --id "$attempt_id" --product wuwaterm-api >/dev/null
attempt="$out/attempts/$attempt_id"
echo "build-delivery: attempt $attempt_id" >&2

git -C "$source" archive --format=tar -o "$attempt/inputs/source.tar" "$commit"
tar -C "$attempt/context" -xf "$attempt/inputs/source.tar"

base_args=()
stages=" "
while read -r keyword ref as name _; do
    [ "${keyword^^}" = "FROM" ] || continue
    lower="${ref,,}"
    if [[ "$stages" != *" $lower "* ]] && [[ " ${base_args[*]-} " != *"--base-image $ref="* ]]; then
        digest="$(docker image inspect --format '{{.Id}}' "$ref")"
        base_args+=(--base-image "$ref=$digest")
    fi
    if [ "${as^^}" = "AS" ] && [ -n "$name" ]; then
        stages="$stages${name,,} "
    fi
done < "$attempt/context/deploy/Dockerfile"

docker build --pull=false --force-rm --target runtime --platform linux/amd64 \
    --build-arg "SOURCE_COMMIT=$commit" \
    --iidfile "$attempt/state/iid" \
    --file "$attempt/context/deploy/Dockerfile" "$attempt/context" </dev/null >&2
iid="$(cat "$attempt/state/iid")"

docker image save -o "$attempt/bundle/image.oci.tar" "$iid"

exec "$python" -I -B "$tool" seal --delivery "$out" --id "$attempt_id" --product wuwaterm-api \
    --commit "$commit" --tree "$tree" "${tag_args[@]}" --iid "$iid" \
    --build-script-sha256 "$script_sha" "${base_args[@]}" \
    --build-arg "SOURCE_COMMIT=$commit"
