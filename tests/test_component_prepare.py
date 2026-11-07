from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from scripts import component_prepare as prepare
from scripts import product_manifest as manifest


def _request(selection: str, receipts: dict[str, str], recipes: dict | None = None) -> dict:
    return {"selection": selection, "receipts": receipts, "recipes": recipes or {}}


def _image() -> dict:
    return {"Id": "sha256:" + "1" * 64,
            "Config": {"Env": ["LANG=C.UTF-8"], "Cmd": ["--db", "/app/data/terms.db"],
                       "Entrypoint": ["wuwaterm-telegram"], "User": "", "WorkingDir": "/app"}}


@pytest.mark.parametrize("selection,receipts", [
    ("api", {}), ("api", {"api": "a" * 64, "telegram": "b" * 64}),
    ("both", {"api": "a" * 64}), ("telegram", {"api": "a" * 64}),
])
def test_selected_receipts_must_be_exact(selection: str, receipts: dict) -> None:
    with pytest.raises(prepare.PrepareError):
        prepare._request(_request(selection, receipts))


def test_request_refuses_recipe_for_unselected_component() -> None:
    with pytest.raises(prepare.PrepareError):
        prepare._request(_request("api", {"api": "a" * 64}, {"telegram": {}}))
    assert prepare._request(_request("both", {"api": "a" * 64, "telegram": "b" * 64}))[0] == ["api", "telegram"]


def test_normalized_target_rejects_unrepresented_runtime_options() -> None:
    refs = {"project": "example", "service": "wuwaterm-telegram"}
    base = {"image": "candidate:placeholder", "network_mode": "host", "privileged": True}
    with pytest.raises(prepare.PrepareError):
        prepare._snapshot_from_compose(refs, base, copy.deepcopy(base), _image())


def test_nonempty_secret_override_is_not_persisted() -> None:
    refs = {"project": "example", "service": "wuwaterm-telegram"}
    base = {"image": "candidate:placeholder", "network_mode": "host",
            "environment": {"TELEGRAM_BOT_TOKEN": "synthetic-canary"}}
    with pytest.raises(prepare.PrepareError):
        prepare._snapshot_from_compose(refs, base, copy.deepcopy(base), _image())


@pytest.mark.skipif(not os.environ.get("COMPONENT_PREPARE_TEST_COMPOSE"), reason="standalone Compose not supplied")
def test_real_compose_renders_target_without_persisting_environment(tmp_path: Path) -> None:
    compose = Path(os.environ["COMPONENT_PREPARE_TEST_COMPOSE"])
    env_file = tmp_path / "fixture.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=synthetic-canary\n", encoding="ascii")
    config = tmp_path / "compose.yml"
    config.write_text(
        "services:\n"
        "  wuwaterm-telegram:\n"
        "    image: placeholder:local\n"
        "    container_name: example-telegram\n"
        "    network_mode: host\n"
        "    restart: unless-stopped\n"
        "    entrypoint: [wuwaterm-telegram]\n"
        "    command: [--db, /app/data/terms.db]\n"
        f"    env_file: [{env_file}]\n"
        "    environment:\n"
        "      WUWATERM_DB_PATH: /app/data/terms.db\n"
        "      WUWATERM_STATE_DIR: state/custom\n"
        "      WUWATERM_POLL_HEALTH: '1'\n"
        "    volumes:\n"
        f"      - {tmp_path}/data:/app/data:ro\n"
        f"      - {tmp_path}/state:/app/state\n"
        "    tmpfs:\n"
        "      - /run/wuwaterm-telegram:rw,noexec,nosuid,nodev,mode=0700\n",
        encoding="ascii",
    )
    refs = {"compose_files": [str(config)], "project_directory": str(tmp_path),
            "project": "example", "service": "wuwaterm-telegram", "interpolation_env_files": []}
    try:
        saved = os.dup(9)
    except OSError:
        saved = None
    (tmp_path / ".deployments").mkdir()
    fd = os.open(tmp_path / ".deployments" / ".deployment.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fd != 9:
            os.dup2(fd, 9)
        snapshot, references = prepare._render_recipe(tmp_path, refs, _image(), compose)
        interpolation = tmp_path / "interpolation.env"
        interpolation.write_text("TEST_ONLY_TOKEN=synthetic-canary\n", encoding="ascii")
        env_file.write_text("TELEGRAM_BOT_TOKEN=${TEST_ONLY_TOKEN}\n", encoding="ascii")
        with pytest.raises(prepare.PrepareError, match="cannot be reproduced"):
            prepare._render_recipe(tmp_path, {**refs, "interpolation_env_files": [str(interpolation)]},
                                   _image(), compose)
    finally:
        if fd != 9:
            os.close(fd)
        if saved is None:
            os.close(9)
        else:
            os.dup2(saved, 9)
            os.close(saved)
    assert set(references) == {str(config), str(env_file)}
    assert snapshot["entrypoint"] == ["wuwaterm-telegram"]
    assert snapshot["command"] == ["--db", "/app/data/terms.db"]
    assert snapshot["env_files"] == [str(env_file)]
    assert {item["name"]: item.get("value") for item in snapshot["environment_overrides"]}[
        "WUWATERM_STATE_DIR"] == "state/custom"
    assert "synthetic-canary" not in str(snapshot)
    assert snapshot["mounts"][-1] == {"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                                       "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700"]}
    manifest.verify_poll_health_mount(snapshot, effective_uid=0)
