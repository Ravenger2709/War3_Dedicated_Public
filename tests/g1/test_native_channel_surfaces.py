# SPDX-License-Identifier: Apache-2.0
"""Autonomous native protocol surfaces, never a Warcraft/JASS execution proof.

The synthetic peers send fixed zero keepalives and execute no game script.
No production source/configuration or public LAN service is changed.
"""
import importlib.util
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('surface_bridge', ROOT / 'tests/g1/native-bridge-test.py')
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
g = bridge.g


class NativeChannelSurfaces(unittest.TestCase):
    setUp = g.GameplayTests.setUp
    tearDown = g.GameplayTests.tearDown
    native = g.GameplayTests.native

    def lobby(self):
        n = self.native({'bot_g1_bridge_mode': '0'}).start().create()
        a = n.peer('First', lan=True, game_keepalive=True)
        b = n.peer('Second', game_keepalive=True)
        return n, a, b

    def loaded(self, n, peers):
        n.command('load')
        for peer in peers:
            peer.sock.sendall(g.frame(0x23))
        bridge.wait(lambda: n.state()['game_loaded'])

    def test_standard_server_text_is_a_text_packet_without_host_injected_actions(self):
        n, a, b = self.lobby()
        self.loaded(n, (a, b))
        before = len(a.frames)
        # Actual in-game CHAT_TO_HOST format, not a fabricated trigger action.
        a.sock.sendall(g.frame(0x28, bytes((1, a.pid, a.pid, 32)) + b'\0' * 4 + b'!help\0'))
        bridge.wait(lambda: any(p[1] == 0x0f and b'Commands: !vote' in p for p in a.frames[before:]))
        self.assertEqual(n.state()['players'], 2)
        self.assertFalse(a.disconnected or b.disconnected)
        incoming = [r for peer in (a, b) for p in peer.frames
                    if p[1] == 0x0c for r in bridge.action_records(p)]
        self.assertEqual(incoming, [])
        self.assertFalse(any(line.startswith('[G1]') for line in n.lines))
        # This says nothing about whether a real client would emit a JASS callback.

    def test_unslotted_virtual_lobby_host_is_removed_before_loading_finishes(self):
        n, a, b = self.lobby()
        real = {a.pid, b.pid}
        bridge.wait(lambda: any(p[1] == 0x06 and len(p) >= 10 and p[8] not in real for p in a.frames))
        virtual = {p[8] for p in a.frames if p[1] == 0x06 and len(p) >= 10} - real
        self.assertEqual(len(virtual), 1)
        virtual_pid = next(iter(virtual))
        slots = bytes(int(word, 16) for word in n.state()['slots_hex'].split())
        self.assertEqual(len(slots), 12 * 9)
        self.assertNotIn(virtual_pid, slots[::9])
        self.loaded(n, (a, b))
        for peer in (a, b):
            bridge.wait(lambda: any(p[1] == 0x07 and p[4] == virtual_pid for p in peer.frames))
            leave = next(i for i, p in enumerate(peer.frames) if p[1] == 0x07 and p[4] == virtual_pid)
            end = next(i for i, p in enumerate(peer.frames) if p[1] == 0x0b)
            self.assertLess(leave, end)
            self.assertEqual(struct.unpack_from('<I', peer.frames[leave], 5)[0], 13)
        self.assertEqual(n.state()['players'], 2)
        # Keeping an extra virtual participant alive is a different, unvalidated change.


if __name__ == '__main__':
    unittest.main()
