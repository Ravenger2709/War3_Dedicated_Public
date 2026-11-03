"""Offline startup preparation; this never enables production rotation."""
import importlib.util
import os
from pathlib import Path
import unittest

from src.runtime.catalog_startup import prepare_catalog_startup
from src.runtime.preload_manifest import parse_startup_manifest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('startup_catalog_fixture', ROOT / 'tests/phase4/test_preload_catalog.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(), 'offline StormLib fixture required')
class StartupTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixture.PreparedCatalogTests('runTest')
        self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)
        self.ipc = self.catalog.root / 'ipc'
        self.journal = self.catalog.root / 'journal'
        self.ipc.mkdir(mode=0o700)
        self.journal.mkdir(mode=0o700)

    def prepare(self, cycle, game=None):
        c = self.catalog
        return prepare_catalog_startup(cycle, c.builder.source, game or c.game,
            default_map_bytes=c.a.stat().st_size, ipc_directory=self.ipc,
            journal_directory=self.journal, library=fixture.fixture.STORM)

    def test_two_maps_repeats_overrides_and_ownership(self):
        bundle, cfg = self.prepare(['one', 'two', 'one'])
        fds = bundle.pass_fds()
        try:
            raw = os.pread(bundle.manifest_fd, os.fstat(bundle.manifest_fd).st_size, 0)
            parsed = parse_startup_manifest(raw)
            self.assertEqual(parsed['cycle_indices'], (0, 1, 0))
            self.assertEqual([e['name'] for e in parsed['entries']], ['Named A', 'Named B'])
            self.assertEqual(len(fds), 3)
            self.assertEqual(cfg['bot_maxgames'], '2')
            self.assertEqual(cfg['autohost_maxgames'], '2')
            self.assertEqual(cfg['bot_start_reservation_gate'], '1')
            self.assertEqual(cfg['bot_preload_manifest_sha256'], bundle.manifest_sha256)
            self.assertEqual(cfg['bot_preload_manifest_fd'], str(bundle.manifest_fd))
            self.assertEqual(cfg['bot_preload_ipc_dir'], str(self.ipc))
        finally:
            bundle.close()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)

    def test_automatic_default_and_config_share_catalog_snapshot(self):
        from unittest.mock import patch
        from src.runtime.config import gameplay_config
        from src.runtime.native import prepare_native
        c=self.catalog
        game=gameplay_config({'map_cycle':['two','one'],'max_concurrent_matches':2,
                              'max_players':4,'autostart_players':4})
        bundle,overrides=prepare_catalog_startup(['two','one'],c.builder.source,game,
            default_map_bytes=None,ipc_directory=self.ipc,journal_directory=self.journal,
            library=fixture.fixture.STORM)
        try:
            self.assertEqual(bundle.default_entry.preset_id,'two')
            self.assertEqual(bundle.default_sha256,bundle.maps[bundle.cycle_indices[0]].sha256)
            self.assertEqual(overrides['bot_preload_default_sha256'],bundle.default_sha256)
            config={'runtime_dir':str(c.root/'state'/'runtime'),
                    'hostbot':{'cwd':str(c.root/'host')},
                    'iccup':{'native_config':str(c.builder.source),'bridge_dir':str(c.root/'bridge')},
                    'gameplay':game}
            (c.root/'host').mkdir()
            # The resolved default stays from the sealed snapshot even if the
            # original preset document changes before writing native config.
            c.preset_path.write_text('{}')
            with patch('src.runtime.native.resolve_preset',side_effect=AssertionError('second preset read')):
                effective=prepare_native(config,'Synthetic',catalog_overrides=overrides,
                                         catalog_generation=1,catalog_default=bundle.default_entry)
            values=effective.read_text()
            self.assertIn('autohost_gamename = Named B\n',values)
            self.assertIn('bot_preload_default_sha256 = '+bundle.default_sha256+'\n',values)
            self.assertIn('map_localpath = b.w3x',
                          (c.root/'state'/'native'/'mapcfg'/'preset.cfg').read_text())
        finally:
            bundle.close()

    def test_single_and_repeated_id_real_backend_and_session_honor_limit_three(self):
        import io
        import json
        from unittest.mock import patch
        import src.runtime.catalog_startup as startup
        from src.runtime.controller import ICCupSupervisor, RuntimeBackend
        from src.supervisor.config import load_config
        root=self.catalog.root
        profile=root/'profile';profile.mkdir(mode=0o700)
        gateway=json.dumps('@ByteArray('+json.dumps({'Synthetic':'127.0.0.1:16112'})+')')
        ini=profile/'launcher.ini'
        ini.write_text('[General]\nUsername="Synthetic"\nConnections=\nCurrConnections=0\nSiteConnections='+gateway+'\nUserConnections='+gateway+'\n')
        ini.chmod(0o600)
        for i,cycle in enumerate((['one'],['one','one'])):
            state=root/f'state{i}';state.mkdir(mode=0o700)
            host=root/f'host{i}';host.mkdir(mode=0o700)
            raw={'mode':'iccup','runtime_dir':str(state/'runtime'),
                 'hostbot':{'cwd':str(host),'command':['/opt/war3/bin/ghost++',str(state/'native'/'ghost.cfg')]},
                 'iccup':{'profile':str(profile),'prefix':str(root/f'prefix{i}'),
                          'session':str(root/f'session{i}'),'bridge_dir':str(root/f'bridge{i}'),
                          'native_config':str(self.catalog.builder.source)},
                 'gameplay':{'map_cycle':cycle,'max_concurrent_matches':3,
                             'max_players':4,'autostart_players':4}}
            path=root/'supervisor.json';path.write_text(json.dumps(raw))
            config=load_config(path)
            service=ICCupSupervisor(config,output=io.StringIO())
            try:
                self.assertIs(type(service.backend),RuntimeBackend)
                self.assertEqual(service.cycle_session['cycle'],cycle)
                service._cycle_initialize()
                self.assertTrue(service._cycle_permits_native_spawn())
                control=service.cycle_control;control.claim_native_launch()
                backend=service.backend;backend.configure_cycle_launch(control)
                control.pin_launch_ipc(backend.catalog_startup['ipc_directory'])
                actual=startup.prepare_catalog_startup
                def prepare(*args,**kwargs):return actual(*args,**kwargs,library=fixture.fixture.STORM)
                with patch.object(startup,'prepare_catalog_startup',side_effect=prepare):
                    bundle=backend.native_preload(1)
                try:
                    raw=os.pread(bundle.manifest_fd,os.fstat(bundle.manifest_fd).st_size,0)
                    manifest=parse_startup_manifest(raw)
                    self.assertEqual(manifest['max_games'],3)
                    self.assertEqual(manifest['cycle_indices'],tuple(0 for _ in cycle))
                    self.assertEqual(len(bundle.maps),1)
                    config_text=Path(backend.command('native',1)[1]).read_text()
                    self.assertIn('bot_maxgames = 3\n',config_text)
                    self.assertIn('bot_preload_default_sha256 = '+bundle.maps[0].sha256+'\n',config_text)
                    self.assertFalse(service._cycle_permits_native_spawn())
                finally:bundle.close()
            finally:
                if service.cycle_control:service.cycle_control.close()
                service.backend.close();service.running=False;service.lockfile.close()

    def test_entire_cycle_and_capacity_rejected_without_fd_leak(self):
        before = set(os.listdir('/proc/self/fd'))
        for cycle, game in [(['one', 'unknown'], self.catalog.game),
                            (['one', 'two'], {**self.catalog.game, 'max_concurrent_matches': 6})]:
            with self.subTest(cycle=cycle, game=game), self.assertRaises(ValueError):
                self.prepare(cycle, game)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_private_distinct_paths_required(self):
        self.ipc.chmod(0o755)
        with self.assertRaises(ValueError): self.prepare(['one'])
        self.ipc.chmod(0o700)
        with self.assertRaises(ValueError):
            prepare_catalog_startup(['one'], self.catalog.builder.source, self.catalog.game,
                default_map_bytes=self.catalog.a.stat().st_size, ipc_directory=self.ipc,
                journal_directory=self.ipc, library=fixture.fixture.STORM)

if __name__ == '__main__': unittest.main()
