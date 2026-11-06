#!/usr/bin/env python3
"""Produce a locally registered Core runtime image from a verified Git tree.

The candidate and its probe container are never attached to serving services.
The registry is a same-operator trust boundary; it does not authenticate a
malicious user with write access to deployment metadata. No component update
is authorized by producing a receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tomllib
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "scripts"))
sys.path.insert(0, str(CODE_ROOT / "src"))
import component_runtime as runtime  # noqa: E402
import deployment_admission as admission  # noqa: E402
import package_recovery_source as capsule  # noqa: E402
import product_manifest as manifest  # noqa: E402


_SOCKET = "/run/docker.sock"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPTS = (".deployments", "component-build-attempts")
_RECEIPTS = (".deployments", "product-builds")
# Legacy tools do not yet gate unresolved component-runtime transactions.
# Keep the candidate producer inactive until that interoperability and crash
# recovery are resolved, even though its isolated build machinery is present.
BUILD_EXECUTION_READY = False


class BuildError(RuntimeError):
    """A candidate cannot be built or its provenance cannot be established."""


def _json(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError) as exc:
        raise BuildError("invalid build record") from exc


def _sha(value: Any) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise BuildError("invalid SHA-256")
    return value


def _id(value: Any) -> str:
    if type(value) is not str or not runtime._DEPLOYMENT_ID.fullmatch(value):
        raise BuildError("invalid build attempt ID")
    return value


def _require_lock(root: Path) -> capsule.DeploymentLock:
    try:
        return capsule.DeploymentLock(root, 9)
    except (capsule.CapsuleError, OSError) as exc:
        raise BuildError("deployment lock FD9 is unavailable or mismatched") from exc


def _read_attempt(root: Path, name: str) -> dict[str, Any]:
    try:
        raw = runtime._read_regular(root.joinpath(*_ATTEMPTS, name))
        value = runtime._decode(raw)
    except runtime.JournalError as exc:
        raise BuildError("invalid build attempt") from exc
    required = {
        "schema_version", "build_id", "status", "phase", "source", "target_ref", "context_path",
        "context_sha256", "recipe_sha256", "command", "build_result", "probe_name",
        "probe_container_id", "receipt_sha256", "failure",
    }
    if type(value) is not dict or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BuildError("invalid build attempt schema")
    if _id(value["build_id"]) + ".json" != name:
        raise BuildError("build attempt ID mismatch")
    if value["status"] not in ("active", "failed", "registered"):
        raise BuildError("unknown build attempt status")
    if type(value["phase"]) is not str or value["phase"] not in (
        "prepared", "context", "build", "inspect", "probe-create", "probe-start", "receipt", "registered", "failed",
    ):
        raise BuildError("unknown build attempt phase")
    if (value["status"] == "registered") != (value["phase"] == "registered"):
        raise BuildError("inconsistent build attempt status")
    if (value["status"] == "failed") != (value["phase"] == "failed"):
        raise BuildError("inconsistent build failure status")
    source = value["source"]
    if type(source) is not dict or set(source) != {"commit", "tree", "tool_bytes_sha256", "source_tool_sha256"}:
        raise BuildError("invalid build source")
    try:
        expected = runtime.source_tool_identity(source["commit"], source["tree"], source["tool_bytes_sha256"])
    except runtime.JournalError as exc:
        raise BuildError("invalid build source") from exc
    if source["source_tool_sha256"] != expected:
        raise BuildError("build source/tool identity mismatch")
    if value["target_ref"] != "wuwaterm-component-build:" + value["build_id"]:
        raise BuildError("invalid build image reference")
    if value["context_path"] != ".deployments/component-build-context-" + value["build_id"]:
        raise BuildError("invalid build context reference")
    if value["probe_name"] != "wuwaterm-component-probe-" + value["build_id"]:
        raise BuildError("invalid build probe name")
    for key in ("context_sha256", "recipe_sha256", "receipt_sha256"):
        if value[key] is not None:
            _sha(value[key])
    if value["probe_container_id"] is not None:
        _sha(value["probe_container_id"])
    if value["status"] == "registered" and value["receipt_sha256"] is None:
        raise BuildError("registered build has no receipt")
    if value["status"] == "failed" and type(value["failure"]) is not str:
        raise BuildError("failed build has no failure code")
    if value["status"] != "failed" and value["failure"] is not None:
        raise BuildError("unexpected build failure code")
    return value


def _gate_journals(root: Path) -> None:
    try:
        admission.admit_component(root, operation="build", code_root=CODE_ROOT)
    except admission.AdmissionError as exc:
        raise BuildError(f"deployment admission refused: {exc}") from exc


def _git(root: Path, *args: str, lock: capsule.DeploymentLock | None = None) -> bytes:
    try:
        return capsule.git(root, *args, lock=lock)
    except capsule.CapsuleError as exc:
        raise BuildError("cannot verify source Git state") from exc


def _preflight_source(root: Path, commit: str, tree: str) -> dict[str, str]:
    if root != CODE_ROOT or root.resolve(strict=True) != root:
        raise BuildError("build source and deployment must be the same physical checkout")
    try:
        runtime._match(commit, runtime._HEX40, "source commit")
        runtime._match(tree, runtime._HEX40, "source tree")
    except runtime.JournalError as exc:
        raise BuildError("invalid source identity") from exc
    top = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    actual_tree = _git(root, "rev-parse", "--verify", f"{commit}^{{tree}}").decode("ascii").strip()
    if top != str(root) or head != commit or actual_tree != tree:
        raise BuildError("source commit, tree, or checkout mismatch")
    if _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no"):
        raise BuildError("tracked source checkout is not clean")
    _git(root, "ls-files", "--error-unmatch", "--", *runtime.TOOL_PATHS)
    try:
        tool_bytes = runtime.tool_bytes_identity(root)
        source_id = runtime.source_tool_identity(commit, tree, tool_bytes)
    except (runtime.JournalError, OSError) as exc:
        raise BuildError("cannot fingerprint source tools") from exc
    return {"commit": commit, "tree": tree, "tool_bytes_sha256": tool_bytes,
            "source_tool_sha256": source_id}


def _write_attempt(root: Path, value: dict[str, Any], *, new: bool = False) -> None:
    raw = _json(value)
    name = _id(value["build_id"]) + ".json"
    try:
        with manifest._directory(root, _ATTEMPTS, create=True) as directory:
            temporary = ".attempt-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if new:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=directory)
                else:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise BuildError("build attempt was replaced")
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except (OSError, manifest.ManifestError) as exc:
        raise BuildError("cannot durably record build intent") from exc


def _step(root: Path, attempt: dict[str, Any], phase: str, **updates: Any) -> None:
    attempt.update(updates)
    attempt["phase"] = phase
    _write_attempt(root, attempt)


def _docker(command: list[str], lock: capsule.DeploymentLock, config: Path,
            *, timeout: int = 1800) -> subprocess.CompletedProcess[bytes]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "DOCKER_CONFIG": str(config), "DOCKER_BUILDKIT": "0"}
    try:
        return subprocess.run(["docker", "--host", f"unix://{_SOCKET}", *command],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              pass_fds=lock.pass_fds(), timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired, capsule.CapsuleError) as exc:
        raise BuildError("Docker command could not complete") from exc


def _inspect_image(image: str, lock: capsule.DeploymentLock, config: Path) -> tuple[str, str]:
    result = _docker(["image", "inspect", "--format", '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}', image], lock, config, timeout=60)
    if result.returncode:
        raise BuildError("candidate image could not be inspected")
    try:
        fields = result.stdout.decode("ascii").strip().split()
    except UnicodeError as exc:
        raise BuildError("candidate image identity is malformed") from exc
    if len(fields) != 2 or not fields[0].startswith("sha256:"):
        raise BuildError("candidate image identity is malformed")
    _sha(fields[0][7:])
    return fields[0], fields[1]


def _verify_export(root: Path, files: dict[str, dict[str, Any]]) -> None:
    """Check the on-disk build context against the actual Git tree."""
    names: set[str] = set()
    for directory, directories, filenames in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise BuildError("source export contains a symlink")
        for name in filenames:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative not in files:
                raise BuildError("source export contains an untracked file")
            try:
                content = runtime._read_regular(path)
                mode = path.stat(follow_symlinks=False).st_mode
            except (runtime.JournalError, OSError) as exc:
                raise BuildError("source export cannot be verified") from exc
            expected = files[relative]
            if (len(content) != expected["size"] or hashlib.sha256(content).hexdigest() != expected["sha256"]
                    or bool(mode & 0o111) != (expected["mode"] == "100755")):
                raise BuildError("source export differs from tracked tree")
            names.add(relative)
    if names != set(files):
        raise BuildError("source export is incomplete")


def _receipt(root: Path, attempt: dict[str, Any], image_id: str, version: str,
             intermediate_ids: list[str]) -> tuple[dict[str, Any], str]:
    record = {
        "schema_version": 1, "build_id": attempt["build_id"], "source": attempt["source"],
        "context_sha256": attempt["context_sha256"], "recipe_sha256": attempt["recipe_sha256"],
        "command": attempt["command"], "result_sha256": attempt["build_result"]["output_sha256"],
        "image_ref": attempt["target_ref"], "image_id": image_id,
        "image_digest": image_id, "image_revision": attempt["source"]["commit"],
        "installed_core_version": version, "probe_container_id": attempt["probe_container_id"],
        "intermediate_container_ids": intermediate_ids,
    }
    raw = _json(record)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with manifest._directory(root, _RECEIPTS, create=True) as directory:
            manifest._write_file(directory, digest + ".json", raw, reuse=True)
    except manifest.ManifestError as exc:
        raise BuildError("cannot publish build receipt") from exc
    return record, digest


def verify_registered_build(root: Path, receipt_sha256: str) -> dict[str, Any]:
    """Verify a local registry entry and its durable successful producer attempt.

    The caller must independently observe the actual image ID, tag and revision
    immediately before starting any serving container.
    """
    digest = _sha(receipt_sha256)
    try:
        with manifest._directory(root, _RECEIPTS, create=False) as directory:
            raw = manifest._read_file(directory, digest + ".json")
        record = runtime._decode(raw)
    except (manifest.ManifestError, runtime.JournalError) as exc:
        raise BuildError("build receipt is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise BuildError("build receipt digest mismatch")
    required = {
        "schema_version", "build_id", "source", "context_sha256", "recipe_sha256", "command",
        "result_sha256", "image_ref", "image_id", "image_digest", "image_revision",
        "installed_core_version", "probe_container_id", "intermediate_container_ids",
    }
    if type(record) is not dict or set(record) != required or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise BuildError("invalid build receipt schema")
    build_id = _id(record["build_id"])
    attempt = _read_attempt(root, build_id + ".json")
    if (attempt["status"] != "registered" or attempt["receipt_sha256"] != digest
            or record["source"] != attempt["source"] or record["context_sha256"] != attempt["context_sha256"]
            or record["recipe_sha256"] != attempt["recipe_sha256"] or record["command"] != attempt["command"]
            or record["image_ref"] != attempt["target_ref"] or record["probe_container_id"] != attempt["probe_container_id"]):
        raise BuildError("build receipt is not registered by its producer attempt")
    if type(attempt["build_result"]) is not dict or record["result_sha256"] != attempt["build_result"].get("output_sha256"):
        raise BuildError("build result and receipt differ")
    _sha(record["context_sha256"])
    _sha(record["recipe_sha256"])
    _sha(record["result_sha256"])
    _sha(record["probe_container_id"])
    if not isinstance(record["image_id"], str) or not record["image_id"].startswith("sha256:"):
        raise BuildError("invalid receipt image ID")
    _sha(record["image_id"][7:])
    if record["image_digest"] != record["image_id"] or record["image_revision"] != record["source"]["commit"]:
        raise BuildError("receipt image binding differs")
    if not isinstance(record["installed_core_version"], str) or not manifest._VERSION.fullmatch(record["installed_core_version"]):
        raise BuildError("invalid installed Core version")
    if type(record["intermediate_container_ids"]) is not list:
        raise BuildError("invalid build resource list")
    for value in record["intermediate_container_ids"]:
        _sha(value)
    return record


def build(root: Path, commit: str, tree: str) -> str:
    """Build from HEAD under the shared lock; return the registered receipt SHA."""
    root = Path(os.path.abspath(os.fspath(root)))
    if root != CODE_ROOT or root.is_symlink():
        raise BuildError("build source and deployment roots differ")
    lock = _require_lock(root)
    _gate_journals(root)
    source = _preflight_source(root, commit, tree)
    if not BUILD_EXECUTION_READY:
        raise BuildError("candidate build is disabled pending cross-tool crash-recovery gating")
    try:
        info = os.stat(_SOCKET, follow_symlinks=False)
    except OSError as exc:
        raise BuildError("local Docker socket is unavailable") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise BuildError("local Docker endpoint is not a Unix socket")
    build_id = manifest.new_deployment_id()
    context_relative = ".deployments/component-build-context-" + build_id
    context = root / context_relative
    target_ref = "wuwaterm-component-build:" + build_id
    attempt: dict[str, Any] = {
        "schema_version": 1, "build_id": build_id, "status": "active", "phase": "prepared",
        "source": source, "target_ref": target_ref, "context_path": context_relative,
        "context_sha256": None, "recipe_sha256": None, "command": None, "build_result": None,
        "probe_name": "wuwaterm-component-probe-" + build_id, "probe_container_id": None,
        "receipt_sha256": None, "failure": None,
    }
    _write_attempt(root, attempt, new=True)
    try:
        context.mkdir(mode=0o700)
        source_dir = context / "source"
        source_dir.mkdir(mode=0o700)
        docker_config = context / "docker-config"
        docker_config.mkdir(mode=0o700)
        archive = context / "source.tar"
        with archive.open("xb") as output:
            result = subprocess.run(
                ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "archive", "--format=tar", commit],
                stdout=output, stderr=subprocess.PIPE, env=capsule.clean_env(),
                pass_fds=lock.pass_fds(), check=False,
            )
            output.flush()
            os.fsync(output.fileno())
        if result.returncode:
            raise BuildError("source archive command failed")
        actual_tree, files = capsule.tree_files(root, commit)
        if actual_tree != tree:
            raise BuildError("source tree changed")
        capsule.inspect_archive(archive, files, extract=source_dir, require_tools=False)
        _verify_export(source_dir, files)
        archive_sha = capsule.sha(archive)
        recipe = source_dir / "deploy" / "Dockerfile"
        recipe_sha = capsule.sha(recipe)
        try:
            version = tomllib.loads((source_dir / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise BuildError("source Core version is invalid") from exc
        if type(version) is not str or not manifest._VERSION.fullmatch(version):
            raise BuildError("source Core version is invalid")
        command = ["build", "--pull=false", "--rm=false", "--target", "runtime",
                   "--build-arg", "SOURCE_COMMIT=" + commit, "--tag", target_ref,
                   "--file", str(recipe), str(source_dir)]
        _step(root, attempt, "context", context_sha256=archive_sha, recipe_sha256=recipe_sha, command=command)
        _step(root, attempt, "build")
        result = _docker(command, lock, docker_config)
        _verify_export(source_dir, files)
        output_sha = hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()
        intermediate = sorted(set(re.findall(rb"Running in ([0-9a-f]{12,64})", result.stdout)))
        attempt["build_result"] = {"exit_code": result.returncode, "output_sha256": output_sha,
                                   "intermediate_prefixes": [value.decode("ascii") for value in intermediate],
                                   "intermediate_container_ids": []}
        _step(root, attempt, "inspect")
        intermediate_ids = []
        for prefix in attempt["build_result"]["intermediate_prefixes"]:
            observed = _docker(["inspect", "--format", "{{.Id}}", prefix], lock, docker_config, timeout=60)
            try:
                value = observed.stdout.decode("ascii").strip()
            except UnicodeError as exc:
                raise BuildError("build resource ID is invalid") from exc
            if observed.returncode:
                raise BuildError("build resource could not be inspected")
            intermediate_ids.append(_sha(value))
            attempt["build_result"]["intermediate_container_ids"] = intermediate_ids.copy()
            _write_attempt(root, attempt)
        if result.returncode:
            raise BuildError("Core Docker build failed")
        image_id, revision = _inspect_image(target_ref, lock, docker_config)
        if revision != commit:
            raise BuildError("built image revision differs from source")
        _step(root, attempt, "probe-create")
        probe = _docker(["create", "--name", attempt["probe_name"], "--network", "none",
                         "--label", "org.wuwaterm.component-build=" + build_id, "--entrypoint", "python",
                         image_id, "-c", "import importlib.metadata; print(importlib.metadata.version('wuwaterm'))"],
                        lock, docker_config, timeout=60)
        if probe.returncode:
            raise BuildError("candidate version probe could not be created")
        try:
            probe_id = _sha(probe.stdout.decode("ascii").strip())
        except UnicodeError as exc:
            raise BuildError("candidate probe ID is invalid") from exc
        _step(root, attempt, "probe-start", probe_container_id=probe_id)
        probed = _docker(["start", "--attach", probe_id], lock, docker_config, timeout=60)
        try:
            actual_version = probed.stdout.decode("ascii").strip()
        except UnicodeError as exc:
            raise BuildError("installed Core version is invalid") from exc
        if probed.returncode or actual_version != version:
            raise BuildError("installed Core version differs from source")
        if _inspect_image(target_ref, lock, docker_config) != (image_id, commit):
            raise BuildError("candidate image reference drifted")
        _step(root, attempt, "receipt")
        _, digest = _receipt(root, attempt, image_id, version, intermediate_ids)
        attempt.update(status="registered", phase="registered", receipt_sha256=digest)
        _write_attempt(root, attempt)
        verify_registered_build(root, digest)
        return digest
    except Exception as exc:
        attempt.update(status="failed", phase="failed", failure=type(exc).__name__)
        try:
            _write_attempt(root, attempt)
        except BuildError as persist_error:
            raise BuildError("build failed and failure intent could not be preserved") from persist_error
        if isinstance(exc, BuildError):
            raise
        raise BuildError("candidate build failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("build")
    create.add_argument("--source-commit", required=True)
    create.add_argument("--source-tree", required=True)
    check = subparsers.add_parser("verify-receipt")
    check.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(build(args.root, args.source_commit, args.source_tree))
        else:
            verify_registered_build(args.root, args.sha256)
    except BuildError as exc:
        print(f"component build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
