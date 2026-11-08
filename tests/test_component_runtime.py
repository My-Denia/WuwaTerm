from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from scripts import component_observe as observe
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


def test_historical_wheel_image_manifest_without_receipt_parses_in_manifest_and_journal(journal: dict) -> None:
    target_manifest = journal["target"]["manifest"]
    telegram_target = target_manifest["components"]["telegram"]
    provenance = telegram_target["provenance"]
    provenance.pop("build_receipt_sha256", None)
    assert set(provenance.keys()) == {
        "kind", "telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_producer_kind",
    }
    assert runtime.manifest.validate_manifest(target_manifest) is target_manifest

    if journal["before"]["topology"].get("telegram") is not None:
        journal["before"]["topology"]["telegram"]["provenance"].pop("build_receipt_sha256", None)

    raw_manifest = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    journal["manifest"]["sha256"] = hashlib.sha256(raw_manifest).hexdigest()

    assert runtime.validate_journal(journal) is journal


def test_schema_three_binds_compose_and_inspect_reports_saved_launcher(tmp_path: Path, journal: dict) -> None:
    journal["schema_version"] = 3
    digest = "a" * 64
    journal["source"]["compose"] = {"path": f".deployments/component-compose-{digest}", "sha256": digest}
    runtime.validate_journal(journal)
    deployments = tmp_path / ".deployments"
    deployments.mkdir(exist_ok=True)
    (deployments / runtime.JOURNAL_NAME).write_text(json.dumps(journal), encoding="utf-8")
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    result = runtime.inspect_deployment(tmp_path)
    assert result["recovery"]["status"] == "recorded-unverified"
    argv = result["recovery"]["argv"]
    assert argv[argv.index("--id") + 1] == journal["transaction_id"]
    assert argv[argv.index("--compose-executable") + 1] == str(tmp_path / journal["source"]["compose"]["path"])
    assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
    journal["source"]["compose"]["path"] = ".deployments/other"
    with pytest.raises(runtime.JournalError, match="invalid path"):
        runtime.validate_journal(journal)


def test_schema_four_requires_a_bounded_exact_predecessor(journal: dict) -> None:
    previous_id = "20261106T100000Z-" + "1" * 32
    predecessor_id = "20261106T103000Z-" + "2" * 32
    previous = copy.deepcopy(journal["target"]["manifest"])
    previous["deployment_id"] = previous_id
    journal["before"]["manifest"] = previous
    journal["pointers"]["old"][".deploy_current"] = {"present": True, "bytes": previous_id + "\n"}
    journal["schema_version"] = 4
    digest = "a" * 64
    journal["source"]["compose"] = {"path": f".deployments/component-compose-{digest}", "sha256": digest}
    prior_raw = b"exact-terminal-journal\n"
    journal["predecessor"] = runtime.predecessor_reference("full", predecessor_id, prior_raw)
    assert runtime.validate_journal(journal) is journal
    assert journal["predecessor"]["journal_sha256"] == hashlib.sha256(prior_raw).hexdigest()
    for field, value in (("kind", "legacy"), ("transaction_id", journal["transaction_id"]),
                         ("journal_path", ".deployments/other.json"),
                         ("archive_path", ".deployments/component-full-history/other.json"),
                         ("journal_sha256", "bad")):
        altered = copy.deepcopy(journal)
        altered["predecessor"][field] = value
        with pytest.raises(runtime.JournalError):
            runtime.validate_journal(altered)
    journal["predecessor"] = None
    with pytest.raises(runtime.JournalError, match="binding is required"):
        runtime.validate_journal(journal)


def test_inspect_reports_version_probe_launcher_without_writes(tmp_path: Path, journal: dict) -> None:
    digest = "a" * 64
    source = {**journal["source"], "compose": {"path": f".deployments/component-compose-{digest}", "sha256": digest}}
    record = {"schema_version": 1, "kind": "legacy-core-version", "probe_id": DEPLOYMENT_ID,
              "status": "active", "phase": "prepared", "image_id": "sha256:" + "1" * 64,
              "image_revision": "b" * 40, "container_name": "wuwaterm-adopt-probe-" + DEPLOYMENT_ID,
              "container_id": None, "installed_core_version": None, "output_sha256": None,
              "failure": None, "source": source, "daemon_id": "2" * 64, "predecessor_sha256": None}
    directory = tmp_path / ".deployments" / "component-adopt-probes"
    directory.mkdir(parents=True)
    (directory / (DEPLOYMENT_ID + ".json")).write_text(json.dumps(record), encoding="utf-8")
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    result = runtime.inspect_deployment(tmp_path)
    argv = result["probe_recovery"][0]["argv"]
    assert argv[argv.index("--kind") + 1] == "versionprobe"
    assert argv[argv.index("--id") + 1] == DEPLOYMENT_ID
    assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before


