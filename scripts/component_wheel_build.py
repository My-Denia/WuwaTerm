#!/usr/bin/env python3
"""Build and register a Telegram image from a Git-bound wheel and Core lock.

The registry is a same-operator trust boundary, not an attestation against an
operator who can rewrite deployment metadata. Builds use no network and do
not change a running service or a deployment pointer.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import copy
import email.parser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
import zlib
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_build as build  # noqa: E402
import component_build_isolation as isolation  # noqa: E402
import component_recovery as recovery  # noqa: E402
import component_runtime as runtime  # noqa: E402
import package_recovery_source as capsule  # noqa: E402
import product_manifest as manifest  # noqa: E402

_ATTEMPTS = (".deployments", "component-wheel-build-attempts")
_RECEIPTS = (".deployments", "product-wheel-builds")
_CONTEXT = ".deployments/component-wheel-build-context-"
_CONTEXT_FILES = (
    "core-lock.json", "pyproject.toml", "src/wuwaterm_telegram/core_compat.py",
    "scripts/core_lock.py", "scripts/verify_wheels.py", "scripts/install_verified.py",
)
_ATTEMPT_KEYS = {
    "schema_version", "build_id", "status", "phase", "operations_source", "recovery_source",
    "source", "target_ref", "context_path", "telegram_wheel_sha256", "telegram_version",
    "core_wheel_sha256", "core_version", "core_lock_sha256", "core_artifact_sha256", "core_checksums_sha256",
    "core_source", "core_producer_kind", "dependencies", "source_archive_sha256", "context_sha256",
    "recipe_sha256", "wheel_build", "command", "build_result", "probe_name", "probe_container_id",
    "probe_sha256", "image_id", "image_revision", "receipt_sha256", "failure",
}
_PROBE = """import hashlib,importlib.metadata as m,json
out={}
for name,prefixes in [('wuwaterm',('wuwaterm/','wuwaterm_api/')),('wuwaterm-telegram',('wuwaterm_telegram/',))]:
 d=m.distribution(name); files={}
 for f in d.files or []:
  s=str(f)
  if s.startswith(prefixes) and not s.endswith('.pyc'):
   p=d.locate_file(f)
   if p.is_file(): files[s]=hashlib.sha256(p.read_bytes()).hexdigest()
 out[name]={'version':d.version,'files':files,'direct_url':json.loads(d.read_text('direct_url.json'))}
print(json.dumps(out,sort_keys=True))
"""


class WheelBuildError(build.BuildError):
    """A wheel image or its recorded inputs cannot be verified."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    try:
        return recovery._read_regular(path, limit=100 * 1024 * 1024)
    except recovery.RecoveryError as exc:
        raise WheelBuildError("cannot read a regular build input") from exc


def _safe_digest(value: Any) -> str:
    try:
        return build._sha(value)
    except build.BuildError as exc:
        raise WheelBuildError("invalid digest") from exc


def _git_id(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise WheelBuildError("invalid Git object ID")
    return value


def _json(value: Any) -> bytes:
    return build._json(value)


def _write_attempt(root: Path, value: dict[str, Any], *, new: bool = False) -> None:
    ident = build._id(value["build_id"])
    raw = _json(value)
    try:
        if value.get("schema_version") == 2:
            _read_v2_attempt(root, ident + ".json", value)
        isolation.publish_attempt(root, "wheel", ident, raw, new=new)
    except (OSError, isolation.IsolationError) as exc:
        raise WheelBuildError("cannot durably record wheel build intent") from exc


def _step(root: Path, attempt: dict[str, Any], phase: str, **updates: Any) -> None:
    attempt.update(updates, phase=phase)
    _write_attempt(root, attempt)


def _command(root: Path, attempt: dict[str, Any]) -> list[str]:
    context = root / attempt["context_path"]
    return ["build", "--network=none", "--pull=false", "--rm=false",
            "--build-arg", "PIP_NO_INDEX=1", "--build-arg", "PIP_FIND_LINKS=/app/wheels/dependencies",
            "--build-arg", "WUWATERM_SOURCE_REVISION=" + attempt["source"]["commit"],
            "--tag", attempt["target_ref"], "--file", str(context / "source/deploy/Dockerfile"),
            str(context / "docker")]


def _read_attempt(root: Path, name: str) -> dict[str, Any]:
    try:
        value = runtime._decode(_read(root.joinpath(*_ATTEMPTS, name)))
        if type(value) is dict and type(value.get("schema_version")) is int and value["schema_version"] == 2:
            return _read_v2_attempt(root, name, value)
        if type(value) is not dict or set(value) != _ATTEMPT_KEYS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise WheelBuildError("invalid wheel attempt schema")
        ident = build._id(value["build_id"])
        if name != ident + ".json" or value["target_ref"] != "wuwaterm-wheel-build:" + ident \
                or value["context_path"] != _CONTEXT + ident or value["probe_name"] != "wuwaterm-wheel-probe-" + ident:
            raise WheelBuildError("wheel attempt identity mismatch")
        if value["status"] not in ("active", "failed", "registered") or value["phase"] not in (
            "prepared", "context", "wheel", "build", "inspect", "probe-create", "probe-start", "receipt", "registered", "failed"
        ) or ((value["status"] == "registered") != (value["phase"] == "registered")) \
                or ((value["status"] == "failed") != (value["phase"] == "failed")):
            raise WheelBuildError("invalid wheel attempt state")
        if (value["status"] == "failed") != (type(value["failure"]) is str):
            raise WheelBuildError("invalid wheel attempt failure")
        if value["failure"] is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", value["failure"]):
            raise WheelBuildError("invalid wheel attempt failure code")
        source = value["source"]
        if type(source) is not dict or set(source) != {"commit", "tree"}:
            raise WheelBuildError("invalid Telegram source identity")
        _git_id(source["commit"])
        _git_id(source["tree"])
        operations = value["operations_source"]
        saved = recovery._validate_source(value["recovery_source"])
        if type(operations) is not dict or set(operations) != {"commit", "tree", "tool_bytes_sha256", "source_tool_sha256"} \
                or any(saved[k] != operations[k] for k in operations):
            raise WheelBuildError("operations source identity differs")
        for k in ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_artifact_sha256", "core_checksums_sha256", "source_archive_sha256", "context_sha256",
                  "recipe_sha256", "probe_container_id", "probe_sha256", "receipt_sha256"):
            if value[k] is not None:
                _safe_digest(value[k])
        for k in ("telegram_version", "core_version"):
            if value[k] is not None and (type(value[k]) is not str or not manifest._VERSION.fullmatch(value[k])):
                raise WheelBuildError("invalid wheel version")
        if value["core_source"] is not None:
            if type(value["core_source"]) is not dict or set(value["core_source"]) != {"commit", "tree"}:
                raise WheelBuildError("invalid Core source identity")
            _git_id(value["core_source"]["commit"])
            _git_id(value["core_source"]["tree"])
        if value["core_producer_kind"] not in (None, "github-actions-artifact", "github-release"):
            raise WheelBuildError("unsupported Core producer")
        if value["dependencies"] is not None:
            if type(value["dependencies"]) is not dict or not value["dependencies"]:
                raise WheelBuildError("invalid dependency wheel list")
            for dep_name, dep_hash in value["dependencies"].items():
                if type(dep_name) is not str or Path(dep_name).name != dep_name or not dep_name.endswith(".whl"):
                    raise WheelBuildError("invalid dependency wheel name")
                _safe_digest(dep_hash)
        if value["command"] is not None and value["command"] != _command(root, value):
            raise WheelBuildError("wheel build command differs from recipe")
        if value["wheel_build"] is not None:
            wb = value["wheel_build"]
            if type(wb) is not dict or set(wb) != {"command", "exit_code", "output_sha256"} \
                    or type(wb["command"]) is not list or len(wb["command"]) != 8 \
                    or type(wb["command"][0]) is not str or not os.path.isabs(wb["command"][0]) \
                    or wb["command"][1:] != ["-m", "build", "--wheel", "--no-isolation", "--outdir",
                                             str(root / value["context_path"] / "wheel-output"),
                                             str(root / value["context_path"] / "wheel-source")] \
                    or type(wb["exit_code"]) is not int:
                raise WheelBuildError("invalid wheel build result")
            _safe_digest(wb["output_sha256"])
        if value["build_result"] is not None:
            result = value["build_result"]
            if type(result) is not dict or set(result) != {"exit_code", "output_sha256", "intermediate_prefixes", "intermediate_container_ids"} \
                    or type(result["exit_code"]) is not int or type(result["intermediate_prefixes"]) is not list \
                    or type(result["intermediate_container_ids"]) is not list \
                    or len(result["intermediate_container_ids"]) > len(result["intermediate_prefixes"]):
                raise WheelBuildError("invalid image build result")
            _safe_digest(result["output_sha256"])
            if any(type(prefix) is not str or not re.fullmatch(r"[0-9a-f]{12,64}", prefix)
                   for prefix in result["intermediate_prefixes"]) \
                    or len(set(result["intermediate_prefixes"])) != len(result["intermediate_prefixes"]):
                raise WheelBuildError("invalid intermediate resource prefixes")
            for prefix, resource in zip(result["intermediate_prefixes"], result["intermediate_container_ids"]):
                if not _safe_digest(resource).startswith(prefix):
                    raise WheelBuildError("intermediate resource differs")
        if value["image_id"] is not None:
            if type(value["image_id"]) is not str or not value["image_id"].startswith("sha256:"):
                raise WheelBuildError("invalid image ID")
            _safe_digest(value["image_id"][7:])
        if value["image_revision"] is not None and value["image_revision"] != source["commit"]:
            raise WheelBuildError("image revision differs")
        if value["status"] == "registered":
            for k in ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_artifact_sha256", "source_archive_sha256", "context_sha256",
                      "recipe_sha256", "probe_container_id", "probe_sha256", "receipt_sha256"):
                _safe_digest(value[k])
            for k in ("telegram_version", "core_version"):
                if type(value[k]) is not str or not manifest._VERSION.fullmatch(value[k]):
                    raise WheelBuildError("invalid wheel version")
            if type(value["core_source"]) is not dict or set(value["core_source"]) != {"commit", "tree"}:
                raise WheelBuildError("invalid Core source identity")
            _git_id(value["core_source"]["commit"])
            _git_id(value["core_source"]["tree"])
            if value["core_producer_kind"] not in ("github-actions-artifact", "github-release"):
                raise WheelBuildError("unsupported Core producer")
            if value["core_producer_kind"] == "github-release":
                _safe_digest(value["core_checksums_sha256"])
            elif value["core_checksums_sha256"] is not None:
                raise WheelBuildError("unexpected Core release checksums")
            if value["command"] != _command(root, value):
                raise WheelBuildError("wheel build command differs from recipe")
            wb = value["wheel_build"]
            if wb is None or wb["exit_code"] != 0:
                raise WheelBuildError("wheel build failed")
            _safe_digest(wb["output_sha256"])
            result = value["build_result"]
            if type(result) is not dict or set(result) != {"exit_code", "output_sha256", "intermediate_prefixes", "intermediate_container_ids"} \
                    or type(result["exit_code"]) is not int or result["exit_code"] != 0:
                raise WheelBuildError("image build failed")
            _safe_digest(result["output_sha256"])
            prefixes, ids = result["intermediate_prefixes"], result["intermediate_container_ids"]
            if type(prefixes) is not list or type(ids) is not list or len(prefixes) != len(ids) or len(set(prefixes)) != len(prefixes):
                raise WheelBuildError("invalid intermediate resources")
            for prefix, ident in zip(prefixes, ids, strict=True):
                if type(prefix) is not str or not re.fullmatch(r"[0-9a-f]{12,64}", prefix) or not _safe_digest(ident).startswith(prefix):
                    raise WheelBuildError("intermediate resource differs")
            if type(value["image_id"]) is not str or not value["image_id"].startswith("sha256:"):
                raise WheelBuildError("invalid image ID")
            _safe_digest(value["image_id"][7:])
            if value["image_revision"] != source["commit"]:
                raise WheelBuildError("image revision differs")
        return value
    except (runtime.JournalError, recovery.RecoveryError, build.BuildError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, WheelBuildError):
            raise
        raise WheelBuildError("invalid wheel attempt") from exc


