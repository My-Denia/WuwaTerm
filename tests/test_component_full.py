from __future__ import annotations

import copy
import errno
import hashlib
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import time

import pytest

from scripts import component_full as full
from scripts import component_runtime as runtime
from scripts import component_state as state
from test_component_runtime import journal as runtime_fixture


@pytest.fixture
def full_journal(tmp_path: Path) -> dict:
    sample = runtime_fixture.__wrapped__(tmp_path)
    identifier = sample["transaction_id"]
    previous_id = "20261106T100000Z-" + "1" * 32
    predecessor_id = "20261106T103000Z-" + "2" * 32
    previous = copy.deepcopy(sample["target"]["manifest"])
    previous["deployment_id"] = previous_id
    target = copy.deepcopy(previous)
    target["deployment_id"] = identifier
    candidate_path = f".deployments/component-full-candidates/{identifier}.db"
    candidate = copy.deepcopy(previous["database"])
    candidate.update(path=str(tmp_path / candidate_path), sha256="c" * 64)
    target["database"] = {**candidate, "path": previous["database"]["path"]}
    source = copy.deepcopy(sample["source"])
    source["compose"] = {"path": ".deployments/component-compose-" + "d" * 64, "sha256": "d" * 64}
    permissions = {"mode": 0o600, "uid": 1000, "gid": 1000}
    old_hash = previous["database"]["sha256"]
    backup = (f"data/deployment-backups/terms.{identifier[:16]}-{int(identifier[17:], 16)}."
              f"{old_hash}.db")
    stage = str(Path(previous["database"]["path"]).parent / f".component-full-promote-{identifier}.db")
    restore = str(Path(previous["database"]["path"]).parent / f".component-full-restore-{identifier}.db")
    before_containers = copy.deepcopy(sample["before"]["containers"])
    return {
        "schema_version": 3, "kind": "component-full", "transaction_id": identifier,
        "action": "update", "status": "active", "phase": "prepared", "source": source,
        "predecessor": runtime.predecessor_reference("runtime", predecessor_id, b"terminal\n"),
        "selection": [],
        "before": {"manifest": previous, "topology": copy.deepcopy(previous["components"]),
                   "containers": before_containers},
        "target": {"manifest": target, "containers": copy.deepcopy(before_containers)},
        "database": {
            "old": {"binding": previous["database"], "stat": sample["database"]["stat"],
                    "permissions": permissions},
            "candidate": {"path": candidate_path, "binding": candidate, "stat": sample["database"]["stat"],
                          "verification": copy.deepcopy(full._VERIFY)},
            "backup": {"path": backup, "sha256": old_hash, "stat": None, "permissions": permissions},
            "stage": {"path": stage, "sha256": candidate["sha256"], "stat": None, "permissions": permissions},
            "restore": {"path": restore, "sha256": old_hash, "stat": None, "permissions": permissions},
            "promoted_stat": None, "restored_stat": None,
        },
        "api_identity": sample["api_identity"], "migrations": {"status": "pending", "items": []},
        "pointers": {
            "old": {".deploy_commit": {"present": True, "bytes": source["commit"] + "\n"},
                    ".deploy_current": {"present": True, "bytes": previous_id + "\n"}},
            "target": {".deploy_commit": {"present": True, "bytes": source["commit"] + "\n"},
                       ".deploy_current": {"present": True, "bytes": identifier + "\n"}},
        },
        "manifest": {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                     "sha256": hashlib.sha256(full.manifest._encode(target)).hexdigest()},
    }


def test_full_journal_binds_owned_candidate_backup_stage_and_predecessor(full_journal: dict) -> None:
    assert full.validate_journal(full_journal) is full_journal
    assert full_journal["selection"] == []
    assert full_journal["migrations"] == {"status": "pending", "items": []}


def test_full4_prepared_record_and_full3_predecessor_remain_readable(full_journal: dict) -> None:
    assert full.validate_journal(full_journal)["schema_version"] == 3
    successor = copy.deepcopy(full_journal)
    successor["schema_version"] = 4
    assert full.validate_journal(successor) is successor
    successor["phase"] = "rollback-untouched"
    assert full.validate_journal(successor) is successor
    old = copy.deepcopy(successor)
    old["schema_version"] = 3
    with pytest.raises(full.FullError, match="old schema"):
        full.validate_journal(old)
    successor["phase"] = "prepared"
    successor["migrations"] = {"status": "planned", "items": [{"kind": "chat-settings"}]}
    with pytest.raises(full.FullError):
        full.validate_journal(successor)


def test_full4_copy_evidence_tracks_one_reserved_inode_through_rename(tmp_path: Path) -> None:
    from test_component_state_endpoint import captured, request

    root = tmp_path / "state"
    root.mkdir()
    source, destination = root / "source", root / "destination"
    source.write_bytes(b"fixture copy payload")
    identifier = "20261106T110000Z-" + "3" * 32
    stage = root / f".component-full-{identifier}-channel-replies.forward"

    def observe(path: Path) -> dict:
        payload = request(root, path, kind="channel-replies")
        payload["mounts"][0]["host"] = str(root)
        return captured(payload)

    origin, target = observe(source), observe(destination)
    binding = {"schema_version": 2, "operation": "state-copy", "transaction_id": identifier,
               "state_kind": "channel-replies", "direction": "forward",
               "source_endpoint_sha256": full._endpoint_hash(origin),
               "destination_before_sha256": full._endpoint_hash(target),
               "image_id": target["image_id"], "image_revision": target["image_revision"], "user": target["user"],
               "recipe_sha256": target["recipe_sha256"], "mounts_sha256": "a" * 64,
               "stage_path": str(stage), "stage_parent_identity": target["writer"]["parent_identity"],
               "expected_raw_sha256": origin["referent"]["sha256"]}
    progress = {"binding": binding, "phase": "intent", "stage_before": observe(stage),
                "reservation": None, "staged": None, "published": None, "discard": None}
    kwargs = {"journal_id": identifier, "state_kind": "channel-replies", "direction": "forward",
              "source": origin, "destination": target, "image": target}
    full._copy_progress_v4(progress, **kwargs)
    stage.touch(mode=0o600)
    progress.update(phase="reserved", reservation=observe(stage))
    full._copy_progress_v4(progress, **kwargs)
    stage.write_bytes(source.read_bytes())
    progress.update(phase="staged", staged=observe(stage))
    full._copy_progress_v4(progress, **kwargs)
    replacement = root / "replacement"
    replacement.write_bytes(source.read_bytes())
    os.replace(replacement, stage)
    altered = copy.deepcopy(progress)
    altered["staged"] = observe(stage)
    with pytest.raises(full.FullError, match="staged contents differ"):
        full._copy_progress_v4(altered, **kwargs)
    stage.unlink()
    # Preserve the original reserved inode for the publication check.
    # The previous stage was deliberately replaced, so make a fresh reservation.
    stage.touch(mode=0o600)
    progress.update(phase="reserved", reservation=observe(stage), staged=None)
    stage.write_bytes(source.read_bytes())
    progress.update(phase="staged", staged=observe(stage))
    os.replace(stage, destination)
    progress.update(phase="published", published=observe(destination))
    full._copy_progress_v4(progress, **kwargs)
    replacement.write_bytes(source.read_bytes())
    os.replace(replacement, destination)
    altered = copy.deepcopy(progress)
    altered["published"] = observe(destination)
    with pytest.raises(full.FullError, match="published endpoint differs"):
        full._copy_progress_v4(altered, **kwargs)


