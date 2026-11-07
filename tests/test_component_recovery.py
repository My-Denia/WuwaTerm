from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import test_component_runtime as component_tests

from scripts import component_recovery, component_runtime
from test_product_manifest import DEPLOYMENT_ID


SOURCE = Path(__file__).resolve().parents[1]
TRANSACTION = DEPLOYMENT_ID


@pytest.fixture
def journal(tmp_path: Path) -> dict:
    return component_tests.journal.__wrapped__(tmp_path)


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "PYTHON"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *arguments],
                            env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=True)
    return result.stdout.strip()


def create_source(root: Path, *, dispatcher_probe: bool = False,
                  enable_fixture_dispatch: bool = False) -> tuple[Path, str, str]:
    """Make a new, isolated Git fixture; do not fetch or export other refs."""
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Synthetic fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    for name in component_runtime.TOOL_PATHS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        original = SOURCE / name
        if original.is_file():
            target.write_bytes(original.read_bytes())
        elif name == "deploy/product-update.sh":
            target.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        else:
            raise AssertionError(f"missing fixture source {name}")
    if dispatcher_probe:
        # This only probes which verified source runs and how it was invoked.
        # It does not emulate or claim service recovery.
        path = root / "scripts" / "component_runtime.py"
        text = path.read_text(encoding="utf-8")
        marker = 'if __name__ == "__main__":\n'
        assert marker in text
        probe = (
            "CAPSULE_RECOVERY_PROTOCOL = 1\n"
            "if __name__ == '__main__' and '--capsule-source-tool' in sys.argv:\n"
            "    print(json.dumps({'fixture': 'source-A', 'code_root': str(CODE_ROOT), "
            "'argv': sys.argv[1:], 'fd9': os.fstat(9).st_ino, "
            "'pythonpath': os.environ.get('PYTHONPATH'), 'git_index': os.environ.get('GIT_INDEX_FILE'), "
            "'canary': os.environ.get('WUWATERM_TEST_CANARY')}))\n"
            "    raise SystemExit(37)\n\n"
        )
        path.write_text(text.replace(marker, probe + marker), encoding="utf-8")
    if enable_fixture_dispatch:
        assert dispatcher_probe
        path = root / "scripts" / "component_recovery.py"
        text = path.read_text(encoding="utf-8")
        assert "\nRECOVERY_EXECUTION_READY = False\n" in text
        path.write_text(text.replace("\nRECOVERY_EXECUTION_READY = False\n", "\nRECOVERY_EXECUTION_READY = True\n", 1),
                        encoding="utf-8")
    documentation = root / "docs" / "fixture.txt"
    documentation.parent.mkdir()
    documentation.write_text("The complete tracked tree belongs in the source capsule.\n", encoding="utf-8")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Synthetic tracked source")
    deployments = root / ".deployments"
    deployments.mkdir(mode=0o700)
    (deployments / ".deployment.lock").touch(mode=0o600)
    return root, git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "HEAD^{tree}")


def locked(root: Path, *args: str, lock: bool = True, poison: bool = False) -> subprocess.CompletedProcess[str]:
    environment = clean_env()
    if poison:
        environment.update(PYTHONPATH="/nonexistent/untrusted", GIT_INDEX_FILE="/nonexistent/untrusted")
    command = list(args)
    if lock:
        command = ["bash", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
                   "fixture-lock", str(root / ".deployments" / ".deployment.lock"), *command]
    return subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, check=False)


def prepare(root: Path, commit: str, tree: str, *, lock: bool = True) -> subprocess.CompletedProcess[str]:
    code = (
        "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "print(json.dumps(m.prepare_source(r,sys.argv[2],sys.argv[3]),sort_keys=True))"
    )
    return locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, lock=lock)


