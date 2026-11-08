from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

import pytest

from scripts import component_observe as observe
from scripts import component_runtime as runtime
from scripts import component_transaction as transaction
from scripts import deployment_manifest as legacy_manifest
from scripts import product_manifest as manifest
from test_product_manifest import make_database
from test_component_runtime import journal as sample_journal  # noqa: F401


DOCKER_FIXTURE = os.environ.get("WUWATERM_TRANSACTION_DOCKER_TEST") == "1"
RUN_BUILD = os.environ.get("WUWATERM_TRANSACTION_RUN_BUILD") == "1"
SOURCE_FIXTURE = os.environ.get("WUWATERM_TRANSACTION_SOURCE_FIXTURE")
COMPOSE = os.environ.get("WUWATERM_TRANSACTION_COMPOSE")
IMAGE = os.environ.get("WUWATERM_TRANSACTION_IMAGE")
EVIDENCE = os.environ.get("WUWATERM_TRANSACTION_EVIDENCE")


def test_recorded_pointer_combinations_refuse_unknown_or_malformed_bytes(tmp_path: Path) -> None:
    old_id = "20261106T100000Z-" + "1" * 32
    new_id = "20261106T110000Z-" + "2" * 32
    journal = {"pointers": {
        "old": {".deploy_commit": {"present": True, "bytes": "a" * 40 + "\n"},
                ".deploy_current": {"present": True, "bytes": old_id + "\n"}},
        "target": {".deploy_commit": {"present": True, "bytes": "b" * 40 + "\n"},
                   ".deploy_current": {"present": True, "bytes": new_id + "\n"}},
    }}
    (tmp_path / ".deploy_commit").write_text("b" * 40 + "\n")
    (tmp_path / ".deploy_current").write_text(old_id + "\n")
    transaction._check_pointers(tmp_path, journal)
    with pytest.raises(transaction.TransactionError, match="pointer drift"):
        transaction._check_pointers(tmp_path, journal, old_only=True)
    (tmp_path / ".deploy_current").write_text("20261106T120000Z-" + "3" * 32 + "\n")
    with pytest.raises(transaction.TransactionError, match="pointer drift"):
        transaction._check_pointers(tmp_path, journal)
    (tmp_path / ".deploy_commit").write_text("b" * 40)
    with pytest.raises(transaction.runtime.JournalError, match="invalid bytes"):
        transaction._check_pointers(tmp_path, journal)


@pytest.mark.parametrize("selected", ["api", "telegram"])
def test_selected_absent_component_is_refused_before_mutation(tmp_path: Path, sample_journal: dict,  # noqa: F811
                                                              selected: str) -> None:
    journal = copy.deepcopy(sample_journal)
    other = "telegram" if selected == "api" else "api"
    journal["selection"] = [selected]
    journal["before"]["topology"][selected] = None
    journal["before"]["containers"][selected] = None
    journal["target"]["manifest"]["components"][selected] = None
    journal["target"]["containers"][selected] = None
    journal["target"]["containers"][other] = copy.deepcopy(journal["before"]["containers"][other])
    if selected == "api":
        journal["api_identity"] = None
        journal["target"]["manifest"]["api_identity"] = None
    raw = (json.dumps(journal["target"]["manifest"], ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    journal["manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    with pytest.raises(transaction.TransactionError):
        transaction.update(tmp_path, journal, compose_path=Path("/nonexistent"))
    assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before


@pytest.mark.parametrize("name,value", [(".deploy_commit", "a" * 40),
                                         (".deploy_current", "20261106T100000Z-" + "1" * 32)])
def test_pointer_rollback_restores_original_absence_under_fd9(tmp_path: Path, name: str, value: str) -> None:
    (tmp_path / ".deployments").mkdir()
    (tmp_path / name).write_text(value + "\n", encoding="ascii")
    code = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
            "old={'present':False,'bytes':None}; new={'present':True,'bytes':sys.argv[4]+'\\n'}; "
            "t._replace_pointer(root,sys.argv[3],old,[old,new]); "
            "t._replace_pointer(root,sys.argv[3],old,[old,new]); print(json.dumps({'absent':not (root/sys.argv[3]).exists()}))")
    result = json.loads(_locked(tmp_path, Path(transaction.__file__).resolve().parents[1], code,
                                str(tmp_path), name, value))
    assert result == {"absent": True}


def test_legacy_probe_does_not_start_behind_an_existing_runtime_journal(tmp_path: Path) -> None:
    directory = tmp_path / ".deployments"
    directory.mkdir()
    (directory / runtime.JOURNAL_NAME).write_text("existing", encoding="ascii")
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    with pytest.raises(transaction.TransactionError, match="completed probe before this journal"):
        transaction._legacy_version(tmp_path, "sha256:" + "1" * 64, "a" * 40, {})
    assert {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{args[0]} exited {result.returncode}: {result.stderr}"
    return result.stdout.strip()


def _git(root: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return _run(["git", *args], cwd=root, env=env)


def _locked(root: Path, code_root: Path, code: str, *args: str,
            extra_env: dict[str, str] | None = None) -> str:
    command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
               "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
               "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + code,
               str(code_root), *args]
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root)}
    if extra_env:
        env.update(extra_env)
    return _run(command, cwd=code_root, env=env)


def _locked_python(root: Path, interpreter: str, code: str, *args: str) -> str:
    command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
               "fixture-lock", str(root), interpreter, "-I", "-B", "-c",
               "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + code,
               str(root), *args]
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root)}
    return _run(command, cwd=root, env=env)


def _docker(*args: str) -> str:
    return _run(["docker", *args])


def _preserve_producer(kind: str, digest: str, receipt: dict) -> None:
    if not EVIDENCE:
        return
    path = Path(EVIDENCE) / ("producer-" + kind + "-" + receipt["build_id"] + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump({"kind": kind, "digest": digest, "receipt": receipt}, output, sort_keys=True, indent=2)
        output.write("\n")


def _wait_ready(container: str) -> None:
    code = ("import urllib.request; "
            "assert urllib.request.urlopen('http://127.0.0.1:8788/readyz',timeout=2).status==200")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "exec", container, "python", "-B", "-c", code],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise AssertionError("real API did not become ready")


def _owned_cleanup(ids: list[str], owner: str) -> None:
    # Inspect each exact resource and its fixture owner before stopping or
    # deleting it. Transaction recovery may already have removed the target.
    for item in ids:
        result = subprocess.run(["docker", "inspect", item], capture_output=True, text=True, check=False)
        if result.returncode:
            continue
        raw = json.loads(result.stdout)[0]
        assert raw["Id"] == item
        assert raw["Config"]["Labels"].get("org.wuwaterm.test-owner") == owner
        if raw["State"]["Running"]:
            _docker("stop", item)
        _docker("rm", item)