def _wheel_solve_command() -> list[str]:
    return ["build", "--progress=plain", "--no-cache", "--frontend=dockerfile.v0",
            "--local", "context=/work/context/wheel-preparation",
            "--local", "dockerfile=/work/context/wheel-preparation",
            "--opt", "filename=Dockerfile", "--opt", "platform=linux/amd64",
            "--opt", "force-network-mode=none", "--output", "type=local,dest=/work/output/wheel"]


def _image_solve_command(commit: str) -> list[str]:
    return ["build", "--progress=plain", "--no-cache", "--frontend=dockerfile.v0",
            "--local", "context=/work/context/docker", "--local", "dockerfile=/work/context/source/deploy",
            "--opt", "filename=Dockerfile", "--opt", "platform=linux/amd64",
            "--opt", "force-network-mode=none", "--opt", "build-arg:PIP_NO_INDEX=1",
            "--opt", "build-arg:PIP_FIND_LINKS=/app/wheels/dependencies",
            "--opt", "build-arg:WUWATERM_SOURCE_REVISION=" + _git_id(commit),
            "--output", "type=oci,dest=/work/output/image.tar"]


def _solve_record(command: list[str], result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"command": command, "generation": None if result is None else result["generation"],
            "exit_code": None if result is None else result["exit_code"],
            "stdout_sha256": None if result is None else result["stdout_sha256"],
            "stderr_sha256": None if result is None else result["stderr_sha256"]}


def _validate_solve_record(value: Any, command: list[str], *, complete: bool) -> None:
    if (type(value) is not dict or set(value) != {"command", "generation", "exit_code", "stdout_sha256", "stderr_sha256"}
            or value["command"] != command):
        raise WheelBuildError("invalid isolated solve record")
    generation = value["generation"]
    if generation is None:
        if complete or any(value[key] is not None for key in ("exit_code", "stdout_sha256", "stderr_sha256")):
            raise WheelBuildError("incomplete isolated solve result")
        return
    if type(generation) is not int or generation < 1 or type(value["exit_code"]) is not int:
        raise WheelBuildError("invalid isolated solve result")
    _safe_digest(value["stdout_sha256"])
    _safe_digest(value["stderr_sha256"])
    if complete and value["exit_code"] != 0:
        raise WheelBuildError("isolated solve was not successful")


def _validate_terminal_wheel(attempt: dict[str, Any], ledger: dict[str, Any]) -> None:
    status = attempt["status"]
    if status == "active":
        return
    parent, worker, probe = ledger["parent"], ledger["worker"], ledger["probe"]
    if (parent["create_outcome"] == "unknown" or worker["create_outcome"] == "unknown"
            or (probe is not None and probe["create_outcome"] == "unknown")):
        raise WheelBuildError("terminal wheel build has an unresolved creator")
    if parent["create_outcome"] == "bound" and (not parent["absent"] or ledger["witness"] is None):
        raise WheelBuildError("terminal wheel build lacks containment exhaustion")
    if worker["create_outcome"] == "bound" and not worker["absent"]:
        raise WheelBuildError("terminal wheel worker remains")
    if probe is not None and probe["create_outcome"] == "bound" and not probe["absent"]:
        raise WheelBuildError("terminal wheel probe remains")
    if any(ledger[key] is not None and not ledger[key]["absent"] for key in ("cache", "output", "probe_control")):
        raise WheelBuildError("terminal wheel build has unclean resources")
    witness = ledger["witness"]
    if witness is not None:
        covered = len(witness["client_generations"])
        for client in ledger["clients"]:
            if (client["operation"].startswith(("worker-", "probe-"))
                    or client["operation"] in ("wheel-prepare", "image-export")) and client["generation"] > covered:
                raise WheelBuildError("wheel exhaustion witness does not cover a process generation")
    receipt, imported = ledger["receipt"], ledger["import"]
    if status in ("failed", "retained_import"):
        if (ledger["cancel_intent"] != attempt["failure"] or attempt["receipt_sha256"] is not None
                or (receipt is not None and receipt["state"] != "revoked")):
            raise WheelBuildError("cancelled wheel build has inconsistent receipt state")
        released = imported is not None and imported["released_generation"] is not None
        if (status == "retained_import") != released or (released and not imported["verified"]):
            raise WheelBuildError("cancelled wheel import retention differs")
        return
    reference = ledger["retained_reference"]
    if (ledger["cancel_intent"] is not None or type(receipt) is not dict or receipt["state"] != "registered"
            or receipt["sha256"] != attempt["receipt_sha256"] or parent["create_outcome"] != "bound"
            or worker["create_outcome"] != "bound" or type(probe) is not dict
            or probe["create_outcome"] != "bound" or not probe["go_intent"]
            or probe["id"] != attempt["probe_container_id"] or probe["output_sha256"] != attempt["probe_sha256"]
            or type(imported) is not dict or not imported["verified"] or imported["native_image_id"] != attempt["image_id"]
            or type(reference) is not dict or reference["ref"] != attempt["target_ref"]):
        raise WheelBuildError("registered wheel build lacks completed evidence")


