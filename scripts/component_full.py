#!/usr/bin/env python3
"""Full component/data transactions and their exact-source recovery records."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_recovery as recovery  # noqa: E402
import component_runtime as runtime  # noqa: E402
import product_manifest as manifest  # noqa: E402


JOURNAL_NAME = ".component-full-transaction.json"
SCHEMA_VERSION = 5
FULL4_SCHEMA_VERSION = 4
PREVIOUS_SCHEMA_VERSION = 3
SCHEMA_VERSIONS = (PREVIOUS_SCHEMA_VERSION, FULL4_SCHEMA_VERSION, SCHEMA_VERSION)
PHASES = frozenset({
    "prepared", "state-plan", "backup", "retain-rollback", "stop-readers",
    "state-snapshot", "state-migrate", "stage-db", "promote-db", "activate-readers",
    "actual-health-bindings", "immutable-manifest", "deploy-commit", "deploy-current",
    "committed", "rollback-untouched", "rollback-stop-readers", "rollback-copyback", "rollback-restore-db",
    "rollback-containers", "rollback-pointers", "rolled-back", "unresolved",
})
_FORWARD = (
    "prepared", "state-plan", "backup", "retain-rollback", "stop-readers", "state-snapshot",
    "state-migrate", "stage-db", "promote-db", "activate-readers", "actual-health-bindings",
    "immutable-manifest", "deploy-commit", "deploy-current", "committed",
)
_ROLLBACK = ("rollback-stop-readers", "rollback-copyback", "rollback-restore-db",
             "rollback-containers", "rollback-pointers", "rolled-back")
_BACKUP = re.compile(r"data/deployment-backups/terms\.([0-9]{8}T[0-9]{6}Z)-([0-9]+)\.([0-9a-f]{64})\.db\Z")
_VERIFY = {
    "profile": "arikatsu", "exact_hit_sample_size": 500,
    "require_free_text_exclusions": True, "require_free_text_zh_exclusions": True,
    "verify_db_exit": 0, "exact_hits_exit": 0, "seed_terms_exit": 0,
}


class FullError(ValueError):
    """The FULL3 record or observed deployment cannot be safely used."""


def _fail(message: str) -> None:
    raise FullError(message)


def _optional_stat(value: Any, where: str) -> None:
    if value is not None:
        runtime._stat(value, where)


def _optional_preimage(value: Any, where: str) -> None:
    if value is not None:
        runtime._file_preimage(value, where)


def _permissions(value: Any, where: str) -> dict[str, int]:
    result = runtime._object(value, {"mode", "uid", "gid"}, where)
    for name in ("mode", "uid", "gid"):
        runtime._integer(result[name], f"{where}.{name}")
    if result["mode"] > 0o7777 or result["uid"] > 2**32 - 2 or result["gid"] > 2**32 - 2:
        _fail(f"{where}: invalid ownership or mode")
    return result


def _migrations(value: Any, *, telegram_present: bool, transaction_id: str) -> None:
    record = runtime._object(value, {"status", "items"}, "full.migrations")
    status = record["status"]
    if status not in ("not-applicable", "pending", "planned", "applied", "copyback", "restored"):
        _fail("full migrations: invalid status")
    items = record["items"]
    if type(items) is not list or len(items) > 2:
        _fail("full migrations: invalid items")
    if not telegram_present:
        if status != "not-applicable" or items:
            _fail("full migrations: Telegram is absent")
        return
    if status == "not-applicable" or (status == "pending" and items):
        _fail("full migrations: inconsistent pending state")
    if status != "pending" and len(items) != 2:
        _fail("full migrations: both state kinds are required")
    seen: set[str] = set()
    for item in items:
        runtime._object(item, {
            "kind", "action", "old_effective_path", "new_effective_path", "old_target_path",
            "new_target_path", "old_legacy_path", "new_legacy_path", "old_preimage", "new_preimage",
            "forward_stage", "forward_stage_preimage", "created", "compatibility", "rollback_compatibility", "copyback",
        }, "full migration")
        kind = item["kind"]
        if kind not in ("chat-settings", "channel-replies") or kind in seen:
            _fail("full migration: invalid or duplicate kind")
        seen.add(kind)
        if item["action"] not in ("none", "validate-existing", "copy-if-absent"):
            _fail("full migration: invalid action")
        for key in ("old_effective_path", "new_effective_path", "old_target_path", "new_target_path"):
            runtime._absolute(item[key], f"full migration.{key}")
        for key in ("old_legacy_path", "new_legacy_path"):
            if item[key] is not None:
                runtime._absolute(item[key], f"full migration.{key}")
        _optional_preimage(item["old_preimage"], "full migration.old_preimage")
        _optional_preimage(item["new_preimage"], "full migration.new_preimage")
        if item["forward_stage"] is not None:
            runtime._absolute(item["forward_stage"], "full migration.forward_stage")
            expected = str(Path(item["new_target_path"]).parent /
                           f".component-full-{transaction_id}-{kind}.forward")
            if item["action"] != "copy-if-absent" or item["forward_stage"] != expected:
                _fail("full migration: unexpected forward staging path")
        elif item["action"] == "copy-if-absent":
            _fail("full migration: missing forward staging path")
        _optional_preimage(item["forward_stage_preimage"], "full migration.forward_stage_preimage")
        if item["forward_stage_preimage"] is not None:
            if (item["old_preimage"] is None or not item["forward_stage_preimage"]["present"]
                    or item["forward_stage_preimage"]["sha256"] != item["old_preimage"]["sha256"]):
                _fail("full migration: forward stage differs from its source")
        if item["created"] is not None:
            created = runtime._object(item["created"], {"sha256", "stat"}, "full migration.created")
            runtime._match(created["sha256"], runtime._HEX64, "full migration.created.sha256")
            runtime._stat(created["stat"], "full migration.created.stat")
            if item["action"] != "copy-if-absent":
                _fail("full migration: creation is not authorized")
            if item["old_preimage"] is None or created["sha256"] != item["old_preimage"]["sha256"]:
                _fail("full migration: created state differs from its source")
        for compatibility in (item["compatibility"], item["rollback_compatibility"]):
            if compatibility is None:
                continue
            runtime._object(compatibility, {"raw_sha256", "old_canonical_sha256", "new_canonical_sha256",
                                            "old_writeable", "clock"},
                            "full migration.compatibility")
            hashes = (compatibility["raw_sha256"], compatibility["old_canonical_sha256"],
                      compatibility["new_canonical_sha256"])
            if any(value is None for value in hashes) and any(value is not None for value in hashes):
                _fail("full migration: canonical evidence is incomplete")
            if hashes[0] is not None:
                for key in ("raw_sha256", "old_canonical_sha256", "new_canonical_sha256"):
                    runtime._match(compatibility[key], runtime._HEX64, f"full migration.compatibility.{key}")
            if compatibility["old_canonical_sha256"] != compatibility["new_canonical_sha256"]:
                _fail("full migration: canonical reader semantics differ")
            if runtime._boolean(compatibility["old_writeable"], "full migration.compatibility.old_writeable") is not True:
                _fail("full migration: old writer cannot update its destination")
            runtime._integer(compatibility["clock"], "full migration.compatibility.clock")
        copyback = item["copyback"]
        if copyback is not None:
            runtime._object(copyback, {"phase", "source", "destination", "published", "stage", "stage_preimage"},
                            "full migration.copyback")
            if copyback["phase"] not in ("intent", "published", "cleaned"):
                _fail("full migration: invalid copy-back phase")
            runtime._file_preimage(copyback["source"], "full migration.copyback.source")
            runtime._file_preimage(copyback["destination"], "full migration.copyback.destination")
            _optional_preimage(copyback["published"], "full migration.copyback.published")
            _optional_preimage(copyback["stage_preimage"], "full migration.copyback.stage_preimage")
            runtime._absolute(copyback["stage"], "full migration.copyback.stage")
            expected = str(Path(item["old_target_path"]).parent /
                           f".component-full-{transaction_id}-{kind}.rollback")
            if copyback["stage"] != expected:
                _fail("full migration: copy-back staging path differs")
            if copyback["phase"] in ("published", "cleaned") and copyback["published"] is None:
                _fail("full migration: published copy-back has no readback")
            if (not copyback["source"]["present"] or item["old_preimage"] != copyback["destination"]
                    or copyback["published"] is not None and (
                        not copyback["published"]["present"]
                        or copyback["published"]["sha256"] != copyback["source"]["sha256"])
                    or copyback["stage_preimage"] is not None and (
                        not copyback["stage_preimage"]["present"]
                        or copyback["stage_preimage"]["sha256"] != copyback["source"]["sha256"])):
                _fail("full migration: copy-back preimages differ")
            if item["rollback_compatibility"] is None or item["rollback_compatibility"]["raw_sha256"] != copyback["source"]["sha256"]:
                _fail("full migration: copy-back lacks native compatibility evidence")
        if (item["compatibility"] is not None and item["old_preimage"] is not None
                and item["compatibility"]["raw_sha256"] != item["old_preimage"]["sha256"]):
            _fail("full migration: initial compatibility differs from its state preimage")


_V5_ITEM_KEYS = frozenset({
    "kind", "action", "old_effective_path", "new_effective_path", "old_target_path", "new_target_path",
    "old_legacy_path", "new_legacy_path", "old_preimage", "new_preimage", "legacy_preimage",
    "compatibility", "rollback_compatibility",
})


def _compatibility_v5(value: Any, where: str) -> dict[str, Any]:
    """Validate one native-reader comparison recorded by the state helpers."""
    evidence = runtime._object(value, {"raw_sha256", "old_canonical_sha256", "new_canonical_sha256",
                                       "old_writeable", "clock"}, where)
    raw = evidence["raw_sha256"]
    if raw is None:
        if evidence["old_canonical_sha256"] is not None or evidence["new_canonical_sha256"] is not None:
            _fail(f"{where}: absent state has canonical content")
    else:
        runtime._match(raw, runtime._HEX64, where)
        for name in ("old_canonical_sha256", "new_canonical_sha256"):
            if evidence[name] is None:
                raise FullError(f"{where}: absent canonical hash for present state")
            runtime._match(evidence[name], runtime._HEX64, f"{where}.{name}")
        if evidence["old_canonical_sha256"] != evidence["new_canonical_sha256"]:
            _fail(f"{where}: native reader semantics differ")
    if runtime._boolean(evidence["old_writeable"], where) is not True:
        _fail(f"{where}: old writer is not writable")
    runtime._integer(evidence["clock"], where)
    return evidence


def _v5_rollback_evidence(item: dict[str, Any]) -> bool:
    """A rollback needs native-reader evidence unless nothing was progressed.

    A native-pending copy that never happened leaves an absent target, so there
    are no bytes for the old reader to accept and nothing to keep.
    """
    if item["rollback_compatibility"] is not None:
        return True
    return (item["action"] == "copy-if-absent" and item["new_preimage"] is not None
            and item["new_preimage"]["present"] is False)


def _migrations_v5(value: Any, *, before: dict[str, Any], target: dict[str, Any]) -> None:
    """Validate the schema-5 preserved-state record: preimages, never copy claims.

    The new product performs its own startup copy, so a record may name a
    pending copy but may not carry forward staging, created targets, copy-backs
    or claim any write. Both plans must name one effective location; changing it
    is relocation and is refused before this record can exist.
    """
    record = runtime._object(value, {"status", "items"}, "full.migrations")
    status = record["status"]
    if status not in ("not-applicable", "pending", "planned", "applied", "restored"):
        _fail("full migrations: invalid status")
    items = record["items"]
    if type(items) is not list or len(items) > 2:
        _fail("full migrations: invalid items")
    old_component, new_component = before["telegram"], target["telegram"]
    if old_component is not None and new_component is None:
        _fail("full migrations: Telegram target disappeared")
    if old_component is None:
        if status != "not-applicable" or items:
            _fail("full migrations: Telegram is absent")
        return
    if status == "not-applicable" or status == "pending" and items or status != "pending" and len(items) != 2:
        _fail("full migrations: inconsistent state")
    if not items:
        return
    seen: set[str] = set()
    for item in items:
        runtime._object(item, set(_V5_ITEM_KEYS), "full migration")
        kind = item["kind"]
        if kind not in ("chat-settings", "channel-replies") or kind in seen:
            _fail("full migration: invalid or duplicate kind")
        seen.add(kind)
        if item["action"] not in ("none", "validate-existing", "copy-if-absent"):
            _fail("full migration: invalid action")
        for key in ("old_effective_path", "new_effective_path", "old_target_path", "new_target_path"):
            runtime._absolute(item[key], f"full migration.{key}")
        for key in ("old_legacy_path", "new_legacy_path"):
            if item[key] is not None:
                runtime._absolute(item[key], f"full migration.{key}")
        if (item["old_effective_path"] != item["new_effective_path"]
                or item["old_target_path"] != item["new_target_path"]):
            _fail("full migration: schema-5 state relocation is unsupported")
        _optional_preimage(item["old_preimage"], "full migration.old_preimage")
        _optional_preimage(item["new_preimage"], "full migration.new_preimage")
        _optional_preimage(item["legacy_preimage"], "full migration.legacy_preimage")
        for name in ("compatibility", "rollback_compatibility"):
            if item[name] is not None:
                _compatibility_v5(item[name], f"full migration.{name}")
        if (item["compatibility"] is not None and item["old_preimage"] is not None
                and item["compatibility"]["raw_sha256"] != item["old_preimage"]["sha256"]):
            _fail("full migration: initial compatibility differs from its state preimage")
        if item["rollback_compatibility"] is not None and item["rollback_compatibility"]["raw_sha256"] is None:
            _fail("full migration: rollback evidence has no progressed bytes")
        if status == "applied":
            for key in ("old_preimage", "new_preimage", "compatibility"):
                if item[key] is None:
                    _fail("full migration: applied state evidence is incomplete")
            if item["rollback_compatibility"] is not None:
                _fail("full migration: applied state already claims a rollback comparison")
            if item["action"] == "copy-if-absent":
                if (item["new_preimage"]["present"] or item["legacy_preimage"] is None
                        or not item["legacy_preimage"]["present"]):
                    _fail("full migration: pending native copy has no absent target or present legacy file")
                if item["new_legacy_path"] is None:
                    _fail("full migration: pending native copy has no legacy path")
        if status == "restored" and not _v5_rollback_evidence(item):
            _fail("full migration: reconciled rollback has no native evidence")


def _endpoint_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(manifest._encode(value)).hexdigest()


def _same_writer_view(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare the native writer location, including a reply entry's link chain."""
    old, new = left["writer"], right["writer"]
    if any(old[key] != new[key] for key in ("role", "host_path", "parent_identity")):
        return False
    left_ref, right_ref = left["referent"], right["referent"]
    if any(left_ref[key] != right_ref[key] for key in ("host_path", "present", "stat", "sha256")):
        return False
    if old["role"] == "referent":
        return True
    entry_keys = ("host_path", "parent_identity", "type", "lstat", "link_bytes_b64")
    if any(left["entry"][key] != right["entry"][key] for key in entry_keys):
        return False
    link_keys = ("host_path", "parent_identity", "lstat", "link_bytes_b64")
    return ([{key: link[key] for key in link_keys} for link in left["links"]] ==
            [{key: link[key] for key in link_keys} for link in right["links"]])


def _same_endpoint_route(before: dict[str, Any], after: dict[str, Any], *,
                         allow_reply_replacement: bool = False) -> bool:
    """Allow normal file progress while keeping the approved path and links."""
    for key in ("kind", "recipe_sha256", "image_id", "image_revision", "user", "working_dir",
                "configured_path", "effective_path"):
        if before[key] != after[key]:
            return False
    for key in ("container_path", "host_path", "mount_identity", "parent_identity"):
        if before["entry"][key] != after["entry"][key] or before["writer"][key] != after["writer"][key]:
            return False
    if (before["writer"]["role"] != after["writer"]["role"]
            or before["writer"]["can_replace"] and not after["writer"]["can_replace"]):
        return False
    if before["entry"]["type"] == "symlink":
        if (allow_reply_replacement and before["kind"] == "channel-replies"
                and after["entry"]["type"] == "regular" and not after["links"]):
            return True
        return before["entry"] == after["entry"] and before["links"] == after["links"]
    return (not before["links"] and not after["links"]
            and after["entry"]["type"] in ("absent", "regular")
            and not (before["entry"]["type"] == "regular" and after["entry"]["type"] == "absent"))


def _cleanup_removed(before: dict[str, Any], post: dict[str, Any], removed: dict[str, Any]) -> bool:
    stable = ("schema_version", "kind", "recipe_sha256", "image_id", "image_revision", "user", "working_dir",
              "configured_path", "effective_path")
    return (all(removed[key] == post[key] for key in stable) and removed["entry"] == before["entry"]
            and removed["referent"] == before["referent"] and removed["links"] == []
            and removed["writer"] == post["writer"])


def _retained_reply_proof(original: dict[str, Any], proof: Any) -> None:
    import component_state_endpoint as endpoint  # noqa: PLC0415

    evidence = runtime._object(proof, {"links", "referent"}, "retained reply proof")
    if (original["kind"] != "channel-replies" or original["entry"]["type"] != "symlink"
            or not original["referent"]["present"] or type(evidence["links"]) is not list
            or len(evidence["links"]) != len(original["links"]) - 1):
        _fail("retained reply proof does not match its original chain")
    for saved, observed in zip(original["links"][1:], evidence["links"], strict=True):
        endpoint.validate_endpoint(observed)
        if (observed["kind"] != original["kind"] or observed["recipe_sha256"] != original["recipe_sha256"]
                or observed["image_id"] != original["image_id"] or observed["image_revision"] != original["image_revision"]
                or observed["user"] != original["user"] or observed["working_dir"] != original["working_dir"]
                or observed["entry"] != {**saved, "type": "symlink"}
                or observed["referent"] != original["referent"]):
            _fail("retained reply link or referent changed")
    referent = evidence["referent"]
    endpoint.validate_endpoint(referent)
    if (referent["kind"] != original["kind"] or referent["recipe_sha256"] != original["recipe_sha256"]
            or referent["image_id"] != original["image_id"] or referent["image_revision"] != original["image_revision"]
            or referent["user"] != original["user"] or referent["working_dir"] != original["working_dir"]
            or referent["effective_path"] != original["referent"]["container_path"] or referent["links"]
            or referent["referent"] != original["referent"]):
        _fail("retained reply referent changed")


def _validate_quiescent_v4(item: dict[str, Any]) -> None:
    """Bind stopped endpoints to planned routes and the native writer source."""
    old, new, legacy = item["old_endpoint"], item["new_endpoint"], item["legacy_endpoint"]
    stopped_old, stopped_new, stopped_legacy = (item["quiescent_old_endpoint"], item["quiescent_new_endpoint"],
                                               item["quiescent_legacy_endpoint"])
    if stopped_old is None and stopped_new is None and stopped_legacy is None:
        if item["retained_reply_proof"] is not None:
            _fail("full migration: retained proof has no stopped endpoint")
        return
    if (stopped_old is None or stopped_new is None or (legacy is None) != (stopped_legacy is None)):
        _fail("full migration: stopped endpoint set is incomplete")
    replaced_reply = (item["kind"] == "channel-replies" and old["entry"]["type"] == "symlink"
                      and stopped_old["entry"]["type"] == "regular")
    if replaced_reply != (item["retained_reply_proof"] is not None):
        _fail("full migration: reply entry transition lacks retained referent proof")
    if replaced_reply:
        _retained_reply_proof(old, item["retained_reply_proof"])
    if (not _same_endpoint_route(old, stopped_old, allow_reply_replacement=replaced_reply)
            or not _same_endpoint_route(new, stopped_new, allow_reply_replacement=replaced_reply)
            or legacy is not None and not _same_endpoint_route(legacy, stopped_legacy,
                                                                allow_reply_replacement=replaced_reply)):
        _fail("full migration: stopped endpoint route differs from its plan")
    relocated = old["writer"]["host_path"] != new["writer"]["host_path"]
    if relocated:
        if (item["action"] != "copy-if-absent" or stopped_legacy is None
                or not _same_writer_view(stopped_old, stopped_legacy)
                or not stopped_legacy["referent"]["present"] or stopped_new["entry"]["type"] != "absent"):
            _fail("full migration: stopped relocation source or destination differs")
    elif not _same_writer_view(stopped_old, stopped_new):
        _fail("full migration: stopped retained writer views differ")


def _compatibility_source_v4(item: dict[str, Any]) -> dict[str, Any]:
    """The stopped endpoint whose bytes the native readers compare, keyed by action.

    Only a relocation reads the legacy file; a retained writer (validate-existing, none)
    compares the active old endpoint even when a stale legacy file still exists.
    """
    return item["quiescent_legacy_endpoint"] if item["action"] == "copy-if-absent" else item["quiescent_old_endpoint"]


def _compatibility_v4(value: Any, source: dict[str, Any], where: str) -> None:
    evidence = runtime._object(value, {"status", "raw_sha256", "old_canonical_sha256", "new_canonical_sha256",
                                      "old_writeable", "clock"}, where)
    if evidence["status"] not in ("pending", "complete"):
        _fail(f"{where}: invalid native comparison status")
    raw = source["referent"]["sha256"]
    if evidence["raw_sha256"] != raw:
        _fail(f"{where}: source content differs")
    hashes = (evidence["old_canonical_sha256"], evidence["new_canonical_sha256"])
    if evidence["status"] == "pending" or raw is None:
        if hashes != (None, None):
            _fail(f"{where}: absent state has canonical content")
    else:
        for digest in hashes:
            runtime._match(digest, runtime._HEX64, where)
        if hashes[0] != hashes[1]:
            _fail(f"{where}: native reader semantics differ")
    if runtime._boolean(evidence["old_writeable"], where) is not True:
        _fail(f"{where}: old writer is not writable")
    runtime._integer(evidence["clock"], where)


