from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from scripts import product_manifest as pm
from wuwaterm.db import SCHEMA


A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40
H = "1" * 64
J = "2" * 64
DEPLOYMENT_ID = "20261106T110000Z-0123456789abcdef0123456789abcdef"


def make_database(path: Path) -> dict:
    metadata = {
        "schema_version": "2", "source_profile": "arikatsu",
        "source_repo_url": "https://github.com/example/data.git", "source_commit": A,
        "source_game_version": "1.0", "source_resource_version": "1",
        "source_changelist": "1", "wutheringdata_commit": A,
    }
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items())
    database, _ = pm.read_database_snapshot(path)
    return database


def snapshot(service: str, image_id: str) -> dict:
    return {
        "schema_version": 1, "image_id": image_id,
        "project": "project", "service": service, "container_name": None,
        "command": ["api"] if service == "wuwaterm-api" else ["--db", "/app/data/terms.db"],
        "entrypoint": ["/app/entrypoint.sh"],
        "mounts": [{"host": "/srv/product/data", "container": "/app/data", "read_only": True}],
        "env_files": ["/srv/product/.env"], "network_mode": "host", "ports": [],
        "restart": {"name": "unless-stopped", "maximum_retry_count": 0},
        "user": None, "working_dir": "/app",
        "environment_overrides": [
            {"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/app/data/terms.db"},
            {"name": "TELEGRAM_BOT_TOKEN", "mode": "empty"},
        ],
        "runtime_options": {
            "read_only_root": False, "privileged": False, "init": None,
            "pid_mode": "", "ipc_mode": "private", "cap_add": [], "cap_drop": [],
            "security_opt": [], "dns": [], "extra_hosts": [],
        },
    }


def component(root: Path, name: str) -> dict:
    if name == "api":
        image_id, commit, tree = "sha256:" + H, A, B
        service, version = "wuwaterm-api", "0.7.0.dev1"
        source = {"revision_claim": commit, "verified_commit": commit, "verified_tree": tree}
        core = {"version": version, **source}
        provenance = {
            "kind": "source-image", "build_recipe_sha256": H,
            "build_receipt_sha256": J, "wheel_identity": "not-built-from-wheel",
        }
        repository = "My-Denia/WuwaTerm"
    else:
        image_id, commit = "sha256:" + J, C
        service, version = "wuwaterm-telegram", "0.1.0.dev1"
        source = {"revision_claim": commit, "verified_commit": None, "verified_tree": None}
        core = {"version": "0.7.0.dev1", "revision_claim": D, "verified_commit": None, "verified_tree": None}
        provenance = {
            "kind": "wheel-image", "telegram_wheel_sha256": J,
            "core_wheel_sha256": H, "core_lock_sha256": J,
            "core_producer_kind": "github-actions-artifact", "build_receipt_sha256": H,
        }
        repository = "My-Denia/WuwaTerm-Telegram"
    snap = snapshot(service, image_id)
    snapshot_path, digest = pm.write_recipe_snapshot(root, snap)
    recipe = {key: snap[key] for key in pm._RECIPE_FIELDS}
    recipe.update({"snapshot_path": snapshot_path, "sha256": digest})
    return {
        "repository": repository, "version": version, "source": source, "core": core,
        "image": {"ref": "runtime:test", "id": image_id, "digest": image_id, "revision": commit},
        "recipe": recipe, "provenance": provenance,
    }


@pytest.fixture
def payload(tmp_path: Path) -> dict:
    return {
        "schema_version": 2, "deployment_id": DEPLOYMENT_ID,
        "deployment_utc": "2026-11-06T11:00:00Z",
        "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": A,
                       "source_tree": B, "source_tool_sha256": H},
        "components": {"api": component(tmp_path, "api"), "telegram": component(tmp_path, "telegram")},
        "database": make_database(tmp_path / "terms.db"),
        "api_identity": {"path": "/srv/product/state-api/devices.db", "device": 1, "inode": 2},
    }


