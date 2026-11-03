import json
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_state import CycleState


class ExternalCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.cycle = ['a', 'b', 'a']
        self.gen = 'a' * 32
        self.match = 'b' * 32

    def open(self):
        return CycleState(self.path, self.cycle)

    def event(self, gen=None, match=None, counter=7):
        return {'version': 1, 'event': 'game_started', 'session_generation': gen or self.gen,
                'match_id': match or self.match, 'host_counter': counter}

    def test_reservation_and_start_survive_restart_and_replay(self):
        state = self.open()
        pending = state.reserve_external(self.gen, 7)
        self.assertEqual(pending, {'session_generation': self.gen, 'host_counter': 7, 'preset_id': 'a'})
        state = self.open()
        self.assertTrue(state.consume_external(self.event()))
        state = self.open()
        self.assertEqual(state.current, 'b')
        self.assertEqual(state.state['last_started'], {**pending, 'match_id': self.match})
        self.assertFalse(state.consume_external(self.event()))
        self.assertEqual(self.open().current, 'b')

    def test_external_reservation_cannot_be_abandoned_without_fence(self):
        state = self.open()
        pending = state.reserve_external(self.gen, 7)
        for value in (None, self.match, '', self.gen):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'LOBBY_MISMATCH'):
                state.abandon(value)
            self.assertEqual(state.state['pending'], pending)
            self.assertEqual(self.open().state['pending'], pending)
        self.assertTrue(state.consume_external(self.event()))

    def test_wrong_epoch_counter_invalid_and_stale_fail_without_changes(self):
        state = self.open()
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            state.consume_external(self.event())
        state.reserve_external(self.gen, 7)
        before = self.path.read_bytes()
        for event in (self.event(gen='c' * 32), self.event(counter=8),
                      {**self.event(), 'version': True}, {**self.event(), 'match_id': 'ABC'},
                      {**self.event(), 'extra': 1}, {**self.event(), 'host_counter': True}):
            with self.subTest(event=event), self.assertRaises(ValueError):
                state.consume_external(event)
            self.assertEqual(self.path.read_bytes(), before)
        state.consume_external(self.event())
        state.reserve_external('c' * 32, 8)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            state.consume_external(self.event(match='d' * 32))
        self.assertFalse(state.consume_external(self.event()))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            state.consume_external(self.event(counter=8))

    def test_old_replays_and_no_reuse_of_same_lobby_pair(self):
        state = self.open()
        first = self.event()
        state.reserve_external(self.gen, 7)
        state.consume_external(first)
        second = self.event(gen='c' * 32, match='d' * 32, counter=7)
        state.reserve_external('c' * 32, 7)
        state.consume_external(second)
        state = self.open()
        self.assertFalse(state.consume_external(first))
        self.assertEqual(state.current, 'a')
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            state.reserve_external(self.gen, 7)

    def test_uncertain_commit_requires_reload_and_recovers_replay(self):
        state = self.open()
        state.reserve_external(self.gen, 7)
        real_write = state._write
        def uncertain(value):
            real_write(value)
            raise OSError('uncertain fsync')
        with patch.object(state, '_write', side_effect=uncertain):
            with self.assertRaises(OSError):
                state.consume_external(self.event())
        with self.assertRaisesRegex(ValueError, 'STORAGE_UNCERTAIN'):
            state.consume_external(self.event())
        self.assertFalse(self.open().consume_external(self.event()))
        self.assertEqual(self.open().current, 'b')

    def test_v1_legacy_state_migrated_without_losing_cursor_or_last(self):
        state = self.open()
        old = state.reserve(2)
        state.started(old['match_id'], 2)
        state = self.open()
        self.assertEqual(state.state['version'], 1)
        state.reserve_external(self.gen, 7)
        state = self.open()
        self.assertEqual(state.current, 'b')
        self.assertEqual(state.state['last_started'], old)
        self.assertEqual(state.state['version'], 3)
        state.consume_external(self.event())
        self.assertEqual(self.open().current, 'a')

    def test_150_starts_and_early_replay_after_restart(self):
        state = self.open()
        first = None
        for i in range(150):
            event = self.event(gen=self.gen, match=f'{i + 1:032x}', counter=i + 1)
            state.reserve_external(self.gen, i + 1)
            self.assertTrue(state.consume_external(event))
            first = first or event
        state = self.open()
        self.assertFalse(state.consume_external(first))
        self.assertEqual(state.current, 'a')
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            state.reserve_external(self.gen, 1)
        with sqlite3.connect(state.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 150)

    def test_v2_history_migration_and_pending_start(self):
        state = self.open()
        legacy = {'session_generation': self.gen, 'match_id': self.match,
                  'host_counter': 7, 'preset_id': 'a'}
        pending = {'session_generation': self.gen, 'host_counter': 8, 'preset_id': 'b'}
        state._save({**state.state, 'version': 2, 'index': 1, 'pending': pending,
                     'last_started': legacy, 'external_history': [legacy]})
        state = self.open()
        self.assertTrue(state.consume_external(self.event(match='c' * 32, counter=8)))
        state = self.open()
        self.assertFalse(state.consume_external(self.event()))
        self.assertEqual(state.current, 'a')

    def test_database_write_error_blocks_until_reopen(self):
        state = self.open()
        state.reserve_external(self.gen, 7)
        with patch.object(state, '_write_db', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                state.consume_external(self.event())
        with self.assertRaisesRegex(ValueError, 'STORAGE_UNCERTAIN'):
            state.consume_external(self.event())
        self.assertTrue(self.open().consume_external(self.event()))

    def test_missing_or_corrupt_database_never_falls_back_to_old_json(self):
        state = self.open()
        state.reserve_external(self.gen, 7)
        state.consume_external(self.event())
        state.db_path.unlink()
        with self.assertRaises(ValueError):
            self.open()
        self.assertEqual(json.loads(self.path.read_text())['version'], 'sqlite')

    def test_process_crash_before_commit_and_after_commit_before_ack(self):
        state = self.open()
        state.reserve_external(self.gen, 7)
        program = r"""
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA synchronous=EXTRA')
conn.execute('BEGIN IMMEDIATE')
conn.execute('INSERT INTO events VALUES (?, ?, ?, ?)', (sys.argv[2], sys.argv[3], 7, 'a'))
conn.execute('UPDATE state SET body=? WHERE id=1', ('corrupt-uncommitted',))
os._exit(17)
"""
        proc = subprocess.run([sys.executable, '-c', program, str(state.db_path), self.gen, self.match])
        self.assertEqual(proc.returncode, 17)
        self.assertEqual(self.open().current, 'a')
        program = r"""
import json, os, sys
from src.runtime.cycle_state import CycleState
s = CycleState(sys.argv[1], ['a', 'b', 'a'])
s.consume_external(json.loads(sys.argv[2]))
os._exit(18)
"""
        proc = subprocess.run([sys.executable, '-c', program, str(self.path), json.dumps(self.event())])
        self.assertEqual(proc.returncode, 18)
        reopened = self.open()
        self.assertEqual(reopened.current, 'b')
        self.assertFalse(reopened.consume_external(self.event()))

    def test_incomplete_migration_database_fails_closed(self):
        state = self.open()
        state.db_path.touch(mode=0o600)
        with self.assertRaises(sqlite3.Error):
            self.open()
        self.assertEqual(json.loads(self.path.read_text())['version'], 1)


if __name__ == '__main__':
    unittest.main()
