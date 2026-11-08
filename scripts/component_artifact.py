#!/usr/bin/env python3
"""Seal, verify and acquire completed component artifacts.

Two independent records are defined here and neither can stand in for the
other or for a schema-2 producer receipt:

* ``completed-component-bundle`` (schema 1): a sealed, nameless OCI archive
  plus the build claims needed to check it offline;
* ``completed-artifact-acquisition`` (schema 1): proof that a target Docker
  store holds exactly that graph under an inspected native image ID.

Verification is pure file parsing. It never starts a container, never uses
BuildKit and never trusts the build-time probe report: the image filesystem is
rebuilt from the layer tars (honouring whiteouts) and every installed file is
hashed again and compared with the sealed source archive or wheel bytes.

The only daemon change made by acquisition is one nameless
``POST /images/load?quiet=1``. Registration and cancellation decisions are
taken under a short artifact-record lock that is never held across a Docker
call. Mutating entry points refuse to run with file descriptor 9 open, so
they can never inherit the deployment lock.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import contextlib
import csv
import dataclasses
import email.parser
import fcntl
import gzip
import hashlib
import http.client
import io
import json
import marshal
import os
from pathlib import Path
import posixpath
import re
import secrets
import socket
import stat
import struct
import sys
import tarfile
import time
import tomllib
import types
from typing import Any, BinaryIO, Callable, Iterator
import zipfile

SOCKET = "/run/docker.sock"
BUNDLE_KIND = "completed-component-bundle"
ACQUISITION_KIND = "completed-artifact-acquisition"
INTENT_KIND = "completed-artifact-import-intent"
CANCEL_KIND = "completed-artifact-cancellation"
DELIVERY_STATE_KIND = "completed-artifact-delivery-state"
REPORT_PATH = "/usr/local/share/wuwaterm/install-probe.json"
PROGRAM_PATH = "/app/scripts/install_probe.py"
BUNDLE_FILE = "bundle.json"
ARCHIVE_FILE = "image.oci.tar"
REPORT_FILE = "install-probe.json"
PAYLOAD_FILES = (ARCHIVE_FILE, REPORT_FILE)
ARTIFACTS = (".deployments", "artifacts")

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT = re.compile(r"[0-9a-f]{40}\Z")
_ATTEMPT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]*\Z")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")
_USER_RE = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
_FORBIDDEN_ENV = {
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV"
}
_CONTRACT = re.compile(r"v[0-9]+\Z")
_CHUNK = 1024 * 1024
_MAX_JSON = 16 * 1024 * 1024
_MAX_CAPTURE = 16 * 1024 * 1024
_MAX_RESPONSE = 8 * 1024 * 1024

_INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
_MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
_CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
_LAYER_GZIP = {"application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"}
_LAYER_TAR = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.docker.image.rootfs.diff.tar"}
_NAME_ANNOTATIONS = ("org.opencontainers.image.ref.name", "io.containerd.image.name")

# product -> (repository, probe product, build-arg naming the source commit,
#             lock file, extra files mapped to their source path)
_PRODUCTS: dict[str, dict[str, Any]] = {
    "wuwaterm-api": {
        "repository": "My-Denia/WuwaTerm", "probe": "core", "revision_arg": "SOURCE_COMMIT",
        "lock": "uv.lock", "extra": {"/entrypoint.sh": "deploy/entrypoint.sh"},
        "source_packages": {"wuwaterm": "src"}, "target": "runtime",
    },
    "wuwaterm-telegram": {
        "repository": "My-Denia/WuwaTerm-Telegram", "probe": "telegram",
        "revision_arg": "WUWATERM_SOURCE_REVISION", "lock": "core-lock.json", "extra": {},
        "source_packages": {"wuwaterm-telegram": "src"}, "target": None,
    },
}
_BUILD_ARGS = {"SOURCE_COMMIT", "WUWATERM_SOURCE_REVISION", "PIP_NO_INDEX", "PIP_FIND_LINKS"}
# Files an installer writes itself; they are not wheel or source content.
_INSTALLER_FILES = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json", "uv_build.json"}


class ArtifactError(RuntimeError):
    """A bundle, acquisition or artifact record cannot be accepted."""


class CancelledError(ArtifactError):
    """The attempt carries a cancellation tombstone and cannot be registered."""


def _fail(message: str) -> None:
    raise ArtifactError(message)


# --------------------------------------------------------------------------
# canonical JSON and small validators
# --------------------------------------------------------------------------

def canonical(value: Any) -> bytes:
    """UTF-8, sorted keys, no insignificant whitespace, one trailing newline."""
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactError("value is not canonical JSON data") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail("non-finite JSON number"))
    except (UnicodeError, ValueError) as exc:
        raise ArtifactError("invalid JSON") from exc


def decode_canonical(raw: bytes) -> dict[str, Any]:
    value = _parse_json(raw)
    if type(value) is not dict or canonical(value) != raw:
        _fail("record is not canonical JSON")
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _exact(value: Any, keys: set[str], what: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail("invalid " + what + " fields")
    return value


def _hex(value: Any, what: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        _fail("invalid " + what)
    return value


def _digest(value: Any, what: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _fail("invalid " + what)
    return value


def _git(value: Any, what: str) -> str:
    if type(value) is not str or not _GIT.fullmatch(value):
        _fail("invalid " + what)
    return value


def _attempt_id(value: Any) -> str:
    if type(value) is not str or not _ATTEMPT.fullmatch(value):
        _fail("invalid attempt or acquisition ID")
    return value


def _str(value: Any, what: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        _fail("invalid " + what)
    return value


def _int(value: Any, what: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("invalid " + what)
    return value


def _bool(value: Any, what: str) -> bool:
    if type(value) is not bool:
        _fail("invalid " + what)
    return value


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id() -> str:
    """Never-reused identifier: ``<UTC>-<32 hex>``."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(16)


def refuse_inherited_fd9() -> None:
    """Mutating entry points must never run holding the deployment lock FD."""
    try:
        os.fstat(9)
    except OSError:
        return
    _fail("file descriptor 9 is open; artifact tools must not inherit the deployment lock (run with 9>&-)")


# --------------------------------------------------------------------------
# safe file reading
# --------------------------------------------------------------------------

