#!/bin/sh
# Official one-command install and opt-in enablement for this checkout.
#
#   ./deploy/setup.sh                      Core API only (shared translation service)
#   ./deploy/setup.sh --with-web --web-origin https://...
#                                          Core API + WuwaTerm-Web workbench
#   ./deploy/setup.sh --with-windows       Core API + Windows desktop token & pairing info
#   ./deploy/setup.sh --with-telegram      Core API + standalone WuwaTerm-Telegram (HTTP mode)
#   ./deploy/setup.sh --with-all --web-origin https://...
#                                          Core API + Web + Telegram + Windows
#   ./deploy/setup.sh --status | --check   report what is installed and healthy
#
# Re-running the same command is the supported way to add a surface to an
# existing installation: the .env, the data build, the credential store, the
# Web/Telegram secrets and the Compose services are only created when they are
# missing. Nothing here prints a secret; the operator receives file paths instead.
#
# Core (wuwaterm-api + data/terms.db) is the single shared base. Web, Windows
# and Telegram each receive an independent device token and state directory
# (state-api/, state-web/, state-telegram/) and consume Core over HTTP /v1.
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
telegram_dir=
telegram_package=
with_web=0
web_prefix=
web_package=
web_origin=
with_windows=0
build_local=0
image_tag=${WUWATERM_IMAGE_TAG:-}
mode=install
check_strict=0
do_adopt=1

usage() {
    cat <<'EOF'
Usage: ./deploy/setup.sh [options]

Start the shared Core translation service (wuwaterm-api + data/terms.db), and
add any combination of client surfaces.

  (no flag)             Core HTTP API only: dictionary lookup, translation,
                        review, device credentials. No bot is started, so an
                        API-only installation cannot fall into a bot restart
                        loop.
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
  --with-windows        Also issue a dedicated Windows desktop device token
                        (state-api/windows-device.token, mode 0600) and print
                        the verified WuwaTerm-Windows release download, SHA-256
                        and HTTPS reverse-proxy pairing instructions from
                        contracts/products.json.
  --with-telegram       Also orchestrate standalone WuwaTerm-Telegram in HTTP
                        mode against Core (requires >= v0.7.0, default v0.7.0,
                        or an explicit compatible --telegram-package; older
                        releases such as v0.6.1 have no HTTP backend and fail
                        closed): issues a dedicated telegram
                        device token (state-telegram/device.token, mode 0600),
                        writes state-telegram/telegram.env (mode 0600) with
                        WUWATERM_CORE_MODE=http, WUWATERM_CORE_API_URL and
                        WUWATERM_CORE_API_TOKEN, verifies image.oci.tar against
                        SHA256SUMS, mounts data/ read-only and
                        state-telegram/state/ read-write, and starts/health-
                        checks wuwaterm-telegram labeled
                        org.wuwaterm.managed-by=setup.sh. Requires
                        TELEGRAM_BOT_TOKEN in .env (or already in
                        state-telegram/telegram.env).
  --telegram-package PATH
                        Use a local WuwaTerm-Telegram OCI archive (image.oci.tar
                        or a directory containing image.oci.tar and optional
                        SHA256SUMS) instead of downloading the release asset.
                        Implies --with-telegram.
  --telegram-dir PATH   Legacy checkout-prep mode (implies --with-telegram):
                        writes a WuwaTerm-Telegram checkout's .env at mode 0600
                        from this .env, copies data/terms.db into it and prints
                        its wheel-build steps instead of starting a container.
                        Requires TELEGRAM_BOT_TOKEN in .env.
  --with-all            Idempotent combination of Core + Web + Telegram +
                        Windows (equivalent to --with-web --with-telegram
                        --with-windows). Each client gets its own device token
                        and state directory while sharing wuwaterm-api and
                        data/terms.db.
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
WUWATERM_API_KNOWLEDGE_DB_PATH (optional, e.g. /app/data/knowledge.db under
Compose) enables a companion read-only entity knowledge database bound to
data/terms.db's revision. When set in .env, setup.sh verifies an existing file
against data/terms.db or builds, verifies, and atomically installs it; when
unset or empty, single-DB behaviour is unchanged. Missing or revision-mismatched
knowledge.db at API runtime degrades safely to dictionary-only mode without
blocking wuwaterm-api startup.
WUWATERM_TELEGRAM_VERSION selects the WuwaTerm-Telegram release tag to install
(default: the tag contracts/products.json of this checkout records, v0.7.0 here;
the script's own constant when that file is absent).
WUWATERM_WEB_VERSION picks the WuwaTerm-Web release the Web install downloads:
v0.8.0, the current published release, or latest (default: the tag
contracts/products.json records, v0.8.0 here; the script's own constant when
that file is absent).
WUWATERM_WINDOWS_VERSION selects the WuwaTerm-Windows release tag named in
pairing instructions (default: the tag contracts/products.json records, v0.10.0
here; the script's own constant when that file is absent).
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
        --with-legacy-bot)
            usage_error "--with-legacy-bot has been removed: the legacy inline bot has been retired from Core; use --with-telegram (or roll back to v0.16.0)" ;;
        --telegram-dir)
            need_value "$@"; telegram_dir=$2; shift ;;
        --telegram-dir=*) telegram_dir=${1#--telegram-dir=} ;;
        --telegram-package)
            need_value "$@"; telegram_package=$2; shift ;;
        --telegram-package=*) telegram_package=${1#--telegram-package=} ;;
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
        --with-windows) with_windows=1 ;;
        --with-all)
            with_web=1
            with_telegram=1
            with_windows=1 ;;
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
# Preparing a Telegram checkout or passing a local OCI package asks for Telegram.
if [ -n "$telegram_dir" ] || [ -n "$telegram_package" ]; then
    with_telegram=1
fi
if [ -n "$telegram_dir" ] && [ -n "$telegram_package" ]; then
    usage_error "--telegram-dir and --telegram-package are alternatives"
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
    if [ "$with_telegram" = 1 ] && [ -z "$telegram_dir" ]; then
        mkdir -p "$code_root/state-telegram" "$code_root/state-telegram/state"
        chmod 700 "$code_root/state-telegram" "$code_root/state-telegram/state"
    fi
    if [ ! -f "$code_root/.env" ]; then
        [ -f "$code_root/deploy/env.example" ] || fail "deploy/env.example is missing"
        cp "$code_root/deploy/env.example" "$code_root/.env"
        printf 'setup: created .env from deploy/env.example; review the model settings.\n'
    fi
    chmod 600 "$code_root/.env"
}

