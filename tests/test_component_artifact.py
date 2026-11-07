"""Completed-component bundle and acquisition: real formats, real records.

The OCI archives, layer tars, git-archive tars, wheels and probe reports here
are byte-level fixtures in the formats Docker and git actually produce. Only
the Docker daemon boundary (info / load / inspect) is replaced; every decision
function, lock and tombstone path runs for real.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import component_artifact as art  # noqa: E402
import install_probe  # noqa: E402

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


def layer(files: dict[str, bytes], *, extra: list[tuple] = (), modes: dict[str, int] | None = None) -> list[tuple]:
    entries, seen = [], set()
    for path in sorted(files):
        for item in dirs_for(path):
            if item[0] not in seen:
                seen.add(item[0])
                entries.append(item)
        entries.append((path, "file", files[path], (modes or {}).get(path, 0o644)))
    return entries + list(extra)


def oci_archive(layers: list[list[tuple]], *, repo_tags=None, annotations=None, config_extra=None,
                corrupt_layer: int | None = None, extra_entry: bool = False) -> bytes:
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
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                      "digest": "sha256:" + sha(config), "size": len(config)},
                           "layers": descriptors}).encode()
    blobs[sha(manifest)] = manifest
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": "sha256:" + sha(manifest), "size": len(manifest),
                            "annotations": dict({"org.opencontainers.image.created": "2026-11-07T00:00:00Z"},
                                                **(annotations or {}))}]}
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
               extras: list[str], product: str) -> bytes:
    """Build the probe report exactly as install_probe.py does, from file bytes."""
    items = []
    for name, version in sorted(packages):
        info = name.replace("-", "_") + "-" + version + ".dist-info/"
        record = fs[SITE.lstrip("/") + "/" + info + "RECORD"].decode()
        paths = [line.split(",")[0] for line in record.splitlines() if line]
        files = []
        for path in sorted(set(paths)):
            data = fs[SITE.lstrip("/") + "/" + path]
            files.append({"path": path, "sha256": sha(data), "size": len(data)})
        raw_url = fs.get(SITE.lstrip("/") + "/" + info + "direct_url.json")
        items.append({"name": name, "version": version, "location": SITE,
                      "direct_url": json.loads(raw_url) if raw_url else None,
                      "record_entries": len(paths), "files": files})
    report = {"schema_version": 1, "kind": "install-probe", "product": product,
              "interpreter": "/app/.venv/bin/python", "python_version": "3.11.16", "site_packages": SITE,
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
        self.pkg = {"wuwaterm/__init__.py": b"VERSION = 1\n", "wuwaterm/core.py": b"def f():\n    return 1\n"}
        self.tg_pkg = {"wuwaterm_telegram/__init__.py": b"T = 1\n"}

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

    def seal(self, layers=None, **options) -> art.VerifiedBundle:
        attempt = self.build(layers, **options)
        image = art.read_oci_archive(attempt / "bundle" / art.ARCHIVE_FILE)
        if self.product == "wuwaterm-api":
            return art.seal_bundle(self.delivery, self.attempt_id, product=self.product, commit=COMMIT,
                                   tree=self.source.tree, iid=image.manifest_digest,
                                   build_script_sha256="c" * 64, base_images=BASES,
                                   build_args=[{"name": "SOURCE_COMMIT", "value": COMMIT}], compatibility=COMPAT)
        wheels = self.write_wheels(attempt)
        core = next(w for w in wheels if w["role"] == "core")
        return art.seal_bundle(
            self.delivery, self.attempt_id, product=self.product, commit=TG_COMMIT, tree=self.source.tree,
            iid=image.manifest_digest, build_script_sha256="c" * 64, base_images=[BASES[1]],
            build_args=[{"name": "WUWATERM_SOURCE_REVISION", "value": TG_COMMIT}],
            compatibility=dict(COMPAT, telegram_state_protocol=1), wheels=wheels,
            core_binding={"core_version": "0.7.0", "core_wheel_sha256": core["sha256"],
                          "core_source_commit": COMMIT, "compatibility": "exact"})

    def write_wheels(self, attempt: Path) -> list[dict]:
        folder = attempt / "inputs" / "wheels"
        folder.mkdir()
        result = []
        for name, version, files, role in (("wuwaterm", "0.7.0", self.pkg, "core"),
                                           ("wuwaterm-telegram", "0.1.0", self.tg_pkg, "telegram")):
            info = name.replace("-", "_") + "-" + version + ".dist-info/"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as wheel:
                for path, data in sorted(files.items()):
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


@pytest.fixture(autouse=True)
def no_inherited_fd9():
    """Mutating entry points refuse FD9; park a runner-inherited FD9 for each test."""
    try:
        saved = os.dup(9)
    except OSError:
        yield
        return
    os.close(9)
    try:
        yield
    finally:
        os.dup2(saved, 9)
        os.close(saved)


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
    source_files["src/wuwaterm_api/cli.py"] = b"def main(): pass\n"
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
    source_files["src/wuwaterm_api/cli.py"] = b"def main(): pass\n"
    verified = fx.seal(source_files=source_files)
    assert verified.bundle["product"] == "wuwaterm-api"

    fx2 = Fixture(tmp_path / "both")
    source_files2 = fx2.source_files()
    source_files2["pyproject.toml"] = (
        b'[project]\nname = "wuwaterm"\nversion = "0.7.0.dev1"\n\n'
        b'[tool.hatch.build.targets.wheel]\npackages = ["src/wuwaterm", "src/wuwaterm_api"]\n'
    )
    source_files2["src/wuwaterm_api/__init__.py"] = b"API = 1\n"
    source_files2["src/wuwaterm_api/cli.py"] = b"def main(): pass\n"
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
        for call in (lambda: art.acquire(verified.path, fx.root, docker=FakeDocker()),
                     lambda: art.cancel(fx.root, fx.attempt_id),
                     lambda: art.recover(fx.root, fx.attempt_id, docker=FakeDocker())):
            with pytest.raises(art.ArtifactError, match="descriptor 9"):
                call()
        assert art.verify_bundle(verified.path).sha256 == verified.sha256  # read-only stays usable
    finally:
        os.close(9)
    assert not (fx.root / ".deployments" / "artifacts" / "attempts").exists()


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