def _read_regular(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ArtifactError("cannot open " + path.name) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            _fail(path.name + " is not a bounded regular file")
        chunks, total = [], 0
        while chunk := os.read(fd, _CHUNK):
            total += len(chunk)
            if total > limit:
                _fail(path.name + " exceeds its size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ArtifactError("cannot open " + path.name) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail(path.name + " is not a regular file")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _member_name(raw: str) -> str:
    name = raw[2:] if raw.startswith("./") else raw
    name = name.rstrip("/")
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        _fail("unsafe archive member name")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("unsafe archive member name")
    return name


class _HashingReader(io.RawIOBase):
    def __init__(self, stream: BinaryIO, limit: int | None = None) -> None:
        self._stream, self._limit = stream, limit
        self.digest, self.size = hashlib.sha256(), 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        want = len(buffer)
        if self._limit is not None:
            want = min(want, self._limit - self.size)
            if want <= 0:
                return 0
        data = self._stream.read(want)
        buffer[:len(data)] = data
        self.digest.update(data)
        self.size += len(data)
        return len(data)

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


# --------------------------------------------------------------------------
# filesystem reconstruction
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Node:
    kind: str            # "file" | "dir" | "symlink" | "other"
    mode: int
    size: int
    sha256: str | None
    link: str | None
    layer: int
    mtime: int = 0


class RootFS:
    """Effective image filesystem metadata reconstructed from layer tars."""

    def __init__(self, capture: Callable[[str], bool]) -> None:
        self.nodes: dict[str, Node] = {}
        self.content: dict[str, bytes] = {}
        self.layer_entries: list[list[tuple[str, str]]] = []
        self._capture = capture

    def _drop(self, path: str, *, keep_self: bool = False) -> None:
        prefix = path + "/"
        for key in [k for k in self.nodes if k.startswith(prefix)]:
            del self.nodes[key]
            self.content.pop(key, None)
        if not keep_self:
            self.nodes.pop(path, None)
            self.content.pop(path, None)

    def apply_layer(self, index: int, stream: BinaryIO) -> None:
        additions: list[tuple[str, Node, bytes | None, str | None]] = []
        whiteouts: list[str] = []
        opaque: list[str] = []
        listing: list[tuple[str, str]] = []
        seen: set[str] = set()
        try:
            with tarfile.open(fileobj=stream, mode="r|") as layer:
                for member in layer:
                    if member.name in (".", "./"):
                        # Root directory metadata only; it cannot hide or change a probed path.
                        if not member.isdir():
                            _fail("layer root entry is not a directory")
                        continue
                    name = _member_name(member.name)
                    if name in seen:
                        _fail("duplicate layer entry")
                    seen.add(name)
                    base = posixpath.basename(name)
                    parent = posixpath.dirname(name)
                    if base == ".wh..wh..opq":
                        opaque.append(parent)
                        listing.append((name, "opaque"))
                        continue
                    if base.startswith(".wh..wh."):
                        _fail("unsupported whiteout entry")
                    if base.startswith(".wh."):
                        whiteouts.append(posixpath.join(parent, base[4:]) if parent else base[4:])
                        listing.append((name, "whiteout"))
                        continue
                    mode = member.mode & 0o7777
                    mtime = int(member.mtime)
                    if member.isreg():
                        data = layer.extractfile(member)
                        if data is None:
                            _fail("layer file unavailable")
                        digest, size, kept = hashlib.sha256(), 0, []
                        keep = self._capture(name) and member.size <= _MAX_CAPTURE
                        while chunk := data.read(_CHUNK):
                            digest.update(chunk)
                            size += len(chunk)
                            if keep:
                                kept.append(chunk)
                        if size != member.size:
                            _fail("layer file truncated")
                        node = Node("file", mode, size, digest.hexdigest(), None, index, mtime)
                        additions.append((name, node, b"".join(kept) if keep else None, None))
                        listing.append((name, "file"))
                    elif member.isdir():
                        additions.append((name, Node("dir", mode, 0, None, None, index, mtime), None, None))
                        listing.append((name, "dir"))
                    elif member.issym():
                        additions.append((name, Node("symlink", mode, 0, None, member.linkname, index, mtime), None, None))
                        listing.append((name, "symlink"))
                    elif member.islnk():
                        additions.append((name, Node("file", mode, 0, None, None, index, mtime), None,
                                          _member_name(member.linkname)))
                        listing.append((name, "hardlink"))
                    else:
                        additions.append((name, Node("other", mode, 0, None, None, index, mtime), None, None))
                        listing.append((name, "other"))
        except tarfile.TarError as exc:
            raise ArtifactError("invalid layer tar") from exc
        # Whiteouts act on the lower layers only; additions of this layer win.
        for path in opaque:
            self._drop(path, keep_self=True)
        for path in whiteouts:
            self._drop(path)
        for name, node, data, hardlink in additions:
            parent = posixpath.dirname(name)
            while parent:
                existing = self.nodes.get(parent)
                if existing is not None and existing.kind != "dir":
                    _fail("layer entry is placed below a non-directory")
                parent = posixpath.dirname(parent)
            previous = self.nodes.get(name)
            if previous is not None and (previous.kind == "dir") != (node.kind == "dir"):
                self._drop(name)
            if hardlink is not None:
                target = self.nodes.get(hardlink)
                if target is None or target.kind != "file":
                    _fail("hard link target is not a regular file")
                node = dataclasses.replace(target, mode=node.mode, layer=index)
                data = self.content.get(hardlink)
            self.nodes[name] = node
            if data is not None:
                self.content[name] = data
            else:
                self.content.pop(name, None)
        self.layer_entries.append(listing)

    def resolve(self, path: str) -> str:
        """Resolve directory symlinks in the parents; the last component is not followed."""
        parts = [p for p in path.strip("/").split("/") if p]
        resolved: list[str] = []
        hops = 0
        while parts:
            part = parts.pop(0)
            if part == ".":
                continue
            if part == "..":
                if resolved:
                    resolved.pop()
                continue
            candidate = "/".join(resolved + [part])
            node = self.nodes.get(candidate)
            if parts and node is not None and node.kind == "symlink":
                hops += 1
                if hops > 40:
                    _fail("symlink loop in image filesystem")
                link = node.link or ""
                if link.startswith("/"):
                    resolved = []
                parts = [p for p in link.split("/") if p] + parts
                continue
            resolved.append(part)
        return "/".join(resolved)

    def regular(self, path: str) -> tuple[str, Node]:
        name = self.resolve(path)
        node = self.nodes.get(name)
        if node is None or node.kind != "file":
            _fail("image path is not a regular file: " + path)
        return name, node

    def read(self, path: str) -> bytes:
        name, node = self.regular(path)
        data = self.content.get(name)
        if data is None or _sha(data) != node.sha256:
            _fail("image file content was not captured: " + path)
        return data


def _capture(name: str) -> bool:
    return (name.endswith((".dist-info/RECORD", ".dist-info/METADATA",
                           ".dist-info/direct_url.json", ".dist-info/entry_points.txt",
                           ".py", ".pyc", ".pyo", ".pth", "/pyvenv.cfg", "pyvenv.cfg"))
            or "/bin/" in ("/" + name)
            or name == "entrypoint.sh"
            or name in (REPORT_PATH.lstrip("/"), PROGRAM_PATH.lstrip("/")))


# --------------------------------------------------------------------------
# OCI archive parsing
# --------------------------------------------------------------------------

@dataclasses.dataclass
class OciImage:
    archive_sha256: str
    archive_size: int
    index_digest: str
    manifest_digest: str
    config_digest: str
    config: dict[str, Any]
    layers: list[dict[str, Any]]
    platform: dict[str, Any]
    rootfs: RootFS

    def facts(self) -> dict[str, Any]:
        return {"archive_sha256": self.archive_sha256, "archive_size": self.archive_size,
                "index_digest": self.index_digest, "manifest_digest": self.manifest_digest,
                "config_digest": self.config_digest, "layers": [dict(x) for x in self.layers],
                "platform": dict(self.platform)}


def native_image_candidate(oci: dict[str, Any], containerd_snapshotter: bool) -> str:
    """The lookup key only; the store's inspect result decides.

    Measured on Engine 29.1.3 with the containerd snapshotter: the image ID is
    the manifest digest. The classic graph-driver store uses the config digest.
    """
    return oci["manifest_digest"] if containerd_snapshotter else oci["config_digest"]


def read_oci_archive(path: Path) -> OciImage:
    archive_sha, archive_size = _hash_file(path)
    try:
        with tarfile.open(path, mode="r:") as archive:
            entries: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                name = _member_name(member.name)
                if name in entries:
                    _fail("duplicate OCI archive entry")
                if member.isdir():
                    if name not in ("blobs", "blobs/sha256"):
                        _fail("unexpected OCI archive directory")
                elif not member.isfile():
                    _fail("unsafe OCI archive entry")
                elif name not in ("oci-layout", "index.json", "manifest.json") and not (
                        name.startswith("blobs/sha256/") and _HEX64.fullmatch(name[13:])):
                    _fail("unexpected OCI archive entry")
                entries[name] = member

            def read(name: str, limit: int = _MAX_JSON) -> bytes:
                member = entries.get(name)
                if member is None or not member.isfile() or member.size > limit:
                    _fail("OCI entry missing or oversized: " + name)
                handle = archive.extractfile(member)
                if handle is None:
                    _fail("OCI entry unavailable")
                data = handle.read(limit + 1)
                if len(data) != member.size:
                    _fail("OCI entry truncated")
                return data

            referenced: set[str] = {"oci-layout", "index.json", "blobs", "blobs/sha256"}

            def blob(descriptor: Any, media: set[str], *, data: bool = True) -> tuple[str, int, bytes | None]:
                if type(descriptor) is not dict or set(descriptor) - {"mediaType", "digest", "size", "annotations"}:
                    _fail("invalid OCI descriptor")
                if descriptor.get("mediaType") not in media:
                    _fail("unsupported OCI media type: " + str(descriptor.get("mediaType")))
                digest = _digest(descriptor.get("digest"), "OCI digest")
                size = _int(descriptor.get("size"), "OCI size")
                if "annotations" in descriptor:
                    _fail("annotated OCI blob descriptor")
                name = "blobs/sha256/" + digest[7:]
                member = entries.get(name)
                if member is None or member.size != size:
                    _fail("OCI blob missing or size differs")
                referenced.add(name)
                if not data:
                    return digest, size, None
                raw = read(name)
                if "sha256:" + _sha(raw) != digest:
                    _fail("OCI blob digest differs")
                return digest, size, raw

            if _parse_json(read("oci-layout", 1024)) != {"imageLayoutVersion": "1.0.0"}:
                _fail("unsupported OCI layout")
            index_raw = read("index.json")
            index = _parse_json(index_raw)
            if type(index) is not dict or set(index) - {"schemaVersion", "mediaType", "manifests", "annotations"}:
                _fail("invalid OCI index")
            if index.get("annotations"):
                _fail("annotated OCI index")
            if index.get("schemaVersion") != 2 or index.get("mediaType") != _INDEX_MEDIA \
                    or type(index.get("manifests")) is not list or len(index["manifests"]) != 1:
                _fail("OCI index must reference exactly one image")
            descriptor = index["manifests"][0]
            if type(descriptor) is not dict or set(descriptor) - {"mediaType", "digest", "size", "annotations", "platform"}:
                _fail("invalid OCI index descriptor")
            annotations = descriptor.get("annotations", {})
            if type(annotations) is not dict:
                _fail("invalid OCI annotations")
            for key in _NAME_ANNOTATIONS:
                if key in annotations:
                    _fail("OCI archive carries an image name annotation (" + key + ")")
            if set(annotations) - {"org.opencontainers.image.created"} \
                    or any(type(v) is not str for v in annotations.values()):
                _fail("unexpected OCI index annotation")
            plain = {k: descriptor.get(k) for k in ("mediaType", "digest", "size")}
            manifest_digest, _, manifest_raw = blob(plain, {_MANIFEST_MEDIA})
            image = _parse_json(manifest_raw or b"")
            if type(image) is not dict or set(image) != {"schemaVersion", "mediaType", "config", "layers"} \
                    or image["schemaVersion"] != 2 or image["mediaType"] != _MANIFEST_MEDIA:
                _fail("invalid OCI manifest, subject or annotations")
            config_digest, _, config_raw = blob(image["config"], {_CONFIG_MEDIA})
            config = _parse_json(config_raw or b"")
            if type(config) is not dict or type(config.get("rootfs")) is not dict \
                    or config["rootfs"].get("type") != "layers" or type(config["rootfs"].get("diff_ids")) is not list:
                _fail("invalid OCI config")
            platform = {"os": config.get("os"), "architecture": config.get("architecture"),
                        "variant": config.get("variant")}
            if type(platform["os"]) is not str or type(platform["architecture"]) is not str \
                    or platform["variant"] is not None and type(platform["variant"]) is not str:
                _fail("invalid OCI config platform")
            descriptor_platform = descriptor.get("platform")
            if descriptor_platform is not None and (
                    descriptor_platform.get("os") != platform["os"]
                    or descriptor_platform.get("architecture") != platform["architecture"]):
                _fail("OCI index platform differs from config")
            runtime_config = config.get("config") or {}
            if type(runtime_config) is not dict or runtime_config.get("Volumes"):
                _fail("OCI image declares volumes")
            layers_desc = image["layers"]
            diff_ids = config["rootfs"]["diff_ids"]
            if type(layers_desc) is not list or not layers_desc or len(layers_desc) != len(diff_ids):
                _fail("OCI layer count differs from config diff IDs")

            if "manifest.json" in entries:
                referenced.add("manifest.json")
                docker = _parse_json(read("manifest.json"))
                if type(docker) is not list or len(docker) != 1 or type(docker[0]) is not dict \
                        or set(docker[0]) - {"Config", "RepoTags", "Layers"}:
                    _fail("invalid docker archive manifest")
                tags = docker[0].get("RepoTags")
                if tags not in (None, []):
                    _fail("OCI archive carries RepoTags")
                if docker[0].get("Config") != "blobs/sha256/" + config_digest[7:] \
                        or docker[0].get("Layers") != ["blobs/sha256/" + d.get("digest", "")[7:]
                                                       for d in layers_desc if type(d) is dict]:
                    _fail("docker archive manifest differs from the OCI manifest")

            rootfs = RootFS(_capture)
            layers = []
            for number, (layer_desc, diff_id) in enumerate(zip(layers_desc, diff_ids, strict=True)):
                digest, size, _ = blob(layer_desc, _LAYER_GZIP | _LAYER_TAR, data=False)
                _digest(diff_id, "layer diff ID")
                member = entries["blobs/sha256/" + digest[7:]]
                handle = archive.extractfile(member)
                if handle is None:
                    _fail("OCI layer unavailable")
                compressed = _HashingReader(handle, size)
                if layer_desc["mediaType"] in _LAYER_GZIP:
                    inner: Any = gzip.GzipFile(fileobj=io.BufferedReader(compressed, _CHUNK), mode="rb")
                else:
                    inner = io.BufferedReader(compressed, _CHUNK)
                plain_reader = _HashingReader(inner)
                try:
                    rootfs.apply_layer(number, io.BufferedReader(plain_reader, _CHUNK))
                    plain_reader.drain()
                    compressed.drain()
                except (OSError, EOFError, gzip.BadGzipFile) as exc:
                    raise ArtifactError("invalid compressed layer") from exc
                if compressed.size != size or "sha256:" + compressed.digest.hexdigest() != digest:
                    _fail("OCI layer digest differs")
                if "sha256:" + plain_reader.digest.hexdigest() != diff_id:
                    _fail("OCI layer diff ID differs")
                layers.append({"digest": digest, "size": size, "diff_id": diff_id,
                               "media_type": layer_desc["mediaType"]})
            if set(entries) != referenced:
                _fail("unreferenced OCI archive content")
            return OciImage(archive_sha, archive_size, "sha256:" + _sha(index_raw), manifest_digest,
                            config_digest, config, layers, platform, rootfs)
    except ArtifactError:
        raise
    except (OSError, EOFError, tarfile.TarError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ArtifactError("invalid OCI archive") from exc


# --------------------------------------------------------------------------
# source archive (git archive bytes)
# --------------------------------------------------------------------------

@dataclasses.dataclass
class SourceArchive:
    sha256: str
    files: dict[str, tuple[int, str, int]]   # path -> (git mode, sha256, size)
    content: dict[str, bytes]
    tree: str
    commit: str | None


def _git_tree(entries: dict[str, tuple[bytes, bytes]]) -> str:
    """Compute a Git tree ID from {path: (mode, blob sha1 digest)}."""
    root: dict[str, Any] = {}
    for path, value in entries.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if type(child) is not dict:
                _fail("source archive path conflict")
            node = child
        if parts[-1] in node:
            _fail("source archive path conflict")
        node[parts[-1]] = value

    def tree(node: dict[str, Any]) -> bytes:
        items = []
        for name, value in node.items():
            raw = name.encode("utf-8")
            if type(value) is dict:
                items.append((raw + b"/", b"40000 " + raw + b"\0" + tree(value)))
            else:
                items.append((raw, value[0] + b" " + raw + b"\0" + value[1]))
        body = b"".join(item for _, item in sorted(items))
        return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).digest()
    return tree(root).hex()


def read_source_archive(path: Path, keep: Callable[[str], bool] = lambda _: True) -> SourceArchive:
    digest, _ = _hash_file(path)
    files: dict[str, tuple[int, str, int]] = {}
    content: dict[str, bytes] = {}
    git_entries: dict[str, tuple[bytes, bytes]] = {}
    try:
        with tarfile.open(path, mode="r:") as archive:
            commit = archive.pax_headers.get("comment")
            for member in archive:
                name = _member_name(member.name)
                if member.isdir():
                    continue
                if name in git_entries:
                    _fail("duplicate source archive entry")
                if member.issym():
                    link = member.linkname.encode("utf-8")
                    blob = hashlib.sha1(b"blob " + str(len(link)).encode() + b"\0" + link).digest()
                    git_entries[name] = (b"120000", blob)
                    continue
                if not member.isreg():
                    _fail("unsupported source archive entry")
                handle = archive.extractfile(member)
                data = handle.read() if handle is not None else b""
                if len(data) != member.size:
                    _fail("source archive entry truncated")
                mode = 0o100755 if member.mode & 0o111 else 0o100644
                files[name] = (mode, _sha(data), len(data))
                if keep(name):
                    content[name] = data
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).digest()
                git_entries[name] = (b"100755" if mode == 0o100755 else b"100644", blob)
    except ArtifactError:
        raise
    except (OSError, EOFError, tarfile.TarError, UnicodeError) as exc:
        raise ArtifactError("invalid source archive") from exc
    if commit is not None and not _GIT.fullmatch(commit):
        _fail("source archive commit header is invalid")
    return SourceArchive(digest, files, content, _git_tree(git_entries), commit)


def context_manifest_sha256(source: SourceArchive) -> str:
    entries = [{"path": p, "mode": oct(m)[2:], "sha256": s, "size": n} for p, (m, s, n) in sorted(source.files.items())]
    return _sha(canonical(entries))


def dockerfile_bases(text: str) -> list[str]:
    """External base image references, in order, excluding earlier stage names."""
    stages: set[str] = set()
    refs: list[str] = []
    for line in text.splitlines():
        words = line.strip().split()
        if not words or words[0].upper() != "FROM":
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        if not args:
            _fail("invalid FROM instruction")
        if args[0].lower() not in stages and args[0] not in refs:
            refs.append(args[0])
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return refs


# --------------------------------------------------------------------------
# wheels
# --------------------------------------------------------------------------

def _wheel_contents(path: Path, package: str, version: str) -> dict[str, str]:
    data = _read_regular(path, 512 * 1024 * 1024)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                _fail("duplicate wheel member")
            for name in names:
                if not name.endswith("/"):
                    _member_name(name)
            metadata = [n for n in names if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                _fail("wheel metadata missing")
            meta = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
            if _normalize(meta.get("Name", "")) != _normalize(package) or meta.get("Version") != version:
                _fail("wheel package or version differs")
            return {n: _sha(archive.read(n)) for n in names if not n.endswith("/")}
    except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ArtifactError("invalid wheel") from exc


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------
# bundle schema
# --------------------------------------------------------------------------

_BUNDLE_KEYS = {"schema_version", "kind", "attempt_id", "product", "repository", "version", "source", "tag",
                "recipe", "wheels", "core_binding", "oci", "install_probe", "build_result", "compatibility",
                "payload"}


def validate_bundle_schema(bundle: Any) -> dict[str, Any]:
    _exact(bundle, _BUNDLE_KEYS, "bundle")
    if bundle["schema_version"] != 1 or type(bundle["schema_version"]) is not int or bundle["kind"] != BUNDLE_KIND:
        _fail("unsupported bundle schema")
    _attempt_id(bundle["attempt_id"])
    product = _PRODUCTS.get(bundle["product"]) if type(bundle["product"]) is str else None
    if product is None:
        _fail("unknown bundle product")
    if bundle["repository"] != product["repository"]:
        _fail("bundle repository differs from product")
    if type(bundle["version"]) is not str or not _VERSION.fullmatch(bundle["version"]):
        _fail("invalid bundle version")
    source = _exact(bundle["source"], {"commit", "tree", "archive_sha256"}, "source")
    _git(source["commit"], "source commit")
    _git(source["tree"], "source tree")
    _hex(source["archive_sha256"], "source archive digest")
    if bundle["tag"] is not None:
        tag = _exact(bundle["tag"], {"object", "peeled_commit"}, "tag")
        _git(tag["object"], "tag object")
        if _git(tag["peeled_commit"], "tag commit") != source["commit"]:
            _fail("tag does not peel to the source commit")
    recipe = _exact(bundle["recipe"], {"dockerfile_sha256", "build_script_sha256", "context_manifest_sha256",
                                       "target", "platform", "build_args", "base_images", "lock_sha256"}, "recipe")
    for key in ("dockerfile_sha256", "build_script_sha256", "context_manifest_sha256", "lock_sha256"):
        _hex(recipe[key], "recipe " + key)
    if recipe["target"] != product["target"]:
        _fail("recipe target differs from product")
    if recipe["platform"] != "linux/amd64":
        _fail("unsupported recipe platform")
    if type(recipe["build_args"]) is not list:
        _fail("invalid build args")
    names = []
    for arg in recipe["build_args"]:
        _exact(arg, {"name", "value"}, "build arg")
        if arg["name"] not in _BUILD_ARGS or type(arg["value"]) is not str:
            _fail("build arg is not on the non-secret allowlist")
        names.append(arg["name"])
    if names != sorted(set(names)):
        _fail("build args must be unique and sorted")
    if {a["name"]: a["value"] for a in recipe["build_args"]}.get(product["revision_arg"]) != source["commit"]:
        _fail("build args do not bind the source commit")
    if type(recipe["base_images"]) is not list or not recipe["base_images"]:
        _fail("invalid base images")
    for base in recipe["base_images"]:
        _exact(base, {"ref", "digest"}, "base image")
        _str(base["ref"], "base image ref")
        _digest(base["digest"], "base image digest")
    if type(bundle["wheels"]) is not list:
        _fail("invalid wheels")
    seen = set()
    seen_packages = set()
    for wheel in bundle["wheels"]:
        _exact(wheel, {"filename", "package", "version", "sha256", "role"}, "wheel")
        filename = _str(wheel["filename"], "wheel filename")
        if "/" in filename or not filename.endswith(".whl") or filename in seen:
            _fail("invalid wheel filename")
        seen.add(filename)
        _str(wheel["package"], "wheel package")
        _str(wheel["version"], "wheel version")
        if not _VERSION.fullmatch(wheel["version"]):
            _fail("invalid wheel version")
        _hex(wheel["sha256"], "wheel digest")
        if wheel["role"] not in ("core", "telegram", "dependency"):
            _fail("invalid wheel role")
        pkg_norm = _normalize(wheel["package"])
        if pkg_norm in seen_packages:
            _fail("duplicate wheel package")
        seen_packages.add(pkg_norm)
        if wheel["role"] == "dependency" and pkg_norm in ("wuwaterm", "wuwaterm-telegram"):
            _fail("dependency wheel cannot be a WuwaTerm product: " + wheel["package"])
    binding = _exact(bundle["core_binding"], {"core_version", "core_wheel_sha256", "core_source_commit",
                                              "compatibility"}, "core binding")
    _str(binding["core_version"], "core version")
    if binding["compatibility"] != "exact":
        _fail("core binding must be exact")
    _git(binding["core_source_commit"], "core source commit")
    cores = [w for w in bundle["wheels"] if w["role"] == "core"]
    telegrams = [w for w in bundle["wheels"] if w["role"] == "telegram"]
    if bundle["product"] == "wuwaterm-api":
        # Core installs from its own source tree; no Core wheel exists.
        if binding["core_wheel_sha256"] is not None or cores or telegrams or binding["core_version"] != bundle["version"] \
                or binding["core_source_commit"] != source["commit"]:
            _fail("Core bundle must bind its own source as the Core")
    else:
        if len(cores) != 1 or cores[0]["package"] != "wuwaterm" \
                or binding["core_wheel_sha256"] != cores[0]["sha256"] \
                or binding["core_version"] != cores[0]["version"]:
            _fail("Telegram bundle must bind exactly one Core wheel")
        if not telegrams:
            _fail("Telegram bundle has no Telegram wheel")
        if len(telegrams) != 1 or telegrams[0]["package"] != "wuwaterm-telegram" \
                or telegrams[0]["version"] != bundle["version"]:
            _fail("Telegram bundle must bind exactly one Telegram wheel")
    oci = _exact(bundle["oci"], {"archive_sha256", "archive_size", "index_digest", "manifest_digest",
                                 "config_digest", "layers", "platform"}, "oci")
    _hex(oci["archive_sha256"], "archive digest")
    _int(oci["archive_size"], "archive size", 1)
    for key in ("index_digest", "manifest_digest", "config_digest"):
        _digest(oci[key], "oci " + key)
    if type(oci["layers"]) is not list or not oci["layers"]:
        _fail("invalid oci layers")
    for layer in oci["layers"]:
        _exact(layer, {"digest", "size", "diff_id", "media_type"}, "oci layer")
        _digest(layer["digest"], "layer digest")
        _digest(layer["diff_id"], "layer diff ID")
        _int(layer["size"], "layer size")
        if layer["media_type"] not in _LAYER_GZIP | _LAYER_TAR:
            _fail("invalid layer media type")
    platform = _exact(oci["platform"], {"os", "architecture", "variant"}, "oci platform")
    if platform["os"] != "linux" or platform["architecture"] != "amd64" or platform["variant"] is not None:
        _fail("oci platform differs from the recipe")
    probe = _exact(bundle["install_probe"], {"program_sha256", "report_sha256", "report_layer_digest", "report_path",
                                             "interpreter", "site_packages", "packages", "extra_files"},
                   "install probe")
    _hex(probe["program_sha256"], "probe program digest")
    _hex(probe["report_sha256"], "probe report digest")
    _digest(probe["report_layer_digest"], "probe layer digest")
    if probe["report_path"] != REPORT_PATH:
        _fail("unexpected probe report path")
    _str(probe["interpreter"], "interpreter")
    _str(probe["site_packages"], "site packages")
    if type(probe["packages"]) is not list or not probe["packages"]:
        _fail("invalid probed packages")
    for package in probe["packages"]:
        _exact(package, {"name", "version", "location", "files"}, "probed package")
        if type(package["files"]) is not list or not package["files"]:
            _fail("invalid probed files")
        for item in package["files"]:
            _exact(item, {"path", "sha256", "size"}, "probed file")
            _str(item["path"], "probed path")
            _hex(item["sha256"], "probed digest")
            _int(item["size"], "probed size")
    if type(probe["extra_files"]) is not list:
        _fail("invalid extra files")
    for item in probe["extra_files"]:
        _exact(item, {"path", "sha256", "mode"}, "extra file")
        _hex(item["sha256"], "extra file digest")
        _int(item["mode"], "extra file mode")
    result = _exact(bundle["build_result"], {"authority", "foreground_exit", "iid", "ci"}, "build result")
    if result["authority"] not in ("official-ci", "local-source"):
        _fail("invalid build authority")
    if type(result["foreground_exit"]) is not int or result["foreground_exit"] != 0:
        _fail("build did not exit successfully")
    _digest(result["iid"], "build image ID")
    if result["authority"] == "local-source":
        if result["ci"] is not None:
            _fail("local-source bundle must not claim CI evidence")
    else:
        ci = _exact(result["ci"], {"run_id", "run_attempt", "job", "workflow_sha256", "control_commit"}, "ci")
        _int(ci["run_id"], "run ID", 1)
        _int(ci["run_attempt"], "run attempt", 1)
        _str(ci["job"], "CI job")
        _hex(ci["workflow_sha256"], "workflow digest")
        _git(ci["control_commit"], "control commit")
    compat = _exact(bundle["compatibility"], {"api", "review", "manuscript", "db_schema", "telegram_state_protocol"},
                    "compatibility")
    for key in ("api", "review", "manuscript"):
        if type(compat[key]) is not str or not _CONTRACT.fullmatch(compat[key]):
            _fail("invalid compatibility " + key)
    _int(compat["db_schema"], "db schema", 1)
    if bundle["product"] == "wuwaterm-api":
        if compat["telegram_state_protocol"] is not None:
            _fail("Core bundle must not claim a Telegram state protocol")
    else:
        _int(compat["telegram_state_protocol"], "Telegram state protocol", 1)
    payload = bundle["payload"]
    if type(payload) is not list or [p.get("path") if type(p) is dict else None for p in payload] != list(PAYLOAD_FILES):
        _fail("bundle payload must be exactly image.oci.tar and install-probe.json")
    for item in payload:
        _exact(item, {"path", "sha256", "size"}, "payload entry")
        _hex(item["sha256"], "payload digest")
        _int(item["size"], "payload size", 1)
    if payload[0]["sha256"] != oci["archive_sha256"] or payload[0]["size"] != oci["archive_size"] \
            or payload[1]["sha256"] != probe["report_sha256"]:
        _fail("bundle payload differs from its OCI or probe claims")
    return bundle


# --------------------------------------------------------------------------
# probe report and installed-state recomputation
# --------------------------------------------------------------------------

def _validate_report(raw: bytes) -> dict[str, Any]:
    report = decode_canonical(raw)
    _exact(report, {"schema_version", "kind", "product", "interpreter", "python_version", "site_packages",
                    "packages", "extra_files"}, "probe report")
    if report["schema_version"] != 1 or report["kind"] != "install-probe":
        _fail("unsupported probe report")
    if type(report["packages"]) is not list or not report["packages"]:
        _fail("probe report lists no packages")
    for package in report["packages"]:
        _exact(package, {"name", "version", "location", "direct_url", "record_entries", "files"}, "probe package")
        _int(package["record_entries"], "record entries", 1)
        if type(package["files"]) is not list or not package["files"]:
            _fail("probe package lists no files")
        paths = [f.get("path") if type(f) is dict else None for f in package["files"]]
        if paths != sorted(set(paths)):
            _fail("probe files must be unique and sorted")
    return report


def _bundle_probe(report: dict[str, Any], program_sha256: str, report_sha256: str, layer: str) -> dict[str, Any]:
    return {
        "program_sha256": program_sha256, "report_sha256": report_sha256, "report_layer_digest": layer,
        "report_path": REPORT_PATH, "interpreter": report["interpreter"], "site_packages": report["site_packages"],
        "packages": [{"name": p["name"], "version": p["version"], "location": p["location"], "files": p["files"]}
                     for p in report["packages"]],
        "extra_files": report["extra_files"],
    }


def _check_final_layer(image: OciImage) -> None:
    """The probe RUN layer may add only the report and its parent directories."""
    allowed_dirs = {"usr", "usr/local", "usr/local/share", "usr/local/share/wuwaterm"}
    report = REPORT_PATH.lstrip("/")
    final = image.rootfs.layer_entries[-1]
    kinds = dict(final)
    if kinds.get(report) != "file":
        _fail("final layer does not add the probe report as a regular file")
    for name, kind in final:
        if name == report:
            continue
        if name in allowed_dirs and kind == "dir":
            continue
        _fail("final layer changes more than the probe report: " + name)


def _record_rows(raw: bytes) -> list[str]:
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    except (UnicodeError, csv.Error) as exc:
        raise ArtifactError("invalid RECORD") from exc
    paths = []
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            _fail("invalid RECORD row")
        paths.append(row[0])
    return paths


def _skipped(path: str) -> bool:
    return path.endswith((".pyc", ".pyo")) or "__pycache__" in path.split("/")


_COMP_NAMES = {"<listcomp>", "<dictcomp>", "<setcomp>"}


def _while_true_scopes(tree: ast.AST) -> set[str]:
    scopes: set[str] = set()

    def check(name: str, body: list[ast.stmt]) -> None:
        queue = list(body)
        while queue:
            node = queue.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
                scopes.add(name)
                break
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    queue.append(child)

    check("<module>", getattr(tree, "body", []))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            check(node.name, node.body)
    return scopes


def _extract_summary(co: types.CodeType, while_true_scopes: set[str]) -> tuple[
    set[tuple[str, Any]], set[str], set[str], list[types.CodeType], list[tuple[str, Any]], set[int]
]:
    prims: set[tuple[str, Any]] = set()
    ordered_prims: list[tuple[str, Any]] = []
    lines = {line for _, _, line in co.co_lines() if line is not None}
    names = set(co.co_names)
    locals_and_cells = (set(v for v in co.co_varnames if not v.startswith("."))
                        | set(co.co_cellvars)
                        | set(co.co_freevars)) - {"__class__"}
    children: list[types.CodeType] = []

    def visit_const(c: Any) -> None:
        if isinstance(c, types.CodeType):
            if c.co_name in _COMP_NAMES:
                sub_p, sub_n, sub_v, sub_ch, sub_o, sub_l = _extract_summary(c, while_true_scopes)
                prims.update(sub_p)
                names.update(sub_n)
                locals_and_cells.update(sub_v)
                children.extend(sub_ch)
                lines.update(sub_l)
            else:
                children.append(c)
        elif isinstance(c, (tuple, frozenset)):
            for x in c:
                visit_const(x)
        else:
            if c is None:
                return
            if isinstance(c, bool):
                if c is True and co.co_name in while_true_scopes:
                    return
                prims.add(("bool", c))
                ordered_prims.append(("bool", c))
                return
            if isinstance(c, str) and c.endswith((".<locals>.<listcomp>", ".<locals>.<dictcomp>", ".<locals>.<setcomp>")):
                return
            prims.add((type(c).__name__, c))
            ordered_prims.append((type(c).__name__, c))

    for c in co.co_consts:
        visit_const(c)
    return prims, names, locals_and_cells, children, ordered_prims, lines


def _compare_merged(co1: types.CodeType, co2: types.CodeType, while_true_scopes: set[str],
                    path: str = "", same_version: bool = False) -> list[str]:
    errs: list[str] = []
    for attr in ("co_name", "co_qualname", "co_filename", "co_firstlineno",
                 "co_argcount", "co_posonlyargcount", "co_kwonlyargcount"):
        v1, v2 = getattr(co1, attr, None), getattr(co2, attr, None)
        if v1 != v2:
            errs.append(f"{path}.{attr}: {v1!r} != {v2!r}")
    p1, n1, v1, ch1, o1, l1 = _extract_summary(co1, while_true_scopes)
    p2, n2, v2, ch2, o2, l2 = _extract_summary(co2, while_true_scopes)
    if n1 != n2:
        errs.append(f"{path}.names: {n1!r} != {n2!r}")
    if v1 != v2:
        errs.append(f"{path}.locals: {v1!r} != {v2!r}")
    if p1 != p2:
        errs.append(f"{path}.prims: {p1!r} != {p2!r}")
    if same_version:
        if o1 != o2:
            errs.append(f"{path}.ordered_prims: {o1!r} != {o2!r}")
        if l1 != l2:
            errs.append(f"{path}.lines: {l1!r} != {l2!r}")
    if len(ch1) != len(ch2):
        errs.append(f"{path}.children len: {len(ch1)} != {len(ch2)}")
    else:
        for c1, c2 in zip(ch1, ch2):
            child_name = getattr(c1, "co_qualname", c1.co_name)
            errs.extend(_compare_merged(c1, c2, while_true_scopes, f"{path}/{child_name}", same_version))
    return errs


def _verify_pyc(rootfs: RootFS, pyc_path: str, site: str, installed: dict[str, str], python_version: str) -> None:
    rel = pyc_path[len(site) + 1:]
    if rel.endswith(".pyo"):
        _fail("unexpected .pyo bytecode file: " + rel)
    parts = rel.split("/")
    py_parts = python_version.split(".")
    xy = "".join(py_parts[:2])
    expected_suffix = f".cpython-{xy}.pyc"
    if (len(parts) < 3
            or parts[-2] != "__pycache__"
            or "__pycache__" in parts[:-2]
            or not parts[-1].endswith(expected_suffix)):
        _fail("invalid or misplaced bytecode file: " + rel)
    mod = parts[-1][:-len(expected_suffix)]
    if not mod.isidentifier():
        _fail("invalid module name in bytecode file: " + rel)
    py_rel = "/".join(parts[:-2] + [mod + ".py"])
    if py_rel not in installed:
        _fail("unrecorded or unverified source file for bytecode: " + rel)
    py_full = site + "/" + py_rel
    py_node = rootfs.nodes.get(py_full)
    if py_node is None or py_node.kind != "file":
        _fail("missing source file for bytecode: " + rel)
    raw = rootfs.read(pyc_path)
    py_bytes = rootfs.read(py_full)
    if len(raw) <= 16:
        _fail("stale or mismatched .pyc header: " + rel)
    magic, flags, mtime, size = struct.unpack("<4sIII", raw[:16])
    if flags != 0:
        _fail("hash-based .pyc bytecode is forbidden: " + rel)
    if xy == "311" and magic != b"\xa7\r\r\n":
        _fail("unexpected .pyc magic bytes: " + rel)
    if xy == "312" and magic != b"\xcb\r\r\n":
        _fail("unexpected .pyc magic bytes: " + rel)
    if mtime != (py_node.mtime & 0xFFFFFFFF) or size != (py_node.size & 0xFFFFFFFF):
        _fail("stale or mismatched .pyc header: " + rel)
    try:
        co_pyc = marshal.loads(raw[16:])
    except Exception:
        _fail("compiled .pyc bytecode differs from verified source: " + rel)
    if not isinstance(co_pyc, types.CodeType):
        _fail("compiled .pyc bytecode differs from verified source: " + rel)
    try:
        co_compiled = compile(py_bytes, "/" + py_full, "exec", dont_inherit=True, optimize=0)
    except Exception:
        _fail("compiled .pyc bytecode differs from verified source: " + rel)

    try:
        target_py = (int(py_parts[0]), int(py_parts[1]))
    except (ValueError, IndexError):
        _fail("invalid python version in probe report: " + python_version)

    same_version = sys.version_info[:2] == target_py
    if same_version:
        if co_pyc != co_compiled:
            _fail("compiled .pyc bytecode differs from verified source: " + rel)
        return

    if co_pyc == co_compiled:
        return

    try:
        tree = ast.parse(py_bytes)
    except Exception:
        _fail("cannot parse source file: " + py_full)
    while_true_scopes = _while_true_scopes(tree)
    errs = _compare_merged(co_pyc, co_compiled, while_true_scopes, rel, same_version)
    if errs:
        _fail("compiled .pyc bytecode differs from verified source: " + rel)


def _console_scripts(rootfs: RootFS, dist_info: str, pyproject: dict[str, Any] | None) -> dict[str, str]:
    ep_path = dist_info + "/entry_points.txt"
    ep_scripts: dict[str, str] | None = None
    if ep_path in rootfs.nodes:
        raw = rootfs.read(ep_path)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ArtifactError("invalid entry_points.txt") from exc
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        try:
            cp.read_string(text)
        except configparser.Error as exc:
            raise ArtifactError("invalid entry_points.txt") from exc
        if cp.has_section("console_scripts"):
            ep_scripts = {k.strip(): v.strip() for k, v in cp.items("console_scripts")}
        else:
            ep_scripts = {}

    toml_scripts: dict[str, str] = {}
    if pyproject is not None:
        proj = pyproject.get("project")
        if isinstance(proj, dict) and "scripts" in proj:
            scripts = proj["scripts"]
            if not isinstance(scripts, dict):
                _fail("project.scripts in pyproject.toml must be a table")
            for k, v in scripts.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    _fail("project.scripts keys and values must be strings")
            toml_scripts = {k.strip(): v.strip() for k, v in scripts.items()}
        if toml_scripts and ep_scripts is None:
            _fail("installed distribution is missing entry_points.txt for project.scripts")
        if ep_scripts is not None and ep_scripts != toml_scripts:
            _fail("installed entry_points.txt differs from pyproject.toml scripts")

    return ep_scripts if ep_scripts is not None else toml_scripts


def _verify_console_launcher(rootfs: RootFS, target: str, entry_point: str, interpreter: str,
                             installed: dict[str, str] | None = None,
                             site: str | None = None) -> None:
    if posixpath.dirname(target) != posixpath.dirname(interpreter):
        _fail("console script launcher differs from entry point: " + target)
    if ":" not in entry_point:
        _fail("invalid console script entry point: " + entry_point)
    module, attr = [part.strip() for part in entry_point.split(":", 1)]
    if not module or not attr:
        _fail("invalid console script entry point: " + entry_point)
    if not all(part.isidentifier() for part in module.split(".")):
        _fail("invalid console script entry point: " + entry_point)
    if not attr.isidentifier():
        _fail("invalid console script entry point: " + entry_point)
    if installed is not None and site is not None:
        mod_rel = module.replace(".", "/")
        py_rel = (mod_rel + ".py") if (mod_rel + ".py") in installed else (
            (mod_rel + "/__init__.py") if (mod_rel + "/__init__.py") in installed else None
        )
        if py_rel is None:
            _fail("console script target module is not installed: " + module)
    _, node = rootfs.regular(target)
    if node.mode & 0o111 == 0:
        _fail("console script is not executable: " + target)
    raw = rootfs.read(target)
    pip_bytes = (
        f"#!{interpreter}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        f"from {module} import {attr}\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({attr}())\n"
    ).encode("utf-8")
    uv_bytes = (
        f"#!{interpreter}\n"
        "# -*- coding: utf-8 -*-\n"
        "import sys\n"
        f"from {module} import {attr}\n"
        'if __name__ == "__main__":\n'
        '    if sys.argv[0].endswith("-script.pyw"):\n'
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        '    elif sys.argv[0].endswith(".exe"):\n'
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        f"    sys.exit({attr}())\n"
    ).encode("utf-8")
    if raw not in (pip_bytes, uv_bytes):
        _fail("console script launcher differs from entry point: " + target)


def _verify_interpreter(rootfs: RootFS, report: dict[str, Any], product: dict[str, Any]) -> None:
    interpreter = _str(report["interpreter"], "interpreter")
    site_packages = _str(report["site_packages"], "site packages")
    if not interpreter.startswith("/") or "\\" in interpreter or posixpath.normpath(interpreter) != interpreter:
        _fail("interpreter path is not normalized: " + interpreter)
    if not site_packages.startswith("/") or "\\" in site_packages or posixpath.normpath(site_packages) != site_packages:
        _fail("site_packages path is not normalized: " + site_packages)
    allowed = ("/app/.venv/bin/python",) if product["probe"] == "core" else ("/usr/local/bin/python", "/app/.venv/bin/python")
    expected_prefix = posixpath.normpath(posixpath.join(site_packages, "../../.."))
    if interpreter not in allowed or interpreter != posixpath.join(expected_prefix, "bin", "python"):
        _fail("unexpected interpreter path: " + interpreter)
    current = rootfs.resolve(interpreter)
    if current not in rootfs.nodes:
        _fail("reported interpreter is missing in the image: " + interpreter)
    for _ in range(40):
        node = rootfs.nodes.get(current)
        if node is None:
            _fail("interpreter symlink target is missing in the image: " + interpreter)
        if node.kind == "file":
            if node.mode & 0o111 == 0:
                _fail("interpreter in the image is not executable: " + interpreter)
            return
        if node.kind != "symlink" or not node.link:
            _fail("interpreter in the image is not a regular file: " + interpreter)
        target = node.link if node.link.startswith("/") else posixpath.join(posixpath.dirname("/" + current), node.link)
        current = rootfs.resolve(target)
    _fail("symlink loop in interpreter path: " + interpreter)


def _verify_installed(image: OciImage, report: dict[str, Any], product: dict[str, Any],
                      source: SourceArchive, wheels: dict[str, dict[str, str]],
                      wheel_hashes: dict[str, str],
                      wheel_versions: dict[str, str] | None = None) -> None:
    rootfs = image.rootfs
    _verify_interpreter(rootfs, report, product)
    site = rootfs.resolve(report["site_packages"])
    if "etc/ld.so.preload" in rootfs.nodes:
        _fail("unauthorized /etc/ld.so.preload in image")
    if (site + "/sitecustomize.py") in rootfs.nodes:
        _fail("unauthorized sitecustomize.py in site-packages")
    if (site + "/usercustomize.py") in rootfs.nodes:
        _fail("unauthorized usercustomize.py in site-packages")
    for path in rootfs.nodes:
        base_prefix = posixpath.basename(path).split(".")[0]
        if base_prefix in ("sitecustomize", "usercustomize"):
            _fail(f"unauthorized {base_prefix} module in image: {path}")
    pycache_site = site + "/__pycache__/"
    for path in rootfs.nodes:
        if path.startswith(pycache_site):
            if posixpath.basename(path) != "typing_extensions.cpython-311.pyc":
                _fail("unexpected bytecode in site-packages: " + path)
        elif posixpath.dirname(path) == site and path.endswith((".pyc", ".pyo")):
            _fail("unexpected bytecode in site-packages: " + path)
    for path in rootfs.nodes:
        if path.endswith(".pth"):
            base = posixpath.basename(path)
            if base not in ("_virtualenv.pth", "distutils-precedence.pth"):
                if "site-packages" in path.split("/"):
                    _fail("unauthorized .pth file in site-packages: " + path)
                _fail("unauthorized .pth file in image: " + path)
            if base == "_virtualenv.pth":
                if rootfs.read(path).strip() != b"import _virtualenv":
                    _fail("unauthorized content in _virtualenv.pth: " + path)
                vpy_path = posixpath.join(posixpath.dirname(path), "_virtualenv.py")
                if vpy_path not in rootfs.nodes:
                    _fail("missing _virtualenv.py for _virtualenv.pth: " + vpy_path)
                if _sha(rootfs.read(vpy_path)) != "6cf30c56faf2a55228914dbbd17f8088ed371ebb08f5e7fa6fd931f913fcaf1d":
                    _fail("unauthorized content in _virtualenv.py: " + vpy_path)
            elif base == "distutils-precedence.pth":
                if _sha(rootfs.read(path)) != "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224":
                    _fail("unauthorized content in distutils-precedence.pth: " + path)
        elif posixpath.basename(path) == "_virtualenv.py":
            if _sha(rootfs.read(path)) != "6cf30c56faf2a55228914dbbd17f8088ed371ebb08f5e7fa6fd931f913fcaf1d":
                _fail("unauthorized content in _virtualenv.py: " + path)
    if report["interpreter"].startswith("/app/.venv/") or "app/.venv" in rootfs.nodes:
        if "app/.venv/pyvenv.cfg" not in rootfs.nodes:
            _fail("missing pyvenv.cfg in virtual environment: app/.venv")
        pyvenv_cfg = rootfs.read("app/.venv/pyvenv.cfg").decode("utf-8", "replace")
        kv = [
            (line.split("=", 1)[0].strip().lower(), line.split("=", 1)[1].strip().lower())
            for line in pyvenv_cfg.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "=" in line
        ]
        if [v for k, v in kv if k == "include-system-site-packages"] != ["false"]:
            _fail("pyvenv.cfg must set include-system-site-packages = false")
    for path, node in rootfs.nodes.items():
        if posixpath.dirname(path) == "app":
            base = posixpath.basename(path)
            if node.kind == "dir":
                if base not in (".venv", "build", "scripts", "src", "wheels"):
                    _fail("unauthorized directory in /app: " + path)
            else:
                if base.endswith((".py", ".pyc", ".pyo", ".pth", ".so", ".pyd", ".egg", ".whl")):
                    _fail("unauthorized executable or package file directly in /app: " + path)
        if any(path.startswith(p) for p in ("app/scripts/", "app/src/", "app/build/")):
            if path.endswith((".pyc", ".pyo")) or "__pycache__" in path.split("/"):
                _fail("unexpected bytecode in app directory: " + path)
        if (path.startswith("app/scripts/") or path.startswith("app/src/")) and path.endswith(".py"):
            rel = path[4:]
            if rel not in source.files or source.files[rel][1] != node.sha256:
                _fail("unverified Python file in /app: " + path)
    verified_bin_scripts: set[str] = set()
    names = [p["name"] for p in report["packages"]]
    if names != sorted(set(names)):
        _fail("probe packages must be unique and sorted")
    expected_packages = set(wheels) | set(product["source_packages"])
    if set(names) != expected_packages:
        _fail("probed packages differ from expected product distributions")
    for package in report["packages"]:
        location = _str(package["location"], "package location")
        if not location.startswith("/") or posixpath.normpath(location) != location:
            _fail("package location is not normalized")
        if location != report["site_packages"]:
            _fail("package location differs from site_packages: " + package["name"])
        site = rootfs.resolve(location)
        site_node = rootfs.nodes.get(site)
        if site_node is None or site_node.kind != "dir":
            _fail("package location is missing in the image")
        dist_infos = []
        for path, node in rootfs.nodes.items():
            if node.kind == "dir" and posixpath.dirname(path) == site and path.endswith(".dist-info"):
                meta = path + "/METADATA"
                if meta in rootfs.nodes:
                    parsed = email.parser.BytesParser().parsebytes(rootfs.read(meta))
                    if _normalize(parsed.get("Name", "")) == _normalize(package["name"]):
                        dist_infos.append((path, parsed))
        if len(dist_infos) != 1:
            _fail("installed distribution metadata is missing or ambiguous: " + package["name"])
        dist_info, meta = dist_infos[0]
        if meta.get("Version") != package["version"]:
            _fail("installed version differs from probe report: " + package["name"])
        record_paths = _record_rows(rootfs.read(dist_info + "/RECORD"))
        for p in record_paths:
            if not p or p.startswith("/") or "\\" in p or posixpath.normpath(p) != p or p in (".", ".."):
                _fail("invalid or unsafe path in RECORD: " + p)
        if len(record_paths) != package["record_entries"]:
            _fail("probe RECORD entry count differs: " + package["name"])
        expected = sorted({p for p in record_paths if not _skipped(p)})
        if [f["path"] for f in package["files"]] != expected:
            _fail("probe file list differs from installed RECORD: " + package["name"])
        installed: dict[str, str] = {}
        for item in package["files"]:
            p = item["path"]
            if not p or p.startswith("/") or "\\" in p or posixpath.normpath(p) != p or p in (".", ".."):
                _fail("invalid or unsafe path in probed file: " + p)
            target = posixpath.normpath(posixpath.join(location, p))
            _, node = rootfs.regular(target)
            if node.sha256 != item["sha256"] or node.size != item["size"]:
                _fail("installed file differs from probe report: " + p)
            installed[p] = item["sha256"]
        for p in record_paths:
            if _skipped(p):
                full_p = site + "/" + p
                if full_p not in rootfs.nodes:
                    _fail("RECORD entry missing in image: " + p)
                _verify_pyc(rootfs, full_p, site, installed, report["python_version"])
        for path, node in rootfs.nodes.items():
            if (path == site or path.startswith(site + "/")) and node.kind not in ("dir", "file"):
                _fail("unsafe non-regular entry in site-packages: " + path)
        # Files placed in the distribution's package directories but omitted
        # from RECORD would still be importable.
        tops = {p.split("/", 1)[0] for p in installed if not p.startswith("../") and "/" in p}
        for top in tops:
            prefix = site + "/" + top + "/"
            for path, node in rootfs.nodes.items():
                if path.startswith(prefix) and node.kind != "dir":
                    relative = path[len(site) + 1:]
                    if not _skipped(relative):
                        if relative not in installed:
                            _fail("unrecorded file in installed package: " + relative)
                    else:
                        _verify_pyc(rootfs, path, site, installed, report["python_version"])
        direct_path = dist_info + "/direct_url.json"
        if direct_path in rootfs.nodes:
            expected_direct = _parse_json(rootfs.read(direct_path))
            if package["direct_url"] != expected_direct:
                _fail("installed direct_url differs from probe report: " + package["name"])
        else:
            if package["direct_url"] is not None:
                _fail("probe report claims direct_url but direct_url.json is missing: " + package["name"])
        pyproject: dict[str, Any] | None = None
        if package["name"] in product["source_packages"]:
            pyproject_raw = source.content.get("pyproject.toml")
            if pyproject_raw is not None:
                try:
                    pyproject = tomllib.loads(pyproject_raw.decode("utf-8"))
                except (UnicodeError, tomllib.TOMLDecodeError) as exc:
                    raise ArtifactError("cannot parse pyproject.toml") from exc
            else:
                pyproject = {}
        console_scripts = _console_scripts(rootfs, dist_info, pyproject)
        if (dist_info + "/entry_points.txt") in rootfs.nodes:
            if package["name"] == "wuwaterm-telegram" and console_scripts.get("wuwaterm-telegram") != "wuwaterm_telegram.cli:main":
                _fail("unexpected console script entry point for wuwaterm-telegram")
        if package["name"] in wheels:
            if wheel_versions is not None and package["version"] != wheel_versions.get(package["name"]):
                _fail("installed distribution version differs from wheel: " + package["name"])
            members = wheels[package["name"]]
            for path, digest in members.items():
                generated = path.split("/", 1)[0].endswith(".dist-info") and posixpath.basename(path) in _INSTALLER_FILES
                if generated or ".data/" in path:
                    continue
                if installed.get(path) != digest:
                    _fail("installed file differs from verified wheel: " + path)
            dist_info_name = posixpath.basename(dist_info)
            for path, digest in installed.items():
                if path.split("/", 1)[0] == dist_info_name and posixpath.basename(path) in _INSTALLER_FILES:
                    continue
                if path in members:
                    if members[path] != digest:
                        _fail("installed file differs from verified wheel: " + path)
                    continue
                if path.startswith("../"):
                    target = posixpath.normpath(posixpath.join(location, path))
                    if (posixpath.dirname(target) == posixpath.dirname(report["interpreter"])
                            and posixpath.basename(target) in console_scripts):
                        _verify_console_launcher(rootfs, target, console_scripts[posixpath.basename(target)],
                                                 report["interpreter"], installed=installed, site=site)
                        verified_bin_scripts.add(target)
                        continue
                    is_data_member = False
                    for m, m_digest in members.items():
                        if ".data/data/" in m and m_digest == digest:
                            data_rel = m.split(".data/data/", 1)[1]
                            if target == posixpath.normpath(posixpath.join(location, "../../..", data_rel)):
                                if (posixpath.dirname(target) == posixpath.dirname(report["interpreter"])
                                        or data_rel.startswith("bin/")):
                                    _fail("data file cannot target bin directory: " + path)
                                is_data_member = True
                                break
                    if is_data_member:
                        continue
                _fail("installed file is not in verified wheel: " + path)
            if console_scripts:
                for script_name, entry_point in console_scripts.items():
                    expected_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(report["interpreter"]), script_name)
                    )
                    expected_rel = posixpath.relpath(expected_target, location)
                    found = (expected_rel in installed) or any(
                        p.startswith("../") and posixpath.normpath(posixpath.join(location, p)) == expected_target
                        for p in installed
                    )
                    if not found:
                        _fail("declared console script is not installed: " + script_name)
                    _verify_console_launcher(rootfs, expected_target, entry_point, report["interpreter"],
                                             installed=installed, site=site)
                    verified_bin_scripts.add(expected_target)
            direct = package["direct_url"]
            if isinstance(direct, dict):
                archive_info = direct.get("archive_info")
                hashes = archive_info.get("hashes") if isinstance(archive_info, dict) else direct.get("hashes")
                if isinstance(hashes, dict) and "sha256" in hashes:
                    if hashes["sha256"] != wheel_hashes[package["name"]]:
                        _fail("installed distribution came from a different wheel: " + package["name"])
        if package["name"] in product["source_packages"]:
            root = product["source_packages"][package["name"]]
            tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
            hatch = tool.get("hatch") if isinstance(tool.get("hatch"), dict) else {}
            build = hatch.get("build") if isinstance(hatch.get("build"), dict) else {}
            targets = build.get("targets") if isinstance(build.get("targets"), dict) else {}
            wheel_cfg = targets.get("wheel") if isinstance(targets.get("wheel"), dict) else {}
            wheel_packages = wheel_cfg.get("packages")
            if wheel_packages is not None:
                if not isinstance(wheel_packages, list) or not wheel_packages:
                    _fail("wheel packages in pyproject.toml must be a non-empty list")
                expected_source_tops: set[str] = set()
                for entry in wheel_packages:
                    if not isinstance(entry, str) or not entry:
                        _fail("wheel package entry in pyproject.toml must be a string")
                    if posixpath.normpath(entry) != entry.strip("/") or ".." in entry.split("/"):
                        _fail("wheel package entry in pyproject.toml is not normalized: " + entry)
                    norm = posixpath.normpath(entry)
                    if not (norm == root or norm.startswith(root + "/")):
                        _fail("wheel package in pyproject.toml is outside the source root: " + entry)
                    rel_entry = norm[len(root) + 1:].strip("/") if norm.startswith(root + "/") else ""
                    if rel_entry:
                        expected_source_tops.add(rel_entry.split("/", 1)[0])
                    else:
                        expected_source_tops.update(
                            p[len(root) + 1:].split("/", 1)[0]
                            for p in source.files
                            if p.startswith(root + "/") and "/" in p[len(root) + 1:] and not _skipped(p)
                        )
            else:
                expected_source_tops = {
                    p[len(root) + 1:].split("/", 1)[0]
                    for p in source.files
                    if p.startswith(root + "/") and "/" in p[len(root) + 1:] and not _skipped(p)
                }
            setuptools = tool.get("setuptools") if isinstance(tool.get("setuptools"), dict) else {}
            st_packages = setuptools.get("packages") if isinstance(setuptools.get("packages"), dict) else {}
            st_find = st_packages.get("find") if isinstance(st_packages.get("find"), dict) else {}
            if "where" in st_find:
                where = st_find["where"]
                if where != [root]:
                    _fail(f"tool.setuptools.packages.find.where must be [{root!r}]")
            for p in source.files:
                if p.startswith(root + "/"):
                    rel = p[len(root) + 1:]
                    if "/" not in rel and not _skipped(p):
                        _fail("stray regular file directly in source root: " + p)
            all_tops = expected_source_tops | {t for t in tops if not t.endswith(".dist-info")}
            dist_info_base = posixpath.basename(dist_info)
            for path in installed:
                if path.startswith(dist_info_base + "/") or ".data/" in path:
                    continue
                if path.startswith("../"):
                    if package["name"] in wheels:
                        continue
                    target = posixpath.normpath(posixpath.join(location, path))
                    if (posixpath.dirname(target) == posixpath.dirname(report["interpreter"])
                            and posixpath.basename(target) in console_scripts):
                        _verify_console_launcher(rootfs, target, console_scripts[posixpath.basename(target)],
                                                 report["interpreter"], installed=installed, site=site)
                        verified_bin_scripts.add(target)
                        continue
                    is_data_file = False
                    st_data_files = setuptools.get("data-files") if isinstance(setuptools.get("data-files"), dict) else {}
                    for dest_dir, src_list in st_data_files.items():
                        if isinstance(src_list, list):
                            for src in src_list:
                                if isinstance(src, str):
                                    origin = source.files.get(src)
                                    if origin is not None and origin[1] == installed[path]:
                                        data_rel = posixpath.normpath(posixpath.join(dest_dir, posixpath.basename(src)))
                                        if target == posixpath.normpath(posixpath.join(location, "../../..", data_rel)):
                                            if (posixpath.dirname(target) == posixpath.dirname(report["interpreter"])
                                                    or data_rel.startswith("bin/")):
                                                _fail("data file cannot target bin directory: " + path)
                                            is_data_file = True
                                            break
                            if is_data_file:
                                break
                    if is_data_file:
                        continue
                    _fail("installed file is outside expected package directories: " + path)
                if "/" not in path or not any(path.startswith(top + "/") for top in all_tops):
                    _fail("installed file is outside expected package directories: " + path)
            for top in sorted(all_tops):
                prefix = root + "/" + top + "/"
                from_source = {p[len(root) + 1:]: v[1] for p, v in source.files.items()
                               if p.startswith(prefix) and not _skipped(p)}
                from_image = {p: d for p, d in installed.items() if p.startswith(top + "/")}
                if not from_source or from_source != from_image:
                    _fail("installed package differs from the sealed source archive: " + top)
            if not all_tops or not (tops - {t for t in tops if t.endswith(".dist-info")}):
                _fail("installed source package is empty")
            if console_scripts:
                for script_name, entry_point in console_scripts.items():
                    expected_target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(report["interpreter"]), script_name)
                    )
                    expected_rel = posixpath.relpath(expected_target, location)
                    found = (expected_rel in installed) or any(
                        p.startswith("../") and posixpath.normpath(posixpath.join(location, p)) == expected_target
                        for p in installed
                    )
                    if not found:
                        _fail("declared console script is not installed: " + script_name)
                    _verify_console_launcher(rootfs, expected_target, entry_point, report["interpreter"],
                                             installed=installed, site=site)
                    verified_bin_scripts.add(expected_target)
        if package["name"] not in wheels and package["name"] not in product["source_packages"]:
            _fail("no verified source or wheel for probed package: " + package["name"])
    bin_dir = posixpath.dirname(report["interpreter"]).lstrip("/")
    for name in ("wuwaterm", "wuwaterm-api", "wuwaterm-telegram"):
        candidate = posixpath.join(bin_dir, name)
        if candidate in rootfs.nodes and ("/" + candidate) not in verified_bin_scripts:
            _fail("unrecorded product console script in image: /" + candidate)
    extras = report["extra_files"]
    if type(extras) is not list or [e.get("path") if type(e) is dict else None for e in extras] != sorted(product["extra"]):
        _fail("probe extra files differ from the product contract")
    for item in extras:
        _exact(item, {"path", "sha256", "mode"}, "extra file")
        _, node = rootfs.regular(item["path"])
        if node.sha256 != item["sha256"] or node.mode != item["mode"]:
            _fail("extra file differs from probe report: " + item["path"])
        origin = source.files.get(product["extra"][item["path"]])
        if (origin is None or origin[1] != item["sha256"]
                or bool(origin[0] & 0o111) != bool(node.mode & 0o111)):
            _fail("extra file differs from the sealed source archive: " + item["path"])
        if node.mode & 0o111 == 0 or origin[0] & 0o111 == 0:
            _fail("extra file is not executable: " + item["path"])
        if not rootfs.read(item["path"]).startswith(b"#!/bin/sh\n"):
            _fail("extra file has invalid shebang: " + item["path"])
        if "bin/sh" in rootfs.nodes or "bin" in rootfs.nodes:
            current = rootfs.resolve("bin/sh")
            if current not in rootfs.nodes:
                _fail("shell is missing in the image: bin/sh")
            for _ in range(40):
                node = rootfs.nodes.get(current)
                if node is None:
                    _fail("shell symlink target is missing in the image: bin/sh")
                if node.kind == "file":
                    if node.mode & 0o111 == 0:
                        _fail("shell in the image is not executable: bin/sh")
                    break
                if node.kind != "symlink" or not node.link:
                    _fail("shell in the image is not a regular file: bin/sh")
                target = node.link if node.link.startswith("/") else posixpath.join(posixpath.dirname("/" + current), node.link)
                current = rootfs.resolve(target)
            else:
                _fail("symlink loop in shell path: bin/sh")
    entrypoint = (image.config.get("config") or {}).get("Entrypoint")
    if isinstance(entrypoint, list) and entrypoint and isinstance(entrypoint[0], str):
        if entrypoint[0].startswith("/"):
            try:
                _, ep_node = rootfs.regular(entrypoint[0])
            except ArtifactError:
                _fail("image Entrypoint is missing or not executable: " + entrypoint[0])
            if ep_node.mode & 0o111 == 0:
                _fail("image Entrypoint is missing or not executable: " + entrypoint[0])
        else:
            ep_script = posixpath.normpath(posixpath.join(posixpath.dirname(report["interpreter"]), entrypoint[0]))
            if ep_script not in verified_bin_scripts:
                _fail("image Entrypoint console script is not installed and verified: " + entrypoint[0])


