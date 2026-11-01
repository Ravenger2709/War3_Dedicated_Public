"""Synthetic Star relay regressions; run only in the isolated CI container.

No Star installation, saved profile, credentials, native process or network
connection is used. Wire fixtures are constructed independently of the relay.
"""
import asyncio
from collections import deque
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.bridge.bncs import Frame, FrameError
from src.bridge import star_relay as relay_module
from src.bridge.star_relay import (
    RelayError, Settings, StarRelay, listed_game, owned_file, publication_intent,
)


USERNAME = "SyntheticUser"
GAME = b"Synthetic Lobby"
HOST_COUNTER = 0x12345
WIRE_COUNTER = 0xF0000000 | HOST_COUNTER
PORT = 16113
OPAQUE = b"SYNTHETIC-PRIVATE-MARKER\x00\xff\x5e\x04\x00\x80\xfe"


def frame_bytes(sid, payload=b""):
    return struct.pack("<BBH", 255, sid, len(payload) + 4) + payload


def game_stats(counter=WIRE_COUNTER):
    return b"8" + f"{counter:08x}".encode()[::-1] + b"SYNTHETIC-MAP-STATS"


def advertisement(name=GAME, counter=WIRE_COUNTER):
    return frame_bytes(0x1C, struct.pack("<5I", 16, 0, 1, 0, 0)
                       + name + b"\0\0" + game_stats(counter) + b"\0")


def intent_object(raw=None, **changes):
    obj = {"version": 1, "pid": 1234, "generation": 7, "sequence": 1,
           "host_counter": HOST_COUNTER, "operation": "publish",
           "packet_hex": (advertisement() if raw is None else raw).hex()}
    obj.update(changes)
    return obj


def intent_bytes(raw=None, **changes):
    return json.dumps(intent_object(raw, **changes)).encode()


def identity(name=USERNAME):
    return Frame(0x0A, name.encode() + b"\0synthetic-stats\0synthetic-account\0")


def chat_event(event, text=b"Synthetic Channel"):
    return Frame(0x0F, struct.pack("<6I", event, 0, 0, 0, 0, 0)
                 + b"synthetic\0" + text + b"\0")


def game_record(name=GAME, port=PORT, counter=WIRE_COUNTER, status=16,
                password=b"", family=2, padding=b"\0" * 8):
    address = struct.pack("<H", family) + struct.pack(">H", port) + b"\xc0\x00\x02\x01" + padding
    return (struct.pack("<HHI", 1, 0, 0) + address + struct.pack("<II", status, 0)
            + name + b"\0" + password + b"\0" + game_stats(counter) + b"\0")


def game_list(*records):
    return struct.pack("<I", len(records)) + b"".join(records)


class ChunkReader:
    def __init__(self, chunks, selector=b"\x01"):
        self.chunks = deque(chunks)
        self.selector = selector

    async def readexactly(self, size):
        if len(self.selector) != size:
            raise asyncio.IncompleteReadError(self.selector, size)
        return self.selector

    async def read(self, size):
        chunk = self.chunks.popleft() if self.chunks else b""
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]


class CaptureWriter:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ConnectionError("synthetic closed writer")
        self.chunks.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    @property
    def data(self):
        return b"".join(self.chunks)


class RelayFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="star-relay-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.relay = StarRelay(Settings(
            "127.0.0.1", 16112, USERNAME, self.root / "intent.json",
            self.root / "status.json", game_port=PORT, publish=True,
            listen_port=16114,
        ))
        self.writer = CaptureWriter()
        self.relay.upstream = self.writer

    def authenticate(self):
        self.relay.connected = True
        self.relay.original_enterchat_packet = frame_bytes(0x0A, b'\0\0')
        self.relay.original_join_channel = frame_bytes(0x0C, struct.pack('<I', 2) + b'Synthetic Channel\0')
        self.relay.inspect_server(identity())
        self.relay.inspect_server(chat_event(7))

    async def confirm_chat_return(self,now):
        with patch.object(relay_module.time,'monotonic',return_value=now):
            frame=identity()
            self.relay.inspect_server(frame)
            self.assertTrue(await self.relay.chat_return_response(frame))
            frame=chat_event(7)
            self.relay.inspect_server(frame)
            await self.relay.chat_return_response(frame)

    def write_intent(self, **changes):
        self.relay.cfg.intent.write_bytes(intent_bytes(**changes))
        self.relay.cfg.intent.chmod(0o600)

    async def tick(self, now=100):
        """Run one real intent-loop iteration with a deterministic clock."""
        async def finish_iteration():
            self.relay.stop.set()
        self.relay.stop.clear()
        with patch.object(relay_module.time, "monotonic", return_value=now), \
                patch.object(self.relay.stop, "wait", finish_iteration):
            await self.relay.intents()

    async def publish(self, now=100, **changes):
        self.authenticate()
        self.write_intent(**changes)
        await self.tick(now)

    def acknowledge(self, code=0):
        return self.relay.inspect_server(Frame(0x1C, struct.pack("<I", code)))


class IntentValidationTests(unittest.TestCase):
    def test_accepts_only_native_publication_and_exact_empty_withdrawal(self):
        obj, raw, name = publication_intent(intent_bytes())
        self.assertEqual((raw, name, obj["host_counter"]), (advertisement(), GAME, HOST_COUNTER))
        _, raw, name = publication_intent(intent_bytes(frame_bytes(2), operation="unpublish"))
        self.assertEqual((raw, name), (b"\xff\x02\x04\x00", None))

    def test_native_startup_withdrawal_accepts_zero_counter_without_publication(self):
        obj,raw,name=publication_intent(intent_bytes(raw=frame_bytes(2),operation='unpublish',host_counter=0))
        self.assertEqual(obj['host_counter'],0)
        self.assertIsNone(name)
        self.assertEqual(raw,frame_bytes(2))
        with self.assertRaises(ValueError):publication_intent(intent_bytes(host_counter=0))

    def test_every_nonpublication_packet_id_including_auth_and_guard_is_rejected(self):
        for sid in range(256):
            if sid == 0x1C:
                continue
            raw = bytearray(advertisement())
            raw[1] = sid
            with self.subTest(sid=sid), self.assertRaises(RelayError):
                publication_intent(intent_bytes(bytes(raw)))

    def test_auth_cannot_be_smuggled_as_a_second_frame_or_withdrawal(self):
        for raw, operation in (
            (advertisement() + frame_bytes(0x53, OPAQUE), "publish"),
            (frame_bytes(0x5E, OPAQUE), "unpublish"),
            (frame_bytes(2, b"extra"), "unpublish"),
            (frame_bytes(2) + frame_bytes(0x51, OPAQUE), "unpublish"),
        ):
            with self.subTest(operation=operation, length=len(raw)), self.assertRaises(RelayError):
                publication_intent(intent_bytes(raw, operation=operation))

    def test_schema_and_all_numeric_fields_are_strict(self):
        valid = intent_object()
        bad = [dict(valid, extra="synthetic"), {k: v for k, v in valid.items() if k != "pid"}]
        for field in ("version", "pid", "generation", "sequence", "host_counter"):
            for value in (True, False, 0, -1, "1", 1.0, None, 2**64):
                bad.append(dict(valid, **{field: value}))
        bad.extend((dict(valid, version=2), dict(valid, pid=2**31),
                    dict(valid, host_counter=2**28), dict(valid, operation="authenticate")))
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises((RelayError, ValueError)):
                publication_intent(json.dumps(obj))

    def test_nondictionary_json_is_rejected_with_a_caught_validation_error(self):
        for obj in (None, 123, True, [], [1], "synthetic"):
            with self.subTest(obj=obj), self.assertRaises((RelayError, ValueError)):
                publication_intent(json.dumps(obj))

    def test_wrong_counter_oversize_bad_hex_headers_and_lengths_are_rejected(self):
        bad_hex = (None, 7, "zz", "f", "", "00", "00" * 513)
        for value in bad_hex:
            with self.subTest(value=value), self.assertRaises((RelayError, ValueError)):
                publication_intent(intent_bytes(packet_hex=value))
        valid = advertisement()
        for raw in (b"\xfe" + valid[1:], valid[:2] + b"\x04\x00" + valid[4:],
                    advertisement(counter=WIRE_COUNTER + 1),
                    frame_bytes(0x1C, valid[4:] + b"x" * 513)):
            with self.subTest(length=len(raw)), self.assertRaises(RelayError):
                publication_intent(intent_bytes(raw))

    def test_nonpublic_state_password_empty_long_name_and_bad_stats_are_rejected(self):
        valid = advertisement()
        invalid = [valid[:4] + struct.pack("<I", 0) + valid[8:], advertisement(b""),
                   advertisement(b"x" * 32),
                   frame_bytes(0x1C, valid[4:24] + GAME + b"\0password\0" + game_stats() + b"\0"),
                   frame_bytes(0x1C, valid[4:24] + GAME + b"\0\0" + game_stats()),
                   frame_bytes(0x1C, valid[4:24] + GAME + b"\0\0" + b"8zzzzzzzzstats\0"),
                   frame_bytes(0x1C, valid[4:] + b"\0")]
        for raw in invalid:
            with self.subTest(length=len(raw)), self.assertRaises(RelayError):
                publication_intent(intent_bytes(raw))