def _copy_progress_v4(value: Any, *, journal_id: str, state_kind: str, direction: str,
                      source: dict[str, Any], destination: dict[str, Any], image: dict[str, Any]) -> None:
    import component_state_endpoint as endpoint  # noqa: PLC0415

    record = runtime._object(value, {"binding", "phase", "stage_before", "reservation", "staged", "published", "discard"}, "full copy")
    binding = runtime._object(record["binding"], {
        "schema_version", "operation", "transaction_id", "state_kind", "direction",
        "source_endpoint_sha256", "destination_before_sha256", "image_id", "image_revision", "user",
        "recipe_sha256", "mounts_sha256", "stage_path", "stage_parent_identity", "expected_raw_sha256",
    }, "full copy.binding")
    if (type(binding["schema_version"]) is not int or binding["schema_version"] != 2
            or binding["operation"] != "state-copy" or binding["transaction_id"] != journal_id
            or binding["state_kind"] != state_kind or binding["direction"] != direction
            or binding["source_endpoint_sha256"] != _endpoint_hash(source)
            or binding["destination_before_sha256"] != _endpoint_hash(destination)
            or binding["image_id"] != image["image_id"] or binding["image_revision"] != image["image_revision"]
            or binding["user"] != image["user"] or binding["recipe_sha256"] != image["recipe_sha256"]
            or binding["expected_raw_sha256"] != source["referent"]["sha256"]):
        _fail("full copy: immutable binding differs")
    runtime._match(binding["mounts_sha256"], runtime._HEX64, "full copy mounts hash")
    runtime._match(binding["expected_raw_sha256"], runtime._HEX64, "full copy source hash")
    writer = destination["writer"]
    expected_stage = str(Path(writer["host_path"]).parent / f".component-full-{journal_id}-{state_kind}.{direction}")
    if binding["stage_path"] != expected_stage or binding["stage_parent_identity"] != writer["parent_identity"]:
        _fail("full copy: stage path or parent differs")
    phases = ("intent", "reserved", "writing", "staged", "publish-intent", "published")
    if record["phase"] not in phases:
        _fail("full copy: invalid progress")
    for key in ("stage_before", "reservation", "staged", "published"):
        if record[key] is not None:
            endpoint.validate_endpoint(record[key])
    index = phases.index(record["phase"])
    if ((record["reservation"] is not None) != (index >= 1)
            or (record["staged"] is not None) != (index >= 3)
            or (record["published"] is not None) != (index >= 5)):
        _fail("full copy: progress evidence differs from its phase")
    stage_before = record["stage_before"]
    if stage_before is not None and (stage_before["kind"] != state_kind
            or stage_before["image_id"] != image["image_id"] or stage_before["image_revision"] != image["image_revision"]
            or stage_before["user"] != image["user"] or stage_before["recipe_sha256"] != image["recipe_sha256"]
            or stage_before["entry"]["host_path"] != binding["stage_path"]
            or stage_before["entry"]["type"] != "absent" or stage_before["links"]
            or stage_before["entry"]["parent_identity"] != binding["stage_parent_identity"]):
        _fail("full copy: original stage was not absent at its bound parent")
    if index >= 1 and stage_before is None:
        _fail("full copy: original stage absence is missing")
    discard = record["discard"]
    if discard is not None:
        runtime._object(discard, {"phase", "copy_phase", "stage_after"}, "full copy discard")
        if (direction != "forward" or record["phase"] not in ("reserved", "writing", "staged", "publish-intent")
                or discard["copy_phase"] != record["phase"] or discard["phase"] not in ("intent", "removed")
                or (discard["stage_after"] is not None) != (discard["phase"] == "removed")):
            _fail("full copy: discard intent differs")
        if discard["stage_after"] is not None:
            endpoint.validate_endpoint(discard["stage_after"])
            if (discard["stage_after"]["entry"]["host_path"] != binding["stage_path"]
                    or discard["stage_after"]["entry"]["type"] != "absent"
                    or discard["stage_after"]["entry"]["parent_identity"] != binding["stage_parent_identity"]):
                _fail("full copy: discarded stage is not absent")
    if not writer["can_replace"]:
        _fail("full copy: recorded destination writer is not writable")
    if direction == "forward" and destination["entry"]["type"] != "absent":
        _fail("full copy: forward destination entry is not absent")
    for key in ("reservation", "staged"):
        stage = record[key]
        if stage is not None and (stage["kind"] != state_kind or stage["image_id"] != image["image_id"]
                or stage["image_revision"] != image["image_revision"] or stage["user"] != image["user"]
                or stage["recipe_sha256"] != image["recipe_sha256"]
                or stage["entry"]["host_path"] != binding["stage_path"]
                or stage["entry"]["type"] != "regular" or stage["links"]
                or stage["entry"]["parent_identity"] != binding["stage_parent_identity"]):
            _fail("full copy: stage identity or image differs")
    if record["reservation"] is not None:
        reserved = record["reservation"]
        if (reserved["referent"]["stat"]["size"] != 0
                or reserved["referent"]["sha256"] != hashlib.sha256(b"").hexdigest()):
            _fail("full copy: reservation differs")
    if record["staged"] is not None:
        staged = record["staged"]
        reservation_stat, stage_stat = reserved["referent"]["stat"], staged["referent"]["stat"]
        if (staged["referent"]["sha256"] != binding["expected_raw_sha256"]
                or any(reservation_stat[key] != stage_stat[key]
                       for key in ("device", "inode", "mode", "uid", "gid", "nlink"))):
            _fail("full copy: staged contents differ")
    if record["published"] is not None:
        published = record["published"]
        published_stat = published["referent"]["stat"]
        if (published["kind"] != state_kind or published["image_id"] != image["image_id"]
                or published["image_revision"] != image["image_revision"] or published["user"] != image["user"]
                or published["recipe_sha256"] != image["recipe_sha256"]
                or published["effective_path"] != destination["effective_path"]
                or published["writer"]["host_path"] != writer["host_path"]
                or published["referent"]["sha256"] != binding["expected_raw_sha256"]
                or published_stat is None or any(stage_stat[key] != published_stat[key]
                    for key in ("device", "inode", "size", "mtime_ns", "mode", "uid", "gid", "nlink"))):
            _fail("full copy: published endpoint differs")
        if (writer["role"] == "referent" and destination["entry"]["type"] == "symlink"
                and (published["entry"] != destination["entry"] or published["links"] != destination["links"])):
            _fail("full copy: settings link chain changed")
        if writer["role"] == "entry" and (published["entry"]["type"] != "regular" or published["links"]):
            _fail("full copy: reply entry was not replaced by its recorded stage")


def _migrations_v4(value: Any, *, journal_id: str, before: dict[str, Any], target: dict[str, Any]) -> None:
    record = runtime._object(value, {"status", "items"}, "full.migrations")
    status, items = record["status"], record["items"]
    if status not in ("not-applicable", "pending", "planned", "applied", "copyback", "restored"):
        _fail("full migrations: invalid status")
    if type(items) is not list or len(items) > 2:
        _fail("full migrations: invalid items")
    old_component, new_component = before["telegram"], target["telegram"]
    if old_component is not None and new_component is None:
        _fail("full migrations: Telegram target disappeared")
    if old_component is None:
        if status != "not-applicable" or items:
            _fail("full migrations: Telegram is absent")
        return
    if status == "not-applicable" or status == "pending" and items or status != "pending" and len(items) != 2:
        _fail("full migrations: inconsistent state")
    if not items:
        return
    import component_state_endpoint as endpoint  # noqa: PLC0415

    seen: set[str] = set()
    for item in items:
        runtime._object(item, {"kind", "action", "old_endpoint", "new_endpoint", "legacy_endpoint",
                               "quiescent_old_endpoint", "quiescent_new_endpoint", "quiescent_legacy_endpoint",
                               "retained_reply_proof", "compatibility", "forward", "forward_old_readback",
                               "progressed_endpoint", "rollback_compatibility",
                               "rollback", "cleanup"}, "full migration")
        kind = item["kind"]
        if kind not in ("chat-settings", "channel-replies") or kind in seen:
            _fail("full migration: invalid or duplicate kind")
        seen.add(kind)
        if item["action"] not in ("none", "validate-existing", "copy-if-absent"):
            _fail("full migration: invalid action")
        for key in ("old_endpoint", "new_endpoint", "legacy_endpoint", "quiescent_old_endpoint",
                    "quiescent_new_endpoint", "quiescent_legacy_endpoint", "progressed_endpoint"):
            if item[key] is not None:
                endpoint.validate_endpoint(item[key])
                if item[key]["kind"] != kind:
                    _fail("full migration: endpoint kind differs")
        if item["old_endpoint"] is None or item["new_endpoint"] is None:
            _fail("full migration: planned endpoints are missing")
        for key, component in (("old_endpoint", old_component), ("new_endpoint", new_component),
                               ("quiescent_old_endpoint", old_component), ("quiescent_new_endpoint", new_component),
                               ("legacy_endpoint", new_component), ("quiescent_legacy_endpoint", new_component),
                               ("progressed_endpoint", new_component)):
            value = item[key]
            if value is not None and (value["recipe_sha256"] != component["recipe"]["sha256"]
                    or value["image_id"] != component["image"]["id"]
                    or value["image_revision"] != component["image"]["revision"]):
                _fail("full migration: endpoint image or recipe differs")
        old_endpoint, new_endpoint = item["old_endpoint"], item["new_endpoint"]
        if not old_endpoint["writer"]["can_replace"] or not new_endpoint["writer"]["can_replace"]:
            _fail("full migration: writer is not writable")
        relocated = old_endpoint["writer"]["host_path"] != new_endpoint["writer"]["host_path"]
        if relocated:
            if (item["action"] != "copy-if-absent" or item["legacy_endpoint"] is None
                    or not _same_writer_view(old_endpoint, item["legacy_endpoint"])
                    or not item["legacy_endpoint"]["referent"]["present"]
                    or new_endpoint["entry"]["type"] != "absent"):
                _fail("full migration: relocation differs from the old writer or has a target entry")
        elif item["action"] == "copy-if-absent" or not _same_writer_view(old_endpoint, new_endpoint):
            _fail("full migration: retained writer views differ")
        _validate_quiescent_v4(item)
        if (status == "applied" and (item["quiescent_old_endpoint"] is None
                or item["quiescent_new_endpoint"] is None or item["compatibility"] is None
                or item["compatibility"]["status"] != "complete")):
            _fail("full migration: applied state lacks complete native evidence")
        if item["compatibility"] is not None:
            if item["quiescent_old_endpoint"] is None:
                _fail("full migration: quiescent old endpoint is missing")
            source = _compatibility_source_v4(item)
            _compatibility_v4(item["compatibility"], source, "full migration.compatibility")
        if item["forward"] is not None:
            if (item["action"] != "copy-if-absent" or item["quiescent_legacy_endpoint"] is None
                    or item["quiescent_new_endpoint"] is None or item["compatibility"] is None
                    or item["compatibility"]["status"] != "complete"):
                _fail("full migration: forward copy lacks endpoints")
            _copy_progress_v4(item["forward"], journal_id=journal_id, state_kind=kind, direction="forward",
                              source=item["quiescent_legacy_endpoint"], destination=item["quiescent_new_endpoint"],
                              image=item["quiescent_new_endpoint"])
        if item["forward_old_readback"] is not None:
            readback = runtime._object(item["forward_old_readback"], {
                "source_endpoint_sha256", "raw_sha256", "canonical_sha256", "old_writeable", "clock",
            }, "full migration.forward_old_readback")
            if (item["forward"] is None or item["forward"]["phase"] != "published"
                    or readback["source_endpoint_sha256"] != _endpoint_hash(item["forward"]["published"])
                    or readback["raw_sha256"] != item["forward"]["published"]["referent"]["sha256"]
                    or readback["canonical_sha256"] != item["compatibility"]["old_canonical_sha256"]
                    or readback["clock"] != item["compatibility"]["clock"] or readback["old_writeable"] is not True):
                _fail("full migration: published state is not bound to the old native reader")
        if (status == "applied" and item["action"] == "copy-if-absent"
                and item["forward_old_readback"] is None):
            _fail("full migration: published state was not verified for the old reader")
        if item["rollback_compatibility"] is not None:
            if item["progressed_endpoint"] is None:
                _fail("full migration: progressed endpoint is missing")
            _compatibility_v4(item["rollback_compatibility"], item["progressed_endpoint"],
                              "full migration.rollback_compatibility")
        if item["rollback"] is not None:
            if (item["progressed_endpoint"] is None or item["quiescent_old_endpoint"] is None
                    or item["rollback_compatibility"] is None
                    or item["rollback_compatibility"]["status"] != "complete"):
                _fail("full migration: rollback copy lacks endpoints")
            _copy_progress_v4(item["rollback"], journal_id=journal_id, state_kind=kind, direction="rollback",
                              source=item["progressed_endpoint"], destination=item["quiescent_old_endpoint"],
                              image=item["quiescent_old_endpoint"])
        if item["cleanup"] is not None:
            cleanup = runtime._object(item["cleanup"], {"phase", "created_entry_claim_sha256",
                                                        "post_stop_new_endpoint", "old_published_endpoint", "removed_endpoint"},
                                      "full migration.cleanup")
            if (cleanup["phase"] not in ("intent", "removed") or item["forward"] is None
                    or item["forward"]["phase"] != "published"
                    or item["quiescent_new_endpoint"]["entry"]["type"] != "absent"):
                _fail("full migration: cleanup lacks a created entry")
            if cleanup["created_entry_claim_sha256"] != _endpoint_hash(item["forward"]["binding"]):
                _fail("full migration: cleanup claim differs")
            endpoint.validate_endpoint(cleanup["post_stop_new_endpoint"])
            endpoint.validate_endpoint(cleanup["old_published_endpoint"])
            if cleanup["post_stop_new_endpoint"] != item["progressed_endpoint"]:
                _fail("full migration: cleanup source differs")
            if (cleanup["removed_endpoint"] is not None) != (cleanup["phase"] == "removed"):
                _fail("full migration: cleanup readback differs from phase")
            if cleanup["removed_endpoint"] is not None:
                endpoint.validate_endpoint(cleanup["removed_endpoint"])
                if not _cleanup_removed(item["quiescent_new_endpoint"], cleanup["post_stop_new_endpoint"],
                                        cleanup["removed_endpoint"]):
                    _fail("full migration: cleanup removed a different entry")
            if item["rollback"] is not None and cleanup["old_published_endpoint"] != item["rollback"]["published"]:
                _fail("full migration: cleanup old publication differs")
            if item["rollback"] is None and (cleanup["old_published_endpoint"] != item["quiescent_old_endpoint"]
                    or cleanup["old_published_endpoint"]["referent"]["sha256"] != item["progressed_endpoint"]["referent"]["sha256"]):
                _fail("full migration: unchanged old endpoint does not contain progressed state")


def validate_journal(value: Any) -> dict[str, Any]:
    """Parse preserved FULL3 and endpoint-aware FULL4 transaction records."""
    try:
        journal = runtime._object(value, {
            "schema_version", "kind", "transaction_id", "action", "status", "phase", "source",
            "predecessor", "selection", "before", "target", "database", "api_identity",
            "migrations", "pointers", "manifest",
        }, "full journal")
        if (type(journal["schema_version"]) is not int
                or journal["schema_version"] not in SCHEMA_VERSIONS):
            _fail("full journal: unsupported schema")
        if journal["kind"] != "component-full" or journal["action"] != "update":
            _fail("full journal: unsupported kind or action")
        identifier = runtime._match(journal["transaction_id"], runtime._DEPLOYMENT_ID, "full transaction ID")
        runtime.validate_predecessor(journal["predecessor"], successor_id=identifier, required=True)
        status, phase = journal["status"], journal["phase"]
        if status not in ("active", "unresolved", "rolled_back", "committed") or phase not in PHASES:
            _fail("full journal: invalid status or phase")
        if journal["schema_version"] == PREVIOUS_SCHEMA_VERSION and phase == "rollback-untouched":
            _fail("full journal: old schema cannot contain a FULL4-only phase")
        if (status == "committed") != (phase == "committed") or (status == "rolled_back") != (phase == "rolled-back"):
            _fail("full journal: terminal status and phase disagree")
        source = recovery._validate_source(journal["source"])
        if "compose" not in source:
            _fail("full journal: saved Compose binding is required")
        selection = journal["selection"]
        if selection not in ([], ["api"], ["telegram"], ["api", "telegram"]):
            _fail("full journal: invalid image selection")
        before = runtime._object(journal["before"], {"manifest", "topology", "containers"}, "full.before")
        target = runtime._object(journal["target"], {"manifest", "containers"}, "full.target")
        previous = runtime._manifest(before["manifest"], "full.before.manifest")
        proposed = runtime._manifest(target["manifest"], "full.target.manifest")
        topology = runtime._object(before["topology"], {"api", "telegram"}, "full.before.topology")
        if previous["components"] != topology or proposed["deployment_id"] != identifier:
            _fail("full journal: manifest topology or deployment differs")
        operations = proposed["operations"]
        if (operations["source_commit"] != source["commit"] or operations["source_tree"] != source["tree"]
                or operations["source_tool_sha256"] != source["source_tool_sha256"]):
            _fail("full journal: target operations source differs")
        if previous["api_identity"] != journal["api_identity"] or proposed["api_identity"] != journal["api_identity"]:
            _fail("full journal: API identity changed")
        old_containers = runtime._object(before["containers"], {"api", "telegram"}, "full.before.containers")
        new_containers = runtime._object(target["containers"], {"api", "telegram"}, "full.target.containers")
        for name in ("api", "telegram"):
            old, new = topology[name], proposed["components"][name]
            observed, expected = old_containers[name], new_containers[name]
            if old is None:
                if new is not None or observed is not None or expected is not None or name in selection:
                    _fail("full journal: absent component was selected or installed")
                continue
            if new is None or observed is None or expected is None:
                _fail("full journal: installed component disappeared")
            runtime._container(observed, f"full.before.{name}", observed=True)
            runtime._container(expected, f"full.target.{name}", observed=False)
            for container, component in ((observed, old), (expected, new)):
                if (container["image_id"] != component["image"]["id"]
                        or container["project"] != component["recipe"]["project"]
                        or container["service"] != component["recipe"]["service"]):
                    _fail("full journal: image or recipe binding differs")
            if observed["running"] != expected["running"]:
                _fail("full journal: target running state differs")
            if name not in selection:
                stable = ("id", "image_id", "project", "service", "name", "transaction_label")
                if old != new or any(observed[key] != expected[key] for key in stable):
                    _fail("full journal: unselected image or container changed")
            elif expected["transaction_label"] != identifier:
                _fail("full journal: selected target label differs")
        database = runtime._object(journal["database"], {
            "old", "candidate", "backup", "stage", "restore", "promoted_stat", "restored_stat",
        }, "full.database")
        old_db = runtime._object(database["old"], {"binding", "stat", "permissions"}, "full.database.old")
        if old_db["binding"] != previous["database"]:
            _fail("full journal: old database differs from its manifest")
        runtime._stat(old_db["stat"], "full.database.old.stat")
        permissions = _permissions(old_db["permissions"], "full.database.old.permissions")
        candidate = runtime._object(database["candidate"], {"path", "binding", "stat", "verification"},
                                    "full.database.candidate")
        if candidate["path"] != f".deployments/component-full-candidates/{identifier}.db":
            _fail("full journal: candidate path differs")
        binding = runtime._object(candidate["binding"], {"path", "sha256", "schema_version", "provenance"},
                                  "full.database.candidate.binding")
        path = runtime._absolute(binding["path"], "full.database.candidate.binding.path")
        if not path.endswith("/" + candidate["path"]):
            _fail("full journal: candidate is not bound to its owned path")
        expected_db = {**binding, "path": old_db["binding"]["path"]}
        if proposed["database"] != expected_db:
            _fail("full journal: target database differs from candidate")
        runtime._stat(candidate["stat"], "full.database.candidate.stat")
        verification = runtime._object(candidate["verification"], set(_VERIFY), "full.database.candidate.verification")
        if verification != _VERIFY or any(type(v) is not type(_VERIFY[k]) for k, v in verification.items()):
            _fail("full journal: candidate verification differs")
        backup = runtime._object(database["backup"], {"path", "sha256", "stat", "permissions"}, "full.database.backup")
        matched = _BACKUP.fullmatch(backup["path"]) if type(backup["path"]) is str else None
        if (matched is None or matched.group(1) != identifier[:16]
                or matched.group(2) != str(int(identifier[17:], 16))
                or matched.group(3) != old_db["binding"]["sha256"]
                or backup["sha256"] != old_db["binding"]["sha256"]):
            _fail("full journal: backup binding differs")
        _optional_stat(backup["stat"], "full.database.backup.stat")
        if _permissions(backup["permissions"], "full.database.backup.permissions") != permissions:
            _fail("full journal: backup ownership or mode differs")
        stage = runtime._object(database["stage"], {"path", "sha256", "stat", "permissions"}, "full.database.stage")
        expected_stage = str(Path(old_db["binding"]["path"]).parent / f".component-full-promote-{identifier}.db")
        if stage["path"] != expected_stage or stage["sha256"] != binding["sha256"]:
            _fail("full journal: promotion stage binding differs")
        _optional_stat(stage["stat"], "full.database.stage.stat")
        if _permissions(stage["permissions"], "full.database.stage.permissions") != permissions:
            _fail("full journal: promotion ownership or mode differs")
        restore = runtime._object(database["restore"], {"path", "sha256", "stat", "permissions"},
                                  "full.database.restore")
        expected_restore = str(Path(old_db["binding"]["path"]).parent / f".component-full-restore-{identifier}.db")
        if restore["path"] != expected_restore or restore["sha256"] != old_db["binding"]["sha256"]:
            _fail("full journal: restore stage binding differs")
        _optional_stat(restore["stat"], "full.database.restore.stat")
        if _permissions(restore["permissions"], "full.database.restore.permissions") != permissions:
            _fail("full journal: restore ownership or mode differs")
        _optional_stat(database["promoted_stat"], "full.database.promoted_stat")
        _optional_stat(database["restored_stat"], "full.database.restored_stat")
        if journal["schema_version"] == PREVIOUS_SCHEMA_VERSION:
            _migrations(journal["migrations"], telegram_present=topology["telegram"] is not None,
                        transaction_id=identifier)
        elif journal["schema_version"] == FULL4_SCHEMA_VERSION:
            _migrations_v4(journal["migrations"], journal_id=identifier, before=topology,
                           target=proposed["components"])
        else:
            _migrations_v5(journal["migrations"], before=topology, target=proposed["components"])
        if status == "committed":
            if backup["stat"] is None or stage["stat"] is None or database["promoted_stat"] is None:
                _fail("full journal: committed database has incomplete durable evidence")
            if topology["telegram"] is not None and journal["migrations"]["status"] != "applied":
                _fail("full journal: committed Telegram state was not reconciled")
            for item in journal["migrations"]["items"]:
                if journal["schema_version"] == PREVIOUS_SCHEMA_VERSION:
                    if item["old_preimage"] is None or item["new_preimage"] is None or item["compatibility"] is None:
                        _fail("full journal: committed Telegram state evidence is incomplete")
                    if (item["action"] == "copy-if-absent"
                            and (item["forward_stage_preimage"] is None or item["created"] is None)):
                        _fail("full journal: committed Telegram copy evidence is incomplete")
                elif journal["schema_version"] == FULL4_SCHEMA_VERSION:
                    if (item["quiescent_old_endpoint"] is None or item["quiescent_new_endpoint"] is None
                            or item["compatibility"] is None or item["compatibility"]["status"] != "complete"):
                        _fail("full journal: committed Telegram endpoint evidence is incomplete")
                    if (item["action"] == "copy-if-absent"
                            and (item["forward"] is None or item["forward"]["phase"] != "published")):
                        _fail("full journal: committed Telegram endpoint copy is incomplete")
                elif (item["compatibility"] is None
                      or item["legacy_preimage"] is not None and not item["legacy_preimage"]["present"]
                      and item["action"] == "copy-if-absent"):
                    # The product copied at start; a committed record keeps the
                    # stopped preimage evidence and the native reader comparison.
                    _fail("full journal: committed preserved-state evidence is incomplete")
            for name in ("api", "telegram"):
                if new_containers[name] is not None:
                    runtime._container(new_containers[name], f"full.target.{name}", observed=True)
        if status == "rolled_back":
            if (journal["schema_version"] == SCHEMA_VERSION
                    and journal["migrations"]["status"] not in ("not-applicable", "pending", "planned", "restored")):
                _fail("full journal: schema-5 Telegram state was not reconciled")
            for item in journal["migrations"]["items"]:
                if journal["schema_version"] == PREVIOUS_SCHEMA_VERSION:
                    if (item["action"] == "copy-if-absent" and item["created"] is not None
                            and (item["copyback"] is None or item["copyback"]["phase"] != "cleaned")):
                        _fail("full journal: relocated Telegram state has not been reconciled")
                elif journal["schema_version"] == FULL4_SCHEMA_VERSION:
                    if (item["forward"] is not None and item["forward"]["phase"] == "published"
                            and (item["cleanup"] is None or item["cleanup"]["phase"] != "removed")):
                        _fail("full journal: relocated Telegram endpoint has not been reconciled")
                    elif (item["forward"] is not None and item["forward"]["reservation"] is not None
                          and item["forward"]["phase"] != "published"
                          and (item["forward"]["discard"] is None or item["forward"]["discard"]["phase"] != "removed")):
                        _fail("full journal: unpublished Telegram stage has not been removed")
                elif (journal["migrations"]["status"] == "restored"
                      and not _v5_rollback_evidence(item)):
                    _fail("full journal: kept Telegram state has no native reader evidence")
        pointers = runtime._object(journal["pointers"], {"old", "target"}, "full.pointers")
        for which in ("old", "target"):
            pair = runtime._object(pointers[which], {".deploy_commit", ".deploy_current"}, f"full.pointers.{which}")
            for name, item in pair.items():
                runtime._pointer(item, name, f"full.pointers.{which}.{name}")
        if (pointers["old"][".deploy_commit"] != {"present": True, "bytes": previous["operations"]["source_commit"] + "\n"}
                or pointers["old"][".deploy_current"] != {"present": True, "bytes": previous["deployment_id"] + "\n"}
                or pointers["target"][".deploy_commit"] != {"present": True, "bytes": source["commit"] + "\n"}
                or pointers["target"][".deploy_current"] != {"present": True, "bytes": identifier + "\n"}):
            _fail("full journal: deployment pointers differ")
        binding = runtime._object(journal["manifest"], {"deployment_id", "path", "sha256"}, "full.manifest")
        if (binding["deployment_id"] != identifier or binding["path"] != f".deployments/products/{identifier}.json"
                or binding["sha256"] != hashlib.sha256(manifest._encode(proposed)).hexdigest()):
            _fail("full journal: immutable manifest binding differs")
        return journal
    except FullError:
        raise
    except (runtime.JournalError, recovery.RecoveryError, manifest.ManifestError, ValueError, TypeError, KeyError) as exc:
        raise FullError("invalid full transaction journal") from exc


