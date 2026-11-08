from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from scripts import component_prepare as prepare
from scripts import product_manifest as manifest


def _request(selection: str, receipts: dict[str, str], recipes: dict | None = None) -> dict:
    return {"selection": selection, "receipts": receipts, "recipes": recipes or {}}


def _image() -> dict:
    return {"Id": "sha256:" + "1" * 64,
            "Config": {"Env": ["LANG=C.UTF-8"], "Cmd": ["--db", "/app/data/terms.db"],
                       "Entrypoint": ["wuwaterm-telegram"], "User": "", "WorkingDir": "/app"}}


@pytest.mark.parametrize("selection,receipts", [
    ("api", {}), ("api", {"api": "a" * 64, "telegram": "b" * 64}),
    ("both", {"api": "a" * 64}), ("telegram", {"api": "a" * 64}),
])
def test_selected_receipts_must_be_exact(selection: str, receipts: dict) -> None:
    with pytest.raises(prepare.PrepareError):
        prepare._request(_request(selection, receipts))


def test_request_refuses_recipe_for_unselected_component() -> None:
    with pytest.raises(prepare.PrepareError):
        prepare._request(_request("api", {"api": "a" * 64}, {"telegram": {}}))
    assert prepare._request(_request("both", {"api": "a" * 64, "telegram": "b" * 64}))[0] == ["api", "telegram"]


def test_normalized_target_rejects_unrepresented_runtime_options() -> None:
    refs = {"project": "example", "service": "wuwaterm-telegram"}
    base = {"image": "candidate:placeholder", "network_mode": "host", "privileged": True}
    with pytest.raises(prepare.PrepareError):
        prepare._snapshot_from_compose(refs, base, copy.deepcopy(base), _image())


def test_nonempty_secret_override_is_not_persisted() -> None:
    refs = {"project": "example", "service": "wuwaterm-telegram"}
    base = {"image": "candidate:placeholder", "network_mode": "host",
            "environment": {"TELEGRAM_BOT_TOKEN": "synthetic-canary"}}
    with pytest.raises(prepare.PrepareError):
        prepare._snapshot_from_compose(refs, base, copy.deepcopy(base), _image())


