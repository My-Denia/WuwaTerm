#!/usr/bin/env python3
"""Shared, read-only admission for current deployment entrypoints.

The lock is the deployment root's inherited FD 9, not a newly opened file.
Historical source capsules contain their own original tools and do not use this
module. Admission does not itself enable the unfinished component transaction.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RUNTIME = ".runtime-transaction.json"
LEGACY_FULL = ".full-transaction.json"
COMPONENT_RUNTIME = ".component-runtime-transaction.json"
COMPONENT_FULL = ".component-full-transaction.json"
BUILD_ATTEMPTS = "component-build-attempts"
WHEEL_BUILD_ATTEMPTS = "component-wheel-build-attempts"
BUILD_STAGING = "component-build-staging"
WHEEL_BUILD_STAGING = "component-wheel-build-staging"
ADOPT_PROBES = "component-adopt-probes"
ADOPT_PROBE_STAGING = "component-adopt-probe-staging"
ISOLATED_BUILD_EXECUTION_READY = False
ARTIFACTS = "artifacts"
STATE_PROBE_LABEL = "org.wuwaterm.component-state-probe"
ADOPT_PROBE_LABEL = "org.wuwaterm.component-adopt-probe"
ROOT_LABEL = "org.wuwaterm.deploy-root-sha256"
_DOCKER_SOCKET = "/run/docker.sock"
_MAX_DOCKER_RESPONSE = 8 * 1024 * 1024
_DEPLOYMENT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")


class AdmissionError(RuntimeError):
    """The deployment state cannot safely admit this operation."""


def state_root(code_root: Path) -> Path:
    """The directory that holds a checkout's mutable deployment state.

    A release checkout `<base>/releases/rN` keeps data/, state/, state-api/,
    .env, .deployments/ and .deploy_commit in `<base>`, so that state outlives
    every release. Any other checkout is its own root.
    """
    if code_root.parent.name == "releases" and re.fullmatch(r"r[1-9][0-9]*", code_root.name):
        return code_root.parent.parent
    return code_root


def root_label_value(root: Path) -> str:
    """The one definition of a deployment root's helper scope label value."""
    return hashlib.sha256(os.path.realpath(os.fspath(root)).encode("utf-8")).hexdigest()


def _labelled_containers(label: str) -> list[dict]:
    """List every container on the local daemon carrying ``label``.

    A missing socket means no helper can have been created through it, so
    the list is empty; any other failure refuses admission.
    """
    import http.client  # noqa: PLC0415
    import socket  # noqa: PLC0415
    from urllib.parse import urlencode  # noqa: PLC0415

    try:
        info = os.stat(_DOCKER_SOCKET, follow_symlinks=False)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AdmissionError("Docker endpoint cannot be inspected for helper containers") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise AdmissionError("Docker endpoint is not a Unix socket")

    class Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(_DOCKER_SOCKET)

    query = urlencode({"all": "1", "filters": json.dumps({"label": [label]})})
    connection = Connection("localhost", timeout=15)
    try:
        connection.request("GET", "/containers/json?" + query)
        response = connection.getresponse()
        raw = response.read(_MAX_DOCKER_RESPONSE + 1)
        if response.status != 200 or len(raw) > _MAX_DOCKER_RESPONSE:
            raise AdmissionError("Docker did not list helper containers")
        value = json.loads(raw)
    except AdmissionError:
        raise
    except (OSError, ValueError, http.client.HTTPException, UnicodeError):
        raise AdmissionError("Docker helper container listing failed") from None
    finally:
        connection.close()
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise AdmissionError("Docker helper container listing is invalid")
    return value


def _probe_statuses(root: Path) -> dict[str, str]:
    """Return {probe_id: status} for every recorded probe in this root."""
    directory = _deployments(root, required=False) / ADOPT_PROBES
    if not _present(directory):
        return {}
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise AdmissionError("probe record directory is not a directory")
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
        import component_runtime  # noqa: PLC0415
        if Path(component_runtime.__file__).resolve() != CODE_ROOT / "scripts/component_runtime.py":
            raise AdmissionError("helper scan imported a foreign source module")
        result = {}
        for entry in sorted(directory.iterdir()):
            record = component_runtime._read_adopt_probe(root, entry.name)
            result[record["probe_id"]] = record["status"]
        return result
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("probe records cannot be read for the helper scan") from exc


def scan_root_helpers(root: Path) -> None:
    """Refuse a write while this root has an unrecorded or terminal helper container.

    A container belongs to this root when it carries this root's
    ``deploy-root-sha256`` label, or when its probe label names a probe
    recorded under this root (historical helpers have no root label). An
    active record's container is ordinary in-progress or crash-recovery state
    and is left to the existing reconciliation. A labelled container without
    a record, or a terminal record whose container still exists, needs the
    operator: every such full container ID is printed and nothing is written.
    """
    root = _physical_root(root)
    statuses = _probe_statuses(root)
    scope = root_label_value(root)
    findings = []
    for label in (STATE_PROBE_LABEL, ADOPT_PROBE_LABEL):
        for container in _labelled_containers(label):
            labels = container.get("Labels") or {}
            identifier = container.get("Id")
            if type(labels) is not dict or type(identifier) is not str:
                raise AdmissionError("Docker helper container listing is invalid")
            probe_id = labels.get(label)
            owned_by_root = labels.get(ROOT_LABEL) == scope
            status = statuses.get(probe_id) if type(probe_id) is str else None
            if status is None and not owned_by_root:
                continue
            if status == "active":
                continue
            reason = "no record" if status is None else f"record is {status}"
            findings.append(f"{identifier} ({label}={probe_id}; {reason})")
    if findings:
        raise AdmissionError(
            "helper containers for this deployment root require operator reconciliation "
            "before any write: " + ", ".join(sorted(findings))
        )


