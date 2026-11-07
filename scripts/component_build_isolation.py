#!/usr/bin/env python3
"""Shared, fail-closed primitives for isolated component image producers.

This module does not enable either producer.  A caller may import the archive
only after its per-attempt worker has been stopped and reconciled.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import ctypes
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import select
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, BinaryIO, Callable, Iterator


class IsolationError(RuntimeError):
    """The isolated producer cannot prove its inputs or resource boundary."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INDEX = "application/vnd.oci.image.index.v1+json"
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_CONFIG = "application/vnd.oci.image.config.v1+json"
_GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
_WORKER_DIGEST = "sha256:97719ccb2ac86fb44fd36f330a6d302f63dddd5326f3ada24f9c05ccac0d9098"
_WORKER_IMAGE = "moby/buildkit@" + _WORKER_DIGEST
_CHUNK = 1024 * 1024
_MAX_JSON = 16 * 1024 * 1024
# Linux UAPI values; some supported CPython distributions omit their names.
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008
_MEMFD_FLAGS = 0x0001 | 0x0002  # MFD_CLOEXEC | MFD_ALLOW_SEALING
SUPPORTED_PHASES = frozenset({
    "prepared", "context-intent", "context", "source-intent", "source-ready", "wheel-source", "wheel-prepared",
    "wheel-solve-intent", "wheel-solved", "image-context", "image-exported", "image-observed", "probe-verified",
    "containment-intent", "containment-bound", "worker-create-intent", "worker-created", "worker-start-intent",
    "worker-bound", "worker-ready", "work-release-intent", "exhaustion-witness", "archive-intent", "archive-sealed",
    "import-intent", "image-verified", "probe-control-intent", "probe-create-intent", "probe-created", "probe-start-intent",
    "probe-bound", "probe-release-intent", "probe-verified-output", "cleanup", "tag-intent", "tagged", "receipt-intent",
    "receipt-reconcile", "archive-reconcile", "reconcile", "registered", "failed", "retained-import",
})


def _memfd() -> int:
    if sys.platform != "linux":
        raise IsolationError("OCI sealing requires Linux memfd")
    if hasattr(os, "memfd_create"):
        return os.memfd_create("wuwaterm-oci", _MEMFD_FLAGS)
    # The official Python API can be absent even with a capable Linux kernel.
    # This invokes the same glibc API and does not weaken or bypass the seals.
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "memfd_create", None)
    if function is None:
        raise IsolationError("Linux memfd_create is unavailable")
    function.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    result = function(b"wuwaterm-oci", _MEMFD_FLAGS)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def check_sealing_capability() -> None:
    """Check the actual kernel sealing capability before creating an attempt."""
    fd = -1
    try:
        fd = _memfd()
        fcntl.fcntl(fd, _F_ADD_SEALS, _SEALS)
        if fcntl.fcntl(fd, _F_GET_SEALS) & _SEALS != _SEALS:
            raise IsolationError("OCI memfd seals are unavailable")
    except OSError as exc:
        raise IsolationError("OCI memfd seals are unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise IsolationError("duplicate OCI JSON key")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    def reject_constant(_: str) -> None:
        raise IsolationError("nonfinite OCI JSON number")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject_constant)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise IsolationError("invalid OCI JSON") from exc


def _path(value: str) -> str:
    if (not value or value.startswith("/") or "\\" in value or "\x00" in value
            or posixpath.normpath(value) != value or any(x in ("", ".", "..") for x in value.split("/"))):
        raise IsolationError("unsafe OCI archive path")
    return value


def _descriptor(value: Any, media_types: set[str]) -> tuple[str, int]:
    if (type(value) is not dict or set(value) != {"mediaType", "digest", "size"}
            or value["mediaType"] not in media_types or type(value["digest"]) is not str
            or not _DIGEST.fullmatch(value["digest"]) or type(value["size"]) is not int
            or value["size"] < 0):
        raise IsolationError("invalid or external OCI descriptor")
    return value["digest"], value["size"]


class _HashingReader:
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.digest.update(data)
        return data


