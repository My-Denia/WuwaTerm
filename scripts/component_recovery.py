#!/usr/bin/env python3
"""Preserve and launch the exact source of an interrupted component update.

The copied bootstrap and its packager are independent of the deployment
checkout.  Recovery itself belongs to the source version's coordinator; this
module does not decide that the running services or pointers are safe.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from typing import Any

sys.dont_write_bytecode = True
capsule: Any = None

CODE_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_FILES = ("component_recovery.py", "package_recovery_source.py")
JOURNAL_NAME = ".component-runtime-transaction.json"
FULL_JOURNAL_NAME = ".component-full-transaction.json"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
DEPLOYMENT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")
# A protocol marker is not evidence that A independently re-verifies the
# recorded capsule and live objects. Keep execution closed until that
# coordinator and its real interruption tests exist.
RECOVERY_EXECUTION_READY = False
FULL_RECOVERY_EXECUTION_READY = False
# Protocol-2 (completed-artifact) capsules have their own readiness. Opening
# protocol 2 never makes a protocol-1 capsule executable: each saved source is
# dispatched by the protocol its own extracted module declares.
ARTIFACT_RECOVERY_EXECUTION_READY = True
ARTIFACT_FULL_RECOVERY_EXECUTION_READY = True
SUPPORTED_PROTOCOLS = frozenset({1, 2})
# Historical journals stay readable by their own validators. Schema 5 records
# only completed-artifact updates, and the FULL5 journal is the preserved-state
# form of the FULL4 record: no copy claims or staging fields.
RUNTIME_SCHEMA_VERSIONS = (2, 3, 4, 5)
FULL_SCHEMA_VERSIONS = (3, 4, 5)


def protocol_ready(protocol: object, *, full: bool) -> bool:
    """Return this bootstrap's readiness for one declared capsule protocol."""
    if type(protocol) is not int or protocol not in SUPPORTED_PROTOCOLS:
        return False
    if protocol == 1:
        return (FULL_RECOVERY_EXECUTION_READY if full else RECOVERY_EXECUTION_READY) is True
    return (ARTIFACT_FULL_RECOVERY_EXECUTION_READY if full else ARTIFACT_RECOVERY_EXECUTION_READY) is True


class RecoveryError(RuntimeError):
    """The saved source or its binding cannot safely be used."""


def _root(path: Path) -> Path:
    path = Path(path)
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
            raise RecoveryError("deployment root is not a physical directory")
        directory = path / ".deployments"
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise RecoveryError("deployment metadata directory is not a directory")
    except OSError as exc:
        raise RecoveryError("deployment root is unavailable") from exc
    return path


class _Lock:
    def __init__(self, root: Path):
        self.root = root
        self.pass_fds()

    def pass_fds(self) -> tuple[int, ...]:
        path = self.root / ".deployments" / ".deployment.lock"
        try:
            actual = os.fstat(9)
            expected = path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
                    or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                    or os.readlink("/proc/self/fd/9") != str(path)):
                raise RecoveryError("deployment lock FD9 does not match the root")
            fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryError("deployment lock FD9 is unavailable") from exc
        return (9,)


def _lock(root: Path) -> _Lock:
    return _Lock(root)