def load_journal(path: Path) -> dict[str, Any]:
    return validate_journal(runtime._decode(runtime._read_regular(path)))


def _verify_handoff_containers(winning: dict[str, Any], fresh: dict[str, Any]) -> None:
    for name in ("api", "telegram"):
        old, current = winning[name], fresh[name]
        if (old is None) != (current is None):
            _fail("handoff: installed component changed")
        if old is not None:
            for key in ("id", "image_id", "project", "service", "name", "transaction_label"):
                if old[key] != current[key]:
                    _fail("handoff: container binding differs")


def _verify_null_manifest_handoff(*, successor_kind: str, successor: dict[str, Any],
                                  predecessor_kind: str, predecessor: dict[str, Any]) -> None:
    """Compare a rolled-back unadopted preimage to the next adoption."""
    if (successor_kind != "runtime" or predecessor_kind != "runtime"
            or any(record.get("kind") != "component-runtime" or record.get("schema_version") != 4
                   or record.get("action") != "adopt" or record.get("selection") != []
                   or record.get("migrations") != [] for record in (predecessor, successor))
            or predecessor.get("status") != "rolled_back"):
        _fail("handoff: null predecessor is not a rolled-back runtime adoption")
    runtime.validate_journal(predecessor)
    runtime.validate_journal(successor)
    old, fresh = predecessor["before"], successor["before"]
    if (old["manifest"] is not None or fresh["manifest"] is not None
            or fresh["topology"] != old["topology"]
            or successor["pointers"]["old"] != predecessor["pointers"]["old"]
            or successor["database"] != predecessor["database"]
            or successor["api_identity"] != predecessor["api_identity"]):
        _fail("handoff: successor deployment preimage differs")
    _verify_handoff_containers(old["containers"], fresh["containers"])


def verify_handoff(root: Path, *, successor_kind: str, successor: dict[str, Any],
                   predecessor: dict[str, Any], predecessor_raw: bytes) -> None:
    """Verify the exact archived predecessor and winning deployment preimage."""
    try:
        if successor_kind not in ("runtime", "full"):
            _fail("handoff: unsupported successor kind")
        if successor_kind == "runtime":
            runtime.validate_journal(successor)
        else:
            validate_journal(successor)
        reference = successor.get("predecessor")
        if type(reference) is not dict or reference.get("kind") not in ("runtime", "full"):
            _fail("handoff: predecessor kind is missing")
        predecessor_kind = reference["kind"]
        if predecessor_kind == "runtime":
            runtime.validate_journal(predecessor)
        else:
            validate_journal(predecessor)
        if predecessor["status"] not in ("committed", "rolled_back"):
            _fail("handoff: predecessor is not terminal")
        expected = runtime.predecessor_reference(predecessor_kind, predecessor["transaction_id"], predecessor_raw)
        if successor.get("predecessor") != expected:
            _fail("handoff: predecessor identity differs")
        history_name = f"component-{predecessor_kind}-history"
        with manifest._directory(root, (".deployments", history_name), create=False) as directory:
            archived = manifest._read_file(directory, predecessor["transaction_id"] + ".json")
        if archived != predecessor_raw:
            _fail("handoff: archived predecessor bytes differ")
        won = "target" if predecessor["status"] == "committed" else "before"
        winning_manifest = (predecessor["target"]["manifest"] if won == "target"
                            else predecessor["before"]["manifest"])
        winning_containers = predecessor[won]["containers"]
        winning_pointers = predecessor["pointers"]["target" if won == "target" else "old"]
        before = successor["before"]
        if winning_manifest is None:
            _verify_null_manifest_handoff(successor_kind=successor_kind, successor=successor,
                                          predecessor_kind=predecessor_kind, predecessor=predecessor)
        else:
            if (before["manifest"] != winning_manifest
                    or before["topology"] != winning_manifest["components"]
                    or successor["pointers"]["old"] != winning_pointers):
                _fail("handoff: successor deployment preimage differs")
            _verify_handoff_containers(winning_containers, before["containers"])
    except FullError:
        raise
    except (runtime.JournalError, manifest.ManifestError, OSError, ValueError, TypeError, KeyError) as exc:
        raise FullError("handoff: predecessor cannot be verified") from exc


def verify_predecessor(root: Path, *, successor_kind: str, successor: dict[str, Any]) -> None:
    """Require the immutable predecessor archive after successor publication."""
    reference = successor.get("predecessor")
    if reference is None:
        if successor_kind == "full" or successor.get("schema_version") in (4, 5) and successor.get("action") == "update":
            _fail("handoff: published successor has no predecessor")
        return
    runtime.validate_predecessor(reference, successor_id=successor["transaction_id"], required=True)
    try:
        history = f"component-{reference['kind']}-history"
        with manifest._directory(root, (".deployments", history), create=False) as directory:
            raw = manifest._read_file(directory, reference["transaction_id"] + ".json")
        predecessor = runtime._decode(raw)
        verify_handoff(root, successor_kind=successor_kind, successor=successor,
                       predecessor=predecessor, predecessor_raw=raw)
    except FullError:
        raise
    except (runtime.JournalError, manifest.ManifestError, OSError, ValueError, TypeError, KeyError) as exc:
        raise FullError("handoff: predecessor archive cannot be verified") from exc


def _sync_handoff_markers(root: Path, *, previous_name: str, previous_raw: bytes | None,
                          successor_name: str, successor_raw: bytes) -> None:
    """Make a visible successor durable before retiring an exact predecessor."""
    if previous_name not in (JOURNAL_NAME, runtime.JOURNAL_NAME) or successor_name not in (
            JOURNAL_NAME, runtime.JOURNAL_NAME) or previous_name == successor_name:
        _fail("handoff: marker names differ")
    previous_path, successor_path = root / ".deployments" / previous_name, root / ".deployments" / successor_name
    with manifest._directory(root, (".deployments",), create=False) as directory:
        def check() -> None:
            if runtime._read_regular(successor_path) != successor_raw:
                _fail("handoff: successor changed before durable handoff")
            try:
                previous_path.lstat()
            except FileNotFoundError:
                if previous_raw is not None:
                    _fail("handoff: predecessor disappeared before durable handoff")
            else:
                if previous_raw is None or runtime._read_regular(previous_path) != previous_raw:
                    _fail("handoff: predecessor changed before durable handoff")

        check()
        os.fsync(directory)
        check()
        if previous_raw is not None:
            os.unlink(previous_name, dir_fd=directory)
            os.fsync(directory)
        if runtime._read_regular(successor_path) != successor_raw:
            _fail("handoff: successor changed after predecessor removal")


def finish_handoff(root: Path, *, successor_kind: str, successor: dict[str, Any]) -> None:
    """After exact-source admission, retire a still-present cross-kind marker."""
    import deployment_admission as admission  # noqa: PLC0415

    try:
        if successor_kind not in ("runtime", "full"):
            _fail("handoff: unsupported successor kind")
        root = admission._physical_root(root)
        admission.require_lock(root)
        identifier = successor["transaction_id"]
        source_id = successor["source"]["source_tool_sha256"]
        admitted = (admission.admit_full_recovery(root, transaction_id=identifier,
                                                   source_tool_sha256=source_id, code_root=CODE_ROOT)
                    if successor_kind == "full" else
                    admission.admit_component_recovery(root, transaction_id=identifier,
                                                        source_tool_sha256=source_id, code_root=CODE_ROOT))
        if admitted != successor:
            _fail("handoff: successor changed during recovery admission")
        reference = successor.get("predecessor")
        if reference is None or reference["kind"] == successor_kind:
            return
        previous_path = root / reference["journal_path"]
        if not admission._present(previous_path):
            if successor["status"] in ("active", "unresolved"):
                successor_name = JOURNAL_NAME if successor_kind == "full" else runtime.JOURNAL_NAME
                current_raw = runtime._read_regular(root / ".deployments" / successor_name)
                if runtime._decode(current_raw) != successor:
                    _fail("handoff: successor changed before directory sync")
                _sync_handoff_markers(root, previous_name=Path(reference["journal_path"]).name,
                                      previous_raw=None, successor_name=successor_name, successor_raw=current_raw)
            return
        previous_raw = runtime._read_regular(previous_path)
        previous = runtime._decode(previous_raw)
        verify_handoff(root, successor_kind=successor_kind, successor=successor,
                       predecessor=previous, predecessor_raw=previous_raw)
        successor_name = JOURNAL_NAME if successor_kind == "full" else runtime.JOURNAL_NAME
        current_raw = runtime._read_regular(root / ".deployments" / successor_name)
        if runtime._decode(current_raw) != successor:
            _fail("handoff: successor changed before predecessor removal")
        _sync_handoff_markers(root, previous_name=Path(reference["journal_path"]).name,
                              previous_raw=previous_raw, successor_name=successor_name, successor_raw=current_raw)
    except FullError:
        raise
    except Exception as exc:
        raise FullError("handoff: predecessor removal could not be reconciled") from exc


