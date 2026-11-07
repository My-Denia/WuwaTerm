#!/usr/bin/env python3
"""FULL4 state entry, symlink chain, referent and writer bindings.

``ENDPOINT_CODE`` runs inside the recorded image and resolves links only in
that container's namespace. The host-side entry mapper never follows a leaf.
This module does not change the runtime/database bind resolver.
"""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


class EndpointError(ValueError):
    """An endpoint or its bind cannot be safely established."""


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_USER = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
_MAX_STATE = 16 * 1024 * 1024
_MAX_LINK = 4095
_PROTECTED = tuple(PurePosixPath(item) for item in (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/app/.venv",
))
_STAT_KEYS = {"device", "inode", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "nlink"}
_PARENT_KEYS = {"device", "inode", "mode", "uid", "gid"}
_MOUNT_KEYS = {"container", "host", "read_only", "device", "inode"}
_ENTRY_KEYS = {"container_path", "host_path", "mount_identity", "parent_identity", "type", "lstat", "link_bytes_b64"}
_LINK_KEYS = _ENTRY_KEYS - {"type"}
_REFERENT_KEYS = {"container_path", "host_path", "mount_identity", "parent_identity", "present", "stat", "sha256"}
_WRITER_KEYS = {"role", "container_path", "host_path", "mount_identity", "parent_identity",
                "settings_lock_binding", "can_replace"}


def _fail(message: str) -> None:
    raise EndpointError(message)


