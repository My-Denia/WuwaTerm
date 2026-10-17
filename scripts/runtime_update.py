#!/usr/bin/env python3
"""Transactional runtime-only updater for the two serving containers.

This path deliberately keeps ``data/terms.db`` out of the transaction.  It
updates the runtime image and the immutable deployment pointer while treating
the existing database as a read-only binding that must remain byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.deployment_manifest import (  # noqa: E402
    ManifestError,
    build_manifest,
    read_db_provenance,
    sha256_file,
    verify_commit_pointer,
    verify_manifest,
    write_manifest,
)
from scripts.full_update import full_journal_exists  # noqa: E402


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
JOURNAL_NAME = ".runtime-transaction.json"
JOURNAL_SCHEMA_VERSION = 1
RUNTIME_BACKUP_SENTINEL = "none"
SERVICES = ("wuwaterm", "wuwaterm-api")
CONTAINERS = {"wuwaterm": "wuwaterm-bot", "wuwaterm-api": "wuwaterm-api"}
JOURNAL_KEYS = {
    "schema_version",
    "mode",
    "status",
    "phase",
    "transaction_id",
    "source_commit",
    "target_image_ref",
    "target_image_id",
    "target_image_digest",
    "target_image_revision",
    "old_pointer",
    "old_manifest_path",
    "old_image_ref",
    "old_image_id",
    "old_bot_running",
    "old_api_running",
    "rollback_image_ref",
    "db_sha256",
    "db_stat",
    "tool_hash",
}
EXPECTED_DB_SCHEMA = {
    "metadata": (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ),
    "terms": (
        ("id", "INTEGER", 0, 1),
        ("category", "TEXT", 1, 0),
        ("source_file", "TEXT", 1, 0),
        ("source_id", "TEXT", 1, 0),
        ("text_key", "TEXT", 1, 0),
        ("zh", "TEXT", 1, 0),
        ("en", "TEXT", 1, 0),
        ("zh_norm", "TEXT", 1, 0),
        ("en_norm", "TEXT", 1, 0),
        ("pinyin", "TEXT", 1, 0),
        ("pinyin_abbrev", "TEXT", 1, 0),
        ("priority", "INTEGER", 1, 0),
    ),
}
EXPECTED_DB_INDEXES = {
    "metadata": {"sqlite_autoindex_metadata_1": (1, "pk")},
    "terms": {
        "idx_terms_category": (0, "c"),
        "idx_terms_pinyin": (0, "c"),
        "idx_terms_en_norm": (0, "c"),
        "idx_terms_zh_norm": (0, "c"),
        "sqlite_autoindex_terms_1": (1, "u"),
    },
}
_termination_requested = False
_in_rollback = False
_termination_signal: int | None = None


class RuntimeUpdateError(RuntimeError):
    """An intentionally terse, non-sensitive transaction failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _on_termination(signum: int, _frame: object) -> None:
    global _termination_requested, _termination_signal
    if _in_rollback:
        return
    _termination_requested = True
    _termination_signal = signum


def _check_termination() -> None:
    if _termination_requested:
        raise RuntimeUpdateError("interrupted")


def _subprocess_options(
    *, env: dict[str, str] | None = None, timeout: float = 300.0
) -> dict[str, Any]:
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
    process = subprocess.Popen(command, **_subprocess_options(env=env, timeout=timeout))
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
                if _termination_requested and not _in_rollback:
                    _terminate_process(process)
                    raise RuntimeUpdateError("interrupted") from None
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_process(process)
        raise RuntimeUpdateError("command_timeout") from None
    return process.returncode, stdout, stderr


def _run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: float = 300.0
) -> str:
    try:
        returncode, stdout, _stderr = _execute(
            command, env=env, timeout=timeout
        )
    except OSError:
        raise RuntimeUpdateError("command_unavailable") from None
    if returncode != 0:
        raise RuntimeUpdateError("command_failed")
    _check_termination()
    return stdout.strip()


def _run_quiet(
    command: list[str], *, env: dict[str, str] | None = None, timeout: float = 300.0
) -> bool:
    try:
        returncode, _stdout, _stderr = _execute(
            command, env=env, timeout=timeout
        )
    except (OSError, RuntimeUpdateError):
        return False
    if returncode != 0:
        return False
    _check_termination()
    return True


def _run_output(
    command: list[str], *, env: dict[str, str] | None = None, allow_empty: bool = False
) -> str:
    output = _run(command, env=env, timeout=60.0)
    if "\n" in output or not output:
        if allow_empty and not output:
            return output
        raise RuntimeUpdateError("invalid_command_output")
    return output


def _commit(value: object) -> str:
    if type(value) is not str or not COMMIT_RE.fullmatch(value):
        raise RuntimeUpdateError("invalid_commit")
    return value