def _prepare_request(request: Any) -> tuple[list[str], dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate one FULL request: completed artifacts, never producer receipts.

    A selection of ``none`` is a database-only transaction and must carry no
    artifact. Every selected component names exactly one registered acquisition;
    the recipe set stays a subset of the selection.
    """
    if type(request) is dict and set(request) == {"selection", "receipts", "recipes"}:
        request = {**request, "artifacts": {}}
    elif type(request) is dict and set(request) == {"selection", "artifacts", "recipes"}:
        request = {"selection": request["selection"], "receipts": {},
                   "artifacts": request["artifacts"], "recipes": request["recipes"]}
    if type(request) is not dict or set(request) != {"selection", "receipts", "artifacts", "recipes"}:
        _fail("full request is invalid")
    if request["receipts"] != {}:
        _fail("producer build receipts are protocol-1 inputs and cannot be used by this source")
    selections = {"none": [], "api": ["api"], "telegram": ["telegram"], "both": ["api", "telegram"]}
    selection = selections.get(request["selection"]) if type(request["selection"]) is str else None
    artifacts, recipes = request["artifacts"], request["recipes"]
    if selection is None or type(artifacts) is not dict or set(artifacts) != set(selection):
        _fail("one completed artifact is required for each selected component")
    if type(recipes) is not dict or not set(recipes).issubset(selection):
        _fail("recipes must refer only to selected images")
    if "api" in recipes:
        _fail("API target recipe changes are unsupported")
    for receipt_path in artifacts.values():
        if not isinstance(receipt_path, (str, Path)) or not os.fspath(receipt_path):
            _fail("artifact input must be an acquisition receipt path")
    return selection, artifacts, recipes


def prepare(root: Path, candidate_db: Path, request: dict[str, Any], *, compose_path: Path,
            expected_sha: str | None = None) -> dict[str, Any]:
    """Construct a FULL3 journal from the adopted deployment and real DB input."""
    import component_build as build  # noqa: PLC0415
    import component_full_db as full_db  # noqa: PLC0415
    import component_observe as observe  # noqa: PLC0415
    import component_prepare as prepare_runtime  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import component_wheel_build as wheel  # noqa: PLC0415
    import deployment_admission as admission  # noqa: PLC0415
    import package_recovery_source as capsule  # noqa: PLC0415

    try:
        root = admission._physical_root(root)
        admission.admit_component(root, operation="full-update", code_root=CODE_ROOT)
        selection, artifacts, recipes = _prepare_request(request)
        compose_path = transaction._compose_binary(compose_path)
        terminal = transaction.verify_terminal_for_full4(root, compose_path=compose_path)
        if terminal is None:
            _fail("full update requires an adopted terminal deployment")
        predecessor_raw = transaction._prepare_predecessor(root, compose_path, for_full4=True)
        terminal = transaction.verify_terminal_for_full4(root, compose_path=compose_path)
        if terminal is None or terminal[0] != predecessor_raw:
            _fail("terminal predecessor changed during preparation")
        previous = terminal[1]
        committed = previous["status"] == "committed"
        old = previous["target"]["manifest"] if committed else previous["before"]["manifest"]
        prior_containers = previous["target"]["containers"] if committed else previous["before"]["containers"]
        current = runtime.inspect_deployment(root)
        if old is None or current["binding"] != "recorded" or current["manifest"] != old:
            _fail("current manifest differs from the adopted terminal deployment")
        if old["schema_version"] not in (manifest.PREVIOUS_MANIFEST_SCHEMA_VERSION,
                                         manifest.MANIFEST_SCHEMA_VERSION):
            _fail("full update requires a schema-2 or schema-3 deployment")
        if any(old["components"][name] is None for name in selection):
            _fail("selected image belongs to an absent component")
        commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
        if expected_sha is not None and runtime._match(expected_sha, runtime._HEX40, "expected source commit") != commit:
            _fail("source commit differs from --expected-sha")
        recovery._load_packager(root / "scripts/package_recovery_source.py")
        recovery._verify_clean_source(root, commit, tree)
        source = recovery.prepare_source(root, commit, tree, compose_path=compose_path)
        compose_path = recovery._verify_bound_compose(root, source)
        old_stat = manifest.verify_database_binding(old["database"])
        old_permissions = full_db.permissions(Path(old["database"]["path"]))
        before: dict[str, Any] = {"api": None, "telegram": None}
        snapshots: dict[str, Any] = {}
        key = os.urandom(32)
        for name in ("api", "telegram"):
            component = old["components"][name]
            if component is None:
                continue
            prior = prior_containers[name]
            if prior is None:
                _fail("terminal container record is missing")
            snapshot = transaction._snapshot(root, component)
            actual = transaction._observed(prior["id"], snapshot, key)
            transaction._recipe_matches(actual, snapshot)
            if not transaction._same_container_binding(actual["container"], prior):
                _fail("installed component binding changed")
            image = transaction._image(snapshot["image_id"])
            if (transaction._effective_environment(snapshot, image, compose_path, root) !=
                    transaction._env_map(observe._inspect(actual["container"]["id"])["Config"].get("Env"))):
                _fail("installed environment cannot be reproduced")
            before[name], snapshots[name] = actual["container"], snapshot
        identifier = manifest.new_deployment_id()
        candidate_path = Path(os.path.abspath(os.fspath(candidate_db)))
        candidate = full_db.prepare_candidate(root, candidate_path, identifier)
        target = copy.deepcopy(old)
        target.update(deployment_id=identifier,
                      deployment_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                      operations={"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                  "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]},
                      database={**candidate["binding"], "path": old["database"]["path"]})
        target_containers = copy.deepcopy(before)
        references: dict[str, tuple[Any, ...]] = {}
        for name in ("api", "telegram"):
            component = target["components"][name]
            if (component is not None and name not in selection
                    and component["provenance"]["kind"] not in manifest.PROVENANCE_KINDS[3]):
                _fail(f"unselected {name} provenance cannot be recorded in a schema-3 manifest")
        target["schema_version"] = manifest.MANIFEST_SCHEMA_VERSION
        for name in selection:
            facts, image, receipt, bundle = prepare_runtime.artifact_input(root, name, artifacts[name])
            image_id = facts["image"]["id"]
            if name in recipes:
                requested, inputs = prepare_runtime._render_recipe(root, recipes[name], image, compose_path)
                for path, identity in inputs.items():
                    if path in references and references[path] != identity:
                        _fail("Compose input changed between selected recipes")
                    references[path] = identity
            if name == "api":
                if name in recipes:
                    _fail("API target recipe changes are unsupported")
                snapshot = {**copy.deepcopy(snapshots[name]), "image_id": image_id}
            elif old["components"]["telegram"]["provenance"]["kind"] == "legacy-image":
                # Counterexample 1: the legacy wrapper's effective DB path and
                # state bindings are translated, never re-derived.
                snapshot = prepare_runtime.translate_preserved_recipe(
                    copy.deepcopy(snapshots[name]), observe._inspect(before[name]["id"]), image)
            else:
                snapshot = {**copy.deepcopy(snapshots[name]), "image_id": image_id}
                manifest.validate_recipe_snapshot(snapshot)
                manifest.verify_poll_health_mount(
                    snapshot, effective_uid=int((snapshot["user"] or "0").split(":", 1)[0]))
            if name in recipes:
                prepare_runtime.refuse_target_recipe_change(snapshot, requested)
            path, digest = manifest.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            built = {**facts, "recipe": recipe}
            manifest.verify_artifact_binding(name, built, receipt, bundle)
            target["components"][name] = built
            target_containers[name].update(id=None, image_id=image_id, project=snapshot["project"],
                                           service=snapshot["service"], name=None, started_at=None,
                                           restart_count=None, transaction_label=identifier)
        manifest.validate_manifest(target)
        old_pointers = {".deploy_commit": transaction._pointer(root, ".deploy_commit"),
                        ".deploy_current": transaction._pointer(root, ".deploy_current")}
        new_pointers = {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                        ".deploy_current": {"present": True, "bytes": identifier + "\n"}}
        backup_name = (f"data/deployment-backups/terms.{identifier[:16]}-{int(identifier[17:], 16)}."
                       f"{old['database']['sha256']}.db")
        stage_name = str(Path(old["database"]["path"]).parent / f".component-full-promote-{identifier}.db")
        restore_name = str(Path(old["database"]["path"]).parent / f".component-full-restore-{identifier}.db")
        predecessor_kind = "full" if previous["kind"] == "component-full" else "runtime"
        journal = {
            "schema_version": SCHEMA_VERSION, "kind": "component-full", "transaction_id": identifier,
            "action": "update", "status": "active", "phase": "prepared", "source": source,
            "predecessor": runtime.predecessor_reference(predecessor_kind, previous["transaction_id"], terminal[0]),
            "selection": selection, "before": {"manifest": copy.deepcopy(old),
                "topology": copy.deepcopy(old["components"]), "containers": before},
            "target": {"manifest": target, "containers": target_containers},
            "database": {"old": {"binding": copy.deepcopy(old["database"]), "stat": old_stat,
                                 "permissions": old_permissions}, "candidate": candidate,
                         "backup": {"path": backup_name, "sha256": old["database"]["sha256"],
                                    "stat": None, "permissions": old_permissions},
                         "stage": {"path": stage_name, "sha256": candidate["binding"]["sha256"],
                                   "stat": None, "permissions": old_permissions},
                         "restore": {"path": restore_name, "sha256": old["database"]["sha256"],
                                     "stat": None, "permissions": old_permissions},
                         "promoted_stat": None, "restored_stat": None},
            "api_identity": copy.deepcopy(old["api_identity"]),
            "migrations": {"status": "pending" if old["components"]["telegram"] else "not-applicable", "items": []},
            "pointers": {"old": old_pointers, "target": new_pointers},
            "manifest": {"deployment_id": identifier, "path": f".deployments/products/{identifier}.json",
                         "sha256": hashlib.sha256(manifest._encode(target)).hexdigest()},
        }
        validate_journal(journal)
        context = copy.deepcopy(journal)
        context["database"] = {"binding": old["database"], "stat": old_stat}
        context["migrations"] = []
        transaction._verify_live_topology(root, context, which="before", compose_path=compose_path,
                                          full4_endpoints=True)
        if any(runtime._compose_input(Path(path)) != identity for path, identity in references.items()):
            _fail("Compose input changed during preparation")
        latest = transaction.verify_terminal_for_full4(root, compose_path=compose_path)
        if latest is None or latest[0] != terminal[0] or full_db.permissions(Path(old["database"]["path"])) != old_permissions:
            _fail("terminal deployment changed during full preparation")
        return journal
    except FullError:
        raise
    except (admission.AdmissionError, build.BuildError, wheel.WheelBuildError, capsule.CapsuleError,
            recovery.RecoveryError, runtime.JournalError, transaction.TransactionError, observe.ObservationError,
            manifest.ManifestError, full_db.FullDatabaseError, OSError, ValueError, TypeError, KeyError) as exc:
        raise FullError("full transaction preparation was refused") from exc


def _verify_initial_preimage(root: Path, journal: dict[str, Any], predecessor: dict[str, Any],
                             predecessor_raw: bytes, *, compose_path: Path, run_verifier: bool) -> None:
    """Recheck the verified terminal winner and all FULL3 inputs before publish."""
    import component_full_db as full_db  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    journal = validate_journal(journal)
    if journal["status"] != "active" or journal["phase"] != "prepared":
        _fail("full successor is not prepared")
    kind = "full" if predecessor["kind"] == "component-full" else "runtime"
    if journal["predecessor"] != runtime.predecessor_reference(kind, predecessor["transaction_id"], predecessor_raw):
        _fail("full successor predecessor binding differs")
    won = "target" if predecessor["status"] == "committed" else "before"
    old = predecessor["target"]["manifest"] if won == "target" else predecessor["before"]["manifest"]
    records = predecessor[won]["containers"]
    pointers = predecessor["pointers"]["target" if won == "target" else "old"]
    if (old is None or journal["before"]["manifest"] != old or journal["before"]["topology"] != old["components"]
            or journal["pointers"]["old"] != pointers):
        _fail("full successor deployment preimage differs")
    for name in ("api", "telegram"):
        previous, current = records[name], journal["before"]["containers"][name]
        if (previous is None) != (current is None):
            _fail("full successor installation differs")
        if previous is not None and not transaction._same_container_binding(previous, current):
            _fail("full successor container binding differs")
    source = journal["source"]
    recovery._load_packager(CODE_ROOT / "scripts/package_recovery_source.py")
    recovery._verify_clean_source(root, source["commit"], source["tree"])
    if runtime.tool_bytes_identity(CODE_ROOT) != source["tool_bytes_sha256"]:
        _fail("full successor source tools changed")
    transaction._verify_saved_source(root, source)
    if recovery._verify_bound_compose(root, source) != compose_path:
        _fail("full successor Compose identity differs")
    if manifest.read_manifest(root, old["deployment_id"]) != old:
        _fail("full successor prior manifest differs")
    old_db = journal["database"]["old"]
    if (manifest.verify_database_binding(old_db["binding"]) != old_db["stat"]
            or full_db.permissions(Path(old_db["binding"]["path"])) != old_db["permissions"]):
        _fail("full successor database preimage differs")
    candidate = journal["database"]["candidate"]
    if run_verifier:
        full_db.verify_candidate(root, candidate, journal["transaction_id"])
    else:
        full_db._candidate_record(root, candidate, journal["transaction_id"])
    transaction._verify_old_topology(root, journal, compose_path=compose_path, predecessor_raw=predecessor_raw,
                                     allow_probe=False, database_binding_override=old_db["binding"])
    if "api" in journal["selection"]:
        transaction._verify_candidate(root, journal)
    if "telegram" in journal["selection"]:
        transaction._telegram_receipt(root, journal)
    transaction._require_target_manifest_absent(root, journal)
    transaction._exact_pointers(root, journal, "old")
    context = copy.deepcopy(journal)
    context["database"] = {"binding": old_db["binding"], "stat": old_db["stat"]}
    context["migrations"] = []
    transaction._verify_live_topology(root, context, which="before", compose_path=compose_path,
                                      full4_endpoints=journal["schema_version"] == FULL4_SCHEMA_VERSION)


def _publish_initial_full_journal(directory: int, raw: bytes) -> None:
    """Publish a new FULL marker with one link and no parsed temporary."""
    manifest._publish_unnamed(directory, JOURNAL_NAME, raw, mode=0o600)


def write_initial_journal(root: Path, successor: dict[str, Any], *, predecessor_raw: bytes,
                          compose_path: Path) -> None:
    """Publish a FULL3 successor and retire only its exact terminal marker."""
    import component_transaction as transaction  # noqa: PLC0415
    import deployment_admission as admission  # noqa: PLC0415

    try:
        root = admission._physical_root(root)
        if root != CODE_ROOT:
            _fail("full successor source and deployment roots differ")
        admission.require_lock(root)
        journal = validate_journal(copy.deepcopy(successor))
        admission._component_conflicts(root)
        terminal_verify = (transaction.verify_terminal_for_full4
                           if journal["schema_version"] in (FULL4_SCHEMA_VERSION, SCHEMA_VERSION)
                           else transaction.verify_terminal)
        terminal = terminal_verify(root, compose_path=compose_path)
        if terminal is None or terminal[0] != predecessor_raw:
            _fail("full terminal predecessor changed")
        previous = terminal[1]
        transaction._predecessor_health(root, previous)
        checked = terminal_verify(root, compose_path=compose_path)
        if checked is None or checked[0] != predecessor_raw:
            _fail("full terminal changed during readiness")
        _verify_initial_preimage(root, journal, previous, predecessor_raw, compose_path=compose_path, run_verifier=True)
        raw = manifest._encode(journal)
        previous_kind = "full" if previous["kind"] == "component-full" else "runtime"
        previous_name = JOURNAL_NAME if previous_kind == "full" else runtime.JOURNAL_NAME
        with manifest._directory(root, (".deployments",), create=False) as directory:
            with manifest._directory(root, (".deployments", f"component-{previous_kind}-history"), create=True) as history:
                manifest._write_file(history, previous["transaction_id"] + ".json", predecessor_raw, reuse=True)
            checked = terminal_verify(root, compose_path=compose_path)
            if checked is None or checked[0] != predecessor_raw:
                _fail("full terminal changed after archival")
            _verify_initial_preimage(root, journal, previous, predecessor_raw, compose_path=compose_path, run_verifier=False)
            if runtime._read_regular(root / ".deployments" / previous_name) != predecessor_raw:
                _fail("full predecessor changed before publication")
            if previous_kind == "runtime":
                _publish_initial_full_journal(directory, raw)
            else:
                temporary = ".component-full-journal-" + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if runtime._read_regular(root / ".deployments" / JOURNAL_NAME) != predecessor_raw:
                        _fail("full predecessor changed during replacement")
                    os.replace(temporary, JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            if runtime._read_regular(root / ".deployments" / JOURNAL_NAME) != raw:
                _fail("full successor readback differs")
            if previous_kind == "runtime":
                verify_handoff(root, successor_kind="full", successor=journal,
                               predecessor=previous, predecessor_raw=predecessor_raw)
                if runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME) != predecessor_raw:
                    _fail("runtime predecessor changed before handoff removal")
                os.unlink(runtime.JOURNAL_NAME, dir_fd=directory)
                os.fsync(directory)
        if runtime._read_regular(root / ".deployments" / JOURNAL_NAME) != raw:
            _fail("full successor changed after publication")
    except FullError:
        raise
    except Exception as exc:
        raise FullError("full successor journal could not be published") from exc


def _admit_current(root: Path, identifier: str, source_id: str) -> tuple[bytes, dict[str, Any]]:
    import deployment_admission as admission  # noqa: PLC0415

    admission.require_lock(root)
    if root != CODE_ROOT:
        journal = admission.admit_full_recovery(root, transaction_id=identifier,
                                                source_tool_sha256=source_id, code_root=CODE_ROOT)
        raw = runtime._read_regular(root / ".deployments" / JOURNAL_NAME)
        if runtime._decode(raw) != journal:
            _fail("full journal changed after recovery admission")
    else:
        path = root / ".deployments" / JOURNAL_NAME
        raw = runtime._read_regular(path)
        journal = validate_journal(runtime._decode(raw))
        source = journal["source"]
        if journal["transaction_id"] != identifier or source["source_tool_sha256"] != source_id:
            _fail("full journal identity differs")
        recovery._load_packager(root / "scripts/package_recovery_source.py")
        recovery._verify_clean_source(root, source["commit"], source["tree"])
        if runtime.tool_bytes_identity(root) != source["tool_bytes_sha256"]:
            _fail("full journal source tools differ")
        recovery._verify_bootstrap(root, source)
        recovery._verify_capsule(root, source)
        recovery._verify_bound_compose(root, source)
        verify_predecessor(root, successor_kind="full", successor=journal)
        admission._component_conflicts(root, allowed_full_id=identifier)
        if runtime._read_regular(path) != raw:
            _fail("full journal changed during admission")
    if admission._present(root / ".deployments" / runtime.JOURNAL_NAME):
        _fail("full predecessor handoff requires recovery before mutation")
    for name, pattern in ((".deploy_commit", runtime._HEX40), (".deploy_current", runtime._DEPLOYMENT_ID)):
        current = runtime._read_pointer(root, name, pattern)
        actual = {"present": current is not None, "bytes": None if current is None else current + "\n"}
        if actual not in (journal["pointers"]["old"][name], journal["pointers"]["target"][name]):
            _fail("full transaction pointer drift")
    return raw, journal


def _advance_migrations_v4(previous: dict[str, Any], current: dict[str, Any]) -> None:
    if not previous["items"]:
        return
    old_items = {item["kind"]: item for item in previous["items"]}
    if set(old_items) != {item["kind"] for item in current["items"]}:
        _fail("full journal migration kinds changed")
    for item in current["items"]:
        old = old_items[item["kind"]]
        for key in ("kind", "action", "old_endpoint", "new_endpoint", "legacy_endpoint"):
            if old[key] != item[key]:
                _fail("full journal endpoint binding changed")
        for key in ("quiescent_old_endpoint", "quiescent_new_endpoint", "quiescent_legacy_endpoint",
                    "retained_reply_proof", "forward_old_readback", "progressed_endpoint"):
            if old[key] is not None and old[key] != item[key]:
                _fail("full journal endpoint observation changed")
        for key in ("compatibility", "rollback_compatibility"):
            prior, current_evidence = old[key], item[key]
            if prior is None:
                if current_evidence is not None and current_evidence["status"] != "pending":
                    _fail("native comparison must begin with a durable clock")
            elif (current_evidence is None or any(prior[field] != current_evidence[field]
                  for field in ("raw_sha256", "old_writeable", "clock"))
                  or (prior["status"] == "complete" and prior != current_evidence)):
                _fail("native comparison binding changed")
        phases = ("intent", "reserved", "writing", "staged", "publish-intent", "published")
        for key in ("forward", "rollback"):
            prior, next_copy = old[key], item[key]
            if prior is None:
                if next_copy is not None and next_copy["phase"] != "intent":
                    _fail("full copy must begin with durable intent")
                continue
            if next_copy is None or prior["binding"] != next_copy["binding"]:
                _fail("full copy binding changed")
            old_index, new_index = phases.index(prior["phase"]), phases.index(next_copy["phase"])
            if (new_index not in (old_index, old_index + 1)
                    or prior["discard"] is not None and new_index != old_index):
                _fail("full copy progress skipped or regressed")
            for field in ("stage_before", "reservation", "staged", "published"):
                if prior[field] is not None and prior[field] != next_copy[field]:
                    _fail("full copy durable observation changed")
            old_discard, new_discard = prior["discard"], next_copy["discard"]
            if old_discard is None:
                if new_discard is not None and new_discard["phase"] != "intent":
                    _fail("full discard must begin with durable intent")
            elif (new_discard is None or old_discard["copy_phase"] != new_discard["copy_phase"]
                  or (old_discard["phase"], new_discard["phase"]) not in (
                      ("intent", "intent"), ("intent", "removed"), ("removed", "removed"))
                  or old_discard["stage_after"] is not None and old_discard["stage_after"] != new_discard["stage_after"]):
                _fail("full discard progress changed")
        prior, cleanup = old["cleanup"], item["cleanup"]
        if prior is None:
            if cleanup is not None and cleanup["phase"] != "intent":
                _fail("full cleanup must begin with durable intent")
        else:
            if cleanup is None or any(prior[field] != cleanup[field] for field in (
                    "created_entry_claim_sha256", "post_stop_new_endpoint", "old_published_endpoint")):
                _fail("full cleanup binding changed")
            if prior["removed_endpoint"] is not None and prior["removed_endpoint"] != cleanup["removed_endpoint"]:
                _fail("full cleanup readback changed")
            if (prior["phase"], cleanup["phase"]) not in (("intent", "intent"), ("intent", "removed"),
                                                         ("removed", "removed")):
                _fail("full cleanup progress regressed")


def _advance_migrations_v5(previous: dict[str, Any], current: dict[str, Any]) -> None:
    """Allow only evidence to appear; never a path, action or copy claim."""
    if not previous["items"]:
        return
    previous_items = {item["kind"]: item for item in previous["items"]}
    if set(previous_items) != {item["kind"] for item in current["items"]}:
        _fail("full journal migration kinds changed")
    for item in current["items"]:
        old_item = previous_items[item["kind"]]
        for key in ("kind", "action", "old_effective_path", "new_effective_path", "old_target_path",
                    "new_target_path", "old_legacy_path", "new_legacy_path"):
            if old_item[key] != item[key]:
                _fail("full journal migration path binding changed")
        for key in ("old_preimage", "new_preimage", "legacy_preimage", "compatibility", "rollback_compatibility"):
            if old_item[key] is not None and old_item[key] != item[key]:
                _fail("full journal migration observation changed")


def _write_journal(root: Path, value: dict[str, Any]) -> None:
    """Durably advance only one bound full journal under its existing FD9."""
    root = Path(root)
    journal = validate_journal(copy.deepcopy(value))
    raw_before, previous = _admit_current(root, journal["transaction_id"], journal["source"]["source_tool_sha256"])
    if previous["status"] in ("committed", "rolled_back"):
        _fail("terminal full transaction is immutable")
    old_phase, new_phase = previous["phase"], journal["phase"]
    forward = old_phase in _FORWARD and new_phase in _FORWARD and (
        _FORWARD.index(new_phase) in (_FORWARD.index(old_phase), _FORWARD.index(old_phase) + 1))
    rollback = old_phase in _ROLLBACK and new_phase in _ROLLBACK and (
        _ROLLBACK.index(new_phase) in (_ROLLBACK.index(old_phase), _ROLLBACK.index(old_phase) + 1))
    untouched = ((old_phase in ("prepared", "state-plan", "backup") and new_phase == "rollback-untouched")
                 or (old_phase == "rollback-untouched" and new_phase in ("rollback-untouched", "rolled-back")))
    if not (forward or rollback or untouched or new_phase == "rollback-stop-readers" or new_phase == "unresolved"):
        _fail("full journal phase transition is invalid")
    for key in ("schema_version", "kind", "transaction_id", "action", "source", "predecessor", "selection",
                "before", "api_identity", "pointers", "manifest"):
        if previous[key] != journal[key]:
            _fail("full journal immutable binding changed")
    if previous["target"]["manifest"] != journal["target"]["manifest"]:
        _fail("full journal target manifest changed")
    for name in ("api", "telegram"):
        old_record, new_record = previous["target"]["containers"][name], journal["target"]["containers"][name]
        if old_record is None:
            if new_record is not None:
                _fail("full journal installed an absent component")
            continue
        if new_record is None:
            _fail("full journal removed a component record")
        if old_record["id"] is not None and new_record["id"] != old_record["id"]:
            _fail("full journal target container ID changed")
        if (old_record["id"] is None and new_record["id"] is not None
                and new_phase not in ("activate-readers", "rollback-stop-readers")):
            _fail("full journal target appeared outside activation")
    old_db, new_db = previous["database"], journal["database"]
    if old_db["old"] != new_db["old"] or old_db["candidate"] != new_db["candidate"]:
        _fail("full journal database preimage changed")
    for name in ("backup", "stage", "restore"):
        for key in ("path", "sha256", "permissions"):
            if old_db[name][key] != new_db[name][key]:
                _fail("full journal database copy binding changed")
        if old_db[name]["stat"] is not None and old_db[name]["stat"] != new_db[name]["stat"]:
            _fail("full journal database copy observation changed")
    for name in ("promoted_stat", "restored_stat"):
        if old_db[name] is not None and old_db[name] != new_db[name]:
            _fail("full journal database observation changed")
    old_migrations, new_migrations = previous["migrations"], journal["migrations"]
    allowed_migration_status = {
        "not-applicable": {"not-applicable"}, "pending": {"pending", "planned"},
        "planned": {"planned", "applied", "copyback", "restored"}, "applied": {"applied", "copyback", "restored"},
        "copyback": {"copyback", "restored"}, "restored": {"restored"},
    }
    if new_migrations["status"] not in allowed_migration_status[old_migrations["status"]]:
        _fail("full journal migration transition is invalid")
    if journal["schema_version"] == FULL4_SCHEMA_VERSION:
        _advance_migrations_v4(old_migrations, new_migrations)
    elif journal["schema_version"] == SCHEMA_VERSION:
        if new_migrations["status"] == "copyback":
            _fail("full journal: schema-5 transactions never copy state back")
        _advance_migrations_v5(old_migrations, new_migrations)
    elif old_migrations["items"]:
        previous_items = {item["kind"]: item for item in old_migrations["items"]}
        if set(previous_items) != {item["kind"] for item in new_migrations["items"]}:
            _fail("full journal migration kinds changed")
        for item in new_migrations["items"]:
            old_item = previous_items[item["kind"]]
            for key in ("kind", "action", "old_effective_path", "new_effective_path", "old_target_path",
                        "new_target_path", "old_legacy_path", "new_legacy_path", "forward_stage"):
                if old_item[key] != item[key]:
                    _fail("full journal migration path binding changed")
            for key in ("old_preimage", "new_preimage", "forward_stage_preimage", "created", "compatibility", "rollback_compatibility"):
                if old_item[key] is not None and old_item[key] != item[key]:
                    _fail("full journal migration observation changed")
            old_copyback, new_copyback = old_item["copyback"], item["copyback"]
            if old_copyback is not None:
                if new_copyback is None:
                    _fail("full journal copy-back intent disappeared")
                for key in ("source", "destination", "stage"):
                    if old_copyback[key] != new_copyback[key]:
                        _fail("full journal copy-back preimage changed")
                phases = ("intent", "published", "cleaned")
                if phases.index(new_copyback["phase"]) not in (
                    phases.index(old_copyback["phase"]), phases.index(old_copyback["phase"]) + 1
                ) or old_copyback["published"] is not None and old_copyback["published"] != new_copyback["published"]:
                    _fail("full journal copy-back observation changed")
                if old_copyback["stage_preimage"] is not None and old_copyback["stage_preimage"] != new_copyback["stage_preimage"]:
                    _fail("full journal copy-back stage observation changed")
    raw = manifest._encode(journal)
    with manifest._directory(root, (".deployments",), create=False) as directory:
        temporary = ".component-full-journal-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if runtime._read_regular(root / ".deployments" / JOURNAL_NAME) != raw_before:
                _fail("full journal changed before publication")
            os.replace(temporary, JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    if runtime._read_regular(root / ".deployments" / JOURNAL_NAME) != raw:
        _fail("full journal readback differs")


def _step(root: Path, journal: dict[str, Any], phase: str, *, status: str = "active") -> None:
    journal.update(phase=phase, status=status)
    _write_journal(root, journal)


def _same_renamed_file(before: dict[str, int], after: dict[str, int]) -> bool:
    # Linux may update ctime on rename; the inode, size and mtime remain bound.
    return all(before[key] == after[key] for key in ("device", "inode", "size", "mtime_ns"))


def classify_database(journal: dict[str, Any]) -> tuple[str, dict[str, int] | None]:
    """Classify only the journal's old, promoted, restored or absent DB."""
    import component_full_db as full_db  # noqa: PLC0415

    journal = validate_journal(journal)
    database = journal["database"]
    old = database["old"]
    live = Path(old["binding"]["path"])
    try:
        live.lstat()
    except FileNotFoundError:
        return "absent", None
    try:
        actual, db_stat = manifest.read_database_snapshot(live)
        if full_db.permissions(live) != old["permissions"]:
            _fail("full database ownership or mode changed")
        expected_new = journal["target"]["manifest"]["database"]
        is_old, is_new = actual == old["binding"], actual == expected_new
        if is_old and db_stat == old["stat"]:
            return "old", db_stat
        if is_old and database["restored_stat"] == db_stat:
            return "restored", db_stat
        if (is_old and database["restore"]["stat"] is not None
                and _same_renamed_file(database["restore"]["stat"], db_stat)):
            return "restored", db_stat
        if is_new and database["promoted_stat"] == db_stat:
            return "new", db_stat
        if is_new and database["stage"]["stat"] is not None and _same_renamed_file(database["stage"]["stat"], db_stat):
            return "new", db_stat
        _fail("full database contents or inode are not a recorded value")
    except FullError:
        raise
    except (manifest.ManifestError, full_db.FullDatabaseError, OSError, ValueError, TypeError) as exc:
        raise FullError("full database cannot be classified safely") from exc


def _sync_observed_database(journal: dict[str, Any], expected_state: str,
                            observed: dict[str, int] | None) -> dict[str, int]:
    """Finish a possibly interrupted live-DB rename before accepting it."""
    if expected_state not in ("new", "restored") or observed is None:
        _fail("database sync requires a recorded renamed inode")
    live = Path(journal["database"]["old"]["binding"]["path"])
    try:
        with manifest._directory(live.parent, (), create=False) as directory:
            manifest._reject_sidecars(directory, live.name)
            before = os.stat(live.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or manifest._db_stat(before) != observed:
                _fail("live database changed before directory sync")
            os.fsync(directory)
            after = os.stat(live.name, dir_fd=directory, follow_symlinks=False)
            manifest._reject_sidecars(directory, live.name)
            if manifest._db_stat(after) != observed:
                _fail("live database changed during directory sync")
        state, checked = classify_database(journal)
        if state != expected_state or checked != observed:
            _fail("live database changed after directory sync")
        return checked
    except FullError:
        raise
    except (manifest.ManifestError, OSError, ValueError, TypeError) as exc:
        raise FullError("live database directory could not be synced") from exc


def _verify_backup(root: Path, journal: dict[str, Any]) -> Path:
    import component_full_db as full_db  # noqa: PLC0415

    old, backup = journal["database"]["old"], journal["database"]["backup"]
    if backup["stat"] is None:
        _fail("full database backup was not durably recorded")
    path = root / backup["path"]
    binding = {**old["binding"], "path": str(path)}
    if (manifest.verify_database_binding(binding) != backup["stat"]
            or full_db.permissions(path) != old["permissions"]):
        _fail("full database backup differs")
    return path


def _readers(root: Path, journal: dict[str, Any], *, compose_path: Path) -> dict[str, dict[str, Any]]:
    """Reconcile every installed reader and only its owned selected target."""
    import component_observe as observe  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    key = os.urandom(32)
    result: dict[str, dict[str, Any]] = {}
    for name in ("api", "telegram"):
        old_component = journal["before"]["topology"][name]
        if old_component is None:
            continue
        before = journal["before"]["containers"][name]
        old_snapshot = transaction._snapshot(root, old_component)
        old = transaction._observed(before["id"], old_snapshot, key)
        transaction._recipe_matches(old, old_snapshot)
        actual = old["container"]
        for field in ("id", "image_id", "project", "service", "transaction_label", "restart_count"):
            if actual[field] != before[field]:
                _fail("full reader identity changed")
        allowed_names = {before["name"]}
        if name in journal["selection"]:
            allowed_names.add(transaction._retained_name(journal, name))
        if actual["name"] not in allowed_names or (not before["running"] and actual["running"]):
            _fail("full reader name or running state is not recorded")
        may_restart = (journal["phase"].startswith("rollback-") or journal["phase"] == "rolled-back"
                       or name not in journal["selection"] and journal["phase"] in (
                           "activate-readers", "actual-health-bindings", "immutable-manifest",
                           "deploy-commit", "deploy-current", "committed"))
        if actual["started_at"] != before["started_at"] and not may_restart:
            _fail("full reader started outside its recorded transition")
        old_image = transaction._image(old_snapshot["image_id"])
        if (old_image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != old_component["image"]["revision"]:
            _fail("full reader image revision differs")
        if (transaction._effective_environment(old_snapshot, old_image, compose_path, root) !=
                transaction._env_map(observe._inspect(before["id"])["Config"].get("Env"))):
            _fail("full reader environment differs")
        target = None
        target_snapshot = None
        if name in journal["selection"]:
            target_component = journal["target"]["manifest"]["components"][name]
            target_snapshot = transaction._snapshot(root, target_component)
            old_scope = transaction._inventory(old_snapshot["project"], old_snapshot["service"])
            new_scope = (transaction._inventory(target_snapshot["project"], target_snapshot["service"])
                         if (old_snapshot["project"], old_snapshot["service"]) !=
                         (target_snapshot["project"], target_snapshot["service"]) else old_scope)
            candidates = []
            for item in new_scope:
                if item.get("Id") == before["id"]:
                    continue
                labels = item.get("Labels") or {}
                if type(labels) is not dict or labels.get(observe.TRANSACTION_LABEL) != journal["transaction_id"]:
                    _fail("full target inventory contains an unknown container")
                candidates.append(item.get("Id"))
            if len(candidates) > 1 or any(item.get("Id") not in {before["id"], *candidates} for item in old_scope):
                _fail("full reader inventory is ambiguous")
            recorded = journal["target"]["containers"][name]
            if recorded["id"] is not None and candidates and recorded["id"] != candidates[0]:
                _fail("full target container identity changed")
            if recorded["id"] is not None and not candidates and journal["phase"] not in (
                "rollback-containers", "rollback-pointers", "rolled-back",
            ):
                _fail("full target container disappeared")
            if candidates:
                target = transaction._observed(candidates[0], target_snapshot, key)
                transaction._recipe_matches(target, target_snapshot)
                target_record = target["container"]
                target_image = transaction._image(target_snapshot["image_id"])
                if ((target_image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
                        != target_component["image"]["revision"]):
                    _fail("full target image revision differs")
                expected_env = transaction._effective_environment(target_snapshot, target_image, compose_path, root)
                if (target_record["name"] != transaction._target_name(journal, target_snapshot, name)
                        or target_record["transaction_label"] != journal["transaction_id"]
                        or (not before["running"] and target_record["running"])
                        or transaction._env_map(observe._inspect(target_record["id"])["Config"].get("Env")) != expected_env):
                    _fail("full target container binding differs")
                if recorded["id"] is None and target_record["running"]:
                    _fail("unrecorded full target is already running")
                if (recorded["started_at"] is not None and not journal["phase"].startswith("rollback-")
                        and (target_record["started_at"] != recorded["started_at"]
                             or target_record["restart_count"] != recorded["restart_count"])):
                    _fail("full target restarted outside its recorded transition")
                if actual["running"] and target_record["running"]:
                    _fail("old and target readers are both running")
        else:
            scope = transaction._inventory(old_snapshot["project"], old_snapshot["service"])
            if {item.get("Id") for item in scope} != {before["id"]}:
                _fail("unselected full reader inventory changed")
        for observation, snapshot in ((old, old_snapshot), (target, target_snapshot)):
            if observation is None:
                continue
            db_path = observation["path_settings"]["WUWATERM_DB_PATH"]
            if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
                db_path = snapshot["command"][1]
            if db_path is None:
                _fail("full reader database path is missing")
            live = journal["database"]["old"]["binding"]["path"]
            try:
                mapped = observe.resolve_bind_path(observation["recipe"], db_path, writable=False)
            except observe.ObservationError:
                # A missing, recorded live file may be restored from backup.
                # Only the exact file-bind source is allowed to be absent;
                # all ancestors and all other mounts remain physical.
                matches = [mount for mount in observation["recipe"]["mounts"]
                           if mount.get("host") == live and mount.get("container") == db_path]
                if len(matches) != 1 or classify_database(journal)[0] != "absent":
                    raise
                with manifest._directory(Path(live).parent, (), create=False):
                    pass
                mapped = live
            if mapped != live:
                _fail("full reader database binding changed")
            if name == "api" and journal["api_identity"] is not None:
                settings = observation["path_settings"]
                device = settings["WUWATERM_API_DEVICE_DB_PATH"]
                if device is None and settings["WUWATERM_API_STATE_DIR"] is not None:
                    device = settings["WUWATERM_API_STATE_DIR"].rstrip("/") + "/devices.db"
                if (device is None or observe.resolve_bind_path(observation["recipe"], device, writable=True)
                        != journal["api_identity"]["path"]):
                    _fail("full API identity binding changed")
        result[name] = {"old": old, "old_snapshot": old_snapshot, "target": target,
                        "target_snapshot": target_snapshot}
    identity = journal["api_identity"]
    if identity is not None:
        with manifest._directory(Path(identity["path"]).parent, (), create=False):
            pass
        info = Path(identity["path"]).stat(follow_symlinks=False)
        import stat as stat_module  # noqa: PLC0415
        if not stat_module.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
            _fail("full API identity store changed")
    return result


def _all_readers_stopped(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_observe as observe  # noqa: PLC0415

    readers = _readers(root, journal, compose_path=compose_path)
    for item in readers.values():
        for observed in (item["old"], item["target"]):
            if observed is None:
                continue
            state = observe._inspect(observed["container"]["id"])["State"]
            if (state.get("Running") is not False or state.get("Status") not in ("created", "exited")
                    or state.get("Dead") or state.get("Restarting")):
                _fail("all database readers must be stopped")


def _guard_readers(root: Path, journal: dict[str, Any], *, compose_path: Path,
                   allowed_database_states: set[str]) -> dict[str, dict[str, Any]]:
    import component_transaction as transaction  # noqa: PLC0415

    readers = _readers(root, journal, compose_path=compose_path)
    state, _ = classify_database(journal)
    if state not in allowed_database_states:
        _fail("full database is outside the allowed transition state")
    if journal["database"]["backup"]["stat"] is not None:
        _verify_backup(root, journal)
    elif state != "old":
        _fail("full transition requires the recorded backup")
    transaction._check_pointers(root, journal)
    return readers


def _state_plans(root: Path, journal: dict[str, Any], *, compose_path: Path) -> tuple[dict, dict] | None:
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    old = journal["before"]["topology"]["telegram"]
    if old is None:
        return None
    target = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, old)
    target_snapshot = transaction._snapshot(root, target)
    old_image = transaction._image(old_snapshot["image_id"])
    target_image = transaction._image(target_snapshot["image_id"])
    old_env = transaction._effective_environment(old_snapshot, old_image, compose_path, root)
    target_env = transaction._effective_environment(target_snapshot, target_image, compose_path, root)
    old_digest = old["provenance"].get("build_receipt_sha256")
    target_digest = target["provenance"].get("build_receipt_sha256")
    db_path = journal["database"]["old"]["binding"]["path"]
    old_plan = state.plan(root, receipt_sha256=old_digest, snapshot=old_snapshot, environment=old_env,
                          database_path=db_path, transaction_id=journal["transaction_id"])
    new_plan = state.plan(root, receipt_sha256=target_digest, snapshot=target_snapshot, environment=target_env,
                          database_path=db_path, transaction_id=journal["transaction_id"])
    return old_plan, new_plan


def _plan_state(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    if journal["schema_version"] == FULL4_SCHEMA_VERSION:
        _plan_state_v4(root, journal, compose_path=compose_path)
        return
    if journal["schema_version"] == SCHEMA_VERSION:
        _plan_state_v5(root, journal, compose_path=compose_path)
        return
    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    items = []
    for kind in ("chat-settings", "channel-replies"):
        old, new = old_plan["kinds"][kind], new_plan["kinds"][kind]
        if old["migration"] == "copy-if-absent":
            _fail("existing Telegram image would perform an unrecorded startup copy")
        relocating = old["mapped_target"] != new["mapped_target"]
        if relocating and (new["migration"] != "copy-if-absent" or new["mapped_legacy"] != old["mapped_target"]):
            _fail("Telegram state relocation is not the product's approved legacy copy")
        if not relocating and new["migration"] == "copy-if-absent":
            _fail("Telegram state copy is not a relocation")
        stage = (str(Path(new["mapped_target"]).parent /
                     f".component-full-{journal['transaction_id']}-{kind}.forward") if relocating else None)
        items.append({
            "kind": kind, "action": new["migration"], "old_effective_path": old["effective_path"],
            "new_effective_path": new["effective_path"], "old_target_path": old["mapped_target"],
            "new_target_path": new["mapped_target"], "old_legacy_path": old["mapped_legacy"],
            "new_legacy_path": new["mapped_legacy"], "old_preimage": None, "new_preimage": None,
            "forward_stage": stage, "forward_stage_preimage": None,
            "created": None, "compatibility": None, "rollback_compatibility": None, "copyback": None,
        })
    journal["migrations"] = {"status": "planned", "items": items}
    _write_journal(root, journal)


def _plan_state_v4(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    before = journal["before"]["topology"]["telegram"]
    target = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot, new_snapshot = transaction._snapshot(root, before), transaction._snapshot(root, target)
    old_env = transaction._effective_environment(old_snapshot, transaction._image(old_snapshot["image_id"]),
                                                 compose_path, root)
    new_env = transaction._effective_environment(new_snapshot, transaction._image(new_snapshot["image_id"]),
                                                 compose_path, root)
    identifier = journal["transaction_id"]
    items = []
    for kind in ("chat-settings", "channel-replies"):
        old, new = old_plan["kinds"][kind], new_plan["kinds"][kind]
        if old["migration"] == "copy-if-absent":
            _fail("existing Telegram image would perform an unrecorded startup copy")
        old_endpoint = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=kind,
                                      effective_path=old["effective_path"], purpose="target", transaction_id=identifier)
        new_endpoint = state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                                      effective_path=new["effective_path"], purpose="target", transaction_id=identifier)
        legacy_endpoint = (None if new["mapped_legacy"] is None else
                           state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                                          effective_path=new["legacy_path"], purpose="legacy", transaction_id=identifier))
        items.append({"kind": kind, "action": new["migration"], "old_endpoint": old_endpoint,
                      "new_endpoint": new_endpoint, "legacy_endpoint": legacy_endpoint,
                      "quiescent_old_endpoint": None, "quiescent_new_endpoint": None,
                      "quiescent_legacy_endpoint": None, "retained_reply_proof": None, "compatibility": None,
                      "forward": None, "forward_old_readback": None,
                      "progressed_endpoint": None, "rollback_compatibility": None, "rollback": None,
                      "cleanup": None})
    journal["migrations"] = {"status": "planned", "items": items}
    _write_journal(root, journal)


def _plan_state_v5(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    """Record the stopped preimages the product itself may progress.

    Schema 5 never moves state: both plans must name one effective location and
    the product's own startup copy is only classified, never staged here.
    """
    import component_prepare as prepare_runtime  # noqa: PLC0415

    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    try:
        prepare_runtime.verify_state_plan_equivalence(old_plan, new_plan)
    except prepare_runtime.PrepareError as exc:
        raise FullError(str(exc)) from exc
    items = []
    for kind in ("chat-settings", "channel-replies"):
        old, new = old_plan["kinds"][kind], new_plan["kinds"][kind]
        if old["mapped_target"] != new["mapped_target"] or old["effective_path"] != new["effective_path"]:
            _fail("Telegram state relocation is unsupported in schema-5 cutover")
        items.append({
            "kind": kind, "action": new["migration"], "old_effective_path": old["effective_path"],
            "new_effective_path": new["effective_path"], "old_target_path": old["mapped_target"],
            "new_target_path": new["mapped_target"], "old_legacy_path": old["mapped_legacy"],
            "new_legacy_path": new["mapped_legacy"], "old_preimage": None, "new_preimage": None,
            "legacy_preimage": None, "compatibility": None, "rollback_compatibility": None,
        })
    journal["migrations"] = {"status": "planned", "items": items}
    _write_journal(root, journal)


def _snapshot_state(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    if journal["schema_version"] == FULL4_SCHEMA_VERSION:
        _snapshot_state_v4(root, journal, compose_path=compose_path)
        return
    if journal["schema_version"] == SCHEMA_VERSION:
        _snapshot_state_v5(root, journal, compose_path=compose_path)
        return
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    before = journal["before"]["topology"]["telegram"]
    target = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, before)
    target_snapshot = transaction._snapshot(root, target)
    clock = int(time.time())
    for item in journal["migrations"]["items"]:
        old, new = old_plan["kinds"][item["kind"]], new_plan["kinds"][item["kind"]]
        if (old["mapped_target"] != item["old_target_path"] or new["mapped_target"] != item["new_target_path"]
                or old["effective_path"] != item["old_effective_path"] or new["effective_path"] != item["new_effective_path"]
                or old["mapped_legacy"] != item["old_legacy_path"] or new["mapped_legacy"] != item["new_legacy_path"]
                or old["migration"] == "copy-if-absent" or new["migration"] != item["action"]):
            _fail("Telegram state plan changed while stopping readers")
        item["old_preimage"] = state.preimage(root, image_id=before["image"]["id"],
                                               image_revision=before["image"]["revision"], snapshot=old_snapshot,
                                               host_path=item["old_target_path"], transaction_id=journal["transaction_id"])
        item["new_preimage"] = state.preimage(root, image_id=target["image"]["id"],
                                               image_revision=target["image"]["revision"], snapshot=target_snapshot,
                                               host_path=item["new_target_path"], transaction_id=journal["transaction_id"])
        relocating = item["old_target_path"] != item["new_target_path"]
        if relocating:
            if (item["action"] != "copy-if-absent" or not item["old_preimage"]["present"]
                    or item["new_preimage"]["present"] or item["new_legacy_path"] != item["old_target_path"]):
                _fail("Telegram state relocation preimages differ from the product plan")
        elif item["old_preimage"] != item["new_preimage"]:
            _fail("Telegram state differs across the retained binding")
        old_semantics = state.canonical(root, snapshot=old_snapshot, host_path=item["old_target_path"],
                                        transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock)
        new_semantics = state.canonical(root, snapshot=target_snapshot, host_path=item["old_target_path"],
                                        transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock)
        if (old_semantics["raw_sha256"] != item["old_preimage"]["sha256"]
                or new_semantics["raw_sha256"] != item["old_preimage"]["sha256"]
                or old_semantics["canonical_sha256"] != new_semantics["canonical_sha256"]
                or not old_semantics["writer_can_replace"]):
            _fail("Telegram native readers or old writer are incompatible")
        item["compatibility"] = {"raw_sha256": old_semantics["raw_sha256"],
                                 "old_canonical_sha256": old_semantics["canonical_sha256"],
                                 "new_canonical_sha256": new_semantics["canonical_sha256"],
                                 "old_writeable": True, "clock": clock}
        _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
    _write_journal(root, journal)


def _snapshot_state_v5(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    """Hash every stopped state leaf and prove both native readers agree."""
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    before = journal["before"]["topology"]["telegram"]
    target = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, before)
    target_snapshot = transaction._snapshot(root, target)
    clock = int(time.time())
    identifier = journal["transaction_id"]
    for item in journal["migrations"]["items"]:
        old, new = old_plan["kinds"][item["kind"]], new_plan["kinds"][item["kind"]]
        if (old["mapped_target"] != item["old_target_path"] or new["mapped_target"] != item["new_target_path"]
                or old["effective_path"] != item["old_effective_path"]
                or new["effective_path"] != item["new_effective_path"]
                or old["mapped_legacy"] != item["old_legacy_path"]
                or new["mapped_legacy"] != item["new_legacy_path"]
                or new["migration"] != item["action"]):
            _fail("Telegram state plan changed while stopping readers")
        if item["old_target_path"] != item["new_target_path"]:
            _fail("Telegram state relocation is unsupported in schema-5 cutover")
        item["old_preimage"] = state.preimage(root, image_id=before["image"]["id"],
                                               image_revision=before["image"]["revision"], snapshot=old_snapshot,
                                               host_path=item["old_target_path"], transaction_id=identifier)
        item["new_preimage"] = state.preimage(root, image_id=target["image"]["id"],
                                               image_revision=target["image"]["revision"], snapshot=target_snapshot,
                                               host_path=item["new_target_path"], transaction_id=identifier)
        if item["old_preimage"] != item["new_preimage"]:
            _fail("Telegram state differs across the retained binding")
        if item["action"] == "copy-if-absent":
            if item["new_preimage"]["present"] or item["new_legacy_path"] is None:
                _fail("Telegram state copy has a target file or no legacy path")
            item["legacy_preimage"] = state.preimage(
                root, image_id=before["image"]["id"], image_revision=before["image"]["revision"],
                snapshot=old_snapshot, host_path=item["new_legacy_path"], transaction_id=identifier)
            if not item["legacy_preimage"]["present"]:
                _fail("Telegram state copy has no legacy file to read")
        elif item["new_legacy_path"] is not None and item["new_legacy_path"] != item["new_target_path"]:
            item["legacy_preimage"] = state.preimage(
                root, image_id=before["image"]["id"], image_revision=before["image"]["revision"],
                snapshot=old_snapshot, host_path=item["new_legacy_path"], transaction_id=identifier)
        old_semantics = state.canonical(root, snapshot=old_snapshot, host_path=item["old_target_path"],
                                        transaction_id=identifier, state_kind=item["kind"], clock=clock)
        new_semantics = state.canonical(root, snapshot=target_snapshot, host_path=item["old_target_path"],
                                        transaction_id=identifier, state_kind=item["kind"], clock=clock)
        if (old_semantics["raw_sha256"] != item["old_preimage"]["sha256"]
                or new_semantics["raw_sha256"] != item["old_preimage"]["sha256"]
                or old_semantics["canonical_sha256"] != new_semantics["canonical_sha256"]
                or not old_semantics["writer_can_replace"]):
            _fail("Telegram native readers or old writer are incompatible")
        item["compatibility"] = {"raw_sha256": old_semantics["raw_sha256"],
                                 "old_canonical_sha256": old_semantics["canonical_sha256"],
                                 "new_canonical_sha256": new_semantics["canonical_sha256"],
                                 "old_writeable": True, "clock": clock}
        _write_journal(root, journal)
        _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})


def _snapshot_state_v4(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_state as state  # noqa: PLC0415
    import component_state_endpoint as endpoint_module  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
    _all_readers_stopped(root, journal, compose_path=compose_path)
    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    old_component = journal["before"]["topology"]["telegram"]
    new_component = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot, new_snapshot = transaction._snapshot(root, old_component), transaction._snapshot(root, new_component)
    old_env = transaction._effective_environment(old_snapshot, transaction._image(old_snapshot["image_id"]),
                                                 compose_path, root)
    new_env = transaction._effective_environment(new_snapshot, transaction._image(new_snapshot["image_id"]),
                                                 compose_path, root)
    identifier = journal["transaction_id"]

    def observe(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        kind = item["kind"]
        old = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=kind,
                             effective_path=item["old_endpoint"]["effective_path"], purpose="target",
                             transaction_id=identifier)
        new = state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                             effective_path=item["new_endpoint"]["effective_path"], purpose="target",
                             transaction_id=identifier)
        legacy = (None if item["legacy_endpoint"] is None else
                  state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                                 effective_path=item["legacy_endpoint"]["effective_path"], purpose="legacy",
                                 transaction_id=identifier))
        return old, new, legacy

    for item in journal["migrations"]["items"]:
        old_plan_item, new_plan_item = old_plan["kinds"][item["kind"]], new_plan["kinds"][item["kind"]]
        if (old_plan_item["effective_path"] != item["old_endpoint"]["effective_path"]
                or new_plan_item["effective_path"] != item["new_endpoint"]["effective_path"]
                or old_plan_item["migration"] == "copy-if-absent" or new_plan_item["migration"] != item["action"]
                or (item["legacy_endpoint"] is not None
                    and new_plan_item["legacy_path"] != item["legacy_endpoint"]["effective_path"])):
            _fail("Telegram native plan changed before quiescence")
        old, new, legacy = observe(item)
        reply_replaced = (item["kind"] == "channel-replies" and item["old_endpoint"]["entry"]["type"] == "symlink"
                          and old["entry"]["type"] == "regular")
        if reply_replaced:
            proof = _capture_retained_reply_proof(root, journal, item, snapshot=old_snapshot, environment=old_env)
            if item["retained_reply_proof"] is not None and item["retained_reply_proof"] != proof:
                _fail("retained reply proof changed after recording")
            item["retained_reply_proof"] = proof
        if (not _same_endpoint_route(item["old_endpoint"], old, allow_reply_replacement=reply_replaced)
                or not _same_endpoint_route(item["new_endpoint"], new, allow_reply_replacement=reply_replaced)):
            _fail("Telegram endpoint route changed while stopping readers")
        if legacy is not None and not _same_endpoint_route(item["legacy_endpoint"], legacy,
                                                           allow_reply_replacement=reply_replaced):
            _fail("Telegram legacy endpoint route changed while stopping readers")
        if item["action"] == "copy-if-absent":
            if (legacy is None or not _same_writer_view(old, legacy) or not legacy["referent"]["present"]
                    or new["entry"]["type"] != "absent"):
                _fail("quiescent Telegram relocation differs from its old writer")
        elif not _same_writer_view(old, new):
            _fail("quiescent retained Telegram writer differs")
        for key, value in (("quiescent_old_endpoint", old), ("quiescent_new_endpoint", new),
                           ("quiescent_legacy_endpoint", legacy)):
            if item[key] is not None and item[key] != value:
                _fail("quiescent Telegram endpoint changed after recording")
            item[key] = value
    _write_journal(root, journal)

    for item in journal["migrations"]["items"]:
        source = _compatibility_source_v4(item)
        if item["compatibility"] is None:
            item["compatibility"] = {"status": "pending", "raw_sha256": source["referent"]["sha256"],
                                     "old_canonical_sha256": None, "new_canonical_sha256": None,
                                     "old_writeable": True, "clock": int(time.time())}
            _write_journal(root, journal)
        compatibility = item["compatibility"]
        if compatibility["status"] == "complete":
            continue
        old_source = item["quiescent_old_endpoint"]
        new_source = item["quiescent_legacy_endpoint"] if item["action"] == "copy-if-absent" else item["quiescent_new_endpoint"]
        old_result = state.canonical_endpoint(root, snapshot=old_snapshot, source=old_source,
                                              writer=old_source, state_kind=item["kind"],
                                              clock=compatibility["clock"], transaction_id=identifier)
        new_result = state.canonical_endpoint(root, snapshot=new_snapshot, source=new_source,
                                              writer=item["quiescent_new_endpoint"], state_kind=item["kind"],
                                              clock=compatibility["clock"], transaction_id=identifier)
        if (old_result["raw_sha256"] != compatibility["raw_sha256"]
                or new_result["raw_sha256"] != compatibility["raw_sha256"]
                or old_result["canonical_sha256"] != new_result["canonical_sha256"]):
            _fail("Telegram native reader semantics differ")
        old, new, legacy = observe(item)
        for recorded, observed in ((item["quiescent_old_endpoint"], old), (item["quiescent_new_endpoint"], new),
                                   (item["quiescent_legacy_endpoint"], legacy)):
            if recorded is not None:
                endpoint_module.require_unchanged(recorded, observed)
        if item["retained_reply_proof"] is not None and _capture_retained_reply_proof(
                root, journal, item, snapshot=old_snapshot, environment=old_env) != item["retained_reply_proof"]:
            _fail("retained reply referent changed during native comparison")
        compatibility.update(status="complete", old_canonical_sha256=old_result["canonical_sha256"],
                             new_canonical_sha256=new_result["canonical_sha256"])
        _write_journal(root, journal)
        _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})