def test_full4_writer_comparison_distinguishes_reply_aliases(tmp_path: Path) -> None:
    from test_component_state_endpoint import captured, request

    root = tmp_path / "state"
    root.mkdir()
    (root / "referent").write_bytes(b"shared")
    (root / "first").symlink_to("referent")
    (root / "second").symlink_to("referent")

    def observe(path: Path, kind: str) -> dict:
        payload = request(root, path, kind=kind)
        payload["mounts"][0]["host"] = str(root)
        return captured(payload)

    settings_first = observe(root / "first", "chat-settings")
    settings_second = observe(root / "second", "chat-settings")
    replies_first = observe(root / "first", "channel-replies")
    replies_second = observe(root / "second", "channel-replies")
    assert full._same_writer_view(settings_first, settings_second)
    assert not full._same_writer_view(replies_first, replies_second)
    (root / "replacement").write_bytes(b"shared")
    os.replace(root / "replacement", root / "referent")
    assert not full._same_writer_view(replies_first, observe(root / "first", "channel-replies"))


def test_full4_reply_shutdown_replacement_retains_original_chain_and_referent(tmp_path: Path) -> None:
    from test_component_state_endpoint import captured, request

    root = tmp_path / "state"
    root.mkdir()
    referent, middle, entry = root / "referent", root / "middle", root / "entry"
    referent.write_bytes(b"original")
    middle.symlink_to("referent")
    entry.symlink_to("middle")

    def observe(path: Path) -> dict:
        payload = request(root, path, kind="channel-replies")
        payload["mounts"][0]["host"] = str(root)
        return captured(payload)

    before = observe(entry)
    replacement = root / "replacement"
    replacement.write_bytes(b"progressed")
    os.replace(replacement, entry)
    stopped = observe(entry)
    assert not full._same_endpoint_route(before, stopped)
    assert full._same_endpoint_route(before, stopped, allow_reply_replacement=True)
    proof = {"links": [observe(middle)], "referent": observe(referent)}
    full._retained_reply_proof(before, proof)
    held = referent.open("rb")
    try:
        referent.unlink()
        referent.write_bytes(b"original")
        altered = {"links": [observe(middle)], "referent": observe(referent)}
        with pytest.raises(full.FullError, match="retained reply"):
            full._retained_reply_proof(before, altered)
    finally:
        held.close()


def test_full4_quiescent_reply_transition_requires_retained_proof(tmp_path: Path) -> None:
    from test_component_state_endpoint import bind, captured, request

    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    referent, entry, target = old_root / "referent", old_root / "entry", new_root / "target"
    referent.write_bytes(b"original")
    entry.symlink_to("referent")
    mounts = [bind(old_root, str(old_root)), bind(new_root, str(new_root))]

    def observe(path: Path) -> dict:
        return captured(request(path.parent, path, kind="channel-replies", mounts=mounts))

    original, destination = observe(entry), observe(target)
    replacement = old_root / "replacement"
    replacement.write_bytes(b"progressed")
    os.replace(replacement, entry)
    stopped = observe(entry)
    item = {"kind": "channel-replies", "action": "copy-if-absent", "old_endpoint": original,
            "new_endpoint": destination, "legacy_endpoint": original, "quiescent_old_endpoint": stopped,
            "quiescent_new_endpoint": destination, "quiescent_legacy_endpoint": stopped,
            "retained_reply_proof": {"links": [], "referent": observe(referent)}}
    full._validate_quiescent_v4(item)
    altered = copy.deepcopy(item)
    altered["retained_reply_proof"] = None
    with pytest.raises(full.FullError, match="retained referent proof"):
        full._validate_quiescent_v4(altered)
    assert referent.read_bytes() == b"original" and entry.read_bytes() == b"progressed"


def test_full4_journal_progress_cannot_skip_reservation_or_rebind_a_claim() -> None:
    item = {"kind": "channel-replies", "action": "copy-if-absent", "old_endpoint": {"id": "old"},
            "new_endpoint": {"id": "new"}, "legacy_endpoint": {"id": "legacy"},
            "quiescent_old_endpoint": {"id": "old-stopped"}, "quiescent_new_endpoint": {"id": "new-stopped"},
            "quiescent_legacy_endpoint": {"id": "legacy-stopped"}, "retained_reply_proof": None,
            "compatibility": None, "forward": None, "forward_old_readback": None,
            "progressed_endpoint": None, "rollback_compatibility": None, "rollback": None, "cleanup": None}
    before = {"items": [item]}
    pending = copy.deepcopy(before)
    pending["items"][0]["compatibility"] = {"status": "pending", "raw_sha256": "a" * 64,
                                           "old_canonical_sha256": None, "new_canonical_sha256": None,
                                           "old_writeable": True, "clock": 123}
    full._advance_migrations_v4(before, pending)
    completed = copy.deepcopy(pending)
    completed["items"][0]["compatibility"].update(status="complete", old_canonical_sha256="b" * 64,
                                                  new_canonical_sha256="b" * 64)
    full._advance_migrations_v4(pending, completed)
    altered = copy.deepcopy(completed)
    altered["items"][0]["compatibility"]["clock"] += 1
    with pytest.raises(full.FullError, match="binding changed"):
        full._advance_migrations_v4(pending, altered)
    intent = copy.deepcopy(completed)
    intent["items"][0]["forward"] = {"binding": {"source": "bound"}, "phase": "intent", "stage_before": None,
                                    "reservation": None, "staged": None, "published": None, "discard": None}
    full._advance_migrations_v4(completed, intent)
    skipped = copy.deepcopy(intent)
    skipped["items"][0]["forward"]["phase"] = "staged"
    with pytest.raises(full.FullError, match="skipped or regressed"):
        full._advance_migrations_v4(intent, skipped)
    changed = copy.deepcopy(intent)
    changed["items"][0]["forward"]["binding"]["source"] = "other"
    with pytest.raises(full.FullError, match="binding changed"):
        full._advance_migrations_v4(intent, changed)


