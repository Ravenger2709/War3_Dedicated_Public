# SPDX-License-Identifier: Apache-2.0
"""Offline invariants only; no engine, native process, socket, or delayed channel."""
import dataclasses
import json
import struct
import subprocess
import sys
import unittest
import zlib

import order_channel_model as model

A, B, OBSERVER, NONCE = 7, 19, 31, 37


def pair(round_id=0, reverse=False, nonce=NONCE):
    records = (model.Record(A, model.marker_payload(0, nonce, round_id)),
               model.Record(B, model.marker_payload(1, nonce, round_id)))
    return records[::-1] if reverse else records


def per_pid(records):
    return {pid: [r.payload for r in records if r.pid == pid] for pid in {r.pid for r in records}}


def decode_wire(packet):
    # Independent test decoder of the native normal-action wire layout.
    assert packet[:2] == b'\xf7\x0c'
    assert struct.unpack_from('<H', packet, 2)[0] == len(packet)
    tick = struct.unpack_from('<H', packet, 4)[0]
    if len(packet) == 6:
        return tick, []
    assert struct.unpack_from('<H', packet, 6)[0] == zlib.crc32(packet[8:]) & 0xffff
    records, offset = [], 8
    while offset < len(packet):
        pid, size = struct.unpack_from('<BH', packet, offset)
        offset += 3
        payload = packet[offset:offset + size]
        assert len(payload) == size
        records.append((pid, payload))
        offset += size
    assert offset == len(packet)
    return tick, records