def _capture_retained_reply_proof(root: Path, journal: dict[str, Any], item: dict[str, Any], *,
                                  snapshot: dict[str, Any], environment: dict[str, str]) -> dict[str, Any]:
    import component_state as state  # noqa: PLC0415

    original = item["old_endpoint"]
    paths = [link["container_path"] for link in original["links"][1:]]
    captured = [state.endpoint(root, snapshot=snapshot, environment=environment, state_kind="channel-replies",
                               effective_path=path, purpose="retained", transaction_id=journal["transaction_id"])
                for path in paths]
    referent = state.endpoint(root, snapshot=snapshot, environment=environment, state_kind="channel-replies",
                              effective_path=original["referent"]["container_path"], purpose="retained",
                              transaction_id=journal["transaction_id"])
    proof = {"links": captured, "referent": referent}
    _retained_reply_proof(original, proof)
    return proof


def _verify_progressed_state(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    """Recheck latest state with both actual product readers before rollback."""
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    _guard_readers(root, journal, compose_path=compose_path,
                   allowed_database_states={"old", "new", "restored", "absent"})
    plans = _state_plans(root, journal, compose_path=compose_path)
    if plans is None:
        return
    old_plan, new_plan = plans
    old_component = journal["before"]["topology"]["telegram"]
    target_component = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, old_component)
    target_snapshot = transaction._snapshot(root, target_component)
    schema5 = journal["schema_version"] == SCHEMA_VERSION
    for item in journal["migrations"]["items"]:
        old, new = old_plan["kinds"][item["kind"]], new_plan["kinds"][item["kind"]]
        if (old["mapped_target"] != item["old_target_path"]
                or new["mapped_target"] != item["new_target_path"]
                or old["mapped_legacy"] != item["old_legacy_path"]
                or new["mapped_legacy"] != item["new_legacy_path"]
                or (not schema5 and old["migration"] == "copy-if-absent")):
            _fail("progressed Telegram state is outside the retained binding")
        if item["old_target_path"] != item["new_target_path"] and item["action"] != "copy-if-absent":
            _fail("progressed Telegram relocation was not authorized")
        if schema5:
            if item["old_target_path"] != item["new_target_path"]:
                _fail("progressed Telegram relocation was not authorized")
            if item["old_preimage"] is None and item["new_preimage"] is None:
                # The forward state snapshot never ran, so this transaction
                # made no state claim and the rollback has nothing to compare.
                continue
            # A distinct legacy file is not the transaction's to change, even
            # though the product may have read it at startup.
            if (item["legacy_preimage"] is not None and item["new_legacy_path"] is not None
                    and item["new_legacy_path"] != item["new_target_path"]
                    and transaction._state_preimage(item["new_legacy_path"], leaf=True,
                                                    recipe=old_snapshot) != item["legacy_preimage"]):
                _fail("legacy Telegram state changed during the cutover")
            leaf = transaction._preserved_leaf(item["new_target_path"])
            if not leaf["present"]:
                if item["new_preimage"]["present"]:
                    _fail("recorded Telegram state is missing")
                continue
            if leaf["link"]:
                # A reply entry the product never replaced stays a link; the
                # rollback neither recreates nor re-points it.
                continue
        elif item["copyback"] is not None and item["copyback"]["phase"] == "cleaned":
            continue
        elif item["old_target_path"] != item["new_target_path"]:
            target_preimage = state.preimage(root, image_id=target_component["image"]["id"],
                                             image_revision=target_component["image"]["revision"], snapshot=target_snapshot,
                                             host_path=item["new_target_path"], transaction_id=journal["transaction_id"])
            if not target_preimage["present"]:
                if item["copyback"] is not None and item["copyback"]["phase"] in ("published", "cleaned"):
                    continue
                if item["created"] is not None or item["copyback"] is not None:
                    _fail("recorded relocated Telegram state is missing")
                continue
        prior = item["rollback_compatibility"]
        clock = int(time.time()) if prior is None else prior["clock"]
        source_path = item["new_target_path"]
        old_result = state.canonical(root, snapshot=old_snapshot, host_path=source_path,
                                     transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock)
        new_result = state.canonical(root, snapshot=target_snapshot, host_path=source_path,
                                     transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock)
        old_destination = (old_result if source_path == item["old_target_path"] else
                           state.canonical(root, snapshot=old_snapshot, host_path=item["old_target_path"],
                                           transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock))
        if (old_result["raw_sha256"] != new_result["raw_sha256"]
                or old_result["canonical_sha256"] != new_result["canonical_sha256"]
                or not old_destination["writer_can_replace"]):
            _fail("progressed Telegram state is incompatible with its retained writer")
        evidence = {"raw_sha256": old_result["raw_sha256"], "old_canonical_sha256": old_result["canonical_sha256"],
                    "new_canonical_sha256": new_result["canonical_sha256"], "old_writeable": True, "clock": clock}
        if prior is not None and prior != evidence:
            _fail("progressed Telegram state changed after recorded compatibility")
        item["rollback_compatibility"] = evidence
        _write_journal(root, journal)
        _guard_readers(root, journal, compose_path=compose_path,
                       allowed_database_states={"old", "new", "restored", "absent"})
    if schema5 and all(_v5_rollback_evidence(item) for item in journal["migrations"]["items"]):
        # The bytes stay where the product wrote them; only the native reader
        # evidence above is recorded, and no state is restored or deleted.
        journal["migrations"]["status"] = "restored"
        _write_journal(root, journal)