def test_full4_discard_requires_durable_absence_readback(tmp_path: Path) -> None:
    from test_component_state_copy_v4 import accepted, capture, setup_copy

    request, stage, _, _ = setup_copy(tmp_path, kind="channel-replies")
    binding, origin, target = request["binding"], request["source_endpoint"], request["destination_before"]
    absent = capture(stage, "channel-replies", request["mounts"])
    progress = {"binding": binding, "phase": "intent", "stage_before": absent,
                "reservation": None, "staged": None, "published": None, "discard": None}
    kwargs = {"journal_id": binding["transaction_id"], "state_kind": "channel-replies", "direction": "forward",
              "source": origin, "destination": target, "image": target}
    full._copy_progress_v4(progress, **kwargs)
    reserved, _ = accepted(request)
    progress.update(phase="reserved", reservation=reserved["endpoint"])
    request.update(action="write", phase="reserved", reservation=reserved["endpoint"])
    staged, _ = accepted(request)
    progress.update(phase="staged", staged=staged["endpoint"],
                    discard={"phase": "intent", "copy_phase": "staged", "stage_after": None})
    full._copy_progress_v4(progress, **kwargs)
    request.update(action="discard", phase="intent", staged=staged["endpoint"],
                   discard={"binding_sha256": request["binding_sha256"], "copy_phase": "staged"})
    removed, _ = accepted(request)
    progress["discard"].update(phase="removed", stage_after=removed["endpoint"])
    full._copy_progress_v4(progress, **kwargs)
    altered = copy.deepcopy(progress)
    altered["discard"]["stage_after"] = staged["endpoint"]
    with pytest.raises(full.FullError, match="discarded stage"):
        full._copy_progress_v4(altered, **kwargs)


def test_full4_quiescent_record_refuses_a_different_source_with_equal_bytes(tmp_path: Path) -> None:
    from test_component_state_copy_v4 import capture, setup_copy

    request, _, _, _ = setup_copy(tmp_path, kind="channel-replies")
    source, target = request["source_endpoint"], request["destination_before"]
    item = {"kind": "channel-replies", "action": "copy-if-absent", "old_endpoint": source,
            "new_endpoint": target, "legacy_endpoint": source, "quiescent_old_endpoint": source,
            "quiescent_new_endpoint": target, "quiescent_legacy_endpoint": source, "retained_reply_proof": None}
    full._validate_quiescent_v4(item)
    alternative = tmp_path / "source/other.json"
    alternative.write_bytes(Path(request["read_path"]).read_bytes())
    altered = copy.deepcopy(item)
    altered["quiescent_legacy_endpoint"] = capture(alternative, "channel-replies", request["mounts"],
                                                  require_writer=False)
    assert altered["quiescent_legacy_endpoint"]["referent"]["sha256"] == source["referent"]["sha256"]
    with pytest.raises(full.FullError, match="stopped endpoint route"):
        full._validate_quiescent_v4(altered)
    partial = copy.deepcopy(item)
    partial["quiescent_new_endpoint"] = None
    with pytest.raises(full.FullError, match="incomplete"):
        full._validate_quiescent_v4(partial)


def test_full4_applied_copy_requires_old_reader_published_readback(tmp_path: Path) -> None:
    from test_component_state_copy_v4 import accepted, capture, publish_request, setup_copy

    root = tmp_path / "copy"
    root.mkdir()
    request, stage, _, _ = setup_copy(root, kind="channel-replies")
    source, target, binding = request["source_endpoint"], request["destination_before"], request["binding"]
    old_mounts = copy.deepcopy(request["mounts"])
    old_mounts[0]["read_only"] = False
    old = capture(Path(request["read_path"]), "channel-replies", old_mounts)
    stage_before = capture(stage, "channel-replies", request["mounts"])
    reserved, _ = accepted(request)
    request.update(action="write", phase="reserved", reservation=reserved["endpoint"])
    staged, _ = accepted(request)
    publish_request(request, staged["endpoint"])
    published, _ = accepted(request)
    compatibility = {"status": "complete", "raw_sha256": source["referent"]["sha256"],
                     "old_canonical_sha256": "b" * 64, "new_canonical_sha256": "b" * 64,
                     "old_writeable": True, "clock": 123}
    item = {"kind": "channel-replies", "action": "copy-if-absent", "old_endpoint": old,
            "new_endpoint": target, "legacy_endpoint": source, "quiescent_old_endpoint": old,
            "quiescent_new_endpoint": target, "quiescent_legacy_endpoint": source, "retained_reply_proof": None,
            "compatibility": compatibility, "forward_old_readback": None,
            "forward": {"binding": binding, "phase": "published", "stage_before": stage_before,
                        "reservation": reserved["endpoint"], "staged": staged["endpoint"],
                        "published": published["endpoint"], "discard": None},
            "progressed_endpoint": None, "rollback_compatibility": None, "rollback": None, "cleanup": None}
    second_root = tmp_path / "same"
    second_root.mkdir()
    second_request, _, _, _ = setup_copy(second_root, kind="chat-settings")
    second_mounts = copy.deepcopy(second_request["mounts"])
    second_mounts[0]["read_only"] = False
    shared = capture(Path(second_request["read_path"]), "chat-settings", second_mounts)
    second = {"kind": "chat-settings", "action": "none", "old_endpoint": shared, "new_endpoint": shared,
              "legacy_endpoint": None, "quiescent_old_endpoint": shared, "quiescent_new_endpoint": shared,
              "quiescent_legacy_endpoint": None, "retained_reply_proof": None,
              "compatibility": {**compatibility, "raw_sha256": shared["referent"]["sha256"]},
              "forward": None, "forward_old_readback": None, "progressed_endpoint": None,
              "rollback_compatibility": None, "rollback": None, "cleanup": None}
    component = {"recipe": {"sha256": old["recipe_sha256"]},
                 "image": {"id": old["image_id"], "revision": old["image_revision"]}}
    kwargs = {"journal_id": binding["transaction_id"], "before": {"telegram": component},
              "target": {"telegram": component}}
    with pytest.raises(full.FullError, match="old reader"):
        full._migrations_v4({"status": "applied", "items": [item, second]}, **kwargs)
    item["forward_old_readback"] = {"source_endpoint_sha256": full._endpoint_hash(published["endpoint"]),
                                    "raw_sha256": source["referent"]["sha256"], "canonical_sha256": "b" * 64,
                                    "old_writeable": True, "clock": 123}
    full._migrations_v4({"status": "applied", "items": [item, second]}, **kwargs)
    item["forward_old_readback"]["source_endpoint_sha256"] = "0" * 64
    with pytest.raises(full.FullError, match="old native reader"):
        full._migrations_v4({"status": "applied", "items": [item, second]}, **kwargs)


