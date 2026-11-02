"""Offline generation handoff: one cursor, separate retained journals."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from src.runtime.native_session import NativeSessionReader
from src.runtime.process_ownership import OwnedChild
from src.runtime.production_cycle import ProductionCycleControl
from src.runtime.reservation_marker import publish
from src.runtime.session_lease import SessionLease, SessionLeaseError


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'session'
        lease=SessionLease.create(self.root,enabled=True);lease.close()
        self.control=ProductionCycleControl(self.root,['a','b'])
        # Exercise journal handoff mechanics with simulated writer exclusion.
        # Production has no such capability and remains fail-closed.
        self.exclusion=mock.patch.object(self.control,'_require_writer_exclusion',return_value=None)
        self.exclusion.start();self.addCleanup(self.exclusion.stop)
        self.procs=[];self.owners=[];self.readers=[]
    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:proc.kill()
            proc.wait();proc.stdout.close()
        for reader in self.readers:reader.close()
        for owner in self.owners:owner.close()
        self.control.close();self.temp.cleanup()
    def spawn(self,gen):
        code='''import os,sys,json,time
st=os.stat(sys.argv[1]); obj={'version':1,'pid':os.getpid(),'session_generation':sys.argv[2],
 'journal_device':st.st_dev,'journal_inode':st.st_ino}
os.write(1,b'[HOSTBOT_SESSION] '+json.dumps(obj).encode()+bytes([10]))
while True:time.sleep(1)
'''
        p=subprocess.Popen([sys.executable,'-u','-c',code,str(self.control.lease.journal),gen],
                           stdout=subprocess.PIPE,bufsize=0)
        self.procs.append(p)
        owner=OwnedChild(p);self.owners.append(owner)
        reader=NativeSessionReader(owner,self.control.lease,os.path.realpath(sys.executable))
        self.readers.append(reader);reader.read_and_persist()
        return p,owner
    def event(self,gen,counter=7,match='b'*32):
        journal=self.control.lease.journal
        path=journal/f'start-{gen}-{match}.json'
        payload=json.dumps({'version':1,'event':'game_started','session_generation':gen,
                            'match_id':match,'host_counter':counter}).encode()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:os.write(fd,payload);os.fsync(fd)
        finally:os.close(fd)
        os.fsync(self.control.lease.journal_fd)
        return path
    def reserve(self,gen,counter=7):
        self.control.state.reserve_external(gen,counter)
        publish(self.control.lease.journal,self.control.state,gen,counter)
    def test_two_generations_share_cursor_and_survive_restart(self):
        gen='a'*32;p,owner=self.spawn(gen);old=self.control.lease.journal
        self.reserve(gen);self.event(gen)
        p.kill();self.assertEqual(owner.wait(5),-signal.SIGKILL)
        second=self.control.handoff_after_verified_exit(owner)
        self.assertNotEqual(old,second);self.assertTrue(old.is_dir())
        self.assertTrue(list(old.glob('start-*.ack')))
        self.assertEqual(self.control.state.current,'b')
        gen2='c'*32;p2,owner2=self.spawn(gen2)
        self.reserve(gen2,8);self.event(gen2,8,'d'*32)
        p2.kill();owner2.wait(5)
        third=self.control.handoff_after_verified_exit(owner2)
        self.assertEqual(self.control.state.current,'a')
        self.control.close()
        self.control=ProductionCycleControl(self.root,['a','b'])
        self.assertEqual(self.control.lease.journal,third)
        self.assertEqual(self.control.state.current,'a')
        self.assertEqual(self.control.state.state['index'],0)
        self.assertEqual(self.control.state._find_external('match_id',gen,'b'*32),(7,'b'*32))
        self.assertEqual(self.control.state._find_external('match_id',gen2,'d'*32),(8,'d'*32))
    def test_unconfirmed_exit_or_missing_event_blocks_handoff(self):
        gen='a'*32;p,owner=self.spawn(gen);self.reserve(gen)
        with self.assertRaisesRegex(ValueError,'EXIT_NOT_VERIFIED'):
            self.control.handoff_after_verified_exit(owner)
        p.kill();owner.wait(5)
        with self.assertRaisesRegex(ValueError,'PENDING_UNRESOLVED'):
            self.control.handoff_after_verified_exit(owner)
        self.assertEqual(self.control.state.state['pending']['host_counter'],7)
        self.assertEqual(self.control.lease.journal,self.root/'journal')
        self.assertFalse(list(self.root.glob('journal-*')))
    def test_competing_reaper_cannot_supply_exit_proof(self):
        p,owner=self.spawn('a'*32)
        p.kill();p.wait()
        with self.assertRaisesRegex(ValueError,'EXIT_NOT_VERIFIED'):
            self.control.handoff_after_verified_exit(owner)
        self.assertFalse(list(self.root.glob('journal-*')))
    def test_ack_without_matching_history_refuses_handoff(self):
        gen='a'*32;p,owner=self.spawn(gen)
        path=self.event(gen);os.link(path,path.with_suffix('.ack'));path.unlink()
        p.kill();owner.wait(5)
        with self.assertRaisesRegex(ValueError,'HISTORY_MISMATCH'):
            self.control.handoff_after_verified_exit(owner)
        self.assertFalse(list(self.root.glob('journal-*')))
    def test_failed_catalog_publish_leaves_orphan_and_refuses_reopen(self):
        gen='a'*32;p,owner=self.spawn(gen);p.kill();owner.wait(5)
        with mock.patch('src.runtime.session_lease.atomic_json',side_effect=OSError('injected')):
            with self.assertRaises(OSError):self.control.handoff_after_verified_exit(owner)
        self.assertTrue(list(self.root.glob('journal-*')))
        self.control.close()
        with self.assertRaises(SessionLeaseError):SessionLease(self.root,enabled=True)
    def test_sigkill_after_catalog_rename_reopens_same_cursor(self):
        self.control.close()
        code='''import os,signal,sys
from unittest.mock import patch
from src.runtime.session_lease import SessionLease
from src.runtime.files import atomic_json
lease=SessionLease(sys.argv[1],enabled=True)
def crash_after_rename(*args,**kwargs):
 atomic_json(*args,**kwargs)
 os.kill(os.getpid(),signal.SIGKILL)
with patch('src.runtime.session_lease.atomic_json',side_effect=crash_after_rename):lease._advance_journal()
'''
        proc=subprocess.run([sys.executable,'-c',code,str(self.root)],timeout=5)
        self.assertEqual(proc.returncode,-signal.SIGKILL)
        self.control=ProductionCycleControl(self.root,['a','b'])
        self.assertEqual(self.control.state.current,'a')
        self.assertNotEqual(self.control.lease.journal,self.root/'journal')
        self.assertTrue((self.root/'journal').is_dir())
        self.assertFalse(self.control.state.state['pending'])

    def test_sigkill_during_catalog_publication_blocks_restart(self):
        self.control.close()
        code='''import os,signal,sys
from unittest.mock import patch
from src.runtime.session_lease import SessionLease
lease=SessionLease(sys.argv[1],enabled=True)
def crash(*args,**kwargs):os.kill(os.getpid(),signal.SIGKILL)
with patch('src.runtime.session_lease.atomic_json',side_effect=crash):lease._advance_journal()
'''
        proc=subprocess.run([sys.executable,'-c',code,str(self.root)],timeout=5)
        self.assertEqual(proc.returncode,-signal.SIGKILL)
        self.assertTrue(list(self.root.glob('journal-*')))
        with self.assertRaises(SessionLeaseError):SessionLease(self.root,enabled=True)


if __name__=='__main__':unittest.main()