def add_journal(root: Path, source: dict, journal: dict) -> None:
    journal["source"] = source
    ops = journal["target"]["manifest"]["operations"]
    ops.update(source_commit=source["commit"], source_tree=source["tree"],
               source_tool_sha256=source["source_tool_sha256"])
    journal["pointers"]["target"][".deploy_commit"]["bytes"] = source["commit"] + "\n"
    raw = (json.dumps(journal["target"]["manifest"], ensure_ascii=True, sort_keys=True,
                      indent=2, allow_nan=False) + "\n").encode("ascii")
    journal["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    component_runtime.validate_journal(journal)
    (root / ".deployments" / ".component-runtime-transaction.json").write_text(
        json.dumps(journal), encoding="utf-8")


def invoke(root: Path, source: dict, transaction: str = TRANSACTION) -> subprocess.CompletedProcess[str]:
    script = root / source["bootstrap"]["path"] / "component_recovery.py"
    return locked(root, sys.executable, "-I", "-B", str(script), "--root", str(root),
                  "--transaction-id", transaction, poison=True)


def saved_hashes(root: Path, source: dict) -> dict[str, str]:
    capsule = root / source["capsule"]["path"]
    bootstrap = root / source["bootstrap"]["path"]
    paths = [*(capsule / name for name in ("manifest.json", "source.tar", "source.bundle")),
             *(bootstrap / name for name in source["bootstrap"]["files_sha256"])]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_clean_tracked_source_is_packaged_durably_and_reused(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    result = prepare(root, commit, tree)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    assert source["commit"] == commit
    assert source["tree"] == tree
    assert source["source_tool_sha256"] == component_runtime.source_tool_identity(
        commit, tree, source["tool_bytes_sha256"])
    source_dir = root / source["capsule"]["path"]
    manifest = json.loads((source_dir / "manifest.json").read_text())
    assert manifest["source_commit"] == commit
    assert "docs/fixture.txt" in manifest["files"]
    for name, key in (("manifest.json", "manifest_sha256"), ("source.tar", "archive_sha256"),
                      ("source.bundle", "bundle_sha256")):
        assert hashlib.sha256((source_dir / name).read_bytes()).hexdigest() == source["capsule"][key]
    bootstrap = root / source["bootstrap"]["path"]
    for name, digest in source["bootstrap"]["files_sha256"].items():
        assert hashlib.sha256((bootstrap / name).read_bytes()).hexdigest() == digest
        assert (bootstrap / name).read_bytes() == (root / "scripts" / name).read_bytes()
    second = prepare(root, commit, tree)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == source
    before = saved_hashes(root, source)
    code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import deployment_admission as a; "
        "a.admit_component(r,operation='adopt',code_root=r)"
    )
    refused = locked(root, sys.executable, "-I", "-B", "-c", code, str(root))
    assert refused.returncode != 0
    assert "runtime saved-source recovery is not ready" in refused.stderr
    assert saved_hashes(root, source) == before


def test_interrupted_source_staging_does_not_block_retry(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "m._publish_directory=lambda *a: (_ for _ in ()).throw(m.RecoveryError('interrupted')); "
        "m.prepare_source(r,sys.argv[2],sys.argv[3])"
    )
    interrupted = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree)
    assert interrupted.returncode != 0
    entries = list((root / ".deployments").iterdir())
    identity = component_runtime.source_tool_identity(commit, tree, component_runtime.tool_bytes_identity(root))
    stages = [path for path in entries if path.name.startswith(".component-source-stage-")]
    assert len(stages) == 1 and stages[0].name.startswith(".component-source-stage-" + identity + "-")
    stage_inode = stages[0].stat().st_ino
    assert not any(path.name.startswith("component-source-") for path in entries)
    retried = prepare(root, commit, tree)
    assert retried.returncode == 0, retried.stderr
    assert (root / json.loads(retried.stdout)["capsule"]["path"] / "source.tar").is_file()
    assert stages[0].stat().st_ino == stage_inode


