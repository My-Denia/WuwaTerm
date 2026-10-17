#!/usr/bin/env python3
"""Durable journal, admission gate and recovery for full-data deployments.

The full-mode updater (``deploy/vps-update.sh``) replaces the live
terminology database, both serving containers and the deployment pointer.
A shell EXIT trap rolls ordinary failures back, but an uncatchable
termination (SIGKILL past the trap, host power loss) inside the promotion
window leaves the live database and the pointer on opposite sides of the
transaction with no process left to reconcile them.

This module owns that window:

* ``prepare`` durably records the transaction's preimages (old and new
  database hashes, backup, pointer, per-surface image bindings, target
  binding, tool fingerprint) BEFORE the first serving-binding mutation;
* ``phase`` advances the record as the deployment progresses, re-verifying
  the step's durable effect before it is claimed;
* ``check`` is the admission gate: ANY outstanding journal, including a
  nominally committed one, refuses every new deployment mode until the
  explicit recovery has verified the observed binding and cleared it;
* ``confirm`` verifies the observed NEW binding (pointer, manifest,
  database hash, both containers' image ids/refs/running states) and only
  then clears the journal;
* ``rollback-clear`` lets the shell's EXIT trap clear the journal only
  through the same observed-binding verification of the OLD side;
* ``recover`` classifies the host by OBSERVED state - not by the journal's
  phase marker - and either confirms the new binding or restores the
  recorded previous one. Any ambiguity keeps the journal and changes
  nothing further.

Everything here is driven from ``deploy/vps-update.sh`` under the shared
deployment lock. The tests exercise it against isolated fake-docker
harnesses with real durable file operations; nothing in this module is a
claim about real host power-loss behaviour or production state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.deployment_manifest import (  # noqa: E402
    ManifestError,
    durable_remove,
    durable_replace,
    publish_commit_pointer,
    sha256_file,
    verify_commit_pointer,
    verify_manifest,
)

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSACTION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9]+$")
CANDIDATE_RE = re.compile(r"^data/candidates/terms\.([0-9]{8}T[0-9]{6}Z-[0-9]+)\.db$")
BACKUP_RE = re.compile(
    r"^data/deployment-backups/terms\.([0-9]{8}T[0-9]{6}Z-[0-9]+)\.([0-9a-f]{64})\.db$"
)
MANIFEST_RE = re.compile(r"^\.deployments/([0-9a-f]{40})\.json$")
ROLLBACK_REF_RE = re.compile(r"^wuwaterm-runtime:rollback-([0-9]{8}T[0-9]{6}Z-[0-9]+)$")
ROLLBACK_API_REF_RE = re.compile(
    r"^wuwaterm-runtime:rollback-api-([0-9]{8}T[0-9]{6}Z-[0-9]+)$"
)

JOURNAL_NAME = ".full-transaction.json"
JOURNAL_SCHEMA_VERSION = 2
PHASES = ("armed", "promoted", "started", "published", "committed")
SERVICES = ("wuwaterm", "wuwaterm-api")
CONTAINERS = {"wuwaterm": "wuwaterm-bot", "wuwaterm-api": "wuwaterm-api"}
SURFACE_KEYS = {
    "wuwaterm": ("old_bot_image_id", "old_bot_ref", "old_bot_running", "rollback_image_ref"),
    "wuwaterm-api": (
        "old_api_image_id",
        "old_api_ref",
        "old_api_running",
        "rollback_api_image_ref",
    ),
}
JOURNAL_KEYS = {
    "schema_version",
    "transaction_id",
    "source_commit",
    "phase",
    "old_pointer_present",
    "old_pointer",
    "old_manifest_path",
    "db_backup",
    "old_db_sha256",
    "new_db_sha256",
    "candidate_path",
    "new_manifest_path",
    "old_bot_image_id",
    "old_bot_ref",
    "old_bot_running",
    "old_api_image_id",
    "old_api_ref",
    "old_api_running",
    "rollback_image_ref",
    "rollback_api_image_ref",
    "target_image_ref",
    "target_image_id",
    "target_image_digest",
    "target_image_revision",
    "tool_hash",
    "updated_utc",
}

_termination_requested = False


class FullUpdateError(RuntimeError):
    """An intentionally terse, non-sensitive transaction failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _on_termination(signum: int, _frame: object) -> None:
    global _termination_requested
    _termination_requested = True


def _check_termination() -> None:
    if _termination_requested:
        raise FullUpdateError("interrupted")


def _subprocess_options(env: dict[str, str] | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cwd": ROOT,
        "env": env,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    # FD 9 is held by vps-update.sh. Passing it to every external child keeps
    # the deployment lock held if this process dies while Docker is mutating.
    if os.name == "posix":
        options["pass_fds"] = (9,)
        options["start_new_session"] = True
    return options


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return process.communicate()
    process.kill()
    return process.communicate()


def _execute(
    command: list[str], *, env: dict[str, str] | None = None, timeout: float = 300.0
) -> tuple[int, str, str]:
    process = subprocess.Popen(command, **_subprocess_options(env))
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired:
                if _termination_requested:
                    _terminate_process(process)
                    raise FullUpdateError("interrupted") from None
    except subprocess.TimeoutExpired:
        _stdout, _stderr = _terminate_process(process)
        raise FullUpdateError("command_timeout") from None
    return process.returncode, stdout, stderr


def _run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: float = 300.0
) -> str:
    try:
        returncode, stdout, _stderr = _execute(command, env=env, timeout=timeout)
    except OSError:
        raise FullUpdateError("command_unavailable") from None
    if returncode != 0:
        raise FullUpdateError("command_failed")
    _check_termination()
    return stdout.strip()


