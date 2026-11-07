"""Real-image checks for the container-identity state probe.

The explicit Docker test builds a registered product image in a task-owned Git
fixture. It retains the labelled setup container and producer evidence for the
owner's separate resource-cleanup preflight.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest

from scripts import component_prepare as prepare
from scripts import component_state as state
from scripts import component_wheel_build as wheel
from scripts import product_manifest as manifest
from test_component_runtime import journal as journal_fixture
from test_component_wheel_build import _invoke, _ops_fixture
from test_product_manifest import make_database


@pytest.mark.skipif(os.geteuid() != 0 or sys.platform != "linux", reason="requires a root-owned cross-UID fixture")
def test_canonical_probe_reads_other_uid_file_after_noatime_eperm() -> None:
    """Exercise the real kernel EPERM path and the installed legacy reader."""
    gid = Path(state.__file__).resolve().parents[1].stat().st_gid
    uid = 65534

    def drop_uid() -> None:
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)

    with tempfile.TemporaryDirectory(prefix="wuwaterm-canonical-uid-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o777)
        source = root / "settings.json"
        source.write_bytes(b'{"public":{"71001":true},"allowed":[71001]}')
        source.chmod(0o644)
        assert source.stat().st_uid == 0 and source.stat().st_uid != uid
        scratch = root / "scratch"
        scratch.mkdir(mode=0o777)
        scratch.chmod(0o777)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        check = ("import errno,os,sys; "
                 "exec(\"try:\\n os.open(sys.argv[1],os.O_RDONLY|os.O_NOATIME)\\n"
                 "except OSError as e:\\n print(e.errno)\\nelse:\\n print(0)\")")
        python = "/usr/bin/python3"
        denied = subprocess.run([python, "-I", "-B", "-c", check, str(source)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=drop_uid, timeout=30, check=False)
        assert denied.returncode == 0, denied.stderr
        assert int(denied.stdout.strip()) == errno.EPERM
        site = Path(sys.executable).parent.parent / "lib/python3.11/site-packages"
        code = "import sys; sys.path.insert(0," + repr(str(site)) + ")\n" + state._CANONICAL_CODE
        command = [python, "-I", "-B", "-c", code, str(source), f"{uid}:{gid}",
                   "[]", "legacy", "chat-settings", "123", str(scratch)]
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=drop_uid, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        actual = json.loads(result.stdout)
        assert actual["raw_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert actual["canonical_sha256"] is not None and actual["writer_can_replace"] is True
        source.chmod(0o000)
        unreadable = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    preexec_fn=drop_uid, timeout=30, check=False)
        assert unreadable.returncode != 0 and unreadable.stdout == ""


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", "--host", "unix:///run/docker.sock", *args],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _stat(path: Path) -> tuple[int, ...]:
    value = path.stat(follow_symlinks=False)
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_atime_ns)


def _component(root: Path, receipt: dict, digest: str, data: Path, state_dir: Path, user: str,
               name: str, project: str, *, read_only: bool = False, state_env: str = "/app/state") -> tuple[dict, dict]:
    source = {"revision_claim": receipt["source"]["commit"], "verified_commit": receipt["source"]["commit"],
              "verified_tree": receipt["source"]["tree"]}
    core = {"version": receipt["core_version"], "revision_claim": receipt["core_source"]["commit"],
            "verified_commit": receipt["core_source"]["commit"], "verified_tree": receipt["core_source"]["tree"]}
    snapshot = {
        "schema_version": 1, "image_id": receipt["image_id"], "project": project,
        "service": "wuwaterm-telegram", "container_name": name,
        "command": ["--db", "/app/data/terms.db"], "entrypoint": ["wuwaterm-telegram"],
        "mounts": [{"host": str(data), "container": "/app/data", "read_only": True},
                   {"host": str(state_dir), "container": "/app/state", "read_only": read_only}],
        "env_files": [], "network_mode": "none", "ports": [],
        "restart": {"name": "no", "maximum_retry_count": 0}, "user": user, "working_dir": "/app",
        "environment_overrides": [
            {"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/app/data/terms.db"},
            {"name": "WUWATERM_STATE_DIR", "mode": "literal", "value": state_env},
        ],
        "runtime_options": {"read_only_root": False, "privileged": False, "init": None,
                            "pid_mode": "", "ipc_mode": "private", "cap_add": [], "cap_drop": [],
                            "security_opt": [], "dns": [], "extra_hosts": []},
    }
    path, sha = manifest.write_recipe_snapshot(root, snapshot)
    recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
    recipe.update(snapshot_path=path, sha256=sha)
    provenance = {key: receipt[key] for key in ("telegram_wheel_sha256", "core_wheel_sha256",
                                                 "core_lock_sha256", "core_producer_kind")}
    provenance.update(kind="wheel-image", build_receipt_sha256=digest)
    result = {"repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
              "source": source, "core": core,
              "image": {"ref": receipt["image_ref"], "id": receipt["image_id"],
                        "digest": receipt["image_id"], "revision": receipt["image_revision"]},
              "recipe": recipe, "provenance": provenance}
    return result, snapshot


def _fixture_journal(root: Path, receipt: dict, digest: str, data: Path, state_dir: Path, user: str,
                     name: str, project: str, container_id: str, started_at: str) -> tuple[dict, dict]:
    journal = journal_fixture.__wrapped__(root)
    component, snapshot = _component(root, receipt, digest, data, state_dir, user, name, project)
    database = make_database(data / "terms.db")
    source = receipt["recovery_source"]
    identifier = journal["transaction_id"]
    before = {"id": container_id, "image_id": receipt["image_id"], "running": False, "started_at": started_at,
              "restart_count": 0, "project": snapshot["project"], "service": snapshot["service"],
              "name": snapshot["container_name"], "transaction_label": None}
    after = {**before, "id": None, "name": None, "restart_count": None, "transaction_label": identifier}
    journal.update(source=source, selection=["telegram"], api_identity=None,
                   database={"binding": database, "stat": manifest.verify_database_binding(database)})
    journal["before"] = {"manifest": None, "topology": {"api": None, "telegram": component},
                         "containers": {"api": None, "telegram": before}}
    target = journal["target"]["manifest"]
    target["operations"].update(source_commit=source["commit"], source_tree=source["tree"],
                                source_tool_sha256=source["source_tool_sha256"])
    target.update(components={"api": None, "telegram": copy.deepcopy(component)}, database=database, api_identity=None)
    journal["target"]["containers"] = {"api": None, "telegram": after}
    journal["pointers"]["old"][".deploy_commit"] = {"present": False, "bytes": None}
    journal["pointers"]["target"][".deploy_commit"]["bytes"] = source["commit"] + "\n"
    raw = (json.dumps(target, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    journal["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return journal, snapshot


def _locked_code(root: Path, code: str, *args: str) -> subprocess.CompletedProcess[str]:
    shell = 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"'
    command = ["bash", "-c", shell, "state-fixture", str(root), sys.executable, "-I", "-B", "-c",
               "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + code,
               str(root), *args]
    return subprocess.run(command, cwd=root, env={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def test_full4_endpoint_probe_preserves_request_and_rejects_unbound_extras(tmp_path: Path) -> None:
    from test_component_state_endpoint import captured, request

    namespace = tmp_path / "namespace"
    namespace.mkdir()
    target = namespace / "settings"
    target.write_bytes(b"fixture state")
    request_data = request(namespace, target)
    endpoint = captured(request_data)
    identifier = "20261106T110000Z-" + "4" * 32
    output = (json.dumps(endpoint, sort_keys=True) + "\n").encode()
    probe = {"schema_version": 4, "kind": "state-endpoint", "probe_id": identifier,
             "status": "completed", "phase": "completed", "transaction_id": identifier,
             "source_tool_sha256": "a" * 64, "daemon_id": "b" * 64,
             "image_id": request_data["image_id"], "image_revision": request_data["image_revision"],
             "container_name": "wuwaterm-state-probe-" + identifier, "container_id": "c" * 64,
             "user": request_data["user"], "working_dir": request_data["working_dir"],
             "mounts": request_data["mounts"], "path_settings": dict.fromkeys(state._PATH_KEYS),
             "input_path": request_data["effective_path"], "command": None,
             "output_sha256": hashlib.sha256(output).hexdigest(), "result": endpoint, "failure": None,
             "transaction_kind": "full", "flavor": None, "state_kind": "chat-settings", "clock": None,
             "endpoint_request": request_data, "endpoint_purpose": "target", "extra_read_binds": []}
    probe["command"] = state._command(probe)
    directory = tmp_path / ".deployments/component-adopt-probes"
    directory.mkdir(parents=True)
    path = directory / (identifier + ".json")
    path.write_text(json.dumps(probe), encoding="utf-8")
    assert state.read_state_probe(tmp_path, path.name) == probe
    for change in (
        lambda item: item["endpoint_request"].update(image_id="sha256:" + "d" * 64),
        lambda item: item.update(extra_read_binds=[{"host_parent": "/unbound"}]),
        lambda item: item.update(endpoint_purpose="unknown"),
        lambda item: item["result"]["referent"].update(sha256="d" * 64),
    ):
        altered = copy.deepcopy(probe)
        change(altered)
        path.write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(state.StateError):
            state.read_state_probe(tmp_path, path.name)


def test_full4_canonical_disjoint_source_is_exact_read_only_parent(tmp_path: Path) -> None:
    from test_component_state_endpoint import captured, request

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    source = old / "replies"
    source.write_bytes(b"fixture data")
    payload = request(old, source, kind="channel-replies")
    payload["mounts"][0]["host"] = str(old)
    endpoint = captured(payload)
    snapshot = {"mounts": [{"host": str(new), "container": "/app/new-state", "read_only": False}]}
    identifier = "20261106T110000Z-" + "5" * 32
    path, mounts, extras = state._canonical_mounts(snapshot, identifier, endpoint)
    assert len(extras) == 1 and extras[0]["read_only"] is True
    assert extras[0]["host_parent"] == str(old) and extras[0]["parent_inode"] == old.stat().st_ino
    assert path == extras[0]["probe_alias"] + "/replies"
    added = next(item for item in mounts if item["container"] == extras[0]["probe_alias"])
    assert added["host"] == str(old) and added["read_only"] is True
    old.chmod(0o755 if old.stat().st_mode & 0o777 == 0o700 else 0o700)
    with pytest.raises(state.StateError, match="parent changed"):
        state._canonical_mounts(snapshot, identifier, endpoint)


def test_rollback_reservation_is_allowed_but_new_forward_reservation_is_not(tmp_path: Path) -> None:
    from test_component_state_copy_v4 import accepted, capture, setup_copy

    backward = tmp_path / "backward"
    backward.mkdir()
    rollback, stage, _, _ = setup_copy(backward, direction="rollback")
    observed = capture(stage, "chat-settings", rollback["mounts"])
    state._check_reservation_boundary({"phase": "rollback-copyback"}, "rollback", observed)
    reserved, _ = accepted(rollback)
    assert stage.is_file() and reserved["endpoint"]["referent"]["stat"]["inode"] == stage.stat().st_ino
    forward_root = tmp_path / "forward"
    forward_root.mkdir()
    forward, forbidden, _, _ = setup_copy(forward_root, direction="forward")
    absent = capture(forbidden, "chat-settings", forward["mounts"])
    with pytest.raises(state.StateError, match="new forward"):
        state._check_reservation_boundary({"phase": "rollback-copyback"}, "forward", absent)
    assert not forbidden.exists()


def test_full4_native_plan_probe_retains_its_recipe_binding(tmp_path: Path) -> None:
    identifier = "20261106T110000Z-" + "6" * 32
    root = tmp_path / "mount"
    root.mkdir()
    info = root.stat()
    record = {"schema_version": 4, "kind": "state-legacy-plan", "probe_id": identifier,
              "status": "active", "phase": "prepared", "transaction_id": identifier,
              "source_tool_sha256": "a" * 64, "daemon_id": "b" * 64,
              "image_id": "sha256:" + "c" * 64, "image_revision": "d" * 40,
              "container_name": "wuwaterm-state-probe-" + identifier, "container_id": None,
              "user": "0:0", "working_dir": "/app",
              "mounts": [{"host": str(root), "container": "/app/data", "read_only": True,
                          "device": info.st_dev, "inode": info.st_ino}],
              "path_settings": dict.fromkeys(state._PATH_KEYS), "input_path": "/app/data/terms.db",
              "command": None, "output_sha256": None, "result": None, "failure": None,
              "transaction_kind": "full", "flavor": None, "state_kind": None, "clock": None,
              "plan_request": {"recipe_sha256": "e" * 64}, "extra_read_binds": []}
    record["command"] = state._command(record)
    directory = tmp_path / ".deployments/component-adopt-probes"
    directory.mkdir(parents=True)
    path = directory / (identifier + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    assert state.read_state_probe(tmp_path, path.name) == record
    record["plan_request"]["recipe_sha256"] = "bad"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(state.StateError):
        state.read_state_probe(tmp_path, path.name)


@pytest.mark.skipif(os.environ.get("WUWATERM_RUN_STATE_DOCKER") != "1", reason="explicit real Docker fixture only")
def test_registered_product_plan_and_root_owned_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest's private base is task-owned; allow the container UID to traverse it.
    tmp_path.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    root, commit, tree = _ops_fixture(tmp_path)
    root.chmod(0o755)
    data, state_dir = root / "state-fixture-data", root / "state-fixture-files"
    data.mkdir(mode=0o755)
    state_dir.mkdir(mode=0o777)
    state_dir.chmod(0o777)
    cfg = {key: os.environ["WUWATERM_STATE_" + key] for key in (
        "BUILD_PYTHON", "TG_REPO", "TG_COMMIT", "TG_TREE", "CORE_WHEEL", "CORE_ARTIFACT", "WHEELHOUSE")}
    monkeypatch.setenv("WUWATERM_WHEEL_BUILD_PYTHON", cfg["BUILD_PYTHON"])
    built = _invoke(root, commit, tree, Path(cfg["TG_REPO"]), cfg["TG_COMMIT"], cfg["TG_TREE"],
                    Path(cfg["CORE_WHEEL"]), Path(cfg["CORE_ARTIFACT"]), Path(cfg["WHEELHOUSE"]))
    assert built.returncode == 0, built.stderr
    digest = built.stdout.strip()
    receipt = wheel.verify_registered_wheel_build(root, digest)
    assert receipt["source"] == {"commit": "6ef554f5d840cf8757b56f6592c64d0914489015",
                                 "tree": "a4ea6e034361530cacefc762b27f7f7bb3ac2b33"}
    assert receipt["core_wheel_sha256"] == "95f9012154f5883f8414d6d32daf574e2e5d4de8f09b765a3678d0ae4e332853"
    name = "wuwaterm-state-fixture-" + uuid.uuid4().hex
    owner = uuid.uuid4().hex
    resources = {"owner": owner, "setup_container_name": name, "image_id": receipt["image_id"],
                 "build_id": receipt["build_id"]}
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    writer_code = "from pathlib import Path; p=Path('/fixture/channel_replies.json'); p.write_bytes(b'{}\\n'); p.chmod(0o600)"
    setup_id = _docker("create", "--name", name, "--network", "none", "--read-only", "--user", "0:0",
                       "--label", "org.wuwaterm.test.state-owner=" + owner,
                       "--mount", f"type=bind,src={state_dir},dst=/fixture", "--entrypoint", "python",
                       receipt["image_id"], "-I", "-B", "-c", writer_code)
    resources["setup_container_id"] = setup_id
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    assert _docker("start", "--attach", setup_id) == ""
    path = state_dir / "channel_replies.json"
    before = _stat(path)
    assert before[3] == 0 and before[2] & 0o777 == 0o600
    with pytest.raises(PermissionError):
        path.read_bytes()
    project, service_name = "state" + uuid.uuid4().hex[:16], "state-" + uuid.uuid4().hex
    resources.update(service_container_name=service_name, project=project)
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    service_id = _docker("create", "--name", service_name, "--network", "none", "--user", "0:0",
                         "--label", "org.wuwaterm.test.state-owner=" + owner,
                         "--label", "com.docker.compose.project=" + project,
                         "--label", "com.docker.compose.service=wuwaterm-telegram",
                         "--mount", f"type=bind,src={data},dst=/app/data,readonly",
                         "--mount", f"type=bind,src={state_dir},dst=/app/state",
                         "--env", "WUWATERM_DB_PATH=/app/data/terms.db", "--env", "WUWATERM_STATE_DIR=/app/state",
                         "--entrypoint", "wuwaterm-telegram", receipt["image_id"], "--db", "/app/data/terms.db")
    resources.update(service_container_id=service_id, service_container_name=service_name, project=project)
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    started_at = _docker("inspect", "--format", "{{.State.StartedAt}}", service_id)
    journal, snapshot = _fixture_journal(root, receipt, digest, data, state_dir, "0:0",
                                         service_name, project, service_id, started_at)
    input_path = root / "fixture-journal-input.json"
    input_path.write_text(json.dumps(journal))
    create = _locked_code(root, "import component_transaction as t,json; r=pathlib.Path(sys.argv[2]); "
                          "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); t._write_journal(r,j,initial=True)",
                          str(root), str(input_path))
    assert create.returncode == 0, create.stderr
    options = {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_STATE_DIR": "/app/state"}
    args = root / "fixture-arguments.json"
    args.write_text(json.dumps({"snapshot": snapshot, "env": options, "digest": digest,
                                "db": str(data / "terms.db"), "transaction": journal["transaction_id"],
                                "path": str(path), "image": receipt["image_id"], "revision": receipt["image_revision"]}))
    code = ("import component_state as s,json; root=pathlib.Path(sys.argv[2]); "
            "a=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
            "p=s.plan(root,receipt_sha256=a['digest'],snapshot=a['snapshot'],environment=a['env'],"
            "database_path=a['db'],transaction_id=a['transaction']); "
            "h=s.preimage(root,image_id=a['image'],image_revision=a['revision'],snapshot=a['snapshot'],"
            "host_path=a['path'],transaction_id=a['transaction']); print(json.dumps({'plan':p,'preimage':h}))")
    observed = _locked_code(root, code, str(root), str(args))
    assert observed.returncode == 0, observed.stderr
    result = json.loads(observed.stdout)
    assert result["plan"]["kinds"]["channel-replies"]["mapped_target"] == str(path)
    assert result["preimage"]["present"] and result["preimage"]["sha256"] == hashlib.sha256(b"{}\n").hexdigest()
    assert _stat(path) == before
    records = [json.loads(p.read_text()) for p in (root / ".deployments/component-adopt-probes").iterdir()]
    assert len(records) == 2 and all(r["status"] == "completed" for r in records)
    assert all(_docker("container", "ls", "-a", "--filter", "id=" + r["container_id"], "--format", "{{.ID}}") == ""
               for r in records)
    assert "{}" not in observed.stderr

    # Rebind the fixture journal to each actual stopped container, preserving
    # the same verified product/source capsule and the same SQLite database.
    owned_variants = []

    def variant(directory: Path, uid: str, *, readonly: bool = False, state_env: str = "/app/state") -> dict:
        variant_name = "state-variant-" + uuid.uuid4().hex
        owned_variants.append({"name": variant_name})
        resources["variants"] = owned_variants
        (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
        mount = f"type=bind,src={directory},dst=/app/state" + (",readonly" if readonly else "")
        ident = _docker("create", "--name", variant_name, "--network", "none", "--user", uid,
                        "--label", "org.wuwaterm.test.state-owner=" + owner,
                        "--label", "com.docker.compose.project=" + project,
                        "--label", "com.docker.compose.service=wuwaterm-telegram",
                        "--mount", f"type=bind,src={data},dst=/app/data,readonly", "--mount", mount,
                        "--env", "WUWATERM_DB_PATH=/app/data/terms.db", "--env", "WUWATERM_STATE_DIR=" + state_env,
                        "--entrypoint", "wuwaterm-telegram", receipt["image_id"], "--db", "/app/data/terms.db")
        owned_variants[-1]["id"] = ident
        (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
        component, recipe = _component(root, receipt, digest, data, directory, uid, variant_name, project,
                                       read_only=readonly, state_env=state_env)
        updated = copy.deepcopy(journal)
        updated["before"]["topology"]["telegram"] = component
        updated["target"]["manifest"]["components"]["telegram"] = copy.deepcopy(component)
        updated["before"]["containers"]["telegram"].update(id=ident, name=variant_name,
            started_at=_docker("inspect", "--format", "{{.State.StartedAt}}", ident))
        target_bytes = (json.dumps(updated["target"]["manifest"], ensure_ascii=True, sort_keys=True,
                                   indent=2, allow_nan=False) + "\n").encode("ascii")
        updated["manifest"]["sha256"] = hashlib.sha256(target_bytes).hexdigest()
        input_path.write_text(json.dumps(updated))
        write = _locked_code(root, "import component_transaction as t,json; r=pathlib.Path(sys.argv[2]); "
                             "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); t._write_journal(r,j)",
                             str(root), str(input_path))
        assert write.returncode == 0, write.stderr
        return recipe

    nonroot = root / "state-fixture-nonroot"
    nonroot.mkdir(mode=0o700)
    nonroot_path = nonroot / "channel_replies.json"
    nonroot_path.write_bytes(b"{\"fixture\":true}\n")
    nonroot_path.chmod(0o600)
    nonroot_before = _stat(nonroot_path)
    uid = f"{os.getuid()}:{os.getgid()}"
    recipe = variant(nonroot, uid)
    args.write_text(json.dumps({"snapshot": recipe, "env": options, "digest": digest,
                                "db": str(data / "terms.db"), "transaction": journal["transaction_id"],
                                "path": str(nonroot_path), "image": receipt["image_id"],
                                "revision": receipt["image_revision"]}))
    positive = _locked_code(root, code, str(root), str(args))
    assert positive.returncode == 0, positive.stderr
    assert json.loads(positive.stdout)["preimage"]["sha256"] == hashlib.sha256(b"{\"fixture\":true}\n").hexdigest()
    assert _stat(nonroot_path) == nonroot_before

    # The real product planner must refuse an inaccessible file, a read-only
    # state mount and an unmapped state path, before any state-file mutation.
    for directory, readonly, state_env in ((state_dir, False, "/app/state"),
                                            (nonroot, True, "/app/state"),
                                            (nonroot, False, "/unmapped")):
        recipe = variant(directory, uid, readonly=readonly, state_env=state_env)
        values = {"snapshot": recipe, "env": {**options, "WUWATERM_STATE_DIR": state_env}, "digest": digest,
                  "db": str(data / "terms.db"), "transaction": journal["transaction_id"],
                  "path": str(directory / "channel_replies.json"), "image": receipt["image_id"],
                  "revision": receipt["image_revision"]}
        args.write_text(json.dumps(values))
        denied = _locked_code(root, code, str(root), str(args))
        assert denied.returncode != 0 and "StateError" in denied.stderr
    assert _stat(path) == before
    assert _stat(nonroot_path) == nonroot_before
    with pytest.raises(state.StateError, match="unique visible bind"):
        state._container_path(recipe, str(root / "outside.json"))
    captured_mounts = state._mounts(recipe)
    captured_mounts[0]["inode"] += 1
    with pytest.raises(state.StateError, match="identity changed"):
        state._check_sources({"mounts": captured_mounts})
    for target in ("/", "/usr", "/usr/local/lib", "/etc", "/proc"):
        with pytest.raises(state.StateError, match="shadow"):
            state._safe_target(target)

    # A same-named fixture with the wrong ownership/configuration is never
    # treated as the recorded probe, and remains untouched for inspection.
    recorded_probe = records[0]
    resources["impostor_name"] = recorded_probe["container_name"]
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    impostor = _docker("create", "--name", recorded_probe["container_name"], "--network", "none",
                       "--label", "org.wuwaterm.test.state-owner=" + owner, receipt["image_id"])
    resources["impostor_id"] = impostor
    (tmp_path / "owned-resources.json").write_text(json.dumps(resources, indent=2) + "\n")
    with pytest.raises(state.StateError, match="ownership and isolation"):
        state._owned(recorded_probe)
    assert _docker("inspect", "--format", "{{.State.Status}}", impostor) == "created"


@pytest.mark.parametrize("kind", ["state-copy-v4", "state-endpoint", "state-canonical-v4", "state-copy-stage",
                                  "state-copy-publish", "state-copy-clean", "state-copy-discard"])
def test_schema5_journals_refuse_mutating_or_full4_helpers_before_admission(tmp_path: Path, kind: str) -> None:
    journal = {"schema_version": 5, "kind": "component-full", "transaction_id": "20261106T110000Z-" + "4" * 32,
               "source": {"source_tool_sha256": "a" * 64}}
    with pytest.raises(state.StateError, match="non-mutating state helpers"):
        state._execute(tmp_path, journal, kind=kind, image_id="sha256:" + "1" * 64, revision="2" * 40,
                       user="0:0", working_dir="/app", mounts=[], settings={}, input_path="/app/x")
    assert not (tmp_path / ".deployments").exists()


def test_schema5_probe_records_are_limited_to_non_mutating_kinds(tmp_path: Path) -> None:
    identifier = "20261106T110000Z-" + "5" * 32
    probe = {"schema_version": 5, "kind": "state-plan", "probe_id": identifier, "status": "active",
             "phase": "prepared", "transaction_id": identifier, "source_tool_sha256": "a" * 64,
             "daemon_id": "b" * 64, "image_id": "sha256:" + "1" * 64, "image_revision": "2" * 40,
             "container_name": "wuwaterm-state-probe-" + identifier, "container_id": None, "user": "0:0",
             "working_dir": "/app",
             "mounts": [{"host": "/srv/w/data", "container": "/app/data", "read_only": True,
                         "device": 1, "inode": 2}],
             "path_settings": dict.fromkeys(state._PATH_KEYS),
             "input_path": "/app/data/terms.db", "command": None, "output_sha256": None, "result": None,
             "failure": None, "transaction_kind": "runtime", "flavor": None, "state_kind": None, "clock": None,
             "canonical_writer": None}
    probe["command"] = state._command(probe)
    directory = tmp_path / ".deployments/component-adopt-probes"
    directory.mkdir(parents=True)
    path = directory / (identifier + ".json")
    path.write_text(json.dumps(probe), encoding="utf-8")
    assert state.read_state_probe(tmp_path, path.name) == probe
    for change in (lambda item: item.update(kind="state-copy-stage"),
                   lambda item: item.update(transaction_kind="other"),
                   lambda item: item.update(kind="state-canonical"),
                   lambda item: item.update(clock=1)):
        altered = copy.deepcopy(probe)
        change(altered)
        path.write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(state.StateError):
            state.read_state_probe(tmp_path, path.name)


def _planned_component(recipe: str, image: str, revision: str, kind: str) -> dict:
    return {"recipe": {"sha256": recipe}, "image": {"id": image, "revision": revision},
            "provenance": {"kind": kind}}


def test_schema5_plan_paths_come_from_both_recorded_recipes() -> None:
    """A cutover plans the legacy image and the product image in one journal."""
    legacy = _planned_component("a" * 64, "sha256:" + "1" * 64, "2" * 40, "legacy-image")
    product = _planned_component("b" * 64, "sha256:" + "3" * 64, "4" * 40, "completed-artifact")
    journal = {"schema_version": 5, "kind": "component-runtime",
               "before": {"topology": {"api": None, "telegram": legacy}},
               "target": {"manifest": {"components": {"api": None, "telegram": product}}}}
    assert state._planned_components(journal, product) == [product, legacy]
    assert state._planned_components(journal, legacy) == [legacy, product]
    # A shared recipe is one owner, not two.
    same = _planned_component("a" * 64, "sha256:" + "1" * 64, "2" * 40, "legacy-image")
    journal["target"]["manifest"]["components"]["telegram"] = same
    assert state._planned_components(journal, same) == [same]
    # Historical schemas keep exactly the component their recipe matched.
    journal["schema_version"] = 4
    journal["target"]["manifest"]["components"]["telegram"] = product
    assert state._planned_components(journal, legacy) == [legacy]


def test_canonical_reader_binding_covers_artifact_and_legacy_images() -> None:
    artifact = _planned_component("a" * 64, "sha256:" + "1" * 64, "2" * 40, "completed-artifact")
    legacy = _planned_component("b" * 64, "sha256:" + "3" * 64, "4" * 40, "legacy-image")
    wheel_image = _planned_component("c" * 64, "sha256:" + "5" * 64, "6" * 40, "wheel-image")
    full = {"schema_version": 5, "kind": "component-full", "selection": []}
    runtime5 = {"schema_version": 5, "kind": "component-runtime", "selection": ["telegram"]}
    assert state._canonical_binding(full, artifact, "chat-settings", 1) == (
        artifact["image"]["id"], "2" * 40, "wheel")
    assert state._canonical_binding(runtime5, legacy, "channel-replies", 1) == (
        legacy["image"]["id"], "4" * 40, "legacy")
    assert state._canonical_binding(runtime5, artifact, "chat-settings", 1)[2] == "wheel"
    assert state._canonical_binding(full, wheel_image, "chat-settings", 1)[2] == "wheel"
    # Historical runtime records still cannot read state, and schema 5 needs
    # its selected Telegram component.
    for journal in ({"schema_version": 4, "kind": "component-runtime", "selection": ["telegram"]},
                    {"schema_version": 5, "kind": "component-runtime", "selection": ["api"]}):
        with pytest.raises(state.StateError, match="full transaction"):
            state._canonical_binding(journal, artifact, "chat-settings", 1)
    with pytest.raises(state.StateError, match="full transaction"):
        state._canonical_binding(runtime5, artifact, "unknown", 1)
    with pytest.raises(state.StateError, match="clock"):
        state._canonical_binding(runtime5, artifact, "chat-settings", -1)
    unknown = _planned_component("d" * 64, "sha256:" + "7" * 64, "8" * 40, "source-image")
    with pytest.raises(state.StateError, match="native reader"):
        state._canonical_binding(runtime5, unknown, "chat-settings", 1)


# --------------------------------------------------- schema-5 native state leaves

def _legacy_plan(db: Path, state_dir: Path | None) -> dict:
    """Run the unselected legacy image's own read-only path resolver."""
    source = Path(state.__file__).resolve().parents[1] / "src"
    code = "import sys; sys.path.insert(0, " + repr(str(source)) + ")\n" + state._LEGACY_PLAN_CODE
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    if state_dir is not None:
        env["WUWATERM_STATE_DIR"] = str(state_dir)
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(db), "[]",
                             f"{os.geteuid()}:{os.getegid()}", "[]"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_legacy_plan_reports_the_native_pending_copy_without_writing(tmp_path: Path) -> None:
    """The old image copies into an empty StateDir; the probe only reports it."""
    data, state_dir = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state_dir.mkdir()
    legacy = data / "chat_settings.json"
    legacy.write_bytes(b'{"public":{},"allowed":[1]}')
    plan = _legacy_plan(data / "terms.db", state_dir)
    settings, replies = plan["kinds"]["chat-settings"], plan["kinds"]["channel-replies"]
    # The product's plan for the same one-to-one binds names the same state, so
    # the cutover is native-pending rather than relocation.
    assert settings["migration"] == "copy-if-absent"
    assert settings["effective_path"] == str(state_dir / "chat_settings.json")
    assert settings["mapped_target"] == str(state_dir / "chat_settings.json")
    assert settings["mapped_legacy"] == str(legacy)
    assert replies["migration"] == "none"
    assert replies["mapped_target"] == str(state_dir / "channel_replies.json")
    assert replies["mapped_legacy"] == str(data / "channel_replies.json")
    # The read-only probe never performs the copy it reports.
    assert os.listdir(state_dir) == []
    assert not (data / "channel_replies.json").exists()
    product = {"database_path": plan["database_path"],
               "kinds": {kind: dict(item) for kind, item in plan["kinds"].items()}}
    assert prepare.verify_state_plan_equivalence(plan, product) == {
        "chat-settings": "native-pending", "channel-replies": "none"}
    # An existing target is validated in place, and no StateDir keeps the old
    # database-adjacent location without any legacy endpoint claim.
    (state_dir / "chat_settings.json").write_bytes(b'{"public":{},"allowed":[1]}')
    assert _legacy_plan(data / "terms.db", state_dir)["kinds"]["chat-settings"]["migration"] == "validate-existing"
    plain = _legacy_plan(data / "terms.db", None)
    assert plain["kinds"]["chat-settings"]["effective_path"] == str(legacy)
    assert plain["kinds"]["chat-settings"]["mapped_legacy"] is None


