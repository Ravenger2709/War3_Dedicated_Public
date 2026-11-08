# SPDX-License-Identifier: Apache-2.0
"""Independent synthetic raw log fixtures; no Docker, private logs or engine."""
import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('peer_ack_audit', Path(__file__).with_name('peer-ack-audit.py'))
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

# Literal known-good records are independent of the auditor's parser/formatter.
GOOD = (b'2026-11-08T10:00:00.000000001Z [G1] ack pid=1 sequence=1 digest=123 host_counter=42\n'
        b'2026-11-08T10:00:00.000000002Z [G1] ack pid=2 sequence=1 digest=123 host_counter=42\n'
        b'2026-11-08T10:10:00.000000001Z [G1] ack pid=1 sequence=2 digest=456 host_counter=42\n'
        b'2026-11-08T10:10:00.000000002Z [G1] ack pid=2 sequence=2 digest=456 host_counter=42\n')


def line(pid, seq, digest=123, seconds=0, epoch=42):
    # Fixture-only formatting, not imported from the implementation.
    stamp = datetime.datetime(2026, 11, 8, 10) + datetime.timedelta(seconds=seconds)
    return (stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
            + f' [G1] ack pid={pid} sequence={seq} digest={digest} host_counter={epoch}\n').encode()


class PeerAckAuditTests(unittest.TestCase):
    def check_logs(self, raw, **kw):
        return checker.audit(raw, epoch=42, pids=(1, 2), **kw)

    def test_literal_positive_and_fixed_evidence_scope(self):
        result = self.check_logs(GOOD)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['status'], 'CONSISTENT_TWO_PEER_ACKS')
        self.assertEqual(result['paired_sequences'], 2)
        self.assertEqual(result['paired_prefix_sequences'], 2)
        self.assertEqual(result['paired_span_ns'], 600000000000)
        self.assertEqual(result['required_seconds'], 600)
        self.assertEqual(result['engine_execution'], 'OPERATOR_CONFIRMATION_REQUIRED')
        self.assertEqual(result['desync_proof'], 'NOT_ESTABLISHED')
        self.assertTrue(all(not isinstance(v, (list, dict)) for v in result.values()))

    def test_earlier_mismatches_not_hidden_by_matching_last_pair(self):
        raw = b''.join(line(pid, seq, seq if pid == 1 or seq == 3 else 999,
                            seq * 300) for seq in (1, 2, 3) for pid in (1, 2))
        result = self.check_logs(raw)
        self.assertEqual(result['code'], 'DIGEST_MISMATCH')
        self.assertEqual(result['digest_mismatches'], 2)
        self.assertEqual(result['paired_sequences'], 3)
        self.assertTrue(result['digest_comparison_complete'])

    def test_duplicate_same_pid_is_never_a_second_peer(self):
        for raw in (line(1, 1) * 2, GOOD + line(2, 2, 456, 601)):
            result = self.check_logs(raw)
            self.assertEqual(result['code'], 'DUPLICATE_PEER_ACK')
            self.assertEqual(result['duplicate_peer_records'], 1)
            self.assertFalse(result['digest_comparison_complete'])
            self.assertEqual(result['status'], 'REFUSED')

    def test_missing_interior_peer_ack_even_with_later_agreement(self):
        raw = line(1, 1) + line(2, 1) + line(1, 2, seconds=300)
        raw += line(1, 3, seconds=600) + line(2, 3, seconds=600)
        result = self.check_logs(raw)
        self.assertEqual(result['code'], 'MISSING_INTERIOR_ACK')
        self.assertEqual(result['interior_unpaired_sequences'], 1)

    def test_missing_whole_sequence_and_starting_after_one(self):
        for raw in (line(1, 2) + line(2, 2),
                    line(1, 1) + line(2, 1) + line(1, 3, seconds=600) + line(2, 3, seconds=600)):
            result = self.check_logs(raw)
            self.assertEqual(result['code'], 'MISSING_SEQUENCE')
            self.assertEqual(result['missing_sequences'], 1)

    def test_one_or_two_shutdown_tail_sequences_reported(self):
        for size in (1, 2):
            raw = GOOD + b''.join(line(1, seq, seconds=601 + seq) for seq in range(3, 3 + size))
            result = self.check_logs(raw)
            self.assertEqual(result['code'], 'OK')
            self.assertEqual(result['unpaired_trailing_sequences'], size)
            self.assertEqual(result['trailing_sequence_1'], 3)
            self.assertEqual(result['trailing_present_pid_1'], 1)
            self.assertEqual(result['paired_span_ns'], 600000000000)
            if size == 2:
                self.assertEqual(result['trailing_sequence_2'], 4)

    def test_three_shutdown_tail_sequences_refused(self):
        result = self.check_logs(GOOD + b''.join(line(1, seq, seconds=601 + seq) for seq in (3, 4, 5)))
        self.assertEqual(result['code'], 'EXCESS_UNPAIRED_TAIL')
        self.assertEqual(result['unpaired_trailing_sequences'], 3)

    def test_unexpected_or_wrong_pid(self):
        for raw in (line(3, 1), GOOD + line(3, 3, seconds=601)):
            result = self.check_logs(raw)
            self.assertEqual(result['code'], 'UNEXPECTED_PEER')
        for pids in ((1, 1), (0, 2), (1, 255), (1,), (True, 2)):
            self.assertEqual(checker.audit(b'', epoch=42, pids=pids)['code'], 'PIDS_ARGUMENT')

    def test_mixed_epochs_cannot_fill_selected_missing_peer(self):
        raw = line(1, 1) + line(2, 1, epoch=43)
        result = self.check_logs(raw, min_seconds=0)
        self.assertEqual(result['code'], 'NO_PAIRED_PREFIX')
        self.assertEqual(result['paired_sequences'], 0)
        self.assertEqual(result['other_epoch_records'], 1)
        foreign = line(200, 99, epoch=43) + GOOD
        result = self.check_logs(foreign)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['other_epoch_records'], 1)

    def test_short_pair_span_not_extended_by_tail(self):
        raw = line(1, 1) + line(2, 1) + line(1, 2, seconds=599) + line(2, 2, seconds=599)
        raw += line(1, 3, seconds=900)
        result = self.check_logs(raw)
        self.assertEqual(result['code'], 'INSUFFICIENT_DURATION')
        self.assertEqual(result['paired_span_seconds'], 599)
        self.assertEqual(result['unpaired_trailing_sequences'], 1)

    def test_pair_completion_span_not_first_single_peer_time(self):
        raw = line(1, 1) + line(2, 1, seconds=100)
        raw += line(1, 2, seconds=600) + line(2, 2, seconds=600)
        self.assertEqual(self.check_logs(raw)['paired_span_seconds'], 500)
        self.assertEqual(self.check_logs(raw)['code'], 'INSUFFICIENT_DURATION')

    def test_chat_prefix_suffix_and_whitespace_spoofs_ignored_or_refused(self):
        bait = line(1, 1)
        ignored = (b'CHAT from PRIVATE_NAME_192.0.2.1 ' + bait
                   + b'2026-11-08T10:00:00Z [CHAT] text=' + bait
                   + b' ' + bait + b'noise\n')
        result = self.check_logs(ignored + GOOD)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['selected_records'], 4)
        self.assertNotIn('PRIVATE_NAME', json.dumps(result))
        self.assertNotIn('192.0.2.1', json.dumps(result))
        suffix = bait.rstrip(b'\n') + b' user chat\n'
        self.assertEqual(self.check_logs(suffix)['code'], 'ACK_FORMAT')

    def test_timestamp_validation_order_ties_and_nanoseconds(self):
        for stamp in (b'garbage', b'2026-02-30T10:00:00Z', b'2026-11-08T25:00:00Z',
                      b'2026-11-08T10:00:00+00:00', b'2026-11-08T10:00:00.1234567890Z'):
            raw = stamp + b' [G1] ack pid=1 sequence=1 digest=1 host_counter=42\n'
            self.assertEqual(self.check_logs(raw)['code'], 'INVALID_TIMESTAMP')
        self.assertEqual(self.check_logs(line(1, 1, seconds=1) + line(2, 1))['code'], 'TIMESTAMP_ORDER')
        self.assertEqual(self.check_logs(line(1, 1) + line(2, 1), min_seconds=0)['code'], 'OK')
        short = GOOD.replace(b'10:10:00.000000002Z', b'10:10:00.000000001Z')
        self.assertEqual(self.check_logs(short)['paired_span_ns'], 599999999999)
        self.assertEqual(self.check_logs(short)['code'], 'INSUFFICIENT_DURATION')

    def test_integer_ranges_and_partial_ack_refusal(self):
        for raw in (line(0, 1), line(255, 1), line(1, 0), line(1, 2**32),
                    line(1, 1, digest=2**32), line(1, 1, epoch=0), line(1, 1, epoch=2**28)):
            self.assertEqual(self.check_logs(raw)['code'], 'ACK_INTEGER_RANGE')
        malformed = b'2026-11-08T10:00:00Z [G1] ack pid=1\n'
        self.assertEqual(self.check_logs(malformed)['code'], 'ACK_FORMAT')

    def test_peer_sequence_regression(self):
        raw = line(1, 2) + line(1, 1, seconds=1)
        self.assertEqual(self.check_logs(raw)['code'], 'PEER_SEQUENCE_ORDER')

    def test_empty_or_one_peer_not_sufficient(self):
        self.assertEqual(self.check_logs(b'noise\n')['code'], 'NO_SELECTED_EPOCH_ACKS')
        self.assertEqual(self.check_logs(line(1, 1), min_seconds=0)['code'], 'NO_PAIRED_PREFIX')

    def test_input_line_and_record_bounds(self):
        with patch.object(checker, 'MAX_INPUT', 10):
            self.assertEqual(self.check_logs(b'noise\nnoise\n')['code'], 'INPUT_BOUND')
        self.assertEqual(self.check_logs(b'x' * (8192 + 1))['code'], 'LINE_BOUND')
        with patch.object(checker, 'MAX_RECORDS', 3):
            self.assertEqual(self.check_logs(GOOD)['code'], 'RECORD_BOUND')
        with patch.object(checker, 'MAX_RECORDS', 1):
            self.assertEqual(self.check_logs(line(1, 1, epoch=43) * 2)['code'], 'RECORD_BOUND')

    def test_foreign_epoch_cannot_inflate_pair_duration(self):
        raw = line(1, 1) + line(2, 1) + line(1, 2, seconds=5) + line(2, 2, seconds=5)
        raw += line(1, 999, seconds=900, epoch=43) + line(2, 999, seconds=901, epoch=43)
        result = self.check_logs(raw)
        self.assertEqual(result['code'], 'INSUFFICIENT_DURATION')
        self.assertEqual(result['paired_span_seconds'], 5)
        self.assertEqual(result['other_epoch_records'], 2)
        self.assertTrue(result['paired_prefix_only'])
        self.assertEqual(result['shutdown_reason'], 'NOT_ESTABLISHED')

    def test_cli_default_duration_and_redacted_argument_errors(self):
        path = Path(__file__).with_name('peer-ack-audit.py')
        proc = subprocess.run([sys.executable, str(path), '--epoch', '42', '--pids', '1', '2'],
                              input=GOOD, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)['required_seconds'], 600)
        self.assertEqual(proc.stderr, b'')
        for args in (['--epoch', 'PRIVATE_NAME_192.0.2.1', '--pids', '1', '2'],
                     ['--epoch', '42', '--pids', '1', '2', '--min-seconds', '0'],
                     ['--unexpected', '/PRIVATE_PATH']):
            proc = subprocess.run([sys.executable, str(path), *args], input=b'', capture_output=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stderr, b'')
            self.assertEqual(json.loads(proc.stdout)['status'], 'REFUSED')
            self.assertNotIn(b'PRIVATE', proc.stdout)


if __name__ == '__main__':
    unittest.main()
