#!/usr/bin/env bash
set -euo pipefail
version="${TAG#v}"
major_minor="${version%.*}"
if ! printf '%s' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "tag '$TAG' is not vX.Y.Z; refusing to promote" >&2
  exit 1
fi

digest_of() {
  local output digest
  if output="$(docker buildx imagetools inspect "$1" 2>&1)"; then
    digest="$(printf '%s\n' "$output" | awk '/^Digest:/{print $2; exit}')"
    if ! [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
      echo "invalid digest inspection for $1" >&2
      return 1
    fi
    printf '%s\n' "$digest"
  elif printf '%s\n' "$output" | grep -Eq 'manifest unknown|^ERROR: [^ ]+: not found$'; then
    return 0
  else
    echo "cannot inspect $1; refusing promotion" >&2
    return 1
  fi
}

version_of() {
  docker buildx imagetools inspect "$1" --format '{{json .Image}}' \
    | jq -er '[.. | objects | select(has("config")) |
               .config.Labels["org.opencontainers.image.version"]] |
              if length > 0 and all(.[]; type == "string" and length > 0)
              then unique | if length == 1 then .[0] else error("ambiguous version") end
              else error("missing version") end'
}

preflight_one() {
  local image="$1" digest="$2" existing
  if ! [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || \
     [ "$(digest_of "${image}@${digest}")" != "$digest" ]; then
    echo "missing or invalid source digest for $image" >&2
    return 1
  fi
  existing="$(digest_of "${image}:${TAG}")" || return 1
  if [ -n "$existing" ] && [ "$existing" != "$digest" ]; then
    echo "refusing to move immutable ${image}:${TAG}" >&2
    return 1
  fi
  existing="$(digest_of "${image}:${major_minor}")" || return 1
  check_minor "$image" "$digest" "$existing"
}

check_minor() {
  local image="$1" digest="$2" existing="$3" old_version old_patch
  if [ -z "$existing" ] || [ "$existing" = "$digest" ]; then
    return 0
  fi
  old_version="$(version_of "${image}@${existing}")" || return 1
  if ! [[ "$old_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
     [ "${old_version%.*}" != "$major_minor" ]; then
    echo "cannot establish the version of ${image}:${major_minor}" >&2
    return 1
  fi
  old_patch="${old_version##*.}"
  if [ "$((10#$old_patch))" -ge "$((10#${version##*.}))" ] || \
     [ "$(digest_of "${image}:v${old_version}")" != "$existing" ]; then
    echo "refusing rollback or conflicting ${image}:${major_minor}" >&2
    return 1
  fi
}

promote_one() {
  image="$1"
  digest="$2"
  source="${image}@${digest}"
  if [ -z "$digest" ] || [ "$digest" = "null" ]; then
    echo "missing digest for $image; stop" >&2
    exit 1
  fi
  for t in "$TAG" "$major_minor"; do
    dest="${image}:${t}"
    existing="$(digest_of "$dest")"
    if [ "$existing" = "$digest" ]; then
      echo "already promoted $dest -> $digest"
      continue
    fi
    # A registry writer outside this workflow can change a tag
    # after preflight. Refuse conflicts at the final observed read.
    if [ "$t" = "$TAG" ]; then
      if [ -n "$existing" ]; then
        echo "refusing to move immutable $dest" >&2
        exit 1
      fi
    else
      check_minor "$image" "$digest" "$existing"
    fi
    docker buildx imagetools create --tag "$dest" "$source"
    got="$(digest_of "$dest")"
    if [ "$got" != "$digest" ]; then
      echo "$dest resolved to '$got', expected $digest" >&2
      exit 1
    fi
    echo "promoted $dest -> $digest"
  done
}

runtime_digest="$(jq -r .images.runtime.digest staging/release-manifest.json)"
builder_digest="$(jq -r .images.builder.digest staging/release-manifest.json)"
runtime_image="$(jq -r .images.runtime.name staging/release-manifest.json)"
builder_image="$(jq -r .images.builder.name staging/release-manifest.json)"
# Reject any conflict for either image before changing any tag.
preflight_one "$runtime_image" "$runtime_digest"
preflight_one "$builder_image" "$builder_digest"
promote_one "$runtime_image" "$runtime_digest"
promote_one "$builder_image" "$builder_digest"

{
  echo "### GHCR tags promoted"
  echo ""
  echo "- source release: $TAG"
  echo "- runtime $TAG / $major_minor -> $runtime_digest"
  echo "- builder $TAG / $major_minor -> $builder_digest"
  echo "- no rebuild; imagetools retag of the published manifest digests"
} >> "$GITHUB_STEP_SUMMARY"
