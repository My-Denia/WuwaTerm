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
  "inspect -f {{.State.Status}} wuwaterm-bot") printf '%s\\n' "${STUB_BOT_STATE:-running}" ;;
  "inspect -f {{.State.Status}} wuwaterm-web") printf '%s\\n' "${STUB_WEB_STATE:-running}" ;;
  "inspect -f {{.State.Status}} wuwaterm-api") printf '%s\\n' "running" ;;
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


def test_telegram_needs_its_token_and_is_started_only_when_asked(install_root):
    write_env(install_root)  # no token
    refused = run_setup(install_root, "--with-telegram", "--no-adopt")

    assert refused.returncode == 1
    assert "TELEGRAM_BOT_TOKEN is empty" in refused.stderr
    assert "wuwaterm-bot" not in docker_log(install_root)

    write_env(install_root, telegram_token="123456:stub-token")
    accepted = run_setup(install_root, "--with-telegram", "--no-adopt")

    assert accepted.returncode == 0, accepted.stderr
    assert "up -d --no-build wuwaterm" in docker_log(install_root)
    assert "setup: Telegram bot is running." in accepted.stdout
    assert re.search(r"up -d --no-build wuwaterm$", docker_log(install_root), re.MULTILINE)


def test_with_web_without_a_package_stops_before_building_data(install_root):
    write_env(install_root)

    result = run_setup(
        install_root,
        "--with-web",
        "--no-adopt",
        env={"STUB_CURL_FAIL_MATCH": "releases/latest"},
    )

    assert result.returncode == 1
    assert "no published WuwaTerm-Web package" in result.stderr
    assert not (install_root / "data" / "terms.db").exists()
    log = docker_log(install_root)
    assert "wuwaterm-builder" not in log
    assert "wuwaterm-api" not in log


def test_with_web_local_package_writes_private_credentials(install_root):
    write_env(install_root)
    package = install_root / "web-package"
    package.mkdir()
    (package / "manifest-root.json").write_text(
        json.dumps(
            {
                "package": "wuwaterm-web",
                "version": "0.1.0",
                "artifact": "wuwaterm-web-0.1.0-root.tar.gz",
                "start": "node server.js",
                "port": 3999,
                "health_path": "/api/readyz",
            }
        ),
        encoding="utf-8",
    )

    result = run_setup(
        install_root, "--with-web", "--web-package", str(package), "--no-adopt"
    )

    assert result.returncode == 0, result.stderr
    web_env = install_root / "state-web" / "web.env"
    assert mode_of(web_env) == 0o600
    values = dict(
        line.split("=", 1)
        for line in web_env.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert set(values) == {
        "WUWATERM_API_BASE_URL",
        "WUWATERM_SITE_DEVICE_TOKEN",
        "WUWATERM_WEB_EDGE_SECRET",
        "WUWATERM_WEB_BASE_PATH",
    }
    assert values["WUWATERM_API_BASE_URL"] == "http://127.0.0.1:8788"
    assert values["WUWATERM_SITE_DEVICE_TOKEN"].startswith("wtd1.dev123.")
    assert values["WUWATERM_WEB_EDGE_SECRET"] not in result.stdout
    web_token = (install_root / "state-web" / "device.token").read_text(encoding="utf-8")
    assert "wtd1.dev123." in web_token
    log = docker_log(install_root)
    assert "device issue --name web --scopes translate,meta" in log
    assert "run -d --name wuwaterm-web" in log
    assert "PORT=3999" in log
    assert "curl" in (install_root / "curl.log").read_text(encoding="utf-8")


def test_status_reports_without_creating_anything(install_root):
    write_env(install_root)

    result = run_setup(install_root, "--status")

    assert result.returncode == 0
    assert "WuwaTerm status" in result.stdout
    assert "api:" in result.stdout
    assert "telegram:" in result.stdout
    assert "web:" in result.stdout
    assert not (install_root / "data").exists()
    assert not (install_root / "state").exists()
    log = docker_log(install_root)
    for mutation in ("up -d", " build", "run --rm", "rm -f", " pull"):
        assert mutation not in log


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


def test_web_prefix_selects_the_prefix_package_and_base_path(install_root):
    write_env(install_root)
    (install_root / "data").mkdir()
    (install_root / "data" / "terms.db").write_text("fixture", encoding="utf-8")
    package = install_root / "web-prefix-package"
    package.mkdir()
    (package / "manifest-prefix.json").write_text(
        json.dumps(
            {
                "package": "wuwaterm-web",
                "version": "0.1.0",
                "artifact": "wuwaterm-web-0.1.0-prefix.tar.gz",
                "start": "node server.js",
                "port": 3998,
                "health_path": "/api/readyz",
            }
        ),
        encoding="utf-8",
    )

    result = run_setup(
        install_root,
        "--web-package",
        str(package),
        "--web-prefix",
        "/private",
        "--no-adopt",
    )

    assert result.returncode == 0, result.stderr
    web_env = (install_root / "state-web" / "web.env").read_text(encoding="utf-8")
    assert "WUWATERM_WEB_BASE_PATH=/private" in web_env
    log = docker_log(install_root)
    assert "PORT=3998" in log
    assert "WUWATERM_WEB_BASE_PATH=/private" in log
