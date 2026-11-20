#!/usr/bin/env python3
"""Manage saved-source legacy image version probes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

# Historical recovery executes this module from the verified source capsule,
# with isolated mode enabled. Do not import modules from the deployment root.
CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
sys.path.insert(0, str(CODE_ROOT / "src"))
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402

from component_journal_runtime import (  # noqa: E402
    _HEX40,
    _HEX64,
    _DEPLOYMENT_ID,
    _LIMIT,
    JournalError,
    _fail,
    _object,
    _match,
    _image_id,
    tool_bytes_identity,
    _decode,
    _read_regular,
)


_LEGACY_VERSION_CODE = (
    "import importlib.metadata,sys,sysconfig; from pathlib import Path; "
    "p=str(Path(sys.executable).parent.parent); "
    "sys.path.append(sysconfig.get_path('purelib',vars={'base':p,'platbase':p})); "
    "print(importlib.metadata.version('wuwaterm'))"
)


def _env_mapping(items: Any) -> dict[str, str]:
    if type(items) is not list:
        _fail("container environment is invalid")
    result = {}
    for item in items:
        if type(item) is not str or "=" not in item:
            _fail("container environment is invalid")
        name, value = item.split("=", 1)
        if not manifest._ENV_NAME.fullmatch(name) or name in result:
            _fail("container environment is ambiguous")
        result[name] = value
    return result

_PROBE_STAGE_DIR = "component-adopt-probe-staging"

_PROBE_STAGE_NAME = re.compile(r"([0-9]{8}T[0-9]{6}Z-[0-9a-f]{32})\.([0-9a-f]{64})\.(claim|stage)\Z")

_PROBE_STAT_KEYS = {"device", "inode", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "nlink"}

_PROBE_STABLE_KEYS = _PROBE_STAT_KEYS - {"ctime_ns", "nlink"}

_PROBE_PARENT_KEYS = {"device", "inode", "mode", "uid", "gid"}


def _probe_file_stat(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
            "nlink": info.st_nlink}


def _probe_parent_stat(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid}


def _probe_read_at(directory: int, name: str) -> tuple[bytes, dict[str, int]]:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size > _LIMIT:
            _fail("probe staging object is not a singly linked regular file")
        data = bytearray()
        while chunk := os.read(fd, 65536):
            data.extend(chunk)
            if len(data) > _LIMIT:
                _fail("probe staging object exceeds the limit")
        final = os.fstat(fd)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (_probe_file_stat(initial) != _probe_file_stat(final) or _probe_file_stat(final) != _probe_file_stat(named)
                or len(data) != final.st_size):
            _fail("probe staging object changed while reading")
        return bytes(data), _probe_file_stat(final)
    finally:
        os.close(fd)


def _probe_staging(root: Path, *, reconcile: bool, probe_id: str | None = None,
                   allow_pending: bool = False) -> str | None:
    """Validate every claim before removing any attributable interrupted stage."""
    path = root / ".deployments" / _PROBE_STAGE_DIR
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    if reconcile:
        admission.require_lock(root)
    with manifest._directory(root, (".deployments", _PROBE_STAGE_DIR), create=False) as staging, \
            manifest._directory(root, (".deployments", "component-adopt-probes"), create=False) as registry:
        stage_parent = _probe_parent_stat(os.fstat(staging))
        registry_parent = _probe_parent_stat(os.fstat(registry))
        if (stat.S_IMODE(stage_parent["mode"]) != 0o700
                or reconcile and stage_parent["uid"] != os.geteuid()):
            _fail("probe staging directory ownership differs")
        names = set(os.listdir(staging))
        matches = {name: _PROBE_STAGE_NAME.fullmatch(name) for name in names}
        if any(match is None for match in matches.values()):
            _fail("unknown probe staging object")
        claims = sorted(name for name, match in matches.items() if match.group(3) == "claim")
        if len({matches[name].group(1) for name in claims}) != len(claims):
            _fail("ambiguous probe staging claims")
        expected_names: set[str] = set(claims)
        actions = []
        for claim_name in claims:
            match = matches[claim_name]
            identifier, digest = match.group(1), match.group(2)
            stage_name = f"{identifier}.{digest}.stage"
            if stage_name in names:
                expected_names.add(stage_name)
            claim_raw, claim_stat = _probe_read_at(staging, claim_name)
            if claim_stat["uid"] != stage_parent["uid"] or stat.S_IMODE(claim_stat["mode"]) != 0o600:
                _fail("probe staging claim ownership differs")
            claim = _object(_decode(claim_raw), {"schema_version", "probe_id", "sha256", "before_sha256",
                              "before", "staged", "stage_parent", "registry_parent"}, "probe staging claim")
            if (manifest._encode(claim) != claim_raw or type(claim["schema_version"]) is not int
                    or claim["schema_version"] != 1 or claim["probe_id"] != identifier or claim["sha256"] != digest
                    or claim["stage_parent"] != stage_parent or claim["registry_parent"] != registry_parent):
                _fail("probe staging claim binding differs")
            _match(claim["before_sha256"], _HEX64, "probe staging preimage")
            before = _object(claim["before"], _PROBE_STAT_KEYS, "probe staging preimage stat")
            staged = _object(claim["staged"], _PROBE_STABLE_KEYS, "probe staging stat")
            if (any(type(value) is not int or value < 0 for value in (*before.values(), *staged.values()))
                    or not stat.S_ISREG(before["mode"]) or before["nlink"] != 1
                    or not stat.S_ISREG(staged["mode"]) or stat.S_IMODE(staged["mode"]) != 0o600
                    or staged["uid"] != stage_parent["uid"]):
                _fail("probe staging claim stat differs")
            target_name = identifier + ".json"
            target_raw, target_stat = _probe_read_at(registry, target_name)
            target = _decode(target_raw)
            if type(target) is not dict or target.get("probe_id") != identifier:
                _fail("probe staging target identity differs")
            prior = target_stat == before and hashlib.sha256(target_raw).hexdigest() == claim["before_sha256"]
            published = (all(target_stat[key] == staged[key] for key in _PROBE_STABLE_KEYS)
                         and target_stat["nlink"] == 1 and hashlib.sha256(target_raw).hexdigest() == digest)
            stage_stat = None
            if stage_name in names:
                stage_raw, stage_stat = _probe_read_at(staging, stage_name)
                value = _decode(stage_raw)
                if (not prior or any(stage_stat[key] != staged[key] for key in _PROBE_STABLE_KEYS)
                        or hashlib.sha256(stage_raw).hexdigest() != digest
                        or type(value) is not dict or value.get("probe_id") != identifier):
                    _fail("probe staging inode or preimage differs")
                mutable = {"status", "phase", "container_id", "installed_core_version", "output_sha256",
                           "failure", "result", "started"}
                if ({key: item for key, item in value.items() if key not in mutable}
                        != {key: item for key, item in target.items() if key not in mutable}):
                    _fail("probe staging immutable binding differs")
            elif not (prior or published):
                _fail("probe staging target is unknown")
            actions.append((claim_name, claim_raw, claim_stat, stage_name, stage_stat, target_name, target_raw, target_stat))
        if names != expected_names:
            _fail("unclaimed probe staging object")
        if not reconcile:
            if actions and not allow_pending:
                _fail("probe staging requires recorded recovery")
            if len(actions) > 1:
                _fail("multiple pending probe stages require inspection")
            return matches[actions[0][0]].group(1) if actions else None
        if len(actions) > 1 or actions and not actions[0][0].startswith(str(probe_id) + "."):
            _fail("another probe staging claim requires its own recovery")
        if set(os.listdir(staging)) != names:
            _fail("probe staging directory changed during inspection")
        # Validate the whole directory before deleting any of its known entries.
        for claim_name, claim_raw, claim_stat, stage_name, stage_stat, target_name, target_raw, target_stat in actions:
            if (_probe_read_at(staging, claim_name) != (claim_raw, claim_stat)
                    or _probe_read_at(registry, target_name) != (target_raw, target_stat)
                    or stage_stat is not None and _probe_read_at(staging, stage_name)[1] != stage_stat):
                _fail("probe staging changed before reconciliation")
        for claim_name, _, _, stage_name, stage_stat, _, _, _ in actions:
            if stage_stat is not None:
                os.unlink(stage_name, dir_fd=staging)
                os.fsync(staging)
            # A prior rename may have happened before either directory sync.
            os.fsync(registry)
            os.fsync(staging)
            os.unlink(claim_name, dir_fd=staging)
            os.fsync(staging)
        return probe_id if actions else None


def pending_adopt_probe_staging(root: Path) -> str | None:
    """Identify one structurally known stage without authorizing or changing it."""
    root = admission._physical_root(Path(root))
    return _probe_staging(root, reconcile=False, allow_pending=True)


def reconcile_adopt_probe_staging(root: Path, probe_id: str, *, source_tool_sha256: str,
                                  transaction_id: str | None = None) -> bool:
    """Reconcile only the already-authorized exact-A probe under FD9.

    The caller must verify the saved A and expected transaction/probe binding
    before calling; this helper does not authorize recovery from JSON alone.
    """
    root = admission._physical_root(Path(root))
    admission.require_lock(root)
    identifier = _match(probe_id, _DEPLOYMENT_ID, "probe staging ID")
    _match(source_tool_sha256, _HEX64, "probe staging source")
    value = _read_adopt_probe(root, identifier + ".json", _pre_reconcile=True)
    if value.get("probe_id") != identifier:
        _fail("probe staging registry identity differs")
    if value.get("kind") == "legacy-core-version":
        source = value.get("source")
        if transaction_id is not None or type(source) is not dict or source.get("source_tool_sha256") != source_tool_sha256:
            _fail("version probe staging source differs")
    else:
        _match(transaction_id, _DEPLOYMENT_ID, "probe staging transaction")
        if value.get("transaction_id") != transaction_id or value.get("source_tool_sha256") != source_tool_sha256:
            _fail("state probe staging transaction differs")
    return _probe_staging(root, reconcile=True, probe_id=identifier) is not None


def _read_adopt_probe(root: Path, name: str, *, _pre_reconcile: bool = False) -> dict[str, Any]:
    if not _pre_reconcile:
        _probe_staging(root, reconcile=False)
    value = _decode(_read_regular(root / ".deployments/component-adopt-probes" / name))
    if type(value) is dict and value.get("kind") in (
        "state-plan", "state-legacy-plan", "state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4", "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard",
    ):
        import component_state_journal as state_journal  # noqa: PLC0415
        if Path(state_journal.__file__).resolve() != CODE_ROOT / "scripts/component_state_journal.py":
            _fail("state probe parser belongs to a foreign source")
        return state_journal.read_state_probe(root, name)
    record = _object(value, {
        "schema_version", "kind", "probe_id", "status", "phase", "image_id", "image_revision",
        "container_name", "container_id", "installed_core_version", "output_sha256", "failure", "source", "daemon_id",
        "predecessor_sha256",
    }, "adoption probe")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        _fail("unsupported adoption probe")
    if record["kind"] != "legacy-core-version":
        _fail("unsupported adoption probe kind")
    import component_recovery as recovery  # noqa: PLC0415
    try:
        recovery._validate_source(record["source"])
    except recovery.RecoveryError as exc:
        raise JournalError("invalid adoption probe source") from exc
    identifier = _match(record["probe_id"], _DEPLOYMENT_ID, "adoption probe ID")
    if name != identifier + ".json" or record["container_name"] != "wuwaterm-adopt-probe-" + identifier:
        _fail("adoption probe name differs")
    _image_id(record["image_id"], "adoption probe image")
    _match(record["image_revision"], _HEX40, "adoption probe revision")
    _match(record["daemon_id"], _HEX64, "adoption probe daemon")
    if record["predecessor_sha256"] is not None:
        _match(record["predecessor_sha256"], _HEX64, "adoption probe predecessor")
    if record["status"] not in ("active", "completed", "failed") or record["phase"] not in (
        "prepared", "create", "start", "cleanup", "completed", "failed",
    ):
        _fail("invalid adoption probe status")
    if ((record["status"] == "completed") != (record["phase"] == "completed")
            or (record["status"] == "failed") != (record["phase"] == "failed")):
        _fail("inconsistent adoption probe status")
    if record["container_id"] is not None:
        _match(record["container_id"], _HEX64, "adoption probe container")
    if record["installed_core_version"] is not None:
        manifest._matches(record["installed_core_version"], manifest._VERSION, "adoption probe Core version")
    if record["output_sha256"] is not None:
        _match(record["output_sha256"], _HEX64, "adoption probe output")
    if record["phase"] == "start" and record["container_id"] is None:
        _fail("started adoption probe has no container ID")
    if record["status"] == "completed" and any(record[key] is None for key in (
        "container_id", "installed_core_version", "output_sha256",
    )):
        _fail("completed adoption probe has no observed result")
    if record["status"] == "failed" or record["status"] == "active" and record["phase"] == "cleanup" and record["failure"] is not None:
        if type(record["failure"]) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", record["failure"]):
            _fail("invalid adoption probe failure")
    elif record["failure"] is not None:
        _fail("unexpected adoption probe failure")
    return record


def _write_adopt_probe(root: Path, record: dict[str, Any], *, new: bool = False) -> None:
    root = admission._physical_root(Path(root))
    admission.require_lock(root)
    raw = manifest._encode(record)
    identifier = _match(record["probe_id"], _DEPLOYMENT_ID, "adoption probe ID")
    name = identifier + ".json"
    with manifest._directory(root, (".deployments", "component-adopt-probes"), create=True) as directory:
        _probe_staging(root, reconcile=False)
        if new:
            manifest._publish_unnamed(directory, name, raw, mode=0o600)
            return
        before_raw, before = _probe_read_at(directory, name)
        value = _decode(before_raw)
        if type(value) is not dict or value.get("probe_id") != identifier:
            _fail("adoption probe preimage differs")
        with manifest._directory(root, (".deployments", _PROBE_STAGE_DIR), create=True) as staging:
            stage_parent = _probe_parent_stat(os.fstat(staging))
            registry_parent = _probe_parent_stat(os.fstat(directory))
            if stat.S_IMODE(stage_parent["mode"]) != 0o700 or stage_parent["uid"] != os.geteuid():
                _fail("probe staging directory ownership differs")
            if os.listdir(staging):
                _fail("probe staging directory was not reconciled")
            digest = hashlib.sha256(raw).hexdigest()
            stage_name = f"{identifier}.{digest}.stage"
            claim_name = f"{identifier}.{digest}.claim"
            fd = os.open(".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=staging)
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        _fail("probe staging write did not advance")
                    remaining = remaining[written:]
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                info = _probe_file_stat(os.fstat(fd))
                if (info["uid"] != stage_parent["uid"] or info["nlink"] != 0 or info["size"] != len(raw)
                        or not stat.S_ISREG(info["mode"]) or stat.S_IMODE(info["mode"]) != 0o600):
                    _fail("anonymous probe staging inode differs")
                claim = {"schema_version": 1, "probe_id": identifier, "sha256": digest,
                         "before_sha256": hashlib.sha256(before_raw).hexdigest(), "before": before,
                         "staged": {key: info[key] for key in _PROBE_STABLE_KEYS},
                         "stage_parent": stage_parent, "registry_parent": registry_parent}
                manifest._publish_unnamed(staging, claim_name, manifest._encode(claim), mode=0o600)
                os.link(f"/proc/self/fd/{fd}", stage_name, dst_dir_fd=staging, follow_symlinks=True)
                os.fsync(staging)
                staged_raw, staged = _probe_read_at(staging, stage_name)
                if (staged_raw != raw or any(staged[key] != claim["staged"][key] for key in _PROBE_STABLE_KEYS)
                        or _probe_read_at(directory, name) != (before_raw, before)
                        or _probe_parent_stat(os.fstat(staging)) != stage_parent
                        or _probe_parent_stat(os.fstat(directory)) != registry_parent):
                    _fail("probe staging preimage changed")
                os.replace(stage_name, name, src_dir_fd=staging, dst_dir_fd=directory)
                os.fsync(directory)
                os.fsync(staging)
                published_raw, published = _probe_read_at(directory, name)
                if (published_raw != raw or any(published[key] != claim["staged"][key]
                                                for key in _PROBE_STABLE_KEYS)):
                    _fail("published probe record differs")
                os.unlink(claim_name, dir_fd=staging)
                os.fsync(staging)
            finally:
                os.close(fd)


def _adopt_probe_daemon() -> str:
    import component_observe as observe  # noqa: PLC0415
    value = observe._docker_json("/info").get("ID")
    if type(value) is not str or not value:
        _fail("Docker daemon identity is unavailable")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _owned_adopt_probe(record: dict[str, Any]) -> dict[str, Any] | None:
    """Find only the uniquely named, labelled and isolated recorded probe."""
    import component_observe as observe  # noqa: PLC0415
    if record.get("kind") != "legacy-core-version":
        _fail("standalone probe recovery accepts only a legacy version probe")
    if _adopt_probe_daemon() != record["daemon_id"]:
        _fail("adoption probe Docker daemon differs")
    connection = observe._UnixHTTPConnection("localhost", timeout=15)
    try:
        connection.request("GET", "/containers/" + record["container_name"] + "/json")
        response = connection.getresponse()
        raw = response.read(observe._MAX_RESPONSE + 1)
        if response.status == 404:
            if record["container_id"] is not None:
                connection.request("GET", "/containers/" + record["container_id"] + "/json")
                by_id = connection.getresponse()
                by_id.read(observe._MAX_RESPONSE + 1)
                if by_id.status != 404:
                    _fail("recorded adoption probe was renamed or cannot be inspected")
            return None
        if response.status != 200 or len(raw) > observe._MAX_RESPONSE:
            _fail("adoption probe cannot be inspected")
        probe = _decode(raw)
        image = observe._docker_json("/images/" + record["image_id"] + "/json")
        config, host = probe["Config"], probe["HostConfig"]
        image_config = image["Config"]
        command = ["-I", "-S", "-B", "-c", _LEGACY_VERSION_CODE]
        health = config.get("Healthcheck")
        if (image.get("Id") != record["image_id"]
                or (image_config.get("Labels") or {}).get("org.opencontainers.image.revision") != record["image_revision"]
                or probe["Image"] != record["image_id"]
                or probe.get("Name") != "/" + record["container_name"]
                or record["container_id"] is not None and probe["Id"] != record["container_id"]
                or (config.get("Labels") or {}).get("org.wuwaterm.component-adopt-probe") != record["probe_id"]
                or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                or probe.get("Mounts") not in (None, [])
                or health is not None and health.get("Test") != ["NONE"]
                or config.get("Entrypoint") != ["python"] or config.get("Cmd") != command
                or _env_mapping(config.get("Env") or []) != _env_mapping(image_config.get("Env") or [])):
            _fail("adoption probe differs from its recorded ownership and isolation")
        _match(probe["Id"], _HEX64, "adoption probe container")
        return probe
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise JournalError("adoption probe cannot be inspected safely") from exc
    finally:
        connection.close()


def _remove_owned_adopt_probe(root: Path, record: dict[str, Any]) -> None:
    """Remove a stopped probe only after matching its durable intent."""
    import component_observe as observe  # noqa: PLC0415
    admission.require_lock(root)
    probe = _owned_adopt_probe(record)
    if probe is not None:
        if (probe["State"]["Running"] or probe["State"]["Status"] not in ("created", "exited")
                or probe["State"].get("Dead") or probe["State"].get("Restarting")):
            _fail("adoption probe is still running")
        if record["container_id"] is None:
            record["container_id"] = probe["Id"]
            _write_adopt_probe(root, record)
        connection = observe._UnixHTTPConnection("localhost", timeout=15)
        try:
            connection.request("DELETE", "/containers/" + record["container_id"] + "?force=0&v=0")
            response = connection.getresponse()
            response.read(observe._MAX_RESPONSE + 1)
        finally:
            connection.close()
        if _owned_adopt_probe(record) is not None:
            _fail("adoption probe could not be removed")
    config_name = "component-adopt-probe-config-" + record["probe_id"]
    with manifest._directory(root, (".deployments",), create=False) as directory:
        try:
            info = os.stat(config_name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            _fail("adoption probe configuration path changed")
        # Only the empty, uniquely named directory created by this attempt is
        # removable. Unexpected contents leave the attempt unresolved.
        os.rmdir(config_name, dir_fd=directory)
        os.fsync(directory)


def _adopt_probe_argv(record: dict[str, Any], action: str, root_label: str | None = None) -> list[str]:
    """Construct only the two Docker operations owned by a version probe."""
    identifier = _match(record.get("probe_id"), _DEPLOYMENT_ID, "adoption probe ID")
    image_id = _image_id(record.get("image_id"), "adoption probe image")
    if (record.get("kind") != "legacy-core-version" or record.get("status") != "active"
            or record.get("container_name") != "wuwaterm-adopt-probe-" + identifier):
        _fail("adoption probe operation has an invalid binding")
    if action == "create" and record.get("phase") == "create" and record.get("container_id") is None:
        # The root label scopes the pre-mutator helper scan to this root; the
        # ownership check still matches only the probe label.
        scope = [] if root_label is None else ["--label", admission.ROOT_LABEL + "=" + _match(
            root_label, _HEX64, "deployment root label")]
        return ["create", "--name", record["container_name"], "--network", "none",
                "--read-only", "--no-healthcheck", "--label",
                "org.wuwaterm.component-adopt-probe=" + identifier, *scope,
                "--entrypoint", "python", image_id, "-I", "-S", "-B", "-c", _LEGACY_VERSION_CODE]
    if action == "start" and record.get("phase") == "start":
        return ["start", "--attach", _match(record.get("container_id"), _HEX64, "adoption probe container")]
    _fail("adoption probe operation does not match its durable phase")


def _run_adopt_probe(root: Path, record: dict[str, Any], lock: Any, action: str) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed version-probe operation under its verified lock and record."""
    import package_recovery_source as capsule  # noqa: PLC0415

    root = admission._physical_root(root)
    admission.require_lock(root)
    command = _adopt_probe_argv(record, action, admission.root_label_value(root))
    if _read_adopt_probe(root, record["probe_id"] + ".json") != record:
        _fail("adoption probe record changed before execution")
    owned = _owned_adopt_probe(record)
    if action == "create":
        if owned is not None:
            _fail("adoption probe name is already in use")
    elif (owned is None or owned["Id"] != record["container_id"]
          or owned["State"].get("Running") is not False or owned["State"].get("Status") != "created"):
        _fail("adoption probe is not the recorded stopped container")
    name = "component-adopt-probe-config-" + record["probe_id"]
    config = root / ".deployments" / name
    try:
        with manifest._directory(root, (".deployments", name), create=False) as directory:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail("adoption probe configuration directory differs")
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C", "DOCKER_CONFIG": str(config)}
            return subprocess.run(["/usr/bin/docker", "--host", "unix:///run/docker.sock", *command],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                  pass_fds=lock.pass_fds(), timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired, capsule.CapsuleError) as exc:
        raise JournalError("adoption probe operation could not complete") from exc


