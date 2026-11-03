"""Strict saved-writer visibility, not authorization to remove reservations.

A pidfd/proc observation does not prove that the native executable did not
fork or delegate a journal writer. Callers MUST NOT treat this result as an
absence tombstone capability. No live-controller integration is performed.
"""
from dataclasses import fields
import json
import os
import re
import select

from .lifecycle import _read_file, _unique_pairs
from .native_session import _parse
from .process_ownership import ProcessIdentity, _boot_id, _identity, _namespace, _valid_identity
from .session_lease import SessionLease


class WriterObservationError(RuntimeError):
    pass


def _pidfd_exited(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(0)
    if not events:
        return False
    if len(events) != 1 or events[0][0] != fd or events[0][1] & (select.POLLERR | select.POLLNVAL):
        raise WriterObservationError('pidfd poll failed')
    if not events[0][1] & (select.POLLIN | select.POLLHUP):
        raise WriterObservationError('pidfd poll ambiguous')
    return True


def _load_binding(lease, generation):
    if (type(lease) is not SessionLease or type(generation) is not str
            or re.fullmatch('[0-9a-f]{32}', generation) is None):
        raise WriterObservationError('invalid binding request')
    lease.check()
    os.fsync(lease.journal_fd)
    name = f'session-{generation}.json'
    try:
        raw, st = _read_file(lease.journal_fd, name)
        if st.st_nlink != 1:
            raise WriterObservationError('invalid binding links')
        named = os.stat(name, dir_fd=lease.journal_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns) != (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise WriterObservationError('binding changed during read')
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'version', 'kind', 'session', 'native', 'process', 'executable'}
                or type(value['version']) is not int or value['version'] != 2
                or value['kind'] != 'native_session_binding'):
            raise ValueError('invalid binding schema')
        session = _parse(b'[HOSTBOT_SESSION] ' + json.dumps(value['session']).encode('ascii'))
        process = value['process']
        if type(process) is not dict or set(process) != {field.name for field in fields(ProcessIdentity)}:
            raise ValueError('invalid process identity')
        identity = ProcessIdentity(**process)
        native = value['native']
        executable = value['executable']
        expected_native = {'boot_id', 'pid', 'start_ticks', 'pid_namespace_device', 'pid_namespace_inode', 'exe_device', 'exe_inode'}
        if (not _valid_identity(identity) or type(native) is not dict or set(native) != expected_native
                or type(executable) is not dict or set(executable) != {'device', 'inode'}
                or any(type(native[k]) is not int or native[k] < 0 for k in expected_native - {'boot_id'})
                or any(type(executable[k]) is not int or executable[k] < 0 for k in executable)
                or native['boot_id'] != identity.boot_id or native['pid'] != identity.pid
                or native['start_ticks'] != identity.start_ticks
                or (native['pid_namespace_device'], native['pid_namespace_inode']) !=
                   (identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode)
                or (native['exe_device'], native['exe_inode']) != (executable['device'], executable['inode'])
                or session['pid'] != identity.pid or session['session_generation'] != generation):
            raise ValueError('inconsistent binding')
        directory = os.fstat(lease.journal_fd)
        if (directory.st_dev, directory.st_ino) != (session['journal_device'], session['journal_inode']):
            raise ValueError('journal inode mismatch')
        lease.check()
        return identity
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RuntimeError) as exc:
        raise WriterObservationError('invalid or inaccessible session binding') from exc


def inspect_recorded_writer(lease, generation):
    """Inspect the recorded process using pidfd and proc; diagnostic only.

    Returns ``running``, ``exited``, ``absent_pid``, ``pid_reused`` or
    ``different_boot``. Any inaccessible proc state, namespace mismatch or
    identity ambiguity raises WriterObservationError. It does not enumerate
    inherited/delegated writers, scan journal events, or modify cursor state.
    """
    identity = _load_binding(lease, generation)
    try:
        boot = _boot_id()
        if boot != identity.boot_id:
            return 'different_boot'
        if _namespace() != (identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode):
            raise WriterObservationError('pid namespace mismatch')
        # Detect a procfs mounted from a different PID namespace.
        with open('/proc/self/stat', encoding='ascii') as proc:
            proc_self = proc.read(65537)
        if len(proc_self) > 65536 or int(proc_self.split(' ', 1)[0]) != os.getpid():
            raise WriterObservationError('proc namespace mismatch')
        current_self, _ = _identity(os.getpid())
        if current_self.pid != os.getpid() or current_self.boot_id != boot:
            raise WriterObservationError('proc namespace mismatch')
        if not hasattr(os, 'pidfd_open'):
            raise WriterObservationError('pidfd unavailable')
        try:
            fd = os.pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            # ESRCH is a kernel PID lookup, not an untrusted missing /proc path.
            if _boot_id() != boot or _namespace() != (identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode):
                raise WriterObservationError('identity context changed')
            lease.check()
            return 'absent_pid'
        try:
            try:
                observed, _ = _identity(identity.pid)
            except FileNotFoundError:
                if _pidfd_exited(fd):
                    lease.check()
                    return 'exited'
                raise WriterObservationError('process identity disappeared without pidfd exit')
            if observed.boot_id != boot or (observed.observer_pid_namespace_device,
                    observed.observer_pid_namespace_inode) != _namespace():
                raise WriterObservationError('identity context changed')
            if observed.start_ticks != identity.start_ticks:
                if observed.start_ticks < identity.start_ticks:
                    raise WriterObservationError('unexpected older process identity')
                lease.check()
                return 'pid_reused'
            if observed.uid != identity.uid:
                raise WriterObservationError('same starttime with different UID')
            result = 'exited' if _pidfd_exited(fd) else 'running'
            lease.check()
            return result
        finally:
            os.close(fd)
    except WriterObservationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WriterObservationError('process identity unavailable') from exc
