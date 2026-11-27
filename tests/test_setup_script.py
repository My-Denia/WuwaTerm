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
    dev_name=fixture
    prev=
    for arg in "$@"; do
      if [ "$prev" = "--name" ]; then dev_name=$arg; break; fi
      prev=$arg
    done
    printf 'device_id: dev-%s\\n' "$dev_name"
    printf 'device_name: %s\\n' "$dev_name"
    printf 'scopes: translate,meta\\n'
    printf 'created_at: 2026-01-01T00:00:00Z\\n' ;;
  *"build-db"*) : > "$STUB_ROOT/data/terms.candidate.db" ;;
  "load -i "*)
    if [ "${STUB_TELEGRAM_LOAD_FAIL:-0}" = 1 ]; then
      printf 'Error: simulated load failure\\n' >&2
      exit 1
    fi
    printf '%s\\n' "${STUB_TELEGRAM_LOAD_OUTPUT:-Loaded image: wuwaterm-telegram:v0.7.0}" ;;
  "image inspect "*)
    if [ "${STUB_TELEGRAM_IMAGE_PRESENT:-1}" = 1 ]; then
      printf 'sha256:0000000000000000000000000000000000000000000000000000000000000000\\n'
      exit 0
    fi
    exit 1 ;;
  "exec wuwaterm-telegram python -B -m wuwaterm_telegram.operations health")
    if [ "${STUB_TELEGRAM_HEALTH_FAIL:-0}" = 1 ]; then
      exit 1
    fi
    exit 0 ;;
  "inspect -f {{.State.Status}} wuwaterm-bot") printf '%s\\n' "${STUB_BOT_STATE:-}" ;;
  "inspect -f {{.State.Status}} wuwaterm-telegram") printf '%s\\n' "${STUB_PRODUCT_STATE:-}" ;;
  *'org.wuwaterm.managed-by'*'wuwaterm-telegram')
    printf '%s\\n' "${STUB_PRODUCT_MANAGED_BY:-}" ;;
  "inspect -f {{.State.Status}} deploy-wuwaterm-telegram-1") printf '%s\\n' "${STUB_AUTO_PRODUCT_STATE:-}" ;;
  "inspect -f {{.State.Status}} wuwaterm-telegram-wuwaterm-telegram-1") printf '%s\\n' "${STUB_PROJECT_PRODUCT_STATE:-}" ;;
  "ps -aq --filter label=com.docker.compose.service=wuwaterm-telegram")
    if [ "${STUB_DOCKER_PS_FAIL:-0}" = 1 ]; then
      printf 'Error: simulated docker ps failure\\n' >&2
      exit 1
    fi
    printf '%s\\n' "${STUB_PRODUCT_LABEL_IDS:-}" ;;
  "inspect -f {{.State.Status}} wuwaterm-web")
    if [ "${STUB_WEB_ABSENT:-0}" = 1 ]; then
      printf 'Error: No such object: wuwaterm-web\\n' >&2
      exit 1
    fi
    printf '%s\\n' "${STUB_WEB_STATE:-running}" ;;
  "rm -f wuwaterm-web")
    if [ "${STUB_WEB_RM_FAIL:-0}" = 1 ]; then
      printf 'Error: simulated stop failure\\n' >&2
      exit 1
    fi
    exit 0 ;;
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
    assert token.startswith("wtd1.dev-setup.")
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


def write_telegram_package(
    root: pathlib.Path, *, include_sums: bool = True, bad_sum: bool = False
) -> pathlib.Path:
    """Create a local Telegram OCI archive package directory."""
    import hashlib

    root.mkdir(parents=True, exist_ok=True)
    archive = root / "image.oci.tar"
    archive.write_bytes(b"fake-oci-archive-content\n")
    if include_sums:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if bad_sum:
            digest = "0" * 64
        (root / "SHA256SUMS").write_text(f"{digest}  image.oci.tar\n", encoding="utf-8")
    return root


