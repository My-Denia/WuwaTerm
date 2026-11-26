"""Refreshing an adopted API record that predates the recorded resource options.

Only the Docker daemon, the saved-source capsule and image provenance are
replaced here. Observation, recipe and journal validation, predecessor
archiving, pointer publication, recovery, terminal verification and update
preparation are the real code.
"""
from __future__ import annotations

import copy
import fcntl
import os
from pathlib import Path
import subprocess

import pytest

from scripts import component_runtime as runtime
from scripts import product_manifest as pm
import component_operations as operations
import component_prepare as prepare
import component_terminal as terminal_ops
import component_transaction as transaction
from test_component_observe import observation
from test_component_runtime import journal as journal_fixture
from test_product_manifest import A, B, H, J

# The modules the production code itself imports (top-level names).
observe = operations.observe
recovery = operations.recovery
admission = operations.admission
adoption = __import__("component_adoption")

OLD_ID = "a" * 64
NEW_ID = "e" * 64
TG_ID = "f" * 64
COMPOSE = Path("/unused/docker-compose")
RESERVATION = 96 * 1024 * 1024
OOM_SCORE = -500
ROOT = Path(__file__).resolve().parents[1]
JournalError = adoption.JournalError


def _raw(identifier: str, *, service: str, name: str, image: str, command: list[str],
         env: list[str], mounts: list[tuple[Path, str, bool]]) -> dict:
    raw = observation()
    raw.update(Id=identifier, Image=image, Name="/" + name)
    raw["Config"].update(Cmd=command, Hostname=identifier[:12], Env=env, Labels={
        "com.docker.compose.project": "project", "com.docker.compose.service": service})
    raw["Mounts"] = [{"Type": "bind", "Source": str(source), "Destination": target, "RW": writable,
                      "Mode": "rw" if writable else "ro", "Propagation": "rprivate"}
                     for source, target, writable in mounts]
    return raw


