#!/usr/bin/env python3
"""Shared, read-only admission for current deployment entrypoints.

The lock is the deployment root's inherited FD 9, not a newly opened file.
Historical source capsules contain their own original tools and do not use this
module. Admission does not itself enable the unfinished component transaction.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RUNTIME = ".runtime-transaction.json"
LEGACY_FULL = ".full-transaction.json"
COMPONENT_RUNTIME = ".component-runtime-transaction.json"
COMPONENT_FULL = ".component-full-transaction.json"
BUILD_ATTEMPTS = "component-build-attempts"
_DEPLOYMENT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")


class AdmissionError(RuntimeError):
    """The deployment state cannot safely admit this operation."""


def _physical_root(root: Path) -> Path:
    root = Path(root)
    try:
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise AdmissionError("deployment root is not a physical directory")
    except OSError as exc:
        raise AdmissionError("deployment root is unavailable") from exc
    return root


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdmissionError("deployment metadata cannot be inspected") from exc
    return True


def _deployments(root: Path, *, required: bool) -> Path:
    directory = root / ".deployments"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if required:
            raise AdmissionError("deployment metadata directory is missing") from None
    except OSError as exc:
        raise AdmissionError("deployment metadata directory cannot be inspected") from exc
    else:
        if not stat.S_ISDIR(info.st_mode):
            raise AdmissionError("deployment metadata directory is not a directory")
    return directory


def require_lock(root: Path) -> None:
    """Verify the root-bound regular lock inode and acquire its inherited OFD."""
    root = _physical_root(root)
    path = _deployments(root, required=True) / ".deployment.lock"
    try:
        actual = os.fstat(9)
        expected = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(actual.st_mode) or not stat.S_ISREG(expected.st_mode)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                or os.readlink("/proc/self/fd/9") != str(path)):
            raise AdmissionError("deployment lock FD9 does not match the root")
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise AdmissionError("deployment lock FD9 is unavailable") from exc


def _no_new_markers(root: Path) -> None:
    root = _physical_root(root)
    directory = _deployments(root, required=False)
    if any(_present(path) for path in (
        root / ".deploy_current", directory / COMPONENT_RUNTIME, directory / COMPONENT_FULL,
    )):
        raise AdmissionError(
            "component deployment metadata is present; inspect with "
            "python3 scripts/component_runtime.py --root <deployment-root> inspect "
            "(component recovery is not yet available)"
        )


def _attempts_clear(root: Path) -> None:
    """Reject interrupted or unknown build attempts, including unsafe paths."""
    directory = _deployments(root, required=False) / BUILD_ATTEMPTS
    if not _present(directory):
        return
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise AdmissionError("build attempt directory is not a directory")
        # Import only when records exist. The parser validates the complete
        # record, not merely a caller-controlled status string.
        entries = list(directory.iterdir())
        if entries:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_build  # noqa: PLC0415
            for entry in entries:
                record = component_build._read_attempt(root, entry.name)
                if record["status"] == "active":
                    raise AdmissionError("interrupted component build requires recovery")
    except AdmissionError:
        raise
    except Exception as exc:
        raise AdmissionError("build attempt metadata is unknown") from exc


def legacy_metadata_absent(root: Path) -> None:
    """Read-only check, also used by the external historical dispatcher."""
    _no_new_markers(root)
    _attempts_clear(root)


def admit_legacy(root: Path) -> None:
    """Admit a legacy parent or its helper under their same held lock."""
    require_lock(root)
    legacy_metadata_absent(root)


def require_root_paths(root: Path, *paths: Path) -> None:
    """Reject CLI write paths outside the physical root or through symlinks."""
    root = _physical_root(root)
    for path in paths:
        target = Path(path)
        if not target.is_absolute():
            target = Path.cwd() / target
        if target == root or not target.is_relative_to(root):
            raise AdmissionError("write path is outside the deployment root")
        current = root
        for part in target.relative_to(root).parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AdmissionError("write path cannot be inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                raise AdmissionError("write path contains a symlink")
        resolved = target.resolve(strict=False)
        if resolved == root or not resolved.is_relative_to(root):
            raise AdmissionError("write path is outside the deployment root")


def admit_component(root: Path, *, operation: str, code_root: Path,
                    transaction_id: str | None = None) -> None:
    """Check known conflicts before a future component writer may proceed.

    Callers still have to verify exact tracked source identity and, before
    replacing a terminal new journal, archive it durably. No writer is enabled
    by this check.
    """
    root = _physical_root(root)
    if root != _physical_root(code_root):
        raise AdmissionError("component source and deployment roots differ")
    require_lock(root)
    directory = _deployments(root, required=True)
    if _present(directory / LEGACY_FULL):
        raise AdmissionError("legacy full transaction requires recovery")
    old_runtime = directory / LEGACY_RUNTIME
    if _present(old_runtime):
        try:
            if not stat.S_ISREG(old_runtime.lstat().st_mode):
                raise ValueError("not a regular journal")
            # Reject duplicate keys before using the existing full schema parser.
            def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            json.loads(old_runtime.read_bytes(), object_pairs_hook=unique)
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import runtime_update  # noqa: PLC0415
            previous = runtime_update._load_journal(old_runtime)
        except Exception as exc:
            raise AdmissionError("legacy runtime journal is unknown") from exc
        if previous["status"] not in ("committed", "rolled_back"):
            raise AdmissionError("legacy runtime transaction requires recovery")
    if _present(directory / COMPONENT_FULL):
        raise AdmissionError("component full transaction requires recovery")
    _attempts_clear(root)
    current = directory / COMPONENT_RUNTIME
    if _present(current):
        try:
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_runtime  # noqa: PLC0415
            journal = component_runtime.load_journal(current)
        except Exception as exc:
            raise AdmissionError("component runtime journal is unknown") from exc
        if operation == "recover":
            if (not transaction_id or journal["transaction_id"] != transaction_id
                    or journal["status"] not in ("active", "unresolved")):
                raise AdmissionError("component recovery does not match the active transaction")
        elif journal["status"] not in ("committed", "rolled_back"):
            raise AdmissionError("component runtime transaction requires recovery")
    elif operation == "recover":
        raise AdmissionError("component recovery transaction is missing")
    pointer = root / ".deploy_current"
    if _present(pointer):
        try:
            if not stat.S_ISREG(pointer.lstat().st_mode):
                raise ValueError("pointer is not a regular file")
            value = pointer.read_text(encoding="ascii")
            if not value.endswith("\n") or not _DEPLOYMENT_ID.fullmatch(value[:-1]):
                raise ValueError("invalid pointer")
            sys.path.insert(0, str(CODE_ROOT / "scripts"))
            import component_runtime  # noqa: PLC0415
            # Syntax is not proof of a recorded binding. The inspector checks
            # the immutable manifest and both pointers without claiming live
            # service health.
            if component_runtime.inspect_deployment(root)["binding"] != "recorded":
                raise ValueError("unverified pointer")
        except (OSError, UnicodeError, ValueError) as exc:
            raise AdmissionError("component deployment pointer is unknown") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("legacy", "legacy-precheck"))
    args = parser.parse_args()
    try:
        if args.operation == "legacy":
            admit_legacy(CODE_ROOT)
        else:
            legacy_metadata_absent(CODE_ROOT)
    except AdmissionError as exc:
        print(f"deployment admission refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
