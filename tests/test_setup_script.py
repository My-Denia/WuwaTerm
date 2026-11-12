"""Behaviour of the one-command installer, with Docker and curl stubbed."""

from __future__ import annotations

import json
import os
import re
import pathlib
import shutil
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "docker $*" >> "$STUB_DOCKER_LOG"
case "$*" in
  "compose version") exit 0 ;;
  *"device list"*) printf '%s\\n' "${STUB_DEVICES:-no devices}" ;;
  *"device issue"*)
    cat >/dev/null
    printf 'device_id: dev123\\n'
    printf 'device_name: fixture\\n'
    printf 'scopes: translate,meta\\n'
    printf 'created_at: 2026-01-01T00:00:00Z\\n' ;;
  *"build-db"*) : > "$STUB_ROOT/data/terms.candidate.db" ;;
  "inspect -f {{.State.Status}} wuwaterm-bot") printf '%s\\n' "${STUB_BOT_STATE:-}" ;;
  "inspect -f {{.State.Status}} wuwaterm-telegram") printf '%s\\n' "${STUB_PRODUCT_STATE:-}" ;;
  "inspect -f {{.State.Status}} wuwaterm-web") printf '%s\\n' "${STUB_WEB_STATE:-running}" ;;
  "inspect -f {{.State.Status}} wuwaterm-api") printf '%s\\n' "running" ;;
  *"com.docker.compose.project"*) printf '%s\\n' "${STUB_COMPOSE_PROJECT-wuwaterm}" ;;
esac
exit 0
"""

CURL_STUB = """#!/bin/sh
printf '%s\\n' "curl $*" >> "$STUB_CURL_LOG"
if [ "${STUB_CURL_FAIL:-0}" = 1 ]; then exit 7; fi
if [ -n "${STUB_CURL_FAIL_MATCH:-}" ]; then
  case "$*" in *"$STUB_CURL_FAIL_MATCH"*) exit 22 ;; esac
