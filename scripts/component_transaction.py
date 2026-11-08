#!/usr/bin/env python3
"""Execute the narrow, retained-container API component transaction.

This module is intentionally not a command-line entry point.  Its caller must
construct the existing runtime2 journal from verified adoption/build records.
Telegram and changed runtime recipes remain refused until their state and
polling contracts are implemented by the coordinator.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import posixpath
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlencode

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_build as build  # noqa: E402
import component_observe as observe  # noqa: E402
import component_recovery as recovery  # noqa: E402
import component_runtime as runtime  # noqa: E402
import component_wheel_build as wheel  # noqa: E402
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402


class TransactionError(RuntimeError):
    """The observed deployment does not permit a safe transaction."""


def _docker(method: str, path: str, body: dict[str, Any] | None = None,
            *, accepted: tuple[int, ...] = (200,)) -> Any:
    """Talk to the local daemon without writing its secret-bearing replies."""
    try:
        if not stat.S_ISSOCK(os.stat(observe.SOCKET, follow_symlinks=False).st_mode):
            raise TransactionError("Docker endpoint is not a socket")
        conn = observe._UnixHTTPConnection("localhost", timeout=30)
        try:
            raw_body = None if body is None else json.dumps(body).encode("utf-8")
            headers = {} if body is None else {"Content-Type": "application/json"}
            conn.request(method, path, body=raw_body, headers=headers)
            response = conn.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if response.status not in accepted or len(raw) > 8 * 1024 * 1024:
                raise TransactionError("Docker operation failed")
            return json.loads(raw) if raw else None
        finally:
            conn.close()
    except TransactionError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise TransactionError("Docker operation failed") from None


def _container_present(container_id: str) -> bool:
    """Distinguish an exact Docker 404 from an unavailable or failed daemon."""
    runtime._match(container_id, runtime._HEX64, "container ID")
    try:
        if not stat.S_ISSOCK(os.stat(observe.SOCKET, follow_symlinks=False).st_mode):
            raise TransactionError("Docker endpoint is not a socket")
        connection = observe._UnixHTTPConnection("localhost", timeout=30)
        try:
            connection.request("GET", "/containers/" + container_id + "/json")
            response = connection.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024 or response.status not in (200, 404):
                raise TransactionError("Docker container presence is unknown")
            return response.status == 200
        finally:
            connection.close()
    except TransactionError:
        raise
    except (OSError, http.client.HTTPException):
        raise TransactionError("Docker container presence is unknown") from None


def _image(image_id: str) -> dict[str, Any]:
    runtime._image_id(image_id, "image")
    result = _docker("GET", "/images/" + quote(image_id, safe=":") + "/json")
    if type(result) is not dict or result.get("Id") != image_id:
        raise TransactionError("image identity differs")
    return result


def _env_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if type(value) is not list:
        raise TransactionError("image or container environment is invalid")
    result: dict[str, str] = {}
    for entry in value:
        if type(entry) is not str or "=" not in entry:
            raise TransactionError("image or container environment is invalid")
        key, val = entry.split("=", 1)
        if not manifest._ENV_NAME.fullmatch(key) or key in result:
            raise TransactionError("image or container environment is ambiguous")
        result[key] = val
    return result


def _compose_binary(path: Path) -> Path:
    """Bind the explicitly selected standalone Compose binary to its receipt."""
    try:
        path = Path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise TransactionError("Compose binary must be a physical absolute path")
        receipt = runtime._read_regular(Path(str(path) + ".sha256")).decode("ascii").strip().split()
        if len(receipt) != 2 or receipt[1].lstrip("*") != path.name:
            raise TransactionError("Compose checksum receipt is invalid")
        if recovery._hash(path) != receipt[0]:
            raise TransactionError("Compose checksum differs")
        return path
    except (OSError, UnicodeError, runtime.JournalError, recovery.RecoveryError):
        raise TransactionError("Compose binary cannot be verified") from None


def _effective_environment(snapshot: dict[str, Any], image: dict[str, Any],
                           compose_path: Path, root: Path) -> dict[str, str]:
    """Render the recorded env references and compare them only in memory."""
    admission.require_lock(root)
    binary = _compose_binary(compose_path)
    preimages = [runtime._read_regular(Path(name)) for name in snapshot["env_files"]]
    environment = {item["name"]: (item.get("value") if item["mode"] == "literal" else "")
                   for item in snapshot["environment_overrides"]}
    document = {"services": {"fixture": {"image": snapshot["image_id"],
                "env_file": snapshot["env_files"], "environment": environment}}}
    try:
        process = subprocess.run(
            [str(binary), "-p", "wuwaterm-component-env", "-f", "-", "--env-file", "/dev/null",
             "config", "--format", "json"], input=json.dumps(document).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=root,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            pass_fds=(9,), timeout=30, check=False,
        )
        if process.returncode or len(process.stdout) > 8 * 1024 * 1024:
            raise TransactionError("recorded environment cannot be rendered")
        rendered = json.loads(process.stdout)["services"]["fixture"].get("environment") or {}
        if type(rendered) is not dict:
            raise TransactionError("recorded environment cannot be rendered")
        result = _env_map(image.get("Config", {}).get("Env"))
        for key, value in rendered.items():
            if not manifest._ENV_NAME.fullmatch(key) or (value is not None and type(value) is not str):
                raise TransactionError("recorded environment is ambiguous")
            if value is None:
                result.pop(key, None)
            else:
                result[key] = value
        if [runtime._read_regular(Path(name)) for name in snapshot["env_files"]] != preimages:
            raise TransactionError("recorded environment changed during rendering")
        return result
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError, runtime.JournalError):
        raise TransactionError("recorded environment cannot be rendered") from None


def _snapshot(root: Path, component: dict[str, Any]) -> dict[str, Any]:
    recipe = component["recipe"]
    snap = manifest.read_recipe_snapshot(root, recipe["sha256"])
    if snap["image_id"] != component["image"]["id"] or any(
        snap[key] != recipe[key] for key in manifest._RECIPE_FIELDS
    ):
        raise TransactionError("recipe snapshot differs from component")
    return snap


def _observed(container_id: str, snapshot: dict[str, Any], key: bytes) -> dict[str, Any]:
    return observe.observe_container(
        container_id, expected_image_id=snapshot["image_id"], project=snapshot["project"],
        service=snapshot["service"], expected_command=snapshot["command"],
        expected_entrypoint=snapshot["entrypoint"], env_fingerprint_key=key,
    )


def _recipe_matches(actual: dict[str, Any], snapshot: dict[str, Any]) -> None:
    # The retained container has a deterministic temporary name.  All other
    # observed settings must still match its immutable recipe.
    keys = (set(manifest._RECIPE_FIELDS) - {"env_files", "container_name"}) | {"runtime_options"}
    if any(actual["recipe"][key] != snapshot[key] for key in keys):
        raise TransactionError("observed container recipe differs")


def _retained_name(journal: dict[str, Any], component: str = "api") -> str:
    return "wuwaterm-rollback-" + journal["transaction_id"] + "-" + component


def _target_name(journal: dict[str, Any], snapshot: dict[str, Any], component: str = "api") -> str:
    return snapshot["container_name"] or "wuwaterm-target-" + journal["transaction_id"] + "-" + component


def _inventory(project: str, service: str) -> list[dict[str, Any]]:
    filters = json.dumps({"label": ["com.docker.compose.project=" + project,
                                     "com.docker.compose.service=" + service]})
    result = _docker("GET", "/containers/json?" + urlencode({"all": "1", "filters": filters}))
    if type(result) is not list or any(type(item) is not dict for item in result):
        raise TransactionError("Docker inventory is invalid")
    return result


def _verify_data(journal: dict[str, Any], observations: list[dict[str, Any]]) -> None:
    if manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
        raise TransactionError("database stat changed")
    identity = journal["api_identity"]
    if identity is not None:
        path = Path(identity["path"])
        # Reuse the no-symlink walk used by the observation helper.
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
            raise TransactionError("API identity changed")
    for observed in observations:
        settings = observed["path_settings"]
        db_path = settings["WUWATERM_DB_PATH"]
        if db_path is None or observe.resolve_bind_path(observed["recipe"], db_path, writable=False) != journal["database"]["binding"]["path"]:
            raise TransactionError("database mount does not match")
        if identity is not None:
            device = settings["WUWATERM_API_DEVICE_DB_PATH"]
            state = settings["WUWATERM_API_STATE_DIR"]
            if device is None:
                if state is None:
                    raise TransactionError("API identity path is not explicit")
                device = state.rstrip("/") + "/devices.db"
            if observe.resolve_bind_path(observed["recipe"], device, writable=True) != identity["path"]:
                raise TransactionError("API identity mount does not match")


def _pointer(root: Path, name: str) -> dict[str, Any]:
    pattern = runtime._HEX40 if name == ".deploy_commit" else runtime._DEPLOYMENT_ID
    value = runtime._read_pointer(root, name, pattern)
    return {"present": value is not None, "bytes": None if value is None else value + "\n"}


def _check_pointers(root: Path, journal: dict[str, Any], *, old_only: bool = False) -> None:
    for name in (".deploy_commit", ".deploy_current"):
        value = _pointer(root, name)
        permitted = [journal["pointers"]["old"][name]]
        if not old_only:
            permitted.append(journal["pointers"]["target"][name])
        if value not in permitted:
            raise TransactionError("deployment pointer drift")


def _exact_pointers(root: Path, journal: dict[str, Any], which: str) -> None:
    for name in (".deploy_commit", ".deploy_current"):
        if _pointer(root, name) != journal["pointers"][which][name]:
            raise TransactionError("deployment pointers differ from the terminal preimage")


def _verify_saved_source(root: Path, source: dict[str, Any]) -> None:
    """Verify the saved source and its own tool fingerprint without B imports."""
    recovery._validate_source(source)
    recovery._verify_bootstrap(root, source)
    if "compose" in source:
        recovery._verify_bound_compose(root, source)
    recovery._load_packager(CODE_ROOT / "scripts" / "package_recovery_source.py")
    lock = recovery._lock(root)
    with tempfile.TemporaryDirectory(prefix="wuwaterm-terminal-source-") as temporary:
        extracted = Path(temporary)
        recovery._verify_capsule(root, source, extracted)
        recovery._verify_tools(extracted, source, lock)
    recovery._verify_capsule(root, source)
    recovery._verify_bootstrap(root, source)


def _require_prepared_source(root: Path, source: dict[str, Any], compose_path: Path) -> None:
    """Recheck prepared bytes and require the recorded Compose for new journals."""
    base = {key: value for key, value in source.items() if key != "compose"}
    if recovery.prepare_source(root, source["commit"], source["tree"]) != base:
        raise TransactionError("saved recovery source differs")
    if "compose" in source and recovery._verify_bound_compose(root, source) != compose_path:
        raise TransactionError("Compose executable differs from the prepared transaction")


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Producer receipts predate the per-transaction Compose binding.
    return ({key: value for key, value in left.items() if key != "compose"}
            == {key: value for key, value in right.items() if key != "compose"})


def _verify_manifest_if_present(root: Path, journal: dict[str, Any], *, required: bool) -> None:
    path = root / journal["manifest"]["path"]
    products = root / ".deployments" / "products"
    try:
        directory = products.lstat()
    except FileNotFoundError:
        if required:
            raise TransactionError("terminal product directory is missing") from None
        return
    if not stat.S_ISDIR(directory.st_mode):
        raise TransactionError("terminal product directory is not a directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise TransactionError("terminal target manifest is missing") from None
        return
    if not stat.S_ISREG(info.st_mode):
        raise TransactionError("terminal target manifest is not a regular file")
    if (manifest.read_manifest(root, journal["transaction_id"]) != journal["target"]["manifest"]
            or manifest.manifest_sha256(root, journal["transaction_id"]) != journal["manifest"]["sha256"]):
        raise TransactionError("terminal target manifest differs")


def _require_target_manifest_absent(root: Path, journal: dict[str, Any]) -> None:
    products = root / ".deployments" / "products"
    try:
        info = products.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise TransactionError("product manifest directory is not a directory")
    try:
        (root / journal["manifest"]["path"]).lstat()
    except FileNotFoundError:
        return
    raise TransactionError("target manifest path already exists")


def _telegram_database_path(actual: dict[str, Any], snapshot: dict[str, Any]) -> str:
    database = actual["path_settings"]["WUWATERM_DB_PATH"]
    command = snapshot["command"]
    if command and command[:1] == ["--db"]:
        database = command[1]
    if database is None:
        raise TransactionError("Telegram database path is not explicit")
    return database


def _telegram_container_paths(actual: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, str]:
    settings = actual["path_settings"]
    database = _telegram_database_path(actual, snapshot)
    state_dir = settings["WUWATERM_STATE_DIR"]
    result = {}
    for kind, filename, variable in (
        ("chat-settings", "chat_settings.json", "WUWATERM_SETTINGS_PATH"),
        ("channel-replies", "channel_replies.json", "WUWATERM_CHANNEL_REPLY_INDEX_PATH"),
    ):
        legacy = posixpath.join(posixpath.dirname(database), filename)
        target = posixpath.join(state_dir, filename) if state_dir is not None else None
        explicit = settings[variable]
        effective = (target if target is not None and (explicit is None or explicit in (legacy, target))
                     else explicit or legacy)
        result[kind] = effective
    return result


def _telegram_effective_paths(actual: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, str]:
    return {kind: observe.resolve_bind_path(actual["recipe"], path, writable=True)
            for kind, path in _telegram_container_paths(actual, snapshot).items()}


def _telegram_full4_entry_paths(actual: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, str]:
    """Map only leaf entries; FULL4's native endpoint checks own link resolution."""
    import component_state_endpoint as endpoint  # noqa: PLC0415

    if Path(endpoint.__file__).resolve() != CODE_ROOT / "scripts/component_state_endpoint.py":
        raise TransactionError("FULL4 endpoint mapper belongs to another source")
    try:
        paths = {kind: endpoint.map_entry(actual["recipe"], path, writable=True)
                 for kind, path in _telegram_container_paths(actual, snapshot).items()}
        for value in paths.values():
            try:
                info = Path(value).lstat()
            except FileNotFoundError:
                continue
            if (info.st_nlink != 1 or not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))
                    or stat.S_ISLNK(info.st_mode) and not 0 < info.st_size <= 4095):
                raise TransactionError("FULL4 Telegram state entry is unsafe")
        return paths
    except (endpoint.EndpointError, OSError) as exc:
        raise TransactionError("FULL4 Telegram state entry cannot be mapped") from exc


