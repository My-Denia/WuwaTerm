#!/usr/bin/env python3
"""Observe Telegram state under a recorded container identity, without writing it.

The deployment journal and source capsule own probe admission and recovery.  A
probe is not a service and never receives the service's secret environment.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(CODE_ROOT / "scripts"))
import component_observe as observe  # noqa: E402
import component_state_endpoint as endpoint_module  # noqa: E402
import component_runtime as runtime  # noqa: E402
import component_wheel_build as wheel  # noqa: E402
import deployment_admission as admission  # noqa: E402
import product_manifest as manifest  # noqa: E402

_PATH_KEYS = ("WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH", "WUWATERM_CHANNEL_REPLY_INDEX_PATH")
_LABEL = "org.wuwaterm.component-state-probe"
# Schema-5 journals use only helpers whose programs do not write state; the
# copy, endpoint and FULL4 canonical helpers stay schema-3/4-only.
SCHEMA5_HELPER_KINDS = frozenset({"state-plan", "state-legacy-plan", "state-preimage", "state-canonical"})
_MAX_OUTPUT = 1024 * 1024
_PROTECTED = tuple(PurePosixPath(p) for p in (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/app/.venv",
))
# The imported planner is the installed product implementation verified by the
# registered wheel producer. It receives namespace paths, never service secrets.
_PLAN_CODE = """import json,os,pathlib,stat,sys,sysconfig
uid,gid=map(int,sys.argv[3].split(':'))
if (os.geteuid(),os.getegid()) != (uid,gid): raise RuntimeError('identity differs')
for path,device,inode in json.loads(sys.argv[4]):
 info=os.stat(path,follow_symlinks=False)
 if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (info.st_dev,info.st_ino)!=(device,inode): raise RuntimeError('bind differs')
from wuwaterm_telegram.operations import state_plan
print(json.dumps(state_plan(pathlib.Path(sys.argv[1]),json.loads(sys.argv[2])),sort_keys=True))
"""
# An unselected legacy image has no modern state_plan. Resolve its own state
# paths with all persistent binds read-only, and report the copy it would make
# at startup instead of attempting it: its resolver writes the legacy file into
# an empty StateDir, which these mounts refuse. Preserve settings referent
# versus reply-index directory-entry semantics when reporting the paths.
_LEGACY_PLAN_CODE = """import json,os,pathlib,stat,sys,sysconfig
uid,gid=map(int,sys.argv[3].split(':'))
if (os.geteuid(),os.getegid()) != (uid,gid): raise RuntimeError('identity differs')
for path,device,inode in json.loads(sys.argv[4]):
 info=os.stat(path,follow_symlinks=False)
 if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (info.st_dev,info.st_ino)!=(device,inode): raise RuntimeError('bind differs')
from wuwaterm.bot import _state_file_path, _same_state_path
db=pathlib.Path(sys.argv[1]); plans={}
state_dir=os.getenv('WUWATERM_STATE_DIR','').strip()
for kind,filename,variable,label in [('chat-settings','chat_settings.json','WUWATERM_SETTINGS_PATH','chat settings'),
 ('channel-replies','channel_replies.json','WUWATERM_CHANNEL_REPLY_INDEX_PATH','channel reply index')]:
 explicit=os.getenv(variable,'').strip() or None
 chosen=pathlib.Path(explicit).expanduser() if explicit else None
 legacy=db.resolve(strict=False).parent/filename
 target=pathlib.Path(state_dir).expanduser()/filename if state_dir else None
 retained=(target is not None and (chosen is None or _same_state_path(chosen,legacy)
  or _same_state_path(chosen,target)))
 pending=(retained and legacy.exists() and not target.exists()
  and not _same_state_path(legacy,target))
 if pending:
  entry=target
 else:
  effective=_state_file_path(db,filename=filename,explicit_path=explicit,label=label)
  entry=pathlib.Path(os.path.abspath(os.fspath(effective)))
 migrated=retained and legacy.exists() and target.exists()
 output=entry.resolve(strict=False) if kind=='chat-settings' else entry
 plans[kind]={'effective_path':str(entry),'legacy_path':str(legacy),
  'migration':'validate-existing' if migrated else 'copy-if-absent' if pending else 'none',
  'mapped_target':str(output),
  'mapped_legacy':str(legacy.resolve(strict=False)) if retained else None}
print(json.dumps({'database_path':str(db),'kinds':plans},sort_keys=True))
"""
_CANONICAL_CODE = """import errno,hashlib,json,os,pathlib,stat,sys,sysconfig
uid,gid=map(int,sys.argv[2].split(':'))
if (os.geteuid(),os.getegid()) != (uid,gid): raise RuntimeError('identity differs')
for path,device,inode in json.loads(sys.argv[3]):
 info=os.stat(path,follow_symlinks=False)
 if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (info.st_dev,info.st_ino)!=(device,inode): raise RuntimeError('bind differs')
path=pathlib.Path(sys.argv[1]); flavor=sys.argv[4]; kind=sys.argv[5]; clock=int(sys.argv[6]); scratch=pathlib.Path(sys.argv[7])/'state.json'
writer_path=pathlib.Path(sys.argv[8]) if len(sys.argv)>8 else path
expected=json.loads(sys.argv[9]) if len(sys.argv)>9 else None
try:
 try: fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_CLOEXEC)
 except OSError as exc:
  if exc.errno!=errno.EPERM: raise
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
except FileNotFoundError: fd=None
data=None
if fd is not None:
 try:
  before=os.fstat(fd)
  if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>16777216: raise ValueError('file')
  data=bytearray()
  while True:
   chunk=os.read(fd,131072)
   if not chunk: break
   data.extend(chunk)
  after=os.fstat(fd); named=os.stat(path,follow_symlinks=False)
  fields=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
  if fields(before)!=fields(after) or fields(after)!=fields(named): raise ValueError('changed')
  if expected is not None:
   observed={'device':after.st_dev,'inode':after.st_ino,'size':after.st_size,'mtime_ns':after.st_mtime_ns,
    'ctime_ns':after.st_ctime_ns,'mode':after.st_mode,'uid':after.st_uid,'gid':after.st_gid,'nlink':after.st_nlink}
   if not expected['present'] or expected['stat']!=observed or expected['sha256']!=hashlib.sha256(data).hexdigest(): raise ValueError('source identity')
 finally: os.close(fd)