def _read_v2_attempt(root: Path, name: str, value: dict[str, Any]) -> dict[str, Any]:
    try:
        if set(value) != _ATTEMPT_KEYS | {"isolation", "preparation", "build_tool_wheels"}:
            raise WheelBuildError("invalid isolated wheel attempt schema")
        ident = build._id(value["build_id"])
        if (name != ident + ".json" or value["target_ref"] != "wuwaterm-wheel-build:" + ident
                or value["context_path"] != _CONTEXT + ident
                or value["probe_name"] != "wuwaterm-wheel-probe-" + ident):
            raise WheelBuildError("isolated wheel attempt identity differs")
        status, phase = value["status"], value["phase"]
        if (status not in ("active", "failed", "retained_import", "registered") or type(phase) is not str
                or phase not in isolation.SUPPORTED_PHASES
                or (status == "registered") != (phase == "registered")
                or (status == "failed") != (phase == "failed")
                or (status == "retained_import") != (phase == "retained-import")):
            raise WheelBuildError("invalid isolated wheel attempt state")
        if status in ("failed", "retained_import"):
            if type(value["failure"]) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", value["failure"]):
                raise WheelBuildError("invalid isolated wheel failure")
        elif value["failure"] is not None:
            raise WheelBuildError("unexpected isolated wheel failure")
        source = value["source"]
        if type(source) is not dict or set(source) != {"commit", "tree"}:
            raise WheelBuildError("invalid Telegram source identity")
        _git_id(source["commit"])
        _git_id(source["tree"])
        operations = value["operations_source"]
        saved = recovery._validate_source(value["recovery_source"])
        if (type(operations) is not dict or set(operations) != {"commit", "tree", "tool_bytes_sha256", "source_tool_sha256"}
                or any(saved[key] != operations[key] for key in operations)
                or runtime.source_tool_identity(operations["commit"], operations["tree"], operations["tool_bytes_sha256"])
                != operations["source_tool_sha256"]):
            raise WheelBuildError("operations source identity differs")
        for key in ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_artifact_sha256",
                    "core_checksums_sha256", "source_archive_sha256", "context_sha256", "recipe_sha256",
                    "probe_container_id", "probe_sha256", "receipt_sha256"):
            if value[key] is not None:
                _safe_digest(value[key])
        for key in ("telegram_version", "core_version"):
            if value[key] is not None and (type(value[key]) is not str or not manifest._VERSION.fullmatch(value[key])):
                raise WheelBuildError("invalid wheel version")
        core = value["core_source"]
        if core is not None:
            if type(core) is not dict or set(core) != {"commit", "tree"}:
                raise WheelBuildError("invalid Core source identity")
            _git_id(core["commit"])
            _git_id(core["tree"])
        if value["core_producer_kind"] not in (None, "github-actions-artifact", "github-release"):
            raise WheelBuildError("unsupported Core producer")
        dependencies = value["dependencies"]
        if dependencies is not None:
            if type(dependencies) is not dict or not dependencies:
                raise WheelBuildError("invalid dependency wheel list")
            for dependency, digest in dependencies.items():
                if type(dependency) is not str or Path(dependency).name != dependency or not dependency.endswith(".whl"):
                    raise WheelBuildError("invalid dependency wheel name")
                _safe_digest(digest)
        tools = value["build_tool_wheels"]
        if type(tools) is not dict or set(tools) != _TOOL_NAMES:
            raise WheelBuildError("invalid build-tool wheel record")
        for tool_name, info in tools.items():
            if type(info) is not dict or set(info) != {"filename", "version", "sha256"}:
                raise WheelBuildError("invalid build-tool wheel record")
            match = _TOOL_FILENAME.fullmatch(info["filename"]) if type(info["filename"]) is str else None
            if match is None or match.groups() != (tool_name, info["version"]):
                raise WheelBuildError("build-tool wheel identity differs")
            _safe_digest(info["sha256"])
        preparation = value["preparation"]
        if preparation is not None:
            if (type(preparation) is not dict or set(preparation) != {"schema_version", "context_path", "source_tree",
                                                                    "context_sha256", "recipe_sha256", "tools"}
                    or type(preparation["schema_version"]) is not int or preparation["schema_version"] != 1
                    or preparation["context_path"] != _PREPARATION or preparation["source_tree"] != source["tree"]
                    or preparation["tools"] != tools):
                raise WheelBuildError("invalid wheel preparation record")
            _safe_digest(preparation["context_sha256"])
            _safe_digest(preparation["recipe_sha256"])
        if value["wheel_build"] is not None:
            _validate_solve_record(value["wheel_build"], _wheel_solve_command(), complete=status == "registered")
        if value["command"] is not None and value["command"] != _image_solve_command(source["commit"]):
            raise WheelBuildError("isolated image recipe differs")
        if value["build_result"] is not None:
            _validate_solve_record(value["build_result"], _image_solve_command(source["commit"]),
                                   complete=status == "registered")
        if value["image_id"] is not None:
            if type(value["image_id"]) is not str or not value["image_id"].startswith("sha256:"):
                raise WheelBuildError("invalid isolated image ID")
            _safe_digest(value["image_id"][7:])
        if value["image_revision"] is not None and value["image_revision"] != source["commit"]:
            raise WheelBuildError("image revision differs")
        ledger = isolation.validate_ledger(root, "wheel", ident, value["isolation"])
        for client in ledger["clients"]:
            if client["operation"] in ("wheel-prepare", "image-export"):
                command = (_wheel_solve_command() if client["operation"] == "wheel-prepare"
                           else _image_solve_command(source["commit"]))
                expected = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec", ledger["worker"]["id"],
                            "/usr/bin/buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", *command]
                if client["argv"] != expected:
                    raise WheelBuildError("isolated solve client recipe differs")
        probe = ledger["probe"]
        if probe is not None and probe["spec"] is not None:
            if (probe["spec"]["command"][-1] != _PROBE or ledger["import"] is None
                    or probe["spec"]["image_id"] != ledger["import"]["native_image_id"]):
                raise WheelBuildError("wheel probe recipe or image differs")
        if status == "registered":
            required = ("telegram_wheel_sha256", "telegram_version", "core_wheel_sha256", "core_version",
                        "core_lock_sha256", "core_artifact_sha256", "core_source", "core_producer_kind",
                        "dependencies", "source_archive_sha256", "context_sha256", "recipe_sha256", "preparation",
                        "wheel_build", "command", "build_result", "probe_container_id", "probe_sha256", "image_id",
                        "image_revision", "receipt_sha256")
            if any(value[key] is None for key in required):
                raise WheelBuildError("registered wheel build is incomplete")
            if value["core_producer_kind"] == "github-release" and value["core_checksums_sha256"] is None:
                raise WheelBuildError("registered release checksums are missing")
            if value["core_producer_kind"] == "github-actions-artifact" and value["core_checksums_sha256"] is not None:
                raise WheelBuildError("unexpected release checksums")
        elif value["receipt_sha256"] is not None:
            # A pending publication may have a digest, but it is never usable
            # without the matching registered producer state.
            if status != "active" or ledger.get("receipt") is None:
                raise WheelBuildError("unregistered wheel receipt is inconsistent")
        _validate_terminal_wheel(value, ledger)
        return value
    except (AttributeError, isolation.IsolationError, runtime.JournalError, recovery.RecoveryError,
            KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, WheelBuildError):
            raise
        raise WheelBuildError("invalid isolated wheel attempt") from exc


def _tree(path: Path) -> str:
    """Compute the Git tree from retained regular files, rejecting links."""
    def node(directory: Path) -> bytes:
        entries = []
        for child in directory.iterdir():
            info = child.lstat()
            name = child.name.encode("utf-8")
            if stat.S_ISDIR(info.st_mode):
                mode, digest, order = b"40000", node(child), name + b"/"
            elif stat.S_ISREG(info.st_mode):
                data = _read(child)
                digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).digest()
                mode, order = (b"100755" if info.st_mode & 0o111 else b"100644"), name
            else:
                raise WheelBuildError("source contains an unsafe entry")
            entries.append((order, mode + b" " + name + b"\0" + digest))
        content = b"".join(item for _, item in sorted(entries))
        return hashlib.sha1(b"tree " + str(len(content)).encode() + b"\0" + content).digest()
    return node(path).hex()


def _source(context: Path, source: dict[str, str]) -> None:
    commit = _read(context / "commit.bin")
    digest = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
    if digest != source["commit"] or not commit.startswith(("tree " + source["tree"] + "\n").encode()) \
            or _tree(context / "source") != source["tree"]:
        raise WheelBuildError("retained Telegram source does not match its Git identity")


def _wheel(path: Path, name: str, version: str) -> tuple[dict[str, str], dict[str, bytes]]:
    try:
        data = _read(path)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(n.startswith("/") or ".." in n.split("/") or "\\" in n for n in names):
                raise WheelBuildError("unsafe wheel members")
            metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise WheelBuildError("wheel metadata missing")
            meta = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
            if meta.get("Name", "").lower().replace("_", "-") != name or meta.get("Version") != version:
                raise WheelBuildError("wheel package or version differs")
            files = {n: archive.read(n) for n in names if not n.endswith("/")}
            prefixes = ("wuwaterm/", "wuwaterm_api/") if name == "wuwaterm" else ("wuwaterm_telegram/",)
            package = {n: _sha(b) for n, b in files.items() if n.startswith(prefixes)}
            if not package:
                raise WheelBuildError("wheel package is empty")
            return package, files
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise WheelBuildError("invalid wheel") from exc


