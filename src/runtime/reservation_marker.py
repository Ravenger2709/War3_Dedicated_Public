"""Publish the optional native start-reservation gate v1 marker.

The caller owns CycleState exclusively and serializes its transitions with this
function. The native process and this publisher must share one trusted UID and
private local journal directory, which must not be replaced while in use.
An exception after publication can leave a marker: retry with the same persisted
reservation. Never remove a marker while that native process may still start.
"""
import ctypes
import errno
import os
import re
import secrets
import stat

from .cycle_state import CycleState
from .lifecycle import _check_directory, _open_directory


class ReservationMarkerError(ValueError):
    """The reservation or journal does not satisfy the gate contract."""


def _stage(name):
    """Crash-test seam."""


def _rename_noreplace(dir_fd, source, target):
    rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'renameat2 unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(dir_fd, os.fsencode(source), dir_fd, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def _verify_marker(dir_fd, name, payload):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size != len(payload)):
            raise ReservationMarkerError('invalid marker metadata')
        data = b''
        while len(data) < len(payload):
            part = os.read(fd, len(payload) - len(data))
            if not part:
                break
            data += part
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                              s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if data != payload or identity(before) != identity(after) or identity(after) != identity(current):
            raise ReservationMarkerError('marker changed or has invalid content')
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(directory, state, session_generation, host_counter):
    """Publish or recover a marker for an already committed external reservation.

    ``state`` must be the exclusive, healthy CycleState owner. Its in-memory
    state and freshly read SQLite state must match, including the preset. A
    matching existing marker is verified and synced, never rewritten. Errors
    are propagated; callers must retry after resolving uncertain I/O. Returns
    the marker filename on success. The caller must ensure the supplied pair
    still identifies the current native lobby and serialize all state changes.
    """
    from .session_lease import require_journal
    if isinstance(state, CycleState):
        state._require_lease()
        require_journal(directory, state.lease)
        if state.lease is not None:
            state.lease.require_native(session_generation)
    if (not isinstance(state, CycleState) or state._write_failed
            or not isinstance(session_generation, str)
            or re.fullmatch(r'[0-9a-f]{32}', session_generation) is None
            or type(host_counter) is not int or not 1 <= host_counter <= 0x0fffffff):
        raise ReservationMarkerError('invalid or uncertain reservation')
    if state.state.get('version') != 3:
        raise ReservationMarkerError('reservation is not persisted in SQLite')
    persisted = state._load_db()
    state._validate(persisted)
    pending = persisted['pending']
    if (persisted != state.state or pending is None
            or set(pending) != {'session_generation', 'host_counter', 'preset_id'}
            or pending['session_generation'] != session_generation
            or pending['host_counter'] != host_counter):
        raise ReservationMarkerError('persisted reservation mismatch')

    name = f'reservation-{session_generation}-{host_counter}.ready'
    payload = (f'version=1\nsession_generation={session_generation}\n'
               f'host_counter={host_counter}\n').encode('ascii')
    directory = os.fspath(directory)
    dir_fd = _open_directory(directory)
    temp = None
    try:
        _check_directory(directory, dir_fd)
        try:
            _verify_marker(dir_fd, name, payload)
        except FileNotFoundError:
            temp = '.reservation-' + secrets.token_hex(16) + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=dir_fd)
            try:
                os.fchmod(fd, 0o600)
                _stage('created')
                remaining = memoryview(payload)
                while remaining:
                    count = os.write(fd, remaining)
                    if count <= 0:
                        raise OSError(errno.EIO, 'short marker write')
                    remaining = remaining[count:]
                _stage('written')
                os.fsync(fd)
                _stage('file_synced')
            finally:
                os.close(fd)
            state._require_lease()
            require_journal(directory, state.lease)
            if state.lease is not None:
                state.lease.require_native(session_generation)
            _check_directory(directory, dir_fd)
            try:
                _rename_noreplace(dir_fd, temp, name)
                temp = None
                _stage('published')
            except FileExistsError:
                # The existing name is authoritative only after full validation.
                pass
            _verify_marker(dir_fd, name, payload)
        _check_directory(directory, dir_fd)
        os.fsync(dir_fd)
        _stage('directory_synced')
        _check_directory(directory, dir_fd)
        return name
    finally:
        try:
            if temp is not None:
                try:
                    os.unlink(temp, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(dir_fd)
