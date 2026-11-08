from __future__ import annotations

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
import zipfile

import pytest

from scripts import component_wheel_build as wheel

REPO = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str) -> bytes:
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_REPLACE_OBJECTS": "1"}
    return subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *arguments],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=True).stdout


def commit_fixture(root: Path) -> tuple[str, str]:
    git(root, "init", "--quiet")
    git(root, "add", "-A")
    git(root, "-c", "user.name=Wheel Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: isolated fixture")
    return (git(root, "rev-parse", "HEAD").decode().strip(), git(root, "rev-parse", "HEAD^{tree}").decode().strip())


def extract_archive(repo: Path, commit: str, dest: Path) -> None:
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit)), mode="r:") as source:
        assert all(item.isfile() or item.isdir() for item in source.getmembers())
        source.extractall(dest, filter="data")


def test_retained_git_tree_and_commit_are_rechecked(tmp_path: Path) -> None:
    repo = tmp_path / "git"
    repo.mkdir()
    (repo / "module.py").write_text("answer = 42\n")
    (repo / "executable").write_text("#!/bin/sh\nexit 0\n")
    (repo / "executable").chmod(0o755)
    commit, tree = commit_fixture(repo)
    context = tmp_path / "context"
    context.mkdir()
    extract_archive(repo, commit, context / "source")
    (context / "commit.bin").write_bytes(git(repo, "cat-file", "commit", commit))
    wheel._source(context, {"commit": commit, "tree": tree})
    (context / "source/module.py").write_text("answer = 0\n")
    with pytest.raises(wheel.WheelBuildError, match="Git identity"):
        wheel._source(context, {"commit": commit, "tree": tree})


def test_wheel_must_match_source_package_and_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "src/wuwaterm_telegram"
    package.mkdir(parents=True)
    (package / "cli.py").write_bytes(b"def main(): pass\n")
    path = tmp_path / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wuwaterm_telegram/cli.py", b"def main(): pass\n")
        archive.writestr("wuwaterm_telegram-0.1.dist-info/METADATA", "Name: wuwaterm-telegram\nVersion: 0.1\n")
        archive.writestr("wuwaterm_telegram-0.1.dist-info/entry_points.txt",
                         "[console_scripts]\nwuwaterm-telegram = wuwaterm_telegram.cli:main\n")
    assert wheel._telegram_wheel(path, source, "0.1") == {
        "wuwaterm_telegram/cli.py": hashlib.sha256(b"def main(): pass\n").hexdigest()}
    (package / "cli.py").write_bytes(b"def main(): return 1\n")
    with pytest.raises(wheel.WheelBuildError, match="differs from source"):
        wheel._telegram_wheel(path, source, "0.1")


def test_unregistered_json_is_not_a_build_receipt(tmp_path: Path) -> None:
    data = b'{"build_id":"20261106T110000Z-0123456789abcdef0123456789abcdef"}\n'
    digest = hashlib.sha256(data).hexdigest()
    directory = tmp_path / ".deployments/product-wheel-builds"
    directory.mkdir(parents=True)
    (directory / (digest + ".json")).write_bytes(data)
    with pytest.raises(wheel.WheelBuildError):
        wheel.verify_registered_wheel_build(tmp_path, digest)


def _tool_wheel(directory: Path, name: str, version: str, *requires: str) -> Path:
    path = directory / f"{name}-{version}-py3-none-any.whl"
    metadata = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.9\n"
                + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires) + "\n")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        archive.writestr(f"{name}-{version}.dist-info/WHEEL",
                         "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n")
        archive.writestr(f"{name}/__init__.py", f"__version__ = '{version}'\n")
    return path


def _preparation_inputs(tmp_path: Path) -> tuple[Path, Path, str]:
    context = tmp_path / "context"
    source = context / "source"
    package = source / "src/wuwaterm_telegram"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=69", "wheel"]\nbuild-backend = "setuptools.build_meta"\n'
    )
    (package / "cli.py").write_text("def main(): pass\n")
    executable = source / "executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    wheelhouse = tmp_path / "tool-wheels"
    wheelhouse.mkdir()
    _tool_wheel(wheelhouse, "packaging", "26.3")
    _tool_wheel(wheelhouse, "setuptools", "84.2.1", 'pytest>=6; extra == "test"')
    _tool_wheel(wheelhouse, "wheel", "0.48.0", "packaging >= 24.0")
    return context, wheelhouse, wheel._tree(source)