def test_full4_validate_existing_compares_active_state_not_stale_legacy_file(tmp_path: Path) -> None:
    from test_component_state_copy_v4 import setup_copy

    root = tmp_path / "retained"
    root.mkdir()
    request, _, _, _ = setup_copy(root, kind="chat-settings", direction="rollback")
    active, stale = request["destination_before"], request["source_endpoint"]
    assert active["referent"]["sha256"] != stale["referent"]["sha256"]
    second_root = tmp_path / "other"
    second_root.mkdir()
    second_request, _, _, _ = setup_copy(second_root, kind="channel-replies", direction="rollback")
    shared = second_request["destination_before"]
    # An earlier relocation left the active state in place; a stale legacy file still exists beside it.
    item = {"kind": "chat-settings", "action": "validate-existing", "old_endpoint": active,
            "new_endpoint": active, "legacy_endpoint": stale, "quiescent_old_endpoint": active,
            "quiescent_new_endpoint": active, "quiescent_legacy_endpoint": stale, "retained_reply_proof": None,
            "compatibility": {"status": "pending", "raw_sha256": active["referent"]["sha256"],
                              "old_canonical_sha256": None, "new_canonical_sha256": None,
                              "old_writeable": True, "clock": 123},
            "forward": None, "forward_old_readback": None, "progressed_endpoint": None,
            "rollback_compatibility": None, "rollback": None, "cleanup": None}
    second = {**item, "kind": "channel-replies", "action": "none", "old_endpoint": shared,
              "new_endpoint": shared, "legacy_endpoint": None, "quiescent_old_endpoint": shared,
              "quiescent_new_endpoint": shared, "quiescent_legacy_endpoint": None,
              "compatibility": {**item["compatibility"], "raw_sha256": shared["referent"]["sha256"]}}
    component = {"recipe": {"sha256": active["recipe_sha256"]},
                 "image": {"id": active["image_id"], "revision": active["image_revision"]}}
    kwargs = {"journal_id": request["binding"]["transaction_id"], "before": {"telegram": component},
              "target": {"telegram": component}}

    assert full._compatibility_source_v4(item) is active
    assert full._compatibility_source_v4({**item, "action": "none"}) is active
    assert full._compatibility_source_v4({**item, "action": "copy-if-absent"}) is stale
    full._migrations_v4({"status": "planned", "items": [item, second]}, **kwargs)
    # The writer records the source selected by action; the stale file's hash must not satisfy the validator.
    wrong = copy.deepcopy(item)
    wrong["compatibility"]["raw_sha256"] = stale["referent"]["sha256"]
    with pytest.raises(full.FullError, match="source content differs"):
        full._migrations_v4({"status": "planned", "items": [wrong, second]}, **kwargs)


def test_database_classification_distinguishes_equal_content_by_recorded_inode(full_journal: dict) -> None:
    from scripts import component_full_db as full_db

    old = full_journal["database"]["old"]
    live = Path(old["binding"]["path"])
    permissions = full_db.permissions(live)
    for name in ("old", "backup", "stage", "restore"):
        full_journal["database"][name]["permissions"] = copy.deepcopy(permissions)
    candidate = full_journal["database"]["candidate"]
    candidate["binding"]["sha256"] = old["binding"]["sha256"]
    full_journal["target"]["manifest"]["database"]["sha256"] = old["binding"]["sha256"]
    full_journal["database"]["stage"]["sha256"] = old["binding"]["sha256"]
    full_journal["manifest"]["sha256"] = hashlib.sha256(
        full.manifest._encode(full_journal["target"]["manifest"])).hexdigest()
    assert full.classify_database(full_journal)[0] == "old"
    stage = Path(full_journal["database"]["stage"]["path"])
    shutil.copy2(live, stage)
    full_journal["database"]["stage"]["stat"] = full.manifest.read_database_snapshot(stage)[1]
    os.replace(stage, live)
    assert full.classify_database(full_journal)[0] == "new"
    restored = Path(full_journal["database"]["restore"]["path"])
    shutil.copy2(live, restored)
    full_journal["database"]["restore"]["stat"] = full.manifest.read_database_snapshot(restored)[1]
    os.replace(restored, live)
    assert full.classify_database(full_journal)[0] == "restored"


