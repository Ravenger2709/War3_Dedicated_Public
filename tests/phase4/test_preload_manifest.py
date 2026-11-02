"""Offline checks for the candidate sealed startup manifest."""
import fcntl
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.runtime import preload_manifest as subject

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalog_fixture', ROOT / 'tests/phase4/test_preload_catalog.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(), 'offline StormLib fixture required')
class StartupManifestTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixture.PreparedCatalogTests('runTest')
        self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)

    def test_sealed_bundle_transfers_ownership_and_preserves_cycle(self):
        prepared = self.catalog.prepare()
        data = subject.encode_startup_manifest(prepared)
        decoded = subject.parse_startup_manifest(data)
        self.assertEqual(decoded['cycle_indices'], (0, 1, 0))
        self.assertEqual(decoded['max_games'], 2)
        self.assertEqual([e['id'] for e in decoded['entries']], ['one', 'two'])
        self.assertEqual([e['sha256'] for e in decoded['entries']], [m.sha256 for m in prepared.pinned.maps])
        self.assertEqual(decoded['accounted_bytes'], prepared.pinned.accounted_bytes)
        bundle = subject.seal_startup_manifest(prepared)
        fds = bundle.pass_fds()
        self.assertEqual(len(fds), 3)
        self.assertEqual(os.pread(bundle.manifest_fd, len(data), 0), data)
        with self.assertRaises(OSError): os.pwrite(bundle.manifest_fd, b'x', 0)
        with self.assertRaisesRegex(ValueError, 'CLOSED'): prepared.pinned.pass_fds()
        bundle.close()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)

    def test_corruption_indices_duplicates_and_budget_fail_closed(self):
        with self.catalog.prepare() as prepared:
            data = subject.encode_startup_manifest(prepared)
            samples = [data[:-1], b'badmagic' + data[8:], data + b'x']
            for offset, value in ((12, 6), (13, 1), (len(data)-1, 32)):
                changed = bytearray(data); changed[offset] = value; samples.append(bytes(changed))
            changed = bytearray(data)
            header = subject._HEADER.size
            entry = subject._ENTRY.unpack_from(data, header)
            next_offset = header + subject._ENTRY.size + subject._SETTINGS.size + entry[3] + entry[4] + entry[5]
            changed[next_offset:next_offset+4] = data[header:header+4]  # duplicate descriptor
            samples.append(bytes(changed))
            for sample in samples:
                with self.subTest(sample=sample[:16], size=len(sample)):
                    with self.assertRaises(ValueError): subject.parse_startup_manifest(sample)

    def test_failed_seal_keeps_prepared_fds_owned(self):
        with self.catalog.prepare() as prepared:
            original = fcntl.fcntl
            def fail_seal(fd, operation, *args):
                if operation == fcntl.F_ADD_SEALS:
                    raise OSError('injected failure')
                return original(fd, operation, *args)
            before = set(os.listdir('/proc/self/fd'))
            with patch.object(subject.fcntl, 'fcntl', side_effect=fail_seal):
                with self.assertRaises(OSError): subject.seal_startup_manifest(prepared)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)
            self.assertEqual(len(prepared.pinned.pass_fds()), 2)

if __name__ == '__main__': unittest.main()