def test_interrupted_bootstrap_staging_does_not_block_retry(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "original=m._publish_directory; "
        "m._publish_directory=lambda a,b: (_ for _ in ()).throw(m.RecoveryError('interrupted')) "
        "if b.name.startswith('component-bootstrap-') else original(a,b); "
        "m.prepare_source(r,sys.argv[2],sys.argv[3])"
    )
    interrupted = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree)
    assert interrupted.returncode != 0
    entries = list((root / ".deployments").iterdir())
    identity = component_runtime.source_tool_identity(commit, tree, component_runtime.tool_bytes_identity(root))
    stages = [path for path in entries if path.name.startswith(".component-bootstrap-stage-")]
    assert len(stages) == 1 and stages[0].name.startswith(".component-bootstrap-stage-" + identity + "-")
    stage_inode = stages[0].stat().st_ino
    assert not any(path.name.startswith("component-bootstrap-") for path in entries)
    retried = prepare(root, commit, tree)
    assert retried.returncode == 0, retried.stderr
    assert (root / json.loads(retried.stdout)["bootstrap"]["path"] / "component_recovery.py").is_file()
    assert stages[0].stat().st_ino == stage_inode


@pytest.mark.parametrize("kind", ["source", "bootstrap", "compose"])
def test_reused_source_publication_syncs_after_rename_before_accepting(tmp_path: Path, kind: str) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    identity = component_runtime.source_tool_identity(commit, tree, component_runtime.tool_bytes_identity(root))
    final = root / ".deployments" / ({"source": "component-source-" + identity,
                                      "bootstrap": "component-bootstrap-" + identity,
                                      "compose": "component-compose-" + digest}[kind])
    if kind == "bootstrap":
        interrupted = (
            "import pathlib,sys; r=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(r/'scripts')); "
            "import component_recovery as m; old=m._publish_directory; "
            "m._publish_directory=lambda a,b: (_ for _ in ()).throw(m.RecoveryError('interrupted')) "
            "if b.name.startswith('component-bootstrap-') else old(a,b); "
            "m.prepare_source(r,sys.argv[2],sys.argv[3])"
        )
        assert locked(root, sys.executable, "-I", "-B", "-c", interrupted, str(root), commit, tree).returncode != 0
    elif kind == "compose":
        assert prepare(root, commit, tree).returncode == 0
    stop_code = r'''
import os,pathlib,signal,sys
r=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(r/'scripts')); import component_recovery as m
final=pathlib.Path(sys.argv[4]); original=os.fsync
def stop(fd):
 try: name=os.readlink('/proc/self/fd/'+str(fd))
 except OSError: name=''
 if name==str(r/'.deployments') and final.exists(): os.kill(os.getpid(),signal.SIGSTOP)
 return original(fd)
os.fsync=stop
m.prepare_source(r,sys.argv[2],sys.argv[3],compose_path=pathlib.Path(sys.argv[5]) if sys.argv[6]=='compose' else None)
'''
    command = ["bash", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
               "fixture-lock", str(root / ".deployments/.deployment.lock"), sys.executable, "-I", "-B", "-c",
               stop_code, str(root), commit, tree, str(final), str(binary), kind]
    child = subprocess.Popen(command, cwd=root, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                assert os.WIFSTOPPED(status), "publication exited before the unsynced boundary"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("publication did not reach its unsynced boundary")
        assert final.exists()
        inode = final.stat().st_ino
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)
    eio_code = r'''
import errno,pathlib,sys
r=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(r/'scripts')); import component_recovery as m
expected=pathlib.Path(sys.argv[4]); original=m._sync_reused_publication
def injected(path,verify):
 if path!=expected: return original(path,verify)
 old=m.capsule.sync_directory
 def fail(_): raise OSError(errno.EIO,'synthetic directory sync failure')
 m.capsule.sync_directory=fail
 try: return original(path,verify)
 finally: m.capsule.sync_directory=old
m._sync_reused_publication=injected
try: m.prepare_source(r,sys.argv[2],sys.argv[3],compose_path=pathlib.Path(sys.argv[5]) if sys.argv[6]=='compose' else None)
except m.RecoveryError: sys.exit(47)
raise AssertionError('unsynced publication was accepted')
'''
    failed = locked(root, sys.executable, "-I", "-B", "-c", eio_code, str(root), commit, tree,
                    str(final), str(binary), kind)
    assert failed.returncode == 47, failed.stderr
    assert final.stat().st_ino == inode
    normal_code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(r/'scripts')); "
        "import component_recovery as m; m.prepare_source(r,sys.argv[2],sys.argv[3],"
        "compose_path=pathlib.Path(sys.argv[4]) if sys.argv[5]=='compose' else None)"
    )
    retried = locked(root, sys.executable, "-I", "-B", "-c", normal_code, str(root), commit, tree, str(binary), kind)
    assert retried.returncode == 0, retried.stderr
    assert final.stat().st_ino == inode


