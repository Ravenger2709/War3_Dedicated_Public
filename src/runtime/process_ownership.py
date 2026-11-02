"""Linux child identity and explicitly *unverified* journal observations.

These primitives do not prove which GHost generation wrote which journal, do
not provide a cursor/marker/scan lease, and MUST NOT authorize reconciliation.
The existing fail-closed reconciliation entry point remains unchanged.
"""
from dataclasses import asdict, dataclass
import fcntl
import json
import os
import re
import secrets
import select
import subprocess
import threading

from .lifecycle import _check_directory, _open_directory
from .reservation_marker import _rename_noreplace


class ProcessOwnershipError(RuntimeError):
    """The exact owned-child or observation proof could not be established."""


@dataclass(frozen=True)
class ProcessIdentity:
    boot_id: str
    observer_pid_namespace_device: int
    observer_pid_namespace_inode: int
    pid: int
    start_ticks: int
    uid: int


def _valid_identity(identity):
    return (type(identity) is ProcessIdentity
            and isinstance(identity.boot_id, str)
            and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', identity.boot_id) is not None
            and all(type(value) is int and value >= 0 for value in
                    (identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode, identity.uid))
            and identity.observer_pid_namespace_inode > 0 and type(identity.pid) is int and identity.pid > 0
            and type(identity.start_ticks) is int and identity.start_ticks > 0)


def _boot_id():
    with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as source:
        value = source.read(64).strip()
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value) is None:
        raise ProcessOwnershipError('BOOT_ID_UNAVAILABLE')
    return value


def _namespace():
    info = os.stat('/proc/self/ns/pid')
    return info.st_dev, info.st_ino


def _process(pid):
    # The kernel stat format encloses comm in parentheses; it may contain ')'.
    # This is metadata only, not cmdline, environment, process memory or logs.
    with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as source:
        text = source.read(65537)
    if len(text) > 65536:
        raise ProcessOwnershipError('PROCESS_STAT_INVALID')
    try:
        end = text.rindex(')')
        fields = text[end + 2:].split()
        if int(text[:text.index(' ')]) != pid or len(fields) <= 19:
            raise ValueError('invalid stat')
        ppid, start = int(fields[1]), int(fields[19])
        if start <= 0:
            raise ValueError('invalid starttime')
    except (ValueError, IndexError) as exc:
        raise ProcessOwnershipError('PROCESS_STAT_INVALID') from exc
    return ppid, start, os.stat(f'/proc/{pid}').st_uid


def _identity(pid):
    boot = _boot_id()
    ns_dev, ns_ino = _namespace()
    ppid, start, uid = _process(pid)
    return ProcessIdentity(boot, ns_dev, ns_ino, pid, start, uid), ppid


