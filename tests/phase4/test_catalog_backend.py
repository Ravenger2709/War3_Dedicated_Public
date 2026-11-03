"""Offline native startup plumbing, with real sealed FDs and local subprocesses."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config
from src.runtime.controller import ICCupSupervisor, RuntimeBackend
from src.runtime.native import prepare_native
from src.runtime.preload_inputs import MapInput, pin_preload_inputs, _SEALS
from src.runtime.preload_manifest import StartupBundle, _ENTRY, _HEADER, _MAGIC, _SETTINGS, _accounted


class CatalogBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'ghost.cfg'
        self.source.write_text('bnet_server =\nbot_defaultmap = selected.cfg\n')
        self.map = self.root / 'map.w3x'; self.map.write_bytes(b'synthetic map bytes')
        self.ipc = self.root / 'ipc'; self.ipc.mkdir(mode=0o700)
        self.journal = self.root / 'journal'; self.journal.mkdir(mode=0o700)
        self.cfg = {'runtime_dir': str(self.root/'runtime'),
                    'hostbot': {'cwd': str(self.root), 'stop_timeout': 1,
                                'command': ['/opt/war3/bin/ghost++', str(self.root/'native/ghost.cfg')]},
                    'gameplay': gameplay_config({}),
                    'iccup': {'native_config': str(self.source), 'bridge_dir': str(self.root/'bridge')}}
        self.spec = {'cycle': ['one'], 'default_map_bytes': self.map.stat().st_size,
                     'ipc_directory': str(self.ipc), 'journal_directory': str(self.journal)}
        self.backend = RuntimeBackend(self.cfg, catalog_startup=self.spec)
        self.backend._username = 'Synthetic'
        self.backend.effective = prepare_native(self.cfg, 'Synthetic')
        self.bundles = []

    def make_bundle(self, cycle, source, gameplay, *, default_map_bytes, ipc_directory, journal_directory):
        self.assertEqual((cycle, source, default_map_bytes), (['one'], str(self.source), self.map.stat().st_size))
        pinned = pin_preload_inputs([MapInput('one', self.map, hashlib.sha256(self.map.read_bytes()).hexdigest())],
                                   default_map_bytes=default_map_bytes, max_games=2)
        map_entry = pinned.maps[0]
        cfg = b'map_path = Maps\\one.w3x\nmap_localpath = one.w3x\n'
        settings = (12, 0, 2, 5, 0, 50, 2, 30, 0, 0, 10, 0, 0, 0, 2)
        body = _ENTRY.pack(map_entry.fd, map_entry.size, bytes.fromhex(map_entry.sha256), 3, 3, len(cfg))
        body += _SETTINGS.pack(*settings) + b'one' + b'One' + cfg
        data = _HEADER.pack(_MAGIC, 1, 1, 2, 0, default_map_bytes,
                            _accounted([map_entry.size], default_map_bytes, 2)) + body + bytes([0])
        fd = os.memfd_create('test-manifest', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.write(fd, data); fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
        bundle = StartupBundle(pinned, fd, os.fstat(fd), hashlib.sha256(data).hexdigest())
        pinned._closed = True
        self.bundles.append(bundle); self.addCleanup(bundle.close)
        return bundle, {'bot_preload_manifest_fd': str(fd), 'bot_preload_manifest_sha256': bundle.manifest_sha256,
                        'bot_preload_ipc_dir': ipc_directory, 'bot_lifecycle_event_dir': journal_directory,
                        'bot_start_reservation_gate': '1', 'bot_maxgames': '2', 'autohost_maxgames': '2'}

    def factory(self):
        return patch('src.runtime.catalog_startup.prepare_catalog_startup', side_effect=self.make_bundle)

    def supervisor(self):
        s = object.__new__(ICCupSupervisor); s.backend = self.backend; s.config = self.cfg
        s.child_generations = {name: 0 for name in ('native', 'relay', 'star')}
        s.children = {}; s.component_exits = {}; s.last_exit_code = None
        s.guard = threading.RLock(); s.running = True; s.events = queue.Queue()
        s.log = lambda *args, **kwargs: None
        return s

    def test_generation_config_and_only_explicit_native_fds(self):
        legacy = self.backend.effective.read_text()
        with self.factory(): bundle = self.backend.native_preload(1)
        fds = bundle.pass_fds()
        command = self.backend.command('native', 1)
        cfg = Path(command[1]); text = cfg.read_text()
        self.assertEqual(cfg.name, 'ghost-catalog-1.cfg')
        self.assertIn('bot_preload_manifest_fd = '+str(bundle.manifest_fd)+'\n', text)
        self.assertIn('bot_preload_manifest_sha256 = '+bundle.manifest_sha256+'\n', text)
        self.assertIn('bot_maxgames = 2\n', text)
        self.assertIn('bot_start_reservation_gate = 1\n', text)
        self.assertEqual(self.backend.effective.read_text(), legacy)
        code = '''import json,os,sys\nfrom src.runtime.preload_manifest import parse_startup_manifest\nf=int(sys.argv[1]); st=os.fstat(f); p=parse_startup_manifest(os.pread(f,st.st_size,0)); print(json.dumps([os.fstat(e['fd']).st_size for e in p['entries']]))'''
        run = subprocess.run([sys.executable, '-c', code, str(bundle.manifest_fd)],
                             cwd=Path(__file__).resolve().parents[2], pass_fds=fds, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), [self.map.stat().st_size])
        probe = 'import os,sys\ntry: os.fstat(int(sys.argv[1])); sys.exit(1)\nexcept OSError: sys.exit(0)'
        for name in ('relay', 'star'):
            self.assertEqual(subprocess.run([sys.executable, '-c', probe, str(bundle.manifest_fd)], close_fds=True).returncode, 0, name)
        with self.assertRaises(ValueError): self.backend.native_preload(2)
        bundle.close()
        with self.assertRaises(ValueError): self.backend.command('native', 1)
        with self.factory(): new = self.backend.native_preload(2)
        self.assertNotEqual(bundle._manifest_identity, new._manifest_identity)
        self.assertEqual(Path(self.backend.command('native', 2)[1]).name, 'ghost-catalog-2.cfg')
        with self.assertRaises(ValueError): self.backend.command('native', 1)

    def test_refusal_closes_bundle_and_never_returns_stale_command(self):
        with self.factory(), patch('src.runtime.controller.prepare_native', side_effect=OSError('write failed')):
            with self.assertRaises(OSError): self.backend.native_preload(1)
        with self.assertRaises(ValueError): self.bundles[-1].pass_fds()
        with self.assertRaises(ValueError): self.backend.command('native', 1)
        service = self.supervisor(); service.child_generations['native'] = 1
        with self.factory(), patch('src.runtime.controller.subprocess.Popen', side_effect=OSError('spawn failed')):
            with self.assertRaises(OSError): service._spawn('native')
        with self.assertRaises(ValueError): self.bundles[-1].pass_fds()

    def test_supervisor_handoff_and_restart_releases_fds(self):
        service = self.supervisor()
        real_popen = subprocess.Popen
        script = ("import os,sys,time; f=int(sys.argv[1]); "
                  "print(os.fstat(f).st_size,flush=True); time.sleep(30)")
        def spawn(command, **options):
            cfg = Path(command[-1]).read_text()
            line = next(line for line in cfg.splitlines() if line.startswith('bot_preload_manifest_fd = '))
            manifest_fd = int(line.split(' = ')[1])
            self.assertIn(manifest_fd, options['pass_fds'])
            return real_popen([sys.executable, '-c', script, str(manifest_fd)], **options)
        try:
            with self.factory(), patch('src.runtime.controller.subprocess.Popen', side_effect=spawn):
                first = service._spawn('native')
                fds = first.preload.pass_fds()
                event = service.events.get(timeout=5)
                self.assertEqual(event[:3], ('component_line', 'native', first.generation))
                self.assertGreater(int(event[3]), 0)
                service._stop_component('native')
                for fd in fds:
                    with self.assertRaises(OSError): os.fstat(fd)
                second = service._spawn('native')
                self.assertNotEqual(first.preload._manifest_identity, second.preload._manifest_identity)
                event = service.events.get(timeout=5)
                self.assertEqual(event[:3], ('component_line', 'native', second.generation))
                new_fds = second.preload.pass_fds()
                service._stop_component('native')
                for fd in new_fds:
                    with self.assertRaises(OSError): os.fstat(fd)
        finally:
            for name in list(service.children): service._stop_component(name)
            service.running = False

    def test_invalid_override_and_legacy(self):
        before = self.backend.effective.read_bytes()
        for overrides in ({'bot_maxgames': '2'}, {'bot_preload_manifest_fd': '3\nbnet_server=evil'}):
            with self.assertRaises(ValueError):
                prepare_native(self.cfg, 'Synthetic', catalog_overrides=overrides, catalog_generation=1)
        with self.factory(): bundle = self.backend.native_preload(1)
        valid = {'bot_preload_manifest_fd': str(bundle.manifest_fd),
                 'bot_preload_manifest_sha256': bundle.manifest_sha256,
                 'bot_preload_ipc_dir': str(self.ipc), 'bot_lifecycle_event_dir': str(self.journal),
                 'bot_start_reservation_gate': '1', 'bot_maxgames': '2', 'autohost_maxgames': '2'}
        for change in ({'bot_preload_ipc_dir': str(self.ipc)+'\nbnet_server = injected'},
                       {'bot_preload_manifest_sha256': 'F'*64}, {'bot_start_reservation_gate': '0'},
                       {'autohost_maxgames': '3'}, {'bot_preload_manifest_fd': '0'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare_native(self.cfg, 'Synthetic', catalog_overrides={**valid, **change}, catalog_generation=2)
        bundle.close()
        self.assertEqual(self.backend.effective.read_bytes(), before)
        legacy = RuntimeBackend(self.cfg)
        self.assertIsNone(legacy.native_preload(1))
        self.assertEqual(legacy.command('native', 1), self.cfg['hostbot']['command'])
        self.assertEqual(prepare_native(self.cfg, 'Synthetic').read_bytes(), before)

    def test_claimed_catalog_launch_uses_new_private_ipc_and_sealed_fds(self):
        from src.runtime.production_cycle import ProductionCycleControl
        from src.runtime.session_lease import SessionLease
        root=self.root/'managed';lease=SessionLease.create(root,enabled=True);lease.close()
        control=ProductionCycleControl(root,['one'])
        try:
            control.claim_native_launch()
            self.backend.configure_cycle_launch(control)
            ipc=root/'ipc-journal'
            self.assertEqual(self.backend.catalog_startup['ipc_directory'],str(ipc))
            self.assertEqual(self.backend.catalog_startup['journal_directory'],str(control.lease.journal))
            self.assertEqual(ipc.stat().st_mode & 0o777,0o700)
            with self.factory(): bundle=self.backend.native_preload(1)
            descriptors=bundle.pass_fds()
            self.assertIn('bot_preload_ipc_dir = '+str(ipc),Path(self.backend.command('native',1)[1]).read_text())
            bundle.close()
            for fd in descriptors:
                with self.assertRaises(OSError):os.fstat(fd)
        finally:control.close()

    def test_cycle_spawn_gate_remains_closed(self):
        s = self.supervisor(); s.cycle_session = {'root': str(self.root), 'cycle': ['one']}
        self.assertFalse(s._cycle_permits_native_spawn())

if __name__ == '__main__': unittest.main()
