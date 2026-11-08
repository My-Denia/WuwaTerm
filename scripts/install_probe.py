#!/usr/bin/env python3
"""Record installed distribution files as a deterministic build-time report.

This program runs as the last filesystem-changing ``RUN`` step of an image
build. It reads ``RECORD`` through ``importlib.metadata`` and hashes the bytes
actually installed; it never trusts the hashes written in ``RECORD``. The only
file it writes is the report named by ``--out``. The report is a build claim:
acquisition reconstructs the image filesystem offline and recomputes every
value instead of trusting it.

The same bytes are used by the Core and Telegram images; the Core copy is the
authoritative source.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import posixpath
import stat
import sys
import sysconfig

SCHEMA_VERSION = 1
KIND = "install-probe"
_PRODUCTS = ("core", "telegram")


class ProbeError(RuntimeError):
    """The installed state cannot be reported exactly."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def _skipped(path: str) -> bool:
    # Compiled caches are removed or regenerated outside the package; they are
    # neither installed evidence nor a stable value.
    return path.endswith((".pyc", ".pyo")) or "__pycache__" in path.split("/")


def _hash_regular(path: Path) -> dict[str, object]:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProbeError("installed file is missing: " + str(path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProbeError("installed path is not a regular file: " + str(path))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size != info.st_size:
        raise ProbeError("installed file changed while hashing: " + str(path))
    return {"sha256": digest.hexdigest(), "size": size, "mode": stat.S_IMODE(info.st_mode)}


def _distribution(name: str) -> dict[str, object]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise ProbeError("distribution is not installed: " + name) from exc
    location = posixpath.normpath(str(dist.locate_file("")))
    if not location.startswith("/"):
        raise ProbeError("distribution location is not absolute: " + name)
    # Parse RECORD directly: Distribution.files silently drops missing files
    # on newer Python versions, which would hide an incomplete install.
    raw_record = dist.read_text("RECORD")
    if not raw_record:
        raise ProbeError("distribution has no RECORD: " + name)
    try:
        rows = [row for row in csv.reader(io.StringIO(raw_record)) if row]
    except csv.Error as exc:
        raise ProbeError("invalid RECORD: " + name) from exc
    if any(len(row) != 3 for row in rows):
        raise ProbeError("invalid RECORD row: " + name)
    entries = [row[0] for row in rows]
    files = []
    seen: set[str] = set()
    for entry in entries:
        relative = entry.replace("\\", "/")
        if _skipped(relative):
            continue
        if relative in seen:
            raise ProbeError("duplicate RECORD entry: " + relative)
        seen.add(relative)
        target = posixpath.normpath(posixpath.join(location, relative))
        item = _hash_regular(Path(target))
        files.append({"path": relative, "sha256": item["sha256"], "size": item["size"]})
    direct_url = None
    raw = dist.read_text("direct_url.json")
    if raw is not None:
        try:
            direct_url = json.loads(raw)
        except ValueError as exc:
            raise ProbeError("invalid direct_url.json: " + name) from exc
    return {
        "name": name,
        "version": dist.version,
        "location": location,
        "direct_url": direct_url,
        "record_entries": len(entries),
        "files": sorted(files, key=lambda item: item["path"]),
    }


def build_report(distributions: list[str], extra_files: list[str], product: str | None) -> dict[str, object]:
    if not distributions or len(set(distributions)) != len(distributions):
        raise ProbeError("distributions must be named once each")
    extras = []
    for raw in sorted(set(extra_files)):
        path = posixpath.normpath(raw)
        if not path.startswith("/") or path != raw:
            raise ProbeError("extra file must be a normalized absolute path: " + raw)
        item = _hash_regular(Path(path))
        extras.append({"path": path, "sha256": item["sha256"], "mode": item["mode"]})
    if len(extras) != len(extra_files):
        raise ProbeError("extra files must be named once each")
    purelib = posixpath.normpath(sysconfig.get_paths()["purelib"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "product": product,
        "interpreter": posixpath.normpath(sys.executable),
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "site_packages": purelib,
        "packages": [_distribution(name) for name in sorted(distributions)],
        "extra_files": extras,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", "--distributions", dest="dist", action="append", required=True,
                        help="installed distribution to record (repeatable)")
    parser.add_argument("--extra-file", dest="extra_file", action="append", default=[],
                        help="absolute path of an extra file to record (repeatable)")
    parser.add_argument("--out", "--output", dest="out", required=True)
    parser.add_argument("--product", choices=_PRODUCTS, default=None)
    args = parser.parse_args(argv)
    out = posixpath.normpath(args.out)
    if not out.startswith("/") or out != args.out:
        parser.error("--out must be a normalized absolute path")
    try:
        data = _canonical(build_report(args.dist, args.extra_file, args.product))
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(target, flags, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except (ProbeError, OSError) as exc:
        print("install probe failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
