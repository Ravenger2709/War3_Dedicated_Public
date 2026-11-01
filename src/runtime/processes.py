"""Private runtime files and prefix-scoped cleanup; never inspect process secrets."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

_LIBC = ctypes.CDLL(None, use_errno=True)


def guard_parent(parent_pid):
    """Linux child setup: close the spawn/ownership-record SIGKILL race."""
    if _LIBC.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(70)


class PrivateLogBudget:
    """Copy/truncate task-owned logs without emitting or parsing their contents.

    Prefix symlinks (Wine's dosdevices, for example) are never followed. Dump
    contents are never read. Limits are constants, not secret-bearing config.
    """
    def __init__(self, roots, file_limit=10 * 1024**2, archives=3,
                 directory_budget=64 * 1024**2):
        self.roots = tuple(private_directory(root) for root in roots)
        self.file_limit = file_limit
        self.archives = archives
        self.directory_budget = directory_budget

    def _open(self, path):
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ProcessError('LOG_FILE_UNSAFE')
            os.fchmod(fd, 0o600)
            return fd, info
        except BaseException:
            os.close(fd)
            raise

    def _archive_path(self, path, index):
        return path.with_name(path.name + '.' + str(index))

    def _remove(self, path):
        # Truncate before unlink so an already-open writer retains no old data.
        try:
            fd, _ = self._open(path)
        except FileNotFoundError:
            return
        try:
            os.ftruncate(fd, 0)
            path.unlink()
        finally:
            os.close(fd)

    def _rotate(self, path):
        source_fd, info = self._open(path)
        try:
            if info.st_size < self.file_limit:
                return
            self._remove(self._archive_path(path, self.archives))
            for index in range(self.archives - 1, 0, -1):
                previous = self._archive_path(path, index)
                if previous.exists() or previous.is_symlink():
                    fd, _ = self._open(previous)
                    os.close(fd)
                    os.replace(previous, self._archive_path(path, index + 1))
            archive = self._archive_path(path, 1)
            dest_fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                # Keep only a bounded tail. These bytes stay private, never decoded.
                offset = max(0, info.st_size - self.file_limit)
                remaining = min(info.st_size, self.file_limit)
                while remaining:
                    copied = os.sendfile(dest_fd, source_fd, offset, remaining)
                    if not copied:
                        break
                    offset += copied
                    remaining -= copied
                os.ftruncate(source_fd, 0)
            finally:
                os.close(dest_fd)
        finally:
            os.close(source_fd)

    @staticmethod
    def _kind(path):
        name = path.name.lower()
        if name.endswith(('.dmp', '.mdmp', '.core')) or name == 'core' or name.startswith('core.'):
            return 'dump'
        base = name.rsplit('.', 1)[0] if name.rsplit('.', 1)[-1].isdigit() else name
        if base.endswith('.log') or base in ('__log.txt', 'backtrace.txt'):
            return 'archive' if base != name else 'log'
        return None

    def enforce(self):
        for base in self.roots:
            for root, directories, files in os.walk(base, followlinks=False):
                directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
                for name in files:
                    path = Path(root) / name
                    kind = self._kind(path)
                    if kind is None:
                        continue
                    try:
                        if kind == 'dump':
                            self._remove(path)
                            continue
                        if kind == 'archive' and path.suffix[1:] not in {str(i) for i in range(1, self.archives + 1)}:
                            self._remove(path)
                            continue
                        if kind == 'log':
                            self._rotate(path)
                        fd, info = self._open(path)
                        os.close(fd)
                        if kind == 'archive' and info.st_size > self.file_limit:
                            # Oversized leftovers are not copied or inspected.
                            fd, _ = self._open(path)
                            try:
                                os.ftruncate(fd, self.file_limit)
                            finally:
                                os.close(fd)
                            info = path.stat()
                    except FileNotFoundError:
                        continue
        # Recount after all renames: old directory entries can now refer to
        # different-sized archives. Never budget using stale rotation metadata.
        records = []
        for base in self.roots:
            for root, directories, files in os.walk(base, followlinks=False):
                directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
                for name in files:
                    path = Path(root) / name
                    kind = self._kind(path)
                    if kind not in ('log', 'archive'):
                        continue
                    try:
                        fd, info = self._open(path)
                        os.close(fd)
                        records.append((path, kind, info.st_size, info.st_mtime_ns))
                    except FileNotFoundError:
                        continue
        total = sum(size for _, _, size, _ in records)
        for path, kind, size, _ in sorted(records, key=lambda row: (row[1] != 'archive', row[3])):
            if total <= self.directory_budget:
                break
            if kind == 'archive':
                self._remove(path)
            else:
                fd, _ = self._open(path)
                try:
                    os.ftruncate(fd, 0)
                finally:
                    os.close(fd)
            total -= size


class ProcessError(ValueError):
    """Arguments are fixed codes, safe for private status documents."""


def checked_path(value):
    path = Path(value)
    if not path.is_absolute() or path == Path('/') or '..' in path.parts:
        raise ProcessError('PATH_UNSAFE')
    for component in reversed((path, *path.parents)):
        try:
            if component.is_symlink():
                raise ProcessError('PATH_UNSAFE')
        except OSError:
            raise ProcessError('PATH_UNSAFE') from None
    return path


def private_directory(path, create=False):
    path = checked_path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProcessError('DIRECTORY_UNSAFE')
    return path


def read_file(path, limit, private=False):
    path = checked_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or (private and (info.st_uid != os.geteuid()
                                 or stat.S_IMODE(info.st_mode) != 0o600))):
            raise ProcessError('FILE_UNSAFE')
        with os.fdopen(fd, 'rb', closefd=False) as source:
            data = source.read(limit + 1)
        after = os.fstat(fd)
        if len(data) != info.st_size or len(data) > limit or (
                after.st_mtime_ns, after.st_size) != (info.st_mtime_ns, info.st_size):
            raise ProcessError('FILE_CHANGED')
        return data
    finally:
        os.close(fd)


def atomic_json(path, document):
    path = checked_path(path)
    private_directory(path.parent)
    if path.exists():
        read_file(path, 65536, private=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(document, output, separators=(',', ':'), allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OwnedLock:
    def __init__(self, directory, name='.star-worker.lock'):
        self.directory = private_directory(directory)
        path = checked_path(self.directory / name)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ProcessError('LOCK_UNSAFE')
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProcessError('WORKER_ALREADY_RUNNING') from None
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def process_stat(pid, proc=Path('/proc')):
    text = (proc / str(pid) / 'stat').read_text(errors='replace')
    end = text.rindex(')')
    fields = text[end + 2:].split()
    return {'pid': int(pid), 'name': text[text.index('(') + 1:end],
            'state': fields[0], 'ppid': int(fields[1]), 'pgid': int(fields[2]),
            'start_ticks': int(fields[19])}


def process_identity(pid):
    try:
        row = process_stat(pid)
        return {key: row[key] for key in ('pid', 'pgid', 'start_ticks')}
    except (OSError, ValueError, IndexError):
        return None


def snapshot(proc=Path('/proc')):
    """Only same-UID stat/maps metadata. No cmdline, environ, mem, logs or sockets."""
    engine = False
    errors = set()
    rows = []
    renderers = 0
    container_initializations = 0
    for entry in proc.glob('[0-9]*'):
        for attempt in range(2):
            row = None
            try:
                if entry.stat().st_uid != os.geteuid():
                    break
                row = process_stat(int(entry.name), proc)
                name = row['name'].lower().replace(' ', '')
                # Linux comm can truncate "Warcraft III.exe" to 15 bytes.
                engine |= name in ('war3.exe', 'warcraftiii.exe', 'warcraftiii.ex', 'warcraftiiiexe')
                live = row['state'] not in ('Z', 'X')
                if live:
                    # Linux path names are bytes, not necessarily UTF-8. Only
                    # compare the module basename; never emit mapping paths.
                    with (entry / 'maps').open('rb') as mappings:
                        for mapping in mappings:
                            parts = mapping.rstrip().split(None, 5)
                            if len(parts) == 6:
                                module = parts[5].removesuffix(b' (deleted)').replace(b'\\', b'/').rsplit(b'/', 1)[-1].lower()
                                engine |= module in (b'war3.exe', b'game.dll')
                rows.append(row)
                renderers += int(live and name.startswith('qtwebengine'))
                break
            except (FileNotFoundError, ProcessLookupError):
                break
            except (OSError, ValueError, IndexError) as error:
                # Docker exec/HEALTHCHECK briefly exposes runc's protected
                # pre-exec init in this PID namespace. Its parent is outside
                # the namespace (kernel PPID=0), unlike application children.
                # Do not treat that runtime bootstrap as an unreadable app.
                # A process with the same name and an in-container parent is
                # still subject to the ordinary fail-closed mapping check.
                if (isinstance(error,PermissionError) and row is not None
                        and row['pid']==int(entry.name) and row['ppid']==0
                        and row['pgid']==row['pid'] and row['name']=='runc:[2:INIT]'):
                    rows.append(row);container_initializations+=1
                    break
                # A process can exit or exec between stat and maps. Retry
                # its complete metadata once; a still-unreadable live PID
                # remains a hard monitoring error, never an implicit pass.
                if attempt:
                    errors.add('PROCESS_METADATA_PERMISSION' if isinstance(error,PermissionError)
                               else 'PROCESS_METADATA_OS_ERROR' if isinstance(error,OSError)
                               else 'PROCESS_METADATA_PARSE_ERROR')
    return {'processes': rows, 'game_engine_observed': bool(engine),
            'renderer_count': renderers, 'monitor_error': bool(errors),
            'monitor_error_codes': sorted(errors),
            'container_initializations': container_initializations}


def stop_group(identity, timeout=2):
    """Stop only a recorded session leader, checking Linux PID reuse first."""
    if not identity:
        return True
    if (not isinstance(identity, dict) or set(identity) != {'pid', 'pgid', 'start_ticks'}
            or any(type(n) is not int or n <= 0 for n in identity.values())
            or identity['pid'] != identity['pgid'] or identity['pgid'] == os.getpgrp()):
        return False
    current = process_identity(identity['pid'])
    if current is None:
        # A missing group leader cannot safely authenticate a reused PGID.
        return True
    if current != identity:
        return False
    try:
        os.killpg(identity['pgid'], signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while process_identity(identity['pid']) == identity and time.monotonic() < deadline:
            if process_stat(identity['pid'])['state'] == 'Z':
                return True
            time.sleep(0.05)
        if process_identity(identity['pid']) == identity:
            os.killpg(identity['pgid'], signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except (OSError, ValueError, IndexError):
        return False


def prefix_record(prefix):
    prefix = private_directory(prefix)
    record = json.loads(read_file(prefix / '.star-owner.json', 4096, private=True))
    info = prefix.stat()
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('uid') != os.geteuid() or record.get('device') != info.st_dev
            or record.get('inode') != info.st_ino):
        raise ProcessError('PREFIX_NOT_OWNED')
    return record


def _stop_locked_prefix(prefix, timeout=12, run=None):
    """Caller must hold OwnedLock(prefix); used by the worker during shutdown."""
    result = {'version': 1, 'cleaned': False, 'reason': 'PREFIX_CLEANUP_FAILED',
              'kill_exit': None, 'wait_exit': None, 'xvfb_stopped': False}
    try:
        run = run or subprocess.run
        record = prefix_record(prefix)
        deadline = time.monotonic() + max(0.1, min(float(timeout), 30))
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'WINEPREFIX': str(prefix),
               'WINEARCH': 'win32', 'WINEDEBUG': '-all', 'HOME': str(prefix)}
        for flag, key in (('-k', 'kill_exit'), ('-w', 'wait_exit')):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                done = run(['wineserver', flag], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=min(2, remaining) if flag == '-k' else max(0.1, remaining - 2),
                           check=False)
                result[key] = done.returncode
            except subprocess.TimeoutExpired:
                result['reason'] = 'PREFIX_CLEANUP_TIMEOUT'
                break
        result['xvfb_stopped'] = stop_group(record.get('xvfb'), max(0, min(1, deadline - time.monotonic())))
        # Wine 11 main.c exits !kill_lock_owner(): -k returns 1 when
        # the server is already absent. A successful, bounded -w still
        # has to confirm the same prefix lock is no longer held.
        result['cleaned'] = result['kill_exit'] in (0, 1) and result['wait_exit'] == 0 and result['xvfb_stopped']
        if result['cleaned']:
            result['reason'] = 'PREFIX_CLEANED'
    except (OSError, ValueError, TypeError):
        pass
    return result


def stop_owned_prefix(prefix, timeout=12):
    """Recover after worker SIGKILL. Refuse live workers and unmarked prefixes.

    Returns fixed-code metadata only; never kills a global Wine server. A false
    ``cleaned`` result must prevent the parent's next account cohort from starting.
    """
    try:
        prefix = private_directory(prefix)
        with OwnedLock(prefix):
            return _stop_locked_prefix(prefix, timeout)
    except (OSError, ValueError, TypeError):
        return {'version': 1, 'cleaned': False, 'reason': 'PREFIX_UNSAFE_OR_BUSY',
                'kill_exit': None, 'wait_exit': None, 'xvfb_stopped': False}
