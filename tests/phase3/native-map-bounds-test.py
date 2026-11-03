#!/usr/bin/env python3
"""Offline CMap bounds; synthetic MPQs and optional locally mounted known maps."""
import importlib.util
import ctypes as C
import os
from pathlib import Path
import shutil
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

gameplay = module('native_gameplay', ROOT / 'tests/phase3/native-test.py')
fixture = module('map_presets', ROOT / 'tests/phase3/test_map_presets.py')
wait = gameplay.wait

class MapBoundsTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown
    native = gameplay.GameplayTests.native

    def builder(self):
        obj = fixture.MPQTests('runTest')
        obj.setUp()
        self.addCleanup(obj.doCleanups)
        return obj

    def compressed_mpq(self, builder):
        path = builder.root / 'maps/compressed.w3x'
        lib = C.CDLL(fixture.STORM)
        create = getattr(lib, 'SFileCreateArchive', None)
        create_flags = 0 if create else 2
        if create is None:
            create = getattr(lib, 'SFileCreateArchiveEx', None) or getattr(lib, '_Z20SFileCreateArchiveExPKcjjPPv')
        create.argtypes = [C.c_char_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)]
        create.restype = C.c_bool
        add = getattr(lib, 'SFileAddFileEx', None) or getattr(lib, '_Z14SFileAddFileExPvPKcS1_jji')
        add.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint32, C.c_uint32, C.c_int]
        add.restype = C.c_bool
        archive = C.c_void_p()
        self.assertTrue(create(os.fsencode(path), create_flags, 32, C.byref(archive)))
        api = fixture.storm_api(fixture.STORM)
        try:
            members = {'war3map.w3i': fixture.info_fixture(), 'war3map.j': fixture.PLAIN_SCRIPT,
                       'war3map.w3u': b'x' * (16 * 1024 * 1024 + 1)}
            for index, (name, content) in enumerate(members.items()):
                source = builder.root / ('compressed-member-%d' % index)
                source.write_bytes(content)
                # MPQ_FILE_COMPRESS and MPQ_COMPRESSION_ZLIB.
                self.assertTrue(add(archive, os.fsencode(source), name.encode('ascii'), 0x200, 2, 2))
        finally:
            api['close'](archive)
        return path

    def apply(self, n, path):
        command = 'preset ' + str(path)
        n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
        value = n.marker('TEST PRESET ')
        n.marker('TEST COMMAND ' + command)
        return value.endswith(' 1')

    def cfg_for(self, n, map_path):
        # Exercise native CRC/SHA member reads, not only metadata preflight.
        for script in ('common.j', 'blizzard.j'):
            (n.root / script).write_bytes(b'globals\nendglobals\n')
        data = (n.root / 'synthetic.cfg').read_text().splitlines()
        size = ' '.join(map(str, struct.pack('<I', map_path.stat().st_size)))
        data = [('map_size = ' + size) if line.startswith('map_size = ') else line for line in data]
        path = n.root / 'next.cfg'
        path.write_text('\n'.join(data) + '\nmap_localpath = ' + map_path.name + '\ntest_max = 4\n')
        return path

    def test_preload_payload_budget_counts_worst_case_copies(self):
        n = self.native()
        try:
            n.start()
            mib = 1024 * 1024
            for games, default_size, sizes, expected_ok, expected_required in [
                (1, 2*mib, [3*mib, 4*mib], True, (128 + 3 + 4 + 2*2 + 4*4)*mib),
                (5, 2*mib, [3*mib, 4*mib], True, (128 + 3 + 4 + 2*2 + 8*4)*mib),
                (5, 128*mib, [128*mib], False, (128 + 128 + 2*128 + 8*128)*mib),
                (1, 32*mib, [64*mib], True, 512*mib),
                (1, 32*mib, [64*mib + 1], False, 512*mib + 5),
                (5, mib, [0], False, 0), (6, mib, [mib], False, 0),
                (5, mib, [129*mib], False, 0), (5, mib, [mib]*33, False, 0),
                (5, 0, [mib], False, 0),
            ]:
                with self.subTest(games=games, sizes=sizes):
                    command = 'budget ' + str(games) + ' ' + str(default_size) + ' ' + ' '.join(map(str, sizes))
                    n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
                    result = n.marker('TEST BUDGET ').split()
                    n.marker('TEST COMMAND ' + command)
                    self.assertEqual(result[-2:], [str(int(expected_ok)), str(expected_required)])
        finally:
            n.stop()

    def test_ordinary_and_oversized_member_fail_closed(self):
        n = self.native({'bot_maxgames': '2'})
        builder = self.builder()
        try:
            n.start().create()
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            wait(lambda: any(p[1] == 0x0c for p in first.frames))
            before = wait(lambda: snapshot if (snapshot := n.snapshot())['operation'] == 'unpublish' else None)
            good = builder.mpq(members={'war3map.w3i': fixture.info_fixture(),
                                        'war3map.j': fixture.PLAIN_SCRIPT}, name='ordinary.w3x')
            target = n.root / 'ordinary.w3x'; shutil.copyfile(good, target)
            self.assertTrue(self.apply(n, self.cfg_for(n, target)))
            boundary = builder.mpq(members={'war3map.w3i': fixture.info_fixture(),
                                            'war3map.j': fixture.PLAIN_SCRIPT,
                                            'war3map.w3u': b'x' * (16 * 1024 * 1024)}, name='boundary.w3x')
            target = n.root / 'boundary.w3x'; shutil.copyfile(boundary, target)
            self.assertTrue(self.apply(n, self.cfg_for(n, target)))
            bad = builder.mpq(members={'war3map.w3i': fixture.info_fixture(),
                                       'war3map.j': fixture.PLAIN_SCRIPT,
                                       'war3map.w3u': b'x' * (16 * 1024 * 1024 + 1)}, name='oversized.w3x')
            target = n.root / 'oversized.w3x'; shutil.copyfile(bad, target)
            self.assertFalse(self.apply(n, self.cfg_for(n, target)))
            self.assertEqual(n.host_state()['active_games'], 1)
            self.assertIsNone(n.host_state()['lobby'])
            self.assertEqual(n.snapshot()['operation'], 'unpublish')
            self.assertEqual(n.snapshot()['sequence'], before['sequence'])
            count = sum(p[1] == 0x0c for p in first.frames)
            wait(lambda: sum(p[1] == 0x0c for p in first.frames) > count)
            self.assertFalse(first.disconnected)
        finally:
            for peer in n.peers: peer.close()
            n.stop()

    def test_compressed_oversized_member_is_rejected(self):
        n = self.native()
        builder = self.builder()
        try:
            n.start()
            path = self.compressed_mpq(builder)
            self.assertLess(path.stat().st_size, 2 * 1024 * 1024)
            target = n.root / 'compressed.w3x'; shutil.copyfile(path, target)
            self.assertFalse(self.apply(n, self.cfg_for(n, target)))
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_aggregate_member_limit_without_oversized_single_member(self):
        n = self.native()
        builder = self.builder()
        try:
            n.start()
            payload = b'x' * (16 * 1024 * 1024)
            members = {'war3map.w3i': fixture.info_fixture(), 'war3map.j': fixture.PLAIN_SCRIPT}
            members.update({name: payload for name in ('war3map.w3u', 'war3map.w3b',
                                                       'war3map.w3d', 'war3map.w3a')})
            path = builder.mpq(members=members, name='aggregate.w3x')
            target = n.root / 'aggregate.w3x'; shutil.copyfile(path, target)
            self.assertFalse(self.apply(n, self.cfg_for(n, target)))
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_oversized_embedded_script_is_rejected_without_default_scripts(self):
        n = self.native()
        builder = self.builder()
        try:
            n.start()
            path = builder.mpq(members={'war3map.w3i': fixture.info_fixture(),
                                        'war3map.j': fixture.PLAIN_SCRIPT,
                                        'Scripts\\common.j': b'x' * (16 * 1024 * 1024 + 1)},
                               name='oversized-script.w3x')
            target = n.root / 'oversized-script.w3x'; shutil.copyfile(path, target)
            self.assertFalse(self.apply(n, self.cfg_for(n, target)))
        finally:
            n.stop()

    def test_required_members_missing_cannot_use_fallback_metadata(self):
        n = self.native()
        builder = self.builder()
        try:
            n.start()
            for index, members in enumerate(({'war3map.w3i': fixture.info_fixture()},
                                             {'war3map.j': fixture.PLAIN_SCRIPT})):
                path = builder.mpq(members=members, name='missing%d.w3x' % index)
                target = n.root / path.name; shutil.copyfile(path, target)
                self.assertFalse(self.apply(n, self.cfg_for(n, target)))
                self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_map_file_symlink_is_rejected(self):
        n = self.native()
        builder = self.builder()
        try:
            n.start()
            path = builder.mpq(members={'war3map.w3i': fixture.info_fixture(),
                                        'war3map.j': fixture.PLAIN_SCRIPT}, name='real.w3x')
            target = n.root / 'real.w3x'; shutil.copyfile(path, target)
            link = n.root / 'link.w3x'; link.symlink_to(target)
            self.assertFalse(self.apply(n, self.cfg_for(n, link)))
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_map_file_limit_is_checked_before_read(self):
        n = self.native()
        try:
            n.start()
            target = n.root / 'large.w3x'
            with target.open('wb') as stream:
                stream.truncate(128 * 1024 * 1024 + 1)
            self.assertFalse(self.apply(n, self.cfg_for(n, target)))
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            n.stop()

    def test_known_local_maps_when_supplied(self):
        paths = [Path(p) for p in os.environ.get('WAR3_KNOWN_MAPS', '').split(':') if p]
        if not paths:
            self.skipTest('optional local map paths not supplied')
        n = self.native()
        try:
            n.start()
            for index, path in enumerate(paths):
                with self.subTest(path=path.name):
                    target = n.root / ('known%d.w3x' % index)
                    shutil.copyfile(path, target)
                    self.assertTrue(self.apply(n, self.cfg_for(n, target)))
        finally:
            n.stop()

if __name__ == '__main__': unittest.main()