# Read WUWATERM_API_KNOWLEDGE_DB_PATH from .env as plain data (without shell
# evaluation), stripping optional quotes, leading `export `, and unquoted
# trailing `#` comments (aligned with scripts/full_update.py::_parse_env_var).
env_kb_raw_value() {
    [ -f "$code_root/.env" ] || return 1
    awk '
        {
            line = $0
            sub(/\r$/, "", line)
            sub(/^[ \t]+/, "", line)
            sub(/[ \t]+$/, "", line)
            if (line == "" || substr(line, 1, 1) == "#") next
            if (substr(line, 1, 7) == "export ") {
                line = substr(line, 8)
                sub(/^[ \t]+/, "", line)
            }
            eq = index(line, "=")
            if (eq == 0) next
            name = substr(line, 1, eq - 1)
            sub(/[ \t]+$/, "", name)
            if (name != "WUWATERM_API_KNOWLEDGE_DB_PATH") next
            val = substr(line, eq + 1)
            sub(/^[ \t]+/, "", val)
            sub(/[ \t]+$/, "", val)
            first = substr(val, 1, 1)
            if (first == "\"" || first == "\047") {
                rest = substr(val, 2)
                endq = index(rest, first)
                if (endq > 0) {
                    trailing = substr(rest, endq + 1)
                    sub(/^[ \t]+/, "", trailing)
                    if (trailing == "" || substr(trailing, 1, 1) == "#") {
                        val = substr(rest, 1, endq - 1)
                        sub(/^[ \t]+/, "", val)
                        sub(/[ \t]+$/, "", val)
                    }
                }
            } else if (first == "#") {
                val = ""
            } else {
                if (match(val, /[ \t]+#/)) {
                    val = substr(val, 1, RSTART - 1)
                    sub(/[ \t]+$/, "", val)
                }
            }
            found = 1
            result = val
        }
        END {
            if (found) printf "%s", result
            else exit 1
        }
    ' "$code_root/.env"
}

# Resolve and validate WUWATERM_API_KNOWLEDGE_DB_PATH from .env.
# Sets kb_configured (0/1), kb_relpath (data/<rel>), kb_host_path, kb_path_error.
# Returns 0 when disabled or valid, 1 when configured to an invalid/unsafe path.
# Unset or explicit empty values leave single-DB behaviour untouched even if a
# leftover data/knowledge.db exists on disk.
resolve_knowledge_db_path() {
    kb_configured=0
    kb_relpath=
    kb_host_path=
    kb_path_error=
    if ! kb_raw=$(env_kb_raw_value); then
        return 0
    fi
    if [ -z "$kb_raw" ]; then
        return 0
    fi
    kb_configured=1
    if [ -L "$code_root/.env" ]; then
        kb_path_error=".env must not be a symlink when WUWATERM_API_KNOWLEDGE_DB_PATH is configured"
        return 1
    fi
    case "$kb_raw" in
        /app/data/*) kb_relpath="data/${kb_raw#/app/data/}" ;;
        data/*) kb_relpath="$kb_raw" ;;
        *)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must be under /app/data/ or data/ (got: $kb_raw)"
            return 1 ;;
    esac
    if [ "${#kb_relpath}" -gt 200 ]; then
        kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH exceeds 200 characters"
        return 1
    fi
    case "$kb_relpath" in
        *[[:space:]]*|*[[:cntrl:]]*|*\\*)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH contains invalid whitespace, control or backslash characters"
            return 1 ;;
    esac
    kb_rest=${kb_relpath#data/}
    case "$kb_rest" in
        ""|/*|*/|*//*)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH contains an empty or malformed path segment: $kb_relpath"
            return 1 ;;
    esac
    kb_first_seg=${kb_rest%%/*}
    case "$kb_first_seg" in
        candidates|deployment-backups|wutheringdata)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must not point inside data/$kb_first_seg/"
            return 1 ;;
    esac
    case "$kb_relpath" in
        data/terms.db|data/terms.candidate.db|data/knowledge.candidate.db)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must not point to $kb_relpath"
            return 1 ;;
        *-wal|*-shm|*-journal)
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must not point to a SQLite sidecar file"
            return 1 ;;
    esac
    if [ -L "$code_root/data" ]; then
        kb_path_error="data/ must not be a symlink when WUWATERM_API_KNOWLEDGE_DB_PATH is configured"
        return 1
    fi
    kb_cursor="$code_root/data"
    kb_remaining=$kb_rest
    while [ -n "$kb_remaining" ]; do
        case "$kb_remaining" in
            */*)
                kb_seg=${kb_remaining%%/*}
                kb_remaining=${kb_remaining#*/}
                kb_is_leaf=0 ;;
            *)
                kb_seg=$kb_remaining
                kb_remaining=
                kb_is_leaf=1 ;;
        esac
        case "$kb_seg" in
            ""|"."|"..")
                kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must not contain '.' or '..' segments"
                return 1 ;;
        esac
        kb_cursor="$kb_cursor/$kb_seg"
        if [ -L "$kb_cursor" ]; then
            kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH must not cross or target a symlink: $kb_relpath"
            return 1
        fi
        if [ "$kb_is_leaf" = 0 ]; then
            if [ -e "$kb_cursor" ] && [ ! -d "$kb_cursor" ]; then
                kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH parent is not a directory: $kb_relpath"
                return 1
            fi
        else
            if [ -e "$kb_cursor" ] && [ ! -f "$kb_cursor" ]; then
                kb_path_error="WUWATERM_API_KNOWLEDGE_DB_PATH target must be a regular file: $kb_relpath"
                return 1
            fi
        fi
    done
    kb_host_path="$code_root/$kb_relpath"
    return 0
}

validate_config() {
    # Sentence translation is the product; exact lookup alone needs no model,
    # but a configuration that cannot translate is not a finished install.
    require_env WUWATERM_OPENAI_BASE_URL
    require_env WUWATERM_OPENAI_API_KEY
    require_env WUWATERM_OPENAI_MODEL
    resolve_knowledge_db_path || fail "$kb_path_error"
    if [ -n "$telegram_dir" ]; then
        require_env TELEGRAM_BOT_TOKEN
        warn_if_empty OWNER_USER_ID
    elif [ "$with_telegram" = 1 ]; then
        existing_tg_token=$(file_value "$code_root/state-telegram/telegram.env" TELEGRAM_BOT_TOKEN || true)
        if [ -z "$existing_tg_token" ]; then
            require_env TELEGRAM_BOT_TOKEN
        fi
        existing_tg_owner=$(file_value "$code_root/state-telegram/telegram.env" OWNER_USER_ID || true)
        if [ -z "$existing_tg_owner" ]; then
            warn_if_empty OWNER_USER_ID
        fi
    fi
    if [ "$with_web" = 1 ]; then
        resolve_web_origin
    fi
    if [ "$with_windows" = 1 ]; then
        case "$windows_product_release" in
            v[0-9]*) ;;
            *) fail "WUWATERM_WINDOWS_VERSION must look like v0.10.0, not: $windows_product_release" ;;
        esac
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

