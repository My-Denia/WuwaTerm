from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts import component_state_endpoint as endpoint


def bind(container: Path, host: str, *, read_only: bool = False) -> dict:
    info = container.stat()
    return {"container": str(container), "host": host, "read_only": read_only,
            "device": info.st_dev, "inode": info.st_ino}


def request(root: Path, target: Path, *, kind: str = "chat-settings", read_only: bool = False,
            require_writer: bool = True, mounts: list[dict] | None = None) -> dict:
    return {"kind": kind, "recipe_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64,
            "image_revision": "3" * 40, "user": f"{os.geteuid()}:{os.getegid()}",
            "working_dir": str(root), "configured_path": target.name, "effective_path": str(target),
            "mounts": mounts if mounts is not None else [bind(root, "/saved/host-state", read_only=read_only)],
            "require_writer": require_writer}


def run_probe(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-B", "-c", endpoint.ENDPOINT_CODE,
                           json.dumps(payload, ensure_ascii=True, sort_keys=True)],
                          env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)


def captured(payload: dict) -> dict:
    result = run_probe(payload)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert endpoint.validate_endpoint(value) is value
    return value


def refused(payload: dict) -> None:
    result = run_probe(payload)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip() == "state endpoint refused"


def test_regular_and_absent_settings_bind_actual_parent_lock_and_uid(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    state = root / "settings.json"
    secret = b"actual-state-never-print-3e9c"
    state.write_bytes(secret)
    state.chmod(0o600)
    payload = request(root, state)
    raw = run_probe(payload)
    assert raw.returncode == 0, raw.stderr
    assert secret.decode() not in raw.stdout + raw.stderr
    result = endpoint.validate_endpoint(json.loads(raw.stdout))
    assert result["entry"]["type"] == "regular" and result["links"] == []
    assert result["entry"]["host_path"] == "/saved/host-state/settings.json"
    assert result["referent"]["sha256"] == hashlib.sha256(secret).hexdigest()
    assert result["writer"]["role"] == "referent" and result["writer"]["can_replace"] is True
    assert result["writer"]["settings_lock_binding"]["type"] == "absent"
    assert stat.S_IMODE(result["referent"]["stat"]["mode"]) == 0o600
    assert result["entry"]["parent_identity"]["inode"] == root.stat().st_ino
    missing = captured(request(root, root / "absent.json"))
    assert missing["entry"]["type"] == "absent" and missing["links"] == []
    assert missing["referent"]["present"] is False and missing["referent"]["stat"] is None


@pytest.mark.parametrize("kind,role", [("chat-settings", "referent"), ("channel-replies", "entry")])
def test_leaf_symlink_resolves_inside_namespace_but_writer_depends_on_kind(
    tmp_path: Path, kind: str, role: str,
) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    (root / "nested").mkdir()
    target = root / "nested/state.json"
    target.write_bytes(b"state-for-hash-only")
    link = root / "current.json"
    link.symlink_to("nested/state.json")
    result = captured(request(root, link, kind=kind))
    assert result["entry"]["type"] == "symlink"
    assert base64.b64decode(result["entry"]["link_bytes_b64"]) == b"nested/state.json"
    assert len(result["links"]) == 1 and result["links"][0]["container_path"] == str(link)
    assert result["referent"]["container_path"] == str(target)
    assert result["referent"]["host_path"] == "/saved/host-state/nested/state.json"
    assert result["writer"]["role"] == role
    assert result["writer"]["container_path"] == str(target if role == "referent" else link)
    assert (result["writer"]["settings_lock_binding"] is None) == (role == "entry")


def test_relative_absolute_multi_link_and_raw_non_utf8_link_bytes(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    (root / "nested").mkdir()
    target = root / "nested/value.json"
    target.write_bytes(b"same payload")
    (root / "first").symlink_to("nested/second")
    (root / "nested/second").symlink_to(str(target))
    result = captured(request(root, root / "first", kind="channel-replies"))
    assert [item["container_path"] for item in result["links"]] == [str(root / "first"), str(root / "nested/second")]
    assert base64.b64decode(result["links"][1]["link_bytes_b64"]) == os.fsencode(target)
    assert result["referent"]["container_path"] == str(target)
    raw_name = b"raw-\xff"
    fd = os.open(os.fsencode(root / "nested") + b"/" + raw_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"raw-name-file")
    finally:
        os.close(fd)
    os.symlink(b"nested/" + raw_name, os.fsencode(root / "raw-link"))
    raw = captured(request(root, root / "raw-link", kind="channel-replies"))
    assert base64.b64decode(raw["entry"]["link_bytes_b64"]) == b"nested/" + raw_name
    assert os.fsencode(raw["referent"]["container_path"]).endswith(b"/nested/" + raw_name)


def test_dangling_symlink_differs_from_absence_and_uses_correct_writer(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    link = root / "link"
    link.symlink_to("missing")
    settings = captured(request(root, link))
    replies = captured(request(root, link, kind="channel-replies"))
    assert settings["entry"]["type"] == replies["entry"]["type"] == "symlink"
    assert settings["referent"]["present"] is replies["referent"]["present"] is False
    assert settings["referent"]["container_path"] == str(root / "missing")
    assert settings["writer"]["container_path"] == str(root / "missing")
    assert replies["writer"]["container_path"] == str(link)
    assert settings["writer"]["can_replace"] and replies["writer"]["can_replace"]


def test_read_only_source_is_recorded_but_not_a_writable_writer(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    target = root / "source"
    target.write_bytes(b"x")
    payload = request(root, target, read_only=True, require_writer=False)
    result = captured(payload)
    assert result["referent"]["present"] and result["writer"]["can_replace"] is False
    payload["require_writer"] = True
    refused(payload)


def test_only_explicit_read_only_extra_can_overlap_a_saved_host_bind(tmp_path: Path) -> None:
    outer, alias = tmp_path / "outer", tmp_path / "alias"
    outer.mkdir()
    alias.mkdir()
    target = outer / "state"
    target.write_bytes(b"state")
    payload = request(outer, target, mounts=[bind(outer, "/saved/root"),
                                            bind(alias, "/saved/root/nested", read_only=True)])
    refused(payload)
    library = endpoint.ENDPOINT_CODE[:endpoint.ENDPOINT_CODE.index("\ntry:\n if len(sys.argv)!=2: fail()")]
    code = library + "\nreq=json.loads(sys.argv[1]); parse(req,read_alias=req['mounts'][1]); print('accepted')\n"
    result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", code, json.dumps(payload)],
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and result.stdout == "accepted\n"
    payload["mounts"][1]["read_only"] = False
    denied = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", code, json.dumps(payload)],
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                            capture_output=True, text=True, check=False)
    assert denied.returncode != 0 and denied.stdout == ""


def test_link_limit_loop_and_different_inodes_with_equal_contents(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    for index in range(41):
        (root / f"link-{index}").symlink_to(f"link-{index + 1}" if index < 40 else "target")
    (root / "target").write_bytes(b"same")
    result = captured(request(root, root / "link-1", kind="channel-replies"))
    assert len(result["links"]) == 40
    refused(request(root, root / "link-0", kind="channel-replies"))
    (root / "loop-a").symlink_to("loop-b")
    (root / "loop-b").symlink_to("loop-a")
    refused(request(root, root / "loop-a", kind="channel-replies"))
    (root / "other").write_bytes(b"same")
    other = captured(request(root, root / "other", kind="channel-replies"))
    assert result["referent"]["sha256"] == other["referent"]["sha256"]
    assert result["referent"]["stat"]["inode"] != other["referent"]["stat"]["inode"]


def test_same_entry_replaced_with_equal_contents_fails_endpoint_comparison(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    target = root / "state"
    target.write_bytes(b"equal contents")
    before = captured(request(root, target, kind="channel-replies"))
    replacement = root / "replacement"
    replacement.write_bytes(b"equal contents")
    os.replace(replacement, target)
    after = captured(request(root, target, kind="channel-replies"))
    assert before["referent"]["sha256"] == after["referent"]["sha256"]
    assert before["referent"]["stat"]["inode"] != after["referent"]["stat"]["inode"]
    with pytest.raises(endpoint.EndpointError, match="endpoint changed"):
        endpoint.require_unchanged(before, after)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read directories regardless of mode")
def test_writer_parent_requires_native_read_write_search_permissions(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    target = root / "state"
    target.write_bytes(b"state")
    root.chmod(0o300)
    try:
        refused(request(root, target))
    finally:
        root.chmod(0o700)


def test_directory_symlink_and_settings_lock_symlink_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    (root / "real").mkdir()
    (root / "real/value").write_bytes(b"x")
    (root / "alias").symlink_to("real", target_is_directory=True)
    refused(request(root, root / "alias/value"))
    (root / "settings").write_bytes(b"x")
    (root / "settings.lock").symlink_to("real/value")
    refused(request(root, root / "settings"))
    (root / "target").write_bytes(b"reachable")
    (root / "hidden-dir-symlink").symlink_to("alias/../target")
    assert (root / "hidden-dir-symlink").read_bytes() == b"reachable"
    refused(request(root, root / "hidden-dir-symlink"))
    (root / "physical-dir-traversal").symlink_to("real/../target")
    assert captured(request(root, root / "physical-dir-traversal"))["referent"]["container_path"] == str(root / "target")


@pytest.mark.parametrize("shadow", ["bind", "tmpfs"])
def test_cross_mount_nested_shadow_is_refused_but_direct_nested_bind_works(tmp_path: Path, shadow: str) -> None:
    root = tmp_path / "namespace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "value").write_bytes(b"x")
    (root / "cross").symlink_to("nested/value")
    extra = bind(nested, "/saved/nested-state") if shadow == "bind" else {
        "type": "tmpfs", "container": str(nested), "options": ["rw"],
    }
    mounts = [bind(root, "/saved/outer-state"), extra]
    refused(request(root, root / "cross", mounts=mounts))
    if shadow == "bind":
        direct = captured(request(root, nested / "value", mounts=mounts))
        assert direct["entry"]["host_path"] == "/saved/nested-state/value"
    else:
        refused(request(root, nested / "value", mounts=mounts))


def test_cross_bind_alias_wrong_identity_and_large_file_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    target = root / "state"
    target.write_bytes(b"x")
    target_link = root / "outside"
    target_link.symlink_to(str(other / "missing"))
    mounts = [bind(root, "/saved/outer"), bind(other, "/saved/other")]
    refused(request(root, target_link, mounts=mounts))
    alias = [bind(root, "/saved/shared"), bind(other, "/saved/shared/nested")]
    refused(request(root, target, mounts=alias))
    broken = request(root, target)
    broken["mounts"][0]["inode"] += 1
    refused(broken)
    wrong_user = request(root, target)
    wrong_user["user"] = f"{os.geteuid() + 1}:{os.getegid()}"
    refused(wrong_user)
    large = root / "large"
    with large.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    refused(request(root, large))


def test_host_entry_mapper_allows_only_leaf_link_and_rejects_parent_alias(tmp_path: Path) -> None:
    host = tmp_path / "host"
    host.mkdir()
    (host / "real").mkdir()
    (host / "real/value").write_bytes(b"x")
    (host / "leaf").symlink_to("real/value")
    snapshot = {"mounts": [{"container": "/app/state", "host": str(host), "read_only": False}]}
    assert endpoint.map_entry(snapshot, "/app/state/leaf", writable=True) == str(host / "leaf")
    assert endpoint.map_entry(snapshot, "/app/state/new", writable=True) == str(host / "new")
    (host / "directory-alias").symlink_to("real", target_is_directory=True)
    with pytest.raises(endpoint.EndpointError):
        endpoint.map_entry(snapshot, "/app/state/directory-alias/value", writable=True)
    snapshot["mounts"][0]["read_only"] = True
    assert endpoint.map_entry(snapshot, "/app/state/leaf", writable=False) == str(host / "leaf")
    with pytest.raises(endpoint.EndpointError):
        endpoint.map_entry(snapshot, "/app/state/leaf", writable=True)


def test_unrelated_regular_file_bind_does_not_hide_a_directory_state_bind(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    state = root / "state"
    state.mkdir()
    target = state / "settings"
    target.write_bytes(b"state")
    database = root / "terms.db"
    database.write_bytes(b"fixture database")
    payload = request(state, target, mounts=[bind(state, str(state)), bind(database, str(database), read_only=True)])
    assert captured(payload)["referent"]["sha256"] == hashlib.sha256(b"state").hexdigest()
    snapshot = {"mounts": [{"container": str(state), "host": str(state), "read_only": False},
                           {"container": str(database), "host": str(database), "read_only": True}]}
    assert endpoint.map_entry(snapshot, str(target), writable=True) == str(target)
    with pytest.raises(endpoint.EndpointError):
        endpoint.map_entry(snapshot, str(database), writable=True)


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_terminal_route_allows_native_progress_but_rejects_link_redirection(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    referent = root / "referent"
    referent.write_bytes(b"original")
    entry = root / "entry"
    entry.symlink_to("referent")
    payload = request(root, entry, kind=kind)
    payload["mounts"][0]["host"] = str(root)
    recorded = captured(payload)
    snapshot = {"mounts": [{"container": str(root), "host": str(root), "read_only": False}]}
    endpoint.verify_terminal_route(snapshot, recorded)
    replacement = root / "replacement"
    replacement.write_bytes(b"progressed")
    if kind == "chat-settings":
        os.replace(replacement, referent)
        endpoint.verify_terminal_route(snapshot, recorded)
        assert os.readlink(entry) == "referent"
        entry.unlink()
        entry.symlink_to("other")
        with pytest.raises(endpoint.EndpointError, match="redirected"):
            endpoint.verify_terminal_route(snapshot, recorded)
    else:
        os.replace(replacement, entry)
        endpoint.verify_terminal_route(snapshot, recorded)
        assert referent.read_bytes() == b"original"


def test_terminal_route_refuses_a_removed_preexisting_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    target = root / "settings"
    target.write_bytes(b"state")
    payload = request(root, target)
    payload["mounts"][0]["host"] = str(root)
    recorded = captured(payload)
    snapshot = {"mounts": [{"container": str(root), "host": str(root), "read_only": False}]}
    target.unlink()
    with pytest.raises(endpoint.EndpointError, match="disappeared"):
        endpoint.verify_terminal_route(snapshot, recorded)


def test_endpoint_validator_refuses_tampering_and_role_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "namespace"
    root.mkdir()
    (root / "target").write_bytes(b"x")
    (root / "link").symlink_to("target")
    valid = captured(request(root, root / "link", kind="channel-replies"))
    changes = [
        lambda item: item["entry"].update(link_bytes_b64="invalid!"),
        lambda item: item["links"][0]["lstat"].update(inode=0),
        lambda item: item["referent"].update(host_path="/wrong/path"),
        lambda item: item["writer"].update(role="referent"),
        lambda item: item["writer"]["mount_identity"].update(read_only=True),
        lambda item: item.update(links=item["links"] * 41),
        lambda item: item.update(unexpected=True),
    ]
    for change in changes:
        altered = copy.deepcopy(valid)
        change(altered)
        with pytest.raises(endpoint.EndpointError):
            endpoint.validate_endpoint(altered)