def _reconcile_forward_v4(root: Path, journal: dict[str, Any], item: dict[str, Any], *,
                          snapshot: dict[str, Any], environment: dict[str, str]) -> bool:
    """Reconcile a stopped forward stage; return whether publication occurred."""
    import component_state as state  # noqa: PLC0415

    progress = item["forward"]
    if progress is None:
        return False
    if progress["phase"] == "published":
        return True
    destination = item["quiescent_new_endpoint"]
    stage_path = str(Path(destination["writer"]["container_path"]).parent / Path(progress["binding"]["stage_path"]).name)
    stage = state.endpoint(root, snapshot=snapshot, environment=environment, state_kind=item["kind"],
                           effective_path=stage_path, purpose="stage", transaction_id=journal["transaction_id"])
    if progress["phase"] == "intent":
        if stage["entry"]["type"] == "absent":
            return False
        if progress["stage_before"] is None:
            _fail("unclaimed FULL4 stage appeared before its absence was recorded")
        reserved = state.copy_v4(root, journal["transaction_id"], item["kind"], direction="forward", action="reserve")
        progress.update(phase="reserved", reservation=reserved["endpoint"])
        _write_journal(root, journal)
    elif (progress["phase"] == "publish-intent" and stage["entry"]["type"] == "absent"
          and progress["discard"] is None):
        published = state.copy_v4(root, journal["transaction_id"], item["kind"], direction="forward", action="publish")
        progress.update(phase="published", published=published["endpoint"])
        _write_journal(root, journal)
        return True
    if progress["discard"] is None:
        if stage["entry"]["type"] == "absent":
            _fail("recorded FULL4 stage disappeared without discard intent")
        progress["discard"] = {"phase": "intent", "copy_phase": progress["phase"], "stage_after": None}
        _write_journal(root, journal)
    if progress["discard"]["phase"] == "intent":
        removed = state.copy_v4(root, journal["transaction_id"], item["kind"], direction="forward", action="discard")
        progress["discard"].update(phase="removed", stage_after=removed["endpoint"])
        _write_journal(root, journal)
    return False


def _capture_interrupted_stop_v4(root: Path, journal: dict[str, Any], *, old_snapshot: dict[str, Any],
                                 new_snapshot: dict[str, Any], old_env: dict[str, str],
                                 new_env: dict[str, str]) -> None:
    """Bind post-stop paths when recovery interrupted the original stop phase."""
    import component_state as state  # noqa: PLC0415

    changed = False
    for item in journal["migrations"]["items"]:
        if item["quiescent_old_endpoint"] is not None:
            continue
        if item["forward"] is not None:
            _fail("FULL4 copy exists without its quiescent endpoints")
        identifier, kind = journal["transaction_id"], item["kind"]
        old = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=kind,
                             effective_path=item["old_endpoint"]["effective_path"], purpose="target",
                             transaction_id=identifier)
        new = state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                             effective_path=item["new_endpoint"]["effective_path"], purpose="target",
                             transaction_id=identifier)
        legacy = (None if item["legacy_endpoint"] is None else
                  state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=kind,
                                 effective_path=item["legacy_endpoint"]["effective_path"], purpose="legacy",
                                 transaction_id=identifier))
        replaced = (kind == "channel-replies" and item["old_endpoint"]["entry"]["type"] == "symlink"
                    and old["entry"]["type"] == "regular")
        item.update(quiescent_old_endpoint=old, quiescent_new_endpoint=new,
                    quiescent_legacy_endpoint=legacy,
                    retained_reply_proof=(_capture_retained_reply_proof(
                        root, journal, item, snapshot=old_snapshot, environment=old_env) if replaced else None))
        _validate_quiescent_v4(item)
        changed = True
    if changed:
        _write_journal(root, journal)


def _reconcile_state_v4(root: Path, journal: dict[str, Any], *, compose_path: Path,
                        reader_started: bool) -> None:
    """Reconcile stopped FULL4 state, including progressed relocated writers."""
    import component_state as state  # noqa: PLC0415
    import component_state_endpoint as endpoint_module  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    migrations = journal["migrations"]
    if migrations["status"] in ("not-applicable", "pending"):
        if reader_started:
            _fail("Telegram reader started before state was recorded")
        return
    if migrations["items"]:
        old_component = journal["before"]["topology"]["telegram"]
        new_component = journal["target"]["manifest"]["components"]["telegram"]
        old_snapshot, new_snapshot = transaction._snapshot(root, old_component), transaction._snapshot(root, new_component)
        old_env = transaction._effective_environment(old_snapshot, transaction._image(old_snapshot["image_id"]),
                                                     compose_path, root)
        new_env = transaction._effective_environment(new_snapshot, transaction._image(new_snapshot["image_id"]),
                                                     compose_path, root)
        identifier = journal["transaction_id"]
        if not reader_started:
            _capture_interrupted_stop_v4(root, journal, old_snapshot=old_snapshot, new_snapshot=new_snapshot,
                                         old_env=old_env, new_env=new_env)
        for item in migrations["items"]:
            old, new = item["quiescent_old_endpoint"], item["quiescent_new_endpoint"]
            published = _reconcile_forward_v4(root, journal, item, snapshot=new_snapshot, environment=new_env)
            if not reader_started and not published and item["cleanup"] is None:
                current_old = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=item["kind"],
                                             effective_path=old["effective_path"], purpose="target", transaction_id=identifier)
                endpoint_module.require_unchanged(old, current_old)
                if item["retained_reply_proof"] is not None and _capture_retained_reply_proof(
                        root, journal, item, snapshot=old_snapshot, environment=old_env) != item["retained_reply_proof"]:
                    _fail("retained reply referent changed after interrupted stop")
                continue
            if old is None or new is None:
                _fail("progressed Telegram state lacks a retained writer")
            relocating = old["writer"]["host_path"] != new["writer"]["host_path"]
            if relocating and not published:
                _fail("relocated Telegram reader started without its recorded publication")
            endpoint_module.verify_terminal_route(old_snapshot, old)
            if item["retained_reply_proof"] is not None and _capture_retained_reply_proof(
                    root, journal, item, snapshot=old_snapshot, environment=old_env) != item["retained_reply_proof"]:
                _fail("retained reply referent changed before rollback")
            if item["cleanup"] is not None:
                if item["cleanup"]["phase"] == "intent":
                    removed = state.copy_v4(root, identifier, item["kind"], direction="forward", action="cleanup")
                    if not _cleanup_removed(new, item["cleanup"]["post_stop_new_endpoint"], removed["endpoint"]):
                        _fail("FULL4 cleanup did not restore the absent entry")
                    item["cleanup"].update(phase="removed", removed_endpoint=removed["endpoint"])
                    _write_journal(root, journal)
                if item["retained_reply_proof"] is not None and _capture_retained_reply_proof(
                        root, journal, item, snapshot=old_snapshot, environment=old_env) != item["retained_reply_proof"]:
                    _fail("retained reply referent changed during cleanup recovery")
                continue
            current = state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=item["kind"],
                                     effective_path=new["effective_path"], purpose="target", transaction_id=identifier)
            if relocating and not _same_endpoint_route(item["forward"]["published"], current):
                _fail("progressed Telegram destination route changed")
            if item["progressed_endpoint"] is None:
                item["progressed_endpoint"] = current
                _write_journal(root, journal)
            else:
                endpoint_module.require_unchanged(item["progressed_endpoint"], current)
            old_current = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=item["kind"],
                                         effective_path=old["effective_path"], purpose="target", transaction_id=identifier)
            if relocating:
                rollback = item["rollback"]
                if rollback is not None and rollback["phase"] == "publish-intent":
                    stage_path = str(Path(old["writer"]["container_path"]).parent /
                                     Path(rollback["binding"]["stage_path"]).name)
                    stage = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=item["kind"],
                                           effective_path=stage_path, purpose="stage", transaction_id=identifier)
                    if stage["entry"]["type"] == "absent":
                        _all_readers_stopped(root, journal, compose_path=compose_path)
                        reconciled = state.copy_v4(root, identifier, item["kind"], direction="rollback",
                                                   action="publish", reconcile_only=True)
                        if not reconciled["reconciled"]:
                            _fail("rollback publication was not an existing rename")
                        rollback.update(phase="published", published=reconciled["endpoint"])
                        _write_journal(root, journal)
                        old_current = state.endpoint(root, snapshot=old_snapshot, environment=old_env,
                                                     state_kind=item["kind"], effective_path=old["effective_path"],
                                                     purpose="target", transaction_id=identifier)
                expected_old = (item["rollback"]["published"] if item["rollback"] is not None
                                and item["rollback"]["published"] is not None else old)
                endpoint_module.require_unchanged(expected_old, old_current)
            elif not _same_writer_view(old_current, current):
                _fail("progressed Telegram writer differs between retained and target recipes")
            if item["rollback_compatibility"] is None:
                item["rollback_compatibility"] = {"status": "pending", "raw_sha256": current["referent"]["sha256"],
                                                   "old_canonical_sha256": None, "new_canonical_sha256": None,
                                                   "old_writeable": True, "clock": int(time.time())}
                _write_journal(root, journal)
            evidence = item["rollback_compatibility"]
            if evidence["status"] != "complete":
                old_result = state.canonical_endpoint(root, snapshot=old_snapshot, source=current, writer=old,
                                                      state_kind=item["kind"], clock=evidence["clock"],
                                                      transaction_id=identifier)
                new_result = state.canonical_endpoint(root, snapshot=new_snapshot, source=current, writer=current,
                                                      state_kind=item["kind"], clock=evidence["clock"],
                                                      transaction_id=identifier)
                if (old_result["raw_sha256"] != evidence["raw_sha256"]
                        or new_result["raw_sha256"] != evidence["raw_sha256"]
                        or old_result["canonical_sha256"] != new_result["canonical_sha256"]):
                    _fail("progressed Telegram state is not compatible with its retained reader")
                after = state.endpoint(root, snapshot=new_snapshot, environment=new_env, state_kind=item["kind"],
                                       effective_path=new["effective_path"], purpose="target", transaction_id=identifier)
                endpoint_module.require_unchanged(current, after)
                endpoint_module.verify_terminal_route(old_snapshot, old)
                evidence.update(status="complete", old_canonical_sha256=old_result["canonical_sha256"],
                                new_canonical_sha256=new_result["canonical_sha256"])
                _write_journal(root, journal)
            if relocating:
                if migrations["status"] != "copyback":
                    migrations["status"] = "copyback"
                    _write_journal(root, journal)
                if item["rollback"] is not None or old["referent"]["sha256"] != current["referent"]["sha256"]:
                    _drive_copy_v4(root, journal, item, direction="rollback", compose_path=compose_path)
                    old_published = item["rollback"]["published"]
                else:
                    old_published = old
                item["cleanup"] = {"phase": "intent", "created_entry_claim_sha256": _endpoint_hash(item["forward"]["binding"]),
                                   "post_stop_new_endpoint": current, "old_published_endpoint": old_published,
                                   "removed_endpoint": None}
                _write_journal(root, journal)
                removed = state.copy_v4(root, identifier, item["kind"], direction="forward", action="cleanup")
                if not _cleanup_removed(new, current, removed["endpoint"]):
                    _fail("FULL4 cleanup did not restore the absent entry")
                item["cleanup"].update(phase="removed", removed_endpoint=removed["endpoint"])
                _write_journal(root, journal)
                if item["retained_reply_proof"] is not None and _capture_retained_reply_proof(
                        root, journal, item, snapshot=old_snapshot, environment=old_env) != item["retained_reply_proof"]:
                    _fail("retained reply referent changed during rollback")
    migrations["status"] = "restored"
    _write_journal(root, journal)


def _copyback_state(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    if journal["migrations"]["status"] in ("not-applicable", "pending"):
        return
    old_component = journal["before"]["topology"]["telegram"]
    target_component = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, old_component)
    target_snapshot = transaction._snapshot(root, target_component)
    for item in journal["migrations"]["items"]:
        if item["old_target_path"] == item["new_target_path"]:
            continue
        _guard_readers(root, journal, compose_path=compose_path,
                       allowed_database_states={"old", "new", "restored", "absent"})
        _all_readers_stopped(root, journal, compose_path=compose_path)
        current = state.preimage(root, image_id=target_component["image"]["id"],
                                 image_revision=target_component["image"]["revision"], snapshot=target_snapshot,
                                 host_path=item["new_target_path"], transaction_id=journal["transaction_id"])
        copyback = item["copyback"]
        if not current["present"]:
            if copyback is not None and copyback["phase"] == "cleaned":
                continue
            if copyback is not None and copyback["phase"] == "published":
                destination = state.preimage(root, image_id=old_component["image"]["id"],
                                             image_revision=old_component["image"]["revision"], snapshot=old_snapshot,
                                             host_path=item["old_target_path"], transaction_id=journal["transaction_id"])
                stage = state.preimage(root, image_id=old_component["image"]["id"],
                                       image_revision=old_component["image"]["revision"], snapshot=old_snapshot,
                                       host_path=copyback["stage"], transaction_id=journal["transaction_id"])
                if destination != copyback["published"] or stage["present"]:
                    _fail("state cleanup cannot be reconciled")
                copyback["phase"] = "cleaned"
                _write_journal(root, journal)
                continue
            if item["created"] is None and copyback is None:
                if item["compatibility"] is None or item["old_preimage"] is None:
                    stage = state.preimage(root, image_id=target_component["image"]["id"],
                                           image_revision=target_component["image"]["revision"], snapshot=target_snapshot,
                                           host_path=item["forward_stage"], transaction_id=journal["transaction_id"])
                    if stage["present"]:
                        _fail("unclaimed forward state stage exists")
                else:
                    state.discard_forward_stage(root, journal["transaction_id"], item["kind"])
                continue
            _fail("relocated Telegram state is missing")
        if item["created"] is None:
            stage = item["forward_stage_preimage"]
            if (stage is None or not stage["present"] or current["sha256"] != stage["sha256"]
                    or not _same_renamed_file(stage["stat"], current["stat"])):
                _fail("unrecorded Telegram target cannot be attributed to its stage")
            item["created"] = {"sha256": current["sha256"], "stat": current["stat"]}
            _write_journal(root, journal)
        compatibility = item["rollback_compatibility"]
        if compatibility is None or compatibility["raw_sha256"] != current["sha256"]:
            _fail("latest relocated Telegram state lacks native compatibility evidence")
        destination = state.preimage(root, image_id=old_component["image"]["id"],
                                     image_revision=old_component["image"]["revision"], snapshot=old_snapshot,
                                     host_path=item["old_target_path"], transaction_id=journal["transaction_id"])
        if copyback is None:
            if destination != item["old_preimage"]:
                _fail("old Telegram state changed after its quiescent preimage")
            stage_path = str(Path(item["old_target_path"]).parent /
                             f".component-full-{journal['transaction_id']}-{item['kind']}.rollback")
            copyback = {"phase": "intent", "source": current, "destination": destination,
                        "published": None, "stage": stage_path, "stage_preimage": None}
            item["copyback"] = copyback
            journal["migrations"]["status"] = "copyback"
            _write_journal(root, journal)
        elif current != copyback["source"]:
            _fail("relocated Telegram state changed during copy-back")
        if copyback["phase"] == "intent":
            if destination == copyback["destination"] and destination["sha256"] == current["sha256"]:
                copyback.update(phase="published", published=destination)
                _write_journal(root, journal)
            else:
                if copyback["stage_preimage"] is None:
                    copyback["stage_preimage"] = state.copy_stage(root, journal["transaction_id"], item["kind"],
                                                                   direction="rollback")
                    _write_journal(root, journal)
                _all_readers_stopped(root, journal, compose_path=compose_path)
                published = state.copy_publish(root, journal["transaction_id"], item["kind"], direction="rollback",
                                               stage_before=copyback["stage_preimage"])
                copyback.update(phase="published", published=published)
                _write_journal(root, journal)
        if copyback["phase"] == "published":
            _all_readers_stopped(root, journal, compose_path=compose_path)
            state.copy_cleanup(root, journal["transaction_id"], item["kind"])
            copyback["phase"] = "cleaned"
            _write_journal(root, journal)
    if journal["migrations"]["status"] != "not-applicable":
        journal["migrations"]["status"] = "restored"
        _write_journal(root, journal)


