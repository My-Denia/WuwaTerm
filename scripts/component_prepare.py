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
import re
import subprocess
import sys
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_artifact as artifact  # noqa: E402
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


RELOCATION_REFUSAL = ("state relocation is not supported in protocol-2 cutover; "
                      "preserve the existing state mount and paths")
_LEGACY_DB_DEFAULT = "/app/data/terms.db"
_POLL_HEALTH_TARGET = "/run/wuwaterm-telegram"
_POLL_HEALTH_OPTIONS = ("rw", "noexec", "nosuid", "nodev", "size=1048576", "mode=0700")
_NUMERIC_USER = re.compile(r"([0-9]+):([0-9]+)\Z")
_STATE_FIELDS = ("command", "entrypoint", "mounts", "env_files", "environment_overrides", "user", "working_dir")
_STATE_KINDS = ("chat-settings", "channel-replies")
_PLAN_KEYS = {"effective_path", "legacy_path", "migration", "mapped_target", "mapped_legacy"}
_PLAN_COMPARED = ("effective_path", "legacy_path", "mapped_target")
_PLAN_CLASS = {"none": "none", "validate-existing": "validate-existing", "copy-if-absent": "native-pending"}


def _observation_limit(what: str) -> PrepareError:
    # These are limits of what the transaction can observe and reproduce,
    # not limits of the native product.
    return PrepareError(f"{what} (a transaction observation limit, not a product limit)")


def translate_preserved_recipe(old_snapshot: dict[str, Any], old_inspect: dict[str, Any],
                               target_image_inspect: dict[str, Any]) -> dict[str, Any]:
    """Translate an adopted Telegram recipe to the Telegram product image, in place.

    Pure: the result keeps every state-bearing input of the old container
    (effective DB argument, working directory, numeric user, binds, env files
    and overrides) and changes only the image and the product command form.
    """
    old_snapshot = manifest.validate_recipe_snapshot(copy.deepcopy(old_snapshot))
    config = old_inspect.get("Config") if type(old_inspect) is dict else None
    host = old_inspect.get("HostConfig") if type(old_inspect) is dict else None
    if type(config) is not dict or type(host) is not dict:
        raise PrepareError("old Telegram container inspection is incomplete")
    target_id = target_image_inspect.get("Id") if type(target_image_inspect) is dict else None
    manifest._image_id(target_id, "target Telegram image")
    entrypoint, command = config.get("Entrypoint"), config.get("Cmd")
    environment = transaction._env_map(config.get("Env"))
    if entrypoint == ["/entrypoint.sh"] and command == ["bot"]:
        database = environment.get("WUWATERM_DB_PATH") or _LEGACY_DB_DEFAULT
    elif entrypoint == ["wuwaterm-telegram"] and type(command) is list and len(command) == 2 and command[0] == "--db":
        database = command[1]
    else:
        raise PrepareError("old Telegram command form is not a known product invocation")
    if (type(database) is not str or database != database.strip() or database.startswith("~")
            or not database.startswith("/")):
        raise _observation_limit("old Telegram database path is relative, home-relative or padded")
    try:
        manifest._path(database, "old Telegram database path")
    except manifest.ManifestError:
        raise _observation_limit("old Telegram database path is not normalized") from None
    working_dir = old_snapshot["working_dir"] or config.get("WorkingDir") or None
    if type(working_dir) is not str:
        raise _observation_limit("old Telegram working directory is not explicit")
    try:
        manifest._path(working_dir, "old Telegram working directory")
    except manifest.ManifestError:
        raise _observation_limit("old Telegram working directory is not normalized") from None
    user = old_snapshot["user"] if old_snapshot["user"] is not None else (config.get("User") or "")
    if user in ("", "0"):
        user = "0:0"
    if type(user) is not str or not _NUMERIC_USER.fullmatch(user):
        raise _observation_limit("old Telegram user is named or lacks an explicit numeric group")
    if host.get("GroupAdd") not in (None, []) or host.get("UsernsMode") not in (None, ""):
        raise _observation_limit("old Telegram user has supplementary groups or a user-namespace mode")
    translated = copy.deepcopy(old_snapshot)
    translated.update(image_id=target_id, entrypoint=["wuwaterm-telegram"], command=["--db", database],
                      working_dir=working_dir, user=user)
    _preserve_poll_health(translated, legacy=entrypoint == ["/entrypoint.sh"], user=user)
    translated = manifest.validate_recipe_snapshot(translated)
    try:
        manifest.verify_poll_health_mount(translated, effective_uid=int(user.split(":", 1)[0]))
    except manifest.ManifestError as exc:
        raise PrepareError(
            "translated Telegram recipe cannot satisfy the standalone poll health requirement") from exc
    return translated


