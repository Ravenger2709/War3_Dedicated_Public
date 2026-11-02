"""Offline regressions for atomic replacement during publication reads."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.bridge.star_relay import owned_file, RelayError
from src.runtime.files import private_bytes

spec = importlib.util.spec_from_file_location('native_publication_test', Path(__file__).with_name('native-publication-test.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class PublicationFileRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'intent.json'
        self.replacement = self.path.with_name('replacement')
        self.old = self.payload(1)
        self.new = self.payload(2)
        self.write(self.path, self.old)
        self.write(self.replacement, self.new)

    @staticmethod
    def payload(sequence):
        return (json.dumps(dict(version=1, pid=1, generation=1, sequence=sequence,
                                host_counter=0, operation='unpublish', packet_hex='ff020400')) + '\n').encode()

    @staticmethod
    def write(path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    def test_replacement_between_open_and_fstat_is_retried(self):
        for reader in (owned_file, private_bytes, native.intent):
            with self.subTest(reader=reader.__name__):
                self.write(self.path, self.old)
                self.write(self.replacement, self.new)
                real_fstat = os.fstat
                observed = []
                def replacing_fstat(fd):
                    if not observed:
                        os.replace(self.replacement, self.path)
                        info = real_fstat(fd)
                        observed.append(info.st_nlink)
                        return info
                    return real_fstat(fd)
                with patch('os.fstat', side_effect=replacing_fstat):
                    result = reader(self.path)
                self.assertEqual(observed, [0])
                self.assertEqual(result['sequence'] if reader is native.intent else result,
                                 2 if reader is native.intent else self.new)

    def test_unsafe_metadata_is_still_rejected(self):
        for reader, error in ((owned_file, RelayError), (private_bytes, ValueError), (native.intent, AssertionError)):
            with self.subTest(reader=reader.__name__, case='mode'):
                self.path.chmod(0o644)
                with self.assertRaises(error): reader(self.path)
                self.path.chmod(0o600)
            with self.subTest(reader=reader.__name__, case='hardlink'):
                os.link(self.path, self.path.with_name('hardlink'))
                try:
                    with self.assertRaises(error): reader(self.path)
                finally:
                    self.path.with_name('hardlink').unlink()

    def test_repeated_replacements_are_bounded(self):
        for reader, error in ((owned_file, RelayError), (private_bytes, ValueError), (native.intent, AssertionError)):
            with self.subTest(reader=reader.__name__):
                self.write(self.path, self.old)
                real_fstat = os.fstat
                observed = []
                def replacing_fstat(fd):
                    self.write(self.replacement, self.new)
                    os.replace(self.replacement, self.path)
                    info = real_fstat(fd)
                    observed.append(info.st_nlink)
                    return info
                with patch('os.fstat', side_effect=replacing_fstat):
                    with self.assertRaises(error): reader(self.path)
                self.assertEqual(observed, [0, 0, 0])


if __name__ == '__main__':
    unittest.main()
