"""Offline supervisor descriptor lifecycle with real local subprocesses."""
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.runtime.controller import ICCupSupervisor
from src.runtime.preload_inputs import MapInput, pin_preload_inputs

CHILD = r'''
import hashlib,json,os,sys,time
fd,dev,ino = map(int,sys.argv[1:4])
try:
    st=os.fstat(fd)
    same=(st.st_dev,st.st_ino)==(dev,ino)
    digest=hashlib.sha256(os.pread(fd,st.st_size,0)).hexdigest() if same else None
except OSError:
    same=False; digest=None
print(json.dumps({'same':same,'digest':digest}),flush=True)
time.sleep(30)
'''

class CatalogBackend:
    def __init__(self, path):
        self.path = path
        self.expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.catalogs = []
        self.fail_command = False
        self.bad_exec = False

    def native_preload(self, generation):
        catalog = pin_preload_inputs([MapInput('map', self.path, self.expected)],
                                     default_map_bytes=self.path.stat().st_size, max_games=2)
        self.catalogs.append(catalog)
        return catalog

    def command(self, name, generation):
        if self.fail_command: raise RuntimeError('simulated command failure')
        if self.bad_exec: return ['/definitely-not-a-real-war3-executable']
        entry = self.catalogs[-1].maps[0]
        return [sys.executable, '-c', CHILD, str(entry.fd), str(entry._device), str(entry._inode)]


class SupervisorFDTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); path = root / 'map.w3x'; path.write_bytes(b'synthetic sealed map')
        self.backend = CatalogBackend(path)
        s = object.__new__(ICCupSupervisor)
        s.backend = self.backend
        s.config = {'hostbot': {'cwd': str(root), 'stop_timeout': 1}}
        s.child_generations = {name: 0 for name in ('native', 'relay', 'star')}
        s.children = {}; s.component_exits = {}; s.last_exit_code = None
        s.guard = threading.RLock(); s.running = True; s.events = queue.Queue()
        s.log = lambda *args, **kwargs: None
        self.service = s
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for name in list(self.service.children):
            self.service._stop_component(name)
        self.service.running = False

    def event(self, name, generation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try: event = self.service.events.get(timeout=.1)
            except queue.Empty: continue
            if event[0:3] == ('component_line', name, generation):
                try: return json.loads(event[3])
                except json.JSONDecodeError: continue
        self.fail('bounded child output timeout')

    def test_only_native_inherits_and_restart_gets_fresh_catalog(self):
        s = self.service
        first = s._spawn('native'); first_catalog = self.backend.catalogs[-1]
        first_identity = (first_catalog.maps[0]._device, first_catalog.maps[0]._inode)
        self.assertEqual(self.event('native', first.generation), {'same': True, 'digest': self.backend.expected})
        for name in ('relay', 'star'):
            child = s._spawn(name)
            self.assertEqual(self.event(name, child.generation), {'same': False, 'digest': None})
            self.assertIsNone(child.preload)
            s._stop_component(name)
        os.kill(first.process.pid, signal.SIGKILL)
        first.process.wait(timeout=5)
        s._stop_component('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): first_catalog.pass_fds()
        second = s._spawn('native'); second_catalog = self.backend.catalogs[-1]
        self.assertEqual(self.event('native', second.generation), {'same': True, 'digest': self.backend.expected})
        self.assertNotEqual(first_identity, (second_catalog.maps[0]._device, second_catalog.maps[0]._inode))
        s._stop_component('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): second_catalog.pass_fds()

    def test_command_and_popen_failures_close_supervisor_descriptors(self):
        self.backend.fail_command = True
        with self.assertRaisesRegex(RuntimeError, 'command failure'): self.service._spawn('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): self.backend.catalogs[-1].pass_fds()
        self.backend.fail_command = False
        with patch('src.runtime.controller.subprocess.Popen', side_effect=OSError('simulated spawn failure')):
            with self.assertRaises(OSError): self.service._spawn('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): self.backend.catalogs[-1].pass_fds()
        self.assertNotIn('native', self.service.children)

    def test_failed_stop_retains_live_child_and_catalog_for_retry(self):
        child = self.service._spawn('native'); catalog = self.backend.catalogs[-1]
        self.assertTrue(self.event('native', child.generation)['same'])
        with patch('src.runtime.controller.os.killpg', side_effect=PermissionError('simulated')):
            with self.assertRaises(PermissionError): self.service._stop_component('native')
        self.assertIs(self.service.children['native'], child)
        self.assertIsNone(child.process.poll())
        self.assertEqual(len(catalog.pass_fds()), 1)
        self.service._stop_component('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): catalog.pass_fds()

    def test_exec_failure_is_reaped_and_closes_catalog(self):
        self.backend.bad_exec = True
        child = self.service._spawn('native'); catalog = self.backend.catalogs[-1]
        self.assertNotEqual(child.process.wait(timeout=5), 0)
        self.service._stop_component('native')
        with self.assertRaisesRegex(ValueError, 'CLOSED'): catalog.pass_fds()

if __name__ == '__main__': unittest.main()
