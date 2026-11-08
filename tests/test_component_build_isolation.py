from __future__ import annotations

import ast
import fcntl
import gzip
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import textwrap
import time

import pytest

from scripts.component_build_isolation import (
    IsolationError, ProducerSession, check_sealing_capability, parent_create_argv, parent_name, publish_oci_archive,
    sealed_oci_input, validate_oci_archive, verify_image_revision, verify_imported_image, verify_parent_snapshot,
)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_session_ledger_mutations_are_confined_to_validated_transitions() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ProducerSession)))
    aliases = {"parent", "worker", "probe", "record", "receipt", "archive", "previous", "clients"}

    class Audit(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions = []
            self.guarded = 0
            self.unprotected = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        @staticmethod
        def root(node: ast.AST) -> str:
            while isinstance(node, (ast.Subscript, ast.Attribute)):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "self" and node.attr in ("attempt", "ledger")):
                    return "self." + node.attr
                node = node.value
            return node.id if isinstance(node, ast.Name) else ""

        def visit_With(self, node: ast.With) -> None:
            guarded = any(isinstance(item.context_expr, ast.Call)
                          and isinstance(item.context_expr.func, ast.Attribute)
                          and isinstance(item.context_expr.func.value, ast.Name)
                          and item.context_expr.func.value.id == "self"
                          and item.context_expr.func.attr == "transition" for item in node.items)
            self.guarded += guarded
            self.generic_visit(node)
            self.guarded -= guarded

        def check(self, node: ast.AST, target: ast.AST) -> None:
            root = self.root(target)
            if root not in aliases | {"self.attempt", "self.ledger"}:
                return
            if self.guarded or self.functions[-1] in ("__init__", "transition"):
                return
            # The only two release callbacks run inside client.persist's transition.
            if self.functions[-1] == "released" and self.functions[-2] in ("import_image", "tag_image"):
                return
            self.unprotected.append((node.lineno, tuple(self.functions), root))

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if isinstance(target, (ast.Subscript, ast.Attribute)):
                    self.check(node, target)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("update", "append", "extend", "pop", "clear"):
                self.check(node, node.func.value)
            self.generic_visit(node)

    audit = Audit()
    audit.visit(tree)
    assert audit.unprotected == []
    client = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "client")
    persist = next(n for n in client.body if isinstance(n, ast.FunctionDef) and n.name == "persist")
    assert any(isinstance(n, ast.With)
               and any(isinstance(item.context_expr, ast.Call)
                       and isinstance(item.context_expr.func, ast.Attribute)
                       and item.context_expr.func.attr == "transition" for item in n.items)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "on_release"
                       for c in ast.walk(n)) for n in persist.body)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _archive(case: str = "valid") -> bytes:
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w") as layer:
        name = "../escape" if case == "layer-path" else "app/version"
        member = tarfile.TarInfo(name)
        if case in ("layer-link", "layer-absolute-link", "layer-absolute-traversal", "layer-absolute-hardlink"):
            member.type = tarfile.LNKTYPE if case == "layer-absolute-hardlink" else tarfile.SYMTYPE
            member.linkname = {"layer-link": "../../escape", "layer-absolute-link": "/usr/bin/tool",
                               "layer-absolute-traversal": "/../escape", "layer-absolute-hardlink": "/usr/bin/tool"}[case]
            layer.addfile(member)
        elif case == "layer-device":
            member.type = tarfile.CHRTYPE
            layer.addfile(member)
        else:
            data = b"1.0\n"
            member.size = len(data)
            member.mode = 0o644
            layer.addfile(member, io.BytesIO(data))
            if case == "layer-duplicate":
                layer.addfile(member, io.BytesIO(data))
    layer_raw = layer_stream.getvalue()
    layer_data = gzip.compress(layer_raw, mtime=0)
    config = {"os": "linux", "architecture": "amd64", "config": {},
              "rootfs": {"type": "layers", "diff_ids": [_digest(layer_raw)]}}
    config_raw = _json(config)
    config_descriptor = {"mediaType": "application/vnd.oci.image.config.v1+json",
                         "digest": _digest(config_raw), "size": len(config_raw)}
    if case == "url":
        config_descriptor["urls"] = ["https://example.invalid/blob"]
    layer_descriptor = {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": _digest(layer_data), "size": len(layer_data)}
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config_descriptor, "layers": [layer_descriptor]}
    if case == "subject":
        manifest["subject"] = config_descriptor
    manifest_raw = _json(manifest)
    manifest_descriptor = {"mediaType": manifest["mediaType"], "digest": _digest(manifest_raw),
                           "size": len(manifest_raw), "platform": {"os": "linux", "architecture": "amd64"}}
    if case == "tag":
        manifest_descriptor["annotations"] = {"org.opencontainers.image.ref.name": "unsafe:tag"}
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [manifest_descriptor]}
    if case == "multiple":
        index["manifests"].append(manifest_descriptor)
    entries = {"oci-layout": _json({"imageLayoutVersion": "1.0.0"}), "index.json": _json(index),
               "blobs/sha256/" + _digest(config_raw)[7:]: config_raw,
               "blobs/sha256/" + _digest(manifest_raw)[7:]: manifest_raw,
               "blobs/sha256/" + _digest(layer_data)[7:]: b"bad" if case == "blob" else layer_data}
    if case == "outer-path":
        entries["../escape"] = b"x"
    if case == "unreferenced":
        entries["blobs/sha256/" + "0" * 64] = b"x"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name in ("blobs", "blobs/sha256"):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
            archive.addfile(entry)
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue() + (b"unexpected" if case == "trailer" else b"")


