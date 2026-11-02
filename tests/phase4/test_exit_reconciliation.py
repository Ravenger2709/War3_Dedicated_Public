"""No PID-only or caller-asserted proof may clear a pending reservation."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_coordinator import CycleCoordinator, ReconciliationUnavailable
from src.runtime.cycle_state import CycleState


class ExitReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='exit-reconcile-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root/'journal'; self.journal.mkdir(mode=0o700)
        self.path = self.root/'cycle.json'
        self.generation = 'a'*32
        self.state = CycleState(self.path, ['one', 'two'])
        self.pending = self.state.reserve_external(self.generation, 7)
        self.coordinator = CycleCoordinator(self.state, self.journal)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdout: child.stdout.close()
            if child.stdin: child.stdin.close()

    def spawn(self, code='import time; time.sleep(60)'):
        child = subprocess.Popen([sys.executable, '-u', '-c', code, str(self.journal), self.generation],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        self.addCleanup(child.stderr.close)
        return child

    def expect_blocked(self, child, generation):
        with self.assertRaisesRegex(ReconciliationUnavailable, 'OWNERSHIP_PROOF_UNAVAILABLE'):
            self.coordinator.reconcile_after_native_exit(child, generation)
        restored = CycleState(self.path, ['one', 'two'])
        self.assertEqual(restored.state['pending'], self.pending)
        self.assertEqual(restored.current, 'one')
        self.assertFalse(restored._find_closed(self.generation, 7))

    def test_kill_before_close_and_retry_stay_pending_even_after_wait(self):
        child = self.spawn()
        os.kill(child.pid, signal.SIGKILL)
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        # A real child handle and successful wait are still not bound to native
        # generation, journal and exclusive ownership by the current runtime.
        for _ in range(2): self.expect_blocked(child, self.generation)
        self.assertEqual(list(self.journal.iterdir()), [])

    def test_real_child_pid_and_generation_mismatch_never_clear_pending(self):
        child = self.spawn()
        other = self.spawn()
        try:
            self.expect_blocked(child, 'b'*32)
            self.expect_blocked(other, self.generation)
            # Invalid caller booleans/PIDs cannot act as an ownership capability.
            self.expect_blocked(True, self.generation)
            self.expect_blocked(child.pid, self.generation)
            self.assertIsNone(child.poll()); self.assertIsNone(other.poll())
        finally:
            child.kill(); other.kill()
            child.wait(timeout=5); other.wait(timeout=5)

    def test_event_next_to_death_is_never_dropped_and_real_event_can_be_consumed(self):
        code = '''
import json,os,sys,time
root,gen=sys.argv[1:]
name='start-'+gen+'-'+('b'*32)+'.json'
temp=os.path.join(root,'.event.tmp')
fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
payload={'version':1,'event':'game_started','session_generation':gen,'match_id':'b'*32,'host_counter':7}
os.write(fd,(json.dumps(payload)+'\\n').encode()); os.fsync(fd); os.close(fd)
os.rename(temp,os.path.join(root,name)); d=os.open(root,os.O_RDONLY|os.O_DIRECTORY); os.fsync(d); os.close(d)
print('published',flush=True); time.sleep(60)
'''
        child = self.spawn(code)
        self.assertEqual(child.stdout.readline().strip(), 'published')
        child.kill(); self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        event = self.journal/f'start-{self.generation}-{"b"*32}.json'
        original = event.read_bytes()
        # Refusal must not accidentally scan, ACK, unlink, or tombstone.
        self.expect_blocked(child, self.generation)
        self.assertEqual(event.read_bytes(), original)
        self.assertFalse(event.with_suffix('.ack').exists())
        self.assertEqual(self.coordinator.consume().processed, 1)
        restored = CycleState(self.path, ['one', 'two'])
        self.assertIsNone(restored.state['pending'])
        self.assertEqual(restored.current, 'two')
        self.assertTrue(event.with_suffix('.ack').exists())

    def test_no_read_or_write_occurs_when_proof_is_unavailable(self):
        child = self.spawn(); child.kill(); child.wait(timeout=5)
        # The denial happens before touching journal or state; no helper can be
        # mistaken for a completed scan or a verified process wait.
        with patch('src.runtime.cycle_coordinator.scan', side_effect=AssertionError('must not scan')):
            self.expect_blocked(child, self.generation)


if __name__ == '__main__':
    unittest.main()