def _image_id(value: object) -> str:
    if type(value) is not str or not IMAGE_ID_RE.fullmatch(value):
        raise RuntimeUpdateError("invalid_image_id")
    return value


def _journal_path() -> Path:
    return ROOT / ".deployments" / JOURNAL_NAME


def _require_held_lock() -> None:
    if os.name != "posix":
        raise RuntimeUpdateError("deployment_lock_missing")
    try:
        lock_target = os.readlink("/proc/self/fd/9")
    except OSError:
        raise RuntimeUpdateError("deployment_lock_missing") from None
    expected_lock = str((ROOT / ".deployments" / ".deployment.lock").resolve())
    if lock_target != expected_lock:
        raise RuntimeUpdateError("deployment_lock_missing")
    try:
        import fcntl

        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        raise RuntimeUpdateError("deployment_lock_missing") from None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _set_journal(journal: dict[str, Any], *, phase: str, status: str | None = None) -> None:
    journal["phase"] = phase
    if status is not None:
        journal["status"] = status
    _write_json_atomic(_journal_path(), journal)


def _load_journal() -> dict[str, Any]:
    path = _journal_path()
    if not path.is_file() or path.is_symlink():
        raise RuntimeUpdateError("invalid_journal")
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeUpdateError("invalid_journal") from None
    if type(payload) is not dict or set(payload) != JOURNAL_KEYS:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != JOURNAL_SCHEMA_VERSION:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["mode"]) is not str or payload["mode"] != "runtime-only":
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["status"]) is not str or payload["status"] not in {
        "active",
        "unresolved",
        "rolled_back",
        "committed",
    }:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["phase"]) is not str or payload["phase"] not in {
        "prepared",
        "rollback_tag",
        "image_build",
        "image_ready",
        "stopping",
        "starting",
        "health",
        "manifest",
        "pointer",
        "rollback_quiesce",
        "rollback_restore",
        "recovery_rollback_tag",
        "rollback_pointer",
        "rolled_back",
        "committed",
        "recovery_quiesce",
        "recovery_restore",
        "recovery_pointer",
        "recovered",
        "unresolved",
    }:
        raise RuntimeUpdateError("invalid_journal")
    _commit(payload["source_commit"])
    _commit(payload["old_pointer"])
    if type(payload["old_manifest_path"]) is not str:
        raise RuntimeUpdateError("invalid_journal")
    old_manifest_name = payload["old_manifest_path"].split("/")[-1]
    if not old_manifest_name.endswith(".json"):
        raise RuntimeUpdateError("invalid_journal")
    _commit(old_manifest_name.removesuffix(".json"))
    if type(payload["transaction_id"]) is not str or not re.fullmatch(
        r"[0-9a-f]{12}-[0-9]+", payload["transaction_id"]
    ):
        raise RuntimeUpdateError("invalid_journal")
    if payload["target_image_ref"] != f"wuwaterm-runtime:{payload['source_commit']}":
        raise RuntimeUpdateError("invalid_journal")
    if payload["target_image_revision"] != payload["source_commit"]:
        raise RuntimeUpdateError("invalid_journal")
    _image_id(payload["old_image_id"])
    _image_id(payload["target_image_id"])
    if type(payload["old_bot_running"]) is not bool or type(
        payload["old_api_running"]
    ) is not bool:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["target_image_digest"]) is not str or not payload["target_image_digest"]:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["db_sha256"]) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", payload["db_sha256"]
    ):
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["db_stat"]) is not dict or set(payload["db_stat"]) != {
        "ino",
        "size",
        "mtime_ns",
        "ctime_ns",
    }:
        raise RuntimeUpdateError("invalid_journal")
    if any(
        type(payload["db_stat"][key]) is not int
        or payload["db_stat"][key] < 0
        for key in payload["db_stat"]
    ):
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["old_manifest_path"]) is not str or payload["old_manifest_path"] != (
        f".deployments/{payload['old_pointer']}.json"
    ):
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["old_image_ref"]) is not str or type(payload["target_image_ref"]) is not str:
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["rollback_image_ref"]) is not str or not re.fullmatch(
        r"wuwaterm-runtime:rollback-runtime-[0-9a-f]{12}-[0-9]+",
        payload["rollback_image_ref"],
    ):
        raise RuntimeUpdateError("invalid_journal")
    if type(payload["tool_hash"]) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", payload["tool_hash"]
    ):
        raise RuntimeUpdateError("invalid_journal")
    return payload


