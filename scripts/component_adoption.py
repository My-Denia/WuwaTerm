#!/usr/bin/env python3
"""Inspect and adopt an existing component deployment."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import stat
import subprocess
import sys
from typing import Any

# Historical recovery executes this module from the verified source capsule,
# with isolated mode enabled. Do not import modules from the deployment root.
CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
sys.path.insert(0, str(CODE_ROOT / "src"))
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402

from component_inspect import (  # noqa: E402
    inspect_deployment as inspect_deployment,
    inspect_container as inspect_container,
    observe_unrecorded as observe_unrecorded,
    _compose_input as _compose_input,
)

from component_adopt_probe import (  # noqa: E402
    _LEGACY_VERSION_CODE as _LEGACY_VERSION_CODE,
    _env_mapping as _env_mapping,
    _PROBE_STAGE_DIR as _PROBE_STAGE_DIR,
    _PROBE_STAGE_NAME as _PROBE_STAGE_NAME,
    _PROBE_STAT_KEYS as _PROBE_STAT_KEYS,
    _PROBE_STABLE_KEYS as _PROBE_STABLE_KEYS,
    _PROBE_PARENT_KEYS as _PROBE_PARENT_KEYS,
    _probe_file_stat as _probe_file_stat,
    _probe_parent_stat as _probe_parent_stat,
    _probe_read_at as _probe_read_at,
    _probe_staging as _probe_staging,
    pending_adopt_probe_staging as pending_adopt_probe_staging,
    reconcile_adopt_probe_staging as reconcile_adopt_probe_staging,
    _read_adopt_probe as _read_adopt_probe,
    _write_adopt_probe as _write_adopt_probe,
    _adopt_probe_daemon as _adopt_probe_daemon,
    _owned_adopt_probe as _owned_adopt_probe,
    _remove_owned_adopt_probe as _remove_owned_adopt_probe,
    _adopt_probe_argv as _adopt_probe_argv,
    _run_adopt_probe as _run_adopt_probe,
    _legacy_core_version as _legacy_core_version,
    recover_adopt_probe as recover_adopt_probe,
)

from component_journal_runtime import (  # noqa: E402
    JOURNAL_NAME,
    RUNTIME4_SCHEMA_VERSION,
    JOURNAL_SCHEMA_VERSION,
    REFRESH_ACTION,
    _HEX40,
    _HEX64,
    _DEPLOYMENT_ID,
    JournalError,
    _fail,
    _match,
    _absolute,
    _image_id,
    tool_bytes_identity,
    predecessor_reference,
    validate_journal,
    _decode,
    _read_pointer,
)


def _project_containers(projects: list[str]) -> dict[str, tuple[dict[str, Any], str, str]]:
    """Discover the installed, non-orphaned product containers in each project."""
    import component_observe as observe  # noqa: PLC0415
    if not projects or len(set(projects)) != len(projects):
        _fail("one or more distinct Compose projects are required")
    for project in projects:
        manifest._name(project, "Compose project")
    connection = observe._UnixHTTPConnection("localhost", timeout=15)
    try:
        connection.request("GET", "/containers/json?all=1")
        response = connection.getresponse()
        raw = response.read(observe._MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > observe._MAX_RESPONSE:
            _fail("Docker container inventory is unavailable")
        entries = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise JournalError("Docker container inventory is unavailable") from exc
    finally:
        connection.close()
    if type(entries) is not list:
        _fail("Docker container inventory is invalid")
    installed: dict[str, tuple[dict[str, Any], str, str]] = {}
    services = {"wuwaterm-api": "api", "wuwaterm-telegram": "telegram"}
    for entry in entries:
        if type(entry) is not dict or entry.get("Labels") is not None and type(entry.get("Labels")) is not dict:
            _fail("Docker container inventory is invalid")
        labels = entry.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        if project not in projects:
            continue
        service = labels.get("com.docker.compose.service")
        if service == "wuwaterm-builder":
            continue
        if labels.get("com.docker.compose.oneoff") == "True":
            _fail("Compose project contains an unknown or duplicate service")
        if service == "wuwaterm":
            state = entry.get("State")
            if state in {"exited", "created", "dead"}:
                sys.stderr.write(
                    "component_adoption: skipping stopped legacy 'wuwaterm' container "
                    "(remove it manually with `docker rm wuwaterm-bot`)\n"
                )
                continue
            _fail(
                f"legacy inline Telegram bot container (service 'wuwaterm', state={state!r}) is still active; "
                "stop and remove it (`docker stop wuwaterm-bot && docker rm wuwaterm-bot`) "
                "and migrate to standalone My-Denia/WuwaTerm-Telegram (or roll back to v0.16.0) before adopting"
            )
        component = services.get(service)
        if component is None or component in installed:
            _fail("Compose project contains an unknown or duplicate service")
        identifier = _match(entry.get("Id"), _HEX64, "container ID")
        installed[component] = (observe._inspect(identifier), project, service)
    if not installed:
        _fail("no installed product containers were discovered")
    return installed


def _compose_config(executable: Path, raw: dict[str, Any], project: str,
                    service: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, tuple[Any, ...]]]:
    """Resolve current Compose config without disclosing its environment values."""
    labels = raw["Config"]["Labels"]
    try:
        working = _absolute(labels["com.docker.compose.project.working_dir"], "Compose working directory")
        files = labels["com.docker.compose.project.config_files"].split(",")
        files = [_absolute(name, "Compose config file") for name in files]
        if not files or len(set(files)) != len(files):
            _fail("Compose config file references are invalid")
        executable = Path(executable)
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            _fail("Compose executable is unavailable")
        command = [str(executable), "--project-name", project, "--project-directory", working]
        for filename in files:
            command += ["--file", filename]
        interpolation = labels.get("com.docker.compose.project.environment_file")
        if interpolation:
            for filename in interpolation.split(","):
                command += ["--env-file", _absolute(filename, "Compose interpolation file")]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

        def run(*arguments: str) -> bytes:
            result = subprocess.run([*command, "config", *arguments], cwd=working, env=env,
                                    pass_fds=(9,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, check=False)
            if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                _fail("Compose config cannot be resolved")
            return result.stdout

        resolved = _decode(run("--format", "json"))["services"][service]
        unresolved = _decode(run("--no-env-resolution", "--format", "json"))["services"][service]
        hashes = run("--hash", service).decode("ascii").strip().split()
        expected_hash = labels.get("com.docker.compose.config-hash")
        if len(hashes) != 2 or hashes[0] != service or hashes[1] != expected_hash:
            _fail("current Compose service differs from the installed container")
        if type(resolved) is not dict or type(unresolved) is not dict:
            _fail("Compose service config is invalid")
        references = set(files)
        default_env = Path(working) / ".env"
        if default_env.exists() or default_env.is_symlink():
            references.add(str(default_env))
        if interpolation:
            references.update(interpolation.split(","))
        env_files = unresolved.get("env_file") or []
        if type(env_files) is not list:
            _fail("Compose env-file references are invalid")
        for item in env_files:
            if (type(item) is not dict or set(item) - {"path", "required"}
                    or item.get("required", True) is not True):
                _fail("optional or unsupported Compose env files are not supported")
            references.add(_absolute(item.get("path"), "Compose env file"))
        fingerprints = {path: _compose_input(Path(path)) for path in references}
        return resolved, unresolved, fingerprints
    except JournalError:
        raise
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise JournalError("Compose service config is unavailable") from exc


def _recipe_from_compose(raw: dict[str, Any], image: dict[str, Any], observed: dict[str, Any],
                         resolved: dict[str, Any], unresolved: dict[str, Any]) -> dict[str, Any]:
    effective = _env_mapping(image["Config"].get("Env") or [])
    resolved_env = resolved.get("environment") or {}
    explicit_env = unresolved.get("environment") or {}
    if type(resolved_env) is not dict or type(explicit_env) is not dict:
        _fail("Compose environment is invalid")
    for name, value in resolved_env.items():
        if type(name) is not str or not manifest._ENV_NAME.fullmatch(name) or type(value) is not str:
            _fail("Compose environment is incomplete")
        effective[name] = value
    if effective != _env_mapping(raw["Config"].get("Env")):
        _fail("current Compose environment differs from the installed container")
    overrides = []
    for name in sorted(explicit_env):
        value = resolved_env.get(name)
        if type(value) is not str:
            _fail("Compose environment override cannot be reproduced")
        if value == "":
            overrides.append({"name": name, "mode": "empty"})
        elif name in manifest._LITERAL_ENV:
            overrides.append({"name": name, "mode": "literal", "value": value})
        else:
            _fail("nonempty secret or unsupported Compose environment override")
    env_files = [item["path"] for item in (unresolved.get("env_file") or [])]
    snapshot = {**observed["recipe"], "schema_version": 1, "image_id": observed["container"]["image_id"],
                "env_files": env_files, "environment_overrides": overrides}
    manifest.validate_recipe_snapshot(snapshot)
    return snapshot


def _snapshot_pointer(root: Path, name: str, pattern: re.Pattern[str]) -> dict[str, Any]:
    value = _read_pointer(root, name, pattern)
    return {"present": value is not None, "bytes": None if value is None else value + "\n"}


def _replace_pointer(root: Path, name: str, expected: dict[str, Any], desired: dict[str, Any]) -> None:
    pattern = _HEX40 if name == ".deploy_commit" else _DEPLOYMENT_ID
    if _snapshot_pointer(root, name, pattern) != expected:
        _fail("deployment pointer changed during adoption")
    with manifest._directory(root, (), create=False) as directory:
        if not desired["present"]:
            if expected["present"]:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            return
        temporary = ".component-pointer-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(desired["bytes"].encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            if _snapshot_pointer(root, name, pattern) != expected:
                _fail("deployment pointer changed during adoption")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _write_adopt_journal(root: Path, value: dict[str, Any], *, new: bool = False) -> None:
    root = admission._physical_root(Path(root))
    admission.require_lock(root)
    raw = manifest._encode(validate_journal(value))
    with manifest._directory(root, (".deployments",), create=False) as directory:
        if new:
            manifest._publish_unnamed(directory, JOURNAL_NAME, raw, mode=0o600)
            return
        temporary = ".component-journal-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            existing = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                _fail("adoption journal was replaced")
            os.replace(temporary, JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _legacy_component(root: Path, image: dict[str, Any], image_ref: str, database: dict[str, Any],
                      old_commit: str | None, versions: dict[str, str], operations_source: dict[str, Any],
                      *, observed_version: str | None = None, compose_path: Path | None = None,
                      predecessor_raw: bytes | None = None) -> dict[str, Any]:
    import deployment_manifest as legacy  # noqa: PLC0415
    image_id = _image_id(image.get("Id"), "legacy image ID")
    revision = (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    if old_commit is None or old_commit != revision:
        _fail("legacy image has no matching prior deployment pointer and revision")
    path = root / ".deployments" / (old_commit + ".json")
    with manifest._directory(root, (".deployments",), create=False) as directory:
        raw = manifest._read_file(directory, path.name)
    decoded = _decode(raw)
    try:
        previous = legacy.verify_manifest(path, source_commit=old_commit, image_id=image_id,
                                          image_digest=None, db_path=Path(database["path"]), immutable=True)
    except legacy.ManifestError as exc:
        raise JournalError("legacy deployment manifest cannot bind the installed image") from exc
    digest = previous["image"]["digest"]
    known_digests = image.get("RepoDigests") or []
    with manifest._directory(root, (".deployments",), create=False) as directory:
        unchanged = manifest._read_file(directory, path.name) == raw
    if (not unchanged or previous != decoded or previous["image"]["revision"] != revision
            or previous["database"]["sha256"] != database["sha256"]
            or (digest != image_id and digest not in known_digests)):
        _fail("legacy deployment image digest or evidence differs")
    if observed_version is not None:
        if image_id in versions and versions[image_id] != observed_version:
            _fail("legacy Core version observations disagree")
        versions[image_id] = observed_version
    elif image_id not in versions:
        versions[image_id] = _legacy_core_version(root, image_id, revision, operations_source,
                                                   compose_path=compose_path, predecessor_raw=predecessor_raw)
    version = versions[image_id]
    # The old manifest binds the observed image and revision claim; it does
    # not authenticate its source or establish unavailable wheel identities.
    source = {"revision_claim": revision, "verified_commit": None, "verified_tree": None}
    return {"repository": "My-Denia/WuwaTerm", "version": version, "source": source,
            "core": {"version": version, **source},
            "image": {"ref": image_ref, "id": image_id, "digest": digest, "revision": revision},
            "provenance": {"kind": "legacy-image", "legacy_manifest_sha256": hashlib.sha256(raw).hexdigest(),
                           "wheel_identity": "unavailable", "lock_identity": "unavailable"}}


def _adopt_component(root: Path, component: str, receipt_hash: str | None, image: dict[str, Any],
                     image_ref: str, database: dict[str, Any], old_commit: str | None,
                     versions: dict[str, str], operations_source: dict[str, Any], *,
                     compose_path: Path | None, predecessor_raw: bytes | None) -> dict[str, Any]:
    import component_build as build  # noqa: PLC0415
    if receipt_hash is None:
        return _legacy_component(root, image, image_ref, database, old_commit, versions, operations_source,
                                 compose_path=compose_path, predecessor_raw=predecessor_raw)
    try:
        receipt = build.verify_registered_build(root, receipt_hash)
    except build.BuildError:
        receipt = None
    image_id = image.get("Id")
    revision = (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
    if receipt is not None:
        if image_id != receipt["image_id"] or revision != receipt["image_revision"]:
            _fail("installed image differs from its registered source build")
        source = {"revision_claim": revision, "verified_commit": receipt["source"]["commit"],
                  "verified_tree": receipt["source"]["tree"]}
        version = receipt["installed_core_version"]
        core = {"version": version, **source}
        if component == "api":
            provenance = {"kind": "source-image", "build_recipe_sha256": receipt["recipe_sha256"],
                          "build_receipt_sha256": receipt_hash, "wheel_identity": "not-built-from-wheel"}
        else:
            return _legacy_component(root, image, image_ref, database, old_commit, versions, operations_source,
                                     observed_version=(version if receipt["recovery_source"] == {
                                         key: value for key, value in operations_source.items() if key != "compose"
                                     } else None),
                                     compose_path=compose_path,
                                     predecessor_raw=predecessor_raw)
        return {"repository": "My-Denia/WuwaTerm", "version": version, "source": source, "core": core,
                "image": {"ref": image_ref, "id": image_id, "digest": image_id, "revision": revision},
                "provenance": provenance}
    if component != "telegram":
        _fail("installed API image has no registered source build")
    try:
        import component_wheel_build as wheel  # noqa: PLC0415
        receipt = wheel.verify_registered_wheel_build(root, receipt_hash)
    except Exception as exc:
        raise JournalError("installed Telegram image has no registered wheel build") from exc
    if image_id != receipt["image_id"] or revision != receipt["image_revision"]:
        _fail("installed image differs from its registered wheel build")
    source = {"revision_claim": revision, "verified_commit": receipt["source"]["commit"],
              "verified_tree": receipt["source"]["tree"]}
    core_source = receipt["core_source"]
    core = {"version": receipt["core_version"], "revision_claim": core_source["commit"],
            "verified_commit": core_source["commit"], "verified_tree": core_source["tree"]}
    provenance = {"kind": "wheel-image", "telegram_wheel_sha256": receipt["telegram_wheel_sha256"],
                  "core_wheel_sha256": receipt["core_wheel_sha256"],
                  "core_lock_sha256": receipt["core_lock_sha256"],
                  "core_producer_kind": receipt["core_producer_kind"], "build_receipt_sha256": receipt_hash}
    return {"repository": "My-Denia/WuwaTerm-Telegram", "version": receipt["telegram_version"],
            "source": source, "core": core,
            "image": {"ref": image_ref, "id": image_id, "digest": image_id, "revision": revision},
            "provenance": provenance}


def adopt(root: Path, *, projects: list[str], compose_executable: Path,
          api_receipt: str | None, telegram_receipt: str | None,
          expected_sha: str | None = None) -> str:
    """Record a verified existing topology without starting or changing services."""
    import component_observe as observe  # noqa: PLC0415
    import component_recovery as recovery  # noqa: PLC0415
    import component_operations as operations  # noqa: PLC0415
    import component_terminal as terminal  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import package_recovery_source as capsule  # noqa: PLC0415

    root = Path(os.path.abspath(os.fspath(root)))
    try:
        admission.admit_component(root, operation="adopt", code_root=CODE_ROOT)
        compose_executable = operations._compose_binary(compose_executable)
        predecessor = terminal.verify_terminal(root, compose_path=compose_executable)
        predecessor_raw = None if predecessor is None else predecessor[0]
        if _read_pointer(root, ".deploy_current", _DEPLOYMENT_ID) is not None:
            _fail("deployment is already adopted")
        commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
        if expected_sha is not None and (_match(expected_sha, _HEX40, "expected source commit") != commit):
            _fail("source commit differs from --expected-sha")
        recovery._load_packager(root / "scripts" / "package_recovery_source.py")
        recovery._verify_clean_source(root, commit, tree)
        old_commit = _read_pointer(root, ".deploy_commit", _HEX40)
        old_pointers = {".deploy_commit": _snapshot_pointer(root, ".deploy_commit", _HEX40),
                        ".deploy_current": _snapshot_pointer(root, ".deploy_current", _DEPLOYMENT_ID)}
        installed = _project_containers(projects)
        requested = {"api": api_receipt, "telegram": telegram_receipt}
        if any(name not in installed and requested[name] is not None for name in requested):
            _fail("a receipt was supplied for an absent component")
        fingerprint_key = os.urandom(32)
        observations: dict[str, Any] = {}
        snapshots: dict[str, Any] = {}
        images: dict[str, Any] = {}
        references: dict[str, tuple[Any, ...]] = {}
        database_paths = set()
        api_identity = None
        api_identity_path = None
        for name, (raw, project, service) in installed.items():
            config = raw["Config"]
            image_id = _image_id(raw["Image"], "installed image ID")
            image = observe._docker_json("/images/" + image_id + "/json")
            if image.get("Id") != image_id:
                _fail("installed image identity changed")
            actual = observe.observe_container(
                raw["Id"], expected_image_id=image_id, project=project, service=service,
                expected_command=config.get("Cmd"), expected_entrypoint=config.get("Entrypoint"),
                env_fingerprint_key=fingerprint_key,
            )
            resolved, unresolved, files = _compose_config(compose_executable, raw, project, service)
            if resolved.get("image") != config.get("Image"):
                _fail("current Compose image reference differs from the installed container")
            snapshot = _recipe_from_compose(raw, image, actual, resolved, unresolved)
            for path, identity in files.items():
                if path in references and references[path] != identity:
                    _fail("Compose input changed during observation")
                references[path] = identity
            command = config.get("Cmd")
            db_path = (command[1] if type(command) is list and len(command) == 2 and command[0] == "--db"
                       else actual["path_settings"]["WUWATERM_DB_PATH"] or "/app/data/terms.db")
            database_paths.add(observe.resolve_bind_path(snapshot, db_path, writable=False))
            if name == "api":
                settings = actual["path_settings"]
                device = settings["WUWATERM_API_DEVICE_DB_PATH"]
                if device is None:
                    state = settings["WUWATERM_API_STATE_DIR"] or posixpath.join(config.get("WorkingDir") or "/", "state-api")
                    device = posixpath.join(state, "devices.db")
                api_identity_path = observe.resolve_bind_path(snapshot, device, writable=True)
                try:
                    info = Path(api_identity_path).lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        _fail("API identity store is not a regular file")
                    api_identity = {"path": api_identity_path, "device": info.st_dev, "inode": info.st_ino}
            observations[name], snapshots[name], images[name] = actual, snapshot, image
        if len(database_paths) != 1:
            _fail("installed components do not share one database")
        database, database_stat = manifest.read_database_snapshot(Path(database_paths.pop()))
        source = recovery.prepare_source(root, commit, tree, compose_path=compose_executable)
        compose_executable = recovery._verify_bound_compose(root, source)
        components: dict[str, Any] = {"api": None, "telegram": None}
        legacy_versions: dict[str, str] = {}
        for name, (raw, _, _) in installed.items():
            components[name] = _adopt_component(root, name, requested[name], images[name],
                                                raw["Config"]["Image"], database, old_commit, legacy_versions, source,
                                                compose_path=compose_executable, predecessor_raw=predecessor_raw)

        def recheck() -> None:
            recovery._verify_clean_source(root, commit, tree)
            if tool_bytes_identity(root) != source["tool_bytes_sha256"]:
                _fail("adoption source tools changed")
            recovery._verify_bootstrap(root, source)
            recovery._verify_capsule(root, source)
            recovery._verify_bound_compose(root, source)
            latest = _project_containers(projects)
            if set(latest) != set(installed):
                _fail("installed component topology changed during adoption")
            for name, (raw, project, service) in latest.items():
                previous, _, _ = installed[name]
                if raw["Id"] != previous["Id"]:
                    _fail("installed container changed during adoption")
                actual = observe.observe_container(
                    raw["Id"], expected_image_id=previous["Image"], project=project, service=service,
                    expected_command=previous["Config"].get("Cmd"),
                    expected_entrypoint=previous["Config"].get("Entrypoint"),
                    env_fingerprint_key=fingerprint_key,
                )
                if actual != observations[name]:
                    _fail("installed container changed during adoption")
            for path, identity in references.items():
                if _compose_input(Path(path)) != identity:
                    _fail("Compose input changed during adoption")
            current_db, current_stat = manifest.read_database_snapshot(Path(database["path"]))
            if current_db != database or current_stat != database_stat:
                _fail("database changed during adoption")
            if api_identity_path is not None:
                try:
                    info = Path(api_identity_path).lstat()
                except FileNotFoundError:
                    identity = None
                else:
                    if not stat.S_ISREG(info.st_mode):
                        _fail("API identity store changed during adoption")
                    identity = {"path": api_identity_path, "device": info.st_dev, "inode": info.st_ino}
                if identity != api_identity:
                    _fail("API identity store changed during adoption")

        recheck()
        for name, snapshot in snapshots.items():
            path, digest = manifest.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            components[name]["recipe"] = recipe
        deployment_id = manifest.new_deployment_id()
        product = {"schema_version": 2, "deployment_id": deployment_id,
                   "deployment_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "operations": {"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                  "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]},
                   "components": components, "database": database, "api_identity": api_identity}
        manifest.validate_manifest(product)
        containers = {name: observations[name]["container"] if name in observations else None
                      for name in ("api", "telegram")}
        target_pointers = {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                           ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}
        product_raw = manifest._encode(product)
        journal = {"schema_version": RUNTIME4_SCHEMA_VERSION, "kind": "component-runtime", "transaction_id": deployment_id,
                   "action": "adopt", "status": "active", "phase": "prepared", "source": source,
                   "predecessor": (None if predecessor is None else
                                   predecessor_reference("runtime", predecessor[1]["transaction_id"], predecessor[0])),
                   "selection": [], "before": {"manifest": None, "topology": copy.deepcopy(components),
                                                 "containers": copy.deepcopy(containers)},
                   "target": {"manifest": product, "containers": copy.deepcopy(containers)},
                   "database": {"binding": database, "stat": database_stat}, "api_identity": api_identity,
                   "migrations": [], "pointers": {"old": old_pointers, "target": target_pointers},
                   "manifest": {"deployment_id": deployment_id,
                                "path": f".deployments/products/{deployment_id}.json",
                                "sha256": hashlib.sha256(product_raw).hexdigest()}}
        recheck()
        current_predecessor = terminal.verify_terminal(root, compose_path=compose_executable)
        if (None if current_predecessor is None else current_predecessor[0]) != predecessor_raw:
            _fail("terminal predecessor changed during adoption preflight")
        transaction.write_initial_journal(root, journal, predecessor_raw=predecessor_raw,
                                          compose_path=compose_executable)
        try:
            journal["phase"] = "immutable-manifest"
            _write_adopt_journal(root, journal)
            manifest.write_manifest(root, product)
            recheck()
            journal["phase"] = "deploy-commit"
            _write_adopt_journal(root, journal)
            _replace_pointer(root, ".deploy_commit", old_pointers[".deploy_commit"], target_pointers[".deploy_commit"])
            recheck()
            journal["phase"] = "deploy-current"
            _write_adopt_journal(root, journal)
            _replace_pointer(root, ".deploy_current", old_pointers[".deploy_current"], target_pointers[".deploy_current"])
            recheck()
            if inspect_deployment(root)["manifest"] != product:
                _fail("adopted metadata readback differs")
            journal.update(status="committed", phase="committed")
            _write_adopt_journal(root, journal)
            return deployment_id
        except Exception as exc:
            try:
                journal["phase"] = "rollback-pointers"
                _write_adopt_journal(root, journal)
                for name in (".deploy_current", ".deploy_commit"):
                    pattern = _DEPLOYMENT_ID if name == ".deploy_current" else _HEX40
                    actual = _snapshot_pointer(root, name, pattern)
                    if actual == old_pointers[name]:
                        continue
                    if actual == target_pointers[name]:
                        _replace_pointer(root, name, actual, old_pointers[name])
                    else:
                        _fail("adoption pointer is no longer a recorded value")
                recheck()
                journal.update(status="rolled_back", phase="rolled-back")
                _write_adopt_journal(root, journal)
            except Exception as rollback_error:
                journal.update(status="unresolved", phase="unresolved")
                try:
                    _write_adopt_journal(root, journal)
                except Exception:
                    pass
                raise JournalError("adoption failed and pointer rollback is unresolved") from rollback_error
            raise JournalError("adoption failed; original pointers were restored") from exc
    except JournalError:
        raise
    except admission.AdmissionError as exc:
        raise JournalError(f"deployment admission refused: {exc}") from exc
    except (recovery.RecoveryError, observe.ObservationError, transaction.TransactionError,
            capsule.CapsuleError, manifest.ManifestError, OSError, UnicodeError, ValueError) as exc:
        raise JournalError("adoption preflight failed") from exc


def refresh_runtime_options(root: Path, *, container_id: str, compose_executable: Path,
                            expected_sha: str | None = None) -> dict[str, Any]:
    """Record API resource options that an earlier adoption record lacks.

    ``container_id`` names the one live API container: either the recorded one
    or the container that replaced it. Everything else must still match the
    record. The change is published as a new metadata-only transaction, so the
    previous manifest, recipe snapshot and journal stay as recorded. No
    container is created, started, stopped or removed.
    """
    import component_observe as observe  # noqa: PLC0415
    import component_recovery as recovery  # noqa: PLC0415
    import component_operations as operations  # noqa: PLC0415
    import component_terminal as terminal_ops  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import package_recovery_source as capsule  # noqa: PLC0415

    root = Path(os.path.abspath(os.fspath(root)))
    try:
        admission.admit_component(root, operation=REFRESH_ACTION, code_root=CODE_ROOT)
        _match(container_id, _HEX64, "container ID")
        compose_executable = operations._compose_binary(compose_executable)
        current = inspect_deployment(root)
        old = current["manifest"]
        if current["binding"] != "recorded" or old is None:
            _fail("an adopted and reconciled deployment is required")
        if old["components"]["api"] is None:
            _fail("the adopted deployment has no API; only the API record can gain these options")
        old_snapshot = operations._snapshot(root, old["components"]["api"])
        actual = operations._observed(container_id, old_snapshot, os.urandom(32))
        # env_files and overrides cannot be observed; terminal verification
        # below compares the rendered environment with the container's. The
        # container name is compared with the recorded container.
        observed_snapshot = {**old_snapshot, **{
            key: value for key, value in actual["recipe"].items()
            if key not in ("env_files", "container_name")}}
        added = operations._resource_additions(old_snapshot, observed_snapshot)
        path, digest = manifest.write_recipe_snapshot(root, observed_snapshot)
        recipe = {key: observed_snapshot[key] for key in manifest._RECIPE_FIELDS}
        recipe.update(snapshot_path=path, sha256=digest)
        terminal = terminal_ops.verify_terminal(
            root, compose_path=compose_executable,
            api_override={"recipe": recipe, "container": actual["container"]})
        if terminal is None:
            _fail("an adopted and reconciled deployment is required")
        predecessor_raw, previous = terminal
        won = "target" if previous["status"] == "committed" else "before"
        winning = previous["target"]["manifest"] if won == "target" else previous["before"]["manifest"]
        if winning != old:
            _fail("current manifest differs from terminal deployment")
        recorded = previous[won]["containers"]["api"]
        fresh = actual["container"]
        differing = [key for key in ("image_id", "project", "service", "name", "running")
                     if fresh[key] != recorded[key]]
        replaced = fresh["id"] != recorded["id"]
        if fresh["transaction_label"] != (None if replaced else recorded["transaction_label"]):
            differing.append("transaction_label")
        if differing:
            _fail("API container differs from the record: " + ", ".join(differing))
        if not added:
            if replaced:
                _fail("the API container was replaced but adds no runtime option; refresh does not rebind it")
            return {"status": "unchanged", "deployment_id": old["deployment_id"]}

        commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
        if expected_sha is not None and _match(expected_sha, _HEX40, "expected source commit") != commit:
            _fail("source commit differs from --expected-sha")
        recovery._load_packager(root / "scripts" / "package_recovery_source.py")
        recovery._verify_clean_source(root, commit, tree)
        source = recovery.prepare_source(root, commit, tree, compose_path=compose_executable)
        compose_executable = recovery._verify_bound_compose(root, source)

        before_containers = copy.deepcopy(previous[won]["containers"])
        if before_containers["telegram"] is not None:
            snapshot = operations._snapshot(root, old["components"]["telegram"])
            before_containers["telegram"] = operations._observed(
                before_containers["telegram"]["id"], snapshot, os.urandom(32))["container"]
        target_containers = copy.deepcopy(before_containers)
        target_containers["api"] = copy.deepcopy(fresh)
        deployment_id = manifest.new_deployment_id()
        product = copy.deepcopy(old)
        product.update(deployment_id=deployment_id,
                       deployment_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                       operations={"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                   "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]})
        product["components"]["api"]["recipe"] = recipe
        manifest.validate_manifest(product)
        old_pointers = {".deploy_commit": _snapshot_pointer(root, ".deploy_commit", _HEX40),
                        ".deploy_current": _snapshot_pointer(root, ".deploy_current", _DEPLOYMENT_ID)}
        target_pointers = {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                           ".deploy_current": {"present": True, "bytes": deployment_id + "\n"}}
        kind = "full" if previous["kind"] == "component-full" else "runtime"
        journal = {
            # The journal schema follows the manifest schema, which stays.
            "schema_version": (RUNTIME4_SCHEMA_VERSION
                               if old["schema_version"] == manifest.PREVIOUS_MANIFEST_SCHEMA_VERSION
                               else JOURNAL_SCHEMA_VERSION),
            "kind": "component-runtime", "transaction_id": deployment_id,
            "action": REFRESH_ACTION, "status": "active", "phase": "prepared", "source": source,
            "predecessor": predecessor_reference(kind, previous["transaction_id"], predecessor_raw),
            "selection": [],
            "before": {"manifest": copy.deepcopy(old), "topology": copy.deepcopy(old["components"]),
                       "containers": before_containers},
            "target": {"manifest": product, "containers": target_containers},
            "database": {"binding": copy.deepcopy(old["database"]),
                         "stat": manifest.verify_database_binding(old["database"])},
            "api_identity": copy.deepcopy(old["api_identity"]), "migrations": [],
            "pointers": {"old": old_pointers, "target": target_pointers},
            "manifest": {"deployment_id": deployment_id,
                         "path": f".deployments/products/{deployment_id}.json",
                         "sha256": hashlib.sha256(manifest._encode(product)).hexdigest()},
        }

        def recheck() -> None:
            recovery._verify_clean_source(root, commit, tree)
            if tool_bytes_identity(root) != source["tool_bytes_sha256"]:
                _fail("refresh source tools changed")
            operations._verify_refresh_live(root, journal, compose_executable)

        transaction.write_initial_journal(root, journal, predecessor_raw=predecessor_raw,
                                          compose_path=compose_executable)
        try:
            journal["phase"] = "immutable-manifest"
            _write_adopt_journal(root, journal)
            recheck()
            manifest.write_manifest(root, product)
            for phase, name in (("deploy-commit", ".deploy_commit"), ("deploy-current", ".deploy_current")):
                journal["phase"] = phase
                _write_adopt_journal(root, journal)
                recheck()
                _replace_pointer(root, name, old_pointers[name], target_pointers[name])
            recheck()
            if inspect_deployment(root)["manifest"] != product:
                _fail("refreshed metadata readback differs")
            journal.update(status="committed", phase="committed")
            _write_adopt_journal(root, journal)
        except Exception as exc:
            try:
                transaction._recover_adoption(root, journal, compose_executable)
            except Exception as rollback_error:
                journal.update(status="unresolved", phase="unresolved")
                try:
                    _write_adopt_journal(root, journal)
                except Exception:
                    pass
                raise JournalError(f"record refresh failed and pointer rollback is unresolved; recover "
                                   f"--transaction-id {deployment_id}") from rollback_error
            raise JournalError("record refresh failed; original pointers were restored") from exc
        return {"status": "refreshed", "deployment_id": deployment_id,
                "added": {key: observed_snapshot["runtime_options"][key] for key in added},
                "container_replaced": replaced, "polling_health": "unconfirmed"}
    except JournalError:
        raise
    except admission.AdmissionError as exc:
        raise JournalError(f"deployment admission refused: {exc}") from exc
    except (operations.TransactionError, observe.ObservationError) as exc:
        # Both carry fixed, nonsecret refusal text; field names, never values.
        raise JournalError(f"record refresh refused: {exc}") from exc
    except (recovery.RecoveryError, capsule.CapsuleError, manifest.ManifestError,
            OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise JournalError("record refresh preflight failed") from exc