def test_legacy_probe_dispatch_is_limited_to_recorded_create_and_start() -> None:
    record = {"kind": "legacy-core-version", "status": "active", "phase": "create",
              "probe_id": DEPLOYMENT_ID, "container_name": "wuwaterm-adopt-probe-" + DEPLOYMENT_ID,
              "container_id": None, "image_id": "sha256:" + "1" * 64}
    create = runtime._adopt_probe_argv(record, "create")
    assert create[:3] == ["create", "--name", record["container_name"]]
    assert create[-6:] == [record["image_id"], "-I", "-S", "-B", "-c", runtime._LEGACY_VERSION_CODE]
    assert "--network" in create and create[create.index("--network") + 1] == "none"
    assert "--read-only" in create and "--no-healthcheck" in create
    with pytest.raises(runtime.JournalError):
        runtime._adopt_probe_argv(record, "build")
    with pytest.raises(runtime.JournalError):
        runtime._adopt_probe_argv(record, "start")
    record.update(phase="start", container_id="2" * 64)
    assert runtime._adopt_probe_argv(record, "start") == ["start", "--attach", "2" * 64]
    with pytest.raises(runtime.JournalError):
        runtime._adopt_probe_argv(record, "create")
    record["container_name"] = "foreign-container"
    with pytest.raises(runtime.JournalError):
        runtime._adopt_probe_argv(record, "start")


def test_inspection_reports_unadopted_legacy_as_unknown_without_writes(tmp_path: Path) -> None:
    (tmp_path / ".deploy_commit").write_text(A + "\n", encoding="ascii")
    before = {entry.name for entry in tmp_path.iterdir()}
    observed = runtime.inspect_deployment(tmp_path)
    assert observed["binding"] == "unknown"
    assert observed["source_commit"] == A
    assert observed["live_binding"] == "unverified"
    assert {entry.name for entry in tmp_path.iterdir()} == before


def test_wrapper_inspect_is_read_only_and_mutators_share_fd9(tmp_path: Path) -> None:
    wrapper = Path(runtime.__file__).resolve().parents[1] / "deploy/product-update.sh"
    command = [str(wrapper), "--root", str(tmp_path)]
    inspected = subprocess.run([*command, "inspect"], capture_output=True, text=True, check=False)
    assert inspected.returncode == 0, inspected.stderr
    assert not (tmp_path / ".deployments").exists()
    adopt = ["adopt", "--project", "test", "--compose-executable", "/bin/false"]
    first = subprocess.run([*command, *adopt], capture_output=True, text=True, check=False)
    assert first.returncode != 0
    assert "source and deployment roots differ" in first.stderr
    lock = tmp_path / ".deployments/.deployment.lock"
    assert lock.is_file()
    with lock.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = subprocess.run([*command, *adopt], capture_output=True, text=True, check=False)
        assert busy.returncode == 75
        assert "already in progress" in busy.stderr
    reused = subprocess.run(
        ["bash", "-c", 'exec 9<>"$1"; flock -n 9; shift; exec "$@"', "fixture", str(lock),
         *command, *adopt], capture_output=True, text=True, check=False,
    )
    assert reused.returncode != 0
    assert "source and deployment roots differ" in reused.stderr
    other = tmp_path / "other-root"
    other.mkdir()
    foreign = subprocess.run(
        ["bash", "-c", 'exec 9<>"$1"; flock -n 9; shift; exec "$@"', "fixture", str(lock),
         str(wrapper), "--root", str(other), *adopt], capture_output=True, text=True, check=False,
    )
    assert foreign.returncode != 0
    assert "inherited deployment lock does not match" in foreign.stderr
    assert not (other / ".deployments").exists()


