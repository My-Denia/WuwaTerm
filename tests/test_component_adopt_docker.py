"""Explicit local-Docker acceptance for metadata-only adoption.

The test creates uniquely labelled compose fixtures, stopped except for the one
API fixture of the running-api topology. When it ends, pass or fail, it stops and
deletes the containers of its own compose project and the adoption probe
containers recorded under its temporary roots, re-checking each exact ID's label
first. Images and files are not touched.
"""
from __future__ import annotations

from collections.abc import Iterator
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest

from scripts import component_build, deployment_manifest, product_manifest
from test_component_build import REPO, clean_env, export_source, git, invoke_with_lock
from test_deploy_scripts import _deployment_db


pytestmark = pytest.mark.skipif(
    os.environ.get("WUWATERM_RUN_ADOPT_DOCKER") != "1", reason="explicit real Docker adoption only",
)


def remove_labelled_containers(label: str, value: str) -> list[str]:
    """Stop and delete every container whose exact ID still carries ``label=value``.

    Each ID is inspected and its label re-verified before it is touched; nothing
    is selected by name, and no image, volume or network is removed.
    """
    def docker(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["docker", "--host", "unix:///run/docker.sock", *arguments], env=clean_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120, check=False)

    def labelled() -> list[str]:
        listing = docker("ps", "-aq", "--no-trunc", "--filter", f"label={label}={value}")
        assert listing.returncode == 0, "cleanup: container listing failed"
        return listing.stdout.split()

    removed = []
    for identifier in labelled():
        inspected = docker("inspect", "--format", '{{.Id}}|{{index .Config.Labels "' + label + '"}}|{{.State.Running}}',
                           identifier)
        if inspected.returncode:
            continue
        found, actual, running = inspected.stdout.strip().split("|")
        if (found, actual) != (identifier, value):
            continue
        if running == "true":
            docker("stop", identifier)
        if docker("rm", identifier).returncode == 0:
            removed.append(identifier)
    assert not labelled(), f"cleanup: containers labelled {label}={value} remain"
    return removed


@pytest.fixture
def compose_projects(tmp_path: Path) -> Iterator[list[str]]:
    """Collect this test's compose project names and remove their containers at the end.

    Adoption version probes carry only their own probe label, so they are found
    through the records the adoption code writes under the test's temporary roots.
    """
    projects: list[str] = []
    yield projects
    targets = [("com.docker.compose.project", project) for project in projects]
    targets += [("org.wuwaterm.component-adopt-probe", record.stem)
                for record in tmp_path.glob("**/.deployments/component-adopt-probes/*.json")]
    failures = []
    for label, value in dict.fromkeys(targets):
        try:
            remove_labelled_containers(label, value)
        except (AssertionError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{label}={value}: {exc}")
    assert not failures, failures


def run(args: list[str], root: Path, name: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=root, env=env or clean_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False)
    (root / f"{name}.command.json").write_text(json.dumps(args, indent=2) + "\n")
    (root / f"{name}.stdout").write_text(result.stdout)
    (root / f"{name}.stderr").write_text(result.stderr)
    (root / f"{name}.exit").write_text(str(result.returncode) + "\n")
    return result


def locked(root: Path, args: list[str], name: str, *, python: str = sys.executable) -> subprocess.CompletedProcess[str]:
    shell = 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"'
    return run(["sh", "-c", shell, "adopt-fixture", str(root / ".deployments/.deployment.lock"),
                python, "-I", "-B", *args], root, name)


def snapshot(container: str) -> dict:
    result = subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", container],
                            env=clean_env(), capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)[0]
    return {"id": value["Id"], "name": value["Name"], "image": value["Image"], "running": value["State"]["Running"],
            "started_at": value["State"]["StartedAt"], "restarts": value["RestartCount"],
            # Docker returns the same bindings in nondeterministic order.
            "mounts": sorted(value["Mounts"], key=lambda item: item["Destination"]),
            "environment_sha256": hashlib.sha256(json.dumps(value["Config"]["Env"], sort_keys=True).encode()).hexdigest()}


def file_snapshot(path: Path) -> tuple:
    info = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino,
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def leaf_snapshot(root: Path) -> dict[str, tuple]:
    result = {}
    for name in ("chat_settings.json", "channel_replies.json"):
        path = root / "state" / name
        info = path.lstat()
        target = path.resolve(strict=True)
        target_info = target.stat()
        result[name] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
                        os.readlink(path), target.read_bytes(), target_info.st_dev, target_info.st_ino,
                        target_info.st_mode, target_info.st_uid, target_info.st_gid)
    return result