class ListParsingTests(unittest.TestCase):
    def test_pvpgn_open_record_differs_from_empty_list_started_status(self):
        self.assertEqual(listed_game(game_list(game_record(status=4)),GAME,WIRE_COUNTER,PORT),(True,None))
        self.assertEqual(listed_game(struct.pack('<II',0,4),GAME,WIRE_COUNTER,PORT),(False,4))
        for status in (6,12,14,17,18):
            self.assertEqual(listed_game(game_list(game_record(status=status)),GAME,WIRE_COUNTER,PORT),(False,None))
        private=bytearray(game_list(game_record(status=4)))
        struct.pack_into('<I',private,4,0x801)
        self.assertEqual(listed_game(private,GAME,WIRE_COUNTER,PORT),(False,None))

    def test_requires_exact_name_network_byte_order_port_and_full_counter(self):
        self.assertEqual(listed_game(game_list(game_record()), GAME, WIRE_COUNTER, PORT), (True, None))
        for change in ({"name": b"Other Lobby"}, {"name": GAME.lower()}, {"port": PORT + 1},
                       {"counter": WIRE_COUNTER + 1}, {"counter": HOST_COUNTER},
                       {"status": 0}, {"password": b"synthetic"}):
            with self.subTest(change=change):
                self.assertEqual(listed_game(game_list(game_record(**change)), GAME, WIRE_COUNTER, PORT), (False, None))

    def test_matches_any_record_without_accepting_an_unrelated_record(self):
        data = game_list(game_record(name=b"Other"), game_record(), game_record(port=PORT + 1))
        self.assertEqual(listed_game(data, GAME, WIRE_COUNTER, PORT), (True, None))

    def test_empty_list_preserves_server_status(self):
        for status in (0, 1, 2, 0xFFFFFFFF):
            self.assertEqual(listed_game(struct.pack("<II", 0, status), GAME, WIRE_COUNTER, PORT), (False, status))

    def test_truncated_trailing_oversized_and_bad_address_records_fail_closed(self):
        data = game_list(game_record())
        invalid = [data[:end] for end in range(len(data))]
        invalid += [data + b"extra", struct.pack("<I", 1001), struct.pack("<I", 0),
                    struct.pack("<III", 0, 0, 0), game_list(game_record(family=23)),
                    game_list(game_record(padding=b"x" * 8))]
        for payload in invalid:
            with self.subTest(length=len(payload)), self.assertRaises(RelayError):
                listed_game(payload, GAME, WIRE_COUNTER, PORT)

    def test_malformed_matching_counter_cannot_claim_visibility(self):
        payload = game_list(game_record()).replace(game_stats(), b"8zzzzzzzzSYNTHETIC")
        with self.assertRaisesRegex(RelayError, "RELAY_LIST_COUNTER"):
            listed_game(payload, GAME, WIRE_COUNTER, PORT)


class WireTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_client_auth_guard_unknown_frames_are_opaque_at_every_split(self):
        sids = (0x50, 0x51, 0x53, 0x54, 0x5E, 0x99, 0x00)
        stream = b"".join(frame_bytes(sid, OPAQUE) for sid in sids)
        for split in range(len(stream) + 1):
            self.writer.chunks.clear()
            await self.relay.client_to_server(ChunkReader([c for c in (stream[:split], stream[split:]) if c]))
            self.assertEqual(self.writer.data, b"\x01" + stream)
        self.writer.chunks.clear()
        await self.relay.client_to_server(ChunkReader([bytes([b]) for b in stream]))
        self.assertEqual(self.writer.data, b"\x01" + stream)
        self.assertFalse(self.relay.authenticated)
        self.assertFalse(self.relay.advert_pending)
        self.assertFalse(self.relay.query_pending)

    async def test_server_auth_guard_and_unowned_publication_responses_pass_unchanged(self):
        stream = b"".join(frame_bytes(sid, OPAQUE) for sid in (0x50, 0x51, 0x53, 0x54, 0x5E, 0x1C, 9, 0x99))
        for chunks in ([stream], [bytes([b]) for b in stream], [stream[:3], stream[3:71], stream[71:]]):
            output = CaptureWriter()
            await self.relay.server_to_client(ChunkReader(chunks), output)
            self.assertEqual(output.data, stream)

    async def test_maximum_sized_opaque_guard_frame_is_not_subject_to_intent_budget(self):
        guard = frame_bytes(0x5E, (OPAQUE * 2500)[:65531])
        self.assertEqual(len(guard), 65535)
        await self.relay.client_to_server(ChunkReader([guard]))
        self.assertEqual(self.writer.data, b"\x01" + guard)
        output = CaptureWriter()
        await self.relay.server_to_client(ChunkReader([guard]), output)
        self.assertEqual(output.data, guard)

    async def test_invalid_selector_never_opens_a_general_tcp_tunnel(self):
        for selector in (b"\x00", b"\x02", b"G", b"\xff"):
            with self.subTest(selector=selector), self.assertRaises(RelayError):
                await self.relay.client_to_server(ChunkReader([], selector=selector))
        self.assertEqual(self.writer.data, b"")

    async def test_truncated_frame_is_never_forwarded_as_a_complete_packet(self):
        partial = frame_bytes(0x5E, OPAQUE)[:-1]
        with self.assertRaises(FrameError):
            await self.relay.client_to_server(ChunkReader([partial]))
        self.assertEqual(self.writer.data, b"\x01")
        output = CaptureWriter()
        with self.assertRaises(FrameError):
            await self.relay.server_to_client(ChunkReader([partial]), output)
        self.assertEqual(output.data, b"")

    async def test_only_native_replies_are_consumed_in_a_coalesced_server_read(self):
        self.relay.generation = (1234, 7)
        self.relay.current = publication_intent(intent_bytes())
        await self.relay.send(frame_bytes(0x1C, b"synthetic-client"))
        await self.relay.send(advertisement(), (self.relay.generation, 1))
        await self.relay.send(frame_bytes(9, b"synthetic-client"))
        await self.relay.send(frame_bytes(9), (self.relay.generation, 1, GAME, WIRE_COUNTER, "directory"))
        client_ack = frame_bytes(0x1C, struct.pack("<I", 3))
        native_ack = frame_bytes(0x1C, struct.pack("<I", 0))
        client_list = frame_bytes(9, OPAQUE)
        native_list = frame_bytes(9, game_list(game_record()))
        guard = frame_bytes(0x5E, OPAQUE)
        output = CaptureWriter()
        await self.relay.server_to_client(ChunkReader([client_ack + native_ack + guard + client_list + native_list]), output)
        self.assertEqual(output.data, client_ack + guard + client_list)
        self.assertEqual(self.relay.native_acks, 1)
        self.assertTrue(self.relay.visible)

    async def test_gameport_rewrite_preserves_auth_guard_and_waits_for_acceptance(self):
        original = frame_bytes(0x45, struct.pack("<H", 6112))
        await self.relay.client_to_server(ChunkReader([original]))
        self.assertEqual(self.writer.data, b"\x01" + original)
        self.writer.chunks.clear()
        self.relay.advertised = True
        guard = frame_bytes(0x5E, OPAQUE)
        await self.relay.client_to_server(ChunkReader([guard + original]))
        self.assertEqual(self.writer.data, b"\x01" + guard + frame_bytes(0x45, struct.pack("<H", PORT)))


class PublicationTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_named_lookup_is_diagnostic_and_never_confirms_public_directory(self):
        await self.publish()
        self.acknowledge()
        await self.relay.send(frame_bytes(9), (self.relay.generation, 1, GAME, WIRE_COUNTER, 'named'))
        self.assertFalse(self.relay.inspect_server(Frame(9, game_list(game_record()))))
        self.assertTrue(self.relay.last_named_match['matching_title'])
        self.assertEqual(self.relay.last_named_match['listed_state'],16)
        self.assertFalse(self.relay.visible)
        self.assertTrue(self.relay.record()['exact_lookup_visible'])
        self.assertFalse(self.relay.record()['public_directory_seen_for_current_lobby'])

    async def test_directory_discovery_and_fresh_exact_lookup_are_separate_and_lobby_owned(self):
        await self.publish()
        self.acknowledge()
        await self.tick(101)
        with patch.object(relay_module.time,'monotonic',return_value=101):
            self.relay.inspect_server(Frame(9,game_list(game_record())))
            self.assertTrue(self.relay.record()['public_directory_seen_for_current_lobby'])
        self.write_intent(sequence=2);self.relay.last_sent=104
        await self.tick(104)
        with patch.object(relay_module.time,'monotonic',return_value=104):
            self.relay.inspect_server(Frame(9,game_list(game_record())))
            self.assertTrue(self.relay.record()['exact_lookup_visible'])
        # A later limited directory page need not contain this lobby. Its
        # confirmed discovery and independently fresh exact lookup stay distinct.
        await self.relay.send(frame_bytes(9),(self.relay.generation,2,GAME,WIRE_COUNTER,'directory'))
        with patch.object(relay_module.time,'monotonic',return_value=105):
            self.relay.inspect_server(Frame(9,game_list(game_record(name=b'Other'))))
            r=self.relay.record()
            self.assertFalse(r['visible_in_realm_query'])
            self.assertTrue(r['exact_lookup_visible'])
            self.assertTrue(r['public_directory_seen_for_current_lobby'])
        with patch.object(relay_module.time,'monotonic',return_value=162):
            self.assertFalse(self.relay.record()['public_directory_seen_for_current_lobby'])
        self.write_intent(raw=advertisement(name=b'Replacement'),sequence=3)
        await self.tick(106)
        self.assertFalse(self.relay.record()['public_directory_seen_for_current_lobby'])
        self.assertFalse(self.relay.record()['exact_lookup_visible'])

    async def test_identity_channel_connection_and_explicit_publish_are_all_required(self):
        for connected, identity_ok, channel_ok, enabled in (
            (False, True, True, True), (True, False, True, True),
            (True, True, False, True), (True, True, True, False),
        ):
            self.relay.connected = connected
            self.relay.identity = identity_ok
            self.relay.channel = channel_ok
            self.relay.cfg.publish = enabled
            self.write_intent(sequence=self.relay.sequence + 1)
            await self.tick()
            self.assertEqual(self.writer.data, b"")
        self.relay.connected = self.relay.identity = self.relay.channel = True
        self.relay.cfg.publish = True
        self.write_intent(sequence=self.relay.sequence + 1)
        await self.tick()
        self.assertEqual(self.writer.data, frame_bytes(0x45, struct.pack("<H", PORT)) + advertisement())
        self.assertFalse(self.relay.visible)
        self.assertFalse(self.relay.advertised)

    async def test_welcome_text_and_wrong_identity_do_not_authorize_publication(self):
        self.relay.connected = True
        self.relay.inspect_server(chat_event(0x12, ("Привет, " + USERNAME + "! Добро пожаловать").encode()))
        self.relay.inspect_server(identity("OtherSyntheticUser"))
        self.relay.inspect_server(chat_event(7))
        self.write_intent()
        await self.tick()
        self.assertTrue(self.relay.welcome)
        self.assertFalse(self.relay.authenticated)
        self.assertEqual(self.writer.data, b"")

    async def test_server_identity_and_channel_can_arrive_in_either_order(self):
        for frames in ((identity(), chat_event(7)), (chat_event(7), identity())):
            self.relay.connected = True
            self.relay.identity = self.relay.channel = False
            self.relay.inspect_server(frames[0])
            self.assertFalse(self.relay.authenticated)
            self.relay.inspect_server(frames[1])
            self.assertTrue(self.relay.authenticated)

    async def test_unterminated_enterchat_identity_cannot_authorize_publication(self):
        self.relay.connected = True
        self.relay.inspect_server(Frame(0x0A, USERNAME.encode()))
        self.relay.inspect_server(chat_event(7))
        self.write_intent()
        await self.tick()
        self.assertFalse(self.relay.authenticated)
        self.assertEqual(self.writer.data, b"")

    async def test_ack_is_acceptance_only_until_exact_realm_query_match(self):
        await self.publish()
        self.assertFalse(self.acknowledge())
        self.assertTrue(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        await self.tick(101)
        self.assertEqual(self.relay.queries, 1)
        self.assertEqual(self.writer.chunks[-1], frame_bytes(9, struct.pack("<4I", 0, 0, 0, 1000) + b"\0\0\0"))
        self.assertFalse(self.relay.inspect_server(Frame(9, game_list(game_record()))))
        status = self.relay.record()
        self.assertTrue(status["advertisement_accepted"])
        self.assertTrue(status["visible_in_realm_query"])

    async def test_rejected_ack_blocks_visibility_and_further_publication(self):
        await self.publish()
        self.acknowledge(2)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        self.assertTrue(self.relay.publication_failed)
        self.write_intent(sequence=2)
        await self.tick(103)
        self.assertEqual(self.relay.advertisements, 1)
        self.assertEqual(self.relay.queries, 0)
        self.assertEqual(self.relay.error, "RELAY_PUBLICATION_REJECTED")

    async def test_absent_mismatching_or_rejected_list_does_not_mean_visible(self):
        await self.publish()
        self.acknowledge()
        for now, payload in ((101, struct.pack("<II", 0, 1)),
                             (106, game_list(game_record(counter=WIRE_COUNTER + 1))),
                             (111, struct.pack("<II", 0, 2))):
            self.write_intent(sequence=self.relay.sequence + 1)
            # Preserve an accepted ad while driving separate list attempts.
            self.relay.last_sent = now
            await self.tick(now)
            self.assertFalse(self.relay.inspect_server(Frame(9, payload)))
            self.assertFalse(self.relay.visible)
        self.assertEqual(self.relay.error, "RELAY_LIST_REJECTED")

    async def test_replayed_sequence_does_not_refresh_native_heartbeat(self):
        await self.publish()
        self.acknowledge()
        await self.tick(111)
        self.assertIsNone(self.relay.current)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        self.assertEqual(self.relay.withdrawals, 1)
        self.assertEqual(self.writer.chunks[-1], b"\xff\x02\x04\x00")
        self.assertEqual(self.relay.error, "RELAY_NATIVE_HEARTBEAT_EXPIRED")

    async def test_fresh_heartbeat_prevents_expiry_and_publish_is_rate_limited(self):
        await self.publish()
        self.acknowledge()
        self.write_intent(sequence=2)
        await self.tick(101)
        self.assertEqual(self.relay.advertisements, 1)
        self.write_intent(sequence=3)
        await self.tick(109)
        self.assertEqual(self.relay.advertisements, 2)
        self.assertEqual(self.relay.last_native, 109)
        self.assertIsNotNone(self.relay.current)

    async def test_ack_timeout_withdraws_and_marks_publication_failed(self):
        await self.publish()
        with self.assertRaisesRegex(RelayError,"RELAY_PUBLICATION_ACK_TIMEOUT"):
            await self.tick(109)
        self.assertTrue(self.relay.publication_failed)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        self.assertEqual(self.relay.withdrawals, 1)
        self.assertEqual(self.relay.error, "RELAY_PUBLICATION_ACK_TIMEOUT")
        self.assertIsNone(self.relay.current)
        self.assertFalse(self.acknowledge())
        self.assertFalse(self.relay.advertised)

    async def test_query_timeout_never_claims_visibility(self):
        await self.publish()
        self.acknowledge()
        await self.tick(101)
        self.write_intent(sequence=2)
        self.relay.last_sent = 110
        with self.assertRaisesRegex(RelayError,"RELAY_GAME_QUERY_TIMEOUT"):
            await self.tick(110)
        self.assertFalse(self.relay.visible)
        self.assertEqual(self.relay.error, "RELAY_GAME_QUERY_TIMEOUT")
        self.assertIsNone(self.relay.current)
        self.assertFalse(self.relay.inspect_server(Frame(9,game_list(game_record()))))
        self.assertFalse(self.relay.visible)

    async def test_explicit_unpublish_does_not_republish_replayed_intent(self):
        await self.publish()
        self.acknowledge()
        self.write_intent(raw=frame_bytes(2), operation="unpublish", sequence=2)
        await self.tick(101)
        await self.tick(102)
        self.assertIsNone(self.relay.current)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        self.assertEqual(self.relay.withdrawals, 1)
        self.assertEqual(self.relay.advertisements, 1)

    async def test_observation_mode_never_sends_native_withdrawal_or_publication(self):
        self.authenticate()
        self.relay.cfg.publish = False
        self.write_intent()
        await self.tick()
        self.write_intent(generation=8)
        await self.tick(101)
        self.write_intent(raw=frame_bytes(2), operation="unpublish", generation=8, sequence=2)
        await self.tick(102)
        self.assertEqual(self.writer.data, b"")
        self.assertEqual(self.relay.withdrawals, 0)

    async def test_heartbeat_for_unchanged_lobby_does_not_invalidate_inflight_responses(self):
        await self.publish()
        self.write_intent(sequence=2)
        await self.tick(101)
        self.acknowledge()
        self.assertTrue(self.relay.advertised)
        await self.tick(102)
        self.write_intent(sequence=3)
        await self.tick(102.1)
        self.relay.inspect_server(Frame(9, game_list(game_record())))
        self.assertTrue(self.relay.visible)

    async def test_native_generation_change_before_auth_cannot_inject_withdrawal(self):
        self.relay.connected = True
        self.write_intent()
        await self.tick()
        self.write_intent(generation=8)
        await self.tick(101)
        self.assertEqual(self.writer.data, b"")
        self.assertEqual(self.relay.withdrawals, 0)

    async def test_late_ack_after_unpublish_cannot_resurrect_acceptance(self):
        await self.publish()
        self.write_intent(raw=frame_bytes(2), operation="unpublish", sequence=2)
        await self.tick(101)
        self.acknowledge()
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.record()["ever_advertisement_accepted"])

    async def test_late_ack_for_old_generation_cannot_accept_replacement(self):
        await self.publish()
        self.write_intent(generation=8, sequence=1)
        await self.tick(103)
        self.assertEqual(self.relay.advertisements, 1)
        self.acknowledge()
        self.assertFalse(self.relay.advertised)
        await self.confirm_chat_return(104)
        self.write_intent(generation=8, sequence=2)
        await self.tick(104)
        self.assertEqual(self.relay.advertisements, 2)
        self.acknowledge()
        self.assertTrue(self.relay.advertised)

    async def test_new_lobby_cannot_inherit_old_lobby_visibility(self):
        await self.publish()
        self.acknowledge()
        await self.tick(101)
        self.relay.inspect_server(Frame(9, game_list(game_record())))
        self.assertTrue(self.relay.visible)
        self.write_intent(raw=advertisement(name=b"Replacement", counter=WIRE_COUNTER + 1),
                          sequence=2, host_counter=HOST_COUNTER + 1)
        await self.tick(104)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)

    async def test_stale_query_for_previous_sequence_cannot_confirm_replacement(self):
        await self.publish()
        self.acknowledge()
        await self.tick(101)
        self.write_intent(raw=advertisement(name=b"Replacement"), sequence=2)
        await self.tick(104)
        self.acknowledge()
        self.relay.inspect_server(Frame(9, game_list(game_record())))
        self.assertFalse(self.relay.visible)

    async def test_rejected_native_auth_never_injects_any_frame(self):
        self.authenticate()
        self.write_intent(raw=frame_bytes(0x53, OPAQUE))
        await self.tick()
        self.assertEqual(self.writer.data, b"")
        self.assertEqual(self.relay.error, "RELAY_NATIVE_INTENT_REJECTED")
        self.assertIsNone(self.relay.current)


class SessionAndPrivacyTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_run_duration_withdraws_and_stops_worker_on_loopback_listener(self):
        self.authenticate()
        self.relay.current = publication_intent(intent_bytes())
        self.relay.advertised = self.relay.visible = True
        self.relay.cfg.duration = 0.001
        exits = []
        workers = []
        class FakeServer:
            async def __aenter__(inner):
                return inner
            async def __aexit__(inner, *args):
                exits.append(True)
        async def worker():
            workers.append(asyncio.current_task())
            await self.relay.stop.wait()
        async def start_server(callback, host, port, **kwargs):
            self.assertEqual(host, "127.0.0.1")
            self.assertEqual(port, 16114)
            return FakeServer()
        loop = asyncio.get_running_loop()
        with patch.object(relay_module.asyncio, "start_server", start_server), \
                patch.object(self.relay, "intents", worker), \
                patch.object(loop, "add_signal_handler"):
            result = await self.relay.run()
        self.assertEqual(exits, [True])
        self.assertTrue(workers and workers[0].done())
        self.assertTrue(self.relay.stop.is_set())
        self.assertEqual(self.writer.data, b"\xff\x02\x04\x00")
        self.assertFalse(result["advertisement_accepted"])
        self.assertFalse(result["visible_in_realm_query"])

    async def test_session_eof_revokes_all_current_auth_and_publication_claims(self):
        self.authenticate()
        self.relay.advertised = self.relay.visible = True
        self.relay.record()
        client = CaptureWriter()
        async def fake_connect(*args, **kwargs):
            return ChunkReader([]), self.writer
        with patch.object(relay_module.asyncio, "open_connection", fake_connect):
            await self.relay.session(ChunkReader([]), client)
        self.assertFalse(self.relay.connected)
        self.assertFalse(self.relay.authenticated)
        self.assertFalse(self.relay.identity)
        self.assertFalse(self.relay.channel)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)
        self.assertIsNone(self.relay.upstream)
        self.assertTrue(client.closed)
        self.assertTrue(self.writer.closed)
        status = self.relay.record()
        self.assertTrue(status["ever_authenticated"])
        self.assertTrue(status["ever_visible_in_realm_query"])
        self.assertFalse(status["visible_in_realm_query"])

    async def test_only_one_client_session_is_ever_allowed(self):
        self.relay.used = True
        rejected = CaptureWriter()
        with patch.object(relay_module.asyncio, "open_connection") as connect:
            await self.relay.session(ChunkReader([]), rejected)
        connect.assert_not_called()
        self.assertTrue(rejected.closed)
        self.assertEqual(self.relay.rejected_connections, 1)

    async def test_status_and_console_never_contain_identity_payload_intent_or_upstream(self):
        self.relay.cfg.expected_username = "SYNTHETIC-IDENTITY-NOT-FOR-STATUS"
        self.relay.cfg.upstream = "192.0.2.123"
        self.relay.current = publication_intent(intent_bytes())
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            await self.relay.client_to_server(ChunkReader([frame_bytes(0x5E, OPAQUE)]))
            await self.relay.server_to_client(ChunkReader([frame_bytes(0x53, OPAQUE)]), CaptureWriter())
            status = self.relay.record()
        encoded = self.relay.cfg.status.read_text() + output.getvalue() + errors.getvalue() + repr(self.relay.cfg)
        for private in ("SYNTHETIC-PRIVATE-MARKER", self.relay.cfg.expected_username,
                        self.relay.cfg.upstream, GAME.decode(), advertisement().hex(),
                        str(self.relay.cfg.intent)):
            self.assertNotIn(private, encoded)
        self.assertFalse(status["payloads_logged"])
        self.assertFalse(status["auth_or_guard_packets_constructed"])
        self.assertEqual(status["packet_counts"], {"client": {"0x5e": 1}, "server": {"0x53": 1}})
        self.assertEqual(self.relay.cfg.status.stat().st_mode & 0o777, 0o600)

    async def test_invalid_json_top_level_does_not_kill_intent_worker(self):
        self.relay.cfg.intent.write_text("null")
        self.relay.cfg.intent.chmod(0o600)
        await self.tick()
        self.assertEqual(self.relay.error, "RELAY_NATIVE_INTENT_REJECTED")
        self.assertEqual(self.writer.data, b"")


