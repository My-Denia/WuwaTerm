from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import component_full as full

import component_transaction as transaction


OLD_ID = "a" * 64
TARGET_ID = "b" * 64
OLD_IMAGE = "sha256:" + "1" * 64
TARGET_IMAGE = "sha256:" + "2" * 64


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    env_file = tmp_path / "operator.env"
    env_file.write_text("FROM_FILE=operator-file\nWUWATERM_API_PORT=9000\nOPERATOR_VALUE=fixture-value\n")
    old_snapshot = {
        "image_id": OLD_IMAGE, "project": "project", "service": "api", "container_name": "api",
        "working_dir": None,
        "env_files": [str(env_file)], "environment_overrides": [
            {"name": "WUWATERM_API_PORT", "mode": "literal", "value": "9100"},
        ],
    }
    target_snapshot = {**old_snapshot, "image_id": TARGET_IMAGE}
    images = {
        OLD_IMAGE: {"Config": {"Env": ["PYTHON_VERSION=3.11", "PYTHON_SHA256=old-sha", "OLD_ONLY=old",
                                        "FROM_FILE=old-default", "WUWATERM_API_PORT=8000"],
                               "Labels": {"org.opencontainers.image.revision": "old-revision"}}},
        TARGET_IMAGE: {"Config": {"Env": ["PYTHON_VERSION=3.12", "PYTHON_SHA256=new-sha", "NEW_ONLY=new",
                                           "FROM_FILE=new-default", "WUWATERM_API_PORT=8001"]}},
    }
    old_env = {"PYTHON_VERSION": "3.11", "PYTHON_SHA256": "old-sha", "OLD_ONLY": "old",
               "FROM_FILE": "operator-file", "WUWATERM_API_PORT": "9100", "OPERATOR_VALUE": "fixture-value"}
    target_env = {"PYTHON_VERSION": "3.12", "PYTHON_SHA256": "new-sha", "NEW_ONLY": "new",
                  "FROM_FILE": "operator-file", "WUWATERM_API_PORT": "9100", "OPERATOR_VALUE": "fixture-value"}

    # Stub the Compose process boundary, not the function combining the image,
    # rendered env files and recorded explicit overrides.
    def compose(_args, *, input, **_kwargs):
        service = json.loads(input)["services"]["fixture"]
        environment = {}
        for name in service["env_file"]:
            for line in Path(name).read_text().splitlines():
                key, value = line.split("=", 1)
                environment[key] = value
        environment.update(service["environment"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"services": {"fixture": {
            "environment": environment}}}).encode())

    monkeypatch.setattr(transaction.admission, "require_lock", lambda *_: None)
    monkeypatch.setattr(transaction, "_compose_binary", lambda path: path)
    monkeypatch.setattr(transaction.subprocess, "run", compose)
    monkeypatch.setattr(transaction, "_image", lambda image_id: images[image_id])
    before = {"id": OLD_ID, "image_id": OLD_IMAGE, "running": False, "started_at": "old-start",
              "restart_count": 0, "project": "project", "service": "api", "name": "api",
              "transaction_label": None}
    target = {**before, "id": TARGET_ID, "image_id": TARGET_IMAGE,
              "transaction_label": "transaction"}
    old_component = {"which": "old", "image": {"revision": "old-revision"}}
    target_component = {"which": "target"}
    journal = {
        "action": "update", "status": "active", "phase": "prepared", "transaction_id": "transaction",
        "selection": ["api"], "source": {}, "api_identity": None,
        "before": {"manifest": None, "topology": {"api": old_component, "telegram": None},
                   "containers": {"api": before, "telegram": None}},
        "target": {"manifest": {"components": {"api": target_component, "telegram": None}},
                   "containers": {"api": {**target, "id": None, "started_at": None}, "telegram": None}},
    }
    raw = {OLD_ID: {"Config": {"Env": [f"{k}={v}" for k, v in old_env.items()], "Labels": {}},
                    "HostConfig": {"NetworkMode": "host"}},
           TARGET_ID: {"Config": {"Env": [f"{k}={v}" for k, v in target_env.items()]}}}
    monkeypatch.setattr(transaction.observe, "_inspect", lambda identifier: raw[identifier])
    monkeypatch.setattr(transaction, "_snapshot", lambda _root, component:
                        old_snapshot if component["which"] == "old" else target_snapshot)
    return {"journal": journal, "old_snapshot": old_snapshot, "target_snapshot": target_snapshot,
            "old_env": old_env, "target_env": target_env, "raw": raw, "target": target}


class StopAtCreate(Exception):
    pass


