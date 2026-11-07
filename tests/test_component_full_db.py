from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

import pytest

from scripts import product_manifest as manifest
from scripts import component_full_db as full_db
from wuwaterm.constants import get_source_profile
from wuwaterm.db import SCHEMA
from wuwaterm.normalize import normalize_text


SOURCE = Path(__file__).resolve().parents[1]
PYTHON = "/usr/bin/python3"
TRANSACTION = "20261106T110000Z-0123456789abcdef0123456789abcdef"


def make_verified_database(path: Path, *, variant: str = "a", rows: int = 510,
                           missing_exclusions: bool = False, bad_seed: bool = False) -> None:
    """Synthetic terms satisfy the unchanged production checks, not only SQLite schema."""
    profile = get_source_profile("arikatsu")
    exclusion = json.dumps({"version": 1, "min_occurrences": 10,
                            "max_aligned_ratio": [1, 5], "surfaces": []}, separators=(",", ":"))
    metadata = {"schema_version": "2", "source_profile": profile.name, "source_repo_url": profile.repo_url,
                "source_commit": profile.pinned_commit, "wutheringdata_commit": profile.pinned_commit,
                "source_game_version": profile.expected_game_version,
                "source_resource_version": profile.expected_resource_version,
                "source_changelist": profile.expected_changelist}
    if not missing_exclusions:
        metadata.update(free_text_lock_exclusions=exclusion, free_text_lock_exclusions_zh=exclusion)
    seeds = [("景燃", "Jingran", "jingran"), ("共鸣者", "Resonator", "badseed" if bad_seed else "gongmingzhe"),
             ("声骸", "Echo", "shenghai"), ("漂泊者", "Rover", "piaobozhe"),
             ("今汐", "Jinhsi", "jinxi"), ("守岸人", "Shorekeeper", "shouanren"),
             ("安可", "Encore", "anke"), ("卡卡罗", "Calcharo", "kakaluo"), ("椿", "Camellya", "chun")]
    categories = ("core_term", "resonator", "weapon", "echo", "item", "skill", "sonata_effect", "location")
    terms = [(zh, en, pinyin, categories[i % len(categories)]) for i, (zh, en, pinyin) in enumerate(seeds)]
    terms.extend((f"虚构术语{index:04d}{variant}", f"Synthetic Term {index:04d} {variant}",
                  f"fixture{index:04d}{variant}", categories[index % len(categories)]) for index in range(rows))
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", metadata.items())
        for index, (zh, en, pinyin, category) in enumerate(terms):
            connection.execute(
                "INSERT INTO terms(category,source_file,source_id,text_key,zh,en,zh_norm,en_norm,pinyin,pinyin_abbrev,priority) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (category, "fixture.json", str(index), f"key-{index}", zh, en, normalize_text(zh),
                 normalize_text(en), pinyin, "fx", index),
            )


def setup_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "deployment"
    (root / ".deployments").mkdir(parents=True)
    (root / ".deployments/.deployment.lock").touch(mode=0o600)
    (root / "data").mkdir()
    live, candidate = root / "data/terms.db", tmp_path / "candidate.db"
    make_verified_database(live, variant="old")
    make_verified_database(candidate, variant="new")
    live.chmod(0o600)
    return root, live, candidate


