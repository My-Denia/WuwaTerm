"""Completed-component bundle and acquisition: real formats, real records.

The OCI archives, layer tars, git-archive tars, wheels and probe reports here
are byte-level fixtures in the formats Docker and git actually produce. Only
the Docker daemon boundary (info / load / inspect) is replaced; every decision
function, lock and tombstone path runs for real.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import posixpath
import struct
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import component_artifact as art  # noqa: E402
import install_probe  # noqa: E402

# Every call here runs with descriptor 9 closed, the state the build script
# establishes with `exec 9>&-` before it touches an artifact entry point.
pytestmark = pytest.mark.closed_fd9

SITE = "/app/.venv/lib/python3.11/site-packages"
PROBE_BYTES = (ROOT / "scripts" / "install_probe.py").read_bytes()
ENTRYPOINT = b"#!/bin/sh\nexec \"$@\"\n"
DOCKERFILE = (b"FROM ghcr.io/astral-sh/uv:0.11.3 AS uv-bin\nFROM python:3.11-slim AS python-base\n"
              b"FROM python-base AS runtime\nRUN true\n")
COMMIT = "1" * 40
TG_COMMIT = "2" * 40
BASES = [{"ref": "ghcr.io/astral-sh/uv:0.11.3", "digest": "sha256:" + "a" * 64},
         {"ref": "python:3.11-slim", "digest": "sha256:" + "b" * 64}]
COMPAT = {"api": "v1", "review": "v2", "manuscript": "v1", "db_schema": 2, "telegram_state_protocol": None}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- tar builders

def tar_bytes(entries: list[tuple], *, pax: dict | None = None) -> bytes:
    """entries: (name, kind, data|linkname, mode)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT, pax_headers=pax or {}) as tar:
        for name, kind, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode, info.mtime = mode, 0
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, data
                tar.addfile(info)
    return buffer.getvalue()


def dirs_for(path: str) -> list[tuple]:
    parts = path.split("/")[:-1]
    return [("/".join(parts[:i + 1]), "dir", None, 0o755) for i in range(len(parts))]


def layer(files: dict[str, bytes], *, extra: list[tuple] = (), modes: dict[str, int] | None = None,
          omit_venv_files: set[str] | None = None) -> list[tuple]:
    omitted = omit_venv_files or set()
    files = {k: v for k, v in files.items() if k not in omitted}
    effective_modes = dict(modes or {})
    if any(k.startswith(SITE.lstrip("/") + "/") for k in files):
        if "app/.venv/bin/python" not in files and "app/.venv/bin/python" not in omitted:
            files["app/.venv/bin/python"] = b"#!/bin/sh\n"
            effective_modes.setdefault("app/.venv/bin/python", 0o755)
        elif "app/.venv/bin/python" in files and "app/.venv/bin/python" not in effective_modes:
            effective_modes["app/.venv/bin/python"] = 0o755

        if "app/.venv/pyvenv.cfg" not in files and "app/.venv/pyvenv.cfg" not in omitted:
            files["app/.venv/pyvenv.cfg"] = b"home = /usr/local/bin\ninclude-system-site-packages = false\n"

        if "entrypoint.sh" not in files and "entrypoint.sh" not in omitted:
            files["entrypoint.sh"] = ENTRYPOINT
            effective_modes.setdefault("entrypoint.sh", 0o755)
        elif "entrypoint.sh" in files and "entrypoint.sh" not in effective_modes:
            effective_modes["entrypoint.sh"] = 0o755

    if "entrypoint.sh" in files or "app/scripts/install_probe.py" in files or "app/.venv/bin/python" in files:
        if "bin/sh" not in files and "usr/bin/sh" not in files and "bin/sh" not in omitted:
            files["bin/sh"] = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
            effective_modes.setdefault("bin/sh", 0o755)
        elif "bin/sh" in files and "bin/sh" not in effective_modes:
            effective_modes["bin/sh"] = 0o755

    entries, seen = [], set()
    for path in sorted(files):
        for item in dirs_for(path):
            if item[0] not in seen:
                seen.add(item[0])
                entries.append(item)
        entries.append((path, "file", files[path], effective_modes.get(path, 0o644)))
    return entries + list(extra)


def oci_archive(layers: list[list[tuple]], *, repo_tags=None, annotations=None, config_extra=None,
                corrupt_layer: int | None = None, extra_entry: bool = False, media: str = "oci",
                config_digest_annotation: str = "absent", descriptor_media: str | None = None) -> bytes:
    """A saved image archive, in the OCI or the Docker v2 schema.

    ``media`` picks the schema the way a daemon or a builder would: Docker v2
    manifests name the Docker config type, OCI manifests name the OCI one.
    ``config_digest_annotation`` controls the index descriptor's
    ``config.digest``: absent, the exact digest, a different digest, or a value
    that is not a digest at all. ``descriptor_media`` overrides only the index
    descriptor's media type, to model a descriptor that disagrees with the
    blob it points at.
    """
    manifest_media = {"oci": "application/vnd.oci.image.manifest.v1+json",
                      "docker": "application/vnd.docker.distribution.manifest.v2+json"}[media]
    config_media = {"oci": "application/vnd.oci.image.config.v1+json",
                    "docker": "application/vnd.docker.container.image.v1+json"}[media]
    blobs: dict[str, bytes] = {}
    descriptors, diff_ids = [], []
    for number, entries in enumerate(layers):
        plain = tar_bytes(entries)
        compressed = gzip.compress(plain, mtime=0)
        digest = sha(compressed)
        if number == corrupt_layer:
            compressed = compressed[:-8] + bytes(8)
        blobs[digest] = compressed
        descriptors.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                            "digest": "sha256:" + digest, "size": len(compressed)})
        diff_ids.append("sha256:" + sha(plain))
    runtime = {"Entrypoint": ["/entrypoint.sh"], "Cmd": ["bot"], "WorkingDir": "/app"}
    runtime.update(config_extra or {})
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": runtime,
                         "rootfs": {"type": "layers", "diff_ids": diff_ids}}).encode()
    blobs[sha(config)] = config
    manifest = json.dumps({"schemaVersion": 2, "mediaType": manifest_media,
                           "config": {"mediaType": config_media,
                                      "digest": "sha256:" + sha(config), "size": len(config)},
                           "layers": descriptors}).encode()
    blobs[sha(manifest)] = manifest
    index_annotations = {"org.opencontainers.image.created": "2026-11-07T00:00:00Z"}
    index_annotations.update(annotations or {})
    if config_digest_annotation != "absent":
        index_annotations["config.digest"] = {
            "matching": "sha256:" + sha(config),
            "different": "sha256:" + "0" * 64,
            "malformed": "sha256:not-a-digest",
        }[config_digest_annotation]
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"mediaType": descriptor_media or manifest_media,
                            "digest": "sha256:" + sha(manifest), "size": len(manifest),
                            "annotations": index_annotations}]}
    docker = [{"Config": "blobs/sha256/" + sha(config), "RepoTags": repo_tags,
               "Layers": ["blobs/sha256/" + d["digest"][7:] for d in descriptors]}]
    entries = [("blobs", "dir", None, 0o755), ("blobs/sha256", "dir", None, 0o755)]
    entries += [("blobs/sha256/" + k, "file", v, 0o444) for k, v in sorted(blobs.items())]
    entries += [("index.json", "file", json.dumps(index).encode(), 0o644),
                ("manifest.json", "file", json.dumps(docker).encode(), 0o644),
                ("oci-layout", "file", b'{"imageLayoutVersion":"1.0.0"}', 0o444)]
    if extra_entry:
        entries.append(("repositories", "file", b"{}", 0o644))
    return tar_bytes(entries)


# ---------------------------------------------------------------- product fixtures

def dist(name: str, version: str, files: dict[str, bytes], direct_url: dict | None = None) -> dict[str, bytes]:
    """Installed distribution files relative to site-packages, with RECORD."""
    info = name.replace("-", "_") + "-" + version + ".dist-info/"
    out = dict(files)
    out[info + "METADATA"] = ("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version)).encode()
    if direct_url is not None:
        out[info + "direct_url.json"] = json.dumps(direct_url).encode()
    rows = ["%s,sha256=x,%d" % (p, len(d)) for p, d in sorted(out.items())] + [info + "RECORD,,"]
    out[info + "RECORD"] = ("\n".join(rows) + "\n").encode()
    return out


def report_for(fs: dict[str, bytes], modes: dict[str, int], packages: list[tuple[str, str]],
               extras: list[str], product: str, python_version: str = "3.11.16") -> bytes:
    """Build the probe report exactly as install_probe.py does, from file bytes."""
    items = []
    for name, version in sorted(packages):
        info = name.replace("-", "_") + "-" + version + ".dist-info/"
        record = fs[SITE.lstrip("/") + "/" + info + "RECORD"].decode()
        paths = [line.split(",")[0] for line in record.splitlines() if line]
        files = []
        for path in sorted(set(paths)):
            target = posixpath.normpath(posixpath.join(SITE, path)).lstrip("/")
            data = fs.get(SITE.lstrip("/") + "/" + path)
            if data is None:
                data = fs[target]
            files.append({"path": path, "sha256": sha(data), "size": len(data)})
        raw_url = fs.get(SITE.lstrip("/") + "/" + info + "direct_url.json")
        items.append({"name": name, "version": version, "location": SITE,
                      "direct_url": json.loads(raw_url) if raw_url else None,
                      "record_entries": len(paths), "files": files})
    report = {"schema_version": 1, "kind": "install-probe", "product": product,
              "interpreter": "/app/.venv/bin/python", "python_version": python_version, "site_packages": SITE,
              "packages": items,
              "extra_files": [{"path": p, "sha256": sha(fs[p.lstrip("/")]), "mode": modes.get(p.lstrip("/"), 0o644)}
                              for p in extras]}
    return install_probe._canonical(report)


class Fixture:
    def __init__(self, tmp: Path, product: str = "wuwaterm-api") -> None:
        self.tmp, self.product = tmp, product
        self.delivery = tmp / "delivery"
        self.root = tmp / "deploy"
        (self.root / ".deployments").mkdir(parents=True)
        self.attempt_id = art.new_id()
        self.version = "0.7.0.dev1"
        self.pkg = {
            "wuwaterm/__init__.py": b"VERSION = 1\n",
            "wuwaterm/core.py": b"def f():\n    return 1\n",
            "wuwaterm/cli.py": b"def main():\n    return 0\n",
        }
        self.tg_pkg = {
            "wuwaterm_telegram/__init__.py": b"T = 1\n",
            "wuwaterm_telegram/cli.py": b"def main():\n    return 0\n",
        }

    # source archive -------------------------------------------------------
    def source_files(self) -> dict[str, bytes]:
        if self.product == "wuwaterm-api":
            files = {"pyproject.toml": b'[project]\nname = "wuwaterm"\nversion = "%s"\n' % self.version.encode(),
                     "deploy/Dockerfile": DOCKERFILE, "deploy/entrypoint.sh": ENTRYPOINT,
                     "scripts/install_probe.py": PROBE_BYTES, "uv.lock": b"version = 1\n"}
            files.update({"src/" + k: v for k, v in self.pkg.items()})
        else:
            files = {"pyproject.toml": b'[project]\nname = "wuwaterm-telegram"\nversion = "0.1.0"\n',
                     "deploy/Dockerfile": b"FROM python:3.11-slim\nRUN true\n",
                     "scripts/install_probe.py": PROBE_BYTES, "core-lock.json": b"{}\n"}
            files.update({"src/" + k: v for k, v in self.tg_pkg.items()})
        return files

    def write_source(self, attempt: Path, files: dict[str, bytes], commit: str) -> art.SourceArchive:
        entries = [(p, "file", d, 0o755 if p.endswith(".sh") else 0o644) for p, d in sorted(files.items())]
        path = attempt / "inputs" / "source.tar"
        path.write_bytes(tar_bytes(entries, pax={"comment": commit}))
        return art.read_source_archive(path)

    # image ------------------------------------------------------------------
    def installed(self) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
        if self.product == "wuwaterm-api":
            site = dist("wuwaterm", self.version, self.pkg, {"url": "file:///app", "dir_info": {"editable": False}})
            return site, [("wuwaterm", self.version)]
        core = dist("wuwaterm", "0.7.0", self.pkg)
        tg = dist("wuwaterm-telegram", "0.1.0", self.tg_pkg)
        return {**core, **tg}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")]

    def image_layers(self, *, program: bytes = PROBE_BYTES, middle: list[tuple] | None = None,
                     final_extra: list[tuple] = (), report_override=None, fs_override=None) -> list[list[tuple]]:
        site, packages = self.installed()
        base = {"app/scripts/install_probe.py": program}
        base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
        modes = {}
        extras = []
        if self.product == "wuwaterm-api":
            base["entrypoint.sh"] = ENTRYPOINT
            modes["entrypoint.sh"] = 0o755
            extras = ["/entrypoint.sh"]
        layers = [layer(base, modes=modes)]
        effective = dict(base)
        if middle is not None:
            layers.append(middle)
            for name, kind, data, _ in middle:
                if kind == "file":
                    effective[name] = data
        effective.update(fs_override or {})
        report = report_override if report_override is not None else report_for(
            effective, modes, packages, extras, "core" if self.product == "wuwaterm-api" else "telegram")
        layers.append(layer({art.REPORT_PATH.lstrip("/"): report}, extra=list(final_extra)))
        return layers

    def build(self, layers: list[list[tuple]] | None = None, *, source_files=None, wheels=None,
              **oci_options) -> Path:
        attempt = art.begin_delivery(self.delivery, self.attempt_id, self.product)
        commit = COMMIT if self.product == "wuwaterm-api" else TG_COMMIT
        source = self.write_source(attempt, source_files or self.source_files(), commit)
        self.source = source
        raw = oci_archive(layers if layers is not None else self.image_layers(), **oci_options)
        (attempt / "bundle" / art.ARCHIVE_FILE).write_bytes(raw)
        return attempt

    def seal(self, layers=None, *, authority: str = "local-source", **options) -> art.VerifiedBundle:
        attempt = self.build(layers, **options)
        image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
        if self.product == "wuwaterm-api":
            return art.seal_bundle(self.delivery, self.attempt_id, product=self.product, commit=COMMIT,
                                   tree=self.source.tree, iid=image.manifest_digest,
                                   build_script_sha256="c" * 64, base_images=BASES,
                                   build_args=[{"name": "SOURCE_COMMIT", "value": COMMIT}], compatibility=COMPAT,
                                   authority=authority)
        wheels = self.write_wheels(attempt)
        core = next(w for w in wheels if w["role"] == "core")
        return art.seal_bundle(
            self.delivery, self.attempt_id, product=self.product, commit=TG_COMMIT, tree=self.source.tree,
            iid=image.manifest_digest, build_script_sha256="c" * 64, base_images=[BASES[1]],
            build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": core["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
            authority=authority)

    def write_wheels(self, attempt: Path, *, extra_files: dict[str, dict[str, bytes]] | None = None) -> list[dict]:
        folder = attempt / "inputs" / "wheels"
        folder.mkdir(exist_ok=True)
        result = []
        extra_files = extra_files or {}
        for name, version, files, role in (("wuwaterm", "0.7.0", self.pkg, "core"),
                                           ("wuwaterm-telegram", "0.1.0", self.tg_pkg, "telegram")):
            info = name.replace("-", "_") + "-" + version + ".dist-info/"
            buffer = io.BytesIO()
            pkg_files = dict(files)
            if name in extra_files:
                pkg_files.update(extra_files[name])
            with zipfile.ZipFile(buffer, "w") as wheel:
                for path, data in sorted(pkg_files.items()):
                    wheel.writestr(path, data)
                wheel.writestr(info + "METADATA", "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version))
                wheel.writestr(info + "RECORD", "")
            filename = name.replace("-", "_") + "-" + version + "-py3-none-any.whl"
            (folder / filename).write_bytes(buffer.getvalue())
            result.append({"filename": filename, "package": name, "version": version,
                           "sha256": sha(buffer.getvalue()), "role": role})
        return result


class FakeDocker:
    """Daemon boundary only: content-addressed store keyed by manifest digest."""

    def __init__(self, daemon: str = "daemon-a") -> None:
        self.daemon, self.images, self.loads = daemon, {}, 0
        self.on_load = None
        self.fail_after_load = False
        self.fail_before_load = False
        self.layer_override = None

    def info(self) -> dict:
        return {"ID": self.daemon, "ServerVersion": "29.1.3", "Driver": "overlayfs",
                "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]}

    def load(self, archive: Path, expected_sha256: str, expected_size: int) -> None:
        if self.fail_before_load:
            raise art.ArtifactError("connection reset before request")
        raw = archive.read_bytes()
        assert sha(raw) == expected_sha256 and len(raw) == expected_size
        image = art.read_oci_archive(archive)
        self.loads += 1
        layers = [x["diff_id"] for x in image.layers]
        self.images[image.manifest_digest] = {
            "Id": image.manifest_digest, "Os": "linux", "Architecture": "amd64",
            "RootFS": {"Type": "layers", "Layers": self.layer_override or layers},
            "Config": dict(image.config["config"])}
        if self.on_load:
            self.on_load()
        if self.fail_after_load:
            raise art.ArtifactError("response lost")

    def inspect_image(self, image_id: str):
        return self.images.get(image_id)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def store(fx: Fixture) -> Path:
    return fx.root / ".deployments" / "artifacts"


# ---------------------------------------------------------------- probe program

def test_install_probe_hashes_installed_bytes_not_record(tmp_path: Path) -> None:
    site = tmp_path / "site"
    files = dist("demo-pkg", "1.2", {"demo_pkg/__init__.py": b"x = 1\n"})
    for path, data in files.items():
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_bytes(data)
    out = tmp_path / "out" / "report.json"
    env = dict(os.environ, PYTHONPATH=str(site))
    command = [sys.executable, "-B", str(ROOT / "scripts" / "install_probe.py"), "--product", "core",
               "--dist", "demo-pkg", "--out", str(out)]
    assert subprocess.run(command, env=env, check=False).returncode == 0
    report = json.loads(out.read_bytes())
    assert out.read_bytes() == install_probe._canonical(report)
    package = report["packages"][0]
    assert package["version"] == "1.2" and package["record_entries"] == 3
    entry = next(f for f in package["files"] if f["path"] == "demo_pkg/__init__.py")
    assert entry["sha256"] == sha(b"x = 1\n")  # RECORD claims sha256=x; the probe hashed the bytes
    # Exclusive output: a second run refuses to overwrite, and a missing file fails the build.
    assert subprocess.run(command, env=env, check=False, capture_output=True).returncode == 1
    (site / "demo_pkg" / "__init__.py").unlink()
    command[-1] = str(tmp_path / "other.json")
    assert subprocess.run(command, env=env, check=False, capture_output=True).returncode == 1


def test_dockerfile_probe_is_the_last_filesystem_step() -> None:
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    runtime = text.split("FROM python-base AS runtime", 1)[1].split("\nFROM ", 1)[0]
    lines = [line for line in runtime.replace("\\\n", " ").splitlines()
             if line.strip() and not line.startswith((" ", "#"))]
    probe = [i for i, line in enumerate(lines) if "install_probe.py" in line]
    assert len(probe) == 1 and lines[probe[0]].startswith("RUN ")
    assert {line.split()[0] for line in lines[probe[0] + 1:]} <= {"ENTRYPOINT", "CMD", "LABEL", "ENV"}
    assert f"--out {art.REPORT_PATH}" in lines[probe[0]] and "--extra-file /entrypoint.sh" in lines[probe[0]]
    assert "COPY scripts ./scripts" in text  # program lives in an earlier layer at /app/scripts


# ---------------------------------------------------------------- formats

def test_git_tree_matches_real_git_archive(tmp_path: Path) -> None:
    archive = tmp_path / "head.tar"
    subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "-o", str(archive), "HEAD"], check=True)
    tree = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"], check=True,
                          capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    source = art.read_source_archive(archive)
    assert source.tree == tree and source.commit == commit


def test_rootfs_reconstruction_honours_whiteouts_and_links(tmp_path: Path) -> None:
    lower = layer({"a/keep": b"k", "a/gone": b"g", "a/sub/deep": b"d", "o/old": b"o", "x/file": b"x"})
    upper = [("a", "dir", None, 0o755), ("a/.wh.gone", "file", b"", 0o644), ("a/.wh.sub", "file", b"", 0o644),
             ("o", "dir", None, 0o755), ("o/.wh..wh..opq", "file", b"", 0o644), ("o/new", "file", b"n", 0o644),
             ("x", "dir", None, 0o755), ("x/hard", "hardlink", "x/file", 0o600), ("l", "symlink", "a", 0o777)]
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([lower, upper]))
    fs = art.read_oci_archive(path).rootfs
    assert set(n for n, v in fs.nodes.items() if v.kind == "file") == {"a/keep", "o/new", "x/file", "x/hard"}
    assert fs.nodes["x/hard"].sha256 == sha(b"x") and fs.nodes["x/hard"].mode == 0o600
    assert fs.regular("/l/keep")[0] == "a/keep"
    with pytest.raises(art.ArtifactError):
        fs.regular("/a/gone")


