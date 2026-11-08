#!/usr/bin/env python3
"""FULL4 state-copy subprocess code; this module does not admit operations.

``COPY_CODE`` is self-contained and takes exactly one JSON argv under
``python -I -B -c``. The caller MUST first validate the FULL4 journal, schema-2
claim and all endpoints; reconcile saved A's probe, hold FD9, stop readers,
verify the source's complete endpoint in its own namespace, bind the exact
image/recipe/mounts/actual UID and any read-only extra, and authorize the
operation and durable phase. Those facts cannot be established by JSON alone.
The caller MUST retain the result durably before advancing each phase. Mount
read-only flags are assertions checked by admission/Docker, not detectable
from the ordinary path operations here. No guarantee is made against a writer
outside this protocol or a hostile process with write access to the mounts.

Request v1 has exactly these fields:

* ``schema_version``: integer 1; ``action``: reserve, write, publish, cleanup, discard.
* ``phase``: intent for reserve/cleanup/discard, reserved or writing for write,
  publish-intent for publish. The caller binds this to durable journal state.
* ``binding``: exact schema-2 copy binding in component_full._copy_progress_v4;
  ``binding_sha256``: SHA-256 of its sorted, indented, ASCII JSON plus newline.
* ``source_endpoint`` and ``destination_before``: journaled FULL4 endpoints.
  Their identically encoded hashes must match the binding. The copy source is
  their source endpoint's referent, never a re-resolved link through an alias.
* ``mounts``: the exact admitted original recipe mounts plus the admitted
  read-only alias when needed, in the ENDPOINT_CODE format. ``read_path`` is
  either the source referent in its original mapping, or its basename under a
  read-only bind of its recorded physical parent. For cleanup, it instead
  identifies cleanup.old_published_endpoint's referent.
* ``flavor``: legacy or wheel; the appropriate image-native validator must exist.
* ``reservation``: null on reserve/cleanup, otherwise the endpoint from reserve;
  ``staged``: endpoint from write on publish and staged/publish-intent discard,
  otherwise null.
* ``cleanup``: null except for cleanup, when it has exactly
  ``created_entry_claim_sha256``, ``post_stop_new_endpoint`` and
  ``old_published_endpoint``. All three are durable journal evidence. The
  first is the forward binding hash. The caller binds post-stop to the latest
  stopped NEW endpoint and old-published to the durable old copy-back result.
* ``discard``: null except for discard, when it is exactly ``binding_sha256``
  and ``copy_phase``. The hash equals the request binding hash; ``copy_phase``
  is reserved, writing, staged or publish-intent. The caller binds these to a
  durable discard intent and the immutable recorded copy progress. Only an
  unpublished forward stage with an exact original destination is eligible.
* ``reconcile_only`` is true only for publication recovery, where an existing
  stage must be refused instead of performing a new rename.

Result v1 is exactly ``schema_version``, ``action``, ``binding_sha256``,
``reconciled`` (whether the side effect preceded this invocation), and
``endpoint`` (the stage endpoint for reserve/write/discard, destination endpoint
for publish/cleanup). Only identity, mapping and hashes are returned, never state
bytes. Refusal prints a generic message on stderr, no result, and exits 1.
For cleanup the returned endpoint is the new endpoint after removal; for discard
the returned endpoint records an absent stage.

The caller must derive the claim from the journal, validate result endpoints,
record reservations before write and stages before publication, record cleanup
or discard intent before unlink, and perform native readback/durable journal
transitions. Absent-stage discard reconciliation requires that same durable
intent and its previously recorded reservation, not absence alone.
The caller also verifies image identity, mounts_sha256, RO alias provenance and
that the claim was for a stage known absent before reservation. This primitive
cannot infer any of those historical facts from the current filesystem.
"""
from __future__ import annotations

try:
    from .component_state_endpoint import ENDPOINT_CODE
except ImportError:  # imported as a top-level module by the saved source capsule
    from component_state_endpoint import ENDPOINT_CODE

# Include the resolver itself, not an import of checkout code, in the saved A
# probe. Keep the end-of-program entrypoint out of the combined script.
_ENDPOINT_LIBRARY = ENDPOINT_CODE[:ENDPOINT_CODE.index("\ntry:\n if len(sys.argv)!=2: fail()")]