elif expected is not None and expected['present']: raise ValueError('source absent')
if data is not None:
 out=os.open(scratch,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(out,'wb') as stream: stream.write(data)
if flavor=='legacy':
 from wuwaterm.bot import _validate_state_file
 from wuwaterm.settings import ChatSettings
 from wuwaterm.channel_reply_index import ChannelReplyIndex
 if data is not None: _validate_state_file(scratch,label='chat settings' if kind=='chat-settings' else 'channel reply index')
elif flavor=='wheel':
 from wuwaterm_telegram.state_validation import main
 from wuwaterm_telegram.settings import ChatSettings
 from wuwaterm_telegram.channel_reply_index import ChannelReplyIndex
 if data is not None and main([str(scratch),kind])!=0: raise ValueError('invalid state')
else: raise ValueError('flavor')
if data is not None and kind=='chat-settings':
 public,allowed=ChatSettings(scratch)._read_state(strict=True)
 canonical={'public':sorted(public.items()),'allowed':sorted(allowed)}
 destination=path.resolve(strict=False); lock=destination.with_name(destination.name+'.lock')
 lock_ok=not lock.exists() or (lock.is_file() and os.access(lock,os.R_OK|os.W_OK))
elif data is not None:
 index=ChannelReplyIndex(storage_path=scratch,clock=lambda:clock)
 if index._last_load_ok is not True or index._load_failures: raise ValueError('reader failed')
 canonical={'entries':[[k[0],k[1],v[0],list(v[1])] for k,v in sorted(index._entries.items())]}
else:
 canonical=None; destination=path.resolve(strict=False) if kind=='chat-settings' else path; lock_ok=True
if kind=='chat-settings':
 destination=writer_path.resolve(strict=False); lock=destination.with_name(destination.name+'.lock')
 lock_ok=not lock.exists() or (lock.is_file() and os.access(lock,os.R_OK|os.W_OK))
else: destination=writer_path; lock_ok=True
parent=destination.parent; info=parent.stat()
try: entry=destination.lstat()
except FileNotFoundError: entry=None
writable=stat.S_ISDIR(info.st_mode) and os.access(parent,os.R_OK|os.W_OK|os.X_OK) and lock_ok
if entry is not None and info.st_mode & stat.S_ISVTX and os.geteuid() not in (0,info.st_uid,entry.st_uid): writable=False
encoded=None if canonical is None else json.dumps(canonical,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
print(json.dumps({'raw_sha256':None if data is None else hashlib.sha256(data).hexdigest(),
 'canonical_sha256':None if encoded is None else hashlib.sha256(encoded).hexdigest(),'writer_can_replace':bool(writable)},sort_keys=True))
"""
_COPY_CODE = """import ctypes,hashlib,json,os,pathlib,stat,sys,sysconfig
uid,gid=map(int,sys.argv[2].split(':'))
if (os.geteuid(),os.getegid()) != (uid,gid): raise RuntimeError('identity differs')
for path,device,inode in json.loads(sys.argv[3]):
 info=os.stat(path,follow_symlinks=False)
 if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (info.st_dev,info.st_ino)!=(device,inode): raise RuntimeError('bind differs')
mode,flavor,kind=sys.argv[4:7]; request=json.loads(sys.argv[7])
def identity(s): return {'device':s.st_dev,'inode':s.st_ino,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns}
def read(path,allow_links=False):
 try: fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_CLOEXEC)
 except FileNotFoundError: return {'present':False,'sha256':None,'stat':None},None
 try:
  b=os.fstat(fd)
  if not stat.S_ISREG(b.st_mode) or b.st_size>16777216 or (b.st_nlink!=1 and not (allow_links and b.st_nlink==2)): raise ValueError('file')
  data=bytearray()
  while True:
   chunk=os.read(fd,131072)
   if not chunk: break
   data.extend(chunk)
  a=os.fstat(fd); n=os.stat(path,follow_symlinks=False)
  if identity(b)!=identity(a) or identity(a)!=identity(n): raise ValueError('changed')
  return {'present':True,'sha256':hashlib.sha256(data).hexdigest(),'stat':identity(a)},data
 finally: os.close(fd)
source=pathlib.Path(request['source']); destination=pathlib.Path(request['destination']); stage=pathlib.Path(request['stage'])
if not all(p.is_absolute() and '..' not in p.parts for p in (source,destination,stage)) or stage.parent!=destination.parent or len({source,destination,stage})!=3: raise ValueError('path')
src,data=read(source)
if mode!='cleanup' and (src!=request['source_before'] or data is None): raise ValueError('source')
def validate(path):
 if flavor=='legacy':
  from wuwaterm.bot import _validate_state_file
  _validate_state_file(path,label='chat settings' if kind=='chat-settings' else 'channel reply index')
 elif flavor=='wheel':
  from wuwaterm_telegram.state_validation import main
  if main([str(path),kind])!=0: raise ValueError('invalid state')
 else: raise ValueError('flavor')
parent=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
def publish_absent():
 rename=ctypes.CDLL(None,use_errno=True).renameat2
 rename.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
 rename.restype=ctypes.c_int
 if rename(parent,os.fsencode(stage.name),parent,os.fsencode(destination.name),1)!=0: raise OSError(ctypes.get_errno(),'renameat2')
try:
 if mode=='stage':
  if read(destination)[0]!=request['destination_before']: raise ValueError('destination')
  fd=os.open(stage.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
  with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
  validate(stage); os.fsync(parent)
  result,_=read(stage)
  if result['sha256']!=src['sha256']: raise ValueError('stage')
 elif mode=='publish':
  staged,_=read(stage,allow_links=True); dest,_=read(destination,allow_links=True); expected=request['stage_before']
  def same_inode(a,b): return a['present'] and b['present'] and all(a['stat'][k]==b['stat'][k] for k in ('device','inode','size','mtime_ns')) and a['sha256']==b['sha256']
  if not staged['present'] and same_inode(dest,expected): result=dest
  elif staged['present'] and same_inode(staged,expected) and same_inode(dest,expected) and not request['destination_before']['present']:
   os.unlink(stage.name,dir_fd=parent); os.fsync(parent); result,_=read(destination)
  else:
   if staged!=expected or dest!=request['destination_before']: raise ValueError('publish preimage')
   validate(stage)
   if read(destination)[0]!=request['destination_before'] or read(source)[0]!=request['source_before']: raise ValueError('changed before publish')
   if dest['present']: os.replace(stage.name,destination.name,src_dir_fd=parent,dst_dir_fd=parent)
   else: publish_absent()
   os.fsync(parent); result,_=read(destination)
  if not result['present'] or result['sha256']!=src['sha256']: raise ValueError('published')
 elif mode=='cleanup':
  dest,_=read(destination); staged,_=read(stage)
  if dest!=request['destination_before'] or staged['present']: raise ValueError('cleanup preimage')
  if src['present']:
   if src!=request['source_before'] or src['sha256']!=dest['sha256']: raise ValueError('cleanup source')
   source_parent=os.open(source.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
   try: os.unlink(source.name,dir_fd=source_parent); os.fsync(source_parent)
   finally: os.close(source_parent)
  result,_=read(source)
  if result['present']: raise ValueError('cleanup remained')
 elif mode=='discard':
  dest,_=read(destination); staged,_=read(stage)
  if dest!=request['destination_before'] or dest['present']: raise ValueError('discard destination')
  if staged['present']:
   info=os.stat(stage,follow_symlinks=False)
   if staged['sha256']!=src['sha256'] or info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o600: raise ValueError('discard stage')
   if request['stage_before'] is not None and staged!=request['stage_before']: raise ValueError('discard preimage')
   os.unlink(stage.name,dir_fd=parent); os.fsync(parent)
  result,_=read(stage)
  if result['present']: raise ValueError('discard remained')
 else: raise ValueError('mode')
 if mode!='cleanup' and read(source)[0]!=request['source_before']: raise ValueError('source changed')
 print(json.dumps(result,sort_keys=True))
finally: os.close(parent)
"""
# O_NOATIME avoids turning a readback into a filesystem metadata change. The
# component journal already uses these exact device/inode/size/time fields.
_HASH_CODE = """import hashlib,json,os,stat,sys
uid,gid=map(int,sys.argv[2].split(':'))
if (os.geteuid(),os.getegid()) != (uid,gid): raise RuntimeError('identity differs')
for path,device,inode in json.loads(sys.argv[3]):
 info=os.stat(path,follow_symlinks=False)
 if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or (info.st_dev,info.st_ino)!=(device,inode): raise RuntimeError('bind differs')
parts=sys.argv[1].split('/')[1:]
if not parts or any(not p or p in ('.','..') for p in parts): raise ValueError('path')
d=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
try:
 for p in parts[:-1]:
  n=os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=d)
  os.close(d); d=n
 try: before=os.stat(parts[-1],dir_fd=d,follow_symlinks=False)
 except FileNotFoundError:
  print(json.dumps({'present':False,'sha256':None,'stat':None},sort_keys=True)); sys.exit(0)
 if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16777216: raise ValueError('file')
 fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME|os.O_CLOEXEC,dir_fd=d)
 try:
  opened=os.fstat(fd)
  fields=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
  if fields(opened)!=fields(before) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1: raise ValueError('changed')
  h=hashlib.sha256()
  while True:
   data=os.read(fd,131072)
   if not data: break
   h.update(data)
  after=os.fstat(fd)
  current=os.stat(parts[-1],dir_fd=d,follow_symlinks=False)
  if fields(after)!=fields(before) or fields(current)!=fields(before) or current.st_nlink!=1: raise ValueError('changed')
  out={'device':after.st_dev,'inode':after.st_ino,'size':after.st_size,'mtime_ns':after.st_mtime_ns,'ctime_ns':after.st_ctime_ns}
  print(json.dumps({'present':True,'sha256':h.hexdigest(),'stat':out},sort_keys=True))
 finally: os.close(fd)
finally: os.close(d)
"""


class StateError(RuntimeError):
    """A safe and attributable state observation could not be established."""


def _fail(message: str) -> None:
    raise StateError(message)


def _request(method: str, endpoint: str, body: dict[str, Any] | None = None,
             *, allowed: tuple[int, ...] = (200,)) -> Any:
    try:
        if not stat.S_ISSOCK(os.stat(observe.SOCKET, follow_symlinks=False).st_mode):
            _fail("Docker endpoint is not a local socket")
        connection = observe._UnixHTTPConnection("localhost", timeout=30)
        try:
            payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
            connection.request(method, endpoint, body=payload,
                               headers={} if payload is None else {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(observe._MAX_RESPONSE + 1)
            if response.status not in allowed or len(raw) > observe._MAX_RESPONSE:
                _fail("Docker state probe operation failed")
            if response.status == 404:
                return None
            return json.loads(raw) if raw else None
        finally:
            connection.close()
    except StateError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise StateError("Docker state probe operation failed") from None


def _daemon() -> str:
    info = _request("GET", "/info")
    if type(info) is not dict or type(info.get("ID")) is not str or not info["ID"]:
        _fail("Docker daemon identity is unavailable")
    options = info.get("SecurityOptions") or []
    if type(options) is not list or any(type(x) is not str for x in options):
        _fail("Docker daemon security options are invalid")
    if any(x.split(",", 1)[0] in ("name=userns", "name=rootless") for x in options):
        _fail("remapped or rootless Docker state access is unsupported")
    return hashlib.sha256(info["ID"].encode("utf-8")).hexdigest()


def _image(image_id: str, revision: str) -> dict[str, Any]:
    runtime._image_id(image_id, "state probe image")
    runtime._match(revision, runtime._HEX40, "state probe revision")
    image = _request("GET", "/images/" + quote(image_id, safe=":") + "/json")
    if (type(image) is not dict or image.get("Id") != image_id or type(image.get("Config")) is not dict
            or (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") != revision
            or image["Config"].get("Volumes") not in (None, {})):
        _fail("state probe image identity or automatic volumes differ")
    return image


def _effective_user(snapshot: dict[str, Any], image: dict[str, Any]) -> str:
    value = snapshot["user"] or image["Config"].get("User") or "0"
    if value == "0":
        value = "0:0"
    # Explicit UID:GID avoids resolving a user's default group in a different
    # image. Image-name users, supplementary groups and remapping are refused.
    if type(value) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", value):
        _fail("runtime user must have an explicit numeric UID and GID")
    if any(int(part) > 2**32 - 2 for part in value.split(":")):
        _fail("runtime user is out of range")
    options = snapshot["runtime_options"]
    if options["privileged"] or options["cap_add"] or options["cap_drop"] or options["security_opt"]:
        _fail("runtime capability settings cannot be faithfully probed")
    return value


def _mounts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for mount in snapshot["mounts"]:
        _safe_target(mount["container"])
        if "host" in mount:
            source = Path(mount["host"])
            # _directory walks the actual parent path without following links.
            with manifest._directory(source.parent, (), create=False):
                pass
            info = source.stat(follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                _fail("state bind source is not a physical file or directory")
            result.append({**mount, "device": info.st_dev, "inode": info.st_ino})
        else:
            result.append(dict(mount))
    return sorted(result, key=lambda item: item["container"])


def _safe_target(path: str) -> None:
    target = PurePosixPath(manifest._path(path, "probe mount target"))
    if any(target.is_relative_to(item) or item.is_relative_to(target) for item in _PROTECTED):
        _fail("state mount could shadow probe code or system files")


def _check_sources(record: dict[str, Any]) -> None:
    for mount in record["mounts"]:
        if "host" in mount:
            source = Path(mount["host"])
            with manifest._directory(source.parent, (), create=False):
                pass
            info = source.stat(follow_symlinks=False)
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (info.st_dev, info.st_ino) != (mount["device"], mount["inode"])):
                _fail("state bind identity changed")


def _namespace_mounts(record: dict[str, Any]) -> list[str]:
    # Tmpfs is visible but never an approved persistent state mapping.
    return [json.dumps({"container": m["container"], "host": m["container"],
                        "read_only": m.get("read_only", True)}, sort_keys=True)
            for m in record["mounts"]]


def _command(record: dict[str, Any]) -> list[str]:
    binds = json.dumps([[m["container"], m["device"], m["inode"]]
                        for m in record["mounts"] if "host" in m])
    if record["kind"] == "state-plan":
        return ["-I", "-B", "-c", _PLAN_CODE, record["input_path"],
                json.dumps(_namespace_mounts(record), sort_keys=True), record["user"], binds]
    if record["kind"] == "state-legacy-plan":
        return ["-I", "-B", "-c", _LEGACY_PLAN_CODE, record["input_path"],
                json.dumps(_namespace_mounts(record), sort_keys=True), record["user"], binds]
    if record["kind"] in ("state-canonical", "state-canonical-v4"):
        # Schema 5 reads the file a leaf names and hands the entry the native
        # writer replaces separately; FULL4 already records both endpoints.
        extra = (([record["canonical_writer"]] if record.get("canonical_writer") is not None else [])
                 if record["kind"] == "state-canonical" else
                 [record["canonical_request"]["writer_path"],
                  json.dumps(record["canonical_request"]["source_preimage"], sort_keys=True)])
        return ["-I", "-B", "-c", _CANONICAL_CODE, record["input_path"], record["user"], binds,
                record["flavor"], record["state_kind"], str(record["clock"]),
                "/run/wuwaterm-full-state-" + record["probe_id"], *extra]
    if record["kind"] == "state-endpoint":
        import component_state_endpoint as endpoint  # noqa: PLC0415
        return ["-I", "-B", "-S", "-c", endpoint.ENDPOINT_CODE,
                json.dumps(record["endpoint_request"], sort_keys=True, ensure_ascii=True)]
    if record["kind"] == "state-copy-v4":
        import component_state_copy_v4 as copy_v4  # noqa: PLC0415
        return ["-I", "-B", "-c", copy_v4.COPY_CODE,
                json.dumps(record["copy_request"], sort_keys=True, ensure_ascii=True)]
    if record["kind"] in ("state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard"):
        return ["-I", "-B", "-c", _COPY_CODE, record["input_path"], record["user"], binds,
                {"state-copy-stage": "stage", "state-copy-publish": "publish", "state-copy-clean": "cleanup",
                 "state-copy-discard": "discard"}[record["kind"]], record["flavor"],
                record["state_kind"], json.dumps(record["copy"], sort_keys=True)]
    return ["-I", "-B", "-S", "-c", _HASH_CODE, record["input_path"], record["user"], binds]


def _environment(image: dict[str, Any], record: dict[str, Any]) -> dict[str, str]:
    # Docker merges image defaults. Override all of them, so no image-provided
    # token or PYTHONPATH can enter this offline operation.
    result: dict[str, str] = {}
    image_uses_venv = False
    for item in image["Config"].get("Env") or []:
        if type(item) is not str or "=" not in item:
            _fail("image environment is invalid")
        name, value = item.split("=", 1)
        if not manifest._ENV_NAME.fullmatch(name) or name in result:
            _fail("image environment is ambiguous")
        if name == "PATH":
            image_uses_venv = value.split(":", 1)[0] == "/app/.venv/bin"
        result[name] = ""
    result.update(PATH=("/app/.venv/bin:" if image_uses_venv else "") + "/usr/local/bin:/usr/bin:/bin", LANG="C.UTF-8")
    result.update({key: value for key, value in record["path_settings"].items() if value is not None})
    return result


def _parse_env(value: Any) -> dict[str, str]:
    if type(value) is not list:
        _fail("state probe environment is invalid")
    result = {}
    for item in value:
        if type(item) is not str or "=" not in item:
            _fail("state probe environment is invalid")
        key, content = item.split("=", 1)
        if key in result:
            _fail("state probe environment is ambiguous")
        result[key] = content
    return result


def _validate_result(kind: str, value: Any, input_path: str) -> None:
    if kind == "state-endpoint":
        import component_state_endpoint as endpoint  # noqa: PLC0415
        endpoint.validate_endpoint(value)
        if value["effective_path"] != input_path:
            _fail("state endpoint path differs")
        return
    if kind == "state-copy-v4":
        import component_state_endpoint as endpoint  # noqa: PLC0415
        result = runtime._object(value, {"schema_version", "action", "binding_sha256", "reconciled", "endpoint"},
                                 "FULL4 state copy result")
        if (type(result["schema_version"]) is not int or result["schema_version"] != 1
                or result["action"] not in ("reserve", "write", "publish", "cleanup", "discard")
                or type(result["reconciled"]) is not bool):
            _fail("FULL4 state copy result is invalid")
        runtime._match(result["binding_sha256"], runtime._HEX64, "FULL4 state copy result binding")
        endpoint.validate_endpoint(result["endpoint"])
        return
    if kind in ("state-preimage", "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard"):
        runtime._file_preimage(value, "state probe result")
        return
    if kind in ("state-canonical", "state-canonical-v4"):
        runtime._object(value, {"raw_sha256", "canonical_sha256", "writer_can_replace"}, "canonical state result")
        if (value["raw_sha256"] is None) != (value["canonical_sha256"] is None):
            _fail("canonical state result has an incomplete hash")
        if value["raw_sha256"] is not None:
            runtime._match(value["raw_sha256"], runtime._HEX64, "canonical state raw hash")
            runtime._match(value["canonical_sha256"], runtime._HEX64, "canonical state hash")
        runtime._boolean(value["writer_can_replace"], "canonical state writer permission")
        return
    if (type(value) is not dict or set(value) != {"database_path", "kinds"}
            or value["database_path"] != input_path or type(value["kinds"]) is not dict
            or set(value["kinds"]) != {"chat-settings", "channel-replies"}):
        _fail("product state plan is invalid")
    for item in value["kinds"].values():
        if (type(item) is not dict or set(item) != {"effective_path", "legacy_path", "migration",
                                                   "mapped_target", "mapped_legacy"}
                or item["migration"] not in ("none", "validate-existing", "copy-if-absent")):
            _fail("product state plan is invalid")
        for key in ("effective_path", "legacy_path", "mapped_target"):
            manifest._path(item[key], "product state path")
        if item["mapped_legacy"] is not None:
            manifest._path(item["mapped_legacy"], "product legacy state path")


def read_state_probe(root: Path, name: str) -> dict[str, Any]:
    """Parse the complete durable state-probe intent without reading state."""
    try:
        value = runtime._decode(runtime._read_regular(root / ".deployments/component-adopt-probes" / name))
        keys = {"schema_version", "kind", "probe_id", "status", "phase", "transaction_id",
                "source_tool_sha256", "daemon_id", "image_id", "image_revision", "container_name",
                "container_id", "user", "working_dir", "mounts", "path_settings", "input_path",
                "command", "output_sha256", "result", "failure"}
        if type(value) is dict and type(value.get("schema_version")) is int and value["schema_version"] in (2, 3, 4, 5):
            keys |= {"transaction_kind", "flavor", "state_kind", "clock"}
            if value["schema_version"] == 3:
                keys.add("copy")
            if value["schema_version"] == 5:
                keys.add("canonical_writer")
            if value["schema_version"] == 4:
                keys.add("extra_read_binds")
                if value.get("kind") == "state-endpoint":
                    keys |= {"endpoint_request", "endpoint_purpose"}
                elif value.get("kind") == "state-copy-v4":
                    keys |= {"copy_request", "started"}
                elif value.get("kind") in ("state-plan", "state-legacy-plan"):
                    keys.add("plan_request")
                else:
                    keys.add("canonical_request")
        runtime._object(value, keys, "state probe")
        if type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2, 3, 4, 5):
            _fail("unsupported state probe schema")
        if value["schema_version"] == 5:
            # Schema-5 probes belong to schema-5 runtime or FULL journals and
            # are limited to the helpers whose programs do not write state.
            if value["kind"] not in SCHEMA5_HELPER_KINDS or value["transaction_kind"] not in ("runtime", "full"):
                _fail("schema-5 state probe kind or transaction is invalid")
            if value["kind"] == "state-canonical":
                if (value["flavor"] not in ("legacy", "wheel")
                        or value["state_kind"] not in ("chat-settings", "channel-replies")
                        or type(value["clock"]) is not int or not 0 <= value["clock"] <= 2**63 - 1):
                    _fail("schema-5 canonical probe context is invalid")
                if value["canonical_writer"] is not None:
                    manifest._path(value["canonical_writer"], "schema-5 canonical writer path")
            elif value["canonical_writer"] is not None or any(
                    value[key] is not None for key in ("flavor", "state_kind", "clock")):
                _fail("schema-5 state probe has unexpected context")
        if value["kind"] not in ("state-plan", "state-legacy-plan", "state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4",
                                  "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard"):
            _fail("unsupported state probe kind")
        if value["schema_version"] == 1 and value["kind"] in (
            "state-legacy-plan", "state-canonical", "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard",
        ):
            _fail("full-only state probe has an old schema")
        if value["schema_version"] == 3 and value["kind"] not in ("state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard"):
            _fail("state copy schema has an unsupported kind")
        v4_only = value["kind"] in ("state-endpoint", "state-canonical-v4", "state-copy-v4")
        if (v4_only and value["schema_version"] != 4
                or value["schema_version"] == 4 and value["kind"] not in (
                    "state-plan", "state-legacy-plan", "state-endpoint", "state-canonical-v4", "state-copy-v4")):
            _fail("FULL4 probe requires its own schema")
        if value["schema_version"] in (2, 3, 4):
            if value["transaction_kind"] != "full":
                _fail("state probe transaction kind is invalid")
            if value["schema_version"] == 4 and value["kind"] in ("state-plan", "state-legacy-plan"):
                request = runtime._object(value["plan_request"], {"recipe_sha256"}, "plan probe request")
                runtime._match(request["recipe_sha256"], runtime._HEX64, "plan recipe")
                if (value["extra_read_binds"] != [] or any(value[key] is not None for key in (
                        "flavor", "state_kind", "clock"))):
                    _fail("plan probe has unexpected context")
            elif value["kind"] == "state-copy-v4":
                request = runtime._object(value["copy_request"], {
                    "schema_version", "action", "phase", "binding", "binding_sha256", "source_endpoint",
                    "destination_before", "read_path", "mounts", "flavor", "reservation", "staged", "cleanup", "discard",
                    "reconcile_only",
                }, "FULL4 state copy request")
                if (type(value["started"]) is not bool or value["clock"] is not None
                        or value["flavor"] not in ("legacy", "wheel") or request["flavor"] != value["flavor"]
                        or value["state_kind"] not in ("chat-settings", "channel-replies")
                        or request["read_path"] != value["input_path"] or request["mounts"] != value["mounts"]
                        or type(request["schema_version"]) is not int or request["schema_version"] != 1
                        or request["action"] not in ("reserve", "write", "publish", "cleanup", "discard")
                        or type(request["reconcile_only"]) is not bool
                        or type(value["extra_read_binds"]) is not list or len(value["extra_read_binds"]) > 1):
                    _fail("FULL4 state copy request differs")
                runtime._match(request["binding_sha256"], runtime._HEX64, "FULL4 copy binding hash")
                if (value["phase"] == "start" or value["status"] == "completed") and not value["started"]:
                    _fail("FULL4 copy probe has no durable start intent")
            elif value["kind"] == "state-endpoint":
                request = runtime._object(value["endpoint_request"], {
                    "kind", "recipe_sha256", "image_id", "image_revision", "user", "working_dir",
                    "configured_path", "effective_path", "mounts", "require_writer",
                }, "endpoint probe request")
                if (value["flavor"] is not None or value["clock"] is not None
                        or value["state_kind"] not in ("chat-settings", "channel-replies")
                        or value["endpoint_purpose"] not in ("target", "legacy", "stage", "retained")
                        or value["extra_read_binds"] != [] or request["kind"] != value["state_kind"]
                        or request["image_id"] != value["image_id"] or request["image_revision"] != value["image_revision"]
                        or request["user"] != value["user"] or request["working_dir"] != value["working_dir"]
                        or request["effective_path"] != value["input_path"] or request["mounts"] != value["mounts"]
                        or type(request["require_writer"]) is not bool):
                    _fail("endpoint probe context differs")
                runtime._match(request["recipe_sha256"], runtime._HEX64, "endpoint recipe")
                if request["configured_path"] is not None:
                    manifest._recipe_environment_path(request["configured_path"], "endpoint configured path")
            elif value["kind"] in ("state-canonical", "state-canonical-v4"):
                if (value["flavor"] not in ("legacy", "wheel")
                        or value["state_kind"] not in ("chat-settings", "channel-replies")
                        or type(value["clock"]) is not int or not 0 <= value["clock"] <= 2**63 - 1):
                    _fail("canonical state probe context is invalid")
                if value["kind"] == "state-canonical-v4":
                    request = runtime._object(value["canonical_request"], {
                        "recipe_sha256", "source_endpoint_sha256", "writer_endpoint_sha256",
                        "source_path", "writer_path", "source_preimage",
                    }, "canonical probe request")
                    for key in ("recipe_sha256", "source_endpoint_sha256", "writer_endpoint_sha256"):
                        runtime._match(request[key], runtime._HEX64, "canonical probe binding")
                    if request["source_path"] != value["input_path"]:
                        _fail("canonical source path differs")
                    manifest._path(request["writer_path"], "canonical writer path")
                    preimage = runtime._object(request["source_preimage"], {"present", "stat", "sha256"},
                                               "canonical source preimage")
                    if type(preimage["present"]) is not bool:
                        _fail("canonical source presence differs")
                    if preimage["present"]:
                        import component_state_endpoint as endpoint  # noqa: PLC0415
                        endpoint._file_stat(preimage["stat"], "canonical source stat", mode_type="regular")
                        runtime._match(preimage["sha256"], runtime._HEX64, "canonical source hash")
                    elif preimage["stat"] is not None or preimage["sha256"] is not None:
                        _fail("absent canonical source has data")
                    if type(value["extra_read_binds"]) is not list or len(value["extra_read_binds"]) > 1:
                        _fail("canonical extra read bindings are invalid")
                    for extra in value["extra_read_binds"]:
                        runtime._object(extra, {"purpose", "endpoint_sha256", "host_parent", "parent_device",
                                                "parent_inode", "probe_alias", "read_only"}, "canonical extra bind")
                        if (extra["purpose"] != "canonical-source" or extra["read_only"] is not True
                                or extra["endpoint_sha256"] != request["source_endpoint_sha256"]):
                            _fail("canonical extra bind differs")
                        manifest._path(extra["host_parent"], "canonical extra host")
                        manifest._path(extra["probe_alias"], "canonical extra alias")
                        runtime._integer(extra["parent_device"], "canonical extra device")
                        runtime._integer(extra["parent_inode"], "canonical extra inode", minimum=1)
            elif value["kind"] in ("state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard"):
                if (value["schema_version"] != 3 or value["flavor"] not in ("legacy", "wheel")
                        or value["state_kind"] not in ("chat-settings", "channel-replies") or value["clock"] is not None):
                    _fail("state copy context is invalid")
                copy = runtime._object(value["copy"], {"direction", "source", "destination", "stage",
                                                       "source_before", "destination_before", "stage_before"}, "state copy")
                if copy["direction"] not in ("forward", "rollback") or copy["source"] != value["input_path"]:
                    _fail("state copy direction or source differs")
                for key in ("source", "destination", "stage"):
                    manifest._path(copy[key], f"state copy.{key}")
                if (Path(copy["stage"]).parent != Path(copy["destination"]).parent
                        or len({copy["source"], copy["destination"], copy["stage"]}) != 3):
                    _fail("state copy paths are inconsistent")
                runtime._file_preimage(copy["source_before"], "state copy source")
                runtime._file_preimage(copy["destination_before"], "state copy destination")
                if not copy["source_before"]["present"]:
                    _fail("state copy source is absent")
                if copy["direction"] == "forward" and copy["destination_before"]["present"]:
                    _fail("forward state copy destination is not absent")
                if value["kind"] == "state-copy-stage" and copy["stage_before"] is not None:
                    _fail("state stage has an unexpected preimage")
                if value["kind"] == "state-copy-publish":
                    runtime._file_preimage(copy["stage_before"], "state copy stage")
                    if not copy["stage_before"]["present"]:
                        _fail("state copy stage is absent")
                if value["kind"] == "state-copy-clean":
                    if copy["direction"] != "rollback" or not copy["destination_before"]["present"]:
                        _fail("state cleanup lacks a restored destination")
                    if copy["stage_before"] is not None:
                        runtime._file_preimage(copy["stage_before"], "state copy stage")
                if value["kind"] == "state-copy-discard":
                    if copy["direction"] != "forward" or copy["destination_before"]["present"]:
                        _fail("state stage discard has an invalid destination")
                    if copy["stage_before"] is not None:
                        runtime._file_preimage(copy["stage_before"], "state copy stage")
            elif any(value[key] is not None for key in ("flavor", "state_kind", "clock")):
                _fail("noncanonical state probe has extra context")
        ident = runtime._match(value["probe_id"], runtime._DEPLOYMENT_ID, "probe ID")
        runtime._match(value["transaction_id"], runtime._DEPLOYMENT_ID, "transaction ID")
        for key in ("source_tool_sha256", "daemon_id"):
            runtime._match(value[key], runtime._HEX64, key)
        runtime._image_id(value["image_id"], "probe image")
        runtime._match(value["image_revision"], runtime._HEX40, "probe revision")
        if name != ident + ".json" or value["container_name"] != "wuwaterm-state-probe-" + ident:
            _fail("state probe name differs")
        if value["container_id"] is not None:
            runtime._match(value["container_id"], runtime._HEX64, "probe container ID")
        if (type(value["user"]) is not str
                or not re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", value["user"])
                or any(int(x) > 2**32 - 2 for x in value["user"].split(":"))):
            _fail("invalid state probe user")
        manifest._path(value["working_dir"], "probe working directory")
        manifest._path(value["input_path"], "probe input path")
        settings = runtime._object(value["path_settings"], set(_PATH_KEYS), "probe path settings")
        for key, path in settings.items():
            if path is not None:
                manifest._path(path, key)
        if value["kind"] in ("state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4", "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard") and any(v is not None for v in settings.values()):
            _fail("state preimage has unexpected path settings")
        if type(value["mounts"]) is not list or not value["mounts"]:
            _fail("state probe mounts are invalid")
        destinations = set()
        for item in value["mounts"]:
            if type(item) is not dict:
                _fail("state probe mount is invalid")
            if set(item) == {"host", "container", "read_only", "device", "inode"}:
                manifest._path(item["host"], "probe mount source")
                runtime._boolean(item["read_only"], "probe mount read-only")
                runtime._integer(item["device"], "probe mount device")
                runtime._integer(item["inode"], "probe mount inode", minimum=1)
            elif set(item) == {"type", "container", "options"} and item["type"] == "tmpfs":
                manifest._tmpfs_options(item["options"], "probe tmpfs")
            else:
                _fail("state probe mount is unsupported")
            dest = manifest._path(item["container"], "probe mount target")
            _safe_target(dest)
            if dest in destinations:
                _fail("state probe mount target is duplicated")
            destinations.add(dest)
        if value["mounts"] != sorted(value["mounts"], key=lambda x: x["container"]):
            _fail("state probe mounts are not canonical")
        if value["kind"] in ("state-canonical", "state-canonical-v4"):
            uid, gid = value["user"].split(":")
            scratch = "/run/wuwaterm-full-state-" + ident
            expected = {"type": "tmpfs", "container": scratch,
                        "options": ["rw", "noexec", "nosuid", "nodev", "uid=" + uid,
                                    "gid=" + gid, "mode=0700"]}
            if expected not in value["mounts"]:
                _fail("canonical state probe scratch binding differs")
            for mount in value["mounts"]:
                if mount == expected:
                    continue
                existing, candidate = PurePosixPath(mount["container"]), PurePosixPath(scratch)
                if existing.is_relative_to(candidate) or candidate.is_relative_to(existing):
                    _fail("canonical scratch overlaps another mount")
        if value["command"] != _command(value):
            _fail("state probe command differs")
        if value["status"] not in ("active", "completed", "failed") or value["phase"] not in (
                "prepared", "create", "start", "cleanup", "completed", "failed"):
            _fail("state probe status is invalid")
        if ((value["status"] == "completed") != (value["phase"] == "completed")
                or (value["status"] == "failed") != (value["phase"] == "failed")):
            _fail("state probe phase is inconsistent")
        if value["phase"] == "start" and value["container_id"] is None:
            _fail("state probe start has no container ID")
        if value["output_sha256"] is not None:
            runtime._match(value["output_sha256"], runtime._HEX64, "probe output hash")
        if (value["result"] is None) != (value["output_sha256"] is None):
            _fail("state probe result and digest disagree")
        if value["result"] is not None:
            _validate_result(value["kind"], value["result"], value["input_path"])
            if value["kind"] == "state-endpoint" and any(value["result"][field] != value["endpoint_request"][field]
                    for field in ("kind", "recipe_sha256", "image_id", "image_revision", "user", "working_dir",
                                  "configured_path", "effective_path")):
                _fail("state endpoint result differs from its request")
            if value["kind"] == "state-copy-v4" and (value["result"]["action"] != value["copy_request"]["action"]
                    or value["result"]["binding_sha256"] != value["copy_request"]["binding_sha256"]):
                _fail("FULL4 copy result differs from its request")
            raw = (json.dumps(value["result"], sort_keys=True) + "\n").encode()
            if hashlib.sha256(raw).hexdigest() != value["output_sha256"]:
                _fail("state probe result digest differs")
        if value["status"] == "completed" and (value["container_id"] is None or value["result"] is None):
            _fail("completed state probe has no result")
        if value["failure"] is not None and (type(value["failure"]) is not str
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", value["failure"])):
            _fail("invalid state probe failure")
        if value["status"] == "failed" and value["failure"] is None:
            _fail("failed state probe has no failure")
        return value
    except (runtime.JournalError, manifest.ManifestError, OSError, ValueError, TypeError, KeyError):
        raise StateError("invalid state probe record") from None


def _record(root: Path, record: dict[str, Any], *, new: bool = False) -> None:
    runtime._write_adopt_probe(root, record, new=new)
    if read_state_probe(root, record["probe_id"] + ".json") != record:
        _fail("state probe record readback differs")


def _admit(root: Path, transaction_id: str, source: str, probe_id: str | None = None,
           *, transaction_kind: str = "runtime") -> dict[str, Any]:
    return admission.admit_state_probe(root, transaction_id=transaction_id, source_tool_sha256=source,
                                       code_root=CODE_ROOT, probe_id=probe_id, transaction_kind=transaction_kind)


def _owned(record: dict[str, Any]) -> dict[str, Any] | None:
    if _daemon() != record["daemon_id"]:
        _fail("Docker daemon changed")
    _check_sources(record)
    image = _image(record["image_id"], record["image_revision"])
    name = quote(record["container_name"], safe="")
    probe = _request("GET", "/containers/" + name + "/json", allowed=(200, 404))
    if probe is None:
        if record["container_id"] is not None and _request(
                "GET", "/containers/" + record["container_id"] + "/json", allowed=(200, 404)) is not None:
            _fail("recorded state probe was renamed")
        return None
    if type(probe) is not dict:
        _fail("state probe inspection is invalid")
    try:
        config, host = probe["Config"], probe["HostConfig"]
        expected_mounts = [{k: v for k, v in item.items() if k not in ("device", "inode")}
                           for item in record["mounts"]]
        health = config.get("Healthcheck")
        if (probe["Name"] != "/" + record["container_name"] or probe["Image"] != record["image_id"]
                or record["container_id"] is not None and probe["Id"] != record["container_id"]
                or (config.get("Labels") or {}).get(_LABEL) != record["probe_id"]
                or config.get("User") != record["user"] or config.get("WorkingDir") != record["working_dir"]
                or config.get("Entrypoint") != ["python"] or config.get("Cmd") != record["command"]
                or _parse_env(config.get("Env")) != _environment(image, record)
                or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                or host.get("Privileged") is not False or host.get("CapAdd") not in (None, [])
                or host.get("CapDrop") not in (None, []) or host.get("GroupAdd") not in (None, [])
                or host.get("PidMode") not in (None, "") or host.get("UsernsMode") not in (None, "")
                or host.get("SecurityOpt") not in (None, []) or host.get("AutoRemove") is not False
                or health is not None and health.get("Test") != ["NONE"]
                or observe._mounts(probe.get("Mounts"), host) != expected_mounts):
            _fail("state probe does not match its ownership and isolation")
        runtime._match(probe["Id"], runtime._HEX64, "probe ID")
        return probe
    except (KeyError, TypeError, ValueError, observe.ObservationError):
        raise StateError("state probe cannot be inspected safely") from None


def reconcile_state_probe(root: Path, record: dict[str, Any]) -> None:
    """Remove only the matching stopped probe, recording and checking removal."""
    root = admission._physical_root(Path(root))
    name = record["probe_id"] + ".json"
    if read_state_probe(root, name) != record or record["status"] != "active":
        _fail("state probe recovery record changed")
    transaction_kind, _ = _select_journal(root, record["transaction_id"])
    _admit(root, record["transaction_id"], record["source_tool_sha256"], record["probe_id"],
           transaction_kind=transaction_kind)
    record["phase"] = "cleanup"
    _record(root, record)
    probe = _owned(record)
    if probe is not None:
        state = probe["State"]
        if (state.get("Running") is not False or state.get("Status") not in ("created", "exited")
                or state.get("Dead") or state.get("Restarting")):
            _fail("state probe is still running or its state is unknown")
        if record["container_id"] is None:
            record["container_id"] = probe["Id"]
            _record(root, record)
        _admit(root, record["transaction_id"], record["source_tool_sha256"], record["probe_id"],
               transaction_kind=transaction_kind)
        _request("DELETE", "/containers/" + record["container_id"] + "?force=0&v=0", allowed=(204,))
        if _owned(record) is not None:
            _fail("state probe remains after removal")


def reconcile_transaction(root: Path, transaction_id: str, source_tool_sha256: str,
                          *, transaction_kind: str = "runtime") -> None:
    """Cancel a single interrupted state probe of the verified transaction."""
    try:
        root = admission._physical_root(Path(root))
        directory = root / ".deployments/component-adopt-probes"
        active = []
        if directory.exists():
            if not stat.S_ISDIR(directory.lstat().st_mode):
                _fail("state probe registry is not a directory")
            for entry in directory.iterdir():
                record = runtime._read_adopt_probe(root, entry.name)
                if record["status"] == "active":
                    if (record["kind"] not in ("state-plan", "state-legacy-plan", "state-preimage", "state-canonical", "state-canonical-v4", "state-endpoint", "state-copy-v4",
                                                "state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard")
                            or record["transaction_id"] != transaction_id
                            or record["source_tool_sha256"] != source_tool_sha256):
                        _fail("another probe prevents state recovery")
                    active.append(record)
        if len(active) > 1:
            _fail("multiple active state probes require inspection")
        kind, journal = _select_journal(root, transaction_id)
        if kind != transaction_kind:
            _fail("state probe transaction kind differs")
        if not active and journal["status"] == "rolled_back":
            admitted = (admission.admit_full_recovery(root, transaction_id=transaction_id,
                                                       source_tool_sha256=source_tool_sha256, code_root=CODE_ROOT)
                        if kind == "full" else
                        admission.admit_component_recovery(root, transaction_id=transaction_id,
                                                            source_tool_sha256=source_tool_sha256, code_root=CODE_ROOT))
            telegram_present = (admitted["before"]["topology"]["telegram"] is not None if kind == "full"
                                else "telegram" in admitted["selection"])
            if admitted["action"] != "update" or not telegram_present:
                _fail("terminal transaction does not match Telegram recovery")
            return
        _admit(root, transaction_id, source_tool_sha256, active[0]["probe_id"] if active else None,
               transaction_kind=transaction_kind)
        if active:
            record = active[0]
            record["failure"] = record["failure"] or "Interrupted"
            _record(root, record)
            reconcile_state_probe(root, record)
            record.update(status="failed", phase="failed")
            _record(root, record)
    except (runtime.JournalError, admission.AdmissionError, OSError, ValueError, KeyError, TypeError):
        raise StateError("state transaction probe recovery was refused") from None


def _capture(command: list[str], root: Path, identifier: str) -> bytes:
    """Bound CLI output and retain FD9 until the Docker writer has exited."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "DOCKER_CONFIG": str(root / ".deployments" / ("state-probe-config-" + identifier))}
    proc = subprocess.Popen(["docker", "--host", "unix://" + observe.SOCKET, *command],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(9,))
    assert proc.stdout is not None and proc.stderr is not None
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in buffers:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + 60
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("state probe timed out")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = buffers[key.fileobj]
                target.extend(chunk)
                if len(target) > _MAX_OUTPUT:
                    _fail("state probe output exceeded its limit")
        result = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        if result:
            _fail("state probe operation refused the binding")
        return bytes(buffers[proc.stdout])
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _execute(root: Path, journal: dict[str, Any], *, kind: str, image_id: str, revision: str,
             user: str, working_dir: str, mounts: list[dict[str, Any]],
             settings: dict[str, str | None], input_path: str, flavor: str | None = None,
             state_kind: str | None = None, clock: int | None = None,
             copy_request: dict[str, Any] | None = None, endpoint_request: dict[str, Any] | None = None,
             endpoint_purpose: str | None = None, canonical_request: dict[str, Any] | None = None,
             canonical_writer: str | None = None,
             extra_read_binds: list[dict[str, Any]] | None = None, plan_request: dict[str, Any] | None = None,
             copy_v4_request: dict[str, Any] | None = None) -> Any:
    transaction_id = journal["transaction_id"]
    source = journal["source"]["source_tool_sha256"]
    transaction_kind = "full" if journal["kind"] == "component-full" else "runtime"
    if journal["schema_version"] == 5 and kind not in SCHEMA5_HELPER_KINDS:
        _fail("schema-5 transactions permit only non-mutating state helpers")
    _admit(root, transaction_id, source, transaction_kind=transaction_kind)
    image = _image(image_id, revision)
    ident = manifest.new_deployment_id()
    schema5 = journal["schema_version"] == 5
    if kind in ("state-canonical", "state-canonical-v4"):
        # A schema-5 runtime record may read state through its selected
        # Telegram image; FULL4 keeps its own endpoint-owned comparison.
        runtime_reader = schema5 and kind == "state-canonical" and transaction_kind == "runtime"
        if (transaction_kind != "full" and not runtime_reader) or flavor not in ("legacy", "wheel") or state_kind not in (
            "chat-settings", "channel-replies",
        ) or type(clock) is not int or not 0 <= clock <= 2**63 - 1:
            _fail("canonical state probe context is invalid")
        scratch = "/run/wuwaterm-full-state-" + ident
        for mount in mounts:
            existing = PurePosixPath(mount["container"])
            candidate = PurePosixPath(scratch)
            if candidate.is_relative_to(existing) or existing.is_relative_to(candidate):
                _fail("canonical scratch conflicts with a recorded mount")
        uid, gid = user.split(":")
        mounts = sorted([*mounts, {"type": "tmpfs", "container": scratch,
                                  "options": ["rw", "noexec", "nosuid", "nodev",
                                              "uid=" + uid, "gid=" + gid, "mode=0700"]}],
                        key=lambda item: item["container"])
    copy_kind = kind in ("state-copy-stage", "state-copy-publish", "state-copy-clean", "state-copy-discard")
    if copy_kind and (transaction_kind != "full" or flavor not in ("legacy", "wheel")
                      or state_kind not in ("chat-settings", "channel-replies") or copy_request is None):
        _fail("state copy probe context is invalid")
    endpoint_kind = kind == "state-endpoint"
    canonical_kind = kind == "state-canonical-v4"
    copy_kind_v4 = kind == "state-copy-v4"
    plan_kind_v4 = kind in ("state-plan", "state-legacy-plan") and transaction_kind == "full" and journal["schema_version"] == 4
    if endpoint_kind and (transaction_kind != "full" or journal["schema_version"] != 4
                          or endpoint_request is None or endpoint_purpose not in ("target", "legacy", "stage", "retained")):
        _fail("state endpoint probe context is invalid")
    if canonical_kind and (transaction_kind != "full" or journal["schema_version"] != 4
                           or canonical_request is None or extra_read_binds is None):
        _fail("canonical endpoint probe context is invalid")
    if plan_kind_v4 and plan_request is None:
        _fail("FULL4 state plan has no recipe binding")
    if copy_kind_v4 and (transaction_kind != "full" or journal["schema_version"] != 4
                         or copy_v4_request is None or extra_read_binds is None):
        _fail("FULL4 state copy probe context is invalid")
    record = {"schema_version": 5 if schema5 else 4 if endpoint_kind or canonical_kind or plan_kind_v4 or copy_kind_v4 else 3 if copy_kind else 2 if transaction_kind == "full" else 1,
              "kind": kind, "probe_id": ident, "status": "active", "phase": "prepared",
              "transaction_id": transaction_id, "source_tool_sha256": source, "daemon_id": _daemon(),
              "image_id": image_id, "image_revision": revision, "container_name": "wuwaterm-state-probe-" + ident,
              "container_id": None, "user": user, "working_dir": working_dir, "mounts": mounts,
              "path_settings": settings, "input_path": input_path, "command": None,
              "output_sha256": None, "result": None, "failure": None}
    if transaction_kind == "full" or schema5:
        record.update(transaction_kind=transaction_kind, flavor=flavor, state_kind=state_kind, clock=clock)
    if schema5:
        record["canonical_writer"] = canonical_writer
    if copy_kind:
        record["copy"] = copy_request
    if endpoint_kind:
        record.update(endpoint_request=endpoint_request, endpoint_purpose=endpoint_purpose, extra_read_binds=[])
    if canonical_kind:
        record.update(canonical_request=canonical_request, extra_read_binds=extra_read_binds)
    if plan_kind_v4:
        record.update(plan_request=plan_request, extra_read_binds=[])
    if copy_kind_v4:
        record.update(copy_request=copy_v4_request, extra_read_binds=extra_read_binds, started=False)
    record["command"] = _command(record)
    _record(root, record, new=True)
    try:
        record["phase"] = "create"
        _record(root, record)
        _admit(root, transaction_id, source, ident, transaction_kind=transaction_kind)
        _check_sources(record)
        binds = [{"Type": "bind", "Source": m["host"], "Target": m["container"], "ReadOnly": m["read_only"]}
                 for m in mounts if "host" in m]
        tmpfs = {m["container"]: ",".join(m["options"]) for m in mounts if m.get("type") == "tmpfs"}
        body = {"Image": image_id, "User": user, "WorkingDir": working_dir, "Entrypoint": ["python"],
                "Cmd": record["command"], "Env": [k + "=" + v for k, v in _environment(image, record).items()],
                "Labels": {_LABEL: ident, admission.ROOT_LABEL: admission.root_label_value(root)},
                "Healthcheck": {"Test": ["NONE"]},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Mounts": binds, "Tmpfs": tmpfs}}
        created = _request("POST", "/containers/create?" + urlencode({"name": record["container_name"]}), body,
                           allowed=(201,))
        if type(created) is not dict:
            _fail("Docker did not identify the state probe")
        record["container_id"] = runtime._match(created.get("Id"), runtime._HEX64, "state probe ID")
        _record(root, record)
        current = _owned(record)
        if current is None or current["State"].get("Status") != "created":
            _fail("new state probe is not stopped")
        record["phase"] = "start"
        if copy_kind_v4:
            record["started"] = True
        _record(root, record)
        _admit(root, transaction_id, source, ident, transaction_kind=transaction_kind)
        output = _capture(["start", "--attach", record["container_id"]], root, ident)
        current = _owned(record)
        if (current is None or current["State"].get("Running") is not False
                or current["State"].get("Status") != "exited" or current["State"].get("ExitCode") != 0):
            _fail("state probe did not exit successfully")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise StateError("state probe output is invalid") from None
        _validate_result(kind, result, input_path)
        record["output_sha256"] = hashlib.sha256(output).hexdigest()
        record["result"] = result
        _record(root, record)
        reconcile_state_probe(root, record)
        record.update(status="completed", phase="completed")
        _record(root, record)
        return result
    except Exception as exc:
        record["failure"] = type(exc).__name__
        try:
            _record(root, record)
            reconcile_state_probe(root, record)
            record.update(status="failed", phase="failed")
            _record(root, record)
        except Exception:
            raise StateError("state probe failed and cleanup remains unresolved") from None
        raise StateError("state probe failed") from None


def _select_journal(root: Path, transaction_id: str) -> tuple[str, dict[str, Any]]:
    """Select one matching runtime or FULL3 journal, never an ambiguous ID."""
    import component_full as full  # noqa: PLC0415

    matches = []
    for kind, name, loader in (("runtime", runtime.JOURNAL_NAME, runtime.load_journal),
                               ("full", full.JOURNAL_NAME, full.load_journal)):
        path = root / ".deployments" / name
        if admission._present(path):
            record = loader(path)
            if record["transaction_id"] == transaction_id:
                matches.append((kind, record))
    if len(matches) != 1:
        _fail("state probe has no unique matching transaction")
    return matches[0]


def _journal_snapshot(root: Path, transaction_id: str, snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    root = admission._physical_root(root)
    manifest.validate_recipe_snapshot(snapshot)
    kind, journal = _select_journal(root, transaction_id)
    telegram_present = (journal["before"]["topology"]["telegram"] is not None if kind == "full"
                        else "telegram" in journal["selection"])
    if journal["transaction_id"] != transaction_id or not telegram_present:
        _fail("state probe does not match the Telegram transaction")
    _admit(root, transaction_id, journal["source"]["source_tool_sha256"], transaction_kind=kind)
    components = [journal["before"]["topology"]["telegram"], journal["target"]["manifest"]["components"]["telegram"]]
    for component in components:
        if (component is not None and manifest.read_recipe_snapshot(root, component["recipe"]["sha256"]) == snapshot
                and snapshot["image_id"] == component["image"]["id"]
                and all(snapshot[key] == component["recipe"][key] for key in manifest._RECIPE_FIELDS)):
            return journal, component
    _fail("state probe recipe is not bound to the transaction")


def verify_plan_probe_binding(root: Path, journal: dict[str, Any], probe: dict[str, Any]) -> None:
    """Bind a FULL4 native plan to the exact recorded image, recipe and environment."""
    import component_recovery as recovery  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    if (journal["kind"] != "component-full" or journal["schema_version"] != 4
            or journal["phase"] not in ("state-plan", "state-snapshot")):
        _fail("FULL4 native plan is outside its authorized phase")
    digest = probe["plan_request"]["recipe_sha256"]
    components = (journal["before"]["topology"]["telegram"],
                  journal["target"]["manifest"]["components"]["telegram"])
    matches = [component for component in components if component is not None
               and component["recipe"]["sha256"] == digest and component["image"]["id"] == probe["image_id"]
               and component["image"]["revision"] == probe["image_revision"]]
    if not matches or len({component["provenance"]["kind"] for component in matches}) != 1:
        _fail("native plan image or recipe differs")
    component = matches[0]
    legacy = component["provenance"]["kind"] == "legacy-image"
    if (not legacy and component["provenance"]["kind"] != "wheel-image"
            or probe["kind"] != ("state-legacy-plan" if legacy else "state-plan")):
        _fail("native plan image flavor differs")
    if not legacy:
        receipt = wheel.verify_registered_wheel_build(root, component["provenance"]["build_receipt_sha256"])
        if (receipt["image_id"] != probe["image_id"] or receipt["image_revision"] != probe["image_revision"]):
            _fail("native plan receipt differs")
    snapshot = manifest.read_recipe_snapshot(root, digest)
    image = _image(probe["image_id"], probe["image_revision"])
    mounts = _mounts(snapshot)
    if legacy:
        mounts = [{**mount, "read_only": True} if "host" in mount else mount for mount in mounts]
    compose = recovery._verify_bound_compose(root, journal["source"])
    environment = transaction._effective_environment(snapshot, image, compose, root)
    settings, _ = observe._environment([key + "=" + value for key, value in environment.items()],
                                       snapshot["working_dir"], os.urandom(32))
    db_path = settings["WUWATERM_DB_PATH"]
    if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
        db_path = snapshot["command"][1]
    if (probe["mounts"] != mounts or probe["user"] != _effective_user(snapshot, image)
            or probe["working_dir"] != (snapshot["working_dir"] or "/")
            or probe["path_settings"] != {key: settings[key] for key in _PATH_KEYS} or probe["input_path"] != db_path
            or observe.resolve_bind_path(snapshot, db_path, writable=False) != journal["database"]["old"]["binding"]["path"]):
        _fail("native plan identity, mounts or environment differs")


def verify_schema5_probe_binding(journal: dict[str, Any], probe: dict[str, Any]) -> None:
    """Bind a schema-5 read-only helper to the journal's own recorded reader.

    Schema 5 makes no endpoint or copy claim, so a helper may only run through
    an image the transaction already recorded; a canonical comparison must also
    name that image's own reader flavor. A runtime record only re-reads state
    through its old container's image, never through the image it is installing.
    """
    if journal["schema_version"] != 5 or probe["schema_version"] != 5:
        _fail("schema-5 state probe requires a schema-5 journal")
    if probe["kind"] not in SCHEMA5_HELPER_KINDS:
        _fail("schema-5 state probe kind is not a read-only helper")
    if probe["transaction_kind"] == "runtime":
        if journal["kind"] != "component-runtime" or "telegram" not in journal["selection"]:
            _fail("schema-5 runtime state probe has no selected Telegram component")
    elif journal["kind"] != "component-full":
        _fail("schema-5 full state probe requires a full journal")
    components = (journal["before"]["topology"]["telegram"],
                  journal["target"]["manifest"]["components"]["telegram"])
    owner = components if probe["kind"] != "state-canonical" or probe["transaction_kind"] == "full" else components[:1]
    matches = [component for component in owner if component is not None
               and component["image"]["id"] == probe["image_id"]
               and component["image"]["revision"] == probe["image_revision"]]
    if not matches:
        _fail("schema-5 state probe image is not recorded")
    if probe["kind"] != "state-canonical":
        return
    provenance = matches[0]["provenance"]["kind"]
    if provenance not in ("legacy-image", "wheel-image", "completed-artifact"):
        _fail("schema-5 canonical probe has no supported native reader")
    if probe["flavor"] != ("legacy" if provenance == "legacy-image" else "wheel"):
        _fail("schema-5 canonical probe reader flavor differs")


def verify_endpoint_probe_binding(root: Path, journal: dict[str, Any], probe: dict[str, Any]) -> None:
    """Bind a FULL4 endpoint request to one saved recipe and durable native plan."""
    import component_recovery as recovery  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415
    import component_state_endpoint as endpoint  # noqa: PLC0415

    if journal["kind"] != "component-full" or journal["schema_version"] != 4:
        _fail("endpoint probe requires a FULL4 journal")
    request, purpose = probe["endpoint_request"], probe["endpoint_purpose"]
    if journal["phase"] not in ("state-plan", "state-snapshot", "state-migrate", "rollback-stop-readers",
                                 "rollback-copyback", "actual-health-bindings"):
        _fail("endpoint probe is outside a state observation phase")
    components = (journal["before"]["topology"]["telegram"],
                  journal["target"]["manifest"]["components"]["telegram"])
    matches = [component for component in components if component is not None
               and component["recipe"]["sha256"] == request["recipe_sha256"]
               and component["image"]["id"] == probe["image_id"]
               and component["image"]["revision"] == probe["image_revision"]]
    if not matches:
        _fail("endpoint probe recipe or image is not in the transaction")
    snapshot = manifest.read_recipe_snapshot(root, request["recipe_sha256"])
    image = _image(probe["image_id"], probe["image_revision"])
    if (probe["mounts"] != _mounts(snapshot) or probe["user"] != _effective_user(snapshot, image)
            or probe["working_dir"] != (snapshot["working_dir"] or "/")):
        _fail("endpoint probe identity differs from its recipe")
    compose = recovery._verify_bound_compose(root, journal["source"])
    environment = transaction._effective_environment(snapshot, image, compose, root)
    settings, _ = observe._environment([key + "=" + value for key, value in environment.items()],
                                       snapshot["working_dir"], os.urandom(32))
    variable = ("WUWATERM_SETTINGS_PATH" if probe["state_kind"] == "chat-settings" else
                "WUWATERM_CHANNEL_REPLY_INDEX_PATH")
    configured = environment.get(variable) or None if purpose == "target" else None
    if request["configured_path"] != configured:
        _fail("endpoint configured path differs from the saved environment")
    if request["require_writer"] != (purpose in ("target", "stage")):
        _fail("endpoint writer requirement differs from its purpose")
    endpoint.map_entry(snapshot, request["effective_path"], writable=request["require_writer"])
    allowed = False
    if purpose in ("target", "legacy"):
        expected_settings = {key: settings[key] for key in _PATH_KEYS}
        db_path = settings["WUWATERM_DB_PATH"]
        if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
            db_path = snapshot["command"][1]
        directory = root / ".deployments/component-adopt-probes"
        for entry in directory.iterdir():
            candidate = runtime._read_adopt_probe(root, entry.name)
            if candidate["kind"] not in ("state-plan", "state-legacy-plan") or candidate["status"] != "completed":
                continue
            mounts = ([{**mount, "read_only": True} if "host" in mount else mount for mount in probe["mounts"]]
                      if candidate["kind"] == "state-legacy-plan" else probe["mounts"])
            if (candidate["transaction_id"] != journal["transaction_id"]
                    or candidate["schema_version"] != 4 or candidate["plan_request"]["recipe_sha256"] != request["recipe_sha256"]
                    or candidate["source_tool_sha256"] != journal["source"]["source_tool_sha256"]
                    or candidate["daemon_id"] != probe["daemon_id"] or candidate["image_id"] != probe["image_id"]
                    or candidate["image_revision"] != probe["image_revision"] or candidate["user"] != probe["user"]
                    or candidate["working_dir"] != probe["working_dir"] or candidate["mounts"] != mounts
                    or candidate["path_settings"] != expected_settings or candidate["input_path"] != db_path):
                continue
            item = candidate["result"]["kinds"][probe["state_kind"]]
            if purpose == "target" and item["effective_path"] == request["effective_path"]:
                allowed = True
            if (purpose == "legacy" and item["mapped_legacy"] is not None
                    and item["legacy_path"] == request["effective_path"]):
                allowed = True
    elif purpose == "stage":
        for item in journal["migrations"]["items"]:
            if item["kind"] != probe["state_kind"]:
                continue
            for direction, field in (("forward", "quiescent_new_endpoint"),
                                     ("rollback", "quiescent_old_endpoint")):
                progress, destination = item[direction], item[field]
                if progress is None or destination is None:
                    continue
                binding = progress["binding"]
                stage = str(PurePosixPath(destination["writer"]["container_path"]).parent /
                            Path(binding["stage_path"]).name)
                if (stage == request["effective_path"] and binding["recipe_sha256"] == request["recipe_sha256"]
                        and binding["image_id"] == probe["image_id"] and binding["user"] == probe["user"]):
                    allowed = True
    else:
        for item in journal["migrations"]["items"]:
            original = item["old_endpoint"]
            if (item["kind"] != "channel-replies" or probe["state_kind"] != "channel-replies"
                    or original["entry"]["type"] != "symlink"
                    or original["recipe_sha256"] != request["recipe_sha256"]
                    or original["image_id"] != probe["image_id"]):
                continue
            paths = [link["container_path"] for link in original["links"][1:]]
            paths.append(original["referent"]["container_path"])
            if request["effective_path"] in paths:
                allowed = True
    if not allowed:
        _fail("endpoint path is not authorized by a durable state plan or copy claim")


def endpoint(root: Path, *, snapshot: dict[str, Any], environment: dict[str, str], state_kind: str,
             effective_path: str, purpose: str, transaction_id: str) -> dict[str, Any]:
    """Observe a FULL4 endpoint inside its exact image and namespace."""
    try:
        journal, component = _journal_snapshot(root, transaction_id, snapshot)
        if (journal["kind"] != "component-full" or journal["schema_version"] != 4
                or state_kind not in ("chat-settings", "channel-replies")
                or purpose not in ("target", "legacy", "stage", "retained")):
            _fail("state endpoint request is invalid")
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        image = _image(image_id, revision)
        user, mounts = _effective_user(snapshot, image), _mounts(snapshot)
        variable = "WUWATERM_SETTINGS_PATH" if state_kind == "chat-settings" else "WUWATERM_CHANNEL_REPLY_INDEX_PATH"
        configured = environment.get(variable) or None if purpose == "target" else None
        request = {"kind": state_kind, "recipe_sha256": component["recipe"]["sha256"],
                   "image_id": image_id, "image_revision": revision, "user": user,
                   "working_dir": snapshot["working_dir"] or "/", "configured_path": configured,
                   "effective_path": effective_path, "mounts": mounts, "require_writer": purpose in ("target", "stage")}
        return _execute(root, journal, kind="state-endpoint", image_id=image_id, revision=revision,
                        user=user, working_dir=request["working_dir"], mounts=mounts,
                        settings=dict.fromkeys(_PATH_KEYS), input_path=effective_path,
                        state_kind=state_kind, endpoint_request=request, endpoint_purpose=purpose)
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("state endpoint could not establish the recorded binding") from None


def _canonical_mounts(snapshot: dict[str, Any], transaction_id: str,
                      source: dict[str, Any], *, force_alias: bool = False,
                      exact_source_mapping: bool = False) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose a recorded referent through its recipe or one bound read-only alias."""
    import component_full as full  # noqa: PLC0415
    import component_state_endpoint as endpoint_module  # noqa: PLC0415

    endpoint_module.validate_endpoint(source)
    mounts = _mounts(snapshot)
    referent = source["referent"]
    try:
        if (not force_alias and (not exact_source_mapping or referent["mount_identity"] in mounts)
                and endpoint_module.map_entry(snapshot, referent["container_path"], writable=False) == referent["host_path"]):
            return referent["container_path"], mounts, []
    except endpoint_module.EndpointError:
        pass
    host_parent = Path(referent["host_path"]).parent
    with manifest._directory(host_parent, (), create=False) as fd:
        info = os.fstat(fd)
        observed = {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
                    "uid": info.st_uid, "gid": info.st_gid}
        if observed != referent["parent_identity"]:
            _fail("canonical source parent changed")
    digest = full._endpoint_hash(source)
    alias = "/run/wuwaterm-full-read-" + transaction_id + "-" + digest[:16]
    _safe_target(alias)
    candidate = PurePosixPath(alias)
    for item in mounts:
        existing = PurePosixPath(item["container"])
        if candidate.is_relative_to(existing) or existing.is_relative_to(candidate):
            _fail("canonical read alias overlaps the saved recipe")
    extra = {"purpose": "canonical-source", "endpoint_sha256": digest, "host_parent": str(host_parent),
             "parent_device": info.st_dev, "parent_inode": info.st_ino, "probe_alias": alias, "read_only": True}
    mounts.append({"host": str(host_parent), "container": alias, "read_only": True,
                   "device": info.st_dev, "inode": info.st_ino})
    mounts.sort(key=lambda item: item["container"])
    return str(candidate / Path(referent["host_path"]).name), mounts, [extra]


def verify_canonical_probe_binding(root: Path, journal: dict[str, Any], probe: dict[str, Any]) -> None:
    """Authorize only the journal's recorded source, writer, clock and RO alias."""
    import component_full as full  # noqa: PLC0415

    if journal["kind"] != "component-full" or journal["schema_version"] != 4:
        _fail("canonical endpoint probe requires a FULL4 journal")
    request = probe["canonical_request"]
    matches = [item for item in journal["migrations"]["items"] if item["kind"] == probe["state_kind"]]
    if len(matches) != 1:
        _fail("canonical endpoint kind is not uniquely bound")
    item = matches[0]
    if journal["phase"] == "state-snapshot":
        evidence = item["compatibility"]
        new_source = item["quiescent_legacy_endpoint"] if item["action"] == "copy-if-absent" else item["quiescent_new_endpoint"]
        pairs = ((item["quiescent_old_endpoint"], item["quiescent_old_endpoint"]),
                 (new_source, item["quiescent_new_endpoint"]))
    elif journal["phase"] == "state-migrate":
        evidence = item["compatibility"]
        staged = None if item["forward"] is None else item["forward"]["staged"]
        published = None if item["forward"] is None else item["forward"]["published"]
        pairs = ((staged, item["quiescent_new_endpoint"]), (published, item["quiescent_old_endpoint"]))
    elif journal["phase"] == "rollback-copyback":
        evidence = item["rollback_compatibility"]
        staged = None if item["rollback"] is None else item["rollback"]["staged"]
        pairs = ((item["progressed_endpoint"], item["quiescent_old_endpoint"]),
                 (item["progressed_endpoint"], item["progressed_endpoint"]),
                 (staged, item["quiescent_old_endpoint"]))
    else:
        _fail("canonical probe is outside an authorized state phase")
    selected = [(source, writer) for source, writer in pairs if source is not None and writer is not None
                and full._endpoint_hash(source) == request["source_endpoint_sha256"]
                and full._endpoint_hash(writer) == request["writer_endpoint_sha256"]]
    if (not selected or any(pair != selected[0] for pair in selected[1:])
            or evidence is None or evidence["clock"] != probe["clock"]):
        _fail("canonical source, writer or durable clock differs")
    source, writer = selected[0]
    if (source["referent"]["sha256"] != evidence["raw_sha256"] or not writer["writer"]["can_replace"]
            or writer["recipe_sha256"] != request["recipe_sha256"]
            or writer["image_id"] != probe["image_id"] or writer["image_revision"] != probe["image_revision"]
            or writer["user"] != probe["user"] or writer["writer"]["container_path"] != request["writer_path"]
            or request["source_preimage"] != {key: source["referent"][key] for key in ("present", "stat", "sha256")}):
        _fail("canonical endpoint binding differs")
    components = (journal["before"]["topology"]["telegram"],
                  journal["target"]["manifest"]["components"]["telegram"])
    candidates = [component for component in components if component is not None
                  and component["recipe"]["sha256"] == request["recipe_sha256"]
                  and component["image"]["id"] == probe["image_id"]
                  and component["image"]["revision"] == probe["image_revision"]]
    if not candidates or candidates[0]["provenance"]["kind"] not in ("legacy-image", "wheel-image"):
        _fail("canonical reader is not a recorded product image")
    flavor = "legacy" if candidates[0]["provenance"]["kind"] == "legacy-image" else "wheel"
    snapshot = manifest.read_recipe_snapshot(root, request["recipe_sha256"])
    image = _image(probe["image_id"], probe["image_revision"])
    path, mounts, extras = _canonical_mounts(snapshot, journal["transaction_id"], source)
    uid, gid = probe["user"].split(":")
    scratch = {"type": "tmpfs", "container": "/run/wuwaterm-full-state-" + probe["probe_id"],
               "options": ["rw", "noexec", "nosuid", "nodev", "uid=" + uid, "gid=" + gid, "mode=0700"]}
    expected_mounts = sorted([*mounts, scratch], key=lambda value: value["container"])
    if (probe["flavor"] != flavor or probe["user"] != _effective_user(snapshot, image)
            or probe["working_dir"] != (snapshot["working_dir"] or "/")
            or probe["mounts"] != expected_mounts or probe["extra_read_binds"] != extras
            or request["source_path"] != path or probe["input_path"] != path):
        _fail("canonical probe mounts or actual reader identity differ")


def canonical_endpoint(root: Path, *, snapshot: dict[str, Any], source: dict[str, Any],
                       writer: dict[str, Any], state_kind: str, clock: int,
                       transaction_id: str) -> dict[str, Any]:
    """Read recorded state with an installed native reader and its real UID."""
    import component_full as full  # noqa: PLC0415

    try:
        journal, component = _journal_snapshot(root, transaction_id, snapshot)
        if (journal["kind"] != "component-full" or journal["schema_version"] != 4
                or state_kind not in ("chat-settings", "channel-replies") or type(clock) is not int
                or not 0 <= clock <= 2**63 - 1 or component["provenance"]["kind"] not in ("legacy-image", "wheel-image")):
            _fail("canonical endpoint request is invalid")
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        image = _image(image_id, revision)
        user = _effective_user(snapshot, image)
        path, mounts, extras = _canonical_mounts(snapshot, transaction_id, source)
        request = {"recipe_sha256": component["recipe"]["sha256"],
                   "source_endpoint_sha256": full._endpoint_hash(source),
                   "writer_endpoint_sha256": full._endpoint_hash(writer),
                   "source_path": path, "writer_path": writer["writer"]["container_path"],
                   "source_preimage": {key: source["referent"][key] for key in ("present", "stat", "sha256")}}
        result = _execute(root, journal, kind="state-canonical-v4", image_id=image_id, revision=revision,
                          user=user, working_dir=snapshot["working_dir"] or "/", mounts=mounts,
                          settings=dict.fromkeys(_PATH_KEYS), input_path=path,
                          flavor="legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
                          state_kind=state_kind, clock=clock, canonical_request=request, extra_read_binds=extras)
        if result["raw_sha256"] != source["referent"]["sha256"] or not result["writer_can_replace"]:
            _fail("canonical reader differs from its source or writer")
        return result
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("native endpoint semantics could not be verified") from None


def _copy_v4_context(root: Path, journal: dict[str, Any], state_kind: str, direction: str,
                     action: str, *, reconcile_only: bool = False) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Derive a copy request only from one durable FULL4 journal entry."""
    import component_full as full  # noqa: PLC0415

    if (journal["kind"] != "component-full" or journal["schema_version"] != 4
            or state_kind not in ("chat-settings", "channel-replies") or direction not in ("forward", "rollback")
            or action not in ("reserve", "write", "publish", "cleanup", "discard")):
        _fail("FULL4 copy context is invalid")
    phases_allowed = ({"state-migrate", "rollback-copyback"} if direction == "forward" and action in ("reserve", "publish") else
                      {"state-migrate"} if direction == "forward" and action not in ("cleanup", "discard") else
                      {"rollback-copyback"})
    if journal["phase"] not in phases_allowed:
        _fail("FULL4 copy is outside its authorized phase")
    if (type(reconcile_only) is not bool or reconcile_only and
            (action != "publish" or journal["phase"] != "rollback-copyback")):
        _fail("FULL4 copy reconciliation mode is invalid")
    matches = [item for item in journal["migrations"]["items"] if item["kind"] == state_kind]
    if len(matches) != 1:
        _fail("FULL4 copy state kind is not unique")
    item = matches[0]
    progress = item[direction]
    if progress is None or item["action"] != "copy-if-absent":
        _fail("FULL4 copy has no durable relocation claim")
    source = item["quiescent_legacy_endpoint"] if direction == "forward" else item["progressed_endpoint"]
    destination = item["quiescent_new_endpoint"] if direction == "forward" else item["quiescent_old_endpoint"]
    if source is None or destination is None:
        _fail("FULL4 copy endpoints are missing")
    phases = {"reserve": ("intent",), "write": ("reserved", "writing"), "publish": ("publish-intent",),
              "cleanup": ("published",), "discard": ("reserved", "writing", "staged", "publish-intent")}
    if progress["phase"] not in phases[action] or progress["stage_before"] is None:
        _fail("FULL4 copy durable phase differs")
    if action == "cleanup" and (direction != "forward" or item["cleanup"] is None
                                 or item["cleanup"]["phase"] != "intent"):
        _fail("FULL4 cleanup has no durable intent")
    if action == "discard" and (direction != "forward" or progress["discard"] is None
                                 or progress["discard"]["phase"] != "intent"
                                 or progress["discard"]["copy_phase"] != progress["phase"]):
        _fail("FULL4 stage discard has no durable intent")
    component = (journal["target"]["manifest"]["components"]["telegram"] if direction == "forward" else
                 journal["before"]["topology"]["telegram"])
    if component["provenance"]["kind"] not in ("legacy-image", "wheel-image"):
        _fail("FULL4 copy image has no native validator")
    snapshot = manifest.read_recipe_snapshot(root, component["recipe"]["sha256"])
    image = _image(component["image"]["id"], component["image"]["revision"])
    if (progress["binding"]["image_id"] != component["image"]["id"]
            or progress["binding"]["image_revision"] != component["image"]["revision"]
            or progress["binding"]["recipe_sha256"] != component["recipe"]["sha256"]
            or progress["binding"]["user"] != _effective_user(snapshot, image)
            or progress["binding"]["mounts_sha256"] != hashlib.sha256(manifest._encode(_mounts(snapshot))).hexdigest()):
        _fail("FULL4 copy image or mount binding differs")
    read_endpoint = item["cleanup"]["old_published_endpoint"] if action == "cleanup" else source
    path, mounts, extras = _canonical_mounts(snapshot, journal["transaction_id"], read_endpoint,
                                              force_alias=action == "cleanup", exact_source_mapping=True)
    cleanup = (None if action != "cleanup" else {key: item["cleanup"][key] for key in (
        "created_entry_claim_sha256", "post_stop_new_endpoint", "old_published_endpoint")})
    request = {"schema_version": 1, "action": action,
               "phase": "intent" if action in ("cleanup", "discard") else progress["phase"],
               "binding": progress["binding"], "binding_sha256": full._endpoint_hash(progress["binding"]),
               "source_endpoint": source, "destination_before": destination, "read_path": path, "mounts": mounts,
               "flavor": "legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
               "reservation": None if action in ("reserve", "cleanup") else progress["reservation"],
               "staged": (progress["staged"] if action == "publish" or
                          action == "discard" and progress["phase"] in ("staged", "publish-intent") else None),
               "cleanup": cleanup, "discard": ({"binding_sha256": full._endpoint_hash(progress["binding"]),
                                                "copy_phase": progress["phase"]} if action == "discard" else None),
               "reconcile_only": (reconcile_only or direction == "forward" and action == "publish"
                                  and journal["phase"] == "rollback-copyback")}
    return component, request, extras


def _verify_copy_claim(root: Path, journal: dict[str, Any], state_kind: str, direction: str,
                       binding: dict[str, Any]) -> None:
    name = f"{journal['transaction_id']}-{state_kind}-{direction}.json"
    with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=False) as directory:
        if manifest._read_file(directory, name) != manifest._encode(binding):
            _fail("FULL4 durable copy claim differs")


def create_copy_claim(root: Path, transaction_id: str, state_kind: str, direction: str) -> None:
    root = admission._physical_root(root)
    kind, journal = _select_journal(root, transaction_id)
    if kind != "full" or journal["schema_version"] != 4:
        _fail("FULL4 copy claim has no transaction")
    _admit(root, transaction_id, journal["source"]["source_tool_sha256"], transaction_kind="full")
    _, request, _ = _copy_v4_context(root, journal, state_kind, direction, "reserve")
    name = f"{transaction_id}-{state_kind}-{direction}.json"
    with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=True) as directory:
        manifest._write_file_unnamed(directory, name, manifest._encode(request["binding"]), reuse=True)
    _verify_copy_claim(root, journal, state_kind, direction, request["binding"])


def verify_copy_probe_binding(root: Path, journal: dict[str, Any], probe: dict[str, Any]) -> None:
    request = probe["copy_request"]
    binding = request["binding"]
    if type(binding) is not dict or binding.get("state_kind") != probe["state_kind"]:
        _fail("FULL4 copy probe state kind differs")
    direction = binding.get("direction")
    component, expected, extras = _copy_v4_context(root, journal, probe["state_kind"], direction, request["action"],
                                                    reconcile_only=request["reconcile_only"])
    if (request != expected or probe["extra_read_binds"] != extras or probe["mounts"] != expected["mounts"]
            or probe["image_id"] != component["image"]["id"]
            or probe["image_revision"] != component["image"]["revision"]
            or probe["user"] != expected["binding"]["user"] or probe["flavor"] != expected["flavor"]):
        _fail("FULL4 copy probe differs from its durable claim")
    _verify_copy_claim(root, journal, probe["state_kind"], direction, binding)


def _verify_copy_source(root: Path, journal: dict[str, Any], source: dict[str, Any], *,
                        purpose: str, state_kind: str) -> None:
    import component_recovery as recovery  # noqa: PLC0415
    import component_state_endpoint as endpoint_module  # noqa: PLC0415
    import component_transaction as transaction  # noqa: PLC0415

    components = (journal["before"]["topology"]["telegram"],
                  journal["target"]["manifest"]["components"]["telegram"])
    candidates = [item for item in components if item is not None and item["recipe"]["sha256"] == source["recipe_sha256"]
                  and item["image"]["id"] == source["image_id"] and item["image"]["revision"] == source["image_revision"]]
    if not candidates:
        _fail("FULL4 copy source has no recorded image")
    snapshot = manifest.read_recipe_snapshot(root, source["recipe_sha256"])
    image = _image(source["image_id"], source["image_revision"])
    compose = recovery._verify_bound_compose(root, journal["source"])
    environment = transaction._effective_environment(snapshot, image, compose, root)
    actual = endpoint(root, snapshot=snapshot, environment=environment, state_kind=state_kind,
                      effective_path=source["effective_path"], purpose=purpose,
                      transaction_id=journal["transaction_id"])
    endpoint_module.require_unchanged(source, actual)


def _check_reservation_boundary(journal: dict[str, Any], direction: str, stage: dict[str, Any]) -> None:
    """Do not create a new forward stage after rollback has begun."""
    import component_state_endpoint as endpoint_module  # noqa: PLC0415

    endpoint_module.validate_endpoint(stage)
    if direction not in ("forward", "rollback"):
        _fail("FULL4 reservation direction is invalid")
    if (journal["phase"] == "rollback-copyback" and direction == "forward"
            and stage["entry"]["type"] == "absent"):
        _fail("rollback cannot create a new forward state reservation")


def copy_v4(root: Path, transaction_id: str, state_kind: str, *, direction: str, action: str,
            reconcile_only: bool = False) -> dict[str, Any]:
    """Run one already-claimed state-copy step under the recorded image UID."""
    try:
        root = admission._physical_root(root)
        kind, journal = _select_journal(root, transaction_id)
        if kind != "full":
            _fail("FULL4 copy has no full transaction")
        _admit(root, transaction_id, journal["source"]["source_tool_sha256"], transaction_kind="full")
        component, request, extras = _copy_v4_context(root, journal, state_kind, direction, action,
                                                       reconcile_only=reconcile_only)
        _verify_copy_claim(root, journal, state_kind, direction, request["binding"])
        if action == "reserve":
            import component_recovery as recovery  # noqa: PLC0415
            import component_transaction as transaction  # noqa: PLC0415

            snapshot = manifest.read_recipe_snapshot(root, component["recipe"]["sha256"])
            image = _image(component["image"]["id"], component["image"]["revision"])
            environment = transaction._effective_environment(snapshot, image,
                          recovery._verify_bound_compose(root, journal["source"]), root)
            stage_path = str(PurePosixPath(request["destination_before"]["writer"]["container_path"]).parent /
                             Path(request["binding"]["stage_path"]).name)
            stage = endpoint(root, snapshot=snapshot, environment=environment, state_kind=state_kind,
                             effective_path=stage_path, purpose="stage", transaction_id=transaction_id)
            _check_reservation_boundary(journal, direction, stage)
            if stage["entry"]["type"] != "absent":
                witnessed = False
                directory = root / ".deployments/component-adopt-probes"
                for entry in directory.iterdir():
                    prior = runtime._read_adopt_probe(root, entry.name)
                    if (prior["kind"] != "state-copy-v4" or prior["status"] not in ("failed", "completed")
                            or not prior["started"] or prior["transaction_id"] != transaction_id
                            or prior["source_tool_sha256"] != journal["source"]["source_tool_sha256"]
                            or prior["image_id"] != component["image"]["id"]
                            or prior["image_revision"] != component["image"]["revision"]
                            or prior["user"] != request["binding"]["user"]
                            or prior["working_dir"] != request["destination_before"]["working_dir"]
                            or prior["extra_read_binds"] != extras
                            or prior["copy_request"] != request or _owned(prior) is not None):
                        continue
                    if prior["result"] is not None and prior["result"]["endpoint"] != stage:
                        continue
                    witnessed = True
                if not witnessed:
                    _fail("existing state reservation has no stopped matching probe")
        source = request["cleanup"]["old_published_endpoint"] if action == "cleanup" else request["source_endpoint"]
        _verify_copy_source(root, journal, source, purpose="target" if direction == "rollback" or action == "cleanup" else "legacy",
                            state_kind=state_kind)
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        result = _execute(root, journal, kind="state-copy-v4", image_id=image_id, revision=revision,
                          user=request["binding"]["user"], working_dir=request["destination_before"]["working_dir"],
                          mounts=request["mounts"], settings=dict.fromkeys(_PATH_KEYS), input_path=request["read_path"],
                          flavor=request["flavor"], state_kind=state_kind, copy_v4_request=request,
                          extra_read_binds=extras)
        _verify_copy_source(root, journal, source, purpose="target" if direction == "rollback" or action == "cleanup" else "legacy",
                            state_kind=state_kind)
        return result
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("FULL4 state copy could not be verified") from None


def _plan_binding_paths(item: dict[str, Any]) -> None:
    """Require the bindings one schema-5 native plan has to name.

    Every plan names one effective path, so a path outside every bind is never
    equivalent to another. Only the product's own pending copy also names the
    retained legacy file; the other actions carry no legacy endpoint to map and
    report ``mapped_legacy`` as ``None`` even inside a complete binding.
    """
    if item.get("mapped_target") is None:
        _fail("state path is outside every bind")
    if item.get("migration") == "copy-if-absent" and item.get("mapped_legacy") is None:
        _fail("state path is outside every bind")


def plan(root: Path, *, receipt_sha256: str | None, snapshot: dict[str, Any], environment: dict[str, str],
         database_path: str, transaction_id: str) -> dict[str, Any]:
    """Run the verified installed product planner with actual bind permissions."""
    try:
        journal, component = _journal_snapshot(root, transaction_id, snapshot)
        is_full = journal["kind"] == "component-full"
        database = journal["database"]["old"]["binding"] if is_full else journal["database"]["binding"]
        if database["path"] != database_path:
            _fail("state planner database binding differs")
        target = journal["target"]["manifest"]["components"]["telegram"]
        endpoint_protocol = is_full and journal["schema_version"] == 4
        schema5 = journal["schema_version"] == 5
        # Schema 5 plans each recipe with its own image: the old legacy Core
        # image through the read-only legacy planner, the product image
        # through the product's native planner.
        planned_component = component if endpoint_protocol or schema5 else target
        provenance = planned_component["provenance"]["kind"]
        legacy = (is_full or schema5) and provenance == "legacy-image"
        if schema5 and provenance == "completed-artifact":
            if receipt_sha256 is not None:
                _fail("schema-5 state planning requires a completed artifact without a producer receipt")
            import component_transaction as transaction  # noqa: PLC0415
            try:
                verified = transaction.verify_artifact_component(root, planned_component, "telegram")
            except transaction.TransactionError:
                _fail("state planner completed artifact cannot be verified")
            receipt = None
            image_id, revision = verified["image_id"], verified["image_revision"]
        elif legacy:
            if receipt_sha256 is not None or (not endpoint_protocol and not schema5 and "telegram" in journal["selection"]):
                _fail("legacy state planning has an unexpected receipt or selection")
            receipt = None
            image_id, revision = planned_component["image"]["id"], planned_component["image"]["revision"]
            _image(image_id, revision)
        else:
            if planned_component["provenance"].get("build_receipt_sha256") != receipt_sha256 or receipt_sha256 is None:
                _fail("state planner receipt differs from the target")
            receipt = wheel.verify_registered_wheel_build(root, receipt_sha256)
            if (receipt["image_id"] != planned_component["image"]["id"]
                    or receipt["image_revision"] != planned_component["image"]["revision"]):
                _fail("state planner image differs from the target")
            image_id, revision = receipt["image_id"], receipt["image_revision"]
        old_image = _image(snapshot["image_id"], component["image"]["revision"])
        user = _effective_user(snapshot, old_image)
        settings, _ = observe._environment([key + "=" + value for key, value in environment.items()],
                                           snapshot["working_dir"], os.urandom(32))
        db_path = settings["WUWATERM_DB_PATH"]
        if snapshot["command"] and snapshot["command"][:1] == ["--db"]:
            db_path = snapshot["command"][1]
        if db_path is None or observe.resolve_bind_path(snapshot, db_path, writable=False) != database_path:
            _fail("state planner database mount differs")
        path_settings = {key: settings[key] for key in _PATH_KEYS}
        mounts = _mounts(snapshot)
        if legacy:
            mounts = [{**mount, "read_only": True} if "host" in mount else mount for mount in mounts]
        result = _execute(root, journal, kind="state-legacy-plan" if legacy else "state-plan", image_id=image_id,
                          revision=revision, user=user,
                          working_dir=snapshot["working_dir"] or "/", mounts=mounts,
                          settings=path_settings, input_path=db_path,
                          plan_request={"recipe_sha256": component["recipe"]["sha256"]} if endpoint_protocol else None)
        _validate_result("state-plan", result, db_path)
        if legacy and not schema5 and any(item["migration"] == "copy-if-absent"
                                          for item in result["kinds"].values()):
            # The old image's own startup copy is a schema-5 native-pending
            # action; older FULL records never planned it.
            _fail("legacy state copy requires a schema-5 cutover")
        if endpoint_protocol:
            import component_state_endpoint as endpoint  # noqa: PLC0415
        for item in result["kinds"].values():
            if schema5:
                _plan_binding_paths(item)
            mapper = (entry_path if schema5 else
                      endpoint.map_entry if endpoint_protocol else observe.resolve_bind_path)
            item["mapped_target"] = mapper(snapshot, item["mapped_target"], writable=True)
            if item["mapped_legacy"] is not None:
                item["mapped_legacy"] = mapper(snapshot, item["mapped_legacy"], writable=False)
        if receipt is not None and wheel.verify_registered_wheel_build(root, receipt_sha256) != receipt:
            _fail("registered state planner changed")
        if legacy:
            _image(image_id, revision)
        return result
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, wheel.WheelBuildError,
            admission.AdmissionError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("state planning could not establish the recorded binding") from None


def _container_path(snapshot: dict[str, Any], host_path: str) -> str:
    manifest._path(host_path, "state host path")
    host = PurePosixPath(host_path)
    choices = []
    for mount in snapshot["mounts"]:
        if "host" not in mount:
            continue
        source = PurePosixPath(mount["host"])
        if host == source or host.is_relative_to(source):
            candidate = str(PurePosixPath(mount["container"]) / host.relative_to(source))
            if observe.resolve_bind_path(snapshot, candidate, writable=False) == host_path:
                choices.append(candidate)
    if len(set(choices)) != 1:
        _fail("state path has no unique visible bind")
    return choices[0]


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


def _planned_components(journal: dict[str, Any], component: dict[str, Any]) -> list[dict[str, Any]]:
    """Components whose recipe may own a completed plan probe.

    A schema-5 cutover plans the old legacy image through the read-only legacy
    planner and the product image through the product planner, so both recipes
    can legitimately own a completed probe of this transaction.
    """
    components = [component]
    if journal["schema_version"] != 5:
        return components
    others = (journal["before"]["topology"]["telegram"],
              journal["target"]["manifest"]["components"]["telegram"])
    for item in others:
        if item is None or item["recipe"]["sha256"] in {known["recipe"]["sha256"] for known in components}:
            continue
        components.append(item)
    return components


def _completed_plan_paths(root: Path, journal: dict[str, Any], transaction_id: str,
                          snapshot: dict[str, Any], component: dict[str, Any]) -> set[str]:
    image = _image(snapshot["image_id"], component["image"]["revision"])
    _effective_user(snapshot, image)
    daemon = _daemon()
    allowed: set[str] = set()
    directory = root / ".deployments/component-adopt-probes"
    for entry in directory.iterdir():
        probe = runtime._read_adopt_probe(root, entry.name)
        if (probe["kind"] not in ("state-plan", "state-legacy-plan") or probe["status"] != "completed"
                or probe["transaction_id"] != transaction_id
                or probe["source_tool_sha256"] != journal["source"]["source_tool_sha256"]
                or probe["daemon_id"] != daemon):
            continue
        for owner in _planned_components(journal, component):
            if (probe["image_id"] != owner["image"]["id"]
                    or probe["image_revision"] != owner["image"]["revision"]):
                continue
            owner_snapshot = (snapshot if owner["recipe"]["sha256"] == component["recipe"]["sha256"]
                              else manifest.read_recipe_snapshot(root, owner["recipe"]["sha256"]))
            owner_image = _image(owner_snapshot["image_id"], owner["image"]["revision"])
            expected_mounts = _mounts(owner_snapshot)
            mounts = ([{**mount, "read_only": True} if "host" in mount else mount for mount in expected_mounts]
                      if probe["kind"] == "state-legacy-plan" else expected_mounts)
            if (probe["user"] != _effective_user(owner_snapshot, owner_image)
                    or probe["working_dir"] != (owner_snapshot["working_dir"] or "/")
                    or probe["mounts"] != mounts):
                continue
            for item in probe["result"]["kinds"].values():
                mapper = entry_path if journal["schema_version"] == 5 else observe.resolve_bind_path
                allowed.add(mapper(owner_snapshot, item["mapped_target"], writable=True))
                if item["mapped_legacy"] is not None:
                    allowed.add(mapper(owner_snapshot, item["mapped_legacy"], writable=False))
    return allowed


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


def _state_probe_paths(journal: dict[str, Any], snapshot: dict[str, Any], host_path: str,
                       *, writable: bool = False) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Return the file a probe reads, a separate writer entry, and its mounts.

    Schema 5 reads the file a leaf link names and replaces the entry itself, so
    a retained reply link needs both container paths; older schemas keep their
    strict single-path mapping and the FULL3 rollback source fallback.
    """
    if journal["schema_version"] != 5:
        path, mounts = _probe_path(journal, snapshot, host_path, writable=writable)
        return path, None, mounts
    try:
        entry = _entry_container(snapshot, host_path, writable=writable)
        read = _leaf_container_path(snapshot, entry)
        return read, (None if read == entry else entry), _mounts(snapshot)
    except (manifest.ManifestError, observe.ObservationError) as exc:
        raise StateError("state path cannot be mapped through its binding") from exc


def _probe_path(journal: dict[str, Any], snapshot: dict[str, Any], host_path: str,
                *, writable: bool = False) -> tuple[str, list[dict[str, Any]]]:
    """Map a recorded state path or bind a disjoint rollback source explicitly."""
    mounts = _mounts(snapshot)
    try:
        path = _container_path(snapshot, host_path)
        if writable and observe.resolve_bind_path(snapshot, path, writable=True) != host_path:
            _fail("state path is not writable through its recorded binding")
        return path, mounts
    except (StateError, observe.ObservationError):
        if (journal["kind"] != "component-full" or journal["phase"] != "rollback-copyback"
                or not any(item["new_target_path"] == host_path and item["old_target_path"] != host_path
                           for item in journal["migrations"]["items"])):
            raise
    if writable:
        _fail("cross-bound state sources are read-only")
    source = Path(manifest._path(host_path, "cross-bound state source"))
    with manifest._directory(source.parent, (), create=False):
        pass
    info = source.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        _fail("cross-bound state source parent is not a directory")
    digest = hashlib.sha256(host_path.encode("utf-8")).hexdigest()[:16]
    target = "/run/wuwaterm-full-cross-" + journal["transaction_id"] + "-" + digest
    _safe_target(target)
    for mount in mounts:
        existing, candidate = PurePosixPath(mount["container"]), PurePosixPath(target)
        if existing.is_relative_to(candidate) or candidate.is_relative_to(existing):
            _fail("cross-bound state source overlaps another mount")
    mounts.append({"host": str(source.parent), "container": target, "read_only": True,
                   "device": info.st_dev, "inode": info.st_ino})
    mounts.sort(key=lambda item: item["container"])
    return str(PurePosixPath(target) / source.name), mounts


def preimage(root: Path, *, image_id: str, image_revision: str, snapshot: dict[str, Any],
             host_path: str, transaction_id: str) -> dict[str, Any]:
    """Hash only a transaction's approved state path using its actual image UID."""
    try:
        journal, component = _journal_snapshot(root, transaction_id, snapshot)
        image = _image(image_id, image_revision)
        user = _effective_user(snapshot, image)
        allowed = _completed_plan_paths(root, journal, transaction_id, snapshot, component)
        if journal["kind"] == "component-full":
            for candidate in (journal["before"]["topology"]["telegram"],
                              journal["target"]["manifest"]["components"]["telegram"]):
                if candidate is not None:
                    other = manifest.read_recipe_snapshot(root, candidate["recipe"]["sha256"])
                    allowed.update(_completed_plan_paths(root, journal, transaction_id, other, candidate))
            for item in journal["migrations"]["items"]:
                # Schema 5 has no staging or copy-back fields; only the FULL3
                # and FULL4 records they belong to may widen this set.
                stage = item.get("forward_stage")
                copyback = item.get("copyback")
                if stage is not None and item["new_target_path"] in allowed:
                    allowed.add(stage)
                if copyback is not None and item["old_target_path"] in allowed:
                    allowed.add(copyback["stage"])
        if (host_path not in allowed or image_id != snapshot["image_id"]
                or image_id != component["image"]["id"] or image_revision != component["image"]["revision"]):
            _fail("state preimage is outside the recorded binding")
        input_path, _, probe_mounts = _state_probe_paths(journal, snapshot, host_path)
        result = _execute(root, journal, kind="state-preimage", image_id=image_id, revision=image_revision,
                          user=user, working_dir=snapshot["working_dir"] or "/", mounts=probe_mounts,
                          settings=dict.fromkeys(_PATH_KEYS), input_path=input_path)
        runtime._file_preimage(result, "state probe result")
        return result
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("state preimage could not establish the recorded binding") from None


def _canonical_binding(journal: dict[str, Any], component: dict[str, Any], state_kind: str,
                       clock: int) -> tuple[str, str, str]:
    """Decide which native reader may compare state bytes for this journal.

    Schema 4 used endpoint claims; schema 5 keeps the same read-only helper for
    a cutover rollback, where the old image must accept what the new product
    wrote. A schema-5 runtime record may therefore read state, but only for its
    selected Telegram component.
    """
    schema5_runtime = (journal["schema_version"] == 5 and journal["kind"] == "component-runtime"
                       and "telegram" in journal["selection"])
    if journal["kind"] != "component-full" and not schema5_runtime:
        _fail("canonical state comparison requires a full transaction")
    if state_kind not in ("chat-settings", "channel-replies"):
        _fail("canonical state comparison requires a full transaction")
    if type(clock) is not int or not 0 <= clock <= 2**63 - 1:
        _fail("canonical state clock is invalid")
    provenance = component["provenance"]["kind"]
    if provenance not in ("legacy-image", "wheel-image", "completed-artifact"):
        _fail("state image has no supported native reader")
    return (component["image"]["id"], component["image"]["revision"],
            "legacy" if provenance == "legacy-image" else "wheel")


def canonical(root: Path, *, snapshot: dict[str, Any], host_path: str, transaction_id: str,
              state_kind: str, clock: int) -> dict[str, Any]:
    """Compare native reader semantics on a private copy under the actual UID.

    The reader is the component that owns ``snapshot``. A schema-5 journal may
    hold either the legacy Core image or an already cut-over product artifact,
    and its rollback must prove the old reader accepts the bytes the new product
    left behind, so a runtime schema-5 Telegram update may run this helper
    without any copy or endpoint helper.
    """
    try:
        journal, component = _journal_snapshot(root, transaction_id, snapshot)
        provenance = component["provenance"]["kind"]
        image_id, revision, flavor = _canonical_binding(journal, component, state_kind, clock)
        if provenance == "completed-artifact":
            import component_transaction as transaction  # noqa: PLC0415
            try:
                transaction.verify_artifact_component(root, component, "telegram")
            except transaction.TransactionError:
                _fail("state image has no supported native reader")
        else:
            _image(image_id, revision)
        before = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                          host_path=host_path, transaction_id=transaction_id)
        image = _image(image_id, revision)
        user = _effective_user(snapshot, image)
        input_path, writer_path, probe_mounts = _state_probe_paths(journal, snapshot, host_path)
        result = _execute(root, journal, kind="state-canonical", image_id=image_id, revision=revision,
                          user=user, working_dir=snapshot["working_dir"] or "/", mounts=probe_mounts,
                          settings=dict.fromkeys(_PATH_KEYS), input_path=input_path, canonical_writer=writer_path,
                          flavor=flavor, state_kind=state_kind, clock=clock)
        if result["raw_sha256"] != before["sha256"]:
            _fail("canonical state source differs from its preimage")
        after = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                         host_path=host_path, transaction_id=transaction_id)
        if after != before:
            _fail("canonical state changed during comparison")
        return result
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("native state semantics could not be verified") from None


def _copy_context(root: Path, transaction_id: str, state_kind: str,
                  direction: str, *, source_writable: bool = False,
                  discard_forward: bool = False) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    import component_transaction as transaction  # noqa: PLC0415

    kind, journal = _select_journal(root, transaction_id)
    if kind != "full" or state_kind not in ("chat-settings", "channel-replies") or direction not in ("forward", "rollback"):
        _fail("state copy is outside a full transaction")
    expected_phase = "state-migrate" if direction == "forward" and not discard_forward else "rollback-copyback"
    if journal["phase"] != expected_phase or journal["status"] not in ("active", "unresolved"):
        _fail("state copy phase differs")
    matches = [item for item in journal["migrations"]["items"] if item["kind"] == state_kind]
    if len(matches) != 1:
        _fail("state copy kind is not uniquely recorded")
    item = matches[0]
    if item["action"] != "copy-if-absent" or item["old_target_path"] == item["new_target_path"]:
        _fail("state copy is not an authorized relocation")
    if direction == "forward":
        component = journal["target"]["manifest"]["components"]["telegram"]
        source, destination, stage = item["old_target_path"], item["new_target_path"], item["forward_stage"]
        source_before, destination_before = item["old_preimage"], item["new_preimage"]
        compatibility = item["compatibility"]
    else:
        component = journal["before"]["topology"]["telegram"]
        copyback = item["copyback"]
        if copyback is None:
            _fail("state copy-back intent is missing")
        source, destination, stage = item["new_target_path"], item["old_target_path"], copyback["stage"]
        source_before, destination_before = copyback["source"], copyback["destination"]
        compatibility = item["rollback_compatibility"]
    if source_before is None or destination_before is None or compatibility is None:
        _fail("state copy lacks verified preimages or compatibility")
    if not source_before["present"] or compatibility["raw_sha256"] != source_before["sha256"]:
        _fail("state copy source differs from native compatibility evidence")
    snapshot = transaction._snapshot(root, component)
    _journal_snapshot(root, transaction_id, snapshot)
    image = _image(component["image"]["id"], component["image"]["revision"])
    user = _effective_user(snapshot, image)
    source_path, probe_mounts = _probe_path(journal, snapshot, source, writable=source_writable)
    destination_path, destination_mounts = _probe_path(journal, snapshot, destination, writable=True)
    stage_path, stage_mounts = _probe_path(journal, snapshot, stage, writable=True)
    if destination_mounts != _mounts(snapshot) or stage_mounts != _mounts(snapshot):
        _fail("state copy destination is outside its recorded writable snapshot")
    context = {"direction": direction, "source": source_path,
               "destination": destination_path, "stage": stage_path,
               "source_before": source_before, "destination_before": destination_before, "stage_before": None}
    claim = {"schema_version": 1, "kind": "state-copy", "transaction_id": transaction_id,
             "state_kind": state_kind, "direction": direction, "source": source, "destination": destination,
             "stage": stage, "source_before": source_before, "destination_before": destination_before,
             "image_id": component["image"]["id"], "image_revision": component["image"]["revision"], "user": user}
    return journal, component, snapshot, {"request": context, "claim": claim, "user": user, "mounts": probe_mounts,
                                           "stage_host": stage, "compatibility": compatibility}


def copy_stage(root: Path, transaction_id: str, state_kind: str, *, direction: str) -> dict[str, Any]:
    """Stage a claimed, product-validated state copy without replacing a target."""
    try:
        root = admission._physical_root(root)
        journal, component, snapshot, context = _copy_context(root, transaction_id, state_kind, direction)
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        prior = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                         host_path=context["stage_host"], transaction_id=transaction_id)
        claim_name = f"{transaction_id}-{state_kind}-{direction}.json"
        with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=True) as directory:
            try:
                existing = os.stat(claim_name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if prior["present"] and existing is None:
                _fail("existing state stage has no owned claim")
            manifest._write_file(directory, claim_name, manifest._encode(context["claim"]), reuse=True)
        if prior["present"]:
            if prior["sha256"] != context["request"]["source_before"]["sha256"]:
                _fail("existing state stage contents differ")
            result = prior
        else:
            result = _execute(root, journal, kind="state-copy-stage", image_id=image_id, revision=revision,
                              user=context["user"], working_dir=snapshot["working_dir"] or "/", mounts=context["mounts"],
                              settings=dict.fromkeys(_PATH_KEYS), input_path=context["request"]["source"],
                              flavor="legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
                              state_kind=state_kind, copy_request=context["request"])
        verified = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                            host_path=context["stage_host"], transaction_id=transaction_id)
        if verified != result or verified["sha256"] != context["request"]["source_before"]["sha256"]:
            _fail("state stage readback differs")
        semantic = canonical(root, snapshot=snapshot, host_path=context["stage_host"], transaction_id=transaction_id,
                             state_kind=state_kind, clock=context["compatibility"]["clock"])
        expected = (context["compatibility"]["new_canonical_sha256"] if direction == "forward" else
                    context["compatibility"]["old_canonical_sha256"])
        if semantic["raw_sha256"] != verified["sha256"] or semantic["canonical_sha256"] != expected:
            _fail("state stage native semantics differ")
        return verified
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError):
        raise StateError("state copy stage could not be verified") from None


