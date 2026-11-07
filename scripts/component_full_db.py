#!/usr/bin/env python3
"""Prepare verified, owned SQLite copies for a full component transaction.

The coordinator owns source admission and the transaction journal. These
helpers require the root-bound deployment lock and never replace the live DB.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402


class FullDatabaseError(RuntimeError):
    """A DB preimage or owned copy could not be safely established."""


# One owned copy is staged under this prefix and published by rename, so a
# refused preparation can name exactly its own leftovers.
_COPY_PREFIX = ".component-full-copy-"
_CANDIDATES = "component-full-candidates"
_CLAIMS = "component-full-db-claims"
# component_full.JOURNAL_NAME, restated here so a discard never imports it.
_FULL_JOURNAL = ".component-full-transaction.json"


VERIFICATION = {
    "profile": "arikatsu",
    "exact_hit_sample_size": 500,
    "require_free_text_exclusions": True,
    "require_free_text_zh_exclusions": True,
    "verify_db_exit": 0,
    "exact_hits_exit": 0,
    "seed_terms_exit": 0,
}


def _identifier(value: Any) -> str:
    if type(value) is not str or not manifest._ID.fullmatch(value):
        raise FullDatabaseError("invalid full transaction ID")
    return value


def _root(path: Path) -> Path:
    try:
        root = admission._physical_root(Path(path))
        admission.require_lock(root)
        return root
    except (admission.AdmissionError, OSError) as exc:
        raise FullDatabaseError("deployment root or lock is unavailable") from exc


def _absolute(path: Path) -> Path:
    try:
        value = manifest._path(os.fspath(path), "database path")
    except (manifest.ManifestError, TypeError) as exc:
        raise FullDatabaseError("database path is invalid") from exc
    return Path(value)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _permissions_and_stat(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    path = _absolute(path)
    try:
        with manifest._directory(path.parent, (), create=False) as directory:
            manifest._reject_sidecars(directory, path.name)
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
            try:
                before = os.fstat(fd)
                after = os.fstat(fd)
                named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(before.st_mode) or _identity(before) != _identity(after)
                        or _identity(after) != _identity(named)):
                    raise FullDatabaseError("database permissions or identity changed")
                manifest._reject_sidecars(directory, path.name)
                return ({"mode": stat.S_IMODE(after.st_mode), "uid": after.st_uid, "gid": after.st_gid},
                        manifest._db_stat(after))
            finally:
                os.close(fd)
    except (manifest.ManifestError, OSError) as exc:
        raise FullDatabaseError("database permissions cannot be verified") from exc


def permissions(path: Path) -> dict[str, int]:
    """Read a regular DB's mode, UID and GID without following path links."""
    return _permissions_and_stat(path)[0]


def _snapshot(path: Path) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    try:
        binding, db_stat = manifest.read_database_snapshot(_absolute(path))
        perms, after_stat = _permissions_and_stat(path)
        if db_stat != after_stat:
            raise FullDatabaseError("database changed during inspection")
        return binding, db_stat, perms
    except manifest.ManifestError as exc:
        raise FullDatabaseError("database cannot be verified") from exc


def _same_contents(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left[key] == right[key] for key in ("sha256", "schema_version", "provenance"))


def _exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FullDatabaseError("owned database path cannot be inspected") from exc
    return True


def _claim(root: Path, identifier: str, kind: str, value: dict[str, Any], *, final_exists: bool) -> None:
    """Record ownership before publication, so a matching retry is explicit."""
    name = f"{identifier}-{kind}.json"
    raw = manifest._encode(value)
    try:
        with manifest._directory(root, (".deployments", _CLAIMS), create=True) as directory:
            exists = _exists(directory, name)
            if final_exists and not exists:
                raise FullDatabaseError("existing database copy has no owned claim")
            manifest._write_file(directory, name, raw, reuse=True)
    except (manifest.ManifestError, OSError) as exc:
        raise FullDatabaseError("database copy claim cannot be established") from exc


