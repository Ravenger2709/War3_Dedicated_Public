"""Durable launch fencing: crash/retry never treats an attempt as absent."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.production_cycle import ProductionCycleControl
from src.runtime.session_lease import SessionLease


class LaunchClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'session'
        lease = SessionLease.create(self.root, enabled=True)
        lease.close()

    def test_sigkill_after_claim_refuses_new_attempt_after_restart(self):
        code = '''import os,signal,sys
from src.runtime.production_cycle import ProductionCycleControl
control=ProductionCycleControl(sys.argv[1],['one'])
control.claim_native_launch()
os.kill(os.getpid(),signal.SIGKILL)
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.root)], timeout=10)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        claim = self.root / 'native-launch-journal.json'
        data = json.loads(claim.read_bytes())
        journal = (self.root / 'journal').stat()
        self.assertEqual((data['version'], data['journal_device'], data['journal_inode']),
                         (1, journal.st_dev, journal.st_ino))
        self.assertEqual(len(data['nonce']), 32)
        with ProductionCycleControlContext(self.root) as control:
            with self.assertRaisesRegex(ValueError, 'CYCLE_LAUNCH_UNRESOLVED'):
                control.claim_native_launch()

    def test_unclaimed_nonempty_legacy_journal_is_not_safe_to_reuse(self):
        event = self.root / 'journal' / 'unknown'
        event.write_bytes(b'legacy'); event.chmod(0o600)
        with ProductionCycleControlContext(self.root) as control:
            with self.assertRaisesRegex(ValueError, 'CYCLE_LAUNCH_JOURNAL_NOT_EMPTY'):
                control.claim_native_launch()
        self.assertFalse((self.root / 'native-launch-journal.json').exists())

    def test_failed_fsync_retains_claim_and_retries_fail_closed(self):
        with ProductionCycleControlContext(self.root) as control:
            with patch('src.runtime.production_cycle.os.fsync', side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    control.claim_native_launch()
            with self.assertRaisesRegex(ValueError, 'CYCLE_LAUNCH_UNRESOLVED'):
                control.claim_native_launch()


class ProductionCycleControlContext:
    def __init__(self, root):
        self.control = ProductionCycleControl(root, ['one'])
    def __enter__(self):
        return self.control
    def __exit__(self, *args):
        self.control.close()


if __name__ == '__main__':
    unittest.main()