def test_legacy_plan_keeps_an_explicit_path_outside_the_state_dir(tmp_path: Path) -> None:
    data, state_dir, custom = tmp_path / "data", tmp_path / "state", tmp_path / "custom"
    for directory in (data, state_dir, custom):
        directory.mkdir()
    (data / "chat_settings.json").write_bytes(b'{"public":{},"allowed":[1]}')
    chosen = custom / "chat_settings.json"
    chosen.write_bytes(b'{"public":{},"allowed":[1]}')
    code = "import sys; sys.path.insert(0, " + repr(str(Path(state.__file__).resolve().parents[1] / "src")) + ")\n"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "WUWATERM_STATE_DIR": str(state_dir), "WUWATERM_SETTINGS_PATH": str(chosen)}
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code + state._LEGACY_PLAN_CODE,
                             str(data / "terms.db"), "[]", f"{os.geteuid()}:{os.getegid()}", "[]"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)["kinds"]["chat-settings"]
    assert (settings["migration"], settings["effective_path"], settings["mapped_legacy"]) == (
        "none", str(chosen), None)
    assert os.listdir(state_dir) == []


def _state_entry_recipe(data: Path, state_dir: Path) -> dict:
    return {"mounts": [{"host": str(data), "container": "/app/data", "read_only": True},
                       {"host": str(state_dir), "container": "/app/state", "read_only": False}],
            "working_dir": "/app"}