def test_preparation_context_preserves_source_and_offline_tools(tmp_path: Path) -> None:
    context, wheelhouse, tree = _preparation_inputs(tmp_path)
    (wheelhouse / "runtime_dependency-1.0-py3-none-any.whl").write_bytes(b"not a build tool")
    tools = wheel.inspect_wheel_build_tools(wheelhouse)
    record = wheel.prepare_wheel_build_context(context, wheelhouse, source_tree=tree, tools=tools)
    wheel.verify_wheel_build_context(context, record)

    preparation = context / record["context_path"]
    recipe = (preparation / "Dockerfile").read_text()
    runs = [line for line in recipe.splitlines() if line.startswith("RUN ")]
    assert len(runs) == 2
    assert all(line.startswith("RUN --network=none ") and '"--no-index"' in line for line in runs)
    assert '"--no-build-isolation"' in runs[1] and '"--no-deps"' in runs[1]
    assert recipe.endswith("FROM scratch\nCOPY --from=wheel-build /build/dist/ /\n")
    assert record["recipe_sha256"] == hashlib.sha256(recipe.encode()).hexdigest()
    assert wheel._tree(preparation / "source") == tree
    assert (preparation / "source/executable").stat().st_mode & 0o777 == 0o755
    assert {item.name for item in (preparation / "tools").iterdir()} == {
        item["filename"] for item in tools.values()
    }
    for item in tools.values():
        assert hashlib.sha256((preparation / "tools" / item["filename"]).read_bytes()).hexdigest() == item["sha256"]

    second = tmp_path / "second"
    second.mkdir()
    shutil.copytree(context / "source", second / "source")
    assert wheel.prepare_wheel_build_context(second, wheelhouse, source_tree=tree, tools=tools) == record
    assert (second / "wheel-preparation/Dockerfile").read_bytes() == (preparation / "Dockerfile").read_bytes()


def test_preparation_refuses_missing_ambiguous_and_changed_tools_before_context_creation(tmp_path: Path) -> None:
    context, wheelhouse, tree = _preparation_inputs(tmp_path)
    tools = wheel.inspect_wheel_build_tools(wheelhouse)
    with (wheelhouse / tools["packaging"]["filename"]).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(wheel.WheelBuildError, match="changed after preflight"):
        wheel.prepare_wheel_build_context(context, wheelhouse, source_tree=tree, tools=tools)
    assert not (context / "wheel-preparation").exists()

    missing = tmp_path / "missing"
    missing.mkdir()
    _tool_wheel(missing, "setuptools", "84.2.1")
    _tool_wheel(missing, "wheel", "0.48.0", "packaging >= 24.0")
    with pytest.raises(wheel.WheelBuildError, match="missing"):
        wheel.inspect_wheel_build_tools(missing)
    _tool_wheel(missing, "packaging", "23.0")
    with pytest.raises(wheel.WheelBuildError, match="does not satisfy"):
        wheel.inspect_wheel_build_tools(missing)
    _tool_wheel(missing, "packaging", "26.3")
    with pytest.raises(wheel.WheelBuildError, match="ambiguous"):
        wheel.inspect_wheel_build_tools(missing)


@pytest.mark.parametrize("changed", ["recipe", "tool", "source", "source_mode", "source_permissions"])
def test_preparation_rechecks_recorded_inputs(tmp_path: Path, changed: str) -> None:
    context, wheelhouse, tree = _preparation_inputs(tmp_path)
    tools = wheel.inspect_wheel_build_tools(wheelhouse)
    record = wheel.prepare_wheel_build_context(context, wheelhouse, source_tree=tree, tools=tools)
    preparation = context / record["context_path"]
    if changed == "recipe":
        with (preparation / "Dockerfile").open("ab") as stream:
            stream.write(b"# changed\n")
    elif changed == "tool":
        with (preparation / "tools" / tools["wheel"]["filename"]).open("ab") as stream:
            stream.write(b"changed")
    elif changed == "source":
        (preparation / "source/src/wuwaterm_telegram/cli.py").write_text("def main(): return 1\n")
    elif changed == "source_mode":
        (preparation / "source/executable").chmod(0o644)
    else:
        (preparation / "source/src/wuwaterm_telegram/cli.py").chmod(0o666)
    with pytest.raises(wheel.WheelBuildError):
        wheel.verify_wheel_build_context(context, record)