@pytest.mark.parametrize("expected_state,stage_name", [("new", "stage"), ("restored", "restore")])
def test_renamed_live_database_requires_directory_sync_before_acceptance(
    tmp_path: Path, full_journal: dict, monkeypatch: pytest.MonkeyPatch, expected_state: str, stage_name: str,
) -> None:
    from scripts import component_full_db as full_db

    old = full_journal["database"]["old"]
    live = Path(old["binding"]["path"])
    permissions = full_db.permissions(live)
    for name in ("old", "backup", "stage", "restore"):
        full_journal["database"][name]["permissions"] = copy.deepcopy(permissions)
    backup = tmp_path / full_journal["database"]["backup"]["path"]
    backup.parent.mkdir(parents=True)
    shutil.copy2(live, backup)
    full_journal["database"]["backup"]["stat"] = full.manifest.read_database_snapshot(backup)[1]
    if expected_state == "new":
        full_journal["database"]["candidate"]["binding"]["sha256"] = old["binding"]["sha256"]
        full_journal["target"]["manifest"]["database"]["sha256"] = old["binding"]["sha256"]
        full_journal["database"]["stage"]["sha256"] = old["binding"]["sha256"]
        full_journal["manifest"]["sha256"] = hashlib.sha256(
            full.manifest._encode(full_journal["target"]["manifest"])).hexdigest()
    stage = Path(full_journal["database"][stage_name]["path"])
    shutil.copy2(live, stage)
    full_journal["database"][stage_name]["stat"] = full.manifest.read_database_snapshot(stage)[1]
    os.replace(stage, live)
    state, observed = full.classify_database(full_journal)
    assert state == expected_state and observed is not None
    recorded_name = "promoted_stat" if expected_state == "new" else "restored_stat"
    full_journal["database"][recorded_name] = observed
    original_fsync = os.fsync
    stopped = []
    monkeypatch.setattr(full, "_all_readers_stopped", lambda *_args, **_kwargs: stopped.append(True))
    replay = full._promote_database if expected_state == "new" else full._restore_database

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic directory sync failure")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory)
        with pytest.raises(full.FullError, match="could not be synced"):
            replay(tmp_path, full_journal, compose_path=Path("/unused-compose"))
    assert stopped == [True]
    assert full.classify_database(full_journal) == (expected_state, observed)
    synced = []

    def track_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", track_directory)
        replay(tmp_path, full_journal, compose_path=Path("/unused-compose"))
    assert synced == [str(live.parent)]
    assert stopped == [True, True, True]
    assert full.classify_database(full_journal) == (expected_state, observed)


def test_disjoint_rollback_source_gets_only_its_bound_cross_mount(tmp_path: Path) -> None:
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    source = new_dir / "replies.json"
    source.write_text("{}", encoding="ascii")
    identifier = "20261106T110000Z-" + "3" * 32
    journal = {"kind": "component-full", "phase": "rollback-copyback", "transaction_id": identifier,
               "migrations": {"items": [{"old_target_path": str(old_dir / "replies.json"),
                                          "new_target_path": str(source)}]}}
    snapshot = {"mounts": [{"host": str(old_dir), "container": "/app/state", "read_only": False}]}
    mapped, mounts = state._probe_path(journal, snapshot, str(source))
    assert mapped.startswith("/run/wuwaterm-full-cross-" + identifier + "-")
    extra = next(item for item in mounts if item["host"] == str(new_dir))
    assert extra["read_only"] is True and extra["inode"] == new_dir.stat().st_ino
    with pytest.raises(state.StateError, match="read-only"):
        state._probe_path(journal, snapshot, str(source), writable=True)
    with pytest.raises(state.StateError):
        state._probe_path(journal, snapshot, str(new_dir / "other.json"))
    journal["phase"] = "state-migrate"
    with pytest.raises(state.StateError):
        state._probe_path(journal, snapshot, str(source))


@pytest.mark.parametrize("mutate", [
    lambda j: j["predecessor"].update(journal_sha256="bad"),
    lambda j: j["source"].pop("compose"),
    lambda j: j["database"]["candidate"].update(path=".deployments/other.db"),
    lambda j: j["database"]["candidate"]["verification"].update(exact_hits_exit=1),
    lambda j: j["database"]["backup"].update(path="data/deployment-backups/other.db"),
    lambda j: j["database"]["backup"]["permissions"].update(mode=0o644),
    lambda j: j["database"]["stage"].update(path="/tmp/other.db"),
    lambda j: j["target"]["containers"]["api"].update(id="f" * 64),
    lambda j: j["pointers"]["target"][".deploy_current"].update(bytes="bad\n"),
    lambda j: j.update(status="committed", phase="committed"),
    lambda j: j.update(unknown=True),
])
def test_full_journal_refuses_unbound_or_changed_fields(full_journal: dict, mutate) -> None:
    # Avoid aliasing permissions or manifest dictionaries across mutation targets.
    altered = copy.deepcopy(full_journal)
    altered["database"]["backup"]["permissions"] = copy.deepcopy(altered["database"]["backup"]["permissions"])
    mutate(altered)
    with pytest.raises(full.FullError):
        full.validate_journal(altered)


def test_runtime_to_full_handoff_requires_exact_immutable_archive(tmp_path: Path, full_journal: dict) -> None:
    fixture_root = tmp_path / "other"
    fixture_root.mkdir()
    previous = runtime_fixture.__wrapped__(fixture_root)
    current = full_journal["before"]["manifest"]
    identifier = current["deployment_id"]
    previous.update(transaction_id=identifier, action="adopt", status="committed", phase="committed", selection=[])
    previous["before"]["topology"] = copy.deepcopy(current["components"])
    previous["before"]["containers"] = copy.deepcopy(full_journal["before"]["containers"])
    previous["target"] = {"manifest": copy.deepcopy(current), "containers": copy.deepcopy(previous["before"]["containers"])}
    previous["database"] = {key: copy.deepcopy(full_journal["database"]["old"][key]) for key in ("binding", "stat")}
    previous["pointers"]["target"][".deploy_current"]["bytes"] = identifier + "\n"
    previous["manifest"] = {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                            "sha256": hashlib.sha256(full.manifest._encode(current)).hexdigest()}
    runtime.validate_journal(previous)
    raw = full.manifest._encode(previous)
    full_journal["predecessor"] = runtime.predecessor_reference("runtime", identifier, raw)
    with full.manifest._directory(tmp_path, (".deployments", "component-runtime-history"), create=True) as directory:
        full.manifest._write_file(directory, identifier + ".json", raw, reuse=False)
    full.verify_handoff(tmp_path, successor_kind="full", successor=full_journal,
                        predecessor=previous, predecessor_raw=raw)
    full.verify_predecessor(tmp_path, successor_kind="full", successor=full_journal)
    archive = tmp_path / full_journal["predecessor"]["archive_path"]
    archive.chmod(0o600)
    with pytest.raises(full.FullError, match="cannot be verified"):
        full.verify_predecessor(tmp_path, successor_kind="full", successor=full_journal)


