"""Exercise deployment admission at the actual command boundaries.

The fixture is an isolated local checkout. Git and Docker are command spies:
admission itself and all deployment Python and shell entrypoints are real.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest

from scripts import component_runtime
from test_component_runtime import journal as runtime_journal  # noqa: F401
from test_deploy_scripts import _deployment_db


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
OLD_HEAD = "f8a9008d7adf60006ca210c750aa97f5de386a26"
COMPONENT_RUNTIME = ".deployments/.component-runtime-transaction.json"
COMPONENT_FULL = ".deployments/.component-full-transaction.json"


def _environment() -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("GIT_", "PYTHON"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def _snapshot(root: Path) -> dict[str, tuple]:
    """Record bytes, directory entries, identities and times, without following links."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                   else path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
        result[str(path.relative_to(root))] = (
            info.st_mode, info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, content,
        )
    return result


@pytest.fixture()
def checkout(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "checkout"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".deployments").mkdir()
    (root / "data").mkdir()
    (root / ".deployments/.deployment.lock").touch()
    shutil.copytree(ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("deploy/vps-update.sh", "deploy/docker-compose.yml",
                 "scripts/deployment_manifest.py", "scripts/deployment_admission.py",
                 "scripts/full_update.py", "scripts/runtime_update.py",
                 "scripts/component_runtime.py", "scripts/component_build.py",
                 "scripts/product_manifest.py", "scripts/package_recovery_source.py"):
        shutil.copy2(ROOT / name, root / name)
    (root / ".env").write_text("fixture=only\n", encoding="ascii")
    (root / ".env").chmod(0o600)
    (root / ".deploy_commit").write_text(COMMIT + "\n", encoding="ascii")
    (root / "data/source").write_bytes(b"source fixture\n")
    (root / "data/destination").write_bytes(b"destination fixture\n")
    _deployment_db(root / "data/terms.db", "admission-fixture")

    env = _environment()
    initialized = subprocess.run(["git", "init", "--quiet", str(root)], env=env,
                                 capture_output=True, text=True, check=False)
    assert initialized.returncode == 0, initialized.stderr
    spy = tmp_path / "command-spy.log"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name in ("git", "docker"):
        script = fakebin / name
        script.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {shlex.quote(name)}\" $*\" >> {shlex.quote(str(spy))}\n"
            + ("case \"$*\" in 'status --porcelain --untracked-files=all') exit 0 ;; esac\n"
               if name == "git" else "")
            + "exit 91\n", encoding="utf-8")
        script.chmod(0o755)
    # -B suppresses bytecode without inheriting or injecting any PYTHON* setting.
    python = fakebin / "python3"
    python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -B \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)
    env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"
    env["WUWATERM_DEPLOY_ROOT"] = str(root)
    return root, env, spy


_LOCK_RUNNER = r"""
import fcntl, os, subprocess, sys
lock, *command = sys.argv[1:]
fd = os.open(lock, os.O_RDWR | os.O_APPEND)
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
result = subprocess.run(command, pass_fds=(9,), capture_output=True, text=True, check=False)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
"""


