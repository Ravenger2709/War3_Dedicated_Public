"""Offline opt-in acquisition of native startup metadata from an owned pipe.

This module does not integrate with the live controller or authorize recovery.
The caller must provide a trusted native executable that neither delegates its
stdout/journal writer nor execs an unrelated program after startup.
"""
from dataclasses import asdict
import io
import json
import os
import re
import select
import secrets
import stat
import time

from .lifecycle import _unique_pairs
from .process_ownership import OwnedChild, ProcessOwnershipError, _identity
from .reservation_marker import _rename_noreplace
from .session_lease import SessionLease


class NativeSessionError(RuntimeError):
    pass


_PREFIX = b'[HOSTBOT_SESSION] '
_FIELDS = {'version', 'pid', 'session_generation', 'journal_device', 'journal_inode'}
_IPC_FIELDS = {'ipc_device', 'ipc_inode'}
_MAX_LINE = 4096
_MAX_OUTPUT = 1024 * 1024


def _parse(line):
    if len(line) > _MAX_LINE or not line.startswith(_PREFIX):
        raise NativeSessionError('invalid session line')
    try:
        value = json.loads(line[len(_PREFIX):].decode('ascii'), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise NativeSessionError('invalid session JSON') from exc
    if (type(value) is not dict or type(value.get('version')) is not int
            or (value['version'] == 1 and set(value) != _FIELDS)
            or (value['version'] == 2 and set(value) != _FIELDS | _IPC_FIELDS)
            or value['version'] not in (1, 2)
            or type(value['pid']) is not int or value['pid'] <= 0
            or type(value['session_generation']) is not str
            or re.fullmatch('[0-9a-f]{32}', value['session_generation']) is None
            or any(type(value[k]) is not int or value[k] < 0 for k in ('journal_device', 'journal_inode'))
            or value['journal_inode'] == 0
            or (value['version'] == 2 and (any(type(value[k]) is not int or value[k] < 0 for k in _IPC_FIELDS)
                                           or value['ipc_inode'] == 0))):
        raise NativeSessionError('invalid session fields')
    return value


class NativeSessionReader:
    """Exclusive owner of unbuffered Popen stdout, including after acquisition."""
    def __init__(self, child, lease, executable, *, ipc_binding=None):
        if type(child) is not OwnedChild or type(lease) is not SessionLease:
            raise NativeSessionError('owned child and session lease required')
        if not isinstance(child.child.stdout, io.FileIO):
            raise NativeSessionError('unbuffered binary child stdout required')
        lease.check()
        if lease._reader_claimed or any(name.startswith(('session-', '.session-'))
                                        for name in os.listdir(lease.journal_fd)):
            raise NativeSessionError('session journal already claimed')
        lease._reader_claimed = True
        self.child, self.lease = child, lease
        self.ipc_binding = ipc_binding
        self.stdout = child.child.stdout
        self.executable_fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.buffer = bytearray()
        self.total = 0
        self.bound = False
        self._session_record = None
        try:
            executable_info = os.fstat(self.executable_fd)
            if not stat.S_ISREG(executable_info.st_mode):
                raise NativeSessionError('invalid native executable')
            self._verify()
        except BaseException:
            self.close()
            raise

    def _verify(self):
        self.lease.check()
        if self.executable_fd is None:
            raise NativeSessionError('session reader closed')
        self.child.verify_running()
        pid = self.child.identity.pid
        try:
            stdout = os.fstat(self.stdout.fileno())
            child_stdout = os.stat(f'/proc/{pid}/fd/1')
            exe, expected = os.stat(f'/proc/{pid}/exe'), os.fstat(self.executable_fd)
            child_ns, self_ns = os.stat(f'/proc/{pid}/ns/pid'), os.stat('/proc/self/ns/pid')
            if (not stat.S_ISFIFO(stdout.st_mode)
                    or (stdout.st_dev, stdout.st_ino) != (child_stdout.st_dev, child_stdout.st_ino)
                    or (exe.st_dev, exe.st_ino) != (expected.st_dev, expected.st_ino)
                    or (child_ns.st_dev, child_ns.st_ino) != (self_ns.st_dev, self_ns.st_ino)):
                raise NativeSessionError('child stdout, executable or pid namespace mismatch')
            current, parent = _identity(pid)
            if current != self.child.identity or parent != os.getpid():
                raise NativeSessionError('process identity changed')
            self.child.verify_running()
            return {'boot_id': current.boot_id, 'pid': current.pid,
                    'start_ticks': current.start_ticks,
                    'pid_namespace_device': child_ns.st_dev, 'pid_namespace_inode': child_ns.st_ino,
                    'exe_device': exe.st_dev, 'exe_inode': exe.st_ino}
        except (OSError, ProcessOwnershipError) as exc:
            raise NativeSessionError('child identity unavailable') from exc

    def read_and_persist(self, timeout=10):
        """Consume stdout until one strict startup line, then fsync its binding.

        This reader must be the *only* reader of the Popen pipe. Bytes after the
        startup line remain in ``buffer`` for the caller. Errors are fail-closed;
        the result never authorizes a tombstone on its own.
        """
        if self.bound or not 0 < timeout <= 60:
            raise NativeSessionError('reader already bound or invalid timeout')
        deadline = time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b'\n')
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if len(line) > _MAX_LINE:
                    raise NativeSessionError('native output line too long')
                if not line.startswith(_PREFIX):
                    continue
                record = _parse(line)
                break
            if len(self.buffer) > _MAX_LINE or self.total > _MAX_OUTPUT:
                raise NativeSessionError('native startup output limit exceeded')
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.stdout], [], [], remaining)[0]:
                raise NativeSessionError('native startup timeout')
            data = os.read(self.stdout.fileno(), 4096)
            if not data:
                raise NativeSessionError('native stdout closed')
            self.total += len(data)
            if self.total > _MAX_OUTPUT:
                raise NativeSessionError('native startup output limit exceeded')
            self.buffer.extend(data)
        return self._persist_record(record)

    def _persist_owned_line(self, line):
        """Bind a line delivered by the controller's exclusive child reader.

        The controller is responsible for authenticating the queue envelope;
        this private entry point must not be exposed as a caller assertion.
        """
        if self.bound:
            raise NativeSessionError('reader already bound')
        return self._persist_record(_parse(line))

    def _persist_record(self, record):
        native_identity = self._verify()
        journal = os.fstat(self.lease.journal_fd)
        if (record['pid'] != self.child.identity.pid
                or (record['journal_device'], record['journal_inode']) != (journal.st_dev, journal.st_ino)):
            raise NativeSessionError('native startup identity mismatch')
        if self.ipc_binding is not None:
            from .lifecycle import _check_directory
            ipc_fd, ipc_path = self.ipc_binding
            _check_directory(str(ipc_path), ipc_fd)
            ipc = os.fstat(ipc_fd)
            if record['version'] != 2 or (record['ipc_device'], record['ipc_inode']) != (ipc.st_dev, ipc.st_ino):
                raise NativeSessionError('native IPC identity mismatch')
        elif record['version'] == 2:
            raise NativeSessionError('native IPC descriptor required')
        executable = os.fstat(self.executable_fd)
        binding = {'version': 2, 'kind': 'native_session_binding', 'session': record,
                   'native': native_identity, 'process': asdict(self.child.identity),
                   'executable': {'device': executable.st_dev, 'inode': executable.st_ino}}
        payload = (json.dumps(binding, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
        name = 'session-' + record['session_generation'] + '.json'
        temporary = '.session-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                     0o600, dir_fd=self.lease.journal_fd)
        published = False
        try:
            os.fchmod(fd, 0o600)
            data = memoryview(payload)
            while data:
                count = os.write(fd, data)
                if count <= 0:
                    raise OSError('short binding write')
                data = data[count:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            if self.ipc_binding is not None:
                from .lifecycle import _check_directory
                _check_directory(str(self.ipc_binding[1]), self.ipc_binding[0])
            if self._verify() != native_identity:
                raise NativeSessionError('native identity changed before persistence')
            _rename_noreplace(self.lease.journal_fd, temporary, name)
            published = True
            os.fsync(self.lease.journal_fd)
            self.lease.check()
            self.bound = True
            self._session_record = dict(record)
            self.lease._native_reader = self
            self.lease._native_generation = record['session_generation']
            return binding
        finally:
            if fd >= 0:
                os.close(fd)
            if not published:
                try:
                    os.unlink(temporary, dir_fd=self.lease.journal_fd)
                except FileNotFoundError:
                    pass

    def close(self):
        if self.executable_fd is not None:
            os.close(self.executable_fd)
            self.executable_fd = None