COPY_CODE = _ENDPOINT_LIBRARY + r'''
import contextlib,ctypes,pathlib
EMPTY=hashlib.sha256(b'').hexdigest()
STABLE=('device','inode','mode','uid','gid','nlink')
RENAMED=('device','inode','size','mtime_ns','mode','uid','gid','nlink')
BINDING_KEYS={'schema_version','operation','transaction_id','state_kind','direction','source_endpoint_sha256',
 'destination_before_sha256','image_id','image_revision','user','recipe_sha256','mounts_sha256',
 'stage_path','stage_parent_identity','expected_raw_sha256'}
REQUEST_KEYS={'schema_version','action','phase','binding','binding_sha256','source_endpoint','destination_before',
 'read_path','mounts','flavor','reservation','staged','cleanup','discard','reconcile_only'}
def obj(x,keys):
 if type(x) is not dict or set(x)!=keys: fail()
 return x
def digest(x):
 return hashlib.sha256((json.dumps(x,ensure_ascii=True,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('ascii')).hexdigest()
def hex64(x):
 if type(x) is not str or not re.fullmatch('[0-9a-f]{64}',x): fail()
 return x
def same_fields(a,b,keys):
 return type(a) is dict and type(b) is dict and all(a.get(k)==b.get(k) for k in keys)
def request_for(endpoint,mounts,path=None):
 return {'kind':endpoint['kind'],'recipe_sha256':endpoint['recipe_sha256'],'image_id':endpoint['image_id'],
  'image_revision':endpoint['image_revision'],'user':endpoint['user'],'working_dir':endpoint['working_dir'],
  'configured_path':endpoint['configured_path'] if path is None else None,
  'effective_path':endpoint['effective_path'] if path is None else path,'mounts':mounts,'require_writer':True}
def current(endpoint,mounts,path=None):
 request=request_for(endpoint,mounts,path)
 return collect(request,mounts)
def absinfo(path):
 try: return os.stat(os.fsencode(path),follow_symlinks=False)
 except FileNotFoundError: return None
def safe_parent(path,expected):
 fd=physical_dir(posixpath.dirname(path))
 if parent_identity(os.fstat(fd))!=expected:
  os.close(fd); fail()
 return fd
def named_parent(path,expected):
 fd=safe_parent(path,expected); os.close(fd)
def file_at(path,parent,expected_stat=None,expected_hash=None,keep=False):
 fd=safe_parent(path,parent)
 try:
  name=os.fsencode(posixpath.basename(path))
  f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=fd)
  try:
   before=os.fstat(f)
   if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX: fail()
   if expected_stat is not None and identity(before)!=expected_stat: fail()
   data=bytearray(); h=hashlib.sha256()
   while True:
    chunk=os.read(f,131072)
    if not chunk: break
    data.extend(chunk); h.update(chunk)
    if len(data)>MAX: fail()
   after=os.fstat(f); named=os.stat(name,dir_fd=fd,follow_symlinks=False)
   if identity(before)!=identity(after) or identity(after)!=identity(named) or len(data)!=after.st_size: fail()
   if expected_hash is not None and h.hexdigest()!=expected_hash: fail()
   named_parent(path,parent)
   return (identity(after),h.hexdigest(),data if keep else None)
  finally: os.close(f)
 finally: os.close(fd)
def read_endpoint_ref(endpoint,path,mounts,keep=False):
 ref=endpoint['referent']; absolute(path)
 if ref['present'] is not True or type(ref['stat']) is not dict: fail()
 mount=mount_for(path,mounts)
 if 'host' not in mount: fail()
 original=(mount==ref['mount_identity'] and path==ref['container_path'])
 alias=(mount['read_only'] is True and mount['host']==posixpath.dirname(ref['host_path'])
  and path==mount['container'].rstrip('/')+'/'+posixpath.basename(ref['host_path'])
  and (mount['device'],mount['inode'])==(ref['parent_identity']['device'],ref['parent_identity']['inode']))
 if not (original or alias): fail()
 if mount_for(path,mounts)!=mount: fail()
 return file_at(path,ref['parent_identity'],ref['stat'],ref['sha256'],keep)
def stage_check(record,destination,stage_path,expected_hash=None):
 if type(record) is not dict or record.get('kind')!=destination['kind']: fail()
 for k in ('image_id','image_revision','user','recipe_sha256','working_dir'):
  if record.get(k)!=destination[k]: fail()
 entry=record.get('entry'); ref=record.get('referent'); writer=record.get('writer')
 if (type(entry) is not dict or type(ref) is not dict or type(writer) is not dict
  or record.get('effective_path')!=stage_path or entry.get('container_path')!=stage_path
  or entry.get('host_path')!=binding['stage_path'] or entry.get('type')!='regular'
  or record.get('links')!=[] or ref.get('present') is not True or ref.get('stat')!=entry.get('lstat')
  or ref.get('container_path')!=stage_path or ref.get('host_path')!=binding['stage_path']
  or entry.get('parent_identity')!=binding['stage_parent_identity']
  or ref.get('parent_identity')!=binding['stage_parent_identity']
  or entry.get('mount_identity')!=destination['writer']['mount_identity']
  or ref.get('mount_identity')!=entry.get('mount_identity') or writer.get('container_path')!=stage_path): fail()
 info=ref['stat']; uid,gid=map(int,binding['user'].split(':'))
 if (type(info) is not dict or not stat.S_ISREG(info['mode']) or stat.S_IMODE(info['mode'])!=0o600
  or info['uid']!=uid or info['gid']!=gid or info['nlink']!=1): fail()
 hex64(ref['sha256'])
 if expected_hash is not None and ref['sha256']!=expected_hash: fail()
 return info
def validate_native(path):
 # Native readers can log malformed values. Only the generic refusal may leave
 # this probe; neither validator diagnostics nor state bytes are probe output.
 with open('/dev/null','w') as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
  if request['flavor']=='legacy':
   from wuwaterm.bot import _validate_state_file
   _validate_state_file(pathlib.Path(path),label='chat settings' if binding['state_kind']=='chat-settings' else 'channel reply index')
  elif request['flavor']=='wheel':
   from wuwaterm_telegram.state_validation import main
   if main([path,binding['state_kind']])!=0: fail()
  else: fail()
def verify_reply_referent_before(mounts):
 ref=destination['referent']
 if destination['kind']=='channel-replies' and destination['links']:
  # The first entry was replaced. Remaining links and the old referent must
  # retain their recorded identities even though the entry no longer reaches them.
  for link in destination['links'][1:]:
   observed=node(link['container_path'],mounts,destination['entry']['mount_identity'])
   if observed['type']!='symlink' or {k:v for k,v in observed.items() if k!='type'}!=link: fail()
  path=ref['container_path']
  if mount_for(path,mounts)!=ref['mount_identity']: fail()
  file_at(path,ref['parent_identity'],ref['stat'],ref['sha256'])
def check_published(observed,staged,mounts):
 old=destination; stage_ref=staged['referent']; new_ref=observed['referent']
 for k in ('schema_version','kind','recipe_sha256','image_id','image_revision','user','working_dir',
           'configured_path','effective_path','writer'):
  if observed[k]!=old[k]: fail()
 if (new_ref['sha256']!=stage_ref['sha256'] or not new_ref['present']
  or not same_fields(new_ref['stat'],stage_ref['stat'],RENAMED)): fail()
 if old['writer']['role']=='referent' and old['links']:
  if observed['entry']!=old['entry'] or observed['links']!=old['links']: fail()
 else:
  if observed['entry']['type']!='regular' or observed['links'] or observed['entry']['lstat']!=new_ref['stat']: fail()
  if any(observed['entry'][k]!=old['entry'][k] for k in ('container_path','host_path','mount_identity','parent_identity')): fail()
 if any(new_ref[k]!=old['referent'][k] for k in ('container_path','host_path','mount_identity','parent_identity')):
  # A reply link is replaced at its entry, so its new referent is the old entry.
  if old['writer']['role']!='entry' or any(new_ref[k]!=old['entry'][k] for k in ('container_path','host_path','mount_identity','parent_identity')): fail()
 verify_reply_referent_before(mounts)
def renamed_noreplace(parent,src,dst):
 fn=ctypes.CDLL(None,use_errno=True).renameat2
 fn.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint); fn.restype=ctypes.c_int
 if fn(parent,src,parent,dst,1)!=0: raise OSError(ctypes.get_errno(),'renameat2')
def source_check(keep=False):
 verify_mounts(mounts)
 return read_endpoint_ref(source,request['read_path'],mounts,keep)
def before_check():
 verify_mounts(mounts)
 if current(destination,mounts)!=destination: fail()
def parse_copy():
 global request,binding,source,destination,mounts,stage_path
 if len(sys.argv)!=2: fail()
 request=obj(json.loads(sys.argv[1],object_pairs_hook=unique,parse_constant=lambda _:fail()),REQUEST_KEYS)
 if type(request['schema_version']) is not int or request['schema_version']!=1: fail()
 binding=obj(request['binding'],BINDING_KEYS); source=request['source_endpoint']; destination=request['destination_before']
 if type(binding['schema_version']) is not int or binding['schema_version']!=2 or binding['operation']!='state-copy': fail()
 txn=binding['transaction_id']; kind=binding['state_kind']; direction=binding['direction']
 if type(txn) is not str or not re.fullmatch('[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}',txn): fail()
 if kind not in ('chat-settings','channel-replies') or direction not in ('forward','rollback'): fail()
 for key in ('source_endpoint_sha256','destination_before_sha256','recipe_sha256','mounts_sha256','expected_raw_sha256'):
  hex64(binding[key])
 if (digest(binding)!=hex64(request['binding_sha256']) or digest(source)!=binding['source_endpoint_sha256']
  or digest(destination)!=binding['destination_before_sha256']): fail()
 if (source['kind']!=kind or destination['kind']!=kind or source['referent']['present'] is not True
  or source['referent']['sha256']!=binding['expected_raw_sha256']): fail()
 if destination['referent']['present'] and same_fields(source['referent']['stat'],destination['referent']['stat'],('device','inode')): fail()
 for key in ('image_id','image_revision','user','recipe_sha256'):
  if destination[key]!=binding[key]: fail()
 writer=destination['writer']; role='referent' if kind=='chat-settings' else 'entry'
 if writer['role']!=role or writer['can_replace'] is not True or writer['mount_identity']['read_only'] is not False: fail()
 if not destination['referent']['present'] and destination['entry']['type']!='absent': fail()
 stage_name='.component-full-'+txn+'-'+kind+'.'+direction
 if binding['stage_path']!=posixpath.join(posixpath.dirname(writer['host_path']),stage_name): fail()
 if binding['stage_parent_identity']!=writer['parent_identity']: fail()
 stage_path=posixpath.join(posixpath.dirname(writer['container_path']),stage_name)
 if stage_path in (writer['container_path'],request['read_path']): fail()
 action=request['action']; phase=request['phase']
 allowed={'reserve':('intent',),'write':('reserved','writing'),'publish':('publish-intent',),
  'cleanup':('intent',),'discard':('intent',)}
 if action not in allowed or phase not in allowed[action] or request['flavor'] not in ('legacy','wheel'): fail()
 if type(request['reconcile_only']) is not bool or (request['reconcile_only'] and action!='publish'): fail()
 if (action=='reserve')!=(request['reservation'] is None) and action!='cleanup': fail()
 if (action=='discard')!=(request['discard'] is not None): fail()
 if action=='discard':
  discard=obj(request['discard'],{'binding_sha256','copy_phase'})
  if (direction!='forward' or discard['binding_sha256']!=request['binding_sha256']
   or discard['copy_phase'] not in ('reserved','writing','staged','publish-intent')): fail()
  complete=discard['copy_phase'] in ('staged','publish-intent')
 else: complete=False
 if (action=='publish' or complete)!=(request['staged'] is not None): fail()
 if (action=='cleanup')!=(request['cleanup'] is not None): fail()
 if action=='cleanup' and (direction!='forward' or request['reservation'] is not None): fail()
 if direction=='forward' and destination['entry']['type']!='absent': fail()
 if direction=='rollback' and not destination['referent']['present']: fail()
 read_ref=(request['cleanup']['old_published_endpoint']['referent'] if action=='cleanup' else source['referent'])
 read_mount=mount_for(request['read_path'],request['mounts'])
 alias=(read_mount if read_mount.get('read_only') is True and read_mount.get('host')==posixpath.dirname(read_ref['host_path'])
  and request['read_path']==read_mount['container'].rstrip('/')+'/'+posixpath.basename(read_ref['host_path'])
  and (read_mount['device'],read_mount['inode'])==(read_ref['parent_identity']['device'],read_ref['parent_identity']['inode'])
  and read_mount!=read_ref['mount_identity'] else None)
 mounts=parse(request_for(destination,request['mounts']),read_alias=alias)
 if mount_for(writer['container_path'],mounts)!=writer['mount_identity']: fail()
 if mount_for(stage_path,mounts)!=writer['mount_identity']: fail()
 if request['reservation'] is not None:
  reserve_info=stage_check(request['reservation'],destination,stage_path,EMPTY)
  if reserve_info['size']!=0: fail()
  if same_fields(reserve_info,source['referent']['stat'],('device','inode')): fail()
 if request['staged'] is not None:
  stage_info=stage_check(request['staged'],destination,stage_path,binding['expected_raw_sha256'])
  if not same_fields(reserve_info,stage_info,STABLE): fail()
 return action
def do_reserve():
 source_stat,_,_=source_check(); before_check(); parent=safe_parent(stage_path,binding['stage_parent_identity'])
 name=os.fsencode(posixpath.basename(stage_path)); existed=False
 try:
  try: f=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
  except FileExistsError:
   existed=True; f=os.open(name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
  try:
   s=os.fstat(f); uid,gid=map(int,binding['user'].split(':'))
   if (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_size!=0 or stat.S_IMODE(s.st_mode)!=0o600
    or (s.st_uid,s.st_gid)!=(uid,gid) or identity(s)!=identity(os.stat(name,dir_fd=parent,follow_symlinks=False))): fail()
   if same_fields(identity(s),source_stat,('device','inode')): fail()
   os.fsync(f); os.fsync(parent)
  finally: os.close(f)
 finally: os.close(parent)
 result=current(destination,mounts,stage_path); stage_check(result,destination,stage_path,EMPTY)
 source_check(); before_check()
 return result,existed
def do_write():
 source_stat,_,data=source_check(True); before_check()
 reserved=stage_check(request['reservation'],destination,stage_path,EMPTY)
 parent=safe_parent(stage_path,binding['stage_parent_identity']); name=os.fsencode(posixpath.basename(stage_path))
 try:
  f=os.open(name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
  try:
   observed=identity(os.fstat(f)); named=identity(os.stat(name,dir_fd=parent,follow_symlinks=False))
   if observed!=named or not same_fields(observed,reserved,STABLE) or observed['size']>MAX: fail()
   source_check(); before_check(); named_parent(stage_path,binding['stage_parent_identity'])
   os.ftruncate(f,0); os.lseek(f,0,os.SEEK_SET)
   view=memoryview(data)
   while view:
    n=os.write(f,view)
    if n<=0: fail()
    view=view[n:]
   os.fsync(f)
   if not same_fields(identity(os.fstat(f)),reserved,STABLE): fail()
   validate_native(stage_path)
   if not same_fields(identity(os.fstat(f)),reserved,STABLE): fail()
   os.fsync(f); os.fsync(parent)
  finally: os.close(f)
 finally: os.close(parent)
 result=current(destination,mounts,stage_path); info=stage_check(result,destination,stage_path,binding['expected_raw_sha256'])
 if not same_fields(info,reserved,STABLE) or info['size']!=source_stat['size']: fail()
 source_check(); before_check()
 return result,False
def do_publish():
 source_check(); staged=request['staged']; expected=stage_check(staged,destination,stage_path,binding['expected_raw_sha256'])
 parent=safe_parent(stage_path,binding['stage_parent_identity']); name=os.fsencode(posixpath.basename(stage_path))
 target=os.fsencode(posixpath.basename(destination['writer']['container_path']))
 try:
  if absinfo(stage_path) is None:
   observed=current(destination,mounts); check_published(observed,staged,mounts)
   validate_native(destination['writer']['container_path']); source_check()
   check_published(current(destination,mounts),staged,mounts); os.fsync(parent)
   return current(destination,mounts),True
  if request['reconcile_only']: fail()
  before_check()
  if current(destination,mounts,stage_path)!=staged: fail()
  validate_native(stage_path); source_check(); before_check()
  if current(destination,mounts,stage_path)!=staged: fail()
  named_parent(stage_path,binding['stage_parent_identity']); verify_mounts(mounts)
  if binding['direction']=='forward': renamed_noreplace(parent,name,target)
  else: os.replace(name,target,src_dir_fd=parent,dst_dir_fd=parent)
  os.fsync(parent)
 finally: os.close(parent)
 observed=current(destination,mounts); check_published(observed,staged,mounts); source_check()
 if absinfo(stage_path) is not None: fail()
 return observed,False
def do_cleanup():
 cleanup=obj(request['cleanup'],{'created_entry_claim_sha256','post_stop_new_endpoint','old_published_endpoint'})
 if cleanup['created_entry_claim_sha256']!=request['binding_sha256']: fail()
 post=cleanup['post_stop_new_endpoint']; old=cleanup['old_published_endpoint']
 if (post['kind']!=destination['kind'] or old['kind']!=destination['kind']
  or post['entry']['type']!='regular' or post['links'] or not post['referent']['present']
  or old['referent']['present'] is not True or old['referent']['sha256']!=post['referent']['sha256']): fail()
 if same_fields(old['referent']['stat'],post['referent']['stat'],('device','inode')): fail()
 if mount_for(request['read_path'],mounts)['read_only'] is not True or absinfo(stage_path) is not None: fail()
 for k in ('kind','recipe_sha256','image_id','image_revision','user','working_dir','configured_path','effective_path'):
  if post[k]!=destination[k]: fail()
 for k in ('role','container_path','host_path','mount_identity','parent_identity'):
  if post['writer'][k]!=destination['writer'][k]: fail()
 for k in ('container_path','host_path','mount_identity','parent_identity'):
  if post['entry'][k]!=destination['entry'][k]: fail()
 def absent_result():
  observed=current(destination,mounts)
  for k in ('schema_version','kind','recipe_sha256','image_id','image_revision','user','working_dir',
            'configured_path','effective_path'):
   if observed[k]!=post[k]: fail()
  if (observed['entry']!=destination['entry'] or observed['referent']!=destination['referent']
   or observed['links']!=[] or observed['writer']!=post['writer']): fail()
  return observed
 def proof():
  verify_mounts(mounts); read_endpoint_ref(old,request['read_path'],mounts)
  validate_native(request['read_path']); read_endpoint_ref(old,request['read_path'],mounts)
 proof()
 path=destination['entry']['container_path']; parent=safe_parent(path,destination['entry']['parent_identity'])
 name=os.fsencode(posixpath.basename(path))
 try:
  if absinfo(path) is None:
   result=absent_result()
   proof(); os.fsync(parent); return result,True
  if current(post,mounts)!=post: fail()
  proof()
  if current(post,mounts)!=post or absinfo(stage_path) is not None: fail()
  named_parent(path,destination['entry']['parent_identity']); verify_mounts(mounts)
  os.unlink(name,dir_fd=parent); os.fsync(parent)
 finally: os.close(parent)
 proof()
 return absent_result(),False
def do_discard():
 # The caller has durably recorded discard intent and the reservation (and,
 # after staging, the complete staged endpoint) before starting this probe.
 source_check(); before_check()
 reserved=stage_check(request['reservation'],destination,stage_path,EMPTY)
 complete=request['discard']['copy_phase'] in ('staged','publish-intent')
 parent=safe_parent(stage_path,binding['stage_parent_identity'])
 name=os.fsencode(posixpath.basename(stage_path))
 try:
  try: f=os.open(name,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
  except FileNotFoundError:
   source_check(); before_check(); named_parent(stage_path,binding['stage_parent_identity'])
   if absinfo(stage_path) is not None: fail()
   os.fsync(parent)
   result=current(destination,mounts,stage_path)
   if result['entry']['type']!='absent' or result['referent']['present']: fail()
   return result,True
  try:
   observed=identity(os.fstat(f)); named=identity(os.stat(name,dir_fd=parent,follow_symlinks=False))
   if observed!=named or not same_fields(observed,reserved,STABLE): fail()
   if complete and current(destination,mounts,stage_path)!=request['staged']: fail()
   source_check(); before_check(); named_parent(stage_path,binding['stage_parent_identity']); verify_mounts(mounts)
   if identity(os.fstat(f))!=identity(os.stat(name,dir_fd=parent,follow_symlinks=False)): fail()
   if not same_fields(identity(os.fstat(f)),reserved,STABLE): fail()
   if complete and current(destination,mounts,stage_path)!=request['staged']: fail()
   os.unlink(name,dir_fd=parent); os.fsync(parent)
  finally: os.close(f)
 finally: os.close(parent)
 source_check(); before_check()
 result=current(destination,mounts,stage_path)
 if result['entry']['type']!='absent' or result['referent']['present']: fail()
 return result,False
try:
 os.umask(0o077)
 action=parse_copy()
 result,reconciled={'reserve':do_reserve,'write':do_write,'publish':do_publish,
  'cleanup':do_cleanup,'discard':do_discard}[action]()
 print(json.dumps({'schema_version':1,'action':action,'binding_sha256':request['binding_sha256'],
  'reconciled':reconciled,'endpoint':result},sort_keys=True,ensure_ascii=True,allow_nan=False))
except Exception:
 print('state copy refused',file=sys.stderr); sys.exit(1)
'''
