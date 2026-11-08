#!/bin/sh
# Official one-command install and opt-in enablement for this checkout.
#
#   ./deploy/setup.sh                      Core API only (shared translation service)
#   ./deploy/setup.sh --with-telegram      add the Telegram bot
#   ./deploy/setup.sh --with-web           add the WuwaTerm-Web workbench
#   ./deploy/setup.sh --status | --check   report what is installed and healthy
#
# Re-running the same command is the supported way to add a surface to an
# existing installation: the .env, the data build, the credential store, the
# Web secrets and the Compose services are only created when they are missing.
# Nothing here prints a secret; the operator receives file paths instead.
#
# Requirements: Linux with Docker (Compose v2), curl, and a POSIX shell. The
# source checkout stays necessary because the Compose files, entrypoints and
# data commands live in it; the images themselves come from GHCR unless
# --build-local is passed.
set -eu
umask 077

code_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$code_root"

with_telegram=0
with_web=0
web_prefix=
web_package=
build_local=0
image_tag=${WUWATERM_IMAGE_TAG:-}
mode=install
check_strict=0
do_adopt=1

usage() {
    cat <<'EOF'
Usage: ./deploy/setup.sh [options]

Start the shared translation service, and add the surfaces you choose.

  (no flag)             Core HTTP API only: dictionary lookup, translation,
                        review, device credentials. The Telegram bot is never
                        started, so an API-only installation cannot fall into a
                        bot restart loop.
  --with-telegram       Also start the Telegram bot. Requires
                        TELEGRAM_BOT_TOKEN in .env; OWNER_USER_ID is strongly
                        recommended (empty means private chat rejects everyone).
  --with-web            Also install and start the WuwaTerm-Web workbench.
                        Requires a published Web package: either the official
                        GitHub release assets (manifest-{root|prefix}.json and
                        SHA256SUMS-{root|prefix}) or --web-package.
  --web-prefix PATH     URL prefix of the Web build (for example /private).
                        Empty, the default, needs the root-path package.
  --web-package PATH    Use a local Web package (directory with a manifest,
                        or a .tar.gz/.tgz archive) instead of a release.
  --build-local         Build the runtime images from this checkout instead of
                        pulling the official images.
  --image-tag TAG       Official image tag to pull. Default: v<declared version>.
  --no-adopt            Do not adopt the topology into deploy/product-update.sh
                        metadata, even when a standalone Compose binary and its
                        checksum receipt are available.
  --status              Report the installed components, data version and
                        health without changing anything.
  --check               Like --status, but exit non-zero when the API is not
                        ready.
  -h, --help            Show this help.

Environment: WUWATERM_IMAGE_TAG is honoured; WUWATERM_WEB_NODE_IMAGE overrides
the Node image used to run a Web package (default node:24-alpine).
EOF
}

fail() {
    printf 'setup: %s\n' "$1" >&2
    exit 1
}

usage_error() {
    printf 'setup: %s\n\n' "$1" >&2
    usage >&2
    exit 2
}

need_value() {
    [ "$#" -ge 2 ] || usage_error "option $1 needs a value"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-telegram) with_telegram=1 ;;
        --with-web) with_web=1 ;;
        --web-prefix)
            need_value "$@"; web_prefix=$2; shift ;;
        --web-prefix=*) web_prefix=${1#--web-prefix=} ;;
        --web-package)
            need_value "$@"; web_package=$2; shift ;;
        --web-package=*) web_package=${1#--web-package=} ;;
        --build-local) build_local=1 ;;
        --image-tag)
            need_value "$@"; image_tag=$2; shift ;;
        --image-tag=*) image_tag=${1#--image-tag=} ;;
        --no-adopt) do_adopt=0 ;;
        --status) mode=status ;;
        --check) mode=status; check_strict=1 ;;
        -h|--help|help) usage; exit 0 ;;
        *) usage_error "unknown option: $1" ;;
    esac
    shift
done