def _tool_hash() -> str:
    digest = hashlib.sha256()
    for relative in (
        "deploy/vps-update.sh",
        "deploy/docker-compose.yml",
        "scripts/deployment_manifest.py",
        "scripts/runtime_update.py",
    ):
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeUpdateError("tool_missing")
        digest.update(relative.encode("ascii"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _db_stat(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "ino": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


def _has_sidecars(path: Path) -> bool:
    return any(sidecar.exists() or sidecar.is_symlink() for sidecar in _sidecars(path))


def _database_snapshot() -> dict[str, Any]:
    data_root = ROOT / "data"
    if not data_root.is_dir() or data_root.is_symlink():
        raise RuntimeUpdateError("invalid_database")
    path = data_root / "terms.db"
    if not path.is_file() or path.is_symlink():
        raise RuntimeUpdateError("invalid_database")
    if _has_sidecars(path):
        raise RuntimeUpdateError("database_sidecar")
    try:
        before_stat = _db_stat(path)
        provenance = read_db_provenance(path, immutable=True)
        database_uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(database_uri, uri=True)
        try:
            integrity = tuple(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            )
            schema_errors = []
            for table, expected_columns in EXPECTED_DB_SCHEMA.items():
                actual_columns = tuple(
                    (
                        str(row[1]),
                        str(row[2]),
                        int(row[3]),
                        int(row[5]),
                    )
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if actual_columns != expected_columns:
                    schema_errors.append(table)
                actual_indexes = {
                    str(row[1]): (int(row[2]), str(row[3]))
                    for row in connection.execute(f"PRAGMA index_list({table})")
                }
                if actual_indexes != EXPECTED_DB_INDEXES[table]:
                    schema_errors.append(f"{table}:indexes")
        finally:
            connection.close()
        database_hash = sha256_file(path)
        after_stat = _db_stat(path)
    except (OSError, sqlite3.Error, ManifestError):
        raise RuntimeUpdateError("invalid_database") from None
    if (
        integrity != ("ok",)
        or tables != ("metadata", "terms")
        or schema_errors
    ):
        raise RuntimeUpdateError("invalid_database")
    if before_stat != after_stat:
        raise RuntimeUpdateError("database_changed")
    snapshot = {
        "sha256": database_hash,
        "stat": after_stat,
        "provenance": provenance,
    }
    if _has_sidecars(path):
        raise RuntimeUpdateError("database_sidecar")
    return snapshot


def _assert_database_unchanged(snapshot: dict[str, Any]) -> None:
    current = _database_snapshot()
    if current["sha256"] != snapshot["sha256"] or current["stat"] != snapshot["stat"]:
        raise RuntimeUpdateError("database_changed")


def _git_preflight() -> str:
    if _run_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], allow_empty=True
    ):
        raise RuntimeUpdateError("dirty_checkout")
    _run(["git", "fetch", "--quiet", "origin", "main:refs/remotes/origin/main"])
    head = _commit(_run_output(["git", "rev-parse", "HEAD"]))
    remote = _commit(_run_output(["git", "rev-parse", "refs/remotes/origin/main"]))
    if head != remote:
        raise RuntimeUpdateError("head_mismatch")
    return head


def _docker_inspect(fmt: str, container: str) -> str:
    return _run_output(["docker", "inspect", "--format", fmt, container])


def _image_inspect(fmt: str, reference: str) -> str:
    return _run_output(["docker", "image", "inspect", "--format", fmt, reference])


def _container_binding(
    service: str,
    *,
    expected_ref: str,
    expected_image_id: str,
    expected_revision: str,
) -> None:
    if _docker_inspect("{{.State.Running}}", service) != "true":
        raise RuntimeUpdateError("service_not_running")
    actual_id = _image_id(_docker_inspect("{{.Image}}", service))
    actual_ref = _docker_inspect("{{.Config.Image}}", service)
    if actual_id != expected_image_id or actual_ref != expected_ref:
        raise RuntimeUpdateError("service_binding_mismatch")
    mounts_raw = _docker_inspect("{{json .Mounts}}", service)
    try:
        mounts = json.loads(mounts_raw)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeUpdateError("mount_mismatch") from None
    if type(mounts) is not list or any(type(item) is not dict for item in mounts):
        raise RuntimeUpdateError("mount_mismatch")
    data_mounts = [item for item in mounts if item.get("Destination") == "/app/data"]
    if len(data_mounts) != 1:
        raise RuntimeUpdateError("mount_mismatch")
    mount = data_mounts[0]
    data_root = str((ROOT / "data").resolve())
    if not (
        mount.get("Type") == "bind"
        and mount.get("Source") == data_root
        and mount.get("Destination") == "/app/data"
        and mount.get("RW") is False
        and mount.get("Mode") == "ro"
    ):
        raise RuntimeUpdateError("mount_mismatch")
    revision = _image_inspect(
        "{{index .Config.Labels \"org.opencontainers.image.revision\"}}",
        expected_ref,
    )
    if revision != expected_revision:
        raise RuntimeUpdateError("image_revision_mismatch")


def _image_binding(reference: str) -> tuple[str, str, str]:
    image_id = _image_id(_image_inspect("{{.Id}}", reference))
    revision = _commit(
        _image_inspect("{{index .Config.Labels \"org.opencontainers.image.revision\"}}", reference)
    )
    try:
        digest = _image_inspect(
            "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}",
            reference,
        )
    except RuntimeUpdateError:
        digest = image_id
    if not digest:
        digest = image_id
    return image_id, digest, revision


def _assert_services(
    *, expected_ref: str, expected_image_id: str, expected_revision: str
) -> None:
    for service in SERVICES:
        _container_binding(
            CONTAINERS[service],
            expected_ref=expected_ref,
            expected_image_id=expected_image_id,
            expected_revision=expected_revision,
        )


def _observed_service_ref(allowed_refs: set[str]) -> str:
    refs = tuple(
        _docker_inspect("{{.Config.Image}}", CONTAINERS[service])
        for service in SERVICES
    )
    if len(set(refs)) != 1 or refs[0] not in allowed_refs:
        raise RuntimeUpdateError("service_binding_mismatch")
    return refs[0]


def _assert_directories() -> None:
    deployments = ROOT / ".deployments"
    if not deployments.is_dir() or deployments.is_symlink():
        raise RuntimeUpdateError("deployment_metadata_missing")
    env_file = ROOT / ".env"
    if (
        not env_file.is_file()
        or env_file.is_symlink()
        or env_file.stat().st_mode & 0o777 != 0o600
    ):
        raise RuntimeUpdateError("invalid_env_permissions")
    for relative in ("state", "state-api"):
        path = ROOT / relative
        if not path.is_dir() or path.is_symlink():
            raise RuntimeUpdateError("state_directory_missing")
    if (ROOT / "state" / "api" / "devices.db").exists():
        raise RuntimeUpdateError("legacy_device_store")


def _manifest(path: Path, *, immutable: bool = True) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeUpdateError("invalid_manifest")
    if path.stat().st_mode & 0o222:
        raise RuntimeUpdateError("invalid_manifest")
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeUpdateError("invalid_manifest") from None
    if type(payload) is not dict:
        raise RuntimeUpdateError("invalid_manifest")
    return payload


def _verify_existing_manifest(
    path: Path, *, source_commit: str, db_path: Path, immutable: bool = True
) -> dict[str, Any]:
    payload = _manifest(path, immutable=immutable)
    image = payload.get("image")
    if type(image) is not dict:
        raise RuntimeUpdateError("invalid_manifest")
    image_id = _image_id(image.get("id"))
    try:
        verify_manifest(
            path,
            source_commit=source_commit,
            image_id=image_id,
            image_digest=image.get("digest"),
            db_path=db_path,
            immutable=immutable,
        )
    except (ManifestError, RuntimeUpdateError):
        raise RuntimeUpdateError("invalid_manifest") from None
    return payload


def _journal_from_preflight(
    *,
    source_commit: str,
    old_pointer: str,
    old_manifest: dict[str, Any],
    db_snapshot: dict[str, Any],
    old_bot_running: bool,
    old_api_running: bool,
    target_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image = old_manifest.get("image")
    if type(image) is not dict:
        raise RuntimeUpdateError("invalid_manifest")
    old_image_id = _image_id(image.get("id"))
    old_ref = image.get("ref")
    if type(old_ref) is not str or not old_ref:
        raise RuntimeUpdateError("invalid_manifest")
    transaction_id = f"{source_commit[:12]}-{os.getpid()}"
    target_ref = f"wuwaterm-runtime:{source_commit}"
    target_image = (
        target_manifest.get("image") if target_manifest is not None else None
    )
    if type(target_image) is not dict:
        target_image = {
            "ref": target_ref,
            "id": old_image_id,
            "digest": old_image_id,
            "revision": source_commit,
        }
    if any(type(target_image.get(key)) is not str for key in ("ref", "id", "digest", "revision")):
        raise RuntimeUpdateError("invalid_manifest")
    if target_image["ref"] != target_ref:
        raise RuntimeUpdateError("manifest_binding_mismatch")
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "mode": "runtime-only",
        "status": "active",
        "phase": "prepared",
        "transaction_id": transaction_id,
        "source_commit": source_commit,
        "target_image_ref": target_image["ref"],
        "target_image_id": _image_id(target_image["id"]),
        "target_image_digest": target_image["digest"],
        "target_image_revision": _commit(target_image["revision"]),
        "old_pointer": old_pointer,
        "old_manifest_path": f".deployments/{old_pointer}.json",
        "old_image_ref": old_ref,
        "old_image_id": old_image_id,
        "old_bot_running": old_bot_running,
        "old_api_running": old_api_running,
        "rollback_image_ref": f"wuwaterm-runtime:rollback-runtime-{transaction_id}",
        "db_sha256": db_snapshot["sha256"],
        "db_stat": db_snapshot["stat"],
        "tool_hash": _tool_hash(),
    }


def _compose(
    *args: str, env: dict[str, str] | None = None, timeout: float = 300.0
) -> None:
    _run(
        ["docker", "compose", "-f", "deploy/docker-compose.yml", *args],
        env=env,
        timeout=timeout,
    )


def _compose_ok(
    *args: str, env: dict[str, str] | None = None, timeout: float = 300.0
) -> bool:
    return _run_quiet(
        ["docker", "compose", "-f", "deploy/docker-compose.yml", *args],
        env=env,
        timeout=timeout,
    )


def _ensure_rollback_alias(journal: dict[str, Any], *, phase: str) -> None:
    binding = _current_image_exists(journal["rollback_image_ref"])
    if binding is None:
        _set_journal(journal, phase=phase, status="unresolved")
        _run(
            [
                "docker",
                "image",
                "tag",
                journal["old_image_id"],
                journal["rollback_image_ref"],
            ]
        )
        binding = _image_binding(journal["rollback_image_ref"])
    rollback_id, _rollback_digest, rollback_revision = binding
    if (
        rollback_id != journal["old_image_id"]
        or rollback_revision != journal["old_pointer"]
    ):
        raise RuntimeUpdateError("rollback_image_mismatch")


def _runtime_env(reference: str, *, source_commit: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if source_commit is None:
        source_commit = reference.rsplit(":", 1)[-1]
    env["SOURCE_COMMIT"] = _commit(source_commit)
    env["WUWATERM_RUNTIME_IMAGE"] = reference
    return env


def _local_health() -> None:
    # Both checks execute inside the corresponding container and never call
    # Telegram, a provider, or a model endpoint.
    _compose(
        "exec",
        "-T",
        "wuwaterm-api",
        "python",
        "-c",
        "import os, time, urllib.error, urllib.request; port=os.environ.get('WUWATERM_API_PORT', '8788'); valid=port.isdigit() and len(port) <= 5 and 1 <= int(port) <= 65535; deadline=time.monotonic() + 60; ok=False\nif valid:\n    while time.monotonic() < deadline:\n        try:\n            r=urllib.request.urlopen('http://127.0.0.1:' + port + '/readyz', timeout=5); ok=(r.status == 200); break\n        except (OSError, urllib.error.URLError):\n            time.sleep(1)\nraise SystemExit(0 if ok else 1)",
    )
    _compose(
        "exec",
        "-T",
        "wuwaterm",
        "python",
        "-c",
        "import os; raise SystemExit(0 if os.path.isfile('/app/data/terms.db') else 1)",
    )


def _pointer(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError:
        raise RuntimeUpdateError("invalid_pointer") from None
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeUpdateError("invalid_pointer") from None
    if (
        not value.endswith("\n")
        or value.count("\n") != 1
        or not COMMIT_RE.fullmatch(value[:-1])
    ):
        raise RuntimeUpdateError("invalid_pointer")
    return value[:-1]


def _publish_pointer(commit: str) -> None:
    _run(
        [
            "python3",
            "scripts/deployment_manifest.py",
            "publish-pointer",
            "--path",
            ".deploy_commit",
            "--source-commit",
            commit,
        ]
    )


def _verify_pointer(commit: str) -> None:
    try:
        verify_commit_pointer(ROOT / ".deploy_commit", commit)
    except ManifestError:
        raise RuntimeUpdateError("pointer_mismatch") from None


def _create_manifest(journal: dict[str, Any], db_path: Path) -> None:
    target_path = ROOT / ".deployments" / f"{journal['source_commit']}.json"
    if target_path.exists():
        existing = _verify_existing_manifest(
            target_path,
            source_commit=journal["source_commit"],
            db_path=db_path,
            immutable=True,
        )
        image = existing.get("image")
        if type(image) is not dict or any(
            image.get(key) != journal[f"target_image_{key}"]
            for key in ("ref", "id", "digest", "revision")
        ):
            raise RuntimeUpdateError("manifest_binding_mismatch")
        if existing.get("backup_path") != RUNTIME_BACKUP_SENTINEL:
            raise RuntimeUpdateError("manifest_binding_mismatch")
        return
    try:
        payload = build_manifest(
            source_commit=journal["source_commit"],
            image_ref=journal["target_image_ref"],
            image_id=journal["target_image_id"],
            image_digest=journal["target_image_digest"],
            image_revision=journal["target_image_revision"],
            db_path=db_path,
            db_display_path="data/terms.db",
            backup_path=RUNTIME_BACKUP_SENTINEL,
            deployment_utc=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            immutable=True,
        )
        write_manifest(target_path, payload)
    except (ManifestError, OSError):
        raise RuntimeUpdateError("manifest_failed") from None
    _verify_existing_manifest(
        target_path,
        source_commit=journal["source_commit"],
        db_path=db_path,
        immutable=True,
    )


def _attempt_quiesce() -> bool:
    try:
        return _compose_ok("stop", *SERVICES, timeout=60.0)
    except Exception:
        return False


def _handle_pre_activation_failure(
    journal: dict[str, Any],
    db_snapshot: dict[str, Any],
    old_manifest: dict[str, Any],
    current_old_ref: str,
) -> None:
    """Close a failure before the first stop without restarting services."""

    global _in_rollback, _termination_requested
    previous_in_rollback = _in_rollback
    previous_termination_requested = _termination_requested
    _in_rollback = True
    _termination_requested = False
    try:
        _assert_database_unchanged(db_snapshot)
        if _pointer(ROOT / ".deploy_commit") != journal["old_pointer"]:
            raise RuntimeUpdateError("pointer_changed")
        current_manifest = _verify_existing_manifest(
            ROOT / journal["old_manifest_path"],
            source_commit=journal["old_pointer"],
            db_path=ROOT / "data" / "terms.db",
            immutable=True,
        )
        if current_manifest != old_manifest:
            raise RuntimeUpdateError("manifest_changed")
        _assert_services(
            expected_ref=current_old_ref,
            expected_image_id=journal["old_image_id"],
            expected_revision=journal["old_pointer"],
        )
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="rolled_back", status="rolled_back")
        return
    except Exception:
        # The old binding could not be proven unchanged. Stop both surfaces and
        # leave an unresolved journal rather than recreating or claiming safety.
        try:
            _set_journal(journal, phase="unresolved", status="unresolved")
        except Exception:
            pass
        _attempt_quiesce()
    finally:
        _in_rollback = previous_in_rollback
        _termination_requested = previous_termination_requested


def _restore_old(
    journal: dict[str, Any], db_snapshot: dict[str, Any], *, stop_first: bool
) -> None:
    global _in_rollback, _termination_requested
    _in_rollback = True
    _termination_requested = False
    try:
        _set_journal(journal, phase="rollback_quiesce", status="unresolved")
        if stop_first and not _compose_ok("stop", *SERVICES, timeout=60.0):
            raise RuntimeUpdateError("rollback_stop_failed")
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="rollback_restore")
        _ensure_rollback_alias(journal, phase="rollback_tag")
        _compose(
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            *SERVICES,
            env=_runtime_env(
                journal["rollback_image_ref"], source_commit=journal["old_pointer"]
            ),
        )
        _assert_services(
            expected_ref=journal["rollback_image_ref"],
            expected_image_id=journal["old_image_id"],
            expected_revision=journal["old_pointer"],
        )
        _local_health()
        _assert_database_unchanged(db_snapshot)
        if _pointer(ROOT / ".deploy_commit") != journal["old_pointer"]:
            _set_journal(journal, phase="rollback_pointer")
            _publish_pointer(journal["old_pointer"])
            _verify_pointer(journal["old_pointer"])
        _set_journal(journal, phase="rolled_back", status="rolled_back")
    except Exception:
        # A rollback failure must never be reported as recovered. Persist
        # unresolved intent, then make a best-effort final quiesce of both
        # services. The caller retains the nonzero result.
        try:
            _set_journal(journal, phase="rollback_quiesce", status="unresolved")
        except Exception:
            pass
        _attempt_quiesce()
        raise
    finally:
        _in_rollback = False


def _current_image_exists(reference: str) -> tuple[str, str, str] | None:
    try:
        returncode, _stdout, _stderr = _execute(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        )
    except (OSError, RuntimeUpdateError):
        raise RuntimeUpdateError("image_inspect_failed") from None
    if returncode != 0:
        return None
    _check_termination()
    return _image_binding(reference)


def _idempotent_target(
    source_commit: str, db_snapshot: dict[str, Any], pointer: str
) -> bool:
    if pointer != source_commit:
        return False
    path = ROOT / ".deployments" / f"{source_commit}.json"
    if not path.is_file():
        raise RuntimeUpdateError("pointer_without_manifest")
    payload = _verify_existing_manifest(
        path,
        source_commit=source_commit,
        db_path=ROOT / "data" / "terms.db",
        immutable=True,
    )
    image = payload["image"]
    _assert_services(
        expected_ref=image["ref"],
        expected_image_id=_image_id(image["id"]),
        expected_revision=source_commit,
    )
    _assert_database_unchanged(db_snapshot)
    return True


def _check_runtime_journal() -> int:
    """Admission gate for the FULL updater: no unresolved runtime record.

    A full data deployment must never run across an unresolved runtime-only
    transaction. Untrusted shapes (symlink, malformed, schema drift) refuse
    as well: nobody has proven them closed. Journals already closed with a
    verified binding (committed, rolled_back) do not block a later full
    deployment.
    """

    path = _journal_path()
    if path.is_symlink():
        raise RuntimeUpdateError("invalid_journal")
    if not path.exists():
        return 0
    journal = _load_journal()
    if journal["status"] in {"active", "unresolved"}:
        raise RuntimeUpdateError("recovery_required")
    if journal["status"] not in {"committed", "rolled_back"}:
        raise RuntimeUpdateError("invalid_journal")
    return 0


def _new_deployment() -> int:
    source_commit = _git_preflight()
    _assert_directories()
    db_path = ROOT / "data" / "terms.db"
    db_snapshot = _database_snapshot()
    pointer = _pointer(ROOT / ".deploy_commit")

    journal_path = _journal_path()
    existing: dict[str, Any] | None = None
    if journal_path.is_symlink():
        raise RuntimeUpdateError("invalid_journal")
    if journal_path.exists():
        existing = _load_journal()
        if existing["status"] in {"active", "unresolved"}:
            raise RuntimeUpdateError("recovery_required")
    if _idempotent_target(source_commit, db_snapshot, pointer):
        return 0
    target_manifest_path = ROOT / ".deployments" / f"{source_commit}.json"
    target_manifest: dict[str, Any] | None = None
    if target_manifest_path.exists():
        if existing is None or existing["status"] != "rolled_back":
            raise RuntimeUpdateError("recovery_required")
        target_manifest = _verify_existing_manifest(
            target_manifest_path,
            source_commit=source_commit,
            db_path=db_path,
            immutable=True,
        )
        if target_manifest.get("backup_path") != RUNTIME_BACKUP_SENTINEL:
            raise RuntimeUpdateError("manifest_binding_mismatch")
        target_image = target_manifest["image"]
        target_id, _target_digest, target_revision = _image_binding(target_image["ref"])
        if target_id != target_image["id"] or target_revision != source_commit:
            raise RuntimeUpdateError("manifest_binding_mismatch")
    old_manifest_path = ROOT / ".deployments" / f"{pointer}.json"
    old_manifest = _verify_existing_manifest(
        old_manifest_path,
        source_commit=pointer,
        db_path=db_path,
        immutable=True,
    )
    allowed_old_refs = {old_manifest["image"]["ref"]}
    if (
        existing is not None
        and existing["status"] == "rolled_back"
        and existing["old_pointer"] == pointer
        and existing["old_image_id"] == old_manifest["image"]["id"]
        and existing["old_image_ref"] == old_manifest["image"]["ref"]
    ):
        allowed_old_refs.add(existing["rollback_image_ref"])
    current_old_ref = _observed_service_ref(allowed_old_refs)
    _assert_services(
        expected_ref=current_old_ref,
        expected_image_id=_image_id(old_manifest["image"]["id"]),
        expected_revision=pointer,
    )
    old_bot_running = True
    old_api_running = True
    journal = _journal_from_preflight(
        source_commit=source_commit,
        old_pointer=pointer,
        old_manifest=old_manifest,
        db_snapshot=db_snapshot,
        old_bot_running=old_bot_running,
        old_api_running=old_api_running,
        target_manifest=target_manifest,
    )
    _set_journal(journal, phase="prepared", status="active")
    activation_attempted = False
    stop_completed = False
    new_started = False
    try:
        _set_journal(journal, phase="rollback_tag")
        _ensure_rollback_alias(journal, phase="rollback_tag")
        if target_manifest is not None:
            target = _image_binding(journal["target_image_ref"])
        else:
            _set_journal(journal, phase="image_build")
            _compose("build", "wuwaterm", env=_runtime_env(journal["target_image_ref"]))
            target = _image_binding(journal["target_image_ref"])
        target_id, target_digest, target_revision = target
        if target_revision != source_commit:
            raise RuntimeUpdateError("image_revision_mismatch")
        journal["target_image_id"] = target_id
        journal["target_image_digest"] = target_digest
        journal["target_image_revision"] = target_revision
        _set_journal(journal, phase="image_ready")
        _set_journal(journal, phase="stopping")
        activation_attempted = True
        _compose("stop", *SERVICES)
        stop_completed = True
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="starting")
        new_started = True
        _compose(
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            *SERVICES,
            env=_runtime_env(journal["target_image_ref"]),
        )
        _assert_services(
            expected_ref=journal["target_image_ref"],
            expected_image_id=journal["target_image_id"],
            expected_revision=source_commit,
        )
        _set_journal(journal, phase="health")
        _local_health()
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="manifest")
        _create_manifest(journal, db_path)
        _set_journal(journal, phase="pointer")
        _publish_pointer(source_commit)
        _verify_pointer(source_commit)
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="committed", status="committed")
        return 0
    except RuntimeUpdateError:
        if not activation_attempted:
            _handle_pre_activation_failure(
                journal, db_snapshot, old_manifest, current_old_ref
            )
        else:
            try:
                _restore_old(
                    journal,
                    db_snapshot,
                    stop_first=stop_completed or new_started,
                )
            except RuntimeUpdateError:
                _set_journal(journal, phase="unresolved", status="unresolved")
                raise
        raise
    except Exception:
        if not activation_attempted:
            _handle_pre_activation_failure(
                journal, db_snapshot, old_manifest, current_old_ref
            )
        else:
            try:
                _restore_old(
                    journal,
                    db_snapshot,
                    stop_first=stop_completed or new_started,
                )
            except Exception:
                _set_journal(journal, phase="unresolved", status="unresolved")
                raise RuntimeUpdateError("internal_error") from None
        raise RuntimeUpdateError("internal_error") from None


