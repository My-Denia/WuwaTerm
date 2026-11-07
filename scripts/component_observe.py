#!/usr/bin/env python3
"""Read a tightly bounded Docker container observation.

This module does not assert readiness, resolve Docker env-file sources, or
perform deployment. Docker's inspect response contains secrets; it stays in
memory and must never be logged or returned. Unsupported runtime settings are
refused because a recipe which omits them could not reproduce the old service.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import socket
import stat
from typing import Any

try:
    from scripts import component_runtime as runtime
    from scripts import product_manifest as manifest
except ModuleNotFoundError:  # isolated capsule scripts directory
    import component_runtime as runtime
    import product_manifest as manifest

SOCKET = "/run/docker.sock"
TRANSACTION_LABEL = "org.wuwaterm.component-transaction"
_PATH_KEYS = tuple(sorted(manifest._PATH_ENV))
_MAX_RESPONSE = 8 * 1024 * 1024


class ObservationError(RuntimeError):
    """An actual container cannot be safely or completely observed."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def _docker_json(endpoint: str) -> dict[str, Any]:
    try:
        if not stat.S_ISSOCK(os.stat(SOCKET, follow_symlinks=False).st_mode):
            raise ObservationError("Docker endpoint is not a Unix socket")
        connection = _UnixHTTPConnection("localhost", timeout=15)
        try:
            connection.request("GET", endpoint)
            response = connection.getresponse()
            if response.status != 200:
                raise ObservationError("Docker did not return the requested metadata")
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ObservationError("Docker observation exceeds the size limit")
            value = json.loads(raw)
        finally:
            connection.close()
    except ObservationError:
        raise
    except (OSError, ValueError, http.client.HTTPException, UnicodeError):
        # Do not chain parser or HTTP exceptions: their attributes may retain
        # Docker's raw response, including its complete environment.
        raise ObservationError("Docker inspection failed") from None
    if type(value) is not dict:
        raise ObservationError("Docker inspection returned an invalid object")
    return value


def _inspect(container_id: str) -> dict[str, Any]:
    return _docker_json(f"/containers/{container_id}/json")


def _daemon_hostname() -> str:
    # Docker supplies its host name to default host-network containers. The
    # info response can contain private details and stays entirely in memory.
    name = _docker_json("/info").get("Name")
    if type(name) is not str or not name:
        raise ObservationError("Docker host identity is unavailable")
    return name


