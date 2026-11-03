#!/usr/bin/env python3
"""Offline native close-v2 recovery across a killed pre-binding owner."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.runtime.catalog_requests import CatalogRequests
from src.runtime.cycle_coordinator import CycleCoordinator
from src.runtime.cycle_state import CycleState
from src.runtime.preload_manifest import seal_startup_manifest

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
fixtures=load('close_catalog_fixture',ROOT/'tests/phase4/test_preload_catalog.py')
gameplay=load('close_native_fixture',ROOT/'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(),'offline StormLib required')
class NativeCloseRecoveryTests(unittest.TestCase):
    def setUp(self):
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        self.catalog=fixtures.PreparedCatalogTests('runTest');self.catalog.setUp();self.addCleanup(self.catalog.doCleanups)
        self.fixture=gameplay.GameplayTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown);self.addCleanup(self.fixture.doCleanups)

    def test_crash_before_binding_then_late_close_never_retires_new_intent(self):
        with self.catalog.prepare(('one','two','one')) as prepared:
            bundle=seal_startup_manifest(prepared);self.addCleanup(bundle.close)
            manifest=os.pread(bundle.manifest_fd,262144,0)
            n=self.fixture.native({'bot_maxgames':'2','autohost_maxgames':'2',
                'autohost_startplayers':'4','autohost_gamename':'Legacy','autohost_owner':'Owner'})
            shutil.copyfile(self.catalog.a,n.root/'a.w3x')
            (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
            for script in ('common.j','blizzard.j'):(n.root/script).write_bytes(b'globals\nendglobals\n')
            journal=n.root/'journal';journal.mkdir(mode=0o700)
            ipc=n.root/'catalog-ipc';ipc.mkdir(mode=0o700)
            with n.cfg.open('a') as stream:
                stream.write(f'bot_lifecycle_event_dir = {journal}\nbot_start_reservation_gate = 1\n'
                             f'bot_preload_manifest_fd = {bundle.manifest_fd}\n'
                             f'bot_preload_manifest_sha256 = {bundle.manifest_sha256}\n'
                             f'bot_preload_ipc_dir = {ipc}\n')
            n.passed_fds=bundle.pass_fds();n.start()
            n.proc.stdin.write('generation\n');n.proc.stdin.flush()
            generation=n.marker('TEST GENERATION ').split()[-1];n.marker('TEST COMMAND generation')
            cursor_path=n.root/'cursor.json';queue_path=n.root/'requests.json'
            state=CycleState(cursor_path,['one','two','one'])
            queue=CatalogRequests.initialize(queue_path,ipc,manifest,generation)
            req=queue.enqueue(state);queue.dispatch(state)
            gameplay.wait(lambda:queue.dispatch(state)[1]=='staged')
            self.assertIsNone(queue.data['pending']['counter'])
            n.command('autohost');gameplay.wait(lambda:n.host_state()['lobby'] is not None)
            counter=n.host_state()['lobby']['host_counter']
            n.command('rehosterror');gameplay.wait(lambda:n.host_state()['lobby'] is None)
            event_path=journal/f'close-{generation}-{counter}.json'
            gameplay.wait(event_path.exists)
            raw=event_path.read_bytes();event=json.loads(raw)
            self.assertEqual(event,{'version':2,'event':'lobby_closed','session_generation':generation,
                'host_counter':counter,'request_id':req.request_id,'manifest_sha256':bundle.manifest_sha256,
                'catalog_index':req.index})
            # The owner dies before persisting any binding or acknowledging close.
            manifest_path=n.root/'manifest.bin';manifest_path.write_bytes(manifest)
            code='''import os,signal,sys
from pathlib import Path
from src.runtime.cycle_state import CycleState
from src.runtime.catalog_requests import CatalogRequests
state=CycleState(sys.argv[1],['one','two','one'])
queue=CatalogRequests(sys.argv[2],sys.argv[3],Path(sys.argv[4]).read_bytes(),sys.argv[5])
assert state.state['pending'] is None and queue.data['pending']['counter'] is None
os.kill(os.getpid(),signal.SIGKILL)
'''
            proc=subprocess.run([sys.executable,'-c',code,str(cursor_path),str(queue_path),str(ipc),
                                 str(manifest_path),generation],cwd=ROOT,timeout=10)
            self.assertEqual(proc.returncode,-signal.SIGKILL)
            state=CycleState(cursor_path,['one','two','one'])
            queue=CatalogRequests(queue_path,ipc,manifest,generation)
            coordinator=CycleCoordinator(state,journal,catalog_requests=queue)
            self.assertEqual(coordinator.consume().processed,1)
            self.assertEqual(state.state['index'],0);self.assertIsNone(state.state['pending'])
            self.assertEqual(queue.data['completed']['status'],'closed')
            self.assertEqual(queue.data['completed']['counter'],counter)
            with self.assertRaises(ValueError):state.consume_external({'version':1,'event':'game_started',
                'session_generation':generation,'host_counter':counter,'match_id':'c'*32})
            newer=queue.enqueue(state);self.assertNotEqual(newer.request_id,req.request_id)
            ack=event_path.with_suffix('.ack');ack.unlink()
            event_path.write_bytes(raw);event_path.chmod(0o600)
            self.assertEqual(coordinator.consume().processed,1)
            self.assertEqual(queue.data['pending']['request_id'],newer.request_id)
            self.assertEqual(state.state['index'],0)

if __name__=='__main__':unittest.main()