class Deployment:
    """One adopted deployment root with an in-memory Docker daemon."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *, telegram: bool,
                 recorded_options: dict[str, int] | None = None) -> None:
        self.root = root
        sample = journal_fixture.__wrapped__(root)
        database = sample["database"]["binding"]
        data, state, api_state = root / "terms.db", root / "state", root / "state-api"
        state.mkdir()
        api_state.mkdir()
        (api_state / "devices.db").write_bytes(b"synthetic-device-store\n")
        identity_stat = (api_state / "devices.db").stat()
        identity = {"path": str(api_state / "devices.db"), "device": identity_stat.st_dev,
                    "inode": identity_stat.st_ino}
        env_file = root / "fixture.env"
        env_file.write_text("FIXTURE=1\n", encoding="ascii")
        components = sample["before"]["topology"]
        database_env = "WUWATERM_DB_PATH=/app/data/terms.db"
        self.containers: dict[str, dict] = {
            OLD_ID: _raw(OLD_ID, service="wuwaterm-api", name="wuwaterm-api",
                         image=components["api"]["image"]["id"], command=["api"],
                         env=[database_env, "WUWATERM_API_STATE_DIR=/app/state-api"],
                         mounts=[(data, "/app/data/terms.db", False), (api_state, "/app/state-api", True)])}
        if telegram:
            self.containers[TG_ID] = _raw(
                TG_ID, service="wuwaterm-telegram", name="wuwaterm-telegram",
                image=components["telegram"]["image"]["id"], command=["bot"],
                env=[database_env, "WUWATERM_STATE_DIR=/app/state"],
                mounts=[(data, "/app/data/terms.db", False), (state, "/app/state", True)])
        self.environment = {raw["Config"]["Labels"]["com.docker.compose.service"]: list(raw["Config"]["Env"])
                            for raw in self.containers.values()}
        self.revisions = {item["image"]["id"]: item["image"]["revision"]
                          for item in components.values()}
        if recorded_options:
            self.apply_options(OLD_ID, **recorded_options)
        records = {"api": None, "telegram": None}
        topology = {"api": None, "telegram": None}
        for name, identifier in (("api", OLD_ID), ("telegram", TG_ID)):
            if identifier not in self.containers:
                continue
            raw = self.containers[identifier]
            actual = observe._extract(
                raw, container_id=identifier, expected_image_id=raw["Image"], project="project",
                service=raw["Config"]["Labels"]["com.docker.compose.service"],
                expected_command=raw["Config"]["Cmd"], expected_entrypoint=raw["Config"]["Entrypoint"],
                env_fingerprint_key=b"k" * 32)
            snapshot = {**actual["recipe"], "schema_version": 1, "image_id": raw["Image"],
                        "env_files": [str(env_file)], "environment_overrides": []}
            path, digest = pm.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in pm._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            topology[name] = {**components[name], "recipe": recipe}
            records[name] = actual["container"]
        if not telegram:
            self.revisions.pop(components["telegram"]["image"]["id"])

        # A committed schema-4 adoption, as an earlier release recorded it.
        compose = {"path": ".deployments/component-compose-" + H, "sha256": H}
        record = copy.deepcopy(sample)
        record.update(schema_version=4, action="adopt", selection=[], predecessor=None,
                      status="committed", phase="committed", api_identity=identity)
        record["source"]["compose"] = compose
        record["target"]["manifest"].update(components=copy.deepcopy(topology), api_identity=identity)
        record["before"].update(topology=copy.deepcopy(topology), containers=copy.deepcopy(records))
        record["target"]["containers"] = copy.deepcopy(records)
        record["manifest"]["sha256"] = __import__("hashlib").sha256(
            pm._encode(record["target"]["manifest"])).hexdigest()
        adoption.validate_journal(record)
        self.source = record["source"]
        self.database = database
        pm.write_manifest(root, record["target"]["manifest"])
        self.journal_path.write_bytes(pm._encode(record))
        for name, value in record["pointers"]["target"].items():
            (root / name).write_text(value["bytes"], encoding="ascii")
        self.adoption = record
        self._patch(monkeypatch)

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def inspect(identifier: str) -> dict:
            if identifier not in self.containers:
                raise observe.ObservationError("Docker did not return the requested metadata")
            return copy.deepcopy(self.containers[identifier])

        def inventory(project: str, service: str) -> list[dict]:
            return [{"Id": identifier, "Labels": raw["Config"]["Labels"]}
                    for identifier, raw in self.containers.items()
                    if raw["Config"]["Labels"]["com.docker.compose.project"] == project
                    and raw["Config"]["Labels"]["com.docker.compose.service"] == service]

        def effective(snapshot: dict, image: dict, compose_path: Path, root: Path) -> dict:
            return operations._env_map(self.environment[snapshot["service"]])

        monkeypatch.setattr(observe, "_inspect", inspect)
        shared = {
            "_inventory": inventory,
            "_image": lambda image_id: {"Id": image_id, "Config": {"Labels": {
                "org.opencontainers.image.revision": self.revisions[image_id]}}},
            "_effective_environment": effective,
            "_compose_binary": lambda path: Path(path),
            "_verify_saved_source": lambda *args: None,
            "_verify_old_topology": lambda *args, **kwargs: None,
            "_verify_candidate": lambda *args: None,
            "_ready": lambda *args: None,
            "_container_present": lambda identifier: identifier in self.containers,
        }
        for name, value in shared.items():
            for module in (operations, transaction):
                monkeypatch.setattr(module, name, value)
        for module in (adoption, terminal_ops, __import__("component_terminal_full"), transaction,
                       admission, prepare, operations):
            monkeypatch.setattr(module, "CODE_ROOT", self.root)
        # Full-record routes also require their modules to come from that root.
        for name in ("component_terminal_full", "component_state_endpoint"):
            monkeypatch.setattr(__import__(name), "__file__", str(self.root / "scripts" / (name + ".py")))
        monkeypatch.setattr(admission, "require_lock", lambda root: None)
        monkeypatch.setattr(admission, "_DOCKER_SOCKET", str(self.root / "absent.sock"))
        monkeypatch.setattr(recovery, "safe_git", lambda root, *args, **kwargs: (
            (B if args[-1].endswith("tree}") else A) + "\n").encode("ascii"))
        monkeypatch.setattr(recovery, "_load_packager", lambda *args: None)
        monkeypatch.setattr(recovery, "_verify_clean_source", lambda *args: None)
        monkeypatch.setattr(recovery, "prepare_source", lambda *args, **kwargs: copy.deepcopy(self.source))
        monkeypatch.setattr(recovery, "_verify_bound_compose", lambda root, source: COMPOSE)
        for module in (adoption, operations.runtime):
            monkeypatch.setattr(module, "tool_bytes_identity", lambda root: H)

    # -- live Docker changes -------------------------------------------------
    def apply_options(self, identifier: str, *, memory_reservation: int = 0, oom_score_adj: int = 0) -> None:
        self.containers[identifier]["HostConfig"].update(
            MemoryReservation=memory_reservation, OomScoreAdj=oom_score_adj)

    def compose_recreate(self, **options: int) -> None:
        """`docker compose up --force-recreate`: a new ID, name and settings kept."""
        raw = self.containers.pop(OLD_ID)
        raw["Id"] = NEW_ID
        raw["Config"]["Hostname"] = NEW_ID[:12]
        raw["State"]["StartedAt"] = "2026-11-07T09:30:00.5Z"
        self.containers[NEW_ID] = raw
        self.apply_options(NEW_ID, **options)

    # -- observations of the record -----------------------------------------
    @property
    def journal_path(self) -> Path:
        directory = self.root / ".deployments"
        directory.mkdir(exist_ok=True)
        return directory / runtime.JOURNAL_NAME

    def journal(self) -> dict:
        return runtime.load_journal(self.journal_path)

    def api_snapshot(self) -> dict:
        product = runtime.inspect_deployment(self.root)["manifest"]
        return operations._snapshot(self.root, product["components"]["api"])

    def strict_terminal(self):
        return terminal_ops.verify_terminal(self.root, compose_path=COMPOSE)

    def refresh(self, identifier: str = OLD_ID) -> dict:
        return runtime.refresh_runtime_options(self.root, container_id=identifier, compose_executable=COMPOSE)

    def authority(self) -> tuple:
        """Everything that decides what the deployment records."""
        history = self.root / ".deployments" / "component-runtime-history"
        products = self.root / ".deployments" / "products"
        return (self.journal_path.read_bytes(), (self.root / ".deploy_commit").read_bytes(),
                (self.root / ".deploy_current").read_bytes(),
                sorted(item.name for item in products.iterdir()),
                sorted(item.name for item in history.iterdir()) if history.exists() else [])


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(**kwargs) -> Deployment:
        kwargs.setdefault("telegram", True)
        root = tmp_path / "root"
        root.mkdir()
        return Deployment(root, monkeypatch, **kwargs)
    return build


def _replayed_host(monkeypatch: pytest.MonkeyPatch, record: Deployment, identifier: str) -> dict:
    created: list[dict] = []
    monkeypatch.setattr(operations, "_docker", lambda method, path, body=None, **kwargs: (
        created.append(body) or {"Id": "d" * 64}))
    operations._create_from_snapshot(
        {"transaction_id": "t" * 16, "before": {"containers": {"api": {"id": identifier}}}},
        record.api_snapshot(), {}, "api")
    return created[0]["HostConfig"]


def _assert_gap_closed(monkeypatch: pytest.MonkeyPatch, record: Deployment, identifier: str,
                       expected: dict[str, int]) -> None:
    # The default strict verification, with no override, accepts the new record.
    assert record.strict_terminal() is not None
    seen = runtime.inspect_container(record.root, "api", identifier)
    assert seen["live_observation"]["different_recipe_fields"] == []
    snapshot = record.api_snapshot()
    operations._recipe_matches(operations._observed(identifier, snapshot, b"k" * 32), snapshot)
    assert {key: value for key, value in snapshot["runtime_options"].items()
            if key in pm._RESOURCE_OPTIONS} == expected
    host = _replayed_host(monkeypatch, record, identifier)
    assert host.get("MemoryReservation") == expected.get("memory_reservation")
    assert host.get("OomScoreAdj") == expected.get("oom_score_adj")


def test_docker_update_reservation_is_recorded_on_the_same_container(deployment, monkeypatch) -> None:
    record = deployment()
    record.apply_options(OLD_ID, memory_reservation=RESERVATION)
    with pytest.raises(operations.TransactionError, match="observed container recipe differs"):
        record.strict_terminal()
    predecessor_raw = record.journal_path.read_bytes()
    old_manifest = runtime.inspect_deployment(record.root)["manifest"]
    old_files = {path: path.read_bytes() for path in (record.root / ".deployments").rglob("*.json")
                 if path != record.journal_path}

    result = record.refresh()

    assert result == {"status": "refreshed", "deployment_id": result["deployment_id"],
                      "added": {"memory_reservation": RESERVATION}, "container_replaced": False,
                      "polling_health": "unconfirmed"}
    journal = record.journal()
    assert (journal["action"], journal["status"], journal["schema_version"]) == (
        "refresh-runtime-options", "committed", 4)
    assert journal["before"]["manifest"] == old_manifest
    archive = record.root / journal["predecessor"]["archive_path"]
    assert archive.read_bytes() == predecessor_raw
    assert {path: path.read_bytes() for path in old_files} == old_files
    product = runtime.inspect_deployment(record.root)["manifest"]
    assert product["components"]["telegram"] == old_manifest["components"]["telegram"]
    assert (record.root / ".deploy_current").read_text() == result["deployment_id"] + "\n"
    _assert_gap_closed(monkeypatch, record, OLD_ID, {"memory_reservation": RESERVATION})


def test_compose_recreated_container_records_both_options(deployment, monkeypatch) -> None:
    """`docker update` cannot set the OOM score, so recording both means a new container."""
    record = deployment()
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    with pytest.raises(operations.TransactionError):
        record.strict_terminal()

    result = record.refresh(NEW_ID)

    assert result["added"] == {"memory_reservation": RESERVATION, "oom_score_adj": OOM_SCORE}
    assert result["container_replaced"] is True
    journal = record.journal()
    assert journal["before"]["containers"]["api"]["id"] == OLD_ID
    assert journal["target"]["containers"]["api"]["id"] == NEW_ID
    _assert_gap_closed(monkeypatch, record, NEW_ID, result["added"])


def test_second_run_is_a_no_op(deployment) -> None:
    record = deployment()
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    first = record.refresh(NEW_ID)
    settled = record.authority()
    recipes = sorted((record.root / ".deployments" / "product-recipes").iterdir())

    assert record.refresh(NEW_ID) == {"status": "unchanged", "deployment_id": first["deployment_id"]}
    assert record.authority() == settled
    assert sorted((record.root / ".deployments" / "product-recipes").iterdir()) == recipes


def test_unchanged_adoption_writes_nothing(deployment) -> None:
    record = deployment()
    before = record.authority()
    assert record.refresh()["status"] == "unchanged"
    assert record.authority() == before


def _prepare_api_update(record: Deployment, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run real update preparation; only the delivery artifact itself is faked."""
    native = "sha256:" + "9" * 64
    record.revisions[native] = A
    old = runtime.inspect_deployment(record.root)["manifest"]["components"]["api"]
    source = {"revision_claim": A, "verified_commit": A, "verified_tree": B}

    def candidate(root, name, receipt, recipe):
        return ({"repository": "My-Denia/WuwaTerm", "version": old["version"], "source": source,
                 "core": {"version": old["version"], **source},
                 "image": {"ref": native, "id": native, "digest": native, "revision": A},
                 "recipe": recipe,
                 "provenance": {"kind": "completed-artifact", "bundle_sha256": H,
                                "acquisition_sha256": J, "authority": "local-source",
                                "native_image_id": native}}, {"Id": native})

    monkeypatch.setattr(prepare, "_artifact_candidate", candidate)
    monkeypatch.setattr(pm, "_verify_artifacts", lambda *args: None, raising=True)
    monkeypatch.setattr(prepare.manifest, "_verify_artifacts", lambda *args: None, raising=True)
    return prepare.prepare_update(record.root, {"selection": "api", "artifacts": {"api": "/receipt"},
                                                "recipes": {}}, COMPOSE)


