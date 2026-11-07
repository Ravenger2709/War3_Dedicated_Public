"""Durable round-robin cursor; a start must match a previously reserved lobby.

This module deliberately does not start games. Its caller must be the single
owner of the file and supply confirmed native lifecycle events.
"""
import ctypes
import errno
import json
from contextlib import closing
import math
import sqlite3
import stat
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .files import atomic_json, private_json, no_symlinks, private_directory


_HISTORY_LIMIT = 128


def _hex_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def _counter(value):
    return type(value) is int and 1 <= value < 2**28


def _cycle(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 32 or
            any(not isinstance(x, str) or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', x) is None for x in value)):
        raise ValueError('MAP_CYCLE_REJECTED')
    return list(value)


class CycleState:
    """Persist cursor transitions before acknowledging a native start event."""

    def __init__(self, path, cycle, clock=time.time, *, lease=None):
        self.path, self.cycle, self.clock = Path(path), _cycle(cycle), clock
        self.lease = lease
        self._require_lease()
        if lease is not None:
            if lease.cursor_claimed:
                raise ValueError('MAP_CYCLE_CURSOR_ALREADY_OPEN')
            lease.cursor_claimed = True
        self._write_failed = False
        self.db_path = self.path.with_name(self.path.name + '.sqlite3')
        self._insert_event = None
        self._insert_close = None
        self._insert_catalog_close = None
        self._insert_idle = None
        if self.db_path.exists() or self.db_path.is_symlink():
            state = self._load_db()
            self._validate(state)
            if state['version'] != 3 or state['cycle'] != self.cycle:
                raise ValueError('MAP_CYCLE_CHANGED_REQUIRES_RECONCILIATION')
            self._finish_migration(state)
            self.state = state
            return
        try:
            state = private_json(self.path)
        except ValueError:
            # Invalid existing state must never be replaced with a fresh cursor.
            if self.path.exists() or self.path.is_symlink():
                raise
            state = self._initial()
            self._write(state)
        self._validate(state)
        if state['cycle'] != self.cycle:
            raise ValueError('MAP_CYCLE_CHANGED_REQUIRES_RECONCILIATION')
        self.state = state

    def _require_lease(self):
        from .session_lease import require_cursor
        require_cursor(self.path, self.lease)

    def _initial(self):
        return {'version': 1, 'cycle': self.cycle, 'index': 0,
                'pending': None, 'last_started': None, 'changed_at': self.clock()}

    def _validate(self, state):
        version = state.get('version')
        fields = {'version', 'cycle', 'index', 'pending', 'last_started', 'changed_at'}
        if version == 2:
            fields.add('external_history')
        if (set(state) != fields
                or type(version) is not int or version not in (1, 2, 3)
                or _cycle(state['cycle']) != state['cycle']
                or type(state['index']) is not int or not 0 <= state['index'] < len(state['cycle'])
                or type(state['changed_at']) not in (int, float)
                or not math.isfinite(state['changed_at'])):
            raise ValueError('MAP_CYCLE_STATE_REJECTED')
        def valid_event(event, pending=False):
            if not isinstance(event, dict):
                return False
            keys = set(event)
            legacy = {'match_id', 'preset_id', 'host_counter'}
            external_pending = {'session_generation', 'preset_id', 'host_counter'}
            external_started = legacy | {'session_generation'}
            allowed = (legacy, external_pending) if pending else (legacy, external_started)
            if keys not in allowed:
                return False
            if 'session_generation' in event and (version not in (2, 3) or not _hex_id(event['session_generation'])):
                return False
            if 'match_id' in event and not _hex_id(event['match_id']):
                return False
            return (isinstance(event['preset_id'], str)
                    and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', event['preset_id']) is not None
                    and _counter(event['host_counter']))

        for key in ('pending', 'last_started'):
            event = state[key]
            if event is not None and not valid_event(event, pending=key == 'pending'):
                raise ValueError('MAP_CYCLE_STATE_REJECTED')
        if version == 2:
            history = state['external_history']
            if not isinstance(history, list) or len(history) > _HISTORY_LIMIT:
                raise ValueError('MAP_CYCLE_STATE_REJECTED')
            pairs, identities = set(), set()
            for event in history:
                if not valid_event(event) or set(event) != {'session_generation', 'match_id', 'preset_id', 'host_counter'}:
                    raise ValueError('MAP_CYCLE_STATE_REJECTED')
                pair = (event['session_generation'], event['host_counter'])
                identity = (event['session_generation'], event['match_id'])
                if pair in pairs or identity in identities:
                    raise ValueError('MAP_CYCLE_STATE_REJECTED')
                pairs.add(pair)
                identities.add(identity)
            pending = state['pending']
            if pending and 'session_generation' in pending and (pending['session_generation'], pending['host_counter']) in pairs:
                raise ValueError('MAP_CYCLE_STATE_REJECTED')
            last = state['last_started']
            if last and 'session_generation' in last and (not history or last != history[-1]):
                raise ValueError('MAP_CYCLE_STATE_REJECTED')
        if state['pending'] and state['pending']['preset_id'] != state['cycle'][state['index']]:
            raise ValueError('MAP_CYCLE_STATE_REJECTED')

    def _save(self, state):
        self._require_lease()
        if self._write_failed:
            raise ValueError('MAP_CYCLE_STORAGE_UNCERTAIN')
        self._validate(state)
        try:
            self._write(state)
        except (OSError, sqlite3.Error, ValueError):
            # A failed fsync after rename leaves the on-disk result uncertain.
            # A new owner must reload the file before processing another event.
            self._write_failed = True
            raise
        self.state = state

    def _write(self, state):
        self._require_lease()
        if state['version'] == 3:
            self._write_db(state)
            return
        atomic_json(self.path, state)
        # Persist the rename as well as the contents before acknowledging start.
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @property
    def current(self):
        return self.cycle[self.state['index']]

    @property
    def next(self):
        return self.cycle[(self.state['index'] + 1) % len(self.cycle)]

    def reserve(self, host_counter):
        if self.state['pending'] is not None:
            raise ValueError('MAP_CYCLE_LOBBY_ALREADY_RESERVED')
        event = {'match_id': uuid.uuid4().hex, 'preset_id': self.current,
                 'host_counter': host_counter}
        self._save({**self.state, 'pending': event})
        return dict(event)

    def abandon(self, match_id):
        pending = self.state['pending']
        if pending is None or 'session_generation' in pending or pending.get('match_id') != match_id:
            raise ValueError('MAP_CYCLE_LOBBY_MISMATCH')
        self._save({**self.state, 'pending': None})

    def change_cycle(self, cycle):
        """Change a validated cycle only with no unaccounted lobby start."""
        cycle = _cycle(cycle)
        if self.state['pending'] is not None:
            raise ValueError('MAP_CYCLE_LOBBY_ALREADY_RESERVED')
        if cycle == self.cycle:
            return False
        self._save({**self.state, 'cycle': cycle, 'index': 0,
                    'changed_at': self.clock()})
        self.cycle = cycle
        return True

    def started(self, match_id, host_counter):
        pending = self.state['pending']
        last = self.state['last_started']
        if last and 'session_generation' not in last and last['match_id'] == match_id and last['host_counter'] == host_counter:
            return False
        if pending is None or 'session_generation' in pending or pending['match_id'] != match_id or pending['host_counter'] != host_counter:
            raise ValueError('MAP_CYCLE_START_MISMATCH')
        self._save({**self.state, 'index': (self.state['index'] + 1) % len(self.cycle),
                    'pending': None, 'last_started': pending, 'changed_at': self.clock()})
        return True


    def _connect(self, path=None):
        self._require_lease()
        path = no_symlinks(self.db_path if path is None else path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
        db.execute('PRAGMA synchronous=EXTRA')
        return db

    @staticmethod
    def _has_close_schema(db):
        # The schema flag and table are created atomically with the first
        # tombstone. A missing table must never silently erase prior fences.
        version = db.execute('PRAGMA user_version').fetchone()[0]
        present = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='closed_lobbies'").fetchone())
        catalog = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_closed_lobbies'").fetchone())
        idle = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='idle_lobbies'").fetchone())
        if version not in (0, 1, 2, 3) or present != (version in (1, 2, 3)) or catalog != (version in (2, 3)) or idle != (version == 3):
            raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
        if catalog:
            columns = [row[1] for row in db.execute('PRAGMA table_info(catalog_closed_lobbies)')]
            if columns != ['generation', 'counter', 'request_id', 'digest', 'catalog_index']:
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            if db.execute('SELECT 1 FROM catalog_closed_lobbies c LEFT JOIN closed_lobbies l ON c.generation=l.generation AND c.counter=l.counter WHERE l.generation IS NULL LIMIT 1').fetchone():
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
        if idle:
            if [r[1] for r in db.execute('PRAGMA table_info(idle_lobbies)')] != ['generation','counter']:
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            if db.execute('SELECT 1 FROM idle_lobbies i LEFT JOIN catalog_closed_lobbies c ON i.generation=c.generation AND i.counter=c.counter WHERE c.generation IS NULL LIMIT 1').fetchone():
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
        return present

    def _load_db(self):
        with closing(self._connect()) as db, db:
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            has_closes = self._has_close_schema(db)
            if has_closes:
                if db.execute('SELECT 1 FROM closed_lobbies c JOIN events e ON c.generation=e.generation AND c.counter=e.counter LIMIT 1').fetchone():
                    raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            if row is None:
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            state = json.loads(row[0])
            pending = state.get('pending')
            if (pending and 'session_generation' in pending
                    and has_closes
                    and db.execute('SELECT 1 FROM closed_lobbies WHERE generation=? AND counter=?',
                                   (pending['session_generation'], pending['host_counter'])).fetchone()):
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            last = state.get('last_started')
            if last and 'session_generation' in last:
                record = db.execute('SELECT counter, preset FROM events WHERE generation=? AND match_id=?',
                                    (last['session_generation'], last['match_id'])).fetchone()
                if record != (last['host_counter'], last['preset_id']):
                    raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
            return state

    def _migration_stage(self, stage):
        """Test seam for crash points; production execution does nothing."""

    def _sync_directory(self):
        fd = os.open(self.db_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _finish_migration(self, state):
        """Make a published migration authoritative before allowing new writes."""
        marker = {'version': 'sqlite', 'database': self.db_path.name}
        original = private_json(self.path)
        if original == marker:
            self._sync_directory()
            return
        self._validate(original)
        if original['version'] not in (1, 2):
            raise ValueError('MAP_CYCLE_MIGRATION_REJECTED')
        expected = {key: value for key, value in original.items() if key != 'external_history'}
        expected['version'] = 3
        if state != expected:
            raise ValueError('MAP_CYCLE_MIGRATION_REJECTED')
        history = original.get('external_history', [])
        with closing(self._connect()) as db, db:
            records = db.execute('SELECT generation, match_id, counter, preset FROM events').fetchall()
        if set(records) != {(e['session_generation'], e['match_id'], e['host_counter'], e['preset_id']) for e in history}:
            raise ValueError('MAP_CYCLE_MIGRATION_REJECTED')
        self._sync_directory()
        self._migration_stage('published_synced')
        atomic_json(self.path, marker)
        self._migration_stage('marker_written')
        self._sync_directory()
        self._migration_stage('marker_synced')

    def _enable_sqlite(self):
        self._require_lease()
        if self.state['version'] == 3:
            return
        private_directory(self.path.parent)
        no_symlinks(self.db_path)
        temp = None
        try:
            fd, name = tempfile.mkstemp(prefix='.' + self.db_path.name + '.migration-', dir=self.db_path.parent)
            temp = Path(name)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            self._migration_stage('temp_created')
            state = {key: value for key, value in self.state.items() if key != 'external_history'}
            state['version'] = 3
            self._validate(state)
            with closing(self._connect(temp)) as db, db:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
                db.execute('CREATE TABLE events (generation TEXT NOT NULL, match_id TEXT NOT NULL, counter INTEGER NOT NULL, preset TEXT NOT NULL, PRIMARY KEY(generation, match_id), UNIQUE(generation, counter))')
                self._migration_stage('schema_created')
                for event in self.state.get('external_history', []):
                    db.execute('INSERT INTO events VALUES (?, ?, ?, ?)',
                               (event['session_generation'], event['match_id'], event['host_counter'], event['preset_id']))
                db.execute('INSERT INTO state VALUES (1, ?)', (json.dumps(state, allow_nan=False),))
            self._migration_stage('populated')
            with closing(self._connect(temp)) as db, db:
                if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('MAP_CYCLE_DATABASE_REJECTED')
                row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
                records = db.execute('SELECT generation, match_id, counter, preset FROM events').fetchall()
                expected = {(e['session_generation'], e['match_id'], e['host_counter'], e['preset_id'])
                            for e in self.state.get('external_history', [])}
                if not row or json.loads(row[0]) != state or set(records) != expected:
                    raise ValueError('MAP_CYCLE_MIGRATION_REJECTED')
            fd = os.open(temp, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._migration_stage('validated')
            # Linux renameat2 is atomic and must not replace any existing file.
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, 'renameat2', None)
            if rename is None:
                raise OSError(errno.ENOSYS, 'renameat2 unavailable')
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(temp), -100, os.fsencode(self.db_path), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            self._migration_stage('published')
            self._finish_migration(state)
            self.state = state
        except (OSError, sqlite3.Error, ValueError):
            # Leave any temporary file for explicit recovery; it may not be ours
            # after a race, and never remove or replace a preexisting final file.
            self._write_failed = True
            raise

    def _write_db(self, state):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if self._insert_close is not None:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                db.execute('CREATE TABLE IF NOT EXISTS closed_lobbies (generation TEXT NOT NULL, counter INTEGER NOT NULL, PRIMARY KEY(generation, counter))')
                if self._insert_catalog_close is not None:
                    db.execute('CREATE TABLE IF NOT EXISTS catalog_closed_lobbies (generation TEXT NOT NULL, counter INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, catalog_index INTEGER NOT NULL, PRIMARY KEY(generation, counter))')
                    db.execute('PRAGMA user_version=' + str(max(version,2)))
                elif version == 0:
                    db.execute('PRAGMA user_version=1')
                db.execute('INSERT INTO closed_lobbies VALUES (?, ?)', self._insert_close)
                if self._insert_catalog_close is not None:
                    db.execute('INSERT INTO catalog_closed_lobbies VALUES (?, ?, ?, ?, ?)', self._insert_catalog_close)
            if self._insert_idle is not None:
                db.execute('CREATE TABLE IF NOT EXISTS idle_lobbies (generation TEXT NOT NULL, counter INTEGER NOT NULL, PRIMARY KEY(generation,counter))')
                db.execute('INSERT INTO idle_lobbies VALUES (?, ?)',self._insert_idle)
                db.execute('PRAGMA user_version=3')
            if self._insert_event is not None:
                event = self._insert_event
                db.execute('INSERT INTO events VALUES (?, ?, ?, ?)',
                           (event['session_generation'], event['match_id'], event['host_counter'], event['preset_id']))
            updated = db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state, allow_nan=False),))
            if updated.rowcount != 1:
                raise ValueError('MAP_CYCLE_DATABASE_REJECTED')

    def _find_external(self, column, generation, value):
        try:
            with closing(self._connect()) as db, db:
                return db.execute('SELECT counter, match_id FROM events WHERE generation=? AND ' + column + '=?',
                                  (generation, value)).fetchone()
        except (OSError, sqlite3.Error):
            self._write_failed = True
            raise

    def _find_closed(self, generation, counter):
        try:
            with closing(self._connect()) as db, db:
                if not self._has_close_schema(db):
                    return False
                return bool(db.execute('SELECT 1 FROM closed_lobbies WHERE generation=? AND counter=?',
                                       (generation, counter)).fetchone())
        except (OSError, sqlite3.Error):
            self._write_failed = True
            raise

    def _find_catalog_close(self, generation, counter):
        try:
            with closing(self._connect()) as db, db:
                self._has_close_schema(db)
                if db.execute('PRAGMA user_version').fetchone()[0] < 2:
                    return None
                return db.execute('SELECT request_id, digest, catalog_index FROM catalog_closed_lobbies WHERE generation=? AND counter=?',
                                  (generation, counter)).fetchone()
        except (OSError, sqlite3.Error):
            self._write_failed = True
            raise

    def _find_catalog_close_request(self, request_id):
        try:
            with closing(self._connect()) as db, db:
                self._has_close_schema(db)
                if db.execute('PRAGMA user_version').fetchone()[0] < 2:
                    return None
                return db.execute('SELECT generation, counter FROM catalog_closed_lobbies WHERE request_id=?',
                                  (request_id,)).fetchone()
        except (OSError, sqlite3.Error):
            self._write_failed = True
            raise

    def _find_idle(self, generation, counter):
        try:
            with closing(self._connect()) as db, db:
                self._has_close_schema(db)
                if db.execute('PRAGMA user_version').fetchone()[0] != 3:
                    return False
                return bool(db.execute('SELECT 1 FROM idle_lobbies WHERE generation=? AND counter=?',
                                       (generation, counter)).fetchone())
        except (OSError, sqlite3.Error, ValueError):
            self._write_failed = True
            raise

    def consume_close(self, event):
        """Fence a close; only a native empty-timeout close advances the cursor."""
        if self._write_failed:
            raise ValueError('MAP_CYCLE_STORAGE_UNCERTAIN')
        basic = {'version', 'event', 'session_generation', 'host_counter'}
        extended = basic | {'request_id', 'manifest_sha256', 'catalog_index'}
        idle = isinstance(event, dict) and event.get('version') == 3
        if (not isinstance(event, dict) or type(event.get('version')) is not int or
                event['version'] not in (1, 2, 3) or set(event) != (basic if event['version'] == 1 else extended | {'reason'} if idle else extended)
                or event['event'] != 'lobby_closed' or not _hex_id(event['session_generation'])
                or not _counter(event['host_counter'])):
            raise ValueError('MAP_CYCLE_EXTERNAL_EVENT_REJECTED')
        catalog = event['version'] in (2,3)
        if idle and event['reason'] != 'empty_timeout':
            raise ValueError('MAP_CYCLE_EXTERNAL_EVENT_REJECTED')
        if catalog and (not _hex_id(event['request_id']) or
                not isinstance(event['manifest_sha256'], str) or
                re.fullmatch(r'[0-9a-f]{64}', event['manifest_sha256']) is None or
                type(event['catalog_index']) is not int or not 0 <= event['catalog_index'] < 32):
            raise ValueError('MAP_CYCLE_EXTERNAL_EVENT_REJECTED')
        generation, counter = event['session_generation'], event['host_counter']
        identity = (event['request_id'], event['manifest_sha256'], event['catalog_index']) if catalog else None
        self._enable_sqlite()
        if self._find_closed(generation, counter):
            if self._find_catalog_close(generation, counter) != identity or self._find_idle(generation,counter) != idle:
                raise ValueError('MAP_CYCLE_CLOSE_IDENTITY_CONFLICT')
            return False
        if self._find_external('counter', generation, counter):
            raise ValueError('MAP_CYCLE_CLOSE_AFTER_START')
        if catalog and self._find_catalog_close_request(event['request_id']) is not None:
            raise ValueError('MAP_CYCLE_CLOSE_IDENTITY_CONFLICT')
        pending = self.state['pending']
        matches = pending and pending.get('session_generation') == generation and pending['host_counter'] == counter
        if idle and not matches:
            raise ValueError('MAP_CYCLE_IDLE_RESERVATION_MISMATCH')
        self._insert_idle = (generation,counter) if idle else None
        self._insert_close = (generation, counter)
        if catalog:
            self._insert_catalog_close = (generation, counter, *identity)
        try:
            self._save({**self.state, 'pending': None if matches else pending,
                        'index':(self.state['index']+1)%len(self.cycle) if idle else self.state['index'],
                        'changed_at': self.clock()})
        finally:
            self._insert_close = self._insert_catalog_close = self._insert_idle = None
        return True

    def reserve_external(self, session_generation, host_counter):
        """Durably reserve the current position using the native lobby identity."""
        if self._write_failed:
            raise ValueError('MAP_CYCLE_STORAGE_UNCERTAIN')
        if not _hex_id(session_generation) or not _counter(host_counter):
            raise ValueError('MAP_CYCLE_EXTERNAL_ID_REJECTED')
        if self.state['pending'] is not None:
            raise ValueError('MAP_CYCLE_LOBBY_ALREADY_RESERVED')
        self._enable_sqlite()
        if self._find_external('counter', session_generation, host_counter) or self._find_closed(session_generation, host_counter):
            raise ValueError('MAP_CYCLE_EXTERNAL_LOBBY_REUSED')
        pending = {'session_generation': session_generation, 'host_counter': host_counter,
                   'preset_id': self.current}
        self._save({**self.state, 'pending': pending})
        return dict(pending)

    def consume_external(self, event):
        """Persist a validated native journal start; return False on exact replay."""
        if self._write_failed:
            raise ValueError('MAP_CYCLE_STORAGE_UNCERTAIN')
        if (not isinstance(event, dict) or set(event) !=
                {'version', 'event', 'session_generation', 'match_id', 'host_counter'}
                or type(event['version']) is not int or event['version'] != 1
                or event['event'] != 'game_started'
                or not _hex_id(event['session_generation']) or not _hex_id(event['match_id'])
                or not _counter(event['host_counter'])):
            raise ValueError('MAP_CYCLE_EXTERNAL_EVENT_REJECTED')
        generation, match, counter = event['session_generation'], event['match_id'], event['host_counter']
        if self.state['version'] == 3 and self._find_closed(generation, counter):
            raise ValueError('MAP_CYCLE_START_AFTER_CLOSE')
        if self.state['version'] == 3:
            previous = self._find_external('match_id', generation, match)
            if previous:
                if previous[0] != counter:
                    raise ValueError('MAP_CYCLE_START_MISMATCH')
                return False
        elif self.state['version'] == 2:
            for previous in self.state['external_history']:
                if (previous['session_generation'], previous['match_id']) == (generation, match):
                    if previous['host_counter'] != counter:
                        raise ValueError('MAP_CYCLE_START_MISMATCH')
                    return False
        pending = self.state['pending']
        if (pending is None or pending.get('session_generation') != generation
                or pending['host_counter'] != counter or 'match_id' in pending):
            raise ValueError('MAP_CYCLE_START_MISMATCH')
        self._enable_sqlite()
        started = {**pending, 'match_id': match}
        self._insert_event = started
        try:
            self._save({**self.state, 'index': (self.state['index'] + 1) % len(self.cycle),
                        'pending': None, 'last_started': started, 'changed_at': self.clock()})
        finally:
            self._insert_event = None
        return True