def _drive_copy_v4(root: Path, journal: dict[str, Any], item: dict[str, Any], *,
                   direction: str, compose_path: Path) -> None:
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    if direction == "forward":
        source, destination, evidence = (item["quiescent_legacy_endpoint"], item["quiescent_new_endpoint"],
                                         item["compatibility"])
        component = journal["target"]["manifest"]["components"]["telegram"]
        canonical_key = "new_canonical_sha256"
    else:
        source, destination, evidence = (item["progressed_endpoint"], item["quiescent_old_endpoint"],
                                         item["rollback_compatibility"])
        component = journal["before"]["topology"]["telegram"]
        canonical_key = "old_canonical_sha256"
    if source is None or destination is None or evidence is None or evidence["status"] != "complete":
        _fail("FULL4 copy lacks complete native compatibility")
    snapshot = transaction._snapshot(root, component)
    environment = transaction._effective_environment(snapshot, transaction._image(snapshot["image_id"]), compose_path, root)
    if item[direction] is None:
        writer = destination["writer"]
        stage = str(Path(writer["host_path"]).parent /
                    f".component-full-{journal['transaction_id']}-{item['kind']}.{direction}")
        binding = {"schema_version": 2, "operation": "state-copy", "transaction_id": journal["transaction_id"],
                   "state_kind": item["kind"], "direction": direction,
                   "source_endpoint_sha256": _endpoint_hash(source),
                   "destination_before_sha256": _endpoint_hash(destination), "image_id": destination["image_id"],
                   "image_revision": destination["image_revision"], "user": destination["user"],
                   "recipe_sha256": destination["recipe_sha256"],
                   "mounts_sha256": hashlib.sha256(manifest._encode(state._mounts(snapshot))).hexdigest(),
                   "stage_path": stage, "stage_parent_identity": writer["parent_identity"],
                   "expected_raw_sha256": source["referent"]["sha256"]}
        item[direction] = {"binding": binding, "phase": "intent", "stage_before": None,
                           "reservation": None, "staged": None, "published": None, "discard": None}
        _write_journal(root, journal)
    progress = item[direction]
    stage_path = str(Path(destination["writer"]["container_path"]).parent / Path(progress["binding"]["stage_path"]).name)
    if progress["stage_before"] is None:
        observed = state.endpoint(root, snapshot=snapshot, environment=environment, state_kind=item["kind"],
                                  effective_path=stage_path, purpose="stage", transaction_id=journal["transaction_id"])
        if observed["entry"]["type"] != "absent":
            _fail("FULL4 copy stage existed before its claim")
        progress["stage_before"] = observed
        _write_journal(root, journal)
    if progress["phase"] == "intent":
        state.create_copy_claim(root, journal["transaction_id"], item["kind"], direction)
        reserved = state.copy_v4(root, journal["transaction_id"], item["kind"], direction=direction, action="reserve")
        progress.update(phase="reserved", reservation=reserved["endpoint"])
        _write_journal(root, journal)
    if progress["phase"] == "reserved":
        progress["phase"] = "writing"
        _write_journal(root, journal)
    if progress["phase"] == "writing":
        staged = state.copy_v4(root, journal["transaction_id"], item["kind"], direction=direction, action="write")
        progress.update(phase="staged", staged=staged["endpoint"])
        _write_journal(root, journal)
    if progress["phase"] == "staged":
        semantic = state.canonical_endpoint(root, snapshot=snapshot, source=progress["staged"],
                                            writer=destination, state_kind=item["kind"], clock=evidence["clock"],
                                            transaction_id=journal["transaction_id"])
        if semantic["raw_sha256"] != evidence["raw_sha256"] or semantic["canonical_sha256"] != evidence[canonical_key]:
            _fail("FULL4 staged state differs from native reader semantics")
        progress["phase"] = "publish-intent"
        _write_journal(root, journal)
    if progress["phase"] == "publish-intent":
        _all_readers_stopped(root, journal, compose_path=compose_path)
        published = state.copy_v4(root, journal["transaction_id"], item["kind"], direction=direction, action="publish")
        progress.update(phase="published", published=published["endpoint"])
        _write_journal(root, journal)
    if direction == "forward" and progress["phase"] == "published" and item["forward_old_readback"] is None:
        import component_state_endpoint as endpoint_module  # noqa: PLC0415

        old_component = journal["before"]["topology"]["telegram"]
        old_snapshot = transaction._snapshot(root, old_component)
        old_env = transaction._effective_environment(old_snapshot, transaction._image(old_snapshot["image_id"]),
                                                     compose_path, root)
        old = item["quiescent_old_endpoint"]
        _all_readers_stopped(root, journal, compose_path=compose_path)
        state._verify_copy_source(root, journal, progress["published"], purpose="target", state_kind=item["kind"])
        observed = state.canonical_endpoint(root, snapshot=old_snapshot, source=progress["published"], writer=old,
                                            state_kind=item["kind"], clock=evidence["clock"],
                                            transaction_id=journal["transaction_id"])
        if (observed["raw_sha256"] != evidence["raw_sha256"]
                or observed["canonical_sha256"] != evidence["old_canonical_sha256"]
                or not observed["writer_can_replace"]):
            _fail("FULL4 published state is not usable by the retained reader")
        old_current = state.endpoint(root, snapshot=old_snapshot, environment=old_env, state_kind=item["kind"],
                                     effective_path=old["effective_path"], purpose="target",
                                     transaction_id=journal["transaction_id"])
        endpoint_module.require_unchanged(old, old_current)
        state._verify_copy_source(root, journal, progress["published"], purpose="target", state_kind=item["kind"])
        item["forward_old_readback"] = {"source_endpoint_sha256": _endpoint_hash(progress["published"]),
                                        "raw_sha256": observed["raw_sha256"],
                                        "canonical_sha256": observed["canonical_sha256"],
                                        "old_writeable": True, "clock": evidence["clock"]}
        _write_journal(root, journal)