def _layer(archive: tarfile.TarFile, member: tarfile.TarInfo, expected_diff: str) -> str:
    """Hash the uncompressed tar and check names, links, entry types and modes."""
    if type(expected_diff) is not str or not _DIGEST.fullmatch(expected_diff):
        raise IsolationError("invalid layer diff ID")
    source = archive.extractfile(member)
    if source is None:
        raise IsolationError("layer blob is unavailable")
    inventory = hashlib.sha256()
    names: set[str] = set()
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
            reader = _HashingReader(compressed)
            with tarfile.open(fileobj=reader, mode="r|", bufsize=512) as layer:
                for entry in layer:
                    name = entry.name.rstrip("/")
                    while name.startswith("./"):
                        name = name[2:]
                    if name in ("", ".") and entry.isdir():
                        continue
                    _path(name)
                    if name in names:
                        raise IsolationError("duplicate layer path")
                    names.add(name)
                    if entry.issparse() or not (entry.isfile() or entry.isdir() or entry.issym() or entry.islnk()):
                        raise IsolationError("unsafe layer entry type")
                    if (type(entry.mode) is not int or entry.mode < 0 or entry.mode > 0o7777
                            or type(entry.uid) is not int or entry.uid < 0
                            or type(entry.gid) is not int or entry.gid < 0):
                        raise IsolationError("invalid layer entry mode or owner")
                    if entry.issym() or entry.islnk():
                        target = entry.linkname
                        if not target or "\\" in target or "\x00" in target:
                            raise IsolationError("unsafe layer link")
                        if target.startswith("/"):
                            if entry.islnk():
                                raise IsolationError("absolute layer hardlink")
                            # Absolute symlinks are relative to the image root;
                            # accept only a canonical target with no traversal.
                            resolved = target[1:]
                            _path(resolved)
                        else:
                            base = posixpath.dirname(name) if entry.issym() else ""
                            resolved = posixpath.normpath(posixpath.join(base, target))
                        if resolved in ("", ".", "..") or resolved.startswith("../"):
                            raise IsolationError("layer link escapes root")
                        _path(resolved)
                    item = [name, entry.type.decode("latin1"), entry.mode, entry.uid, entry.gid,
                            entry.size, entry.linkname]
                    inventory.update(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            while tail := reader.read(_CHUNK):
                if any(tail):
                    raise IsolationError("unexpected data after layer tar")
            actual = "sha256:" + reader.digest.hexdigest()
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise IsolationError("invalid compressed layer") from exc
    if actual != expected_diff:
        raise IsolationError("layer diff ID differs")
    return inventory.hexdigest()


def validate_oci_archive(stream: BinaryIO) -> dict[str, Any]:
    """Validate one nameless linux/amd64 OCI graph from a seekable stream.

    The stream supplied to Docker must be this same immutable byte sequence.
    `sealed_oci_input` provides that property for local callers.
    """
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            entries: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                name = _path(member.name.rstrip("/"))
                if name in entries or not (member.isfile() or member.isdir()):
                    raise IsolationError("duplicate or unsafe OCI archive entry")
                if member.isdir() and name not in ("blobs", "blobs/sha256"):
                    raise IsolationError("unexpected OCI archive directory")
                if type(member.mode) is not int or member.mode < 0 or member.mode > 0o7777:
                    raise IsolationError("invalid OCI archive mode")
                entries[name] = member
            stream.seek(archive.offset)
            trailer = stream.read(1024)
            if len(trailer) != 1024 or any(trailer):
                raise IsolationError("OCI archive trailer is missing")
            while tail := stream.read(_CHUNK):
                if any(tail):
                    raise IsolationError("unexpected data after OCI archive")

            def read(name: str, limit: int = _MAX_JSON) -> bytes:
                member = entries.get(name)
                if member is None or not member.isfile() or member.size > limit:
                    raise IsolationError("OCI entry missing or oversized")
                file = archive.extractfile(member)
                if file is None:
                    raise IsolationError("OCI entry unavailable")
                data = file.read(limit + 1)
                if len(data) != member.size:
                    raise IsolationError("OCI entry truncated")
                return data

            if _json(read("oci-layout", 1024)) != {"imageLayoutVersion": "1.0.0"}:
                raise IsolationError("unsupported OCI layout")
            index = _json(read("index.json"))
            if (type(index) is not dict or set(index) != {"schemaVersion", "mediaType", "manifests"}
                    or type(index["schemaVersion"]) is not int or index["schemaVersion"] != 2
                    or index["mediaType"] != _INDEX or type(index["manifests"]) is not list
                    or len(index["manifests"]) != 1):
                raise IsolationError("OCI index must contain one image and no subject")
            desc = index["manifests"][0]
            if (type(desc) is not dict or set(desc) - {"mediaType", "digest", "size", "platform", "annotations"}
                    or desc.get("platform") != {"os": "linux", "architecture": "amd64"}):
                raise IsolationError("unexpected OCI image platform or descriptor")
            annotations = desc.get("annotations", {})
            if type(annotations) is not dict or set(annotations) - {"org.opencontainers.image.created"}:
                raise IsolationError("named, tagged or annotated OCI image")
            if any(type(v) is not str for v in annotations.values()):
                raise IsolationError("invalid OCI annotation")
            references: set[str] = set()

            def blob(descriptor: Any, media: set[str]) -> tuple[tarfile.TarInfo, str]:
                digest, size = _descriptor(descriptor, media)
                name = "blobs/sha256/" + digest[7:]
                member = entries.get(name)
                if member is None or not member.isfile() or member.size != size:
                    raise IsolationError("OCI blob missing or size differs")
                file = archive.extractfile(member)
                if file is None:
                    raise IsolationError("OCI blob unavailable")
                sha = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = file.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise IsolationError("OCI blob truncated")
                    sha.update(chunk)
                    remaining -= len(chunk)
                if "sha256:" + sha.hexdigest() != digest:
                    raise IsolationError("OCI blob digest differs")
                references.add(name)
                return member, digest

            manifest_member, manifest_id = blob({k: desc.get(k) for k in ("mediaType", "digest", "size")}, {_MANIFEST})
            image = _json(read(manifest_member.name))
            if (type(image) is not dict or set(image) != {"schemaVersion", "mediaType", "config", "layers"}
                    or type(image["schemaVersion"]) is not int or image["schemaVersion"] != 2
                    or image["mediaType"] != _MANIFEST):
                raise IsolationError("invalid OCI manifest or subject")
            config_member, config_id = blob(image["config"], {_CONFIG})
            config = _json(read(config_member.name))
            if (type(config) is not dict or config.get("os") != "linux" or config.get("architecture") != "amd64"
                    or any(k in config for k in ("name", "tag", "subject", "referrers", "urls"))):
                raise IsolationError("invalid OCI config platform or references")
            rootfs = config.get("rootfs")
            layers = image["layers"]
            if (type(rootfs) is not dict or rootfs.get("type") != "layers"
                    or type(rootfs.get("diff_ids")) is not list or type(layers) is not list
                    or not layers or len(layers) != len(rootfs["diff_ids"])):
                raise IsolationError("invalid OCI layers")
            layer_ids, inventories = [], []
            for descriptor, diff in zip(layers, rootfs["diff_ids"], strict=True):
                member, digest = blob(descriptor, {_GZIP_LAYER})
                layer_ids.append(digest)
                inventories.append(_layer(archive, member, diff))
            if set(entries) != {"oci-layout", "index.json", "blobs", "blobs/sha256"} | references:
                raise IsolationError("unreferenced OCI archive content")
            return {"manifest_digest": manifest_id, "config_digest": config_id,
                    "layer_digests": layer_ids, "diff_ids": rootfs["diff_ids"],
                    "layer_inventories_sha256": inventories,
                    "platform": {"os": "linux", "architecture": "amd64"},
                    "config": config.get("config", {})}
    except (OSError, EOFError, tarfile.TarError, KeyError, TypeError, ValueError) as exc:
        raise IsolationError("invalid OCI archive") from exc


@contextmanager
def sealed_oci_input(path: Path, *, expected_sha256: str | None = None) -> Iterator[tuple[int, dict[str, Any]]]:
    """Copy and validate bytes in a sealed memfd suitable for an exact Docker load."""
    source = -1
    memfd = -1
    try:
        try:
            source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise IsolationError("OCI archive is not a single-link regular file")
            memfd = _memfd()
            digest = hashlib.sha256()
            while True:
                block = os.read(source, _CHUNK)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(memfd, view)
                    if written <= 0:
                        raise IsolationError("cannot write sealed OCI memfd")
                    view = view[written:]
            actual = digest.hexdigest()
            if expected_sha256 is not None and actual != expected_sha256:
                raise IsolationError("sealed OCI archive digest differs")
            fcntl.fcntl(memfd, _F_ADD_SEALS, _SEALS)
            if fcntl.fcntl(memfd, _F_GET_SEALS) & _SEALS != _SEALS:
                raise IsolationError("OCI memfd sealing failed")
            with os.fdopen(os.dup(memfd), "rb") as stream:
                proof = validate_oci_archive(stream)
            os.lseek(memfd, 0, os.SEEK_SET)
        except OSError as exc:
            raise IsolationError("cannot seal OCI archive") from exc
        yield memfd, {**proof, "archive_sha256": actual}
    finally:
        if memfd >= 0:
            os.close(memfd)
        if source >= 0:
            os.close(source)


def publish_oci_archive(source: Path, destination: Path) -> dict[str, Any]:
    """Persist the verified memfd bytes to a new attempt-owned archive.

    The caller must persist ownership and destination intent first.  A partial
    file after an interrupted write remains for that attempt's reconciliation.
    The mode is a guard against accidents, not a substitute for memfd seals.
    """
    destination = Path(os.path.abspath(destination))
    if destination.name in ("", ".", "..") or destination.parent.resolve(strict=True) != destination.parent:
        raise IsolationError("invalid OCI archive destination")
    with sealed_oci_input(source) as (memfd, proof):
        directory = -1
        output = -1
        try:
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            output = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
            digest = hashlib.sha256()
            while True:
                block = os.read(memfd, _CHUNK)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(output, view)
                    if written <= 0:
                        raise IsolationError("cannot persist OCI archive")
                    view = view[written:]
            if digest.hexdigest() != proof["archive_sha256"]:
                raise IsolationError("persisted OCI archive differs")
            os.fchmod(output, 0o444)
            os.fsync(output)
            os.fsync(directory)
        except OSError as exc:
            raise IsolationError("cannot persist OCI archive") from exc
        finally:
            if output >= 0:
                os.close(output)
            if directory >= 0:
                os.close(directory)
        return proof


def verify_imported_image(daemon: dict[str, Any], expected_daemon: dict[str, Any],
                          image: dict[str, Any], proof: dict[str, Any]) -> str:
    """Bind an inspected image to its OCI graph on the supported Docker store.

    The caller must obtain `daemon` and `image` from the same explicit local
    daemon.  This verifies content, not exclusive ownership of the image or
    any tags other attempts may already have attached to it.
    """
    keys = {"id", "server_version", "os", "architecture", "driver_type"}
    if (type(daemon) is not dict or set(daemon) != keys or daemon != expected_daemon
            or type(daemon["id"]) is not str or not daemon["id"]
            or daemon["server_version"] != "29.1.3" or daemon["os"] != "linux"
            or daemon["architecture"] != "x86_64"
            or daemon["driver_type"] != "io.containerd.snapshotter.v1"):
        raise IsolationError("Docker daemon or image store is unsupported or changed")
    if (type(proof) is not dict or type(proof.get("manifest_digest")) is not str
            or not _DIGEST.fullmatch(proof["manifest_digest"])
            or proof.get("platform") != {"os": "linux", "architecture": "amd64"}
            or type(proof.get("diff_ids")) is not list or type(proof.get("config")) is not dict):
        raise IsolationError("invalid sealed OCI image proof")
    if (type(image) is not dict or image.get("Id") != proof["manifest_digest"]
            or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or type(image.get("RootFS")) is not dict or image["RootFS"].get("Type") != "layers"
            or image["RootFS"].get("Layers") != proof["diff_ids"]
            or image.get("Config") != proof["config"]):
        raise IsolationError("imported image differs from sealed OCI graph")
    return proof["manifest_digest"]


def verify_image_revision(proof: dict[str, Any], commit: str) -> None:
    """Check the source revision in the validated archive's image config."""
    if type(commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", commit) or type(proof) is not dict:
        raise IsolationError("invalid expected image revision")
    config = proof.get("config")
    labels = config.get("Labels") if type(config) is dict else None
    if type(labels) is not dict or labels.get("org.opencontainers.image.revision") != commit:
        raise IsolationError("sealed image revision differs")


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise IsolationError("cannot identify the host boot") from exc
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise IsolationError("invalid host boot identity")
    return value


def check_client_capability() -> None:
    """Require pidfd fencing before any active build attempt is created."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise IsolationError("pidfd client fencing is unavailable")
    try:
        fd = os.pidfd_open(os.getpid(), 0)
        try:
            signal.pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
    except OSError as exc:
        raise IsolationError("pidfd client fencing is unavailable") from exc


def preflight(root: Path) -> dict[str, Any]:
    """Check supported local capabilities without creating an attempt."""
    if os.geteuid() != 0:
        raise IsolationError("isolated builds require one authorized root operator")
    lock = _fd9(root)
    check_sealing_capability()
    check_client_capability()
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C", "DOCKER_CONFIG": "/dev/null"}
    version = "{{json .ID}}|{{json .ServerVersion}}|{{json .OSType}}|{{json .Architecture}}|" \
              "{{json .DriverStatus}}|{{json .CgroupVersion}}|{{json .CgroupDriver}}|{{json .SecurityOptions}}"
    image_format = "{{json .Id}}|{{json .Os}}|{{json .Architecture}}|{{json .Config.Volumes}}|{{json .RepoDigests}}"
    docker = ["/usr/bin/docker", "--host", "unix:///run/docker.sock"]
    try:
        info = subprocess.run(docker + ["info", "--format", version], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20, check=False)
        image = subprocess.run(docker + ["image", "inspect", "--format", image_format, _WORKER_IMAGE], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        systemd = subprocess.run(["/usr/bin/busctl", "--system", "get-property", "org.freedesktop.systemd1",
                                  "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "Version"],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        if info.returncode or image.returncode or systemd.returncode or not systemd.stdout.startswith(b's "'):
            raise IsolationError("Docker, pinned worker image, or systemd is unavailable")
        daemon_id, server, operating_system, architecture, drivers, cgroup_version, cgroup_driver, security = (
            json.loads(value) for value in info.stdout.decode("utf-8").strip().split("|")
        )
        image_id, image_os, image_arch, volumes, repo_digests = (
            json.loads(value) for value in image.stdout.decode("utf-8").strip().split("|")
        )
        if (type(drivers) is not list or any(type(item) is not list or len(item) != 2 for item in drivers)
                or len(dict(drivers)) != len(drivers) or type(security) is not list
                or any(type(item) is not str for item in security)):
            raise IsolationError("Docker capabilities are malformed")
        daemon = {"id": daemon_id, "server_version": server, "os": operating_system,
                  "architecture": architecture, "driver_type": dict(drivers).get("driver-type")}
        if (type(daemon_id) is not str or not daemon_id or server != "29.1.3" or operating_system != "linux"
                or architecture != "x86_64" or daemon["driver_type"] != "io.containerd.snapshotter.v1"
                or cgroup_version != "2" or cgroup_driver != "systemd"
                or any(item.startswith("name=rootless") for item in security)
                or image_id != _WORKER_DIGEST or image_os != "linux" or image_arch != "amd64"
                or volumes != {"/var/lib/buildkit": {}} or type(repo_digests) is not list
                or _WORKER_IMAGE not in repo_digests):
            raise IsolationError("Docker store, cgroup, or pinned worker is unsupported")
        binaries = {name: _file_sha256(Path("/usr/bin") / name) for name in ("docker", "busctl", "systemctl")}
        return {"daemon": daemon, "boot_id": _boot_id(), "lock": lock, "worker_image": _WORKER_IMAGE,
                "worker_image_id": image_id, "worker_platform": "linux/amd64", "binaries_sha256": binaries}
    except (OSError, UnicodeError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise IsolationError("cannot preflight isolated build capabilities") from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _argv_sha256(argv: list[str]) -> str:
    if (type(argv) is not list or not argv
            or any(type(item) is not str or "\x00" in item for item in argv)):
        raise IsolationError("invalid client arguments")
    return hashlib.sha256(b"".join(os.fsencode(item) + b"\0" for item in argv)).hexdigest()


def _fd9(root: Path) -> dict[str, int]:
    root = Path(os.path.abspath(root))
    try:
        if root.resolve(strict=True) != root:
            raise IsolationError("deployment root is not physical")
        directory = root / ".deployments"
        if not stat.S_ISDIR(directory.lstat().st_mode) or directory.resolve(strict=True) != directory:
            raise IsolationError("deployment metadata directory is unsafe")
        path = directory / ".deployment.lock"
        expected = path.stat(follow_symlinks=False)
        actual = os.fstat(9)
        if (not stat.S_ISREG(expected.st_mode) or not stat.S_ISREG(actual.st_mode)
                or expected.st_nlink != 1 or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
                or os.readlink("/proc/self/fd/9") != str(path)):
            raise IsolationError("FD9 does not match the deployment lock")
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return {"device": actual.st_dev, "inode": actual.st_ino}
    except OSError as exc:
        raise IsolationError("deployment lock FD9 is unavailable") from exc


def parent_name(build_id: str) -> str:
    """Derive one systemd slice name from a validated build attempt ID."""
    match = re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-([0-9a-f]{32})", build_id) if type(build_id) is str else None
    if match is None:
        raise IsolationError("invalid build attempt ID")
    return "wuwatermbuild" + match.group(1) + ".slice"


def parent_create_argv(name: str) -> list[str]:
    if not re.fullmatch(r"wuwatermbuild[0-9a-f]{32}\.slice", name):
        raise IsolationError("invalid build containment name")
    return ["/usr/bin/busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
            name, "fail", "2", "Description", "s", "WuwaTerm isolated build",
            "StopWhenUnneeded", "b", "false", "0"]


def parent_snapshot(name: str, *, description: str = "WuwaTerm isolated build") -> dict[str, Any]:
    """Read a transient slice and its persistent cgroup-v2 events identity."""
    if type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9]{0,100}\.slice", name):
        raise IsolationError("invalid containment slice name")
    names = ("Id", "LoadState", "ActiveState", "ControlGroup", "InvocationID", "StopWhenUnneeded", "Transient",
             "Description", "Job")
    argv = ["/usr/bin/systemctl", "show", name, *("--property=" + key for key in names)]
    try:
        result = subprocess.run(argv, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        if result.returncode:
            raise IsolationError("cannot inspect containment slice")
        properties: dict[str, str] = {}
        for line in result.stdout.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in properties:
                raise IsolationError("invalid containment slice properties")
            properties[key] = value
        if (set(properties) != set(names) or properties["Id"] != name or properties["LoadState"] != "loaded"
                or properties["ActiveState"] != "active" or properties["ControlGroup"] != "/" + name
                or properties["StopWhenUnneeded"] != "no" or properties["Transient"] != "yes"
                or properties["Description"] != description
                or properties["Job"]
                or not re.fullmatch(r"[0-9a-f]{32}", properties["InvocationID"])):
            raise IsolationError("containment slice identity or configuration differs")
        directory = Path("/sys/fs/cgroup") / name
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            event_fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                event_info = os.fstat(event_fd)
                raw = os.read(event_fd, 4097)
            finally:
                os.close(event_fd)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            raise IsolationError("containment events are oversized")
        values: dict[str, str] = {}
        for line in raw.decode("ascii").splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] in values:
                raise IsolationError("invalid containment events")
            values[parts[0]] = parts[1]
        if values.get("populated") not in ("0", "1"):
            raise IsolationError("containment population is unknown")
        return {"name": name, "description": description, "boot_id": _boot_id(),
                "invocation_id": properties["InvocationID"],
                "control_group": properties["ControlGroup"],
                "directory": {"device": info.st_dev, "inode": info.st_ino},
                "events": {"device": event_info.st_dev, "inode": event_info.st_ino},
                "populated": int(values["populated"])}
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise IsolationError("cannot observe containment slice") from exc


def parent_absent(name: str) -> bool:
    if type(name) is not str or not re.fullmatch(r"wuwatermbuild[0-9a-f]{32}\.slice", name):
        raise IsolationError("invalid build containment name")
    argv = ["/usr/bin/systemctl", "show", name, "--property=Id", "--property=LoadState",
            "--property=ActiveState", "--property=ControlGroup", "--property=Job", "--property=Transient",
            "--property=InvocationID", "--property=FragmentPath", "--property=UnitFileState"]
    try:
        result = subprocess.run(argv, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        if result.returncode:
            raise IsolationError("containment state is unknown")
        values: dict[str, str] = {}
        for line in result.stdout.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise IsolationError("containment state is malformed")
            values[key] = value
        keys = {"Id", "LoadState", "ActiveState", "ControlGroup", "Job", "Transient", "InvocationID",
                "FragmentPath", "UnitFileState"}
        if set(values) != keys or values["Id"] != name:
            raise IsolationError("containment state is malformed")
        if (values["LoadState"] in ("not-found", "loaded") and values["ActiveState"] == "inactive"
                and values["Transient"] == "no" and not any(values[key] for key in
                    ("ControlGroup", "Job", "InvocationID", "FragmentPath", "UnitFileState"))
                and not os.path.lexists(Path("/sys/fs/cgroup") / name)):
            return True
        return False
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise IsolationError("containment state is unknown") from exc


def verify_parent_snapshot(expected: dict[str, Any], observed: dict[str, Any], *, populated: int) -> None:
    keys = {"name", "description", "boot_id", "invocation_id", "control_group", "directory", "events", "populated"}
    if (type(expected) is not dict or type(observed) is not dict or set(expected) != keys or set(observed) != keys
            or populated not in (0, 1) or observed["populated"] != populated
            or any(expected[key] != observed[key] for key in keys - {"populated"})):
        raise IsolationError("containment parent identity or population differs")


def worker_membership(parent: dict[str, Any], pid: int) -> dict[str, Any]:
    """Observe a running worker init in private PID/cgroup namespaces."""
    if type(parent) is not dict or type(pid) is not int or pid <= 1:
        raise IsolationError("invalid worker containment identity")
    try:
        proc = Path("/proc") / str(pid)
        raw = (proc / "cgroup").read_text(encoding="ascii").strip().splitlines()
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        namespaces = {kind: os.readlink(proc / "ns" / kind) for kind in ("pid", "cgroup")}
        host = {kind: os.readlink(Path("/proc/self/ns") / kind) for kind in ("pid", "cgroup")}
        if (len(raw) != 1 or not raw[0].startswith("0::" + parent["control_group"] + "/")
                or len(fields) <= 19 or namespaces["pid"] == host["pid"]
                or namespaces["cgroup"] == host["cgroup"]):
            raise IsolationError("worker is outside its private process boundary")
        return {"pid": pid, "start_ticks": int(fields[19]), "cgroup": raw[0][3:],
                "namespaces": namespaces, "host_namespaces": host}
    except (OSError, KeyError, ValueError) as exc:
        raise IsolationError("cannot verify worker process boundary") from exc


def worker_spec(root: Path, kind: str, build_id: str) -> dict[str, Any]:
    """Bind a worker recipe to existing, attempt-owned physical directories."""
    root = Path(os.path.abspath(root))
    unit = parent_name(build_id)
    if kind not in ("source", "wheel"):
        raise IsolationError("invalid build kind")
    prefix = "component-build-context-" if kind == "source" else "component-wheel-build-context-"
    context = root / ".deployments" / (prefix + build_id)
    if root.resolve(strict=True) != root or context.resolve(strict=True) != context:
        raise IsolationError("worker context is not a physical attempt directory")
    mounts = []
    for source, target, writable in ((context, "/work/context", False),
                                     (context / "worker-output", "/work/output", True),
                                     (context / "worker-cache", "/var/lib/buildkit", True)):
        try:
            info = source.lstat()
            if (source.resolve(strict=True) != source or not stat.S_ISDIR(info.st_mode)
                    or any(c in str(source) for c in (",", "\n", "\r"))):
                raise IsolationError("worker mount is unsafe")
        except OSError as exc:
            raise IsolationError("worker mount is unavailable") from exc
        mounts.append({"source": str(source), "target": target, "writable": writable,
                       "device": info.st_dev, "inode": info.st_ino})
    name = "wuwaterm-" + kind + "-worker-" + build_id
    command = ["--addr", "unix:///run/buildkit/buildkitd.sock", "--root", "/var/lib/buildkit",
               "--oci-worker=true", "--containerd-worker=false"]
    return {"kind": kind, "build_id": build_id, "name": name, "slice": unit,
            "image": _WORKER_IMAGE, "image_id": _WORKER_DIGEST, "network": "bridge",
            "entrypoint": ["/usr/bin/buildkitd"], "command": command, "mounts": mounts}


def worker_create_argv(spec: dict[str, Any]) -> list[str]:
    root = Path(spec["mounts"][0]["source"]).parents[1]
    if worker_spec(root, spec["kind"], spec["build_id"]) != spec:
        raise IsolationError("worker specification changed")
    return _worker_create_argv(spec)


def _worker_create_argv(spec: dict[str, Any]) -> list[str]:
    argv = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "container", "create", "--pull=never",
            "--name", spec["name"], "--label", "org.wuwaterm.build-kind=" + spec["kind"],
            "--label", "org.wuwaterm.build-id=" + spec["build_id"], "--network", spec["network"],
            "--cgroup-parent", spec["slice"], "--cgroupns", "private", "--restart", "no", "--privileged"]
    for mount in spec["mounts"]:
        value = "type=bind,src=" + mount["source"] + ",dst=" + mount["target"]
        if not mount["writable"]:
            value += ",readonly"
        argv.extend(["--mount", value])
    return argv + ["--entrypoint", spec["entrypoint"][0], spec["image"], *spec["command"]]


def verify_worker(spec: dict[str, Any], container: dict[str, Any], *, worker_id: str | None = None,
                  statuses: tuple[str, ...] = ("created", "running", "exited")) -> dict[str, Any]:
    """Verify exact worker identity, recipe and isolation from Docker inspect."""
    try:
        root = Path(spec["mounts"][0]["source"]).parents[1]
        if worker_spec(root, spec["kind"], spec["build_id"]) != spec:
            raise IsolationError("worker mount identity changed")
        identifier = container["Id"]
        host, config, state = container["HostConfig"], container["Config"], container["State"]
        labels, actual_mounts = config["Labels"] or {}, container["Mounts"] or []
        if (type(identifier) is not str or not re.fullmatch(r"[0-9a-f]{64}", identifier)
                or (worker_id is not None and identifier != worker_id)
                or container["Name"] != "/" + spec["name"] or container["Image"] != spec["image_id"]
                or config["Image"] != spec["image"] or config["Entrypoint"] != spec["entrypoint"]
                or config["Cmd"] != spec["command"] or config["User"] != ""
                or config["Volumes"] != {"/var/lib/buildkit": {}}
                or labels.get("org.wuwaterm.build-kind") != spec["kind"]
                or labels.get("org.wuwaterm.build-id") != spec["build_id"]
                or host["NetworkMode"] != spec["network"] or host["CgroupParent"] != spec["slice"]
                or host["PidMode"] not in ("", "private") or host["CgroupnsMode"] != "private"
                or host["Privileged"] is not True or host["RestartPolicy"]["Name"] != "no"
                or host["RestartPolicy"]["MaximumRetryCount"] != 0
                or host["AutoRemove"] is not False or state["Status"] not in statuses
                or len(actual_mounts) != len(spec["mounts"])):
            raise IsolationError("worker identity or isolation differs")
        actual = {(item.get("Source"), item.get("Destination"), item.get("RW"))
                  for item in actual_mounts if item.get("Type") == "bind" and item.get("Propagation") == "rprivate"}
        expected = {(item["source"], item["target"], item["writable"]) for item in spec["mounts"]}
        if actual != expected:
            raise IsolationError("worker mounts differ")
        if state["Status"] == "running":
            if (type(state["Pid"]) is not int or state["Pid"] <= 0 or type(state["StartedAt"]) is not str
                    or not state["StartedAt"] or container["RestartCount"] != 0):
                raise IsolationError("worker start identity differs")
        elif state["Pid"] != 0:
            raise IsolationError("stopped worker still has a PID")
        return {"id": identifier, "status": state["Status"], "pid": state["Pid"],
                "started_at": state["StartedAt"], "restart_count": container["RestartCount"]}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise IsolationError("invalid worker inspection") from exc


_PROBE_GATE = 'while [ ! -f /wuwaterm-probe/GO ]; do sleep 0.1; done; exec python -B -c "$1"'


def probe_spec(root: Path, kind: str, build_id: str, image_id: str, code: str) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    parent_name(build_id)
    if (kind not in ("source", "wheel") or type(image_id) is not str or not _DIGEST.fullmatch(image_id)
            or type(code) is not str or not code or "\x00" in code):
        raise IsolationError("invalid probe specification")
    prefix = "component-build-context-" if kind == "source" else "component-wheel-build-context-"
    control = root / ".deployments" / (prefix + build_id) / "probe-control"
    try:
        info = control.lstat()
        if (control.resolve(strict=True) != control or not stat.S_ISDIR(info.st_mode)
                or any(c in str(control) for c in (",", "\n", "\r"))):
            raise IsolationError("probe control directory is unsafe")
    except OSError as exc:
        raise IsolationError("probe control directory is unavailable") from exc
    return {"kind": kind, "build_id": build_id, "name": "wuwaterm-" + kind + "-probe-" + build_id,
            "slice": parent_name(build_id), "image_id": image_id,
            "control": {"source": str(control), "device": info.st_dev, "inode": info.st_ino},
            "entrypoint": ["/bin/sh"], "command": ["-c", _PROBE_GATE, "--", code]}


def probe_create_argv(spec: dict[str, Any]) -> list[str]:
    root = Path(spec["control"]["source"]).parents[2]
    if probe_spec(root, spec["kind"], spec["build_id"], spec["image_id"], spec["command"][-1]) != spec:
        raise IsolationError("probe specification changed")
    return _probe_create_argv(spec)


def _probe_create_argv(spec: dict[str, Any]) -> list[str]:
    mount = "type=bind,src=" + spec["control"]["source"] + ",dst=/wuwaterm-probe,readonly"
    return ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "container", "create", "--pull=never",
            "--name", spec["name"], "--label", "org.wuwaterm.build-kind=" + spec["kind"],
            "--label", "org.wuwaterm.build-id=" + spec["build_id"], "--label", "org.wuwaterm.build-probe=1",
            "--network", "none", "--cgroup-parent", spec["slice"], "--cgroupns", "private",
            "--restart", "no", "--read-only", "--env", "PYTHONDONTWRITEBYTECODE=1", "--mount", mount,
            "--entrypoint", spec["entrypoint"][0], spec["image_id"], *spec["command"]]


def verify_probe_container(spec: dict[str, Any], container: dict[str, Any], *, probe_id: str | None = None,
                           statuses: tuple[str, ...] = ("created", "running", "exited")) -> dict[str, Any]:
    try:
        root = Path(spec["control"]["source"]).parents[2]
        if probe_spec(root, spec["kind"], spec["build_id"], spec["image_id"], spec["command"][-1]) != spec:
            raise IsolationError("probe control identity changed")
        identifier = container["Id"]
        host, config, state = container["HostConfig"], container["Config"], container["State"]
        labels, mounts = config["Labels"] or {}, container["Mounts"] or []
        if (type(identifier) is not str or not re.fullmatch(r"[0-9a-f]{64}", identifier)
                or (probe_id is not None and identifier != probe_id) or container["Name"] != "/" + spec["name"]
                or container["Image"] != spec["image_id"] or config["Image"] != spec["image_id"]
                or config["Entrypoint"] != spec["entrypoint"] or config["Cmd"] != spec["command"]
                or config["Volumes"] not in (None, {}) or labels.get("org.wuwaterm.build-kind") != spec["kind"]
                or labels.get("org.wuwaterm.build-id") != spec["build_id"]
                or labels.get("org.wuwaterm.build-probe") != "1" or host["NetworkMode"] != "none"
                or host["CgroupParent"] != spec["slice"] or host["PidMode"] not in ("", "private")
                or host["CgroupnsMode"] != "private" or host["Privileged"] is not False
                or host["RestartPolicy"] != {"Name": "no", "MaximumRetryCount": 0}
                or host["AutoRemove"] is not False or host["ReadonlyRootfs"] is not True
                or state["Status"] not in statuses or len(mounts) != 1):
            raise IsolationError("probe identity or isolation differs")
        environment = config["Env"] or []
        if (type(environment) is not list
                or [value for value in environment if isinstance(value, str) and value.startswith("PYTHONDONTWRITEBYTECODE=")]
                != ["PYTHONDONTWRITEBYTECODE=1"]):
            raise IsolationError("probe bytecode setting differs")
        mount = mounts[0]
        if (mount.get("Type") != "bind" or mount.get("Source") != spec["control"]["source"]
                or mount.get("Destination") != "/wuwaterm-probe" or mount.get("RW") is not False
                or mount.get("Propagation") != "rprivate"):
            raise IsolationError("probe mount differs")
        if state["Status"] == "running":
            if (type(state["Pid"]) is not int or state["Pid"] <= 0 or type(state["StartedAt"]) is not str
                    or not state["StartedAt"] or container["RestartCount"] != 0):
                raise IsolationError("probe start identity differs")
        elif state["Pid"] != 0:
            raise IsolationError("stopped probe still has a PID")
        return {"id": identifier, "status": state["Status"], "pid": state["Pid"],
                "started_at": state["StartedAt"], "restart_count": container["RestartCount"]}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise IsolationError("invalid probe inspection") from exc


def _proc_client(pid: int) -> dict[str, Any]:
    proc = Path("/proc") / str(pid)
    try:
        text = (proc / "stat").read_text(encoding="ascii")
        fields = text[text.rfind(")") + 2:].split()
        if len(fields) <= 19:
            raise IsolationError("invalid client process identity")
        executable = (proc / "exe").resolve(strict=True)
        cmdline = (proc / "cmdline").read_bytes()
        lock = (proc / "fd" / "9").stat()
        return {"pid": pid, "start_ticks": int(fields[19]), "uid": proc.stat().st_uid,
                "state": fields[0], "executable_sha256": _file_sha256(executable),
                "argv_sha256": hashlib.sha256(cmdline).hexdigest(),
                "lock": {"device": lock.st_dev, "inode": lock.st_ino}}
    except (OSError, ValueError) as exc:
        raise IsolationError("cannot verify client process identity") from exc


def _proc_start_ticks(pid: int) -> int:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        fields = text[text.rfind(")") + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError) as exc:
        raise IsolationError("cannot verify process start identity") from exc


def fence_recorded_client(root: Path, client: dict[str, Any], *, timeout: float = 10.0) -> str:
    """Fence a verified client without acquiring FD9 or mutating its record.

    The caller must first validate the saved-A source and the complete attempt.
    A fenced/absent client is not proof that daemon work has stopped.
    """
    check_client_capability()
    keys = {"generation", "operation", "state", "boot_id", "pid", "start_ticks", "uid", "lock",
            "launcher_sha256", "launcher_argv_sha256", "target_sha256", "target_argv_sha256",
            "argv", "environment", "stdout_path", "stderr_path", "result_path", "result_sha256", "exit_code",
            "extra_fds"}
    if (type(client) is not dict or set(client) != keys or client["state"] not in ("armed", "release-intent")
            or type(client["pid"]) is not int or client["pid"] <= 0
            or type(client["start_ticks"]) is not int or client["start_ticks"] <= 0
            or type(client["uid"]) is not int or client["uid"] != os.geteuid()
            or type(client["boot_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", client["boot_id"])
            or type(client["lock"]) is not dict or set(client["lock"]) != {"device", "inode"}
            or any(type(client["lock"][key]) is not int or client["lock"][key] < 0 for key in ("device", "inode"))
            or type(client["argv"]) is not list or _argv_sha256(client["argv"]) != client["target_argv_sha256"]
            or type(client["environment"]) is not dict
            or any(type(client[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", client[key])
                   for key in ("launcher_sha256", "launcher_argv_sha256", "target_sha256", "target_argv_sha256"))):
        raise IsolationError("invalid recorded client identity")
    root = Path(os.path.abspath(root))
    try:
        if root.resolve(strict=True) != root:
            raise IsolationError("deployment root is not physical")
        lock_path = root / ".deployments" / ".deployment.lock"
        lock = lock_path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1
                or {"device": lock.st_dev, "inode": lock.st_ino} != client["lock"]):
            raise IsolationError("recorded client lock differs")
    except OSError as exc:
        raise IsolationError("recorded client lock is unavailable") from exc
    if client["boot_id"] != _boot_id():
        return "previous-boot"
    try:
        descriptor = os.pidfd_open(client["pid"], 0)
    except ProcessLookupError:
        return "absent"
    except OSError as exc:
        raise IsolationError("cannot open client pidfd") from exc
    try:
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        if poll.poll(0):
            return "exited"
        try:
            if _proc_start_ticks(client["pid"]) != client["start_ticks"]:
                return "reused-pid"
            actual = _proc_client(client["pid"])
        except IsolationError:
            if poll.poll(0):
                return "exited"
            raise
        if actual["start_ticks"] != client["start_ticks"] or _proc_start_ticks(client["pid"]) != client["start_ticks"]:
            raise IsolationError("client process changed during verification")
        pairs = {(client["launcher_sha256"], client["launcher_argv_sha256"]),
                 (client["target_sha256"], client["target_argv_sha256"])}
        if (actual["uid"] != client["uid"] or actual["lock"] != client["lock"]
                or (actual["executable_sha256"], actual["argv_sha256"]) not in pairs):
            raise IsolationError("recorded client identity changed")
        if os.readlink(f"/proc/{client['pid']}/fd/9") != str(lock_path):
            raise IsolationError("recorded client lock path changed")
        # A pidfd refers to this process even if its numeric PID is later reused.
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not poll.poll(int(timeout * 1000)):
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            if not poll.poll(int(timeout * 1000)):
                raise IsolationError("recorded client did not exit")
        return "fenced"
    except OSError as exc:
        if poll.poll(0):
            return "exited"
        raise IsolationError("cannot fence recorded client") from exc
    finally:
        os.close(descriptor)


def _control_address(kind: str, build_id: str, lock: dict[str, int], nonce: str) -> str:
    body = json.dumps([kind, build_id, lock, nonce], sort_keys=True, separators=(",", ":")).encode("ascii")
    return "wuwaterm-build-" + hashlib.sha256(body).hexdigest()


def _socket_frame(connection: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        block = connection.recv(min(1024, limit + 1 - len(data)))
        if not block:
            raise IsolationError("incomplete build control message")
        data.extend(block)
        if b"\n" in block:
            payload, _, trailing = bytes(data).partition(b"\n")
            if trailing or len(payload) > limit:
                raise IsolationError("invalid build control message")
            return payload
    raise IsolationError("oversized build control message")


def _control_record(root: Path, kind: str, build_id: str, control: dict[str, Any]) -> None:
    keys = {"kind", "build_id", "state", "pid", "start_ticks", "uid", "boot_id", "lock",
            "executable_sha256", "argv_sha256", "nonce", "address"}
    if (kind not in ("source", "wheel") or type(control) is not dict or set(control) != keys
            or control["kind"] != kind or control["build_id"] != build_id
            or control["state"] not in ("intent", "listening")
            or type(control["pid"]) is not int or control["pid"] <= 0
            or type(control["start_ticks"]) is not int or control["start_ticks"] <= 0
            or type(control["uid"]) is not int or control["uid"] != os.geteuid()
            or type(control["boot_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", control["boot_id"])
            or type(control["lock"]) is not dict or set(control["lock"]) != {"device", "inode"}
            or any(type(control["lock"][key]) is not int or control["lock"][key] < 0 for key in ("device", "inode"))
            or any(type(control[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", control[key])
                   for key in ("executable_sha256", "argv_sha256", "nonce"))):
        raise IsolationError("invalid build supervisor identity")
    parent_name(build_id)
    if control["address"] != _control_address(kind, build_id, control["lock"], control["nonce"]):
        raise IsolationError("build control socket identity differs")
    root = Path(os.path.abspath(root))
    try:
        if root.resolve(strict=True) != root:
            raise IsolationError("deployment root is not physical")
        lock_path = root / ".deployments" / ".deployment.lock"
        info = lock_path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or {"device": info.st_dev, "inode": info.st_ino} != control["lock"]):
            raise IsolationError("build supervisor lock differs")
    except OSError as exc:
        raise IsolationError("build supervisor lock is unavailable") from exc


class CancelEndpoint:
    """An attempt-bound abstract socket served by the FD9 lock holder."""

    def __init__(self, root: Path, kind: str, build_id: str,
                 persist_control: Callable[[dict[str, Any]], None], mark_cancel: Callable[[], None]) -> None:
        check_client_capability()
        root = Path(os.path.abspath(root))
        if kind not in ("source", "wheel"):
            raise IsolationError("invalid build kind")
        parent_name(build_id)
        lock = _fd9(root)
        process = _proc_client(os.getpid())
        nonce = secrets.token_hex(32)
        self.control: dict[str, Any] = {"kind": kind, "build_id": build_id, "state": "intent",
                                        "pid": process["pid"], "start_ticks": process["start_ticks"],
                                        "uid": process["uid"], "boot_id": _boot_id(), "lock": lock,
                                        "executable_sha256": process["executable_sha256"],
                                        "argv_sha256": process["argv_sha256"], "nonce": nonce,
                                        "address": _control_address(kind, build_id, lock, nonce)}
        self.cancelled = threading.Event()
        self._root = root
        self._closing = threading.Event()
        self._mark_cancel = mark_cancel
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        self._thread: threading.Thread | None = None
        try:
            persist_control(dict(self.control))
            self._socket.bind("\0" + self.control["address"])
            self._socket.listen(4)
            self._socket.settimeout(0.25)
            self.control["state"] = "listening"
            persist_control(dict(self.control))
            self._thread = threading.Thread(target=self._serve, name="wuwaterm-build-cancel", daemon=True)
            self._thread.start()
        except Exception:
            self._socket.close()
            raise

    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(3)
                    peer = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    request = _socket_frame(connection, 1024)
                    if peer[1] != self.control["uid"]:
                        raise IsolationError("invalid cancel peer")
                    decoded = _json(request)
                    if decoded != {"kind": self.control["kind"], "build_id": self.control["build_id"],
                                   "nonce": self.control["nonce"]}:
                        raise IsolationError("cancel request does not match the attempt")
                    if _fd9(self._root) != self.control["lock"]:
                        raise IsolationError("build supervisor lost its lock")
                    # The caller serializes this callback with all other attempt
                    # writes and returns only after durable cancellation intent.
                    if not self.cancelled.is_set():
                        self._mark_cancel()
                        self.cancelled.set()
                    connection.sendall(b"accepted\n")
                except Exception:
                    try:
                        connection.sendall(b"refused\n")
                    except OSError:
                        pass

    def close(self) -> None:
        self._closing.set()
        self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def __enter__(self) -> CancelEndpoint:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def request_live_cancel(root: Path, kind: str, build_id: str, control: dict[str, Any]) -> str:
    """Contact a verified live lock holder, or report an absent owner.

    This read-only pre-lock operation must run from verified saved-A code after
    complete attempt validation. It never edits an attempt or signals a PID.
    """
    check_client_capability()
    _control_record(root, kind, build_id, control)
    if control["boot_id"] != _boot_id():
        return "owner-dead"
    try:
        descriptor = os.pidfd_open(control["pid"], 0)
    except ProcessLookupError:
        return "owner-dead"
    except OSError as exc:
        raise IsolationError("cannot inspect build supervisor") from exc
    try:
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        if poll.poll(0):
            return "owner-dead"
        try:
            if _proc_start_ticks(control["pid"]) != control["start_ticks"]:
                return "owner-dead"
            actual = _proc_client(control["pid"])
        except IsolationError:
            if poll.poll(0):
                return "owner-dead"
            raise
        if (actual["start_ticks"] != control["start_ticks"] or actual["uid"] != control["uid"]
                or actual["lock"] != control["lock"] or actual["executable_sha256"] != control["executable_sha256"]
                or actual["argv_sha256"] != control["argv_sha256"]):
            raise IsolationError("build supervisor identity changed")
        if (os.readlink(f"/proc/{control['pid']}/fd/9") != str(Path(os.path.abspath(root)) / ".deployments/.deployment.lock")
                or _proc_start_ticks(control["pid"]) != control["start_ticks"]):
            raise IsolationError("build supervisor lock or process changed")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as client:
            client.settimeout(5)
            try:
                client.connect("\0" + control["address"])
                peer = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer[:2] != (control["pid"], control["uid"]) or _proc_start_ticks(peer[0]) != control["start_ticks"]:
                    raise IsolationError("build control socket peer differs")
                request = {"kind": kind, "build_id": build_id, "nonce": control["nonce"]}
                client.sendall(json.dumps(request, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
                if _socket_frame(client, 32) != b"accepted":
                    raise IsolationError("build cancellation was not durably accepted")
            except OSError as exc:
                if poll.poll(0):
                    return "owner-dead"
                raise IsolationError("live build supervisor cannot be contacted") from exc
        return "requested"
    finally:
        os.close(descriptor)


def acquire_recovery_fd9(root: Path, *, timeout: float = 5.0) -> dict[str, int]:
    """Acquire the real lock after a verified dead-owner client fence."""
    root = Path(os.path.abspath(root))
    created = False
    try:
        if root.resolve(strict=True) != root:
            raise IsolationError("deployment root is not physical")
        directory = root / ".deployments"
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise IsolationError("deployment metadata directory is unsafe")
        path = directory / ".deployment.lock"
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IsolationError("deployment lock is unsafe")
        try:
            existing = os.fstat(9)
        except OSError:
            existing = None
        if existing is None:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            if descriptor != 9:
                try:
                    os.dup2(descriptor, 9, inheritable=True)
                finally:
                    os.close(descriptor)
            else:
                os.set_inheritable(9, True)
            created = True
        elif ((existing.st_dev, existing.st_ino) != (info.st_dev, info.st_ino)
              or os.readlink("/proc/self/fd/9") != str(path)):
            raise IsolationError("inherited FD9 does not match the deployment lock")
        deadline = time.monotonic() + timeout
        while True:
            try:
                return _fd9(root)
            except IsolationError:
                if time.monotonic() >= deadline:
                    raise IsolationError("deployment lock remains held or mismatched") from None
                time.sleep(0.05)
    except OSError as exc:
        if created:
            os.close(9)
        raise IsolationError("cannot acquire recovery FD9") from exc
    except Exception:
        if created:
            os.close(9)
        raise


def fence_before_recovery(root: Path, kind: str, build_id: str, control: dict[str, Any],
                          clients: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask a live holder or fence exact clients and acquire FD9 for recovery.

    Saved-A must validate the entire attempt before this call and reread it
    under FD9 after a ``locked`` result. No journal write occurs here.
    """
    if type(clients) is not list:
        raise IsolationError("invalid recorded client list")
    requested = request_live_cancel(root, kind, build_id, control)
    if requested == "requested":
        return {"status": "requested", "clients": []}
    results: list[dict[str, Any]] = []
    for client in clients:
        if type(client) is not dict or client.get("state") not in ("planned", "armed", "release-intent", "completed"):
            raise IsolationError("unknown recorded client state")
        if client["state"] in ("armed", "release-intent"):
            results.append({"generation": client["generation"], "result": fence_recorded_client(root, client)})
    lock = acquire_recovery_fd9(root)
    return {"status": "locked", "clients": results, "lock": lock}


def _client_launcher(arguments: list[str]) -> int:
    if len(arguments) < 5:
        return 126
    root = Path(arguments[0])
    try:
        go, ready = int(arguments[1]), int(arguments[2])
        expected, command = arguments[3], arguments[4:]
        _fd9(root)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or not command or not os.path.isabs(command[0]):
            return 126
        target = os.open(command[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(target)
            if not stat.S_ISREG(info.st_mode):
                return 126
            digest = hashlib.sha256()
            while block := os.read(target, _CHUNK):
                digest.update(block)
            if digest.hexdigest() != expected:
                return 126
            os.lseek(target, 0, os.SEEK_SET)
            os.write(ready, b"R")
            os.close(ready)
            if os.read(go, 1) != b"G":
                return 125
            os.close(go)
            os.execve(target, command, os.environ)
        finally:
            os.close(target)
    except (OSError, ValueError, IsolationError):
        return 126
    return 126


def run_guarded_client(root: Path, operation: str, argv: list[str], environment: dict[str, str],
                       directory: Path, generation: int, persist: Callable[[dict[str, Any]], None], *,
                       timeout: float = 1800, cancelled: Callable[[], bool] | None = None,
                       extra_fds: tuple[int, ...] = (), release_lock: Any = None) -> dict[str, Any]:
    """Run one client only after its durable identity and release intent.

    `persist` must durably store the supplied complete client record inside
    the active attempt. The caller owns cancellation reconciliation; this
    helper's client exit alone never establishes worker or daemon quiescence.
    """
    check_client_capability()
    root = Path(os.path.abspath(root))
    lock = _fd9(root)
    _argv_sha256(argv)
    if (type(operation) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", operation)
            or type(generation) is not int or generation < 1 or timeout <= 0
            or type(environment) is not dict
            or any(type(k) is not str or type(v) is not str or "\x00" in v for k, v in environment.items())
            or set(environment) - {"PATH", "LANG", "LC_ALL", "DOCKER_CONFIG", "DOCKER_BUILDKIT",
                                   "SYSTEMD_COLORS", "SYSTEMD_PAGER", "SYSTEMD_LOG_LEVEL",
                                   "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_NO_REPLACE_OBJECTS",
                                   "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "GIT_NO_LAZY_FETCH", "SUDO_UID"}):
        raise IsolationError("invalid client execution parameters")
    directory = Path(os.path.abspath(directory))
    try:
        if (directory.resolve(strict=True) != directory or not directory.is_relative_to(root / ".deployments")
                or not stat.S_ISDIR(directory.lstat().st_mode)):
            raise IsolationError("client output directory is not attempt-owned")
        executable = Path(argv[0])
        if not executable.is_absolute() or executable.resolve(strict=True) != executable:
            raise IsolationError("client executable must be a physical absolute path")
        target_hash = _file_sha256(executable)
        launcher = Path(sys.executable).resolve(strict=True)
        launcher_hash = _file_sha256(launcher)
        script = Path(__file__).resolve(strict=True)
        extra = []
        for descriptor in extra_fds:
            if type(descriptor) is not int or descriptor < 3 or descriptor == 9:
                raise IsolationError("invalid additional client descriptor")
            info = os.fstat(descriptor)
            extra.append({"fd": descriptor, "device": info.st_dev, "inode": info.st_ino})
        if len({item["fd"] for item in extra}) != len(extra):
            raise IsolationError("duplicate additional client descriptor")
    except OSError as exc:
        raise IsolationError("cannot verify client execution inputs") from exc
    prefix = f"client-{generation:08d}"
    paths = {name: str((directory / f"{prefix}-{name}").relative_to(root))
             for name in ("stdout", "stderr", "result")}
    state: dict[str, Any] = {"generation": generation, "operation": operation, "state": "planned",
                             "boot_id": _boot_id(), "pid": None, "start_ticks": None, "uid": os.geteuid(),
                             "lock": lock, "launcher_sha256": launcher_hash, "launcher_argv_sha256": None,
                             "target_sha256": target_hash, "target_argv_sha256": _argv_sha256(argv),
                             "argv": list(argv), "environment": dict(environment),
                             "stdout_path": paths["stdout"], "stderr_path": paths["stderr"],
                             "result_path": paths["result"], "result_sha256": None, "exit_code": None,
                             "extra_fds": extra}
    persist(dict(state))
    out = err = go_read = go_write = ready_read = ready_write = -1
    process: subprocess.Popen[bytes] | None = None
    released = False
    try:
        out = os.open(root / paths["stdout"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        err = os.open(root / paths["stderr"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        go_read, go_write = os.pipe2(os.O_CLOEXEC)
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        launch = [str(launcher), "-I", "-B", str(script), "--_client-barrier", str(root),
                  str(go_read), str(ready_write), target_hash, *argv]
        state["launcher_argv_sha256"] = _argv_sha256(launch)
        process = subprocess.Popen(launch, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   env=environment, pass_fds=(9, go_read, ready_write, *extra_fds))
        os.close(go_read)
        go_read = -1
        os.close(ready_write)
        ready_write = -1
        if not select.select([ready_read], [], [], 10)[0] or os.read(ready_read, 1) != b"R":
            raise IsolationError("client barrier did not become ready")
        actual = _proc_client(process.pid)
        if (actual["uid"] != state["uid"] or actual["lock"] != lock
                or actual["executable_sha256"] != launcher_hash
                or actual["argv_sha256"] != state["launcher_argv_sha256"]):
            raise IsolationError("armed client identity differs")
        state.update(state="armed", pid=process.pid, start_ticks=actual["start_ticks"])
        persist(dict(state))
        with release_lock if release_lock is not None else nullcontext():
            if cancelled is not None and cancelled():
                raise IsolationError("producer cancellation preceded client release")
            state["state"] = "release-intent"
            persist(dict(state))
            if cancelled is not None and cancelled():
                raise IsolationError("producer cancellation preceded client release")
            os.write(go_write, b"G")
            released = True
        os.close(go_write)
        go_write = -1
        deadline = time.monotonic() + timeout
        outcome = "exited"
        while process.poll() is None:
            if cancelled is not None and cancelled():
                outcome = "cancelled"
                fence_recorded_client(root, state)
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                fence_recorded_client(root, state)
                break
            time.sleep(0.05)
        result_code = process.wait(timeout=10)
        os.fsync(out)
        os.fsync(err)
        result = {"generation": generation, "operation": operation, "exit_code": result_code,
                  "outcome": outcome, "stdout_sha256": _file_sha256(root / paths["stdout"]),
                  "stderr_sha256": _file_sha256(root / paths["stderr"])}
        raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        result_fd = os.open(root / paths["result"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(result_fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        state.update(state="completed", result_sha256=hashlib.sha256(raw).hexdigest(), exit_code=result_code)
        persist(dict(state))
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        raise IsolationError("client execution is unresolved") from exc
    finally:
        for descriptor in (go_read, go_write, ready_read, ready_write, out, err):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None:
            if released and process.poll() is None:
                fence_recorded_client(root, state)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise IsolationError("client exit is unresolved") from exc


def new_ledger(kind: str, build_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Create the initial versioned ledger after read-only preflight."""
    if kind not in ("source", "wheel"):
        raise IsolationError("invalid producer kind")
    unit = parent_name(build_id)
    if (type(binding) is not dict or set(binding) != {"daemon", "boot_id", "lock", "worker_image",
                                                      "worker_image_id", "worker_platform", "binaries_sha256"}
            or binding["worker_image"] != _WORKER_IMAGE or binding["worker_image_id"] != _WORKER_DIGEST
            or binding["worker_platform"] != "linux/amd64" or binding["boot_id"] != _boot_id()):
        raise IsolationError("invalid producer capability binding")
    return {"schema_version": 2, "kind": kind, "build_id": build_id, "binding": binding,
            "cancel_intent": None, "control": None, "clients": [], "client_fences": [],
            "parent": {"name": unit, "create_intent": False, "create_outcome": "not_attempted", "snapshot": None,
                       "stop_intent": False, "absent": False},
            "worker": {"spec": None, "create_intent": False, "create_outcome": "not_attempted", "id": None, "bound": None,
                       "buildkit": None,
                       "stop_intent": False, "remove_intent": False, "absent": False},
            "probe": None, "archive": None, "import": None, "retained_reference": None,
            "worker_witness": None, "witness": None, "context": None, "cache": None, "output": None, "probe_control": None,
            "receipt": None}


def validate_ledger(root: Path, kind: str, build_id: str, ledger: Any) -> dict[str, Any]:
    """Validate every persisted isolation field without executing its commands."""
    root = Path(os.path.abspath(root))
    keys = {"schema_version", "kind", "build_id", "binding", "cancel_intent", "control", "clients", "client_fences",
            "parent", "worker", "probe", "archive", "import", "retained_reference", "worker_witness", "witness",
            "context", "cache", "output", "probe_control", "receipt"}
    if (type(ledger) is not dict or set(ledger) != keys or type(ledger["schema_version"]) is not int
            or ledger["schema_version"] not in (1, 2) or kind not in ("source", "wheel")
            or ledger["kind"] != kind or ledger["build_id"] != build_id):
        raise IsolationError("invalid producer isolation ledger")
    unit = parent_name(build_id)
    prefix = "component-build-context-" if kind == "source" else "component-wheel-build-context-"
    context = root / ".deployments" / (prefix + build_id)
    binding = ledger["binding"]
    if (type(binding) is not dict or set(binding) != {"daemon", "boot_id", "lock", "worker_image",
                                                      "worker_image_id", "worker_platform", "binaries_sha256"}
            or binding["worker_image"] != _WORKER_IMAGE or binding["worker_image_id"] != _WORKER_DIGEST
            or binding["worker_platform"] != "linux/amd64" or type(binding["boot_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", binding["boot_id"])):
        raise IsolationError("invalid producer capability binding")
    daemon, lock, binaries = binding["daemon"], binding["lock"], binding["binaries_sha256"]
    if (type(daemon) is not dict or set(daemon) != {"id", "server_version", "os", "architecture", "driver_type"}
            or type(daemon["id"]) is not str or not daemon["id"] or daemon["server_version"] != "29.1.3"
            or daemon["os"] != "linux" or daemon["architecture"] != "x86_64"
            or daemon["driver_type"] != "io.containerd.snapshotter.v1"
            or type(lock) is not dict or set(lock) != {"device", "inode"}
            or any(type(lock[key]) is not int or lock[key] < 0 for key in lock)
            or type(binaries) is not dict or set(binaries) != {"docker", "busctl", "systemctl"}
            or any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value) for value in binaries.values())):
        raise IsolationError("invalid producer daemon, lock, or tools")
    if ledger["cancel_intent"] is not None and (type(ledger["cancel_intent"]) is not str
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", ledger["cancel_intent"])):
        raise IsolationError("invalid producer cancellation intent")
    control = ledger["control"]
    if control is not None:
        control_keys = {"kind", "build_id", "state", "pid", "start_ticks", "uid", "boot_id", "lock",
                        "executable_sha256", "argv_sha256", "nonce", "address"}
        if (type(control) is not dict or set(control) != control_keys or control["kind"] != kind
                or control["build_id"] != build_id or control["state"] not in ("intent", "listening")
                or type(control["pid"]) is not int or control["pid"] <= 0
                or type(control["uid"]) is not int or control["uid"] != 0
                or type(control["start_ticks"]) is not int or control["start_ticks"] <= 0
                or any(type(control[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", control[key])
                       for key in ("executable_sha256", "argv_sha256", "nonce"))
                or control["address"] != _control_address(kind, build_id, lock, control["nonce"])
                or control["boot_id"] != binding["boot_id"] or control["lock"] != lock):
            raise IsolationError("producer supervisor binding differs")
    clients = ledger["clients"]
    client_keys = {"generation", "operation", "state", "boot_id", "pid", "start_ticks", "uid", "lock",
                   "launcher_sha256", "launcher_argv_sha256", "target_sha256", "target_argv_sha256", "argv",
                   "environment", "stdout_path", "stderr_path", "result_path", "result_sha256", "exit_code",
                   "extra_fds"}
    if type(clients) is not list:
        raise IsolationError("invalid producer client list")
    for index, client in enumerate(clients, 1):
        if (type(client) is not dict or set(client) != client_keys or type(client["generation"]) is not int
                or client["generation"] != index or type(client["operation"]) is not str
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", client["operation"])
                or client["state"] not in ("planned", "armed", "release-intent", "completed")
                or client["boot_id"] != binding["boot_id"] or type(client["uid"]) is not int
                or client["uid"] != 0 or client["lock"] != lock
                or type(client["argv"]) is not list or _argv_sha256(client["argv"]) != client["target_argv_sha256"]
                or client["argv"][0] not in ("/usr/bin/docker", "/usr/bin/busctl", "/usr/bin/systemctl", "/usr/bin/git")
                or type(client["environment"]) is not dict or type(client["extra_fds"]) is not list):
            raise IsolationError("invalid producer client binding")
        allowed_environment = {"PATH", "LANG", "LC_ALL", "DOCKER_CONFIG", "DOCKER_BUILDKIT", "SYSTEMD_COLORS",
                               "SYSTEMD_PAGER", "SYSTEMD_LOG_LEVEL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                               "GIT_NO_REPLACE_OBJECTS", "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT",
                               "GIT_NO_LAZY_FETCH", "SUDO_UID"}
        if (set(client["environment"]) - allowed_environment
                or any(type(k) is not str or type(v) is not str or "\x00" in v
                       for k, v in client["environment"].items())):
            raise IsolationError("invalid producer client environment")
        for name in ("launcher_sha256", "target_sha256", "target_argv_sha256"):
            if type(client[name]) is not str or not re.fullmatch(r"[0-9a-f]{64}", client[name]):
                raise IsolationError("invalid producer client digest")
        if client["state"] == "planned":
            if client["pid"] is not None or client["start_ticks"] is not None or client["launcher_argv_sha256"] is not None:
                raise IsolationError("planned client has a process identity")
        elif (type(client["pid"]) is not int or client["pid"] <= 0 or type(client["start_ticks"]) is not int
              or client["start_ticks"] <= 0 or type(client["launcher_argv_sha256"]) is not str
              or not re.fullmatch(r"[0-9a-f]{64}", client["launcher_argv_sha256"])):
            raise IsolationError("invalid armed client identity")
        if client["state"] == "completed":
            if (type(client["exit_code"]) is not int or type(client["result_sha256"]) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", client["result_sha256"])):
                raise IsolationError("completed client lacks a result")
        elif client["exit_code"] is not None or client["result_sha256"] is not None:
            raise IsolationError("incomplete client has a result")
        for part in ("stdout", "stderr", "result"):
            expected_path = context / "clients" / f"client-{index:08d}-{part}"
            if client[part + "_path"] != str(expected_path.relative_to(root)):
                raise IsolationError("producer client output path differs")
        descriptors = client["extra_fds"]
        if any(type(item) is not dict or set(item) != {"fd", "device", "inode"}
               or any(type(item[key]) is not int or item[key] < 0 for key in item)
               or item["fd"] < 3 or item["fd"] == 9 for item in descriptors):
            raise IsolationError("invalid producer client descriptors")
        if len({item["fd"] for item in descriptors}) != len(descriptors):
            raise IsolationError("duplicate producer client descriptors")
    fences = ledger["client_fences"]
    if type(fences) is not list:
        raise IsolationError("invalid producer client fences")
    generations: set[int] = set()
    for fence in fences:
        if (type(fence) is not dict or set(fence) != {"generation", "result"}
                or type(fence["generation"]) is not int or not 1 <= fence["generation"] <= len(clients)
                or fence["generation"] in generations
                or fence["result"] not in ("not-released", "fenced", "exited", "absent", "previous-boot", "reused-pid")
                or clients[fence["generation"] - 1]["state"] not in ("planned", "armed", "release-intent")):
            raise IsolationError("invalid producer client fence")
        if fence["result"] == "not-released" and clients[fence["generation"] - 1]["state"] != "planned":
            raise IsolationError("released client cannot be marked not-released")
        generations.add(fence["generation"])
    parent = ledger["parent"]
    parent_keys = {"name", "create_intent", "create_outcome", "snapshot", "stop_intent", "absent"}
    if (type(parent) is not dict or set(parent) != parent_keys or parent["name"] != unit
            or any(type(parent[key]) is not bool for key in ("create_intent", "stop_intent", "absent"))
            or parent["create_outcome"] not in ("not_attempted", "unknown", "not_released", "bound")):
        raise IsolationError("invalid producer parent state")
    if ((not parent["create_intent"]) != (parent["create_outcome"] == "not_attempted")
            or (parent["create_outcome"] == "bound") != (parent["snapshot"] is not None)
            or (parent["create_outcome"] == "not_released" and not parent["absent"])
            or (parent["absent"] and parent["create_outcome"] == "bound" and not parent["stop_intent"])):
        raise IsolationError("inconsistent producer parent state")
    if parent["snapshot"] is not None:
        snap = parent["snapshot"]
        if (type(snap) is not dict or set(snap) != {"name", "description", "boot_id", "invocation_id",
                                                       "control_group", "directory", "events", "populated"}
                or snap["name"] != unit or snap["description"] != "WuwaTerm isolated build"
                or snap["boot_id"] != binding["boot_id"] or snap["control_group"] != "/" + unit
                or type(snap["invocation_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", snap["invocation_id"])
                or type(snap["populated"]) is not int or snap["populated"] not in (0, 1)):
            raise IsolationError("invalid producer parent snapshot")
        for field in ("directory", "events"):
            value = snap[field]
            if (type(value) is not dict or set(value) != {"device", "inode"}
                    or any(type(value[key]) is not int or value[key] < 0 for key in value)):
                raise IsolationError("invalid producer parent inode")
    worker = ledger["worker"]
    worker_keys = {"spec", "create_intent", "create_outcome", "id", "bound", "buildkit", "stop_intent",
                   "remove_intent", "absent"}
    if (type(worker) is not dict or set(worker) != worker_keys
            or any(type(worker[key]) is not bool for key in ("create_intent", "stop_intent", "remove_intent", "absent"))
            or worker["create_outcome"] not in ("not_attempted", "unknown", "not_released", "bound")):
        raise IsolationError("invalid producer worker state")
    if ((not worker["create_intent"]) != (worker["create_outcome"] == "not_attempted")
            or (worker["create_outcome"] == "bound") != (worker["id"] is not None)
            or (worker["create_outcome"] == "not_released" and not worker["absent"])
            or (worker["absent"] and worker["create_outcome"] == "bound" and not worker["remove_intent"])):
        raise IsolationError("inconsistent producer worker state")
    if worker["id"] is not None and (type(worker["id"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", worker["id"])):
        raise IsolationError("invalid producer worker ID")
    spec = worker["spec"]
    if spec is not None:
        spec_keys = {"kind", "build_id", "name", "slice", "image", "image_id", "network", "entrypoint", "command", "mounts"}
        command = ["--addr", "unix:///run/buildkit/buildkitd.sock", "--root", "/var/lib/buildkit",
                   "--oci-worker=true", "--containerd-worker=false"]
        if (type(spec) is not dict or set(spec) != spec_keys or spec["kind"] != kind or spec["build_id"] != build_id
                or spec["name"] != "wuwaterm-" + kind + "-worker-" + build_id or spec["slice"] != unit
                or spec["image"] != _WORKER_IMAGE or spec["image_id"] != _WORKER_DIGEST or spec["network"] != "bridge"
                or spec["entrypoint"] != ["/usr/bin/buildkitd"] or spec["command"] != command
                or type(spec["mounts"]) is not list or len(spec["mounts"]) != 3):
            raise IsolationError("invalid producer worker recipe")
        expected_mounts = ((context, "/work/context", False), (context / "worker-output", "/work/output", True),
                           (context / "worker-cache", "/var/lib/buildkit", True))
        for mount, (path, target, writable) in zip(spec["mounts"], expected_mounts, strict=True):
            if (type(mount) is not dict or set(mount) != {"source", "target", "writable", "device", "inode"}
                    or mount["source"] != str(path) or mount["target"] != target or mount["writable"] is not writable
                    or any(type(mount[key]) is not int or mount[key] < 0 for key in ("device", "inode"))):
                raise IsolationError("invalid producer worker mount")
    elif worker["create_intent"]:
        raise IsolationError("worker creation lacks its recipe")
    def check_bound(bound: Any, identifier: str | None) -> None:
        if type(bound) is not dict or set(bound) != {"container", "membership"}:
            raise IsolationError("invalid producer process binding")
        container, membership = bound["container"], bound["membership"]
        if (type(container) is not dict or set(container) != {"id", "status", "pid", "started_at", "restart_count"}
                or container["id"] != identifier or container["status"] != "running"
                or type(container["pid"]) is not int or container["pid"] <= 1
                or type(container["restart_count"]) is not int or container["restart_count"] != 0
                or type(container["started_at"]) is not str
                or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", container["started_at"])
                or type(membership) is not dict or set(membership) != {"pid", "start_ticks", "cgroup", "namespaces", "host_namespaces"}
                or membership["pid"] != container["pid"] or type(membership["start_ticks"]) is not int
                or membership["start_ticks"] <= 0 or type(membership["cgroup"]) is not str
                or not membership["cgroup"].startswith("/" + unit + "/")):
            raise IsolationError("invalid producer process binding")
        for field in ("namespaces", "host_namespaces"):
            item = membership[field]
            if (type(item) is not dict or set(item) != {"pid", "cgroup"}
                    or any(type(item[key]) is not str or not re.fullmatch(key + r":\[[0-9]+\]", item[key])
                           for key in ("pid", "cgroup"))):
                raise IsolationError("invalid producer namespace binding")
        if any(membership["namespaces"][key] == membership["host_namespaces"][key] for key in ("pid", "cgroup")):
            raise IsolationError("producer process namespaces are not private")

    if worker["bound"] is not None:
        check_bound(worker["bound"], worker["id"])
    if worker["buildkit"] is not None:
        value = worker["buildkit"]
        allowed_keys = ({"id", "platforms", "version", "revision"},)
        if ledger["schema_version"] == 1:
            allowed_keys += ({"id", "platforms"},)
        if (worker["bound"] is None or type(value) is not dict or set(value) not in allowed_keys
                or type(value["id"]) is not str or not re.fullmatch(r"[a-z0-9]{12,64}", value["id"])
                or type(value["platforms"]) is not list or "linux/amd64" not in value["platforms"]
                or any(type(item) is not str for item in value["platforms"])
                or ("version" in value and (value["version"] != "v0.34.2" or type(value["revision"]) is not str
                                           or not re.fullmatch(r"[0-9a-f]{40}", value["revision"])) )):
            raise IsolationError("invalid BuildKit worker identity")
    for key, name in (("context", None), ("cache", "worker-cache"), ("output", "worker-output"),
                      ("probe_control", "probe-control")):
        value = ledger[key]
        if value is None:
            continue
        expected_path = context if name is None else context / name
        expected_keys = {"path", "create_intent", "device", "inode"}
        if name is not None:
            expected_keys |= {"remove_intent", "absent"}
        if (type(value) is not dict or set(value) != expected_keys or value["path"] != str(expected_path.relative_to(root))
                or value["create_intent"] is not True
                or any(value[key] is not None and (type(value[key]) is not int or value[key] < 0)
                       for key in ("device", "inode"))
                or ((value["device"] is None) != (value["inode"] is None))
                or (name is not None and any(type(value[key]) is not bool for key in ("remove_intent", "absent")))):
            raise IsolationError("invalid producer owned path")
    probe = ledger["probe"]
    if probe is not None:
        probe_keys = {"control_path", "control_intent", "spec", "create_intent", "create_outcome", "id", "bound",
                      "go_intent", "stop_intent", "remove_intent", "absent", "output_sha256"}
        if (type(probe) is not dict or set(probe) != probe_keys or probe["control_intent"] is not True
                or probe["control_path"] != str((context / "probe-control").relative_to(root))
                or ledger["probe_control"] is None
                or any(type(probe[key]) is not bool for key in
                       ("create_intent", "go_intent", "stop_intent", "remove_intent", "absent"))
                or probe["create_outcome"] not in ("not_attempted", "unknown", "not_released", "bound")
                or (not probe["create_intent"]) != (probe["create_outcome"] == "not_attempted")
                or (probe["create_outcome"] == "bound") != (probe["id"] is not None)
                or (probe["create_outcome"] == "not_released" and not probe["absent"])
                or (probe["absent"] and probe["create_outcome"] == "bound" and not probe["remove_intent"])):
            raise IsolationError("invalid producer probe state")
        if probe["id"] is not None and (type(probe["id"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", probe["id"])):
            raise IsolationError("invalid producer probe ID")
        if probe["output_sha256"] is not None and (type(probe["output_sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", probe["output_sha256"]) or not probe["go_intent"]):
            raise IsolationError("invalid producer probe output")
        spec = probe["spec"]
        if spec is not None:
            spec_keys = {"kind", "build_id", "name", "slice", "image_id", "control", "entrypoint", "command"}
            item = ledger["probe_control"]
            if (type(spec) is not dict or set(spec) != spec_keys or spec["kind"] != kind or spec["build_id"] != build_id
                    or spec["name"] != "wuwaterm-" + kind + "-probe-" + build_id or spec["slice"] != unit
                    or type(spec["image_id"]) is not str or not _DIGEST.fullmatch(spec["image_id"])
                    or spec["control"] != {"source": str(context / "probe-control"), "device": item["device"], "inode": item["inode"]}
                    or type(item["device"]) is not int or type(item["inode"]) is not int
                    or spec["entrypoint"] != ["/bin/sh"] or type(spec["command"]) is not list or len(spec["command"]) != 4
                    or spec["command"][:3] != ["-c", _PROBE_GATE, "--"] or type(spec["command"][3]) is not str):
                raise IsolationError("invalid producer probe recipe")
        elif probe["create_intent"]:
            raise IsolationError("probe creation lacks its recipe")
        if probe["bound"] is not None:
            check_bound(probe["bound"], probe["id"])
        if probe["go_intent"] and probe["bound"] is None:
            raise IsolationError("probe work was not bound")
    elif ledger["probe_control"] is not None:
        raise IsolationError("probe control exists without a probe")
    archive = ledger["archive"]
    if archive is not None:
        if ledger["worker_witness"] is None:
            raise IsolationError("producer archive lacks prior worker exhaustion")
        if (type(archive) is not dict or set(archive) != {"path", "sha256", "graph", "state", "remove_intent"}
                or archive["path"] != str((context / "sealed-image.tar").relative_to(root))
                or type(archive["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
                or archive["state"] not in ("intent", "sealed", "discarded") or type(archive["remove_intent"]) is not bool):
            raise IsolationError("invalid producer archive")
        graph = archive["graph"]
        graph_keys = {"manifest_digest", "config_digest", "layer_digests", "diff_ids", "layer_inventories_sha256", "platform"}
        if (type(graph) is not dict or set(graph) != graph_keys or graph["platform"] != {"os": "linux", "architecture": "amd64"}
                or any(type(graph[key]) is not str or not _DIGEST.fullmatch(graph[key])
                       for key in ("manifest_digest", "config_digest"))
                or any(type(graph[key]) is not list for key in ("layer_digests", "diff_ids", "layer_inventories_sha256"))
                or not graph["layer_digests"] or len(graph["layer_digests"]) != len(graph["diff_ids"])
                or len(graph["diff_ids"]) != len(graph["layer_inventories_sha256"])
                or any(type(item) is not str or not _DIGEST.fullmatch(item) for item in graph["layer_digests"] + graph["diff_ids"])
                or any(type(item) is not str or not re.fullmatch(r"[0-9a-f]{64}", item)
                       for item in graph["layer_inventories_sha256"])):
            raise IsolationError("invalid producer OCI graph")
    imported = ledger["import"]
    if imported is not None:
        if (type(imported) is not dict or set(imported) != {"archive_sha256", "released_generation", "native_image_id", "verified"}
                or archive is None or imported["archive_sha256"] != archive["sha256"] or type(imported["verified"]) is not bool):
            raise IsolationError("invalid producer import binding")
        generation = imported["released_generation"]
        if generation is not None and (type(generation) is not int or not 1 <= generation <= len(clients)
                or clients[generation - 1]["operation"] != "image-load"
                or clients[generation - 1]["state"] not in ("release-intent", "completed")):
            raise IsolationError("invalid producer import generation")
        if imported["verified"] and (generation is None or archive["state"] != "sealed"
                or imported["native_image_id"] != archive["graph"]["manifest_digest"]):
            raise IsolationError("invalid verified import")
        if not imported["verified"] and imported["native_image_id"] is not None:
            raise IsolationError("unverified import has an image ID")
    reference = ledger["retained_reference"]
    if reference is not None:
        reference_keys = {"ref", "issued_generation", "archive_sha256", "graph_descriptor_digest",
                          "expected_native_image_id", "allowed_observation", "late_appearance_allowed"}
        expected = ("wuwaterm-component-build:" if kind == "source" else "wuwaterm-wheel-build:") + build_id
        if (type(reference) is not dict or set(reference) != reference_keys or imported is None or not imported["verified"]
                or reference["ref"] != expected or reference["archive_sha256"] != archive["sha256"]
                or reference["graph_descriptor_digest"] != archive["graph"]["manifest_digest"]
                or reference["expected_native_image_id"] != imported["native_image_id"]
                or reference["allowed_observation"] != ["absent", "exact_expected_binding"]
                or reference["late_appearance_allowed"] is not True):
            raise IsolationError("invalid retained producer reference")
        generation = reference["issued_generation"]
        if (type(generation) is not int or not 1 <= generation <= len(clients)
                or clients[generation - 1]["operation"] != "image-tag"
                or clients[generation - 1]["state"] not in ("release-intent", "completed")):
            raise IsolationError("invalid retained reference generation")
    def check_witness(witness: Any, expected_ids: list[str]) -> None:
        if (type(witness) is not dict or set(witness) != {"parent", "deleted_container_ids", "client_generations", "client_fences"}
                or parent["snapshot"] is None or type(witness["deleted_container_ids"]) is not list
                or type(witness["client_generations"]) is not list or type(witness["client_fences"]) is not list):
            raise IsolationError("invalid producer exhaustion witness")
        verify_parent_snapshot(parent["snapshot"], witness["parent"], populated=0)
        generations = witness["client_generations"]
        if (witness["deleted_container_ids"] != expected_ids or generations != list(range(1, len(generations) + 1))
                or len(generations) > len(clients) or (worker["id"] is not None and not worker["absent"])
                or any(item not in fences or item["generation"] > len(generations) for item in witness["client_fences"])):
            raise IsolationError("producer exhaustion witness differs")
        fenced_generations = {item["generation"] for item in witness["client_fences"]}
        for client in clients[:len(generations)]:
            execution = (client["operation"].startswith(("worker-", "probe-"))
                         or client["operation"] in ("image-build", "image-export", "wheel-prepare"))
            if execution and client["state"] != "completed" and client["generation"] not in fenced_generations:
                raise IsolationError("producer exhaustion witness lacks a client fence")
    worker_witness = ledger["worker_witness"]
    if worker_witness is not None:
        if worker["id"] is None:
            raise IsolationError("worker exhaustion lacks a worker ID")
        check_witness(worker_witness, [worker["id"]])
    witness = ledger["witness"]
    if witness is not None:
        if probe is not None and probe["id"] is not None and not probe["absent"]:
            raise IsolationError("producer witness has a live probe")
        expected_ids = [item for item in (worker["id"], None if probe is None else probe["id"]) if item is not None]
        check_witness(witness, expected_ids)
    receipt = ledger["receipt"]
    if receipt is not None:
        if (type(receipt) is not dict or set(receipt) != {"path", "sha256", "state", "remove_intent"}
                or type(receipt["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])
                or receipt["state"] not in ("intent", "revoked", "registered") or type(receipt["remove_intent"]) is not bool
                or receipt["path"] != "/".join((".deployments", "product-builds" if kind == "source" else "product-wheel-builds",
                                                  receipt["sha256"] + ".json"))):
            raise IsolationError("invalid producer receipt intent")
    docker = ["/usr/bin/docker", "--host", "unix:///run/docker.sock"]
    docker_environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C",
                          "DOCKER_CONFIG": str(context / "docker-config")}
    git_options = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
                   "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.allow=never"]
    git_environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
                       "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
    worker_id = worker["id"]
    probe_id = None if probe is None else probe["id"]
    for client in clients:
        operation, argv = client["operation"], client["argv"]
        if argv[0] in ("/usr/bin/docker", "/usr/bin/busctl", "/usr/bin/systemctl"):
            if client["environment"] != docker_environment or client["target_sha256"] != binaries[Path(argv[0]).name]:
                raise IsolationError("producer system client environment or executable differs")
        elif argv[0] == "/usr/bin/git":
            env = dict(client["environment"])
            sudo_uid = env.pop("SUDO_UID", None)
            if (kind != "wheel" or env != git_environment or (sudo_uid is not None and not re.fullmatch(r"[0-9]+", sudo_uid))
                    or len(argv) < 16 or argv[1] != "-C" or not os.path.isabs(argv[2]) or argv[3:15] != git_options):
                raise IsolationError("producer Git client recipe differs")
            tail = argv[15:]
            git_ok = ((operation == "git-root" and tail == ["rev-parse", "--show-toplevel"])
                      or (operation == "git-tree" and len(tail) == 2 and tail[0] == "rev-parse"
                          and re.fullmatch(r"[0-9a-f]{40}\^\{tree\}", tail[1]) is not None)
                      or (operation == "git-ls-tree" and len(tail) == 4 and tail[:3] == ["ls-tree", "-rlz", "--full-tree"]
                          and re.fullmatch(r"[0-9a-f]{40}", tail[3]) is not None)
                      or (operation in ("git-commit", "core-git-commit") and len(tail) == 3
                          and tail[:2] == ["cat-file", "commit"] and re.fullmatch(r"[0-9a-f]{40}", tail[2]) is not None)
                      or (operation == "git-archive" and len(tail) == 3 and tail[:2] == ["archive", "--format=tar"]
                          and re.fullmatch(r"[0-9a-f]{40}", tail[2]) is not None))
            if not git_ok:
                raise IsolationError("producer Git operation is unsupported")
            continue
        exact = None
        if operation == "parent-create":
            if not parent["create_intent"]:
                raise IsolationError("containment creation lacks intent")
            exact = parent_create_argv(unit)
        elif operation == "parent-stop":
            if not parent["stop_intent"] or parent["snapshot"] is None:
                raise IsolationError("containment stop lacks intent")
            exact = ["/usr/bin/systemctl", "stop", unit]
        elif operation == "worker-create" and worker["spec"] is not None:
            if not worker["create_intent"] or parent["snapshot"] is None:
                raise IsolationError("worker creation lacks intent")
            exact = _worker_create_argv(worker["spec"])
        elif operation == "worker-start" and worker_id is not None:
            exact = docker + ["container", "start", worker_id]
        elif operation == "worker-stop" and worker_id is not None:
            if not worker["stop_intent"]:
                raise IsolationError("worker stop lacks intent")
            exact = docker + ["container", "stop", "--timeout", "0", worker_id]
        elif operation == "worker-wait" and worker_id is not None:
            exact = docker + ["container", "wait", worker_id]
        elif operation == "worker-remove" and worker_id is not None:
            if not worker["remove_intent"]:
                raise IsolationError("worker removal lacks intent")
            exact = docker + ["container", "rm", worker_id]
        elif operation == "worker-ready" and worker_id is not None:
            if worker["bound"] is None:
                raise IsolationError("worker readiness lacks a process binding")
            exact = docker + ["exec", worker_id, "/usr/bin/buildctl", "--addr",
                              "unix:///run/buildkit/buildkitd.sock", "debug", "workers"]
        elif operation == "worker-version" and worker_id is not None:
            if worker["bound"] is None:
                raise IsolationError("worker version lacks a process binding")
            exact = docker + ["exec", worker_id, "/usr/bin/buildkitd", "--version"]
        elif operation in ("image-build", "image-export", "wheel-prepare") and worker_id is not None:
            if ((kind == "source" and operation != "image-build")
                    or (kind == "wheel" and operation not in ("image-export", "wheel-prepare"))
                    or worker["bound"] is None or worker["buildkit"] is None):
                raise IsolationError("producer solve kind or worker differs")
            prefix_argv = docker + ["exec", worker_id, "/usr/bin/buildctl", "--addr",
                                    "unix:///run/buildkit/buildkitd.sock", "build"]
            if argv[:len(prefix_argv)] != prefix_argv:
                raise IsolationError("producer solve target differs")
            continue
        elif operation == "image-load":
            if imported is None or archive is None:
                raise IsolationError("producer import lacks intent")
            prefix_argv = docker + ["image", "load", "--input"]
            if (len(argv) != len(prefix_argv) + 1 or argv[:len(prefix_argv)] != prefix_argv
                    or not re.fullmatch(r"/proc/self/fd/[0-9]+", argv[-1]) or len(client["extra_fds"]) != 1
                    or int(argv[-1].rsplit("/", 1)[1]) != client["extra_fds"][0]["fd"]):
                raise IsolationError("producer import client differs")
            continue
        elif operation == "image-tag" and imported is not None and imported["native_image_id"] is not None:
            target = ("wuwaterm-component-build:" if kind == "source" else "wuwaterm-wheel-build:") + build_id
            exact = docker + ["image", "tag", imported["native_image_id"], target]
        elif operation == "probe-create" and probe is not None and probe["spec"] is not None:
            if not probe["create_intent"]:
                raise IsolationError("probe creation lacks intent")
            exact = _probe_create_argv(probe["spec"])
        elif operation == "probe-start" and probe_id is not None:
            exact = docker + ["container", "start", probe_id]
        elif operation == "probe-stop" and probe_id is not None:
            if not probe["stop_intent"]:
                raise IsolationError("probe stop lacks intent")
            exact = docker + ["container", "stop", "--timeout", "0", probe_id]
        elif operation in ("probe-wait", "probe-final-wait") and probe_id is not None:
            if operation == "probe-wait" and not probe["go_intent"]:
                raise IsolationError("probe work lacks release intent")
            exact = docker + ["container", "wait", probe_id]
        elif operation == "probe-logs" and probe_id is not None:
            exact = docker + ["container", "logs", probe_id]
        elif operation == "probe-remove" and probe_id is not None:
            if not probe["remove_intent"]:
                raise IsolationError("probe removal lacks intent")
            exact = docker + ["container", "rm", probe_id]
        if exact is None or argv != exact or client["extra_fds"]:
            raise IsolationError("producer client operation differs")
    for operation in ("parent-create", "worker-create", "worker-start", "probe-create", "probe-start", "image-tag"):
        if sum(client["operation"] == operation for client in clients) > 1:
            raise IsolationError("producer creation or start was issued more than once")
    return ledger


def _attempt_directories(kind: str) -> tuple[str, str]:
    if kind == "source":
        return "component-build-attempts", "component-build-staging"
    if kind == "wheel":
        return "component-wheel-build-attempts", "component-wheel-build-staging"
    raise IsolationError("invalid producer kind")


def _owned_attempt_bytes(directory: int, name: str) -> tuple[bytes, os.stat_result]:
    """Read one singly linked, operator-owned attempt without following links."""
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > _MAX_JSON):
            raise IsolationError("producer journal file is not exclusively owned")
        pieces: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(fd, min(_CHUNK, remaining))
            if not block:
                raise IsolationError("producer journal file is truncated")
            pieces.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
                    value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise IsolationError("producer journal file changed while reading")
        return b"".join(pieces), after
    finally:
        os.close(fd)


def _match_attempt_identity(kind: str, current: dict[str, Any], candidate: dict[str, Any]) -> None:
    if kind == "source":
        immutable = ("schema_version", "kind", "build_id", "source", "recovery_source",
                     "target_ref", "context_path", "probe_name", "command")
    elif kind == "wheel":
        immutable = ("schema_version", "build_id", "operations_source", "recovery_source", "source",
                     "target_ref", "context_path", "probe_name", "build_tool_wheels")
    else:
        raise IsolationError("invalid producer kind")
    if (any(candidate[key] != current[key] for key in immutable)
            or any(candidate["isolation"][key] != current["isolation"][key]
                   for key in ("kind", "build_id", "binding"))):
        raise IsolationError("producer journal immutable identity differs")


def publish_attempt(root: Path, kind: str, build_id: str, raw: bytes, *, new: bool) -> None:
    """Publish a validated journal without visible partials or extra links."""
    import product_manifest as manifest  # noqa: PLC0415
    parent_name(build_id)
    attempts, staging = _attempt_directories(kind)
    name = build_id + ".json"
    try:
        with manifest._directory(root, (".deployments", attempts), create=True) as target:
            if new:
                manifest._publish_unnamed(target, name, raw, mode=0o600)
                return
            with manifest._directory(root, (".deployments", staging), create=True) as temporary:
                if os.listdir(temporary):
                    raise IsolationError("producer journal staging requires reconciliation")
                before, info = _owned_attempt_bytes(target, name)
                previous, candidate = _json(before), _json(raw)
                if candidate.get("schema_version") == 2:
                    if kind == "source":
                        import component_build as producer  # noqa: PLC0415
                        producer._read_attempt_v2(root, name, previous)
                    else:
                        import component_wheel_build as producer  # noqa: PLC0415
                        producer._read_v2_attempt(root, name, previous)
                    _match_attempt_identity(kind, previous, candidate)
                stage_name = build_id + "." + hashlib.sha256(before).hexdigest() + ".json"
                manifest._publish_unnamed(temporary, stage_name, raw, mode=0o600)
                current, now = _owned_attempt_bytes(target, name)
                staged, _ = _owned_attempt_bytes(temporary, stage_name)
                if current != before or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or staged != raw:
                    raise IsolationError("producer journal preimage or candidate changed")
                os.replace(stage_name, name, src_dir_fd=temporary, dst_dir_fd=target)
                os.fsync(target)
                os.fsync(temporary)
    except (OSError, manifest.ManifestError) as exc:
        raise IsolationError("producer journal publication is unresolved") from exc


def sync_registered_attempt(root: Path, kind: str, build_id: str, attempt: dict[str, Any]) -> None:
    """Finish a visible registration's directory durability under its FD9."""
    import product_manifest as manifest  # noqa: PLC0415
    _fd9(root)
    parent_name(build_id)
    if attempt.get("schema_version") != 2 or attempt.get("status") != "registered":
        raise IsolationError("only a versioned registration can finish publication")
    attempts, staging = _attempt_directories(kind)
    name = build_id + ".json"
    try:
        with manifest._directory(root, (".deployments", attempts), create=False) as target:
            before, info = _owned_attempt_bytes(target, name)
            if _json(before) != attempt:
                raise IsolationError("canonical registration changed before synchronization")
            os.fsync(target)
            if os.path.lexists(root / ".deployments" / staging):
                with manifest._directory(root, (".deployments", staging), create=False) as temporary:
                    if os.listdir(temporary):
                        raise IsolationError("registration staging requires exact recovery")
                    os.fsync(temporary)
            after, current = _owned_attempt_bytes(target, name)
            if after != before or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise IsolationError("canonical registration changed during synchronization")
    except (OSError, manifest.ManifestError) as exc:
        raise IsolationError("registration publication durability remains unresolved") from exc


def reconcile_attempt_stage(root: Path, kind: str, build_id: str, attempt: dict[str, Any]) -> None:
    """Discard only an exact-A attempt's completely bound uncommitted stage."""
    import product_manifest as manifest  # noqa: PLC0415
    import component_runtime as runtime  # noqa: PLC0415
    _fd9(root)
    attempts, staging = _attempt_directories(kind)
    directory_path = root / ".deployments" / staging
    if not os.path.lexists(directory_path):
        return
    try:
        with manifest._directory(root, (".deployments", staging), create=False) as temporary:
            entries = os.listdir(temporary)
            if not entries:
                return
            pattern = re.compile(re.escape(build_id) + r"\.([0-9a-f]{64})\.json\Z")
            if len(entries) != 1 or (match := pattern.fullmatch(entries[0])) is None:
                raise IsolationError("producer journal staging is not uniquely bound")
            stage_name = entries[0]
            staged, staged_info = _owned_attempt_bytes(temporary, stage_name)
            value = runtime._decode(staged)
            if kind == "source":
                import component_build as producer  # noqa: PLC0415
                candidate = producer._read_attempt_v2(root, build_id + ".json", value)
            else:
                import component_wheel_build as producer  # noqa: PLC0415
                candidate = producer._read_v2_attempt(root, build_id + ".json", value)
            _match_attempt_identity(kind, attempt, candidate)
            with manifest._directory(root, (".deployments", attempts), create=False) as target:
                current, _ = _owned_attempt_bytes(target, build_id + ".json")
                if runtime._decode(current) != attempt:
                    raise IsolationError("canonical producer journal changed")
                current_hash = hashlib.sha256(current).hexdigest()
                if current_hash not in (match.group(1), hashlib.sha256(staged).hexdigest()):
                    raise IsolationError("producer journal staging preimage differs")
                repeated, after = _owned_attempt_bytes(temporary, stage_name)
                if repeated != staged or (after.st_dev, after.st_ino) != (staged_info.st_dev, staged_info.st_ino):
                    raise IsolationError("producer journal stage changed")
                os.unlink(stage_name, dir_fd=temporary)
                os.fsync(temporary)
    except (OSError, manifest.ManifestError, runtime.JournalError) as exc:
        raise IsolationError("producer journal staging remains unresolved") from exc


class ProducerSession:
    """Persist a single producer's guarded clients and containment intents."""

    def __init__(self, root: Path, attempt: dict[str, Any], write_attempt: Callable[[dict[str, Any]], None]) -> None:
        self.root = Path(os.path.abspath(root))
        self.attempt = attempt
        self.ledger = attempt["isolation"]
        self.write_attempt = write_attempt
        self.mutex = threading.RLock()
        self._poisoned = False
        self.endpoint: CancelEndpoint | None = None
        self.context = self.root / attempt["context_path"]
        self.client_directory = self.context / "clients"
        self.docker_config = self.context / "docker-config"
        self.environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C",
                            "DOCKER_CONFIG": str(self.docker_config)}

    def require_context(self) -> None:
        record = self.ledger["context"]
        if type(record) is not dict or record["device"] is None or record["inode"] is None:
            raise IsolationError("attempt context identity is unresolved")
        try:
            info = self.context.lstat()
            if (record["path"] != str(self.context.relative_to(self.root)) or not stat.S_ISDIR(info.st_mode)
                    or self.context.resolve(strict=True) != self.context
                    or (info.st_dev, info.st_ino) != (record["device"], record["inode"])):
                raise IsolationError("attempt context identity differs")
        except OSError as exc:
            raise IsolationError("attempt context is unavailable") from exc

    def bind_incomplete_context(self) -> None:
        record = self.ledger["context"]
        if record is None or record["device"] is not None or not os.path.lexists(self.context):
            return
        try:
            info = self.context.lstat()
            if (not record["create_intent"] or not stat.S_ISDIR(info.st_mode)
                    or self.context.resolve(strict=True) != self.context or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(self.context)):
                raise IsolationError("unbound attempt context is unknown")
            with self.transition(phase="reconcile"):
                record.update(device=info.st_dev, inode=info.st_ino)
        except OSError as exc:
            raise IsolationError("cannot bind incomplete attempt context") from exc

    @contextmanager
    def transition(self, *, phase: str | None = None) -> Iterator[None]:
        """Publish one complete, validated state change or poison this session."""
        with self.mutex:
            if self._poisoned:
                raise IsolationError("producer journal publication requires exact-source recovery")
            try:
                yield
                if phase is not None:
                    self.attempt["phase"] = phase
                try:
                    if self.ledger["kind"] == "source":
                        import component_build as producer  # noqa: PLC0415
                        producer._read_attempt_v2(self.root, self.ledger["build_id"] + ".json", self.attempt)
                    elif self.ledger["kind"] == "wheel":
                        import component_wheel_build as producer  # noqa: PLC0415
                        producer._read_v2_attempt(self.root, self.ledger["build_id"] + ".json", self.attempt)
                    else:
                        raise IsolationError("invalid producer journal kind")
                except Exception as exc:
                    raise IsolationError("producer journal validation refused") from exc
                self.write_attempt(self.attempt)
            except BaseException:
                self._poisoned = True
                raise

    def save(self, *, phase: str | None = None) -> None:
        with self.transition(phase=phase):
            pass

    def cancel(self, reason: str) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", reason):
            raise IsolationError("invalid cancellation reason")
        with self.mutex:
            if self._poisoned:
                raise IsolationError("producer journal publication requires exact-source recovery")
            if self.attempt["status"] == "registered":
                raise IsolationError("registered producer is immutable")
            if self.ledger["cancel_intent"] is None:
                with self.transition(phase="reconcile"):
                    self.ledger["cancel_intent"] = reason

    def open_control(self) -> None:
        def control(value: dict[str, Any]) -> None:
            with self.transition():
                self.ledger["control"] = value

        self.endpoint = CancelEndpoint(self.root, self.ledger["kind"], self.ledger["build_id"],
                                       control, lambda: self.cancel("Cancelled"))

    def close_control(self) -> None:
        if self.endpoint is not None:
            self.endpoint.close()
            self.endpoint = None

    def prepare_directories(self) -> None:
        if os.path.lexists(self.context):
            raise IsolationError("attempt context name already exists")
        with self.transition(phase="context-intent"):
            self.ledger["context"] = {"path": str(self.context.relative_to(self.root)), "create_intent": True,
                                      "device": None, "inode": None}
            for key, name in (("cache", "worker-cache"), ("output", "worker-output")):
                self.ledger[key] = {"path": str((self.context / name).relative_to(self.root)), "create_intent": True,
                                    "device": None, "inode": None, "remove_intent": False, "absent": False}
        try:
            self.context.mkdir(mode=0o700)
            context_info = self.context.lstat()
            with self.transition():
                self.ledger["context"].update(device=context_info.st_dev, inode=context_info.st_ino)
            for name in ("clients", "docker-config", "worker-output", "worker-cache"):
                (self.context / name).mkdir(mode=0o700)
                if name in ("worker-cache", "worker-output"):
                    key = "cache" if name == "worker-cache" else "output"
                    info = (self.context / name).lstat()
                    with self.transition():
                        self.ledger[key].update(device=info.st_dev, inode=info.st_ino)
            for directory in (self.client_directory, self.docker_config, self.context / "worker-output",
                              self.context / "worker-cache", self.context, self.context.parent):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.save(phase="context")
        except OSError as exc:
            raise IsolationError("cannot create attempt-owned directories") from exc

    def client(self, operation: str, argv: list[str], *, timeout: float = 1800,
               extra_fds: tuple[int, ...] = (), environment: dict[str, str] | None = None,
               during_recovery: bool = False,
               on_release: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        self.require_context()
        if self._poisoned or (not during_recovery and self.ledger["cancel_intent"] is not None):
            raise IsolationError("producer cancellation is pending")
        with self.mutex:
            generation = len(self.ledger["clients"]) + 1

        def persist(value: dict[str, Any]) -> None:
            with self.transition():
                clients = self.ledger["clients"]
                if value["generation"] == len(clients) + 1:
                    clients.append(value)
                elif value["generation"] == len(clients) and clients[-1]["generation"] == value["generation"]:
                    clients[-1] = value
                else:
                    raise IsolationError("client generation changed")
                if value["state"] == "release-intent" and on_release is not None:
                    on_release(value)

        result = run_guarded_client(self.root, operation, argv, environment or self.environment,
                                    self.client_directory, generation, persist, timeout=timeout,
                                    cancelled=(lambda: self._poisoned if during_recovery else
                                               self._poisoned or self.ledger["cancel_intent"] is not None),
                                    extra_fds=extra_fds, release_lock=self.mutex)
        if result["outcome"] != "exited":
            raise IsolationError("producer client was interrupted")
        return result

    def output(self, generation: int, which: str, *, limit: int = 16 * 1024 * 1024) -> bytes:
        self.require_context()
        if which not in ("stdout", "stderr") or generation < 1 or generation > len(self.ledger["clients"]):
            raise IsolationError("invalid client output reference")
        client = self.ledger["clients"][generation - 1]
        if client["state"] != "completed":
            raise IsolationError("client response is not complete")

        def read(path: Path, maximum: int) -> bytes:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                    raise IsolationError("client output is unsafe or oversized")
                chunks = []
                remaining = info.st_size
                while remaining:
                    block = os.read(fd, min(_CHUNK, remaining))
                    if not block:
                        raise IsolationError("client output is truncated")
                    chunks.append(block)
                    remaining -= len(block)
                return b"".join(chunks)
            finally:
                os.close(fd)

        try:
            result_raw = read(self.root / client["result_path"], 64 * 1024)
            result = _json(result_raw)
            if (hashlib.sha256(result_raw).hexdigest() != client["result_sha256"] or type(result) is not dict
                    or result.get("generation") != generation or result.get("operation") != client["operation"]
                    or result.get("exit_code") != client["exit_code"]):
                raise IsolationError("client result differs from its ledger")
            data = read(self.root / client[which + "_path"], limit)
            if hashlib.sha256(data).hexdigest() != result.get(which + "_sha256"):
                raise IsolationError("client output differs from its result")
            return data
        except OSError as exc:
            raise IsolationError("client output is unavailable") from exc

    def copy_output(self, generation: int, which: str, destination: Path, *,
                    limit: int = 100 * 1024 * 1024) -> str:
        """Copy a completed client's exact output to a new owned file and fsync it."""
        self.require_context()
        if (which not in ("stdout", "stderr") or generation < 1 or generation > len(self.ledger["clients"])
                or type(limit) is not int or limit <= 0):
            raise IsolationError("invalid client output reference")
        client = self.ledger["clients"][generation - 1]
        if client["state"] != "completed":
            raise IsolationError("client response is not complete")
        destination = Path(os.path.abspath(destination))
        if (not destination.is_relative_to(self.context) or destination == self.context
                or destination.parent.resolve(strict=True) != destination.parent):
            raise IsolationError("client output destination is not attempt-owned")
        result_path = self.root / client["result_path"]
        source_path = self.root / client[which + "_path"]
        source = target = directory = -1
        try:
            result_fd = os.open(result_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(result_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024:
                    raise IsolationError("client result is unsafe")
                raw = os.read(result_fd, info.st_size + 1)
            finally:
                os.close(result_fd)
            result = _json(raw)
            if (len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != client["result_sha256"]
                    or type(result) is not dict or result.get("generation") != generation
                    or result.get("operation") != client["operation"] or result.get("exit_code") != client["exit_code"]):
                raise IsolationError("client result differs from its ledger")
            source = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise IsolationError("client output is unsafe")
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            target = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
            digest = hashlib.sha256()
            while block := os.read(source, _CHUNK):
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(target, view)
                    if written <= 0:
                        raise IsolationError("client output copy failed")
                    view = view[written:]
            after = os.fstat(source)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or digest.hexdigest() != result.get(which + "_sha256")):
                raise IsolationError("client output changed while copying")
            os.fsync(target)
            os.fsync(directory)
            return digest.hexdigest()
        except OSError as exc:
            raise IsolationError("cannot copy client output") from exc
        finally:
            for descriptor in (source, target, directory):
                if descriptor >= 0:
                    os.close(descriptor)

    def read_docker(self, argv: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
        self.require_context()
        try:
            return subprocess.run(["/usr/bin/docker", "--host", "unix:///run/docker.sock", *argv],
                                  env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise IsolationError("cannot inspect Docker state") from exc

    def inspect_container(self, identifier: str) -> dict[str, Any] | None:
        result = self.read_docker(["container", "inspect", identifier])
        if result.returncode:
            if result.returncode == 1 and b"No such container" in result.stderr:
                return None
            raise IsolationError("container inspection is unknown")
        try:
            value = json.loads(result.stdout)
            if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
                raise IsolationError("container inspection is malformed")
            return value[0]
        except (UnicodeError, ValueError) as exc:
            raise IsolationError("container inspection is malformed") from exc

    def create_parent(self) -> None:
        parent = self.ledger["parent"]
        if parent["create_intent"]:
            raise IsolationError("containment creation requires reconciliation")
        if not parent_absent(parent["name"]):
            raise IsolationError("containment slice name already exists")
        with self.transition(phase="containment-intent"):
            parent["create_intent"] = True
            parent["create_outcome"] = "unknown"
        result = self.client("parent-create", parent_create_argv(parent["name"]), timeout=60)
        if result["exit_code"]:
            raise IsolationError("containment creation response is not successful")
        deadline = time.monotonic() + 30
        while True:
            try:
                observed = parent_snapshot(parent["name"])
                break
            except IsolationError:
                if time.monotonic() >= deadline:
                    raise IsolationError("containment creation remains unresolved") from None
                time.sleep(0.1)
        with self.transition(phase="containment-bound"):
            parent["snapshot"] = observed
            parent["create_outcome"] = "bound"

    def create_worker(self) -> None:
        parent = self.ledger["parent"]
        worker = self.ledger["worker"]
        if parent["snapshot"] is None or worker["create_intent"]:
            raise IsolationError("worker creation requires reconciliation")
        verify_parent_snapshot(parent["snapshot"], parent_snapshot(parent["name"]), populated=0)
        spec = worker_spec(self.root, self.ledger["kind"], self.ledger["build_id"])
        if self.inspect_container(spec["name"]) is not None:
            raise IsolationError("worker name already exists")
        with self.transition(phase="worker-create-intent"):
            worker["spec"] = spec
            worker["create_intent"] = True
            worker["create_outcome"] = "unknown"
        result = self.client("worker-create", worker_create_argv(worker["spec"]), timeout=60)
        if result["exit_code"]:
            raise IsolationError("worker creation response is not successful")
        identifier = self.output(result["generation"], "stdout", limit=128).decode("ascii").strip()
        container = self.inspect_container(identifier)
        if container is None:
            raise IsolationError("created worker cannot be inspected")
        observed = verify_worker(worker["spec"], container, worker_id=identifier, statuses=("created",))
        with self.transition(phase="worker-created"):
            worker["id"] = observed["id"]
            worker["create_outcome"] = "bound"
        self.save(phase="worker-start-intent")
        started = self.client("worker-start", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                "container", "start", identifier], timeout=60)
        if started["exit_code"]:
            raise IsolationError("worker start response is not successful")
        container = self.inspect_container(identifier)
        if container is None:
            raise IsolationError("started worker is unavailable")
        observed = verify_worker(worker["spec"], container, worker_id=identifier, statuses=("running",))
        actual_parent = parent_snapshot(parent["name"])
        verify_parent_snapshot(parent["snapshot"], actual_parent, populated=1)
        membership = worker_membership(actual_parent, observed["pid"])
        with self.transition(phase="worker-bound"):
            worker["bound"] = {"container": observed, "membership": membership}

    def solve(self, operation: str, arguments: list[str], *, timeout: float = 1800) -> dict[str, Any]:
        worker = self.ledger["worker"]
        if worker["id"] is None or worker["bound"] is None or worker["buildkit"] is None:
            raise IsolationError("worker is not bound")
        container = self.inspect_container(worker["id"])
        if container is None:
            raise IsolationError("worker is unavailable")
        observed = verify_worker(worker["spec"], container, worker_id=worker["id"], statuses=("running",))
        if observed != worker["bound"]["container"]:
            raise IsolationError("worker start identity changed")
        parent = parent_snapshot(self.ledger["parent"]["name"])
        verify_parent_snapshot(self.ledger["parent"]["snapshot"], parent, populated=1)
        membership = worker_membership(parent, observed["pid"])
        previous = worker["bound"]["membership"]
        if any(membership[key] != previous[key] for key in ("pid", "start_ticks", "namespaces", "host_namespaces")):
            raise IsolationError("worker process boundary changed")
        argv = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec", worker["id"],
                "/usr/bin/buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", *arguments]
        with self.transition(phase="work-release-intent"):
            worker["bound"]["membership"] = membership
        result = self.client(operation, argv, timeout=timeout)
        if result["exit_code"]:
            raise IsolationError("isolated build operation failed")
        return result

    def wait_worker_ready(self, *, timeout: float = 30) -> None:
        worker = self.ledger["worker"]
        if worker["id"] is None or worker["bound"] is None:
            raise IsolationError("worker is not bound")
        deadline = time.monotonic() + timeout
        while True:
            argv = ["/usr/bin/docker", "--host", "unix:///run/docker.sock", "exec", worker["id"],
                    "/usr/bin/buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", "debug", "workers"]
            result = self.client("worker-ready", argv, timeout=10)
            if result["exit_code"] == 0:
                try:
                    rows = self.output(result["generation"], "stdout", limit=64 * 1024).decode("ascii").splitlines()
                    if len(rows) != 2 or rows[0].split() != ["ID", "PLATFORMS"]:
                        raise IsolationError("BuildKit worker inventory is malformed")
                    fields = rows[1].split()
                    if (len(fields) != 2 or not re.fullmatch(r"[a-z0-9]{12,64}", fields[0])
                            or "linux/amd64" not in fields[1].split(",")):
                        raise IsolationError("BuildKit worker platform differs")
                except UnicodeError as exc:
                    raise IsolationError("BuildKit worker inventory is malformed") from exc
                container = self.inspect_container(worker["id"])
                if container is None:
                    raise IsolationError("ready worker is unavailable")
                observed = verify_worker(worker["spec"], container, worker_id=worker["id"], statuses=("running",))
                if observed != worker["bound"]["container"]:
                    raise IsolationError("ready worker start identity changed")
                parent = parent_snapshot(self.ledger["parent"]["name"])
                verify_parent_snapshot(self.ledger["parent"]["snapshot"], parent, populated=1)
                membership = worker_membership(parent, observed["pid"])
                previous = worker["bound"]["membership"]
                if any(membership[key] != previous[key] for key in ("pid", "start_ticks", "namespaces", "host_namespaces")):
                    raise IsolationError("ready worker process identity changed")
                version_result = self.client("worker-version", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                                   "exec", worker["id"], "/usr/bin/buildkitd", "--version"],
                                             timeout=10)
                if version_result["exit_code"]:
                    raise IsolationError("BuildKit version is unavailable")
                try:
                    text = self.output(version_result["generation"], "stdout", limit=4096).decode("ascii").strip()
                except UnicodeError as exc:
                    raise IsolationError("BuildKit version is malformed") from exc
                version = re.fullmatch(r"buildkitd github\.com/moby/buildkit (v0\.34\.2) ([0-9a-f]{40})", text)
                if version is None:
                    raise IsolationError("BuildKit version differs")
                with self.transition(phase="worker-ready"):
                    worker["bound"]["membership"] = membership
                    worker["buildkit"] = {"id": fields[0], "platforms": fields[1].split(","),
                                          "version": version.group(1), "revision": version.group(2)}
                return
            if time.monotonic() >= deadline:
                raise IsolationError("isolated worker did not become ready")
            time.sleep(0.2)

    def seal_export(self) -> dict[str, Any]:
        if self.ledger["worker_witness"] is None or not self.ledger["worker"]["absent"]:
            raise IsolationError("image export requires exhausted worker")
        raw = self.context / "worker-output" / "image.tar"
        destination = self.context / "sealed-image.tar"
        with sealed_oci_input(raw) as (_, proof):
            graph = {key: value for key, value in proof.items() if key not in ("config", "archive_sha256")}
            archive = {"path": str(destination.relative_to(self.root)), "sha256": proof["archive_sha256"],
                       "graph": graph, "state": "intent", "remove_intent": False}
        with self.transition(phase="archive-intent"):
            self.ledger["archive"] = archive
        written = publish_oci_archive(raw, destination)
        if (written["archive_sha256"] != archive["sha256"]
                or {key: value for key, value in written.items() if key not in ("config", "archive_sha256")} != graph):
            raise IsolationError("sealed image changed during publication")
        with self.transition(phase="archive-sealed"):
            archive["state"] = "sealed"
        return graph

    @contextmanager
    def sealed_archive(self) -> Iterator[tuple[int, dict[str, Any]]]:
        self.require_context()
        archive = self.ledger["archive"]
        if type(archive) is not dict or archive["state"] not in ("intent", "sealed"):
            raise IsolationError("sealed archive is unavailable")
        path = self.root / archive["path"]
        if path != self.context / "sealed-image.tar":
            raise IsolationError("sealed archive path differs")
        with sealed_oci_input(path, expected_sha256=archive["sha256"]) as (fd, proof):
            graph = {key: value for key, value in proof.items() if key not in ("config", "archive_sha256")}
            if graph != archive["graph"]:
                raise IsolationError("sealed image graph differs")
            yield fd, proof

    def _verify_image(self, proof: dict[str, Any]) -> str:
        binding = preflight(self.root)
        if (binding["daemon"] != self.ledger["binding"]["daemon"]
                or binding["boot_id"] != self.ledger["binding"]["boot_id"]
                or binding["binaries_sha256"] != self.ledger["binding"]["binaries_sha256"]):
            raise IsolationError("Docker daemon or tools changed")
        inspected = self.read_docker(["image", "inspect", proof["manifest_digest"]])
        if inspected.returncode:
            raise IsolationError("imported image cannot be inspected")
        try:
            images = _json(inspected.stdout)
            if type(images) is not list or len(images) != 1:
                raise IsolationError("imported image inspection is malformed")
            return verify_imported_image(binding["daemon"], self.ledger["binding"]["daemon"], images[0], proof)
        except (TypeError, KeyError) as exc:
            raise IsolationError("imported image inspection is malformed") from exc

    def import_image(self, *, during_recovery: bool = False) -> str:
        archive = self.ledger["archive"]
        if type(archive) is not dict or self.ledger["worker_witness"] is None:
            raise IsolationError("image import lacks an archive")
        previous = self.ledger["import"]
        if previous is None:
            previous = {"archive_sha256": archive["sha256"], "released_generation": None,
                        "native_image_id": None, "verified": False}
            with self.transition(phase="import-intent"):
                self.ledger["import"] = previous
        elif previous["archive_sha256"] != archive["sha256"]:
            raise IsolationError("image import archive binding differs")

        def released(client: dict[str, Any]) -> None:
            if previous["released_generation"] is None:
                previous["released_generation"] = client["generation"]

        with self.sealed_archive() as (descriptor, proof):
            result = self.client("image-load", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                 "image", "load", "--input", f"/proc/self/fd/{descriptor}"],
                                 timeout=1800, extra_fds=(descriptor,), during_recovery=during_recovery,
                                 on_release=released)
            if result["exit_code"]:
                raise IsolationError("image load response is not successful")
            native_id = self._verify_image(proof)
        with self.transition(phase="image-verified"):
            previous.update(native_image_id=native_id, verified=True)
            archive["state"] = "sealed"
        return native_id

    def inspect_reference(self, reference: str, expected_id: str) -> str:
        inspected = self.read_docker(["image", "inspect", "--format", "{{.Id}}", reference])
        if inspected.returncode:
            if inspected.returncode == 1 and b"No such image" in inspected.stderr:
                return "absent"
            raise IsolationError("candidate reference is unknown")
        try:
            value = inspected.stdout.decode("ascii").strip()
        except UnicodeError as exc:
            raise IsolationError("candidate reference is malformed") from exc
        if value != expected_id:
            raise IsolationError("candidate reference has a different binding")
        return "exact_expected_binding"

    def tag_image(self, reference: str) -> None:
        image = self.ledger["import"]
        archive = self.ledger["archive"]
        if type(image) is not dict or not image["verified"] or type(archive) is not dict:
            raise IsolationError("candidate tag requires a verified import")
        expected = ("wuwaterm-component-build:" if self.ledger["kind"] == "source" else "wuwaterm-wheel-build:") \
                   + self.ledger["build_id"]
        if reference != expected or self.ledger["retained_reference"] is not None:
            raise IsolationError("candidate tag is not the unique attempt reference")
        native_id = image["native_image_id"]
        if self.inspect_reference(reference, native_id) != "absent":
            raise IsolationError("candidate reference already exists")
        self.save(phase="tag-intent")

        def released(client: dict[str, Any]) -> None:
            self.ledger["retained_reference"] = {
                "ref": reference, "issued_generation": client["generation"], "archive_sha256": archive["sha256"],
                "graph_descriptor_digest": archive["graph"]["manifest_digest"],
                "expected_native_image_id": native_id, "allowed_observation": ["absent", "exact_expected_binding"],
                "late_appearance_allowed": True,
            }

        result = self.client("image-tag", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                             "image", "tag", native_id, reference], timeout=60, on_release=released)
        if result["exit_code"] or self.inspect_reference(reference, native_id) != "exact_expected_binding":
            raise IsolationError("candidate tag remains unresolved")
        self.save(phase="tagged")

    def _client_state(self, operation: str) -> dict[str, Any] | None:
        found = [entry for entry in self.ledger["clients"] if entry["operation"] == operation]
        return found[-1] if found else None

    def resolve_creators(self) -> None:
        """Bind known creations; do not resend an unknown create request."""
        parent = self.ledger["parent"]
        if parent["create_intent"] and parent["snapshot"] is None:
            client = self._client_state("parent-create")
            if client is None or client["state"] in ("planned", "armed"):
                if not parent_absent(parent["name"]):
                    raise IsolationError("unreleased containment name is occupied")
                with self.transition():
                    parent["create_outcome"] = "not_released"
                    parent["absent"] = True
            else:
                try:
                    snapshot = parent_snapshot(parent["name"])
                except IsolationError as exc:
                    raise IsolationError("containment creation remains pending") from exc
                with self.transition():
                    parent["snapshot"] = snapshot
                    parent["create_outcome"] = "bound"
        worker = self.ledger["worker"]
        if worker["create_intent"] and worker["id"] is None:
            if worker["spec"] is None:
                raise IsolationError("worker creation lacks its bound recipe")
            found = self.inspect_container(worker["spec"]["name"])
            client = self._client_state("worker-create")
            if found is None:
                if client is None or client["state"] in ("planned", "armed"):
                    with self.transition():
                        worker["create_outcome"] = "not_released"
                        worker["absent"] = True
                else:
                    raise IsolationError("worker creation remains pending")
            else:
                observed = verify_worker(worker["spec"], found)
                with self.transition():
                    worker["id"] = observed["id"]
                    worker["create_outcome"] = "bound"
        probe = self.ledger["probe"]
        if probe is not None and probe["create_intent"] and probe["id"] is None:
            if probe["spec"] is None:
                raise IsolationError("probe creation lacks its bound recipe")
            found = self.inspect_container(probe["spec"]["name"])
            client = self._client_state("probe-create")
            if found is None:
                if client is None or client["state"] in ("planned", "armed"):
                    with self.transition():
                        probe["create_outcome"] = "not_released"
                        probe["absent"] = True
                else:
                    raise IsolationError("probe creation remains pending")
            else:
                observed = verify_probe_container(probe["spec"], found)
                with self.transition():
                    probe["id"] = observed["id"]
                    probe["create_outcome"] = "bound"

    def _endpoint_absent(self, identifier: str, network: str) -> None:
        if network not in ("bridge", "none"):
            raise IsolationError("unknown owned container network")
        result = self.read_docker(["network", "inspect", network, "--format", "{{json .Containers}}"])
        if result.returncode:
            raise IsolationError("worker network endpoint is unknown")
        try:
            containers = _json(result.stdout)
            if containers is None:
                containers = {}
            if type(containers) is not dict or identifier in containers:
                raise IsolationError("worker network endpoint remains")
        except (ValueError, TypeError) as exc:
            raise IsolationError("worker network endpoint is unknown") from exc

    def stop_worker(self) -> None:
        worker = self.ledger["worker"]
        if not worker["create_intent"] or worker["create_outcome"] == "not_released":
            return
        if worker["id"] is None or worker["spec"] is None:
            raise IsolationError("worker identity is unresolved")
        identifier = worker["id"]
        container = self.inspect_container(identifier)
        if container is None:
            if not worker["remove_intent"]:
                raise IsolationError("worker disappeared before removal intent")
            self._endpoint_absent(identifier, "bridge")
            with self.transition():
                worker["absent"] = True
            return
        observed = verify_worker(worker["spec"], container, worker_id=identifier)
        if observed["status"] == "running":
            with self.transition(phase="reconcile"):
                worker["stop_intent"] = True
            self.client("worker-stop", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                         "container", "stop", "--timeout", "0", identifier],
                        timeout=60, during_recovery=True)
            container = self.inspect_container(identifier)
            if container is None:
                raise IsolationError("worker disappeared before removal intent")
            observed = verify_worker(worker["spec"], container, worker_id=identifier)
        if observed["status"] not in ("created", "exited"):
            raise IsolationError("worker has not stopped")
        if observed["status"] == "exited":
            waited = self.client("worker-wait", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                   "container", "wait", identifier], timeout=60, during_recovery=True)
            if waited["exit_code"]:
                raise IsolationError("worker wait response is unknown")
        with self.transition(phase="reconcile"):
            worker["remove_intent"] = True
        result = self.client("worker-remove", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                "container", "rm", identifier], timeout=60, during_recovery=True)
        remaining = self.inspect_container(identifier)
        if remaining is not None:
            verify_worker(worker["spec"], remaining, worker_id=identifier)
            raise IsolationError("worker removal remains unresolved")
        if result["exit_code"] and b"No such container" not in self.output(result["generation"], "stderr", limit=4096):
            raise IsolationError("worker removal response is unknown")
        self._endpoint_absent(identifier, "bridge")
        with self.transition():
            worker["absent"] = True

    def probe_image(self, image_id: str, code: str, *, timeout: float = 60) -> bytes:
        if self.ledger["probe"] is not None or self.ledger["witness"] is None:
            raise IsolationError("probe requires an exhausted worker")
        result = self.read_docker(["image", "inspect", image_id])
        if result.returncode:
            raise IsolationError("probe image is unavailable")
        try:
            images = _json(result.stdout)
            if (type(images) is not list or len(images) != 1 or images[0].get("Id") != image_id
                    or images[0].get("Config", {}).get("Volumes") not in (None, {})):
                raise IsolationError("probe image declares unsupported volumes")
        except (TypeError, AttributeError) as exc:
            raise IsolationError("probe image inspection is malformed") from exc
        control_path = self.context / "probe-control"
        if os.path.lexists(control_path):
            raise IsolationError("probe control name already exists")
        probe: dict[str, Any] = {"control_path": str(control_path.relative_to(self.root)), "control_intent": True,
                                 "spec": None, "create_intent": False, "create_outcome": "not_attempted",
                                 "id": None, "bound": None, "go_intent": False,
                                 "stop_intent": False, "remove_intent": False, "absent": False,
                                 "output_sha256": None}
        with self.transition(phase="probe-control-intent"):
            self.ledger["probe"] = probe
            self.ledger["probe_control"] = {"path": str(control_path.relative_to(self.root)), "create_intent": True,
                                            "device": None, "inode": None, "remove_intent": False, "absent": False}
            self.ledger["witness"] = None
        try:
            control_path.mkdir(mode=0o755)
            info = control_path.lstat()
            with self.transition():
                self.ledger["probe_control"].update(device=info.st_dev, inode=info.st_ino)
            directory = os.open(self.context, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise IsolationError("cannot create probe control directory") from exc
        spec = probe_spec(self.root, self.ledger["kind"], self.ledger["build_id"], image_id, code)
        if self.inspect_container(spec["name"]) is not None:
            raise IsolationError("probe name already exists")
        with self.transition(phase="probe-create-intent"):
            probe["spec"] = spec
            probe["create_intent"] = True
            probe["create_outcome"] = "unknown"
        created = self.client("probe-create", probe_create_argv(probe["spec"]), timeout=60)
        if created["exit_code"]:
            raise IsolationError("probe creation response is not successful")
        identifier = self.output(created["generation"], "stdout", limit=128).decode("ascii").strip()
        container = self.inspect_container(identifier)
        if container is None:
            raise IsolationError("created probe cannot be inspected")
        observed = verify_probe_container(probe["spec"], container, probe_id=identifier, statuses=("created",))
        with self.transition(phase="probe-created"):
            probe["id"] = observed["id"]
            probe["create_outcome"] = "bound"
        self.save(phase="probe-start-intent")
        started = self.client("probe-start", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                               "container", "start", identifier], timeout=60)
        if started["exit_code"]:
            raise IsolationError("probe start response is not successful")
        container = self.inspect_container(identifier)
        if container is None:
            raise IsolationError("started probe is unavailable")
        observed = verify_probe_container(probe["spec"], container, probe_id=identifier, statuses=("running",))
        parent = parent_snapshot(self.ledger["parent"]["name"])
        verify_parent_snapshot(self.ledger["parent"]["snapshot"], parent, populated=1)
        membership = worker_membership(parent, observed["pid"])
        with self.transition(phase="probe-bound"):
            probe["bound"] = {"container": observed, "membership": membership}
        with self.mutex:
            if self._poisoned or self.ledger["cancel_intent"] is not None:
                raise IsolationError("probe cancellation is pending")
            with self.transition(phase="probe-release-intent"):
                probe["go_intent"] = True
            fd = os.open(control_path / "GO", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            directory = os.open(control_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        waited = self.client("probe-wait", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                             "container", "wait", identifier], timeout=timeout)
        if waited["exit_code"] or self.output(waited["generation"], "stdout", limit=128).strip() != b"0":
            raise IsolationError("installed-package probe failed")
        logs = self.client("probe-logs", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                           "container", "logs", identifier], timeout=60)
        if logs["exit_code"]:
            raise IsolationError("installed-package probe output is unavailable")
        output = self.output(logs["generation"], "stdout")
        with self.transition(phase="probe-verified-output"):
            probe["output_sha256"] = hashlib.sha256(output).hexdigest()
        return output

    def stop_probe(self) -> None:
        probe = self.ledger["probe"]
        if probe is None or not probe["create_intent"] or probe["create_outcome"] == "not_released":
            return
        if probe["id"] is None or probe["spec"] is None:
            raise IsolationError("probe identity is unresolved")
        identifier = probe["id"]
        container = self.inspect_container(identifier)
        if container is None:
            if not probe["remove_intent"]:
                raise IsolationError("probe disappeared before removal intent")
            self._endpoint_absent(identifier, "none")
            with self.transition():
                probe["absent"] = True
            return
        observed = verify_probe_container(probe["spec"], container, probe_id=identifier)
        if observed["status"] == "running":
            with self.transition(phase="reconcile"):
                probe["stop_intent"] = True
            self.client("probe-stop", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                        "container", "stop", "--timeout", "0", identifier],
                        timeout=60, during_recovery=True)
            container = self.inspect_container(identifier)
            if container is None:
                raise IsolationError("probe disappeared before removal intent")
            observed = verify_probe_container(probe["spec"], container, probe_id=identifier)
        if observed["status"] not in ("created", "exited"):
            raise IsolationError("probe has not stopped")
        if observed["status"] == "exited":
            waited = self.client("probe-final-wait", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                                       "container", "wait", identifier], timeout=60, during_recovery=True)
            if waited["exit_code"]:
                raise IsolationError("probe wait response is unknown")
        with self.transition(phase="reconcile"):
            probe["remove_intent"] = True
        result = self.client("probe-remove", ["/usr/bin/docker", "--host", "unix:///run/docker.sock",
                                               "container", "rm", identifier], timeout=60, during_recovery=True)
        remaining = self.inspect_container(identifier)
        if remaining is not None:
            verify_probe_container(probe["spec"], remaining, probe_id=identifier)
            raise IsolationError("probe removal remains unresolved")
        if result["exit_code"] and b"No such container" not in self.output(result["generation"], "stderr", limit=4096):
            raise IsolationError("probe removal response is unknown")
        self._endpoint_absent(identifier, "none")
        with self.transition():
            probe["absent"] = True

    def exhaustion_witness(self) -> None:
        parent, worker = self.ledger["parent"], self.ledger["worker"]
        if parent["snapshot"] is None:
            if parent["create_outcome"] in ("not_attempted", "not_released"):
                return
            raise IsolationError("containment parent is unresolved")
        if worker["create_intent"] and worker["create_outcome"] not in ("not_released", "bound"):
            raise IsolationError("worker creation is unresolved")
        if worker["create_outcome"] == "bound" and not worker["absent"]:
            raise IsolationError("worker has not been removed")
        probe = self.ledger["probe"]
        if probe is not None and probe["create_intent"] and (probe.get("create_outcome") not in ("not_released", "bound")
                                                             or (probe.get("create_outcome") == "bound" and not probe.get("absent"))):
            raise IsolationError("probe has not been reconciled")
        if parent["stop_intent"] and parent_absent(parent["name"]):
            if self.ledger["witness"] is None:
                raise IsolationError("missing containment parent lacks a durable witness")
            verify_parent_snapshot(parent["snapshot"], self.ledger["witness"]["parent"], populated=0)
            return
        observed = parent_snapshot(parent["name"])
        verify_parent_snapshot(parent["snapshot"], observed, populated=0)
        deleted = [value for value in (worker["id"], None if probe is None else probe.get("id")) if value is not None]
        with self.transition(phase="exhaustion-witness"):
            self.ledger["witness"] = {"parent": observed, "deleted_container_ids": deleted,
                                      "client_generations": [item["generation"] for item in self.ledger["clients"]],
                                      "client_fences": [dict(item) for item in self.ledger["client_fences"]]}
            if self.ledger["worker_witness"] is None and worker["id"] is not None and probe is None:
                self.ledger["worker_witness"] = dict(self.ledger["witness"])

    def reconcile_clients(self, prior: list[dict[str, Any]] | None = None) -> None:
        """Record exact host-client fences while holding the recovered FD9."""
        known = {item["generation"] for item in self.ledger["client_fences"]}
        for item in prior or []:
            if (type(item) is not dict or set(item) != {"generation", "result"}
                    or type(item["generation"]) is not int or not 1 <= item["generation"] <= len(self.ledger["clients"])
                    or item["result"] not in ("fenced", "exited", "absent", "previous-boot", "reused-pid")):
                raise IsolationError("invalid recovered client fence")
            client = self.ledger["clients"][item["generation"] - 1]
            if client["state"] not in ("armed", "release-intent"):
                raise IsolationError("recovered fence does not match an active client")
            if item["generation"] not in known:
                with self.transition(phase="reconcile"):
                    self.ledger["client_fences"].append(dict(item))
                known.add(item["generation"])
        for client in self.ledger["clients"]:
            generation = client["generation"]
            if generation in known or client["state"] == "completed":
                continue
            result = "not-released" if client["state"] == "planned" else fence_recorded_client(self.root, client)
            with self.transition(phase="reconcile"):
                self.ledger["client_fences"].append({"generation": generation, "result": result})
            known.add(generation)

    def _cleanup_directory(self, key: str, name: str) -> None:
        record = self.ledger[key]
        if record is None:
            return
        if os.path.lexists(self.context):
            self.require_context()
        if self.ledger["parent"]["snapshot"] is not None and self.ledger["witness"] is None:
            raise IsolationError("directory cleanup requires a durable exhaustion witness")
        path = self.root / record["path"]
        if path != self.context / name or not shutil.rmtree.avoids_symlink_attacks:
            raise IsolationError("owned directory cleanup path is unsupported")
        creator_not_released = (not self.ledger["probe"]["create_intent"] if key == "probe_control"
                                else not self.ledger["worker"]["create_intent"])
        try:
            if not os.path.lexists(path):
                if not record["remove_intent"] and not (
                    record["create_intent"] and record["device"] is None and creator_not_released
                ):
                    raise IsolationError("directory disappeared before removal intent")
                with self.transition():
                    record["absent"] = True
                return
            info = path.lstat()
            if record["device"] is None and record["create_intent"] and creator_not_released:
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o700, 0o755):
                    raise IsolationError("unbound directory ownership differs")
                with self.transition():
                    record.update(device=info.st_dev, inode=info.st_ino)
            if (not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path
                    or (info.st_dev, info.st_ino) != (record["device"], record["inode"])):
                raise IsolationError("owned directory identity differs")
            if key == "probe_control":
                names = set(os.listdir(path))
                if names - {"GO"} or ("GO" in names and not self.ledger["probe"]["go_intent"]):
                    raise IsolationError("probe control content is unknown")
                if "GO" in names:
                    gate = (path / "GO").lstat()
                    if not stat.S_ISREG(gate.st_mode) or gate.st_nlink != 1 or gate.st_size != 0:
                        raise IsolationError("probe release marker differs")
            prefix = str(path)
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                fields = line.split(" - ", 1)[0].split()
                if len(fields) < 5:
                    raise IsolationError("mount inventory is malformed")
                mount = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
                if mount == prefix or mount.startswith(prefix + "/"):
                    raise IsolationError("owned directory contains a mount")
            with self.transition(phase="cleanup"):
                record["remove_intent"] = True
            shutil.rmtree(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            with self.transition():
                record["absent"] = True
        except OSError as exc:
            raise IsolationError("owned directory cleanup remains unresolved") from exc

    def cleanup_cache(self) -> None:
        self._cleanup_directory("cache", "worker-cache")

    def cleanup_output(self) -> None:
        if self.ledger["import"] is not None and self.ledger["archive"] is None:
            raise IsolationError("imported output lacks its retained archive")
        self._cleanup_directory("output", "worker-output")

    def cleanup_probe_control(self) -> None:
        if self.ledger["probe_control"] is not None:
            self._cleanup_directory("probe_control", "probe-control")

    def remove_parent(self) -> None:
        parent = self.ledger["parent"]
        if parent["create_outcome"] in ("not_attempted", "not_released"):
            return
        if parent["snapshot"] is None or self.ledger["witness"] is None:
            raise IsolationError("containment removal lacks an exhaustion witness")
        if self.ledger["cache"] is not None and not self.ledger["cache"]["absent"]:
            raise IsolationError("containment removal requires cache cleanup")
        if self.ledger["output"] is not None and not self.ledger["output"]["absent"]:
            raise IsolationError("containment removal requires output cleanup")
        if self.ledger["probe_control"] is not None and not self.ledger["probe_control"]["absent"]:
            raise IsolationError("containment removal requires probe control cleanup")
        if parent_absent(parent["name"]):
            if not parent["stop_intent"]:
                raise IsolationError("containment disappeared before stop intent")
            with self.transition():
                parent["absent"] = True
            return
        verify_parent_snapshot(parent["snapshot"], parent_snapshot(parent["name"]), populated=0)
        with self.transition(phase="cleanup"):
            parent["stop_intent"] = True
        result = self.client("parent-stop", ["/usr/bin/systemctl", "stop", parent["name"]],
                             timeout=60, during_recovery=True)
        if result["exit_code"] or not parent_absent(parent["name"]):
            raise IsolationError("containment removal remains unresolved")
        with self.transition():
            parent["absent"] = True

    def reconcile_receipt(self) -> None:
        receipt = self.ledger["receipt"]
        if receipt is None:
            return
        if self.attempt["status"] == "registered" or receipt["state"] == "registered":
            raise IsolationError("registered receipt is immutable")
        if receipt["state"] == "revoked":
            return
        expected_dir = (".deployments", "product-builds" if self.ledger["kind"] == "source" else "product-wheel-builds")
        expected_path = "/".join((*expected_dir, receipt["sha256"] + ".json"))
        if receipt["path"] != expected_path:
            raise IsolationError("partial receipt path differs")
        path = self.root / expected_path
        try:
            _fd9(self.root)
            if os.path.lexists(path.parent):
                parent = path.parent.lstat()
                if not stat.S_ISDIR(parent.st_mode) or path.parent.resolve(strict=True) != path.parent:
                    raise IsolationError("partial receipt directory is unsafe")
            if os.path.lexists(path):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o222
                            or info.st_size > 16 * 1024 * 1024):
                        raise IsolationError("partial receipt is unsafe")
                    raw = b""
                    while len(raw) < info.st_size:
                        block = os.read(fd, min(_CHUNK, info.st_size - len(raw)))
                        if not block:
                            raise IsolationError("partial receipt is truncated")
                        raw += block
                    after = os.fstat(fd)
                    if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise IsolationError("partial receipt changed while reading")
                finally:
                    os.close(fd)
                value = _json(raw)
                if (hashlib.sha256(raw).hexdigest() != receipt["sha256"] or type(value) is not dict
                        or value.get("build_id") != self.ledger["build_id"]):
                    raise IsolationError("partial receipt binding differs")
                with self.transition(phase="receipt-reconcile"):
                    receipt["remove_intent"] = True
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    now = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
                        raise IsolationError("partial receipt changed before revocation")
                    os.unlink(path.name, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(directory)
                if os.path.lexists(path):
                    raise IsolationError("partial receipt remains")
            with self.transition():
                receipt["state"] = "revoked"
                self.attempt["receipt_sha256"] = None
        except OSError as exc:
            raise IsolationError("partial receipt reconciliation remains unresolved") from exc

    def discard_unimported_archive(self) -> None:
        archive = self.ledger["archive"]
        if archive is None:
            return
        self.require_context()
        if self.ledger["import"] is not None and self.ledger["import"]["released_generation"] is not None:
            raise IsolationError("a released import archive must be retained")
        path = self.root / archive["path"]
        if path != self.context / "sealed-image.tar":
            raise IsolationError("archive path differs")
        try:
            if os.path.lexists(path):
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) not in (0o600, 0o444)):
                    raise IsolationError("unimported archive is unsafe")
                if archive["state"] == "sealed" and _file_sha256(path) != archive["sha256"]:
                    raise IsolationError("unimported archive differs")
                with self.transition(phase="archive-reconcile"):
                    archive["remove_intent"] = True
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    now = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
                        raise IsolationError("archive changed before removal")
                    os.unlink(path.name, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(directory)
            with self.transition():
                archive["state"] = "discarded"
        except OSError as exc:
            raise IsolationError("unimported archive cleanup remains unresolved") from exc

    def register(self, receipt: dict[str, Any], receipts: tuple[str, ...]) -> str:
        """Atomically publish one immutable receipt after completed proof."""
        expected = (".deployments", "product-builds" if self.ledger["kind"] == "source" else "product-wheel-builds")
        if receipts != expected or type(receipt) is not dict or receipt.get("schema_version") != 2 \
                or receipt.get("build_id") != self.ledger["build_id"]:
            raise IsolationError("invalid producer receipt")
        image, reference, probe = self.ledger["import"], self.ledger["retained_reference"], self.ledger["probe"]
        if (type(image) is not dict or not image["verified"] or type(reference) is not dict
                or receipt.get("image_id") != image["native_image_id"]
                or receipt.get("image_ref") != reference["ref"] or self.ledger["witness"] is None
                or not self.ledger["parent"]["absent"] or not self.ledger["worker"]["absent"]
                or type(probe) is not dict or probe["bound"] is None or not probe["go_intent"] or not probe["absent"]
                or receipt.get("probe_container_id") != probe["id"]
                or receipt.get("probe_sha256") != probe["output_sha256"] or probe["output_sha256"] is None
                or any(self.ledger[key] is not None and not self.ledger[key]["absent"]
                       for key in ("cache", "output", "probe_control"))):
            raise IsolationError("producer receipt lacks completed proof")
        if self.inspect_reference(reference["ref"], image["native_image_id"]) != "exact_expected_binding":
            raise IsolationError("registered candidate reference is unavailable")
        raw = (json.dumps(receipt, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        with self.mutex:
            if self._poisoned or self.attempt["status"] != "active" or self.ledger["cancel_intent"] is not None:
                raise IsolationError("producer cannot register after cancellation")
            with self.transition(phase="receipt-intent"):
                self.ledger["receipt"] = {"path": "/".join((*receipts, digest + ".json")), "sha256": digest,
                                          "state": "intent", "remove_intent": False}
            import product_manifest as manifest  # noqa: PLC0415
            try:
                with manifest._directory(self.root, receipts, create=True) as directory:
                    manifest._write_file_unnamed(directory, digest + ".json", raw, reuse=False)
                    if manifest._read_file(directory, digest + ".json") != raw:
                        raise IsolationError("published receipt differs")
            except BaseException as exc:
                self._poisoned = True
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise IsolationError("receipt publication remains unresolved") from exc
            with self.transition():
                self.ledger["receipt"]["state"] = "registered"
                self.attempt.update(status="registered", phase="registered", receipt_sha256=digest, failure=None)
        return digest

    def verify_terminal(self) -> None:
        """Recheck a retryable terminal outcome without changing it."""
        if self.attempt["status"] not in ("failed", "retained_import"):
            raise IsolationError("producer attempt is not retryable terminal")
        current = preflight(self.root)
        if (current["daemon"] != self.ledger["binding"]["daemon"]
                or current["boot_id"] != self.ledger["binding"]["boot_id"]
                or current["binaries_sha256"] != self.ledger["binding"]["binaries_sha256"]):
            raise IsolationError("terminal producer daemon, boot, or tools changed")
        fenced = {item["generation"] for item in self.ledger["client_fences"]}
        if any(client["state"] != "completed" and client["generation"] not in fenced for client in self.ledger["clients"]):
            raise IsolationError("terminal producer has an unfenced client")
        parent, worker, probe = self.ledger["parent"], self.ledger["worker"], self.ledger["probe"]
        for resource, network in ((worker, "bridge"), (probe, "none")):
            if resource is None or not resource["create_intent"] or resource["create_outcome"] == "not_released":
                continue
            if (resource["create_outcome"] != "bound" or not resource["absent"]
                    or resource["id"] is None or self.inspect_container(resource["id"]) is not None):
                raise IsolationError("terminal producer retains an execution container")
            self._endpoint_absent(resource["id"], network)
        if parent["create_outcome"] == "bound":
            if not parent["absent"] or not parent_absent(parent["name"]) or self.ledger["witness"] is None:
                raise IsolationError("terminal producer containment remains")
            verify_parent_snapshot(parent["snapshot"], self.ledger["witness"]["parent"], populated=0)
        elif parent["create_outcome"] not in ("not_attempted", "not_released"):
            raise IsolationError("terminal producer creation is unresolved")
        for key in ("cache", "output", "probe_control"):
            record = self.ledger[key]
            if record is not None and (not record["absent"] or os.path.lexists(self.root / record["path"])):
                raise IsolationError("terminal producer retains nonretained staging")
        receipt = self.ledger["receipt"]
        if receipt is not None:
            path = self.root / receipt["path"]
            if os.path.lexists(path.parent):
                info = path.parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or path.parent.resolve(strict=True) != path.parent:
                    raise IsolationError("cancelled producer receipt directory is unsafe")
            if receipt["state"] != "revoked" or os.path.lexists(path):
                raise IsolationError("cancelled producer receipt remains")
        if self.attempt["receipt_sha256"] is not None:
            raise IsolationError("cancelled producer has a usable receipt")
        imported = self.ledger["import"]
        if self.attempt["status"] == "retained_import":
            if imported is None or imported["released_generation"] is None or not imported["verified"]:
                raise IsolationError("retained producer import is unresolved")
            with self.sealed_archive() as (_, proof):
                native = self._verify_image(proof)
            reference = self.ledger["retained_reference"]
            if reference is not None:
                self.inspect_reference(reference["ref"], native)
        elif imported is not None and imported["released_generation"] is not None:
            raise IsolationError("failed producer has a released image import")

    def reconcile(self, *, prior_fences: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Reconcile one cancelled attempt without starting replacement work."""
        _fd9(self.root)
        validate_ledger(self.root, self.ledger["kind"], self.ledger["build_id"], self.ledger)
        _saved_source(self.root, self.ledger["kind"], self.attempt, executing=True)
        if self.attempt["status"] == "registered":
            raise IsolationError("registered producer is immutable")
        if self.attempt["status"] in ("failed", "retained_import"):
            self.verify_terminal()
            return self.attempt
        if self.ledger["cancel_intent"] is None:
            self.cancel("Interrupted")
        current = preflight(self.root)
        if (current["daemon"] != self.ledger["binding"]["daemon"]
                or current["boot_id"] != self.ledger["binding"]["boot_id"]
                or current["binaries_sha256"] != self.ledger["binding"]["binaries_sha256"]):
            raise IsolationError("producer daemon, boot, or tools changed")
        self.bind_incomplete_context()
        self.reconcile_clients(prior_fences)
        self.resolve_creators()
        self.stop_worker()
        self.stop_probe()
        self.exhaustion_witness()
        imported = self.ledger["import"]
        retained = imported is not None and imported["released_generation"] is not None
        if retained:
            native_id = self.import_image(during_recovery=True)
            reference = self.ledger["retained_reference"]
            if reference is not None:
                self.inspect_reference(reference["ref"], native_id)
        else:
            self.discard_unimported_archive()
        self.reconcile_receipt()
        self.cleanup_probe_control()
        self.cleanup_cache()
        self.cleanup_output()
        self.remove_parent()
        with self.transition():
            self.attempt.update(status="retained_import" if retained else "failed",
                                phase="retained-import" if retained else "failed",
                                receipt_sha256=None, failure=self.ledger["cancel_intent"])
        return self.attempt


def _saved_source(root: Path, kind: str, attempt: dict[str, Any], *, executing: bool) -> dict[str, Any]:
    """Verify the saved capsule and, for recovery, the running exact-A source."""
    try:
        import component_recovery as recovery  # noqa: PLC0415
        import component_runtime as runtime  # noqa: PLC0415
        import deployment_admission as admission  # noqa: PLC0415
        import package_recovery_source as capsule  # noqa: PLC0415
        code_root = Path(__file__).resolve().parents[1]
        for module, name in ((recovery, "component_recovery.py"), (runtime, "component_runtime.py"),
                             (admission, "deployment_admission.py"), (capsule, "package_recovery_source.py")):
            if Path(module.__file__).resolve() != code_root / "scripts" / name:
                raise IsolationError("producer imported foreign recovery code")
        source = recovery._validate_source(attempt["recovery_source"])
        operations = attempt["source"] if kind == "source" else attempt["operations_source"]
        if type(operations) is not dict or any(source[key] != operations[key] for key in operations):
            raise IsolationError("producer saved source binding differs")
        recovery.capsule = capsule
        recovery._verify_bootstrap(root, source)
        recovery._verify_capsule(root, source)
        if executing:
            if code_root == Path(os.path.abspath(root)):
                if runtime.tool_bytes_identity(code_root) != source["tool_bytes_sha256"]:
                    raise IsolationError("live producer tools changed")
            else:
                admission._verify_recovery_extraction(root, source, code_root)
        return source
    except (OSError, KeyError, TypeError, ValueError, recovery.RecoveryError, runtime.JournalError,
            admission.AdmissionError) as exc:
        raise IsolationError("producer saved source cannot be verified") from exc


def verify_attempt_terminal(root: Path, kind: str, build_id: str,
                            read_attempt: Callable[[Path, str], dict[str, Any]]) -> dict[str, Any]:
    """Read-only admission witness for one failed or retained producer."""
    root = Path(os.path.abspath(root))
    parent_name(build_id)
    _fd9(root)
    attempt = read_attempt(root, build_id + ".json")
    if attempt.get("schema_version") != 2 or attempt.get("status") not in ("failed", "retained_import"):
        raise IsolationError("producer is not a versioned retryable terminal attempt")
    validate_ledger(root, kind, build_id, attempt["isolation"])
    _saved_source(root, kind, attempt, executing=False)
    session = ProducerSession(root, attempt, lambda _: (_ for _ in ()).throw(IsolationError("terminal verification wrote state")))
    session.verify_terminal()
    return attempt


def recover_attempt(root: Path, kind: str, build_id: str,
                    read_attempt: Callable[[Path, str], dict[str, Any]],
                    write_attempt: Callable[[Path, dict[str, Any]], None], *,
                    prior_fences: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Reconcile a single producer from its verified saved-A code under FD9."""
    root = Path(os.path.abspath(root))
    parent_name(build_id)
    _fd9(root)
    if Path(__file__).resolve().parents[1] == root:
        raise IsolationError("builder recovery requires verified extracted source")
    attempt = read_attempt(root, build_id + ".json")
    if attempt.get("schema_version") != 2:
        raise IsolationError("old producer attempts cannot execute this recovery")
    validate_ledger(root, kind, build_id, attempt["isolation"])
    source = _saved_source(root, kind, attempt, executing=True)
    reconcile_attempt_stage(root, kind, build_id, attempt)
    attempt = read_attempt(root, build_id + ".json")
    validate_ledger(root, kind, build_id, attempt["isolation"])
    source = _saved_source(root, kind, attempt, executing=True)
    try:
        import deployment_admission as admission  # noqa: PLC0415
        gate = getattr(admission, "admit_builder_recovery", None)
        if not callable(gate):
            raise IsolationError("builder recovery admission is unavailable")
        gate(root, kind=kind, build_id=build_id, source=source, code_root=Path(__file__).resolve().parents[1])
    except admission.AdmissionError as exc:
        raise IsolationError("builder recovery admission refused") from exc
    if attempt["status"] == "registered":
        if kind == "source":
            import component_build as producer  # noqa: PLC0415
            producer.verify_registered_build(root, attempt["receipt_sha256"])
        else:
            import component_wheel_build as producer  # noqa: PLC0415
            producer.verify_registered_wheel_build(root, attempt["receipt_sha256"])
        sync_registered_attempt(root, kind, build_id, attempt)
        return attempt
    session = ProducerSession(root, attempt, lambda value: write_attempt(root, value))
    return session.reconcile(prior_fences=prior_fences)


def request_cancel(root: Path, kind: str, build_id: str) -> dict[str, Any]:
    """Pre-lock request from verified saved A; fence dead-owner clients safely."""
    root = Path(os.path.abspath(root))
    parent_name(build_id)
    if Path(__file__).resolve().parents[1] == root:
        raise IsolationError("builder cancellation requires verified extracted source")
    if kind == "source":
        import component_build as producer  # noqa: PLC0415
        reader, writer, directory = producer._read_attempt, producer._write_attempt, "component-build-attempts"
    elif kind == "wheel":
        import component_wheel_build as producer  # noqa: PLC0415
        reader, writer, directory = producer.read_attempt, producer._write_attempt, "component-wheel-build-attempts"
    else:
        raise IsolationError("invalid producer kind")
    import component_runtime as runtime  # noqa: PLC0415
    path = root / ".deployments" / directory / (build_id + ".json")
    before = runtime._read_regular(path)
    attempt = reader(root, build_id + ".json")
    if attempt.get("schema_version") != 2:
        raise IsolationError("old producer attempts cannot execute cancellation")
    validate_ledger(root, kind, build_id, attempt["isolation"])
    _saved_source(root, kind, attempt, executing=True)
    if runtime._read_regular(path) != before:
        raise IsolationError("producer attempt changed during source verification")
    ledger = attempt["isolation"]
    fences: list[dict[str, Any]] = []
    if attempt["status"] == "active" and ledger["control"] is not None:
        result = fence_before_recovery(root, kind, build_id, ledger["control"], ledger["clients"])
        if result["status"] == "requested":
            return {"build_id": build_id, "status": "requested"}
        fences = result["clients"]
    else:
        if attempt["status"] == "active" and ledger["clients"]:
            raise IsolationError("active producer lacks a supervisor binding")
        acquire_recovery_fd9(root)
    if runtime._read_regular(path) != before:
        raise IsolationError("producer attempt changed before recovery lock")
    result = recover_attempt(root, kind, build_id, reader, writer, prior_fences=fences)
    return {"build_id": build_id, "status": result["status"]}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_client-barrier":
        raise SystemExit(_client_launcher(sys.argv[2:]))
    raise SystemExit(2)
