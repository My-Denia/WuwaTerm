#!/usr/bin/env python3
"""Map persisted Telegram state entries and their leaf links to host paths."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

import component_observe as observe
import component_state_endpoint as endpoint_module
import product_manifest as manifest


class StateError(RuntimeError):
    """A safe and attributable state observation could not be established."""


def _fail(message: str) -> None:
    raise StateError(message)


def _entry_mount(snapshot: dict[str, Any], container_path: str) -> dict[str, Any]:
    path = PurePosixPath(container_path)
    candidates = [mount for mount in snapshot["mounts"] if "host" in mount
                  and (path == PurePosixPath(mount["container"])
                       or path.is_relative_to(PurePosixPath(mount["container"])))]
    if not candidates:
        _fail("state entry has no visible bind")
    return max(candidates, key=lambda item: len(PurePosixPath(item["container"]).parts))


def entry_path(snapshot: dict[str, Any], container_path: str, *, writable: bool) -> str:
    """Map one state entry through its parent without following its leaf.

    Schema 5 admits the two native leaf forms: a settings entry may be a link
    to the file the product rewrites, and a reply entry may still be the link
    the product replaces in place.  Only the parent has to be a physical
    directory inside the entry's own persistent bind.
    """
    try:
        mount = _entry_mount(snapshot, container_path)
        path = PurePosixPath(manifest._path(container_path, "state entry path"))
        relative = path.relative_to(PurePosixPath(mount["container"]))
        if not relative.parts:
            _fail("state entry is a bind root")
        if writable and mount["read_only"]:
            _fail("state entry is not on a writable persistent bind")
        source = Path(manifest._path(mount["host"], "state entry bind"))
        host = source.joinpath(*relative.parts)
        parent = PurePosixPath(mount["container"]).joinpath(*relative.parts[:-1])
        if observe.resolve_bind_path(snapshot, str(parent), writable=writable) != str(host.parent):
            # resolve_bind_path rejects a symlinked component and tolerates a
            # missing final path, which is exactly the entry-parent rule here.
            _fail("state entry parent does not map to its own binding")
        return str(host)
    except (manifest.ManifestError, observe.ObservationError) as exc:
        raise StateError("state entry cannot be mapped through its parent") from exc


def _entry_container(snapshot: dict[str, Any], host_path: str, *, writable: bool) -> str:
    """Map a state entry host path back to the container path it was planned from."""
    host = PurePosixPath(manifest._path(host_path, "state entry host path"))
    choices = []
    for mount in snapshot["mounts"]:
        if "host" not in mount:
            continue
        source = PurePosixPath(mount["host"])
        if host == source or host.is_relative_to(source):
            candidate = str(PurePosixPath(mount["container"]) / host.relative_to(source))
            try:
                if entry_path(snapshot, candidate, writable=writable) == host_path:
                    choices.append(candidate)
            except StateError:
                continue
    if len(set(choices)) != 1:
        _fail("state entry has no unique visible bind")
    return choices[0]


def _leaf_container_path(snapshot: dict[str, Any], container_path: str) -> str:
    """Follow only leaf links of one state entry, inside the entry's own bind."""
    mount = _entry_mount(snapshot, container_path)
    current = container_path
    for _ in range(41):
        # The leaf itself may be the link under inspection, so only its parent
        # is mapped strictly.
        host = entry_path(snapshot, current, writable=False)
        try:
            info = Path(host).lstat()
        except FileNotFoundError:
            return current
        if not stat.S_ISLNK(info.st_mode):
            return current
        if not 0 < info.st_size <= 4095:
            _fail("state entry link is unsafe")
        try:
            current = endpoint_module._next_link(current, os.readlink(host), {"container": mount["container"]})
        except endpoint_module.EndpointError as exc:
            raise StateError("state entry link leaves its binding") from exc
    _fail("state entry link chain is too long")
    raise StateError("state entry link chain is too long")


def leaf_host_path(snapshot: dict[str, Any], host_path: str) -> str:
    """Follow a state entry's leaf links inside the bind that holds it.

    An entry may name a relative referent or a container-absolute one, so the
    chain is walked in the container namespace the product itself would use and
    mapped back through the same persistent bind.
    """
    try:
        entry = _entry_container(snapshot, host_path, writable=False)
        read = _leaf_container_path(snapshot, entry)
        return host_path if read == entry else entry_path(snapshot, read, writable=False)
    except (manifest.ManifestError, observe.ObservationError) as exc:
        raise StateError("state entry link cannot be followed") from exc
