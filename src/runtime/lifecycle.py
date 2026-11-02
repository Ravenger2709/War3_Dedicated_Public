"""Opt-in reader for the native lifecycle journal v1.

A caller supplies an idempotent callback that *durably* stores the event and
its native identity before returning True: (session_generation, match_id) for
starts and (session_generation, host_counter) for closes. This module does
not connect the journal to the controller or to a map-cycle cursor. In
particular, native IDs are generated at start and are not CycleState.reserve's
pre-start UUID; correlation needs a separately agreed integration protocol.

This is an at-least-once interface: a crash after the callback commits but
before durable acknowledgement can call it again. Callbacks must reject the
same key with conflicting data and make all effects transactional/idempotent.
The journal directory and the callback are in one trusted ownership domain.
"""

from dataclasses import dataclass
import fcntl
import json
import os
import re
import stat
from typing import Callable


_MAX_EVENT_BYTES = 4096
_NAME = re.compile(r'start-([0-9a-f]{32})-([0-9a-f]{32})\.json\Z', re.ASCII)
_CLOSE_NAME = re.compile(r'close-([0-9a-f]{32})-([1-9][0-9]{0,8})\.json\Z', re.ASCII)
_FIELDS = frozenset(('version', 'event', 'session_generation', 'match_id', 'host_counter'))
_CLOSE_FIELDS = frozenset(('version', 'event', 'session_generation', 'host_counter'))
_CLOSE_V2_FIELDS = _CLOSE_FIELDS | {'request_id', 'manifest_sha256', 'catalog_index'}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class LifecycleJournalError(RuntimeError):
    """Journal validation or processing failed; pending records are retained."""


@dataclass(frozen=True)
class GameStarted:
    version: int
    event: str
    session_generation: str
    match_id: str
    host_counter: int

    @property
    def idempotency_key(self):
        return (self.session_generation, self.match_id)


@dataclass(frozen=True)
class LobbyClosed:
    version: int
    event: str
    session_generation: str
    host_counter: int

    @property
    def idempotency_key(self):
        return (self.session_generation, self.host_counter)


@dataclass(frozen=True)
class CatalogLobbyClosed:
    version: int
    event: str
    session_generation: str
    host_counter: int
    request_id: str
    manifest_sha256: str
    catalog_index: int

    @property
    def idempotency_key(self):
        return (self.session_generation, self.host_counter)


@dataclass(frozen=True)
class ScanResult:
    processed: int
    recovered_acks: int


def _open_directory(path):
    path = os.fspath(path)
    if not isinstance(path, str) or not path.startswith('/') or '\0' in path:
        raise LifecycleJournalError('journal path must be an absolute text path')
    parts = path.split('/')[1:]
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise LifecycleJournalError('invalid journal directory path')
    fd = os.open('/', _DIR_FLAGS)
    try:
        for part in parts:
            new_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LifecycleJournalError('journal directory must be owned by the effective UID and mode 0700')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_directory(path, held_fd):
    current_fd = _open_directory(path)
    try:
        held, current = os.fstat(held_fd), os.fstat(current_fd)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise LifecycleJournalError('journal directory was replaced')
    finally:
        os.close(current_fd)