@pytest.mark.parametrize("options, message", [
    ({"repo_tags": ["wuwaterm:latest"]}, "RepoTags"),
    ({"annotations": {"org.opencontainers.image.ref.name": "latest"}}, "name annotation"),
    ({"annotations": {"io.containerd.image.name": "docker.io/x:y"}}, "name annotation"),
    ({"corrupt_layer": 0}, "layer"),
    ({"extra_entry": True}, "unexpected OCI archive entry"),
    ({"config_extra": {"Volumes": {"/data": {}}}}, "volumes"),
])
def test_named_corrupt_or_unsafe_archives_are_rejected(tmp_path: Path, options: dict, message: str) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], **options))
    with pytest.raises(art.ArtifactError, match=message):
        art.read_oci_archive(path)


def test_truncated_archive_is_rejected(tmp_path: Path) -> None:
    raw = oci_archive([layer({"f": b"1" * 4096})])
    path = tmp_path / "img.tar"
    path.write_bytes(raw[:len(raw) // 2])
    with pytest.raises(art.ArtifactError):
        art.read_oci_archive(path)


# ---------------------------------------------------------------- OCI and Docker v2 schemas
#
# The published My-Denia/WuwaTerm-Telegram v0.2.8 archive is an OCI index whose
# single descriptor names a Docker v2 manifest and carries a `config.digest`
# annotation; the classifier below accepted only the OCI spelling and refused
# the archive outright. Both schemas name the same image, so both are read, and
# the parts that could disagree are still cross-checked rather than trusted.

def test_plain_oci_archive_is_the_control(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})]))
    image = art.read_oci_archive(path)
    assert image.platform == {"os": "linux", "architecture": "amd64", "variant": None}
    assert image.rootfs.regular("/f")[0] == "f"


def test_matching_config_digest_annotation_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], config_digest_annotation="matching"))
    assert art.read_oci_archive(path).rootfs.regular("/f")[0] == "f"


def test_docker_v2_manifest_and_config_media_types_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], media="docker",
                                 config_digest_annotation="matching"))
    image = art.read_oci_archive(path)
    assert image.rootfs.regular("/f")[0] == "f"
    assert image.layers[0]["media_type"] == "application/vnd.oci.image.layer.v1.tar+gzip"


def test_a_config_digest_annotation_that_disagrees_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], config_digest_annotation="different"))
    with pytest.raises(art.ArtifactError, match="OCI config digest annotation differs"):
        art.read_oci_archive(path)


def test_a_malformed_config_digest_annotation_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], config_digest_annotation="malformed"))
    with pytest.raises(art.ArtifactError, match="invalid OCI config digest annotation"):
        art.read_oci_archive(path)


@pytest.mark.parametrize(("media", "descriptor_media"), [
    ("oci", "application/vnd.docker.distribution.manifest.v2+json"),
    ("docker", "application/vnd.oci.image.manifest.v1+json"),
])
def test_a_descriptor_that_names_another_schema_than_its_blob_is_refused(
    tmp_path: Path, media: str, descriptor_media: str
) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], media=media, descriptor_media=descriptor_media))
    with pytest.raises(art.ArtifactError, match="OCI manifest media type differs from index descriptor"):
        art.read_oci_archive(path)


def test_an_unknown_index_annotation_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(oci_archive([layer({"f": b"1"})], annotations={"org.example.extra": "x"}))
    with pytest.raises(art.ArtifactError, match="unexpected OCI index annotation"):
        art.read_oci_archive(path)


# ---------------------------------------------------------------- sealing and bundle verification

def test_core_bundle_seals_and_verifies(fx: Fixture) -> None:
    verified = fx.seal()
    raw = verified.path.read_bytes()
    assert raw == art.canonical(json.loads(raw)) and verified.sha256 == sha(raw)
    bundle = verified.bundle
    assert bundle["kind"] == "completed-component-bundle" and bundle["schema_version"] == 1
    assert bundle["wheels"] == [] and bundle["core_binding"]["core_wheel_sha256"] is None
    assert [p["path"] for p in bundle["payload"]] == ["image.oci.tar", "install-probe.json"]
    assert bundle["install_probe"]["program_sha256"] == sha(PROBE_BYTES)
    assert art.verify_bundle(verified.path).sha256 == verified.sha256
    states = sorted(os.listdir(verified.path.parent.parent / "state"))
    assert states == ["building.json", "sealed.json"]


def test_telegram_bundle_verifies_against_wheels_and_rejects_wrong_wheel(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    verified = fx.seal()
    wheel = verified.path.parent.parent / "inputs" / "wheels" / verified.bundle["wheels"][1]["filename"]
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 1
    wheel.write_bytes(bytes(data))
    with pytest.raises(art.ArtifactError, match="wheel bytes differ"):
        art.verify_bundle(verified.path)


def test_telegram_install_differing_from_wheel_is_rejected(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    fx.tg_pkg = {"wuwaterm_telegram/__init__.py": b"T = 1\n"}
    attempt = fx.build()
    fx.tg_pkg = {"wuwaterm_telegram/__init__.py": b"T = 2\n"}   # wheels built from different bytes
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    wheels = fx.write_wheels(attempt)
    with pytest.raises(art.ArtifactError, match="differs from verified wheel"):
        art.seal_bundle(fx.delivery, fx.attempt_id, product=fx.product, commit=TG_COMMIT, tree=fx.source.tree,
                        iid=image.manifest_digest, build_script_sha256="c" * 64, base_images=[BASES[1]],
                        build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
                        compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
                        core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                                      "core_source_commit": COMMIT, "compatibility": "exact"})


def _modified_install(fx: Fixture) -> list[list[tuple]]:
    path = SITE.lstrip("/") + "/wuwaterm/core.py"
    middle = [(path, "file", b"def f():\n    return 2\n", 0o644)]
    # A self-consistent report: RECORD-listed bytes equal the tampered file.
    return fx.image_layers(middle=middle)


@pytest.mark.parametrize("make, message", [
    (lambda fx: fx.image_layers(final_extra=[("tmp", "dir", None, 0o1777), ("tmp/x", "file", b"", 0o644)]),
     "final layer changes more"),
    (lambda fx: fx.image_layers(final_extra=[("app/.venv/.wh.x", "file", b"", 0o644)]), "final layer changes more"),
    (_modified_install, "differs from the sealed source archive"),
    (lambda fx: fx.image_layers(program=PROBE_BYTES + b"# changed\n"), "probe program"),
    (lambda fx: fx.image_layers(middle=[(SITE.lstrip("/") + "/wuwaterm/.wh.core.py", "file", b"", 0o644)]),
     "not a regular file"),
    (lambda fx: fx.image_layers(middle=[(SITE.lstrip("/") + "/wuwaterm/evil.py", "file", b"x", 0o644)]),
     "unrecorded file"),
    (lambda fx: fx.image_layers(fs_override={SITE.lstrip("/") + "/wuwaterm/core.py": b"lie\n"}),
     "differs from probe report"),
    (lambda fx: fx.image_layers(middle=[("entrypoint.sh", "file", b"#!/bin/sh\nevil\n", 0o755)]),
     "extra file differs from the sealed source"),
])
def test_tampered_images_are_rejected_at_seal(fx: Fixture, make, message: str) -> None:
    with pytest.raises(art.ArtifactError, match=message):
        fx.seal(make(fx))
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()


def test_tampered_bundle_records_are_rejected(fx: Fixture) -> None:
    verified = fx.seal()
    bundle = json.loads(verified.path.read_bytes())
    original = verified.path.read_bytes()
    for mutate, message in (
        (lambda b: b.update(extra=1), "bundle fields"),
        (lambda b: b["install_probe"].update(program_sha256="0" * 64), "probe program"),
        (lambda b: b["oci"]["layers"][0].update(size=1), "OCI archive differs"),
        (lambda b: b["source"].update(tree="0" * 40), "tree differs"),
        (lambda b: b["recipe"].update(dockerfile_sha256="0" * 64), "Dockerfile"),
        (lambda b: b["build_result"].update(foreground_exit=1), "did not exit"),
        (lambda b: b["recipe"]["build_args"].append({"name": "TOKEN", "value": "x"}), "allowlist"),
    ):
        changed = json.loads(json.dumps(bundle))
        mutate(changed)
        verified.path.write_bytes(art.canonical(changed))
        with pytest.raises(art.ArtifactError, match=message):
            art.verify_bundle(verified.path)
    verified.path.write_bytes(original.rstrip(b"\n") + b" \n")
    with pytest.raises(art.ArtifactError, match="canonical"):
        art.verify_bundle(verified.path)
    verified.path.write_bytes(original)
    (verified.path.parent / "stray").write_bytes(b"")
    with pytest.raises(art.ArtifactError, match="exactly"):
        art.verify_bundle(verified.path)


def test_report_copy_and_payload_tamper_is_rejected(fx: Fixture) -> None:
    verified = fx.seal()
    report = verified.path.parent / art.REPORT_FILE
    report.write_bytes(report.read_bytes().replace(b"core", b"tele", 1))
    with pytest.raises(art.ArtifactError, match="payload file differs"):
        art.verify_bundle(verified.path)


def test_missing_source_package_is_rejected_at_seal_and_verify(fx: Fixture) -> None:
    source_files = fx.source_files()
    source_files["src/wuwaterm_api/__init__.py"] = b"API = 1\n"
    source_files["src/wuwaterm_api/service.py"] = b"def main(): pass\n"
    with pytest.raises(art.ArtifactError, match="installed package differs from the sealed source archive: wuwaterm_api"):
        fx.seal(source_files=source_files)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()
    bundle_file = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_file.exists()
    with pytest.raises(art.ArtifactError, match="installed package differs from the sealed source archive: wuwaterm_api"):
        art.verify_bundle(bundle_file)


def test_hatch_wheel_packages_in_pyproject_toml(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "restricted")
    source_files = fx.source_files()
    source_files["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n\n'
        b'[tool.hatch.build.targets.wheel]\npackages = ["src/wuwaterm"]\n'
    )
    source_files["src/wuwaterm_api/__init__.py"] = b"API = 1\n"
    source_files["src/wuwaterm_api/service.py"] = b"def main(): pass\n"
    verified = fx.seal(source_files=source_files)
    assert verified.bundle["product"] == "wuwaterm-api"

    fx2 = Fixture(tmp_path / "both")
    source_files2 = fx2.source_files()
    source_files2["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n\n'
        b'[tool.hatch.build.targets.wheel]\npackages = ["src/wuwaterm", "src/wuwaterm_api"]\n'
    )
    source_files2["src/wuwaterm_api/__init__.py"] = b"API = 1\n"
    source_files2["src/wuwaterm_api/service.py"] = b"def main(): pass\n"
    with pytest.raises(art.ArtifactError, match="installed package differs from the sealed source archive: wuwaterm_api"):
        fx2.seal(source_files=source_files2)

    fx3 = Fixture(tmp_path / "outside")
    source_files3 = fx3.source_files()
    source_files3["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n\n'
        b'[tool.hatch.build.targets.wheel]\npackages = ["outside/pkg"]\n'
    )
    with pytest.raises(art.ArtifactError, match="wheel package in pyproject.toml is outside the source root"):
        fx3.seal(source_files=source_files3)


def test_telegram_bundle_omitting_telegram_package_is_rejected_at_seal_and_verify(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    core = dist("wuwaterm", "0.7.0", fx.pkg)
    base = {"app/scripts/install_probe.py": PROBE_BYTES}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in core.items()})
    report = report_for(base, {}, [("wuwaterm", "0.7.0")], [], "telegram")
    layers = [layer(base), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="installed Telegram version differs from the bundle"):
        fx.seal(layers)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()
    bundle_file = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_file.exists()
    with pytest.raises(art.ArtifactError, match="installed Telegram version differs from the bundle"):
        art.verify_bundle(bundle_file)


