"""Exercise manifest and map FD handoff in real offline subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import unittest

from src.runtime.controller import ICCupSupervisor
from src.runtime.preload_manifest import seal_startup_manifest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalog_supervisor_fixture', ROOT / 'tests/phase4/test_preload_catalog.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)

CHILD = '''
import json,os,sys,time
manifest,dev,ino=map(int,sys.argv[1:])
try:
 st=os.fstat(manifest)
 same=(st.st_dev,st.st_ino)==(dev,ino)
 if same:
  from src.runtime.preload_manifest import parse_startup_manifest
  parsed=parse_startup_manifest(os.pread(manifest,st.st_size,0))
  maps=[os.fstat(e['fd']).st_size==e['size'] for e in parsed['entries']]
 else: maps=[]
except OSError: same=False; maps=[]
print(json.dumps({'same':same,'maps':maps}),flush=True)
time.sleep(30)
'''

@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(), 'offline StormLib fixture required')
class BundleSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fixture.PreparedCatalogTests('runTest'); self.catalog.setUp()
        self.addCleanup(self.catalog.doCleanups)
        parent = self
        class Backend:
            bundles = []
            def native_preload(self, generation):
                bundle = seal_startup_manifest(parent.catalog.prepare())
                self.bundles.append(bundle)
                return bundle
            def command(self, name, generation):
                b = self.bundles[-1]; st = os.fstat(b.manifest_fd)
                return [sys.executable, '-c', CHILD, str(b.manifest_fd), str(st.st_dev), str(st.st_ino)]
        self.backend = Backend()
        s = object.__new__(ICCupSupervisor); s.backend = self.backend
        s.config = {'hostbot': {'cwd': str(ROOT), 'stop_timeout': 1}}
        s.child_generations = {name: 0 for name in ('native', 'relay', 'star')}
        s.children = {}; s.component_exits = {}; s.last_exit_code = None
        s.guard = threading.RLock(); s.running = True; s.events = queue.Queue()
        s.log = lambda *args, **kwargs: None
        self.service = s; self.addCleanup(self.stop)

    def stop(self):
        for name in list(self.service.children): self.service._stop_component(name)
        self.service.running = False

    def event(self, name, generation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try: event = self.service.events.get(timeout=.1)
            except queue.Empty: continue
            if event[:3] == ('component_line', name, generation):
                try: return json.loads(event[3])
                except json.JSONDecodeError: pass
        self.fail('bounded child output timeout')

    def test_handoff_is_native_only_and_restart_closes_all_descriptors(self):
        s = self.service
        first = s._spawn('native'); bundle = self.backend.bundles[-1]
        old = bundle.pass_fds(); old_identity = bundle._manifest_identity
        self.assertEqual(self.event('native', first.generation), {'same': True, 'maps': [True, True]})
        for name in ('star', 'relay'):
            child = s._spawn(name)
            self.assertEqual(self.event(name, child.generation), {'same': False, 'maps': []})
            s._stop_component(name)
        os.kill(first.process.pid, signal.SIGKILL); first.process.wait(timeout=5)
        s._stop_component('native')
        for fd in old:
            with self.assertRaises(OSError): os.fstat(fd)
        second = s._spawn('native'); newer = self.backend.bundles[-1]
        self.assertNotEqual(old_identity, newer._manifest_identity)
        self.assertEqual(self.event('native', second.generation), {'same': True, 'maps': [True, True]})
        new_fds = newer.pass_fds(); s._stop_component('native')
        for fd in new_fds:
            with self.assertRaises(OSError): os.fstat(fd)

if __name__ == '__main__': unittest.main()
