#!/usr/bin/env python3
"""Verify cross-kind deployment handoffs against their predecessor archives.

The primitives here read, validate and compare predecessor records and sync the
durable markers. They deliberately depend on no admission module: the one
operation that needs admission, retiring a verified predecessor, lives in
``component_handoff_finish`` above both this module and
``deployment_admission``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import component_journal_full as full_journal
import component_journal_runtime as runtime
import product_manifest as manifest

JOURNAL_NAME = full_journal.JOURNAL_NAME
FullError = full_journal.FullError
_fail = full_journal._fail
validate_journal = full_journal.validate_journal


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
