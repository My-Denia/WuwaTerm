"""Exact historical source restoration through the real legacy entrypoints."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from test_deploy_scripts import OLD_COMMIT, _make_deploy_harness
from test_full_deploy_recovery import (
    API_ROLLBACK_REF,
    BOT_ROLLBACK_REF,
    OLD_IMAGE,
    OLD_REF,
    TXN,
    _journal,
    _plant,
    _surface,
    _write_full_fake_docker,
    _write_full_fake_python,
)
from test_runtime_only_deploy import OLD_RUNTIME_IMAGE_ID, _write_runtime_fake_docker

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = "cc5743df0e807c42e4b669564fd8b4d0a465b79c"
RUNTIME_ONLY_COMMIT = "34bd8b4a465624b4521bb093dc03ad999a869ac9"
FIRST_FULL_COMMIT = "d6b7e0ff3a59f5e4e8fe1957fa688e917a27325a"
NEW_SHELL_COMMIT = "a9491997369d68c9934f17bbfc4a343bd1527307"
UNREACHABLE = b"private unreachable fixture object\n"
PACKAGE = ROOT / "scripts/package_recovery_source.py"
DISPATCH = ROOT / "scripts/legacy_recovery.py"


def env_clean():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "PYTHON"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    return env


def command(args, *, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env or env_clean(), text=True,
                          capture_output=True, timeout=120, check=False)


def checked(args, *, cwd=None, env=None):
    result = command(args, cwd=cwd, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="session")
def capsule(tmp_path_factory):
    # CI uses a shallow checkout. Build an isolated source commit from its
    # tracked files; a local historical fixture can supply the exact old SHA.
    directory = tmp_path_factory.mktemp("historical-capsule")
    history = os.environ.get("LEGACY_TEST_SOURCE_REPOSITORY")
    source = directory / "source"
    source.mkdir()
    checked(["git", "init", str(source)])
    if history:
        commit = os.environ.get("LEGACY_TEST_SOURCE_COMMIT", HISTORICAL)
        checked(["git", "-C", str(source), "fetch", "--no-tags", history, commit])
        checked(["git", "-C", str(source), "checkout", "--detach", commit])
    else:
        paths = checked(["git", "-C", str(ROOT), "ls-files", "-z"]).split("\0")
        for path in filter(None, paths):
            original = ROOT / path
            destination = source / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
        checked(["git", "-C", str(source), "add", "--all"])
        checked(["git", "-C", str(source), "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-m", "fixture source"])
        commit = checked(["git", "-C", str(source), "rev-parse", "HEAD"])
    inserted = subprocess.run(["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                              input=UNREACHABLE, capture_output=True, env=env_clean(), check=False)
    assert inserted.returncode == 0, inserted.stderr
    output = directory / "capsule"
    checked([sys.executable, str(PACKAGE), "--repository", str(source),
             "--source-commit", commit, "--output", str(output)])
    return output


def source_commit(capsule):
    return json.loads((capsule / "manifest.json").read_text())["source_commit"]


def test_capsule_declares_only_available_recovery_kinds(capsule):
    manifest = json.loads((capsule / "manifest.json").read_text())
    source = manifest["source_commit"]
    assert manifest["tool_hashes"]["runtime"] is not None
    if source == RUNTIME_ONLY_COMMIT:
        assert manifest["supported_kinds"] == ["runtime"]
        assert manifest["tool_hashes"]["runtime"].startswith("6b172316")
        assert manifest["tool_hashes"]["full"] is None
    else:
        assert manifest["supported_kinds"] == ["runtime", "full"]
        if source == FIRST_FULL_COMMIT:
            assert manifest["tool_hashes"]["runtime"].startswith("1e024cd0")
            assert manifest["tool_hashes"]["full"].startswith("e3bb030a")
        if source in (NEW_SHELL_COMMIT, HISTORICAL):
            assert manifest["tool_hashes"]["runtime"].startswith("5bcdf001")
            assert manifest["tool_hashes"]["full"].startswith("70cc11d9")


def test_bundle_excludes_unreachable_fixture_object(tmp_path, capsule):
    bare = tmp_path / "consumer.git"
    checked(["git", "init", "--bare", str(bare)])
    checked(["git", "-C", str(bare), "fetch", "--no-tags", str(capsule / "source.bundle"),
             "refs/capsule/source"])
    oid = hashlib.sha1(b"blob " + str(len(UNREACHABLE)).encode() + b"\0" + UNREACHABLE).hexdigest()
    assert command(["git", "-C", str(bare), "cat-file", "-e", oid]).returncode != 0


def test_capsule_refuses_private_path_in_ancestor(tmp_path, capsule):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    git(root, "fetch", "--no-tags", str(capsule / "source.bundle"), "refs/capsule/source")
    git(root, "checkout", "--detach", source_commit(capsule))
    (root / ".env").write_text("fixture only")
    git(root, "add", "-f", ".env")
    config = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
    git(root, *config, "commit", "-m", "private fixture")
    git(root, "rm", "--quiet", ".env")
    git(root, *config, "commit", "-m", "remove private fixture")
    current = git(root, "rev-parse", "HEAD")
    output = tmp_path / "refused"
    result = command([sys.executable, str(PACKAGE), "--repository", str(root),
                      "--source-commit", current, "--output", str(output)])
    assert result.returncode != 0
    assert "protected path" in result.stderr
    assert not output.exists()


def git(root, *args):
    return checked(["git", "-C", str(root), *args])


def deployment(tmp_path, capsule, kind):
    historical = source_commit(capsule)
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    harness, env, old_hash, new_hash = _make_deploy_harness(harness_dir)
    if kind == "runtime":
        _write_runtime_fake_docker(harness)
        manifest_path = harness / ".deployments" / f"{OLD_COMMIT}.json"
        manifest_path.chmod(0o600)
        old_manifest = json.loads(manifest_path.read_text())
        old_manifest["image"]["id"] = OLD_RUNTIME_IMAGE_ID
        old_manifest["image"]["digest"] = OLD_RUNTIME_IMAGE_ID
        manifest_path.write_text(json.dumps(old_manifest))
        manifest_path.chmod(0o444)
        (harness / "state-api").mkdir()
    else:
        _write_full_fake_docker(harness)
        _write_full_fake_python(harness)
        for service in ("bot", "api"):
            _surface(harness, service, image=OLD_IMAGE, ref=OLD_REF, running=False)
        for ref in (BOT_ROLLBACK_REF, API_ROLLBACK_REF):
            (harness / f"tag-{ref}").write_text(OLD_IMAGE + "\n")
    root = tmp_path / "deployment"
    root.mkdir()
    with tarfile.open(capsule / "source.tar") as archive:
        archive.extractall(root, filter="data")
    git(root, "init")
    for item in harness.iterdir():
        if item.name in {"src", "scripts", "deploy"}:
            continue
        destination = root / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
    # Historical validators bind the DB's provenance to their own data pin.
    # Build the synthetic fixture with that exact source, then update the old
    # manifest; importing today's profile would make earlier journals invalid.
    fixture_code = r'''
import hashlib, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root/'src'), str(root/'tests')]
from test_deploy_scripts import _deployment_db
from scripts.deployment_manifest import read_db_provenance
for name, suffix in [('terms.db', 'old'), ('candidate-seed.db', 'new')]:
    target = root/'data'/name
    temporary = root/'data'/(name + '.fixture')
    _deployment_db(temporary, suffix)
    os.replace(temporary, target)
old = root/'data/terms.db'
new = root/'data/candidate-seed.db'
path = root/'.deployments'/('1'*40 + '.json')
manifest = json.loads(path.read_text())
manifest['database']['sha256'] = hashlib.sha256(old.read_bytes()).hexdigest()
manifest['database']['provenance'] = read_db_provenance(old, immutable=True)
path.chmod(0o600)
path.write_text(json.dumps(manifest))
path.chmod(0o444)
print(manifest['database']['sha256'])
print(hashlib.sha256(new.read_bytes()).hexdigest())
'''
    old_hash, new_hash = checked([sys.executable, "-I", "-B", "-c", fixture_code, str(root)]).splitlines()
    if kind == "full":
        backup = root / f"data/deployment-backups/terms.{TXN}.{old_hash}.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "data/terms.db", backup)
    exclude = root / ".git/info/exclude"
    exclude.write_text(exclude.read_text() + "\n/bot-*\n/api-*\n/tag-*\n/actions.log\n/docker.log\n/rollback-tag-created\n/running-image\n")
    # Preserve real Git identity while reusing the established fake Docker.
    fake_git = harness_dir / "bin/git"
    fake_git.write_text("#!/bin/sh\nexec /usr/bin/git \"$@\"\n")
    fake_git.chmod(0o755)
    env = {key: value for key, value in env.items() if not key.startswith(("GIT_", "PYTHON"))}
    env["FAKE_DEPLOY_ROOT"] = str(root)
    env["WUWATERM_DEPLOY_ROOT"] = str(root)
    env["RUNTIME_FAKE_HEAD_COMMIT"] = historical
    db = root / "data/terms.db"
    hashes = json.loads((capsule / "manifest.json").read_text())["tool_hashes"]
    if kind == "full":
        payload = _journal(root, phase="armed", old_db_sha=old_hash,
                           db_backup=f"data/deployment-backups/terms.{TXN}.{old_hash}.db",
                           new_db_sha=new_hash, candidate_rel=f"data/candidates/terms.{TXN}.db",
                           source_commit=historical, tool_hash=hashes["full"] or "0" * 64)
        _plant(root, payload)
    else:
        info = db.stat()
        txn = historical[:12] + "-1234"
        payload = {"schema_version": 1, "mode": "runtime-only", "status": "unresolved",
                   "phase": "unresolved", "transaction_id": txn,
                   "source_commit": historical, "target_image_ref": f"wuwaterm-runtime:{historical}",
                   "target_image_id": OLD_RUNTIME_IMAGE_ID, "target_image_digest": OLD_RUNTIME_IMAGE_ID,
                   "target_image_revision": historical, "old_pointer": OLD_COMMIT,
                   "old_manifest_path": f".deployments/{OLD_COMMIT}.json",
                   "old_image_ref": "wuwaterm-runtime:old", "old_image_id": OLD_RUNTIME_IMAGE_ID,
                   "old_bot_running": True, "old_api_running": True,
                   "rollback_image_ref": f"wuwaterm-runtime:rollback-runtime-{txn}",
                   "db_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
                   "db_stat": {"ino": info.st_ino, "size": info.st_size,
                               "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns},
                   "tool_hash": hashes["runtime"]}
        (root / ".deployments/.runtime-transaction.json").write_text(json.dumps(payload))
    lock = root / ".deployments/.deployment.lock"
    lock.touch()
    for name in ("state/user-file", "state-api/device-file"):
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_text("owner data")
    # Move the deployment source forward without changing either legacy tool.
    (root / "README.md").write_text((root / "README.md").read_text() + "\ncurrent source\n")
    git(root, "add", "--all")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture current source")
    # The old commit is absent: recovery must import it from the offline bundle.
    assert command(["git", "-C", str(root), "cat-file", "-e", historical]).returncode != 0
    return root, env, payload


def dispatch(root, env, capsule, kind, *, recover=True, pass_fds=()):
    args = [sys.executable, "-I", str(DISPATCH), "--root", str(root), "--capsule", str(capsule),
            "--kind", kind]
    if recover:
        args.append("--recover")
    return subprocess.run(args, env=env, cwd=root, text=True, capture_output=True,
                          timeout=120, check=False, pass_fds=pass_fds)


def test_preflight_is_read_only(tmp_path, capsule):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    before_head = git(root, "rev-parse", "HEAD")
    before = {path.name: path.read_bytes() for path in (root / ".deployments").iterdir()}
    result = dispatch(root, env, capsule, "full", recover=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "HEAD") == before_head
    assert {path.name: path.read_bytes() for path in (root / ".deployments").iterdir()} == before
    assert not (root / "docker.log").exists()


@pytest.mark.parametrize("marker", [".deploy_current", ".deployments/.component-runtime-transaction.json",
                                    ".deployments/.component-full-transaction.json"])
def test_dispatcher_rejects_new_metadata_before_source_mutation(tmp_path, capsule, marker):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    head = git(root, "rev-parse", "HEAD")
    journal = root / ".deployments/.full-transaction.json"
    before = journal.read_bytes()
    db = (root / "data/terms.db").read_bytes()
    marker_path = root / marker
    marker_path.symlink_to("missing-record")
    entries = {path.name for path in (root / ".deployments").iterdir()}

    result = dispatch(root, env, capsule, "full")

    assert result.returncode != 0
    assert "component deployment metadata" in result.stderr
    assert git(root, "rev-parse", "HEAD") == head
    assert journal.read_bytes() == before
    assert (root / "data/terms.db").read_bytes() == db
    assert {path.name for path in (root / ".deployments").iterdir()} == entries
    assert not (root / "docker.log").exists()


@pytest.mark.parametrize("stage", ["before-lock", "after-checkout"])
def test_dispatcher_rechecks_new_metadata_across_checkout(tmp_path, capsule, stage):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    head = git(root, "rev-parse", "HEAD")
    journal = root / ".deployments/.full-transaction.json"
    before = journal.read_bytes()
    db = (root / "data/terms.db").read_bytes()
    wrapper = tmp_path / "harness/bin/git"
    wrapper.write_text('''#!/bin/sh
/usr/bin/git "$@"
status=$?
if [ "$status" -eq 0 ]; then
  case "$LEGACY_RACE_STAGE:$*" in
    *"before-lock:"*" ls-files -v -z"|*"after-checkout:"*" checkout --detach --no-overwrite-ignore "*)
      printf 'invalid\\n' > "$FAKE_DEPLOY_ROOT/.deploy_current" ;;
  esac
fi
exit "$status"
''', encoding="utf-8")
    wrapper.chmod(0o755)
    env["LEGACY_RACE_STAGE"] = stage

    result = dispatch(root, env, capsule, "full")

    assert result.returncode != 0
    assert "component deployment metadata" in result.stderr
    assert (root / ".deploy_current").read_bytes() == b"invalid\n"
    assert journal.read_bytes() == before
    assert (root / "data/terms.db").read_bytes() == db
    assert not (root / "docker.log").exists()
    if stage == "before-lock":
        assert git(root, "rev-parse", "HEAD") == head
        assert not list((root / ".deployments").glob("legacy-source-return-*"))
    else:
        assert git(root, "rev-parse", "HEAD") == source_commit(capsule)
        assert len(list((root / ".deployments").glob("legacy-source-return-*"))) == 1


def test_inherited_git_and_python_environment_cannot_redirect_recovery(tmp_path, capsule):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    env.update(GIT_DIR=str(tmp_path / "foreign.git"), GIT_WORK_TREE=str(tmp_path / "foreign"),
               GIT_INDEX_FILE=str(tmp_path / "foreign-index"), PYTHONPATH=str(tmp_path / "foreign"),
               PYTHONHOME=str(tmp_path / "foreign"))
    result = dispatch(root, env, capsule, "full")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "HEAD") == source_commit(capsule)
    assert not (tmp_path / "foreign-index").exists()


def test_full_recovery_rejects_unknown_companion_journal(tmp_path, capsule):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    head = git(root, "rev-parse", "HEAD")
    (root / ".deployments/.runtime-transaction.json").write_text('{"schema_version": 3}')
    result = dispatch(root, env, capsule, "full")
    assert result.returncode != 0
    assert git(root, "rev-parse", "HEAD") == head
    assert not (root / "docker.log").exists()


def test_early_runtime_source_refuses_full_journal_before_mutation(tmp_path, capsule):
    if source_commit(capsule) != RUNTIME_ONLY_COMMIT:
        pytest.skip("requires the historical runtime-only source fixture")
    root, env, _payload = deployment(tmp_path, capsule, "full")
    before = git(root, "rev-parse", "HEAD")
    journal = root / ".deployments/.full-transaction.json"
    original = journal.read_bytes()
    result = dispatch(root, env, capsule, "full")
    assert result.returncode != 0
    assert "does not support this recovery kind" in result.stderr
    assert git(root, "rev-parse", "HEAD") == before
    assert journal.read_bytes() == original
    assert not (root / "docker.log").exists()
    assert not list((root / ".deployments").glob("legacy-source-return-*"))


def test_return_backup_allows_current_source_without_legacy_tools(tmp_path, capsule):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    git(root, "rm", "--quiet", "deploy/vps-update.sh", "deploy/docker-compose.yml",
        "scripts/runtime_update.py", "scripts/full_update.py", "scripts/deployment_manifest.py")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture post-migration source")
    current = git(root, "rev-parse", "HEAD")
    result = dispatch(root, env, capsule, "full")
    assert result.returncode == 0, result.stdout + result.stderr
    backup, = (root / ".deployments").glob("legacy-source-return-*")
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["source_commit"] == current
    assert manifest["tool_hashes"] == {"runtime": None, "full": None}
    checked([sys.executable, str(PACKAGE), "--verify", "--source-commit", current,
             "--output", str(backup)])


@pytest.mark.parametrize("kind", ["runtime", "full"])
def test_original_recovery_runs_from_exact_restored_commit(tmp_path, capsule, kind):
    root, env, payload = deployment(tmp_path, capsule, kind)
    before = git(root, "rev-parse", "HEAD")
    lock = root / ".deployments/.deployment.lock"
    inode = lock.stat().st_ino
    data = (root / "data/terms.db").read_bytes()
    if kind == "full":
        # Model an interruption after the candidate replaced the live DB.
        candidate = (root / "data/candidate-seed.db").read_bytes()
        assert candidate != data
        (root / "data/terms.db").write_bytes(candidate)
        payload["phase"] = "promoted"
        (root / ".deployments/.full-transaction.json").write_text(json.dumps(payload))
    result = dispatch(root, env, capsule, kind)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "HEAD") == source_commit(capsule)
    assert (root / "data/terms.db").read_bytes() == data
    assert lock.stat().st_ino == inode
    assert (root / "state/user-file").read_text() == "owner data"
    assert (root / "state-api/device-file").read_text() == "owner data"
    backups = list((root / ".deployments").glob("legacy-source-return-*"))
    assert len(backups) == 1
    assert json.loads((backups[0] / "return.json").read_text())["source_commit"] == before
    assert checked(["git", "bundle", "list-heads", str(backups[0] / "source.bundle")]).startswith(before)
    assert (root / "docker.log").exists()
    if kind == "runtime":
        assert json.loads((root / ".deployments/.runtime-transaction.json").read_text())["status"] == "rolled_back"
    else:
        assert not (root / ".deployments/.full-transaction.json").exists()
    assert (root / ".deploy_commit").read_text().strip() == OLD_COMMIT
    assert payload["source_commit"] == source_commit(capsule)


@pytest.mark.parametrize("fault", ["schema", "phase", "journal_hash", "journal_source", "dirty",
                                  "index_flag", "untracked", "symlink", "archive", "bundle",
                                  "missing_objects", "traversal", "archive_symlink", "transitive"])
def test_preflight_refuses_before_checkout_or_docker(tmp_path, capsule, fault):
    root, env, payload = deployment(tmp_path, capsule, "full")
    saved_head = git(root, "rev-parse", "HEAD")
    journal = root / ".deployments/.full-transaction.json"
    test_capsule = capsule
    if fault in {"schema", "phase", "journal_hash", "journal_source"}:
        key, value = {"schema": ("schema_version", 3), "phase": ("phase", "unknown"),
                      "journal_hash": ("tool_hash", "0" * 64),
                      "journal_source": ("source_commit", "f" * 40)}[fault]
        payload[key] = value
        journal.write_text(json.dumps(payload))
    elif fault == "dirty":
        (root / "README.md").write_text("uncommitted")
    elif fault == "index_flag":
        git(root, "update-index", "--assume-unchanged", "README.md")
        (root / "README.md").write_text("unreported change")
    elif fault == "untracked":
        # A path present only in the old tree must never overwrite an ignored file.
        git(root, "rm", "--quiet", "docs/deployment.md")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "fixture deleted tracked path")
        saved_head = git(root, "rev-parse", "HEAD")
        exclude = root / ".git/info/exclude"
        exclude.write_text(exclude.read_text() + "/docs/deployment.md\n")
        (root / "docs/deployment.md").write_text("user content")
    elif fault == "symlink":
        (root / "docs/deployment.md").unlink()
        (root / "docs/deployment.md").symlink_to(root / "data/terms.db")
    else:
        test_capsule = tmp_path / "bad-capsule"
        shutil.copytree(capsule, test_capsule)
        if fault in {"archive", "transitive"}:
            path = test_capsule / "source.tar"
            raw = bytearray(path.read_bytes())
            if fault == "transitive":
                marker = b"from wuwaterm.data_source import"
                offset = raw.find(marker)
                assert offset >= 0
                raw[offset] ^= 1
                path.write_bytes(raw)
                manifest_path = test_capsule / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["archive_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest_path.write_text(json.dumps(manifest))
            else:
                raw[1000] ^= 1
                path.write_bytes(raw)
        elif fault in {"traversal", "archive_symlink"}:
            path = test_capsule / "source.tar"
            with tarfile.open(path, "a:") as archive:
                member = tarfile.TarInfo("../escape" if fault == "traversal" else "malicious-link")
                member.type = tarfile.SYMTYPE
                member.linkname = "data/terms.db"
                archive.addfile(member)
            manifest_path = test_capsule / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["archive_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
        elif fault == "missing_objects":
            path = test_capsule / "source.bundle"
            original = path.read_bytes()
            path.write_bytes(original[:len(original) // 2])
            manifest_path = test_capsule / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["bundle_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
        else:
            with (test_capsule / "source.bundle").open("ab") as stream:
                stream.write(b"tamper")
    journal_before = journal.read_bytes()
    result = dispatch(root, env, test_capsule, "full")
    assert result.returncode != 0, result.stdout + result.stderr
    assert git(root, "rev-parse", "HEAD") == saved_head
    assert journal.read_bytes() == journal_before
    assert not (root / "docker.log").exists()
    assert not list((root / ".deployments").glob("legacy-source-return-*"))
    if fault == "untracked":
        assert (root / "docs/deployment.md").read_text() == "user content"
        assert "source would overwrite untracked data" in result.stderr


def test_inherited_deployment_lock_reaches_original_children(tmp_path, capsule):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    lock = root / ".deployments/.deployment.lock"
    fake_docker = tmp_path / "harness/bin/docker"
    text = fake_docker.read_text()
    fake_docker.write_text(text.replace("#!/bin/sh\n", "#!/bin/sh\nstat -Lc '%d:%i' /proc/self/fd/9 >> \"$FAKE_DEPLOY_ROOT/.deployments/fd9\"\n", 1))
    # A separate wrapper owns FD 9; overwriting a descriptor in pytest itself
    # can close pytest's fault-handler descriptor and invalidate the run.
    wrapper = """