def _run(root: Path, env: dict[str, str], command: list[str], *,
         lock: Path | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    if lock is not None:
        command = [sys.executable, "-B", "-c", _LOCK_RUNNER, str(lock), *command]
    return subprocess.run(command, cwd=cwd or root, env=env, capture_output=True,
                          text=True, timeout=30, check=False)


def _manifest_commands(root: Path) -> list[list[str]]:
    script = str(root / "scripts/deployment_manifest.py")
    prefix = [sys.executable, "-B", script]
    return [
        [*prefix, "create", "--path", str(root / "missing/manifest.json"),
         "--source-commit", NEXT_COMMIT, "--image-ref", "fixture:image",
         "--image-id", "fixture-id", "--image-digest", "fixture-digest",
         "--image-revision", NEXT_COMMIT, "--db", str(root / "data/terms.db"),
         "--backup-path", "none", "--deployment-utc", "2026-01-01T00:00:00+00:00"],
        [*prefix, "publish-pointer", "--path", str(root / "missing/pointer"),
         "--source-commit", NEXT_COMMIT],
        [*prefix, "durable-replace", "--source", str(root / "data/source"),
         "--destination", str(root / "data/destination")],
        [*prefix, "durable-remove", "--path", str(root / "data/source")],
    ]


def _writers(root: Path) -> list[list[str]]:
    shell = ["sh", str(root / "deploy/vps-update.sh")]
    runtime = [sys.executable, "-B", str(root / "scripts/runtime_update.py")]
    full = [sys.executable, "-B", str(root / "scripts/full_update.py")]
    prepare = [*full, "prepare", "--transaction-id", "20260101T000000Z-1",
               "--source-commit", NEXT_COMMIT, "--old-pointer-present", "1",
               "--old-pointer", COMMIT, "--db-backup", "none", "--old-db-sha", "1" * 64,
               "--candidate-path", "data/candidates/terms.20260101T000000Z-1.db",
               "--old-image-id", "sha256:" + "1" * 64,
               "--old-api-image-id", "sha256:" + "1" * 64,
               "--old-ref", "fixture:old", "--old-api-ref", "fixture:old",
               "--old-bot-running", "true", "--old-api-running", "true",
               "--rollback-image-ref", "wuwaterm-runtime:rollback-20260101T000000Z-1",
               "--rollback-api-image-ref", "wuwaterm-runtime:rollback-api-20260101T000000Z-1",
               "--target-image-ref", "wuwaterm-runtime:" + NEXT_COMMIT,
               "--target-image-id", "sha256:" + "2" * 64,
               "--target-image-digest", "sha256:" + "2" * 64,
               "--target-image-revision", NEXT_COMMIT]
    return [shell, *[[*shell, option] for option in
                     ("--runtime-only", "--recover-runtime", "--recover-full")],
            [*runtime, "--runtime-only"], [*runtime, "--recover-runtime"],
            *[[*full, command] for command in
              ("confirm", "rollback-clear", "recover", "restore-db")],
            [*full, "phase", "started"], prepare, *_manifest_commands(root)]


MARKERS = [
    (".deploy_current", "valid"), (".deploy_current", "malformed"), (".deploy_current", "link"),
    *[(name, shape) for name in (COMPONENT_RUNTIME, COMPONENT_FULL)
      for shape in ("active", "terminal", "malformed", "link")],
]


def _plant_marker(root: Path, name: str, shape: str) -> None:
    path = root / name
    if shape == "link":
        path.symlink_to("missing-record")
    elif shape == "valid":
        path.write_text("20261106T110000Z-0123456789abcdef0123456789abcdef\n", encoding="ascii")
    elif shape == "malformed":
        path.write_bytes(b"{invalid\n")
    else:
        path.write_text(json.dumps({"status": "active" if shape == "active" else "committed"}) + "\n",
                        encoding="ascii")


@pytest.mark.parametrize(("name", "shape"), MARKERS)
def test_current_writers_refuse_every_new_marker_without_mutation(checkout, name, shape, runtime_journal):  # noqa: F811
    root, env, spy = checkout
    if name == COMPONENT_RUNTIME and shape in ("active", "terminal"):
        journal = copy.deepcopy(runtime_journal)
        if shape == "terminal":
            journal.update(status="committed", phase="committed")
        component_runtime.validate_journal(journal)
        (root / name).write_text(json.dumps(journal), encoding="ascii")
    else:
        _plant_marker(root, name, shape)
    before = _snapshot(root)
    lock = root / ".deployments/.deployment.lock"
    for command in _writers(root):
        result = _run(root, env, command, lock=lock)
        assert result.returncode != 0, command
        assert "admission" in result.stderr.lower(), (command, result.stderr)
        assert _snapshot(root) == before, command
        assert not spy.exists(), command


@pytest.mark.parametrize("entry", ["shell", "runtime"])
def test_f8_writer_crosses_a_new_journal_before_git_fetch(checkout, entry):
    root, env, spy = checkout
    history = Path(os.environ.get("LEGACY_TEST_SOURCE_REPOSITORY", str(ROOT)))
    available = subprocess.run(["git", "cat-file", "-e", f"{OLD_HEAD}^{{commit}}"],
                               cwd=history, env=_environment(), capture_output=True, check=False)
    if available.returncode:
        pytest.skip("f8 historical commit is unavailable in this checkout")
    for name in ("deploy/vps-update.sh", "scripts/deployment_manifest.py",
                 "scripts/runtime_update.py", "scripts/full_update.py"):
        old = subprocess.run(["git", "show", f"{OLD_HEAD}:{name}"], cwd=history,
                             env=_environment(), capture_output=True, check=False)
        assert old.returncode == 0, old.stderr
        (root / name).write_bytes(old.stdout)
    _plant_marker(root, COMPONENT_RUNTIME, "active")
    if entry == "shell":
        result = _run(root, env, ["sh", str(root / "deploy/vps-update.sh")])
    else:
        result = _run(root, env, [sys.executable, "-B", str(root / "scripts/runtime_update.py"),
                                  "--runtime-only"], lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert "git fetch --quiet origin main:refs/remotes/origin/main" in spy.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["absent", "foreign", "replaced"])
def test_manifest_mutators_refuse_unverified_fd9_without_mutation(checkout, tmp_path, kind):
    root, env, spy = checkout
    lock = root / ".deployments/.deployment.lock"
    open_fd = None
    if kind == "foreign":
        lock = tmp_path / "foreign.lock"
        lock.touch()
    elif kind == "replaced":
        open_fd = os.open(lock, os.O_RDWR)
        replacement = tmp_path / "replacement.lock"
        replacement.touch()
        os.replace(replacement, lock)
    before = _snapshot(root)
    try:
        for command in _manifest_commands(root):
            if open_fd is None:
                result = _run(root, env, command, lock=None if kind == "absent" else lock)
            else:
                runner = r"""
import fcntl, os, subprocess, sys
fd = int(sys.argv[1]); os.dup2(fd, 9); fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
result = subprocess.run(sys.argv[2:], pass_fds=(9,), capture_output=True, text=True, check=False)
sys.stdout.write(result.stdout); sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
"""
                result = subprocess.run([sys.executable, "-B", "-c", runner, str(open_fd), *command],
                                        cwd=root, env=env, pass_fds=(open_fd,), capture_output=True,
                                        text=True, timeout=30, check=False)
            assert result.returncode != 0, command
            assert "admission" in result.stderr.lower(), (command, result.stderr)
            assert _snapshot(root) == before, command
            assert not spy.exists(), command
    finally:
        if open_fd is not None:
            os.close(open_fd)


@pytest.mark.parametrize(("operation", "foreign_argument"), [
    ("create", "--path"), ("publish-pointer", "--path"),
    ("durable-replace", "--source"), ("durable-replace", "--destination"),
    ("durable-remove", "--path"),
])
def test_manifest_mutators_confine_write_paths_to_script_checkout(checkout, tmp_path, operation, foreign_argument):
    root, env, spy = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "source"
    source.write_bytes(b"external fixture\n")
    command = next(cmd for cmd in _manifest_commands(root) if cmd[3] == operation)
    target = source if operation == "durable-remove" or foreign_argument == "--source" else outside / "new-dir/result"
    command[command.index(foreign_argument) + 1] = str(target)
    before, outside_before = _snapshot(root), _snapshot(outside)
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock", cwd=outside)
    assert result.returncode != 0
    assert "admission" in result.stderr.lower()
    assert _snapshot(root) == before
    assert _snapshot(outside) == outside_before
    assert not spy.exists()


def test_manifest_uses_script_root_when_cwd_is_elsewhere(checkout, tmp_path):
    root, env, spy = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    command = [sys.executable, "-B", str(root / "scripts/deployment_manifest.py"),
               "publish-pointer", "--path", str(root / ".deploy_commit"), "--source-commit", NEXT_COMMIT]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock", cwd=outside)
    assert result.returncode == 0, result.stderr
    assert (root / ".deploy_commit").read_bytes() == (NEXT_COMMIT + "\n").encode("ascii")
    assert not list(outside.iterdir())
    assert not spy.exists()


@pytest.mark.parametrize("operation", ["create", "publish-pointer", "durable-replace", "durable-remove"])
def test_manifest_mutators_work_with_admitted_lock(checkout, operation):
    root, env, _spy = checkout
    command = next(cmd for cmd in _manifest_commands(root) if cmd[3] == operation)
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode == 0, result.stderr
    if operation == "create":
        assert json.loads((root / "missing/manifest.json").read_text())["source_commit"] == NEXT_COMMIT
    elif operation == "publish-pointer":
        assert (root / "missing/pointer").read_bytes() == (NEXT_COMMIT + "\n").encode("ascii")
    elif operation == "durable-replace":
        assert not (root / "data/source").exists()
        assert (root / "data/destination").read_bytes() == b"source fixture\n"
    else:
        assert not (root / "data/source").exists()


def test_manifest_mutators_refuse_contended_lock(checkout):
    root, env, spy = checkout
    lock = root / ".deployments/.deployment.lock"
    before = _snapshot(root)
    runner = r"""
import os, subprocess, sys
fd = os.open(sys.argv[1], os.O_RDWR)
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
result = subprocess.run(sys.argv[2:], pass_fds=(9,), capture_output=True, text=True, check=False)
sys.stdout.write(result.stdout); sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
"""
    with lock.open("r+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        for command in _manifest_commands(root):
            result = _run(root, env, [sys.executable, "-B", "-c", runner, str(lock), *command])
            assert result.returncode != 0, command
            assert "admission" in result.stderr.lower(), (command, result.stderr)
            assert _snapshot(root) == before
            assert not spy.exists()


@pytest.mark.parametrize(("operation", "foreign_argument"), [
    ("create", "--path"), ("publish-pointer", "--path"),
    ("durable-replace", "--source"), ("durable-replace", "--destination"),
    ("durable-remove", "--path"),
])
@pytest.mark.parametrize("parent_dotdot", [False, True])
def test_manifest_mutators_refuse_symlink_escape(checkout, tmp_path, operation, foreign_argument, parent_dotdot):
    root, env, spy = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source").write_bytes(b"external fixture\n")
    (outside / "subdir").mkdir()
    (root / "link").symlink_to(outside / "subdir" if parent_dotdot else outside, target_is_directory=True)
    command = next(cmd for cmd in _manifest_commands(root) if cmd[3] == operation)
    target = "source" if operation == "durable-remove" or foreign_argument == "--source" else "new-dir/result"
    link = root / "link" / ".." if parent_dotdot else root / "link"
    command[command.index(foreign_argument) + 1] = str(link / target)
    before, outside_before = _snapshot(root), _snapshot(outside)
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert "admission" in result.stderr.lower()
    assert _snapshot(root) == before
    assert _snapshot(outside) == outside_before
    assert not spy.exists()


def test_manifest_create_external_wal_cannot_create_shared_memory(checkout, tmp_path):
    root, env, spy = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    original = root / "data/terms.db"
    connection = sqlite3.connect(original)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("wal_fixture", "test"))
        connection.commit()
        database = outside / "terms.db"
        shutil.copyfile(original, database)
        shutil.copyfile(Path(str(original) + "-wal"), Path(str(database) + "-wal"))
    finally:
        connection.close()
    shared_memory = Path(str(database) + "-shm")
    assert not shared_memory.exists()
    command = _manifest_commands(root)[0]
    command[command.index("--db") + 1] = str(database)
    before, outside_before = _snapshot(root), _snapshot(outside)
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0, {"external_shm_created": shared_memory.exists()}
    assert "admission" in result.stderr.lower(), result.stderr
    assert _snapshot(root) == before
    assert _snapshot(outside) == outside_before
    assert not spy.exists()


def test_manifest_create_immutable_external_database_is_read_only(checkout, tmp_path):
    root, env, spy = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    database = outside / "terms.db"
    shutil.copyfile(root / "data/terms.db", database)
    command = _manifest_commands(root)[0]
    command[command.index("--db") + 1] = str(database)
    command.append("--immutable")
    before = _snapshot(outside)
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode == 0, result.stderr
    assert _snapshot(outside) == before
    assert not spy.exists()


def test_manifest_readonly_commands_need_no_lock_and_do_not_write(checkout):
    from scripts.deployment_manifest import build_manifest, write_manifest

    root, env, spy = checkout
    db = root / "data/terms.db"
    path = root / ".deployments" / f"{COMMIT}.json"
    payload = build_manifest(source_commit=COMMIT, image_ref="fixture:image",
                             image_id="fixture-id", image_digest="fixture-digest",
                             image_revision=COMMIT, db_path=db,
                             db_display_path="data/terms.db", backup_path="none",
                             deployment_utc="2026-01-01T00:00:00+00:00")
    write_manifest(path, payload)
    before = _snapshot(root)
    script = str(root / "scripts/deployment_manifest.py")
    commands = [
        [sys.executable, "-B", script, "verify", "--path", str(path),
         "--source-commit", COMMIT, "--image-id", "fixture-id",
         "--image-digest", "fixture-digest", "--db", str(db)],
        [sys.executable, "-B", script, "verify", "--path", str(path),
         "--source-commit", COMMIT, "--image-id", "fixture-id",
         "--image-digest", "fixture-digest", "--db", str(db), "--immutable"],
        [sys.executable, "-B", script, "verify-pointer", "--path", str(root / ".deploy_commit"),
         "--source-commit", COMMIT],
    ]
    for command in commands:
        result = _run(root, env, command)
        assert result.returncode == 0, result.stderr
        assert _snapshot(root) == before
        assert not spy.exists()


@pytest.mark.parametrize("exclusive", [False, True])
def test_manifest_readonly_verify_rejects_real_wal_without_mutation(checkout, exclusive):
    from scripts.deployment_manifest import build_manifest, write_manifest

    root, env, spy = checkout
    db = root / "data/terms.db"
    path = root / ".deployments" / f"{COMMIT}.json"
    payload = build_manifest(source_commit=COMMIT, image_ref="fixture:image",
                             image_id="fixture-id", image_digest="fixture-digest",
                             image_revision=COMMIT, db_path=db,
                             db_display_path="data/terms.db", backup_path="none",
                             deployment_utc="2026-01-01T00:00:00+00:00")
    connection = sqlite3.connect(db)
    try:
        if exclusive:
            assert connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() == ("exclusive",)
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("INSERT INTO metadata (key, value) VALUES (?, ?)", ("sidecar_fixture", "test"))
        connection.commit()
        wal, shm = Path(str(db) + "-wal"), Path(str(db) + "-shm")
        assert wal.is_file()
        assert shm.exists() is not exclusive
        # Keep the original manifest schema and provenance but bind its hash
        # to the actual main DB bytes so rejection cannot be a hash mismatch.
        payload["database"]["sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
        write_manifest(path, payload)
        before = _snapshot(root)
        command = [sys.executable, "-B", str(root / "scripts/deployment_manifest.py"), "verify",
                   "--path", str(path), "--source-commit", COMMIT, "--image-id", "fixture-id",
                   "--image-digest", "fixture-digest", "--db", str(db)]
        result = _run(root, env, command)
        assert result.returncode != 0
        assert "sidecar" in result.stderr.lower(), result.stderr
        assert _snapshot(root) == before
        assert not spy.exists()
    finally:
        connection.close()


def _active_runtime_journal() -> dict:
    image = "sha256:" + "1" * 64
    return {
        "schema_version": 1, "mode": "runtime-only", "status": "active", "phase": "pointer",
        "transaction_id": "a" * 12 + "-1", "source_commit": NEXT_COMMIT,
        "target_image_ref": "wuwaterm-runtime:" + NEXT_COMMIT, "target_image_id": image,
        "target_image_digest": image, "target_image_revision": NEXT_COMMIT,
        "old_pointer": COMMIT, "old_manifest_path": ".deployments/" + COMMIT + ".json",
        "old_image_ref": "fixture:old", "old_image_id": image,
        "old_bot_running": True, "old_api_running": True,
        "rollback_image_ref": "wuwaterm-runtime:rollback-runtime-" + "a" * 12 + "-1",
        "db_sha256": "1" * 64, "db_stat": {"ino": 1, "size": 1, "mtime_ns": 1, "ctime_ns": 1},
        "tool_hash": "2" * 64,
    }


def test_real_legacy_parent_can_call_manifest_child_during_active_phase(checkout):
    root, env, _spy = checkout
    path = root / ".deployments/.runtime-transaction.json"
    path.write_text(json.dumps(_active_runtime_journal()), encoding="ascii")
    journal_before = path.read_bytes()
    code = r"""
import sys
from scripts import runtime_update as runtime
runtime._require_held_lock()
journal = runtime._load_journal()
assert journal['status'] == 'active' and journal['phase'] == 'pointer'
runtime._publish_pointer(sys.argv[1])
"""
    result = _run(root, env, [sys.executable, "-B", "-c", code, NEXT_COMMIT],
                  lock=root / ".deployments/.deployment.lock")
    assert result.returncode == 0, result.stderr
    assert (root / ".deploy_commit").read_bytes() == (NEXT_COMMIT + "\n").encode("ascii")
    assert path.read_bytes() == journal_before


def test_admitted_component_parent_can_import_manifest_helper(checkout):
    root, env, _spy = checkout
    code = r"""
import pathlib, sys
from scripts import deployment_admission as admission
from scripts import deployment_manifest as manifest
root = pathlib.Path(sys.argv[1])
admission.admit_component(root, operation='adopt', code_root=root)
manifest.publish_commit_pointer(root / '.deploy_commit', sys.argv[2])
"""
    result = _run(root, env, [sys.executable, "-B", "-c", code, str(root), NEXT_COMMIT],
                  lock=root / ".deployments/.deployment.lock")
    assert result.returncode == 0, result.stderr
    assert (root / ".deploy_commit").read_bytes() == (NEXT_COMMIT + "\n").encode("ascii")


_COMPONENT_ADMIT = r"""
import pathlib, sys
from scripts import deployment_admission as admission
root = pathlib.Path(sys.argv[1])
try:
    admission.admit_component(root, operation=sys.argv[2], code_root=pathlib.Path(sys.argv[3]),
                              transaction_id=sys.argv[4] or None)
except admission.AdmissionError as exc:
    print('admission refused: ' + str(exc), file=sys.stderr)
    raise SystemExit(1)
print('admitted')
"""


@pytest.mark.parametrize(("old_state", "allowed"), [
    ("absent", True), ("committed", True), ("rolled_back", True),
    ("active", False), ("malformed", False), ("link", False), ("full", False),
])
def test_component_admission_respects_old_journal_without_rewriting_it(checkout, old_state, allowed):
    root, env, spy = checkout
    old = root / ".deployments/.runtime-transaction.json"
    if old_state in ("committed", "rolled_back", "active"):
        journal = _active_runtime_journal()
        journal.update(status=old_state, phase=old_state if old_state != "active" else "pointer")
        old.write_text(json.dumps(journal), encoding="ascii")
    elif old_state == "malformed":
        old.write_bytes(b"{malformed\n")
    elif old_state == "link":
        old.symlink_to("missing")
    elif old_state == "full":
        (root / ".deployments/.full-transaction.json").write_text("{}", encoding="ascii")
    before = _snapshot(root)
    command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), "adopt", str(root), ""]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    if allowed:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "admitted"
    else:
        assert result.returncode != 0
        assert "admission refused" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize(("state", "operation", "transaction", "allowed"), [
    ("active", "adopt", "", False),
    ("active", "recover", "matching", True),
    ("active", "recover", "other", False),
    ("terminal", "adopt", "", True),
    ("terminal", "recover", "matching", False),
    ("absent", "recover", "matching", False),
    ("corrupt", "adopt", "", False),
    ("link", "adopt", "", False),
    ("full-conflict", "recover", "matching", False),
    ("build-unknown", "adopt", "", False),
    ("pointer-malformed", "adopt", "", False),
    ("pointer-unbound", "adopt", "", False),
])
def test_component_admission_requires_matching_recovery_and_refuses_conflicts(
    checkout, runtime_journal, state, operation, transaction, allowed,  # noqa: F811
):
    root, env, spy = checkout
    path = root / COMPONENT_RUNTIME
    if state in ("active", "terminal", "full-conflict"):
        journal = copy.deepcopy(runtime_journal)
        if state == "terminal":
            journal.update(status="committed", phase="committed")
        component_runtime.validate_journal(journal)
        path.write_text(json.dumps(journal), encoding="ascii")
    elif state == "corrupt":
        path.write_bytes(b"{invalid\n")
    elif state == "link":
        path.symlink_to("missing")
    elif state == "build-unknown":
        attempts = root / ".deployments/component-build-attempts"
        attempts.mkdir()
        (attempts / "unknown.json").write_text("{}", encoding="ascii")
    elif state.startswith("pointer-"):
        value = ("invalid" if state == "pointer-malformed"
                 else "20261106T110000Z-0123456789abcdef0123456789abcdef")
        (root / ".deploy_current").write_text(value + "\n", encoding="ascii")
    if state == "full-conflict":
        (root / ".deployments/.full-transaction.json").write_text("{}", encoding="ascii")
    if transaction == "matching":
        transaction = runtime_journal["transaction_id"]
    elif transaction == "other":
        transaction = "20261106T110000Z-ffffffffffffffffffffffffffffffff"
    before = _snapshot(root)
    command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), operation, str(root), transaction]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    if allowed:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "admission refused" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


def test_component_admission_rejects_foreign_code_root_and_missing_lock(checkout, tmp_path):
    root, env, spy = checkout
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    before = _snapshot(root)
    for code_root, lock in ((root, None), (foreign, root / ".deployments/.deployment.lock")):
        command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), "adopt", str(code_root), ""]
        result = _run(root, env, command, lock=lock)
        assert result.returncode != 0
        assert "admission refused" in result.stderr
        assert _snapshot(root) == before
        assert not spy.exists()