def test_immutable_manifest_records_independent_components_and_db(tmp_path: Path, payload: dict) -> None:
    path = pm.write_manifest(tmp_path, payload)
    assert path == tmp_path / ".deployments" / "products" / f"{DEPLOYMENT_ID}.json"
    assert path.stat().st_mode & 0o222 == 0
    assert pm.read_manifest(tmp_path, DEPLOYMENT_ID) == payload
    assert pm.manifest_sha256(tmp_path, DEPLOYMENT_ID) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["components"]["api"]["core"]["revision_claim"] != payload["components"]["telegram"]["core"]["revision_claim"]
    assert pm.verify_database_binding(payload["database"])["inode"] == (tmp_path / "terms.db").stat().st_ino
    with pytest.raises(pm.ManifestError, match="already exists"):
        pm.write_manifest(tmp_path, payload)
    assert pm.read_manifest(tmp_path, DEPLOYMENT_ID) == payload


def test_stopped_or_absent_component_representation_and_distinct_ids(tmp_path: Path, payload: dict) -> None:
    payload["components"]["telegram"] = None
    pm.write_manifest(tmp_path, payload)
    second = copy.deepcopy(payload)
    second["deployment_id"] = "20261106T110000Z-fedcba9876543210fedcba9876543210"
    pm.write_manifest(tmp_path, second)
    assert pm.read_manifest(tmp_path, DEPLOYMENT_ID)["components"]["telegram"] is None
    assert pm.read_manifest(tmp_path, second["deployment_id"])["operations"] == payload["operations"]
    assert pm.new_deployment_id() != pm.new_deployment_id()


def test_legacy_provenance_does_not_invent_wheel_evidence(payload: dict) -> None:
    legacy = payload["components"]["telegram"]
    legacy["core"]["revision_claim"] = legacy["source"]["revision_claim"]
    legacy["provenance"] = {"kind": "legacy-image", "legacy_manifest_sha256": H,
                            "wheel_identity": "unavailable", "lock_identity": "unavailable"}
    pm.validate_manifest(payload)
    legacy["provenance"]["wheel_identity"] = H
    with pytest.raises(pm.ManifestError, match="unavailable"):
        pm.validate_manifest(payload)


def test_wheel_provenance_requires_a_specific_registered_receipt(payload: dict) -> None:
    provenance = payload["components"]["telegram"]["provenance"]
    assert pm.validate_manifest(payload) is payload
    del provenance["build_receipt_sha256"]
    with pytest.raises(pm.ManifestError, match="unexpected or missing fields"):
        pm.validate_manifest(payload)
    provenance["build_receipt_sha256"] = "not-a-sha256"
    with pytest.raises(pm.ManifestError, match="invalid value"):
        pm.validate_manifest(payload)


def test_unverified_wheel_revision_can_be_absent_without_false_attestation(payload: dict) -> None:
    telegram = payload["components"]["telegram"]
    telegram["source"]["revision_claim"] = None
    telegram["image"]["revision"] = None
    pm.validate_manifest(payload)
    telegram["source"]["verified_commit"] = C
    with pytest.raises(pm.ManifestError, match="recorded together"):
        pm.validate_manifest(payload)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(schema_version=True),
    lambda p: p.update(schema_version=3),
    lambda p: p.update(deployment_id=A),
    lambda p: p.update(extra="not allowed"),
    lambda p: p["components"].update(api=None, telegram=None),
    lambda p: p["components"]["api"]["recipe"].update(environment={"TOKEN": "secret"}),
    lambda p: p["components"]["api"]["recipe"].update(snapshot_path="../../outside"),
    lambda p: p["components"]["api"]["image"].update(revision=C),
    lambda p: p["components"]["api"]["source"].update(verified_tree=None),
    lambda p: p["database"].update(schema_version=3),
    lambda p: p["database"]["provenance"].update(source_commit=C),
    lambda p: p["database"]["provenance"].update(source_repo_url="https://user:secret@example.com/x"),
    lambda p: p["api_identity"].update(sha256=H),
    lambda p: p["components"]["api"]["recipe"]["command"].append("--token=secret"),
    lambda p: p["components"]["api"]["recipe"]["command"].extend(["--api-key", "secret"]),
])
def test_bad_manifest_is_rejected_before_publication(tmp_path: Path, payload: dict, mutation) -> None:
    mutation(payload)
    with pytest.raises(pm.ManifestError):
        pm.write_manifest(tmp_path, payload)
    assert not (tmp_path / ".deployments" / "products" / f"{DEPLOYMENT_ID}.json").exists()