def copy_publish(root: Path, transaction_id: str, state_kind: str, *, direction: str,
                 stage_before: dict[str, Any]) -> dict[str, Any]:
    """CAS-publish only the recorded stage under the product's effective UID."""
    try:
        root = admission._physical_root(root)
        journal, component, snapshot, context = _copy_context(root, transaction_id, state_kind, direction)
        runtime._file_preimage(stage_before, "state copy stage")
        item = next(item for item in journal["migrations"]["items"] if item["kind"] == state_kind)
        recorded_stage = (item["forward_stage_preimage"] if direction == "forward" else
                          item["copyback"]["stage_preimage"])
        if recorded_stage != stage_before:
            _fail("state copy stage is not the durable transaction observation")
        if not stage_before["present"] or stage_before["sha256"] != context["request"]["source_before"]["sha256"]:
            _fail("state copy stage differs")
        claim_name = f"{transaction_id}-{state_kind}-{direction}.json"
        with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=False) as directory:
            if manifest._read_file(directory, claim_name) != manifest._encode(context["claim"]):
                _fail("state copy claim differs")
        request = {**context["request"], "stage_before": stage_before}
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        result = _execute(root, journal, kind="state-copy-publish", image_id=image_id, revision=revision,
                          user=context["user"], working_dir=snapshot["working_dir"] or "/", mounts=context["mounts"],
                          settings=dict.fromkeys(_PATH_KEYS), input_path=request["source"],
                          flavor="legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
                          state_kind=state_kind, copy_request=request)
        actual = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                          host_path=context["claim"]["destination"], transaction_id=transaction_id)
        if result != actual:
            _fail("published state readback differs")
        return actual
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise StateError("state copy publication could not be verified") from None


