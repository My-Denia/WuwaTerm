#!/usr/bin/env python3
"""Finish a cross-kind handoff once its successor passed recovery admission.

``component_handoff`` holds the predecessor primitives and must not reach into
``deployment_admission``; that module in turn verifies predecessors through the
same primitives. This entry point is the only place that needs both, so it sits
above the pair: admission proves the successor still matches its own journal,
then the durable marker sync retires exactly the predecessor that was verified.

Keeping the removal here leaves ``deployment_admission -> component_handoff`` a
one-way edge instead of a back-edge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import component_handoff as handoff
import component_journal_full as full_journal
import component_journal_runtime as runtime
import deployment_admission as admission

CODE_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_NAME = full_journal.JOURNAL_NAME
FullError = full_journal.FullError
_fail = full_journal._fail


def finish_handoff(root: Path, *, successor_kind: str, successor: dict[str, Any]) -> None:
    """After exact-source admission, retire a still-present cross-kind marker."""
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
                handoff._sync_handoff_markers(root, previous_name=Path(reference["journal_path"]).name,
                                              previous_raw=None, successor_name=successor_name,
                                              successor_raw=current_raw)
            return
        previous_raw = runtime._read_regular(previous_path)
        previous = runtime._decode(previous_raw)
        handoff.verify_handoff(root, successor_kind=successor_kind, successor=successor,
                               predecessor=previous, predecessor_raw=previous_raw)
        successor_name = JOURNAL_NAME if successor_kind == "full" else runtime.JOURNAL_NAME
        current_raw = runtime._read_regular(root / ".deployments" / successor_name)
        if runtime._decode(current_raw) != successor:
            _fail("handoff: successor changed before predecessor removal")
        handoff._sync_handoff_markers(root, previous_name=Path(reference["journal_path"]).name,
                                      previous_raw=previous_raw, successor_name=successor_name,
                                      successor_raw=current_raw)
    except FullError:
        raise
    except Exception as exc:
        raise FullError("handoff: predecessor removal could not be reconciled") from exc
