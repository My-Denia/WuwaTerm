#!/bin/sh
# Official one-command install and opt-in enablement for this checkout.
#
#   ./deploy/setup.sh                      Core API only (shared translation service)
#   ./deploy/setup.sh --with-telegram      hand the Telegram surface to its own product
#   ./deploy/setup.sh --with-legacy-bot    deprecated: start the inline bot in this checkout
#   ./deploy/setup.sh --with-web           add the WuwaTerm-Web workbench
#   ./deploy/setup.sh --status | --check   report what is installed and healthy
#
# Re-running the same command is the supported way to add a surface to an
# existing installation: the .env, the data build, the credential store, the
# Web secrets and the Compose services are only created when they are missing.
# Nothing here prints a secret; the operator receives file paths instead.
#
# The Telegram bot is a separate product with its own repository, release and
# Compose file, so --with-telegram routes to that entry instead of installing
# the inline bot this checkout still carries. See install_telegram().
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
with_legacy_bot=0
telegram_dir=
with_web=0
web_prefix=
web_package=
web_origin=
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
                        review, device credentials. No bot is started, so an
                        API-only installation cannot fall into a bot restart
                        loop.
  --with-telegram       Install the Telegram surface through its own product,
                        WuwaTerm-Telegram, which has its own repository,
                        release and Compose file. This checkout starts no bot:
                        it prints that product's install steps, filled in with
                        this installation's database path. Pass --telegram-dir
                        to also write that checkout's .env and copy the
                        database into it.
  --telegram-dir PATH   WuwaTerm-Telegram checkout to prepare (implies
                        --with-telegram): writes its .env at mode 0600 from
                        this .env and copies data/terms.db into it. Requires
                        TELEGRAM_BOT_TOKEN in .env.
  --with-legacy-bot     Deprecated. Also start the inline bot this checkout
                        still carries (wuwaterm-bot). Requires
                        TELEGRAM_BOT_TOKEN in .env; OWNER_USER_ID is strongly
                        recommended (empty means private chat rejects everyone).
                        The supported Telegram surface is the standalone
                        product instead: --with-telegram.
  --with-web            Also install and start the WuwaTerm-Web workbench.
                        Requires a published Web package: either the official
                        GitHub release assets (manifest-{root|prefix}.json and
                        SHA256SUMS-{root|prefix}) or --web-package.
  --web-prefix PATH     URL prefix of the Web build (for example /private).
                        Empty, the default, needs the root-path package.
  --web-package PATH    Use a local Web package instead of a release: a
                        directory holding bundle/start.mjs (with or without a
                        manifest), or a .tar.gz/.tgz archive.
  --web-origin URL      External HTTPS origin the Web workbench is served
                        from, without the prefix (for example
                        https://wuwaterm.example). The reverse proxy keeps
                        terminating TLS; the Web process uses this origin for
                        its session cookies. Defaults to WUWATERM_WEB_ORIGIN,
                        then the value already stored in state-web/web.env.
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
WUWATERM_TELEGRAM_VERSION selects the WuwaTerm-Telegram release the printed
steps name (default v0.2.9, or the tag recorded in contracts/products.json).
WUWATERM_WEB_VERSION picks the WuwaTerm-Web release the Web install downloads:
v0.2.0, the current published release, or latest (default v0.2.0, or the tag
recorded in contracts/products.json).
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
        --with-legacy-bot) with_legacy_bot=1 ;;
        --telegram-dir)
            need_value "$@"; telegram_dir=$2; shift ;;
        --telegram-dir=*) telegram_dir=${1#--telegram-dir=} ;;
        --with-web) with_web=1 ;;
        --web-prefix)
            need_value "$@"; web_prefix=$2; shift ;;
        --web-prefix=*) web_prefix=${1#--web-prefix=} ;;
        --web-package)
            need_value "$@"; web_package=$2; shift ;;
        --web-package=*) web_package=${1#--web-package=} ;;
        --web-origin)
            need_value "$@"; web_origin=$2; shift ;;
        --web-origin=*) web_origin=${1#--web-origin=} ;;
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
# Preparing the product checkout is asking for the Telegram surface.
if [ -n "$telegram_dir" ]; then
    with_telegram=1
fi
# Routing the surface and installing the deprecated inline bot are two
# different installations, and one host cannot run both against one token.
if [ "$with_telegram" = 1 ] && [ "$with_legacy_bot" = 1 ]; then
    usage_error "--with-telegram and --with-legacy-bot are alternatives: the standalone product replaces the inline bot"
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
    # Routing the surface needs no token here; the product has its own .env.
    # Writing that .env, or starting the deprecated inline bot, does.
    if [ "$with_legacy_bot" = 1 ] || [ -n "$telegram_dir" ]; then
        require_env TELEGRAM_BOT_TOKEN
        warn_if_empty OWNER_USER_ID
    fi
    if [ "$with_web" = 1 ]; then
        resolve_web_origin
    fi
}

