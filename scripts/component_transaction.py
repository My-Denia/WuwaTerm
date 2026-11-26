#!/usr/bin/env python3
"""Execute the narrow, retained-container API component transaction.

This module is intentionally not a command-line entry point.  Its caller must
construct the existing runtime2 journal from verified adoption/build records.
Telegram and changed runtime recipes remain refused until their state and
polling contracts are implemented by the coordinator.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_observe as observe  # noqa: E402
import component_recovery as recovery  # noqa: E402
import component_adopt_probe as probe  # noqa: E402
import component_journal_runtime as runtime  # noqa: E402
import component_wheel_build as wheel  # noqa: E402
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402

from component_terminal import (  # noqa: E402
    _predecessor_health as _predecessor_health,
    verify_terminal as verify_terminal,
    verify_terminal_for_full4 as verify_terminal_for_full4,
    _verify_terminal as _verify_terminal,
    _prepare_predecessor as _prepare_predecessor,
)

from component_operations import (  # noqa: E402
    TransactionError as TransactionError,
    _docker as _docker,
    _container_present as _container_present,
    _image as _image,
    _env_map as _env_map,
    _compose_binary as _compose_binary,
    _effective_environment as _effective_environment,
    _snapshot as _snapshot,
    _observed as _observed,
    _recipe_matches as _recipe_matches,
    _retained_name as _retained_name,
    _target_name as _target_name,
    _inventory as _inventory,
    _verify_data as _verify_data,
    _pointer as _pointer,
    _check_pointers as _check_pointers,
    _exact_pointers as _exact_pointers,
    _verify_saved_source as _verify_saved_source,
    _same_source as _same_source,
    _verify_manifest_if_present as _verify_manifest_if_present,
    _require_target_manifest_absent as _require_target_manifest_absent,
    _telegram_database_path as _telegram_database_path,
    _telegram_container_paths as _telegram_container_paths,
    _telegram_effective_paths as _telegram_effective_paths,
    _telegram_full4_entry_paths as _telegram_full4_entry_paths,
    _telegram_v5_paths as _telegram_v5_paths,
    _same_container_binding as _same_container_binding,
    _recorded_state_items as _recorded_state_items,
    _verify_current_telegram_paths as _verify_current_telegram_paths,
    _state_entry_safe as _state_entry_safe,
    _verify_adoption_telegram_paths as _verify_adoption_telegram_paths,
    _verify_full4_telegram_paths as _verify_full4_telegram_paths,
    _verify_live_topology as _verify_live_topology,
    _verify_refresh_live as _verify_refresh_live,
    _full4_record_route as _full4_record_route,
    _verify_terminal_record as _verify_terminal_record,
    _terminal_health as _terminal_health,
    _verify_initial_preimage as _verify_initial_preimage,
    _replace_pointer as _replace_pointer,
    _container_command as _container_command,
    _ready as _ready,
    _other_component as _other_component,
    _actual as _actual,
    verify_artifact_component as verify_artifact_component,
    _verify_candidate as _verify_candidate,
    _legacy_version as _legacy_version,
    _legacy_database as _legacy_database,
    _verify_old_topology as _verify_old_topology,
    _telegram_receipt as _telegram_receipt,
    _telegram_context as _telegram_context,
    _leaf_host as _leaf_host,
    _state_preimage as _state_preimage,
    _preserved_leaf as _preserved_leaf,
    _other_api as _other_api,
    _create_from_snapshot as _create_from_snapshot,
    _telegram_state_bindings as _telegram_state_bindings,
    _telegram_ready as _telegram_ready,
    _retire_telegram as _retire_telegram,
    _create_target as _create_target,
    _rename as _rename,
    _retire_api as _retire_api,
    _retire_committed as _retire_committed,
)


def _require_prepared_source(root: Path, source: dict[str, Any], compose_path: Path) -> None:
    """Recheck prepared bytes and require the recorded Compose for new journals."""
    base = {key: value for key, value in source.items() if key != "compose"}
    if recovery.prepare_source(root, source["commit"], source["tree"]) != base:
        raise TransactionError("saved recovery source differs")
    if "compose" in source and recovery._verify_bound_compose(root, source) != compose_path:
        raise TransactionError("Compose executable differs from the prepared transaction")


def _state_module() -> Any:
    try:
        import component_state as state  # noqa: PLC0415
        if Path(state.__file__).resolve() != CODE_ROOT / "scripts" / "component_state.py":
            raise TransactionError("state operation imported a foreign source")
        return state
    except ImportError as exc:
        raise TransactionError("Telegram state probes are unavailable") from exc



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
        # A refresh exists because the predecessor's API record no longer
        # matches Docker (the recorded container may be gone), so it verifies
        # the predecessor against the API it records and, like adoption,
        # makes no readiness claim.
        refresh = journal["action"] == runtime.REFRESH_ACTION
        override = None if not refresh else {
            "recipe": journal["target"]["manifest"]["components"]["api"]["recipe"],
            "container": journal["target"]["containers"]["api"]}

        def current_terminal() -> tuple[bytes, dict[str, Any]] | None:
            if override is None:
                return verify_terminal(root, compose_path=compose_path)
            return verify_terminal(root, compose_path=compose_path, api_override=override)

        def predecessor_health(record: dict[str, Any]) -> None:
            if not refresh:
                _predecessor_health(root, record)

        terminal = current_terminal()
        if (terminal is None) != (predecessor_raw is None):
            raise TransactionError("terminal predecessor presence changed")
        if terminal is not None and terminal[0] != predecessor_raw:
            raise TransactionError("terminal predecessor bytes changed")
        previous = None if terminal is None else terminal[1]
        if previous is not None:
            predecessor_health(previous)
            checked = current_terminal()
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
                checked = current_terminal()
                if checked is None or checked[0] != predecessor_raw:
                    raise TransactionError("terminal predecessor changed after archival")
                predecessor_health(previous)
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
                import component_handoff as handoff  # noqa: PLC0415
                if runtime._read_regular(root / ".deployments" / runtime.JOURNAL_NAME) != raw:
                    raise TransactionError("runtime successor readback differs")
                handoff.verify_handoff(root, successor_kind="runtime", successor=journal,
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
        import component_state_plans as prepare  # noqa: PLC0415
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
            if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
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
    pending = probe.pending_adopt_probe_staging(root)
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
        probe.reconcile_adopt_probe_staging(root, pending, source_tool_sha256=source_tool_sha256,
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
        import component_handoff_finish as handoff_finish  # noqa: PLC0415
        handoff_finish.finish_handoff(root, successor_kind="runtime", successor=journal)
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
    if journal["action"] in ("adopt", runtime.REFRESH_ACTION):
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
    if journal["action"] not in ("adopt", runtime.REFRESH_ACTION) or journal["selection"] != []:
        raise TransactionError("journal is not a metadata-only adoption or refresh")
    _verify_saved_source(root, journal["source"])
    _verify_old_topology(root, journal, compose_path=compose_path, allow_probe=False)
    _check_pointers(root, journal)
    if journal["before"]["manifest"] is not None:
        before = journal["before"]["manifest"]
        if manifest.read_manifest(root, before["deployment_id"]) != before:
            raise TransactionError("adoption preimage manifest differs")
    pointer_is_target = _pointer(root, ".deploy_current") == journal["pointers"]["target"][".deploy_current"]
    _verify_manifest_if_present(root, journal, required=pointer_is_target)
    if journal["action"] == runtime.REFRESH_ACTION:
        _verify_refresh_live(root, journal, compose_path)
    else:
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