def test_preparation_refuses_unknown_backend_or_missing_backend_requirement(tmp_path: Path) -> None:
    context, wheelhouse, _ = _preparation_inputs(tmp_path)
    project = context / "source/pyproject.toml"
    project.write_text('[build-system]\nrequires = ["unknown-builder>=1"]\nbuild-backend = "setuptools.build_meta"\n')
    tree = wheel._tree(context / "source")
    tools = wheel.inspect_wheel_build_tools(wheelhouse)
    with pytest.raises(wheel.WheelBuildError, match="required build tool is missing"):
        wheel.prepare_wheel_build_context(context, wheelhouse, source_tree=tree, tools=tools)
    assert not (context / "wheel-preparation").exists()
    project.write_text('[build-system]\nrequires = ["setuptools>=69"]\nbuild-backend = "other.backend"\n')
    with pytest.raises(wheel.WheelBuildError, match="unsupported Telegram build backend"):
        wheel.prepare_wheel_build_context(context, wheelhouse, source_tree=wheel._tree(context / "source"), tools=tools)
    assert not (context / "wheel-preparation").exists()


def test_preparation_refuses_multiple_root_wheel_metadata(tmp_path: Path) -> None:
    _, wheelhouse, _ = _preparation_inputs(tmp_path)
    path = wheelhouse / "wheel-0.48.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("extra-1.0.dist-info/METADATA", "Name: extra\nVersion: 1.0\n\n")
    with pytest.raises(wheel.WheelBuildError, match="wheel metadata"):
        wheel.inspect_wheel_build_tools(wheelhouse)