def _read_file(directory_fd, name):
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink not in (1, 2)
                or not 0 < before.st_size <= _MAX_EVENT_BYTES):
            raise LifecycleJournalError('invalid journal file metadata: ' + name)
        chunks = []
        remaining = _MAX_EVENT_BYTES + 1
        while remaining:
            data = os.read(fd, remaining)
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)
        if len(raw) != before.st_size or identity(before) != identity(after):
            raise LifecycleJournalError('journal file changed during read: ' + name)
        return raw, after
    finally:
        os.close(fd)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _parse(name, raw):
    match = _NAME.fullmatch(name)
    close = _CLOSE_NAME.fullmatch(name)
    if match is None and close is None:
        raise LifecycleJournalError('invalid event filename: ' + name)
    try:
        obj = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid constant')))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise LifecycleJournalError('invalid event JSON: ' + name) from exc
    if close is not None:
        if (type(obj) is not dict or type(obj.get('version')) is not int or
                obj['version'] not in (1, 2) or
                obj.keys() != (_CLOSE_FIELDS if obj['version'] == 1 else _CLOSE_V2_FIELDS)
                or type(obj['event']) is not str or obj['event'] != 'lobby_closed'
                or type(obj['session_generation']) is not str or obj['session_generation'] != close[1]
                or type(obj['host_counter']) is not int or not 1 <= obj['host_counter'] < 2**28
                or str(obj['host_counter']) != close[2]):
            raise LifecycleJournalError('invalid close event fields: ' + name)
        if obj['version'] == 2:
            if (type(obj['request_id']) is not str or re.fullmatch(r'[0-9a-f]{32}',obj['request_id']) is None or
                    type(obj['manifest_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',obj['manifest_sha256']) is None or
                    type(obj['catalog_index']) is not int or not 0 <= obj['catalog_index'] < 32):
                raise LifecycleJournalError('invalid catalog close fields: ' + name)
            return CatalogLobbyClosed(**obj)
        return LobbyClosed(**obj)
    if (type(obj) is not dict or obj.keys() != _FIELDS
            or type(obj['version']) is not int or obj['version'] != 1
            or type(obj['event']) is not str or obj['event'] != 'game_started'
            or type(obj['session_generation']) is not str or obj['session_generation'] != match[1]
            or type(obj['match_id']) is not str or obj['match_id'] != match[2]
            or type(obj['host_counter']) is not int or not 0 <= obj['host_counter'] <= 0x0fffffff):
        raise LifecycleJournalError('invalid event fields: ' + name)
    return GameStarted(**obj)


def _same_file(a, b):
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _unchanged(directory_fd, name, original, identity):
    raw, info = _read_file(directory_fd, name)
    if raw != original or not _same_file(info, identity):
        raise LifecycleJournalError('event changed while processing: ' + name)


def _ack_matches(directory_fd, ack_name, expected):
    try:
        raw, _ = _read_file(directory_fd, ack_name)
    except FileNotFoundError:
        return False
    if raw != expected:
        raise LifecycleJournalError('conflicting acknowledgement: ' + ack_name)
    return True


def scan(directory, persist: Callable[[GameStarted | LobbyClosed], bool], *, lease=None) -> ScanResult:
    """Scan once and acknowledge v1 events after ``persist(event) is True``.

    ``persist`` MUST durably and idempotently commit the event/key before it
    returns True. False, None, exceptions, and journal/OS errors stop the scan;
    no ACK is created for that callback failure. A matching durable ACK is
    recovered without another callback. Results count only this invocation;
    callers must periodically scan again, including after process restarts.

    Cooperating readers use a nonblocking exclusive flock on the directory.
    Ordering is unspecified and cannot be used as a cursor. The caller must
    provide a private local directory matching the native v1 contract.
    """
    from .session_lease import require_journal
    require_journal(directory, lease)
    directory = os.fspath(directory)
    dir_fd = _open_directory(directory)
    processed = recovered = 0
    try:
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LifecycleJournalError('journal consumer already active') from exc
        names = sorted(name for name in os.listdir(dir_fd)
                       if name.startswith(('start-', 'close-')) and name.endswith('.json'))
        # A producer's rename may be visible before its own directory fsync.
        os.fsync(dir_fd)
        _check_directory(directory, dir_fd)
        for name in names:
            require_journal(directory, lease)
            raw, identity = _read_file(dir_fd, name)
            event = _parse(name, raw)
            ack_name = name[:-5] + '.ack'
            acknowledged = _ack_matches(dir_fd, ack_name, raw)
            if not acknowledged:
                if persist(event) is not True:
                    raise LifecycleJournalError('callback did not confirm durable persistence: ' + name)
                require_journal(directory, lease)
                _check_directory(directory, dir_fd)
                _unchanged(dir_fd, name, raw, identity)
                try:
                    os.link(name, ack_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                            follow_symlinks=False)
                except FileExistsError:
                    if not _ack_matches(dir_fd, ack_name, raw):
                        raise LifecycleJournalError('acknowledgement disappeared: ' + ack_name)
                # Verify the link and its content even after a racing replacement.
                if not _ack_matches(dir_fd, ack_name, raw):
                    raise LifecycleJournalError('acknowledgement disappeared: ' + ack_name)
                processed += 1
            else:
                recovered += 1
            # Make ACK durable before removing the pending name, including on
            # recovery when a previous consumer died before its directory fsync.
            os.fsync(dir_fd)
            require_journal(directory, lease)
            _check_directory(directory, dir_fd)
            _unchanged(dir_fd, name, raw, identity)
            os.unlink(name, dir_fd=dir_fd)
            os.fsync(dir_fd)
        return ScanResult(processed, recovered)
    except OSError as exc:
        raise LifecycleJournalError('journal filesystem operation failed') from exc
    finally:
        os.close(dir_fd)
