#!/usr/bin/env python3
"""Recover an old transaction with its exact historical source, offline.

Preflight is read-only. The explicit recovery operation takes the deployment
lock, retains a portable snapshot of the current source, restores the journal's
commit in the same checkout, then calls the unchanged historical shell entry.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# The scripts are retained together outside the checkout. Keep that import
# usable under Python isolated mode, which ignores inherited PYTHON* settings.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_recovery_source import (  # noqa: E402
    CapsuleError,
    DeploymentLock,
    clean_env,
    git,
    package,
    sha,
    sync_directory,
    tree_files,
    verify,
)

JOURNALS = {"runtime": ".runtime-transaction.json", "full": ".full-transaction.json"}


def regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def read_journal(root: Path, kind: str) -> tuple[bytes, dict[str, Any]]:
    deployments = root / ".deployments"
    journal = deployments / JOURNALS[kind]
    if deployments.is_symlink() or not deployments.is_dir() or not regular(journal):
        raise CapsuleError("journal or deployment directory is not a regular path")
    raw = journal.read_bytes()
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CapsuleError("invalid legacy journal") from exc
    if type(payload) is not dict or type(payload.get("schema_version")) is not int:
        raise CapsuleError("unsupported legacy journal")
    if kind == "runtime":
        if payload["schema_version"] != 1 or payload.get("mode") != "runtime-only":
            raise CapsuleError("unsupported legacy runtime journal")
        if payload.get("status") not in ("active", "unresolved"):
            raise CapsuleError("runtime journal does not need recovery")
        other = deployments / JOURNALS["full"]
        if other.exists() or other.is_symlink():
            raise CapsuleError("full transaction also exists")
    elif payload["schema_version"] != 2:
        raise CapsuleError("unsupported legacy full journal")
    if type(payload.get("source_commit")) is not str or type(payload.get("tool_hash")) is not str:
        raise CapsuleError("invalid legacy journal source")
    return raw, payload


def validate_historical_parser(extracted: Path, kind: str, raw: bytes) -> None:
    # This is parsing only. ROOT is never patched: the original module resolves
    # its own location in this temporary verified source; recovery itself runs in
    # the real deployment root through the original shell entry below.
    folder = extracted / ".deployments"
    folder.mkdir(exist_ok=True)
    (folder / JOURNALS[kind]).write_bytes(raw)
    module = "runtime_update" if kind == "runtime" else "full_update"
    code = ("import pathlib,sys; r=pathlib.Path(sys.argv[1]); "
            "sys.path[:0]=[str(r),str(r/'src')]; "
            f"from scripts import {module} as m; m._load_journal()")
    process = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(extracted)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=clean_env(), cwd=extracted, check=False)
    if process.returncode:
        raise CapsuleError("historical journal parser refused the record")


def source_preflight(root: Path, capsule: Path, kind: str) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    raw, journal = read_journal(root, kind)
    with tempfile.TemporaryDirectory(prefix="wuwaterm-journal-") as directory:
        source = Path(directory)
        manifest = verify(capsule, journal["source_commit"], extracted=source)
        if kind not in manifest["supported_kinds"]:
            raise CapsuleError("historical source does not support this recovery kind")
        if manifest["tool_hashes"][kind] != journal["tool_hash"]:
            raise CapsuleError("journal tool fingerprint mismatch")
        validate_historical_parser(source, kind, raw)
        if kind == "full":
            companion = root / ".deployments" / JOURNALS["runtime"]
            if companion.exists() or companion.is_symlink():
                if not regular(companion):
                    raise CapsuleError("invalid companion runtime journal")
                companion_raw = companion.read_bytes()
                try:
                    previous = json.loads(companion_raw)
                except (ValueError, UnicodeError) as exc:
                    raise CapsuleError("invalid companion runtime journal") from exc
                if (type(previous) is not dict or type(previous.get("schema_version")) is not int
                        or previous["schema_version"] != 1 or previous.get("mode") != "runtime-only"
                        or previous.get("status") not in ("committed", "rolled_back")):
                    raise CapsuleError("unfinished or unsupported companion runtime journal")
                validate_historical_parser(source, "runtime", companion_raw)
    return raw, journal, manifest


def checkout_preflight(root: Path, manifest: dict[str, Any]) -> str:
    metadata = root / ".git"
    if metadata.is_symlink() or not metadata.is_dir():
        raise CapsuleError("deployment Git metadata must be a local directory")
    top = git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if Path(top).resolve() != root:
        raise CapsuleError("target is not the deployment checkout root")
    filters = subprocess.run(["git", "-C", str(root), "config", "--local", "--name-only",
                              "--get-regexp", r"^filter\."], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=clean_env(), check=False)
    if filters.returncode != 1:
        raise CapsuleError("configured checkout filters are unsupported")
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise CapsuleError("dirty deployment checkout")
    _, current = tree_files(root, head)
    for entry in git(root, "ls-files", "-v", "-z").split(b"\0"):
        if entry and entry[:1] != b"H":
            raise CapsuleError("unsupported index flags")
    target = manifest["files"]
    for name in set(current) | set(target):
        path = root
        parts = name.split("/")
        for component in parts[:-1]:
            path = path / component
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise CapsuleError("source path has an unsafe parent")
        path = root.joinpath(*parts)
        if path.is_symlink():
            raise CapsuleError("source path is a symlink")
        if name in target and name not in current and path.exists():
            raise CapsuleError("source would overwrite untracked data")
        if name in current:
            item = current[name]
            if not regular(path) or sha(path) != item["sha256"]:
                raise CapsuleError("current source content mismatch")
            if bool(path.stat().st_mode & 0o111) != (item["mode"] == "100755"):
                raise CapsuleError("current source mode mismatch")
    # Ignored user files can be just as important as untracked files; checkout
    # receives --no-overwrite-ignore as a second guard against these collisions.
    return head


def deployment_lock(root: Path) -> DeploymentLock:
    lock = root / ".deployments" / ".deployment.lock"
    if lock.is_symlink() or (lock.exists() and not regular(lock)):
        raise CapsuleError("invalid deployment lock")
    try:
        os.fstat(9)
    except OSError:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        if fd != 9:
            os.dup2(fd, 9)
            os.close(fd)
    return DeploymentLock(root, 9)


def verify_checkout(root: Path, journal: dict[str, Any], manifest: dict[str, Any]) -> None:
    if git(root, "rev-parse", "HEAD").decode("ascii").strip() != journal["source_commit"]:
        raise CapsuleError("restored HEAD mismatch")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise CapsuleError("restored checkout is dirty")
    tree, files = tree_files(root, journal["source_commit"])
    if tree != manifest["source_tree"] or files != manifest["files"]:
        raise CapsuleError("restored source tree mismatch")
    for name, item in files.items():
        path = root.joinpath(*name.split("/"))
        if not regular(path) or sha(path) != item["sha256"]:
            raise CapsuleError("restored source content mismatch")
        if bool(path.stat().st_mode & 0o111) != (item["mode"] == "100755"):
            raise CapsuleError("restored source mode mismatch")


def run(root: Path, capsule: Path, kind: str, *, recover: bool) -> int:
    root = root.resolve(strict=True)
    absolute = capsule.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise CapsuleError("capsule path contains a symlink")
    capsule = capsule.resolve(strict=True)
    # If inside the checkout, the capsule must live in the protected deployment
    # metadata directory so checkout cannot remove or overwrite it.
    if capsule.is_relative_to(root) and not capsule.is_relative_to(root / ".deployments"):
        raise CapsuleError("capsule must be outside source or under .deployments")
    raw, journal, manifest = source_preflight(root, capsule, kind)
    current = checkout_preflight(root, manifest)
    lock = root / ".deployments" / ".deployment.lock"
    if lock.is_symlink() or (lock.exists() and not regular(lock)):
        raise CapsuleError("invalid deployment lock")
    if not recover:
        print(f"legacy {kind} source preflight passed for {journal['source_commit']}")
        return 0

    held_lock = deployment_lock(root)
    locked_raw, locked_journal, locked_manifest = source_preflight(root, capsule, kind)
    if locked_raw != raw or locked_journal != journal or locked_manifest != manifest:
        raise CapsuleError("journal or capsule changed during preflight")
    if checkout_preflight(root, manifest) != current:
        raise CapsuleError("checkout changed during preflight")
    branch_proc = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=clean_env(), check=False)
    branch = branch_proc.stdout.decode("utf-8").strip() if branch_proc.returncode == 0 else None
    backup = root / ".deployments" / f"legacy-source-return-{current[:12]}-{uuid.uuid4().hex}"
    package(root, current, backup, legacy=False, lock=held_lock)
    verify(backup, current, allow_backup=True)
    with (backup / "return.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps({"source_commit": current, "branch": branch},
                                sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(backup)
    print(f"current source backup: {backup}", flush=True)
    if checkout_preflight(root, manifest) != current:
        raise CapsuleError("checkout changed while preserving source")
    git(root, "fetch", "--no-tags", "--no-write-fetch-head", str(capsule / "source.bundle"),
        "refs/capsule/source", lock=held_lock)
    git(root, "checkout", "--detach", "--no-overwrite-ignore", journal["source_commit"],
        lock=held_lock)
    verify_checkout(root, journal, manifest)
    if read_journal(root, kind)[0] != raw:
        raise CapsuleError("journal changed before recovery")
    env = clean_env()
    env["WUWATERM_DEPLOY_ROOT"] = str(root)
    option = "--recover-runtime" if kind == "runtime" else "--recover-full"
    return subprocess.run(["sh", "deploy/vps-update.sh", option], cwd=root, env=env,
                          pass_fds=held_lock.pass_fds(), check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--kind", choices=tuple(JOURNALS), required=True)
    parser.add_argument("--recover", action="store_true", help="restore source and run the original recovery")
    args = parser.parse_args()
    try:
        return run(args.root, args.capsule, args.kind, recover=args.recover)
    except (CapsuleError, OSError, UnicodeError, ValueError) as exc:
        print(f"legacy recovery refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
