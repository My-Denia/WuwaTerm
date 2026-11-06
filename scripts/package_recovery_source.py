#!/usr/bin/env python3
"""Package a committed legacy deployment source and its reachable Git history.

The commit is an input, not a branch name: the transaction journal supplies it.
The capsule contains only that commit's tracked tree and reachable Git objects.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

COMMIT = re.compile(r"[0-9a-f]{40}\Z")
TOOLS = {
    "runtime": ("deploy/vps-update.sh", "deploy/docker-compose.yml",
                "scripts/deployment_manifest.py", "scripts/runtime_update.py"),
    "full": ("deploy/vps-update.sh", "deploy/docker-compose.yml",
             "scripts/deployment_manifest.py", "scripts/full_update.py"),
}
ENTRYPOINTS = {
    "runtime": (b"--recover-runtime)", b"exec python3 scripts/runtime_update.py --recover-runtime"),
    "full": (b"--recover-full)", b"exec python3 scripts/full_update.py recover"),
}


class CapsuleError(RuntimeError):
    pass


class DeploymentLock:
    """Validated open-file description for deployment-root child writers."""

    def __init__(self, root: Path, fd: int):
        self.root = root
        self.fd = fd
        self.pass_fds()

    def pass_fds(self) -> tuple[int, ...]:
        path = self.root / ".deployments" / ".deployment.lock"
        try:
            actual = os.fstat(self.fd)
            expected = path.stat(follow_symlinks=False)
            if (self.fd != 9 or not stat.S_ISREG(actual.st_mode)
                    or not stat.S_ISREG(expected.st_mode)
                    or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                    or os.readlink(f"/proc/self/fd/{self.fd}") != str(path)):
                raise CapsuleError("deployment lock identity mismatch")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CapsuleError("deployment lock unavailable") from exc
        return (self.fd,)


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "PYTHON"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return env


def git(repo: Path, *args: str, input_bytes: bytes | None = None,
        lock: DeploymentLock | None = None) -> bytes:
    command = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args]
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=clean_env(), check=False,
                            pass_fds=lock.pass_fds() if lock else ())
    if result.returncode:
        raise CapsuleError("git operation failed: " + " ".join(args[:2]))
    return result.stdout


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    protected = {".git", ".env", ".gitconfig", ".venv", "data", "state",
                 "state-api", ".deployments", ".deploy_commit", "goal-runs"}
    parts = name.split("/")
    private = any(part in {".git", ".gitconfig", ".venv", "id_rsa", "id_ed25519"}
                  or (part.startswith(".env") and part != ".env.example")
                  or part.lower() in {"textmap", "configdb"} for part in parts)
    return (bool(name) and parts[0] not in protected and not private
            and path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}
            and not path.is_absolute() and all(p not in ("", ".", "..") for p in parts)
            and "\\" not in name and "\x00" not in name)


def check_history_paths(repo: Path, commit: str) -> None:
    # The bundle includes all ancestors, not just the selected tree. Refuse a
    # history with private deployment files rather than silently shipping them.
    raw = git(repo, "log", "--root", "--full-history", "-m", "--format=", "--name-only",
              "-z", "--no-renames", commit)
    for path in raw.split(b"\0"):
        if path and not safe_name(path.decode("utf-8", errors="strict")):
            raise CapsuleError("source history contains a protected path")


def tree_files(repo: Path, commit: str) -> tuple[str, dict[str, dict[str, str]]]:
    if not COMMIT.fullmatch(commit):
        raise CapsuleError("expected a full source commit")
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    entries = git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")
    files: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split(" ")
        name = raw_name.decode("utf-8", errors="strict")
        # Symlinks, gitlinks and unusual paths cannot be restored safely by this
        # dispatcher. Refuse rather than following a link outside the checkout.
        if kind != "blob" or mode not in ("100644", "100755") or not safe_name(name):
            raise CapsuleError("unsupported tracked path or mode")
        files[name] = {"mode": mode, "blob": blob}
    if not files:
        raise CapsuleError("empty source tree")
    request = b"".join(f"{item['blob']}\n".encode("ascii") for item in files.values())
    stream = io.BytesIO(git(repo, "cat-file", "--batch", input_bytes=request))
    for item in files.values():
        header = stream.readline().decode("ascii").strip().split()
        if len(header) != 3 or header[0] != item["blob"] or header[1] != "blob":
            raise CapsuleError("missing source blob")
        content = stream.read(int(header[2]))
        if stream.read(1) != b"\n":
            raise CapsuleError("invalid source blob")
        expected = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if expected != item["blob"]:
            raise CapsuleError("source blob identity mismatch")
        item["sha256"] = hashlib.sha256(content).hexdigest()
        item["size"] = len(content)
    return tree, files


def inspect_archive(path: Path, files: dict[str, dict[str, Any]], *, extract: Path | None = None,
                    require_tools: bool = True) -> dict[str, str | None]:
    seen: set[str] = set()
    contents: dict[str, bytes] = {}
    directories = {str(PurePosixPath(name).parent) for name in files}
    directories |= {str(parent) for name in files for parent in PurePosixPath(name).parents}
    try:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if not safe_name(name) or name in seen:
                    raise CapsuleError("unsafe or duplicate archive member")
                seen.add(name)
                if member.isdir():
                    if name not in directories:
                        raise CapsuleError("unexpected archive directory")
                    continue
                if not member.isfile() or name not in files:
                    raise CapsuleError("unexpected archive member")
                item = files[name]
                executable = 0o111 if item["mode"] == "100755" else 0
                if member.size != item["size"] or member.mode & 0o111 != executable:
                    raise CapsuleError("archive member metadata mismatch")
                source = archive.extractfile(member)
                if source is None:
                    raise CapsuleError("unreadable archive member")
                content = source.read()
                if hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise CapsuleError("archive member content mismatch")
                contents[name] = content
    except (tarfile.TarError, OSError) as exc:
        raise CapsuleError("invalid source archive") from exc
    if set(contents) != set(files):
        raise CapsuleError("incomplete source archive")
    fingerprints = {}
    for kind, paths in TOOLS.items():
        shell = contents.get("deploy/vps-update.sh", b"")
        if not all(marker in shell for marker in ENTRYPOINTS[kind]):
            fingerprints[kind] = None
            continue
        digest = hashlib.sha256()
        for name in paths:
            if name not in contents:
                break
            digest.update(name.encode("ascii"))
            digest.update(contents[name])
        else:
            fingerprints[kind] = digest.hexdigest()
            continue
        fingerprints[kind] = None
    if require_tools and not any(fingerprints.values()):
        raise CapsuleError("historical recovery tool missing")
    if extract is not None:
        for name, content in contents.items():
            target = extract.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(int(files[name]["mode"], 8) & 0o777)
    return fingerprints


def package(repo: Path, commit: str, output: Path, *, legacy: bool = True,
            lock: DeploymentLock | None = None) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    if lock:
        if lock.root != repo or output.parent != repo / ".deployments":
            raise CapsuleError("backup lock does not match deployment root")
        lock.pass_fds()
    if not COMMIT.fullmatch(commit):
        raise CapsuleError("expected a full source commit")
    actual = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if actual != commit:
        raise CapsuleError("source commit mismatch")
    check_history_paths(repo, commit)
    tree, files = tree_files(repo, commit)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    archive = output / "source.tar"
    with archive.open("wb") as stream:
        proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit],
                              stdout=stream, stderr=subprocess.PIPE, env=clean_env(), check=False,
                              pass_fds=lock.pass_fds() if lock else ())
    if proc.returncode:
        raise CapsuleError("git archive failed")
    fingerprints = inspect_archive(archive, files, require_tools=legacy)
    with tempfile.TemporaryDirectory(prefix="wuwaterm-capsule-") as temp:
        temp_path = Path(temp)
        bare = temp_path / "objects.git"
        git(temp_path, "init", "--bare", str(bare))
        pack = temp_path / "source.pack"
        with pack.open("wb") as stream:
            proc = subprocess.run(["git", "-C", str(repo), "pack-objects", "--revs", "--stdout"],
                                  input=f"{commit}\n".encode(), stdout=stream,
                                  stderr=subprocess.PIPE, env=clean_env(), check=False)
        if proc.returncode:
            raise CapsuleError("cannot pack source history")
        with pack.open("rb") as stream:
            proc = subprocess.run(["git", "-C", str(bare), "index-pack", "--stdin"],
                                  stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=clean_env(), check=False)
        if proc.returncode:
            raise CapsuleError("cannot index source history")
        git(bare, "update-ref", "refs/capsule/source", commit)
        git(bare, "bundle", "create", str((output / "source.bundle").resolve()),
            "refs/capsule/source", lock=lock)
    result = {"format": "wuwaterm-legacy-source-v2" if legacy else "wuwaterm-source-backup-v2", "source_commit": commit,
              "source_tree": tree, "archive_sha256": sha(archive),
              "bundle_sha256": sha(output / "source.bundle"), "files": files,
              "supported_kinds": [kind for kind, value in fingerprints.items() if value is not None],
              "tool_hashes": fingerprints}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    for path in (archive, output / "source.bundle", manifest_path):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    sync_directory(output)
    sync_directory(output.parent)
    return result


def verify(capsule: Path, commit: str, *, extracted: Path | None = None,
           allow_backup: bool = False) -> dict[str, Any]:
    if capsule.is_symlink() or not capsule.is_dir():
        raise CapsuleError("invalid capsule directory")
    for name in ("manifest.json", "source.tar", "source.bundle"):
        path = capsule / name
        if path.is_symlink() or not path.is_file():
            raise CapsuleError("invalid capsule file")
    try:
        manifest = json.loads((capsule / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapsuleError("invalid capsule manifest") from exc
    if type(manifest) is not dict or set(manifest) != {"format", "source_commit", "source_tree", "archive_sha256", "bundle_sha256", "files", "supported_kinds", "tool_hashes"}:
        raise CapsuleError("invalid capsule manifest")
    accepted = {"wuwaterm-legacy-source-v2"}
    if allow_backup:
        accepted.add("wuwaterm-source-backup-v2")
    if manifest["format"] not in accepted or manifest["source_commit"] != commit or not COMMIT.fullmatch(commit):
        raise CapsuleError("capsule source mismatch")
    archive, bundle = capsule / "source.tar", capsule / "source.bundle"
    if sha(archive) != manifest["archive_sha256"] or sha(bundle) != manifest["bundle_sha256"]:
        raise CapsuleError("capsule checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="wuwaterm-verify-") as temp:
        parent = Path(temp)
        bare = parent / "verify.git"
        git(parent, "init", "--bare", str(bare))
        refs = git(bare, "bundle", "list-heads", str(bundle.resolve())).decode("ascii").splitlines()
        if refs != [f"{commit} refs/capsule/source"]:
            raise CapsuleError("bundle source mismatch")
        git(bare, "bundle", "verify", str(bundle.resolve()))
        git(bare, "fetch", "--no-tags", "--no-write-fetch-head", str(bundle.resolve()),
            "+refs/capsule/source:refs/capsule/source")
        git(bare, "fsck", "--full", "--strict", "--no-reflogs")
        reachable = set(git(bare, "rev-list", "--objects", "--no-object-names", commit).splitlines())
        all_objects = set(git(bare, "cat-file", "--batch-all-objects",
                              "--batch-check=%(objectname)").splitlines())
        if all_objects != reachable:
            raise CapsuleError("bundle contains objects outside source history")
        check_history_paths(bare, commit)
        actual_tree, actual_files = tree_files(bare, commit)
        if actual_tree != manifest["source_tree"] or actual_files != manifest["files"]:
            raise CapsuleError("capsule tree mismatch")
        hashes = inspect_archive(archive, actual_files, extract=extracted,
                                 require_tools=manifest["format"] == "wuwaterm-legacy-source-v2")
        kinds = [kind for kind, value in hashes.items() if value is not None]
        if hashes != manifest["tool_hashes"] or kinds != manifest["supported_kinds"]:
            raise CapsuleError("capsule tool fingerprint mismatch")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--repository", type=Path)
    operation.add_argument("--verify", action="store_true")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            verify(args.output.absolute(), args.source_commit, allow_backup=True)
        else:
            package(args.repository, args.source_commit, args.output.absolute())
    except (CapsuleError, OSError, UnicodeError, ValueError) as exc:
        print(f"source capsule failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
