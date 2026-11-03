"""Fail-closed, non-mutating audit of one managed native journal.

The result is an inventory, not proof that no writer can subsequently write.
This does not ACK events or authorize recovery/tombstones.
"""
from dataclasses import dataclass
import os
import re

from .lifecycle import _parse, _read_file
from .reservation_marker import _verify_marker
from .writer_observation import _load_binding


class JournalAuditError(RuntimeError):
    pass


_MARKER = re.compile(r'reservation-([0-9a-f]{32})-([1-9][0-9]{0,8})\.ready\Z', re.ASCII)


@dataclass(frozen=True)
class AuditedEvent:
    event: object
    pending: bool
    acknowledged: bool


def audit(lease, generation):
    """Sync and fully enumerate recognized records under the managed lease.

    Partial, unknown, malformed, inconsistent and conflicting entries fail
    closed. ACK-only entries are returned, never treated as cursor proof. The
    caller must validate durable cursor history and writer fencing separately.
    """
    _load_binding(lease, generation)
    try:
        lease.check()
        os.fsync(lease.journal_fd)
        names = sorted(os.listdir(lease.journal_fd))
        found = {}
        metadata = {}
        for name in names:
            lease.check()
            if name == f'session-{generation}.json':
                continue
            initial = os.stat(name, dir_fd=lease.journal_fd, follow_symlinks=False)
            metadata[name] = (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
            marker = _MARKER.fullmatch(name)
            if marker:
                counter = int(marker[2])
                if marker[1] != generation or not 1 <= counter < 2**28:
                    raise JournalAuditError('foreign or invalid reservation marker')
                payload = f'version=1\nsession_generation={generation}\nhost_counter={counter}\n'.encode('ascii')
                _verify_marker(lease.journal_fd, name, payload)
                continue
            if name.startswith(('start-', 'close-')) and name.endswith(('.json', '.ack')):
                pending = name.endswith('.json')
                event_name = name if pending else name[:-4] + '.json'
                raw, st = _read_file(lease.journal_fd, name)
                named = os.stat(name, dir_fd=lease.journal_fd, follow_symlinks=False)
                if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns):
                    raise JournalAuditError('journal record changed during audit')
                event = _parse(event_name, raw)
                if event.session_generation != generation or not 1 <= event.host_counter < 2**28:
                    raise JournalAuditError('foreign or invalid event')
                previous = found.get(event_name)
                if previous is not None and previous[0] != raw:
                    raise JournalAuditError('conflicting event and acknowledgement')
                if previous is None:
                    previous = [raw, event, False, False]
                    found[event_name] = previous
                previous[2 if pending else 3] = True
                continue
            raise JournalAuditError('unrecognized or partial journal entry: ' + name)
        seen = {}
        for _, event, _, _ in found.values():
            old = seen.setdefault(event.host_counter, event)
            if old != event:
                raise JournalAuditError('conflicting events for lobby pair')
        os.fsync(lease.journal_fd)
        lease.check()
        if names != sorted(os.listdir(lease.journal_fd)):
            raise JournalAuditError('journal changed during audit')
        for name, old in metadata.items():
            now = os.stat(name, dir_fd=lease.journal_fd, follow_symlinks=False)
            if old != (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns):
                raise JournalAuditError('journal entry changed during audit')
        _load_binding(lease, generation)
        return tuple(AuditedEvent(value[1], value[2], value[3]) for _, value in sorted(found.items()))
    except JournalAuditError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise JournalAuditError('journal audit failed') from exc