case "$web_prefix" in
    "") ;;
    /*) web_prefix=${web_prefix%/} ;;
    *) usage_error "--web-prefix must start with /" ;;
esac
# Asking for a Web package or a prefix is asking for the Web surface.
if [ -n "$web_package" ] || [ -n "$web_prefix" ]; then
    with_web=1
fi

# The Compose files bind-mount ../data, ../state, ../state-api, so the root is
# always the directory above deploy/. --env-file makes the root .env the single
# interpolation source regardless of the caller's working directory.
compose_file="$code_root/deploy/docker-compose.yml"
ghcr_file="$code_root/deploy/docker-compose.ghcr.yml"

compose() {
    if [ "$build_local" = 1 ] || [ -z "$image_tag" ]; then
        env WUWATERM_IMAGE_TAG="$image_tag" docker compose \
            --env-file "$code_root/.env" -f "$compose_file" "$@"
    else
        env WUWATERM_IMAGE_TAG="$image_tag" docker compose \
            --env-file "$code_root/.env" -f "$compose_file" -f "$ghcr_file" "$@"
    fi
}

declared_version() {
    sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$code_root/pyproject.toml" | head -n 1
}

require_docker() {
    command -v docker >/dev/null 2>&1 || fail "docker is required (install Docker Engine with Compose v2)"
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required: install the compose plugin (docker compose version must succeed)"
    command -v curl >/dev/null 2>&1 || fail "curl is required to check service readiness"
}

# Read a var from .env without executing it. Only the value text is printed.
env_value() {
    key=$1
    [ -f "$code_root/.env" ] || return 1
    sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*//p" "$code_root/.env" |
        tail -n 1 | tr -d '\r'
}

require_env() {
    name=$1
    value=$(env_value "$name" || true)
    [ -n "$value" ] || fail "$name is empty in .env. Edit $code_root/.env before running setup."
}

warn_if_empty() {
    name=$1
    value=$(env_value "$name" || true)
    [ -n "$value" ] || printf 'setup: warning: %s is empty in .env; the bot will refuse that path.\n' "$name" >&2
}

ensure_layout() {
    mkdir -p "$code_root/data" "$code_root/state" "$code_root/state-api"
    chmod 700 "$code_root/state" "$code_root/state-api"
    if [ "$with_web" = 1 ]; then
        mkdir -p "$code_root/state-web"
        chmod 700 "$code_root/state-web"
    fi
    if [ ! -f "$code_root/.env" ]; then
        [ -f "$code_root/deploy/env.example" ] || fail "deploy/env.example is missing"
        cp "$code_root/deploy/env.example" "$code_root/.env"
        printf 'setup: created .env from deploy/env.example; review the model settings.\n'
    fi
    chmod 600 "$code_root/.env"
}

validate_config() {
    # Sentence translation is the product; exact lookup alone needs no model,
    # but a configuration that cannot translate is not a finished install.
    require_env WUWATERM_OPENAI_BASE_URL
    require_env WUWATERM_OPENAI_API_KEY
    require_env WUWATERM_OPENAI_MODEL
    if [ "$with_telegram" = 1 ]; then
        require_env TELEGRAM_BOT_TOKEN
        warn_if_empty OWNER_USER_ID
    fi
}

wait_for_ready() {
    url=$1
    label=$2
    tries=0
    while [ "$tries" -lt 60 ]; do
        if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
    fail "$label did not become ready at $url"
}

start_api() {
    if [ "$build_local" = 1 ]; then
        compose build wuwaterm-api
        compose up -d --no-build wuwaterm-api
    else
        [ -n "$image_tag" ] || fail "set --image-tag or WUWATERM_IMAGE_TAG for the official images"
        compose pull wuwaterm-api \
            || fail "could not pull the official image $image_tag; publish that release or pass --build-local"
        compose up -d --no-build wuwaterm-api
    fi
    wait_for_ready "http://127.0.0.1:$(api_port)/readyz" "wuwaterm-api"
}

