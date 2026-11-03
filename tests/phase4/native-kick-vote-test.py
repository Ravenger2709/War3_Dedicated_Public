#!/usr/bin/env python3
"""Offline actual native/W3GS kick voting; synthetic peers, no Warcraft engine.

Run with PYTHONPATH=/repo and optional WAR3_KICK_NATIVE_DRIVER pointing at the
reviewed test driver. Driver hooks age timers/clear chat throttling only; votes,
leaves, loading acknowledgements, and action packets traverse real W3GS sockets.
"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import struct
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kick_gameplay", ROOT / "tests/phase3/native-test.py")
gameplay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gameplay)
wait = gameplay.wait


class KickNative(gameplay.Native):
    def start(self, ready=True):
        # Keep the shared fixture untouched and select only its test executable.
        driver = os.environ.get("WAR3_KICK_NATIVE_DRIVER", str(ROOT / "build/native-driver"))
        original = gameplay.base.subprocess.Popen
        def spawn(argv, **kwargs):
            return original([driver, *argv[1:]], **kwargs)
        with patch.object(gameplay.base.subprocess, "Popen", side_effect=spawn):
            return super().start(ready)

    def kick(self, index=0):
        command = f"kickstate {index}"
        self.proc.stdin.write(command + "\n"); self.proc.stdin.flush()
        value = self.marker("TEST KICKSTATE ")
        self.marker("TEST COMMAND " + command)
        return json.loads(value[len("TEST KICKSTATE "):])


class KickVoteTests(unittest.TestCase):
    setUp = gameplay.GameplayTests.setUp
    tearDown = gameplay.GameplayTests.tearDown

    def native(self, extra=None, **kwargs):
        n = KickNative(Path(self.temp.name) / f"n{len(self.children)}", {
            "bot_maxplayers": "12", "bot_maxgames": "3",
            "autohost_maxgames": "0", "bot_lobby_refresh_enabled": "false",
            **(extra or {}),
        }, **kwargs)
        self.children.append(n)
        return n.start().create()

    @contextmanager
    def session(self):
        n = self.native()
        try:
            yield n
        finally:
            for peer in n.peers:
                peer.close()
            if n.proc is not None and n.proc.poll() is None:
                n.stop()

    def peers(self, n, names):
        return [n.peer(name, game_keepalive=True) for name in names]

    def load(self, n, peers, count=1):
        n.command("startlobby")
        wait(lambda: n.host_state()["active_games"] == count)
        for peer in peers:
            peer.sock.sendall(gameplay.frame(0x23))
        wait(lambda: all(peer.game_keepalives_sent > 0 for peer in peers))
        wait(lambda: all(g["game_loaded"] for g in n.host_state()["games"]))

    def chat(self, n, peer, text, index=0):
        n.command(f"kickunlimit {index}")
        if index < 0:
            peer.chat(text)
        else:
            # Warcraft's in-game CHAT_TO_HOST uses flag32 plus extra flags.
            peer.sock.sendall(gameplay.frame(0x28, bytes((1, peer.pid, peer.pid, 32))
                              + struct.pack("<I", 0) + text.encode() + b"\0"))
            time.sleep(.06)
        return n.kick(index)

    def test_help_describes_commands_and_status_remains_concise(self):
        with self.session() as n:
            a=n.peer('Reader');n.command('unlimit');a.chat('!help')
            wait(lambda:any('!kick <player> - Start or support' in text for text in a.chats()))
            self.assertTrue(any('70%' in text for text in a.chats()))
            n.command('unlimit');before=len(a.chats());a.chat('!status')
            wait(lambda:any('Commands: !vote' in text for text in a.chats()[before:]))
            self.assertFalse(any('!kick <player> - Start or support' in text for text in a.chats()[before:]))

    def action(self, peer):
        before = len(peer.frames)
        # A distinguishable public synthetic action, echoed through CGame.
        payload = b"\x01"
        peer.sock.sendall(gameplay.frame(0x26, struct.pack("<I", 0) + payload))
        def echoed():
            for packet in peer.frames[before:]:
                if packet[1] != 0x0c:
                    continue
                offset = 8
                while offset + 3 <= len(packet):
                    pid, size = struct.unpack_from("<BH", packet, offset)
                    offset += 3
                    if pid == peer.pid and packet[offset:offset+size] == payload:
                        return True
                    offset += size
            return False
        wait(echoed)
        self.assertFalse(peer.disconnected)

    def hold_actions(self, *peers, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for peer in peers:
                self.action(peer)
            time.sleep(.05)

    def no_pass(self, *peers):
        self.assertFalse(any("Kick vote passed" in chat for p in peers for chat in p.chats()))

    def test_quorum_boundary_for_every_three_through_twelve_humans(self):
        # Explicit boundary examples catch rounding-down and target-inclusive
        # denominators; five eligible humans must need FOUR, not three.
        for humans, required in ((3, 2), (4, 3), (5, 3), (6, 4), (7, 5),
                                 (8, 5), (9, 6), (10, 7), (11, 7), (12, 8)):
            with self.subTest(humans=humans), self.session() as n:
                ps = self.peers(n, [f"Human{i}" for i in range(humans)])
                self.load(n, ps)
                target = ps[-1]
                state = self.chat(n, ps[0], f"!kick {target.name}")
                self.assertEqual((state["yes"], state["required"], state["eligible"]),
                                 (1, required, humans-1))
                self.chat(n, ps[0], f"!kick {target.name}")
                self.chat(n, target, "!yes")
                self.assertEqual(n.kick()["yes"], 1)
                for voter in ps[1:required-1]:
                    self.chat(n, voter, "!yes")
                self.assertFalse(target.disconnected)
                self.no_pass(*ps)
                self.action(ps[0])
                self.chat(n, ps[required-1], f"!kick {target.name}")
                wait(lambda: target.disconnected)
                wait(lambda: any(f"Kick vote passed for {target.name}." in c for c in ps[0].chats()))
                self.assertTrue(all(not p.disconnected for p in ps[:-1]))
                self.action(ps[0])

    def test_lobby_and_loading_reject_then_fully_loaded_session_accepts(self):
        n = self.native(); ps = self.peers(n, ["Alpha", "Bravo", "Target"])
        self.chat(n, ps[0], "!kick Target", -1)
        self.chat(n, ps[1], "!yes", -1)
        self.no_pass(*ps)
        self.assertFalse(n.kick(-1)["active"])
        n.command("startlobby")
        wait(lambda: n.host_state()["active_games"] == 1)
        ps[0].sock.sendall(gameplay.frame(0x23))
        self.chat(n, ps[0], "!kick Target")
        self.assertFalse(n.kick()["active"])
        self.no_pass(*ps)
        for p in ps[1:]: p.sock.sendall(gameplay.frame(0x23))
        wait(lambda: all(p.game_keepalives_sent > 0 for p in ps))
        self.chat(n, ps[0], "!kick Target")
        self.chat(n, ps[1], "!yes")
        wait(lambda: ps[2].disconnected)

    def test_countdown_and_fewer_than_three_humans_cannot_start_ballot(self):
        n = self.native({"bot_countdownseconds": "5"})
        ps = self.peers(n, ["Alpha", "Bravo", "Target"])
        ps[0].chat("!vote"); ps[1].chat("!vote")
        wait(lambda: n.state()["phase"] == "COUNTDOWN")
        self.chat(n, ps[0], "!kick Target", -1)
        self.assertFalse(n.kick(-1)["active"])
        self.no_pass(*ps)
        for p in ps: p.close()
        n.stop()
        n = self.native(); ps = self.peers(n, ["Alpha", "Target"])
        self.load(n, ps)
        self.chat(n, ps[0], "!kick Target")
        self.assertFalse(n.kick()["active"])
        self.assertTrue(any("at least 3 human players" in c for c in ps[0].chats()))
        self.assertFalse(ps[1].disconnected)
        self.no_pass(*ps)

    def test_names_self_ambiguity_exact_precedence_and_distinct_yes(self):
        n = self.native()
        ps = self.peers(n, ["Owner", "Bravo", "Victim", "VictimExtra", "Violet", "Delta"])
        self.load(n, ps)
        for command in ("!kick Owner", "!kick Missing", "!kick Vi", "!yes"):
            self.chat(n, ps[0], command)
            self.assertFalse(n.kick()["active"])
        # Exact match wins over the longer prefix; case-insensitive partial
        # commands identify the same target when voting again.
        self.chat(n, ps[0], "!kick vIcTiM")
        self.assertTrue(n.kick()["active"])
        for command in ("!yes", "!kick Victim", "!votekick Victim"):
            self.chat(n, ps[0], command)
        self.chat(n, ps[2], "!yes")
        self.chat(n, ps[1], "!kick Violet")
        self.chat(n, ps[1], "!kick victim")
        self.chat(n, ps[3], "!yes")
        self.no_pass(*ps)
        self.assertFalse(ps[2].disconnected)
        self.chat(n, ps[4], "!kick ictim")  # ambiguous; must not contribute
        self.no_pass(*ps)
        self.chat(n, ps[4], "!yes")
        wait(lambda: ps[2].disconnected)
        self.assertFalse(ps[3].disconnected)
        self.action(ps[0])
        self.assertTrue(n.kick()["cooling"])
        self.chat(n, ps[0], "!votekick Extra")
        self.assertFalse(n.kick()["active"])
        n.command("agekickcooldown 0 30")
        state = self.chat(n, ps[0], "!votekick Extra")
        self.assertEqual(state["target_pid"], ps[3].pid)
        self.assertEqual(state["yes"], 1)
        self.assertEqual(state["required"], 3)

    def test_computer_slots_do_not_count_as_voters_or_raise_quorum(self):
        n = self.native()
        ps = self.peers(n, ["Alpha", "Bravo", "Target"])
        n.command("computer"); n.command("computer")
        self.assertEqual(n.state()["computer_players"], 2)
        self.load(n, ps)
        state = self.chat(n, ps[0], "!kick Target")
        self.assertEqual((state["eligible"], state["required"]), (2, 2))
        self.assertEqual(set(state["human_pids"]), {p.pid for p in ps})
        self.assertFalse(ps[2].disconnected)
        self.no_pass(*ps)
        self.chat(n, ps[1], "!yes")
        wait(lambda: ps[2].disconnected)
        self.action(ps[0])

    def test_connected_human_observer_participates_but_computers_do_not(self):
        n = self.native(observers=True)
        ps = self.peers(n, ["Alpha", "Bravo", "Target", "Watcher"])
        n.command("spectator Watcher")
        n.command("computer")
        self.load(n, ps)
        state = self.chat(n, ps[0], "!kick Target")
        self.assertEqual((state["eligible"], state["required"]), (3, 3))
        self.chat(n, ps[1], "!yes")
        self.assertFalse(ps[2].disconnected)
        self.no_pass(*ps)
        self.chat(n, ps[3], "!yes")
        wait(lambda: ps[2].disconnected)
        self.action(ps[0])

    def test_voter_and_nonvoter_leave_cancel_instead_of_shrinking_quorum(self):
        for leaving in (1, 3):
            with self.subTest(leaving_voted=leaving == 1), self.session() as n:
                ps = self.peers(n, [f"Human{i}" for i in range(6)])
                self.load(n, ps)
                for p in ps[:3]: self.chat(n, p, "!kick Human5")
                self.no_pass(*ps)
                ps[leaving].close()
                wait(lambda: not n.kick()["active"])
                self.assertTrue(n.kick()["cooling"])
                self.chat(n, ps[4], "!yes")
                self.no_pass(*ps)
                self.assertFalse(ps[5].disconnected)
                self.assertTrue(any("cancelled because players joined or left" in c for c in ps[0].chats()))
                self.action(ps[0])
                n.command("agekickcooldown 0 30")
                state = self.chat(n, ps[0], "!kick Human5")
                self.assertEqual((state["yes"], state["required"]), (1, 3))
                self.chat(n, ps[4], "!yes")
                self.assertFalse(ps[5].disconnected)

    def test_target_exit_cancels_without_success_or_stale_yes(self):
        n = self.native(); ps = self.peers(n, [f"Human{i}" for i in range(6)])
        self.load(n, ps)
        self.chat(n, ps[0], "!kick Human5")
        ps[5].close()
        wait(lambda: not n.kick()["active"])
        self.chat(n, ps[1], "!yes")
        self.no_pass(*ps)
        self.assertTrue(any("cancelled because the target left" in c for c in ps[0].chats()))
        self.action(ps[0])

    def test_sixty_second_timeout_thirty_second_cooldown_and_new_ballot(self):
        n = self.native(); ps = self.peers(n, ["Alpha", "Bravo", "Charlie", "Target"])
        self.load(n, ps)
        self.chat(n, ps[0], "!kick Target")
        n.command("agekick 0 59")
        self.assertTrue(n.kick()["active"])
        n.command("agekick 0 60")
        wait(lambda: not n.kick()["active"])
        self.no_pass(*ps)
        self.assertTrue(any("Kick vote expired." in c for c in ps[0].chats()))
        self.chat(n, ps[0], "!kick Target")
        self.assertFalse(n.kick()["active"])
        n.command("agekickcooldown 0 29")
        self.chat(n, ps[0], "!kick Target")
        self.assertFalse(n.kick()["active"])
        n.command("agekickcooldown 0 30")
        self.chat(n, ps[0], "!kick Target")
        self.assertTrue(n.kick()["active"])
        self.chat(n, ps[1], "!yes")
        self.assertFalse(ps[3].disconnected)  # No carried votes from old ballot.
        self.chat(n, ps[2], "!yes")
        wait(lambda: ps[3].disconnected)

    def test_parallel_games_reused_pids_and_names_never_share_votes(self):
        n = self.native()
        first = self.peers(n, ["Alpha", "Bravo", "Target"])
        self.load(n, first)
        first_counter = n.host_state()["games"][0]["host_counter"]
        n.create()
        # A reconnect carrying the running game's namespace cannot join the
        # new lobby, even though that lobby reuses low player IDs.
        rejected = n.peer("StaleReconnect", counter_override=first_counter, accepted=False)
        wait(lambda: rejected.disconnected)
        second = self.peers(n, ["Alpha", "Bravo", "Target"])
        self.load(n, second, 2)
        self.assertEqual([p.pid for p in first], [p.pid for p in second])
        self.chat(n, first[0], "!kick Target", 0)
        self.chat(n, second[1], "!yes", 1)
        self.assertFalse(n.kick(1)["active"])
        self.no_pass(*first, *second)
        self.assertFalse(first[2].disconnected)
        self.hold_actions(first[0], second[0])
        self.chat(n, second[0], "!kick Target", 1)
        self.chat(n, first[0], "!yes", 0)  # duplicate only in game A
        self.assertFalse(first[2].disconnected)
        self.assertFalse(second[2].disconnected)
        self.chat(n, second[1], "!yes", 1)
        wait(lambda: second[2].disconnected)
        self.assertFalse(first[2].disconnected)
        self.action(first[0]); self.action(second[0])
        self.chat(n, first[1], "!yes", 0)
        wait(lambda: first[2].disconnected)
        self.action(first[0]); self.action(second[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
