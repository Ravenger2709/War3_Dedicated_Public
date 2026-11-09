#!/usr/bin/env python3
"""Offline direct native catalog construction; startup wiring and IPC pending."""
import importlib.util
import json
import fcntl
import os
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.runtime.preload_manifest import seal_startup_manifest, encode_startup_manifest, _HEADER, _ENTRY, _SETTINGS
from src.runtime.preload_inputs import _new_memfd, _SEALS, _write_all

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
fixtures = load('startup_catalog_fixture', ROOT / 'tests/phase4/test_preload_catalog.py')
gameplay = load('startup_native_fixture', ROOT / 'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(), 'offline StormLib required')
class StartupCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixtures.PreparedCatalogTests('runTest'); self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)
        self.native_fixture = gameplay.GameplayTests('runTest'); self.native_fixture.setUp()
        self.addCleanup(self.native_fixture.tearDown)
        self.addCleanup(self.native_fixture.doCleanups)

    def test_two_maps_are_loaded_once_before_any_lobby(self):
        with self.catalog.prepare() as prepared:
            bundle = seal_startup_manifest(prepared); self.addCleanup(bundle.close)
            n = self.native_fixture.native({'bot_maxgames':'2'})
            shutil.copyfile(self.catalog.a, n.root / 'a.w3x')
            (n.root / 'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
            for script in ('common.j', 'blizzard.j'):
                (n.root / script).write_bytes(b'globals\nendglobals\n')
            n.passed_fds = bundle.pass_fds(); n.start()
            self.catalog.a.unlink(); self.catalog.b.unlink()
            command = f'catalog {bundle.manifest_fd}'
            n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
            value = n.marker('TEST CATALOG ').split(); n.marker('TEST COMMAND '+command)
            if Path('/sys/fs/cgroup').stat().st_ino == 1:
                self.assertEqual(value[-7:], ['2','3','0','1','0',
                    'one:Maps\\Download\\a.w3x','two:Maps\\Download\\b.w3x'])
            else:
                self.assertEqual(value[-1], '0')
            self.assertIsNone(n.host_state()['lobby'])

    def test_sealed_catalog_accepts_total_ai_target_with_fixed_computers(self):
        if Path('/sys/fs/cgroup').stat().st_ino != 1:
            self.skipTest('full cgroup hierarchy required')
        for fixed in (set(range(8, 12)), {3, 7, 11}, set()):
            with self.subTest(fixed=len(fixed)):
                self.catalog.a.unlink()
                self.catalog.a = self.catalog.builder.mpq(script=fixtures.fixture.AI_SCRIPT,
                    info=fixtures.fixture.info_fixture(statuses=[2 if sid in fixed else 1 for sid in range(12)]),
                    name='a.w3x')
                entry=self.catalog.doc['presets'][0]
                entry.update(max_players=12, start={'autostart_enabled':False,'autostart_players':4},
                             ai={'enabled':True,'target':11,'skill':1})
                self.catalog.doc.pop('capabilities',None)
                self.catalog.preset_path.write_text(json.dumps(self.catalog.doc))
                with self.catalog.prepare(('one',)) as prepared:
                    bundle=seal_startup_manifest(prepared);self.addCleanup(bundle.close)
                    n=self.native_fixture.native({'bot_maxgames':'2'},slots=12)
                    try:
                        shutil.copyfile(self.catalog.a,n.root/'a.w3x')
                        (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
                        for script in ('common.j','blizzard.j'):
                            (n.root/script).write_bytes(b'globals\nendglobals\n')
                        n.passed_fds=bundle.pass_fds();n.start()
                        command=f'catalog {bundle.manifest_fd}'
                        n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
                        result=n.marker('TEST CATALOG ').split();n.marker('TEST COMMAND '+command)
                        self.assertEqual(result[2:4],['1','1'])
                        self.assertIsNone(n.host_state()['lobby'])
                    finally:n.stop()

    def test_bad_second_map_capacity_aborts_catalog_without_pending(self):
        with self.catalog.prepare() as prepared:
            data = bytearray(encode_startup_manifest(prepared))
            first = _HEADER.size
            entry = _ENTRY.unpack_from(data, first)
            second = first + _ENTRY.size + _SETTINGS.size + entry[3] + entry[4] + entry[5]
            data[second+_ENTRY.size:second+_ENTRY.size+4] = (12).to_bytes(4, 'little')
            bad = _new_memfd(); self.addCleanup(os.close, bad)
            _write_all(bad, data); fcntl.fcntl(bad, fcntl.F_ADD_SEALS, _SEALS)
            bundle = seal_startup_manifest(prepared); self.addCleanup(bundle.close)
            n = self.native_fixture.native({'bot_maxgames':'2'})
            shutil.copyfile(self.catalog.a, n.root / 'a.w3x')
            (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
            for script in ('common.j','blizzard.j'): (n.root/script).write_bytes(b'globals\nendglobals\n')
            n.passed_fds = (*bundle.pass_fds(), bad); n.start()
            def load(fd):
                command=f'catalog {fd}'; n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
                result=n.marker('TEST CATALOG ').split(); n.marker('TEST COMMAND '+command)
                return result[2:]
            before = len(os.listdir(f'/proc/{n.proc.pid}/fd'))
            self.assertEqual(load(bad), ['0'])
            self.assertEqual(len(os.listdir(f'/proc/{n.proc.pid}/fd')), before)
            self.assertIsNone(n.host_state()['lobby'])
            if Path('/sys/fs/cgroup').stat().st_ino == 1:
                self.assertEqual(load(bundle.manifest_fd)[0], '2')
            self.assertIsNone(n.host_state()['lobby'])

    def test_native_actual_concurrency_and_default_size_mismatch_fail_closed(self):
        with self.catalog.prepare() as prepared:
            bundle=seal_startup_manifest(prepared); self.addCleanup(bundle.close)
            self.assertNotEqual(self.catalog.a.stat().st_size, self.catalog.b.stat().st_size)
            for maximum, default in ((1,self.catalog.a),(2,self.catalog.b)):
                with self.subTest(maximum=maximum,default=default.name):
                    n=self.native_fixture.native({'bot_maxgames':str(maximum)})
                    shutil.copyfile(default,n.root/default.name)
                    (n.root/'synthetic.cfg').write_text(f'map_path = Maps\\Download\\{default.name}\nmap_localpath = {default.name}\n')
                    for script in ('common.j','blizzard.j'): (n.root/script).write_bytes(b'globals\nendglobals\n')
                    n.passed_fds=bundle.pass_fds();n.start()
                    command=f'catalog {bundle.manifest_fd}'
                    n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
                    result=n.marker('TEST CATALOG ').split();n.marker('TEST COMMAND '+command)
                    self.assertEqual(result[-1],'0')
                    self.assertIsNone(n.host_state()['lobby'])
                    n.stop()

    def test_insufficient_real_cgroup_headroom_rejects_catalog(self):
        if os.environ.get('WAR3_EXPECT_BUDGET_REJECT') != '1': self.skipTest('run with constrained memory cgroup')
        with self.catalog.prepare() as prepared:
            bundle=seal_startup_manifest(prepared); self.addCleanup(bundle.close)
            n=self.native_fixture.native({'bot_maxgames':'2'})
            shutil.copyfile(self.catalog.a,n.root/'a.w3x')
            (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
            for script in ('common.j','blizzard.j'): (n.root/script).write_bytes(b'globals\nendglobals\n')
            n.passed_fds=bundle.pass_fds();n.start()
            command=f'catalog {bundle.manifest_fd}'
            n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
            result=n.marker('TEST CATALOG ').split();n.marker('TEST COMMAND '+command)
            self.assertEqual(result[-1],'0')
            self.assertIsNone(n.host_state()['lobby'])

    def test_direct_stage_without_durable_claim_is_rejected(self):
        if Path('/sys/fs/cgroup').stat().st_ino != 1: self.skipTest('full cgroup hierarchy required')
        with self.catalog.prepare() as prepared:
            bundle=seal_startup_manifest(prepared); self.addCleanup(bundle.close)
            n=self.native_fixture.native({'bot_maxgames':'2'})
            shutil.copyfile(self.catalog.a,n.root/'a.w3x')
            (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
            for script in ('common.j','blizzard.j'): (n.root/script).write_bytes(b'globals\nendglobals\n')
            n.passed_fds=bundle.pass_fds(); n.start()
            command=f'cataloghold {bundle.manifest_fd}'; n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
            result=n.marker('TEST CATALOGHOLD ').split();n.marker('TEST COMMAND '+command)
            self.assertEqual(result[-1],'2')
            command='catalogstage '+'a'*32+' 0';n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
            result=n.marker('TEST CATALOGSTAGE ').split();n.marker('TEST COMMAND '+command)
            self.assertEqual(result[-1],'0')
            self.assertIsNone(n.host_state()['lobby'])

if __name__ == '__main__': unittest.main()
