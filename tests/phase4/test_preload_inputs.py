"""Sealed memfd startup input snapshots; no native preload or IPC involved."""
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime import preload_inputs as subject


class PinnedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a = self.root / 'a.w3x'; self.a.write_bytes(b'first synthetic map')
        self.b = self.root / 'b.w3x'; self.b.write_bytes(b'second synthetic map')

    @staticmethod
    def spec(key, path):
        return subject.MapInput(key, path, hashlib.sha256(path.read_bytes()).hexdigest())

    def pin(self, inputs):
        return subject.pin_preload_inputs(inputs, default_map_bytes=1024, max_games=3)

    def test_two_inputs_duplicate_id_sealed_and_source_changes_are_isolated(self):
        first = self.spec('one', self.a); second = self.spec('two', self.b)
        with self.pin([first, second, first]) as catalog:
            self.assertEqual(catalog.cycle_indices, (0, 1, 0))
            self.assertEqual(len(catalog.pass_fds()), 2)
            fds = catalog.pass_fds()
            self.a.write_bytes(b'modified after pinning')
            self.assertEqual(os.pread(fds[0], 100, 0), b'first synthetic map')
            self.assertEqual(os.pread(fds[1], 100, 0), b'second synthetic map')
            for fd in fds:
                seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
                self.assertEqual(seals & subject._SEALS, subject._SEALS)
                with self.assertRaises(OSError) as raised:
                    os.pwrite(fd, b'x', 0)
                self.assertEqual(raised.exception.errno, errno.EPERM)
        with self.assertRaisesRegex(ValueError, 'CLOSED'):
            catalog.pass_fds()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)

    def test_duplicate_id_conflict_and_wrong_digest_fail(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_ID_CONFLICT'):
            self.pin([self.spec('one', self.a), self.spec('one', self.b)])
        with self.assertRaisesRegex(ValueError, 'DIGEST_MISMATCH'):
            self.pin([subject.MapInput('one', self.a, '0' * 64)])

    def test_final_and_ancestor_symlinks_rejected(self):
        link = self.root / 'map-link.w3x'; link.symlink_to(self.a)
        with self.assertRaises(OSError): self.pin([self.spec('one', link)])
        directory = self.root / 'dir'; directory.mkdir()
        inside = directory / 'map.w3x'; inside.write_bytes(b'data')
        alias = self.root / 'dir-link'; alias.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(OSError): self.pin([self.spec('one', alias / 'map.w3x')])

    def test_map_change_during_copy_is_rejected(self):
        self.a.write_bytes(b'a' * (1024 * 1024 + 32))
        original = subject._write_all
        calls = 0
        def change(fd, data):
            nonlocal calls
            calls += 1
            original(fd, data)
            if calls == 1:
                with self.a.open('r+b') as stream:
                    stream.seek(0); stream.write(b'b')
        with patch.object(subject, '_write_all', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'MAP_CHANGED|DIGEST_MISMATCH'):
                self.pin([self.spec('one', self.a)])

    def test_budget_and_input_limits_fail_before_copy(self):
        huge = self.root / 'huge.w3x'
        with huge.open('wb') as stream: stream.truncate(128 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'MAP_SIZE_REJECTED'):
            self.pin([subject.MapInput('one', huge, 'a' * 64)])
        with huge.open('wb') as stream: stream.truncate(128 * 1024 * 1024)
        with patch.object(subject, '_new_memfd', side_effect=AssertionError('must not allocate')):
            with self.assertRaisesRegex(ValueError, 'BUDGET_REJECTED'):
                subject.pin_preload_inputs([subject.MapInput('one', huge, 'a' * 64)],
                                           default_map_bytes=128 * 1024 * 1024, max_games=5)
        with self.assertRaises(ValueError): self.pin([])
        with self.assertRaises(ValueError): self.pin([self.spec('one', self.a)] * 33)

    def test_partial_failure_closes_every_created_descriptor(self):
        first = self.spec('one', self.a); bad = subject.MapInput('two', self.b, '0' * 64)
        before = set(os.listdir('/proc/self/fd'))
        with self.assertRaisesRegex(ValueError, 'DIGEST_MISMATCH'):
            self.pin([first, bad])
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_optional_local_maps_are_sealed_once_per_id(self):
        paths = [Path(value) for value in os.environ.get('WAR3_KNOWN_MAPS', '').split(':') if value]
        if len(paths) < 2:
            self.skipTest('optional two local map paths not supplied')
        first, second = self.spec('one', paths[0]), self.spec('two', paths[1])
        with subject.pin_preload_inputs([first, second, first],
                                        default_map_bytes=paths[0].stat().st_size, max_games=2) as catalog:
            self.assertEqual(catalog.cycle_indices, (0, 1, 0))
            self.assertEqual(len(catalog.pass_fds()), 2)
            for entry in catalog.maps:
                digest = hashlib.sha256()
                offset = 0
                while offset < entry.size:
                    chunk = os.pread(entry.fd, min(1024 * 1024, entry.size - offset), offset)
                    self.assertTrue(chunk)
                    digest.update(chunk); offset += len(chunk)
                self.assertEqual(digest.hexdigest(), entry.sha256)

    def test_memfd_never_uses_standard_descriptors(self):
        saved = fcntl.fcntl(0, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(0)
        try:
            fd = subject._new_memfd()
            try: self.assertGreaterEqual(fd, 3)
            finally: os.close(fd)
        finally:
            os.dup2(saved, 0); os.close(saved)

    def test_reused_descriptor_is_not_closed_by_catalog(self):
        catalog = self.pin([self.spec('one', self.a)])
        fd = catalog.pass_fds()[0]
        os.close(fd)
        replacement = os.open('/dev/null', os.O_RDONLY)
        try:
            self.assertEqual(replacement, fd)
            with self.assertRaisesRegex(ValueError, 'CHANGED'): catalog.pass_fds()
            catalog.close()
            os.fstat(replacement)
        finally:
            os.close(replacement)

if __name__ == '__main__': unittest.main()