def test_cross_kind_handoff_syncs_successor_before_removing_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / ".deployments"
    directory.mkdir()
    previous = directory / runtime.JOURNAL_NAME
    successor = directory / full.JOURNAL_NAME
    previous.write_bytes(b"exact predecessor")
    successor.write_bytes(b"exact successor")
    kwargs = {"previous_name": runtime.JOURNAL_NAME, "previous_raw": b"exact predecessor",
              "successor_name": full.JOURNAL_NAME, "successor_raw": b"exact successor"}
    original = os.fsync

    def fail_before_unlink(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic directory sync failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_before_unlink)
        with pytest.raises(full.manifest.ManifestError, match="synthetic directory sync failure") as error:
            full._sync_handoff_markers(tmp_path, **kwargs)
        assert isinstance(error.value.__cause__, OSError) and error.value.__cause__.errno == errno.EIO
    assert previous.read_bytes() == b"exact predecessor" and successor.read_bytes() == b"exact successor"
    calls = []

    def fail_after_unlink(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append(os.readlink(f"/proc/self/fd/{fd}"))
            if len(calls) == 2:
                raise OSError(errno.EIO, "synthetic post-unlink sync failure")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_after_unlink)
        with pytest.raises(full.manifest.ManifestError, match="synthetic post-unlink sync failure") as error:
            full._sync_handoff_markers(tmp_path, **kwargs)
        assert isinstance(error.value.__cause__, OSError) and error.value.__cause__.errno == errno.EIO
    assert calls == [str(directory), str(directory)] and not previous.exists()
    assert successor.read_bytes() == b"exact successor"
    full._sync_handoff_markers(tmp_path, **{**kwargs, "previous_raw": None})
    assert successor.read_bytes() == b"exact successor" and not previous.exists()


@pytest.mark.parametrize("position", ["before-link", "after-link"])
def test_full_successor_publication_has_one_link_after_process_death(tmp_path: Path, position: str) -> None:
    directory = tmp_path / ".deployments"
    directory.mkdir()
    previous = directory / runtime.JOURNAL_NAME
    previous.write_bytes(b"exact predecessor")
    code = r'''
import os,signal,sys
sys.path.insert(0,sys.argv[1]); import component_full as full
directory=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC); original=os.link
def paused(*args,**kwargs):
 if sys.argv[3]=='before-link': os.kill(os.getpid(),signal.SIGSTOP)
 original(*args,**kwargs)
 if sys.argv[3]=='after-link': os.kill(os.getpid(),signal.SIGSTOP)
os.link=paused
full._publish_initial_full_journal(directory,b'exact successor')
'''
    command = [sys.executable, "-I", "-B", "-c", code, str(Path(full.__file__).resolve().parent),
               str(directory), position]
    child = subprocess.Popen(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                assert os.WIFSTOPPED(status), "successor publisher exited before its link boundary"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("successor publisher did not reach its link boundary")
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)
    target = directory / full.JOURNAL_NAME
    assert previous.read_bytes() == b"exact predecessor"
    assert {entry.name for entry in directory.iterdir()} == (
        {runtime.JOURNAL_NAME, full.JOURNAL_NAME} if position == "after-link" else {runtime.JOURNAL_NAME})
    if position == "after-link":
        assert target.read_bytes() == b"exact successor" and target.stat().st_nlink == 1
        assert target.stat().st_mode & 0o777 == 0o600
        full._sync_handoff_markers(tmp_path, previous_name=runtime.JOURNAL_NAME,
                                   previous_raw=b"exact predecessor", successor_name=full.JOURNAL_NAME,
                                   successor_raw=b"exact successor")
        assert target.read_bytes() == b"exact successor" and not previous.exists()


def test_full_to_runtime_handoff_checks_successor_preimage(tmp_path: Path, full_journal: dict) -> None:
    previous = copy.deepcopy(full_journal)
    previous.update(status="committed", phase="committed")
    stat = previous["database"]["old"]["stat"]
    previous["database"]["backup"]["stat"] = stat
    previous["database"]["stage"]["stat"] = stat
    previous["database"]["promoted_stat"] = stat
    absent = {"present": False, "sha256": None, "stat": None}
    compatible = {"raw_sha256": None, "old_canonical_sha256": None, "new_canonical_sha256": None,
                  "old_writeable": True, "clock": 1}
    previous["migrations"] = {"status": "applied", "items": [
        {"kind": kind, "action": "none", "old_effective_path": "/app/state/" + kind,
         "new_effective_path": "/app/state/" + kind, "old_target_path": "/srv/state/" + kind,
         "new_target_path": "/srv/state/" + kind, "old_legacy_path": None, "new_legacy_path": None,
         "old_preimage": absent, "new_preimage": absent, "forward_stage": None, "forward_stage_preimage": None,
         "created": None, "compatibility": compatible,
         "rollback_compatibility": None, "copyback": None}
        for kind in ("chat-settings", "channel-replies")
    ]}
    fixture_root = tmp_path / "other"
    fixture_root.mkdir()
    successor = runtime_fixture.__wrapped__(fixture_root)
    identifier = "20261106T120000Z-" + "3" * 32
    successor.update(schema_version=4, transaction_id=identifier, source=copy.deepcopy(previous["source"]))
    current = copy.deepcopy(previous["target"]["manifest"])
    target = copy.deepcopy(current)
    target["deployment_id"] = identifier
    successor["before"] = {"manifest": current, "topology": copy.deepcopy(current["components"]),
                           "containers": copy.deepcopy(previous["target"]["containers"])}
    records = copy.deepcopy(successor["before"]["containers"])
    records["api"].update(id=None, name=None, started_at=None, restart_count=None, transaction_label=identifier)
    successor["target"] = {"manifest": target, "containers": records}
    successor["database"]["binding"] = copy.deepcopy(current["database"])
    successor["pointers"]["old"] = copy.deepcopy(previous["pointers"]["target"])
    successor["pointers"]["target"][".deploy_current"]["bytes"] = identifier + "\n"
    successor["manifest"] = {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                             "sha256": hashlib.sha256(full.manifest._encode(target)).hexdigest()}
    raw = full.manifest._encode(previous)
    successor["predecessor"] = runtime.predecessor_reference("full", previous["transaction_id"], raw)
    with full.manifest._directory(tmp_path, (".deployments", "component-full-history"), create=True) as directory:
        full.manifest._write_file(directory, previous["transaction_id"] + ".json", raw, reuse=False)
    full.verify_handoff(tmp_path, successor_kind="runtime", successor=successor,
                        predecessor=previous, predecessor_raw=raw)
    full.verify_predecessor(tmp_path, successor_kind="runtime", successor=successor)
    successor["before"]["containers"]["telegram"]["id"] = "f" * 64
    successor["target"]["containers"]["telegram"]["id"] = "f" * 64
    with pytest.raises(full.FullError, match="container binding differs"):
        full.verify_handoff(tmp_path, successor_kind="runtime", successor=successor,
                            predecessor=previous, predecessor_raw=raw)