def test_telegram_bundle_wrong_installed_version_is_rejected_at_seal_and_verify(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    core = dist("wuwaterm", "0.7.0", fx.pkg)
    tg_wrong = dist("wuwaterm-telegram", "0.2.0", fx.tg_pkg)
    base = {"app/scripts/install_probe.py": PROBE_BYTES}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg_wrong}.items()})
    report = report_for(base, {}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.2.0")], [], "telegram")
    layers = [layer(base), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="installed Telegram version differs from the bundle"):
        fx.seal(layers)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()
    bundle_file = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_file.exists()
    with pytest.raises(art.ArtifactError, match="installed Telegram version differs from the bundle"):
        art.verify_bundle(bundle_file)


def test_telegram_bundle_wrong_pyproject_version_is_rejected_at_seal_and_verify(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "seal", "wuwaterm-telegram")
    source_files = fx.source_files()
    source_files["pyproject.toml"] = b'[project]\nname = "wuwaterm-telegram"\nversion = "0.9.9"\n'
    with pytest.raises(art.ArtifactError, match="bundle version differs from the sealed source"):
        fx.seal(source_files=source_files)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()

    fx2 = Fixture(tmp_path / "verify", "wuwaterm-telegram")
    verified = fx2.seal()
    bundle_data = json.loads(verified.path.read_bytes())
    bundle_data["version"] = "0.9.9"
    for w in bundle_data["wheels"]:
        if w["role"] == "telegram":
            w["version"] = "0.9.9"
    verified.path.write_bytes(art.canonical(bundle_data))
    with pytest.raises(art.ArtifactError, match="bundle version differs from the sealed source"):
        art.verify_bundle(verified.path)


def test_telegram_bundle_unexpected_package_is_rejected_at_seal(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    core = dist("wuwaterm", "0.7.0", fx.pkg)
    tg = dist("wuwaterm-telegram", "0.1.0", fx.tg_pkg)
    extra = dist("unexpected-pkg", "1.0.0", {"unexpected_pkg/__init__.py": b"U = 1\n"})
    base = {"app/scripts/install_probe.py": PROBE_BYTES}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg, **extra}.items()})
    report = report_for(base, {}, [("unexpected-pkg", "1.0.0"), ("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers = [layer(base), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="probed packages differ from expected product distributions"):
        fx.seal(layers)


# ---------------------------------------------------------------- acquisition, cancellation, recovery

def test_acquire_registers_exact_graph_and_verifies_receipt(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    result = art.acquire(verified.path, fx.root, docker=docker)
    receipt = result["receipt"]
    assert result["state"] == "registered" and docker.loads == 1
    assert receipt["native_image_id"] == verified.bundle["oci"]["manifest_digest"]
    path = store(fx) / "acquisitions" / (receipt["acquisition_id"] + ".json")
    checked = art.verify_acquisition(path, docker=docker)
    assert checked["bundle_sha256"] == verified.sha256
    assert (store(fx) / "bundles" / (verified.sha256 + ".json")).read_bytes() == verified.raw
    # Already registered: cancel reports it and does not overwrite.
    assert art.cancel(fx.root, fx.attempt_id) == {"state": "registered", "already_registered": True}
    assert not (store(fx) / "attempts" / fx.attempt_id / "cancelled.json").exists()
    # A pruned image makes the receipt unusable rather than silently accepted ...
    before = sorted(p.name for p in store(fx).rglob("*"))
    docker.images.clear()
    with pytest.raises(art.ArtifactError, match="does not hold"):
        art.verify_acquisition(path, docker=docker)
    # ... and re-acquiring the same sealed bundle restores it without new records.
    again = art.acquire(verified.path, fx.root, docker=docker)
    assert again["reloaded"] is True and again["receipt"] == receipt and docker.loads == 2
    assert art.acquire(verified.path, fx.root, docker=docker)["reloaded"] is False
    assert sorted(p.name for p in store(fx).rglob("*")) == before
    assert art.verify_acquisition(path, docker=docker)["receipt"] == receipt


def test_cancel_before_acquire_blocks_load(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    assert art.cancel(fx.root, fx.attempt_id)["state"] == "cancelled"
    with pytest.raises(art.CancelledError):
        art.acquire(verified.path, fx.root, docker=docker)
    assert docker.loads == 0 and os.listdir(store(fx) / "acquisitions") == []


def test_cancel_during_load_wins_and_late_image_stays_unregistered(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    docker.on_load = lambda: art.cancel(fx.root, fx.attempt_id)
    with pytest.raises(art.CancelledError):
        art.acquire(verified.path, fx.root, docker=docker)
    attempt = store(fx) / "attempts" / fx.attempt_id
    assert sorted(os.listdir(attempt)) == ["cancelled.json", "import-intent.json"]
    assert docker.images  # the late artifact exists in the store
    assert art.recover(fx.root, fx.attempt_id, docker=docker)["state"] == "cancelled"
    assert art.cancel(fx.root, fx.attempt_id)["already_cancelled"] is True
    assert os.listdir(store(fx) / "acquisitions") == []
    # Retry needs a new attempt ID: a newly sealed bundle registers.
    retry = Fixture(fx.tmp / "retry")
    retry.root = fx.root
    assert art.acquire(retry.seal().path, fx.root, docker=docker)["state"] == "registered"


def test_lost_load_response_recovers_idempotently(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    docker.fail_after_load = True
    with pytest.raises(art.ArtifactError, match="response lost"):
        art.acquire(verified.path, fx.root, docker=docker)
    assert sorted(os.listdir(store(fx) / "attempts" / fx.attempt_id)) == ["import-intent.json"]
    docker.fail_after_load = False
    with pytest.raises(art.ArtifactError, match="use recover"):
        art.acquire(verified.path, fx.root, docker=docker)
    first = art.recover(fx.root, fx.attempt_id, docker=docker)
    assert first["state"] == "registered" and first["already_registered"] is False
    assert docker.loads == 1  # present image found by full ID; no replay needed
    second = art.recover(fx.root, fx.attempt_id, docker=docker)
    assert second["already_registered"] is True and second["receipt"] == first["receipt"]


def test_failure_before_load_replays_the_sealed_bytes(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    docker.fail_before_load = True
    with pytest.raises(art.ArtifactError):
        art.acquire(verified.path, fx.root, docker=docker)
    docker.fail_before_load = False
    assert art.recover(fx.root, fx.attempt_id, docker=docker)["state"] == "registered"
    assert docker.loads == 1
    receipt_index = os.listdir(store(fx) / "acquisitions")
    # A crash after registered.json but before the index copy is repaired by recover.
    (store(fx) / "acquisitions" / receipt_index[0]).unlink()
    assert art.recover(fx.root, fx.attempt_id, docker=docker)["already_registered"] is True
    assert os.listdir(store(fx) / "acquisitions") == receipt_index


def test_graph_mismatch_and_foreign_store_are_refused(fx: Fixture) -> None:
    verified = fx.seal()
    docker = FakeDocker()
    docker.layer_override = ["sha256:" + "0" * 64]
    with pytest.raises(art.ArtifactError, match="graph differs"):
        art.acquire(verified.path, fx.root, docker=docker)
    assert not (store(fx) / "attempts" / fx.attempt_id / "registered.json").exists()
    with pytest.raises(art.ArtifactError, match="store recorded"):
        art.recover(fx.root, fx.attempt_id, docker=FakeDocker("daemon-b"))


def test_overlapping_delivery_and_deploy_root_is_refused(fx: Fixture) -> None:
    verified = fx.seal()
    (fx.delivery / ".deployments").mkdir()
    with pytest.raises(art.ArtifactError, match="overlap"):
        art.acquire(verified.path, fx.delivery, docker=FakeDocker())
    nested = fx.delivery / "deploy"   # different subtree of the delivery root
    (nested / ".deployments").mkdir(parents=True)
    with pytest.raises(art.ArtifactError, match="overlap"):
        art.acquire(verified.path, nested, docker=FakeDocker())
    assert not (nested / ".deployments" / "artifacts").exists()


def test_acquire_refuses_uninitialized_deployment_root_without_deployments(fx: Fixture, tmp_path: Path) -> None:
    verified = fx.seal()
    uninitialized = tmp_path / "uninitialized"
    uninitialized.mkdir()
    with pytest.raises(art.ArtifactError, match="has no .deployments directory"):
        art.acquire(verified.path, uninitialized, docker=FakeDocker())


def test_mutating_entry_points_refuse_inherited_fd9(fx: Fixture) -> None:
    verified = fx.seal()
    fd = os.open(os.devnull, os.O_RDONLY)
    if fd != 9:
        os.dup2(fd, 9)
        os.close(fd)
    try:
        for call in (lambda: art.begin_delivery(fx.delivery, art.new_id(), fx.product),
                     lambda: art.acquire(verified.path, fx.root, docker=FakeDocker()),
                     lambda: art.cancel(fx.root, fx.attempt_id),
                     lambda: art.recover(fx.root, fx.attempt_id, docker=FakeDocker())):
            with pytest.raises(art.ArtifactError, match="descriptor 9"):
                call()
        assert art.verify_bundle(verified.path).sha256 == verified.sha256  # read-only stays usable
    finally:
        os.close(9)
    assert not (fx.root / ".deployments" / "artifacts" / "attempts").exists()


def test_call_phase_runs_with_fd9_closed(fx: Fixture) -> None:
    """Descriptor 9 is free here, so this module's entry-point calls match a deploy.

    Asserted rather than assumed because the number has two very different
    owners depending on the machine: a runner that passes one in, or pytest's
    own fd capture when nothing was inherited. Both have to leave the entry
    points a closed 9 during the call, and neither may be left holding a number
    that was taken away from under it.
    """
    with pytest.raises(OSError):
        os.fstat(9)
    verified = fx.seal()
    assert art.acquire(verified.path, fx.root, docker=FakeDocker())["receipt"]["acquisition_id"]


def test_a_session_without_an_inherited_descriptor_finishes_green() -> None:
    """The reported breakage: no inherited fd 9 at all, which is what a local run has.

    ``scripts/validate.py`` spawns the suite with ``subprocess`` and its default
    ``close_fds``, so the tests see descriptors 0-2 and nothing else. That is
    the condition that failed: pytest's capture file landed on 9, the module's
    fixture closed it, and every capture restore afterwards raised EBADF. Only
    the descriptor-9 tests run here, so this pins the failure without running
    the whole gate.
    """
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("COV_CORE", "PYTEST_"))}
    module = Path(__file__).resolve()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         f"{module}::test_build_delivery_closes_fd9_before_anything_else",
         f"{module}::test_mutating_entry_points_refuse_inherited_fd9",
         f"{module}::test_call_phase_runs_with_fd9_closed"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=300)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_build_delivery_closes_fd9_before_anything_else(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "build-delivery.sh"
    result = subprocess.run(f"bash {script} 9>/dev/null", shell=True, capture_output=True, text=True,
                            check=False)
    assert result.returncode == 2 and "usage:" in result.stderr and "descriptor 9" not in result.stderr
    text = script.read_text(encoding="utf-8")
    assert text.index("exec 9>&-") < text.index("if docker buildx")
    build = text.split("\ndocker build ", 1)[1].split("\n\n", 1)[0]
    assert "--iidfile" in build and "--force-rm" in build and " -t " not in build and "--tag" not in build


def test_verify_acquisition_binds_native_id_to_sealed_digests_without_daemon(fx: Fixture) -> None:
    verified = fx.seal()
    receipt = art.acquire(verified.path, fx.root, docker=FakeDocker())["receipt"]
    path = store(fx) / "acquisitions" / (receipt["acquisition_id"] + ".json")
    assert art.verify_acquisition(path, check_daemon=False)["receipt"] == receipt
    # A receipt whose recorded native ID is not the sealed graph's ID is refused
    # even when both of its copies were changed consistently.
    forged = dict(receipt, native_image_id=verified.bundle["oci"]["config_digest"])
    raw = art.canonical(forged)
    for target in (path, store(fx) / "attempts" / fx.attempt_id / "registered.json"):
        target.write_bytes(raw)
    with pytest.raises(art.ArtifactError, match="differs from the sealed bundle digests"):
        art.verify_acquisition(path, check_daemon=False)


def test_attempt_without_an_intent_is_tombstoned_instead_of_dead_ending(fx: Fixture) -> None:
    """A crash between mkdir and import-intent.json must stay resolvable."""
    identifier = art.new_id()
    attempt = store(fx) / "attempts" / identifier
    attempt.mkdir(parents=True)
    assert art.read_attempt_state(store(fx), identifier) == {"state": "none"}
    recovered = art.recover(fx.root, identifier, docker=FakeDocker())
    assert recovered["state"] == "cancelled"
    assert recovered["cancellation"]["prior_state"] == "none"
    assert recovered["cancellation"]["attempt_id"] == identifier
    assert art.read_attempt_state(store(fx), identifier)["state"] == "cancelled"
    assert art.cancel(fx.root, identifier)["already_cancelled"] is True
    # Nothing was loaded and no receipt was written for an empty attempt.
    assert os.listdir(store(fx) / "acquisitions") == []


def test_telegram_bundle_source_tamper_is_rejected_at_seal_and_verify(tmp_path: Path) -> None:
    # 1. seal_bundle rejects when source.tar differs from installed / wheel
    fx = Fixture(tmp_path / "seal", "wuwaterm-telegram")
    source_files = fx.source_files()
    source_files["src/wuwaterm_telegram/__init__.py"] = b"T = 999\n"
    with pytest.raises(art.ArtifactError, match="installed package differs from the sealed source archive: wuwaterm_telegram"):
        fx.seal(source_files=source_files)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()

    # 2. verify_bundle also rejects when source package differs from installed
    fx2 = Fixture(tmp_path / "verify", "wuwaterm-telegram")
    verified = fx2.seal()
    source_tar = fx2.delivery / "attempts" / fx2.attempt_id / "inputs" / "source.tar"
    tampered_files = fx2.source_files()
    tampered_files["src/wuwaterm_telegram/__init__.py"] = b"T = 999\n"
    entries = [(p, "file", d, 0o755 if p.endswith(".sh") else 0o644) for p, d in sorted(tampered_files.items())]
    source_tar.write_bytes(tar_bytes(entries, pax={"comment": TG_COMMIT}))
    new_source = art.read_source_archive(source_tar)
    bundle_data = json.loads(verified.path.read_bytes())
    bundle_data["source"]["archive_sha256"] = new_source.sha256
    bundle_data["source"]["tree"] = new_source.tree
    bundle_data["recipe"]["context_manifest_sha256"] = art.context_manifest_sha256(new_source)
    verified.path.write_bytes(art.canonical(bundle_data))
    with pytest.raises(art.ArtifactError, match="installed package differs from the sealed source archive: wuwaterm_telegram"):
        art.verify_bundle(verified.path)


def test_wheel_package_extra_file_in_image_is_rejected_at_seal(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    extra_files = dict(fx.tg_pkg)
    extra_files["wuwaterm_telegram/extra.py"] = b"extra = 1\n"
    core = dist("wuwaterm", "0.7.0", fx.pkg)
    tg_extra = dist("wuwaterm-telegram", "0.1.0", extra_files)
    site = {**core, **tg_extra}
    base = {"app/scripts/install_probe.py": PROBE_BYTES}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    report = report_for(base, {}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers = [layer(base), layer({art.REPORT_PATH.lstrip("/"): report})]
    attempt = fx.build(layers)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    wheels = fx.write_wheels(attempt)
    with pytest.raises(art.ArtifactError, match="installed file is not in verified wheel: wuwaterm_telegram/extra.py"):
        art.seal_bundle(
            fx.delivery, fx.attempt_id, product=fx.product, commit=TG_COMMIT, tree=fx.source.tree,
            iid=image.manifest_digest, build_script_sha256="c" * 64, base_images=[BASES[1]],
            build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"})


def test_site_packages_top_level_files_rejected(tmp_path: Path) -> None:
    # 1. Recorded top-level file in site-packages (source package wuwaterm-api)
    fx_rec_api = Fixture(tmp_path / "rec_api")
    site_tampered = dist("wuwaterm", fx_rec_api.version, {**fx_rec_api.pkg, "stray_toplevel.py": b"# evil\n"},
                         {"url": "file:///app", "dir_info": {"editable": False}})
    base_api = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base_api.update({SITE.lstrip("/") + "/" + k: v for k, v in site_tampered.items()})
    modes_api = {"entrypoint.sh": 0o755}
    report_api = report_for(base_api, modes_api, [("wuwaterm", fx_rec_api.version)], ["/entrypoint.sh"], "core")
    layers_api = [layer(base_api, modes=modes_api), layer({art.REPORT_PATH.lstrip("/"): report_api})]
    with pytest.raises(art.ArtifactError, match="installed file is outside expected package directories: stray_toplevel.py"):
        fx_rec_api.seal(layers_api)

    # 2. Recorded top-level file in site-packages (wheel package in wuwaterm-telegram)
    fx_rec_tg = Fixture(tmp_path / "rec_tg", "wuwaterm-telegram")
    core = dist("wuwaterm", "0.7.0", {**fx_rec_tg.pkg, "stray_toplevel.py": b"# evil\n"})
    tg = dist("wuwaterm-telegram", "0.1.0", fx_rec_tg.tg_pkg)
    base_tg = {"app/scripts/install_probe.py": PROBE_BYTES}
    base_tg.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg}.items()})
    report_tg = report_for(base_tg, {}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers_tg = [layer(base_tg), layer({art.REPORT_PATH.lstrip("/"): report_tg})]
    attempt = fx_rec_tg.build(layers_tg)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    wheels = fx_rec_tg.write_wheels(attempt)
    with pytest.raises(art.ArtifactError, match="installed file is not in verified wheel: stray_toplevel.py"):
        art.seal_bundle(
            fx_rec_tg.delivery, fx_rec_tg.attempt_id, product=fx_rec_tg.product, commit=TG_COMMIT,
            tree=fx_rec_tg.source.tree, iid=image.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"})


def test_wheel_uniqueness_and_dependency_wheel_validation(tmp_path: Path) -> None:
    # 1. Duplicate wheel package names in schema validation
    base_wheel = {"filename": "dep_a-1.0-py3-none-any.whl", "package": "dep-a", "version": "1.0",
                  "sha256": "0" * 64, "role": "dependency"}
    dup_wheel = {"filename": "dep_a_dup-1.0-py3-none-any.whl", "package": "dep_a", "version": "1.0",
                 "sha256": "1" * 64, "role": "dependency"}
    bundle = {
        "schema_version": 1, "kind": art.BUNDLE_KIND, "attempt_id": art.new_id(), "product": "wuwaterm-api",
        "repository": "My-Denia/WuwaTerm", "version": "0.7.0.dev1",
        "source": {"commit": COMMIT, "tree": COMMIT, "archive_sha256": "0" * 64}, "tag": None,
        "recipe": {"dockerfile_sha256": "0" * 64, "build_script_sha256": "0" * 64,
                   "context_manifest_sha256": "0" * 64, "target": "runtime", "platform": "linux/amd64",
                   "build_args": [{"name": "SOURCE_COMMIT", "value": COMMIT}],
                   "base_images": BASES, "lock_sha256": "0" * 64},
        "wheels": [base_wheel, dup_wheel],
        "core_binding": {"core_version": "0.7.0.dev1", "core_wheel_sha256": None,
                         "core_source_commit": COMMIT, "compatibility": "exact"},
        "oci": {"archive_sha256": "0" * 64, "archive_size": 1, "index_digest": "sha256:" + "0" * 64,
                "manifest_digest": "sha256:" + "0" * 64, "config_digest": "sha256:" + "0" * 64,
                "layers": [{"digest": "sha256:" + "0" * 64, "size": 1, "diff_id": "sha256:" + "0" * 64,
                            "media_type": "application/vnd.oci.image.layer.v1.tar+gzip"}],
                "platform": {"os": "linux", "architecture": "amd64", "variant": None}},
        "install_probe": {"program_sha256": "0" * 64, "report_sha256": "0" * 64,
                          "report_layer_digest": "sha256:" + "0" * 64, "report_path": art.REPORT_PATH,
                          "interpreter": "/python", "site_packages": "/site",
                          "packages": [{"name": "wuwaterm", "version": "0.7.0.dev1", "location": "/site",
                                        "files": [{"path": "m.py", "sha256": "0" * 64, "size": 1}]}],
                          "extra_files": [{"path": "/entrypoint.sh", "sha256": "0" * 64, "mode": 0o755}]},
        "build_result": {"authority": "local-source", "foreground_exit": 0, "iid": "sha256:" + "0" * 64, "ci": None},
        "compatibility": COMPAT,
        "payload": [{"path": art.ARCHIVE_FILE, "sha256": "0" * 64, "size": 1},
                    {"path": art.REPORT_FILE, "sha256": "0" * 64, "size": 1}],
    }
    with pytest.raises(art.ArtifactError, match="duplicate wheel package"):
        art.validate_bundle_schema(bundle)

    # 2. Dependency wheel cannot be wuwaterm or wuwaterm-telegram
    bundle["wheels"] = [{"filename": "wuwaterm-0.7.0-py3-none-any.whl", "package": "wuwaterm",
                         "version": "0.7.0", "sha256": "0" * 64, "role": "dependency"}]
    with pytest.raises(art.ArtifactError, match="dependency wheel cannot be a WuwaTerm product"):
        art.validate_bundle_schema(bundle)

    # 3. Dependency wheel with mismatched METADATA rejected at verify_bundle
    fx = Fixture(tmp_path / "dep_verify", "wuwaterm-telegram")
    verified = fx.seal()
    wheel_folder = verified.path.parent.parent / "inputs" / "wheels"
    dep_buffer = io.BytesIO()
    with zipfile.ZipFile(dep_buffer, "w") as zf:
        zf.writestr("dep_pkg-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: mismatched-name\nVersion: 1.0\n")
        zf.writestr("dep_pkg-1.0.dist-info/RECORD", "")
    dep_filename = "dep_pkg-1.0-py3-none-any.whl"
    dep_bytes = dep_buffer.getvalue()
    (wheel_folder / dep_filename).write_bytes(dep_bytes)
    bundle_data = json.loads(verified.path.read_bytes())
    bundle_data["wheels"].append({"filename": dep_filename, "package": "dep-pkg", "version": "1.0",
                                  "sha256": sha(dep_bytes), "role": "dependency"})
    verified.path.write_bytes(art.canonical(bundle_data))
    with pytest.raises(art.ArtifactError, match="wheel package or version differs"):
        art.verify_bundle(verified.path)


def test_pyproject_name_and_core_lock_mismatches_are_rejected(tmp_path: Path) -> None:
    # 1. Mismatched pyproject.toml [project].name for API
    fx_api = Fixture(tmp_path / "api_wrong_name")
    api_src = fx_api.source_files()
    api_src["pyproject.toml"] = b'[project]\nname = "wrong-api"\nversion = "0.7.0.dev1"\n'
    with pytest.raises(art.ArtifactError, match="source pyproject.toml project name differs"):
        fx_api.seal(source_files=api_src)

    # 2. Mismatched pyproject.toml [project].name for Telegram
    fx_tg = Fixture(tmp_path / "tg_wrong_name", "wuwaterm-telegram")
    tg_src = fx_tg.source_files()
    tg_src["pyproject.toml"] = b'[project]\nname = "wrong-tg"\nversion = "0.1.0"\n'
    with pytest.raises(art.ArtifactError, match="source pyproject.toml project name differs"):
        fx_tg.seal(source_files=tg_src)

    # 3. Mismatched non-empty core-lock.json version in wuwaterm-telegram (local-source and official-ci)
    fx_lock_ver = Fixture(tmp_path / "lock_ver", "wuwaterm-telegram")
    lock_ver_src = fx_lock_ver.source_files()
    lock_ver_src["core-lock.json"] = json.dumps({
        "version": "9.9.9", "sha256": "0" * 64, "checkout_sha": COMMIT
    }).encode()
    with pytest.raises(art.ArtifactError, match="core-lock.json version differs from core binding"):
        fx_lock_ver.seal(source_files=lock_ver_src, authority="local-source")

    fx_lock_ver_ci = Fixture(tmp_path / "lock_ver_ci", "wuwaterm-telegram")
    lock_ver_ci_src = fx_lock_ver_ci.source_files()
    lock_ver_ci_src["core-lock.json"] = json.dumps({
        "version": "9.9.9", "sha256": "0" * 64, "checkout_sha": COMMIT
    }).encode()
    with pytest.raises(art.ArtifactError, match="core-lock.json version differs from core binding"):
        fx_lock_ver_ci.seal(source_files=lock_ver_ci_src, authority="official-ci")

    # 4. Mismatched non-empty core-lock.json sha256 in wuwaterm-telegram (official-ci only)
    fx_lock_sha = Fixture(tmp_path / "lock_sha", "wuwaterm-telegram")
    lock_sha_src = fx_lock_sha.source_files()
    lock_sha_src["core-lock.json"] = json.dumps({
        "version": "0.7.0", "sha256": "0" * 64, "checkout_sha": COMMIT
    }).encode()
    with pytest.raises(art.ArtifactError, match="core-lock.json sha256 differs from core binding"):
        fx_lock_sha.seal(source_files=lock_sha_src, authority="official-ci")

    # 5. Mismatched non-empty core-lock.json checkout_sha in wuwaterm-telegram (official-ci only)
    fx_lock_commit = Fixture(tmp_path / "lock_commit", "wuwaterm-telegram")
    lock_commit_src = fx_lock_commit.source_files()
    attempt = fx_lock_commit.build(source_files=lock_commit_src)
    wheels = fx_lock_commit.write_wheels(attempt)
    core_wheel = next(w for w in wheels if w["role"] == "core")
    correct_sha_lock = json.dumps({
        "version": "0.7.0", "sha256": core_wheel["sha256"], "checkout_sha": "9" * 40
    }).encode()
    lock_commit_src["core-lock.json"] = correct_sha_lock
    new_src = fx_lock_commit.write_source(attempt, lock_commit_src, TG_COMMIT)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="core-lock.json source commit differs from core binding"):
        art.seal_bundle(
            fx_lock_commit.delivery, fx_lock_commit.attempt_id, product=fx_lock_commit.product,
            commit=TG_COMMIT, tree=new_src.tree, iid=image.manifest_digest,
            build_script_sha256="c" * 64, base_images=[BASES[1]],
            build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": core_wheel["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
            authority="official-ci")

    # 6. Empty core-lock.json {} rejected under official-ci
    fx_lock_empty = Fixture(tmp_path / "lock_empty", "wuwaterm-telegram")
    lock_empty_src = fx_lock_empty.source_files()
    lock_empty_src["core-lock.json"] = b"{}"
    with pytest.raises(art.ArtifactError, match="official-ci Telegram bundle requires non-empty core-lock.json"):
        fx_lock_empty.seal(source_files=lock_empty_src, authority="official-ci")

    # 7. Mismatched dependencies pin in pyproject.toml
    fx_dep_pin = Fixture(tmp_path / "dep_pin", "wuwaterm-telegram")
    dep_pin_src = fx_dep_pin.source_files()
    dep_pin_src["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm-telegram"\nversion = "0.1.0"\n'
        b'dependencies = ["wuwaterm==9.9.9"]\n'
    )
    with pytest.raises(art.ArtifactError, match="pyproject.toml wuwaterm dependency pin differs"):
        fx_dep_pin.seal(source_files=dep_pin_src)


def test_seal_bundle_validates_building_state(fx: Fixture) -> None:
    attempt = fx.build()
    state_file = attempt / "state" / "building.json"
    state_file.write_bytes(art.canonical({"schema_version": 1, "kind": art.DELIVERY_STATE_KIND,
                                          "attempt_id": fx.attempt_id, "state": "other",
                                          "product": fx.product, "utc": "2026-01-01T00:00:00Z"}))
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="delivery attempt is not in the building state"):
        art.seal_bundle(fx.delivery, fx.attempt_id, product=fx.product, commit=COMMIT,
                        tree=fx.source.tree, iid=image.manifest_digest,
                        build_script_sha256="c" * 64, base_images=BASES,
                        build_args=[{"name": "SOURCE_COMMIT", "value": COMMIT}], compatibility=COMPAT)


def test_seal_bundle_requires_lock_file(fx: Fixture) -> None:
    source_files = fx.source_files()
    del source_files["uv.lock"]
    with pytest.raises(art.ArtifactError, match="source archive is missing lock file"):
        fx.seal(source_files=source_files)


def test_direct_url_tamper_is_rejected_at_seal(fx: Fixture) -> None:
    # 1. direct_url.json in rootfs differs from probe report
    site = dist("wuwaterm", fx.version, fx.pkg, {"url": "file:///app", "dir_info": {"editable": False}})
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    # Build report, then tamper the probe report's direct_url claim
    raw_report = report_for(base, modes, [("wuwaterm", fx.version)], ["/entrypoint.sh"], "core")
    report_data = json.loads(raw_report)
    report_data["packages"][0]["direct_url"] = {"url": "file:///forged"}
    tampered_report = install_probe._canonical(report_data)

    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): tampered_report})]
    with pytest.raises(art.ArtifactError, match="installed direct_url differs from probe report: wuwaterm"):
        fx.seal(layers)

    # 2. Probe report claims direct_url, but direct_url.json missing in rootfs
    fx2 = Fixture(fx.tmp / "no_url")
    site_no_url = dist("wuwaterm", fx2.version, fx2.pkg)
    base_no_url = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base_no_url.update({SITE.lstrip("/") + "/" + k: v for k, v in site_no_url.items()})
    raw_report2 = report_for(base_no_url, modes, [("wuwaterm", fx2.version)], ["/entrypoint.sh"], "core")
    report_data2 = json.loads(raw_report2)
    report_data2["packages"][0]["direct_url"] = {"url": "file:///app"}
    tampered_report2 = install_probe._canonical(report_data2)

    layers2 = [layer(base_no_url, modes=modes), layer({art.REPORT_PATH.lstrip("/"): tampered_report2})]
    with pytest.raises(art.ArtifactError, match="probe report claims direct_url but direct_url.json is missing: wuwaterm"):
        fx2.seal(layers2)

    # 3. Wheel direct_url hash differs from verified wheel hash
    fx_tg = Fixture(fx.tmp / "tg_url", "wuwaterm-telegram")
    core = dist("wuwaterm", "0.7.0", fx_tg.pkg, {"url": "file:///pkg.whl", "archive_info": {"hashes": {"sha256": "0" * 64}}})
    tg = dist("wuwaterm-telegram", "0.1.0", fx_tg.tg_pkg)
    base_tg = {"app/scripts/install_probe.py": PROBE_BYTES}
    base_tg.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg}.items()})
    report_tg = report_for(base_tg, {}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers_tg = [layer(base_tg), layer({art.REPORT_PATH.lstrip("/"): report_tg})]
    attempt = fx_tg.build(layers_tg)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    wheels = fx_tg.write_wheels(attempt)
    with pytest.raises(art.ArtifactError, match="installed distribution came from a different wheel: wuwaterm"):
        art.seal_bundle(
            fx_tg.delivery, fx_tg.attempt_id, product=fx_tg.product, commit=TG_COMMIT,
            tree=fx_tg.source.tree, iid=image.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"})


def uv_launcher(interpreter: str, module: str, attr: str) -> bytes:
    return (
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


def pip_launcher(interpreter: str, module: str, attr: str) -> bytes:
    return (
        f"#!{interpreter}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        f"from {module} import {attr}\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({attr}())\n"
    ).encode("utf-8")


def test_valid_console_script_launcher_passes_verification(tmp_path: Path) -> None:
    # 1. uv launcher in wuwaterm-api (source package)
    fx_api = Fixture(tmp_path / "uv_valid")
    src_api = fx_api.source_files()
    src_api["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n'
        b'[project.scripts]\nwuwaterm = "wuwaterm.cli:main"\n'
    )
    info_api = "wuwaterm-0.7.0.dev1.dist-info/"
    launcher_api = uv_launcher("/app/.venv/bin/python", "wuwaterm.cli", "main")
    site_api = dist(
        "wuwaterm", fx_api.version,
        {**fx_api.pkg, info_api + "entry_points.txt": b"[console_scripts]\nwuwaterm = wuwaterm.cli:main\n",
         "../../../bin/wuwaterm": launcher_api},
        {"url": "file:///app", "dir_info": {"editable": False}},
    )
    base_api = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
                "app/.venv/bin/wuwaterm": launcher_api}
    base_api.update({SITE.lstrip("/") + "/" + k: v for k, v in site_api.items() if not k.startswith("../")})
    modes_api = {"entrypoint.sh": 0o755, "app/.venv/bin/wuwaterm": 0o755}
    report_api = report_for(base_api, modes_api, [("wuwaterm", fx_api.version)], ["/entrypoint.sh"], "core")
    layers_api = [layer(base_api, modes=modes_api), layer({art.REPORT_PATH.lstrip("/"): report_api})]
    verified_api = fx_api.seal(layers_api, source_files=src_api)
    assert art.verify_bundle(verified_api.path).sha256 == verified_api.sha256

    # 2. pip launcher in wuwaterm-telegram (wheel + source package)
    fx_tg = Fixture(tmp_path / "pip_valid", "wuwaterm-telegram")
    src_tg = fx_tg.source_files()
    src_tg["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm-telegram"\nversion = "0.1.0"\n'
        b'[project.scripts]\nwuwaterm-telegram = "wuwaterm_telegram.cli:main"\n'
    )
    info_tg = "wuwaterm_telegram-0.1.0.dist-info/"
    launcher_tg = pip_launcher("/app/.venv/bin/python", "wuwaterm_telegram.cli", "main")
    tg = dist(
        "wuwaterm-telegram", "0.1.0",
        {**fx_tg.tg_pkg, info_tg + "entry_points.txt": b"[console_scripts]\nwuwaterm-telegram = wuwaterm_telegram.cli:main\n",
         "../../../bin/wuwaterm-telegram": launcher_tg},
    )
    core = dist("wuwaterm", "0.7.0", fx_tg.pkg)
    base_tg = {"app/scripts/install_probe.py": PROBE_BYTES, "app/.venv/bin/wuwaterm-telegram": launcher_tg}
    base_tg.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg}.items() if not k.startswith("../")})
    modes_tg = {"app/.venv/bin/wuwaterm-telegram": 0o755}
    report_tg = report_for(base_tg, modes_tg, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers_tg = [layer(base_tg, modes=modes_tg), layer({art.REPORT_PATH.lstrip("/"): report_tg})]
    attempt_tg = fx_tg.build(layers_tg, source_files=src_tg)
    wheels_tg = fx_tg.write_wheels(
        attempt_tg,
        extra_files={"wuwaterm-telegram": {info_tg + "entry_points.txt": b"[console_scripts]\nwuwaterm-telegram = wuwaterm_telegram.cli:main\n"}},
    )
    image_tg = art.read_oci_archive(attempt_tg / "bundle" / art.ARCHIVE_FILE)
    verified_tg = art.seal_bundle(
        fx_tg.delivery, fx_tg.attempt_id, product=fx_tg.product, commit=TG_COMMIT, tree=fx_tg.source.tree,
        iid=image_tg.manifest_digest, build_script_sha256="c" * 64, base_images=[BASES[1]],
        build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
        compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels_tg,
        core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels_tg[0]["sha256"],
                      "core_source_commit": COMMIT, "compatibility": "exact"},
    )
    assert art.verify_bundle(verified_tg.path).sha256 == verified_tg.sha256


