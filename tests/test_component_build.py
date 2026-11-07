from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading

import pytest

from scripts import component_build as build
from scripts import product_manifest as manifest


REPO = Path(__file__).resolve().parents[1]


def clean_env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}


def git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
                          env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def export_source(repo: Path, tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "source-export"
    root.mkdir()
    archive = git(repo, "archive", "--format=tar", "HEAD").stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        members = source.getmembers()
        assert all(item.isfile() or item.isdir() for item in members)
        source.extractall(root, filter="data")
        paths = [item.name for item in members if item.isfile()]
    if repo == REPO:
        # The candidate is staged and may also have owned working-tree edits.
        # Exercise that whole selected tree, not HEAD plus a few hand-picked
        # scripts; otherwise capsule/build evidence would describe older docs
        # and tests. Untracked additions are restricted to the named work.
        assert not git(repo, "diff", "--name-only", "--diff-filter=D", "-z", "HEAD").stdout
        changed = git(repo, "diff", "--name-only", "--diff-filter=ACMRT", "-z", "HEAD").stdout
        additions = git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--",
                        *build.runtime.TOOL_PATHS,
                        "tests/test_component_adopt_docker.py", "tests/test_component_transaction.py",
                        "tests/test_component_wheel_build.py", "tests/test_component_state.py",
                        "tests/test_component_build_isolation.py", "tests/test_component_full.py",
                        "tests/test_component_full_db.py", "tests/test_component_full_docker.py",
                        "tests/test_component_prepare.py").stdout
        for encoded in (changed + additions).split(b"\0"):
            if not encoded:
                continue
            relative = encoded.decode("utf-8")
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / relative, destination)
            if relative not in paths:
                paths.append(relative)
    for relative in ("scripts/product_manifest.py", "scripts/component_runtime.py", "scripts/component_build.py",
                     "scripts/deployment_admission.py", "scripts/deployment_manifest.py", "scripts/full_update.py",
                     "scripts/runtime_update.py", "scripts/package_recovery_source.py", "deploy/vps-update.sh",
                     "scripts/component_observe.py", "scripts/component_recovery.py", "deploy/product-update.sh",
                     "scripts/component_transaction.py", "scripts/component_wheel_build.py", "scripts/component_state.py"):
        destination = root / relative
        if (repo / relative).exists() and (repo == REPO or not destination.exists()):
            destination.parent.mkdir(parents=True, exist_ok=True)
            was_missing = not destination.exists()
            shutil.copyfile(repo / relative, destination)
            if was_missing:
                paths.append(relative)
    # Minimal external fixture repositories may omit these two tools. The
    # real repository fixture above copies their actual current bytes instead.
    for relative in ("scripts/component_recovery.py", "deploy/product-update.sh"):
        destination = root / relative
        if not destination.exists():
            destination.write_text("# isolated build fixture\n", encoding="utf-8")
            paths.append(relative)
    git(root, "init", "--quiet")
    git(root, "add", "--", *paths)
    git(root, "-c", "user.name=Build Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: source export")
    commit = git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").stdout.decode("ascii").strip()
    (root / ".deployments").mkdir()
    return root, commit, tree


@pytest.fixture
def exported_git(tmp_path: Path) -> tuple[Path, str, str]:
    return export_source(REPO, tmp_path)


def test_export_uses_input_head_and_preserves_tracked_files(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    files = (
        "scripts/product_manifest.py", "scripts/component_runtime.py",
        "scripts/component_build.py", "scripts/deployment_admission.py", "scripts/component_recovery.py",
        "deploy/product-update.sh",
    )
    for name in files:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# tracked {name}\n", encoding="utf-8")
    git(source, "init", "--quiet")
    git(source, "add", "--", *files)
    git(source, "-c", "user.name=Build Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: input source")
    output = tmp_path / "output"
    output.mkdir()

    exported, _, _ = export_source(source, output)

    for name in files:
        assert (exported / name).read_bytes() == (source / name).read_bytes()


def invoke_with_lock(root: Path, commit: str, tree: str) -> subprocess.CompletedProcess[str]:
    script = 'set -eu; exec 9>>"$1"; flock -n 9; shift; exec "$@"'
    return subprocess.run(
        ["sh", "-c", script, "fixture-lock", str(root / ".deployments/.deployment.lock"),
         sys.executable, "-I", "-B", str(root / "scripts/component_build.py"), "--root", str(root),
         "build", "--source-commit", commit, "--source-tree", tree],
        env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )


@pytest.mark.parametrize("journal_kind", ["legacy-full", "legacy-runtime-malformed", "legacy-runtime-symlink", "component-runtime-unknown"])
def test_unknown_legacy_and_component_journals_refuse_before_build(exported_git, journal_kind: str) -> None:
    root, commit, tree = exported_git
    deployments = root / ".deployments"
    if journal_kind == "legacy-full":
        (deployments / ".full-transaction.json").write_text("{}", encoding="utf-8")
    elif journal_kind == "legacy-runtime-symlink":
        (deployments / ".runtime-transaction.json").symlink_to(root / "missing")
    elif journal_kind == "legacy-runtime-malformed":
        (deployments / ".runtime-transaction.json").write_text("{", encoding="utf-8")
    else:
        (deployments / ".component-runtime-transaction.json").write_text('{"schema_version":99}', encoding="utf-8")
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode != 0
    assert not (deployments / "component-build-attempts").exists()
    assert not (deployments / "product-builds").exists()


def test_dirty_tracked_source_refuses_before_build(exported_git) -> None:
    root, commit, tree = exported_git
    with (root / "deploy/Dockerfile").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed after commit\n")
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode != 0
    assert "not clean" in result.stderr
    assert not (root / ".deployments/component-build-attempts").exists()


def test_candidate_build_requires_root_bound_inherited_lock(exported_git) -> None:
    root, commit, tree = exported_git
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "scripts/component_build.py"), "--root", str(root),
         "build", "--source-commit", commit, "--source-tree", tree],
        env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    assert result.returncode != 0
    assert "lock" in result.stderr
    assert not (root / ".deployments/component-build-attempts").exists()


