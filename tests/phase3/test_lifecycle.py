"""Offline tests for the independent native lifecycle journal reader."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from src.runtime import lifecycle

GEN = 'a' * 32
MID = 'b' * 32
OTHER = 'c' * 32


class LifecycleJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lifecycle-test-')
        self.root = Path(self.temp.name)
        self.spool = self.root / 'journal'
        self.spool.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, generation=GEN, match=MID, counter=1, **overrides):
        value = {'version': 1, 'event': 'game_started', 'session_generation': generation,
                 'match_id': match, 'host_counter': counter}
        value.update(overrides)
        path = self.spool / f'start-{generation}-{match}.json'
        path.write_bytes((json.dumps(value, separators=(',', ':')) + '\n').encode())
        path.chmod(0o600)
        return path

    def scan(self, callback=lambda _: True):
        return lifecycle.scan(self.spool, callback)

    def run_child(self, code, *args):
        return subprocess.run([sys.executable, '-c', code, *map(str, args)],
                              cwd=Path(__file__).resolve().parents[2], timeout=10)

    def test_multiple_events_are_acked_with_precise_order(self):
        first, second = self.event(), self.event(match=OTHER, counter=0x0fffffff)
        events, operations = [], []
        real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink
        def sync(fd):
            operations.append('fsync')
            return real_fsync(fd)
        def link(*args, **kwargs):
            operations.append('link')
            return real_link(*args, **kwargs)
        def unlink(*args, **kwargs):
            operations.append('unlink')
            return real_unlink(*args, **kwargs)
        def persist(event):
            operations.append('callback')
            events.append(event)
            return True
        with mock.patch.object(lifecycle.os, 'fsync', sync), mock.patch.object(lifecycle.os, 'link', link), mock.patch.object(lifecycle.os, 'unlink', unlink):
            result = self.scan(persist)
        self.assertEqual(result, lifecycle.ScanResult(2, 0))
        self.assertEqual(operations, ['fsync', 'callback', 'link', 'fsync', 'unlink', 'fsync',
                                      'callback', 'link', 'fsync', 'unlink', 'fsync'])
        self.assertEqual([e.idempotency_key for e in events], [(GEN, MID), (GEN, OTHER)])
        self.assertEqual(events[1].host_counter, 0x0fffffff)
        for path in (first, second):
            self.assertFalse(path.exists())
            self.assertTrue(path.with_suffix('.ack').exists())
        self.assertEqual(self.scan(), lifecycle.ScanResult(0, 0))

    def test_callback_must_confirm_and_exceptions_leave_event_unacked(self):
        for callback in (lambda _: None, lambda _: False, lambda _: 1):
            with self.subTest(callback=callback):
                path = self.event()
                with self.assertRaises(lifecycle.LifecycleJournalError):
                    self.scan(callback)
                self.assertTrue(path.exists())
                self.assertFalse(path.with_suffix('.ack').exists())
        def fail(_):
            raise ValueError('store failed')
        with self.assertRaisesRegex(ValueError, 'store failed'):
            self.scan(fail)
        self.assertTrue(path.exists())
        self.assertEqual(self.scan(), lifecycle.ScanResult(1, 0))

    def test_matching_ack_recovers_without_callback_and_conflict_is_not_replaced(self):
        path = self.event()
        ack = path.with_suffix('.ack')
        os.link(path, ack)
        self.assertEqual(self.scan(lambda _: self.fail('already acked')), lifecycle.ScanResult(0, 1))
        self.assertFalse(path.exists())
        original = ack.read_bytes()
        path.write_bytes(original)
        path.chmod(0o600)
        ack.unlink()
        ack.write_bytes(b'conflict')
        ack.chmod(0o600)
        with self.assertRaisesRegex(lifecycle.LifecycleJournalError, 'conflicting acknowledgement'):
            self.scan(lambda _: self.fail('conflict'))
        self.assertEqual(ack.read_bytes(), b'conflict')
        self.assertTrue(path.exists())

    def test_invalid_payloads_and_names_are_not_acked(self):
        cases = [
            b'\xff', b'{', b'[]', b'{"version":1,"version":1}', b'{}',
            b' ' * 4097, b'',
            json.dumps({'version': True, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':1}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':True}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':-1}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':0x10000000}).encode(),
            json.dumps({'version': 2, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':1}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':OTHER, 'match_id':MID, 'host_counter':1}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':GEN, 'match_id':OTHER, 'host_counter':1}).encode(),
            json.dumps({'version': 1, 'event':'game_started', 'session_generation':GEN, 'match_id':MID, 'host_counter':1, 'extra':1}).encode(),
        ]
        path = self.spool / f'start-{GEN}-{MID}.json'
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                path.write_bytes(raw); path.chmod(0o600)
                with self.assertRaises(lifecycle.LifecycleJournalError):
                    self.scan(lambda _: self.fail('invalid event delivered'))
                self.assertTrue(path.exists())
                self.assertFalse(path.with_suffix('.ack').exists())
        path.unlink()
        bad = self.spool / f'start-{GEN.upper()}-{MID}.json'
        bad.write_text('{}'); bad.chmod(0o600)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        self.assertTrue(bad.exists())

    def test_symlink_fifo_permissions_and_directory_replacement(self):
        path = self.spool / f'start-{GEN}-{MID}.json'
        target = self.root / 'target'; target.write_text('sensitive')
        path.symlink_to(target)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        self.assertEqual(target.read_text(), 'sensitive')
        path.unlink(); os.mkfifo(path, 0o600)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        path.unlink()
        path = self.event(); path.chmod(0o644)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        path.chmod(0o600)
        ack = path.with_suffix('.ack'); ack.symlink_to(target)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        self.assertTrue(path.exists()); ack.unlink()
        def replace(_):
            self.spool.rename(self.root/'old-journal')
            self.spool.mkdir(mode=0o700)
            return True
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan(replace)
        self.assertTrue((self.root/'old-journal'/path.name).exists())
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_directory_validation_lock_and_callback_mutation(self):
        path = self.event()
        self.spool.chmod(0o755)
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan()
        self.spool.chmod(0o700)
        link = self.root/'link'; link.symlink_to(self.spool, target_is_directory=True)
        with self.assertRaises(OSError):
            lifecycle.scan(link, lambda _: True)
        fd = os.open(self.spool, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(lifecycle.LifecycleJournalError, 'already active'):
                self.scan()
        finally:
            os.close(fd)
        def mutate(_):
            path.write_bytes(b'changed')
            return True
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan(mutate)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())

    def test_filesystem_failures_preserve_or_recover_ack(self):
        path = self.event()
        real_fsync = os.fsync
        calls = 0
        def fail_ack_sync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk failure')
            return real_fsync(fd)
        with mock.patch.object(lifecycle.os, 'fsync', fail_ack_sync):
            with self.assertRaises(lifecycle.LifecycleJournalError):
                self.scan()
        self.assertTrue(path.exists())
        self.assertTrue(path.with_suffix('.ack').exists())
        self.assertEqual(self.scan(lambda _: self.fail('ack recovery reran callback')),
                         lifecycle.ScanResult(0, 1))

    def test_failure_before_scan_sync_or_link_never_discards_event(self):
        path = self.event()
        with mock.patch.object(lifecycle.os, 'fsync', side_effect=OSError('sync failed')):
            with self.assertRaises(lifecycle.LifecycleJournalError):
                self.scan(lambda _: self.fail('callback before scan fsync'))
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())
        with mock.patch.object(lifecycle.os, 'link', side_effect=OSError('link failed')):
            with self.assertRaises(lifecycle.LifecycleJournalError):
                self.scan(lambda _: True)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())
        self.assertEqual(self.scan(), lifecycle.ScanResult(1, 0))

    def test_last_fsync_failure_keeps_durable_ack(self):
        path = self.event()
        real_fsync = os.fsync
        calls = 0
        def fail_final(fd):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('final sync failed')
            return real_fsync(fd)
        with mock.patch.object(lifecycle.os, 'fsync', fail_final):
            with self.assertRaises(lifecycle.LifecycleJournalError):
                self.scan()
        self.assertTrue(path.with_suffix('.ack').exists())
        # If an unsynced unlink is rolled back by a crash, recovery treats the
        # already durable ack as complete, without invoking the callback.
        os.link(path.with_suffix('.ack'), path)
        self.assertEqual(self.scan(lambda _: self.fail('durable ack redelivered')),
                         lifecycle.ScanResult(0, 1))

    def test_replaced_event_even_with_identical_contents_is_not_acked(self):
        path = self.event()
        def replace(_):
            content = path.read_bytes()
            moved = path.with_suffix('.old')
            path.rename(moved)
            path.write_bytes(content)
            path.chmod(0o600)
            return True
        with self.assertRaisesRegex(lifecycle.LifecycleJournalError, 'event changed'):
            self.scan(replace)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())

    def test_ack_symlink_and_unrelated_files_are_never_consumed(self):
        (self.spool/'.start-uncommitted.tmp').write_text('partial')
        (self.spool/'unrelated.json').write_text('not an event')
        orphan = self.spool/f'start-{GEN}-{MID}.ack'
        orphan.write_text('old ack'); orphan.chmod(0o600)
        self.assertEqual(self.scan(lambda _: self.fail('unrelated entry')), lifecycle.ScanResult(0, 0))
        path = self.event()
        orphan.unlink()
        orphan.symlink_to(self.root/'nonexistent')
        with self.assertRaises(lifecycle.LifecycleJournalError):
            self.scan(lambda _: self.fail('symlink ack'))
        self.assertTrue(path.exists())

    def test_real_sigkill_after_durable_callback_before_ack_replays_idempotently(self):
        path = self.event()
        db = self.root/'state.db'
        code = '''
import os, signal, sqlite3, sys
from src.runtime.lifecycle import scan
conn=sqlite3.connect(sys.argv[2]); conn.execute('PRAGMA synchronous=FULL')
conn.execute('CREATE TABLE IF NOT EXISTS events (generation TEXT, match_id TEXT, counter INTEGER, PRIMARY KEY(generation, match_id))'); conn.commit()
def save(event):
    conn.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (*event.idempotency_key, event.host_counter)); conn.commit()
    os.kill(os.getpid(), signal.SIGKILL)
scan(sys.argv[1],save)
'''
        result = self.run_child(code, self.spool, db)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(path.exists()); self.assertFalse(path.with_suffix('.ack').exists())
        with sqlite3.connect(db) as conn:
            def save(event):
                conn.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', (*event.idempotency_key, event.host_counter))
                conn.commit()
                return True
            self.assertEqual(self.scan(save), lifecycle.ScanResult(1, 0))
            self.assertEqual(conn.execute('SELECT * FROM events').fetchall(), [(GEN, MID, 1)])

    def test_real_sigkill_during_ack_recovers_without_callback(self):
        for phase in ('after_link', 'after_ack_fsync'):
            with self.subTest(phase=phase):
                path = self.event(match=MID if phase == 'after_link' else OTHER)
                marker = self.root / (phase + '.saved')
                code = '''
import os, signal, sys
import src.runtime.lifecycle as journal
phase, marker = sys.argv[2:]
def save(event):
    fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,b'saved'); os.fsync(fd); os.close(fd)
    d=os.open(os.path.dirname(marker),os.O_RDONLY|os.O_DIRECTORY); os.fsync(d); os.close(d)
    return True
original_link=journal.os.link
original_unlink=journal.os.unlink
def link(*a,**kw):
    result=original_link(*a,**kw)
    if phase=='after_link': os.kill(os.getpid(),signal.SIGKILL)
    return result
def unlink(*a,**kw):
    if phase=='after_ack_fsync': os.kill(os.getpid(),signal.SIGKILL)
    return original_unlink(*a,**kw)
journal.os.link=link; journal.os.unlink=unlink
journal.scan(sys.argv[1],save)
'''
                result = self.run_child(code, self.spool, phase, marker)
                self.assertEqual(result.returncode, -signal.SIGKILL)
                self.assertTrue(marker.exists()); self.assertTrue(path.exists())
                self.assertTrue(path.with_suffix('.ack').exists())
                self.assertEqual(self.scan(lambda _: self.fail('callback repeated after ACK')),
                                 lifecycle.ScanResult(0, 1))
                self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