ensure_builder_image() {
    [ "${builder_ready:-0}" = 1 ] && return 0
    if [ "$build_local" = 1 ]; then
        compose build wuwaterm-builder
    else
        compose pull wuwaterm-builder \
            || fail "could not pull the official builder image $image_tag; publish that release or pass --build-local"
    fi
    builder_ready=1
}

cleanup_kb_candidate() {
    [ -n "${kb_candidate_host:-}" ] || return 0
    rm -f \
        "$kb_candidate_host" \
        "${kb_candidate_host}-wal" \
        "${kb_candidate_host}-shm" \
        "${kb_candidate_host}-journal"
}

reject_kb_destination_sidecars() {
    kb_found_sidecars=
    for kb_sc_suffix in -wal -shm -journal; do
        kb_sc_path="${kb_host_path}${kb_sc_suffix}"
        if [ -e "$kb_sc_path" ] || [ -L "$kb_sc_path" ]; then
            if [ -n "$kb_found_sidecars" ]; then
                kb_found_sidecars="${kb_found_sidecars}, ${kb_relpath}${kb_sc_suffix}"
            else
                kb_found_sidecars="${kb_relpath}${kb_sc_suffix}"
            fi
        fi
    done
    if [ -n "$kb_found_sidecars" ]; then
        cleanup_kb_candidate
        fail "refusing to reuse or promote $kb_relpath while leftover SQLite sidecar file(s) exist ($kb_found_sidecars); stop any writer process and safely checkpoint or recover the database before retrying"
    fi
}

build_data() {
    builder_ready=0
    data_refreshed=0
    # Stage 1: terminology database (terms.db).
    if [ -f "$code_root/data/terms.db" ]; then
        printf 'setup: data/terms.db already present; keeping it.\n'
    else
        printf 'setup: data/terms.db is missing; building it from the pinned upstream source.\n'
        printf 'setup: this fetches roughly a gigabyte and can fail part-way; re-running resumes it.\n'
        ensure_builder_image
        compose run --rm wuwaterm-builder refresh-data
        data_refreshed=1
        compose run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder build-db --atomic
        compose run --rm -e WUWATERM_DB_PATH=/app/data/terms.candidate.db wuwaterm-builder verify-db
        [ -f "$code_root/data/terms.candidate.db" ] \
            || fail "the builder did not produce data/terms.candidate.db"
        if [ -L "$code_root/data/terms.db" ] || [ -e "$code_root/data/terms.db" ]; then
            fail "data/terms.db appeared while building; refusing to overwrite it"
        fi
        mv "$code_root/data/terms.candidate.db" "$code_root/data/terms.db"
        printf 'setup: data/terms.db is in place.\n'
    fi

    # Stage 2: optional companion knowledge database (only when explicitly configured in .env).
    kb_candidate_host=
    resolve_knowledge_db_path || fail "$kb_path_error"
    [ "$kb_configured" = 1 ] || return 0
    reject_kb_destination_sidecars
    ensure_builder_image
    if [ -f "$kb_host_path" ]; then
        if compose run --rm \
            -e WUWATERM_DB_PATH=/app/data/terms.db \
            -e "WUWATERM_KNOWLEDGE_DB_PATH=/app/$kb_relpath" \
            wuwaterm-builder verify-kb; then
            printf 'setup: %s already present and verified against data/terms.db; keeping it.\n' "$kb_relpath"
            return 0
        fi
        printf 'setup: %s failed verification against data/terms.db; rebuilding it.\n' "$kb_relpath"
    else
        printf 'setup: %s is missing; building companion knowledge database against data/terms.db.\n' "$kb_relpath"
    fi
    if [ "$data_refreshed" != 1 ] && [ ! -d "$code_root/data/wutheringdata" ]; then
        printf 'setup: data/wutheringdata is missing; refreshing pinned upstream source for %s.\n' "$kb_relpath"
        compose run --rm wuwaterm-builder refresh-data
        data_refreshed=1
    fi
    kb_candidate_host="$code_root/data/knowledge.candidate.db"
    cleanup_kb_candidate
    if ! compose run --rm \
        -e WUWATERM_DB_PATH=/app/data/terms.db \
        -e WUWATERM_KNOWLEDGE_DB_PATH=/app/data/knowledge.candidate.db \
        wuwaterm-builder build-kb --atomic; then
        cleanup_kb_candidate
        fail "the builder failed to build $kb_relpath"
    fi
    if ! compose run --rm \
        -e WUWATERM_DB_PATH=/app/data/terms.db \
        -e WUWATERM_KNOWLEDGE_DB_PATH=/app/data/knowledge.candidate.db \
        wuwaterm-builder verify-kb; then
        cleanup_kb_candidate
        fail "the built candidate for $kb_relpath failed verify-kb against data/terms.db"
    fi
    if [ -L "$kb_candidate_host" ] || [ ! -f "$kb_candidate_host" ]; then
        cleanup_kb_candidate
        fail "the builder did not produce a regular file at data/knowledge.candidate.db"
    fi
    if ! resolve_knowledge_db_path; then
        cleanup_kb_candidate
        fail "$kb_path_error"
    fi
    kb_parent=$(dirname -- "$kb_host_path")
    mkdir -p "$kb_parent"
    if [ -L "$kb_parent" ] || [ -L "$kb_host_path" ] || { [ -e "$kb_host_path" ] && [ ! -f "$kb_host_path" ]; }; then
        cleanup_kb_candidate
        fail "refusing to promote companion knowledge database across unsafe target: $kb_relpath"
    fi
    reject_kb_destination_sidecars
    if ! mv -f "$kb_candidate_host" "$kb_host_path"; then
        cleanup_kb_candidate
        fail "could not promote data/knowledge.candidate.db to $kb_relpath"
    fi
    printf 'setup: %s is in place.\n' "$kb_relpath"
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
windows_product_repo=https://github.com/My-Denia/WuwaTerm-Windows

# The releases these instructions name. contracts/products.json is the recorded
# snapshot and docs/products.md points readers at it, so when that file can be
# read its release tags ARE the default: a release bump then lands in one place
# instead of three. The constants stay for a checkout or a host without python3.
DEFAULT_TELEGRAM_VERSION=v0.7.0
DEFAULT_WEB_VERSION=v0.8.0
DEFAULT_WINDOWS_VERSION=v0.10.0

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

recorded_product_asset_sha256() {
    [ -f "$code_root/contracts/products.json" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$code_root/contracts/products.json" "$1" "$2" "$3" 2>/dev/null <<'PY'
import json
import sys

path, product_id, expected_tag, asset_name = sys.argv[1:5]
try:
    with open(path, encoding="utf-8") as stream:
        snapshot = json.load(stream)
    products = snapshot.get("products")
except (OSError, ValueError, AttributeError):
    raise SystemExit(1)
if not isinstance(products, list):
    raise SystemExit(1)
for product in products:
    if isinstance(product, dict) and product.get("id") == product_id:
        release = product.get("release")
        if not isinstance(release, dict) or release.get("tag") != expected_tag:
            raise SystemExit(1)
        digests = release.get("asset_sha256")
        if isinstance(digests, dict):
            digest = digests.get(asset_name)
            if isinstance(digest, str) and len(digest) == 64:
                print(digest)
                raise SystemExit(0)
raise SystemExit(1)
PY
}

recorded_windows_zip_name() {
    [ -f "$code_root/contracts/products.json" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$code_root/contracts/products.json" "$1" 2>/dev/null <<'PY'
import json
import sys

path, expected_tag = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as stream:
        snapshot = json.load(stream)
    products = snapshot.get("products")
except (OSError, ValueError, AttributeError):
    raise SystemExit(1)
if not isinstance(products, list):
    raise SystemExit(1)
for product in products:
    if isinstance(product, dict) and product.get("id") == "windows":
        release = product.get("release")
        if not isinstance(release, dict) or release.get("tag") != expected_tag:
            raise SystemExit(1)
        assets = release.get("assets")
        if isinstance(assets, list):
            for name in assets:
                if isinstance(name, str) and name.endswith(".zip"):
                    print(name)
                    raise SystemExit(0)
raise SystemExit(1)
PY
}

telegram_product_default=$(recorded_product_tag telegram) || telegram_product_default=$DEFAULT_TELEGRAM_VERSION
web_product_default=$(recorded_product_tag web) || web_product_default=$DEFAULT_WEB_VERSION
windows_product_default=$(recorded_product_tag windows) || windows_product_default=$DEFAULT_WINDOWS_VERSION
telegram_product_release=${WUWATERM_TELEGRAM_VERSION:-$telegram_product_default}
web_product_release=${WUWATERM_WEB_VERSION:-$web_product_default}
windows_product_release=${WUWATERM_WINDOWS_VERSION:-$windows_product_default}
telegram_oci_archive=
telegram_image=
telegram_image_version=

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
WUWATERM_TRANSLATION_THINKING_ZH_EN
WUWATERM_TRANSLATION_THINKING_EN_ZH
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

telegram_http_optional_env_keys() {
    cat <<'EOF'
OWNER_USER_ID
WUWATERM_LOG_LEVEL
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

telegram_version_supports_http() {
    ver=$1
    case "$ver" in
        v[0-9]*) ;;
        *) return 1 ;;
    esac
    ver=${ver#v}
    ver=${ver%%-*}
    ver=${ver%%+*}
    old_ifs=$IFS
    IFS=.
    set -- $ver
    IFS=$old_ifs
    [ "$#" -ge 2 ] && [ "$#" -le 3 ] || return 1
    major=$1
    minor=$2
    case "$major" in ''|*[!0-9]*) return 1 ;; esac
    case "$minor" in ''|*[!0-9]*) return 1 ;; esac
    if [ "$#" -eq 3 ]; then
        patch=$3
        case "$patch" in ''|*[!0-9]*) return 1 ;; esac
    fi
    if [ "$major" -gt 0 ]; then
        return 0
    fi
    [ "$minor" -ge 7 ]
}