def _full_only_state_probe(kind: str) -> bool:
    """Helpers that compare or move state through FULL endpoints and copies."""
    return kind in ("state-legacy-plan", "state-canonical", "state-canonical-v4", "state-endpoint",
                    "state-copy-v4", "state-copy-stage", "state-copy-publish", "state-copy-clean",
                    "state-copy-discard")


def refuse_inadmissible_state_probe(journal: dict, probe: dict, transaction_kind: str) -> None:
    """Refuse a helper whose kind cannot run in this transaction kind.

    Schema 5 reads state directly through its own recorded image, so its
    read-only native plan and canonical comparison also run in a runtime
    transaction; every FULL4 endpoint or copy helper still requires a full one.
    """
    if journal["schema_version"] == 5 and probe["kind"] not in (
            "state-plan", "state-legacy-plan", "state-preimage", "state-canonical"):
        raise AdmissionError("schema-5 transactions permit only non-mutating state helpers")
    if probe["schema_version"] in (2, 3, 4, 5) and probe["transaction_kind"] != transaction_kind:
        raise AdmissionError("state probe transaction kind differs")
    if _full_only_state_probe(probe["kind"]) and transaction_kind != "full":
        if not (journal["schema_version"] == 5
                and probe["kind"] in ("state-legacy-plan", "state-canonical")):
            raise AdmissionError("full-only state probe cannot run in a runtime transaction")


def _check_artifact_attempts(root: Path) -> None:
    """Refuse mutator entry while a completed-artifact acquisition is unresolved.

    Registered and cancelled attempts are terminal. Lock order is FD9 then
    the short artifact-record lock; no Docker call is made under either.
    """
    store = _deployments(root, required=False) / ARTIFACTS
    if not _present(store / "attempts"):
        return
    try:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
        import component_artifact as artifact  # noqa: PLC0415
        if Path(artifact.__file__).resolve() != CODE_ROOT / "scripts/component_artifact.py":
            raise AdmissionError("artifact admission imported a foreign source module")
        with artifact.artifact_lock(root) as locked:
            pending = sorted(attempt for attempt in os.listdir(locked / "attempts")
                             if artifact.read_attempt_state(locked, attempt)["state"] not in ("registered", "cancelled"))
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("completed-artifact attempt records are unknown") from exc
    if pending:
        raise AdmissionError("completed-artifact acquisition requires recover or cancel: " + ", ".join(pending))


def _physical_root(root: Path) -> Path:
    root = Path(root)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise AdmissionError("deployment root is not a physical directory")
    except OSError as exc:
        raise AdmissionError("deployment root is unavailable") from exc
    return root


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdmissionError("deployment metadata cannot be inspected") from exc
    return True


def _deployments(root: Path, *, required: bool) -> Path:
    directory = root / ".deployments"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if required:
            raise AdmissionError("deployment metadata directory is missing") from None
    except OSError as exc:
        raise AdmissionError("deployment metadata directory cannot be inspected") from exc
    else:
        if not stat.S_ISDIR(info.st_mode):
            raise AdmissionError("deployment metadata directory is not a directory")
    return directory


def require_lock(root: Path) -> None:
    """Verify the root-bound regular lock inode and acquire its inherited OFD."""
    root = _physical_root(root)
    path = _deployments(root, required=True) / ".deployment.lock"
    try:
        actual = os.fstat(9)
        expected = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                or os.readlink("/proc/self/fd/9") != str(path)):
            raise AdmissionError("deployment lock FD9 does not match the root")
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise AdmissionError("deployment lock FD9 is unavailable") from exc


def _no_new_markers(root: Path) -> None:
    root = _physical_root(root)
    directory = _deployments(root, required=False)
    if any(_present(path) for path in (
        root / ".deploy_current", directory / COMPONENT_RUNTIME, directory / COMPONENT_FULL,
    )):
        raise AdmissionError(
            "component deployment metadata is present; inspect with "
            "python3 scripts/component_runtime.py --root <deployment-root> inspect "
            "(component recovery is not yet available)"
        )