def test_fake_unregistered_and_hash_mismatch_receipts_are_rejected(tmp_path: Path) -> None:
    record = {
        "schema_version": 1, "build_id": "20261106T110000Z-0123456789abcdef0123456789abcdef",
        "source": {}, "context_sha256": "1" * 64, "recipe_sha256": "2" * 64,
        "command": [], "result_sha256": "3" * 64, "image_ref": "example:fixture",
        "image_id": "sha256:" + "4" * 64, "image_digest": "sha256:" + "4" * 64,
        "image_revision": "a" * 40, "installed_core_version": "0.7.0.dev1",
        "probe_container_id": "5" * 64, "intermediate_container_ids": [],
    }
    raw = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("ascii")
    digest = hashlib.sha256(raw).hexdigest()
    with manifest._directory(tmp_path, (".deployments", "product-builds"), create=True) as directory:
        manifest._write_file(directory, digest + ".json", raw, reuse=False)
    with pytest.raises(build.BuildError):
        build.verify_registered_build(tmp_path, digest)
    with pytest.raises(build.BuildError):
        build.verify_registered_build(tmp_path, "f" * 64)


def test_classic_build_command_is_refused_before_docker(tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="classic Docker producer execution is unsupported"):
        build._docker(["build", "--rm=false", str(tmp_path)], None, tmp_path)


def test_registered_receipt_requires_successful_exact_producer_result(tmp_path: Path) -> None:
    build_id = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    commit, tree, tool = "a" * 40, "b" * 40, "c" * 64
    source = {"commit": commit, "tree": tree, "tool_bytes_sha256": tool,
              "source_tool_sha256": build.runtime.source_tool_identity(commit, tree, tool)}
    source_id = source["source_tool_sha256"]
    bootstrap_files = {name: "d" * 64 for name in build.runtime.BOOTSTRAP_FILES}
    recovery_source = {
        **source,
        "capsule": {"path": ".deployments/component-source-" + source_id,
                    "manifest_sha256": "e" * 64, "archive_sha256": "f" * 64,
                    "bundle_sha256": "1" * 64},
        "bootstrap": {"path": ".deployments/component-bootstrap-" + source_id,
                      "files_sha256": bootstrap_files,
                      "sha256": build.runtime.bootstrap_identity(bootstrap_files)},
    }
    context_path = ".deployments/component-build-context-" + build_id
    context = tmp_path / context_path / "source"
    target_ref = "wuwaterm-component-build:" + build_id
    attempt = {
        "schema_version": 1, "build_id": build_id, "status": "active", "phase": "receipt",
        "source": source, "target_ref": target_ref, "context_path": context_path,
        "context_sha256": "1" * 64, "recipe_sha256": "2" * 64,
        "command": ["build", "--pull=false", "--rm=false", "--target", "runtime",
                    "--build-arg", "SOURCE_COMMIT=" + commit, "--tag", target_ref,
                    "--file", str(context / "deploy/Dockerfile"), str(context)],
        "build_result": {"exit_code": 0, "output_sha256": "3" * 64,
                         "intermediate_prefixes": ["4" * 12], "intermediate_container_ids": ["4" * 64]},
        "probe_name": "wuwaterm-component-probe-" + build_id, "probe_container_id": "5" * 64,
        "receipt_sha256": None, "failure": None, "recovery_source": recovery_source,
    }
    _, digest = build._receipt(tmp_path, attempt, "sha256:" + "6" * 64, "0.7.0.dev1", ["4" * 64])
    attempt.update(status="registered", phase="registered", receipt_sha256=digest)
    build._write_attempt(tmp_path, attempt, new=True)
    assert build._read_attempt(tmp_path, build_id + ".json")["source"] == source
    # A syntactically consistent, fabricated attempt is still missing the
    # independently retained source and must never be accepted as provenance.
    with pytest.raises(build.BuildError, match="retained build source"):
        build.verify_registered_build(tmp_path, digest)

    # A matching receipt hash cannot turn a failed build into a registered one.
    attempt["build_result"]["exit_code"] = 1
    build._write_attempt(tmp_path, attempt)
    with pytest.raises(build.BuildError, match="successful result"):
        build._read_attempt(tmp_path, build_id + ".json")

    attempt["build_result"]["exit_code"] = 0
    attempt["build_result"]["intermediate_container_ids"] = ["7" * 64]
    build._write_attempt(tmp_path, attempt)
    with pytest.raises(build.BuildError, match="resource identity"):
        build._read_attempt(tmp_path, build_id + ".json")


