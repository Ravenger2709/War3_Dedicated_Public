# SPDX-License-Identifier: Apache-2.0
"""Synthetic native-writer fixtures; no replay, network, or engine needed."""
import binascii
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import zlib

SPEC = importlib.util.spec_from_file_location('replay_audit', Path(__file__).with_name('replay-audit.py'))
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def u16(n):
    return struct.pack('<H', n)


def u32(n):
    return struct.pack('<I', n)


def triggered(text, a=821, b=821):
    return b'\x60' + u32(a) + u32(b) + text + b'\0'


def export(key, value, cache=b'AIG1.w3v', mission=b'g1', opcode=0x6b):
    return bytes([opcode]) + cache + b'\0' + mission + b'\0' + key + b'\0' + u32(value)


def frame(*records, delta=250, op=0x1f):
    content = u16(delta) + b''.join(bytes([pid]) + u16(len(data)) + data for pid, data in records)
    return bytes([op]) + u16(len(content)) + content


def body(records=b'', names=b'PRIVATE_NAME_192.0.2.1', loading=b''):
    # Independently assemble the fields written by native BuildReplay.
    host = b'\x10\x01\0\0\0\x01' + names + b'\0\x01\0'
    header = host + b'PRIVATE_GAME\0\0PRIVATE_STAT\0' + struct.pack('<III', 2, 0, 0x0012f8b0)
    # The fake sender need not occur in m_Players; its occupied slot is authoritative.
    slots = bytes([1, 100, 2, 0, 0, 0, 1, 1, 100, 2, 100, 2, 0, 0, 11, 1, 1, 100])
    start = b'\x19' + u16(25) + b'\x02' + slots + u32(1234) + b'\0\x02'
    return header + start + b'\x1a\x01\0\0\0\x1b\x01\0\0\0' + loading + b'\x1c\x01\0\0\0' + records


def packed(raw, duration=0):
    # Independent fixture writer: no auditor helpers/constants/CRC-fold function.
    padded = raw + bytes(8192 - len(raw) % 8192)
    chunks = []
    for i in range(0, len(padded), 8192):
        compressed = zlib.compress(padded[i:i + 8192])
        sizes = struct.pack('<HH', len(compressed), 8192)
        h = binascii.crc32(sizes + bytes(4))
        d = binascii.crc32(compressed)
        checksum = ((h ^ (h >> 16)) & 0xffff) | (((d ^ (d >> 16)) & 0xffff) << 16)
        chunks.append(sizes + u32(checksum) + compressed)
    header = b'Warcraft III recorded game\x1a\0'
    header += struct.pack('<IIIII', 68, 68 + sum(map(len, chunks)), 1, len(raw), len(chunks))
    header += b'PX3W' + struct.pack('<IHHI', 26, 6401, 32768, duration)
    header += u32(binascii.crc32(header + bytes(4)))
    return header + b''.join(chunks)


def reheader(data, offset, value):
    data = bytearray(data)
    struct.pack_into('<I', data, offset, value)
    struct.pack_into('<I', data, 64, 0)
    struct.pack_into('<I', data, 64, binascii.crc32(data[:68]))
    return bytes(data)


