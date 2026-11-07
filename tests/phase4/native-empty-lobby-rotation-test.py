#!/usr/bin/env python3
"""Real GHost + controller idle rotation; no account, realm, Wine or engine."""
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('idle_controller_fixture',ROOT/'tests/phase4/native-controller-cycle-e2e-test.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)


class EmptyLobbyControllerTests(base.ControllerCycleE2E):
    def setUp(self):
        original=base.gameplay.GameplayTests.native
        original_prepare=base.fixtures.PreparedCatalogTests.prepare
        def native(fixture,extra=None):
            return original(fixture,{**(extra or {}),'bot_empty_lobby_rotation_seconds':'0'})
        def prepare(maps,cycle=('one','two','one')):
            if self._testMethodName == 'test_computers_do_not_pin_an_empty_lobby':
                maps.a.unlink()
                maps.a=maps.builder.mpq(script=base.fixtures.fixture.AI_SCRIPT,
                    info=base.fixtures.fixture.info_fixture(statuses=[1]*4,colours=[0,1,6,7],title=b'AI map'),name='a.w3x')
                maps.doc.pop('capabilities',None)
                maps.doc['presets'][0]['ai']={'enabled':True,'target':4,'skill':2}
                maps.preset_path.write_text(json.dumps(maps.doc))
            return original_prepare(maps,cycle)
        with (patch.object(base.gameplay.GameplayTests,'native',native),
              patch.object(base.fixtures.PreparedCatalogTests,'prepare',prepare)):
            super().setUp()

    def idle_events(self):
        events=[]
        for p in (self.root/'journal').glob('close-*.ack'):
            v=json.loads(p.read_text())
            if v.get('version')==3:events.append(v)
        return events

    def test_empty_lobbies_skip_in_order_without_recording_games(self):
        pid=self.child.process.pid;first=self.lobby(0);self.reserve(first)
        self.command('emptytimeout 2');self.until(lambda:self.current_index()==1,12,'first idle skip')
        self.command('emptytimeout 0')
        second=self.lobby(1);self.reserve(second)
        self.assertEqual(second['game_name'],'Named B')
        self.command('emptytimeout 2');self.until(lambda:self.current_index()==2,12,'second idle skip')
        self.command('emptytimeout 0')
        third=self.lobby(2);self.reserve(third)
        self.assertEqual(third['game_name'],'Named C')
        self.assertEqual(self.child.process.pid,pid);self.assertEqual(self.host()['active_games'],0)
        self.assertIsNone(self.s.cycle_control.state.state['last_started'])
        self.assertEqual(len(self.idle_events()),2)
        self.assertTrue(all(v['reason']=='empty_timeout' for v in self.idle_events()))
        self.assertIn('Empty lobby timeout reached',self.s.output.getvalue())

    def test_downloading_human_blocks_rotation_and_leaving_starts_full_timeout(self):
        first=self.lobby(0);self.reserve(first)
        peer=self.n.peer('Downloading',ready=False)
        self.command('emptytimeout 2')
        time.sleep(3.2)
        self.assertFalse(peer.disconnected);self.assertEqual(self.current_index(),0)
        self.assertEqual(self.host()['lobby']['host_counter'],first['host_counter'])
        self.assertEqual(self.idle_events(),[])
        peer.close();self.until(lambda:self.host()['lobby']['players']==0,4,'human leaves lobby')
        left=time.monotonic();time.sleep(1.2)
        self.assertEqual(self.current_index(),0)
        self.until(lambda:self.current_index()==1,10,'fresh idle period expires')
        self.assertGreaterEqual(time.monotonic()-left,1.8)
        self.command('emptytimeout 0')

    def test_computers_do_not_pin_an_empty_lobby(self):
        first=self.lobby(0);self.reserve(first)
        self.assertEqual(first['players'],0)
        # This four-slot fixture retains admission headroom for the virtual host.
        self.assertGreater(first['computer_players'],0)
        self.command('emptytimeout 2')
        self.until(lambda:self.current_index()==1,12,'empty AI lobby advances')
        self.command('emptytimeout 0')
        self.assertEqual(self.host()['active_games'],0)
        self.assertIsNone(self.s.cycle_control.state.state['last_started'])

    def test_pending_handshake_defers_expiry_without_resetting_timer(self):
        first=self.lobby(0);self.reserve(first)
        potential=socket.create_connection(('127.0.0.1',16113),timeout=2)
        try:
            self.command('emptytimeout 2');time.sleep(3.2)
            self.assertEqual(self.current_index(),0);self.assertEqual(self.idle_events(),[])
        finally:potential.close()
        self.until(lambda:self.current_index()==1,6,'pending handshake is gone')
        self.command('emptytimeout 0')

    def test_running_match_survives_next_empty_lobby_rotation(self):
        first=self.lobby(0);self.reserve(first)
        self.until(lambda:self.n.path.exists() and self.n.snapshot()['operation']=='publish',8,'first published')
        peers=[self.n.peer(name,ready=False,game_keepalive=True) for name in ('RunningA','RunningB')]
        for p in peers:p.sock.sendall(base.gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        self.command('ping');self.command('startlobby')
        for p in peers:p.sock.sendall(base.gameplay.frame(0x23))
        self.until(lambda:all(p.game_keepalives_sent>1 for p in peers),10,'running game actions')
        self.until(lambda:self.current_index()==1,10,'real start advances once')
        second=self.lobby(1);self.reserve(second)
        counters=[p.game_keepalives_sent for p in peers];pid=self.child.process.pid
        self.command('emptytimeout 2');self.until(lambda:self.current_index()==2,12,'empty next lobby skips')
        self.command('emptytimeout 0')
        third=self.lobby(2);self.reserve(third)
        self.until(lambda:all(p.game_keepalives_sent>n for p,n in zip(peers,counters)),5,'original game keeps producing actions')
        self.assertTrue(all(not p.disconnected for p in peers))
        self.assertEqual(self.host()['active_games'],1);self.assertEqual(self.child.process.pid,pid)
        self.assertEqual(len(self.acknowledged_starts()),1)


if __name__=='__main__':
    names=[n for n in EmptyLobbyControllerTests.__dict__ if n.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(EmptyLobbyControllerTests(n) for n in names))
    raise SystemExit(not result.wasSuccessful())
