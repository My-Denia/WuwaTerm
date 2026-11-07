"""Strict, immutable records for installed product deployments.

The transaction coordinator must establish the facts in these records from
actual images, mounts, build receipts and the database. Schema validation is
not evidence that a build-supplied revision claim is true. The operations
``source_tool_sha256`` identifies the commit, tree and exact tool bytes; it is
not a digest of a single script. A recipe snapshot is a deliberately limited
declarative representation: callers must refuse a service with effective
settings that this representation cannot reproduce.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any, Iterator
from urllib.parse import urlsplit


# Schema 3 is written by protocol-2 (completed-artifact) updates. Schema 2
# remains readable: historical journals embed schema-2 manifests and their
# source-image / wheel-image provenance is interpreted only by schema 2.
MANIFEST_SCHEMA_VERSION = 3
PREVIOUS_MANIFEST_SCHEMA_VERSION = 2
SCHEMA_VERSION = MANIFEST_SCHEMA_VERSION
PREVIOUS_SCHEMA_VERSION = PREVIOUS_MANIFEST_SCHEMA_VERSION
PROVENANCE_KINDS = {
    PREVIOUS_MANIFEST_SCHEMA_VERSION: frozenset({"source-image", "wheel-image", "legacy-image"}),
    MANIFEST_SCHEMA_VERSION: frozenset({"completed-artifact", "legacy-image"}),
}
ARTIFACT_REPOSITORIES = {"api": "My-Denia/WuwaTerm", "telegram": "My-Denia/WuwaTerm-Telegram"}
ARTIFACT_PRODUCTS = {"api": "wuwaterm-api", "telegram": "wuwaterm-telegram"}
RECIPE_SCHEMA_VERSION = 1
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GIT = re.compile(r"[0-9a-f]{40}\Z")
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,511}\Z")

DB_PROVENANCE_KEYS = frozenset({
    "schema_version", "source_profile", "source_repo_url", "source_commit",
    "source_game_version", "source_resource_version", "source_changelist",
    "wutheringdata_commit",
})
# Literal values are limited to nonsecret process-routing options. All other
# overrides must be empty; secret values belong in the referenced env files.
_LITERAL_ENV = frozenset({
    "WUWATERM_DB_PATH", "WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH",
    "WUWATERM_CHANNEL_REPLY_INDEX_PATH", "WUWATERM_API_STATE_DIR",
    "WUWATERM_API_DEVICE_DB_PATH", "WUWATERM_API_BIND", "WUWATERM_API_PORT",
    "WUWATERM_API_WEB_ENABLED", "WUWATERM_POLL_HEALTH",
})
_PATH_ENV = frozenset({
    "WUWATERM_DB_PATH", "WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH",
    "WUWATERM_CHANNEL_REPLY_INDEX_PATH", "WUWATERM_API_STATE_DIR",
    "WUWATERM_API_DEVICE_DB_PATH",
})
_RECIPE_FIELDS = frozenset({
    "project", "service", "container_name", "command", "entrypoint", "mounts",
    "env_files", "network_mode", "ports", "restart", "user", "working_dir",
})
_LIMIT = 4 * 1024 * 1024
_DB_SCHEMA = {
    "metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
    "terms": (
        ("id", "INTEGER", 0, 1), ("category", "TEXT", 1, 0),
        ("source_file", "TEXT", 1, 0), ("source_id", "TEXT", 1, 0),
        ("text_key", "TEXT", 1, 0), ("zh", "TEXT", 1, 0),
        ("en", "TEXT", 1, 0), ("zh_norm", "TEXT", 1, 0),
        ("en_norm", "TEXT", 1, 0), ("pinyin", "TEXT", 1, 0),
        ("pinyin_abbrev", "TEXT", 1, 0), ("priority", "INTEGER", 1, 0),
    ),
}
_DB_INDEXES = {
    "metadata": {"sqlite_autoindex_metadata_1": (1, "pk")},
    "terms": {
        "idx_terms_category": (0, "c"), "idx_terms_pinyin": (0, "c"),
        "idx_terms_en_norm": (0, "c"), "idx_terms_zh_norm": (0, "c"),
        "sqlite_autoindex_terms_1": (1, "u"),
    },
}


class ManifestError(ValueError):
    """A record or its immutable backing file is incomplete or untrusted."""


def _fail(message: str) -> None:
    raise ManifestError(message)


def _object(value: Any, keys: set[str] | frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{where}: unexpected or missing fields")
    return value


def _text(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or (not allow_empty and not value)
            or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail(f"{where}: invalid text")
    return value


def _matches(value: Any, pattern: re.Pattern[str], where: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(f"{where}: invalid value")
    return value


def _number(value: Any, where: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{where}: invalid integer")
    return value


def _boolean(value: Any, where: str) -> bool:
    if type(value) is not bool:
        _fail(f"{where}: expected boolean")
    return value


def _path(value: Any, where: str) -> str:
    value = _text(value, where)
    if value == "/":
        return value
    if not value.startswith("/") or "\\" in value or any(
        part in (".", "..", "") for part in value.split("/")[1:]
    ):
        _fail(f"{where}: expected normalized absolute path")
    return value


def _recipe_environment_path(value: Any, where: str) -> str:
    """Keep a container path literal while requiring a normalized spelling."""
    value = _text(value, where)
    if value != value.strip() or value.startswith("~") or "\\" in value:
        _fail(f"{where}: expected normalized container path")
    if value.startswith("/"):
        return _path(value, where)
    if any(part in ("", ".", "..") for part in value.split("/")):
        _fail(f"{where}: expected normalized container path")
    return value


def _name(value: Any, where: str) -> str:
    return _matches(value, _NAME, where)


def _sha(value: Any, where: str) -> str:
    return _matches(value, _SHA, where)


def _git(value: Any, where: str) -> str:
    return _matches(value, _GIT, where)


def _image_id(value: Any, where: str) -> str:
    value = _text(value, where)
    if not value.startswith("sha256:"):
        _fail(f"{where}: expected sha256 image ID")
    _sha(value[7:], where)
    return value


def _image_ref(value: Any, where: str) -> str:
    value = _matches(value, _IMAGE, where)
    if "@" in value:
        parts = value.split("@")
        if len(parts) != 2 or not parts[1].startswith("sha256:"):
            _fail(f"{where}: invalid digest reference")
        _sha(parts[1][7:], where)
    return value


def _identity(value: Any, where: str, *, claim_required: bool) -> dict[str, Any]:
    identity = _object(value, {"revision_claim", "verified_commit", "verified_tree"}, where)
    claim = identity["revision_claim"]
    if claim is None:
        if claim_required:
            _fail(f"{where}: revision claim missing")
    else:
        _git(claim, f"{where}.revision_claim")
    commit, tree = identity["verified_commit"], identity["verified_tree"]
    if (commit is None) != (tree is None):
        _fail(f"{where}: commit and tree must be recorded together")
    if commit is not None:
        _git(commit, f"{where}.verified_commit")
        _git(tree, f"{where}.verified_tree")
        if commit != claim:
            _fail(f"{where}: verified commit differs from revision claim")
    return identity


def _argv(value: Any, where: str) -> None:
    # Docker distinguishes null (no value supplied) from an explicit empty
    # array; retain the observation so a rollback can reproduce it.
    if value is None:
        return
    if not isinstance(value, list):
        _fail(f"{where}: expected array or null")
    for argument in value:
        argument = _text(argument, where, allow_empty=True)
        # Do not serialize the common forms of inlined credential arguments.
        if re.search(r"(?i)(?:token|secret|password|credential|api[_-]?key)\s*=", argument) or re.fullmatch(
            r"(?i)--?(?:[a-z0-9_-]*[-_])?(?:token|secret|password|credential|api[-_]?key)", argument
        ):
            _fail(f"{where}: inline credential assignment is forbidden")


def _tmpfs_options(value: Any, where: str) -> None:
    if not isinstance(value, list):
        _fail(f"{where}: expected array")
    seen: set[str] = set()
    switches = {
        "rw": "access", "ro": "access", "exec": "exec", "noexec": "exec",
        "suid": "suid", "nosuid": "suid", "dev": "dev", "nodev": "dev",
    }
    for option in value:
        option = _text(option, where)
        if option in switches:
            key = switches[option]
        elif re.fullmatch(r"mode=[0-7]{3,4}", option):
            key = "mode"
            if int(option[5:], 8) > 0o7777:
                _fail(f"{where}: invalid mode")
        elif re.fullmatch(r"size=[0-9]+", option):
            key = "size"
            if not 1 <= int(option[5:]) <= 2**63 - 1:
                _fail(f"{where}: invalid size")
        elif re.fullmatch(r"(?:uid|gid)=[0-9]+", option):
            key, number = option.split("=", 1)
            if int(number) > 2**32 - 2:
                _fail(f"{where}: invalid owner")
        else:
            _fail(f"{where}: unsupported option")
        if key in seen:
            _fail(f"{where}: duplicate or conflicting option")
        seen.add(key)


def _recipe(value: Any, where: str) -> dict[str, Any]:
    recipe = _object(value, set(_RECIPE_FIELDS), where)
    _name(recipe["project"], f"{where}.project")
    _name(recipe["service"], f"{where}.service")
    if recipe["container_name"] is not None:
        _name(recipe["container_name"], f"{where}.container_name")
    _argv(recipe["command"], f"{where}.command")
    _argv(recipe["entrypoint"], f"{where}.entrypoint")
    mounts = recipe["mounts"]
    if not isinstance(mounts, list):
        _fail(f"{where}.mounts: expected array")
    destinations = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            _fail(f"{where}.mount: expected object")
        if set(mount) == {"host", "container", "read_only"}:
            _path(mount["host"], f"{where}.mount.host")
            _boolean(mount["read_only"], f"{where}.mount.read_only")
        elif set(mount) == {"type", "container", "options"} and mount["type"] == "tmpfs":
            _tmpfs_options(mount["options"], f"{where}.mount.options")
        else:
            _fail(f"{where}.mount: unsupported fields or type")
        target = _path(mount["container"], f"{where}.mount.container")
        if target in destinations:
            _fail(f"{where}.mounts: duplicate container path")
        destinations.add(target)
    env_files = recipe["env_files"]
    if not isinstance(env_files, list):
        _fail(f"{where}.env_files: expected array")
    for path in env_files:
        _path(path, f"{where}.env_file")
    if len(set(env_files)) != len(env_files):
        _fail(f"{where}.env_files: duplicate reference")
    _name(recipe["network_mode"], f"{where}.network_mode")
    ports = recipe["ports"]
    if not isinstance(ports, list):
        _fail(f"{where}.ports: expected array")
    seen_ports = set()
    for port in ports:
        _object(port, {"host_ip", "host_port", "container_port", "protocol"}, f"{where}.port")
        if port["host_ip"] is not None:
            try:
                ipaddress.ip_address(_text(port["host_ip"], f"{where}.port.host_ip"))
            except ValueError:
                _fail(f"{where}.port.host_ip: invalid IP")
        _number(port["host_port"], f"{where}.port.host_port", 1, 65535)
        _number(port["container_port"], f"{where}.port.container_port", 1, 65535)
        if port["protocol"] not in ("tcp", "udp"):
            _fail(f"{where}.port.protocol: invalid protocol")
        key = (port["host_ip"], port["host_port"], port["container_port"], port["protocol"])
        if key in seen_ports:
            _fail(f"{where}.ports: duplicate port")
        seen_ports.add(key)
    restart = _object(recipe["restart"], {"name", "maximum_retry_count"}, f"{where}.restart")
    if restart["name"] not in ("no", "always", "unless-stopped", "on-failure"):
        _fail(f"{where}.restart.name: invalid policy")
    retries = _number(restart["maximum_retry_count"], f"{where}.restart.maximum_retry_count")
    if restart["name"] != "on-failure" and retries:
        _fail(f"{where}.restart: retries require on-failure")
    if recipe["user"] is not None:
        _text(recipe["user"], f"{where}.user")
    if recipe["working_dir"] is not None:
        _path(recipe["working_dir"], f"{where}.working_dir")
    return recipe


def validate_recipe_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _object(value, set(_RECIPE_FIELDS) | {
        "schema_version", "image_id", "environment_overrides", "runtime_options",
    }, "recipe snapshot")
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != RECIPE_SCHEMA_VERSION:
        _fail("recipe snapshot: unsupported schema version")
    _image_id(snapshot["image_id"], "recipe snapshot.image_id")
    _recipe({key: snapshot[key] for key in _RECIPE_FIELDS}, "recipe snapshot")
    overrides = snapshot["environment_overrides"]
    if not isinstance(overrides, list):
        _fail("recipe snapshot.environment_overrides: expected array")
    names = set()
    for item in overrides:
        if not isinstance(item, dict) or item.get("mode") not in ("empty", "literal"):
            _fail("recipe snapshot: invalid environment override")
        keys = {"name", "mode"} if item["mode"] == "empty" else {"name", "mode", "value"}
        _object(item, keys, "recipe snapshot.environment_override")
        name = _matches(item["name"], _ENV_NAME, "recipe snapshot.environment_override.name")
        if name in names:
            _fail("recipe snapshot: duplicate environment override")
        names.add(name)
        if item["mode"] == "literal":
            if name not in _LITERAL_ENV:
                _fail("recipe snapshot: literal environment value is not allowed")
            value = _text(item["value"], "recipe snapshot.environment_override.value")
            if name in _PATH_ENV:
                _recipe_environment_path(value, "recipe snapshot.environment_override.value")
            elif name == "WUWATERM_API_PORT":
                if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
                    _fail("recipe snapshot: invalid API port")
            elif name == "WUWATERM_API_BIND":
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    _fail("recipe snapshot: invalid API bind")
            elif name == "WUWATERM_POLL_HEALTH":
                if value not in ("0", "1"):
                    _fail("recipe snapshot: invalid polling health flag")
            elif value.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                _fail("recipe snapshot: invalid API web flag")
    options = _object(snapshot["runtime_options"], {
        "read_only_root", "privileged", "init", "pid_mode", "ipc_mode",
        "cap_add", "cap_drop", "security_opt", "dns", "extra_hosts",
    }, "recipe snapshot.runtime_options")
    _boolean(options["read_only_root"], "recipe snapshot.runtime_options.read_only_root")
    _boolean(options["privileged"], "recipe snapshot.runtime_options.privileged")
    if options["init"] is not None:
        _boolean(options["init"], "recipe snapshot.runtime_options.init")
    for key in ("pid_mode", "ipc_mode"):
        if options[key] not in ("", "private", "host", "shareable"):
            _fail(f"recipe snapshot.runtime_options.{key}: unsupported mode")
    for key in ("cap_add", "cap_drop", "security_opt", "dns", "extra_hosts"):
        if not isinstance(options[key], list):
            _fail(f"recipe snapshot.runtime_options.{key}: expected array")
        for item in options[key]:
            _text(item, f"recipe snapshot.runtime_options.{key}")
    for key in ("cap_add", "cap_drop"):
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", item) for item in options[key]):
            _fail(f"recipe snapshot.runtime_options.{key}: invalid capability")
    if options["security_opt"]:
        _fail("recipe snapshot: custom security options are unsupported")
    for address in options["dns"]:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            _fail("recipe snapshot: invalid DNS address")
    for host in options["extra_hosts"]:
        pieces = host.rsplit(":", 1)
        if len(pieces) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", pieces[0]):
            _fail("recipe snapshot: invalid extra host")
        try:
            ipaddress.ip_address(pieces[1])
        except ValueError:
            _fail("recipe snapshot: invalid extra host address")
    return snapshot


def verify_poll_health_mount(snapshot: dict[str, Any], *, effective_uid: int) -> None:
    """Verify health tmpfs access against an independently observed process UID.

    A valid recipe and writable health mount do not prove the poller is ready;
    the caller must use the Telegram product's health command for that.
    """
    validate_recipe_snapshot(snapshot)
    _number(effective_uid, "poll health effective uid", 0, 2**32 - 2)
    if not any(item == {"name": "WUWATERM_POLL_HEALTH", "mode": "literal", "value": "1"}
               for item in snapshot["environment_overrides"]):
        _fail("poll health flag is not enabled")
    matches = [mount for mount in snapshot["mounts"]
               if mount.get("type") == "tmpfs" and mount["container"] == "/run/wuwaterm-telegram"]
    if len(matches) != 1:
        _fail("poll health tmpfs is missing")
    options = matches[0]["options"]
    if "ro" in options:
        _fail("poll health tmpfs is read-only")
    uid_options = [option for option in options if option.startswith("uid=")]
    mode_options = [option for option in options if option.startswith("mode=")]
    if effective_uid and not uid_options:
        _fail("poll health tmpfs owner is not explicit")
    owner_uid = int(uid_options[0][4:]) if uid_options else 0
    if owner_uid != effective_uid:
        _fail("poll health tmpfs owner differs from process UID")
    if not mode_options or int(mode_options[0][5:], 8) & 0o700 != 0o700:
        _fail("poll health tmpfs owner lacks explicit read/write/search mode")


def validate_manifest_v2(value: Any) -> dict[str, Any]:
    """Validate a historical schema-2 manifest only."""
    manifest = validate_manifest(value)
    if manifest["schema_version"] != PREVIOUS_MANIFEST_SCHEMA_VERSION:
        _fail("manifest: expected schema version 2")
    return manifest


def validate_manifest(value: Any) -> dict[str, Any]:
    """Validate a schema-2 or schema-3 manifest by its own declared version."""
    manifest = _object(value, {
        "schema_version", "deployment_id", "deployment_utc", "operations",
        "components", "database", "api_identity",
    }, "manifest")
    version = manifest["schema_version"]
    if type(version) is not int or version not in PROVENANCE_KINDS:
        _fail("manifest: unsupported schema version")
    deployment_id = _matches(manifest["deployment_id"], _ID, "manifest.deployment_id")
    try:
        datetime.strptime(deployment_id[:16], "%Y%m%dT%H%M%SZ")
        timestamp = _text(manifest["deployment_utc"], "manifest.deployment_utc")
        if not timestamp.endswith("Z"):
            _fail("manifest.deployment_utc: expected UTC")
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        _fail("manifest: invalid deployment time")
    ops = _object(manifest["operations"], {
        "repository", "source_commit", "source_tree", "source_tool_sha256",
    }, "manifest.operations")
    _matches(ops["repository"], _REPOSITORY, "manifest.operations.repository")
    _git(ops["source_commit"], "manifest.operations.source_commit")
    _git(ops["source_tree"], "manifest.operations.source_tree")
    _sha(ops["source_tool_sha256"], "manifest.operations.source_tool_sha256")
    components = _object(manifest["components"], {"api", "telegram"}, "manifest.components")
    if all(component is None for component in components.values()):
        _fail("manifest: at least one component is required")
    for name, component in components.items():
        if component is None:
            continue
        where = f"manifest.components.{name}"
        component = _object(component, {
            "repository", "version", "source", "core", "image", "recipe", "provenance",
        }, where)
        _matches(component["repository"], _REPOSITORY, f"{where}.repository")
        _matches(component["version"], _VERSION, f"{where}.version")
        provenance = component["provenance"]
        if not isinstance(provenance, dict):
            _fail(f"{where}.provenance: expected object")
        kind = provenance.get("kind")
        if kind not in PROVENANCE_KINDS[version]:
            _fail(f"{where}: unsupported provenance")
        if kind == "completed-artifact":
            _object(provenance, {
                "kind", "bundle_sha256", "acquisition_sha256", "authority", "native_image_id",
            }, f"{where}.provenance")
            _sha(provenance["bundle_sha256"], f"{where}.provenance.bundle_sha256")
            _sha(provenance["acquisition_sha256"], f"{where}.provenance.acquisition_sha256")
            _image_id(provenance["native_image_id"], f"{where}.provenance.native_image_id")
            if provenance["authority"] not in ("official-ci", "local-source"):
                _fail(f"{where}: invalid completed-artifact authority")
            if component["repository"] != ARTIFACT_REPOSITORIES[name]:
                _fail(f"{where}: completed-artifact repository differs from component")
        elif kind == "source-image":
            _object(provenance, {
                "kind", "build_recipe_sha256", "build_receipt_sha256", "wheel_identity",
            }, f"{where}.provenance")
            if name != "api" or provenance["wheel_identity"] != "not-built-from-wheel":
                _fail(f"{where}: invalid source-image provenance")
            _sha(provenance["build_recipe_sha256"], f"{where}.provenance.build_recipe_sha256")
            _sha(provenance["build_receipt_sha256"], f"{where}.provenance.build_receipt_sha256")
        elif kind == "wheel-image":
            _object(provenance, {
                "kind", "telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256",
                "core_producer_kind", "build_receipt_sha256",
            }, f"{where}.provenance")
            if name != "telegram" or provenance["core_producer_kind"] not in (
                "github-actions-artifact", "github-release",
            ):
                _fail(f"{where}: invalid wheel-image provenance")
            for key in ("telegram_wheel_sha256", "core_wheel_sha256", "core_lock_sha256", "build_receipt_sha256"):
                _sha(provenance[key], f"{where}.provenance.{key}")
        elif kind == "legacy-image":
            _object(provenance, {
                "kind", "legacy_manifest_sha256", "wheel_identity", "lock_identity",
            }, f"{where}.provenance")
            _sha(provenance["legacy_manifest_sha256"], f"{where}.provenance.legacy_manifest_sha256")
            if provenance["wheel_identity"] != "unavailable" or provenance["lock_identity"] != "unavailable":
                _fail(f"{where}: legacy wheel and lock evidence must be unavailable")
        else:
            _fail(f"{where}: unsupported provenance")
        source = _identity(component["source"], f"{where}.source", claim_required=kind != "wheel-image")
        if kind == "completed-artifact":
            if source["verified_commit"] is None:
                _fail(f"{where}: completed artifact requires a verified source")
        core = _object(component["core"], {
            "version", "revision_claim", "verified_commit", "verified_tree",
        }, f"{where}.core")
        _matches(core["version"], _VERSION, f"{where}.core.version")
        _identity({key: core[key] for key in ("revision_claim", "verified_commit", "verified_tree")},
                  f"{where}.core", claim_required=True)
        image = _object(component["image"], {"ref", "id", "digest", "revision"}, f"{where}.image")
        _image_ref(image["ref"], f"{where}.image.ref")
        _image_id(image["id"], f"{where}.image.id")
        digest = _text(image["digest"], f"{where}.image.digest")
        if digest.startswith("sha256:"):
            _image_id(digest, f"{where}.image.digest")
        else:
            _image_ref(digest, f"{where}.image.digest")
            if "@sha256:" not in digest:
                _fail(f"{where}.image.digest: immutable digest required")
        if image["revision"] != source["revision_claim"]:
            _fail(f"{where}: image revision and source claim differ")
        if kind == "completed-artifact" and (image["id"] != provenance["native_image_id"]
                                             or image["digest"] != provenance["native_image_id"]):
            # A nameless acquisition has no registry digest; the store's
            # native ID is the only immutable reference.
            _fail(f"{where}: image differs from the registered native image")
        if kind == "source-image" or (kind == "completed-artifact" and name == "api"):
            if source["verified_commit"] is None or source["verified_commit"] != core["verified_commit"] \
                    or source["verified_tree"] != core["verified_tree"] \
                    or component["version"] != core["version"]:
                _fail(f"{where}: source image and installed Core binding differ")
        if kind == "legacy-image" and core["revision_claim"] != source["revision_claim"]:
            _fail(f"{where}: legacy Core and OCI revision differ")
        recipe = _object(component["recipe"], set(_RECIPE_FIELDS) | {"sha256", "snapshot_path"}, f"{where}.recipe")
        _recipe({key: recipe[key] for key in _RECIPE_FIELDS}, f"{where}.recipe")
        digest = _sha(recipe["sha256"], f"{where}.recipe.sha256")
        if recipe["snapshot_path"] != f".deployments/product-recipes/{digest}.json":
            _fail(f"{where}.recipe.snapshot_path: not content addressed")
    database = _object(manifest["database"], {"path", "sha256", "schema_version", "provenance"}, "manifest.database")
    _path(database["path"], "manifest.database.path")
    _sha(database["sha256"], "manifest.database.sha256")
    if type(database["schema_version"]) is not int or database["schema_version"] != 2:
        _fail("manifest.database: unsupported schema version")
    dbp = _object(database["provenance"], DB_PROVENANCE_KEYS, "manifest.database.provenance")
    for key, item in dbp.items():
        _text(item, f"manifest.database.provenance.{key}")
    if dbp["schema_version"] != "2":
        _fail("manifest.database.provenance: unsupported schema version")
    _git(dbp["source_commit"], "manifest.database.provenance.source_commit")
    if dbp["source_commit"] != dbp["wutheringdata_commit"]:
        _fail("manifest.database.provenance: source commit mismatch")
    url = urlsplit(dbp["source_repo_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        _fail("manifest.database.provenance: invalid source URL")
    identity = manifest["api_identity"]
    if identity is not None:
        if components["api"] is None:
            _fail("manifest.api_identity: API is absent")
        _object(identity, {"path", "device", "inode"}, "manifest.api_identity")
        _path(identity["path"], "manifest.api_identity.path")
        _number(identity["device"], "manifest.api_identity.device")
        _number(identity["inode"], "manifest.api_identity.inode", 1)
    return manifest


def new_deployment_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(16)


def _decode(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("duplicate JSON object key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        _fail("non-finite JSON number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError("invalid JSON") from exc


def _encode(value: Any) -> bytes:
    try:
        data = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestError("cannot encode record") from exc
    if len(data) > _LIMIT:
        _fail("record exceeds size limit")
    return data


@contextmanager
def _directory(root: Path, parts: tuple[str, ...], *, create: bool) -> Iterator[int]:
    """Walk every component without following symlinks, including root ancestors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    absolute = Path(os.path.abspath(os.fspath(root)))
    fds: list[int] = []
    try:
        fd = os.open("/", flags)
        fds.append(fd)
        root_parts = absolute.parts[1:]
        for index, name in enumerate((*root_parts, *parts)):
            if name in ("", ".", "..") or "/" in name:
                _fail("invalid directory component")
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create or index < len(root_parts):
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(name, flags, dir_fd=fd)
            fds.append(child)
            fd = child
        yield fd
    except OSError as exc:
        raise ManifestError(f"cannot access deployment metadata directory: {exc.strerror}") from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read_file(directory: int, name: str, *, limit: int = _LIMIT) -> bytes:
    if type(limit) is not int or not 0 <= limit <= 16 * 1024 * 1024:
        _fail("immutable record read limit is invalid")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_nlink != 1:
                _fail("immutable record is not a singly-linked read-only regular file")
            if info.st_size > limit:
                _fail("immutable record exceeds size limit")
            chunks = []
            size = 0
            while chunk := os.read(fd, min(65536, limit + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    _fail("immutable record exceeds size limit")
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if _db_stat(info) != _db_stat(after) or _db_stat(after) != _db_stat(named):
                _fail("immutable record changed while being read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ManifestError(f"cannot read immutable record: {exc.strerror}") from exc


def _write_file(directory: int, name: str, raw: bytes, *, reuse: bool) -> None:
    # Archives may contain the full journal rather than a 4 MiB manifest.
    _write_file_unnamed(directory, name, raw, reuse=reuse, limit=16 * 1024 * 1024)


def _publish_unnamed(directory: int, name: str, raw: bytes, *, mode: int,
                     limit: int | None = None) -> None:
    """Publish one fully synced file without a named temporary or replacement."""
    maximum = (16 * 1024 * 1024 if mode == 0o600 else _LIMIT) if limit is None else limit
    if (type(name) is not str or name in ("", ".", "..") or "/" in name or "\x00" in name
            or type(raw) is not bytes or type(mode) is not int or mode not in (0o400, 0o600)
            or type(maximum) is not int or not 0 <= maximum <= 16 * 1024 * 1024 or len(raw) > maximum):
        _fail("invalid unnamed publication request")
    if not hasattr(os, "O_TMPFILE"):
        _fail("unnamed publication is unavailable")
    fd = None
    try:
        if not stat.S_ISDIR(os.fstat(directory).st_mode):
            _fail("unnamed publication directory is invalid")
        fd = os.open(".", os.O_TMPFILE | os.O_WRONLY | os.O_CLOEXEC, 0o600, dir_fd=directory)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                _fail("unnamed publication write did not advance")
            offset += written
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=directory, follow_symlinks=True)
        os.fsync(directory)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ManifestError(f"cannot publish unnamed record: {exc.strerror}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _write_file_unnamed(directory: int, name: str, raw: bytes, *, reuse: bool, limit: int = _LIMIT) -> None:
    """Publish or exactly reuse a singly-linked immutable record."""
    try:
        _publish_unnamed(directory, name, raw, mode=0o400, limit=limit)
    except FileExistsError:
        if not reuse or _read_file(directory, name, limit=limit) != raw:
            _fail("immutable record already exists")
        try:
            os.fsync(directory)
        except OSError as exc:
            raise ManifestError(f"cannot sync reused immutable record: {exc.strerror}") from exc


def _db_stat(info: os.stat_result) -> dict[str, int]:
    return {
        "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
        "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
    }


def _reject_sidecars(directory: int, filename: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.stat(filename + suffix, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _fail("database sidecar exists")


def read_database_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    """Read the actual schema-2 SQLite DB without writes and bind its stat.

    The caller must compare the returned stat as well as the hash throughout
    the transaction. SQLite sidecars, symlinks, schema drift and concurrent
    replacement or modification are refused.
    """
    absolute = Path(os.path.abspath(os.fspath(path)))
    with _directory(absolute.parent, (), create=False) as directory:
        try:
            _reject_sidecars(directory, absolute.name)
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    _fail("database is not a regular file")
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                uri = f"file:/proc/self/fd/{fd}?mode=ro&immutable=1"
                with closing(sqlite3.connect(uri, uri=True)) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchall()
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                    if integrity != [("ok",)] or tables != [("metadata",), ("terms",)]:
                        _fail("database integrity or table set differs from schema 2")
                    for table, expected in _DB_SCHEMA.items():
                        columns = tuple(
                            (row[1], row[2], row[3], row[5])
                            for row in connection.execute(f"PRAGMA table_info({table})")
                        )
                        indexes = {
                            row[1]: (row[2], row[3])
                            for row in connection.execute(f"PRAGMA index_list({table})")
                        }
                        if columns != expected or indexes != _DB_INDEXES[table]:
                            _fail("database structure differs from schema 2")
                    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in DB_PROVENANCE_KEYS):
                    _fail("database provenance is incomplete")
                provenance = {key: metadata[key] for key in DB_PROVENANCE_KEYS}
                if provenance["schema_version"] != "2":
                    _fail("database provenance schema is not 2")
                _git(provenance["source_commit"], "database source commit")
                if provenance["source_commit"] != provenance["wutheringdata_commit"]:
                    _fail("database provenance source commits differ")
                after = os.fstat(fd)
                named = os.stat(absolute.name, dir_fd=directory, follow_symlinks=False)
                if _db_stat(before) != _db_stat(after) or _db_stat(after) != _db_stat(named):
                    _fail("database changed while being inspected")
                _reject_sidecars(directory, absolute.name)
            finally:
                os.close(fd)
        except (OSError, sqlite3.Error) as exc:
            raise ManifestError("cannot inspect immutable database") from exc
    database = {
        "path": str(absolute), "sha256": digest.hexdigest(),
        "schema_version": 2, "provenance": provenance,
    }
    return database, _db_stat(after)


def verify_database_binding(database: dict[str, Any]) -> dict[str, int]:
    """Compare the manifest DB fields with the actual schema and provenance."""
    _object(database, {"path", "sha256", "schema_version", "provenance"}, "database")
    _path(database["path"], "database.path")
    actual, db_stat = read_database_snapshot(Path(database["path"]))
    if actual != database:
        _fail("database differs from manifest")
    return db_stat


def write_recipe_snapshot(root: Path, snapshot: dict[str, Any]) -> tuple[str, str]:
    raw = _encode(validate_recipe_snapshot(snapshot))
    digest = hashlib.sha256(raw).hexdigest()
    with _directory(root, (".deployments", "product-recipes"), create=True) as directory:
        _write_file(directory, f"{digest}.json", raw, reuse=True)
    return f".deployments/product-recipes/{digest}.json", digest


def read_recipe_snapshot(root: Path, digest: str) -> dict[str, Any]:
    _sha(digest, "recipe digest")
    with _directory(root, (".deployments", "product-recipes"), create=False) as directory:
        raw = _read_file(directory, f"{digest}.json")
    if hashlib.sha256(raw).hexdigest() != digest:
        _fail("recipe snapshot digest mismatch")
    return validate_recipe_snapshot(_decode(raw))


def _verify_snapshots(root: Path, manifest: dict[str, Any]) -> None:
    for component in manifest["components"].values():
        if component is None:
            continue
        recipe = component["recipe"]
        snapshot = read_recipe_snapshot(root, recipe["sha256"])
        if snapshot["image_id"] != component["image"]["id"] or any(
            snapshot[key] != recipe[key] for key in _RECIPE_FIELDS
        ):
            _fail("recipe snapshot does not match installed component")


def _artifact_module() -> Any:
    import component_artifact  # noqa: PLC0415
    if Path(component_artifact.__file__).resolve() != Path(__file__).resolve().parent / "component_artifact.py":
        _fail("artifact verifier is not this source's module")
    return component_artifact


def read_artifact_records(root: Path, bundle_sha256: str, acquisition_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one registered acquisition receipt and its bundle copy, without writing.

    The receipt is located by content digest among the root's immutable
    registered receipts; no record is created and no lock file is opened.
    """
    _sha(bundle_sha256, "bundle digest")
    _sha(acquisition_sha256, "acquisition digest")
    artifact = _artifact_module()
    store = Path(root) / ".deployments" / "artifacts"
    try:
        for part in (Path(root) / ".deployments", store, store / "acquisitions", store / "bundles"):
            if not stat.S_ISDIR(os.lstat(part).st_mode):
                _fail("artifact store path is not a directory")
        names = sorted(os.listdir(store / "acquisitions"))
    except OSError as exc:
        raise ManifestError("registered artifact store is unavailable") from exc
    found = []
    try:
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = artifact._read_regular(store / "acquisitions" / name, _LIMIT)
            if hashlib.sha256(raw).hexdigest() == acquisition_sha256:
                found.append((name, raw))
        if len(found) != 1:
            _fail("registered acquisition receipt is missing or duplicated")
        name, raw = found[0]
        receipt = artifact.validate_receipt(artifact.decode_canonical(raw))
        if name != receipt["acquisition_id"] + ".json" or receipt["bundle_sha256"] != bundle_sha256:
            _fail("acquisition receipt differs from its manifest binding")
        bundle_raw = artifact._read_regular(store / "bundles" / (bundle_sha256 + ".json"), _LIMIT)
        if hashlib.sha256(bundle_raw).hexdigest() != bundle_sha256:
            _fail("registered bundle copy differs")
        bundle = artifact.validate_bundle_schema(artifact.decode_canonical(bundle_raw))
        if bundle["attempt_id"] != receipt["attempt_id"]:
            _fail("acquisition receipt and bundle attempts differ")
        if receipt["native_image_id"] != artifact.native_image_candidate(
                bundle["oci"], receipt["store"]["containerd_snapshotter"]):
            _fail("acquisition receipt image differs from the sealed bundle")
    except artifact.ArtifactError as exc:
        raise ManifestError("registered artifact records cannot be verified") from exc
    return receipt, bundle


def verify_artifact_binding(name: str, component: dict[str, Any], receipt: dict[str, Any],
                            bundle: dict[str, Any]) -> None:
    """Require a schema-3 component to state exactly its registered bundle's facts."""
    provenance = component["provenance"]
    where = f"manifest.components.{name}"
    if (bundle["product"] != ARTIFACT_PRODUCTS[name] or bundle["repository"] != component["repository"]
            or bundle["version"] != component["version"]
            or bundle["build_result"]["authority"] != provenance["authority"]
            or receipt["native_image_id"] != provenance["native_image_id"]):
        _fail(f"{where}: completed-artifact facts differ from the registered bundle")
    source = component["source"]
    if (source["revision_claim"] != bundle["source"]["commit"]
            or source["verified_commit"] != bundle["source"]["commit"]
            or source["verified_tree"] != bundle["source"]["tree"]):
        _fail(f"{where}: completed-artifact source differs from the registered bundle")
    core = component["core"]
    binding = bundle["core_binding"]
    if core["version"] != binding["core_version"] or core["revision_claim"] != binding["core_source_commit"]:
        _fail(f"{where}: completed-artifact Core binding differs from the registered bundle")
    if name == "api":
        if core["verified_commit"] != bundle["source"]["commit"] or core["verified_tree"] != bundle["source"]["tree"]:
            _fail(f"{where}: Core bundle must bind its own source as the Core")
    elif core["verified_commit"] is not None and core["verified_commit"] != binding["core_source_commit"]:
        _fail(f"{where}: Telegram Core binding differs from the registered bundle")


def _verify_artifacts(root: Path, manifest: dict[str, Any]) -> None:
    for name, component in manifest["components"].items():
        if component is None or component["provenance"]["kind"] != "completed-artifact":
            continue
        provenance = component["provenance"]
        receipt, bundle = read_artifact_records(root, provenance["bundle_sha256"], provenance["acquisition_sha256"])
        verify_artifact_binding(name, component, receipt, bundle)


def write_manifest(root: Path, payload: dict[str, Any]) -> Path:
    raw = _encode(validate_manifest(payload))
    manifest = validate_manifest(_decode(raw))
    _verify_snapshots(root, manifest)
    _verify_artifacts(root, manifest)
    verify_database_binding(manifest["database"])
    name = f"{manifest['deployment_id']}.json"
    with _directory(root, (".deployments", "products"), create=True) as directory:
        _write_file(directory, name, raw, reuse=False)
    return Path(root) / ".deployments" / "products" / name


def read_manifest(root: Path, deployment_id: str) -> dict[str, Any]:
    _matches(deployment_id, _ID, "deployment ID")
    with _directory(root, (".deployments", "products"), create=False) as directory:
        raw = _read_file(directory, f"{deployment_id}.json")
    manifest = validate_manifest(_decode(raw))
    if manifest["deployment_id"] != deployment_id:
        _fail("manifest ID and filename differ")
    _verify_snapshots(root, manifest)
    _verify_artifacts(root, manifest)
    return manifest


def manifest_sha256(root: Path, deployment_id: str) -> str:
    _matches(deployment_id, _ID, "deployment ID")
    with _directory(root, (".deployments", "products"), create=False) as directory:
        raw = _read_file(directory, f"{deployment_id}.json")
    manifest = validate_manifest(_decode(raw))
    if manifest["deployment_id"] != deployment_id:
        _fail("manifest ID and filename differ")
    _verify_snapshots(root, manifest)
    _verify_artifacts(root, manifest)
    return hashlib.sha256(raw).hexdigest()
