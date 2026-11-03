#!/usr/bin/env python3
"""Exercise only the native manifest envelope; there is no catalog consumer yet."""
import fcntl
import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.runtime import preload_manifest as manifest
from src.runtime.preload_inputs import _new_memfd, _SEALS, _write_all

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

fixtures = load('manifest_catalog_fixture', ROOT / 'tests/phase4/test_preload_catalog.py')
gameplay = load('manifest_native_fixture', ROOT / 'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(), 'offline StormLib fixture required')
class NativeEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixtures.PreparedCatalogTests('runTest'); self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)
        self.native_fixture = gameplay.GameplayTests('runTest'); self.native_fixture.setUp()
        self.addCleanup(self.native_fixture.tearDown)
        self.addCleanup(self.native_fixture.doCleanups)

    def test_two_maps_repeated_cycle_and_invalid_envelopes(self):
        prepared = self.catalog.prepare()
        self.addCleanup(prepared.close)
        data = manifest.encode_startup_manifest(prepared)
        descriptors = []
        def candidate(value, seal=True):
            fd = _new_memfd(); descriptors.append(fd); _write_all(fd, value)
            if seal: fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
            return fd
        good = candidate(data)
        bad_magic = candidate(b'badmagic' + data[8:])
        wrong_hash = bytearray(data); wrong_hash[manifest._HEADER.size + 12] ^= 1
        bad_hash = candidate(wrong_hash)
        wrong_fd = bytearray(data); wrong_fd[manifest._HEADER.size:manifest._HEADER.size+4] = (0x7fffffff).to_bytes(4, 'little')
        bad_fd = candidate(wrong_fd)
        bad_index = candidate(data[:-1] + b'\x20')
        first = manifest._HEADER.size
        first_entry = manifest._ENTRY.unpack_from(data, first)
        next_entry = first + manifest._ENTRY.size + manifest._SETTINGS.size + first_entry[3] + first_entry[4] + first_entry[5]
        duplicate_fd = bytearray(data); duplicate_fd[next_entry:next_entry+4] = data[first:first+4]
        duplicate_id = bytearray(data)
        first_id = first + manifest._ENTRY.size + manifest._SETTINGS.size
        second_id = next_entry + manifest._ENTRY.size + manifest._SETTINGS.size
        duplicate_id[second_id:second_id+3] = data[first_id:first_id+3]
        bad_id = bytearray(data); bad_id[first_id] = ord('!')
        duplicates = [candidate(duplicate_fd), candidate(duplicate_id), candidate(bad_id)]
        settings = first + manifest._ENTRY.size
        def set_number(index, value):
            changed = bytearray(data)
            changed[settings+4*index:settings+4*index+4] = value.to_bytes(4, 'little')
            return changed
        invalid_settings = [candidate(set_number(0, 13)), candidate(set_number(1, 2)),
                            candidate(set_number(6, 1)), candidate(set_number(8, 601))]
        valid_cooldown = candidate(set_number(8, 0))
        auto_off = set_number(1, 0); auto_off[settings+8:settings+12] = (12).to_bytes(4, 'little')
        valid_auto_off = candidate(auto_off)
        bad_config = bytearray(data)
        cfg_position = first_id + first_entry[3] + first_entry[4]
        bad_config[cfg_position:cfg_position+3] = b'BAD'
        invalid_cfg = candidate(bad_config)
        unsealed = candidate(data, False)
        self.addCleanup(lambda: [os.close(fd) for fd in descriptors])
        n = self.native_fixture.native(); n.passed_fds = (*prepared.pinned.pass_fds(), *descriptors); n.start()
        def check(fd, games=2, default=None, limit=1024*1024*1024):
            if default is None: default = prepared.default_map_bytes
            command = f'envelope {fd} {games} {default} 0 {limit}'
            n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
            result = n.marker('TEST ENVELOPE ').split()
            n.marker('TEST COMMAND '+command)
            return int(result[-2])
        self.assertEqual(check(good), 1)
        self.assertEqual(check(valid_cooldown), 1)
        self.assertEqual(check(valid_auto_off), 1)
        for fd in (bad_magic, bad_hash, bad_fd, bad_index, unsealed, -1, invalid_cfg, *duplicates, *invalid_settings): self.assertEqual(check(fd), 0)
        self.assertEqual(check(good, games=1), 0)
        self.assertEqual(check(good, default=prepared.default_map_bytes+1), 0)
        self.assertEqual(check(good, limit=1), 0)
        self.assertIsNone(n.host_state()['lobby'])

    def test_utf8_lobby_name_is_supported_and_bad_utf8_rejected(self):
        import json
        self.catalog.doc['presets'][0]['lobby_name'] = 'Карта'
        self.catalog.preset_path.write_text(json.dumps(self.catalog.doc))
        with self.catalog.prepare(('one',)) as prepared:
            data = manifest.encode_startup_manifest(prepared)
            entry = manifest._ENTRY.unpack_from(data, manifest._HEADER.size)
            name_start = manifest._HEADER.size + manifest._ENTRY.size + manifest._SETTINGS.size + entry[3]
            bad = bytearray(data); bad[name_start] = 0xff
            nonprintable = bytearray(data); nonprintable[name_start:name_start+2] = b'\xc2\xa0'
            unsupported = bytearray(data); unsupported[name_start:name_start+2] = 'é'.encode('utf-8')
            fds = []
            for value in (data, bad, nonprintable, unsupported):
                fd = _new_memfd(); fds.append(fd); _write_all(fd, value); fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
            self.addCleanup(lambda: [os.close(fd) for fd in fds])
            n = self.native_fixture.native(); n.passed_fds = (*prepared.pinned.pass_fds(), *fds); n.start()
            for fd, expected in zip(fds, (1, 0, 0, 0)):
                command = f'envelope {fd} 2 {prepared.default_map_bytes} 0 {1024*1024*1024}'
                n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
                result = n.marker('TEST ENVELOPE ').split(); n.marker('TEST COMMAND '+command)
                self.assertEqual(int(result[-2]), expected)

    def test_manifest_digest_binds_sealed_bytes(self):
        import hashlib
        with self.catalog.prepare(('one',)) as prepared:
            data=manifest.encode_startup_manifest(prepared);expected=hashlib.sha256(data).hexdigest()
            fds=[]
            for seal in (True, False):
                fd=_new_memfd();fds.append(fd);_write_all(fd,data)
                if seal: fcntl.fcntl(fd,fcntl.F_ADD_SEALS,_SEALS)
            self.addCleanup(lambda: [os.close(fd) for fd in fds])
            n=self.native_fixture.native();n.passed_fds=(*prepared.pinned.pass_fds(),*fds);n.start()
            for fd,digest,valid in ((fds[0],expected,1),(fds[0],'0'*64,0),
                                     (fds[0],'G'*64,0),(fds[1],expected,0),(-1,expected,0)):
                command=f'manifesthash {fd} {digest}';n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
                value=n.marker('TEST MANIFESTHASH ').split();n.marker('TEST COMMAND '+command)
                self.assertEqual(int(value[-1]),valid)
            self.assertIsNone(n.host_state()['lobby'])

    def test_manual_configuration_is_accepted_without_slot_override(self):
        self.catalog.setup_manual('map_loadingame')
        with self.catalog.prepare(('manual', 'manual')) as prepared:
            bundle = manifest.seal_startup_manifest(prepared)
            self.addCleanup(bundle.close)
            n = self.native_fixture.native(); n.passed_fds = bundle.pass_fds(); n.start()
            command = f'envelope {bundle.manifest_fd} 2 {prepared.default_map_bytes} 0 {1024*1024*1024}'
            n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
            result = n.marker('TEST ENVELOPE ').split()
            n.marker('TEST COMMAND '+command)
            self.assertEqual(result[-2], '1')
            self.assertIsNone(n.host_state()['lobby'])

if __name__ == '__main__': unittest.main()
