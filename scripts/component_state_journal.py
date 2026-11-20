#!/usr/bin/env python3
"""Validate durable Telegram state-probe records and their commands."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

import component_journal_runtime as runtime
import product_manifest as manifest
from component_state_paths import StateError, _fail


_PATH_KEYS = ("WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH", "WUWATERM_CHANNEL_REPLY_INDEX_PATH")


# Schema-5 journals use only helpers whose programs do not write state; the
# copy, endpoint and FULL4 canonical helpers stay schema-3/4-only.
SCHEMA5_HELPER_KINDS = frozenset({"state-plan", "state-legacy-plan", "state-preimage", "state-canonical"})


_PROTECTED = tuple(PurePosixPath(p) for p in (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev", "/app/.venv",
))


# The planner is the installed product implementation verified by the
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


# A legacy image has no state_plan. Resolve its paths with persistent binds
# read-only and report any copy it would make at startup. Settings referents
# and reply-index directory entries have different semantics.
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


# O_NOATIME prevents a readback from changing filesystem metadata.
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


def _safe_target(path: str) -> None:
    target = PurePosixPath(manifest._path(path, "probe mount target"))
    if any(target.is_relative_to(item) or item.is_relative_to(target) for item in _PROTECTED):
        _fail("state mount could shadow probe code or system files")


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