def test_update_preparation_is_refused_before_and_accepted_after_the_refresh(deployment, monkeypatch) -> None:
    record = deployment(telegram=False)
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    with pytest.raises(prepare.PrepareError):
        _prepare_api_update(record, monkeypatch)

    record.refresh(NEW_ID)
    journal = _prepare_api_update(record, monkeypatch)

    assert journal["action"] == "update" and journal["before"]["containers"]["api"]["id"] == NEW_ID
    target = operations._snapshot(record.root, journal["target"]["manifest"]["components"]["api"])
    assert target["runtime_options"]["memory_reservation"] == RESERVATION
    assert target["runtime_options"]["oom_score_adj"] == OOM_SCORE

    refresh_raw = record.journal_path.read_bytes()
    transaction.write_initial_journal(record.root, journal, predecessor_raw=refresh_raw, compose_path=COMPOSE)
    assert record.journal()["transaction_id"] == journal["transaction_id"]
    assert (record.root / journal["predecessor"]["archive_path"]).read_bytes() == refresh_raw
    __import__("component_handoff").verify_predecessor(record.root, successor_kind="runtime", successor=journal)


@pytest.mark.parametrize("telegram", [False, True])
def test_full_update_route_accepts_the_refresh_record(deployment, telegram) -> None:
    record = deployment(telegram=telegram)
    record.apply_options(OLD_ID, memory_reservation=RESERVATION)
    record.refresh()

    assert terminal_ops.verify_terminal_for_full4(record.root, compose_path=COMPOSE) is not None