@pytest.mark.parametrize("route", ["runtime-api", "runtime-both", "full-api", "full-both"])
def test_replacement_payload_uses_target_environment_on_each_caller_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deployment: dict, route: str,
) -> None:
    journal = deployment["journal"]
    if route.endswith("both"):
        journal["selection"] = ["api", "telegram"]
        journal["before"]["topology"]["telegram"] = {"which": "target"}
        journal["before"]["containers"]["telegram"] = {"id": "c" * 64, "running": False}
        journal["target"]["manifest"]["components"]["telegram"] = {"which": "target"}
        journal["target"]["containers"]["telegram"] = {"id": None}
    # Public API updates keep the recorded recipe; image defaults may change.
    payloads = []

    def docker(method, path, body=None, **_kwargs):
        assert method == "POST" and path.startswith("/containers/create?")
        payloads.append(copy.deepcopy(body))
        raise StopAtCreate

    monkeypatch.setattr(transaction, "_docker", docker)
    if route.startswith("full"):
        monkeypatch.setattr(full, "_all_readers_stopped", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(full, "_guard_readers", lambda *_args, **_kwargs: {
            "api": {"target": None, "target_snapshot": deployment["target_snapshot"]}})
        with pytest.raises(StopAtCreate):
            full._activate_readers(tmp_path, journal, compose_path=Path("/compose"))
    else:
        monkeypatch.setattr(transaction.admission, "_physical_root", lambda root: root)
        monkeypatch.setattr(transaction.admission, "admit_component", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(transaction.runtime, "validate_journal", lambda value: value)
        for name in ("_verify_candidate", "_require_prepared_source", "_verify_old_topology",
                     "_require_target_manifest_absent", "write_initial_journal", "_step", "_rename",
                     "_container_command", "_write_journal", "_recover_pending_telegram",
                     "_refresh_telegram_state"):
            monkeypatch.setattr(transaction, name, lambda *_args, **_kwargs: None)
        monkeypatch.setattr(transaction, "_prepare_predecessor", lambda *_args: None)
        monkeypatch.setattr(transaction, "_telegram_context", lambda *_args: {})
        monkeypatch.setattr(transaction, "_probe_telegram_state", lambda *_args: [])
        monkeypatch.setattr(transaction, "_actual", lambda *_args, **_kwargs:
                            ({"container": journal["before"]["containers"]["api"]}, None))
        monkeypatch.setattr(transaction, "_actual_telegram", lambda *_args, **_kwargs:
                            ({"container": journal["before"]["containers"]["telegram"]}, None))
        with pytest.raises(transaction.TransactionError) as exc:
            transaction.update(tmp_path, journal, compose_path=Path("/compose"))
        assert isinstance(exc.value.__cause__, StopAtCreate)

    expected = dict(deployment["target_env"])
    assert len(payloads) == 1
    assert payloads[0]["Image"] == TARGET_IMAGE
    assert transaction._env_map(payloads[0]["Env"]) == expected
    assert transaction._env_map(deployment["raw"][OLD_ID]["Config"]["Env"]) == deployment["old_env"]


@pytest.mark.parametrize("drift", ["none", "old", "target"])
def test_runtime_reconciliation_checks_each_container_against_its_own_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deployment: dict, drift: str,
) -> None:
    journal = deployment["journal"]
    journal["phase"] = "selected-activate"
    journal["target"]["containers"]["api"]["id"] = TARGET_ID
    if drift != "none":
        identifier = OLD_ID if drift == "old" else TARGET_ID
        deployment["raw"][identifier]["Config"]["Env"].append("UNEXPECTED=drift")
    records = {OLD_ID: journal["before"]["containers"]["api"], TARGET_ID: deployment["target"]}

    def observed(identifier, _snapshot, key):
        env = deployment["raw"][identifier]["Config"]["Env"]
        _, fingerprint = transaction.observe._environment(env, None, key)
        return {"container": records[identifier], "environment_fingerprint": fingerprint}

    monkeypatch.setattr(transaction, "_observed", observed)
    monkeypatch.setattr(transaction, "_recipe_matches", lambda *_args: None)
    monkeypatch.setattr(transaction, "_inventory", lambda *_args: [
        {"Id": OLD_ID}, {"Id": TARGET_ID, "Labels": {transaction.observe.TRANSACTION_LABEL: "transaction"}}])
    monkeypatch.setattr(transaction, "_verify_data", lambda *_args: None)
    monkeypatch.setattr(transaction, "_other_component", lambda *_args: None)
    monkeypatch.setattr(transaction, "_check_pointers", lambda *_args, **_kwargs: None)
    if drift == "none":
        old, target = transaction._actual(tmp_path, journal, Path("/compose"))
        assert old["container"]["id"] == OLD_ID and target["container"]["id"] == TARGET_ID
    else:
        message = "original container" if drift == "old" else "target effective environment differs"
        with pytest.raises(transaction.TransactionError, match=message):
            transaction._actual(tmp_path, journal, Path("/compose"))