# --------------------------------------------------------------------------
# bundle verification
# --------------------------------------------------------------------------

@dataclasses.dataclass
class VerifiedBundle:
    path: Path
    raw: bytes
    sha256: str
    bundle: dict[str, Any]

    @property
    def archive(self) -> Path:
        return self.path.parent / ARCHIVE_FILE


def _default_inputs(bundle_path: Path) -> Path:
    return bundle_path.parent.parent / "inputs"


def _verify_source(bundle: dict[str, Any], source_path: Path) -> SourceArchive:
    wanted = {"pyproject.toml", "deploy/Dockerfile", "scripts/install_probe.py", "core-lock.json"}
    source = read_source_archive(source_path, keep=lambda name: name in wanted)
    if source.sha256 != bundle["source"]["archive_sha256"]:
        _fail("source archive differs from the bundle")
    if source.tree != bundle["source"]["tree"]:
        _fail("source archive tree differs from the bundle")
    if source.commit != bundle["source"]["commit"]:
        _fail("source archive commit differs from the bundle")
    return source


def _verify_telegram_core_binding(source: SourceArchive, pyproject_data: dict[str, Any],
                                   core_binding: dict[str, Any], authority: str) -> None:
    raw_lock = source.content.get("core-lock.json")
    if raw_lock is None:
        _fail("source archive has no core-lock.json")
    lock_data = _parse_json(raw_lock)
    if not isinstance(lock_data, dict):
        _fail("core-lock.json is not a JSON object")
    if authority == "official-ci":
        if not lock_data:
            _fail("official-ci Telegram bundle requires non-empty core-lock.json")
        if lock_data.get("version") != core_binding["core_version"]:
            _fail("core-lock.json version differs from core binding")
        if lock_data.get("sha256") != core_binding["core_wheel_sha256"]:
            _fail("core-lock.json sha256 differs from core binding")
        lock_commit = lock_data.get("checkout_sha") or lock_data.get("source_commit")
        if lock_commit != core_binding["core_source_commit"]:
            _fail("core-lock.json source commit differs from core binding")
    elif authority == "local-source":
        if lock_data and "version" in lock_data:
            if lock_data.get("version") != core_binding["core_version"]:
                _fail("core-lock.json version differs from core binding")
    deps = pyproject_data.get("project", {}).get("dependencies") if isinstance(pyproject_data.get("project"), dict) else None
    if isinstance(deps, list):
        for dep in deps:
            if isinstance(dep, str):
                norm_dep = re.sub(r"\s+", "", dep)
                m = re.match(r"^wuwaterm==([^;,\s]+)", norm_dep, re.IGNORECASE)
                if m and m.group(1) != core_binding["core_version"]:
                    _fail("pyproject.toml wuwaterm dependency pin differs from core binding: " + m.group(1))