class PrivateInputTests(RelayFixture, unittest.TestCase):
    def test_only_bounded_owned_mode600_single_link_regular_inputs_are_read(self):
        self.write_intent()
        path = self.relay.cfg.intent
        self.assertEqual(owned_file(path), path.read_bytes())
        path.chmod(0o644)
        with self.assertRaises(RelayError):
            owned_file(path)
        path.chmod(0o600)
        link = self.root / "link.json"
        os.link(path, link)
        with self.assertRaises(RelayError):
            owned_file(path)
        link.unlink()
        link.symlink_to(path)
        with self.assertRaises(OSError):
            owned_file(link)
        path.write_bytes(b"x" * 4097)
        with self.assertRaises(RelayError):
            owned_file(path)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(RelayError):
            owned_file(fifo)


class StarSessionProofTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_original_star_proof_and_fresh_named_server_welcome_channel(self):
        self.relay.connected=True
        await self.relay.client_to_server(ChunkReader([frame_bytes(0x53,b'A'*32+USERNAME.encode()+b'\0')]))
        self.assertTrue(self.relay.requested_identity_matches)
        self.relay.inspect_server(Frame(0x54,struct.pack('<I',0)))
        self.relay.inspect_server(chat_event(7))
        self.assertFalse(self.relay.authenticated)
        self.relay.inspect_server(chat_event(0x12,('Привет, '+USERNAME+'! Добро пожаловать').encode()))
        self.assertFalse(self.relay.authenticated)
        self.relay.inspect_server(chat_event(0x12,'WarCraft 3 / DotA Сервер iCCup!'.encode()))
        self.assertFalse(self.relay.authenticated)
        self.relay.inspect_server(identity())
        self.assertTrue(self.relay.authenticated)

    async def test_bad_proof_or_user_chat_cannot_become_auth_evidence(self):
        self.relay.connected=True;self.relay.requested_identity_matches=True
        self.relay.inspect_server(Frame(0x54,struct.pack('<I',2)))
        self.relay.inspect_server(chat_event(7))
        self.relay.inspect_server(chat_event(0x12,('Привет, '+USERNAME+'! Добро пожаловать WarCraft 3 / DotA Сервер iCCup!').encode()))
        self.assertFalse(self.relay.authenticated)
        self.relay.welcome=self.relay.realm_welcome=False
        self.relay.inspect_server(Frame(0x54,struct.pack('<I',0)))
        self.relay.inspect_server(chat_event(5,('Привет, '+USERNAME+'! Добро пожаловать WarCraft 3 / DotA Сервер iCCup!').encode()))
        self.assertFalse(self.relay.authenticated)

    async def test_malformed_client_identity_cannot_authorize_and_opaque_status_is_redacted(self):
        await self.relay.client_to_server(ChunkReader([frame_bytes(0x53,b'A'*32+USERNAME.encode())]))
        self.assertFalse(self.relay.requested_identity_matches)
        self.relay.inspect_server(Frame(0x54,b'NONCE-IS-NOT-A-STATUS'))
        self.assertEqual(self.relay.server_proof_status,'OTHER')


class OriginalChatOwnershipTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_successful_original_proof_does_not_construct_enterchat_or_join(self):
        self.relay.connected=True;self.relay.requested_identity_matches=True
        self.relay.server_proof_status=0;self.relay.original_channel_list=True
        await self.tick(100)
        self.assertEqual(self.writer.data,b'')
        self.assertFalse(self.relay.authenticated)


class NativeGamePortOrderingTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_native_refresh_does_not_repeat_game_port_registration(self):
        await self.publish()
        self.acknowledge()
        self.write_intent(sequence=2)
        await self.tick(103)
        self.assertEqual(sum(c[1]==0x45 for c in self.writer.chunks),1)
        self.assertEqual(sum(c[1]==0x1c for c in self.writer.chunks),2)

    async def test_native_listen_port_is_sent_before_original_enterchat(self):
        self.relay.requested_identity_matches=True;self.relay.server_proof_status=0
        enter=frame_bytes(0x0a,b'\0\0')
        await self.relay.client_to_server(ChunkReader([enter]))
        self.assertEqual(self.writer.data,b'\1'+frame_bytes(0x45,struct.pack('<H',PORT))+enter)
        self.assertTrue(self.relay.netgameport_before_enterchat)
        self.assertFalse(self.relay.authenticated)

    def test_list_metadata_identifies_mismatch_without_exposing_game_data(self):
        diagnostic={}
        self.assertEqual(listed_game(game_list(game_record(port=PORT+1)),GAME,WIRE_COUNTER,PORT,diagnostic),(False,None))
        self.assertTrue(diagnostic['matching_title']);self.assertFalse(diagnostic['port_network_matches'])
        self.assertTrue(diagnostic['counter_matches']);self.assertTrue(diagnostic['public_state'])
        self.assertNotIn(GAME.decode(),json.dumps(diagnostic))

    def test_directory_position_counts_records_but_only_reports_our_exact_public_lobby(self):
        records=[game_record(name=b'Other game'),game_record(port=PORT+1),game_record(),game_record(counter=WIRE_COUNTER+1)]
        diagnostic={}
        self.assertEqual(listed_game(game_list(*records),GAME,WIRE_COUNTER,PORT,diagnostic),(True,None))
        self.assertEqual(diagnostic['matching_record_positions_1based'],[3])
        self.assertNotIn('Other game',json.dumps(diagnostic))
        self.assertNotIn(GAME.decode(),json.dumps(diagnostic))

    def test_nonmatching_or_private_lobbies_never_produce_position_evidence(self):
        for record in (game_record(port=PORT+1),game_record(counter=WIRE_COUNTER+1),
                       game_record(name=GAME.lower()),game_record(status=8)):
            with self.subTest(record=record):
                diagnostic={}
                self.assertEqual(listed_game(game_list(record),GAME,WIRE_COUNTER,PORT,diagnostic),(False,None))
                self.assertNotIn('matching_record_positions_1based',diagnostic)