def _telegram_v5_paths(actual: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map each configured state entry the way a schema-5 plan reports it.

    A settings entry resolves its leaf because the product rewrites the file it
    names; a reply entry stays an entry because the product replaces it, so a
    retained link is the recorded path and only its referent is read.
    """
    state = _state_module()
    recipe = actual["recipe"]
    result: dict[str, dict[str, str]] = {}
    for kind, container in _telegram_container_paths(actual, snapshot).items():
        try:
            entry = state.entry_path(recipe, container, writable=True)
            planned = (state.leaf_host_path(recipe, entry) if kind == "chat-settings" else entry)
        except state.StateError as exc:
            raise TransactionError(str(exc) or "Telegram state entry cannot be mapped") from exc
        result[kind] = {"entry": entry, "planned": planned}
    return result


def _same_container_binding(actual: dict[str, Any], recorded: dict[str, Any]) -> bool:
    return all(actual[key] == recorded[key] for key in (
        "id", "image_id", "project", "service", "name", "transaction_label",
    ))


def _recorded_state_items(journal: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Return recorded state items and their target-path key for either shape.

    A runtime record keeps a list of migrations; a FULL4/FULL5 record keeps
    ``{"status", "items"}`` and names the target as ``new_target_path``.
    """
    migrations = journal["migrations"]
    if type(migrations) is dict:
        return list(migrations.get("items") or []), "new_target_path"
    return list(migrations), "target_path"


def _verify_current_telegram_paths(journal: dict[str, Any], actual: dict[str, Any],
                                   snapshot: dict[str, Any]) -> None:
    items, key = _recorded_state_items(journal)
    if journal.get("schema_version") == 5:
        # Schema 5 records the planned path, which is the referent of a
        # settings link and the entry of a retained reply link.
        paths = _telegram_v5_paths(actual, snapshot)
        if items and any(paths[item["kind"]]["planned"] != item[key] for item in items):
            raise TransactionError("terminal Telegram effective state path changed")
        for value in paths.values():
            _state_entry_safe(value["entry"])
        return
    paths = _telegram_effective_paths(actual, snapshot)
    if items and any(paths[item["kind"]] != item[key] for item in items):
        raise TransactionError("terminal Telegram effective state path changed")
    for value in paths.values():
        target = Path(value)
        with manifest._directory(target.parent, (), create=False):
            pass
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TransactionError("terminal Telegram state path is unsafe")


def _state_entry_safe(entry: str) -> None:
    """Require an absent, regular or single retained link state entry."""
    target = Path(entry)
    try:
        parent = target.parent.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(parent.st_mode):
        raise TransactionError("Telegram state entry parent is not a directory")
    with manifest._directory(target.parent, (), create=False):
        pass
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        if info.st_nlink != 1 or not 0 < info.st_size <= 4095:
            raise TransactionError("Telegram state entry link is unsafe")
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TransactionError("Telegram state entry is unsafe")


def _verify_adoption_telegram_paths(actual: dict[str, Any], snapshot: dict[str, Any], *,
                                    terminal: bool) -> None:
    """Admit existing leaf links for metadata only; FULL4 verifies their targets."""
    for container_path in _telegram_container_paths(actual, snapshot).values():
        try:
            value = observe.resolve_bind_path(actual["recipe"], container_path, writable=True)
        except observe.ObservationError as strict_error:
            # A mapping failure is not itself evidence of an admissible link.
            # Independently validate the original writable bind and physical parent.
            import component_state_endpoint as endpoint  # noqa: PLC0415

            if Path(endpoint.__file__).resolve() != CODE_ROOT / "scripts/component_state_endpoint.py":
                raise TransactionError("adoption endpoint mapper belongs to another source")
            try:
                value = endpoint.map_entry(actual["recipe"], container_path, writable=True)
                info = Path(value).lstat()
            except (endpoint.EndpointError, OSError) as exc:
                raise TransactionError("adoption Telegram state entry cannot be mapped") from exc
            if not stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 4095:
                raise TransactionError("adoption Telegram state entry is not a safe leaf link") from strict_error
            continue
        target = Path(value)
        if terminal:
            with manifest._directory(target.parent, (), create=False):
                pass
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or terminal and info.st_nlink != 1:
            raise TransactionError("adoption Telegram state entry is unsafe")


def _verify_full4_telegram_paths(journal: dict[str, Any], actual: dict[str, Any],
                                 snapshot: dict[str, Any]) -> None:
    paths = _telegram_full4_entry_paths(actual, snapshot)
    migrations = journal["migrations"]
    if type(migrations) is list and migrations and any(paths[item["kind"]] != item["target_path"] for item in migrations):
        raise TransactionError("terminal Telegram effective state entry changed")


def _verify_live_topology(root: Path, journal: dict[str, Any], *, which: str,
                          compose_path: Path, allow_selected_restart: bool = False,
                          allow_retained: bool = False, allow_runtime_drift: bool = False,
                          full4_endpoints: bool = False, _full4_runtime_predecessor: bool = False) -> None:
    """Check recorded containers and supported bindings with no mutations."""
    if type(full4_endpoints) is not bool or type(_full4_runtime_predecessor) is not bool:
        raise TransactionError("invalid FULL4 route selection")
    if _full4_runtime_predecessor and not full4_endpoints:
        raise TransactionError("FULL4 predecessor route is inconsistent")
    if full4_endpoints and not (
        journal.get("kind") == "component-full" and journal.get("schema_version") == 4
        or _full4_runtime_predecessor and journal.get("kind") == "component-runtime"
        and journal.get("schema_version") in (2, 3, 4)
    ):
        raise TransactionError("FULL4 endpoint route requires an admitted FULL4 context")
    adoption = (journal.get("kind") == "component-runtime" and journal.get("schema_version") == 4
                and journal.get("action") == "adopt")
    if adoption:
        runtime.validate_journal(journal)
        adoption = journal["selection"] == [] and journal["migrations"] == []
    if which == "target":
        topology = journal["target"]["manifest"]["components"]
        records = journal["target"]["containers"]
    else:
        topology = journal["before"]["topology"]
        records = journal["before"]["containers"]
    key = os.urandom(32)
    required: dict[tuple[str, str], set[str]] = {}
    optional: dict[tuple[str, str], dict[str, str]] = {}
    api = None
    for name in ("api", "telegram"):
        component, expected = topology[name], records[name]
        if component is None:
            continue
        snapshot = _snapshot(root, component)
        scope = (snapshot["project"], snapshot["service"])
        required.setdefault(scope, set()).add(expected["id"])
        actual = _observed(expected["id"], snapshot, key)
        _recipe_matches(actual, snapshot)
        record = actual["container"]
        comparable = dict(expected)
        if allow_selected_restart and name in journal["selection"] and expected["running"]:
            comparable["started_at"] = record["started_at"]
        if (not _same_container_binding(record, expected) if allow_runtime_drift else record != comparable):
            raise TransactionError("terminal container identity or running state changed")
        image = _image(snapshot["image_id"])
        if (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != component["image"]["revision"]:
            raise TransactionError("terminal image revision changed")
        if (_effective_environment(snapshot, image, compose_path, root) !=
                _env_map(observe._inspect(expected["id"])["Config"].get("Env"))):
            raise TransactionError("terminal environment no longer matches its references")
        if name == "api":
            api = actual
        else:
            database = _telegram_database_path(actual, snapshot)
            if observe.resolve_bind_path(actual["recipe"], database, writable=False) != journal["database"]["binding"]["path"]:
                raise TransactionError("terminal Telegram database binding changed")
            if full4_endpoints:
                _verify_full4_telegram_paths(journal, actual, snapshot)
            elif adoption:
                _verify_adoption_telegram_paths(actual, snapshot, terminal=allow_runtime_drift)
            elif allow_runtime_drift:
                _verify_current_telegram_paths(journal, actual, snapshot)
            elif journal["migrations"]:
                _telegram_state_bindings(journal, [actual])
            elif journal.get("schema_version") == 5:
                _verify_current_telegram_paths(journal, actual, snapshot)
            else:
                _telegram_effective_paths(actual, snapshot)
        if allow_retained and name in journal["selection"]:
            previous = journal["before"]["containers"][name]
            previous_component = journal["before"]["topology"][name]
            previous_snapshot = _snapshot(root, previous_component)
            old_scope = (previous_snapshot["project"], previous_snapshot["service"])
            optional.setdefault(old_scope, {})[previous["id"]] = name
    if which == "before" and journal["action"] == "update":
        for name in journal["selection"]:
            component = journal["target"]["manifest"]["components"][name]
            snapshot = _snapshot(root, component)
            required.setdefault((snapshot["project"], snapshot["service"]), set())
    for scope in set(required) | set(optional):
        ids = {item.get("Id") for item in _inventory(*scope)}
        mandatory, possible = required.get(scope, set()), optional.get(scope, {})
        if not mandatory.issubset(ids) or not ids.issubset(mandatory | set(possible)):
            raise TransactionError("terminal container inventory changed")
        for identifier in ids - mandatory:
            name = possible[identifier]
            before = journal["before"]["containers"][name]
            snapshot = _snapshot(root, journal["before"]["topology"][name])
            actual = _observed(identifier, snapshot, key)
            _recipe_matches(actual, snapshot)
            if actual["container"] != {**before, "name": _retained_name(journal, name), "running": False}:
                raise TransactionError("terminal retained container differs")
    if api is None:
        if manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
            raise TransactionError("terminal database changed")
    else:
        _verify_data(journal, [api])
    if which == "before" and journal["action"] == "update":
        for name in journal["selection"]:
            target = journal["target"]["containers"][name]
            if target["id"] is not None and _container_present(target["id"]):
                raise TransactionError("terminal rollback still has a target container")


def _full4_record_route(journal: dict[str, Any], *, for_full4: bool) -> bool:
    """Decide whether a terminal record is read through FULL4's entry mapper.

    FULL4 admitted that separate mapper for the schema-2..4 records it could
    inherit, whose Telegram entries may still be the product's own leaf links.
    A schema-5 record carries its own native leaf checks, so asking for the
    FULL4 route there would demand an admission the record never recorded.
    """
    if type(for_full4) is not bool:
        raise TransactionError("invalid FULL4 route selection")
    return for_full4 and journal.get("schema_version") != runtime.JOURNAL_SCHEMA_VERSION


def _verify_terminal_record(root: Path, journal: dict[str, Any], raw: bytes, compose_path: Path, *,
                            full4_predecessor: bool = False) -> None:
    if journal["status"] not in ("committed", "rolled_back"):
        raise TransactionError("runtime transaction is not terminal")
    if journal["schema_version"] in (4, 5):
        import component_full as full  # noqa: PLC0415
        full.verify_predecessor(root, successor_kind="runtime", successor=journal)
    _verify_saved_source(root, journal["source"])
    _verify_old_topology(root, journal, compose_path=compose_path, allow_probe=False,
                         require_live_image=not (journal["status"] == "committed" and journal["action"] == "update"))
    committed = journal["status"] == "committed"
    which = "target" if committed else "before"
    _exact_pointers(root, journal, "target" if committed else "old")
    if journal["before"]["manifest"] is not None:
        previous = journal["before"]["manifest"]
        if manifest.read_manifest(root, previous["deployment_id"]) != previous:
            raise TransactionError("terminal prior manifest differs")
    _verify_manifest_if_present(root, journal, required=committed)
    endpoints = _full4_record_route(journal, for_full4=full4_predecessor)
    _verify_live_topology(root, journal, which=which, compose_path=compose_path,
                          allow_selected_restart=not committed and journal["action"] == "update",
                          allow_retained=committed and journal["action"] == "update", allow_runtime_drift=True,
                          full4_endpoints=endpoints, _full4_runtime_predecessor=endpoints)
    if committed and journal["action"] == "update":
        if "api" in journal["selection"]:
            _verify_candidate(root, journal)
        if "telegram" in journal["selection"]:
            _telegram_receipt(root, journal)
    _exact_pointers(root, journal, "target" if committed else "old")
    path = root / ".deployments" / runtime.JOURNAL_NAME
    if runtime._read_regular(path) != raw:
        raise TransactionError("terminal journal changed during verification")


def _terminal_health(root: Path, journal: dict[str, Any]) -> dict[str, str]:
    """Check current running targets after probe-free terminal admission."""
    committed = journal["status"] == "committed"
    topology = journal["target"]["manifest"]["components"] if committed else journal["before"]["topology"]
    records = journal["target"]["containers"] if committed else journal["before"]["containers"]
    health = {"api": "absent", "telegram": "absent"}
    for name in ("api", "telegram"):
        if topology[name] is None:
            continue
        snapshot = _snapshot(root, topology[name])
        current = _observed(records[name]["id"], snapshot, os.urandom(32))
        health[name] = "unconfirmed"
        if not current["container"]["running"]:
            continue
        if name == "api":
            _ready(records[name]["id"])
        elif committed and journal["action"] == "update" and name in journal["selection"]:
            _telegram_ready(records[name]["id"], snapshot)
        else:
            continue
        after = _observed(records[name]["id"], snapshot, os.urandom(32))
        if after["container"] != current["container"]:
            raise TransactionError("current component changed during readiness")
        if name == "api":
            _verify_data(journal, [after])
        health[name] = "ready"
    return health


def _predecessor_health(root: Path, journal: dict[str, Any]) -> dict[str, str]:
    if journal["kind"] == "component-full":
        import component_full as full  # noqa: PLC0415
        return full.terminal_health(root, journal)
    return _terminal_health(root, journal)


def verify_terminal(root: Path, *, compose_path: Path) -> tuple[bytes, dict[str, Any]] | None:
    """Read and reconcile a terminal journal without creating state probes or writes."""
    return _verify_terminal(root, compose_path=compose_path, full4_predecessor=False)


def verify_terminal_for_full4(root: Path, *, compose_path: Path) -> tuple[bytes, dict[str, Any]] | None:
    """Admit a predecessor to a FULL successor's terminal verification.

    Runtime records themselves retain their original schema and strict default
    verifier. This opt-in only changes the leaf mapper a FULL successor needs
    for a schema-2..4 record; a schema-5 record keeps its own native leaf
    checks, and either kind must still verify complete endpoints before state
    mutation.
    """
    return _verify_terminal(root, compose_path=compose_path, full4_predecessor=True)


def _verify_terminal(root: Path, *, compose_path: Path,
                     full4_predecessor: bool) -> tuple[bytes, dict[str, Any]] | None:
    root = admission._physical_root(root)
    if root != CODE_ROOT:
        raise TransactionError("terminal verification requires the physical source checkout")
    admission.require_lock(root)
    path = root / ".deployments" / runtime.JOURNAL_NAME
    full_path = root / ".deployments" / admission.COMPONENT_FULL
    if admission._present(full_path):
        if admission._present(path):
            raise TransactionError("cross-kind handoff requires recovery")
        try:
            import component_full as full  # noqa: PLC0415
            if Path(full.__file__).resolve() != CODE_ROOT / "scripts/component_full.py":
                raise TransactionError("full terminal module belongs to another source")
            return full.verify_terminal(root, compose_path=compose_path)
        except full.FullError as exc:
            raise TransactionError("full terminal deployment cannot be verified") from exc
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        raw = runtime._read_regular(path)
        journal = runtime.validate_journal(runtime._decode(raw))
        _verify_terminal_record(root, journal, raw, compose_path, full4_predecessor=full4_predecessor)
        return raw, journal
    except TransactionError:
        raise
    except Exception as exc:
        raise TransactionError("terminal deployment cannot be verified") from exc


def _verify_initial_preimage(root: Path, journal: dict[str, Any], compose_path: Path,
                             predecessor: dict[str, Any] | None, predecessor_raw: bytes | None) -> None:
    if (journal["status"] != "active" or journal["phase"] != "prepared"
            or journal["action"] not in ("adopt", "update")):
        raise TransactionError("successor journal is not prepared")
    if journal["action"] == "update" and any(
        journal["before"]["topology"][name] is None or journal["target"]["containers"][name]["id"] is not None
        for name in journal["selection"]
    ):
        raise TransactionError("successor selection is absent or already activated")
    previous_kind = "full" if predecessor is not None and predecessor.get("kind") == "component-full" else "runtime"
    if journal["schema_version"] in (4, 5):
        expected = (None if predecessor is None or predecessor_raw is None else
                    runtime.predecessor_reference(previous_kind, predecessor["transaction_id"], predecessor_raw))
        if journal["predecessor"] != expected:
            raise TransactionError("successor predecessor binding differs")
    if predecessor is not None:
        if journal["action"] == "adopt" and predecessor["status"] == "committed":
            raise TransactionError("deployment is already adopted")
        committed = predecessor["status"] == "committed"
        prior_manifest = (predecessor["target"]["manifest"] if committed else predecessor["before"]["manifest"])
        prior_topology = (predecessor["target"]["manifest"]["components"] if committed else
                          predecessor["before"]["topology"])
        prior_containers = predecessor["target"]["containers"] if committed else predecessor["before"]["containers"]
        prior_pointers = predecessor["pointers"]["target" if committed else "old"]
        if journal["schema_version"] in (4, 5) and prior_manifest is None:
            import component_full as full  # noqa: PLC0415
            if Path(full.__file__).resolve() != CODE_ROOT / "scripts/component_full.py":
                raise TransactionError("full handoff module belongs to another source")
            try:
                full._verify_null_manifest_handoff(successor_kind="runtime", successor=journal,
                                                    predecessor_kind=previous_kind, predecessor=predecessor)
            except (full.FullError, runtime.JournalError) as exc:
                raise TransactionError("successor preimage differs from the terminal deployment") from exc
        else:
            if (journal["before"]["manifest"] != prior_manifest or journal["before"]["topology"] != prior_topology
                    or journal["pointers"]["old"] != prior_pointers):
                raise TransactionError("successor preimage differs from the terminal deployment")
            for name in ("api", "telegram"):
                old, fresh = prior_containers[name], journal["before"]["containers"][name]
                if (old is None) != (fresh is None) or old is not None and old["id"] != fresh["id"]:
                    raise TransactionError("successor container differs from the terminal deployment")
    recovery._load_packager(CODE_ROOT / "scripts" / "package_recovery_source.py")
    recovery._verify_clean_source(root, journal["source"]["commit"], journal["source"]["tree"])
    if runtime.tool_bytes_identity(CODE_ROOT) != journal["source"]["tool_bytes_sha256"]:
        raise TransactionError("successor tool bytes differ")
    _verify_saved_source(root, journal["source"])
    if journal["before"]["manifest"] is not None:
        before = journal["before"]["manifest"]
        if manifest.read_manifest(root, before["deployment_id"]) != before:
            raise TransactionError("successor prior manifest differs")
    _verify_old_topology(root, journal, compose_path=compose_path, predecessor_raw=predecessor_raw, allow_probe=False)
    if journal["action"] == "update":
        if "api" in journal["selection"]:
            _verify_candidate(root, journal)
        if "telegram" in journal["selection"]:
            _telegram_receipt(root, journal)
    _require_target_manifest_absent(root, journal)
    _exact_pointers(root, journal, "old")
    _verify_live_topology(root, journal, which="before", compose_path=compose_path)


def _publish_initial_runtime_journal(root: Path, directory: int, raw: bytes) -> None:
    """Publish the initial runtime marker as one fully synced link under FD9."""
    admission.require_lock(root)
    with manifest._directory(root, (".deployments",), create=False) as named:
        first, second = os.fstat(directory), os.fstat(named)
        if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
            raise TransactionError("runtime journal publication directory differs")
        manifest._publish_unnamed(directory, runtime.JOURNAL_NAME, raw, mode=0o600)


def write_initial_journal(root: Path, successor: dict[str, Any], *, predecessor_raw: bytes | None,
                          compose_path: Path) -> None:
    """Atomically publish a verified successor, retaining its terminal predecessor."""
    try:
        root = admission._physical_root(root)
        if root != CODE_ROOT:
            raise TransactionError("successor source and deployment roots differ")
        admission.require_lock(root)
        journal = runtime.validate_journal(copy.deepcopy(successor))
        admission._component_conflicts(root)
        terminal = verify_terminal(root, compose_path=compose_path)
        if (terminal is None) != (predecessor_raw is None):
            raise TransactionError("terminal predecessor presence changed")
        if terminal is not None and terminal[0] != predecessor_raw:
            raise TransactionError("terminal predecessor bytes changed")
        previous = None if terminal is None else terminal[1]
        if previous is not None:
            _predecessor_health(root, previous)
            checked = verify_terminal(root, compose_path=compose_path)
            if checked is None or checked[0] != predecessor_raw:
                raise TransactionError("terminal predecessor changed during readiness")
        _verify_initial_preimage(root, journal, compose_path, previous, predecessor_raw)
        raw = manifest._encode(journal)
        with manifest._directory(root, (".deployments",), create=False) as directory:
            if previous is not None:
                if previous["transaction_id"] == journal["transaction_id"]:
                    raise TransactionError("transaction ID was already used")
                previous_kind = "full" if previous["kind"] == "component-full" else "runtime"
                history_name = f"component-{previous_kind}-history"
                previous_name = admission.COMPONENT_FULL if previous_kind == "full" else runtime.JOURNAL_NAME
                with manifest._directory(root, (".deployments", history_name), create=True) as history:
                    manifest._write_file(history, previous["transaction_id"] + ".json", predecessor_raw, reuse=True)
                checked = verify_terminal(root, compose_path=compose_path)
                if checked is None or checked[0] != predecessor_raw:
                    raise TransactionError("terminal predecessor changed after archival")
                _predecessor_health(root, previous)
                _verify_initial_preimage(root, journal, compose_path, previous, predecessor_raw)
                if runtime._read_regular(root / ".deployments" / previous_name) != predecessor_raw:
                    raise TransactionError("terminal predecessor bytes changed before replacement")
            if previous is None or previous["kind"] == "component-full":
                # A named hardlink temporarily gives the authoritative record
                # nlink=2. O_TMPFILE links the fully synced inode exactly once.
                _publish_initial_runtime_journal(root, directory, raw)
            else:
                temporary = ".component-journal-" + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME) != predecessor_raw:
                        raise TransactionError("terminal predecessor bytes changed during replacement")
                    os.replace(temporary, runtime.JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            if previous is not None and previous["kind"] == "component-full":
                import component_full as full  # noqa: PLC0415
                if runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME) != raw:
                    raise TransactionError("runtime successor readback differs")
                full.verify_handoff(root, successor_kind="runtime", successor=journal,
                                    predecessor=previous, predecessor_raw=predecessor_raw)
                if runtime._read_regular(root / ".deployments" / admission.COMPONENT_FULL) != predecessor_raw:
                    raise TransactionError("full predecessor changed before handoff removal")
                os.unlink(admission.COMPONENT_FULL, dir_fd=directory)
                os.fsync(directory)
        if runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME) != raw:
            raise TransactionError("successor journal readback differs")
    except TransactionError:
        raise
    except Exception as exc:
        raise TransactionError("initial runtime journal cannot be published") from exc