def test_sealed_input_matches_validated_graph_and_cannot_change(tmp_path: Path) -> None:
    check_sealing_capability()
    archive = _archive()
    path = tmp_path / "image.tar"
    path.write_bytes(archive)
    with sealed_oci_input(path) as (fd, proof):
        assert proof["archive_sha256"] == hashlib.sha256(archive).hexdigest()
        assert proof["platform"] == {"os": "linux", "architecture": "amd64"}
        assert len(proof["layer_digests"]) == len(proof["diff_ids"]) == 1
        assert len(proof["layer_inventories_sha256"][0]) == 64
        assert fcntl.fcntl(fd, 1034) & 0x0008  # Linux F_GET_SEALS / F_SEAL_WRITE
        with pytest.raises(OSError):
            os.write(fd, b"tamper")
        path.write_bytes(b"changed after sealing")
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, len(archive) + 1) == archive
    with pytest.raises(IsolationError, match="digest differs"):
        with sealed_oci_input(path, expected_sha256=proof["archive_sha256"]):
            pass


def test_canonical_absolute_symlink_is_relative_to_the_image_root() -> None:
    proof = validate_oci_archive(io.BytesIO(_archive("layer-absolute-link")))
    assert proof["platform"] == {"os": "linux", "architecture": "amd64"}