fi
exit 0
"""


@pytest.fixture()
def install_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "checkout"
    (root / "deploy").mkdir(parents=True)
    (root / "bin").mkdir()
    for name in ("setup.sh", "env.example", "docker-compose.yml", "docker-compose.ghcr.yml"):
        shutil.copy2(ROOT / "deploy" / name, root / "deploy" / name)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "wuwaterm"\nversion = "0.7.0"\n', encoding="utf-8"
    )
    (root / "bin" / "docker").write_text(DOCKER_STUB, encoding="utf-8")
    (root / "bin" / "curl").write_text(CURL_STUB, encoding="utf-8")
    for name in ("docker", "curl"):
        (root / "bin" / name).chmod(0o755)
    (root / "deploy" / "setup.sh").chmod(0o755)
    return root


def write_env(root: pathlib.Path, *, telegram_token: str | None = None) -> None:
    lines = [
        "WUWATERM_OPENAI_BASE_URL=https://model.invalid/v1",
        "WUWATERM_OPENAI_API_KEY=stub-key-value",
        "WUWATERM_OPENAI_MODEL=stub-model",
        "WUWATERM_API_PORT=8788",
    ]
    if telegram_token is not None:
        lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token}")
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_setup(root: pathlib.Path, *args: str, env: dict[str, str] | None = None):
    base = {
        "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
        "STUB_ROOT": str(root),
        "STUB_DOCKER_LOG": str(root / "docker.log"),
        "STUB_CURL_LOG": str(root / "curl.log"),
        "HOME": str(root),
    }
    base.update(env or {})
    return subprocess.run(
        [str(root / "deploy" / "setup.sh"), *args],
        cwd=root,
        env=base,
        capture_output=True,
        text=True,
        check=False,
    )


def docker_log(root: pathlib.Path) -> str:
    log = root / "docker.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def mode_of(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)

# The published WuwaTerm-Web v0.1.0 assets, copied field for field. The
# archive name lives under archive; start, port and health are properties of
# the bundle, so a manifest that only records the archive still installs.
WEB_ROOT_MANIFEST = {
    "format": "wuwaterm-web-selfhost-v1",
    "tag": "v0.1.0",
    "tag_status": "proposed",
    "version": "0.1.0",
    "source_sha": "9f738e717c1b1c1b0fb099247bb4db6e8e811148",
    "source_base_sha": None,
    "source_tree": "72e0245bbdfd7b66172ab28c4765f244d91ec473",
    "source_export_excludes": [
        ".openai",
        ".env*",
        "**/.env*",
        ".dev.vars*",
        "**/.dev.vars*",
    ],
    "build_node": "v24.22.2",
    "platform": "linux-x64",
    "base_path": "",
    "ci": {
        "workflow": "Web CI",
        "run_id": "42531510475",
        "url": "https://github.com/My-Denia/WuwaTerm-Web/actions/runs/42531510475",
    },
    "archive": {
        "name": "wuwaterm-web-v0.1.0-linux-x64-root.tar.gz",
        "sha256": "4e5e9ba8b4c14b0501b9336b3f46ef90204f127e1a48c7a416d68f3dc4e9eeac",
    },
}

WEB_PREFIX_MANIFEST = {
    **WEB_ROOT_MANIFEST,
    "base_path": "/private",
    "archive": {
        "name": "wuwaterm-web-v0.1.0-linux-x64-prefix-71708eb255bc.tar.gz",
        "sha256": "2dc6d7222a57a037c1351067cc34b657126e2031a93c946246270f860bcd485d",
    },
}

START_MJS = """// The downloaded bundle keeps this launcher beside the generated server.
process.env.PORT ||= '3000';
await import('./server.js');
"""


def write_web_package(root: pathlib.Path, manifest_name: str, manifest: dict) -> pathlib.Path:
    """A package shaped like the published archive: bundle/start.mjs plus a manifest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / manifest_name).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    bundle = root / "bundle"
    bundle.mkdir(exist_ok=True)
    (bundle / "start.mjs").write_text(START_MJS, encoding="utf-8")
    (bundle / "server.js").write_text("// fixture server\n", encoding="utf-8")
    (bundle / "BUILD-INFO.json").write_text(
        json.dumps(
            {
                "format": manifest["format"],
                "tag": manifest["tag"],
                "version": manifest["version"],
                "source_sha": manifest["source_sha"],
                "source_tree": manifest["source_tree"],
                "build_node": manifest["build_node"],
                "platform": manifest["platform"],
                "base_path": manifest["base_path"],
                "ci": manifest["ci"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def web_env_values(root: pathlib.Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (root / "state-web" / "web.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def test_help_lists_the_three_surfaces_and_changes_nothing(install_root):
    result = run_setup(install_root, "--help")

    assert result.returncode == 0
    assert "Core HTTP API only" in result.stdout
    assert "--with-telegram" in result.stdout
    assert "--with-web" in result.stdout
    assert not (install_root / "data").exists()
    assert docker_log(install_root) == ""


def test_unknown_option_is_a_usage_error(install_root):
    result = run_setup(install_root, "--enable-everything")

    assert result.returncode == 2
    assert "unknown option" in result.stderr
    assert "Usage:" in result.stderr


def test_default_install_builds_data_and_never_starts_the_bot(install_root):
    write_env(install_root)

    result = run_setup(install_root, "--no-adopt")

    assert result.returncode == 0, result.stderr
    assert (install_root / "data" / "terms.db").exists()
    assert mode_of(install_root / ".env") == 0o600
    assert mode_of(install_root / "state") == 0o700
    assert mode_of(install_root / "state-api") == 0o700
    token_file = install_root / "state-api" / "first-device.token"
    assert mode_of(token_file) == 0o600
    token = token_file.read_text(encoding="utf-8").strip()
    assert token.startswith("wtd1.dev123.")
    assert token.rsplit(".", 1)[1] not in result.stdout
    log = docker_log(install_root)
    assert "up -d --no-build wuwaterm-api" in log
    assert not re.search(r"up -d --no-build wuwaterm$", log, re.MULTILINE)
    assert "run --rm wuwaterm-builder refresh-data" in log
    assert "device issue --name setup --scopes translate,meta" in log
    assert "setup: done." in result.stdout


def test_reinstall_keeps_data_and_the_issued_device(install_root):
    write_env(install_root)
    assert run_setup(install_root, "--no-adopt").returncode == 0
    (install_root / "docker.log").unlink()

    second = run_setup(install_root, "--no-adopt")

    assert second.returncode == 0, second.stderr
    log = docker_log(install_root)
    assert "wuwaterm-builder" not in log
    assert "device issue" not in log


def test_missing_model_configuration_is_refused_before_docker_work(install_root):
    (install_root / ".env").write_text(
        "WUWATERM_OPENAI_BASE_URL=https://model.invalid/v1\n"
        "WUWATERM_OPENAI_API_KEY=\n"
        "WUWATERM_OPENAI_MODEL=stub-model\n",
        encoding="utf-8",
    )

    result = run_setup(install_root, "--no-adopt")

    assert result.returncode == 1
    assert "WUWATERM_OPENAI_API_KEY is empty" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
    assert docker_log(install_root).strip() == "docker compose version"


def product_checkout(root: pathlib.Path) -> pathlib.Path:
    """The shape deploy/setup.sh needs to recognise a WuwaTerm-Telegram checkout."""
    product = root / "product"
    (product / "deploy").mkdir(parents=True)
    (product / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (product / "core-lock.json").write_text('{"version": "0.8.0"}\n', encoding="utf-8")
    return product


def env_values(path: pathlib.Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )


def test_with_telegram_routes_to_the_standalone_product_and_starts_no_bot(install_root):
    # Routing the surface needs no bot token here: the product has its own .env.
    write_env(install_root)

    result = run_setup(install_root, "--with-telegram", "--no-adopt")

    assert result.returncode == 0, result.stderr
    assert "My-Denia/WuwaTerm-Telegram" in result.stdout
    assert "This checkout starts no bot." in result.stdout
    # The product's own entry is named, and the shared service still installs.
    assert "prepare_docker_context.py" in result.stdout
    log = docker_log(install_root)
    assert "up -d --no-build wuwaterm-api" in log
    # The inline bot is neither built, pulled nor started.
    assert not re.search(r"(build|pull|up -d --no-build) wuwaterm$", log, re.MULTILINE), log


def test_with_legacy_bot_keeps_the_deprecated_inline_bot_explicit(install_root):
    write_env(install_root)  # no token
    refused = run_setup(install_root, "--with-legacy-bot", "--no-adopt")

    assert refused.returncode == 1
    assert "TELEGRAM_BOT_TOKEN is empty" in refused.stderr
    assert "wuwaterm-bot" not in docker_log(install_root)

    write_env(install_root, telegram_token="123456:stub-token")
    accepted = run_setup(
        install_root, "--with-legacy-bot", "--no-adopt", env={"STUB_BOT_STATE": "running"}
    )

    assert accepted.returncode == 0, accepted.stderr
    assert "deprecated inline bot (wuwaterm-bot) is running" in accepted.stdout
    assert "--with-legacy-bot starts the deprecated inline bot" in accepted.stderr
    assert re.search(
        r"up -d --no-build wuwaterm$", docker_log(install_root), re.MULTILINE
    ), docker_log(install_root)


def test_the_two_telegram_installations_are_alternatives(install_root):
    result = run_setup(install_root, "--with-telegram", "--with-legacy-bot")

    assert result.returncode == 2
    assert "alternatives" in result.stderr
    assert docker_log(install_root) == ""


def test_telegram_dir_gets_the_database_and_only_the_bots_settings(install_root):
    write_env(install_root, telegram_token="123456:stub-token")
    with (install_root / ".env").open("a", encoding="utf-8") as handle:
        handle.write(
            "OWNER_USER_ID=42\n"
            "WUWATERM_API_STATE_DIR=state-api\n"
            "WUWATERM_API_WEB_EDGE_SECRET=do-not-copy\n"
            "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        )
    product = product_checkout(install_root)

    result = run_setup(install_root, "--telegram-dir", str(product), "--no-adopt")

    assert result.returncode == 0, result.stderr
    product_env = product / ".env"
    assert mode_of(product_env) == 0o600
    values = env_values(product_env)
    assert values["TELEGRAM_BOT_TOKEN"] == "123456:stub-token"
    assert values["OWNER_USER_ID"] == "42"
    assert values["WUWATERM_OPENAI_MODEL"] == "stub-model"
    # This installation's API and Web credentials belong to other processes.
    assert not [key for key in values if key.startswith(("WUWATERM_API_", "WUWATERM_SITE_"))]
    assert "do-not-copy" not in product_env.read_text(encoding="utf-8")
    # The product reads a copy of the shared database from its own checkout.
    assert (product / "data" / "terms.db").read_bytes() == (
        install_root / "data" / "terms.db"
    ).read_bytes()
    assert mode_of(product / "state") == 0o700
    assert f"copied the database to {product}/data/terms.db" in result.stdout
    assert f"wrote {product}/.env (mode 0600)" in result.stdout
    assert not re.search(
        r"(build|pull|up -d --no-build) wuwaterm$", docker_log(install_root), re.MULTILINE
    )

    # A second run keeps both, and does not copy the database over again.
    (product / "data" / "terms.db").write_text("older fixture", encoding="utf-8")
    again = run_setup(install_root, "--telegram-dir", str(product), "--no-adopt")

    assert again.returncode == 0, again.stderr
    assert "already present; keeping it" in again.stdout
    assert (product / "data" / "terms.db").read_text(encoding="utf-8") == "older fixture"


def test_telegram_dir_must_be_a_product_checkout(install_root):
    write_env(install_root, telegram_token="123456:stub-token")
    empty = install_root / "not-a-product"
    empty.mkdir()

    refused = run_setup(install_root, "--telegram-dir", str(empty), "--no-adopt")

    assert refused.returncode == 1
    assert "not a WuwaTerm-Telegram checkout" in refused.stderr

    itself = run_setup(install_root, "--telegram-dir", str(install_root), "--no-adopt")

    assert itself.returncode == 1
    assert "never share a directory" in itself.stderr
    assert not (install_root / "data" / "terms.db").exists()


def test_with_web_without_a_package_stops_before_building_data(install_root):
    write_env(install_root)

    result = run_setup(
        install_root,
        "--with-web",
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
        env={"STUB_CURL_FAIL_MATCH": "releases/latest"},
    )

    assert result.returncode == 1
    assert "no published WuwaTerm-Web package" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
    log = docker_log(install_root)
    assert "wuwaterm-builder" not in log
    assert "wuwaterm-api" not in log


def test_with_web_needs_the_external_origin(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)

    result = run_setup(
        install_root, "--with-web", "--web-package", str(package), "--no-adopt"
    )

    assert result.returncode == 1
    assert "--web-origin" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_with_web_local_package_writes_private_credentials(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)

    result = run_setup(
        install_root,
        "--with-web",
        "--web-package",
        str(package),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    web_env = install_root / "state-web" / "web.env"
    assert mode_of(web_env) == 0o600
    values = web_env_values(install_root)
    assert set(values) == {
        "WUWATERM_RUNTIME_MODE",
        "WUWATERM_WEB_ORIGIN",
        "WUWATERM_WEB_EDGE_SECRET",
        "WUWATERM_WEB_BASE_PATH",
        "WUWATERM_API_BASE_URL",
        "WUWATERM_SITE_DEVICE_TOKEN",
        "WUWATERM_TRANSLATION_ENABLED",
        "WUWATERM_LOCAL_DB_PATH",
    }
    assert values["WUWATERM_RUNTIME_MODE"] == "selfhost"
    assert values["WUWATERM_WEB_ORIGIN"] == "https://wuwaterm.example"
    assert values["WUWATERM_WEB_BASE_PATH"] == ""
    assert values["WUWATERM_TRANSLATION_ENABLED"] == "true"
    assert values["WUWATERM_API_BASE_URL"] == "http://127.0.0.1:8788"
    # The admission database lives outside the read-only /app bundle mount, so
    # Docker never has to create a mountpoint below it.
    assert values["WUWATERM_LOCAL_DB_PATH"] == "/var/lib/wuwaterm-web/pool.sqlite3"
    assert values["WUWATERM_SITE_DEVICE_TOKEN"].startswith("wtd1.dev123.")
    # The Web runtime rejects anything outside its edge-secret alphabet.
    edge = values["WUWATERM_WEB_EDGE_SECRET"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", edge)
    assert edge not in result.stdout
    assert edge not in result.stderr
    web_token = (install_root / "state-web" / "device.token").read_text(encoding="utf-8")
    assert "wtd1.dev123." in web_token
    log = docker_log(install_root)
    assert "device issue --name web --scopes translate,meta" in log
    assert "run -d --name wuwaterm-web" in log
    assert f"-v {package / 'bundle'}:/app:ro" in log
    assert f"-v {install_root / 'state-web' / 'data'}:/var/lib/wuwaterm-web:rw" in log
    assert not (package / "bundle" / "data").exists()
    assert "PORT=3000" in log
    assert "sh -c node start.mjs" in log
    assert mode_of(install_root / "state-web" / "data") == 0o700
    # Readiness is an authenticated request: page mints the cookie, then pool.
    curl_log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert "-H @-" in curl_log
    assert "-o /dev/null http://127.0.0.1:3000/" in curl_log
    assert "http://127.0.0.1:3000/api/pool" in curl_log
    assert edge not in curl_log


def test_web_env_with_the_old_app_data_path_gets_its_mountpoint(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    state.mkdir()
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_API_BASE_URL=http://127.0.0.1:8788\n"
        "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        "WUWATERM_TRANSLATION_ENABLED=true\n"
        "WUWATERM_LOCAL_DB_PATH=/app/data/pool.sqlite3\n",
        encoding="utf-8",
    )

    result = run_setup(
        install_root,
        "--with-web",
        "--web-package",
        str(package),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    log = docker_log(install_root)
    # Docker refuses to create a mountpoint below the read-only /app mount, so
    # an installation that still names /app/data keeps that path only with the
    # directory already present in the bundle.
    assert f"-v {package / 'bundle'}:/app:ro" in log
    assert f"-v {install_root / 'state-web' / 'data'}:/app/data:rw" in log
    assert (package / "bundle" / "data").is_dir()
    assert "WUWATERM_LOCAL_DB_PATH=/app/data/pool.sqlite3" in (state / "web.env").read_text(
        encoding="utf-8"
    )


def test_web_env_with_a_relative_database_path_is_refused(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    state.mkdir()
    (state / "web.env").write_text(
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_LOCAL_DB_PATH=pool.sqlite3\n",
        encoding="utf-8",
    )

    result = run_setup(
        install_root,
        "--with-web",
        "--web-package",
        str(package),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 1
    assert "WUWATERM_LOCAL_DB_PATH" in result.stderr
    assert "must be an absolute path" in result.stderr
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_web_package_archive_with_the_published_layout(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-archive-src", "manifest-root.json", WEB_ROOT_MANIFEST)
    archive = install_root / "wuwaterm-web-v0.1.0-linux-x64-root.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(package), "bundle"], check=True
    )

    result = run_setup(
        install_root,
        "--web-package",
        str(archive),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    extracted = install_root / "state-web" / "package" / "bundle"
    assert (extracted / "start.mjs").exists()
    assert f"-v {extracted}:/app:ro" in docker_log(install_root)

    # A re-run keeps the installed bundle and the stored origin; it needs
    # neither --web-package nor --web-origin and never reaches the network.
    (install_root / "curl.log").unlink()
    again = run_setup(install_root, "--with-web", "--no-adopt")

    assert again.returncode == 0, again.stderr
    assert "reusing the installed Web package" in again.stdout
    assert not (install_root / "state-web" / "manifest-root.json").exists()
    curl_log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert "releases/latest" not in curl_log


def test_web_package_built_for_another_prefix_is_refused(install_root):
    write_env(install_root)
    package = write_web_package(
        install_root / "web-prefix-package", "manifest-prefix.json", WEB_PREFIX_MANIFEST
    )

    result = run_setup(
        install_root,
        "--with-web",
        "--web-package",
        str(package),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 1
    assert "built for base path /private" in result.stderr
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_status_reports_without_creating_anything(install_root):
    write_env(install_root)

    result = run_setup(install_root, "--status")

    assert result.returncode == 0
    assert "WuwaTerm status" in result.stdout
    assert "api:" in result.stdout
    assert "telegram:     not installed (standalone product)" in result.stdout
    assert "legacy bot:   not installed" in result.stdout
    assert "web:" in result.stdout
    assert not (install_root / "data").exists()
    assert not (install_root / "state").exists()
    log = docker_log(install_root)
    for mutation in ("up -d", " build", "run --rm", "rm -f", " pull"):
        assert mutation not in log


def test_status_tells_the_two_telegram_installations_apart(install_root):
    write_env(install_root)

    result = run_setup(
        install_root,
        "--status",
        env={"STUB_PRODUCT_STATE": "running", "STUB_BOT_STATE": "exited"},
    )

    assert result.returncode == 0
    assert "telegram:     running (standalone product)" in result.stdout
    assert "legacy bot:   exited (deprecated inline bot)" in result.stdout


def test_check_fails_when_the_api_is_not_ready(install_root):
    write_env(install_root)

    result = run_setup(install_root, "--check", env={"STUB_CURL_FAIL": "1"})

    assert result.returncode == 1
    assert "not ready" in result.stdout


def test_existing_active_device_is_not_reissued(install_root):
    write_env(install_root)
    (install_root / "data").mkdir()
    (install_root / "data" / "terms.db").write_text("fixture", encoding="utf-8")

    result = run_setup(
        install_root,
        "--no-adopt",
        env={"STUB_DEVICES": "dev9  active  scopes=translate,meta  created=now  last_used=-  name=old"},
    )

    assert result.returncode == 0, result.stderr
    assert "active device already exists" in result.stdout
    assert "device issue" not in docker_log(install_root)
    assert not (install_root / "state-api" / "first-device.token").exists()


def test_status_probes_the_edge_gated_web_page(install_root):
    write_env(install_root)
    state = install_root / "state-web"
    state.mkdir()
    edge = "a" * 64
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={edge}\n"
        "WUWATERM_WEB_BASE_PATH=/private\n",
        encoding="utf-8",
    )
    (state / "web.port").write_text("3100\n", encoding="utf-8")

    result = run_setup(install_root, "--status")

    assert result.returncode == 0
    assert "web health:   ready (http://127.0.0.1:3100/private/)" in result.stdout
    curl_log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert "-H @-" in curl_log
    assert "http://127.0.0.1:3100/private/" in curl_log
    assert edge not in curl_log
    assert docker_log(install_root) != ""


def test_web_prefix_selects_the_prefix_package_and_base_path(install_root):
    write_env(install_root)
    (install_root / "data").mkdir()
    (install_root / "data" / "terms.db").write_text("fixture", encoding="utf-8")
    package = write_web_package(
        install_root / "web-prefix-package", "manifest-prefix.json", WEB_PREFIX_MANIFEST
    )

    result = run_setup(
        install_root,
        "--web-package",
        str(package),
        "--web-prefix",
        "/private",
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    values = web_env_values(install_root)
    assert values["WUWATERM_WEB_BASE_PATH"] == "/private"
    assert values["WUWATERM_WEB_ORIGIN"] == "https://wuwaterm.example"
    curl_log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert "http://127.0.0.1:3000/private/" in curl_log
    assert "http://127.0.0.1:3000/private/api/pool" in curl_log


def test_web_origin_must_be_an_origin(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)

    for origin in ("http://wuwaterm.example", "https://wuwaterm.example/private", "https://wuwaterm.example?x=1"):
        result = run_setup(
            install_root,
            "--web-package",
            str(package),
            "--web-origin",
            origin,
            "--no-adopt",
        )
        assert result.returncode == 1, origin
        assert "--web-origin" in result.stderr

    assert not (install_root / "state-web" / "web.env").exists()


# deploy/product-update.sh creates .deployments/ and its lock file BEFORE the
# adopt transaction runs, so an existing directory proves nothing about
# adoption. These stubs reproduce both outcomes: one records the deployment
# pointers together with the manifest they name, the other refuses with nothing
# but an empty directory left behind.
ADOPT_RECORDED = """#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
deployment_id=20260101T000000Z-0123456789abcdef0123456789abcdef
( umask 077
  mkdir -p "$root/.deployments/products"
  printf '{"deployment_id": "%s"}\\n' "$deployment_id" \
    > "$root/.deployments/products/$deployment_id.json"
  printf '%s\\n' "0123456789abcdef0123456789abcdef01234567" > "$root/.deploy_commit"
  printf '%s\\n' "$deployment_id" > "$root/.deploy_current"
)
printf '%s\\n' "$deployment_id"
"""

ADOPT_REFUSED = """#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
( umask 077; mkdir -p "$root/.deployments"; : >> "$root/.deployments/.deployment.lock" )
printf '%s\\n' "adopt: no legacy .deploy_commit to record" >&2
exit 1
"""

COMPOSE_STUB = """#!/bin/sh
exit 0
"""


def install_adopt_stub(root: pathlib.Path, script: str) -> None:
    """Give the checkout a product-update.sh and the verified Compose binary it wants."""
    target = root / "deploy" / "product-update.sh"
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)
    compose = root / "bin" / "docker-compose"
    compose.write_text(COMPOSE_STUB, encoding="utf-8")
    compose.chmod(0o755)
    (root / "bin" / "docker-compose.sha256").write_text(
        f"{'0' * 64}  docker-compose\n", encoding="utf-8"
    )


def test_status_reads_the_deployment_pointer_not_the_directory(install_root):
    write_env(install_root)
    (install_root / ".deployments").mkdir()

    result = run_setup(install_root, "--status")

    assert result.returncode == 0
    assert "updates:      not adopted" in result.stdout


def test_failed_adoption_is_reported_and_retried_instead_of_called_adopted(install_root):
    write_env(install_root)
    install_adopt_stub(install_root, ADOPT_REFUSED)

    first = run_setup(install_root)

    assert first.returncode == 0, first.stderr
    assert "setup: adoption failed" in first.stderr
    assert "updates:      not adopted" in first.stdout
    assert "updates:      adopted (" not in first.stdout
    # The failed product-update.sh adopt left its directory and lock behind.
    assert (install_root / ".deployments").is_dir()
    assert not (install_root / ".deploy_current").exists()

    second = run_setup(install_root)

    assert second.returncode == 0, second.stderr
    assert "already adopted" not in second.stdout
    assert "setup: adoption failed" in second.stderr
    assert "updates:      not adopted" in second.stdout


def test_recorded_adoption_is_reported_and_not_repeated(install_root):
    write_env(install_root)
    install_adopt_stub(install_root, ADOPT_RECORDED)

    first = run_setup(install_root)

    assert first.returncode == 0, first.stderr
    assert "setup: adopted the installed topology" in first.stdout
    assert "updates:      adopted (deploy/product-update.sh)" in first.stdout

    second = run_setup(install_root)

    assert second.returncode == 0, second.stderr
    assert "deployment metadata already adopted" in second.stdout
    assert "adoption failed" not in second.stderr
    assert "updates:      adopted (deploy/product-update.sh)" in second.stdout
