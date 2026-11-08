from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import component_state_copy_v4 as copy_v4
from scripts import component_state_endpoint as endpoint


TXN = "20261107T000000Z-" + "a" * 32
SETTINGS_BYTES = b'{"public":{"101":true},"allowed":[]}'
REPLIES = b'{"version":1,"entries":[]}'


def encode_hash(value: object) -> str:
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def mount(path: Path, host: str, read_only: bool) -> dict:
    info = path.stat()
    return {"container": str(path), "host": host, "read_only": read_only,
            "device": info.st_dev, "inode": info.st_ino}


def endpoint_request(path: Path, kind: str, mounts: list[dict], *, require_writer: bool) -> dict:
    return {"kind": kind, "recipe_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64,
            "image_revision": "3" * 40, "user": f"{os.geteuid()}:{os.getegid()}",
            "working_dir": str(path.parent), "configured_path": path.name,
            "effective_path": str(path), "mounts": mounts, "require_writer": require_writer}


def capture(path: Path, kind: str, mounts: list[dict], *, require_writer: bool = True) -> dict:
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", endpoint.ENDPOINT_CODE,
                           json.dumps(endpoint_request(path, kind, mounts, require_writer=require_writer))],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return endpoint.validate_endpoint(json.loads(proc.stdout))


def execute(request: dict, *, trace: bool = False) -> subprocess.CompletedProcess[str]:
    prefix = ""
    if trace:
        prefix = r'''import os,sys,stat,atexit
real_sync=os.fsync; real_truncate=os.ftruncate; events=[]
def traced_sync(fd):
 events.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'); return real_sync(fd)
def traced_truncate(fd,size):
 events.append('truncate'); return real_truncate(fd,size)
os.fsync=traced_sync; os.ftruncate=traced_truncate
atexit.register(lambda: sys.stderr.write('events:'+','.join(events)+'\n'))
'''
    return subprocess.run([sys.executable, "-I", "-B", "-c", prefix + copy_v4.COPY_CODE, json.dumps(request)],
                          capture_output=True, text=True, check=False)


def accepted(request: dict, *, trace: bool = False) -> tuple[dict, str]:
    proc = execute(request, trace=trace)
    assert proc.returncode == 0, proc.stderr
    assert SETTINGS_BYTES.decode() not in proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert set(result) == {"schema_version", "action", "binding_sha256", "reconciled", "endpoint"}
    assert result["schema_version"] == 1 and result["binding_sha256"] == request["binding_sha256"]
    endpoint.validate_endpoint(result["endpoint"])
    return result, proc.stderr


def refused(request: dict) -> None:
    proc = execute(request)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert proc.stderr == "state copy refused\n"


def setup_copy(tmp_path: Path, *, kind: str = "chat-settings", direction: str = "forward",
               link: bool | str = False, data: bytes | None = None) -> tuple[dict, Path, Path, Path]:
    source_root = tmp_path / "source"
    dest_root = tmp_path / "destination"
    source_root.mkdir()
    dest_root.mkdir()
    source = source_root / "source.json"
    source.write_bytes(data if data is not None else SETTINGS_BYTES if kind == "chat-settings" else REPLIES)
    target = dest_root / "state.json"
    old_referent = dest_root / "old.json"
    if direction == "rollback":
        old_referent.write_bytes(b'{"public":{},"allowed":[]}' if kind == "chat-settings" else REPLIES)
        if link == "chain":
            (dest_root / "middle.json").symlink_to(old_referent.name)
            target.symlink_to("middle.json")
        elif link:
            target.symlink_to(old_referent.name)
        else:
            target.write_bytes(old_referent.read_bytes())
    mounts = [mount(source_root, "/synthetic-source", True), mount(dest_root, "/synthetic-destination", False)]
    src = capture(source, kind, mounts, require_writer=False)
    dst = capture(target, kind, mounts)
    name = f".component-full-{TXN}-{kind}.{direction}"
    binding = {"schema_version": 2, "operation": "state-copy", "transaction_id": TXN,
               "state_kind": kind, "direction": direction, "source_endpoint_sha256": encode_hash(src),
               "destination_before_sha256": encode_hash(dst), "image_id": dst["image_id"],
               "image_revision": dst["image_revision"], "user": dst["user"], "recipe_sha256": dst["recipe_sha256"],
               "mounts_sha256": "4" * 64, "stage_path": str(Path(dst["writer"]["host_path"]).parent / name),
               "stage_parent_identity": dst["writer"]["parent_identity"],
               "expected_raw_sha256": src["referent"]["sha256"]}
    req = {"schema_version": 1, "action": "reserve", "phase": "intent", "binding": binding,
           "binding_sha256": encode_hash(binding), "source_endpoint": src, "destination_before": dst,
           "read_path": str(source), "mounts": mounts, "flavor": "legacy", "reservation": None,
           "staged": None, "cleanup": None, "discard": None, "reconcile_only": False}
    stage = Path(dst["writer"]["container_path"]).parent / name
    return req, stage, target, old_referent