def verify_bundle(bundle_path: Path, *, source_archive: Path | None = None,
                  wheel_dir: Path | None = None) -> VerifiedBundle:
    """Verify a sealed bundle offline. Read-only; safe while FD9 is held."""
    bundle_path = Path(bundle_path)
    raw = _read_regular(bundle_path, _MAX_JSON)
    bundle = validate_bundle_schema(decode_canonical(raw))
    product = _PRODUCTS[bundle["product"]]
    directory = bundle_path.parent
    try:
        present = sorted(os.listdir(directory))
    except OSError as exc:
        raise ArtifactError("cannot list bundle directory") from exc
    if present != sorted((BUNDLE_FILE,) + PAYLOAD_FILES) or bundle_path.name != BUNDLE_FILE:
        _fail("bundle directory must hold exactly bundle.json, image.oci.tar and install-probe.json")
    for item in bundle["payload"]:
        if _hash_file(directory / item["path"]) != (item["sha256"], item["size"]):
            _fail("bundle payload file differs: " + item["path"])
    inputs = _default_inputs(bundle_path)
    source = _verify_source(bundle, source_archive or inputs / "source.tar")
    recipe = bundle["recipe"]
    dockerfile = source.content.get("deploy/Dockerfile")
    if dockerfile is None or _sha(dockerfile) != recipe["dockerfile_sha256"]:
        _fail("recipe Dockerfile differs from the sealed source")
    if [b["ref"] for b in recipe["base_images"]] != dockerfile_bases(dockerfile.decode("utf-8", "replace")):
        _fail("recipe base images differ from the Dockerfile")
    lock = source.files.get(product["lock"])
    if lock is None or lock[1] != recipe["lock_sha256"]:
        _fail("recipe lock differs from the sealed source")
    if context_manifest_sha256(source) != recipe["context_manifest_sha256"]:
        _fail("recipe context manifest differs from the sealed source")
    program = source.content.get("scripts/install_probe.py")
    if program is None or _sha(program) != bundle["install_probe"]["program_sha256"]:
        _fail("probe program differs from the sealed source")
    pyproject_raw = source.content.get("pyproject.toml")
    if pyproject_raw is None:
        _fail("source archive has no pyproject.toml")
    try:
        pyproject_data = tomllib.loads(pyproject_raw.decode("utf-8"))
        proj_section = pyproject_data.get("project")
        if not isinstance(proj_section, dict):
            _fail("pyproject.toml has no project section")
        source_version = proj_section["version"]
        proj_name = proj_section.get("name", "")
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError("cannot read the source package version") from exc
    expected_name = "wuwaterm" if bundle["product"] == "wuwaterm-api" else "wuwaterm-telegram"
    if not isinstance(proj_name, str) or _normalize(proj_name) != expected_name:
        _fail("source pyproject.toml project name differs from expected product: " + str(proj_name))
    if source_version != bundle["version"]:
        _fail("bundle version differs from the sealed source")
    if bundle["product"] == "wuwaterm-telegram":
        _verify_telegram_core_binding(source, pyproject_data, bundle["core_binding"],
                                      bundle["build_result"]["authority"])
    wheels: dict[str, dict[str, str]] = {}
    wheel_hashes: dict[str, str] = {}
    wheel_versions: dict[str, str] = {}
    if bundle["wheels"]:
        folder = wheel_dir or inputs / "wheels"
        for wheel in bundle["wheels"]:
            path = folder / wheel["filename"]
            if _hash_file(path)[0] != wheel["sha256"]:
                _fail("wheel bytes differ from the bundle: " + wheel["filename"])
            if wheel["role"] in ("core", "telegram"):
                wheels[wheel["package"]] = _wheel_contents(path, wheel["package"], wheel["version"])
                wheel_hashes[wheel["package"]] = wheel["sha256"]
                wheel_versions[wheel["package"]] = wheel["version"]
            elif wheel["role"] == "dependency":
                _wheel_contents(path, wheel["package"], wheel["version"])
    image = read_oci_archive(directory / ARCHIVE_FILE)
    if image.facts() != bundle["oci"]:
        _fail("sealed OCI archive differs from the bundle")
    _verify_image(image, bundle, product, source, wheels, wheel_hashes, wheel_versions,
                  _read_regular(directory / REPORT_FILE, _MAX_JSON))
    return VerifiedBundle(bundle_path, raw, _sha(raw), bundle)