def _commit_full_update(record: Deployment) -> dict:
    """Leave a committed schema-3 full update as the terminal record."""
    import hashlib
    import shutil

    import component_full_db as full_db
    import component_journal_full as full

    previous = copy.deepcopy(record.adoption["target"]["manifest"])
    database_path = Path(previous["database"]["path"])
    predecessor_raw = record.journal_path.read_bytes()
    reference = runtime.predecessor_reference("runtime", previous["deployment_id"], predecessor_raw)
    archive = record.root / reference["archive_path"]
    archive.parent.mkdir()
    archive.write_bytes(predecessor_raw)
    archive.chmod(0o400)
    record.journal_path.unlink()

    identifier = "20261107T080000Z-" + "5" * 32
    target = copy.deepcopy(previous)
    target["deployment_id"] = identifier
    candidate_path = f".deployments/component-full-candidates/{identifier}.db"
    candidate = {**previous["database"], "path": str(record.root / candidate_path)}
    permissions = full_db.permissions(database_path)
    stat = pm.verify_database_binding(previous["database"])
    old_hash = previous["database"]["sha256"]
    backup_path = (f"data/deployment-backups/terms.{identifier[:16]}-{int(identifier[17:], 16)}."
                   f"{old_hash}.db")
    backup = record.root / backup_path
    backup.parent.mkdir(parents=True)
    shutil.copy2(database_path, backup)
    stage = str(database_path.parent / f".component-full-promote-{identifier}.db")
    restore = str(database_path.parent / f".component-full-restore-{identifier}.db")
    containers = copy.deepcopy(record.adoption["target"]["containers"])
    pointers = {
        "old": copy.deepcopy(record.adoption["pointers"]["target"]),
        "target": {".deploy_commit": {"present": True, "bytes": A + "\n"},
                   ".deploy_current": {"present": True, "bytes": identifier + "\n"}},
    }
    value = {
        "schema_version": 3, "kind": "component-full", "transaction_id": identifier,
        "action": "update", "status": "committed", "phase": "committed",
        "source": copy.deepcopy(record.source), "predecessor": reference, "selection": [],
        "before": {"manifest": previous, "topology": copy.deepcopy(previous["components"]),
                   "containers": containers},
        "target": {"manifest": target, "containers": copy.deepcopy(containers)},
        "database": {
            "old": {"binding": previous["database"], "stat": stat, "permissions": permissions},
            "candidate": {"path": candidate_path, "binding": candidate, "stat": stat,
                          "verification": copy.deepcopy(full._VERIFY)},
            "backup": {"path": backup_path, "sha256": old_hash, "permissions": permissions,
                       "stat": pm.verify_database_binding({**previous["database"], "path": str(backup)})},
            "stage": {"path": stage, "sha256": old_hash, "stat": stat, "permissions": permissions},
            "restore": {"path": restore, "sha256": old_hash, "stat": None, "permissions": permissions},
            "promoted_stat": stat, "restored_stat": None,
        },
        "api_identity": previous["api_identity"], "migrations": {"status": "not-applicable", "items": []},
        "pointers": pointers,
        "manifest": {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                     "sha256": hashlib.sha256(pm._encode(target)).hexdigest()},
    }
    full.validate_journal(value)
    pm.write_manifest(record.root, target)
    (record.root / ".deployments" / admission.COMPONENT_FULL).write_bytes(pm._encode(value))
    for name, pointer in pointers["target"].items():
        (record.root / name).write_text(pointer["bytes"], encoding="ascii")
    return value