def test_undeclared_binary_or_outside_file_in_record_is_rejected(tmp_path: Path) -> None:
    # 1. Undeclared ../../../bin/evil in wheels mode (wuwaterm-telegram)
    fx_tg_bin = Fixture(tmp_path / "tg_bin", "wuwaterm-telegram")
    tg_bin = dist("wuwaterm-telegram", "0.1.0", {**fx_tg_bin.tg_pkg, "../../../bin/evil": b"evil"})
    core_tg = dist("wuwaterm", "0.7.0", fx_tg_bin.pkg)
    base_tg_bin = {"app/scripts/install_probe.py": PROBE_BYTES, "app/.venv/bin/evil": b"evil"}
    base_tg_bin.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core_tg, **tg_bin}.items() if not k.startswith("../")})
    modes_tg_bin = {"app/.venv/bin/evil": 0o755}
    report_tg_bin = report_for(base_tg_bin, modes_tg_bin, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers_tg_bin = [layer(base_tg_bin, modes=modes_tg_bin), layer({art.REPORT_PATH.lstrip("/"): report_tg_bin})]
    attempt_tg_bin = fx_tg_bin.build(layers_tg_bin)
    wheels_tg_bin = fx_tg_bin.write_wheels(attempt_tg_bin)
    image_tg_bin = art.read_oci_archive(attempt_tg_bin / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="installed file is not in verified wheel: ../../../bin/evil"):
        art.seal_bundle(
            fx_tg_bin.delivery, fx_tg_bin.attempt_id, product=fx_tg_bin.product, commit=TG_COMMIT,
            tree=fx_tg_bin.source.tree, iid=image_tg_bin.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels_tg_bin,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels_tg_bin[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
        )

    # 2. Undeclared ../outside.py in wheels mode (wuwaterm-telegram)
    fx_tg_out = Fixture(tmp_path / "tg_out", "wuwaterm-telegram")
    tg_out = dist("wuwaterm-telegram", "0.1.0", {**fx_tg_out.tg_pkg, "../outside.py": b"outside"})
    target_out = posixpath.normpath(posixpath.join(SITE, "../outside.py")).lstrip("/")
    base_tg_out = {"app/scripts/install_probe.py": PROBE_BYTES, target_out: b"outside"}
    base_tg_out.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core_tg, **tg_out}.items() if not k.startswith("../")})
    report_tg_out = report_for(base_tg_out, {}, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers_tg_out = [layer(base_tg_out), layer({art.REPORT_PATH.lstrip("/"): report_tg_out})]
    attempt_tg_out = fx_tg_out.build(layers_tg_out)
    wheels_tg_out = fx_tg_out.write_wheels(attempt_tg_out)
    image_tg_out = art.read_oci_archive(attempt_tg_out / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match=r"installed file is not in verified wheel: \.\./outside\.py"):
        art.seal_bundle(
            fx_tg_out.delivery, fx_tg_out.attempt_id, product=fx_tg_out.product, commit=TG_COMMIT,
            tree=fx_tg_out.source.tree, iid=image_tg_out.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels_tg_out,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels_tg_out[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
        )

    # 3. Undeclared ../../../bin/evil in source_packages mode (wuwaterm-api)
    fx_api_bin = Fixture(tmp_path / "api_bin")
    site_api_bin = dist("wuwaterm", fx_api_bin.version, {**fx_api_bin.pkg, "../../../bin/evil": b"evil"},
                        {"url": "file:///app", "dir_info": {"editable": False}})
    base_api_bin = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "app/.venv/bin/evil": b"evil"}
    base_api_bin.update({SITE.lstrip("/") + "/" + k: v for k, v in site_api_bin.items() if not k.startswith("../")})
    modes_api_bin = {"entrypoint.sh": 0o755, "app/.venv/bin/evil": 0o755}
    report_api_bin = report_for(base_api_bin, modes_api_bin, [("wuwaterm", fx_api_bin.version)], ["/entrypoint.sh"], "core")
    layers_api_bin = [layer(base_api_bin, modes=modes_api_bin), layer({art.REPORT_PATH.lstrip("/"): report_api_bin})]
    with pytest.raises(art.ArtifactError, match="installed file is outside expected package directories: ../../../bin/evil"):
        fx_api_bin.seal(layers_api_bin)

    # 4. Undeclared ../outside.py in source_packages mode (wuwaterm-api)
    fx_api_out = Fixture(tmp_path / "api_out")
    site_api_out = dist("wuwaterm", fx_api_out.version, {**fx_api_out.pkg, "../outside.py": b"outside"},
                        {"url": "file:///app", "dir_info": {"editable": False}})
    base_api_out = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, target_out: b"outside"}
    base_api_out.update({SITE.lstrip("/") + "/" + k: v for k, v in site_api_out.items() if not k.startswith("../")})
    modes_api_out = {"entrypoint.sh": 0o755}
    report_api_out = report_for(base_api_out, modes_api_out, [("wuwaterm", fx_api_out.version)], ["/entrypoint.sh"], "core")
    layers_api_out = [layer(base_api_out, modes=modes_api_out), layer({art.REPORT_PATH.lstrip("/"): report_api_out})]
    with pytest.raises(art.ArtifactError, match=r"installed file is outside expected package directories: \.\./outside\.py"):
        fx_api_out.seal(layers_api_out)


