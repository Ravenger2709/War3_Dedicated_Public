#!/usr/bin/env python3
"""Three synthetic native matches, natural starts, and a fourth lobby.

This reuses the offline controller harness: no Star, realm or Warcraft engine.
Only lobby-creation time is aged; votes and countdowns follow native handlers.
The harness launch override is not evidence for production startup acceptance.
"""
import importlib.util
from pathlib import Path
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location(
    'capacity3_countdown', ROOT/'tests/phase4/native-controller-countdown-e2e-test.py')
countdown=importlib.util.module_from_spec(spec);spec.loader.exec_module(countdown)
base=countdown.base


@unittest.skipUnless(Path(base.fixtures.fixture.STORM).is_file(),'offline StormLib required')
class CapacityThreeE2E(unittest.TestCase):
    MAX_MATCHES=3
    setUp=base.ControllerCycleE2E.setUp
    shutdown=base.ControllerCycleE2E.shutdown
    until=base.ControllerCycleE2E.until
    line=base.ControllerCycleE2E.line
    command=countdown.NaturalCountdownE2E.command
    host=base.ControllerCycleE2E.host
    current_index=base.ControllerCycleE2E.current_index
    lobby=base.ControllerCycleE2E.lobby
    reserve=base.ControllerCycleE2E.reserve
    acknowledged_starts=base.ControllerCycleE2E.acknowledged_starts
    action=base.ControllerCycleE2E.action
    players=countdown.NaturalCountdownE2E.players
    countdown_and_load=countdown.NaturalCountdownE2E.countdown_and_load

    def test_three_matches_survive_capacity_and_fourth_lobby(self):
        matches=[]
        groups=[]
        for index,names in enumerate((['Owner','VoteA'],
                                      ['Owner','AutoB1','AutoB2'],
                                      ['Owner','AutoC'])):
            lobby=self.lobby(index)
            self.assertEqual(lobby['map_name'],('a.w3x','b.w3x','c.w3x')[index])
            players=self.players(lobby,names,self.bundle.maps[index].size)
            if index==0:
                players[0].chat('!vote')
                self.until(lambda:(self.host()['lobby'] or {}).get('votes')==1,5,'first A vote')
                players[1].chat('!vote')
            self.countdown_and_load(players,index+1)
            self.until(lambda:self.current_index()==(index+1)%3,12,'durable cycle advance')
            self.until(lambda:lobby['host_counter'] in self.acknowledged_starts(),8,'start ACK')
            matches.append(lobby)
            groups.append(players)
            for previous in groups:
                self.action(previous[0])

        counters={item['host_counter'] for item in matches}
        self.assertEqual(len(counters),3)
        records=[self.acknowledged_starts()[item['host_counter']] for item in matches]
        self.assertEqual(len({item['match_id'] for item in records}),3)
        self.assertEqual(len({item['session_generation'] for item in records}),1)
        self.until(lambda:self.s.snapshot().get('cycle',{}).get('status_code')=='CAPACITY_FULL',10,
                   'normal full-capacity status')
        full=self.s.snapshot()['cycle']
        self.assertEqual((full['active_matches'],full['max_matches']),(3,3))
        self.assertEqual({item['host_counter'] for item in full['active_match_list']},counters)
        self.command('autohost');self.command('trycreate')
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual(self.current_index(),0)
        before={path.name:path.stat().st_ino for path in self.ipc.iterdir()}
        time.sleep(3.2)
        self.assertIsNone(self.host()['lobby'])
        self.assertEqual({path.name:path.stat().st_ino for path in self.ipc.iterdir()},before)
        for players in groups:self.action(players[0])

        for peer in groups[0]:peer.close()
        self.until(lambda:self.host()['active_games']==2,12,'A releases capacity')
        self.action(groups[1][0]);self.action(groups[2][0])
        fourth=self.lobby(0)
        self.assertEqual((fourth['map_name'],fourth['game_name']),('a.w3x','Named A'))
        self.assertNotIn(fourth['host_counter'],counters)
        self.reserve(fourth)
        self.action(groups[1][0]);self.action(groups[2][0])
        self.assertTrue(all(not peer.disconnected for players in groups[1:] for peer in players))
        self.assertEqual(self.current_index(),0,'opening a lobby must not advance the cycle')
        self.assertEqual(self.s.child_generations,{'native':1,'star':0,'relay':0})
        self.assertFalse(self.s.cycle_failed)


if __name__=='__main__':unittest.main(verbosity=2)