def _acquire_launcher_lock(root: Path) -> None:
    """Reuse an inherited FD9 or acquire the existing root-bound lock."""
    root = _root(root)
    try:
        os.fstat(9)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise RecoveryError("deployment lock FD9 is unavailable") from exc
        fd = os.open(root / ".deployments" / ".deployment.lock",
                     os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RecoveryError("deployment lock is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.dup2(fd, 9, inheritable=True)
        finally:
            if fd != 9:
                os.close(fd)
    _lock(root)


def _load_packager(path: Path) -> None:
    """Import the local packager only after its bootstrap bytes are verified."""
    global capsule
    try:
        spec = importlib.util.spec_from_file_location("component_saved_packager", path)
        if spec is None or spec.loader is None:
            raise RecoveryError("cannot load source packager")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        capsule = module
    except (OSError, ImportError) as exc:
        raise RecoveryError("cannot load source packager") from exc


def _read_regular(path: Path, *, limit: int = 16 * 1024 * 1024) -> bytes:
    """Read without following links and reject replacements during the read."""
    if not path.is_absolute():
        raise RecoveryError("path must be absolute")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directories: list[int] = []
    fd: int | None = None
    try:
        directory = os.open("/", flags | os.O_DIRECTORY)
        directories.append(directory)
        for part in path.parts[1:-1]:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            directories.append(directory)
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise RecoveryError("invalid regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise RecoveryError("file is too large")
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise RecoveryError("file changed while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise RecoveryError("cannot safely read file") from exc
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def _hash(path: Path) -> str:
    if not path.is_absolute():
        raise RecoveryError("path must be absolute")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directories: list[int] = []
    fd: int | None = None
    try:
        directory = os.open("/", flags | os.O_DIRECTORY)
        directories.append(directory)
        for part in path.parts[1:-1]:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            directories.append(directory)
        fd = os.open(path.name, flags | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("invalid regular file")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise RecoveryError("file changed while hashing")
        return digest.hexdigest()
    except OSError as exc:
        raise RecoveryError("cannot safely hash file") from exc
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def _id(commit: str, tree: str, tool_hash: str) -> str:
    if not HEX40.fullmatch(commit) or not HEX40.fullmatch(tree) or not HEX64.fullmatch(tool_hash):
        raise RecoveryError("invalid source identity")
    return hashlib.sha256(b"wuwaterm-component-source-v1\0" + bytes.fromhex(commit)
                          + bytes.fromhex(tree) + bytes.fromhex(tool_hash)).hexdigest()


def _bootstrap_id(files: dict[str, str]) -> str:
    if type(files) is not dict or set(files) != set(BOOTSTRAP_FILES):
        raise RecoveryError("invalid bootstrap files")
    digest = hashlib.sha256(b"wuwaterm-component-bootstrap-v1\0")
    for name in sorted(BOOTSTRAP_FILES):
        value = files[name]
        if type(value) is not str or not HEX64.fullmatch(value):
            raise RecoveryError("invalid bootstrap hash")
        encoded = name.encode("ascii")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def _validate_source(source: Any) -> dict[str, Any]:
    base = {
        "commit", "tree", "tool_bytes_sha256", "source_tool_sha256", "capsule", "bootstrap"
    }
    if type(source) is not dict or set(source) not in (base, base | {"compose"}):
        raise RecoveryError("invalid source record")
    try:
        identity = _id(source["commit"], source["tree"], source["tool_bytes_sha256"])
    except (TypeError, KeyError) as exc:
        raise RecoveryError("invalid source identity") from exc
    if source["source_tool_sha256"] != identity:
        raise RecoveryError("source/tool identity mismatch")
    record = source["capsule"]
    if type(record) is not dict or set(record) != {"path", "manifest_sha256", "archive_sha256", "bundle_sha256"}:
        raise RecoveryError("invalid source capsule record")
    if record["path"] != f".deployments/component-source-{identity}":
        raise RecoveryError("invalid source capsule path")
    for name in ("manifest_sha256", "archive_sha256", "bundle_sha256"):
        if type(record[name]) is not str or not HEX64.fullmatch(record[name]):
            raise RecoveryError("invalid source capsule digest")
    bootstrap = source["bootstrap"]
    if type(bootstrap) is not dict or set(bootstrap) != {"path", "files_sha256", "sha256"}:
        raise RecoveryError("invalid bootstrap record")
    if (bootstrap["path"] != f".deployments/component-bootstrap-{identity}"
            or bootstrap["sha256"] != _bootstrap_id(bootstrap["files_sha256"])):
        raise RecoveryError("bootstrap identity mismatch")
    if "compose" in source:
        compose = source["compose"]
        if (type(compose) is not dict or set(compose) != {"path", "sha256"}
                or type(compose["sha256"]) is not str or not HEX64.fullmatch(compose["sha256"])
                or compose["path"] != f".deployments/component-compose-{compose['sha256']}"):
            raise RecoveryError("invalid saved Compose identity")
    return source


def _verify_bootstrap(root: Path, source: dict[str, Any]) -> None:
    record = source["bootstrap"]
    directory = root / record["path"]
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise RecoveryError("bootstrap is not a directory")
        if {p.name for p in directory.iterdir()} != set(BOOTSTRAP_FILES):
            raise RecoveryError("bootstrap files differ")
        for name, expected in record["files_sha256"].items():
            if _hash(directory / name) != expected:
                raise RecoveryError("bootstrap file differs")
    except OSError as exc:
        raise RecoveryError("bootstrap cannot be inspected") from exc


def _verify_capsule(root: Path, source: dict[str, Any], extracted: Path | None = None) -> None:
    record = source["capsule"]
    source_dir = root / record["path"]
    try:
        if not stat.S_ISDIR(source_dir.lstat().st_mode):
            raise RecoveryError("source capsule is not a directory")
        for filename, key in (("manifest.json", "manifest_sha256"), ("source.tar", "archive_sha256"),
                              ("source.bundle", "bundle_sha256")):
            if _hash(source_dir / filename) != record[key]:
                raise RecoveryError("source capsule digest differs")
        result = capsule.verify(source_dir, source["commit"], extracted=extracted, allow_backup=True)
        if result["source_tree"] != source["tree"]:
            raise RecoveryError("source capsule tree differs")
        for name, digest in source["bootstrap"]["files_sha256"].items():
            if result["files"].get("scripts/" + name, {}).get("sha256") != digest:
                raise RecoveryError("bootstrap does not match source capsule")
        if extracted is not None:
            seen: set[str] = set()
            for directory, directories, filenames in os.walk(extracted, followlinks=False):
                for name in directories:
                    if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                        raise RecoveryError("extracted source contains an unsafe directory")
                for name in filenames:
                    path = Path(directory) / name
                    relative = path.relative_to(extracted).as_posix()
                    entry = result["files"].get(relative)
                    if (entry is None or _hash(path) != entry["sha256"]
                            or bool(path.stat(follow_symlinks=False).st_mode & 0o111) != (entry["mode"] == "100755")):
                        raise RecoveryError("extracted source differs from capsule")
                    seen.add(relative)
            if seen != set(result["files"]):
                raise RecoveryError("extracted source is incomplete")
        # Detect replacements after the packager's verification and extraction.
        for filename, key in (("manifest.json", "manifest_sha256"), ("source.tar", "archive_sha256"),
                              ("source.bundle", "bundle_sha256")):
            if _hash(source_dir / filename) != record[key]:
                raise RecoveryError("source capsule changed during verification")
    except (OSError, capsule.CapsuleError, ValueError, UnicodeError) as exc:
        raise RecoveryError("source capsule verification failed") from exc


def _isolated_env() -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_LAZY_FETCH": "1"}
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid is not None:
        if not re.fullmatch(r"[0-9]{1,10}", sudo_uid) or int(sudo_uid) > 2**32 - 2:
            raise RecoveryError("operator identity is invalid")
        env["SUDO_UID"] = sudo_uid
    return env


def _run_readonly_git(repo: Path, *args: str, lock_root: Path | None = None,
                      allow_absent: bool = False) -> bytes:
    repo = Path(repo)
    try:
        if not repo.is_absolute() or repo.resolve(strict=True) != repo or not repo.is_dir():
            raise RecoveryError("Git repository is not a physical absolute directory")
        descriptors = () if lock_root is None else _lock(_root(lock_root)).pass_fds()
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "submodule.recurse=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "protocol.allow=never", *args],
            env=_isolated_env(), pass_fds=descriptors, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
        if result.returncode and not (allow_absent and result.returncode == 1):
            raise RecoveryError("read-only Git verification failed")
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError("read-only Git verification failed") from exc


def _reject_partial_source(repo: Path, *, lock_root: Path | None = None) -> None:
    """Fail before object access when PR187's unchanged packager might fetch."""
    config = _run_readonly_git(repo, "config", "--local", "--get-regexp",
                               r"^(extensions\.partialclone|remote\..*\.promisor)$",
                               lock_root=lock_root, allow_absent=True)
    if config:
        raise RecoveryError("partial-clone source is unsupported")
    try:
        common = Path(_run_readonly_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                        lock_root=lock_root).decode("utf-8").strip())
        if not common.is_absolute() or common.resolve(strict=True) != common or not common.is_dir():
            raise RecoveryError("Git object storage is not physical")
        objects = common / "objects"
        if objects.resolve(strict=True) != objects or not objects.is_dir():
            raise RecoveryError("Git object storage is not physical")
        pack = objects / "pack"
        if (pack.exists() or pack.is_symlink()) and (pack.resolve(strict=True) != pack or not pack.is_dir()):
            raise RecoveryError("Git pack storage is not physical")
        if any(pack.glob("*.promisor")):
            raise RecoveryError("partial-clone source is unsupported")
        info = objects / "info"
        if (info.exists() or info.is_symlink()) and (info.resolve(strict=True) != info or not info.is_dir()):
            raise RecoveryError("Git object metadata is not physical")
        for alternate in (objects / "info/alternates", objects / "info/http-alternates"):
            if alternate.exists() or alternate.is_symlink():
                raise RecoveryError("alternate Git object storage is unsupported")
    except (OSError, UnicodeError) as exc:
        raise RecoveryError("Git object storage cannot be verified") from exc


def safe_git(repo: Path, *args: str, lock_root: Path | None = None) -> bytes:
    _reject_partial_source(repo, lock_root=lock_root)
    return _run_readonly_git(repo, *args, lock_root=lock_root)


def _verify_tools(extracted: Path, source: dict[str, Any], lock: _Lock | None,
                  *, require_recovery_protocol: bool = False, require_full_protocol: bool = False,
                  protocols: frozenset[int] = SUPPORTED_PROTOCOLS) -> int | None:
    """Verify A's tool bytes and return the protocol A itself declares."""
    # Import only the verified, extracted A tree, in isolated mode. B's current
    # Python source and any inherited PYTHON*/GIT_* settings are never used.
    code = ("import json,pathlib,sys; r=pathlib.Path(sys.argv[1]); "
            "sys.path.insert(0,str(r/'scripts')); import component_runtime as m; "
            "print(json.dumps([m.tool_bytes_identity(r), getattr(m, 'CAPSULE_RECOVERY_PROTOCOL', None), "
            "getattr(m, 'CAPSULE_FULL_RECOVERY_PROTOCOL', None)]))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(extracted)],
                            cwd=extracted, env=_isolated_env(), pass_fds=lock.pass_fds() if lock else (),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        observed = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        observed = None
    if (result.returncode or type(observed) is not list or len(observed) != 3
            or observed[0] != source["tool_bytes_sha256"]):
        raise RecoveryError("saved tool bytes do not match source identity")
    if require_recovery_protocol and (type(observed[1]) is not int or observed[1] not in protocols):
        raise RecoveryError("saved source has no enabled capsule recovery protocol")
    if require_full_protocol and (type(observed[2]) is not int or observed[2] not in protocols):
        raise RecoveryError("saved source has no enabled full recovery protocol")
    return observed[2] if require_full_protocol else observed[1]


def _verify_journal(root: Path, extracted: Path, lock: _Lock) -> None:
    """Let the exact source version validate the complete journal schema."""
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
         "validate-journal", "--journal", str(root / ".deployments" / JOURNAL_NAME)],
        cwd=extracted, env=_isolated_env(), pass_fds=lock.pass_fds(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RecoveryError("saved source rejected the complete journal")


def _verify_clean_source(root: Path, commit: str, tree: str) -> dict[str, dict[str, Any]]:
    if not HEX40.fullmatch(commit) or not HEX40.fullmatch(tree):
        raise RecoveryError("invalid source commit or tree")
    try:
        top = safe_git(root, "rev-parse", "--show-toplevel", lock_root=root).decode("utf-8").strip()
        head = safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        if top != str(root) or head != commit:
            raise RecoveryError("source checkout or commit differs")
        if safe_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no", lock_root=root):
            raise RecoveryError("tracked source checkout is not clean")
        for entry in safe_git(root, "ls-files", "-v", "-z", lock_root=root).split(b"\0"):
            if entry and entry[:1] != b"H":
                raise RecoveryError("unsupported source index flags")
        actual_tree, files = capsule.tree_files(root, commit)
        _reject_partial_source(root, lock_root=root)
        if actual_tree != tree:
            raise RecoveryError("source tree differs")
        for name, item in files.items():
            path = root / name
            data = _read_regular(path, limit=max(item["size"], 1))
            if (len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]
                    or bool(path.stat(follow_symlinks=False).st_mode & 0o111) != (item["mode"] == "100755")):
                raise RecoveryError("tracked source differs from its commit")
        return files
    except (capsule.CapsuleError, UnicodeError, OSError) as exc:
        raise RecoveryError("cannot verify source checkout") from exc


def _publish_directory(staging: Path, destination: Path) -> None:
    """Publish an owned directory without replacing even an empty destination."""
    if staging.parent != destination.parent:
        raise RecoveryError("source staging directory is outside its destination")
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise RecoveryError("atomic no-replace directory publication is unavailable") from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise RecoveryError("source publication destination already exists")
        raise RecoveryError("atomic no-replace directory publication failed")
    capsule.sync_directory(destination.parent)


def _sync_reused_publication(path: Path, verify: Any) -> Any:
    """Durably accept an already-visible publication without changing it."""
    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    before = path.lstat()
    checked = verify()
    capsule.sync_directory(path.parent)
    after = path.lstat()
    verified = verify()
    final = path.lstat()
    if identity(before) != identity(after) or identity(after) != identity(final) or verified != checked:
        raise RecoveryError("reused source publication changed during directory sync")
    return checked


def _verify_bound_compose(root: Path, source: dict[str, Any]) -> Path:
    _validate_source(source)
    if "compose" not in source:
        raise RecoveryError("saved source has no bound Compose executable")
    record = source["compose"]
    path = root / record["path"]
    expected_receipt = (record["sha256"] + "  " + path.name + "\n").encode("ascii")
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o100
                or info.st_mode & 0o022 or _hash(path) != record["sha256"]
                or _read_regular(Path(str(path) + ".sha256")) != expected_receipt):
            raise RecoveryError("saved Compose executable differs")
    except OSError as exc:
        raise RecoveryError("saved Compose executable is unavailable") from exc
    return path


def _requested_compose(root: Path, source: dict[str, Any], requested: Path) -> Path:
    """Check the operator's input but execute only the source-bound copy."""
    saved = _verify_bound_compose(root, source)
    try:
        requested = Path(requested)
        if (not requested.is_absolute() or requested.resolve(strict=True) != requested
                or not requested.is_file() or not os.access(requested, os.X_OK)
                or _hash(requested) != source["compose"]["sha256"]):
            raise RecoveryError("requested Compose executable differs from saved identity")
    except OSError as exc:
        raise RecoveryError("requested Compose executable cannot be verified") from exc
    return saved


def _prepare_compose(root: Path, source: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    # The explicit operator choice and its receipt are verified while normal
    # source admission is held. Recovery uses only the resulting saved bytes.
    import component_transaction as transaction  # noqa: PLC0415
    binary = transaction._compose_binary(compose_path)
    digest = _hash(binary)
    destination = root / ".deployments" / ("component-compose-" + digest)
    receipt = Path(str(destination) + ".sha256")
    receipt_bytes = (digest + "  " + destination.name + "\n").encode("ascii")
    if receipt.exists() or receipt.is_symlink():
        if _read_regular(receipt) != receipt_bytes:
            raise RecoveryError("existing saved Compose receipt differs")
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o100
                or info.st_mode & 0o022 or _hash(destination) != digest):
            raise RecoveryError("existing saved Compose executable differs")
    if not destination.exists() and not destination.is_symlink():
        stage = destination.parent / (".component-compose-stage-" + digest + "-" + secrets.token_hex(16))
        source_fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            initial = os.fstat(source_fd)
            if not stat.S_ISREG(initial.st_mode) or not initial.st_mode & 0o111:
                raise RecoveryError("Compose executable is not a regular executable")
            target_fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500)
            with os.fdopen(target_fd, "wb") as target, os.fdopen(os.dup(source_fd), "rb") as origin:
                while data := origin.read(1024 * 1024):
                    target.write(data)
                target.flush()
                os.fsync(target.fileno())
            final = os.fstat(source_fd)
            named = binary.stat(follow_symlinks=False)
            def identity(item: os.stat_result) -> tuple[int, ...]:
                return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns
            if identity(initial) != identity(final) or identity(final) != identity(named) or _hash(stage) != digest:
                raise RecoveryError("Compose executable changed during preservation")
        finally:
            os.close(source_fd)
        _publish_directory(stage, destination)
    if not receipt.exists() and not receipt.is_symlink():
        stage = receipt.parent / (".component-compose-receipt-stage-" + digest + "-" + secrets.token_hex(16))
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "wb") as stream:
            stream.write(receipt_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_directory(stage, receipt)
    result = {**source, "compose": {"path": str(destination.relative_to(root)), "sha256": digest}}
    _sync_reused_publication(destination, lambda: _verify_bound_compose(root, result))
    _sync_reused_publication(receipt, lambda: _verify_bound_compose(root, result))
    transaction._compose_binary(binary)
    if _hash(binary) != digest:
        raise RecoveryError("Compose executable changed during preservation")
    return result


def prepare_source(root: Path, commit: str, tree: str, *, compose_path: Path | None = None) -> dict[str, Any]:
    """Durably preserve the complete clean source and return journal fields.

    A new or reused capsule is verified before returning. The caller owns FD9
    and must write the prepared journal only after this function succeeds.
    """
    root = _root(root)
    if root != CODE_ROOT:
        raise RecoveryError("source and deployment roots differ")
    lock = _lock(root)
    _load_packager(root / "scripts" / "package_recovery_source.py")
    # These imports are from the normal source tree only. The bootstrap path
    # never calls this function and never imports deployment-root modules.
    sys.path.insert(0, str(root / "scripts"))
    import deployment_admission as admission  # noqa: PLC0415
    import component_runtime as runtime  # noqa: PLC0415
    try:
        admission.admit_component(root, operation="prepare-source", code_root=root)
        files = _verify_clean_source(root, commit, tree)
        if not set(runtime.TOOL_PATHS).issubset(files):
            raise RecoveryError("component toolset is not fully tracked")
        tool_hash = runtime.tool_bytes_identity(root)
        identity = _id(commit, tree, tool_hash)
        if runtime.source_tool_identity(commit, tree, tool_hash) != identity:
            raise RecoveryError("source identity algorithm differs")
        file_hashes = {name: files["scripts/" + name]["sha256"] for name in BOOTSTRAP_FILES}
        bootstrap = {"path": f".deployments/component-bootstrap-{identity}",
                     "files_sha256": file_hashes, "sha256": _bootstrap_id(file_hashes)}
        source_dir = root / f".deployments/component-source-{identity}"
        bootstrap_dir = root / bootstrap["path"]
        # Never replace an existing, incomplete or unknown directory.
        if bootstrap_dir.exists() or bootstrap_dir.is_symlink():
            _sync_reused_publication(bootstrap_dir, lambda: _verify_bootstrap(root, {"bootstrap": bootstrap}))
        if source_dir.exists() or source_dir.is_symlink():
            if not stat.S_ISDIR(source_dir.lstat().st_mode):
                raise RecoveryError("existing source capsule is not a directory")
            _sync_reused_publication(source_dir, lambda: capsule.verify(source_dir, commit, allow_backup=True))
        else:
            # The unchanged packager creates its output before writing all
            # files. Keep interrupted work at a unique owned staging name so
            # an incomplete directory cannot occupy the immutable final name.
            stage = source_dir.parent / (".component-source-stage-" + identity + "-" + secrets.token_hex(16))
            capsule.package(root, commit, stage, legacy=False, lock=lock)
            packaged = capsule.verify(stage, commit, allow_backup=True)
            if packaged["source_tree"] != tree:
                raise RecoveryError("staged source tree differs")
            _publish_directory(stage, source_dir)
        record = {"path": str(source_dir.relative_to(root)),
                  "manifest_sha256": _hash(source_dir / "manifest.json"),
                  "archive_sha256": _hash(source_dir / "source.tar"),
                  "bundle_sha256": _hash(source_dir / "source.bundle")}
        source = {"commit": commit, "tree": tree, "source_tool_sha256": identity,
                  "tool_bytes_sha256": tool_hash, "capsule": record, "bootstrap": bootstrap}
        _validate_source(source)
        _verify_capsule(root, source)
        if not bootstrap_dir.exists():
            stage = bootstrap_dir.parent / (".component-bootstrap-stage-" + identity + "-" + secrets.token_hex(16))
            stage.mkdir(mode=0o700)
            for name in BOOTSTRAP_FILES:
                data = _read_regular(root / "scripts" / name)
                if hashlib.sha256(data).hexdigest() != file_hashes[name]:
                    raise RecoveryError("bootstrap source changed")
                fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            capsule.sync_directory(stage)
            _publish_directory(stage, bootstrap_dir)
        _verify_bootstrap(root, source)
        with tempfile.TemporaryDirectory(prefix="wuwaterm-component-source-") as temporary:
            extracted = Path(temporary)
            _verify_capsule(root, source, extracted)
            _verify_tools(extracted, source, lock)
        _verify_clean_source(root, commit, tree)
        if compose_path is not None:
            source = _prepare_compose(root, source, compose_path)
            _verify_clean_source(root, commit, tree)
        return source
    except (admission.AdmissionError, capsule.CapsuleError, runtime.JournalError, OSError, KeyError) as exc:
        raise RecoveryError("cannot preserve component recovery source") from exc


def _journal(root: Path, transaction_id: str) -> tuple[bytes, dict[str, Any]]:
    if type(transaction_id) is not str or not DEPLOYMENT_ID.fullmatch(transaction_id):
        raise RecoveryError("invalid transaction ID")
    raw = _read_regular(root / ".deployments" / JOURNAL_NAME)
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError("duplicate journal key")
            result[key] = value
        return result
    try:
        journal = json.loads(raw, object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("invalid JSON number")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("invalid journal") from exc
    if (type(journal) is not dict or journal.get("schema_version") not in RUNTIME_SCHEMA_VERSIONS
            or type(journal.get("schema_version")) is not int or journal.get("kind") != "component-runtime"
            or journal.get("transaction_id") != transaction_id
            or journal.get("status") not in ("active", "unresolved", "committed", "rolled_back")):
        raise RecoveryError("journal does not match the requested transaction")
    _validate_source(journal.get("source"))
    return raw, journal


def _full_journal(root: Path, transaction_id: str) -> tuple[bytes, dict[str, Any]]:
    if type(transaction_id) is not str or not DEPLOYMENT_ID.fullmatch(transaction_id):
        raise RecoveryError("invalid full transaction ID")
    raw = _read_regular(root / ".deployments" / FULL_JOURNAL_NAME)
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError("duplicate full journal key")
            result[key] = value
        return result
    try:
        journal = json.loads(raw, object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("invalid JSON number")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("invalid full journal") from exc
    if (type(journal) is not dict or type(journal.get("schema_version")) is not int
            or journal["schema_version"] not in FULL_SCHEMA_VERSIONS or journal.get("kind") != "component-full"
            or journal.get("transaction_id") != transaction_id
            or journal.get("status") not in ("active", "unresolved", "committed", "rolled_back")):
        raise RecoveryError("full journal does not match the requested transaction")
    _validate_source(journal.get("source"))
    return raw, journal


def _probe(root: Path, probe_id: str) -> tuple[bytes, dict[str, Any]]:
    if type(probe_id) is not str or not DEPLOYMENT_ID.fullmatch(probe_id):
        raise RecoveryError("invalid probe ID")
    raw = _read_regular(root / ".deployments" / "component-adopt-probes" / (probe_id + ".json"))
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError("duplicate probe key")
            result[key] = value
        return result
    try:
        record = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("invalid JSON number")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("invalid probe") from exc
    if (type(record) is not dict or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1 or record.get("kind") != "legacy-core-version"
            or record.get("probe_id") != probe_id or record.get("status") not in ("active", "failed", "completed")):
        raise RecoveryError("record does not match the requested version probe")
    _validate_source(record.get("source"))
    return raw, record


def _build_attempt(root: Path, kind: str, build_id: str) -> tuple[bytes, dict[str, Any]]:
    """Select a source-bound build attempt without importing deployment code."""
    if kind not in ("source", "wheel") or type(build_id) is not str or not DEPLOYMENT_ID.fullmatch(build_id):
        raise RecoveryError("invalid builder kind or ID")
    directory = "component-build-attempts" if kind == "source" else "component-wheel-build-attempts"
    raw = _read_regular(root / ".deployments" / directory / (build_id + ".json"))
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError("duplicate build attempt key")
            result[key] = value
        return result
    try:
        record = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("invalid JSON number")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("invalid build attempt") from exc
    if (type(record) is not dict or type(record.get("schema_version")) is not int
            or record["schema_version"] != 2 or record.get("build_id") != build_id
            or record.get("status") not in ("active", "failed", "retained_import", "registered")):
        raise RecoveryError("record does not match a recoverable builder attempt")
    _validate_source(record.get("recovery_source"))
    return raw, record


def run_build_recovery(root: Path, kind: str, build_id: str) -> int:
    """Verify saved A before asking its builder supervisor for cancellation."""
    root = _root(root)
    raw, record = _build_attempt(root, kind, build_id)
    source = record["recovery_source"]
    expected_script = root / source["bootstrap"]["path"] / "component_recovery.py"
    if Path(__file__).absolute() != expected_script:
        raise RecoveryError("recovery must execute from the recorded bootstrap")
    _verify_bootstrap(root, source)
    _load_packager(expected_script.parent / "package_recovery_source.py")
    with tempfile.TemporaryDirectory(prefix="wuwaterm-component-build-recovery-") as temporary:
        extracted = Path(temporary)
        _verify_capsule(root, source, extracted)
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
             "--root", str(root), "validate-build-attempt", "--kind", kind, "--build-id", build_id],
            cwd=extracted, env=_isolated_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RecoveryError("saved source rejected the complete build attempt")
        # Producer attempts exist only under protocol 1; protocol 2 has no
        # producer and must not interpret them.
        protocol = _verify_tools(extracted, source, None, require_recovery_protocol=True,
                                 protocols=frozenset({1}))
        _verify_capsule(root, source)
        if _build_attempt(root, kind, build_id)[0] != raw:
            raise RecoveryError("build attempt changed during recovery preflight")
        _verify_bootstrap(root, source)
        if not protocol_ready(protocol, full=False):
            raise RecoveryError("component recovery execution is not yet enabled")
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
             "--root", str(root), "capsule-cancel-build", "--kind", kind, "--build-id", build_id,
             "--capsule-source-tool", source["source_tool_sha256"]],
            cwd=extracted, env=_isolated_env(), check=False,
        )
        return process.returncode


def run_probe_recovery(root: Path, probe_id: str, *, compose_path: Path) -> int:
    root = _root(root)
    lock = _lock(root)
    raw, record = _probe(root, probe_id)
    source = record["source"]
    expected_script = root / source["bootstrap"]["path"] / "component_recovery.py"
    if Path(__file__).absolute() != expected_script:
        raise RecoveryError("recovery must execute from the recorded bootstrap")
    _verify_bootstrap(root, source)
    _load_packager(expected_script.parent / "package_recovery_source.py")
    with tempfile.TemporaryDirectory(prefix="wuwaterm-component-recovery-") as temporary:
        extracted = Path(temporary)
        _verify_capsule(root, source, extracted)
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
             "--root", str(root), "validate-probe", "--probe-id", probe_id],
            cwd=extracted, env=_isolated_env(), pass_fds=lock.pass_fds(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RecoveryError("saved source rejected the complete probe")
        protocol = _verify_tools(extracted, source, lock, require_recovery_protocol=True)
        _verify_capsule(root, source)
        if _probe(root, probe_id)[0] != raw:
            raise RecoveryError("probe changed during recovery preflight")
        _verify_bootstrap(root, source)
        if not protocol_ready(protocol, full=False):
            raise RecoveryError("component recovery execution is not yet enabled")
        saved_compose = _requested_compose(root, source, compose_path)
        if _probe(root, probe_id)[0] != raw:
            raise RecoveryError("probe changed before recovery execution")
        _verify_bootstrap(root, source)
        _verify_bound_compose(root, source)
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
             "--root", str(root), "capsule-recover-probe", "--probe-id", probe_id,
             "--capsule-source-tool", source["source_tool_sha256"],
             "--compose-executable", str(saved_compose)],
            cwd=extracted, env=_isolated_env(),
            pass_fds=lock.pass_fds(), check=False,
        )
        return process.returncode


