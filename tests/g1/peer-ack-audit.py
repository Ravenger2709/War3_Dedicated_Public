#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Original stdlib audit of operator-supplied timestamped native G1 ACK logs.

Usage: python3 peer-ack-audit.py --epoch N --pids A B [--min-seconds 600]
Input is stdin, e.g. Docker logs --timestamps --since containerStartedAt.
This tool neither obtains logs nor proves that their senders are real engines.
Only exact anchored ACK records are parsed; all output is aggregate JSON.
"""
import argparse
import datetime
import io
import json
import re
import sys

MAX_INPUT = 8 * 1024 * 1024
MAX_LINE = 8 * 1024
MAX_RECORDS = 10000
MAX_EPOCH = (1 << 28) - 1
MAX_UINT32 = (1 << 32) - 1
# The anchored candidate discriminator never searches inside chat/log payloads.
CANDIDATE = re.compile(rb'\S+ \[G1\] ack(?: |$)')
ACK = re.compile(rb'(\S+) \[G1\] ack pid=([0-9]{1,10}) sequence=([0-9]{1,10}) digest=([0-9]{1,10}) host_counter=([0-9]{1,10})\Z')
STAMP = re.compile(rb'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z\Z')


class Refusal(Exception):
    """Contains a fixed local code, never input text."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def timestamp_ns(raw):
    match = STAMP.fullmatch(raw)
    require(match is not None, 'INVALID_TIMESTAMP')
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    try:
        date = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        raise Refusal('INVALID_TIMESTAMP') from None
    fraction = int((match[7] or b'').ljust(9, b'0'))
    return ((date.toordinal() * 86400 + hour * 3600 + minute * 60 + second)
            * 1000000000 + fraction)


def base_result(required_seconds):
    return dict(code='INSUFFICIENT_EVIDENCE', status='REFUSED',
                evidence_scope='SELECTED_EPOCH_TWO_PIDS_LOG_ONLY',
                engine_execution='OPERATOR_CONFIRMATION_REQUIRED',
                desync_proof='NOT_ESTABLISHED', required_seconds=required_seconds,
                digest_comparison_complete=False, paired_prefix_only=True,
                shutdown_reason='NOT_ESTABLISHED',
                input_bytes=0, ignored_lines=0, ack_records=0, other_epoch_records=0,
                selected_records=0, peer_a_records=0, peer_b_records=0,
                paired_sequences=0, paired_prefix_sequences=0,
                digest_mismatches=0, duplicate_peer_records=0,
                unexpected_peer_records=0, missing_sequences=0,
                interior_unpaired_sequences=0, unpaired_trailing_sequences=0,
                trailing_sequence_1=None, trailing_present_pid_1=None,
                trailing_sequence_2=None, trailing_present_pid_2=None,
                paired_span_ns=0, paired_span_seconds=0)


