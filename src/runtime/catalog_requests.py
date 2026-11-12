"""Offline durable catalog selection coordinator; no native process is managed here.

One exclusive owner must serialize this object with CycleState and protect both
state directories against replacement, rollback and deletion. Initialization is
explicit: a missing state on recovery is an error, never a new queue. The caller
must authenticate the native generation and provide the exact startup manifest.
"""
import hashlib
import os
import secrets
from pathlib import Path

from .cycle_state import CycleState
from .files import atomic_json, private_directory, private_json, private_bytes
from .preload_ipc import Request, notify_request, read_ack
from .preload_manifest import parse_startup_manifest
from .reservation_marker import publish as publish_reservation


class CatalogRequestError(ValueError):
    pass


class CatalogRequests:
    def __init__(self, path, ipc_directory, manifest, generation):
        self.path = Path(path)
        self.ipc_directory = Path(ipc_directory)
        parsed = parse_startup_manifest(manifest)
        self.digest = hashlib.sha256(manifest).hexdigest()
        self.entries = tuple((entry['id'],entry['sha256']) for entry in parsed['entries'])
        self.indices = parsed['cycle_indices']
        self.cycle = [parsed['entries'][i]['id'] for i in self.indices]
        Request(generation, self.digest, '0' * 32, 0)
        self.generation = generation
        self.failed = False
        private_directory(self.path.parent)
        self.data = private_json(self.path)
        self._validate()
        # Settle a rename whose directory fsync failed in the previous owner.
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def initialize(cls, path, ipc_directory, manifest, generation):
        # Validate all external data before creating a queue.
        parse_startup_manifest(manifest)
        digest = hashlib.sha256(manifest).hexdigest()
        Request(generation, digest, '0' * 32, 0)
        path = Path(path)
        private_directory(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        os.close(fd)
        # An interruption here leaves an invalid file, requiring reconciliation.
        atomic_json(path, {'version': 1, 'generation': generation, 'digest': digest,
                           'pending': None, 'completed': None})
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return cls(path, ipc_directory, manifest, generation)

    def _validate_record(self, record, completed=False):
        fields = {'request_id', 'index', 'position', 'counter', 'status'}
        if (not isinstance(record, dict) or
                (set(record) != fields and not (completed and set(record) == fields | {'outcome'} and record['outcome'] == 'closed'))):
            raise CatalogRequestError('invalid request state')
        Request(self.generation, self.digest, record['request_id'], record['index'])
        pos = record['position']
        if (type(pos) is not int or not 0 <= pos < len(self.indices)
                or self.indices[pos] != record['index']
                or (record['counter'] is not None and (type(record['counter']) is not int
                    or not 1 <= record['counter'] < 2**28))
                or (record['counter'] is not None and record['status'] != 'staged'
                    and not (completed and record['status'] == 'closed'
                             and record.get('outcome','closed') == 'closed'))
                or record['status'] not in (None, 'staged', 'rejected', 'uncertain', 'closed')
                or (record['status'] == 'closed' and not (completed and record.get('outcome','closed') == 'closed'))):
            raise CatalogRequestError('invalid request state')

    def _validate(self):
        d = self.data
        if (set(d) != {'version', 'generation', 'digest', 'pending', 'completed'}
                or type(d['version']) is not int or d['version'] not in (1, 2)
                or d['generation'] != self.generation or d['digest'] != self.digest):
            raise CatalogRequestError('queue identity mismatch')
        for key in ('pending', 'completed'):
            if d[key] is not None:
                self._validate_record(d[key], completed=(key == 'completed'))
        if d['completed'] is not None and (d['completed']['counter'] is None or d['completed']['status'] not in ('staged','closed')):
            raise CatalogRequestError('invalid completed request')
        if d['version'] == 1 and d['completed'] is not None and d['completed']['status'] == 'closed':
            raise CatalogRequestError('invalid legacy request state')
        if d['pending'] is not None and d['completed'] is not None and d['pending']['request_id'] == d['completed']['request_id']:
            raise CatalogRequestError('duplicate request identity')

    def _save(self, data):
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        try:
            atomic_json(self.path, data)
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            self.failed = True
            raise
        self.data = data

    def _cursor(self, cursor):
        if not isinstance(cursor, CycleState) or cursor._write_failed or cursor.cycle != self.cycle:
            raise CatalogRequestError('cursor mismatch or uncertain')
        if cursor.state['version'] == 3:
            persisted = cursor._load_db()
        else:
            # A database left by an interrupted migration must never be ignored.
            if cursor.db_path.exists() or cursor.db_path.is_symlink():
                raise CatalogRequestError('cursor migration requires reload')
            persisted = private_json(cursor.path)
        cursor._validate(persisted)
        if persisted != cursor.state:
            raise CatalogRequestError('cursor changed')
        return persisted

    def _request(self, record):
        return Request(self.generation, self.digest, record['request_id'], record['index'])

    def enqueue(self, cursor):
        """Persist selection before any lobby or host counter exists.

        The caller exclusively owns the cursor and serializes lifecycle events.
        An unrelated preexisting reservation is a conflict, never a new intent.
        """
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        state = self._cursor(cursor)
        pos = state['index']
        pending = state['pending']
        record = self.data['pending']
        if record is not None:
            if (record['position'] != pos or record['index'] != self.indices[pos]
                    or (pending is not None and
                        (record['counter'] is None or pending.get('session_generation') != self.generation
                         or pending.get('host_counter') != record['counter']))
                    or (pending is None and record['counter'] is not None)):
                raise CatalogRequestError('pending request conflicts with cursor')
            return self._request(record)
        if pending is not None:
            raise CatalogRequestError('cursor already reserved')
        record = {'request_id': secrets.token_hex(16), 'index': self.indices[pos],
                  'position': pos, 'counter': None, 'status': None}
        self._save({**self.data, 'pending': record})
        return self._request(record)

    def bind_reservation(self, cursor, request_id, host_counter, journal):
        """Bind a verified staged selection to the actual lobby and publish its gate.

        The caller must authenticate that this generation/counter identifies the
        lobby created for this selection. The counter is persisted before cursor
        reservation, so a crash cannot silently bind the request to another lobby.
        Retrying the same pair recovers either interrupted operation.
        """
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        if type(host_counter) is not int or not 1 <= host_counter < 2**28:
            raise CatalogRequestError('invalid host counter')
        record = self.data['pending']
        if record is None or record['request_id'] != request_id or record['status'] != 'staged':
            raise CatalogRequestError('request not staged')
        if read_ack(self.ipc_directory, self._request(record)) != 'staged':
            raise CatalogRequestError('staged ACK unavailable')
        state = self._cursor(cursor)
        if state['index'] != record['position'] or self.indices[state['index']] != record['index']:
            raise CatalogRequestError('cursor position mismatch')
        pending = state['pending']
        if record['counter'] is not None and record['counter'] != host_counter:
            raise CatalogRequestError('lobby counter conflict')
        if pending is not None and (record['counter'] is None
                or pending.get('session_generation') != self.generation
                or pending.get('host_counter') != host_counter):
            raise CatalogRequestError('cursor reservation mismatch')
        if (state['version'] == 3 and pending is None and
                (cursor._find_closed(self.generation, host_counter)
                 or cursor._find_external('counter', self.generation, host_counter))):
            raise CatalogRequestError('lobby already closed or started')
        if record['counter'] is None:
            record = {**record, 'counter': host_counter}
            self._save({**self.data, 'pending': record})
        if pending is None:
            cursor.reserve_external(self.generation, host_counter)
        return publish_reservation(journal, cursor, self.generation, host_counter)

    def consume_catalog_close(self, cursor, event):
        """Persist a native v2 close and reconcile its exact selection, if current.

        The lifecycle scanner must authenticate the journal and retry this
        operation until both the cursor tombstone and selection update commit.
        A different late event never retires the current request.
        """
        if (self.failed or not isinstance(event, dict) or event.get('version') not in (2,3)
                or event.get('session_generation') != self.generation
                or event.get('manifest_sha256') != self.digest):
            raise CatalogRequestError('invalid catalog close')
        state = self._cursor(cursor)
        record = self.data['pending']
        completed = self.data['completed']
        request_id = event.get('request_id')
        active = record is not None and record['request_id'] == request_id
        previous = completed is not None and completed['request_id'] == request_id
        candidate = record if active else completed if previous else None
        if candidate is not None:
            if (event.get('session_generation') != self.generation or
                    event.get('manifest_sha256') != self.digest or
                    event.get('catalog_index') != candidate['index'] or
                    candidate['counter'] not in (None, event.get('host_counter'))):
                raise CatalogRequestError('catalog close identity mismatch')
            idle_committed = (event.get('version') == 3 and state['version'] == 3
                              and cursor._find_idle(self.generation,event.get('host_counter')))
            position = (candidate['position']+1)%len(self.indices) if idle_committed else candidate['position']
            if active and (position != state['index'] or candidate['status'] == 'rejected'):
                raise CatalogRequestError('catalog close conflicts with selection')
            if previous and candidate.get('outcome') != 'closed' and candidate['status'] != 'closed':
                raise CatalogRequestError('catalog close conflicts with completion')
        pending = state['pending']
        matches_pair = pending is not None and (pending.get('session_generation'), pending.get('host_counter')) == (
            event.get('session_generation'), event.get('host_counter'))
        if matches_pair and not active:
            raise CatalogRequestError('catalog close cannot clear another reservation')
        if active and pending is not None and not matches_pair:
            raise CatalogRequestError('catalog close conflicts with cursor reservation')
        cursor.consume_close(event)
        if active:
            self._save({**self.data, 'version': 2, 'pending': None,
                        'completed': {**record, 'counter': event['host_counter'], 'status': 'closed', 'outcome': 'closed'}})
            return True
        return False

    def dispatch(self, cursor):
        """Publish the durable request or return its verified ACK status."""
        if self.data['pending'] is None:
            raise CatalogRequestError('no pending request; enqueue explicitly')
        request = self.enqueue(cursor)
        record = self.data['pending']
        # Check existing ACK before any retry; a conflicting ACK fails closed.
        status = read_ack(self.ipc_directory, request)
        if status is None and record['status'] is None:
            try:
                notify_request(self.ipc_directory, request)
            except FileNotFoundError:
                # Native may consume next.ready during the publisher's final
                # verification. Accept only a separately verified matching ACK.
                status = read_ack(self.ipc_directory, request)
                if status is None:
                    raise
            else:
                status = read_ack(self.ipc_directory, request)
        if record['status'] is not None and status != record['status']:
            raise CatalogRequestError('ACK missing or changed')
        if status is not None and record['status'] is None:
            self._save({**self.data, 'pending': {**record, 'status': status}})
        return request, status

    def retire_rejected(self, cursor, request_id):
        """Skip only an immutable, matching negative ACK before any reservation.

        Keep native IPC untouched. The durable receipt is the cross-file intent:
        recovery may observe either cursor value, but advances it exactly once.
        This is an admission failure, never a synthetic match/close event.
        """
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        record = self.data['pending']
        if (record is None or record['request_id'] != request_id
                or record['status'] != 'rejected' or record['counter'] is not None):
            raise CatalogRequestError('request not cleanly rejected')
        if read_ack(self.ipc_directory, self._request(record)) != 'rejected':
            raise CatalogRequestError('rejected ACK unavailable')
        if self.rejection_cleanup_pending(record):
            raise CatalogRequestError('negative ACK mailbox not consumed')
        state = self._cursor(cursor)
        if state['pending'] is not None:
            raise CatalogRequestError('cursor already reserved')
        path = self.path.with_name('rejected-selection-' + self.generation + '-' + request_id + '.json')
        if path.exists() or path.is_symlink():
            intent = private_json(path)
        else:
            if state['index'] != record['position']:
                raise CatalogRequestError('rejection cursor mismatch')
            intent = {'version': 1, 'generation': self.generation, 'digest': self.digest,
                      'request': record, 'before': state,
                      'after': {**state, 'index': (state['index'] + 1) % len(self.indices),
                                'changed_at': cursor.clock()}}
            atomic_json(path, intent)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if (set(intent) != {'version', 'generation', 'digest', 'request', 'before', 'after'}
                or type(intent['version']) is not int or intent['version'] != 1
                or intent['generation'] != self.generation or intent['digest'] != self.digest
                or intent['request'] != record):
            raise CatalogRequestError('rejection intent mismatch')
        before, after = intent['before'], intent['after']
        cursor._validate(before)
        cursor._validate(after)
        if (before['pending'] is not None or before['index'] != record['position']
                or before['cycle'] != self.cycle
                or after != {**before, 'index': (record['position'] + 1) % len(self.indices),
                              'changed_at': after['changed_at']}
                or state not in (before, after)):
            raise CatalogRequestError('rejection state mismatch')
        if read_ack(self.ipc_directory, self._request(record)) != 'rejected':
            raise CatalogRequestError('rejected ACK changed')
        if state != after:
            cursor._save(after)
        # A crash before this write replays the same receipt, not another advance.
        self._save({**self.data, 'pending': None})
        return True

    def rejection_cleanup_pending(self, record):
        """An ACK can precede native's final removal of the exact mailbox."""
        try:
            payload = private_bytes(self.ipc_directory / 'next.ready', 512)
        except FileNotFoundError:
            return False
        if payload != self._request(record).payload():
            raise CatalogRequestError('foreign mailbox during rejection')
        return True

    def complete(self, cursor, request_id):
        """Retire only after the separately verified cursor start was committed."""
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        record = self.data['pending']
        if record is None:
            if (self.data['completed'] and self.data['completed']['request_id'] == request_id
                    and self.data['completed'].get('outcome') != 'closed'
                    and self.data['completed']['status'] != 'closed'):
                return False
            raise CatalogRequestError('unknown request')
        if record['request_id'] != request_id or record['status'] != 'staged' or record['counter'] is None:
            raise CatalogRequestError('request not staged')
        if read_ack(self.ipc_directory, self._request(record)) != 'staged':
            raise CatalogRequestError('staged ACK unavailable')
        state = self._cursor(cursor)
        started = state['last_started']
        if (state['pending'] is not None or state['index'] != (record['position'] + 1) % len(self.indices)
                or started is None or started.get('session_generation') != self.generation
                or started.get('host_counter') != record['counter']
                or started.get('preset_id') != self.cycle[record['position']]):
            raise CatalogRequestError('cursor start mismatch')
        self._save({**self.data, 'pending': None, 'completed': record})
        return True

    def retire_closed(self, cursor, request_id):
        """Retire a staged lobby only after its matching durable fenced close.

        Cursor advancement, if any, was committed with the idle close. As with all
        operations, lifecycle consumption and cursor access must be serialized.
        """
        if self.failed:
            raise CatalogRequestError('storage uncertain; reopen')
        record = self.data['pending']
        if record is None:
            completed = self.data['completed']
            if (completed and completed['request_id'] == request_id
                    and (completed.get('outcome') == 'closed' or completed['status'] == 'closed')):
                return False
            raise CatalogRequestError('unknown request')
        if (record['request_id'] != request_id or record['status'] != 'staged'
                or record['counter'] is None):
            raise CatalogRequestError('request has no bound staged lobby')
        state = self._cursor(cursor)
        if (state['version'] != 3 or state['pending'] is not None
                or state['index'] != ((record['position']+1)%len(self.indices) if cursor._find_idle(self.generation,record['counter']) else record['position'])
                or self.indices[record['position']] != record['index']
                or not cursor._find_closed(self.generation, record['counter'])
                or cursor._find_external('counter', self.generation, record['counter'])):
            raise CatalogRequestError('matching fenced close unavailable')
        self._save({**self.data, 'pending': None, 'completed': {**record, 'outcome': 'closed'}})
        return True
