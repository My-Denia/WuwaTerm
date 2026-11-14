"""Opt-in, no-network FULL3 CLI acceptance using pre-registered real builds.

Set WUWATERM_FULL3_DOCKER_TEST=1 and WUWATERM_FULL3_DOCKER_CASES to an
absolute JSON file beneath goal-runs/full3-implementation. Its object has
``source_commit``, ``source_tree``, ``compose_path``, ``compose_sha256`` and
``cases``. Each case is a distinct, clean, task-owned Git root beneath that
same directory with two genuine already-registered API receipts, named
``api_old_receipt`` and ``api_new_receipt``. The interruption cases also need
``source_b_commit``, an already-present local commit distinct from source A.
The registered build tags (``image_ref`` of ``api_old_receipt`` and
``api_new_receipt``) are pruned by the producer's retention (``BUILD_IMAGE_KEEP``
newest per family in ``scripts/component_build_isolation.py``; images used by any
container are never pruned), so register every receipt a run needs within one
retention window and keep the old receipt's image in use before the next build.

Case keys: normal-running, normal-stopped, crash-promotion,
handoff-runtime-to-full, handoff-full-to-runtime. These tests never build,
fetch, send Telegram messages, remove images or clean fixture files. When a
case ends, pass or fail, ``Case.finish()`` stops and deletes the containers
that still carry that case's own owner label, and nothing else. The per-case
ledger, which records the removed IDs, is retained.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
from typing import Any

import pytest

from scripts import product_manifest as manifest
from test_component_full_db import make_verified_database
from wuwaterm_api.auth import DeviceStore


SOURCE = Path(__file__).resolve().parents[1]
AREA = SOURCE / "goal-runs/full3-implementation"
ENABLED = os.environ.get("WUWATERM_FULL3_DOCKER_TEST") == "1"
pytestmark = pytest.mark.skipif(not ENABLED, reason="explicit FULL3 Docker acceptance was not requested")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")
_USED_ROOTS: set[Path] = set()
_OWNER_LABEL = "org.wuwaterm.test-owner"
_RUNTIME = ".component-runtime-transaction.json"
_FULL = ".component-full-transaction.json"
_OWNER_FORMAT = '{{.Id}}|{{index .Config.Labels "' + _OWNER_LABEL + '"}}|{{.State.Running}}'


def _physical(value: Any, *, directory: bool) -> Path:
    assert type(value) is str and value.startswith("/")
    path = Path(value)
    assert path.resolve(strict=True) == path
    assert path.is_dir() if directory else path.is_file()
    return path


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config() -> dict[str, Any]:
    path = _physical(os.environ["WUWATERM_FULL3_DOCKER_CASES"], directory=False)
    assert path.is_relative_to(AREA.resolve(strict=True))
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict and set(value) == {
        "source_commit", "source_tree", "compose_path", "compose_sha256", "cases",
    }
    assert _HEX40.fullmatch(value["source_commit"]) and _HEX40.fullmatch(value["source_tree"])
    assert _HEX64.fullmatch(value["compose_sha256"])
    compose = _physical(value["compose_path"], directory=False)
    assert os.access(compose, os.X_OK) and _hash(compose) == value["compose_sha256"]
    checksum = Path(str(compose) + ".sha256")
    assert checksum.resolve(strict=True) == checksum
    fields = checksum.read_text(encoding="ascii").strip().split()
    assert len(fields) == 2 and fields[0] == value["compose_sha256"] and fields[1].lstrip("*") == compose.name
    assert type(value["cases"]) is dict
    return value


def _git_env() -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1"}


def _git(root: Path, *arguments: str) -> str:
    command = ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *arguments]
    result = subprocess.run(command, cwd=AREA, env=_git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, f"fixture git command failed: {arguments[0]}"
    return result.stdout.strip()


def _contains(path: Path, needle: bytes) -> bool:
    carry = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            data = carry + chunk
            if needle in data:
                return True
            carry = data[-max(len(needle) - 1, 0):]
    return False


def _docker_cli(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "--host", "unix:///run/docker.sock", *arguments], env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False)


def _remove_owned_containers(owner: str, environment: dict[str, str]) -> list[str]:
    """Stop and delete every container whose exact ID still carries this owner label.

    Each ID is inspected and its label re-verified before it is touched; nothing
    is selected by name, and no image, volume or network is removed.
    """
    def owned() -> list[str]:
        listing = _docker_cli(environment, "ps", "-aq", "--no-trunc", "--filter", f"label={_OWNER_LABEL}={owner}")
        assert listing.returncode == 0, "cleanup: owned-container listing failed"
        return listing.stdout.split()

    removed = []
    for identifier in owned():
        inspected = _docker_cli(environment, "inspect", "--format", _OWNER_FORMAT, identifier)
        if inspected.returncode:
            continue  # Transaction recovery may already have removed it.
        found, label, running = inspected.stdout.strip().split("|")
        if (found, label) != (identifier, owner):
            continue
        if running == "true":
            _docker_cli(environment, "stop", identifier)
        if _docker_cli(environment, "rm", identifier).returncode == 0:
            removed.append(identifier)
    assert not owned(), "cleanup: owned containers remain"
    return removed


class Case:
    """Own one isolated project; ``finish()`` removes its owner-labelled containers."""

    def __init__(self, name: str):
        config = _config()
        assert name in config["cases"]
        entry = config["cases"][name]
        assert type(entry) is dict and set(entry) in (
            {"root", "api_old_receipt", "api_new_receipt"},
            {"root", "api_old_receipt", "api_new_receipt", "source_b_commit"},
        )
        self.root = _physical(entry["root"], directory=True)
        assert self.root.is_relative_to(AREA.resolve(strict=True)) and self.root != SOURCE
        assert self.root not in _USED_ROOTS
        _USED_ROOTS.add(self.root)
        self.head, self.tree = config["source_commit"], config["source_tree"]
        self.compose = Path(config["compose_path"])
        self.source_b = entry.get("source_b_commit")
        if self.source_b is not None:
            assert _HEX40.fullmatch(self.source_b) and self.source_b != self.head
        for root in (SOURCE, self.root):
            assert _git(root, "rev-parse", "--show-toplevel") == str(root)
            assert _git(root, "rev-parse", "--verify", "HEAD^{commit}") == self.head
            assert _git(root, "rev-parse", "--verify", "HEAD^{tree}") == self.tree
            assert not _git(root, "status", "--porcelain=v1", "--untracked-files=no")
        filter_check = subprocess.run(
            ["git", "-C", str(self.root), "config", "--includes", "--get-regexp",
             r"^(filter\.|include\.|includeif\.|core\.attributesfile)"],
            cwd=AREA, env=_git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
        assert filter_check.returncode == 1 and not filter_check.stdout
        assert _git(SOURCE, "ls-files", "--error-unmatch", "tests/test_component_full_docker.py")
        if self.source_b is not None:
            assert _git(self.root, "rev-parse", "--verify", self.source_b + "^{commit}") == self.source_b
        for name_to_check in (".deploy_commit", ".deploy_current", ".deployments/" + _RUNTIME,
                              ".deployments/" + _FULL, ".deployments/.full-transaction.json",
                              ".deployments/.runtime-transaction.json"):
            path = self.root / name_to_check
            assert not path.exists() and not path.is_symlink()
        self.old_digest = entry["api_old_receipt"]
        self.new_digest = entry["api_new_receipt"]
        assert _HEX64.fullmatch(self.old_digest) and _HEX64.fullmatch(self.new_digest)
        assert self.old_digest != self.new_digest
        self.owner = "full3case" + secrets.token_hex(8)
        self.directory = self.root / "goal-runs/full3-acceptance" / self.owner
        self.directory.mkdir(parents=True, mode=0o700)
        self.ledger = self.directory / "ledger.jsonl"
        fd = os.open(self.ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.canary = "synthetic-" + secrets.token_hex(24)
        self.environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                            "LC_ALL": "C", "DOCKER_CONFIG": str(self.directory / "docker-config")}
        (self.directory / "docker-config").mkdir(mode=0o700)
        self.event("fixture", {"case": name, "root": str(self.root), "owner_label": _OWNER_LABEL,
                               "owner": self.owner, "source_commit": self.head, "source_tree": self.tree,
                               "compose_sha256": config["compose_sha256"], "network": "none",
                               "env_allowlist": self.environment})
        self.old_receipt = self.receipt(self.old_digest)
        self.new_receipt = self.receipt(self.new_digest)
        self.env_file = self.directory / "fixture.env"
        self.env_file.write_text("FULL3_SYNTHETIC_SECRET=" + self.canary + "\n", encoding="ascii")
        self.env_file.chmod(0o600)
        self.data = self.directory / "data"
        self.state = self.directory / "state-api"
        self.data.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.live = self.data / "terms.db"
        self.candidate_a = self.directory / "candidate-a.db"
        self.candidate_b = self.directory / "candidate-b.db"
        make_verified_database(self.live, variant="a")
        self.live.chmod(0o600)
        self.candidate_a.write_bytes(self.live.read_bytes())
        make_verified_database(self.candidate_b, variant="b")
        self.db_a, self.db_b = _hash(self.live), _hash(self.candidate_b)
        assert self.db_a != self.db_b
        self.identity = self.state / "devices.db"
        DeviceStore(self.identity, guard_legacy_default=False).initialize()
        self.identity_inode = self.identity.stat().st_ino
        self.env_mode = stat.S_IMODE(self.env_file.stat().st_mode)
        self.compose_file = self.directory / "compose.json"
        self.compose_file.write_text(json.dumps({"services": {"wuwaterm-api": {
            "image": self.old_receipt["image_ref"], "container_name": self.owner + "-api",
            "network_mode": "none", "restart": "no", "labels": {_OWNER_LABEL: self.owner},
            "env_file": [str(self.env_file)],
            "environment": {"WUWATERM_DB_PATH": "/app/data/terms.db", "WUWATERM_API_STATE_DIR": "/app/state-api"},
            "volumes": [str(self.data) + ":/app/data:ro", str(self.state) + ":/app/state-api"], "command": ["api"],
        }}}), encoding="utf-8")
        self.api_id: str | None = None
        self.event("fixture-bindings", {"database_path": str(self.live), "database_a_sha256": self.db_a,
                                       "database_b_sha256": self.db_b, "identity_path": str(self.identity),
                                       "identity_inode": self.identity_inode, "env_path": str(self.env_file),
                                       "env_mode": self.env_mode, "compose_file": str(self.compose_file)})

    def event(self, kind: str, data: dict[str, Any]) -> None:
        raw = (json.dumps({"kind": kind, **data}, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        fd = os.open(self.ledger, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                assert written > 0
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def run(self, label: str, command: list[str], *, success: bool = True, timeout: int = 300,
            allow_canary: bool = False) -> str:
        try:
            result = subprocess.run(command, cwd=self.directory, env=self.environment, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            self.event("command-timeout", {"label": label, "argv": command, "timeout_seconds": timeout})
            raise AssertionError(f"{label}: command timed out") from None
        contains_canary = self.canary.encode() in result.stdout or self.canary.encode() in result.stderr
        self.event("command", {"label": label, "argv": command, "exit": result.returncode,
                               "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                               "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                               "contains_canary": contains_canary})
        assert allow_canary or not contains_canary, f"{label}: secret canary appeared in output"
        assert (result.returncode == 0) == success, f"{label}: unexpected exit {result.returncode}"
        return result.stdout.decode("utf-8").strip()

    def receipt(self, digest: str) -> dict[str, Any]:
        path = self.root / ".deployments/product-builds" / (digest + ".json")
        assert path.resolve(strict=True) == path and _hash(path) == digest
        value = json.loads(path.read_text(encoding="utf-8"))
        source = value["recovery_source"]
        assert source["commit"] == self.head and source["tree"] == self.tree
        self.run("verify-registered-receipt", [sys.executable, "-I", "-B",
                 str(self.root / "scripts/component_build.py"), "--root", str(self.root),
                 "verify-receipt", "--sha256", digest])
        image = json.loads(self.run("inspect-receipt-image", ["docker", "--host", "unix:///run/docker.sock",
                           "image", "inspect", value["image_ref"]]))[0]
        assert image["Id"] == value["image_id"]
        assert image["Config"]["Labels"]["org.opencontainers.image.revision"] == value["image_revision"]
        self.event("receipt", {"sha256": digest, "image_id": value["image_id"], "image_ref": value["image_ref"]})
        return value

    def docker(self, label: str, *arguments: str, success: bool = True) -> str:
        return self.run(label, ["docker", "--host", "unix:///run/docker.sock", *arguments], success=success,
                        allow_canary=label == "inspect-owned")

    def cli(self, *arguments: str, success: bool = True) -> str:
        return self.run("product-cli", [str(self.root / "deploy/product-update.sh"), "--root", str(self.root),
                                         *arguments], success=success)

    def journal(self, kind: str) -> tuple[bytes, dict[str, Any]]:
        path = self.root / ".deployments" / (_FULL if kind == "full" else _RUNTIME)
        raw = path.read_bytes()
        record = json.loads(raw)
        assert _ID.fullmatch(record["transaction_id"])
        return raw, record

    def inventory(self) -> dict[str, dict[str, Any]]:
        ids = self.docker("owned-inventory", "ps", "-aq", "--no-trunc", "--filter",
                          "label=" + _OWNER_LABEL + "=" + self.owner).splitlines()
        result = {}
        for identifier in ids:
            raw = json.loads(self.docker("inspect-owned", "inspect", identifier))[0]
            assert raw["Id"] == identifier and raw["Config"]["Labels"][_OWNER_LABEL] == self.owner
            assert raw["HostConfig"]["NetworkMode"] == "none"
            result[identifier] = {"image_id": raw["Image"], "name": raw["Name"],
                                  "running": raw["State"]["Running"], "started_at": raw["State"]["StartedAt"],
                                  "restart_count": raw["RestartCount"], "owner": self.owner,
                                  "service": raw["Config"]["Labels"].get("com.docker.compose.service"),
                                  "transaction": raw["Config"]["Labels"].get("org.wuwaterm.component-transaction")}
        self.event("inventory", {"containers": result})
        return result

    def provision(self, *, running: bool) -> dict[str, Any]:
        self.event("compose-create-intent", {"project": self.owner, "service": "wuwaterm-api",
                                             "container_name": self.owner + "-api"})
        self.run("compose-create", [str(self.compose), "--project-name", self.owner,
                 "--project-directory", str(self.root), "--file", str(self.compose_file),
                 "create", "--no-build", "--pull", "never"])
        inventory = self.inventory()
        assert len(inventory) == 1
        self.api_id = next(iter(inventory))
        assert inventory[self.api_id]["image_id"] == self.old_receipt["image_id"]
        assert not inventory[self.api_id]["running"]
        if running:
            self.docker("start-owned-api", "start", self.api_id)
            self.ready(self.api_id)
        before = self.inventory()
        assert before[self.api_id]["running"] is running
        response = json.loads(self.cli("adopt", "--project", self.owner, "--compose-executable", str(self.compose),
                                       "--api-receipt", self.old_digest, "--expected-sha", self.head))
        assert _ID.fullmatch(response["deployment_id"])
        _, journal = self.journal("runtime")
        assert journal["status"] == "committed" and journal["transaction_id"] == response["deployment_id"]
        assert journal["target"]["manifest"]["database"]["sha256"] == self.db_a
        assert journal["target"]["manifest"]["components"]["telegram"] is None
        assert self.inventory() == before
        self.check_common(self.db_a)
        return before[self.api_id]

    def ready(self, identifier: str) -> None:
        code = ("import urllib.request; "
                "assert urllib.request.urlopen('http://127.0.0.1:8788/readyz',timeout=2).status==200")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "--host", "unix:///run/docker.sock", "exec", identifier,
                                     "python", "-I", "-B", "-c", code], cwd=self.directory,
                                    env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=5, check=False)
            if result.returncode == 0:
                self.event("readiness", {"container_id": identifier, "exit": result.returncode,
                                         "address": "container-loopback"})
                return
            time.sleep(0.25)
        self.event("readiness", {"container_id": identifier, "status": "timeout"})
        raise AssertionError("owned API did not become ready")

    def reader_value(self, identifier: str) -> str:
        code = ("import sqlite3; c=sqlite3.connect('file:/app/data/terms.db?mode=ro',uri=True); "
                "print(c.execute('select en from terms where text_key=?',('key-9',)).fetchone()[0])")
        return self.docker("query-reader-db", "exec", identifier, "python", "-I", "-B", "-c", code)

    def check_common(self, db_sha256: str) -> None:
        binding, _ = manifest.read_database_snapshot(self.live)
        assert binding["sha256"] == _hash(self.live) == db_sha256
        candidate, _ = manifest.read_database_snapshot(self.candidate_a)
        assert binding["provenance"] == candidate["provenance"]
        assert self.identity.stat().st_ino == self.identity_inode
        assert stat.S_IMODE(self.env_file.stat().st_mode) == self.env_mode == 0o600
        assert self.env_file.read_text(encoding="ascii").endswith(self.canary + "\n")

    def immutable_snapshot(self) -> dict[str, Any]:
        pointers = {}
        for name in (".deploy_commit", ".deploy_current"):
            path = self.root / name
            pointers[name] = path.read_bytes().hex() if path.exists() else None
        journals = {}
        for name in (_RUNTIME, _FULL):
            path = self.root / ".deployments" / name
            journals[name] = _hash(path) if path.exists() else None
        return {"containers": self.inventory(), "database": _hash(self.live),
                "identity_inode": self.identity.stat().st_ino, "identity_sha256": _hash(self.identity),
                "env_sha256": _hash(self.env_file), "env_mode": stat.S_IMODE(self.env_file.stat().st_mode),
                "pointers": pointers, "journals": journals}

    def check_canary(self) -> None:
        metadata = self.root / ".deployments"
        for directory, _, names in os.walk(metadata, followlinks=False):
            for name in names:
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                assert not _contains(path, self.canary.encode()), "secret canary appeared in deployment metadata"

    def inspect_launcher(self, kind: str, identifier: str) -> list[str]:
        inspected = json.loads(self.cli("inspect"))
        item = inspected["full_recovery" if kind == "full" else "recovery"]
        assert item["status"] == "recorded-unverified"
        argv = item["argv"]
        assert argv[argv.index("--id") + 1] == identifier
        assert argv[argv.index("--kind") + 1] == kind
        return argv

    def poison_b(self) -> tuple[str, Path]:
        assert self.source_b is not None
        self.event("source-b-switch-intent", {"source_b_commit": self.source_b})
        _git(self.root, "switch", "--detach", self.source_b)
        assert _git(self.root, "rev-parse", "HEAD") == self.source_b
        marker = self.directory / "source-b-imported"
        target = self.root / "scripts/component_runtime.py"
        assert target.is_file() and not target.is_symlink()
        trap = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('source B imported')\n"
        target.write_text(trap, encoding="utf-8")
        self.event("source-b-poisoned", {"source_b_commit": self.source_b, "trap_path": str(target)})
        return trap, marker

    def finish(self) -> None:
        try:
            self.inventory()
        except Exception as exc:
            self.event("inventory-error", {"error_type": type(exc).__name__})
        try:
            removed = _remove_owned_containers(self.owner, self.environment)
        except Exception as exc:
            self.event("cleanup-error", {"owner": self.owner, "error_type": type(exc).__name__})
            raise
        self.event("cleaned", {"root": str(self.root), "owner": self.owner, "removed": removed,
                               "retained": "images, fixture files and this ledger"})


class _Registers(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8", "rax", "rcx",
        "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags", "rsp", "ss", "fs_base",
        "gs_base", "ds", "es", "fs", "gs")]


def _interrupt_after_syscall(case: Case, arguments: list[str], *, operation: str, destination: str,
                             expected_db: str | None = None) -> dict[str, Any]:
    """Kill the real CLI at a kernel publication or predecessor-removal boundary."""
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    libc.ptrace.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def trace_child() -> None:
        if libc.ptrace(0, 0, None, None) != 0:
            os._exit(97)

    command = [str(case.root / "deploy/product-update.sh"), "--root", str(case.root), *arguments]
    late_attach = operation == "rename" and expected_db is not None
    child = subprocess.Popen(command, cwd=case.directory, env=case.environment, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, preexec_fn=None if late_attach else trace_child)

    def ptrace(request: int, address: int = 0, data: int = 0) -> int:
        ctypes.set_errno(0)
        result = libc.ptrace(request, child.pid, ctypes.c_void_p(address), ctypes.c_void_p(data))
        if result == -1 and ctypes.get_errno():
            raise OSError(ctypes.get_errno(), "fixture ptrace failed")
        return result

    def read_string(address: int) -> bytes:
        value = bytearray()
        for offset in range(0, 4096, 8):
            word = ptrace(2, address + offset) & ((1 << 64) - 1)
            value.extend(word.to_bytes(8, "little"))
            if b"\0" in value:
                return bytes(value).split(b"\0", 1)[0]
        raise AssertionError("traced filename was not terminated")

    observed = False
    pending = False
    interrupted: dict[str, Any] = {}
    try:
        if late_attach:
            journal = case.root / ".deployments" / _FULL
            ready = False
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    break
                try:
                    phase = json.loads(journal.read_text(encoding="utf-8"))["phase"]
                except (OSError, ValueError, KeyError):
                    phase = None
                if phase == "state-migrate":
                    ptrace(16)  # PTRACE_ATTACH while the CLI owns FD9.
                    ready = True
                    break
                if phase in ("stage-db", "promote-db", "activate-readers", "committed"):
                    break
                time.sleep(0.01)
            assert ready, "fixture CLI passed the safe attach phase"
        _, initial = os.waitpid(child.pid, 0)
        assert os.WIFSTOPPED(initial)
        ptrace(0x4200, data=1)
        ptrace(24)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child.pid, os.WUNTRACED)
            assert waited == child.pid
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                child.returncode = os.waitstatus_to_exitcode(status)
                break
            assert os.WIFSTOPPED(status)
            number = os.WSTOPSIG(status)
            if number == signal.SIGTRAP | 0x80:
                registers = _Registers()
                ptrace(12, data=ctypes.addressof(registers))
                result = ctypes.c_longlong(registers.rax).value
                syscall = registers.orig_rax
                if operation == "before-unlink" and syscall in (87, 263) and result == -38:
                    target = read_string(registers.rdi if syscall == 87 else registers.rsi)
                    if target in (os.fsencode(destination), os.fsencode(Path(destination).name)):
                        observed = True
                        interrupted = {"operation": operation, "destination": destination, "syscall": syscall}
                        os.kill(child.pid, signal.SIGKILL)
                        _, final = os.waitpid(child.pid, 0)
                        child.returncode = os.waitstatus_to_exitcode(final)
                        break
                supported = (operation == "link" and syscall == 265) or (
                    operation == "rename" and syscall in (82, 264, 316))
                if supported and result == -38:
                    pointer = registers.rsi if syscall == 82 else registers.r10
                    target = read_string(pointer)
                    pending = target in (os.fsencode(destination), os.fsencode(Path(destination).name))
                elif supported and pending:
                    if result == 0:
                        observed = True
                        if expected_db is not None:
                            assert _hash(case.live) == expected_db
                            inventory = case.inventory()
                            assert inventory and all(not item["running"] for item in inventory.values())
                        interrupted = {"operation": operation, "destination": destination, "syscall": syscall}
                        os.kill(child.pid, signal.SIGKILL)
                        _, final = os.waitpid(child.pid, 0)
                        child.returncode = os.waitstatus_to_exitcode(final)
                        break
                    pending = False
                ptrace(24)
            else:
                ptrace(24, data=0 if number == signal.SIGTRAP else number)
        if not observed and child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            _, final = os.waitpid(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(final)
    except BaseException:
        if child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            _, final = os.waitpid(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(final)
        raise
    stdout, stderr = child.communicate(timeout=30)
    contains_canary = case.canary.encode() in stdout or case.canary.encode() in stderr
    case.event("interrupted-command", {"argv": command, "exit": child.returncode,
               "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
               "contains_canary": contains_canary, "boundary": interrupted})
    assert not contains_canary
    assert observed and child.returncode == -signal.SIGKILL
    return interrupted


def _assert_archive(case: Case, record: dict[str, Any], predecessor_raw: bytes, kind: str) -> None:
    predecessor = record["predecessor"]
    assert predecessor["kind"] == kind
    assert predecessor["journal_sha256"] == hashlib.sha256(predecessor_raw).hexdigest()
    archive = case.root / predecessor["archive_path"]
    assert archive.read_bytes() == predecessor_raw
    assert stat.S_IMODE(archive.stat().st_mode) & 0o222 == 0


def _assert_pointers(case: Case, record: dict[str, Any], which: str) -> None:
    for name, expected in record["pointers"][which].items():
        path = case.root / name
        if expected["present"]:
            assert path.read_bytes() == expected["bytes"].encode("ascii")
        else:
            assert not path.exists() and not path.is_symlink()


@pytest.mark.parametrize("running", [True, False], ids=["running", "stopped"])
def test_normal_cli_adopt_full_runtime_full(running: bool) -> None:
    case = Case("normal-running" if running else "normal-stopped")
    try:
        original = case.provision(running=running)
        old_id = case.api_id
        assert old_id is not None
        if running:
            assert case.reader_value(old_id) == "Synthetic Term 0000 a"
        prior_raw, prior = case.journal("runtime")
        first = json.loads(case.cli("full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                                    "--compose-executable", str(case.compose)))
        assert first["status"] == "committed"
        full_raw, full = case.journal("full")
        assert full["status"] == "committed" and full["selection"] == []
        _assert_archive(case, full, prior_raw, "runtime")
        _assert_pointers(case, full, "target")
        assert not (case.root / ".deployments" / _RUNTIME).exists()
        after_full = case.inventory()
        assert set(after_full) == {old_id}
        assert after_full[old_id]["running"] is running
        assert after_full[old_id]["image_id"] == original["image_id"]
        assert after_full[old_id]["restart_count"] == original["restart_count"]
        assert (after_full[old_id]["started_at"] != original["started_at"]) is running
        case.check_common(case.db_b)
        if running:
            case.ready(old_id)
            assert case.reader_value(old_id) == "Synthetic Term 0000 b"
        updated = json.loads(case.cli("runtime-update", "--components", "api", "--api-receipt", case.new_digest,
                                      "--compose-executable", str(case.compose)))
        assert updated["status"] == "committed"
        runtime_raw, runtime = case.journal("runtime")
        assert runtime["schema_version"] == 4 and runtime["status"] == "committed"
        _assert_archive(case, runtime, full_raw, "full")
        _assert_pointers(case, runtime, "target")
        assert not (case.root / ".deployments" / _FULL).exists()
        after_runtime = case.inventory()
        assert len(after_runtime) == 1
        new_id = next(iter(after_runtime))
        assert new_id != old_id and after_runtime[new_id]["image_id"] == case.new_receipt["image_id"]
        assert after_runtime[new_id]["running"] is running
        case.check_common(case.db_b)
        if running:
            case.ready(new_id)
            assert case.reader_value(new_id) == "Synthetic Term 0000 b"
        final = json.loads(case.cli("full-update", "--candidate-db", str(case.candidate_a), "--components", "none",
                                    "--compose-executable", str(case.compose)))
        assert final["status"] == "committed"
        last_raw, last = case.journal("full")
        assert last["status"] == "committed" and last["selection"] == []
        _assert_archive(case, last, runtime_raw, "runtime")
        _assert_pointers(case, last, "target")
        after_last = case.inventory()
        assert set(after_last) == {new_id} and after_last[new_id]["running"] is running
        assert after_last[new_id]["image_id"] == after_runtime[new_id]["image_id"]
        assert (after_last[new_id]["started_at"] != after_runtime[new_id]["started_at"]) is running
        case.check_common(case.db_a)
        if running:
            case.ready(new_id)
            assert case.reader_value(new_id) == "Synthetic Term 0000 a"
        selected = json.loads(case.cli("full-update", "--candidate-db", str(case.candidate_b),
                                      "--components", "api", "--api-receipt", case.old_digest,
                                      "--compose-executable", str(case.compose)))
        assert selected["status"] == "committed"
        _, selected_journal = case.journal("full")
        assert selected_journal["selection"] == ["api"] and selected_journal["status"] == "committed"
        _assert_archive(case, selected_journal, last_raw, "full")
        _assert_pointers(case, selected_journal, "target")
        selected_inventory = case.inventory()
        assert len(selected_inventory) == 1
        selected_id = next(iter(selected_inventory))
        assert selected_id != new_id and selected_inventory[selected_id]["image_id"] == case.old_receipt["image_id"]
        assert selected_inventory[selected_id]["running"] is running
        assert selected_journal["target"]["manifest"]["components"]["api"]["provenance"]["build_receipt_sha256"] == case.old_digest
        case.check_common(case.db_b)
        if running:
            case.ready(selected_id)
            assert case.reader_value(selected_id) == "Synthetic Term 0000 b"
        assert prior["target"]["manifest"]["api_identity"]["inode"] == case.identity_inode
        case.check_canary()
        if running:
            invalid = case.directory / "candidate-invalid.db"
            make_verified_database(invalid, variant="invalid", missing_exclusions=True)
            before_refusal = case.immutable_snapshot()
            case.cli("full-update", "--candidate-db", str(invalid), "--components", "none",
                     "--compose-executable", str(case.compose), success=False)
            assert case.immutable_snapshot() == before_refusal
            case.cli("full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                     "--api-receipt", case.old_digest, "--compose-executable", str(case.compose), success=False)
            assert case.immutable_snapshot() == before_refusal
            bad_compose = case.directory / "untrusted-compose"
            bad_marker = case.directory / "untrusted-compose-executed"
            bad_compose.write_text(f"#!/bin/sh\nprintf bad > {shlex.quote(str(bad_marker))}\n", encoding="ascii")
            bad_compose.chmod(0o500)
            case.cli("full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                     "--compose-executable", str(bad_compose), success=False)
            assert not bad_marker.exists() and case.immutable_snapshot() == before_refusal
            env_bytes = case.env_file.read_bytes()
            case.env_file.write_bytes(env_bytes + b"FULL3_DRIFT=changed\n")
            drifted = case.immutable_snapshot()
            case.cli("full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                     "--compose-executable", str(case.compose), success=False)
            assert case.immutable_snapshot() == drifted
            case.env_file.write_bytes(env_bytes)
            original_identity = case.directory / "retained-original-identity.db"
            assert not original_identity.exists()
            identity_bytes = case.identity.read_bytes()
            os.rename(case.identity, original_identity)
            case.identity.write_bytes(identity_bytes)
            case.identity.chmod(0o600)
            assert case.identity.stat().st_ino != case.identity_inode
            case.event("identity-drift-fixture", {"original_path": str(original_identity),
                       "original_inode": case.identity_inode, "replacement_inode": case.identity.stat().st_ino})
            identity_drift = case.immutable_snapshot()
            case.cli("full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                     "--compose-executable", str(case.compose), success=False)
            assert case.immutable_snapshot() == identity_drift
            assert original_identity.stat().st_ino == case.identity_inode
            case.check_canary()
    finally:
        case.finish()


@pytest.mark.parametrize("boundary", ["runtime-to-full", "full-to-runtime"], ids=str)
def test_cross_kind_handoff_recovery_uses_saved_a(boundary: str) -> None:
    case = Case("handoff-" + boundary)
    try:
        before = case.provision(running=True)
        if boundary == "full-to-runtime":
            result = json.loads(case.cli("full-update", "--candidate-db", str(case.candidate_b),
                                         "--components", "none", "--compose-executable", str(case.compose)))
            assert result["status"] == "committed"
            prior_raw, prior = case.journal("full")
            predecessor_kind, successor_kind = "full", "runtime"
            arguments = ["runtime-update", "--components", "api", "--api-receipt", case.new_digest,
                         "--compose-executable", str(case.compose)]
            expected_db = case.db_b
        else:
            prior_raw, prior = case.journal("runtime")
            predecessor_kind, successor_kind = "runtime", "full"
            arguments = ["full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                         "--compose-executable", str(case.compose)]
            expected_db = case.db_a
        prior_inventory = case.inventory()
        predecessor_name = _FULL if predecessor_kind == "full" else _RUNTIME
        _interrupt_after_syscall(case, arguments, operation="before-unlink", destination=predecessor_name)
        successor_raw, successor = case.journal(successor_kind)
        assert successor["status"] == "active" and successor["phase"] == "prepared"
        assert (case.root / ".deployments" / (_FULL if predecessor_kind == "full" else _RUNTIME)).read_bytes() == prior_raw
        assert successor_raw
        _assert_archive(case, successor, prior_raw, predecessor_kind)
        _assert_pointers(case, successor, "old")
        assert case.inventory() == prior_inventory
        case.check_common(expected_db)
        launcher = case.inspect_launcher(successor_kind, successor["transaction_id"])
        trap, marker = case.poison_b()
        recovered = json.loads(case.run("saved-a-recovery", launcher))
        assert recovered["status"] == "rolled_back"
        repeated = json.loads(case.run("saved-a-recovery-repeat", launcher))
        assert repeated["status"] == "rolled_back"
        _, terminal = case.journal(successor_kind)
        assert terminal["status"] == "rolled_back"
        _assert_pointers(case, terminal, "old")
        assert not (case.root / ".deployments" / (_FULL if predecessor_kind == "full" else _RUNTIME)).exists()
        restored_inventory = case.inventory()
        assert set(restored_inventory) == set(prior_inventory)
        for identifier in restored_inventory:
            for field in ("image_id", "name", "running", "restart_count", "owner", "service", "transaction"):
                assert restored_inventory[identifier][field] == prior_inventory[identifier][field]
        case.check_common(expected_db)
        assert _git(case.root, "rev-parse", "HEAD") == case.source_b
        assert (case.root / "scripts/component_runtime.py").read_text(encoding="utf-8") == trap
        assert not marker.exists()
        assert before["running"]
        case.check_canary()
    finally:
        case.finish()


def test_promotion_interruption_recovers_saved_a_with_recorded_compose() -> None:
    case = Case("crash-promotion")
    try:
        before = case.provision(running=True)
        prior_raw, _ = case.journal("runtime")
        arguments = ["full-update", "--candidate-db", str(case.candidate_b), "--components", "none",
                     "--compose-executable", str(case.compose)]
        _interrupt_after_syscall(case, arguments, operation="rename", destination=str(case.live), expected_db=case.db_b)
        _, interrupted = case.journal("full")
        assert interrupted["status"] == "active" and interrupted["phase"] == "promote-db"
        _assert_archive(case, interrupted, prior_raw, "runtime")
        launcher = case.inspect_launcher("full", interrupted["transaction_id"])
        before_refusal = case.inventory()
        pointers_before = {name: (case.root / name).read_bytes() for name in (".deploy_commit", ".deploy_current")}
        wrong = case.directory / "wrong-compose"
        wrong_marker = case.directory / "wrong-compose-executed"
        wrong.write_text(f"#!/bin/sh\nprintf bad > {shlex.quote(str(wrong_marker))}\n", encoding="ascii")
        wrong.chmod(0o500)
        wrong_argv = list(launcher)
        wrong_argv[wrong_argv.index("--compose-executable") + 1] = str(wrong)
        case.run("wrong-compose-refusal", wrong_argv, success=False)
        assert not wrong_marker.exists() and case.inventory() == before_refusal
        assert {name: (case.root / name).read_bytes() for name in pointers_before} == pointers_before
        case.check_common(case.db_b)
        wrong_id = list(launcher)
        position = wrong_id.index("--id") + 1
        wrong_id[position] = interrupted["transaction_id"][:-1] + (
            "0" if interrupted["transaction_id"][-1] != "0" else "1")
        case.run("wrong-transaction-refusal", wrong_id, success=False)
        assert case.inventory() == before_refusal
        assert {name: (case.root / name).read_bytes() for name in pointers_before} == pointers_before
        wrong_root = case.directory / "foreign-root"
        wrong_root.mkdir()
        wrong_root_argv = list(launcher)
        wrong_root_argv[wrong_root_argv.index("--root") + 1] = str(wrong_root)
        case.run("wrong-root-refusal", wrong_root_argv, success=False)
        assert not list(wrong_root.iterdir()) and case.inventory() == before_refusal
        case.check_common(case.db_b)
        trap, marker = case.poison_b()
        recovered = json.loads(case.run("saved-a-promotion-recovery", launcher))
        assert recovered["status"] == "rolled_back"
        repeated = json.loads(case.run("saved-a-promotion-recovery-repeat", launcher))
        assert repeated["status"] == "rolled_back"
        _, terminal = case.journal("full")
        assert terminal["status"] == "rolled_back"
        _assert_pointers(case, terminal, "old")
        inventory = case.inventory()
        assert case.api_id is not None and set(inventory) == {case.api_id}
        assert inventory[case.api_id]["image_id"] == before["image_id"] and inventory[case.api_id]["running"]
        case.ready(case.api_id)
        assert case.reader_value(case.api_id) == "Synthetic Term 0000 a"
        case.check_common(case.db_a)
        assert _git(case.root, "rev-parse", "HEAD") == case.source_b
        assert (case.root / "scripts/component_runtime.py").read_text(encoding="utf-8") == trap
        assert not marker.exists()
        case.check_canary()
    finally:
        case.finish()