def reserve_and_write(req: dict) -> tuple[dict, dict]:
    reserved, _ = accepted(req)
    req.update(action="write", phase="reserved", reservation=reserved["endpoint"])
    staged, _ = accepted(req)
    return reserved["endpoint"], staged["endpoint"]


def publish_request(req: dict, staged: dict) -> None:
    req.update(action="publish", phase="publish-intent", staged=staged)


def test_reservation_crash_partial_write_retry_publish_and_cleanup(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reservation, events = accepted(req, trace=True)
    assert events == "events:file,dir\n"
    assert stage.read_bytes() == b""
    retry, _ = accepted(req)
    assert retry["reconciled"] is True
    assert retry["endpoint"] == reservation["endpoint"]
    inode = stage.stat().st_ino
    stage.write_bytes(b"partial")
    req.update(action="write", phase="writing", reservation=reservation["endpoint"])
    result, events = accepted(req, trace=True)
    assert events == "events:truncate,file,file,dir\n"
    assert stage.stat().st_ino == inode and stage.read_bytes() == SETTINGS_BYTES
    publish_request(req, result["endpoint"])
    published, _ = accepted(req)
    assert published["reconciled"] is False
    assert target.read_bytes() == SETTINGS_BYTES and not stage.exists()
    assert target.stat().st_ino == inode
    req.update(action="cleanup", phase="intent", reservation=None, staged=None,
               cleanup={"created_entry_claim_sha256": req["binding_sha256"],
                        "post_stop_new_endpoint": published["endpoint"],
                        "old_published_endpoint": req["source_endpoint"]})
    removed, events = accepted(req, trace=True)
    assert events == "events:dir\n" and removed["reconciled"] is False
    assert not target.exists()
    repeated, _ = accepted(req)
    assert repeated["reconciled"] is True and not target.exists()
    bad = copy.deepcopy(req)
    bad["phase"] = "removed"
    refused(bad)


def test_cleanup_preserves_settings_lock_created_by_running_writer(tmp_path: Path) -> None:
    req, _, target, _ = setup_copy(tmp_path)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    published, _ = accepted(req)
    lock = target.with_name(target.name + ".lock")
    lock.write_bytes(b"")
    post = capture(target, "chat-settings", req["mounts"])
    assert post["writer"]["settings_lock_binding"]["type"] == "regular"
    req.update(action="cleanup", phase="intent", reservation=None, staged=None,
               cleanup={"created_entry_claim_sha256": req["binding_sha256"],
                        "post_stop_new_endpoint": post, "old_published_endpoint": req["source_endpoint"]})
    removed, _ = accepted(req)
    assert not target.exists() and lock.is_file()
    assert removed["endpoint"]["writer"]["settings_lock_binding"] == post["writer"]["settings_lock_binding"]
    assert published["endpoint"]["writer"]["settings_lock_binding"]["type"] == "absent"


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o777])
def test_reservation_mode_is_not_changed_by_inherited_umask(tmp_path: Path, mask: int) -> None:
    req, stage, _, _ = setup_copy(tmp_path)
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", copy_v4.COPY_CODE, json.dumps(req)],
                          capture_output=True, text=True, check=False, preexec_fn=lambda: os.umask(mask))
    assert proc.returncode == 0, proc.stderr
    assert stage.stat().st_mode & 0o777 == 0o600


def discard_request(req: dict, reservation: dict, copy_phase: str, staged: dict | None = None) -> None:
    req.update(action="discard", phase="intent", reservation=reservation, staged=staged,
               discard={"binding_sha256": req["binding_sha256"], "copy_phase": copy_phase})


@pytest.mark.parametrize("copy_phase", ["reserved", "writing"])
def test_discard_partial_reserved_inode_and_reconcile(tmp_path: Path, copy_phase: str) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reserved, _ = accepted(req)
    original_inode = stage.stat().st_ino
    stage.write_bytes(b"unfinished private bytes")
    assert stage.stat().st_ino == original_inode
    discard_request(req, reserved["endpoint"], copy_phase)
    removed, events = accepted(req, trace=True)
    assert events == "events:dir\n" and removed["reconciled"] is False
    assert removed["endpoint"]["entry"]["type"] == "absent"
    assert not stage.exists() and not target.exists()
    repeated, events = accepted(req, trace=True)
    assert events == "events:dir\n" and repeated["reconciled"] is True
    assert repeated["endpoint"] == removed["endpoint"]


