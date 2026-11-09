from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_GAME_TEXT_BYTES = 1_000_000
BULK_GAME_PATH_MARKERS = (
    "TextMap/",
    "Textmaps/",
    "ConfigDB/",
    "BinData/",
)
RUNTIME_STATE_NAMES = {
    "chat_settings.json",
    "chat_settings.json.lock",
    "channel_replies.json",
}


def _git_paths(args: list[str]) -> list[str]:
    proc = subprocess.run(
        args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return [p.decode("utf-8") for p in proc.stdout.split(b"\0") if p]


def tracked_paths() -> list[str]:
    """Repo-relative paths present in the INDEX - what a commit would carry."""
    return _git_paths(["git", "ls-files", "--cached", "-z"])


def untracked_paths() -> list[str]:
    return _git_paths(["git", "ls-files", "--others", "--exclude-standard", "-z"])


def candidate_files() -> list[Path]:
    return sorted({ROOT / rel for rel in tracked_paths() + untracked_paths()})


def is_game_text_path(rel: str) -> bool:
    return (
        any(marker in rel for marker in BULK_GAME_PATH_MARKERS)
        or rel.startswith("data/")
        or "wutheringdata" in rel.casefold()
    )


def is_runtime_state_path(rel: str) -> bool:
    name = Path(rel).name
    return (
        rel.startswith(("state/", "state-api/", "state-web/"))
        or name in RUNTIME_STATE_NAMES
        or name.startswith(".chat_settings.")
        or name.startswith(".channel_replies.")
    )


# SQLite writes this at offset 0 of every database file it creates.
SQLITE_MAGIC = b"SQLite format 3\x00"


def looks_like_a_database(path: Path) -> bool:
    """Identify a database by its CONTENT, not by what it is called.

    The suffix check is necessary and was not sufficient. A test that pointed a
    device store at a deliberately silly path wrote a real SQLite file into the
    repository root under a name with no extension at all; the suffix rule did
    not see it, nothing else did either, and it was committed. A database is a
    database whichever name it arrives under, and the first sixteen bytes say
    so.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def staged_blob_is_a_database(rel: str) -> bool:
    """The same question asked of the INDEX rather than the working tree.

    Reading the working tree was the wrong SOURCE for a tracked path, and the
    gap is reachable: stage a file and then delete or replace it, and the index
    still carries the original blob while the path on disk is gone. Reproduced
    as an `AD` state - index holding the SQLite magic, working tree empty - in
    which a worktree-only check reported the repository clean and the database
    blob was free to be committed. What a commit carries is the index, so that
    is what has to be inspected.

    Only the HEADER is read. ``git cat-file`` can stream an arbitrarily large
    object; buffering it to look at sixteen bytes would mean loading bulk game
    data this guard exists to reject, so the check could be killed by the very
    thing it is meant to report. Bytes after the magic are discarded in chunks.
    """
    return rel in staged_database_paths([rel])


_SKIP_CHUNK = 1 << 16


def _read_until_nul(stream) -> bytes | None:
    """Read one NUL-terminated frame. None means the stream ended mid-frame."""
    buf = bytearray()
    while True:
        ch = stream.read(1)
        if not ch:
            return None if not buf else bytes(buf)
        if ch == b"\0":
            return bytes(buf)
        buf.extend(ch)


def _drain(stream, nbytes: int) -> bool:
    """Discard exactly nbytes, or return False if the stream ends early."""
    remaining = nbytes
    while remaining > 0:
        chunk = stream.read(min(remaining, _SKIP_CHUNK))
        if not chunk:
            return False
        remaining -= len(chunk)
    return True


def staged_database_paths(paths: list[str]) -> set[str]:
    """Which of these staged paths hold a database, in ONE git process.

    Asking per file spawned one ``git cat-file`` per tracked path: measured at
    5.35 seconds and about 170 processes on this repository, against roughly
    0.03 seconds before, growing linearly with the index. That is a bad trade
    for a gate that runs on every commit and in CI.

    ``--batch -Z`` answers the whole list down one pipe with NUL framing on
    both sides. Git paths may contain newlines, so newline-delimited requests
    would split one path into several queries and desynchronise the stream.
    Every resolved object — blob or not — has its declared body and trailing
    delimiter consumed; skipping the body of a non-blob (a gitlink resolving to
    a commit that exists in the object store) would leave those bytes in the
    pipe and make every later path read the wrong frame.

    A missing response is ``<requested-name> missing`` with no body. The name
    may itself contain spaces, so detection is by the trailing `` missing``
    marker rather than by splitting and inspecting a fixed field index.

    Git 2.51+ additionally emits ``<oid> submodule`` (no size, no body) for
    gitlink index entries whose target is absent; treat that form the same as
    an ordinary missing response so the gate neither crashes nor desyncs.

    Only the SQLite header is retained from a blob. The remainder is discarded
    in bounded chunks so a large blob never lands in memory whole.

    If ``cat-file`` itself fails (for example because this Git build does not
    support ``-Z``), the gate fails closed rather than reporting a clean tree.
    """
    if not paths:
        return set()
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch", "-Z"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    found: set[str] = set()
    stream_error: str | None = None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for rel in paths:
            try:
                proc.stdin.write(b":" + rel.encode("utf-8") + b"\0")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                # cat-file already exited (unsupported -Z, crash, etc.).
                # Fall through to the rc / stream_error checks below.
                stream_error = "cat-file process closed stdin early"
                break
            header = _read_until_nul(proc.stdout)
            if header is None:
                stream_error = "cat-file stream ended before all paths were answered"
                break
            # Missing responses: "<requested-name> missing". The name can
            # contain spaces, so never split-and-index to find the marker.
            if header.endswith(b" missing"):
                continue
            parts = header.split()
            # Git 2.51+ gitlink (absent target): "<oid> submodule" (no size/body).
            if len(parts) == 2 and parts[1] == b"submodule":
                continue
            # Resolved: "<oid> <type> <size>". oid and type are single tokens.
            if len(parts) < 3:
                stream_error = f"malformed cat-file header: {header!r}"
                break
            try:
                size = int(parts[-1])
            except ValueError:
                stream_error = f"malformed cat-file size in header: {header!r}"
                break
            obj_type = parts[-2]
            is_blob = obj_type == b"blob"
            wanted = min(size, len(SQLITE_MAGIC)) if is_blob else 0
            magic_hdr = b""
            if wanted:
                magic_hdr = proc.stdout.read(wanted)
                if len(magic_hdr) < wanted:
                    stream_error = "cat-file stream ended mid-object"
                    break
            if not _drain(proc.stdout, size - wanted):
                stream_error = "cat-file stream ended mid-object"
                break
            # Resolved objects are followed by a NUL object delimiter under -Z.
            trail = proc.stdout.read(1)
            if trail != b"\0":
                stream_error = "cat-file stream missing object delimiter"
                break
            if is_blob and magic_hdr == SQLITE_MAGIC:
                found.add(rel)
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        stderr_data = b""
        if proc.stderr is not None:
            stderr_data = proc.stderr.read()
            proc.stderr.close()
        if proc.stdout is not None:
            proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        err = stderr_data.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"git cat-file --batch -Z failed (rc={rc})"
            + (f": {err}" if err else "")
        )
    if stream_error is not None:
        raise RuntimeError(stream_error)
    return found


def main() -> int:
    failures: list[str] = []
    tracked_list = tracked_paths()
    tracked = set(tracked_list)
    # Resolved once for the whole index rather than per file: see
    # staged_database_paths for the process-count measurement that forced this.
    staged_databases = staged_database_paths(
        [r for r in tracked_list if not r.endswith((".db", ".sqlite", ".sqlite3"))]
    )
    for path in candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.endswith((".db", ".sqlite", ".sqlite3")):
            failures.append(f"tracked generated DB: {rel}")
            continue
        # Tracked paths are judged on the INDEX, because that is what a commit
        # would carry; untracked ones only exist on disk. Checking the working
        # tree for both let a staged database blob through whenever the path
        # had since been deleted or replaced.
        if rel in tracked:
            if rel in staged_databases:
                failures.append(
                    f"staged database blob (detected by content): {rel}"
                )
                continue
        elif (
            path.is_file()
            # is_file() FOLLOWS the link, so a symlink pointing at a database
            # outside the repository was reported as one - but git stores only
            # the target path string, so there is no database to commit and the
            # gate was blocked by a false positive.
            and not path.is_symlink()
            and looks_like_a_database(path)
        ):
            failures.append(f"untracked database file (detected by content): {rel}")
            continue
        if is_runtime_state_path(rel):
            failures.append(f"tracked runtime state: {rel}")
            continue
        if not path.exists():
            continue
        size = path.stat().st_size
        if is_game_text_path(rel) and size > MAX_GAME_TEXT_BYTES:
            failures.append(f"tracked bulk game-text file >1MB: {rel} ({size} bytes)")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("repo hygiene ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
