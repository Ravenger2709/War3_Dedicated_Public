#!/usr/bin/env python3
"""Offline real controller loop + merged native, synthetic maps and W3GS peers.

Only this harness opens the production launch fuse and gives the native test
binary a stdin control pipe. Driver commands age autohost and invoke the real
EventGameStarted (which still checks its durable gate). No Star/realm is run.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime.preload_manifest import seal_startup_manifest
from src.runtime.session_lease import SessionLease


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

fixtures=load('controller_e2e_catalog','tests/phase4/test_preload_catalog.py')
gameplay=load('controller_e2e_gameplay','tests/phase3/native-test.py')


class HarnessSupervisor(ICCupSupervisor):
    """Only test-specific launch permission and diagnostic capture differ."""
    def __init__(self,*args,**kwargs):
        self.test_lines=queue.Queue()
        self.freeze_states=False
        super().__init__(*args,**kwargs)
    def _cycle_permits_native_spawn(self):
        return self.child_generations['native']==0
    def _native_line(self,generation,line,*args):
        if self.freeze_states and line.startswith('[HOSTBOT_STATE] '):return
        super()._native_line(generation,line,*args)
        if line.startswith('TEST '):self.test_lines.put(line.rstrip())


@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(),'offline StormLib required')
class ControllerCycleE2E(unittest.TestCase):
    MAX_MATCHES=2

    def setUp(self):
        max_matches=getattr(self,'MAX_MATCHES',2)
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        self.maps=fixtures.PreparedCatalogTests('runTest');self.maps.setUp()
        self.addCleanup(self.maps.doCleanups)
        self.game=gameplay.GameplayTests('runTest');self.game.setUp()
        self.addCleanup(self.game.tearDown);self.addCleanup(self.game.doCleanups)
        self.c=self.maps.builder.mpq(members={
            'war3map.w3i':fixtures.fixture.info_fixture(statuses=[1]*4,title=b'Third'),
            'war3map.j':fixtures.fixture.PLAIN_SCRIPT,'war3map.w3u':b'third distinct'},name='c.w3x')
        self.maps.doc['presets'].insert(2,{'id':'three','enabled':True,'map_filename':'c.w3x',
            'lobby_name':'Named C','max_players':2,'start':{'autostart_players':2}})
        self.maps.preset_path.write_text(json.dumps(self.maps.doc))
        self.maps.game['max_concurrent_matches']=max_matches
        prepared=self.maps.prepare(('one','two','three'));self.addCleanup(prepared.close)
        self.bundle=seal_startup_manifest(prepared);self.addCleanup(self.bundle.close)
        self.n=self.game.native({'bot_maxgames':str(max_matches),'autohost_maxgames':str(max_matches),
            'autohost_startplayers':'4','autohost_gamename':'Legacy','autohost_owner':'Owner'})
        shutil.copyfile(self.maps.a,self.n.root/'a.w3x')
        (self.n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
        for script in ('common.j','blizzard.j'):(self.n.root/script).write_bytes(b'globals\nendglobals\n')
        self.root=self.n.root/'managed';lease=SessionLease.create(self.root,enabled=True);lease.close()
        self.ipc=self.root/'ipc-journal';self.ipc.mkdir(mode=0o700)
        (self.n.root/'bridge').mkdir(mode=0o700)
        with self.n.cfg.open('a') as out:
            out.write(f'bot_lifecycle_event_dir = {self.root}/journal\nbot_start_reservation_gate = 1\n'
                      f'bot_preload_manifest_fd = {self.bundle.manifest_fd}\n'
                      f'bot_preload_manifest_sha256 = {self.bundle.manifest_sha256}\n'
                      f'bot_preload_ipc_dir = {self.ipc}\n')
        n,bundle,root,ipc=self.n,self.bundle,self.root,self.ipc
        binary=ROOT/'build/native-driver'
        class Backend:
            catalog_startup={'cycle':['one','two','three'],'journal_directory':str(root/'journal'),
                             'ipc_directory':str(ipc)}
            def prepare(self):pass
            def native_preload(self,generation):return bundle
            def command(self,name,generation):
                if name!='native':raise AssertionError('realm/Star launch forbidden in harness')
                return [str(binary),str(n.cfg)]
            def listener(self,pid):return False
            def cleanup_prefix(self):return {'cleaned':True}
            def close(self):pass
        config={'mode':'iccup','runtime_dir':str(n.root/'supervisor'),
            'http':{'host':'127.0.0.1','port':0},
            'hostbot':{'command':['/unused'],'cwd':str(n.root),'startup_timeout':165,'stop_timeout':2,'lobby_timeout':1},
            'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':180},
            'iccup':{**iccup_config({}),'bridge_dir':str(n.root/'bridge'),'metadata_ttl':10},
            'gameplay':gameplay_config({'max_concurrent_matches':max_matches})}
        self.s=HarnessSupervisor(config,output=io.StringIO(),backend=Backend(),
                                 cycle_session={'root':str(root),'cycle':['one','two','three']})
        self.assertFalse(ICCupSupervisor._cycle_permits_native_spawn(self.s))
        # Keep the normal realm branch disabled; no Star, relay or credentials.
        self.s.circuit_open.add('realm')
        self.s.permanent_errors['realm']='OFFLINE_HARNESS_NO_REALM'
        self.addCleanup(self.shutdown)
        real_popen=subprocess.Popen
        def spawn(argv,**kwargs):
            # Popen remains owned by the actual ICCupSupervisor._spawn.
            kwargs['stdin']=subprocess.PIPE
            return real_popen(argv,**kwargs)
        class SubprocessProxy:
            def __getattr__(self,name):return getattr(subprocess,name)
            Popen=staticmethod(spawn)
        with mock.patch('src.runtime.controller.subprocess',SubprocessProxy()):
            self.s.start()
            self.until(lambda:self.s.children.get('native') is not None,15,'native spawn')
        self.child=self.s.children['native']
        self.until(lambda:self.s.cycle_control is not None and self.s.cycle_control.catalog is not None,
                   15,'owned session and catalog')
        self.assertEqual(self.s.child_generations['native'],1)
        self.assertEqual(self.s.child_generations['star'],0)
        self.assertEqual(self.s.child_generations['relay'],0)

    def shutdown(self):
        for peer in self.n.peers:peer.close()
        if self.s.running:
            self.s.running=False
            self.s.thread.join(12)
        self.assertFalse(self.s.thread.is_alive())
        # The controller alone owns and waits for the child.
        if hasattr(self,'child') and self.child.process.stdin:self.child.process.stdin.close()

    def until(self,predicate,timeout=12,label='condition'):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            if self.s.cycle_failed:
                self.fail(f"{label}: cycle failed; status={self.s.cycle_status}; logs={(self.s.runtime/'supervisor.jsonl').read_text()[-10000:]}")
            if not self.s.running:self.fail(f'{label}: controller stopped; logs={self.s.output.getvalue()[-3000:]}')
            time.sleep(.03)
        self.fail(f'{label}: timeout; status={self.s.cycle_status}; native={self.s.native_observation}; logs={self.s.output.getvalue()[-3000:]}')

    def line(self,prefix,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:line=self.s.test_lines.get(timeout=.1)
            except queue.Empty:continue
            if line.startswith(prefix):return line
        self.fail('missing native driver output: '+prefix)

    def command(self,value):
        self.child.process.stdin.write((value+'\n').encode());self.child.process.stdin.flush()
        self.line('TEST COMMAND '+value)

    def host(self):
        self.child.process.stdin.write(b'host\n');self.child.process.stdin.flush()
        value=json.loads(self.line('TEST HOST ')[len('TEST HOST '):])
        self.line('TEST COMMAND host')
        return value

    def current_index(self):
        control=self.s.cycle_control
        return None if control is None else control.state.state['index']

    def lobby(self,request_index,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.command('autohost')
            snapshot=self.host();lobby=snapshot['lobby']
            if lobby is not None and lobby.get('catalog_index')==request_index:return lobby
            if self.s.cycle_failed:self.fail('cycle failed while waiting for lobby')
            time.sleep(.2)
        self.fail(f'lobby {request_index} unavailable: {self.s.cycle_status} {self.s.native_observation}')

    def reserve(self,lobby):
        generation=self.s.cycle_control.lease._native_generation
        marker=self.root/'journal'/f"reservation-{generation}-{lobby['host_counter']}.ready"
        self.until(marker.exists,12,'durable reservation')
        return marker

    def start_game(self,lobby,name,map_size,before_start=None):
        self.reserve(lobby)
        self.until(lambda:self.n.path.exists() and self.n.snapshot()['operation']=='publish'
                   and self.n.snapshot()['host_counter']==lobby['host_counter'],8,'lobby advertisement')
        peer=self.n.peer(name,ready=False,game_keepalive=True)
        peer.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',map_size)))
        self.command('ping')
        if before_start is not None:before_start()
        self.command('startlobby')
        peer.sock.sendall(gameplay.frame(0x23))
        self.until(lambda:any(frame[1]==0x0c for frame in peer.frames),10,'W3GS loading and actions')
        return peer

    def acknowledged_starts(self):
        records=[json.loads(path.read_text()) for path in (self.root/'journal').glob('start-*.ack')]
        self.assertTrue(all(record['event']=='game_started' and record['version']==1 for record in records))
        return {record['host_counter']:record for record in records}

    def action(self,peer):
        before=len(peer.frames)
        peer.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        self.until(lambda:any(frame[1]==0x0c and len(frame)>8 for frame in peer.frames[before:]),
                   8,'W3GS outgoing action echoed')
        self.assertFalse(peer.disconnected)

    def admission(self,command):
        self.child.process.stdin.write((command+'\n').encode());self.child.process.stdin.flush()
        result=int(self.line('TEST CATALOGADMISSION ').split()[-1])
        self.line('TEST COMMAND '+command)
        return result

    def prepare_staged_full(self):
        a=self.lobby(0)
        peer_a=self.start_game(a,'RaceA',self.bundle.maps[0].size)
        self.until(lambda:self.current_index()==1,12,'first start')
        self.until(lambda:self.s.cycle_observation is not None and
                   self.s.cycle_observation[1]['phase']=='IN_GAME' and
                   self.s.cycle_observation[1]['active_games']==1,8,'authentic capacity-one snapshot')
        stale=self.s.cycle_observation
        b=self.lobby(1);self.reserve(b)
        def delay_current_state():
            # Replay a real earlier line from the owned reader, simulating a
            # delayed supervisor snapshot while the native event loop advances.
            self.s.freeze_states=True
            self.s.cycle_observation=stale
        peer_b=self.start_game(b,'RaceB',self.bundle.maps[1].size,before_start=delay_current_state)
        self.until(lambda:self.current_index()==2,8,'second durable start')
        self.until(lambda:self.host()['active_games']==2,8,'native capacity full')
        def staged():
            pending=self.s.cycle_control.catalog.data['pending']
            return pending is not None and pending['index']==2 and pending['status']=='staged'
        self.until(staged,8,'stale snapshot dispatches third request')
        pending=dict(self.s.cycle_control.catalog.data['pending'])
        return peer_a,peer_b,pending['request_id']

    def test_stale_snapshot_stages_once_while_native_capacity_is_full(self):
        peer_a,peer_b,request_id=self.prepare_staged_full()
        self.assertEqual(self.admission('catalogcheck'),0)
        for command in ('catalogcheckbad','catalogcheckdigest','catalogcheckindex','catalogchecksettings',
                        'catalogcheckgeneration','catalogcheckmap'):
            self.assertEqual(self.admission(command),-1,command)
        self.assertEqual(self.admission('catalogattempt'),0)
        self.assertIsNone(self.host()['lobby'])
        self.action(peer_a);self.action(peer_b)
        self.until(lambda:not (self.ipc/'next.ready').exists(),8,'native consumes mailbox')
        before={path.name:path.stat().st_ino for path in self.ipc.iterdir()}
        queue_before=self.s.cycle_control.catalog.path.read_bytes()
        time.sleep(2.2)
        self.assertEqual({path.name:path.stat().st_ino for path in self.ipc.iterdir()},before)
        self.assertEqual(self.s.cycle_control.catalog.path.read_bytes(),queue_before)
        self.assertFalse((self.ipc/'next.ready').exists())
        self.assertEqual(self.s.cycle_control.catalog.data['pending']['request_id'],request_id)
        self.assertFalse(self.s.cycle_failed)
        self.assertEqual(self.s.child_generations['native'],1)
        peer_a.close()
        self.until(lambda:self.host()['active_games']==1,10,'capacity becomes available')
        self.s.freeze_states=False
        self.action(peer_b)
        c=self.lobby(2)
        self.assertEqual(c['catalog_request_id'],request_id)
        self.assertEqual((c['map_name'],c['game_name']),('c.w3x','Named C'))
        self.reserve(c);self.action(peer_b)
        self.assertEqual(self.current_index(),2)
        self.assertEqual(self.s.child_generations['native'],1)

    def test_invalid_request_at_creation_blocks_new_games_not_existing_matches(self):
        peer_a,peer_b,request_id=self.prepare_staged_full()
        self.assertEqual(self.admission('catalogattemptbad'),-1)
        self.until(lambda:'catalog admission identity invalid' in
                   (self.s.runtime/'supervisor.jsonl').read_text(),8,'identity rejection')
        self.s.freeze_states=False
        self.until(lambda:self.s.snapshot().get('cycle',{}).get('rotation_errors')==1,10,'rotation error count')
        self.assertEqual(self.s.snapshot()['cycle']['active_matches'],2)
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual(self.host()['active_games'],2)
        self.assertEqual(self.s.cycle_control.catalog.data['pending']['request_id'],request_id)
        self.assertEqual(self.s.child_generations['native'],1)
        self.action(peer_a);self.action(peer_b)
        peer_a.close()
        self.until(lambda:self.host()['active_games']==1,10,'active match exit after invalid admission')
        self.command('autohost')
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual(self.s.cycle_control.catalog.data['pending']['request_id'],request_id)
        self.action(peer_b)

    def test_three_presets_capacity_fenced_rehost_and_live_w3gs(self):
        a=self.lobby(0);self.assertEqual((a['map_name'],a['game_name'],a['max_players']),('a.w3x','Named A',4))
        peer_a=self.start_game(a,'PeerA',self.bundle.maps[0].size)
        self.until(lambda:self.current_index()==1,12,'durable A start')
        self.until(lambda:a['host_counter'] in self.acknowledged_starts(),8,'A start ACK')
        start_a=self.acknowledged_starts()[a['host_counter']]
        b0=self.lobby(1);self.assertEqual((b0['map_name'],b0['game_name'],b0['max_players']),('b.w3x','Named B',3))
        self.reserve(b0);self.action(peer_a)
        self.command('rehosterror')
        generation=self.s.cycle_control.lease._native_generation
        closed=self.root/'journal'/f"close-{generation}-{b0['host_counter']}.ack"
        self.until(closed.exists,12,'fenced B close ACK')
        close_record=json.loads(closed.read_text())
        self.assertEqual((close_record['version'],close_record['event'],close_record['session_generation'],
                          close_record['host_counter'],close_record['request_id'],close_record['catalog_index']),
                         (2,'lobby_closed',generation,b0['host_counter'],b0['catalog_request_id'],1))
        self.assertEqual(set(self.acknowledged_starts()),{a['host_counter']})
        self.assertEqual(self.current_index(),1)
        b=self.lobby(1);self.assertNotEqual(b['host_counter'],b0['host_counter'])
        self.assertNotEqual(b['catalog_request_id'],b0['catalog_request_id'])
        self.assertEqual(self.current_index(),1)
        peer_b=self.start_game(b,'PeerB',self.bundle.maps[1].size)
        self.until(lambda:self.current_index()==2,12,'durable B start')
        self.until(lambda:b['host_counter'] in self.acknowledged_starts(),8,'B start ACK')
        start_b=self.acknowledged_starts()[b['host_counter']]
        self.assertEqual(start_a['session_generation'],start_b['session_generation'])
        self.assertNotEqual(start_a['match_id'],start_b['match_id'])
        self.until(lambda:self.host()['active_games']==2,8,'capacity full')
        def two_matches():
            cycle=self.s.snapshot().get('cycle',{})
            return cycle.get('active_match_list') if cycle.get('active_matches')==2 else None
        self.until(lambda:two_matches() is not None,10,'verified two-match telemetry')
        matches={item['host_counter']:item for item in two_matches()}
        self.assertEqual(set(matches),{a['host_counter'],b['host_counter']})
        self.assertEqual(matches[a['host_counter']]['match_id'],start_a['match_id'])
        self.assertEqual(matches[b['host_counter']]['match_id'],start_b['match_id'])
        self.assertEqual((matches[a['host_counter']]['preset_id'],matches[b['host_counter']]['preset_id']),('one','two'))
        self.assertEqual((matches[a['host_counter']]['map_sha256'],matches[b['host_counter']]['map_sha256']),
                         (self.bundle.maps[0].sha256,self.bundle.maps[1].sha256))
        self.assertTrue(all(item['phase'] in ('loading','playing') for item in matches.values()))
        self.assertEqual(self.s.snapshot()['cycle']['rotation_errors'],0)
        self.action(peer_a);self.action(peer_b)
        self.command('autohost');self.command('trycreate')
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual(self.current_index(),2)
        self.assertIsNone(self.s.cycle_control.catalog.data['pending'])
        before_requests=set(self.ipc.glob('request-*.ready'))
        self.action(peer_a);self.action(peer_b)
        # Remain full across several controller ticks: C must not be selected.
        time.sleep(3.2)
        self.assertIsNone(self.host()['lobby'])
        self.assertIsNone(self.s.cycle_control.catalog.data['pending'])
        self.assertEqual(set(self.ipc.glob('request-*.ready')),before_requests)
        self.assertEqual(self.current_index(),2)
        self.action(peer_a);self.action(peer_b)
        peer_a.close()
        self.until(lambda:self.host()['active_games']==1,12,'first match finished')
        self.until(lambda:self.s.snapshot().get('cycle',{}).get('finished_matches')==1,10,'retired match count')
        self.assertEqual(self.s.snapshot()['cycle']['active_match_list'][0]['host_counter'],b['host_counter'])
        self.action(peer_b)
        c=self.lobby(2)
        self.assertEqual((c['map_name'],c['game_name'],c['max_players']),('c.w3x','Named C',2))
        self.reserve(c);self.action(peer_b)
        self.assertEqual(self.current_index(),2)
        self.assertEqual(self.s.child_generations['native'],1)
        self.assertEqual(self.s.child_generations['star'],0)
        self.assertEqual(self.s.child_generations['relay'],0)


if __name__=='__main__':unittest.main(verbosity=2)
