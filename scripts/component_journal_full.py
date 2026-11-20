#!/usr/bin/env python3
"""Read and validate full component and data transaction journals."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

import component_journal_runtime as runtime
import component_recovery as recovery
import product_manifest as manifest


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


def _full4_endpoint_route(journal: dict[str, Any]) -> bool:
    """Report whether this record's Telegram state is read through FULL4 probes.

    FULL4's separate ``state-endpoint`` probes exist only for the schema-4
    cutover. A schema-5 record verifies the same leaves through the product's
    own plans, so asking for the FULL4 route there would demand an admission
    the record never recorded.
    """
    return journal["schema_version"] == FULL4_SCHEMA_VERSION


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

    A stopped leaf the snapshot recorded as absent carries no bytes for either
    reader to accept, so a target that is still absent is already reconciled.
    Every other kept state needs the comparison ``_verify_progressed_state``
    records, including a retained reply link, which both native readers parse
    through to the file it names.
    """
    if item["rollback_compatibility"] is not None:
        return True
    return item["new_preimage"] is not None and item["new_preimage"]["present"] is False


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
