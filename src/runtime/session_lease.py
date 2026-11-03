"""Opt-in exclusive ownership of one private cursor and journal session.

Only cooperating versions of this runtime are fenced. A trusted supervisor
must create a fresh session root, keep it private, and never rename or mutate
its marker. This is not a sandbox against other processes of the same UID.
"""
import fcntl
import os
import re
import secrets
import stat
import threading
from pathlib import Path

from .lifecycle import _open_directory, _check_directory
from .files import atomic_json, private_json


class SessionLeaseError(RuntimeError):
    pass


_MARKER = '.phase4-managed-session'
_CONTENTS = b'phase4-managed-session-v1\n'
_CATALOG = '.phase4-journals.json'


def _marker(root):
    try:
        fd = os.open(root / _MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or os.read(fd, len(_CONTENTS) + 1) != _CONTENTS):
            raise SessionLeaseError('invalid managed session marker')
        return True
    finally:
        os.close(fd)


class SessionLease:
    """One nonblocking flock held for the complete supervisor session."""
    def __init__(self, root, *, enabled=False):
        if enabled is not True:
            raise SessionLeaseError('managed session feature disabled')
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.journal = self.root / 'journal'
        self.cursor = self.root / 'cursor.json'
        self.owner_pid = os.getpid()
        self.owner_thread = threading.get_ident()
        self.cursor_claimed = False
        self._native_reader = None
        self._reader_claimed = False
        self._native_generation = None
        self.fd = _open_directory(str(self.root))
        self.journal_fd = None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not _marker(self.root):
                raise SessionLeaseError('session marker missing')
            self._catalog = self._read_catalog()
            self.journal = self.root / self._catalog['journals'][-1]['name']
            self.journal_fd = _open_directory(str(self.journal))
            self.check()
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(cls, root, *, enabled=False):
        """Create a new empty, private, supervisor-owned session root."""
        if enabled is not True:
            raise SessionLeaseError('managed session feature disabled')
        root = Path(os.path.abspath(os.fspath(root)))
        os.mkdir(root, 0o700)
        root_fd = _open_directory(str(root))
        try:
            os.mkdir('journal', 0o700, dir_fd=root_fd)
            fd = os.open(_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=root_fd)
            try:
                os.fchmod(fd, 0o600)
                if os.write(fd, _CONTENTS) != len(_CONTENTS):
                    raise OSError('short session marker write')
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
            parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.close(root_fd)
        return cls(root, enabled=True)

    def _read_catalog(self):
        """Read all retained journal identities; unlisted directories block use."""
        catalog_path = self.root / _CATALOG
        if not catalog_path.exists() and not catalog_path.is_symlink():
            info = os.stat('journal', dir_fd=self.fd, follow_symlinks=False)
            value = {'version': 1, 'journals': [{'name':'journal', 'device':info.st_dev, 'inode':info.st_ino}]}
        else:
            value = private_json(catalog_path, limit=131072)
        if (type(value) is not dict or set(value) != {'version','journals'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['journals']) is not list or not 1 <= len(value['journals']) <= 1024):
            raise SessionLeaseError('journal catalog rejected')
        names = set()
        for index, entry in enumerate(value['journals']):
            if (type(entry) is not dict or set(entry) != {'name','device','inode'}
                    or type(entry['name']) is not str
                    or (entry['name'] != 'journal' if index == 0 else
                        re.fullmatch(r'journal-[0-9a-f]{32}',entry['name']) is None)
                    or entry['name'] in names or type(entry['device']) is not int or entry['device'] < 0
                    or type(entry['inode']) is not int or entry['inode'] <= 0):
                raise SessionLeaseError('journal catalog rejected')
            names.add(entry['name'])
            fd = _open_directory(str(self.root / entry['name']))
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (entry['device'], entry['inode']):
                    raise SessionLeaseError('retained journal identity changed')
            finally:
                os.close(fd)
        disk_names = {name for name in os.listdir(self.fd) if name == 'journal' or name.startswith('journal-')}
        if disk_names != names:
            raise SessionLeaseError('unlisted journal directory')
        return value

    def _advance_journal(self):
        """Publish a fresh journal slot after the caller verified handoff."""
        self.check()
        if len(self._catalog['journals']) >= 1024:
            raise SessionLeaseError('journal catalog full')
        name = 'journal-' + secrets.token_hex(16)
        os.mkdir(name, 0o700, dir_fd=self.fd)
        new_fd = None
        try:
            os.fsync(self.fd)
            new_fd = _open_directory(str(self.root/name))
            info = os.fstat(new_fd)
            catalog = {'version': 1, 'journals': self._catalog['journals'] +
                       [{'name':name,'device':info.st_dev,'inode':info.st_ino}]}
            atomic_json(self.root/_CATALOG,catalog)
            os.fsync(self.fd)
            old_fd = self.journal_fd
            self._catalog = catalog
            self.journal = self.root/name
            self.journal_fd = new_fd
            new_fd = None
            self._native_reader = None
            self._native_generation = None
            self._reader_claimed = False
            os.close(old_fd)
            self.check()
            return self.journal
        finally:
            if new_fd is not None:os.close(new_fd)

    def check(self, journal=None, cursor=None):
        if self.fd is None or self.journal_fd is None or self.owner_pid != os.getpid() or self.owner_thread != threading.get_ident():
            raise SessionLeaseError('lease unavailable or inherited across fork')
        try:
            info = os.fstat(self.fd)
            with open(f'/proc/self/fdinfo/{self.fd}', encoding='ascii') as source:
                lines = source.read(65537)
            if len(lines) > 65536:
                raise SessionLeaseError('lease fdinfo too large')
            lock = f'FLOCK  ADVISORY  WRITE {self.owner_pid} '
            device = f'{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}'
            if not any(line.startswith('lock:') and lock in line and
                       line.split()[-3:] == [device, '0', 'EOF']
                       for line in lines.splitlines()):
                raise SessionLeaseError('lease lock not held by this descriptor')
            _check_directory(str(self.root), self.fd)
            if self._read_catalog() != self._catalog:
                raise SessionLeaseError('journal catalog changed')
            _check_directory(str(self.journal), self.journal_fd)
            if not _marker(self.root):
                raise SessionLeaseError('session marker missing')
            if journal is not None and Path(os.path.abspath(os.fspath(journal))) != self.journal:
                raise SessionLeaseError('journal outside session')
            if cursor is not None and Path(os.path.abspath(os.fspath(cursor))) != self.cursor:
                raise SessionLeaseError('cursor outside session')
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, SessionLeaseError):
                raise
            raise SessionLeaseError('session identity unavailable') from exc

    def require_native(self, generation):
        self.check()
        if self._native_reader is None or self._native_generation != generation:
            raise SessionLeaseError('durable native session binding required')
        self._native_reader._verify()

    def close(self):
        if self.journal_fd is not None:
            os.close(self.journal_fd)
            self.journal_fd = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_):
        self.close()


def require_cursor(path, lease):
    path = Path(os.path.abspath(os.fspath(path)))
    if lease is not None:
        if type(lease) is not SessionLease:
            raise SessionLeaseError('invalid lease')
        lease.check(cursor=path)
    elif _marker(path.parent):
        raise SessionLeaseError('managed cursor requires lease')


def require_journal(path, lease):
    path = Path(os.path.abspath(os.fspath(path)))
    if lease is not None:
        if type(lease) is not SessionLease:
            raise SessionLeaseError('invalid lease')
        lease.check(journal=path)
    elif _marker(path.parent):
        raise SessionLeaseError('managed journal requires lease')