class ReplayAuditTests(unittest.TestCase):
    def result(self, records=b'', duration=0, **kw):
        return audit.audit(packed(body(records, **kw), duration))

    def refuse(self, data, code=None):
        result = audit.audit(data)
        self.assertNotEqual(result['code'], 'OK')
        self.assertEqual(result['structural_integrity'], 'REFUSED')
        self.assertNotIn('commands', result)
        if code:
            self.assertEqual(result['code'], code)
        return result

    def test_empty_native_and_privacy(self):
        result = self.result()
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['container_integrity'], 'PASS')
        self.assertEqual(result['engine_playback'], 'NOT_ASSESSED')
        self.assertEqual(result['engine_desync'], 'NOT_ASSESSED')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('192.0.2.1', json.dumps(result))

    def test_probe_then_actor_and_two_peer_digests(self):
        records = frame((1, export(b'wire_version', 2) + export(b'tick', 8)),
                        (2, triggered(b'~g1:42:0:8:0')))
        records += frame((1, export(b'tick', 9) + export(b'probe', 42)))
        records += frame((2, triggered(b'~g1:42:1:16:1')))
        records += frame((1, export(b'tick', 17) + export(b'digest', 123) + export(b'ack', 1)),
                         (2, export(b'digest', 123) + export(b'ack', 1)))
        result = self.result(records, 1000)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['counts']['probe_causal'], 1)
        self.assertEqual([c['kind'] for c in result['commands']], [0, 1])
        self.assertEqual(result['commands'][1]['ack_count'], 2)
        self.assertEqual([(p['pid'], p['epoch'], p['last_digest']) for p in result['peers']],
                         [(1, 42, 123), (2, 42, 123)])
        self.assertEqual(result['incoming_batch_causality'], 'NOT_RECOVERABLE')

    def test_probe_wrong_epoch_same_record_and_missing_later_tick(self):
        records = frame((1, export(b'wire_version', 2) + export(b'tick', 8)),
                        (2, triggered(b'~g1:42:0:8:0')))
        records += frame((1, export(b'probe', 99) + export(b'probe', 42)))
        result = self.result(records, 500)
        self.assertEqual(result['counts']['orphan_probe'], 1)
        self.assertEqual(result['counts']['probe_noncausal'], 1)
        same = frame((1, export(b'wire_version', 2) + triggered(b'~g1:42:0:8:0')
                     + export(b'tick', 9) + export(b'probe', 42)))
        self.assertEqual(self.result(same, 250)['counts']['probe_noncausal'], 1)

    def test_ack_requires_preceding_command_and_fresh_digest(self):
        records = frame((1, export(b'digest', 9) + export(b'ack', 1)))
        records += frame((2, triggered(b'~g1:42:1:8:1')))
        records += frame((1, export(b'ack', 1) + export(b'digest', 10)
                         + export(b'ack', 1) + export(b'ack', 1)))
        result = self.result(records, 750)
        self.assertEqual(result['commands'][0]['ack_count'], 1)
        self.assertEqual(result['counts']['orphan_ack'], 2)
        self.assertEqual(result['counts']['duplicate_ack'], 1)

    def test_epoch_sequence_and_digest_mismatch(self):
        records = frame((2, triggered(b'~g1:42:1:8:1')))
        records += frame((1, export(b'digest', 1) + export(b'ack', 1)),
                         (2, export(b'digest', 2) + export(b'ack', 1)))
        records += frame((2, triggered(b'~g1:42:1:16:1') + triggered(b'~g1:43:1:16:1')))
        result = self.result(records, 750)
        self.assertEqual(result['counts']['digest_mismatches'], 1)
        self.assertEqual([c['sequence_increasing'] for c in result['commands']], [True, False, True])

    def test_foreign_payload_embedded_opcodes_are_not_events(self):
        bait = triggered(b'~g1:42:1:8:1')
        selection = b'\x16\x01' + u16(4) + (bait + bytes(32))[:32]
        other = export(b'ack', 1, cache=b'Other') + export(b'tick', 8, mission=b'other')
        other += export(b'ack', 1, opcode=0x6c) + triggered(b'not-g1 ~g1:42:1:8:1')
        chatbody = b'\x20' + u32(0) + bait + b'\0'
        chat = b'\x20\x01' + u16(len(chatbody)) + chatbody
        # Embedded zeros in an actual chat string violate native chat framing.
        self.refuse(packed(body(chat)), 'CHAT_LENGTH')
        records = frame((1, selection + other))
        result = self.result(records, 250)
        self.assertEqual(result['commands'], [])
        self.assertEqual(result['counts']['foreign_cache'], 3)
        self.assertEqual(result['counts']['foreign_chat'], 1)
        self.assertEqual(result['receiver_pairs'], [])

    def test_hello_exact_and_receiver_pair_numeric_only(self):
        result = self.result(frame((1, triggered(b'g1:hello', 821, 999)
                                  + triggered(b'g1:hello suffix'))), 250)
        self.assertEqual(result['counts']['hello'], 1)
        self.assertEqual(result['publisher_pid'], 1)
        self.assertEqual(result['receiver_pairs'], [dict(pid=1, a=821, b=999, count=1)])
        self.assertNotIn('g1:hello', json.dumps(result))

    def test_header_crc_and_compressed_payload_crc(self):
        data = bytearray(packed(body()))
        data[60] ^= 1
        self.refuse(data, 'HEADER_CRC')
        data = bytearray(packed(body()))
        data[-1] ^= 1
        self.refuse(data, 'BLOCK_CRC')
        data = bytearray(packed(body()))
        data[72] ^= 1
        self.refuse(data, 'BLOCK_CRC')

    def test_zlib_failure_even_with_recomputed_outer_crc(self):
        data = bytearray(packed(body()))
        data[76] = 0
        d = binascii.crc32(data[76:])
        struct.pack_into('<H', data, 74, (d ^ (d >> 16)) & 65535)
        self.refuse(data, 'ZLIB_INVALID')

    def test_inflation_bomb_and_trailing_zlib_stream(self):
        original = packed(body())
        for payload, code in ((zlib.compress(bytes(8193)), 'ZLIB_BOUND_OR_STREAM'),
                              (zlib.compress(bytes(8192)) + zlib.compress(b'ignored'),
                               'ZLIB_BOUND_OR_STREAM')):
            sizes = struct.pack('<HH', len(payload), 8192)
            h, d = binascii.crc32(sizes + bytes(4)), binascii.crc32(payload)
            crc = ((h ^ (h >> 16)) & 65535) | (((d ^ (d >> 16)) & 65535) << 16)
            data = original[:68] + sizes + u32(crc) + payload
            self.refuse(reheader(data, 32, len(data)), code)

    def test_export_summary_without_command_never_invents_ack_epoch(self):
        records = frame((1, export(b'wire_version', 2) + export(b'receiver0', 1048667)
                         + export(b'receiver11', 1048678) + export(b'digest', 999)
                         + export(b'ack', 1)))
        result = self.result(records, 250)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['peers'], [])
        self.assertEqual(result['exports'][0]['digest_count'], 1)
        self.assertEqual(result['exports'][0]['receiver11'], 1048678)
        self.assertEqual(result['counts']['orphan_ack'], 1)
        self.assertEqual(result['receiver_binding'], 'UNVERIFIED')

    def test_truncation_every_offset(self):
        data = packed(body(frame((1, triggered(b'g1:hello')))), 250)
        for n in range(len(data)):
            self.refuse(data[:n])

    def test_truncated_inner_action_record_and_string(self):
        bad = b'\x1f' + u16(6) + u16(250) + b'\x01' + u16(8) + b'\x60'
        self.refuse(packed(body(bad), 250), 'TRUNCATED')
        self.refuse(packed(body(frame((1, b'\x60' + bytes(8) + b'no-null'))), 250),
                    'STRING_BOUND_OR_TRUNCATED')

    def test_unknown_record_or_opcode_no_partial_semantic_report(self):
        for records in (b'\xff', frame((1, triggered(b'g1:hello') + b'\xff'))):
            result = self.refuse(packed(body(records), 250 if records[0] == 0x1f else 0))
            self.assertEqual(result['container_integrity'], 'PASS')
            self.assertNotIn('receiver_pairs', result)

    def test_uncompressed_and_compressed_limits_without_large_allocations(self):
        data = packed(body())
        self.refuse(reheader(data, 40, 128 * 1024 * 1024), 'INFLATED_BOUND')
        self.refuse(reheader(data, 44, 16385), 'INFLATED_BOUND')
        with patch.object(audit, 'MAX_COMPRESSED', len(data) - 1):
            self.refuse(data, 'COMPRESSED_BOUND')
        with patch.object(audit, 'MAX_STEPS', 2):
            self.refuse(packed(body(frame((1, b'\x02' * 4))), 250), 'RECORD_BOUND')

    def test_multi_block_and_padding(self):
        records = frame(delta=0) * 2000
        result = self.result(records)
        self.assertEqual(result['code'], 'OK')
        self.assertGreater(result['compressed_blocks'], 1)
        self.assertEqual(result['timeslots'], 2000)
        data = packed(body())
        self.refuse(reheader(data, 40, len(body()) - 4), 'NONZERO_PADDING')

    def test_pause_resume_loading_leave_and_checksum(self):
        leave = b'\x17' + u32(1) + b'\x02' + u32(0) + u32(1)
        records = frame((1, b'\x01\x00\x02'), op=0x1e, delta=0) + b'\x22\x04' + u32(123)
        result = self.result(records, loading=leave)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['counts']['pause'], 1)
        self.assertEqual(result['counts']['resume'], 1)
        self.assertEqual(result['leave_records'], 1)
        self.assertEqual(result['checksum_records'], 1)

    def test_wrong_duration_client_pid_and_reserved_payload(self):
        self.refuse(packed(body(frame(delta=250))), 'DURATION_MISMATCH')
        self.refuse(reheader(packed(body()), 52, 29), 'UNSUPPORTED_CLIENT')
        self.refuse(packed(body(frame((99, b'\x02'))), 250), 'PID_INVALID')
        for text in (b'~g1:1:0:8:1', b'~g1:4294967296:1:8:1', b'~g1:1:1:8:1junk', b'~g1:01:1:8:1'):
            self.refuse(packed(body(frame((2, triggered(text)))), 250), 'G1_COMMAND_INVALID')

    def test_sequence_high_watermark_and_expired_probe(self):
        records = frame((1, export(b'wire_version', 2) + export(b'tick', 8)),
                        (2, triggered(b'~g1:42:0:8:0')))
        records += frame((1, export(b'tick', 29) + export(b'probe', 42)),
                         (2, triggered(b'~g1:42:5:29:1') + triggered(b'~g1:42:3:29:1')
                          + triggered(b'~g1:42:4:29:2')))
        result = self.result(records, 500)
        self.assertEqual(result['counts']['probe_causal'], 0)
        self.assertEqual(result['counts']['probe_noncausal'], 1)
        self.assertEqual([c['sequence_increasing'] for c in result['commands'][1:]],
                         [True, False, False])

    def test_summary_bound(self):
        with patch.object(audit, 'MAX_COMMANDS', 1):
            self.refuse(packed(body(frame((2, triggered(b'~g1:1:1:8:1')
                                              + triggered(b'~g1:1:2:16:2')))), 250), 'SUMMARY_BOUND')

    def test_cli_io_errors_never_echo_path_or_traceback(self):
        script = Path(__file__).with_name('replay-audit.py')
        proc = subprocess.run([sys.executable, str(script), '/nonexistent/PRIVATE_NAME_192.0.2.1'],
                              capture_output=True, text=True)
        self.assertEqual(json.loads(proc.stdout), {'code': 'INPUT_IO'})
        self.assertEqual(proc.stderr, '')
        self.assertEqual(proc.returncode, 1)


if __name__ == '__main__':
    unittest.main()