def _legacy_core_version(root: Path, image_id: str, revision: str, source: dict[str, Any], *,
                         compose_path: Path | None = None, predecessor_raw: bytes | None = None) -> str:
    """Inspect installed Core without starting either serving container."""
    import component_build as build  # noqa: PLC0415
    import component_observe as observe  # noqa: PLC0415
    lock = build._require_lock(root)
    admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                           code_root=CODE_ROOT, compose_path=compose_path,
                                           predecessor_raw=predecessor_raw)
    image = observe._docker_json("/images/" + image_id + "/json")
    if image.get("Id") != image_id or image.get("Config", {}).get("Volumes") not in (None, {}):
        _fail("legacy image has unsupported automatic volumes")
    identifier = manifest.new_deployment_id()
    record = {"schema_version": 1, "kind": "legacy-core-version", "probe_id": identifier,
              "status": "active", "phase": "prepared",
              "image_id": image_id, "image_revision": revision,
              "container_name": "wuwaterm-adopt-probe-" + identifier, "container_id": None,
              "installed_core_version": None, "output_sha256": None, "failure": None, "source": source,
              "daemon_id": _adopt_probe_daemon(),
              "predecessor_sha256": None if predecessor_raw is None else hashlib.sha256(predecessor_raw).hexdigest()}
    _write_adopt_probe(root, record, new=True)
    try:
        config = root / ".deployments" / ("component-adopt-probe-config-" + identifier)
        config.mkdir(mode=0o700)
        record["phase"] = "create"
        _write_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                               code_root=CODE_ROOT, compose_path=compose_path,
                                               probe_id=identifier, predecessor_raw=predecessor_raw)
        created = _run_adopt_probe(root, record, lock, "create")
        if created.returncode:
            _fail("legacy Core version probe could not be created")
        probe_id = _match(created.stdout.decode("ascii").strip(), _HEX64, "adoption probe container")
        record["container_id"] = probe_id
        _write_adopt_probe(root, record)
        probe = _owned_adopt_probe(record)
        if probe is None or probe["State"]["Running"]:
            _fail("new legacy version probe is unexpectedly running")
        record["phase"] = "start"
        _write_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                               code_root=CODE_ROOT, compose_path=compose_path,
                                               probe_id=identifier, predecessor_raw=predecessor_raw)
        result = _run_adopt_probe(root, record, lock, "start")
        version = result.stdout.decode("ascii").strip()
        if result.returncode or not manifest._VERSION.fullmatch(version):
            _fail("legacy image does not report an installed Core version")
        probe = _owned_adopt_probe(record)
        if probe is None:
            _fail("legacy Core version probe disappeared")
        state = probe["State"]
        if state["Running"] or state["ExitCode"] != 0:
            _fail("legacy Core version probe did not exit successfully")
        if build._inspect_image(image_id, lock, config) != (image_id, revision):
            _fail("legacy image identity changed during version inspection")
        output_sha = hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()
        record.update(phase="cleanup", installed_core_version=version, output_sha256=output_sha)
        _write_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                               code_root=CODE_ROOT, compose_path=compose_path,
                                               probe_id=identifier, predecessor_raw=predecessor_raw)
        _remove_owned_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                               code_root=CODE_ROOT, compose_path=compose_path,
                                               probe_id=identifier, predecessor_raw=predecessor_raw)
        record.update(status="completed", phase="completed")
        _write_adopt_probe(root, record)
        return version
    except Exception as exc:
        record.update(status="active", phase="cleanup", failure=type(exc).__name__)
        try:
            admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                                   code_root=CODE_ROOT, compose_path=compose_path,
                                                   probe_id=identifier, predecessor_raw=predecessor_raw)
            _write_adopt_probe(root, record)
            _remove_owned_adopt_probe(root, record)
            admission.admit_adoption_probe_context(root, source=source, image_id=image_id, revision=revision,
                                                   code_root=CODE_ROOT, compose_path=compose_path,
                                                   probe_id=identifier, predecessor_raw=predecessor_raw)
            record.update(status="failed", phase="failed")
            _write_adopt_probe(root, record)
        except Exception as persist_error:
            raise JournalError("legacy version probe failed and its cleanup is unresolved") from persist_error
        if isinstance(exc, JournalError):
            raise
        raise JournalError("legacy Core version probe failed") from exc