def test_compose_input_snapshot_detects_replacement_and_mode_drift(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("NAME=fixture\n")
    path.chmod(0o644)
    original = runtime._compose_input(path)
    path.chmod(0o600)
    assert runtime._compose_input(path) != original
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    same_mode = runtime._compose_input(path)
    os.replace(replacement, path)
    assert runtime._compose_input(path) != same_mode


@pytest.mark.skipif(not os.environ.get("WUWATERM_OBSERVE_TEST_ID"), reason="Docker fixture was not supplied")
def test_readonly_cli_inspects_actual_recorded_container(tmp_path: Path) -> None:
    container_id = os.environ["WUWATERM_OBSERVE_TEST_ID"]
    image_id = os.environ["WUWATERM_OBSERVE_TEST_IMAGE"]
    actual = observe.observe_container(
        container_id, expected_image_id=image_id, project="wuwaterm-observe-cbe9", service="api",
        expected_command=["bot"], expected_entrypoint=None, env_fingerprint_key=b"k" * 32,
    )
    snapshot = {**actual["recipe"], "schema_version": 1, "image_id": image_id,
                "env_files": [], "environment_overrides": []}
    path, digest = runtime.manifest.write_recipe_snapshot(tmp_path, snapshot)
    installed = component(tmp_path, "api")
    installed["image"].update(id=image_id, digest=image_id)
    installed["recipe"] = {key: snapshot[key] for key in runtime.manifest._RECIPE_FIELDS}
    installed["recipe"].update(snapshot_path=path, sha256=digest)
    product = {
        "schema_version": 2, "deployment_id": DEPLOYMENT_ID, "deployment_utc": "2026-11-06T11:00:00Z",
        "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": A,
                       "source_tree": B, "source_tool_sha256": H},
        "components": {"api": installed, "telegram": None},
        "database": make_database(tmp_path / "terms.db"), "api_identity": None,
    }
    runtime.manifest.write_manifest(tmp_path, product)
    (tmp_path / ".deploy_commit").write_text(A + "\n", encoding="ascii")
    (tmp_path / ".deploy_current").write_text(DEPLOYMENT_ID + "\n", encoding="ascii")
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    result = subprocess.run(
        [str(Path(runtime.__file__).resolve().parents[1] / "deploy/product-update.sh"), "--root", str(tmp_path),
         "inspect", "--component", "api", "--container-id", container_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    recorded = json.loads(result.stdout)
    live = recorded["live_observation"]
    assert live["container"]["id"] == container_id
    assert live["container"]["running"] is False
    assert live["health"] == live["env_file_binding"] == live["source_provenance"] == "unverified"
    assert live["different_recipe_fields"] == []
    assert "environment_fingerprint" not in result.stdout
    assert "not-a-real-secret" not in result.stdout
    assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before

    unrecorded = subprocess.run(
        [str(Path(runtime.__file__).resolve().parents[1] / "deploy/product-update.sh"), "observe",
         "--container-id", container_id, "--image-id", image_id, "--project", "wuwaterm-observe-cbe9",
         "--service", "api", "--command-json", '["bot"]', "--entrypoint-json", "null"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    assert unrecorded.returncode == 0, unrecorded.stderr
    response = json.loads(unrecorded.stdout)
    assert response["container"]["id"] == container_id
    assert response["binding"] == response["health"] == response["env_file_binding"] == "unverified"
    assert "environment_fingerprint" not in unrecorded.stdout
    assert "not-a-real-secret" not in unrecorded.stdout


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
    for arguments in (["adopt", "--project", "test", "--compose-executable", "/bin/false"],
                      ["update", "--components", "api", "--api-receipt", "1" * 64,
                       "--compose-executable", "/bin/false"]):
        result = subprocess.run([sys.executable, "-I", "-B", str(tool), "--root", str(tmp_path), *arguments],
                                capture_output=True, text=True, env=env, check=False)
        assert result.returncode != 0
        assert "component source and deployment roots differ" in result.stderr
    recovery = subprocess.run(
        [sys.executable, "-I", "-B", str(tool), "--root", str(tmp_path), "recover",
         "--transaction-id", journal["transaction_id"], "--compose-executable", "/bin/false"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert recovery.returncode != 0
    assert "lock" in recovery.stderr or "metadata directory" in recovery.stderr
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


def _version_probe_record(journal: dict) -> dict:
    source = {**journal["source"], "compose": {"path": ".deployments/component-compose-" + "a" * 64,
                                                "sha256": "a" * 64}}
    return {"schema_version": 1, "kind": "legacy-core-version", "probe_id": DEPLOYMENT_ID,
            "status": "active", "phase": "prepared", "image_id": "sha256:" + "1" * 64,
            "image_revision": "b" * 40, "container_name": "wuwaterm-adopt-probe-" + DEPLOYMENT_ID,
            "container_id": None, "installed_core_version": None, "output_sha256": None,
            "failure": None, "source": source, "daemon_id": "2" * 64, "predecessor_sha256": None}


def _locked_runtime_command(root: Path, code: str, *arguments: str) -> list[str]:
    scripts = Path(runtime.__file__).resolve().parent
    return ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
            "fixture-lock", str(root), sys.executable, "-I", "-B", "-c", code, str(scripts), str(root), *arguments]


def _run_locked_runtime(root: Path, code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_locked_runtime_command(root, code, *arguments),
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)


def _kill_on_stop(command: list[str]) -> None:
    child = subprocess.Popen(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if pid:
                if os.WIFSTOPPED(status):
                    os.kill(child.pid, signal.SIGKILL)
                    assert child.wait(timeout=10) == -signal.SIGKILL
                    return
                child.returncode = os.waitstatus_to_exitcode(status)
                _, stderr = child.communicate()
                raise AssertionError(f"publisher exited before boundary: {stderr.decode(errors='replace')}")
            time.sleep(0.01)
        raise AssertionError("publisher did not reach the requested boundary")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate()


@pytest.mark.parametrize("boundary", ["before-claim", "after-claim", "after-stage-link", "after-rename"])
@pytest.mark.parametrize("completed", [False, True])
def test_probe_staging_crashes_recover_only_bound_inode(
    tmp_path: Path, journal: dict, boundary: str, completed: bool,
) -> None:
    (tmp_path / ".deployments").mkdir(exist_ok=True)
    old = _version_probe_record(journal)
    if completed:
        old.update(phase="cleanup", container_id="3" * 64, installed_core_version="0.6.1",
                   output_sha256="4" * 64)
    new = {**old, "phase": "completed" if completed else "create"}
    if completed:
        new["status"] = "completed"
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    new_path.write_text(json.dumps(new), encoding="utf-8")
    setup = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()),new=True)
'''
    result = _run_locked_runtime(tmp_path, setup, str(old_path))
    assert result.returncode == 0, result.stderr
    code = r'''import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
real_link=os.link; real_replace=os.replace; boundary=sys.argv[4]
def link(src,dst,*args,**kwargs):
 if str(dst).endswith('.claim') and boundary=='before-claim': os.kill(os.getpid(),signal.SIGSTOP)
 result=real_link(src,dst,*args,**kwargs)
 if str(dst).endswith('.claim') and boundary=='after-claim': os.kill(os.getpid(),signal.SIGSTOP)
 if str(dst).endswith('.stage') and boundary=='after-stage-link': os.kill(os.getpid(),signal.SIGSTOP)
 return result
def replace(src,dst,*args,**kwargs):
 result=real_replace(src,dst,*args,**kwargs)
 if str(dst).endswith('.json') and boundary=='after-rename': os.kill(os.getpid(),signal.SIGSTOP)
 return result
os.link=link; os.replace=replace
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
    _kill_on_stop(_locked_runtime_command(tmp_path, code, str(new_path), boundary))
    registry = tmp_path / ".deployments/component-adopt-probes"
    assert sorted(p.name for p in registry.iterdir()) == [DEPLOYMENT_ID + ".json"]
    stored = json.loads((registry / (DEPLOYMENT_ID + ".json")).read_bytes())
    assert stored == (new if boundary == "after-rename" else old)
    stage_dir = tmp_path / ".deployments/component-adopt-probe-staging"
    if boundary == "before-claim":
        assert list(stage_dir.iterdir()) == []
        assert runtime.pending_adopt_probe_staging(tmp_path) is None
    else:
        assert runtime.pending_adopt_probe_staging(tmp_path) == DEPLOYMENT_ID
        before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
        inspection = runtime.inspect_deployment(tmp_path)
        assert inspection["probe_recovery"][0]["staging"] == "pending-unverified"
        assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
        with pytest.raises(runtime.JournalError, match="requires recorded recovery"):
            runtime._read_adopt_probe(tmp_path, DEPLOYMENT_ID + ".json")
        validate = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(runtime.__file__).resolve()), "--root", str(tmp_path),
             "validate-probe", "--probe-id", DEPLOYMENT_ID],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert validate.returncode == 0, validate.stderr
        assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
        reconcile = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
print(json.dumps(r.reconcile_adopt_probe_staging(pathlib.Path(sys.argv[2]),sys.argv[3],source_tool_sha256=sys.argv[4])))
'''
        bad = _run_locked_runtime(tmp_path, reconcile, DEPLOYMENT_ID, "f" * 64)
        assert bad.returncode != 0 and runtime.pending_adopt_probe_staging(tmp_path) == DEPLOYMENT_ID
        first = _run_locked_runtime(tmp_path, reconcile, DEPLOYMENT_ID, old["source"]["source_tool_sha256"])
        assert first.returncode == 0 and json.loads(first.stdout) is True
        repeated = _run_locked_runtime(tmp_path, reconcile, DEPLOYMENT_ID, old["source"]["source_tool_sha256"])
        assert repeated.returncode == 0 and json.loads(repeated.stdout) is False
        assert list(stage_dir.iterdir()) == []
    assert runtime._read_adopt_probe(tmp_path, DEPLOYMENT_ID + ".json") == stored
    if stored != new:
        update = _run_locked_runtime(tmp_path, setup.replace(",new=True", ""), str(new_path))
        assert update.returncode == 0, update.stderr
    assert runtime._read_adopt_probe(tmp_path, DEPLOYMENT_ID + ".json") == new


def test_unknown_probe_staging_is_not_removed_or_admitted(tmp_path: Path, journal: dict) -> None:
    (tmp_path / ".deployments").mkdir(exist_ok=True)
    old = _version_probe_record(journal)
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(old), encoding="utf-8")
    setup = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()),new=True)
'''
    assert _run_locked_runtime(tmp_path, setup, str(record_path)).returncode == 0
    staging = tmp_path / ".deployments/component-adopt-probe-staging"
    staging.mkdir(mode=0o700)
    foreign = staging / "unknown"
    foreign.write_bytes(b"not an owned stage")
    before = foreign.stat().st_ino
    with pytest.raises(runtime.JournalError, match="unknown probe staging"):
        runtime.pending_adopt_probe_staging(tmp_path)
    assert runtime.inspect_deployment(tmp_path)["probe_recovery"] == {"status": "untrusted"}
    attempt = _run_locked_runtime(tmp_path, setup.replace(",new=True", ""), str(record_path))
    assert attempt.returncode != 0
    assert foreign.stat().st_ino == before and foreign.read_bytes() == b"not an owned stage"


@pytest.mark.parametrize("writer", ["adoption", "runtime"])
@pytest.mark.parametrize("boundary", ["before-link", "after-link"])
def test_initial_runtime_publication_is_single_link_across_process_death(
    tmp_path: Path, journal: dict, writer: str, boundary: str,
) -> None:
    (tmp_path / ".deployments").mkdir(exist_ok=True)
    baseline = {p.name for p in (tmp_path / ".deployments").iterdir()}
    input_path = tmp_path / "journal.json"
    input_path.write_text(json.dumps(journal), encoding="utf-8")
    code = r'''import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r,component_transaction as t,product_manifest as m
root=pathlib.Path(sys.argv[2]); journal=json.loads(pathlib.Path(sys.argv[3]).read_text())
writer,boundary=sys.argv[4:6]; original=os.link
def link(src,dst,*args,**kwargs):
 if dst==r.JOURNAL_NAME and boundary=='before-link': os.kill(os.getpid(),signal.SIGSTOP)
 result=original(src,dst,*args,**kwargs)
 if dst==r.JOURNAL_NAME and boundary=='after-link': os.kill(os.getpid(),signal.SIGSTOP)
 return result
os.link=link
if writer=='adoption': r._write_adopt_journal(root,journal,new=True)
else:
 with m._directory(root,('.deployments',),create=False) as d:
  t._publish_initial_runtime_journal(root,d,m._encode(r.validate_journal(journal)))
'''
    _kill_on_stop(_locked_runtime_command(tmp_path, code, str(input_path), writer, boundary))
    directory = tmp_path / ".deployments"
    entries = {p.name for p in directory.iterdir()} - {".deployment.lock"} - baseline
    path = directory / runtime.JOURNAL_NAME
    if boundary == "before-link":
        assert entries == set() and not path.exists()
    else:
        assert entries == {runtime.JOURNAL_NAME} and path.stat().st_nlink == 1
        assert runtime.load_journal(path) == journal
        sync = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_transaction as t
root=pathlib.Path(sys.argv[2]); t._sync_recovered_journal(root,json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
        assert _run_locked_runtime(tmp_path, sync, str(input_path)).returncode == 0
        assert runtime.load_journal(path) == journal and path.stat().st_nlink == 1


def test_replaced_probe_stage_same_bytes_is_not_recovery_authority(tmp_path: Path, journal: dict) -> None:
    (tmp_path / ".deployments").mkdir(exist_ok=True)
    old = _version_probe_record(journal)
    new = {**old, "phase": "create"}
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    new_path.write_text(json.dumps(new), encoding="utf-8")
    setup = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()),new=True)
'''
    assert _run_locked_runtime(tmp_path, setup, str(old_path)).returncode == 0
    interrupt = r'''import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
original=os.replace
def replace(src,dst,*args,**kwargs):
 if str(dst).endswith('.json'): os.kill(os.getpid(),signal.SIGSTOP)
 return original(src,dst,*args,**kwargs)
os.replace=replace
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
    _kill_on_stop(_locked_runtime_command(tmp_path, interrupt, str(new_path)))
    staging = tmp_path / ".deployments/component-adopt-probe-staging"
    staged, = staging.glob("*.stage")
    data = staged.read_bytes()
    held = staged.open("rb")
    try:
        staged.unlink()
        staged.write_bytes(data)
        staged.chmod(0o600)
        replacement = staged.stat().st_ino
        with pytest.raises(runtime.JournalError, match="inode or preimage differs"):
            runtime.pending_adopt_probe_staging(tmp_path)
        reconcile = r'''import pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
r.reconcile_adopt_probe_staging(pathlib.Path(sys.argv[2]),sys.argv[3],source_tool_sha256=sys.argv[4])
'''
        result = _run_locked_runtime(tmp_path, reconcile, DEPLOYMENT_ID, old["source"]["source_tool_sha256"])
        assert result.returncode != 0
        assert staged.stat().st_ino == replacement and staged.read_bytes() == data
        assert json.loads((tmp_path / ".deployments/component-adopt-probes" / (DEPLOYMENT_ID + ".json")).read_bytes()) == old
    finally:
        held.close()


def test_state_probe_staging_recovery_binds_exact_transaction(tmp_path: Path) -> None:
    from scripts import component_state as state

    (tmp_path / ".deployments").mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir()
    info = data.stat()
    identifier = "20261106T110000Z-" + "4" * 32
    transaction_id = "20261106T120000Z-" + "5" * 32
    record = {"schema_version": 4, "kind": "state-legacy-plan", "probe_id": identifier,
              "status": "active", "phase": "prepared", "transaction_id": transaction_id,
              "source_tool_sha256": "a" * 64, "daemon_id": "b" * 64,
              "image_id": "sha256:" + "c" * 64, "image_revision": "d" * 40,
              "container_name": "wuwaterm-state-probe-" + identifier, "container_id": None,
              "user": f"{os.geteuid()}:{os.getegid()}", "working_dir": "/app",
              "mounts": [{"host": str(data), "container": "/app/data", "read_only": True,
                          "device": info.st_dev, "inode": info.st_ino}],
              "path_settings": dict.fromkeys(state._PATH_KEYS), "input_path": "/app/data/terms.db",
              "command": None, "output_sha256": None, "result": None, "failure": None,
              "transaction_kind": "full", "flavor": None, "state_kind": None, "clock": None,
              "plan_request": {"recipe_sha256": "e" * 64}, "extra_read_binds": []}
    record["command"] = state._command(record)
    before_path, after_path = tmp_path / "before.json", tmp_path / "after.json"
    before_path.write_text(json.dumps(record), encoding="utf-8")
    after_path.write_text(json.dumps({**record, "phase": "create"}), encoding="utf-8")
    setup = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()),new=True)
