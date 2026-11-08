#!/usr/bin/env python3
"""Cross-only delivery through actual CGame, using synthetic W3GS peers.

This checks native records/CRC/timeslots/replay, not engine callback delivery.
Run only against a freshly built native-driver with cross_only support.
"""
import hashlib
import importlib.util
import struct
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'g1bridge', ROOT / 'tests/g1/native-bridge-test.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
g = bridge.g
wait, cache, chat, send = bridge.wait, bridge.cache, bridge.chat, bridge.send


def action_records(packet):
    """Validate both native wire formats; 0x48 has zero interval and a CRC."""
    assert packet[0] == 0xf7 and packet[1] in (0x0c, 0x48)
    assert struct.unpack_from('<H', packet, 2)[0] == len(packet)
    assert len(packet) >= 6
    if packet[1] == 0x0c:
        # Native empty final ticks are six bytes, with no CRC or records.
        assert len(packet) == 6 or len(packet) >= 11
        return bridge.action_records(packet)
    assert packet[4:6] == b'\0\0'
    assert len(packet) >= 11, 'empty overflow packet must be suppressed'
    assert struct.unpack_from('<H', packet, 6)[0] == zlib.crc32(packet[8:]) & 0xffff
    records, offset = [], 8
    while offset < len(packet):
        assert offset + 3 <= len(packet)
        pid, size = packet[offset], struct.unpack_from('<H', packet, offset + 1)[0]
        offset += 3
        assert offset + size <= len(packet)
        records.append((pid, packet[offset:offset + size]))
        offset += size
    assert offset == len(packet)
    return records


def packets(peer, start=0):
    return [p for p in peer.frames[start:] if p[1] in (0x0c, 0x48)]


def records(peer, start=0):
    return [record for packet in packets(peer, start) for record in action_records(packet)]


def action_frame(data):
    return g.frame(0x26, struct.pack('<I', zlib.crc32(data)) + data)


def replay_bytes(path):
    data = path.read_bytes()
    offset = struct.unpack_from('<I', data, 28)[0]
    blocks = struct.unpack_from('<I', data, 44)[0]
    raw = bytearray()
    for _ in range(blocks):
        compressed, expanded = struct.unpack_from('<HH', data, offset)
        offset += 8
        block = zlib.decompress(data[offset:offset + compressed])
        assert len(block) == expanded
        raw.extend(block)
        offset += compressed
    assert offset == len(data)
    return bytes(raw)


