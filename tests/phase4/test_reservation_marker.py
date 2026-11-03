import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_state import CycleState
from src.runtime import reservation_marker as marker
from src.runtime.lifecycle import LifecycleJournalError


class ReservationMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'journal'
        self.journal.mkdir(mode=0o700)
        self.path = self.root / 'state.json'
        self.gen = 'a' * 32
        self.state = CycleState(self.path, ['map-a', 'map-b'])
        self.state.reserve_external(self.gen, 42)
        self.name = f'reservation-{self.gen}-42.ready'
        self.target = self.journal / self.name
        self.payload = f'version=1\nsession_generation={self.gen}\nhost_counter=42\n'.encode()

    def publish(self, state=None, generation=None, counter=42, directory=None):
        return marker.publish(directory or self.journal, state or self.state,
                              self.gen if generation is None else generation, counter)

    def test_publish_and_retry_preserve_inode_and_content(self):
        self.assertEqual(self.publish(), self.name)
        info = self.target.stat()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink, info.st_uid), (0o600, 1, os.geteuid()))
        self.assertEqual(self.publish(), self.name)
        self.assertEqual(self.target.stat().st_ino, info.st_ino)
        reopened = CycleState(self.path, ['map-a', 'map-b'])
        self.assertEqual(self.publish(state=reopened), self.name)

    def test_must_match_actual_healthy_persisted_reservation(self):
        for gen, count in [('b' * 32, 42), (self.gen, 43), ('A' * 32, 42),
                           (self.gen, 0), (self.gen, True), (self.gen, 2**28)]:
            with self.subTest(gen=gen, count=count), self.assertRaises(ValueError):
                self.publish(generation=gen, counter=count)
        self.state.state['pending']['preset_id'] = 'map-b'
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.target.exists())
        self.state = CycleState(self.path, ['map-a', 'map-b'])
        self.state._write_failed = True
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.target.exists())

    def test_unreserved_or_consumed_state_is_rejected(self):
        empty = CycleState(self.root / 'empty', ['map-a'])
        with self.assertRaises(ValueError):
            self.publish(state=empty)
        self.state.consume_external({'version': 1, 'event': 'game_started',
            'session_generation': self.gen, 'host_counter': 42, 'match_id': 'b' * 32})
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.target.exists())

    def test_reject_existing_wrong_files_without_modification(self):
        for kind in ('content', 'mode', 'hardlink', 'symlink', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                other = self.root / 'other'
                if kind in ('content', 'mode', 'hardlink'):
                    self.target.write_bytes(b'bad' if kind == 'content' else self.payload)
                    self.target.chmod(0o644 if kind == 'mode' else 0o600)
                    if kind == 'hardlink':
                        os.link(self.target, other)
                elif kind == 'symlink':
                    other.write_bytes(self.payload)
                    self.target.symlink_to(other)
                elif kind == 'fifo':
                    os.mkfifo(self.target, 0o600)
                else:
                    self.target.mkdir()
                before = self.target.lstat()
                with self.assertRaises((ValueError, OSError)):
                    self.publish()
                self.assertEqual(self.target.lstat().st_ino, before.st_ino)
                if kind == 'directory':
                    self.target.rmdir()
                else:
                    self.target.unlink()
                if other.exists():
                    other.unlink()

    def test_reject_symlink_directory_and_wrong_mode(self):
        link = self.root / 'link'
        link.symlink_to(self.journal, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.publish(directory=link)
        self.journal.chmod(0o755)
        with self.assertRaises((ValueError, OSError, LifecycleJournalError)):
            self.publish()
        self.assertFalse(self.target.exists())

    def test_io_failures_and_retry(self):
        real_fsync = os.fsync
        for failing_call in (1, 2, 3):
            with self.subTest(failing_call=failing_call):
                if self.target.exists():
                    self.target.unlink()
                calls = []
                def fsync(fd):
                    calls.append(fd)
                    if len(calls) == failing_call:
                        raise OSError('injected fsync failure')
                    return real_fsync(fd)
                with patch.object(marker.os, 'fsync', side_effect=fsync):
                    with self.assertRaises(OSError):
                        self.publish()
                self.assertEqual(self.publish(), self.name)
                self.assertEqual(self.target.read_bytes(), self.payload)
        with patch.object(marker, '_rename_noreplace', side_effect=OSError('no rename support')):
            self.target.unlink()
            with self.assertRaises(OSError):
                self.publish()
            self.assertFalse(self.target.exists())

    def test_race_never_overwrites_existing_marker(self):
        def conflict(fd, source, target):
            self.target.write_bytes(b'foreign')
            self.target.chmod(0o600)
            raise FileExistsError()
        with patch.object(marker, '_rename_noreplace', side_effect=conflict):
            with self.assertRaises(ValueError):
                self.publish()
        self.assertEqual(self.target.read_bytes(), b'foreign')

    def test_crash_at_each_publication_stage_then_retry(self):
        program = r'''
import os, sys
from src.runtime.cycle_state import CycleState
from src.runtime import reservation_marker as m
s = CycleState(sys.argv[1], ['map-a', 'map-b'])
def crash(stage):
    if stage == sys.argv[3]: os._exit(73)
m._stage = crash
m.publish(sys.argv[2], s, 'a' * 32, 42)
'''
        for stage in ('created', 'written', 'file_synced', 'published', 'directory_synced'):
            with self.subTest(stage=stage):
                if self.target.exists():
                    self.target.unlink()
                proc = subprocess.run([sys.executable, '-c', program, str(self.path), str(self.journal), stage])
                self.assertEqual(proc.returncode, 73)
                if self.target.exists():
                    self.assertEqual(self.target.read_bytes(), self.payload)
                    self.assertEqual(self.target.stat().st_nlink, 1)
                self.assertEqual(self.publish(), self.name)
                self.assertEqual(self.target.read_bytes(), self.payload)


if __name__ == '__main__':
    unittest.main()
