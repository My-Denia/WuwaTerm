#!/usr/bin/env python3
"""Verify and retire terminal full component/data deployments."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import component_journal_full as journal
import component_journal_runtime as runtime
import product_manifest as manifest
from component_handoff import verify_predecessor

CODE_ROOT = Path(__file__).resolve().parents[1]
FullError = journal.FullError
JOURNAL_NAME = journal.JOURNAL_NAME
PREVIOUS_SCHEMA_VERSION = journal.PREVIOUS_SCHEMA_VERSION
SCHEMA_VERSION = journal.SCHEMA_VERSION
_fail = journal._fail
_full4_endpoint_route = journal._full4_endpoint_route
validate_journal = journal.validate_journal


def verify_terminal(root: Path, *, compose_path: Path, strict_runtime: bool = False,
                    recovery_source_id: str | None = None,
                    api_override: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]] | None:
    """Verify a terminal FULL3 binding without reconciling or changing services."""
    import component_full_db as database  # noqa: PLC0415
    import component_operations as transaction  # noqa: PLC0415
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
                                          allow_retained=committed and api_override is None,
                                          allow_runtime_drift=True,
                                          full4_endpoints=_full4_endpoint_route(journal),
                                          api_override=api_override)
        topology = winning["components"]
        records = journal[which]["containers"]
        if api_override is not None:
            topology = {**topology, "api": {**topology["api"], "recipe": api_override["recipe"]}}
            records = {**records, "api": api_override["container"]}
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
    import component_operations as transaction  # noqa: PLC0415

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
    import component_operations as transaction  # noqa: PLC0415

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