def test_schema5_entry_mapping_preserves_relative_and_absolute_leaf_links(tmp_path: Path) -> None:
    data, state_dir = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    (state_dir / "refs").mkdir(parents=True)
    (state_dir / "refs" / "settings.json").write_bytes(b"settings")
    (state_dir / "refs" / "replies.json").write_bytes(b"replies")
    (state_dir / "chat_settings.json").symlink_to("refs/settings.json")
    (state_dir / "channel_replies.json").symlink_to("/app/state/refs/replies.json")
    recipe = _state_entry_recipe(data, state_dir)
    # A settings entry resolves to its referent; a reply entry stays the entry
    # the product replaces, for both relative and absolute links.
    assert state.entry_path(recipe, "/app/state/chat_settings.json", writable=True) == str(
        state_dir / "chat_settings.json")
    assert state.entry_path(recipe, "/app/state/refs/settings.json", writable=True) == str(
        state_dir / "refs/settings.json")
    journal = {"schema_version": 5, "kind": "component-runtime", "transaction_id": "t"}
    assert state._state_probe_paths(journal, recipe, str(state_dir / "refs" / "settings.json"))[:2] == (
        "/app/state/refs/settings.json", None)
    assert state._state_probe_paths(journal, recipe, str(state_dir / "channel_replies.json"))[:2] == (
        "/app/state/refs/replies.json", "/app/state/channel_replies.json")
    # A link may not leave the entry's own bind, and a read-only bind is not a
    # writable state location.
    (state_dir / "outside.json").symlink_to("/app/data/elsewhere.json")
    with pytest.raises(state.StateError, match="leaves its binding"):
        state._state_probe_paths(journal, recipe, str(state_dir / "outside.json"))
    readonly = _state_entry_recipe(data, state_dir)
    readonly["mounts"][1]["read_only"] = True
    with pytest.raises(state.StateError, match="writable"):
        state.entry_path(readonly, "/app/state/chat_settings.json", writable=True)
    # A nested host directory link is not a parent the mapper may cross.
    alias = tmp_path / "alias"
    alias.symlink_to(state_dir, target_is_directory=True)
    aliased = {"mounts": [{"host": str(alias), "container": "/app/state", "read_only": False}]}
    with pytest.raises(state.StateError, match="parent"):
        state.entry_path(aliased, "/app/state/chat_settings.json", writable=True)
    # Older schemas keep the strict single-path mapper.
    legacy_journal = {"schema_version": 4, "kind": "component-full", "phase": "state-snapshot"}
    with pytest.raises(state.observe.ObservationError, match="symlink"):
        state._state_probe_paths(legacy_journal, recipe, str(state_dir / "channel_replies.json"))