def _publish(directory: int, temporary: str, destination: str) -> None:
    """Atomically publish a single owned file without replacing a destination."""
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise FullDatabaseError("atomic no-replace publication is unavailable") from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(temporary), directory, os.fsencode(destination), 1) != 0:
        if ctypes.get_errno() == errno.EEXIST:
            raise FullDatabaseError("database publication destination already exists")
        raise FullDatabaseError("atomic database publication failed")
    os.fsync(directory)


def _set_permissions(fd: int, expected: dict[str, int]) -> None:
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid) != (expected["uid"], expected["gid"]):
        os.fchown(fd, expected["uid"], expected["gid"])
    os.fchmod(fd, expected["mode"])
    actual = os.fstat(fd)
    if {"mode": stat.S_IMODE(actual.st_mode), "uid": actual.st_uid, "gid": actual.st_gid} != expected:
        raise FullDatabaseError("database permissions cannot be preserved")


def _verification_scratch(root: Path, identifier: str) -> tuple[Path, tuple[int, int]]:
    with manifest._directory(root, (".deployments",), create=False) as directory:
        name = ".component-full-verify-" + identifier + "-" + secrets.token_hex(16)
        os.mkdir(name, 0o700, dir_fd=directory)
        os.fsync(directory)
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        return root / ".deployments" / name, (info.st_dev, info.st_ino)