def test_record_from_a_full_update_is_refreshed_and_handed_over(deployment, monkeypatch) -> None:
    record = deployment(telegram=False)
    full_record = _commit_full_update(record)
    full_path = record.root / ".deployments" / admission.COMPONENT_FULL
    full_raw = full_path.read_bytes()
    assert record.strict_terminal()[1]["kind"] == "component-full"
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    with pytest.raises(Exception, match="cannot be verified"):
        record.strict_terminal()

    result = record.refresh(NEW_ID)

    journal = record.journal()
    assert journal["predecessor"]["kind"] == "full"
    assert journal["predecessor"]["transaction_id"] == full_record["transaction_id"]
    assert (record.root / journal["predecessor"]["archive_path"]).read_bytes() == full_raw
    assert not full_path.exists()
    _assert_gap_closed(monkeypatch, record, NEW_ID, result["added"])


def test_record_from_a_completed_artifact_update_is_refreshed_as_schema_five(deployment, monkeypatch) -> None:
    record = deployment(telegram=False)
    update = _prepare_api_update(record, monkeypatch)
    transaction.write_initial_journal(record.root, update, predecessor_raw=record.journal_path.read_bytes(),
                                      compose_path=COMPOSE)
    # Finish the update the way the transaction does: replace, publish, commit.
    raw = record.containers.pop(OLD_ID)
    raw.update(Id=NEW_ID, Image="sha256:" + "9" * 64)
    raw["Config"]["Hostname"] = NEW_ID[:12]
    raw["Config"]["Labels"][observe.TRANSACTION_LABEL] = update["transaction_id"]
    record.containers[NEW_ID] = raw
    update["target"]["containers"]["api"].update(
        id=NEW_ID, name="wuwaterm-api", running=True, started_at="2026-11-07T00:00:00Z", restart_count=0)
    pm.write_manifest(record.root, update["target"]["manifest"])
    for name, value in update["pointers"]["target"].items():
        (record.root / name).write_text(value["bytes"], encoding="ascii")
    update.update(status="committed", phase="committed")
    transaction._write_journal(record.root, update)
    assert record.strict_terminal() is not None

    # The same container keeps its ownership label, which the refresh requires.
    record.apply_options(NEW_ID, memory_reservation=RESERVATION)
    record.refresh(NEW_ID)

    journal = record.journal()
    assert (journal["schema_version"], journal["target"]["manifest"]["schema_version"]) == (5, 3)
    assert journal["target"]["containers"]["api"]["transaction_label"] == update["transaction_id"]
    _assert_gap_closed(monkeypatch, record, NEW_ID, {"memory_reservation": RESERVATION})