import fcntl, os, subprocess, sys
lock, dispatcher, root, capsule = sys.argv[1:]
fd = os.open(lock, os.O_RDWR)
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
raise SystemExit(subprocess.run([sys.executable, '-I', dispatcher, '--root', root,
    '--capsule', capsule, '--kind', 'full', '--recover'], pass_fds=(9,)).returncode)
"""
    result = command([sys.executable, "-c", wrapper, str(lock), str(DISPATCH),
                      str(root), str(capsule)], env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    info = lock.stat()
    assert set((root / ".deployments/fd9").read_text().splitlines()) == {f"{info.st_dev}:{info.st_ino}"}


@pytest.mark.parametrize("kind", ["foreign", "replaced"])
def test_dispatcher_refuses_foreign_or_replaced_inherited_lock(tmp_path, capsule, kind):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    before = git(root, "rev-parse", "HEAD")
    journal = root / ".deployments/.full-transaction.json"
    raw = journal.read_bytes()
    wrapper = r'''
import fcntl, os, pathlib, subprocess, sys
root, dispatcher, capsule, kind = sys.argv[1:]
lock = pathlib.Path(root)/'.deployments/.deployment.lock'
source = lock if kind == 'replaced' else pathlib.Path(root).parent/'foreign-lock'
fd = os.open(source, os.O_RDWR | os.O_CREAT, 0o600)
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
if kind == 'replaced':
    lock.rename(lock.with_name('.previous-lock'))
    lock.touch()
raise SystemExit(subprocess.run([sys.executable, '-I', dispatcher, '--root', root,
    '--capsule', capsule, '--kind', 'full', '--recover'], pass_fds=(9,)).returncode)
'''
    result = command([sys.executable, "-c", wrapper, str(root), str(DISPATCH), str(capsule), kind],
                     env=env)
    assert result.returncode != 0
    assert "deployment lock identity mismatch" in result.stderr
    assert git(root, "rev-parse", "HEAD") == before
    assert journal.read_bytes() == raw
    assert not (root / "docker.log").exists()
    assert not list((root / ".deployments").glob("legacy-source-return-*"))


@pytest.fixture(scope="session")
def git_write_barrier(tmp_path_factory):
    """Pause the real Git process at a write to the deployment fixture."""
    directory = tmp_path_factory.mktemp("git-write-barrier")
    source = directory / "barrier.c"
    library = directory / "barrier.so"
    source.write_text(r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static ssize_t (*actual_write)(int, const void *, size_t);
static int paused;

ssize_t write(int fd, const void *buffer, size_t count) {
    if (!actual_write) actual_write = dlsym(RTLD_NEXT, "write");
    const char *stage = getenv("LEGACY_PAUSE_STAGE");
    const char *root = getenv("FAKE_DEPLOY_ROOT");
    if (!paused && stage && root) {
        char proc[64], path[PATH_MAX];
        snprintf(proc, sizeof(proc), "/proc/self/fd/%d", fd);
        ssize_t length = readlink(proc, path, sizeof(path) - 1);
        if (length > 0) {
            path[length] = 0;
            int in_root = !strncmp(path, root, strlen(root)) && path[strlen(root)] == '/';
            int backup = strstr(path, "/.deployments/legacy-source-return-") != NULL;
            int match = in_root && (
                (!strcmp(stage, "archive") && backup && strstr(path, "/source.tar")) ||
                (!strcmp(stage, "bundle") && backup && strstr(path, "/source.bundle")) ||
                (!strcmp(stage, "fetch") && strstr(path, "/.git/objects/")) ||
                !strcmp(stage, "checkout"));
            if (match) {
                paused = 1;
                int marker = open(getenv("LEGACY_PAUSE_MARKER"), O_WRONLY|O_CREAT|O_EXCL, 0600);
                if (marker >= 0) {
                    char value[PATH_MAX + 64];
                    int n = snprintf(value, sizeof(value), "%ld\n%s", (long)getpid(), path);
                    actual_write(marker, value, n);
                    close(marker);
                    while (access(getenv("LEGACY_PAUSE_RELEASE"), F_OK) != 0) usleep(10000);
                }
            }
        }
    }
    return actual_write(fd, buffer, count);
}
''')
    checked(["cc", "-shared", "-fPIC", "-o", str(library), str(source), "-ldl"])
    return library


@pytest.mark.parametrize("stage", ["archive", "bundle", "fetch", "checkout"])
def test_parent_death_does_not_release_lock_while_git_writes(tmp_path, capsule, git_write_barrier, stage):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    before = git(root, "rev-parse", "HEAD")
    lock = root / ".deployments/.deployment.lock"
    inode = lock.stat().st_ino
    data = (root / "data/terms.db").read_bytes()
    marker = tmp_path / "paused"
    release = tmp_path / "release"
    wrapper = tmp_path / "harness/bin/git"
    wrapper.write_text('''#!/usr/bin/python3
import os, sys
args = sys.argv[1:]
root = os.environ.get("FAKE_DEPLOY_ROOT", "")
stage = os.environ.get("LEGACY_PAUSE_STAGE", "")
repo = args[args.index("-C") + 1] if "-C" in args else ""
is_live = repo == root
is_bundle = ("bundle" in args and "create" in args and any(
    item.startswith(root + "/.deployments/legacy-source-return-") for item in args))
if ((stage == "archive" and is_live and "archive" in args)
    or (stage == "bundle" and is_bundle)
    or (stage == "fetch" and is_live and "fetch" in args)
    or (stage == "checkout" and is_live and "checkout" in args)):
    os.environ["LD_PRELOAD"] = os.environ["LEGACY_PAUSE_LIBRARY"]
os.execv("/usr/bin/git", ["git", *args])
''')
    wrapper.chmod(0o755)
    env.update(LEGACY_PAUSE_STAGE=stage, LEGACY_PAUSE_MARKER=str(marker),
               LEGACY_PAUSE_RELEASE=str(release), LEGACY_PAUSE_LIBRARY=str(git_write_barrier))
    args = [sys.executable, "-I", str(DISPATCH), "--root", str(root), "--capsule", str(capsule),
            "--kind", "full", "--recover"]
    process = subprocess.Popen(args, env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = None
    try:
        deadline = time.monotonic() + 30
        while True:
            if marker.exists() and len(marker.read_text().splitlines()) == 2:
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"dispatcher exited before {stage} write: {stdout!r} {stderr!r}")
            assert time.monotonic() < deadline, f"{stage} write barrier was not reached"
            time.sleep(0.01)
        pid, written_path = marker.read_text().splitlines()
        child = int(pid)
        written = Path(written_path)
        assert written.is_relative_to(root)
        if stage == "archive":
            assert written.name == "source.tar"
        elif stage == "bundle":
            # Git may publish via its own source.bundle.lock temporary file.
            assert written.name in {"source.bundle", "source.bundle.lock"}
        elif stage == "fetch":
            assert written.is_relative_to(root / ".git/objects")
        executable = Path(f"/proc/{child}/exe")
        assert hashlib.sha256(executable.read_bytes()).digest() == hashlib.sha256(
            Path("/usr/bin/git").read_bytes()
        ).digest()
        os.kill(process.pid, signal.SIGKILL)  # Only the dispatcher is killed.
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
        retry_env = {key: value for key, value in env.items() if not key.startswith("LEGACY_PAUSE_")}
        # Checkout can already have modified tracked files, so a competing
        # dispatcher may refuse on dirtiness before it reaches flock. Probe the
        # same lock separately to prove it remains held in all four stages.
        assert command(["flock", "-n", str(lock), "true"], env=retry_env).returncode != 0
        blocked = dispatch(root, retry_env, capsule, "full")
        assert blocked.returncode != 0, blocked.stdout + blocked.stderr
        assert ("deployment lock unavailable" in blocked.stderr or
                (stage == "checkout" and "dirty deployment checkout" in blocked.stderr))
        assert git(root, "rev-parse", "HEAD") == before
        assert (root / "data/terms.db").read_bytes() == data
        assert lock.stat().st_ino == inode
        assert not (root / "docker.log").exists()
    finally:
        release.write_text("continue")
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        if child is not None:
            deadline = time.monotonic() + 30
            while True:
                try:
                    state = Path(f"/proc/{child}/stat").read_text().split()[2]
                except FileNotFoundError:
                    break
                if state == "Z":
                    break
                assert time.monotonic() < deadline, f"{stage} Git child did not exit"
                time.sleep(0.01)
    deadline = time.monotonic() + 30
    while command(["flock", "-n", str(lock), "true"], env=retry_env).returncode != 0:
        assert time.monotonic() < deadline, f"{stage} Git process did not release the lock"
        time.sleep(0.01)
    result = dispatch(root, retry_env, capsule, "full")
    assert result.returncode == 0, result.stdout + result.stderr
    assert lock.stat().st_ino == inode
    assert (root / "data/terms.db").read_bytes() == data
    print(f"{stage}: lock held across Git write to {written.relative_to(root)}")


def test_repeated_git_interruption_blocks_current_writer_until_legacy_recovery(tmp_path, capsule, git_write_barrier):
    root, env, _payload = deployment(tmp_path, capsule, "full")
    # Install the actual current entrypoints in this isolated Git fixture. The
    # dispatcher must still restore the historical journal's original source.
    paths = (
        "deploy/vps-update.sh", "scripts/deployment_admission.py", "scripts/deployment_manifest.py",
        "scripts/runtime_update.py", "scripts/full_update.py", "scripts/component_runtime.py",
        "scripts/component_build.py", "scripts/product_manifest.py", "scripts/package_recovery_source.py",
    )
    for name in paths:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    with (root / "README.md").open("a", encoding="utf-8") as stream:
        stream.write("\ncurrent admission fixture\n")
    git(root, "add", "--", *paths, "README.md")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture current admission")
    source = git(root, "rev-parse", "HEAD")
    lock = root / ".deployments/.deployment.lock"
    journal = root / ".deployments/.full-transaction.json"
    journal_before = journal.read_bytes()
    data = (root / "data/terms.db").read_bytes()
    wrapper = tmp_path / "harness/bin/git"
    wrapper.write_text('''#!/usr/bin/python3
import os, sys
args = sys.argv[1:]
root = os.environ.get("FAKE_DEPLOY_ROOT", "")
repo = args[args.index("-C") + 1] if "-C" in args else ""
stage = os.environ.get("LEGACY_PAUSE_STAGE")
if repo == root and ((stage == "fetch" and "fetch" in args) or (stage == "archive" and "archive" in args)):
    os.environ["LD_PRELOAD"] = os.environ["LEGACY_PAUSE_LIBRARY"]
os.execv("/usr/bin/git", ["git", *args])
''', encoding="utf-8")
    wrapper.chmod(0o755)

    def current_writer():
        command = ('exec 9>>"$1"; shift; exec "$@"')
        return subprocess.run(
            ["sh", "-c", command, "fixture", str(lock), sys.executable, "-I", "-B",
             str(root / "scripts/component_runtime.py"), "--root", str(root), "adopt"],
            cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False,
        )

    for attempt, stage in enumerate(("archive", "fetch")):
        marker, release = tmp_path / f"paused-{attempt}", tmp_path / f"release-{attempt}"
        env.update(LEGACY_PAUSE_STAGE=stage, LEGACY_PAUSE_MARKER=str(marker),
                   LEGACY_PAUSE_RELEASE=str(release), LEGACY_PAUSE_LIBRARY=str(git_write_barrier))
        args = [sys.executable, "-I", str(DISPATCH), "--root", str(root), "--capsule", str(capsule),
                "--kind", "full", "--recover"]
        process = subprocess.Popen(args, env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = None
        try:
            deadline = time.monotonic() + 30
            while not marker.exists() or len(marker.read_text().splitlines()) != 2:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"dispatcher exited before {stage} Git write: {stdout!r} {stderr!r}")
                assert time.monotonic() < deadline
                time.sleep(0.01)
            pid, written_path = marker.read_text().splitlines()
            child = int(pid)
            if stage == "fetch":
                assert Path(written_path).is_relative_to(root / ".git/objects")
            else:
                assert Path(written_path).name == "source.tar"
            assert hashlib.sha256(Path(f"/proc/{child}/exe").read_bytes()).digest() == hashlib.sha256(
                Path("/usr/bin/git").read_bytes()).digest()
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            assert process.returncode == -signal.SIGKILL
            blocked = current_writer()
            assert blocked.returncode != 0
            assert "deployment lock" in blocked.stderr
            assert journal.read_bytes() == journal_before
        finally:
            release.write_text("continue", encoding="ascii")
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
        assert child is not None
        deadline = time.monotonic() + 30
        while True:
            try:
                state = Path(f"/proc/{child}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        for key in ("LEGACY_PAUSE_STAGE", "LEGACY_PAUSE_MARKER", "LEGACY_PAUSE_RELEASE", "LEGACY_PAUSE_LIBRARY"):
            env.pop(key)
        blocked = current_writer()
        assert blocked.returncode != 0
        assert "legacy full transaction requires recovery" in blocked.stderr
        assert journal.read_bytes() == journal_before
        assert (root / "data/terms.db").read_bytes() == data
        assert git(root, "rev-parse", "HEAD") == source
        assert not (root / "docker.log").exists()

    recovered = dispatch(root, env, capsule, "full")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not journal.exists()
    assert (root / "data/terms.db").read_bytes() == data