sync_telegram_env_value() {
    env_file=$1
    env_key=$2
    env_val=$3
    tmp_env="$env_file.tmp.$$"
    (
        umask 077
        WUWATERM_SYNC_KEY="$env_key" WUWATERM_SYNC_VAL="$env_val" awk '
            BEGIN {
                replaced = 0
                key = ENVIRON["WUWATERM_SYNC_KEY"]
                val = ENVIRON["WUWATERM_SYNC_VAL"]
                pat = "^[[:space:]]*" key "[[:space:]]*="
            }
            $0 ~ pat {
                if (!replaced) {
                    print key "=" val
                    replaced = 1
                }
                next
            }
            { print }
            END {
                if (!replaced) print key "=" val
            }
        ' "$env_file" >"$tmp_env"
    ) && chmod 600 "$tmp_env" \
        || { rm -f "$tmp_env"; fail "could not prepare updated $env_file"; }
    mv -f "$tmp_env" "$env_file" \
        || { rm -f "$tmp_env"; fail "could not update $env_key in $env_file"; }
}

write_telegram_http_env() {
    target=$1
    token=$2
    bot_token=$(env_value TELEGRAM_BOT_TOKEN || true)
    [ -n "$bot_token" ] || fail "TELEGRAM_BOT_TOKEN is empty in .env. Edit $code_root/.env before running setup."
    scratch="$target.$$"
    if ! (
        umask 077
        exec >"$scratch"
        printf '# Written by deploy/setup.sh for WuwaTerm-Telegram (Core HTTP mode).\n'
        printf 'WUWATERM_CORE_MODE=http\n'
        printf 'WUWATERM_CORE_API_URL=http://127.0.0.1:%s\n' "$(api_port)"
        printf 'WUWATERM_CORE_API_TOKEN=%s\n' "$token"
        printf 'WUWATERM_STATE_DIR=/app/state\n'
        printf 'WUWATERM_POLL_HEALTH=1\n'
        printf 'TELEGRAM_BOT_TOKEN=%s\n' "$bot_token"
        telegram_http_optional_env_keys | while IFS= read -r key; do
            value=$(env_value "$key" || true)
            [ -n "$value" ] || continue
            printf '%s=%s\n' "$key" "$value"
        done
    ); then
        rm -f "$scratch"
        fail "cannot write $target"
    fi
    mv "$scratch" "$target" || { rm -f "$scratch"; fail "cannot write $target"; }
    chmod 600 "$target"
}

# Detect legacy inline bot or unmanaged standalone Telegram containers before
# any destructive action. Only a container explicitly labeled
# org.wuwaterm.managed-by=setup.sh may be replaced by this script; existing
# Compose-managed or manually started Telegram containers fail closed so an
# operator deployment is never deleted and two pollers never race on one token.
preflight_telegram_containers() {
    [ "$with_telegram" = 1 ] || return 0
    [ -z "$telegram_dir" ] || return 0
    if [ "$(container_state wuwaterm-bot)" = running ]; then
        fail "deprecated inline bot (wuwaterm-bot) is running; stop it first (docker stop wuwaterm-bot) before starting wuwaterm-telegram"
    fi
    if ! tg_compose_ids=$(docker ps -aq --filter label=com.docker.compose.service=wuwaterm-telegram 2>&1); then
        fail "could not inspect existing Compose-managed wuwaterm-telegram containers via docker ps: $tg_compose_ids"
    fi
    if [ -n "$tg_compose_ids" ]; then
        tg_compose_summary=
        for tg_cid in $tg_compose_ids; do
            tg_cstate=$(container_state "$tg_cid")
            tg_compose_summary="${tg_compose_summary:+$tg_compose_summary, }${tg_cid}(${tg_cstate:-unknown})"
        done
        fail "existing Compose-managed wuwaterm-telegram container(s) detected: $tg_compose_summary; stop and remove them via WuwaTerm-Telegram's docker compose before running --with-telegram (refusing to delete unmanaged containers or start dual pollers)"
    fi
    tg_named_state=$(container_state wuwaterm-telegram)
    if [ -n "$tg_named_state" ]; then
        tg_managed=$(docker inspect -f '{{ index .Config.Labels "org.wuwaterm.managed-by" }}' \
            wuwaterm-telegram 2>/dev/null || true)
        if [ "$tg_managed" != "setup.sh" ]; then
            fail "existing wuwaterm-telegram container (state: $tg_named_state) is not managed by deploy/setup.sh; stop and remove or migrate it manually before running --with-telegram (refusing to delete unmanaged containers)"
        fi
    fi
}