def _recovery() -> int:
    journal = _load_journal()
    if journal["status"] in {"committed", "rolled_back"}:
        raise RuntimeUpdateError("no_recovery_needed")
    if _run_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], allow_empty=True
    ):
        raise RuntimeUpdateError("dirty_checkout")
    head = _commit(_run_output(["git", "rev-parse", "HEAD"]))
    if head != journal["source_commit"]:
        raise RuntimeUpdateError("recovery_tool_mismatch")
    if _tool_hash() != journal["tool_hash"]:
        raise RuntimeUpdateError("recovery_tool_mismatch")
    _assert_directories()
    global _in_rollback, _termination_requested
    _in_rollback = True
    _termination_requested = False
    try:
        db_snapshot = _database_snapshot()
        if (
            db_snapshot["sha256"] != journal["db_sha256"]
            or db_snapshot["stat"] != journal["db_stat"]
        ):
            raise RuntimeUpdateError("database_changed")
        old_manifest_path = ROOT / journal["old_manifest_path"]
        old_manifest = _verify_existing_manifest(
            old_manifest_path,
            source_commit=journal["old_pointer"],
            db_path=ROOT / "data" / "terms.db",
            immutable=True,
        )
        old_image = old_manifest.get("image")
        if (
            type(old_image) is not dict
            or old_image["ref"] != journal["old_image_ref"]
            or old_image["id"] != journal["old_image_id"]
            or old_image["revision"] != journal["old_pointer"]
        ):
            raise RuntimeUpdateError("invalid_journal")
        _ensure_rollback_alias(journal, phase="recovery_rollback_tag")
        _set_journal(journal, phase="recovery_quiesce", status="unresolved")
        if not _compose_ok("stop", *SERVICES, timeout=60.0):
            raise RuntimeUpdateError("recovery_stop_failed")
        _set_journal(journal, phase="recovery_restore")
        _compose(
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            *SERVICES,
            env=_runtime_env(
                journal["rollback_image_ref"], source_commit=journal["old_pointer"]
            ),
        )
        _assert_services(
            expected_ref=journal["rollback_image_ref"],
            expected_image_id=journal["old_image_id"],
            expected_revision=journal["old_pointer"],
        )
        _local_health()
        pointer = _pointer(ROOT / ".deploy_commit")
        if pointer != journal["old_pointer"]:
            if pointer != journal["source_commit"]:
                raise RuntimeUpdateError("invalid_pointer")
            _set_journal(journal, phase="recovery_pointer")
            _publish_pointer(journal["old_pointer"])
            _verify_pointer(journal["old_pointer"])
        _assert_database_unchanged(db_snapshot)
        _set_journal(journal, phase="recovered", status="rolled_back")
        return 0
    except Exception:
        try:
            _set_journal(journal, phase="unresolved", status="unresolved")
        except Exception:
            pass
        _attempt_quiesce()
        raise
    finally:
        _in_rollback = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--runtime-only", action="store_true")
    group.add_argument("--recover-runtime", action="store_true")
    group.add_argument("--check-runtime-journal", action="store_true")
    args = parser.parse_args(argv)
    try:
        _require_held_lock()
    except RuntimeUpdateError as exc:
        print(f"runtime-only deployment failed: {exc.code}", file=sys.stderr)
        return 1
    if args.check_runtime_journal:
        try:
            return _check_runtime_journal()
        except RuntimeUpdateError as exc:
            print(f"runtime-only deployment failed: {exc.code}", file=sys.stderr)
            return 1
    signal.signal(signal.SIGINT, _on_termination)
    signal.signal(signal.SIGTERM, _on_termination)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_termination)
    try:
        # An interrupted full-data update leaves the live database and the
        # pointer on opposite sides of a transaction this mode must not
        # touch. ANY outstanding full journal - including one whose phase
        # marker says committed - refuses admission until the explicit full
        # recovery has verified the observed binding and cleared it.
        if full_journal_exists():
            raise RuntimeUpdateError("full_recovery_required")
        return _new_deployment() if args.runtime_only else _recovery()
    except RuntimeUpdateError as exc:
        print(f"runtime-only deployment failed: {exc.code}", file=sys.stderr)
        if os.environ.get("WUWATERM_RUNTIME_TRACEBACK"):
            traceback.print_exc()
        return 1
    except Exception:
        print("runtime-only deployment failed: internal_error", file=sys.stderr)
        if os.environ.get("WUWATERM_RUNTIME_TRACEBACK"):
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
