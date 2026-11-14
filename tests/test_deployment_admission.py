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

from scripts import component_recovery
from scripts import component_runtime
from scripts import component_state
from scripts import deployment_admission as admission
from scripts import package_recovery_source
import test_component_recovery as recovery_tests
from test_component_runtime import journal as runtime_journal  # noqa: F401
from test_deploy_scripts import _deployment_db


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
OLD_HEAD = "f8a9008d7adf60006ca210c750aa97f5de386a26"
COMPONENT_RUNTIME = ".deployments/.component-runtime-transaction.json"
COMPONENT_FULL = ".deployments/.component-full-transaction.json"


@pytest.mark.parametrize("directory", [admission.BUILD_STAGING, admission.WHEEL_BUILD_STAGING])
def test_builder_staging_requires_exact_recovery_before_other_admission(tmp_path: Path, directory: str) -> None:
    deployments = tmp_path / ".deployments"
    deployments.mkdir()
    staging = deployments / directory
    staging.mkdir()
    admission._attempts_clear(tmp_path)
    unknown = staging / "unknown.json"
    unknown.write_bytes(b"untrusted stage")
    before = unknown.stat()
    with pytest.raises(admission.AdmissionError, match="requires recovery"):
        admission._attempts_clear(tmp_path)
    assert unknown.read_bytes() == b"untrusted stage" and unknown.stat().st_ino == before.st_ino


@pytest.mark.parametrize("directory", [admission.BUILD_STAGING, admission.WHEEL_BUILD_STAGING])
def test_builder_staging_refuses_symlinked_directory(tmp_path: Path, directory: str) -> None:
    deployments = tmp_path / ".deployments"
    deployments.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    (deployments / directory).symlink_to(target, target_is_directory=True)
    with pytest.raises(admission.AdmissionError, match="cannot be inspected"):
        admission._attempts_clear(tmp_path)
    assert target.is_dir() and list(target.iterdir()) == []


def test_unknown_probe_staging_blocks_admission_without_removal(tmp_path: Path) -> None:
    staging = tmp_path / ".deployments" / admission.ADOPT_PROBE_STAGING
    staging.mkdir(parents=True)
    unknown = staging / "unknown"
    unknown.write_bytes(b"unattributed stage")
    inode = unknown.stat().st_ino
    with pytest.raises(admission.AdmissionError, match="probe staging metadata is unknown"):
        admission._attempts_clear(tmp_path)
    assert unknown.read_bytes() == b"unattributed stage" and unknown.stat().st_ino == inode


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


