# SPDX-License-Identifier: Apache-2.0
"""Offline order-channel hypothesis; never connected to native or game code.

Input records stand for already-authenticated, genuine client actions ready in
ONE normal aggregation tick. Bytes alone cannot establish that provenance.
Only an exact adjacent A/B marker pair at the head may change order. A,B means
bit 0; B,A means bit 1. The two SDK SyncStoredInteger-shaped reference payloads
write the SAME cache/mission/key, with A=1 and B=0. No action is manufactured by
apply(), mutated, split, dropped, retained, delayed, or retried.

Receiver ASSUMPTION ONLY: this model conservatively selects a THIRD genuine
observer to reduce sender self-sync ambiguity, not because the engine is known
to require three clients. It might read the common cache and relay a result
using its own genuine SyncStoredInteger. A=1/B=0 would map last-winner to the
ordered bit and first-winner to its inverse; neither winner rule is established.
Sender self-sync, callback timing, winner selection, and observer relay remain
UNVERIFIED. No game-side receiver or ACK is simulated, no spare player/slot is
allocated, and no engine delivery is proved. Record invariants need no winner
assumption.
"""
from dataclasses import dataclass
import json
import struct
import zlib

STATUS = 'MODEL_ONLY_ENGINE_NOT_RUN'
NO_SYMBOL = 'NO_SYMBOL'
MAX_MESSAGE_BYTES = 64
MAX_ROUNDS = (3 + 1 + MAX_MESSAGE_BYTES + 4) * 8
MAX_RECORDS = 32
MAX_ACTION_BYTES = 1024  # CIncomingAction::GetLength adds 3; native cap is 1027.
MAX_BODY_BYTES = 1452  # one native normal packet, at most 1460 bytes with CRC
_CACHE = b'\x6bG1ORDER.w3v\0'
_MAGIC = b'G1O'


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


@dataclass(frozen=True)
class Record:
    pid: int
    payload: bytes


@dataclass(frozen=True)
class Result:
    records: tuple
    symbol: object = NO_SYMBOL  # encoded intent, NEVER a received/acknowledged bit
    reason: str = 'INELIGIBLE'
    status: str = STATUS


def marker_payload(side, nonce, round_id):
    """Reference bytes for matching/fixtures, NOT an injection/send operation.

    Both clients must have independently supplied these exact fixed payloads.
    Each record must contain only this single integer sync action.
    """
    if not (_integer(side, 0, 1) and _integer(nonce, 1, 0x7fffffff)
            and _integer(round_id, 0, MAX_ROUNDS - 1)):
        raise ValueError('invalid marker parameters')
    mission = f'n{nonce:08x}'.encode('ascii') + b'\0'
    key = f'r{round_id:03d}'.encode('ascii') + b'\0'
    return _CACHE + mission + key + struct.pack('<I', 1 - side)


def _batch_reason(ready):
    # Never materialize an unbounded iterable or copy a payload to validate it.
    if type(ready) is not tuple or len(ready) > MAX_RECORDS:
        return 'RECORD_BOUND'
    size = 0
    for record in ready:
        if (type(record) is not Record or not _integer(record.pid, 1, 254)
                or type(record.payload) is not bytes):
            return 'RECORD_TYPE'
        if len(record.payload) > MAX_ACTION_BYTES:
            return 'ACTION_BOUND'
        size += 3 + len(record.payload)
        if size > MAX_BODY_BYTES:
            return 'BATCH_BOUND'
    return None


def _participants(pids):
    return (type(pids) is tuple and 1 <= len(pids) <= 12
            and all(_integer(pid, 1, 254) for pid in pids)
            and len(set(pids)) == len(pids))