def test_declared_console_script_tampered_or_omitted_is_rejected(tmp_path: Path) -> None:
    def make_setup(case_name: str, launcher_data: bytes, mode: int = 0o755, omit_record: bool = False):
        fx = Fixture(tmp_path / case_name)
        src = fx.source_files()
        src["pyproject.toml"] = (
            b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n'
            b'[project.scripts]\nwuwaterm = "wuwaterm.cli:main"\n'
        )
        info = "wuwaterm-0.7.0.dev1.dist-info/"
        extra = {info + "entry_points.txt": b"[console_scripts]\nwuwaterm = wuwaterm.cli:main\n"}
        if not omit_record:
            extra["../../../bin/wuwaterm"] = launcher_data
        site = dist("wuwaterm", fx.version, {**fx.pkg, **extra}, {"url": "file:///app", "dir_info": {"editable": False}})
        base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "app/.venv/bin/wuwaterm": launcher_data}
        base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items() if not k.startswith("../")})
        modes = {"entrypoint.sh": 0o755, "app/.venv/bin/wuwaterm": mode}
        report = report_for(base, modes, [("wuwaterm", fx.version)], ["/entrypoint.sh"], "core")
        layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
        return fx, layers, src

    # 1. Tampered module: from evil import main
    bad_module_launcher = (
        b"#!/app/.venv/bin/python\n# -*- coding: utf-8 -*-\nimport sys\nfrom evil import main\n"
        b'if __name__ == "__main__":\n'
        b'    if sys.argv[0].endswith("-script.pyw"): sys.argv[0] = sys.argv[0][:-11]\n'
        b'    elif sys.argv[0].endswith(".exe"): sys.argv[0] = sys.argv[0][:-4]\n'
        b"    sys.exit(main())\n"
    )
    fx_mod, layers_mod, src_mod = make_setup("bad_mod", bad_module_launcher)
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        fx_mod.seal(layers_mod, source_files=src_mod)
    bundle_mod = fx_mod.delivery / "attempts" / fx_mod.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_mod.exists()
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        art.verify_bundle(bundle_mod)

    # 2. Tampered shebang: #!/usr/bin/python3
    bad_shebang_launcher = (
        b"#!/usr/bin/python3\n# -*- coding: utf-8 -*-\nimport sys\nfrom wuwaterm.cli import main\n"
        b'if __name__ == "__main__":\n'
        b'    if sys.argv[0].endswith("-script.pyw"): sys.argv[0] = sys.argv[0][:-11]\n'
        b'    elif sys.argv[0].endswith(".exe"): sys.argv[0] = sys.argv[0][:-4]\n'
        b"    sys.exit(main())\n"
    )
    fx_sh, layers_sh, src_sh = make_setup("bad_shebang", bad_shebang_launcher)
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        fx_sh.seal(layers_sh, source_files=src_sh)
    bundle_sh = fx_sh.delivery / "attempts" / fx_sh.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_sh.exists()
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        art.verify_bundle(bundle_sh)

    # 3. Arbitrary payload
    payload_launcher = b"#!/bin/sh\necho evil\n"
    fx_pay, layers_pay, src_pay = make_setup("bad_payload", payload_launcher)
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        fx_pay.seal(layers_pay, source_files=src_pay)
    bundle_pay = fx_pay.delivery / "attempts" / fx_pay.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_pay.exists()
    with pytest.raises(art.ArtifactError, match="console script launcher differs from entry point"):
        art.verify_bundle(bundle_pay)

    # 4. Non-executable mode (0o644)
    valid_launcher = uv_launcher("/app/.venv/bin/python", "wuwaterm.cli", "main")
    fx_mode, layers_mode, src_mode = make_setup("bad_mode", valid_launcher, mode=0o644)
    with pytest.raises(art.ArtifactError, match="console script is not executable"):
        fx_mode.seal(layers_mode, source_files=src_mode)
    bundle_mode = fx_mode.delivery / "attempts" / fx_mode.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_mode.exists()
    with pytest.raises(art.ArtifactError, match="console script is not executable"):
        art.verify_bundle(bundle_mode)

    # 5. Omitted from RECORD when entry_points.txt declares it
    fx_omit, layers_omit, src_omit = make_setup("omitted", valid_launcher, omit_record=True)
    with pytest.raises(art.ArtifactError, match="declared console script is not installed: wuwaterm"):
        fx_omit.seal(layers_omit, source_files=src_omit)
    bundle_omit = fx_omit.delivery / "attempts" / fx_omit.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle_omit.exists()
    with pytest.raises(art.ArtifactError, match="declared console script is not installed: wuwaterm"):
        art.verify_bundle(bundle_omit)


def test_entrypoint_mode_rejection(tmp_path: Path) -> None:
    # 1. Image /entrypoint.sh has mode 0o644 while source has 0o755
    fx1 = Fixture(tmp_path / "img_mode_644")
    site1, pkgs1 = fx1.installed()
    base1 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base1.update({SITE.lstrip("/") + "/" + k: v for k, v in site1.items()})
    modes1 = {"entrypoint.sh": 0o644}
    report1 = report_for(base1, modes1, pkgs1, ["/entrypoint.sh"], "core")
    layers1 = [layer(base1, modes=modes1), layer({art.REPORT_PATH.lstrip("/"): report1})]
    with pytest.raises(art.ArtifactError, match="extra file differs from the sealed source archive: /entrypoint.sh"):
        fx1.seal(layers1)
    bundle1 = fx1.delivery / "attempts" / fx1.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle1.exists()
    with pytest.raises(art.ArtifactError, match="extra file differs from the sealed source archive: /entrypoint.sh"):
        art.verify_bundle(bundle1)

    # 2. Both source and image have mode 0o644 (non-executable extra file)
    fx2 = Fixture(tmp_path / "both_mode_644")
    src2 = fx2.source_files()
    attempt2 = art.begin_delivery(fx2.delivery, fx2.attempt_id, fx2.product)
    entries2 = [(p, "file", d, 0o644 if p.endswith(".sh") else 0o644) for p, d in sorted(src2.items())]
    path2 = attempt2 / "inputs" / "source.tar"
    path2.write_bytes(tar_bytes(entries2, pax={"comment": COMMIT}))
    fx2.source = art.read_source_archive(path2)
    site2, pkgs2 = fx2.installed()
    base2 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base2.update({SITE.lstrip("/") + "/" + k: v for k, v in site2.items()})
    modes2 = {"entrypoint.sh": 0o644}
    report2 = report_for(base2, modes2, pkgs2, ["/entrypoint.sh"], "core")
    layers2 = [layer(base2, modes=modes2), layer({art.REPORT_PATH.lstrip("/"): report2})]
    raw2 = oci_archive(layers2)
    (attempt2 / "bundle" / art.ARCHIVE_FILE).write_bytes(raw2)
    image2 = art.read_oci_archive(attempt2 / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="extra file is not executable: /entrypoint.sh"):
        art.seal_bundle(fx2.delivery, fx2.attempt_id, product=fx2.product, commit=COMMIT,
                        tree=fx2.source.tree, iid=image2.manifest_digest,
                        build_script_sha256="c" * 64, base_images=BASES,
                        build_args=[{"name": "SOURCE_COMMIT", "value": COMMIT}], compatibility=COMPAT,
                        authority="local-source")
    bundle2 = attempt2 / "bundle" / art.BUNDLE_FILE
    assert bundle2.exists()
    with pytest.raises(art.ArtifactError, match="extra file is not executable: /entrypoint.sh"):
        art.verify_bundle(bundle2)


def test_omitted_entry_points_txt_rejected(tmp_path: Path) -> None:
    fx = Fixture(tmp_path / "omit_ep")
    src = fx.source_files()
    src["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n'
        b'[project.scripts]\nwuwaterm = "wuwaterm.cli:main"\n'
    )
    site = dist("wuwaterm", fx.version, fx.pkg, {"url": "file:///app", "dir_info": {"editable": False}})
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, [("wuwaterm", fx.version)], ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="installed distribution is missing entry_points.txt for project.scripts"):
        fx.seal(layers, source_files=src)
    bundle = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    assert bundle.exists()
    with pytest.raises(art.ArtifactError, match="installed distribution is missing entry_points.txt for project.scripts"):
        art.verify_bundle(bundle)


def test_data_member_bin_bypass_rejected(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "wuwaterm-telegram")
    evil_bin = b"#!/bin/sh\necho evil\n"
    core = dist("wuwaterm", "0.7.0", fx.pkg)
    tg = dist("wuwaterm-telegram", "0.1.0", {**fx.tg_pkg, "../../../bin/wuwaterm-telegram": evil_bin})
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "app/.venv/bin/wuwaterm-telegram": evil_bin}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in {**core, **tg}.items() if not k.startswith("../")})
    modes = {"app/.venv/bin/wuwaterm-telegram": 0o755}
    report = report_for(base, modes, [("wuwaterm", "0.7.0"), ("wuwaterm-telegram", "0.1.0")], [], "telegram")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    attempt = fx.build(layers)
    extra_files = {"wuwaterm-telegram": {"wuwaterm_telegram-0.1.0.data/data/bin/wuwaterm-telegram": evil_bin}}
    wheels = fx.write_wheels(attempt, extra_files=extra_files)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="data file cannot target bin directory: ../../../bin/wuwaterm-telegram"):
        art.seal_bundle(
            fx.delivery, fx.attempt_id, product=fx.product, commit=TG_COMMIT,
            tree=fx.source.tree, iid=image.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
        )


