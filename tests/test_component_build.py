from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

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
    for relative in ("scripts/product_manifest.py", "scripts/component_runtime.py", "scripts/component_build.py",
                     "scripts/deployment_admission.py", "scripts/deployment_manifest.py", "scripts/full_update.py",
                     "scripts/runtime_update.py", "scripts/package_recovery_source.py", "deploy/vps-update.sh"):
        destination = root / relative
        if (repo / relative).exists() and (repo == REPO or not destination.exists()):
            destination.parent.mkdir(parents=True, exist_ok=True)
            was_missing = not destination.exists()
            shutil.copyfile(repo / relative, destination)
            if was_missing:
                paths.append(relative)
    # These two fixture-only files are not executed by the candidate build;
    # they stand in for the independently developed dispatcher and shell while
    # testing the source-bound Core Docker recipe on this isolated commit.
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


def test_candidate_build_stays_closed_until_crash_recovery_gate_is_ready(exported_git) -> None:
    root, commit, tree = exported_git
    result = invoke_with_lock(root, commit, tree)
    assert result.returncode != 0
    assert "disabled pending cross-tool crash-recovery gating" in result.stderr
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
    inspect = subprocess.run(
        ["docker", "--host", "unix:///run/docker.sock", "image", "inspect", "--format", "{{.Id}}", receipt["image_ref"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env(), check=False,
    )
    assert inspect.returncode == 0
    assert inspect.stdout.strip() == receipt["image_id"]
    print(f"receipt: {digest}; image: {receipt['image_id']}; probe: {receipt['probe_container_id']}", flush=True)