def _set_recovery_capability(root: Path, *, kind: str, ready: bool, protocol: object = 1) -> None:
    """Change only the copied checkout for tests of ordinary admission."""
    flag = "FULL_RECOVERY_EXECUTION_READY" if kind == "full" else "RECOVERY_EXECUTION_READY"
    artifact_flag = "ARTIFACT_FULL_RECOVERY_EXECUTION_READY" if kind == "full" else "ARTIFACT_RECOVERY_EXECUTION_READY"
    name = "CAPSULE_FULL_RECOVERY_PROTOCOL" if kind == "full" else "CAPSULE_RECOVERY_PROTOCOL"
    recovery = root / "scripts/component_recovery.py"
    text = recovery.read_text(encoding="utf-8")
    original = f"\n{flag} = False\n"
    assert original in text
    text = text.replace(original, f"\n{flag} = {ready!r}\n", 1)
    if protocol == 2:
        assert f"\n{artifact_flag} = True\n" in text
        text = text.replace(f"\n{artifact_flag} = True\n", f"\n{artifact_flag} = False\n", 1)
    recovery.write_text(text, encoding="utf-8")
    runtime = root / "scripts/component_runtime.py"
    text = runtime.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":\n'
    assert marker in text
    runtime.write_text(text.replace(marker, f"{name} = {protocol!r}\n\n" + marker, 1), encoding="utf-8")


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
                 "scripts/component_artifact.py", "scripts/install_probe.py",
                 "scripts/component_build_isolation.py", "scripts/component_full.py", "scripts/component_full_db.py",
                 "scripts/component_prepare.py",
                 "scripts/component_recovery.py", "scripts/component_wheel_build.py",
                 "scripts/component_state.py", "scripts/component_state_copy_v4.py", "scripts/component_state_endpoint.py",
                 "scripts/component_transaction.py",
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
        "old_bot_present": True,
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
    _set_recovery_capability(root, kind="runtime", ready=True)
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
    ("committed-legacy", True), ("rolled_back-legacy", True),
    ("active", False), ("active-legacy", False),
    ("malformed", False), ("link", False), ("full", False),
])
def test_component_admission_respects_old_journal_without_rewriting_it(checkout, old_state, allowed):
    root, env, spy = checkout
    _set_recovery_capability(root, kind="runtime", ready=True)
    old = root / ".deployments/.runtime-transaction.json"
    base_state = old_state.removesuffix("-legacy")
    if base_state in ("committed", "rolled_back", "active"):
        journal = _active_runtime_journal()
        if old_state.endswith("-legacy"):
            journal.pop("old_bot_present")
        journal.update(status=base_state, phase=base_state if base_state != "active" else "pointer")
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
        if old_state == "active-legacy":
            assert "legacy runtime transaction requires recovery" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize(("state", "operation", "transaction", "allowed"), [
    ("active", "adopt", "", False),
    ("active", "recover", "matching", False),
    ("active", "recover", "other", False),
    ("terminal", "adopt", "", True),
    ("terminal", "recover", "matching", False),
    ("absent", "recover", "matching", False),
    ("corrupt", "adopt", "", False),
    ("link", "adopt", "", False),
    ("full-conflict", "recover", "matching", False),
    ("build-unknown", "adopt", "", False),
    ("wheel-build-unknown", "adopt", "", False),
    ("adopt-probe-unknown", "adopt", "", False),
    ("pointer-malformed", "adopt", "", False),
    ("pointer-unbound", "adopt", "", False),
])
def test_component_admission_requires_matching_recovery_and_refuses_conflicts(
    checkout, runtime_journal, state, operation, transaction, allowed,  # noqa: F811
):
    root, env, spy = checkout
    _set_recovery_capability(root, kind="runtime", ready=True)
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
    elif state == "wheel-build-unknown":
        attempts = root / ".deployments/component-wheel-build-attempts"
        attempts.mkdir()
        (attempts / "unknown.json").write_text("{}", encoding="ascii")
    elif state == "adopt-probe-unknown":
        attempts = root / ".deployments/component-adopt-probes"
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
    for operation in ("adopt", "prepare-source"):
        for code_root, lock in ((root, None), (foreign, root / ".deployments/.deployment.lock")):
            command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), operation, str(code_root), ""]
            result = _run(root, env, command, lock=lock)
            assert result.returncode != 0
            assert "lock FD9" in result.stderr if lock is None else "roots differ" in result.stderr
            assert _snapshot(root) == before
            assert not spy.exists()


@pytest.mark.parametrize("operation,kind", [("adopt", "runtime"), ("update", "runtime"),
                                             ("full-update", "full")])
@pytest.mark.parametrize("ready,protocol", [(False, 1), (True, None), (True, True),
                                           (True, "1"), (True, 1.0), (True, 2)])
def test_component_writes_require_their_own_recovery_capability(checkout, operation, kind, ready, protocol):
    root, env, spy = checkout
    _set_recovery_capability(root, kind=kind, ready=ready, protocol=protocol)
    # A conflict makes the ordering observable: readiness must fail first.
    (root / ".deployments/.full-transaction.json").write_text("{}", encoding="ascii")
    before = _snapshot(root)
    command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), operation, str(root), ""]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert f"{kind} saved-source recovery is not ready" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize("kind,operation", [("runtime", "update"), ("full", "full-update")])