@pytest.mark.parametrize("copy_phase", ["staged", "publish-intent"])
def test_discard_complete_staged_inode_and_reconcile(tmp_path: Path, copy_phase: str) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reservation, staged = reserve_and_write(req)
    discard_request(req, reservation, copy_phase, staged)
    removed, _ = accepted(req)
    assert removed["reconciled"] is False and not stage.exists() and not target.exists()
    repeated, _ = accepted(req)
    assert repeated["reconciled"] is True and repeated["endpoint"] == removed["endpoint"]


@pytest.mark.parametrize("copy_phase", ["writing", "staged"])
def test_discard_refuses_unknown_inode_with_identical_bytes(tmp_path: Path, copy_phase: str) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    if copy_phase == "staged":
        reservation, staged = reserve_and_write(req)
    else:
        reserved, _ = accepted(req)
        reservation, staged = reserved["endpoint"], None
        stage.write_bytes(SETTINGS_BYTES)
    discard_request(req, reservation, copy_phase, staged)
    held = stage.open("rb")
    try:
        stage.unlink()
        stage.write_bytes(SETTINGS_BYTES)
        stage.chmod(0o600)
        replacement = stage.stat().st_ino
        refused(req)
        assert stage.stat().st_ino == replacement and stage.read_bytes() == SETTINGS_BYTES and not target.exists()
    finally:
        held.close()