def _empty(value: Any) -> bool:
    return value in (None, "", [], {}, False)


def _verify_runtime_config(runtime_config: dict[str, Any], bundle: dict[str, Any], *,
                           dockerfile_text: str | None = None) -> None:
    if not isinstance(runtime_config, dict):
        _fail("runtime config is not an object")
    for field in ("Volumes", "OnBuild", "Healthcheck", "Shell"):
        if not _empty(runtime_config.get(field)):
            _fail(f"runtime config declares {field}")
    if runtime_config.get("WorkingDir") != "/app":
        _fail("runtime config WorkingDir must be /app")
    user = runtime_config.get("User")
    if user not in (None, "", "0", "0:0"):
        if not isinstance(user, str) or not _USER_RE.fullmatch(user):
            _fail("runtime config User is invalid: " + str(user))
    if bundle["product"] == "wuwaterm-api":
        if runtime_config.get("Entrypoint") != ["/entrypoint.sh"] or runtime_config.get("Cmd") != ["bot"]:
            _fail("invalid runtime config Entrypoint or Cmd for wuwaterm-api")
    elif bundle["product"] == "wuwaterm-telegram":
        expected_tg = (["wuwaterm-telegram"], ["--db", "/app/data/terms.db"])
        legacy_tg = (["/entrypoint.sh"], ["bot"])
        current_entry = (runtime_config.get("Entrypoint"), runtime_config.get("Cmd"))
        if dockerfile_text is not None and "ENTRYPOINT" in dockerfile_text:
            if current_entry != expected_tg:
                _fail("invalid runtime config Entrypoint or Cmd for wuwaterm-telegram")
        else:
            if current_entry not in (expected_tg, legacy_tg):
                _fail("invalid runtime config Entrypoint or Cmd for wuwaterm-telegram")
    labels = runtime_config.get("Labels")
    if labels is not None:
        if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
            _fail("runtime config Labels must be an object with string values")
    has_rev = isinstance(labels, dict) and "org.opencontainers.image.revision" in labels
    df_has_rev = dockerfile_text is not None and "org.opencontainers.image.revision" in dockerfile_text
    if has_rev or df_has_rev:
        if (labels or {}).get("org.opencontainers.image.revision") != bundle["source"]["commit"]:
            _fail("runtime config revision label differs from bundle source commit")
    env = runtime_config.get("Env")
    if env is not None:
        if not isinstance(env, list):
            _fail("runtime config Env must be a list")
        seen_keys: set[str] = set()
        for item in env:
            if not isinstance(item, str) or "=" not in item:
                _fail("invalid env entry: " + str(item))
            key, val = item.split("=", 1)
            if not _ENV_KEY_RE.fullmatch(key):
                _fail("invalid env key: " + key)
            if key in seen_keys:
                _fail("duplicate env key: " + key)
            seen_keys.add(key)
            if key in _FORBIDDEN_ENV:
                _fail("forbidden env variable: " + key)
            if key == "PATH":
                parts = val.split(":")
                if not parts or not all(p.startswith("/") and "\\" not in p and posixpath.normpath(p) == p for p in parts):
                    _fail("PATH variable entries must be normalized absolute paths")
                expected_bin = posixpath.dirname(bundle["install_probe"]["interpreter"])
                if parts[0] != expected_bin:
                    _fail("PATH does not start with interpreter bin directory: " + parts[0])