def test_startup_hooks_and_unrecorded_bin_rejected(tmp_path: Path) -> None:
    # 1. Unrecorded sitecustomize.py in site-packages
    fx_site = Fixture(tmp_path / "sitecust")
    site_sc, pkgs_sc = fx_site.installed()
    base_sc = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
               SITE.lstrip("/") + "/sitecustomize.py": b"# evil\n"}
    base_sc.update({SITE.lstrip("/") + "/" + k: v for k, v in site_sc.items()})
    modes_sc = {"entrypoint.sh": 0o755}
    report_sc = report_for(base_sc, modes_sc, pkgs_sc, ["/entrypoint.sh"], "core")
    layers_sc = [layer(base_sc, modes=modes_sc), layer({art.REPORT_PATH.lstrip("/"): report_sc})]
    with pytest.raises(art.ArtifactError, match="unauthorized sitecustomize.py in site-packages"):
        fx_site.seal(layers_sc)

    # 2. Unrecorded evil.pth in site-packages
    fx_pth = Fixture(tmp_path / "evil_pth")
    site_pth, pkgs_pth = fx_pth.installed()
    base_pth = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
                SITE.lstrip("/") + "/evil.pth": b"import evil\n"}
    base_pth.update({SITE.lstrip("/") + "/" + k: v for k, v in site_pth.items()})
    modes_pth = {"entrypoint.sh": 0o755}
    report_pth = report_for(base_pth, modes_pth, pkgs_pth, ["/entrypoint.sh"], "core")
    layers_pth = [layer(base_pth, modes=modes_pth), layer({art.REPORT_PATH.lstrip("/"): report_pth})]
    with pytest.raises(art.ArtifactError, match=r"unauthorized \.pth file in site-packages"):
        fx_pth.seal(layers_pth)

    # 3. Unrecorded /app/.venv/bin/wuwaterm binary
    fx_bin = Fixture(tmp_path / "unrec_bin")
    site_bin, pkgs_bin = fx_bin.installed()
    base_bin = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
                "app/.venv/bin/wuwaterm": b"#!/bin/sh\n"}
    base_bin.update({SITE.lstrip("/") + "/" + k: v for k, v in site_bin.items()})
    modes_bin = {"entrypoint.sh": 0o755, "app/.venv/bin/wuwaterm": 0o755}
    report_bin = report_for(base_bin, modes_bin, pkgs_bin, ["/entrypoint.sh"], "core")
    layers_bin = [layer(base_bin, modes=modes_bin), layer({art.REPORT_PATH.lstrip("/"): report_bin})]
    with pytest.raises(art.ArtifactError, match="unrecorded product console script in image: /app/.venv/bin/wuwaterm"):
        fx_bin.seal(layers_bin)


def test_invalid_runtime_config_and_interpreter_rejected(tmp_path: Path) -> None:
    # 1. WorkingDir mismatch
    fx_wd = Fixture(tmp_path / "bad_wd")
    with pytest.raises(art.ArtifactError, match="runtime config WorkingDir must be /app"):
        fx_wd.seal(config_extra={"WorkingDir": "/root"})

    # 2. Entrypoint mismatch
    fx_ep = Fixture(tmp_path / "bad_ep")
    with pytest.raises(art.ArtifactError, match="invalid runtime config Entrypoint or Cmd for wuwaterm-api"):
        fx_ep.seal(config_extra={"Entrypoint": ["/bin/sh"]})

    # 3. Cmd mismatch
    fx_cmd = Fixture(tmp_path / "bad_cmd")
    with pytest.raises(art.ArtifactError, match="invalid runtime config Entrypoint or Cmd for wuwaterm-api"):
        fx_cmd.seal(config_extra={"Cmd": ["bash"]})

    # 4. Env with PYTHONPATH
    fx_py = Fixture(tmp_path / "bad_pythonpath")
    with pytest.raises(art.ArtifactError, match="forbidden env variable: PYTHONPATH"):
        fx_py.seal(config_extra={"Env": ["PYTHONPATH=/evil"]})

    # 5. Env with LD_PRELOAD
    fx_ld = Fixture(tmp_path / "bad_ld_preload")
    with pytest.raises(art.ArtifactError, match="forbidden env variable: LD_PRELOAD"):
        fx_ld.seal(config_extra={"Env": ["LD_PRELOAD=/libevil.so"]})

    # 6. Mismatched revision label
    fx_rev = Fixture(tmp_path / "bad_rev")
    with pytest.raises(art.ArtifactError, match="runtime config revision label differs from bundle source commit"):
        fx_rev.seal(config_extra={"Labels": {"org.opencontainers.image.revision": "bad" * 10}})

    # 7. Non-executable interpreter in image rootfs
    fx_interp = Fixture(tmp_path / "bad_interp")
    site, pkgs = fx_interp.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
            "app/.venv/bin/python": b"#!/bin/sh\n"}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755, "app/.venv/bin/python": 0o644}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="interpreter in the image is not executable: /app/.venv/bin/python"):
        fx_interp.seal(layers)


_PRECOMPUTED_311_PYC: dict[tuple[str, bytes], bytes] = {
    ("/app/.venv/lib/python3.11/site-packages/wuwaterm/__init__.py", b"def f(x):\n    return x + 1\n"): (
        b'\xa7\r\r\n\x00\x00\x00\x00\x00\x00\x00\x00\x1b\x00\x00\x00\xe3\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00d\x00\x84\x00Z\x00d\x01S\x00)\x02c\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00|\x00d\x01z\x00\x00\x00S\x00)\x02N\xe9\x01\x00\x00\x00\xa9\x00)\x01\xda\x01xs\x01\x00\x00\x00 \xfa</app/.venv/lib/python3.11/site-packages/wuwaterm/__init__.py\xda\x01fr\x07\x00\x00\x00\x01\x00\x00\x00s\x0b\x00\x00\x00\x80\x00\xd8\x0b\x0c\x88q\x895\x80L\xf3\x00\x00\x00\x00N)\x01r\x07\x00\x00\x00r\x04\x00\x00\x00r\x08\x00\x00\x00r\x06\x00\x00\x00\xfa\x08<module>r\t\x00\x00\x00\x01\x00\x00\x00s\x1e\x00\x00\x00\xf0\x03\x01\x01\x01\xf0\x02\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11r\x08\x00\x00\x00'
    ),
    ("/app/.venv/lib/python3.11/site-packages/wuwaterm/__init__.py", b"def f(x):\n    return x - 1\n"): (
        b'\xa7\r\r\n\x00\x00\x00\x00\x00\x00\x00\x00\x1b\x00\x00\x00\xe3\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00d\x00\x84\x00Z\x00d\x01S\x00)\x02c\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00|\x00d\x01z\n\x00\x00S\x00)\x02N\xe9\x01\x00\x00\x00\xa9\x00)\x01\xda\x01xs\x01\x00\x00\x00 \xfa</app/.venv/lib/python3.11/site-packages/wuwaterm/__init__.py\xda\x01fr\x07\x00\x00\x00\x01\x00\x00\x00s\x0b\x00\x00\x00\x80\x00\xd8\x0b\x0c\x88q\x895\x80L\xf3\x00\x00\x00\x00N)\x01r\x07\x00\x00\x00r\x04\x00\x00\x00r\x08\x00\x00\x00r\x06\x00\x00\x00\xfa\x08<module>r\t\x00\x00\x00\x01\x00\x00\x00s\x1e\x00\x00\x00\xf0\x03\x01\x01\x01\xf0\x02\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11r\x08\x00\x00\x00'
    ),
    ("/usr/local/lib/python3.11/site-packages/wuwaterm/__init__.py", b"def f(x):\n    return x + 1\n"): (
        b'\xa7\r\r\n\x00\x00\x00\x00\x00\x00\x00\x00\x1b\x00\x00\x00\xe3\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00d\x00\x84\x00Z\x00d\x01S\x00)\x02c\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00|\x00d\x01z\x00\x00\x00S\x00)\x02N\xe9\x01\x00\x00\x00\xa9\x00)\x01\xda\x01xs\x01\x00\x00\x00 \xfa</usr/local/lib/python3.11/site-packages/wuwaterm/__init__.py\xda\x01fr\x07\x00\x00\x00\x01\x00\x00\x00s\x0b\x00\x00\x00\x80\x00\xd8\x0b\x0c\x88q\x895\x80L\xf3\x00\x00\x00\x00N)\x01r\x07\x00\x00\x00r\x04\x00\x00\x00r\x08\x00\x00\x00r\x06\x00\x00\x00\xfa\x08<module>r\t\x00\x00\x00\x01\x00\x00\x00s\x1e\x00\x00\x00\xf0\x03\x01\x01\x01\xf0\x02\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11r\x08\x00\x00\x00'
    ),
    ("/usr/local/lib/python3.11/site-packages/wuwaterm/__init__.py", b"def f(x):\n    return x - 1\n"): (
        b'\xa7\r\r\n\x00\x00\x00\x00\x00\x00\x00\x00\x1b\x00\x00\x00\xe3\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00d\x00\x84\x00Z\x00d\x01S\x00)\x02c\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x00\x00\x03\x00\x00\x00\xf3\x0c\x00\x00\x00\x97\x00|\x00d\x01z\n\x00\x00S\x00)\x02N\xe9\x01\x00\x00\x00\xa9\x00)\x01\xda\x01xs\x01\x00\x00\x00 \xfa</usr/local/lib/python3.11/site-packages/wuwaterm/__init__.py\xda\x01fr\x07\x00\x00\x00\x01\x00\x00\x00s\x0b\x00\x00\x00\x80\x00\xd8\x0b\x0c\x88q\x895\x80L\xf3\x00\x00\x00\x00N)\x01r\x07\x00\x00\x00r\x04\x00\x00\x00r\x08\x00\x00\x00r\x06\x00\x00\x00\xfa\x08<module>r\t\x00\x00\x00\x01\x00\x00\x00s\x1e\x00\x00\x00\xf0\x03\x01\x01\x01\xf0\x02\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11\xf0\x00\x01\x01\x11r\x08\x00\x00\x00'
    ),
}


def compile_311_pyc(py_bytes: bytes, filename: str, mtime: int = 0, source_size: int | None = None, flags: int = 0) -> bytes:
    if source_size is None:
        source_size = len(py_bytes)
    magic = b"\xa7\r\r\n"
    if sys.version_info[:2] == (3, 11):
        co = compile(py_bytes, filename, "exec", dont_inherit=True, optimize=0)
        payload = marshal.dumps(co)
    else:
        interp = art._find_trusted_python(3, 11)
        if interp is not None:
            cmd = [interp, "-I", "-B", "-c", """
import sys, marshal
filename = sys.argv[1]
source = sys.stdin.buffer.read()
co = compile(source, filename, "exec", dont_inherit=True, optimize=0)
sys.stdout.buffer.write(marshal.dumps(co))
""", filename]
            proc = subprocess.run(cmd, input=py_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            payload = proc.stdout
        elif (filename, py_bytes) in _PRECOMPUTED_311_PYC:
            payload = _PRECOMPUTED_311_PYC[(filename, py_bytes)][16:]
        else:
            raise RuntimeError(f"Python 3.11 interpreter not found to compile {filename}")
    header = struct.pack("<4sIII", magic, flags, mtime, source_size)
    return header + payload


def test_bytecode_pep552_hash_pyc_rejected(tmp_path: Path) -> None:
    # Codex reproduction: unrecorded wuwaterm/__pycache__/__init__.cpython-XY.pyc
    # with PEP 552 unchecked-hash (flags=1) returning VERSION = 999
    fx = Fixture(tmp_path / "pep552")
    site, pkgs = fx.installed()
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    xy = f"{sys.version_info.major}{sys.version_info.minor}"
    magic = importlib.util.MAGIC_NUMBER
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    co_evil = compile(b"VERSION = 999\n", "/" + py_full, "exec", dont_inherit=True, optimize=0)
    raw_pyc = struct.pack("<4sIII", magic, 1, 0, 0) + marshal.dumps(co_evil)
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=cur_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="hash-based .pyc bytecode is forbidden"):
        fx.seal(layers)
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="hash-based .pyc bytecode is forbidden"):
        art.verify_bundle(bundle_path)


def test_bytecode_forged_timestamp_pyc_rejected(tmp_path: Path) -> None:
    # Forged timestamp-based wuwaterm/__pycache__/__init__.cpython-XY.pyc (flags=0)
    # matching (mtime, size) for wuwaterm/__init__.py, but code object sets VERSION = 999
    fx = Fixture(tmp_path / "forged_ts")
    site, pkgs = fx.installed()
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    xy = f"{sys.version_info.major}{sys.version_info.minor}"
    magic = importlib.util.MAGIC_NUMBER
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    py_bytes = fx.pkg["wuwaterm/__init__.py"]
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    co_evil = compile(b"VERSION = 999\n", "/" + py_full, "exec", dont_inherit=True, optimize=0)
    raw_pyc = struct.pack("<4sIII", magic, 0, 0, len(py_bytes)) + marshal.dumps(co_evil)
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=cur_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        fx.seal(layers)
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        art.verify_bundle(bundle_path)


def test_bytecode_valid_timestamp_pyc_accepted(tmp_path: Path) -> None:
    # Valid timestamp-based wuwaterm/__pycache__/__init__.cpython-XY.pyc (flags=0)
    # matching (mtime, size) and matching compiled code object for VERSION = 1
    fx = Fixture(tmp_path / "valid_pyc")
    site, pkgs = fx.installed()
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    xy = f"{sys.version_info.major}{sys.version_info.minor}"
    magic = importlib.util.MAGIC_NUMBER
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    py_bytes = fx.pkg["wuwaterm/__init__.py"]
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    co_valid = compile(py_bytes, "/" + py_full, "exec", dont_inherit=True, optimize=0)
    raw_pyc = struct.pack("<4sIII", magic, 0, 0, len(py_bytes)) + marshal.dumps(co_valid)
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=cur_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    verified = fx.seal(layers)
    assert verified is not None
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    verified2 = art.verify_bundle(bundle_path)
    assert verified2.sha256 == verified.sha256


def test_bytecode_forged_bool_constant_rejected(tmp_path: Path) -> None:
    # Forged timestamp-based .pyc flipping FLAG = True -> FLAG = False is rejected
    fx = Fixture(tmp_path / "forged_bool")
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    xy = f"{sys.version_info.major}{sys.version_info.minor}"
    magic = importlib.util.MAGIC_NUMBER
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    py_bytes = b"FLAG = True\n"
    fx.pkg["wuwaterm/__init__.py"] = py_bytes
    site, pkgs = fx.installed()
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    co_evil = compile(b"FLAG = False\n", "/" + py_full, "exec", dont_inherit=True, optimize=0)
    raw_pyc = struct.pack("<4sIII", magic, 0, 0, len(py_bytes)) + marshal.dumps(co_evil)
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=cur_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        fx.seal(layers)
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        art.verify_bundle(bundle_path)


def test_bytecode_forged_constant_order_rejected(tmp_path: Path) -> None:
    # Forged timestamp-based .pyc swapping constant order PAIR = (1, 2) -> PAIR = (2, 1) is rejected
    fx = Fixture(tmp_path / "forged_order")
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    xy = f"{sys.version_info.major}{sys.version_info.minor}"
    magic = importlib.util.MAGIC_NUMBER
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    py_bytes = b"PAIR = (1, 2)\n"
    fx.pkg["wuwaterm/__init__.py"] = py_bytes
    site, pkgs = fx.installed()
    co_evil = compile(b"PAIR = (2, 1)\n", "/" + py_full, "exec", dont_inherit=True, optimize=0)
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    raw_pyc = struct.pack("<4sIII", magic, 0, 0, len(py_bytes)) + marshal.dumps(co_evil)
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=cur_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        fx.seal(layers)
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
        art.verify_bundle(bundle_path)