def _telegram_wheel(path: Path, source: Path, version: str) -> dict[str, str]:
    package, files = _wheel(path, "wuwaterm-telegram", version)
    prefix = source / "src/wuwaterm_telegram"
    expected = {"wuwaterm_telegram/" + p.relative_to(prefix).as_posix(): _sha(_read(p))
                for p in prefix.rglob("*") if p.is_file()}
    if package != expected:
        raise WheelBuildError("Telegram wheel package differs from source")
    entry = [k for k in files if k.endswith(".dist-info/entry_points.txt")]
    if len(entry) != 1:
        raise WheelBuildError("Telegram wheel entrypoint missing")
    parser = configparser.ConfigParser()
    try:
        parser.read_string(files[entry[0]].decode("utf-8"))
        if dict(parser["console_scripts"]) != {"wuwaterm-telegram": "wuwaterm_telegram.cli:main"}:
            raise WheelBuildError("Telegram wheel entrypoint differs")
    except (UnicodeError, configparser.Error, KeyError) as exc:
        raise WheelBuildError("invalid wheel entrypoint") from exc
    return package


def _lock(source: Path, core_wheel: Path) -> tuple[dict[str, Any], str, str]:
    raw = _read(source / "core-lock.json")
    try:
        lock = runtime._decode(raw)
        project = tomllib.loads(_read(source / "pyproject.toml").decode("utf-8"))["project"]
        version = project["version"]
        if type(version) is not str or not manifest._VERSION.fullmatch(version):
            raise ValueError("bad version")
        if type(lock) is not dict or lock.get("schema") != 2 or lock.get("kind") not in ("github-actions-artifact", "github-release") \
                or lock.get("repository") != "My-Denia/WuwaTerm":
            raise ValueError("unsupported lock")
        core_version = lock["version"]
        if type(core_version) is not str or not manifest._VERSION.fullmatch(core_version) \
                or project["dependencies"].count("wuwaterm==" + core_version) != 1:
            raise ValueError("Core pin differs")
        parsed = ast.parse(_read(source / "src/wuwaterm_telegram/core_compat.py").decode("utf-8"))
        constants = [item.value.value for item in parsed.body if isinstance(item, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "CORE_VERSION" for target in item.targets)
                     and isinstance(item.value, ast.Constant)]
        if constants != [core_version]:
            raise ValueError("Core compatibility differs")
        _safe_digest(lock["sha256"])
        if lock["filename"] != f"wuwaterm-{core_version}-py3-none-any.whl":
            raise ValueError("invalid Core wheel name")
        if lock["kind"] == "github-actions-artifact":
            for key in ("checkout_sha", "checkout_tree", "run_head_sha"):
                _git_id(lock[key])
            for key in ("sdist_sha256", "artifact_sha256"):
                _safe_digest(lock[key])
            for key in ("repository_id", "workflow_id", "run_id", "run_attempt", "artifact_id", "artifact_bytes"):
                if type(lock[key]) is not int or lock[key] < 1:
                    raise ValueError("invalid producer identity")
            if (lock["sdist_filename"] != f"wuwaterm-{core_version}.tar.gz"
                    or lock.get("event") != "pull_request" or lock.get("artifact_name") != "python-package"):
                raise ValueError("invalid producer artifact")
        else:
            _git_id(lock["source_commit"])
            tag = lock["tag"]
            if (type(tag) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]+", tag)
                    or lock["url"] != f'https://github.com/My-Denia/WuwaTerm/releases/download/{tag}/{lock["filename"]}'):
                raise ValueError("invalid release identity")
        if core_wheel.name != lock["filename"] or _sha(_read(core_wheel)) != lock["sha256"]:
            raise ValueError("Core wheel differs from lock")
        _wheel(core_wheel, "wuwaterm", core_version)
        return lock, version, _sha(raw)
    except (runtime.JournalError, tomllib.TOMLDecodeError, SyntaxError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise WheelBuildError("Core lock, source pin, or wheel differs") from exc


def _context_hash(path: Path) -> str:
    entries = {}
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                raise WheelBuildError("unsafe context directory")
        for name in files:
            item = Path(directory) / name
            entries[item.relative_to(path).as_posix()] = _sha(_read(item))
    return _sha(_json(entries))


_PREPARATION = "wheel-preparation"
_TOOL_NAMES = frozenset({"packaging", "setuptools", "wheel"})
_TOOL_FILENAME = re.compile(r"(packaging|setuptools|wheel)-([0-9]+(?:\.[0-9]+)*)-py3-none-any\.whl\Z")
_REQUIREMENT = re.compile(r"([A-Za-z0-9_.-]+)\s*(.*)\Z")
_VERSION_SPEC = re.compile(r"(==|!=|>=|<=|>|<)\s*([0-9]+(?:\.[0-9]+)*)\Z")


def _physical_directory(path: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        if path.resolve(strict=True) != path or not stat.S_ISDIR(path.lstat().st_mode):
            raise WheelBuildError("build input must be a physical directory")
    except OSError as exc:
        raise WheelBuildError("build input directory is unavailable") from exc
    return path


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise WheelBuildError("unsupported build-tool version")
    return tuple(int(part) for part in value.split("."))


def _check_specifier(actual: str, specifier: str) -> None:
    for part in specifier.split(","):
        match = _VERSION_SPEC.fullmatch(part.strip())
        if match is None:
            raise WheelBuildError("unsupported build-tool version requirement")
        left, right = _version(actual), _version(match.group(2))
        width = max(len(left), len(right))
        left += (0,) * (width - len(left))
        right += (0,) * (width - len(right))
        matched = {"==": left == right, "!=": left != right, ">=": left >= right,
                   "<=": left <= right, ">": left > right, "<": left < right}[match.group(1)]
        if not matched:
            raise WheelBuildError("build-tool version does not satisfy a requirement")


def _check_tool_requirement(requirement: str, tools: dict[str, dict[str, str]]) -> None:
    if type(requirement) is not str:
        raise WheelBuildError("invalid build-tool requirement")
    expression, separator, marker = requirement.partition(";")
    if separator:
        # None of these tools is installed with extras. An extra-only marker
        # is false; other marker expressions must not be guessed on the host.
        if (re.search(r"\bor\b", marker, re.IGNORECASE)
                or not re.search(r"\bextra\s*==\s*['\"][^'\"]+['\"]", marker, re.IGNORECASE)):
            raise WheelBuildError("unsupported build-tool requirement marker")
        return
    match = _REQUIREMENT.fullmatch(expression.strip())
    if match is None:
        raise WheelBuildError("invalid build-tool requirement")
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    if name not in tools:
        raise WheelBuildError("required build tool is missing")
    specifier = match.group(2).strip()
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1].strip()
    if specifier:
        _check_specifier(tools[name]["version"], specifier)


def _tool_metadata(data: bytes, name: str, version: str) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(entries) > 10000 or sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                raise WheelBuildError("build-tool wheel is too large")
            if len(names) != len(set(names)):
                raise WheelBuildError("duplicate build-tool wheel member")
            for item in entries:
                parts = item.filename.rstrip("/").split("/")
                mode = item.external_attr >> 16
                if (not item.filename or item.filename.startswith("/") or "\\" in item.filename
                        or any(part in ("", ".", "..") for part in parts)
                        or stat.S_ISLNK(mode)):
                    raise WheelBuildError("unsafe build-tool wheel member")
            prefix = f"{name}-{version}.dist-info/"
            metadata_roots = {item.split("/", 1)[0] + "/" for item in names
                              if "/" in item and item.split("/", 1)[0].endswith(".dist-info")}
            if metadata_roots != {prefix}:
                raise WheelBuildError("build-tool wheel metadata is ambiguous")
            metadata_info = archive.getinfo(prefix + "METADATA")
            wheel_info = archive.getinfo(prefix + "WHEEL")
            if metadata_info.file_size > 1024 * 1024 or wheel_info.file_size > 1024 * 1024:
                raise WheelBuildError("build-tool metadata is too large")
            metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_info))
            wheel_info_data = email.parser.BytesParser().parsebytes(archive.read(wheel_info))
            if (metadata.defects or wheel_info_data.defects or metadata.get_all("Name") != [name]
                    or metadata.get_all("Version") != [version]
                    or wheel_info_data.get_all("Root-Is-Purelib") != ["true"]
                    or "py3-none-any" not in (wheel_info_data.get_all("Tag") or [])):
                raise WheelBuildError("build-tool wheel metadata differs")
            python = metadata.get_all("Requires-Python")
            if python is None or len(python) != 1:
                raise WheelBuildError("build-tool Python requirement is missing")
            _check_specifier("3.11", python[0])
            if archive.testzip() is not None:
                raise WheelBuildError("build-tool wheel is corrupt")
            return metadata.get_all("Requires-Dist") or []
    except (OSError, KeyError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise WheelBuildError("invalid build-tool wheel") from exc


def inspect_wheel_build_tools(wheelhouse: Path) -> dict[str, dict[str, str]]:
    """Inspect an explicit offline tool wheelhouse without creating an attempt."""
    wheelhouse = _physical_directory(wheelhouse)
    tools: dict[str, dict[str, str]] = {}
    requirements: list[str] = []
    try:
        entries = sorted(wheelhouse.iterdir(), key=lambda item: item.name)
        for path in entries:
            if not stat.S_ISREG(path.lstat().st_mode) or not path.name.endswith(".whl"):
                raise WheelBuildError("build-tool wheelhouse must contain regular wheels")
            match = _TOOL_FILENAME.fullmatch(path.name)
            if match is None:
                if re.match(r"(?:packaging|setuptools|wheel)[-_]", path.name, re.IGNORECASE):
                    raise WheelBuildError("unsupported build-tool wheel filename")
                continue
            name, version = match.groups()
            if name in tools:
                raise WheelBuildError("ambiguous build-tool wheels")
            data = _read(path)
            requirements.extend(_tool_metadata(data, name, version))
            tools[name] = {"filename": path.name, "version": version, "sha256": _sha(data)}
    except OSError as exc:
        raise WheelBuildError("cannot inspect build-tool wheelhouse") from exc
    if set(tools) != _TOOL_NAMES:
        raise WheelBuildError("required build-tool wheels are missing")
    for requirement in requirements:
        _check_tool_requirement(requirement, tools)
    return {name: tools[name] for name in sorted(tools)}


def _preparation_recipe(tools: dict[str, dict[str, str]]) -> bytes:
    install = ["python", "-m", "pip", "install", "--no-index", "--no-deps"] + [
        "/build/tools/" + tools[name]["filename"] for name in sorted(_TOOL_NAMES)
    ]
    wheel = ["python", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
             "--wheel-dir", "/build/dist", "/build/source"]
    lines = ["FROM python:3.11-slim AS wheel-build",
             "ENV PIP_NO_INDEX=1 PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1",
             "WORKDIR /build", "COPY source/ /build/source/", "COPY tools/ /build/tools/",
             "RUN --network=none " + json.dumps(install, separators=(",", ":")),
             "RUN --network=none " + json.dumps(wheel, separators=(",", ":")),
             "FROM scratch", "COPY --from=wheel-build /build/dist/ /"]
    return ("\n".join(lines) + "\n").encode("ascii")


def _build_requirements(source: Path, tools: dict[str, dict[str, str]]) -> None:
    try:
        project = tomllib.loads(_read(source / "pyproject.toml").decode("utf-8"))
        system = project["build-system"]
        if (type(system) is not dict or system.get("build-backend") != "setuptools.build_meta"
                or type(system.get("requires")) is not list or not system["requires"]):
            raise WheelBuildError("unsupported Telegram build backend")
        for requirement in system["requires"]:
            _check_tool_requirement(requirement, tools)
    except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise WheelBuildError("invalid Telegram build requirements") from exc


def _write_preparation_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def prepare_wheel_build_context(context: Path, wheelhouse: Path, *, source_tree: str,
                                tools: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Create a tracked-source, offline-wheel preparation context; run no build."""
    context, wheelhouse = _physical_directory(context), _physical_directory(wheelhouse)
    source_tree = _git_id(source_tree)
    source = _physical_directory(context / "source")
    current_tools = inspect_wheel_build_tools(wheelhouse)
    if current_tools != tools:
        raise WheelBuildError("build-tool wheelhouse changed after preflight")
    if _tree(source) != source_tree:
        raise WheelBuildError("Telegram preparation source tree differs")
    _build_requirements(source, tools)
    target = context / _PREPARATION
    try:
        target.mkdir(mode=0o700)
        (target / "source").mkdir(mode=0o755)
        (target / "tools").mkdir(mode=0o755)
        for directory, directories, files in os.walk(source, followlinks=False):
            destination = target / "source" / Path(directory).relative_to(source)
            for name in sorted(directories):
                item = Path(directory) / name
                if not stat.S_ISDIR(item.lstat().st_mode):
                    raise WheelBuildError("unsafe Telegram preparation directory")
                (destination / name).mkdir(mode=0o755)
            for name in sorted(files):
                item = Path(directory) / name
                info = item.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise WheelBuildError("unsafe Telegram preparation file")
                _write_preparation_file(destination / name, _read(item), 0o755 if info.st_mode & 0o111 else 0o644)
        for item in tools.values():
            data = _read(wheelhouse / item["filename"])
            if _sha(data) != item["sha256"]:
                raise WheelBuildError("build-tool wheel changed while copying")
            _write_preparation_file(target / "tools" / item["filename"], data)
        recipe = _preparation_recipe(tools)
        _write_preparation_file(target / "Dockerfile", recipe)
        _sync_tree(target)
        record = {"schema_version": 1, "context_path": _PREPARATION, "source_tree": source_tree,
                  "context_sha256": _context_hash(target), "recipe_sha256": _sha(recipe), "tools": tools}
        verify_wheel_build_context(context, record)
        return record
    except (OSError, capsule.CapsuleError) as exc:
        raise WheelBuildError("cannot create wheel preparation context") from exc


def verify_wheel_build_context(context: Path, record: dict[str, Any]) -> None:
    """Recheck the preparation recipe, source, modes and exact tool wheels."""
    context = _physical_directory(context)
    if (type(record) is not dict or set(record) != {"schema_version", "context_path", "source_tree",
                                                    "context_sha256", "recipe_sha256", "tools"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["context_path"] != _PREPARATION):
        raise WheelBuildError("invalid wheel preparation record")
    source_tree = _git_id(record["source_tree"])
    _safe_digest(record["context_sha256"])
    _safe_digest(record["recipe_sha256"])
    target = _physical_directory(context / _PREPARATION)
    source = _physical_directory(context / "source")
    prepared_source = _physical_directory(target / "source")
    if {item.name for item in target.iterdir()} != {"Dockerfile", "source", "tools"}:
        raise WheelBuildError("unexpected wheel preparation content")
    tools = inspect_wheel_build_tools(target / "tools")
    if tools != record["tools"]:
        raise WheelBuildError("wheel preparation tools differ")
    if _tree(source) != source_tree or _tree(prepared_source) != source_tree:
        raise WheelBuildError("wheel preparation source differs")
    for directory, _, files in os.walk(prepared_source, followlinks=False):
        for name in files:
            if stat.S_IMODE((Path(directory) / name).lstat().st_mode) not in (0o644, 0o755):
                raise WheelBuildError("wheel preparation source mode differs")
    _build_requirements(source, tools)
    recipe = _preparation_recipe(tools)
    if (_read(target / "Dockerfile") != recipe or _sha(recipe) != record["recipe_sha256"]
            or _context_hash(target) != record["context_sha256"]):
        raise WheelBuildError("wheel preparation recipe or context differs")
    for path in (target / "Dockerfile", *(target / "tools" / tool["filename"] for tool in tools.values())):
        if stat.S_IMODE(path.lstat().st_mode) != 0o644:
            raise WheelBuildError("wheel preparation file mode differs")


def _artifact(path: Path, lock: dict[str, Any], core_wheel: Path) -> None:
    """Verify the actual producer ZIP as well as its source manifest and wheel."""
    raw = _read(path)
    try:
        if _sha(raw) != lock["artifact_sha256"] or len(raw) != lock["artifact_bytes"]:
            raise WheelBuildError("Core producer artifact differs from the source lock")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            expected = {"SHA256SUMS", "source-manifest.json", lock["filename"], lock["sdist_filename"]}
            if len(names) != len(expected) or set(names) != expected:
                raise WheelBuildError("unexpected Core producer artifact contents")
            source_manifest = runtime._decode(archive.read("source-manifest.json"))
            if (type(source_manifest) is not dict or source_manifest.get("schema") != 1 or source_manifest.get("source_commit") != lock["checkout_sha"]
                    or source_manifest.get("source_tree") != lock["checkout_tree"]
                    or source_manifest.get("version") != lock["version"]
                    or source_manifest.get("sha256") != {lock["filename"]: lock["sha256"],
                                                         lock["sdist_filename"]: lock["sdist_sha256"]}
                    or archive.read(lock["filename"]) != _read(core_wheel)
                    or _sha(archive.read(lock["sdist_filename"])) != lock["sdist_sha256"]):
                raise WheelBuildError("Core artifact source or contents differ")
            sums = archive.read("SHA256SUMS").decode("ascii").splitlines()
            if set(sums) != {f'{lock["sha256"]}  {lock["filename"]}',
                             f'{lock["sdist_sha256"]}  {lock["sdist_filename"]}'} or len(sums) != 2:
                raise WheelBuildError("Core artifact checksums differ")
    except (KeyError, TypeError, UnicodeError, zipfile.BadZipFile, runtime.JournalError) as exc:
        raise WheelBuildError("invalid Core producer artifact") from exc


def _core_evidence(context: Path, lock: dict[str, Any], core_wheel: Path) -> tuple[dict[str, str], str, str | None]:
    if lock["kind"] == "github-actions-artifact":
        artifact = context / "core-artifact.zip"
        _artifact(artifact, lock, core_wheel)
        return ({"commit": lock["checkout_sha"], "tree": lock["checkout_tree"]}, _sha(_read(artifact)), None)

    try:
        commit_bytes = _read(context / "core-commit.bin")
        commit = hashlib.sha1(b"commit " + str(len(commit_bytes)).encode() + b"\0" + commit_bytes).hexdigest()
        match = re.match(rb"tree ([0-9a-f]{40})\n", commit_bytes)
        if commit != lock["source_commit"] or match is None:
            raise WheelBuildError("Core release source commit object differs")
        manifest_bytes = _read(context / "core-release-manifest.json")
        release = runtime._decode(manifest_bytes)
        if (type(release) is not dict or release.get("tag") != lock["tag"]
                or release.get("version") != lock["version"] or release.get("source_commit") != commit
                or release.get("dry_run") is not False):
            raise WheelBuildError("Core release manifest differs from the lock")
        sums_bytes = _read(context / "core-release-SHA256SUMS")
        lines = sums_bytes.decode("ascii").splitlines()
        entries = [re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line) for line in lines]
        if len(entries) != 2 or any(item is None for item in entries):
            raise WheelBuildError("invalid Core release checksums")
        hashes = {item.group(2): item.group(1) for item in entries if item is not None}
        if (len(hashes) != 2 or set(hashes) != {lock["filename"], f'wuwaterm-{lock["version"]}.tar.gz'}
                or hashes[lock["filename"]] != lock["sha256"]):
            raise WheelBuildError("Core release wheel checksum differs")
        return ({"commit": commit, "tree": match.group(1).decode("ascii")},
                _sha(manifest_bytes), _sha(sums_bytes))
    except (KeyError, UnicodeError, runtime.JournalError) as exc:
        raise WheelBuildError("invalid Core release evidence") from exc


def _verify_context(root: Path, attempt: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    context = root / attempt["context_path"]
    _source(context, attempt["source"])
    if _sha(_read(context / "source.tar")) != attempt["source_archive_sha256"]:
        raise WheelBuildError("retained Telegram archive differs")
    docker = context / "docker"
    if _context_hash(docker) != attempt["context_sha256"] or _sha(_read(context / "source/deploy/Dockerfile")) != attempt["recipe_sha256"]:
        raise WheelBuildError("build context or Dockerfile differs")
    for name in _CONTEXT_FILES:
        if _read(docker / name) != _read(context / "source" / name):
            raise WheelBuildError("context differs from Telegram source")
    lock_raw = runtime._decode(_read(docker / "core-lock.json"))
    core_name = lock_raw["filename"]
    telegram_name = "wuwaterm_telegram-" + attempt["telegram_version"] + "-py3-none-any.whl"
    if any(Path(n).name != n for n in (core_name, telegram_name)):
        raise WheelBuildError("unsafe wheel filename")
    core_path = docker / "wheels" / core_name
    tg_path = docker / "wheels" / telegram_name
    lock, version, lock_hash = _lock(context / "source", core_path)
    core_source, artifact_hash, checksums_hash = _core_evidence(context, lock, core_path)
    if (version != attempt["telegram_version"] or lock_hash != attempt["core_lock_sha256"]
            or lock["sha256"] != attempt["core_wheel_sha256"] or lock["version"] != attempt["core_version"]
            or core_source != attempt["core_source"] or lock["kind"] != attempt["core_producer_kind"]
            or artifact_hash != attempt["core_artifact_sha256"] or checksums_hash != attempt["core_checksums_sha256"]
            or _sha(_read(tg_path)) != attempt["telegram_wheel_sha256"]):
        raise WheelBuildError("wheel or Core lock identity differs")
    for filename, content in (("core.filename", core_name + "\n"), ("telegram.filename", telegram_name + "\n"),
                              ("telegram.sha256", attempt["telegram_wheel_sha256"] + "\n")):
        if _read(docker / "wheels" / filename) != content.encode("ascii"):
            raise WheelBuildError("wheel context metadata differs")
    deps = attempt["dependencies"]
    if type(deps) is not dict or not deps or any(type(k) is not str or Path(k).name != k or not k.endswith(".whl") for k in deps):
        raise WheelBuildError("invalid dependency wheel list")
    for name, digest in deps.items():
        if _sha(_read(docker / "wheels/dependencies" / name)) != _safe_digest(digest):
            raise WheelBuildError("dependency wheel differs")
    expected = set(_CONTEXT_FILES) | {"wheels/" + core_name, "wheels/" + telegram_name,
        "wheels/core.filename", "wheels/telegram.filename", "wheels/telegram.sha256"} | {
        "wheels/dependencies/" + n for n in deps}
    actual = {p.relative_to(docker).as_posix() for p in docker.rglob("*") if p.is_file()}
    if actual != expected:
        raise WheelBuildError("unexpected context content")
    tg_files = _telegram_wheel(tg_path, context / "source", version)
    core_files, _ = _wheel(core_path, "wuwaterm", lock["version"])
    return core_files, tg_files


def _verify_probe(data: bytes, core_files: dict[str, str], tg_files: dict[str, str], attempt: dict[str, Any]) -> None:
    try:
        value = json.loads(data)
        for name, expected, version, digest in (
            ("wuwaterm", core_files, attempt["core_version"], attempt["core_wheel_sha256"]),
            ("wuwaterm-telegram", tg_files, attempt["telegram_version"], attempt["telegram_wheel_sha256"]),
        ):
            item = value[name]
            if item["version"] != version or item["files"] != expected \
                    or item["direct_url"]["archive_info"]["hashes"]["sha256"] != digest:
                raise WheelBuildError("installed package differs from verified wheel")
    except (ValueError, TypeError, KeyError) as exc:
        raise WheelBuildError("invalid installed-package probe") from exc


def _receipt_v2(attempt: dict[str, Any]) -> dict[str, Any]:
    ledger = attempt["isolation"]
    fields = ("binding", "parent", "worker", "probe", "archive", "import", "retained_reference", "worker_witness", "witness",
              "context", "cache", "output", "probe_control", "clients")
    evidence = {key: copy.deepcopy(ledger[key]) for key in fields}
    record = {key: copy.deepcopy(value) for key, value in attempt.items()
              if key not in ("status", "phase", "receipt_sha256", "failure", "isolation")}
    record.update(isolation=evidence, image_ref=attempt["target_ref"], image_digest=attempt["image_id"])
    return record


def _verify_solve_evidence(session: isolation.ProducerSession, attempt: dict[str, Any],
                           record: dict[str, Any], operation: str) -> None:
    generation = record["generation"]
    clients = attempt["isolation"]["clients"]
    if type(generation) is not int or generation < 1 or generation > len(clients):
        raise WheelBuildError("isolated solve generation differs")
    client = clients[generation - 1]
    worker_id = attempt["isolation"]["worker"]["id"]
    argv = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec", worker_id,
            "/usr/bin/buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", *record["command"]]
    if (client["generation"] != generation or client["operation"] != operation
            or client["state"] != "completed" or client["exit_code"] != record["exit_code"] or client["argv"] != argv):
        raise WheelBuildError("isolated solve client differs")
    for name in ("stdout", "stderr"):
        data = session.output(generation, name, limit=100 * 1024 * 1024)
        if _sha(data) != record[name + "_sha256"]:
            raise WheelBuildError("isolated solve output differs")


def verify_registered_wheel_build(root: Path, receipt_sha256: str) -> dict[str, Any]:
    """Verify the retained producer attempt; caller separately inspects the live image."""
    digest = _safe_digest(receipt_sha256)
    try:
        with manifest._directory(root, _RECEIPTS, create=False) as directory:
            raw = manifest._read_file(directory, digest + ".json")
        record = runtime._decode(raw)
        if _sha(raw) != digest or type(record) is not dict:
            raise WheelBuildError("wheel receipt digest differs")
        attempt = _read_attempt(root, build._id(record["build_id"]) + ".json")
        if attempt["schema_version"] == 2:
            expected = _receipt_v2(attempt)
        else:
            expected = {k: v for k, v in attempt.items() if k not in ("status", "phase", "receipt_sha256", "failure")}
        if attempt["status"] != "registered" or attempt["receipt_sha256"] != digest or record != expected:
            raise WheelBuildError("wheel receipt has no matching registered producer")
        recovery.capsule = capsule
        recovery._verify_bootstrap(root, attempt["recovery_source"])
        recovery._verify_capsule(root, attempt["recovery_source"])
        core_files, tg_files = _verify_context(root, attempt)
        if attempt["schema_version"] == 2:
            verify_wheel_build_context(root / attempt["context_path"], attempt["preparation"])
            session = isolation.ProducerSession(root, attempt, lambda _: None)
            _verify_solve_evidence(session, attempt, attempt["wheel_build"], "wheel-prepare")
            _verify_solve_evidence(session, attempt, attempt["build_result"], "image-export")
            with session.sealed_archive() as (_, proof):
                if proof["manifest_digest"] != attempt["image_id"]:
                    raise WheelBuildError("retained wheel image graph differs")
                isolation.verify_image_revision(proof, attempt["source"]["commit"])
        probe = _read(root / attempt["context_path"] / "probe.json")
        if _sha(probe) != attempt["probe_sha256"]:
            raise WheelBuildError("recorded probe output differs")
        _verify_probe(probe, core_files, tg_files, attempt)
        return {**record, "image_ref": record["target_ref"], "image_digest": record["image_id"]}
    except (manifest.ManifestError, runtime.JournalError, recovery.RecoveryError, capsule.CapsuleError,
            isolation.IsolationError, OSError, KeyError, TypeError) as exc:
        raise WheelBuildError("registered wheel build cannot be verified") from exc


def read_attempt(root: Path, name: str) -> dict[str, Any]:
    """Parse an attempt for the shared admission boundary."""
    return _read_attempt(root, name)


def _copy(source: Path, target: Path) -> None:
    data = _read(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _sync_tree(root: Path) -> None:
    for directory, _, filenames in os.walk(root, topdown=False, followlinks=False):
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink():
                raise WheelBuildError("build context contains a symlink")
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        capsule.sync_directory(Path(directory))


def _git_command(repo: Path, *arguments: str) -> list[str]:
    return ["/usr/bin/git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false", "-c", "gc.auto=0",
            "-c", "maintenance.auto=false", "-c", "protocol.allow=never", *arguments]


def _git_environment() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
    sudo_uid = os.environ.get("SUDO_UID")
    if os.geteuid() == 0 and sudo_uid is not None and re.fullmatch(r"[0-9]+", sudo_uid):
        env["SUDO_UID"] = sudo_uid
    return env


def _git_client(session: isolation.ProducerSession, operation: str, repo: Path,
                *arguments: str) -> dict[str, Any]:
    result = session.client(operation, _git_command(repo, *arguments), environment=_git_environment())
    if result["exit_code"]:
        raise WheelBuildError("recorded Git operation failed")
    return result


def _git_tree_entries(raw: bytes) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    try:
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, filename = entry.split(b"\t", 1)
            mode, kind, blob, size = metadata.decode("ascii").split()
            name = filename.decode("utf-8")
            if (mode not in ("100644", "100755") or kind != "blob" or not capsule.safe_name(name)
                    or name in entries or not re.fullmatch(r"[0-9a-f]{40}", blob)
                    or not re.fullmatch(r"[0-9]+", size)):
                raise WheelBuildError("unsupported Telegram source tree entry")
            entries[name] = {"mode": mode, "blob": blob, "size": int(size)}
    except (UnicodeError, ValueError) as exc:
        raise WheelBuildError("invalid Telegram source tree entries") from exc
    if not entries:
        raise WheelBuildError("Telegram source tree is empty")
    return entries


def _archive_manifest(path: Path, entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bind archive files to the full guarded Git tree listing and blob IDs."""
    files: dict[str, dict[str, Any]] = {}
    directories = {str(parent) for name in entries for parent in Path(name).parents if str(parent) != "."}
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if not capsule.safe_name(name) or name in seen:
                    raise WheelBuildError("unsafe or duplicate Telegram archive member")
                seen.add(name)
                if member.isdir():
                    if name not in directories:
                        raise WheelBuildError("unexpected Telegram archive directory")
                    continue
                expected = entries.get(name)
                if (not member.isfile() or expected is None or member.size != expected["size"]
                        or bool(member.mode & 0o111) != (expected["mode"] == "100755")):
                    raise WheelBuildError("Telegram archive metadata differs from Git")
                stream = archive.extractfile(member)
                if stream is None:
                    raise WheelBuildError("Telegram archive member is unavailable")
                git_hash = hashlib.sha1(b"blob " + str(member.size).encode("ascii") + b"\0")
                sha256 = hashlib.sha256()
                total = 0
                while block := stream.read(1024 * 1024):
                    total += len(block)
                    git_hash.update(block)
                    sha256.update(block)
                if total != member.size or git_hash.hexdigest() != expected["blob"]:
                    raise WheelBuildError("Telegram archive content differs from Git")
                files[name] = {"mode": expected["mode"], "size": total, "sha256": sha256.hexdigest()}
    except (OSError, tarfile.TarError) as exc:
        raise WheelBuildError("invalid Telegram source archive") from exc
    if set(files) != set(entries):
        raise WheelBuildError("Telegram source archive is incomplete")
    return files


def _offline_dockerfile(path: Path) -> None:
    """Reject frontend/network overrides in the unchanged Telegram recipe."""
    try:
        text = _read(path).decode("utf-8")
    except UnicodeError as exc:
        raise WheelBuildError("Telegram Dockerfile is not UTF-8") from exc
    flattened = re.sub(r"\\[ \t]*\r?\n[ \t]*", "", text).lower()
    if ("--network" in flattened
            or re.search(r"(?im)^[ \t]*#[ \t]*(?:syntax|escape)[ \t]*=", text)):
        raise WheelBuildError("Telegram Dockerfile may override the offline frontend")


def _active_update(session: isolation.ProducerSession, phase: str, **updates: Any) -> None:
    with session.mutex:
        if session.attempt["status"] != "active" or session.ledger["cancel_intent"] is not None:
            raise WheelBuildError("wheel build cancellation is pending")
        with session.transition(phase=phase):
            session.attempt.update(updates)


def _image_revision(session: isolation.ProducerSession, image_id: str, commit: str) -> None:
    result = session.read_docker(["image", "inspect", "--format",
                                  '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}', image_id])
    try:
        fields = result.stdout.decode("ascii").strip().split()
    except UnicodeError as exc:
        raise WheelBuildError("isolated image identity is malformed") from exc
    if result.returncode or fields != [image_id, commit]:
        raise WheelBuildError("isolated image revision differs")



def build_wheel_image(root: Path, operations_commit: str, operations_tree: str, telegram_repo: Path,
                      telegram_commit: str, telegram_tree: str, core_wheel: Path, core_artifact: Path,
                      dependencies: Path, core_source_repo: Path | None = None,
                      core_checksums: Path | None = None, build_tool_wheelhouse: Path | None = None) -> str:
    """Build a source-bound Telegram wheel and image in one isolated worker."""
    root = Path(os.path.abspath(os.fspath(root)))
    if root != CODE_ROOT or root.is_symlink():
        raise WheelBuildError("source and deployment roots differ")
    build._require_lock(root)
    build._gate_journals(root)
    operations = build._preflight_source(root, operations_commit, operations_tree)
    if build_tool_wheelhouse is None:
        raise WheelBuildError("an explicit build-tool wheelhouse is required")
    source = {"commit": _git_id(telegram_commit), "tree": _git_id(telegram_tree)}
    try:
        telegram_repo = _physical_directory(telegram_repo)
        dependencies = _physical_directory(dependencies)
        build_tool_wheelhouse = _physical_directory(build_tool_wheelhouse)
        core_wheel = Path(os.path.abspath(os.fspath(core_wheel)))
        core_artifact = Path(os.path.abspath(os.fspath(core_artifact)))
        _read(core_wheel)
        _read(core_artifact)
        dependency_files = sorted(dependencies.iterdir())
        if not dependency_files or any(not item.name.endswith(".whl")
                                       or not stat.S_ISREG(item.lstat().st_mode) for item in dependency_files):
            raise WheelBuildError("dependency wheelhouse must contain regular wheels")
        tools = inspect_wheel_build_tools(build_tool_wheelhouse)
        binding = isolation.preflight(root)
        preserved = recovery.prepare_source(root, operations_commit, operations_tree)
        if any(preserved[key] != operations[key] for key in operations):
            raise WheelBuildError("preserved operations source differs")
    except (OSError, isolation.IsolationError, recovery.RecoveryError) as exc:
        raise WheelBuildError("cannot preflight isolated wheel build") from exc

    ident = manifest.new_deployment_id()
    relative = _CONTEXT + ident
    context = root / relative
    try:
        ledger = isolation.new_ledger("wheel", ident, binding)
    except isolation.IsolationError as exc:
        raise WheelBuildError("cannot bind isolated wheel build") from exc
    attempt: dict[str, Any] = {
        "schema_version": 2, "build_id": ident, "status": "active", "phase": "prepared",
        "operations_source": operations, "recovery_source": preserved, "source": source,
        "target_ref": "wuwaterm-wheel-build:" + ident, "context_path": relative,
        "telegram_wheel_sha256": None, "telegram_version": None, "core_wheel_sha256": None,
        "core_version": None, "core_lock_sha256": None, "core_artifact_sha256": None, "core_checksums_sha256": None,
        "core_source": None, "core_producer_kind": None, "dependencies": None, "source_archive_sha256": None,
        "context_sha256": None, "recipe_sha256": None, "wheel_build": None, "command": None, "build_result": None,
        "probe_name": "wuwaterm-wheel-probe-" + ident, "probe_container_id": None, "probe_sha256": None,
        "image_id": None, "image_revision": None, "receipt_sha256": None, "failure": None,
        "isolation": ledger, "preparation": None, "build_tool_wheels": tools,
    }
    _write_attempt(root, attempt, new=True)
    session = isolation.ProducerSession(root, attempt, lambda value: _write_attempt(root, value))
    try:
        session.prepare_directories()
        session.open_control()
        result = _git_client(session, "git-root", telegram_repo, "rev-parse", "--show-toplevel")
        if session.output(result["generation"], "stdout").decode("utf-8").strip() != str(telegram_repo):
            raise WheelBuildError("Telegram repository root differs")
        result = _git_client(session, "git-tree", telegram_repo, "rev-parse", telegram_commit + "^{tree}")
        if session.output(result["generation"], "stdout").decode("ascii").strip() != telegram_tree:
            raise WheelBuildError("Telegram source tree differs")
        result = _git_client(session, "git-ls-tree", telegram_repo, "ls-tree", "-rlz", "--full-tree", telegram_commit)
        entries = _git_tree_entries(session.output(result["generation"], "stdout"))
        result = _git_client(session, "git-commit", telegram_repo, "cat-file", "commit", telegram_commit)
        session.copy_output(result["generation"], "stdout", context / "commit.bin", limit=16 * 1024 * 1024)
        result = _git_client(session, "git-archive", telegram_repo, "archive", "--format=tar", telegram_commit)
        archive = context / "source.tar"
        archive_hash = session.copy_output(result["generation"], "stdout", archive)
        files = _archive_manifest(archive, entries)
        source_dir = context / "source"
        source_dir.mkdir(mode=0o700)
        capsule.inspect_archive(archive, files, extract=source_dir, require_tools=False)
        build._verify_export(source_dir, files)
        _source(context, source)
        _offline_dockerfile(source_dir / "deploy/Dockerfile")
        core_lock, telegram_version, core_lock_hash = _lock(source_dir, core_wheel)
        if core_lock["kind"] == "github-actions-artifact":
            if core_source_repo is not None or core_checksums is not None:
                raise WheelBuildError("unexpected Core release inputs")
            _copy(core_artifact, context / "core-artifact.zip")
        else:
            if core_source_repo is None or core_checksums is None:
                raise WheelBuildError("Core release source and checksums are required")
            core_source_repo = _physical_directory(core_source_repo)
            result = _git_client(session, "core-git-commit", core_source_repo,
                                 "cat-file", "commit", core_lock["source_commit"])
            session.copy_output(result["generation"], "stdout", context / "core-commit.bin", limit=16 * 1024 * 1024)
            _copy(core_artifact, context / "core-release-manifest.json")
            _copy(Path(os.path.abspath(os.fspath(core_checksums))), context / "core-release-SHA256SUMS")
        core_source, artifact_hash, checksums_hash = _core_evidence(context, core_lock, core_wheel)
        _sync_tree(source_dir)
        _active_update(session, "wheel-source", source_archive_sha256=archive_hash, telegram_version=telegram_version,
                       core_version=core_lock["version"], core_wheel_sha256=core_lock["sha256"],
                       core_lock_sha256=core_lock_hash, core_artifact_sha256=artifact_hash,
                       core_checksums_sha256=checksums_hash, core_source=core_source,
                       core_producer_kind=core_lock["kind"])
        preparation = prepare_wheel_build_context(context, build_tool_wheelhouse, source_tree=telegram_tree, tools=tools)
        _active_update(session, "wheel-prepared", preparation=preparation)
        session.create_parent()
        session.create_worker()
        session.wait_worker_ready()
        wheel_command = _wheel_solve_command()
        (context / "worker-output/wheel").mkdir(mode=0o700)
        _active_update(session, "wheel-solve-intent", wheel_build=_solve_record(wheel_command))
        result = session.solve("wheel-prepare", wheel_command)
        _active_update(session, "wheel-solved", wheel_build=_solve_record(wheel_command, result))
        verify_wheel_build_context(context, preparation)
        wheel_output = _physical_directory(context / "worker-output/wheel")
        wheels = list(wheel_output.iterdir())
        expected_name = "wuwaterm_telegram-" + telegram_version + "-py3-none-any.whl"
        if len(wheels) != 1 or wheels[0].name != expected_name or not stat.S_ISREG(wheels[0].lstat().st_mode):
            raise WheelBuildError("unexpected Telegram wheel output")
        _telegram_wheel(wheels[0], source_dir, telegram_version)
        telegram_hash = _sha(_read(wheels[0]))
        docker = context / "docker"
        docker.mkdir(mode=0o700)
        for name in _CONTEXT_FILES:
            _copy(source_dir / name, docker / name)
        _copy(core_wheel, docker / "wheels" / core_wheel.name)
        _copy(wheels[0], docker / "wheels" / expected_name)
        dep_hashes = {}
        for dependency in dependency_files:
            _copy(dependency, docker / "wheels/dependencies" / dependency.name)
            dep_hashes[dependency.name] = _sha(_read(docker / "wheels/dependencies" / dependency.name))
        for name, value in (("core.filename", core_wheel.name), ("telegram.filename", expected_name),
                            ("telegram.sha256", telegram_hash)):
            _write_preparation_file(docker / "wheels" / name, (value + "\n").encode("ascii"))
        image_command = _image_solve_command(telegram_commit)
        _active_update(session, "image-context", telegram_wheel_sha256=telegram_hash, dependencies=dep_hashes,
                       context_sha256=_context_hash(docker), recipe_sha256=_sha(_read(source_dir / "deploy/Dockerfile")),
                       command=image_command, build_result=_solve_record(image_command))
        _verify_context(root, attempt)
        _sync_tree(docker)
        capsule.sync_directory(context)
        result = session.solve("image-export", image_command)
        _active_update(session, "image-exported", build_result=_solve_record(image_command, result))
        _verify_context(root, attempt)
        verify_wheel_build_context(context, preparation)
        session.stop_worker()
        session.exhaustion_witness()
        session.seal_export()
        image_id = session.import_image()
        _image_revision(session, image_id, telegram_commit)
        _active_update(session, "image-observed", image_id=image_id, image_revision=telegram_commit)
        probed = session.probe_image(image_id, _PROBE)
        core_files, telegram_files = _verify_context(root, attempt)
        _verify_probe(probed, core_files, telegram_files, attempt)
        _write_preparation_file(context / "probe.json", probed, 0o600)
        capsule.sync_directory(context)
        probe_id = _safe_digest(session.ledger["probe"]["id"])
        probe_hash = _sha(probed)
        if session.ledger["probe"]["output_sha256"] != probe_hash:
            raise WheelBuildError("package probe output binding differs")
        _active_update(session, "probe-verified", probe_container_id=probe_id, probe_sha256=probe_hash)
        session.stop_probe()
        session.exhaustion_witness()
        session.cleanup_probe_control()
        session.cleanup_cache()
        session.cleanup_output()
        session.remove_parent()
        session.tag_image(attempt["target_ref"])
        _image_revision(session, image_id, telegram_commit)
        with session.mutex:
            if session.ledger["cancel_intent"] is not None:
                raise WheelBuildError("wheel build cancellation is pending")
            receipt = _receipt_v2(attempt)
        digest = session.register(receipt, _RECEIPTS)
        verify_registered_wheel_build(root, digest)
        session.prune_build_images()
        return digest
    except BaseException as exc:
        if attempt["status"] == "registered":
            raise WheelBuildError("registered wheel build verification is unresolved") from exc
        try:
            session.cancel(type(exc).__name__)
            session.reconcile()
        except Exception as reconcile_error:
            raise WheelBuildError("wheel build remains active and requires exact-source recovery") from reconcile_error
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, WheelBuildError):
            raise
        raise WheelBuildError("isolated wheel image build failed") from exc
    finally:
        session.close_control()


def recover_wheel_build(root: Path, build_id: str) -> dict[str, Any]:
    """Reconcile an interrupted attempt through its verified saved source."""
    try:
        return isolation.recover_attempt(root, "wheel", build._id(build_id), read_attempt, _write_attempt)
    except isolation.IsolationError as exc:
        raise WheelBuildError("wheel build recovery remains unresolved") from exc


def verify_wheel_build_terminal(root: Path, build_id: str) -> dict[str, Any]:
    """Reverify a cancelled attempt and any explicitly retained reference."""
    try:
        return isolation.verify_attempt_terminal(root, "wheel", build._id(build_id), read_attempt)
    except isolation.IsolationError as exc:
        raise WheelBuildError("wheel build terminal state is unresolved") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--operations-commit", required=True)
    parser.add_argument("--operations-tree", required=True)
    parser.add_argument("--telegram-repo", type=Path, required=True)
    parser.add_argument("--telegram-commit", required=True)
    parser.add_argument("--telegram-tree", required=True)
    parser.add_argument("--core-wheel", type=Path, required=True)
    parser.add_argument("--core-artifact", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--core-source-repo", type=Path)
    parser.add_argument("--core-checksums", type=Path)
    parser.add_argument("--build-tool-wheelhouse", type=Path)
    args = parser.parse_args(argv)
    try:
        print(build_wheel_image(args.root, args.operations_commit, args.operations_tree, args.telegram_repo,
                                args.telegram_commit, args.telegram_tree, args.core_wheel, args.core_artifact,
                                args.dependencies, args.core_source_repo, args.core_checksums, args.build_tool_wheelhouse))
    except build.BuildError as exc:
        print(f"component wheel build: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
