"""Interruption recovery for full-data deployments.

These tests simulate the kill/power-loss window the EXIT-trap rollback
cannot cover: a durable full-transaction journal is planted in the phase the
interruption landed in, the harness state is set to exactly that phase's
on-disk reality, and `vps-update.sh --recover-full` must restore the
recorded previous binding (or confirm an already-published one) by OBSERVED
state, never by journal phase alone.

Everything here runs against ISOLATED fake-docker harnesses with real
durable file operations; one test sends a real SIGKILL to the updater's
process group after the candidate database has been durably promoted and
before the next phase marker is written. A refused or failing recovery in
these tests says nothing about any production host; it exercises the
recovery tooling only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from test_deploy_scripts import (
    NEW_COMMIT,
    NEW_IMAGE,
    OLD_COMMIT,
    OLD_IMAGE,
    _make_deploy_harness,
)

ROOT = Path(__file__).resolve().parents[1]

TXN = "20261017T000000Z-1234"
OLD_DEPLOYED = "2026-07-15T00:00:00Z"
API_ALT_IMAGE = "sha256:" + "a" * 64
UNKNOWN_COMMIT = "c" * 40
UNKNOWN_IMAGE = "sha256:" + "3" * 64
JOURNAL_RELPATH = ".deployments/.full-transaction.json"
BOT_ROLLBACK_REF = f"wuwaterm-runtime:rollback-{TXN}"
API_ROLLBACK_REF = f"wuwaterm-runtime:rollback-api-{TXN}"
OLD_REF = "wuwaterm-runtime:old"


def _tool_hash(root: Path) -> str:
    """The fingerprint the recovery module demands over its tool code."""

    digest = hashlib.sha256()
    for relative in (
        "deploy/vps-update.sh",
        "deploy/docker-compose.yml",
        "scripts/deployment_manifest.py",
        "scripts/full_update.py",
    ):
        digest.update(relative.encode("ascii"))
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def _write_full_fake_docker(root: Path) -> None:
    """A fake docker daemon with per-surface observable state.

    Every surface keeps its own image id, Config.Image ref and running flag,
    so recovery must prove the binding per surface rather than per host.
    `docker image tag` records the rollback tag exactly as the daemon's
    tag store would, and `compose up` resolves it back.
    """

    fake_docker = root.parent / "bin" / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${FAKE_DEPLOY_ROOT:?}\"\n"
        "printf '%s|%s\\n' \"${WUWATERM_RUNTIME_IMAGE:-unset}\" \"$*\" "
        ">> \"$FAKE_DEPLOY_ROOT/docker.log\"\n"
        "printf 'docker %s\\n' \"$*\" >> \"$FAKE_DEPLOY_ROOT/actions.log\"\n"
        "service=\n"
        "case \"$*\" in\n"
        "  *wuwaterm-api*) service=api ;;\n"
        "  *' wuwaterm '*|*wuwaterm-bot*) service=bot ;;\n"
        "esac\n"
        "if [ \"${1:-}\" = inspect ]; then\n"
        # An absent container is PROVEN absence: the daemon's own "No such
        # object" answer. Anything else nonzero is an observation failure.
        "  if [ -z \"$service\" ] || [ ! -f \"$FAKE_DEPLOY_ROOT/$service-present\" ]; then\n"
        "    name=wuwaterm-bot\n"
        "    case \"$*\" in *wuwaterm-api*) name=wuwaterm-api ;; esac\n"
        "    printf 'Error response from daemon: No such object: %s\\n' \"$name\" >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  case \"$*\" in\n"
        "    *State.Running*) cat \"$FAKE_DEPLOY_ROOT/$service-running\" ;;\n"
        "    *Config.Image*) cat \"$FAKE_DEPLOY_ROOT/$service-ref\" ;;\n"
        "    *) cat \"$FAKE_DEPLOY_ROOT/$service-image\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = tag ]; then\n"
        "  printf '%s\\n' \"$3\" > \"$FAKE_DEPLOY_ROOT/tag-$4\"\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = inspect ]; then\n"
        "  ref=$5\n"
        "  case \"$*\" in\n"
        "    *Config.Labels*)\n"
        "      case \"$ref\" in\n"
        f"        *rollback*|*:{OLD_COMMIT}) printf '%s\\n' '{OLD_COMMIT}' ;;\n"
        f"        *) printf '%s\\n' '{NEW_COMMIT}' ;;\n"
        "      esac ;;\n"
        "    *)\n"
        "      case \"$ref\" in\n"
        "        *rollback*)\n"
        "          if [ ! -f \"$FAKE_DEPLOY_ROOT/tag-$ref\" ]; then\n"
        "            printf 'Error response from daemon: No such object: %s\\n' \"$ref\" >&2\n"
        "            exit 1\n"
        "          fi\n"
        "          cat \"$FAKE_DEPLOY_ROOT/tag-$ref\" ;;\n"
        f"        *) printf '%s\\n' '{NEW_IMAGE}' ;;\n"
        "      esac ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = compose ]; then\n"
        "  case \"$*\" in\n"
        "    *'build-db --atomic'*)\n"
        "      candidate=''\n"
        "      for arg in \"$@\"; do\n"
        "        case \"$arg\" in WUWATERM_DB_PATH=*) candidate=${arg#WUWATERM_DB_PATH=} ;; esac\n"
        "      done\n"
        "      candidate=$FAKE_DEPLOY_ROOT/data/${candidate#/app/data/}\n"
        "      cp \"$FAKE_DEPLOY_ROOT/data/candidate-seed.db\" \"$candidate\"\n"
        "      ;;\n"
        "    *'verify-db'*)\n"
        "      candidate=''\n"
        "      for arg in \"$@\"; do\n"
        "        case \"$arg\" in WUWATERM_DB_PATH=*) candidate=${arg#WUWATERM_DB_PATH=} ;; esac\n"
        "      done\n"
        "      candidate=$FAKE_DEPLOY_ROOT/data/${candidate#/app/data/}\n"
        "      python3 \"$FAKE_DEPLOY_ROOT/scripts/verify_db.py\" \"$candidate\"\n"
        "      ;;\n"
        "    *' stop '*)\n"
        "      printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"\n"
        "      printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/api-running\"\n"
        "      ;;\n"
        "    *' up -d '*)\n"
        # Reentry fault: fail the first rollback start once, then allow it, so
        # recovery can be proven reentrant after an interrupted restore.
        "      if [ \"${FULL_RECOVERY_FAIL_UP_ONCE:-0}\" = 1 ] && "
        "[ ! -f \"$FAKE_DEPLOY_ROOT/up-failed-once\" ]; then\n"
        "        printf '1\\n' > \"$FAKE_DEPLOY_ROOT/up-failed-once\"\n"
        "        exit 46\n"
        "      fi\n"
        "      image=\"${WUWATERM_RUNTIME_IMAGE:?}\"\n"
        "      case \"$image\" in\n"
        "        *rollback*) resolved=\"$(cat \"$FAKE_DEPLOY_ROOT/tag-$image\")\" ;;\n"
        f"        *) resolved='{NEW_IMAGE}' ;;\n"
        "      esac\n"
        "      case \"$*\" in\n"
        "        *' wuwaterm '*|*' wuwaterm')\n"
        "          printf '%s\\n' \"$resolved\" > \"$FAKE_DEPLOY_ROOT/bot-image\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/bot-ref\"\n"
        "          printf 'true\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"\n"
        "          printf '1\\n' > \"$FAKE_DEPLOY_ROOT/bot-present\"\n"
        "          ;;\n"
        "      esac\n"
        "      case \"$*\" in\n"
        "        *wuwaterm-api*)\n"
        "          printf '%s\\n' \"$resolved\" > \"$FAKE_DEPLOY_ROOT/api-image\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/api-ref\"\n"
        "          printf 'true\\n' > \"$FAKE_DEPLOY_ROOT/api-running\"\n"
        "          printf '1\\n' > \"$FAKE_DEPLOY_ROOT/api-present\"\n"
        "          ;;\n"
        "      esac\n"
        "      ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)


def _write_full_fake_python(root: Path) -> None:
    """Fake python3 with the post-promotion barrier for the SIGKILL test.

    After the REAL durable candidate promotion completes, the wrapper writes
    a barrier file and waits; the test observes the promoted database and the
    not-yet-advanced phase marker, then SIGKILLs the process group. The
    wrapper never exits on its own, so the "promoted" phase marker cannot be
    written before the kill - that is precisely the interruption window.
    """

    fake_python = root.parent / "bin" / "python3"
    import sys

    fake_python.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${FAKE_DEPLOY_ROOT:?}\"\n"
        "echo \"python3 $*\" >> \"$FAKE_DEPLOY_ROOT/actions.log\"\n"
        f"real_python={sys.executable!r}\n"
        "case \"$*\" in\n"
        "  *'full_update.py phase started'*)\n"
        "    if [ \"${FULL_CORRUPT_BACKUP:-0}\" = 1 ] && "
        "[ ! -f \"$FAKE_DEPLOY_ROOT/backup-corrupted-once\" ]; then\n"
        "      printf '1\\n' > \"$FAKE_DEPLOY_ROOT/backup-corrupted-once\"\n"
        "      for f in \"$FAKE_DEPLOY_ROOT\"/data/deployment-backups/terms.*.db; do\n"
        "        [ -e \"$f\" ] || continue\n"
        "        printf 'corrupted after preparation\\n' > \"$f\"\n"
        "      done\n"
        "    fi\n"
        "    exec \"$real_python\" \"$@\"\n"
        "    ;;\n"
        "  *'deployment_manifest.py durable-replace --source data/candidates/'*)\n"
        "    \"$real_python\" \"$@\"\n"
        "    rc=$?\n"
        "    if [ -n \"${FULL_KILL_BARRIER_FILE:-}\" ]; then\n"
        "      printf '%s' \"$$\" > \"${FULL_KILL_PID_FILE:?}\"\n"
        "      printf ready > \"$FULL_KILL_BARRIER_FILE\"\n"
        "      while [ ! -e \"${FULL_KILL_RELEASE_FILE:?}\" ]; do sleep 0.05; done\n"
        "    fi\n"
        "    exit \"$rc\"\n"
        "    ;;\n"
        "esac\n"
        "exec \"$real_python\" \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)


def _surface(root: Path, service: str, *, image: str, ref: str, running: bool,
             present: bool = True) -> None:
    (root / f"{service}-image").write_text(f"{image}\n", encoding="ascii")
    (root / f"{service}-ref").write_text(f"{ref}\n", encoding="ascii")
    (root / f"{service}-running").write_text(
        "true\n" if running else "false\n", encoding="ascii"
    )
    present_path = root / f"{service}-present"
    if present:
        present_path.write_text("1\n", encoding="ascii")
    else:
        present_path.unlink(missing_ok=True)


def _journal(
    root: Path,
    *,
    phase: str,
    old_db_sha: str | None,
    db_backup: str = "none",
    old_pointer_present: bool = True,
    source_commit: str = NEW_COMMIT,
    new_db_sha: str,
    candidate_rel: str,
    bot_id: str | None = OLD_IMAGE,
    api_id: str | None = OLD_IMAGE,
    bot_ref: str | None = OLD_REF,
    api_ref: str | None = OLD_REF,
    bot_running: bool = True,
    api_running: bool = True,
    tool_hash: str | None = None,
) -> dict:
    payload = {
        "schema_version": 2,
        "transaction_id": TXN,
        "source_commit": source_commit,
        "phase": phase,
        "old_pointer_present": old_pointer_present,
        "old_pointer": OLD_COMMIT if old_pointer_present else None,
        "old_manifest_path": f".deployments/{OLD_COMMIT}.json"
        if old_pointer_present
        else None,
        "db_backup": db_backup,
        "old_db_sha256": old_db_sha,
        "new_db_sha256": new_db_sha,
        "candidate_path": candidate_rel,
        "new_manifest_path": f".deployments/{source_commit}.json",
        "old_bot_image_id": bot_id,
        "old_bot_ref": bot_ref,
        "old_bot_running": bot_running,
        "old_api_image_id": api_id,
        "old_api_ref": api_ref,
        "old_api_running": api_running,
        "rollback_image_ref": BOT_ROLLBACK_REF,
        "rollback_api_image_ref": API_ROLLBACK_REF,
        "target_image_ref": f"wuwaterm-runtime:{source_commit}",
        "target_image_id": NEW_IMAGE,
        "target_image_digest": NEW_IMAGE,
        "target_image_revision": source_commit,
        "tool_hash": tool_hash if tool_hash is not None else _tool_hash(root),
        "updated_utc": "2026-10-17T00:00:00+00:00",
    }
    return payload


def _plant(root: Path, payload: dict) -> Path:
    path = root / JOURNAL_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _run_recover(root: Path, env: dict) -> subprocess.CompletedProcess:
    env = dict(env)
    for name in ("WUWATERM_FAIL_STEP", "FULL_KILL_BARRIER_FILE",
                 "FULL_KILL_RELEASE_FILE", "FULL_KILL_PID_FILE",
                 "FULL_CORRUPT_BACKUP"):
        env.pop(name, None)
    return subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--recover-full"],
        cwd=root, env=env, text=True, capture_output=True, timeout=60,
        check=False,
    )


def _run_mode(root: Path, env: dict, mode: list[str]) -> subprocess.CompletedProcess:
    env = dict(env)
    for name in ("FULL_KILL_BARRIER_FILE", "FULL_KILL_RELEASE_FILE",
                 "FULL_KILL_PID_FILE"):
        env.pop(name, None)
    return subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh"), *mode],
        cwd=root, env=env, text=True, capture_output=True, timeout=60,
        check=False,
    )


def _set_db(root: Path, content: bytes | None) -> None:
    path = root / "data" / "terms.db"
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)


def _actions(root: Path) -> list[str]:
    path = root / "actions.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _docker_log(root: Path) -> list[str]:
    path = root / "docker.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


@pytest.fixture()
def full_harness(tmp_path):
    root, env, old_hash, new_hash = _make_deploy_harness(tmp_path)
    _write_full_fake_docker(root)
    _write_full_fake_python(root)
    old_bytes = (root / "data" / "terms.db").read_bytes()
    new_bytes = (root / "data" / "candidate-seed.db").read_bytes()
    backup_rel = f"data/deployment-backups/terms.{TXN}.{old_hash}.db"
    backup = root / backup_rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "data" / "terms.db", backup)
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)
    # The interrupted deployment already tagged each old surface for rollback.
    (root / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    (root / f"tag-{API_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    return root, env, old_hash, new_hash, old_bytes, new_bytes, backup_rel


def _old_binding_restored(root: Path, old_hash: str) -> None:
    assert hashlib.sha256(
        (root / "data" / "terms.db").read_bytes()
    ).hexdigest() == old_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert not (root / JOURNAL_RELPATH).exists()


# --- the original kill window, with a real process-group SIGKILL ------------


def test_sigkill_after_durable_promotion_before_phase_marker(full_harness):
    """The audit's exact window: candidate DB durably promoted, marker not
    yet written, updater SIGKILLed. Recovery must restore the recorded old
    binding from the journal - no operator guessing about which backup fits.
    """

    root, env, old_hash, new_hash, _old_bytes, new_bytes, _backup_rel = (
        full_harness
    )
    barrier = root / "kill-barrier"
    release = root / "kill-release"  # never created: the kill IS the release
    pid_file = root / "kill-pid"
    env["FULL_KILL_BARRIER_FILE"] = str(barrier)
    env["FULL_KILL_RELEASE_FILE"] = str(release)
    env["FULL_KILL_PID_FILE"] = str(pid_file)

    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        for _ in range(400):
            if barrier.exists() and pid_file.exists():
                break
            if process.poll() is not None:
                pytest.fail("updater exited before the promotion barrier")
            time.sleep(0.05)
        else:
            pytest.fail("promotion barrier never reached")

        # Observed state at the kill: the live database IS the promoted
        # candidate, and the journal has NOT advanced past "armed".
        assert hashlib.sha256(
            (root / "data" / "terms.db").read_bytes()
        ).hexdigest() == new_hash
        journal = json.loads((root / JOURNAL_RELPATH).read_text(encoding="utf-8"))
        assert journal["phase"] == "armed"
        assert journal["new_db_sha256"] == new_hash

        os.killpg(process.pid, signal.SIGKILL)
        _out, _err = process.communicate(timeout=30)
        assert process.returncode == -signal.SIGKILL

        # The durable record survived the kill; the marker never advanced.
        journal_after = json.loads(
            (root / JOURNAL_RELPATH).read_text(encoding="utf-8")
        )
        assert journal_after["phase"] == "armed"
        # The marker command never ran.
        assert not any(
            "full_update.py phase promoted" in line for line in _actions(root)
        )
        assert hashlib.sha256(
            (root / "data" / "terms.db").read_bytes()
        ).hexdigest() == new_hash

        result = _run_recover(root, env)
        assert result.returncode == 0, result.stderr
        _old_binding_restored(root, old_hash)
        # Old surfaces are back, each on its recorded image id and the
        # rollback reference THIS transaction recorded.
        assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
        assert (root / "api-image").read_text(encoding="ascii").strip() == OLD_IMAGE
        assert (root / "bot-ref").read_text(encoding="ascii").strip() == journal_after[
            "rollback_image_ref"
        ]
        assert (root / "api-ref").read_text(encoding="ascii").strip() == journal_after[
            "rollback_api_image_ref"
        ]
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=30)


# --- recovery by observed phase reality -------------------------------------


def test_killed_after_stop_recovers_without_touching_database(full_harness):
    root, env, old_hash, _new_hash, old_bytes, _nb, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="armed", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha="0" * 64,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    # Reality at this phase: containers stopped, database not yet promoted.
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    # Nothing was re-copied: the untouched old database stayed in place and
    # the recorded running surfaces came back on their rollback refs.
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "bot-ref").read_text(encoding="ascii").strip() == BOT_ROLLBACK_REF


def test_killed_after_db_promote_restores_verified_backup(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    # The candidate file was consumed by the durable replace; the live
    # database is the new candidate; containers are stopped.
    _set_db(root, new_bytes)
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-ref").read_text(encoding="ascii").strip() == BOT_ROLLBACK_REF
    # The rollback start ran under this transaction's recorded rollback ref
    # (docker.log carries the image env the daemon was asked to run).
    docker_log = _docker_log(root)
    assert any(
        " up -d " in line and BOT_ROLLBACK_REF in line for line in docker_log
    )


def test_missing_database_restores_verified_backup_before_restart(full_harness):
    root, env, old_hash, _new_hash, _ob, _nb, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha="0" * 64,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    # The live database is simply gone; the recorded backup is intact.
    _set_db(root, None)
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)


def test_unknown_live_database_refuses_and_keeps_journal(full_harness):
    root, env, old_hash, _new_hash, _ob, _nb, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha="0" * 64,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    # A live database this journal cannot name: never overwritten.
    _set_db(root, b"somebody else's database")
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == b"somebody else's database"


def test_killed_after_start_quiesces_then_restores(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    # Replacement containers are up on the new image.
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert (root / "bot-ref").read_text(encoding="ascii").strip() == BOT_ROLLBACK_REF
    # Nothing may serve while the binding is restored: quiescence is issued
    # and confirmed before the database moves.
    actions = _actions(root)
    stop_at = next(
        i for i, line in enumerate(actions) if " stop wuwaterm " in line
    )
    up_at = next(i for i, line in enumerate(actions) if " up -d " in line)
    assert stop_at < up_at


def test_published_transaction_rolls_back_to_old_binding(full_harness):
    from scripts.deployment_manifest import build_manifest, write_manifest

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="published", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    # The new manifest was created and verified before the interruption.
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path=backup_rel,
            deployment_utc="2026-10-17T00:00:00Z",
        ),
    )

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)


def test_committed_binding_is_confirmed_from_observed_state(full_harness):
    from scripts.deployment_manifest import (
        build_manifest,
        publish_commit_pointer,
        write_manifest,
    )

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="committed", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    publish_commit_pointer(root / ".deploy_commit", NEW_COMMIT)
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path=backup_rel,
            deployment_utc="2026-10-17T00:00:00Z",
        ),
    )

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    # The published new binding stays exactly as it is: no stop, no restore.
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert not (root / JOURNAL_RELPATH).exists()
    assert not any(" stop " in line for line in _docker_log(root))


def test_recovery_is_reentrant_after_interrupted_rollback(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    env["FULL_RECOVERY_FAIL_UP_ONCE"] = "1"
    failed = _run_recover(root, env)
    assert failed.returncode == 1
    # The record survived the interrupted restore; the host is left quiesced
    # with the old database already back and no surface restarted.
    assert (root / JOURNAL_RELPATH).exists()
    assert hashlib.sha256(
        (root / "data" / "terms.db").read_bytes()
    ).hexdigest() == old_hash
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "false"

    # The second attempt completes from exactly the state the first left.
    result = _run_recover(root, env)
    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"


def test_first_deploy_restores_recorded_absence(full_harness):
    root, env, _old_hash, _new_hash, _ob, new_bytes, _backup_rel = full_harness
    # First deploy: no old pointer, manifest, database or containers ever.
    (root / ".deploy_commit").unlink()
    (root / ".deployments" / f"{OLD_COMMIT}.json").unlink()
    _plant(root, _journal(
        root, phase="started", old_db_sha=None, db_backup="none",
        old_pointer_present=False, new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
        bot_id=None, api_id=None, bot_ref=None, api_ref=None,
        bot_running=False, api_running=False,
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert not (root / "data" / "terms.db").exists()
    assert not (root / ".deploy_commit").exists()
    assert not (root / JOURNAL_RELPATH).exists()
    # Nothing is started on a host that had nothing running.
    assert not any(" up -d " in line for line in _docker_log(root))


def test_per_surface_images_restore_exactly(full_harness):
    """A hand-recovered host can have the surfaces on different images; the
    journal records each one and recovery must put each back on its own."""

    root, env, old_hash, _new_hash, _ob, _nb, backup_rel = full_harness
    bot_id, api_id = OLD_IMAGE, API_ALT_IMAGE
    _plant(root, _journal(
        root, phase="armed", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha="0" * 64,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
        bot_id=bot_id, api_id=api_id,
    ))
    (root / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{bot_id}\n", encoding="ascii")
    (root / f"tag-{API_ROLLBACK_REF}").write_text(f"{api_id}\n", encoding="ascii")
    _surface(root, "bot", image=bot_id, ref=OLD_REF, running=False)
    _surface(root, "api", image=api_id, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-image").read_text(encoding="ascii").strip() == bot_id
    assert (root / "api-image").read_text(encoding="ascii").strip() == api_id
    assert (root / "bot-ref").read_text(encoding="ascii").strip() == BOT_ROLLBACK_REF
    assert (root / "api-ref").read_text(encoding="ascii").strip() == API_ROLLBACK_REF


def test_previously_stopped_surface_is_not_started(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
        api_running=False,
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "false"
    api_ups = [
        line for line in _docker_log(root)
        if " up -d " in line and "wuwaterm-api" in line
    ]
    assert not api_ups, api_ups


# --- fail-closed observed-binding matrix ------------------------------------


def test_unknown_pointer_is_unresolved_and_keeps_journal(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    pointer = root / ".deploy_commit"
    pointer.chmod(0o644)
    pointer.write_text(f"{UNKNOWN_COMMIT}\n", encoding="ascii")

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not _docker_log(root), "no mutation is allowed on an unknown binding"


def test_new_pointer_with_old_services_is_unresolved_never_success(full_harness):
    """The audit's fail-closed case: pointer already NEW, manifest/DB match
    the new side, but the containers are still on the old image. Confirming
    that as "already published" would certify a mixed binding."""

    from scripts.deployment_manifest import build_manifest, write_manifest

    root, env, _old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="committed", old_db_sha=None, db_backup="none",
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    (root / ".deploy_commit").chmod(0o644)
    (root / ".deploy_commit").write_text(f"{NEW_COMMIT}\n", encoding="ascii")
    # Services still run the OLD image under the OLD ref.
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path="none",
            deployment_utc="2026-10-17T00:00:00Z",
        ),
    )

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    # The only durable record is RETAINED and the mixed binding is untouched.
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


def test_new_pointer_with_missing_services_is_unresolved(full_harness):
    from scripts.deployment_manifest import build_manifest, write_manifest

    root, env, _old_hash, _new_hash, _ob, new_bytes, _backup_rel = full_harness
    _plant(root, _journal(
        root, phase="committed", old_db_sha=None, db_backup="none",
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    (root / ".deploy_commit").chmod(0o644)
    (root / ".deploy_commit").write_text(f"{NEW_COMMIT}\n", encoding="ascii")
    # The complete NEW binding is present except the services themselves;
    # this isolates the missing-services condition from manifest/db checks.
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path="none",
            deployment_utc="2026-10-17T00:00:00Z",
        ),
    )
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False,
             present=False)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False,
             present=False)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


def test_corrupt_journal_refuses_and_keeps_record(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    path = _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    path.write_text("{ not json\n", encoding="utf-8")
    before = path.read_bytes()

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "invalid_full_journal" in result.stderr
    assert path.read_bytes() == before
    assert not _docker_log(root)


def test_symlink_journal_refuses_and_keeps_record(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    path = _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    target = root / "foreign-journal.json"
    target.write_text("{}\n", encoding="ascii")
    path.unlink()
    path.symlink_to(target)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "invalid_full_journal" in result.stderr
    assert path.is_symlink()
    assert not _docker_log(root)


@pytest.mark.parametrize(
    "mutate",
    (
        "extra_key",
        "missing_key",
        "running_is_string",
        "running_is_int",
        "absolute_candidate",
        "bad_backup_hash",
        "bogus_phase",
    ),
)
def test_journal_shape_and_type_negatives_refuse_before_mutation(
    full_harness, mutate
):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    candidate_rel = f"data/candidates/terms.{TXN}.db"
    payload = _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=candidate_rel,
    )
    if mutate == "extra_key":
        payload["surprise"] = True
    elif mutate == "missing_key":
        del payload["tool_hash"]
    elif mutate == "running_is_string":
        payload["old_bot_running"] = "yes"
    elif mutate == "running_is_int":
        payload["old_api_running"] = 1
    elif mutate == "absolute_candidate":
        payload["candidate_path"] = "/etc/passwd"
    elif mutate == "bad_backup_hash":
        payload["db_backup"] = (
            f"data/deployment-backups/terms.{TXN}.{'f' * 64}.db"
        )
    elif mutate == "bogus_phase":
        payload["phase"] = "sideways"
    path = _plant(root, payload)
    _set_db(root, new_bytes)
    before = path.read_bytes()

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "invalid_full_journal" in result.stderr
    assert path.read_bytes() == before
    assert not _docker_log(root), mutate


def test_backup_hash_mismatch_refuses_before_copy(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    # The backup was modified after the journal recorded its hash.
    (root / backup_rel).write_bytes(b"tampered backup")
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_backup_missing" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    # The unknown-content database was not overwritten and nothing restarted.
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert not any(" up -d " in line for line in _docker_log(root))


def test_missing_backup_refuses_before_any_mutation(full_harness):
    """The recorded backup is validated in the preflight, so a missing
    backup refuses BEFORE services are stopped: the host keeps serving the
    promoted binding it already has, and the record is retained."""

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    (root / backup_rel).unlink()
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_backup_missing" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert not any(
        " stop " in line or " up -d " in line for line in _docker_log(root)
    )
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"


def test_unconfined_journal_path_refuses_before_mutation(full_harness, tmp_path):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)
    # A journal-referenced path that leaves the deploy root through a
    # symlinked component must never be read or written, and the refusal
    # must land BEFORE any stop: running surfaces are left exactly as they
    # were, with the record retained.
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    shutil.copy2(root / backup_rel, outside / "terms.db")
    (root / backup_rel).unlink()
    (root / "data" / "deployment-backups").rmdir()
    (root / "data" / "deployment-backups").symlink_to(outside, target_is_directory=True)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_journal_path_unconfined" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert (outside / "terms.db").read_bytes() != b""
    assert not any(" up -d " in line for line in _docker_log(root))
    assert not any(" stop " in line for line in _docker_log(root)), (
        "refusal must land before any stop"
    )
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"


def test_tool_drift_refuses_recovery_and_keeps_journal(full_harness):
    """A journal written by DIFFERENT tool code is not recoverable by this
    code: the fingerprint covers the shell, compose file, manifest helper
    and the recovery module itself."""

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    # Plant the journal with the CURRENT tool fingerprint, then drift the
    # tool code the way an unreviewed local edit would.
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    with open(root / "scripts" / "full_update.py", "a", encoding="utf-8") as fh:
        fh.write("# local drift\n")
    _set_db(root, new_bytes)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_tool_mismatch" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


# --- admission gates ---------------------------------------------------------


def test_full_journal_refuses_new_full_deployment(full_harness):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)

    result = _run_mode(root, env, [])

    assert result.returncode == 1
    assert "refusing full deployment" in result.stderr
    assert "requires recovery" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not _docker_log(root)


def test_committed_marker_alone_does_not_admit_new_deployments(full_harness):
    """A journal whose phase says committed is still an outstanding record:
    the marker alone is not permission to admit new deploys."""

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="committed", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)

    result = _run_mode(root, env, [])

    assert result.returncode == 1
    assert "requires recovery" in result.stderr


@pytest.mark.parametrize(
    "mode", (["--runtime-only"], ["--recover-runtime"])
)
def test_full_journal_refuses_runtime_modes_before_any_change(full_harness, mode):
    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="promoted", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)

    result = _run_mode(root, env, mode)

    assert result.returncode == 1
    assert "full_recovery_required" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not _docker_log(root)


def test_ordinary_rollback_clears_journal_through_binding_verifier(
    full_harness,
):
    """The shell EXIT rollback may drop the record only via the same observed
    old-binding verification: an ordinary mid-deploy failure leaves no
    journal behind."""

    root, env, old_hash, _new_hash, _ob, new_bytes, backup_rel = full_harness
    env["WUWATERM_FAIL_STEP"] = "start"

    result = _run_mode(root, env, [])

    assert result.returncode == 97, result.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    assert hashlib.sha256(
        (root / "data" / "terms.db").read_bytes()
    ).hexdigest() == old_hash
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE


# --- preflight admissibility: refuse BEFORE any mutation ---------------------


def _started_reality(full_harness):
    """Started-phase reality: promoted new database, replacement surfaces on
    the target image, valid backup, per-surface rollback tags."""

    root, env, old_hash, new_hash, _ob, new_bytes, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="started", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    return root, env, old_hash, new_hash, new_bytes


def test_unknown_old_side_service_identity_refuses_before_mutation(full_harness):
    """A running surface this transaction cannot account for is unknown
    state: recovery must refuse BEFORE stopping or replacing it."""

    root, env, old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    _surface(root, "bot", image=UNKNOWN_IMAGE, ref="unrelated-runtime:unrelated",
             running=True)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    # Read-only inspection is not a mutation; nothing that changes state
    # (no stop, no start) and the observed bytes and flags are untouched.
    assert not any(
        " stop " in line or " up -d " in line for line in _docker_log(root)
    ), result.stdout + result.stderr
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "bot-image").read_text(encoding="ascii").strip() == UNKNOWN_IMAGE
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


def test_drifted_rollback_tag_refuses_before_any_execution(full_harness):
    """A rollback reference that no longer resolves to the recorded
    per-surface old id must be detected BEFORE it can run."""

    root, env, _old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    (root / f"tag-{BOT_ROLLBACK_REF}").write_text(
        f"{UNKNOWN_IMAGE}\n", encoding="ascii"
    )

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unresolved" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not any(" up -d " in line for line in _docker_log(root)), (
        "the drifted image must never execute"
    )
    assert not any(" stop " in line for line in _docker_log(root))
    assert (root / "bot-image").read_text(encoding="ascii").strip() == NEW_IMAGE
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


def test_mixed_partial_transition_is_admissible(full_harness):
    """Positive control for the identity preflight: a partially transitioned
    host (one surface on the target, one still on the old binding) is a
    known, recoverable state."""

    root, env, old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    _surface(root, "api", image=OLD_IMAGE, ref=OLD_REF, running=False)

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    _old_binding_restored(root, old_hash)
    assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert (root / "api-image").read_text(encoding="ascii").strip() == OLD_IMAGE


# --- trustworthy observation -------------------------------------------------


def test_inspect_daemon_failure_blocks_recovery(full_harness):
    """Inspect failures that cannot POSITIVELY prove absence must block
    recovery, never read as absence: no database removal, no journal clear."""

    root, env, _old_hash, new_hash, _ob, new_bytes, _backup_rel = full_harness
    (root / ".deploy_commit").unlink()
    (root / ".deployments" / f"{OLD_COMMIT}.json").unlink()
    _plant(root, _journal(
        root, phase="started", old_db_sha=None, db_backup="none",
        old_pointer_present=False, new_db_sha=new_hash,
        candidate_rel=f"data/candidates/terms.{TXN}.db",
        bot_id=None, api_id=None, bot_ref=None, api_ref=None,
        bot_running=False, api_running=False,
    ))
    _set_db(root, new_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    fake_docker = root.parent / "bin" / "docker"
    script = fake_docker.read_text(encoding="utf-8")
    needle = 'if [ "${1:-}" = inspect ]; then\n'
    assert script.count(needle) == 1
    script = script.replace(
        needle,
        needle + '  echo "Cannot connect to the Docker daemon" >&2\n  exit 1\n',
    )
    fake_docker.write_text(script, encoding="utf-8")

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unobservable" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert not any(" stop " in line for line in _docker_log(root))


def test_invalid_running_output_blocks_recovery(full_harness):
    """A running-state that is not exactly true/false is unobservable, not
    stopped: the refusal happens before any mutation."""

    root, env, _old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    (root / "bot-running").write_text("maybe\n", encoding="ascii")

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_recovery_unobservable" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not any(
        " stop " in line or " up -d " in line for line in _docker_log(root)
    )
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "maybe"
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


# --- path preflight before any mutation --------------------------------------


def test_old_manifest_symlink_refuses_before_stop(full_harness, tmp_path):
    """The recorded old manifest is validated with the rest of the preflight:
    a symlinked manifest refuses BEFORE services are stopped."""

    root, env, _old_hash, _new_hash, new_bytes = _started_reality(full_harness)
    outside = tmp_path / "outside-manifest.json"
    shutil.copy2(root / ".deployments" / f"{OLD_COMMIT}.json", outside)
    manifest = root / ".deployments" / f"{OLD_COMMIT}.json"
    manifest.chmod(0o644)
    manifest.unlink()
    manifest.symlink_to(outside)

    result = _run_recover(root, env)

    assert result.returncode == 1
    assert "full_journal_path_unconfined" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    assert not any(" stop " in line for line in _docker_log(root))
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "data" / "terms.db").read_bytes() == new_bytes


# --- equal old/new database preimages ----------------------------------------


def test_equal_old_new_hashes_deploy_and_confirm(full_harness):
    """A byte-identical candidate (equal old/new hashes) is a valid data
    update: the deploy promotes, confirms and clears the record."""

    root, env, old_hash, _new_hash, old_bytes, _nb, _backup_rel = full_harness
    (root / "data" / "candidate-seed.db").write_bytes(old_bytes)

    result = _run_mode(root, env, [])

    assert result.returncode == 0, result.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    assert hashlib.sha256(
        (root / "data" / "terms.db").read_bytes()
    ).hexdigest() == old_hash


def test_equal_old_new_hashes_recovery_confirms_new(full_harness):
    from scripts.deployment_manifest import (
        build_manifest,
        publish_commit_pointer,
        write_manifest,
    )

    root, env, old_hash, _new_hash, old_bytes, _nb, backup_rel = full_harness
    _plant(root, _journal(
        root, phase="committed", old_db_sha=old_hash, db_backup=backup_rel,
        new_db_sha=old_hash,  # equal preimages are valid
        candidate_rel=f"data/candidates/terms.{TXN}.db",
    ))
    _set_db(root, old_bytes)
    _surface(root, "bot", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    _surface(root, "api", image=NEW_IMAGE,
             ref=f"wuwaterm-runtime:{NEW_COMMIT}", running=True)
    publish_commit_pointer(root / ".deploy_commit", NEW_COMMIT)
    write_manifest(
        root / ".deployments" / f"{NEW_COMMIT}.json",
        build_manifest(
            source_commit=NEW_COMMIT,
            image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
            image_id=NEW_IMAGE,
            image_digest=NEW_IMAGE,
            image_revision=NEW_COMMIT,
            db_path=root / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path=backup_rel,
            deployment_utc="2026-10-17T00:00:00Z",
        ),
    )

    result = _run_recover(root, env)

    assert result.returncode == 0, result.stderr
    assert not (root / JOURNAL_RELPATH).exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    assert (root / "data" / "terms.db").read_bytes() == old_bytes


# --- shell EXIT rollback uses the shared verified restoration ----------------


def test_rollback_with_corrupt_backup_leaves_live_database(full_harness):
    """R4: a backup corrupted after preparation must not be promoted over
    the known live database. The EXIT trap's verified restoration refuses
    before replacing; the record is kept for --recover-full."""

    root, env, old_hash, _new_hash, _ob, new_bytes, _backup_rel = full_harness
    env["WUWATERM_FAIL_STEP"] = "start"
    env["FULL_CORRUPT_BACKUP"] = "1"

    result = _run_mode(root, env, [])

    assert result.returncode == 97, result.stderr
    assert "verified database restoration failed" in result.stderr
    assert "old database content was not restored" in result.stderr
    assert (root / JOURNAL_RELPATH).exists()
    # The promoted (new) database is untouched: unverified bytes never
    # replaced it.
    assert (root / "data" / "terms.db").read_bytes() == new_bytes
    assert old_hash != hashlib.sha256(new_bytes).hexdigest()
    # Services were quiesced by the trap and no rollback start happened
    # (the deploy's own new-image `up` earlier in the log is expected).
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "false"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "false"
    assert not any(
        " up -d " in line and "rollback" in line for line in _docker_log(root)
    )