class OrderChannelTests(unittest.TestCase):
    def channel(self):
        return model.OrderChannel((A, B), NONCE)

    def refused(self, ready, **kw):
        result = self.channel().apply(ready, kw.pop('round_id', 0), kw.pop('bit', 0), **kw)
        self.assertIs(result.records, ready)
        self.assertEqual(result.symbol, model.NO_SYMBOL)
        self.assertEqual(result.status, 'MODEL_ONLY_ENGINE_NOT_RUN')
        return result

    def test_status_is_explicit_in_module_and_result(self):
        out = subprocess.check_output([sys.executable, model.__file__], text=True)
        status = json.loads(out)
        self.assertEqual(status['status'], 'MODEL_ONLY_ENGINE_NOT_RUN')
        self.assertFalse(status['engine_run'])
        self.assertEqual(status['model_receiver_policy'], 'THIRD_GENUINE_OBSERVER')
        self.assertEqual(status['engine_requires_third_client'], 'UNVERIFIED')
        self.assertEqual(status['winner_timing_and_self_sync'], 'ASSUMPTION_UNVERIFIED')
        self.assertEqual(model.MAX_ROUNDS, 576)
        self.assertEqual(self.channel().apply(pair(), 0, 0).status, status['status'])

    def test_literal_sdk_integer_marker_is_exact(self):
        common = b'\x6bG1ORDER.w3v\0n00000025\0r000\0'
        self.assertEqual(model.marker_payload(0, NONCE, 0), common + b'\x01\0\0\0')
        self.assertEqual(model.marker_payload(1, NONCE, 0), common + b'\0\0\0\0')
        self.assertNotEqual(model.marker_payload(0, NONCE, 1), common + b'\x01\0\0\0')

    def test_both_bits_from_both_arrival_orders_keep_exact_objects(self):
        for reverse in (False, True):
            for bit in (0, 1):
                ready = pair(reverse=reverse)
                before = tuple((id(r), r.pid, r.payload) for r in ready)
                result = self.channel().apply(ready, 0, bit)
                self.assertEqual(result.symbol, bit)
                self.assertEqual(tuple(r.pid for r in result.records), (A, B) if bit == 0 else (B, A))
                self.assertEqual({id(r) for r in result.records}, {id(r) for r in ready})
                self.assertEqual(tuple((id(r), r.pid, r.payload) for r in ready), before)
                for record in result.records:
                    self.assertIs(record.payload, next(r.payload for r in ready if r.pid == record.pid))
                if reverse == bool(bit):
                    self.assertIs(result.records, ready)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            pair()[0].pid = OBSERVER

    def test_per_pid_fifo_and_every_tail_record_preserved(self):
        tail = (model.Record(A, b'\x01'), model.Record(B, b'\x02'),
                model.Record(OBSERVER, b'\x06ordinary\0'), model.Record(A, b'\x11' + bytes(22)))
        ready = pair() + tail
        result = self.channel().apply(ready, 0, 1)
        self.assertEqual(per_pid(result.records), per_pid(ready))
        for old, new in zip(tail, result.records[2:]):
            self.assertIs(old, new)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.symbol = 0

    def test_identical_recipient_wire_crc_tick_and_original_record_bytes(self):
        ready = pair() + (model.Record(A, b'\x01'), model.Record(OBSERVER, b'\x02'))
        result = self.channel().apply(ready, 0, 1)
        views = model.recipient_views(result.records, (A, B, OBSERVER), 250)
        expected_records = [(B, ready[1].payload), (A, ready[0].payload), (A, b'\x01'), (OBSERVER, b'\x02')]
        body = b''.join(struct.pack('<BH', pid, len(payload)) + payload for pid, payload in expected_records)
        expected = (b'\xf7\x0c' + struct.pack('<HHH', len(body) + 8, 250, zlib.crc32(body) & 0xffff) + body)
        for packet in views.values():
            self.assertIs(packet, views[A])
            self.assertEqual(packet, expected)
            self.assertEqual(decode_wire(packet), (250, expected_records))
        corrupted = expected[:-1] + bytes((expected[-1] ^ 1,))
        with self.assertRaises(AssertionError):
            decode_wire(corrupted)

    def test_empty_normal_tick_survives_no_symbol(self):
        ready = ()
        result = self.refused(ready)
        self.assertEqual(model.normal_packet(result.records, 100), b'\xf7\x0c\x06\0\x64\0')
        self.assertEqual(decode_wire(model.normal_packet((), 100)), (100, []))

    def test_missing_peer_and_same_pid_records_never_encode(self):
        a, b = pair()
        for ready in ((a,), (b,), (a, a), (model.Record(OBSERVER, b'x'),)):
            self.refused(ready)
        channel = self.channel()
        self.assertEqual(channel.apply((a,), 0, 1).symbol, model.NO_SYMBOL)
        self.assertEqual(channel.apply((a, b), 0, 1).symbol, model.NO_SYMBOL)
        next_round = pair(1)
        self.assertEqual(channel.apply(next_round, 1, 1).records, next_round[::-1])

    def test_conservative_observer_policy_uses_genuine_identity_without_spare_slot(self):
        channel = self.channel()
        for peers in ((A,), (A, B)):
            self.assertFalse(channel.observer_eligible(OBSERVER, peers))
        self.assertFalse(channel.observer_eligible(A, (A, B, OBSERVER)))
        self.assertFalse(channel.observer_eligible(B, (A, B, OBSERVER)))
        self.assertTrue(channel.observer_eligible(OBSERVER, (A, B, OBSERVER)))
        for peers in ((A, OBSERVER), (A, A, OBSERVER), (A, B, 255), tuple(range(1, 14))):
            self.assertFalse(channel.observer_eligible(OBSERVER, peers))

    def test_other_records_block_head_search_and_never_move(self):
        a, b = pair()
        ordinary = model.Record(A, b'\x01')
        for ready in ((ordinary, a, b), (a, ordinary, b), (ordinary, b, a)):
            self.refused(ready)
        # Marker-looking bytes inside an unrelated payload are never scanned.
        ready = (a, b, model.Record(OBSERVER, b'\x11' + a.payload))
        result = self.channel().apply(ready, 0, 1)
        self.assertEqual(result.symbol, 1)
        self.assertIs(result.records[-1], ready[-1])

    def test_coalesced_or_extended_marker_records_are_not_split(self):
        a, b = pair()
        for payload in (a.payload + b.payload, a.payload + b'\x01', b'\x01' + a.payload):
            self.refused((model.Record(A, payload), b))
        self.refused((a, model.Record(B, b.payload + b'\x02')))

    def test_wrong_round_nonce_cache_mission_or_value_never_matches(self):
        a, b = pair()
        variants = (pair(1), pair(nonce=NONCE + 1), (a, pair(1)[1]),
                    (model.Record(A, a.payload.replace(b'G1ORDER', b'g1order')), b),
                    (model.Record(A, a.payload.replace(b'n00000025', b'x00000025')), b),
                    (model.Record(A, a.payload[:-4] + bytes(4)), b))
        for ready in variants:
            self.refused(ready)

    def test_duplicate_or_backlogged_markers_decline_entire_batch(self):
        a, b = pair()
        for tail in (a, b, pair(1)[0], model.Record(OBSERVER, a.payload)):
            self.refused((a, b, tail))
        self.assertEqual(self.refused((a, b), backlog=True).reason, 'BACKLOG')

    def test_invalid_sender_configuration_and_nonce_rejected(self):
        for senders in ((A, A), (A,), (0, B), (A, 255), (True, B), [A, B]):
            with self.assertRaises(ValueError):
                model.OrderChannel(senders, NONCE)
        for nonce in (0, -1, True, '37', 0x80000000):
            with self.assertRaises(ValueError):
                model.OrderChannel((A, B), nonce)
        self.assertEqual(model.OrderChannel((1, 254), 0x7fffffff).nonce, 0x7fffffff)

    def test_invalid_or_mutable_records_decline_unchanged(self):
        a, b = pair()
        for ready in ([a, b], (a, object()), (a, model.Record(0, b.payload)),
                      (a, model.Record(True, b.payload)), (a, model.Record(B, bytearray(b.payload)))):
            self.refused(ready)

    def test_pause_consumes_round_without_holding_actions_or_retry(self):
        channel = self.channel()
        ready = pair()
        result = channel.apply(ready, 0, 1, running=False)
        self.assertIs(result.records, ready)
        self.assertEqual(result.symbol, model.NO_SYMBOL)
        self.assertEqual(channel.apply(ready, 0, 1).symbol, model.NO_SYMBOL)
        resumed = pair(1)
        self.assertEqual(channel.apply(resumed, 1, 1).records, resumed[::-1])

    def test_duplicate_and_out_of_order_rounds_decline_without_retries(self):
        channel = self.channel()
        self.assertEqual(channel.apply(pair(2), 2, 0).symbol, 0)
        for round_id in (2, 1, 0):
            ready = pair(round_id)
            result = channel.apply(ready, round_id, 1)
            self.assertIs(result.records, ready)
            self.assertEqual(result.symbol, model.NO_SYMBOL)
        self.assertEqual(channel.apply(pair(4), 4, 1).symbol, 1)

    def test_round_and_bit_bounds_are_finite(self):
        self.assertEqual(self.channel().apply(pair(575), 575, 1).symbol, 1)
        for round_id in (-1, 576, 10**20, True, '0'):
            self.refused(pair(), round_id=round_id)
        for bit in (-1, 2, True, None, '1'):
            self.refused(pair(), bit=bit)
        for args in ((2, NONCE, 0), (0, 0, 0), (0, NONCE, 576)):
            with self.assertRaises(ValueError):
                model.marker_payload(*args)

    def test_action_bound_accounts_for_native_three_byte_record_header(self):
        # Real CGame tests GetLength() <= 1027, including PID and uint16 length.
        valid = pair() + (model.Record(A, b'\x61' * 1024),)
        self.assertEqual(self.channel().apply(valid, 0, 1).symbol, 1)
        for size in (1025, 1026, 1027):
            ready = pair() + (model.Record(A, b'\x61' * size),)
            self.assertEqual(self.refused(ready).reason, 'ACTION_BOUND')

    def test_pressure_bounds_return_original_and_cannot_retry_round(self):
        head = pair()
        oversized = (head + (model.Record(A, b'x' * 1028),),
                     head + tuple(model.Record(A, b'\x01') for _ in range(31)),
                     head + (model.Record(A, b'\x01' * 1000), model.Record(B, b'\x02' * 500)))
        for ready in oversized:
            self.refused(ready)
        # Exact normal-packet capacity remains admissible, without overflow.
        remaining = 1452 - sum(3 + len(r.payload) for r in head) - 6
        exact = head + (model.Record(A, b'\x01' * 1000), model.Record(B, b'\x02' * (remaining - 1000)))
        result = self.channel().apply(exact, 0, 1)
        self.assertEqual(result.symbol, 1)
        self.assertEqual(len(model.normal_packet(result.records, 100)), 1460)
        channel = self.channel()
        channel.apply(oversized[0], 0, 0)
        self.assertEqual(channel.apply(head, 0, 0).symbol, model.NO_SYMBOL)

    def test_wire_bounds_cannot_create_overflow_or_private_recipient_views(self):
        for interval in (-1, 65536, True, '100'):
            with self.assertRaises(ValueError):
                model.normal_packet(pair(), interval)
        for pids in ((), (A, A), (A, 255), [A, B], tuple(range(1, 14))):
            with self.assertRaises(ValueError):
                model.recipient_views(pair(), pids, 100)
        with self.assertRaises(ValueError):
            model.normal_packet(pair() + (model.Record(A, b'x' * 1028),), 100)

    def test_message_codec_roundtrips_all_lengths_zero_through_64(self):
        for size in range(65):
            message = bytes(range(size))
            bits = model.frame_bits(message)
            self.assertLessEqual(len(bits), 576)
            self.assertEqual(model.unframe_bits(bits), message)
        self.assertEqual(len(model.frame_bits(b'')), 64)
        self.assertEqual(len(model.frame_bits(bytes(64))), model.MAX_ROUNDS)

    def test_every_single_bit_frame_corruption_is_detected(self):
        for message in (b'', b'order', bytes(range(64))):
            bits = model.frame_bits(message)
            for i in range(len(bits)):
                bad = bits[:i] + (1 - bits[i],) + bits[i + 1:]
                with self.assertRaises(ValueError):
                    model.unframe_bits(bad)

    def test_codec_rejects_missing_symbols_truncation_and_invalid_frames(self):
        for message in (bytes(65), bytearray(b'x'), 'text'):
            with self.assertRaises(ValueError):
                model.frame_bits(message)
        bits = model.frame_bits(b'abc')
        for bad in (bits[:-1], bits[1:], bits[:-8], bits + (0,) * 8, bits * 10,
                    (model.NO_SYMBOL,) + bits[1:], (True,) + bits[1:], list(bits), ()):
            with self.assertRaises(ValueError):
                model.unframe_bits(bad)
        # Valid CRC alone cannot authorize an invalid frame marker or length.
        for body in (b'BAD\0', b'G1O\x41' + bytes(65), b'G1O\x02abc'):
            frame = body + struct.pack('<I', zlib.crc32(body))
            forged_bits = tuple(int(bit) for byte in frame for bit in f'{byte:08b}')
            with self.assertRaises(ValueError):
                model.unframe_bits(forged_bits)


if __name__ == '__main__':
    unittest.main(verbosity=2)
