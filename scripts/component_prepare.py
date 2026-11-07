#!/usr/bin/env python3
"""Construct a runtime update from an admitted, observed deployment.

The transaction coordinator remains the only publisher of a prepared journal.
Environment contents are used in memory and never written to the journal.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_build as build  # noqa: E402
import component_observe as observe  # noqa: E402
import component_recovery as recovery  # noqa: E402
import component_runtime as runtime  # noqa: E402
import component_transaction as transaction  # noqa: E402
import component_wheel_build as wheel  # noqa: E402
import deployment_admission as admission  # noqa: E402
import package_recovery_source as capsule  # noqa: E402
import product_manifest as manifest  # noqa: E402


class PrepareError(RuntimeError):
    """A runtime update cannot be safely prepared from the given inputs."""


def _request(value: Any) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
    if type(value) is not dict or set(value) != {"selection", "receipts", "recipes"}:
        raise PrepareError("invalid update request")
    selections = {"api": ["api"], "telegram": ["telegram"], "both": ["api", "telegram"]}
    selection = selections.get(value["selection"]) if type(value["selection"]) is str else None
    receipts, recipes = value["receipts"], value["recipes"]
    if selection is None or type(receipts) is not dict or set(receipts) != set(selection):
        raise PrepareError("one receipt is required for each selected component")
    if type(recipes) is not dict or not set(recipes).issubset(selection):
        raise PrepareError("recipes must refer only to selected components")
    for digest in receipts.values():
        runtime._match(digest, runtime._HEX64, "receipt")
    return selection, receipts, recipes


def _references(recipe: Any) -> tuple[dict[str, Any], dict[str, tuple[Any, ...]]]:
    required = {"compose_files", "project_directory", "project", "service", "interpolation_env_files"}
    if type(recipe) is not dict or set(recipe) != required:
        raise PrepareError("invalid Compose recipe references")
    manifest._name(recipe["project"], "Compose project")
    manifest._name(recipe["service"], "Compose service")
    directory = Path(manifest._path(recipe["project_directory"], "Compose project directory"))
    if directory.resolve(strict=True) != directory or not directory.is_dir():
        raise PrepareError("Compose project directory is not physical")
    for key in ("compose_files", "interpolation_env_files"):
        if type(recipe[key]) is not list or (key == "compose_files" and not recipe[key]):
            raise PrepareError("invalid Compose file references")
        if len(set(recipe[key])) != len(recipe[key]):
            raise PrepareError("duplicate Compose file reference")
    paths = recipe["compose_files"] + recipe["interpolation_env_files"]
    for path in paths:
        manifest._path(path, "Compose file reference")
    return recipe, {path: runtime._compose_input(Path(path)) for path in paths}


def _render_recipe(root: Path, recipe: dict[str, Any], image: dict[str, Any],
                   compose_path: Path) -> tuple[dict[str, Any], dict[str, tuple[Any, ...]]]:
    """Render the deliberately bounded replayable Compose service subset."""
    recipe, references = _references(recipe)
    binary = transaction._compose_binary(compose_path)
    command = [str(binary), "--project-name", recipe["project"], "--project-directory",
               recipe["project_directory"]]
    for filename in recipe["compose_files"]:
        command += ["--file", filename]
    # An explicit empty interpolation source prevents ambient .env or host
    # process variables from silently altering the rendered recipe.
    for filename in recipe["interpolation_env_files"] or ["/dev/null"]:
        command += ["--env-file", filename]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

    def render(*flags: str) -> dict[str, Any]:
        process = subprocess.run([*command, "config", *flags, "--format", "json"],
                                 cwd=recipe["project_directory"], env=env, pass_fds=(9,),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        if process.returncode or len(process.stdout) > 8 * 1024 * 1024:
            raise PrepareError("Compose target cannot be rendered")
        document = runtime._decode(process.stdout)
        if type(document) is not dict or type(document.get("services")) is not dict:
            raise PrepareError("Compose target is invalid")
        service = document["services"].get(recipe["service"])
        if type(service) is not dict:
            raise PrepareError("Compose target service is absent")
        return service

    unresolved = render("--no-env-resolution")
    env_files = unresolved.get("env_file") or []
    if type(env_files) is not list:
        raise PrepareError("Compose env files are invalid")
    for entry in env_files:
        if type(entry) is not dict or set(entry) - {"path", "required"} or entry.get("required", True) is not True:
            raise PrepareError("optional or unsupported env files cannot be replayed")
        path = manifest._path(entry.get("path"), "Compose env file")
        identity = runtime._compose_input(Path(path))
        if path in references and references[path] != identity:
            raise PrepareError("Compose input changed during rendering")
        references[path] = identity
    resolved = render()
    if render("--no-env-resolution") != unresolved or any(
        runtime._compose_input(Path(path)) != identity for path, identity in references.items()
    ):
        raise PrepareError("Compose input changed during rendering")
    snapshot = _snapshot_from_compose(recipe, unresolved, resolved, image)
    expected = transaction._env_map(image.get("Config", {}).get("Env"))
    for name, value in (resolved.get("environment") or {}).items():
        if type(name) is not str or not manifest._ENV_NAME.fullmatch(name):
            raise PrepareError("Compose target environment is invalid")
        if value is None:
            expected.pop(name, None)
        elif type(value) is str:
            expected[name] = value
        else:
            raise PrepareError("Compose target environment is invalid")
    if transaction._effective_environment(snapshot, image, binary, root) != expected:
        raise PrepareError("Compose target environment cannot be reproduced from saved references")
    if any(runtime._compose_input(Path(path)) != identity for path, identity in references.items()):
        raise PrepareError("Compose input changed during rendering")
    return snapshot, references


def _snapshot_from_compose(refs: dict[str, Any], unresolved: dict[str, Any],
                           resolved: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    # The transaction engine can only reproduce this subset. Unknown fields
    # must not be silently discarded, even when Compose itself accepts them.
    allowed = {"image", "container_name", "command", "entrypoint", "environment", "env_file",
               "network_mode", "restart", "volumes", "tmpfs", "user", "working_dir"}
    if set(unresolved) - allowed or set(resolved) - (allowed - {"env_file"}):
        raise PrepareError("Compose target contains unsupported runtime settings")
    if unresolved.get("network_mode") not in ("host", "none"):
        raise PrepareError("Compose target must use host or none networking")
    image_config = image.get("Config")
    if type(image_config) is not dict:
        raise PrepareError("target image configuration is unavailable")
    explicit = unresolved.get("environment") or {}
    rendered = resolved.get("environment") or {}
    if type(explicit) is not dict or type(rendered) is not dict:
        raise PrepareError("Compose target environment is invalid")
    overrides = []
    for name in sorted(explicit):
        value = rendered.get(name)
        if type(value) is not str:
            raise PrepareError("Compose environment override cannot be reproduced")
        if value == "":
            overrides.append({"name": name, "mode": "empty"})
        elif name in manifest._LITERAL_ENV:
            overrides.append({"name": name, "mode": "literal", "value": value})
        else:
            raise PrepareError("nonempty secret or unsupported Compose override")
    mounts = []
    volumes = unresolved.get("volumes") or []
    if type(volumes) is not list:
        raise PrepareError("Compose volumes are invalid")
    for item in volumes:
        if (type(item) is not dict or set(item) - {"type", "source", "target", "read_only", "bind"}
                or item.get("type") != "bind" or item.get("bind", {}) != {}
                or type(item.get("read_only", False)) is not bool):
            raise PrepareError("Compose volume cannot be replayed")
        mounts.append({"host": item.get("source"), "container": item.get("target"),
                       "read_only": item.get("read_only", False)})
    tmpfs = unresolved.get("tmpfs") or []
    if type(tmpfs) is not list:
        raise PrepareError("Compose tmpfs is invalid")
    for item in tmpfs:
        if type(item) is not str:
            raise PrepareError("Compose tmpfs is invalid")
        target, separator, options = item.partition(":")
        mounts.append({"type": "tmpfs", "container": target,
                       "options": options.split(",") if separator and options else []})
    mounts.sort(key=lambda item: item.get("container") or "")
    restart = unresolved.get("restart", "no")
    retry = 0
    if type(restart) is str and restart.startswith("on-failure:"):
        count = restart.removeprefix("on-failure:")
        if not count.isascii() or not count.isdigit():
            raise PrepareError("Compose restart policy is invalid")
        restart, retry = "on-failure", int(count)
    snapshot = {
        "schema_version": 1, "image_id": image.get("Id"), "project": refs["project"],
        "service": refs["service"], "container_name": unresolved.get("container_name"),
        "command": unresolved.get("command", image_config.get("Cmd")),
        "entrypoint": unresolved.get("entrypoint", image_config.get("Entrypoint")),
        "mounts": mounts, "env_files": [entry["path"] for entry in unresolved.get("env_file", [])],
        "environment_overrides": overrides, "network_mode": unresolved["network_mode"], "ports": [],
        "restart": {"name": restart, "maximum_retry_count": retry},
        "user": unresolved.get("user", image_config.get("User")) or None,
        "working_dir": unresolved.get("working_dir", image_config.get("WorkingDir")) or None,
        "runtime_options": {"read_only_root": False, "privileged": False, "init": None,
                            "pid_mode": "", "ipc_mode": "private", "cap_add": [], "cap_drop": [],
                            "security_opt": [], "dns": [], "extra_hosts": []},
    }
    manifest.validate_recipe_snapshot(snapshot)
    return snapshot


def _candidate(name: str, digest: str, receipt: dict[str, Any], recipe: dict[str, Any]) -> dict[str, Any]:
    source = {"revision_claim": receipt["source"]["commit"], "verified_commit": receipt["source"]["commit"],
              "verified_tree": receipt["source"]["tree"]}
    if name == "api":
        version = receipt["installed_core_version"]
        core = {"version": version, **source}
        provenance = {"kind": "source-image", "build_recipe_sha256": receipt["recipe_sha256"],
                      "build_receipt_sha256": digest, "wheel_identity": "not-built-from-wheel"}
        repository = "My-Denia/WuwaTerm"
    else:
        version = receipt["telegram_version"]
        origin = receipt["core_source"]
        core = {"version": receipt["core_version"], "revision_claim": origin["commit"],
                "verified_commit": origin["commit"], "verified_tree": origin["tree"]}
        provenance = {key: receipt[key] for key in ("telegram_wheel_sha256", "core_wheel_sha256",
                                                     "core_lock_sha256", "core_producer_kind")}
        provenance.update(kind="wheel-image", build_receipt_sha256=digest)
        repository = "My-Denia/WuwaTerm-Telegram"
    return {"repository": repository, "version": version, "source": source, "core": core,
            "image": {"ref": receipt["image_ref"], "id": receipt["image_id"],
                      "digest": receipt["image_digest"], "revision": receipt["image_revision"]},
            "recipe": recipe, "provenance": provenance}


def prepare_update(root: Path, request: dict[str, Any], compose_path: Path) -> dict[str, Any]:
    """Return a strictly validated prepared journal under the caller's FD9."""
    try:
        selection, receipts, recipes = _request(request)
        root = admission._physical_root(root)
        admission.admit_component(root, operation="update", code_root=CODE_ROOT)
        compose_path = transaction._compose_binary(compose_path)
        terminal = transaction.verify_terminal(root, compose_path=compose_path)
        current = runtime.inspect_deployment(root)
        if terminal is None or current["binding"] != "recorded" or current["manifest"] is None:
            raise PrepareError("an adopted and reconciled deployment is required")
        previous = terminal[1]
        prior_manifest = previous["target"]["manifest"] if previous["status"] == "committed" else previous["before"]["manifest"]
        prior_containers = previous["target"]["containers"] if previous["status"] == "committed" else previous["before"]["containers"]
        old = current["manifest"]
        if old != prior_manifest:
            raise PrepareError("current manifest differs from terminal deployment")
        if any(old["components"][name] is None for name in selection):
            raise PrepareError("selected component is not installed")
        if ("telegram" in selection and old["components"]["telegram"]["provenance"]["kind"] == "legacy-image"
                and "telegram" not in recipes):
            raise PrepareError("legacy Telegram requires an explicit target Compose recipe")

        commit = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{commit}", lock_root=root).decode("ascii").strip()
        tree = recovery.safe_git(root, "rev-parse", "--verify", "HEAD^{tree}", lock_root=root).decode("ascii").strip()
        recovery._load_packager(root / "scripts" / "package_recovery_source.py")
        recovery._verify_clean_source(root, commit, tree)
        source = recovery.prepare_source(root, commit, tree, compose_path=compose_path)
        compose_path = transaction._compose_binary(root / source["compose"]["path"])
        database_stat = manifest.verify_database_binding(old["database"])
        before: dict[str, Any] = {"api": None, "telegram": None}
        snapshots: dict[str, Any] = {}
        key = os.urandom(32)
        for name in ("api", "telegram"):
            component = old["components"][name]
            if component is None:
                continue
            previous_container = prior_containers[name]
            if previous_container is None:
                raise PrepareError("terminal container record is missing")
            snapshot = transaction._snapshot(root, component)
            actual = transaction._observed(previous_container["id"], snapshot, key)
            transaction._recipe_matches(actual, snapshot)
            if not transaction._same_container_binding(actual["container"], previous_container):
                raise PrepareError("installed component binding changed")
            image = transaction._image(snapshot["image_id"])
            if (transaction._effective_environment(snapshot, image, compose_path, root) !=
                    transaction._env_map(observe._inspect(actual["container"]["id"])["Config"].get("Env"))):
                raise PrepareError("installed environment no longer matches its references")
            before[name], snapshots[name] = actual["container"], snapshot

        transaction_id = manifest.new_deployment_id()
        target = copy.deepcopy(old)
        target.update(deployment_id=transaction_id,
                      deployment_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                      operations={"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                  "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]})
        target_containers = copy.deepcopy(before)
        references: dict[str, tuple[Any, ...]] = {}
        for name in selection:
            receipt = (build.verify_registered_build(root, receipts[name]) if name == "api" else
                       wheel.verify_registered_wheel_build(root, receipts[name]))
            base_source = {key: value for key, value in source.items() if key != "compose"}
            if receipt["recovery_source"] != base_source:
                raise PrepareError("receipt was produced by a different operations source")
            image = transaction._image(receipt["image_id"])
            if name in recipes:
                snapshot, inputs = _render_recipe(root, recipes[name], image, compose_path)
                for path, identity in inputs.items():
                    if path in references and references[path] != identity:
                        raise PrepareError("Compose input changed between target recipes")
                    references[path] = identity
            else:
                snapshot = copy.deepcopy(snapshots[name])
                snapshot["image_id"] = receipt["image_id"]
            if name == "api" and any(snapshot[key] != snapshots[name][key] for key in snapshot if key != "image_id"):
                raise PrepareError("API target recipe changes are unsupported")
            path, digest = manifest.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            target["components"][name] = _candidate(name, receipts[name], receipt, recipe)
            target_containers[name].update(id=None, image_id=receipt["image_id"],
                                           project=snapshot["project"], service=snapshot["service"],
                                           name=None, started_at=None, restart_count=None,
                                           transaction_label=transaction_id)
        manifest.validate_manifest(target)
        old_pointers = {".deploy_commit": transaction._pointer(root, ".deploy_commit"),
                        ".deploy_current": transaction._pointer(root, ".deploy_current")}
        new_pointers = {".deploy_commit": {"present": True, "bytes": commit + "\n"},
                        ".deploy_current": {"present": True, "bytes": transaction_id + "\n"}}
        kind = "full" if previous["kind"] == "component-full" else "runtime"
        predecessor = runtime.predecessor_reference(kind, previous["transaction_id"], terminal[0])
        journal = {"schema_version": runtime.JOURNAL_SCHEMA_VERSION, "kind": "component-runtime", "transaction_id": transaction_id,
                   "action": "update", "status": "active", "phase": "prepared", "source": source,
                   "predecessor": predecessor,
                   "selection": selection,
                   "before": {"manifest": copy.deepcopy(old), "topology": copy.deepcopy(old["components"]),
                              "containers": before},
                   "target": {"manifest": target, "containers": target_containers},
                   "database": {"binding": copy.deepcopy(old["database"]), "stat": database_stat},
                   "api_identity": copy.deepcopy(old["api_identity"]), "migrations": [],
                   "pointers": {"old": old_pointers, "target": new_pointers},
                   "manifest": {"deployment_id": transaction_id,
                                "path": f".deployments/products/{transaction_id}.json",
                                "sha256": hashlib.sha256(manifest._encode(target)).hexdigest()}}
        runtime.validate_journal(journal)
        transaction._verify_initial_preimage(root, journal, compose_path, previous, terminal[0])
        if "api" in selection:
            transaction._actual(root, journal, compose_path, initial=True)
        if "telegram" in selection:
            context = transaction._telegram_context(root, journal, compose_path)
            settings, _ = observe._environment(
                [key + "=" + value for key, value in context["target_environment"].items()],
                context["target_snapshot"]["working_dir"], os.urandom(32))
            path = settings["WUWATERM_DB_PATH"]
            command = context["target_snapshot"]["command"]
            if command and command[:1] == ["--db"]:
                path = command[1]
            if path is None or observe.resolve_bind_path(context["target_snapshot"], path, writable=False) != old["database"]["path"]:
                raise PrepareError("target Telegram database binding differs")
        if any(runtime._compose_input(Path(path)) != identity for path, identity in references.items()):
            raise PrepareError("Compose input changed during update preparation")
        final = transaction.verify_terminal(root, compose_path=compose_path)
        if final is None or final[0] != terminal[0]:
            raise PrepareError("terminal deployment changed during preparation")
        return journal
    except PrepareError:
        raise
    except (admission.AdmissionError, build.BuildError, wheel.WheelBuildError,
            capsule.CapsuleError, recovery.RecoveryError, runtime.JournalError,
            transaction.TransactionError, observe.ObservationError, manifest.ManifestError,
            OSError, subprocess.SubprocessError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise PrepareError("runtime update preparation was refused") from exc