def test_recipe_snapshot_is_immutable_and_refuses_secret_values(tmp_path: Path) -> None:
    item = snapshot("wuwaterm-api", "sha256:" + H)
    path, digest = pm.write_recipe_snapshot(tmp_path, item)
    assert pm.read_recipe_snapshot(tmp_path, digest) == item
    assert pm.write_recipe_snapshot(tmp_path, item) == (path, digest)
    for override in (
        {"name": "TELEGRAM_BOT_TOKEN", "mode": "literal", "value": "secret"},
        {"name": "WUWATERM_API_PORT", "mode": "literal", "value": "secret"},
    ):
        bad = copy.deepcopy(item)
        bad["environment_overrides"] = [override]
        with pytest.raises(pm.ManifestError):
            pm.write_recipe_snapshot(tmp_path, bad)


@pytest.mark.parametrize("position", ["before-link", "after-link"])
def test_unnamed_publication_survives_process_death_without_named_partial(tmp_path: Path, position: str) -> None:
    code = r'''
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import product_manifest as manifest
directory = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
real_link = os.link
def paused_link(*args, **kwargs):
    if sys.argv[3] == 'before-link': os.kill(os.getpid(), signal.SIGSTOP)
    real_link(*args, **kwargs)
    if sys.argv[3] == 'after-link': os.kill(os.getpid(), signal.SIGSTOP)
os.link = paused_link
manifest._publish_unnamed(directory, 'receipt.json', b'fixture receipt\n', mode=0o400)
'''
    scripts = Path(pm.__file__).resolve().parent
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(scripts), str(tmp_path), position],
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stopped = False
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                if os.WIFSTOPPED(status):
                    stopped = True
                    break
                child.returncode = os.waitstatus_to_exitcode(status)
                raise AssertionError("publisher exited before the requested boundary")
            time.sleep(0.01)
        assert stopped
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
        entries = list(tmp_path.iterdir())
        if position == "before-link":
            assert entries == []
        else:
            assert entries == [tmp_path / "receipt.json"]
            receipt = entries[0]
            assert receipt.read_bytes() == b"fixture receipt\n"
            assert receipt.stat().st_nlink == 1 and receipt.stat().st_mode & 0o777 == 0o400
            fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                pm._write_file_unnamed(fd, "receipt.json", b"fixture receipt\n", reuse=True)
            finally:
                os.close(fd)
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)