def _active_v2_source_attempt(tmp_path: Path) -> dict:
    build_id = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    commit, tree, tool = "a" * 40, "b" * 40, "c" * 64
    source = {"commit": commit, "tree": tree, "tool_bytes_sha256": tool,
              "source_tool_sha256": build.runtime.source_tool_identity(commit, tree, tool)}
    identity = source["source_tool_sha256"]
    bootstrap_files = {name: "d" * 64 for name in build.runtime.BOOTSTRAP_FILES}
    preserved = {**source, "capsule": {"path": ".deployments/component-source-" + identity,
                                     "manifest_sha256": "e" * 64, "archive_sha256": "f" * 64,
                                     "bundle_sha256": "1" * 64},
                 "bootstrap": {"path": ".deployments/component-bootstrap-" + identity,
                               "files_sha256": bootstrap_files,
                               "sha256": build.runtime.bootstrap_identity(bootstrap_files)}}
    binding = {"daemon": {"id": "fixture-daemon", "server_version": "29.1.3", "os": "linux",
                          "architecture": "x86_64", "driver_type": "io.containerd.snapshotter.v1"},
               "boot_id": build.isolation._boot_id(), "lock": {"device": 1, "inode": 2},
               "worker_image": build.isolation._WORKER_IMAGE, "worker_image_id": build.isolation._WORKER_DIGEST,
               "worker_platform": "linux/amd64", "binaries_sha256": {name: "2" * 64 for name in
                                                                        ("docker", "busctl", "systemctl")}}
    ledger = build.isolation.new_ledger("source", build_id, binding)
    attempt = {"schema_version": 2, "kind": "source", "build_id": build_id, "status": "active", "phase": "prepared",
               "source": source, "recovery_source": preserved, "target_ref": "wuwaterm-component-build:" + build_id,
               "context_path": ".deployments/component-build-context-" + build_id, "context_sha256": None,
               "recipe_sha256": None, "command": build._source_recipe(commit), "build_result": None,
               "probe_name": "wuwaterm-source-probe-" + build_id, "probe_container_id": None, "probe_sha256": None,
               "installed_core_version": None, "receipt_sha256": None, "failure": None, "isolation": ledger}
    return attempt


