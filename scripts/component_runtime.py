#!/usr/bin/env python3
"""Validate the historical component-runtime journal without live mutations.

Transaction execution is deliberately unavailable until the source-capsule
lock inheritance and source-image receipt contracts have been resolved.  This
module never treats a syntactically valid journal as evidence of a live Docker
binding or a healthy service.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
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
JOURNAL_SCHEMA_VERSION = 2
TOOL_PATHS = (
    "deploy/product-update.sh",
    "scripts/deployment_admission.py",
    "scripts/component_build.py",
    "scripts/component_recovery.py",
    "scripts/component_runtime.py",
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


class JournalError(ValueError):
    """An unsupported, malformed, or inconsistent transaction journal."""


def _fail(message: str) -> None:
    raise JournalError(message)


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


def validate_journal(value: Any) -> dict[str, Any]:
    """Reject unknown schemas and unsafe or inconsistent recovery records.

    This is a pure structural parser. Recovery must separately verify capsule
    bytes, current Docker observations, DB identity, state files and pointers.
    """
    journal = _object(value, {
        "schema_version", "kind", "transaction_id", "action", "status", "phase",
        "source", "selection", "before", "target", "database", "api_identity",
        "migrations", "pointers", "manifest",
    }, "journal")
    if type(journal["schema_version"]) is not int or journal["schema_version"] != JOURNAL_SCHEMA_VERSION:
        _fail("journal: unsupported schema version")
    if journal["kind"] != "component-runtime":
        _fail("journal: unsupported kind")
    transaction_id = _match(journal["transaction_id"], _DEPLOYMENT_ID, "journal.transaction_id")
    action = journal["action"]
    if action not in ("adopt", "update"):
        _fail("journal: unsupported action")
    status, phase = journal["status"], journal["phase"]
    if (status not in ("active", "unresolved", "rolled_back", "committed")
            or type(phase) is not str or phase not in PHASES):
        _fail("journal: unsupported status or phase")
    if (status == "committed") != (phase == "committed") or (status == "rolled_back") != (phase == "rolled-back"):
        _fail("journal: final phase and status disagree")

    source = _object(journal["source"], {
        "commit", "tree", "source_tool_sha256", "tool_bytes_sha256", "capsule", "bootstrap",
    }, "journal.source")
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
    target_manifest = _manifest(target["manifest"], "journal.target.manifest")
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
    for name in (".full-transaction.json", ".runtime-transaction.json", JOURNAL_NAME,
                 admission.COMPONENT_FULL, admission.BUILD_ATTEMPTS):
        try:
            info = (deployments / name).lstat()
        except FileNotFoundError:
            journal_states[name] = "absent"
        else:
            journal_states[name] = "present" if stat.S_ISREG(info.st_mode) else "untrusted"
    if deployment_id is None:
        result = {"binding": "unknown", "reason": "legacy-or-unadopted",
                  "source_commit": commit, "deployment_id": None, "manifest": None,
                  "live_binding": "unverified", "journals": journal_states}
    else:
        try:
            product = manifest.read_manifest(root, deployment_id)
        except manifest.ManifestError as exc:
            raise JournalError("recorded product manifest cannot be verified") from exc
        if commit != product["operations"]["source_commit"]:
            _fail("deployment pointers do not match the recorded manifest")
        result = {"binding": "recorded", "reason": None, "source_commit": commit,
                  "deployment_id": deployment_id, "manifest": product,
                  "live_binding": "unverified", "journals": journal_states}
    if (_read_pointer(root, ".deploy_commit", _HEX40) != commit
            or _read_pointer(root, ".deploy_current", _DEPLOYMENT_ID) != deployment_id):
        _fail("deployment pointers changed during inspection")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-journal")
    validate.add_argument("--journal", required=True, type=Path)
    commands.add_parser("inspect")
    commands.add_parser("adopt")
    update = commands.add_parser("update")
    update.add_argument("--components", choices=("api", "telegram", "both"), required=True)
    commands.add_parser("recover")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-journal":
            if not args.journal.is_absolute():
                raise JournalError("journal path must be absolute")
            load_journal(args.journal)
            return 0
        if args.command == "inspect":
            if args.root is None:
                raise JournalError("deployment root is required")
            print(json.dumps(inspect_deployment(args.root), sort_keys=True))
            return 0
        if args.root is None:
            raise JournalError("deployment root is required")
        try:
            root = Path(os.path.abspath(os.fspath(args.root)))
            admission.admit_component(root, operation=args.command, code_root=CODE_ROOT)
        except admission.AdmissionError as exc:
            raise JournalError(f"deployment admission refused: {exc}") from exc
        # No live inspection or mutation is available in this offline parser.
        # Refusal is explicit, including for recover: a journal must never be
        # reinterpreted or mutated by an incomplete transaction implementation.
        raise JournalError("component runtime execution is not yet available")
    except JournalError as exc:
        print(f"component runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