def test_unnamed_publication_rejects_existing_or_untrusted_links(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        pm._publish_unnamed(fd, "attempt.json", b"attempt", mode=0o600)
        assert (tmp_path / "attempt.json").stat().st_nlink == 1
        assert (tmp_path / "attempt.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            pm._publish_unnamed(fd, "attempt.json", b"other", mode=0o600)
        pm._write_file_unnamed(fd, "receipt.json", b"receipt", reuse=False)
        with pytest.raises(pm.ManifestError, match="already exists"):
            pm._write_file_unnamed(fd, "receipt.json", b"different", reuse=True)
        os.link(tmp_path / "receipt.json", tmp_path / "foreign-link")
        with pytest.raises(pm.ManifestError, match="singly-linked"):
            pm._write_file_unnamed(fd, "receipt.json", b"receipt", reuse=True)
        assert (tmp_path / "receipt.json").read_bytes() == b"receipt"
        assert (tmp_path / "foreign-link").stat().st_nlink == 2
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["recipe", "manifest"])
@pytest.mark.parametrize("position", ["before-link", "after-link"])
def test_public_metadata_publication_has_no_named_partial_after_process_death(
    tmp_path: Path, payload: dict, kind: str, position: str,
) -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J) if kind == "recipe" else payload
    if kind == "recipe":
        item["environment_overrides"] = [{"name": "WUWATERM_STATE_DIR", "mode": "literal", "value": "state"}]
        raw = pm._encode(item)
        digest = hashlib.sha256(raw).hexdigest()
        directory, name = tmp_path / ".deployments/product-recipes", digest + ".json"
    else:
        directory, name = tmp_path / ".deployments/products", DEPLOYMENT_ID + ".json"
    previous = {entry.name for entry in directory.iterdir()} if directory.exists() else set()
    code = r'''
import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import product_manifest as m
root=pathlib.Path(sys.argv[2]); item=json.loads(sys.argv[3]); kind=sys.argv[4]; point=sys.argv[5]
original=os.link
def paused(*args,**kwargs):
 if point=='before-link': os.kill(os.getpid(),signal.SIGSTOP)
 original(*args,**kwargs)
 if point=='after-link': os.kill(os.getpid(),signal.SIGSTOP)
os.link=paused
m.write_recipe_snapshot(root,item) if kind=='recipe' else m.write_manifest(root,item)
'''
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(Path(pm.__file__).resolve().parent),
                              str(tmp_path), json.dumps(item), kind, position],
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                assert os.WIFSTOPPED(status), "metadata publisher exited before its link boundary"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("metadata publisher did not reach its link boundary")
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)
    assert {entry.name for entry in directory.iterdir()} == previous | ({name} if position == "after-link" else set())
    target = directory / name
    if position == "before-link":
        assert not target.exists()
        return
    info = target.stat()
    assert info.st_nlink == 1 and info.st_mode & 0o777 == 0o400
    before = {entry.name: (entry.stat().st_ino, entry.stat().st_mtime_ns, entry.stat().st_ctime_ns)
              for entry in directory.iterdir()}
    if kind == "recipe":
        assert pm.read_recipe_snapshot(tmp_path, digest) == item
        assert pm.write_recipe_snapshot(tmp_path, item)[1] == digest
    else:
        assert pm.read_manifest(tmp_path, DEPLOYMENT_ID) == item
        with pytest.raises(pm.ManifestError, match="already exists"):
            pm.write_manifest(tmp_path, item)
    assert {entry.name: (entry.stat().st_ino, entry.stat().st_mtime_ns, entry.stat().st_ctime_ns)
            for entry in directory.iterdir()} == before