'''
    assert _run_locked_runtime(tmp_path, setup, str(before_path)).returncode == 0
    interrupt = r'''import json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
original=os.replace
def replace(src,dst,*args,**kwargs):
 if str(dst).endswith('.json'): os.kill(os.getpid(),signal.SIGSTOP)
 return original(src,dst,*args,**kwargs)
os.replace=replace
r._write_adopt_probe(pathlib.Path(sys.argv[2]),json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
    _kill_on_stop(_locked_runtime_command(tmp_path, interrupt, str(after_path)))
    assert runtime.pending_adopt_probe_staging(tmp_path) == identifier
    code = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_runtime as r
print(json.dumps(r.reconcile_adopt_probe_staging(pathlib.Path(sys.argv[2]),sys.argv[3],
 source_tool_sha256=sys.argv[4],transaction_id=sys.argv[5])))
'''
    wrong = _run_locked_runtime(tmp_path, code, identifier, "a" * 64, "20261106T130000Z-" + "6" * 32)
    assert wrong.returncode != 0 and runtime.pending_adopt_probe_staging(tmp_path) == identifier
    first = _run_locked_runtime(tmp_path, code, identifier, "a" * 64, transaction_id)
    assert first.returncode == 0 and json.loads(first.stdout) is True
    repeated = _run_locked_runtime(tmp_path, code, identifier, "a" * 64, transaction_id)
    assert repeated.returncode == 0 and json.loads(repeated.stdout) is False
    assert runtime._read_adopt_probe(tmp_path, identifier + ".json") == record


def test_recovery_sync_refuses_hardlinked_runtime_journal(tmp_path: Path, journal: dict) -> None:
    directory = tmp_path / ".deployments"
    directory.mkdir(exist_ok=True)
    path = directory / runtime.JOURNAL_NAME
    raw = runtime.manifest._encode(runtime.validate_journal(journal))
    path.write_bytes(raw)
    alias = tmp_path / "foreign-hardlink"
    os.link(path, alias)
    input_path = tmp_path / "journal-input.json"
    input_path.write_text(json.dumps(journal), encoding="utf-8")
    code = r'''import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import component_transaction as t
root=pathlib.Path(sys.argv[2]); t._sync_recovered_journal(root,json.loads(pathlib.Path(sys.argv[3]).read_text()))
'''
    result = _run_locked_runtime(tmp_path, code, str(input_path))
    assert result.returncode != 0
    assert path.stat().st_nlink == 2 and alias.stat().st_ino == path.stat().st_ino
    assert path.read_bytes() == alias.read_bytes() == raw


@pytest.mark.parametrize("case", ["symlink", "inaccessible", "missing-registry"])
def test_inspect_untrusted_probe_staging_is_read_only_and_blocks_admission(
    tmp_path: Path, journal: dict, case: str,
) -> None:
    if case == "inaccessible" and os.geteuid() == 0:
        pytest.skip("requires a non-root filesystem permission check")
    deployments = tmp_path / ".deployments"
    deployments.mkdir(exist_ok=True)
    staging = deployments / "component-adopt-probe-staging"
    registry = deployments / "component-adopt-probes"
    watched = [tmp_path, deployments]
    probe_path = None
    sentinel = None
    if case != "missing-registry":
        registry.mkdir(mode=0o700)
        probe_path = registry / (DEPLOYMENT_ID + ".json")
        probe_path.write_bytes(runtime.manifest._encode(_version_probe_record(journal)))
        watched.extend((registry, probe_path))
    if case == "symlink":
        target = tmp_path / "foreign-directory"
        target.mkdir()
        sentinel = target / "sentinel"
        sentinel.write_bytes(b"untouched")
        staging.symlink_to(target, target_is_directory=True)
        watched.extend((target, sentinel))
    else:
        staging.mkdir(mode=0o700)
        if case == "inaccessible":
            sentinel = staging / "sentinel"
            sentinel.write_bytes(b"untouched")
            staging.chmod(0o000)
    watched.append(staging)

    def identity(path: Path) -> tuple[int, ...]:
        item = path.lstat()
        return (item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_gid,
                item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

    before = {path: identity(path) for path in watched}
    probe_raw = None if probe_path is None else probe_path.read_bytes()
    try:
        result = runtime.inspect_deployment(tmp_path)
        assert result["probe_recovery"] == {"status": "untrusted"}
        with pytest.raises(runtime.admission.AdmissionError):
            runtime.admission._attempts_clear(tmp_path)
        assert {path: identity(path) for path in watched} == before
        if probe_path is not None:
            assert probe_path.read_bytes() == probe_raw
        if case == "symlink":
            assert staging.is_symlink() and sentinel is not None and sentinel.read_bytes() == b"untouched"
        if case == "missing-registry":
            assert not registry.exists() and list(staging.iterdir()) == []
    finally:
        if case == "inaccessible":
            staging.chmod(0o700)
            assert sentinel is not None and sentinel.read_bytes() == b"untouched"


# ---------------------------------------------------------------- schema 5

PRIOR_ID = "20261106T100000Z-" + "9" * 32


def _schema5(journal: dict) -> dict:
    """An API-only completed-artifact runtime update over an adopted schema-2 manifest."""
    value = copy.deepcopy(journal)
    target = value["target"]["manifest"]
    target["components"]["telegram"] = None
    for side in ("before", "target"):
        value[side]["containers"]["telegram"] = None
    prior = copy.deepcopy(target)
    prior["deployment_id"] = PRIOR_ID
    value["before"].update(manifest=prior, topology=copy.deepcopy(prior["components"]))
    value["pointers"]["old"] = {".deploy_commit": {"present": True, "bytes": A + "\n"},
                                ".deploy_current": {"present": True, "bytes": PRIOR_ID + "\n"}}
    api = target["components"]["api"]
    api["image"]["digest"] = api["image"]["id"]
    api["provenance"] = {"kind": "completed-artifact", "bundle_sha256": H, "acquisition_sha256": J,
                         "authority": "local-source", "native_image_id": api["image"]["id"]}
    target["schema_version"] = 3
    value["source"]["compose"] = {"path": ".deployments/component-compose-" + H, "sha256": H}
    value.update(schema_version=5,
                 predecessor=runtime.predecessor_reference("runtime", PRIOR_ID, b"prior journal\n"))
    raw = (json.dumps(target, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    value["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return value


def test_schema5_journal_is_a_completed_artifact_update(journal: dict) -> None:
    assert runtime.JOURNAL_SCHEMA_VERSION == 5 and runtime.RUNTIME4_SCHEMA_VERSION == 4
    value = _schema5(journal)
    assert runtime.validate_journal(value) is value
    # Historical schemas still parse with their own rules.
    assert runtime.validate_journal(journal) is journal


@pytest.mark.parametrize("change, message", [
    (lambda j: j.update(schema_version=4), "schema differs from the journal schema"),
    (lambda j: j.update(action="adopt", selection=[]), "schema-5 records only completed-artifact updates"),
    (lambda j: j.pop("predecessor"), "unexpected or missing fields"),
])
def test_schema5_journal_refusals(journal: dict, change, message: str) -> None:
    value = _schema5(journal)
    change(value)
    with pytest.raises(runtime.JournalError, match=message):
        runtime.validate_journal(value)


def test_schema4_journal_cannot_embed_a_schema3_manifest(journal: dict) -> None:
    value = _schema5(journal)
    value["schema_version"] = 4
    with pytest.raises(runtime.JournalError, match="schema differs"):
        runtime.validate_journal(value)


def test_adopt_probe_create_carries_the_root_scope_label() -> None:
    record = {"kind": "legacy-core-version", "status": "active", "phase": "create",
              "probe_id": DEPLOYMENT_ID, "container_name": "wuwaterm-adopt-probe-" + DEPLOYMENT_ID,
              "container_id": None, "image_id": "sha256:" + "1" * 64}
    scope = runtime.admission.root_label_value(Path("/srv/example-root"))
    create = runtime._adopt_probe_argv(record, "create", scope)
    labels = [create[i + 1] for i, item in enumerate(create) if item == "--label"]
    assert labels == ["org.wuwaterm.component-adopt-probe=" + DEPLOYMENT_ID,
                      runtime.admission.ROOT_LABEL + "=" + scope]
    with pytest.raises(runtime.JournalError):
        runtime._adopt_probe_argv(record, "create", "not-a-digest")


@pytest.mark.parametrize("command", [
    ["capsule-recover", "--transaction-id", DEPLOYMENT_ID],
    ["capsule-recover-full", "--transaction-id", DEPLOYMENT_ID],
    ["capsule-recover-probe", "--probe-id", DEPLOYMENT_ID],
])
def test_capsule_recovery_entries_stay_closed_under_protocol2(tmp_path: Path, command: list[str]) -> None:
    """Protocol 2 is open by default, but fails closed when readiness flags are false."""
    assert runtime.capsule_execution_ready(full=False) is True
    assert runtime.capsule_execution_ready(full=True) is True
    assert runtime.CAPSULE_RECOVERY_PROTOCOL == runtime.CAPSULE_FULL_RECOVERY_PROTOCOL == 2
    root = tmp_path / "root"
    (root / ".deployments").mkdir(parents=True)
    before = sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*"))
    scripts_dir = str(Path(runtime.__file__).resolve().parent)
    code = (
        "import sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "import component_recovery as cr; "
        "cr.ARTIFACT_RECOVERY_EXECUTION_READY = False; "
        "cr.ARTIFACT_FULL_RECOVERY_EXECUTION_READY = False; "
        "import component_runtime as runtime; "
        "raise SystemExit(runtime.main(sys.argv[2:]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, scripts_dir, "--root", str(root), *command,
         "--capsule-source-tool", H, "--compose-executable", "/bin/false"],
        capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "not yet available" in result.stderr
    assert sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*")) == before
