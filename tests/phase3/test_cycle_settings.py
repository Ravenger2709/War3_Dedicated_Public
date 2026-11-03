"""Stopped cycle edits retain durable history and never clear uncertain launches."""
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from amp import production
from src.runtime.cycle_state import CycleState
from src.runtime.session_lease import SessionLease

namespace = {'__name__': 'cycle_amend_test'}
exec(compile(production._CYCLE_AMEND_SCRIPT, 'cycle-amend', 'exec'), namespace)
amend = namespace['amend']


class CycleSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'cycle'
        self.lease = SessionLease.create(self.root, enabled=True)
        self.cursor = CycleState(self.lease.cursor, ['a'], lease=self.lease)

    def close(self):
        self.lease.close()

    def test_sqlite_cycle_edit_keeps_start_and_close_history_and_replay_fences(self):
        generation, match = 'a' * 32, 'b' * 32
        self.cursor.reserve_external(generation, 1)
        start = {'version': 1, 'event': 'game_started', 'session_generation': generation,
                 'match_id': match, 'host_counter': 1}
        self.cursor.consume_external(start)
        self.cursor.consume_close({'version': 1, 'event': 'lobby_closed',
                                   'session_generation': generation, 'host_counter': 2})
        last = dict(self.cursor.state['last_started'])
        self.close()
        self.assertTrue(amend(self.root, ['b', 'a']))
        lease = SessionLease(self.root, enabled=True)
        try:
            state = CycleState(lease.cursor, ['b', 'a'], lease=lease)
            self.assertEqual(state.current, 'b')
            self.assertEqual(state.state['last_started'], last)
            self.assertFalse(state.consume_external(start))
            self.assertTrue(state._find_closed(generation, 2))
        finally:
            lease.close()
        self.assertFalse(amend(self.root, ['b', 'a']))

    def test_legacy_edit_and_invalid_cycle(self):
        self.close()
        before = (self.root / 'cursor.json').read_bytes()
        with self.assertRaises(ValueError):
            amend(self.root, [])
        self.assertEqual((self.root / 'cursor.json').read_bytes(), before)
        self.assertTrue(amend(self.root, ['b']))
        self.assertEqual(json.loads((self.root / 'cursor.json').read_text())['cycle'], ['b'])

    def test_pending_reservation_is_preserved(self):
        self.cursor.reserve(1)
        self.close()
        before = (self.root / 'cursor.json').read_bytes()
        with self.assertRaises(ValueError):
            amend(self.root, ['b'])
        self.assertEqual((self.root / 'cursor.json').read_bytes(), before)

    def test_claim_ipc_or_unconsumed_journal_each_block_without_deletion(self):
        self.close()
        for kind in ('native-launch-journal.json', 'ipc-journal', 'journal/unconsumed.json'):
            with self.subTest(kind=kind):
                path = self.root / kind
                if kind == 'ipc-journal':
                    path.mkdir(mode=0o700)
                else:
                    path.write_text('retained')
                    path.chmod(0o600)
                before = (self.root / 'cursor.json').read_bytes()
                with self.assertRaises(ValueError):
                    amend(self.root, ['b'])
                self.assertTrue(path.exists())
                self.assertEqual((self.root / 'cursor.json').read_bytes(), before)
                path.rmdir() if path.is_dir() else path.unlink()

    def test_owned_live_lease_blocks_another_owner(self):
        # A separate descriptor cannot acquire the exclusive session lease.
        try:
            with self.assertRaises(BlockingIOError):
                amend(self.root, ['b'])
        finally:
            self.close()

    def test_invalid_database_state_version_is_not_rewritten(self):
        self.cursor.consume_close({'version': 1, 'event': 'lobby_closed',
                                   'session_generation': 'a' * 32, 'host_counter': 2})
        path = self.cursor.db_path
        self.close()
        with sqlite3.connect(path) as db:
            value = json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            value['version'] = 1
            raw = json.dumps(value)
            db.execute('UPDATE state SET body=? WHERE id=1', (raw,))
        with self.assertRaises(ValueError):
            amend(self.root, ['b'])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0], raw)

    def test_existing_container_rejects_before_any_mutating_docker_run(self):
        instance = Path(self.tmp.name) / 'instance'
        (instance / 'data/state').mkdir(parents=True)
        (instance / 'data/state/cycle').mkdir()
        spec = {'container_name': 'war3-phase3-staging'}
        with patch.object(production, 'image_preflight', side_effect=production.SettingsError('RUNTIME_STILL_RUNNING')), \
                patch.object(production.subprocess, 'run', side_effect=AssertionError('mutating run forbidden')):
            with self.assertRaises(production.SettingsError):
                production.reconfigure_cycle_stopped(instance, spec, {'map_cycle': ['b']})


if __name__ == '__main__':
    unittest.main()