def test_schema5_preimage_and_canonical_read_the_file_a_leaf_names(tmp_path: Path, monkeypatch) -> None:
    data, state_dir = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    (state_dir / "refs").mkdir(parents=True)
    (state_dir / "refs" / "replies.json").write_bytes(b"replies")
    entry = state_dir / "channel_replies.json"
    # An absolute link names the container path the product would see.
    entry.symlink_to("/app/state/refs/replies.json")
    image, revision = "sha256:" + "1" * 64, "2" * 40
    recipe = dict(_state_entry_recipe(data, state_dir), image_id=image)
    identifier = "20261106T110000Z-" + "7" * 32
    journal = {"schema_version": 5, "kind": "component-runtime", "transaction_id": identifier,
               "selection": ["telegram"]}
    component = {"image": {"id": image, "revision": revision}, "provenance": {"kind": "legacy-image"}}
    preimage = {"present": True, "sha256": "a" * 64,
                "stat": {"device": 1, "inode": 2, "size": 1, "mtime_ns": 3, "ctime_ns": 4}}
    calls = []

    def probe(root_path: Path, journal_value: dict, **kwargs):
        calls.append(kwargs)
        return (preimage if kwargs["kind"] == "state-preimage" else
                {"raw_sha256": preimage["sha256"], "canonical_sha256": "b" * 64, "writer_can_replace": True})

    monkeypatch.setattr(state, "_journal_snapshot", lambda *args: (journal, component))
    monkeypatch.setattr(state, "_image", lambda *args: {"Config": {"User": ""}})
    monkeypatch.setattr(state, "_effective_user", lambda *args: "0:0")
    monkeypatch.setattr(state, "_completed_plan_paths", lambda *args: {str(entry)})
    monkeypatch.setattr(state, "_execute", probe)
    assert state.preimage(tmp_path, image_id=image, image_revision=revision, snapshot=recipe,
                          host_path=str(entry), transaction_id=identifier) == preimage
    result = state.canonical(tmp_path, snapshot=recipe, host_path=str(entry), transaction_id=identifier,
                             state_kind="channel-replies", clock=7)
    assert result["writer_can_replace"] is True
    # The retained link is read through and replaced at its own entry: the
    # preimage before and after the comparison and the canonical probe all read
    # the referent, and only canonical names the replaceable entry.
    assert [call["kind"] for call in calls] == ["state-preimage", "state-preimage",
                                                "state-canonical", "state-preimage"]
    assert {call["input_path"] for call in calls} == {"/app/state/refs/replies.json"}
    assert [call.get("canonical_writer") for call in calls] == [None, None,
                                                                "/app/state/channel_replies.json", None]