def _verify_image(image: OciImage, bundle: dict[str, Any], product: dict[str, Any], source: SourceArchive,
                  wheels: dict[str, dict[str, str]], wheel_hashes: dict[str, str],
                  wheel_versions: dict[str, str], report_copy: bytes) -> None:
    _check_final_layer(image)
    _verify_runtime_config(image.config.get("config") or {}, bundle,
                           dockerfile_text=source.content["deploy/Dockerfile"].decode("utf-8", "replace"))
    probe = bundle["install_probe"]
    if probe["report_layer_digest"] != image.layers[-1]["digest"]:
        _fail("probe report is not in the final layer")
    _, program_node = image.rootfs.regular(PROGRAM_PATH)
    if program_node.sha256 != probe["program_sha256"] or program_node.layer >= len(image.layers) - 1:
        _fail("probe program in the image differs from the sealed source")
    report_raw = image.rootfs.read(REPORT_PATH)
    if report_raw != report_copy or _sha(report_raw) != probe["report_sha256"]:
        _fail("probe report in the image differs from the bundle copy")
    report = _validate_report(report_raw)
    if report["product"] != product["probe"]:
        _fail("probe report product differs")
    if _bundle_probe(report, probe["program_sha256"], probe["report_sha256"], probe["report_layer_digest"]) != probe:
        _fail("bundle probe claims differ from the probe report")
    version = {p["name"]: p["version"] for p in report["packages"]}
    if bundle["product"] == "wuwaterm-api" and version.get("wuwaterm") != bundle["version"]:
        _fail("installed Core version differs from the bundle")
    if bundle["product"] == "wuwaterm-telegram":
        if version.get("wuwaterm") != bundle["core_binding"]["core_version"]:
            _fail("installed Core version differs from the Core binding")
        if version.get("wuwaterm-telegram") != bundle["version"]:
            _fail("installed Telegram version differs from the bundle")
    iid = bundle["build_result"]["iid"]
    if iid not in (image.manifest_digest, image.config_digest):
        _fail("build image ID differs from the sealed archive")
    _verify_installed(image, report, product, source, wheels, wheel_hashes, wheel_versions)


# --------------------------------------------------------------------------
# sealing (local-source)
# --------------------------------------------------------------------------