def test_discard_staged_requires_exact_full_preimage(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reservation, staged = reserve_and_write(req)
    discard_request(req, reservation, "staged", staged)
    stage.write_bytes(b"different partial bytes")
    inode = stage.stat().st_ino
    refused(req)
    assert stage.stat().st_ino == inode and stage.read_bytes() == b"different partial bytes" and not target.exists()


def test_discard_refuses_changed_destination_and_published_result(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reservation, staged = reserve_and_write(req)
    discarded = copy.deepcopy(req)
    discard_request(discarded, reservation, "publish-intent", staged)
    target.write_bytes(SETTINGS_BYTES)
    target.chmod(0o600)
    refused(discarded)
    assert target.read_bytes() == SETTINGS_BYTES and stage.read_bytes() == SETTINGS_BYTES
    target.unlink()
    publish_request(req, staged)
    accepted(req)
    refused(discarded)
    assert target.read_bytes() == SETTINGS_BYTES and not stage.exists()


def test_discard_requires_exact_durable_intent_and_source(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    reserved, _ = accepted(req)
    discard_request(req, reserved["endpoint"], "writing")
    for change in (
        {"binding_sha256": "f" * 64, "copy_phase": "writing"},
        {"binding_sha256": req["binding_sha256"], "copy_phase": "intent"},
        {"binding_sha256": req["binding_sha256"], "copy_phase": "staged"},
    ):
        invalid = copy.deepcopy(req)
        invalid["discard"] = change
        refused(invalid)
    invalid = copy.deepcopy(req)
    invalid["reservation"] = None
    refused(invalid)
    source = Path(req["read_path"])
    source.write_bytes(b'{"public":{},"allowed":[]}')
    refused(req)
    assert stage.exists() and stage.read_bytes() == b"" and not target.exists()


def test_unrecorded_nonempty_and_unknown_same_content_inode_refused(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    stage.write_bytes(b"unknown")
    stage.chmod(0o600)
    before = stage.stat()
    refused(req)
    assert stage.read_bytes() == b"unknown" and stage.stat().st_ino == before.st_ino and not target.exists()
    stage.unlink()
    reservation, _ = accepted(req)
    req.update(action="write", phase="writing", reservation=reservation["endpoint"])
    # Keep the original inode alive, so a same-content replacement cannot be reused by the filesystem.
    held = stage.open("rb")
    try:
        stage.unlink()
        stage.write_bytes(b"")
        stage.chmod(0o600)
        replacement = stage.stat().st_ino
        refused(req)
        assert stage.stat().st_ino == replacement and stage.read_bytes() == b"" and not target.exists()
    finally:
        held.close()


def test_rename_before_record_requires_recorded_stage_inode(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    stage.rename(target)
    reconciled, _ = accepted(req)
    assert reconciled["reconciled"] is True and target.read_bytes() == SETTINGS_BYTES
    held = target.open("rb")
    try:
        target.unlink()
        target.write_bytes(SETTINGS_BYTES)
        target.chmod(0o600)
        refused(req)
        assert target.read_bytes() == SETTINGS_BYTES
    finally:
        held.close()


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_rollback_preserves_settings_link_or_reply_old_referent(tmp_path: Path, kind: str) -> None:
    req, stage, target, old_referent = setup_copy(tmp_path, kind=kind, direction="rollback", link=True)
    old_bytes = old_referent.read_bytes()
    old_link = os.readlink(target)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    result, _ = accepted(req)
    expected = SETTINGS_BYTES if kind == "chat-settings" else REPLIES
    assert target.read_bytes() == expected and not stage.exists()
    if kind == "chat-settings":
        assert target.is_symlink() and os.readlink(target) == old_link
        assert old_referent.read_bytes() == expected
    else:
        assert not target.is_symlink() and old_referent.read_bytes() == old_bytes
    assert result["endpoint"]["referent"]["sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_changed_link_is_refused_without_destination_write(tmp_path: Path, kind: str) -> None:
    req, stage, target, old_referent = setup_copy(tmp_path, kind=kind, direction="rollback", link=True)
    old_bytes = old_referent.read_bytes()
    source_bytes = Path(req["read_path"]).read_bytes()
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    target.unlink()
    target.symlink_to("other.json")
    refused(req)
    assert os.readlink(target) == "other.json" and old_referent.read_bytes() == old_bytes
    assert stage.read_bytes() == source_bytes


def test_native_validator_rejects_invalid_stage_and_same_inode_can_retry(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path, data=b"not-json")
    reservation, _ = accepted(req)
    req.update(action="write", phase="reserved", reservation=reservation["endpoint"])
    refused(req)
    assert stage.read_bytes() == b"not-json" and not target.exists()
    assert stage.stat().st_ino == reservation["endpoint"]["referent"]["stat"]["inode"]


def test_changed_source_invalid_claim_and_wrong_owner_are_refused(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    invalid = copy.deepcopy(req)
    invalid["binding"]["expected_raw_sha256"] = "f" * 64
    refused(invalid)
    invalid = copy.deepcopy(req)
    invalid["destination_before"]["user"] = f"{os.geteuid() + 1}:{os.getegid()}"
    invalid["binding"]["destination_before_sha256"] = encode_hash(invalid["destination_before"])
    invalid["binding"]["user"] = invalid["destination_before"]["user"]
    invalid["binding_sha256"] = encode_hash(invalid["binding"])
    refused(invalid)
    source = Path(req["read_path"])
    source.write_bytes(b'{"public":{},"allowed":[]}')
    refused(req)
    assert not stage.exists() and not target.exists()


def test_destination_appearing_before_publish_is_never_replaced(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    target.write_bytes(SETTINGS_BYTES)
    target.chmod(0o600)
    inode = target.stat().st_ino
    refused(req)
    assert target.stat().st_ino == inode and target.read_bytes() == SETTINGS_BYTES
    assert stage.exists() and stage.stat().st_ino == staged["referent"]["stat"]["inode"]


def test_reconcile_only_never_performs_a_new_rename(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    req["reconcile_only"] = True
    refused(req)
    assert stage.exists() and not target.exists()
    stage.rename(target)
    result, events = accepted(req, trace=True)
    assert result["reconciled"] is True and target.stat().st_ino == staged["referent"]["stat"]["inode"]
    assert "dir" in events


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_rollback_rename_before_record_is_resynced_without_a_new_write(tmp_path: Path, kind: str) -> None:
    req, stage, target, original_referent = setup_copy(tmp_path, kind=kind, direction="rollback", link=True)
    original_bytes = original_referent.read_bytes()
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    writer = original_referent if kind == "chat-settings" else target
    os.replace(stage, writer)
    published_inode = writer.stat().st_ino
    req["reconcile_only"] = True
    result, events = accepted(req, trace=True)
    assert result["reconciled"] is True and "dir" in events
    assert writer.stat().st_ino == published_inode == staged["referent"]["stat"]["inode"]
    if kind == "chat-settings":
        assert target.is_symlink()
    else:
        assert not target.is_symlink() and original_referent.read_bytes() == original_bytes
    repeated, _ = accepted(req)
    assert repeated["reconciled"] is True and writer.stat().st_ino == published_inode


def test_same_content_source_replacement_is_not_authority(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    source = Path(req["read_path"])
    held = source.open("rb")
    try:
        source.unlink()
        source.write_bytes(SETTINGS_BYTES)
        refused(req)
        assert source.read_bytes() == SETTINGS_BYTES and not stage.exists() and not target.exists()
    finally:
        held.close()


def test_rename_reconciliation_refuses_replaced_reply_referent(tmp_path: Path) -> None:
    req, stage, target, old_referent = setup_copy(tmp_path, kind="channel-replies", direction="rollback", link=True)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    stage.rename(target)
    held = old_referent.open("rb")
    try:
        old_referent.unlink()
        old_referent.write_bytes(REPLIES)
        refused(req)
        assert target.read_bytes() == REPLIES and old_referent.read_bytes() == REPLIES
    finally:
        held.close()


def test_rename_reconciliation_checks_preserved_reply_chain(tmp_path: Path) -> None:
    req, stage, target, old_referent = setup_copy(tmp_path, kind="channel-replies", direction="rollback", link="chain")
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    stage.rename(target)
    middle = target.parent / "middle.json"
    # Replacing a preserved link with the same link bytes must not count as
    # retaining its identity; keep the inode open to rule out inode reuse.
    held = os.open(middle, os.O_PATH | os.O_NOFOLLOW)
    try:
        middle.unlink()
        middle.symlink_to(old_referent.name)
        refused(req)
        assert target.read_bytes() == REPLIES and os.readlink(middle) == old_referent.name
    finally:
        os.close(held)


def test_rename_reconciliation_checks_settings_link(tmp_path: Path) -> None:
    req, stage, target, old_referent = setup_copy(tmp_path, direction="rollback", link="chain")
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    stage.rename(old_referent)
    middle = target.parent / "middle.json"
    held = os.open(middle, os.O_PATH | os.O_NOFOLLOW)
    try:
        middle.unlink()
        middle.symlink_to(old_referent.name)
        refused(req)
        assert target.read_bytes() == SETTINGS_BYTES and stage.exists() is False
    finally:
        os.close(held)


def test_request_rejects_duplicate_or_unknown_fields(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    extra = copy.deepcopy(req)
    extra["unbound"] = True
    refused(extra)
    serialized = json.dumps(req)
    duplicated = '{"schema_version":1,' + serialized[1:]
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", copy_v4.COPY_CODE, duplicated],
                          capture_output=True, text=True, check=False)
    assert proc.returncode != 0 and proc.stdout == "" and proc.stderr == "state copy refused\n"
    assert not stage.exists() and not target.exists()


def test_cleanup_requires_exact_old_proof_latest_entry_and_absent_stage(tmp_path: Path) -> None:
    req, stage, target, _ = setup_copy(tmp_path)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    published, _ = accepted(req)
    req.update(action="cleanup", phase="intent", reservation=None, staged=None,
               cleanup={"created_entry_claim_sha256": req["binding_sha256"],
                        "post_stop_new_endpoint": published["endpoint"],
                        "old_published_endpoint": req["source_endpoint"]})
    stage.write_bytes(b"unknown")
    refused(req)
    assert target.read_bytes() == SETTINGS_BYTES and stage.read_bytes() == b"unknown"
    stage.unlink()
    source = Path(req["read_path"])
    held = source.open("rb")
    try:
        source.unlink()
        source.write_bytes(SETTINGS_BYTES)
        refused(req)
        assert target.read_bytes() == SETTINGS_BYTES and source.read_bytes() == SETTINGS_BYTES
    finally:
        held.close()


def test_source_from_recorded_read_only_parent_alias(tmp_path: Path) -> None:
    req, _, target, _ = setup_copy(tmp_path)
    # The real source namespace used /recorded/source; the running probe sees
    # only its physical parent through the supplied read-only alias.
    src = req["source_endpoint"]
    original = req["mounts"][0]["container"]
    recorded = "/recorded/source"

    def remap(path: str) -> str:
        assert path == original or path.startswith(original + "/")
        return recorded + path[len(original):]

    src["working_dir"] = remap(src["working_dir"])
    src["effective_path"] = remap(src["effective_path"])
    for item in (src["entry"], src["referent"], src["writer"], src["writer"]["settings_lock_binding"]):
        item["container_path"] = remap(item["container_path"])
        item["mount_identity"] = dict(item["mount_identity"], container=recorded)
    endpoint.validate_endpoint(src)
    req["binding"]["source_endpoint_sha256"] = encode_hash(src)
    req["binding_sha256"] = encode_hash(req["binding"])
    invalid = copy.deepcopy(req)
    invalid["mounts"][0]["read_only"] = False
    refused(invalid)
    _, staged = reserve_and_write(req)
    publish_request(req, staged)
    published, _ = accepted(req)
    assert target.read_bytes() == SETTINGS_BYTES
    req.update(action="cleanup", phase="intent", reservation=None, staged=None,
               cleanup={"created_entry_claim_sha256": req["binding_sha256"],
                        "post_stop_new_endpoint": published["endpoint"],
                        "old_published_endpoint": src})
    accepted(req)
    assert not target.exists()