# The Web process needs its external HTTPS origin: it is the only origin an
# unsafe request is allowed to come from, and the session cookie is Secure.
# Re-running --with-web reuses the value already in state-web/web.env.
resolve_web_origin() {
    if [ -z "$web_origin" ]; then
        web_origin=${WUWATERM_WEB_ORIGIN:-}
    fi
    if [ -z "$web_origin" ]; then
        web_origin=$(file_value "$code_root/state-web/web.env" WUWATERM_WEB_ORIGIN || true)
    fi
    [ -n "$web_origin" ] \
        || fail "--with-web needs the external HTTPS origin: pass --web-origin https://wuwaterm.example (the proxy terminates TLS and the Web process issues a Secure session cookie)."
    web_origin=${web_origin%/}
    case "$web_origin" in
        https://*) ;;
        *) fail "--web-origin must be an https:// origin such as https://wuwaterm.example" ;;
    esac
    host=${web_origin#https://}
    case "$host" in
        ""|*/*|*'?'*|*'#'*|*@*|*' '*) fail "--web-origin must be an origin only: no path, trailing slash, query, fragment or credentials" ;;
    esac
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

# The Telegram bot is its own product: its own repository, release, Compose
# file and state, and its own pinned Core wheel in core-lock.json. It reads a
# schema-2 database, which this install builds, and nothing else from here.
# Installing it from this script would mean re-implementing that product's
# verified-wheel and image-build steps in a second repository, where they would
# drift, so this script routes to that product's own entry instead.
telegram_product_repo=https://github.com/My-Denia/WuwaTerm-Telegram
web_product_repo=https://github.com/My-Denia/WuwaTerm-Web

# The releases these instructions name. contracts/products.json is the recorded
# snapshot and docs/products.md points readers at it, so when that file can be
# read its release tags ARE the default: a release bump then lands in one place
# instead of three. The constants stay for a checkout or a host without python3.
DEFAULT_TELEGRAM_VERSION=v0.2.9
DEFAULT_WEB_VERSION=v0.2.0

recorded_product_tag() {
    [ -f "$code_root/contracts/products.json" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$code_root/contracts/products.json" "$1" 2>/dev/null <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        snapshot = json.load(stream)
    products = snapshot.get("products")
except (OSError, ValueError, AttributeError):
    raise SystemExit(1)
if not isinstance(products, list):
    raise SystemExit(1)
for product in products:
    if isinstance(product, dict) and product.get("id") == sys.argv[2]:
        release = product.get("release")
        tag = release.get("tag") if isinstance(release, dict) else None
        if isinstance(tag, str) and tag.startswith("v") and all(part.isdigit() for part in tag[1:].split(".")):
            print(tag)
            raise SystemExit(0)
raise SystemExit(1)
PY
}

telegram_product_default=$(recorded_product_tag telegram) || telegram_product_default=$DEFAULT_TELEGRAM_VERSION
web_product_default=$(recorded_product_tag web) || web_product_default=$DEFAULT_WEB_VERSION
telegram_product_release=${WUWATERM_TELEGRAM_VERSION:-$telegram_product_default}
web_product_release=${WUWATERM_WEB_VERSION:-$web_product_default}

# Only the bot's own settings travel to the product .env. The Web and API
# secrets in this .env belong to other processes; the product's Compose file
# blanks them, and copying them would hand a bot a surface it does not have.
# WUWATERM_SEMANTIC_REVIEW_ENABLED is deliberately absent: the product keeps its
# model review track off for production and public bots, so turning it on is a
# decision for that checkout, not a copy of whatever this one does.
telegram_env_keys() {
    cat <<'EOF'
TELEGRAM_BOT_TOKEN
OWNER_USER_ID
WUWATERM_OPENAI_BASE_URL
WUWATERM_OPENAI_API_KEY
WUWATERM_OPENAI_MODEL
WUWATERM_LOG_LEVEL
WUWATERM_LLM_TIMEOUT_SECONDS
WUWATERM_LLM_MAX_CONCURRENCY
WUWATERM_RATE_LIMIT_PER_MINUTE
WUWATERM_REDACTION_SECRET
WUWATERM_TR_REJECT_SILENT
WUWATERM_GROUP_TR_REJECT_TEXT
WUWATERM_PRIVATE_TR_REJECT_TEXT
WUWATERM_CHANNEL_AUTOTRANSLATE
WUWATERM_CHANNEL_MIN_CJK
WUWATERM_CHANNEL_MIN_LATIN
WUWATERM_CHANNEL_TEXT_LIMIT
WUWATERM_CHANNEL_CAPTION_LIMIT
WUWATERM_CHANNEL_MAX_PENDING
WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE
WUWATERM_CHANNEL_MAX_AGE_SECONDS
EOF
}

# Copy the values this installation already has, and only those: a key that is
# empty or absent here stays out of the file, so the product's own defaults
# apply instead of an empty string that reads as unset anyway.
write_telegram_env_body() {
    telegram_env_keys | while IFS= read -r key; do
        value=$(env_value "$key" || true)
        [ -n "$value" ] || continue
        printf '%s=%s\n' "$key" "$value"
    done
}

write_telegram_env() {
    target=$1
    scratch="$target.$$"
    # umask before the redirect: the file holds a bot token, so it must never
    # exist with wider permissions, not even for the moment before a chmod.
    if ! (
        umask 077
        exec >"$scratch"
        printf '# Written by deploy/setup.sh from the Core .env. The Compose file\n'
        printf '# under deploy/ mounts data/ read-only and state/ read-write.\n'
        write_telegram_env_body
    ); then
        rm -f "$scratch"
        fail "cannot write $target"
    fi
    mv "$scratch" "$target" || { rm -f "$scratch"; fail "cannot write $target"; }
    chmod 600 "$target"
}

# Resolved before the data build for the same reason the Web package is: a
# wrong path is a typo, and it should stop the install instead of surfacing
# after a gigabyte of upstream data was fetched.
resolve_telegram_dir() {
    [ -n "$telegram_dir" ] || return 0
    [ -d "$telegram_dir" ] \
        || fail "--telegram-dir does not exist: $telegram_dir. Clone the product first: git clone --branch $telegram_product_release --depth 1 $telegram_product_repo <dir>"
    telegram_checkout=$(CDPATH= cd -- "$telegram_dir" && pwd -P)
    [ "$telegram_checkout" != "$code_root" ] \
        || fail "--telegram-dir must be the WuwaTerm-Telegram checkout, not this Core checkout: the two products never share a directory"
    [ -f "$telegram_checkout/deploy/docker-compose.yml" ] \
        || fail "$telegram_checkout is not a WuwaTerm-Telegram checkout: deploy/docker-compose.yml is missing"
    [ -f "$telegram_checkout/core-lock.json" ] \
        || fail "$telegram_checkout is not a WuwaTerm-Telegram checkout: core-lock.json is missing"
}

# The product mounts ../data and ../state relative to its own deploy/, so the
# database and the state directory have to be inside that checkout. Both are
# this installation's to give: the database is built here and the bot only
# reads it, and the state directory is the product's own.
prepare_telegram_checkout() {
    [ -f "$code_root/data/terms.db" ] \
        || fail "$code_root/data/terms.db is missing; run setup without --telegram-dir first so the database is built"
    mkdir -p "$telegram_checkout/data" "$telegram_checkout/state"
    chmod 700 "$telegram_checkout/state"
    if [ -e "$telegram_checkout/data/terms.db" ]; then
        printf 'setup: %s/data/terms.db already present; keeping it.\n' "$telegram_checkout"
    else
        cp "$code_root/data/terms.db" "$telegram_checkout/data/terms.db" \
            || fail "cannot copy the database into $telegram_checkout/data"
        printf 'setup: copied the database to %s/data/terms.db.\n' "$telegram_checkout"
    fi
    if [ -e "$telegram_checkout/.env" ]; then
        printf 'setup: %s/.env already present; keeping it.\n' "$telegram_checkout"
        return 0
    fi
    write_telegram_env "$telegram_checkout/.env"
    printf 'setup: wrote %s/.env (mode 0600) from this installation.\n' "$telegram_checkout"
}

# The commands below are the product's own documented path, printed with this
# installation's paths filled in. Its README at that tag stays authoritative:
# if a flag moved, follow the README instead of this printout.
print_telegram_steps() {
    wheel=$1
    where=$2
    prepared=$3
    download="$telegram_product_repo/releases/download/$telegram_product_release"
    step=1
    if [ "$prepared" = 0 ]; then
        printf 'setup: %s. fetch the product at its release tag, in its own directory:\n' "$step"
        printf 'setup:      git clone --branch %s --depth 1 %s %s\n' \
            "$telegram_product_release" "$telegram_product_repo" "$where"
        step=$((step + 1))
        printf 'setup: %s. give it a schema-2 database; this installation built one:\n' "$step"
        printf 'setup:      mkdir -p %s/data && cp %s/data/terms.db %s/data/terms.db\n' \
            "$where" "$code_root" "$where"
        step=$((step + 1))
        printf 'setup: %s. write %s/.env at mode 0600 with TELEGRAM_BOT_TOKEN,\n' "$step" "$where"
        printf 'setup:    OWNER_USER_ID and the model settings, and keep it out of git.\n'
        printf 'setup:      cp %s/deploy/env.example %s/.env\n' "$where" "$where"
        step=$((step + 1))
    fi
    printf 'setup: %s. verify the Telegram wheel against the release checksums:\n' "$step"
    printf 'setup:      cd %s && mkdir -p artifacts\n' "$where"
    printf 'setup:      curl -fsSLo artifacts/%s %s/%s\n' "$wheel" "$download" "$wheel"
    printf 'setup:      curl -fsSLo artifacts/SHA256SUMS %s/SHA256SUMS\n' "$download"
    printf 'setup:      (cd artifacts && grep " %s$" SHA256SUMS | sha256sum -c -)\n' "$wheel"
    printf "setup:      telegram_sha=\$(awk '\$2 == \"%s\" {print \$1}' artifacts/SHA256SUMS)\n" "$wheel"
    step=$((step + 1))
    printf 'setup: %s. fetch the pinned Core wheel and build the verified context:\n' "$step"
    printf 'setup:      python3 scripts/fetch_core.py --lock core-lock.json --output-dir artifacts\n'
    printf 'setup:      python3 scripts/prepare_docker_context.py --lock core-lock.json \\\n'
    printf 'setup:          --core artifacts/wuwaterm-*.whl --telegram artifacts/%s \\\n' "$wheel"
    printf "setup:          --telegram-sha256 \"\$telegram_sha\" --output-dir artifacts/docker-context\n"
    step=$((step + 1))
    printf 'setup: %s. start it with the product Compose file. Poll health is opt-in\n' "$step"
    printf 'setup:    and read from the environment when the container starts. `-p\n'
    printf 'setup:    wuwaterm-telegram` pins the Compose project name so it stays\n'
    printf 'setup:    separate from this checkout\047s `deploy` project; releases\n'
    printf 'setup:    without an explicit container_name name the container\n'
    printf 'setup:    `<project>-<service>-1` (until one sets `wuwaterm-telegram`):\n'
    printf 'setup:      WUWATERM_POLL_HEALTH=1 docker compose -p wuwaterm-telegram -f deploy/docker-compose.yml up --build -d\n'
    printf 'setup:    then check the running container:\n'
    printf 'setup:      docker compose -p wuwaterm-telegram -f deploy/docker-compose.yml exec -T wuwaterm-telegram \\\n'
    printf 'setup:          python -B -m wuwaterm_telegram.operations health\n'
}

install_telegram() {
    [ "$with_telegram" = 1 ] || return 0
    case "$telegram_product_release" in
        v[0-9]*) ;;
        *) fail "WUWATERM_TELEGRAM_VERSION must look like v0.2.9, not: $telegram_product_release" ;;
    esac
    wheel="wuwaterm_telegram-${telegram_product_release#v}-py3-none-any.whl"
    if [ -n "$telegram_dir" ]; then
        prepare_telegram_checkout
        where=$telegram_checkout
        prepared=1
    else
        where='<product-dir>'
        prepared=0
    fi
    printf '\nsetup: the Telegram bot is the standalone product %s,\n' "$telegram_product_repo"
    printf 'setup: release %s. This checkout starts no bot.\n' "$telegram_product_release"
    print_telegram_steps "$wheel" "$where" "$prepared"
    printf '\nsetup: the bot uses the Core library from its own pinned wheel, so the API\n'
    printf 'setup: above is not in its path and does not have to be published for it.\n'
    if [ "$(container_state wuwaterm-bot)" = running ]; then
        printf 'setup: the deprecated inline bot is running here; stop it before the product\n'
        printf 'setup: polls the same token: docker compose -f deploy/docker-compose.yml stop wuwaterm\n'
    fi
}

# Kept for existing installations and for an explicit rollback. The install
# entry no longer reaches it by accident: a first install starts no bot, and
# this flag is the only thing in this script that starts the inline one.
enable_legacy_bot() {
    [ "$with_legacy_bot" = 1 ] || return 0
    printf 'setup: warning: --with-legacy-bot starts the deprecated inline bot (wuwaterm-bot).\n' >&2
    printf 'setup: warning: the supported Telegram surface is the standalone product: ./deploy/setup.sh --with-telegram\n' >&2
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
    printf 'setup: deprecated inline bot (wuwaterm-bot) is running.\n'
}

web_variant() {
    if [ -n "$web_prefix" ]; then
        printf 'prefix'
    else
        printf 'root'
    fi
}

# Print one dotted string field of a JSON document. Exit status says whether
# the field exists, so an empty string stays distinguishable from an absent
# one: a root-path Web bundle records "base_path": "".
manifest_string() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

path, dotted = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)
for part in dotted.split("."):
    if not isinstance(value, dict):
        raise SystemExit(1)
    value = value.get(part)
if not isinstance(value, str):
    raise SystemExit(1)
print(value)
PY
}

# Read one var from a private env file without evaluating it.
file_value() {
    [ -f "$1" ] || return 1
    sed -n "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*//p" "$1" | tail -n 1 | tr -d '\r'
}

