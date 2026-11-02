#!/usr/bin/env python3
"""Offline CMap sealed-FD adapter tests; no production preload or IPC."""
import hashlib
import importlib.util
import os
import shutil
import struct
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.runtime.preload_inputs import MapInput, pin_preload_inputs

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

base = load('bounds_test', ROOT / 'tests/phase3/native-map-bounds-test.py')

class SealedMapTests(unittest.TestCase):
    setUp = base.MapBoundsTests.setUp
    tearDown = base.MapBoundsTests.tearDown
    native = base.MapBoundsTests.native
    builder = base.MapBoundsTests.builder
    cfg_for = base.MapBoundsTests.cfg_for

    def mapfd(self, n, fd, cfg):
        command = f'mapfd {fd} {cfg}'
        n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
        value = n.marker('TEST MAPFD ').split()
        n.marker('TEST COMMAND ' + command)
        return int(value[-2]), int(value[-1])

    def test_native_sha256_binds_sealed_fd_and_rejects_wrong_digest(self):
        n = self.native()
        builder = self.builder()
        path = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                    'war3map.j': base.fixture.PLAIN_SCRIPT}, name='hash.w3x')
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with pin_preload_inputs([MapInput('one', path, expected)],
                                default_map_bytes=path.stat().st_size, max_games=2) as catalog:
            entry = catalog.maps[0]
            n.passed_fds = catalog.pass_fds(); n.start()
            cfg = self.cfg_for(n, path)
            def checked(digest):
                command = f'mapfdhash {entry.fd} {digest} {cfg}'
                n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
                result = n.marker('TEST MAPFDHASH ').split()
                n.marker('TEST COMMAND ' + command)
                return int(result[-2]), int(result[-1])
            self.assertEqual(checked(expected), (1, entry.size))
            self.assertEqual(checked('0' * 64), (0, 0))
            self.assertEqual(checked('f' * 63), (0, 0))
            self.assertEqual(checked('G' * 64), (0, 0))
            self.assertIsNone(n.host_state()['lobby'])

    def test_two_sealed_maps_loaded_without_original_paths(self):
        n = self.native()
        builder = self.builder()
        a = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                 'war3map.j': base.fixture.PLAIN_SCRIPT}, name='a.w3x')
        b = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                 'war3map.j': base.fixture.PLAIN_SCRIPT,
                                 'war3map.w3u': b'other'}, name='b.w3x')
        inputs = [MapInput('a', a, hashlib.sha256(a.read_bytes()).hexdigest()),
                  MapInput('b', b, hashlib.sha256(b.read_bytes()).hexdigest())]
        with pin_preload_inputs(inputs, default_map_bytes=a.stat().st_size, max_games=2) as catalog:
            fds = catalog.pass_fds()
            n.passed_fds = fds
            n.start()
            for entry, original in zip(catalog.maps, (a, b)):
                # Metadata references a path which does not exist in GHost's map dir.
                cfg = self.cfg_for(n, original)
                os.lseek(entry.fd, 0, os.SEEK_END)  # Child must not rely on shared FD offset.
                original.unlink()
                self.assertEqual(self.mapfd(n, entry.fd, cfg), (1, entry.size))
            self.assertIsNone(n.host_state()['lobby'])

    def test_two_real_native_matches_with_two_sealed_maps(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '4', 'autohost_gamename': 'Initial',
                         'autohost_owner': 'Owner'})
        builder = self.builder()
        a = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                 'war3map.j': base.fixture.PLAIN_SCRIPT}, name='a.w3x')
        b = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                 'war3map.j': base.fixture.PLAIN_SCRIPT,
                                 'war3map.w3u': b'other'}, name='b.w3x')
        specs = [MapInput('a', a, hashlib.sha256(a.read_bytes()).hexdigest()),
                 MapInput('b', b, hashlib.sha256(b.read_bytes()).hexdigest())]
        with pin_preload_inputs(specs, default_map_bytes=a.stat().st_size, max_games=2) as catalog:
            n.passed_fds = catalog.pass_fds(); n.start()
            configs = []
            for index, path in enumerate((a, b)):
                raw = self.cfg_for(n, path).read_text().replace('Synthetic.w3x', f'Map{index}.w3x')
                cfg = n.root / f'map-{index}.cfg'; cfg.write_text(raw); configs.append(cfg)
            def stage(fd, cfg):
                command = f'presetfd {fd} {cfg}'
                n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
                result = n.marker('TEST PRESETFD ')
                n.marker('TEST COMMAND ' + command)
                self.assertEqual(result, 'TEST PRESETFD 1')
            stage(catalog.maps[0].fd, configs[0]); n.command('autohost')
            base.wait(lambda: n.host_state()['lobby'] is not None)
            first_lobby = n.host_state()['lobby']
            self.assertEqual(first_lobby['map_name'], 'Map0.w3x')
            first = n.peer('First', ready=False, game_keepalive=True)
            first.sock.sendall(base.gameplay.frame(0x42, b'\0' * 4 + b'\x01' + struct.pack('<I', catalog.maps[0].size)))
            n.command('ping')
            n.command('startlobby'); base.wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(base.gameplay.frame(0x23))
            try:
                base.wait(lambda: any(p[1] == 0x0c for p in first.frames))
            except AssertionError:
                raise AssertionError((n.host_state(), [p[1] for p in first.frames], n.lines[-40:])) from None
            stage(catalog.maps[1].fd, configs[1]); n.command('autohost')
            base.wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(n.host_state()['lobby']['map_name'], 'Map1.w3x')
            second = n.peer('Second', ready=False, game_keepalive=True)
            second.sock.sendall(base.gameplay.frame(0x42, b'\0' * 4 + b'\x01' + struct.pack('<I', catalog.maps[1].size)))
            n.command('ping')
            n.command('startlobby'); base.wait(lambda: n.host_state()['active_games'] == 2)
            second.sock.sendall(base.gameplay.frame(0x23))
            count = sum(p[1] == 0x0c for p in first.frames)
            base.wait(lambda: sum(p[1] == 0x0c for p in first.frames) > count)
            self.assertFalse(first.disconnected)
            self.assertNotEqual(first_lobby['host_counter'], n.host_state()['games'][1]['host_counter'])

    def test_optional_two_local_maps_via_sealed_fd(self):
        paths = [Path(p) for p in os.environ.get('WAR3_KNOWN_MAPS', '').split(':') if p]
        if len(paths) < 2:
            self.skipTest('two local maps not supplied')
        n = self.native()
        specs = [MapInput('local%d' % i, path, hashlib.sha256(path.read_bytes()).hexdigest())
                 for i, path in enumerate(paths[:2])]
        with pin_preload_inputs(specs, default_map_bytes=paths[0].stat().st_size, max_games=2) as catalog:
            n.passed_fds = catalog.pass_fds()
            n.start()
            for entry, path in zip(catalog.maps, paths):
                self.assertFalse((n.root / path.name).exists())
                cfg = self.cfg_for(n, path)
                self.assertEqual(self.mapfd(n, entry.fd, cfg), (1, entry.size))
                command = f'mapfdhash {entry.fd} {entry.sha256} {cfg}'
                n.proc.stdin.write(command + '\n'); n.proc.stdin.flush()
                result = n.marker('TEST MAPFDHASH ').split()
                n.marker('TEST COMMAND ' + command)
                self.assertEqual((int(result[-2]), int(result[-1])), (1, entry.size))
            self.assertIsNone(n.host_state()['lobby'])

    def test_unsealed_regular_and_invalid_fds_fail_closed(self):
        n = self.native()
        builder = self.builder()
        path = builder.mpq(members={'war3map.w3i': base.fixture.info_fixture(),
                                    'war3map.j': base.fixture.PLAIN_SCRIPT}, name='a.w3x')
        fd = os.open(path, os.O_RDONLY)
        try:
            n.passed_fds = (fd,)
            n.start()
            target = n.root / path.name; shutil.copyfile(path, target)
            cfg = self.cfg_for(n, target)
            self.assertEqual(self.mapfd(n, fd, cfg), (0, 0))
            self.assertEqual(self.mapfd(n, -1, cfg), (0, 0))
            self.assertIsNone(n.host_state()['lobby'])
        finally:
            os.close(fd)

if __name__ == '__main__': unittest.main()
