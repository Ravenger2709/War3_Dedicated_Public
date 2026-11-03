#!/usr/bin/env python3
"""Offline native lifecycle tests; synthetic peers do not establish production readiness."""
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_gameplay', ROOT / 'tests/phase3/native-test.py')
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
wait = gameplay.wait


class MultiMatchTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown
    native = gameplay.GameplayTests.native

    def test_first_match_receives_server_actions_after_next_lobby_and_start(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                         'autohost_owner': 'Owner'})
        try:
            n.start().create()
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            wait(lambda: any(packet[1] == 0x0c for packet in first.frames))
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            after_lobby = sum(packet[1] == 0x0c for packet in first.frames)
            wait(lambda: sum(packet[1] == 0x0c for packet in first.frames) > after_lobby)
            second = n.peer('Second', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 2)
            second.sock.sendall(gameplay.frame(0x23))
            wait(lambda: any(packet[1] == 0x0c for packet in second.frames))
            after_second_start = sum(packet[1] == 0x0c for packet in first.frames)
            wait(lambda: sum(packet[1] == 0x0c for packet in first.frames) > after_second_start)
            self.assertFalse(first.disconnected)
        finally:
            for peer in n.peers:
                peer.close()
            n.stop()

    def test_limit_config_fails_closed(self):
        for raw, expected in [(None, 1), ('1', 1), ('2', 2), ('3', 3), ('4', 4), ('5', 5),
                              ('0', 1), ('6', 1), ('-1', 1), ('2junk', 1),
                              ('9999999999999999999999', 1)]:
            with self.subTest(raw=raw):
                n = self.native({} if raw is None else {'bot_maxgames': raw})
                try:
                    n.start()
                    self.assertEqual(n.host_state()['max_games'], expected)
                finally:
                    n.stop()

    def test_default_one_blocks_next_lobby_until_game_ends(self):
        n = self.native({'autohost_maxgames': '5', 'autohost_startplayers': '6',
                         'autohost_gamename': 'Same map', 'autohost_owner': 'Owner'})
        try:
            n.start().create()
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            n.command('trycreate')
            n.command('autohost')
            self.assertIsNone(n.host_state()['lobby'])
            self.assertFalse(first.disconnected)
            first.close()
            wait(lambda: n.host_state()['active_games'] == 0, 8)
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
        finally:
            for peer in n.peers:
                peer.close()
            n.stop()

    def test_five_running_games_are_admitted_and_sixth_is_blocked(self):
        n = self.native({'bot_maxgames': '5', 'autohost_maxgames': '5',
                         'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                         'autohost_owner': 'Owner'})
        try:
            n.start().create()
            counters = []
            for i in range(5):
                counters.append(n.host_state()['lobby']['host_counter'])
                peer = n.peer('P' + str(i), game_keepalive=True)
                n.command('startlobby')
                wait(lambda: n.host_state()['active_games'] == i + 1)
                peer.sock.sendall(gameplay.frame(0x23))
                wait(lambda: peer.game_keepalives_sent > 0)
                if i < 4:
                    n.command('autohost')
                    wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(len(set(counters)), 5)
            n.command('trycreate')
            n.command('autohost')
            self.assertIsNone(n.host_state()['lobby'])
            self.assertTrue(all(not peer.disconnected for peer in n.peers))
        finally:
            for peer in n.peers:
                peer.close()
            n.stop()

    def test_second_lobby_cap_and_player_survival(self):
        n = self.native({'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '6', 'autohost_gamename': 'Same map',
                         'autohost_owner': 'Owner'})
        try:
            n.start().create()
            n.proc.stdin.write('mapcheck\n'); n.proc.stdin.flush()
            self.assertEqual(n.marker('TEST MAP ISOLATED '), 'TEST MAP ISOLATED 1')
            n.marker('TEST COMMAND mapcheck')
            first_map = n.host_state()['lobby']['map_name']
            first_counter = n.host_state()['lobby']['host_counter']
            first = n.peer('First', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 1)
            first.sock.sendall(gameplay.frame(0x23))
            wait(lambda: first.game_keepalives_sent > 0)
            # The autohost scan is aged only in the test driver; creation is native.
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(n.host_state()['lobby']['map_name'], first_map)
            self.assertNotEqual(n.host_state()['lobby']['host_counter'], first_counter)
            second = n.peer('Second', game_keepalive=True)
            n.command('startlobby')
            wait(lambda: n.host_state()['active_games'] == 2)
            second.sock.sendall(gameplay.frame(0x23))
            wait(lambda: second.game_keepalives_sent > 0)
            before = first.game_keepalives_sent
            n.command('trycreate')
            n.command('autohost')
            time.sleep(.2)
            self.assertIsNone(n.host_state()['lobby'])
            wait(lambda: first.game_keepalives_sent > before)
            self.assertFalse(first.disconnected)
            second.close()
            wait(lambda: n.host_state()['active_games'] == 1, 8)
            n.command('autohost')
            wait(lambda: n.host_state()['lobby'] is not None)
            self.assertEqual(n.host_state()['lobby']['map_name'], first_map)
            self.assertFalse(first.disconnected)
        finally:
            for peer in n.peers:
                peer.close()
            n.stop()


if __name__ == '__main__':
    unittest.main()
