"""Owned atomic runtime metadata; failures never echo private file contents."""
import json
import os
from pathlib import Path
import stat
import tempfile


def no_symlinks(path):
    path=Path(path).absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('RUNTIME_SYMLINK_REJECTED')
    return path


def private_directory(path):
    path=no_symlinks(path)
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('RUNTIME_DIRECTORY_REJECTED')
    return path


def private_bytes(path,limit=65536):
    path=no_symlinks(path)
    for attempt in range(3):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        try:
            info=os.fstat(fd)
            # Atomic replacement can unlink the opened inode before fstat.
            if info.st_nlink==0 and attempt<2:continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>limit:
                raise ValueError('RUNTIME_METADATA_REJECTED')
            data=os.read(fd,limit+1)
            if len(data)!=info.st_size:raise ValueError('RUNTIME_METADATA_CHANGED')
            return data
        finally:os.close(fd)


def private_json(path,limit=65536):
    try:
        obj=json.loads(private_bytes(path,limit))
        if not isinstance(obj,dict):raise ValueError()
        return obj
    except Exception:raise ValueError('RUNTIME_METADATA_REJECTED') from None


def atomic_text(path,text):
    path=no_symlinks(path);directory=private_directory(path.parent)
    if path.exists():private_bytes(path,1024*1024)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            out.write(text);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        try:os.unlink(temp)
        except FileNotFoundError:pass


def atomic_json(path,obj):
    atomic_text(path,json.dumps(obj,ensure_ascii=False,allow_nan=False)+'\n')