def test_component_admission_accepts_independently_ready_capability(checkout, kind, operation):
    root, env, spy = checkout
    _set_recovery_capability(root, kind=kind, ready=True)
    before = _snapshot(root)
    command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), operation, str(root), ""]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "admitted"
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize("conflict,expected", [
    ("legacy-full", "legacy full transaction requires recovery"),
    ("runtime", "component runtime journal is unknown"),
    ("pointer", "component deployment pointer is unknown"),
    ("registry", "build attempt metadata is unknown"),
])
def test_prepare_source_admission_keeps_conflict_pointer_and_registry_checks(checkout, conflict, expected):
    root, env, spy = checkout
    if conflict == "legacy-full":
        (root / ".deployments/.full-transaction.json").write_text("{}", encoding="ascii")
    elif conflict == "runtime":
        (root / COMPONENT_RUNTIME).write_text("{}", encoding="ascii")
    elif conflict == "pointer":
        (root / ".deploy_current").write_text("invalid\n", encoding="ascii")
    else:
        directory = root / ".deployments/component-build-attempts"
        directory.mkdir()
        (directory / "unknown.json").write_text("{}", encoding="ascii")
    before = _snapshot(root)
    code = r"""
import pathlib, sys
r = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(r / 'scripts'))
import component_recovery as recovery
recovery.prepare_source(r, 'a' * 40, 'b' * 40)
"""
    result = _run(root, env, [sys.executable, "-B", "-c", code, str(root)],
                  lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert expected in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


def test_prepare_source_refuses_a_different_physical_root(checkout, tmp_path):
    root, env, spy = checkout
    other = tmp_path / "other-root"
    (other / ".deployments").mkdir(parents=True)
    before, other_before = _snapshot(root), _snapshot(other)
    code = r"""
import pathlib, sys
source = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(source / 'scripts'))
import component_recovery as recovery
recovery.prepare_source(pathlib.Path(sys.argv[2]), 'a' * 40, 'b' * 40)
"""
    result = _run(root, env, [sys.executable, "-B", "-c", code, str(root), str(other)])
    assert result.returncode != 0
    assert "source and deployment roots differ" in result.stderr
    assert _snapshot(root) == before and _snapshot(other) == other_before
    assert not spy.exists()


def test_prepare_source_rechecks_registered_build_provenance(checkout, runtime_journal):  # noqa: F811
    from scripts import component_build  # noqa: PLC0415

    root, env, spy = checkout
    recovery_source = runtime_journal["source"]
    source = {key: recovery_source[key] for key in ("commit", "tree", "tool_bytes_sha256", "source_tool_sha256")}
    identifier = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    context_path = ".deployments/component-build-context-" + identifier
    context = root / context_path / "source"
    target_ref = "wuwaterm-component-build:" + identifier
    attempt = {
        "schema_version": 1, "build_id": identifier, "status": "active", "phase": "receipt",
        "source": source, "target_ref": target_ref, "context_path": context_path,
        "context_sha256": "1" * 64, "recipe_sha256": "2" * 64,
        "command": ["build", "--pull=false", "--rm=false", "--target", "runtime",
                    "--build-arg", "SOURCE_COMMIT=" + source["commit"], "--tag", target_ref,
                    "--file", str(context / "deploy/Dockerfile"), str(context)],
        "build_result": {"exit_code": 0, "output_sha256": "3" * 64,
                         "intermediate_prefixes": ["4" * 12], "intermediate_container_ids": ["4" * 64]},
        "probe_name": "wuwaterm-component-probe-" + identifier, "probe_container_id": "5" * 64,
        "receipt_sha256": None, "failure": None, "recovery_source": recovery_source,
    }
    _, digest = component_build._receipt(root, attempt, "sha256:" + "6" * 64, "0.7.0.dev1", ["4" * 64])
    attempt.update(status="registered", phase="registered", receipt_sha256=digest)
    component_build._write_attempt(root, attempt, new=True)
    assert component_build._read_attempt(root, identifier + ".json")["status"] == "registered"
    before = _snapshot(root)
    code = r"""
import pathlib, sys
r = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(r / 'scripts'))
import component_recovery as recovery
recovery.prepare_source(r, 'a' * 40, 'b' * 40)
"""
    result = _run(root, env, [sys.executable, "-B", "-c", code, str(root)],
                  lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert "retained build source" in result.stderr
    assert "build attempt metadata is unknown" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize("route", ["adopt", "api", "telegram", "both", "full"])
def test_direct_mutators_refuse_before_predecessor_or_external_work(checkout, runtime_journal, route):  # noqa: F811
    root, env, spy = checkout
    shutil.copy2(ROOT / "scripts/component_observe.py", root / "scripts/component_observe.py")
    # The inverse combination proves that FULL does not borrow runtime readiness.
    if route == "full":
        _set_recovery_capability(root, kind="runtime", ready=True)
        _set_recovery_capability(root, kind="full", ready=False)
    else:
        _set_recovery_capability(root, kind="runtime", ready=False)
    journal = copy.deepcopy(runtime_journal)
    selection = {"api": ["api"], "telegram": ["telegram"], "both": ["api", "telegram"]}.get(route, ["api"])
    journal["selection"] = selection
    for name in ("api", "telegram"):
        target = journal["target"]["containers"][name]
        if name in selection:
            target.update(id=None, name=None, started_at=None, restart_count=None,
                          transaction_label=journal["transaction_id"])
        else:
            journal["target"]["containers"][name] = copy.deepcopy(journal["before"]["containers"][name])
    component_runtime.validate_journal(journal)
    code = r"""
import json, pathlib, sys
r = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(r / 'scripts'))
import component_runtime as runtime
import component_transaction as transaction
import component_full as full
def forbidden(*args, **kwargs):
    raise AssertionError('predecessor or external work was reached')
transaction._prepare_predecessor = forbidden
transaction._compose_binary = forbidden
route = sys.argv[2]
if route == 'adopt':
    runtime.adopt(r, projects=[], compose_executable=pathlib.Path('/unused'), api_receipt=None, telegram_receipt=None)
elif route == 'full':
    full.prepare(r, pathlib.Path('/unused'), {'selection': 'none', 'receipts': {}, 'recipes': {}},
                 compose_path=pathlib.Path('/unused'))
else:
    transaction.update(r, json.loads(sys.argv[3]), compose_path=pathlib.Path('/unused'))
"""
    before = _snapshot(root)
    result = _run(root, env, [sys.executable, "-B", "-c", code, str(root), route, json.dumps(journal)],
                  lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert ("full" if route == "full" else "runtime") + " saved-source recovery is not ready" in result.stderr
    assert "predecessor or external work was reached" not in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


def test_full_ready_runtime_unready_reaches_real_source_and_compose_packaging(tmp_path, runtime_journal):  # noqa: F811
    root, _, _ = recovery_tests.create_source(tmp_path / "source")
    _set_recovery_capability(root, kind="full", ready=True)
    recovery_tests.git(root, "add", "scripts/component_recovery.py", "scripts/component_runtime.py")
    recovery_tests.git(root, "commit", "--quiet", "-m", "Enable full recovery fixture")
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    previous = copy.deepcopy(runtime_journal)
    previous["status"] = "committed"
    code = r"""
import json, pathlib, sys
r = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(r / 'scripts'))
import component_full as full
import component_recovery as recovery
import component_runtime as runtime
import component_transaction as transaction
import deployment_admission as admission
recovery.ARTIFACT_RECOVERY_EXECUTION_READY = False
previous = json.loads(sys.argv[3]); events = []; saved = []
admission.admit_component(r, operation='full-update', code_root=r)
try:
    admission.admit_component(r, operation='update', code_root=r)
except admission.AdmissionError as exc:
    assert 'runtime saved-source recovery is not ready' in str(exc)
else:
    raise AssertionError('runtime admission unexpectedly succeeded')
transaction.verify_terminal_for_full4 = lambda *args, **kwargs: (b'verified predecessor', previous)
def retirement(*args, **kwargs):
    events.append('predecessor')
    return b'verified predecessor'
transaction._prepare_predecessor = retirement
runtime.inspect_deployment = lambda root: {'binding': 'recorded', 'manifest': previous['target']['manifest']}
original = recovery.prepare_source
def capture(*args, **kwargs):
    source = original(*args, **kwargs)
    saved.append(source)
    return source
recovery.prepare_source = capture
class AfterPackaging(Exception):
    pass
def stop_after_packaging(*args, **kwargs):
    raise AfterPackaging()
full.manifest.verify_database_binding = stop_after_packaging
try:
    full.prepare(r, pathlib.Path('/unused'), {'selection': 'none', 'receipts': {}, 'recipes': {}},
                 compose_path=pathlib.Path(sys.argv[2]))
except AfterPackaging:
    pass
else:
    raise AssertionError('expected to stop after source packaging')
assert events == ['predecessor'] and len(saved) == 1
source = saved[0]
recovery._verify_capsule(r, source)
recovery._verify_bootstrap(r, source)
assert recovery._verify_bound_compose(r, source) == r / source['compose']['path']
print(json.dumps(source))
"""
    result = recovery_tests.locked(root, sys.executable, "-I", "-B", "-c", code,
                                   str(root), str(binary), json.dumps(previous))
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    assert source["compose"]["sha256"] == digest
    assert (root / source["capsule"]["path"] / "source.tar").is_file()
    assert (root / source["bootstrap"]["path"] / "component_recovery.py").is_file()


@pytest.mark.parametrize("state", ["active", "failed", "unknown"])
@pytest.mark.parametrize("operation", ["legacy", "component"])
def test_wheel_build_attempts_gate_legacy_and_component_writers(checkout, runtime_journal, state, operation):  # noqa: F811
    root, env, spy = checkout
    if operation == "component":
        _set_recovery_capability(root, kind="runtime", ready=True)
    source = runtime_journal["source"]
    ident = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    operations = {key: source[key] for key in ("commit", "tree", "tool_bytes_sha256", "source_tool_sha256")}
    attempt = {
        "schema_version": 1, "build_id": ident, "status": state, "phase": "failed" if state == "failed" else "prepared",
        "operations_source": operations, "recovery_source": source,
        "source": {"commit": "c" * 40, "tree": "d" * 40},
        "target_ref": "wuwaterm-wheel-build:" + ident,
        "context_path": ".deployments/component-wheel-build-context-" + ident,
        "telegram_wheel_sha256": None, "telegram_version": None, "core_wheel_sha256": None,
        "core_version": None, "core_lock_sha256": None, "core_artifact_sha256": None,
        "core_checksums_sha256": None, "core_source": None, "core_producer_kind": None,
        "dependencies": None, "source_archive_sha256": None, "context_sha256": None,
        "recipe_sha256": None, "wheel_build": None, "command": None, "build_result": None,
        "probe_name": "wuwaterm-wheel-probe-" + ident, "probe_container_id": None,
        "probe_sha256": None, "image_id": None, "image_revision": None, "receipt_sha256": None,
        "failure": "BuildError" if state == "failed" else None,
    }
    directory = root / ".deployments/component-wheel-build-attempts"
    directory.mkdir()
    (directory / (ident + ".json")).write_text(json.dumps(attempt if state != "unknown" else {}))
    before = _snapshot(root)
    if operation == "legacy":
        command = [sys.executable, "-B", str(root / "scripts/deployment_admission.py"), "legacy"]
    else:
        command = [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), "adopt", str(root), ""]
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    if state == "failed":
        assert "legacy failed builder lacks verified cleanup evidence" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize("operation", ["legacy", "component"])
def test_legacy_failed_source_builder_blocks_writers(checkout, runtime_journal, operation):  # noqa: F811
    root, env, spy = checkout
    if operation == "component":
        _set_recovery_capability(root, kind="runtime", ready=True)
    source = runtime_journal["source"]
    ident = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    operations = {key: source[key] for key in ("commit", "tree", "tool_bytes_sha256", "source_tool_sha256")}
    attempt = {"schema_version": 1, "build_id": ident, "status": "failed", "phase": "failed",
               "source": operations, "target_ref": "wuwaterm-component-build:" + ident,
               "context_path": ".deployments/component-build-context-" + ident,
               "context_sha256": None, "recipe_sha256": None, "command": None, "build_result": None,
               "probe_name": "wuwaterm-component-probe-" + ident, "probe_container_id": None,
               "receipt_sha256": None, "failure": "BuildError", "recovery_source": source}
    directory = root / ".deployments/component-build-attempts"
    directory.mkdir()
    (directory / (ident + ".json")).write_text(json.dumps(attempt), encoding="utf-8")
    before = _snapshot(root)
    command = ([sys.executable, "-B", str(root / "scripts/deployment_admission.py"), "legacy"] if operation == "legacy"
               else [sys.executable, "-B", "-c", _COMPONENT_ADMIT, str(root), "adopt", str(root), ""])
    result = _run(root, env, command, lock=root / ".deployments/.deployment.lock")
    assert result.returncode != 0
    assert "legacy failed builder lacks verified cleanup evidence" in result.stderr
    assert _snapshot(root) == before
    assert not spy.exists()


@pytest.mark.parametrize("case", [
    "old", "commit-target", "current-target", "both-target", "foreign-pointer",
    "rolled-back", "rolled-back-target", "committed", "committed-old", "committed-split",
    "changed-source", "wrong-identity", "conflict", "no-lock",
])
def test_recovery_admission_verifies_saved_a_against_b_and_pointer_preimages(
    tmp_path, runtime_journal, case,  # noqa: F811
):
    root, commit_a, tree_a = recovery_tests.create_source(tmp_path / "source-b")
    prepared = recovery_tests.prepare(root, commit_a, tree_a)
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    journal = copy.deepcopy(runtime_journal)
    if case.startswith("rolled-back"):
        journal.update(status="rolled_back", phase="rolled-back")
    elif case.startswith("committed"):
        journal.update(status="committed", phase="committed")
    recovery_tests.add_journal(root, source, journal)

    # The actual deployment checkout advances to B, whose runtime module
    # deliberately cannot be imported. Admission must use the extracted A.
    runtime_b = root / "scripts/component_runtime.py"
    runtime_b.write_text(runtime_b.read_text() + '\nraise RuntimeError("source B was imported")\n')
    recovery_tests.git(root, "add", "scripts/component_runtime.py")
    recovery_tests.git(root, "commit", "--quiet", "-m", "Synthetic source B")
    assert recovery_tests.git(root, "rev-parse", "HEAD") != commit_a

    code_root = tmp_path / "extracted-a"
    code_root.mkdir()
    package_recovery_source.verify(root / source["capsule"]["path"], commit_a,
                                   extracted=code_root, allow_backup=True)
    if case == "changed-source":
        (code_root / "docs/fixture.txt").write_text("modified after extraction\n")
    old_commit = journal["pointers"]["old"][".deploy_commit"]["bytes"]
    target_commit = journal["pointers"]["target"][".deploy_commit"]["bytes"]
    target_current = journal["pointers"]["target"][".deploy_current"]["bytes"]
    (root / ".deploy_commit").write_text(
        ("f" * 40 + "\n") if case == "foreign-pointer" else
        target_commit if case in ("commit-target", "both-target", "rolled-back-target", "committed", "committed-split") else old_commit,
        encoding="ascii",
    )
    if case in ("current-target", "both-target", "committed"):
        (root / ".deploy_current").write_text(target_current, encoding="ascii")
    if case == "conflict":
        (root / ".deployments/.full-transaction.json").write_text("{}", encoding="ascii")
    identity = "0" * 64 if case == "wrong-identity" else source["source_tool_sha256"]
    before = _snapshot(root)
    code = r"""
import pathlib, sys
code_root = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(code_root / 'scripts'))
import deployment_admission as admission
try:
    journal = admission.admit_component_recovery(pathlib.Path(sys.argv[1]),
        transaction_id=sys.argv[3], source_tool_sha256=sys.argv[4], code_root=code_root)
except admission.AdmissionError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
print(journal['transaction_id'])
"""
    result = recovery_tests.locked(root, sys.executable, "-I", "-B", "-c", code,
                                   str(root), str(code_root), journal["transaction_id"], identity,
                                   lock=case != "no-lock")
    if case in ("old", "commit-target", "current-target", "both-target", "rolled-back", "committed"):
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == journal["transaction_id"]
    else:
        assert result.returncode != 0
    assert _snapshot(root) == before


@pytest.mark.parametrize("mode,case,allowed", [
    ("same", "new", True), ("same", "matching", True), ("same", "no-id", False),
    ("same", "wrong-id", False), ("same", "wrong-txn", False), ("same", "wrong-source", False),
    ("same", "multiple", False), ("same", "no-lock", False), ("same", "non-telegram", False),
    ("standalone", "matching", False),
    ("capsule", "matching", True), ("capsule", "wrong-txn", False),
    ("capsule", "wrong-source", False), ("capsule", "multiple", False), ("capsule", "terminal", False),
    ("capsule", "non-telegram", False), ("capsule", "committed", False),
])
def test_state_probe_admission_is_bound_to_one_verified_telegram_transaction(
    tmp_path, runtime_journal, mode, case, allowed,  # noqa: F811
):
    root, commit_a, tree_a = recovery_tests.create_source(tmp_path / "source")
    prepared = recovery_tests.prepare(root, commit_a, tree_a)
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    journal = copy.deepcopy(runtime_journal)
    if case != "non-telegram":
        journal["selection"] = ["telegram"]
        journal["target"]["containers"]["api"] = copy.deepcopy(journal["before"]["containers"]["api"])
        telegram = copy.deepcopy(journal["before"]["containers"]["telegram"])
        telegram.update(id=None, name=None, started_at=None, restart_count=None,
                        transaction_label=journal["transaction_id"])
        journal["target"]["containers"]["telegram"] = telegram
    if case == "terminal":
        journal.update(status="rolled_back", phase="rolled-back")
    elif case == "committed":
        journal.update(status="committed", phase="committed")
    recovery_tests.add_journal(root, source, journal)
    (root / ".deploy_commit").write_text(journal["pointers"]["old"][".deploy_commit"]["bytes"])
    probe_id = "20261106T110001Z-0123456789abcdef0123456789abcdef"
    record = {
        "schema_version": 1, "kind": "state-plan", "probe_id": probe_id,
        "status": "active", "phase": "prepared", "transaction_id": journal["transaction_id"],
        "source_tool_sha256": source["source_tool_sha256"], "daemon_id": "1" * 64,
        "image_id": "sha256:" + "2" * 64, "image_revision": "3" * 40,
        "container_name": "wuwaterm-state-probe-" + probe_id, "container_id": None,
        "user": "0:0", "working_dir": "/app",
        "mounts": [{"host": "/srv/state", "container": "/app/data", "read_only": True,
                    "device": 1, "inode": 2}],
        "path_settings": {key: None for key in component_state._PATH_KEYS},
        "input_path": "/app/data/terms.db", "command": None, "output_sha256": None,
        "result": None, "failure": None,
    }
    if case == "wrong-txn":
        record["transaction_id"] = "20261106T110000Z-ffffffffffffffffffffffffffffffff"
    if case == "wrong-source":
        record["source_tool_sha256"] = "f" * 64
    record["command"] = component_state._command(record)
    if case != "new":
        directory = root / ".deployments/component-adopt-probes"
        directory.mkdir()
        (directory / (probe_id + ".json")).write_text(json.dumps(record))
        if case == "multiple":
            second = copy.deepcopy(record)
            second["probe_id"] = "20261106T110002Z-0123456789abcdef0123456789abcdef"
            second["container_name"] = "wuwaterm-state-probe-" + second["probe_id"]
            (directory / (second["probe_id"] + ".json")).write_text(json.dumps(second))
    code_root = root
    if mode == "capsule":
        code_root = tmp_path / "extracted-a"
        code_root.mkdir()
        package_recovery_source.verify(root / source["capsule"]["path"], commit_a,
                                       extracted=code_root, allow_backup=True)
        runtime_b = root / "scripts/component_runtime.py"
        runtime_b.write_text(runtime_b.read_text() + '\nraise RuntimeError("source B was imported")\n')
        recovery_tests.git(root, "add", "scripts/component_runtime.py")
        recovery_tests.git(root, "commit", "--quiet", "-m", "Synthetic source B")
    supplied_id = ("" if case in ("new", "no-id") else
                   "20261106T110003Z-ffffffffffffffffffffffffffffffff" if case == "wrong-id" else probe_id)
    code = r"""
import pathlib,sys
root,code_root=map(pathlib.Path,sys.argv[1:3]); sys.path.insert(0,str(code_root/'scripts'))
import deployment_admission as a
try:
 if sys.argv[3]=='capsule':
  a.admit_component_recovery(root,transaction_id=sys.argv[4],source_tool_sha256=sys.argv[5],code_root=code_root)
 elif sys.argv[3]=='standalone':
  a.admit_adoption_probe_recovery(root,probe_id=sys.argv[6],code_root=code_root)
 else:
  a.admit_state_probe(root,transaction_id=sys.argv[4],source_tool_sha256=sys.argv[5],
                      code_root=code_root,probe_id=sys.argv[6] or None)
except a.AdmissionError as exc:
 print(str(exc),file=sys.stderr); raise SystemExit(1)
print('admitted')
"""
    before = _snapshot(root)
    result = recovery_tests.locked(root, sys.executable, "-I", "-B", "-c", code,
                                   str(root), str(code_root), mode, journal["transaction_id"],
                                   source["source_tool_sha256"], supplied_id, lock=case != "no-lock")
    if allowed:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    assert _snapshot(root) == before


# ------------------------------------------------- protocol-2 mutator gates

def _parked_fd9():
    try:
        saved = os.dup(9)
    except OSError:
        return None
    os.close(9)
    return saved


def _restore_fd9(saved) -> None:
    if saved is not None:
        os.dup2(saved, 9)
        os.close(saved)


def test_artifact_attempts_block_mutators_until_registered_or_cancelled(tmp_path: Path) -> None:
    import test_component_artifact as artifact_tests  # noqa: PLC0415

    art = artifact_tests.art
    fx = artifact_tests.Fixture(tmp_path)
    admission._check_artifact_attempts(fx.root)  # no store: nothing to block
    saved = _parked_fd9()
    try:
        verified = fx.seal()
        docker = artifact_tests.FakeDocker()
        docker.fail_after_load = True
        with pytest.raises(art.ArtifactError):
            art.acquire(verified.path, fx.root, docker=docker)
    finally:
        _restore_fd9(saved)
    attempts = fx.root / ".deployments/artifacts/attempts"
    before = sorted((p.relative_to(fx.root), p.read_bytes()) for p in attempts.rglob("*") if p.is_file())
    with pytest.raises(admission.AdmissionError, match="requires recover or cancel: " + fx.attempt_id):
        admission._check_artifact_attempts(fx.root)
    assert sorted((p.relative_to(fx.root), p.read_bytes()) for p in attempts.rglob("*") if p.is_file()) == before
    saved = _parked_fd9()
    try:
        docker.fail_after_load = False
        assert art.recover(fx.root, fx.attempt_id, docker=docker)["state"] == "registered"
        other = artifact_tests.Fixture(tmp_path / "other")
        other.root = fx.root
        other.seal()
        art.cancel(fx.root, other.attempt_id)
    finally:
        _restore_fd9(saved)
    admission._check_artifact_attempts(fx.root)


def _helper_scan(monkeypatch, root: Path, statuses: dict, containers: dict) -> None:
    monkeypatch.setattr(admission, "_probe_statuses", lambda _root: dict(statuses))
    monkeypatch.setattr(admission, "_labelled_containers", lambda label: list(containers.get(label, [])))
    admission.scan_root_helpers(root)


def test_helper_scan_refuses_unrecorded_or_terminal_containers_with_exact_ids(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    (root / ".deployments").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    mine, foreign = admission.root_label_value(root), admission.root_label_value(other)
    assert mine == hashlib.sha256(str(root).encode()).hexdigest() != foreign
    probe_a, probe_b, probe_c = ("20261106T110000Z-" + c * 32 for c in "abc")
    state, adopt = admission.STATE_PROBE_LABEL, admission.ADOPT_PROBE_LABEL
    unrecorded = {"Id": "1" * 64, "Labels": {state: probe_a, admission.ROOT_LABEL: mine}}
    terminal = {"Id": "2" * 64, "Labels": {adopt: probe_b}}
    active = {"Id": "3" * 64, "Labels": {state: probe_c, admission.ROOT_LABEL: mine}}
    cross_root = {"Id": "4" * 64, "Labels": {state: "20261106T110000Z-" + "d" * 32, admission.ROOT_LABEL: foreign}}
    historical_foreign = {"Id": "5" * 64, "Labels": {adopt: "20261106T110000Z-" + "e" * 32}}
    statuses = {probe_b: "completed", probe_c: "active"}
    # Active helpers, other roots' helpers and unrelated historical helpers pass.
    _helper_scan(monkeypatch, root, statuses, {state: [active, cross_root], adopt: [historical_foreign]})
    with pytest.raises(admission.AdmissionError) as refused:
        _helper_scan(monkeypatch, root, statuses,
                     {state: [unrecorded, active, cross_root], adopt: [terminal, historical_foreign]})
    message = str(refused.value)
    assert "1" * 64 in message and "2" * 64 in message
    assert "3" * 64 not in message and "4" * 64 not in message and "5" * 64 not in message
    assert "no record" in message and "record is completed" in message
    # The other root sees only its own leftover, never this root's.
    with pytest.raises(admission.AdmissionError, match="4" * 64):
        _helper_scan(monkeypatch, other, {}, {state: [unrecorded, cross_root]})


def test_helper_scan_runs_inside_component_conflicts_before_writes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    (root / ".deployments").mkdir(parents=True)
    leftover = {"Id": "6" * 64, "Labels": {admission.STATE_PROBE_LABEL: "20261106T110000Z-" + "f" * 32,
                                           admission.ROOT_LABEL: admission.root_label_value(root)}}
    monkeypatch.setattr(admission, "_labelled_containers",
                        lambda label: [leftover] if label == admission.STATE_PROBE_LABEL else [])
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    with pytest.raises(admission.AdmissionError, match="6" * 64):
        admission._component_conflicts(root)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_helper_scan_against_the_real_daemon_ignores_other_fixture_roots(tmp_path: Path) -> None:
    """Read-only listing only: other fixtures' helpers on this daemon never block a fresh root."""
    root = tmp_path / "fresh"
    (root / ".deployments").mkdir(parents=True)
    try:
        for label in (admission.STATE_PROBE_LABEL, admission.ADOPT_PROBE_LABEL):
            assert type(admission._labelled_containers(label)) is list
    except admission.AdmissionError as exc:
        pytest.skip(f"Docker listing unavailable here: {exc}")
    admission.scan_root_helpers(root)


def test_product_update_refuses_build_before_taking_fd9(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for argv in (["--root", str(root), "build", "api"], ["--root=" + str(root), "build"]):
        result = subprocess.run(["sh", str(ROOT / "deploy/product-update.sh"), *argv],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 2
        assert "build" in result.stderr and "build-delivery" in result.stderr
    assert list(root.iterdir()) == []


def test_schema5_runtime_canonical_is_admissible_only_as_a_read_only_helper() -> None:
    runtime5 = {"schema_version": 5, "kind": "component-runtime", "selection": ["telegram"]}
    full5 = {"schema_version": 5, "kind": "component-full"}
    canonical = {"schema_version": 5, "kind": "state-canonical", "transaction_kind": "runtime"}
    # Schema 5 reads state through its own image, so its canonical comparison is
    # not a FULL-only endpoint claim.
    admission.refuse_inadmissible_state_probe(runtime5, canonical, "runtime")
    admission.refuse_inadmissible_state_probe(full5, dict(canonical, transaction_kind="full"), "full")
    for journal, probe, kind in (
            (runtime5, dict(canonical, kind="state-copy-stage"), "runtime"),
            (runtime5, dict(canonical, kind="state-endpoint"), "runtime"),
            (runtime5, dict(canonical, kind="state-canonical-v4"), "runtime"),
            ({"schema_version": 4, "kind": "component-runtime", "selection": ["telegram"]},
             dict(canonical, schema_version=4), "runtime"),
            (runtime5, dict(canonical, transaction_kind="full"), "runtime"),
            (full5, canonical, "full")):
        with pytest.raises(admission.AdmissionError):
            admission.refuse_inadmissible_state_probe(journal, probe, kind)
    # A schema-5 record still admits only the helpers whose programs never write.
    with pytest.raises(admission.AdmissionError, match="non-mutating"):
        admission.refuse_inadmissible_state_probe(
            runtime5, {"schema_version": 5, "kind": "state-copy-v4", "transaction_kind": "runtime"}, "runtime")


def test_artifact_recovery_flags_are_ready_by_default() -> None:
    assert component_recovery.ARTIFACT_RECOVERY_EXECUTION_READY is True
    assert component_recovery.ARTIFACT_FULL_RECOVERY_EXECUTION_READY is True