def _remove_empty_verification_scratch(root: Path, scratch: Path, expected: tuple[int, int]) -> None:
    """Remove only the empty directory created by this successful verification."""
    if scratch.parent != root / ".deployments" or not scratch.name.startswith(".component-full-verify-"):
        raise FullDatabaseError("verification scratch path differs")
    with manifest._directory(root, (".deployments",), create=False) as parent:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(scratch.name, flags, dir_fd=parent)
        try:
            info = os.fstat(fd)
            named = os.stat(scratch.name, dir_fd=parent, follow_symlinks=False)
            if ((info.st_dev, info.st_ino) != expected or (named.st_dev, named.st_ino) != expected
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or os.listdir(fd)):
                raise FullDatabaseError("verification scratch is not the owned empty directory")
            current = os.stat(scratch.name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != expected:
                raise FullDatabaseError("verification scratch changed before removal")
            os.rmdir(scratch.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)


def _verify_candidate(root: Path, path: Path, identifier: str) -> dict[str, Any]:
    """Run the unchanged deployment verifiers with inherited FD9 and no output leakage."""
    scratch, scratch_identity = _verification_scratch(root, identifier)
    commands = [
        [sys.executable, "-I", "-B", str(CODE_ROOT / "scripts/verify_db.py"), str(path),
         "--profile", "arikatsu", "--min-category", "resonator", "--min-category", "weapon",
         "--min-category", "echo", "--min-category", "item", "--min-category", "skill",
         "--min-category", "sonata_effect", "--min-category", "location",
         "--require-free-text-exclusions", "--require-free-text-zh-exclusions"],
        [sys.executable, "-I", "-B", str(CODE_ROOT / "scripts/verify_exact_hits.py"), str(path),
         "--sample-size", "500"],
        [sys.executable, "-I", "-B", str(CODE_ROOT / "scripts/verify_seed_terms.py"), str(path),
         "--discrepancies", str(scratch / "seed-discrepancies.json")],
    ]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C"}
    for command in commands:
        admission.require_lock(root)
        try:
            result = subprocess.run(command, cwd=CODE_ROOT, env=env, pass_fds=(9,),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FullDatabaseError("candidate database verifier could not run") from exc
        if result.returncode != 0:
            raise FullDatabaseError("candidate database did not pass the required verifiers")
    _remove_empty_verification_scratch(root, scratch, scratch_identity)
    return dict(VERIFICATION)


def _copy_owned(root: Path, identifier: str, kind: str, source: Path, source_binding: dict[str, Any],
                source_stat: dict[str, int], source_permissions: dict[str, int], destination: Path,
                expected_permissions: dict[str, int], *, verify_candidate: bool) -> tuple[dict[str, Any], dict[str, int]]:
    """Copy and verify without replacing an unknown destination."""
    source, destination = _absolute(source), _absolute(destination)
    if source == destination:
        raise FullDatabaseError("source and owned database destination are identical")
    try:
        with manifest._directory(destination.parent, (), create=False) as directory:
            manifest._reject_sidecars(directory, destination.name)
            final_exists = _exists(directory, destination.name)
            claim = {"schema_version": 1, "kind": kind, "transaction_id": identifier,
                     "source": source_binding, "source_stat": source_stat,
                     "source_permissions": source_permissions, "destination": str(destination),
                     "destination_permissions": expected_permissions}
            _claim(root, identifier, kind, claim, final_exists=final_exists)
            if _snapshot(source) != (source_binding, source_stat, source_permissions):
                raise FullDatabaseError("source database changed before copy")
            if final_exists:
                current, current_stat, actual_permissions = _snapshot(destination)
                if not _same_contents(current, source_binding) or actual_permissions != expected_permissions:
                    raise FullDatabaseError("existing owned database differs")
                if verify_candidate:
                    _verify_candidate(root, destination, identifier)
                    if _snapshot(destination) != (current, current_stat, actual_permissions):
                        raise FullDatabaseError("candidate changed during verification")
                if _snapshot(source) != (source_binding, source_stat, source_permissions):
                    raise FullDatabaseError("source database changed during verification")
                # A previous attempt may have died after rename but before
                # syncing this directory. Establish that durability before
                # accepting the already-visible owned copy.
                os.fsync(directory)
                if (_snapshot(destination) != (current, current_stat, actual_permissions)
                        or _snapshot(source) != (source_binding, source_stat, source_permissions)):
                    raise FullDatabaseError("database changed while syncing its publication")
                return current, current_stat
            temporary = f"{_COPY_PREFIX}{identifier}-{kind}-" + secrets.token_hex(16) + ".db"
            with manifest._directory(source.parent, (), create=False) as source_directory:
                manifest._reject_sidecars(source_directory, source.name)
                source_fd = os.open(source.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=source_directory)
                try:
                    before = os.fstat(source_fd)
                    if (not stat.S_ISREG(before.st_mode) or manifest._db_stat(before) != source_stat
                            or {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid,
                                "gid": before.st_gid} != source_permissions):
                        raise FullDatabaseError("source database changed before copying")
                    target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                        0o600, dir_fd=directory)
                    try:
                        digest = hashlib.sha256()
                        while chunk := os.read(source_fd, 1024 * 1024):
                            digest.update(chunk)
                            offset = 0
                            while offset < len(chunk):
                                written = os.write(target_fd, chunk[offset:])
                                if written <= 0:
                                    raise FullDatabaseError("database copy write did not progress")
                                offset += written
                        _set_permissions(target_fd, expected_permissions)
                        os.fsync(target_fd)
                    finally:
                        os.close(target_fd)
                    after = os.fstat(source_fd)
                    named = os.stat(source.name, dir_fd=source_directory, follow_symlinks=False)
                    manifest._reject_sidecars(source_directory, source.name)
                    if (_identity(before) != _identity(after) or _identity(after) != _identity(named)
                            or digest.hexdigest() != source_binding["sha256"]):
                        raise FullDatabaseError("source database changed during copy")
                finally:
                    os.close(source_fd)
            stage_path = destination.parent / temporary
            staged, staged_stat, staged_permissions = _snapshot(stage_path)
            if not _same_contents(staged, source_binding) or staged_permissions != expected_permissions:
                raise FullDatabaseError("staged database differs")
            if verify_candidate:
                _verify_candidate(root, stage_path, identifier)
                if _snapshot(stage_path) != (staged, staged_stat, staged_permissions):
                    raise FullDatabaseError("candidate changed during verification")
            if _snapshot(source) != (source_binding, source_stat, source_permissions):
                raise FullDatabaseError("source database changed before publication")
            manifest._reject_sidecars(directory, destination.name)
            _publish(directory, temporary, destination.name)
            published, published_stat, published_permissions = _snapshot(destination)
            if not _same_contents(published, source_binding) or published_permissions != expected_permissions:
                raise FullDatabaseError("published database differs")
            if _snapshot(source) != (source_binding, source_stat, source_permissions):
                raise FullDatabaseError("source database changed during publication")
            return published, published_stat
    except FullDatabaseError:
        raise
    except (manifest.ManifestError, admission.AdmissionError, OSError, KeyError, TypeError, ValueError) as exc:
        raise FullDatabaseError("database copy could not be prepared") from exc


def prepare_candidate(root: Path, input_path: Path, transaction_id: str) -> dict[str, Any]:
    """Copy a caller-supplied candidate and run the real production verifiers."""
    root, identifier = _root(root), _identifier(transaction_id)
    source = _absolute(input_path)
    binding, db_stat, source_permissions = _snapshot(source)
    relative = f".deployments/{_CANDIDATES}/{identifier}.db"
    try:
        with manifest._directory(root, (".deployments", _CANDIDATES), create=True):
            pass
        destination = root / relative
        owned, owned_stat = _copy_owned(root, identifier, "candidate", source, binding, db_stat,
                                         source_permissions, destination,
                                         {"mode": 0o400, "uid": os.geteuid(), "gid": os.getegid()},
                                         verify_candidate=True)
        return {"path": relative, "binding": owned, "stat": owned_stat, "verification": dict(VERIFICATION)}
    except (manifest.ManifestError, OSError) as exc:
        raise FullDatabaseError("candidate database could not be prepared") from exc


def _open_metadata(directory: int, name: str) -> int | None:
    """Open one deployment metadata directory, reporting absence as ``None``."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None


def _published_transaction(root: Path, identifier: str) -> bool:
    """Report whether a published FULL journal already names this candidate."""
    import component_runtime as runtime  # noqa: PLC0415

    path = root / ".deployments" / _FULL_JOURNAL
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    try:
        record = manifest._decode(runtime._read_regular(path))
    except (runtime.JournalError, ValueError) as exc:
        raise FullDatabaseError("published full journal cannot be inspected") from exc
    if type(record) is not dict:
        raise FullDatabaseError("published full journal is not an object")
    return record.get("transaction_id") == identifier


def _claim_record(directory: int, name: str, identifier: str, destination: str) -> dict[str, Any] | None:
    """Read the ownership claim an interrupted preparation left behind."""
    if not _exists(directory, name):
        return None
    claim = manifest._decode(manifest._read_file(directory, name))
    if (type(claim) is not dict
            or set(claim) != {"schema_version", "kind", "transaction_id", "source", "source_stat",
                              "source_permissions", "destination", "destination_permissions"}
            or type(claim["schema_version"]) is not int or claim["schema_version"] != 1
            or claim["kind"] != "candidate" or claim["transaction_id"] != identifier
            or claim["destination"] != destination):
        raise FullDatabaseError("candidate ownership claim differs")
    return claim


def _discard_claimed_copy(directory: int, claim: dict[str, Any]) -> None:
    """Remove the one copy its claim authorizes, while it still matches it."""
    name = Path(claim["destination"]).name
    if not _exists(directory, name):
        return
    binding, _stat, permissions = _snapshot(Path(claim["destination"]))
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not _same_contents(binding, claim["source"])
                or permissions != claim["destination_permissions"]):
            raise FullDatabaseError("existing candidate copy differs from its claim")
    finally:
        os.close(fd)
    os.unlink(name, dir_fd=directory)
    os.fsync(directory)


def _discard_copy_temporaries(directory: int, identifier: str) -> None:
    """Remove only the copy temporaries named for this refused transaction."""
    prefix, suffix = f"{_COPY_PREFIX}{identifier}-candidate-", ".db"
    removed = False
    for name in os.listdir(directory):
        if not (type(name) is str and name.startswith(prefix) and name.endswith(suffix)):
            continue
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise FullDatabaseError("candidate copy temporary is not owned")
        os.unlink(name, dir_fd=directory)
        removed = True
    if removed:
        os.fsync(directory)


def discard_candidate(root: Path, transaction_id: str) -> None:
    """Remove a refused preparation's own, unreferenced candidate copy.

    The ownership claim is written before the copy, so it is the only proof an
    interrupted attempt leaves behind: a copy that still matches its claim goes,
    together with that claim and this transaction's own copy temporaries. A copy
    without a matching claim, or one a published journal still names, is kept
    for inspection.
    """
    root, identifier = _root(root), _identifier(transaction_id)
    try:
        if _published_transaction(root, identifier):
            raise FullDatabaseError("published transaction still references its candidate copy")
        destination = str(root / ".deployments" / _CANDIDATES / f"{identifier}.db")
        with manifest._directory(root, (".deployments",), create=False) as deployments:
            directory = _open_metadata(deployments, _CANDIDATES)
            claims = _open_metadata(deployments, _CLAIMS)
            try:
                claim_name = f"{identifier}-candidate.json"
                claim = (None if claims is None else
                         _claim_record(claims, claim_name, identifier, destination))
                if directory is not None:
                    if claim is None and _exists(directory, f"{identifier}.db"):
                        raise FullDatabaseError("existing candidate copy has no owned claim")
                    if claim is not None:
                        _discard_claimed_copy(directory, claim)
                    _discard_copy_temporaries(directory, identifier)
                if claims is not None and claim is not None:
                    os.unlink(claim_name, dir_fd=claims)
                    os.fsync(claims)
            finally:
                for handle in (directory, claims):
                    if handle is not None:
                        os.close(handle)
    except FullDatabaseError:
        raise
    except (manifest.ManifestError, admission.AdmissionError, OSError, KeyError, TypeError, ValueError) as exc:
        raise FullDatabaseError("candidate copy cannot be discarded") from exc


def _candidate_record(root: Path, candidate: Any, identifier: str) -> tuple[Path, dict[str, Any], dict[str, int]]:
    if type(candidate) is not dict or set(candidate) != {"path", "binding", "stat", "verification"}:
        raise FullDatabaseError("candidate record is invalid")
    relative = f".deployments/{_CANDIDATES}/{identifier}.db"
    verification = candidate["verification"]
    if (candidate["path"] != relative or type(verification) is not dict or verification != VERIFICATION
            or any(type(verification[key]) is not type(value) for key, value in VERIFICATION.items())):
        raise FullDatabaseError("candidate record does not match the transaction")
    recorded_stat = candidate["stat"]
    if (type(recorded_stat) is not dict
            or set(recorded_stat) != {"device", "inode", "size", "mtime_ns", "ctime_ns"}
            or any(type(value) is not int or value < 0 for value in recorded_stat.values())
            or recorded_stat["inode"] == 0):
        raise FullDatabaseError("candidate stat is invalid")
    source = root / relative
    binding, source_stat, source_permissions = _snapshot(source)
    if binding != candidate["binding"] or source_stat != recorded_stat:
        raise FullDatabaseError("owned candidate changed")
    try:
        with manifest._directory(root, (".deployments", _CLAIMS), create=False) as directory:
            raw = manifest._read_file(directory, f"{identifier}-candidate.json")
        claim = manifest._decode(raw)
        if (type(claim) is not dict or set(claim) != {"schema_version", "kind", "transaction_id", "source",
                                                      "source_stat", "source_permissions", "destination",
                                                      "destination_permissions"}
                or type(claim["schema_version"]) is not int or claim["schema_version"] != 1
                or claim["kind"] != "candidate" or claim["transaction_id"] != identifier
                or claim["destination"] != str(source) or claim["destination_permissions"] != source_permissions
                or not _same_contents(claim["source"], binding)):
            raise FullDatabaseError("candidate ownership claim differs")
        if (type(claim["source_stat"]) is not dict
                or set(claim["source_stat"]) != {"device", "inode", "size", "mtime_ns", "ctime_ns"}
                or any(type(value) is not int or value < 0 for value in claim["source_stat"].values())
                or type(claim["source_permissions"]) is not dict
                or set(claim["source_permissions"]) != {"mode", "uid", "gid"}
                or any(type(value) is not int or value < 0 for value in claim["source_permissions"].values())):
            raise FullDatabaseError("candidate source claim is invalid")
        return source, binding, source_stat
    except (manifest.ManifestError, OSError, KeyError, TypeError, ValueError) as exc:
        raise FullDatabaseError("candidate ownership cannot be verified") from exc


def verify_candidate(root: Path, candidate: dict[str, Any], transaction_id: str) -> None:
    """Re-run all verifiers against the exact claimed candidate under FD9."""
    root, identifier = _root(root), _identifier(transaction_id)
    source, binding, source_stat = _candidate_record(root, candidate, identifier)
    before = _snapshot(source)
    if before[:2] != (binding, source_stat):
        raise FullDatabaseError("candidate changed before verification")
    _verify_candidate(root, source, identifier)
    if _snapshot(source) != before:
        raise FullDatabaseError("candidate changed during verification")
    _candidate_record(root, candidate, identifier)


def prepare_backup(root: Path, old_binding: dict[str, Any], old_stat: dict[str, int],
                   transaction_id: str) -> dict[str, Any]:
    """Preserve exactly the recorded live DB, retaining its effective permissions."""
    root, identifier = _root(root), _identifier(transaction_id)
    try:
        source = _absolute(Path(old_binding["path"]))
        binding, current_stat, source_permissions = _snapshot(source)
        if binding != old_binding or current_stat != old_stat:
            raise FullDatabaseError("live database differs from its recorded preimage")
        decimal = str(int(identifier[17:], 16))
        relative = f"data/deployment-backups/terms.{identifier[:16]}-{decimal}.{binding['sha256']}.db"
        with manifest._directory(root, ("data", "deployment-backups"), create=True):
            pass
        destination = root / relative
        copied, copied_stat = _copy_owned(root, identifier, "backup", source, binding, current_stat,
                                          source_permissions, destination, source_permissions, verify_candidate=False)
        return {"path": relative, "sha256": copied["sha256"], "stat": copied_stat,
                "permissions": source_permissions}
    except FullDatabaseError:
        raise
    except (manifest.ManifestError, OSError, KeyError, TypeError, ValueError) as exc:
        raise FullDatabaseError("database backup could not be prepared") from exc


def prepare_stage(root: Path, candidate: dict[str, Any], live_path: Path, transaction_id: str) -> dict[str, Any]:
    """Create a same-filesystem candidate stage; the caller owns promotion."""
    root, identifier = _root(root), _identifier(transaction_id)
    try:
        source, binding, source_stat = _candidate_record(root, candidate, identifier)
        _, _, source_permissions = _snapshot(source)
        live = _absolute(live_path)
        live_binding, live_stat, live_permissions = _snapshot(live)
        destination = live.parent / (".component-full-promote-" + identifier + ".db")
        with manifest._directory(live.parent, (), create=False) as directory:
            if os.fstat(directory).st_dev != live_stat["device"]:
                raise FullDatabaseError("promotion stage is not on the live filesystem")
        copied, copied_stat = _copy_owned(root, identifier, "stage", source, binding, source_stat,
                                          source_permissions, destination, live_permissions, verify_candidate=False)
        if _snapshot(live) != (live_binding, live_stat, live_permissions):
            raise FullDatabaseError("live database changed while preparing the stage")
        return {"path": str(destination), "sha256": copied["sha256"], "stat": copied_stat,
                "permissions": live_permissions}
    except FullDatabaseError:
        raise
    except (manifest.ManifestError, OSError, KeyError, TypeError, ValueError) as exc:
        raise FullDatabaseError("database promotion stage could not be prepared") from exc
