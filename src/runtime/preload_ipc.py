"""Offline building blocks for a future native catalog request/ACK channel.

The caller must own the native session and coordinate its durable request state.
This module does not authorize, deliver, or execute a request, and no native
consumer or crash recovery protocol is wired to it.
"""
from dataclasses import dataclass
import errno
import os
import re
import secrets
import stat

from .lifecycle import _check_directory, _open_directory
from .reservation_marker import _rename_noreplace, _verify_marker

_HEX32 = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_HEX64 = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_LIMIT = 512


class PreloadIPCError(ValueError):
    """The request, acknowledgement, or protected directory is invalid."""


@dataclass(frozen=True)
class Request:
    session_generation: str
    manifest_sha256: str
    request_id: str
    index: int

    def __post_init__(self):
        if (not isinstance(self.session_generation, str) or not _HEX32.fullmatch(self.session_generation)
                or not isinstance(self.manifest_sha256, str) or not _HEX64.fullmatch(self.manifest_sha256)
                or not isinstance(self.request_id, str) or not _HEX32.fullmatch(self.request_id)
                or type(self.index) is not int or not 0 <= self.index < 32):
            raise PreloadIPCError('invalid preload request')

    def payload(self):
        return (f'version=1\nsession_generation={self.session_generation}\n'
                f'manifest_sha256={self.manifest_sha256}\nrequest_id={self.request_id}\n'
                f'index={self.index}\n').encode('ascii')


def _publish_exact(directory, name, payload, request=None):
    directory = os.fspath(directory)
    dir_fd = _open_directory(directory)
    temp = None
    try:
        _check_directory(directory, dir_fd)
        if request is not None:
            _verify_marker(dir_fd, 'request-' + request.request_id + '.ready', request.payload())
        try:
            _verify_marker(dir_fd, name, payload)
        except FileNotFoundError:
            temp = '.request-' + secrets.token_hex(16) + '.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=dir_fd)
            try:
                os.fchmod(fd, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError(errno.EIO, 'short request write')
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            _check_directory(directory, dir_fd)
            if request is not None:
                _verify_marker(dir_fd, 'request-' + request.request_id + '.ready', request.payload())
            try:
                _rename_noreplace(dir_fd, temp, name)
                temp = None
            except FileExistsError:
                pass
            _verify_marker(dir_fd, name, payload)
        _check_directory(directory, dir_fd)
        if request is not None:
            _verify_marker(dir_fd, 'request-' + request.request_id + '.ready', request.payload())
        os.fsync(dir_fd)
        _check_directory(directory, dir_fd)
        _verify_marker(dir_fd, name, payload)
        return name
    finally:
        if temp is not None:
            try: os.unlink(temp, dir_fd=dir_fd)
            except FileNotFoundError: pass
        os.close(dir_fd)


def publish_request(directory, request):
    """Publish one immutable request name; exact retries are safe.

    Persisting intent and session binding is the caller's responsibility.
    A failed fsync can leave an uncertain on-disk request.
    """
    if type(request) is not Request:
        raise PreloadIPCError('invalid preload request')
    request.__post_init__()
    return _publish_exact(directory, 'request-' + request.request_id + '.ready', request.payload())


def notify_request(directory, request):
    """Publish a single-slot notification after the durable request exists.

    Another outstanding ID fails closed; the caller retries after it is
    consumed. Publishing a notification does not prove staging or delivery.
    """
    publish_request(directory, request)
    return _publish_exact(directory, 'next.ready', request.payload(), request=request)


def read_ack(directory, request):
    """Return a matching ACK status or None; malformed/conflicting data fails.

    A staged ACK only means the native consumer reports staging; it must not
    be interpreted as proof that a lobby was published or a match started.
    Native ACK production and recovery have not yet been implemented.
    """
    if type(request) is not Request:
        raise PreloadIPCError('invalid preload request')
    request.__post_init__()
    directory = os.fspath(directory)
    dir_fd = _open_directory(directory)
    name = 'ack-' + request.request_id + '.ready'
    try:
        _check_directory(directory, dir_fd)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            _check_directory(directory, dir_fd)
            return None
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or not 1 <= before.st_size <= _LIMIT):
                raise PreloadIPCError('invalid preload ACK metadata')
            data = b''
            while len(data) < before.st_size:
                part = os.read(fd, before.st_size - len(data))
                if not part: break
                data += part
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                                  s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise PreloadIPCError('preload ACK changed')
            statuses = ('staged', 'rejected', 'uncertain')
            expected = {request.payload() + f'status={status}\n'.encode('ascii'): status
                        for status in statuses}
            if data not in expected:
                raise PreloadIPCError('invalid or conflicting preload ACK')
            _check_directory(directory, dir_fd)
            _verify_marker(dir_fd, 'request-' + request.request_id + '.ready', request.payload())
            _verify_marker(dir_fd, name, data)
            os.fsync(dir_fd)
            _check_directory(directory, dir_fd)
            _verify_marker(dir_fd, 'request-' + request.request_id + '.ready', request.payload())
            _verify_marker(dir_fd, name, data)
            return expected[data]
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