api_port() {
    value=$(env_value WUWATERM_API_PORT || true)
    printf '%s' "${value:-8788}"
}

build_data() {
    if [ -f "$code_root/data/terms.db" ]; then
        printf 'setup: data/terms.db already present; keeping it.\n'
        return 0
    fi
    printf 'setup: data/terms.db is missing; building it from the pinned upstream source.\n'
    printf 'setup: this fetches roughly a gigabyte and can fail part-way; re-running resumes it.\n'
    if [ "$build_local" = 1 ]; then
        compose build wuwaterm-builder
    else
        compose pull wuwaterm-builder \
            || fail "could not pull the official builder image $image_tag; publish that release or pass --build-local"
    fi
    compose run --rm wuwaterm-builder refresh-data
    compose run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
    compose run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
    [ -f "$code_root/data/terms.candidate.db" ] \
        || fail "the builder did not produce data/terms.candidate.db"
    if [ -L "$code_root/data/terms.db" ] || [ -e "$code_root/data/terms.db" ]; then
        fail "data/terms.db appeared while building; refusing to overwrite it"
    fi
    mv "$code_root/data/terms.candidate.db" "$code_root/data/terms.db"
    printf 'setup: data/terms.db is in place.\n'
}

# Print nothing secret: the caller only learns the device id and where the
# token was written. The secret is generated here, fed to the CLI on stdin and
# stored in one 600 file.
issue_device() {
    name=$1
    scopes=$2
    token_file=$3
    if [ -s "$token_file" ]; then
        printf 'setup: %s already holds a device token; keeping it.\n' "$token_file"
        return 0
    fi
    secret=$( (head -c 48 /dev/urandom | base64 | tr -d '\n') )
    [ -n "$secret" ] || fail "could not generate a device secret"
    secret_file="$code_root/state-api/.device-secret.$$"
    (umask 077; printf '%s' "$secret" >"$secret_file")
    output="$code_root/state-api/.device-issue.$$"
    if ! compose run --rm -T wuwaterm-api device issue --name "$name" \
        --scopes "$scopes" <"$secret_file" >"$output" 2>&1; then
        rm -f "$secret_file" "$output"
        fail "device issue failed for $name"
    fi
    rm -f "$secret_file"
    device_id=$(sed -n 's/^device_id: //p' "$output")
    if [ -z "$device_id" ]; then
        rm -f "$output"
        fail "device issue did not report a device id"
    fi
    rm -f "$output"
    (umask 077; printf 'wtd1.%s.%s\n' "$device_id" "$secret" >"$token_file")
    chmod 600 "$token_file"
    printf 'setup: issued device %s (%s); token written to %s.\n' "$device_id" "$name" "$token_file"
}

ensure_api_device() {
    token_file="$code_root/state-api/first-device.token"
    if [ -s "$token_file" ]; then
        printf 'setup: client device credential already present.\n'
        return 0
    fi
    listing=$(compose run --rm -T wuwaterm-api device list 2>/dev/null || true)
    if printf '%s' "$listing" | grep -q "  active  "; then
        printf 'setup: an active device already exists in the credential store; no new token written.\n'
        printf 'setup: issue another with: docker compose -f deploy/docker-compose.yml run --rm -T wuwaterm-api device issue --name <name>\n'
        return 0
    fi
    issue_device "setup" "translate,meta" "$token_file"
}

enable_telegram() {
    [ "$with_telegram" = 1 ] || return 0
    if [ "$build_local" = 1 ]; then
        compose build wuwaterm
        compose up -d --no-build wuwaterm
    else
        compose pull wuwaterm \
            || fail "could not pull the official image $image_tag; publish that release or pass --build-local"
        compose up -d --no-build wuwaterm
    fi
    sleep 3
    state=$(docker inspect -f '{{.State.Status}}' wuwaterm-bot 2>/dev/null || true)
    [ "$state" = "running" ] || fail "wuwaterm-bot is not running (state: ${state:-missing}); check docker logs wuwaterm-bot"
    printf 'setup: Telegram bot is running.\n'
}