def _interrupt_initial_writer(root: Path, code: str, input_path: Path, predecessor_id: str,
                              boundary: str) -> dict:
    """Kill the real writer at a journal filesystem syscall boundary."""
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    libc.ptrace.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    class Registers(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8", "rax", "rcx",
            "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags", "rsp", "ss", "fs_base",
            "gs_base", "ds", "es", "fs", "gs")]

    def child_trace() -> None:
        if libc.ptrace(0, 0, None, None) != 0:
            os._exit(97)

    command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
               "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
               "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + code,
               str(root), str(root), str(input_path), COMPOSE]
    child = subprocess.Popen(command, cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(root),
                             "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             preexec_fn=child_trace)

    def ptrace(request: int, address: int = 0, data: int = 0) -> int:
        ctypes.set_errno(0)
        result = libc.ptrace(request, child.pid, ctypes.c_void_p(address), ctypes.c_void_p(data))
        if result == -1 and ctypes.get_errno():
            raise OSError(ctypes.get_errno(), "ptrace failed")
        return result

    def read_string(address: int) -> bytes:
        value = bytearray()
        for offset in range(0, 512, 8):
            word = ptrace(2, address + offset) & ((1 << 64) - 1)
            value.extend(word.to_bytes(8, "little"))
            if b"\0" in value:
                return bytes(value).split(b"\0", 1)[0]
        raise AssertionError("unterminated traced pathname")

    observed = False
    replacing = False
    try:
        _, status = os.waitpid(child.pid, 0)
        assert os.WIFSTOPPED(status)
        ptrace(0x4200, data=1)
        ptrace(24)
        while True:
            _, status = os.waitpid(child.pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                child.returncode = os.waitstatus_to_exitcode(status)
                break
            assert os.WIFSTOPPED(status)
            signal_number = os.WSTOPSIG(status)
            if signal_number == signal.SIGTRAP | 0x80:
                registers = Registers()
                ptrace(12, data=ctypes.addressof(registers))
                result = ctypes.c_longlong(registers.rax).value
                if registers.orig_rax == 265 and result == -38:
                    target = read_string(registers.r10)
                    if boundary == "before-archive" and target == (predecessor_id + ".json").encode():
                        observed = True
                if registers.orig_rax in (264, 316):
                    if result == -38:
                        target = read_string(registers.r10)
                        replacing = target == runtime.JOURNAL_NAME.encode()
                        if replacing and boundary == "after-archive":
                            observed = True
                    elif replacing:
                        if boundary == "after-replace" and result == 0:
                            observed = True
                        replacing = False
                if observed:
                    os.kill(child.pid, signal.SIGKILL)
                    _, final = os.waitpid(child.pid, 0)
                    child.returncode = os.waitstatus_to_exitcode(final)
                    break
                ptrace(24)
            else:
                ptrace(24, data=0 if signal_number == signal.SIGTRAP else signal_number)
    except BaseException:
        if child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            os.waitpid(child.pid, 0)
        raise
    _, error = child.communicate()
    assert observed and child.returncode == -signal.SIGKILL, error.decode("utf-8", "replace")
    return {"boundary": boundary, "parent_exit": child.returncode, "syscall_observed": observed}


def _component(root: Path, image_id: str, image_ref: str, revision: str,
               actual: dict, overrides: list[dict], *, legacy: bool, env_files: list[str] | None = None,
               legacy_digest: str = "1" * 64) -> dict:
    snap = {**actual["recipe"], "schema_version": 1, "image_id": image_id,
            "env_files": env_files or [], "environment_overrides": overrides}
    path, digest = manifest.write_recipe_snapshot(root, snap)
    recipe = {key: snap[key] for key in manifest._RECIPE_FIELDS}
    recipe.update(snapshot_path=path, sha256=digest)
    source = {"revision_claim": revision, "verified_commit": None if legacy else revision,
              "verified_tree": None if legacy else "b" * 40}
    core = {"version": "0.7.0.dev1", **source}
    provenance = ({"kind": "legacy-image", "legacy_manifest_sha256": legacy_digest,
                   "wheel_identity": "unavailable", "lock_identity": "unavailable"} if legacy else
                  {"kind": "source-image", "build_recipe_sha256": "1" * 64,
                   "build_receipt_sha256": "2" * 64, "wheel_identity": "not-built-from-wheel"})
    return {"repository": "My-Denia/WuwaTerm", "version": "0.7.0.dev1", "source": source,
            "core": core, "image": {"ref": image_ref, "id": image_id, "digest": image_id,
                                      "revision": revision}, "recipe": recipe, "provenance": provenance}


def _legacy_digest(root: Path, revision: str, image_id: str, image_ref: str, database: dict) -> str:
    payload = {"schema_version": 1, "source_commit": revision,
               "image": {"ref": image_ref, "id": image_id, "digest": image_id, "revision": revision},
               "database": {"path": database["path"], "sha256": database["sha256"],
                            "provenance": database["provenance"]},
               "deployment_utc": "2026-11-06T00:00:00Z", "backup_path": "fixture"}
    path = root / ".deployments" / (revision + ".json")
    legacy_manifest.write_manifest(path, payload)
    assert legacy_manifest.verify_manifest(path, source_commit=revision, image_id=image_id,
                                           image_digest=image_id, db_path=Path(database["path"]), immutable=True) == payload
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _built_component(root: Path, base: dict, receipt: dict, commit: str, tree: str) -> dict:
    built = receipt["receipt"]
    snap = manifest.read_recipe_snapshot(root, base["recipe"]["sha256"])
    snap["image_id"] = built["image_id"]
    snapshot_path, digest = manifest.write_recipe_snapshot(root, snap)
    recipe = {key: snap[key] for key in manifest._RECIPE_FIELDS}
    recipe.update(snapshot_path=snapshot_path, sha256=digest)
    identity_source = {"revision_claim": commit, "verified_commit": commit, "verified_tree": tree}
    return {"repository": "My-Denia/WuwaTerm", "version": built["installed_core_version"],
            "source": identity_source, "core": {"version": built["installed_core_version"], **identity_source},
            "image": {"ref": built["image_ref"], "id": built["image_id"],
                      "digest": built["image_digest"], "revision": built["image_revision"]},
            "recipe": recipe,
            "provenance": {"kind": "source-image", "build_recipe_sha256": built["recipe_sha256"],
                           "build_receipt_sha256": receipt["digest"], "wheel_identity": "not-built-from-wheel"}}


def _wheel_component(root: Path, base: dict, built: dict, name: str) -> dict:
    receipt = built["receipt"]
    snapshot = manifest.read_recipe_snapshot(root, base["recipe"]["sha256"])
    snapshot.update(image_id=receipt["image_id"], service="wuwaterm-telegram", container_name=name,
                    command=["--db", "/app/data/terms.db"], entrypoint=["wuwaterm-telegram"])
    snapshot["mounts"].append({"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                               "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700"]})
    snapshot["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})
    path, digest = manifest.write_recipe_snapshot(root, snapshot)
    recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
    recipe.update(snapshot_path=path, sha256=digest)
    source = {"revision_claim": receipt["source"]["commit"], "verified_commit": receipt["source"]["commit"],
              "verified_tree": receipt["source"]["tree"]}
    core = {"version": receipt["core_version"], "revision_claim": receipt["core_source"]["commit"],
            "verified_commit": receipt["core_source"]["commit"], "verified_tree": receipt["core_source"]["tree"]}
    provenance = {key: receipt[key] for key in ("telegram_wheel_sha256", "core_wheel_sha256",
                                                "core_lock_sha256", "core_producer_kind")}
    provenance.update(kind="wheel-image", build_receipt_sha256=built["digest"])
    return {"repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
            "source": source, "core": core,
            "image": {"ref": receipt["image_ref"], "id": receipt["image_id"],
                      "digest": receipt["image_digest"], "revision": receipt["image_revision"]},
            "recipe": recipe, "provenance": provenance}


@pytest.mark.skipif(not DOCKER_FIXTURE, reason="real Docker transaction fixture not requested")
@pytest.mark.parametrize("mode", ["a-b-recovery", "registered-build-update", "retirement-interruption",
                                  "post-commit-old-image-pruned"])
def test_actual_api_transaction(tmp_path: Path, mode: str) -> None:
    real_update = mode != "a-b-recovery"
    if real_update and not RUN_BUILD:
        pytest.skip("real candidate build not requested")
    assert SOURCE_FIXTURE and COMPOSE and IMAGE
    root = tmp_path / "deployment"
    shutil.copytree(SOURCE_FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "source A fixture " + uuid.uuid4().hex)
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    (root / ".deployments").mkdir()
    extracted = tmp_path / "extracted-a"
    extracted.mkdir()
    prepare = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
               "s=r.prepare_source(root,sys.argv[3],sys.argv[4]); "
               "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
               "r._verify_capsule(root,s,pathlib.Path(sys.argv[5])); print(json.dumps(s))")
    source = json.loads(_locked(root, root, prepare, str(root), commit, tree, str(extracted)))
    receipt = None
    if real_update:
        build_code = ("import component_build as b,json; root=pathlib.Path(sys.argv[2]); "
                      "digest=b.build(root,sys.argv[3],sys.argv[4]); "
                      "print(json.dumps({'digest':digest,'receipt':b.verify_registered_build(root,digest)}))")
        receipt = json.loads(_locked(root, root, build_code, str(root), commit, tree))
        _preserve_producer("api", receipt["digest"], receipt["receipt"])

    image_raw = json.loads(_docker("image", "inspect", IMAGE))[0]
    image_id = image_raw["Id"]
    revision = image_raw["Config"]["Labels"]["org.opencontainers.image.revision"]
    assert len(revision) == 40
    owner = "tx-" + uuid.uuid4().hex[:20]
    project = owner
    service = "api"
    name = owner + "-api"
    deployment_id = manifest.new_deployment_id()
    old_id = manifest.new_deployment_id()
    db_dir = root / "fixture-data"
    db_dir.mkdir()
    database = make_database(db_dir / "terms.db")
    legacy_hash = _legacy_digest(root, revision, image_id, IMAGE, database)
    state = root / "fixture-api-state"
    state.mkdir()
    variables = {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_API_STATE_DIR": "/app/state-api",
                 "WUWATERM_API_BIND": "127.0.0.1", "WUWATERM_API_PORT": "8788"}
    overrides = [{"name": key, "mode": "literal", "value": value} for key, value in variables.items()]
    args = ["create", "--name", name, "--network", "none",
            "--label", "org.wuwaterm.test-owner=" + owner,
            "--label", "com.docker.compose.project=" + project,
            "--label", "com.docker.compose.service=" + service,
            "--mount", f"type=bind,src={db_dir},dst=/app/data,readonly",
            "--mount", f"type=bind,src={state},dst=/app/state-api"]
    for key, value in variables.items():
        args += ["--env", key + "=" + value]
    owned: list[str] = []
    second_evidence = None
    try:
        before_id = _docker(*args, IMAGE, "api")
        owned.append(before_id)
        _docker("start", before_id)
        _wait_ready(before_id)
        identity_stat = (state / "devices.db").stat()
        identity = {"path": str(state / "devices.db"), "device": identity_stat.st_dev,
                    "inode": identity_stat.st_ino}
        old = observe.observe_container(before_id, expected_image_id=image_id, project=project, service=service,
                                        expected_command=["api"], expected_entrypoint=["/entrypoint.sh"],
                                        env_fingerprint_key=b"x" * 32)
        old_component = _component(root, image_id, IMAGE, revision, old, overrides,
                                   legacy=True, legacy_digest=legacy_hash)
        new_component = _component(root, image_id, IMAGE, revision, old, overrides, legacy=False)
        if receipt is not None:
            new_component = _built_component(root, old_component, receipt, commit, tree)
        old_manifest = {"schema_version": 2, "deployment_id": old_id,
                        "deployment_utc": "2026-11-06T11:00:00Z",
                        "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": "a" * 40,
                                       "source_tree": "b" * 40, "source_tool_sha256": "1" * 64},
                        "components": {"api": old_component, "telegram": None},
                        "database": database, "api_identity": identity}
        manifest.write_manifest(root, old_manifest)
        target_manifest = copy.deepcopy(old_manifest)
        target_manifest.update(deployment_id=deployment_id,
                               operations={"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                           "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]})
        target_manifest["components"]["api"] = new_component
        if receipt is not None:
            planned = copy.deepcopy(old["container"])
            planned.update(id=None, image_id=receipt["receipt"]["image_id"], name=None,
                           started_at=None, restart_count=None, transaction_label=deployment_id)
            target_raw = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
            journal = {"schema_version": 2, "kind": "component-runtime", "transaction_id": deployment_id,
                       "action": "update", "status": "active", "phase": "prepared", "source": source,
                       "selection": ["api"],
                       "before": {"manifest": old_manifest, "topology": old_manifest["components"],
                                  "containers": {"api": old["container"], "telegram": None}},
                       "target": {"manifest": target_manifest, "containers": {"api": planned, "telegram": None}},
                       "database": {"binding": database, "stat": manifest.verify_database_binding(database)},
                       "api_identity": identity, "migrations": [],
                       "pointers": {"old": {".deploy_commit": {"present": True, "bytes": "a" * 40 + "\n"},
                                            ".deploy_current": {"present": True, "bytes": old_id + "\n"}},
                                    "target": {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                                               ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}},
                       "manifest": {"deployment_id": deployment_id,
                                    "path": f".deployments/products/{deployment_id}.json",
                                    "sha256": hashlib.sha256(target_raw).hexdigest()}}
            runtime.validate_journal(journal)
            input_path = root / ".deployments" / "fixture-input.json"
            input_path.write_text(json.dumps(journal), encoding="ascii")
            (root / ".deploy_commit").write_text("a" * 40 + "\n", encoding="ascii")
            (root / ".deploy_current").write_text(old_id + "\n", encoding="ascii")
            call = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                    "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                    "print(json.dumps(t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))))")
            if mode == "retirement-interruption":
                command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
                           "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
                           "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + call,
                           str(root), str(root), str(input_path), COMPOSE]
                env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root)}
                process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                journal_path = root / ".deployments" / runtime.JOURNAL_NAME
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("update ended before the retirement interruption")
                    try:
                        current = json.loads(journal_path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.002)
                        continue
                    if current.get("status") == "committed":
                        os.kill(process.pid, signal.SIGSTOP)
                        assert subprocess.run(["docker", "inspect", before_id], capture_output=True,
                                              check=False).returncode == 0
                        process.kill()
                        process.communicate(timeout=15)
                        assert process.returncode == -signal.SIGKILL
                        break
                    time.sleep(0.002)
                else:
                    process.kill()
                    process.communicate(timeout=15)
                    raise AssertionError("commit boundary was not reached")
                response = {"status": "committed", "retired_container_id": before_id,
                            "retirement": "interrupted"}
            else:
                response = json.loads(_locked(root, root, call, str(root), str(input_path), COMPOSE))
                assert response["status"] == "committed" and response["retired_container_id"] == before_id
                assert response["retirement"] == "complete"
            stored = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
            target_id = stored["target"]["containers"]["api"]["id"]
            owned.append(target_id)
            target = observe.observe_container(target_id, expected_image_id=receipt["receipt"]["image_id"],
                                               project=project, service=service, expected_command=["api"],
                                               expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert target["container"]["running"] and target["container"]["name"] == name
            assert stored["status"] == "committed" and manifest.read_manifest(root, deployment_id) == target_manifest
            assert (root / ".deploy_commit").read_text() == commit + "\n"
            assert (root / ".deploy_current").read_text() == deployment_id + "\n"
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            assert (state / "devices.db").stat().st_ino == identity["inode"]
            if mode != "retirement-interruption":
                assert subprocess.run(["docker", "inspect", before_id], capture_output=True, check=False).returncode != 0
            if mode in ("retirement-interruption", "post-commit-old-image-pruned"):
                (root / "README.md").write_text((root / "README.md").read_text() + "\nfixture second source\n")
                _git(root, "add", "README.md")
                _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-q", "-m", "source B for second candidate")
                commit_b = _git(root, "rev-parse", "HEAD")
                tree_b = _git(root, "rev-parse", "HEAD^{tree}")
                receipt_b = json.loads(_locked(root, root, build_code, str(root), commit_b, tree_b))
                _preserve_producer("api", receipt_b["digest"], receipt_b["receipt"])
                assert receipt_b["receipt"]["image_id"] != receipt["receipt"]["image_id"]
                second_id = manifest.new_deployment_id()
                second_manifest = copy.deepcopy(target_manifest)
                second_manifest["deployment_id"] = second_id
                second_source = receipt_b["receipt"]["recovery_source"]
                second_manifest["operations"].update(source_commit=commit_b, source_tree=tree_b,
                                                     source_tool_sha256=second_source["source_tool_sha256"])
                second_manifest["components"]["api"] = _built_component(root, new_component, receipt_b, commit_b, tree_b)
                next_target = copy.deepcopy(target["container"])
                next_target.update(id=None, image_id=receipt_b["receipt"]["image_id"], name=None,
                                   started_at=None, restart_count=None, transaction_label=second_id)
                second = copy.deepcopy(journal)
                second.update(transaction_id=second_id, source=second_source)
                second["before"] = {"manifest": target_manifest, "topology": target_manifest["components"],
                                    "containers": {"api": target["container"], "telegram": None}}
                second["target"] = {"manifest": second_manifest, "containers": {"api": next_target, "telegram": None}}
                second["pointers"]["old"] = copy.deepcopy(journal["pointers"]["target"])
                second["pointers"]["target"] = {".deploy_commit": {"present": True, "bytes": commit_b + "\n"},
                                                 ".deploy_current": {"present": True, "bytes": second_id + "\n"}}
                raw = (json.dumps(second_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
                second["manifest"] = {"deployment_id": second_id,
                                      "path": f".deployments/products/{second_id}.json",
                                      "sha256": hashlib.sha256(raw).hexdigest()}
                runtime.validate_journal(second)
                input_path.write_text(json.dumps(second), encoding="ascii")
                again = json.loads(_locked(root, root, call, str(root), str(input_path), COMPOSE))
                assert again["status"] == "committed" and again["retirement"] == "complete"
                assert subprocess.run(["docker", "inspect", before_id], capture_output=True, check=False).returncode != 0
                assert subprocess.run(["docker", "inspect", target_id], capture_output=True, check=False).returncode != 0
                final = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
                final_id = final["target"]["containers"]["api"]["id"]
                owned.append(final_id)
                assert final["status"] == "committed" and final["transaction_id"] == second_id
                assert (root / ".deploy_commit").read_text() == commit_b + "\n"
                assert (root / ".deploy_current").read_text() == second_id + "\n"
                history = root / ".deployments" / "component-runtime-history" / (deployment_id + ".json")
                assert history.is_file() and not history.stat().st_mode & 0o222
                assert manifest.verify_database_binding(database) == journal["database"]["stat"]
                assert (state / "devices.db").stat().st_ino == identity["inode"]
                second_evidence = {"source_b": commit_b, "second_receipt_sha256": receipt_b["digest"],
                                   "second_image_id": receipt_b["receipt"]["image_id"],
                                   "second_transaction_id": second_id, "second_container_id": final_id,
                                   "second_response": again, "first_old_removed": before_id,
                                   "first_target_removed": target_id,
                                   "retirement_process_exit": -signal.SIGKILL if mode == "retirement-interruption" else None}
                if mode == "post-commit-old-image-pruned":
                    # This source-A image is unique to the synthetic fixture;
                    # retain its receipt, inspect every dependent container,
                    # and remove only the exact stopped producer probe/tag.
                    old_image_id = receipt["receipt"]["image_id"]
                    old_ref = receipt["receipt"]["image_ref"]
                    probe_id = receipt["receipt"]["probe_container_id"]
                    image = json.loads(_docker("image", "inspect", old_image_id))[0]
                    assert image["Id"] == old_image_id and image["RepoTags"] == [old_ref]
                    assert image["Config"]["Labels"]["org.opencontainers.image.revision"] == commit
                    dependents = _docker("ps", "-aq", "--no-trunc", "--filter", "ancestor=" + old_image_id).splitlines()
                    assert dependents == [probe_id]
                    probe = json.loads(_docker("inspect", probe_id))[0]
                    assert not probe["State"]["Running"]
                    assert probe["Config"]["Labels"]["org.wuwaterm.component-build"] == receipt["receipt"]["build_id"]
                    preflight = {"image_id": old_image_id, "image_ref": old_ref, "probe_id": probe_id,
                                 "build_id": receipt["receipt"]["build_id"], "dependents": dependents}
                    preflight_path = (Path(EVIDENCE) if EVIDENCE else root / ".deployments") / ("prune-preflight-" + second_id + ".json")
                    fd = os.open(preflight_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        json.dump(preflight, output, sort_keys=True, indent=2)
                        output.write("\n")
                    _docker("rm", probe_id)
                    assert not _docker("ps", "-aq", "--no-trunc", "--filter", "ancestor=" + old_image_id)
                    _docker("image", "rm", old_ref)
                    assert subprocess.run(["docker", "image", "inspect", old_image_id],
                                          capture_output=True, check=False).returncode != 0
                    _docker("restart", final_id)
                    _wait_ready(final_id)
                    fresh_api = observe.observe_container(final_id, expected_image_id=receipt_b["receipt"]["image_id"],
                                                          project=project, service=service, expected_command=["api"],
                                                          expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
                    assert fresh_api["container"]["id"] == final["target"]["containers"]["api"]["id"]
                    assert fresh_api["container"]["started_at"] != final["target"]["containers"]["api"]["started_at"]
                    _docker("exec", final_id, "python", "-B", "-c",
                            "import sqlite3; c=sqlite3.connect('/app/state-api/devices.db'); "
                            "c.execute('PRAGMA user_version = 123'); c.commit(); c.close()")
                    assert (state / "devices.db").stat().st_ino == identity["inode"]
                    terminal_code = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                                     "r=t.verify_terminal(root,compose_path=pathlib.Path(sys.argv[3])); "
                                     "print(json.dumps({'status':r[1]['status'],'id':r[1]['transaction_id']}))")
                    for _ in range(2):
                        terminal = json.loads(_locked(root, root, terminal_code, str(root), COMPOSE))
                        assert terminal == {"status": "committed", "id": second_id}
                    extracted_b = tmp_path / "extracted-b"
                    extracted_b.mkdir()
                    extract_b = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                                 "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                                 "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                                 "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
                    assert _locked(root, root, extract_b, str(root), str(input_path), str(extracted_b)) == "ok"
                    source_file = root / "scripts/component_transaction.py"
                    source_text = source_file.read_text()
                    needle = "from __future__ import annotations"
                    assert source_text.count(needle) == 1
                    source_file.write_text(source_text.replace(needle, needle + "\nimport os as _fixture_os\n"
                                                               "if _fixture_os.getenv('FIXTURE_C_IMPORT_TRAP') == '1': "
                                                               "raise RuntimeError('source C loaded during B recovery')", 1))
                    _git(root, "add", "scripts/component_transaction.py")
                    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-q", "-m", "source C after old image pruning")
                    commit_c, tree_c = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
                    recover_committed = ("import component_transaction as t,json; "
                                         "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                                         "compose_path=pathlib.Path(sys.argv[5]))))")
                    recovery_args = (str(root), second_id, second_source["source_tool_sha256"], COMPOSE)
                    committed_recoveries = []
                    _docker("stop", final_id)
                    stopped = json.loads(_locked(root, extracted_b, recover_committed, *recovery_args,
                                                 extra_env={"FIXTURE_C_IMPORT_TRAP": "1"}))
                    assert stopped["status"] == "committed" and stopped["already_committed"]
                    assert stopped["health"]["api"] == "unconfirmed"
                    assert not observe._inspect(final_id)["State"]["Running"]
                    committed_recoveries.append(stopped)
                    _docker("start", final_id)
                    _wait_ready(final_id)
                    result = json.loads(_locked(root, extracted_b, recover_committed, *recovery_args,
                                                extra_env={"FIXTURE_C_IMPORT_TRAP": "1"}))
                    assert result["status"] == "committed" and result["already_committed"]
                    assert result["health"]["api"] == "ready"
                    committed_recoveries.append(result)
                    fresh_api = observe.observe_container(final_id, expected_image_id=receipt_b["receipt"]["image_id"],
                                                          project=project, service=service, expected_command=["api"],
                                                          expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
                    assert (root / ".deploy_commit").read_text() == commit_b + "\n"
                    assert (root / ".deploy_current").read_text() == second_id + "\n"
                    receipt_c = json.loads(_locked(root, root, build_code, str(root), commit_c, tree_c))
                    _preserve_producer("api", receipt_c["digest"], receipt_c["receipt"])
                    third_id = manifest.new_deployment_id()
                    third_manifest = copy.deepcopy(second_manifest)
                    third_manifest["deployment_id"] = third_id
                    third_source = receipt_c["receipt"]["recovery_source"]
                    third_manifest["operations"].update(source_commit=commit_c, source_tree=tree_c,
                                                        source_tool_sha256=third_source["source_tool_sha256"])
                    third_manifest["components"]["api"] = _built_component(root, second_manifest["components"]["api"],
                                                                           receipt_c, commit_c, tree_c)
                    current_api = fresh_api["container"]
                    third_target = copy.deepcopy(current_api)
                    third_target.update(id=None, image_id=receipt_c["receipt"]["image_id"], name=None,
                                        started_at=None, restart_count=None, transaction_label=third_id)
                    third = copy.deepcopy(second)
                    third.update(transaction_id=third_id, source=third_source)
                    third["before"] = {"manifest": second_manifest, "topology": second_manifest["components"],
                                       "containers": {"api": current_api, "telegram": None}}
                    third["target"] = {"manifest": third_manifest, "containers": {"api": third_target, "telegram": None}}
                    third["pointers"]["old"] = copy.deepcopy(second["pointers"]["target"])
                    third["pointers"]["target"] = {".deploy_commit": {"present": True, "bytes": commit_c + "\n"},
                                                    ".deploy_current": {"present": True, "bytes": third_id + "\n"}}
                    raw_c = manifest._encode(third_manifest)
                    third["manifest"] = {"deployment_id": third_id, "path": f".deployments/products/{third_id}.json",
                                         "sha256": hashlib.sha256(raw_c).hexdigest()}
                    runtime.validate_journal(third)
                    input_path.write_text(json.dumps(third), encoding="ascii")
                    third_result = json.loads(_locked(root, root, call, str(root), str(input_path), COMPOSE))
                    assert third_result["status"] == "committed" and third_result["retirement"] == "complete"
                    third_final = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
                    owned.append(third_final["target"]["containers"]["api"]["id"])
                    assert (root / ".deploy_commit").read_text() == commit_c + "\n"
                    assert (root / ".deploy_current").read_text() == third_id + "\n"
                    second_evidence["old_image_pruned"] = preflight
                    second_evidence["committed_recoveries"] = committed_recoveries
                    second_evidence["third_update"] = third_result
            if EVIDENCE:
                record = {"fixture": "actual-api-registered-build-update", "source": source,
                          "build_receipt_sha256": receipt["digest"], "build": receipt["receipt"],
                          "before": old, "target": target,
                          "database": journal["database"], "api_identity": identity,
                          "target_pointers": journal["pointers"]["target"], "response": response,
                          "second_update": second_evidence}
                path = Path(EVIDENCE) / ("api-update-" + deployment_id + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(record, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        _docker("rename", before_id, "wuwaterm-rollback-" + deployment_id + "-api")
        _docker("stop", before_id)
        target_args = args + ["--label", observe.TRANSACTION_LABEL + "=" + deployment_id]
        target_id = _docker(*target_args, IMAGE, "api")
        owned.append(target_id)
        _docker("start", target_id)
        _wait_ready(target_id)
        target = observe.observe_container(target_id, expected_image_id=image_id, project=project, service=service,
                                           expected_command=["api"], expected_entrypoint=["/entrypoint.sh"],
                                           env_fingerprint_key=b"x" * 32)
        target_raw = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
        journal = {"schema_version": 2, "kind": "component-runtime", "transaction_id": deployment_id,
                   "action": "update", "status": "active", "phase": "deploy-commit", "source": source,
                   "selection": ["api"],
                   "before": {"manifest": old_manifest, "topology": old_manifest["components"],
                              "containers": {"api": old["container"], "telegram": None}},
                   "target": {"manifest": target_manifest,
                              "containers": {"api": target["container"], "telegram": None}},
                   "database": {"binding": database, "stat": manifest.verify_database_binding(database)},
                   "api_identity": identity, "migrations": [],
                   "pointers": {"old": {".deploy_commit": {"present": True, "bytes": "a" * 40 + "\n"},
                                        ".deploy_current": {"present": True, "bytes": old_id + "\n"}},
                                "target": {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                                           ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}},
                   "manifest": {"deployment_id": deployment_id,
                                "path": f".deployments/products/{deployment_id}.json",
                                "sha256": hashlib.sha256(target_raw).hexdigest()}}
        runtime.validate_journal(journal)
        (root / ".deployments" / runtime.JOURNAL_NAME).write_text(json.dumps(journal), encoding="ascii")
        (root / ".deploy_commit").write_text(commit + "\n", encoding="ascii")
        (root / ".deploy_current").write_text(old_id + "\n", encoding="ascii")
        (root / "scripts" / "component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
        _git(root, "add", "scripts/component_transaction.py")
        _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-q", "-m", "source B")
        source_b = _git(root, "rev-parse", "HEAD")
        call = ("import component_transaction as t,json; "
                "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                "compose_path=pathlib.Path(sys.argv[5]))))")
        response = json.loads(_locked(root, extracted, call, str(root), deployment_id,
                                      source["source_tool_sha256"], COMPOSE))
        assert response["status"] == "rolled_back" and not response["already_restored"]
        assert _git(root, "rev-parse", "HEAD") == source_b
        assert (root / ".deploy_commit").read_text() == "a" * 40 + "\n"
        assert (root / ".deploy_current").read_text() == old_id + "\n"
        restored = observe.observe_container(before_id, expected_image_id=image_id, project=project, service=service,
                                             expected_command=["api"], expected_entrypoint=["/entrypoint.sh"],
                                             env_fingerprint_key=b"x" * 32)
        assert restored["container"]["running"] and restored["container"]["name"] == name
        assert manifest.verify_database_binding(database) == journal["database"]["stat"]
        assert (state / "devices.db").stat().st_ino == identity["inode"]
        assert subprocess.run(["docker", "inspect", target_id], capture_output=True, check=False).returncode != 0
        retry = json.loads(_locked(root, extracted, call, str(root), deployment_id,
                                   source["source_tool_sha256"], COMPOSE))
        assert retry["status"] == "rolled_back" and retry["already_restored"]
        if EVIDENCE:
            record = {"fixture": "actual-api-split-pointers-source-a-b-a", "source_a": commit,
                      "source_b": source_b, "source_tool_sha256": source["source_tool_sha256"],
                      "old_observation": old, "target_observation": target,
                      "restored_observation": restored, "database": journal["database"],
                      "api_identity": identity, "old_pointers": journal["pointers"]["old"],
                      "first_recovery": response, "repeated_recovery": retry,
                      "removed_target": target_id}
            path = Path(EVIDENCE) / ("api-recovery-" + deployment_id + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(record, output, sort_keys=True, indent=2)
                output.write("\n")
    finally:
        matches = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner)
        owned.extend(item for item in matches.splitlines() if item not in owned)
        _owned_cleanup(owned, owner)


@pytest.mark.skipif(not DOCKER_FIXTURE or not RUN_BUILD, reason="real Telegram wheel build not requested")
@pytest.mark.parametrize("mode", ["stopped", "loopback-polling", "tg-401", "tg-stale", "root-owned-state", "pending-state-interruption",
                                  "tg-unselected-api", "api-unselected-tg",
                                  "both-loopback", "both-crash-after-stop", "both-crash-partial-start",
                                  "both-crash-after-retire", "both-crash-after-manifest", "both-crash-between-pointers"])
def test_telegram_preflight_uses_registered_wheel_product_state_plan(tmp_path: Path, mode: str) -> None:
    running = mode != "stopped"
    both = mode.startswith("both-")
    api_selected = both or mode == "api-unselected-tg"
    tg_selected = mode != "api-unselected-tg"
    has_api = both or mode in ("tg-unselected-api", "api-unselected-tg")
    assert SOURCE_FIXTURE and COMPOSE and IMAGE
    required = ("TG_REPO", "TG_COMMIT", "TG_TREE", "CORE_WHEEL", "CORE_ARTIFACT", "WHEELHOUSE", "BUILD_PYTHON")
    config = {key: os.environ["WUWATERM_TRANSACTION_" + key] for key in required}
    root = tmp_path / "deployment"
    shutil.copytree(SOURCE_FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "Telegram state plan fixture " + uuid.uuid4().hex)
    commit, tree = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
    (root / ".deployments").mkdir()
    code = ("import component_wheel_build as w,json; root=pathlib.Path(sys.argv[2]); "
            "d=w.build_wheel_image(root,sys.argv[3],sys.argv[4],pathlib.Path(sys.argv[5]),sys.argv[6],"
            "sys.argv[7],pathlib.Path(sys.argv[8]),pathlib.Path(sys.argv[9]),pathlib.Path(sys.argv[10])); "
            "print(json.dumps({'digest':d,'receipt':w.verify_registered_wheel_build(root,d)}))")
    built = json.loads(_locked_python(root, config["BUILD_PYTHON"], code, str(root), commit, tree,
                                     config["TG_REPO"], config["TG_COMMIT"], config["TG_TREE"],
                                     config["CORE_WHEEL"], config["CORE_ARTIFACT"], config["WHEELHOUSE"]))
    receipt, digest = built["receipt"], built["digest"]
    _preserve_producer("telegram", digest, receipt)
    api_build = None
    if api_selected:
        api_code = ("import component_build as b,json; root=pathlib.Path(sys.argv[2]); "
                    "digest=b.build(root,sys.argv[3],sys.argv[4]); "
                    "print(json.dumps({'digest':digest,'receipt':b.verify_registered_build(root,digest)}))")
        api_build = json.loads(_locked(root, root, api_code, str(root), commit, tree))
        _preserve_producer("api", api_build["digest"], api_build["receipt"])
    image_raw = json.loads(_docker("image", "inspect", IMAGE))[0]
    image_id = image_raw["Id"]
    revision = image_raw["Config"]["Labels"]["org.opencontainers.image.revision"]
    owner = "tg-plan-" + uuid.uuid4().hex[:16]
    db_dir, state = root / "fixture-data", root / "fixture-state"
    db_dir.mkdir()
    state.mkdir()
    database = make_database(db_dir / "terms.db")
    legacy_hash = _legacy_digest(root, revision, image_id, IMAGE, database)
    (root / ".deploy_commit").write_text(revision + "\n", encoding="ascii")
    variables = {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_STATE_DIR": "/app/state",
                 "WUWATERM_SETTINGS_PATH": "/app/state/chat_settings.json",
                 "WUWATERM_CHANNEL_REPLY_INDEX_PATH": "/app/state/channel_replies.json"}
    overrides = [{"name": key, "mode": "literal", "value": value} for key, value in variables.items()]
    args = ["create", "--name", owner, "--network", "none",
            "--label", "org.wuwaterm.test-owner=" + owner,
            "--label", "com.docker.compose.project=" + owner,
            "--label", "com.docker.compose.service=wuwaterm",
            "--mount", f"type=bind,src={db_dir},dst=/app/data,readonly",
            "--mount", f"type=bind,src={state},dst=/app/state"]
    env_files: list[str] = []
    if running:
        if mode not in ("root-owned-state", "pending-state-interruption"):
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        fixture = root / "fixture-loopback"
        fixture.mkdir()
        (fixture / "sitecustomize.py").write_text('''
import importlib.util, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
is_tg = importlib.util.find_spec("wuwaterm_telegram") is not None
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        method = self.path.rsplit("/", 1)[-1]
        denied = is_tg and os.environ.get("FIXTURE_TG_MODE") == "401" and method == "getUpdates"
        with Path("/app/state/fixture-calls.log").open("a") as out: out.write(method + (":401" if denied else "") + "\\n")
        if denied:
            data = json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
            self.send_response(401); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if method == "getMe": result = {"id": 123456, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
        elif method == "deleteWebhook": result = True
        elif method == "getUpdates": time.sleep(0.15); result = []
        else: self.send_error(400); return
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        try: self.wfile.write(data)
        except BrokenPipeError: pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:" + str(server.server_port)
if is_tg:
    from wuwaterm_telegram import bot
    real = bot.PollHealthBot
    def local(*args, **kwargs): return real(*args, base_url=base+"/bot", base_file_url=base+"/file/bot", **kwargs)
    bot.PollHealthBot = local
else:
    from wuwaterm import bot
    real = bot.ApplicationBuilder
    def local(): return real().base_url(base+"/bot").base_file_url(base+"/file/bot")
    bot.ApplicationBuilder = local
''', encoding="utf-8")
        env_file = root / ".fixture.env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=123456:LOCAL_FIXTURE\nPYTHONPATH=/fixture\nNO_PROXY=*\n"
                            + ("FIXTURE_TG_MODE=401\n" if mode == "tg-401" else ""), encoding="ascii")
        env_file.chmod(0o600)
        env_files = [str(env_file)]
        args += ["--env-file", str(env_file), "--mount", f"type=bind,src={fixture},dst=/fixture,readonly"]
    for key, value in variables.items():
        args += ["--env", key + "=" + value]
    owned: list[str] = []
    process: subprocess.Popen | None = None
    writer = None
    frozen_target = None
    try:
        old_id = _docker(*args, IMAGE, "bot")
        owned.append(old_id)
        if running:
            _docker("start", old_id)
            deadline = time.monotonic() + 45
            calls_path = state / "fixture-calls.log"
            while time.monotonic() < deadline:
                if calls_path.exists() and "getUpdates" in calls_path.read_text():
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("legacy bot did not poll the isolated loopback server")
        old = observe.observe_container(old_id, expected_image_id=image_id, project=owner, service="wuwaterm",
                                        expected_command=["bot"], expected_entrypoint=["/entrypoint.sh"],
                                        env_fingerprint_key=b"x" * 32)
        legacy = _component(root, image_id, IMAGE, revision, old, overrides, legacy=True,
                            env_files=env_files, legacy_digest=legacy_hash)
        api_old = None
        api_legacy = None
        api_target = None
        api_identity = None
        if has_api:
            api_state = root / "fixture-api-state"
            api_state.mkdir()
            api_vars = {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_API_STATE_DIR": "/app/state-api",
                        "WUWATERM_API_BIND": "127.0.0.1", "WUWATERM_API_PORT": "8788"}
            api_args = ["create", "--name", owner + "-api", "--network", "none",
                        "--label", "org.wuwaterm.test-owner=" + owner,
                        "--label", "com.docker.compose.project=" + owner,
                        "--label", "com.docker.compose.service=wuwaterm-api",
                        "--mount", f"type=bind,src={db_dir},dst=/app/data,readonly",
                        "--mount", f"type=bind,src={api_state},dst=/app/state-api"]
            for key, value in api_vars.items():
                api_args += ["--env", key + "=" + value]
            api_id = _docker(*api_args, IMAGE, "api")
            owned.append(api_id)
            _docker("start", api_id)
            _wait_ready(api_id)
            api_stat = (api_state / "devices.db").stat()
            api_identity = {"path": str(api_state / "devices.db"), "device": api_stat.st_dev, "inode": api_stat.st_ino}
            api_old = observe.observe_container(api_id, expected_image_id=image_id, project=owner,
                                                service="wuwaterm-api", expected_command=["api"],
                                                expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            api_overrides = [{"name": key, "mode": "literal", "value": value} for key, value in api_vars.items()]
            api_legacy = _component(root, image_id, IMAGE, revision, api_old, api_overrides,
                                    legacy=True, legacy_digest=legacy_hash)
            api_target = _built_component(root, api_legacy, api_build, commit, tree) if api_selected else api_legacy
        snap = manifest.read_recipe_snapshot(root, legacy["recipe"]["sha256"])
        snap.update(image_id=receipt["image_id"], service="wuwaterm-telegram", container_name=owner + "-new",
                    command=["--db", "/app/data/terms.db"], entrypoint=["wuwaterm-telegram"])
        health_options = ["rw", "noexec", "nosuid", "nodev", "mode=0700"]
        if running and mode not in ("root-owned-state", "pending-state-interruption"):
            health_options += [f"uid={os.getuid()}", f"gid={os.getgid()}"]
        snap["mounts"].append({"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                               "options": health_options})
        snap["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})
        snapshot_path, snap_digest = manifest.write_recipe_snapshot(root, snap)
        recipe = {key: snap[key] for key in manifest._RECIPE_FIELDS}
        recipe.update(snapshot_path=snapshot_path, sha256=snap_digest)
        source = {"revision_claim": receipt["source"]["commit"],
                  "verified_commit": receipt["source"]["commit"], "verified_tree": receipt["source"]["tree"]}
        core = {"version": receipt["core_version"], "revision_claim": receipt["core_source"]["commit"],
                "verified_commit": receipt["core_source"]["commit"], "verified_tree": receipt["core_source"]["tree"]}
        provenance = {key: receipt[key] for key in ("telegram_wheel_sha256", "core_wheel_sha256",
                                                    "core_lock_sha256", "core_producer_kind")}
        provenance.update(kind="wheel-image", build_receipt_sha256=digest)
        component = {"repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
                     "source": source, "core": core,
                     "image": {"ref": receipt["image_ref"], "id": receipt["image_id"],
                               "digest": receipt["image_digest"], "revision": receipt["image_revision"]},
                     "recipe": recipe, "provenance": provenance}
        deployment_id = manifest.new_deployment_id()
        target_manifest = {"schema_version": 2, "deployment_id": deployment_id,
                           "deployment_utc": "2026-11-06T11:00:00Z",
                           "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                          "source_tree": tree,
                                          "source_tool_sha256": receipt["recovery_source"]["source_tool_sha256"]},
                           "components": {"api": api_target, "telegram": component if tg_selected else legacy},
                           "database": database, "api_identity": api_identity}
        planned = copy.deepcopy(old["container"])
        if tg_selected:
            planned.update(id=None, image_id=receipt["image_id"], project=owner, service="wuwaterm-telegram",
                           name=None, started_at=None, restart_count=None, transaction_label=deployment_id)
        api_planned = copy.deepcopy(api_old["container"]) if has_api else None
        if api_selected:
            api_planned.update(id=None, image_id=api_build["receipt"]["image_id"], name=None,
                               started_at=None, restart_count=None, transaction_label=deployment_id)
        migrations = []
        if mode not in ("root-owned-state", "pending-state-interruption"):
            for kind, filename in (("chat-settings", "chat_settings.json"), ("channel-replies", "channel_replies.json")):
                migrations.append({"kind": kind, "action": "none", "effective_path": "/app/state/" + filename,
                                   "source_path": str(db_dir / filename), "target_path": str(state / filename),
                                   "source_before": transaction._state_preimage(str(db_dir / filename)),
                                   "target_before": transaction._state_preimage(str(state / filename)), "created": None})
        raw = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
        journal = {"schema_version": 2, "kind": "component-runtime", "transaction_id": deployment_id,
                   "action": "update", "status": "active", "phase": "prepared",
                   "source": receipt["recovery_source"],
                   "selection": ["api", "telegram"] if both else (["api"] if api_selected else ["telegram"]),
                   "before": {"manifest": None, "topology": {"api": api_legacy, "telegram": legacy},
                              "containers": {"api": api_old["container"] if has_api else None, "telegram": old["container"]}},
                   "target": {"manifest": target_manifest, "containers": {"api": api_planned, "telegram": planned}},
                   "database": {"binding": database, "stat": manifest.verify_database_binding(database)},
                   "api_identity": api_identity, "migrations": migrations if tg_selected else [],
                   "pointers": {"old": {".deploy_commit": {"present": True, "bytes": revision + "\n"},
                                        ".deploy_current": {"present": False, "bytes": None}},
                                "target": {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                                           ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}},
                   "manifest": {"deployment_id": deployment_id,
                                "path": f".deployments/products/{deployment_id}.json",
                                "sha256": hashlib.sha256(raw).hexdigest()}}
        runtime.validate_journal(journal)
        input_path = root / ".deployments" / "fixture-input.json"
        input_path.write_text(json.dumps(journal), encoding="ascii")
        call = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                "print(json.dumps(t.preflight_telegram(root,j,compose_path=pathlib.Path(sys.argv[4]))))")
        result = None
        if tg_selected and mode not in ("root-owned-state", "pending-state-interruption"):
            result = json.loads(_locked(root, root, call, str(root), str(input_path), COMPOSE))
            assert result["wheel_receipt_sha256"] == digest
            for kind in ("chat-settings", "channel-replies"):
                assert result["state_plan"]["kinds"][kind]["migration"] == "none"
                assert result["state_plan"]["kinds"][kind]["mapped_target"] == result["old_state_plan"]["kinds"][kind]["mapped_target"]
        assert old["container"]["running"] is running
        rejected = None
        if not running:
            bad = copy.deepcopy(journal)
            bad["before"]["topology"]["telegram"]["provenance"]["legacy_manifest_sha256"] = "0" * 64
            bad_path = root / ".deployments" / "fixture-bad-legacy.json"
            bad_path.write_text(json.dumps(bad), encoding="ascii")
            bad_call = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                        "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                        "\ntry: t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))"
                        "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                        "\nelse: raise AssertionError('incorrect legacy evidence was admitted')")
            bad_result = json.loads(_locked(root, root, bad_call, str(root), str(bad_path), COMPOSE))
            assert bad_result["error"] == "legacy manifest digest or binding differs"
            assert not (root / ".deployments" / runtime.JOURNAL_NAME).exists()
            (db_dir / "chat_settings.json").write_text("{}\n", encoding="ascii")
            refusing = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                        "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                        "\ntry: t.preflight_telegram(root,j,compose_path=pathlib.Path(sys.argv[4]))"
                        "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                        "\nelse: raise AssertionError('state relocation was admitted')")
            rejected = json.loads(_locked(root, root, refusing, str(root), str(input_path), COMPOSE))
            assert rejected["error"] == "Telegram state relocation requires a full update"
            update_refusal = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                              "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                              "\ntry: t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))"
                              "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                              "\nelse: raise AssertionError('state relocation was admitted')")
            update_rejected = json.loads(_locked(root, root, update_refusal, str(root), str(input_path), COMPOSE))
            assert update_rejected["error"] == "Telegram state preflight refused; services unchanged"
            refused_journal = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
            assert refused_journal["status"] == "rolled_back" and refused_journal["migrations"] == []
            unchanged = observe.observe_container(old_id, expected_image_id=image_id, project=owner, service="wuwaterm",
                                                   expected_command=["bot"], expected_entrypoint=["/entrypoint.sh"],
                                                   env_fingerprint_key=b"x" * 32)
            assert unchanged["container"] == old["container"]
            assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            assert (db_dir / "chat_settings.json").read_text(encoding="ascii") == "{}\n"
            (db_dir / "chat_settings.json").unlink()
            deployment_id = manifest.new_deployment_id()
            journal["transaction_id"] = deployment_id
            target_manifest["deployment_id"] = deployment_id
            journal["target"]["containers"]["telegram"]["transaction_label"] = deployment_id
            journal["pointers"]["target"][".deploy_current"]["bytes"] = deployment_id + "\n"
            raw = (json.dumps(target_manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
            journal["manifest"] = {"deployment_id": deployment_id,
                                   "path": f".deployments/products/{deployment_id}.json",
                                   "sha256": hashlib.sha256(raw).hexdigest()}
            runtime.validate_journal(journal)
            input_path.write_text(json.dumps(journal), encoding="ascii")
        update_call = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                       "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                       "print(json.dumps(t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))))")
        if mode == "tg-401":
            failing = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                       "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                       "\ntry: t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))"
                       "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                       "\nelse: raise AssertionError('401 polling was accepted')")
            refused = json.loads(_locked(root, root, failing, str(root), str(input_path), COMPOSE))
            assert refused["error"] == "update interrupted; recover the recorded transaction"
            interrupted = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
            assert interrupted["status"] == "active" and interrupted["phase"] == "actual-health-bindings"
            target_id = interrupted["target"]["containers"]["telegram"]["id"]
            assert target_id is not None
            owned.append(target_id)
            assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
            assert "getUpdates:401" in (state / "fixture-calls.log").read_text().splitlines()
            extracted = tmp_path / "extracted-a"
            extracted.mkdir()
            extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                       "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                       "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                       "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
            assert _locked(root, root, extract, str(root), str(input_path), str(extracted)) == "ok"
            (root / "scripts" / "component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
            _git(root, "add", "scripts/component_transaction.py")
            _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "source B must not execute")
            recover_call = ("import component_transaction as t,json; "
                            "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                            "compose_path=pathlib.Path(sys.argv[5]))))")
            recovered = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                            receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert recovered["status"] == "rolled_back" and recovered["polling_health"] == "unconfirmed"
            restored = observe.observe_container(old_id, expected_image_id=image_id, project=owner,
                                                  service="wuwaterm", expected_command=["bot"],
                                                  expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert restored["container"]["running"] and restored["container"]["name"] == owner
            assert subprocess.run(["docker", "inspect", target_id], capture_output=True, check=False).returncode != 0
            assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            calls = (state / "fixture-calls.log").read_text().splitlines()
            assert "sendMessage" not in calls
            repeated = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                          receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert repeated["status"] == "rolled_back" and repeated["already_restored"]
            if EVIDENCE:
                evidence = {"fixture": "telegram-401-a-b-recovery", "source_a": commit,
                            "source_b": _git(root, "rev-parse", "HEAD"), "interrupted": interrupted,
                            "before": old, "restored": restored, "database": journal["database"],
                            "first_recovery": recovered, "repeated_recovery": repeated, "sdk_calls": calls}
                path = Path(EVIDENCE) / ("telegram-401-" + deployment_id + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        if mode == "pending-state-interruption":
            command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
                       "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
                       "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + update_call,
                       str(root), str(root), str(input_path), COMPOSE]
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root)}
            process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            journal_path = root / ".deployments" / runtime.JOURNAL_NAME
            registry = root / ".deployments" / "component-adopt-probes"
            deadline = time.monotonic() + 150
            probe_at_kill = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    raise AssertionError(f"update ended before state-probe interruption: {error.decode()}")
                for path in registry.glob("*.json") if registry.is_dir() else ():
                    try:
                        probe = json.loads(path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    if (probe.get("kind") in ("state-plan", "state-preimage")
                            and probe.get("transaction_id") == deployment_id
                            and probe.get("source_tool_sha256") == receipt["recovery_source"]["source_tool_sha256"]
                            and probe.get("status") == "active" and probe.get("phase") == "start"):
                        children_path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                        try:
                            children = [int(value) for value in children_path.read_text().split()]
                        except FileNotFoundError:
                            continue
                        writer = None
                        for child in children:
                            try:
                                args = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
                            except (FileNotFoundError, ProcessLookupError):
                                continue
                            if (b"start" in args and b"--attach" in args
                                    and probe.get("container_id", "").encode("ascii") in args):
                                writer = child
                                break
                        if writer is None:
                            continue
                        os.kill(writer, signal.SIGSTOP)
                        writer_stopped = False
                        for _ in range(100):
                            try:
                                status = Path(f"/proc/{writer}/status").read_text()
                            except FileNotFoundError:
                                break
                            if "State:\tT" in status:
                                writer_stopped = True
                                break
                            time.sleep(0.002)
                        if not writer_stopped:
                            try:
                                os.kill(writer, signal.SIGCONT)
                            except ProcessLookupError:
                                pass
                            continue
                        os.kill(process.pid, signal.SIGSTOP)
                        _, stopped = os.waitpid(process.pid, os.WUNTRACED)
                        assert os.WIFSTOPPED(stopped)
                        pending = json.loads(journal_path.read_text())
                        assert pending["phase"] == "prepared" and pending["migrations"] == []
                        probe_at_kill = probe
                        process.kill()
                        try:
                            blocked = subprocess.run(["flock", "-n", "-E", "99",
                                                      str(root / ".deployments" / ".deployment.lock"), "true"],
                                                     capture_output=True, check=False).returncode
                            assert blocked == 99
                        finally:
                            os.kill(writer, signal.SIGCONT)
                        process.communicate(timeout=30)
                        assert process.returncode == -signal.SIGKILL
                        released = subprocess.run(["flock", "-w", "30",
                                                   str(root / ".deployments" / ".deployment.lock"), "true"],
                                                  capture_output=True, check=False).returncode
                        assert released == 0
                        break
                if probe_at_kill is not None:
                    break
                time.sleep(0.002)
            else:
                process.kill()
                process.communicate(timeout=30)
                raise AssertionError("active state probe was not observed")
            refusal = ("import deployment_admission as a,json; "
                       "\ntry: a.admit_component(pathlib.Path(sys.argv[2]),operation='update',code_root=pathlib.Path(sys.argv[2]))"
                       "\nexcept a.AdmissionError as e: print(json.dumps({'refused':str(e)}))"
                       "\nelse: raise AssertionError('unfinished transaction was admitted')")
            refused = json.loads(_locked(root, root, refusal, str(root)))
            assert "requires recovery" in refused["refused"]
            extracted = tmp_path / "extracted-a"
            extracted.mkdir()
            extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                       "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                       "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                       "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
            assert _locked(root, root, extract, str(root), str(input_path), str(extracted)) == "ok"
            (root / "scripts" / "component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
            _git(root, "add", "scripts/component_transaction.py")
            _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "source B must not execute")
            source_b = _git(root, "rev-parse", "HEAD")
            recover_call = ("import component_transaction as t,json; "
                            "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                            "compose_path=pathlib.Path(sys.argv[5]))))")
            recovered = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                            receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert recovered["status"] == "rolled_back" and not recovered["already_restored"]
            restored = observe.observe_container(old_id, expected_image_id=image_id, project=owner,
                                                  service="wuwaterm", expected_command=["bot"],
                                                  expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert restored["container"] == old["container"]
            assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            assert _git(root, "rev-parse", "HEAD") == source_b
            probes = [json.loads(path.read_text()) for path in registry.glob("*.json")]
            assert all(item["status"] != "active" for item in probes)
            assert any(item["probe_id"] == probe_at_kill["probe_id"] and item["status"] == "failed" for item in probes)
            repeated = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                          receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert repeated["already_restored"] and repeated["status"] == "rolled_back"
            calls = (state / "fixture-calls.log").read_text().splitlines()
            assert "sendMessage" not in calls
            if EVIDENCE:
                evidence = {"fixture": "prepared-pending-probe-kill-a-b-recovery", "source_a": commit,
                            "source_b": source_b, "probe_at_kill": probe_at_kill, "pending_journal": pending,
                            "before": old, "after": restored, "database": journal["database"],
                            "first_recovery": recovered, "repeated_recovery": repeated,
                            "sdk_calls": calls, "parent_exit": -signal.SIGKILL,
                            "flock_while_child_stopped": blocked, "flock_after_child_exit": released,
                            "unfinished_admission": refused}
                path = Path(EVIDENCE) / ("pending-recovery-" + deployment_id + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        if mode.startswith("both-crash-"):
            command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
                       "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
                       "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + update_call,
                       str(root), str(root), str(input_path), COMPOSE]
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(root)}
            process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            journal_path = root / ".deployments" / runtime.JOURNAL_NAME
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    raise AssertionError(f"update ended before pointer interruption: {error.decode()}")
                try:
                    current = json.loads(journal_path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.003)
                    continue
                phase = current.get("phase")
                at_boundary = ((mode == "both-crash-after-stop" and phase == "state-validate-migrate") or
                               (mode == "both-crash-after-retire" and phase == "retire-old") or
                               (mode == "both-crash-after-manifest" and phase == "deploy-commit") or
                               (mode == "both-crash-between-pointers" and phase == "deploy-current"))
                if mode == "both-crash-partial-start" and phase == "selected-activate":
                    api_candidate = current["target"]["containers"]["api"]["id"]
                    tg_candidate = current["target"]["containers"]["telegram"]["id"]
                    if api_candidate and tg_candidate:
                        try:
                            api_running = observe._inspect(api_candidate)["State"]["Running"]
                            tg_running = observe._inspect(tg_candidate)["State"]["Running"]
                            at_boundary = api_running and not tg_running
                        except observe.ObservationError:
                            pass
                if at_boundary:
                    os.kill(process.pid, signal.SIGSTOP)
                    _, stopped = os.waitpid(process.pid, os.WUNTRACED)
                    assert os.WIFSTOPPED(stopped)
                    if mode == "both-crash-between-pointers":
                        assert (root / ".deploy_commit").read_text() == commit + "\n"
                        assert not (root / ".deploy_current").exists()
                    elif mode == "both-crash-after-manifest":
                        assert (root / current["manifest"]["path"]).is_file()
                        assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
                    elif mode == "both-crash-after-stop":
                        assert not observe._inspect(api_id)["State"]["Running"]
                        assert not observe._inspect(old_id)["State"]["Running"]
                    elif mode == "both-crash-partial-start":
                        assert observe._inspect(api_candidate)["State"]["Running"]
                        assert not observe._inspect(tg_candidate)["State"]["Running"]
                    target_api_id = current["target"]["containers"]["api"]["id"]
                    target_tg_id = current["target"]["containers"]["telegram"]["id"]
                    owned += [item for item in (target_api_id, target_tg_id) if item is not None]
                    state_before_recovery = transaction._state_preimage(str(state / "channel_replies.json"))
                    process.kill()
                    process.communicate(timeout=15)
                    assert process.returncode == -signal.SIGKILL
                    break
                time.sleep(0.003)
            else:
                process.kill()
                process.communicate(timeout=15)
                raise AssertionError("requested transaction boundary was not reached")
            extracted = tmp_path / "extracted-a"
            extracted.mkdir()
            extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                       "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                       "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                       "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
            assert _locked(root, root, extract, str(root), str(input_path), str(extracted)) == "ok"
            (root / "scripts" / "component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
            _git(root, "add", "scripts/component_transaction.py")
            _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "source B must not execute")
            source_b = _git(root, "rev-parse", "HEAD")
            recover_call = ("import component_transaction as t,json; "
                            "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                            "compose_path=pathlib.Path(sys.argv[5]))))")
            recovered = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                            receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert recovered["status"] == "rolled_back" and recovered["polling_health"] == "unconfirmed"
            assert _git(root, "rev-parse", "HEAD") == source_b
            assert (root / ".deploy_commit").read_text() == revision + "\n" and not (root / ".deploy_current").exists()
            for item in (target_api_id, target_tg_id):
                if item is not None:
                    assert subprocess.run(["docker", "inspect", item], capture_output=True, check=False).returncode != 0
            restored_api = observe.observe_container(api_id, expected_image_id=image_id, project=owner,
                                                      service="wuwaterm-api", expected_command=["api"],
                                                      expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            restored_tg = observe.observe_container(old_id, expected_image_id=image_id, project=owner,
                                                     service="wuwaterm", expected_command=["bot"],
                                                     expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert restored_api["container"]["running"] and restored_tg["container"]["running"]
            assert restored_api["container"]["name"] == owner + "-api" and restored_tg["container"]["name"] == owner
            _wait_ready(api_id)
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            assert Path(api_identity["path"]).stat().st_ino == api_identity["inode"]
            state_after_recovery = transaction._state_preimage(str(state / "channel_replies.json"))
            assert state_after_recovery["present"] == state_before_recovery["present"]
            assert state_after_recovery["sha256"] == state_before_recovery["sha256"]
            calls = (state / "fixture-calls.log").read_text().splitlines()
            assert "getUpdates" in calls and "sendMessage" not in calls
            repeated = json.loads(_locked(root, extracted, recover_call, str(root), deployment_id,
                                          receipt["recovery_source"]["source_tool_sha256"], COMPOSE))
            assert repeated["already_restored"] and repeated["status"] == "rolled_back"
            if EVIDENCE:
                evidence = {"fixture": mode + "-a-b-recovery", "source_a": commit,
                            "source_b": source_b, "journal_at_kill": current,
                            "restored_api": restored_api, "restored_telegram": restored_tg,
                            "state_before_recovery": state_before_recovery, "state_after_recovery": state_after_recovery,
                            "first_recovery": recovered, "repeated_recovery": repeated,
                            "database": journal["database"], "api_identity": api_identity,
                            "sdk_calls": calls, "parent_exit": -signal.SIGKILL}
                path = Path(EVIDENCE) / ("both-recovery-" + deployment_id + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        updated = json.loads(_locked(root, root, update_call, str(root), str(input_path), COMPOSE))
        assert updated["status"] == "committed" and updated["retirement"] == "complete"
        stored = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
        target_id = stored["target"]["containers"]["telegram"]["id"]
        owned.append(target_id)
        if tg_selected:
            target = observe.observe_container(target_id, expected_image_id=receipt["image_id"], project=owner,
                                               service="wuwaterm-telegram", expected_command=["--db", "/app/data/terms.db"],
                                               expected_entrypoint=["wuwaterm-telegram"], env_fingerprint_key=b"x" * 32)
            assert target["container"]["running"] is running and target["container"]["name"] == owner + "-new"
        else:
            target = observe.observe_container(target_id, expected_image_id=image_id, project=owner,
                                               service="wuwaterm", expected_command=["bot"],
                                               expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert target["container"] == old["container"]
        if running:
            calls = (state / "fixture-calls.log").read_text().splitlines()
            assert "getUpdates" in calls and "sendMessage" not in calls
            if tg_selected:
                health = _docker("exec", target_id, "python", "-B", "-m", "wuwaterm_telegram.operations", "health")
                assert json.loads(health)["status"] == "ready"
        stale_refusal = None
        crash_refusal = None
        if mode == "tg-stale":
            _docker("kill", "--signal", "STOP", target_id)
            frozen_target = target_id
            stale = ("import json,pathlib; p=pathlib.Path('/run/wuwaterm-telegram/poll.json'); "
                     "x=json.loads(p.read_text()); x['success_monotonic_ns']=1; p.write_text(json.dumps(x))")
            _docker("exec", target_id, "python", "-B", "-c", stale)
            check = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                     "snap=t.manifest.read_recipe_snapshot(root,sys.argv[4]); "
                     "\ntry: t._telegram_ready(sys.argv[3],snap,timeout_seconds=2)"
                     "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                     "\nelse: raise AssertionError('stale poll record was accepted')")
            stale_refusal = json.loads(_locked(root, root, check, str(root), target_id, snap_digest))
            assert stale_refusal["error"] == "Telegram SDK polling readiness did not succeed"
            _docker("kill", "--signal", "CONT", target_id)
            frozen_target = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                healthy = subprocess.run(["docker", "exec", target_id, "python", "-B", "-m",
                                          "wuwaterm_telegram.operations", "health"],
                                         capture_output=True, check=False)
                if healthy.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("polling did not resume after stale-record check")
            _docker("kill", "--signal", "KILL", target_id)
            crash_refusal = json.loads(_locked(root, root, check, str(root), target_id, snap_digest))
            assert crash_refusal["error"] == "Telegram process is not running"
        if tg_selected:
            assert subprocess.run(["docker", "inspect", old_id], capture_output=True, check=False).returncode != 0
        assert manifest.read_manifest(root, deployment_id) == target_manifest
        assert (root / ".deploy_commit").read_text() == commit + "\n"
        assert (root / ".deploy_current").read_text() == deployment_id + "\n"
        assert manifest.verify_database_binding(database) == journal["database"]["stat"]
        if env_files:
            assert Path(env_files[0]).stat().st_mode & 0o777 == 0o600
        if mode == "root-owned-state":
            private_state = state / "channel_replies.json"
            info = private_state.stat()
            assert info.st_uid == 0 and info.st_mode & 0o777 == 0o600
            assert os.geteuid() != 0 and not os.access(private_state, os.R_OK)
        new_api = None
        if api_selected:
            api_target_id = stored["target"]["containers"]["api"]["id"]
            owned.append(api_target_id)
            new_api = observe.observe_container(api_target_id, expected_image_id=api_build["receipt"]["image_id"],
                                                project=owner, service="wuwaterm-api", expected_command=["api"],
                                                expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert new_api["container"]["running"]
            _wait_ready(api_target_id)
            assert subprocess.run(["docker", "inspect", api_id], capture_output=True, check=False).returncode != 0
            assert Path(api_identity["path"]).stat().st_ino == api_identity["inode"]
        elif has_api:
            new_api = observe.observe_container(api_id, expected_image_id=image_id, project=owner,
                                                service="wuwaterm-api", expected_command=["api"],
                                                expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert new_api["container"] == api_old["container"]
            assert Path(api_identity["path"]).stat().st_ino == api_identity["inode"]
        if EVIDENCE:
            record = {"fixture": "actual-telegram-wheel-state-preflight", "receipt_sha256": digest,
                      "source": receipt["source"], "core_source": receipt["core_source"],
                      "old_observation": old, "result": result, "database": journal["database"],
                      "copy_if_absent_refusal": rejected, "update": updated, "target_observation": target,
                      "running_loopback_sdk": running,
                      "both_selected": both, "api_target_observation": new_api,
                      "stale_refusal": stale_refusal, "crash_refusal": crash_refusal,
                      "sdk_calls": (state / "fixture-calls.log").read_text().splitlines() if running else []}
            path = Path(EVIDENCE) / ("telegram-preflight-" + deployment_id + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(record, output, sort_keys=True, indent=2)
                output.write("\n")
    finally:
        if frozen_target is not None:
            _docker("kill", "--signal", "CONT", frozen_target)
        if process is not None and process.poll() is None:
            if writer is not None:
                try:
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                    if str(writer) in children:
                        os.kill(writer, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except FileNotFoundError:
                    pass
            process.kill()
            process.communicate(timeout=30)
        matches = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner)
        owned.extend(item for item in matches.splitlines() if item not in owned)
        _owned_cleanup(owned, owner)


@pytest.mark.skipif(not DOCKER_FIXTURE or not RUN_BUILD, reason="real adoption and producer builds not requested")
@pytest.mark.parametrize("mode", ["adopt-api", "adopt-restarted-api", "adopt-telegram", "adopt-progressed-tg", "adopt-both",
                                  "adopt-crash-manifest", "adopt-crash-pointers",
                                  "adopt-archive-before", "adopt-archive-published", "adopt-replace-published",
                                  "adopt-archive-conflict", "terminal-env-drift", "terminal-extra-id",
                                  "terminal-image-change"])
def test_actual_adoption_successor_and_recovery(tmp_path: Path, mode: str) -> None:
    assert SOURCE_FIXTURE and COMPOSE and IMAGE
    root = tmp_path / "deployment"
    shutil.copytree(SOURCE_FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "actual adoption lifecycle source A " + uuid.uuid4().hex)
    commit, tree = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
    (root / ".deployments").mkdir()
    _run(["sha256sum", "-c", Path(COMPOSE).name + ".sha256"], cwd=Path(COMPOSE).parent)
    api_code = ("import component_build as b,json; root=pathlib.Path(sys.argv[2]); "
                "digest=b.build(root,sys.argv[3],sys.argv[4]); "
                "print(json.dumps({'digest':digest,'receipt':b.verify_registered_build(root,digest)}))")
    api_build = json.loads(_locked(root, root, api_code, str(root), commit, tree))
    _preserve_producer("api", api_build["digest"], api_build["receipt"])
    wheel_build = None
    if mode in ("adopt-telegram", "adopt-progressed-tg", "adopt-both"):
        names = ("TG_REPO", "TG_COMMIT", "TG_TREE", "CORE_WHEEL", "CORE_ARTIFACT", "WHEELHOUSE", "BUILD_PYTHON")
        config = {key: os.environ["WUWATERM_TRANSACTION_" + key] for key in names}
        code = ("import component_wheel_build as w,json; root=pathlib.Path(sys.argv[2]); "
                "d=w.build_wheel_image(root,sys.argv[3],sys.argv[4],pathlib.Path(sys.argv[5]),sys.argv[6],"
                "sys.argv[7],pathlib.Path(sys.argv[8]),pathlib.Path(sys.argv[9]),pathlib.Path(sys.argv[10])); "
                "print(json.dumps({'digest':d,'receipt':w.verify_registered_wheel_build(root,d)}))")
        wheel_build = json.loads(_locked_python(root, config["BUILD_PYTHON"], code, str(root), commit, tree,
                                               config["TG_REPO"], config["TG_COMMIT"], config["TG_TREE"],
                                               config["CORE_WHEEL"], config["CORE_ARTIFACT"], config["WHEELHOUSE"]))
        _preserve_producer("telegram", wheel_build["digest"], wheel_build["receipt"])
    crashing = mode.startswith("adopt-crash-")
    old_ref = api_build["receipt"]["image_ref"] if crashing else IMAGE
    old_image = json.loads(_docker("image", "inspect", old_ref))[0]
    old_image_id = old_image["Id"]
    old_revision = old_image["Config"]["Labels"]["org.opencontainers.image.revision"]
    owner = "adopt-tx-" + uuid.uuid4().hex[:16]
    data, tg_state, api_state = root / "fixture-data", root / "fixture-state", root / "fixture-api-state"
    for directory in (data, tg_state, api_state):
        directory.mkdir()
    database = make_database(data / "terms.db")
    if mode != "adopt-restarted-api":
        (api_state / "devices.db").write_bytes(b"synthetic-device-store\n")
    env_file = root / ".fixture.env"
    env_file.write_text("FIXTURE_VALUE=local-only\n", encoding="ascii")
    env_file.chmod(0o600)
    old_commit = None
    if not crashing:
        _legacy_digest(root, old_revision, old_image_id, old_ref, database)
        (root / ".deploy_commit").write_text(old_revision + "\n", encoding="ascii")
        old_commit = old_revision + "\n"
    services = {"wuwaterm-api": {
        "image": old_ref, "container_name": owner + "-api", "network_mode": "none",
        "labels": {"org.wuwaterm.test-owner": owner}, "env_file": [str(env_file)],
        "environment": {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_API_STATE_DIR": "/app/state-api"},
        "volumes": [str(data) + ":/app/data:ro", str(api_state) + ":/app/state-api"], "command": ["api"],
    }}
    if not crashing:
        services["wuwaterm"] = {
            "image": old_ref, "container_name": owner + "-tg", "network_mode": "none",
            "labels": {"org.wuwaterm.test-owner": owner}, "env_file": [str(env_file)],
            "environment": {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_STATE_DIR": "/app/state",
                            "WUWATERM_SETTINGS_PATH": "/app/state/chat_settings.json",
                            "WUWATERM_CHANNEL_REPLY_INDEX_PATH": "/app/state/channel_replies.json"},
            "volumes": [str(data) + ":/app/data:ro", str(tg_state) + ":/app/state"], "command": ["bot"],
        }
    config_path = root / "fixture-compose.json"
    config_path.write_text(json.dumps({"services": services}), encoding="utf-8")
    compose_command = [COMPOSE, "--project-name", owner, "--project-directory", str(root),
                       "--file", str(config_path), "create"]
    _run(compose_command, cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8"})
    owned = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner).splitlines()
    assert len(owned) == len(services)
    if mode == "adopt-restarted-api":
        api_identifier = next(identifier for identifier in owned
                              if observe._inspect(identifier)["Config"]["Labels"]["com.docker.compose.service"] == "wuwaterm-api")
        _docker("start", api_identifier)
        _wait_ready(api_identifier)
    before: dict[str, dict] = {}
    for identifier in owned:
        raw = observe._inspect(identifier)
        service = raw["Config"]["Labels"]["com.docker.compose.service"]
        name = "api" if service == "wuwaterm-api" else "telegram"
        before[name] = observe.observe_container(identifier, expected_image_id=old_image_id, project=owner,
                                                service=service, expected_command=raw["Config"]["Cmd"],
                                                expected_entrypoint=raw["Config"]["Entrypoint"],
                                                env_fingerprint_key=b"x" * 32)
    adopt_code = ("import component_runtime as r,json; root=pathlib.Path(sys.argv[2]); "
                  "receipt=None if sys.argv[5]=='-' else sys.argv[5]; "
                  "d=r.adopt(root,projects=[sys.argv[3]],compose_executable=pathlib.Path(sys.argv[4]),"
                  "api_receipt=receipt,telegram_receipt=None); print(json.dumps({'deployment_id':d}))")
    api_receipt = api_build["digest"] if crashing else "-"
    process: subprocess.Popen | None = None
    try:
        if crashing:
            command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
                       "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
                       "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + adopt_code,
                       str(root), str(root), owner, COMPOSE, api_receipt]
            process = subprocess.Popen(command, cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(root),
                                       "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            path = root / ".deployments" / runtime.JOURNAL_NAME
            wanted = "deploy-commit" if mode == "adopt-crash-manifest" else "deploy-current"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    raise AssertionError(f"adoption ended before interruption: {error.decode()}")
                try:
                    journal = json.loads(path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.001)
                    continue
                if journal.get("phase") == wanted:
                    os.kill(process.pid, signal.SIGSTOP)
                    _, stopped = os.waitpid(process.pid, os.WUNTRACED)
                    assert os.WIFSTOPPED(stopped)
                    journal = json.loads(path.read_text())
                    assert journal["phase"] == wanted
                    target_path = root / journal["manifest"]["path"]
                    target_raw = target_path.read_bytes()
                    assert hashlib.sha256(target_raw).hexdigest() == journal["manifest"]["sha256"]
                    if wanted == "deploy-commit":
                        assert not (root / ".deploy_commit").exists()
                    else:
                        assert (root / ".deploy_commit").read_text() == commit + "\n"
                    assert not (root / ".deploy_current").exists()
                    process.kill()
                    process.communicate(timeout=30)
                    assert process.returncode == -signal.SIGKILL
                    break
                time.sleep(0.001)
            else:
                process.kill()
                process.communicate(timeout=30)
                raise AssertionError("adoption interruption boundary was not reached")
            extracted = tmp_path / "extracted-a"
            extracted.mkdir()
            extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                       "s=r._journal(root,sys.argv[3])[1]['source']; "
                       "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                       "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
            assert _locked(root, root, extract, str(root), journal["transaction_id"], str(extracted)) == "ok"
            (root / "scripts/component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
            _git(root, "add", "scripts/component_transaction.py")
            _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "-m", "source B must not execute")
            source_b = _git(root, "rev-parse", "HEAD")
            recover_code = ("import component_transaction as t,json; "
                            "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                            "compose_path=pathlib.Path(sys.argv[5]))))")
            args = (str(root), journal["transaction_id"], journal["source"]["source_tool_sha256"], COMPOSE)
            recovered = json.loads(_locked(root, extracted, recover_code, *args))
            assert recovered["status"] == "rolled_back" and not recovered["already_restored"]
            assert not (root / ".deploy_commit").exists() and not (root / ".deploy_current").exists()
            assert target_path.read_bytes() == target_raw and not target_path.stat().st_mode & 0o222
            after = observe.observe_container(before["api"]["container"]["id"], expected_image_id=old_image_id,
                                              project=owner, service="wuwaterm-api", expected_command=["api"],
                                              expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert after["container"] == before["api"]["container"]
            assert manifest.verify_database_binding(database) == journal["database"]["stat"]
            assert _git(root, "rev-parse", "HEAD") == source_b
            repeated = json.loads(_locked(root, extracted, recover_code, *args))
            assert repeated["status"] == "rolled_back" and repeated["already_restored"]
            if EVIDENCE:
                evidence = {"fixture": mode, "source_a": commit, "source_b": source_b, "at_kill": journal,
                            "before": before, "after": after, "first_recovery": recovered,
                            "repeated_recovery": repeated, "parent_exit": -signal.SIGKILL}
                p = Path(EVIDENCE) / (mode + "-" + journal["transaction_id"] + ".json")
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        adopted = json.loads(_locked(root, root, adopt_code, str(root), owner, COMPOSE, api_receipt))
        predecessor_path = root / ".deployments" / runtime.JOURNAL_NAME
        predecessor_raw = predecessor_path.read_bytes()
        predecessor = runtime.load_journal(predecessor_path)
        assert predecessor["action"] == "adopt" and predecessor["status"] == "committed"
        assert predecessor["transaction_id"] == adopted["deployment_id"]
        if mode.startswith("terminal-"):
            if mode == "terminal-env-drift":
                env_file.write_text("FIXTURE_VALUE=changed-after-adoption\n", encoding="ascii")
                expected_error = "terminal environment no longer matches its references"
            elif mode == "terminal-extra-id":
                extra = _docker("create", "--name", owner + "-unexpected", "--network", "none",
                                "--label", "org.wuwaterm.test-owner=" + owner,
                                "--label", "com.docker.compose.project=" + owner,
                                "--label", "com.docker.compose.service=wuwaterm-api", old_ref, "api")
                owned.append(extra)
                expected_error = "terminal container inventory changed"
            else:
                original_id = before["api"]["container"]["id"]
                original_raw = observe._inspect(original_id)
                assert not original_raw["State"]["Running"]
                assert original_raw["Config"]["Labels"]["org.wuwaterm.test-owner"] == owner
                _docker("rm", original_id)
                replacement = _docker("create", "--name", owner + "-api", "--network", "none",
                                      "--label", "org.wuwaterm.test-owner=" + owner,
                                      "--label", "com.docker.compose.project=" + owner,
                                      "--label", "com.docker.compose.service=wuwaterm-api",
                                      api_build["receipt"]["image_ref"], "api")
                owned.append(replacement)
                expected_error = "terminal deployment cannot be verified"
            fixture_ids = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner).splitlines()
            fixture_state = {identifier: observe._inspect(identifier)["State"]["StartedAt"] for identifier in fixture_ids}
            refusing = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                        "\ntry: t.verify_terminal(root,compose_path=pathlib.Path(sys.argv[3]))"
                        "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                        "\nelse: raise AssertionError('terminal drift was accepted')")
            refused = json.loads(_locked(root, root, refusing, str(root), COMPOSE))
            assert refused["error"] == expected_error
            assert predecessor_path.read_bytes() == predecessor_raw
            assert (root / ".deploy_commit").read_text() == commit + "\n"
            assert (root / ".deploy_current").read_text() == predecessor["transaction_id"] + "\n"
            assert {identifier: observe._inspect(identifier)["State"]["StartedAt"] for identifier in fixture_ids} == fixture_state
            assert manifest.verify_database_binding(database) == predecessor["database"]["stat"]
            if EVIDENCE:
                evidence = {"fixture": mode, "source_a": commit,
                            "predecessor_sha256": hashlib.sha256(predecessor_raw).hexdigest(),
                            "fixture_containers": fixture_ids, "refusal": refused}
                p = Path(EVIDENCE) / (mode + "-" + predecessor["transaction_id"] + ".json")
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        if mode == "adopt-restarted-api":
            api_identifier = before["api"]["container"]["id"]
            _docker("restart", api_identifier)
            _wait_ready(api_identifier)
            fresh = observe.observe_container(api_identifier, expected_image_id=old_image_id, project=owner,
                                              service="wuwaterm-api", expected_command=["api"],
                                              expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=b"x" * 32)
            assert fresh["container"]["id"] == predecessor["target"]["containers"]["api"]["id"]
            assert fresh["container"]["started_at"] != predecessor["target"]["containers"]["api"]["started_at"]
            before["api"] = fresh
        if mode == "adopt-progressed-tg":
            (tg_state / "chat_settings.json").write_text('{"public":{},"allowed":[]}\n', encoding="ascii")
        target = copy.deepcopy(predecessor["target"]["manifest"])
        deployment_id = manifest.new_deployment_id()
        target["deployment_id"] = deployment_id
        selection = ({"adopt-api": ["api"], "adopt-restarted-api": ["api"],
                      "adopt-telegram": ["telegram"], "adopt-progressed-tg": ["telegram"],
                      "adopt-both": ["api", "telegram"]}.get(mode) or ["api"])
        fresh_containers = {name: before[name]["container"] for name in ("api", "telegram")}
        records = copy.deepcopy(fresh_containers)
        if "api" in selection:
            target["components"]["api"] = _built_component(root, target["components"]["api"], api_build, commit, tree)
            records["api"].update(id=None, image_id=api_build["receipt"]["image_id"], name=None,
                                  started_at=None, restart_count=None, transaction_label=deployment_id)
        if "telegram" in selection:
            receipt = wheel_build["receipt"]
            snap = manifest.read_recipe_snapshot(root, target["components"]["telegram"]["recipe"]["sha256"])
            snap.update(image_id=receipt["image_id"], service="wuwaterm-telegram", container_name=owner + "-tg-new",
                        command=["--db", "/app/data/terms.db"], entrypoint=["wuwaterm-telegram"])
            snap["mounts"].append({"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                                   "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700"]})
            snap["environment_overrides"].append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})
            snapshot_path, digest = manifest.write_recipe_snapshot(root, snap)
            recipe = {key: snap[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=snapshot_path, sha256=digest)
            source = {"revision_claim": receipt["source"]["commit"], "verified_commit": receipt["source"]["commit"],
                      "verified_tree": receipt["source"]["tree"]}
            core = {"version": receipt["core_version"], "revision_claim": receipt["core_source"]["commit"],
                    "verified_commit": receipt["core_source"]["commit"], "verified_tree": receipt["core_source"]["tree"]}
            provenance = {key: receipt[key] for key in ("telegram_wheel_sha256", "core_wheel_sha256",
                                                        "core_lock_sha256", "core_producer_kind")}
            provenance.update(kind="wheel-image", build_receipt_sha256=wheel_build["digest"])
            target["components"]["telegram"] = {
                "repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
                "source": source, "core": core,
                "image": {"ref": receipt["image_ref"], "id": receipt["image_id"],
                          "digest": receipt["image_digest"], "revision": receipt["image_revision"]},
                "recipe": recipe, "provenance": provenance,
            }
            records["telegram"].update(id=None, image_id=receipt["image_id"], service="wuwaterm-telegram",
                                       name=None, started_at=None, restart_count=None, transaction_label=deployment_id)
        raw = manifest._encode(target)
        successor = {"schema_version": 2, "kind": "component-runtime", "transaction_id": deployment_id,
                     "action": "update", "status": "active", "phase": "prepared", "source": predecessor["source"],
                     "selection": selection,
                     "before": {"manifest": predecessor["target"]["manifest"],
                                "topology": predecessor["target"]["manifest"]["components"],
                                "containers": fresh_containers},
                     "target": {"manifest": target, "containers": records}, "database": predecessor["database"],
                     "api_identity": predecessor["api_identity"], "migrations": [],
                     "pointers": {"old": predecessor["pointers"]["target"],
                                  "target": {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                                             ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}},
                     "manifest": {"deployment_id": deployment_id,
                                  "path": f".deployments/products/{deployment_id}.json",
                                  "sha256": hashlib.sha256(raw).hexdigest()}}
        runtime.validate_journal(successor)
        input_path = root / ".deployments" / "successor-input.json"
        input_path.write_text(json.dumps(successor), encoding="ascii")
        update_code = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                       "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                       "print(json.dumps(t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))))")
        if mode == "adopt-archive-conflict":
            history_dir = root / ".deployments/component-runtime-history"
            history_dir.mkdir()
            history = history_dir / (predecessor["transaction_id"] + ".json")
            history.write_bytes(b"different immutable history\n")
            history.chmod(0o400)
            refusing = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                        "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                        "\ntry: t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))"
                        "\nexcept t.TransactionError as e: print(json.dumps({'error':str(e)}))"
                        "\nelse: raise AssertionError('unequal history was accepted')")
            refused = json.loads(_locked(root, root, refusing, str(root), str(input_path), COMPOSE))
            assert refused["error"] == "initial runtime journal cannot be published"
            assert history.read_bytes() == b"different immutable history\n"
            assert predecessor_path.read_bytes() == predecessor_raw
            assert (root / ".deploy_commit").read_text() == commit + "\n"
            assert (root / ".deploy_current").read_text() == predecessor["transaction_id"] + "\n"
            for original in before.values():
                item = original["container"]
                actual = observe.observe_container(item["id"], expected_image_id=item["image_id"],
                                                   project=item["project"], service=item["service"],
                                                   expected_command=original["recipe"]["command"],
                                                   expected_entrypoint=original["recipe"]["entrypoint"],
                                                   env_fingerprint_key=b"x" * 32)
                assert actual["container"] == item
            assert manifest.verify_database_binding(database) == predecessor["database"]["stat"]
            assert env_file.stat().st_mode & 0o777 == 0o600
            assert Path(predecessor["api_identity"]["path"]).stat().st_ino == predecessor["api_identity"]["inode"]
            if EVIDENCE:
                evidence = {"fixture": mode, "source_a": commit, "predecessor": predecessor,
                            "predecessor_sha256": hashlib.sha256(predecessor_raw).hexdigest(), "refusal": refused}
                p = Path(EVIDENCE) / (mode + "-" + deployment_id + ".json")
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        if mode.startswith("adopt-archive-") or mode == "adopt-replace-published":
            boundary = {"adopt-archive-before": "before-archive", "adopt-archive-published": "after-archive",
                        "adopt-replace-published": "after-replace"}[mode]
            proof = _interrupt_initial_writer(root, update_code, input_path, predecessor["transaction_id"], boundary)
            history = root / ".deployments/component-runtime-history" / (predecessor["transaction_id"] + ".json")
            if boundary == "before-archive":
                assert not history.exists() and predecessor_path.read_bytes() == predecessor_raw
            elif boundary == "after-archive":
                assert history.read_bytes() == predecessor_raw and predecessor_path.read_bytes() == predecessor_raw
            else:
                assert history.read_bytes() == predecessor_raw
                active = runtime.load_journal(predecessor_path)
                assert active["status"] == "active" and active["phase"] == "prepared"
                assert active["transaction_id"] == deployment_id
            for name, original in before.items():
                item = original["container"]
                actual = observe.observe_container(item["id"], expected_image_id=item["image_id"],
                                                   project=item["project"], service=item["service"],
                                                   expected_command=predecessor["before"]["topology"][name]["recipe"]["command"],
                                                   expected_entrypoint=predecessor["before"]["topology"][name]["recipe"]["entrypoint"],
                                                   env_fingerprint_key=b"x" * 32)
                assert actual["container"] == item
            assert (root / ".deploy_commit").read_text() == commit + "\n"
            assert (root / ".deploy_current").read_text() == predecessor["transaction_id"] + "\n"
            assert manifest.verify_database_binding(database) == predecessor["database"]["stat"]
            if boundary == "after-replace":
                extracted = tmp_path / "extracted-a"
                extracted.mkdir()
                extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                           "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                           "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                           "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
                assert _locked(root, root, extract, str(root), str(input_path), str(extracted)) == "ok"
                recover_code = ("import component_transaction as t,json; "
                                "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                                "compose_path=pathlib.Path(sys.argv[5]))))")
                recovered = json.loads(_locked(root, extracted, recover_code, str(root), deployment_id,
                                               successor["source"]["source_tool_sha256"], COMPOSE))
                assert recovered["status"] == "rolled_back"
                assert history.read_bytes() == predecessor_raw
            else:
                retried = json.loads(_locked(root, root, update_code, str(root), str(input_path), COMPOSE))
                assert retried["status"] == "committed"
                assert history.read_bytes() == predecessor_raw
            if EVIDENCE:
                evidence = {"fixture": mode, "boundary": proof, "predecessor_sha256": hashlib.sha256(predecessor_raw).hexdigest(),
                            "source": commit, "before": before, "successor_id": deployment_id,
                            "database": predecessor["database"]}
                p = Path(EVIDENCE) / (mode + "-" + deployment_id + ".json")
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump(evidence, output, sort_keys=True, indent=2)
                    output.write("\n")
            return
        updated = json.loads(_locked(root, root, update_code, str(root), str(input_path), COMPOSE))
        assert updated["status"] == "committed" and updated["retirement"] == "complete"
        history = root / ".deployments/component-runtime-history" / (predecessor["transaction_id"] + ".json")
        assert history.read_bytes() == predecessor_raw and not history.stat().st_mode & 0o222
        current = runtime.load_journal(predecessor_path)
        assert current["status"] == "committed" and current["selection"] == selection
        for name in ("api", "telegram"):
            record = current["target"]["containers"][name]
            actual = observe.observe_container(record["id"], expected_image_id=record["image_id"],
                                               project=record["project"], service=record["service"],
                                               expected_command=target["components"][name]["recipe"]["command"],
                                               expected_entrypoint=target["components"][name]["recipe"]["entrypoint"],
                                               env_fingerprint_key=b"x" * 32)
            assert actual["container"]["running"] == before[name]["container"]["running"]
            if name not in selection:
                assert actual["container"] == before[name]["container"]
        assert manifest.verify_database_binding(database) == predecessor["database"]["stat"]
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert (root / ".deploy_commit").read_text() == commit + "\n"
        assert (root / ".deploy_current").read_text() == deployment_id + "\n"
        progressed_result = None
        if mode == "adopt-progressed-tg":
            assert next(item for item in current["migrations"] if item["kind"] == "chat-settings")["target_before"]["present"]
            state_file = tg_state / "chat_settings.json"
            assert state_file.read_text(encoding="ascii") == '{"public":{},"allowed":[]}\n'
            state_file.unlink()
            terminal_code = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                             "r=t.verify_terminal(root,compose_path=pathlib.Path(sys.argv[3])); "
                             "print(json.dumps({'status':r[1]['status'],'id':r[1]['transaction_id']}))")
            assert json.loads(_locked(root, root, terminal_code, str(root), COMPOSE)) == {
                "status": "committed", "id": deployment_id,
            }
            next_id = manifest.new_deployment_id()
            next_manifest = copy.deepcopy(target)
            next_manifest["deployment_id"] = next_id
            next_records = copy.deepcopy(current["target"]["containers"])
            next_records["telegram"].update(id=None, name=None, started_at=None, restart_count=None,
                                            transaction_label=next_id)
            next_journal = copy.deepcopy(successor)
            next_journal["transaction_id"] = next_id
            next_journal["before"] = {"manifest": target, "topology": target["components"],
                                      "containers": current["target"]["containers"]}
            next_journal["target"] = {"manifest": next_manifest, "containers": next_records}
            next_journal["pointers"]["old"] = copy.deepcopy(successor["pointers"]["target"])
            next_journal["pointers"]["target"][".deploy_current"]["bytes"] = next_id + "\n"
            next_journal["manifest"] = {"deployment_id": next_id, "path": f".deployments/products/{next_id}.json",
                                        "sha256": hashlib.sha256(manifest._encode(next_manifest)).hexdigest()}
            runtime.validate_journal(next_journal)
            input_path.write_text(json.dumps(next_journal), encoding="ascii")
            progressed_result = json.loads(_locked(root, root, update_code, str(root), str(input_path), COMPOSE))
            assert progressed_result["status"] == "committed" and progressed_result["retirement"] == "complete"
            assert not state_file.exists()
            assert manifest.verify_database_binding(database) == predecessor["database"]["stat"]
        if EVIDENCE:
            evidence = {"fixture": mode, "source_a": commit, "adopted": predecessor,
                        "adopted_raw_sha256": hashlib.sha256(predecessor_raw).hexdigest(),
                        "successor": current, "before": before, "update": updated, "old_commit": old_commit,
                        "progressed_state_retry": progressed_result}
            p = Path(EVIDENCE) / (mode + "-" + deployment_id + ".json")
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(evidence, output, sort_keys=True, indent=2)
                output.write("\n")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=30)
        matches = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner)
        owned.extend(item for item in matches.splitlines() if item not in owned)
        _owned_cleanup(owned, owner)