class FreshVisibilityAndWorkerTests(RelayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_visible_game_is_requeried_and_expired_without_fresh_matches(self):
        await self.publish()
        self.acknowledge()
        await self.tick(101)
        with patch.object(relay_module.time,'monotonic',return_value=101):
            self.relay.inspect_server(Frame(9,game_list(game_record())))
        self.assertTrue(self.relay.visible)
        self.write_intent(sequence=2);self.relay.last_sent=106
        await self.tick(106)
        self.assertEqual(self.relay.queries,2)
        self.write_intent(sequence=3);self.relay.last_sent=110
        await self.tick(110)
        self.assertFalse(self.relay.visible)

    async def test_requested_stop_and_worker_completion_race_is_normal(self):
        class FakeServer:
            async def __aenter__(inner):return inner
            async def __aexit__(inner,*args):pass
        async def normal_worker():await self.relay.stop.wait()
        async def start_server(*args,**kwargs):return FakeServer()
        loop=asyncio.get_running_loop()
        with patch.object(self.relay,'intents',normal_worker), patch.object(relay_module.asyncio,'start_server',start_server), patch.object(loop,'add_signal_handler'):
            task=asyncio.create_task(self.relay.run())
            await asyncio.sleep(0.001);self.relay.stop.set()
            result=await task
        self.assertFalse(result['publication_failed'])
        self.assertIsNone(result['error'])

    async def test_failed_intent_worker_cannot_leave_live_successful_relay(self):
        class FakeServer:
            async def __aenter__(inner):return inner
            async def __aexit__(inner,*args):pass
        async def failed_worker():raise RuntimeError('SYNTHETIC-ERROR-MUST-NOT-BE-LOGGED')
        async def start_server(*args,**kwargs):return FakeServer()
        loop=asyncio.get_running_loop()
        with patch.object(self.relay,'intents',failed_worker), patch.object(relay_module.asyncio,'start_server',start_server), patch.object(loop,'add_signal_handler'):
            with self.assertRaisesRegex(RelayError,'RELAY_INTENT_WORKER_FAILED'):await self.relay.run()
        self.assertTrue(self.relay.stop.is_set())
        self.assertTrue(self.relay.publication_failed)
        self.assertEqual(self.relay.record()['error'],'RELAY_INTENT_WORKER_FAILED')
        self.assertNotIn('SYNTHETIC-ERROR',self.relay.cfg.status.read_text())


class GatewayPolicyTests(unittest.TestCase):
    def test_user_gateway_remains_selected_with_downloaded_official_site_list(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'launcher.ini'
            user=json.dumps('@ByteArray('+json.dumps({'CI-Relay':'127.0.0.1:16112'})+')')
            for site in ({'DE':'192.0.2.1'}, {'MAIN':'192.0.2.2','DE':'192.0.2.1'}):
                value=json.dumps('@ByteArray('+json.dumps(site)+')')
                profile.write_text('[General]\nConnections=\nCurrConnections=0\nUserConnections='+user+'\nSiteConnections='+value+'\n')
                profile.chmod(0o600)
                relay_module.require_loopback_profile(profile,16112)
            profile.write_text(profile.read_text().replace('127.0.0.1:16112','192.0.2.3:16112'))
            with self.assertRaises(ValueError):relay_module.require_loopback_profile(profile,16112)

    def test_explicit_gateway_port_matches_only_the_configured_relay_port(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'launcher.ini'
            value=json.dumps('@ByteArray('+json.dumps({'local':'127.0.0.1:16112'})+')')
            profile.write_text('[General]\nConnections=\nCurrConnections=0\nSiteConnections='+value+'\n');profile.chmod(0o600)
            relay_module.require_loopback_profile(profile,16112)
            with self.assertRaises(ValueError):relay_module.require_loopback_profile(profile,6112)

    def test_config_cannot_bypass_relay_via_selected_or_extra_gateway(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'launcher.ini'
            for addresses,connections,index,valid in [
                ({'local':'127.0.0.1'},'',0,True),
                ({'local':'127.0.0.1','remote':'192.0.2.1'},'',0,False),
                ({'remote':'192.0.2.1'},'',0,False),
                ({'local':'127.0.0.1'},'192.0.2.1',0,False),
                ({'local':'127.0.0.1'},'',1,False),
            ]:
                with self.subTest(addresses=addresses,connections=connections,index=index):
                    value=json.dumps('@ByteArray('+json.dumps(addresses)+')')
                    profile.write_text('[General]\nSiteConnections='+value+'\nConnections='+connections+'\nCurrConnections='+str(index)+'\n')
                    profile.chmod(0o600)
                    if valid:relay_module.require_loopback_profile(profile)
                    else:
                        with self.assertRaises(ValueError):relay_module.require_loopback_profile(profile)


def refresh_object(now=104, **changes):
    value={'version':1,'pid':1234,'generation':7,'host_counter':HOST_COUNTER,
           'sequence':1,'observed_monotonic_seconds':now}
    value.update(changes)
    return value


class RefreshValidationTests(unittest.TestCase):
    def test_refresh_exact_schema_and_current_native_context(self):
        current=publication_intent(intent_bytes())
        self.assertEqual(relay_module.refresh_request(json.dumps(refresh_object()),current,104)['sequence'],1)
        for change in ({'pid':1235},{'generation':8},{'host_counter':HOST_COUNTER+1},
                       {'packet_hex':advertisement().hex()},{'operation':'publish'},{'version':2}):
            with self.subTest(change=change),self.assertRaises(RelayError):
                relay_module.refresh_request(json.dumps(refresh_object(**change)),current,104)
        for current in (None,publication_intent(intent_bytes(raw=frame_bytes(2),operation='unpublish'))):
            with self.assertRaises(RelayError):relay_module.refresh_request(json.dumps(refresh_object()),current,104)

    def test_refresh_rejects_bad_keys_types_ranges_and_duplicate_keys(self):
        current=publication_intent(intent_bytes())
        objects=[None,[],True,'synthetic',{k:v for k,v in refresh_object().items() if k!='version'}]
        for field in ('version','pid','generation','host_counter','sequence'):
            for value in (True,False,None,'1',1.0,0,-1,2**64):objects.append(refresh_object(**{field:value}))
        objects.extend((refresh_object(pid=2**31),refresh_object(host_counter=2**28)))
        for obj in objects:
            with self.subTest(obj=obj),self.assertRaises(RelayError):relay_module.refresh_request(json.dumps(obj),current,104)
        duplicate=json.dumps(refresh_object())[:-1]+',"sequence":2}'
        with self.assertRaises(RelayError):relay_module.refresh_request(duplicate,current,104)

    def test_refresh_ttl_is_ten_seconds_with_no_future_or_nonfinite_values(self):
        current=publication_intent(intent_bytes())
        relay_module.refresh_request(json.dumps(refresh_object(now=94)),current,104)
        for value in (93.999,104.001,True,None,'104',float('nan'),float('inf'),-float('inf'),10**1000):
            with self.subTest(value=value),self.assertRaises(RelayError):
                relay_module.refresh_request(json.dumps(refresh_object(now=value)),current,104)

    def test_production_defaults_and_explicit_intervals_are_bounded(self):
        paths=(Path('/synthetic/intent'),Path('/synthetic/status'))
        self.assertEqual(Settings('127.0.0.1',16112,USERNAME,*paths).publication_min_interval,2.5)
        self.assertEqual(Settings('127.0.0.1',16112,USERNAME,*paths,continuous=True).publication_min_interval,30)
        for change in ({'publication_min_interval':2.49},{'publication_min_interval':float('nan')},
                       {'publication_min_interval':float('inf')},{'refresh_min_interval':4.99},
                       {'refresh_min_interval':float('inf')},{'realm_idle_timeout':9}):
            with self.subTest(change=change),self.assertRaises(RelayError):Settings('127.0.0.1',16112,USERNAME,*paths,**change)


class ContinuousFixture(RelayFixture):
    def setUp(self):
        super().setUp()
        self.relay.cfg.continuous=True;self.relay.cfg.publication_min_interval=30
        self.relay.cfg.refresh_request=self.root/'refresh.json'

    def write_refresh(self, now=104, **changes):
        self.relay.cfg.refresh_request.write_text(json.dumps(refresh_object(now,**changes)))
        self.relay.cfg.refresh_request.chmod(0o600)

    def ack_at(self,now,code=0):
        with patch.object(relay_module.time,'monotonic',return_value=now):return self.acknowledge(code)

    def query_answers(self,now):
        with patch.object(relay_module.time,'monotonic',return_value=now):
            while self.relay.query_pending:self.relay.inspect_server(Frame(9,game_list(game_record())))

    async def live_publish(self):
        self.authenticate();self.write_intent();await self.tick(100)
        self.assertEqual(self.relay.advertisements,0)
        self.write_intent(sequence=2);await self.tick(103);self.ack_at(103)

    async def heartbeat(self,now):
        self.write_intent(sequence=self.relay.sequence+1);await self.tick(now);self.query_answers(now)


class ContinuousPublicationTests(ContinuousFixture,unittest.IsolatedAsyncioTestCase):
    async def test_native_heartbeat_is_independent_of_thirty_second_wire_throttle(self):
        await self.live_publish()
        for now in range(106,133,3):
            await self.heartbeat(now)
            self.assertIsNotNone(self.relay.current)
            self.assertEqual(self.relay.last_native,now)
        self.assertEqual(self.relay.advertisements,1)
        await self.heartbeat(133)
        self.assertEqual(self.relay.advertisements,2)
        self.ack_at(133)
        self.assertFalse(self.relay.publication_failed)

    async def test_initial_advertisement_is_immediate_after_auth_with_fresh_intent(self):
        self.write_intent();await self.tick(1)
        self.authenticate();self.write_intent(sequence=2);await self.tick(2)
        self.assertEqual(self.relay.advertisements,1)
        self.assertEqual(self.relay.last_sent,2)

    async def test_native_heartbeat_before_auth_is_not_lost(self):
        self.relay.connected=True;self.write_intent();await self.tick(100)
        self.write_intent(sequence=2);await self.tick(103)
        self.assertEqual(self.relay.advertisements,0)
        self.authenticate();await self.tick(104)
        self.assertEqual(self.relay.advertisements,1)

    async def test_rejected_advertisement_uses_cooldown_and_exponential_backoff(self):
        self.authenticate();self.write_intent();await self.tick(100)
        self.write_intent(sequence=2);await self.tick(103);self.ack_at(103,2)
        self.assertEqual(self.relay.retry_at,133)
        for now in range(106,133,3):await self.heartbeat(now)
        self.assertEqual(self.relay.advertisements,1)
        await self.heartbeat(133);self.ack_at(133,2)
        self.assertEqual(self.relay.retry_at,193)
        self.assertEqual(self.relay.advertisements,2)
        for now in range(136,193,3):await self.heartbeat(now)
        await self.heartbeat(193);self.ack_at(193)
        self.assertFalse(self.relay.publication_failed)
        self.assertEqual(self.relay.retry_at,0)

    async def test_replayed_native_file_cannot_restore_expired_heartbeat(self):
        await self.live_publish();await self.tick(114)
        self.assertIsNone(self.relay.current)
        self.assertEqual(self.relay.withdrawals,1)
        await self.tick(115)
        self.assertIsNone(self.relay.current)
        self.assertEqual(self.relay.advertisements,1)

    async def test_native_unpublish_leaves_continuous_worker_alive_and_new_lobby_can_publish(self):
        await self.live_publish()
        self.write_intent(raw=frame_bytes(2),operation='unpublish',sequence=3);await self.tick(104)
        self.assertIsNone(self.relay.current)
        self.assertEqual(self.relay.withdrawals,1)
        self.assertEqual(self.relay.client_sessions,0)
        self.write_intent(raw=advertisement(name=b'Next Lobby'),sequence=4);await self.tick(106)
        self.assertIsNotNone(self.relay.current)
        self.assertEqual(self.relay.advertisements,1)
        await self.confirm_chat_return(107)
        for now in range(109,134,3):
            self.write_intent(raw=advertisement(name=b'Next Lobby'),sequence=self.relay.sequence+1)
            await self.tick(now);self.query_answers(now)
        self.assertEqual(self.relay.advertisements,2)

    async def test_same_title_rehost_returns_to_chat_before_current_counter_publication(self):
        await self.live_publish()
        self.write_intent(raw=frame_bytes(2),operation='unpublish',sequence=3)
        await self.tick(104)
        original_enter=self.relay.original_enterchat_packet
        original_join=self.relay.original_join_channel
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=4)
        await self.tick(106)
        self.assertEqual(self.writer.chunks[-1],original_enter)
        self.assertEqual(self.relay.advertisements,1)
        self.assertFalse(self.relay.authenticated)
        self.assertTrue(self.relay.record()['chat_return_pending'])
        self.assertFalse(self.relay.record()['exact_lookup_visible'])
        star=CaptureWriter()
        with patch.object(relay_module.time,'monotonic',return_value=107):
            await self.relay.server_to_client(ChunkReader([frame_bytes(0x0a,identity().payload),
                                                          frame_bytes(0x0f,chat_event(7).payload)]),star)
        self.assertEqual(self.writer.chunks[-1],original_join)
        self.assertNotIn(original_enter,star.chunks)
        self.assertNotIn(frame_bytes(0x0a,identity().payload),star.chunks)
        self.assertTrue(self.relay.authenticated)
        self.assertEqual(self.relay.chat_returns,1)
        self.assertFalse(self.relay.record()['chat_return_pending'])
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=5)
        await self.tick(133);self.ack_at(133)
        self.assertEqual(self.relay.advertisements,2)
        self.assertEqual(self.writer.chunks[-1],advertisement(counter=WIRE_COUNTER+1))
        for now in (134,137):
            self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,
                              sequence=self.relay.sequence+1)
            await self.tick(now)
            with patch.object(relay_module.time,'monotonic',return_value=now):
                while self.relay.query_pending:
                    self.relay.inspect_server(Frame(9,game_list(game_record(counter=WIRE_COUNTER+1))))
        self.assertTrue(self.relay.named_visible)
        self.assertTrue(self.relay.visible)
        self.assertEqual(sum(chunk==original_enter for chunk in self.writer.chunks),1)

    async def test_chat_return_timeout_cannot_republish_without_fresh_server_confirmation(self):
        await self.live_publish()
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        self.assertFalse(self.relay.authenticated)
        await self.tick(112.01)
        self.assertEqual(self.relay.error,'RELAY_CHAT_RETURN_TIMEOUT')
        self.assertEqual(self.relay.advertisements,1)
        self.assertTrue(self.relay.publication_failed)

    async def test_rehost_missing_original_control_frames_fails_closed(self):
        await self.live_publish()
        self.relay.original_enterchat_packet=None
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        self.assertEqual(self.relay.error,'RELAY_CHAT_RETURN_UNAVAILABLE')
        self.assertEqual(self.relay.advertisements,1)

    @patch.object(relay_module.time,'monotonic',return_value=104)
    async def test_chat_return_requires_expected_identity_before_replaying_original_channel(self,_clock):
        await self.live_publish()
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        frame=identity('OtherSynthetic')
        self.relay.inspect_server(frame)
        with self.assertRaisesRegex(RelayError,'RELAY_CHAT_RETURN_IDENTITY'):
            await self.relay.chat_return_response(frame)
        self.assertNotEqual(self.writer.chunks[-1],self.relay.original_join_channel)
        self.assertFalse(self.relay.authenticated)

    @patch.object(relay_module.time,'monotonic',return_value=104)
    async def test_chat_return_channel_event_before_identity_does_not_confirm(self,_clock):
        await self.live_publish()
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        frame=chat_event(7)
        self.relay.inspect_server(frame)
        await self.relay.chat_return_response(frame)
        self.assertEqual(self.relay.chat_returns,0)
        self.assertIsNotNone(self.relay.chat_return_started)
        self.assertFalse(self.relay.authenticated)
        frame=identity()
        self.relay.inspect_server(frame)
        await self.relay.chat_return_response(frame)
        self.assertFalse(self.relay.channel)
        self.assertFalse(self.relay.authenticated)
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=4)
        await self.tick(106)
        self.assertEqual(self.relay.advertisements,1)
        frame=chat_event(7)
        self.relay.inspect_server(frame)
        await self.relay.chat_return_response(frame)
        self.assertTrue(self.relay.authenticated)

    @patch.object(relay_module.time,'monotonic',return_value=104)
    async def test_original_enterchat_ack_is_forwarded_before_replay_owned_ack(self,_clock):
        await self.live_publish()
        original=frame_bytes(0x0a,b'\0\0')
        await self.relay.send(original)  # An original Star request already in flight.
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        before=len(self.writer.chunks)
        original_reply=identity()
        self.assertTrue(self.relay.inspect_server(original_reply))
        self.assertFalse(await self.relay.chat_return_response(original_reply))
        self.assertEqual(len(self.writer.chunks),before)
        self.assertTrue(self.relay.chat_return_join_pending)
        self.assertFalse(self.relay.authenticated)
        replay_reply=identity()
        self.relay.inspect_server(replay_reply)
        self.assertTrue(await self.relay.chat_return_response(replay_reply))
        self.assertEqual(self.writer.chunks[-1],self.relay.original_join_channel)
        self.assertFalse(self.relay.authenticated)
        frame=chat_event(7)
        self.relay.inspect_server(frame);await self.relay.chat_return_response(frame)
        self.assertTrue(self.relay.authenticated)
        self.assertFalse(self.relay.enterchat_pending)

    @patch.object(relay_module.time,'monotonic',return_value=104)
    async def test_retired_chat_return_ack_does_not_change_new_epoch_identity(self,_clock):
        await self.live_publish()
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(104)
        self.relay.epoch+=1
        frame=identity()
        self.assertFalse(self.relay.inspect_server(frame))
        self.assertFalse(await self.relay.chat_return_response(frame))
        self.assertFalse(self.relay.identity)
        self.assertFalse(self.relay.authenticated)

    async def test_original_chat_frames_are_captured_exactly_and_cleared_between_sessions(self):
        enter=frame_bytes(0x0a,b'\0\0')
        join=frame_bytes(0x0c,struct.pack('<I',2)+b'Original Synthetic Channel\0')
        await self.relay.client_to_server(ChunkReader([enter,join]))
        self.assertEqual(self.relay.original_enterchat_packet,enter)
        self.assertEqual(self.relay.original_join_channel,join)
        self.relay.chat_return_required=True;self.relay.chat_return_started=100
        self.relay.reset_session()
        self.assertIsNone(self.relay.original_enterchat_packet)
        self.assertIsNone(self.relay.original_join_channel)
        self.assertFalse(self.relay.chat_return_required)
        self.assertIsNone(self.relay.chat_return_started)
        self.assertFalse(self.relay.enterchat_pending)

    async def test_retired_native_generation_cannot_be_adopted_again(self):
        await self.live_publish();self.write_intent(generation=8);await self.tick(104)
        self.assertEqual(self.relay.generation,(1234,7))
        self.write_intent(generation=8,sequence=2);await self.tick(106)
        self.assertEqual(self.relay.generation,(1234,8))
        self.write_intent(generation=7,sequence=10);await self.tick(109)
        self.assertEqual(self.relay.generation,(1234,8))

    async def test_late_ack_deadline_is_checked_when_response_arrives(self):
        self.authenticate();self.write_intent();await self.tick(100)
        self.write_intent(sequence=2);await self.tick(103)
        with self.assertRaisesRegex(RelayError,'RELAY_PUBLICATION_ACK_TIMEOUT'):self.ack_at(111.01)
        self.assertEqual(self.relay.native_acks,0)
        self.assertFalse(self.relay.advertised)

    async def test_timed_out_original_ack_cannot_shift_fifo_onto_native(self):
        await self.live_publish()
        with patch.object(relay_module.time,'monotonic',return_value=104):await self.relay.send(frame_bytes(0x1c,OPAQUE))
        self.write_intent(sequence=3)
        await self.tick(113)
        self.assertFalse(self.relay.connected)
        self.assertTrue(self.relay.publication_failed)
        self.assertEqual(self.relay.error,'RELAY_PUBLICATION_ACK_TIMEOUT')
        self.assertEqual(self.relay.advertisements,1)

    async def test_health_uses_server_traffic_and_does_not_infer_it_from_client_sends(self):
        await self.live_publish()
        self.relay.last_native=163
        with patch.object(relay_module.time,'monotonic',return_value=164):
            status=self.relay.record()
        self.assertFalse(status['realm_connection_healthy'])
        self.assertEqual(status['relay_pid'],os.getpid())
        self.assertEqual(status['last_server_traffic_monotonic_seconds'],103)
        self.assertEqual(status['last_ack_monotonic_seconds'],103)
        self.assertEqual(status['last_ack_status'],0)

    async def test_record_expires_freshness_even_before_worker_runs(self):
        await self.live_publish();await self.heartbeat(106)
        self.assertTrue(self.relay.named_visible or self.relay.visible)
        with patch.object(relay_module.time,'monotonic',return_value=117):status=self.relay.record()
        self.assertFalse(status['native_heartbeat_fresh'])
        self.assertFalse(status['advertisement_accepted'])
        self.assertFalse(status['exact_lookup_visible'])
        self.assertFalse(status['public_directory_seen_for_current_lobby'])

    async def test_fresh_native_does_not_make_an_old_directory_page_fresh(self):
        await self.live_publish();await self.heartbeat(106)
        self.relay.last_native=116
        with patch.object(relay_module.time,'monotonic',return_value=117):status=self.relay.record()
        self.assertTrue(status['advertisement_accepted'])
        self.assertTrue(status['public_directory_seen_for_current_lobby'])
        self.assertFalse(status['visible_in_realm_query'])