def invoke(root: Path, action: str, *values: object, lock: bool = True) -> subprocess.CompletedProcess[str]:
    code = (
        "import json,pathlib,sys; sys.path.insert(0,sys.argv[1]); import component_full_db as m; "
        "r=pathlib.Path(sys.argv[2]); a=sys.argv[3]; x=json.loads(sys.argv[4]); "
        "v=(m.prepare_candidate(r,pathlib.Path(x[0]),x[1]) if a=='candidate' else "
        "m.prepare_backup(r,x[0],x[1],x[2]) if a=='backup' else "
        "m.prepare_stage(r,x[0],pathlib.Path(x[1]),x[2]) if a=='stage' else "
        "{'verified':m.verify_candidate(r,x[0],x[1]) is None} if a=='verify' else "
        "m.permissions(pathlib.Path(x[0]))); print(json.dumps(v,sort_keys=True))"
    )
    command = [PYTHON, "-I", "-B", "-c", code, str(SOURCE / "scripts"), str(root), action,
               json.dumps(values, ensure_ascii=True)]
    if lock:
        command = ["sh", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
                   "fixture-lock", str(root / ".deployments/.deployment.lock"), *command]
    return subprocess.run(command, cwd=SOURCE, env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                          "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=120, check=False)


def verify_result(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def actual_verifier_exits(path: Path, output: Path) -> list[int]:
    commands = [
        [PYTHON, "-I", "-B", str(SOURCE / "scripts/verify_db.py"), str(path), "--profile", "arikatsu",
         "--min-category", "resonator", "--min-category", "weapon", "--min-category", "echo",
         "--min-category", "item", "--min-category", "skill", "--min-category", "sonata_effect",
         "--min-category", "location", "--require-free-text-exclusions", "--require-free-text-zh-exclusions"],
        [PYTHON, "-I", "-B", str(SOURCE / "scripts/verify_exact_hits.py"), str(path), "--sample-size", "500"],
        [PYTHON, "-I", "-B", str(SOURCE / "scripts/verify_seed_terms.py"), str(path), "--discrepancies", str(output)],
    ]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C"}
    return [subprocess.run(command, cwd=SOURCE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=120, check=False).returncode for command in commands]


def test_real_verifiers_candidate_backup_stage_and_retry(tmp_path: Path) -> None:
    root, live, input_path = setup_root(tmp_path)
    old_binding, old_stat = manifest.read_database_snapshot(live)
    old_bytes = live.read_bytes()
    assert actual_verifier_exits(input_path, tmp_path / "seed-output-valid.json") == [0, 0, 0]
    candidate = verify_result(invoke(root, "candidate", str(input_path), TRANSACTION))
    assert not list((root / ".deployments").glob(".component-full-verify-*"))
    assert candidate["verification"] == {
        "profile": "arikatsu", "exact_hit_sample_size": 500, "require_free_text_exclusions": True,
        "require_free_text_zh_exclusions": True, "verify_db_exit": 0, "exact_hits_exit": 0, "seed_terms_exit": 0,
    }
    assert candidate["path"] == f".deployments/component-full-candidates/{TRANSACTION}.db"
    assert candidate["binding"]["sha256"] != old_binding["sha256"]
    assert (root / candidate["path"]).stat().st_mode & 0o777 == 0o400
    assert verify_result(invoke(root, "candidate", str(input_path), TRANSACTION)) == candidate
    assert verify_result(invoke(root, "verify", candidate, TRANSACTION)) == {"verified": True}
    backup = verify_result(invoke(root, "backup", old_binding, old_stat, TRANSACTION))
    assert backup["sha256"] == old_binding["sha256"]
    assert backup["permissions"] == {"mode": 0o600, "uid": os.geteuid(), "gid": os.getegid()}
    assert (root / backup["path"]).read_bytes() == old_bytes
    assert verify_result(invoke(root, "backup", old_binding, old_stat, TRANSACTION)) == backup
    stage = verify_result(invoke(root, "stage", candidate, str(live), TRANSACTION))
    assert Path(stage["path"]).parent == live.parent
    assert Path(stage["path"]).name == f".component-full-promote-{TRANSACTION}.db"
    assert stage["sha256"] == candidate["binding"]["sha256"]
    assert stage["permissions"] == backup["permissions"]
    assert stage["stat"]["device"] == old_stat["device"]
    assert verify_result(invoke(root, "stage", candidate, str(live), TRANSACTION)) == stage
    assert live.read_bytes() == old_bytes
    assert manifest.read_database_snapshot(live) == (old_binding, old_stat)


@pytest.mark.parametrize("variant", ["missing-exclusions", "too-few-exact", "bad-seed"])
def test_real_verifiers_refuse_invalid_candidate_before_publication(tmp_path: Path, variant: str) -> None:
    root, live, _ = setup_root(tmp_path)
    candidate = tmp_path / (variant + ".db")
    make_verified_database(candidate, rows=20 if variant == "too-few-exact" else 510,
                           missing_exclusions=variant == "missing-exclusions", bad_seed=variant == "bad-seed")
    expected = {"missing-exclusions": [1, 0, 0], "too-few-exact": [0, 1, 0], "bad-seed": [0, 0, 1]}
    assert actual_verifier_exits(candidate, tmp_path / (variant + "-seed-output.json")) == expected[variant]
    before = manifest.read_database_snapshot(live)
    result = invoke(root, "candidate", str(candidate), TRANSACTION)
    assert result.returncode != 0
    assert "required verifiers" in result.stderr
    assert not (root / ".deployments/component-full-candidates" / (TRANSACTION + ".db")).exists()
    assert manifest.read_database_snapshot(live) == before


def test_symlinks_sidecars_and_missing_lock_refuse_before_copy(tmp_path: Path) -> None:
    root, live, candidate = setup_root(tmp_path)
    link = tmp_path / "candidate-link.db"
    link.symlink_to(candidate)
    assert invoke(root, "candidate", str(link), TRANSACTION).returncode != 0
    directory_link = tmp_path / "linked"
    directory_link.symlink_to(candidate.parent, target_is_directory=True)
    assert invoke(root, "candidate", str(directory_link / candidate.name), TRANSACTION).returncode != 0
    assert invoke(root, "candidate", str(candidate), TRANSACTION, lock=False).returncode != 0
    sidecar = Path(str(candidate) + "-wal")
    sidecar.write_text("not sqlite", encoding="ascii")
    assert invoke(root, "candidate", str(candidate), TRANSACTION).returncode != 0
    assert not (root / ".deployments/component-full-candidates" / (TRANSACTION + ".db")).exists()
    old = manifest.read_database_snapshot(live)
    Path(str(live) + "-shm").write_text("not sqlite", encoding="ascii")
    assert invoke(root, "backup", old[0], old[1], TRANSACTION).returncode != 0


def test_forged_verification_record_does_not_bypass_real_checks(tmp_path: Path) -> None:
    root, live, _ = setup_root(tmp_path)
    bad = tmp_path / "missing-exclusions.db"
    make_verified_database(bad, missing_exclusions=True)
    before = manifest.read_database_snapshot(live)
    assert invoke(root, "candidate", str(bad), TRANSACTION).returncode != 0
    relative = f".deployments/component-full-candidates/{TRANSACTION}.db"
    fake_path = root / relative
    fake_path.write_bytes(bad.read_bytes())
    fake_path.chmod(0o400)
    binding, db_stat = manifest.read_database_snapshot(fake_path)
    fake = {"path": relative, "binding": binding, "stat": db_stat,
            "verification": {"profile": "arikatsu", "exact_hit_sample_size": 500,
                             "require_free_text_exclusions": True, "require_free_text_zh_exclusions": True,
                             "verify_db_exit": 0, "exact_hits_exit": 0, "seed_terms_exit": 0}}
    result = invoke(root, "verify", fake, TRANSACTION)
    assert result.returncode != 0
    assert "required verifiers" in result.stderr
    assert manifest.read_database_snapshot(live) == before


def test_unclaimed_final_and_changed_live_are_never_overwritten(tmp_path: Path) -> None:
    root, live, candidate = setup_root(tmp_path)
    directory = root / ".deployments/component-full-candidates"
    directory.mkdir()
    final = directory / (TRANSACTION + ".db")
    final.write_bytes(b"unknown-owner")
    before = hashlib.sha256(final.read_bytes()).hexdigest()
    result = invoke(root, "candidate", str(candidate), TRANSACTION)
    assert result.returncode != 0
    assert "no owned claim" in result.stderr
    assert hashlib.sha256(final.read_bytes()).hexdigest() == before
    binding, db_stat = manifest.read_database_snapshot(live)
    os.utime(live, ns=(live.stat().st_atime_ns, live.stat().st_mtime_ns + 1_000_000))
    assert invoke(root, "backup", binding, db_stat, TRANSACTION).returncode != 0
    assert not (root / "data/deployment-backups").exists()


def test_live_source_drift_during_copy_refuses_publication(tmp_path: Path) -> None:
    root, live, candidate = setup_root(tmp_path)
    before = manifest.read_database_snapshot(live)
    code = """
import json,os,pathlib,sys
sys.path.insert(0,sys.argv[1])
import component_full_db as m
root, source = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
inode = source.stat().st_ino
original = os.read
observed = {'scans': 0, 'changed': False}
def read(fd, size):
    data = original(fd, size)
    if os.fstat(fd).st_ino == inode:
        if not data:
            observed['scans'] += 1
        elif observed['scans'] >= 2 and not observed['changed']:
            value = source.stat()
            os.utime(source, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000000))
            observed['changed'] = True
    return data
os.read = read
try:
    m.prepare_candidate(root, source, sys.argv[4])
except m.FullDatabaseError:
    print(json.dumps(observed))
else:
    raise AssertionError('source drift was accepted')
"""
    command = ["sh", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
               "fixture-lock", str(root / ".deployments/.deployment.lock"), PYTHON, "-I", "-B", "-c", code,
               str(SOURCE / "scripts"), str(root), str(candidate), TRANSACTION]
    result = subprocess.run(command, cwd=SOURCE, env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                            "LANG": "C.UTF-8"}, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changed"] is True
    assert not (root / ".deployments/component-full-candidates" / (TRANSACTION + ".db")).exists()
    assert manifest.read_database_snapshot(live) == before


@pytest.mark.parametrize("kind", ["candidate", "backup", "stage"])
def test_real_publication_before_process_death_is_reconciled_on_retry(tmp_path: Path, kind: str) -> None:
    root, live, input_path = setup_root(tmp_path)
    old_binding, old_stat = manifest.read_database_snapshot(live)
    candidate = verify_result(invoke(root, "candidate", str(input_path), TRANSACTION)) if kind == "stage" else None
    if kind == "candidate":
        values = [str(input_path), TRANSACTION]
        final = root / ".deployments/component-full-candidates" / (TRANSACTION + ".db")
    elif kind == "backup":
        values = [old_binding, old_stat, TRANSACTION]
        final = root / "data/deployment-backups" / (
            f"terms.{TRANSACTION[:16]}-{int(TRANSACTION[17:], 16)}.{old_binding['sha256']}.db")
    else:
        values = [candidate, str(live), TRANSACTION]
        final = live.parent / (".component-full-promote-" + TRANSACTION + ".db")
    code = """
import json,os,pathlib,sys
sys.path.insert(0,sys.argv[1])
import component_full_db as m
root, kind, values = pathlib.Path(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4])
original = m._publish
def publish_then_die(directory, temporary, destination):
    original(directory, temporary, destination)
    os._exit(75)
m._publish = publish_then_die
if kind == 'candidate':
    m.prepare_candidate(root, pathlib.Path(values[0]), values[1])
elif kind == 'backup':
    m.prepare_backup(root, values[0], values[1], values[2])
else:
    m.prepare_stage(root, values[0], pathlib.Path(values[1]), values[2])
raise AssertionError('publication boundary was not reached')
"""
    command = ["sh", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
               "fixture-lock", str(root / ".deployments/.deployment.lock"), PYTHON, "-I", "-B", "-c", code,
               str(SOURCE / "scripts"), str(root), kind, json.dumps(values)]
    killed = subprocess.run(command, cwd=SOURCE, env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                            "LANG": "C.UTF-8"}, capture_output=True, text=True, timeout=120, check=False)
    assert killed.returncode == 75, killed.stderr
    assert final.is_file()
    published_inode = final.stat().st_ino
    retried = verify_result(invoke(root, kind, *values))
    assert final.stat().st_ino == published_inode
    assert retried["stat"]["inode"] == published_inode
    assert manifest.read_database_snapshot(live) == (old_binding, old_stat)


def test_verification_scratch_cleanup_refuses_unknown_contents(tmp_path: Path) -> None:
    root, _, _ = setup_root(tmp_path)
    scratch, identity = full_db._verification_scratch(root, TRANSACTION)
    unknown = scratch / "unknown"
    unknown.write_bytes(b"do not remove")
    with pytest.raises(full_db.FullDatabaseError, match="owned empty"):
        full_db._remove_empty_verification_scratch(root, scratch, identity)
    assert unknown.read_bytes() == b"do not remove" and scratch.stat().st_ino == identity[1]


def test_backup_rename_before_directory_sync_requires_successful_resync(tmp_path: Path) -> None:
    root, live, _ = setup_root(tmp_path)
    binding, db_stat = manifest.read_database_snapshot(live)
    directory = root / "data/deployment-backups"
    name = f"terms.{TRANSACTION[:16]}-{int(TRANSACTION[17:], 16)}.{binding['sha256']}.db"
    final = directory / name
    code = r'''
import errno,json,os,pathlib,signal,sys
sys.path.insert(0,sys.argv[1]); import component_full_db as m
root=pathlib.Path(sys.argv[2]); values=json.loads(sys.argv[3]); directory=sys.argv[4]; final=pathlib.Path(sys.argv[5]); mode=sys.argv[6]
real=os.fsync
def injected(fd):
 try: path=os.readlink('/proc/self/fd/'+str(fd))
 except OSError: path=''
 if path==directory and final.exists():
  if mode=='stop': os.kill(os.getpid(),signal.SIGSTOP)
  if mode=='eio': raise OSError(errno.EIO,'synthetic directory sync failure')
 return real(fd)
os.fsync=injected
try: m.prepare_backup(root,values[0],values[1],values[2])
except m.FullDatabaseError: sys.exit(47)
'''

    def command(mode: str) -> list[str]:
        return ["sh", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
                "fixture-lock", str(root / ".deployments/.deployment.lock"), PYTHON, "-I", "-B", "-c", code,
                str(SOURCE / "scripts"), str(root), json.dumps([binding, db_stat, TRANSACTION]),
                str(directory), str(final), mode]

    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    child = subprocess.Popen(command("stop"), cwd=SOURCE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                assert os.WIFSTOPPED(status), "backup exited before its unsynced publication boundary"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("backup did not reach its unsynced publication boundary")
        assert final.is_file()
        before = final.stat()
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)
    rejected = subprocess.run(command("eio"), cwd=SOURCE, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30, check=False)
    assert rejected.returncode == 47
    assert final.stat().st_ino == before.st_ino and final.read_bytes() == live.read_bytes()
    result = verify_result(invoke(root, "backup", binding, db_stat, TRANSACTION))
    assert result["stat"]["inode"] == before.st_ino and result["sha256"] == binding["sha256"]


def test_real_verifier_inherits_fd9_after_parent_death(tmp_path: Path) -> None:
    root, live, candidate = setup_root(tmp_path)
    code = ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import component_full_db as m; "
            "m.prepare_candidate(pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]),sys.argv[4])")
    lock_path = root / ".deployments/.deployment.lock"
    command = ["sh", "-c", 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"',
               "fixture-lock", str(lock_path), PYTHON, "-I", "-B", "-c", code,
               str(SOURCE / "scripts"), str(root), str(candidate), TRANSACTION]
    process = subprocess.Popen(command, cwd=SOURCE, env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                               "LANG": "C.UTF-8"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    verifier = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
            except FileNotFoundError:
                break
            for child in children:
                try:
                    arguments = Path(f"/proc/{child}/cmdline").read_bytes()
                    if b"verify_exact_hits.py" not in arguments:
                        continue
                    target = os.readlink(f"/proc/{child}/fd/9")
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if target == str(lock_path):
                    try:
                        os.kill(int(child), signal.SIGSTOP)
                    except ProcessLookupError:
                        continue
                    verifier = int(child)
                    break
            if verifier is not None:
                break
            time.sleep(0.002)
        assert verifier is not None, "actual exact-hit verifier was not observed"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status = Path(f"/proc/{verifier}/status").read_text()
            except FileNotFoundError:
                break
            if "State:\tT" in status:
                break
            time.sleep(0.002)
        else:
            raise AssertionError("the selected verifier did not stop")
        assert os.readlink(f"/proc/{verifier}/fd/9") == str(lock_path)
        assert os.stat(f"/proc/{verifier}/fd/9").st_ino == lock_path.stat().st_ino
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
        with lock_path.open("r+") as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.kill(verifier, signal.SIGCONT)
            verifier = None
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("verifier did not release inherited lock") from None
                    time.sleep(0.01)
    finally:
        if verifier is not None:
            try:
                os.kill(verifier, signal.SIGCONT)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert verify_result(invoke(root, "candidate", str(candidate), TRANSACTION))["verification"]["exact_hits_exit"] == 0
    assert live.is_file()


@pytest.mark.parametrize("kind", ["backup", "stage"])
def test_unclaimed_backup_or_stage_is_not_overwritten(tmp_path: Path, kind: str) -> None:
    root, live, input_path = setup_root(tmp_path)
    old_binding, old_stat = manifest.read_database_snapshot(live)
    old_bytes = live.read_bytes()
    if kind == "backup":
        directory = root / "data/deployment-backups"
        directory.mkdir()
        number = int(TRANSACTION[17:], 16)
        target = directory / f"terms.{TRANSACTION[:16]}-{number}.{old_binding['sha256']}.db"
        target.write_bytes(b"unknown-backup")
        result = invoke(root, "backup", old_binding, old_stat, TRANSACTION)
    else:
        candidate = verify_result(invoke(root, "candidate", str(input_path), TRANSACTION))
        target = live.parent / (".component-full-promote-" + TRANSACTION + ".db")
        target.write_bytes(b"unknown-stage")
        result = invoke(root, "stage", candidate, str(live), TRANSACTION)
    assert result.returncode != 0
    assert "no owned claim" in result.stderr
    assert target.read_bytes() == (b"unknown-backup" if kind == "backup" else b"unknown-stage")
    assert live.read_bytes() == old_bytes
    assert manifest.read_database_snapshot(live) == (old_binding, old_stat)


def test_permissions_refuse_symlink_and_match_live(tmp_path: Path) -> None:
    root, live, _ = setup_root(tmp_path)
    assert verify_result(invoke(root, "permissions", str(live))) == {
        "mode": 0o600, "uid": os.geteuid(), "gid": os.getegid(),
    }
    link = tmp_path / "live-link"
    link.symlink_to(live)
    assert invoke(root, "permissions", str(link)).returncode != 0