def interruptible_adoption(root: Path, args: list[str], expected_commit: str, *,
                          interrupt_probe: bool = False, drift_env: bool = False,
                          stop_at: str | None = None, original_pointer_inode: int | None = None) -> subprocess.CompletedProcess[str]:
    """Inject a filesystem failure or stop the process after real probe creation.

    Ptrace pauses the actual process at a syscall boundary. The pointer failure
    comes from real directory permissions, not an application mock.
    """
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    libc.ptrace.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    class Registers(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8", "rax", "rcx",
            "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags", "rsp", "ss", "fs_base",
            "gs_base", "ds", "es", "fs", "gs")]

    def traced_child() -> None:
        if libc.ptrace(0, 0, None, None) != 0:  # PTRACE_TRACEME
            os._exit(97)

    def capability_status(pid: int) -> dict:
        fields = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
        return {"uids": [int(value) for value in fields["Uid"].split()],
                **{name: int(fields[name].strip(), 16) for name in ("CapEff", "CapBnd", "CapInh", "CapAmb")}}

    permission_fault = not interrupt_probe and not drift_env and stop_at is None
    parent_capabilities = capability_status(os.getpid())
    adoption = [sys.executable, "-I", "-B", *args]
    if permission_fault:
        assert os.geteuid() == 0 and parent_capabilities["CapEff"] & 0x2
        # The locked test environment may use an interpreter below a private
        # home directory. Adoption itself is stdlib-only; keep the reduced-
        # capability child on the independently accessible system interpreter.
        adoption = ["/usr/bin/python3", "-I", "-B", *args]
        adoption = ["/usr/bin/setpriv", "--inh-caps=-dac_override,-dac_read_search",
                    "--ambient-caps=-dac_override,-dac_read_search",
                    "--bounding-set=-dac_override,-dac_read_search", "--", *adoption]
    shell = 'exec 9<>"$1"; shift; flock -n 9 || exit 98; exec "$@"'
    command = ["sh", "-c", shell, "adopt-fixture", str(root / ".deployments/.deployment.lock"),
               *adoption]
    child = subprocess.Popen(command, cwd=root, env=clean_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, preexec_fn=traced_child)
    mode = root.stat().st_mode & 0o7777
    fault_pending = False
    observed_eacces = False
    observed_probe_interruption = False
    observed_env_drift = False
    observed_publication = False
    fault_child_capabilities = None

    def ptrace(request: int, address: int = 0, data: int = 0) -> int:
        ctypes.set_errno(0)
        result = libc.ptrace(request, child.pid, ctypes.c_void_p(address), ctypes.c_void_p(data))
        if result == -1 and ctypes.get_errno():
            raise OSError(ctypes.get_errno(), "ptrace failed")
        return result

    def read_string(address: int) -> bytes:
        result = bytearray()
        for offset in range(0, 4096, 8):
            word = ptrace(2, address + offset) & ((1 << 64) - 1)  # PTRACE_PEEKDATA
            result.extend(word.to_bytes(8, "little"))
            if b"\0" in result:
                return bytes(result).split(b"\0", 1)[0]
        raise AssertionError("unterminated syscall pathname")

    try:
        _, status = os.waitpid(child.pid, 0)
        assert os.WIFSTOPPED(status)
        ptrace(0x4200, data=1)  # PTRACE_SETOPTIONS, PTRACE_O_TRACESYSGOOD
        ptrace(24)  # PTRACE_SYSCALL
        while True:
            _, status = os.waitpid(child.pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                child.returncode = os.waitstatus_to_exitcode(status)
                break
            assert os.WIFSTOPPED(status)
            sig = os.WSTOPSIG(status)
            if sig == signal.SIGTRAP | 0x80:
                registers = Registers()
                ptrace(12, data=ctypes.addressof(registers))  # PTRACE_GETREGS
                result = ctypes.c_longlong(registers.rax).value
                if interrupt_probe and result == -38 and registers.orig_rax in (56, 58, 435):
                    probes = list((root / ".deployments/component-adopt-probes").glob("*.json"))
                    matching = [json.loads(path.read_text()) for path in probes]
                    matching = [probe for probe in matching if probe.get("kind") == "legacy-core-version"
                                and probe.get("status") == "active" and probe.get("phase") == "start"
                                and probe.get("source", {}).get("commit") == expected_commit]
                    if matching:
                        assert len(matching) == 1
                        probe = matching[0]
                        if probe["status"] == "active":
                            actual = json.loads(subprocess.check_output(
                                ["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]]))[0]
                            assert actual["Config"]["Labels"]["org.wuwaterm.component-adopt-probe"] == probe["probe_id"]
                            assert actual["HostConfig"]["NetworkMode"] == "none" and not actual["Mounts"]
                            assert not actual["State"]["Running"]
                            observed_probe_interruption = True
                            ptrace(24, data=signal.SIGKILL)
                            continue
                if registers.orig_rax in (264, 316):  # renameat / renameat2
                    destination = read_string(registers.r10) if result == -38 else None
                    if stop_at and destination == (b".deploy_commit" if stop_at == "manifest" else b".deploy_current"):
                        journal = json.loads((root / ".deployments/.component-runtime-transaction.json").read_bytes())
                        assert journal["phase"] == ("deploy-commit" if stop_at == "manifest" else "deploy-current")
                        published = root / journal["manifest"]["path"]
                        assert hashlib.sha256(published.read_bytes()).hexdigest() == journal["manifest"]["sha256"]
                        assert not published.stat().st_mode & 0o222
                        assert not (root / ".deploy_current").exists()
                        pointer = root / ".deploy_commit"
                        assert pointer.read_bytes() == (expected_commit + "\n").encode()
                        assert (pointer.stat().st_ino == original_pointer_inode) == (stop_at == "manifest")
                        observed_publication = True
                        ptrace(24, data=signal.SIGKILL)
                        continue
                    if drift_env and destination == b".deploy_commit":
                        (root / ".env").write_text("FIXTURE_VALUE=changed-during-adoption\n")
                        observed_env_drift = True
                    elif not interrupt_probe and not drift_env and not stop_at and destination == b".deploy_current":
                        assert (root / ".deploy_commit").read_bytes() == (expected_commit + "\n").encode()
                        assert not (root / ".deploy_current").exists()
                        fault_child_capabilities = capability_status(child.pid)
                        assert fault_child_capabilities["uids"] == [0, 0, 0, 0]
                        assert all(not fault_child_capabilities[name] & 0x6
                                   for name in ("CapEff", "CapBnd", "CapInh", "CapAmb"))
                        os.chmod(root, 0o555)
                        fault_pending = True
                    elif fault_pending and result != -38:
                        observed_eacces = result == -13
                        os.chmod(root, mode)
                        fault_pending = False
                ptrace(24)
            else:
                ptrace(24, data=0 if sig == signal.SIGTRAP else sig)
    except BaseException:
        if child.returncode is None:
            os.kill(child.pid, signal.SIGKILL)
            os.waitpid(child.pid, 0)
        raise
    finally:
        os.chmod(root, mode)
    stdout, stderr = child.communicate()
    name = ("adopt-publication-" + stop_at if stop_at else "adopt-interrupt" if interrupt_probe
            else "adopt-env-drift" if drift_env else "adopt-fault")
    (root / f"{name}.command.json").write_text(json.dumps(command, indent=2) + "\n")
    (root / f"{name}.stdout").write_text(stdout)
    (root / f"{name}.stderr").write_text(stderr)
    (root / f"{name}.exit").write_text(str(child.returncode) + "\n")
    (root / f"{name}.proof.json").write_text(json.dumps({
        "commit_was_published_before_fault": observed_eacces,
        "kernel_errno": 13 if observed_eacces else None,
        "isolated_probe_was_created_before_kill": observed_probe_interruption,
        "env_changed_before_commit_rename": observed_env_drift,
        "publication_boundary_observed": observed_publication,
        "parent_capabilities": parent_capabilities,
        "permission_fault_child_capabilities": fault_child_capabilities,
        "exit_code": child.returncode,
    }, indent=2) + "\n")
    if stop_at:
        assert observed_publication and child.returncode == -signal.SIGKILL
    elif interrupt_probe:
        assert observed_probe_interruption and child.returncode == -signal.SIGKILL
    elif drift_env:
        assert observed_env_drift
    else:
        assert observed_eacces, "the kernel did not reject the actual pointer rename with EACCES"
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


@pytest.mark.parametrize("topology", ["api", "running-api", "telegram", "both", "wheel-both", "unknown", "unknown-no-receipt",
                                      "pointer-fail-absent", "pointer-fail-present", "legacy-no-receipt",
                                      "legacy-wrong-digest", "legacy-writable-manifest", "legacy-no-core", "legacy-interrupted",
                                      "legacy-terminal-probe", "env-drift", "leaf-committed", "leaf-pointer-fail",
                                      "leaf-retry-interrupted",
                                      "leaf-crash-manifest", "leaf-crash-current"])
def test_real_metadata_adoption_preserves_installed_topology(tmp_path: Path, topology: str,
                                                              compose_projects: list[str]) -> None:
    compose = os.environ["WUWATERM_COMPOSE_EXECUTABLE"]
    root, commit, tree = export_source(REPO, tmp_path)
    source_result = invoke_with_lock(root, commit, tree)
    (root / "source-build.exit").write_text(str(source_result.returncode) + "\n")
    (root / "source-build.stderr").write_text(source_result.stderr)
    assert source_result.returncode == 0, source_result.stderr
    source_receipt = source_result.stdout.strip()
    source = component_build.verify_registered_build(root, source_receipt)
    if topology.startswith("legacy-"):
        # The serving deployment is a separate clean Git fixture without any
        # registered build. Its old image is identified by its v1 manifest.
        deployment = tmp_path / "legacy-deployment"
        deployment.mkdir()
        root, commit, tree = export_source(REPO, deployment)
        (root / ".deployments/.deployment.lock").touch()
        assert not (root / ".deployments/product-builds").exists()
    legacy_commit = source["source"]["commit"]
    if topology == "legacy-no-core":
        bad_context = tmp_path / "legacy-no-core-context"
        bad_context.mkdir()
        (bad_context / "Dockerfile").write_text(
            "FROM python:3.11-slim\nARG SOURCE_COMMIT\n"
            "LABEL org.opencontainers.image.revision=$SOURCE_COMMIT\n"
            "LABEL org.wuwaterm.test.owner=runtime-build-adopt-cbe9\n")
        bad_ref = "wuwaterm-adopt-no-core:" + uuid.uuid4().hex
        built = run(["docker", "--host", "unix:///run/docker.sock", "build", "--pull=false", "--network", "none",
                     "--build-arg", "SOURCE_COMMIT=" + legacy_commit, "--tag", bad_ref, str(bad_context)],
                    root, "missing-core-image")
        assert built.returncode == 0, built.stderr
        inspected = run(["docker", "--host", "unix:///run/docker.sock", "image", "inspect", "--format", "{{.Id}}", bad_ref],
                        root, "missing-core-image-inspect")
        assert inspected.returncode == 0
        source = {**source, "image_ref": bad_ref, "image_id": inspected.stdout.strip()}
    wheel_receipt = None
    wheel = None
    if topology == "wheel-both":
        python = os.environ["WUWATERM_WHEEL_BUILD_PYTHON"]
        arguments = [str(root / "scripts/component_wheel_build.py"), "--root", str(root),
                     "--operations-commit", commit, "--operations-tree", tree,
                     "--telegram-repo", os.environ["WUWATERM_TELEGRAM_REPO"],
                     "--telegram-commit", os.environ["WUWATERM_TELEGRAM_COMMIT"],
                     "--telegram-tree", os.environ["WUWATERM_TELEGRAM_TREE"],
                     "--core-wheel", os.environ["WUWATERM_CORE_WHEEL"],
                     "--core-artifact", os.environ["WUWATERM_CORE_ARTIFACT"],
                     "--dependencies", os.environ["WUWATERM_WHEELHOUSE"]]
        result = locked(root, arguments, "wheel-build", python=python)
        assert result.returncode == 0, result.stderr
        wheel_receipt = result.stdout.strip()
        from scripts import component_wheel_build
        wheel = component_wheel_build.verify_registered_wheel_build(root, wheel_receipt)
    for directory in ("data", "state", "state-api"):
        (root / directory).mkdir()
    leaf_case = topology.startswith("leaf-")
    if leaf_case:
        (root / "state/refs").mkdir()
        for name in ("settings", "replies"):
            (root / "state/refs" / name).write_bytes((name + "\n").encode())
        (root / "state/chat_settings.json").symlink_to("refs/settings")
        (root / "state/channel_replies.json").symlink_to("refs/replies")
    _deployment_db(root / "data/terms.db", "metadata-adoption")
    database, _ = product_manifest.read_database_snapshot(root / "data/terms.db")
    running = topology == "running-api"
    if running:
        from wuwaterm_api.auth import DeviceStore
        DeviceStore(root / "state-api/devices.db").initialize()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
    else:
        (root / "state-api/devices.db").write_bytes(b"synthetic identity store\n")
    env_file = root / ".env"
    env_file.write_text("FIXTURE_VALUE=local-only\n")
    env_file.chmod(0o600)
    has_api = topology in ("api", "running-api", "both", "wheel-both", "unknown", "unknown-no-receipt", "pointer-fail-absent", "pointer-fail-present", "env-drift") or topology.startswith("legacy-") or leaf_case
    has_telegram = topology in ("telegram", "both", "wheel-both") or topology.startswith("legacy-") or leaf_case
    if has_telegram and wheel is None:
        legacy = deployment_manifest.build_manifest(
            source_commit=legacy_commit, image_ref=source["image_ref"], image_id=source["image_id"],
            image_digest="sha256:" + "0" * 64 if topology == "legacy-wrong-digest" else source["image_id"],
            image_revision=legacy_commit, db_path=root / "data/terms.db",
            db_display_path=str(root / "data/terms.db"), backup_path="fixture",
            deployment_utc="2026-11-06T00:00:00Z", immutable=True,
        )
        deployment_manifest.write_manifest(root / ".deployments" / (legacy_commit + ".json"), legacy)
        if topology == "legacy-writable-manifest":
            (root / ".deployments" / (legacy_commit + ".json")).chmod(0o644)
        (root / ".deploy_commit").write_text(legacy_commit + "\n")
    if topology == "pointer-fail-present":
        (root / ".deploy_commit").write_text("f" * 40 + "\n")
    project = "wuwaterm-adopt-" + uuid.uuid4().hex[:12]
    compose_projects.append(project)
    config = root / "adopt-compose.yml"
    services = []
    for service, image, command, state_mount in (
        ("wuwaterm-api", source["image_ref"], "api", "state-api"),
        ("wuwaterm-telegram" if wheel else "wuwaterm", wheel["image_ref"] if wheel else source["image_ref"],
         None if wheel else "bot", "state"),
    ):
        if (service == "wuwaterm-api" and not has_api) or (service != "wuwaterm-api" and not has_telegram):
            continue
        target_state = "/app/state-api" if state_mount == "state-api" else "/app/state"
        environment = "WUWATERM_API_STATE_DIR" if state_mount == "state-api" else "WUWATERM_STATE_DIR"
        extra_environment = f"      WUWATERM_API_PORT: '{port}'\n" if running and service == "wuwaterm-api" else ""
        text = (f"  {service}:\n    image: {image}\n    network_mode: host\n"
                f"    env_file: [{env_file}]\n    labels:\n      org.wuwaterm.test.owner: runtime-build-adopt-cbe9\n"
                f"    environment:\n      WUWATERM_DB_PATH: /app/data/terms.db\n      {environment}: {target_state}\n"
                f"{extra_environment}"
                f"    volumes:\n      - {root / 'data'}:/app/data:ro\n      - {root / state_mount}:{target_state}\n")
        if command:
            text += f"    command: [{command}]\n"
        services.append(text)
    config.write_text("services:\n" + "".join(services))
    created = run([compose, "--project-name", project, "--project-directory", str(root),
                   "--file", str(config), "create"], root, "compose-create")
    assert created.returncode == 0, created.stderr
    if running:
        started = run([compose, "--project-name", project, "--project-directory", str(root),
                       "--file", str(config), "start", "wuwaterm-api"], root, "compose-start")
        assert started.returncode == 0, started.stderr
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("local API fixture did not become reachable")
    listed = run(["docker", "--host", "unix:///run/docker.sock", "ps", "-a", "--no-trunc",
                  "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.ID}}"],
                 root, "containers")
    assert listed.returncode == 0
    ids = listed.stdout.splitlines()
    assert len(ids) == int(has_api) + int(has_telegram)
    (root / "resources.json").write_text(json.dumps({"owner": "runtime-build-adopt-cbe9", "project": project,
        "containers": ids, "source_receipt": source_receipt, "wheel_receipt": wheel_receipt}, indent=2) + "\n")
    before = {identifier: snapshot(identifier) for identifier in ids}
    assert all(value["running"] == running for value in before.values())
    files_before = {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")}
    leaves_before = leaf_snapshot(root) if leaf_case else None
    old_pointer = (root / ".deploy_commit").read_bytes() if (root / ".deploy_commit").exists() else None
    old_pointer_inode = (root / ".deploy_commit").stat().st_ino if old_pointer is not None else None
    assert (old_pointer is not None) == (has_telegram and wheel is None or topology == "pointer-fail-present")
    arguments = [str(root / "scripts/component_runtime.py"), "--root", str(root), "adopt",
                 "--project", project, "--compose-executable", compose]
    if has_api and not topology.startswith("legacy-") and topology != "unknown-no-receipt":
        arguments += ["--api-receipt", "f" * 64 if topology == "unknown" else source_receipt]
    if has_telegram and not topology.startswith("legacy-"):
        arguments += ["--telegram-receipt", wheel_receipt or source_receipt]
    if topology == "legacy-terminal-probe":
        failed = interruptible_adoption(root, arguments, commit)
        assert failed.returncode != 0 and "original pointers were restored" in failed.stderr
        journal_path = root / ".deployments/.component-runtime-transaction.json"
        predecessor = journal_path.read_bytes()
        prior = json.loads(predecessor)
        assert prior["status"] == "rolled_back"
        old_probes = [json.loads(path.read_text()) for path in (root / ".deployments/component-adopt-probes").glob("*.json")]
        assert len(old_probes) == 1 and old_probes[0]["status"] == "completed"
        with (root / "README.md").open("a", encoding="utf-8") as stream:
            stream.write("\nSynthetic operations source B for terminal probe recovery.\n")
        git(root, "add", "README.md")
        git(root, "-c", "user.name=Probe Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: operations source B")
        commit_b = git(root, "rev-parse", "HEAD").stdout.decode().strip()
        assert commit_b != commit
        (root.parent / "operations-source-b.marker").write_text(commit_b + "\n")
        interrupted = interruptible_adoption(root, arguments, commit_b, interrupt_probe=True)
        assert interrupted.returncode == -signal.SIGKILL
        probes = [json.loads(path.read_text()) for path in (root / ".deployments/component-adopt-probes").glob("*.json")]
        active = [item for item in probes if item["status"] == "active"]
        assert len(active) == 1 and active[0]["source"]["commit"] == commit_b
        assert active[0]["predecessor_sha256"] == hashlib.sha256(predecessor).hexdigest()
        assert old_probes[0]["source"]["commit"] == commit
        assert journal_path.read_bytes() == predecessor
        assert ((root / ".deploy_commit").read_bytes() if (root / ".deploy_commit").exists() else None) == old_pointer
        assert not (root / ".deploy_current").exists()
        assert locked(root, arguments, "terminal-probe-writer-refusal").returncode != 0
        active_path = root / ".deployments/component-adopt-probes" / (active[0]["probe_id"] + ".json")
        original_active = active_path.read_bytes()
        tampered = json.loads(original_active)
        tampered["predecessor_sha256"] = "f" * 64
        active_path.write_text(json.dumps(tampered))
        refused = locked(root, [str(root / "scripts/component_runtime.py"), "--root", str(root), "recover-probe",
                                "--probe-id", active[0]["probe_id"], "--compose-executable", compose],
                         "terminal-probe-context-refusal")
        assert refused.returncode != 0 and journal_path.read_bytes() == predecessor
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", active[0]["container_id"]],
                              capture_output=True, check=False).returncode == 0
        active_path.write_bytes(original_active)
        recovered = locked(root, [str(root / "scripts/component_runtime.py"), "--root", str(root), "recover-probe",
                                  "--probe-id", active[0]["probe_id"], "--compose-executable", compose],
                           "terminal-probe-recovery")
        assert recovered.returncode == 0, recovered.stderr
        assert journal_path.read_bytes() == predecessor
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", active[0]["container_id"]],
                              capture_output=True, check=False).returncode != 0
        retried = locked(root, arguments, "terminal-probe-retry")
        assert retried.returncode == 0, retried.stderr
        product = product_manifest.read_manifest(root, json.loads(retried.stdout)["deployment_id"])
        assert product["operations"]["source_commit"] == commit_b
        archived = root / ".deployments/component-runtime-history" / (prior["transaction_id"] + ".json")
        assert archived.read_bytes() == predecessor
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        return
    if topology.startswith("leaf-crash-"):
        result = interruptible_adoption(root, arguments, commit,
                                        stop_at="manifest" if topology.endswith("manifest") else "current",
                                        original_pointer_inode=old_pointer_inode)
    elif topology.startswith("pointer-fail-") or topology in (
        "legacy-interrupted", "env-drift", "leaf-pointer-fail", "leaf-retry-interrupted",
    ):
        result = interruptible_adoption(root, arguments, commit, interrupt_probe=topology == "legacy-interrupted",
                                        drift_env=topology == "env-drift")
    else:
        result = locked(root, arguments, "adopt")
    if leaf_case:
        journal_path = root / ".deployments/.component-runtime-transaction.json"
        if topology in ("leaf-pointer-fail", "leaf-retry-interrupted"):
            assert result.returncode != 0 and "original pointers were restored" in result.stderr
            predecessor = journal_path.read_bytes()
            old_journal = json.loads(predecessor)
            assert old_journal["status"] == "rolled_back"
            assert (root / ".deploy_commit").read_bytes() == old_pointer
            assert not (root / ".deploy_current").exists() and leaf_snapshot(root) == leaves_before
            if topology == "leaf-retry-interrupted":
                interrupted = interruptible_adoption(root, arguments, commit, stop_at="current",
                                                     original_pointer_inode=(root / ".deploy_commit").stat().st_ino)
                assert interrupted.returncode == -signal.SIGKILL
                active = json.loads(journal_path.read_bytes())
                assert active["status"] == "active" and active["before"]["manifest"] is None
                assert active["predecessor"]["journal_sha256"] == hashlib.sha256(predecessor).hexdigest()
                first_archive = root / active["predecessor"]["archive_path"]
                assert first_archive.read_bytes() == predecessor and not first_archive.stat().st_mode & 0o222
                launcher = root / active["source"]["bootstrap"]["path"] / "component_recovery.py"
                recover = [sys.executable, "-I", "-B", str(launcher), "--root", str(root), "--kind", "adopt",
                           "--id", active["transaction_id"], "--compose-executable",
                           str(root / active["source"]["compose"]["path"])]
                (root / "scripts/component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
                git(root, "add", "scripts/component_transaction.py")
                git(root, "-c", "user.name=Adoption Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: trap interrupted retry imports")
                assert git(root, "rev-parse", "HEAD").stdout.decode().strip() != commit
                for attempt in (1, 2):
                    recovered = run(recover, root, f"leaf-retry-rollback-{attempt}")
                    assert recovered.returncode == 0, recovered.stderr
                    outcome = json.loads(recovered.stdout)
                    assert outcome["status"] == "rolled_back" and outcome["already_restored"] is (attempt == 2)
                    assert (root / ".deploy_commit").read_bytes() == old_pointer
                    assert not (root / ".deploy_current").exists()
                    assert {identifier: snapshot(identifier) for identifier in ids} == before
                    assert leaf_snapshot(root) == leaves_before
                    assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
                retry_predecessor = journal_path.read_bytes()
                retry_journal = json.loads(retry_predecessor)
                assert retry_journal["status"] == "rolled_back" and retry_journal["predecessor"] == active["predecessor"]
                git(root, "checkout", "--detach", commit)
                assert git(root, "rev-parse", "HEAD").stdout.decode().strip() == commit
                assert not git(root, "diff", "HEAD", "--").stdout
            result = locked(root, arguments, "leaf-adopt-after-rollback")
            assert result.returncode == 0, result.stderr
            archive = root / ".deployments/component-runtime-history" / (old_journal["transaction_id"] + ".json")
            assert archive.read_bytes() == predecessor and not archive.stat().st_mode & 0o222
            if topology == "leaf-retry-interrupted":
                retry_archive = root / ".deployments/component-runtime-history" / (retry_journal["transaction_id"] + ".json")
                assert retry_archive.read_bytes() == retry_predecessor and not retry_archive.stat().st_mode & 0o222
        journal = json.loads(journal_path.read_bytes())
        published = root / journal["manifest"]["path"]
        published_bytes = published.read_bytes()
        assert hashlib.sha256(published_bytes).hexdigest() == journal["manifest"]["sha256"]
        assert not published.stat().st_mode & 0o222
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert leaf_snapshot(root) == leaves_before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        committed_case = topology in ("leaf-committed", "leaf-pointer-fail", "leaf-retry-interrupted")
        assert (result.returncode == 0) == committed_case
        if committed_case:
            assert journal["status"] == "committed" and json.loads(result.stdout)["deployment_id"] == journal["transaction_id"]
            old_journal_bytes = journal_path.read_bytes()
            old_pointers = ((root / ".deploy_commit").read_bytes(), (root / ".deploy_current").read_bytes())
            update = locked(root, [str(root / "scripts/component_runtime.py"), "--root", str(root), "runtime-update",
                                   "--components", "api", "--api-receipt", source_receipt,
                                   "--compose-executable", compose], "leaf-update-refusal")
            assert update.returncode != 0 and "runtime update preparation failed" in update.stderr
            assert journal_path.read_bytes() == old_journal_bytes
            assert ((root / ".deploy_commit").read_bytes(), (root / ".deploy_current").read_bytes()) == old_pointers
            assert {identifier: snapshot(identifier) for identifier in ids} == before and leaf_snapshot(root) == leaves_before
        else:
            assert result.returncode == -signal.SIGKILL and journal["status"] == "active"
            assert not (root / ".deploy_current").exists()
        # The recorded launcher must use saved source A even when B traps imports.
        (root / "scripts/component_transaction.py").write_text("raise RuntimeError('source B loaded')\n")
        git(root, "add", "scripts/component_transaction.py")
        git(root, "-c", "user.name=Adoption Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: trap source B imports")
        assert git(root, "rev-parse", "HEAD").stdout.decode().strip() != commit
        launcher = root / journal["source"]["bootstrap"]["path"] / "component_recovery.py"
        recover = [sys.executable, "-I", "-B", str(launcher), "--root", str(root), "--kind", "adopt",
                   "--id", journal["transaction_id"], "--compose-executable", str(root / journal["source"]["compose"]["path"])]
        for attempt in (1, 2):
            recovered = run(recover, root, f"leaf-recovery-{attempt}")
            assert recovered.returncode == 0, recovered.stderr
            outcome = json.loads(recovered.stdout)
            if committed_case:
                assert outcome["status"] == "committed" and outcome["already_committed"] is True
                assert (root / ".deploy_current").read_bytes() == (journal["transaction_id"] + "\n").encode()
                assert (root / ".deploy_commit").read_bytes() == (commit + "\n").encode()
            else:
                assert outcome["status"] == "rolled_back" and outcome["already_restored"] is (attempt == 2)
                assert (root / ".deploy_commit").read_bytes() == old_pointer
                assert not (root / ".deploy_current").exists()
            assert published.read_bytes() == published_bytes and not published.stat().st_mode & 0o222
            assert {identifier: snapshot(identifier) for identifier in ids} == before
            assert leaf_snapshot(root) == leaves_before
            assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        return
    if topology == "env-drift":
        assert result.returncode != 0 and "pointer rollback is unresolved" in result.stderr
        assert not (root / ".deploy_commit").exists() and not (root / ".deploy_current").exists()
        journal = json.loads((root / ".deployments/.component-runtime-transaction.json").read_text())
        assert journal["status"] == "unresolved"
        assert env_file.read_text() == "FIXTURE_VALUE=changed-during-adoption\n"
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert all(file_snapshot(path) == files_before[str(path)] for path in
                   (root / "data/terms.db", root / "state-api/devices.db"))
        return
    if topology == "legacy-interrupted":
        probes = list((root / ".deployments/component-adopt-probes").glob("*.json"))
        assert len(probes) == 1
        probe = json.loads(probes[0].read_text())
        assert probe["status"] == "active" and probe["phase"] == "start" and probe["container_id"]
        assert (root / ".deploy_commit").read_bytes() == old_pointer
        assert not (root / ".deploy_current").exists()
        assert not (root / ".deployments/products").exists()
        refused = locked(root, [str(root / "scripts/deployment_admission.py"), "legacy"], "interrupted-legacy")
        assert refused.returncode != 0
        refused = locked(root, arguments, "interrupted-component")
        assert refused.returncode != 0
        foreign_parent = tmp_path / "foreign-source-b"
        foreign_parent.mkdir()
        foreign, _, _ = export_source(REPO, foreign_parent)
        with (foreign / "README.md").open("a", encoding="utf-8") as stream:
            stream.write("\nSynthetic source B for interrupted-probe refusal.\n")
        git(foreign, "add", "README.md")
        git(foreign, "-c", "user.name=Probe Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "test: synthetic source B")
        (foreign / ".deployments/.deployment.lock").touch()
        (foreign / ".deploy_commit").write_bytes(old_pointer)
        for key in ("capsule", "bootstrap"):
            relative = probe["source"][key]["path"]
            shutil.copytree(root / relative, foreign / relative)
        foreign_probes = foreign / ".deployments/component-adopt-probes"
        foreign_probes.mkdir()
        (foreign_probes / probes[0].name).write_bytes(probes[0].read_bytes())
        refused = locked(foreign, [str(foreign / "scripts/component_runtime.py"), "--root", str(foreign),
                                   "recover-probe", "--probe-id", probe["probe_id"]], "foreign-probe-recovery")
        assert refused.returncode != 0
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]],
                              capture_output=True, check=False).returncode == 0
        original_probe_record = probes[0].read_bytes()
        altered = json.loads(original_probe_record)
        altered["container_id"] = ids[0]  # A real serving fixture is never an owned probe.
        probes[0].write_text(json.dumps(altered))
        refused = locked(root, [str(root / "scripts/component_runtime.py"), "--root", str(root),
                                "recover-probe", "--probe-id", probe["probe_id"]], "probe-recovery-mismatch")
        assert refused.returncode != 0
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]],
                              capture_output=True, check=False).returncode == 0
        probes[0].write_bytes(original_probe_record)
        recovered = locked(root, [str(root / "scripts/component_runtime.py"), "--root", str(root),
                                  "recover-probe", "--probe-id", probe["probe_id"]], "probe-recovery")
        assert recovered.returncode == 0, recovered.stderr
        assert json.loads(recovered.stdout)["status"] == "failed-retryable"
        assert json.loads(probes[0].read_text())["status"] == "failed"
        assert not (root / ".deployments" / ("component-adopt-probe-config-" + probe["probe_id"])).exists()
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]],
                              capture_output=True, check=False).returncode != 0
        assert (root / ".deploy_commit").read_bytes() == old_pointer
        assert not (root / ".deploy_current").exists()
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        retried = locked(root, arguments, "adopt-after-probe-recovery")
        assert retried.returncode == 0, retried.stderr
        adopted = product_manifest.read_manifest(root, json.loads(retried.stdout)["deployment_id"])
        assert adopted["components"]["api"]["provenance"]["kind"] == "legacy-image"
        assert adopted["components"]["telegram"]["provenance"]["kind"] == "legacy-image"
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        return
    if topology.startswith("pointer-fail-"):
        assert result.returncode != 0
        assert "original pointers were restored" in result.stderr
        assert ((root / ".deploy_commit").read_bytes() if (root / ".deploy_commit").exists() else None) == old_pointer
        assert not (root / ".deploy_current").exists()
        journal_path = root / ".deployments/.component-runtime-transaction.json"
        predecessor = journal_path.read_bytes()
        journal = json.loads(predecessor)
        assert journal["status"] == "rolled_back"
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        retried = locked(root, arguments, "adopt-after-pointer-rollback")
        assert retried.returncode == 0, retried.stderr
        successor = json.loads(retried.stdout)["deployment_id"]
        archived = root / ".deployments/component-runtime-history" / (journal["transaction_id"] + ".json")
        assert archived.read_bytes() == predecessor and archived.stat().st_mode & 0o222 == 0
        assert (root / ".deploy_commit").read_bytes() == (commit + "\n").encode()
        assert (root / ".deploy_current").read_bytes() == (successor + "\n").encode()
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        return
    if topology in ("unknown", "unknown-no-receipt", "legacy-wrong-digest", "legacy-writable-manifest", "legacy-no-core"):
        assert result.returncode != 0
        assert ((root / ".deploy_commit").read_bytes() if (root / ".deploy_commit").exists() else None) == old_pointer
        assert not (root / ".deploy_current").exists()
        assert not (root / ".deployments/products").exists()
        assert {identifier: snapshot(identifier) for identifier in ids} == before
        assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
        probes = list((root / ".deployments/component-adopt-probes").glob("*.json"))
        if topology == "legacy-no-core":
            assert len(probes) == 1 and json.loads(probes[0].read_text())["status"] == "failed"
            probe = json.loads(probes[0].read_text())
            assert not (root / ".deployments" / ("component-adopt-probe-config-" + probe["probe_id"])).exists()
            assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]],
                                  capture_output=True, check=False).returncode != 0
        else:
            assert not probes
        return
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["polling_health"] == "unconfirmed"
    product = product_manifest.read_manifest(root, outcome["deployment_id"])
    assert (product["components"]["api"] is not None) == has_api
    assert (product["components"]["telegram"] is not None) == has_telegram
    assert product["database"] == database
    for name, component in product["components"].items():
        if component is None:
            continue
        recipe = product_manifest.read_recipe_snapshot(root, component["recipe"]["sha256"])
        assert recipe["env_files"] == [str(env_file)]
        state_name = "WUWATERM_API_STATE_DIR" if name == "api" else "WUWATERM_STATE_DIR"
        state_path = "/app/state-api" if name == "api" else "/app/state"
        assert {"name": state_name, "mode": "literal", "value": state_path} in recipe["environment_overrides"]
    if has_api:
        identity_info = (root / "state-api/devices.db").stat()
        assert product["api_identity"] == {"path": str(root / "state-api/devices.db"),
                                           "device": identity_info.st_dev, "inode": identity_info.st_ino}
    assert (root / ".deploy_commit").read_bytes() == (commit + "\n").encode()
    assert (root / ".deploy_current").read_bytes() == (outcome["deployment_id"] + "\n").encode()
    assert {identifier: snapshot(identifier) for identifier in ids} == before
    assert {str(p): file_snapshot(p) for p in (env_file, root / "data/terms.db", root / "state-api/devices.db")} == files_before
    if wheel:
        assert product["components"]["telegram"]["provenance"]["kind"] == "wheel-image"
        assert product["components"]["telegram"]["provenance"]["build_receipt_sha256"] == wheel_receipt
        assert product["components"]["telegram"]["core"]["verified_commit"] != commit
        assert product["components"]["telegram"]["source"]["verified_commit"] == os.environ["WUWATERM_TELEGRAM_COMMIT"]
    if has_telegram and not wheel:
        assert product["components"]["telegram"]["provenance"]["kind"] == "legacy-image"
        assert product["components"]["telegram"]["source"]["verified_commit"] is None
    if topology == "legacy-no-receipt":
        assert product["components"]["api"]["provenance"]["kind"] == "legacy-image"
        assert product["components"]["api"]["source"]["verified_commit"] is None
        assert product["components"]["api"]["core"]["version"] == "0.7.0.dev1"
        assert not (root / ".deployments/product-builds").exists()
        probes = list((root / ".deployments/component-adopt-probes").glob("*.json"))
        assert len(probes) == 1
        probe = json.loads(probes[0].read_text())
        assert probe["status"] == "completed" and probe["installed_core_version"] == "0.7.0.dev1"
        assert probe["predecessor_sha256"] is None
        assert not (root / ".deployments" / ("component-adopt-probe-config-" + probe["probe_id"])).exists()
        assert probe["image_id"] == source["image_id"]
        assert probe["output_sha256"] == hashlib.sha256(b"0.7.0.dev1\n\0").hexdigest()
        assert subprocess.run(["docker", "--host", "unix:///run/docker.sock", "inspect", probe["container_id"]],
                              capture_output=True, check=False).returncode != 0