class ManualRefreshTests(ContinuousFixture,unittest.IsolatedAsyncioTestCase):
    async def test_refresh_counts_success_only_from_matching_real_ack(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_successful),(1,0,0))
        await self.heartbeat(108)
        self.assertEqual((self.relay.refresh_sent,self.relay.refresh_successful),(1,0))
        self.ack_at(108)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_successful,self.relay.refresh_failed),(1,1,1,0))
        self.assertEqual(self.relay.last_ack_context['refresh_sequence'],1)
        self.assertEqual(self.relay.last_refresh_result,{'pid':1234,'generation':7,'host_counter':HOST_COUNTER,'sequence':1,'status':'accepted'})

    async def test_duplicate_refresh_sequence_never_sends_twice(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108);self.ack_at(108)
        await self.heartbeat(111)
        self.assertEqual(self.relay.refresh_requested,1)
        self.assertEqual(self.relay.refresh_sent,1)

    async def test_explicit_refreshes_are_at_least_five_seconds_apart(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108);self.ack_at(108)
        self.write_refresh(now=109,sequence=2);await self.tick(109)
        await self.heartbeat(112)
        self.assertEqual(self.relay.refresh_sent,1)
        await self.tick(113)
        self.assertEqual(self.relay.refresh_sent,2)
        self.assertEqual(self.relay.last_refresh_sent,113)

    async def test_new_pending_refresh_supersedes_old_without_misattributing_ack(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        self.write_refresh(now=105,sequence=2);await self.tick(105)
        await self.heartbeat(108);self.ack_at(108)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_successful,self.relay.refresh_failed),(2,1,1,1))
        self.assertEqual(self.relay.last_ack_context['refresh_sequence'],2)

    async def test_new_request_while_old_inflight_cannot_steal_old_ack(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108)
        self.write_refresh(now=109,sequence=2);await self.tick(109)
        self.ack_at(109)
        self.assertEqual(self.relay.last_refresh_result['sequence'],1)
        self.assertEqual(self.relay.refresh_pending['sequence'],2)
        self.assertEqual(self.relay.refresh_successful,1)

    async def test_rejected_refresh_counts_failure_and_enters_cooldown(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108);self.ack_at(108,2)
        self.assertEqual((self.relay.refresh_sent,self.relay.refresh_successful,self.relay.refresh_failed),(1,0,1))
        self.assertEqual(self.relay.last_refresh_ack,108)
        self.assertEqual(self.relay.last_refresh_result['status'],'rejected')
        self.assertGreaterEqual(self.relay.retry_at,138)

    async def test_unpublish_cancels_refresh_and_late_ack_cannot_count_success(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108)
        self.write_intent(raw=frame_bytes(2),operation='unpublish',sequence=4);await self.tick(109)
        self.ack_at(109)
        self.assertEqual((self.relay.refresh_successful,self.relay.refresh_failed),(0,1))
        self.assertFalse(self.relay.advertised)

    async def test_expired_queued_refresh_counts_failure_without_reusing_request(self):
        await self.live_publish();self.relay.retry_at=140;self.relay.publication_failed=True
        self.write_refresh();await self.tick(104);self.query_answers(104)
        for now in (107,110,113,116):await self.heartbeat(now)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_failed),(1,0,1))
        self.assertEqual(self.relay.last_refresh_result['status'],'expired')

    async def test_refresh_ipc_cannot_accept_packets_or_authentication(self):
        await self.live_publish();self.write_refresh(packet_hex=frame_bytes(0x53,OPAQUE).hex())
        await self.tick(104)
        self.assertEqual(self.relay.refresh_requested,0)
        self.assertEqual(self.relay.refresh_request_error,'RELAY_REFRESH_REQUEST_REJECTED')
        self.assertTrue(self.relay.authenticated)
        self.assertEqual(self.relay.advertisements,1)
        self.assertNotIn(OPAQUE,self.writer.data)

    async def test_refresh_file_must_be_private_and_owned_regular_input(self):
        await self.live_publish();self.write_refresh();self.relay.cfg.refresh_request.chmod(0o644)
        await self.tick(104)
        self.assertEqual(self.relay.refresh_requested,0)
        self.assertEqual(self.relay.refresh_request_error,'RELAY_REFRESH_REQUEST_REJECTED')

    async def test_refresh_timeout_fails_once_without_counting_an_ack(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108)
        await self.tick(117)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_successful,self.relay.refresh_failed),(1,1,0,1))
        self.assertIsNone(self.relay.last_refresh_ack)
        await self.tick(118)
        self.assertEqual(self.relay.refresh_failed,1)

    async def test_stale_refresh_context_cannot_target_replacement_lobby(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        self.write_intent(raw=advertisement(counter=WIRE_COUNTER+1),host_counter=HOST_COUNTER+1,sequence=3)
        await self.tick(106)
        self.assertIsNone(self.relay.refresh_pending)
        self.assertEqual((self.relay.refresh_requested,self.relay.refresh_sent,self.relay.refresh_failed),(1,0,1))
        self.assertEqual(self.relay.refresh_request_error,'RELAY_REFRESH_REQUEST_REJECTED')

    async def test_refresh_traffic_does_not_extend_native_heartbeat(self):
        await self.live_publish();self.write_refresh(now=111);await self.tick(111);self.query_answers(111)
        self.ack_at(111)
        self.write_refresh(now=114,sequence=2);await self.tick(114)
        self.assertIsNone(self.relay.current)
        self.assertEqual(self.relay.last_native,103)
        self.assertEqual(self.relay.error,'RELAY_NATIVE_HEARTBEAT_EXPIRED')

    async def test_refresh_status_preserves_actual_ack_context_when_new_hint_is_cancelled(self):
        await self.live_publish();self.write_refresh();await self.tick(104);self.query_answers(104)
        await self.heartbeat(108);self.ack_at(108)
        self.write_refresh(now=109,sequence=2);await self.tick(109)
        self.relay.cancel_refreshes()
        self.assertEqual(self.relay.last_refresh_result['sequence'],2)
        self.assertEqual(self.relay.last_refresh_ack_context['refresh_sequence'],1)
        self.assertEqual(self.relay.last_refresh_ack_context['status'],0)


class ContinuousSessionTests(ContinuousFixture,unittest.IsolatedAsyncioTestCase):
    async def test_sequential_sessions_reset_all_auth_queues_visibility_and_port_state(self):
        self.relay.cfg.refresh_request=None
        calls=[]
        async def connect(*args,**kwargs):
            writer=CaptureWriter();calls.append(writer)
            return ChunkReader([]),writer
        with patch.object(relay_module.asyncio,'open_connection',connect):
            await self.relay.session(ChunkReader([]),CaptureWriter())
            self.relay.identity=self.relay.channel=self.relay.welcome=self.relay.realm_welcome=True
            self.relay.server_proof_status=0;self.relay.requested_identity_matches=True
            self.relay.netgameport_sent=True;self.relay.last_ack=100;self.relay.visible=self.relay.named_visible=True
            self.relay.advert_pending.append(((1,None,None),100));self.relay.query_pending.append((None,100))
            await self.relay.session(ChunkReader([]),CaptureWriter())
        self.assertEqual(len(calls),2)
        self.assertTrue(all(writer.closed for writer in calls))
        self.assertFalse(self.relay.used)
        self.assertEqual(self.relay.session_generation,2)
        self.assertFalse(self.relay.identity or self.relay.channel or self.relay.welcome or self.relay.realm_welcome or self.relay.netgameport_sent)
        self.assertIsNone(self.relay.server_proof_status)
        self.assertEqual((len(self.relay.advert_pending),len(self.relay.query_pending),self.relay.last_ack),(0,0,0))

    async def test_concurrent_session_is_rejected_until_both_old_writers_are_closed(self):
        close_entered=asyncio.Event();release_close=asyncio.Event();entered=asyncio.Event()
        class SlowClose(CaptureWriter):
            async def wait_closed(inner):close_entered.set();await release_close.wait()
        upstream=SlowClose()
        async def connect(*args,**kwargs):entered.set();return ChunkReader([]),upstream
        with patch.object(relay_module.asyncio,'open_connection',connect) as connect_mock:
            first=asyncio.create_task(self.relay.session(ChunkReader([]),CaptureWriter()))
            await asyncio.wait_for(close_entered.wait(),1)
            rejected=CaptureWriter();await self.relay.session(ChunkReader([]),rejected)
            self.assertTrue(rejected.closed)
            self.assertEqual(self.relay.client_sessions,1)
            self.assertEqual(self.relay.rejected_connections,1)
            release_close.set();await first
            await self.relay.session(ChunkReader([]),CaptureWriter())
        self.assertEqual(self.relay.client_sessions,2)

    async def test_reconnect_baselines_existing_native_intent_until_new_progress(self):
        await self.live_publish();self.write_intent(sequence=3)
        self.relay.reset_session();self.relay.upstream=self.writer;self.authenticate()
        await self.tick(106)
        self.assertIsNone(self.relay.current)
        self.assertEqual(self.relay.advertisements,1)
        self.write_intent(sequence=4);await self.tick(109)
        self.assertEqual(self.relay.advertisements,2)
        self.assertFalse(self.relay.advertised)
        self.assertFalse(self.relay.visible)

    async def test_continuous_run_ignores_bounded_duration_and_stops_explicitly(self):
        self.relay.cfg.duration=0.001
        class FakeServer:
            async def __aenter__(inner):return inner
            async def __aexit__(inner,*args):pass
        async def start_server(*args,**kwargs):return FakeServer()
        loop=asyncio.get_running_loop()
        with patch.object(relay_module.asyncio,'start_server',start_server),patch.object(loop,'add_signal_handler'):
            task=asyncio.create_task(self.relay.run())
            await asyncio.sleep(0.01)
            self.assertFalse(task.done());self.assertTrue(self.relay.listening)
            self.relay.stop.set();result=await asyncio.wait_for(task,1)
        self.assertFalse(result['listener_ready'])
        self.assertFalse(self.relay.session_tasks)

    async def test_session_error_messages_never_copy_exception_payloads(self):
        async def connect(*args,**kwargs):raise OSError('SYNTHETIC-PRIVATE-MARKER')
        with patch.object(relay_module.asyncio,'open_connection',connect):await self.relay.session(ChunkReader([]),CaptureWriter())
        self.assertEqual(self.relay.error,'RELAY_SESSION_CLOSED')
        self.assertNotIn('SYNTHETIC-PRIVATE-MARKER',self.relay.cfg.status.read_text())

    async def test_stale_status_temporary_symlink_is_never_followed(self):
        victim=self.root/'victim';victim.write_text('unchanged')
        self.relay.cfg.status.with_name('status.json.tmp').symlink_to(victim)
        self.relay.record()
        self.assertEqual(victim.read_text(),'unchanged')
        self.assertEqual(self.relay.cfg.status.stat().st_mode&0o777,0o600)

    async def test_old_write_completion_cannot_mutate_or_send_into_new_session(self):
        await self.live_publish()
        draining=asyncio.Event();release=asyncio.Event()
        class BlockingWriter(CaptureWriter):
            async def drain(inner):draining.set();await release.wait()
        old=BlockingWriter();self.relay.upstream=old
        task=asyncio.create_task(self.relay.send(advertisement(),(self.relay.generation,3)))
        await draining.wait()
        self.relay.session_generation+=1;self.relay.reset_session()
        replacement=CaptureWriter();self.relay.upstream=replacement
        release.set()
        with self.assertRaisesRegex(RelayError,'RELAY_NO_SESSION'):await task
        self.assertEqual(replacement.data,b'')
        self.assertEqual(self.relay.last_sent,0)
        self.assertFalse(self.relay.advertised)

    async def test_stale_tick_error_cannot_cancel_new_session(self):
        active=asyncio.create_task(asyncio.Event().wait())
        async def stale_tick():
            self.relay.session_generation+=1;self.relay.active_session=active
            raise RelayError('RELAY_NO_SESSION')
        async def stop_iteration():self.relay.stop.set()
        with patch.object(self.relay,'intent_tick',stale_tick),patch.object(self.relay.stop,'wait',stop_iteration):
            await self.relay.intents()
        self.assertFalse(active.cancelled());self.assertFalse(active.done())
        active.cancel();await asyncio.gather(active,return_exceptions=True)


class LoopbackRecoveryTests(ContinuousFixture,unittest.IsolatedAsyncioTestCase):
    async def until(self,predicate):
        async with asyncio.timeout(2):
            while not predicate():await asyncio.sleep(0.001)

    async def open_network(self):
        """Real TCP sockets, exclusively on the network=none container loopback."""
        self.relay.upstream=None;self.realms=[];self.realm_tasks=set();self.clients=[]
        self.server_frames=frame_bytes(0x0a,identity().payload)+frame_bytes(0x0f,chat_event(7).payload)+frame_bytes(0x5e,OPAQUE)
        async def realm(reader,writer):
            task=asyncio.current_task();self.realm_tasks.add(task);entry={'writer':writer,'received':bytearray()};self.realms.append(entry)
            try:
                entry['received'].extend(await reader.readexactly(1))
                writer.write(self.server_frames);await writer.drain()
                while data:=await reader.read(8192):entry['received'].extend(data)
            except (OSError,asyncio.IncompleteReadError):pass
            finally:
                await StarRelay.close_writer(writer);self.realm_tasks.discard(task)
        self.realm_server=await asyncio.start_server(realm,'127.0.0.1',0)
        self.relay.cfg.upstream_port=self.realm_server.sockets[0].getsockname()[1]
        self.local_server=await asyncio.start_server(self.relay.session,'127.0.0.1',0)
        self.local_port=self.local_server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self.close_network)

    async def close_network(self):
        self.relay.stop.set()
        self.local_server.close();self.realm_server.close()
        for writer in self.clients:await StarRelay.close_writer(writer)
        for task in tuple(self.relay.session_tasks):task.cancel()
        await asyncio.gather(*tuple(self.relay.session_tasks),return_exceptions=True)
        for entry in self.realms:await StarRelay.close_writer(entry['writer'])
        await asyncio.wait_for(asyncio.gather(*tuple(self.realm_tasks),return_exceptions=True),2)
        await self.local_server.wait_closed();await self.realm_server.wait_closed()

    async def star_connect(self):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.local_port);self.clients.append(writer)
        writer.write(b'\x01');await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(self.server_frames)),2),self.server_frames)
        await self.until(lambda:self.relay.authenticated)
        return reader,writer

    async def test_real_loopback_eof_and_reconnect_do_not_adopt_old_ack_or_auth(self):
        self.write_intent();await self.open_network()
        first_reader,first=await self.star_connect()
        self.write_intent(sequence=2);await self.relay.intent_tick()
        self.assertEqual(self.relay.advertisements,1)
        await self.until(lambda:advertisement() in self.realms[0]['received'])
        original_ack=frame_bytes(0x1c,struct.pack('<I',0))
        self.realms[0]['writer'].write(original_ack);await self.realms[0]['writer'].drain()
        await self.until(lambda:self.relay.advertised)
        original_request=frame_bytes(0x1c,b'original-request')
        first.write(original_request);await first.drain()
        await self.until(lambda:original_request in self.realms[0]['received'])
        self.realms[0]['writer'].write(original_ack);await self.realms[0]['writer'].drain()
        self.assertEqual(await asyncio.wait_for(first_reader.readexactly(len(original_ack)),2),original_ack)
        await StarRelay.close_writer(first);await self.until(lambda:not self.relay.used)
        self.assertFalse(self.relay.authenticated or self.relay.advertised)
        second_reader,second=await self.star_connect()
        self.assertEqual(self.relay.session_generation,2)
        self.assertFalse(self.relay.advertised)
        self.assertEqual(self.relay.last_ack,0)
        await self.relay.intent_tick()
        self.assertEqual(self.relay.advertisements,1)
        self.write_intent(sequence=3);await self.relay.intent_tick()
        self.assertEqual(self.relay.advertisements,2)
        await self.until(lambda:advertisement() in self.realms[1]['received'])
        self.assertEqual(len(self.realms),2)

    async def test_real_publication_timeout_closes_cohort_and_worker_accepts_new_star(self):
        self.write_intent();await self.open_network();reader,writer=await self.star_connect()
        self.write_intent(sequence=2);await self.relay.intent_tick()
        owner,_=self.relay.advert_pending[0]
        self.relay.advert_pending[0]=(owner,relay_module.time.monotonic()-9)
        worker=asyncio.create_task(self.relay.intents())
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(1),2),b'')
            await self.until(lambda:not self.relay.used)
            self.assertFalse(worker.done())
            self.assertFalse(self.relay.advertised)
            self.assertEqual(self.relay.error,'RELAY_PUBLICATION_ACK_TIMEOUT')
            await self.star_connect()
            self.assertEqual(self.relay.session_generation,2)
            self.assertIsNone(self.relay.error)
            self.assertFalse(self.relay.advertised)
        finally:
            self.relay.stop.set();await asyncio.wait_for(worker,2)

    async def test_real_original_query_timeout_cannot_assign_reply_to_native_query(self):
        self.write_intent();await self.open_network();reader,writer=await self.star_connect()
        original=frame_bytes(9,b'original-query')
        writer.write(original);await writer.drain()
        await self.until(lambda:len(self.relay.query_pending)==1)
        owner,_=self.relay.query_pending[0]
        self.assertIsNone(owner)
        self.relay.query_pending[0]=(owner,relay_module.time.monotonic()-9)
        worker=asyncio.create_task(self.relay.intents())
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(1),2),b'')
            await self.until(lambda:not self.relay.used)
            self.assertFalse(worker.done())
            self.assertEqual(self.relay.error,'RELAY_GAME_QUERY_TIMEOUT')
            self.assertEqual(self.relay.queries,0)
        finally:
            self.relay.stop.set();await asyncio.wait_for(worker,2)

    async def test_run_shutdown_closes_live_tcp_cohort_before_waiting_for_listener(self):
        self.write_intent();await self.open_network()
        self.local_server.close();await self.local_server.wait_closed()
        self.relay.cfg.listen_port=0
        original=relay_module.asyncio.start_server;opened=[]
        async def capture(*args,**kwargs):
            server=await original(*args,**kwargs);opened.append(server);return server
        loop=asyncio.get_running_loop()
        with patch.object(relay_module.asyncio,'start_server',capture),patch.object(loop,'add_signal_handler'):
            run=asyncio.create_task(self.relay.run())
            await self.until(lambda:self.relay.listening)
            self.local_port=opened[0].sockets[0].getsockname()[1]
            reader,writer=await self.star_connect()
            self.write_intent(sequence=2)
            await self.until(lambda:self.relay.advertisements==1)
            self.realms[0]['writer'].write(frame_bytes(0x1c,struct.pack('<I',0)))
            await self.realms[0]['writer'].drain();await self.until(lambda:self.relay.advertised)
            self.relay.stop.set()
            result=await asyncio.wait_for(run,2)
            self.assertEqual(await asyncio.wait_for(reader.read(1),2),b'')
            await self.until(lambda:not self.realm_tasks)
        self.assertFalse(result['authenticated'])
        self.assertFalse(result['listener_ready'])
        self.assertFalse(self.relay.session_tasks)
        self.assertEqual(result['withdrawals_sent'],1)
        self.assertEqual(self.realms[0]['received'].count(frame_bytes(2)),1)


