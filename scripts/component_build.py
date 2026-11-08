#!/usr/bin/env python3
"""Produce a locally registered Core runtime image from a verified Git tree.

The candidate and its probe container are never attached to serving services.
The registry is a same-operator trust boundary; it does not authenticate a
malicious user with write access to deployment metadata. No component update
is authorized by producing a receipt.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
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
import component_recovery as recovery  # noqa: E402
import component_build_isolation as isolation  # noqa: E402
import deployment_admission as admission  # noqa: E402
import package_recovery_source as capsule  # noqa: E402
import product_manifest as manifest  # noqa: E402


_SOCKET = "/run/docker.sock"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPTS = (".deployments", "component-build-attempts")
_RECEIPTS = (".deployments", "product-builds")


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


def _registered_result(root: Path, attempt: dict[str, Any]) -> None:
    """Require the completed producer record, not just a matching JSON hash.

    The registry is still a same-operator trust boundary. This catches partial
    or inconsistent records; it is not an attestation against that operator.
    """
    context = root / attempt["context_path"] / "source"
    expected_command = [
        "build", "--pull=false", "--rm=false", "--target", "runtime",
        "--build-arg", "SOURCE_COMMIT=" + attempt["source"]["commit"],
        "--tag", attempt["target_ref"], "--file", str(context / "deploy" / "Dockerfile"), str(context),
    ]
    if attempt["command"] != expected_command:
        raise BuildError("registered build command differs from the producer recipe")
    for name in ("context_sha256", "recipe_sha256", "probe_container_id"):
        _sha(attempt[name])
    result = attempt["build_result"]
    if (type(result) is not dict
            or set(result) != {"exit_code", "output_sha256", "intermediate_prefixes", "intermediate_container_ids"}
            or type(result["exit_code"]) is not int or result["exit_code"] != 0):
        raise BuildError("registered build did not record a successful result")
    _sha(result["output_sha256"])
    prefixes, identifiers = result["intermediate_prefixes"], result["intermediate_container_ids"]
    if (type(prefixes) is not list or type(identifiers) is not list or len(prefixes) != len(identifiers)
            or any(type(prefix) is not str for prefix in prefixes) or len(set(prefixes)) != len(prefixes)):
        raise BuildError("registered build resource list is inconsistent")
    for prefix, identifier in zip(prefixes, identifiers, strict=True):
        if type(prefix) is not str or not re.fullmatch(r"[0-9a-f]{12,64}", prefix):
            raise BuildError("invalid build resource prefix")
        if not _sha(identifier).startswith(prefix):
            raise BuildError("build resource identity differs from its prefix")


def _require_lock(root: Path) -> capsule.DeploymentLock:
    try:
        return capsule.DeploymentLock(root, 9)
    except (capsule.CapsuleError, OSError) as exc:
        raise BuildError("deployment lock FD9 is unavailable or mismatched") from exc


_SOURCE_PROBE = "import importlib.metadata; print(importlib.metadata.version('wuwaterm'))"


def _source_recipe(commit: str) -> list[str]:
    return ["build", "--progress=plain", "--no-cache", "--frontend=dockerfile.v0",
            "--local", "context=/work/context/source", "--local", "dockerfile=/work/context/source/deploy",
            "--opt", "filename=Dockerfile", "--opt", "target=runtime", "--opt", "build-arg:SOURCE_COMMIT=" + commit,
            "--opt", "platform=linux/amd64", "--output", "type=oci,dest=/work/output/image.tar"]


def _read_attempt_v2(root: Path, name: str, value: dict[str, Any]) -> dict[str, Any]:
    keys = {"schema_version", "kind", "build_id", "status", "phase", "source", "target_ref", "context_path",
            "context_sha256", "recipe_sha256", "command", "build_result", "probe_name", "probe_container_id",
            "probe_sha256", "installed_core_version", "receipt_sha256", "failure", "recovery_source", "isolation"}
    phases = {"prepared", "context-intent", "context", "source-intent", "source-ready", "image-exported",
              "probe-verified", "containment-intent", "containment-bound",
              "worker-create-intent", "worker-created", "worker-start-intent", "worker-bound", "worker-ready",
              "work-release-intent", "exhaustion-witness", "archive-intent", "archive-sealed", "import-intent",
              "image-verified", "probe-control-intent", "probe-create-intent", "probe-created", "probe-start-intent",
              "probe-bound", "probe-release-intent", "probe-verified-output", "cleanup", "tag-intent", "tagged",
              "receipt-intent", "receipt-reconcile", "archive-reconcile", "reconcile", "registered", "failed",
              "retained-import"}
    if (set(value) != keys or value["kind"] != "source" or type(value["schema_version"]) is not int
            or value["schema_version"] != 2 or _id(value["build_id"]) + ".json" != name
            or value["status"] not in ("active", "failed", "retained_import", "registered")
            or value["phase"] not in phases):
        raise BuildError("invalid versioned build attempt")
    source = value["source"]
    if type(source) is not dict or set(source) != {"commit", "tree", "tool_bytes_sha256", "source_tool_sha256"}:
        raise BuildError("invalid build source")
    try:
        if runtime.source_tool_identity(source["commit"], source["tree"], source["tool_bytes_sha256"]) != source["source_tool_sha256"]:
            raise BuildError("build source identity differs")
        preserved = recovery._validate_source(value["recovery_source"])
        if any(preserved[key] != source[key] for key in source):
            raise BuildError("preserved build source differs")
        ledger = isolation.validate_ledger(root, "source", value["build_id"], value["isolation"])
    except (runtime.JournalError, recovery.RecoveryError, isolation.IsolationError) as exc:
        raise BuildError("invalid versioned build binding") from exc
    probe = ledger["probe"]
    if probe is not None and probe["spec"] is not None:
        if (probe["spec"]["command"][-1] != _SOURCE_PROBE or ledger["import"] is None
                or probe["spec"]["image_id"] != ledger["import"]["native_image_id"]):
            raise BuildError("source probe recipe or image differs")
    ident = value["build_id"]
    if (value["target_ref"] != "wuwaterm-component-build:" + ident
            or value["context_path"] != ".deployments/component-build-context-" + ident
            or value["probe_name"] != "wuwaterm-source-probe-" + ident
            or value["command"] != _source_recipe(source["commit"])):
        raise BuildError("versioned build recipe differs")
    for client in ledger["clients"]:
        if client["operation"] == "image-build":
            expected = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec", ledger["worker"]["id"],
                        "/usr/bin/buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", *value["command"]]
            if client["argv"] != expected:
                raise BuildError("source solve client recipe differs")
    for key in ("context_sha256", "recipe_sha256", "probe_container_id", "probe_sha256", "receipt_sha256"):
        if value[key] is not None:
            _sha(value[key])
    if value["installed_core_version"] is not None and (type(value["installed_core_version"]) is not str
            or not manifest._VERSION.fullmatch(value["installed_core_version"])):
        raise BuildError("invalid installed Core version")
    result = value["build_result"]
    if result is not None:
        result_keys = {"generation", "operation", "exit_code", "outcome", "stdout_sha256", "stderr_sha256"}
        if (type(result) is not dict or set(result) != result_keys or result["operation"] != "image-build"
                or type(result["generation"]) is not int or not 1 <= result["generation"] <= len(ledger["clients"])
                or type(result["exit_code"]) is not int or result["outcome"] not in ("exited", "cancelled", "timeout")
                or ledger["clients"][result["generation"] - 1]["operation"] != "image-build"):
            raise BuildError("invalid source image build result")
        _sha(result["stdout_sha256"])
        _sha(result["stderr_sha256"])
    terminal = {"failed": "failed", "retained_import": "retained-import", "registered": "registered"}
    if value["status"] in terminal:
        if value["phase"] != terminal[value["status"]]:
            raise BuildError("versioned build terminal phase differs")
    elif value["phase"] in terminal.values():
        raise BuildError("active build has a terminal phase")
    if value["status"] in ("failed", "retained_import"):
        if (type(value["failure"]) is not str or value["failure"] != ledger["cancel_intent"]
                or value["receipt_sha256"] is not None):
            raise BuildError("invalid cancelled build outcome")
    elif value["failure"] is not None:
        raise BuildError("unexpected build failure")
    if value["status"] != "active":
        parent, worker, probe = ledger["parent"], ledger["worker"], ledger["probe"]
        if (parent["create_outcome"] == "unknown" or worker["create_outcome"] == "unknown"
                or (probe is not None and probe["create_outcome"] == "unknown")
                or (parent["create_outcome"] == "bound" and (not parent["absent"] or ledger["witness"] is None))
                or (worker["create_outcome"] == "bound" and not worker["absent"])
                or (probe is not None and probe["create_outcome"] == "bound" and not probe["absent"])
                or any(ledger[key] is not None and not ledger[key]["absent"] for key in ("cache", "output", "probe_control"))):
            raise BuildError("terminal source build has unresolved resources")
        if ledger["witness"] is not None:
            covered = len(ledger["witness"]["client_generations"])
            if any((client["operation"].startswith(("worker-", "probe-")) or client["operation"] == "image-build")
                   and client["generation"] > covered for client in ledger["clients"]):
                raise BuildError("source exhaustion witness does not cover a process generation")
        if (value["status"] in ("failed", "retained_import") and ledger["receipt"] is not None
                and ledger["receipt"]["state"] != "revoked"):
            raise BuildError("cancelled source build retains a receipt")
    if value["status"] == "registered":
        probe, receipt = ledger["probe"], ledger["receipt"]
        if (value["receipt_sha256"] is None or ledger["cancel_intent"] is not None
                or result is None or result["exit_code"] != 0 or result["outcome"] != "exited"
                or value["context_sha256"] is None or value["recipe_sha256"] is None
                or value["installed_core_version"] is None or probe is None or not probe["absent"]
                or value["probe_container_id"] != probe["id"] or value["probe_sha256"] != probe["output_sha256"]
                or receipt is None or receipt["state"] != "registered" or receipt["sha256"] != value["receipt_sha256"]
                or ledger["witness"] is None or not ledger["parent"]["absent"] or not ledger["worker"]["absent"]
                or ledger["retained_reference"] is None):
            raise BuildError("registered versioned build lacks completed proof")
    elif value["receipt_sha256"] is not None:
        raise BuildError("unregistered build exposes a receipt")
    if value["status"] == "retained_import" and (ledger["import"] is None
            or ledger["import"]["released_generation"] is None or not ledger["import"]["verified"]):
        raise BuildError("retained build lacks its import")
    if value["status"] == "failed" and ledger["import"] is not None \
            and ledger["import"]["released_generation"] is not None:
        raise BuildError("failed build has a released import")
    return value


def _read_attempt(root: Path, name: str) -> dict[str, Any]:
    try:
        raw = runtime._read_regular(root.joinpath(*_ATTEMPTS, name))
        value = runtime._decode(raw)
    except runtime.JournalError as exc:
        raise BuildError("invalid build attempt") from exc
    if type(value) is dict and value.get("schema_version") == 2:
        return _read_attempt_v2(root, name, value)
    required = {
        "schema_version", "build_id", "status", "phase", "source", "target_ref", "context_path",
        "context_sha256", "recipe_sha256", "command", "build_result", "probe_name",
        "probe_container_id", "receipt_sha256", "failure", "recovery_source",
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
    if value["status"] == "registered":
        _registered_result(root, value)
    try:
        preserved = recovery._validate_source(value["recovery_source"])
    except recovery.RecoveryError as exc:
        raise BuildError("invalid preserved build source") from exc
    if any(preserved[key] != source[key] for key in source):
        raise BuildError("preserved build source identity differs")
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
        env = capsule.clean_env()
        env["GIT_NO_LAZY_FETCH"] = "1"
        command = ["/usr/bin/git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
                   "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
                   "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.allow=never", *args]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                pass_fds=lock.pass_fds() if lock else (), check=False)
        if result.returncode:
            raise BuildError("cannot verify source Git state")
        return result.stdout
    except (OSError, capsule.CapsuleError) as exc:
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
    try:
        common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode("utf-8").strip()
        if any((Path(common) / "objects" / "pack").glob("*.promisor")):
            raise BuildError("partial-clone source is unsupported")
        env = capsule.clean_env()
        env["GIT_NO_LAZY_FETCH"] = "1"
        configured = subprocess.run(["/usr/bin/git", "-C", str(root), "-c", "protocol.allow=never", "config",
                                     "--local", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if configured.returncode == 0:
            raise BuildError("partial-clone source is unsupported")
        if configured.returncode != 1:
            raise BuildError("cannot verify source Git configuration")
    except (OSError, UnicodeError) as exc:
        raise BuildError("cannot verify source object storage") from exc
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
    ident = _id(value["build_id"])
    try:
        if value.get("schema_version") == 2:
            _read_attempt_v2(root, ident + ".json", value)
        isolation.publish_attempt(root, "source", ident, raw, new=new)
    except (OSError, isolation.IsolationError) as exc:
        raise BuildError("cannot durably record build intent") from exc


def _step(root: Path, attempt: dict[str, Any], phase: str, **updates: Any) -> None:
    attempt.update(updates)
    attempt["phase"] = phase
    _write_attempt(root, attempt)


def _docker(command: list[str], lock: capsule.DeploymentLock, config: Path,
            *, timeout: int = 1800) -> subprocess.CompletedProcess[bytes]:
    if command[:2] != ["image", "inspect"]:
        raise BuildError("classic Docker producer execution is unsupported")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "DOCKER_CONFIG": str(config)}
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
        "recovery_source": attempt["recovery_source"],
    }
    raw = _json(record)
    digest = hashlib.sha256(raw).hexdigest()
    attempt["receipt_sha256"] = digest
    try:
        with manifest._directory(root, _RECEIPTS, create=True) as directory:
            manifest._write_file(directory, digest + ".json", raw, reuse=False)
    except manifest.ManifestError as exc:
        raise BuildError("cannot publish build receipt") from exc
    return record, digest


def _remove_failed_receipt(root: Path, digest: str, build_id: str) -> None:
    """Remove only this producer's receipt if registration did not complete."""
    try:
        with manifest._directory(root, _RECEIPTS, create=False) as directory:
            try:
                raw = manifest._read_file(directory, digest + ".json")
            except manifest.ManifestError:
                try:
                    os.stat(digest + ".json", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise
            record = runtime._decode(raw)
            if hashlib.sha256(raw).hexdigest() != digest or record.get("build_id") != build_id:
                raise BuildError("failed build receipt was replaced")
            os.unlink(digest + ".json", dir_fd=directory)
            os.fsync(directory)
    except (manifest.ManifestError, runtime.JournalError, OSError, AttributeError) as exc:
        raise BuildError("cannot remove an incomplete build receipt") from exc


def _source_receipt_v2(attempt: dict[str, Any]) -> dict[str, Any]:
    ledger = attempt["isolation"]
    fields = ("binding", "parent", "worker", "probe", "archive", "import", "retained_reference", "worker_witness", "witness",
              "context", "cache", "output", "probe_control", "clients")
    evidence = {key: copy.deepcopy(ledger[key]) for key in fields}
    image = ledger["import"]["native_image_id"]
    return {"schema_version": 2, "build_id": attempt["build_id"], "source": copy.deepcopy(attempt["source"]),
            "recovery_source": copy.deepcopy(attempt["recovery_source"]), "context_sha256": attempt["context_sha256"],
            "recipe_sha256": attempt["recipe_sha256"], "command": copy.deepcopy(attempt["command"]),
            "build_result": copy.deepcopy(attempt["build_result"]), "image_ref": attempt["target_ref"],
            "image_id": image, "image_digest": image, "image_revision": attempt["source"]["commit"],
            "installed_core_version": attempt["installed_core_version"],
            "probe_container_id": attempt["probe_container_id"], "probe_sha256": attempt["probe_sha256"],
            "isolation": evidence}


def _verify_registered_v2(root: Path, digest: str, record: dict[str, Any]) -> dict[str, Any]:
    try:
        attempt = _read_attempt(root, _id(record["build_id"]) + ".json")
        if (attempt["schema_version"] != 2 or attempt["status"] != "registered" or attempt["receipt_sha256"] != digest
                or record != _source_receipt_v2(attempt)):
            raise BuildError("source receipt is not registered by its producer")
        recovery.capsule = capsule
        recovery._verify_bootstrap(root, attempt["recovery_source"])
        recovery._verify_capsule(root, attempt["recovery_source"])
        saved = capsule.verify(root / attempt["recovery_source"]["capsule"]["path"],
                               attempt["source"]["commit"], allow_backup=True)
        context = root / attempt["context_path"]
        if (recovery._hash(context / "source.tar") != attempt["context_sha256"]
                or attempt["context_sha256"] != attempt["recovery_source"]["capsule"]["archive_sha256"]
                or recovery._hash(context / "source/deploy/Dockerfile") != attempt["recipe_sha256"]):
            raise BuildError("retained source build context differs")
        _verify_export(context / "source", saved["files"])
        project = tomllib.loads((context / "source/pyproject.toml").read_text(encoding="utf-8"))
        if project["project"]["version"] != attempt["installed_core_version"]:
            raise BuildError("retained source version differs")
        probe = runtime._read_regular(context / "probe.json")
        if (hashlib.sha256(probe).hexdigest() != attempt["probe_sha256"]
                or probe.decode("ascii").strip() != attempt["installed_core_version"]):
            raise BuildError("retained source probe differs")
        session = isolation.ProducerSession(root, attempt, lambda _: None)
        result = attempt["build_result"]
        client = attempt["isolation"]["clients"][result["generation"] - 1]
        expected = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec",
                    attempt["isolation"]["worker"]["id"], "/usr/bin/buildctl", "--addr",
                    "unix:///run/buildkit/buildkitd.sock", *attempt["command"]]
        if (client["argv"] != expected or client["state"] != "completed" or client["exit_code"] != 0
                or client["operation"] != "image-build"):
            raise BuildError("retained source solve differs")
        for field in ("stdout", "stderr"):
            if hashlib.sha256(session.output(result["generation"], field, limit=100 * 1024 * 1024)).hexdigest() != result[field + "_sha256"]:
                raise BuildError("retained source solve output differs")
        with session.sealed_archive() as (_, proof):
            if proof["manifest_digest"] != record["image_id"]:
                raise BuildError("retained source image graph differs")
            isolation.verify_image_revision(proof, attempt["source"]["commit"])
        return record
    except (KeyError, OSError, UnicodeError, tomllib.TOMLDecodeError, isolation.IsolationError,
            recovery.RecoveryError, capsule.CapsuleError, runtime.JournalError) as exc:
        raise BuildError("registered versioned source build cannot be verified") from exc


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
    if type(record) is dict and record.get("schema_version") == 2:
        return _verify_registered_v2(root, digest, record)
    required = {
        "schema_version", "build_id", "source", "context_sha256", "recipe_sha256", "command",
        "result_sha256", "image_ref", "image_id", "image_digest", "image_revision",
        "installed_core_version", "probe_container_id", "intermediate_container_ids", "recovery_source",
    }
    if type(record) is not dict or set(record) != required or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise BuildError("invalid build receipt schema")
    build_id = _id(record["build_id"])
    attempt = _read_attempt(root, build_id + ".json")
    if (attempt["status"] != "registered" or attempt["receipt_sha256"] != digest
            or record["source"] != attempt["source"] or record["context_sha256"] != attempt["context_sha256"]
            or record["recipe_sha256"] != attempt["recipe_sha256"] or record["command"] != attempt["command"]
            or record["image_ref"] != attempt["target_ref"] or record["probe_container_id"] != attempt["probe_container_id"]
            or record["recovery_source"] != attempt["recovery_source"]):
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
    if record["intermediate_container_ids"] != attempt["build_result"]["intermediate_container_ids"]:
        raise BuildError("build resources and receipt differ")
    for value in record["intermediate_container_ids"]:
        _sha(value)
    # A matching JSON document is not itself evidence of the source. Check the
    # retained producer context against the independently saved Git capsule.
    try:
        recovery.capsule = capsule
        recovery._verify_bootstrap(root, record["recovery_source"])
        recovery._verify_capsule(root, record["recovery_source"])
        context = root / attempt["context_path"]
        if (recovery._hash(context / "source.tar") != record["context_sha256"]
                or record["context_sha256"] != record["recovery_source"]["capsule"]["archive_sha256"]
                or recovery._hash(context / "source/deploy/Dockerfile") != record["recipe_sha256"]):
            raise BuildError("retained build context differs from the receipt")
        saved = capsule.verify(root / record["recovery_source"]["capsule"]["path"],
                               record["source"]["commit"], allow_backup=True)
        _verify_export(context / "source", saved["files"])
    except (recovery.RecoveryError, capsule.CapsuleError, OSError) as exc:
        raise BuildError("retained build source cannot be verified") from exc
    return record


