#!/usr/bin/env python3
"""Real offline native clean stop/restart with a managed cursor and new IPC."""
import importlib.util
import io
import os
from pathlib import Path
import queue
import shutil
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.runtime.config import gameplay_config,iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime.preload_manifest import seal_startup_manifest
from src.runtime.session_lease import SessionLease


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
fixture=load('restart_catalog_fixture','tests/phase4/test_preload_catalog.py')
gameplay=load('restart_native_fixture','tests/phase3/native-test.py')


@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(),'offline StormLib required')
class ControlledNativeRestart(unittest.TestCase):
    def setUp(self):
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        self.maps=fixture.PreparedCatalogTests('runTest');self.maps.setUp();self.addCleanup(self.maps.doCleanups)
        self.game=gameplay.GameplayTests('runTest');self.game.setUp();self.addCleanup(self.game.tearDown)
        self.n=self.game.native({'bot_maxgames':'2','autohost_maxgames':'2',
            'autohost_startplayers':'4','autohost_gamename':'Legacy','autohost_owner':'Owner'})
        shutil.copyfile(self.maps.a,self.n.root/'a.w3x')
        (self.n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
        for script in ('common.j','blizzard.j'):(self.n.root/script).write_bytes(b'globals\nendglobals\n')
        self.base=self.n.cfg.read_text();self.root=self.n.root/'managed'
        lease=SessionLease.create(self.root,enabled=True);lease.close()
        (self.n.root/'bridge').mkdir(mode=0o700)
        parent=self
        class Backend:
            catalog_startup={'cycle':['one','two','one']}
            def prepare(self):pass
            def configure_cycle_launch(self,control):
                ipc=control.provision_launch_ipc()
                self.catalog_startup={'cycle':['one','two','one'],'ipc_directory':str(ipc),
                                      'journal_directory':str(control.lease.journal)}
            def native_preload(self,generation):
                prepared=parent.maps.prepare()
                try:self.bundle=seal_startup_manifest(prepared)
                except BaseException:prepared.close();raise
                spec=self.catalog_startup
                cfg=parent.n.root/f'restart-{generation}.cfg'
                cfg.write_text(parent.base+f"\nbot_lifecycle_event_dir = {spec['journal_directory']}\n"
                    f"bot_start_reservation_gate = 1\nbot_preload_manifest_fd = {self.bundle.manifest_fd}\n"
                    f"bot_preload_manifest_sha256 = {self.bundle.manifest_sha256}\n"
                    f"bot_preload_ipc_dir = {spec['ipc_directory']}\n")
                self.cfg=cfg
                return self.bundle
            def command(self,name,generation):return [str(ROOT/'build/ghost/ghost++'),str(self.cfg)]
            def listener(self,pid):return False
            def cleanup_prefix(self):return {'cleaned':True}
            def close(self):pass
        config={'mode':'iccup','runtime_dir':str(self.n.root/'supervisor'),
            'http':{'host':'127.0.0.1','port':0},
            'hostbot':{'command':['/unused'],'cwd':str(self.n.root),'startup_timeout':15,'stop_timeout':6,'lobby_timeout':1},
            'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
            'iccup':{**iccup_config({}),'bridge_dir':str(self.n.root/'bridge')},
            'gameplay':gameplay_config({'max_concurrent_matches':2})}
        self.config,self.backend_type=config,Backend
        self.s=ICCupSupervisor(config,output=io.StringIO(),backend=Backend(),
                               cycle_session={'root':str(self.root),'cycle':['one','two','one']})
        self.addCleanup(self.cleanup)
        self.s._cycle_initialize()
        self.assertFalse(self.s._cycle_permits_native_spawn())

    def cleanup(self):
        self.s._stop_component('native')
        if self.s.cycle_control:self.s.cycle_control.close()
        self.s.running=False;self.s.lockfile.close()

    def launch(self):
        child=self.s._spawn('native');deadline=time.monotonic()+15; lines=[]
        while time.monotonic()<deadline:
            try:event=self.s.events.get(timeout=.2)
            except queue.Empty:continue
            if event[0]=='component_line' and event[1]=='native':
                lines.append(event[3]);self.s._native_line(event[2],event[3],*event[4:])
                if self.s.cycle_failed:self.fail('session binding failed: '+self.s.output.getvalue()[-3000:])
                if self.s.cycle_control.catalog is not None:return child
        self.fail('native startup timed out: '+repr(lines[-30:])+self.s.output.getvalue()[-3000:])

    def stop_cleanly(self,child):
        fds=child.preload.pass_fds()
        self.s._stop_component('native',controlled_cycle=True)
        self.assertEqual(child.session_owner._verified_exit[1],0)
        for fd in fds:
            with self.assertRaises(OSError):os.fstat(fd)
        self.s.cycle_next_scan=0.;self.s._cycle_tick()
        self.assertFalse(self.s.cycle_failed,self.s.output.getvalue()[-3000:])

    def test_real_native_controlled_restart_keeps_cursor_and_rotates_ipc(self):
        previous=self.s.cycle_control.state.reserve(7)
        self.s.cycle_control.state.started(previous['match_id'],7)
        self.assertEqual(self.s.cycle_control.state.current,'two')
        first=self.launch();old_journal=self.s.cycle_control.lease.journal
        first_ipc=Path(self.s.backend.catalog_startup['ipc_directory']);inode=first_ipc.stat().st_ino
        cursor=self.s.cycle_control.state.state.copy()
        self.stop_cleanly(first)
        self.assertEqual(self.s.cycle_control.state.state,cursor)
        self.assertNotEqual(old_journal,self.s.cycle_control.lease.journal)
        # Restart the supervisor too: only the managed cursor/journals persist.
        self.s.cycle_control.close();self.s.lockfile.close();self.s.running=False
        self.s=ICCupSupervisor(self.config,output=io.StringIO(),backend=self.backend_type(),
                               cycle_session={'root':str(self.root),'cycle':['one','two','one']})
        self.s._cycle_initialize()
        self.assertEqual(self.s.cycle_control.state.state,cursor)
        second=self.launch()
        second_ipc=Path(self.s.backend.catalog_startup['ipc_directory'])
        self.assertNotEqual(first_ipc,second_ipc);self.assertNotEqual(inode,second_ipc.stat().st_ino)
        self.assertTrue(first_ipc.is_dir());self.assertEqual(self.s.cycle_control.state.state,cursor)
        self.stop_cleanly(second)
        self.assertFalse(self.s._cycle_permits_native_spawn())

if __name__=='__main__':unittest.main()
