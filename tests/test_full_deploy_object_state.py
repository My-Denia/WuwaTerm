"""PR160 object-state regressions: isolated fake Docker, not production."""
from __future__ import annotations

import subprocess

import pytest

from scripts.deployment_manifest import build_manifest, write_manifest
from test_full_deploy_recovery import (
    API_ALT_IMAGE,
    API_ROLLBACK_REF,
    BOT_ROLLBACK_REF,
    JOURNAL_RELPATH,
    NEW_COMMIT,
    NEW_IMAGE,
    OLD_COMMIT,
    OLD_DEPLOYED,
    OLD_IMAGE,
    OLD_REF,
    TXN,
    UNKNOWN_IMAGE,
    _docker_log,
    _journal,
    _plant,
    _run_recover,
    _set_db,
    _surface,
)

pytest_plugins = ["test_full_deploy_recovery"]


def _reality(harness, *, old_bot="stopped", observed="target"):
    root, env, old_hash, new_hash, old_bytes, new_bytes, backup = harness
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup,
        new_db_sha=new_hash, candidate_rel=f"data/candidates/terms.{TXN}.db",
        bot_id=None if old_bot == "absent" else OLD_IMAGE,
        bot_ref=None if old_bot == "absent" else OLD_REF,
        bot_running=old_bot == "running",
    ))
    _set_db(root, new_bytes)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "bot", image=OLD_IMAGE if observed == "original" else NEW_IMAGE,
             ref=OLD_REF if observed == "original" else f"wuwaterm-runtime:{NEW_COMMIT}",
             running=observed == "running", present=observed != "missing")
    return root, env, old_bytes


@pytest.mark.parametrize("observed", ["target", "missing", "original"])
def test_recorded_stopped_object_restores_presence_image_and_ref(full_harness, observed):
    root, env, old_bytes = _reality(full_harness, observed=observed)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    assert (root / "bot-present").exists()
    assert (root / "bot-running").read_text().strip() == "false"
    assert (root / "bot-image").read_text().strip() == OLD_IMAGE
    assert (root / "bot-ref").read_text().strip() == (
        OLD_REF if observed == "original" else BOT_ROLLBACK_REF
    )
    log = _docker_log(root)
    assert not any(" up -d " in line and "wuwaterm-api" not in line for line in log)
    if observed != "original":
        assert any(" create " in line and line.endswith(" wuwaterm") for line in log)


@pytest.mark.parametrize("running", [False, True])
def test_recorded_absent_object_removes_only_known_stopped_replacement(full_harness, running):
    root, env, old_bytes = _reality(
        full_harness, old_bot="absent", observed="running" if running else "target"
    )

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert not (root / "bot-present").exists()
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    # docker.log records "image-reference|arguments", not the binary name.
    commands = [line.split("|", 1)[1] for line in _docker_log(root)]
    removals = [command for command in commands if command.startswith("rm ")]
    assert removals == ["rm wuwaterm-bot"]
    assert not any("--force" in command or "--volumes" in command for command in removals)


@pytest.mark.parametrize("residue", ["stopped-target", "missing-stopped", "absent-target"])
def test_old_binding_verifier_does_not_certify_nonrunning_wrong_objects(full_harness, residue):
    root, env, old_bytes = _reality(
        full_harness, old_bot="absent" if residue == "absent-target" else "stopped",
        observed="missing" if residue == "missing-stopped" else "target",
    )
    _set_db(root, old_bytes)
    _surface(root, "api", image=OLD_IMAGE, ref=API_ROLLBACK_REF, running=True)
    result = subprocess.run(
        ["sh", "-c", "exec 9>>.deployments/.deployment.lock; flock -n 9 || exit 75; "
         "exec python3 scripts/full_update.py rollback-clear"],
        cwd=root, env=env, text=True, capture_output=True, timeout=30, check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    commands = [line.split("|", 1)[1] for line in _docker_log(root)]
    assert not any(" stop " in command or " up -d " in command or command.startswith("rm ")
                   for command in commands)


@pytest.mark.parametrize("owner", ["bot", "api", "foreign"])
def test_manifest_is_verified_against_its_own_recorded_image(full_harness, owner):
    root, env, old_hash, new_hash, old_bytes, new_bytes, backup = full_harness
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    manifest.chmod(0o600)
    manifest.unlink()
    image = {"bot": OLD_IMAGE, "api": API_ALT_IMAGE, "foreign": UNKNOWN_IMAGE}[owner]
    write_manifest(manifest, build_manifest(
        source_commit=OLD_COMMIT, image_ref=OLD_REF, image_id=image,
        image_digest=image, image_revision=OLD_COMMIT, db_path=root / backup,
        db_display_path="data/terms.db", backup_path="legacy", deployment_utc=OLD_DEPLOYED,
    ))
    (root / f"tag-{API_ROLLBACK_REF}").write_text(API_ALT_IMAGE + "\n")
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup,
        new_db_sha=new_hash, candidate_rel=f"data/candidates/terms.{TXN}.db",
        api_id=API_ALT_IMAGE,
    ))
    _set_db(root, new_bytes)
    for surface in ("bot", "api"):
        _surface(root, surface, image=NEW_IMAGE,
                 ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    result = _run_recover(root, env)

    if owner == "foreign":
        assert result.returncode == 1
        assert (root / JOURNAL_RELPATH).exists()
        assert (root / "data" / "terms.db").read_bytes() == new_bytes
        assert not any(" stop " in line or " up -d " in line for line in _docker_log(root))
    else:
        assert result.returncode == 0, result.stderr
        assert not (root / JOURNAL_RELPATH).exists()
        assert (root / "data" / "terms.db").read_bytes() == old_bytes
        assert (root / "bot-image").read_text().strip() == OLD_IMAGE
        assert (root / "api-image").read_text().strip() == API_ALT_IMAGE


@pytest.mark.parametrize("operation", ["create", "rm"])
def test_object_repair_reenters_after_partial_command_failure(full_harness, operation):
    root, env, old_bytes = _reality(
        full_harness, old_bot="absent" if operation == "rm" else "stopped"
    )
    env["FULL_FAIL_OBJECT_REPAIR_ONCE"] = operation

    first = _run_recover(root, env)

    assert first.returncode != 0
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    second = _run_recover(root, env)
    assert second.returncode == 0, second.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    if operation == "rm":
        assert not (root / "bot-present").exists()
    else:
        assert (root / "bot-present").exists()
        assert (root / "bot-running").read_text().strip() == "false"
        assert (root / "bot-image").read_text().strip() == OLD_IMAGE
