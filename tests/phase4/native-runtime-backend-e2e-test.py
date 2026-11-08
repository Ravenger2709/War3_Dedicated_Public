#!/usr/bin/env python3
"""Offline real RuntimeBackend/controller/native from public multi-map config."""
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('production_e2e_fixture', ROOT/'tests/phase4/test_preload_catalog.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
from src.runtime.controller import ICCupSupervisor, RuntimeBackend
from src.runtime.files import private_json
from src.supervisor.config import load_config

@unittest.skipUnless(Path(fixture.fixture.STORM).is_file(),'offline StormLib required')
class ProductionBackendE2E(unittest.TestCase):
    def test_real_backend_votes_autostart_capacity_and_repeated_map(self):
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        hold_seconds=int(os.environ.get('WAR3_TEST_HOLD_SECONDS','3'))
        self.assertTrue(3<=hold_seconds<=900,'offline hold must be 3..900 seconds')
        maps=fixture.PreparedCatalogTests('runTest');maps.setUp();self.addCleanup(maps.doCleanups)
        root=maps.root;state=root/'state';state.mkdir(mode=0o700)
        profile=root/'profile';profile.mkdir(mode=0o700)
        gateway=json.dumps('@ByteArray('+json.dumps({'Synthetic':'127.0.0.1:16112'})+')')
        ini=profile/'launcher.ini';ini.write_text('[General]\nUsername="Synthetic"\nConnections=\nCurrConnections=0\nSiteConnections='+gateway+'\nUserConnections='+gateway+'\n');ini.chmod(0o600)
        host=root/'host';host.mkdir(mode=0o700)
        (state/'native').mkdir(mode=0o700)
        cfgdir=state/'native'/'mapcfg';cfgdir.mkdir(mode=0o700)
        for name in ('common.j','blizzard.j'):(cfgdir/name).write_bytes(b'globals\nendglobals\n')
        binary=os.environ.get('WAR3_TEST_NATIVE_BINARY',str(ROOT/'build/ghost/ghost++'))
        raw={'mode':'iccup','runtime_dir':str(state/'runtime'),
             'hostbot':{'cwd':str(host),'command':[binary,str(state/'native'/'ghost.cfg')],
                        'startup_timeout':60,'stop_timeout':5,'lobby_timeout':60},
             'http':{'host':'127.0.0.1','port':0},
             'iccup':{'native_config':str(maps.builder.source),'bridge_dir':str(root/'bridge'),
                      'profile':str(profile),'prefix':str(root/'prefix'),'session':str(root/'session'),
                      'metadata_ttl':3,'native_state_ttl':15},
             'gameplay':{'map_cycle':['one','two','one'],'max_concurrent_matches':2,
                         'max_players':4,'autostart_players':4,'lobby_message_interval':10}}
        path=root/'supervisor.json';path.write_text(json.dumps(raw))
        config=load_config(path)
        import src.runtime.catalog_startup as catalog
        import src.runtime.preload_catalog as preload
        actual=catalog.prepare_catalog_startup
        actual_preload=preload.prepare_preload_cycle
        def prepare(*args,**kwargs):return actual(*args,**kwargs,library=fixture.fixture.STORM)
        def early_check(*args,**kwargs):return actual_preload(*args,**kwargs,library=fixture.fixture.STORM)
        gameplay_spec=importlib.util.spec_from_file_location('ordinary_peers',ROOT/'tests/phase3/native-test.py')
        gameplay=importlib.util.module_from_spec(gameplay_spec);gameplay_spec.loader.exec_module(gameplay)
        peers=[]
        with (patch('src.runtime.controller._NATIVE_BINARY',binary),
              patch.object(catalog,'prepare_catalog_startup',side_effect=prepare),
              patch.object(preload,'prepare_preload_cycle',side_effect=early_check)):
            s=ICCupSupervisor(config,output=io.StringIO())
            self.assertIs(type(s.backend),RuntimeBackend)
            self.assertIs(type(s),ICCupSupervisor)
            s.circuit_open.add('realm');s.permanent_errors['realm']='OFFLINE_SYNTHETIC'
            def until(predicate,timeout=15,label='condition'):
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    if s.cycle_failed:self.fail(label+': cycle blocked: '+s.output.getvalue()[-4000:])
                    if not s.running:self.fail(label+': supervisor stopped: '+s.output.getvalue()[-4000:])
                    if predicate():return
                    time.sleep(.05)
                self.fail(label+': timeout: '+s.output.getvalue()[-4000:])
            def lobby(index):
                def matching():
                    event=s.snapshot()['game']
                    return (event.get('phase')=='LOBBY' and event.get('catalog_index')==index
                            and s.cycle_control and s.cycle_control.state.state['pending']
                            and s.cycle_control.state.state['pending']['host_counter']==event['host_counter'])
                until(matching,65,'reserved lobby '+str(index))
                return dict(s.snapshot()['game'])
            view=SimpleNamespace(key=0,snapshot=lambda:private_json(root/'bridge/native-intent.json'))
            def join(names,size,total_players=None):
                group=[]
                for name in names:
                    peer=gameplay.Peer(view,name,ready=False,game_keepalive=True,server_port=6113)
                    peers.append(peer);group.append(peer)
                    peer.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',size)))
                until(lambda:all(all(any(line.startswith(command+' - ') for line in peer.chats())
                                    for command in ('!help','!status','!vote')) for peer in group),
                      10,'English command MOTD delivered to each joining player')
                until(lambda:all(any(frame[1]==0x01 for frame in peer.frames) for peer in group),15,'real host pings')
                expected=len(group) if total_players is None else total_players
                until(lambda:s.snapshot()['game'].get('players')==expected,10,'native player count')
                time.sleep(.15)  # Let the host consume the ping replies before voting.
                return group
            def load(group,count):
                until(lambda:all(any(frame[1]==0x0b for frame in peer.frames) for peer in group),25,'natural countdown')
                for peer in group:peer.sock.sendall(gameplay.frame(0x23))
                until(lambda:all(any(frame[1]==0x0c for frame in peer.frames) for peer in group),15,'loaded action stream')
                until(lambda:s.snapshot().get('cycle',{}).get('active_matches')==count,10,'owned active count')
            def action(peer):
                previous=len(peer.frames)
                peer.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
                until(lambda:any(frame[1]==0x0c and len(frame)>8 for frame in peer.frames[previous:]),10,'action echo')
                self.assertFalse(peer.disconnected)
            try:
                s.start()
                a=lobby(0);native_pid=s.children['native'].process.pid
                self.assertEqual(a['map_name'],'a.w3x')
                # Observe several idle native heartbeats with the default 3s
                # realm TTL. Monitoring must not flap between the 4s frames.
                until(lambda:(s.snapshot()['cycle'].get('current_lobby') or {}).get('host_counter')==a['host_counter'],
                      5,'initial cycle monitoring snapshot')
                idle_until=time.monotonic()+10
                while time.monotonic()<idle_until:
                    cycle=s.snapshot()['cycle']
                    self.assertEqual(cycle['active_matches'],0)
                    self.assertEqual(cycle['current_lobby']['host_counter'],a['host_counter'])
                    self.assertEqual(cycle['availability']['active_matches'],'verified_snapshot')
                    time.sleep(.1)
                first=join(['VoteA1'],maps.a.stat().st_size)
                until(lambda:'Auto-start requires 4 human players (3 more needed).' in first[0].chats(),
                      15,'separate auto-start notice')
                until(lambda:'Voting requires at least 2 human players (1 more needed). Type "!vote" to start map' in first[0].chats(),
                      6,'periodic voting reminder without invoking a command')
                self.assertFalse(any('Voting' in line or '!vote' in line for line in first[0].chats()
                                     if line.startswith('Auto-start requires ')))
                first+=join(['VoteA2'],maps.a.stat().st_size,total_players=2)
                first[0].chat('!vote')
                until(lambda:s.snapshot()['game'].get('votes')==1,5,'first vote')
                first[1].chat('!vote');load(first,1)
                until(lambda:s.cycle_control.state.state['index']==1,10,'A advances cursor')
                b=lobby(1)
                self.assertEqual((b['map_name'],b['max_players']),('b.w3x',3))
                self.assertNotEqual(a['host_counter'],b['host_counter'])
                action(first[0])
                second=join(['AutoB1','AutoB2','AutoB3'],maps.b.stat().st_size)
                load(second,2)  # No vote or test command: B uses real autostart.
                until(lambda:s.cycle_control.state.state['index']==2,10,'B advances cursor')
                until(lambda:s.snapshot().get('cycle',{}).get('status_code')=='CAPACITY_FULL',10,'capacity full')
                matches=s.snapshot()['cycle']['active_match_list']
                self.assertEqual({item['preset_id'] for item in matches},{'one','two'})
                until(lambda:all(item['phase']=='playing' for item in s.snapshot()['cycle']['active_match_list']),10,'both games loaded')
                self.assertEqual(len({item['match_id'] for item in matches}),2)
                action(first[0]);action(second[0])
                def resources():
                    status=Path(f'/proc/{native_pid}/status').read_text()
                    rss=next(int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmRSS:'))
                    return len(list(Path(f'/proc/{native_pid}/fd').iterdir())),rss
                baseline_fds,baseline_rss=resources()
                peak_fds,peak_rss=baseline_fds,baseline_rss
                held=time.monotonic();echoes=0;slowest=0.;last_report=held
                while time.monotonic()-held<hold_seconds:
                    for peer in (first[0],second[0]):
                        started=time.monotonic();action(peer)
                        slowest=max(slowest,time.monotonic()-started);echoes+=1
                    cycle=s.snapshot()['cycle']
                    self.assertEqual(cycle['active_matches'],2)
                    self.assertTrue(all(item['phase']=='playing' for item in cycle['active_match_list']))
                    self.assertEqual(s.children['native'].process.pid,native_pid)
                    fds,rss=resources();peak_fds=max(peak_fds,fds);peak_rss=max(peak_rss,rss)
                    self.assertLessEqual(fds,baseline_fds+2,'native descriptors grew during stable matches')
                    self.assertLessEqual(rss,baseline_rss+64*1024*1024,'native memory growth exceeded soak budget')
                    if hold_seconds>=60 and time.monotonic()-last_report>=60:
                        print('OFFLINE_SOAK '+json.dumps({'seconds':int(time.monotonic()-held),
                              'active_matches':2,'action_echoes':echoes,'peak_fds':peak_fds,
                              'peak_rss_bytes':peak_rss}),flush=True)
                        last_report=time.monotonic()
                    time.sleep(min(2,max(0,hold_seconds-(time.monotonic()-held))))
                if hold_seconds>=60:
                    print('OFFLINE_SOAK_RESULT '+json.dumps({'held_seconds':round(time.monotonic()-held,3),
                          'action_echoes':echoes,'max_echo_seconds':round(slowest,3),
                          'baseline_fds':baseline_fds,'peak_fds':peak_fds,
                          'baseline_rss_bytes':baseline_rss,'peak_rss_bytes':peak_rss,
                          'game_engine_executed':False,'realm_exercised':False}),flush=True)
                self.assertEqual(s.snapshot()['game']['phase'],'IN_GAME')
                self.assertIsNone(s.cycle_control.catalog.data['pending'])
                for peer in first:peer.close()
                until(lambda:s.snapshot()['cycle']['active_matches']==1,15,'A releases capacity')
                action(second[0])
                repeat=lobby(0)
                self.assertEqual((repeat['map_name'],repeat['max_players']),('a.w3x',4))
                self.assertNotIn(repeat['host_counter'],(a['host_counter'],b['host_counter']))
                self.assertEqual(s.cycle_control.catalog.data['pending']['position'],2)
                self.assertEqual(s.cycle_control.state.state['index'],2)
                action(second[0])
                self.assertEqual(s.child_generations,{'native':1,'star':0,'relay':0})
                self.assertEqual(s.children['native'].process.pid,native_pid)
                self.assertIn('bot_preload_default_sha256 = ',Path(s.backend.command('native',1)[1]).read_text())
            finally:
                for peer in peers:peer.close()
                s.running=False;s.thread.join(12)
                self.assertFalse(s.thread.is_alive())

if __name__=='__main__':unittest.main(verbosity=2)