def _migrate_state(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_state as state  # noqa: PLC0415

    if journal["migrations"]["status"] == "not-applicable":
        return
    if journal["schema_version"] == SCHEMA_VERSION:
        _migrate_state_v5(root, journal, compose_path=compose_path)
        return
    if journal["schema_version"] == FULL4_SCHEMA_VERSION:
        import component_state_endpoint as endpoint_module  # noqa: PLC0415
        import component_transaction as transaction  # noqa: PLC0415

        old_component = journal["before"]["topology"]["telegram"]
        new_component = journal["target"]["manifest"]["components"]["telegram"]
        old_snapshot, new_snapshot = transaction._snapshot(root, old_component), transaction._snapshot(root, new_component)
        old_env = transaction._effective_environment(old_snapshot, transaction._image(old_snapshot["image_id"]),
                                                     compose_path, root)
        new_env = transaction._effective_environment(new_snapshot, transaction._image(new_snapshot["image_id"]),
                                                     compose_path, root)
        for item in journal["migrations"]["items"]:
            if item["compatibility"] is None or item["compatibility"]["status"] != "complete":
                _fail("FULL4 native state comparison is incomplete")
            _all_readers_stopped(root, journal, compose_path=compose_path)
            if item["action"] != "copy-if-absent":
                for snapshot, environment, field in ((old_snapshot, old_env, "quiescent_old_endpoint"),
                                                      (new_snapshot, new_env, "quiescent_new_endpoint")):
                    expected = item[field]
                    current = state.endpoint(root, snapshot=snapshot, environment=environment, state_kind=item["kind"],
                                             effective_path=expected["effective_path"], purpose="target",
                                             transaction_id=journal["transaction_id"])
                    endpoint_module.require_unchanged(expected, current)
                continue
            _drive_copy_v4(root, journal, item, direction="forward", compose_path=compose_path)
        journal["migrations"]["status"] = "applied"
        _write_journal(root, journal)
        return
    for item in journal["migrations"]["items"]:
        if item["action"] != "copy-if-absent":
            continue
        _all_readers_stopped(root, journal, compose_path=compose_path)
        if classify_database(journal)[0] != "old":
            _fail("database changed before Telegram state migration")
        _verify_backup(root, journal)
        if item["forward_stage_preimage"] is None:
            item["forward_stage_preimage"] = state.copy_stage(root, journal["transaction_id"], item["kind"],
                                                               direction="forward")
            _write_journal(root, journal)
        _all_readers_stopped(root, journal, compose_path=compose_path)
        result = state.copy_publish(root, journal["transaction_id"], item["kind"], direction="forward",
                                    stage_before=item["forward_stage_preimage"])
        if item["created"] is not None and item["created"] != {"sha256": result["sha256"], "stat": result["stat"]}:
            _fail("migrated Telegram state changed")
        item["created"] = {"sha256": result["sha256"], "stat": result["stat"]}
        _write_journal(root, journal)
    journal["migrations"]["status"] = "applied"
    _write_journal(root, journal)


def _migrate_state_v5(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    """Verify the stopped leaves; the product performs any pending copy itself."""
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    before = journal["before"]["topology"]["telegram"]
    target = journal["target"]["manifest"]["components"]["telegram"]
    old_snapshot = transaction._snapshot(root, before)
    target_snapshot = transaction._snapshot(root, target)
    for item in journal["migrations"]["items"]:
        _all_readers_stopped(root, journal, compose_path=compose_path)
        if classify_database(journal)[0] != "old":
            _fail("database changed before Telegram state migration")
        _verify_backup(root, journal)
        if item["compatibility"]["old_canonical_sha256"] != item["compatibility"]["new_canonical_sha256"]:
            _fail("FULL5 native state comparison is incomplete")
        current = state.preimage(root, image_id=target["image"]["id"], image_revision=target["image"]["revision"],
                                 snapshot=target_snapshot, host_path=item["new_target_path"],
                                 transaction_id=journal["transaction_id"])
        if current != item["new_preimage"]:
            _fail("Telegram state changed while readers were stopped")
        if item["legacy_preimage"] is not None:
            legacy = state.preimage(root, image_id=before["image"]["id"],
                                    image_revision=before["image"]["revision"], snapshot=old_snapshot,
                                    host_path=item["new_legacy_path"], transaction_id=journal["transaction_id"])
            if legacy != item["legacy_preimage"]:
                _fail("Telegram legacy state changed while readers were stopped")
        if item["action"] == "copy-if-absent" and current["present"]:
            _fail("Telegram state copied before the product started")
    journal["migrations"]["status"] = "applied"
    _write_journal(root, journal)


def _prepare_backup(root: Path, journal: dict[str, Any]) -> None:
    import component_full_db as full_db  # noqa: PLC0415

    old, expected = journal["database"]["old"], journal["database"]["backup"]
    actual = full_db.prepare_backup(root, old["binding"], old["stat"], journal["transaction_id"])
    if any(actual[key] != expected[key] for key in ("path", "sha256", "permissions")):
        _fail("prepared database backup binding differs")
    if expected["stat"] is not None and expected["stat"] != actual["stat"]:
        _fail("prepared database backup observation changed")
    expected["stat"] = actual["stat"]
    _write_journal(root, journal)


def _prepare_stage(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_full_db as full_db  # noqa: PLC0415

    _all_readers_stopped(root, journal, compose_path=compose_path)
    state, _ = classify_database(journal)
    if state != "old":
        _fail("old database changed before staging")
    _verify_backup(root, journal)
    expected = journal["database"]["stage"]
    actual = full_db.prepare_stage(root, journal["database"]["candidate"],
                                   Path(journal["database"]["old"]["binding"]["path"]),
                                   journal["transaction_id"])
    if any(actual[key] != expected[key] for key in ("path", "sha256", "permissions")):
        _fail("prepared database stage binding differs")
    if expected["stat"] is not None and expected["stat"] != actual["stat"]:
        _fail("prepared database stage observation changed")
    expected["stat"] = actual["stat"]
    _write_journal(root, journal)


def _promote_database(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_full_db as full_db  # noqa: PLC0415

    _all_readers_stopped(root, journal, compose_path=compose_path)
    _verify_backup(root, journal)
    state, observed = classify_database(journal)
    if state == "new":
        observed = _sync_observed_database(journal, "new", observed)
        _all_readers_stopped(root, journal, compose_path=compose_path)
        _verify_backup(root, journal)
        if journal["database"]["promoted_stat"] is None:
            journal["database"]["promoted_stat"] = observed
            _write_journal(root, journal)
        return
    if state != "old":
        _fail("database promotion requires the recorded old preimage")
    stage = journal["database"]["stage"]
    if stage["stat"] is None:
        _fail("database promotion stage is not recorded")
    stage_path = Path(stage["path"])
    stage_binding = {**journal["database"]["candidate"]["binding"], "path": str(stage_path)}
    if (manifest.verify_database_binding(stage_binding) != stage["stat"]
            or full_db.permissions(stage_path) != stage["permissions"]):
        _fail("database promotion stage changed")
    live = Path(journal["database"]["old"]["binding"]["path"])
    _all_readers_stopped(root, journal, compose_path=compose_path)
    if classify_database(journal)[0] != "old":
        _fail("database changed immediately before promotion")
    with manifest._directory(live.parent, (), create=False) as directory:
        if stage_path.parent != live.parent:
            _fail("database stage is not in the live directory")
        manifest._reject_sidecars(directory, live.name)
        manifest._reject_sidecars(directory, stage_path.name)
        os.replace(stage_path.name, live.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    state, actual = classify_database(journal)
    if state != "new" or actual is None:
        _fail("promoted database readback differs")
    journal["database"]["promoted_stat"] = actual
    _write_journal(root, journal)


def _restore_database(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_full_db as full_db  # noqa: PLC0415

    _all_readers_stopped(root, journal, compose_path=compose_path)
    state, observed = classify_database(journal)
    if journal["database"]["backup"]["stat"] is not None:
        _verify_backup(root, journal)
    if state == "old":
        return
    if state == "restored":
        observed = _sync_observed_database(journal, "restored", observed)
        _all_readers_stopped(root, journal, compose_path=compose_path)
        if journal["database"]["backup"]["stat"] is not None:
            _verify_backup(root, journal)
        if journal["database"]["restored_stat"] is None:
            journal["database"]["restored_stat"] = observed
            _write_journal(root, journal)
        return
    if state not in ("new", "absent"):
        _fail("database restore has an unknown live preimage")
    backup_path = _verify_backup(root, journal)
    old, backup, restore = (journal["database"][key] for key in ("old", "backup", "restore"))
    backup_binding = {**old["binding"], "path": str(backup_path)}
    restore_path = Path(restore["path"])
    copied, copied_stat = full_db._copy_owned(root, journal["transaction_id"], "restore", backup_path,
                                               backup_binding, backup["stat"], backup["permissions"],
                                               restore_path, restore["permissions"], verify_candidate=False)
    if copied["sha256"] != restore["sha256"]:
        _fail("database restore stage differs")
    if restore["stat"] is not None and restore["stat"] != copied_stat:
        _fail("database restore stage observation changed")
    restore["stat"] = copied_stat
    _write_journal(root, journal)
    _all_readers_stopped(root, journal, compose_path=compose_path)
    if classify_database(journal)[0] != state:
        _fail("database changed immediately before restoration")
    _verify_backup(root, journal)
    live = Path(old["binding"]["path"])
    with manifest._directory(live.parent, (), create=False) as directory:
        if restore_path.parent != live.parent:
            _fail("database restore stage is not in the live directory")
        manifest._reject_sidecars(directory, live.name)
        manifest._reject_sidecars(directory, restore_path.name)
        os.replace(restore_path.name, live.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    restored, actual = classify_database(journal)
    if restored != "restored" or actual is None:
        _fail("restored database readback differs")
    journal["database"]["restored_stat"] = actual
    _write_journal(root, journal)


def _reader_view(root: Path, journal: dict[str, Any], observed: dict[str, Any], snapshot: dict[str, Any],
                 *, expected_binding: dict[str, Any], expected_stat: dict[str, int]) -> None:
    """Query actual running reader mounts, including same-container file binds."""
    if not observed["container"]["running"]:
        return
    path = observed["path_settings"]["WUWATERM_DB_PATH"]
    if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
        path = snapshot["command"][1]
    if path is None:
        _fail("running reader database path is missing")
    code = (
        "import hashlib,json,os,pathlib,sqlite3,sys; p=sys.argv[1]; "
        "s=os.stat(p,follow_symlinks=False); h=hashlib.sha256(); "
        "f=open(p,'rb'); [h.update(x) for x in iter(lambda:f.read(1048576),b'')]; f.close(); "
        "c=sqlite3.connect(pathlib.Path(p).as_uri()+'?mode=ro&immutable=1',uri=True); "
        "n=c.execute('select count(*) from terms').fetchone()[0]; "
        "v=c.execute(\"select value from metadata where key='schema_version'\").fetchone(); c.close(); "
        "print(json.dumps({'sha256':h.hexdigest(),'device':s.st_dev,'inode':s.st_ino,'rows':n,'schema':v[0]}))"
    )
    try:
        result = subprocess.run(
            ["docker", "exec", observed["container"]["id"], "python", "-I", "-S", "-B", "-c", code, path],
            cwd=root, env=recovery._isolated_env(), pass_fds=(9,), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        if result.returncode or len(result.stdout) > 4096:
            _fail("running reader database could not be inspected")
        value = runtime._decode(result.stdout)
        if (type(value) is not dict or set(value) != {"sha256", "device", "inode", "rows", "schema"}
                or value["sha256"] != expected_binding["sha256"] or value["device"] != expected_stat["device"]
                or value["inode"] != expected_stat["inode"] or type(value["rows"]) is not int
                or value["rows"] < 0 or value["schema"] != "2"):
            _fail("running reader observes a different database")
    except FullError:
        raise
    except (OSError, subprocess.SubprocessError, runtime.JournalError, ValueError, TypeError, KeyError) as exc:
        raise FullError("running reader database could not be verified") from exc


def _stop_readers(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_transaction as transaction  # noqa: PLC0415

    for name in ("api", "telegram"):
        readers = _guard_readers(root, journal, compose_path=compose_path,
                                  allowed_database_states={"old", "new", "restored", "absent"})
        if name not in readers:
            continue
        for which in ("target", "old"):
            observed = readers[name][which]
            if observed is not None and observed["container"]["running"]:
                transaction._container_command("POST", observed["container"]["id"], "stop?t=10")
                readers = _guard_readers(root, journal, compose_path=compose_path,
                                          allowed_database_states={"old", "new", "restored", "absent"})
    _all_readers_stopped(root, journal, compose_path=compose_path)


def _activate_readers(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_transaction as transaction  # noqa: PLC0415

    _all_readers_stopped(root, journal, compose_path=compose_path)
    for name in ("api", "telegram"):
        readers = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"new"})
        if name not in readers:
            continue
        before = journal["before"]["containers"][name]
        if name in journal["selection"]:
            target = readers[name]["target"]
            snapshot = readers[name]["target_snapshot"]
            if target is None:
                if name == "api":
                    target_id = transaction._create_target(journal, snapshot, root=root, compose_path=compose_path)
                else:
                    image = transaction._image(snapshot["image_id"])
                    environment = transaction._effective_environment(snapshot, image, compose_path, root)
                    target_id = transaction._create_from_snapshot(journal, snapshot, environment, name)
                target = _readers(root, journal, compose_path=compose_path)[name]["target"]
                if target is None or target["container"]["id"] != target_id:
                    _fail("created full target cannot be reconciled")
            journal["target"]["containers"][name]["id"] = target["container"]["id"]
            _write_journal(root, journal)
            if before["running"] and not target["container"]["running"]:
                transaction._container_command("POST", target["container"]["id"], "start")
            target = _readers(root, journal, compose_path=compose_path)[name]["target"]
            if target is None or target["container"]["running"] != before["running"]:
                _fail("full target running state differs")
            journal["target"]["containers"][name].update(target["container"])
            _write_journal(root, journal)
        else:
            old = readers[name]["old"]
            if before["running"] and not old["container"]["running"]:
                transaction._container_command("POST", old["container"]["id"], "start")
            old = _readers(root, journal, compose_path=compose_path)[name]["old"]
            if old["container"]["running"] != before["running"]:
                _fail("unselected full reader running state differs")
            journal["target"]["containers"][name].update(old["container"])
            _write_journal(root, journal)


def _verify_activated(root: Path, journal: dict[str, Any], *, compose_path: Path, restored: bool = False) -> None:
    import component_transaction as transaction  # noqa: PLC0415

    state, db_stat = classify_database(journal)
    if state not in (("old", "restored") if restored else ("new",)) or db_stat is None:
        _fail("reader database is not at its expected transaction state")
    binding = (journal["database"]["old"]["binding"] if restored else journal["target"]["manifest"]["database"])
    readers = _readers(root, journal, compose_path=compose_path)
    for name, entry in readers.items():
        before = journal["before"]["containers"][name]
        selected = name in journal["selection"] and not restored
        observed = entry["target"] if selected else entry["old"]
        snapshot = entry["target_snapshot"] if selected else entry["old_snapshot"]
        if observed is None or observed["container"]["running"] != before["running"]:
            _fail("full reader running state differs")
        _reader_view(root, journal, observed, snapshot, expected_binding=binding, expected_stat=db_stat)
        if observed["container"]["running"]:
            prior = observed["container"]
            if name == "api":
                transaction._ready(prior["id"])
            elif selected:
                transaction._telegram_ready(prior["id"], snapshot)
            again = _readers(root, journal, compose_path=compose_path)[name]["target" if selected else "old"]
            if again is None or again["container"] != prior:
                _fail("full reader restarted during readiness")
    if classify_database(journal)[1] != db_stat:
        _fail("full database changed during reader verification")


def update(root: Path, prepared: dict[str, Any], *, compose_path: Path) -> dict[str, Any]:
    """Execute a fully admitted FULL3 transaction from a prepared record."""
    import component_transaction as transaction  # noqa: PLC0415
    import deployment_admission as admission  # noqa: PLC0415

    if not runtime.capsule_execution_ready(full=True):
        _fail("full transaction execution is not yet enabled")
    root = admission._physical_root(root)
    journal = validate_journal(copy.deepcopy(prepared))
    if journal["status"] != "active" or journal["phase"] != "prepared":
        _fail("full transaction is not prepared")
    admission.admit_component(root, operation="full-update", code_root=CODE_ROOT)
    terminal_verify = (transaction.verify_terminal_for_full4
                       if journal["schema_version"] in (FULL4_SCHEMA_VERSION, SCHEMA_VERSION)
                       else transaction.verify_terminal)
    terminal = terminal_verify(root, compose_path=compose_path)
    if terminal is None:
        _fail("full transaction has no terminal predecessor")
    write_initial_journal(root, journal, predecessor_raw=terminal[0], compose_path=compose_path)
    try:
        _step(root, journal, "state-plan")
        _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
        _plan_state(root, journal, compose_path=compose_path)
        _step(root, journal, "backup")
        _readers(root, journal, compose_path=compose_path)
        if classify_database(journal)[0] != "old":
            _fail("full database changed before backup")
        _prepare_backup(root, journal)
        _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
        _step(root, journal, "retain-rollback")
        for name in journal["selection"]:
            entry = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})[name]
            before = journal["before"]["containers"][name]
            if entry["old"]["container"]["name"] != before["name"]:
                _fail("full reader name changed before retention")
            transaction._rename(before["id"], transaction._retained_name(journal, name))
            _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
        _step(root, journal, "stop-readers")
        _stop_readers(root, journal, compose_path=compose_path)
        _step(root, journal, "state-snapshot")
        _snapshot_state(root, journal, compose_path=compose_path)
        _step(root, journal, "state-migrate")
        _migrate_state(root, journal, compose_path=compose_path)
        _all_readers_stopped(root, journal, compose_path=compose_path)
        _step(root, journal, "stage-db")
        _prepare_stage(root, journal, compose_path=compose_path)
        _step(root, journal, "promote-db")
        _promote_database(root, journal, compose_path=compose_path)
        _step(root, journal, "activate-readers")
        _activate_readers(root, journal, compose_path=compose_path)
        _step(root, journal, "actual-health-bindings")
        _verify_activated(root, journal, compose_path=compose_path)
        _step(root, journal, "immutable-manifest")
        _verify_activated(root, journal, compose_path=compose_path)
        manifest.write_manifest(root, journal["target"]["manifest"])
        if manifest.manifest_sha256(root, journal["transaction_id"]) != journal["manifest"]["sha256"]:
            _fail("full manifest publication differs")
        for phase, name in (("deploy-commit", ".deploy_commit"), ("deploy-current", ".deploy_current")):
            _step(root, journal, phase)
            _readers(root, journal, compose_path=compose_path)
            if classify_database(journal)[0] != "new":
                _fail("full database changed before pointer publication")
            allowed = [journal["pointers"][which][name] for which in ("old", "target")]
            transaction._replace_pointer(root, name, journal["pointers"]["target"][name], allowed)
        _verify_activated(root, journal, compose_path=compose_path)
        transaction._exact_pointers(root, journal, "target")
        _step(root, journal, "committed", status="committed")
        try:
            retire_committed(root, journal, compose_path=compose_path)
            retirement = "complete"
        except Exception:
            retirement = "pending"
        return {"status": "committed", "transaction_id": journal["transaction_id"], "retirement": retirement}
    except Exception as exc:
        raise FullError("full transaction interrupted; recover the recorded transaction") from exc


def _restore_readers(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    import component_transaction as transaction  # noqa: PLC0415

    initial = _readers(root, journal, compose_path=compose_path)
    if any(entry["old"]["container"]["running"] for entry in initial.values()):
        if any(entry["target"] is not None for entry in initial.values()):
            _fail("rollback started an old reader before removing all targets")
        for name, entry in initial.items():
            if entry["old"]["container"]["name"] != journal["before"]["containers"][name]["name"]:
                _fail("rollback started an old reader before restoring all names")
        if classify_database(journal)[0] not in ("old", "restored"):
            _fail("rollback started an old reader before restoring the database")
    else:
        _all_readers_stopped(root, journal, compose_path=compose_path)
    for name in journal["selection"]:
        entry = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old", "restored"})[name]
        target = entry["target"]
        if target is not None:
            target_id = target["container"]["id"]
            if journal["target"]["containers"][name]["id"] != target_id:
                _fail("full rollback target ID is not recorded")
            transaction._docker("DELETE", "/containers/" + target_id + "?v=0&force=0", accepted=(204,))
            if transaction._container_present(target_id):
                _fail("full rollback target remains after removal")
        entry = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old", "restored"})[name]
        before = journal["before"]["containers"][name]
        current_name = entry["old"]["container"]["name"]
        if current_name == transaction._retained_name(journal, name):
            transaction._rename(before["id"], before["name"])
        elif current_name != before["name"]:
            _fail("full rollback original reader name changed")
    for name in ("api", "telegram"):
        entries = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old", "restored"})
        if name not in entries:
            continue
        before = journal["before"]["containers"][name]
        old = entries[name]["old"]["container"]
        if before["running"] and not old["running"]:
            transaction._container_command("POST", old["id"], "start")
        after = _readers(root, journal, compose_path=compose_path)[name]["old"]["container"]
        if after["running"] != before["running"]:
            _fail("full rollback reader running state differs")
    _verify_activated(root, journal, compose_path=compose_path, restored=True)


def _verify_untouched_recovery(root: Path, journal: dict[str, Any], *, compose_path: Path) -> None:
    """Verify an early interruption without stopping or changing any reader."""
    import component_transaction as transaction  # noqa: PLC0415

    if (journal["schema_version"] < FULL4_SCHEMA_VERSION or classify_database(journal)[0] != "old"
            or journal["database"]["stage"]["stat"] is not None or journal["database"]["promoted_stat"] is not None
            or any(item.get("forward") is not None for item in journal["migrations"]["items"])):
        _fail("early recovery has a recorded service or data mutation")
    readers = _guard_readers(root, journal, compose_path=compose_path, allowed_database_states={"old"})
    for name, entry in readers.items():
        before = journal["before"]["containers"][name]
        current = entry["old"]["container"]
        if entry["target"] is not None or any(current[key] != before[key] for key in (
                "id", "image_id", "running", "started_at", "restart_count", "project", "service", "name",
                "transaction_label")):
            _fail("early recovery reader is not unchanged")
    transaction._require_target_manifest_absent(root, journal)
    transaction._exact_pointers(root, journal, "old")


def _reconcile_pending_probe_before_admission(root: Path, transaction_id: str, source_tool_sha256: str,
                                               *, compose_path: Path) -> None:
    """Reconcile only a matching probe stage after verifying the saved A."""
    import deployment_admission as admission  # noqa: PLC0415

    admission.require_lock(root)
    pending = runtime.pending_adopt_probe_staging(root)
    if pending is None:
        return
    path = root / ".deployments" / JOURNAL_NAME
    raw = runtime._read_regular(path)
    journal = validate_journal(runtime._decode(raw))
    source = journal["source"]
    if (journal["transaction_id"] != transaction_id or journal["status"] not in ("active", "unresolved")
            or source["source_tool_sha256"] != source_tool_sha256
            or journal["before"]["topology"]["telegram"] is None):
        _fail("probe staging does not belong to the active full transaction")
    admission._verify_recovery_extraction(root, source, CODE_ROOT)
    verify_predecessor(root, successor_kind="full", successor=journal)
    if recovery._verify_bound_compose(root, source) != compose_path:
        _fail("probe staging Compose identity differs")
    for name, pattern in ((".deploy_commit", runtime._HEX40), (".deploy_current", runtime._DEPLOYMENT_ID)):
        value = runtime._read_pointer(root, name, pattern)
        actual = {"present": value is not None, "bytes": None if value is None else value + "\n"}
        if actual not in (journal["pointers"]["old"][name], journal["pointers"]["target"][name]):
            _fail("probe staging pointer differs from the transaction")
    if runtime._read_regular(path) != raw:
        _fail("full journal changed before probe staging reconciliation")
    runtime.reconcile_adopt_probe_staging(root, pending, source_tool_sha256=source_tool_sha256,
                                          transaction_id=transaction_id)
    if runtime._read_regular(path) != raw:
        _fail("full journal changed during probe staging reconciliation")


def recover(root: Path, transaction_id: str, source_tool_sha256: str, *,
            compose_path: Path) -> dict[str, Any]:
    """Reconcile one FULL3 transaction from its independently verified A."""
    import component_state as state  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import deployment_admission as admission  # noqa: PLC0415

    try:
        root = admission._physical_root(root)
        _reconcile_pending_probe_before_admission(root, transaction_id, source_tool_sha256, compose_path=compose_path)
        journal = admission.admit_full_recovery(root, transaction_id=transaction_id,
                                                 source_tool_sha256=source_tool_sha256, code_root=CODE_ROOT)
        if recovery._verify_bound_compose(root, journal["source"]) != compose_path:
            _fail("full recovery Compose identity differs")
        if journal["status"] in ("active", "unresolved"):
            db_state, _ = classify_database(journal)
            backup = journal["database"]["backup"]
            if backup["stat"] is not None:
                backup_path = _verify_backup(root, journal)
                prior_binding = {**journal["database"]["old"]["binding"], "path": str(backup_path)}
            elif db_state == "old":
                prior_binding = journal["database"]["old"]["binding"]
            else:
                _fail("full recovery requires its recorded backup")
            transaction._verify_old_topology(root, journal, compose_path=compose_path, allow_probe=False,
                                             database_binding_override=prior_binding)
            if "api" in journal["selection"]:
                transaction._verify_candidate(root, journal)
            if "telegram" in journal["selection"]:
                transaction._telegram_receipt(root, journal)
            _readers(root, journal, compose_path=compose_path)
        finish_handoff(root, successor_kind="full", successor=journal)
        if journal["status"] in ("committed", "rolled_back"):
            checked = verify_terminal(root, compose_path=compose_path, recovery_source_id=source_tool_sha256)
            if checked is None or checked[1] != journal:
                _fail("full terminal record changed")
            if journal["status"] == "committed":
                health = terminal_health(root, journal)
                retire_committed(root, journal, compose_path=compose_path)
                return {"status": "committed", "transaction_id": transaction_id,
                        "already_committed": True, "health": health}
            return {"status": "rolled_back", "transaction_id": transaction_id, "already_restored": True}
        if journal["before"]["topology"]["telegram"] is not None:
            state.reconcile_transaction(root, transaction_id, source_tool_sha256, transaction_kind="full")
        if journal["schema_version"] >= FULL4_SCHEMA_VERSION and journal["phase"] in (
                "prepared", "state-plan", "backup", "rollback-untouched"):
            _verify_untouched_recovery(root, journal, compose_path=compose_path)
            if journal["phase"] != "rollback-untouched":
                _step(root, journal, "rollback-untouched")
            _verify_untouched_recovery(root, journal, compose_path=compose_path)
            _verify_activated(root, journal, compose_path=compose_path, restored=True)
            _verify_untouched_recovery(root, journal, compose_path=compose_path)
            _step(root, journal, "rolled-back", status="rolled_back")
            return {"status": "rolled_back", "transaction_id": transaction_id, "already_restored": False}
        if journal["phase"] not in _ROLLBACK:
            _step(root, journal, "rollback-stop-readers")
        if journal["phase"] == "rollback-stop-readers":
            entries = _readers(root, journal, compose_path=compose_path)
            for name in journal["selection"]:
                target = entries[name]["target"]
                if target is not None and journal["target"]["containers"][name]["id"] is None:
                    journal["target"]["containers"][name]["id"] = target["container"]["id"]
                    _write_journal(root, journal)
            _stop_readers(root, journal, compose_path=compose_path)
            _step(root, journal, "rollback-copyback")
        if journal["phase"] == "rollback-copyback":
            _all_readers_stopped(root, journal, compose_path=compose_path)
            entries = _readers(root, journal, compose_path=compose_path)
            target = entries.get("telegram", {}).get("target")
            target_started = (target is not None and target["container"]["started_at"] != "0001-01-01T00:00:00Z")
            if journal["schema_version"] == FULL4_SCHEMA_VERSION:
                old_reader = entries.get("telegram", {}).get("old")
                before = journal["before"]["containers"]["telegram"]
                old_restarted = (old_reader is not None and before is not None
                                 and old_reader["container"]["started_at"] != before["started_at"])
                _reconcile_state_v4(root, journal, compose_path=compose_path,
                                    reader_started=target_started or old_restarted)
            elif journal["schema_version"] == SCHEMA_VERSION:
                # Schema 5 never copies back: keep the progressed bytes, prove
                # the old native readers accept them, and never recreate links.
                _verify_progressed_state(root, journal, compose_path=compose_path)
            else:
                relocating = any(item["old_target_path"] != item["new_target_path"]
                                 for item in journal["migrations"]["items"])
                if target_started or relocating:
                    _verify_progressed_state(root, journal, compose_path=compose_path)
                if relocating:
                    _copyback_state(root, journal, compose_path=compose_path)
            _step(root, journal, "rollback-restore-db")
        if journal["phase"] == "rollback-restore-db":
            _restore_database(root, journal, compose_path=compose_path)
            _step(root, journal, "rollback-containers")
        if journal["phase"] == "rollback-containers":
            _restore_readers(root, journal, compose_path=compose_path)
            _step(root, journal, "rollback-pointers")
        if journal["phase"] == "rollback-pointers":
            for name in (".deploy_current", ".deploy_commit"):
                _readers(root, journal, compose_path=compose_path)
                values = [journal["pointers"][which][name] for which in ("old", "target")]
                transaction._replace_pointer(root, name, journal["pointers"]["old"][name], values)
            _verify_activated(root, journal, compose_path=compose_path, restored=True)
            transaction._exact_pointers(root, journal, "old")
            _step(root, journal, "rolled-back", status="rolled_back")
        return {"status": "rolled_back", "transaction_id": transaction_id, "already_restored": False}
    except FullError:
        raise
    except Exception as exc:
        raise FullError("full transaction recovery refused or remains unresolved") from exc


def verify_terminal(root: Path, *, compose_path: Path, strict_runtime: bool = False,
                    recovery_source_id: str | None = None) -> tuple[bytes, dict[str, Any]] | None:
    """Verify a terminal FULL3 binding without reconciling or changing services."""
    import component_full_db as database  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import deployment_admission as admission  # noqa: PLC0415

    try:
        root = admission._physical_root(root)
        admission.require_lock(root)
        path = root / ".deployments" / JOURNAL_NAME
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        raw = runtime._read_regular(path)
        journal = validate_journal(runtime._decode(raw))
        if root != CODE_ROOT:
            if recovery_source_id is None or recovery_source_id != journal["source"]["source_tool_sha256"]:
                _fail("full terminal source extraction is not identified")
            admitted = admission.admit_full_recovery(root, transaction_id=journal["transaction_id"],
                                                      source_tool_sha256=recovery_source_id, code_root=CODE_ROOT)
            if admitted != journal:
                _fail("full terminal changed during recovery admission")
        verify_predecessor(root, successor_kind="full", successor=journal)
        if journal["status"] not in ("committed", "rolled_back"):
            _fail("full transaction is not terminal")
        transaction._verify_saved_source(root, journal["source"])
        committed = journal["status"] == "committed"
        which = "target" if committed else "before"
        winning = journal["target"]["manifest"] if committed else journal["before"]["manifest"]
        observed_stat = (journal["database"]["promoted_stat"] if committed else
                         journal["database"]["restored_stat"] or journal["database"]["old"]["stat"])
        transaction._exact_pointers(root, journal, "target" if committed else "old")
        if manifest.read_manifest(root, journal["before"]["manifest"]["deployment_id"]) != journal["before"]["manifest"]:
            _fail("full terminal prior manifest differs")
        transaction._verify_manifest_if_present(root, journal, required=committed)
        if manifest.verify_database_binding(winning["database"]) != observed_stat:
            _fail("full terminal database stat differs")
        if database.permissions(Path(winning["database"]["path"])) != journal["database"]["old"]["permissions"]:
            _fail("full terminal database permissions differ")

        # A prior legacy image's manifest describes the OLD database. Once a
        # target DB is live, verify that provenance against the durable backup.
        backup = journal["database"]["backup"]
        old_binding = journal["database"]["old"]["binding"]
        if backup["stat"] is not None:
            backup_path = root / backup["path"]
            prior_binding = {**old_binding, "path": str(backup_path)}
            if (manifest.verify_database_binding(prior_binding) != backup["stat"]
                    or database.permissions(backup_path) != backup["permissions"]):
                _fail("full terminal backup differs")
        elif committed:
            _fail("full terminal backup is missing")
        else:
            prior_binding = old_binding
        transaction._verify_old_topology(root, journal, compose_path=compose_path, allow_probe=False,
                                         require_live_image=not committed, database_binding_override=prior_binding)
        current = copy.deepcopy(journal)
        current["database"] = {"binding": winning["database"], "stat": observed_stat}
        # FULL3 state records carry both paths and copy-back progress. The
        # shared runtime helper handles inventory/recipes/env/DB; path checks
        # for the full transaction are performed explicitly below.
        current["migrations"] = []
        transaction._verify_live_topology(root, current, which=which, compose_path=compose_path,
                                          allow_retained=committed, allow_runtime_drift=True,
                                          full4_endpoints=journal["schema_version"] == FULL4_SCHEMA_VERSION)
        topology = winning["components"]
        records = journal[which]["containers"]
        for name in ("api", "telegram"):
            component, expected = topology[name], records[name]
            if component is None:
                continue
            snapshot = transaction._snapshot(root, component)
            actual = transaction._observed(expected["id"], snapshot, os.urandom(32))
            observed = actual["container"]
            if strict_runtime and (observed["running"] != expected["running"]
                                   or observed["restart_count"] != expected["restart_count"]
                                   or committed and observed["started_at"] != expected["started_at"]):
                _fail("full terminal reader state changed")
            if name == "telegram" and journal["migrations"]["items"]:
                if journal["schema_version"] == PREVIOUS_SCHEMA_VERSION:
                    paths = transaction._telegram_effective_paths(actual, snapshot)
                    key = "new_target_path" if committed else "old_target_path"
                    if any(paths[item["kind"]] != item[key] for item in journal["migrations"]["items"]):
                        _fail("full terminal Telegram state path changed")
                elif journal["schema_version"] == SCHEMA_VERSION:
                    # Schema 5 keeps one effective location; a native-pending
                    # copy may be absent or progressed, and a retained reply
                    # entry may still be the product's own link.
                    paths = transaction._telegram_v5_paths(actual, snapshot)
                    if any(paths[item["kind"]]["planned"] != item["new_target_path"]
                           for item in journal["migrations"]["items"]):
                        _fail("full terminal Telegram state path changed")
                    import stat as stat_module  # noqa: PLC0415

                    for item in journal["migrations"]["items"]:
                        transaction._state_entry_safe(paths[item["kind"]]["entry"])
                        leaf = transaction._preserved_leaf(item["new_target_path"])
                        if not leaf["present"]:
                            continue
                        if leaf["link"]:
                            size = Path(item["new_target_path"]).lstat().st_size
                            if not 0 < size <= 4095:
                                _fail("full terminal Telegram state link is unsafe")
                        elif not stat_module.S_ISREG(Path(item["new_target_path"]).lstat().st_mode):
                            _fail("full terminal Telegram state is unsafe")
                else:
                    import component_state_endpoint as endpoint  # noqa: PLC0415
                    for item in journal["migrations"]["items"]:
                        progress = item["forward"] if committed else item["rollback"]
                        recorded = (progress["published"] if progress is not None and progress["published"] is not None
                                    else item["quiescent_new_endpoint" if committed else "quiescent_old_endpoint"]
                                    or item["new_endpoint" if committed else "old_endpoint"])
                        if recorded is None:
                            _fail("full terminal endpoint is missing")
                        endpoint.verify_terminal_route(snapshot, recorded)
        if committed:
            if "api" in journal["selection"]:
                transaction._verify_candidate(root, current)
            if "telegram" in journal["selection"]:
                transaction._telegram_receipt(root, current)
        transaction._exact_pointers(root, journal, "target" if committed else "old")
        if runtime._read_regular(path) != raw:
            _fail("full terminal journal changed during verification")
        return raw, journal
    except FullError:
        raise
    except Exception as exc:
        raise FullError("full terminal deployment cannot be verified") from exc


def terminal_health(root: Path, journal: dict[str, Any]) -> dict[str, str]:
    """Use the existing component readiness probes against the winning DB."""
    import component_transaction as transaction  # noqa: PLC0415

    current = copy.deepcopy(validate_journal(journal))
    committed = current["status"] == "committed"
    winning = current["target"]["manifest"] if committed else current["before"]["manifest"]
    db_stat = (current["database"]["promoted_stat"] if committed else
               current["database"]["restored_stat"] or current["database"]["old"]["stat"])
    current["database"] = {"binding": winning["database"], "stat": db_stat}
    current["migrations"] = []
    return transaction._terminal_health(root, current)


def retire_committed(root: Path, journal: dict[str, Any], *, compose_path: Path) -> bool:
    """Remove only reverified, stopped, retained full-update containers."""
    import component_transaction as transaction  # noqa: PLC0415

    journal = validate_journal(copy.deepcopy(journal))
    if journal["status"] != "committed":
        _fail("full retirement requires a committed journal")
    expected = manifest._encode(journal)
    source_id = None if root == CODE_ROOT else journal["source"]["source_tool_sha256"]
    retired = False
    for name in journal["selection"]:
        checked = verify_terminal(root, compose_path=compose_path, recovery_source_id=source_id)
        if checked is None or checked[0] != expected:
            _fail("full terminal changed before retirement")
        terminal_health(root, journal)
        before = journal["before"]["containers"][name]
        snapshot = transaction._snapshot(root, journal["before"]["topology"][name])
        found = transaction._inventory(snapshot["project"], snapshot["service"])
        if before["id"] not in {entry.get("Id") for entry in found}:
            continue
        actual = transaction._observed(before["id"], snapshot, os.urandom(32))
        transaction._recipe_matches(actual, snapshot)
        if actual["container"] != {**before, "name": transaction._retained_name(journal, name), "running": False}:
            _fail("full retained container differs from its retirement record")
        transaction._docker("DELETE", "/containers/" + before["id"] + "?v=0&force=0", accepted=(204,))
        if transaction._container_present(before["id"]):
            _fail("full retained container remains after retirement")
        retired = True
    checked = verify_terminal(root, compose_path=compose_path, recovery_source_id=source_id)
    if checked is None or checked[0] != expected:
        _fail("full terminal changed during retirement")
    return retired