web_variant() {
    if [ -n "$web_prefix" ]; then
        printf 'prefix'
    else
        printf 'root'
    fi
}

manifest_key() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    value = json.load(handle).get(key)
if not isinstance(value, str) or not value:
    raise SystemExit(f"manifest is missing a string {key!r}")
print(value)
PY
}

sha256_of() {
    sha256sum "$1" | awk '{print $1}'
}

resolve_web_package() {
    command -v python3 >/dev/null 2>&1 \
        || fail "python3 is required to read the Web package manifest"
    variant=$(web_variant)
    package_root="$code_root/state-web/package"
    if [ -z "$web_package" ] \
        && [ -f "$package_root/manifest-$variant.json" ]; then
        # A package installed by an earlier run stays usable without network.
        printf 'setup: reusing the installed Web package at %s.\n' "$package_root"
    elif [ -n "$web_package" ]; then
        [ -e "$web_package" ] || fail "--web-package path does not exist: $web_package"
        if [ -d "$web_package" ]; then
            package_root=$(CDPATH= cd -- "$web_package" && pwd -P)
        elif [ -f "$web_package" ]; then
            mkdir -p "$code_root/state-web"
            rm -rf "$code_root/state-web/package"
            mkdir -p "$code_root/state-web/package"
            tar -xzf "$web_package" -C "$code_root/state-web/package" \
                || fail "could not extract $web_package"
            package_root="$code_root/state-web/package"
        else
            fail "--web-package must be a directory or a .tar.gz/.tgz archive"
        fi
    else
        base="https://github.com/My-Denia/WuwaTerm-Web/releases/latest/download"
        mkdir -p "$code_root/state-web"
        manifest_url="$base/manifest-$variant.json"
        sums_url="$base/SHA256SUMS-$variant"
        if ! curl -fsSL --max-time 30 "$manifest_url" -o "$code_root/state-web/manifest-$variant.json"; then
            fail "no published WuwaTerm-Web package was found ($manifest_url). WuwaTerm-Web has no release yet; download a package from its repository and pass --web-package, or install the Core API and the Web product separately."
        fi
        curl -fsSL --max-time 30 "$sums_url" -o "$code_root/state-web/SHA256SUMS-$variant" \
            || fail "the Web release does not publish SHA256SUMS-$variant; refusing an unverified package"
        artifact=$(manifest_key "$code_root/state-web/manifest-$variant.json" artifact)
        case "$artifact" in
            ""|*/*|*..*|.*) fail "the Web manifest names an unsafe artifact file" ;;
        esac
        curl -fsSL --max-time 600 "$base/$artifact" -o "$code_root/state-web/$artifact" \
            || fail "could not download the Web package $artifact"
        expected=$(awk -v name="$artifact" '$2 == name || $2 == "*" name {print $1}' \
            "$code_root/state-web/SHA256SUMS-$variant" | head -n 1)
        [ -n "$expected" ] || fail "SHA256SUMS-$variant does not list $artifact"
        actual=$(sha256_of "$code_root/state-web/$artifact")
        [ "$expected" = "$actual" ] || fail "the Web package checksum does not match SHA256SUMS-$variant"
        rm -rf "$code_root/state-web/package"
        mkdir -p "$code_root/state-web/package"
        tar -xzf "$code_root/state-web/$artifact" -C "$code_root/state-web/package" \
            || fail "could not extract the Web package"
        package_root="$code_root/state-web/package"
    fi
    manifest="$package_root/manifest-$variant.json"
    [ -f "$manifest" ] || fail "the Web package has no manifest-$variant.json"
    web_start=$(manifest_key "$manifest" start)
    web_port=$(python3 - "$manifest" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get("port", 3000)
if not isinstance(value, int) or not 1 <= value <= 65535:
    raise SystemExit("manifest port must be 1..65535")
print(value)
PY
)
    web_health=$(manifest_key "$manifest" health_path)
}

enable_web() {
    [ "$with_web" = 1 ] || return 0
    web_env="$code_root/state-web/web.env"
    if [ ! -f "$web_env" ]; then
        edge_secret=$(head -c 48 /dev/urandom | base64 | tr -d '\n')
        api_device_file="$code_root/state-web/device.token"
        issue_device "web" "translate,meta" "$api_device_file"
        web_token=$(cat "$api_device_file")
        (
            umask 077
            {
                printf 'WUWATERM_API_BASE_URL=http://127.0.0.1:%s\n' "$(api_port)"
                printf 'WUWATERM_SITE_DEVICE_TOKEN=%s\n' "$web_token"
                printf 'WUWATERM_WEB_EDGE_SECRET=%s\n' "$edge_secret"
                printf 'WUWATERM_WEB_BASE_PATH=%s\n' "$web_prefix"
            } >"$web_env"
        )
        chmod 600 "$web_env"
        printf 'setup: wrote Web credentials to %s (mode 600).\n' "$web_env"
    fi
    node_image=${WUWATERM_WEB_NODE_IMAGE:-node:24-alpine}
    docker rm -f wuwaterm-web >/dev/null 2>&1 || true
    docker run -d --name wuwaterm-web --restart unless-stopped --network host \
        --env-file "$web_env" -e "PORT=$web_port" \
        -e "WUWATERM_WEB_BASE_PATH=$web_prefix" \
        -v "$package_root:/app:ro" -w /app "$node_image" sh -c "$web_start" >/dev/null \
        || fail "could not start the Web workbench"
    wait_for_ready "http://127.0.0.1:$web_port$web_health" "wuwaterm-web"
    printf 'setup: Web workbench is running at http://127.0.0.1:%s%s/ behind your authenticated proxy.\n' "$web_port" "$web_prefix"
}

maybe_adopt() {
    [ "$do_adopt" = 1 ] || return 0
    [ -f "$code_root/deploy/product-update.sh" ] || return 0
    if [ -d "$code_root/.deployments" ]; then
        printf 'setup: deployment metadata already adopted.\n'
        return 0
    fi
    compose_bin=$(command -v docker-compose 2>/dev/null || true)
    if [ -z "$compose_bin" ]; then
        printf 'setup: adoption skipped: a standalone docker-compose binary was not found.\n' >&2
        printf 'setup: install one with a <binary>.sha256 receipt to use deploy/product-update.sh later.\n' >&2
        return 0
    fi
    compose_bin=$(readlink -f "$compose_bin" 2>/dev/null || printf '%s' "$compose_bin")
    if [ ! -f "$compose_bin.sha256" ]; then
        printf 'setup: adoption skipped: %s.sha256 is missing.\n' "$compose_bin" >&2
        return 0
    fi
    project=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' \
        wuwaterm-api 2>/dev/null || true)
    [ -n "$project" ] || {
        printf 'setup: adoption skipped: the running service has no Compose project label.\n' >&2
        return 0
    }
    if "$code_root/deploy/product-update.sh" --root "$code_root" adopt \
        --project "$project" --compose-executable "$compose_bin"; then
        printf 'setup: adopted the installed topology for deploy/product-update.sh.\n'
    else
        printf 'setup: adoption failed; the service is running, but transactional updates are not set up.\n' >&2
    fi
}

container_state() {
    docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || true
}

file_mode() {
    stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || printf '?'
}

report_status() {
    printf 'WuwaTerm status\n\n'
    api_port_value=$(api_port)
    if [ -f "$code_root/.env" ]; then
        printf 'env:          %s (mode %s)\n' "$code_root/.env" "$(file_mode "$code_root/.env")"
    else
        printf 'env:          missing (%s)\n' "$code_root/.env"
    fi
    if [ -d "$code_root/state-api" ]; then
        printf 'state-api:    %s (mode %s)\n' "$code_root/state-api" "$(file_mode "$code_root/state-api")"
    else
        printf 'state-api:    missing\n'
    fi
    if [ -f "$code_root/data/terms.db" ]; then
        printf 'data:         data/terms.db present (%s bytes)\n' \
            "$(wc -c <"$code_root/data/terms.db" | tr -d ' ')"
    else
        printf 'data:         data/terms.db missing\n'
    fi
    api_state=$(container_state wuwaterm-api)
    printf 'api:          %s\n' "${api_state:-not installed}"
    if curl -fsS --max-time 3 "http://127.0.0.1:$api_port_value/readyz" >/dev/null 2>&1; then
        printf 'readiness:    ready (http://127.0.0.1:%s/readyz)\n' "$api_port_value"
        ready=1
    else
        printf 'readiness:    not ready (http://127.0.0.1:%s/readyz)\n' "$api_port_value"
        ready=0
    fi
    if [ -s "$code_root/state-api/first-device.token" ]; then
        token=$(cat "$code_root/state-api/first-device.token")
        # The header travels on stdin so the device token never appears in the
        # process argument list, where any local user could read it via ps.
        meta=$(printf 'Authorization: Bearer %s\n' "$token" | \
            curl -fsS --max-time 5 -H @- \
                "http://127.0.0.1:$api_port_value/v1/meta" 2>/dev/null || true)
        if [ -n "$meta" ]; then
            summary=$(printf '%s' "$meta" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(
    "schema=" + str(d.get("schema_version")),
    "game=" + str(d.get("source_game_version")),
    "source=" + str(d.get("source_commit"))[:12],
    "terms=" + str(d.get("term_count")),
)
' 2>/dev/null || true)
            printf 'data version: %s\n' "${summary:-unavailable}"
        else
            printf 'data version: could not be read (run --check after the API is ready)\n'
        fi
    fi
    bot_state=$(container_state wuwaterm-bot)
    printf 'telegram:     %s\n' "${bot_state:-not installed}"
    if [ -f "$code_root/state-web/web.env" ]; then
        web_state=$(container_state wuwaterm-web)
        printf 'web:          %s\n' "${web_state:-not running}"
        web_port_value=
        for manifest_name in manifest-prefix.json manifest-root.json; do
            manifest_path="$code_root/state-web/package/$manifest_name"
            [ -f "$manifest_path" ] || continue
            web_port_value=$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
                "$manifest_path" | head -n 1)
            [ -z "$web_port_value" ] || break
        done
        if [ -n "$web_port_value" ] && curl -fsS --max-time 3 "http://127.0.0.1:$web_port_value/api/readyz" >/dev/null 2>&1; then
            printf 'web health:   ready\n'
        else
            printf 'web health:   not ready\n'
        fi
    else
        printf 'web:          not configured\n'
    fi
    if [ -d "$code_root/.deployments" ]; then
        printf 'updates:      adopted (deploy/product-update.sh)\n'
    else
        printf 'updates:      not adopted\n'
    fi
    if [ "$check_strict" = 1 ] && [ "${ready:-0}" != 1 ]; then
        return 1
    fi
    return 0
}

if [ "$mode" = status ]; then
    command -v docker >/dev/null 2>&1 \
        || printf 'setup: warning: docker is not available; container states are unknown.\n' >&2
    report_status
    exit $?
fi

require_docker
if [ -z "$image_tag" ]; then
    version=$(declared_version)
    [ -n "$version" ] || fail "cannot read the declared version from pyproject.toml"
    image_tag="v$version"
fi
case "$image_tag" in
    *[!A-Za-z0-9._-]*) fail "--image-tag contains characters Compose cannot use: $image_tag" ;;
esac

ensure_layout
validate_config
# Resolve the Web package before the data build: a missing package should stop
# the install instead of after a gigabyte of upstream data was fetched.
if [ "$with_web" = 1 ]; then
    resolve_web_package
fi
build_data
start_api
ensure_api_device
enable_telegram
enable_web
maybe_adopt

printf '\nsetup: done.\n'
report_status