def _mapping(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ObservationError("Docker observation has an invalid structure")
    return value


def _sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if type(value) is not list:
        raise ObservationError("Docker observation has an invalid list")
    return value


def _nonsecret_argv(value: Any, *, entrypoint: bool) -> None:
    """Only currently supported, known product commands can be recorded.

    Arbitrary arguments may contain credentials even without a recognizable
    --token flag. Additional commands need a reviewed nonsecret contract.
    """
    if value is None or value == []:
        return
    if entrypoint:
        if value not in (["/entrypoint.sh"], ["wuwaterm-telegram"]):
            raise ObservationError("container entrypoint is unsupported")
    else:
        if value in (["api"], ["bot"]):
            return
        if type(value) is list and len(value) == 2 and value[0] == "--db":
            try:
                manifest._path(value[1], "container database path")
            except manifest.ManifestError as exc:
                raise ObservationError("container command is unsupported") from exc
            return
        raise ObservationError("container command is unsupported")


def _environment(value: Any, working_dir: str | None, key: bytes) -> tuple[dict[str, str | None], str]:
    if type(key) is not bytes or len(key) < 32:
        raise ObservationError("a private environment fingerprint key is required")
    env: dict[str, str] = {}
    for item in _sequence(value):
        if type(item) is not str or "=" not in item:
            raise ObservationError("container environment is invalid")
        name, content = item.split("=", 1)
        if not manifest._ENV_NAME.fullmatch(name) or name in env or "\x00" in content:
            raise ObservationError("container environment is ambiguous")
        env[name] = content
    # The key is supplied and retained by the caller. A digest is not a
    # substitute for proving that a referenced env file reproduces these bytes.
    digest = hmac.new(key, b"wuwaterm-container-environment-v1\0", hashlib.sha256)
    for name, content in sorted(env.items()):
        for part in (name, content):
            try:
                encoded = part.encode("utf-8")
            except UnicodeError:
                raise ObservationError("container environment is invalid") from None
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    settings: dict[str, str | None] = {}
    for name in _PATH_KEYS:
        content = env.get(name)
        if content is None or content == "":
            settings[name] = None
            continue
        if (len(content) > 4096 or "\\" in content or content.startswith("~") or
                content != content.strip() or any(part in (".", "..") for part in content.split("/")) or
                any(ord(c) < 32 or ord(c) == 127 for c in content)):
            raise ObservationError("container path setting is invalid")
        path = posixpath.normpath(posixpath.join(working_dir or "/", content))
        try:
            manifest._path(path, "container path setting")
        except manifest.ManifestError as exc:
            raise ObservationError("container path setting is invalid") from exc
        settings[name] = path
    return settings, digest.hexdigest()


# These are Docker's default values for settings not expressible by the
# product recipe schema. A changed value must not be silently dropped on replay.
_ZERO_FIELDS = frozenset({
    "BlkioWeight", "CpuCount", "CpuPercent", "CpuPeriod", "CpuQuota",
    "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpuShares", "IOMaximumBandwidth",
    "IOMaximumIOps", "Memory", "MemoryReservation", "MemorySwap", "NanoCpus", "OomScoreAdj",
})
_EMPTY_STRING_FIELDS = frozenset({
    "Cgroup", "CgroupParent", "ContainerIDFile", "CpusetCpus", "CpusetMems", "Isolation",
    "UTSMode", "UsernsMode", "VolumeDriver",
})
_EMPTY_ARRAY_FIELDS = frozenset({
    "BlkioDeviceReadBps", "BlkioDeviceReadIOps", "BlkioDeviceWriteBps", "BlkioDeviceWriteIOps",
    "BlkioWeightDevice", "DeviceCgroupRules", "DeviceRequests", "Devices", "DnsOptions", "DnsSearch",
    "GroupAdd", "Links", "Ulimits", "VolumesFrom",
})
_DEFAULT_MASKED = frozenset({
    "/proc/acpi", "/proc/asound", "/proc/interrupts", "/proc/kcore", "/proc/keys", "/proc/latency_stats",
    "/proc/sched_debug", "/proc/scsi", "/proc/timer_list", "/proc/timer_stats",
    "/sys/devices/virtual/powercap", "/sys/firmware",
})
_DEFAULT_READONLY = frozenset({"/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"})
_SUPPORTED_FIELDS = frozenset({
    "Binds", "CapAdd", "CapDrop", "CgroupnsMode", "ConsoleSize", "Dns", "ExtraHosts", "IpcMode",
    "LogConfig", "MaskedPaths", "MemorySwappiness", "Mounts", "NetworkMode", "OomKillDisable",
    "PidMode", "PidsLimit", "PortBindings", "Privileged", "PublishAllPorts", "ReadonlyPaths",
    "ReadonlyRootfs", "RestartPolicy", "Runtime", "SecurityOpt", "ShmSize", "Tmpfs", "AutoRemove",
})


def _check_unsupported(host: dict[str, Any]) -> None:
    known = _ZERO_FIELDS | _EMPTY_STRING_FIELDS | _EMPTY_ARRAY_FIELDS | _SUPPORTED_FIELDS
    if set(host) - known or (known - {"Tmpfs", "Mounts"}) - set(host):
        raise ObservationError("Docker host configuration contains unknown or missing fields")
    if any(type(host[k]) is not int or host[k] != 0 for k in _ZERO_FIELDS):
        raise ObservationError("container resource settings cannot be replayed")
    if any(host[k] != "" for k in _EMPTY_STRING_FIELDS):
        raise ObservationError("container namespace settings cannot be replayed")
    if any(host[k] not in (None, []) for k in _EMPTY_ARRAY_FIELDS):
        raise ObservationError("container device or runtime settings cannot be replayed")
    if (host["AutoRemove"] is not False or (host["OomKillDisable"] is not False and host["OomKillDisable"] is not None) or
            host["PublishAllPorts"] is not False or host["MemorySwappiness"] is not None or
            host["PidsLimit"] is not None or host["ShmSize"] != 67108864 or
            host["ConsoleSize"] != [0, 0] or host["CgroupnsMode"] != "private" or
            host["Runtime"] != "runc" or host["LogConfig"] != {"Type": "json-file", "Config": {}} or
            frozenset(_sequence(host["MaskedPaths"])) != _DEFAULT_MASKED or
            frozenset(_sequence(host["ReadonlyPaths"])) != _DEFAULT_READONLY):
        raise ObservationError("container host settings cannot be replayed")


def _mounts(observed: Any, host: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _sequence(observed):
        item = _mapping(item)
        if (item.get("Type") != "bind" or item.get("Propagation") != "rprivate" or
                item.get("Mode") not in ("", "ro", "rw") or type(item.get("RW")) is not bool):
            raise ObservationError("container mount cannot be replayed")
        result.append({"host": item.get("Source"), "container": item.get("Destination"),
                       "read_only": not item["RW"]})
    # Docker's --mount accepts additional fields the product schema does not
    # represent. Only plain bind configurations are accepted.
    for item in _sequence(host.get("Mounts")):
        item = _mapping(item)
        if set(item) - {"Type", "Source", "Target", "ReadOnly"} or item.get("Type") != "bind":
            raise ObservationError("container mount options cannot be replayed")
        binding = {"host": item.get("Source"), "container": item.get("Target"),
                   "read_only": item.get("ReadOnly", False)}
        if binding not in result:
            raise ObservationError("container mount binding is inconsistent")
    tmpfs = host.get("Tmpfs") or {}
    if type(tmpfs) is not dict:
        raise ObservationError("container tmpfs is invalid")
    for target, options in tmpfs.items():
        if type(options) is not str:
            raise ObservationError("container tmpfs is invalid")
        result.append({"type": "tmpfs", "container": target, "options": options.split(",") if options else []})
    return sorted(result, key=lambda item: item.get("container") or "")


def _network(raw: dict[str, Any], host: dict[str, Any], name: str, service: str,
             container_id: str) -> None:
    """Refuse attachments whose addressing or aliases the recipe omits."""
    network_name = host["NetworkMode"]
    networks = _mapping(_mapping(raw.get("NetworkSettings")).get("Networks"))
    if type(network_name) is not str or set(networks) != {network_name}:
        raise ObservationError("container network attachments are unsupported")
    attachment = _mapping(networks[network_name])
    if attachment.get("IPAMConfig") not in (None, {}):
        raise ObservationError("static container network addressing is unsupported")
    if attachment.get("Links") not in (None, []):
        raise ObservationError("container network links are unsupported")
    if attachment.get("DriverOpts") not in (None, {}):
        raise ObservationError("container network options are unsupported")
    aliases = _sequence(attachment.get("Aliases"))
    if any(type(alias) is not str or alias not in {name, service, container_id[:12], container_id}
           for alias in aliases) or len(set(aliases)) != len(aliases):
        raise ObservationError("custom container network aliases are unsupported")


def _ports(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    result = []
    for port, bindings in _mapping(value).items():
        if type(port) is not str or not re.fullmatch(r"[0-9]+/(?:tcp|udp)", port):
            raise ObservationError("container ports cannot be replayed")
        number, protocol = port.split("/")
        for item in _sequence(bindings):
            item = _mapping(item)
            host_port = item.get("HostPort")
            if type(host_port) is not str or not host_port.isascii() or not host_port.isdigit():
                raise ObservationError("container port is dynamically allocated")
            result.append({"host_ip": item.get("HostIp") or None, "host_port": int(host_port),
                           "container_port": int(number), "protocol": protocol})
    return sorted(result, key=lambda item: (item["container_port"], item["protocol"],
                                            item["host_ip"] or "", item["host_port"]))


def resolve_bind_path(recipe: dict[str, Any], container_path: str, *, writable: bool) -> str:
    """Map a container path through its longest observed bind, without writes.

    Refuse tmpfs, read-only or unmapped paths, and host symlink traversal.
    This observation is not a substitute for rechecking filesystem identity
    under the deployment lock immediately before a transaction.
    """
    try:
        manifest._path(container_path, "container path")
        if type(writable) is not bool:
            raise ObservationError("writable mapping flag is invalid")
        candidates = []
        for mount in _sequence(_mapping(recipe).get("mounts")):
            mount = _mapping(mount)
            target = manifest._path(mount.get("container"), "mount target")
            candidate = PurePosixPath(container_path)
            base = PurePosixPath(target)
            if candidate == base or candidate.is_relative_to(base):
                candidates.append((len(base.parts), mount, candidate.relative_to(base)))
        if not candidates:
            raise ObservationError("container path has no visible host binding")
        _, chosen, relative = max(candidates, key=lambda item: item[0])
        if "host" not in chosen or type(chosen.get("read_only")) is not bool:
            raise ObservationError("container path is not on an observed bind")
        if writable and chosen["read_only"]:
            raise ObservationError("container path binding is read-only")
        source = manifest._path(chosen["host"], "mount source")
        host_path = Path(source).joinpath(*relative.parts)
        # Existing ancestors must be physical paths. A missing final path is
        # reported without creating it, for the coordinator's preflight.
        cursor = Path("/")
        for part in host_path.parts[1:]:
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                if cursor == Path(source) or not cursor.is_relative_to(Path(source)):
                    raise ObservationError("container host binding is unavailable") from None
                break
            if stat.S_ISLNK(info.st_mode):
                raise ObservationError("container host binding crosses a symlink")
        return str(host_path)
    except manifest.ManifestError as exc:
        raise ObservationError("container path binding is invalid") from exc


def _extract(raw: dict[str, Any], *, container_id: str, expected_image_id: str, project: str,
             service: str, expected_command: list[str] | None, expected_entrypoint: list[str] | None,
             env_fingerprint_key: bytes, daemon_hostname: str | None = None) -> dict[str, Any]:
    config = _mapping(raw.get("Config"))
    host = _mapping(raw.get("HostConfig"))
    state = _mapping(raw.get("State"))
    labels = _mapping(config.get("Labels"))
    known_config = {
        "AttachStderr", "AttachStdin", "AttachStdout", "Cmd", "Domainname", "Entrypoint", "Env",
        "Hostname", "Image", "Labels", "OpenStdin", "StdinOnce", "Tty", "User", "Volumes",
        "WorkingDir", "ExposedPorts", "Healthcheck", "MacAddress", "StopSignal", "StopTimeout",
    }
    if set(config) - known_config:
        raise ObservationError("Docker container configuration contains unknown fields")
    if (raw.get("Id") != container_id or raw.get("Image") != expected_image_id or
            labels.get("com.docker.compose.project") != project or
            labels.get("com.docker.compose.service") != service):
        raise ObservationError("container identity does not match the explicit binding")
    if config.get("Cmd") != expected_command or config.get("Entrypoint") != expected_entrypoint:
        raise ObservationError("container command or entrypoint differs from the binding")
    if any(config.get(name) not in (None, False) for name in ("Tty", "OpenStdin", "StdinOnce", "AttachStdin")):
        raise ObservationError("interactive container configuration is unsupported")
    hostname = config.get("Hostname")
    host_default = host.get("NetworkMode") == "host" and daemon_hostname is not None and hostname == daemon_hostname
    if (hostname not in ("", container_id[:12]) and not host_default) or config.get("Domainname") not in (None, ""):
        raise ObservationError("custom container hostname is unsupported")
    if any(config.get(name) not in (None, "") for name in ("MacAddress", "StopSignal", "StopTimeout", "Healthcheck")):
        raise ObservationError("container config overrides are unsupported")
    if (state.get("Paused") is not False or state.get("Restarting") is not False or
            state.get("Dead") is not False or state.get("Error") not in (None, "") or
            state.get("Status") not in ("created", "exited", "running")):
        raise ObservationError("container state is not stable")
    _check_unsupported(host)
    name = raw.get("Name")
    if type(name) is not str or not name.startswith("/"):
        raise ObservationError("container name is invalid")
    name = name[1:]
    _network(raw, host, name, service, container_id)
    transaction_label = labels.get(TRANSACTION_LABEL)
    container = {"id": container_id, "image_id": expected_image_id, "running": state.get("Running"),
                 "started_at": state.get("StartedAt"), "restart_count": raw.get("RestartCount"),
                 "project": project, "service": service, "name": name,
                 "transaction_label": transaction_label}
    try:
        runtime._container(container, "container observation", observed=True)
    except runtime.JournalError as exc:
        raise ObservationError("container identity or state is invalid") from exc
    working_dir = config.get("WorkingDir") or None
    user = config.get("User") or None
    if working_dir is not None:
        try:
            manifest._path(working_dir, "container working directory")
        except manifest.ManifestError as exc:
            raise ObservationError("container working directory is invalid") from exc
    settings, fingerprint = _environment(config.get("Env"), working_dir, env_fingerprint_key)
    restart = _mapping(host["RestartPolicy"])
    recipe = {
        "project": project, "service": service, "container_name": name,
        "command": expected_command, "entrypoint": expected_entrypoint,
        "mounts": _mounts(raw.get("Mounts"), host), "network_mode": host["NetworkMode"],
        "ports": _ports(host["PortBindings"]),
        "restart": {"name": restart.get("Name"), "maximum_retry_count": restart.get("MaximumRetryCount")},
        "user": user, "working_dir": working_dir,
        "runtime_options": {
            "read_only_root": host["ReadonlyRootfs"], "privileged": host["Privileged"], "init": None,
            "pid_mode": host["PidMode"], "ipc_mode": host["IpcMode"],
            "cap_add": _sequence(host["CapAdd"]), "cap_drop": _sequence(host["CapDrop"]),
            "security_opt": _sequence(host["SecurityOpt"]), "dns": _sequence(host["Dns"]),
            "extra_hosts": _sequence(host["ExtraHosts"]),
        },
    }
    # Env-file references and persistent overrides cannot be recovered from
    # inspect. The validation placeholders are never returned as observations.
    snapshot = {**recipe, "schema_version": 1, "image_id": expected_image_id,
                "env_files": [], "environment_overrides": []}
    try:
        manifest.validate_recipe_snapshot(snapshot)
    except (manifest.ManifestError, TypeError, ValueError) as exc:
        raise ObservationError("container recipe is not representable") from exc
    return {"container": container, "recipe": recipe, "path_settings": settings,
            "environment_fingerprint": fingerprint, "health": "unverified"}


def observe_container(container_id: str, *, expected_image_id: str, project: str, service: str,
                      expected_command: list[str] | None, expected_entrypoint: list[str] | None,
                      env_fingerprint_key: bytes) -> dict[str, Any]:
    """Observe one explicitly bound container through the local Docker socket.

    Return only replayable, nonsecret observations and a keyed fingerprint of
    the actual environment; do not infer env-file references or readiness.
    The caller must keep the fingerprint key private and independently verify
    source provenance, state mappings, env-file references and service health.
    """
    try:
        runtime._match(container_id, runtime._HEX64, "container ID")
        runtime._image_id(expected_image_id, "image ID")
        manifest._name(project, "project")
        manifest._name(service, "service")
        _nonsecret_argv(expected_command, entrypoint=False)
        _nonsecret_argv(expected_entrypoint, entrypoint=True)
        if type(env_fingerprint_key) is not bytes or len(env_fingerprint_key) < 32:
            raise ObservationError("a private environment fingerprint key is required")
    except (runtime.JournalError, manifest.ManifestError) as exc:
        raise ObservationError("explicit container binding is invalid") from exc
    raw = _inspect(container_id)
    config, host = _mapping(raw.get("Config")), _mapping(raw.get("HostConfig"))
    hostname = config.get("Hostname")
    daemon_hostname = (_daemon_hostname() if host.get("NetworkMode") == "host"
                       and hostname not in ("", container_id[:12]) else None)
    return _extract(raw, container_id=container_id, expected_image_id=expected_image_id,
                    project=project, service=service, expected_command=expected_command,
                    expected_entrypoint=expected_entrypoint, env_fingerprint_key=env_fingerprint_key,
                    daemon_hostname=daemon_hostname)