def audit(stream, *, epoch, pids, min_seconds=600):
    """Audit bytes/a binary stream. Helper accepts min_seconds=0 for unit tests.

    Pair duration is last pair-completion time minus first pair-completion
    time, never inflated by a shutdown tail or another epoch. Timestamps must
    be nondecreasing across all recognized ACKs (ties are permitted).
    """
    result = base_result(min_seconds if type(min_seconds) is int else 600)
    try:
        require(type(epoch) is int and 1 <= epoch <= MAX_EPOCH, 'EPOCH_ARGUMENT')
        require(isinstance(pids, (tuple, list)) and len(pids) == 2
                and all(type(pid) is int and 1 <= pid <= 254 for pid in pids)
                and pids[0] != pids[1], 'PIDS_ARGUMENT')
        require(type(min_seconds) is int and 0 <= min_seconds <= MAX_UINT32,
                'DURATION_ARGUMENT')
        first_pid, second_pid = pids
        result.update(epoch=epoch, peer_a_pid=first_pid, peer_b_pid=second_pid)
        peers = {first_pid: {}, second_pid: {}}
        last_stamp = None
        last_sequence = {}
        if isinstance(stream, bytes):
            stream = io.BytesIO(stream)
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            result['input_bytes'] += len(line)
            require(result['input_bytes'] <= MAX_INPUT, 'INPUT_BOUND')
            require(len(line) <= MAX_LINE, 'LINE_BOUND')
            if line.endswith(b'\n'):
                line = line[:-1]
                if line.endswith(b'\r'):
                    line = line[:-1]
            if not CANDIDATE.match(line):
                result['ignored_lines'] += 1
                continue
            result['ack_records'] += 1
            require(result['ack_records'] <= MAX_RECORDS, 'RECORD_BOUND')
            match = ACK.fullmatch(line)
            require(match is not None, 'ACK_FORMAT')
            stamp = timestamp_ns(match[1])
            require(last_stamp is None or stamp >= last_stamp, 'TIMESTAMP_ORDER')
            last_stamp = stamp
            pid, sequence, digest, counter = map(int, match.groups()[1:])
            require(1 <= pid <= 254 and 1 <= sequence <= MAX_UINT32
                    and 0 <= digest <= MAX_UINT32 and 1 <= counter <= MAX_EPOCH,
                    'ACK_INTEGER_RANGE')
            if counter != epoch:
                result['other_epoch_records'] += 1
                continue
            result['selected_records'] += 1
            if pid not in peers:
                result['unexpected_peer_records'] += 1
                raise Refusal('UNEXPECTED_PEER')
            peer = peers[pid]
            if sequence in peer:
                result['duplicate_peer_records'] += 1
                raise Refusal('DUPLICATE_PEER_ACK')
            require(sequence > last_sequence.get(pid, 0), 'PEER_SEQUENCE_ORDER')
            last_sequence[pid] = sequence
            peer[sequence] = (digest, stamp)
            result['peer_a_records' if pid == first_pid else 'peer_b_records'] += 1
        a, b = peers[first_pid], peers[second_pid]
        union = a.keys() | b.keys()
        shared = a.keys() & b.keys()
        result['paired_sequences'] = len(shared)
        result['digest_mismatches'] = sum(a[seq][0] != b[seq][0] for seq in shared)
        result['digest_comparison_complete'] = True
        prefix = 0
        while prefix + 1 in shared:
            prefix += 1
        result['paired_prefix_sequences'] = prefix
        maximum = max(union, default=0)
        last_shared = max(shared, default=0)
        result['missing_sequences'] = maximum - len(union)
        unpaired = sorted(union - shared)
        result['interior_unpaired_sequences'] = sum(seq <= last_shared for seq in unpaired)
        tail = [seq for seq in unpaired if seq > last_shared]
        result['unpaired_trailing_sequences'] = len(tail)
        for index, seq in enumerate(tail[:2], 1):
            result['trailing_sequence_' + str(index)] = seq
            result['trailing_present_pid_' + str(index)] = first_pid if seq in a else second_pid
        if prefix:
            # Completion means BOTH peers have ACKed that sequence.
            start = max(a[1][1], b[1][1])
            end = max(a[prefix][1], b[prefix][1])
            result['paired_span_ns'] = end - start
            result['paired_span_seconds'] = (end - start) / 1000000000
        require(result['selected_records'] > 0, 'NO_SELECTED_EPOCH_ACKS')
        require(result['digest_mismatches'] == 0, 'DIGEST_MISMATCH')
        require(result['missing_sequences'] == 0, 'MISSING_SEQUENCE')
        require(result['interior_unpaired_sequences'] == 0, 'MISSING_INTERIOR_ACK')
        require(prefix > 0, 'NO_PAIRED_PREFIX')
        require(len(shared) == prefix, 'MISSING_PAIRED_PREFIX')
        require(len(tail) <= 2, 'EXCESS_UNPAIRED_TAIL')
        require(result['paired_span_ns'] >= min_seconds * 1000000000,
                'INSUFFICIENT_DURATION')
        result.update(code='OK', status='CONSISTENT_TWO_PEER_ACKS')
    except Refusal as error:
        result['code'] = error.args[0]
    except (OSError, ValueError):
        result['code'] = 'INPUT_IO'
    return result


class FixedArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal('ARGUMENTS')


def argument_integer(raw):
    require(re.fullmatch(r'[0-9]{1,10}', raw) is not None, 'ARGUMENTS')
    return int(raw)


def main(argv=None, stdin=None):
    required = 600
    try:
        parser = FixedArgumentParser(prog='peer-ack-audit', add_help=False)
        parser.add_argument('--epoch', required=True)
        parser.add_argument('--pids', nargs=2, required=True)
        parser.add_argument('--min-seconds', default='600')
        args = parser.parse_args(argv)
        epoch = argument_integer(args.epoch)
        pids = [argument_integer(pid) for pid in args.pids]
        required = argument_integer(args.min_seconds)
        # Zero-duration evidence is reserved for the helper's synthetic tests.
        require(required > 0, 'DURATION_ARGUMENT')
        result = audit(sys.stdin.buffer if stdin is None else stdin,
                       epoch=epoch, pids=pids, min_seconds=required)
    except Refusal as error:
        result = base_result(required)
        result['code'] = error.args[0]
    except KeyboardInterrupt:
        result = base_result(required)
        result['code'] = 'INTERRUPTED'
    except Exception:
        result = base_result(required)
        result['code'] = 'INTERNAL_ERROR'
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['code'] == 'OK' else 1


if __name__ == '__main__':
    sys.exit(main())