def _attempts_clear(root: Path, *, allowed_probe_id: str | None = None,
                    allowed_state: tuple[str, str, str | None] | None = None,
                    allowed_builder: tuple[str, str] | None = None,
                    sync_registered: bool = False) -> None:
    """Reject interrupted or unknown build attempts, including unsafe paths."""
    matched_state = 0
    deployments = _deployments(root, required=False)
    if _present(deployments / ADOPT_PROBE_STAGING):
        try:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_runtime  # noqa: PLC0415
            if Path(component_runtime.__file__).resolve() != CODE_ROOT / "scripts/component_runtime.py":
                raise AdmissionError("probe staging imported a foreign source module")
            if component_runtime.pending_adopt_probe_staging(root) is not None:
                raise AdmissionError("interrupted probe staging requires recorded recovery")
        except AdmissionError:
            raise
        except Exception as exc:
            raise AdmissionError("probe staging metadata is unknown") from exc
    for name in (BUILD_STAGING, WHEEL_BUILD_STAGING):
        path = deployments / name
        if not _present(path):
            continue
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(path, flags)
            try:
                before = os.fstat(fd)
                named = path.stat(follow_symlinks=False)
                if (not stat.S_ISDIR(before.st_mode) or not stat.S_ISDIR(named.st_mode)
                        or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
                    raise AdmissionError("builder staging directory identity differs")
                if os.listdir(fd):
                    raise AdmissionError("unreconciled builder staging record requires recovery")
                after = path.stat(follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise AdmissionError("builder staging directory changed during admission")
            finally:
                os.close(fd)
        except AdmissionError:
            raise
        except OSError as exc:
            raise AdmissionError("builder staging directory cannot be inspected") from exc
    for name in (BUILD_ATTEMPTS, WHEEL_BUILD_ATTEMPTS, ADOPT_PROBES):
        directory = deployments / name
        if not _present(directory):
            continue
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise AdmissionError("build attempt directory is not a directory")
            # Both parsers validate the complete record rather than trusting a
            # caller-controlled status string.
            entries = list(directory.iterdir())
            if entries:
                sys.path.insert(0, str(CODE_ROOT / "scripts"))
                if name == BUILD_ATTEMPTS:
                    import component_build  # noqa: PLC0415
                    reader = component_build._read_attempt
                    verifier = component_build.verify_registered_build
                    module = component_build
                    module_name = "component_build.py"
                elif name == WHEEL_BUILD_ATTEMPTS:
                    import component_wheel_build  # noqa: PLC0415
                    reader = component_wheel_build.read_attempt
                    verifier = component_wheel_build.verify_registered_wheel_build
                    module = component_wheel_build
                    module_name = "component_wheel_build.py"
                else:
                    import component_runtime  # noqa: PLC0415
                    reader = component_runtime._read_adopt_probe
                    verifier = None
                    module = component_runtime
                    module_name = "component_runtime.py"
                if Path(module.__file__).resolve() != CODE_ROOT / "scripts" / module_name:
                    raise AdmissionError("build admission imported a foreign source module")
                for entry in entries:
                    record = reader(root, entry.name)
                    if record["status"] == "active":
                        legacy_match = (name == ADOPT_PROBES and record["kind"] == "legacy-core-version"
                                        and record["probe_id"] == allowed_probe_id)
                        state_match = (name == ADOPT_PROBES and record["kind"] in (
                                           "state-plan", "state-legacy-plan", "state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4",
                                           "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard")
                                       and allowed_state is not None
                                       and allowed_state[:2] == (record["transaction_id"], record["source_tool_sha256"])
                                       and (allowed_state[2] is None or allowed_state[2] == record["probe_id"]))
                        builder_match = (allowed_builder is not None and
                                         ((name == BUILD_ATTEMPTS and allowed_builder[0] == "source")
                                          or (name == WHEEL_BUILD_ATTEMPTS and allowed_builder[0] == "wheel"))
                                         and record["build_id"] == allowed_builder[1])
                        if state_match:
                            matched_state += 1
                            if matched_state > 1:
                                raise AdmissionError("multiple active state probes require reconciliation")
                        if not legacy_match and not state_match and not builder_match:
                            raise AdmissionError("interrupted component build or adoption probe requires recovery")
                    if record["status"] == "registered":
                        if verifier is None:
                            raise AdmissionError("unknown registered build attempt")
                        verifier(root, record["receipt_sha256"])
                        if sync_registered and record.get("schema_version") == 2:
                            import component_build_isolation as isolation  # noqa: PLC0415
                            if Path(isolation.__file__).resolve() != CODE_ROOT / "scripts/component_build_isolation.py":
                                raise AdmissionError("registration imported a foreign publication module")
                            kind = "source" if name == BUILD_ATTEMPTS else "wheel"
                            isolation.sync_registered_attempt(root, kind, record["build_id"], record)
                    elif (record["status"] == "failed" and record.get("schema_version") == 1
                          and name in (BUILD_ATTEMPTS, WHEEL_BUILD_ATTEMPTS)):
                        raise AdmissionError("legacy failed builder lacks verified cleanup evidence")
                    elif (name in (BUILD_ATTEMPTS, WHEEL_BUILD_ATTEMPTS)
                          and record["status"] in ("failed", "retained_import") and record.get("schema_version") == 2):
                        terminal = (getattr(module, "verify_build_terminal", None) if name == BUILD_ATTEMPTS else
                                    getattr(module, "verify_wheel_build_terminal", None) if name == WHEEL_BUILD_ATTEMPTS else None)
                        if terminal is None or terminal(root, record["build_id"]) != record:
                            raise AdmissionError("terminal builder resources cannot be verified")
        except AdmissionError:
            raise
        except Exception as exc:
            raise AdmissionError("build attempt metadata is unknown") from exc


def legacy_metadata_absent(root: Path, *, sync_registered: bool = False) -> None:
    """Inspect legacy compatibility; only held-lock mutators finish publication."""
    _no_new_markers(root)
    _attempts_clear(root, sync_registered=sync_registered)


def admit_legacy(root: Path) -> None:
    """Admit a legacy parent or its helper under their same held lock."""
    require_lock(root)
    legacy_metadata_absent(root, sync_registered=True)


def require_root_paths(root: Path, *paths: Path) -> None:
    """Reject CLI write paths outside the physical root or through symlinks."""
    root = _physical_root(root)
    for path in paths:
        target = Path(path)
        if not target.is_absolute():
            target = Path.cwd() / target
        if target == root or not target.is_relative_to(root):
            raise AdmissionError("write path is outside the deployment root")
        current = root
        for part in target.relative_to(root).parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AdmissionError("write path cannot be inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                raise AdmissionError("write path contains a symlink")
        resolved = target.resolve(strict=False)
        if resolved == root or not resolved.is_relative_to(root):
            raise AdmissionError("write path is outside the deployment root")


def _component_conflicts(root: Path, *, allowed_probe_id: str | None = None,
                         allowed_state: tuple[str, str, str | None] | None = None,
                         allowed_full_id: str | None = None,
                         runtime_recovery: dict | None = None,
                         allowed_builder: tuple[str, str] | None = None) -> Path:
    directory = _deployments(root, required=True)
    if _present(directory / LEGACY_FULL):
        raise AdmissionError("legacy full transaction requires recovery")
    old_runtime = directory / LEGACY_RUNTIME
    if _present(old_runtime):
        try:
            if not stat.S_ISREG(old_runtime.lstat().st_mode):
                raise ValueError("not a regular journal")
            # Reject duplicate keys before using the existing full schema parser.
            def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            json.loads(old_runtime.read_bytes(), object_pairs_hook=unique)
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import runtime_update  # noqa: PLC0415
            if Path(runtime_update.__file__).resolve() != CODE_ROOT / "scripts" / "runtime_update.py":
                raise ValueError("foreign runtime module")
            previous = runtime_update._load_journal(old_runtime)
        except Exception as exc:
            raise AdmissionError("legacy runtime journal is unknown") from exc
        if previous["status"] not in ("committed", "rolled_back"):
            raise AdmissionError("legacy runtime transaction requires recovery")
    full_path = directory / COMPONENT_FULL
    if _present(full_path):
        try:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_full as full  # noqa: PLC0415
            import component_runtime as runtime  # noqa: PLC0415
            if Path(full.__file__).resolve() != CODE_ROOT / "scripts/component_full.py":
                raise ValueError("foreign full transaction module")
            full_raw = runtime._read_regular(full_path)
            full_journal = full.validate_journal(runtime._decode(full_raw))
            if allowed_full_id is not None:
                if full_journal["transaction_id"] != allowed_full_id:
                    raise ValueError("full transaction ID differs")
            elif runtime_recovery is not None:
                full.verify_handoff(root, successor_kind="runtime", successor=runtime_recovery,
                                    predecessor=full_journal, predecessor_raw=full_raw)
            elif (full_journal["status"] not in ("committed", "rolled_back")
                  or _present(directory / COMPONENT_RUNTIME)):
                raise ValueError("full transaction or handoff requires recovery")
            if runtime._read_regular(full_path) != full_raw:
                raise ValueError("full transaction changed during admission")
        except Exception as exc:
            raise AdmissionError("component full transaction requires recovery") from exc
    _attempts_clear(root, allowed_probe_id=allowed_probe_id, allowed_state=allowed_state,
                    allowed_builder=allowed_builder, sync_registered=True)
    scan_root_helpers(root)
    return directory


def _require_execution_readiness(operation: str) -> None:
    """Require the saved-source recovery capability for the write being admitted."""
    if operation not in ("adopt", "update", "full-update"):
        return
    try:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
        import component_recovery as recovery  # noqa: PLC0415
        import component_runtime as runtime  # noqa: PLC0415
        if (Path(recovery.__file__).resolve() != CODE_ROOT / "scripts/component_recovery.py"
                or Path(runtime.__file__).resolve() != CODE_ROOT / "scripts/component_runtime.py"):
            raise AdmissionError("component readiness imported a foreign source module")
        full = operation == "full-update"
        protocol = getattr(runtime, "CAPSULE_FULL_RECOVERY_PROTOCOL" if full else "CAPSULE_RECOVERY_PROTOCOL", None)
        # Protocol 1 keeps its original flags; protocol 2 has its own. Any
        # other declaration is refused.
        ready = recovery.protocol_ready(protocol, full=full)
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("component recovery readiness cannot be verified") from exc
    if ready is not True:
        kind = "full" if full else "runtime"
        raise AdmissionError(f"{kind} saved-source recovery is not ready")


def admit_component(root: Path, *, operation: str, code_root: Path,
                    transaction_id: str | None = None) -> None:
    """Admit a normal component operation from its own physical checkout."""
    root = _physical_root(root)
    if root != _physical_root(code_root) or root != CODE_ROOT:
        raise AdmissionError("component source and deployment roots differ")
    if operation not in ("build", "prepare-source", "adopt", "update", "full-update") or transaction_id is not None:
        raise AdmissionError("recovery requires verified capsule admission")
    require_lock(root)
    if operation == "build" and not ISOLATED_BUILD_EXECUTION_READY:
        # Existing producers cannot prove that a cancelled daemon-side build
        # has stopped. No entrypoint may start one until that lifecycle is
        # implemented and verified, including direct Python callers.
        raise AdmissionError("component builds are unavailable until cancellation is supported")
    _require_execution_readiness(operation)
    _check_artifact_attempts(root)
    directory = _component_conflicts(root)
    current = directory / COMPONENT_RUNTIME
    if _present(current):
        try:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_runtime  # noqa: PLC0415
            if Path(component_runtime.__file__).resolve() != CODE_ROOT / "scripts" / "component_runtime.py":
                raise ValueError("foreign component runtime module")
            journal = component_runtime.load_journal(current)
        except Exception as exc:
            raise AdmissionError("component runtime journal is unknown") from exc
        if journal["status"] not in ("committed", "rolled_back"):
            raise AdmissionError("component runtime transaction requires recovery")
    pointer = root / ".deploy_current"
    if _present(pointer):
        try:
            if not stat.S_ISREG(pointer.lstat().st_mode):
                raise ValueError("pointer is not a regular file")
            value = pointer.read_text(encoding="ascii")
            if not value.endswith("\n") or not _DEPLOYMENT_ID.fullmatch(value[:-1]):
                raise ValueError("invalid pointer")
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_runtime  # noqa: PLC0415
            # Syntax is not proof of a recorded binding. The inspector checks
            # the immutable manifest and both pointers without claiming live
            # service health.
            if component_runtime.inspect_deployment(root)["binding"] != "recorded":
                raise ValueError("unverified pointer")
        except (OSError, UnicodeError, ValueError) as exc:
            raise AdmissionError("component deployment pointer is unknown") from exc


def _verify_recovery_extraction(root: Path, source: dict, code_root: Path) -> None:
    """Verify the whole executing A extraction against its saved capsule."""
    import component_recovery as recovery  # noqa: PLC0415
    import component_runtime as runtime  # noqa: PLC0415
    for module, name in ((recovery, "component_recovery.py"), (runtime, "component_runtime.py")):
        if Path(module.__file__).resolve() != code_root / "scripts" / name:
            raise AdmissionError("recovery imported a foreign source module")
    recovery._validate_source(source)
    recovery._verify_bootstrap(root, source)
    recovery._load_packager(code_root / "scripts" / "package_recovery_source.py")
    recovery._verify_capsule(root, source)
    if runtime.tool_bytes_identity(code_root) != source["tool_bytes_sha256"]:
        raise AdmissionError("recovery tool bytes differ from the saved source")
    manifest_bytes = runtime._read_regular(root / source["capsule"]["path"] / "manifest.json")
    if hashlib.sha256(manifest_bytes).hexdigest() != source["capsule"]["manifest_sha256"]:
        raise AdmissionError("recovery capsule manifest changed")
    files = runtime._decode(manifest_bytes)["files"]
    seen = set()
    for current, directories, names in os.walk(code_root, followlinks=False):
        for name in directories:
            if not stat.S_ISDIR((Path(current) / name).lstat().st_mode):
                raise AdmissionError("recovery source contains an unsafe directory")
        for name in names:
            item = Path(current) / name
            relative = item.relative_to(code_root).as_posix()
            expected = files.get(relative)
            info = item.lstat()
            if (expected is None or not stat.S_ISREG(info.st_mode)
                    or info.st_size != expected["size"]
                    or bool(info.st_mode & 0o111) != (expected["mode"] == "100755")
                    or recovery._hash(item) != expected["sha256"]):
                raise AdmissionError("recovery extraction differs from its saved source")
            seen.add(relative)
    if seen != set(files):
        raise AdmissionError("recovery extraction is incomplete")
    recovery._verify_capsule(root, source)
    recovery._verify_bootstrap(root, source)


def admit_component_recovery(root: Path, *, transaction_id: str,
                             source_tool_sha256: str, code_root: Path) -> dict:
    """Admit only the recorded transaction from its independently verified A tree.

    The deployment checkout may now contain source B. Never import it: the
    caller and all verification tools must be the executing, extracted A tree.
    Split pointers are allowed only when each exactly matches the journal's
    recorded old or target bytes, including original absence.
    """
    root = _physical_root(root)
    code_root = _physical_root(code_root)
    if code_root != CODE_ROOT:
        raise AdmissionError("recovery code root differs from executing source")
    require_lock(root)
    directory = _deployments(root, required=True)
    path = directory / COMPONENT_RUNTIME
    if not _present(path):
        raise AdmissionError("component recovery transaction is missing")
    sys.path.insert(0, str(code_root / "scripts"))
    try:
        import component_recovery as recovery  # noqa: PLC0415
        import component_runtime as runtime  # noqa: PLC0415
        for module, name in ((recovery, "component_recovery.py"), (runtime, "component_runtime.py")):
            if Path(module.__file__).resolve() != code_root / "scripts" / name:
                raise AdmissionError("recovery imported a foreign source module")
        raw = runtime._read_regular(path)
        journal = runtime.validate_journal(runtime._decode(raw))
        source = journal["source"]
        if (journal["transaction_id"] != transaction_id
                or journal["status"] not in ("active", "unresolved", "rolled_back", "committed")
                or source["source_tool_sha256"] != source_tool_sha256):
            raise AdmissionError("component recovery identity does not match the active transaction")
        _verify_recovery_extraction(root, source, code_root)
        if journal["schema_version"] in (4, 5):
            import component_full as full  # noqa: PLC0415
            if Path(full.__file__).resolve() != code_root / "scripts/component_full.py":
                raise AdmissionError("component recovery imported a foreign full module")
            full.verify_predecessor(root, successor_kind="runtime", successor=journal)

        allowed_state = ((transaction_id, source_tool_sha256, None)
                         if journal["action"] == "update" and "telegram" in journal["selection"]
                         and journal["status"] in ("active", "unresolved") else None)
        _component_conflicts(root, allowed_state=allowed_state, runtime_recovery=journal)
        for name, pattern in ((".deploy_commit", runtime._HEX40),
                              (".deploy_current", runtime._DEPLOYMENT_ID)):
            actual = runtime._read_pointer(root, name, pattern)
            actual = {"present": actual is not None,
                      "bytes": None if actual is None else actual + "\n"}
            if journal["status"] == "rolled_back":
                allowed = (journal["pointers"]["old"][name],)
            elif journal["status"] == "committed":
                allowed = (journal["pointers"]["target"][name],)
            else:
                allowed = (journal["pointers"]["old"][name], journal["pointers"]["target"][name])
            if actual not in allowed:
                raise AdmissionError("component recovery pointer differs from both recorded values")
        if runtime._read_regular(path) != raw:
            raise AdmissionError("component recovery journal changed during admission")
        recovery._verify_bootstrap(root, source)
        return journal
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("component recovery source or journal cannot be verified") from exc


def admit_full_recovery(root: Path, *, transaction_id: str,
                        source_tool_sha256: str, code_root: Path) -> dict:
    """Admit the exact recorded FULL3 transaction from its saved extraction."""
    root = _physical_root(root)
    code_root = _physical_root(code_root)
    if code_root != CODE_ROOT:
        raise AdmissionError("full recovery code root differs from executing source")
    require_lock(root)
    directory = _deployments(root, required=True)
    path = directory / COMPONENT_FULL
    if not _present(path):
        raise AdmissionError("full recovery transaction is missing")
    try:
        sys.path.insert(0, str(code_root / "scripts"))
        import component_full as full  # noqa: PLC0415
        import component_runtime as runtime  # noqa: PLC0415
        import component_recovery as recovery  # noqa: PLC0415
        if Path(full.__file__).resolve() != code_root / "scripts/component_full.py":
            raise AdmissionError("full recovery imported a foreign source module")
        raw = runtime._read_regular(path)
        journal = full.validate_journal(runtime._decode(raw))
        source = journal["source"]
        if (journal["transaction_id"] != transaction_id
                or journal["status"] not in ("active", "unresolved", "rolled_back", "committed")
                or source["source_tool_sha256"] != source_tool_sha256):
            raise AdmissionError("full recovery identity differs")
        _verify_recovery_extraction(root, source, code_root)
        full.verify_predecessor(root, successor_kind="full", successor=journal)
        allowed_state = ((transaction_id, source_tool_sha256, None)
                         if journal["before"]["topology"]["telegram"] is not None
                         and journal["status"] in ("active", "unresolved") else None)
        _component_conflicts(root, allowed_state=allowed_state, allowed_full_id=transaction_id)
        runtime_path = directory / COMPONENT_RUNTIME
        if _present(runtime_path):
            predecessor_raw = runtime._read_regular(runtime_path)
            predecessor = runtime.validate_journal(runtime._decode(predecessor_raw))
            full.verify_handoff(root, successor_kind="full", successor=journal,
                                predecessor=predecessor, predecessor_raw=predecessor_raw)
        for name, pattern in ((".deploy_commit", runtime._HEX40), (".deploy_current", runtime._DEPLOYMENT_ID)):
            value = runtime._read_pointer(root, name, pattern)
            actual = {"present": value is not None, "bytes": None if value is None else value + "\n"}
            if journal["status"] == "rolled_back":
                allowed = (journal["pointers"]["old"][name],)
            elif journal["status"] == "committed":
                allowed = (journal["pointers"]["target"][name],)
            else:
                allowed = (journal["pointers"]["old"][name], journal["pointers"]["target"][name])
            if actual not in allowed:
                raise AdmissionError("full recovery pointer differs from recorded values")
        if runtime._read_regular(path) != raw:
            raise AdmissionError("full recovery journal changed during admission")
        recovery._verify_bootstrap(root, source)
        recovery._verify_bound_compose(root, source)
        return journal
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("full recovery source or journal cannot be verified") from exc


def admit_builder_recovery(root: Path, *, kind: str, build_id: str,
                           source: dict, code_root: Path) -> dict:
    """Admit only the recorded schema-2 builder from verified saved source A."""
    root = _physical_root(root)
    code_root = _physical_root(code_root)
    if code_root != CODE_ROOT or kind not in ("source", "wheel"):
        raise AdmissionError("builder recovery kind or executing source differs")
    if type(build_id) is not str or not _DEPLOYMENT_ID.fullmatch(build_id):
        raise AdmissionError("builder recovery ID is invalid")
    require_lock(root)
    try:
        sys.path.insert(0, str(code_root / "scripts"))
        import component_recovery as recovery  # noqa: PLC0415
        import component_runtime as runtime  # noqa: PLC0415
        if Path(recovery.__file__).resolve() != code_root / "scripts/component_recovery.py":
            raise AdmissionError("builder recovery imported a foreign source")
        source = recovery._validate_source(source)
        _verify_recovery_extraction(root, source, code_root)
        if kind == "source":
            import component_build as producer  # noqa: PLC0415
            name, filename, reader = BUILD_ATTEMPTS, "component_build.py", producer._read_attempt
        else:
            import component_wheel_build as producer  # noqa: PLC0415
            name, filename, reader = WHEEL_BUILD_ATTEMPTS, "component_wheel_build.py", producer.read_attempt
        if Path(producer.__file__).resolve() != code_root / "scripts" / filename:
            raise AdmissionError("builder recovery imported a foreign producer")
        path = _deployments(root, required=True) / name / (build_id + ".json")
        raw = runtime._read_regular(path)
        record = reader(root, build_id + ".json")
        if (type(record.get("schema_version")) is not int or record["schema_version"] != 2
                or record["build_id"] != build_id or record["recovery_source"] != source
                or record["status"] not in ("active", "failed", "retained_import", "registered")):
            raise AdmissionError("builder recovery record differs")
        directory = _component_conflicts(root, allowed_builder=(kind, build_id))
        current = directory / COMPONENT_RUNTIME
        if _present(current):
            journal = runtime.load_journal(current)
            if journal["status"] not in ("committed", "rolled_back"):
                raise AdmissionError("runtime transaction conflicts with builder recovery")
        if runtime._read_regular(path) != raw:
            raise AdmissionError("builder attempt changed during recovery admission")
        recovery._verify_bootstrap(root, source)
        return record
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("builder recovery source or attempt cannot be verified") from exc


def admit_state_probe(root: Path, *, transaction_id: str, source_tool_sha256: str,
                      code_root: Path, probe_id: str | None = None,
                      transaction_kind: str = "runtime") -> dict:
    """Admit one fixed state probe only for its verified Telegram transaction."""
    root = _physical_root(root)
    code_root = _physical_root(code_root)
    if code_root != CODE_ROOT:
        raise AdmissionError("state probe code root differs from executing source")
    if transaction_kind not in ("runtime", "full"):
        raise AdmissionError("state probe transaction kind is invalid")
    if probe_id is not None and (type(probe_id) is not str or not _DEPLOYMENT_ID.fullmatch(probe_id)):
        raise AdmissionError("invalid state probe ID")
    require_lock(root)
    sys.path.insert(0, str(code_root / "scripts"))
    try:
        import component_runtime as runtime  # noqa: PLC0415
        import component_recovery as recovery  # noqa: PLC0415
        import component_full as full  # noqa: PLC0415
        for module, filename in ((runtime, "component_runtime.py"), (recovery, "component_recovery.py")):
            if Path(module.__file__).resolve() != code_root / "scripts" / filename:
                raise AdmissionError("state probe imported a foreign source module")
        if code_root != root:
            journal = (admit_full_recovery(root, transaction_id=transaction_id,
                                           source_tool_sha256=source_tool_sha256, code_root=code_root)
                       if transaction_kind == "full" else
                       admit_component_recovery(root, transaction_id=transaction_id,
                                                source_tool_sha256=source_tool_sha256, code_root=code_root))
            if probe_id is None:
                _component_conflicts(root, allowed_full_id=transaction_id if transaction_kind == "full" else None)
        else:
            path = _deployments(root, required=True) / (COMPONENT_FULL if transaction_kind == "full" else COMPONENT_RUNTIME)
            raw = runtime._read_regular(path)
            journal = (full.validate_journal(runtime._decode(raw)) if transaction_kind == "full" else
                       runtime.validate_journal(runtime._decode(raw)))
            source = journal["source"]
            if (journal["transaction_id"] != transaction_id
                    or source["source_tool_sha256"] != source_tool_sha256
                    or journal["status"] not in ("active", "unresolved")):
                raise AdmissionError("state probe does not match the active transaction")
            recovery._load_packager(code_root / "scripts/package_recovery_source.py")
            recovery._verify_clean_source(root, source["commit"], source["tree"])
            if runtime.tool_bytes_identity(root) != source["tool_bytes_sha256"]:
                raise AdmissionError("state probe source bytes differ")
            recovery._verify_bootstrap(root, source)
            recovery._verify_capsule(root, source)
            if transaction_kind == "full":
                full.verify_predecessor(root, successor_kind="full", successor=journal)
            allowed = None if probe_id is None else (transaction_id, source_tool_sha256, probe_id)
            _component_conflicts(root, allowed_state=allowed,
                                 allowed_full_id=transaction_id if transaction_kind == "full" else None)
            if transaction_kind == "full" and _present(_deployments(root, required=True) / COMPONENT_RUNTIME):
                previous_path = _deployments(root, required=True) / COMPONENT_RUNTIME
                previous_raw = runtime._read_regular(previous_path)
                previous = runtime.validate_journal(runtime._decode(previous_raw))
                full.verify_handoff(root, successor_kind="full", successor=journal,
                                    predecessor=previous, predecessor_raw=previous_raw)
            for name, pattern in ((".deploy_commit", runtime._HEX40),
                                  (".deploy_current", runtime._DEPLOYMENT_ID)):
                value = runtime._read_pointer(root, name, pattern)
                actual = {"present": value is not None, "bytes": None if value is None else value + "\n"}
                if actual not in (journal["pointers"]["old"][name], journal["pointers"]["target"][name]):
                    raise AdmissionError("state probe pointer differs from the transaction")
            if runtime._read_regular(path) != raw:
                raise AdmissionError("state probe journal changed during admission")
        telegram_selected = (journal["before"]["topology"]["telegram"] is not None if transaction_kind == "full"
                             else "telegram" in journal["selection"])
        if (journal["action"] != "update" or not telegram_selected or journal["status"] not in ("active", "unresolved")):
            raise AdmissionError("state probe requires an active Telegram update")
        if probe_id is not None:
            probe = runtime._read_adopt_probe(root, probe_id + ".json")
            if (probe["status"] != "active" or probe["kind"] not in (
                    "state-plan", "state-legacy-plan", "state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4",
                    "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard")
                    or probe["transaction_id"] != transaction_id
                    or probe["source_tool_sha256"] != source_tool_sha256):
                raise AdmissionError("state probe does not match its transaction")
            refuse_inadmissible_state_probe(journal, probe, transaction_kind)
            if probe["schema_version"] == 4:
                import component_state as state  # noqa: PLC0415
                if Path(state.__file__).resolve() != code_root / "scripts/component_state.py":
                    raise AdmissionError("state probe imported a foreign verifier")
                if probe["kind"] in ("state-plan", "state-legacy-plan"):
                    state.verify_plan_probe_binding(root, journal, probe)
                elif probe["kind"] == "state-endpoint":
                    state.verify_endpoint_probe_binding(root, journal, probe)
                elif probe["kind"] == "state-copy-v4":
                    state.verify_copy_probe_binding(root, journal, probe)
                else:
                    state.verify_canonical_probe_binding(root, journal, probe)
            elif probe["schema_version"] == 5:
                import component_state as state  # noqa: PLC0415
                if Path(state.__file__).resolve() != code_root / "scripts/component_state.py":
                    raise AdmissionError("state probe imported a foreign verifier")
                state.verify_schema5_probe_binding(journal, probe)
        return journal
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("state probe source or transaction cannot be verified") from exc


_UNSET = object()


def admit_adoption_probe_context(root: Path, *, source: dict, image_id: str, revision: str,
                                 code_root: Path, compose_path: Path | None,
                                 probe_id: str | None = None,
                                 predecessor_raw: bytes | None | object = _UNSET,
                                 extracted_source: bool = False,
                                 recovering: bool = False) -> tuple[dict | None, bytes | None]:
    """Admit one source-bound legacy probe beside an independently verified terminal journal."""
    root = _physical_root(root)
    code_root = _physical_root(code_root)
    if code_root != CODE_ROOT or (not extracted_source and root != code_root):
        raise AdmissionError("probe source and deployment roots differ")
    if probe_id is not None and (type(probe_id) is not str or not _DEPLOYMENT_ID.fullmatch(probe_id)):
        raise AdmissionError("invalid adoption probe ID")
    require_lock(root)
    directory = _component_conflicts(root, allowed_probe_id=probe_id)
    try:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
        import component_runtime  # noqa: PLC0415
        import component_recovery  # noqa: PLC0415
        import component_observe  # noqa: PLC0415
        if Path(component_runtime.__file__).resolve() != CODE_ROOT / "scripts/component_runtime.py":
            raise ValueError("foreign runtime module")
        if Path(component_recovery.__file__).resolve() != CODE_ROOT / "scripts/component_recovery.py":
            raise ValueError("foreign recovery module")
        if Path(component_observe.__file__).resolve() != CODE_ROOT / "scripts/component_observe.py":
            raise ValueError("foreign observation module")
        source = component_recovery._validate_source(source)
        if "compose" in source and component_recovery._verify_bound_compose(root, source) != compose_path:
            raise ValueError("probe Compose executable differs from its saved identity")
        if extracted_source:
            _verify_recovery_extraction(root, source, code_root)
        else:
            component_recovery._load_packager(CODE_ROOT / "scripts/package_recovery_source.py")
            component_recovery._verify_clean_source(root, source["commit"], source["tree"])
            if component_runtime.tool_bytes_identity(root) != source["tool_bytes_sha256"]:
                raise ValueError("probe source tools differ")
            component_recovery._verify_bootstrap(root, source)
            component_recovery._verify_capsule(root, source)
        component_runtime._image_id(image_id, "probe image")
        component_runtime._match(revision, component_runtime._HEX40, "probe image revision")
        image = component_observe._docker_json("/images/" + image_id + "/json")
        if (image.get("Id") != image_id
                or (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != revision):
            raise ValueError("probe image differs")
        daemon = component_runtime._adopt_probe_daemon()
        record = None
        if probe_id is not None:
            record = component_runtime._read_adopt_probe(root, probe_id + ".json")
            statuses = ("active", "failed", "completed") if recovering else (
                ("active", "failed") if extracted_source else ("active",))
            if (record["status"] not in statuses
                    or record["kind"] != "legacy-core-version"
                    or record["source"] != source or record["image_id"] != image_id
                    or record["image_revision"] != revision or record["daemon_id"] != daemon):
                raise ValueError("probe differs from its context")
        current = directory / COMPONENT_RUNTIME
        raw = None
        if _present(current):
            if compose_path is None:
                raise ValueError("terminal journal verification requires Compose")
            import component_transaction  # noqa: PLC0415
            if Path(component_transaction.__file__).resolve() != CODE_ROOT / "scripts/component_transaction.py":
                raise ValueError("foreign transaction module")
            if extracted_source:
                raw = component_runtime._read_regular(current)
                previous = component_runtime.validate_journal(component_runtime._decode(raw))
                component_transaction._verify_terminal_record(root, previous, raw, compose_path)
            else:
                verified = component_transaction.verify_terminal(root, compose_path=compose_path)
                if verified is None:
                    raise ValueError("terminal journal disappeared")
                raw = verified[0]
        elif _present(root / ".deploy_current"):
            raise ValueError("component pointer has no terminal journal")
        actual_predecessor = None if raw is None else hashlib.sha256(raw).hexdigest()
        if record is not None and record["predecessor_sha256"] != actual_predecessor:
            raise ValueError("probe predecessor context differs")
        if predecessor_raw is not _UNSET and raw != predecessor_raw:
            raise ValueError("terminal journal changed")
        component_runtime._read_pointer(root, ".deploy_commit", component_runtime._HEX40)
        return record, raw
    except Exception as exc:
        raise AdmissionError("adoption probe source, image or terminal context cannot be verified") from exc


def admit_adoption_probe_recovery(root: Path, *, probe_id: str, code_root: Path,
                                  compose_path: Path | None = None,
                                  extracted_source: bool = False) -> tuple[dict, bytes | None]:
    """Admit recovery of one recorded probe, preserving any terminal result."""
    root = _physical_root(root)
    if type(probe_id) is not str or not _DEPLOYMENT_ID.fullmatch(probe_id):
        raise AdmissionError("invalid adoption probe ID")
    try:
        sys.path.insert(0, str(CODE_ROOT / "scripts"))
        import component_runtime  # noqa: PLC0415
        record = component_runtime._read_adopt_probe(root, probe_id + ".json")
        admitted, raw = admit_adoption_probe_context(
            root, source=record["source"], image_id=record["image_id"], revision=record["image_revision"],
            code_root=code_root, compose_path=compose_path, probe_id=probe_id, extracted_source=extracted_source,
            recovering=True,
        )
        if admitted is None:
            raise ValueError("probe is missing")
        return admitted, raw
    except Exception as exc:
        raise AdmissionError("matching active adoption probe cannot be verified") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("legacy", "legacy-precheck"))
    args = parser.parse_args()
    try:
        if args.operation == "legacy":
            admit_legacy(state_root(CODE_ROOT))
        else:
            legacy_metadata_absent(state_root(CODE_ROOT))
    except AdmissionError as exc:
        print(f"deployment admission refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