def test_guarded_source_archive_manifest_matches_git_blobs(tmp_path: Path) -> None:
    repo = tmp_path / "git-source"
    repo.mkdir()
    (repo / "module.py").write_text("answer = 42\n")
    (repo / "scripts").mkdir()
    (repo / "scripts/run.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "scripts/run.sh").chmod(0o755)
    commit, tree = commit_fixture(repo)
    entries = wheel._git_tree_entries(git(repo, "ls-tree", "-rlz", "--full-tree", commit))
    archive_path = tmp_path / "source.tar"
    archive_path.write_bytes(git(repo, "archive", "--format=tar", commit))
    files = wheel._archive_manifest(archive_path, entries)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    wheel.capsule.inspect_archive(archive_path, files, extract=extracted, require_tools=False)
    assert wheel._tree(extracted) == tree
    assert files["scripts/run.sh"]["mode"] == "100755"

    changed_archive = tmp_path / "changed.tar"
    with tarfile.open(archive_path, "r:") as original, tarfile.open(changed_archive, "w") as changed:
        for item in original:
            stream = original.extractfile(item) if item.isfile() else None
            data = stream.read() if stream is not None else None
            if item.name == "module.py":
                assert data is not None
                data = data.replace(b"42", b"24")
            changed.addfile(item, io.BytesIO(data) if data is not None else None)
    with pytest.raises(wheel.WheelBuildError, match="archive content differs"):
        wheel._archive_manifest(changed_archive, entries)


@pytest.mark.parametrize("content", [
    "FROM python:3.11-slim\nRUN --network=host true\n",
    "FROM python:3.11-slim\nRUN --net\\\nwork=host true\n",
    "# syntax=docker/dockerfile:latest\nFROM python:3.11-slim\n",
    "# escape=`\nFROM python:3.11-slim\n",
])
def test_telegram_recipe_refuses_network_or_frontend_overrides(tmp_path: Path, content: str) -> None:
    recipe = tmp_path / "Dockerfile"
    recipe.write_text(content)
    with pytest.raises(wheel.WheelBuildError, match="offline frontend"):
        wheel._offline_dockerfile(recipe)


def test_telegram_recipe_without_override_accepts_builtin_offline_frontend(tmp_path: Path) -> None:
    recipe = tmp_path / "Dockerfile"
    recipe.write_text("FROM python:3.11-slim\nRUN python -m pip check\n")
    wheel._offline_dockerfile(recipe)
    command = wheel._image_solve_command("a" * 40)
    assert "--frontend=dockerfile.v0" in command
    assert "force-network-mode=none" in command
    assert "build-arg:PIP_NO_INDEX=1" in command
    assert "type=oci,dest=/work/output/image.tar" in command


def _active_v2_attempt(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "deployment"
    root.mkdir()
    _, wheelhouse, _ = _preparation_inputs(tmp_path)
    tools = wheel.inspect_wheel_build_tools(wheelhouse)
    commit, tree, tool = "a" * 40, "b" * 40, "c" * 64
    identity = wheel.runtime.source_tool_identity(commit, tree, tool)
    operations = {"commit": commit, "tree": tree, "tool_bytes_sha256": tool, "source_tool_sha256": identity}
    bootstrap_files = {name: "d" * 64 for name in wheel.runtime.BOOTSTRAP_FILES}
    saved = {**operations, "capsule": {"path": ".deployments/component-source-" + identity,
                                      "manifest_sha256": "1" * 64, "archive_sha256": "2" * 64,
                                      "bundle_sha256": "3" * 64},
             "bootstrap": {"path": ".deployments/component-bootstrap-" + identity,
                           "files_sha256": bootstrap_files,
                           "sha256": wheel.runtime.bootstrap_identity(bootstrap_files)}}
    ident = "20261106T110000Z-0123456789abcdef0123456789abcdef"
    binding = {"daemon": {"id": "fixture", "server_version": "29.1.3", "os": "linux",
                          "architecture": "x86_64", "driver_type": "io.containerd.snapshotter.v1"},
               "boot_id": wheel.isolation._boot_id(), "lock": {"device": 1, "inode": 2},
               "worker_image": wheel.isolation._WORKER_IMAGE, "worker_image_id": wheel.isolation._WORKER_DIGEST,
               "worker_platform": "linux/amd64",
               "binaries_sha256": {name: "4" * 64 for name in ("docker", "busctl", "systemctl")}}
    attempt = {key: None for key in wheel._ATTEMPT_KEYS}
    attempt.update(schema_version=2, build_id=ident, status="active", phase="prepared",
                   operations_source=operations, recovery_source=saved, source={"commit": commit, "tree": tree},
                   target_ref="wuwaterm-wheel-build:" + ident, context_path=wheel._CONTEXT + ident,
                   probe_name="wuwaterm-wheel-probe-" + ident,
                   isolation=wheel.isolation.new_ledger("wheel", ident, binding),
                   preparation=None, build_tool_wheels=tools)
    return root, attempt


def test_schema2_reader_keeps_cancelled_receipt_and_retention_fail_closed(tmp_path: Path) -> None:
    root, attempt = _active_v2_attempt(tmp_path)
    wheel._write_attempt(root, attempt, new=True)
    name = attempt["build_id"] + ".json"
    assert wheel.read_attempt(root, name) == attempt
    path = root / ".deployments/component-wheel-build-attempts" / name

    def rejects_corrupt_state(message: str) -> None:
        before = path.read_bytes()
        with pytest.raises(wheel.WheelBuildError, match=message):
            wheel._write_attempt(root, attempt)
        assert path.read_bytes() == before
        path.write_bytes(wheel._json(attempt))  # simulate a corrupt external journal
        with pytest.raises(wheel.WheelBuildError, match=message):
            wheel.read_attempt(root, name)
        path.write_bytes(before)

    attempt["phase"] = "unsupported-future-phase"
    rejects_corrupt_state("state")
    attempt["phase"] = "prepared"
    attempt["status"], attempt["phase"], attempt["failure"] = "failed", "failed", "Cancelled"
    attempt["isolation"]["cancel_intent"] = "Cancelled"
    wheel._write_attempt(root, attempt)
    assert wheel.read_attempt(root, name)["status"] == "failed"

    attempt["status"], attempt["phase"] = "retained_import", "retained-import"
    rejects_corrupt_state("retention differs")
    attempt["status"], attempt["phase"], attempt["receipt_sha256"] = "failed", "failed", "1" * 64
    rejects_corrupt_state("receipt")


def test_schema2_refuses_terminal_with_unresolved_creation(tmp_path: Path) -> None:
    root, attempt = _active_v2_attempt(tmp_path)
    attempt.update(status="failed", phase="failed", failure="Cancelled")
    attempt["isolation"]["cancel_intent"] = "Cancelled"
    attempt["isolation"]["parent"].update(create_intent=True, create_outcome="unknown")
    with pytest.raises(wheel.WheelBuildError, match="unresolved creator"):
        wheel._write_attempt(root, attempt, new=True)
    directory = root / ".deployments/component-wheel-build-attempts"
    directory.mkdir(parents=True)
    (directory / (attempt["build_id"] + ".json")).write_bytes(wheel._json(attempt))
    with pytest.raises(wheel.WheelBuildError, match="unresolved creator"):
        wheel.read_attempt(root, attempt["build_id"] + ".json")


def test_cancel_cannot_publish_a_partial_wheel_probe_transition(tmp_path: Path, monkeypatch) -> None:
    root, attempt = _active_v2_attempt(tmp_path)
    ledger = attempt["isolation"]
    unit = ledger["parent"]["name"]
    snapshot = {"name": unit, "description": "WuwaTerm isolated build", "boot_id": ledger["binding"]["boot_id"],
                "invocation_id": "1" * 32, "control_group": "/" + unit,
                "directory": {"device": 1, "inode": 2}, "events": {"device": 1, "inode": 3}, "populated": 0}
    ledger["parent"].update(create_intent=True, create_outcome="bound", snapshot=snapshot)
    ledger["witness"] = {"parent": snapshot, "deleted_container_ids": [], "client_generations": [], "client_fences": []}
    wheel._write_attempt(root, attempt, new=True)
    versions = []

    def write(value: dict) -> None:
        wheel._write_attempt(root, value)
        versions.append(wheel.read_attempt(root, value["build_id"] + ".json"))

    session = wheel.isolation.ProducerSession(root, attempt, write)
    partial, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []
    path = attempt["context_path"] + "/probe-control"
    lines, first = inspect.getsourcelines(session.probe_image)
    pause_line = next(first + index for index, line in enumerate(lines) if 'self.ledger["probe_control"] = ' in line)
    image_id = "sha256:" + "a" * 64
    inspected = subprocess.CompletedProcess([], 0, json.dumps([{"Id": image_id, "Config": {}}]).encode(), b"")
    monkeypatch.setattr(session, "read_docker", lambda *args, **kwargs: inspected)

    def update() -> None:
        try:
            def trace(frame, event, argument):
                if frame.f_code.co_name == "probe_image" and event == "line" and frame.f_lineno == pause_line:
                    sys.settrace(None)
                    partial.set()
                    if not release.wait(5):
                        raise RuntimeError("test release was not delivered")
                return trace

            sys.settrace(trace)
            try:
                session.probe_image(image_id, "fixture probe")
            except wheel.isolation.IsolationError as exc:
                if "probe control directory" not in str(exc):
                    raise
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
    assert all(item["isolation"]["probe_control"]["path"] == path for item in versions)
    assert versions[-1]["isolation"]["cancel_intent"] == "Cancelled"
    assert wheel.read_attempt(root, attempt["build_id"] + ".json") == attempt


def test_killed_wheel_update_is_reconciled_by_its_exact_attempt(tmp_path: Path) -> None:
    root, attempt = _active_v2_attempt(tmp_path)
    wheel._write_attempt(root, attempt, new=True)
    lock = root / ".deployments/.deployment.lock"
    shell = 'set -eu; exec 9>>"$1"; flock -n 9; shift; exec "$@"'

    def execute(program: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["/bin/sh", "-c", shell, "fixture-lock", str(lock), sys.executable, "-I", "-B",
                               "-c", program, str(REPO / "scripts"), str(root), attempt["build_id"]],
                              env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
                                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    crash = """import os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]);import component_wheel_build as w,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=w.read_attempt(root,ident+'.json');a['phase']='wheel-source'
def stop(*args,**kwargs):os.kill(os.getpid(),signal.SIGKILL)
i.os.replace=stop
w._write_attempt(root,a)
"""
    killed = execute(crash)
    assert killed.returncode == -9, killed.stderr
    stage = root / ".deployments/component-wheel-build-staging"
    entries = list(stage.iterdir())
    assert len(entries) == 1 and entries[0].stat().st_nlink == 1
    before = wheel.read_attempt(root, attempt["build_id"] + ".json")
    reconcile = """import pathlib,sys
sys.path.insert(0,sys.argv[1]);import component_wheel_build as w,component_build_isolation as i
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3];a=w.read_attempt(root,ident+'.json')
i.reconcile_attempt_stage(root,'wheel',ident,a)
"""
    recovered = execute(reconcile)
    assert recovered.returncode == 0, recovered.stderr
    assert not list(stage.iterdir()) and wheel.read_attempt(root, attempt["build_id"] + ".json") == before


def test_schema2_receipt_is_not_usable_without_registration(tmp_path: Path) -> None:
    root, attempt = _active_v2_attempt(tmp_path)
    wheel._write_attempt(root, attempt, new=True)
    receipt = wheel._receipt_v2(attempt)
    raw = wheel._json(receipt)
    digest = hashlib.sha256(raw).hexdigest()
    directory = root / ".deployments/product-wheel-builds"
    directory.mkdir()
    receipt_path = directory / (digest + ".json")
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o444)
    with pytest.raises(wheel.WheelBuildError, match="no matching registered producer"):
        wheel.verify_registered_wheel_build(root, digest)
    assert receipt["isolation"] == wheel._receipt_v2(attempt)["isolation"]
    attempt["isolation"]["receipt"] = {"path": "fixture", "sha256": digest, "state": "intent", "remove_intent": False}
    assert receipt["isolation"] == wheel._receipt_v2(attempt)["isolation"]


def _ops_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "operations-fixture"
    extract_archive(REPO, "HEAD", root)
    # Copy the actual in-progress tools into a new isolated Git commit. This is
    # a fixture source identity, not a claim about an official Core release.
    for name in (*wheel.runtime.TOOL_PATHS, "scripts/component_wheel_build.py"):
        src, dest = REPO / name, root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(src.stat().st_mode & 0o777)
    commit, tree = commit_fixture(root)
    (root / ".deployments").mkdir()
    return root, commit, tree


def _invoke(root: Path, commit: str, tree: str, telegram: Path, tg_commit: str, tg_tree: str,
            core: Path, artifact: Path, dependencies: Path, core_source_repo: Path | None = None,
            core_checksums: Path | None = None) -> subprocess.CompletedProcess[str]:
    lock_script = 'set -eu; exec 9>>"$1"; flock -n 9; shift; exec "$@"'
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    if "SUDO_UID" in os.environ:
        env["SUDO_UID"] = os.environ["SUDO_UID"]
    command = ["sh", "-c", lock_script, "wheel-fixture", str(root / ".deployments/.deployment.lock"),
               os.environ["WUWATERM_WHEEL_BUILD_PYTHON"], "-I", "-B", str(root / "scripts/component_wheel_build.py"),
               "--root", str(root), "--operations-commit", commit, "--operations-tree", tree,
               "--telegram-repo", str(telegram), "--telegram-commit", tg_commit, "--telegram-tree", tg_tree,
               "--core-wheel", str(core), "--core-artifact", str(artifact), "--dependencies", str(dependencies),
               "--build-tool-wheelhouse", os.environ["WUWATERM_WHEEL_BUILD_TOOLS"]]
    if core_source_repo is not None:
        command.extend(["--core-source-repo", str(core_source_repo)])
    if core_checksums is not None:
        command.extend(["--core-checksums", str(core_checksums)])
    return subprocess.run(command,
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


@pytest.mark.skipif(os.environ.get("WUWATERM_RUN_WHEEL_DOCKER") != "1", reason="explicit real Docker build only")
def test_real_source_bound_wheel_image_and_failure_boundaries(tmp_path: Path) -> None:
    root, commit, tree = _ops_fixture(tmp_path)
    telegram = Path(os.environ["WUWATERM_WHEEL_TG_REPO"])
    tg_commit, tg_tree = os.environ["WUWATERM_WHEEL_TG_COMMIT"], os.environ["WUWATERM_WHEEL_TG_TREE"]
    core, artifact, dependencies = (Path(os.environ[k]) for k in (
        "WUWATERM_WHEEL_CORE", "WUWATERM_WHEEL_ARTIFACT", "WUWATERM_WHEEL_DEPENDENCIES"))
    print(f"isolated operations fixture: {root}", flush=True)
    result = _invoke(root, commit, tree, telegram, tg_commit, tg_tree, core, artifact, dependencies)
    assert result.returncode == 0, result.stderr
    digest = result.stdout.strip()
    record = wheel.verify_registered_wheel_build(root, digest)
    assert record["source"] == {"commit": tg_commit, "tree": tg_tree}
    assert record["core_source"] == {"commit": "9607f797258ad03c435547a5a377610e1d936530",
                                      "tree": "682cb97d121463846e111e40a24e84db114bac62"}
    assert record["core_wheel_sha256"] == "95f9012154f5883f8414d6d32daf574e2e5d4de8f09b765a3678d0ae4e332853"
    assert record["core_producer_kind"] == "github-actions-artifact"
    assert record["image_revision"] == tg_commit
    assert record["telegram_version"] == "0.1.0.dev1" and record["core_version"] == "0.7.0.dev1"
    inspected = subprocess.run(["docker", "--host", "unix:///run/docker.sock", "image", "inspect", "--format",
                                '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}', record["image_ref"]],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert inspected.returncode == 0
    assert inspected.stdout.strip() == f'{record["image_id"]} {tg_commit}'
    print(f'receipt: {digest}; image: {record["image_id"]}; probe: {record["probe_container_id"]}', flush=True)

    context = root / record["context_path"]
    for relative in ("docker/core-lock.json", "docker/wheels/" + core.name,
                     "source/src/wuwaterm_telegram/poll_health.py", "probe.json", "core-artifact.zip"):
        path = context / relative
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with pytest.raises(wheel.WheelBuildError):
            wheel.verify_registered_wheel_build(root, digest)
        path.write_bytes(original)
    assert wheel.verify_registered_wheel_build(root, digest)["image_id"] == record["image_id"]

    receipts = root / ".deployments/product-wheel-builds"
    before = set(receipts.iterdir())
    wrong_tree = _invoke(root, commit, tree, telegram, tg_commit, "0" * 40, core, artifact, dependencies)
    assert wrong_tree.returncode != 0 and "source tree differs" in wrong_tree.stderr
    assert set(receipts.iterdir()) == before

    bad_core = tmp_path / "tampered-core" / core.name
    bad_core.parent.mkdir()
    bad_core.write_bytes(core.read_bytes() + b"tampered")
    wrong_wheel = _invoke(root, commit, tree, telegram, tg_commit, tg_tree, bad_core, artifact, dependencies)
    assert wrong_wheel.returncode != 0 and "Core lock, source pin, or wheel differs" in wrong_wheel.stderr
    assert set(receipts.iterdir()) == before

    bad_artifact = tmp_path / "tampered-producer.zip"
    bad_artifact.write_bytes(artifact.read_bytes() + b"tampered")
    wrong_artifact = _invoke(root, commit, tree, telegram, tg_commit, tg_tree, core, bad_artifact, dependencies)
    assert wrong_artifact.returncode != 0 and "Core producer artifact differs" in wrong_artifact.stderr
    assert set(receipts.iterdir()) == before

    bad_lock = tmp_path / "synthetic-telegram-lock-mismatch"
    extract_archive(telegram, tg_commit, bad_lock)
    lock_path = bad_lock / "core-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["sha256"] = "0" * 64
    lock_path.write_text(json.dumps(lock))
    bad_commit, bad_tree = commit_fixture(bad_lock)
    lock_failure = _invoke(root, commit, tree, bad_lock, bad_commit, bad_tree, core, artifact, dependencies)
    assert lock_failure.returncode != 0 and "Core lock, source pin, or wheel differs" in lock_failure.stderr
    assert set(receipts.iterdir()) == before

    # These are intentionally synthetic Git repositories, never official TG provenance.
    for variant, line, expected in (
        ("failed-recipe", "INVALID_FIXTURE_INSTRUCTION true", "image build failed"),
        ("installed-mismatch", "RUN python -c \"import pathlib; pathlib.Path('/usr/local/lib/python3.11/site-packages/wuwaterm_telegram/cli.py').write_text('changed')\"",
         "installed package differs"),
    ):
        source = tmp_path / ("synthetic-telegram-" + variant)
        extract_archive(telegram, tg_commit, source)
        with (source / "deploy/Dockerfile").open("a") as stream:
            stream.write("\n" + line + "\n")
        fixture_commit, fixture_tree = commit_fixture(source)
        before = set((root / ".deployments/product-wheel-builds").iterdir())
        failed = _invoke(root, commit, tree, source, fixture_commit, fixture_tree, core, artifact, dependencies)
        assert failed.returncode != 0 and expected in failed.stderr, failed.stderr
        assert set((root / ".deployments/product-wheel-builds").iterdir()) == before
    # This synthetic release lock, manifest and source repository exercise the
    # release protocol only. They are not a published Core release or evidence
    # that the reused development wheel came from this fixture source.
    fixture_core = tmp_path / "synthetic-core-release-source"
    fixture_core.mkdir()
    (fixture_core / "README").write_text("Synthetic release-source fixture\n")
    release_core_commit, release_core_tree = commit_fixture(fixture_core)
    (fixture_core / "README").write_text("A later synthetic checkout state\n")
    git(fixture_core, "add", "README")
    git(fixture_core, "-c", "user.name=Wheel Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: later fixture commit")
    assert git(fixture_core, "rev-parse", "HEAD^{tree}").decode().strip() != release_core_tree
    release_tg = tmp_path / "synthetic-telegram-release-lock"
    extract_archive(telegram, tg_commit, release_tg)
    version = "0.7.0.dev1"
    tag = "fixture-v0.7.0.dev1"
    core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
    release_lock = {"schema": 2, "kind": "github-release", "repository": "My-Denia/WuwaTerm",
                    "version": version, "filename": core.name, "sha256": core_hash,
                    "source_commit": release_core_commit, "tag": tag,
                    "url": f"https://github.com/My-Denia/WuwaTerm/releases/download/{tag}/{core.name}"}
    (release_tg / "core-lock.json").write_text(json.dumps(release_lock))
    release_tg_commit, release_tg_tree = commit_fixture(release_tg)
    release_manifest = tmp_path / "synthetic-release-manifest.json"
    release_manifest.write_text(json.dumps({"tag": tag, "version": version,
                                            "source_commit": release_core_commit, "dry_run": False}))
    checksums = tmp_path / "synthetic-release-SHA256SUMS"
    checksums.write_text(f"{core_hash}  {core.name}\n{'1' * 64}  wuwaterm-{version}.tar.gz\n")
    release_result = _invoke(root, commit, tree, release_tg, release_tg_commit, release_tg_tree,
                             core, release_manifest, dependencies, fixture_core, checksums)
    assert release_result.returncode == 0, release_result.stderr
    release_digest = release_result.stdout.strip()
    release_record = wheel.verify_registered_wheel_build(root, release_digest)
    assert release_record["core_producer_kind"] == "github-release"
    assert release_record["core_source"] == {"commit": release_core_commit, "tree": release_core_tree}
    release_context = root / release_record["context_path"]
    for name in ("core-commit.bin", "core-release-manifest.json", "core-release-SHA256SUMS"):
        target = release_context / name
        original = target.read_bytes()
        target.write_bytes(original + b"tampered")
        with pytest.raises(wheel.WheelBuildError):
            wheel.verify_registered_wheel_build(root, release_digest)
        target.write_bytes(original)
    assert wheel.verify_registered_wheel_build(root, release_digest)["core_source"]["tree"] == release_core_tree
    before = set(receipts.iterdir())
    wrong_repo = _invoke(root, commit, tree, release_tg, release_tg_commit, release_tg_tree,
                         core, release_manifest, dependencies, telegram, checksums)
    assert wrong_repo.returncode != 0
    bad_sums = tmp_path / "synthetic-release-bad-sums"
    bad_sums.write_text(f"{'0' * 64}  {core.name}\n{'1' * 64}  wuwaterm-{version}.tar.gz\n")
    wrong_sums = _invoke(root, commit, tree, release_tg, release_tg_commit, release_tg_tree,
                         core, release_manifest, dependencies, fixture_core, bad_sums)
    assert wrong_sums.returncode != 0 and "release wheel checksum differs" in wrong_sums.stderr
    bad_manifest = tmp_path / "synthetic-release-bad-manifest.json"
    bad_manifest.write_text(json.dumps({"tag": tag, "version": version, "source_commit": "0" * 40, "dry_run": False}))
    wrong_manifest = _invoke(root, commit, tree, release_tg, release_tg_commit, release_tg_tree,
                             core, bad_manifest, dependencies, fixture_core, checksums)
    assert wrong_manifest.returncode != 0 and "release manifest differs" in wrong_manifest.stderr
    assert set(receipts.iterdir()) == before
    print(f"synthetic release-path receipt: {release_digest}; source is a fixture", flush=True)

    attempts = [wheel.read_attempt(root, item.name) for item in
                (root / ".deployments/component-wheel-build-attempts").iterdir()]
    assert sum(item["status"] == "registered" for item in attempts) == 2
    assert sum(item["status"] == "failed" for item in attempts) == 8
    retained = [item for item in attempts if item["status"] == "retained_import"]
    assert len(retained) == 1 and retained[0]["receipt_sha256"] is None
    assert retained[0]["isolation"]["archive"]["state"] == "sealed"
    assert retained[0]["isolation"]["worker"]["absent"] and retained[0]["isolation"]["probe"]["absent"]
    print("wrong source, Core wheel, artifact, lock, invalid recipe and altered installed package refused", flush=True)