resolve_telegram_package() {
    [ "$with_telegram" = 1 ] || return 0
    [ -z "$telegram_dir" ] || return 0
    case "$telegram_product_release" in
        v[0-9]*) ;;
        *) fail "WUWATERM_TELEGRAM_VERSION must look like v0.7.0, not: $telegram_product_release" ;;
    esac
    tg_state="$code_root/state-telegram"
    tg_cached_ref=
    tg_cached_ver=
    if [ -s "$tg_state/image.ref" ]; then
        tg_cached_ref=$(head -n 1 "$tg_state/image.ref" | tr -d '\r')
    fi
    if [ -s "$tg_state/image.version" ]; then
        tg_cached_ver=$(head -n 1 "$tg_state/image.version" | tr -d '\r')
    fi
    if [ -n "$telegram_package" ]; then
        if [ -n "${WUWATERM_TELEGRAM_VERSION:-}" ] \
            && ! telegram_version_supports_http "$telegram_product_release"; then
            fail "WuwaTerm-Telegram $telegram_product_release does not include the HTTP Core backend (WUWATERM_CORE_API_URL / WUWATERM_CORE_API_TOKEN). HTTP Core orchestration requires >= v0.7.0 (default $DEFAULT_TELEGRAM_VERSION); unset WUWATERM_TELEGRAM_VERSION or pass a release >= v0.7.0, or use --telegram-dir <checkout> with WuwaTerm-Telegram's own deploy/docker-compose.yml for older releases"
        fi
    elif [ -z "${WUWATERM_TELEGRAM_VERSION:-}" ] \
        && [ "$tg_cached_ver" = "local-package" ] \
        && [ -n "$tg_cached_ref" ] \
        && docker image inspect "$tg_cached_ref" >/dev/null 2>&1; then
        telegram_image=$tg_cached_ref
        telegram_oci_archive=
        telegram_image_version=local-package
        printf 'setup: reusing the installed Telegram image %s (local-package).\n' "$telegram_image"
        return 0
    else
        if ! telegram_version_supports_http "$telegram_product_release"; then
            fail "WuwaTerm-Telegram $telegram_product_release does not include the HTTP Core backend (WUWATERM_CORE_API_URL / WUWATERM_CORE_API_TOKEN). HTTP Core orchestration requires >= v0.7.0 (default $DEFAULT_TELEGRAM_VERSION); unset WUWATERM_TELEGRAM_VERSION or pass a release >= v0.7.0, pass --telegram-package <path> with a compatible OCI archive, or use --telegram-dir <checkout> with WuwaTerm-Telegram's own deploy/docker-compose.yml for older releases"
        fi
    fi
    mkdir -p "$tg_state" "$tg_state/state"
    chmod 700 "$tg_state" "$tg_state/state"
    if [ -z "$telegram_package" ] \
        && [ -n "$tg_cached_ref" ] \
        && [ "$tg_cached_ver" = "$telegram_product_release" ] \
        && docker image inspect "$tg_cached_ref" >/dev/null 2>&1; then
        telegram_image=$tg_cached_ref
        telegram_oci_archive=
        telegram_image_version=$telegram_product_release
        printf 'setup: reusing the installed Telegram image %s (%s).\n' "$telegram_image" "$telegram_product_release"
        return 0
    fi
    if [ -n "$telegram_package" ]; then
        [ -e "$telegram_package" ] || fail "--telegram-package path does not exist: $telegram_package"
        sums_file=
        if [ -d "$telegram_package" ]; then
            pkg_dir=$(CDPATH= cd -- "$telegram_package" && pwd -P)
            telegram_oci_archive="$pkg_dir/image.oci.tar"
            [ -f "$telegram_oci_archive" ] \
                || fail "--telegram-package directory is missing image.oci.tar: $pkg_dir"
            if [ -f "$pkg_dir/SHA256SUMS" ]; then
                sums_file="$pkg_dir/SHA256SUMS"
            fi
        elif [ -f "$telegram_package" ]; then
            telegram_oci_archive="$telegram_package"
            pkg_dir=$(dirname -- "$telegram_package")
            if [ -f "$pkg_dir/SHA256SUMS" ]; then
                sums_file="$pkg_dir/SHA256SUMS"
            fi
        else
            fail "--telegram-package must be an OCI archive file (image.oci.tar) or a directory holding it"
        fi
        if [ -n "$sums_file" ]; then
            archive_name=$(basename -- "$telegram_oci_archive")
            expected=$(awk -v name="$archive_name" '$2 == name || $2 == "*" name {print $1}' \
                "$sums_file" | head -n 1)
            [ -n "$expected" ] || fail "$sums_file does not list $archive_name"
            actual=$(sha256_of "$telegram_oci_archive")
            [ "$expected" = "$actual" ] \
                || fail "the Telegram OCI archive checksum does not match $sums_file"
        fi
        if [ -n "${WUWATERM_TELEGRAM_VERSION:-}" ]; then
            telegram_image_version=$telegram_product_release
        else
            telegram_image_version=local-package
        fi
        return 0
    fi
    telegram_image_version=$telegram_product_release
    base="$telegram_product_repo/releases/download/$telegram_product_release"
    sums_file="$tg_state/SHA256SUMS"
    archive_file="$tg_state/image.oci.tar"
    curl -fsSL --max-time 30 "$base/SHA256SUMS" -o "$sums_file" \
        || fail "could not download Telegram SHA256SUMS ($base/SHA256SUMS); refusing an unverified Telegram package"
    expected=$(awk -v name="image.oci.tar" '$2 == name || $2 == "*" name {print $1}' \
        "$sums_file" | head -n 1)
    [ -n "$expected" ] || fail "Telegram SHA256SUMS does not list image.oci.tar"
    if recorded_sha=$(recorded_product_asset_sha256 telegram "$telegram_product_release" image.oci.tar); then
        [ "$expected" = "$recorded_sha" ] \
            || fail "Telegram SHA256SUMS digest for image.oci.tar disagrees with contracts/products.json"
    fi
    curl -fsSL --max-time 600 "$base/image.oci.tar" -o "$archive_file" \
        || fail "could not download Telegram OCI archive ($base/image.oci.tar)"
    actual=$(sha256_of "$archive_file")
    [ "$expected" = "$actual" ] \
        || fail "the downloaded Telegram image.oci.tar checksum does not match SHA256SUMS"
    telegram_oci_archive=$archive_file
}