def _run_output(command: list[str], *, timeout: float = 60.0) -> str:
    output = _run(command, timeout=timeout)
    if "\n" in output or not output:
        raise FullUpdateError("invalid_command_output")
    return output


def _require_held_lock() -> None:
    if os.name != "posix":
        raise FullUpdateError("deployment_lock_missing")
    try:
        lock_target = os.readlink("/proc/self/fd/9")
    except OSError:
        raise FullUpdateError("deployment_lock_missing") from None
    expected_lock = str((ROOT / ".deployments" / ".deployment.lock").resolve())
    if lock_target != expected_lock:
        raise FullUpdateError("deployment_lock_missing")
    try:
        import fcntl

        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        raise FullUpdateError("deployment_lock_missing") from None


def _journal_path() -> Path:
    return ROOT / ".deployments" / JOURNAL_NAME


def full_journal_exists() -> bool:
    """True when a full-transaction journal is present in ANY shape.

    The admission gate refuses on presence, not on parseability: a corrupt
    or symlinked record is still an outstanding transaction nobody has
    resolved, and the gate must not disagree with recovery about that.
    """

    path = _journal_path()
    return path.exists() or path.is_symlink()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_journal_atomic(payload: dict[str, Any]) -> None:
    path = _journal_path()
    directory = path.parent
    if not directory.is_dir() or directory.is_symlink():
        raise FullUpdateError("invalid_full_journal")
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _commit(value: object) -> str:
    if type(value) is not str or not COMMIT_RE.fullmatch(value):
        raise FullUpdateError("invalid_full_journal")
    return value


def _image_id(value: object) -> str:
    if type(value) is not str or not IMAGE_ID_RE.fullmatch(value):
        raise FullUpdateError("invalid_full_journal")
    return value


def _optional_image_id(value: object) -> str | None:
    if value is None:
        return None
    return _image_id(value)


def _surface_ref(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or any(character.isspace() for character in value)
    ):
        raise FullUpdateError("invalid_full_journal")
    return value


def _validate_journal(payload: object) -> dict[str, Any]:
    """Strict journal validation: exact keys, exact types, coupled invariants.

    Booleans must be real JSON booleans (a string ``"true"`` or an integer
    ``1`` is corruption, not a value), optional fields must be real nulls,
    and every recorded path must have its deterministic shape so that path
    confinement can be decided before anything is touched.
    """

    if type(payload) is not dict or set(payload) != JOURNAL_KEYS:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["schema_version"]) is not int or (
        payload["schema_version"] != JOURNAL_SCHEMA_VERSION
    ):
        raise FullUpdateError("invalid_full_journal")
    transaction_id = payload["transaction_id"]
    if type(transaction_id) is not str or not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise FullUpdateError("invalid_full_journal")
    source_commit = _commit(payload["source_commit"])
    if type(payload["phase"]) is not str or payload["phase"] not in PHASES:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["old_pointer_present"]) is not bool:
        raise FullUpdateError("invalid_full_journal")
    if payload["old_pointer_present"]:
        old_pointer = _commit(payload["old_pointer"])
        if payload["old_manifest_path"] != f".deployments/{old_pointer}.json":
            raise FullUpdateError("invalid_full_journal")
    else:
        if payload["old_pointer"] is not None or payload["old_manifest_path"] is not None:
            raise FullUpdateError("invalid_full_journal")
        old_pointer = None
    db_backup = payload["db_backup"]
    old_db_sha = payload["old_db_sha256"]
    if type(db_backup) is not str:
        raise FullUpdateError("invalid_full_journal")
    if db_backup == "none":
        if old_db_sha is not None:
            raise FullUpdateError("invalid_full_journal")
    else:
        if type(old_db_sha) is not str or not SHA256_RE.fullmatch(old_db_sha):
            raise FullUpdateError("invalid_full_journal")
        match = BACKUP_RE.fullmatch(db_backup)
        if match is None or match.group(1) != transaction_id:
            raise FullUpdateError("invalid_full_journal")
        # The backup filename itself carries the old database hash; the
        # recorded preimage must agree with it before the path is trusted.
        if match.group(2) != old_db_sha:
            raise FullUpdateError("invalid_full_journal")
    if type(payload["new_db_sha256"]) is not str or not SHA256_RE.fullmatch(
        payload["new_db_sha256"]
    ):
        raise FullUpdateError("invalid_full_journal")
    if type(payload["candidate_path"]) is not str:
        raise FullUpdateError("invalid_full_journal")
    candidate_match = CANDIDATE_RE.fullmatch(payload["candidate_path"])
    if candidate_match is None or candidate_match.group(1) != transaction_id:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["new_manifest_path"]) is not str or (
        payload["new_manifest_path"] != f".deployments/{source_commit}.json"
    ):
        raise FullUpdateError("invalid_full_journal")
    for image_key, ref_key, running_key in (
        ("old_bot_image_id", "old_bot_ref", "old_bot_running"),
        ("old_api_image_id", "old_api_ref", "old_api_running"),
    ):
        image_id = _optional_image_id(payload[image_key])
        ref = _surface_ref(payload[ref_key])
        if type(payload[running_key]) is not bool:
            raise FullUpdateError("invalid_full_journal")
        if (image_id is None) != (ref is None):
            raise FullUpdateError("invalid_full_journal")
        if image_id is None and payload[running_key]:
            raise FullUpdateError("invalid_full_journal")
    if type(payload["rollback_image_ref"]) is not str:
        raise FullUpdateError("invalid_full_journal")
    rollback_match = ROLLBACK_REF_RE.fullmatch(payload["rollback_image_ref"])
    if rollback_match is None or rollback_match.group(1) != transaction_id:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["rollback_api_image_ref"]) is not str:
        raise FullUpdateError("invalid_full_journal")
    rollback_api_match = ROLLBACK_API_REF_RE.fullmatch(payload["rollback_api_image_ref"])
    if rollback_api_match is None or rollback_api_match.group(1) != transaction_id:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["target_image_ref"]) is not str or (
        payload["target_image_ref"] != f"wuwaterm-runtime:{source_commit}"
    ):
        raise FullUpdateError("invalid_full_journal")
    _image_id(payload["target_image_id"])
    if type(payload["target_image_digest"]) is not str or not payload["target_image_digest"]:
        raise FullUpdateError("invalid_full_journal")
    if payload["target_image_revision"] != source_commit:
        raise FullUpdateError("invalid_full_journal")
    if type(payload["tool_hash"]) is not str or not SHA256_RE.fullmatch(payload["tool_hash"]):
        raise FullUpdateError("invalid_full_journal")
    updated_utc = payload["updated_utc"]
    if type(updated_utc) is not str:
        raise FullUpdateError("invalid_full_journal")
    try:
        parsed = datetime.fromisoformat(updated_utc.replace("Z", "+00:00"))
    except ValueError:
        raise FullUpdateError("invalid_full_journal") from None
    if parsed.tzinfo is None:
        raise FullUpdateError("invalid_full_journal")
    return payload