def _preserve_poll_health(translated: dict[str, Any], *, legacy: bool, user: str) -> None:
    """Keep a standalone Telegram recipe's poll-health inputs satisfiable.

    The legacy Core image never needed a health tmpfs or a health flag; the
    standalone product does. A recipe that is already cut over is preserved and
    only verified, so a caller cannot silently gain or lose a health mount.
    An explicit ``WUWATERM_POLL_HEALTH=0`` cannot be honoured here: appending a
    second override is invalid and rewriting it to ``1`` would silently change
    the caller's stated intent, so both are refused.
    """
    overrides = translated["environment_overrides"]
    flag = [item for item in overrides if item["name"] == "WUWATERM_POLL_HEALTH"]
    mounts = translated["mounts"]
    if not legacy:
        return
    if flag and (flag[0]["mode"] != "literal" or flag[0]["value"] != "1"):
        raise PrepareError("explicit Telegram poll health override conflicts with the standalone product")
    if not any(mount["container"] == _POLL_HEALTH_TARGET for mount in mounts):
        uid, gid = user.split(":", 1)
        mounts.append({"type": "tmpfs", "container": _POLL_HEALTH_TARGET,
                       "options": [*_POLL_HEALTH_OPTIONS, "uid=" + uid, "gid=" + gid]})
    if not flag:
        overrides.append({"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"})


def refuse_target_recipe_change(translated: dict[str, Any], requested: dict[str, Any]) -> None:
    """Protocol 2 accepts no caller recipe that differs from the translation."""
    if any(requested[key] != translated[key] for key in _STATE_FIELDS):
        raise PrepareError(RELOCATION_REFUSAL)
    if any(requested[key] != translated[key] for key in translated if key != "image_id"):
        raise PrepareError("Telegram target recipe changes are unsupported in protocol-2 cutover")


def verify_state_plan_equivalence(old_plan: dict[str, Any], target_plan: dict[str, Any]) -> dict[str, str]:
    """Require the old and translated state plans to name exactly the same state.

    Both plans are native product plans already mapped to host paths. A path
    outside every bind maps to ``None``; two such paths are not equivalent.
    Returns the per-kind classification: none, validate-existing or
    native-pending (the product itself copies at start; the transaction never does).
    """
    result = {}
    for plan in (old_plan, target_plan):
        if type(plan) is not dict or type(plan.get("kinds")) is not dict or set(plan["kinds"]) != set(_STATE_KINDS):
            raise PrepareError("Telegram state plan is invalid")
    for kind in _STATE_KINDS:
        old, new = old_plan["kinds"][kind], target_plan["kinds"][kind]
        for item in (old, new):
            if type(item) is not dict or set(item) != _PLAN_KEYS or item["migration"] not in _PLAN_CLASS:
                raise PrepareError("Telegram state plan is invalid")
            if item["mapped_target"] is None:
                raise PrepareError(f"{kind} state is outside every writable bind")
        if old["migration"] != new["migration"]:
            raise PrepareError(f"{kind} old and target state plans disagree on the migration action")
        if any(old[key] != new[key] for key in _PLAN_COMPARED):
            raise PrepareError(RELOCATION_REFUSAL)
        if new["migration"] == "copy-if-absent":
            # Both planners name the retained legacy endpoint, but only the
            # copy case decides what the transaction may do with it, so only
            # that case has to agree exactly.
            for item in (old, new):
                if item["mapped_legacy"] is None:
                    raise PrepareError(f"{kind} legacy state is outside every bind")
            if old["mapped_legacy"] != new["mapped_legacy"]:
                raise PrepareError(RELOCATION_REFUSAL)
        result[kind] = _PLAN_CLASS[new["migration"]]
    return result


