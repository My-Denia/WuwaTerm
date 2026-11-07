from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from scripts import component_observe as observe


ID = "a" * 64
IMAGE = "sha256:" + "b" * 64
KEY = b"private-test-fingerprint-key-0000"
PRIVATE = "secret-for-test-only-6391"


def observation() -> dict:
    host = {**{name: 0 for name in observe._ZERO_FIELDS},
            **{name: "" for name in observe._EMPTY_STRING_FIELDS},
            **{name: [] for name in observe._EMPTY_ARRAY_FIELDS}}
    host.update({
        "Binds": None, "CapAdd": None, "CapDrop": None, "CgroupnsMode": "private",
        "ConsoleSize": [0, 0], "Dns": [], "ExtraHosts": None, "IpcMode": "private",
        "LogConfig": {"Type": "json-file", "Config": {}}, "MaskedPaths": sorted(observe._DEFAULT_MASKED),
        "MemorySwappiness": None, "Mounts": [], "NetworkMode": "host", "OomKillDisable": False,
        "PidMode": "", "PidsLimit": None, "PortBindings": {}, "Privileged": False,
        "PublishAllPorts": False, "ReadonlyPaths": sorted(observe._DEFAULT_READONLY),
        "ReadonlyRootfs": False, "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "Runtime": "runc", "SecurityOpt": None, "ShmSize": 67108864, "Tmpfs": {},
        "AutoRemove": False,
    })
    return {
        "Id": ID, "Image": IMAGE, "Name": "/bot", "RestartCount": 0,
        "Config": {"Cmd": ["bot"], "Entrypoint": ["/entrypoint.sh"], "Hostname": ID[:12],
                   "Domainname": "", "WorkingDir": "/app", "User": "1000:1000",
                   "Labels": {"com.docker.compose.project": "project", "com.docker.compose.service": "bot"},
                   "Env": ["WUWATERM_DB_PATH=/app/data/terms.db", "WUWATERM_STATE_DIR=relative-state",
                           "TELEGRAM_BOT_TOKEN=" + PRIVATE]},
        "HostConfig": host,
        "State": {"Running": True, "Paused": False, "Restarting": False, "Dead": False, "Error": "",
                  "Status": "running", "StartedAt": "2026-11-06T11:00:00.123456789Z"},
        "Mounts": [{"Type": "bind", "Source": "/srv/state", "Destination": "/app/state", "RW": True,
                    "Mode": "rw", "Propagation": "rprivate"}],
        "NetworkSettings": {"Networks": {"host": {"IPAMConfig": None, "Links": None,
                                                       "Aliases": None, "DriverOpts": None}}},
    }


def extract(raw: dict, *, daemon_hostname: str | None = None) -> dict:
    return observe._extract(raw, container_id=ID, expected_image_id=IMAGE, project="project", service="bot",
                            expected_command=["bot"], expected_entrypoint=["/entrypoint.sh"],
                            env_fingerprint_key=KEY, daemon_hostname=daemon_hostname)


def test_reading_effective_paths_is_nonsecret_and_does_not_claim_health() -> None:
    raw = observation()
    result = extract(raw)
    assert result["health"] == "unverified"
    assert result["path_settings"]["WUWATERM_STATE_DIR"] == "/app/relative-state"
    assert result["recipe"]["mounts"] == [{"host": "/srv/state", "container": "/app/state", "read_only": False}]
    assert "env_files" not in result["recipe"]
    assert PRIVATE not in json.dumps(result)
    assert "TELEGRAM_BOT_TOKEN" not in json.dumps(result)
    raw["Config"]["Env"][-1] = "TELEGRAM_BOT_TOKEN=another-secret-for-test"
    changed = extract(raw)
    assert result["environment_fingerprint"] != changed["environment_fingerprint"]
    assert "another-secret-for-test" not in json.dumps(changed)



@pytest.mark.parametrize("missing_mounts", [False, True])
@pytest.mark.parametrize("oom_default", [False, None])
def test_docker_defaults_keep_bind_recipe_when_host_mounts_is_absent(oom_default, missing_mounts):
    raw = observation()
    if missing_mounts:
        del raw["HostConfig"]["Mounts"]
    raw["HostConfig"]["Binds"] = ["/srv/state:/app/state:rw"]
    raw["HostConfig"]["OomKillDisable"] = oom_default
    assert extract(raw)["recipe"]["mounts"] == [
        {"host": "/srv/state", "container": "/app/state", "read_only": False},
    ]


def test_explicit_oom_kill_disable_is_not_silently_replayed():
    raw = observation()
    raw["HostConfig"]["OomKillDisable"] = True
    with pytest.raises(observe.ObservationError, match="host settings"):
        extract(raw)


def test_default_host_network_hostname_and_absent_tmpfs_are_observed() -> None:
    raw = observation()
    raw["Config"]["Hostname"] = "daemon-host"
    del raw["HostConfig"]["Tmpfs"]
    assert extract(raw, daemon_hostname="daemon-host")["recipe"]["network_mode"] == "host"
    with pytest.raises(observe.ObservationError, match="hostname"):
        extract(raw, daemon_hostname="different-host")


@pytest.mark.skipif(not os.environ.get("WUWATERM_LEGACY_OBSERVE_TEST_ID"),
                    reason="legacy Docker fixture was not supplied")