def _load_journal() -> dict[str, Any]:
    path = _journal_path()
    deployments = path.parent
    if not deployments.is_dir() or deployments.is_symlink():
        raise FullUpdateError("invalid_full_journal")
    if path.is_symlink() or not path.is_file():
        raise FullUpdateError("invalid_full_journal")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise FullUpdateError("invalid_full_journal") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise FullUpdateError("invalid_full_journal") from None
    return _validate_journal(payload)


def _confined(relative: object) -> Path:
    """Resolve a journal-recorded relative path inside the deploy root.

    The shape was already validated deterministically; this walk rejects
    anything that leaves the root through a symlinked component, so a
    hostile record can never point a delete or a copy at a file outside
    the deployment it claims to describe.
    """

    if type(relative) is not str or not relative:
        raise FullUpdateError("invalid_full_journal")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FullUpdateError("invalid_full_journal")
    current = ROOT.resolve()
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise FullUpdateError("full_journal_path_unconfined")
    return current


def _tool_hash() -> str:
    digest = hashlib.sha256()
    for relative in (
        "deploy/vps-update.sh",
        "deploy/docker-compose.yml",
        "scripts/deployment_manifest.py",
        "scripts/full_update.py",
    ):
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise FullUpdateError("tool_missing")
        digest.update(relative.encode("ascii"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _require_tool_match(journal: dict[str, Any]) -> None:
    if journal["tool_hash"] != _tool_hash():
        raise FullUpdateError("full_recovery_tool_mismatch")


def _docker_inspect(fmt: str, container: str) -> str:
    return _run_output(["docker", "inspect", "--format", fmt, container])


def _image_inspect(fmt: str, reference: str) -> str:
    return _run_output(["docker", "image", "inspect", "--format", fmt, reference])


_ABSENT_RE = re.compile(r"no such (?:object|container)", re.IGNORECASE)


def _run_allow_fail(
    command: list[str], *, timeout: float = 60.0
) -> tuple[int, str, str]:
    """Run a read-only docker command, returning its outcome without
    interpreting it. Only the spawn failing (docker unavailable) is an
    observation failure by itself."""

    try:
        return _execute(command, timeout=timeout)
    except OSError:
        raise FullUpdateError("full_recovery_unobservable") from None


def _image_ref_id(reference: str) -> str:
    """Resolve an image reference to the image id it currently names.

    A reference that resolves to nothing is known state ("unresolved"); a
    daemon that cannot answer is not observable ("unobservable"). Either
    way the caller must not run the reference.
    """

    returncode, stdout, stderr = _run_allow_fail(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference]
    )
    if returncode != 0:
        if _ABSENT_RE.search(stderr):
            raise FullUpdateError("full_recovery_unresolved")
        raise FullUpdateError("full_recovery_unobservable")
    value = stdout.strip()
    if not value or "\n" in value:
        raise FullUpdateError("full_recovery_unobservable")
    return value


def _container_state(container: str) -> tuple[bool, str, str] | None:
    """Observed (running, image id, image ref), or None when PROVEN absent.

    Absence counts only when the daemon positively says the object does not
    exist. A daemon/transport/permission failure, garbage output, or a
    running-state that is not exactly ``true``/``false`` means the surface
    cannot be observed: recovery must refuse rather than guess, because both
    quiescence and the final binding verifier depend on trustworthy eyes.
    """

    returncode, stdout, stderr = _run_allow_fail(
        ["docker", "inspect", "--format", "{{.State.Running}}", container]
    )
    if returncode != 0:
        if _ABSENT_RE.search(stderr):
            return None
        raise FullUpdateError("full_recovery_unobservable")
    running_raw = stdout.strip()
    if running_raw not in ("true", "false"):
        raise FullUpdateError("full_recovery_unobservable")
    try:
        image = _docker_inspect("{{.Image}}", container)
        reference = _docker_inspect("{{.Config.Image}}", container)
    except FullUpdateError as exc:
        raise FullUpdateError("full_recovery_unobservable") from exc
    return running_raw == "true", image, reference


def _runtime_env(reference: str, *, source_commit: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["SOURCE_COMMIT"] = source_commit if source_commit is not None else "unknown"
    env["WUWATERM_RUNTIME_IMAGE"] = reference
    return env


def _compose(
    *args: str, env: dict[str, str] | None = None, timeout: float = 300.0
) -> None:
    _run(
        ["docker", "compose", "-f", "deploy/docker-compose.yml", *args],
        env=env,
        timeout=timeout,
    )


def _pointer_state() -> tuple[bool, str | None]:
    """(present, value); value is None when present but unparseable."""

    path = ROOT / ".deploy_commit"
    if path.is_symlink():
        return True, None
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError:
        return True, None
    if (
        not value.endswith("\n")
        or value.count("\n") != 1
        or not COMMIT_RE.fullmatch(value[:-1])
    ):
        return True, None
    return True, value[:-1]


def _live_db_state(journal: dict[str, Any]) -> str:
    """Live database classification; "both" when old and new digests are equal.

    Equal old/new preimages are valid (a byte-identical "data update"), so
    the classification is a set of matches, not an exclusive pick.
    """

    data_dir = ROOT / "data"
    path = data_dir / "terms.db"
    if data_dir.is_symlink() or path.is_symlink():
        return "unknown"
    if any(
        path.with_name(path.name + suffix).exists() or path.with_name(path.name + suffix).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        return "unknown"
    if not path.exists():
        return "absent"
    try:
        digest = sha256_file(path)
    except OSError:
        return "unknown"
    old_match = journal["old_db_sha256"] is not None and digest == journal["old_db_sha256"]
    new_match = digest == journal["new_db_sha256"]
    if old_match and new_match:
        return "both"
    if old_match:
        return "old"
    if new_match:
        return "new"
    return "unknown"


def _classify(journal: dict[str, Any]) -> str:
    """Which side of the transaction the OBSERVED host is on.

    The live pointer decides the side; a pointer that is neither the
    recorded old nor the recorded new commit describes a state this
    journal does not know about, and so does a pointer that vanished when
    one was recorded, or one that cannot be parsed.
    """

    present, value = _pointer_state()
    if present:
        if value is None:
            return "unknown"
        if value == journal["source_commit"]:
            return "new"
        if journal["old_pointer_present"] and value == journal["old_pointer"]:
            return "old"
        return "unknown"
    if journal["old_pointer_present"]:
        return "unknown"
    return "old"


def _verify_new_binding(journal: dict[str, Any]) -> None:
    """Confirm the observed host matches the complete NEW binding.

    A published pointer alone is not success: the manifest must verify
    against the observed database, and BOTH containers must actually run
    the target image id under the target reference. Anything less is an
    unresolved mixed binding, never a clean confirmation.
    """

    present, value = _pointer_state()
    if not present or value != journal["source_commit"]:
        raise FullUpdateError("full_recovery_unresolved")
    db_path = ROOT / "data" / "terms.db"
    if _live_db_state(journal) not in ("new", "both"):
        raise FullUpdateError("full_recovery_unresolved")
    manifest_path = _confined(journal["new_manifest_path"])
    if not manifest_path.is_file():
        raise FullUpdateError("full_recovery_unresolved")
    try:
        verify_manifest(
            manifest_path,
            source_commit=journal["source_commit"],
            image_id=journal["target_image_id"],
            image_digest=journal["target_image_digest"],
            db_path=db_path,
            immutable=True,
        )
    except ManifestError:
        raise FullUpdateError("full_recovery_unresolved") from None
    for service in SERVICES:
        state = _container_state(CONTAINERS[service])
        if (
            state is None
            or not state[0]
            or state[1] != journal["target_image_id"]
            or state[2] != journal["target_image_ref"]
        ):
            raise FullUpdateError("full_recovery_unresolved")
    revision = _image_inspect(
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        journal["target_image_ref"],
    )
    if revision != journal["source_commit"]:
        raise FullUpdateError("full_recovery_unresolved")


def _verify_old_binding(journal: dict[str, Any]) -> None:
    """Confirm the observed host matches the complete OLD binding.

    Used by recovery after restoring it and by the shell's EXIT rollback
    before it may clear the journal: the pointer, the database hash (or
    the recorded absence), the old manifest and every recorded surface's
    observed image id/running state must agree. Only surfaces that were
    recorded running are expected back, each on its own recorded image.
    """

    present, value = _pointer_state()
    if journal["old_pointer_present"]:
        if not present or value != journal["old_pointer"]:
            raise FullUpdateError("full_recovery_unresolved")
    elif present:
        raise FullUpdateError("full_recovery_unresolved")
    db_state = _live_db_state(journal)
    expected_db_state = "old" if journal["old_db_sha256"] is not None else "absent"
    if db_state not in (expected_db_state, "both"):
        raise FullUpdateError("full_recovery_unresolved")
    if journal["old_pointer_present"]:
        manifest_path = _confined(journal["old_manifest_path"])
        if not manifest_path.is_file():
            raise FullUpdateError("full_recovery_unresolved")
        verify_id: str | None = None
        for image_key, running_key in (
            ("old_bot_image_id", "old_bot_running"),
            ("old_api_image_id", "old_api_running"),
        ):
            if journal[running_key] and journal[image_key]:
                verify_id = journal[image_key]
                break
        try:
            if journal["old_db_sha256"] is not None:
                if verify_id is None:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    verify_id = payload["image"]["id"]
                verify_manifest(
                    manifest_path,
                    source_commit=journal["old_pointer"],
                    image_id=verify_id,
                    image_digest=None,
                    db_path=ROOT / "data" / "terms.db",
                    immutable=True,
                )
            else:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                if verify_id is None:
                    verify_id = payload["image"]["id"]
                if (
                    payload.get("schema_version") != 1
                    or payload.get("source_commit") != journal["old_pointer"]
                    or payload.get("image", {}).get("id") != verify_id
                    or payload.get("image", {}).get("revision") != journal["old_pointer"]
                ):
                    raise FullUpdateError("full_recovery_unresolved")
        except (ManifestError, OSError, ValueError, KeyError, TypeError):
            raise FullUpdateError("full_recovery_unresolved") from None
        try:
            verify_commit_pointer(ROOT / ".deploy_commit", journal["old_pointer"])
        except ManifestError:
            raise FullUpdateError("full_recovery_unresolved") from None
    for service in SERVICES:
        image_key, _ref_key, running_key, rollback_key = SURFACE_KEYS[service]
        expected_running = bool(journal[running_key] and journal[image_key])
        state = _container_state(CONTAINERS[service])
        if expected_running:
            if (
                state is None
                or not state[0]
                or state[1] != journal[image_key]
                or state[2] != journal[rollback_key]
            ):
                raise FullUpdateError("full_recovery_unresolved")
        elif state is not None and state[0]:
            raise FullUpdateError("full_recovery_unresolved")


def _preflight_journal_paths(journal: dict[str, Any]) -> None:
    """Confinement, existence and recorded-preimage checks for every path
    the recovery may act on. Runs BEFORE any mutation: a journal that
    points outside the deploy root or at content that does not match the
    recorded preimage is refused while the host is still untouched."""

    if journal["old_db_sha256"] is not None:
        backup = _confined(journal["db_backup"])
        if not backup.is_file():
            raise FullUpdateError("full_recovery_backup_missing")
        try:
            if sha256_file(backup) != journal["old_db_sha256"]:
                raise FullUpdateError("full_recovery_backup_missing")
        except OSError:
            raise FullUpdateError("full_recovery_backup_missing") from None
    if journal["old_pointer_present"]:
        manifest = _confined(journal["old_manifest_path"])
        if not manifest.is_file():
            raise FullUpdateError("full_recovery_unresolved")
        _preflight_old_manifest(journal, manifest)


def _preflight_old_manifest(journal: dict[str, Any], path: Path) -> None:
    """Static binding of the recorded old manifest to the old preimage.

    The live database may still hold the new candidate here, so the deep
    database verification stays in the final observed-binding verifier; this
    preflight proves the manifest itself records exactly the old source,
    revision and recorded database hash, and an image id this transaction
    actually recorded, before anything is stopped, copied or started.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise FullUpdateError("full_recovery_unresolved") from None
    if type(payload) is not dict:
        raise FullUpdateError("full_recovery_unresolved")
    image = payload.get("image")
    database = payload.get("database")
    recorded_ids = {
        journal[key]
        for key in ("old_bot_image_id", "old_api_image_id")
        if journal[key]
    }
    if (
        payload.get("schema_version") != 1
        or payload.get("source_commit") != journal["old_pointer"]
        or not isinstance(image, dict)
        or image.get("revision") != journal["old_pointer"]
        or (recorded_ids and image.get("id") not in recorded_ids)
        or not isinstance(database, dict)
        or (
            journal["old_db_sha256"] is not None
            and database.get("sha256") != journal["old_db_sha256"]
        )
    ):
        raise FullUpdateError("full_recovery_unresolved")


def _assert_admissible_surface(
    journal: dict[str, Any], service: str, state: tuple[bool, str, str] | None
) -> None:
    """An observed surface must be one of THIS transaction's known identities.

    Legitimate intermediate states are the recorded old binding, the
    per-surface rollback binding (tag still resolving to the recorded old
    id), or the target/new binding — in any running state, including
    surfaces that differ per host or were never running. Anything else is
    unknown service state: recovery has no standing to overwrite it.
    """

    if state is None:
        return
    image_key, ref_key, _running_key, rollback_key = SURFACE_KEYS[service]
    image_id, reference = state[1], state[2]
    known = (
        (
            journal[image_key] is not None
            and image_id == journal[image_key]
            and reference == journal[ref_key]
        ),
        (
            journal[image_key] is not None
            and image_id == journal[image_key]
            and reference == journal[rollback_key]
        ),
        (
            image_id == journal["target_image_id"]
            and reference == journal["target_image_ref"]
        ),
    )
    if not any(known):
        raise FullUpdateError("full_recovery_unresolved")


def _preflight_old_side(journal: dict[str, Any]) -> None:
    """The admissibility contract, established BEFORE any mutation.

    Order matters: every actionable journal path is confined and consistent,
    every observed surface is a known transaction identity (absence only
    when positively proven), and every recorded rollback reference resolves
    to the exact per-surface old image id recorded for it — so a drifted
    tag can never execute. Refusal here leaves the host exactly as it was.
    """

    _preflight_journal_paths(journal)
    if _live_db_state(journal) not in ("old", "new", "both", "absent"):
        raise FullUpdateError("full_recovery_unresolved")
    for service in SERVICES:
        state = _container_state(CONTAINERS[service])
        _assert_admissible_surface(journal, service, state)
        image_key, _ref_key, _running_key, rollback_key = SURFACE_KEYS[service]
        if journal[image_key]:
            if _image_ref_id(journal[rollback_key]) != journal[image_key]:
                raise FullUpdateError("full_recovery_unresolved")


def _restore_database(journal: dict[str, Any]) -> None:
    """The ONE verified database restoration, shared by explicit recovery
    and the shell's EXIT rollback (``restore-db``): the recorded backup is
    hashed BEFORE any copy, the temporary copy is verified BEFORE it can
    replace the live file, and the promoted file is re-verified after.
    Any failure leaves the live database exactly as it was."""

    db_path = ROOT / "data" / "terms.db"
    db_state = _live_db_state(journal)
    if journal["old_db_sha256"] is not None:
        backup = _confined(journal["db_backup"])
        if not backup.is_file():
            raise FullUpdateError("full_recovery_backup_missing")
        try:
            if sha256_file(backup) != journal["old_db_sha256"]:
                raise FullUpdateError("full_recovery_backup_missing")
        except OSError:
            raise FullUpdateError("full_recovery_backup_missing") from None
        if db_state not in ("old", "new", "both", "absent"):
            # A live database this journal cannot name is unknown state:
            # it is left exactly as it is for an operator decision.
            raise FullUpdateError("full_recovery_unresolved")
        if db_state in ("new", "absent"):
            # "new" is the interrupted promotion; "absent" is a host that
            # lost the file before recovery ran. "both" needs no copy: the
            # live bytes already equal the recorded old preimage.
            temporary = db_path.with_name(
                f"{db_path.name}.recovery-{journal['transaction_id']}.tmp"
            )
            if temporary.exists() or temporary.is_symlink():
                durable_remove(temporary)
            try:
                shutil.copyfile(backup, temporary)
            except OSError:
                raise FullUpdateError("full_recovery_restore_failed") from None
            try:
                if sha256_file(temporary) != journal["old_db_sha256"]:
                    raise FullUpdateError("full_recovery_restore_failed")
                durable_replace(temporary, db_path)
                if sha256_file(db_path) != journal["old_db_sha256"]:
                    raise FullUpdateError("full_recovery_restore_failed")
            except OSError:
                raise FullUpdateError("full_recovery_restore_failed") from None
    else:
        if db_state == "new":
            durable_remove(db_path)
            if db_path.exists() or db_path.is_symlink():
                raise FullUpdateError("full_recovery_restore_failed")
        elif db_state not in ("absent",):
            raise FullUpdateError("full_recovery_unresolved")


def _restore_old_binding(journal: dict[str, Any]) -> None:
    """Bring the host back to the recorded previous binding, then verify it.

    The admissibility preflight (paths, observed identities, rollback-tag
    resolution) runs FIRST and mutates nothing; only a host this
    transaction can fully account for is touched. Order after that:
    nothing may serve while the database moves, so known surfaces are
    quiesced first and verified stopped; the database returns through the
    shared verified restoration; only recorded-running surfaces come
    back, each on its own recorded image with the tag re-resolved
    immediately before it runs; and the final observed binding is
    verified before the caller may clear the journal.
    """

    _preflight_old_side(journal)
    running = [
        service
        for service in SERVICES
        if (state := _container_state(CONTAINERS[service])) is not None and state[0]
    ]
    if running:
        try:
            _compose("stop", *SERVICES, timeout=120.0)
        except FullUpdateError:
            raise FullUpdateError("full_recovery_stop_failed") from None
        for service in SERVICES:
            state = _container_state(CONTAINERS[service])
            if state is not None and state[0]:
                raise FullUpdateError("full_recovery_stop_failed")
    _check_termination()

    _restore_database(journal)
    _check_termination()

    pointer_path = ROOT / ".deploy_commit"
    present, value = _pointer_state()
    if journal["old_pointer_present"]:
        if present and value == journal["source_commit"]:
            if pointer_path.is_symlink():
                raise FullUpdateError("full_recovery_unresolved")
            publish_commit_pointer(pointer_path, journal["old_pointer"])
            try:
                verify_commit_pointer(pointer_path, journal["old_pointer"])
            except ManifestError:
                raise FullUpdateError("full_recovery_unresolved") from None
        elif not present or value != journal["old_pointer"]:
            raise FullUpdateError("full_recovery_unresolved")
    else:
        if present and value == journal["source_commit"]:
            durable_remove(pointer_path)
            if pointer_path.exists() or pointer_path.is_symlink():
                raise FullUpdateError("full_recovery_restore_failed")
        elif present:
            raise FullUpdateError("full_recovery_unresolved")
    _check_termination()

    for service in SERVICES:
        image_key, _ref_key, running_key, rollback_key = SURFACE_KEYS[service]
        if journal[running_key] and journal[image_key]:
            # The tag was resolved in the preflight; re-resolve it
            # immediately before running so a tag retargeted in between
            # can never execute.
            if _image_ref_id(journal[rollback_key]) != journal[image_key]:
                raise FullUpdateError("full_recovery_unresolved")
            _compose(
                "up",
                "-d",
                "--no-build",
                "--force-recreate",
                service,
                env=_runtime_env(journal[rollback_key], source_commit=journal["old_pointer"]),
            )
        else:
            state = _container_state(CONTAINERS[service])
            if state is not None and state[0]:
                raise FullUpdateError("full_recovery_unresolved")
    _check_termination()
    _verify_old_binding(journal)


def _clear_journal(journal: dict[str, Any]) -> None:
    """Remove the confined stale candidate, then the journal itself.

    The journal is removed LAST: any earlier failure leaves the record in
    place so recovery stays reentrant. A candidate that cannot be resolved
    safely is kept rather than deleted through an untrusted path.
    """

    try:
        candidate = _confined(journal["candidate_path"])
        if candidate.exists() or candidate.is_symlink():
            durable_remove(candidate)
    except FullUpdateError:
        pass
    durable_remove(_journal_path())


def _load_manifest_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise FullUpdateError("invalid_full_journal") from None
    if type(payload) is not dict:
        raise FullUpdateError("invalid_full_journal")
    return payload


def cmd_prepare(args: argparse.Namespace) -> int:
    _require_held_lock()
    if full_journal_exists():
        raise FullUpdateError("full_journal_exists")
    transaction_id = args.transaction_id
    if type(transaction_id) is not str or not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise FullUpdateError("invalid_full_journal")
    source_commit = args.source_commit
    if type(source_commit) is not str or not COMMIT_RE.fullmatch(source_commit):
        raise FullUpdateError("invalid_full_journal")
    if args.target_image_ref != f"wuwaterm-runtime:{source_commit}":
        raise FullUpdateError("invalid_full_journal")
    _image_id(args.target_image_id)
    if not args.target_image_digest or args.target_image_revision != source_commit:
        raise FullUpdateError("invalid_full_journal")
    if args.rollback_image_ref != f"wuwaterm-runtime:rollback-{transaction_id}" or (
        args.rollback_api_image_ref != f"wuwaterm-runtime:rollback-api-{transaction_id}"
    ):
        raise FullUpdateError("invalid_full_journal")

    old_pointer_present = args.old_pointer_present == "1"
    old_pointer: str | None = args.old_pointer if old_pointer_present else None
    if old_pointer is not None and not COMMIT_RE.fullmatch(old_pointer):
        raise FullUpdateError("invalid_full_journal")
    old_db_sha: str | None = None if args.old_db_sha == "none" else args.old_db_sha
    if old_db_sha is not None and not SHA256_RE.fullmatch(old_db_sha):
        raise FullUpdateError("invalid_full_journal")
    if (args.db_backup == "none") != (old_db_sha is None):
        raise FullUpdateError("invalid_full_journal")
    if args.db_backup != "none" and BACKUP_RE.fullmatch(args.db_backup) is None:
        raise FullUpdateError("invalid_full_journal")
    if CANDIDATE_RE.fullmatch(args.candidate_path) is None:
        raise FullUpdateError("invalid_full_journal")

    parsed_surfaces: list[tuple[str | None, str | None, bool]] = []
    for image_arg, ref_arg, running_arg in (
        (args.old_image_id, args.old_ref, args.old_bot_running),
        (args.old_api_image_id, args.old_api_ref, args.old_api_running),
    ):
        image_id = None if image_arg == "none" else image_arg
        reference = None if ref_arg == "none" else ref_arg
        if image_id is None and reference is not None:
            raise FullUpdateError("invalid_full_journal")
        if image_id is not None:
            _image_id(image_id)
            if reference is None or not reference or len(reference) > 200:
                raise FullUpdateError("invalid_full_journal")
        running = running_arg == "true"
        if image_id is None and running:
            raise FullUpdateError("invalid_full_journal")
        parsed_surfaces.append((image_id, reference, running))
    (bot_id, bot_ref, bot_running), (api_id, api_ref, api_running) = parsed_surfaces

    present, value = _pointer_state()
    if old_pointer_present:
        if not present or value != old_pointer:
            raise FullUpdateError("pointer_mismatch")
        old_manifest = _confined(f".deployments/{old_pointer}.json")
        if not old_manifest.is_file():
            raise FullUpdateError("old_manifest_missing")
    elif present:
        raise FullUpdateError("pointer_mismatch")

    candidate = _confined(args.candidate_path)
    if not candidate.is_file():
        raise FullUpdateError("invalid_full_journal")
    new_db_sha = sha256_file(candidate)

    if args.db_backup != "none":
        backup = _confined(args.db_backup)
        if not backup.is_file():
            raise FullUpdateError("backup_verification_failed")
        if sha256_file(backup) != old_db_sha:
            raise FullUpdateError("backup_verification_failed")

    new_manifest = _confined(f".deployments/{source_commit}.json")
    if new_manifest.exists() or new_manifest.is_symlink():
        if not new_manifest.is_file():
            raise FullUpdateError("manifest_binding_mismatch")
        payload = _load_manifest_payload(new_manifest)
        image = payload.get("image")
        database = payload.get("database")
        if (
            not isinstance(image, dict)
            or not isinstance(database, dict)
            or payload.get("source_commit") != source_commit
            or image.get("ref") != args.target_image_ref
            or image.get("id") != args.target_image_id
            or image.get("digest") != args.target_image_digest
            or image.get("revision") != source_commit
            or database.get("sha256") != new_db_sha
        ):
            raise FullUpdateError("manifest_binding_mismatch")

    journal: dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "source_commit": source_commit,
        "phase": "armed",
        "old_pointer_present": old_pointer_present,
        "old_pointer": old_pointer,
        "old_manifest_path": f".deployments/{old_pointer}.json"
        if old_pointer_present
        else None,
        "db_backup": args.db_backup,
        "old_db_sha256": old_db_sha,
        "new_db_sha256": new_db_sha,
        "candidate_path": args.candidate_path,
        "new_manifest_path": f".deployments/{source_commit}.json",
        "old_bot_image_id": bot_id,
        "old_bot_ref": bot_ref,
        "old_bot_running": bot_running,
        "old_api_image_id": api_id,
        "old_api_ref": api_ref,
        "old_api_running": api_running,
        "rollback_image_ref": args.rollback_image_ref,
        "rollback_api_image_ref": args.rollback_api_image_ref,
        "target_image_ref": args.target_image_ref,
        "target_image_id": args.target_image_id,
        "target_image_digest": args.target_image_digest,
        "target_image_revision": args.target_image_revision,
        "tool_hash": _tool_hash(),
        "updated_utc": _now_utc(),
    }
    _validate_journal(journal)
    _write_journal_atomic(journal)
    print("full transaction journal prepared")
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    _require_held_lock()
    if args.phase not in PHASES:
        raise FullUpdateError("invalid_full_journal")
    journal = _load_journal()
    _require_tool_match(journal)
    if PHASES.index(args.phase) <= PHASES.index(journal["phase"]):
        raise FullUpdateError("invalid_full_journal")
    if args.phase == "promoted":
        if _live_db_state(journal) not in ("new", "both"):
            raise FullUpdateError("promoted_db_mismatch")
    elif args.phase == "published":
        manifest_path = _confined(journal["new_manifest_path"])
        if not manifest_path.is_file():
            raise FullUpdateError("manifest_binding_mismatch")
        try:
            verify_manifest(
                manifest_path,
                source_commit=journal["source_commit"],
                image_id=journal["target_image_id"],
                image_digest=journal["target_image_digest"],
                db_path=ROOT / "data" / "terms.db",
                immutable=True,
            )
        except ManifestError:
            raise FullUpdateError("manifest_binding_mismatch") from None
    elif args.phase == "committed":
        present, value = _pointer_state()
        if not present or value != journal["source_commit"]:
            raise FullUpdateError("pointer_mismatch")
    journal["phase"] = args.phase
    journal["updated_utc"] = _now_utc()
    _validate_journal(journal)
    _write_journal_atomic(journal)
    print(f"full transaction phase: {args.phase}")
    return 0


def cmd_confirm(_args: argparse.Namespace) -> int:
    _require_held_lock()
    if not full_journal_exists():
        raise FullUpdateError("invalid_full_journal")
    journal = _load_journal()
    _require_tool_match(journal)
    _verify_new_binding(journal)
    _clear_journal(journal)
    print("full transaction confirmed on the new binding; journal cleared")
    return 0


def cmd_rollback_clear(_args: argparse.Namespace) -> int:
    """Clear the journal only through the observed-binding verifier.

    The shell's EXIT trap restores the previous binding itself; this
    command is the only way that rollback may drop the durable record,
    and it does so by verifying the SAME observed old binding (pointer,
    database, manifest, both surfaces' image ids and running states) that
    explicit recovery verifies. A rollback that cannot prove its outcome
    keeps the journal for `--recover-full`.
    """

    _require_held_lock()
    if not full_journal_exists():
        # Failures before the journal was armed have no record to clear.
        return 0
    journal = _load_journal()
    _require_tool_match(journal)
    _verify_old_binding(journal)
    _clear_journal(journal)
    print("rollback verified against the observed previous binding; journal cleared")
    return 0


def cmd_recover(_args: argparse.Namespace) -> int:
    _require_held_lock()
    if not full_journal_exists():
        print("no full transaction to recover")
        return 0
    journal = _load_journal()
    _require_tool_match(journal)
    side = _classify(journal)
    if side == "new":
        _verify_new_binding(journal)
        _clear_journal(journal)
        print("full transaction confirmed on the new binding; journal cleared")
        return 0
    if side == "old":
        _restore_old_binding(journal)
        _clear_journal(journal)
        print("full transaction rolled back to the previous binding")
        return 0
    raise FullUpdateError("full_recovery_unresolved")


def cmd_check(_args: argparse.Namespace) -> int:
    _require_held_lock()
    if full_journal_exists():
        raise FullUpdateError("full_recovery_required")
    return 0


def cmd_restore_db(_args: argparse.Namespace) -> int:
    """The shared verified database restoration (backup and copied bytes
    are hashed BEFORE the live file can be replaced).

    The shell's EXIT rollback calls this after quiescing the serving
    surfaces, so the recovery contract cannot diverge between explicit
    recovery and in-process rollback: same journal, same confinement, same
    verified restore. Never touches services, the pointer, or the journal.
    """

    _require_held_lock()
    if not full_journal_exists():
        raise FullUpdateError("invalid_full_journal")
    journal = _load_journal()
    _require_tool_match(journal)
    _preflight_journal_paths(journal)
    _restore_database(journal)
    print("verified database restoration complete")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--transaction-id", required=True)
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--old-pointer-present", required=True, choices=["0", "1"])
    prepare.add_argument("--old-pointer", required=True)
    prepare.add_argument("--db-backup", required=True)
    prepare.add_argument("--old-db-sha", required=True)
    prepare.add_argument("--candidate-path", required=True)
    prepare.add_argument("--old-image-id", required=True)
    prepare.add_argument("--old-api-image-id", required=True)
    prepare.add_argument("--old-ref", required=True)
    prepare.add_argument("--old-api-ref", required=True)
    prepare.add_argument("--old-bot-running", required=True, choices=["true", "false"])
    prepare.add_argument("--old-api-running", required=True, choices=["true", "false"])
    prepare.add_argument("--rollback-image-ref", required=True)
    prepare.add_argument("--rollback-api-image-ref", required=True)
    prepare.add_argument("--target-image-ref", required=True)
    prepare.add_argument("--target-image-id", required=True)
    prepare.add_argument("--target-image-digest", required=True)
    prepare.add_argument("--target-image-revision", required=True)

    phase = commands.add_parser("phase")
    phase.add_argument("phase", choices=list(PHASES))

    commands.add_parser("confirm")
    commands.add_parser("rollback-clear")
    commands.add_parser("recover")
    commands.add_parser("restore-db")
    commands.add_parser("check")

    args = parser.parse_args(argv)
    _require_held_lock()
    signal.signal(signal.SIGINT, _on_termination)
    signal.signal(signal.SIGTERM, _on_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_termination)
    handlers = {
        "prepare": cmd_prepare,
        "phase": cmd_phase,
        "confirm": cmd_confirm,
        "rollback-clear": cmd_rollback_clear,
        "recover": cmd_recover,
        "restore-db": cmd_restore_db,
        "check": cmd_check,
    }
    try:
        return handlers[args.command](args)
    except FullUpdateError as exc:
        print(f"full-data transaction failed: {exc.code}", file=sys.stderr)
        if os.environ.get("WUWATERM_RUNTIME_TRACEBACK"):
            import traceback

            traceback.print_exc()
        return 1
    except Exception:
        print("full-data transaction failed: internal_error", file=sys.stderr)
        if os.environ.get("WUWATERM_RUNTIME_TRACEBACK"):
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