class CrossProbeTests(unittest.TestCase):
    # Reuse only the lifecycle fixture, without inheriting its other tests.
    setUp = g.GameplayTests.setUp
    tearDown = g.GameplayTests.tearDown
    native = g.GameplayTests.native

    def start_cross(self, third=False, latency=100):
        n = self.native({
            'bot_g1_bridge_mode': '4',
            'bot_g1_map_sha256': hashlib.sha256(b'').hexdigest(),
            'bot_g1_receiver_binding': 'wire_probe',
            'bot_g1_receiver_a': '821', 'bot_g1_receiver_b': '821',
            'bot_g1_secondary_receiver_a': '822', 'bot_g1_secondary_receiver_b': '822',
            'bot_g1_dual_delivery': 'cross_only',
            'bot_latency': str(latency), 'bot_savereplays': '1',
        })
        replay = n.root / 'replays'
        replay.mkdir()
        n.cfg.write_text(n.cfg.read_text() + f'bot_replaypath = {replay}/\n')
        n.start().create()
        a = n.peer('First', lan=True, game_keepalive=True)
        b = n.peer('Second', game_keepalive=True)
        peers = [a, b]
        if third:
            peers.append(n.peer('Third', game_keepalive=True))
        n.command('load')
        for peer in peers:
            peer.sock.sendall(g.frame(0x23))
        wait(lambda: n.state()['game_loaded'])
        return n, peers

    def expected_probes(self, n, peers):
        epoch = n.state()['host_counter']
        return [(peers[0].pid, chat(f'~g1:{epoch}:0:8:0', 821, 821)),
                (peers[1].pid, chat(f'~g1:{epoch}:0:8:0', 822, 822))]

    def queue_probes(self, n, peers):
        originals = [(peer.pid, cache('wire_version', 3) + cache('tick', 8)
                      + cache('original_' + str(i), 101 + i))
                     for i, peer in enumerate(peers[:2])]
        for peer, (_, payload) in zip(peers, originals):
            send(peer, payload)
        probes = self.expected_probes(n, peers)
        wait(lambda: all(probes[1 - i] in records(peer)
                         for i, peer in enumerate(peers[:2])))
        if len(peers) > 2:
            wait(lambda: all(probe in records(peers[2]) for probe in probes))
        self.assert_delivery(peers, probes, originals)
        return probes, originals

    def assert_delivery(self, peers, probes, originals):
        for i, peer in enumerate(peers):
            observed = records(peer)
            for original in originals:
                self.assertEqual(observed.count(original), 1)
            expected = [probes[1 - i]] if i < 2 else probes
            generated = [r for r in observed if r[1].startswith(b'\x60')
                         and b'~g1:' in r[1]]
            self.assertCountEqual(generated, expected)
            self.assertFalse(peer.disconnected)

    def assert_no_actors(self, n):
        self.assertFalse(any('POSITIVE CONTROL' in line or '[G1] fixed command queued' in line
                             for line in n.lines))
        self.assertIsNone(n.proc.poll())

    def ack(self, n, peer, colour):
        # Emulate the exports from receiving only the other peer's probe.
        payload = (cache('tick', 9) + cache('raw0', 0 if colour == 0 else 1)
                   + cache('raw1', 1 if colour == 0 else 0)
                   + cache('accepted_probe', 1) + cache('probe', n.state()['host_counter']))
        send(peer, payload)
        return peer.pid, payload

    def confirm(self, n, peers):
        first = self.ack(n, peers[0], 0)
        wait(lambda: sum('dual probe ACK pid=' in line for line in n.lines) == 1)
        self.assertFalse(any('dual probe confirmed by both peers' in line for line in n.lines))
        second = self.ack(n, peers[1], 1)
        wait(lambda: any('dual probe confirmed by both peers' in line for line in n.lines))
        wait(lambda: all(first in records(peer) and second in records(peer) for peer in peers))
        self.assertEqual(sum('dual probe ACK pid=' in line for line in n.lines), 2)
        self.assert_no_actors(n)

    def test_cross_sources_preserve_all_originals_and_require_both_acks(self):
        n, peers = self.start_cross()
        probes, originals = self.queue_probes(n, peers)
        self.confirm(n, peers)
        ticks = [(peer.pid, cache('tick', 100)) for peer in peers]
        for peer, (_, payload) in zip(peers, ticks):
            send(peer, payload)
        wait(lambda: all(all(tick in records(peer) for tick in ticks) for peer in peers))
        self.assert_delivery(peers, probes, originals)
        self.assertFalse(any('dual probe failed' in line for line in n.lines))
        self.assert_no_actors(n)

    def test_third_recipient_receives_both_probes_and_every_original(self):
        n, peers = self.start_cross(third=True)
        ordinary = peers[2].pid, cache('observer_original', 303)
        send(peers[2], ordinary[1])
        wait(lambda: all(ordinary in records(peer) for peer in peers))
        probes, originals = self.queue_probes(n, peers)
        wait(lambda: all(probe in records(peers[2]) for probe in probes))
        self.assert_delivery(peers, probes, originals + [ordinary])
        self.confirm(n, peers)
        self.assert_delivery(peers, probes, originals + [ordinary])

    def test_later_flushes_do_not_filter_ordinary_records_from_probe_senders(self):
        n, peers = self.start_cross()
        probes, originals = self.queue_probes(n, peers)
        self.confirm(n, peers)
        # Repeated allocation/free cycles exercise stale pointer labels. We do
        # not assume a particular allocator must reuse a particular address.
        for turn in range(8):
            later = [(peer.pid, cache('later_' + str(i), turn))
                     for i, peer in enumerate(peers)]
            for peer, (_, payload) in zip(peers, later):
                send(peer, payload)
            wait(lambda: all(all(record in records(peer) for record in later) for peer in peers))
            for peer in peers:
                for record in later:
                    self.assertEqual(records(peer).count(record), 1)
        self.assert_delivery(peers, probes, originals)
        self.assert_no_actors(n)

    def test_replay_keeps_both_canonical_injections_despite_recipient_filtering(self):
        n, peers = self.start_cross()
        probes, originals = self.queue_probes(n, peers)
        self.confirm(n, peers)
        self.assert_delivery(peers, probes, originals)
        for peer in peers:
            peer.close()
        n.stop()
        replays = list((n.root / 'replays').glob('*.w3g'))
        self.assertEqual(len(replays), 1)
        raw = replay_bytes(replays[0])
        for pid, payload in probes + originals:
            record = bytes((pid,)) + struct.pack('<H', len(payload)) + payload
            self.assertEqual(raw.count(record), 1)

    def test_overflow_filters_probes_and_rebuilds_crc_preserving_final_tick(self):
        n, peers = self.start_cross(latency=500)
        a, b = peers
        first = a.pid, cache('wire_version', 3) + cache('tick', 8)
        send(a, first[1])
        wait(lambda: all(first in records(peer) for peer in peers))
        # Start at a fresh native tick. Two separately valid <1027-byte actions
        # plus metadata/probes exceed the native 1452-byte aggregation budget.
        count = len(packets(b))
        wait(lambda: len(packets(b)) > count)
        starts = [len(peer.frames) for peer in peers]
        second = b.pid, cache('wire_version', 3) + cache('tick', 8)
        large = [(b.pid, b''.join(cache(f'pad_{i:02}', value) for i in range(40)))
                 for value in (401, 402)]
        self.assertTrue(all(900 <= len(payload) <= 1027 for _, payload in large))
        b.sock.sendall(b''.join(action_frame(payload) for _, payload in [second] + large))
        probes = self.expected_probes(n, peers)
        wait(lambda: all(all(record in records(peer, start) for record in large)
                         for peer, start in zip(peers, starts)))
        for i, (peer, start) in enumerate(zip(peers, starts)):
            wire = packets(peer, start)
            overflow = [p for p in wire if p[1] == 0x48]
            self.assertTrue(overflow, 'large actions must exercise native overflow')
            flattened = [r for p in wire for r in action_records(p)]
            for original in [second] + large:
                self.assertEqual(flattened.count(original), 1)
            self.assertIn(probes[1 - i], [r for p in overflow for r in action_records(p)])
            self.assertNotIn(probes[i], flattened)
            last_overflow = max(j for j, p in enumerate(wire) if p[1] == 0x48)
            self.assertTrue(any(p[1] == 0x0c for p in wire[last_overflow + 1:]))
            for packet in wire:
                self.assertLessEqual(len(packet), 1460)
                self.assertEqual(struct.unpack_from('<H', packet, 4)[0],
                                 0 if packet[1] == 0x48 else 500)
        self.assert_delivery(peers, probes, [first, second] + large)
        self.confirm(n, peers)


if __name__ == '__main__':
    unittest.main(verbosity=2)