sha256_of() {
    sha256sum "$1" | awk '{print $1}'
}

# The launcher is what makes a directory a Web bundle. A published archive
# extracts to bundle/start.mjs; a plain build output keeps it at the top.
bundle_root() {
    if [ -f "$1/bundle/start.mjs" ]; then
        printf '%s/bundle' "$1"
    elif [ -f "$1/start.mjs" ]; then
        printf '%s' "$1"
    fi
}

# The published manifest records the path prefix the bundle was built for, and
# so does the bundle itself in BUILD-INFO.json. A mismatch would serve assets
# from the wrong location, so refuse the package instead of starting it.
check_web_variant() {
    if recorded=$(manifest_string "$1/BUILD-INFO.json" base_path); then
        :
    elif recorded=$(manifest_string "$2" base_path); then
        :
    else
        return 0
    fi
    [ "$recorded" = "$web_prefix" ] \
        || fail "the Web bundle was built for base path ${recorded:-<root>}, but this install uses ${web_prefix:-<root>}; fetch the $variant package that matches"
}

resolve_web_package() {
    command -v python3 >/dev/null 2>&1 \
        || fail "python3 is required to read the Web package manifest"
    variant=$(web_variant)
    state="$code_root/state-web"
    mkdir -p "$state"
    chmod 700 "$state"
    package_root="$state/package"
    manifest="$state/manifest-$variant.json"
    if [ -z "$web_package" ] && [ -n "$(bundle_root "$package_root")" ]; then
        # A package installed by an earlier run stays usable without network.
        # Remove state-web/package to install the current release instead.
        printf 'setup: reusing the installed Web package at %s.\n' "$package_root"
    elif [ -n "$web_package" ]; then
        [ -e "$web_package" ] || fail "--web-package path does not exist: $web_package"
        if [ -d "$web_package" ]; then
            package_root=$(CDPATH= cd -- "$web_package" && pwd -P)
            if [ -f "$package_root/manifest-$variant.json" ]; then
                manifest="$package_root/manifest-$variant.json"
            fi
        elif [ -f "$web_package" ]; then
            rm -rf "$package_root"
            mkdir -p "$package_root"
            tar -xzf "$web_package" -C "$package_root" \
                || fail "could not extract $web_package"
        else
            fail "--web-package must be a directory or a .tar.gz/.tgz archive"
        fi
    else
        # The published Web release this install verifies. `latest` follows the
        # repository's newest release; a tag pins one, which is what a recorded
        # products.json tag does.
        case "$web_product_release" in
            latest) base="$web_product_repo/releases/latest/download" ;;
            v[0-9]*) base="$web_product_repo/releases/download/$web_product_release" ;;
            *) fail "WUWATERM_WEB_VERSION must be latest or a tag like v0.2.0, not: $web_product_release" ;;
        esac
        manifest_url="$base/manifest-$variant.json"
        sums_url="$base/SHA256SUMS-$variant"
        if ! curl -fsSL --max-time 30 "$manifest_url" -o "$manifest"; then
            fail "no published WuwaTerm-Web package was found ($manifest_url). Download a package from its repository and pass --web-package, or install the Core API and the Web product separately."
        fi
        curl -fsSL --max-time 30 "$sums_url" -o "$state/SHA256SUMS-$variant" \
            || fail "the Web release does not publish SHA256SUMS-$variant; refusing an unverified package"
        expected_manifest=$(awk -v name="manifest-$variant.json" \
            '$2 == name || $2 == "*" name {print $1}' \
            "$state/SHA256SUMS-$variant" | head -n 1)
        if [ -n "$expected_manifest" ]; then
            [ "$expected_manifest" = "$(sha256_of "$manifest")" ] \
                || fail "the Web manifest checksum does not match SHA256SUMS-$variant"
        fi
        # v0.1.0 records the file under archive.name; artifact is the older
        # top-level field and is accepted so an earlier package still works.
        if artifact=$(manifest_string "$manifest" archive.name); then
            :
        elif artifact=$(manifest_string "$manifest" artifact); then
            :
        else
            fail "the Web manifest names no archive (expected archive.name)"
        fi
        case "$artifact" in
            ""|*/*|*..*|.*) fail "the Web manifest names an unsafe artifact file" ;;
        esac
        curl -fsSL --max-time 600 "$base/$artifact" -o "$state/$artifact" \
            || fail "could not download the Web package $artifact"
        expected=$(awk -v name="$artifact" '$2 == name || $2 == "*" name {print $1}' \
            "$state/SHA256SUMS-$variant" | head -n 1)
        [ -n "$expected" ] || fail "SHA256SUMS-$variant does not list $artifact"
        actual=$(sha256_of "$state/$artifact")
        [ "$expected" = "$actual" ] || fail "the Web package checksum does not match SHA256SUMS-$variant"
        if recorded=$(manifest_string "$manifest" archive.sha256); then
            [ "$recorded" = "$actual" ] \
                || fail "the Web manifest archive digest does not match the downloaded package"
        fi
        rm -rf "$package_root"
        mkdir -p "$package_root"
        tar -xzf "$state/$artifact" -C "$package_root" \
            || fail "could not extract the Web package"
    fi
    web_bundle=$(bundle_root "$package_root")
    [ -n "$web_bundle" ] \
        || fail "the Web package has no start.mjs (expected bundle/start.mjs or start.mjs under $package_root); use the self-host release task of WuwaTerm-Web"
    check_web_variant "$web_bundle" "$manifest"
    # The v0.1.0 manifest records the archive and the build prefix; the start
    # command, port and responses belong to the bundle, so those fields are
    # optional and a package that omits them still installs.
    if web_start=$(manifest_string "$manifest" start); then
        [ -n "$web_start" ] || web_start='node start.mjs'
    else
        web_start='node start.mjs'
    fi
    if web_port=$(manifest_string "$manifest" port); then
        case "$web_port" in
            ''|*[!0-9]*) fail "the Web manifest port must be a number" ;;
        esac
    else
        web_port=3000
    fi
    [ "$web_port" -ge 1 ] && [ "$web_port" -le 65535 ] \
        || fail "the Web manifest port must be 1..65535"
}

# WuwaTerm-Web answers 403 without the edge header that the authenticated
# proxy is required to set, so readiness is an authenticated request: the page
# has to mint a session cookie and /api/pool has to answer 200 with it. Both
# calls read the header from stdin so the secret never appears in argv.
wait_for_web() {
    port=$1
    prefix=$2
    edge=$3
    base="http://127.0.0.1:$port$prefix"
    jar="$code_root/state-web/.probe-cookies.$$"
    tries=0
    while [ "$tries" -lt 60 ]; do
        : >"$jar"
        if printf 'X-WuwaTerm-Edge: %s\n' "$edge" | \
                curl -fsS --max-time 5 -H @- -c "$jar" -o /dev/null \
                    "$base/" 2>/dev/null \
            && printf 'X-WuwaTerm-Edge: %s\n' "$edge" | \
                curl -fsS --max-time 5 -H @- -b "$jar" -o /dev/null \
                    "$base/api/pool" 2>/dev/null; then
            rm -f "$jar"
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
    rm -f "$jar"
    fail "wuwaterm-web did not serve an authenticated page and /api/pool at $base/; check docker logs wuwaterm-web, the edge secret and the Core device credential"
}

# The Web runtime creates its SQLite admission database at
# WUWATERM_LOCAL_DB_PATH, so the host data directory is mounted at that file's
# directory and the container needs no writable path of its own. The default
# sits outside the read-only /app bundle mount because Docker refuses to create
# a mountpoint below a read-only parent ("create mountpoint for /app/data ...
# read-only file system", Docker 29.1.3). An installation whose web.env still
# names /app/data keeps that path and gets its mountpoint created instead.
web_db_dir() {
    db_path=$(file_value "$1" WUWATERM_LOCAL_DB_PATH || true)
    [ -n "$db_path" ] || db_path=/var/lib/wuwaterm-web/pool.sqlite3
    case "$db_path" in
        /*) ;;
        *) fail "WUWATERM_LOCAL_DB_PATH in $1 must be an absolute path: $db_path" ;;
    esac
    case "$db_path" in
        */|*/../*|*/..) fail "WUWATERM_LOCAL_DB_PATH in $1 must name a file below a directory: $db_path" ;;
    esac
    db_dir=$(dirname "$db_path")
    [ "$db_dir" != "/" ] \
        || fail "WUWATERM_LOCAL_DB_PATH in $1 must name a file below a directory: $db_path"
    printf '%s' "$db_dir"
}