def copy_cleanup(root: Path, transaction_id: str, state_kind: str) -> None:
    """Remove only the recorded relocated target after verified copy-back."""
    try:
        root = admission._physical_root(root)
        journal, component, snapshot, context = _copy_context(root, transaction_id, state_kind, "rollback",
                                                               source_writable=True)
        item = next(item for item in journal["migrations"]["items"] if item["kind"] == state_kind)
        copyback = item["copyback"]
        if copyback["phase"] not in ("published", "cleaned") or copyback["published"] is None:
            _fail("state copy-back has not been published")
        claim_name = f"{transaction_id}-{state_kind}-rollback.json"
        with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=True) as directory:
            manifest._write_file(directory, claim_name, manifest._encode(context["claim"]), reuse=True)
        request = {**context["request"], "destination_before": copyback["published"],
                   "stage_before": copyback["stage_preimage"]}
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        result = _execute(root, journal, kind="state-copy-clean", image_id=image_id, revision=revision,
                          user=context["user"], working_dir=snapshot["working_dir"] or "/", mounts=context["mounts"],
                          settings=dict.fromkeys(_PATH_KEYS), input_path=request["source"],
                          flavor="legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
                          state_kind=state_kind, copy_request=request)
        if result != {"present": False, "sha256": None, "stat": None}:
            _fail("state copy cleanup did not remove the exact target")
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise StateError("state copy cleanup could not be verified") from None