def recover_adopt_probe(root: Path, probe_id: str, *, compose_path: Path | None = None,
                        capsule_source_tool: str | None = None) -> None:
    """Cancel one interrupted, explicitly recorded isolated adoption probe."""
    import component_recovery as recovery  # noqa: PLC0415
    import package_recovery_source as capsule  # noqa: PLC0415
    root = admission._physical_root(Path(root))
    try:
        admission.require_lock(root)
        identifier = _match(probe_id, _DEPLOYMENT_ID, "adoption probe ID")
        selected = _read_adopt_probe(root, identifier + ".json", _pre_reconcile=True)
        if selected["kind"] != "legacy-core-version":
            _fail("recorded recovery is not a version probe")
        source = selected["source"]
        if "compose" in selected["source"]:
            if compose_path is None:
                _fail("adoption probe recovery requires Compose")
            compose_path = recovery._requested_compose(root, selected["source"], compose_path)
        elif capsule_source_tool is not None:
            _fail("adoption probe has no saved Compose identity")
        if capsule_source_tool is not None:
            if (source["source_tool_sha256"] != capsule_source_tool or compose_path is None
                    or recovery._verify_bound_compose(root, source) != compose_path):
                _fail("adoption probe recovery identity differs")
            admission._verify_recovery_extraction(root, source, CODE_ROOT)
        else:
            if root != CODE_ROOT:
                _fail("version probe recovery source differs from the checkout")
            commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
            tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
            recovery._load_packager(root / "scripts/package_recovery_source.py")
            recovery._verify_clean_source(root, commit, tree)
            if (source["commit"] != commit or source["tree"] != tree
                    or tool_bytes_identity(root) != source["tool_bytes_sha256"]):
                _fail("adoption probe source differs from the clean checkout")
            recovery._verify_bootstrap(root, source)
            recovery._verify_capsule(root, source)
        reconcile_adopt_probe_staging(root, identifier, source_tool_sha256=source["source_tool_sha256"])
        record, predecessor_raw = admission.admit_adoption_probe_recovery(
            root, probe_id=probe_id, code_root=CODE_ROOT, compose_path=compose_path,
            extracted_source=capsule_source_tool is not None,
        )
        if record != selected:
            _fail("version probe changed during staging recovery")
        if record["status"] in ("failed", "completed"):
            if (_owned_adopt_probe(record) is not None
                    or admission._present(root / ".deployments" / ("component-adopt-probe-config-" + probe_id))):
                _fail("terminal adoption probe retains an owned object")
            return
        record.update(phase="cleanup", failure="Interrupted")
        _write_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=record["image_id"],
                                               revision=record["image_revision"], code_root=CODE_ROOT,
                                               compose_path=compose_path, probe_id=probe_id,
                                               predecessor_raw=predecessor_raw,
                                               extracted_source=capsule_source_tool is not None)
        _remove_owned_adopt_probe(root, record)
        admission.admit_adoption_probe_context(root, source=source, image_id=record["image_id"],
                                               revision=record["image_revision"], code_root=CODE_ROOT,
                                               compose_path=compose_path, probe_id=probe_id,
                                               predecessor_raw=predecessor_raw,
                                               extracted_source=capsule_source_tool is not None)
        record.update(status="failed", phase="failed")
        _write_adopt_probe(root, record)
    except (admission.AdmissionError, recovery.RecoveryError, capsule.CapsuleError,
            manifest.ManifestError, OSError, UnicodeError) as exc:
        raise JournalError("adoption probe recovery refused or remains unresolved") from exc