def _stage_source(session: isolation.ProducerSession, source: dict[str, str], preserved: dict[str, Any]) -> tuple[str, str, str]:
    context = session.context
    session.save(phase="source-intent")
    capsule_path = session.root / preserved["capsule"]["path"]
    source_archive = capsule_path / "source.tar"
    destination = context / "source.tar"
    source_fd = target_fd = -1
    try:
        saved = capsule.verify(capsule_path, source["commit"], allow_backup=True)
        if saved["source_tree"] != source["tree"] or saved["archive_sha256"] != preserved["capsule"]["archive_sha256"]:
            raise BuildError("saved source archive binding differs")
        source_fd = os.open(source_archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BuildError("saved source archive is unsafe")
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        digest = hashlib.sha256()
        while block := os.read(source_fd, 1024 * 1024):
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise BuildError("cannot copy source archive")
                view = view[written:]
        after = os.fstat(source_fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or digest.hexdigest() != saved["archive_sha256"]):
            raise BuildError("saved source archive changed while copying")
        os.fsync(target_fd)
        capsule.sync_directory(context)
        source_dir = context / "source"
        source_dir.mkdir(mode=0o700)
        capsule.inspect_archive(destination, saved["files"], extract=source_dir, require_tools=False)
        _verify_export(source_dir, saved["files"])
        for directory, _, files in os.walk(source_dir, topdown=False, followlinks=False):
            for name in files:
                fd = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            capsule.sync_directory(Path(directory))
        recipe_path = source_dir / "deploy/Dockerfile"
        recipe_text = recipe_path.read_text(encoding="utf-8")
        if re.search(r"(?im)^[ \t]*#[ \t]*(?:syntax|escape)[ \t]*=", recipe_text):
            raise BuildError("source Dockerfile overrides the verified frontend")
        recipe = capsule.sha(recipe_path)
        project = tomllib.loads((source_dir / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        if type(version) is not str or not manifest._VERSION.fullmatch(version):
            raise BuildError("source Core version is invalid")
        return digest.hexdigest(), recipe, version
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError, capsule.CapsuleError) as exc:
        raise BuildError("cannot prepare verified source context") from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _source_update(session: isolation.ProducerSession, phase: str, **updates: Any) -> None:
    with session.mutex:
        if session.attempt["status"] != "active" or session.ledger["cancel_intent"] is not None:
            raise BuildError("source build cancellation is pending")
        with session.transition(phase=phase):
            session.attempt.update(updates)


def _build_source_v2(root: Path, commit: str, tree: str) -> str:
    root = Path(os.path.abspath(os.fspath(root)))
    if root != CODE_ROOT or root.is_symlink():
        raise BuildError("build source and deployment roots differ")
    _require_lock(root)
    source = _preflight_source(root, commit, tree)
    _gate_journals(root)
    try:
        binding = isolation.preflight(root)
        preserved = recovery.prepare_source(root, commit, tree)
        if any(preserved[key] != source[key] for key in source):
            raise BuildError("preserved build recovery source differs")
    except (isolation.IsolationError, recovery.RecoveryError) as exc:
        raise BuildError("cannot preflight isolated source build") from exc
    ident = manifest.new_deployment_id()
    relative = ".deployments/component-build-context-" + ident
    if os.path.lexists(root / relative):
        raise BuildError("source build context already exists")
    try:
        ledger = isolation.new_ledger("source", ident, binding)
    except isolation.IsolationError as exc:
        raise BuildError("cannot bind isolated source build") from exc
    attempt: dict[str, Any] = {
        "schema_version": 2, "kind": "source", "build_id": ident, "status": "active", "phase": "prepared",
        "source": source, "recovery_source": preserved, "target_ref": "wuwaterm-component-build:" + ident,
        "context_path": relative, "context_sha256": None, "recipe_sha256": None,
        "command": _source_recipe(commit), "build_result": None, "probe_name": "wuwaterm-source-probe-" + ident,
        "probe_container_id": None, "probe_sha256": None, "installed_core_version": None,
        "receipt_sha256": None, "failure": None, "isolation": ledger,
    }
    _write_attempt(root, attempt, new=True)
    session = isolation.ProducerSession(root, attempt, lambda value: _write_attempt(root, value))
    try:
        session.prepare_directories()
        session.open_control()
        archive_hash, recipe_hash, version = _stage_source(session, source, preserved)
        _source_update(session, "source-ready", context_sha256=archive_hash, recipe_sha256=recipe_hash,
                       installed_core_version=version)
        session.create_parent()
        session.create_worker()
        session.wait_worker_ready()
        result = session.solve("image-build", attempt["command"])
        _source_update(session, "image-exported", build_result=result)
        session.stop_worker()
        session.exhaustion_witness()
        session.seal_export()
        image_id = session.import_image()
        with session.sealed_archive() as (_, proof):
            isolation.verify_image_revision(proof, commit)
        probed = session.probe_image(image_id, _SOURCE_PROBE)
        if probed.decode("ascii").strip() != version:
            raise BuildError("installed Core version differs from source")
        probe_path = session.context / "probe.json"
        fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(probed)
            output.flush()
            os.fsync(output.fileno())
        capsule.sync_directory(session.context)
        _source_update(session, "probe-verified", probe_container_id=session.ledger["probe"]["id"],
                       probe_sha256=hashlib.sha256(probed).hexdigest())
        session.stop_probe()
        session.exhaustion_witness()
        session.cleanup_probe_control()
        session.cleanup_cache()
        session.cleanup_output()
        session.remove_parent()
        session.tag_image(attempt["target_ref"])
        with session.mutex:
            if session.ledger["cancel_intent"] is not None:
                raise BuildError("source build cancellation is pending")
            receipt = _source_receipt_v2(attempt)
        digest = session.register(receipt, _RECEIPTS)
        verify_registered_build(root, digest)
        return digest
    except BaseException as exc:
        if attempt["status"] == "registered":
            raise BuildError("registered source build verification is unresolved") from exc
        try:
            session.cancel(type(exc).__name__)
            session.reconcile()
        except Exception as reconcile_error:
            raise BuildError("source build remains active and requires exact-source recovery") from reconcile_error
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, BuildError):
            raise
        raise BuildError("isolated source image build failed") from exc
    finally:
        session.close_control()


def build(root: Path, commit: str, tree: str) -> str:
    """Build a source-bound image only through the isolated producer."""
    return _build_source_v2(root, commit, tree)


def recover_build(root: Path, build_id: str) -> dict[str, Any]:
    """Reconcile a source build through its verified saved source."""
    try:
        return isolation.recover_attempt(root, "source", _id(build_id), _read_attempt, _write_attempt)
    except isolation.IsolationError as exc:
        raise BuildError("source build recovery remains unresolved") from exc


def verify_build_terminal(root: Path, build_id: str) -> dict[str, Any]:
    """Verify a retryable source-build outcome without changing it."""
    try:
        return isolation.verify_attempt_terminal(root, "source", _id(build_id), _read_attempt)
    except isolation.IsolationError as exc:
        raise BuildError("source build terminal state differs") from exc


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