wait_for_telegram() {
    tries=0
    while [ "$tries" -lt 30 ]; do
        if docker exec wuwaterm-telegram python -B -m wuwaterm_telegram.operations health >/dev/null 2>&1; then
            return 0
        fi
        tries=$((tries + 1))
        sleep 2
    done
    fail "wuwaterm-telegram did not pass health check; check docker logs wuwaterm-telegram"
}

install_telegram() {
    [ "$with_telegram" = 1 ] || return 0
    case "$telegram_product_release" in
        v[0-9]*) ;;
        *) fail "WUWATERM_TELEGRAM_VERSION must look like v0.7.0, not: $telegram_product_release" ;;
    esac
    if [ -n "$telegram_dir" ]; then
        wheel="wuwaterm_telegram-${telegram_product_release#v}-py3-none-any.whl"
        prepare_telegram_checkout
        where=$telegram_checkout
        prepared=1
        printf '\nsetup: the Telegram bot is the standalone product %s,\n' "$telegram_product_repo"
        printf 'setup: release %s. This checkout starts no bot in --telegram-dir mode.\n' "$telegram_product_release"
        print_telegram_steps "$wheel" "$where" "$prepared"
        if [ "$(container_state wuwaterm-bot)" = running ]; then
            printf 'setup: the deprecated inline bot is running here; stop it before the product\n'
            printf 'setup: polls the same token: docker stop wuwaterm-bot\n'
        fi
        return 0
    fi
    preflight_telegram_containers
    [ -f "$code_root/data/terms.db" ] \
        || fail "$code_root/data/terms.db is missing; cannot start wuwaterm-telegram without the dictionary database"
    tg_root="$code_root/state-telegram"
    tg_state_dir="$tg_root/state"
    tg_env="$tg_root/telegram.env"
    tg_token_file="$tg_root/device.token"
    mkdir -p "$tg_root" "$tg_state_dir"
    chmod 700 "$tg_root" "$tg_state_dir"
    tg_token_reissued=0
    if [ ! -s "$tg_token_file" ]; then
        tg_token_reissued=1
    fi
    issue_device "telegram" "translate,meta" "$tg_token_file"
    tg_device_token=$(cat "$tg_token_file")
    if [ ! -f "$tg_env" ]; then
        write_telegram_http_env "$tg_env" "$tg_device_token"
        printf 'setup: wrote Telegram HTTP-mode env to %s (mode 600).\n' "$tg_env"
    else
        chmod 600 "$tg_env"
        existing_core_token=$(file_value "$tg_env" WUWATERM_CORE_API_TOKEN || true)
        if [ "$tg_token_reissued" = 1 ] || [ "$existing_core_token" != "$tg_device_token" ]; then
            sync_telegram_env_value "$tg_env" WUWATERM_CORE_API_TOKEN "$tg_device_token"
            printf 'setup: updated WUWATERM_CORE_API_TOKEN in %s from %s.\n' "$tg_env" "$tg_token_file"
        else
            printf 'setup: %s already present; keeping it.\n' "$tg_env"
        fi
    fi
    tg_check_bot_token=$(file_value "$tg_env" TELEGRAM_BOT_TOKEN || true)
    [ -n "$tg_check_bot_token" ] || fail "TELEGRAM_BOT_TOKEN is empty in $tg_env"
    tg_check_api_url=$(file_value "$tg_env" WUWATERM_CORE_API_URL || true)
    [ -n "$tg_check_api_url" ] || fail "WUWATERM_CORE_API_URL is empty in $tg_env"
    tg_check_api_token=$(file_value "$tg_env" WUWATERM_CORE_API_TOKEN || true)
    [ -n "$tg_check_api_token" ] || fail "WUWATERM_CORE_API_TOKEN is empty in $tg_env"
    if [ -n "$telegram_oci_archive" ]; then
        load_output=$(docker load -i "$telegram_oci_archive" 2>&1) \
            || fail "could not load Telegram OCI archive $telegram_oci_archive"
        telegram_image=$(printf '%s\n' "$load_output" \
            | sed -n 's/^Loaded image:[[:space:]]*//p' | tail -n 1 | tr -d '\r')
        if [ -z "$telegram_image" ]; then
            telegram_image=$(printf '%s\n' "$load_output" \
                | sed -n 's/^Loaded image ID:[[:space:]]*//p' | tail -n 1 | tr -d '\r')
        fi
        [ -n "$telegram_image" ] \
            || fail "docker load did not report a loaded image reference for $telegram_oci_archive"
        (umask 077; printf '%s\n' "$telegram_image" >"$tg_root/image.ref")
        chmod 600 "$tg_root/image.ref"
        (umask 077; printf '%s\n' "$telegram_image_version" >"$tg_root/image.version")
        chmod 600 "$tg_root/image.version"
    fi
    [ -n "$telegram_image" ] || fail "no Telegram container image resolved"
    if [ -n "$(container_state wuwaterm-telegram)" ]; then
        docker rm -f wuwaterm-telegram >/dev/null 2>&1 \
            || fail "could not replace existing setup.sh-managed wuwaterm-telegram container"
    fi
    docker run -d --name wuwaterm-telegram --restart unless-stopped \
        --network host \
        --label org.wuwaterm.managed-by=setup.sh \
        --env-file "$tg_env" \
        -v "$code_root/data:/app/data:ro" \
        -v "$tg_state_dir:/app/state:rw" \
        --tmpfs /run/wuwaterm-telegram:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
        "$telegram_image" >/dev/null \
        || fail "could not start wuwaterm-telegram"
    wait_for_telegram
    printf 'setup: wuwaterm-telegram is running in HTTP mode against http://127.0.0.1:%s.\n' "$(api_port)"
}

