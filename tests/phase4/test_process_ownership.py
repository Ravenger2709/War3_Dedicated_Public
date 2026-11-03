"""Linux pidfd child proof and intentionally unverified observations."""
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import signal
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.process_ownership import (OwnedChild, ProcessOwnershipError,
    inspect_saved_identity, write_unverified_observation)


@unittest.skipUnless(hasattr(os, 'pidfd_open') and hasattr(os, 'P_PIDFD'), 'Linux pidfd wait required')
class ProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='process-owner-')
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name)/'journal'; self.journal.mkdir(mode=0o700)
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None: child.kill()
            child.wait(timeout=5)
            if child.stdout: child.stdout.close()
            if child.stderr: child.stderr.close()

    def spawn(self):
        child = subprocess.Popen([sys.executable, '-u', '-c', 'import time; print("ready",flush=True); time.sleep(60)'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        return child

    def test_real_child_pidfd_wait_and_timeout(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            self.assertEqual(owned.identity.pid, child.pid)
            self.assertGreater(owned.identity.start_ticks, 0)
            self.assertEqual(owned.identity.uid, os.geteuid())
            self.assertEqual(inspect_saved_identity(owned.identity), 'same_identity_visible')
            with self.assertRaises(TimeoutError): owned.wait(timeout=.01)
            child.send_signal(signal.SIGKILL)
            self.assertEqual(owned.wait(timeout=5), -signal.SIGKILL)
            self.assertEqual(inspect_saved_identity(owned.identity), 'absent_unverified')
            with self.assertRaises(ProcessOwnershipError): owned.wait(timeout=0)

    def test_liveness_observation_never_reaps_pidfd_child(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            self.assertTrue(owned.is_running())
            child.kill()
            poller=select.poll();poller.register(owned.fd,select.POLLIN)
            self.assertTrue(poller.poll(5000))
            for _ in range(3):
                self.assertFalse(owned.is_running())
                self.assertIsNone(child.returncode)
                self.assertIsNone(owned._verified_exit)
            self.assertEqual(owned.wait(timeout=5),-signal.SIGKILL)
            self.assertFalse(owned.is_running())
            self.assertEqual(owned._verified_exit,(owned.identity,-signal.SIGKILL))

    def test_lost_pidfd_observation_never_claims_exit(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            with patch('src.runtime.process_ownership.os.waitid',side_effect=PermissionError('injected')):
                with self.assertRaisesRegex(ProcessOwnershipError,'WAIT_UNAVAILABLE'):
                    owned.is_running()
            self.assertIsNone(owned._verified_exit)
            self.assertIsNone(child.returncode)

    def test_competing_reaper_and_wrong_inputs_fail_closed(self):
        for value in (True, os.getpid(), object()):
            with self.subTest(value=value), self.assertRaises(ProcessOwnershipError): OwnedChild(value)
        child = self.spawn()
        with OwnedChild(child) as owned:
            child.kill(); child.wait(timeout=5)
            with self.assertRaisesRegex(ProcessOwnershipError, 'WAIT_UNAVAILABLE'):
                owned.wait(timeout=1)
            with self.assertRaises(ProcessOwnershipError):
                write_unverified_observation(owned, self.journal, 'a'*32)
        self.assertEqual(list(self.journal.iterdir()), [])

    def test_record_is_durable_private_explicitly_unverified_and_no_overwrite(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            name = write_unverified_observation(owned, self.journal, 'a'*32)
            path = self.journal/name
            record = json.loads(path.read_text())
            self.assertEqual(record['kind'], 'unverified_process_observation')
            self.assertIs(record['verified_binding'], False)
            self.assertEqual(record['reported_session_generation'], 'a'*32)
            self.assertEqual(record['process']['pid'], child.pid)
            self.assertEqual(record['process']['start_ticks'], owned.identity.start_ticks)
            info = self.journal.stat()
            self.assertEqual(record['journal'], {'device':info.st_dev, 'inode':info.st_ino})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            before = path.read_bytes()
            other = write_unverified_observation(owned, self.journal, 'b'*32)
            self.assertNotEqual(name, other)
            other_record = json.loads((self.journal/other).read_text())
            self.assertEqual(other_record['process'], record['process'])
            self.assertNotEqual(other_record['reported_session_generation'], record['reported_session_generation'])
            self.assertIs(other_record['verified_binding'], False)
            self.assertEqual(path.read_bytes(), before)

    def test_wrong_generation_or_busy_directory_never_creates_record(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            for bad in ('A'*32, 'b'*31, None, True):
                with self.subTest(bad=bad), self.assertRaises(ProcessOwnershipError):
                    write_unverified_observation(owned, self.journal, bad)
            fd = os.open(self.journal, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ProcessOwnershipError, 'JOURNAL_BUSY'):
                    write_unverified_observation(owned, self.journal, 'a'*32)
            finally:
                os.close(fd)
        self.assertEqual(list(self.journal.iterdir()), [])

    def test_restart_inspection_is_diagnostic_and_detects_pid_reuse(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            identity = owned.identity
            replacement = replace(identity, start_ticks=identity.start_ticks+1)
            with patch('src.runtime.process_ownership._identity', return_value=(replacement, os.getpid())):
                self.assertEqual(inspect_saved_identity(identity), 'pid_reused_or_identity_changed')
            with patch('src.runtime.process_ownership._boot_id', return_value='b'*8+'-bbbb-bbbb-bbbb-'+'b'*12):
                self.assertEqual(inspect_saved_identity(identity), 'different_boot_unverified')
            with patch('src.runtime.process_ownership._namespace', return_value=(identity.observer_pid_namespace_device, identity.observer_pid_namespace_inode+1)):
                self.assertEqual(inspect_saved_identity(identity), 'different_namespace_unverified')
            with patch('src.runtime.process_ownership._identity', side_effect=PermissionError()):
                self.assertEqual(inspect_saved_identity(identity), 'visibility_ambiguous')
            with self.assertRaises(ProcessOwnershipError):
                inspect_saved_identity(replace(identity, pid=-1))

    def test_restart_reads_observation_but_cannot_promote_it_to_proof(self):
        child = self.spawn()
        with OwnedChild(child) as owned:
            name = write_unverified_observation(owned, self.journal, 'a'*32)
            child.kill(); self.assertEqual(owned.wait(timeout=5), -signal.SIGKILL)
        code = """
import json,sys
from src.runtime.process_ownership import ProcessIdentity, inspect_saved_identity
record=json.load(open(sys.argv[1]))
print(json.dumps({'binding':record['verified_binding'], 'status':inspect_saved_identity(ProcessIdentity(**record['process']))}))
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.journal/name)], text=True,
                                capture_output=True, timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout), {'binding':False, 'status':'absent_unverified'})

    def test_pid_reuse_simulation_uses_real_proc_identity_and_fails_closed(self):
        first, second = self.spawn(), self.spawn()
        with OwnedChild(first) as a, OwnedChild(second) as b:
            # Simulate a persisted old lifetime under second's live PID; the
            # current process metadata is genuinely read from /proc.
            old_lifetime = replace(a.identity, pid=b.identity.pid, start_ticks=b.identity.start_ticks+1)
            self.assertEqual(inspect_saved_identity(old_lifetime), 'pid_reused_or_identity_changed')
            self.assertEqual(inspect_saved_identity(b.identity), 'same_identity_visible')

    def test_verified_wait_and_nonempty_journal_do_not_authorize_reconcile(self):
        from src.runtime.cycle_coordinator import CycleCoordinator, ReconciliationUnavailable
        from src.runtime.cycle_state import CycleState
        state = CycleState(Path(self.tmp.name)/'cycle.json', ['one','two'])
        pending = state.reserve_external('a'*32, 7)
        child = self.spawn()
        with OwnedChild(child) as owned:
            name = write_unverified_observation(owned, self.journal, 'a'*32)
            event = self.journal/('start-'+('a'*32)+'-'+('b'*32)+'.json')
            event.write_text(json.dumps({'version':1,'event':'game_started','session_generation':'a'*32,
                                         'match_id':'b'*32,'host_counter':7})+'\n')
            event.chmod(0o600)
            child.kill()
            self.assertEqual(owned.wait(timeout=5), -signal.SIGKILL)
            coordinator = CycleCoordinator(state, self.journal)
            for generation in ('a'*32, 'b'*32):
                with self.assertRaises(ReconciliationUnavailable):
                    coordinator.reconcile_after_native_exit(owned, generation)
            self.assertEqual(state.state['pending'], pending)
            self.assertTrue(event.exists())
            self.assertFalse(event.with_suffix('.ack').exists())
            self.assertFalse(state._find_closed('a'*32, 7))
            self.assertEqual(coordinator.consume().processed, 1)
            self.assertEqual(state.current, 'two')
            self.assertTrue((self.journal/name).exists())

    def test_failure_before_or_after_rename_never_creates_verified_proof(self):
        child = self.spawn()
        real_sync = os.fsync
        with OwnedChild(child) as owned:
            count = 0
            def fail_after_rename(fd):
                nonlocal count
                count += 1
                if count == 2: raise OSError('directory fsync failed')
                return real_sync(fd)
            with patch('src.runtime.process_ownership.os.fsync', side_effect=fail_after_rename):
                with self.assertRaises(OSError):
                    write_unverified_observation(owned, self.journal, 'a'*32)
        records = list(self.journal.glob('unverified-observation-*.json'))
        self.assertEqual(len(records), 1)
        self.assertIs(json.loads(records[0].read_text())['verified_binding'], False)
        self.assertEqual(list(self.journal.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