def _object(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail(f"{where}: unexpected fields")
    return value


def _integer(value: Any, where: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{where}: invalid integer")
    return value


def _text(value: Any, where: str) -> str:
    if (type(value) is not str or not value or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail(f"{where}: invalid text")
    return value


def _absolute(value: Any, where: str) -> str:
    path = _text(value, where)
    if path != "/" and (not path.startswith("/") or "\\" in path
                        or any(part in ("", ".", "..") for part in path.split("/")[1:])):
        _fail(f"{where}: path is not normalized and absolute")
    return path


def _match(value: Any, pattern: re.Pattern[str], where: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(f"{where}: invalid identity")
    return value


def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _mount(value: Any, where: str) -> dict[str, Any]:
    item = _object(value, _MOUNT_KEYS, where)
    container = _absolute(item["container"], f"{where}.container")
    _absolute(item["host"], f"{where}.host")
    if container == "/" or type(item["read_only"]) is not bool:
        _fail(f"{where}: invalid directory bind")
    _integer(item["device"], f"{where}.device")
    _integer(item["inode"], f"{where}.inode", minimum=1)
    return item


def _parent(value: Any, where: str) -> dict[str, int]:
    item = _object(value, _PARENT_KEYS, where)
    for name in _PARENT_KEYS:
        _integer(item[name], f"{where}.{name}", minimum=1 if name == "inode" else 0)
    if not stat.S_ISDIR(item["mode"]) or item["uid"] > 2**32 - 2 or item["gid"] > 2**32 - 2:
        _fail(f"{where}: invalid directory identity")
    return item


def _file_stat(value: Any, where: str, *, mode_type: str) -> dict[str, int]:
    item = _object(value, _STAT_KEYS, where)
    for name in _STAT_KEYS:
        _integer(item[name], f"{where}.{name}", minimum=1 if name in ("inode", "nlink") else 0)
    if (item["uid"] > 2**32 - 2 or item["gid"] > 2**32 - 2 or item["nlink"] != 1
            or (mode_type == "regular" and not stat.S_ISREG(item["mode"]))
            or (mode_type == "symlink" and not stat.S_ISLNK(item["mode"]))):
        _fail(f"{where}: invalid file identity")
    if item["size"] > (_MAX_STATE if mode_type == "regular" else _MAX_LINK):
        _fail(f"{where}: file exceeds size limit")
    return item


def _mapping(value: dict[str, Any], where: str) -> tuple[str, str, dict[str, Any], dict[str, int]]:
    container = _absolute(value["container_path"], f"{where}.container_path")
    host = _absolute(value["host_path"], f"{where}.host_path")
    mount = _mount(value["mount_identity"], f"{where}.mount_identity")
    parent = _parent(value["parent_identity"], f"{where}.parent_identity")
    if not _inside(container, mount["container"]) or container == mount["container"]:
        _fail(f"{where}: path is outside a directory bind")
    relative = PurePosixPath(container).relative_to(PurePosixPath(mount["container"]))
    if str(PurePosixPath(mount["host"]) / relative) != host:
        _fail(f"{where}: host mapping differs")
    return container, host, mount, parent


def _link_bytes(value: Any, info: dict[str, int], where: str) -> bytes:
    if type(value) is not str or len(value) > 8192:
        _fail(f"{where}: invalid link encoding")
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise EndpointError(f"{where}: invalid link encoding") from exc
    if (not data or len(data) != info["size"] or len(data) > _MAX_LINK or b"\0" in data
            or base64.b64encode(data).decode("ascii") != value):
        _fail(f"{where}: link bytes differ from recorded length")
    return data


def _next_link(path: str, raw: bytes, mount: dict[str, Any]) -> str:
    target = os.fsdecode(raw)
    if any(ord(char) < 32 or ord(char) == 127 for char in target) or "\\" in target:
        _fail("link target contains unsupported path bytes")
    base = PurePosixPath(mount["container"])
    if target.startswith("/"):
        parts = target.split("/")
        mount_parts = list(base.parts[1:])
        if parts[1:1 + len(mount_parts)] != mount_parts:
            _fail("link target leaves its original bind")
        current = list(mount_parts)
        pending = parts[1 + len(mount_parts):]
    else:
        current = list(PurePosixPath(path).parent.parts[1:])
        mount_parts = list(base.parts[1:])
        pending = target.split("/")
    for component in pending:
        if component in ("", "."):
            continue
        if component == "..":
            if len(current) <= len(mount_parts):
                _fail("link target leaves its original bind")
            current.pop()
        else:
            current.append(component)
    result = "/" + "/".join(current)
    return _absolute(result, "link target")


def _entry(value: Any, where: str, *, lock: bool = False) -> tuple[dict[str, Any], bytes | None]:
    item = _object(value, _ENTRY_KEYS, where)
    _mapping(item, where)
    kind = item["type"]
    if kind == "absent":
        if item["lstat"] is not None or item["link_bytes_b64"] is not None:
            _fail(f"{where}: absent entry has file data")
        return item, None
    if kind == "regular":
        _file_stat(item["lstat"], f"{where}.lstat", mode_type="regular")
        if item["link_bytes_b64"] is not None:
            _fail(f"{where}: regular entry has link bytes")
        return item, None
    if kind != "symlink" or lock:
        _fail(f"{where}: unsupported entry type")
    info = _file_stat(item["lstat"], f"{where}.lstat", mode_type="symlink")
    return item, _link_bytes(item["link_bytes_b64"], info, where)


def validate_endpoint(value: Any) -> dict[str, Any]:
    """Validate the complete FULL4 endpoint and kind-specific writer binding."""
    endpoint = _object(value, {"schema_version", "kind", "recipe_sha256", "image_id", "image_revision", "user",
                               "working_dir", "configured_path", "effective_path", "entry", "links", "referent",
                               "writer"}, "endpoint")
    if type(endpoint["schema_version"]) is not int or endpoint["schema_version"] != 1:
        _fail("endpoint: unsupported schema")
    if endpoint["kind"] not in ("chat-settings", "channel-replies"):
        _fail("endpoint: unsupported state kind")
    _match(endpoint["recipe_sha256"], _HEX64, "endpoint.recipe_sha256")
    image = _text(endpoint["image_id"], "endpoint.image_id")
    if not image.startswith("sha256:"):
        _fail("endpoint.image_id: invalid image")
    _match(image[7:], _HEX64, "endpoint.image_id")
    _match(endpoint["image_revision"], _HEX40, "endpoint.image_revision")
    user = _match(endpoint["user"], _USER, "endpoint.user")
    if any(int(part) > 2**32 - 2 for part in user.split(":")):
        _fail("endpoint.user: invalid identity")
    _absolute(endpoint["working_dir"], "endpoint.working_dir")
    if endpoint["configured_path"] is not None:
        _text(endpoint["configured_path"], "endpoint.configured_path")
    effective = _absolute(endpoint["effective_path"], "endpoint.effective_path")
    entry, entry_link = _entry(endpoint["entry"], "endpoint.entry")
    if entry["container_path"] != effective:
        _fail("endpoint.entry: effective path differs")
    links = endpoint["links"]
    if type(links) is not list or len(links) > 40:
        _fail("endpoint.links: invalid chain")
    current = effective
    seen: set[str] = set()
    for index, raw_link in enumerate(links):
        link = _object(raw_link, _LINK_KEYS, "endpoint.link")
        _mapping(link, "endpoint.link")
        info = _file_stat(link["lstat"], "endpoint.link.lstat", mode_type="symlink")
        raw = _link_bytes(link["link_bytes_b64"], info, "endpoint.link")
        if (link["container_path"] != current or current in seen
                or link["mount_identity"] != entry["mount_identity"]):
            _fail("endpoint.links: chain or bind differs")
        if index == 0 and (entry_link is None or link != {key: entry[key] for key in _LINK_KEYS}):
            _fail("endpoint.links: first link differs from entry")
        seen.add(current)
        current = _next_link(current, raw, link["mount_identity"])
    if (entry_link is None) != (len(links) == 0):
        _fail("endpoint.links: entry and chain disagree")
    referent = _object(endpoint["referent"], _REFERENT_KEYS, "endpoint.referent")
    _mapping(referent, "endpoint.referent")
    if referent["container_path"] != current or referent["mount_identity"] != entry["mount_identity"]:
        _fail("endpoint.referent: chain or bind differs")
    if type(referent["present"]) is not bool:
        _fail("endpoint.referent: invalid presence")
    if referent["present"]:
        _file_stat(referent["stat"], "endpoint.referent.stat", mode_type="regular")
        _match(referent["sha256"], _HEX64, "endpoint.referent.sha256")
    elif referent["stat"] is not None or referent["sha256"] is not None:
        _fail("endpoint.referent: absent file has data")
    if not links:
        if (referent["present"] != (entry["type"] == "regular") or referent["stat"] != entry["lstat"]
                or referent["parent_identity"] != entry["parent_identity"]):
            _fail("endpoint.referent: direct entry differs")
    writer = _object(endpoint["writer"], _WRITER_KEYS, "endpoint.writer")
    _mapping(writer, "endpoint.writer")
    role = "referent" if endpoint["kind"] == "chat-settings" else "entry"
    source = referent if role == "referent" else entry
    if writer["role"] != role or any(writer[key] != source[key] for key in (
        "container_path", "host_path", "mount_identity", "parent_identity",
    )):
        _fail("endpoint.writer: kind-specific location differs")
    if type(writer["can_replace"]) is not bool or (writer["can_replace"] and writer["mount_identity"]["read_only"]):
        _fail("endpoint.writer: invalid replacement capability")
    lock = writer["settings_lock_binding"]
    if role == "entry":
        if lock is not None:
            _fail("endpoint.writer: reply index has no settings lock")
    else:
        lock, _ = _entry(lock, "endpoint.writer.settings_lock_binding", lock=True)
        if (lock["container_path"] != writer["container_path"] + ".lock"
                or lock["host_path"] != writer["host_path"] + ".lock"
                or lock["mount_identity"] != writer["mount_identity"]
                or lock["parent_identity"] != writer["parent_identity"]):
            _fail("endpoint.writer: settings lock differs")
    return endpoint


def require_unchanged(expected: Any, observed: Any) -> None:
    """Require the same complete endpoint before a claimed state operation."""
    validate_endpoint(expected)
    validate_endpoint(observed)
    if expected != observed:
        _fail("endpoint changed after observation")


def _open_physical_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = None
    try:
        fd = os.open("/", flags)
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise EndpointError("entry parent is not a physical directory") from exc


def _physical_directory(path: Path) -> None:
    os.close(_open_physical_directory(path))


def map_entry(snapshot: dict[str, Any], container_path: str, *, writable: bool) -> str:
    """Map a leaf entry without following it or relaxing other bind mappers."""
    path = _absolute(container_path, "entry path")
    if type(snapshot) is not dict or type(snapshot.get("mounts")) is not list or type(writable) is not bool:
        _fail("entry mapping: invalid recipe")
    mounts: list[dict[str, Any]] = []
    for raw in snapshot["mounts"]:
        if type(raw) is not dict:
            _fail("entry mapping: invalid mount")
        target = _absolute(raw.get("container"), "entry mapping.mount.container")
        target_path = PurePosixPath(target)
        if target == "/" or any(target_path.is_relative_to(item) or item.is_relative_to(target_path)
                                for item in _PROTECTED):
            _fail("entry mapping: protected mount")
        if "host" in raw:
            if set(raw) not in ({"container", "host", "read_only"}, _MOUNT_KEYS):
                _fail("entry mapping: invalid bind")
            _absolute(raw["host"], "entry mapping.mount.host")
            if type(raw["read_only"]) is not bool:
                _fail("entry mapping: invalid read-only flag")
        elif set(raw) != {"type", "container", "options"} or raw["type"] != "tmpfs" or type(raw["options"]) is not list:
            _fail("entry mapping: unsupported mount")
        mounts.append(raw)
    if len({item["container"] for item in mounts}) != len(mounts):
        _fail("entry mapping: duplicate mount")
    binds = [item for item in mounts if "host" in item]
    identities = []
    for bind in binds:
        source = Path(bind["host"])
        _physical_directory(source.parent)
        info = source.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _physical_directory(source)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail("entry mapping: bind is not a physical file or directory")
        if "device" in bind or "inode" in bind:
            if (type(bind.get("device")) is not int or type(bind.get("inode")) is not int
                    or (info.st_dev, info.st_ino) != (bind["device"], bind["inode"])):
                _fail("entry mapping: bind identity differs")
        identities.append((info.st_dev, info.st_ino))
    if len(set(identities)) != len(identities):
        _fail("entry mapping: ambiguous bind identity")
    for index, left in enumerate(binds):
        for right in binds[index + 1:]:
            if _inside(left["host"], right["host"]) or _inside(right["host"], left["host"]):
                _fail("entry mapping: ambiguous bind alias")
    candidates = [item for item in mounts if _inside(path, item["container"])]
    if not candidates:
        _fail("entry mapping: no visible bind")
    chosen = max(candidates, key=lambda item: len(PurePosixPath(item["container"]).parts))
    if ("host" not in chosen or path == chosen["container"] or (writable and chosen["read_only"])
            or not stat.S_ISDIR(Path(chosen["host"]).stat(follow_symlinks=False).st_mode)):
        _fail("entry mapping: persistent writable bind unavailable")
    source = Path(chosen["host"])
    enclosing_path = str(PurePosixPath(chosen["container"]).parent)
    enclosing = [item for item in mounts if item is not chosen and _inside(enclosing_path, item["container"])]
    if enclosing:
        outer = max(enclosing, key=lambda item: len(PurePosixPath(item["container"]).parts))
        if "host" not in outer:
            _fail("entry mapping: nested bind parent is not persistent")
        relative_parent = PurePosixPath(enclosing_path).relative_to(PurePosixPath(outer["container"]))
        _physical_directory(Path(outer["host"]).joinpath(*relative_parent.parts))
    relative = PurePosixPath(path).relative_to(PurePosixPath(chosen["container"]))
    host = source.joinpath(*relative.parts)
    _physical_directory(host.parent)
    return str(host)


def verify_terminal_route(snapshot: dict[str, Any], recorded: dict[str, Any]) -> None:
    """Check a terminal writer route while allowing ordinary file progress."""
    validate_endpoint(recorded)

    def inspect(binding: dict[str, Any]) -> tuple[os.stat_result | None, bytes | None]:
        host = map_entry(snapshot, binding["container_path"], writable=False)
        if host != binding["host_path"]:
            _fail("terminal endpoint host mapping changed")
        mount = binding["mount_identity"]
        mount_info = Path(mount["host"]).stat(follow_symlinks=False)
        if (mount_info.st_dev, mount_info.st_ino) != (mount["device"], mount["inode"]):
            _fail("terminal endpoint mount changed")
        parent = Path(host).parent
        fd = _open_physical_directory(parent)
        try:
            info = os.fstat(fd)
            identity = {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
                        "uid": info.st_uid, "gid": info.st_gid}
            if identity != binding["parent_identity"]:
                _fail("terminal endpoint parent changed")
            try:
                observed = os.stat(os.fsencode(Path(host).name), dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None, None
            link = (os.readlink(os.fsencode(Path(host).name), dir_fd=fd)
                    if stat.S_ISLNK(observed.st_mode) else None)
            again = os.stat(os.fsencode(Path(host).name), dir_fd=fd, follow_symlinks=False)
            def fields(value: os.stat_result) -> tuple[int, ...]:
                return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
                        value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if fields(observed) != fields(again):
                _fail("terminal endpoint changed during inspection")
            named = _open_physical_directory(parent)
            try:
                latest = os.fstat(named)
                if (latest.st_dev, latest.st_ino, latest.st_mode, latest.st_uid, latest.st_gid) != (
                        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid):
                    _fail("terminal endpoint parent was replaced")
            finally:
                os.close(named)
            return observed, link
        finally:
            os.close(fd)

    entry, link = inspect(recorded["entry"])
    original_type = recorded["entry"]["type"]
    if recorded["kind"] == "channel-replies" and entry is not None and stat.S_ISREG(entry.st_mode):
        if entry.st_nlink != 1:
            _fail("terminal reply entry is not an independent file")
        return
    if original_type == "symlink":
        if entry is None or not stat.S_ISLNK(entry.st_mode):
            _fail("terminal state link was replaced unexpectedly")
        for saved in recorded["links"]:
            current, raw = inspect(saved)
            if current is None or not stat.S_ISLNK(current.st_mode):
                _fail("terminal state link disappeared")
            fields = {"device": current.st_dev, "inode": current.st_ino, "size": current.st_size,
                      "mtime_ns": current.st_mtime_ns, "ctime_ns": current.st_ctime_ns,
                      "mode": current.st_mode, "uid": current.st_uid, "gid": current.st_gid,
                      "nlink": current.st_nlink}
            if fields != saved["lstat"] or raw != base64.b64decode(saved["link_bytes_b64"], validate=True):
                _fail("terminal state link was redirected")
        observed, _ = inspect(recorded["referent"])
    else:
        observed = entry
        if link is not None:
            _fail("terminal state entry became a link")
    if observed is None:
        if recorded["referent"]["present"]:
            _fail("terminal state file disappeared")
    elif not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        _fail("terminal state file has an unsupported type")
    lock = recorded["writer"]["settings_lock_binding"]
    if lock is not None:
        current, _ = inspect(lock)
        if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
            _fail("terminal state lock has an unsupported type")


# This is intentionally self-contained: a saved A probe runs it inside the
# recorded image, never importing code from the deployment checkout.
ENDPOINT_CODE = r'''import base64,hashlib,json,os,posixpath,re,stat,sys
MAX=16777216
PROTECTED=('/usr','/bin','/sbin','/lib','/lib64','/etc','/proc','/sys','/dev','/app/.venv')
def fail(): raise ValueError('endpoint refused')
def text(x):
 if type(x) is not str or not x or len(x)>4096 or any(ord(c)<32 or ord(c)==127 for c in x): fail()
 return x
def absolute(x):
 x=text(x)
 if x!='/' and (not x.startswith('/') or '\\' in x or any(p in ('','.','..') for p in x.split('/')[1:])): fail()
 return x
def inside(path,base): return path==base or path.startswith(base.rstrip('/')+'/')
def unique(pairs):
 result={}
 for k,v in pairs:
  if k in result: fail()
  result[k]=v
 return result
def number(value,minimum=0):
 if type(value) is not int or not minimum<=value<=2**63-1: fail()
 return value
def identity(s):
 return {'device':s.st_dev,'inode':s.st_ino,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns,
  'mode':s.st_mode,'uid':s.st_uid,'gid':s.st_gid,'nlink':s.st_nlink}
def parent_identity(s): return {'device':s.st_dev,'inode':s.st_ino,'mode':s.st_mode,'uid':s.st_uid,'gid':s.st_gid}
def physical_dir(path):
 absolute(path); fds=[]; flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
 try:
  fd=os.open('/',flags); fds.append(fd)
  for part in path.split('/')[1:]:
   if not part: continue
   fd=os.open(part,flags,dir_fd=fd); fds.append(fd)
  result=os.dup(fd)
 finally:
  for fd in reversed(fds): os.close(fd)
 return result
def access(path,mode):
 if os.access in os.supports_effective_ids: return os.access(path,mode,effective_ids=True)
 if (os.getuid(),os.getgid())!=(os.geteuid(),os.getegid()): fail()
 return os.access(path,mode)
def parse(req,read_alias=None):
 keys={'kind','recipe_sha256','image_id','image_revision','user','working_dir','configured_path','effective_path',
  'mounts','require_writer'}
 if type(req) is not dict or set(req)!=keys or req['kind'] not in ('chat-settings','channel-replies'): fail()
 if type(req['recipe_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',req['recipe_sha256']): fail()
 if type(req['image_id']) is not str or not re.fullmatch('sha256:[0-9a-f]{64}',req['image_id']): fail()
 if type(req['image_revision']) is not str or not re.fullmatch('[0-9a-f]{40}',req['image_revision']): fail()
 if type(req['user']) is not str or not re.fullmatch(r'(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)',req['user']): fail()
 uid,gid=map(int,req['user'].split(':'))
 if uid>2**32-2 or gid>2**32-2 or (os.geteuid(),os.getegid())!=(uid,gid): fail()
 absolute(req['working_dir']); absolute(req['effective_path'])
 if req['configured_path'] is not None: text(req['configured_path'])
 if type(req['require_writer']) is not bool or type(req['mounts']) is not list or not 1<=len(req['mounts'])<=128: fail()
 mounts=[]; targets=set(); binds=[]
 for item in req['mounts']:
  if type(item) is not dict: fail()
  c=absolute(item.get('container'))
  if c=='/' or c in targets or any(inside(c,p) or inside(p,c) for p in PROTECTED): fail()
  targets.add(c)
  if set(item)=={'host','container','read_only','device','inode'}:
   absolute(item['host']); number(item['device']); number(item['inode'],1)
   if type(item['read_only']) is not bool: fail()
   verify_mounts([item])
   binds.append(item)
  elif set(item)=={'type','container','options'} and item['type']=='tmpfs' and type(item['options']) is list:
   if any(type(option) is not str for option in item['options']): fail()
  else: fail()
  mounts.append(item)
 for index,a in enumerate(binds):
  for b in binds[index+1:]:
   if inside(a['host'],b['host']) or inside(b['host'],a['host']) or (a['device'],a['inode'])==(b['device'],b['inode']):
    if read_alias is None or read_alias not in (a,b) or read_alias.get('read_only') is not True: fail()
 if read_alias is not None:
  if read_alias not in binds or read_alias['read_only'] is not True: fail()
  for item in mounts:
   if item!=read_alias and (inside(item['container'],read_alias['container']) or inside(read_alias['container'],item['container'])): fail()
 return mounts
def verify_mounts(mounts):
 for item in mounts:
  if 'host' not in item: continue
  path=item['container']; parent=physical_dir(posixpath.dirname(path))
  try: fd=os.open(os.fsencode(posixpath.basename(path)),os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
  finally: os.close(parent)
  try:
   info=os.fstat(fd)
   if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink==1) or (info.st_dev,info.st_ino)!=(item['device'],item['inode']): fail()
  finally: os.close(fd)
def mount_for(path,mounts):
 possible=[item for item in mounts if inside(path,item['container'])]
 if not possible: fail()
 return max(possible,key=lambda item: len(item['container'].split('/')))
def mapped(path,mount):
 if 'host' not in mount or path==mount['container'] or not inside(path,mount['container']): fail()
 suffix=path[len(mount['container']):].lstrip('/')
 return absolute(mount['host'].rstrip('/')+'/'+suffix)
def node(path,mounts,initial):
 path=absolute(path); mount=mount_for(path,mounts)
 if mount!=initial or 'host' not in mount: fail()
 parent=posixpath.dirname(path); name=posixpath.basename(path)
 fd=physical_dir(parent)
 try:
  before_parent=os.fstat(fd)
  try: item=os.stat(os.fsencode(name),dir_fd=fd,follow_symlinks=False)
  except FileNotFoundError: item=None
  if item is None: kind='absent'; info=None; link=None
  elif stat.S_ISLNK(item.st_mode):
   kind='symlink'; info=identity(item)
   if item.st_nlink!=1 or item.st_size<1 or item.st_size>4095: fail()
   raw=os.readlink(os.fsencode(name),dir_fd=fd)
   if len(raw)!=item.st_size: fail()
   link=base64.b64encode(raw).decode('ascii')
  elif stat.S_ISREG(item.st_mode):
   kind='regular'; info=identity(item); link=None
   if item.st_nlink!=1 or item.st_size>MAX: fail()
  else: fail()
  try: again=os.stat(os.fsencode(name),dir_fd=fd,follow_symlinks=False)
  except FileNotFoundError: again=None
  if (None if item is None else identity(item))!=(None if again is None else identity(again)): fail()
  after_parent=os.fstat(fd)
  if parent_identity(before_parent)!=parent_identity(after_parent): fail()
  named_parent=physical_dir(parent)
  try:
   if parent_identity(os.fstat(named_parent))!=parent_identity(after_parent): fail()
  finally: os.close(named_parent)
  return {'container_path':path,'host_path':mapped(path,mount),'mount_identity':dict(mount),
   'parent_identity':parent_identity(after_parent),'type':kind,'lstat':info,'link_bytes_b64':link}
 finally: os.close(fd)
def next_path(current,raw,mount,mounts):
 target=os.fsdecode(raw)
 if not target or any(ord(c)<32 or ord(c)==127 for c in target) or '\\' in target: fail()
 base=mount['container'].split('/')[1:]
 if target.startswith('/'):
  parts=target.split('/')
  if parts[1:1+len(base)]!=base: fail()
  current_parts=list(base); pending=parts[1+len(base):]
 else:
  current_parts=posixpath.dirname(current).split('/')[1:]; pending=target.split('/')
 pending=[component for component in pending if component not in ('','.')]
 for index,component in enumerate(pending):
  if component=='..':
   if len(current_parts)<=len(base): fail()
   current_parts.pop()
  else: current_parts.append(component)
  candidate='/'+ '/'.join(current_parts)
  if mount_for(candidate,mounts)!=mount: fail()
  if index<len(pending)-1:
   fd=physical_dir(candidate)
   os.close(fd)
 return absolute('/'+'/'.join(current_parts))
def content(entry):
 path=entry['container_path']; parent=posixpath.dirname(path); name=posixpath.basename(path); fd=physical_dir(parent)
 try:
  if parent_identity(os.fstat(fd))!=entry['parent_identity']: fail()
  if entry['type']=='absent':
   try: os.stat(os.fsencode(name),dir_fd=fd,follow_symlinks=False)
   except FileNotFoundError:
    named_parent=physical_dir(parent)
    try:
     if parent_identity(os.fstat(named_parent))!=entry['parent_identity']: fail()
    finally: os.close(named_parent)
    return False,None,None
   fail()
  if entry['type']!='regular': fail()
  flags=os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC
  f=os.open(os.fsencode(name),flags,dir_fd=fd)
  try:
   before=os.fstat(f)
   if identity(before)!=entry['lstat'] or not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX: fail()
   digest=hashlib.sha256(); total=0
   while True:
    chunk=os.read(f,131072)
    if not chunk: break
    total+=len(chunk)
    if total>MAX: fail()
    digest.update(chunk)
   after=os.fstat(f); named=os.stat(os.fsencode(name),dir_fd=fd,follow_symlinks=False)
   if identity(before)!=identity(after) or identity(after)!=identity(named) or total!=after.st_size: fail()
   named_parent=physical_dir(parent)
   try:
    if parent_identity(os.fstat(named_parent))!=entry['parent_identity']: fail()
   finally: os.close(named_parent)
   return True,identity(after),digest.hexdigest()
  finally: os.close(f)
 finally: os.close(fd)
def collect(req,mounts):
 verify_mounts(mounts)
 path=req['effective_path']; original=mount_for(path,mounts)
 if 'host' not in original: fail()
 first=node(path,mounts,original); current=first; links=[]; seen=set()
 while current['type']=='symlink':
  if current['container_path'] in seen or len(links)>=40: fail()
  seen.add(current['container_path']); links.append({k:v for k,v in current.items() if k!='type'})
  raw=base64.b64decode(current['link_bytes_b64'],validate=True)
  current=node(next_path(current['container_path'],raw,original,mounts),mounts,original)
 present,file_stat,digest=content(current)
 referent={k:current[k] for k in ('container_path','host_path','mount_identity','parent_identity')}
 referent.update(present=present,stat=file_stat,sha256=digest)
 role='referent' if req['kind']=='chat-settings' else 'entry'
 target=referent if role=='referent' else first
 writer={k:target[k] for k in ('container_path','host_path','mount_identity','parent_identity')}
 writer['role']=role; writer['settings_lock_binding']=None
 writer_path=writer['container_path']; parent=posixpath.dirname(writer_path)
 parent_fd=physical_dir(parent)
 try:
  parent_stat=os.fstat(parent_fd)
  if parent_identity(parent_stat)!=writer['parent_identity']: fail()
  current_writer=node(writer_path,mounts,original)
  writable=not original['read_only'] and access(parent,os.R_OK|os.W_OK|os.X_OK)
  if parent_stat.st_mode & stat.S_ISVTX and current_writer['lstat'] is not None and os.geteuid() not in (0,parent_stat.st_uid,current_writer['lstat']['uid']): writable=False
  if role=='referent':
   lock=node(writer_path+'.lock',mounts,original)
   if lock['type']=='symlink': fail()
   writer['settings_lock_binding']=lock
   if lock['type']=='regular' and not access(lock['container_path'],os.R_OK|os.W_OK): writable=False
  writer['can_replace']=bool(writable)
 finally: os.close(parent_fd)
 if req['require_writer'] and not writer['can_replace']: fail()
 verify_mounts(mounts)
 return {'schema_version':1,'kind':req['kind'],'recipe_sha256':req['recipe_sha256'],'image_id':req['image_id'],
  'image_revision':req['image_revision'],'user':req['user'],'working_dir':req['working_dir'],
  'configured_path':req['configured_path'],'effective_path':path,'entry':first,'links':links,
  'referent':referent,'writer':writer}
try:
 if len(sys.argv)!=2: fail()
 request=json.loads(sys.argv[1],object_pairs_hook=unique,parse_constant=lambda _:fail())
 mounts=parse(request); first=collect(request,mounts); second=collect(request,mounts)
 if first!=second: fail()
 print(json.dumps(second,sort_keys=True,ensure_ascii=True,allow_nan=False))
except Exception:
 print('state endpoint refused',file=sys.stderr); sys.exit(1)
'''