class CliContractTests(RelayFixture,unittest.IsolatedAsyncioTestCase):
    async def command(self,continuous=True):
        reservation=await asyncio.start_server(lambda reader,writer:writer.close(),'127.0.0.1',0)
        port=reservation.sockets[0].getsockname()[1]
        reservation.close();await reservation.wait_closed()
        profile=self.root/'launcher.ini'
        gateway=json.dumps('@ByteArray('+json.dumps({'synthetic':'127.0.0.1:'+str(port)})+')')
        profile.write_text('[General]\nUsername='+json.dumps(USERNAME)+'\nConnections=\nCurrConnections=0\nUserConnections='+gateway+'\nSiteConnections='+gateway+'\n')
        profile.chmod(0o600)
        args=[sys.executable,'-m','src.bridge.star_relay','--upstream','127.0.0.1','--offline-test',
              '--profile',str(profile),'--intent',str(self.relay.cfg.intent),'--status',str(self.relay.cfg.status),
              '--listen-port',str(port),'--game-port','6113','--publish','--publication-interval','30',
              '--refresh-request',str(self.root/'refresh-request.json'),'--duration','0']
        if continuous:args.append('--continuous')
        return await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)

    async def test_exact_production_cli_alias_starts_before_star_and_sigterm_is_clean(self):
        child=await self.command()
        try:
            async with asyncio.timeout(3):
                while not self.relay.cfg.status.exists() or not json.loads(self.relay.cfg.status.read_text())['listener_ready']:
                    if child.returncode is not None:self.fail('synthetic production CLI exited before listening')
                    await asyncio.sleep(0.005)
            status=json.loads(self.relay.cfg.status.read_text())
            self.assertEqual(status['relay_pid'],child.pid)
            self.assertTrue(status['continuous']);self.assertFalse(status['authenticated'])
            self.assertEqual(status['publication_min_interval_seconds'],30)
            self.assertEqual(status['session_generation'],0)
            child.terminate();stdout,stderr=await asyncio.wait_for(child.communicate(),3)
            self.assertEqual(child.returncode,0,(stdout,stderr))
            self.assertFalse(json.loads(self.relay.cfg.status.read_text())['listener_ready'])
        finally:
            if child.returncode is None:child.kill();await child.communicate()

    async def test_duration_zero_does_not_implicitly_enable_production(self):
        child=await self.command(continuous=False)
        stdout,stderr=await asyncio.wait_for(child.communicate(),3)
        self.assertEqual(child.returncode,1)
        self.assertEqual(json.loads(stdout),{'result':'FAIL','reason':'STAR_RELAY_REJECTED'})
        self.assertEqual(stderr,b'')


if __name__ == "__main__":
    unittest.main()