def test_publish_persists_only_validated_bytes_without_overwriting(tmp_path: Path) -> None:
    source, destination = tmp_path / "export.tar", tmp_path / "sealed.tar"
    source.write_bytes(_archive())
    proof = publish_oci_archive(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o444
    with sealed_oci_input(destination, expected_sha256=proof["archive_sha256"]) as (_, repeated):
        assert repeated == proof
    with pytest.raises(IsolationError, match="persist"):
        publish_oci_archive(source, destination)
    source.write_bytes(_archive("tag"))
    with pytest.raises(IsolationError):
        publish_oci_archive(source, tmp_path / "invalid.tar")
    assert not (tmp_path / "invalid.tar").exists()


def test_image_binding_requires_the_observed_store_and_matches_the_graph() -> None:
    proof = validate_oci_archive(io.BytesIO(_archive()))
    daemon = {"id": "fixture-daemon", "server_version": "29.1.3", "os": "linux", "architecture": "x86_64",
              "driver_type": "io.containerd.snapshotter.v1"}
    image = {"Id": proof["manifest_digest"], "Os": "linux", "Architecture": "amd64",
             "RootFS": {"Type": "layers", "Layers": proof["diff_ids"]}, "Config": proof["config"],
             "RepoTags": ["other-attempt:retained"]}
    assert verify_imported_image(daemon, daemon, image, proof) == proof["manifest_digest"]
    for changed in ({**image, "Id": proof["config_digest"]}, {**image, "Config": {"Env": ["DRIFT=1"]}},
                    {**image, "RootFS": {"Type": "layers", "Layers": []}}):
        with pytest.raises(IsolationError, match="differs"):
            verify_imported_image(daemon, daemon, changed, proof)
    for changed in ({**daemon, "id": "other-daemon"}, {**daemon, "driver_type": "overlay2"},
                    {**daemon, "server_version": "29.1.2"}):
        with pytest.raises(IsolationError, match="daemon or image store"):
            verify_imported_image(changed, daemon, image, proof)


def test_validated_image_revision_must_match_source_commit() -> None:
    proof = validate_oci_archive(io.BytesIO(_archive()))
    proof["config"] = {"Labels": {"org.opencontainers.image.revision": "a" * 40}}
    verify_image_revision(proof, "a" * 40)
    with pytest.raises(IsolationError, match="revision differs"):
        verify_image_revision(proof, "b" * 40)


def test_parent_binding_requires_the_same_unit_and_positive_population() -> None:
    name = parent_name("20261106T110000Z-0123456789abcdef0123456789abcdef")
    assert name in parent_create_argv(name)
    original = {"name": name, "description": "WuwaTerm isolated build", "boot_id": "boot",
                "invocation_id": "invocation", "control_group": "/" + name,
                "directory": {"device": 1, "inode": 2}, "events": {"device": 1, "inode": 3},
                "populated": 1}
    observed = {**original, "populated": 0}
    verify_parent_snapshot(original, observed, populated=0)
    for changed in ({**observed, "populated": 1}, {**observed, "invocation_id": "other"},
                    {**observed, "events": {"device": 1, "inode": 4}}):
        with pytest.raises(IsolationError, match="parent identity or population"):
            verify_parent_snapshot(original, changed, populated=0)
    with pytest.raises(IsolationError):
        parent_name("../../wrong")


@pytest.mark.parametrize("case", ["tag", "multiple", "subject", "url", "outer-path", "layer-path",
                                  "layer-link", "layer-absolute-traversal", "layer-absolute-hardlink", "layer-duplicate",
                                  "layer-device", "blob", "unreferenced", "trailer"])
def test_invalid_graphs_are_rejected_before_import(case: str) -> None:
    with pytest.raises(IsolationError):
        validate_oci_archive(io.BytesIO(_archive(case)))


_CLIENT_SUPERVISOR = r"""
import fcntl, json, os, sys, threading
from pathlib import Path
root = Path(sys.argv[1])
mode = sys.argv[2]
sys.path.insert(0, sys.argv[3])
from component_build_isolation import IsolationError, run_guarded_client
fd = os.open(root / '.deployments/.deployment.lock', os.O_RDWR)
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
cancelled = [False]
def persist(record):
    if mode == 'before-identity' and record['state'] == 'armed':
        os._exit(91)
    with (root / 'records.jsonl').open('a') as output:
        output.write(json.dumps(record) + '\n')
        output.flush()
        os.fsync(output.fileno())
    if mode == 'cancel-before-release' and record['state'] == 'armed':
        cancelled[0] = True
    if record['state'] == mode:
        os._exit(91)
target = str(Path(sys.executable).resolve())
code = "from pathlib import Path; import time; Path(%r).write_text('GO'); time.sleep(60)" % str(root / 'go-marker')
try:
    run_guarded_client(root, 'fixture-client', [target, '-c', code],
                       {'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8'}, root / '.deployments/attempt', 1, persist,
                       timeout=90, cancelled=lambda:cancelled[0], release_lock=threading.RLock())
except IsolationError:
    if mode != 'cancel-before-release': raise
"""


def _locked(root: Path) -> bool:
    import fcntl
    fd = os.open(root / ".deployments/.deployment.lock", os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def _eventually(predicate, *, timeout: float = 10) -> bool:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _supervisor(root: Path, mode: str) -> subprocess.Popen[bytes]:
    (root / ".deployments/attempt").mkdir(parents=True)
    (root / ".deployments/.deployment.lock").touch(mode=0o600)
    runner = root / "supervisor.py"
    runner.write_text(_CLIENT_SUPERVISOR)
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    return subprocess.Popen(["/usr/bin/python3", "-I", "-B", str(runner), str(root), mode, str(scripts)],
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize(("crash_state", "last_state"), [
    ("before-identity", "planned"), ("armed", "armed"), ("release-intent", "release-intent"),
])
def test_barrier_owner_death_before_go_never_starts_client(tmp_path: Path, crash_state: str,
                                                          last_state: str) -> None:
    process = _supervisor(tmp_path, crash_state)
    _, stderr = process.communicate(timeout=15)
    assert process.returncode == 91, stderr.decode(errors="replace")
    assert _eventually(lambda: not _locked(tmp_path))
    assert not (tmp_path / "go-marker").exists()
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert records[-1]["state"] == last_state


def test_durable_cancellation_before_go_prevents_client_execution(tmp_path: Path) -> None:
    process = _supervisor(tmp_path, "cancel-before-release")
    _, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr.decode(errors="replace")
    assert _eventually(lambda: not _locked(tmp_path))
    assert not (tmp_path / "go-marker").exists()
    records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert records[-1]["state"] == "armed"


def test_sealed_archive_descriptor_reaches_guarded_client_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".deployments/attempt").mkdir(parents=True)
    (tmp_path / ".deployments/.deployment.lock").touch(mode=0o600)
    data = _archive()
    (tmp_path / "image.tar").write_bytes(data)
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    program = """import fcntl,os,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,sys.argv[2])
from component_build_isolation import run_guarded_client,sealed_oci_input
lock=os.open(root/'.deployments/.deployment.lock',os.O_RDWR)
if lock!=9:os.dup2(lock,9);os.close(lock)
fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)
records=[]
with sealed_oci_input(root/'image.tar') as (fd,proof):
 code='import hashlib,os,sys; print(hashlib.sha256(os.read(int(sys.argv[1]),1000000)).hexdigest())'
 result=run_guarded_client(root,'fixture-read',[str(Path(sys.executable).resolve()),'-c',code,str(fd)],
  {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},root/'.deployments/attempt',1,records.append,extra_fds=(fd,))
 assert result['exit_code']==0
 print((root/records[-1]['stdout_path']).read_text().strip())
"""
    result = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", program, str(tmp_path), str(scripts)],
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(data).hexdigest()


def test_dead_owner_client_is_fenced_with_pidfd_before_fd9_is_released(tmp_path: Path) -> None:
    process = _supervisor(tmp_path, "never")
    try:
        assert _eventually(lambda: (tmp_path / "go-marker").exists())
        records = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
        client = records[-1]
        assert client["state"] == "release-intent" and _locked(tmp_path)
        process.kill()
        process.wait(timeout=10)
        assert _locked(tmp_path)
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        program = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from component_build_isolation import acquire_recovery_fd9, fence_recorded_client
root=Path(sys.argv[2]); client=json.loads((root/'records.jsonl').read_text().splitlines()[-1])
stale={**client,'start_ticks':client['start_ticks']+1}
assert fence_recorded_client(root,stale)=='reused-pid'
print(fence_recorded_client(root,client))
assert acquire_recovery_fd9(root)==client['lock']
print('locked')
"""
        result = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", program, str(scripts), str(tmp_path)],
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["fenced", "locked"]
        assert _eventually(lambda: not _locked(tmp_path))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


_CONTROL_SUPERVISOR = r"""
import fcntl,json,os,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,sys.argv[2])
from component_build_isolation import CancelEndpoint
fd=os.open(root/'.deployments/.deployment.lock',os.O_RDWR)
if fd!=9: os.dup2(fd,9);os.close(fd)
fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)
def save(record):
    tmp=root/'control.next'
    with tmp.open('x') as f:json.dump(record,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,root/'control.json')
    d=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(d);os.close(d)
