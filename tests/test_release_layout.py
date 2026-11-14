"""Release layout: <base>/{releases/rN, current -> releases/rN, data, ...}.

The helpers in deploy/release-layout.sh run against plain temporary
directories. The updater and the migration run against a temporary host whose
Docker is the fake from test_deploy_scripts and whose git is real.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from test_deploy_scripts import NEW_COMMIT, _make_deploy_harness
from test_runtime_only_deploy import _make_runtime_harness

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "release-layout.sh"
MIGRATE = ROOT / "deploy" / "migrate-layout.sh"
GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")


def lib(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", f'. "{LIB}"\n{script}'],
        cwd=cwd, text=True, capture_output=True, check=False,
    )


def releases(base: Path) -> list[str]:
    return sorted(p.name for p in (base / "releases").iterdir())


def make_base(tmp_path: Path, names: list[str], current: str, previous: str | None = None) -> Path:
    base = tmp_path / "base"
    (base / "data").mkdir(parents=True)
    (base / "data" / "terms.db").write_text("terms")
    for name in names:
        (base / "releases" / name).mkdir(parents=True)
    (base / "current").symlink_to(f"releases/{current}")
    if previous:
        (base / "releases" / current / ".release-previous").write_text(previous + "\n")
    return base


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], "1"),
        (["r1"], "2"),
        (["r1", "r3", "r10"], "11"),
        # Not releases: other schemes' directories, a failed clone, a file, a symlink.
        (["r2", "r2x", "r0", "abc1", ".incoming-r40", "b9d13fc7a8ee"], "3"),
    ],
)
def test_next_release_number_is_the_highest_plus_one(tmp_path, names, expected):
    directory = tmp_path / "releases"
    directory.mkdir()
    for name in names:
        (directory / name).mkdir()
    (directory / "r50").symlink_to(directory / "r2")
    (directory / "r60").write_text("not a directory")
    result = lib(f'release_next_number "{directory}"', tmp_path)
    assert result.stdout.strip() == expected, result.stderr


def test_publish_switches_current_with_one_rename_and_records_the_previous_release(tmp_path):
    base = make_base(tmp_path, ["r1", "r2"], current="r1")
    seen: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        # A reader that finds `current` missing, even once, saw a gap.
        while not stop.is_set():
            try:
                seen.append(os.readlink(base / "current"))
            except FileNotFoundError:
                seen.append("MISSING")

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for name in ["r2", "r1"] * 40:
            result = lib(f'release_publish "{base}" {name}', tmp_path)
            assert result.returncode == 0, result.stderr
    finally:
        stop.set()
        thread.join()

    assert "MISSING" not in seen
    assert set(seen) <= {"releases/r1", "releases/r2"}
    assert (base / "current").is_symlink()
    assert sorted(p.name for p in base.iterdir()) == ["current", "data", "releases"]


def test_publish_records_what_it_replaces_and_a_redeploy_keeps_it(tmp_path):
    base = make_base(tmp_path, ["r1", "r2"], current="r1")
    assert lib(f'release_publish "{base}" r2', tmp_path).returncode == 0
    assert (base / "releases/r2/.release-previous").read_text() == "r1\n"
    assert os.readlink(base / "current") == "releases/r2"
    # The same release published again must not record itself as previous.
    assert lib(f'release_publish "{base}" r2', tmp_path).returncode == 0
    assert (base / "releases/r2/.release-previous").read_text() == "r1\n"


def test_publish_of_a_missing_release_leaves_current_alone(tmp_path):
    base = make_base(tmp_path, ["r1"], current="r1")
    assert lib(f'release_publish "{base}" r2', tmp_path).returncode != 0
    assert os.readlink(base / "current") == "releases/r1"


def test_prune_keeps_current_and_its_previous_and_nothing_else(tmp_path):
    # r3 failed (never current); r4 is current and replaced r2, not r3.
    base = make_base(tmp_path, ["r1", "r2", "r3", "r4", ".incoming-r5"], current="r4", previous="r2")
    old_scheme = base / "releases" / "b9d13fc7a8ee90aee46f3cd7a3aa5aa6b041a33d"
    old_scheme.mkdir()
    result = lib(f'release_prune "{base}"', tmp_path)
    assert result.returncode == 0, result.stderr
    assert releases(base) == ["b9d13fc7a8ee90aee46f3cd7a3aa5aa6b041a33d", "r2", "r4"]


def test_prune_never_reaches_shared_state_through_a_release(tmp_path):
    base = make_base(tmp_path, ["r1", "r2", "r3"], current="r3", previous="r2")
    (base / "state").mkdir()
    (base / "state" / "chat_settings.json").write_text("{}")
    # A compatibility link inside the release being removed points at the state.
    (base / "releases/r1/data").symlink_to("../../data")
    (base / "releases/r1/state").symlink_to("../../state")
    assert lib(f'release_prune "{base}"', tmp_path).returncode == 0
    assert releases(base) == ["r2", "r3"]
    assert (base / "data/terms.db").read_text() == "terms"
    assert (base / "state/chat_settings.json").read_text() == "{}"


def test_prune_keeps_a_release_that_holds_real_state(tmp_path):
    base = make_base(tmp_path, ["r1", "r2", "r3"], current="r3", previous="r2")
    (base / "releases/r1/data").mkdir()
    (base / "releases/r1/data/terms.db").write_text("old in-place data")
    result = lib(f'release_prune "{base}"', tmp_path)
    assert result.returncode == 0
    assert "keeping r1" in result.stderr
    assert (base / "releases/r1/data/terms.db").read_text() == "old in-place data"


def test_finish_prunes_nothing_when_the_switch_fails(tmp_path):
    base = make_base(tmp_path, ["r1", "r2", "r3"], current="r1")
    # r9 does not exist, so the switch fails; r2 and r3 must survive.
    result = lib(f'release_finish "{base}" r9', tmp_path)
    assert result.returncode != 0
    assert releases(base) == ["r1", "r2", "r3"]
    assert os.readlink(base / "current") == "releases/r1"


class Host:
    """A temporary /opt/wuwaterm in the old layout: one in-place git checkout."""

    def __init__(self, tmp_path: Path, *, runtime: bool = False):
        make = _make_runtime_harness if runtime else _make_deploy_harness
        root, self.env, self.old_hash, self.new_hash = make(tmp_path)
        self.base = tmp_path / "opt-wuwaterm"
        self.base.mkdir()
        self.fake_bin = Path(self.env["PATH"].split(os.pathsep)[0])
        (self.fake_bin / "git").unlink()  # the stub; this host has real git
        # What the fake Docker keeps in files belongs to the host, not the checkout.
        tracked = {"deploy", "scripts", "src"}
        shared = {"data", "state", "state-api", ".deployments", ".env", ".deploy_commit"}
        for entry in list(root.iterdir()):
            if entry.name not in tracked | shared:
                shutil.move(str(entry), self.base / entry.name)
        (self.base / "scripts").mkdir()
        shutil.copy2(root / "scripts" / "verify_db.py", self.base / "scripts" / "verify_db.py")
        self.current = self.base / "current"
        root.rename(self.current)
        (self.current / ".deployments" / ".deployment.lock").touch(mode=0o600)
        shutil.copy2(ROOT / ".gitignore", self.current / ".gitignore")
        for name in ("release-layout.sh", "migrate-layout.sh"):
            shutil.copy2(ROOT / "deploy" / name, self.current / "deploy" / name)
        self.origin = tmp_path / "origin.git"
        self.work = tmp_path / "work"
        self.git(self.current, "init", "-q", "-b", "main")
        self.git(self.current, "add", "-A")
        self.git(self.current, "commit", "-q", "-m", "c1")
        subprocess.run(["git", "clone", "-q", "--bare", self.current, self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.work], check=True)
        self.git(self.current, "remote", "add", "origin", str(self.origin))
        self.git(self.current, "fetch", "-q", "origin")
        self.head = self.rev(self.current)
        self.env["WUWATERM_DEPLOY_ROOT"] = str(self.current)
        self.env["FAKE_DEPLOY_ROOT"] = str(self.base)
        self.target = NEW_COMMIT

    @staticmethod
    def git(cwd: Path, *args: str) -> str:
        done = subprocess.run([*GIT, *args], cwd=cwd, text=True, capture_output=True, check=True)
        return done.stdout.strip()

    def rev(self, cwd: Path) -> str:
        return self.git(cwd, "rev-parse", "HEAD")

    def push(self, name: str) -> str:
        """A new commit on origin/main; the fake Docker is told it is what gets built."""
        (self.work / f"{name}.txt").write_text(name)
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", name)
        self.git(self.work, "push", "-q", "origin", "main")
        sha = self.rev(self.work)
        self.retarget(sha)
        return sha

    def retarget(self, sha: str) -> None:
        """Tell the fake Docker which commit the next deployment builds."""
        for stub in self.fake_bin.iterdir():
            stub.write_text(stub.read_text().replace(self.target, sha))
        self.target = sha

    def run(self, script: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(script), *args], cwd=self.base, text=True, capture_output=True,
            env={**self.env, **env}, timeout=120, check=False,
        )

    def migrate(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(self.current / "deploy" / "migrate-layout.sh", *args, str(self.base))

    def deploy(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return self.run(self.base / "current" / "deploy" / "vps-update.sh", *args, **env)

    def tree(self) -> dict[str, str]:
        """Every path below the base with its type, link target and mtime."""
        seen = {}
        for path in sorted(self.base.rglob("*")):
            if path.parent == self.base and path.suffix == ".log":
                continue  # the fake Docker's own call logs
            info = path.lstat()
            link = os.readlink(path) if path.is_symlink() else ""
            seen[str(path.relative_to(self.base))] = f"{info.st_mode:o} {info.st_mtime_ns} {link}"
        return seen


@pytest.fixture()
def host(tmp_path):
    return Host(tmp_path)


@pytest.fixture()
def runtime_host(tmp_path):
    return Host(tmp_path, runtime=True)


def test_migration_dry_run_prints_every_step_and_changes_nothing(host):
    before = host.tree()
    result = host.migrate("--dry-run")
    assert result.returncode == 0, result.stderr
    assert host.tree() == before
    assert "DRY RUN" in result.stdout
    for step in ("move the checkout to releases/r1", "move shared state data", "move shared state .env",
                 "move shared state .deployments", "point current at the release"):
        assert step in result.stdout
    assert "undo:" in result.stdout


def test_migration_moves_state_out_of_the_release_and_is_idempotent(host):
    result = host.migrate()
    assert result.returncode == 0, result.stderr
    base = host.base
    assert os.readlink(base / "current") == "releases/r1"
    release = base / "releases/r1"
    for item in ("data", "state"):
        assert (base / item).is_dir() and not (base / item).is_symlink()
        assert os.readlink(release / item) == f"../../{item}"
    for item in (".deployments", ".env", ".deploy_commit"):
        assert (base / item).is_file() or (base / item).is_dir()
        assert not os.path.lexists(release / item)
    assert (base / "data/terms.db").is_file()
    assert (base / ".env").is_file()
    assert (base / "releases/r1/.release-commit").read_text().strip() == host.head
    # Old container mount paths keep resolving until the containers are recreated.
    assert (base / "current/data/terms.db").is_file()
    assert host.git(base / "releases/r1", "status", "--porcelain", "--untracked-files=all") == ""

    after_first = host.tree()
    again = host.migrate()
    assert again.returncode == 0, again.stderr
    assert "already migrated" in again.stdout
    assert host.tree() == after_first


def test_migration_refuses_while_a_deployment_holds_the_lock(host):
    lock = host.current / ".deployments" / ".deployment.lock"
    holder = subprocess.Popen(
        ["sh", "-c", 'exec 9>>"$1"; flock -n 9 && echo held && sleep 60', "holder", str(lock)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        before = host.tree()
        result = host.migrate()
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert "holds" in result.stderr
    assert host.tree() == before


def test_migration_refuses_with_an_open_transaction_journal(host):
    (host.current / ".deployments" / ".full-transaction.json").write_text("{}")
    before = host.tree()
    result = host.migrate()
    assert result.returncode == 1
    assert "unresolved full transaction journal" in result.stderr
    assert host.tree() == before


def committed(host: Host) -> str:
    return (host.base / ".deploy_commit").read_text().strip()


def test_first_deploy_after_migration_builds_the_next_release_and_switches_current(host):
    assert host.migrate().returncode == 0
    commit = host.push("c2")

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    base = host.base
    assert os.readlink(base / "current") == "releases/r2"
    assert releases(base) == ["r1", "r2"]
    assert (base / "releases/r2/.release-commit").read_text().strip() == commit
    assert (base / "releases/r2/.release-previous").read_text().strip() == "r1"
    assert host.rev(base / "releases/r2") == commit
    # The containers now mount the shared paths, so the migration's links are gone.
    assert not [n for n in ("data", "state") if os.path.lexists(base / "releases/r1" / n)]
    assert committed(host) == commit
    # The new release carries code only; state stayed where it was.
    assert not [p for p in ("data", "state", ".env", ".deployments") if os.path.lexists(base / "releases/r2" / p)]
    assert hashlib.sha256((base / "data/terms.db").read_bytes()).hexdigest() == host.new_hash
    # Compose ran from the new release and mounted the shared state.
    actions = (base / "actions.log").read_text()
    assert f"-f {base}/releases/r2/deploy/docker-compose.yml" in actions
    assert "releases/r1/deploy" not in actions


def test_deploying_from_the_migrated_release_itself_drops_its_compatibility_links(host):
    assert host.migrate().returncode == 0
    base = host.base
    assert os.path.islink(base / "releases/r1/data")
    host.retarget(host.head)  # origin/main is what r1 already holds

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert releases(base) == ["r1"]
    assert os.readlink(base / "current") == "releases/r1"
    assert not [n for n in ("data", "state") if os.path.lexists(base / "releases/r1" / n)]
    assert (base / "data/terms.db").is_file() and (base / "state").is_dir()
    assert host.git(base / "releases/r1", "status", "--porcelain", "--untracked-files=all") == ""


def test_deploying_the_commit_already_current_reuses_the_release(host):
    assert host.migrate().returncode == 0
    host.push("c2")
    assert host.deploy().returncode == 0
    again = host.deploy()
    assert again.returncode == 0, again.stderr
    assert releases(host.base) == ["r1", "r2"]
    assert os.readlink(host.base / "current") == "releases/r2"
    assert (host.base / "releases/r2/.release-previous").read_text().strip() == "r1"


def test_retention_keeps_current_and_one_previous_release(host):
    assert host.migrate().returncode == 0
    for name in ("c2", "c3", "c4"):
        host.push(name)
        result = host.deploy()
        assert result.returncode == 0, result.stderr
    base = host.base
    assert releases(base) == ["r3", "r4"]
    assert os.readlink(base / "current") == "releases/r4"
    assert (base / "releases/r4/.release-previous").read_text().strip() == "r3"
    assert (base / "data/terms.db").is_file() and (base / ".env").is_file()
    assert (base / "state").is_dir() and (base / ".deployments").is_dir()


def test_failed_health_check_neither_switches_current_nor_prunes(host):
    assert host.migrate().returncode == 0
    base = host.base
    # Leftovers a passing deploy would remove.
    (base / "releases/r2").mkdir()
    (base / "releases/r3").mkdir()

    host.push("c2")
    failed = host.deploy(WUWATERM_FAIL_STEP="smoke")

    assert failed.returncode == 97, failed.stderr
    assert os.readlink(base / "current") == "releases/r1"
    # r4 is the release the failed attempt built; nothing was pruned.
    assert releases(base) == ["r1", "r2", "r3", "r4"]
    assert committed(host) == "1" * 40

    # A deploy that passes switches, keeps the release it replaced (not the
    # failed one), and prunes the rest.
    host.push("c3")
    ok = host.deploy()
    assert ok.returncode == 0, ok.stderr
    assert releases(base) == ["r1", "r5"]
    assert os.readlink(base / "current") == "releases/r5"
    assert (base / "releases/r5/.release-previous").read_text().strip() == "r1"


def test_a_release_without_shared_state_refuses_before_writing_anything(tmp_path):
    base = tmp_path / "opt"
    release = base / "releases" / "r1"
    shutil.copytree(ROOT / "deploy", release / "deploy")
    shutil.copytree(ROOT / "scripts", release / "scripts")
    before = sorted(str(p) for p in base.rglob("*"))
    result = subprocess.run(
        ["sh", str(release / "deploy/vps-update.sh")], cwd=tmp_path, text=True,
        capture_output=True, check=False, env={**os.environ, "WUWATERM_DEPLOY_ROOT": str(release)},
    )
    assert result.returncode == 1
    assert "holds no .env" in result.stderr
    assert sorted(str(p) for p in base.rglob("*")) == before


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/opt/wuwaterm/releases/r7", "/opt/wuwaterm"),
        ("/opt/wuwaterm/current", "/opt/wuwaterm/current"),
        ("/opt/wuwaterm/releases/r0", "/opt/wuwaterm/releases/r0"),
        ("/opt/wuwaterm/releases/r1x", "/opt/wuwaterm/releases/r1x"),
        ("/opt/wuwaterm/other/r1", "/opt/wuwaterm/other/r1"),
    ],
)
def test_only_a_release_directory_has_a_separate_state_root(path, expected):
    from scripts import deployment_admission

    assert deployment_admission.state_root(Path(path)) == Path(expected)


def runtime_host_with_leftovers(host: Host) -> Path:
    assert host.migrate().returncode == 0
    (host.base / "releases/r2").mkdir()
    (host.base / "releases/r3").mkdir()
    host.push("c2")
    return host.base


def test_runtime_only_update_switches_current_and_prunes_after_it_succeeds(runtime_host):
    base = runtime_host_with_leftovers(runtime_host)
    ok = runtime_host.deploy("--runtime-only")
    assert ok.returncode == 0, ok.stderr
    assert os.readlink(base / "current") == "releases/r4"
    assert releases(base) == ["r1", "r4"]
    assert committed(runtime_host) == runtime_host.target


def test_failed_runtime_only_update_neither_switches_current_nor_prunes(runtime_host):
    base = runtime_host_with_leftovers(runtime_host)
    failed = runtime_host.deploy("--runtime-only", RUNTIME_FAKE_READINESS_FAIL="1")
    assert failed.returncode != 0
    assert os.readlink(base / "current") == "releases/r1"
    assert releases(base) == ["r1", "r2", "r3", "r4"]
    assert committed(runtime_host) == "1" * 40


def test_runtime_only_refuses_containers_that_still_mount_the_old_paths(runtime_host):
    base = runtime_host_with_leftovers(runtime_host)
    docker = runtime_host.fake_bin / "docker"
    old = '"Source":"%s/data"'
    assert old in docker.read_text()
    docker.write_text(docker.read_text().replace(old, '"Source":"%s/current/data"'))

    result = runtime_host.deploy("--runtime-only")

    assert result.returncode != 0
    assert "mount_mismatch" in result.stderr
    # Refused at admission: nothing was stopped, nothing switched or pruned.
    assert " stop " not in (base / "actions.log").read_text()
    assert os.readlink(base / "current") == "releases/r1"
    assert releases(base) == ["r1", "r2", "r3", "r4"]