def _runtime_journal(schema: int = 5, *, target_image: str | None = None) -> dict:
    image, revision = "sha256:" + "1" * 64, "2" * 40
    telegram = {"recipe": {"sha256": "c" * 64}, "image": {"id": image, "revision": revision},
                "provenance": {"kind": "completed-artifact"}}
    target = (telegram if target_image is None else
              {"recipe": {"sha256": "d" * 64}, "image": {"id": target_image, "revision": "3" * 40},
               "provenance": {"kind": "completed-artifact"}})
    return {"schema_version": schema, "kind": "component-runtime",
            "transaction_id": "20261106T110000Z-" + "6" * 32, "selection": ["telegram"],
            "before": {"topology": {"api": None, "telegram": telegram}},
            "target": {"manifest": {"components": {"api": None, "telegram": target}}}}


def _canonical_probe(identifier: str) -> dict:
    probe = {"schema_version": 5, "kind": "state-canonical", "probe_id": identifier, "status": "active",
             "phase": "prepared", "transaction_id": "20261106T110000Z-" + "6" * 32,
             "source_tool_sha256": "a" * 64, "daemon_id": "b" * 64,
             "image_id": "sha256:" + "1" * 64, "image_revision": "2" * 40,
             "container_name": "wuwaterm-state-probe-" + identifier, "container_id": None, "user": "0:0",
             "working_dir": "/app",
             "mounts": [{"host": "/srv/w/state", "container": "/app/state", "read_only": False,
                         "device": 1, "inode": 2},
                        {"type": "tmpfs", "container": "/run/wuwaterm-full-state-" + identifier,
                         "options": ["rw", "noexec", "nosuid", "nodev", "uid=0", "gid=0", "mode=0700"]}],
             "path_settings": {key: None for key in state._PATH_KEYS},
             "input_path": "/app/state/refs/replies.json", "command": None, "output_sha256": None,
             "result": None, "failure": None, "transaction_kind": "runtime", "flavor": "wheel",
             "state_kind": "channel-replies", "clock": 7,
             "canonical_writer": "/app/state/channel_replies.json"}
    probe["command"] = state._command(probe)
    return probe


