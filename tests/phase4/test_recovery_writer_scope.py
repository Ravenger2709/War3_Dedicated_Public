"""Real offline crash, IPC fencing and descendant-writer regression."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.runtime.native_session import NativeSessionReader
from src.runtime.process_ownership import OwnedChild
from src.runtime.production_cycle import ProductionCycleControl
from src.runtime.session_lease import SessionLease


class WriterScopeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'session'
        lease=SessionLease.create(self.root,enabled=True);lease.close()

    def test_sigkill_after_ipc_creation_blocks_retry_and_preserves_data(self):
        script='''import os,signal,sys
from src.runtime.production_cycle import ProductionCycleControl
c=ProductionCycleControl(sys.argv[1],['one','two'])
c.claim_native_launch();p=c.provision_launch_ipc()
fd=os.open(p/'request-retained',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.write(fd,b'durable');os.fsync(fd);os.close(fd)
d=os.open(p,os.O_RDONLY|os.O_DIRECTORY);os.fsync(d);os.close(d)
os.kill(os.getpid(),signal.SIGKILL)
'''
        proc=subprocess.run([sys.executable,'-c',script,str(self.root)],timeout=10)
        self.assertEqual(proc.returncode,-signal.SIGKILL)
        ipc=self.root/'ipc-journal'
        self.assertEqual((ipc/'request-retained').read_bytes(),b'durable')
        self.assertEqual(ipc.stat().st_mode & 0o777,0o700)
        control=ProductionCycleControl(self.root,['one','two'])
        try:
            self.assertEqual(control.state.current,'one')
            with self.assertRaisesRegex(ValueError,'CYCLE_LAUNCH_UNRESOLVED'):
                control.claim_native_launch()
            with self.assertRaisesRegex(ValueError,'CYCLE_LAUNCH_UNRESOLVED'):
                control.provision_launch_ipc()
            self.assertEqual((ipc/'request-retained').read_bytes(),b'durable')
        finally:control.close()

    def test_clean_restart_before_launch_preserves_cursor(self):
        script="""import sys
from src.runtime.production_cycle import ProductionCycleControl
c=ProductionCycleControl(sys.argv[1],['one','two'])
p=c.state.reserve(7);c.state.started(p['match_id'],7);c.close()
"""
        proc=subprocess.run([sys.executable,'-c',script,str(self.root)],timeout=10)
        self.assertEqual(proc.returncode,0)
        c=ProductionCycleControl(self.root,['one','two'])
        try:
            self.assertEqual(c.state.current,'two')
            c.claim_native_launch()
            self.assertEqual(c.provision_launch_ipc(),self.root/'ipc-journal')
        finally:c.close()

    def test_ipc_is_exclusive_and_failed_attempt_is_not_reused(self):
        c=ProductionCycleControl(self.root,['one'])
        try:
            with self.assertRaisesRegex(ValueError,'LAUNCH_UNRESOLVED'):c.provision_launch_ipc()
            c.claim_native_launch();path=c.provision_launch_ipc()
            self.assertEqual(path,self.root/'ipc-journal')
            before=path.stat()
            with self.assertRaisesRegex(ValueError,'IPC_ALREADY_EXISTS'):c.provision_launch_ipc()
            self.assertEqual((path.stat().st_dev,path.stat().st_ino),(before.st_dev,before.st_ino))
            path.rmdir()
            with self.assertRaisesRegex(ValueError,'IPC_ALREADY_EXISTS'):c.provision_launch_ipc()
            self.assertFalse(path.exists())
        finally:c.close()

    def test_group_probe_fails_closed_on_live_or_ambiguous_group(self):
        control=object.__new__(ProductionCycleControl)
        identity=SimpleNamespace(pid=12345)
        child=SimpleNamespace(identity=identity,_verified_exit=(identity,0))
        control.lease=SimpleNamespace(_native_reader=SimpleNamespace(child=child))
        control._controlled_stop_identity=identity
        with patch('src.runtime.production_cycle.os.killpg',return_value=None):
            with self.assertRaisesRegex(ValueError,'GROUP_NOT_EMPTY'):control._require_writer_exclusion()
        with patch('src.runtime.production_cycle.os.killpg',side_effect=PermissionError()):
            with self.assertRaisesRegex(ValueError,'GROUP_UNVERIFIED'):control._require_writer_exclusion()
        with patch('src.runtime.production_cycle.os.killpg',side_effect=ProcessLookupError()):
            control._require_writer_exclusion()
            child._verified_exit=(identity,-signal.SIGKILL)
            with self.assertRaisesRegex(ValueError,'WRITER_SCOPE_UNPROVEN'):control._require_writer_exclusion()

    @unittest.skipUnless(hasattr(os,'fork') and hasattr(os,'pidfd_open'),'Linux fork and pidfd required')
    def test_verified_child_exit_does_not_prove_descendant_writer_dead(self):
        c=ProductionCycleControl(self.root,['one']);child=None;owner=None;reader=None;descendant=None
        pidfile=self.root/'descendant.pid';trigger=self.root/'write-now';written=self.root/'journal'/'descendant-wrote'
        script='''import json,os,sys,time
journal,pidfile,trigger=sys.argv[1:]
pid=os.fork()
if pid==0:
 os.close(1)
 with open(pidfile,'w') as f:f.write(str(os.getpid()))
 while not os.path.exists(trigger):time.sleep(.01)
 with open(os.path.join(journal,'descendant-wrote'),'w') as f:f.write('still alive')
 while True:time.sleep(1)
st=os.stat(journal)
print('[HOSTBOT_SESSION] '+json.dumps({'version':1,'pid':os.getpid(),'session_generation':'a'*32,
 'journal_device':st.st_dev,'journal_inode':st.st_ino}),flush=True)
while True:time.sleep(1)
'''
        try:
            c.claim_native_launch()
            child=subprocess.Popen([sys.executable,'-u','-c',script,str(c.lease.journal),str(pidfile),str(trigger)],
                                   stdout=subprocess.PIPE,bufsize=0)
            owner=OwnedChild(child);reader=NativeSessionReader(owner,c.lease,os.path.realpath(sys.executable))
            reader.read_and_persist(timeout=5)
            deadline=time.monotonic()+5
            while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.01)
            descendant=int(pidfile.read_text())
            os.kill(child.pid,signal.SIGKILL);self.assertEqual(owner.wait(5),-signal.SIGKILL)
            with self.assertRaisesRegex(ValueError,'CYCLE_WRITER_SCOPE_UNPROVEN'):
                c.handoff_after_verified_exit(owner)
            trigger.write_text('go')
            deadline=time.monotonic()+5
            while not written.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(written.read_text(),'still alive')
            self.assertEqual(c.lease.journal,self.root/'journal')
            self.assertEqual(c.state.current,'one')
        finally:
            if descendant is not None:
                try:os.kill(descendant,signal.SIGKILL)
                except ProcessLookupError:pass
            if reader:reader.close()
            if owner:owner.close()
            if child:
                if child.poll() is None:child.kill();child.wait(timeout=5)
                child.stdout.close()
            c.close()
        reopened=ProductionCycleControl(self.root,['one'])
        try:
            with self.assertRaises(ValueError):reopened.claim_native_launch()
            self.assertTrue(written.exists())
        finally:reopened.close()

if __name__ == '__main__':unittest.main()
