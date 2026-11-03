#!/usr/bin/env python3
"""Offline controller/native cycle with real votes and unmodified countdown.

The inherited harness permits one native child and ages only the autohost lobby
creation timer. This test never sends its direct-start or countdown-aging test
commands: W3GS peers vote or meet the actual autostart threshold.
"""
import importlib.util
import json
import os
import struct
import time
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('cycle_e2e_base',ROOT/'tests/phase4/native-controller-cycle-e2e-test.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
gameplay=base.gameplay


@unittest.skipUnless(Path(base.fixtures.fixture.STORM).is_file(),'offline StormLib required')
class NaturalCountdownE2E(unittest.TestCase):
    setUp=base.ControllerCycleE2E.setUp
    shutdown=base.ControllerCycleE2E.shutdown
    until=base.ControllerCycleE2E.until
    line=base.ControllerCycleE2E.line
    def command(self,value):
        if value in ('startlobby','startagain','load','auto') or value.startswith('agecountdown'):
            self.fail('direct start or countdown manipulation is forbidden')
        return base.ControllerCycleE2E.command(self,value)
    host=base.ControllerCycleE2E.host
    current_index=base.ControllerCycleE2E.current_index
    lobby=base.ControllerCycleE2E.lobby
    reserve=base.ControllerCycleE2E.reserve
    acknowledged_starts=base.ControllerCycleE2E.acknowledged_starts
    action=base.ControllerCycleE2E.action

    def players(self,lobby,names,map_size):
        self.reserve(lobby)
        self.until(lambda:self.n.path.exists() and self.n.snapshot()['operation']=='publish'
                   and self.n.snapshot()['host_counter']==lobby['host_counter'],8,'lobby publication')
        players=[]
        for name in names:
            peer=self.n.peer(name,ready=False,game_keepalive=True)
            peer.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',map_size)))
            players.append(peer)
        self.command('ping')
        self.until(lambda:(self.host()['lobby'] or {}).get('players')==len(players),8,'players in lobby')
        return players

    def countdown_and_load(self,players,expected_games):
        self.until(lambda:(self.host()['lobby'] or {}).get('phase')=='COUNTDOWN',18,'native countdown')
        observed=time.monotonic()
        self.until(lambda:all(any(frame[1]==0x0b for frame in peer.frames) for peer in players),12,'W3GS countdown end')
        self.assertGreaterEqual(time.monotonic()-observed,2.0,'countdown ran too quickly')
        for peer in players:peer.sock.sendall(gameplay.frame(0x23))
        self.until(lambda:self.host()['active_games']==expected_games,10,'native start')
        self.until(lambda:all(any(frame[1]==0x0c for frame in peer.frames) for peer in players),10,'W3GS actions')
        for peer in players:
            self.assertEqual(sum(frame[1]==0x0a for frame in peer.frames),1)
            self.assertEqual(sum(frame[1]==0x0b for frame in peer.frames),1)
            self.assertFalse(peer.disconnected)

    def test_vote_then_autostart_capacity_rehost_and_actions(self):
        from src.runtime.preload_manifest import parse_startup_manifest
        size=os.fstat(self.bundle.manifest_fd).st_size
        manifest=parse_startup_manifest(os.pread(self.bundle.manifest_fd,size,0))
        a_settings=manifest['entries'][0]['settings'];b_settings=manifest['entries'][1]['settings']
        self.assertEqual((a_settings[1],a_settings[2],a_settings[3],a_settings[4]),(1,4,5,1))
        self.assertEqual((b_settings[1],b_settings[2],b_settings[3],b_settings[4]),(1,3,5,0))
        a=self.lobby(0)
        self.assertEqual((a['map_name'],a['game_name'],a['max_players']),('a.w3x','Named A',4))
        players_a=self.players(a,['Owner','VoteA'],self.bundle.maps[0].size)
        players_a[0].chat('!vote')
        self.until(lambda:(self.host()['lobby'] or {}).get('votes')==1,5,'first vote recorded')
        self.assertEqual(self.host()['lobby']['phase'],'LOBBY')
        players_a[1].chat('!vote')
        self.countdown_and_load(players_a,1)
        self.until(lambda:self.current_index()==1,12,'durable A start')
        self.until(lambda:a['host_counter'] in self.acknowledged_starts(),8,'A start ACK')
        start_a=self.acknowledged_starts()[a['host_counter']]
        b0=self.lobby(1)
        self.assertEqual((b0['map_name'],b0['game_name'],b0['max_players']),('b.w3x','Named B',3))
        self.reserve(b0);self.action(players_a[0])
        self.command('rehosterror')
        generation=self.s.cycle_control.lease._native_generation
        closed=self.root/'journal'/f"close-{generation}-{b0['host_counter']}.ack"
        self.until(closed.exists,12,'fenced B close ACK')
        close=json.loads(closed.read_text())
        self.assertEqual((close['version'],close['event'],close['request_id'],close['catalog_index']),
                         (2,'lobby_closed',b0['catalog_request_id'],1))
        self.assertEqual(self.current_index(),1)
        b=self.lobby(1)
        self.assertNotEqual((b['host_counter'],b['catalog_request_id']),
                            (b0['host_counter'],b0['catalog_request_id']))
        players_b=self.players(b,['Owner','AutoB1','AutoB2'],self.bundle.maps[1].size)
        # No chat/start command here: all three ready players trigger autostart.
        self.countdown_and_load(players_b,2)
        self.until(lambda:self.current_index()==2,12,'durable B start')
        self.until(lambda:b['host_counter'] in self.acknowledged_starts(),8,'B start ACK')
        start_b=self.acknowledged_starts()[b['host_counter']]
        self.assertEqual(start_a['session_generation'],start_b['session_generation'])
        self.assertNotEqual(start_a['match_id'],start_b['match_id'])
        self.action(players_a[0]);self.action(players_b[0])
        self.command('autohost');self.command('trycreate')
        self.assertIsNone(self.host()['lobby'])
        self.assertIsNone(self.s.cycle_control.catalog.data['pending'])
        before={path.name:path.stat().st_ino for path in self.ipc.iterdir()}
        time.sleep(3.2)
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual({path.name:path.stat().st_ino for path in self.ipc.iterdir()},before)
        self.action(players_a[0]);self.action(players_b[0])
        for peer in players_a:peer.close()
        self.until(lambda:self.host()['active_games']==1,12,'A has ended')
        self.action(players_b[0])
        c=self.lobby(2)
        self.assertEqual((c['map_name'],c['game_name'],c['max_players']),('c.w3x','Named C',2))
        self.reserve(c);self.action(players_b[0])
        self.assertEqual(self.current_index(),2)
        self.assertEqual(self.s.child_generations,{'native':1,'star':0,'relay':0})
        self.assertFalse(self.s.cycle_failed)


if __name__=='__main__':unittest.main(verbosity=2)
