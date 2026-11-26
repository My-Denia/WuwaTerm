#!/usr/bin/env python3
"""Select and verify terminal predecessor records before a transaction."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import component_operations as operations
import component_journal_runtime as runtime
import deployment_admission as admission

CODE_ROOT = Path(__file__).resolve().parents[1]
TransactionError = operations.TransactionError
_retire_committed = operations._retire_committed
_terminal_health = operations._terminal_health
_verify_terminal_record = operations._verify_terminal_record


def _predecessor_health(root: Path, journal: dict[str, Any]) -> dict[str, str]:
    if journal["kind"] == "component-full":
        import component_terminal_full as full  # noqa: PLC0415
        return full.terminal_health(root, journal)
    return _terminal_health(root, journal)


def verify_terminal(root: Path, *, compose_path: Path,
                    api_override: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]] | None:
    """Read and reconcile a terminal journal without creating state probes or writes.

    ``api_override`` is reserved for the runtime-options refresh; see
    ``component_operations._verify_live_topology``.
    """
    return _verify_terminal(root, compose_path=compose_path, full4_predecessor=False,
                            api_override=api_override)


def verify_terminal_for_full4(root: Path, *, compose_path: Path) -> tuple[bytes, dict[str, Any]] | None:
    """Admit a predecessor to a FULL successor's terminal verification.

    Runtime records themselves retain their original schema and strict default
    verifier. This opt-in only changes the leaf mapper a FULL successor needs
    for a schema-2..4 record; a schema-5 record keeps its own native leaf
    checks, and either kind must still verify complete endpoints before state
    mutation.
    """
    return _verify_terminal(root, compose_path=compose_path, full4_predecessor=True)


def _verify_terminal(root: Path, *, compose_path: Path, full4_predecessor: bool,
                     api_override: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]] | None:
    root = admission._physical_root(root)
    if root != CODE_ROOT:
        raise TransactionError("terminal verification requires the physical source checkout")
    admission.require_lock(root)
    path = root / ".deployments" / runtime.JOURNAL_NAME
    full_path = root / ".deployments" / admission.COMPONENT_FULL
    if admission._present(full_path):
        if admission._present(path):
            raise TransactionError("cross-kind handoff requires recovery")
        try:
            import component_terminal_full as full  # noqa: PLC0415
            if Path(full.__file__).resolve() != CODE_ROOT / "scripts/component_terminal_full.py":
                raise TransactionError("full terminal module belongs to another source")
            return full.verify_terminal(root, compose_path=compose_path, api_override=api_override)
        except full.FullError as exc:
            raise TransactionError("full terminal deployment cannot be verified") from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        raw = runtime._read_regular(path)
        journal = runtime.validate_journal(runtime._decode(raw))
        _verify_terminal_record(root, journal, raw, compose_path, full4_predecessor=full4_predecessor,
                                api_override=api_override)
        return raw, journal
    except TransactionError:
        raise
    except Exception as exc:
        raise TransactionError("terminal deployment cannot be verified") from exc


def _prepare_predecessor(root: Path, compose_path: Path, *, for_full4: bool = False) -> bytes | None:
    verify = verify_terminal_for_full4 if for_full4 else verify_terminal
    terminal = verify(root, compose_path=compose_path)
    if terminal is None:
        return None
    raw, journal = terminal
    if journal["kind"] == "component-full":
        import component_terminal_full as full  # noqa: PLC0415
        full.terminal_health(root, journal)
    else:
        _terminal_health(root, journal)
    verified = verify(root, compose_path=compose_path)
    if verified is None or verified[0] != raw:
        raise TransactionError("terminal predecessor changed during readiness")
    if journal["action"] == "update" and journal["status"] == "committed":
        if journal["kind"] == "component-full":
            full.retire_committed(root, journal, compose_path=compose_path)
        else:
            _retire_committed(root, journal, for_full4=for_full4)
        verified = verify(root, compose_path=compose_path)
        if verified is None or verified[0] != raw:
            raise TransactionError("terminal predecessor changed during retirement")
    return raw