def test_source_publication_does_not_replace_an_existing_directory(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "destination"
    stage.mkdir()
    destination.mkdir()
    (destination / "unknown").write_text("preserve", encoding="ascii")
    with pytest.raises(component_recovery.RecoveryError, match="already exists"):
        component_recovery._publish_directory(stage, destination)
    assert (destination / "unknown").read_text(encoding="ascii") == "preserve"
    assert stage.is_dir()


def test_direct_producers_refuse_before_creating_attempts(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    before = sorted(path.name for path in (root / ".deployments").iterdir())
    commands = [
        [str(root / "scripts/component_build.py"), "--root", str(root), "build",
         "--source-commit", commit, "--source-tree", tree],
        [str(root / "scripts/component_wheel_build.py"), "--root", str(root),
         "--operations-commit", commit, "--operations-tree", tree,
         "--telegram-repo", str(root), "--telegram-commit", commit, "--telegram-tree", tree,
         "--core-wheel", str(root / "unused.whl"), "--core-artifact", str(root / "unused.zip"),
         "--dependencies", str(root)],
    ]
    for command in commands:
        result = locked(root, sys.executable, "-I", "-B", *command)
        assert result.returncode != 0
        assert "component builds are unavailable until cancellation is supported" in result.stderr
        assert sorted(path.name for path in (root / ".deployments").iterdir()) == before


def test_compose_is_copied_and_bound_to_saved_source(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    code = (
        "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "s=m.prepare_source(r,sys.argv[2],sys.argv[3],compose_path=pathlib.Path(sys.argv[4])); "
        "print(json.dumps(s,sort_keys=True))"
    )
    result = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, str(binary))
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    saved = root / source["compose"]["path"]
    assert source["compose"]["sha256"] == digest
    assert saved.read_bytes() == binary.read_bytes()
    assert Path(str(saved) + ".sha256").read_text(encoding="ascii") == f"{digest}  {saved.name}\n"
    assert component_recovery._requested_compose(root, source, binary) == saved
    binary.chmod(0o700)
    binary.write_bytes(b"#!/bin/sh\nexit 9\n")
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest
    with pytest.raises(component_recovery.RecoveryError, match="requested Compose executable differs"):
        component_recovery._requested_compose(root, source, binary)
    saved.chmod(0o700)
    saved.write_bytes(b"changed")
    with pytest.raises(component_recovery.RecoveryError, match="saved Compose executable differs"):
        component_recovery._verify_bound_compose(root, source)


def test_interrupted_compose_receipt_publication_retries_without_overwrite(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "original=m._publish_directory; "
        "m._publish_directory=lambda a,b: (_ for _ in ()).throw(m.RecoveryError('interrupted')) "
        "if b.name.startswith('component-compose-') and b.name.endswith('.sha256') else original(a,b); "
        "m.prepare_source(r,sys.argv[2],sys.argv[3],compose_path=pathlib.Path(sys.argv[4]))"
    )
    interrupted = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, str(binary))
    assert interrupted.returncode != 0
    destination = root / ".deployments" / ("component-compose-" + digest)
    assert destination.read_bytes() == binary.read_bytes()
    assert not Path(str(destination) + ".sha256").exists()
    original_inode = destination.stat().st_ino
    retry = (
        "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "print(json.dumps(m.prepare_source(r,sys.argv[2],sys.argv[3],"
        "compose_path=pathlib.Path(sys.argv[4]))))"
    )
    result = locked(root, sys.executable, "-I", "-B", "-c", retry, str(root), commit, tree, str(binary))
    assert result.returncode == 0, result.stderr
    assert destination.stat().st_ino == original_inode
    assert json.loads(result.stdout)["compose"]["sha256"] == digest
    assert Path(str(destination) + ".sha256").is_file()


def test_unknown_saved_compose_is_not_overwritten(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    destination = root / ".deployments" / ("component-compose-" + digest)
    destination.write_bytes(b"unrelated")
    destination.chmod(0o500)
    code = (
        "import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "m.prepare_source(r,sys.argv[2],sys.argv[3],compose_path=pathlib.Path(sys.argv[4]))"
    )
    result = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, str(binary))
    assert result.returncode != 0
    assert "existing saved Compose executable differs" in result.stderr
    assert destination.read_bytes() == b"unrelated"
    assert not Path(str(destination) + ".sha256").exists()


def test_missing_lock_and_dirty_tracked_source_fail_before_writes(tmp_path: Path) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    missing = prepare(root, commit, tree, lock=False)
    assert missing.returncode != 0
    assert "lock FD9" in missing.stderr
    assert sorted(path.name for path in (root / ".deployments").iterdir()) == [".deployment.lock"]
    (root / "docs" / "fixture.txt").write_text("changed\n", encoding="utf-8")
    dirty = prepare(root, commit, tree)
    assert dirty.returncode != 0
    assert "not clean" in dirty.stderr
    assert sorted(path.name for path in (root / ".deployments").iterdir()) == [".deployment.lock"]


@pytest.mark.parametrize("kind", ["config", "promisor", "alternate"])
def test_partial_or_alternate_git_storage_refuses_before_packaging(tmp_path: Path, kind: str) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    if kind == "config":
        git(root, "config", "remote.fixture.promisor", "true")
    elif kind == "promisor":
        pack = root / ".git/objects/pack"
        pack.mkdir(exist_ok=True)
        (pack / "fixture.promisor").write_bytes(b"")
    else:
        info = root / ".git/objects/info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_text("/nonexistent/fixture-objects\n", encoding="ascii")
    before = sorted(path.name for path in (root / ".deployments").iterdir())
    result = prepare(root, commit, tree)
    assert result.returncode != 0
    assert "partial-clone source is unsupported" in result.stderr or "alternate Git object storage is unsupported" in result.stderr
    assert sorted(path.name for path in (root / ".deployments").iterdir()) == before


def test_safe_git_preserves_sudo_uid_and_excludes_inherited_git_poison(tmp_path: Path) -> None:
    root, _, _ = create_source(tmp_path / "source")
    code = (
        "import json,os,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "os.environ['SUDO_UID']='1234'; os.environ['GIT_INDEX_FILE']='/nonexistent/untrusted'; "
        "raw=m.safe_git(r,'-c','alias.fixture=!env','fixture',lock_root=r).decode(); "
        "env=dict(line.split('=',1) for line in raw.splitlines() if '=' in line); "
        "print(json.dumps({k:env.get(k) for k in ('SUDO_UID','GIT_INDEX_FILE','GIT_NO_LAZY_FETCH')}))"
    )
    result = locked(root, sys.executable, "-I", "-B", "-c", code, str(root))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"SUDO_UID": "1234", "GIT_INDEX_FILE": None, "GIT_NO_LAZY_FETCH": "1"}


def test_tamper_and_wrong_transaction_fail_before_saved_source_execution(tmp_path: Path, journal: dict) -> None:
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True)
    result = prepare(root, commit, tree)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    add_journal(root, source, journal)
    wrong = invoke(root, source, "20261106T120000Z-ffffffffffffffffffffffffffffffff")
    assert wrong.returncode != 0
    assert "journal does not match" in wrong.stderr
    packager = root / source["bootstrap"]["path"] / "package_recovery_source.py"
    with packager.open("ab") as stream:
        stream.write(b"\nraise RuntimeError('tampered bootstrap was executed')\n")
    corrupted = invoke(root, source)
    assert corrupted.returncode != 0
    assert "bootstrap file differs" in corrupted.stderr
    assert "tampered bootstrap was executed" not in corrupted.stderr


@pytest.mark.parametrize("kind", ["runtime", "versionprobe", "sourcebuild", "wheelbuild"])
def test_normal_cli_refuses_tampered_bootstrap_before_it_can_execute(
    tmp_path: Path, journal: dict, kind: str,
) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    prepared = prepare(root, commit, tree)
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    if kind == "runtime":
        add_journal(root, source, journal)
        arguments = ["recover", "--transaction-id", TRANSACTION, "--compose-executable", "/bin/false"]
    elif kind == "versionprobe":
        record = {"schema_version": 1, "kind": "legacy-core-version", "probe_id": TRANSACTION,
                  "status": "active", "phase": "prepared", "image_id": "sha256:" + "1" * 64,
                  "image_revision": "b" * 40, "container_name": "wuwaterm-adopt-probe-" + TRANSACTION,
                  "container_id": None, "installed_core_version": None, "output_sha256": None,
                  "failure": None, "source": source, "daemon_id": "2" * 64, "predecessor_sha256": None}
        directory = root / ".deployments/component-adopt-probes"
        directory.mkdir()
        (directory / (TRANSACTION + ".json")).write_text(json.dumps(record), encoding="utf-8")
        arguments = ["recover-probe", "--probe-id", TRANSACTION, "--compose-executable", "/bin/false"]
    else:
        record = {"schema_version": 2, "build_id": TRANSACTION, "status": "active", "recovery_source": source}
        directory_name = "component-build-attempts" if kind == "sourcebuild" else "component-wheel-build-attempts"
        directory = root / ".deployments" / directory_name
        directory.mkdir()
        (directory / (TRANSACTION + ".json")).write_text(json.dumps(record), encoding="utf-8")
        arguments = ["cancel-build", "--kind", "source" if kind == "sourcebuild" else "wheel",
                     "--build-id", TRANSACTION]
    script = root / source["bootstrap"]["path"] / "component_recovery.py"
    marker = tmp_path / "tampered-bootstrap-executed"
    contents = script.read_text(encoding="utf-8")
    script.write_text(contents.replace("from __future__ import annotations\n",
                                     f"from __future__ import annotations\nopen({str(marker)!r}, 'w').write('executed')\n", 1),
                      encoding="utf-8")
    result = locked(root, sys.executable, "-I", "-B", str(root / "scripts/component_runtime.py"),
                    "--root", str(root), *arguments)
    assert result.returncode != 0
    assert "bootstrap cannot be verified" in result.stderr
    assert not marker.exists()
    if kind in ("sourcebuild", "wheelbuild"):
        with (root / ".deployments/.deployment.lock").open("r+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = subprocess.run(
                ["sh", str(root / "deploy/product-update.sh"), "--root", str(root), *arguments],
                capture_output=True, text=True, env=clean_env(), timeout=30, check=False,
            )
        assert busy.returncode != 0
        assert "builder bootstrap cannot be verified" in busy.stderr
        assert "already in progress" not in busy.stderr
        assert not marker.exists()


def test_saved_source_is_verified_after_checkout_advances_without_importing_new_source(tmp_path: Path, journal: dict) -> None:
    root, commit_a, tree_a = create_source(tmp_path / "source", dispatcher_probe=True)
    result = prepare(root, commit_a, tree_a)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    saved_before = saved_hashes(root, source)
    add_journal(root, source, journal)
    (root / ".deploy_commit").write_text(commit_a + "\n", encoding="ascii")
    runtime_b = root / "scripts" / "component_runtime.py"
    runtime_b.write_text("raise RuntimeError('source B imported')\n", encoding="ascii")
    git(root, "add", "scripts/component_runtime.py")
    git(root, "commit", "--quiet", "-m", "Advance synthetic source")
    commit_b = git(root, "rev-parse", "HEAD")
    pointer_before = (root / ".deploy_commit").read_bytes()
    invoked = invoke(root, source)
    assert invoked.returncode != 0
    assert invoked.stdout == ""
    assert "component recovery execution is not yet enabled" in invoked.stderr
    assert saved_hashes(root, source) == saved_before
    assert "source B imported" not in invoked.stderr
    assert git(root, "rev-parse", "HEAD") == commit_b
    assert runtime_b.read_text() == "raise RuntimeError('source B imported')\n"
    assert (root / ".deploy_commit").read_bytes() == pointer_before
    assert not (root / ".deploy_current").exists()


def test_public_saved_launcher_acquires_fd9_without_importing_checkout(tmp_path: Path, journal: dict) -> None:
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True)
    prepared = prepare(root, commit, tree)
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    saved_before = saved_hashes(root, source)
    add_journal(root, source, journal)
    (root / "scripts" / "component_runtime.py").write_text("raise RuntimeError('source B imported')\n")
    script = root / source["bootstrap"]["path"] / "component_recovery.py"
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", "runtime",
         "--id", TRANSACTION], capture_output=True, text=True, env=clean_env(), check=False,
    )
    assert result.returncode != 0
    assert "component recovery execution is not yet enabled" in result.stderr
    assert saved_hashes(root, source) == saved_before
    assert "source B imported" not in result.stderr
    assert "lock FD9" not in result.stderr


