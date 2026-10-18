"""Same-source identity overlap: isolated updater/fake Docker, no production."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from scripts.deployment_manifest import build_manifest, write_manifest
from test_full_deploy_recovery import (
    API_ROLLBACK_REF,
    BOT_ROLLBACK_REF,
    JOURNAL_RELPATH,
    NEW_COMMIT,
    NEW_IMAGE,
    OLD_COMMIT,
    OLD_DEPLOYED,
    OLD_IMAGE,
    TXN,
    UNKNOWN_IMAGE,
    _docker_log,
    _journal,
    _plant,
    _run_mode,
    _run_recover,
    _surface,
)

pytest_plugins = ["test_full_deploy_recovery"]
CANONICAL_REF = f"wuwaterm-runtime:{OLD_COMMIT}"


def _same_source(harness):
    root, env, old_hash, _new_hash, old_bytes, _new_bytes, backup = harness
    # Make the actual full shell admission consistent: git source, target
    # image answers, observed old containers and existing immutable manifest
    # all describe exactly the same source/image/database binding.
    git = root.parent / "bin" / "git"
    git.write_text(git.read_text().replace(NEW_COMMIT, OLD_COMMIT))
    docker = root.parent / "bin" / "docker"
    docker.write_text(docker.read_text().replace(NEW_IMAGE, OLD_IMAGE)
                      .replace(NEW_COMMIT, OLD_COMMIT))
    (root / "data" / "candidate-seed.db").write_bytes(old_bytes)
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    manifest.chmod(0o600)
    manifest.unlink()
    write_manifest(manifest, build_manifest(
        source_commit=OLD_COMMIT, image_ref=CANONICAL_REF,
        image_id=OLD_IMAGE, image_digest=OLD_IMAGE, image_revision=OLD_COMMIT,
        db_path=root / "data" / "terms.db", db_display_path="data/terms.db",
        backup_path="legacy", deployment_utc=OLD_DEPLOYED,
    ))
    for surface in ("bot", "api"):
        _surface(root, surface, image=OLD_IMAGE, ref=CANONICAL_REF, running=True)
    return root, env, old_hash, old_bytes, backup


def _shared_record(harness):
    root, env, old_hash, old_bytes, backup = _same_source(harness)
    journal = _journal(
        root, phase="armed", source_commit=OLD_COMMIT,
        old_db_sha=old_hash, db_backup=backup, new_db_sha=old_hash,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
        bot_ref=CANONICAL_REF, api_ref=CANONICAL_REF,
    )
    journal["target_image_id"] = OLD_IMAGE
    journal["target_image_digest"] = OLD_IMAGE
    _plant(root, journal)
    return root, env, old_bytes


def _mutations(root):
    commands = [line.split("|", 1)[1] for line in _docker_log(root)]
    return [x for x in commands if " stop " in x or " up -d " in x
            or " create " in x or x.startswith("rm ")]


def test_same_source_identical_binding_full_update_is_admitted(full_harness):
    root, env, _old_hash, old_bytes, _backup = _same_source(full_harness)
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    original = manifest.read_bytes()

    result = _run_mode(root, env, [])

    assert result.returncode == 0, result.stderr
    assert manifest.read_bytes() == original
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    assert not (root / JOURNAL_RELPATH).exists()


def test_same_source_sigkill_after_stop_and_real_promotion_recovers(full_harness):
    root, env, old_hash, old_bytes, _backup = _same_source(full_harness)
    barrier, release, pidfile = (root / name for name in ("same-barrier", "same-release", "same-pid"))
    env.update(FULL_KILL_BARRIER_FILE=str(barrier), FULL_KILL_RELEASE_FILE=str(release),
               FULL_KILL_PID_FILE=str(pidfile))
    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh")], cwd=root, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        for _ in range(400):
            if barrier.exists() and pidfile.exists():
                break
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"same-source updater failed before barrier: {out} {err}")
            time.sleep(0.05)
        else:
            pytest.fail("same-source promotion barrier not reached")
        journal = json.loads((root / JOURNAL_RELPATH).read_text())
        assert journal["old_pointer"] == journal["source_commit"] == OLD_COMMIT
        assert journal["old_db_sha256"] == journal["new_db_sha256"] == old_hash
        assert journal["phase"] == "armed"
        assert (root / "data" / "terms.db").read_bytes() == old_bytes
        assert (root / "bot-running").read_text().strip() == "false"
        assert (root / "api-running").read_text().strip() == "false"
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=30)
        assert process.returncode == -signal.SIGKILL

        result = _run_recover(root, env)

        assert result.returncode == 0, result.stderr
        assert "rolled back" in result.stdout
        assert not (root / JOURNAL_RELPATH).exists()
        assert (root / "data" / "terms.db").read_bytes() == old_bytes
        for surface in ("bot", "api"):
            assert (root / f"{surface}-running").read_text().strip() == "true"
            assert (root / f"{surface}-image").read_text().strip() == OLD_IMAGE
        assert _run_recover(root, env).returncode == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=30)


def test_shared_pointer_complete_new_binding_confirms_without_restore(full_harness):
    root, env, old_bytes = _shared_record(full_harness)
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    before = manifest.read_bytes()
    inode = (root / "data" / "terms.db").stat().st_ino

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert "confirmed on the new binding" in result.stdout
    assert not _mutations(root)
    assert not (root / JOURNAL_RELPATH).exists()
    assert manifest.read_bytes() == before
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    assert (root / "data" / "terms.db").stat().st_ino == inode


@pytest.mark.parametrize("observed", ["stopped", "missing", "partial", "rollback-refs"])
def test_shared_pointer_known_incomplete_binding_restores_old(full_harness, observed):
    root, env, old_bytes = _shared_record(full_harness)
    for surface in ("bot", "api"):
        _surface(root, surface, image=OLD_IMAGE,
                 ref=(BOT_ROLLBACK_REF if surface == "bot" else API_ROLLBACK_REF)
                 if observed == "rollback-refs" else CANONICAL_REF,
                 running=observed == "rollback-refs" or (observed == "partial" and surface == "api"),
                 present=not (observed == "missing" and surface == "bot"))

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert "rolled back" in result.stdout
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
    for surface in ("bot", "api"):
        assert (root / f"{surface}-running").read_text().strip() == "true"
        assert (root / f"{surface}-image").read_text().strip() == OLD_IMAGE


@pytest.mark.parametrize("fault", ["foreign-image", "unknown-db", "inspect-error"])
def test_shared_pointer_unknown_or_unobservable_refuses_without_mutation(full_harness, fault):
    root, env, old_bytes = _shared_record(full_harness)
    expected = old_bytes
    if fault == "foreign-image":
        _surface(root, "bot", image=UNKNOWN_IMAGE, ref="unrelated-runtime:unrelated", running=True)
    elif fault == "unknown-db":
        expected = b"unrecognized live bytes"
        (root / "data" / "terms.db").write_bytes(expected)
    else:
        docker = root.parent / "bin" / "docker"
        script = docker.read_text()
        needle = 'if [ "${1:-}" = inspect ]; then\n'
        assert script.count(needle) == 1
        docker.write_text(script.replace(needle, needle +
                          '  echo "Cannot connect to the Docker daemon" >&2\n  exit 1\n'))

    result = _run_recover(root, env)

    assert result.returncode == 1, result.stdout
    if fault == "inspect-error":
        assert "full_recovery_unobservable" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == expected
    assert not _mutations(root)
    assert (root / "bot-running").read_text().strip() == "true"


def test_shared_pointer_recovery_reenters_after_restore_command_failure(full_harness):
    root, env, old_bytes = _shared_record(full_harness)
    for surface in ("bot", "api"):
        _surface(root, surface, image=OLD_IMAGE, ref=CANONICAL_REF, running=False)
    env["FULL_RECOVERY_FAIL_UP_ONCE"] = "1"

    first = _run_recover(root, env)

    assert first.returncode == 1
    assert (root / JOURNAL_RELPATH).exists()
    second = _run_recover(root, env)
    assert second.returncode == 0, second.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == old_bytes