def test_bytecode_cross_version_operator_change_rejected(tmp_path: Path) -> None:
    # Codex reproduction in PRRT_kwDOS82bU860-g2N:
    # Source is def f(x):\n    return x + 1\n
    # Pyc is genuine Python 3.11 bytecode compiled from def f(x):\n    return x - 1\n
    # Header flags=0, matching (mtime, size) of source
    fx = Fixture(tmp_path / "operator_change")
    site_prefix = SITE.lstrip("/")
    py_full = site_prefix + "/wuwaterm/__init__.py"
    py_bytes = b"def f(x):\n    return x + 1\n"
    evil_bytes = b"def f(x):\n    return x - 1\n"
    fx.pkg["wuwaterm/__init__.py"] = py_bytes
    site, pkgs = fx.installed()
    pyc_full = site_prefix + "/wuwaterm/__pycache__/__init__.cpython-311.pyc"
    raw_pyc = compile_311_pyc(evil_bytes, "/" + py_full, mtime=0, source_size=len(py_bytes))
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version="3.11.16")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]

    has_trusted_311 = art._find_trusted_python(3, 11) is not None or sys.version_info[:2] == (3, 11)
    expected_match = (
        "compiled .pyc bytecode differs from verified source"
        if has_trusted_311
        else "cannot verify cross-version .pyc bytecode without trusted python3.11 interpreter"
    )
    with pytest.raises(art.ArtifactError, match=expected_match):
        fx.seal(layers)
    bundle_path = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match=expected_match):
        art.verify_bundle(bundle_path)

    # AND if any cross-version interpreter ((3, 11) or (3, 12) != sys.version_info[:2]) is available
    # via art._find_trusted_python, explicitly verify that valid cross-version return x + 1 .pyc is
    # accepted by fx.seal and art.verify_bundle, while forged return x - 1 .pyc is rejected.
    cross_candidates = [(3, 11), (3, 12)]
    cross_target = None
    for cand in cross_candidates:
        if cand != sys.version_info[:2] and art._find_trusted_python(*cand) is not None:
            cross_target = cand
            break

    if cross_target is not None:
        c_maj, c_min = cross_target
        c_ver = f"{c_maj}.{c_min}.0"
        c_xy = f"{c_maj}{c_min}"
        interp = art._find_trusted_python(c_maj, c_min)
        assert interp is not None

        def _compile_cross(src: bytes) -> bytes:
            cmd = [interp, "-I", "-B", "-c", """
import importlib.util, marshal, struct, sys
filename = sys.argv[1]
source = sys.stdin.buffer.read()
co = compile(source, filename, "exec", dont_inherit=True, optimize=0)
payload = marshal.dumps(co)
magic = importlib.util.MAGIC_NUMBER
sys.stdout.buffer.write(magic + payload)
""", "/" + py_full]
            proc = subprocess.run(cmd, input=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            magic = proc.stdout[:4]
            payload = proc.stdout[4:]
            return magic + struct.pack("<III", 0, 0, len(py_bytes)) + payload

        # 1. Valid cross-version return x + 1 .pyc is accepted
        fx_valid = Fixture(tmp_path / "cross_valid")
        fx_valid.pkg["wuwaterm/__init__.py"] = py_bytes
        site_v, pkgs_v = fx_valid.installed()
        pyc_path_v = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{c_xy}.pyc"
        valid_pyc = _compile_cross(py_bytes)
        base_v = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_path_v: valid_pyc}
        base_v.update({site_prefix + "/" + k: v for k, v in site_v.items()})
        rep_v = report_for(base_v, modes, pkgs_v, ["/entrypoint.sh"], "core", python_version=c_ver)
        layers_v = [layer(base_v, modes=modes), layer({art.REPORT_PATH.lstrip("/"): rep_v})]
        verified_v = fx_valid.seal(layers_v)
        assert verified_v is not None
        bp_v = fx_valid.delivery / "attempts" / fx_valid.attempt_id / "bundle" / art.BUNDLE_FILE
        assert art.verify_bundle(bp_v).sha256 == verified_v.sha256

        # 2. Forged cross-version return x - 1 .pyc is rejected
        fx_forged = Fixture(tmp_path / "cross_forged")
        fx_forged.pkg["wuwaterm/__init__.py"] = py_bytes
        site_f, pkgs_f = fx_forged.installed()
        pyc_path_f = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{c_xy}.pyc"
        evil_pyc = _compile_cross(evil_bytes)
        base_f = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_path_f: evil_pyc}
        base_f.update({site_prefix + "/" + k: v for k, v in site_f.items()})
        rep_f = report_for(base_f, modes, pkgs_f, ["/entrypoint.sh"], "core", python_version=c_ver)
        layers_f = [layer(base_f, modes=modes), layer({art.REPORT_PATH.lstrip("/"): rep_f})]
        with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
            fx_forged.seal(layers_f)
        bp_f = fx_forged.delivery / "attempts" / fx_forged.attempt_id / "bundle" / art.BUNDLE_FILE
        with pytest.raises(art.ArtifactError, match="compiled .pyc bytecode differs from verified source"):
            art.verify_bundle(bp_f)


def test_bytecode_missing_trusted_python_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = Fixture(tmp_path / "missing_trusted_py")
    site, pkgs = fx.installed()
    site_prefix = SITE.lstrip("/")
    init_py = fx.pkg["wuwaterm/__init__.py"]
    if sys.version_info[:2] == (3, 11):
        target_ver = "3.12.0"
        xy = "312"
        magic = b"\xcb\r\r\n"
    else:
        target_ver = "3.11.16"
        xy = "311"
        magic = b"\xa7\r\r\n"
    pyc_full = site_prefix + f"/wuwaterm/__pycache__/__init__.cpython-{xy}.pyc"
    raw_pyc = magic + struct.pack("<III", 0, 0, len(init_py)) + b"x"
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, pyc_full: raw_pyc}
    base.update({site_prefix + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core", python_version=target_ver)
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    monkeypatch.setattr(art, "_find_trusted_python", lambda major, minor: None)
    with pytest.raises(art.ArtifactError, match="cannot verify cross-version .pyc bytecode without trusted"):
        fx.seal(layers)


def test_app_shadowing_and_runtime_hooks_rejected(tmp_path: Path) -> None:
    # 1. Shadowing in /app: /app/wuwaterm.py
    fx1 = Fixture(tmp_path / "shadow_py")
    site1, pkgs1 = fx1.installed()
    base1 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "app/wuwaterm.py": b"# evil\n"}
    base1.update({SITE.lstrip("/") + "/" + k: v for k, v in site1.items()})
    modes1 = {"entrypoint.sh": 0o755}
    report1 = report_for(base1, modes1, pkgs1, ["/entrypoint.sh"], "core")
    layers1 = [layer(base1, modes=modes1), layer({art.REPORT_PATH.lstrip("/"): report1})]
    with pytest.raises(art.ArtifactError, match="unauthorized executable or package file directly in /app"):
        fx1.seal(layers1)

    # 2. Shadowing in /app: /app/evil/__init__.py
    fx2 = Fixture(tmp_path / "shadow_dir")
    site2, pkgs2 = fx2.installed()
    base2 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "app/evil/__init__.py": b"# evil\n"}
    base2.update({SITE.lstrip("/") + "/" + k: v for k, v in site2.items()})
    modes2 = {"entrypoint.sh": 0o755}
    report2 = report_for(base2, modes2, pkgs2, ["/entrypoint.sh"], "core")
    layers2 = [layer(base2, modes=modes2), layer({art.REPORT_PATH.lstrip("/"): report2})]
    with pytest.raises(art.ArtifactError, match="unauthorized directory in /app"):
        fx2.seal(layers2)

    # 3. Tampered _virtualenv.pth content
    fx3 = Fixture(tmp_path / "bad_vpth")
    site3, pkgs3 = fx3.installed()
    base3 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             SITE.lstrip("/") + "/_virtualenv.pth": b"import evil\n"}
    base3.update({SITE.lstrip("/") + "/" + k: v for k, v in site3.items()})
    modes3 = {"entrypoint.sh": 0o755}
    report3 = report_for(base3, modes3, pkgs3, ["/entrypoint.sh"], "core")
    layers3 = [layer(base3, modes=modes3), layer({art.REPORT_PATH.lstrip("/"): report3})]
    with pytest.raises(art.ArtifactError, match="unauthorized content in _virtualenv.pth"):
        fx3.seal(layers3)

    # 4. sitecustomize package (directory)
    fx4 = Fixture(tmp_path / "sitecust_dir")
    site4, pkgs4 = fx4.installed()
    base4 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             "app/sitecustomize/__init__.py": b"# evil\n"}
    base4.update({SITE.lstrip("/") + "/" + k: v for k, v in site4.items()})
    modes4 = {"entrypoint.sh": 0o755}
    report4 = report_for(base4, modes4, pkgs4, ["/entrypoint.sh"], "core")
    layers4 = [layer(base4, modes=modes4), layer({art.REPORT_PATH.lstrip("/"): report4})]
    with pytest.raises(art.ArtifactError, match="unauthorized sitecustomize module in image"):
        fx4.seal(layers4)

    # 5. sitecustomize pyc
    fx5 = Fixture(tmp_path / "sitecust_pyc")
    site5, pkgs5 = fx5.installed()
    base5 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             SITE.lstrip("/") + "/sitecustomize.cpython-311.pyc": b"\x00" * 32}
    base5.update({SITE.lstrip("/") + "/" + k: v for k, v in site5.items()})
    modes5 = {"entrypoint.sh": 0o755}
    report5 = report_for(base5, modes5, pkgs5, ["/entrypoint.sh"], "core")
    layers5 = [layer(base5, modes=modes5), layer({art.REPORT_PATH.lstrip("/"): report5})]
    with pytest.raises(art.ArtifactError, match="unauthorized sitecustomize module in image"):
        fx5.seal(layers5)

    # 6. /etc/ld.so.preload
    fx6 = Fixture(tmp_path / "ld_preload")
    site6, pkgs6 = fx6.installed()
    base6 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "etc/ld.so.preload": b"/libevil.so\n"}
    base6.update({SITE.lstrip("/") + "/" + k: v for k, v in site6.items()})
    modes6 = {"entrypoint.sh": 0o755}
    report6 = report_for(base6, modes6, pkgs6, ["/entrypoint.sh"], "core")
    layers6 = [layer(base6, modes=modes6), layer({art.REPORT_PATH.lstrip("/"): report6})]
    with pytest.raises(art.ArtifactError, match="unauthorized /etc/ld.so.preload in image"):
        fx6.seal(layers6)

    # 7. Unexpected .pyo file
    fx7 = Fixture(tmp_path / "pyo_file")
    site7, pkgs7 = fx7.installed()
    base7 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             SITE.lstrip("/") + "/wuwaterm/__pycache__/__init__.cpython-311.pyo": b"pyo"}
    base7.update({SITE.lstrip("/") + "/" + k: v for k, v in site7.items()})
    modes7 = {"entrypoint.sh": 0o755}
    report7 = report_for(base7, modes7, pkgs7, ["/entrypoint.sh"], "core")
    layers7 = [layer(base7, modes=modes7), layer({art.REPORT_PATH.lstrip("/"): report7})]
    with pytest.raises(art.ArtifactError, match="unexpected .pyo bytecode file"):
        fx7.seal(layers7)

    # 8. Stale .pyc header (mtime mismatch)
    fx8 = Fixture(tmp_path / "stale_pyc")
    site8, pkgs8 = fx8.installed()
    py_full8 = SITE.lstrip("/") + "/wuwaterm/__init__.py"
    py_bytes8 = fx8.pkg["wuwaterm/__init__.py"]
    co8 = compile(py_bytes8, "/" + py_full8, "exec", dont_inherit=True, optimize=0)
    raw_pyc8 = struct.pack("<4sIII", b"\xa7\r\r\n", 0, 99999, len(py_bytes8)) + marshal.dumps(co8)
    base8 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             SITE.lstrip("/") + "/wuwaterm/__pycache__/__init__.cpython-311.pyc": raw_pyc8}
    base8.update({SITE.lstrip("/") + "/" + k: v for k, v in site8.items()})
    modes8 = {"entrypoint.sh": 0o755}
    report8 = report_for(base8, modes8, pkgs8, ["/entrypoint.sh"], "core")
    layers8 = [layer(base8, modes=modes8), layer({art.REPORT_PATH.lstrip("/"): report8})]
    with pytest.raises(art.ArtifactError, match="stale or mismatched .pyc header"):
        fx8.seal(layers8)

    # 9. Tampered _virtualenv.py hash
    fx9 = Fixture(tmp_path / "bad_vpy")
    site9, pkgs9 = fx9.installed()
    base9 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
             SITE.lstrip("/") + "/_virtualenv.pth": b"import _virtualenv",
             SITE.lstrip("/") + "/_virtualenv.py": b"# evil virtualenv"}
    base9.update({SITE.lstrip("/") + "/" + k: v for k, v in site9.items()})
    modes9 = {"entrypoint.sh": 0o755}
    report9 = report_for(base9, modes9, pkgs9, ["/entrypoint.sh"], "core")
    layers9 = [layer(base9, modes=modes9), layer({art.REPORT_PATH.lstrip("/"): report9})]
    with pytest.raises(art.ArtifactError, match="unauthorized content in _virtualenv.py"):
        fx9.seal(layers9)

    # 10. Tampered distutils-precedence.pth hash
    fx10 = Fixture(tmp_path / "bad_distutils")
    site10, pkgs10 = fx10.installed()
    base10 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              SITE.lstrip("/") + "/distutils-precedence.pth": b"import evil"}
    base10.update({SITE.lstrip("/") + "/" + k: v for k, v in site10.items()})
    modes10 = {"entrypoint.sh": 0o755}
    report10 = report_for(base10, modes10, pkgs10, ["/entrypoint.sh"], "core")
    layers10 = [layer(base10, modes=modes10), layer({art.REPORT_PATH.lstrip("/"): report10})]
    with pytest.raises(art.ArtifactError, match="unauthorized content in distutils-precedence.pth"):
        fx10.seal(layers10)

    # 11. Tampered pyvenv.cfg include-system-site-packages = true
    fx11 = Fixture(tmp_path / "bad_pyvenv")
    site11, pkgs11 = fx11.installed()
    base11 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              "app/.venv/pyvenv.cfg": b"include-system-site-packages = true\n"}
    base11.update({SITE.lstrip("/") + "/" + k: v for k, v in site11.items()})
    modes11 = {"entrypoint.sh": 0o755}
    report11 = report_for(base11, modes11, pkgs11, ["/entrypoint.sh"], "core")
    layers11 = [layer(base11, modes=modes11), layer({art.REPORT_PATH.lstrip("/"): report11})]
    with pytest.raises(art.ArtifactError, match="pyvenv.cfg must set include-system-site-packages = false"):
        fx11.seal(layers11)

    # 12. Unexpected bytecode in app/scripts
    fx12 = Fixture(tmp_path / "bad_scripts_pyc")
    site12, pkgs12 = fx12.installed()
    base12 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              "app/scripts/__pycache__/install_probe.cpython-311.pyc": b"pyc"}
    base12.update({SITE.lstrip("/") + "/" + k: v for k, v in site12.items()})
    modes12 = {"entrypoint.sh": 0o755}
    report12 = report_for(base12, modes12, pkgs12, ["/entrypoint.sh"], "core")
    layers12 = [layer(base12, modes=modes12), layer({art.REPORT_PATH.lstrip("/"): report12})]
    with pytest.raises(art.ArtifactError, match="unexpected bytecode in app directory"):
        fx12.seal(layers12)

    # 13. Unexpected bytecode in site-packages/__pycache__/
    fx13 = Fixture(tmp_path / "bad_site_pycache")
    site13, pkgs13 = fx13.installed()
    base13 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              SITE.lstrip("/") + "/__pycache__/evil.cpython-311.pyc": b"pyc"}
    base13.update({SITE.lstrip("/") + "/" + k: v for k, v in site13.items()})
    modes13 = {"entrypoint.sh": 0o755}
    report13 = report_for(base13, modes13, pkgs13, ["/entrypoint.sh"], "core")
    layers13 = [layer(base13, modes=modes13), layer({art.REPORT_PATH.lstrip("/"): report13})]
    with pytest.raises(art.ArtifactError, match="unexpected bytecode in site-packages"):
        fx13.seal(layers13)

    # 14. Unverified Python file in app/scripts/
    fx14 = Fixture(tmp_path / "bad_app_script")
    site14, pkgs14 = fx14.installed()
    base14 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              "app/scripts/evil.py": b"# evil\n"}
    base14.update({SITE.lstrip("/") + "/" + k: v for k, v in site14.items()})
    modes14 = {"entrypoint.sh": 0o755}
    report14 = report_for(base14, modes14, pkgs14, ["/entrypoint.sh"], "core")
    layers14 = [layer(base14, modes=modes14), layer({art.REPORT_PATH.lstrip("/"): report14})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app"):
        fx14.seal(layers14)

    # 15. Missing pyvenv.cfg in virtual environment when python executable exists
    fx15 = Fixture(tmp_path / "missing_pyvenv_cfg")
    site15, pkgs15 = fx15.installed()
    base15 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              "app/.venv/bin/python": b"fake-python"}
    base15.update({SITE.lstrip("/") + "/" + k: v for k, v in site15.items()})
    modes15 = {"entrypoint.sh": 0o755, "app/.venv/bin/python": 0o755}
    report15 = report_for(base15, modes15, pkgs15, ["/entrypoint.sh"], "core")
    layers15 = [layer(base15, modes=modes15, omit_venv_files={"app/.venv/pyvenv.cfg"}), layer({art.REPORT_PATH.lstrip("/"): report15})]
    with pytest.raises(art.ArtifactError, match="missing pyvenv.cfg in virtual environment"):
        fx15.seal(layers15)

    # 16. Unauthorized .pth file anywhere in image
    fx16 = Fixture(tmp_path / "bad_pth_anywhere")
    site16, pkgs16 = fx16.installed()
    base16 = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
              "usr/local/lib/python3.11/dist-packages/evil.pth": b"import evil\n"}
    base16.update({SITE.lstrip("/") + "/" + k: v for k, v in site16.items()})
    modes16 = {"entrypoint.sh": 0o755}
    report16 = report_for(base16, modes16, pkgs16, ["/entrypoint.sh"], "core")
    layers16 = [layer(base16, modes=modes16), layer({art.REPORT_PATH.lstrip("/"): report16})]
    with pytest.raises(art.ArtifactError, match="unauthorized .pth file in image"):
        fx16.seal(layers16)