def _set(path: str, value):
    def apply(record: Deployment) -> None:
        item = record.containers[NEW_ID]
        *parents, leaf = path.split(".")
        for key in parents:
            item = item[key]
        item[leaf] = value
    return apply


def _extra_environment(record: Deployment) -> None:
    record.containers[NEW_ID]["Config"]["Env"].append("WUWATERM_NEW_SETTING=1")


def _extra_container(record: Deployment) -> None:
    record.containers["1" * 64] = copy.deepcopy(record.containers[NEW_ID]) | {"Id": "1" * 64}


def _other_network(record: Deployment) -> None:
    raw = record.containers[NEW_ID]
    raw["HostConfig"]["NetworkMode"] = "none"
    raw["NetworkSettings"]["Networks"] = {"none": raw["NetworkSettings"]["Networks"].pop("host")}


def _writable_mount(record: Deployment) -> None:
    record.containers[NEW_ID]["Mounts"][0].update(RW=True, Mode="rw")


def _ownership_label(record: Deployment) -> None:
    record.containers[NEW_ID]["Config"]["Labels"][observe.TRANSACTION_LABEL] = (
        "20261107T000000Z-" + "7" * 32)


@pytest.mark.parametrize("drift,reason", [
    (_set("Image", "sha256:" + "8" * 64), "identity does not match"),
    (_set("Config.Cmd", ["bot"]), "command or entrypoint"),
    (_set("Config.User", "0:0"), "recorded settings differ: user"),
    (_set("Config.WorkingDir", "/srv"), "recorded settings differ: working_dir"),
    (_writable_mount, "recorded settings differ: mounts"),
    (_other_network, "recorded settings differ: network_mode"),
    (_set("HostConfig.PortBindings", {"8788/tcp": [{"HostIp": "", "HostPort": "8788"}]}),
     "recorded settings differ: ports"),
    (_set("HostConfig.RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
     "recorded settings differ: restart"),
    (_set("HostConfig.ReadonlyRootfs", True), "recorded settings differ: runtime_options.read_only_root"),
    (_set("HostConfig.CapAdd", ["NET_ADMIN"]), "recorded settings differ: runtime_options.cap_add"),
    (_set("HostConfig.Memory", 1 << 28), "resource settings cannot be replayed"),
    (_extra_environment, "environment no longer matches"),
    (_extra_container, "container inventory changed"),
    (_set("Name", "/renamed-api"), "differs from the record: name"),
    (_set("State.Running", False), "differs from the record: running"),
    (_ownership_label, "differs from the record: transaction_label"),
    (_set("HostConfig.MemoryReservation", -1), "resource settings cannot be replayed"),
    (_set("HostConfig.MemoryReservation", True), "resource settings cannot be replayed"),
    (_set("HostConfig.OomScoreAdj", 1001), "resource settings cannot be replayed"),
])
def test_any_other_drift_is_refused_without_changing_the_record(deployment, drift, reason) -> None:
    record = deployment()
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    drift(record)
    before = record.authority()

    with pytest.raises(JournalError, match=reason):
        record.refresh(NEW_ID)
    assert record.authority() == before


@pytest.mark.parametrize("field", ["MemoryReservation", "OomScoreAdj"])
def test_telegram_resource_settings_stay_refused(deployment, field) -> None:
    record = deployment()
    record.apply_options(OLD_ID, memory_reservation=RESERVATION)
    record.containers[TG_ID]["HostConfig"][field] = RESERVATION if field == "MemoryReservation" else OOM_SCORE
    before = record.authority()

    with pytest.raises(JournalError, match="observed container recipe differs"):
        record.refresh()
    assert record.authority() == before


@pytest.mark.parametrize("live", [{"memory_reservation": RESERVATION * 2, "oom_score_adj": OOM_SCORE},
                                  {"oom_score_adj": OOM_SCORE},
                                  {"memory_reservation": RESERVATION, "oom_score_adj": -100}])
def test_a_recorded_option_cannot_be_changed_or_dropped(deployment, live) -> None:
    record = deployment(recorded_options={"memory_reservation": RESERVATION, "oom_score_adj": OOM_SCORE})
    record.compose_recreate(**live)
    before = record.authority()

    with pytest.raises(JournalError, match="recorded settings differ: runtime_options"):
        record.refresh(NEW_ID)
    assert record.authority() == before


def test_replaced_container_without_a_new_option_is_not_rebound(deployment) -> None:
    record = deployment()
    record.compose_recreate()
    before = record.authority()

    with pytest.raises(JournalError, match="does not rebind"):
        record.refresh(NEW_ID)
    assert record.authority() == before


def test_unadopted_root_is_refused(deployment) -> None:
    record = deployment()
    (record.root / ".deploy_current").unlink()
    with pytest.raises(JournalError, match="adopted and reconciled|pointer"):
        record.refresh()


@pytest.mark.parametrize("failing", [".deploy_commit", ".deploy_current"])
def test_publication_failure_restores_pointers_and_can_be_retried(deployment, monkeypatch, failing) -> None:
    record = deployment()
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    pointers = record.authority()[1:3]
    original = adoption._replace_pointer

    def fail(root, name, expected, desired):
        if name == failing:
            raise OSError("injected")
        original(root, name, expected, desired)

    monkeypatch.setattr(adoption, "_replace_pointer", fail)
    with pytest.raises(JournalError, match="original pointers were restored"):
        record.refresh(NEW_ID)
    assert record.journal()["status"] == "rolled_back"
    assert record.authority()[1:3] == pointers
    # The drift is still there, so strict verification keeps saying so.
    with pytest.raises((operations.TransactionError, observe.ObservationError)):
        record.strict_terminal()

    monkeypatch.setattr(adoption, "_replace_pointer", original)
    assert record.refresh(NEW_ID)["status"] == "refreshed"
    assert record.strict_terminal() is not None


class _Killed(BaseException):
    """Stands for a killed process: no inline rollback runs."""


def test_interrupted_refresh_blocks_new_work_until_recovered(deployment, monkeypatch) -> None:
    record = deployment()
    record.apply_options(OLD_ID, memory_reservation=RESERVATION)
    pointers = record.authority()[1:3]
    original = adoption._replace_pointer

    def die(root, name, expected, desired):
        if name == ".deploy_current":
            raise _Killed
        original(root, name, expected, desired)

    monkeypatch.setattr(adoption, "_replace_pointer", die)
    with pytest.raises(_Killed):
        record.refresh()
    journal = record.journal()
    assert (journal["status"], journal["phase"]) == ("active", "deploy-current")
    assert (record.root / ".deploy_commit").read_bytes() == pointers[0]
    with pytest.raises(JournalError, match="admission refused"):
        record.refresh()
    inspected = runtime.inspect_deployment(record.root)
    assert inspected["recovery"]["argv"][inspected["recovery"]["argv"].index("--kind") + 1] == "runtime"

    recovered = transaction._recover_adoption(record.root, journal, COMPOSE)

    assert recovered["status"] == "rolled_back"
    assert record.authority()[1:3] == pointers
    monkeypatch.setattr(adoption, "_replace_pointer", original)
    assert record.refresh()["status"] == "refreshed"


def _forge(change):
    def apply(journal: dict) -> None:
        change(journal)
        journal["manifest"]["sha256"] = __import__("hashlib").sha256(
            pm._encode(journal["target"]["manifest"])).hexdigest()
    return apply


@pytest.mark.parametrize("change,reason", [
    (lambda j: j["target"]["manifest"]["components"]["api"].update(version="9.9.9", core={
        **j["target"]["manifest"]["components"]["api"]["core"], "version": "9.9.9"}),
     "only the API recipe snapshot"),
    (lambda j: j["target"]["containers"]["api"].update(name="other"), "container binding"),
    (lambda j: j["target"]["containers"]["api"].update(transaction_label=j["transaction_id"]),
     "ownership label"),
    (lambda j: j["target"]["containers"]["telegram"].update(restart_count=7), "unselected component"),
    (lambda j: j.update(selection=["api"]), "action and selection"),
    (lambda j: j.update(predecessor=None), "predecessor"),
    (lambda j: j.update(schema_version=5), "schema"),
])
def test_refresh_journal_cannot_carry_another_change(deployment, change, reason) -> None:
    record = deployment()
    record.compose_recreate(memory_reservation=RESERVATION, oom_score_adj=OOM_SCORE)
    record.refresh(NEW_ID)
    journal = record.journal()
    _forge(change)(journal)

    with pytest.raises(JournalError, match=reason):
        runtime.validate_journal(journal)


def test_terminal_verification_rejects_a_snapshot_that_changes_more_than_resources(deployment) -> None:
    """The journal parser cannot read snapshots, so terminal verification does."""
    record = deployment()
    record.apply_options(OLD_ID, memory_reservation=RESERVATION)
    record.refresh()
    journal = record.journal()
    snapshot = record.api_snapshot()
    snapshot["runtime_options"]["dns"] = ["127.0.0.1"]
    path, digest = pm.write_recipe_snapshot(record.root, snapshot)
    journal["target"]["manifest"]["components"]["api"]["recipe"].update(snapshot_path=path, sha256=digest)
    with pytest.raises(operations.TransactionError, match="runtime_options.dns"):
        operations._verify_refresh_delta(record.root, journal)


def test_wrapper_runs_the_refresh_only_under_the_deployment_lock(tmp_path: Path) -> None:
    directory = tmp_path / ".deployments"
    directory.mkdir()
    with (directory / ".deployment.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = subprocess.run(
            ["sh", str(ROOT / "deploy" / "product-update.sh"), "--root", str(tmp_path.resolve()),
             "refresh-runtime-options", "--container-id", OLD_ID, "--compose-executable", "/unused"],
            capture_output=True, text=True, check=False, timeout=60, env={**os.environ})
    assert result.returncode == 75 and "already in progress" in result.stderr