@pytest.mark.skipif(not os.environ.get("COMPONENT_PREPARE_TEST_COMPOSE"), reason="standalone Compose not supplied")
def test_real_compose_renders_target_without_persisting_environment(tmp_path: Path) -> None:
    compose = Path(os.environ["COMPONENT_PREPARE_TEST_COMPOSE"])
    env_file = tmp_path / "fixture.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=synthetic-canary\n", encoding="ascii")
    config = tmp_path / "compose.yml"
    config.write_text(
        "services:\n"
        "  wuwaterm-telegram:\n"
        "    image: placeholder:local\n"
        "    container_name: example-telegram\n"
        "    network_mode: host\n"
        "    restart: unless-stopped\n"
        "    entrypoint: [wuwaterm-telegram]\n"
        "    command: [--db, /app/data/terms.db]\n"
        f"    env_file: [{env_file}]\n"
        "    environment:\n"
        "      WUWATERM_DB_PATH: /app/data/terms.db\n"
        "      WUWATERM_STATE_DIR: state/custom\n"
        "      WUWATERM_POLL_HEALTH: '1'\n"
        "    volumes:\n"
        f"      - {tmp_path}/data:/app/data:ro\n"
        f"      - {tmp_path}/state:/app/state\n"
        "    tmpfs:\n"
        "      - /run/wuwaterm-telegram:rw,noexec,nosuid,nodev,mode=0700\n",
        encoding="ascii",
    )
    refs = {"compose_files": [str(config)], "project_directory": str(tmp_path),
            "project": "example", "service": "wuwaterm-telegram", "interpolation_env_files": []}
    try:
        saved = os.dup(9)
    except OSError:
        saved = None
    (tmp_path / ".deployments").mkdir()
    fd = os.open(tmp_path / ".deployments" / ".deployment.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fd != 9:
            os.dup2(fd, 9)
        snapshot, references = prepare._render_recipe(tmp_path, refs, _image(), compose)
        interpolation = tmp_path / "interpolation.env"
        interpolation.write_text("TEST_ONLY_TOKEN=synthetic-canary\n", encoding="ascii")
        env_file.write_text("TELEGRAM_BOT_TOKEN=${TEST_ONLY_TOKEN}\n", encoding="ascii")
        with pytest.raises(prepare.PrepareError, match="cannot be reproduced"):
            prepare._render_recipe(tmp_path, {**refs, "interpolation_env_files": [str(interpolation)]},
                                   _image(), compose)
    finally:
        if fd != 9:
            os.close(fd)
        if saved is None:
            os.close(9)
        else:
            os.dup2(saved, 9)
            os.close(saved)
    assert set(references) == {str(config), str(env_file)}
    assert snapshot["entrypoint"] == ["wuwaterm-telegram"]
    assert snapshot["command"] == ["--db", "/app/data/terms.db"]
    assert snapshot["env_files"] == [str(env_file)]
    assert {item["name"]: item.get("value") for item in snapshot["environment_overrides"]}[
        "WUWATERM_STATE_DIR"] == "state/custom"
    assert "synthetic-canary" not in str(snapshot)
    assert snapshot["mounts"][-1] == {"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                                       "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700"]}
    manifest.verify_poll_health_mount(snapshot, effective_uid=0)


# ------------------------------------------------- protocol-2 cutover inputs

def _legacy_snapshot(**changes) -> dict:
    snapshot = {
        "schema_version": 1, "image_id": "sha256:" + "2" * 64, "project": "wuwaterm", "service": "bot",
        "container_name": "wuwaterm-bot", "command": None, "entrypoint": None,
        "mounts": [{"host": "/srv/w/custom", "container": "/custom", "read_only": False},
                   {"host": "/srv/w/data", "container": "/app/data", "read_only": True}],
        "env_files": ["/srv/w/.env"],
        "environment_overrides": [{"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/custom/terms.db"}],
        "network_mode": "host", "ports": [], "restart": {"name": "unless-stopped", "maximum_retry_count": 0},
        "user": None, "working_dir": None,
        "runtime_options": {"read_only_root": False, "privileged": False, "init": None, "pid_mode": "",
                            "ipc_mode": "private", "cap_add": [], "cap_drop": [], "security_opt": [],
                            "dns": [], "extra_hosts": []},
    }
    snapshot.update(changes)
    return manifest.validate_recipe_snapshot(snapshot)


def _legacy_inspect(env: list[str], *, user: str = "", working_dir: str = "/app",
                    entrypoint=("/entrypoint.sh",), command=("bot",), host: dict | None = None) -> dict:
    return {"Config": {"Env": env, "User": user, "WorkingDir": working_dir,
                       "Entrypoint": list(entrypoint), "Cmd": list(command)},
            "HostConfig": host if host is not None else {"GroupAdd": None, "UsernsMode": ""}}


def test_translation_preserves_custom_database_and_state_inputs() -> None:
    """Counterexample 1: WUWATERM_DB_PATH=/custom/terms.db and no StateDir."""
    old = _legacy_snapshot()
    translated = prepare.translate_preserved_recipe(
        old, _legacy_inspect(["WUWATERM_DB_PATH=/custom/terms.db"]), _image())
    assert translated["entrypoint"] == ["wuwaterm-telegram"]
    assert translated["command"] == ["--db", "/custom/terms.db"]
    assert translated["working_dir"] == "/app" and translated["user"] == "0:0"
    assert translated["image_id"] == _image()["Id"]
    for key in ("env_files", "project", "service", "container_name", "network_mode", "ports", "restart",
                "runtime_options"):
        assert translated[key] == old[key]
    # Every persistent state binding survives verbatim; only the standalone
    # product's own poll-health requirements are added.
    assert translated["mounts"][:len(old["mounts"])] == old["mounts"]
    assert translated["mounts"][len(old["mounts"]):] == [{
        "type": "tmpfs", "container": "/run/wuwaterm-telegram",
        "options": ["rw", "noexec", "nosuid", "nodev", "size=1048576", "mode=0700", "uid=0", "gid=0"]}]
    assert translated["environment_overrides"][:len(old["environment_overrides"])] == old["environment_overrides"]
    assert translated["environment_overrides"][len(old["environment_overrides"]):] == [
        {"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"}]
    manifest.verify_poll_health_mount(translated, effective_uid=0)
    # The legacy entrypoint default applies only when the variable is unset.
    assert prepare.translate_preserved_recipe(old, _legacy_inspect([]), _image())["command"] == [
        "--db", "/app/data/terms.db"]


def test_translation_completes_poll_health_deterministically() -> None:
    """Counterexample: a legacy Core recipe carries no standalone health inputs."""
    old = _legacy_snapshot()
    first = prepare.translate_preserved_recipe(old, _legacy_inspect([], user="1000:1000"), _image())
    again = prepare.translate_preserved_recipe(old, _legacy_inspect([], user="1000:1000"), _image())
    assert first == again
    assert first["mounts"][-1]["options"][-2:] == ["uid=1000", "gid=1000"]
    # An existing health mount and flag are preserved, never duplicated.
    kept = _legacy_snapshot(
        mounts=[{"host": "/srv/w/custom", "container": "/custom", "read_only": False},
                {"host": "/srv/w/data", "container": "/app/data", "read_only": True},
                {"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                 "options": ["rw", "noexec", "nosuid", "nodev", "size=1048576", "mode=0700",
                             "uid=1000", "gid=1000"]}],
        environment_overrides=[{"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/custom/terms.db"},
                               {"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"}])
    preserved = prepare.translate_preserved_recipe(kept, _legacy_inspect([], user="1000:1000"), _image())
    assert preserved["mounts"] == kept["mounts"]
    assert preserved["environment_overrides"] == kept["environment_overrides"]
    # An explicit disabled health flag is neither duplicated nor silently
    # rewritten: the conflict is refused before any state is touched.
    disabled = _legacy_snapshot(environment_overrides=[
        {"name": "WUWATERM_DB_PATH", "mode": "literal", "value": "/custom/terms.db"},
        {"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "0"}])
    with pytest.raises(prepare.PrepareError, match="poll health override conflicts"):
        prepare.translate_preserved_recipe(disabled, _legacy_inspect([]), _image())
    # A health mount whose owner differs from the process UID is not repaired.
    wrong = _legacy_snapshot(mounts=[{"host": "/srv/w/custom", "container": "/custom", "read_only": False},
                                     {"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                                      "options": ["rw", "noexec", "nosuid", "nodev", "mode=0700",
                                                  "uid=4242", "gid=4242"]}])
    with pytest.raises(prepare.PrepareError, match="poll health requirement"):
        prepare.translate_preserved_recipe(wrong, _legacy_inspect([], user="1000:1000"), _image())


def test_already_cut_over_recipe_is_preserved_and_verified() -> None:
    """A standalone invocation keeps its own mounts and overrides unchanged."""
    installed = _legacy_snapshot(
        mounts=[{"host": "/srv/w/custom", "container": "/custom", "read_only": False},
                {"host": "/srv/w/data", "container": "/app/data", "read_only": True},
                {"type": "tmpfs", "container": "/run/wuwaterm-telegram",
                 "options": ["rw", "noexec", "nosuid", "nodev", "size=1048576", "mode=0700",
                             "uid=1000", "gid=1000"]}],
        environment_overrides=[{"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"},
                               {"name": "WUWATERM_STATE_DIR", "mode": "literal", "value": "state"},
                               {"name": "TELEGRAM_BOT_TOKEN", "mode": "empty"}],
        user="1000:1000")
    translated = prepare.translate_preserved_recipe(
        installed, _legacy_inspect([], entrypoint=["wuwaterm-telegram"],
                                  command=["--db", "/custom/terms.db"], user="1000:1000"), _image())
    assert translated["mounts"] == installed["mounts"]
    assert translated["environment_overrides"] == installed["environment_overrides"]
    assert translated["command"] == ["--db", "/custom/terms.db"] and translated["user"] == "1000:1000"
    # No silent repair: a cut-over recipe without the health tmpfs is refused.
    broken = _legacy_snapshot(user="1000:1000",
                              environment_overrides=[{"name": "WUWATERM_POLL_HEALTH", "mode": "literal",
                                                      "value": "1"}])
    with pytest.raises(prepare.PrepareError, match="poll health requirement"):
        prepare.translate_preserved_recipe(
            broken, _legacy_inspect([], entrypoint=["wuwaterm-telegram"],
                                    command=["--db", "/custom/terms.db"], user="1000:1000"), _image())


@pytest.mark.parametrize("inspect, message", [
    (_legacy_inspect(["WUWATERM_DB_PATH=data/terms.db"]), "transaction observation limit"),
    (_legacy_inspect(["WUWATERM_DB_PATH=~/terms.db"]), "transaction observation limit"),
    (_legacy_inspect([], user="bot"), "transaction observation limit"),
    (_legacy_inspect([], user="1000"), "transaction observation limit"),
    (_legacy_inspect([], host={"GroupAdd": ["44"], "UsernsMode": ""}), "supplementary groups"),
    (_legacy_inspect([], working_dir=""), "transaction observation limit"),
    (_legacy_inspect([], command=["api"]), "not a known product invocation"),
])
def test_translation_refuses_what_the_transaction_cannot_reproduce(inspect: dict, message: str) -> None:
    with pytest.raises(prepare.PrepareError, match=message):
        prepare.translate_preserved_recipe(_legacy_snapshot(), inspect, _image())


def test_caller_recipe_that_moves_state_is_refused_as_relocation() -> None:
    translated = prepare.translate_preserved_recipe(
        _legacy_snapshot(), _legacy_inspect(["WUWATERM_DB_PATH=/custom/terms.db"]), _image())
    compose = copy.deepcopy(translated)
    compose["mounts"] = [*compose["mounts"], {"host": "/srv/w/state", "container": "/app/state", "read_only": False}]
    with pytest.raises(prepare.PrepareError, match="state relocation is not supported in protocol-2 cutover"):
        prepare.refuse_target_recipe_change(translated, compose)
    restart = copy.deepcopy(translated)
    restart["restart"] = {"name": "always", "maximum_retry_count": 0}
    with pytest.raises(prepare.PrepareError, match="recipe changes are unsupported"):
        prepare.refuse_target_recipe_change(translated, restart)
    prepare.refuse_target_recipe_change(translated, copy.deepcopy(translated))


def _plan(**kinds) -> dict:
    base = {"effective_path": "/custom/chat_settings.json", "legacy_path": None, "migration": "validate-existing",
            "mapped_target": "/srv/w/custom/chat_settings.json", "mapped_legacy": None}
    replies = dict(base, effective_path="/custom/channel_replies.jsonl",
                   mapped_target="/srv/w/custom/channel_replies.jsonl", migration="none")
    plan = {"database_path": "/custom/terms.db", "kinds": {"chat-settings": base, "channel-replies": replies}}
    for kind, change in kinds.items():
        plan["kinds"][kind.replace("_", "-")] = dict(plan["kinds"][kind.replace("_", "-")], **change)
    return plan


def test_state_plan_equivalence_classifies_and_refuses_unmounted_or_moved_state() -> None:
    assert prepare.verify_state_plan_equivalence(_plan(), _plan()) == {
        "chat-settings": "validate-existing", "channel-replies": "none"}
    pending = {"legacy_path": "/custom/old.json", "mapped_legacy": "/srv/w/custom/old.json",
               "migration": "copy-if-absent"}
    assert prepare.verify_state_plan_equivalence(_plan(chat_settings=pending), _plan(chat_settings=pending))[
        "chat-settings"] == "native-pending"
    # None == None is not equivalence: the path is in no bind on either side.
    unmounted = {"mapped_target": None}
    with pytest.raises(prepare.PrepareError, match="outside every writable bind"):
        prepare.verify_state_plan_equivalence(_plan(chat_settings=unmounted), _plan(chat_settings=unmounted))
    # The copy case needs both legacy endpoints; two unresolvable ones are not
    # an equivalence either.
    legacy_unmounted = {"legacy_path": "/custom/old.json", "mapped_legacy": None,
                        "migration": "copy-if-absent"}
    with pytest.raises(prepare.PrepareError, match="legacy state is outside every bind"):
        prepare.verify_state_plan_equivalence(_plan(chat_settings=legacy_unmounted),
                                              _plan(chat_settings=legacy_unmounted))
    mismatched = dict(pending, mapped_legacy="/srv/w/other/old.json")
    with pytest.raises(prepare.PrepareError, match="state relocation is not supported"):
        prepare.verify_state_plan_equivalence(_plan(chat_settings=pending), _plan(chat_settings=mismatched))
    moved = {"effective_path": "/app/state/chat_settings.json",
             "mapped_target": "/srv/w/state/chat_settings.json"}
    with pytest.raises(prepare.PrepareError, match="state relocation is not supported"):
        prepare.verify_state_plan_equivalence(_plan(), _plan(chat_settings=moved))
    with pytest.raises(prepare.PrepareError, match="disagree on the migration action"):
        prepare.verify_state_plan_equivalence(_plan(), _plan(chat_settings={"migration": "none"}))


def test_state_plan_equivalence_accepts_the_legacy_mapped_legacy_asymmetry() -> None:
    """A retained legacy endpoint is compared only when a copy is pending."""
    retained = {"legacy_path": "/custom/old.json", "mapped_legacy": None}
    resolved = {"legacy_path": "/custom/old.json", "mapped_legacy": "/srv/w/custom/old.json"}
    for migration in ("none", "validate-existing"):
        old = _plan(chat_settings=dict(retained, migration=migration))
        new = _plan(chat_settings=dict(resolved, migration=migration))
        assert prepare.verify_state_plan_equivalence(old, new)["chat-settings"] == (
            "none" if migration == "none" else "validate-existing")
    # Under a copy the two sides must name the same legacy file.
    copied = {"legacy_path": "/custom/old.json", "mapped_legacy": "/srv/w/custom/old.json",
              "migration": "copy-if-absent"}
    assert prepare.verify_state_plan_equivalence(_plan(chat_settings=copied),
                                                 _plan(chat_settings=copied))["chat-settings"] == "native-pending"
    with pytest.raises(prepare.PrepareError, match="legacy state is outside every bind"):
        prepare.verify_state_plan_equivalence(_plan(chat_settings=dict(copied, mapped_legacy=None)),
                                              _plan(chat_settings=copied))
    with pytest.raises(prepare.PrepareError, match="legacy state is outside every bind"):
        prepare.verify_state_plan_equivalence(_plan(chat_settings=copied),
                                              _plan(chat_settings=dict(copied, mapped_legacy=None)))


def test_artifact_request_shape_and_protocol1_receipts_are_refused(tmp_path: Path) -> None:
    assert prepare._artifact_request({"selection": "both", "artifacts": {"api": "/a.json", "telegram": "/b.json"},
                                      "recipes": {}})[0] == ["api", "telegram"]
    for bad in ({"selection": "api", "artifacts": {}, "recipes": {}},
                {"selection": "api", "artifacts": {"api": "/a.json"}, "recipes": {"api": {}}},
                {"selection": "api", "artifacts": {"api": "/a.json", "telegram": "/b.json"}, "recipes": {}}):
        with pytest.raises(prepare.PrepareError):
            prepare._artifact_request(bad)
    with pytest.raises(prepare.PrepareError, match="protocol-1 inputs"):
        prepare.prepare_update(tmp_path, _request("api", {"api": "a" * 64}), tmp_path / "compose")
    assert not (tmp_path / ".deployments").exists()


def test_legacy_plan_shape_matches_the_product_native_pending_record() -> None:
    """The legacy planner's copy record is what the product plan must equal."""
    legacy = {"effective_path": "/app/state/chat_settings.json", "legacy_path": "/app/data/chat_settings.json",
              "migration": "copy-if-absent", "mapped_target": "/srv/w/state/chat_settings.json",
              "mapped_legacy": "/srv/w/data/chat_settings.json"}
    replies = {"effective_path": "/app/state/channel_replies.json", "legacy_path": "/app/data/channel_replies.json",
               "migration": "none", "mapped_target": "/srv/w/state/channel_replies.json",
               "mapped_legacy": "/srv/w/data/channel_replies.json"}
    plan = {"database_path": "/app/data/terms.db",
            "kinds": {"chat-settings": legacy, "channel-replies": replies}}
    assert prepare.verify_state_plan_equivalence(plan, copy.deepcopy(plan)) == {
        "chat-settings": "native-pending", "channel-replies": "none"}
    # A legacy plan that leaves its mapped_legacy unset cannot be classified as
    # a copy, and the product's copy has to name the same file.
    for change in ({"mapped_legacy": None}, {"mapped_legacy": "/srv/w/other/chat_settings.json"},
                   {"migration": "validate-existing"}):
        altered = copy.deepcopy(plan)
        altered["kinds"]["chat-settings"].update(change)
        with pytest.raises(prepare.PrepareError):
            prepare.verify_state_plan_equivalence(plan, altered)