enable_windows() {
    [ "$with_windows" = 1 ] || return 0
    case "$windows_product_release" in
        v[0-9]*) ;;
        *) fail "WUWATERM_WINDOWS_VERSION must look like v0.9.0, not: $windows_product_release" ;;
    esac
    win_token_file="$code_root/state-api/windows-device.token"
    issue_device "windows" "translate,meta" "$win_token_file"
    win_zip=$(recorded_windows_zip_name "$windows_product_release") \
        || win_zip="WuwaTerm-${windows_product_release#v}-windows-x64.zip"
    win_sha=$(recorded_product_asset_sha256 windows "$windows_product_release" "$win_zip" || true)
    win_release_url="$windows_product_repo/releases/tag/$windows_product_release"
    win_download_url="$windows_product_repo/releases/download/$windows_product_release/$win_zip"
    printf '\nsetup: Windows desktop pairing (%s):\n' "$windows_product_release"
    printf 'setup:   release page: %s\n' "$win_release_url"
    printf 'setup:   archive URL:  %s\n' "$win_download_url"
    if [ -n "$win_sha" ]; then
        printf 'setup:   SHA-256:      %s\n' "$win_sha"
    else
        printf 'setup:   checksums:    %s/releases/download/%s/SHA256SUMS\n' \
            "$windows_product_repo" "$windows_product_release"
    fi
    printf 'setup:   device token: %s (mode 0600; copy into Windows Settings -> Connection)\n' "$win_token_file"
    printf 'setup:   Core API:     http://127.0.0.1:%s (expose over HTTPS via your reverse proxy, forwarding Authorization unchanged)\n' "$(api_port)"
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
            *) fail "WUWATERM_WEB_VERSION must be latest or a tag like v0.3.0, not: $web_product_release" ;;
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

stop_web_container() {
    web_inspect=$(docker inspect -f '{{.State.Status}}' wuwaterm-web 2>&1) && {
        docker rm -f wuwaterm-web >/dev/null 2>&1 \
            || fail "could not stop existing wuwaterm-web container before migrating the Web admission database"
        return 0
    }
    case "$web_inspect" in
        *"No such object"*|*"No such container"*) return 0 ;;
    esac
    fail "could not inspect wuwaterm-web state before migrating the Web admission database: $web_inspect"
}

classify_web_db_file() {
    python3 - "$1" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
expected_columns = [
    "id",
    "second_key",
    "minute_key",
    "day_key",
    "upstream_used",
    "translation_minute_used",
    "terms_used",
    "translation_used",
    "character_used",
    "meta_used",
    "review_used",
]

try:
    with open(path, "rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            print("unknown")
            raise SystemExit(0)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        quick = conn.execute("PRAGMA quick_check(1)").fetchone()
        if not quick or quick[0] != "ok":
            print("unknown")
            raise SystemExit(0)
        if app_id == 0x5755544E and version == 1:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tm_schema'"
            ).fetchone()
            print("tm" if row else "unknown")
            raise SystemExit(0)
        if app_id == 0x5755544D and version == 1:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(shared_pool)").fetchall()]
            print("pool" if cols == expected_columns else "unknown")
            raise SystemExit(0)
        print("unknown")
    finally:
        conn.close()
except (OSError, sqlite3.DatabaseError):
    print("unknown")
PY
}

pool_dbs_identical() {
    python3 - "$1" "$2" <<'PY'
from pathlib import Path
import sqlite3
import sys

left_path, right_path = sys.argv[1], sys.argv[2]
query = (
    "SELECT id, second_key, minute_key, day_key, upstream_used, "
    "translation_minute_used, terms_used, translation_used, "
    "character_used, meta_used, review_used FROM shared_pool ORDER BY id ASC"
)

def read_rows(path: str) -> list[tuple[int, ...]]:
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()

try:
    if read_rows(left_path) == read_rows(right_path):
        raise SystemExit(0)
except sqlite3.DatabaseError:
    pass
raise SystemExit(1)
PY
}

