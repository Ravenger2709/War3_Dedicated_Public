"""Real SQLite/journal fixtures; no game, Wine, socket or production inputs."""
from contextlib import closing
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from amp import power_recovery as recovery
from src.runtime.cycle_state import CycleState
from src.runtime.files import atomic_json
from src.runtime.process_ownership import _identity
from src.runtime.session_lease import SessionLease

OLD = '11111111-1111-1111-1111-111111111111'
NEW = '22222222-2222-2222-2222-222222222222'
GEN = 'a' * 32


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'cycle'
        self.lease = SessionLease.create(self.root, enabled=True)
        self.addCleanup(self.lease.close)
        self.cursor = CycleState(self.lease.cursor, ['a', 'b'], lease=self.lease)
        self.journal = self.lease.journal

    def fixture(self, pending=True, binding=True, saved_fence=False):
        st = self.journal.stat()
        atomic_json(self.root / 'native-launch-journal.json', {'version': 1,
                    'journal_device': st.st_dev, 'journal_inode': st.st_ino, 'nonce': 'b' * 32})
        ipc = self.root / 'ipc-journal'; ipc.mkdir(mode=0o700)
        if binding:
            identity, _ = _identity(os.getpid()); identity = replace(identity, boot_id=OLD)
            exe = Path('/proc/self/exe').stat()
            atomic_json(self.journal / ('session-' + GEN + '.json'), {
                'version': 2, 'kind': 'native_session_binding',
                'session': {'version': 2, 'session_generation': GEN, 'pid': identity.pid,
                            'journal_device': st.st_dev, 'journal_inode': st.st_ino,
                            'ipc_device': ipc.stat().st_dev, 'ipc_inode': ipc.stat().st_ino},
                'process': asdict(identity), 'executable': {'device': exe.st_dev, 'inode': exe.st_ino},
                'native': {'boot_id': OLD, 'pid': identity.pid, 'start_ticks': identity.start_ticks,
                           'pid_namespace_device': identity.observer_pid_namespace_device,
                           'pid_namespace_inode': identity.observer_pid_namespace_inode,
                           'exe_device': exe.st_dev, 'exe_inode': exe.st_ino}})
        if saved_fence:
            atomic_json(recovery.fence(self.root, self.lease._catalog['journals'][-1]),
                        {'version': 1, 'boot_id': OLD, 'journal': self.lease._catalog['journals'][-1]})
        if pending:
            self.cursor.reserve_external(GEN, 2)
            atomic_json(self.root / ('catalog-' + GEN + '.json'), {
                'version': 1, 'generation': GEN, 'digest': 'c' * 64,
                'pending': {'request_id': 'd' * 32, 'index': 0, 'position': 0,
                            'counter': 2, 'status': 'staged'}, 'completed': None})
            p = self.journal / ('reservation-' + GEN + '-2.ready')
            p.write_bytes(('version=1\nsession_generation=' + GEN + '\nhost_counter=2\n').encode());p.chmod(0o600)

    def run_recovery(self, boot=NEW):
        self.lease.close()
        with patch.object(recovery, '_boot_id', return_value=boot):
            return recovery.recover(self.root, boot)

    def read_state(self):
        with closing(recovery.database(self.root)) as db:
            return json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]), recovery.history(db)

    def test_reboot_preserves_history_cursor_and_all_native_evidence(self):
        start = {'version': 1, 'event': 'game_started', 'session_generation': 'e'*32,
                 'host_counter': 1, 'match_id': 'f'*32}
        self.cursor.reserve_external('e'*32, 1);self.cursor.consume_external(start)
        self.cursor.consume_close({'version': 1, 'event': 'lobby_closed', 'session_generation': 'e'*32, 'host_counter': 3})
        self.fixture()
        # Bind fixture queue to the actual cursor position.
        qpath = self.root / ('catalog-' + GEN + '.json');q = json.loads(qpath.read_text())
        q['pending']['position'] = 1;q['pending']['index'] = 1;atomic_json(qpath, q)
        before, retained = self.read_state()
        evidence = recovery.fingerprint(self.root, 'journal')
        self.assertTrue(self.run_recovery()['recovered'])
        after, actual = self.read_state()
        self.assertEqual(after, {**before, 'pending': None});self.assertEqual(actual, retained)
        self.assertEqual(recovery.fingerprint(self.root, 'journal'), evidence)
        self.assertTrue((self.root/'native-launch-journal.json').exists())
        lease = SessionLease(self.root, enabled=True)
        try:
            self.assertNotEqual(lease.journal, self.journal)
            self.assertEqual(os.listdir(lease.journal), [])
            self.assertEqual(json.loads(recovery.fence(self.root, lease._catalog['journals'][-1]).read_text())['boot_id'], NEW)
        finally:lease.close()
        self.assertFalse(self.run_recovery()['recovered'])

    def test_real_unacknowledged_start_replays_once_not_a_fake_close(self):
        self.fixture()
        atomic_json(self.journal / ('start-' + GEN + '-' + 'b'*32 + '.json'), {
            'version': 1, 'event': 'game_started', 'session_generation': GEN,
            'match_id': 'b'*32, 'host_counter': 2})
        self.run_recovery()
        state, _ = self.read_state();self.assertEqual(state['index'], 1)
        with closing(recovery.database(self.root)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone(), (1,))
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='closed_lobbies'").fetchone())
        self.run_recovery();self.assertEqual(self.read_state()[0], state)

    def test_same_boot_blocks_without_touching_pending(self):
        self.fixture();before = self.read_state()
        with self.assertRaises(ValueError):self.run_recovery(OLD)
        self.assertEqual(self.read_state(), before)
        self.assertFalse((self.root/recovery.INTENT).exists())

    def test_unbound_launch_requires_old_fence_and_no_pending(self):
        self.fixture(pending=False, binding=False, saved_fence=True)
        self.assertTrue(self.run_recovery()['recovered'])

    def test_missing_boot_evidence_does_not_retire_claim(self):
        self.fixture(pending=False, binding=False)
        with self.assertRaises(ValueError):self.run_recovery()
        self.assertTrue((self.root/'native-launch-journal.json').exists())

    def test_unknown_partial_event_is_preserved_and_blocks(self):
        self.fixture();p=self.journal/'partial.tmp';p.write_text('partial');p.chmod(0o600)
        before=self.read_state()
        with self.assertRaises(RuntimeError):self.run_recovery()
        self.assertEqual(self.read_state(), before);self.assertTrue(p.exists())

    def test_fence_binding_conflict_blocks(self):
        self.fixture(saved_fence=True)
        f=recovery.fence(self.root,self.lease._catalog['journals'][-1]);v=json.loads(f.read_text())
        v['boot_id']=NEW;atomic_json(f,v)
        with self.assertRaises(ValueError):self.run_recovery()

    def test_queue_pending_mismatch_blocks(self):
        self.fixture();p=self.root/('catalog-'+GEN+'.json');v=json.loads(p.read_text())
        v['pending']['counter']=3;atomic_json(p,v)
        with self.assertRaises(ValueError):self.run_recovery()

    def test_held_lease_blocks_recovery(self):
        self.fixture()
        with patch.object(recovery,'_boot_id',return_value=NEW),self.assertRaises(BlockingIOError):
            recovery.recover(self.root,NEW)

    def test_crash_at_each_publication_stage_resumes_without_lost_history(self):
        self.fixture();before, retained=self.read_state();self.lease.close()
        for name in ('intent','mkdir','state','catalog','receipt'):
            with self.subTest(stage=name):
                def crash(at):
                    if at==name:raise InterruptedError()
                with patch.object(recovery,'_boot_id',return_value=NEW),patch.object(recovery,'stage',side_effect=crash):
                    with self.assertRaises(InterruptedError):recovery.recover(self.root,NEW)
                self.assertTrue((self.root/recovery.INTENT).exists())
        self.assertTrue(self.run_recovery()['recovered'])
        after, actual=self.read_state();self.assertEqual(after,{**before,'pending':None});self.assertEqual(actual,retained)
        self.assertEqual(len(json.loads((self.root/'.phase4-journals.json').read_text())['journals']),2)

    def test_second_reboot_during_recovery_resumes(self):
        self.fixture();self.lease.close()
        with patch.object(recovery,'_boot_id',return_value=NEW),patch.object(recovery,'stage',side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):recovery.recover(self.root,NEW)
        third='33333333-3333-3333-3333-333333333333'
        self.assertTrue(self.run_recovery(third)['recovered'])

    def test_corrupt_intent_or_history_after_interruption_is_not_accepted(self):
        self.fixture();self.lease.close()
        with patch.object(recovery,'_boot_id',return_value=NEW),patch.object(recovery,'stage',side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):recovery.recover(self.root,NEW)
        with closing(recovery.database(self.root)) as db,db:
            db.execute('INSERT INTO events VALUES(?,?,?,?)',('e'*32,'f'*32,3,'a'))
        with self.assertRaises(ValueError):self.run_recovery()
        self.assertTrue((self.root/recovery.INTENT).exists())


    def test_real_catalog_close_replays_and_preserves_native_event(self):
        self.fixture()
        event={'version':2,'event':'lobby_closed','session_generation':GEN,'host_counter':2,
               'request_id':'d'*32,'manifest_sha256':'c'*64,'catalog_index':0}
        atomic_json(self.journal/('close-'+GEN+'-2.json'),event)
        self.run_recovery()
        with closing(recovery.database(self.root)) as db:
            self.assertEqual(db.execute('SELECT generation,counter FROM closed_lobbies').fetchall(),[(GEN,2)])
            self.assertEqual(db.execute('SELECT request_id,digest,catalog_index FROM catalog_closed_lobbies').fetchall(),[('d'*32,'c'*64,0)])
        self.assertTrue((self.journal/('close-'+GEN+'-2.json')).exists())
        self.assertEqual(self.read_state()[0]['index'],0)

    def idle_recovery(self, committed):
        self.fixture()
        event = {'version': 3, 'event': 'lobby_closed', 'session_generation': GEN,
                 'host_counter': 2, 'request_id': 'd'*32, 'manifest_sha256': 'c'*64,
                 'catalog_index': 0, 'reason': 'empty_timeout'}
        atomic_json(self.journal/('close-'+GEN+'-2.json'), event)
        if committed:
            self.cursor.consume_close(event)
        self.run_recovery()
        state, retained = self.read_state()
        self.assertEqual(state['index'], 1)
        self.assertIsNone(state['pending'])
        self.assertIsNone(state['last_started'])
        with closing(recovery.database(self.root)) as db:
            self.assertEqual(db.execute('SELECT generation,counter FROM idle_lobbies').fetchall(), [(GEN, 2)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.run_recovery()
        self.assertEqual(self.read_state(), (state, retained))

    def test_idle_close_after_reboot_advances_once_without_creating_match(self):
        self.idle_recovery(False)

    def test_committed_idle_close_replayed_after_reboot_does_not_advance_twice(self):
        self.idle_recovery(True)

    def test_foreign_catalog_close_does_not_create_tombstone(self):
        self.fixture()
        atomic_json(self.journal/('close-'+GEN+'-2.json'),{
            'version':2,'event':'lobby_closed','session_generation':GEN,'host_counter':2,
            'request_id':'e'*32,'manifest_sha256':'c'*64,'catalog_index':0})
        before=self.read_state()
        with self.assertRaises(ValueError):self.run_recovery()
        self.assertEqual(self.read_state(),before)

    def test_corrupt_database_not_replaced(self):
        self.fixture();self.lease.close();p=self.root/'cursor.json.sqlite3'
        p.write_bytes(b'not a database');before=p.read_bytes()
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):self.run_recovery()
        self.assertEqual(p.read_bytes(),before)

    def test_boot_id_must_match_control_host(self):
        self.fixture();before=self.read_state();self.lease.close()
        with patch.object(recovery,'_boot_id',return_value=NEW),self.assertRaises(ValueError):
            recovery.recover(self.root,OLD)
        self.assertEqual(self.read_state(),before)

    def test_clean_start_stamps_fence_without_recovery(self):
        self.assertFalse(self.run_recovery()['recovered'])
        lease=SessionLease(self.root,enabled=True)
        try:
            value=json.loads(recovery.fence(self.root,lease._catalog['journals'][-1]).read_text())
            self.assertEqual(value['boot_id'],NEW)
        finally:lease.close()


class ControlIntegrationTests(unittest.TestCase):
    def test_stopped_control_runs_recovery_before_amendment_without_vendor_binds(self):
        from amp import production
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            instance=Path(temp);(instance/'data/state/cycle').mkdir(parents=True)
            calls=[]
            def run(args,**kw):
                calls.append((args,kw))
                return SimpleNamespace(stdout=b'{"recovered":false}',returncode=0)
            with patch.object(production,'image_preflight'),patch.object(production.subprocess,'run',side_effect=run):
                production.reconfigure_cycle_stopped(instance,{'uid':os.geteuid(),'gid':os.getegid(),
                    'image_id':'sha256:'+'f'*64}, {'map_cycle':['a']})
            self.assertEqual(len(calls),3)
            self.assertIn('def recover(',calls[0][0][-1])
            self.assertEqual(calls[1][0][-1],production._CYCLE_AMEND_SCRIPT)
            self.assertEqual(calls[0][0][calls[0][0].index('--network')+1],'none')
            mounts=[calls[0][0][i+1] for i,a in enumerate(calls[0][0]) if a=='--mount']
            self.assertEqual(len(mounts),1);self.assertIn('dst=/data/state/cycle',mounts[0])
            self.assertEqual(json.loads(calls[1][1]['input']),['a'])

    def test_rejected_recovery_never_runs_rotation_amendment(self):
        from amp import production
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            instance=Path(temp);(instance/'data/state/cycle').mkdir(parents=True)
            calls=[]
            def run(args,**kw):
                calls.append(args)
                if 'run' in args:raise subprocess.CalledProcessError(1,args)
            with patch.object(production,'image_preflight'),patch.object(production.subprocess,'run',side_effect=run):
                with self.assertRaises(production.SettingsError) as error:
                    production.reconfigure_cycle_stopped(instance,{'uid':os.geteuid(),'gid':os.getegid(),
                        'image_id':'sha256:'+'f'*64}, {'map_cycle':['a']})
            self.assertEqual(error.exception.args,('POWER_RECOVERY_BLOCKED',))
            self.assertEqual(len(calls),2);self.assertIn('rm',calls[-1])


if __name__=='__main__':unittest.main()
