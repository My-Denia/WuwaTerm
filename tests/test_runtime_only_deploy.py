"""Focused transaction tests for the explicit runtime-only deployment path."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_deploy_scripts import (  # noqa: E402
    NEW_COMMIT,
    OLD_COMMIT,
    _make_deploy_harness,
)
from scripts.deployment_manifest import (
    build_manifest,
    publish_commit_pointer,
    write_manifest,
)


OLD_RUNTIME_IMAGE_ID = "sha256:" + "1" * 64
NEW_RUNTIME_IMAGE_ID = "sha256:" + "2" * 64
ALT_COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
ALT_RUNTIME_IMAGE_ID = "sha256:" + "a" * 64
NEXT_RUNTIME_IMAGE_ID = "sha256:" + "b" * 64


def _snapshot_file(path: Path) -> tuple[bool, bytes | None, tuple[int, int, int, int] | None]:
    if not path.exists():
        return False, None, None
    stat = path.stat()
    return (
        True,
        path.read_bytes(),
        (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
    )


def _snapshot_database(root: Path) -> dict[str, object]:
    return {
        "terms": _snapshot_file(root / "data" / "terms.db"),
        "wal": _snapshot_file(root / "data" / "terms.db-wal"),
        "shm": _snapshot_file(root / "data" / "terms.db-shm"),
        "journal": _snapshot_file(root / "data" / "terms.db-journal"),
    }


def _write_runtime_fake_docker(root: Path) -> None:
    fake_docker = root.parent / "bin" / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${FAKE_DEPLOY_ROOT:?}\"\n"
        f"target_commit=\"${{RUNTIME_FAKE_HEAD_COMMIT:-{NEW_COMMIT}}}\"\n"
        "printf '%s|%s\\n' \"${WUWATERM_RUNTIME_IMAGE:-unset}\" \"$*\" "
        ">> \"$FAKE_DEPLOY_ROOT/docker.log\"\n"
        "printf 'docker %s\\n' \"$*\" >> \"$FAKE_DEPLOY_ROOT/actions.log\"\n"
        "service=\n"
        "case \"$*\" in\n"
        "  *wuwaterm-api*) service=api ;;\n"
        "  *wuwaterm-bot*|*' wuwaterm '*) service=bot ;;\n"
        "esac\n"
        "if [ \"${1:-}\" = inspect ]; then\n"
        "  case \"$*\" in\n"
        "    *State.Running*) cat \"$FAKE_DEPLOY_ROOT/$service-running\" ;;\n"
        "    *Config.Image*) cat \"$FAKE_DEPLOY_ROOT/$service-ref\" ;;\n"
        "    *'.Mounts'*) printf '[{\"Type\":\"bind\",\"Source\":\"%s/data\",\"Destination\":\"/app/data\",\"Mode\":\"ro\",\"RW\":false}]\\n' \"$FAKE_DEPLOY_ROOT\" ;;\n"
        "    *) cat \"$FAKE_DEPLOY_ROOT/$service-image\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = inspect ]; then\n"
        "  ref=\"$5\"\n"
        "  case \"$ref\" in *rollback*) if [ ! -f \"$FAKE_DEPLOY_ROOT/tag-$ref\" ]; then exit 46; fi ;; esac\n"
        f"  case \"$ref\" in *rollback*|*old*) image_id='{OLD_RUNTIME_IMAGE_ID}'; revision='{OLD_COMMIT}' ;; *{ALT_COMMIT}*) image_id='{ALT_RUNTIME_IMAGE_ID}'; revision='{ALT_COMMIT}' ;; *{NEXT_COMMIT}*) image_id='{NEXT_RUNTIME_IMAGE_ID}'; revision='{NEXT_COMMIT}' ;; *) image_id='{NEW_RUNTIME_IMAGE_ID}'; revision=\"$target_commit\" ;; esac\n"
        "  if [ -f \"$FAKE_DEPLOY_ROOT/tag-$ref\" ]; then\n"
        "    image_id=\"$(cat \"$FAKE_DEPLOY_ROOT/tag-$ref\")\"\n"
        f"    case \"$image_id\" in '{OLD_RUNTIME_IMAGE_ID}') revision='{OLD_COMMIT}' ;; '{ALT_RUNTIME_IMAGE_ID}') revision='{ALT_COMMIT}' ;; '{NEXT_RUNTIME_IMAGE_ID}') revision='{NEXT_COMMIT}' ;; *) revision='{NEW_COMMIT}' ;; esac\n"
        "  fi\n"
        "  if [ \"${RUNTIME_FAKE_TARGET_IMAGE_MISSING:-0}\" = 1 ] && [ -z \"$(cat \"$FAKE_DEPLOY_ROOT/target-image-created\" 2>/dev/null || true)\" ]; then\n"
        f"    case \"$ref\" in *{NEW_COMMIT}*) exit 46 ;; esac\n"
        "  fi\n"
        "  if [ \"${RUNTIME_FAKE_ROLLBACK_ALIAS_MISSING:-0}\" = 1 ] && [ -z \"$(cat \"$FAKE_DEPLOY_ROOT/rollback-tag-created\" 2>/dev/null || true)\" ]; then\n"
        "    case \"$ref\" in *rollback*) exit 46 ;; esac\n"
        "  fi\n"
        "  case \"$*\" in\n"
        "    *Config.Labels*) if [ \"${RUNTIME_FAKE_LABEL_FAIL:-0}\" = 1 ]; then case \"$ref\" in *\"$target_commit\") exit 54 ;; esac; fi; printf '%s\\n' \"$revision\" ;;\n"
        "    *) printf '%s\\n' \"$image_id\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = tag ]; then\n"
        "  if [ \"${RUNTIME_FAKE_TAG_FAIL:-0}\" = 1 ]; then exit 53; fi\n"
        "  if [ \"${RUNTIME_FAKE_BLOCK_PHASE:-}\" = tag ]; then\n"
        "    if [ -n \"${RUNTIME_FAKE_CHILD_PID_FILE:-}\" ]; then printf '%s' \"$$\" > \"$RUNTIME_FAKE_CHILD_PID_FILE\"; fi\n"
        "    printf ready > \"${RUNTIME_FAKE_BLOCK_FILE:?}\"\n"
        "    while [ ! -e \"${RUNTIME_FAKE_RELEASE_FILE:?}\" ]; do sleep 0.05; done\n"
        "  fi\n"
        "  printf '%s\\n' \"$3\" > \"$FAKE_DEPLOY_ROOT/tag-$4\"\n"
        "  printf done > \"$FAKE_DEPLOY_ROOT/rollback-tag-created\"\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = compose ]; then\n"
        "  case \"$*\" in\n"
        "    *' stop '*wuwaterm*)\n"
        "      if [ \"${RUNTIME_FAKE_BLOCK_PHASE:-}\" = stop ]; then\n"
        "        printf ready > \"${RUNTIME_FAKE_BLOCK_FILE:?}\"\n"
        "        while [ ! -e \"${RUNTIME_FAKE_RELEASE_FILE:?}\" ]; do sleep 0.05; done\n"
        "        if [ -n \"${RUNTIME_FAKE_DONE_FILE:-}\" ]; then printf done > \"$RUNTIME_FAKE_DONE_FILE\"; fi\n"
        "      fi\n"
        "      mode=\"${RUNTIME_FAKE_STOP:-all}\"\n"
        "      if [ \"${RUNTIME_FAKE_ROLLBACK_STOP:-0}\" = 1 ] && [ -e \"$FAKE_DEPLOY_ROOT/new-started\" ]; then exit 47; fi\n"
        "      case \"$mode\" in\n"
        "        bot) printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"; exit 42 ;;\n"
        "        api) printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"; printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/api-running\"; exit 42 ;;\n"
        "      esac\n"
        "      printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"\n"
        "      printf 'false\\n' > \"$FAKE_DEPLOY_ROOT/api-running\"\n"
        "      ;;\n"
        "    *' build '*wuwaterm*)\n"
        "      if [ \"${RUNTIME_FAKE_BUILD_CHANGE_DB:-0}\" = 1 ]; then printf external-build-change >> \"$FAKE_DEPLOY_ROOT/data/terms.db\"; fi\n"
        "      if [ \"${RUNTIME_FAKE_BUILD_FAIL:-0}\" = 1 ]; then exit 51; fi\n"
        "      printf 'built\\n' > \"$FAKE_DEPLOY_ROOT/runtime-built\"\n"
        "      printf done > \"$FAKE_DEPLOY_ROOT/target-image-created\"\n"
        "      ;;\n"
        "    *' up -d '*wuwaterm*)\n"
        "      if [ \"${RUNTIME_FAKE_BLOCK_PHASE:-}\" = start ]; then\n"
        "        if [ -n \"${RUNTIME_FAKE_CHILD_PID_FILE:-}\" ]; then printf '%s' \"$$\" > \"$RUNTIME_FAKE_CHILD_PID_FILE\"; fi\n"
        "        printf ready > \"${RUNTIME_FAKE_BLOCK_FILE:?}\"\n"
        "        while [ ! -e \"${RUNTIME_FAKE_RELEASE_FILE:?}\" ]; do sleep 0.05; done\n"
        "        if [ -n \"${RUNTIME_FAKE_DONE_FILE:-}\" ]; then printf done > \"$RUNTIME_FAKE_DONE_FILE\"; fi\n"
        "      fi\n"
        "      image=\"${WUWATERM_RUNTIME_IMAGE:-unset}\"\n"
        "      case \"$image\" in *rollback*) ;; *) printf started > \"$FAKE_DEPLOY_ROOT/new-started\" ;; esac\n"
        "      case \"$image\" in\n"
        "        *rollback*)\n"
        "          rollback_id=\"$(cat \"$FAKE_DEPLOY_ROOT/tag-$image\")\"\n"
        "          printf '%s\\n' \"$rollback_id\" > \"$FAKE_DEPLOY_ROOT/bot-image\"\n"
        "          printf '%s\\n' \"$rollback_id\" > \"$FAKE_DEPLOY_ROOT/api-image\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/bot-ref\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/api-ref\"\n"
        "          ;;\n"
        "        *)\n"
        f"          case \"$image\" in *{ALT_COMMIT}*) image_id='{ALT_RUNTIME_IMAGE_ID}' ;; *{NEXT_COMMIT}*) image_id='{NEXT_RUNTIME_IMAGE_ID}' ;; *) image_id='{NEW_RUNTIME_IMAGE_ID}' ;; esac\n"
        "          printf '%s\\n' \"$image_id\" > \"$FAKE_DEPLOY_ROOT/bot-image\"\n"
        "          printf '%s\\n' \"$image_id\" > \"$FAKE_DEPLOY_ROOT/api-image\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/bot-ref\"\n"
        "          printf '%s\\n' \"$image\" > \"$FAKE_DEPLOY_ROOT/api-ref\"\n"
        "          ;;\n"
        "      esac\n"
        "      printf 'true\\n' > \"$FAKE_DEPLOY_ROOT/bot-running\"\n"
        "      case \"$image\" in *rollback*) if [ \"${RUNTIME_FAKE_ROLLBACK_START:-}\" = bot ]; then exit 46; fi ;; *) if [ \"${RUNTIME_FAKE_START:-}\" = bot ]; then exit 43; fi ;; esac\n"
        "      case \"$image\" in *rollback*) if [ \"${RUNTIME_FAKE_ROLLBACK_START:-}\" = api ]; then exit 46; fi ;; esac\n"
        "      printf 'true\\n' > \"$FAKE_DEPLOY_ROOT/api-running\"\n"
        "      case \"$image\" in *rollback*) ;; *) if [ \"${RUNTIME_FAKE_START:-}\" = api ]; then exit 43; fi ;; esac\n"
        "      ;;\n"
        "    *' publish-pointer '*wuwaterm*)\n"
        "      if [ \"${RUNTIME_FAKE_POINTER_FAILURE:-0}\" = 1 ]; then exit 45; fi\n"
        "      ;;\n"
        "    *' exec -T '*wuwaterm-api*)\n"
        "      if [ \"${RUNTIME_FAKE_READINESS_FAIL:-0}\" = 1 ]; then exit 44; fi\n"
        "      if [ \"${RUNTIME_FAKE_ROLLBACK_READINESS_FAIL:-0}\" = 1 ] && grep -q rollback \"$FAKE_DEPLOY_ROOT/bot-ref\"; then exit 44; fi\n"
        "      ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    for service in ("bot", "api"):
        (root / f"{service}-image").write_text(
            f"{OLD_RUNTIME_IMAGE_ID}\n", encoding="ascii"
        )
        (root / f"{service}-ref").write_text(
            "wuwaterm-runtime:old\n", encoding="ascii"
        )
        (root / f"{service}-running").write_text("true\n", encoding="ascii")


@pytest.fixture()
def runtime_harness(tmp_path):
    root, env, old_hash, new_hash = _make_deploy_harness(tmp_path)
    _write_runtime_fake_docker(root)
    env["FAKE_DEPLOY_ROOT"] = str(root)
    env["PYTHONPATH"] = ""
    fake_git = root.parent / "bin" / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"$*\" in\n"
        "  'fetch --quiet origin main:refs/remotes/origin/main')\n"
        "    if [ \"${FAKE_GIT_REMOTE_UNREACHABLE:-0}\" = 1 ]; then exit 88; fi\n"
        "    exit 0 ;;\n"
        "  'status --porcelain --untracked-files=all')\n"
        "    if [ \"${FAKE_GIT_DIRTY:-0}\" = 1 ]; then printf dirty; fi\n"
        "    exit 0 ;;\n"
        f"  'rev-parse HEAD') printf '%s\\n' \"${{RUNTIME_FAKE_HEAD_COMMIT:-{NEW_COMMIT}}}\"; exit 0 ;;\n"
        f"  'rev-parse refs/remotes/origin/main')\n"
        f"    if [ \"${{FAKE_GIT_HEAD_MISMATCH:-0}}\" = 1 ]; then printf '%s\\n' '{OLD_COMMIT}'; else printf '%s\\n' \"${{RUNTIME_FAKE_REMOTE_COMMIT:-{NEW_COMMIT}}}\"; fi\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    old_manifest_path = root / ".deployments" / f"{OLD_COMMIT}.json"
    old_manifest_path.chmod(0o600)
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_manifest["image"]["id"] = OLD_RUNTIME_IMAGE_ID
    old_manifest["image"]["digest"] = OLD_RUNTIME_IMAGE_ID
    old_manifest_path.write_text(
        json.dumps(old_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    old_manifest_path.chmod(0o444)
    (root / "state-api").mkdir()
    return root, env, old_hash, new_hash


def _run_runtime(root: Path, env: dict[str, str], *, recover: bool = False):
    mode = "--recover-runtime" if recover else "--runtime-only"
    return subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh"), mode],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _run_full(root: Path, env: dict[str, str]):
    return subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("different_root", [False, True])
def test_runtime_lock_is_bound_to_current_deployment_root(tmp_path, monkeypatch, different_root):
    import fcntl
    import scripts.runtime_update as runtime_update

    root = tmp_path / "current"
    other = tmp_path / "other"
    monkeypatch.setattr(runtime_update, "ROOT", root)
    lock_target = (other if different_root else root) / ".deployments" / ".deployment.lock"
    monkeypatch.setattr(os, "readlink", lambda _path: str(lock_target))
    monkeypatch.setattr(fcntl, "flock", lambda *_args: None)
    if different_root:
        with pytest.raises(runtime_update.RuntimeUpdateError, match="deployment_lock_missing"):
            runtime_update._require_held_lock()
    else:
        runtime_update._require_held_lock()


@pytest.mark.parametrize("lock_mode", ["available", "inherited", "contended"])
def test_runtime_helper_acquires_real_lock_before_admission(tmp_path, lock_mode):
    import fcntl

    root = tmp_path / "root"
    lock = root / ".deployments" / ".deployment.lock"
    lock.parent.mkdir(parents=True)
    code = r'''
import os, pathlib, sys
import scripts.runtime_update as runtime
root=pathlib.Path(sys.argv[1]); inherited=int(sys.argv[2])
runtime.ROOT=root
fd=inherited if inherited>=0 else os.open(root/'.deployments/.deployment.lock',os.O_RDWR)
if fd!=9: os.dup2(fd,9)
runtime._new_deployment=lambda: (print('admitted') or 0)
raise SystemExit(runtime.main(['--runtime-only']))
'''
    with lock.open("a+") as held:
        if lock_mode != "available":
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited = held.fileno() if lock_mode == "inherited" else -1
        result = subprocess.run(
            [sys.executable, "-c", code, str(root), str(inherited)],
            pass_fds=(held.fileno(),) if inherited >= 0 else (),
            capture_output=True, text=True, timeout=10, check=False,
        )
    if lock_mode == "contended":
        assert result.returncode != 0
        assert "admitted" not in result.stdout
        assert "deployment_lock" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "admitted"


@pytest.mark.parametrize("lock_kind", ["current", "foreign", "replaced"])
def test_runtime_entry_validates_and_preserves_inherited_lock(runtime_harness, lock_kind):
    root, env, _old_hash, _new_hash = runtime_harness
    before = _snapshot_database(root)
    code = r'''
import fcntl, os, pathlib, subprocess, sys
root=pathlib.Path(sys.argv[1]); kind=sys.argv[2]
lock=root/'.deployments/.deployment.lock'
if kind=='foreign':
    lock=root.parent/'foreign/.deployments/.deployment.lock'
    lock.parent.mkdir(parents=True)
fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_APPEND,0o600)
if fd!=9:
    os.dup2(fd,9);os.close(fd)
fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)
if kind=='replaced':
    lock.unlink();lock.touch()
try:
    result=subprocess.run(['sh',str(root/'deploy/vps-update.sh'),'--runtime-only'],
                          pass_fds=(9,),capture_output=True,text=True,timeout=30)
    print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
    raise SystemExit(result.returncode)
finally: os.close(9)
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), lock_kind],
        env=env, capture_output=True, text=True, timeout=40, check=False,
    )
    assert _snapshot_database(root) == before
    if lock_kind == "current":
        assert result.returncode == 0, result.stderr
        assert (root / ".deploy_commit").read_text().strip() == NEW_COMMIT
    else:
        assert result.returncode != 0
        assert not (root / "docker.log").exists()
        assert (root / ".deploy_commit").read_text().strip() == OLD_COMMIT


def test_source_handoff_competitor_cannot_rewrite_checkout(runtime_harness):
    import fcntl

    root, env, _old_hash, _new_hash = runtime_harness
    marker = root / "checkout-marker"
    marker.write_text("old")
    lock = root / ".deployments/.deployment.lock"
    with lock.open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["sh", "-c", 'exec 9>>.deployments/.deployment.lock; flock -n 9 || exit 75; '
             'printf changed > checkout-marker; exec sh deploy/vps-update.sh --runtime-only'],
            cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 75
        assert marker.read_text() == "old"
        assert not (root / "docker.log").exists()


def test_runtime_execute_runs_a_real_lightweight_child(tmp_path):
    import scripts.runtime_update as runtime_update

    lock_fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    saved_fd = None
    try:
        try:
            saved_fd = os.dup(9)
        except OSError:
            saved_fd = None
        if lock_fd != 9:
            os.dup2(lock_fd, 9)
        returncode, stdout, stderr = runtime_update._execute(
            [sys.executable, "-c", "print('runtime execute ok')"], timeout=5
        )
    finally:
        if lock_fd != 9:
            if saved_fd is None:
                os.close(9)
            else:
                os.dup2(saved_fd, 9)
        if saved_fd is not None:
            os.close(saved_fd)
        os.close(lock_fd)
    assert returncode == 0
    assert stdout.strip() == "runtime execute ok"
    assert stderr == ""


@pytest.mark.parametrize("journal_state", ["active", "unresolved"])
def test_full_mode_refuses_unresolved_runtime_journal_before_docker(
    runtime_harness, journal_state
):
    root, env, old_hash, _new_hash = runtime_harness
    journal = root / ".deployments" / ".runtime-transaction.json"
    journal.write_text(
        json.dumps({"status": journal_state}) + "\n", encoding="ascii"
    )
    before = _snapshot_database(root)

    result = _run_full(root, env)

    assert result.returncode != 0
    assert "refusing full deployment" in result.stderr
    assert _snapshot_database(root) == before
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert not (root / "docker.log").exists()


@pytest.mark.parametrize("journal_shape", ["malformed", "symlink"])
def test_full_mode_refuses_untrusted_runtime_journal_before_docker(
    runtime_harness, journal_shape
):
    root, env, _old_hash, _new_hash = runtime_harness
    journal = root / ".deployments" / ".runtime-transaction.json"
    if journal_shape == "malformed":
        journal.write_text("[]\n", encoding="ascii")
    else:
        target = root / "foreign-journal.json"
        target.write_text("{}\n", encoding="ascii")
        journal.symlink_to(target)
    before = _snapshot_database(root)

    result = _run_full(root, env)

    assert result.returncode != 0
    assert "refusing full deployment" in result.stderr
    assert _snapshot_database(root) == before
    assert not (root / "docker.log").exists()


def test_deployment_lock_symlink_cannot_truncate_database(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    database = root / "data" / "terms.db"
    before = _snapshot_database(root)
    (root / ".deployments" / ".deployment.lock").symlink_to(database)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert not (root / "docker.log").exists()


def test_deployment_metadata_directory_symlink_is_rejected_before_lock_open(
    runtime_harness,
):
    root, env, _old_hash, _new_hash = runtime_harness
    real_metadata = root / "real-deployments"
    (root / ".deployments").rename(real_metadata)
    external_metadata = root / "external-deployments"
    external_metadata.mkdir()
    (root / ".deployments").symlink_to(external_metadata, target_is_directory=True)
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert not (root / "docker.log").exists()
    assert not (external_metadata / ".deployment.lock").exists()


def test_runtime_only_success_has_no_data_commands_or_db_writes(runtime_harness):
    root, env, old_hash, _new_hash = runtime_harness
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert _snapshot_database(root) == before
    actions = (root / "actions.log").read_text(encoding="utf-8")
    for forbidden in ("refresh-data", "build-db", "verify-db", "durable-replace", "terms.db.backup"):
        assert forbidden not in actions
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    manifest = json.loads(
        (root / ".deployments" / f"{NEW_COMMIT}.json").read_text(encoding="utf-8")
    )
    assert manifest["backup_path"] == "none"
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"


def test_runtime_only_builds_cached_runtime_target_when_missing(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_TARGET_IMAGE_MISSING"] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot_database(root) == before
    actions = (root / "actions.log").read_text(encoding="utf-8")
    assert " build wuwaterm" in actions
    assert "wuwaterm-builder" not in actions
    assert (root / "target-image-created").exists()


def test_review_unmanifested_target_tag_is_rebuilt(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    before = _snapshot_database(root)
    # The fake daemon reports an existing tag with the expected revision label,
    # but there is deliberately no immutable manifest binding it to a build.
    result = _run_runtime(root, env)
    assert result.returncode == 0, result.stderr
    assert " build wuwaterm" in (root / "actions.log").read_text()
    assert _snapshot_database(root) == before


@pytest.mark.parametrize("failure", ["TAG", "BUILD", "LABEL"])
def test_review_pre_activation_failure_preserves_old_services_and_retries(runtime_harness, failure):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_TARGET_IMAGE_MISSING"] = "1"
    env[f"RUNTIME_FAKE_{failure}_FAIL"] = "1"
    before = _snapshot_database(root)
    serving = {p: _snapshot_file(root / p) for p in (
        "bot-image", "bot-ref", "bot-running", "api-image", "api-ref", "api-running")}
    failed = _run_runtime(root, env)
    assert failed.returncode != 0
    actions = (root / "actions.log").read_text()
    assert " stop wuwaterm" not in actions
    assert " up -d " not in actions
    assert {p: _snapshot_file(root / p) for p in serving} == serving
    assert _snapshot_database(root) == before
    journal = json.loads((root / ".deployments/.runtime-transaction.json").read_text())
    assert journal["status"] == "rolled_back"
    env.pop(f"RUNTIME_FAKE_{failure}_FAIL")
    retried = _run_runtime(root, env)
    assert retried.returncode == 0, retried.stderr
    assert _snapshot_database(root) == before


def test_review_pre_activation_database_drift_still_quiesces(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env.update(RUNTIME_FAKE_TARGET_IMAGE_MISSING="1", RUNTIME_FAKE_BUILD_FAIL="1",
               RUNTIME_FAKE_BUILD_CHANGE_DB="1")
    before = (root / "data/terms.db").read_bytes()
    result = _run_runtime(root, env)
    assert result.returncode != 0
    assert (root / "data/terms.db").read_bytes() == before + b"external-build-change"
    assert (root / "bot-running").read_text().strip() == "false"
    assert (root / "api-running").read_text().strip() == "false"
    journal = json.loads((root / ".deployments/.runtime-transaction.json").read_text())
    assert journal["status"] == "unresolved"


def test_review_closed_rollback_cannot_stop_later_full_deployment(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_START"] = "api"
    assert _run_runtime(root, env).returncode != 0
    env.pop("RUNTIME_FAKE_START")
    journal_path = root / ".deployments/.runtime-transaction.json"
    assert json.loads(journal_path.read_text())["status"] == "rolled_back"
    # Simulate a subsequent authorized full deployment at the same source SHA.
    with sqlite3.connect(root / "data/terms.db") as connection:
        connection.execute("UPDATE terms SET priority = priority + 1")
    manifest = build_manifest(
        source_commit=NEW_COMMIT, image_ref=f"wuwaterm-runtime:{NEW_COMMIT}",
        image_id=NEW_RUNTIME_IMAGE_ID, image_digest=NEW_RUNTIME_IMAGE_ID,
        image_revision=NEW_COMMIT, db_path=root / "data/terms.db",
        db_display_path="data/terms.db", backup_path="full-deployment-fixture",
        deployment_utc="2026-01-01T00:00:00Z",
    )
    write_manifest(root / ".deployments" / f"{NEW_COMMIT}.json", manifest)
    publish_commit_pointer(root / ".deploy_commit", NEW_COMMIT)
    for service in ("bot", "api"):
        (root / f"{service}-image").write_text(NEW_RUNTIME_IMAGE_ID + "\n")
        (root / f"{service}-ref").write_text(f"wuwaterm-runtime:{NEW_COMMIT}\n")
        (root / f"{service}-running").write_text("true\n")
    db_before = _snapshot_database(root)
    journal_before = journal_path.read_bytes()
    log_before = (root / "docker.log").read_bytes()
    result = _run_runtime(root, env, recover=True)
    assert result.returncode != 0
    assert "no_recovery_needed" in result.stderr
    assert (root / "docker.log").read_bytes() == log_before
    assert journal_path.read_bytes() == journal_before
    assert _snapshot_database(root) == db_before
    assert (root / "bot-running").read_text().strip() == "true"
    assert (root / "api-running").read_text().strip() == "true"


@pytest.mark.parametrize("entry", ["snapshot", "shell"])
def test_review_database_parent_symlink_is_rejected_before_open(runtime_harness, monkeypatch, entry):
    import scripts.runtime_update as runtime_update

    root, env, _old_hash, _new_hash = runtime_harness
    outside = root.parent / "outside-data"
    (root / "data").rename(outside)
    (root / "data").symlink_to(outside, target_is_directory=True)
    before = _snapshot_file(outside / "terms.db")
    if entry == "snapshot":
        monkeypatch.setattr(runtime_update, "ROOT", root)
        with pytest.raises(runtime_update.RuntimeUpdateError):
            runtime_update._database_snapshot()
    else:
        result = _run_runtime(root, env)
        assert result.returncode != 0
        assert not (root / "docker.log").exists()
    assert _snapshot_file(outside / "terms.db") == before


def test_runtime_only_rejects_extra_arguments_before_mutation(runtime_harness):
    root, env, old_hash, _new_hash = runtime_harness
    before = _snapshot_database(root)

    result = subprocess.run(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only", "extra"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "unknown" in result.stderr or "argument" in result.stderr
    assert _snapshot_database(root) == before
    assert not (root / "docker.log").exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash


def test_runtime_only_rejects_database_sidecar_before_docker(runtime_harness):
    root, env, old_hash, _new_hash = runtime_harness
    sidecar = root / "data" / "terms.db-wal"
    sidecar.write_bytes(b"sqlite sidecar fixture")

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert "database_sidecar" in result.stderr
    assert sidecar.read_bytes() == b"sqlite sidecar fixture"
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert not (root / "docker.log").exists()


def test_runtime_only_rejects_dangling_journal_before_docker(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    before = _snapshot_database(root)
    journal = root / ".deployments" / ".runtime-transaction.json"
    journal.symlink_to(root / "uncreated-journal.json")

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert "invalid_journal" in result.stderr
    assert journal.is_symlink()
    assert _snapshot_database(root) == before
    assert not (root / "docker.log").exists()


@pytest.mark.parametrize("failure_env", ["FAKE_GIT_DIRTY", "FAKE_GIT_HEAD_MISMATCH"])
def test_runtime_only_rejects_untrusted_source_before_docker(runtime_harness, failure_env):
    root, env, old_hash, _new_hash = runtime_harness
    env[failure_env] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert hashlib.sha256((root / "data" / "terms.db").read_bytes()).hexdigest() == old_hash
    assert not (root / "docker.log").exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"


def test_recovery_rejects_malformed_journal_without_docker(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    journal = root / ".deployments" / ".runtime-transaction.json"
    journal.write_text('{"mode":"runtime-only"}\n', encoding="ascii")

    result = _run_runtime(root, env, recover=True)

    assert result.returncode != 0
    assert "invalid_journal" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (root / "docker.log").exists()


def test_runtime_only_same_commit_retry_is_idempotent(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness

    first = _run_runtime(root, env)
    assert first.returncode == 0, first.stderr
    before_actions = (root / "actions.log").read_text(encoding="utf-8")
    before_db = _snapshot_database(root)

    second = _run_runtime(root, env)

    assert second.returncode == 0, second.stdout + second.stderr
    assert _snapshot_database(root) == before_db
    after_actions = (root / "actions.log").read_text(encoding="utf-8")
    assert after_actions.count(" stop ") == before_actions.count(" stop ")
    assert after_actions.count(" build ") == before_actions.count(" build ")
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"


@pytest.mark.parametrize("stop_mode", ["bot", "api"])
def test_runtime_only_partial_stop_rolls_back_without_touching_db(runtime_harness, stop_mode):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_STOP"] = stop_mode
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (
        (root / "bot-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert (
        (root / "api-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert "durable-replace" not in (root / "actions.log").read_text(encoding="utf-8")


def test_runtime_only_partial_start_rolls_back_both_services(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_START"] = "api"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (
        (root / "bot-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert (
        (root / "api-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "true"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "true"


def test_runtime_only_readiness_failure_rolls_back_without_db_change(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_READINESS_FAIL"] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / "bot-image").read_text(encoding="ascii").strip() == OLD_RUNTIME_IMAGE_ID
    assert (root / "api-image").read_text(encoding="ascii").strip() == OLD_RUNTIME_IMAGE_ID


def test_runtime_only_rollback_partial_start_quiesces_both_and_stays_unresolved(
    runtime_harness,
):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_READINESS_FAIL"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_START"] = "api"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    journal = json.loads(
        (root / ".deployments" / ".runtime-transaction.json").read_text(
            encoding="ascii"
        )
    )
    assert journal["status"] == "unresolved"
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "false"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "false"
    actions = (root / "actions.log").read_text(encoding="utf-8").splitlines()
    assert sum(" stop wuwaterm wuwaterm-api" in line for line in actions) >= 2


def test_runtime_only_rollback_stop_failure_attempts_final_quiesce(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_READINESS_FAIL"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_STOP"] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    journal = json.loads(
        (root / ".deployments" / ".runtime-transaction.json").read_text(
            encoding="ascii"
        )
    )
    assert journal["status"] == "unresolved"
    actions = (root / "actions.log").read_text(encoding="utf-8").splitlines()
    assert sum(" stop wuwaterm wuwaterm-api" in line for line in actions) >= 3


def test_runtime_only_old_rollback_health_failure_stays_unresolved(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_READINESS_FAIL"] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    journal = json.loads(
        (root / ".deployments" / ".runtime-transaction.json").read_text(
            encoding="ascii"
        )
    )
    assert journal["status"] == "unresolved"
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "false"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "false"


def test_runtime_only_pointer_failure_restores_old_binding(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"
    before = _snapshot_database(root)

    result = _run_runtime(root, env)

    assert result.returncode != 0
    assert _snapshot_database(root) == before
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    # The immutable target manifest is retained for explicit recovery even
    # though pointer publication failed.
    assert (root / ".deployments" / f"{NEW_COMMIT}.json").exists()


def test_runtime_only_sigterm_leaves_recoverable_journal(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_BLOCK_PHASE"] = "start"
    env["RUNTIME_FAKE_BLOCK_FILE"] = str(root / "start-ready")
    env["RUNTIME_FAKE_RELEASE_FILE"] = str(root / "start-release")
    before = _snapshot_database(root)
    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "start-ready").exists():
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                pytest.fail("runtime-only helper did not reach start barrier")
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGTERM)
        (root / "start-release").write_text("release\n", encoding="ascii")
        returncode = process.wait(timeout=20)
        stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    assert returncode != 0, stdout + stderr
    assert _snapshot_database(root) == before
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (root / ".deployments" / ".runtime-transaction.json").exists()


def test_runtime_only_recovery_does_not_require_fresh_main(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_START"] = "api"
    env["RUNTIME_FAKE_ROLLBACK_READINESS_FAIL"] = "1"
    failed = _run_runtime(root, env)
    assert failed.returncode != 0
    assert json.loads((root / ".deployments/.runtime-transaction.json").read_text())["status"] == "unresolved"
    env["FAKE_GIT_REMOTE_UNREACHABLE"] = "1"
    env["RUNTIME_FAKE_START"] = ""
    env.pop("RUNTIME_FAKE_ROLLBACK_READINESS_FAIL")

    recovered = _run_runtime(root, env, recover=True)

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (
        (root / "bot-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert (
        (root / "api-image").read_text(encoding="ascii").strip()
        == OLD_RUNTIME_IMAGE_ID
    )
    assert _snapshot_database(root)["terms"][1] is not None


def test_runtime_only_recovery_failure_attempts_final_quiesce(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_READINESS_FAIL"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_STOP"] = "1"
    before = _snapshot_database(root)
    failed = _run_runtime(root, env)
    assert failed.returncode != 0
    actions_before = (root / "actions.log").read_text(encoding="utf-8").splitlines()

    recovery = _run_runtime(root, env, recover=True)

    assert recovery.returncode != 0
    assert _snapshot_database(root) == before
    journal = json.loads(
        (root / ".deployments" / ".runtime-transaction.json").read_text(
            encoding="ascii"
        )
    )
    assert journal["status"] == "unresolved"
    actions_after = (root / "actions.log").read_text(encoding="utf-8").splitlines()
    assert sum(" stop wuwaterm wuwaterm-api" in line for line in actions_after) >= (
        sum(" stop wuwaterm wuwaterm-api" in line for line in actions_before) + 2
    )


def test_runtime_only_recovery_then_same_commit_retry_reuses_immutable_manifest(
    runtime_harness,
):
    root, env, _old_hash, _new_hash = runtime_harness
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_READINESS_FAIL"] = "1"
    failed = _run_runtime(root, env)
    assert failed.returncode != 0
    assert json.loads((root / ".deployments/.runtime-transaction.json").read_text())["status"] == "unresolved"
    target_manifest = root / ".deployments" / f"{NEW_COMMIT}.json"
    before_manifest = target_manifest.read_bytes()

    env.pop("RUNTIME_FAKE_ROLLBACK_READINESS_FAIL")
    recovered = _run_runtime(root, env, recover=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr

    env.pop("FAKE_POINTER_DURABILITY_FAILURE")
    before_actions = (root / "actions.log").read_text(encoding="utf-8")
    retried = _run_runtime(root, env)

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert target_manifest.read_bytes() == before_manifest
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEW_COMMIT}\n"
    after_actions = (root / "actions.log").read_text(encoding="utf-8")
    assert after_actions.count(" build ") == before_actions.count(" build ")


def test_runtime_only_ignores_stale_rolled_back_journal_after_new_binding(
    runtime_harness,
):
    root, env, _old_hash, _new_hash = runtime_harness
    env["FAKE_POINTER_DURABILITY_FAILURE"] = "1"
    env["RUNTIME_FAKE_ROLLBACK_READINESS_FAIL"] = "1"
    failed = _run_runtime(root, env)
    assert failed.returncode != 0
    assert json.loads((root / ".deployments/.runtime-transaction.json").read_text())["status"] == "unresolved"
    env.pop("RUNTIME_FAKE_ROLLBACK_READINESS_FAIL")
    recovered = _run_runtime(root, env, recover=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr

    alt_manifest = build_manifest(
        source_commit=ALT_COMMIT,
        image_ref=f"wuwaterm-runtime:{ALT_COMMIT}",
        image_id=ALT_RUNTIME_IMAGE_ID,
        image_digest=ALT_RUNTIME_IMAGE_ID,
        image_revision=ALT_COMMIT,
        db_path=root / "data" / "terms.db",
        db_display_path="data/terms.db",
        backup_path="none",
        deployment_utc="2026-01-01T00:00:00Z",
    )
    write_manifest(
        root / ".deployments" / f"{ALT_COMMIT}.json", alt_manifest
    )
    publish_commit_pointer(root / ".deploy_commit", ALT_COMMIT)
    for service in ("bot", "api"):
        (root / f"{service}-image").write_text(
            f"{ALT_RUNTIME_IMAGE_ID}\n", encoding="ascii"
        )
        (root / f"{service}-ref").write_text(
            f"wuwaterm-runtime:{ALT_COMMIT}\n", encoding="ascii"
        )
        (root / f"{service}-running").write_text("true\n", encoding="ascii")
    env.pop("FAKE_POINTER_DURABILITY_FAILURE")
    env["RUNTIME_FAKE_HEAD_COMMIT"] = NEXT_COMMIT
    env["RUNTIME_FAKE_REMOTE_COMMIT"] = NEXT_COMMIT

    result = _run_runtime(root, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{NEXT_COMMIT}\n"


def test_runtime_only_lock_blocks_a_second_transaction(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_BLOCK_PHASE"] = "start"
    env["RUNTIME_FAKE_BLOCK_FILE"] = str(root / "start-ready")
    env["RUNTIME_FAKE_RELEASE_FILE"] = str(root / "start-release")
    env["RUNTIME_FAKE_DONE_FILE"] = str(root / "start-done")
    first = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "start-ready").exists():
            if first.poll() is not None:
                pytest.fail("first runtime-only process exited before lock barrier")
            if time.monotonic() >= deadline:
                pytest.fail("first runtime-only process did not reach start barrier")
            time.sleep(0.05)
        before = (root / "actions.log").read_text(encoding="utf-8")
        second = _run_runtime(root, env)
        assert second.returncode != 0
        assert "already in progress" in second.stderr
        assert (root / "actions.log").read_text(encoding="utf-8") == before
        (root / "start-release").write_text("release\n", encoding="ascii")
        first_returncode = first.wait(timeout=30)
        stdout, stderr = first.communicate()
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
            first.wait(timeout=10)
    assert first_returncode == 0, stdout + stderr
    assert (root / "start-done").exists()


def test_runtime_only_sigkill_child_keeps_lock_until_explicit_release(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_BLOCK_PHASE"] = "start"
    env["RUNTIME_FAKE_BLOCK_FILE"] = str(root / "sigkill-ready")
    env["RUNTIME_FAKE_RELEASE_FILE"] = str(root / "sigkill-release")
    env["RUNTIME_FAKE_DONE_FILE"] = str(root / "sigkill-done")
    env["RUNTIME_FAKE_CHILD_PID_FILE"] = str(root / "sigkill-child-pid")
    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "sigkill-ready").exists():
            if process.poll() is not None:
                pytest.fail("runtime-only process exited before SIGKILL barrier")
            if time.monotonic() >= deadline:
                pytest.fail("runtime-only process did not reach SIGKILL barrier")
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        blocked = _run_runtime(root, env, recover=True)
        assert blocked.returncode != 0
        assert "already in progress" in blocked.stderr
        (root / "sigkill-release").write_text("release\n", encoding="ascii")
        deadline = time.monotonic() + 15
        while not (root / "sigkill-done").exists():
            if time.monotonic() >= deadline:
                pytest.fail("orphaned Docker child did not release")
            time.sleep(0.05)
        while True:
            recovered = _run_runtime(root, env, recover=True)
            if recovered.returncode != 75:
                break
            if time.monotonic() >= deadline:
                pytest.fail("orphaned Docker child did not release the lock")
            time.sleep(0.05)
    finally:
        (root / "sigkill-release").write_text("release\n", encoding="ascii")
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        child_pid_path = root / "sigkill-child-pid"
        if child_pid_path.exists():
            try:
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                os.kill(child_pid, signal.SIGKILL)
            except (ProcessLookupError, ValueError, OSError):
                pass
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"


def test_runtime_only_recovery_rebuilds_alias_after_sigkill_during_tag(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_ROLLBACK_ALIAS_MISSING"] = "1"
    env["RUNTIME_FAKE_BLOCK_PHASE"] = "tag"
    env["RUNTIME_FAKE_BLOCK_FILE"] = str(root / "tag-ready")
    env["RUNTIME_FAKE_RELEASE_FILE"] = str(root / "tag-release")
    env["RUNTIME_FAKE_CHILD_PID_FILE"] = str(root / "tag-child-pid")
    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "tag-ready").exists():
            if process.poll() is not None:
                pytest.fail("runtime-only process exited before tag barrier")
            if time.monotonic() >= deadline:
                pytest.fail("runtime-only process did not reach tag barrier")
            time.sleep(0.05)
        child_pid = int((root / "tag-child-pid").read_text(encoding="ascii"))
        os.killpg(child_pid, signal.SIGKILL)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        journal = json.loads(
            (root / ".deployments" / ".runtime-transaction.json").read_text(
                encoding="ascii"
            )
        )
        assert journal["phase"] == "rollback_tag"
        assert not (root / "rollback-tag-created").exists()
        env["RUNTIME_FAKE_BLOCK_PHASE"] = ""
        recovered = _run_runtime(root, env, recover=True)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (root / "rollback-tag-created").exists()
    assert (root / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"


def test_runtime_only_recovery_quiesces_after_external_db_change(runtime_harness):
    root, env, _old_hash, _new_hash = runtime_harness
    env["RUNTIME_FAKE_BLOCK_PHASE"] = "start"
    env["RUNTIME_FAKE_BLOCK_FILE"] = str(root / "db-ready")
    env["RUNTIME_FAKE_RELEASE_FILE"] = str(root / "db-release")
    env["RUNTIME_FAKE_DONE_FILE"] = str(root / "db-done")
    env["RUNTIME_FAKE_CHILD_PID_FILE"] = str(root / "db-child-pid")
    changed_snapshot = None
    process = subprocess.Popen(
        ["sh", str(root / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "db-ready").exists():
            if process.poll() is not None:
                pytest.fail("runtime-only process exited before DB-change barrier")
            if time.monotonic() >= deadline:
                pytest.fail("runtime-only process did not reach DB-change barrier")
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        with sqlite3.connect(root / "data" / "terms.db") as connection:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = ?",
                ("externally-changed", "source_commit"),
            )
            connection.commit()
        changed_snapshot = _snapshot_database(root)
        blocked = _run_runtime(root, env, recover=True)
        assert blocked.returncode == 75
        (root / "db-release").write_text("release\n", encoding="ascii")
        env["RUNTIME_FAKE_BLOCK_PHASE"] = ""
        deadline = time.monotonic() + 20
        while True:
            recovery = _run_runtime(root, env, recover=True)
            if recovery.returncode != 75:
                break
            if time.monotonic() >= deadline:
                pytest.fail("orphaned Docker child did not release the lock")
            time.sleep(0.1)
    finally:
        (root / "db-release").write_text("release\n", encoding="ascii")
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        child_pid_path = root / "db-child-pid"
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            child_deadline = time.monotonic() + 15
            while time.monotonic() < child_deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    assert recovery.returncode != 0
    assert changed_snapshot is not None
    assert _snapshot_database(root) == changed_snapshot
    journal = json.loads(
        (root / ".deployments" / ".runtime-transaction.json").read_text(
            encoding="ascii"
        )
    )
    assert journal["status"] == "unresolved"
    assert (root / "bot-running").read_text(encoding="ascii").strip() == "false"
    assert (root / "api-running").read_text(encoding="ascii").strip() == "false"