enable_web() {
    [ "$with_web" = 1 ] || return 0
    web_env="$code_root/state-web/web.env"
    data_dir="$code_root/state-web/data"
    mkdir -p "$data_dir"
    chmod 700 "$data_dir"
    if [ ! -f "$web_env" ]; then
        # The runtime accepts 32-128 letters, digits, underscores or hyphens, so
        # a base64 secret with '+' or '/' would be refused at startup.
        edge_secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
        [ -n "$edge_secret" ] || fail "could not generate a Web edge secret"
        api_device_file="$code_root/state-web/device.token"
        issue_device "web" "translate,meta" "$api_device_file"
        web_token=$(cat "$api_device_file")
        (
            umask 077
            {
                printf 'WUWATERM_RUNTIME_MODE=selfhost\n'
                printf 'WUWATERM_WEB_ORIGIN=%s\n' "$web_origin"
                printf 'WUWATERM_WEB_EDGE_SECRET=%s\n' "$edge_secret"
                printf 'WUWATERM_WEB_BASE_PATH=%s\n' "$web_prefix"
                printf 'WUWATERM_API_BASE_URL=http://127.0.0.1:%s\n' "$(api_port)"
                printf 'WUWATERM_SITE_DEVICE_TOKEN=%s\n' "$web_token"
                printf 'WUWATERM_TRANSLATION_ENABLED=true\n'
                printf 'WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/pool.sqlite3\n'
            } >"$web_env"
        )
        chmod 600 "$web_env"
        printf 'setup: wrote Web credentials to %s (mode 600).\n' "$web_env"
    fi
    edge_secret=$(file_value "$web_env" WUWATERM_WEB_EDGE_SECRET || true)
    case "$edge_secret" in
        ''|*[!A-Za-z0-9_-]*)
            fail "WUWATERM_WEB_EDGE_SECRET in $web_env must be 32-128 letters, digits, underscores or hyphens before the Web process will start; change it there and in the reverse proxy." ;;
    esac
    [ "${#edge_secret}" -ge 32 ] && [ "${#edge_secret}" -le 128 ] \
        || fail "WUWATERM_WEB_EDGE_SECRET in $web_env must be 32-128 characters; change it there and in the reverse proxy."
    web_data_dir=$(web_db_dir "$web_env")
    case "$web_data_dir" in
        /app|/app/*)
            # Docker will not create a bind mountpoint below a read-only
            # mount, so a Web database inside /app needs the directory to
            # exist in the extracted bundle before the container starts.
            web_data_target="$web_bundle${web_data_dir#/app}"
            if ! mkdir -p "$web_data_target" 2>/dev/null \
                || [ ! -d "$web_data_target" ]; then
                fail "cannot create $web_data_target so Docker can mount $web_data_dir; set WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/pool.sqlite3 in $web_env, or use a Web package directory this account can write"
            fi
            ;;
    esac
    node_image=${WUWATERM_WEB_NODE_IMAGE:-node:24-alpine}
    docker rm -f wuwaterm-web >/dev/null 2>&1 || true
    docker run -d --name wuwaterm-web --restart unless-stopped --network host \
        --env-file "$web_env" -e "PORT=$web_port" \
        -v "$web_bundle:/app:ro" -v "$data_dir:$web_data_dir:rw" \
        -w /app "$node_image" sh -c "$web_start" >/dev/null \
        || fail "could not start the Web workbench"
    (umask 077; printf '%s\n' "$web_port" >"$code_root/state-web/web.port")
    wait_for_web "$web_port" "$web_prefix" "$edge_secret"
    printf 'setup: Web workbench is running at http://127.0.0.1:%s%s/ behind your authenticated proxy.\n' "$web_port" "$web_prefix"
}

is_adopted() {
    # Adopted means a transaction can actually be named: the pointer holds a
    # deployment id and the manifest that id refers to is on disk. The
    # .deployments DIRECTORY says nothing — deploy/product-update.sh creates it
    # together with its lock file BEFORE adopt runs, so a first install with no
    # legacy .deploy_commit is left with an empty directory when adoption fails.
    # Reading the directory as "adopted" reported updates as adopted on a
    # clean install and skipped the retry that would have adopted it.
    [ -s "$code_root/.deploy_current" ] || return 1
    [ -s "$code_root/.deploy_commit" ] || return 1
    deployment_id=$(cat "$code_root/.deploy_current" 2>/dev/null) || return 1
    case "$deployment_id" in
        ''|*[!0-9A-Za-z-]*) return 1 ;;
    esac
    # A product deployment writes .deployments/products/<id>.json; a
    # runtime-only deployment updates the component journal in place.
    [ -s "$code_root/.deployments/products/$deployment_id.json" ] \
        || [ -s "$code_root/.deployments/.component-runtime-transaction.json" ]
}

maybe_adopt() {
    [ "$do_adopt" = 1 ] || return 0
    [ -f "$code_root/deploy/product-update.sh" ] || return 0
    if is_adopted; then
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

# A release with an explicit container_name runs as `wuwaterm-telegram`; an
# earlier release without one runs as `<project>-wuwaterm-telegram-1` (such as
# `deploy-wuwaterm-telegram-1` or `wuwaterm-telegram-wuwaterm-telegram-1`).
# Compose labels every one of them with the service name, and a running
# container takes precedence over a leftover stopped one.
standalone_telegram_state() {
    sts_first=$(container_state wuwaterm-telegram)
    if [ "$sts_first" = running ]; then
        printf '%s' running
        return 0
    fi
    sts_ids=$(docker ps -aq --filter label=com.docker.compose.service=wuwaterm-telegram \
        2>/dev/null || true)
    for sts_id in $sts_ids; do
        sts_state=$(container_state "$sts_id")
        if [ "$sts_state" = running ]; then
            printf '%s' running
            return 0
        fi
        if [ -z "$sts_first" ] && [ -n "$sts_state" ]; then
            sts_first=$sts_state
        fi
    done
    if [ -n "$sts_first" ]; then
        printf '%s' "$sts_first"
    fi
    return 0
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
    product_state=$(standalone_telegram_state)
    if [ -n "$product_state" ]; then
        printf 'telegram:     %s (standalone product)\n' "$product_state"
    else
        printf 'telegram:     not installed (standalone product)\n'
    fi
    bot_state=$(container_state wuwaterm-bot)
    if [ -n "$bot_state" ]; then
        printf 'legacy bot:   %s (deprecated inline bot)\n' "$bot_state"
    else
        printf 'legacy bot:   not installed\n'
    fi
    if [ -f "$code_root/state-web/web.env" ]; then
        web_state=$(container_state wuwaterm-web)
        printf 'web:          %s\n' "${web_state:-not running}"
        web_port_value=$(cat "$code_root/state-web/web.port" 2>/dev/null || true)
        [ -n "$web_port_value" ] || web_port_value=3000
        web_prefix_value=$(file_value "$code_root/state-web/web.env" WUWATERM_WEB_BASE_PATH || true)
        web_edge_value=$(file_value "$code_root/state-web/web.env" WUWATERM_WEB_EDGE_SECRET || true)
        # The page is edge-gated: a bare GET would report 403 on a healthy
        # service, so the probe carries the header from stdin.
        if [ -n "$web_edge_value" ] \
            && printf 'X-WuwaTerm-Edge: %s\n' "$web_edge_value" | \
                curl -fsS --max-time 3 -H @- -o /dev/null \
                    "http://127.0.0.1:$web_port_value$web_prefix_value/" 2>/dev/null; then
            printf 'web health:   ready (http://127.0.0.1:%s%s/)\n' \
                "$web_port_value" "$web_prefix_value"
        else
            printf 'web health:   not ready (http://127.0.0.1:%s%s/)\n' \
                "$web_port_value" "$web_prefix_value"
        fi
    else
        printf 'web:          not configured\n'
    fi
    if is_adopted; then
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
# Resolve the Web package and the Telegram checkout before the data build: a
# missing package or a wrong path should stop the install instead of after a
# gigabyte of upstream data was fetched.
if [ "$with_web" = 1 ]; then
    resolve_web_package
fi
resolve_telegram_dir
build_data
start_api
ensure_api_device
install_telegram
enable_legacy_bot
enable_web
maybe_adopt

printf '\nsetup: done.\n'
report_status