def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Exclusive, durable create: temp file, fsync, link, fsync parent."""
    temporary = path.parent / (".tmp-" + path.name + "-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, path)
    except FileExistsError:
        raise ArtifactError("record already exists: " + path.name) from None
    finally:
        os.unlink(temporary)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_or_match(path: Path, data: bytes) -> None:
    try:
        _write_new(path, data)
    except ArtifactError:
        if _read_regular(path, _MAX_JSON) != data:
            raise ArtifactError("existing record differs: " + path.name) from None


def _delivery_attempt(delivery: Path, attempt_id: str) -> Path:
    return Path(delivery) / "attempts" / _attempt_id(attempt_id)


def begin_delivery(delivery: Path, attempt_id: str, product: str) -> Path:
    refuse_inherited_fd9()
    if product not in _PRODUCTS:
        _fail("unknown product")
    attempt = _delivery_attempt(delivery, attempt_id)
    attempt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        attempt.mkdir(mode=0o700)
    except FileExistsError:
        raise ArtifactError("delivery attempt already exists; attempt IDs are never reused") from None
    for name in ("state", "inputs", "context", "bundle"):
        (attempt / name).mkdir(mode=0o700)
    _fsync_dir(attempt.parent)
    _write_new(attempt / "state" / "building.json",
               canonical({"schema_version": 1, "kind": DELIVERY_STATE_KIND, "attempt_id": attempt_id,
                          "state": "building", "product": product, "utc": _utc_now()}))
    return attempt


def seal_bundle(delivery: Path, attempt_id: str, *, product: str, commit: str, tree: str, iid: str,
                build_script_sha256: str, base_images: list[dict[str, str]], build_args: list[dict[str, str]],
                compatibility: dict[str, Any], tag: dict[str, str] | None = None,
                wheels: list[dict[str, str]] | None = None, core_binding: dict[str, Any] | None = None,
                authority: str = "local-source", ci: dict[str, Any] | None = None) -> VerifiedBundle:
    """Seal a built and saved image into a bundle, then verify it in full."""
    refuse_inherited_fd9()
    attempt = _delivery_attempt(delivery, attempt_id)
    state = attempt / "state"
    if not (state / "building.json").is_file():
        _fail("delivery attempt is missing building.json")
    if (state / "sealed.json").exists():
        _fail("delivery attempt is already sealed")
    building = decode_canonical(_read_regular(state / "building.json", _MAX_JSON))
    if building.get("state") != "building" or building.get("attempt_id") != attempt_id or building.get("product") != product:
        _fail("delivery attempt is not in the building state for this product and attempt")
    info = _PRODUCTS.get(product)
    if info is None:
        _fail("unknown product")
    source = read_source_archive(attempt / "inputs" / "source.tar",
                                 keep=lambda n: n in ("pyproject.toml", "deploy/Dockerfile",
                                                      "scripts/install_probe.py", "core-lock.json"))
    if source.tree != tree or source.commit != commit:
        _fail("source archive differs from the selected Git objects")
    if info["lock"] not in source.files:
        _fail("source archive is missing lock file: " + info["lock"])
    bundle_dir = attempt / "bundle"
    image = read_oci_archive(bundle_dir / ARCHIVE_FILE)
    report_raw = image.rootfs.read(REPORT_PATH)
    report = _validate_report(report_raw)
    pyproject_raw = source.content.get("pyproject.toml")
    if pyproject_raw is None:
        _fail("source archive has no pyproject.toml")
    try:
        pyproject_data = tomllib.loads(pyproject_raw.decode("utf-8"))
        proj_section = pyproject_data.get("project")
        if not isinstance(proj_section, dict):
            _fail("pyproject.toml has no project section")
        source_version = proj_section["version"]
        proj_name = proj_section.get("name", "")
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError("cannot read the source package version") from exc
    expected_name = "wuwaterm" if product == "wuwaterm-api" else "wuwaterm-telegram"
    if not isinstance(proj_name, str) or _normalize(proj_name) != expected_name:
        _fail("source pyproject.toml project name differs from expected product: " + str(proj_name))
    if product == "wuwaterm-api":
        version = source_version
    else:
        telegram_wheels = [w for w in (wheels or []) if w.get("role") == "telegram"]
        if not telegram_wheels:
            _fail("Telegram bundle has no Telegram wheel")
        version = telegram_wheels[0]["version"]
        if version != source_version:
            _fail("bundle version differs from the sealed source")
    binding = core_binding or {"core_version": version, "core_wheel_sha256": None,
                               "core_source_commit": commit, "compatibility": "exact"}
    if product == "wuwaterm-telegram":
        _verify_telegram_core_binding(source, pyproject_data, binding, authority)
    _write_new(bundle_dir / REPORT_FILE, report_raw, 0o644)
    program = source.content.get("scripts/install_probe.py")
    if program is None:
        _fail("source archive has no probe program")
    bundle = {
        "schema_version": 1, "kind": BUNDLE_KIND, "attempt_id": attempt_id, "product": product,
        "repository": info["repository"], "version": version,
        "source": {"commit": commit, "tree": tree, "archive_sha256": source.sha256}, "tag": tag,
        "recipe": {
            "dockerfile_sha256": _sha(source.content["deploy/Dockerfile"]),
            "build_script_sha256": build_script_sha256,
            "context_manifest_sha256": context_manifest_sha256(source),
            "target": info["target"], "platform": "linux/amd64",
            "build_args": sorted(build_args, key=lambda a: a["name"]),
            "base_images": base_images,
            "lock_sha256": source.files[info["lock"]][1],
        },
        "wheels": wheels or [],
        "core_binding": binding,
        "oci": image.facts(),
        "install_probe": _bundle_probe(report, _sha(program), _sha(report_raw), image.layers[-1]["digest"]),
        "build_result": {"authority": authority, "foreground_exit": 0, "iid": iid, "ci": ci},
        "compatibility": compatibility,
        "payload": [{"path": ARCHIVE_FILE, "sha256": image.archive_sha256, "size": image.archive_size},
                    {"path": REPORT_FILE, "sha256": _sha(report_raw), "size": len(report_raw)}],
    }
    validate_bundle_schema(bundle)
    _write_new(bundle_dir / BUNDLE_FILE, canonical(bundle), 0o644)
    verified = verify_bundle(bundle_dir / BUNDLE_FILE)
    _write_new(state / "sealed.json",
               canonical({"schema_version": 1, "kind": DELIVERY_STATE_KIND, "attempt_id": attempt_id,
                          "state": "sealed", "product": product, "utc": _utc_now(),
                          "bundle_sha256": verified.sha256}))
    return verified


# --------------------------------------------------------------------------
# Docker daemon boundary
# --------------------------------------------------------------------------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class DockerClient:
    """The three daemon calls acquisition needs: info, nameless load, inspect."""

    def __init__(self, socket_path: str = SOCKET, timeout: float = 900) -> None:
        self.socket_path, self.timeout = socket_path, timeout

    def _connection(self) -> _UnixHTTPConnection:
        try:
            if not stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                _fail("Docker endpoint is not a Unix socket")
        except OSError as exc:
            raise ArtifactError("Docker endpoint is unavailable") from exc
        return _UnixHTTPConnection(self.socket_path, self.timeout)

    def _json(self, method: str, path: str, allowed: tuple[int, ...] = (200,)) -> Any:
        connection = self._connection()
        try:
            connection.request(method, path)
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE + 1)
            if response.status not in allowed or len(raw) > _MAX_RESPONSE:
                _fail("Docker request failed: " + method + " " + path.split("?")[0])
            return None if response.status == 404 else json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ArtifactError("Docker request failed") from exc
        finally:
            connection.close()

    def info(self) -> dict[str, Any]:
        value = self._json("GET", "/info")
        if type(value) is not dict:
            _fail("invalid Docker info")
        return value

    def inspect_image(self, image_id: str) -> dict[str, Any] | None:
        _digest(image_id, "image ID")
        value = self._json("GET", "/images/" + image_id + "/json", (200, 404))
        if value is not None and type(value) is not dict:
            _fail("invalid image inspect")
        return value

    def load(self, archive: Path, expected_sha256: str, expected_size: int) -> None:
        connection = self._connection()
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if os.fstat(fd).st_size != expected_size:
                _fail("sealed archive size changed")
            digest = hashlib.sha256()
            connection.putrequest("POST", "/images/load?quiet=1")
            connection.putheader("Content-Type", "application/x-tar")
            connection.putheader("Content-Length", str(expected_size))
            connection.endheaders()
            sent = 0
            while chunk := os.read(fd, _CHUNK):
                digest.update(chunk)
                sent += len(chunk)
                connection.send(chunk)
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE + 1)
            if sent != expected_size or digest.hexdigest() != expected_sha256:
                _fail("sealed archive bytes changed while loading")
            if response.status != 200 or len(raw) > _MAX_RESPONSE:
                _fail("Docker image load failed")
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.strip():
                    message = json.loads(line)
                    if type(message) is dict and (message.get("error") or message.get("errorDetail")):
                        _fail("Docker image load reported an error")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ArtifactError("Docker image load failed") from exc
        finally:
            os.close(fd)
            connection.close()


def store_identity(docker: Any) -> dict[str, Any]:
    info = docker.info()
    daemon = info.get("ID")
    version = info.get("ServerVersion")
    driver = info.get("Driver")
    status = info.get("DriverStatus") or []
    if type(daemon) is not str or not daemon or type(version) is not str or type(driver) is not str \
            or type(status) is not list:
        _fail("Docker store identity is unavailable")
    snapshotter = any(type(row) is list and len(row) == 2 and row[0] == "driver-type"
                      and row[1] == "io.containerd.snapshotter.v1" for row in status)
    return {"daemon_id": _sha(daemon.encode("utf-8")), "engine_version": version,
            "storage_driver": driver, "containerd_snapshotter": snapshotter}


def verify_inspected_graph(inspected: dict[str, Any] | None, bundle: dict[str, Any], expected_id: str,
                           config: dict[str, Any] | None = None) -> str:
    """Require the store's image to be exactly the sealed graph; return its native ID."""
    if inspected is None:
        _fail("target store does not hold the expected image")
    if inspected.get("Id") != expected_id:
        _fail("target store returned a different image ID")
    rootfs = inspected.get("RootFS")
    if type(rootfs) is not dict or rootfs.get("Type") != "layers" \
            or rootfs.get("Layers") != [layer["diff_id"] for layer in bundle["oci"]["layers"]]:
        _fail("target store image graph differs from the bundle")
    if inspected.get("Os") != bundle["oci"]["platform"]["os"] \
            or inspected.get("Architecture") != bundle["oci"]["platform"]["architecture"]:
        _fail("target store image platform differs")
    actual = inspected.get("Config") or {}
    if type(actual) is not dict or not _empty(actual.get("Volumes")):
        _fail("target store image declares volumes")
    _verify_runtime_config(actual, bundle)
    if config is not None:
        for key, value in config.items():
            if actual.get(key) != value and not (_empty(value) and _empty(actual.get(key))):
                _fail("target store image config differs: " + key)
        for key, value in actual.items():
            if key not in config and not _empty(value):
                _fail("target store image config has an unsealed field: " + key)
    return expected_id


# --------------------------------------------------------------------------
# artifact store, lock and attempt state machine
# --------------------------------------------------------------------------

def _artifacts(root: Path, *, create: bool) -> Path:
    root = Path(root)
    deployments = root / ARTIFACTS[0]
    try:
        info = os.lstat(deployments)
    except OSError as exc:
        raise ArtifactError("deployment root has no .deployments directory") from exc
    if not stat.S_ISDIR(info.st_mode):
        _fail(".deployments is not a directory")
    path = deployments / ARTIFACTS[1]
    if create:
        for directory in (path, path / "attempts", path / "acquisitions", path / "bundles"):
            try:
                directory.mkdir(mode=0o700)
                _fsync_dir(directory.parent)
            except FileExistsError:
                pass
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                _fail("artifact store path is not a directory")
    return path