backup_legacy_pool_db() {
    src_db=$1
    dst_db=$2
    tmp_db="$dst_db.tmp.$$"
    rm -f "$tmp_db"
    if ! python3 - "$src_db" "$tmp_db" <<'PY'
import os
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
fd = os.open(dst_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
    dst.commit()
finally:
    dst.close()
    src.close()
os.chmod(dst_path, 0o600)
PY
    then
        rm -f "$tmp_db"
        fail "could not create SQLite backup from $src_db to $dst_db"
    fi
    [ "$(classify_web_db_file "$tmp_db")" = "pool" ] || {
        rm -f "$tmp_db"
        fail "verified SQLite backup $tmp_db did not pass admission pool schema check"
    }
    mv -f "$tmp_db" "$dst_db" || {
        rm -f "$tmp_db"
        fail "could not finalize $dst_db"
    }
    chmod 600 "$dst_db"
}

rewrite_web_env_pool_path() {
    web_env_file=$1
    new_pool_path=$2
    tmp_env="$web_env_file.tmp.$$"
    (
        umask 077
        WUWATERM_NEW_POOL_PATH="$new_pool_path" awk '
            BEGIN { replaced = 0; val = ENVIRON["WUWATERM_NEW_POOL_PATH"] }
            /^[[:space:]]*WUWATERM_LOCAL_DB_PATH[[:space:]]*=/ {
                if (!replaced) {
                    print "WUWATERM_LOCAL_DB_PATH=" val
                    replaced = 1
                }
                next
            }
            { print }
            END {
                if (!replaced) print "WUWATERM_LOCAL_DB_PATH=" val
            }
        ' "$web_env_file" >"$tmp_env"
    ) && chmod 600 "$tmp_env" \
        || { rm -f "$tmp_env"; fail "could not prepare updated $web_env_file"; }

    effective_tmp=$(file_value "$tmp_env" WUWATERM_LOCAL_DB_PATH || true)
    if [ "$effective_tmp" != "$new_pool_path" ] || [ "$(basename "$effective_tmp")" = "tm.sqlite3" ]; then
        rm -f "$tmp_env"
        fail "rewritten $web_env_file still resolves WUWATERM_LOCAL_DB_PATH to $effective_tmp"
    fi

    mv -f "$tmp_env" "$web_env_file" \
        || { rm -f "$tmp_env"; fail "could not update WUWATERM_LOCAL_DB_PATH in $web_env_file"; }

    effective_live=$(file_value "$web_env_file" WUWATERM_LOCAL_DB_PATH || true)
    if [ "$effective_live" != "$new_pool_path" ] || [ "$(basename "$effective_live")" = "tm.sqlite3" ]; then
        fail "effective WUWATERM_LOCAL_DB_PATH in $web_env_file resolved to $effective_live after rewrite"
    fi
}

# WuwaTerm-Web v0.7.0+ derives its self-hosted translation memory database as
# tmDatabasePathForPool(WUWATERM_LOCAL_DB_PATH) = dirname(poolDbPath)/tm.sqlite3
# and ignores any separate env var. An older web.env that pointed
# WUWATERM_LOCAL_DB_PATH at .../tm.sqlite3 therefore opens the same file for both
# databases and fails startup on SQLite application_id mismatch. Verify the
# legacy admission pool schema read-only first, stop any running wuwaterm-web
# container, back up atomically to .../pool.sqlite3, clean up the old
# tm.sqlite3* files, and switch web.env last so every crash point is recoverable.
reconcile_web_pool_db_path() {
    web_env_file=$1
    data_dir=$2
    local_db_path=$(file_value "$web_env_file" WUWATERM_LOCAL_DB_PATH || true)
    [ -n "$local_db_path" ] || return 0
    db_dir=$(web_db_dir "$web_env_file")
    local_base=$(basename "$local_db_path")
    new_pool_path="$db_dir/pool.sqlite3"
    host_tm_file="$data_dir/tm.sqlite3"
    host_pool_file="$data_dir/pool.sqlite3"

    if [ "$local_base" != "tm.sqlite3" ]; then
        [ -e "$host_tm_file" ] || return 0
        command -v python3 >/dev/null 2>&1 \
            || fail "python3 is required to inspect $host_tm_file"
        tm_role=$(classify_web_db_file "$host_tm_file")
        case "$tm_role" in
            tm)
                return 0
                ;;
            pool)
                if [ "$local_base" != "pool.sqlite3" ]; then
                    fail "refusing to delete legacy admission pool $host_tm_file while WUWATERM_LOCAL_DB_PATH points to custom $local_db_path; resolve the two databases manually before rerunning"
                fi
                if [ -e "$host_pool_file" ]; then
                    [ "$(classify_web_db_file "$host_pool_file")" = "pool" ] \
                        || fail "refusing to clean up legacy $host_tm_file while $host_pool_file is not a valid admission pool database"
                    pool_dbs_identical "$host_tm_file" "$host_pool_file" \
                        || fail "refusing to remove $host_tm_file: $host_tm_file and $host_pool_file are both admission pool databases with different contents"
                    stop_web_container
                    pool_dbs_identical "$host_tm_file" "$host_pool_file" \
                        || fail "refusing to remove $host_tm_file: $host_tm_file changed before container stopped and differs from $host_pool_file"
                    rm -f "$host_tm_file" "$host_tm_file-wal" "$host_tm_file-shm"
                else
                    stop_web_container
                    backup_legacy_pool_db "$host_tm_file" "$host_pool_file"
                    rm -f "$host_tm_file" "$host_tm_file-wal" "$host_tm_file-shm"
                fi
                printf 'setup: finished migrating leftover legacy %s to %s.\n' "$host_tm_file" "$host_pool_file"
                return 0
                ;;
            *)
                fail "refusing to touch unrecognized file $host_tm_file while WUWATERM_LOCAL_DB_PATH=$local_db_path"
                ;;
        esac
    fi

    if [ -e "$host_tm_file" ] && [ -e "$host_pool_file" ]; then
        command -v python3 >/dev/null 2>&1 \
            || fail "python3 is required to inspect $host_tm_file and $host_pool_file"
        [ "$(classify_web_db_file "$host_pool_file")" = "pool" ] \
            || fail "refusing to migrate: $host_pool_file exists alongside $host_tm_file and is not a valid admission pool database"
        [ "$(classify_web_db_file "$host_tm_file")" = "pool" ] \
            || fail "refusing to remove $host_tm_file alongside $host_pool_file: not a legacy admission pool database"
        pool_dbs_identical "$host_tm_file" "$host_pool_file" \
            || fail "refusing to migrate: $host_tm_file and $host_pool_file are both admission pool databases with different contents"
        stop_web_container
        pool_dbs_identical "$host_tm_file" "$host_pool_file" \
            || fail "refusing to migrate: $host_tm_file changed before container stopped and differs from $host_pool_file"
        rm -f "$host_tm_file" "$host_tm_file-wal" "$host_tm_file-shm"
        rewrite_web_env_pool_path "$web_env_file" "$new_pool_path"
    elif [ -e "$host_tm_file" ]; then
        command -v python3 >/dev/null 2>&1 \
            || fail "python3 is required to verify and migrate $host_tm_file before relocating WUWATERM_LOCAL_DB_PATH"
        tm_role=$(classify_web_db_file "$host_tm_file")
        [ "$tm_role" = "pool" ] \
            || fail "refusing to migrate $host_tm_file to $host_pool_file (detected role: $tm_role); ensure it is a valid Web admission pool database or update WUWATERM_LOCAL_DB_PATH=$new_pool_path in $web_env_file manually"
        stop_web_container
        backup_legacy_pool_db "$host_tm_file" "$host_pool_file"
        rm -f "$host_tm_file" "$host_tm_file-wal" "$host_tm_file-shm"
        rewrite_web_env_pool_path "$web_env_file" "$new_pool_path"
    elif [ -e "$host_pool_file" ]; then
        command -v python3 >/dev/null 2>&1 \
            || fail "python3 is required to verify $host_pool_file before updating WUWATERM_LOCAL_DB_PATH"
        [ "$(classify_web_db_file "$host_pool_file")" = "pool" ] \
            || fail "refusing to switch $web_env_file to $new_pool_path: $host_pool_file is not a valid admission pool database"
        rewrite_web_env_pool_path "$web_env_file" "$new_pool_path"
    else
        rewrite_web_env_pool_path "$web_env_file" "$new_pool_path"
    fi

    printf 'setup: relocated WUWATERM_LOCAL_DB_PATH in %s from %s to %s to avoid colliding with tm.sqlite3.\n' \
        "$web_env_file" "$local_db_path" "$new_pool_path"
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
    reconcile_web_pool_db_path "$web_env" "$data_dir"
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
    if resolve_knowledge_db_path; then
        if [ "$kb_configured" = 1 ]; then
            if [ -f "$kb_host_path" ]; then
                printf 'knowledge:    %s present (%s bytes, configured)\n' \
                    "$kb_relpath" "$(wc -c <"$kb_host_path" | tr -d ' ')"
            else
                printf 'knowledge:    %s missing (configured)\n' "$kb_relpath"
            fi
        elif [ -f "$code_root/data/knowledge.db" ]; then
            printf 'knowledge:    data/knowledge.db present (%s bytes, not configured)\n' \
                "$(wc -c <"$code_root/data/knowledge.db" | tr -d ' ')"
        else
            printf 'knowledge:    not configured\n'
        fi
    else
        printf 'knowledge:    invalid configuration (%s)\n' "$kb_path_error"
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
    if [ -s "$code_root/state-api/windows-device.token" ]; then
        printf 'windows:      token ready (%s, mode %s)\n' \
            "$code_root/state-api/windows-device.token" \
            "$(file_mode "$code_root/state-api/windows-device.token")"
    else
        printf 'windows:      not configured\n'
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
# Resolve the Telegram checkout/containers/package and the Web package before
# the data build: an incompatible version, conflicting container, missing
# package, or wrong path should stop the install instead of surfacing after a
# gigabyte of upstream data was fetched.
resolve_telegram_dir
preflight_telegram_containers
resolve_telegram_package
if [ "$with_web" = 1 ]; then
    resolve_web_package
fi
build_data
start_api
ensure_api_device
install_telegram
enable_web
enable_windows
maybe_adopt

printf '\nsetup: done.\n'
report_status