def discard_forward_stage(root: Path, transaction_id: str, state_kind: str) -> None:
    """Discard a claimed unpublished forward stage during rollback."""
    try:
        root = admission._physical_root(root)
        journal, component, snapshot, context = _copy_context(root, transaction_id, state_kind, "forward",
                                                                 discard_forward=True)
        item = next(item for item in journal["migrations"]["items"] if item["kind"] == state_kind)
        if item["created"] is not None:
            _fail("published state cannot be discarded as a stage")
        image_id, revision = component["image"]["id"], component["image"]["revision"]
        current = preimage(root, image_id=image_id, image_revision=revision, snapshot=snapshot,
                           host_path=context["stage_host"], transaction_id=transaction_id)
        if not current["present"]:
            return
        claim_name = f"{transaction_id}-{state_kind}-forward.json"
        with manifest._directory(root, (".deployments", "component-state-copy-claims"), create=False) as directory:
            if manifest._read_file(directory, claim_name) != manifest._encode(context["claim"]):
                _fail("state forward stage claim differs")
        request = {**context["request"], "stage_before": item["forward_stage_preimage"]}
        result = _execute(root, journal, kind="state-copy-discard", image_id=image_id, revision=revision,
                          user=context["user"], working_dir=snapshot["working_dir"] or "/", mounts=context["mounts"],
                          settings=dict.fromkeys(_PATH_KEYS), input_path=request["source"],
                          flavor="legacy" if component["provenance"]["kind"] == "legacy-image" else "wheel",
                          state_kind=state_kind, copy_request=request)
        if result != {"present": False, "sha256": None, "stat": None}:
            _fail("state forward stage remains after discard")
    except (runtime.JournalError, manifest.ManifestError, observe.ObservationError, admission.AdmissionError,
            OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise StateError("state forward stage could not be discarded") from None