class OwnedChild:
    """A retained pidfd for a still-running child of this Python process.

    Capture and wait rely on kernel parent/child checks through waitid(P_PIDFD),
    never a PID alone. Internal observations and verified waits are serialized;
    callers must not independently poll/wait on Popen because a competing
    reaper makes proof unavailable. This object proves only the child lifetime;
    it does not authenticate native stdout or a journal.
    """
    def __init__(self, child):
        if type(child) is not subprocess.Popen or not hasattr(os, 'pidfd_open') or not hasattr(os, 'P_PIDFD'):
            raise ProcessOwnershipError('OWNED_CHILD_UNAVAILABLE')
        if type(child.pid) is not int or child.pid <= 0 or child.returncode is not None:
            raise ProcessOwnershipError('OWNED_CHILD_UNAVAILABLE')
        self.child = child
        self.fd = None
        self._verified_exit = None
        self._wait_lock = threading.RLock()
        try:
            self.fd = os.pidfd_open(child.pid, 0)
            first, parent = _identity(child.pid)
            if parent != os.getpid() or first.uid != os.geteuid():
                raise ProcessOwnershipError('NOT_OWNED_CHILD')
            self._assert_unreaped_running()
            second, parent2 = _identity(child.pid)
            if first != second or parent2 != parent:
                raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
            self.identity = first
        except (OSError, ChildProcessError, ProcessOwnershipError) as exc:
            self.close()
            if isinstance(exc, ProcessOwnershipError):
                raise
            raise ProcessOwnershipError('OWNED_CHILD_UNAVAILABLE') from exc

    def _assert_unreaped_running(self):
        try:
            info = os.waitid(os.P_PIDFD, self.fd, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except (OSError, ChildProcessError) as exc:
            raise ProcessOwnershipError('CHILD_WAIT_UNAVAILABLE') from exc
        if info is not None:
            raise ProcessOwnershipError('CHILD_ALREADY_EXITED')

    def is_running(self):
        """Observe this child without reaping or asserting a verified exit.

        Popen.returncode may have been set by another reaper. It is useful to
        report liveness, but only wait() can establish the handoff capability.
        """
        with self._wait_lock:
            if self._verified_exit is not None or self.child.returncode is not None:
                return False
            if self.fd is None or self.child.pid != self.identity.pid:
                raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
            try:
                result = os.waitid(os.P_PIDFD, self.fd, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except (OSError, ChildProcessError) as exc:
                raise ProcessOwnershipError('CHILD_WAIT_UNAVAILABLE') from exc
            if result is not None and result.si_pid != self.identity.pid:
                raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
            return result is None

    def verify_running(self):
        with self._wait_lock:
            if self.fd is None or self.child.pid != self.identity.pid:
                raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
            self._assert_unreaped_running()
            try:
                current, parent = _identity(self.identity.pid)
            except (OSError, ProcessOwnershipError) as exc:
                raise ProcessOwnershipError('CHILD_IDENTITY_UNAVAILABLE') from exc
            if current != self.identity or parent != os.getpid():
                raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
            self._assert_unreaped_running()

    def wait(self, timeout=None):
        """Wait and verify this unreaped pidfd child; no process is signalled."""
        if self.fd is None or self.child.pid != self.identity.pid:
            raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
        poller = select.poll()
        poller.register(self.fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        if not poller.poll(None if timeout is None else max(0, int(timeout * 1000))):
            raise TimeoutError('CHILD_STILL_RUNNING')
        try:
            with self._wait_lock:
                if self.fd is None or self.child.pid != self.identity.pid:
                    raise ProcessOwnershipError('CHILD_IDENTITY_CHANGED')
                info = os.waitid(os.P_PIDFD, self.fd, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                if info is None or info.si_pid != self.identity.pid:
                    raise ProcessOwnershipError('CHILD_WAIT_UNAVAILABLE')
                if info.si_code == os.CLD_EXITED:
                    expected = info.si_status
                elif info.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                    expected = -info.si_status
                else:
                    raise ProcessOwnershipError('CHILD_WAIT_UNAVAILABLE')
                actual = self.child.wait(timeout=0)
                if actual != expected:
                    raise ProcessOwnershipError('CHILD_EXIT_STATUS_MISMATCH')
                self._verified_exit = (self.identity, actual)
                return actual
        except (OSError, ChildProcessError, subprocess.TimeoutExpired) as exc:
            raise ProcessOwnershipError('CHILD_WAIT_UNAVAILABLE') from exc

    def close(self):
        with self._wait_lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def inspect_saved_identity(identity):
    """Return diagnostic visibility only, never authorization to reconcile.

    An absent PID can also mean visibility/namespace/permission ambiguity.
    A reused PID identifies a different process, not the writer of a generation.
    """
    if not _valid_identity(identity):
        raise ProcessOwnershipError('IDENTITY_INVALID')
    try:
        boot, ns = _boot_id(), _namespace()
        if boot != identity.boot_id:
            return 'different_boot_unverified'
        if ns != (identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode):
            return 'different_namespace_unverified'
        try:
            current, _ = _identity(identity.pid)
        except FileNotFoundError:
            return 'absent_unverified'
        if current == identity:
            return 'same_identity_visible'
        return 'pid_reused_or_identity_changed'
    except (OSError, ProcessOwnershipError):
        return 'visibility_ambiguous'


def write_unverified_observation(child, journal, reported_generation):
    """Durably write diagnostic metadata; this is explicitly *not* a binding.

    A caller-reported generation is not authenticated by Popen, pidfd or a
    directory inode. The record cannot be used to clear a reservation. Locking
    the journal during this write does not fence cursor or marker writers.
    """
    if not isinstance(child, OwnedChild) or not isinstance(reported_generation, str) or re.fullmatch(r'[0-9a-f]{32}', reported_generation) is None:
        raise ProcessOwnershipError('OBSERVATION_INVALID')
    child.verify_running()
    directory = os.fspath(journal)
    fd = _open_directory(directory)
    temporary = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProcessOwnershipError('JOURNAL_BUSY') from exc
        _check_directory(directory, fd)
        info = os.fstat(fd)
        record = {'version': 1, 'kind': 'unverified_process_observation',
                  'verified_binding': False, 'reported_session_generation': reported_generation,
                  'process': asdict(child.identity),
                  'journal': {'device': info.st_dev, 'inode': info.st_ino}}
        data = (json.dumps(record, separators=(',', ':'), sort_keys=True) + '\n').encode('ascii')
        token = secrets.token_hex(16)
        temporary = '.observation-' + token + '.tmp'
        name = 'unverified-observation-' + token + '.json'
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            os.fchmod(out, 0o600)
            remaining = memoryview(data)
            while remaining:
                count = os.write(out, remaining)
                if count <= 0:
                    raise OSError('short observation write')
                remaining = remaining[count:]
            os.fsync(out)
        finally:
            os.close(out)
        child.verify_running()
        _check_directory(directory, fd)
        _rename_noreplace(fd, temporary, name)
        temporary = None
        os.fsync(fd)
        _check_directory(directory, fd)
        return name
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.close(fd)
