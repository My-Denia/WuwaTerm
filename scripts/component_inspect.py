#!/usr/bin/env python3
"""Inspect recorded deployment metadata and observed containers."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys
from typing import Any

import component_adopt_probe as probe
import component_journal_runtime as runtime
import deployment_admission as admission
import product_manifest as manifest

CODE_ROOT = Path(__file__).resolve().parents[1]
JournalError = runtime.JournalError
JOURNAL_NAME = runtime.JOURNAL_NAME
_match = runtime._match
_read_regular = runtime._read_regular
_DEPLOYMENT_ID = runtime._DEPLOYMENT_ID
_HEX40 = runtime._HEX40
_read_pointer = runtime._read_pointer
_fail = runtime._fail
load_journal = runtime.load_journal
pending_adopt_probe_staging = probe.pending_adopt_probe_staging
_read_adopt_probe = probe._read_adopt_probe
_PROBE_STAGE_DIR = probe._PROBE_STAGE_DIR


def inspect_deployment(root: Path) -> dict[str, Any]:
    """Read recorded metadata; never claim it proves live Docker bindings."""
    root = Path(os.path.abspath(os.fspath(root)))
    try:
        with manifest._directory(root, (), create=False):
            pass
    except manifest.ManifestError as exc:
        raise JournalError("deployment root cannot be safely inspected") from exc
    deployments = root / ".deployments"
    try:
        directory_info = deployments.lstat()
    except FileNotFoundError:
        directory_info = None
    if directory_info is not None and not stat.S_ISDIR(directory_info.st_mode):
        _fail("deployment metadata directory is not a regular directory")
    commit = _read_pointer(root, ".deploy_commit", _HEX40)
    deployment_id = _read_pointer(root, ".deploy_current", _DEPLOYMENT_ID)
    journal_states = {}
    directories = {admission.BUILD_ATTEMPTS, admission.WHEEL_BUILD_ATTEMPTS, admission.ADOPT_PROBES}
    for name in (".full-transaction.json", ".runtime-transaction.json", JOURNAL_NAME,
                 admission.COMPONENT_FULL, *sorted(directories)):
        try:
            info = (deployments / name).lstat()
        except FileNotFoundError:
            journal_states[name] = "absent"
        else:
            expected = stat.S_ISDIR(info.st_mode) if name in directories else stat.S_ISREG(info.st_mode)
            journal_states[name] = "present" if expected else "untrusted"
    if deployment_id is None:
        result = {"binding": "unknown", "reason": "legacy-or-unadopted",
                  "source_commit": commit, "deployment_id": None, "manifest": None,
                  "live_binding": "unverified", "journals": journal_states}
    else:
        try:
            product = manifest.read_manifest(root, deployment_id)
        except manifest.ManifestError as exc:
            raise JournalError("recorded product manifest cannot be verified") from exc
        binding = "recorded" if commit == product["operations"]["source_commit"] else "unknown"
        result = {"binding": binding, "reason": None if binding == "recorded" else "pointer-mismatch", "source_commit": commit,
                  "deployment_id": deployment_id, "manifest": product,
                  "live_binding": "unverified", "journals": journal_states}
    if journal_states[JOURNAL_NAME] == "present":
        try:
            record = load_journal(deployments / JOURNAL_NAME)
            source = record["source"]
            if "compose" in source:
                launcher = root / source["bootstrap"]["path"] / "component_recovery.py"
                command = [sys.executable, "-I", "-B", str(launcher), "--root", str(root),
                           "--kind", "adopt" if record["action"] == "adopt" else "runtime",
                           "--id", record["transaction_id"], "--compose-executable",
                           str(root / source["compose"]["path"])]
                result["recovery"] = {"status": "recorded-unverified", "argv": command}
            else:
                result["recovery"] = {"status": "no-recorded-compose"}
        except (JournalError, KeyError, TypeError):
            result["recovery"] = {"status": "untrusted"}
    if journal_states[admission.COMPONENT_FULL] == "present":
        try:
            import component_journal_full as full  # noqa: PLC0415
            record = full.load_journal(deployments / admission.COMPONENT_FULL)
            source = record["source"]
            launcher = root / source["bootstrap"]["path"] / "component_recovery.py"
            result["full_recovery"] = {"status": "recorded-unverified", "argv": [
                sys.executable, "-I", "-B", str(launcher), "--root", str(root), "--kind", "full",
                "--id", record["transaction_id"], "--compose-executable", str(root / source["compose"]["path"]),
            ]}
        except (JournalError, full.FullError, KeyError, TypeError):
            result["full_recovery"] = {"status": "untrusted"}
    try:
        pending = pending_adopt_probe_staging(root)
        if journal_states[admission.ADOPT_PROBES] == "present":
            probes = []
            for entry in sorted((deployments / admission.ADOPT_PROBES).iterdir()):
                record = _read_adopt_probe(root, entry.name, _pre_reconcile=pending is not None)
                if pending is not None and record["probe_id"] != pending:
                    continue
                if record["kind"] != "legacy-core-version":
                    continue
                recoverable = record["status"] in ("active", "failed") or (
                    pending == record["probe_id"] and record["status"] == "completed")
                if not recoverable or "compose" not in record["source"]:
                    continue
                source = record["source"]
                launcher = root / source["bootstrap"]["path"] / "component_recovery.py"
                probes.append({"status": "recorded-unverified", "argv": [
                    sys.executable, "-I", "-B", str(launcher), "--root", str(root),
                    "--kind", "versionprobe", "--id", record["probe_id"], "--compose-executable",
                    str(root / source["compose"]["path"]),
                ]})
                if pending is not None:
                    probes[-1]["staging"] = "pending-unverified"
            if probes:
                result["probe_recovery"] = probes
        elif os.path.lexists(deployments / _PROBE_STAGE_DIR):
            result["probe_recovery"] = {"status": "untrusted"}
    except (JournalError, manifest.ManifestError, OSError, KeyError, TypeError):
        result["probe_recovery"] = {"status": "untrusted"}
    builds = []
    for kind, directory in (("source", admission.BUILD_ATTEMPTS), ("wheel", admission.WHEEL_BUILD_ATTEMPTS)):
        if journal_states[directory] != "present":
            continue
        try:
            import component_recovery as recovery  # noqa: PLC0415
            import component_build as build  # noqa: PLC0415
            import component_wheel_build as wheel  # noqa: PLC0415
            for entry in sorted((deployments / directory).iterdir()):
                if not entry.name.endswith(".json"):
                    _fail("builder registry contains an unknown entry")
                identifier = _match(entry.name[:-5], _DEPLOYMENT_ID, "build ID")
                reader = build._read_attempt if kind == "source" else wheel.read_attempt
                complete = reader(root, entry.name)
                if complete["schema_version"] == 1:
                    if complete["status"] == "registered":
                        continue
                    _fail("legacy builder record requires separate inspection")
                _, record = recovery._build_attempt(root, kind, identifier)
                if record["status"] == "registered":
                    continue
                source = record["recovery_source"]
                launcher = root / source["bootstrap"]["path"] / "component_recovery.py"
                builds.append({"status": "recorded-unverified", "argv": [
                    sys.executable, "-I", "-B", str(launcher), "--root", str(root),
                    "--kind", "sourcebuild" if kind == "source" else "wheelbuild", "--id", identifier,
                ]})
        except (JournalError, recovery.RecoveryError, build.BuildError, OSError, KeyError, TypeError):
            result["build_recovery"] = {"status": "untrusted-or-legacy"}
            break
    else:
        if builds:
            result["build_recovery"] = builds
    if (_read_pointer(root, ".deploy_commit", _HEX40) != commit
            or _read_pointer(root, ".deploy_current", _DEPLOYMENT_ID) != deployment_id):
        _fail("deployment pointers changed during inspection")
    return result


def inspect_container(root: Path, component: str, container_id: str) -> dict[str, Any]:
    """Compare a real Docker container with a recorded component, read-only.

    Docker does not retain env-file provenance and this observation does not
    establish service health or the authenticity of the component's image.
    """
    if component not in ("api", "telegram"):
        _fail("unknown component")
    result = inspect_deployment(root)
    product = result["manifest"]
    if product is None or product["components"][component] is None:
        _fail("component is not present in recorded topology")
    installed = product["components"][component]
    # Lazy import keeps metadata-only inspection independent of Docker.
    import component_observe as observe  # noqa: PLC0415
    try:
        snapshot = manifest.read_recipe_snapshot(root, installed["recipe"]["sha256"])
        actual = observe.observe_container(
            container_id, expected_image_id=installed["image"]["id"],
            project=snapshot["project"], service=snapshot["service"],
            expected_command=snapshot["command"], expected_entrypoint=snapshot["entrypoint"],
            env_fingerprint_key=os.urandom(32),
        )
    except (manifest.ManifestError, observe.ObservationError) as exc:
        raise JournalError("recorded component cannot be observed") from exc
    recipe = actual["recipe"]
    keys = (set(manifest._RECIPE_FIELDS) - {"env_files", "container_name"}) | {"runtime_options"}
    if snapshot["container_name"] is not None:
        keys.add("container_name")
    different = sorted(key for key in keys if recipe[key] != snapshot[key])
    # An ephemeral fingerprint cannot establish env-file identity and need
    # not be disclosed by a one-shot read-only inspection.
    del actual["environment_fingerprint"]
    result["live_observation"] = {
        "component": component, **actual, "different_recipe_fields": different,
        "env_file_binding": "unverified", "source_provenance": "unverified",
    }
    return result


def observe_unrecorded(container_id: str, image_id: str, project: str, service: str,
                       command: Any, entrypoint: Any) -> dict[str, Any]:
    """Observe an explicitly identified container without asserting adoption."""
    import component_observe as observe  # noqa: PLC0415
    try:
        actual = observe.observe_container(
            container_id, expected_image_id=image_id, project=project, service=service,
            expected_command=command, expected_entrypoint=entrypoint,
            env_fingerprint_key=os.urandom(32),
        )
    except observe.ObservationError as exc:
        raise JournalError("container cannot be observed with the supplied binding") from exc
    del actual["environment_fingerprint"]
    return {"binding": "unverified", "env_file_binding": "unverified",
            "source_provenance": "unverified", **actual}


def _compose_input(path: Path) -> tuple[Any, ...]:
    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    before = path.lstat()
    content = _read_regular(path)
    after = path.lstat()
    if identity(before) != identity(after):
        _fail("Compose input changed while being read")
    return (*identity(after), hashlib.sha256(content).hexdigest())