def run_recovery(root: Path, transaction_id: str, *, kind: str = "runtime",
                 compose_path: Path | None = None) -> int:
    """Verify bootstrap and source A, then invoke A against real root B.

    The caller must have acquired the root's FD9. The A coordinator must
    validate the entire journal, admission conflicts and actual live objects.
    """
    root = _root(root)
    lock = _lock(root)
    raw, journal = _journal(root, transaction_id)
    if kind not in ("runtime", "adopt") or (kind == "adopt") != (journal.get("action") == "adopt"):
        raise RecoveryError("record kind does not match the requested transaction")
    source = journal["source"]
    expected_script = root / source["bootstrap"]["path"] / "component_recovery.py"
    if Path(__file__).absolute() != expected_script:
        raise RecoveryError("recovery must execute from the recorded bootstrap")
    _verify_bootstrap(root, source)
    _load_packager(expected_script.parent / "package_recovery_source.py")
    with tempfile.TemporaryDirectory(prefix="wuwaterm-component-recovery-") as temporary:
        extracted = Path(temporary)
        _verify_capsule(root, source, extracted)
        _verify_journal(root, extracted, lock)
        protocol = _verify_tools(extracted, source, lock, require_recovery_protocol=True)
        _verify_capsule(root, source)
        if _journal(root, transaction_id)[0] != raw:
            raise RecoveryError("journal changed during recovery preflight")
        _verify_bootstrap(root, source)
        if not protocol_ready(protocol, full=False):
            raise RecoveryError("component recovery execution is not yet enabled")
        if compose_path is None:
            raise RecoveryError("a matching Compose executable is required")
        saved_compose = _requested_compose(root, source, compose_path)
        if _journal(root, transaction_id)[0] != raw:
            raise RecoveryError("journal changed before recovery execution")
        _verify_bootstrap(root, source)
        _verify_bound_compose(root, source)
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts" / "component_runtime.py"),
             "--root", str(root), "capsule-recover", "--transaction-id", transaction_id,
             "--capsule-source-tool", source["source_tool_sha256"],
             "--compose-executable", str(saved_compose)],
            cwd=extracted, env=_isolated_env(),
            pass_fds=lock.pass_fds(), check=False,
        )
        return process.returncode


