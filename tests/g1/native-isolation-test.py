#!/usr/bin/env python3
"""Two real CGame sessions with synthetic W3GS peers, never engine acceptance."""
import hashlib
import importlib.util
from pathlib import Path
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('g1_bridge_checks', ROOT / 'tests/g1/native-bridge-test.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
g = bridge.g
wait = g.wait


def received(peer, expected, sender=None):
    # This test keeps batches small enough for a normal 0x0c timeslot. Match
    # complete records and their CRC, never bytes buried in another payload.
    return any(payload == expected and (sender is None or pid == sender)
               for packet in peer.frames if packet[1] == 0x0c
               for pid, payload in bridge.action_records(packet))


def sender_pid(peer):
    slots = peer.slots()[-1]
    row = next(slots[i:i + 9] for i in range(0, len(slots), 9)
               if slots[i + 5] == 11)
    assert row[0] and row[2:4] == bytes((2, 0))
    assert row[0] != peer.pid
    return row[0]


class IsolationTests(unittest.TestCase):
    setUp = g.GameplayTests.setUp
    tearDown = g.GameplayTests.tearDown
    native = g.GameplayTests.native

    def test_same_process_sessions_require_their_own_probe_and_keep_sequences_separate(self):
        n = self.native({'bot_g1_bridge_mode': '3',
                         'bot_g1_receiver_binding': 'wire_probe',
                         'bot_g1_map_sha256': hashlib.sha256(b'').hexdigest(),
                         'bot_g1_receiver_a': '832', 'bot_g1_receiver_b': '832',
                         'bot_maxgames': '2', 'autohost_maxgames': '2',
                         'autohost_startplayers': '6', 'autohost_gamename': 'G1 isolation',
                         'autohost_owner': 'Owner'})
        n.start().create()
        first_epoch = n.host_state()['lobby']['host_counter']
        first = n.peer('FirstPublisher', game_keepalive=True)
        first_sender = sender_pid(first)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 1)
        first.sock.sendall(g.frame(0x23))
        wait(lambda: any(p[1] == 0x0c for p in first.frames))
        bridge.send(first, bridge.cache('wire_version', 2) + bridge.cache('tick', 8))
        probe1 = bridge.chat(f'~g1:{first_epoch}:0:8:0', 832, 832)
        wait(lambda: received(first, probe1, first_sender))
        bridge.send(first, bridge.cache('tick', 9) + bridge.cache('probe', first_epoch))
        wait(lambda: sum('receiver probe ACK confirmed' in s for s in n.lines) == 1)
        bridge.send(first, bridge.cache('tick', 16))
        command1 = bridge.chat(f'~g1:{first_epoch}:1:16:1', 832, 832)
        wait(lambda: received(first, command1, first_sender))
        bridge.send(first, bridge.cache('digest', 111) + bridge.cache('ack', 1))
        wait(lambda: any('digest=111' in s and '[G1] ack ' in s for s in n.lines))

        n.command('autohost')
        wait(lambda: n.host_state()['lobby'] is not None)
        second_epoch = n.host_state()['lobby']['host_counter']
        self.assertNotEqual(first_epoch, second_epoch)
        second = n.peer('SecondPublisher', game_keepalive=True)
        second_sender = sender_pid(second)
        n.command('startlobby')
        wait(lambda: n.host_state()['active_games'] == 2)
        second.sock.sendall(g.frame(0x23))
        wait(lambda: any(p[1] == 0x0c for p in second.frames))
        bridge.send(second, bridge.cache('wire_version', 2) + bridge.cache('tick', 8))
        probe2 = bridge.chat(f'~g1:{second_epoch}:0:8:0', 832, 832)
        wait(lambda: received(second, probe2, second_sender))
        # An identical receiver pair and a valid prior match ACK grant nothing here.
        bridge.send(second, bridge.cache('tick', 9) + bridge.cache('probe', first_epoch))
        time.sleep(.12)
        self.assertEqual(sum('receiver probe ACK confirmed' in s for s in n.lines), 1)
        self.assertFalse(received(second, command1))
        self.assertFalse(received(first, probe2))

        bridge.send(first, bridge.cache('tick', 24))
        first_next = bridge.chat(f'~g1:{first_epoch}:2:24:2', 832, 832)
        wait(lambda: received(first, first_next, first_sender))
        self.assertFalse(received(second, first_next))
        bridge.send(second, bridge.cache('probe', second_epoch))
        wait(lambda: sum('receiver probe ACK confirmed' in s for s in n.lines) == 2)
        bridge.send(second, bridge.cache('tick', 16))
        second_first = bridge.chat(f'~g1:{second_epoch}:1:16:1', 832, 832)
        wait(lambda: received(second, second_first, second_sender))
        self.assertFalse(received(first, second_first))
        bridge.send(second, bridge.cache('digest', 222) + bridge.cache('ack', 1))
        wait(lambda: any('digest=222' in s and f'host_counter={second_epoch}' in s for s in n.lines))
        first.close()
        bridge.send(second, bridge.cache('tick', 24))
        second_next = bridge.chat(f'~g1:{second_epoch}:2:24:2', 832, 832)
        wait(lambda: received(second, second_next, second_sender))
        self.assertFalse(second.disconnected)


if __name__ == '__main__':
    unittest.main()