def artifact_input(root: Path, name: str, receipt_path: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify one registered acquisition and return its recipe-free component facts.

    Returns (facts, target image, receipt, bundle). The caller attaches the
    recipe it will actually run, so a translated Telegram recipe and an
    unchanged API recipe share the same verified artifact facts.
    """
    if type(receipt_path) is not str and not isinstance(receipt_path, Path):
        raise PrepareError("artifact input must be an acquisition receipt path")
    path = Path(os.path.abspath(os.fspath(receipt_path)))
    if path.parent != root / ".deployments" / "artifacts" / "acquisitions":
        raise PrepareError("acquisition receipt is not registered in this deployment root")
    verified = artifact.verify_acquisition(path)
    receipt, bundle = verified["receipt"], verified["bundle"]
    if bundle["product"] != manifest.ARTIFACT_PRODUCTS[name]:
        raise PrepareError(f"{name} artifact is a different product")
    if bundle["compatibility"]["db_schema"] != 2 or (name == "api" and bundle["compatibility"]["api"] != "v1"):
        raise PrepareError(f"{name} artifact is not compatible with this deployment")
    native = receipt["native_image_id"]
    image = transaction._image(native)
    commit = bundle["source"]["commit"]
    if (image.get("Id") != native
            or (image.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision") != commit):
        raise PrepareError(f"{name} artifact image revision differs from its sealed source")
    source = {"revision_claim": commit, "verified_commit": commit, "verified_tree": bundle["source"]["tree"]}
    binding = bundle["core_binding"]
    core = ({"version": binding["core_version"], **source} if name == "api" else
            {"version": binding["core_version"], "revision_claim": binding["core_source_commit"],
             "verified_commit": None, "verified_tree": None})
    facts = {"repository": bundle["repository"], "version": bundle["version"], "source": source, "core": core,
             "image": {"ref": native, "id": native, "digest": native, "revision": commit},
             "provenance": {"kind": "completed-artifact", "bundle_sha256": verified["bundle_sha256"],
                            "acquisition_sha256": verified["receipt_sha256"],
                            "authority": bundle["build_result"]["authority"], "native_image_id": native}}
    return facts, image, receipt, bundle


def _artifact_candidate(root: Path, name: str, receipt_path: Any, recipe: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (manifest component, target image) from one registered acquisition."""
    facts, image, receipt, bundle = artifact_input(root, name, receipt_path)
    component = {**facts, "recipe": recipe}
    manifest.verify_artifact_binding(name, component, receipt, bundle)
    return component, image


def _artifact_request(value: Any) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
    """Validate a protocol-2 request: one acquisition receipt per selected component."""
    if type(value) is not dict or set(value) != {"selection", "artifacts", "recipes"}:
        raise PrepareError("invalid update request")
    selections = {"api": ["api"], "telegram": ["telegram"], "both": ["api", "telegram"]}
    selection = selections.get(value["selection"]) if type(value["selection"]) is str else None
    artifacts, recipes = value["artifacts"], value["recipes"]
    if selection is None or type(artifacts) is not dict or set(artifacts) != set(selection):
        raise PrepareError("one completed artifact is required for each selected component")
    if type(recipes) is not dict or not set(recipes).issubset(selection):
        raise PrepareError("recipes must refer only to selected components")
    if "api" in recipes:
        raise PrepareError("API target recipe changes are unsupported")
    for receipt in artifacts.values():
        if not isinstance(receipt, (str, Path)) or not os.fspath(receipt):
            raise PrepareError("artifact input must be an acquisition receipt path")
    return selection, artifacts, recipes


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
        if type(request) is dict and "receipts" in request:
            # Producer receipts are protocol-1 records; this source has no
            # producer and writes only schema-3 completed-artifact manifests.
            _request(request)
            raise PrepareError("producer build receipts are protocol-1 inputs; "
                               "use --api-artifact / --telegram-artifact acquisition receipts")
        selection, artifacts, recipes = _artifact_request(request)
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
        for name, component in old["components"].items():
            if (name not in selection and component is not None
                    and component["provenance"]["kind"] not in manifest.PROVENANCE_KINDS[manifest.MANIFEST_SCHEMA_VERSION]):
                raise PrepareError(f"unselected {name} has producer provenance that a schema-3 manifest cannot carry")

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
        target.update(schema_version=manifest.MANIFEST_SCHEMA_VERSION, deployment_id=transaction_id,
                      deployment_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                      operations={"repository": "My-Denia/WuwaTerm", "source_commit": commit,
                                  "source_tree": tree, "source_tool_sha256": source["source_tool_sha256"]})
        target_containers = copy.deepcopy(before)
        references: dict[str, tuple[Any, ...]] = {}
        for name in selection:
            candidate, image = _artifact_candidate(root, name, artifacts[name], {})
            native = candidate["image"]["id"]
            if name == "telegram":
                snapshot = translate_preserved_recipe(snapshots[name], observe._inspect(before[name]["id"]), image)
                if name in recipes:
                    rendered, inputs = _render_recipe(root, recipes[name], image, compose_path)
                    refuse_target_recipe_change(snapshot, rendered)
                    for path, identity in inputs.items():
                        if path in references and references[path] != identity:
                            raise PrepareError("Compose input changed between target recipes")
                        references[path] = identity
            else:
                snapshot = copy.deepcopy(snapshots[name])
                snapshot["image_id"] = native
            path, digest = manifest.write_recipe_snapshot(root, snapshot)
            recipe = {key: snapshot[key] for key in manifest._RECIPE_FIELDS}
            recipe.update(snapshot_path=path, sha256=digest)
            candidate["recipe"] = recipe
            target["components"][name] = candidate
            target_containers[name].update(id=None, image_id=native,
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
    except (admission.AdmissionError, artifact.ArtifactError, build.BuildError, wheel.WheelBuildError,
            capsule.CapsuleError, recovery.RecoveryError, runtime.JournalError,
            transaction.TransactionError, observe.ObservationError, manifest.ManifestError,
            OSError, subprocess.SubprocessError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise PrepareError("runtime update preparation was refused") from exc