def _null_adoption_pair(tmp_path: Path) -> tuple[dict, dict]:
    """Build two complete runtime records around an unadopted winner."""
    previous = copy.deepcopy(runtime_fixture.__wrapped__(tmp_path))
    previous.update(schema_version=4, action="adopt", selection=[], predecessor=None,
                    status="rolled_back", phase="rolled-back")
    digest = "d" * 64
    previous["source"]["compose"] = {"path": f".deployments/component-compose-{digest}", "sha256": digest}
    previous["target"]["containers"] = copy.deepcopy(previous["before"]["containers"])
    _retarget_adoption(previous, "20261106T100000Z-" + "1" * 32)
    runtime.validate_journal(previous)
    successor = copy.deepcopy(previous)
    successor.update(status="active", phase="prepared")
    _retarget_adoption(successor, "20261106T110000Z-" + "2" * 32)
    successor["predecessor"] = runtime.predecessor_reference(
        "runtime", previous["transaction_id"], full.manifest._encode(previous))
    runtime.validate_journal(successor)
    return previous, successor


def _retarget_adoption(record: dict, identifier: str) -> None:
    record["transaction_id"] = identifier
    record["target"]["manifest"]["deployment_id"] = identifier
    record["pointers"]["target"][".deploy_current"]["bytes"] = identifier + "\n"
    record["manifest"] = {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                          "sha256": hashlib.sha256(full.manifest._encode(record["target"]["manifest"])).hexdigest()}


def _archive_runtime(root: Path, previous: dict) -> bytes:
    raw = full.manifest._encode(previous)
    with full.manifest._directory(root, (".deployments", "component-runtime-history"), create=True) as directory:
        full.manifest._write_file(directory, previous["transaction_id"] + ".json", raw, reuse=False)
    return raw


def _different_adoption_source(record: dict) -> None:
    source = record["source"]
    source.update(commit="c" * 40, tree="d" * 40, tool_bytes_sha256="e" * 64)
    source_id = runtime.source_tool_identity(source["commit"], source["tree"], source["tool_bytes_sha256"])
    source["source_tool_sha256"] = source_id
    source["capsule"]["path"] = ".deployments/component-source-" + source_id
    source["bootstrap"]["path"] = ".deployments/component-bootstrap-" + source_id
    source["compose"] = {"path": ".deployments/component-compose-" + "f" * 64, "sha256": "f" * 64}
    record["target"]["manifest"]["operations"].update(source_commit=source["commit"], source_tree=source["tree"],
                                                     source_tool_sha256=source_id)
    record["pointers"]["target"][".deploy_commit"]["bytes"] = source["commit"] + "\n"
    _retarget_adoption(record, record["transaction_id"])


@pytest.mark.parametrize("status,phase", [("active", "prepared"), ("unresolved", "prepared"),
                                         ("rolled_back", "rolled-back"), ("committed", "committed")])
def test_rolled_back_unadopted_handoff_accepts_lifecycle_and_fresh_observations(
    tmp_path: Path, status: str, phase: str,
) -> None:
    previous, successor = _null_adoption_pair(tmp_path)
    raw = _archive_runtime(tmp_path, previous)
    successor.update(status=status, phase=phase)
    _different_adoption_source(successor)
    for location in (successor["before"]["containers"], successor["target"]["containers"]):
        location["api"].update(running=False, started_at="2026-11-06T12:00:00.123456789Z", restart_count=7)
    runtime.validate_journal(successor)
    assert successor["source"] != previous["source"]
    full.verify_handoff(tmp_path, successor_kind="runtime", successor=successor,
                        predecessor=previous, predecessor_raw=raw)
    full.verify_predecessor(tmp_path, successor_kind="runtime", successor=successor)


@pytest.mark.parametrize("change", ["topology", "provenance", "recipe-reference", "database", "database-stat",
                                    "api-identity", "old-pointer", "component-absent", "container-id",
                                    "container-image", "container-project", "container-service", "container-name",
                                    "container-label"])
def test_rolled_back_unadopted_handoff_refuses_changed_preimage(tmp_path: Path, change: str) -> None:
    previous, successor = _null_adoption_pair(tmp_path)
    raw = _archive_runtime(tmp_path, previous)
    topology = successor["before"]["topology"]
    if change == "topology":
        topology["api"]["version"] = "0.7.1"
        topology["api"]["core"]["version"] = "0.7.1"
    elif change == "provenance":
        topology["api"]["provenance"]["build_receipt_sha256"] = "f" * 64
    elif change == "recipe-reference":
        topology["api"]["recipe"].update(sha256="f" * 64, snapshot_path=".deployments/product-recipes/" + "f" * 64 + ".json")
    elif change == "database":
        successor["database"]["binding"]["sha256"] = "f" * 64
        successor["target"]["manifest"]["database"] = copy.deepcopy(successor["database"]["binding"])
    elif change == "database-stat":
        successor["database"]["stat"]["inode"] += 1
    elif change == "api-identity":
        successor["api_identity"]["inode"] += 1
        successor["target"]["manifest"]["api_identity"] = copy.deepcopy(successor["api_identity"])
    elif change == "old-pointer":
        successor["pointers"]["old"][".deploy_commit"] = {"present": False, "bytes": None}
    elif change == "component-absent":
        topology["api"] = None
        successor["api_identity"] = None
        successor["target"]["manifest"]["api_identity"] = None
        successor["before"]["containers"]["api"] = None
        successor["target"]["containers"]["api"] = None
    elif change in ("container-image", "container-project", "container-service"):
        if change == "container-image":
            key, value = "image_id", "sha256:" + "f" * 64
            topology["api"]["image"].update(id=value, digest=value)
        else:
            key = change.removeprefix("container-")
            value = "different-" + key
            topology["api"]["recipe"][key] = value
        for location in (successor["before"]["containers"], successor["target"]["containers"]):
            location["api"][key] = value
    else:
        key, value = {"container-id": ("id", "f" * 64), "container-name": ("name", "other-api"),
                      "container-label": ("transaction_label", "20261106T090000Z-" + "3" * 32)}[change]
        for location in (successor["before"]["containers"], successor["target"]["containers"]):
            location["api"][key] = value
    successor["target"]["manifest"]["components"] = copy.deepcopy(topology)
    _retarget_adoption(successor, successor["transaction_id"])
    runtime.validate_journal(successor)
    with pytest.raises(full.FullError, match="preimage differs|container binding differs"):
        full.verify_handoff(tmp_path, successor_kind="runtime", successor=successor,
                            predecessor=previous, predecessor_raw=raw)
    with pytest.raises(full.FullError, match="preimage differs|container binding differs"):
        full.verify_predecessor(tmp_path, successor_kind="runtime", successor=successor)


