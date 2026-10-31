"""Trusted runner-side snapshot program. Not agent instructions or shell input.

Only durable Codex thread state is portable; generated config, credentials,
logs, shell snapshots and plugin/skill caches are never exported. SQLite backup
is performed only after confirmed process/lease shutdown, including its WAL.
"""

from __future__ import annotations

# Kept standalone so the exact reviewed program can run with python3 -I -c.
STATE_PROGRAM = r"""
import base64,hashlib,json,os,pathlib,re,shutil,sqlite3,stat,sys,tempfile
root=pathlib.Path(sys.argv[2])
pattern=r'/workspace/\.opencuria/harness/codex/[0-9a-f-]{36}/config'
assert re.fullmatch(pattern,str(root))
MAX=24*1024*1024
DB=re.compile(r'(?:state|thread_history|queue|goals|memories(?:_v2)?)_\d+\.sqlite')
def dirs(p):
    cur=pathlib.Path('/')
    for part in p.parts[1:]:
        cur=cur/part
        if cur.exists() or cur.is_symlink():
            assert stat.S_ISDIR(cur.lstat().st_mode) and not cur.is_symlink()
        else: cur.mkdir(mode=0o700)
def regular(p,limit=MAX):
    s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<=limit
    return s
def valid(name):
    if DB.fullmatch(name): return True
    memory=r'(?:memories|memories_v2)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md'
    if re.fullmatch(memory,name): return True
    pattern=(r'(?:sessions|archived_sessions)/\d{4}/\d{2}/\d{2}/'
             r'rollout-[A-Za-z0-9T_.:-]+\.jsonl')
    return bool(re.fullmatch(pattern,name))
def read(p,limit=MAX):
    regular(p,limit)
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        s=os.fstat(fd);assert s.st_nlink==1 and s.st_size<=limit
        with os.fdopen(fd,'rb',closefd=False) as f: data=f.read(limit+1)
        assert len(data)<=limit
        return data
    finally: os.close(fd)
dirs(root.parent)
os.umask(0o077)
action=sys.argv[1]
if action=='prepare':
    # Remove any upload target first; never open a workspace-created hardlink.
    target=root.parent/'restore.json'
    if target.exists() or target.is_symlink(): target.unlink()
    for target in root.parent.glob('restore-*.part'):
        assert re.fullmatch(r'restore-\d+\.part',target.name)
        target.unlink()
elif action=='assemble':
    count=int(sys.argv[3]);digest=sys.argv[4]
    assert 1<=count<=9 and re.fullmatch(r'[a-f0-9]{64}',digest)
    target=root.parent/'restore.json'
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    hasher=hashlib.sha256();total=0
    try:
        with os.fdopen(fd,'wb') as output:
            for index in range(count):
                part=root.parent/('restore-'+str(index)+'.part')
                data=read(part,4*1024*1024);total+=len(data)
                assert total<=34*1024*1024
                hasher.update(data);output.write(data);part.unlink()
            output.flush();os.fsync(output.fileno())
        assert hasher.hexdigest()==digest
    except BaseException:
        target.unlink();raise
elif action=='restore':
    record=json.loads(read(root.parent/'restore.json',34*1024*1024))
    assert record['home']==str(root) and record['version']==1
    files=record['files'];assert isinstance(files,list) and 0<len(files)<=256
    stage=pathlib.Path(tempfile.mkdtemp(prefix='.restore-',dir=root.parent))
    total=0;seen=set()
    try:
        for item in files:
            name=item['path'];assert valid(name) and name not in seen
            seen.add(name);data=base64.b64decode(item['data'],validate=True)
            total+=len(data);assert total<=MAX
            assert hashlib.sha256(data).hexdigest()==item['sha256']
            target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data);target.chmod(0o600)
        old=root.parent/'previous'
        assert not old.exists() and not old.is_symlink()
        if root.exists():
            dirs(root);os.rename(root,old)
        os.rename(stage,root)
        if old.exists(): shutil.rmtree(old)
    finally:
        if stage.exists(): shutil.rmtree(stage)
    (root.parent/'restore.json').unlink()
elif action=='fresh':
    # Fork/edit deliberately clears the retained runtime, never user workspace files.
    if root.exists() or root.is_symlink():
        dirs(root);shutil.rmtree(root)
    dirs(root)
elif action=='snapshot':
    dirs(root);files=[];total=0
    paths=list(root.iterdir())
    for subtree in ('sessions','archived_sessions','memories','memories_v2'):
        base=root/subtree
        if base.exists():
            dirs(base)
            for directory,children,names in os.walk(base,followlinks=False):
                assert len(paths)+len(children)+len(names)<=4096
                for name in children:
                    assert not (pathlib.Path(directory)/name).is_symlink()
                paths.extend(pathlib.Path(directory)/name for name in names)
    for p in sorted(paths):
        name=str(p.relative_to(root))
        if p.is_symlink(): raise ValueError('Symlink in managed Codex state')
        if not valid(name): continue
        regular(p)
        if DB.fullmatch(name):
            fd,tmp=tempfile.mkstemp(dir=root.parent);os.close(fd)
            try:
                src=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True)
                dst=sqlite3.connect(tmp);src.backup(dst);dst.close();src.close()
                data=read(pathlib.Path(tmp))
            finally: os.unlink(tmp)
        else: data=read(p)
        total+=len(data);assert total<=MAX and len(files)<256
        files.append({'path':name,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()})
    assert any(x['path'].startswith('sessions/') for x in files)
    payload=json.dumps({'version':1,'home':str(root),'files':files},separators=(',',':')).encode()
    target=root.parent/'snapshot.json'
    if target.exists() or target.is_symlink():
        regular(target);target.unlink()
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(payload)
    # Chunk reads stay within the shared no-follow media reader's 10 MiB cap.
    chunks=[]
    for index,offset in enumerate(range(0,len(payload),4*1024*1024)):
        chunk=root.parent/('snapshot-'+str(index)+'.part')
        if chunk.exists() or chunk.is_symlink():
            regular(chunk);chunk.unlink()
        fd=os.open(chunk,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(payload[offset:offset+4*1024*1024])
        chunks.append(str(index))
    target.unlink()
    print(hashlib.sha256(payload).hexdigest()+':'+str(len(chunks)))
elif action=='purge':
    for p in root.parent.glob('snapshot-*.part'):
        assert re.fullmatch(r'snapshot-\d+\.part',p.name)
        p.unlink()
elif action=='skills':
    bodies=json.loads(read(root.parent/'restore.json'))
    assert isinstance(bodies,list) and len(bodies)<=128
    base=root/'skills';dirs(base)
    for target in base.glob('opencuria-skill-*'):
        assert re.fullmatch(r'opencuria-skill-\d{3}',target.name)
        dirs(target);shutil.rmtree(target)
    for index,body in enumerate(bodies):
        assert isinstance(body,str) and len(body.encode())<=64*1024
        if not body.strip(): continue
        name='opencuria-skill-'+str(index+1).zfill(3)
        target=base/name;dirs(target)
        payload=body.strip()
        if not payload.startswith('---\n'):
            header='---\nname: '+name+'\ndescription: OpenCuria selected skill\n---\n\n'
            payload=header+payload
        fd=os.open(target/'SKILL.md',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(payload.encode())
    (root.parent/'restore.json').unlink()
elif action=='config':
    dirs(root)
    data=base64.b64decode(sys.argv[3],validate=True);assert len(data)<512*1024
    target=root/'config.toml'
    if target.exists() or target.is_symlink():
        regular(target);target.unlink()
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    assert os.fstat(fd).st_nlink==1
    with os.fdopen(fd,'wb') as f:f.write(data)
else: raise ValueError('Unknown state action')
"""