def run_full_recovery(root: Path, transaction_id: str, *, compose_path: Path) -> int:
    """Dispatch an explicitly selected FULL3 transaction to its saved A."""
    root = _root(root)
    lock = _lock(root)
    raw, journal = _full_journal(root, transaction_id)
    source = journal["source"]
    expected_script = root / source["bootstrap"]["path"] / "component_recovery.py"
    if Path(__file__).absolute() != expected_script:
        raise RecoveryError("recovery must execute from the recorded bootstrap")
    _verify_bootstrap(root, source)
    _load_packager(expected_script.parent / "package_recovery_source.py")
    with tempfile.TemporaryDirectory(prefix="wuwaterm-full-recovery-") as temporary:
        extracted = Path(temporary)
        _verify_capsule(root, source, extracted)
        parsed = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts/component_runtime.py"),
             "validate-full-journal", "--journal", str(root / ".deployments" / FULL_JOURNAL_NAME)],
            cwd=extracted, env=_isolated_env(), pass_fds=lock.pass_fds(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if parsed.returncode:
            raise RecoveryError("saved source rejected the complete full journal")
        protocol = _verify_tools(extracted, source, lock, require_full_protocol=True)
        _verify_capsule(root, source)
        if _full_journal(root, transaction_id)[0] != raw:
            raise RecoveryError("full journal changed during recovery preflight")
        _verify_bootstrap(root, source)
        if not protocol_ready(protocol, full=True):
            raise RecoveryError("full recovery execution is not yet enabled")
        saved_compose = _requested_compose(root, source, compose_path)
        if _full_journal(root, transaction_id)[0] != raw:
            raise RecoveryError("full journal changed before recovery execution")
        _verify_bootstrap(root, source)
        _verify_bound_compose(root, source)
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(extracted / "scripts/component_runtime.py"),
             "--root", str(root), "capsule-recover-full", "--transaction-id", transaction_id,
             "--capsule-source-tool", source["source_tool_sha256"], "--compose-executable", str(saved_compose)],
            cwd=extracted, env=_isolated_env(), pass_fds=lock.pass_fds(), check=False,
        )
        return process.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--kind", choices=("runtime", "adopt", "full", "versionprobe", "sourcebuild", "wheelbuild"),
                        default="runtime")
    ids = parser.add_mutually_exclusive_group(required=True)
    ids.add_argument("--id")
    ids.add_argument("--transaction-id")
    parser.add_argument("--compose-executable", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.kind in ("sourcebuild", "wheelbuild"):
            return run_build_recovery(args.root, "source" if args.kind == "sourcebuild" else "wheel",
                                      args.id or args.transaction_id)
        _acquire_launcher_lock(args.root)
        if args.kind == "full":
            if args.compose_executable is None:
                raise RecoveryError("a matching Compose executable is required")
            return run_full_recovery(args.root, args.id or args.transaction_id, compose_path=args.compose_executable)
        if args.kind == "versionprobe":
            if args.compose_executable is None:
                raise RecoveryError("a matching Compose executable is required")
            return run_probe_recovery(args.root, args.id or args.transaction_id, compose_path=args.compose_executable)
        return run_recovery(args.root, args.id or args.transaction_id, kind=args.kind,
                            compose_path=args.compose_executable)
    except (RecoveryError, OSError, ValueError) as exc:
        print(f"component recovery refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