@contextlib.contextmanager
def artifact_lock(root: Path) -> Iterator[Path]:
    """Short record lock: never held across a Docker call. Order: FD9 -> artifact lock."""
    path = _artifacts(root, create=True)
    fd = os.open(path / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


_STATE_FILES = {"import-intent.json", "registered.json", "cancelled.json"}


def read_attempt_state(store: Path, attempt_id: str) -> dict[str, Any]:
    """Return {'state': none|import-intent|registered|cancelled, ...records}."""
    directory = store / "attempts" / _attempt_id(attempt_id)
    try:
        names = {n for n in os.listdir(directory) if not n.startswith(".tmp-")}
    except FileNotFoundError:
        return {"state": "none"}
    except OSError as exc:
        raise ArtifactError("cannot read attempt state") from exc
    if names - _STATE_FILES:
        _fail("unexpected file in attempt state")
    records = {n: decode_canonical(_read_regular(directory / n, _MAX_JSON)) for n in names}
    if "registered.json" in records and "cancelled.json" in records:
        _fail("attempt is both registered and cancelled")
    if "registered.json" in records:
        if "import-intent.json" not in records:
            _fail("registered attempt has no import intent")
        return {"state": "registered", "intent": records["import-intent.json"],
                "receipt": records["registered.json"], "receipt_raw": _read_regular(directory / "registered.json", _MAX_JSON)}
    if "cancelled.json" in records:
        return {"state": "cancelled", "cancellation": records["cancelled.json"],
                "intent": records.get("import-intent.json")}
    if "import-intent.json" in records:
        return {"state": "import-intent", "intent": records["import-intent.json"]}
    return {"state": "none"}


def _attempt_dir(store: Path, attempt_id: str) -> Path:
    directory = store / "attempts" / _attempt_id(attempt_id)
    try:
        directory.mkdir(mode=0o700)
        _fsync_dir(directory.parent)
    except FileExistsError:
        pass
    return directory


_INTENT_KEYS = {"schema_version", "kind", "attempt_id", "acquisition_id", "bundle_sha256", "archive_sha256",
                "archive_size", "expected_image_id", "store", "bundle_path", "created_utc"}
_RECEIPT_KEYS = {"schema_version", "kind", "acquisition_id", "bundle_sha256", "attempt_id", "store",
                 "native_image_id", "graph_equal", "state", "registered_utc"}


def validate_receipt(receipt: Any) -> dict[str, Any]:
    _exact(receipt, _RECEIPT_KEYS, "acquisition receipt")
    if receipt["schema_version"] != 1 or receipt["kind"] != ACQUISITION_KIND:
        _fail("unsupported acquisition receipt")
    _attempt_id(receipt["acquisition_id"])
    _attempt_id(receipt["attempt_id"])
    _hex(receipt["bundle_sha256"], "bundle digest")
    _validate_store(receipt["store"])
    _digest(receipt["native_image_id"], "native image ID")
    if receipt["graph_equal"] is not True or receipt["state"] != "registered":
        _fail("acquisition receipt is not a registered graph match")
    if type(receipt["registered_utc"]) is not str or not _UTC.fullmatch(receipt["registered_utc"]):
        _fail("invalid registration time")
    return receipt


def _validate_store(store: Any) -> dict[str, Any]:
    _exact(store, {"daemon_id", "engine_version", "storage_driver", "containerd_snapshotter"}, "store")
    _hex(store["daemon_id"], "daemon ID")
    _str(store["engine_version"], "engine version")
    _str(store["storage_driver"], "storage driver")
    _bool(store["containerd_snapshotter"], "snapshotter flag")
    return store


def _check_disjoint(bundle_path: Path, root: Path, attempt_id: str) -> None:
    """Require the delivery layout and a delivery root disjoint from the deployment root."""
    parts = Path(os.path.realpath(bundle_path)).parts
    if len(parts) < 5 or parts[-1] != BUNDLE_FILE or parts[-2] != "bundle" or parts[-3] != attempt_id \
            or parts[-4] != "attempts":
        _fail("bundle is not at <DELIVERY_ROOT>/attempts/<attempt_id>/bundle/bundle.json")
    delivery = str(Path(*parts[:-4]))
    deploy = os.path.realpath(root)
    if os.path.commonpath([delivery, deploy]) in (delivery, deploy):
        _fail("delivery workspace and deployment root must not overlap")


def _register(root: Path, verified: VerifiedBundle, intent: dict[str, Any], native_id: str) -> dict[str, Any]:
    receipt = {"schema_version": 1, "kind": ACQUISITION_KIND,
               "acquisition_id": intent["acquisition_id"],
               "bundle_sha256": verified.sha256, "attempt_id": intent["attempt_id"], "store": intent["store"],
               "native_image_id": native_id, "graph_equal": True, "state": "registered",
               "registered_utc": _utc_now()}
    validate_receipt(receipt)
    with artifact_lock(root) as store:
        state = read_attempt_state(store, intent["attempt_id"])
        if state["state"] == "cancelled":
            raise CancelledError("attempt was cancelled; it is not registered")
        if state["state"] == "registered":
            return {"state": "registered", "already_registered": True, "receipt": state["receipt"]}
        if state["state"] != "import-intent" or state["intent"] != intent:
            _fail("attempt import intent changed before registration")
        _write_or_match(store / "bundles" / (verified.sha256 + ".json"), verified.raw)
        data = canonical(receipt)
        _write_new(store / "attempts" / intent["attempt_id"] / "registered.json", data)
        _write_or_match(store / "acquisitions" / (receipt["acquisition_id"] + ".json"), data)
    return {"state": "registered", "already_registered": False, "receipt": receipt}


def _load_and_register(root: Path, verified: VerifiedBundle, intent: dict[str, Any], docker: Any,
                       *, replay: bool) -> dict[str, Any]:
    expected = intent["expected_image_id"]
    config = read_oci_config(verified)
    inspected = docker.inspect_image(expected) if replay else None
    if inspected is None:
        docker.load(verified.archive, intent["archive_sha256"], intent["archive_size"])
        inspected = docker.inspect_image(expected)
    native = verify_inspected_graph(inspected, verified.bundle, expected, config)
    return _register(root, verified, intent, native)


def read_oci_config(verified: VerifiedBundle) -> dict[str, Any]:
    """Return the sealed runtime config (already digest-checked by verify_bundle)."""
    digest = verified.bundle["oci"]["config_digest"]
    try:
        with tarfile.open(verified.archive, mode="r:") as archive:
            handle = archive.extractfile("blobs/sha256/" + digest[7:])
            raw = handle.read(_MAX_JSON + 1) if handle is not None else b""
    except (OSError, KeyError, tarfile.TarError) as exc:
        raise ArtifactError("cannot read the sealed image config") from exc
    if "sha256:" + _sha(raw) != digest:
        _fail("sealed image config differs")
    value = _parse_json(raw).get("config") or {}
    if type(value) is not dict:
        _fail("invalid sealed image config")
    return value


def acquire(bundle_path: Path, root: Path, *, docker: Any | None = None, source_archive: Path | None = None,
            wheel_dir: Path | None = None) -> dict[str, Any]:
    refuse_inherited_fd9()
    docker = docker or DockerClient()
    bundle_path = Path(bundle_path)
    verified = verify_bundle(bundle_path, source_archive=source_archive, wheel_dir=wheel_dir)
    bundle = verified.bundle
    _check_disjoint(bundle_path, root, bundle["attempt_id"])
    store_id = store_identity(docker)
    attempt_id = bundle["attempt_id"]
    intent = {"schema_version": 1, "kind": INTENT_KIND, "attempt_id": attempt_id, "acquisition_id": new_id(),
              "bundle_sha256": verified.sha256, "archive_sha256": bundle["oci"]["archive_sha256"],
              "archive_size": bundle["oci"]["archive_size"],
              "expected_image_id": native_image_candidate(bundle["oci"], store_id["containerd_snapshotter"]),
              "store": store_id, "bundle_path": os.path.realpath(bundle_path), "created_utc": _utc_now()}
    with artifact_lock(root) as store:
        state = read_attempt_state(store, attempt_id)
        if state["state"] == "cancelled":
            raise CancelledError("attempt was cancelled; retry with a new attempt")
        if state["state"] == "import-intent":
            _fail("attempt already has an import intent; use recover")
        if state["state"] != "registered":
            _write_or_match(store / "bundles" / (verified.sha256 + ".json"), verified.raw)
            _write_new(_attempt_dir(store, attempt_id) / "import-intent.json", canonical(intent))
    if state["state"] == "registered":
        return _ensure_registered_image(verified, validate_receipt(state["receipt"]), store_id, docker)
    return _load_and_register(root, verified, intent, docker, replay=False)


def _ensure_registered_image(verified: VerifiedBundle, receipt: dict[str, Any], store_id: dict[str, Any],
                             docker: Any) -> dict[str, Any]:
    """Re-acquiring a registered attempt restores a pruned image; records stay unchanged."""
    if receipt["bundle_sha256"] != verified.sha256:
        _fail("registered attempt is bound to a different bundle")
    if receipt["store"] != store_id:
        _fail("attempt is registered in a different Docker store")
    native = receipt["native_image_id"]
    inspected = docker.inspect_image(native)
    reloaded = inspected is None
    if reloaded:
        docker.load(verified.archive, verified.bundle["oci"]["archive_sha256"], verified.bundle["oci"]["archive_size"])
        inspected = docker.inspect_image(native)
    verify_inspected_graph(inspected, verified.bundle, native, read_oci_config(verified))
    return {"state": "registered", "already_registered": True, "reloaded": reloaded, "receipt": receipt}


def recover(root: Path, attempt_id: str, *, docker: Any | None = None, source_archive: Path | None = None,
            wheel_dir: Path | None = None) -> dict[str, Any]:
    refuse_inherited_fd9()
    with artifact_lock(root) as store:
        state = read_attempt_state(store, attempt_id)
        if state["state"] == "registered":
            receipt = validate_receipt(state["receipt"])
            bundle_copy = store / "bundles" / (receipt["bundle_sha256"] + ".json")
            if _sha(_read_regular(bundle_copy, _MAX_JSON)) != receipt["bundle_sha256"]:
                _fail("registered bundle copy differs")
            _write_or_match(store / "acquisitions" / (receipt["acquisition_id"] + ".json"), state["receipt_raw"])
            return {"state": "registered", "already_registered": True, "receipt": receipt}
        if state["state"] == "cancelled":
            return {"state": "cancelled", "cancellation": state["cancellation"]}
        if state["state"] == "none":
            # A crash between creating the attempt directory and writing its
            # intent leaves nothing to load. Tombstone it instead of leaving an
            # attempt no command can resolve.
            try:
                directory = os.lstat(store / "attempts" / _attempt_id(attempt_id))
            except FileNotFoundError:
                raise ArtifactError("attempt has no acquisition intent to recover") from None
            if not stat.S_ISDIR(directory.st_mode):
                raise ArtifactError("attempt has no acquisition intent to recover") from None
            return {"state": "cancelled", "cancellation": _tombstone(store, attempt_id, "none")}
        intent = _exact(state["intent"], _INTENT_KEYS, "import intent")
    docker = docker or DockerClient()
    if store_identity(docker) != intent["store"]:
        _fail("recovery must run against the store recorded in the import intent")
    verified = verify_bundle(Path(intent["bundle_path"]), source_archive=source_archive, wheel_dir=wheel_dir)
    if verified.sha256 != intent["bundle_sha256"]:
        _fail("bundle differs from the import intent")
    return _load_and_register(root, verified, intent, docker, replay=True)


def _tombstone(store: Path, attempt_id: str, prior_state: str) -> dict[str, Any]:
    """Write the terminal cancellation record for one attempt."""
    record = {"schema_version": 1, "kind": CANCEL_KIND, "attempt_id": attempt_id,
              "prior_state": prior_state, "cancelled_utc": _utc_now()}
    _write_new(_attempt_dir(store, attempt_id) / "cancelled.json", canonical(record))
    return record


def cancel(root: Path, attempt_id: str) -> dict[str, Any]:
    refuse_inherited_fd9()
    with artifact_lock(root) as store:
        state = read_attempt_state(store, attempt_id)
        if state["state"] == "registered":
            return {"state": "registered", "already_registered": True}
        if state["state"] == "cancelled":
            return {"state": "cancelled", "already_cancelled": True}
        _tombstone(store, attempt_id, state["state"])
    return {"state": "cancelled", "already_cancelled": False}


def verify_acquisition(receipt_path: Path, *, docker: Any | None = None, check_daemon: bool = True) -> dict[str, Any]:
    """Verify a registered receipt and its bundle copy. Read-only; safe under FD9.

    The artifact lock is taken only briefly to read the records.
    """
    receipt_path = Path(receipt_path)
    store = receipt_path.parent.parent
    if receipt_path.parent.name != "acquisitions" or store.name != ARTIFACTS[1] or store.parent.name != ARTIFACTS[0]:
        _fail("receipt is not inside a deployment artifact store")
    raw = _read_regular(receipt_path, _MAX_JSON)
    receipt = validate_receipt(decode_canonical(raw))
    if receipt_path.name != receipt["acquisition_id"] + ".json":
        _fail("receipt name differs from its acquisition ID")
    with artifact_lock(store.parent.parent) as locked:
        state = read_attempt_state(locked, receipt["attempt_id"])
        if state["state"] != "registered" or state["receipt_raw"] != raw:
            _fail("receipt is not the registered record of its attempt")
        bundle_raw = _read_regular(locked / "bundles" / (receipt["bundle_sha256"] + ".json"), _MAX_JSON)
    if _sha(bundle_raw) != receipt["bundle_sha256"]:
        _fail("registered bundle copy differs")
    bundle = validate_bundle_schema(decode_canonical(bundle_raw))
    if bundle["attempt_id"] != receipt["attempt_id"]:
        _fail("receipt attempt differs from its bundle")
    # The registered ID came from the store's inspect, which _register already
    # bound to this candidate. Rechecking it here binds the receipt to the
    # sealed manifest/config digests even when no daemon is consulted.
    if receipt["native_image_id"] != native_image_candidate(bundle["oci"], receipt["store"]["containerd_snapshotter"]):
        _fail("receipt native image ID differs from the sealed bundle digests")
    if check_daemon:
        docker = docker or DockerClient()
        if store_identity(docker) != receipt["store"]:
            _fail("receipt belongs to a different Docker store")
        verify_inspected_graph(docker.inspect_image(receipt["native_image_id"]), bundle, receipt["native_image_id"])
    return {"receipt": receipt, "receipt_sha256": _sha(raw), "bundle": bundle, "bundle_sha256": receipt["bundle_sha256"]}


def list_attempts(root: Path) -> list[dict[str, Any]]:
    store = _artifacts(root, create=False)
    try:
        names = sorted(os.listdir(store / "attempts"))
    except FileNotFoundError:
        return []
    result = []
    for name in names:
        state = read_attempt_state(store, name)
        result.append({"attempt_id": name, "state": state["state"]})
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _pairs_arg(values: list[str], key: str, value: str) -> list[dict[str, str]]:
    result = []
    for item in values:
        name, sep, rest = item.partition("=")
        if not sep or not name:
            raise ArtifactError("expected NAME=VALUE")
        result.append({key: name, value: rest})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--delivery", type=Path, required=True)
    begin.add_argument("--id", required=True)
    begin.add_argument("--product", choices=sorted(_PRODUCTS), required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--delivery", type=Path, required=True)
    seal.add_argument("--id", required=True)
    seal.add_argument("--product", choices=sorted(_PRODUCTS), required=True)
    seal.add_argument("--commit", required=True)
    seal.add_argument("--tree", required=True)
    seal.add_argument("--tag-object")
    seal.add_argument("--iid", required=True)
    seal.add_argument("--build-script-sha256", required=True)
    seal.add_argument("--base-image", action="append", default=[], help="REF=DIGEST")
    seal.add_argument("--build-arg", action="append", default=[], help="NAME=VALUE")
    seal.add_argument("--api", default="v1")
    seal.add_argument("--review", default="v2")
    seal.add_argument("--manuscript", default="v1")
    seal.add_argument("--db-schema", type=int, default=2)
    for name in ("verify-bundle",):
        verify = sub.add_parser(name)
        verify.add_argument("--bundle", type=Path, required=True)
        verify.add_argument("--source-archive", type=Path)
        verify.add_argument("--wheel-dir", type=Path)
    acq = sub.add_parser("acquire")
    acq.add_argument("--bundle", type=Path, required=True)
    acq.add_argument("--root", "--store", dest="root", type=Path, required=True)
    acq.add_argument("--source-archive", type=Path)
    acq.add_argument("--wheel-dir", type=Path)
    for name in ("cancel", "recover"):
        item = sub.add_parser(name)
        item.add_argument("--root", "--store", dest="root", type=Path, required=True)
        item.add_argument("--id", required=True)
        if name == "recover":
            item.add_argument("--source-archive", type=Path)
            item.add_argument("--wheel-dir", type=Path)
    receipt = sub.add_parser("verify")
    receipt.add_argument("--receipt", type=Path, required=True)
    receipt.add_argument("--no-daemon", action="store_true")
    listing = sub.add_parser("list")
    listing.add_argument("--root", "--store", dest="root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "begin":
            begin_delivery(args.delivery, args.id, args.product)
            result: Any = {"state": "building", "attempt_id": args.id}
        elif args.command == "seal":
            tag = None
            if args.tag_object:
                tag = {"object": args.tag_object, "peeled_commit": args.commit}
            bases = _pairs_arg(args.base_image, "ref", "digest")
            verified = seal_bundle(
                args.delivery, args.id, product=args.product, commit=args.commit, tree=args.tree, iid=args.iid,
                build_script_sha256=args.build_script_sha256, base_images=bases,
                build_args=_pairs_arg(args.build_arg, "name", "value"), tag=tag,
                compatibility={"api": args.api, "review": args.review, "manuscript": args.manuscript,
                               "db_schema": args.db_schema, "telegram_state_protocol": None})
            result = {"state": "sealed", "bundle": str(verified.path), "bundle_sha256": verified.sha256}
        elif args.command == "verify-bundle":
            verified = verify_bundle(args.bundle, source_archive=args.source_archive, wheel_dir=args.wheel_dir)
            result = {"verified": True, "bundle_sha256": verified.sha256, "attempt_id": verified.bundle["attempt_id"]}
        elif args.command == "acquire":
            result = acquire(args.bundle, args.root, source_archive=args.source_archive, wheel_dir=args.wheel_dir)
        elif args.command == "cancel":
            result = cancel(args.root, args.id)
        elif args.command == "recover":
            result = recover(args.root, args.id, source_archive=args.source_archive, wheel_dir=args.wheel_dir)
        elif args.command == "verify":
            checked = verify_acquisition(args.receipt, check_daemon=not args.no_daemon)
            result = {"verified": True, "receipt_sha256": checked["receipt_sha256"],
                      "native_image_id": checked["receipt"]["native_image_id"]}
        else:
            result = list_attempts(args.root)
    except CancelledError as exc:
        print("component artifact: " + str(exc), file=sys.stderr)
        return 3
    except (ArtifactError, OSError) as exc:
        print("component artifact: " + str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(canonical(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
