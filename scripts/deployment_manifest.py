from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "src"))

from scripts import deployment_admission  # noqa: E402
from wuwaterm.constants import get_source_profile  # noqa: E402
from wuwaterm.db import SCHEMA_VERSION  # noqa: E402

# Where data/, .deployments/ and .deploy_commit live: the checkout itself, or
# the base directory above `releases/rN`.
ROOT = deployment_admission.state_root(CODE_ROOT)


MANIFEST_SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(source: Path, destination: Path) -> None:
    """Fsync, atomically replace, and durably publish a file."""

    source_parent = source.parent.resolve()
    destination_parent = destination.parent.resolve()
    _fsync_file(source)
    os.replace(source, destination)
    _fsync_directory(destination_parent)
    if source_parent != destination_parent:
        _fsync_directory(source_parent)


def durable_remove(path: Path) -> None:
    """Remove a file, if present, and fsync the containing directory."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent.resolve())


PROVENANCE_KEYS = (
    "schema_version",
    "source_profile",
    "source_repo_url",
    "source_commit",
    "source_game_version",
    "source_resource_version",
    "source_changelist",
    "wutheringdata_commit",
)


def read_db_provenance(
    path: Path, *, require_active_profile: bool = True, immutable: bool = False
) -> dict[str, str]:
    if not path.is_file():
        raise ManifestError(f"database is missing: {path}")
    if immutable and path.is_symlink():
        raise ManifestError(f"database must not be a symlink: {path}")
    if immutable and any(
        path.with_name(path.name + suffix).exists()
        or path.with_name(path.name + suffix).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ManifestError(f"database sidecar exists: {path}")
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    if immutable:
        database_uri += "&immutable=1"
    try:
        with closing(
            sqlite3.connect(database_uri, uri=True)
        ) as conn:
            rows = conn.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    except sqlite3.Error as exc:
        raise ManifestError(f"cannot read database provenance: {exc}") from exc
    metadata = {str(key): str(value) for key, value in rows}
    missing = [key for key in PROVENANCE_KEYS if not metadata.get(key)]
    if missing:
        raise ManifestError(
            f"database provenance is incomplete: {', '.join(missing)}"
        )
    if not require_active_profile:
        # Historical manifests must remain verifiable after a later source pin
        # or schema upgrade. The manifest's exact recorded provenance, not the
        # current checkout's constants, is authoritative for that readback.
        return {key: metadata[key] for key in PROVENANCE_KEYS}
    profile_name = metadata.get("source_profile", "")
    try:
        profile = get_source_profile(profile_name)
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    expected = {
        "schema_version": SCHEMA_VERSION,
        "source_profile": profile.name,
        "source_repo_url": profile.repo_url,
        "source_commit": profile.pinned_commit,
        "source_game_version": profile.expected_game_version,
        "source_resource_version": profile.expected_resource_version,
        "source_changelist": profile.expected_changelist,
        "wutheringdata_commit": profile.pinned_commit,
    }
    for key, value in expected.items():
        if value is None:
            if not metadata.get(key):
                raise ManifestError(f"database metadata {key} is missing")
        elif metadata.get(key) != value:
            raise ManifestError(
                f"database metadata {key} mismatch: expected {value!r}, "
                f"got {metadata.get(key)!r}"
            )
    return {key: metadata[key] for key in PROVENANCE_KEYS}


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"invalid deployment timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ManifestError("deployment timestamp must include a timezone")


def build_manifest(
    *,
    source_commit: str,
    image_ref: str,
    image_id: str,
    image_digest: str,
    image_revision: str,
    db_path: Path,
    db_display_path: str,
    backup_path: str,
    deployment_utc: str,
    immutable: bool = False,
) -> dict[str, Any]:
    if not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("source commit must be a 40-character lowercase hex SHA")
    if image_revision != source_commit:
        raise ManifestError(
            f"image revision mismatch: expected {source_commit}, got {image_revision}"
        )
    if not image_ref or not image_id or not image_digest:
        raise ManifestError("image ref, id and digest must be non-empty")
    _validate_timestamp(deployment_utc)
    metadata = read_db_provenance(db_path, immutable=immutable)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_commit": source_commit,
        "image": {
            "ref": image_ref,
            "id": image_id,
            "digest": image_digest,
            "revision": image_revision,
        },
        "database": {
            "path": db_display_path,
            "sha256": sha256_file(db_path),
            "provenance": metadata,
        },
        "deployment_utc": deployment_utc,
        "backup_path": backup_path,
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read deployment manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("deployment manifest must be a JSON object")
    return payload


def verify_manifest(
    path: Path,
    *,
    source_commit: str,
    image_id: str,
    image_digest: str | None,
    db_path: Path,
    immutable: bool = False,
) -> dict[str, Any]:
    payload = _load_manifest(path)
    required_top = {
        "schema_version",
        "source_commit",
        "image",
        "database",
        "deployment_utc",
        "backup_path",
    }
    if set(payload) != required_top:
        raise ManifestError(f"manifest fields mismatch: got {sorted(payload)}")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("manifest schema version mismatch")
    if payload["source_commit"] != source_commit:
        raise ManifestError("manifest source commit mismatch")
    _validate_timestamp(str(payload["deployment_utc"]))

    image = payload.get("image")
    if not isinstance(image, dict) or set(image) != {"ref", "id", "digest", "revision"}:
        raise ManifestError("manifest image fields mismatch")
    if image["id"] != image_id:
        raise ManifestError("manifest image identity mismatch")
    if image_digest is not None and image["digest"] != image_digest:
        raise ManifestError("manifest image digest mismatch")
    if image["revision"] != source_commit:
        raise ManifestError("manifest image revision mismatch")

    database = payload.get("database")
    if not isinstance(database, dict) or set(database) != {"path", "sha256", "provenance"}:
        raise ManifestError("manifest database fields mismatch")
    if database["sha256"] != sha256_file(db_path):
        raise ManifestError("manifest database hash mismatch")
    if database["provenance"] != read_db_provenance(
        db_path, require_active_profile=False, immutable=immutable
    ):
        raise ManifestError("manifest database provenance mismatch")
    return payload


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o444)
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            raise ManifestError(f"immutable deployment manifest already exists: {path}")
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def publish_commit_pointer(path: Path, source_commit: str) -> None:
    if not COMMIT_RE.fullmatch(source_commit):
        raise ManifestError("source commit must be a 40-character lowercase hex SHA")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"{source_commit}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o444)
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def verify_commit_pointer(path: Path, source_commit: str) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read deployment pointer {path}: {exc}") from exc
    expected = f"{source_commit}\n".encode("ascii")
    if actual != expected:
        raise ManifestError("deployment pointer readback mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--path", required=True, type=Path)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--image-ref", required=True)
    create.add_argument("--image-id", required=True)
    create.add_argument("--image-digest", required=True)
    create.add_argument("--image-revision", required=True)
    create.add_argument("--db", required=True, type=Path)
    create.add_argument("--db-display-path", default="data/terms.db")
    create.add_argument("--backup-path", required=True)
    create.add_argument("--deployment-utc", required=True)
    create.add_argument("--immutable", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--path", required=True, type=Path)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--image-id", required=True)
    verify.add_argument("--image-digest")
    verify.add_argument("--db", required=True, type=Path)
    verify.add_argument("--immutable", action="store_true")

    publish_pointer = subparsers.add_parser("publish-pointer")
    publish_pointer.add_argument("--path", required=True, type=Path)
    publish_pointer.add_argument("--source-commit", required=True)

    replace_file = subparsers.add_parser("durable-replace")
    replace_file.add_argument("--source", required=True, type=Path)
    replace_file.add_argument("--destination", required=True, type=Path)

    remove_file = subparsers.add_parser("durable-remove")
    remove_file.add_argument("--path", required=True, type=Path)

    verify_pointer = subparsers.add_parser("verify-pointer")
    verify_pointer.add_argument("--path", required=True, type=Path)
    verify_pointer.add_argument("--source-commit", required=True)

    args = parser.parse_args()
    try:
        if args.command in ("create", "publish-pointer", "durable-replace", "durable-remove"):
            deployment_admission.admit_legacy(ROOT)
            paths = ((args.source, args.destination) if args.command == "durable-replace"
                     else (args.path,))
            if args.command == "create" and not args.immutable:
                # SQLite mode=ro can still write WAL shared memory. Treat its
                # database and companion files as potential write paths.
                paths += (args.db, *(args.db.with_name(args.db.name + suffix)
                                    for suffix in ("-wal", "-shm", "-journal")))
            deployment_admission.require_root_paths(ROOT, *paths)
        if args.command == "create":
            payload = build_manifest(
                source_commit=args.source_commit,
                image_ref=args.image_ref,
                image_id=args.image_id,
                image_digest=args.image_digest,
                image_revision=args.image_revision,
                db_path=args.db,
                db_display_path=args.db_display_path,
                backup_path=args.backup_path,
                deployment_utc=args.deployment_utc,
                immutable=args.immutable,
            )
            if args.path.exists():
                verify_manifest(
                    args.path,
                    source_commit=args.source_commit,
                    image_id=args.image_id,
                    image_digest=args.image_digest,
                    db_path=args.db,
                    immutable=args.immutable,
                )
            else:
                write_manifest(args.path, payload)
        elif args.command == "verify":
            # A SQLite read-only connection can still create or update WAL
            # shared-memory files. CLI verification is lock-free and must not
            # write, so refuse sidecars and use SQLite's immutable read mode.
            verify_manifest(
                args.path,
                source_commit=args.source_commit,
                image_id=args.image_id,
                image_digest=args.image_digest,
                db_path=args.db,
                immutable=True,
            )
        elif args.command == "publish-pointer":
            publish_commit_pointer(args.path, args.source_commit)
        elif args.command == "durable-replace":
            durable_replace(args.source, args.destination)
        elif args.command == "durable-remove":
            durable_remove(args.path)
        else:
            verify_commit_pointer(args.path, args.source_commit)
    except deployment_admission.AdmissionError as exc:
        print(f"deployment admission refused: {exc}", file=sys.stderr)
        return 1
    except ManifestError as exc:
        print(f"deployment manifest error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
