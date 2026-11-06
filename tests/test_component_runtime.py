from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import component_runtime as runtime
from test_product_manifest import A, B, H, J, DEPLOYMENT_ID, component, make_database


def container(record: dict, *, name: str, image_digit: str) -> dict:
    return {
        "id": image_digit * 64, "image_id": record["image"]["id"], "running": True,
        "started_at": "2026-11-06T11:00:00.123456789Z", "restart_count": 0,
        "project": record["recipe"]["project"], "service": record["recipe"]["service"],
        "name": name, "transaction_label": None,
    }


@pytest.fixture
def journal(tmp_path: Path) -> dict:
    source_id = runtime.source_tool_identity(A, B, H)
    components = {"api": component(tmp_path, "api"), "telegram": component(tmp_path, "telegram")}
    db = make_database(tmp_path / "terms.db")
    target_manifest = {
        "schema_version": 2, "deployment_id": DEPLOYMENT_ID,
        "deployment_utc": "2026-11-06T11:00:00Z",
        "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": A,
                       "source_tree": B, "source_tool_sha256": source_id},
        "components": components, "database": db,
        "api_identity": {"path": "/srv/product/state-api/devices.db", "device": 1, "inode": 2},
    }
    db_stat = runtime.manifest.verify_database_binding(db)
    before_containers = {
        "api": container(components["api"], name="wuwaterm-api", image_digit="a"),
        "telegram": container(components["telegram"], name="wuwaterm-telegram", image_digit="b"),
    }
    target_containers = copy.deepcopy(before_containers)
    target_containers["api"].update({
        "id": None, "name": None, "started_at": None, "restart_count": None,
        "transaction_label": DEPLOYMENT_ID,
    })
    bootstrap_files = {name: H for name in runtime.BOOTSTRAP_FILES}
    raw_manifest = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    return {
        "schema_version": 2, "kind": "component-runtime", "transaction_id": DEPLOYMENT_ID,
        "action": "update", "status": "active", "phase": "prepared",
        "source": {
            "commit": A, "tree": B, "source_tool_sha256": source_id, "tool_bytes_sha256": H,
            "capsule": {"path": ".deployments/component-source-" + source_id,
                        "manifest_sha256": H, "archive_sha256": H, "bundle_sha256": J},
            "bootstrap": {"path": ".deployments/component-bootstrap-" + source_id,
                          "files_sha256": bootstrap_files,
                          "sha256": runtime.bootstrap_identity(bootstrap_files)},
        },
        "selection": ["api"],
        "before": {"manifest": None, "topology": copy.deepcopy(components),
                   "containers": before_containers},
        "target": {"manifest": target_manifest, "containers": target_containers},
        "database": {"binding": db, "stat": db_stat},
        "api_identity": copy.deepcopy(target_manifest["api_identity"]), "migrations": [],
        "pointers": {
            "old": {".deploy_commit": {"present": True, "bytes": A + "\n"},
                    ".deploy_current": {"present": False, "bytes": None}},
            "target": {".deploy_commit": {"present": True, "bytes": A + "\n"},
                       ".deploy_current": {"present": True, "bytes": DEPLOYMENT_ID + "\n"}},
        },
        "manifest": {"deployment_id": DEPLOYMENT_ID,
                     "path": ".deployments/products/" + DEPLOYMENT_ID + ".json",
                     "sha256": hashlib.sha256(raw_manifest).hexdigest()},
    }


def test_complete_legacy_preimage_parses_without_current_pointer(journal: dict) -> None:
    assert runtime.validate_journal(journal) is journal
    assert journal["before"]["manifest"] is None
    assert journal["before"]["topology"]["api"] is not None
    assert journal["before"]["topology"]["telegram"] is not None
    assert journal["pointers"]["old"][".deploy_current"] == {"present": False, "bytes": None}


def test_inspection_reports_unadopted_legacy_as_unknown_without_writes(tmp_path: Path) -> None:
    (tmp_path / ".deploy_commit").write_text(A + "\n", encoding="ascii")
    before = {entry.name for entry in tmp_path.iterdir()}
    observed = runtime.inspect_deployment(tmp_path)
    assert observed["binding"] == "unknown"
    assert observed["source_commit"] == A
    assert observed["live_binding"] == "unverified"
    assert {entry.name for entry in tmp_path.iterdir()} == before


def test_capsule_identity_changes_when_commit_or_tree_changes() -> None:
    a = runtime.source_tool_identity(A, B, H)
    assert a != runtime.source_tool_identity(B, B, H)
    assert a != runtime.source_tool_identity(A, A, H)
    assert a != runtime.source_tool_identity(A, B, J)


