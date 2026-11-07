#!/usr/bin/env python3
"""Inspect component deployments and adopt an existing topology as metadata.

Adoption observes but does not start, stop or replace installed services.
Component update and recovery execution remain separate unfinished work.
A valid journal or image identity is not a service-health check.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
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


JOURNAL_NAME = ".component-runtime-transaction.json"
# Schema 5 is the protocol-2 (completed-artifact, no copy) runtime update.
# Schemas 2/3/4 keep their exact historical meaning; adoption still writes 4.
JOURNAL_SCHEMA_VERSION = 5
RUNTIME4_SCHEMA_VERSION = 4
PRODUCER_EXECUTION_READY = False
# This source's saved-source recovery protocol. Protocol 2 means completed-
# artifact inputs and no producer; protocol-1 capsules keep being interpreted
# by their own saved source and its own (closed) readiness flags.
CAPSULE_RECOVERY_PROTOCOL = 2
CAPSULE_FULL_RECOVERY_PROTOCOL = 2
TOOL_PATHS = (
    "deploy/product-update.sh",
    "scripts/deployment_admission.py",
    "scripts/deployment_manifest.py",
    "scripts/component_artifact.py",
    "scripts/component_build.py",
    "scripts/component_build_isolation.py",
    "scripts/component_full.py",
    "scripts/component_full_db.py",
    "scripts/component_observe.py",
    "scripts/component_prepare.py",
    "scripts/component_recovery.py",
    "scripts/component_runtime.py",
    "scripts/component_state.py",
    "scripts/component_state_copy_v4.py",
    "scripts/component_state_endpoint.py",
    "scripts/component_transaction.py",
    "scripts/component_wheel_build.py",
    "scripts/install_probe.py",
    "scripts/package_recovery_source.py",
    "scripts/product_manifest.py",
)
BOOTSTRAP_FILES = ("component_recovery.py", "package_recovery_source.py")
PHASES = frozenset({
    "prepared", "retain-rollback", "selected-stop", "state-validate-migrate",
    "selected-activate", "actual-health-bindings", "retire-old",
    "immutable-manifest", "deploy-commit", "deploy-current", "committed",
    "rollback-stop", "rollback-restore", "rollback-pointers", "rolled-back",
    "unresolved",
})
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DEPLOYMENT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DOCKER_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z\Z")
_LIMIT = 16 * 1024 * 1024
_LEGACY_VERSION_CODE = (
    "import importlib.metadata,sys,sysconfig; from pathlib import Path; "
    "p=str(Path(sys.executable).parent.parent); "
    "sys.path.append(sysconfig.get_path('purelib',vars={'base':p,'platbase':p})); "
    "print(importlib.metadata.version('wuwaterm'))"
)


class JournalError(ValueError):
    """An unsupported, malformed, or inconsistent transaction journal."""


def _fail(message: str) -> None:
    raise JournalError(message)


def _capsule_protocol(*, full: bool) -> object:
    return globals().get("CAPSULE_FULL_RECOVERY_PROTOCOL" if full else "CAPSULE_RECOVERY_PROTOCOL")


def capsule_execution_ready(*, full: bool) -> bool:
    """This source's own readiness for the protocol it declares."""
    import component_recovery as recovery  # noqa: PLC0415
    return recovery.protocol_ready(_capsule_protocol(full=full), full=full)


