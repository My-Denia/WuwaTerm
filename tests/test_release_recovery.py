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
    API_ALT_IMAGE,
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

    # Recovery's own outcome line names the side it really settled, and it is
    # the whole of stdout while the flag is in use.
    raw = _run_recovery_script(
        host, release, "full_update.py", "recover", "--settle-result"
    )
    assert raw.returncode == 0, raw.stderr
    assert raw.stdout == f"settle-result v1 kind=old-verified pointer=present:{old_pointer}\n"

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


@pytest.fixture()
def migrated_host(tmp_path):
    """The release layout as `migrate-layout.sh` left it.

    The migration records the in-place HEAD as r1's commit while the deployment
    pointer keeps naming the commit that was deployed before it, so r1's marker
    legitimately leads the pointer and a rollback restores a commit no release
    carries.
    """

    host = Host(tmp_path)
    _write_full_fake_docker(host.fake_bin.parent / "install")
    _write_full_fake_python(host.fake_bin.parent / "install")
    assert host.migrate().returncode == 0
    _surface(host.base, "bot", image=OLD_IMAGE, ref=OLD_REF, running=True)
    _surface(host.base, "api", image=OLD_IMAGE, ref=OLD_REF, running=True)
    return host


def _assert_the_marker_leads_the_pointer(host: Host) -> None:
    base = host.base
    assert (base / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert (
        (base / "releases" / "r1" / ".release-commit")
        .read_text(encoding="ascii")
        .strip()
        == host.head
    )


@pytest.mark.parametrize("layout", ["full_host", "migrated_host"])
def test_a_per_surface_old_binding_is_settled_without_moving_current(
    request, layout
):
    """One image per manifest cannot re-express a host that ran two old ones.

    The bot was stopped on the image its commit's manifest records and the API
    was serving a different, older one - a state a hand-recovered host really
    has. Recovery restores each recorded image and verifies that binding step by
    step, so the single-image `published` prover never has to re-express it.

    The two layouts differ in what `current` can be asked: on an aligned host
    the release r1 carries the commit the pointer names and the link needs no
    change at all; after a migration r1's marker is the in-place HEAD, so no
    release's source corresponds to the restored commit and the settlement says
    that instead of inventing one. Both are a completed recovery with the link
    where it was, and both stay that way when the command is repeated.
    """

    host = request.getfixturevalue(layout)
    base = host.base
    if layout == "migrated_host":
        _assert_the_marker_leads_the_pointer(host)
        old_pointer = OLD_COMMIT
    else:
        old_pointer = host.head
    commit = push_tool_change(host)
    assert create_release(host, commit) == "r2"
    release = base / "releases" / "r2"
    old_bytes = (base / "data" / "terms.db").read_bytes()
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    backup_rel = f"data/deployment-backups/terms.{TXN}.{old_hash}.db"
    (base / backup_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base / "data" / "terms.db", base / backup_rel)
    _surface(base, "bot", image=OLD_IMAGE, ref=OLD_REF, running=False)
    _surface(base, "api", image=API_ALT_IMAGE, ref=OLD_REF, running=True)
    (base / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    (base / f"tag-{API_ROLLBACK_REF}").write_text(f"{API_ALT_IMAGE}\n", encoding="ascii")
    _plant(
        base,
        _journal(
            release,
            phase="armed",
            old_db_sha=old_hash,
            db_backup=backup_rel,
            old_pointer=old_pointer,
            source_commit=commit,
            new_db_sha="0" * 64,
            candidate_rel=f"data/candidates/terms.{TXN}.db",
            bot_id=OLD_IMAGE,
            api_id=API_ALT_IMAGE,
            bot_running=False,
            api_running=True,
        ),
    )

    result = recover(host, release, "--recover-full")

    assert result.returncode == 0, result.stderr
    assert "rolled back to the previous binding" in result.stdout + result.stderr
    assert current_target(base) == "releases/r1"
    assert not (base / JOURNAL_RELPATH).exists()
    assert (base / ".deploy_commit").read_text(encoding="ascii") == f"{old_pointer}\n"
    assert (base / "data" / "terms.db").read_bytes() == old_bytes
    assert (base / "bot-image").read_text(encoding="ascii").strip() == OLD_IMAGE
    assert (base / "api-image").read_text(encoding="ascii").strip() == API_ALT_IMAGE
    assert (base / "bot-running").read_text(encoding="ascii").strip() == "false"
    assert (base / "api-running").read_text(encoding="ascii").strip() == "true"
    if layout == "full_host":
        # The link already names the commit the rollback restored.
        assert "needs no change" in result.stdout
    else:
        # The restored commit is the one the in-place checkout deployed, and no
        # release's source carries it: named as such, never invented.
        assert "no release" in result.stderr
        assert "is not switched on a guess" in result.stderr
        assert "predates" not in result.stderr

    # The journal is gone, so the repeat has nothing to recover. It reads the
    # link's own evidence and reports a no-op - still with the link unmoved,
    # and still without claiming anything it did not verify.
    before = stops(base / "docker.log")
    again = recover(host, release, "--recover-full")
    assert again.returncode == 0, again.stderr
    assert current_target(base) == "releases/r1"
    assert stops(base / "docker.log") == before
    assert "no full transaction to recover" in again.stderr
    if layout == "full_host":
        assert "no transaction is pending" in again.stdout
        assert "needs no adjustment" in again.stdout
    else:
        assert "no source entry was found to adjust" in again.stderr
        assert "predates" not in again.stderr
    assert "recovery restored" not in again.stdout
    assert "recovery restored" not in again.stderr


def test_a_migrated_full_rollback_reports_success_without_switching_current(
    migrated_host,
):
    """The pointer names a commit no release's source carries.

    The migration never rewrites the pointer, so a full rollback restores the
    commit that was deployed in place. There is no checkout to switch to, and
    the recovery did restore the host; naming the absent correspondence and
    leaving `current` alone is the honest outcome.
    """

    host = migrated_host
    base = host.base
    _assert_the_marker_leads_the_pointer(host)
    commit = push_tool_change(host)
    assert create_release(host, commit) == "r2"
    release = base / "releases" / "r2"
    old_bytes = (base / "data" / "terms.db").read_bytes()
    new_bytes = (base / "data" / "candidate-seed.db").read_bytes()
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    backup_rel = f"data/deployment-backups/terms.{TXN}.{old_hash}.db"
    (base / backup_rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base / "data" / "terms.db", base / backup_rel)
    # The interruption: the database and both surfaces already run the
    # candidate, so recovery has to put the recorded old binding back.
    (base / "data" / "terms.db").write_bytes(new_bytes)
    _surface(
        base, "bot", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{commit}", running=True
    )
    _surface(
        base, "api", image=NEW_IMAGE, ref=f"wuwaterm-runtime:{commit}", running=True
    )
    (base / f"tag-{BOT_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    (base / f"tag-{API_ROLLBACK_REF}").write_text(f"{OLD_IMAGE}\n", encoding="ascii")
    _plant(
        base,
        _journal(
            release,
            phase="started",
            old_db_sha=old_hash,
            db_backup=backup_rel,
            old_pointer=OLD_COMMIT,
            source_commit=commit,
            new_db_sha=hashlib.sha256(new_bytes).hexdigest(),
            candidate_rel=f"data/candidates/terms.{TXN}.db",
        ),
    )

    result = recover(host, release, "--recover-full")

    assert result.returncode == 0, result.stderr
    assert "rolled back to the previous binding" in result.stdout + result.stderr
    assert current_target(base) == "releases/r1"
    assert not (base / JOURNAL_RELPATH).exists()
    assert (base / "data" / "terms.db").read_bytes() == old_bytes
    assert (base / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert "no release" in result.stderr
    assert "is not switched on a guess" in result.stderr


@pytest.fixture()
def migrated_runtime_host(tmp_path):
    host = Host(tmp_path, runtime=True)
    assert host.migrate().returncode == 0
    return host


def test_a_migrated_runtime_rollback_reports_success_without_switching_current(
    migrated_runtime_host,
):
    """The same misalignment after a runtime-only rollback.

    The runtime record restores the pointer it recorded, which is the commit
    the in-place deployment published, not the commit the migrated release
    carries. The rollout did roll back and the link does need no move.
    """

    host = migrated_runtime_host
    base = host.base
    _assert_the_marker_leads_the_pointer(host)
    push_tool_change(host)
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
    assert '"status": "unresolved"' in (base / RUNTIME_JOURNAL).read_text(
        encoding="utf-8"
    )
    assert current_target(base) == "releases/r1"

    result = recover(host, base / "releases" / "r2", "--recover-runtime")

    assert result.returncode == 0, result.stderr
    assert '"status": "rolled_back"' in (base / RUNTIME_JOURNAL).read_text(
        encoding="utf-8"
    )
    assert current_target(base) == "releases/r1"
    assert (base / ".deploy_commit").read_text(encoding="ascii") == f"{OLD_COMMIT}\n"
    assert "is not switched on a guess" in result.stderr

    # A rolled-back runtime record stays closed: repeating the recovery is
    # refused exactly as before, and the settlement never runs. This change
    # added no idempotency to that path.
    again = recover(host, base / "releases" / "r2", "--recover-runtime")
    assert again.returncode == 1
    assert "no_recovery_needed" in again.stderr
    assert current_target(base) == "releases/r1"


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
    # The runtime entry reports its own outcome line: a committed record is the
    # NEW binding re-proved, not a rollback.
    raw = _run_recovery_script(
        host, release, "runtime_update.py", "--recover-runtime", "--settle-result"
    )
    assert raw.returncode == 0, raw.stderr
    assert raw.stdout == f"settle-result v1 kind=new-verified pointer=present:{commit}\n"

    result = recover(host, release, "--recover-runtime")

    assert result.returncode == 0, result.stderr
    assert current_target(base) == "releases/r2"

    before = stops(base / "docker.log")
    again = recover(host, release, "--recover-runtime")
    assert again.returncode == 0, again.stderr
    assert current_target(base) == "releases/r2"
    assert stops(base / "docker.log") == before


# --- what the settlement accepts as evidence -------------------------------
#
# Settling the link is the last step of a recovery, and the one state it must
# never produce is a link moved on evidence it did not read. These cases take
# the same completed deployment - the transaction is closed and `current` is one
# release behind - and break one read at a time.


def _break_candidate_auth(host: Host, release: Path, defect: str) -> None:
    """One way a candidate checkout stops being the source of its commit."""

    marker = release / ".release-commit"
    if defect == "marker-missing":
        marker.unlink()
    elif defect == "marker-wrong":
        marker.write_text("0" * 40 + "\n", encoding="ascii")
    elif defect == "head-moved":
        host.git(release, "checkout", "-q", "--detach", host.head)
    elif defect == "dirty":
        (release / "release.txt").write_text("uncommitted\n", encoding="utf-8")
    elif defect == "status-unreadable":
        # A corrupt index is exactly what makes `git status` exit non-zero while
        # HEAD still reads: the state that used to pass as "clean".
        (release / ".git" / "index").write_bytes(b"not an index\n")
    else:  # pragma: no cover - the parameter list is exhaustive
        raise AssertionError(defect)


@pytest.mark.parametrize(
    "defect",
    ["marker-missing", "marker-wrong", "head-moved", "dirty", "status-unreadable"],
)
def test_a_candidate_that_cannot_be_authenticated_never_moves_current(
    full_host, defect
):
    """A near miss is refused as a source-entry problem, not as "no carrier".

    The deployment is complete and this release records its commit somewhere:
    at HEAD with a missing or wrong marker (`marker-missing`, `marker-wrong`),
    in the marker with a moved HEAD (`head-moved`), or in both from a tree that
    is dirty or whose status cannot be read. None of that proves the checkout IS
    that source, and a half answer must become neither a switch nor the "no
    release carries it" no-op.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    release = base / "releases" / "r2"
    assert commit not in (base / "releases" / "r1" / ".release-commit").read_text(
        encoding="ascii"
    )
    _break_candidate_auth(host, release, defect)

    result = recover(host, release, "--recover-full")

    assert result.returncode != 0, result.stdout + result.stderr
    assert current_target(base) == "releases/r1"
    assert "no release" not in result.stderr


def test_a_missing_marker_does_not_hide_the_release_that_carries_the_commit(
    full_host,
):
    """The scan reads HEAD as well as the marker.

    A checkout whose `.release-commit` was removed still holds the commit at
    HEAD. Running the settlement from an unrelated release must name that one,
    not report that no release carries the commit.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    (base / "releases" / "r2" / ".release-commit").unlink()
    assert create_release(host, host.head) == "r3"

    refused = recover(host, base / "releases" / "r3", "--recover-full")

    assert refused.returncode == 1
    assert current_target(base) == "releases/r1"
    assert commit in refused.stderr
    assert "run recovery from r2" in refused.stderr
    # A runtime record is always terminal for a second recovery here, so the
    # hint names the mode that can settle it.
    assert "--recover-runtime" not in refused.stderr


def test_the_current_marker_alone_does_not_make_the_link_right(full_host):
    """A marker is a label; the link is right only when the source matches.

    Here `current` labels the deployed commit while its HEAD is still the
    release before it - a checkout that cannot serve what the marker claims.
    The settlement must not read that label as "already current" and stop: it
    names the release whose source really carries the commit.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    (base / "releases" / "r1" / ".release-commit").write_text(
        f"{commit}\n", encoding="ascii"
    )
    # An unrelated release, so the candidate's own record is not the one the
    # settlement is asked about.
    assert create_release(host, host.head) == "r3"

    refused = recover(host, base / "releases" / "r3", "--recover-full")

    assert refused.returncode == 1
    assert current_target(base) == "releases/r1"
    assert "is current for" not in refused.stdout
    assert "does not carry" in refused.stderr
    assert "run recovery from r2" in refused.stderr


def _write_outcome_mangling_python(fake_bin: Path, sed_expression: str) -> None:
    """A python3 wrapper that corrupts the recovery command's outcome line.

    The settlement reads that line from a separate process, so the boundary is
    where a wrong or truncated answer can arrive. Only the recovery invocation
    is piped through `sed`; every other call runs unchanged.
    """

    assert "'" not in sed_expression
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "set -u\n"
        f"real_python={sys.executable!r}\n"
        'case "$*" in\n'
        "  *'full_update.py recover --settle-result'*)\n"
        f"    \"$real_python\" \"$@\" | sed -e '{sed_expression}'\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        'exec "$real_python" "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)


@pytest.mark.parametrize(
    ("expression", "expected", "why"),
    [
        (
            "s/^settle-result.*/settle-result v1 kind=rolled-back pointer=absent/",
            "not a result this settlement can act on",
            "an unknown kind",
        ),
        (
            "s/^settle-result.*/settle-result v1 kind=new-verified pointer=present:"
            + "0" * 40
            + "/",
            "they disagree",
            "a commit the pointer does not name",
        ),
        ("s/^settle-result.*/recovery done/", "not a result", "no outcome at all"),
    ],
)
def test_an_outcome_the_settlement_cannot_act_on_is_refused(
    full_host, expression, expected, why
):
    """Nothing moves on an outcome that is unknown or contradicts the pointer."""

    host = full_host
    base = host.base
    interrupted_full_deploy(host, "after")
    _write_outcome_mangling_python(host.fake_bin, expression)

    result = recover(host, base / "releases" / "r2", "--recover-full")

    assert result.returncode == 1, why
    assert current_target(base) == "releases/r1"
    assert expected in result.stderr


def test_a_settlement_with_nothing_to_do_claims_no_recovery(full_host):
    """A no-op is a no-op: it verified the link, not the deployment.

    After the tail window was completed the journal is gone and `current` names
    the serving release. The live database no longer matches the published
    manifest, which the settlement does not read for standing still - so it is
    still a no-op with the link where it is, and it says only that much: no
    recovery happened on this run.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    released = recover(host, base / "releases" / "r2", "--recover-full")
    assert released.returncode == 0, released.stderr
    assert current_target(base) == "releases/r2"
    with (base / "data" / "terms.db").open("ab") as stream:
        stream.write(b"drift")

    result = recover(host, base / "releases" / "r2", "--recover-full")

    assert result.returncode == 0, result.stderr
    assert current_target(base) == "releases/r2"
    assert "no transaction is pending" in result.stdout
    assert "releases/r2 is current" in result.stdout
    assert commit in result.stdout
    assert "needs no adjustment" in result.stdout
    assert "recovery restored" not in result.stdout
    assert "recovery restored" not in result.stderr
    assert "confirmed on the new binding" not in result.stderr


@pytest.mark.parametrize("pointer", ["missing", "invalid"])
def test_the_settlement_never_reads_a_pointer_it_cannot_read(
    full_host, pointer
):
    """Missing and unreadable are different answers, and neither is a switch."""

    host = full_host
    base = host.base
    interrupted_full_deploy(host, "after")
    if pointer == "missing":
        (base / ".deploy_commit").unlink()
    else:
        # The pointer is published read-only; the content is what is wrong here.
        (base / ".deploy_commit").chmod(0o644)
        (base / ".deploy_commit").write_text("not a deployment\n", encoding="ascii")

    result = recover(host, base / "releases" / "r2", "--recover-full")

    assert current_target(base) == "releases/r1"
    if pointer == "missing":
        # Nothing is deployed to name, and no recovery happened on this run.
        assert result.returncode == 0, result.stderr
        assert "no deployment is recorded" in result.stdout
        assert "recovery restored" not in result.stdout
    else:
        # An unreadable pointer is not "nothing deployed".
        assert result.returncode == 1
        assert "deployment pointer cannot be read" in result.stderr


def test_the_recovery_entry_also_settles_from_a_relative_invocation(full_host):
    """The settlement must not depend on how the entry was spelled.

    A relative `./deploy/vps-update.sh` inside the release resolves to the same
    checkout as an absolute path, so it settles from that release's record
    instead of following `current` into the release that is still serving.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "after")
    release = base / "releases" / "r2"

    result = subprocess.run(
        ["sh", "./deploy/vps-update.sh", "--recover-full"],
        cwd=release,
        env=fresh_shell_env(host),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert current_target(base) == "releases/r2"
    assert commit in result.stdout


def _run_recovery_script(host: Host, release: Path, script: str, *args: str):
    """Run one recovery tool directly, under the deployment lock it requires.

    The updater opens the lock on fd 9 and the tools inherit it, so a direct run
    has to bind that descriptor itself - the same way `migrate-layout.sh` does.
    """

    return subprocess.run(
        [
            "sh",
            "-c",
            'set -eu\nexec 9>>"$1/.deployments/.deployment.lock"\nshift\nexec "$@"',
            "sh",
            str(host.base),
            sys.executable,
            str(release / "scripts" / script),
            *args,
        ],
        cwd=release,
        env=fresh_shell_env(host),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_the_outcome_line_is_the_whole_of_stdout(full_host):
    """One machine line on stdout, and only after the record was closed.

    The settlement reads this line, so nothing else may share stdout with it,
    and the human log of the same run belongs on stderr.
    """

    host = full_host
    base = host.base
    commit = interrupted_full_deploy(host, "before")
    release = base / "releases" / "r2"

    result = _run_recovery_script(
        host, release, "full_update.py", "recover", "--settle-result"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"settle-result v1 kind=new-verified pointer=present:{commit}\n"
    assert "confirmed on the new binding" in result.stderr
    assert not (base / JOURNAL_RELPATH).exists()

    # Without the option the tool logs where it always did, so a human running
    # it directly reads the same output and exit status as before.
    plain = _run_recovery_script(host, release, "full_update.py", "recover")
    assert plain.returncode == 0
    assert plain.stdout == "no full transaction to recover\n"


def test_a_failed_recovery_reports_no_outcome_and_keeps_its_status(full_host):
    """A refused recovery is not a result: same non-zero status, no claim."""

    host = full_host
    base = host.base
    interrupted_full_deploy(host, "before")
    release = base / "releases" / "r1"
    # The record binds the fingerprint of the release that wrote it.
    result = _run_recovery_script(
        host, release, "full_update.py", "recover", "--settle-result"
    )

    assert result.returncode == 1
    assert result.stdout == "settle-result v1 kind=failed\n"
    assert "full_recovery_tool_mismatch" in result.stderr
    assert (base / JOURNAL_RELPATH).exists()
