"""Owned subprocess and sealed catalog glue, with production rotation guarded."""
import importlib.util
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime.preload_manifest import seal_startup_manifest
from src.runtime.session_lease import SessionLease

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result
fixture = load('glue_catalog_fixture', 'tests/phase4/test_preload_catalog.py')
session = load('glue_session_fixture', 'tests/phase4/test_controller_session.py')


@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(), 'offline StormLib fixture required')
class ProductionGlueTests(unittest.TestCase):
    def setUp(self):
        self.maps = fixture.PreparedCatalogTests('runTest'); self.maps.setUp()
        self.addCleanup(self.maps.doCleanups)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.session = self.root/'session'; self.ipc = self.session/'ipc-journal'
        lease = SessionLease.create(self.session, enabled=True); lease.close(); self.ipc.mkdir(mode=0o700)
        self.cfg = {'mode':'iccup', 'runtime_dir':str(self.root/'supervisor'),
                    'http':{'host':'127.0.0.1','port':0},
                    'hostbot':{'command':['/unused'],'cwd':str(self.root),'startup_timeout':2,'stop_timeout':.5,'lobby_timeout':1},
                    'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
                    'iccup':{**iccup_config({}),'bridge_dir':str(self.root/'bridge')},
                    'gameplay':gameplay_config({'map_cycle':['one','two','one'],'max_concurrent_matches':2})}
        self.service = None
        self.addCleanup(self.cleanup_service)

    def cleanup_service(self):
        s = self.service
        if s:
            s._stop_component('native')
            if s.cycle_control: s.cycle_control.close()
            s.running = False; s.backend.close(); s.lockfile.close()

    def start(self, *, cycle=None, max_games=2, tamper_map=False, reserve=False):
        backend = session.Backend(self.root, self.session/'journal', {})
        backend.catalog_startup = {'cycle': cycle or ['one','two','one'],
            'default_map_bytes': self.maps.a.stat().st_size,
            'ipc_directory':str(self.ipc), 'journal_directory':str(self.session/'journal')}
        def preload(generation):
            bundle=seal_startup_manifest(self.maps.prepare())
            if tamper_map:
                bundle.maps=(replace(bundle.maps[0],sha256='0'*64),*bundle.maps[1:])
            return bundle
        backend.native_preload = preload
        self.cfg['gameplay']['max_concurrent_matches'] = max_games
        s = ICCupSupervisor(self.cfg, output=io.StringIO(), backend=backend,
            cycle_session={'root':str(self.session),'cycle':['one','two','one']})
        self.service = s; s._cycle_initialize()
        if reserve:s.cycle_control.state.reserve_external('f'*32,7)
        self.assertFalse(s._cycle_permits_native_spawn())
        child = s._spawn('native')  # Explicit offline fixture; normal supervisor cannot spawn.
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            try: event=s.events.get(timeout=.2)
            except Exception: continue
            if event[0]=='component_line' and event[1]=='native':
                s._native_line(event[2],event[3],*event[4:])
                if event[3].startswith('[HOSTBOT_SESSION] '): return s,child
        self.fail('bounded session output timeout')

    def test_owned_catalog_attaches_and_guard_prevents_requests(self):
        s,child = self.start()
        self.assertFalse(s.cycle_failed)
        control=s.cycle_control
        self.assertIsNotNone(control.catalog); self.assertIsNotNone(control.driver)
        self.assertEqual(control.catalog.generation, 'a'*32)
        self.assertEqual(control.catalog.digest, child.preload.manifest_sha256)
        self.assertEqual(control.catalog.cycle, ['one','two','one'])
        s._cycle_tick()
        self.assertFalse(s.cycle_failed)
        self.assertIsNone(control.catalog.data['pending'])
        self.assertEqual(list(self.ipc.iterdir()), [])
        self.assertEqual(control.state.state['index'], 0)
        self.assertFalse(s._cycle_permits_native_spawn())
        self.assertEqual(list((self.session/'journal').glob('allow-*')), [])

    def test_pending_intent_is_not_dispatched_or_lost_on_verified_exit(self):
        s, child = self.start()
        control = s.cycle_control
        request = control.catalog.enqueue(control.state)
        s._cycle_tick()
        self.assertEqual(control.catalog.data['pending']['request_id'], request.request_id)
        self.assertEqual(list(self.ipc.iterdir()), [])
        old_journal = control.lease.journal
        s._stop_component('native')
        s.cycle_next_scan = 0
        s._cycle_tick()
        self.assertFalse(s.cycle_failed)
        self.assertEqual(s.cycle_status['state'], 'blocked_pending')
        self.assertEqual(control.lease.journal, old_journal)
        self.assertEqual(control.state.state['index'], 0)
        self.assertEqual(list(self.ipc.iterdir()), [])

    def test_duplicate_attach_and_other_bundle_rejected(self):
        s, child = self.start()
        with self.assertRaisesRegex(ValueError, 'OWNER_REJECTED'):
            s.cycle_control.attach_catalog(child, child.preload, self.ipc, 2)
        self.assertEqual(list(self.ipc.iterdir()), [])
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_mismatched_max_fails_closed_without_request(self):
        s,child = self.start(max_games=3)
        self.assertTrue(s.cycle_failed)
        self.assertIsNone(s.cycle_control.catalog)
        self.assertEqual(list(self.ipc.iterdir()), [])
        self.assertIsNone(child.process.poll())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_manifest_map_descriptor_metadata_mismatch_blocks_attach(self):
        s,child=self.start(tamper_map=True)
        self.assertTrue(s.cycle_failed)
        self.assertIsNone(s.cycle_control.catalog)
        self.assertFalse(list(self.session.glob('catalog-*.json')))
        self.assertEqual(list(self.ipc.iterdir()),[])
        self.assertTrue(child.session_owner.is_running())

    def test_nonempty_ipc_or_pending_cursor_is_not_reinitialized(self):
        (self.ipc/'unknown').write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError,'CYCLE_IPC_NOT_EMPTY'):
            self.start()
        self.assertEqual((self.ipc/'unknown').read_bytes(),b'unknown')
        self.assertFalse(list(self.session.glob('catalog-*.json')))

    def test_pending_cursor_blocks_fresh_catalog_attach(self):
        s,child=self.start(reserve=True)
        self.assertTrue(s.cycle_failed)
        self.assertEqual(s.cycle_control.state.state['pending']['host_counter'],7)
        self.assertFalse(list(self.session.glob('catalog-*.json')))
        self.assertTrue(child.session_owner.is_running())

    def test_mismatched_backend_cycle_fails_closed(self):
        s, child = self.start(cycle=['two', 'one'])
        self.assertTrue(s.cycle_failed)
        self.assertIsNone(s.cycle_control.catalog)
        self.assertEqual(list(self.ipc.iterdir()), [])
        self.assertIsNone(child.process.poll())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_config_multi_cycle_automatically_selects_guarded_path(self):
        backend = session.Backend(self.root, self.session/'journal', {})
        s = ICCupSupervisor(self.cfg, output=io.StringIO(), backend=backend)
        self.service = s
        self.assertEqual(s.cycle_session['cycle'], ['one','two','one'])
        self.assertFalse(s._cycle_permits_native_spawn())

if __name__ == '__main__': unittest.main()