def test_schema5_runtime_canonical_probe_passes_its_record_and_binding_gates(tmp_path: Path) -> None:
    identifier = "20261106T110000Z-" + "8" * 32
    probe = _canonical_probe(identifier)
    directory = tmp_path / ".deployments/component-adopt-probes"
    directory.mkdir(parents=True)
    (directory / (identifier + ".json")).write_text(json.dumps(probe), encoding="utf-8")
    assert state.read_state_probe(tmp_path, identifier + ".json") == probe
    # The canonical program reads argv[1] and replaces the entry at argv[8].
    assert probe["command"][11] == "/app/state/channel_replies.json"
    journal = _runtime_journal()
    state.verify_schema5_probe_binding(journal, probe)
    # The reader must be the flavor of the image the journal already recorded.
    with pytest.raises(state.StateError, match="reader flavor"):
        state.verify_schema5_probe_binding(journal, dict(probe, flavor="legacy"))
    with pytest.raises(state.StateError, match="not recorded"):
        state.verify_schema5_probe_binding(journal, dict(probe, image_id="sha256:" + "9" * 64))
    with pytest.raises(state.StateError, match="read-only helper"):
        state.verify_schema5_probe_binding(journal, dict(probe, kind="state-copy-v4"))
    # A runtime record only re-reads state through its own old image, while the
    # native plan may run through either recorded recipe.
    upgraded = _runtime_journal(target_image="sha256:" + "9" * 64)
    state.verify_schema5_probe_binding(upgraded, dict(probe, kind="state-plan", flavor=None,
                                                      state_kind=None, clock=None, canonical_writer=None))
    with pytest.raises(state.StateError, match="not recorded"):
        state.verify_schema5_probe_binding(upgraded, dict(probe, image_id="sha256:" + "9" * 64))
    state.verify_schema5_probe_binding(upgraded, probe)
    # The record itself refuses a canonical probe without its reader context
    # and accepts only an absolute writer path when one is recorded.
    for change in (lambda item: item.update(flavor=None),
                   lambda item: item.update(clock=None),
                   lambda item: item.update(canonical_writer="relative/path")):
        altered = copy.deepcopy(probe)
        change(altered)
        altered["command"] = state._command(altered)
        (directory / (identifier + ".json")).write_text(json.dumps(altered), encoding="utf-8")
        with pytest.raises(state.StateError):
            state.read_state_probe(tmp_path, identifier + ".json")


