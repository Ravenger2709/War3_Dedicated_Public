"""Real controller loop: observation must not reap the owned native child."""
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime.cycle_state import CycleState
from src.runtime.session_lease import SessionLease


CHILD = r'''import json, os, signal, sys, time
journal, mode, go = sys.argv[1:]
st=os.stat(journal)
generation='a'*32
record={'version':1,'pid':os.getpid(),'session_generation':generation,
        'journal_device':st.st_dev,'journal_inode':st.st_ino}
os.write(1,b'[HOSTBOT_SESSION] '+json.dumps(record).encode()+b'\n')
def finish(*args):
    payload=json.dumps({'version':1,'event':'lobby_closed',
                        'session_generation':generation,'host_counter':7}).encode()+b'\n'
    directory=os.open(journal,os.O_RDONLY|os.O_DIRECTORY)
    fd=os.open('.fixture-close.tmp',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=directory)
    os.write(fd,payload);os.fsync(fd);os.close(fd)
    os.rename('.fixture-close.tmp','close-'+generation+'-7.json',src_dir_fd=directory,dst_dir_fd=directory)
    os.fsync(directory);os.close(directory)
    sys.exit(0)
signal.signal(signal.SIGTERM,finish)
while True:
    if mode=='natural' and os.path.exists(go):finish()
    time.sleep(.01)
'''


class Backend:
    def __init__(self, root, journal, mode):
        self.root,self.journal,self.mode=root,journal,mode
    def prepare(self): (self.root/'bridge').mkdir(mode=0o700)
    def command(self,name,generation):
        if name!='native': raise AssertionError('realm must not start in fixture')
        return [sys.executable,'-u','-c',CHILD,str(self.journal),self.mode,str(self.root/'go')]
    def listener(self,pid): return False
    def cleanup_prefix(self): return {'cleaned':True}
    def close(self): pass


class FixtureSupervisor(ICCupSupervisor):
    # The production opt-in remains blocked pending IPC. Only this test fixture
    # starts one synthetic child to exercise the real production event loop.
    def _cycle_permits_native_spawn(self):
        return self.child_generations['native']==0


class ControllerPidfdLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.session=self.root/'cycle'
        lease=SessionLease.create(self.session,enabled=True)
        state=CycleState(lease.cursor,['a','b'],lease=lease)
        state.reserve_external('a'*32,7)
        lease.close()
        self.cfg={'mode':'iccup','runtime_dir':str(self.root/'supervisor'),
                  'http':{'host':'127.0.0.1','port':0},
                  'hostbot':{'command':['/unused'],'cwd':str(self.root),'startup_timeout':10,'stop_timeout':2,'lobby_timeout':1},
                  'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
                  'iccup':{**iccup_config({}),'bridge_dir':str(self.root/'bridge'),'native_state_ttl':15},
                  'gameplay':gameplay_config({})}
        self.s=None
    def tearDown(self):
        if self.s is not None:
            self.s.submit('quit');self.s.thread.join(8)
            self.assertFalse(self.s.thread.is_alive())
        self.temp.cleanup()
    def wait_for(self,predicate,timeout=7):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('controller timeout: '+repr(self.s.snapshot())+' '+self.s.output.getvalue())
    def start(self,mode):
        self.s=FixtureSupervisor(self.cfg,output=io.StringIO(),
            backend=Backend(self.root,self.session/'journal',mode),
            cycle_session={'root':str(self.session),'cycle':['a','b']})
        self.s.start()
        self.wait_for(lambda:'native' in self.s.children and self.s.children['native'].session_reader is not None)
        return self.s.children['native']
    def assert_exited_but_blocked(self,child):
        self.wait_for(lambda:(bool(list(self.session.glob('journal-*'))) and
                              not self.s.snapshot().get('cycle',{}).get('pending',True))
                             or self.s.cycle_failed)
        self.assertTrue(self.s.cycle_failed,self.s.output.getvalue())
        self.assertIsNotNone(child.session_owner._verified_exit)
        self.assertEqual(child.session_owner._verified_exit[0],child.session_owner.identity)
        self.assertEqual(child.session_owner._verified_exit[1],0)
        self.assertTrue(list((self.session/'journal').glob('close-*.ack')))
        self.assertFalse(list(self.session.glob('journal-*')))
        self.assertEqual(self.s.snapshot()['cycle']['state'],'storage_error')
    def test_natural_exit_is_observed_without_reaping_before_verified_wait(self):
        child=self.start('natural')
        entered=threading.Event();release=threading.Event()
        original=self.s._stop_realm
        def paused_stop():
            entered.set()
            if not release.wait(5):raise TimeoutError('test gate timed out')
            return original()
        with patch.object(self.s,'_stop_realm',side_effect=paused_stop):
            try:
                (self.root/'go').touch()
                self.assertTrue(entered.wait(5),'controller did not observe natural exit')
                # Dead child is still waitable despite controller and HTTP-style snapshots.
                for _ in range(5):self.s.snapshot()
                self.assertIsNone(child.process.returncode)
                self.assertIsNone(child.session_owner._verified_exit)
            finally:
                release.set()
            self.assert_exited_but_blocked(child)
    def test_lost_pidfd_observation_blocks_cycle_without_killing_child(self):
        from src.runtime.process_ownership import OwnedChild, ProcessOwnershipError
        child=self.start('graceful')
        with patch.object(OwnedChild,'is_running',side_effect=ProcessOwnershipError('injected')):
            self.wait_for(lambda:self.s.cycle_failed)
            os.kill(child.process.pid,0)
            self.assertIsNone(child.process.returncode)
            self.assertIsNone(child.session_owner._verified_exit)
            self.assertFalse(list(self.session.glob('journal-*')))
            self.assertEqual(self.s.snapshot()['cycle']['state'],'storage_error')

    def test_external_reaper_never_produces_verified_handoff(self):
        child=self.start('natural')
        with patch.object(self.s,'_child_alive',return_value=True):
            (self.root/'go').touch()
            self.assertEqual(child.process.wait(timeout=5),0)
        self.wait_for(lambda:self.s.cycle_failed)
        self.assertIsNone(child.session_owner._verified_exit)
        self.assertFalse(list(self.session.glob('journal-*')))
        self.assertEqual(self.s.snapshot()['cycle']['state'],'storage_error')

    def test_controlled_stop_records_wait_and_handoff(self):
        child=self.start('graceful')
        for _ in range(5):self.s.snapshot()
        self.s.submit('stop')
        self.wait_for(lambda:bool(list(self.session.glob('journal-*'))) or self.s.cycle_failed)
        self.assertFalse(self.s.cycle_failed,self.s.output.getvalue())
        self.assertEqual(child.session_owner._verified_exit[1],0)
        self.assertTrue(list((self.session/'journal').glob('close-*.ack')))
        self.assertTrue(list(self.session.glob('journal-*')))


if __name__=='__main__':unittest.main()