def test_versioned_source_attempt_cannot_claim_terminal_with_unknown_creator(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build_id = attempt["build_id"]
    ledger = attempt["isolation"]
    build._write_attempt(tmp_path, attempt, new=True)
    assert build._read_attempt(tmp_path, build_id + ".json")["status"] == "active"
    ledger["cancel_intent"] = "Interrupted"
    ledger["parent"].update(create_intent=True, create_outcome="unknown")
    attempt.update(status="failed", phase="failed", failure="Interrupted")
    path = tmp_path / ".deployments/component-build-attempts" / (build_id + ".json")
    before = path.read_bytes()
    with pytest.raises(build.BuildError, match="unresolved resources"):
        build._write_attempt(tmp_path, attempt)
    assert path.read_bytes() == before
    path.write_bytes(build._json(attempt))  # simulate a corrupt external journal
    with pytest.raises(build.BuildError, match="unresolved resources"):
        build._read_attempt(tmp_path, build_id + ".json")
    attempt.update(status="retained_import", phase="retained-import")
    with pytest.raises(build.BuildError):
        build._read_attempt_v2(tmp_path, build_id + ".json", attempt)


def test_cancel_cannot_publish_a_partial_source_transition(tmp_path: Path, monkeypatch) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    versions = []

    def write(value: dict) -> None:
        build._write_attempt(tmp_path, value)
        versions.append(build._read_attempt(tmp_path, value["build_id"] + ".json"))

    session = build.isolation.ProducerSession(tmp_path, attempt, write)
    partial, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []
    lines, first = inspect.getsourcelines(session.create_parent)
    pause_line = next(first + index for index, line in enumerate(lines) if 'parent["create_outcome"] = "unknown"' in line)

    class ReachedClient(Exception):
        pass

    monkeypatch.setattr(build.isolation, "parent_absent", lambda _: True)
    monkeypatch.setattr(session, "client", lambda *args, **kwargs: (_ for _ in ()).throw(ReachedClient()))

    def update() -> None:
        try:
            def trace(frame, event, argument):
                if frame.f_code.co_name == "create_parent" and event == "line" and frame.f_lineno == pause_line:
                    sys.settrace(None)
                    partial.set()
                    if not release.wait(5):
                        raise RuntimeError("test release was not delivered")
                return trace

            sys.settrace(trace)
            try:
                session.create_parent()
            except ReachedClient:
                pass
            finally:
                sys.settrace(None)
        except BaseException as exc:
            errors.append(exc)

    def cancel() -> None:
        try:
            session.cancel("Cancelled")
            cancelled.set()
        except BaseException as exc:
            errors.append(exc)

    producer = threading.Thread(target=update)
    client = threading.Thread(target=cancel)
    producer.start()
    assert partial.wait(5)
    client.start()
    try:
        assert not cancelled.wait(0.1)
    finally:
        release.set()
        producer.join(5)
        client.join(5)
    assert not producer.is_alive() and not client.is_alive() and not errors and cancelled.is_set()
    assert len(versions) == 2
    assert all(item["isolation"]["parent"]["create_outcome"] == "unknown" for item in versions)
    assert versions[-1]["isolation"]["cancel_intent"] == "Cancelled"
    assert build._read_attempt(tmp_path, attempt["build_id"] + ".json") == attempt


@pytest.mark.parametrize("published", [False, True])
def test_source_writer_failure_poisons_session_without_stale_overwrite(tmp_path: Path, published: bool) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    path = tmp_path / ".deployments/component-build-attempts" / (attempt["build_id"] + ".json")
    before = path.read_bytes()

    def fail_write(value: dict) -> None:
        if published:
            build._write_attempt(tmp_path, value)
        raise OSError("fixture publication outcome is uncertain")

    session = build.isolation.ProducerSession(tmp_path, attempt, fail_write)
    with pytest.raises(OSError, match="uncertain"):
        with session.transition(phase="containment-intent"):
            session.ledger["parent"].update(create_intent=True, create_outcome="unknown")
    persisted = path.read_bytes()
    assert (persisted != before) is published
    assert build._read_attempt(tmp_path, attempt["build_id"] + ".json")["status"] == "active"
    with pytest.raises(build.isolation.IsolationError, match="exact-source recovery"):
        session.cancel("Cancelled")
    assert path.read_bytes() == persisted


def test_invalid_source_transition_is_never_persisted_and_poisons_session(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    path = tmp_path / ".deployments/component-build-attempts" / (attempt["build_id"] + ".json")
    before = path.read_bytes()
    session = build.isolation.ProducerSession(tmp_path, attempt, lambda value: build._write_attempt(tmp_path, value))
    with pytest.raises(build.isolation.IsolationError, match="validation refused"):
        with session.transition():
            session.ledger["parent"]["create_intent"] = True
    assert path.read_bytes() == before
    with pytest.raises(build.isolation.IsolationError, match="exact-source recovery"):
        session.cancel("Cancelled")
    assert path.read_bytes() == before


def test_failed_cancel_publication_is_never_acknowledged_on_retry(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)

    def fail_write(value: dict) -> None:
        raise OSError("cancel intent was not durably published")

    session = build.isolation.ProducerSession(tmp_path, attempt, fail_write)
    with pytest.raises(OSError, match="not durably published"):
        session.cancel("Cancelled")
    with pytest.raises(build.isolation.IsolationError, match="exact-source recovery"):
        session.cancel("Cancelled")
    persisted = build._read_attempt(tmp_path, attempt["build_id"] + ".json")
    assert persisted["isolation"]["cancel_intent"] is None


def _locked_python(root: Path, program: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    lock = root / ".deployments/.deployment.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    shell = 'set -eu; exec 9>>"$1"; flock -n 9; shift; exec "$@"'
    return subprocess.run(["/bin/sh", "-c", shell, "fixture-lock", str(lock), sys.executable, "-I", "-B",
                           "-c", program, str(REPO / "scripts"), str(root), *args],
                          env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


@pytest.mark.parametrize("point", ["before-replace", "after-replace"])
def test_killed_source_update_leaves_only_a_bound_recoverable_stage(tmp_path: Path, point: str) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    name = attempt["build_id"] + ".json"
    canonical = tmp_path / ".deployments/component-build-attempts" / name
    before = canonical.read_bytes()
    program = """import os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];point=sys.argv[4]
a=b._read_attempt(root,ident+'.json');a['phase']='source-intent'
original=os.replace
def crash(*args,**kwargs):
 if point=='before-replace':os.kill(os.getpid(),signal.SIGKILL)
 original(*args,**kwargs)
 os.kill(os.getpid(),signal.SIGKILL)
i.os.replace=crash
b._write_attempt(root,a)
"""
    killed = _locked_python(tmp_path, program, attempt["build_id"], point)
    assert killed.returncode == -9, killed.stderr
    stage = tmp_path / ".deployments/component-build-staging"
    entries = list(stage.iterdir())
    assert len(entries) == (1 if point == "before-replace" else 0)
    current = build._read_attempt(tmp_path, name)
    assert current["phase"] == ("prepared" if point == "before-replace" else "source-intent")
    if entries:
        assert entries[0].name == attempt["build_id"] + "." + hashlib.sha256(before).hexdigest() + ".json"
        assert entries[0].stat().st_nlink == 1
    reconcile = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=b._read_attempt(root,ident+'.json')
i.reconcile_attempt_stage(root,'source',ident,a)
assert b._read_attempt(root,ident+'.json')==a
"""
    recovered = _locked_python(tmp_path, reconcile, attempt["build_id"])
    assert recovered.returncode == 0, recovered.stderr
    assert not list(stage.iterdir())
    assert build._read_attempt(tmp_path, name) == current


@pytest.mark.parametrize("point", ["before-link", "after-link"])
def test_killed_initial_source_publication_has_no_named_temporary(tmp_path: Path, point: str) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    program = """import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,product_manifest as m
root=pathlib.Path(sys.argv[2]);a=json.loads(sys.argv[3]);point=sys.argv[4];original=os.link
def crash(*args,**kwargs):
 if point=='before-link':os.kill(os.getpid(),signal.SIGKILL)
 original(*args,**kwargs)
 os.kill(os.getpid(),signal.SIGKILL)
m.os.link=crash
b._write_attempt(root,a,new=True)
"""
    killed = _locked_python(tmp_path, program, json.dumps(attempt), point)
    assert killed.returncode == -9, killed.stderr
    directory = tmp_path / ".deployments/component-build-attempts"
    entries = list(directory.iterdir())
    assert len(entries) == (0 if point == "before-link" else 1)
    if entries:
        assert entries[0].name == attempt["build_id"] + ".json" and entries[0].stat().st_nlink == 1
        assert build._read_attempt(tmp_path, entries[0].name) == attempt


@pytest.mark.parametrize("conflict", ["preimage", "source", "multiple", "hardlink", "unknown-name", "changed-final"])
def test_exact_source_recovery_refuses_unowned_or_conflicting_stage(tmp_path: Path, conflict: str) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    name = attempt["build_id"] + ".json"
    canonical = tmp_path / ".deployments/component-build-attempts" / name
    before = canonical.read_bytes()
    candidate = copy.deepcopy(attempt)
    candidate["phase"] = "source-intent"
    if conflict == "source":
        source = candidate["source"]
        source["commit"] = "d" * 40
        source["source_tool_sha256"] = build.runtime.source_tool_identity(source["commit"], source["tree"],
                                                                        source["tool_bytes_sha256"])
        saved = candidate["recovery_source"]
        saved.update(source)
        saved["capsule"]["path"] = ".deployments/component-source-" + source["source_tool_sha256"]
        saved["bootstrap"]["path"] = ".deployments/component-bootstrap-" + source["source_tool_sha256"]
        candidate["command"] = build._source_recipe(source["commit"])
        build._read_attempt_v2(tmp_path, name, candidate)
    stage = tmp_path / ".deployments/component-build-staging"
    stage.mkdir(mode=0o700)
    preimage = "0" * 64 if conflict == "preimage" else hashlib.sha256(before).hexdigest()
    entry = stage / ("unknown" if conflict == "unknown-name" else attempt["build_id"] + "." + preimage + ".json")
    entry.write_bytes(build._json(candidate))
    entry.chmod(0o600)
    if conflict == "multiple":
        (stage / (attempt["build_id"] + "." + "1" * 64 + ".json")).write_bytes(build._json(candidate))
    elif conflict == "hardlink":
        os.link(entry, tmp_path / "second-link")
    elif conflict == "changed-final":
        changed = copy.deepcopy(attempt)
        changed["phase"] = "source-ready"
        canonical.write_bytes(build._json(changed))
    final_before = canonical.read_bytes()
    stage_before = entry.read_bytes()
    reconcile = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=b._read_attempt(root,ident+'.json')
i.reconcile_attempt_stage(root,'source',ident,a)
"""
    result = _locked_python(tmp_path, reconcile, attempt["build_id"])
    assert result.returncode != 0
    assert canonical.read_bytes() == final_before and entry.read_bytes() == stage_before


def test_exact_source_recovery_accepts_already_published_identical_stage(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    canonical = tmp_path / ".deployments/component-build-attempts" / (attempt["build_id"] + ".json")
    before = canonical.read_bytes()
    attempt["phase"] = "source-intent"
    build._write_attempt(tmp_path, attempt)
    stage = tmp_path / ".deployments/component-build-staging"
    entry = stage / (attempt["build_id"] + "." + hashlib.sha256(before).hexdigest() + ".json")
    entry.write_bytes(canonical.read_bytes())
    entry.chmod(0o600)
    reconcile = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=b._read_attempt(root,ident+'.json')
i.reconcile_attempt_stage(root,'source',ident,a)
"""
    result = _locked_python(tmp_path, reconcile, attempt["build_id"])
    assert result.returncode == 0, result.stderr
    assert not entry.exists() and build._read_attempt(tmp_path, attempt["build_id"] + ".json") == attempt


def test_cancel_reconciles_a_partially_published_source_receipt(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    raw = build._json({"schema_version": 2, "build_id": attempt["build_id"]})
    digest = hashlib.sha256(raw).hexdigest()
    attempt["isolation"]["cancel_intent"] = "Cancelled"
    attempt["isolation"]["receipt"] = {"path": ".deployments/product-builds/" + digest + ".json",
                                       "sha256": digest, "state": "intent", "remove_intent": False}
    build._write_attempt(tmp_path, attempt, new=True)
    publish = """import os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]);import product_manifest as m
root=pathlib.Path(sys.argv[2]);digest=sys.argv[3];raw=bytes.fromhex(sys.argv[4]);original=os.link
def crash(*args,**kwargs):
 original(*args,**kwargs)
 os.kill(os.getpid(),signal.SIGKILL)
m.os.link=crash
with m._directory(root,('.deployments','product-builds'),create=True) as directory:
 m._write_file_unnamed(directory,digest+'.json',raw,reuse=False)
"""
    killed = _locked_python(tmp_path, publish, digest, raw.hex())
    assert killed.returncode == -9, killed.stderr
    receipt = tmp_path / attempt["isolation"]["receipt"]["path"]
    assert receipt.exists() and receipt.stat().st_nlink == 1
    program = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=b._read_attempt(root,ident+'.json')
s=i.ProducerSession(root,a,lambda value:b._write_attempt(root,value));s.reconcile_receipt()
"""
    recovered = _locked_python(tmp_path, program, attempt["build_id"])
    assert recovered.returncode == 0, recovered.stderr
    result = build._read_attempt(tmp_path, attempt["build_id"] + ".json")
    assert result["isolation"]["receipt"]["state"] == "revoked" and result["receipt_sha256"] is None
    assert not receipt.exists()


def test_real_source_session_context_and_cancel_publish_only_parseable_versions(tmp_path: Path) -> None:
    attempt = _active_v2_source_attempt(tmp_path)
    build._write_attempt(tmp_path, attempt, new=True)
    program = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build as b,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=b._read_attempt(root,ident+'.json');versions=[]
def write(value):
 b._write_attempt(root,value)
 versions.append(b._read_attempt(root,ident+'.json'))
s=i.ProducerSession(root,a,write)
s.prepare_directories();s.cancel('Cancelled')
assert len(versions)>=6 and versions[-1]['isolation']['cancel_intent']=='Cancelled'
assert b._read_attempt(root,ident+'.json')==a
print(len(versions))
"""
    result = _locked_python(tmp_path, program, attempt["build_id"])
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) >= 6


@pytest.mark.parametrize("kind", ["source", "wheel"])
def test_registered_sync_finishes_post_replace_window_and_refuses_eio(tmp_path: Path, kind: str) -> None:
    ident = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    attempts, staging = build.isolation._attempt_directories(kind)
    final_dir = tmp_path / ".deployments" / attempts
    stage_dir = tmp_path / ".deployments" / staging
    final_dir.mkdir(parents=True)
    stage_dir.mkdir()
    name = ident + ".json"
    prior = {"schema_version": 2, "status": "active", "build_id": ident}
    registered = {"schema_version": 2, "status": "registered", "build_id": ident}
    canonical, staged = final_dir / name, stage_dir / "fixture-candidate"
    canonical.write_bytes(build._json(prior))
    canonical.chmod(0o600)
    staged.write_bytes(build._json(registered))
    staged.chmod(0o600)
    kill = """import os,pathlib,signal,sys
root=pathlib.Path(sys.argv[2]);kind=sys.argv[3];ident=sys.argv[4]
attempts='component-build-attempts' if kind=='source' else 'component-wheel-build-attempts'
stage='component-build-staging' if kind=='source' else 'component-wheel-build-staging'
os.replace(root/'.deployments'/stage/'fixture-candidate',root/'.deployments'/attempts/(ident+'.json'))
os.kill(os.getpid(),signal.SIGKILL)
"""
    killed = _locked_python(tmp_path, kill, kind, ident)
    assert killed.returncode == -9 and not staged.exists()
    before = canonical.read_bytes()
    assert json.loads(before) == registered and canonical.stat().st_nlink == 1
    script = """import errno,json,os,pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build_isolation as i
root=pathlib.Path(sys.argv[2]);kind=sys.argv[3];ident=sys.argv[4];mode=sys.argv[5]
attempts='component-build-attempts' if kind=='source' else 'component-wheel-build-attempts'
stage='component-build-staging' if kind=='source' else 'component-wheel-build-staging'
paths={name:(root/'.deployments'/name).stat().st_ino for name in (attempts,stage)}
record=json.loads((root/'.deployments'/attempts/(ident+'.json')).read_bytes());original=os.fsync;seen=[]
def controlled(fd):
 inode=os.fstat(fd).st_ino
 for name,expected in paths.items():
  if inode==expected:
   seen.append(name)
   if mode==name:raise OSError(errno.EIO,'synthetic fsync failure')
 return original(fd)
os.fsync=controlled
try:
 i.sync_registered_attempt(root,kind,ident,record)
except i.IsolationError:
 if mode=='ok':raise
 print(json.dumps({'accepted':False,'synced':seen}));sys.exit(0)
if mode!='ok':raise RuntimeError('EIO was accepted')
print(json.dumps({'accepted':True,'synced':seen}))
"""
    for mode in (attempts, staging, "ok"):
        result = _locked_python(tmp_path, script, kind, ident, mode)
        assert result.returncode == 0, result.stderr
        evidence = json.loads(result.stdout)
        assert evidence["accepted"] is (mode == "ok")
        if mode == "ok":
            assert set(evidence["synced"]) == {attempts, staging}
        assert canonical.read_bytes() == before


@pytest.mark.parametrize("kind", ["source", "wheel"])
def test_registered_sync_rejects_canonical_replacement_during_sync(tmp_path: Path, kind: str) -> None:
    ident = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    attempts, staging = build.isolation._attempt_directories(kind)
    final_dir = tmp_path / ".deployments" / attempts
    final_dir.mkdir(parents=True)
    (tmp_path / ".deployments" / staging).mkdir()
    canonical = final_dir / (ident + ".json")
    raw = build._json({"schema_version": 2, "status": "registered", "build_id": ident})
    canonical.write_bytes(raw)
    canonical.chmod(0o600)
    script = """import json,os,pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_build_isolation as i
root=pathlib.Path(sys.argv[2]);kind=sys.argv[3];ident=sys.argv[4]
directory='component-build-attempts' if kind=='source' else 'component-wheel-build-attempts'
path=root/'.deployments'/directory/(ident+'.json');record=json.loads(path.read_bytes())
target=path.parent.stat().st_ino;original=os.fsync;swapped=[False]
def replace_after_sync(fd):
 original(fd)
 if not swapped[0] and os.fstat(fd).st_ino==target:
  alternative=path.parent/'fixture-alternative';alternative.write_bytes(path.read_bytes());alternative.chmod(0o600)
  os.replace(alternative,path);swapped[0]=True
os.fsync=replace_after_sync
try:i.sync_registered_attempt(root,kind,ident,record)
except i.IsolationError:
 assert swapped[0];sys.exit(0)
raise RuntimeError('canonical replacement was accepted')
"""
    result = _locked_python(tmp_path, script, kind, ident)
    assert result.returncode == 0, result.stderr
    assert canonical.read_bytes() == raw


def test_saved_source_verification_loads_packager_in_a_fresh_process(exported_git) -> None:
    root, commit, tree = exported_git
    lock_script = 'set -eu; exec 9>>"$1"; flock -n 9; shift; exec "$@"'
    prepare = ("import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(r/'scripts')); "
               "import component_recovery as m; print(json.dumps(m.prepare_source(r,sys.argv[2],sys.argv[3])))")
    args = ["sh", "-c", lock_script, "fixture-lock", str(root / ".deployments/.deployment.lock"),
            sys.executable, "-I", "-B", "-c", prepare, str(root), commit, tree]
    made = subprocess.run(args, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert made.returncode == 0, made.stderr
    preserved = json.loads(made.stdout)
    verify = ("import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); s=json.loads(sys.argv[2]); "
              "sys.path.insert(0,str(r/'scripts')); import component_recovery as m; "
              "import component_build_isolation as i; assert m.capsule is None; "
              "a={'source':{k:s[k] for k in ('commit','tree','tool_bytes_sha256','source_tool_sha256')},"
              "'recovery_source':s}; i._saved_source(r,'source',a,executing=False); assert m.capsule is not None")
    args = ["sh", "-c", lock_script, "fixture-lock", str(root / ".deployments/.deployment.lock"),
            sys.executable, "-I", "-B", "-c", verify, str(root), json.dumps(preserved)]
    checked = subprocess.run(args, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.skipif(os.environ.get("WUWATERM_RUN_DOCKER_BUILD") != "1", reason="explicit real Docker build only")
def test_real_source_bound_core_build_and_registered_receipt(exported_git) -> None:
    root, commit, tree = exported_git
    print(f"retained real build fixture: {root}", flush=True)
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode == 0, result.stderr
    digest = result.stdout.strip()
    receipt = build.verify_registered_build(root, digest)
    assert receipt["source"]["commit"] == commit
    assert receipt["source"]["tree"] == tree
    assert receipt["image_revision"] == commit
    assert receipt["installed_core_version"] == "0.7.0.dev1"
    assert receipt["recovery_source"]["source_tool_sha256"] == receipt["source"]["source_tool_sha256"]
    inspect = subprocess.run(
        ["docker", "--host", "unix:///run/docker.sock", "image", "inspect", "--format", "{{.Id}}", receipt["image_ref"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(), check=False,
    )
    assert inspect.returncode == 0
    assert inspect.stdout.strip() == receipt["image_id"]
    print(f"receipt: {digest}; image: {receipt['image_id']}; probe: {receipt['probe_container_id']}", flush=True)


@pytest.mark.skipif(os.environ.get("WUWATERM_RUN_DOCKER_BUILD") != "1", reason="explicit real Docker build only")
def test_real_failed_build_does_not_register_or_publish_pointers(exported_git) -> None:
    root, _, _ = exported_git
    with (root / "deploy/Dockerfile").open("a", encoding="utf-8") as stream:
        stream.write("\nINVALID_TEST_DOCKER_INSTRUCTION true\n")
    git(root, "add", "deploy/Dockerfile")
    git(root, "-c", "user.name=Build Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: invalid build recipe")
    commit = git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").stdout.decode("ascii").strip()
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode != 0
    attempts = list((root / ".deployments/component-build-attempts").glob("*.json"))
    assert len(attempts) == 1
    attempt = build._read_attempt(root, attempts[0].name)
    assert attempt["status"] == "failed"
    assert not (root / ".deployments/product-builds").exists()
    assert not (root / ".deploy_commit").exists()
    assert not (root / ".deploy_current").exists()


@pytest.mark.skipif(os.environ.get("WUWATERM_RUN_DOCKER_BUILD") != "1", reason="explicit real Docker build only")
def test_real_build_rejects_wrong_installed_core_version(exported_git) -> None:
    root, _, _ = exported_git
    recipe = root / "deploy/Dockerfile"
    content = recipe.read_text(encoding="utf-8")
    marker = "\n\nFROM python-base AS builder"
    assert content.count(marker) == 1
    corrupt = (
        "\nRUN python -c \"from pathlib import Path; "
        "p=next(Path('/app/.venv/lib/python3.11/site-packages').glob('wuwaterm-*.dist-info/METADATA')); "
        "p.write_text(p.read_text().replace('Version: 0.7.0.dev1', 'Version: 9.9.9', 1))\""
    )
    recipe.write_text(content.replace(marker, corrupt + marker), encoding="utf-8")
    git(root, "add", "deploy/Dockerfile")
    git(root, "-c", "user.name=Build Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: wrong installed version")
    commit = git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").stdout.decode("ascii").strip()
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode != 0 and "installed Core version differs" in result.stderr
    attempts = list((root / ".deployments/component-build-attempts").glob("*.json"))
    assert len(attempts) == 1
    attempt = build._read_attempt(root, attempts[0].name)
    assert attempt["status"] == "retained_import" and attempt["build_result"]["exit_code"] == 0
    ledger = attempt["isolation"]
    assert ledger["probe"]["absent"] and ledger["worker"]["absent"] and ledger["cache"]["absent"]
    assert ledger["archive"]["state"] == "sealed" and attempt["receipt_sha256"] is None
    logs = [client for client in ledger["clients"] if client["operation"] == "probe-logs"]
    assert len(logs) == 1
    session = build.isolation.ProducerSession(root, attempt, lambda _: None)
    assert session.output(logs[0]["generation"], "stdout").decode("ascii").strip() == "9.9.9"
    assert not (root / ".deployments/product-builds").exists()
    assert not (root / ".deploy_commit").exists() and not (root / ".deploy_current").exists()