@pytest.mark.skipif(not DOCKER_FIXTURE or not RUN_BUILD, reason="real unadopted retry requires Docker builds")
@pytest.mark.parametrize("interrupt_b_probe", [False, True], ids=["retry", "interrupted-b-probe"])
def test_unadopted_rollback_a_to_b_uses_fresh_b_legacy_probe(tmp_path: Path, interrupt_b_probe: bool) -> None:
    assert SOURCE_FIXTURE and COMPOSE
    names = ("TG_REPO", "TG_COMMIT", "TG_TREE", "CORE_WHEEL", "CORE_ARTIFACT", "WHEELHOUSE", "BUILD_PYTHON")
    config = {key: os.environ["WUWATERM_TRANSACTION_" + key] for key in names}
    root = tmp_path / "deployment"
    shutil.copytree(SOURCE_FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-q", "-m", "unadopted retry source A " + uuid.uuid4().hex)
    commit_a, tree_a = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
    (root / ".deployments").mkdir()
    api_code = ("import component_build as b,json; root=pathlib.Path(sys.argv[2]); "
                "d=b.build(root,sys.argv[3],sys.argv[4]); print(json.dumps({'digest':d,'receipt':b.verify_registered_build(root,d)}))")
    api_build = json.loads(_locked(root, root, api_code, str(root), commit_a, tree_a))
    _preserve_producer("api", api_build["digest"], api_build["receipt"])
    wheel_code = ("import component_wheel_build as w,json; root=pathlib.Path(sys.argv[2]); "
                  "d=w.build_wheel_image(root,sys.argv[3],sys.argv[4],pathlib.Path(sys.argv[5]),sys.argv[6],"
                  "sys.argv[7],pathlib.Path(sys.argv[8]),pathlib.Path(sys.argv[9]),pathlib.Path(sys.argv[10])); "
                  "print(json.dumps({'digest':d,'receipt':w.verify_registered_wheel_build(root,d)}))")

    def wheel_for(commit: str, tree: str) -> dict:
        result = json.loads(_locked_python(root, config["BUILD_PYTHON"], wheel_code, str(root), commit, tree,
                                          config["TG_REPO"], config["TG_COMMIT"], config["TG_TREE"],
                                          config["CORE_WHEEL"], config["CORE_ARTIFACT"], config["WHEELHOUSE"]))
        _preserve_producer("telegram", result["digest"], result["receipt"])
        return result

    wheel_a = wheel_for(commit_a, tree_a)
    old_image = api_build["receipt"]["image_id"]
    old_ref = api_build["receipt"]["image_ref"]
    data, state = root / "fixture-data", root / "fixture-state"
    data.mkdir()
    state.mkdir()
    database = make_database(data / "terms.db")
    legacy_hash = _legacy_digest(root, commit_a, old_image, old_ref, database)
    (root / ".deploy_commit").write_text(commit_a + "\n", encoding="ascii")
    owner = "unadopted-retry-" + uuid.uuid4().hex[:16]
    values = {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_STATE_DIR": "/app/state",
              "WUWATERM_SETTINGS_PATH": "/app/state/chat_settings.json",
              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": "/app/state/channel_replies.json"}
    overrides = [{"name": key, "mode": "literal", "value": value} for key, value in values.items()]
    args = ["create", "--name", owner, "--network", "none",
            "--label", "org.wuwaterm.test-owner=" + owner,
            "--label", "com.docker.compose.project=" + owner,
            "--label", "com.docker.compose.service=wuwaterm",
            "--mount", f"type=bind,src={data},dst=/app/data,readonly",
            "--mount", f"type=bind,src={state},dst=/app/state"]
    for key, value in values.items():
        args += ["--env", key + "=" + value]
    owned: list[str] = []
    process: subprocess.Popen | None = None
    try:
        old_id = _docker(*args, old_ref, "bot")
        owned.append(old_id)
        old = observe.observe_container(old_id, expected_image_id=old_image, project=owner, service="wuwaterm",
                                        expected_command=["bot"], expected_entrypoint=["/entrypoint.sh"],
                                        env_fingerprint_key=b"x" * 32)
        legacy = _component(root, old_image, old_ref, commit_a, old, overrides,
                            legacy=True, legacy_digest=legacy_hash)

        def successor(source: dict, built: dict, original: dict) -> dict:
            identifier = manifest.new_deployment_id()
            component = _wheel_component(root, legacy, built, owner + "-new")
            product = {"schema_version": 2, "deployment_id": identifier,
                       "deployment_utc": "2026-11-06T11:00:00Z",
                       "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": source["commit"],
                                      "source_tree": source["tree"], "source_tool_sha256": source["source_tool_sha256"]},
                       "components": {"api": None, "telegram": component}, "database": database, "api_identity": None}
            target_record = copy.deepcopy(original)
            target_record.update(id=None, image_id=built["receipt"]["image_id"], service="wuwaterm-telegram",
                                 name=None, started_at=None, restart_count=None, transaction_label=identifier)
            raw = manifest._encode(product)
            return {"schema_version": 2, "kind": "component-runtime", "transaction_id": identifier,
                    "action": "update", "status": "active", "phase": "prepared", "source": source,
                    "selection": ["telegram"],
                    "before": {"manifest": None, "topology": {"api": None, "telegram": legacy},
                               "containers": {"api": None, "telegram": original}},
                    "target": {"manifest": product, "containers": {"api": None, "telegram": target_record}},
                    "database": {"binding": database, "stat": manifest.verify_database_binding(database)},
                    "api_identity": None, "migrations": [],
                    "pointers": {"old": {".deploy_commit": {"present": True, "bytes": commit_a + "\n"},
                                         ".deploy_current": {"present": False, "bytes": None}},
                                 "target": {".deploy_commit": {"present": True, "bytes": source["commit"] + "\n"},
                                            ".deploy_current": {"present": True, "bytes": identifier + "\n"}}},
                    "manifest": {"deployment_id": identifier,
                                 "path": f".deployments/products/{identifier}.json",
                                 "sha256": hashlib.sha256(raw).hexdigest()}}

        first = successor(wheel_a["receipt"]["recovery_source"], wheel_a, old["container"])
        runtime.validate_journal(first)
        input_path = root / ".deployments" / "fixture-input.json"
        input_path.write_text(json.dumps(first), encoding="ascii")
        update_code = ("import component_transaction as t,json; root=pathlib.Path(sys.argv[2]); "
                       "j=json.loads(pathlib.Path(sys.argv[3]).read_text()); "
                       "print(json.dumps(t.update(root,j,compose_path=pathlib.Path(sys.argv[4]))))")

        def start_update() -> subprocess.Popen:
            command = ["bash", "-c", 'exec 9>"$1/.deployments/.deployment.lock"; flock -x 9; shift; exec "$@"',
                       "fixture-lock", str(root), sys.executable, "-I", "-B", "-c",
                       "import pathlib,sys; sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'scripts')); " + update_code,
                       str(root), str(root), str(input_path), COMPOSE]
            return subprocess.Popen(command, cwd=root, env={"PATH": os.environ["PATH"], "HOME": str(root),
                                    "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process = start_update()
        journal_path = root / ".deployments" / runtime.JOURNAL_NAME
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _, error = process.communicate()
                raise AssertionError(f"first update ended before interruption: {error.decode()}")
            try:
                active = json.loads(journal_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.002)
                continue
            if active.get("phase") == "state-validate-migrate":
                os.kill(process.pid, signal.SIGSTOP)
                _, stopped = os.waitpid(process.pid, os.WUNTRACED)
                assert os.WIFSTOPPED(stopped)
                assert not observe._inspect(old_id)["State"]["Running"]
                process.kill()
                process.communicate(timeout=30)
                assert process.returncode == -signal.SIGKILL
                break
            time.sleep(0.002)
        else:
            process.kill()
            process.communicate(timeout=30)
            raise AssertionError("first update stop boundary was not reached")
        extracted = tmp_path / "extracted-a"
        extracted.mkdir()
        extract = ("import component_recovery as r,json; root=pathlib.Path(sys.argv[2]); "
                   "s=json.loads(pathlib.Path(sys.argv[3]).read_text())['source']; "
                   "r._load_packager(root/'scripts'/'package_recovery_source.py'); "
                   "r._verify_capsule(root,s,pathlib.Path(sys.argv[4])); print('ok')")
        assert _locked(root, root, extract, str(root), str(input_path), str(extracted)) == "ok"
        source_file = root / "scripts/component_transaction.py"
        content = source_file.read_text()
        needle = "from __future__ import annotations"
        assert content.count(needle) == 1
        source_file.write_text(content.replace(needle, needle + "\nimport os as _fixture_os\n"
                                               "if _fixture_os.getenv('FIXTURE_B_IMPORT_TRAP') == '1': "
                                               "raise RuntimeError('source B loaded during A recovery')", 1))
        _git(root, "add", "scripts/component_transaction.py")
        _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "commit", "-q", "-m", "unadopted retry source B")
        commit_b, tree_b = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
        recover_code = ("import component_transaction as t,json; "
                        "print(json.dumps(t.recover(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4],"
                        "compose_path=pathlib.Path(sys.argv[5]))))")
        recovered = json.loads(_locked(root, extracted, recover_code, str(root), first["transaction_id"],
                                        first["source"]["source_tool_sha256"], COMPOSE,
                                        extra_env={"FIXTURE_B_IMPORT_TRAP": "1"}))
        assert recovered["status"] == "rolled_back" and not recovered["already_restored"]
        predecessor_raw = journal_path.read_bytes()
        predecessor = runtime.load_journal(journal_path)
        assert predecessor["status"] == "rolled_back"
        restored = observe.observe_container(old_id, expected_image_id=old_image, project=owner, service="wuwaterm",
                                             expected_command=["bot"], expected_entrypoint=["/entrypoint.sh"],
                                             env_fingerprint_key=b"x" * 32)
        assert restored["container"] == old["container"]
        wheel_b = wheel_for(commit_b, tree_b)
        second = successor(wheel_b["receipt"]["recovery_source"], wheel_b, restored["container"])
        runtime.validate_journal(second)
        input_path.write_text(json.dumps(second), encoding="ascii")
        if interrupt_b_probe:
            process = start_update()
            registry = root / ".deployments/component-adopt-probes"
            deadline = time.monotonic() + 150
            captured = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    raise AssertionError(f"B update ended before probe interruption: {error.decode()}")
                for path in registry.glob("*.json"):
                    try:
                        candidate = json.loads(path.read_text())
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    if (candidate.get("kind") == "legacy-core-version" and candidate.get("status") == "active"
                            and candidate.get("phase") == "start" and candidate.get("source", {}).get("commit") == commit_b):
                        os.kill(process.pid, signal.SIGSTOP)
                        _, stopped = os.waitpid(process.pid, os.WUNTRACED)
                        assert os.WIFSTOPPED(stopped)
                        assert journal_path.read_bytes() == predecessor_raw
                        assert candidate["predecessor_sha256"] == hashlib.sha256(predecessor_raw).hexdigest()
                        captured = candidate
                        process.kill()
                        process.communicate(timeout=30)
                        break
                if captured is not None:
                    break
                time.sleep(0.002)
            else:
                process.kill()
                process.communicate(timeout=30)
                raise AssertionError("fresh B legacy probe was not observed")
            refusal = ("import deployment_admission as a,json; "
                       "\ntry: a.admit_component(pathlib.Path(sys.argv[2]),operation='update',code_root=pathlib.Path(sys.argv[2]))"
                       "\nexcept a.AdmissionError as e: print(json.dumps({'refused':str(e)}))"
                       "\nelse: raise AssertionError('active probe was admitted')")
            assert "probe" in json.loads(_locked(root, root, refusal, str(root)))["refused"]
            probe_recover = ("import component_runtime as r,json; "
                             "r.recover_adopt_probe(pathlib.Path(sys.argv[2]),sys.argv[3],compose_path=pathlib.Path(sys.argv[4])); "
                             "print(json.dumps({'recovered':True}))")
            assert json.loads(_locked(root, root, probe_recover, str(root), captured["probe_id"], COMPOSE))["recovered"]
            assert journal_path.read_bytes() == predecessor_raw
        updated = json.loads(_locked(root, root, update_code, str(root), str(input_path), COMPOSE))
        assert updated["status"] == "committed" and updated["retirement"] == "complete"
        history = root / ".deployments/component-runtime-history" / (first["transaction_id"] + ".json")
        assert history.read_bytes() == predecessor_raw and not history.stat().st_mode & 0o222
        records = [json.loads(path.read_text()) for path in (root / ".deployments/component-adopt-probes").glob("*.json")]
        b_probes = [item for item in records if item.get("kind") == "legacy-core-version"
                    and item.get("source", {}).get("commit") == commit_b and item.get("status") == "completed"]
        assert len(b_probes) == 1 and b_probes[0]["image_id"] == old_image
        assert b_probes[0]["predecessor_sha256"] == hashlib.sha256(predecessor_raw).hexdigest()
        assert (root / ".deploy_commit").read_text() == commit_b + "\n"
        assert (root / ".deploy_current").read_text() == second["transaction_id"] + "\n"
        assert manifest.verify_database_binding(database) == first["database"]["stat"]
        if EVIDENCE:
            evidence = {"fixture": "unadopted-a-b-retry", "interrupted_b_probe": interrupt_b_probe,
                        "source_a": commit_a, "source_b": commit_b, "old_registered_build": api_build,
                        "first_journal_sha256": hashlib.sha256(predecessor_raw).hexdigest(),
                        "first_recovery": recovered, "second_update": updated, "b_probe": b_probes[0]}
            p = Path(EVIDENCE) / ("unadopted-retry-" + second["transaction_id"] + ".json")
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(evidence, output, sort_keys=True, indent=2)
                output.write("\n")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=30)
        matches = _docker("ps", "-aq", "--no-trunc", "--filter", "label=org.wuwaterm.test-owner=" + owner)
        owned.extend(item for item in matches.splitlines() if item not in owned)
        _owned_cleanup(owned, owner)


def test_full4_leaf_entry_mapper_preserves_symlinks_and_runtime_mapper_stays_strict(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "refs").mkdir()
    (data / "refs/settings.json").write_bytes(b"settings")
    (data / "refs/replies.json").write_bytes(b"replies")
    settings = data / "chat_settings.json"
    replies = data / "channel_replies.json"
    settings.symlink_to("refs/settings.json")
    replies.symlink_to("refs/replies.json")
    recipe = {"mounts": [{"host": str(data), "container": "/app/data", "read_only": False}]}
    actual = {"recipe": recipe, "path_settings": {"WUWATERM_DB_PATH": "/app/data/terms.db",
              "WUWATERM_STATE_DIR": None, "WUWATERM_SETTINGS_PATH": None,
              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}
    snapshot = {"command": ["--db", "/app/data/terms.db"]}
    before = [(p.lstat().st_ino, os.readlink(p)) for p in (settings, replies)]
    with pytest.raises(transaction.observe.ObservationError):
        transaction._telegram_effective_paths(actual, snapshot)
    paths = transaction._telegram_full4_entry_paths(actual, snapshot)
    assert paths == {"chat-settings": str(settings), "channel-replies": str(replies)}
    transaction._verify_full4_telegram_paths({"migrations": []}, actual, snapshot)
    assert [(p.lstat().st_ino, os.readlink(p)) for p in (settings, replies)] == before
    assert (data / "refs/settings.json").read_bytes() == b"settings"
    assert (data / "refs/replies.json").read_bytes() == b"replies"


def test_full4_entry_mapper_requires_original_writable_physical_parent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "refs").mkdir()
    (data / "refs/settings.json").write_bytes(b"settings")
    (data / "chat_settings.json").symlink_to("refs/settings.json")
    recipe = {"mounts": [{"host": str(data), "container": "/app/data", "read_only": False}]}
    actual = {"recipe": recipe, "path_settings": {"WUWATERM_DB_PATH": "/app/data/terms.db",
              "WUWATERM_STATE_DIR": None, "WUWATERM_SETTINGS_PATH": None,
              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}
    snapshot = {"command": ["--db", "/app/data/terms.db"]}
    recipe["mounts"][0]["read_only"] = True
    with pytest.raises(transaction.TransactionError, match="cannot be mapped"):
        transaction._telegram_full4_entry_paths(actual, snapshot)
    recipe["mounts"][0]["read_only"] = False
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    recipe["mounts"][0]["host"] = str(alias)
    with pytest.raises(transaction.TransactionError, match="cannot be mapped"):
        transaction._telegram_full4_entry_paths(actual, snapshot)


@pytest.mark.parametrize("kind,version", [("component-runtime", 4), ("component-full", 3), ("other", 4)])
def test_full4_live_topology_opt_in_rejects_other_contexts_before_observation(kind: str, version: int) -> None:
    with pytest.raises(transaction.TransactionError, match="requires an admitted FULL4 context"):
        transaction._verify_live_topology(Path("/unused"), {"kind": kind, "schema_version": version},
                                          which="before", compose_path=Path("/unused"), full4_endpoints=True)


@pytest.fixture
def adoption_leaf_fixture(tmp_path: Path, sample_journal: dict, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:  # noqa: F811
    journal = copy.deepcopy(sample_journal)
    state = tmp_path / "adoption-state"
    state.mkdir()
    (state / "refs").mkdir()
    for name in ("settings", "replies"):
        (state / "refs" / name).write_bytes((name + "\n").encode())
    (state / "chat_settings.json").symlink_to("refs/settings")
    (state / "channel_replies.json").symlink_to("refs/replies")
    component = journal["before"]["topology"]["telegram"]
    snapshot = manifest.read_recipe_snapshot(tmp_path, component["recipe"]["sha256"])
    snapshot["mounts"] = [
        {"host": str(tmp_path / "terms.db"), "container": "/app/data/terms.db", "read_only": True},
        {"host": str(state), "container": "/app/state", "read_only": False},
    ]
    path, digest = manifest.write_recipe_snapshot(tmp_path, snapshot)
    component["recipe"] = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
    component["recipe"].update(snapshot_path=path, sha256=digest)
    journal["target"]["manifest"]["components"]["telegram"] = copy.deepcopy(component)
    for where in (journal["before"]["topology"], journal["before"]["containers"],
                  journal["target"]["manifest"]["components"], journal["target"]["containers"]):
        where["api"] = None
    journal["api_identity"] = None
    journal["target"]["manifest"]["api_identity"] = None
    journal["target"]["containers"]["telegram"] = copy.deepcopy(journal["before"]["containers"]["telegram"])
    journal.update(schema_version=4, action="adopt", selection=[], predecessor=None)
    digest = "a" * 64
    journal["source"]["compose"] = {"path": f".deployments/component-compose-{digest}", "sha256": digest}
    journal["manifest"]["sha256"] = hashlib.sha256(manifest._encode(journal["target"]["manifest"])).hexdigest()
    runtime.validate_journal(journal)
    actual = {"recipe": copy.deepcopy(snapshot), "container": journal["before"]["containers"]["telegram"],
              "path_settings": {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_STATE_DIR": "/app/state",
                                "WUWATERM_SETTINGS_PATH": None, "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}
    monkeypatch.setattr(transaction, "_observed", lambda *args: actual)
    monkeypatch.setattr(transaction, "_image", lambda *args: {"Config": {"Labels": {
        "org.opencontainers.image.revision": component["image"]["revision"]}}})
    monkeypatch.setattr(transaction, "_effective_environment", lambda *args: {})
    monkeypatch.setattr(transaction.observe, "_inspect", lambda *args: {"Config": {"Env": []}})
    monkeypatch.setattr(transaction, "_inventory", lambda *args: [{"Id": actual["container"]["id"]}])
    return journal, state


def _leaf_snapshot(state: Path) -> list[tuple]:
    result = []
    for name in ("chat_settings.json", "channel_replies.json"):
        path = state / name
        info = path.lstat()
        referent = path.resolve(strict=True)
        target = referent.stat()
        result.append((info.st_dev, info.st_ino, info.st_mode, info.st_nlink, os.readlink(path),
                       referent.read_bytes(), target.st_dev, target.st_ino, target.st_mode, target.st_uid, target.st_gid))
    return result


@pytest.mark.parametrize("which,terminal", [("before", False), ("before", True), ("target", True)])
def test_schema4_adoption_topology_preserves_leaf_links(adoption_leaf_fixture: tuple[dict, Path],
                                                        which: str, terminal: bool) -> None:
    journal, state = adoption_leaf_fixture
    before = _leaf_snapshot(state)
    transaction._verify_live_topology(state.parent, journal, which=which, compose_path=Path("/unused"),
                                      allow_runtime_drift=terminal)
    assert _leaf_snapshot(state) == before


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("terminal", [False, True])
def test_old_adoption_topology_keeps_strict_leaf_mapping(adoption_leaf_fixture: tuple[dict, Path],
                                                        version: int, terminal: bool) -> None:
    journal, state = adoption_leaf_fixture
    journal["schema_version"] = version
    journal.pop("predecessor")
    if version == 2:
        journal["source"].pop("compose")
    runtime.validate_journal(journal)
    with pytest.raises(transaction.observe.ObservationError, match="symlink"):
        transaction._verify_live_topology(state.parent, journal, which="before", compose_path=Path("/unused"),
                                          allow_runtime_drift=terminal)


@pytest.mark.parametrize("field,value", [("selection", ["telegram"]), ("migrations", [{}]), ("status", "invalid")])
def test_invalid_adoption_journal_is_refused_before_leaf_exception(adoption_leaf_fixture: tuple[dict, Path],
                                                                   field: str, value: object) -> None:
    journal, state = adoption_leaf_fixture
    journal[field] = value
    with pytest.raises(transaction.runtime.JournalError):
        transaction._verify_live_topology(state.parent, journal, which="before", compose_path=Path("/unused"))


def test_schema4_runtime_update_keeps_strict_leaf_mapping(adoption_leaf_fixture: tuple[dict, Path],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    journal, state = adoption_leaf_fixture
    previous = copy.deepcopy(journal["target"]["manifest"])
    previous["deployment_id"] = "20261106T100000Z-" + "1" * 32
    journal["before"]["manifest"] = previous
    journal["pointers"]["old"][".deploy_current"] = {"present": True, "bytes": previous["deployment_id"] + "\n"}
    journal["predecessor"] = runtime.predecessor_reference("runtime", previous["deployment_id"], b"previous")
    journal["action"] = "update"
    journal["selection"] = ["telegram"]
    journal["target"]["containers"]["telegram"].update(
        id=None, name=None, started_at=None, restart_count=None, transaction_label=journal["transaction_id"])
    runtime.validate_journal(journal)
    root = state.parent
    manifest.write_manifest(root, previous)
    (root / ".deploy_commit").write_text(journal["source"]["commit"] + "\n")
    (root / ".deploy_current").write_text(previous["deployment_id"] + "\n")
    prior_raw = b"previous"
    prior = {"kind": "component-runtime", "status": "committed", "transaction_id": previous["deployment_id"],
             "target": {"manifest": previous, "containers": journal["before"]["containers"]},
             "pointers": {"target": journal["pointers"]["old"]}}
    monkeypatch.setattr(transaction, "_verify_saved_source", lambda *args: None)
    monkeypatch.setattr(transaction, "_verify_old_topology", lambda *args, **kwargs: None)
    monkeypatch.setattr(transaction, "_telegram_receipt", lambda *args: None)
    monkeypatch.setattr(transaction.recovery, "_load_packager", lambda *args: None)
    monkeypatch.setattr(transaction.recovery, "_verify_clean_source", lambda *args: None)
    monkeypatch.setattr(transaction.runtime, "tool_bytes_identity", lambda *args: journal["source"]["tool_bytes_sha256"])
    before = _leaf_snapshot(state)
    with pytest.raises(transaction.observe.ObservationError, match="symlink"):
        transaction._verify_initial_preimage(root, journal, Path("/unused"), prior, prior_raw)
    assert _leaf_snapshot(state) == before
    assert (root / ".deploy_commit").read_text() == journal["source"]["commit"] + "\n"
    assert (root / ".deploy_current").read_text() == previous["deployment_id"] + "\n"
    assert not (root / ".deployments" / runtime.JOURNAL_NAME).exists()


@pytest.mark.parametrize("case", ["readonly", "bind-link", "parent-link", "missing-bind", "fifo", "hardlinked-link"])
@pytest.mark.parametrize("terminal", [False, True])
def test_adoption_leaf_refuses_unsafe_entries(tmp_path: Path, case: str, terminal: bool) -> None:
    state = tmp_path / "state"
    state.mkdir()
    leaf = state / "chat_settings.json"
    leaf.symlink_to("target")
    recipe = {"mounts": [{"host": str(state), "container": "/app/state", "read_only": False}]}
    actual = {"recipe": recipe, "path_settings": {"WUWATERM_DB_PATH": "/app/state/terms.db",
              "WUWATERM_STATE_DIR": "/app/state", "WUWATERM_SETTINGS_PATH": None,
              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}
    snapshot = {"command": ["--db", "/app/state/terms.db"]}
    if case == "readonly":
        recipe["mounts"][0]["read_only"] = True
    elif case == "bind-link":
        alias = tmp_path / "bind-alias"
        alias.symlink_to(state, target_is_directory=True)
        recipe["mounts"][0]["host"] = str(alias)
    elif case == "parent-link":
        (state / "nested").symlink_to(state, target_is_directory=True)
        actual["path_settings"]["WUWATERM_SETTINGS_PATH"] = "/app/state/nested/chat_settings.json"
    elif case == "missing-bind":
        recipe["mounts"][0]["host"] = str(tmp_path / "missing")
    elif case == "fifo":
        leaf.unlink()
        os.mkfifo(leaf)
    else:
        os.link(leaf, state / "another-link", follow_symlinks=False)
    with pytest.raises(transaction.TransactionError):
        transaction._verify_adoption_telegram_paths(actual, snapshot, terminal=terminal)


def test_adoption_regular_and_absent_paths_keep_existing_alias_behavior(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "chat_settings.json").write_bytes(b"settings")
    mounts = [{"host": str(state), "container": target, "read_only": False} for target in ("/app/state", "/alias")]
    actual = {"recipe": {"mounts": mounts}, "path_settings": {"WUWATERM_DB_PATH": "/app/state/terms.db",
              "WUWATERM_STATE_DIR": "/app/state", "WUWATERM_SETTINGS_PATH": None,
              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}
    snapshot = {"command": ["--db", "/app/state/terms.db"]}
    transaction._verify_adoption_telegram_paths(actual, snapshot, terminal=False)
    transaction._verify_adoption_telegram_paths(actual, snapshot, terminal=True)


@pytest.mark.parametrize("committed", [False, True])
def test_adoption_leaf_initial_terminal_and_recovery_routing(adoption_leaf_fixture: tuple[dict, Path],
                                                             monkeypatch: pytest.MonkeyPatch,
                                                             committed: bool) -> None:
    journal, state = adoption_leaf_fixture
    root = state.parent
    journal["pointers"]["old"][".deploy_commit"] = {"present": False, "bytes": None}
    monkeypatch.setattr(transaction, "_verify_saved_source", lambda *args: None)
    monkeypatch.setattr(transaction, "_verify_old_topology", lambda *args, **kwargs: None)
    monkeypatch.setattr(transaction.recovery, "_load_packager", lambda *args: None)
    monkeypatch.setattr(transaction.recovery, "_verify_clean_source", lambda *args: None)
    monkeypatch.setattr(transaction.runtime, "tool_bytes_identity", lambda *args: journal["source"]["tool_bytes_sha256"])
    monkeypatch.setattr(transaction.admission, "require_lock", lambda *args: None)
    before = _leaf_snapshot(state)
    transaction._verify_initial_preimage(root, journal, Path("/unused"), None, None)
    manifest.write_manifest(root, journal["target"]["manifest"])
    (root / ".deploy_commit").write_text(journal["source"]["commit"] + "\n")
    journal_path = root / ".deployments" / runtime.JOURNAL_NAME
    if committed:
        (root / ".deploy_current").write_text(journal["transaction_id"] + "\n")
        journal.update(status="committed", phase="committed")
        raw = manifest._encode(journal)
        journal_path.write_bytes(raw)
        transaction._verify_terminal_record(root, journal, raw, Path("/unused"))
    else:
        journal["phase"] = "deploy-current"
        journal_path.write_bytes(manifest._encode(journal))
        first = transaction._recover_adoption(root, journal, Path("/unused"))
        assert first["status"] == "rolled_back" and first["already_restored"] is False
        assert not (root / ".deploy_commit").exists() and not (root / ".deploy_current").exists()
        second = transaction._recover_adoption(root, journal, Path("/unused"))
        assert second["status"] == "rolled_back" and second["already_restored"] is True
        transaction._verify_terminal_record(root, journal, journal_path.read_bytes(), Path("/unused"))
    assert _leaf_snapshot(state) == before


@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("changed_preimage", [False, True])
def test_legacy_null_manifest_retry_preserves_preimage_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_journal: dict,  # noqa: F811
    schema: int, changed_preimage: bool,
) -> None:
    successor = copy.deepcopy(sample_journal)
    successor["schema_version"] = schema
    if schema == 3:
        digest = "a" * 64
        successor["source"]["compose"] = {
            "path": f".deployments/component-compose-{digest}", "sha256": digest,
        }
    previous = copy.deepcopy(successor)
    previous.update(status="rolled_back", phase="rolled-back")
    if changed_preimage:
        successor["pointers"]["old"][".deploy_commit"]["bytes"] = "c" * 40 + "\n"
    runtime.validate_journal(previous)
    runtime.validate_journal(successor)

    class SourceVerificationReached(Exception):
        pass

    def source_reached(*args: object) -> None:
        raise SourceVerificationReached

    monkeypatch.setattr(transaction.recovery, "_load_packager", source_reached)
    expected = transaction.TransactionError if changed_preimage else SourceVerificationReached
    with pytest.raises(expected):
        transaction._verify_initial_preimage(
            tmp_path, successor, Path("/unused"), previous, manifest._encode(previous),
        )


@pytest.mark.parametrize("change", [None, "database", "database-stat", "api-identity", "recipe-reference",
                                    "container-name", "container-label", "old-pointer", "schema", "status"])
def test_null_adoption_initial_preimage_uses_handoff_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str | None,
) -> None:
    from test_component_full import _null_adoption_pair, _retarget_adoption

    previous, successor = _null_adoption_pair(tmp_path)
    if change == "schema":
        previous["schema_version"] = 3
        previous.pop("predecessor")
    elif change == "status":
        previous.update(status="active", phase="prepared")
    elif change == "database":
        successor["database"]["binding"]["sha256"] = "f" * 64
        successor["target"]["manifest"]["database"] = copy.deepcopy(successor["database"]["binding"])
    elif change == "database-stat":
        successor["database"]["stat"]["inode"] += 1
    elif change == "api-identity":
        successor["api_identity"]["inode"] += 1
        successor["target"]["manifest"]["api_identity"] = copy.deepcopy(successor["api_identity"])
    elif change == "recipe-reference":
        recipe = successor["before"]["topology"]["api"]["recipe"]
        recipe.update(sha256="f" * 64, snapshot_path=".deployments/product-recipes/" + "f" * 64 + ".json")
        successor["target"]["manifest"]["components"] = copy.deepcopy(successor["before"]["topology"])
    elif change in ("container-name", "container-label"):
        key, value = (("name", "other-api") if change == "container-name" else
                      ("transaction_label", "20261106T090000Z-" + "3" * 32))
        for location in (successor["before"]["containers"], successor["target"]["containers"]):
            location["api"][key] = value
    elif change == "old-pointer":
        successor["pointers"]["old"][".deploy_commit"] = {"present": False, "bytes": None}
    _retarget_adoption(successor, successor["transaction_id"])
    raw = manifest._encode(previous)
    successor["predecessor"] = runtime.predecessor_reference("runtime", previous["transaction_id"], raw)
    runtime.validate_journal(previous)
    runtime.validate_journal(successor)

    class SourceVerificationReached(Exception):
        pass

    def source_reached(*args: object) -> None:
        raise SourceVerificationReached

    monkeypatch.setattr(transaction.recovery, "_load_packager", source_reached)
    if change is None:
        with pytest.raises(SourceVerificationReached):
            transaction._verify_initial_preimage(tmp_path, successor, Path("/unused"), previous, raw)
    else:
        with pytest.raises(transaction.TransactionError, match="successor preimage differs"):
            transaction._verify_initial_preimage(tmp_path, successor, Path("/unused"), previous, raw)


@pytest.mark.parametrize("environment_db,cli_db,accepted", [
    (None, "/app/data/terms.db", True),
    ("/app/data/default.db", "/app/data/terms.db", True),
    ("/app/data/terms.db", "/app/data/other.db", False),
])
def test_unselected_telegram_preserves_cli_database_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment_db: str | None,
    cli_db: str, accepted: bool,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "terms.db"
    database.write_bytes(b"fixture")
    from test_product_manifest import component, snapshot
    from test_component_runtime import container

    record = component(tmp_path, "telegram")
    recipe = snapshot("wuwaterm-telegram", record["image"]["id"])
    recipe["command"] = ["--db", cli_db]
    recipe["mounts"][0]["host"] = str(data)
    path, digest = manifest.write_recipe_snapshot(tmp_path, recipe)
    record["recipe"] = {key: recipe[key] for key in manifest._RECIPE_FIELDS}
    record["recipe"].update(snapshot_path=path, sha256=digest)
    expected = container(record, name="fixture-telegram", image_digit="b")
    actual = {"container": expected, "recipe": recipe,
              "path_settings": {"WUWATERM_DB_PATH": environment_db}}
    journal = {"before": {"topology": {"telegram": record}, "containers": {"telegram": expected}},
               "database": {"binding": {"path": str(database)}}}
    # Inject only the Docker observation boundary; recipe comparison and
    # database resolution/binding validation execute unchanged.
    monkeypatch.setattr(transaction, "_inventory", lambda *args: [{"Id": expected["id"]}])
    monkeypatch.setattr(transaction, "_observed", lambda *args: actual)
    if accepted:
        transaction._other_component(tmp_path, journal, b"x" * 32)
    else:
        with pytest.raises(transaction.TransactionError, match="database mount changed"):
            transaction._other_component(tmp_path, journal, b"x" * 32)


@pytest.mark.parametrize("environment,command,expected", [
    ("/app/data/env.db", [], "/app/data/env.db"),
    (None, ["--db", "/app/data/cli.db"], "/app/data/cli.db"),
    ("/app/data/env.db", ["--db", "/app/data/cli.db"], "/app/data/cli.db"),
])
def test_telegram_database_resolution_uses_command_precedence(
    environment: str | None, command: list[str], expected: str,
) -> None:
    actual = {"path_settings": {"WUWATERM_DB_PATH": environment}}
    assert transaction._telegram_database_path(actual, {"command": command}) == expected


def test_telegram_database_resolution_requires_explicit_path() -> None:
    with pytest.raises(transaction.TransactionError, match="not explicit"):
        transaction._telegram_database_path({"path_settings": {"WUWATERM_DB_PATH": None}}, {"command": []})


@pytest.mark.parametrize("operation,expected_error", [
    ("prepare_pending", "pending preflight database mount changed"),
    ("retire_committed", "committed Telegram database mount changed"),
])
def test_telegram_lifecycle_checks_honor_command_db_precedence_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, expected_error: str,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    correct_db = data / "correct.db"
    correct_db.write_bytes(b"correct")
    wrong_db = data / "wrong.db"
    wrong_db.write_bytes(b"wrong")

    from test_product_manifest import component, snapshot
    from test_component_runtime import container

    record = component(tmp_path, "telegram")
    recipe = snapshot("wuwaterm-telegram", record["image"]["id"])
    # command specifies the correct db mount path
    recipe["command"] = ["--db", "/app/data/correct.db"]
    recipe["mounts"][0]["host"] = str(data)
    path, digest = manifest.write_recipe_snapshot(tmp_path, recipe)
    record["recipe"] = {key: recipe[key] for key in manifest._RECIPE_FIELDS}
    record["recipe"].update(snapshot_path=path, sha256=digest)
    expected = container(record, name="fixture-telegram", image_digit="b")

    # In path_settings (environment), WUWATERM_DB_PATH points to a different/wrong db path
    actual = {
        "container": expected,
        "recipe": recipe,
        "path_settings": {"WUWATERM_DB_PATH": "/app/data/wrong.db", "WUWATERM_STATE_DIR": None,
                          "WUWATERM_SETTINGS_PATH": None, "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None},
    }

    stat = {"device": 1, "inode": 2}
    journal = {
        "schema_version": 5,
        "kind": "component-runtime",
        "status": "active",
        "phase": "prepared",
        "transaction_id": "20261107T120000Z-" + "3" * 32,
        "selection": ["telegram"],
        "migrations": [],
        "before": {"topology": {"api": None, "telegram": record}, "containers": {"api": None, "telegram": expected}},
        "target": {
            "manifest": {"components": {"telegram": record}},
            "containers": {"telegram": None if operation == "prepare_pending" else {**expected, "running": False}},
        },
        "database": {
            "binding": {"path": str(correct_db), "sha256": "4" * 64, "schema_version": 2, "provenance": "fixture"},
            "stat": stat,
        },
        "pointers": {
            "old": {
                ".deploy_commit": {"present": False, "bytes": None},
                ".deploy_current": {"present": False, "bytes": None},
            },
            "target": {
                ".deploy_commit": {"present": False, "bytes": None},
                ".deploy_current": {"present": False, "bytes": None},
            },
        },
    }

    target_container = container(record, name="fixture-telegram-target", image_digit="c")
    retained_name = transaction._retained_name(journal, "telegram")
    old_actual = {
        "container": {**expected, "name": retained_name, "running": False},
        "recipe": recipe,
        "path_settings": {"WUWATERM_DB_PATH": "/app/data/wrong.db", "WUWATERM_STATE_DIR": None,
                          "WUWATERM_SETTINGS_PATH": None, "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None},
    }
    target_actual = {
        "container": target_container,
        "recipe": recipe,
        "path_settings": {"WUWATERM_DB_PATH": "/app/data/wrong.db", "WUWATERM_STATE_DIR": None,
                          "WUWATERM_SETTINGS_PATH": None, "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None},
    }

    if operation == "retire_committed":
        journal["target"]["containers"]["telegram"] = target_container

    def mock_observed(cid: str, snapshot: dict, key: bytes) -> dict:
        if cid == expected["id"]:
            return old_actual
        return target_actual

    monkeypatch.setattr(transaction, "_inventory", lambda *args: [{"Id": expected["id"]}] if operation == "prepare_pending" else [{"Id": expected["id"]}, {"Id": target_container["id"]}])
    monkeypatch.setattr(transaction, "_observed", lambda cid, snap, key: actual if operation == "prepare_pending" else mock_observed(cid, snap, key))
    monkeypatch.setattr(transaction, "_image", lambda *args: {"Config": {"Labels": {"org.opencontainers.image.revision": record["image"]["revision"]}}})
    monkeypatch.setattr(transaction, "_effective_environment", lambda *args: {})
    monkeypatch.setattr(transaction, "_env_map", lambda *args: {})
    monkeypatch.setattr(transaction.observe, "_inspect", lambda *args: {"Config": {"Env": []}})
    monkeypatch.setattr(transaction, "_pointer", lambda root, name: {"present": False, "bytes": None})
    monkeypatch.setattr(transaction.manifest, "verify_database_binding", lambda *args: stat)
    monkeypatch.setattr(transaction.manifest, "read_manifest", lambda *args: journal["target"]["manifest"])
    monkeypatch.setattr(transaction, "_same_container_binding", lambda c1, c2: True)
    monkeypatch.setattr(transaction, "_telegram_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(transaction, "_verify_current_telegram_paths", lambda *args: None)
    monkeypatch.setattr(transaction, "_other_api", lambda *args, **kwargs: None)
    monkeypatch.setattr(transaction, "_docker", lambda *args, **kwargs: None)

    # With command precedence, binding path resolves to /app/data/correct.db -> str(correct_db), matching journal
    if operation == "prepare_pending":
        res = transaction._recover_pending_telegram(tmp_path, journal, Path("/unused"), finish=False)
        assert res["status"] == "pending"
    else:
        res = transaction._retire_telegram(tmp_path, journal, remove=False)
        assert res is True

    # If journal expects wrong_db, but command gave correct_db, it must raise expected_error because command takes precedence
    journal["database"]["binding"]["path"] = str(wrong_db)
    with pytest.raises(transaction.TransactionError, match=expected_error):
        if operation == "prepare_pending":
            transaction._recover_pending_telegram(tmp_path, journal, Path("/unused"), finish=False)
        else:
            transaction._retire_telegram(tmp_path, journal, remove=False)


def _rollback_fixture(tmp_path: Path) -> tuple[Path, dict, dict]:
    """A schema-5 cutover journal with one recorded state kind per kind pair."""
    root = tmp_path / "root"
    (root / ".deployments").mkdir(parents=True)
    data = root / "data"
    state_dir = root / "state"
    data.mkdir()
    state_dir.mkdir()
    snapshot = {
        "schema_version": 1, "image_id": "sha256:" + "1" * 64, "project": "wuwaterm",
        "service": "wuwaterm-telegram", "container_name": "wuwaterm-telegram",
        "command": ["--db", "/app/data/terms.db"], "entrypoint": ["wuwaterm-telegram"],
        "mounts": [{"host": str(data), "container": "/app/data", "read_only": False},
                   {"host": str(state_dir), "container": "/app/state", "read_only": False}],
        "env_files": [], "network_mode": "host", "ports": [],
        "restart": {"name": "unless-stopped", "maximum_retry_count": 0},
        "user": "0:0", "working_dir": "/app",
        "environment_overrides": [{"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/app/data/terms.db"}],
        "runtime_options": {"read_only_root": False, "privileged": False, "init": None, "pid_mode": "",
                            "ipc_mode": "private", "cap_add": [], "cap_drop": [], "security_opt": [],
                            "dns": [], "extra_hosts": []},
    }
    path, digest = manifest.write_recipe_snapshot(root, snapshot)
    recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
    recipe.update(snapshot_path=path, sha256=digest)
    component = {"recipe": recipe, "image": {"id": snapshot["image_id"], "revision": "2" * 40},
                 "provenance": {"kind": "legacy-image"}}
    journal = {"schema_version": 5, "kind": "component-runtime", "transaction_id": "20261107T120000Z-" + "3" * 32,
               "before": {"topology": {"api": None, "telegram": component}}, "migrations": []}
    return root, journal, {"data": data, "state": state_dir}


def test_schema5_rollback_keeps_progressed_state_and_proves_old_readers(tmp_path: Path) -> None:
    root, journal, paths = _rollback_fixture(tmp_path)
    legacy = paths["data"] / "chat_settings.json"
    target = paths["state"] / "chat_settings.json"
    legacy.write_bytes(b'{"allowed":[1]}')
    target.write_bytes(b'{"allowed":[1,2]}')
    journal["migrations"] = [{
        "kind": "chat-settings", "action": "copy-if-absent", "effective_path": "/app/state/chat_settings.json",
        "source_path": str(legacy), "target_path": str(target),
        "source_before": transaction._state_preimage(str(legacy)),
        "target_before": {"present": False, "sha256": None, "stat": None}, "created": None}]
    calls = []

    class Reader:
        StateError = type("StateError", (Exception,), {})

        @staticmethod
        def canonical(root_path: Path, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"raw_sha256": "a" * 64, "canonical_sha256": "a" * 64}

    original = transaction._state_module
    transaction._state_module = lambda: Reader  # type: ignore[assignment]
    try:
        before = (legacy.stat().st_ino, legacy.read_bytes(), target.read_bytes())
        transaction._preserved_state_rollback(root, journal)
        # The progressed bytes are kept: nothing restores or deletes state.
        assert (legacy.stat().st_ino, legacy.read_bytes(), target.read_bytes()) == before
        assert len(calls) == 1 and calls[0]["host_path"] == str(target)
        assert calls[0]["state_kind"] == "chat-settings" and calls[0]["snapshot"]["image_id"] == journal[
            "before"]["topology"]["telegram"]["image"]["id"]
        # A distinct legacy endpoint is not this transaction's to change.
        legacy.write_bytes(b'{"allowed":[]}')
        with pytest.raises(transaction.TransactionError, match="legacy Telegram state changed"):
            transaction._preserved_state_rollback(root, journal)
        legacy.write_bytes(b'{"allowed":[1]}')
        journal["migrations"][0]["source_before"] = transaction._state_preimage(str(legacy))
        # An old reader that rejects the progressed bytes stops the rollback.
        class Rejecting(Reader):
            @staticmethod
            def canonical(root_path: Path, **kwargs: object) -> dict:
                raise Reader.StateError("unreadable")

        transaction._state_module = lambda: Rejecting  # type: ignore[assignment]
        with pytest.raises(transaction.TransactionError, match="old Telegram reader cannot accept"):
            transaction._preserved_state_rollback(root, journal)
        # Historical schemas never read state during rollback.
        calls.clear()
        historical = dict(journal, schema_version=4)
        transaction._preserved_state_rollback(root, historical)
        assert calls == []
        # A recorded present target may not disappear.
        target.unlink()
        transaction._state_module = lambda: Reader  # type: ignore[assignment]
        journal["migrations"][0]["target_before"] = {"present": True, "sha256": "b" * 64, "stat": {
            "device": 1, "inode": 2, "size": 1, "mtime_ns": 1, "ctime_ns": 1}}
        with pytest.raises(transaction.TransactionError, match="Telegram state disappeared"):
            transaction._preserved_state_rollback(root, journal)
    finally:
        transaction._state_module = original


def test_schema5_rollback_skips_absent_state_and_reply_links_are_not_recreated(tmp_path: Path) -> None:
    root, journal, paths = _rollback_fixture(tmp_path)
    settings = paths["data"] / "chat_settings.json"
    legacy_replies = paths["data"] / "channel_replies.jsonl"
    entry = paths["state"] / "channel_replies.jsonl"
    referent = paths["state"] / "replies-2026.jsonl"
    settings.write_bytes(b'{"allowed":[7]}')
    legacy_replies.write_bytes(b'{"channel":"c"}\n')
    referent.write_bytes(b'{"channel":"c"}\n')
    entry.symlink_to(referent)
    journal["migrations"] = [
        {"kind": "chat-settings", "action": "validate-existing", "effective_path": "/app/state/chat_settings.json",
         "source_path": str(settings), "target_path": str(settings),
         "source_before": transaction._state_preimage(str(settings)),
         "target_before": transaction._state_preimage(str(settings)), "created": None},
        {"kind": "channel-replies", "action": "copy-if-absent", "effective_path": "/app/state/channel_replies.jsonl",
         "source_path": str(legacy_replies), "target_path": str(entry),
         "source_before": transaction._state_preimage(str(legacy_replies)),
         "target_before": {"present": False, "sha256": None, "stat": None}, "created": None}]
    calls = []

    class Reader:
        StateError = type("StateError", (Exception,), {})

        @staticmethod
        def canonical(root_path: Path, **kwargs: object) -> dict:
            calls.append(kwargs["state_kind"])
            assert kwargs["host_path"] != str(entry), "a reply link is not a state leaf"
            return {"raw_sha256": "a" * 64, "canonical_sha256": "a" * 64}

    original = transaction._state_module
    transaction._state_module = lambda: Reader  # type: ignore[assignment]
    try:
        link_before = (entry.lstat().st_ino, os.readlink(entry))
        transaction._preserved_state_rollback(root, journal)
        # The retained writer path is verified; the stale reply link stays as it
        # is because rollback never writes or restores state.
        assert calls == ["chat-settings"]
        assert (entry.lstat().st_ino, os.readlink(entry)) == link_before
        assert referent.read_bytes() == b'{"channel":"c"}\n'
    finally:
        transaction._state_module = original


def _probe_plans(migration: str = "copy-if-absent") -> list[dict]:
    def kind(name: str, action: str, legacy: str | None) -> dict:
        return {"effective_path": f"/app/state/{name}", "legacy_path": legacy,
                "migration": action, "mapped_target": f"/host/state/{name}",
                "mapped_legacy": None if legacy is None else f"/host/data/{name}"}

    old = {"database_path": "/custom/terms.db",
           "kinds": {"chat-settings": kind("chat_settings.json", migration, "chat_settings.json"),
                     "channel-replies": kind("channel_replies.jsonl", "none", None)}}
    return [old, copy.deepcopy(old)]


def _probe_journal(schema: int) -> dict:
    return {"schema_version": schema, "transaction_id": "20261107T120000Z-" + "9" * 32,
            "before": {"topology": {"api": None, "telegram": {
                "image": {"id": "sha256:" + "1" * 64, "revision": "2" * 40},
                "provenance": {"kind": "legacy-image"}}}},
            "target": {"manifest": {"components": {"telegram": {
                "image": {"id": "sha256:" + "1" * 64, "revision": "2" * 40},
                "provenance": {"kind": "completed-artifact"}}}}},
            "database": {"binding": {"path": "/custom/terms.db"}}}


def test_schema5_state_probe_accepts_native_pending_and_keeps_historical_refusals() -> None:
    """The product copies at start; the journal records it without copying."""
    context = {"old_snapshot": {"image_id": "sha256:" + "1" * 64}, "old_environment": {},
               "target_snapshot": {"image_id": "sha256:" + "1" * 64}, "target_environment": {}}
    plans = _probe_plans()

    class Planner:
        @staticmethod
        def plan(root: Path, *, snapshot: dict, **kwargs: object) -> dict:
            return plans[0] if snapshot is context["old_snapshot"] else plans[1]

        @staticmethod
        def preimage(root: Path, *, host_path: str, **kwargs: object) -> dict:
            assert kwargs["image_id"] == "sha256:" + "1" * 64
            return {"present": host_path.startswith("/host/data/"), "sha256": "a" * 64,
                    "stat": {"device": 1, "inode": 2, "size": 1, "mtime_ns": 1, "ctime_ns": 1}}

    original = transaction._state_module
    transaction._state_module = lambda: Planner  # type: ignore[assignment]
    try:
        journal = _probe_journal(5)
        migrations = transaction._probe_telegram_state(Path("/"), journal, context)
        assert [(item["kind"], item["action"]) for item in migrations] == [
            ("chat-settings", "copy-if-absent"), ("channel-replies", "none")]
        assert all(item["created"] is None for item in migrations)
        assert migrations[0]["source_path"] == "/host/data/chat_settings.json"
        assert migrations[0]["target_path"] == "/host/state/chat_settings.json"
        assert migrations[0]["target_before"]["present"] is False
        assert migrations[1]["source_before"] is None
        # Historical schemas keep refusing an unrecorded startup copy.
        with pytest.raises(transaction.TransactionError, match="full update"):
            transaction._probe_telegram_state(Path("/"), _probe_journal(4), context)
        # A relocated effective path is refused before anything is recorded.
        plans[1]["kinds"]["chat-settings"]["effective_path"] = "/app/elsewhere/chat_settings.json"
        with pytest.raises(transaction.TransactionError, match="state relocation"):
            transaction._probe_telegram_state(Path("/"), _probe_journal(5), context)
        # A copy needs both legacy endpoints to agree.
        plans[:] = _probe_plans()
        plans[0]["kinds"]["chat-settings"]["mapped_legacy"] = None
        with pytest.raises(transaction.TransactionError, match="legacy state is outside every bind"):
            transaction._probe_telegram_state(Path("/"), _probe_journal(5), context)
    finally:
        transaction._state_module = original


# ------------------------------------------------ schema-5 native state leaves

def _leaf_recipe(data: Path, state_dir: Path) -> dict:
    """An observed Telegram recipe whose state entry may be a retained link."""
    return {"recipe": {"mounts": [{"host": str(data), "container": "/app/data", "read_only": True},
                                  {"host": str(state_dir), "container": "/app/state", "read_only": False}],
                       "working_dir": "/app", "command": ["--db", "/app/data/terms.db"]},
            "path_settings": {"WUWATERM_DB_PATH": "/app/data/terms.db",
                              "WUWATERM_STATE_DIR": "/app/state",
                              "WUWATERM_SETTINGS_PATH": None,
                              "WUWATERM_CHANNEL_REPLY_INDEX_PATH": None}}


def _leaf_tree(tmp_path: Path, replies_link: str) -> tuple[Path, Path, dict]:
    data, state_dir = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    (state_dir / "refs").mkdir(parents=True)
    (state_dir / "refs" / "settings.json").write_bytes(b'{"allowed":[1]}')
    (state_dir / "refs" / "replies.json").write_bytes(b'{"channel":"c"}\n')
    (state_dir / "chat_settings.json").symlink_to("refs/settings.json")
    (state_dir / "channel_replies.json").symlink_to(replies_link)
    return data, state_dir, _leaf_recipe(data, state_dir)


@pytest.mark.parametrize("replies_link", ["refs/replies.json", "/app/state/refs/replies.json"])
def test_schema5_runtime_paths_record_the_planned_leaf(tmp_path: Path, replies_link: str) -> None:
    data, state_dir, actual = _leaf_tree(tmp_path, replies_link)
    paths = transaction._telegram_v5_paths(actual, actual["recipe"])
    # A settings entry resolves to the file the product rewrites; a reply entry
    # stays the entry the product replaces, for relative and absolute links.
    assert paths["chat-settings"] == {"entry": str(state_dir / "chat_settings.json"),
                                      "planned": str(state_dir / "refs" / "settings.json")}
    assert paths["channel-replies"] == {"entry": str(state_dir / "channel_replies.json"),
                                        "planned": str(state_dir / "channel_replies.json")}
    assert transaction._verify_current_telegram_paths(
        {"schema_version": 5, "kind": "component-runtime", "migrations": []}, actual, actual["recipe"]) is None
    # A settings link may not name bytes outside the state bind.
    (state_dir / "chat_settings.json").unlink()
    (state_dir / "chat_settings.json").symlink_to("/etc/passwd")
    with pytest.raises(transaction.TransactionError, match="leaves its binding"):
        transaction._telegram_v5_paths(actual, actual["recipe"])
    assert data.is_dir()


@pytest.mark.parametrize("replies_link", ["refs/replies.json", "/app/state/refs/replies.json"])
def test_schema5_preimages_and_bindings_follow_a_retained_reply_link(tmp_path: Path, replies_link: str) -> None:
    _, state_dir, actual = _leaf_tree(tmp_path, replies_link)
    entry = state_dir / "channel_replies.json"
    settings = state_dir / "refs" / "settings.json"
    referent = (state_dir / "refs" / "replies.json").resolve()
    journal = {"schema_version": 5, "kind": "component-runtime",
               "transaction_id": "20261106T110000Z-" + "6" * 32, "selection": ["telegram"],
               "migrations": [
                   {"kind": "chat-settings", "action": "validate-existing",
                    "effective_path": "/app/state/chat_settings.json",
                    "source_path": None, "target_path": str(settings),
                    "source_before": None, "target_before": transaction._state_preimage(str(settings)),
                    "created": None},
                   {"kind": "channel-replies", "action": "validate-existing",
                    "effective_path": "/app/state/channel_replies.json",
                    "source_path": None, "target_path": str(entry),
                    "source_before": None,
                    "target_before": transaction._state_preimage(str(entry), leaf=True,
                                                                 recipe=actual["recipe"]),
                    "created": None}]}
    assert journal["migrations"][1]["target_before"]["present"] is True
    transaction._verify_probed_preimages(journal, actual["recipe"])
    transaction._telegram_state_bindings(journal, [actual])
    transaction._verify_current_telegram_paths(journal, actual, actual["recipe"])
    # Progressed bytes are still the file the reply link names.
    referent.write_bytes(b'{"channel":"d"}\n')
    with pytest.raises(transaction.TransactionError, match="state changed"):
        transaction._verify_probed_preimages(journal, actual["recipe"])
    # Replacing the entry, as the product does, keeps the planned path valid.
    entry.unlink()
    entry.write_bytes(b'{"channel":"d"}\n')
    transaction._telegram_state_bindings(journal, [actual])
    (state_dir / "chat_settings.json").unlink()
    (state_dir / "chat_settings.json").symlink_to("refs/settings.json")
    # A state entry that is not absent, a regular file or a safe single link is
    # refused before any service mutation.
    entry.unlink()
    entry.symlink_to("/app/state/refs/replies.json")
    entry_link = state_dir / "channel_replies.json"
    entry_link.unlink()
    os.link(state_dir / "refs" / "replies.json", entry_link)
    with pytest.raises(transaction.TransactionError, match="unsafe"):
        transaction._verify_current_telegram_paths(journal, actual, actual["recipe"])


def test_schema5_rollback_keeps_a_linked_settings_referent(tmp_path: Path) -> None:
    root, journal, paths = _rollback_fixture(tmp_path)
    state_dir = paths["state"]
    (state_dir / "refs").mkdir()
    referent = state_dir / "refs" / "settings.json"
    referent.write_bytes(b'{"allowed":[1]}')
    entry = state_dir / "chat_settings.json"
    entry.symlink_to("/app/state/refs/settings.json")
    replies = state_dir / "channel_replies.json"
    replies.write_bytes(b'{"channel":"c"}\n')
    journal["migrations"] = [
        {"kind": "chat-settings", "action": "validate-existing",
         "effective_path": "/app/state/chat_settings.json", "source_path": None,
         "target_path": str(referent), "source_before": None,
         "target_before": transaction._state_preimage(str(referent)), "created": None},
        {"kind": "channel-replies", "action": "none",
         "effective_path": "/app/state/channel_replies.json", "source_path": None,
         "target_path": str(replies), "source_before": None,
         "target_before": transaction._state_preimage(str(replies)), "created": None}]
    calls = []

    class Reader:
        StateError = type("StateError", (Exception,), {})

        @staticmethod
        def canonical(root_path: Path, *, host_path: str, **kwargs: object) -> dict:
            calls.append((host_path, kwargs["state_kind"]))
            return {"raw_sha256": "a" * 64, "canonical_sha256": "a" * 64}

    original = transaction._state_module
    transaction._state_module = lambda: Reader  # type: ignore[assignment]
    try:
        before = (entry.lstat().st_ino, os.readlink(entry), referent.read_bytes())
        transaction._preserved_state_rollback(root, journal)
        # The old reader is asked about the file the link names, and rollback
        # neither recreates nor re-points the installed link.
        assert calls == [(str(referent), "chat-settings"), (str(replies), "channel-replies")]
        assert (entry.lstat().st_ino, os.readlink(entry), referent.read_bytes()) == before
    finally:
        transaction._state_module = original


def test_schema5_pending_native_copy_stays_pending_until_the_product_starts(tmp_path: Path) -> None:
    """The transaction records the startup copy and never performs it."""
    data, state_dir = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state_dir.mkdir()
    settings, replies = data / "chat_settings.json", data / "channel_replies.json"
    settings.write_bytes(b'{"allowed":[1]}')
    replies.write_bytes(b'{"channel":"c"}\n')
    entries = {"chat-settings": state_dir / "chat_settings.json",
               "channel-replies": state_dir / "channel_replies.json"}
    actual = _leaf_recipe(data, state_dir)
    journal = {"schema_version": 5, "kind": "component-runtime",
               "transaction_id": "20261106T110000Z-" + "6" * 32, "selection": ["telegram"],
               "migrations": [
                   {"kind": kind, "action": "copy-if-absent",
                    "effective_path": f"/app/state/{entry.name}",
                    "source_path": str(settings if kind == "chat-settings" else replies),
                    "target_path": str(entry),
                    "source_before": transaction._state_preimage(
                        str(settings if kind == "chat-settings" else replies)),
                    "target_before": {"present": False, "sha256": None, "stat": None},
                    "created": None}
                   for kind, entry in entries.items()]}
    # Nothing was copied: the legacy settings file is still the only bytes.
    assert entries["chat-settings"].exists() is False
    transaction._verify_probed_preimages(journal, actual["recipe"])
    transaction._telegram_state_bindings(journal, [actual])
    # Historical schemas keep refusing an unrecorded startup copy.
    with pytest.raises(transaction.TransactionError, match="changed or requires relocation"):
        transaction._verify_probed_preimages(dict(journal, schema_version=4), actual["recipe"])
    with pytest.raises(transaction.TransactionError, match="relocation is unsupported"):
        transaction._telegram_state_bindings(dict(journal, schema_version=4), [actual])
    # The staged legacy replies file the plan names has to exist as well.
    with pytest.raises(transaction.TransactionError, match="Telegram legacy state changed"):
        transaction._verify_probed_preimages(
            {"schema_version": 5, "kind": "component-runtime", "migrations": [
                dict(item, source_before={"present": False, "sha256": None, "stat": None})
                for item in journal["migrations"]]}, actual["recipe"])
    # Once the product copies, the recorded path is a regular file it owns and
    # the distinct legacy file must still be untouched.
    entries["chat-settings"].write_bytes(b'{"allowed":[1]}')
    transaction._telegram_state_bindings(journal, [actual])
    replies.unlink()
    with pytest.raises(transaction.TransactionError, match="legacy Telegram state disappeared"):
        transaction._telegram_state_bindings(journal, [actual])


def test_api_ready_probe_script_formats_loopback_addresses_and_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """_ready executes an inline probe script that respects WUWATERM_API_BIND and WUWATERM_API_PORT."""
    recorded_commands: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        recorded_commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    transaction._ready("test-container-id")
    assert recorded_commands, "subprocess.run was not called"
    cmd = recorded_commands[0]
    assert cmd[:6] == ["docker", "exec", "test-container-id", "python", "-B", "-c"]
    inline_script = cmd[6]

    # Verify execution of the inline script under different environment configurations:
    def eval_probe(env_vars: dict[str, str]) -> str:
        requested_urls: list[str] = []

        class DummyResponse:
            status = 200

        def dummy_urlopen(url: str, *a: Any, **kw: Any) -> DummyResponse:
            requested_urls.append(url)
            return DummyResponse()

        # Run the script with the requested environment and fake urlopen
        script_code = compile(inline_script, "<ready_probe>", "exec")
        env_backup = {k: os.environ.get(k) for k in ("WUWATERM_API_BIND", "WUWATERM_API_PORT")}
        try:
            for k in ("WUWATERM_API_BIND", "WUWATERM_API_PORT"):
                os.environ.pop(k, None)
            os.environ.update(env_vars)
            globs = {"__builtins__": __builtins__}
            locs: dict[str, Any] = {}
            with monkeypatch.context() as m:
                import urllib.request  # noqa: PLC0415
                m.setattr(urllib.request, "urlopen", dummy_urlopen)
                exec(script_code, globs, locs)  # noqa: S102
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        assert requested_urls, f"urlopen was not called for env {env_vars}"
        return requested_urls[0]

    # 1. Default (unset) -> http://127.0.0.1:8788/readyz
    assert eval_probe({}) == "http://127.0.0.1:8788/readyz"

    # 2. IPv4 loopback with custom port -> http://127.0.0.2:9000/readyz
    assert eval_probe({"WUWATERM_API_BIND": "127.0.0.2", "WUWATERM_API_PORT": "9000"}) == "http://127.0.0.2:9000/readyz"

    # 3. IPv6 loopback without brackets -> http://[::1]:8788/readyz
    assert eval_probe({"WUWATERM_API_BIND": "::1"}) == "http://[::1]:8788/readyz"

    # 4. IPv6 loopback with brackets -> http://[::1]:8788/readyz
    assert eval_probe({"WUWATERM_API_BIND": "[::1]"}) == "http://[::1]:8788/readyz"