def test_missing_reported_interpreter_rejected_at_seal_and_verify(tmp_path: Path) -> None:
    # Codex reproduction PRRT_kwDOS82bU860-UgA: missing report["interpreter"]
    # in image rootfs is rejected at both seal_bundle and verify_bundle
    fx = Fixture(tmp_path / "missing_interp")
    site, pkgs = fx.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes, omit_venv_files={"app/.venv/bin/python"}),
              layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="reported interpreter is missing in the image: /app/.venv/bin/python"):
        fx.seal(layers)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()
    bundle_file = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="reported interpreter is missing in the image: /app/.venv/bin/python"):
        art.verify_bundle(bundle_file)


def test_entrypoint_missing_console_launcher_rejected(tmp_path: Path) -> None:
    # Entrypoint: ["wuwaterm-telegram"] when wuwaterm-telegram console script
    # launcher is absent from image is rejected
    fx = Fixture(tmp_path / "missing_ep_script", "wuwaterm-telegram")
    site, pkgs = fx.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    report = report_for(base, {}, pkgs, [], "telegram")
    layers = [layer(base), layer({art.REPORT_PATH.lstrip("/"): report})]
    attempt = fx.build(layers, config_extra={"Entrypoint": ["wuwaterm-telegram"], "Cmd": ["--db", "/app/data/terms.db"]})
    wheels = fx.write_wheels(attempt)
    image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
    with pytest.raises(art.ArtifactError, match="image Entrypoint console script is not installed and verified: wuwaterm-telegram"):
        art.seal_bundle(
            fx.delivery, fx.attempt_id, product=fx.product, commit=TG_COMMIT,
            tree=fx.source.tree, iid=image.manifest_digest, build_script_sha256="c" * 64,
            base_images=[BASES[1]], build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": wheels[0]["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"},
        )


def test_console_launcher_missing_target_module_rejected(tmp_path: Path) -> None:
    # Console launcher whose target module does not exist in installed is rejected
    fx = Fixture(tmp_path / "missing_target_module")
    pkg = {k: v for k, v in fx.pkg.items() if k != "wuwaterm/cli.py"}
    src = fx.source_files()
    src.pop("src/wuwaterm/cli.py", None)
    src["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n'
        b'[project.scripts]\nwuwaterm = "wuwaterm.cli:main"\n'
    )
    info = "wuwaterm-0.7.0.dev1.dist-info/"
    launcher_data = uv_launcher("/app/.venv/bin/python", "wuwaterm.cli", "main")
    extra = {
        info + "entry_points.txt": b"[console_scripts]\nwuwaterm = wuwaterm.cli:main\n",
        "../../../bin/wuwaterm": launcher_data,
    }
    site = dist("wuwaterm", fx.version, {**pkg, **extra}, {"url": "file:///app", "dir_info": {"editable": False}})
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT, "app/.venv/bin/wuwaterm": launcher_data}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items() if not k.startswith("../")})
    modes = {"entrypoint.sh": 0o755, "app/.venv/bin/wuwaterm": 0o755}
    report = report_for(base, modes, [("wuwaterm", fx.version)], ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="console script target module is not installed: wuwaterm.cli"):
        fx.seal(layers, source_files=src)


def test_entrypoint_sh_invalid_shebang_rejected(tmp_path: Path) -> None:
    # /entrypoint.sh extra file with invalid shebang (not #!/bin/sh\n) is rejected
    fx = Fixture(tmp_path / "bad_shebang")
    site, pkgs = fx.installed()
    bad_entrypoint = b"#!/bin/bash\nexec python -m wuwaterm\n"
    src = fx.source_files()
    src["deploy/entrypoint.sh"] = bad_entrypoint
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": bad_entrypoint}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    with pytest.raises(art.ArtifactError, match="extra file has invalid shebang: /entrypoint.sh"):
        fx.seal(layers, source_files=src)


def test_entrypoint_missing_bin_sh_rejected(tmp_path: Path) -> None:
    # 1. Neither "bin" nor "bin/sh" nor "usr/bin/sh" exists in rootfs.nodes
    # (Codex reproduction in PRRT_kwDOS82bU860_loP)
    fx = Fixture(tmp_path / "missing_bin_sh")
    site, pkgs = fx.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [
        layer(base, modes=modes, omit_venv_files={"bin/sh"}),
        layer({art.REPORT_PATH.lstrip("/"): report}, omit_venv_files={"bin/sh"}),
    ]
    with pytest.raises(art.ArtifactError, match="entrypoint shell /bin/sh is missing"):
        fx.seal(layers)
    sealed = fx.delivery / "attempts" / fx.attempt_id / "state" / "sealed.json"
    assert not sealed.exists()
    bundle_file = fx.delivery / "attempts" / fx.attempt_id / "bundle" / art.BUNDLE_FILE
    with pytest.raises(art.ArtifactError, match="entrypoint shell /bin/sh is missing"):
        art.verify_bundle(bundle_file)

    # 2a. "bin/sh" exists as non-executable regular file (mode 0o644)
    fx_ne = Fixture(tmp_path / "nonexec_bin_sh")
    site_ne, pkgs_ne = fx_ne.installed()
    base_ne = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
               "bin/sh": b"\x7fELF" + bytes(12)}
    base_ne.update({SITE.lstrip("/") + "/" + k: v for k, v in site_ne.items()})
    modes_ne = {"entrypoint.sh": 0o755, "bin/sh": 0o644}
    report_ne = report_for(base_ne, modes_ne, pkgs_ne, ["/entrypoint.sh"], "core")
    layers_ne = [layer(base_ne, modes=modes_ne), layer({art.REPORT_PATH.lstrip("/"): report_ne})]
    with pytest.raises(art.ArtifactError, match="entrypoint shell /bin/sh is not executable: /bin/sh"):
        fx_ne.seal(layers_ne)

    # 2b. "bin/sh" exists as a dangling symlink (link="missing_dash")
    fx_sym = Fixture(tmp_path / "dangling_bin_sh")
    site_sym, pkgs_sym = fx_sym.installed()
    base_sym = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base_sym.update({SITE.lstrip("/") + "/" + k: v for k, v in site_sym.items()})
    modes_sym = {"entrypoint.sh": 0o755}
    report_sym = report_for(base_sym, modes_sym, pkgs_sym, ["/entrypoint.sh"], "core")
    extra_sym = [("bin", "dir", None, 0o755), ("bin/sh", "symlink", "missing_dash", 0o777)]
    layers_sym = [
        layer(base_sym, modes=modes_sym, omit_venv_files={"bin/sh"}, extra=extra_sym),
        layer({art.REPORT_PATH.lstrip("/"): report_sym}),
    ]
    with pytest.raises(art.ArtifactError, match="entrypoint shell /bin/sh is missing in image: /bin/missing_dash"):
        fx_sym.seal(layers_sym)

    # 3a. /app/build/lib/wuwaterm/__init__.py tampered vs source.tar
    fx_build = Fixture(tmp_path / "tampered_build_lib")
    site_b, pkgs_b = fx_build.installed()
    base_b = {
        "app/scripts/install_probe.py": PROBE_BYTES,
        "entrypoint.sh": ENTRYPOINT,
        "app/build/lib/wuwaterm/__init__.py": b"VERSION = 999\n",
    }
    base_b.update({SITE.lstrip("/") + "/" + k: v for k, v in site_b.items()})
    modes_b = {"entrypoint.sh": 0o755}
    report_b = report_for(base_b, modes_b, pkgs_b, ["/entrypoint.sh"], "core")
    layers_b = [layer(base_b, modes=modes_b), layer({art.REPORT_PATH.lstrip("/"): report_b})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app"):
        fx_build.seal(layers_b)

    # 3b. /app/scripts/build-delivery.sh tampered vs source.tar
    fx_sh = Fixture(tmp_path / "tampered_app_script")
    site_sh, pkgs_sh = fx_sh.installed()
    base_sh = {
        "app/scripts/install_probe.py": PROBE_BYTES,
        "entrypoint.sh": ENTRYPOINT,
        "app/scripts/build-delivery.sh": b"#!/bin/sh\necho malicious\n",
    }
    base_sh.update({SITE.lstrip("/") + "/" + k: v for k, v in site_sh.items()})
    modes_sh = {"entrypoint.sh": 0o755, "app/scripts/build-delivery.sh": 0o755}
    report_sh = report_for(base_sh, modes_sh, pkgs_sh, ["/entrypoint.sh"], "core")
    layers_sh = [layer(base_sh, modes=modes_sh), layer({art.REPORT_PATH.lstrip("/"): report_sh})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app"):
        fx_sh.seal(layers_sh)


def test_wuwaterm_console_scripts_and_cli_audit(tmp_path: Path) -> None:
    # 1. Unexpected console script entry point for wuwaterm
    fx_ep = Fixture(tmp_path / "bad_ep")
    src_ep = fx_ep.source_files()
    src_ep["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n'
        b'[project.scripts]\nwuwaterm = "wuwaterm.custom:main"\n'
    )
    info = "wuwaterm-0.7.0.dev1.dist-info/"
    extra_ep = {info + "entry_points.txt": b"[console_scripts]\nwuwaterm = wuwaterm.custom:main\n"}
    site_ep = dist("wuwaterm", fx_ep.version, {**fx_ep.pkg, **extra_ep}, {"url": "file:///app", "dir_info": {"editable": False}})
    base_ep = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base_ep.update({SITE.lstrip("/") + "/" + k: v for k, v in site_ep.items()})
    modes_ep = {"entrypoint.sh": 0o755}
    report_ep = report_for(base_ep, modes_ep, [("wuwaterm", fx_ep.version)], ["/entrypoint.sh"], "core")
    layers_ep = [layer(base_ep, modes=modes_ep), layer({art.REPORT_PATH.lstrip("/"): report_ep})]
    with pytest.raises(art.ArtifactError, match="unexpected console script entry point for wuwaterm"):
        fx_ep.seal(layers_ep, source_files=src_ep)

    # 2. Missing required cli module for entrypoint.sh (wuwaterm/cli.py in source but not installed)
    fx_cli = Fixture(tmp_path / "missing_cli")
    pkg_no_cli = {k: v for k, v in fx_cli.pkg.items() if k != "wuwaterm/cli.py"}
    site_cli = dist("wuwaterm", fx_cli.version, pkg_no_cli, {"url": "file:///app", "dir_info": {"editable": False}})
    base_cli = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT}
    base_cli.update({SITE.lstrip("/") + "/" + k: v for k, v in site_cli.items()})
    modes_cli = {"entrypoint.sh": 0o755}
    report_cli = report_for(base_cli, modes_cli, [("wuwaterm", fx_cli.version)], ["/entrypoint.sh"], "core")
    layers_cli = [layer(base_cli, modes=modes_cli), layer({art.REPORT_PATH.lstrip("/"): report_cli})]
    with pytest.raises(art.ArtifactError, match="missing required cli module for entrypoint.sh"):
        fx_cli.seal(layers_cli)


def test_generated_egg_info_metadata_is_inert_but_not_importable(tmp_path: Path) -> None:
    # Installing the distribution inside the image makes the setuptools backend
    # write src/<name>.egg-info metadata into /app/src, where the sealed git
    # archive has nothing. That is tolerated only for inert non-executable
    # metadata under app/src/*.egg-info: everything else under app/scripts,
    # app/src and app/build stays pinned to the sealed source.
    fx = Fixture(tmp_path / "egg_info")
    site, pkgs = fx.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
            "app/src/wuwaterm.egg-info/PKG-INFO": b"Metadata-Version: 2.1\nName: wuwaterm\n",
            "app/src/wuwaterm.egg-info/top_level.txt": b"wuwaterm\n"}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    assert fx.seal(layers).bundle["product"] == "wuwaterm-api"

    # 1. Importable file hidden in the generated metadata directory
    fx_py = Fixture(tmp_path / "egg_info_py")
    site_py, pkgs_py = fx_py.installed()
    base_py = dict(base, **{"app/src/wuwaterm.egg-info/evil.py": b"# evil\n"})
    base_py.update({SITE.lstrip("/") + "/" + k: v for k, v in site_py.items()})
    report_py = report_for(base_py, modes, pkgs_py, ["/entrypoint.sh"], "core")
    layers_py = [layer(base_py, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report_py})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app"):
        fx_py.seal(layers_py)

    # 2. Executable file hidden in the generated metadata directory
    fx_exec = Fixture(tmp_path / "egg_info_exec")
    site_exec, pkgs_exec = fx_exec.installed()
    base_exec = dict(base, **{"app/src/wuwaterm.egg-info/run.sh": b"#!/bin/sh\ntrue\n"})
    base_exec.update({SITE.lstrip("/") + "/" + k: v for k, v in site_exec.items()})
    modes_exec = {"entrypoint.sh": 0o755, "app/src/wuwaterm.egg-info/run.sh": 0o755}
    report_exec = report_for(base_exec, modes_exec, pkgs_exec, ["/entrypoint.sh"], "core")
    layers_exec = [layer(base_exec, modes=modes_exec), layer({art.REPORT_PATH.lstrip("/"): report_exec})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app"):
        fx_exec.seal(layers_exec)

    # 3. Regular file in /app/build outside the build/lib copy of the source
    fx_bdist = Fixture(tmp_path / "build_outside_lib")
    site_bdist, pkgs_bdist = fx_bdist.installed()
    base_bdist = dict(base, **{"app/build/bdist.linux-x86_64/evil.txt": b"payload\n"})
    base_bdist.update({SITE.lstrip("/") + "/" + k: v for k, v in site_bdist.items()})
    report_bdist = report_for(base_bdist, modes, pkgs_bdist, ["/entrypoint.sh"], "core")
    layers_bdist = [layer(base_bdist, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report_bdist})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app/build"):
        fx_bdist.seal(layers_bdist)

    # 4. /app/build/lib file that the sealed source does not carry
    fx_extra = Fixture(tmp_path / "build_lib_extra")
    site_extra, pkgs_extra = fx_extra.installed()
    base_extra = dict(base, **{"app/build/lib/wuwaterm/evil.py": b"# evil\n"})
    base_extra.update({SITE.lstrip("/") + "/" + k: v for k, v in site_extra.items()})
    report_extra = report_for(base_extra, modes, pkgs_extra, ["/entrypoint.sh"], "core")
    layers_extra = [layer(base_extra, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report_extra})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app/build"):
        fx_extra.seal(layers_extra)


def test_build_lib_copy_must_match_sealed_source(tmp_path: Path) -> None:
    # setuptools leaves app/build/lib/<top> as a copy of the sealed source; only
    # an exact copy is accepted, anything else is an unverified file in /app/build
    fx = Fixture(tmp_path / "build_lib_copy")
    site, pkgs = fx.installed()
    base = {"app/scripts/install_probe.py": PROBE_BYTES, "entrypoint.sh": ENTRYPOINT,
            "app/build/lib/wuwaterm/core.py": b"def f():\n    return 1\n",
            "app/build/lib/wuwaterm/cli.py": b"def main():\n    return 0\n"}
    base.update({SITE.lstrip("/") + "/" + k: v for k, v in site.items()})
    modes = {"entrypoint.sh": 0o755}
    report = report_for(base, modes, pkgs, ["/entrypoint.sh"], "core")
    layers = [layer(base, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report})]
    assert fx.seal(layers).bundle["product"] == "wuwaterm-api"

    fx_bad = Fixture(tmp_path / "build_lib_copy_bad")
    site_bad, pkgs_bad = fx_bad.installed()
    base_bad = dict(base, **{"app/build/lib/wuwaterm/core.py": b"def f():\n    return 2\n"})
    base_bad.update({SITE.lstrip("/") + "/" + k: v for k, v in site_bad.items()})
    report_bad = report_for(base_bad, modes, pkgs_bad, ["/entrypoint.sh"], "core")
    layers_bad = [layer(base_bad, modes=modes), layer({art.REPORT_PATH.lstrip("/"): report_bad})]
    with pytest.raises(art.ArtifactError, match="unverified or modified file in /app/build"):
        fx_bad.seal(layers_bad)