def test_real_legacy_core_host_network_observation() -> None:
    container_id = os.environ["WUWATERM_LEGACY_OBSERVE_TEST_ID"]
    image_id = os.environ["WUWATERM_LEGACY_OBSERVE_TEST_IMAGE"]
    result = observe.observe_container(
        container_id, expected_image_id=image_id, project="wuwaterm-observe-legacy-cbe9", service="api",
        expected_command=["api"], expected_entrypoint=["/entrypoint.sh"], env_fingerprint_key=KEY,
    )
    assert result["container"]["id"] == container_id
    assert result["container"]["running"] is False
    assert result["recipe"]["network_mode"] == "host"
    assert result["health"] == "unverified"


@pytest.mark.parametrize("value", ["~", "~/state", "~other/state", " /app/state", "relative/../state"])
def test_state_paths_with_unresolved_expansion_or_symlink_sensitive_segments_fail(value: str) -> None:
    raw = observation()
    raw["Config"]["Env"][1] = "WUWATERM_STATE_DIR=" + value
    with pytest.raises(observe.ObservationError, match="path setting"):
        extract(raw)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(Id="c" * 64),
    lambda raw: raw.update(Image="sha256:" + "c" * 64),
    lambda raw: raw["Config"]["Labels"].update({"com.docker.compose.service": "another"}),
    lambda raw: raw["Config"].update(Cmd=["bot", "--token=" + PRIVATE]),
    lambda raw: raw["Config"].update(NewUnknownField=PRIVATE),
    lambda raw: raw["Config"]["Env"].append("TELEGRAM_BOT_TOKEN=duplicate"),
    lambda raw: raw["HostConfig"].update(AutoRemove=True),
    lambda raw: raw["HostConfig"].update(Memory=512),
    lambda raw: raw["HostConfig"].update(UnknownDockerSetting=True),
    lambda raw: raw["HostConfig"].update(SecurityOpt=["seccomp=unconfined"]),
    lambda raw: raw["Mounts"][0].update(Propagation="shared"),
    lambda raw: raw["Mounts"][0].update(Type="volume"),
    lambda raw: raw["State"].update(Restarting=True),
    lambda raw: raw["NetworkSettings"]["Networks"]["host"].update(IPAMConfig={"IPv4Address": "127.0.0.1"}),
    lambda raw: raw["NetworkSettings"]["Networks"]["host"].update(Aliases=["unexpected"]),
])
def test_unknown_or_changed_bindings_refuse_without_exposing_values(mutation) -> None:
    raw = observation()
    mutation(raw)
    with pytest.raises(observe.ObservationError) as caught:
        extract(raw)
    assert PRIVATE not in str(caught.value)


def test_explicit_command_and_key_are_validated_before_docker_inspection(monkeypatch) -> None:
    def forbidden(*args):
        raise AssertionError("must not inspect Docker")

    monkeypatch.setattr(observe, "_inspect", forbidden)
    args = dict(expected_image_id=IMAGE, project="project", service="bot",
                expected_command=["bot", PRIVATE], expected_entrypoint=["/entrypoint.sh"],
                env_fingerprint_key=KEY)
    with pytest.raises(observe.ObservationError, match="unsupported"):
        observe.observe_container(ID, **args)
    args.update(expected_command=["bot"], env_fingerprint_key=b"short")
    with pytest.raises(observe.ObservationError, match="fingerprint key"):
        observe.observe_container(ID, **args)


def test_path_mapping_follows_longest_mount_and_refuses_readonly_or_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "state"
    source.mkdir()
    recipe = {"mounts": [{"host": str(source), "container": "/app/state", "read_only": False}]}
    assert observe.resolve_bind_path(recipe, "/app/state/settings.json", writable=True) == str(source / "settings.json")
    readonly = copy.deepcopy(recipe)
    readonly["mounts"][0]["read_only"] = True
    with pytest.raises(observe.ObservationError, match="read-only"):
        observe.resolve_bind_path(readonly, "/app/state/settings.json", writable=True)
    shadow = copy.deepcopy(recipe)
    shadow["mounts"].append({"type": "tmpfs", "container": "/app/state/tmp", "options": []})
    with pytest.raises(observe.ObservationError, match="not on an observed bind"):
        observe.resolve_bind_path(shadow, "/app/state/tmp/settings.json", writable=True)
    (source / "outside").symlink_to(tmp_path)
    with pytest.raises(observe.ObservationError, match="symlink"):
        observe.resolve_bind_path(recipe, "/app/state/outside/settings.json", writable=True)


@pytest.mark.skipif(not os.environ.get("WUWATERM_OBSERVE_TEST_ID"), reason="Docker fixture was not supplied")
def test_real_docker_container_inspection_and_wrong_binding() -> None:
    container_id = os.environ["WUWATERM_OBSERVE_TEST_ID"]
    image = os.environ["WUWATERM_OBSERVE_TEST_IMAGE"]
    args = dict(expected_image_id=image, project="wuwaterm-observe-cbe9", service="api",
                expected_command=["bot"], expected_entrypoint=None, env_fingerprint_key=KEY)
    result = observe.observe_container(container_id, **args)
    assert result["container"]["id"] == container_id
    assert result["container"]["image_id"] == image
    assert result["container"]["running"] is False
    assert result["health"] == "unverified"
    assert result["path_settings"]["WUWATERM_STATE_DIR"] == "/app/relative-state"
    assert len(result["recipe"]["mounts"]) == 2
    assert any(mount.get("type") == "tmpfs" for mount in result["recipe"]["mounts"])
    assert "not-a-real-secret" not in json.dumps(result)
    assert "OBSERVE_TEST_SECRET" not in json.dumps(result)
    args["project"] = "incorrect"
    with pytest.raises(observe.ObservationError, match="identity"):
        observe.observe_container(container_id, **args)