@pytest.mark.parametrize("change", ["active", "schema2", "schema3", "update"])
def test_null_handoff_refuses_other_predecessor_records(tmp_path: Path, change: str) -> None:
    previous, successor = _null_adoption_pair(tmp_path)
    if change == "active":
        previous.update(status="active", phase="prepared")
    elif change.startswith("schema"):
        previous["schema_version"] = int(change[-1])
        previous.pop("predecessor")
        if change == "schema2":
            previous["source"].pop("compose")
    else:
        previous.update(action="update", selection=["api"])
        previous["target"]["containers"]["api"].update(id=None, name=None, started_at=None,
                                                         restart_count=None, transaction_label=previous["transaction_id"])
        previous["schema_version"] = 3
        previous.pop("predecessor")
    runtime.validate_journal(previous)
    raw = _archive_runtime(tmp_path, previous)
    successor["predecessor"] = runtime.predecessor_reference("runtime", previous["transaction_id"], raw)
    with pytest.raises(full.FullError):
        full.verify_handoff(tmp_path, successor_kind="runtime", successor=successor,
                            predecessor=previous, predecessor_raw=raw)


@pytest.mark.parametrize("damage", ["missing", "changed", "writable"])
def test_null_handoff_requires_exact_immutable_archive(tmp_path: Path, damage: str) -> None:
    previous, successor = _null_adoption_pair(tmp_path)
    if damage != "missing":
        _archive_runtime(tmp_path, previous)
        archive = tmp_path / successor["predecessor"]["archive_path"]
        archive.chmod(0o600)
        if damage == "changed":
            archive.write_bytes(full.manifest._encode(previous) + b" ")
            archive.chmod(0o400)
    with pytest.raises(full.FullError):
        full.verify_predecessor(tmp_path, successor_kind="runtime", successor=successor)


def test_null_handoff_repeated_failures_and_committed_retry_to_update(tmp_path: Path) -> None:
    first, second = _null_adoption_pair(tmp_path)
    _archive_runtime(tmp_path, first)
    second.update(status="rolled_back", phase="rolled-back")
    second_raw = _archive_runtime(tmp_path, second)
    full.verify_predecessor(tmp_path, successor_kind="runtime", successor=second)
    third = copy.deepcopy(second)
    third.update(status="active", phase="prepared")
    _retarget_adoption(third, "20261106T120000Z-" + "3" * 32)
    third["predecessor"] = runtime.predecessor_reference("runtime", second["transaction_id"], second_raw)
    full.verify_predecessor(tmp_path, successor_kind="runtime", successor=third)
    third.update(status="committed", phase="committed")
    third_raw = _archive_runtime(tmp_path, third)
    update = copy.deepcopy(third)
    update.update(status="active", phase="prepared", action="update", selection=["api"])
    _retarget_adoption(update, "20261106T130000Z-" + "4" * 32)
    update["before"] = {"manifest": copy.deepcopy(third["target"]["manifest"]),
                        "topology": copy.deepcopy(third["target"]["manifest"]["components"]),
                        "containers": copy.deepcopy(third["target"]["containers"])}
    update["target"]["containers"]["api"].update(id=None, name=None, started_at=None, restart_count=None,
                                                  transaction_label=update["transaction_id"])
    update["pointers"]["old"] = copy.deepcopy(third["pointers"]["target"])
    update["predecessor"] = runtime.predecessor_reference("runtime", third["transaction_id"], third_raw)
    runtime.validate_journal(update)
    full.verify_predecessor(tmp_path, successor_kind="runtime", successor=update)


def test_null_handoff_refuses_runtime_update_and_full_successors(tmp_path: Path, full_journal: dict) -> None:
    fixture_root = tmp_path / "adoption"
    fixture_root.mkdir()
    previous, successor = _null_adoption_pair(fixture_root)
    raw = _archive_runtime(tmp_path, previous)
    update = copy.deepcopy(successor)
    update.update(action="update", selection=["api"])
    update["before"] = {"manifest": copy.deepcopy(previous["target"]["manifest"]),
                        "topology": copy.deepcopy(previous["target"]["manifest"]["components"]),
                        "containers": copy.deepcopy(previous["target"]["containers"])}
    update["target"]["containers"]["api"].update(id=None, name=None, started_at=None, restart_count=None,
                                                  transaction_label=update["transaction_id"])
    update["pointers"]["old"] = copy.deepcopy(previous["pointers"]["target"])
    runtime.validate_journal(update)
    with pytest.raises(full.FullError):
        full.verify_handoff(tmp_path, successor_kind="runtime", successor=update,
                            predecessor=previous, predecessor_raw=raw)
    full_journal["predecessor"] = runtime.predecessor_reference("runtime", previous["transaction_id"], raw)
    full.validate_journal(full_journal)
    with pytest.raises(full.FullError):
        full.verify_handoff(tmp_path, successor_kind="full", successor=full_journal,
                            predecessor=previous, predecessor_raw=raw)