def mark():
    with (root/'cancel-intent').open('x') as f:f.write('durable');f.flush();os.fsync(f.fileno())
    d=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(d);os.close(d)
with CancelEndpoint(root,'source','20261106T110000Z-0123456789abcdef0123456789abcdef',save,mark) as endpoint:
    if not endpoint.cancelled.wait(20): raise RuntimeError('cancel was not received')
"""


def test_live_cancel_verifies_owner_and_acknowledges_durable_intent(tmp_path: Path) -> None:
    (tmp_path / ".deployments").mkdir()
    (tmp_path / ".deployments/.deployment.lock").touch(mode=0o600)
    runner = tmp_path / "control-supervisor.py"
    runner.write_text(_CONTROL_SUPERVISOR)
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    owner = subprocess.Popen(["/usr/bin/python3", "-I", "-B", str(runner), str(tmp_path), str(scripts)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert _eventually(lambda: (tmp_path / "control.json").exists()
                           and json.loads((tmp_path / "control.json").read_text())["state"] == "listening")
        code = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from component_build_isolation import IsolationError, request_live_cancel
root=Path(sys.argv[2]); control=json.loads((root/'control.json').read_text())
wrong={**control,'nonce':'0'*64}
try:request_live_cancel(root,'source',control['build_id'],wrong)
except IsolationError:pass
else:raise RuntimeError('wrong control accepted')
assert not (root/'cancel-intent').exists()
print(request_live_cancel(root,'source',control['build_id'],control))
assert (root/'cancel-intent').read_text()=='durable'
"""
        result = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code, str(scripts), str(tmp_path)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "requested"
        _, stderr = owner.communicate(timeout=10)
        assert owner.returncode == 0, stderr.decode(errors="replace")
        code = """import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from component_build_isolation import request_live_cancel
root=Path(sys.argv[2]); c=json.loads((root/'control.json').read_text())
print(request_live_cancel(root,'source',c['build_id'],c))
"""
        after = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", code, str(scripts), str(tmp_path)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               timeout=10, check=False)
        assert after.returncode == 0, after.stderr
        assert after.stdout.strip() == "owner-dead"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
