#!/usr/bin/env python3
"""Offline native startup + automatic event-loop mailbox, supervisor pending."""
import importlib.util
import json
import os
import subprocess
import threading
import time
from pathlib import Path
import shutil
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.runtime.preload_ipc import Request, notify_request, read_ack
from src.runtime.preload_manifest import seal_startup_manifest
from src.runtime.native import native_state

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
fixtures=load('auto_catalog_fixture',ROOT/'tests/phase4/test_preload_catalog.py')
gameplay=load('auto_native_fixture',ROOT/'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(),'offline StormLib required')
class NativeStartupIPCTests(unittest.TestCase):
    def setUp(self):
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        self.catalog=fixtures.PreparedCatalogTests('runTest');self.catalog.setUp();self.addCleanup(self.catalog.doCleanups)
        self.fixture=gameplay.GameplayTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown);self.addCleanup(self.fixture.doCleanups)

    def setup_native(self, bad_digest=False):
        prepared=self.catalog.prepare();self.addCleanup(prepared.close)
        bundle=seal_startup_manifest(prepared);self.addCleanup(bundle.close)
        n=self.fixture.native({'bot_maxgames':'2','autohost_maxgames':'2',
            'autohost_startplayers':'4','autohost_gamename':'Legacy','autohost_owner':'Owner'})
        shutil.copyfile(self.catalog.a,n.root/'a.w3x')
        (n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
        for script in ('common.j','blizzard.j'):(n.root/script).write_bytes(b'globals\nendglobals\n')
        journal=n.root/'journal';journal.mkdir(mode=0o700)
        ipc=n.root/'preload';ipc.mkdir(mode=0o700)
        digest='0'*64 if bad_digest else bundle.manifest_sha256
        with n.cfg.open('a') as stream:
            stream.write(f'bot_lifecycle_event_dir = {journal}\nbot_start_reservation_gate = 1\n'
                         f'bot_preload_manifest_fd = {bundle.manifest_fd}\n'
                         f'bot_preload_manifest_sha256 = {digest}\nbot_preload_ipc_dir = {ipc}\n')
        n.passed_fds=bundle.pass_fds()
        return n,bundle,ipc,journal

    def test_startup_and_automatic_poll_create_only_requested_lobby(self):
        n,bundle,ipc,journal=self.setup_native();n.start()
        n.proc.stdin.write('generation\n');n.proc.stdin.flush()
        generation=n.marker('TEST GENERATION ').split()[-1];n.marker('TEST COMMAND generation')
        self.assertIsNone(n.host_state()['lobby'])
        req=Request(generation,bundle.manifest_sha256,'a'*32,1)
        notify_request(ipc,req)
        gameplay.wait(lambda:read_ack(ipc,req)=='staged')
        self.assertFalse((ipc/'next.ready').exists())
        n.command('autohost');gameplay.wait(lambda:n.host_state()['lobby'] is not None)
        lobby=n.host_state()['lobby']
        self.assertEqual((lobby['map_name'],lobby['catalog_request_id'],lobby['catalog_index']),
                         ('b.w3x',req.request_id,1))
        n.command('closecurrent');gameplay.wait(lambda:n.host_state()['lobby'] is None)
        notify_request(ipc,req)
        gameplay.wait(lambda:not (ipc/'next.ready').exists())
        n.command('autohost');n.command('trycreate')
        self.assertIsNone(n.host_state()['lobby'])

    def test_real_binary_without_stdin_polls_mailbox_and_publishes_proof(self):
        n,bundle,ipc,journal=self.setup_native()
        with n.cfg.open('a') as stream:
            stream.write(f'bot_preload_default_sha256 = {bundle.maps[0].sha256}\n')
        process=subprocess.Popen([str(ROOT/'build/ghost/ghost++'),str(n.cfg)],cwd=n.root,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,bufsize=1,pass_fds=bundle.pass_fds())
        lines=[]
        def read():
            for line in process.stdout:lines.append(line.rstrip())
        reader=threading.Thread(target=read,daemon=True);reader.start()
        def cleanup():
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            reader.join(timeout=3);process.stdout.close()
        self.addCleanup(cleanup)
        def current():
            for line in reversed(lines):
                if line.startswith('[HOSTBOT_STATE] '):
                    return json.loads(line[len('[HOSTBOT_STATE] '):])
            return None
        gameplay.wait(lambda:current() and current().get('session_generation'))
        generation=current()['session_generation']
        session=[json.loads(line[len('[HOSTBOT_SESSION] '):]) for line in lines if line.startswith('[HOSTBOT_SESSION] ')]
        self.assertEqual(len(session),1)
        self.assertEqual((session[0]['version'],session[0]['ipc_device'],session[0]['ipc_inode']),
                         (2,ipc.stat().st_dev,ipc.stat().st_ino))
        self.assertEqual((session[0]['pid'],session[0]['session_generation'],session[0]['journal_device'],session[0]['journal_inode']),
                         (process.pid,generation,journal.stat().st_dev,journal.stat().st_ino))
        req=Request(generation,bundle.manifest_sha256,'b'*32,1)
        notify_request(ipc,req)
        gameplay.wait(lambda:read_ack(ipc,req)=='staged')
        deadline=time.monotonic()+40;proof=None
        while time.monotonic()<deadline:
            proof=current()
            if proof and proof.get('catalog_request_id')==req.request_id:break
            if process.poll() is not None:self.fail(f'native exited: {lines[-20:]}')
            time.sleep(.1)
        self.assertIsNotNone(proof)
        self.assertEqual((proof.get('catalog_request_id'),proof.get('catalog_index'),proof.get('map_name')),
                         (req.request_id,1,'b.w3x'))
        self.assertGreater(proof['host_counter'],0)
        self.assertEqual(proof['session_generation'],generation)
        parsed=native_state('[HOSTBOT_STATE] '+json.dumps(proof))
        self.assertEqual((parsed['catalog_request_id'],parsed['catalog_index']),(req.request_id,1))

    def test_sealed_startup_binds_owned_session_reader(self):
        from src.runtime.session_lease import SessionLease
        from src.runtime.process_ownership import OwnedChild
        from src.runtime.native_session import NativeSessionReader
        n,bundle,ipc,journal=self.setup_native()
        lease=SessionLease.create(n.root/'managed-session',enabled=True)
        n.cfg.write_text(n.cfg.read_text().replace(
            f'bot_lifecycle_event_dir = {journal}\n',f'bot_lifecycle_event_dir = {lease.journal}\n'))
        binary=ROOT/'build/ghost/ghost++'
        process=subprocess.Popen([str(binary),str(n.cfg)],cwd=n.root,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,pass_fds=bundle.pass_fds())
        try:
            with OwnedChild(process) as owned:
                ipc_fd=os.open(ipc,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                self.addCleanup(os.close,ipc_fd)
                reader=NativeSessionReader(owned,lease,binary,ipc_binding=(ipc_fd,ipc))
                try:
                    record=reader.read_and_persist(timeout=10)
                    self.assertEqual(record['session']['pid'],process.pid)
                    self.assertEqual(record['session']['journal_inode'],lease.journal.stat().st_ino)
                    self.assertEqual(record['native']['exe_inode'],binary.stat().st_ino)
                    process.terminate()
                    owned.wait(timeout=5)
                    self.assertIsNotNone(owned._verified_exit)
                finally:reader.close()
        finally:
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close();lease.close()

    def test_real_controller_reserves_only_matching_owned_native_lobby(self):
        import io
        import queue
        from src.runtime.config import gameplay_config, iccup_config
        from src.runtime.controller import ICCupSupervisor
        from src.runtime.session_lease import SessionLease

        n,bundle,old_ipc,old_journal=self.setup_native()
        root=n.root/'managed';lease=SessionLease.create(root,enabled=True);lease.close()
        ipc=root/'ipc-journal';ipc.mkdir(mode=0o700)
        cfg_text=n.cfg.read_text().replace(str(old_journal),str(root/'journal')).replace(str(old_ipc),str(ipc))
        n.cfg.write_text(cfg_text)
        binary=ROOT/'build/ghost/ghost++'
        class Backend:
            catalog_startup={'cycle':['one','two','one'],'journal_directory':str(root/'journal'),
                             'ipc_directory':str(ipc)}
            def prepare(self):pass
            def native_preload(self,generation):return bundle
            def command(self,name,generation):return [str(binary),str(n.cfg)]
            def listener(self,pid):return False
            def cleanup_prefix(self):return {'cleaned':True}
            def close(self):pass
        config={'mode':'iccup','runtime_dir':str(n.root/'supervisor'),
            'http':{'host':'127.0.0.1','port':0},
            'hostbot':{'command':['/unused'],'cwd':str(n.root),'startup_timeout':10,'stop_timeout':2,'lobby_timeout':1},
            'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
            'iccup':{**iccup_config({}),'bridge_dir':str(n.root/'bridge')},'gameplay':gameplay_config({'max_concurrent_matches':2})}
        s=ICCupSupervisor(config,output=io.StringIO(),backend=Backend(),
                          cycle_session={'root':str(root),'cycle':['one','two','one']})
        def cleanup():
            s._stop_component('native')
            if s.cycle_control:s.cycle_control.close()
            s.running=False;s.lockfile.close()
        self.addCleanup(cleanup)
        s._cycle_initialize();child=s._spawn('native')
        def pump():
            try:event=s.events.get(timeout=.1)
            except queue.Empty:event=None
            if event and event[0]=='component_line' and event[1]=='native':
                s._native_line(event[2],event[3],*event[4:])
            s.cycle_next_scan=0.;s._cycle_tick()
            if s.cycle_failed:self.fail('controller rejected native session or journal')
            if not child.session_owner.is_running():self.fail('native exited')
        deadline=time.monotonic()+15
        while s.cycle_control.catalog is None and time.monotonic()<deadline:pump()
        self.assertIsNotNone(s.cycle_control.catalog)
        self.assertFalse(s._cycle_permits_native_spawn())
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            pump()
            record=s.cycle_control.catalog.data['pending']
            if record is not None and s.cycle_control.state.state['pending'] is not None:
                break
        proof=s.native_observation;record=s.cycle_control.catalog.data['pending']
        self.assertIsNotNone(record)
        self.assertEqual((proof.get('catalog_request_id'),proof.get('catalog_index')),
                         (record['request_id'],record['index']))
        self.assertEqual(record['status'],'staged')
        pending=s.cycle_control.state.state['pending']
        self.assertEqual((pending['session_generation'],pending['host_counter']),
                         (proof['session_generation'],proof['host_counter']))
        self.assertTrue(list((root/'journal').glob('reservation-*.ready')))
        self.assertEqual(s.cycle_control.state.state['index'],0)

    def test_session_must_attest_native_ipc_directory_inode(self):
        """Reproduce replacement after native attach but before session delivery."""
        import io
        import queue
        from src.runtime.config import gameplay_config, iccup_config
        from src.runtime.controller import ICCupSupervisor
        from src.runtime.session_lease import SessionLease

        n,bundle,old_ipc,old_journal=self.setup_native()
        root=n.root/'managed';lease=SessionLease.create(root,enabled=True);lease.close()
        ipc=root/'ipc-journal';ipc.mkdir(mode=0o700)
        n.cfg.write_text(n.cfg.read_text().replace(str(old_journal),str(root/'journal')).replace(str(old_ipc),str(ipc)))
        binary=ROOT/'build/ghost/ghost++'
        class Backend:
            catalog_startup={'cycle':['one','two','one'],'journal_directory':str(root/'journal'),
                             'ipc_directory':str(ipc)}
            def prepare(self):pass
            def native_preload(self,generation):return bundle
            def command(self,name,generation):return [str(binary),str(n.cfg)]
            def listener(self,pid):return False
            def cleanup_prefix(self):return {'cleaned':True}
            def close(self):pass
        config={'mode':'iccup','runtime_dir':str(n.root/'supervisor'),
            'http':{'host':'127.0.0.1','port':0},
            'hostbot':{'command':['/unused'],'cwd':str(n.root),'startup_timeout':10,'stop_timeout':2,'lobby_timeout':1},
            'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
            'iccup':{**iccup_config({}),'bridge_dir':str(n.root/'bridge')},
            'gameplay':gameplay_config({'max_concurrent_matches':2})}
        s=ICCupSupervisor(config,output=io.StringIO(),backend=Backend(),
                          cycle_session={'root':str(root),'cycle':['one','two','one']})
        def cleanup():
            s._stop_component('native')
            if s.cycle_control:s.cycle_control.close()
            s.running=False;s.lockfile.close()
        self.addCleanup(cleanup)
        s._cycle_initialize();child=s._spawn('native')
        deadline=time.monotonic()+15;session=None
        while time.monotonic()<deadline:
            try:event=s.events.get(timeout=.2)
            except queue.Empty:continue
            if event[0]=='component_line' and event[1]=='native':
                if event[3].startswith('[HOSTBOT_SESSION] '):session=event;break
                s._native_line(event[2],event[3],*event[4:])
        self.assertIsNotNone(session,f'native session was not emitted: {s.output.getvalue()}')
        original=ipc.stat()
        def owned_inode():
            for path in Path(f'/proc/{child.process.pid}/fd').iterdir():
                try:st=path.stat()
                except FileNotFoundError:continue
                if (st.st_dev,st.st_ino)==(original.st_dev,original.st_ino):return True
            return False
        self.assertTrue(owned_inode(),'native did not pin original IPC inode')
        ipc.rename(root/'detached-ipc');ipc.mkdir(mode=0o700)
        self.assertNotEqual(ipc.stat().st_ino,original.st_ino)
        s._native_line(session[2],session[3],*session[4:])
        self.assertTrue(s.cycle_failed,'supervisor accepted a different IPC inode from native')
        self.assertIsNone(s.cycle_control.catalog)
        self.assertFalse(list(root.glob('catalog-*.json')))
        self.assertEqual(list(ipc.iterdir()),[])
        self.assertTrue(child.session_owner.is_running())

    def test_same_size_changed_default_map_is_not_admitted(self):
        n,bundle,ipc,journal=self.setup_native()
        path=n.root/'a.w3x'; original=path.read_bytes(); mutated=bytearray(original)
        mutated[-1]^=1;path.write_bytes(mutated)
        self.assertEqual(path.stat().st_size,len(original))
        with n.cfg.open('a') as stream:
            stream.write(f'bot_preload_default_sha256 = {bundle.maps[0].sha256}\n')
        process=subprocess.Popen([str(ROOT/'build/ghost/ghost++'),str(n.cfg)],cwd=n.root,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,pass_fds=bundle.pass_fds())
        output,_=process.communicate(timeout=15)
        self.assertIn('native catalog startup validation failed',output[-5000:])
        self.assertNotIn('[HOSTBOT_SESSION]',output)
        self.assertEqual(list(ipc.iterdir()),[])

    def test_default_map_sha_mismatch_fails_before_session_or_ipc(self):
        n,bundle,ipc,journal=self.setup_native()
        with n.cfg.open('a') as stream:
            stream.write('bot_preload_default_sha256 = '+'0'*64+'\n')
        process=subprocess.Popen([str(ROOT/'build/ghost/ghost++'),str(n.cfg)],cwd=n.root,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,pass_fds=bundle.pass_fds())
        output,_=process.communicate(timeout=15)
        self.assertIn('native catalog startup validation failed',output)
        self.assertNotIn('[HOSTBOT_SESSION]',output)
        self.assertEqual(list(ipc.iterdir()),[])

    def test_wrong_manifest_digest_fails_startup(self):
        n,bundle,ipc,journal=self.setup_native(bad_digest=True)
        n.start(ready=False)
        self.assertEqual(n.proc.wait(timeout=10),78)
        n.thread.join(timeout=3)
        self.assertFalse(any(x.startswith('[HOSTBOT_SESSION] ') for x in n.lines))
        self.assertFalse(any(x.startswith('[GHOST] offline initialization complete') for x in n.lines))
        self.assertEqual(list(ipc.iterdir()),[])
        self.assertTrue(any('native catalog startup validation failed' in x for x in n.lines))

if __name__=='__main__':unittest.main()