def test_unnamed_publication_uses_attempt_and_receipt_size_bounds(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        large_attempt = b"x" * (pm._LIMIT + 1)
        pm._publish_unnamed(fd, "attempt.json", large_attempt, mode=0o600)
        assert (tmp_path / "attempt.json").stat().st_size == len(large_attempt)
        with pytest.raises(pm.ManifestError, match="invalid unnamed"):
            pm._publish_unnamed(fd, "receipt.json", large_attempt, mode=0o400)
        with pytest.raises(pm.ManifestError, match="invalid unnamed"):
            pm._publish_unnamed(fd, "too-large.json", b"x" * (16 * 1024 * 1024 + 1), mode=0o600)
        assert not (tmp_path / "receipt.json").exists() and not (tmp_path / "too-large.json").exists()
    finally:
        os.close(fd)


def test_immutable_publisher_refuses_filesystem_without_unnamed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original = os.open

    def unsupported(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, "synthetic unsupported filesystem")
        return original(path, flags, mode, dir_fd=dir_fd)

    try:
        monkeypatch.setattr(os, "open", unsupported)
        with pytest.raises(pm.ManifestError, match="cannot publish unnamed"):
            pm._write_file(fd, "claim.json", b"fixture", reuse=True)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(fd)


@pytest.mark.parametrize("value", ["/app/state", "state", "state/custom-settings.json"])
def test_recipe_path_environment_preserves_normalized_literals(tmp_path: Path, value: str) -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["environment_overrides"] = [{"name": "WUWATERM_SETTINGS_PATH", "mode": "literal", "value": value}]
    _, digest = pm.write_recipe_snapshot(tmp_path, item)
    assert pm.read_recipe_snapshot(tmp_path, digest)["environment_overrides"][0]["value"] == value


@pytest.mark.parametrize("value", [
    "", "./state", "state/../file", "state//file", "state/", "~user/state", "state\\file",
    " state", "state ", "state\nfile", "/app//state", "/app/../state",
])
def test_recipe_path_environment_rejects_non_normalized_literals(value: str) -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["environment_overrides"] = [{"name": "WUWATERM_SETTINGS_PATH", "mode": "literal", "value": value}]
    with pytest.raises(pm.ManifestError):
        pm.validate_recipe_snapshot(item)


def test_relative_environment_path_does_not_relax_host_mount_paths() -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["environment_overrides"] = [{"name": "WUWATERM_STATE_DIR", "mode": "literal", "value": "state"}]
    item["mounts"][0]["host"] = "state"
    with pytest.raises(pm.ManifestError, match="absolute path"):
        pm.validate_recipe_snapshot(item)


def test_recipe_preserves_null_and_empty_command_separately(tmp_path: Path) -> None:
    item = snapshot("wuwaterm-api", "sha256:" + H)
    item["command"] = None
    item["entrypoint"] = []
    _, digest = pm.write_recipe_snapshot(tmp_path, item)
    stored = pm.read_recipe_snapshot(tmp_path, digest)
    assert stored["command"] is None
    assert stored["entrypoint"] == []


def test_poll_health_tmpfs_recipe_and_nonroot_owner_are_preserved(tmp_path: Path) -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["user"] = "1000:1000"
    item["mounts"].append({
        "type": "tmpfs", "container": "/run/wuwaterm-telegram",
        "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700", "uid=1000", "gid=1000", "size=4194304"],
    })
    item["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})
    path, digest = pm.write_recipe_snapshot(tmp_path, item)
    restored = pm.read_recipe_snapshot(tmp_path, digest)
    assert restored == item
    assert path.endswith(digest + ".json")
    pm.verify_poll_health_mount(restored, effective_uid=1000)
    with pytest.raises(pm.ManifestError, match="differs"):
        pm.verify_poll_health_mount(restored, effective_uid=1001)


@pytest.mark.parametrize("options", [
    ["rw", "ro"], ["mode=0700", "mode=0755"], ["uid=1", "uid=2"],
    ["size=0"], ["size=9223372036854775808"], ["mode=8888"],
    ["uid=4294967295"], ["gid=-1"], ["unknown"],
])
def test_tmpfs_refuses_ambiguous_or_unsafe_options(options: list[str]) -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["mounts"].append({"type": "tmpfs", "container": "/run/wuwaterm-telegram", "options": options})
    with pytest.raises(pm.ManifestError):
        pm.validate_recipe_snapshot(item)


def test_tmpfs_cannot_duplicate_bind_destination_or_hide_nonroot_owner() -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["mounts"].append({"type": "tmpfs", "container": "/app/data", "options": ["mode=0700"]})
    with pytest.raises(pm.ManifestError, match="duplicate"):
        pm.validate_recipe_snapshot(item)
    item["mounts"][-1]["container"] = "/run/wuwaterm-telegram"
    item["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})
    with pytest.raises(pm.ManifestError, match="not explicit"):
        pm.verify_poll_health_mount(item, effective_uid=1000)
    item["mounts"][-1]["options"] += ["uid=1000", "ro"]
    with pytest.raises(pm.ManifestError, match="read-only"):
        pm.verify_poll_health_mount(item, effective_uid=1000)


def test_poll_health_flag_refuses_nonboolean_and_secret_literals() -> None:
    item = snapshot("wuwaterm-telegram", "sha256:" + J)
    item["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "secret"})
    with pytest.raises(pm.ManifestError, match="health flag"):
        pm.validate_recipe_snapshot(item)


def test_manifest_refuses_missing_different_and_tampered_snapshots(tmp_path: Path, payload: dict) -> None:
    recipe = payload["components"]["api"]["recipe"]
    recipe["sha256"] = "9" * 64
    recipe["snapshot_path"] = ".deployments/product-recipes/" + "9" * 64 + ".json"
    with pytest.raises(pm.ManifestError):
        pm.write_manifest(tmp_path, payload)
    recipe = payload["components"]["telegram"]["recipe"]
    filename = tmp_path / recipe["snapshot_path"]
    os.chmod(filename, 0o600)
    filename.write_text("{}\n")
    os.chmod(filename, 0o400)
    with pytest.raises(pm.ManifestError):
        pm.read_recipe_snapshot(tmp_path, recipe["sha256"])


def test_recipe_cannot_disagree_with_snapshot(tmp_path: Path, payload: dict) -> None:
    payload["components"]["api"]["recipe"]["restart"]["name"] = "always"
    with pytest.raises(pm.ManifestError, match="does not match"):
        pm.write_manifest(tmp_path, payload)


@pytest.mark.parametrize("target", ["parent", "leaf"])
def test_symlinked_manifest_paths_are_rejected(tmp_path: Path, payload: dict, target: str) -> None:
    if target == "parent":
        other = tmp_path / "other"
        other.mkdir()
        (tmp_path / ".deployments" / "products").symlink_to(other, target_is_directory=True)
    else:
        products = tmp_path / ".deployments" / "products"
        products.mkdir()
        (products / f"{DEPLOYMENT_ID}.json").symlink_to(tmp_path / "terms.db")
    with pytest.raises(pm.ManifestError):
        pm.write_manifest(tmp_path, payload)
    with pytest.raises(pm.ManifestError):
        pm.read_manifest(tmp_path, DEPLOYMENT_ID)


def test_symlinked_root_ancestor_is_rejected(tmp_path: Path, payload: dict) -> None:
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(pm.ManifestError):
        pm.write_manifest(alias, payload)


def test_nonregular_manifest_is_rejected_without_blocking(tmp_path: Path, payload: dict) -> None:
    products = tmp_path / ".deployments" / "products"
    products.mkdir()
    os.mkfifo(products / f"{DEPLOYMENT_ID}.json")
    with pytest.raises(pm.ManifestError, match="regular file"):
        pm.read_manifest(tmp_path, DEPLOYMENT_ID)


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected_on_read(tmp_path: Path, payload: dict) -> None:
    path = pm.write_manifest(tmp_path, payload)
    raw = path.read_text()
    for replacement in ('"schema_version": 2, "schema_version": 2,', '"schema_version": NaN,'):
        os.chmod(path, 0o600)
        path.write_text(raw.replace('"schema_version": 2,', replacement, 1))
        os.chmod(path, 0o400)
        with pytest.raises(pm.ManifestError):
            pm.read_manifest(tmp_path, DEPLOYMENT_ID)


def test_database_actual_provenance_schema_and_sidecars_are_verified(tmp_path: Path, payload: dict) -> None:
    path = tmp_path / "terms.db"
    before = pm.verify_database_binding(payload["database"])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metadata SET value = 'wrong' WHERE key = 'source_profile'")
    with pytest.raises(pm.ManifestError, match="differs"):
        pm.verify_database_binding(payload["database"])
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_terms_en_norm")
    with pytest.raises(pm.ManifestError, match="schema 2"):
        pm.read_database_snapshot(path)
    assert path.stat().st_ino == before["inode"]

    second = tmp_path / "second.db"
    make_database(second)
    (tmp_path / "second.db-wal").symlink_to(tmp_path / "missing")
    with pytest.raises(pm.ManifestError, match="sidecar"):
        pm.read_database_snapshot(second)


# ---------------------------------------------------------------- schema 3

def _acquired_api(tmp_path: Path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import test_component_artifact as artifact_tests  # noqa: PLC0415

    fx = artifact_tests.Fixture(tmp_path / "artifact")
    verified = fx.seal()
    result = artifact_tests.art.acquire(verified.path, fx.root, docker=artifact_tests.FakeDocker())
    receipt = result["receipt"]
    path = fx.root / ".deployments/artifacts/acquisitions" / (receipt["acquisition_id"] + ".json")
    return fx, verified, receipt, hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Acquisition refuses an inherited FD9; park a runner-inherited one.
    try:
        saved = os.dup(9)
    except OSError:
        saved = None
    else:
        os.close(9)
    try:
        fx, verified, receipt, acquisition_sha = _acquired_api(tmp_path)
    finally:
        if saved is not None:
            os.dup2(saved, 9)
            os.close(saved)
    root = fx.root
    bundle = verified.bundle
    native = receipt["native_image_id"]
    api = component(root, "api")
    source = {"revision_claim": bundle["source"]["commit"], "verified_commit": bundle["source"]["commit"],
              "verified_tree": bundle["source"]["tree"]}
    snap = snapshot("wuwaterm-api", native)
    snapshot_path, digest = pm.write_recipe_snapshot(root, snap)
    recipe = {key: snap[key] for key in pm._RECIPE_FIELDS}
    recipe.update({"snapshot_path": snapshot_path, "sha256": digest})
    api.update(version=bundle["version"], source=source, core={"version": bundle["version"], **source},
               image={"ref": native, "id": native, "digest": native, "revision": bundle["source"]["commit"]},
               recipe=recipe,
               provenance={"kind": "completed-artifact", "bundle_sha256": verified.sha256,
                           "acquisition_sha256": acquisition_sha, "authority": "local-source",
                           "native_image_id": native})
    payload = {
        "schema_version": 3, "deployment_id": DEPLOYMENT_ID, "deployment_utc": "2026-11-06T11:00:00Z",
        "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": A,
                       "source_tree": B, "source_tool_sha256": H},
        "components": {"api": api, "telegram": None},
        "database": make_database(tmp_path / "terms.db"), "api_identity": None,
    }
    return root, payload


def test_schema3_completed_artifact_is_bound_to_registered_records(tmp_path: Path, monkeypatch) -> None:
    root, payload = artifact_payload(tmp_path, monkeypatch)
    pm.write_manifest(root, payload)
    assert pm.read_manifest(root, DEPLOYMENT_ID) == payload
    assert pm.SCHEMA_VERSION == pm.MANIFEST_SCHEMA_VERSION == 3 and pm.PREVIOUS_SCHEMA_VERSION == 2
    # The registered receipt is required at every read, not only at write.
    receipts = root / ".deployments/artifacts/acquisitions"
    for item in receipts.iterdir():
        item.rename(item.with_suffix(".moved"))
    with pytest.raises(pm.ManifestError, match="receipt is missing"):
        pm.read_manifest(root, DEPLOYMENT_ID)


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p["components"]["api"]["provenance"].update(native_image_id="sha256:" + H), "native image"),
    (lambda p: p["components"]["api"]["provenance"].update(authority="official-ci"), "registered bundle"),
    (lambda p: p["components"]["api"].update(version="9.9.9"), "differ"),
    (lambda p: p["components"]["api"]["provenance"].update(acquisition_sha256=J), "receipt is missing"),
    (lambda p: p["components"]["api"]["provenance"].update(authority="github"), "authority"),
    (lambda p: p["components"]["api"].update(repository="My-Denia/WuwaTerm-Telegram"), "repository"),
    (lambda p: p.update(schema_version=2), "unsupported provenance"),
])
def test_schema3_artifact_binding_refusals(tmp_path: Path, monkeypatch, mutate, message: str) -> None:
    root, payload = artifact_payload(tmp_path, monkeypatch)
    mutate(payload)
    with pytest.raises(pm.ManifestError, match=message):
        pm.write_manifest(root, payload)
    assert not (root / ".deployments/products").exists()


def test_schema3_refuses_producer_provenance_and_schema2_stays_readable(tmp_path: Path, payload: dict) -> None:
    assert pm.validate_manifest_v2(copy.deepcopy(payload)) == payload
    for name in ("api", "telegram"):
        upgraded = copy.deepcopy(payload)
        upgraded["schema_version"] = 3
        upgraded["components"]["telegram" if name == "api" else "api"] = None
        with pytest.raises(pm.ManifestError, match="unsupported provenance"):
            pm.validate_manifest(upgraded)
    legacy = copy.deepcopy(payload)
    legacy["schema_version"] = 3
    legacy["components"]["api"] = None
    legacy["api_identity"] = None
    legacy["components"]["telegram"]["provenance"] = {
        "kind": "legacy-image", "legacy_manifest_sha256": H, "wheel_identity": "unavailable",
        "lock_identity": "unavailable"}
    legacy["components"]["telegram"]["source"] = {"revision_claim": C, "verified_commit": None, "verified_tree": None}
    legacy["components"]["telegram"]["core"]["revision_claim"] = C
    assert pm.validate_manifest(legacy)["schema_version"] == 3
    with pytest.raises(pm.ManifestError, match="expected schema version 2"):
        pm.validate_manifest_v2(legacy)