def _object(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail(f"{where}: unexpected or missing fields")
    return value


def _match(value: Any, pattern: re.Pattern[str], where: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(f"{where}: invalid value")
    return value


def _text(value: Any, where: str, *, empty: bool = False) -> str:
    if (type(value) is not str or (not value and not empty) or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail(f"{where}: invalid text")
    return value


def _absolute(value: Any, where: str) -> str:
    value = _text(value, where)
    if value == "/":
        return value
    if (not value.startswith("/") or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/")[1:])):
        _fail(f"{where}: expected normalized absolute path")
    return value


def _boolean(value: Any, where: str) -> bool:
    if type(value) is not bool:
        _fail(f"{where}: expected boolean")
    return value


def _integer(value: Any, where: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        _fail(f"{where}: invalid integer")
    return value


def _image_id(value: Any, where: str) -> str:
    value = _text(value, where)
    if not value.startswith("sha256:"):
        _fail(f"{where}: invalid image ID")
    _match(value[7:], _HEX64, where)
    return value


def source_tool_identity(commit: str, tree: str, tool_bytes_sha256: str) -> str:
    """Bind the exact source tree and separately recorded tool fingerprint."""
    _match(commit, _HEX40, "source commit")
    _match(tree, _HEX40, "source tree")
    _match(tool_bytes_sha256, _HEX64, "tool bytes hash")
    return hashlib.sha256(
        b"wuwaterm-component-source-v1\0" + bytes.fromhex(commit)
        + bytes.fromhex(tree) + bytes.fromhex(tool_bytes_sha256)
    ).hexdigest()


def bootstrap_identity(files_sha256: dict[str, str]) -> str:
    """Bind each independently preserved bootstrap filename and byte hash."""
    _object(files_sha256, set(BOOTSTRAP_FILES), "bootstrap.files_sha256")
    digest = hashlib.sha256(b"wuwaterm-component-bootstrap-v1\0")
    for name in sorted(BOOTSTRAP_FILES):
        value = _match(files_sha256[name], _HEX64, f"bootstrap.{name}")
        encoded = name.encode("ascii")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def tool_bytes_identity(root: Path) -> str:
    """Fingerprint named regular tool files without following symlinks.

    This function does not by itself prove the files are tracked, clean, or
    correspond to a commit. The deployment coordinator must establish that.
    """
    digest = hashlib.sha256(b"wuwaterm-component-tool-bytes-v1\0")
    for relative in TOOL_PATHS:
        raw = _read_regular(root / relative)
        encoded = relative.encode("ascii")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _stat(value: Any, where: str) -> dict[str, int]:
    item = _object(value, {"device", "inode", "size", "mtime_ns", "ctime_ns"}, where)
    for key in item:
        _integer(item[key], f"{where}.{key}", minimum=1 if key == "inode" else 0)
    return item


def _file_preimage(value: Any, where: str) -> dict[str, Any]:
    item = _object(value, {"present", "sha256", "stat"}, where)
    if _boolean(item["present"], f"{where}.present"):
        _match(item["sha256"], _HEX64, f"{where}.sha256")
        _stat(item["stat"], f"{where}.stat")
    elif item["sha256"] is not None or item["stat"] is not None:
        _fail(f"{where}: absent file has recorded contents")
    return item


def _container(value: Any, where: str, *, observed: bool) -> dict[str, Any]:
    item = _object(value, {
        "id", "image_id", "running", "started_at", "restart_count", "project",
        "service", "name", "transaction_label",
    }, where)
    if item["id"] is not None:
        _match(item["id"], _HEX64, f"{where}.id")
    elif observed:
        _fail(f"{where}: observed container ID is missing")
    _image_id(item["image_id"], f"{where}.image_id")
    _boolean(item["running"], f"{where}.running")
    if item["started_at"] is not None:
        stamp = _match(item["started_at"], _DOCKER_TIME, f"{where}.started_at")
        try:
            datetime.fromisoformat(stamp[:-1] + "+00:00")
        except ValueError:
            _fail(f"{where}: invalid start time")
    elif observed:
        _fail(f"{where}: observed start time is missing")
    if item["restart_count"] is not None:
        _integer(item["restart_count"], f"{where}.restart_count")
    elif observed:
        _fail(f"{where}: observed restart count is missing")
    _match(item["project"], _NAME, f"{where}.project")
    _match(item["service"], _NAME, f"{where}.service")
    if item["name"] is not None:
        _match(item["name"], _NAME, f"{where}.name")
    elif observed:
        _fail(f"{where}: observed container name is missing")
    if item["transaction_label"] is not None:
        _match(item["transaction_label"], _DEPLOYMENT_ID, f"{where}.transaction_label")
    return item


def _pointer(value: Any, name: str, where: str) -> dict[str, Any]:
    pointer = _object(value, {"present", "bytes"}, where)
    if not _boolean(pointer["present"], f"{where}.present"):
        if pointer["bytes"] is not None:
            _fail(f"{where}: absent pointer has bytes")
        return pointer
    data = pointer["bytes"]
    if type(data) is not str or not data.endswith("\n"):
        _fail(f"{where}: invalid pointer bytes")
    pattern = _HEX40 if name == ".deploy_commit" else _DEPLOYMENT_ID
    _match(data[:-1], pattern, f"{where}.bytes")
    return pointer


def _migrations(value: Any, *, permitted: bool) -> None:
    if type(value) is not list or len(value) > 2:
        _fail("migrations: invalid list")
    if not permitted and value:
        _fail("migrations: Telegram is not selected")
    seen: set[str] = set()
    for item in value:
        _object(item, {
            "kind", "action", "effective_path", "source_path", "target_path",
            "source_before", "target_before", "created",
        }, "migration")
        kind = item["kind"]
        if kind not in ("chat-settings", "channel-replies") or kind in seen:
            _fail("migration: invalid or duplicate kind")
        seen.add(kind)
        if item["action"] not in ("none", "validate-existing", "copy-if-absent"):
            _fail("migration: invalid action")
        _absolute(item["effective_path"], "migration.effective_path")
        _absolute(item["target_path"], "migration.target_path")
        if item["source_path"] is not None:
            _absolute(item["source_path"], "migration.source_path")
            _file_preimage(item["source_before"], "migration.source_before")
        elif item["source_before"] is not None:
            _fail("migration: source preimage without source path")
        before = _file_preimage(item["target_before"], "migration.target_before")
        if item["action"] == "copy-if-absent":
            if before["present"] or item["source_before"] is None or not item["source_before"]["present"]:
                _fail("migration: invalid copy preimage")
        elif item["action"] == "validate-existing" and not before["present"]:
            _fail("migration: existing target is absent")
        if item["created"] is not None:
            if item["action"] != "copy-if-absent":
                _fail("migration: unexpected created file")
            created = _object(item["created"], {"sha256", "stat"}, "migration.created")
            _match(created["sha256"], _HEX64, "migration.created.sha256")
            _stat(created["stat"], "migration.created.stat")


def _manifest(value: Any, where: str) -> dict[str, Any]:
    try:
        return manifest.validate_manifest(value)
    except (manifest.ManifestError, ValueError, TypeError) as exc:
        raise JournalError(f"{where}: invalid product manifest") from exc


def predecessor_reference(kind: str, transaction_id: str, raw: bytes) -> dict[str, str]:
    """Identify immutable terminal bytes and their kind-specific archive."""
    if kind not in ("runtime", "full") or type(raw) is not bytes:
        _fail("predecessor: unsupported kind or bytes")
    identifier = _match(transaction_id, _DEPLOYMENT_ID, "predecessor transaction ID")
    return {"kind": kind, "transaction_id": identifier,
            "journal_path": f".deployments/.component-{kind}-transaction.json",
            "journal_sha256": hashlib.sha256(raw).hexdigest(),
            "archive_path": f".deployments/component-{kind}-history/{identifier}.json"}


def validate_predecessor(value: Any, *, successor_id: str, required: bool) -> dict[str, str] | None:
    if value is None:
        if required:
            _fail("predecessor: terminal binding is required")
        return None
    record = _object(value, {"kind", "transaction_id", "journal_path", "journal_sha256", "archive_path"},
                     "predecessor")
    kind = record["kind"]
    if kind not in ("runtime", "full"):
        _fail("predecessor: unsupported kind")
    identifier = _match(record["transaction_id"], _DEPLOYMENT_ID, "predecessor transaction ID")
    if identifier == successor_id:
        _fail("predecessor: transaction ID was reused")
    _match(record["journal_sha256"], _HEX64, "predecessor journal hash")
    if (record["journal_path"] != f".deployments/.component-{kind}-transaction.json"
            or record["archive_path"] != f".deployments/component-{kind}-history/{identifier}.json"):
        _fail("predecessor: journal or archive path differs")
    return record


def validate_journal(value: Any) -> dict[str, Any]:
    """Reject unknown schemas and unsafe or inconsistent recovery records.

    This is a pure structural parser. Recovery must separately verify capsule
    bytes, current Docker observations, DB identity, state files and pointers.
    """
    if type(value) is not dict:
        _fail("journal: expected object")
    version = value.get("schema_version")
    keys = {
        "schema_version", "kind", "transaction_id", "action", "status", "phase",
        "source", "selection", "before", "target", "database", "api_identity",
        "migrations", "pointers", "manifest",
    }
    if type(version) is int and version in (RUNTIME4_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION):
        keys.add("predecessor")
    journal = _object(value, keys, "journal")
    if type(journal["schema_version"]) is not int or journal["schema_version"] not in (
            2, 3, RUNTIME4_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION):
        _fail("journal: unsupported schema version")
    if journal["kind"] != "component-runtime":
        _fail("journal: unsupported kind")
    transaction_id = _match(journal["transaction_id"], _DEPLOYMENT_ID, "journal.transaction_id")
    action = journal["action"]
    if action not in ("adopt", "update"):
        _fail("journal: unsupported action")
    if journal["schema_version"] >= RUNTIME4_SCHEMA_VERSION:
        validate_predecessor(journal["predecessor"], successor_id=transaction_id, required=action == "update")
    if journal["schema_version"] == JOURNAL_SCHEMA_VERSION and action != "update":
        _fail("journal: schema-5 records only completed-artifact updates")
    status, phase = journal["status"], journal["phase"]
    if (status not in ("active", "unresolved", "rolled_back", "committed")
            or type(phase) is not str or phase not in PHASES):
        _fail("journal: unsupported status or phase")
    if (status == "committed") != (phase == "committed") or (status == "rolled_back") != (phase == "rolled-back"):
        _fail("journal: final phase and status disagree")

    source_fields = {
        "commit", "tree", "source_tool_sha256", "tool_bytes_sha256", "capsule", "bootstrap",
    }
    if journal["schema_version"] >= 3:
        source_fields.add("compose")
    source = _object(journal["source"], source_fields, "journal.source")
    if journal["schema_version"] >= 3:
        compose = _object(source["compose"], {"path", "sha256"}, "journal.source.compose")
        digest = _match(compose["sha256"], _HEX64, "journal.source.compose.sha256")
        if compose["path"] != f".deployments/component-compose-{digest}":
            _fail("journal.source.compose: invalid path")
    _match(source["commit"], _HEX40, "journal.source.commit")
    _match(source["tree"], _HEX40, "journal.source.tree")
    _match(source["tool_bytes_sha256"], _HEX64, "journal.source.tool_bytes_sha256")
    source_id = source_tool_identity(source["commit"], source["tree"], source["tool_bytes_sha256"])
    if source["source_tool_sha256"] != source_id:
        _fail("journal.source: source/tool identity mismatch")
    capsule = _object(source["capsule"], {
        "path", "manifest_sha256", "archive_sha256", "bundle_sha256",
    }, "journal.source.capsule")
    if capsule["path"] != f".deployments/component-source-{source_id}":
        _fail("journal.source.capsule: invalid path")
    for key in ("manifest_sha256", "archive_sha256", "bundle_sha256"):
        _match(capsule[key], _HEX64, f"journal.source.capsule.{key}")
    bootstrap = _object(source["bootstrap"], {"path", "files_sha256", "sha256"}, "journal.source.bootstrap")
    if bootstrap["path"] != f".deployments/component-bootstrap-{source_id}":
        _fail("journal.source.bootstrap: invalid path")
    if bootstrap["sha256"] != bootstrap_identity(bootstrap["files_sha256"]):
        _fail("journal.source.bootstrap: invalid identity")

    selection = journal["selection"]
    if selection not in ([], ["api"], ["telegram"], ["api", "telegram"]):
        _fail("journal.selection: unsupported selection")
    if (action == "adopt") != (selection == []):
        _fail("journal.selection: action and selection disagree")
    before = _object(journal["before"], {"manifest", "topology", "containers"}, "journal.before")
    target = _object(journal["target"], {"manifest", "containers"}, "journal.target")
    if journal["schema_version"] >= RUNTIME4_SCHEMA_VERSION and action == "update" and before["manifest"] is None:
        _fail("journal.before: runtime update requires an adopted manifest")
    target_manifest = _manifest(target["manifest"], "journal.target.manifest")
    # Historical schemas embed only schema-2 manifests; schema 5 writes 3.
    manifest_versions = ((manifest.PREVIOUS_MANIFEST_SCHEMA_VERSION, manifest.MANIFEST_SCHEMA_VERSION)
                         if journal["schema_version"] == JOURNAL_SCHEMA_VERSION
                         else (manifest.PREVIOUS_MANIFEST_SCHEMA_VERSION,))
    if before["manifest"] is not None and (type(before["manifest"]) is not dict
                                           or before["manifest"].get("schema_version") not in manifest_versions):
        _fail("journal.before.manifest: schema differs from the journal schema")
    if target_manifest["schema_version"] != manifest_versions[-1]:
        _fail("journal.target.manifest: schema differs from the journal schema")
    if target_manifest["deployment_id"] != transaction_id:
        _fail("journal.target: deployment ID mismatch")
    ops = target_manifest["operations"]
    if (ops["source_commit"] != source["commit"] or ops["source_tree"] != source["tree"]
            or ops["source_tool_sha256"] != source_id):
        _fail("journal.target: operations source mismatch")
    before_topology = _object(before["topology"], {"api", "telegram"}, "journal.before.topology")
    if before["manifest"] is not None:
        old = _manifest(before["manifest"], "journal.before.manifest")
        if old["components"] != before_topology:
            _fail("journal.before: manifest and topology differ")
        if old["api_identity"] != journal["api_identity"]:
            _fail("journal.before: API identity changed")
    else:
        # Legacy adoption has no v2 manifest, but the exact preimage topology
        # must be just as strict and complete as an installed v2 topology.
        old = copy.deepcopy(target_manifest)
        old["components"] = before_topology
        old["api_identity"] = journal["api_identity"]
        _manifest(old, "journal.before.topology")
    if before_topology["api"] is None and journal["api_identity"] is not None:
        _fail("journal.api_identity: API is absent")
    if target_manifest["api_identity"] != journal["api_identity"]:
        _fail("journal.api_identity: target identity changed")
    database = _object(journal["database"], {"binding", "stat"}, "journal.database")
    if database["binding"] != target_manifest["database"]:
        _fail("journal.database: target binding changed")
    _stat(database["stat"], "journal.database.stat")
    if before["manifest"] is not None and old["database"] != database["binding"]:
        _fail("journal.database: prior binding differs")

    before_containers = _object(before["containers"], {"api", "telegram"}, "journal.before.containers")
    target_containers = _object(target["containers"], {"api", "telegram"}, "journal.target.containers")
    for component_name in ("api", "telegram"):
        previous, proposed = before_topology[component_name], target_manifest["components"][component_name]
        previous_container, target_container = before_containers[component_name], target_containers[component_name]
        if previous is None:
            if previous_container is not None or proposed is not None or target_container is not None:
                _fail("journal: component installation is outside runtime update")
            continue
        if proposed is None or previous_container is None or target_container is None:
            _fail("journal: installed component or container is missing")
        observed = _container(previous_container, f"journal.before.{component_name}", observed=True)
        expected = _container(target_container, f"journal.target.{component_name}", observed=False)
        for record, component in ((observed, previous), (expected, proposed)):
            if (record["image_id"] != component["image"]["id"]
                    or record["project"] != component["recipe"]["project"]
                    or record["service"] != component["recipe"]["service"]):
                _fail("journal: container and component binding differ")
        if expected["running"] != observed["running"]:
            _fail("journal: selected running state changed")
        if component_name not in selection:
            if previous != proposed or observed != expected:
                _fail("journal: unselected component changed")
        elif expected["transaction_label"] != transaction_id:
            _fail("journal: selected target ownership label differs")
    if action == "adopt" and before_topology != target_manifest["components"]:
        _fail("journal: adoption changed topology")
    _migrations(journal["migrations"], permitted="telegram" in selection)
    if journal["schema_version"] == JOURNAL_SCHEMA_VERSION:
        for name in selection:
            if target_manifest["components"][name]["provenance"]["kind"] != "completed-artifact":
                _fail("journal: schema-5 selected component is not a completed artifact")
        # The transaction never copies state: a native-pending copy is made
        # by the product itself, so no migration may claim a created file. The
        # plan's own copy-if-absent action stays recorded as native-pending.
        if any(item["created"] is not None for item in journal["migrations"]):
            _fail("journal: schema-5 transactions do not copy state")

    pointers = _object(journal["pointers"], {"old", "target"}, "journal.pointers")
    for which in ("old", "target"):
        pair = _object(pointers[which], {".deploy_commit", ".deploy_current"}, f"journal.pointers.{which}")
        for name, item in pair.items():
            _pointer(item, name, f"journal.pointers.{which}.{name}")
    if pointers["target"][".deploy_commit"] != {"present": True, "bytes": source["commit"] + "\n"}:
        _fail("journal.pointers: target commit mismatch")
    if pointers["target"][".deploy_current"] != {"present": True, "bytes": transaction_id + "\n"}:
        _fail("journal.pointers: target deployment mismatch")
    if before["manifest"] is None:
        if pointers["old"][".deploy_current"]["present"]:
            _fail("journal.pointers: unbound prior deployment")
    elif (pointers["old"][".deploy_current"] != {
        "present": True, "bytes": old["deployment_id"] + "\n",
    } or pointers["old"][".deploy_commit"] != {
        "present": True, "bytes": old["operations"]["source_commit"] + "\n",
    }):
        _fail("journal.pointers: prior manifest mismatch")
    manifest_record = _object(journal["manifest"], {"deployment_id", "path", "sha256"}, "journal.manifest")
    expected_raw = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    if (manifest_record["deployment_id"] != transaction_id
            or manifest_record["path"] != f".deployments/products/{transaction_id}.json"
            or manifest_record["sha256"] != hashlib.sha256(expected_raw).hexdigest()):
        _fail("journal.manifest: immutable manifest binding differs")
    return journal


def _decode(raw: bytes) -> Any:
    def unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("journal: duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        _fail("journal: non-finite JSON value")

    try:
        return json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise JournalError("journal: invalid JSON") from exc


def _read_regular(path: Path) -> bytes:
    """Open every path component without symlink traversal or FIFO blocking."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directories: list[int] = []
    fd = None
    try:
        directory = os.open("/", flags | os.O_DIRECTORY)
        directories.append(directory)
        for part in absolute.parts[1:-1]:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            directories.append(directory)
        fd = os.open(absolute.name, flags | os.O_NONBLOCK, dir_fd=directory)
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > _LIMIT:
            _fail("journal: invalid regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(65536, _LIMIT + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > _LIMIT:
                _fail("journal: file too large")
        final = os.fstat(fd)
        named = os.stat(absolute.name, dir_fd=directory, follow_symlinks=False)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if identity(initial) != identity(final) or identity(final) != identity(named):
            _fail("journal: file changed while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise JournalError("journal: cannot safely read file") from exc
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def load_journal(path: Path) -> dict[str, Any]:
    return validate_journal(_decode(_read_regular(path)))


def _read_pointer(root: Path, name: str, pattern: re.Pattern[str]) -> str | None:
    path = root / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        _fail("deployment pointer is not a regular file")
    try:
        data = _read_regular(path).decode("ascii")
    except UnicodeError as exc:
        raise JournalError("deployment pointer is not ASCII") from exc
    if not data.endswith("\n"):
        _fail("deployment pointer has invalid bytes")
    return _match(data[:-1], pattern, "deployment pointer")


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
            import component_full as full  # noqa: PLC0415
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


def _project_containers(projects: list[str]) -> dict[str, tuple[dict[str, Any], str, str]]:
    """Discover the installed, non-orphaned product containers in each project."""
    import component_observe as observe  # noqa: PLC0415
    if not projects or len(set(projects)) != len(projects):
        _fail("one or more distinct Compose projects are required")
    for project in projects:
        manifest._name(project, "Compose project")
    connection = observe._UnixHTTPConnection("localhost", timeout=15)
    try:
        connection.request("GET", "/containers/json?all=1")
        response = connection.getresponse()
        raw = response.read(observe._MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > observe._MAX_RESPONSE:
            _fail("Docker container inventory is unavailable")
        entries = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise JournalError("Docker container inventory is unavailable") from exc
    finally:
        connection.close()
    if type(entries) is not list:
        _fail("Docker container inventory is invalid")
    installed: dict[str, tuple[dict[str, Any], str, str]] = {}
    services = {"wuwaterm-api": "api", "wuwaterm": "telegram", "wuwaterm-telegram": "telegram"}
    for entry in entries:
        if type(entry) is not dict or entry.get("Labels") is not None and type(entry.get("Labels")) is not dict:
            _fail("Docker container inventory is invalid")
        labels = entry.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        if project not in projects:
            continue
        service = labels.get("com.docker.compose.service")
        if service == "wuwaterm-builder":
            continue
        component = services.get(service)
        if component is None or component in installed or labels.get("com.docker.compose.oneoff") == "True":
            _fail("Compose project contains an unknown or duplicate service")
        identifier = _match(entry.get("Id"), _HEX64, "container ID")
        installed[component] = (observe._inspect(identifier), project, service)
    if not installed:
        _fail("no installed product containers were discovered")
    return installed


def _compose_input(path: Path) -> tuple[Any, ...]:
    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    before = path.lstat()
    content = _read_regular(path)
    after = path.lstat()
    if identity(before) != identity(after):
        _fail("Compose input changed while being read")
    return (*identity(after), hashlib.sha256(content).hexdigest())


def _compose_config(executable: Path, raw: dict[str, Any], project: str,
                    service: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[Any, ...]]]:
    """Resolve current Compose config without disclosing its environment values."""
    labels = raw["Config"]["Labels"]
    try:
        working = _absolute(labels["com.docker.compose.project.working_dir"], "Compose working directory")
        files = labels["com.docker.compose.project.config_files"].split(",")
        files = [_absolute(name, "Compose config file") for name in files]
        if not files or len(set(files)) != len(files):
            _fail("Compose config file references are invalid")
        executable = Path(executable)
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            _fail("Compose executable is unavailable")
        command = [str(executable), "--project-name", project, "--project-directory", working]
        for filename in files:
            command += ["--file", filename]
        interpolation = labels.get("com.docker.compose.project.environment_file")
        if interpolation:
            for filename in interpolation.split(","):
                command += ["--env-file", _absolute(filename, "Compose interpolation file")]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

        def run(*arguments: str) -> bytes:
            result = subprocess.run([*command, "config", *arguments], cwd=working, env=env,
                                    pass_fds=(9,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, check=False)
            if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                _fail("Compose config cannot be resolved")
            return result.stdout

        resolved = _decode(run("--format", "json"))["services"][service]
        unresolved = _decode(run("--no-env-resolution", "--format", "json"))["services"][service]
        hashes = run("--hash", service).decode("ascii").strip().split()
        expected_hash = labels.get("com.docker.compose.config-hash")
        if len(hashes) != 2 or hashes[0] != service or hashes[1] != expected_hash:
            _fail("current Compose service differs from the installed container")
        if type(resolved) is not dict or type(unresolved) is not dict:
            _fail("Compose service config is invalid")
        references = set(files)
        default_env = Path(working) / ".env"
        if default_env.exists() or default_env.is_symlink():
            references.add(str(default_env))
        if interpolation:
            references.update(interpolation.split(","))
        env_files = unresolved.get("env_file") or []
        if type(env_files) is not list:
            _fail("Compose env-file references are invalid")
        for item in env_files:
            if (type(item) is not dict or set(item) - {"path", "required"}
                    or item.get("required", True) is not True):
                _fail("optional or unsupported Compose env files are not supported")
            references.add(_absolute(item.get("path"), "Compose env file"))
        fingerprints = {path: _compose_input(Path(path)) for path in references}
        return resolved, unresolved, fingerprints
    except JournalError:
        raise
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise JournalError("Compose service config is unavailable") from exc


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


def _recipe_from_compose(raw: dict[str, Any], image: dict[str, Any], observed: dict[str, Any],
                         resolved: dict[str, Any], unresolved: dict[str, Any]) -> dict[str, Any]:
    effective = _env_mapping(image["Config"].get("Env") or [])
    resolved_env = resolved.get("environment") or {}
    explicit_env = unresolved.get("environment") or {}
    if type(resolved_env) is not dict or type(explicit_env) is not dict:
        _fail("Compose environment is invalid")
    for name, value in resolved_env.items():
        if type(name) is not str or not manifest._ENV_NAME.fullmatch(name) or type(value) is not str:
            _fail("Compose environment is incomplete")
        effective[name] = value
    if effective != _env_mapping(raw["Config"].get("Env")):
        _fail("current Compose environment differs from the installed container")
    overrides = []
    for name in sorted(explicit_env):
        value = resolved_env.get(name)
        if type(value) is not str:
            _fail("Compose environment override cannot be reproduced")
        if value == "":
            overrides.append({"name": name, "mode": "empty"})
        elif name in manifest._LITERAL_ENV:
            overrides.append({"name": name, "mode": "literal", "value": value})
        else:
            _fail("nonempty secret or unsupported Compose environment override")
    env_files = [item["path"] for item in (unresolved.get("env_file") or [])]
    snapshot = {**observed["recipe"], "schema_version": 1, "image_id": observed["container"]["image_id"],
                "env_files": env_files, "environment_overrides": overrides}
    manifest.validate_recipe_snapshot(snapshot)
    return snapshot


def _snapshot_pointer(root: Path, name: str, pattern: re.Pattern[str]) -> dict[str, Any]:
    value = _read_pointer(root, name, pattern)
    return {"present": value is not None, "bytes": None if value is None else value + "\n"}


def _replace_pointer(root: Path, name: str, expected: dict[str, Any], desired: dict[str, Any]) -> None:
    pattern = _HEX40 if name == ".deploy_commit" else _DEPLOYMENT_ID
    if _snapshot_pointer(root, name, pattern) != expected:
        _fail("deployment pointer changed during adoption")
    with manifest._directory(root, (), create=False) as directory:
        if not desired["present"]:
            if expected["present"]:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            return
        temporary = ".component-pointer-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(desired["bytes"].encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            if _snapshot_pointer(root, name, pattern) != expected:
                _fail("deployment pointer changed during adoption")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _write_adopt_journal(root: Path, value: dict[str, Any], *, new: bool = False) -> None:
    root = admission._physical_root(Path(root))
    admission.require_lock(root)
    raw = manifest._encode(validate_journal(value))
    with manifest._directory(root, (".deployments",), create=False) as directory:
        if new:
            manifest._publish_unnamed(directory, JOURNAL_NAME, raw, mode=0o600)
            return
        temporary = ".component-journal-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            existing = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                _fail("adoption journal was replaced")
            os.replace(temporary, JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


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
        import component_state  # noqa: PLC0415
        if Path(component_state.__file__).resolve() != CODE_ROOT / "scripts/component_state.py":
            _fail("state probe parser belongs to a foreign source")
        return component_state.read_state_probe(root, name)
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


def _legacy_component(root: Path, image: dict[str, Any], image_ref: str, database: dict[str, Any],
                      old_commit: str | None, versions: dict[str, str], operations_source: dict[str, Any],
                      *, observed_version: str | None = None, compose_path: Path | None = None,
                      predecessor_raw: bytes | None = None) -> dict[str, Any]:
    import deployment_manifest as legacy  # noqa: PLC0415
    image_id = _image_id(image.get("Id"), "legacy image ID")
    revision = (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    if old_commit is None or old_commit != revision:
        _fail("legacy image has no matching prior deployment pointer and revision")
    path = root / ".deployments" / (old_commit + ".json")
    with manifest._directory(root, (".deployments",), create=False) as directory:
        raw = manifest._read_file(directory, path.name)
    decoded = _decode(raw)
    try:
        previous = legacy.verify_manifest(path, source_commit=old_commit, image_id=image_id,
                                          image_digest=None, db_path=Path(database["path"]), immutable=True)
    except legacy.ManifestError as exc:
        raise JournalError("legacy deployment manifest cannot bind the installed image") from exc
    digest = previous["image"]["digest"]
    known_digests = image.get("RepoDigests") or []
    with manifest._directory(root, (".deployments",), create=False) as directory:
        unchanged = manifest._read_file(directory, path.name) == raw
    if (not unchanged or previous != decoded or previous["image"]["revision"] != revision
            or previous["database"]["sha256"] != database["sha256"]
            or (digest != image_id and digest not in known_digests)):
        _fail("legacy deployment image digest or evidence differs")
    if observed_version is not None:
        if image_id in versions and versions[image_id] != observed_version:
            _fail("legacy Core version observations disagree")
        versions[image_id] = observed_version
    elif image_id not in versions:
        versions[image_id] = _legacy_core_version(root, image_id, revision, operations_source,
                                                   compose_path=compose_path, predecessor_raw=predecessor_raw)
    version = versions[image_id]
    # The old manifest binds the observed image and revision claim; it does
    # not authenticate its source or establish unavailable wheel identities.
    source = {"revision_claim": revision, "verified_commit": None, "verified_tree": None}
    return {"repository": "My-Denia/WuwaTerm", "version": version, "source": source,
            "core": {"version": version, **source},
            "image": {"ref": image_ref, "id": image_id, "digest": digest, "revision": revision},
            "provenance": {"kind": "legacy-image", "legacy_manifest_sha256": hashlib.sha256(raw).hexdigest(),
                           "wheel_identity": "unavailable", "lock_identity": "unavailable"}}


def _adopt_component(root: Path, component: str, receipt_hash: str | None, image: dict[str, Any],
                     image_ref: str, database: dict[str, Any], old_commit: str | None,
                     versions: dict[str, str], operations_source: dict[str, Any], *,
                     compose_path: Path | None, predecessor_raw: bytes | None) -> dict[str, Any]:
    import component_build as build  # noqa: PLC0415
    if receipt_hash is None:
        return _legacy_component(root, image, image_ref, database, old_commit, versions, operations_source,
                                 compose_path=compose_path, predecessor_raw=predecessor_raw)
    try:
        receipt = build.verify_registered_build(root, receipt_hash)
    except build.BuildError:
        receipt = None
    image_id = image.get("Id")
    revision = (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    if receipt is not None:
        if image_id != receipt["image_id"] or revision != receipt["image_revision"]:
            _fail("installed image differs from its registered source build")
        source = {"revision_claim": revision, "verified_commit": receipt["source"]["commit"],
                  "verified_tree": receipt["source"]["tree"]}
        version = receipt["installed_core_version"]
        core = {"version": version, **source}
        if component == "api":
            provenance = {"kind": "source-image", "build_recipe_sha256": receipt["recipe_sha256"],
                          "build_receipt_sha256": receipt_hash, "wheel_identity": "not-built-from-wheel"}
        else:
            return _legacy_component(root, image, image_ref, database, old_commit, versions, operations_source,
                                     observed_version=(version if receipt["recovery_source"] == {
                                         key: value for key, value in operations_source.items() if key != "compose"
                                     } else None),
                                     compose_path=compose_path,
                                     predecessor_raw=predecessor_raw)
        return {"repository": "My-Denia/WuwaTerm", "version": version, "source": source, "core": core,
                "image": {"ref": image_ref, "id": image_id, "digest": image_id, "revision": revision},
                "provenance": provenance}
    if component != "telegram":
        _fail("installed API image has no registered source build")
    try:
        import component_wheel_build as wheel  # noqa: PLC0415
        receipt = wheel.verify_registered_wheel_build(root, receipt_hash)
    except Exception as exc:
        raise JournalError("installed Telegram image has no registered wheel build") from exc
    if image_id != receipt["image_id"] or revision != receipt["image_revision"]:
        _fail("installed image differs from its registered wheel build")
    source = {"revision_claim": revision, "verified_commit": receipt["source"]["commit"],
              "verified_tree": receipt["source"]["tree"]}
    core_source = receipt["core_source"]
    core = {"version": receipt["core_version"], "revision_claim": core_source["commit"],
            "verified_commit": core_source["commit"], "verified_tree": core_source["tree"]}
    provenance = {"kind": "wheel-image", "telegram_wheel_sha256": receipt["telegram_wheel_sha256"],
                  "core_wheel_sha256": receipt["core_wheel_sha256"],
                  "core_lock_sha256": receipt["core_lock_sha256"],
                  "core_producer_kind": receipt["core_producer_kind"], "build_receipt_sha256": receipt_hash}
    return {"repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
            "source": source, "core": core,
            "image": {"ref": image_ref, "id": image_id, "digest": image_id, "revision": revision},
            "provenance": provenance}


def adopt(root: Path, *, projects: list[str], compose_executable: Path,
          api_receipt: str | None, telegram_receipt: str | None,
          expected_sha: str | None = None) -> str:
    """Record a verified existing topology without starting or changing services."""
    import component_observe as observe  # noqa: PLC0415
    import component_recovery as recovery  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import package_recovery_source as capsule  # noqa: PLC0415

    root = Path(os.path.abspath(os.fspath(root)))
    try:
        admission.admit_component(root, operation="adopt", code_root=CODE_ROOT)
        compose_executable = transaction._compose_binary(compose_executable)
        predecessor = transaction.verify_terminal(root, compose_path=compose_executable)
        predecessor_raw = None if predecessor is None else predecessor[0]
        if _read_pointer(root, ".deploy_current", _DEPLOYMENT_ID) is not None:
            _fail("deployment is already adopted")
        commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
        if expected_sha is not None and (_match(expected_sha, _HEX40, "expected source commit") != commit):
            _fail("source commit differs from --expected-sha")
        recovery._load_packager(root / "scripts" / "package_recovery_source.py")
        recovery._verify_clean_source(root, commit, tree)
        old_commit = _read_pointer(root, ".deploy_commit", _HEX40)
        old_pointers = {".deploy_commit": _snapshot_pointer(root, ".deploy_commit", _HEX40),
                        ".deploy_current": _snapshot_pointer(root, ".deploy_current", _DEPLOYMENT_ID)}
        installed = _project_containers(projects)
        requested = {"api": api_receipt, "telegram": telegram_receipt}
        if any(name not in installed and requested[name] is not None for name in requested):
            _fail("a receipt was supplied for an absent component")
        fingerprint_key = os.urandom(32)
        observations: dict[str, Any] = {}
        snapshots: dict[str, Any] = {}
        images: dict[str, Any] = {}
        references: dict[str, tuple[Any, ...]] = {}
        database_paths = set()
        api_identity = None
        api_identity_path = None
        for name, (raw, project, service) in installed.items():
            config = raw["Config"]
            image_id = _image_id(raw["Image"], "installed image ID")
            image = observe._docker_json("/images/" + image_id + "/json")
            if image.get("Id") != image_id:
                _fail("installed image identity changed")
            actual = observe.observe_container(
                raw["Id"], expected_image_id=image_id, project=project, service=service,
                expected_command=config.get("Cmd"), expected_entrypoint=config.get("Entrypoint"),
                env_fingerprint_key=fingerprint_key,
            )
            resolved, unresolved, files = _compose_config(compose_executable, raw, project, service)
            if resolved.get("image") != config.get("Image"):
                _fail("current Compose image reference differs from the installed container")
            snapshot = _recipe_from_compose(raw, image, actual, resolved, unresolved)
            for path, identity in files.items():
                if path in references and references[path] != identity:
                    _fail("Compose input changed during observation")
                references[path] = identity
            command = config.get("Cmd")
            db_path = (command[1] if type(command) is list and len(command) == 2 and command[0] == "--db"
                       else actual["path_settings"]["WUWATERM_DB_PATH"] or "/app/data/terms.db")
            database_paths.add(observe.resolve_bind_path(snapshot, db_path, writable=False))
            if name == "api":
                settings = actual["path_settings"]
                device = settings["WUWATERM_API_DEVICE_DB_PATH"]
                if device is None:
                    state = settings["WUWATERM_API_STATE_DIR"] or posixpath.join(config.get("WorkingDir") or "/", "state-api")
                    device = posixpath.join(state, "devices.db")
                api_identity_path = observe.resolve_bind_path(snapshot, device, writable=True)
                try:
                    info = Path(api_identity_path).lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        _fail("API identity store is not a regular file")
                    api_identity = {"path": api_identity_path, "device": info.st_dev, "inode": info.st_ino}
            observations[name], snapshots[name], images[name] = actual, snapshot, image
        if len(database_paths) != 1:
            _fail("installed components do not share one database")
        database, database_stat = manifest.read_database_snapshot(Path(database_paths.pop()))
        source = recovery.prepare_source(root, commit, tree, compose_path=compose_executable)
        compose_executable = recovery._verify_bound_compose(root, source)
        components: dict[str, Any] = {"api": None, "telegram": None}
        legacy_versions: dict[str, str] = {}
        for name, (raw, _, _) in installed.items():
            components[name] = _adopt_component(root, name, requested[name], images[name],
                                                raw["Config"]["Image"], database, old_commit, legacy_versions, source,
                                                compose_path=compose_executable, predecessor_raw=predecessor_raw)

        def recheck() -> None:
            recovery._verify_clean_source(root, commit, tree)
            if tool_bytes_identity(root) != source["tool_bytes_sha256"]:
                _fail("adoption source tools changed")
            recovery._verify_bootstrap(root, source)
            recovery._verify_capsule(root, source)
            recovery._verify_bound_compose(root, source)
            latest = _project_containers(projects)
            if set(latest) != set(installed):
                _fail("installed component topology changed during adoption")
            for name, (raw, project, service) in latest.items():
                previous, _, _ = installed[name]
                if raw["Id"] != previous["Id"]:
                    _fail("installed container changed during adoption")
                actual = observe.observe_container(
                    raw["Id"], expected_image_id=previous["Image"], project=project, service=service,
                    expected_command=previous["Config"].get("Cmd"),
                    expected_entrypoint=previous["Config"].get("Entrypoint"),
                    env_fingerprint_key=fingerprint_key,
                )
                if actual != observations[name]:
                    _fail("installed container changed during adoption")
            for path, identity in references.items():
                if _compose_input(Path(path)) != identity:
                    _fail("Compose input changed during adoption")
            current_db, current_stat = manifest.read_database_snapshot(Path(database["path"]))
            if current_db != database or current_stat != database_stat:
                _fail("database changed during adoption")
            if api_identity_path is not None:
                try:
                    info = Path(api_identity_path).lstat()
                except FileNotFoundError:
                    identity = None
                else:
                    if not stat.S_ISREG(info.st_mode):
                        _fail("API identity store changed during adoption")
                    identity = {"path": api_identity_path, "device": info.st_dev, "inode": info.st_ino}
                if identity != api_identity:
                    _fail("API identity store changed during adoption")

        recheck()
        for name, snapshot in snapshots.items():
            path, digest = manifest.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            components[name]["recipe"] = recipe
        deployment_id = manifest.new_deployment_id()
        product = {"schema_version": 2, "deployment_id": deployment_id,
                   "deployment_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                  "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]},
                   "components": components, "database": database, "api_identity": api_identity}
        manifest.validate_manifest(product)
        containers = {name: observations[name]["container"] if name in observations else None
                      for name in ("api", "telegram")}
        target_pointers = {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                           ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}
        product_raw = manifest._encode(product)
        journal = {"schema_version": RUNTIME4_SCHEMA_VERSION, "kind": "component-runtime", "transaction_id": deployment_id,
                   "action": "adopt", "status": "active", "phase": "prepared", "source": source,
                   "predecessor": (None if predecessor is None else
                                   predecessor_reference("runtime", predecessor[1]["transaction_id"], predecessor[0])),
                   "selection": [], "before": {"manifest": None, "topology": copy.deepcopy(components),
                                                 "containers": copy.deepcopy(containers)},
                   "target": {"manifest": product, "containers": copy.deepcopy(containers)},
                   "database": {"binding": database, "stat": database_stat}, "api_identity": api_identity,
                   "migrations": [], "pointers": {"old": old_pointers, "target": target_pointers},
                   "manifest": {"deployment_id": deployment_id,
                                "path": f".deployments/products/{deployment_id}.json",
                                "sha256": hashlib.sha256(product_raw).hexdigest()}}
        recheck()
        current_predecessor = transaction.verify_terminal(root, compose_path=compose_executable)
        if (None if current_predecessor is None else current_predecessor[0]) != predecessor_raw:
            _fail("terminal predecessor changed during adoption preflight")
        transaction.write_initial_journal(root, journal, predecessor_raw=predecessor_raw,
                                          compose_path=compose_executable)
        try:
            journal["phase"] = "immutable-manifest"
            _write_adopt_journal(root, journal)
            manifest.write_manifest(root, product)
            recheck()
            journal["phase"] = "deploy-commit"
            _write_adopt_journal(root, journal)
            _replace_pointer(root, ".deploy_commit", old_pointers[".deploy_commit"], target_pointers[".deploy_commit"])
            recheck()
            journal["phase"] = "deploy-current"
            _write_adopt_journal(root, journal)
            _replace_pointer(root, ".deploy_current", old_pointers[".deploy_current"], target_pointers[".deploy_current"])
            recheck()
            if inspect_deployment(root)["manifest"] != product:
                _fail("adopted metadata readback differs")
            journal.update(status="committed", phase="committed")
            _write_adopt_journal(root, journal)
            return deployment_id
        except Exception as exc:
            try:
                journal["phase"] = "rollback-pointers"
                _write_adopt_journal(root, journal)
                for name in (".deploy_current", ".deploy_commit"):
                    pattern = _DEPLOYMENT_ID if name == ".deploy_current" else _HEX40
                    actual = _snapshot_pointer(root, name, pattern)
                    if actual == old_pointers[name]:
                        continue
                    if actual == target_pointers[name]:
                        _replace_pointer(root, name, actual, old_pointers[name])
                    else:
                        _fail("adoption pointer is no longer a recorded value")
                recheck()
                journal.update(status="rolled_back", phase="rolled-back")
                _write_adopt_journal(root, journal)
            except Exception as rollback_error:
                journal.update(status="unresolved", phase="unresolved")
                try:
                    _write_adopt_journal(root, journal)
                except Exception:
                    pass
                raise JournalError("adoption failed and pointer rollback is unresolved") from rollback_error
            raise JournalError("adoption failed; original pointers were restored") from exc
    except JournalError:
        raise
    except admission.AdmissionError as exc:
        raise JournalError(f"deployment admission refused: {exc}") from exc
    except (recovery.RecoveryError, observe.ObservationError, transaction.TransactionError,
            capsule.CapsuleError, manifest.ManifestError, OSError, UnicodeError, ValueError) as exc:
        raise JournalError("adoption preflight failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-journal")
    validate.add_argument("--journal", required=True, type=Path)
    validate_full = commands.add_parser("validate-full-journal", help=argparse.SUPPRESS)
    validate_full.add_argument("--journal", required=True, type=Path)
    validate_probe = commands.add_parser("validate-probe", help=argparse.SUPPRESS)
    validate_probe.add_argument("--probe-id", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--component", choices=("api", "telegram"))
    inspect.add_argument("--container-id")
    observe = commands.add_parser("observe")
    observe.add_argument("--container-id", required=True)
    observe.add_argument("--image-id", required=True)
    observe.add_argument("--project", required=True)
    observe.add_argument("--service", required=True)
    observe.add_argument("--command-json", required=True)
    observe.add_argument("--entrypoint-json", required=True)
    adopt_command = commands.add_parser("adopt")
    adopt_command.add_argument("--project", action="append", required=True)
    adopt_command.add_argument("--compose-executable", type=Path, required=True)
    adopt_command.add_argument("--api-receipt")
    adopt_command.add_argument("--telegram-receipt")
    adopt_command.add_argument("--expected-sha")
    probe_recovery = commands.add_parser("recover-probe")
    probe_recovery.add_argument("--probe-id", required=True)
    probe_recovery.add_argument("--compose-executable", type=Path, required=True)
    update = commands.add_parser("runtime-update", aliases=["update"])
    update.add_argument("--components", choices=("api", "telegram", "both"), required=True)
    update.add_argument("--api-receipt", help=argparse.SUPPRESS)
    update.add_argument("--telegram-receipt", help=argparse.SUPPRESS)
    update.add_argument("--api-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    update.add_argument("--telegram-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    update.add_argument("--compose-executable", type=Path, required=True)
    update.add_argument("--expected-sha")
    full_update = commands.add_parser("full-update")
    full_update.add_argument("--candidate-db", type=Path, required=True)
    full_update.add_argument("--components", choices=("none", "api", "telegram", "both"), default="none")
    full_update.add_argument("--api-receipt", help=argparse.SUPPRESS)
    full_update.add_argument("--telegram-receipt", help=argparse.SUPPRESS)
    full_update.add_argument("--api-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    full_update.add_argument("--telegram-artifact", type=Path, help="registered completed-artifact acquisition receipt")
    full_update.add_argument("--compose-executable", type=Path, required=True)
    full_update.add_argument("--expected-sha")
    for command in (update, full_update):
        for name in ("api", "telegram"):
            command.add_argument(f"--{name}-compose-file", action="append", default=[])
            command.add_argument(f"--{name}-project-directory")
            command.add_argument(f"--{name}-project")
            command.add_argument(f"--{name}-service")
            command.add_argument(f"--{name}-interpolation-env-file", action="append", default=[])
    build_command = commands.add_parser("build")
    build_types = build_command.add_subparsers(dest="build_component", required=True)
    api_build = build_types.add_parser("api")
    api_build.add_argument("--expected-sha")
    telegram_build = build_types.add_parser("telegram")
    telegram_build.add_argument("--expected-sha")
    telegram_build.add_argument("--telegram-repo", type=Path, required=True)
    telegram_build.add_argument("--telegram-commit", required=True)
    telegram_build.add_argument("--core-wheel", type=Path, required=True)
    telegram_build.add_argument("--core-artifact", type=Path, required=True)
    telegram_build.add_argument("--dependencies", type=Path, required=True)
    telegram_build.add_argument("--build-tool-wheelhouse", type=Path, required=True)
    telegram_build.add_argument("--core-source-repo", type=Path)
    telegram_build.add_argument("--core-checksums", type=Path)
    cancel = commands.add_parser("cancel-build")
    cancel.add_argument("--kind", choices=("source", "wheel"), required=True)
    cancel.add_argument("--build-id", required=True)
    recover = commands.add_parser("recover")
    recover.add_argument("--kind", choices=("runtime", "full"), default="runtime")
    recover.add_argument("--transaction-id", required=True)
    recover.add_argument("--compose-executable", type=Path, required=True)
    internal = commands.add_parser("capsule-recover", help=argparse.SUPPRESS)
    internal.add_argument("--transaction-id", required=True)
    internal.add_argument("--capsule-source-tool", required=True)
    internal.add_argument("--compose-executable", type=Path, required=True)
    internal_full = commands.add_parser("capsule-recover-full", help=argparse.SUPPRESS)
    internal_full.add_argument("--transaction-id", required=True)
    internal_full.add_argument("--capsule-source-tool", required=True)
    internal_full.add_argument("--compose-executable", type=Path, required=True)
    internal_probe = commands.add_parser("capsule-recover-probe", help=argparse.SUPPRESS)
    internal_probe.add_argument("--probe-id", required=True)
    internal_probe.add_argument("--capsule-source-tool", required=True)
    internal_probe.add_argument("--compose-executable", type=Path, required=True)
    validate_build = commands.add_parser("validate-build-attempt", help=argparse.SUPPRESS)
    validate_build.add_argument("--kind", choices=("source", "wheel"), required=True)
    validate_build.add_argument("--build-id", required=True)
    cancel_internal = commands.add_parser("capsule-cancel-build", help=argparse.SUPPRESS)
    cancel_internal.add_argument("--kind", choices=("source", "wheel"), required=True)
    cancel_internal.add_argument("--build-id", required=True)
    cancel_internal.add_argument("--capsule-source-tool", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-journal":
            if not args.journal.is_absolute():
                raise JournalError("journal path must be absolute")
            load_journal(args.journal)
            return 0
        if args.command == "validate-full-journal":
            import component_full as full  # noqa: PLC0415
            if not args.journal.is_absolute():
                raise JournalError("full journal path must be absolute")
            try:
                full.load_journal(args.journal)
            except full.FullError as exc:
                raise JournalError("full journal is invalid") from exc
            return 0
        if args.command == "observe":
            try:
                command = json.loads(args.command_json)
                entrypoint = json.loads(args.entrypoint_json)
            except (ValueError, TypeError) as exc:
                raise JournalError("command and entrypoint must be JSON arrays or null") from exc
            print(json.dumps(observe_unrecorded(args.container_id, args.image_id, args.project, args.service,
                                               command, entrypoint), sort_keys=True))
            return 0
        if args.command == "inspect":
            if args.root is None:
                raise JournalError("deployment root is required")
            if (args.component is None) != (args.container_id is None):
                raise JournalError("component and container ID must be supplied together")
            result = (inspect_deployment(args.root) if args.component is None else
                      inspect_container(args.root, args.component, args.container_id))
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.root is None:
            raise JournalError("deployment root is required")
        root = Path(os.path.abspath(os.fspath(args.root)))
        if args.command == "capsule-cancel-build":
            import component_recovery as recovery  # noqa: PLC0415
            # Producer attempts are protocol-1 only.
            if _capsule_protocol(full=False) != 1 or not capsule_execution_ready(full=False):
                _fail("saved-source builder cancellation is not yet enabled")
            try:
                root = admission._physical_root(root)
                raw, attempt = recovery._build_attempt(root, args.kind, args.build_id)
                source = attempt["recovery_source"]
                if source["source_tool_sha256"] != args.capsule_source_tool:
                    _fail("builder cancellation source identity differs")
                admission._verify_recovery_extraction(root, source, CODE_ROOT)
                import component_build_isolation as isolation  # noqa: PLC0415
                import component_build as build  # noqa: PLC0415
                if Path(isolation.__file__).resolve() != CODE_ROOT / "scripts/component_build_isolation.py":
                    _fail("builder cancellation imported a foreign isolation helper")
                if recovery._build_attempt(root, args.kind, args.build_id)[0] != raw:
                    _fail("builder attempt changed before cancellation")
                recovery._verify_bootstrap(root, source)
                try:
                    result = isolation.request_cancel(root, args.kind, args.build_id)
                except (isolation.IsolationError, build.BuildError) as exc:
                    raise JournalError("builder cancellation refused or remains unresolved") from exc
                if (type(result) is not dict or set(result) != {"build_id", "status"}
                        or result["build_id"] != args.build_id
                        or result["status"] not in ("requested", "failed", "retained_import", "registered")):
                    _fail("builder cancellation returned an invalid outcome")
            except (admission.AdmissionError, recovery.RecoveryError) as exc:
                raise JournalError("builder cancellation refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "validate-build-attempt":
            import component_build as build  # noqa: PLC0415
            import component_wheel_build as wheel  # noqa: PLC0415
            identifier = _match(args.build_id, _DEPLOYMENT_ID, "build ID")
            try:
                (build._read_attempt if args.kind == "source" else wheel.read_attempt)(root, identifier + ".json")
            except build.BuildError as exc:
                raise JournalError("build attempt cannot be verified") from exc
            return 0
        if args.command == "validate-probe":
            identifier = _match(args.probe_id, _DEPLOYMENT_ID, "probe ID")
            pending = pending_adopt_probe_staging(root)
            if pending is not None and pending != identifier:
                _fail("another probe staging claim requires its own recovery")
            record = _read_adopt_probe(root, identifier + ".json", _pre_reconcile=pending is not None)
            if record["kind"] != "legacy-core-version":
                _fail("standalone recovery is limited to a version probe")
            return 0
        if args.command == "capsule-recover-probe":
            # The declared protocol must be supported AND its own flag open.
            if not capsule_execution_ready(full=False):
                _fail("capsule recovery execution is not yet available")
            recover_adopt_probe(root, args.probe_id, compose_path=args.compose_executable,
                                capsule_source_tool=args.capsule_source_tool)
            record = _read_adopt_probe(root, args.probe_id + ".json")
            status = "completed" if record["status"] == "completed" else "failed-retryable"
            print(json.dumps({"probe_id": args.probe_id, "status": status}, sort_keys=True))
            return 0
        if args.command == "capsule-recover":
            import component_transaction as transaction  # noqa: PLC0415
            if not capsule_execution_ready(full=False):
                _fail("capsule recovery execution is not yet available")
            try:
                result = transaction.recover(root, args.transaction_id, args.capsule_source_tool,
                                             compose_path=args.compose_executable)
            except transaction.TransactionError as exc:
                raise JournalError("recorded transaction recovery refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "capsule-recover-full":
            if not capsule_execution_ready(full=True):
                _fail("full capsule recovery execution is not yet available")
            import component_full as full  # noqa: PLC0415
            try:
                result = full.recover(root, args.transaction_id, args.capsule_source_tool,
                                      compose_path=args.compose_executable)
            except full.FullError as exc:
                raise JournalError("full transaction recovery refused or remains unresolved") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "recover":
            import component_recovery as recovery  # noqa: PLC0415
            admission.require_lock(root)
            try:
                _, record = (recovery._full_journal(root, args.transaction_id) if args.kind == "full" else
                             recovery._journal(root, args.transaction_id))
                recovery._verify_bootstrap(root, record["source"])
            except recovery.RecoveryError as exc:
                raise JournalError("recorded transaction bootstrap cannot be verified") from exc
            source = record["source"]
            script = root / source["bootstrap"]["path"] / "component_recovery.py"
            kind = "full" if args.kind == "full" else "adopt" if record["action"] == "adopt" else "runtime"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", kind,
                 "--id", args.transaction_id, "--compose-executable", str(args.compose_executable)],
                env=recovery._isolated_env(),
                pass_fds=(9,), check=False,
            )
            return process.returncode
        if args.command == "adopt":
            import component_recovery as recovery  # noqa: PLC0415
            admission.admit_component(root, operation="adopt", code_root=CODE_ROOT)
            if not capsule_execution_ready(full=False):
                _fail("adoption is unavailable until saved-source recovery is enabled")
            deployment_id = adopt(args.root, projects=args.project, compose_executable=args.compose_executable,
                                  api_receipt=args.api_receipt, telegram_receipt=args.telegram_receipt,
                                  expected_sha=args.expected_sha)
            print(json.dumps({"deployment_id": deployment_id, "polling_health": "unconfirmed"}, sort_keys=True))
            return 0
        if args.command == "recover-probe":
            import component_recovery as recovery  # noqa: PLC0415
            admission.require_lock(root)
            try:
                _, record = recovery._probe(root, args.probe_id)
                recovery._verify_bootstrap(root, record["source"])
            except recovery.RecoveryError as exc:
                raise JournalError("recorded version probe bootstrap cannot be verified") from exc
            script = root / record["source"]["bootstrap"]["path"] / "component_recovery.py"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root), "--kind", "versionprobe",
                 "--id", args.probe_id, "--compose-executable", str(args.compose_executable)],
                env=recovery._isolated_env(),
                pass_fds=(9,), check=False,
            )
            return process.returncode
        if args.command == "cancel-build":
            import component_recovery as recovery  # noqa: PLC0415
            try:
                root = recovery._root(root)
                _, record = recovery._build_attempt(root, args.kind, args.build_id)
                recovery._verify_bootstrap(root, record["recovery_source"])
            except recovery.RecoveryError as exc:
                raise JournalError("recorded builder bootstrap cannot be verified") from exc
            script = root / record["recovery_source"]["bootstrap"]["path"] / "component_recovery.py"
            process = subprocess.run(
                [sys.executable, "-I", "-B", str(script), "--root", str(root),
                 "--kind", "sourcebuild" if args.kind == "source" else "wheelbuild", "--id", args.build_id],
                env=recovery._isolated_env(), check=False,
            )
            return process.returncode
        if args.command == "build":
            admission.admit_component(root, operation="build", code_root=CODE_ROOT)
            if not PRODUCER_EXECUTION_READY:
                _fail("component builds are unavailable until the builder lifecycle is supported")
            import component_build as build  # noqa: PLC0415
            import component_wheel_build as wheel  # noqa: PLC0415
            import component_recovery as recovery  # noqa: PLC0415
            import package_recovery_source as capsule  # noqa: PLC0415
            attempt_kind = "source" if args.build_component == "api" else "wheel"
            attempt_directory = root / ".deployments" / (
                admission.BUILD_ATTEMPTS if attempt_kind == "source" else admission.WHEEL_BUILD_ATTEMPTS)
            before_attempts = ({item.name for item in attempt_directory.iterdir()}
                               if attempt_directory.exists() else set())
            try:
                commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
                tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
                if args.expected_sha is not None and _match(args.expected_sha, _HEX40, "expected source commit") != commit:
                    _fail("source commit differs from --expected-sha")
                if args.build_component == "api":
                    digest = build.build(root, commit, tree)
                else:
                    revision = _match(args.telegram_commit, _HEX40, "Telegram source commit")
                    repo = args.telegram_repo
                    if not repo.is_absolute() or repo.resolve(strict=True) != repo or not repo.is_dir():
                        _fail("Telegram source repository is not a physical absolute directory")
                    telegram_tree = recovery.safe_git(repo, "rev-parse", "--verify", revision + "^{tree}",
                                                      lock_root=root).decode("ascii").strip()
                    digest = wheel.build_wheel_image(root, commit, tree, repo, revision, telegram_tree,
                                                     args.core_wheel, args.core_artifact, args.dependencies,
                                                     args.core_source_repo, args.core_checksums,
                                                     args.build_tool_wheelhouse)
                print(_match(digest, _HEX64, "registered build receipt"))
                return 0
            except (build.BuildError, capsule.CapsuleError, recovery.RecoveryError, OSError, UnicodeError) as exc:
                try:
                    names = ({item.name for item in attempt_directory.iterdir()} - before_attempts
                             if attempt_directory.exists() else set())
                    if len(names) == 1:
                        name = next(iter(names))
                        reader = build._read_attempt if attempt_kind == "source" else wheel.read_attempt
                        record = reader(root, name)
                        if record["status"] == "active":
                            raise JournalError(f"build {record['build_id']} requires cancel-build "
                                               f"--kind {attempt_kind} --build-id {record['build_id']}") from exc
                except JournalError:
                    raise
                except (build.BuildError, OSError, ValueError, TypeError, KeyError):
                    pass
                raise JournalError("component build failed or requires recorded attempt recovery") from exc
        if args.command in ("runtime-update", "update", "full-update"):
            import component_full as full  # noqa: PLC0415
            import component_prepare as prepare  # noqa: PLC0415
            import component_recovery as recovery  # noqa: PLC0415
            import component_transaction as transaction  # noqa: PLC0415
            import package_recovery_source as capsule  # noqa: PLC0415
            is_full = args.command == "full-update"
            admission.admit_component(root, operation="full-update" if is_full else "update", code_root=CODE_ROOT)
            if is_full and not capsule_execution_ready(full=True):
                _fail("full update is unavailable until full saved-source recovery is enabled")
            if not is_full and not capsule_execution_ready(full=False):
                _fail("runtime update is unavailable until saved-source recovery is enabled")
            if args.expected_sha is not None:
                expected = _match(args.expected_sha, _HEX40, "expected source commit")
                try:
                    actual = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
                except (capsule.CapsuleError, recovery.RecoveryError, UnicodeError) as exc:
                    raise JournalError("operations source cannot be verified") from exc
                if expected != actual:
                    _fail("source commit differs from --expected-sha")
            recipes = {}
            for name in ("api", "telegram"):
                files = getattr(args, name + "_compose_file")
                directory = getattr(args, name + "_project_directory")
                project = getattr(args, name + "_project")
                service = getattr(args, name + "_service")
                interpolation = getattr(args, name + "_interpolation_env_file")
                if files or directory or project or service or interpolation:
                    if not files or not directory or not project or not service:
                        _fail(f"{name} Compose recipe requires files, project directory, project and service")
                    recipes[name] = {"compose_files": files, "project_directory": directory,
                                     "project": project, "service": service,
                                     "interpolation_env_files": interpolation}
            receipts = {name: value for name, value in (("api", args.api_receipt),
                        ("telegram", args.telegram_receipt)) if value is not None}
            artifacts = {name: str(value) for name, value in (("api", args.api_artifact),
                         ("telegram", args.telegram_artifact)) if value is not None}
            if receipts and artifacts:
                _fail("producer receipts and completed artifacts cannot be combined")
            try:
                request = ({"selection": args.components, "receipts": receipts, "recipes": recipes} if receipts else
                           {"selection": args.components, "artifacts": artifacts, "recipes": recipes})
                journal = (full.prepare(root, args.candidate_db, request, compose_path=args.compose_executable,
                                        expected_sha=args.expected_sha) if is_full else
                           prepare.prepare_update(root, request, args.compose_executable))
            except (full.FullError, prepare.PrepareError, recovery.RecoveryError, transaction.TransactionError) as exc:
                raise JournalError("full update preparation failed" if is_full else "runtime update preparation failed") from exc
            if args.expected_sha is not None and (
                _match(args.expected_sha, _HEX40, "expected source commit") != journal["source"]["commit"]
            ):
                _fail("source commit differs from --expected-sha")
            bound_compose = recovery._verify_bound_compose(root, journal["source"])
            try:
                result = (full.update(root, journal, compose_path=bound_compose) if is_full else
                          transaction.update(root, journal, compose_path=bound_compose))
            except (full.FullError, transaction.TransactionError) as exc:
                try:
                    saved = (full.load_journal(root / ".deployments" / full.JOURNAL_NAME) if is_full else
                             load_journal(root / ".deployments" / JOURNAL_NAME))
                except (full.FullError, JournalError):
                    saved = None
                if saved is not None and saved["transaction_id"] == journal["transaction_id"]:
                    raise JournalError(f"transaction {journal['transaction_id']} interrupted; use recover "
                                       f"--kind {'full' if is_full else 'runtime'} "
                                       f"--transaction-id {journal['transaction_id']}") from exc
                raise JournalError("full update preflight failed" if is_full else "runtime update preflight failed") from exc
            print(json.dumps(result, sort_keys=True))
            return 0
        _fail("unsupported component command")
    except (JournalError, admission.AdmissionError, OSError, ValueError) as exc:
        print(f"component runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