def test_schema5_runtime_canonical_passes_the_execute_context_gate(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    journal = {"schema_version": 5, "kind": "component-runtime",
               "transaction_id": "20261106T110000Z-" + "6" * 32,
               "source": {"source_tool_sha256": "a" * 64}}

    class Reached(RuntimeError):
        pass

    admissions: list[dict] = []
    monkeypatch.setattr(state, "_admit", lambda *args, **kwargs: admissions.append(kwargs))
    monkeypatch.setattr(state, "_image", lambda *args: {"Config": {"User": ""}})
    monkeypatch.setattr(state, "_daemon", lambda: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):
        state._execute(tmp_path, journal, kind="state-canonical", image_id="sha256:" + "1" * 64,
                       revision="2" * 40, user="0:0", working_dir="/app", mounts=[], settings={},
                       input_path="/app/state/refs/replies.json",
                       canonical_writer="/app/state/channel_replies.json", flavor="wheel",
                       state_kind="channel-replies", clock=7)
    assert admissions == [{"transaction_kind": "runtime"}]
    # Historical runtime records keep refusing to read state.
    with pytest.raises(state.StateError, match="canonical state probe context is invalid"):
        state._execute(tmp_path, dict(journal, schema_version=4), kind="state-canonical",
                       image_id="sha256:" + "1" * 64, revision="2" * 40, user="0:0", working_dir="/app",
                       mounts=[], settings={}, input_path="/app/state/chat_settings.json",
                       flavor="wheel", state_kind="chat-settings", clock=7)