def test_public_launcher_dispatches_saved_a_with_bound_compose_over_poisoned_b(tmp_path: Path, journal: dict) -> None:
    # This fixture checks dispatch and bindings only; its dispatcher deliberately
    # exits without claiming to reconcile a real transaction.
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True, enable_fixture_dispatch=True)
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    code = (
        "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "print(json.dumps(m.prepare_source(r,sys.argv[2],sys.argv[3],"
        "compose_path=pathlib.Path(sys.argv[4])),sort_keys=True))"
    )
    prepared = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, str(binary))
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    journal["schema_version"] = 3
    add_journal(root, source, journal)
    (root / "scripts/component_runtime.py").write_text("raise RuntimeError('source B imported')\n")
    git(root, "add", "scripts/component_runtime.py")
    git(root, "commit", "--quiet", "-m", "Advance synthetic source")
    head_b = git(root, "rev-parse", "HEAD")
    script = root / source["bootstrap"]["path"] / "component_recovery.py"
    command = [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", "runtime",
               "--id", TRANSACTION, "--compose-executable", str(binary)]
    env = clean_env()
    env.update(PYTHONPATH="/nonexistent/untrusted", GIT_INDEX_FILE="/nonexistent/untrusted",
               WUWATERM_TEST_CANARY="must-not-propagate")
    result = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 37, result.stderr
    output = json.loads(result.stdout)
    assert output["fixture"] == "source-A"
    assert output["argv"][output["argv"].index("--compose-executable") + 1] == str(root / source["compose"]["path"])
    assert output["pythonpath"] is None and output["git_index"] is None and output["canary"] is None
    assert "must-not-propagate" not in result.stdout + result.stderr
    assert git(root, "rev-parse", "HEAD") == head_b
    binary.chmod(0o700)
    binary.write_bytes(b"#!/bin/sh\nexit 9\n")
    refused = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    assert refused.returncode != 0
    assert "requested Compose executable differs" in refused.stderr
    assert refused.stdout == ""


@pytest.mark.parametrize("completed", [False, True])
def test_public_launcher_selects_only_the_recorded_legacy_probe(tmp_path: Path, completed: bool) -> None:
    # This verifies actual bootstrap staging preflight and source selection;
    # the fixture dispatcher deliberately does not claim Docker cleanup.
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True, enable_fixture_dispatch=True)
    binary = tmp_path / "compose"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    Path(str(binary) + ".sha256").write_text(f"{digest}  {binary.name}\n", encoding="ascii")
    code = (
        "import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
        "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
        "print(json.dumps(m.prepare_source(r,sys.argv[2],sys.argv[3],"
        "compose_path=pathlib.Path(sys.argv[4])),sort_keys=True))"
    )
    prepared = locked(root, sys.executable, "-I", "-B", "-c", code, str(root), commit, tree, str(binary))
    assert prepared.returncode == 0, prepared.stderr
    source = json.loads(prepared.stdout)
    probe = {"schema_version": 1, "kind": "legacy-core-version", "probe_id": TRANSACTION,
             "status": "active", "phase": "prepared", "image_id": "sha256:" + "1" * 64,
             "image_revision": "b" * 40, "container_name": "wuwaterm-adopt-probe-" + TRANSACTION,
             "container_id": None, "installed_core_version": None, "output_sha256": None,
             "failure": None, "source": source, "daemon_id": "2" * 64, "predecessor_sha256": None}
    if completed:
        probe.update(phase="cleanup", container_id="3" * 64, installed_core_version="0.6.1",
                     output_sha256="4" * 64)
    directory = root / ".deployments/component-adopt-probes"
    directory.mkdir()
    path = directory / (TRANSACTION + ".json")
    path.write_text(json.dumps(probe), encoding="utf-8")
    published = {**probe, "phase": "completed" if completed else "create"}
    if completed:
        published["status"] = "completed"
    pending = tmp_path / "pending-probe.json"
    pending.write_text(json.dumps(published), encoding="utf-8")
    interrupt = r'''import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
real_replace=os.replace
def replace(src,dst,*args,**kwargs):
 result=real_replace(src,dst,*args,**kwargs)
 if str(dst).endswith('.json'): os.kill(os.getpid(),signal.SIGSTOP)
 return result
os.replace=replace
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
    component_tests._kill_on_stop(component_tests._locked_runtime_command(root, interrupt, str(pending)))
    assert component_runtime.pending_adopt_probe_staging(root) == TRANSACTION
    inspection = component_runtime.inspect_deployment(root)
    assert inspection["probe_recovery"][0]["staging"] == "pending-unverified"
    before = path.read_bytes()
    (root / "scripts/component_runtime.py").write_text("raise RuntimeError('source B imported')\n")
    git(root, "add", "scripts/component_runtime.py")
    git(root, "commit", "--quiet", "-m", "Advance synthetic source")
    script = root / source["bootstrap"]["path"] / "component_recovery.py"
    command = [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", "versionprobe",
               "--id", TRANSACTION, "--compose-executable", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, env=clean_env(), check=False)
    assert result.returncode == 37, result.stderr
    output = json.loads(result.stdout)
    assert output["fixture"] == "source-A"
    assert "capsule-recover-probe" in output["argv"]
    assert path.read_bytes() == before
    assert component_runtime.pending_adopt_probe_staging(root) == TRANSACTION
    probe["kind"] = "state-plan"
    path.write_text(json.dumps(probe), encoding="utf-8")
    refused = subprocess.run(command, capture_output=True, text=True, env=clean_env(), check=False)
    assert refused.returncode != 0
    assert "does not match the requested version probe" in refused.stderr
    assert refused.stdout == ""


def test_recovery_refuses_until_source_enables_protocol(tmp_path: Path, journal: dict) -> None:
    root, commit, tree = create_source(tmp_path / "source")
    result = prepare(root, commit, tree)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    saved_before = saved_hashes(root, source)
    add_journal(root, source, journal)
    invoked = invoke(root, source)
    assert invoked.returncode != 0
    assert "no enabled capsule recovery protocol" in invoked.stderr
    assert saved_hashes(root, source) == saved_before


def test_saved_parser_rejects_invalid_complete_journal(tmp_path: Path, journal: dict) -> None:
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True)
    result = prepare(root, commit, tree)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    add_journal(root, source, journal)
    journal["pointers"]["target"][".deploy_current"]["bytes"] = "bad\n"
    (root / ".deployments" / ".component-runtime-transaction.json").write_text(
        json.dumps(journal), encoding="utf-8")
    invoked = invoke(root, source)
    assert invoked.returncode != 0
    assert "saved source rejected the complete journal" in invoked.stderr
    assert invoked.stdout == ""


@pytest.mark.parametrize("filename", ["source.tar", "source.bundle", "manifest.json"])
def test_capsule_corruption_is_refused(tmp_path: Path, filename: str, journal: dict) -> None:
    root, commit, tree = create_source(tmp_path / "source", dispatcher_probe=True)
    result = prepare(root, commit, tree)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    add_journal(root, source, journal)
    with (root / source["capsule"]["path"] / filename).open("ab") as stream:
        stream.write(b"tampered")
    invoked = invoke(root, source)
    assert invoked.returncode != 0
    assert "digest differs" in invoked.stderr