class OrderChannel:
    """Bounded, queue-free scheduler. Every in-range round attempt is consumed.

    Failed/paused/missing rounds have no symbol and cannot be retried later;
    ready records immediately return unchanged for the caller's normal flush.
    No time source, sleep, waiting queue, retransmission, or per-recipient path.
    """
    def __init__(self, senders, nonce):
        if not (_participants(senders) and len(senders) == 2
                and _integer(nonce, 1, 0x7fffffff)):
            raise ValueError('two distinct real PID claims and a valid nonce required')
        self.senders = senders
        self.nonce = nonce
        self._last_round = -1

    def apply(self, ready, round_id, bit, *, running=True, backlog=False):
        def refused(reason):
            return Result(ready, reason=reason)

        if not _integer(round_id, 0, MAX_ROUNDS - 1):
            return refused('ROUND_BOUND')
        if round_id <= self._last_round:
            return refused('ROUND_REPLAY')
        self._last_round = round_id
        if not _integer(bit, 0, 1):
            return refused('BIT_BOUND')
        if running is not True:
            return refused('PAUSED')
        if backlog is not False:
            return refused('BACKLOG')
        problem = _batch_reason(ready)
        if problem:
            return refused(problem)
        if len(ready) < 2 or {ready[0].pid, ready[1].pid} != set(self.senders):
            return refused('HEAD_PAIR_REQUIRED')
        for record in ready[:2]:
            side = self.senders.index(record.pid)
            if record.payload != marker_payload(side, self.nonce, round_id):
                return refused('MARKER_MISMATCH')
        # A further channel record signals duplicate/queued/coalesced markers.
        # Do not scan inside unrelated action payloads, or search for a later pair.
        if any(record.payload.startswith(_CACHE) for record in ready[2:]):
            return refused('BACKLOG')
        first_pid = self.senders[bit]
        ordered = ready if ready[0].pid == first_pid else (ready[1], ready[0]) + ready[2:]
        return Result(ordered, bit, 'ORDERED')

    def observer_eligible(self, observer_pid, genuine_pids):
        """Conservative model policy, NOT an established engine requirement."""
        return (_participants(genuine_pids) and len(genuine_pids) >= 3
                and _integer(observer_pid, 1, 254) and observer_pid in genuine_pids
                and observer_pid not in self.senders
                and all(pid in genuine_pids for pid in self.senders))


def normal_packet(ready, interval_ms):
    """Offline W3GS 0x0c serializer, CRC of unchanged PID/length/action records.

    Over-capacity input is refused here, never split into overflow or delayed.
    apply() still returns such input untouched for ordinary native handling.
    """
    if _batch_reason(ready) or not _integer(interval_ms, 0, 65535):
        raise ValueError('normal packet bounds')
    body = b''.join(bytes((r.pid,)) + struct.pack('<H', len(r.payload)) + r.payload
                    for r in ready)
    content = struct.pack('<H', interval_ms)
    if ready:
        content += struct.pack('<H', zlib.crc32(body) & 0xffff) + body
    return b'\xf7\x0c' + struct.pack('<H', len(content) + 4) + content


def recipient_views(ready, genuine_pids, interval_ms):
    """One identical normal packet for everyone, including both senders."""
    if not _participants(genuine_pids):
        raise ValueError('invalid recipient PID claims')
    packet = normal_packet(ready, interval_ms)
    return {pid: packet for pid in genuine_pids}


def frame_bits(message):
    """Bounded codec demonstration, MSB first; no delivery/retry mechanism."""
    if type(message) is not bytes or len(message) > MAX_MESSAGE_BYTES:
        raise ValueError('message must be 0..64 bytes')
    body = _MAGIC + bytes((len(message),)) + message
    frame = body + struct.pack('<I', zlib.crc32(body))
    return tuple((value >> shift) & 1 for value in frame for shift in range(7, -1, -1))


def unframe_bits(bits):
    if (type(bits) is not tuple or not 64 <= len(bits) <= MAX_ROUNDS or len(bits) % 8
            or any(not _integer(bit, 0, 1) for bit in bits)):
        raise ValueError('invalid bit frame')
    raw = bytes(sum(bits[i + j] << (7 - j) for j in range(8))
                for i in range(0, len(bits), 8))
    if raw[:3] != _MAGIC or raw[3] > MAX_MESSAGE_BYTES or len(raw) != raw[3] + 8:
        raise ValueError('invalid frame length or marker')
    if struct.unpack('<I', raw[-4:])[0] != zlib.crc32(raw[:-4]):
        raise ValueError('frame CRC mismatch')
    return raw[4:-4]


if __name__ == '__main__':
    print(json.dumps({'status': STATUS, 'engine_run': False,
                      'model_receiver_policy': 'THIRD_GENUINE_OBSERVER',
                      'engine_requires_third_client': 'UNVERIFIED',
                      'winner_timing_and_self_sync': 'ASSUMPTION_UNVERIFIED',
                      'max_message_bytes': MAX_MESSAGE_BYTES,
                      'max_rounds': MAX_ROUNDS}))
