"""data/deployment-backups keeps the newest five snapshots after a full deploy.

A snapshot is every file that carries one deployment id (deploy/backup-retention.sh).
The real vps-update.sh runs against the fake-docker harness of test_deploy_scripts.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_deploy_scripts import _make_deploy_harness

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
ID = re.compile(r"(\d{8}T\d{6}Z-\d+)\.(?:[0-9a-f]{64}\.db|json|deploy_commit)$")


@pytest.fixture()
def backups(tmp_path):
    root, env, *_ = _make_deploy_harness(tmp_path)
    (root / "data" / "deployment-backups").mkdir()
    return root, env, root / "data" / "deployment-backups"


def snapshot(directory: Path, snapshot_id: str, mtime: int = 0) -> set[str]:
    names = {f"terms.{snapshot_id}.{SHA}.db", f"db-diff.{snapshot_id}.json", f"{snapshot_id}.deploy_commit"}
    for name in names:
        (directory / name).write_text(name)
        os.utime(directory / name, (mtime, mtime))
    return names


def snapshot_ids(directory: Path) -> set[str]:
    return {m.group(1) for e in os.scandir(directory)
            if e.is_file(follow_symlinks=False) and (m := ID.search(e.name))}


def deploy(root: Path, env: dict, clock: str | None = None) -> subprocess.CompletedProcess:
    if clock:  # the id of this deployment, so that its own snapshot sorts first or last
        date = Path(env["PATH"].split(os.pathsep)[0]) / "date"
        date.write_text(
            f'#!/bin/sh\n[ "$*" = "-u +%Y%m%dT%H%M%SZ" ] && echo {clock} || exec {shutil.which("date")} "$@"\n'
        )
        date.chmod(0o755)
    result = subprocess.run(["sh", str(root / "deploy" / "vps-update.sh")], cwd=root, env=env,
                            text=True, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result


def test_full_deploy_keeps_the_five_newest_snapshots_and_touches_nothing_else(backups):
    root, env, directory = backups
    old = [f"2026010{day}T120000Z-{day}00" for day in range(1, 8)]
    # The newest ids get the oldest mtimes: only the id may decide.
    names = {i: snapshot(directory, i, mtime=10_000 - 100 * n) for n, i in enumerate(old)}
    others = {"notes.txt", "terms.db.manual", "20260101T120000Z.deploy_commit",
              "db-diff.20260101T120000Z-100.json.bak", f"terms.20260101T120000Z-100.{SHA[:8]}.db"}
    for name in others:
        (directory / name).write_text(name)
    outside = root / "outside.txt"
    outside.write_text("keep")
    (directory / "20250102T000000Z-2.deploy_commit").symlink_to(outside)
    (directory / f"terms.20250101T000000Z-1.{SHA}.db").mkdir()
    others |= {"20250102T000000Z-2.deploy_commit", f"terms.20250101T000000Z-1.{SHA}.db"}

    result = deploy(root, env)

    present = set(os.listdir(directory))
    new = present - others - set().union(*names.values())
    assert len(snapshot_ids(directory)) == 5
    assert len(new) == 3
    assert present == others | new | set().union(*(names[i] for i in old[3:]))
    assert outside.read_text() == "keep"
    assert "backup retention: removed" in result.stdout and f"terms.{old[0]}.{SHA}.db" in result.stdout


@pytest.mark.parametrize(
    ("clock", "dropped"),
    [(None, {"20260101T000000Z-9", "20251201T000000Z-1"}),
     # A clock set back: this deployment's own snapshot is the oldest by id and still survives.
     ("19990101T000000Z", {"20251201T000000Z-1"})],
)
def test_snapshots_sort_by_id_with_the_pid_as_a_number_and_the_new_one_survives(backups, clock, dropped):
    root, env, directory = backups
    # One timestamp, three pids that order differently as text (9 > 10 > 100) and as numbers.
    old = ["20260301T000000Z-1", "20260201T000000Z-1", "20260101T000000Z-9",
           "20260101T000000Z-10", "20260101T000000Z-100", "20251201T000000Z-1"]
    for n, i in enumerate(old):
        snapshot(directory, i, mtime=10_000 - 100 * n)

    deploy(root, env, clock)

    left = snapshot_ids(directory)
    new = left - set(old)
    assert len(new) == 1 and (clock is None or new.pop().startswith("1999"))
    assert set(old) - left == dropped
    assert len(left) == 5 + (clock is not None)


def test_fewer_than_five_snapshots_are_left_alone(backups):
    root, env, directory = backups
    kept = snapshot(directory, "20260101T120000Z-1") | snapshot(directory, "20260102T120000Z-2")

    result = deploy(root, env)

    assert kept <= set(os.listdir(directory)) and len(snapshot_ids(directory)) == 3
    assert "backup retention: removed" not in result.stdout


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_a_missing_or_empty_directory_is_a_no_op(tmp_path, kind):
    target = tmp_path / kind
    if kind == "empty":
        target.mkdir()
    result = subprocess.run(
        ["sh", "-c", '. "$1"; backup_retention_prune "$2" 5 x', "sh",
         str(ROOT / "deploy" / "backup-retention.sh"), str(target)],
        text=True, capture_output=True, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