@pytest.mark.parametrize("mutation", [
    lambda j: j.update(schema_version=True),
    lambda j: j.update(schema_version=3),
    lambda j: j.update(extra="unknown"),
    lambda j: j.update(phase={}),
    lambda j: j.update(status="committed"),
    lambda j: j["source"].update(tree="f" * 40),
    lambda j: j["source"]["capsule"].update(path="../outside"),
    lambda j: j["source"]["capsule"].update(manifest_sha256="x" * 64),
    lambda j: j["source"]["bootstrap"]["files_sha256"].update(extra=H),
    lambda j: j["selection"].append("api"),
    lambda j: j["before"].update(topology={"api": None, "telegram": None}),
    lambda j: j["before"]["containers"]["telegram"].update(started_at="not-time"),
    lambda j: j["target"]["containers"]["telegram"].update(id="c" * 64),
    lambda j: j["target"]["containers"]["api"].update(running=False),
    lambda j: j["target"]["containers"]["api"].update(transaction_label=None),
    lambda j: j["database"]["stat"].update(inode=True),
    lambda j: j["pointers"]["old"][".deploy_current"].update(present=False, bytes="wrong"),
    lambda j: j["pointers"]["target"][".deploy_commit"].update(bytes=B + "\n"),
    lambda j: j["manifest"].update(sha256=H),
    lambda j: j["manifest"].update(path="../outside"),
])
def test_inconsistent_journal_is_refused(journal: dict, mutation) -> None:
    mutation(journal)
    with pytest.raises(runtime.JournalError):
        runtime.validate_journal(journal)


def test_recorded_prior_manifest_requires_exact_old_pointer(journal: dict) -> None:
    previous = copy.deepcopy(journal["target"]["manifest"])
    previous["deployment_id"] = "20261106T100000Z-0123456789abcdef0123456789abcdef"
    journal["before"]["manifest"] = previous
    journal["pointers"]["old"][".deploy_current"] = {
        "present": True, "bytes": previous["deployment_id"] + "\n",
    }
    runtime.validate_journal(journal)
    journal["pointers"]["old"][".deploy_current"]["bytes"] = DEPLOYMENT_ID + "\n"
    with pytest.raises(runtime.JournalError, match="prior manifest"):
        runtime.validate_journal(journal)


def test_migration_requires_recorded_preimages(journal: dict) -> None:
    journal["selection"] = ["telegram"]
    api = journal["before"]["containers"]["api"]
    journal["target"]["containers"]["api"] = copy.deepcopy(api)
    telegram = journal["target"]["containers"]["telegram"]
    telegram.update(id=None, name=None, started_at=None, restart_count=None, transaction_label=DEPLOYMENT_ID)
    info = {"device": 1, "inode": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5}
    journal["migrations"] = [{
        "kind": "chat-settings", "action": "copy-if-absent", "effective_path": "/app/state/custom.json",
        "source_path": "/srv/product/data/old.json", "target_path": "/srv/product/state/custom.json",
        "source_before": {"present": True, "sha256": H, "stat": info},
        "target_before": {"present": False, "sha256": None, "stat": None}, "created": None,
    }]
    runtime.validate_journal(journal)
    journal["migrations"][0]["target_before"] = {"present": True, "sha256": H, "stat": info}
    with pytest.raises(runtime.JournalError, match="copy preimage"):
        runtime.validate_journal(journal)


def test_isolated_historical_parser_and_fail_closed_commands(tmp_path: Path, journal: dict) -> None:
    path = tmp_path / "transaction.json"
    path.write_text(json.dumps(journal), encoding="utf-8")
    tool = Path(runtime.__file__)
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8"}
    valid = subprocess.run([sys.executable, "-I", "-B", str(tool), "validate-journal", "--journal", str(path)],
                           capture_output=True, text=True, env=env, check=False)
    assert valid.returncode == 0, valid.stderr
    inspected = subprocess.run([sys.executable, "-I", "-B", str(tool), "--root", str(tmp_path), "inspect"],
                               capture_output=True, text=True, env=env, check=False)
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["binding"] == "unknown"
    for arguments in (["adopt"], ["update", "--components", "api"], ["recover"]):
        result = subprocess.run([sys.executable, "-I", "-B", str(tool), "--root", str(tmp_path), *arguments],
                                capture_output=True, text=True, env=env, check=False)
        assert result.returncode != 0
        assert "component source and deployment roots differ" in result.stderr
    assert json.loads(path.read_text()) == journal


def test_duplicate_keys_and_nonfinite_values_are_rejected(tmp_path: Path, journal: dict) -> None:
    path = tmp_path / "transaction.json"
    data = json.dumps(journal)
    for replacement in ('"schema_version": 2, "schema_version": 2', '"schema_version": NaN'):
        path.write_text(data.replace('"schema_version": 2', replacement, 1), encoding="utf-8")
        with pytest.raises(runtime.JournalError):
            runtime.load_journal(path)


def test_symlink_ancestor_leaf_and_fifo_are_refused(tmp_path: Path, journal: dict) -> None:
    actual = tmp_path / "journal.json"
    actual.write_text(json.dumps(journal), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(actual)
    with pytest.raises(runtime.JournalError):
        runtime.load_journal(link)
    ancestor = tmp_path.parent / (tmp_path.name + "-alias")
    ancestor.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(runtime.JournalError):
        runtime.load_journal(ancestor / "journal.json")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(runtime.JournalError, match="regular"):
        runtime.load_journal(fifo)