def test_with_telegram_orchestrates_core_and_http_mode_container(install_root):
    write_env(install_root, telegram_token="123456:stub-tg-secret")
    with (install_root / ".env").open("a", encoding="utf-8") as handle:
        handle.write(
            "OWNER_USER_ID=42\n"
            "WUWATERM_API_WEB_EDGE_SECRET=do-not-copy\n"
            "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev-other.secret\n"
        )
    tg_pkg = write_telegram_package(install_root / "tg-pkg")

    result = run_setup(
        install_root,
        "--with-telegram",
        "--telegram-package",
        str(tg_pkg),
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    assert "123456:stub-tg-secret" not in result.stdout
    assert "123456:stub-tg-secret" not in result.stderr
    tg_root = install_root / "state-telegram"
    assert mode_of(tg_root) == 0o700
    assert mode_of(tg_root / "state") == 0o700
    token_file = tg_root / "device.token"
    assert mode_of(token_file) == 0o600
    tg_token = token_file.read_text(encoding="utf-8").strip()
    assert tg_token.startswith("wtd1.dev-telegram.")
    assert tg_token.rsplit(".", 1)[1] not in result.stdout
    assert tg_token.rsplit(".", 1)[1] not in result.stderr
    tg_env = tg_root / "telegram.env"
    assert mode_of(tg_env) == 0o600
    values = env_values(tg_env)
    assert values["WUWATERM_CORE_API_URL"] == "http://127.0.0.1:8788"
    assert values["WUWATERM_CORE_API_TOKEN"] == tg_token
    assert "WUWATERM_CORE_MODE" not in values
    assert "WUWATERM_API_BASE_URL" not in values
    assert "WUWATERM_API_DEVICE_TOKEN" not in values
    assert values["TELEGRAM_BOT_TOKEN"] == "123456:stub-tg-secret"
    assert values["OWNER_USER_ID"] == "42"
    assert values["WUWATERM_STATE_DIR"] == "/app/state"
    assert "WUWATERM_OPENAI_API_KEY" not in values
    assert "WUWATERM_API_WEB_EDGE_SECRET" not in values
    assert "WUWATERM_SITE_DEVICE_TOKEN" not in values
    assert "do-not-copy" not in tg_env.read_text(encoding="utf-8")
    assert (tg_root / "image.version").read_text(encoding="utf-8").strip() == "local-package"
    log = docker_log(install_root)
    assert "up -d --no-build wuwaterm-api" in log
    assert "device issue --name telegram --scopes translate,meta" in log
    assert f"load -i {tg_pkg / 'image.oci.tar'}" in log
    assert "rm -f wuwaterm-telegram" not in log
    assert (
        "run -d --name wuwaterm-telegram --restart unless-stopped --network host "
        "--label org.wuwaterm.managed-by=setup.sh"
    ) in log
    assert "--label com.docker.compose.service=wuwaterm-telegram" not in log
    assert f"--env-file {tg_env}" in log
    assert f"-v {install_root / 'data'}:/app/data:ro" in log
    assert f"-v {tg_root / 'state'}:/app/state:rw" in log
    assert "--tmpfs /run/wuwaterm-telegram:" in log
    assert "exec wuwaterm-telegram python -B -m wuwaterm_telegram.operations health" in log
    assert not re.search(r"(build|pull|up -d --no-build) wuwaterm$", log, re.MULTILINE), log

    # Re-running against a setup.sh-managed container replaces only that container
    # and reuses the loaded image, token and env without network.
    (install_root / "docker.log").unlink()
    (install_root / "curl.log").unlink()
    again = run_setup(
        install_root,
        "--with-telegram",
        "--no-adopt",
        env={"STUB_PRODUCT_STATE": "running", "STUB_PRODUCT_MANAGED_BY": "setup.sh"},
    )
    assert again.returncode == 0, again.stderr
    assert "reusing the installed Telegram image" in again.stdout
    again_log = docker_log(install_root)
    assert "rm -f wuwaterm-telegram" in again_log
    assert "device issue" not in again_log
    assert "load -i" not in again_log
    assert "SHA256SUMS" not in (install_root / "curl.log").read_text(encoding="utf-8")

    # If state-telegram/device.token is reissued, WUWATERM_CORE_API_TOKEN in
    # state-telegram/telegram.env is synchronized to match it.
    token_file.unlink()
    reissued = run_setup(
        install_root,
        "--with-telegram",
        "--no-adopt",
        env={"STUB_PRODUCT_STATE": "running", "STUB_PRODUCT_MANAGED_BY": "setup.sh"},
    )
    assert reissued.returncode == 0, reissued.stderr
    new_token = token_file.read_text(encoding="utf-8").strip()
    assert new_token != tg_token
    assert env_values(tg_env)["WUWATERM_CORE_API_TOKEN"] == new_token
    assert mode_of(tg_env) == 0o600


def test_with_telegram_and_with_all_fail_closed_on_default_v061(install_root):
    (install_root / "contracts").mkdir()
    shutil.copy2(ROOT / "contracts" / "products.json", install_root / "contracts" / "products.json")
    write_env(install_root, telegram_token="123456:stub-tg-secret")
    web_pkg = write_web_package(
        install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST
    )

    tg_only = run_setup(install_root, "--with-telegram", "--no-adopt")
    assert tg_only.returncode == 1
    assert "v0.6.1 does not include the HTTP Core backend" in tg_only.stderr
    assert "No published WuwaTerm-Telegram release includes the HTTP Core backend yet (latest is v0.6.1)" in tg_only.stderr
    assert ">= v0.7.0" in tg_only.stderr
    assert "--telegram-dir" in tg_only.stderr
    assert "Pass WUWATERM_TELEGRAM_VERSION=v0.7.0" not in tg_only.stderr
    assert not (install_root / "data" / "terms.db").exists()
    assert not (install_root / "curl.log").exists()
    assert "run -d --name wuwaterm-telegram" not in docker_log(install_root)

    with_all = run_setup(
        install_root,
        "--with-all",
        "--web-origin",
        "https://wuwaterm.example",
        "--web-package",
        str(web_pkg),
        "--no-adopt",
    )
    assert with_all.returncode == 1
    assert "v0.6.1 does not include the HTTP Core backend" in with_all.stderr
    assert not (install_root / "data" / "terms.db").exists()
    assert "run -d --name wuwaterm-telegram" not in docker_log(install_root)
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_with_telegram_v070_release_orchestrates_and_invalidates_cache_on_version_change(
    install_root,
):
    import hashlib

    write_env(install_root, telegram_token="123456:stub-v070-secret")
    archive_bytes = b"fake-v070-oci-archive\n"
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    # Replace curl stub so release downloads write matching SHA256SUMS + image.oci.tar.
    (install_root / "bin" / "curl").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "curl $*" >> "$STUB_CURL_LOG"\n'
        'if [ "${STUB_CURL_FAIL:-0}" = 1 ]; then exit 7; fi\n'
        "out=\n"
        "prev=\n"
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-o" ]; then out=$arg; prev=; continue; fi\n'
        '  prev=$arg\n'
        "done\n"
        'case "$*" in\n'
        "  *SHA256SUMS*)\n"
        f'    [ -n "$out" ] && printf \'%s  image.oci.tar\\n\' "{archive_sha}" >"$out" ;;\n'
        "  *image.oci.tar*)\n"
        '    [ -n "$out" ] && printf \'fake-v070-oci-archive\\n\' >"$out" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (install_root / "bin" / "curl").chmod(0o755)

    result = run_setup(
        install_root,
        "--with-telegram",
        "--no-adopt",
        env={
            "WUWATERM_TELEGRAM_VERSION": "v0.7.0",
            "STUB_TELEGRAM_LOAD_OUTPUT": "Loaded image: wuwaterm-telegram:v0.7.0",
        },
    )

    assert result.returncode == 0, result.stderr
    curl_text = (install_root / "curl.log").read_text(encoding="utf-8")
    assert "/releases/download/v0.7.0/SHA256SUMS" in curl_text
    assert "/releases/download/v0.7.0/image.oci.tar" in curl_text
    tg_root = install_root / "state-telegram"
    assert (tg_root / "image.version").read_text(encoding="utf-8").strip() == "v0.7.0"
    assert (tg_root / "image.ref").read_text(encoding="utf-8").strip() == "wuwaterm-telegram:v0.7.0"
    values = env_values(tg_root / "telegram.env")
    assert values["WUWATERM_CORE_API_URL"] == "http://127.0.0.1:8788"
    assert values["WUWATERM_CORE_API_TOKEN"] == (tg_root / "device.token").read_text(
        encoding="utf-8"
    ).strip()
    assert "WUWATERM_CORE_MODE" not in values
    log = docker_log(install_root)
    assert "--label org.wuwaterm.managed-by=setup.sh" in log
    assert "--label com.docker.compose.service=wuwaterm-telegram" not in log
    assert f"-v {install_root / 'data'}:/app/data:ro" in log
    assert f"-v {tg_root / 'state'}:/app/state:rw" in log
    assert "exec wuwaterm-telegram python -B -m wuwaterm_telegram.operations health" in log

    # Same version reuses cache without network.
    (install_root / "curl.log").unlink()
    (install_root / "docker.log").unlink()
    same = run_setup(
        install_root,
        "--with-telegram",
        "--no-adopt",
        env={
            "WUWATERM_TELEGRAM_VERSION": "v0.7.0",
            "STUB_PRODUCT_STATE": "running",
            "STUB_PRODUCT_MANAGED_BY": "setup.sh",
        },
    )
    assert same.returncode == 0, same.stderr
    assert "reusing the installed Telegram image wuwaterm-telegram:v0.7.0 (v0.7.0)" in same.stdout
    assert "SHA256SUMS" not in (install_root / "curl.log").read_text(encoding="utf-8")

    # Bumping WUWATERM_TELEGRAM_VERSION invalidates the cached image.ref and fetches v0.8.0.
    (install_root / "curl.log").unlink()
    (install_root / "docker.log").unlink()
    upgraded = run_setup(
        install_root,
        "--with-telegram",
        "--no-adopt",
        env={
            "WUWATERM_TELEGRAM_VERSION": "v0.8.0",
            "STUB_PRODUCT_STATE": "running",
            "STUB_PRODUCT_MANAGED_BY": "setup.sh",
            "STUB_TELEGRAM_LOAD_OUTPUT": "Loaded image: wuwaterm-telegram:v0.8.0",
        },
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert "/releases/download/v0.8.0/SHA256SUMS" in (install_root / "curl.log").read_text(
        encoding="utf-8"
    )
    assert (tg_root / "image.version").read_text(encoding="utf-8").strip() == "v0.8.0"
    assert (tg_root / "image.ref").read_text(encoding="utf-8").strip() == "wuwaterm-telegram:v0.8.0"


@pytest.mark.parametrize(
    ("stub_env", "expected_error"),
    [
        ({"STUB_PRODUCT_STATE": "running"}, "not managed by deploy/setup.sh"),
        ({"STUB_PRODUCT_STATE": "exited"}, "not managed by deploy/setup.sh"),
        (
            {
                "STUB_PRODUCT_LABEL_IDS": "deploy-wuwaterm-telegram-1",
                "STUB_AUTO_PRODUCT_STATE": "running",
            },
            "existing Compose-managed wuwaterm-telegram container",
        ),
        (
            {
                "STUB_PRODUCT_LABEL_IDS": "deploy-wuwaterm-telegram-1",
                "STUB_AUTO_PRODUCT_STATE": "exited",
            },
            "existing Compose-managed wuwaterm-telegram container",
        ),
        (
            {"STUB_DOCKER_PS_FAIL": "1"},
            "could not inspect existing Compose-managed wuwaterm-telegram containers via docker ps",
        ),
    ],
)
def test_with_telegram_fails_closed_on_unmanaged_or_compose_container_without_rm(
    install_root, stub_env, expected_error
):
    write_env(install_root, telegram_token="123456:stub-tg-secret")
    tg_pkg = write_telegram_package(install_root / "tg-pkg")

    result = run_setup(
        install_root,
        "--with-telegram",
        "--telegram-package",
        str(tg_pkg),
        "--no-adopt",
        env=stub_env,
    )

    assert result.returncode == 1
    assert expected_error in result.stderr
    log = docker_log(install_root)
    assert "rm -f" not in log
    assert "run -d --name wuwaterm-telegram" not in log
    assert not (install_root / "data" / "terms.db").exists()


def test_with_telegram_refuses_bad_checksum_before_data_build(install_root):
    write_env(install_root, telegram_token="123456:stub-tg-secret")
    bad_pkg = write_telegram_package(install_root / "bad-tg-pkg", bad_sum=True)

    result = run_setup(
        install_root,
        "--with-telegram",
        "--telegram-package",
        str(bad_pkg),
        "--no-adopt",
    )

    assert result.returncode == 1
    assert "checksum does not match" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
    assert "wuwaterm-builder" not in docker_log(install_root)


def test_with_telegram_refuses_when_legacy_bot_is_running(install_root):
    write_env(install_root, telegram_token="123456:stub-tg-secret")
    tg_pkg = write_telegram_package(install_root / "tg-pkg")

    result = run_setup(
        install_root,
        "--with-telegram",
        "--telegram-package",
        str(tg_pkg),
        "--no-adopt",
        env={"STUB_BOT_STATE": "running"},
    )

    assert result.returncode == 1
    assert "deprecated inline bot (wuwaterm-bot) is running" in result.stderr
    assert "run -d --name wuwaterm-telegram" not in docker_log(install_root)


def test_with_windows_issues_dedicated_token_and_prints_release_guidance(install_root):
    (install_root / "contracts").mkdir()
    shutil.copy2(ROOT / "contracts" / "products.json", install_root / "contracts" / "products.json")
    write_env(install_root)

    result = run_setup(install_root, "--with-windows", "--no-adopt")

    assert result.returncode == 0, result.stderr
    win_token_file = install_root / "state-api" / "windows-device.token"
    assert mode_of(win_token_file) == 0o600
    win_token = win_token_file.read_text(encoding="utf-8").strip()
    assert win_token.startswith("wtd1.dev-windows.")
    assert win_token.rsplit(".", 1)[1] not in result.stdout
    assert "device issue --name windows --scopes translate,meta" in docker_log(install_root)
    snapshot = json.loads((ROOT / "contracts" / "products.json").read_text(encoding="utf-8"))
    win = next(p for p in snapshot["products"] if p["id"] == "windows")
    win_tag = win["release"]["tag"]
    win_zip = next(a for a in win["release"]["assets"] if a.endswith(".zip"))
    win_sha = win["release"]["asset_sha256"][win_zip]
    assert f"Windows desktop pairing ({win_tag})" in result.stdout
    assert win["release"]["url"] in result.stdout
    assert win_zip in result.stdout
    assert win_sha in result.stdout
    assert str(win_token_file) in result.stdout
    assert "windows:      token ready" in result.stdout

    # Idempotent re-run keeps the existing Windows token without reissuing.
    (install_root / "docker.log").unlink()
    again = run_setup(install_root, "--with-windows", "--no-adopt")
    assert again.returncode == 0, again.stderr
    assert win_token_file.read_text(encoding="utf-8").strip() == win_token
    assert "device issue" not in docker_log(install_root)


def test_with_all_combines_all_clients_with_isolated_tokens_and_state(install_root):
    (install_root / "contracts").mkdir()
    shutil.copy2(ROOT / "contracts" / "products.json", install_root / "contracts" / "products.json")
    write_env(install_root, telegram_token="123456:stub-all-tg-secret")
    web_pkg = write_web_package(
        install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST
    )
    tg_pkg = write_telegram_package(install_root / "tg-pkg")

    result = run_setup(
        install_root,
        "--with-all",
        "--web-origin",
        "https://wuwaterm.example",
        "--web-package",
        str(web_pkg),
        "--telegram-package",
        str(tg_pkg),
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    assert "123456:stub-all-tg-secret" not in result.stdout
    assert "123456:stub-all-tg-secret" not in result.stderr
    # Distinct tokens and isolated state directories for every client surface.
    first_tok = (install_root / "state-api" / "first-device.token").read_text(encoding="utf-8").strip()
    web_tok = (install_root / "state-web" / "device.token").read_text(encoding="utf-8").strip()
    tg_tok = (install_root / "state-telegram" / "device.token").read_text(encoding="utf-8").strip()
    win_tok = (install_root / "state-api" / "windows-device.token").read_text(encoding="utf-8").strip()
    assert len({first_tok, web_tok, tg_tok, win_tok}) == 4
    for tok_file in (
        install_root / "state-api" / "first-device.token",
        install_root / "state-web" / "device.token",
        install_root / "state-telegram" / "device.token",
        install_root / "state-api" / "windows-device.token",
        install_root / "state-web" / "web.env",
        install_root / "state-telegram" / "telegram.env",
    ):
        assert mode_of(tok_file) == 0o600
    for state_dir in (
        install_root / "state-api",
        install_root / "state-web",
        install_root / "state-web" / "data",
        install_root / "state-telegram",
        install_root / "state-telegram" / "state",
    ):
        assert mode_of(state_dir) == 0o700
    log = docker_log(install_root)
    assert "up -d --no-build wuwaterm-api" in log
    assert "run -d --name wuwaterm-web" in log
    assert "run -d --name wuwaterm-telegram" in log
    assert "device issue --name setup --scopes translate,meta" in log
    assert "device issue --name web --scopes translate,meta" in log
    assert "device issue --name telegram --scopes translate,meta" in log
    assert "device issue --name windows --scopes translate,meta" in log

    # Idempotent re-run keeps all tokens, DB, and packages without rebuilding or reissuing.
    (install_root / "docker.log").unlink()
    again = run_setup(
        install_root,
        "--with-all",
        "--web-origin",
        "https://wuwaterm.example",
        "--web-package",
        str(web_pkg),
        "--no-adopt",
    )
    assert again.returncode == 0, again.stderr
    again_log = docker_log(install_root)
    assert "wuwaterm-builder" not in again_log
    assert "device issue" not in again_log
    assert (install_root / "state-api" / "windows-device.token").read_text(encoding="utf-8").strip() == win_tok
    assert (install_root / "state-telegram" / "device.token").read_text(encoding="utf-8").strip() == tg_tok


def test_telegram_steps_pin_the_compose_project_name(install_root):
    """In legacy --telegram-dir mode, printed commands pin the Compose project name."""
    write_env(install_root, telegram_token="123456:stub-token")
    product = product_checkout(install_root)

    result = run_setup(install_root, "--telegram-dir", str(product), "--no-adopt")

    assert result.returncode == 0, result.stderr
    assert "`<project>-<service>-1`" in result.stdout, result.stdout
    assert "so the container is `wuwaterm-telegram`" not in result.stdout
    assert (
        "WUWATERM_POLL_HEALTH=1 docker compose -p wuwaterm-telegram "
        "-f deploy/docker-compose.yml up --build -d" in result.stdout
    ), result.stdout
    assert (
        "docker compose -p wuwaterm-telegram -f deploy/docker-compose.yml "
        "exec -T wuwaterm-telegram" in result.stdout
    ), result.stdout


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
        # The release host refuses the manifest, whichever release tag the
        # install resolved; the point is that nothing is built without it.
        env={"STUB_CURL_FAIL_MATCH": "manifest-root.json"},
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
    assert values["WUWATERM_SITE_DEVICE_TOKEN"].startswith("wtd1.dev-web.")
    # The Web runtime rejects anything outside its edge-secret alphabet.
    edge = values["WUWATERM_WEB_EDGE_SECRET"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", edge)
    assert edge not in result.stdout
    assert edge not in result.stderr
    web_token = (install_root / "state-web" / "device.token").read_text(encoding="utf-8")
    assert "wtd1.dev-web." in web_token
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


def _create_legacy_pool_db(path: pathlib.Path, *, second_key: int = 123) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE shared_pool (
                id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
                second_key INTEGER NOT NULL CHECK(second_key>=0),
                minute_key INTEGER NOT NULL CHECK(minute_key>=0),
                day_key INTEGER NOT NULL CHECK(day_key>=0),
                upstream_used INTEGER NOT NULL CHECK(upstream_used>=0),
                translation_minute_used INTEGER NOT NULL CHECK(translation_minute_used>=0),
                terms_used INTEGER NOT NULL CHECK(terms_used>=0),
                translation_used INTEGER NOT NULL CHECK(translation_used>=0),
                character_used INTEGER NOT NULL CHECK(character_used>=0),
                meta_used INTEGER NOT NULL CHECK(meta_used>=0),
                review_used INTEGER NOT NULL CHECK(review_used>=0)
            )"""
        )
        conn.execute("PRAGMA application_id=0x5755544d")
        conn.execute("PRAGMA user_version=1")
        conn.execute(
            "INSERT INTO shared_pool VALUES (1, ?, 2, 3, 4, 5, 6, 7, 8, 9, 10)",
            (second_key,),
        )
        conn.commit()
    finally:
        conn.close()


def _create_uncheckpointed_wal_pool_db(
    staging_dir: pathlib.Path, target_path: pathlib.Path, *, base_key: int, wal_key: int
) -> None:
    import sqlite3

    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_db = staging_dir / "tm.sqlite3"
    conn = sqlite3.connect(staged_db)
    try:
        conn.execute(
            """CREATE TABLE shared_pool (
                id INTEGER PRIMARY KEY NOT NULL CHECK(id=1),
                second_key INTEGER NOT NULL CHECK(second_key>=0),
                minute_key INTEGER NOT NULL CHECK(minute_key>=0),
                day_key INTEGER NOT NULL CHECK(day_key>=0),
                upstream_used INTEGER NOT NULL CHECK(upstream_used>=0),
                translation_minute_used INTEGER NOT NULL CHECK(translation_minute_used>=0),
                terms_used INTEGER NOT NULL CHECK(terms_used>=0),
                translation_used INTEGER NOT NULL CHECK(translation_used>=0),
                character_used INTEGER NOT NULL CHECK(character_used>=0),
                meta_used INTEGER NOT NULL CHECK(meta_used>=0),
                review_used INTEGER NOT NULL CHECK(review_used>=0)
            )"""
        )
        conn.execute("PRAGMA application_id=0x5755544d")
        conn.execute("PRAGMA user_version=1")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(
            "INSERT INTO shared_pool VALUES (1, ?, 2, 3, 4, 5, 6, 7, 8, 9, 10)",
            (base_key,),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("UPDATE shared_pool SET second_key = ? WHERE id = 1", (wal_key,))
        conn.commit()

        # Copy main db + live -wal/-shm BEFORE closing conn so the committed
        # WAL frames stay un-checkpointed in -wal.
        for suffix in ("", "-wal", "-shm"):
            src = pathlib.Path(f"{staged_db}{suffix}")
            if src.exists():
                dst = pathlib.Path(f"{target_path}{suffix}")
                shutil.copy2(src, dst)
                dst.chmod(0o600)
    finally:
        conn.close()

    wal_file = pathlib.Path(f"{target_path}-wal")
    assert wal_file.is_file() and wal_file.stat().st_size > 0
    with sqlite3.connect(f"file:{target_path}?immutable=1", uri=True) as ro_main:
        assert ro_main.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (base_key,)


def test_web_env_with_tm_sqlite3_pool_path_is_relocated_with_wal_backup(install_root, tmp_path):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    legacy_pool = data / "tm.sqlite3"
    _create_uncheckpointed_wal_pool_db(
        tmp_path / "wal_stage", legacy_pool, base_key=111, wal_key=987
    )
    # Include both unindented and indented duplicate entries to prove every
    # matching line is rewritten before relocating the database.
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_API_BASE_URL=http://127.0.0.1:8788\n"
        "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        "WUWATERM_TRANSLATION_ENABLED=true\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/tm.sqlite3\n"
        "  WUWATERM_LOCAL_DB_PATH = /var/lib/wuwaterm-web/tm.sqlite3\n",
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
    values = web_env_values(install_root)
    assert values["WUWATERM_LOCAL_DB_PATH"] == "/var/lib/wuwaterm-web/pool.sqlite3"
    env_lines = [line for line in (state / "web.env").read_text(encoding="utf-8").splitlines() if "WUWATERM_LOCAL_DB_PATH" in line]
    assert env_lines == ["WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/pool.sqlite3"]
    assert mode_of(state / "web.env") == 0o600
    assert (data / "pool.sqlite3").is_file()
    assert mode_of(data / "pool.sqlite3") == 0o600
    assert not (data / "tm.sqlite3").exists()
    assert not (data / "tm.sqlite3-wal").exists()
    assert not (data / "tm.sqlite3-shm").exists()

    with sqlite3.connect(data / "pool.sqlite3") as conn:
        row = conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone()
    assert row == (987,)
    assert "rm -f wuwaterm-web" in docker_log(install_root)
    assert "relocated WUWATERM_LOCAL_DB_PATH" in result.stdout


def test_web_migration_aborts_without_backup_when_stopping_container_fails(install_root):
    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    legacy_pool = data / "tm.sqlite3"
    _create_legacy_pool_db(legacy_pool, second_key=555)
    original_env = (
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/tm.sqlite3\n"
    )
    (state / "web.env").write_text(original_env, encoding="utf-8")

    result = run_setup(
        install_root,
        "--with-web",
        "--web-package",
        str(package),
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
        env={"STUB_WEB_RM_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "could not stop existing wuwaterm-web" in result.stderr
    assert legacy_pool.exists()
    assert not (data / "pool.sqlite3").exists()
    assert (state / "web.env").read_text(encoding="utf-8") == original_env


@pytest.mark.parametrize("crash_state", ["both_pool_files_env_tm", "only_new_pool_env_tm", "both_pool_files_env_pool"])
def test_web_migration_recovers_idempotently_across_crash_points(install_root, crash_state):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    _create_legacy_pool_db(data / "pool.sqlite3", second_key=777)
    if crash_state in {"both_pool_files_env_tm", "both_pool_files_env_pool"}:
        _create_legacy_pool_db(data / "tm.sqlite3", second_key=777)

    env_path_value = (
        "/var/lib/wuwaterm-web/pool.sqlite3"
        if crash_state == "both_pool_files_env_pool"
        else "/var/lib/wuwaterm-web/tm.sqlite3"
    )
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_API_BASE_URL=http://127.0.0.1:8788\n"
        "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        "WUWATERM_TRANSLATION_ENABLED=true\n"
        f"WUWATERM_LOCAL_DB_PATH={env_path_value}\n",
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
    assert web_env_values(install_root)["WUWATERM_LOCAL_DB_PATH"] == "/var/lib/wuwaterm-web/pool.sqlite3"
    assert not (data / "tm.sqlite3").exists()
    with sqlite3.connect(data / "pool.sqlite3") as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (777,)


@pytest.mark.parametrize("env_target", ["tm.sqlite3", "pool.sqlite3"])
def test_web_migration_fails_closed_when_both_pool_dbs_have_different_contents(install_root, env_target):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    _create_legacy_pool_db(data / "tm.sqlite3", second_key=987)
    _create_legacy_pool_db(data / "pool.sqlite3", second_key=111)
    original_env = (
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        f"WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/{env_target}\n"
    )
    (state / "web.env").write_text(original_env, encoding="utf-8")

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
    assert "different contents" in result.stderr
    assert (state / "web.env").read_text(encoding="utf-8") == original_env
    with sqlite3.connect(data / "tm.sqlite3") as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (987,)
    with sqlite3.connect(data / "pool.sqlite3") as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (111,)
    assert "rm -f wuwaterm-web" not in docker_log(install_root)
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_web_migration_rechecks_after_stop_to_block_toctou_write(install_root):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    tm_db = data / "tm.sqlite3"
    pool_db = data / "pool.sqlite3"
    _create_legacy_pool_db(tm_db, second_key=111)
    _create_legacy_pool_db(pool_db, second_key=111)

    original_env = (
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/tm.sqlite3\n"
    )
    (state / "web.env").write_text(original_env, encoding="utf-8")

    # Replace docker stub so `rm -f wuwaterm-web` simulates a final write to
    # tm.sqlite3 right before the container stops.
    docker_bin = install_root / "bin" / "docker"
    docker_bin.write_text(
        DOCKER_STUB.replace(
            '  "rm -f wuwaterm-web")\n',
            '  "rm -f wuwaterm-web")\n'
            '    python3 -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); '
            'c.execute(\'UPDATE shared_pool SET second_key = 987 WHERE id = 1\'); '
            'c.commit(); c.close()" "$STUB_ROOT/state-web/data/tm.sqlite3"\n',
        ),
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
    assert "differs from" in result.stderr
    assert tm_db.exists()
    with sqlite3.connect(tm_db) as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (987,)
    with sqlite3.connect(pool_db) as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (111,)
    assert (state / "web.env").read_text(encoding="utf-8") == original_env
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_web_migration_fails_closed_on_custom_pool_path_with_legacy_tm_sqlite3(install_root):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    _create_legacy_pool_db(data / "quota.sqlite3", second_key=111)
    _create_legacy_pool_db(data / "tm.sqlite3", second_key=987)
    original_env = (
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/quota.sqlite3\n"
    )
    (state / "web.env").write_text(original_env, encoding="utf-8")

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
    assert "refusing to delete legacy admission pool" in result.stderr
    assert (state / "web.env").read_text(encoding="utf-8") == original_env
    with sqlite3.connect(data / "tm.sqlite3") as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (987,)
    with sqlite3.connect(data / "quota.sqlite3") as conn:
        assert conn.execute("SELECT second_key FROM shared_pool WHERE id = 1").fetchone() == (111,)
    assert "rm -f wuwaterm-web" not in docker_log(install_root)
    assert "run -d --name wuwaterm-web" not in docker_log(install_root)


def test_web_rerun_keeps_real_tm_sqlite3_untouched_with_custom_pool_path(install_root):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    _create_legacy_pool_db(data / "quota.sqlite3", second_key=222)
    with sqlite3.connect(data / "tm.sqlite3") as tm_conn:
        tm_conn.execute("CREATE TABLE tm_schema (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        tm_conn.execute("INSERT INTO tm_schema VALUES ('format', 'wuwaterm-tm-v1')")
        tm_conn.execute("PRAGMA application_id=0x5755544e")
        tm_conn.execute("PRAGMA user_version=1")
        tm_conn.commit()
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_API_BASE_URL=http://127.0.0.1:8788\n"
        "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        "WUWATERM_TRANSLATION_ENABLED=true\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/quota.sqlite3\n",
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
    assert (data / "tm.sqlite3").is_file()
    with sqlite3.connect(data / "tm.sqlite3") as tm_conn:
        assert tm_conn.execute("SELECT value FROM tm_schema WHERE key='format'").fetchone() == ("wuwaterm-tm-v1",)


def test_web_rerun_keeps_real_tm_sqlite3_untouched(install_root):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    _create_legacy_pool_db(data / "pool.sqlite3", second_key=222)
    with sqlite3.connect(data / "tm.sqlite3") as tm_conn:
        tm_conn.execute("CREATE TABLE tm_schema (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
        tm_conn.execute("INSERT INTO tm_schema VALUES ('format', 'wuwaterm-tm-v1')")
        tm_conn.execute("PRAGMA application_id=0x5755544e")
        tm_conn.execute("PRAGMA user_version=1")
        tm_conn.commit()
    (state / "web.env").write_text(
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_API_BASE_URL=http://127.0.0.1:8788\n"
        "WUWATERM_SITE_DEVICE_TOKEN=wtd1.dev123.secret\n"
        "WUWATERM_TRANSLATION_ENABLED=true\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/pool.sqlite3\n",
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
    assert (data / "tm.sqlite3").is_file()
    with sqlite3.connect(data / "tm.sqlite3") as tm_conn:
        assert tm_conn.execute("SELECT value FROM tm_schema WHERE key='format'").fetchone() == ("wuwaterm-tm-v1",)


@pytest.mark.parametrize("kind", ["tm_db", "unknown_sqlite", "non_sqlite"])
def test_web_env_with_tm_sqlite3_pool_path_fails_closed_on_unrecognized_file(install_root, kind):
    import sqlite3

    write_env(install_root)
    package = write_web_package(install_root / "web-package", "manifest-root.json", WEB_ROOT_MANIFEST)
    state = install_root / "state-web"
    data = state / "data"
    data.mkdir(parents=True)
    target = data / "tm.sqlite3"
    if kind == "tm_db":
        with sqlite3.connect(target) as conn:
            conn.execute("PRAGMA application_id=0x5755544e")
            conn.execute("CREATE TABLE tm_schema (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
    elif kind == "unknown_sqlite":
        with sqlite3.connect(target) as conn:
            conn.execute("CREATE TABLE other_app (id INTEGER PRIMARY KEY)")
    else:
        target.write_text("not a sqlite file\n", encoding="utf-8")

    original_env = (
        "WUWATERM_RUNTIME_MODE=selfhost\n"
        "WUWATERM_WEB_ORIGIN=https://wuwaterm.example\n"
        f"WUWATERM_WEB_EDGE_SECRET={'b' * 64}\n"
        "WUWATERM_WEB_BASE_PATH=\n"
        "WUWATERM_LOCAL_DB_PATH=/var/lib/wuwaterm-web/tm.sqlite3\n"
    )
    (state / "web.env").write_text(original_env, encoding="utf-8")

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
    assert "refusing to migrate" in result.stderr
    assert (state / "web.env").read_text(encoding="utf-8") == original_env
    assert target.exists()
    assert not (data / "pool.sqlite3").exists()
    assert "rm -f wuwaterm-web" not in docker_log(install_root)
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


def test_status_finds_a_product_container_named_after_its_compose_project(
    install_root,
):
    """A release without an explicit container_name still reports as installed.

    `docker compose up` names that container after its project
    (`deploy-wuwaterm-telegram-1`), so asking only for `wuwaterm-telegram`
    reported a running product as not installed. The Compose service label is
    what identifies it.
    """
    write_env(install_root)

    result = run_setup(
        install_root,
        "--status",
        env={
            "STUB_PRODUCT_LABEL_IDS": "deploy-wuwaterm-telegram-1",
            "STUB_AUTO_PRODUCT_STATE": "running",
        },
    )

    assert result.returncode == 0
    assert "telegram:     running (standalone product)" in result.stdout
    assert "legacy bot:   not installed" in result.stdout
    assert (
        "ps -aq --filter label=com.docker.compose.service=wuwaterm-telegram"
        in docker_log(install_root)
    )
    for mutation in ("up -d", " build", "run --rm", "rm -f", " pull"):
        assert mutation not in docker_log(install_root)


def test_status_prefers_a_running_product_container_over_an_exited_one(
    install_root,
):
    """An earlier exited container must not mask a running labelled container."""
    write_env(install_root)

    result = run_setup(
        install_root,
        "--status",
        env={
            "STUB_PRODUCT_LABEL_IDS": (
                "deploy-wuwaterm-telegram-1\n"
                "wuwaterm-telegram-wuwaterm-telegram-1"
            ),
            "STUB_AUTO_PRODUCT_STATE": "exited",
            "STUB_PROJECT_PRODUCT_STATE": "running",
        },
    )

    assert result.returncode == 0
    assert "telegram:     running (standalone product)" in result.stdout

    stopped = run_setup(
        install_root,
        "--status",
        env={
            "STUB_PRODUCT_LABEL_IDS": (
                "deploy-wuwaterm-telegram-1\n"
                "wuwaterm-telegram-wuwaterm-telegram-1"
            ),
            "STUB_AUTO_PRODUCT_STATE": "exited",
            "STUB_PROJECT_PRODUCT_STATE": "created",
        },
    )

    assert stopped.returncode == 0
    assert "telegram:     exited (standalone product)" in stopped.stdout


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


# ---------------------------------------------------------------- release defaults
#
# The installer names one Telegram release and downloads one Web release. Both
# were literals inside the script and both went stale while the products moved
# on, so the default now comes from contracts/products.json when that file is
# readable and from a constant otherwise. A snapshot bump alone then cannot
# leave the installer naming last quarter's tag.

def _constants() -> dict[str, str]:
    text = (ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
    return dict(re.findall(r"^(DEFAULT_(?:TELEGRAM|WEB|WINDOWS)_VERSION)=([^\s]+)$", text, re.M))


def _recorded_tags() -> dict[str, str]:
    snapshot = json.loads((ROOT / "contracts" / "products.json").read_text(encoding="utf-8"))
    return {product["id"]: product["release"]["tag"] for product in snapshot["products"]}


def _recommended_tags() -> dict[str, str]:
    """The tags docs/products.md recommends in its top section."""
    text = (ROOT / "docs" / "products.md").read_text(encoding="utf-8")
    top = text.split("## 版本与兼容")[0]
    return {
        "telegram": re.search(r"当前\s*`([0-9]+\.[0-9]+\.[0-9]+)`\s*已正式发布", top).group(1),
        "web": re.search(r"Web\s*(v[0-9]+\.[0-9]+\.[0-9]+)\s*发布页", top).group(1),
    }


def test_release_defaults_match_the_recorded_snapshot_and_the_guide():
    constants = _constants()
    recorded = _recorded_tags()
    recommended = _recommended_tags()
    assert constants["DEFAULT_TELEGRAM_VERSION"] == recorded["telegram"] == "v0.6.1"
    assert recommended["telegram"] == recorded["telegram"].lstrip("v")
    assert constants["DEFAULT_WEB_VERSION"] == recorded["web"] == "v0.7.0"
    assert recommended["web"] == recorded["web"]
    assert constants["DEFAULT_WINDOWS_VERSION"] == recorded["windows"] == "v0.9.0"


def test_the_recorded_snapshot_supplies_the_telegram_release(install_root):
    """A checkout carrying the snapshot uses the Telegram tag the snapshot records."""
    (install_root / "contracts").mkdir()
    shutil.copy2(ROOT / "contracts" / "products.json", install_root / "contracts" / "products.json")
    write_env(install_root, telegram_token="1:stub-token")
    product = product_checkout(install_root)

    result = run_setup(install_root, "--telegram-dir", str(product), "--no-adopt")

    assert result.returncode == 0, result.stderr
    assert "release v0.6.1" in result.stdout
    assert "wuwaterm_telegram-0.6.1-py3-none-any.whl" in result.stdout

    # In default HTTP-mode orchestration, v0.6.1 fails closed before network access
    # because the HTTP Core backend requires >= v0.7.0.
    (install_root / "curl.log").unlink()
    http_result = run_setup(install_root, "--with-telegram", "--no-adopt")
    assert http_result.returncode == 1
    assert "v0.6.1 does not include the HTTP Core backend" in http_result.stderr
    assert not (install_root / "curl.log").exists()


def test_without_the_snapshot_the_constant_names_the_same_tag(install_root):
    write_env(install_root, telegram_token="1:stub-token")
    product = product_checkout(install_root)

    result = run_setup(install_root, "--telegram-dir", str(product), "--no-adopt")

    assert result.returncode == 0, result.stderr
    assert "release v0.6.1" in result.stdout


@pytest.mark.parametrize(("version", "expected"), [
    ("v0.1.0", "/releases/download/v0.1.0/manifest-root.json"),
    ("latest", "/releases/latest/download/manifest-root.json"),
])
def test_the_web_release_is_an_addressable_version_or_latest(install_root, version, expected):
    """The manifest URL is the version the install resolved, not a fixed one.

    The curl stub answers every request without writing bytes, so the install
    stops at its own verification; what this pins is the URL it asked for.
    """
    write_env(install_root)

    result = run_setup(
        install_root,
        "--with-web",
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
        env={"WUWATERM_WEB_VERSION": version},
    )

    assert result.returncode == 1
    log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert expected in log
    assert not (install_root / "data" / "terms.db").exists()


def test_the_default_web_release_is_the_recorded_tag(install_root):
    write_env(install_root)

    result = run_setup(
        install_root,
        "--with-web",
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
    )

    assert result.returncode == 1
    log = (install_root / "curl.log").read_text(encoding="utf-8")
    assert f"/releases/download/{_recorded_tags()['web']}/manifest-root.json" in log


@pytest.mark.parametrize("version", ["0.1.1", "v", "vX.Y", "main"])
def test_an_unusable_web_version_is_refused(install_root, version):
    write_env(install_root)

    result = run_setup(
        install_root,
        "--with-web",
        "--web-origin",
        "https://wuwaterm.example",
        "--no-adopt",
        env={"WUWATERM_WEB_VERSION": version},
    )

    assert result.returncode == 1
    assert "WUWATERM_WEB_VERSION must be latest or a tag like v0.3.0" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