def _replace_pointer(root: Path, name: str, desired: dict[str, Any], allowed: list[dict[str, Any]]) -> None:
    admission.require_lock(root)
    if _pointer(root, name) not in allowed:
        raise TransactionError("deployment pointer drift")
    with manifest._directory(root, (), create=False) as directory:
        if not desired["present"]:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.fsync(directory)
            return
        temporary = ".component-pointer-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(desired["bytes"].encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            if _pointer(root, name) not in allowed:
                raise TransactionError("deployment pointer drift")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _write_journal(root: Path, journal: dict[str, Any]) -> None:
    admission.require_lock(root)
    runtime.validate_journal(journal)
    raw = (json.dumps(journal, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    with manifest._directory(root, (".deployments",), create=False) as directory:
        name = runtime.JOURNAL_NAME
        temporary = ".component-journal-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            current = runtime.load_journal(root / ".deployments" / name)
            if current["transaction_id"] != journal["transaction_id"]:
                raise TransactionError("journal identity changed")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TransactionError("journal path changed")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _step(root: Path, journal: dict[str, Any], phase: str, *, status: str = "active") -> None:
    journal["phase"], journal["status"] = phase, status
    _write_journal(root, journal)


def _container_command(method: str, container_id: str, operation: str) -> None:
    runtime._match(container_id, runtime._HEX64, "container ID")
    _docker(method, "/containers/" + container_id + "/" + operation, accepted=(204, 304))


def _ready(container_id: str) -> None:
    # The probe reads only the local API and inherits the same lock FD9.
    code = ("import os,urllib.request; b=(os.getenv('WUWATERM_API_BIND') or '127.0.0.1').strip().strip('[]'); "
            "h=f'[{b}]' if ':' in b else b; p=int(os.getenv('WUWATERM_API_PORT') or '8788'); "
            "r=urllib.request.urlopen(f'http://{h}:{p}/readyz',timeout=2); "
            "assert r.status==200")
    deadline = time.monotonic() + 45
    while True:
        try:
            result = subprocess.run(["docker", "exec", container_id, "python", "-B", "-c", code],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    pass_fds=(9,), timeout=4, check=False)
            if result.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= deadline:
            raise TransactionError("API readiness did not succeed")
        time.sleep(0.5)


def _other_component(root: Path, journal: dict[str, Any], key: bytes, *, terminal: bool = False) -> None:
    component = journal["before"]["topology"]["telegram"]
    if component is None:
        return
    expected = journal["before"]["containers"]["telegram"]
    snap = _snapshot(root, component)
    found = _inventory(snap["project"], snap["service"])
    if len(found) != 1 or found[0].get("Id") != expected["id"]:
        raise TransactionError("unselected component inventory changed")
    actual = _observed(expected["id"], snap, key)
    _recipe_matches(actual, snap)
    if (not _same_container_binding(actual["container"], expected) if terminal else actual["container"] != expected):
        raise TransactionError("unselected component changed")
    db = _telegram_database_path(actual, snap)
    if observe.resolve_bind_path(actual["recipe"], db, writable=False) != journal["database"]["binding"]["path"]:
        raise TransactionError("unselected database mount changed")


def _actual(root: Path, journal: dict[str, Any], compose_path: Path, *,
            initial: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Reconcile the recorded API with the actual old and target containers."""
    key = os.urandom(32)
    before = journal["before"]["containers"]["api"]
    old_snap = _snapshot(root, journal["before"]["topology"]["api"])
    new_snap = _snapshot(root, journal["target"]["manifest"]["components"]["api"])
    if old_snap["project"] != new_snap["project"] or old_snap["service"] != new_snap["service"]:
        raise TransactionError("API project or service changed")
    if any(old_snap[key] != new_snap[key] for key in old_snap if key != "image_id"):
        raise TransactionError("API recipe changes are not supported by this transaction")
    old = _observed(before["id"], old_snap, key)
    _recipe_matches(old, old_snap)
    old_record = old["container"]
    for field in ("id", "image_id", "project", "service", "transaction_label", "restart_count"):
        if old_record[field] != before[field]:
            raise TransactionError("retained API identity changed")
    if old_record["name"] not in (before["name"], _retained_name(journal)):
        raise TransactionError("retained API name changed")
    if (old_record["started_at"] != before["started_at"] and
            journal["phase"] not in ("rollback-restore", "rollback-pointers", "rolled-back")):
        raise TransactionError("retained API start time changed")
    if initial and old_record != before:
        raise TransactionError("initial API state changed")
    old_raw = observe._inspect(before["id"])
    old_image = _image(old_snap["image_id"])
    new_image = _image(new_snap["image_id"])
    expected_revision = journal["before"]["topology"]["api"]["image"]["revision"]
    if (old_image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != expected_revision:
        raise TransactionError("retained API image revision differs")
    old_env = old_raw["Config"].get("Env")
    if (_effective_environment(old_snap, old_image, compose_path, root) != _env_map(old_env) or
            observe._environment(old_env, old_snap["working_dir"], key)[1] != old["environment_fingerprint"]):
        raise TransactionError("recorded environment no longer reproduces the original container")
    target_env = _effective_environment(new_snap, new_image, compose_path, root)

    found = _inventory(old_snap["project"], old_snap["service"])
    candidates = []
    for item in found:
        item_id, labels = item.get("Id"), item.get("Labels") or {}
        if item_id == before["id"]:
            continue
        if type(labels) is not dict or labels.get(observe.TRANSACTION_LABEL) != journal["transaction_id"]:
            raise TransactionError("unknown API container")
        candidates.append(item_id)
    if len(candidates) > 1:
        raise TransactionError("ambiguous API target")
    recorded = journal["target"]["containers"]["api"]
    if recorded["id"] is not None and candidates and recorded["id"] != candidates[0]:
        raise TransactionError("API target identity changed")
    if (recorded["id"] is not None and not candidates
            and journal["phase"] not in ("rollback-restore", "rollback-pointers", "rolled-back")):
        raise TransactionError("recorded API target disappeared")
    if initial and candidates:
        raise TransactionError("API target already exists")
    target = None
    if candidates:
        target = _observed(candidates[0], new_snap, key)
        _recipe_matches(target, new_snap)
        record = target["container"]
        if (record["transaction_label"] != journal["transaction_id"]
                or record["name"] != _target_name(journal, new_snap)):
            raise TransactionError("API target binding differs")
        if (recorded["started_at"] is not None and journal["phase"] not in
                ("rollback-stop", "rollback-restore", "rollback-pointers", "rolled-back")
                and (record["started_at"] != recorded["started_at"]
                     or record["restart_count"] != recorded["restart_count"])):
            raise TransactionError("API target restarted")
        actual_target_env = observe._inspect(record["id"])["Config"].get("Env")
        if (_env_map(actual_target_env) != target_env or
                observe._environment(actual_target_env, new_snap["working_dir"], key)[1]
                != target["environment_fingerprint"]):
            raise TransactionError("API target effective environment differs")
    if old_record["running"] and target is not None and target["container"]["running"]:
        raise TransactionError("both API containers are running")
    if not before["running"] and old_record["running"]:
        raise TransactionError("originally stopped API was started")
    if not before["running"] and target is not None and target["container"]["running"]:
        raise TransactionError("replacement for a stopped API was started")
    if journal["phase"] in ("immutable-manifest", "deploy-commit", "deploy-current", "committed"):
        if (old_record["running"] or target is None
                or target["container"]["running"] != before["running"]):
            raise TransactionError("ready API topology changed before publication")
    if journal["api_identity"] is None and before["running"]:
        raise TransactionError("running API identity is not recorded")
    _verify_data(journal, [old] + ([] if target is None else [target]))
    if "telegram" not in journal["selection"]:
        _other_component(root, journal, key)
    _check_pointers(root, journal, old_only=initial)
    return old, target


def verify_artifact_component(root: Path, component: dict[str, Any], name: str) -> dict[str, Any]:
    """Verify a schema-3 component against its registered acquisition and the live store.

    Read-only: the receipt and bundle copy are re-read, the store identity
    and the image graph are inspected by full native ID, and the image's
    revision label must be the sealed source commit.
    """
    import component_artifact as artifact  # noqa: PLC0415
    if Path(artifact.__file__).resolve() != CODE_ROOT / "scripts/component_artifact.py":
        raise TransactionError("artifact verifier belongs to another source")
    provenance = component["provenance"]
    if provenance["kind"] != "completed-artifact":
        raise TransactionError(f"{name} target is not a completed artifact")
    try:
        receipt, bundle = manifest.read_artifact_records(root, provenance["bundle_sha256"],
                                                         provenance["acquisition_sha256"])
        manifest.verify_artifact_binding(name, component, receipt, bundle)
        path = root / ".deployments/artifacts/acquisitions" / (receipt["acquisition_id"] + ".json")
        verified = artifact.verify_acquisition(path)
    except (manifest.ManifestError, artifact.ArtifactError) as exc:
        raise TransactionError(f"{name} completed artifact cannot be verified") from exc
    if (verified["receipt_sha256"] != provenance["acquisition_sha256"]
            or verified["bundle_sha256"] != provenance["bundle_sha256"]):
        raise TransactionError(f"{name} completed artifact records changed")
    image = _docker("GET", "/images/" + component["image"]["id"] + "/json")
    if (type(image) is not dict or image.get("Id") != component["image"]["id"]
            or (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
            != component["image"]["revision"]):
        raise TransactionError(f"{name} artifact image or revision changed")
    return {"kind": "completed-artifact", "image_id": component["image"]["id"],
            "image_revision": component["image"]["revision"], "receipt": verified["receipt"],
            "bundle_sha256": verified["bundle_sha256"]}


def _verify_candidate(root: Path, journal: dict[str, Any]) -> None:
    component = journal["target"]["manifest"]["components"]["api"]
    provenance = component["provenance"]
    if provenance["kind"] == "completed-artifact":
        verify_artifact_component(root, component, "api")
        return
    if provenance["kind"] != "source-image":
        raise TransactionError("API target has no registered source build")
    receipt = build.verify_registered_build(root, provenance["build_receipt_sha256"])
    if (receipt["image_id"] != component["image"]["id"]
            or receipt["image_ref"] != component["image"]["ref"]
            or receipt["image_digest"] != component["image"]["digest"]
            or receipt["image_revision"] != component["image"]["revision"]
            or receipt["installed_core_version"] != component["version"]
            or receipt["recipe_sha256"] != provenance["build_recipe_sha256"]
            or not _same_source(receipt["recovery_source"], journal["source"])):
        raise TransactionError("API build receipt differs from the target")
    image = _docker("GET", "/images/" + quote(receipt["image_ref"], safe=":/") + "/json")
    if (type(image) is not dict or image.get("Id") != receipt["image_id"]
            or (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
            != receipt["image_revision"]):
        raise TransactionError("API image tag or revision changed")


def _legacy_version(root: Path, image_id: str, revision: str, source: dict[str, Any], *,
                    compose_path: Path | None = None, predecessor_raw: bytes | None = None,
                    allow_create: bool = True) -> str:
    """Reuse a completed exact version probe; never strand one behind a journal."""
    directory = root / ".deployments" / "component-adopt-probes"
    values: set[tuple[str, str]] = set()
    registered_versions: set[str] = set()
    try:
        if directory.exists() or directory.is_symlink():
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise TransactionError("legacy probe registry is not a directory")
            daemon = runtime._adopt_probe_daemon()
            for entry in directory.iterdir():
                record = runtime._read_adopt_probe(root, entry.name)
                if (record["kind"] == "legacy-core-version" and record["status"] == "completed"
                        and record["image_id"] == image_id and record["image_revision"] == revision
                        and _same_source(record["source"], source) and record["daemon_id"] == daemon):
                    if runtime._owned_adopt_probe(record) is not None:
                        raise TransactionError("completed legacy probe still has a container")
                    values.add((record["installed_core_version"], record["output_sha256"]))
        receipts = root / ".deployments" / "product-builds"
        if receipts.exists() or receipts.is_symlink():
            if not stat.S_ISDIR(receipts.lstat().st_mode):
                raise TransactionError("source build registry is not a directory")
            for entry in receipts.iterdir():
                if not entry.name.endswith(".json") or not runtime._HEX64.fullmatch(entry.name[:-5]):
                    raise TransactionError("source build registry has an unknown entry")
                receipt = build.verify_registered_build(root, entry.name[:-5])
                if (receipt["image_id"] == image_id and receipt["image_revision"] == revision
                        and _same_source(receipt["recovery_source"], source)):
                    registered_versions.add(receipt["installed_core_version"])
    except (OSError, runtime.JournalError, build.BuildError) as exc:
        raise TransactionError("legacy Core version evidence is unknown") from exc
    observed_versions = {version for version, _ in values} | registered_versions
    if len(values) > 1 or len(observed_versions) > 1:
        raise TransactionError("legacy Core version observations disagree")
    if observed_versions:
        return observed_versions.pop()
    if not allow_create:
        raise TransactionError("legacy Core version has no completed probe")
    journal_path = root / ".deployments" / runtime.JOURNAL_NAME
    if (journal_path.exists() or journal_path.is_symlink()) and (predecessor_raw is None or compose_path is None):
        raise TransactionError("legacy Core version needs a completed probe before this journal")
    try:
        return runtime._legacy_core_version(root, image_id, revision, source,
                                            compose_path=compose_path, predecessor_raw=predecessor_raw)
    except (admission.AdmissionError, runtime.JournalError) as exc:
        raise TransactionError("legacy Core version probe was refused") from exc


def _legacy_database(root: Path, journal: dict[str, Any], expected_sha256: str) -> Path:
    """Follow immutable predecessor records to a verified old-DB backup."""
    import component_full as full  # noqa: PLC0415
    import component_full_db as full_db  # noqa: PLC0415

    runtime._match(expected_sha256, runtime._HEX64, "legacy database hash")
    current = journal
    seen: set[tuple[str, str]] = set()
    for _ in range(1024):
        if current.get("kind") == "component-full":
            full.validate_journal(current)
            old = current["database"]["old"]
            backup = current["database"]["backup"]
            if old["binding"]["sha256"] == expected_sha256:
                if backup["stat"] is None:
                    path = Path(old["binding"]["path"])
                    binding = old["binding"]
                    expected_stat = old["stat"]
                else:
                    path = root / backup["path"]
                    binding = {**old["binding"], "path": str(path)}
                    expected_stat = backup["stat"]
                if (manifest.verify_database_binding(binding) != expected_stat
                        or full_db.permissions(path) != old["permissions"]):
                    raise TransactionError("legacy database backup differs")
                return path
        reference = current.get("predecessor")
        if reference is None:
            break
        runtime.validate_predecessor(reference, successor_id=current["transaction_id"], required=True)
        marker = (reference["kind"], reference["transaction_id"])
        if marker in seen:
            raise TransactionError("predecessor history has a cycle")
        seen.add(marker)
        history = f"component-{reference['kind']}-history"
        archive = root / reference["archive_path"]
        if admission._present(archive):
            with manifest._directory(root, (".deployments", history), create=False) as directory:
                raw = manifest._read_file(directory, reference["transaction_id"] + ".json")
        else:
            # Before successor publication the exact terminal predecessor is
            # still authoritative; no successor file may be substituted here.
            successor_kind = "full" if current.get("kind") == "component-full" else "runtime"
            successor_path = root / ".deployments" / f".component-{successor_kind}-transaction.json"
            if admission._present(successor_path):
                successor = runtime._decode(runtime._read_regular(successor_path))
                if type(successor) is dict and successor.get("transaction_id") == current["transaction_id"]:
                    raise TransactionError("published successor lacks its archived predecessor")
            raw = runtime._read_regular(root / reference["journal_path"])
        if hashlib.sha256(raw).hexdigest() != reference["journal_sha256"]:
            raise TransactionError("predecessor history hash differs")
        previous = runtime._decode(raw)
        if reference["kind"] == "runtime":
            previous = runtime.validate_journal(previous)
        else:
            previous = full.validate_journal(previous)
        if (previous["transaction_id"] != reference["transaction_id"]
                or previous["status"] not in ("committed", "rolled_back")):
            raise TransactionError("predecessor history is not terminal")
        won = "target" if previous["status"] == "committed" else "before"
        manifest_before = previous["target"]["manifest"] if won == "target" else previous["before"]["manifest"]
        pointers_before = previous["pointers"]["target" if won == "target" else "old"]
        if (manifest_before is None or current["before"]["manifest"] != manifest_before
                or current["before"]["topology"] != manifest_before["components"]
                or current["pointers"]["old"] != pointers_before):
            raise TransactionError("predecessor deployment binding differs")
        current = previous
    raise TransactionError("legacy database backup is unavailable")


def _verify_old_topology(root: Path, journal: dict[str, Any], *, compose_path: Path | None = None,
                         predecessor_raw: bytes | None = None, allow_probe: bool = True,
                         require_live_image: bool = True,
                         database_binding_override: dict[str, Any] | None = None) -> None:
    """Bind legacy preimages to their immutable v1 evidence and actual image."""
    import deployment_manifest as legacy  # noqa: PLC0415

    versions: dict[str, str] = {}
    for name, component in journal["before"]["topology"].items():
        if component is None:
            continue
        kind = component["provenance"]["kind"]
        if kind != "legacy-image":
            if kind == "completed-artifact":
                try:
                    receipt, bundle = manifest.read_artifact_records(
                        root, component["provenance"]["bundle_sha256"], component["provenance"]["acquisition_sha256"])
                    manifest.verify_artifact_binding(name, component, receipt, bundle)
                except manifest.ManifestError as exc:
                    raise TransactionError("installed completed-artifact evidence differs") from exc
            elif kind == "source-image":
                receipt = build.verify_registered_build(root, component["provenance"]["build_receipt_sha256"])
                source = receipt["source"]
                if (receipt["image_id"] != component["image"]["id"]
                        or receipt["image_ref"] != component["image"]["ref"]
                        or receipt["image_digest"] != component["image"]["digest"]
                        or receipt["image_revision"] != component["image"]["revision"]
                        or receipt["installed_core_version"] != component["version"]
                        or receipt["recipe_sha256"] != component["provenance"]["build_recipe_sha256"]
                        or component["source"]["verified_commit"] != source["commit"]
                        or component["source"]["verified_tree"] != source["tree"]):
                    raise TransactionError("unadopted API build evidence differs")
            elif kind == "wheel-image":
                receipt = wheel.verify_registered_wheel_build(root, component["provenance"]["build_receipt_sha256"])
                if (receipt["image_id"] != component["image"]["id"]
                        or receipt["image_ref"] != component["image"]["ref"]
                        or receipt["image_digest"] != component["image"]["digest"]
                        or receipt["image_revision"] != component["image"]["revision"]
                        or receipt["telegram_version"] != component["version"]
                        or receipt["core_version"] != component["core"]["version"]
                        or receipt["source"] != {"commit": component["source"]["verified_commit"],
                                                  "tree": component["source"]["verified_tree"]}
                        or receipt["core_source"] != {"commit": component["core"]["verified_commit"],
                                                       "tree": component["core"]["verified_tree"]}
                        or any(receipt[key] != component["provenance"][key] for key in
                               ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_producer_kind"))):
                    raise TransactionError("unadopted Telegram wheel evidence differs")
            else:
                raise TransactionError("unadopted image provenance is unsupported")
            if require_live_image and _image(component["image"]["id"]).get("Id") != component["image"]["id"]:
                raise TransactionError("unadopted image identity changed")
            continue
        revision = component["image"]["revision"]
        image = _image(component["image"]["id"]) if require_live_image else None
        if image is not None and (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != revision:
            raise TransactionError("legacy image revision differs")
        path = root / ".deployments" / (revision + ".json")
        with manifest._directory(root, (".deployments",), create=False) as directory:
            raw = manifest._read_file(directory, path.name)
        decoded = runtime._decode(raw)
        try:
            expected_db_sha256 = decoded["database"]["sha256"]
            runtime._match(expected_db_sha256, runtime._HEX64, "legacy database hash")
            db_binding = (journal["database"]["binding"] if database_binding_override is None
                          else database_binding_override)
            db_path = (Path(db_binding["path"]) if db_binding["sha256"] == expected_db_sha256 else
                       _legacy_database(root, journal, expected_db_sha256))
            previous = legacy.verify_manifest(path, source_commit=revision, image_id=component["image"]["id"],
                                              image_digest=None, db_path=db_path,
                                              immutable=True)
        except (legacy.ManifestError, KeyError, TypeError, runtime.JournalError) as exc:
            raise TransactionError("legacy manifest cannot bind the observed image and database") from exc
        with manifest._directory(root, (".deployments",), create=False) as directory:
            unchanged = manifest._read_file(directory, path.name) == raw
        digest = previous["image"]["digest"]
        if (not unchanged or previous != decoded or hashlib.sha256(raw).hexdigest() !=
                component["provenance"]["legacy_manifest_sha256"]
                or previous["database"]["sha256"] != expected_db_sha256
                or digest != component["image"]["digest"]
                or (image is not None and digest != component["image"]["id"]
                    and digest not in (image.get("RepoDigests") or []))):
            raise TransactionError("legacy manifest digest or binding differs")
        if journal["before"]["manifest"] is None:
            if journal["pointers"]["old"][".deploy_commit"] != {"present": True, "bytes": revision + "\n"}:
                raise TransactionError("unadopted legacy pointer does not identify the image")
            if component["image"]["id"] not in versions:
                versions[component["image"]["id"]] = _legacy_version(
                    root, component["image"]["id"], revision, journal["source"], compose_path=compose_path,
                    predecessor_raw=predecessor_raw, allow_create=allow_probe)
            if component["core"]["version"] != versions[component["image"]["id"]]:
                raise TransactionError("legacy installed Core version differs")


def _telegram_receipt(root: Path, journal: dict[str, Any]) -> dict[str, Any]:
    component = journal["target"]["manifest"]["components"]["telegram"]
    if component is not None and component["provenance"]["kind"] == "completed-artifact":
        return verify_artifact_component(root, component, "telegram")
    if component is None or component["provenance"]["kind"] != "wheel-image":
        raise TransactionError("Telegram target has no registered wheel build")
    provenance = component["provenance"]
    digest = provenance.get("build_receipt_sha256")
    if type(digest) is not str:
        raise TransactionError("Telegram wheel receipt is missing")
    receipt = wheel.verify_registered_wheel_build(root, digest)
    source, core = component["source"], component["core"]
    if (not _same_source(receipt["recovery_source"], journal["source"])
            or receipt["image_id"] != component["image"]["id"]
            or receipt["image_ref"] != component["image"]["ref"]
            or receipt["image_digest"] != component["image"]["digest"]
            or receipt["image_revision"] != component["image"]["revision"]
            or receipt["telegram_version"] != component["version"]
            or receipt["core_version"] != core["version"]
            or receipt["source"] != {"commit": source["verified_commit"], "tree": source["verified_tree"]}
            or receipt["core_source"] != {"commit": core["verified_commit"], "tree": core["verified_tree"]}
            or source["revision_claim"] != receipt["source"]["commit"]
            or core["revision_claim"] != receipt["core_source"]["commit"]
            or any(receipt[key] != provenance[key] for key in
                   ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "core_producer_kind"))):
        raise TransactionError("Telegram wheel receipt differs from the target")
    image = _docker("GET", "/images/" + quote(receipt["image_ref"], safe=":/") + "/json")
    if (type(image) is not dict or image.get("Id") != receipt["image_id"]
            or (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
            != receipt["image_revision"]):
        raise TransactionError("Telegram image tag or revision changed")
    return receipt


def _telegram_context(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    """Check immutable TG inputs and the actual old environment, without writes."""
    receipt = _telegram_receipt(root, journal)
    old_component = journal["before"]["topology"]["telegram"]
    target_component = journal["target"]["manifest"]["components"]["telegram"]
    old_snap = _snapshot(root, old_component)
    target_snap = _snapshot(root, target_component)
    old_id = journal["before"]["containers"]["telegram"]["id"]
    old_image, target_image = _image(old_snap["image_id"]), _image(target_snap["image_id"])
    if (old_image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != old_component["image"]["revision"]:
        raise TransactionError("old Telegram image revision differs")
    old_env = _effective_environment(old_snap, old_image, compose_path, root)
    if old_env != _env_map(observe._inspect(old_id)["Config"].get("Env")):
        raise TransactionError("recorded Telegram environment differs from the old container")
    target_env = _effective_environment(target_snap, target_image, compose_path, root)
    user = target_snap["user"] or target_image.get("Config", {}).get("User") or "0"
    uid = user.split(":", 1)[0]
    if not uid.isascii() or not uid.isdigit():
        raise TransactionError("Telegram runtime UID is not numeric")
    manifest.verify_poll_health_mount(target_snap, effective_uid=int(uid))
    if target_env.get("WUWATERM_POLL_HEALTH") != "1":
        raise TransactionError("Telegram poll health is not enabled")
    return {"receipt": receipt, "old_snapshot": old_snap, "target_snapshot": target_snap,
            "old_environment": old_env, "target_environment": target_env}


def _state_module() -> Any:
    try:
        import component_state as state  # noqa: PLC0415
        if Path(state.__file__).resolve() != CODE_ROOT / "scripts" / "component_state.py":
            raise TransactionError("state operation imported a foreign source")
        return state
    except ImportError as exc:
        raise TransactionError("Telegram state probes are unavailable") from exc


def _probe_telegram_state(root: Path, journal: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Use recorded actual-UID probes; a complete pair is required to advance.

    Schema 2-4 ask whether the two plans name the same state without copying.
    Schema 5 additionally allows the product's own startup copy to be pending
    (``copy-if-absent``): the transaction never copies, so it records the
    stopped preimages and lets the native writer progress them.
    """
    state = _state_module()
    component = journal["target"]["manifest"]["components"]["telegram"]
    database_path = journal["database"]["binding"]["path"]
    plans = []
    for prefix, recorded in (("old", journal["before"]["topology"]["telegram"]), ("target", component)):
        # Each plan runs with its own recorded image: a completed artifact has
        # no producer receipt and is verified through its acquisition instead.
        plans.append(state.plan(root, receipt_sha256=recorded["provenance"].get("build_receipt_sha256"),
                                snapshot=context[prefix + "_snapshot"],
                                environment=context[prefix + "_environment"], database_path=database_path,
                                transaction_id=journal["transaction_id"]))
    old_plan, target_plan = plans
    schema5 = journal["schema_version"] == runtime.JOURNAL_SCHEMA_VERSION
    if schema5:
        import component_prepare as prepare  # noqa: PLC0415
        try:
            prepare.verify_state_plan_equivalence(old_plan, target_plan)
        except prepare.PrepareError as exc:
            raise TransactionError(str(exc)) from exc
    migrations = []
    for kind in ("chat-settings", "channel-replies"):
        old, new = old_plan["kinds"][kind], target_plan["kinds"][kind]
        if not schema5 and (old["migration"] == "copy-if-absent" or new["migration"] == "copy-if-absent"):
            # Schema 2-4 have no native-pending rollback contract.
            raise TransactionError("Telegram state relocation requires a full update")
        if (old["mapped_target"] != new["mapped_target"] or old["effective_path"] != new["effective_path"]):
            raise TransactionError("Telegram state relocation requires a full update")
        if not schema5 and old["mapped_legacy"] != new["mapped_legacy"]:
            raise TransactionError("Telegram state relocation requires a full update")
        snapshot = context["target_snapshot"]
        kwargs = {"image_id": component["image"]["id"], "image_revision": component["image"]["revision"],
                  "snapshot": snapshot, "transaction_id": journal["transaction_id"]}
        target_before = state.preimage(root, host_path=new["mapped_target"], **kwargs)
        source_before = (None if new["mapped_legacy"] is None else
                         state.preimage(root, host_path=new["mapped_legacy"], **kwargs))
        migrations.append({"kind": kind, "action": new["migration"], "effective_path": new["effective_path"],
                           "source_path": new["mapped_legacy"], "target_path": new["mapped_target"],
                           "source_before": source_before, "target_before": target_before, "created": None})
    return migrations


def _state_plan(root: Path, receipt: dict[str, Any], receipt_sha256: str,
                snapshot: dict[str, Any], environment: dict[str, str], database_path: str) -> dict[str, Any]:
    """Run the verified Telegram product's offline state planner on real binds."""
    settings, _ = observe._environment([key + "=" + value for key, value in environment.items()],
                                      snapshot["working_dir"], os.urandom(32))
    db_path = settings["WUWATERM_DB_PATH"]
    if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
        db_path = snapshot["command"][1]
    if db_path is None:
        raise TransactionError("Telegram database path is not explicit")
    if observe.resolve_bind_path(snapshot, db_path, writable=False) != database_path:
        raise TransactionError("Telegram database mount differs")
    mounts = [{key: mount[key] for key in ("host", "container", "read_only")}
              for mount in snapshot["mounts"] if "host" in mount]
    source = root / receipt["context_path"] / "source" / "src"
    code = ("import json,pathlib,sys; sys.path.insert(0,sys.argv[1]); "
            "from wuwaterm_telegram.operations import state_plan; "
            "print(json.dumps(state_plan(pathlib.Path(sys.argv[2]),json.loads(sys.argv[3])),sort_keys=True))")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    for name in ("WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH", "WUWATERM_CHANNEL_REPLY_INDEX_PATH"):
        if settings[name] is not None:
            env[name] = settings[name]
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(source), db_path,
                                 json.dumps([json.dumps(mount, sort_keys=True) for mount in mounts])],
                                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=(9,), timeout=30, check=False)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise TransactionError("Telegram product state planner refused the binding")
        plan = json.loads(result.stdout)
        if (type(plan) is not dict or set(plan) != {"database_path", "kinds"}
                or plan["database_path"] != db_path or type(plan["kinds"]) is not dict
                or set(plan["kinds"]) != {"chat-settings", "channel-replies"}):
            raise TransactionError("Telegram product state plan is invalid")
        for item in plan["kinds"].values():
            if (type(item) is not dict or set(item) != {"effective_path", "legacy_path", "migration",
                                                       "mapped_target", "mapped_legacy"}
                    or item["migration"] not in ("none", "validate-existing", "copy-if-absent")):
                raise TransactionError("Telegram product state plan is invalid")
        if wheel.verify_registered_wheel_build(root, receipt_sha256) != receipt:
            raise TransactionError("Telegram source changed during state planning")
        return plan
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise TransactionError("Telegram product state planning failed") from None


def _leaf_host(recipe: dict[str, Any], path: str) -> str:
    """Resolve one retained state link in the namespace that wrote it."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return path
    if not stat.S_ISLNK(info.st_mode):
        return path
    if not 0 < info.st_size <= 4095:
        raise TransactionError("Telegram state entry link is unsafe")
    state = _state_module()
    try:
        return state.leaf_host_path(recipe, path)
    except state.StateError as exc:
        raise TransactionError("Telegram state entry link cannot be followed") from exc


def _state_preimage(path: str, *, leaf: bool = False, recipe: dict[str, Any] | None = None) -> dict[str, Any]:
    target = Path(path)
    # Runtime-only updates do not create state directories. Requiring an
    # existing physical parent also rejects host symlink traversal.
    with manifest._directory(target.parent, (), create=False):
        pass
    try:
        before = target.lstat()
    except FileNotFoundError:
        return {"present": False, "sha256": None, "stat": None}
    if leaf and recipe is None:
        raise TransactionError("Telegram state leaf resolution requires its recipe")
    if leaf and stat.S_ISLNK(before.st_mode):
        # A retained reply or settings link is read through: both native readers
        # parse the file it names, not the entry the reply writer replaces.
        target = Path(_leaf_host(recipe, path))
        try:
            with manifest._directory(target.parent, (), create=False):
                pass
        except manifest.ManifestError:
            return {"present": False, "sha256": None, "stat": None}
        try:
            before = target.lstat()
        except FileNotFoundError:
            return {"present": False, "sha256": None, "stat": None}
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise TransactionError("Telegram state is not a singly linked regular file")
    digest = recovery._hash(target)
    after = target.lstat()
    if manifest._db_stat(before) != manifest._db_stat(after):
        raise TransactionError("Telegram state changed during preflight")
    return {"present": True, "sha256": digest, "stat": manifest._db_stat(after)}


def _preserved_state_rollback(root: Path, journal: dict[str, Any]) -> None:
    """Prove the old native readers accept the state the new product progressed.

    Schema-5 cutover never copies and never restores state, so rollback keeps
    the current bytes at every effective path: an entry the new writer made a
    regular file stays one, and nothing recreates an old reply link. What the
    rollback owes instead is evidence that the old image can still read those
    bytes, and that a distinct legacy file was not touched.
    """
    if journal["schema_version"] != runtime.JOURNAL_SCHEMA_VERSION or journal["kind"] != "component-runtime":
        return
    migrations = journal["migrations"]
    if not migrations:
        return
    state = _state_module()
    before = journal["before"]["topology"]["telegram"]
    snapshot = _snapshot(root, before)
    clock = int(time.time())
    for item in migrations:
        source, target = item["source_path"], item["target_path"]
        if (source is not None and source != target and item["source_before"] is not None
                and item["source_before"]["present"]
                and _state_preimage(source, leaf=True, recipe=snapshot) != item["source_before"]):
            # With an explicit StateDir the legacy file is a distinct endpoint;
            # its inode and bytes are not this transaction's to change.
            raise TransactionError("legacy Telegram state changed during the cutover")
        current = _preserved_leaf(target)
        if not current["present"]:
            if item["target_before"]["present"]:
                raise TransactionError("Telegram state disappeared during the cutover")
            continue
        if current["link"]:
            # The product replaces a reply entry with a regular file. An entry
            # that is still a link was not progressed, and rollback must not
            # recreate or re-point it, so there is nothing to compare.
            continue
        # Present bytes still have to be readable: the old container is about
        # to run again against whatever this transaction left.
        try:
            state.canonical(root, snapshot=snapshot, host_path=target,
                            transaction_id=journal["transaction_id"], state_kind=item["kind"], clock=clock)
        except state.StateError as exc:
            raise TransactionError("old Telegram reader cannot accept the progressed state") from exc


def _preserved_leaf(path: str) -> dict[str, Any]:
    """Observe one recorded state leaf, including a retained reply link."""
    target = Path(path)
    with manifest._directory(target.parent, (), create=False):
        pass
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {"present": False, "sha256": None, "stat": None, "link": False}
    if stat.S_ISLNK(info.st_mode):
        return {"present": True, "sha256": None, "stat": manifest._db_stat(info), "link": True}
    return {**_state_preimage(path), "link": False}


def preflight_telegram(root: Path, journal: dict[str, Any], *, compose_path: Path) -> dict[str, Any]:
    """Verify a real wheel receipt and run its state planner before mutations.

    This is a building block, not an enabled Telegram transaction. It refuses
    relocation and copy-if-absent and compares the candidate's effective host
    paths with those of the actual old container.
    """
    root = admission._physical_root(root)
    admission.admit_component(root, operation="update", code_root=CODE_ROOT)
    journal = runtime.validate_journal(journal)
    if (journal["action"] != "update" or "telegram" not in journal["selection"]
            or journal["before"]["topology"]["telegram"] is None):
        raise TransactionError("Telegram is not an installed selected component")
    receipt = _telegram_receipt(root, journal)
    digest = journal["target"]["manifest"]["components"]["telegram"]["provenance"]["build_receipt_sha256"]
    old_component = journal["before"]["topology"]["telegram"]
    target_component = journal["target"]["manifest"]["components"]["telegram"]
    old_snap = _snapshot(root, old_component)
    target_snap = _snapshot(root, target_component)
    old_id = journal["before"]["containers"]["telegram"]["id"]
    observed = _observed(old_id, old_snap, os.urandom(32))
    _recipe_matches(observed, old_snap)
    if observed["container"] != journal["before"]["containers"]["telegram"]:
        raise TransactionError("old Telegram container changed")
    old_image, target_image = _image(old_snap["image_id"]), _image(target_snap["image_id"])
    if (old_image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != old_component["image"]["revision"]:
        raise TransactionError("old Telegram image revision differs")
    old_env = _effective_environment(old_snap, old_image, compose_path, root)
    if old_env != _env_map(observe._inspect(old_id)["Config"].get("Env")):
        raise TransactionError("recorded Telegram environment differs from the old container")
    target_env = _effective_environment(target_snap, target_image, compose_path, root)
    user = target_snap["user"] or target_image.get("Config", {}).get("User") or "0"
    uid = user.split(":", 1)[0]
    if not uid.isascii() or not uid.isdigit():
        raise TransactionError("Telegram runtime UID is not numeric")
    effective_uid = int(uid)
    if effective_uid not in (0, os.geteuid()):
        raise TransactionError("Telegram state access cannot be preflighted for this UID")
    manifest.verify_poll_health_mount(target_snap, effective_uid=effective_uid)
    if target_env.get("WUWATERM_POLL_HEALTH") != "1":
        raise TransactionError("Telegram poll health is not enabled")
    database_path = journal["database"]["binding"]["path"]
    old_plan = _state_plan(root, receipt, digest, old_snap, old_env, database_path)
    new_plan = _state_plan(root, receipt, digest, target_snap, target_env, database_path)
    migrations = {item["kind"]: item for item in journal["migrations"]}
    if set(migrations) != {"chat-settings", "channel-replies"}:
        raise TransactionError("Telegram state preimages are incomplete")
    for kind in migrations:
        old, new, recorded = old_plan["kinds"][kind], new_plan["kinds"][kind], migrations[kind]
        if (old["migration"] == "copy-if-absent" or new["migration"] == "copy-if-absent"
                or old["mapped_target"] != new["mapped_target"]
                or old["effective_path"] != new["effective_path"]):
            raise TransactionError("Telegram state relocation requires a full update")
        if (recorded["created"] is not None or recorded["action"] != new["migration"]
                or recorded["effective_path"] != new["effective_path"]
                or recorded["target_path"] != new["mapped_target"]
                or recorded["source_path"] != new["mapped_legacy"]
                or recorded["target_before"] != _state_preimage(recorded["target_path"])):
            raise TransactionError("Telegram state preimage differs from the product plan")
        if (recorded["source_path"] is not None
                and recorded["source_before"] != _state_preimage(recorded["source_path"])):
            raise TransactionError("Telegram legacy state preimage changed")
    if manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
        raise TransactionError("database stat changed")
    return {"state_plan": new_plan, "old_state_plan": old_plan,
            "wheel_receipt_sha256": digest, "effective_uid": effective_uid}


def _other_api(root: Path, journal: dict[str, Any], key: bytes, *, terminal: bool = False) -> None:
    component = journal["before"]["topology"]["api"]
    if component is None:
        if manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
            raise TransactionError("database stat changed")
        return
    expected = journal["before"]["containers"]["api"]
    snap = _snapshot(root, component)
    found = _inventory(snap["project"], snap["service"])
    if len(found) != 1 or found[0].get("Id") != expected["id"]:
        raise TransactionError("unselected API inventory changed")
    actual = _observed(expected["id"], snap, key)
    _recipe_matches(actual, snap)
    if (not _same_container_binding(actual["container"], expected) if terminal else actual["container"] != expected):
        raise TransactionError("unselected API changed")
    _verify_data(journal, [actual])


def _recover_pending_telegram(root: Path, journal: dict[str, Any], compose_path: Path, *,
                              finish: bool = True) -> dict[str, Any]:
    """Close preflight only if every original service and pointer is untouched."""
    if (journal["migrations"] != [] or journal["phase"] not in ("prepared", "rolled-back")
            or any(journal["target"]["containers"][name] is not None and
                   journal["target"]["containers"][name]["id"] is not None for name in journal["selection"])):
        raise TransactionError("pending Telegram preflight cannot be reconciled")
    key = os.urandom(32)
    api = None
    for name in ("api", "telegram"):
        component = journal["before"]["topology"][name]
        if component is None:
            continue
        before = journal["before"]["containers"][name]
        snapshot = _snapshot(root, component)
        inventory = _inventory(snapshot["project"], snapshot["service"])
        if len(inventory) != 1 or inventory[0].get("Id") != before["id"]:
            raise TransactionError("pending preflight container inventory changed")
        actual = _observed(before["id"], snapshot, key)
        _recipe_matches(actual, snapshot)
        if actual["container"] != before:
            raise TransactionError("pending preflight service changed")
        image = _image(snapshot["image_id"])
        if (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != component["image"]["revision"]:
            raise TransactionError("pending preflight image revision changed")
        if (_effective_environment(snapshot, image, compose_path, root) !=
                _env_map(observe._inspect(before["id"])["Config"].get("Env"))):
            raise TransactionError("pending preflight environment changed")
        if name == "api":
            api = actual
        else:
            db = actual["path_settings"]["WUWATERM_DB_PATH"]
            if db is None and snapshot["command"] and snapshot["command"][:1] == ["--db"]:
                db = snapshot["command"][1]
            if db is None or observe.resolve_bind_path(actual["recipe"], db, writable=False) != journal["database"]["binding"]["path"]:
                raise TransactionError("pending preflight database mount changed")
    for name in journal["selection"]:
        proposed = journal["target"]["manifest"]["components"][name]
        snapshot = _snapshot(root, proposed)
        permitted = {record["id"] for record in journal["before"]["containers"].values()
                     if record is not None and record["project"] == snapshot["project"]
                     and record["service"] == snapshot["service"]}
        if {item.get("Id") for item in _inventory(snapshot["project"], snapshot["service"])} != permitted:
            raise TransactionError("pending preflight target inventory changed")
    if api is None:
        if manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
            raise TransactionError("pending preflight database changed")
    else:
        _verify_data(journal, [api])
    _check_pointers(root, journal, old_only=True)
    already = journal["status"] == "rolled_back"
    if not finish:
        return {"status": "pending", "transaction_id": journal["transaction_id"]}
    if not already:
        _step(root, journal, "rolled-back", status="rolled_back")
    return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
            "already_restored": already, "polling_health": "unconfirmed"}


def _create_from_snapshot(journal: dict[str, Any], snapshot: dict[str, Any],
                          environment: dict[str, str], component: str) -> str:
    original = observe._inspect(journal["before"]["containers"][component]["id"])
    labels = {name: value for name, value in (original["Config"].get("Labels") or {}).items()
              if not name.startswith("com.docker.compose.") and name != "org.opencontainers.image.revision"}
    labels.update({"com.docker.compose.project": snapshot["project"],
                   "com.docker.compose.service": snapshot["service"],
                   "com.docker.compose.container-number": "1", observe.TRANSACTION_LABEL: journal["transaction_id"]})
    host: dict[str, Any] = {
        "NetworkMode": snapshot["network_mode"],
        "RestartPolicy": {"Name": snapshot["restart"]["name"],
                          "MaximumRetryCount": snapshot["restart"]["maximum_retry_count"]},
        "Mounts": [{"Type": "bind", "Source": mount["host"], "Target": mount["container"],
                    "ReadOnly": mount["read_only"]} for mount in snapshot["mounts"] if "host" in mount],
        "Tmpfs": {mount["container"]: ",".join(mount["options"]) for mount in snapshot["mounts"]
                  if mount.get("type") == "tmpfs"},
    }
    options = snapshot["runtime_options"]
    for source, destination in (("read_only_root", "ReadonlyRootfs"), ("privileged", "Privileged"),
                                ("init", "Init"), ("pid_mode", "PidMode"), ("ipc_mode", "IpcMode"),
                                ("cap_add", "CapAdd"), ("cap_drop", "CapDrop"),
                                ("security_opt", "SecurityOpt"), ("dns", "Dns"), ("extra_hosts", "ExtraHosts")):
        if options[source] is not None:
            host[destination] = options[source]
    ports: dict[str, list[dict[str, str]]] = {}
    for port in snapshot["ports"]:
        key = f'{port["container_port"]}/{port["protocol"]}'
        ports.setdefault(key, []).append({"HostIp": port["host_ip"] or "", "HostPort": str(port["host_port"])})
    host["PortBindings"] = ports
    body = {"Image": snapshot["image_id"], "Cmd": snapshot["command"], "Entrypoint": snapshot["entrypoint"],
            "User": snapshot["user"] or "", "WorkingDir": snapshot["working_dir"] or "",
            "Env": [name + "=" + value for name, value in sorted(environment.items())], "Labels": labels,
            "ExposedPorts": {name: {} for name in ports}, "HostConfig": host}
    result = _docker("POST", "/containers/create?" + urlencode({"name": _target_name(journal, snapshot, component)}),
                     body, accepted=(201,))
    if type(result) is not dict:
        raise TransactionError("Docker did not identify the target")
    return runtime._match(result.get("Id"), runtime._HEX64, "target ID")


def _verify_probed_preimages(journal: dict[str, Any], recipe: dict[str, Any]) -> None:
    # The probe already hashed each file as the actual UID. Recheck the exact
    # inode and timestamps before service mutation without reopening private
    # state through the host account.
    schema5 = journal.get("schema_version") == 5

    def matches(path: str, preimage: dict[str, Any]) -> bool:
        target = Path(path)
        with manifest._directory(target.parent, (), create=False):
            pass
        try:
            info = target.lstat()
        except FileNotFoundError:
            return not preimage["present"]
        if schema5 and stat.S_ISLNK(info.st_mode):
            # A retained reply link records the file it names, which is what
            # both native readers parse; the entry itself is replaced later.
            referent = Path(_leaf_host(recipe, path))
            try:
                with manifest._directory(referent.parent, (), create=False):
                    pass
            except manifest.ManifestError:
                # The chain already stayed inside its bind, so an unusable
                # referent parent means the file it names is not there.
                return not preimage["present"]
            try:
                info = referent.lstat()
            except FileNotFoundError:
                return not preimage["present"]
        return (preimage["present"] and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and manifest._db_stat(info) == preimage["stat"])

    if {item["kind"] for item in journal["migrations"]} != {"chat-settings", "channel-replies"}:
        raise TransactionError("Telegram state preimages are incomplete")
    for item in journal["migrations"]:
        if item["created"] is not None:
            raise TransactionError("Telegram state changed or requires relocation")
        if item["action"] == "copy-if-absent":
            # Schema 5 records the copy the product performs at startup and
            # never performs it; the target must still be absent here.
            if (not schema5 or item["target_before"]["present"] or item["source_before"] is None
                    or item["source_path"] is None):
                raise TransactionError("Telegram state changed or requires relocation")
            if not matches(item["source_path"], item["source_before"]):
                raise TransactionError("Telegram legacy state changed")
            continue
        if not matches(item["target_path"], item["target_before"]):
            raise TransactionError("Telegram state changed or requires relocation")
        if (item["source_path"] is not None and
                not matches(item["source_path"], item["source_before"])):
            raise TransactionError("Telegram legacy state changed")


def _telegram_state_bindings(journal: dict[str, Any], observations: list[dict[str, Any]]) -> None:
    """Keep the shared effective files, including changes made by a poller."""
    items, _ = _recorded_state_items(journal)
    if {item["kind"] for item in items} != {"chat-settings", "channel-replies"}:
        raise TransactionError("Telegram state bindings are incomplete")
    full = type(journal["migrations"]) is dict
    schema5 = journal.get("schema_version") == 5
    recipe = observations[0]["recipe"]
    for item in items:
        created = item.get("created") if full else item["created"]
        effective = item["new_effective_path"] if full else item["effective_path"]
        target_path = item["new_target_path"] if full else item["target_path"]
        source_path = item["new_legacy_path"] if full else item["source_path"]
        pending = item["action"] == "copy-if-absent"
        if created is not None or (pending and not schema5):
            raise TransactionError("Telegram state relocation is unsupported")
        for observed in observations:
            if schema5:
                if _telegram_v5_paths(observed, observed["recipe"])[item["kind"]]["planned"] != target_path:
                    raise TransactionError("Telegram effective state path changed")
            elif observe.resolve_bind_path(observed["recipe"], effective, writable=True) != target_path:
                raise TransactionError("Telegram effective state path changed")

        def present(path: str, *, leaf: bool, recipe: dict[str, Any]) -> bool:
            target = Path(path)
            with manifest._directory(target.parent, (), create=False):
                pass
            try:
                info = target.lstat()
            except FileNotFoundError:
                return False
            if leaf and stat.S_ISLNK(info.st_mode):
                # A retained reply link is present when the file it names is.
                referent = Path(_leaf_host(recipe, path))
                try:
                    with manifest._directory(referent.parent, (), create=False):
                        pass
                except manifest.ManifestError:
                    return False
                try:
                    info = referent.lstat()
                except FileNotFoundError:
                    return False
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TransactionError("Telegram state is not a singly linked regular file")
            return True

        current = present(target_path, leaf=schema5, recipe=recipe)
        if item["target_before"]["present"] and not current:
            raise TransactionError("existing Telegram state disappeared")
        source_present = source_path is not None and present(source_path, leaf=schema5, recipe=recipe)
        if pending and source_path is not None and source_path != target_path and not source_present:
            # The product reads this file once at startup; losing it before
            # then would start the product on empty state.
            raise TransactionError("legacy Telegram state disappeared")
        if source_present and not current and source_path != target_path and not pending:
            raise TransactionError("Telegram state would require a copy")


def _refresh_telegram_state(root: Path, journal: dict[str, Any], compose_path: Path) -> None:
    """Record state after the old poller has stopped and re-run product planning."""
    previous = {item["kind"]: item for item in journal["migrations"]}
    revised = _probe_telegram_state(root, journal, _telegram_context(root, journal, compose_path))
    for item in revised:
        old = previous.get(item["kind"])
        if old is None or any(item[key] != old[key] for key in ("effective_path", "target_path", "source_path")):
            raise TransactionError("Telegram state changed to a different path")
    journal["migrations"] = revised
    _write_journal(root, journal)


def _telegram_ready(container_id: str, snapshot: dict[str, Any], *, timeout_seconds: float = 90) -> None:
    """Ask the running product to validate its recent successful SDK poll."""
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600:
        raise TransactionError("Telegram readiness timeout is invalid")
    try:
        baseline = observe._inspect(container_id)
        start = baseline["State"]["StartedAt"]
        restarts = baseline["RestartCount"]
        if not baseline["State"]["Running"]:
            raise TransactionError("Telegram process is not running")
        uid = subprocess.run(["docker", "exec", container_id, "python", "-B", "-c",
                              "import os; print(os.geteuid())"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, pass_fds=(9,), timeout=10, check=False)
        raw_uid = uid.stdout.decode("ascii").strip()
        if uid.returncode or not raw_uid.isascii() or not raw_uid.isdigit():
            raise TransactionError("Telegram effective UID is unavailable")
        manifest.verify_poll_health_mount(snapshot, effective_uid=int(raw_uid))
        writable = ("import os,secrets; d='/run/wuwaterm-telegram'; "
                    "p=d+'/.probe-'+secrets.token_hex(12); "
                    "f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.close(f); os.unlink(p)")
        probe = subprocess.run(["docker", "exec", container_id, "python", "-B", "-c", writable],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               pass_fds=(9,), timeout=10, check=False)
        if probe.returncode:
            raise TransactionError("Telegram health tmpfs is not writable")
        deadline = time.monotonic() + timeout_seconds
        while True:
            observed = observe._inspect(container_id)
            if (not observed["State"]["Running"] or observed["State"].get("Restarting")
                    or observed["State"]["StartedAt"] != start or observed["RestartCount"] != restarts):
                raise TransactionError("Telegram process stopped or restarted before polling readiness")
            result = subprocess.run(["docker", "exec", container_id, "python", "-B", "-m",
                                     "wuwaterm_telegram.operations", "health"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, pass_fds=(9,), timeout=10, check=False)
            if not result.returncode:
                response = json.loads(result.stdout)
                if (type(response) is dict and response.get("status") == "ready"
                        and type(response.get("pid")) is int and response["pid"] > 0):
                    return
            if time.monotonic() >= deadline:
                raise TransactionError("Telegram SDK polling readiness did not succeed")
            time.sleep(0.5)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError, KeyError):
        raise TransactionError("Telegram polling health could not be verified") from None


def _actual_telegram(root: Path, journal: dict[str, Any], compose_path: Path, *,
                             initial: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
    before = journal["before"]["containers"]["telegram"]
    old_snap = _snapshot(root, journal["before"]["topology"]["telegram"])
    target_snap = _snapshot(root, journal["target"]["manifest"]["components"]["telegram"])
    key = os.urandom(32)
    old = _observed(before["id"], old_snap, key)
    _recipe_matches(old, old_snap)
    expected_old = {**before, "name": old["container"]["name"], "running": old["container"]["running"],
                    "started_at": old["container"]["started_at"]}
    if (old["container"] != expected_old or old["container"]["name"] not in
            (before["name"], _retained_name(journal, "telegram")) or
            (initial and old["container"] != before)):
        raise TransactionError("Telegram preimage changed")
    if (old["container"]["started_at"] != before["started_at"] and journal["phase"] not in
            ("rollback-restore", "rollback-pointers", "rolled-back")):
        raise TransactionError("retained Telegram start time changed")
    if not before["running"] and old["container"]["running"]:
        raise TransactionError("originally stopped Telegram was started")
    old_image, target_image = _image(old_snap["image_id"]), _image(target_snap["image_id"])
    for image, component in ((old_image, journal["before"]["topology"]["telegram"]),
                             (target_image, journal["target"]["manifest"]["components"]["telegram"])):
        if (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != component["image"]["revision"]:
            raise TransactionError("Telegram image revision changed")
    if (_effective_environment(old_snap, old_image, compose_path, root) !=
            _env_map(observe._inspect(before["id"])["Config"].get("Env"))):
        raise TransactionError("old Telegram environment changed")
    expected_env = _effective_environment(target_snap, target_image, compose_path, root)
    old_scope = _inventory(old_snap["project"], old_snap["service"])
    target_scope = (_inventory(target_snap["project"], target_snap["service"])
                    if (old_snap["project"], old_snap["service"]) !=
                    (target_snap["project"], target_snap["service"]) else old_scope)
    candidates = []
    for item in target_scope:
        if item.get("Id") == before["id"]:
            continue
        labels = item.get("Labels") or {}
        if type(labels) is not dict or labels.get(observe.TRANSACTION_LABEL) != journal["transaction_id"]:
            raise TransactionError("unknown Telegram target object")
        candidates.append(item.get("Id"))
    allowed = {before["id"], *candidates}
    if any(item.get("Id") not in allowed for item in old_scope) or len(candidates) > 1:
        raise TransactionError("unknown or ambiguous Telegram inventory")
    recorded = journal["target"]["containers"]["telegram"]
    if recorded["id"] is not None and candidates and recorded["id"] != candidates[0]:
        raise TransactionError("Telegram target identity changed")
    if (recorded["id"] is not None and not candidates
            and journal["phase"] not in ("rollback-restore", "rollback-pointers", "rolled-back")):
        raise TransactionError("Telegram target disappeared")
    if initial and candidates:
        raise TransactionError("Telegram target already exists")
    target = None
    if candidates:
        target = _observed(candidates[0], target_snap, key)
        _recipe_matches(target, target_snap)
        actual = target["container"]
        if ((actual["running"] and not before["running"]) or
                actual["name"] != _target_name(journal, target_snap, "telegram")
                or actual["transaction_label"] != journal["transaction_id"]
                or _env_map(observe._inspect(actual["id"])["Config"].get("Env")) != expected_env):
            raise TransactionError("Telegram target differs")
        if (recorded["started_at"] is not None and journal["phase"] not in
                ("rollback-stop", "rollback-restore", "rollback-pointers", "rolled-back")
                and (actual["started_at"] != recorded["started_at"]
                     or actual["restart_count"] != recorded["restart_count"])):
            raise TransactionError("Telegram target restarted")
    if old["container"]["running"] and target is not None and target["container"]["running"]:
        raise TransactionError("both Telegram containers are running")
    if journal["phase"] in ("retire-old", "immutable-manifest", "deploy-commit", "deploy-current", "committed"):
        if (old["container"]["running"] or target is None
                or target["container"]["running"] != before["running"]):
            raise TransactionError("ready Telegram topology changed before publication")
    if initial:
        _verify_probed_preimages(journal, old["recipe"])
    _telegram_state_bindings(journal, [old] + ([] if target is None else [target]))
    for observed, snapshot in [(old, old_snap)] + ([] if target is None else [(target, target_snap)]):
        path = _telegram_database_path(observed, snapshot)
        if observe.resolve_bind_path(observed["recipe"], path, writable=False) != journal["database"]["binding"]["path"]:
            raise TransactionError("Telegram database binding changed")
    if "api" not in journal["selection"]:
        _other_api(root, journal, key)
    elif manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
        raise TransactionError("database stat changed")
    _check_pointers(root, journal, old_only=initial)
    return old, target


def _retire_telegram(root: Path, journal: dict[str, Any], *, remove: bool = True,
                     for_full4: bool = False) -> bool:
    before = journal["before"]["containers"]["telegram"]
    target_record = journal["target"]["containers"]["telegram"]
    for name in (".deploy_commit", ".deploy_current"):
        if _pointer(root, name) != journal["pointers"]["target"][name]:
            raise TransactionError("committed pointers changed")
    if manifest.read_manifest(root, journal["transaction_id"]) != journal["target"]["manifest"]:
        raise TransactionError("committed manifest changed")
    old_snap = _snapshot(root, journal["before"]["topology"]["telegram"])
    target_snap = _snapshot(root, journal["target"]["manifest"]["components"]["telegram"])
    old_scope = _inventory(old_snap["project"], old_snap["service"])
    target_scope = (_inventory(target_snap["project"], target_snap["service"])
                    if (old_snap["project"], old_snap["service"]) !=
                    (target_snap["project"], target_snap["service"]) else old_scope)
    ids = {item.get("Id") for item in old_scope + target_scope}
    if ids not in ({target_record["id"]}, {before["id"], target_record["id"]}):
        raise TransactionError("committed Telegram inventory changed")
    key = os.urandom(32)
    target = _observed(target_record["id"], target_snap, key)
    _recipe_matches(target, target_snap)
    if not _same_container_binding(target["container"], target_record):
        raise TransactionError("committed Telegram target changed")
    live_record = target["container"]
    if live_record["running"]:
        _telegram_ready(target_record["id"], target_snap)
        target = _observed(target_record["id"], target_snap, key)
        _recipe_matches(target, target_snap)
        if target["container"] != live_record:
            raise TransactionError("committed Telegram restarted during retirement readiness")
    observations = [target]
    if before["id"] in ids:
        old = _observed(before["id"], old_snap, key)
        _recipe_matches(old, old_snap)
        if old["container"] != {**before, "name": _retained_name(journal, "telegram"), "running": False}:
            raise TransactionError("retained Telegram no longer matches retirement intent")
        observations.append(old)
    db = target["path_settings"]["WUWATERM_DB_PATH"]
    if db is None and target_snap["command"] and target_snap["command"][:1] == ["--db"]:
        db = target_snap["command"][1]
    if db is None or observe.resolve_bind_path(target["recipe"], db, writable=False) != journal["database"]["binding"]["path"]:
        raise TransactionError("committed Telegram database mount changed")
    for observed in observations:
        snapshot = target_snap if observed is target else old_snap
        if for_full4:
            _verify_full4_telegram_paths(journal, observed, snapshot)
        else:
            _verify_current_telegram_paths(journal, observed, snapshot)
    if "api" not in journal["selection"]:
        _other_api(root, journal, key, terminal=True)
    elif manifest.verify_database_binding(journal["database"]["binding"]) != journal["database"]["stat"]:
        raise TransactionError("database stat changed")
    for name in (".deploy_commit", ".deploy_current"):
        if _pointer(root, name) != journal["pointers"]["target"][name]:
            raise TransactionError("committed pointers changed during retirement")
    if before["id"] not in ids:
        return False
    if not remove:
        return True
    _docker("DELETE", "/containers/" + before["id"] + "?v=0&force=0", accepted=(204,))
    if any(item.get("Id") == before["id"] for item in _inventory(old_snap["project"], old_snap["service"])):
        raise TransactionError("committed Telegram retirement could not be reconciled")
    return True


def _update_telegram(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    if (journal["status"] != "active" or journal["phase"] != "prepared"
            or journal["before"]["topology"]["telegram"] is None
            or journal["target"]["containers"]["telegram"] is None
            or journal["target"]["containers"]["telegram"]["id"] is not None):
        raise TransactionError("Telegram journal is not prepared")
    admission.admit_component(root, operation="update", code_root=CODE_ROOT)
    predecessor_raw = _prepare_predecessor(root, compose_path)
    context = _telegram_context(root, journal, compose_path)
    _require_prepared_source(root, journal["source"], compose_path)
    _verify_old_topology(root, journal, compose_path=compose_path, predecessor_raw=predecessor_raw)
    if journal["before"]["manifest"] is not None:
        old = journal["before"]["manifest"]
        if manifest.read_manifest(root, old["deployment_id"]) != old:
            raise TransactionError("old manifest differs")
    _require_target_manifest_absent(root, journal)
    # Empty migrations are an explicit preflight-pending state, not a claim
    # that either state file is absent. The actual-UID probes run only after
    # this durable source/topology intent and before any service mutation.
    journal["migrations"] = []
    _recover_pending_telegram(root, journal, compose_path, finish=False)
    write_initial_journal(root, journal, predecessor_raw=predecessor_raw, compose_path=compose_path)
    try:
        journal["migrations"] = _probe_telegram_state(root, journal, context)
        _write_journal(root, journal)
        _actual_telegram(root, journal, compose_path, initial=True)
        _step(root, journal, "retain-rollback")
        _actual_telegram(root, journal, compose_path)
        old_id = journal["before"]["containers"]["telegram"]["id"]
        _rename(old_id, _retained_name(journal, "telegram"))
        _step(root, journal, "selected-stop")
        old, _ = _actual_telegram(root, journal, compose_path)
        if old["container"]["running"]:
            _container_command("POST", old_id, "stop?t=10")
        _step(root, journal, "state-validate-migrate")
        _actual_telegram(root, journal, compose_path)
        _refresh_telegram_state(root, journal, compose_path)
        _step(root, journal, "selected-activate")
        snap = _snapshot(root, journal["target"]["manifest"]["components"]["telegram"])
        environment = _effective_environment(snap, _image(snap["image_id"]), compose_path, root)
        target_id = _create_from_snapshot(journal, snap, environment, "telegram")
        journal["target"]["containers"]["telegram"]["id"] = target_id
        _write_journal(root, journal)
        _step(root, journal, "actual-health-bindings")
        _, target = _actual_telegram(root, journal, compose_path)
        if target is None or target["container"]["running"]:
            raise TransactionError("replacement Telegram was not created stopped")
        if journal["before"]["containers"]["telegram"]["running"]:
            _container_command("POST", target_id, "start")
            _, target = _actual_telegram(root, journal, compose_path)
            if target is None or not target["container"]["running"]:
                raise TransactionError("replacement Telegram did not start")
            start, count = target["container"]["started_at"], target["container"]["restart_count"]
            _telegram_ready(target_id, snap)
            _, target = _actual_telegram(root, journal, compose_path)
            if (target is None or not target["container"]["running"]
                    or target["container"]["started_at"] != start
                    or target["container"]["restart_count"] != count):
                raise TransactionError("Telegram restarted during polling readiness")
        journal["target"]["containers"]["telegram"].update(target["container"])
        _step(root, journal, "retire-old")
        _actual_telegram(root, journal, compose_path)
        _step(root, journal, "immutable-manifest")
        manifest.write_manifest(root, journal["target"]["manifest"])
        if manifest.manifest_sha256(root, journal["transaction_id"]) != journal["manifest"]["sha256"]:
            raise TransactionError("published manifest differs")
        for phase, name in (("deploy-commit", ".deploy_commit"), ("deploy-current", ".deploy_current")):
            _step(root, journal, phase)
            _actual_telegram(root, journal, compose_path)
            values = [journal["pointers"][which][name] for which in ("old", "target")]
            _replace_pointer(root, name, journal["pointers"]["target"][name], values)
        _actual_telegram(root, journal, compose_path)
        if any(_pointer(root, name) != journal["pointers"]["target"][name]
               for name in (".deploy_commit", ".deploy_current")):
            raise TransactionError("target deployment pointers changed")
        _step(root, journal, "committed", status="committed")
        try:
            _retire_telegram(root, journal)
            retirement = "complete"
        except Exception:
            retirement = "pending"
        return {"status": "committed", "transaction_id": journal["transaction_id"],
                "retired_container_id": old_id, "retirement": retirement}
    except Exception as exc:
        closed_pending = False
        try:
            persisted = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
            if persisted["phase"] == "prepared" and persisted["migrations"] == []:
                _state_module().reconcile_transaction(root, persisted["transaction_id"],
                                                      persisted["source"]["source_tool_sha256"])
                _recover_pending_telegram(root, persisted, compose_path)
                closed_pending = True
        except Exception:
            pass
        if closed_pending:
            raise TransactionError("Telegram state preflight refused; services unchanged") from exc
        raise TransactionError("update interrupted; recover the recorded transaction") from exc


def _create_target(journal: dict[str, Any], snapshot: dict[str, Any], *,
                   root: Path, compose_path: Path) -> str:
    original = observe._inspect(journal["before"]["containers"]["api"]["id"])
    config = original["Config"]
    # Only supported settings were accepted by _observed before copying these
    # actual Docker values. Environment bytes stay in memory and in Docker.
    fields = ("User", "Cmd", "Entrypoint", "WorkingDir", "Volumes", "ExposedPorts",
              "AttachStdin", "AttachStdout", "AttachStderr", "OpenStdin", "StdinOnce", "Tty")
    body = {key: copy.deepcopy(config.get(key)) for key in fields}
    body["Image"] = snapshot["image_id"]
    environment = _effective_environment(snapshot, _image(snapshot["image_id"]), compose_path, root)
    body["Env"] = [name + "=" + value for name, value in sorted(environment.items())]
    body["Labels"] = copy.deepcopy(config.get("Labels") or {})
    body["Labels"][observe.TRANSACTION_LABEL] = journal["transaction_id"]
    body["HostConfig"] = copy.deepcopy(original["HostConfig"])
    result = _docker("POST", "/containers/create?" + urlencode({"name": _target_name(journal, snapshot)}),
                     body, accepted=(201,))
    if type(result) is not dict:
        raise TransactionError("Docker did not identify the API target")
    return runtime._match(result.get("Id"), runtime._HEX64, "API target ID")


def _rename(container_id: str, name: str) -> None:
    runtime._match(container_id, runtime._HEX64, "container ID")
    runtime._match(name, runtime._NAME, "container name")
    _docker("POST", "/containers/" + container_id + "/rename?" + urlencode({"name": name}), accepted=(204,))


def _retire_api(root: Path, journal: dict[str, Any], *, remove: bool = True) -> bool:
    for name in (".deploy_commit", ".deploy_current"):
        if _pointer(root, name) != journal["pointers"]["target"][name]:
            raise TransactionError("committed pointers changed")
    if manifest.read_manifest(root, journal["transaction_id"]) != journal["target"]["manifest"]:
        raise TransactionError("committed manifest changed")
    before = journal["before"]["containers"]["api"]
    target_record = journal["target"]["containers"]["api"]
    old_snap = _snapshot(root, journal["before"]["topology"]["api"])
    target_snap = _snapshot(root, journal["target"]["manifest"]["components"]["api"])
    found = _inventory(old_snap["project"], old_snap["service"])
    ids = {item.get("Id") for item in found}
    if ids not in ({target_record["id"]}, {before["id"], target_record["id"]}):
        raise TransactionError("committed API inventory changed")
    key = os.urandom(32)
    target = _observed(target_record["id"], target_snap, key)
    _recipe_matches(target, target_snap)
    if not _same_container_binding(target["container"], target_record):
        raise TransactionError("committed API target changed")
    live_record = target["container"]
    if live_record["running"]:
        _ready(target_record["id"])
        target = _observed(target_record["id"], target_snap, key)
        _recipe_matches(target, target_snap)
        if target["container"] != live_record:
            raise TransactionError("committed API restarted during retirement readiness")
    observations = [target]
    if before["id"] in ids:
        original = _observed(before["id"], old_snap, key)
        _recipe_matches(original, old_snap)
        expected = {**before, "name": _retained_name(journal), "running": False}
        if original["container"] != expected:
            raise TransactionError("retained API no longer matches retirement intent")
        observations.append(original)
    _verify_data(journal, observations)
    if "telegram" not in journal["selection"]:
        _other_component(root, journal, key, terminal=True)
    for name in (".deploy_commit", ".deploy_current"):
        if _pointer(root, name) != journal["pointers"]["target"][name]:
            raise TransactionError("committed pointers changed during retirement")
    if before["id"] not in ids:
        return False
    if not remove:
        return True
    _docker("DELETE", "/containers/" + before["id"] + "?v=0&force=0", accepted=(204,))
    remaining = _inventory(old_snap["project"], old_snap["service"])
    if {item.get("Id") for item in remaining} != {target_record["id"]}:
        raise TransactionError("committed API retirement could not be reconciled")
    return True


def _retire_committed(root: Path, journal: dict[str, Any], *, for_full4: bool = False) -> bool:
    """Reconcile the committed intent against real objects before retirement."""
    admission.require_lock(root)
    if journal["status"] != "committed":
        raise TransactionError("terminal retirement is unsupported")
    endpoints = _full4_record_route(journal, for_full4=for_full4)
    if journal["selection"] == ["api"]:
        return _retire_api(root, journal)
    if journal["selection"] == ["telegram"]:
        return _retire_telegram(root, journal, for_full4=endpoints)
    if journal["selection"] != ["api", "telegram"]:
        raise TransactionError("terminal retirement is unsupported")
    api_pending = _retire_api(root, journal, remove=False)
    tg_pending = _retire_telegram(root, journal, remove=False, for_full4=endpoints)
    if api_pending:
        _retire_telegram(root, journal, remove=False, for_full4=endpoints)
        _retire_api(root, journal)
    if tg_pending:
        _retire_api(root, journal, remove=False)
        _retire_telegram(root, journal, for_full4=endpoints)
    return api_pending or tg_pending


def _prepare_predecessor(root: Path, compose_path: Path, *, for_full4: bool = False) -> bytes | None:
    verify = verify_terminal_for_full4 if for_full4 else verify_terminal
    terminal = verify(root, compose_path=compose_path)
    if terminal is None:
        return None
    raw, journal = terminal
    if journal["kind"] == "component-full":
        import component_full as full  # noqa: PLC0415
        full.terminal_health(root, journal)
    else:
        _terminal_health(root, journal)
    verified = verify(root, compose_path=compose_path)
    if verified is None or verified[0] != raw:
        raise TransactionError("terminal predecessor changed during readiness")
    if journal["action"] == "update" and journal["status"] == "committed":
        if journal["kind"] == "component-full":
            full.retire_committed(root, journal, compose_path=compose_path)
        else:
            _retire_committed(root, journal, for_full4=for_full4)
        verified = verify(root, compose_path=compose_path)
        if verified is None or verified[0] != raw:
            raise TransactionError("terminal predecessor changed during retirement")
    return raw


def _update_both(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    if (journal["status"] != "active" or journal["phase"] != "prepared"
            or any(journal["before"]["topology"][name] is None for name in ("api", "telegram"))
            or any(journal["target"]["containers"][name]["id"] is not None for name in ("api", "telegram"))):
        raise TransactionError("both-component journal is not prepared")
    admission.admit_component(root, operation="update", code_root=CODE_ROOT)
    predecessor_raw = _prepare_predecessor(root, compose_path)
    _verify_candidate(root, journal)
    context = _telegram_context(root, journal, compose_path)
    _require_prepared_source(root, journal["source"], compose_path)
    _verify_old_topology(root, journal, compose_path=compose_path, predecessor_raw=predecessor_raw)
    if journal["before"]["manifest"] is not None:
        old = journal["before"]["manifest"]
        if manifest.read_manifest(root, old["deployment_id"]) != old:
            raise TransactionError("old manifest differs")
    _require_target_manifest_absent(root, journal)
    if journal["before"]["containers"]["api"]["id"] == journal["before"]["containers"]["telegram"]["id"]:
        raise TransactionError("selected components share one container")
    journal["migrations"] = []
    _recover_pending_telegram(root, journal, compose_path, finish=False)
    write_initial_journal(root, journal, predecessor_raw=predecessor_raw, compose_path=compose_path)
    try:
        journal["migrations"] = _probe_telegram_state(root, journal, context)
        _write_journal(root, journal)
        _actual(root, journal, compose_path, initial=True)
        _actual_telegram(root, journal, compose_path, initial=True)
        _step(root, journal, "retain-rollback")
        for name in ("api", "telegram"):
            _actual(root, journal, compose_path)
            _actual_telegram(root, journal, compose_path)
            _rename(journal["before"]["containers"][name]["id"], _retained_name(journal, name))
        _step(root, journal, "selected-stop")
        for name, observe_current in (("telegram", _actual_telegram), ("api", _actual)):
            old, _ = observe_current(root, journal, compose_path)
            if old["container"]["running"]:
                _container_command("POST", old["container"]["id"], "stop?t=10")
        _step(root, journal, "state-validate-migrate")
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        _refresh_telegram_state(root, journal, compose_path)
        _step(root, journal, "selected-activate")
        api_snap = _snapshot(root, journal["target"]["manifest"]["components"]["api"])
        tg_snap = _snapshot(root, journal["target"]["manifest"]["components"]["telegram"])
        api_id = _create_target(journal, api_snap, root=root, compose_path=compose_path)
        journal["target"]["containers"]["api"]["id"] = api_id
        _write_journal(root, journal)
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        environment = _effective_environment(tg_snap, _image(tg_snap["image_id"]), compose_path, root)
        tg_id = _create_from_snapshot(journal, tg_snap, environment, "telegram")
        journal["target"]["containers"]["telegram"]["id"] = tg_id
        _write_journal(root, journal)
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        for name, container_id in (("api", api_id), ("telegram", tg_id)):
            if journal["before"]["containers"][name]["running"]:
                _container_command("POST", container_id, "start")
                _actual(root, journal, compose_path)
                _actual_telegram(root, journal, compose_path)
        _step(root, journal, "actual-health-bindings")
        _, api = _actual(root, journal, compose_path)
        _, tg = _actual_telegram(root, journal, compose_path)
        if api is None or tg is None:
            raise TransactionError("selected target disappeared")
        for name, item, container_id, snapshot in (("api", api, api_id, api_snap), ("telegram", tg, tg_id, tg_snap)):
            if item["container"]["running"] != journal["before"]["containers"][name]["running"]:
                raise TransactionError("selected running state changed")
            if item["container"]["running"]:
                start, count = item["container"]["started_at"], item["container"]["restart_count"]
                if name == "api":
                    _ready(container_id)
                    _, checked = _actual(root, journal, compose_path)
                else:
                    _telegram_ready(container_id, snapshot)
                    _, checked = _actual_telegram(root, journal, compose_path)
                if (checked is None or not checked["container"]["running"]
                        or checked["container"]["started_at"] != start
                        or checked["container"]["restart_count"] != count):
                    raise TransactionError("selected component restarted during readiness")
        _, api = _actual(root, journal, compose_path)
        _, tg = _actual_telegram(root, journal, compose_path)
        if api is None or tg is None:
            raise TransactionError("selected target disappeared")
        journal["target"]["containers"]["api"].update(api["container"])
        journal["target"]["containers"]["telegram"].update(tg["container"])
        _step(root, journal, "retire-old")
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        _step(root, journal, "immutable-manifest")
        manifest.write_manifest(root, journal["target"]["manifest"])
        if manifest.manifest_sha256(root, journal["transaction_id"]) != journal["manifest"]["sha256"]:
            raise TransactionError("published manifest differs")
        for phase, name in (("deploy-commit", ".deploy_commit"), ("deploy-current", ".deploy_current")):
            _step(root, journal, phase)
            _actual(root, journal, compose_path)
            _actual_telegram(root, journal, compose_path)
            values = [journal["pointers"][which][name] for which in ("old", "target")]
            _replace_pointer(root, name, journal["pointers"]["target"][name], values)
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        if any(_pointer(root, name) != journal["pointers"]["target"][name]
               for name in (".deploy_commit", ".deploy_current")):
            raise TransactionError("target deployment pointers changed")
        _step(root, journal, "committed", status="committed")
        try:
            _retire_committed(root, journal)
            retirement = "complete"
        except Exception:
            retirement = "pending"
        return {"status": "committed", "transaction_id": journal["transaction_id"],
                "retired_container_ids": {name: journal["before"]["containers"][name]["id"]
                                          for name in ("api", "telegram")}, "retirement": retirement}
    except Exception as exc:
        closed_pending = False
        try:
            persisted = runtime.load_journal(root / ".deployments" / runtime.JOURNAL_NAME)
            if persisted["phase"] == "prepared" and persisted["migrations"] == []:
                _state_module().reconcile_transaction(root, persisted["transaction_id"],
                                                      persisted["source"]["source_tool_sha256"])
                _recover_pending_telegram(root, persisted, compose_path)
                closed_pending = True
        except Exception:
            pass
        if closed_pending:
            raise TransactionError("Telegram state preflight refused; services unchanged") from exc
        raise TransactionError("update interrupted; recover the recorded transaction") from exc


def update(root: Path, journal: dict[str, Any], *, compose_path: Path) -> dict[str, Any]:
    """Execute one admitted, registered API-only update from a prepared journal.

    The caller supplies the immutable snapshots and complete runtime2 journal;
    this function independently checks their source, build, live preimage,
    state and environment before its first service mutation.
    """
    root = admission._physical_root(root)
    journal = copy.deepcopy(runtime.validate_journal(journal))
    if journal["action"] == "update" and journal["selection"] == ["api", "telegram"]:
        return _update_both(root, journal, compose_path)
    if journal["action"] == "update" and journal["selection"] == ["telegram"]:
        return _update_telegram(root, journal, compose_path)
    if (journal["action"] != "update" or journal["selection"] != ["api"]
            or journal["status"] != "active" or journal["phase"] != "prepared"
            or journal["before"]["topology"]["api"] is None
            or journal["target"]["containers"]["api"]["id"] is not None):
        raise TransactionError("only a prepared API-only update is supported")
    admission.admit_component(root, operation="update", code_root=CODE_ROOT)
    predecessor_raw = _prepare_predecessor(root, compose_path)
    _verify_candidate(root, journal)
    _require_prepared_source(root, journal["source"], compose_path)
    _verify_old_topology(root, journal, compose_path=compose_path, predecessor_raw=predecessor_raw)
    if journal["before"]["manifest"] is not None:
        old = journal["before"]["manifest"]
        if manifest.read_manifest(root, old["deployment_id"]) != old:
            raise TransactionError("old manifest differs")
    _require_target_manifest_absent(root, journal)
    _actual(root, journal, compose_path, initial=True)
    write_initial_journal(root, journal, predecessor_raw=predecessor_raw, compose_path=compose_path)
    try:
        _step(root, journal, "retain-rollback")
        _actual(root, journal, compose_path)
        _rename(journal["before"]["containers"]["api"]["id"], _retained_name(journal))
        _step(root, journal, "selected-stop")
        old, _ = _actual(root, journal, compose_path)
        if old["container"]["running"]:
            _container_command("POST", old["container"]["id"], "stop?t=10")
        _step(root, journal, "selected-activate")
        _actual(root, journal, compose_path)
        snap = _snapshot(root, journal["target"]["manifest"]["components"]["api"])
        target_id = _create_target(journal, snap, root=root, compose_path=compose_path)
        journal["target"]["containers"]["api"]["id"] = target_id
        _write_journal(root, journal)
        _, target = _actual(root, journal, compose_path)
        if target is None or target["container"]["running"]:
            raise TransactionError("new API was not created stopped")
        if journal["before"]["containers"]["api"]["running"]:
            _container_command("POST", target_id, "start")
        _step(root, journal, "actual-health-bindings")
        _, target = _actual(root, journal, compose_path)
        if target is None or target["container"]["running"] != journal["before"]["containers"]["api"]["running"]:
            raise TransactionError("API running state differs")
        if target["container"]["running"]:
            started = target["container"]["started_at"]
            restarts = target["container"]["restart_count"]
            _ready(target_id)
            _, target = _actual(root, journal, compose_path)
            if (target is None or not target["container"]["running"]
                    or target["container"]["started_at"] != started
                    or target["container"]["restart_count"] != restarts):
                raise TransactionError("API restarted during readiness")
        journal["target"]["containers"]["api"].update(target["container"])
        _step(root, journal, "immutable-manifest")
        _actual(root, journal, compose_path)
        manifest.write_manifest(root, journal["target"]["manifest"])
        if manifest.manifest_sha256(root, journal["transaction_id"]) != journal["manifest"]["sha256"]:
            raise TransactionError("published manifest differs")
        for phase, name in (("deploy-commit", ".deploy_commit"), ("deploy-current", ".deploy_current")):
            _step(root, journal, phase)
            _actual(root, journal, compose_path)
            values = [journal["pointers"][which][name] for which in ("old", "target")]
            _replace_pointer(root, name, journal["pointers"]["target"][name], values)
        _actual(root, journal, compose_path)
        if any(_pointer(root, name) != journal["pointers"]["target"][name]
               for name in (".deploy_commit", ".deploy_current")):
            raise TransactionError("target deployment pointers changed")
        _step(root, journal, "committed", status="committed")
        try:
            _retire_committed(root, journal)
            retirement = "complete"
        except Exception:
            retirement = "pending"
        return {"status": "committed", "transaction_id": journal["transaction_id"],
                "retired_container_id": journal["before"]["containers"]["api"]["id"],
                "retirement": retirement}
    except Exception as exc:
        # The journal remains the source of truth; no guessed rollback is
        # attempted when reconciliation itself fails.
        raise TransactionError("update interrupted; recover the recorded transaction") from exc


def _sync_recovered_journal(root: Path, expected: dict[str, Any]) -> None:
    """Make an observed, singly linked journal durable before recovery acts."""
    admission.require_lock(root)
    path = root / ".deployments" / runtime.JOURNAL_NAME
    before_raw = runtime._read_regular(path)
    if runtime._decode(before_raw) != expected:
        raise TransactionError("recovery journal changed before sync")
    with manifest._directory(root, (".deployments",), create=False) as directory:
        before = os.stat(runtime.JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise TransactionError("recovery journal is not a singly linked file")
        os.fsync(directory)
        after = os.stat(runtime.JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
        def fields(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    info.st_mode, info.st_uid, info.st_gid, info.st_nlink)
        if fields(before) != fields(after) or runtime._read_regular(path) != before_raw:
            raise TransactionError("recovery journal changed during sync")


def recover(root: Path, transaction_id: str, source_tool_sha256: str, *,
            compose_path: Path) -> dict[str, Any]:
    """Restore actual API objects and both pointer preimages from saved A.

    Call this only from the saved source extraction. Admission independently
    verifies that extraction, the source capsule, root-bound FD9, conflicting
    journals, and old/target pointer combinations; no source from root B is
    imported. The original container is retained through the commit boundary.
    """
    root = admission._physical_root(root)
    pending = runtime.pending_adopt_probe_staging(root)
    if pending is not None:
        admission.require_lock(root)
        path = root / ".deployments" / runtime.JOURNAL_NAME
        raw = runtime._read_regular(path)
        candidate = runtime.validate_journal(runtime._decode(raw))
        if (candidate["transaction_id"] != transaction_id or candidate["kind"] != "component-runtime"
                or candidate["status"] not in ("active", "unresolved") or candidate["action"] != "update"
                or "telegram" not in candidate["selection"]
                or candidate["source"]["source_tool_sha256"] != source_tool_sha256):
            raise TransactionError("pending state probe does not match the recovery journal")
        admission._verify_recovery_extraction(root, candidate["source"], CODE_ROOT)
        if ("compose" in candidate["source"]
                and recovery._verify_bound_compose(root, candidate["source"]) != compose_path):
            raise TransactionError("pending state probe Compose identity differs")
        runtime.reconcile_adopt_probe_staging(root, pending, source_tool_sha256=source_tool_sha256,
                                               transaction_id=transaction_id)
        if runtime._read_regular(path) != raw:
            raise TransactionError("runtime journal changed during probe staging recovery")
    journal = admission.admit_component_recovery(
        root, transaction_id=transaction_id, source_tool_sha256=source_tool_sha256, code_root=CODE_ROOT,
    )
    if "compose" in journal["source"] and recovery._verify_bound_compose(root, journal["source"]) != compose_path:
        raise TransactionError("Compose executable differs from the recorded recovery source")
    _sync_recovered_journal(root, journal)
    if journal["schema_version"] in (4, 5):
        import component_full as full  # noqa: PLC0415
        full.finish_handoff(root, successor_kind="runtime", successor=journal)
    if journal["status"] == "committed":
        raw = runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME)
        _verify_terminal_record(root, journal, raw, compose_path)
        health = _terminal_health(root, journal)
        if journal["action"] == "update":
            _retire_committed(root, journal)
            _verify_terminal_record(root, journal, raw, compose_path)
            health = _terminal_health(root, journal)
        return {"status": "committed", "transaction_id": transaction_id,
                "already_committed": True, "health": health}
    if journal["action"] == "adopt":
        return _recover_adoption(root, journal, compose_path)
    if "telegram" in journal["selection"]:
        try:
            _state_module().reconcile_transaction(root, transaction_id, source_tool_sha256)
        except AttributeError as exc:
            raise TransactionError("Telegram state-probe recovery is unavailable") from exc
    if "telegram" in journal["selection"] and journal["migrations"] == []:
        return _recover_pending_telegram(root, journal, compose_path)
    if journal["action"] == "update" and journal["selection"] == ["api", "telegram"]:
        return _recover_both(root, journal, compose_path)
    if journal["action"] == "update" and journal["selection"] == ["telegram"]:
        return _recover_telegram(root, journal, compose_path)
    if (journal["action"] != "update" or journal["selection"] != ["api"]
            or journal["before"]["topology"]["api"] is None):
        raise TransactionError("only API-only update recovery is supported")
    old, target = _actual(root, journal, compose_path)
    before = journal["before"]["containers"]["api"]
    if journal["status"] == "rolled_back":
        if (target is not None or old["container"]["name"] != before["name"]
                or old["container"]["running"] != before["running"]):
            raise TransactionError("terminal rollback differs from actual services")
        _check_pointers(root, journal, old_only=True)
        return {"status": "rolled_back", "transaction_id": transaction_id, "already_restored": True}
    _step(root, journal, "rollback-stop")
    if target is not None and target["container"]["running"]:
        _container_command("POST", target["container"]["id"], "stop?t=10")
    old, target = _actual(root, journal, compose_path)
    if target is not None and target["container"]["running"]:
        raise TransactionError("API target did not stop")
    _step(root, journal, "rollback-restore")
    if target is not None:
        # Only the exact observed stopped transaction-labelled container may be
        # removed; volumes and bind-mounted state are never removed.
        target_id = target["container"]["id"]
        _docker("DELETE", "/containers/" + target_id + "?v=0&force=0", accepted=(204,))
    old, target = _actual(root, journal, compose_path)
    if target is not None:
        raise TransactionError("API target remains after removal")
    if old["container"]["name"] == _retained_name(journal):
        _rename(old["container"]["id"], before["name"])
    old, _ = _actual(root, journal, compose_path)
    if old["container"]["name"] != before["name"]:
        raise TransactionError("original API name was not restored")
    if before["running"] and not old["container"]["running"]:
        _container_command("POST", old["container"]["id"], "start")
    old, _ = _actual(root, journal, compose_path)
    if old["container"]["running"] != before["running"]:
        raise TransactionError("original API running state was not restored")
    if before["running"]:
        start, count = old["container"]["started_at"], old["container"]["restart_count"]
        _ready(before["id"])
        old, _ = _actual(root, journal, compose_path)
        if (not old["container"]["running"] or old["container"]["started_at"] != start
                or old["container"]["restart_count"] != count):
            raise TransactionError("restored API restarted during readiness")
    _step(root, journal, "rollback-pointers")
    for name in (".deploy_current", ".deploy_commit"):
        _actual(root, journal, compose_path)
        values = [journal["pointers"][which][name] for which in ("old", "target")]
        _replace_pointer(root, name, journal["pointers"]["old"][name], values)
    _actual(root, journal, compose_path)
    _check_pointers(root, journal, old_only=True)
    _step(root, journal, "rolled-back", status="rolled_back")
    return {"status": "rolled_back", "transaction_id": transaction_id, "already_restored": False}


def _verify_adoption_recovery(root: Path, journal: dict[str, Any], compose_path: Path) -> None:
    if journal["action"] != "adopt" or journal["selection"] != []:
        raise TransactionError("journal is not a metadata-only adoption")
    _verify_saved_source(root, journal["source"])
    _verify_old_topology(root, journal, compose_path=compose_path, allow_probe=False)
    _check_pointers(root, journal)
    if journal["before"]["manifest"] is not None:
        before = journal["before"]["manifest"]
        if manifest.read_manifest(root, before["deployment_id"]) != before:
            raise TransactionError("adoption preimage manifest differs")
    pointer_is_target = _pointer(root, ".deploy_current") == journal["pointers"]["target"][".deploy_current"]
    _verify_manifest_if_present(root, journal, required=pointer_is_target)
    _verify_live_topology(root, journal, which="before", compose_path=compose_path)


def _recover_adoption(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    """Restore metadata only; never change an adopted serving container."""
    _verify_adoption_recovery(root, journal, compose_path)
    if journal["status"] == "rolled_back":
        _exact_pointers(root, journal, "old")
        return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
                "already_restored": True, "polling_health": "unconfirmed"}
    _step(root, journal, "rollback-pointers")
    for name in (".deploy_current", ".deploy_commit"):
        _verify_adoption_recovery(root, journal, compose_path)
        allowed = [journal["pointers"][which][name] for which in ("old", "target")]
        _replace_pointer(root, name, journal["pointers"]["old"][name], allowed)
    _verify_adoption_recovery(root, journal, compose_path)
    _exact_pointers(root, journal, "old")
    _step(root, journal, "rolled-back", status="rolled_back")
    return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
            "already_restored": False, "polling_health": "unconfirmed"}


def _recover_both(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    api_old, api = _actual(root, journal, compose_path)
    tg_old, tg = _actual_telegram(root, journal, compose_path)
    before = journal["before"]["containers"]
    if journal["status"] == "rolled_back":
        if (api is not None or tg is not None or
                any(actual["container"]["name"] != before[name]["name"] or
                    actual["container"]["running"] != before[name]["running"]
                    for name, actual in (("api", api_old), ("telegram", tg_old)))):
            raise TransactionError("terminal rollback differs from actual services")
        _check_pointers(root, journal, old_only=True)
        return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
                "already_restored": True, "polling_health": "unconfirmed"}
    _step(root, journal, "rollback-stop")
    for target in (tg, api):
        if target is not None and target["container"]["running"]:
            _container_command("POST", target["container"]["id"], "stop?t=10")
    _, api = _actual(root, journal, compose_path)
    _, tg = _actual_telegram(root, journal, compose_path)
    if any(item is not None and item["container"]["running"] for item in (api, tg)):
        raise TransactionError("selected targets did not stop")
    _preserved_state_rollback(root, journal)
    _step(root, journal, "rollback-restore")
    for target in (tg, api):
        if target is not None:
            _docker("DELETE", "/containers/" + target["container"]["id"] + "?v=0&force=0", accepted=(204,))
    api_old, api = _actual(root, journal, compose_path)
    tg_old, tg = _actual_telegram(root, journal, compose_path)
    if api is not None or tg is not None:
        raise TransactionError("selected target remains after removal")
    for name, old in (("api", api_old), ("telegram", tg_old)):
        if old["container"]["name"] == _retained_name(journal, name):
            _rename(old["container"]["id"], before[name]["name"])
    for name, observe_current in (("api", _actual), ("telegram", _actual_telegram)):
        old, _ = observe_current(root, journal, compose_path)
        if old["container"]["name"] != before[name]["name"]:
            raise TransactionError("selected original name was not restored")
        if before[name]["running"] and not old["container"]["running"]:
            _container_command("POST", old["container"]["id"], "start")
    api_old, _ = _actual(root, journal, compose_path)
    tg_old, _ = _actual_telegram(root, journal, compose_path)
    for name, old in (("api", api_old), ("telegram", tg_old)):
        if old["container"]["running"] != before[name]["running"]:
            raise TransactionError("selected original running state was not restored")
    if before["api"]["running"]:
        start, count = api_old["container"]["started_at"], api_old["container"]["restart_count"]
        _ready(before["api"]["id"])
        api_old, _ = _actual(root, journal, compose_path)
        if (not api_old["container"]["running"] or api_old["container"]["started_at"] != start
                or api_old["container"]["restart_count"] != count):
            raise TransactionError("restored API restarted during readiness")
    if before["telegram"]["running"]:
        start, count = tg_old["container"]["started_at"], tg_old["container"]["restart_count"]
        time.sleep(1)
        tg_old, _ = _actual_telegram(root, journal, compose_path)
        if (not tg_old["container"]["running"] or tg_old["container"]["started_at"] != start
                or tg_old["container"]["restart_count"] != count):
            raise TransactionError("restored Telegram process did not remain running")
    _step(root, journal, "rollback-pointers")
    for name in (".deploy_current", ".deploy_commit"):
        _actual(root, journal, compose_path)
        _actual_telegram(root, journal, compose_path)
        values = [journal["pointers"][which][name] for which in ("old", "target")]
        _replace_pointer(root, name, journal["pointers"]["old"][name], values)
    _actual(root, journal, compose_path)
    _actual_telegram(root, journal, compose_path)
    _check_pointers(root, journal, old_only=True)
    _step(root, journal, "rolled-back", status="rolled_back")
    return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
            "already_restored": False, "polling_health": "unconfirmed"}


def _recover_telegram(root: Path, journal: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    old, target = _actual_telegram(root, journal, compose_path)
    before = journal["before"]["containers"]["telegram"]
    if journal["status"] == "rolled_back":
        if (target is not None or old["container"]["name"] != before["name"]
                or old["container"]["running"] != before["running"]):
            raise TransactionError("terminal Telegram rollback differs from actual services")
        _check_pointers(root, journal, old_only=True)
        return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
                "already_restored": True, "polling_health": "unconfirmed"}
    _step(root, journal, "rollback-stop")
    if target is not None and target["container"]["running"]:
        _container_command("POST", target["container"]["id"], "stop?t=10")
    _, target = _actual_telegram(root, journal, compose_path)
    if target is not None and target["container"]["running"]:
        raise TransactionError("Telegram target did not stop")
    _preserved_state_rollback(root, journal)
    _step(root, journal, "rollback-restore")
    if target is not None:
        _docker("DELETE", "/containers/" + target["container"]["id"] + "?v=0&force=0", accepted=(204,))
    old, target = _actual_telegram(root, journal, compose_path)
    if target is not None:
        raise TransactionError("Telegram target remains after removal")
    if old["container"]["name"] == _retained_name(journal, "telegram"):
        _rename(old["container"]["id"], before["name"])
    old, _ = _actual_telegram(root, journal, compose_path)
    if old["container"]["name"] != before["name"]:
        raise TransactionError("Telegram preimage name was not restored")
    if before["running"] and not old["container"]["running"]:
        _container_command("POST", old["container"]["id"], "start")
    old, _ = _actual_telegram(root, journal, compose_path)
    if old["container"]["running"] != before["running"]:
        raise TransactionError("Telegram running preimage was not restored")
    if before["running"]:
        start, count = old["container"]["started_at"], old["container"]["restart_count"]
        time.sleep(1)
        old, _ = _actual_telegram(root, journal, compose_path)
        if (not old["container"]["running"] or old["container"]["started_at"] != start
                or old["container"]["restart_count"] != count):
            raise TransactionError("restored Telegram process did not remain running")
    _step(root, journal, "rollback-pointers")
    for name in (".deploy_current", ".deploy_commit"):
        _actual_telegram(root, journal, compose_path)
        values = [journal["pointers"][which][name] for which in ("old", "target")]
        _replace_pointer(root, name, journal["pointers"]["old"][name], values)
    _actual_telegram(root, journal, compose_path)
    _check_pointers(root, journal, old_only=True)
    _step(root, journal, "rolled-back", status="rolled_back")
    return {"status": "rolled_back", "transaction_id": journal["transaction_id"],
            "already_restored": False, "polling_health": "unconfirmed"}
