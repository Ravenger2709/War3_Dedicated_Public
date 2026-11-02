#!/usr/bin/env python3
"""Prove canonical manual mapcfg settings reach real sealed CMap offline."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
fixtures = load('manual_catalog_fixture', ROOT/'tests/phase4/test_preload_catalog.py')
gameplay = load('manual_native_fixture', ROOT/'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(), 'offline StormLib required')
class ManualNativeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixtures.PreparedCatalogTests('runTest'); self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)
        self.native_fixture = gameplay.GameplayTests('runTest'); self.native_fixture.setUp()
        self.addCleanup(self.native_fixture.tearDown)
        self.addCleanup(self.native_fixture.doCleanups)

    def test_legacy_and_native_keys_reach_real_cmap(self):
        for key in ('map_loadInGame', 'map_loadingame'):
            with self.subTest(key=key):
                self.catalog.setup_manual(key)
                with self.catalog.prepare(('manual',)) as prepared:
                    n = self.native_fixture.native(); n.passed_fds = prepared.pinned.pass_fds(); n.start()
                    for script in ('common.j', 'blizzard.j'):
                        (n.root / script).write_bytes(b'globals\nendglobals\n')
                    cfg = n.root / 'manual.cfg'; cfg.write_text(prepared.entries[0].mapcfg)
                    entry = prepared.pinned.maps[0]
                    command = f'mapfdstate {entry.fd} {entry.sha256} {cfg}'
                    n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
                    value = n.marker('TEST MAPFDSTATE ').split()
                    n.marker('TEST COMMAND '+command)
                    self.assertEqual(value[-6:], ['1', '1', '2', '1', '3', '7'])
                    self.assertIsNone(n.host_state()['lobby'])
                    n.stop()

if __name__ == '__main__': unittest.main()
