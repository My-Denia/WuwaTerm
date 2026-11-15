"""Release layout recovery: which checkout a recovery entry binds, and when
`current` moves.

The updater switches `current` after the full or runtime transaction committed,
so a kill between those two steps leaves a host that already serves the new
deployment while `current` still names the release that was serving. The
recovery command a failed run prints is an absolute path into the release that
wrote the record, not into `current`.

These tests interrupt the REAL updater at exactly that window with a
process-group SIGKILL against isolated fake Docker and real git, then run the
printed command from a shell that carries no `WUWATERM_DEPLOY_ROOT` or
`WUWATERM_STATE_ROOT`. What they hold to is the documented contract: recovery
enters the invoked release, settles `current` only from a binding it can prove,
and never turns an exit status into a release switch.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_deploy_scripts import NEW_IMAGE, OLD_COMMIT, OLD_IMAGE
from scripts.deployment_manifest import build_manifest, publish_commit_pointer, write_manifest
from test_full_deploy_recovery import (
    API_ROLLBACK_REF,
    BOT_ROLLBACK_REF,
    JOURNAL_RELPATH,
    OLD_REF,
    TXN,
    _journal,
    _plant,
    _surface,
    _write_full_fake_docker,
    _write_full_fake_python,
)
from test_release_layout import Host

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_JOURNAL = ".deployments/.runtime-transaction.json"


def current_target(base: Path) -> str:
    return os.readlink(base / "current")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def push_tool_change(host: Host) -> str:
    """A new origin/main commit that also changes the updater's fingerprint.

    The journal binds a hash of the updater's own files, so a recovery that
    entered the previous release must refuse; a recovery that entered this one
    must complete. A comment is enough to make the two fingerprints differ
    without changing behaviour.
    """

    updater = host.work / "deploy" / "vps-update.sh"
    updater.write_text(updater.read_text() + "# release recovery test\n", encoding="utf-8")
    (host.work / "release.txt").write_text("next release\n", encoding="utf-8")
    host.git(host.work, "add", "-A")
    host.git(host.work, "commit", "-q", "-m", "c2")
    host.git(host.work, "push", "-q", "origin", "main")
    sha = host.rev(host.work)
    host.retarget(sha)
    return sha


def create_release(host: Host, commit: str) -> str:
    """The next release, built by the updater's own hand-off helper."""

    host.git(host.current, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
    result = subprocess.run(
        [
            "sh",
            "-c",
            f'. "{ROOT}/deploy/release-layout.sh"\n'
            f'release_create "{host.current}" "{host.base}/releases" '
            f'"{commit}" "{host.origin}"',
        ],
        cwd=host.base,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def fresh_shell_env(host: Host, **extra: str) -> dict[str, str]:
    """The environment a printed recovery command must work in.

    Everything the updater itself exported is dropped: no deployment root, no
    state root, no hand-off marker. Only the harness PATH (fake Docker, wrapper
    interpreters) and its own bookkeeping survive.
    """

    env = {
        key: value
        for key, value in host.env.items()
        if not key.startswith(("WUWATERM_", "FULL_", "RUNTIME_"))
    }
    env.update(extra)
    return env


def recover(host: Host, release: Path, mode: str, **extra: str) -> subprocess.CompletedProcess:
    """Run a release's own recovery entry from a shell with nothing exported."""

    return subprocess.run(
        ["sh", str(release / "deploy" / "vps-update.sh"), mode],
        cwd=host.base.parent,
        env=fresh_shell_env(host, **extra),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def wait_for(path: Path, process: subprocess.Popen, what: str) -> None:
    for _ in range(600):
        if path.exists():
            return
        if process.poll() is not None:
            pytest.fail(f"the updater exited before {what}")
        time.sleep(0.05)
    pytest.fail(f"{what} was never reached")


def kill_group(process: subprocess.Popen) -> None:
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=30)
    assert process.returncode == -signal.SIGKILL


def stops(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if " stop " in line]


def adopt_release_commit(host: Host) -> str:
    """Record the deployment at the commit the migrated release actually is.

    The base harness seeds a synthetic pointer; a release layout records its own
    commit in `.release-commit`, and a host that migrated from an in-place
    checkout is deployed at exactly that commit. Without this, the pointer and
    the release it names disagree for a reason that has nothing to do with the
    behaviour under test.
    """

    base, head = host.base, host.head
    (base / ".deployments" / f"{OLD_COMMIT}.json").unlink()
    write_manifest(
        base / ".deployments" / f"{head}.json",
        build_manifest(
            source_commit=head,
            image_ref=OLD_REF,
            image_id=OLD_IMAGE,
            image_digest=OLD_IMAGE,
            image_revision=head,
            db_path=base / "data" / "terms.db",
            db_display_path="data/terms.db",
            backup_path="legacy",
            deployment_utc="2026-07-15T00:00:00Z",
        ),
    )
    publish_commit_pointer(base / ".deploy_commit", head)
    return head


@pytest.fixture()
def full_host(tmp_path):
    """The migrated release layout, with the per-surface fake daemon."""

    host = Host(tmp_path)
    # The fake daemon and the wrapper interpreter live in the harness PATH
    # directory, so the generators are given a root whose parent is it.
    _write_full_fake_docker(host.fake_bin.parent / "install")
    _write_full_fake_python(host.fake_bin.parent / "install")
    assert host.migrate().returncode == 0
    adopt_release_commit(host)
    _surface(host.base, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(host.base, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)
    return host


def interrupted_full_deploy(host: Host, stage: str) -> str:
    """Run a real full deployment and SIGKILL it in the release-switch window.

    `stage` picks the exact side of the window: `before` leaves the journal at
    its committed phase, `after` has already cleared it. Both are produced by
    the real updater, not by a planted record, and in both the durable
    deployment is complete while `current` still names the serving release.
    """

    commit = push_tool_change(host)
    barrier = host.base / f"tail-barrier-{stage}"
    process = subprocess.Popen(
        ["sh", str(host.current / "deploy" / "vps-update.sh")],
        cwd=host.base,
        env={
            **host.env,
            f"FULL_TAIL_{stage.upper()}_BARRIER_FILE": str(barrier),
            "FULL_TAIL_RELEASE_FILE": str(host.base / "tail-release-never"),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_for(barrier, process, f"the {stage}-confirm window")
        assert (host.base / ".deploy_commit").read_text(encoding="ascii") == f"{commit}\n"
        assert current_target(host.base) == "releases/r1"
        if stage == "before":
            assert (host.base / JOURNAL_RELPATH).exists()
        else:
            assert not (host.base / JOURNAL_RELPATH).exists()
        kill_group(process)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=30)
    return commit


@pytest.mark.parametrize("stage", ["before", "after"])
def test_the_release_switch_window_is_completed_by_the_printed_command(full_host, stage):
    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, stage)
    release = base / "releases" / "r2"

    # The printed command is an absolute path into the release that wrote the
    # record, and `current` is not it. Entering `current` instead is refused -
    # the fingerprint only matches the release the transaction came from -
    # which is the failure this entry binding exists to prevent.
    pinned = recover(
        host, release, "--recover-full", WUWATERM_DEPLOY_ROOT=str(base / "current")
    )
    assert pinned.returncode == 1, pinned.stdout
    if stage == "before":
        # The record's fingerprint only matches the release that wrote it, so
        # the wrong checkout refuses before anything is cleared.
        assert "full_recovery_tool_mismatch" in pinned.stderr
        assert (base / JOURNAL_RELPATH).exists()
    else:
        # Nothing is left to recover; the settlement refuses a commit r1 does
        # not carry instead of moving `current` on the strength of a pointer.
        assert "does not carry" in pinned.stderr
    assert current_target(base) == "releases/r1"

    result = recover(host, release, "--recover-full")

    assert result.returncode == 0, result.stderr
    assert current_target(base) == "releases/r2"
    assert not (base / JOURNAL_RELPATH).exists()
    assert commit in result.stdout
    assert digest(base / "data" / "terms.db") == digest(base / "data" / "candidate-seed.db")

    # Repeating it changes nothing and touches no service: the settlement is a
    # completion, not a second deployment.
    before = stops(base / "docker.log")
    again = recover(host, release, "--recover-full")
    assert again.returncode == 0, again.stderr
    assert current_target(base) == "releases/r2"
    assert stops(base / "docker.log") == before
    assert "already" in again.stdout or "is current" in again.stdout


def test_a_rollback_keeps_current_on_the_serving_release(full_host):
    host = full_host
    base = host.base
    old_pointer = host.head
    commit = push_tool_change(host)
    # The failed candidate: the release the interrupted deployment created.
    assert create_release(host, commit) == "r2"
    release = base / "releases" / "r2"
    old_bytes = (base / "data" / "terms.db").read_bytes()
    new_bytes = (base / "data" / "candidate-seed.db").read_bytes()
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    backup_rel = f"data/deployment-backups/terms.{TXN}.{old_hash}.db"
    (base / backup_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base / "data" / "terms.db", base / backup_rel)
    # The interruption: the database was promoted and both surfaces now run the
    # candidate, before the manifest and pointer were published.
    (base / "data" / "terms.db").write_bytes(new_bytes)
    _surface(base, "bot", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{commit}", running=True)
    _surface(base, "api", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{commit}", running=True)
    (base / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    (base / f"tag-{API_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    _plant(
        base,
        _journal(
            release,
            phase="started",
            old_db_sha=old_hash,
            db_backup=backup_rel,
            old_pointer=old_pointer,
            source_commit=commit,
            new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
            candidate_rel=f"data/candidates/terms.{TXN}.db",
        ),
    )

    result = recover(host, release, "--recover-full")

    assert result.returncode == 0, result.stderr
    # The host is back on the binding it had; the candidate never deployed, so
    # `current` must not follow it.
    assert current_target(base) == "releases/r1"
    assert (base / "data" / "terms.db").read_bytes() == old_bytes
    assert (base / ".deploy_commit").read_text(encoding="ascii") == f"{old_pointer}\n"
    assert not (base / JOURNAL_RELPATH).exists()
    assert (base / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert (base / "api-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert "r1 is current" in result.stdout


def test_an_unprovable_binding_is_refused_without_moving_current(full_host):
    host = full_host
    base = host.base
    interrupted_full_deploy(host, "after")
    release = base / "releases" / "r2"
    # A live database that no longer matches the published manifest: the
    # pointer alone must never be taken as the answer.
    with (base / "data" / "terms.db").open("ab") as stream:
        stream.write(b"drift")

    result = recover(host, release, "--recover-full")

    assert result.returncode == 1
    assert current_target(base) == "releases/r1"
    assert "could not be proven" in result.stderr


def test_a_pointer_this_release_did_not_publish_is_refused_and_named(full_host):
    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    # An unrelated release, built at the commit that is still serving: it is a
    # real checkout, it is clean, and it did not publish the pointer.
    assert create_release(host, host.head) == "r3"
    stranger = base / "releases" / "r3"

    refused = recover(host, stranger, "--recover-full")

    assert refused.returncode == 1
    assert current_target(base) == "releases/r1"
    assert commit in refused.stderr
    assert "run recovery from r2" in refused.stderr

    # The release the pointer names still settles the link.
    settled = recover(host, base / "releases" / "r2", "--recover-full")
    assert settled.returncode == 0, settled.stderr
    assert current_target(base) == "releases/r2"


def test_an_open_runtime_transaction_is_not_settled_by_the_full_entry(runtime_host):
    host = runtime_host
    base = host.base
    push_tool_change(host)
    # The runtime transaction cannot finish its own rollback, so its record
    # stays open and the pointer may be anywhere in that transition. Nothing
    # settles `current` from it.
    failed = subprocess.run(
        ["sh", str(host.current / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=base,
        env={
            **host.env,
            "RUNTIME_FAKE_READINESS_FAIL": "1",
            "RUNTIME_FAKE_ROLLBACK_START": "api",
        },
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert failed.returncode != 0
    journal = (base / RUNTIME_JOURNAL).read_text(encoding="utf-8")
    assert '"status": "unresolved"' in journal
    assert current_target(base) == "releases/r1"

    result = recover(host, base / "releases" / "r2", "--recover-full")

    assert result.returncode == 1
    assert "open runtime-only transaction" in result.stderr
    assert current_target(base) == "releases/r1"


def _write_runtime_tail_python(root: Path) -> None:
    """A python3 wrapper that stops after the runtime transaction committed.

    The real command runs first, so the record is in its committed state; the
    wrapper then waits, which is the window in which the release link has not
    been switched yet.
    """

    fake_python = root.parent / "bin" / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "set -u\n"
        f"real_python={sys.executable!r}\n"
        "case \"$*\" in\n"
        "  *'runtime_update.py --runtime-only'*)\n"
        "    \"$real_python\" \"$@\"\n"
        "    rc=$?\n"
        "    if [ -n \"${RUNTIME_TAIL_BARRIER_FILE:-}\" ]; then\n"
        "      printf ready > \"$RUNTIME_TAIL_BARRIER_FILE\"\n"
        "      while [ ! -e \"${RUNTIME_TAIL_RELEASE_FILE:-/nonexistent}\" ]; do sleep 0.05; done\n"
        "    fi\n"
        "    exit \"$rc\"\n"
        "    ;;\n"
        "esac\n"
        "exec \"$real_python\" \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)


@pytest.fixture()
def runtime_host(tmp_path):
    host = Host(tmp_path, runtime=True)
    _write_runtime_tail_python(host.fake_bin.parent / "install")
    assert host.migrate().returncode == 0
    return host


def test_the_runtime_tail_window_is_completed_from_the_recorded_binding(runtime_host):
    host = runtime_host
    base = host.base
    commit = push_tool_change(host)
    barrier = base / "runtime-tail-barrier"
    process = subprocess.Popen(
        ["sh", str(host.current / "deploy" / "vps-update.sh"), "--runtime-only"],
        cwd=base,
        env={
            **host.env,
            "RUNTIME_TAIL_BARRIER_FILE": str(barrier),
            "RUNTIME_TAIL_RELEASE_FILE": str(base / "runtime-tail-release-never"),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_for(barrier, process, "the runtime commit window")
        assert current_target(base) == "releases/r1"
        journal = (base / RUNTIME_JOURNAL).read_text(encoding="utf-8")
        assert '"status": "committed"' in journal
        assert commit in journal
        kill_group(process)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=30)

    release = base / "releases" / "r2"
    result = recover(host, release, "--recover-runtime")

    assert result.returncode == 0, result.stderr
    assert current_target(base) == "releases/r2"

    before = stops(base / "docker.log")
    again = recover(host, release, "--recover-runtime")
    assert again.returncode == 0, again.stderr
    assert current_target(base) == "releases/r2"
    assert stops(base / "docker.log") == before
