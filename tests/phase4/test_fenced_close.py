"""Durable close consumption and its boundary with the cycle cursor."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_coordinator import CycleCoordinator
from src.runtime.cycle_state import CycleState
from src.runtime.lifecycle import LifecycleJournalError, scan


class FencedCloseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fenced-cycle-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spool = self.root/'journal'; self.spool.mkdir(mode=0o700)
        self.path = self.root/'cycle.json'
        self.gen = 'a'*32
        self.cycle = ['one', 'two']

    def state(self):
        return CycleState(self.path, self.cycle)

    def close(self, counter=7, gen=None, payload=None):
        gen = gen or self.gen
        path = self.spool/f'close-{gen}-{counter}.json'
        value = payload if payload is not None else {'version':1, 'event':'lobby_closed', 'session_generation':gen, 'host_counter':counter}
        path.write_text(json.dumps(value)+'\n'); path.chmod(0o600)
        return path

    def start(self, counter=7, gen=None, match='b'*32):
        gen = gen or self.gen
        path = self.spool/f'start-{gen}-{match}.json'
        path.write_text(json.dumps({'version':1,'event':'game_started','session_generation':gen,'host_counter':counter,'match_id':match})+'\n')
        path.chmod(0o600)
        return path

    def test_matching_close_clears_pending_without_advancing_and_blocks_reuse(self):
        state = self.state(); state.reserve_external(self.gen, 7)
        path = self.close()
        self.assertEqual(CycleCoordinator(state, self.spool).consume().processed, 1)
        self.assertFalse(path.exists()); self.assertTrue(path.with_suffix('.ack').exists())
        state = self.state()
        self.assertIsNone(state.state['pending']); self.assertEqual(state.current, 'one')
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            state.reserve_external(self.gen, 7)
        # Redelivery after durable state commit and deletion of the prior ACK.
        path.with_suffix('.ack').unlink(); self.close()
        self.assertEqual(CycleCoordinator(state, self.spool).consume().processed, 1)
        self.assertEqual(self.state().current, 'one')
        state.reserve_external(self.gen, 8)
        self.assertEqual(state.state['pending']['host_counter'], 8)

    def test_unknown_close_is_recorded_without_releasing_other_reservation(self):
        state = self.state(); pending = state.reserve_external(self.gen, 8)
        path = self.close(7)
        CycleCoordinator(state, self.spool).consume()
        self.assertEqual(self.state().state['pending'], pending)
        with self.assertRaisesRegex(ValueError, 'LOBBY_ALREADY_RESERVED'):
            state.reserve_external(self.gen, 7)
        self.start(counter=8); CycleCoordinator(state, self.spool).consume()
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            state.reserve_external(self.gen, 7)
        self.assertTrue(path.with_suffix('.ack').exists())

    def test_missing_close_table_after_schema_upgrade_fails_closed(self):
        state = self.state(); state.reserve_external(self.gen, 7)
        self.close(); CycleCoordinator(state, self.spool).consume()
        with sqlite3.connect(state.db_path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone(), (1,))
            db.execute('DROP TABLE closed_lobbies')
        with self.assertRaisesRegex(ValueError, 'DATABASE_REJECTED'):
            self.state()

    def test_start_close_conflicts_never_ack_or_clear_other_event(self):
        state = self.state(); state.reserve_external(self.gen, 7)
        self.start(); CycleCoordinator(state, self.spool).consume()
        close = self.close()
        with self.assertRaisesRegex(ValueError, 'CLOSE_AFTER_START'):
            CycleCoordinator(state, self.spool).consume()
        self.assertTrue(close.exists()); self.assertFalse(close.with_suffix('.ack').exists())
        close.unlink()
        other = 'c'*32
        self.close(8, other); CycleCoordinator(state, self.spool).consume()
        start = self.start(8, other, 'd'*32)
        with self.assertRaisesRegex(ValueError, 'START_AFTER_CLOSE'):
            CycleCoordinator(state, self.spool).consume()
        self.assertTrue(start.exists()); self.assertFalse(start.with_suffix('.ack').exists())

    def test_malformed_close_is_not_acked(self):
        for payload in ({'version':True,'event':'lobby_closed','session_generation':self.gen,'host_counter':7},
                        {'version':1,'event':'lobby_closed','session_generation':'b'*32,'host_counter':7},
                        {'version':1,'event':'lobby_closed','session_generation':self.gen,'host_counter':8},
                        {'version':1,'event':'lobby_closed','session_generation':self.gen,'host_counter':7,'extra':1}):
            with self.subTest(payload=payload):
                path = self.close(payload=payload)
                with self.assertRaises(LifecycleJournalError):
                    scan(self.spool, lambda _: self.fail('invalid close delivered'))
                self.assertTrue(path.exists()); self.assertFalse(path.with_suffix('.ack').exists())
                path.unlink()
        path = self.spool/f'close-{self.gen}-07.json'; path.write_text('{}'); path.chmod(0o600)
        with self.assertRaises(LifecycleJournalError):
            scan(self.spool, lambda _: True)

    def test_catalog_close_requires_full_identity_and_coordinator(self):
        event={'version':2,'event':'lobby_closed','session_generation':self.gen,'host_counter':7,
               'request_id':'c'*32,'manifest_sha256':'d'*64,'catalog_index':1}
        for key,value in (('request_id','C'*32),('manifest_sha256','d'*63),
                          ('catalog_index',True),('catalog_index',32)):
            malformed={**event,key:value};path=self.close(payload=malformed)
            with self.subTest(key=key,value=value),self.assertRaises(LifecycleJournalError):
                scan(self.spool,lambda _:self.fail('malformed close delivered'))
            path.unlink()
        path=self.close(payload=event)
        with self.assertRaisesRegex(ValueError,'CATALOG_CLOSE_COORDINATOR_REQUIRED'):CycleCoordinator(self.state(),self.spool).consume()
        self.assertTrue(path.exists());self.assertFalse(path.with_suffix('.ack').exists())

    def test_catalog_tombstone_identity_and_legacy_replay_conflicts(self):
        state=self.state()
        event={'version':2,'event':'lobby_closed','session_generation':self.gen,'host_counter':7,
               'request_id':'c'*32,'manifest_sha256':'d'*64,'catalog_index':1}
        self.assertTrue(state.consume_close(event));self.assertFalse(state.consume_close(event))
        self.assertEqual(state._find_catalog_close(self.gen,7),('c'*32,'d'*64,1))
        with sqlite3.connect(state.db_path) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(2,))
        for changed in ({**event,'manifest_sha256':'e'*64},{**event,'request_id':'f'*32},
                        {'version':1,'event':'lobby_closed','session_generation':self.gen,'host_counter':7},
                        {**event,'host_counter':8}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):state.consume_close(changed)
        with self.assertRaises(ValueError):state.reserve_external(self.gen,7)
        self.assertEqual(state.current,'one')

    def test_uncertain_close_transaction_requires_reload_before_ack(self):
        for stage in ('before', 'after'):
            with self.subTest(stage=stage):
                root = self.root/stage; root.mkdir(mode=0o700)
                spool = root/'journal'; spool.mkdir(mode=0o700)
                path = root/'cycle.json'
                state = CycleState(path, self.cycle); state.reserve_external(self.gen, 7)
                event_path = spool/f'close-{self.gen}-7.json'
                event_path.write_text(json.dumps({'version':1,'event':'lobby_closed','session_generation':self.gen,'host_counter':7})+'\n')
                event_path.chmod(0o600)
                original = state._write
                def fail(value):
                    if stage == 'after': original(value)
                    raise OSError('uncertain close transaction')
                with patch.object(state, '_write', side_effect=fail):
                    with self.assertRaises(LifecycleJournalError): CycleCoordinator(state, spool).consume()
                self.assertTrue(event_path.exists())
                self.assertFalse(event_path.with_suffix('.ack').exists())
                with self.assertRaisesRegex(ValueError, 'STORAGE_UNCERTAIN'):
                    CycleCoordinator(state, spool).consume()
                restored = CycleState(path, self.cycle)
                self.assertEqual(restored.state['pending'] is None, stage == 'after')
                self.assertEqual(CycleCoordinator(restored, spool).consume().processed, 1)
                self.assertIsNone(CycleState(path,self.cycle).state['pending'])
                with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
                    CycleState(path,self.cycle).reserve_external(self.gen, 7)

    def test_sigkill_after_cursor_commit_before_ack_replays_close(self):
        self.state().reserve_external(self.gen, 7)
        path = self.close()
        code = '''
import os,signal,sys
import src.runtime.lifecycle as lifecycle
from src.runtime.cycle_state import CycleState
from src.runtime.cycle_coordinator import CycleCoordinator
original=lifecycle.os.link
def die(*args, **kwargs): os.kill(os.getpid(),signal.SIGKILL)
lifecycle.os.link=die
state=CycleState(sys.argv[1],['one','two'])
CycleCoordinator(state,sys.argv[2]).consume()
'''
        proc = subprocess.run([sys.executable, '-c', code, str(self.path), str(self.spool)],
                              cwd=Path(__file__).resolve().parents[2], timeout=8)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertTrue(path.exists()); self.assertFalse(path.with_suffix('.ack').exists())
        state = self.state(); self.assertIsNone(state.state['pending']); self.assertEqual(state.current, 'one')
        self.assertEqual(CycleCoordinator(state,self.spool).consume().processed, 1)
        self.assertTrue(path.with_suffix('.ack').exists())
        with self.assertRaisesRegex(ValueError, 'LOBBY_REUSED'):
            self.state().reserve_external(self.gen, 7)


if __name__ == '__main__':
    unittest.main()
