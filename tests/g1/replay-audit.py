#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Original stdlib auditor for classic native GHost replay output, not playback.

Grammar reference: local ghost/replay.cpp BuildReplay/AddTimeSlot* and
packed.cpp Compress. No external parser code is incorporated. Unsupported
records/actions refuse the entire semantic report; nothing is substring-scanned.
CLI: python3 tests/g1/replay-audit.py REPLAY ; stdout is redacted JSON only.
Replay order is evidence, not proof of original network-batch/engine causality.
"""
import json
import os
import re
import stat
import struct
import sys
import zlib

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_INFLATED = 128 * 1024 * 1024
MAX_STEPS = 1000000
MAX_COMMANDS = 4096
MAGIC = b'Warcraft III recorded game\x1a\0'
COMMAND = re.compile(rb'~g1:([0-9]{1,10}):([0-9]{1,10}):([0-9]{1,10}):([0-9]{1,10})\Z')
KEYS = {b'wire_version', b'receiver0', b'receiver11', b'tick', b'probe', b'digest', b'ack'}


class Refusal(Exception):
    """Only fixed, locally authored codes may cross the output boundary."""


def need(condition, code):
    if not condition:
        raise Refusal(code)


class Cursor:
    def __init__(self, data):
        self.data = memoryview(data)
        self.pos = 0

    def left(self):
        return len(self.data) - self.pos

    def take(self, count):
        need(0 <= count <= self.left(), 'TRUNCATED')
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def number(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def u8(self):
        return self.number('<B')

    def u16(self):
        return self.number('<H')

    def u32(self):
        return self.number('<I')

    def string(self, limit=4096):
        start = self.pos
        while self.left() and self.pos - start <= limit:
            if self.u8() == 0:
                return bytes(self.data[start:self.pos - 1])
        raise Refusal('STRING_BOUND_OR_TRUNCATED')

    def expect(self, value):
        need(self.take(len(value)) == value, 'NATIVE_GRAMMAR')


def fold_crc(data):
    value = zlib.crc32(data)
    return (value ^ (value >> 16)) & 65535


def unpack(data):
    need(len(data) <= MAX_COMPRESSED, 'COMPRESSED_BOUND')
    c = Cursor(data)
    c.expect(MAGIC)
    header_size, packed_size, version, size, blocks = struct.unpack('<5I', c.take(20))
    need(header_size == 68 and version == 1, 'UNSUPPORTED_HEADER')
    need(packed_size == len(data), 'PACKED_SIZE')
    need(size < MAX_INFLATED and 0 < blocks <= MAX_INFLATED // 8192, 'INFLATED_BOUND')
    need(blocks == size // 8192 + 1, 'NATIVE_PADDING_SIZE')
    c.expect(b'PX3W')
    war_version, build, flags, duration, crc = struct.unpack('<IHHII', c.take(16))
    need(war_version == 26 and build == 6401 and flags == 32768, 'UNSUPPORTED_CLIENT')
    need(zlib.crc32(bytes(c.data[:64]) + bytes(4)) == crc, 'HEADER_CRC')
    out = bytearray()
    for _ in range(blocks):
        compressed, inflated, checksum = struct.unpack('<HHI', c.take(8))
        need(0 < compressed <= 8213 and inflated == 8192, 'BLOCK_SIZE')
        payload = c.take(compressed)
        expected = fold_crc(struct.pack('<HHI', compressed, inflated, 0)) | (fold_crc(payload) << 16)
        need(expected == checksum, 'BLOCK_CRC')
        try:
            decoder = zlib.decompressobj()
            piece = decoder.decompress(payload, 8193)
        except zlib.error:
            raise Refusal('ZLIB_INVALID') from None
        need(len(piece) == 8192 and decoder.eof and not decoder.unconsumed_tail
             and not decoder.unused_data, 'ZLIB_BOUND_OR_STREAM')
        out.extend(piece)
    need(not c.left(), 'TRAILING_COMPRESSED')
    need(not any(memoryview(out)[size:]), 'NONZERO_PADDING')
    del out[size:]
    return out, duration, blocks


# Sizes exclude the opcode. Variable-length fields are parsed separately.
FIXED = {0x01: 0, 0x02: 0, 0x03: 1, 0x04: 0, 0x05: 0, 0x07: 4,
         0x10: 14, 0x11: 22, 0x12: 30, 0x13: 38, 0x14: 43, 0x15: 51,
         0x18: 2, 0x19: 12, 0x1a: 0, 0x1b: 9, 0x1c: 9, 0x1d: 8,
         0x1e: 5, 0x1f: 5, 0x50: 5, 0x51: 9, 0x61: 0, 0x62: 12,
         0x63: 8, 0x64: 8, 0x65: 8, 0x66: 0, 0x67: 0}


class Summary:
    def __init__(self, colours):
        self.colours = colours
        self.steps = self.batch = self.action = 0
        self.publisher = None
        self.clock = {}
        self.digests = {}
        self.pending = None
        self.probe = None
        self.exports = {}
        self.groups = {}
        self.pairs = {}
        self.commands = []
        self.sequences = {}
        self.counts = dict(hello=0, foreign_chat=0, foreign_cache=0, other_exports=0,
                           pause=0, resume=0, orphan_ack=0, duplicate_ack=0,
                           orphan_probe=0, probe_causal=0, probe_noncausal=0,
                           clock_regressions=0, digest_mismatches=0)

    def step(self):
        self.steps += 1
        need(self.steps <= MAX_STEPS, 'RECORD_BOUND')

    def pair(self, pid, a, b):
        key = (pid, a, b)
        need(key in self.pairs or len(self.pairs) < 64, 'SUMMARY_BOUND')
        self.pairs[key] = self.pairs.get(key, 0) + 1

    def group(self, pid, epoch):
        key = (pid, epoch)
        need(key in self.groups or len(self.groups) < 512, 'SUMMARY_BOUND')
        return self.groups.setdefault(key, dict(pid=pid, epoch=epoch, ack_count=0,
                    probe_count=0, digest_count=0, last_digest=0, last_ack_sequence=0))

    def observe_chat(self, pid, a, b, text):
        if text == b'g1:hello':
            self.pair(pid, a, b)
            self.counts['hello'] += 1
            if self.colours.get(pid) == 0 and self.publisher is None:
                self.publisher = pid
            return
        match = COMMAND.fullmatch(text)
        if not match:
            need(not text.startswith(b'~g1:'), 'G1_COMMAND_INVALID')
            self.counts['foreign_chat'] += 1
            return
        epoch, seq, tick, kind = map(int, match.groups())
        need(all(n <= 0xffffffff for n in (epoch, seq, tick, kind)) and epoch > 0,
             'G1_COMMAND_INVALID')
        need(text == ('~g1:%d:%d:%d:%d' % (epoch, seq, tick, kind)).encode('ascii'),
             'G1_COMMAND_INVALID')
        need((seq == 0 and kind == 0) or (seq > 0 and kind in (1, 2)), 'G1_COMMAND_INVALID')
        need(len(self.commands) < MAX_COMMANDS, 'SUMMARY_BOUND')
        self.pair(pid, a, b)
        command = dict(sender_pid=pid, epoch=epoch, sequence=seq, source_tick=tick,
                       kind=kind, receiver_a=a, receiver_b=b, ack_count=0,
                       probe_ack_count=0, sequence_increasing=True)
        self.commands.append(command)
        pending = (command, self.action, self.batch, set())
        if seq == 0:
            self.probe = pending
        else:
            command['sequence_increasing'] = seq > self.sequences.get(epoch, 0)
            self.sequences[epoch] = max(seq, self.sequences.get(epoch, 0))
            self.pending = pending

    def observe_cache(self, pid, key, value):
        exports = self.exports.setdefault(pid, dict(pid=pid, wire_version=0,
                    receiver0=0, receiver11=0, tick=0, digest=0, ack=0, probe=0,
                    wire_version_count=0, receiver0_count=0, receiver11_count=0,
                    tick_count=0, digest_count=0, ack_count=0, probe_count=0))
        label = key.decode('ascii')  # callers whitelist these seven fixed keys
        exports[label] = value
        exports[label + '_count'] += 1
        if key == b'wire_version':
            if value == 2 and self.colours.get(pid) == 0 and self.publisher is None:
                self.publisher = pid
        elif key == b'tick':
            if value <= self.clock.get(pid, -1):
                self.counts['clock_regressions'] += 1
            self.clock[pid] = value
        elif key == b'digest':
            self.digests[pid] = (value, self.action)
        elif key == b'probe':
            if self.probe is None or value != self.probe[0]['epoch']:
                self.counts['orphan_probe'] += 1
                return
            command, order, batch, seen = self.probe
            group = self.group(pid, value)
            group['probe_count'] += 1
            command['probe_ack_count'] += 1
            tick = self.clock.get(pid, -1)
            causal = (pid == self.publisher and self.action > order and self.batch > batch
                      and command['source_tick'] < tick <= command['source_tick'] + 20)
            self.counts['probe_causal' if causal else 'probe_noncausal'] += 1
        elif key == b'ack':
            if self.pending is None or value != self.pending[0]['sequence']:
                self.counts['orphan_ack'] += 1
                return
            command, order, batch, seen = self.pending
            digest = self.digests.get(pid)
            if not digest or digest[1] <= order or self.action <= order or self.batch <= batch:
                self.counts['orphan_ack'] += 1
                return
            if pid in seen:
                self.counts['duplicate_ack'] += 1
                return
            seen.add(pid)
            command['ack_count'] += 1
            group = self.group(pid, command['epoch'])
            group['ack_count'] += 1
            group['digest_count'] += 1
            group['last_digest'] = digest[0]
            group['last_ack_sequence'] = value
            values = [self.group(p, command['epoch'])['last_digest'] for p in seen]
            if len(set(values)) > 1:
                self.counts['digest_mismatches'] += 1
        else:
            self.counts['other_exports'] += 1

    def actions(self, pid, raw):
        c = Cursor(raw)
        while c.left():
            self.step()
            self.action += 1
            op = c.u8()
            if op in FIXED:
                c.take(FIXED[op])
                if op in (1, 2):
                    self.counts['pause' if op == 1 else 'resume'] += 1
            elif op in (0x16, 0x17):
                c.u8()
                c.take(c.u16() * 8)
            elif op == 6:
                c.string(128)
            elif op == 0x60:
                a, b = c.u32(), c.u32()
                self.observe_chat(pid, a, b, c.string(128))
            elif op in (0x6b, 0x6c, 0x6d):
                cache, mission, key = c.string(128), c.string(128), c.string(128)
                value = c.u32()
                if op == 0x6b and cache == b'AIG1.w3v' and mission == b'g1':
                    if key in KEYS:
                        self.observe_cache(pid, key, value)
                    else:
                        self.counts['other_exports'] += 1
                else:
                    self.counts['foreign_cache'] += 1
            else:
                raise Refusal('UNSUPPORTED_ACTION')


def parse_native(data, duration):
    c = Cursor(data)
    c.expect(b'\x10\x01\0\0\0')
    host = c.u8()
    c.string()
    c.expect(b'\x01\0')
    c.string()
    c.expect(b'\0')
    c.string()
    slots = c.u32()
    need(1 <= slots <= 12, 'SLOT_COUNT')
    c.take(8)  # game type and native language constant; never expose stat strings
    players = {host}
    while True:
        op = c.u8()
        if op == 0x19:
            break
        need(op == 0x16 and len(players) < 16, 'NATIVE_GRAMMAR')
        pid = c.u8()
        need(pid not in players, 'DUPLICATE_PID')
        players.add(pid)
        c.string()
        c.expect(b'\x01\0' + bytes(4))
    length = c.u16()
    count = c.u8()
    need(count == slots and length == 7 + count * 9, 'SLOT_COUNT')
    colours = {}
    for _ in range(count):
        pid, download, status, computer, team, colour, race, skill, handicap = c.take(9)
        need(status <= 2 and computer <= 1 and colour <= 12 and team <= 12, 'SLOT_INVALID')
        if status == 2 and not computer:
            need(pid not in colours and colour not in colours.values(), 'SLOT_INVALID')
            colours[pid] = colour
    c.take(6)  # seed, layout, start spot count
    c.expect(b'\x1a\x01\0\0\0\x1b\x01\0\0\0')
    summary = Summary(colours)
    allowed = players | set(colours)
    leaves = checksums = timeslots = elapsed = 0

    def leave():
        block = Cursor(c.take(13))
        block.u32()
        need(block.u8() in allowed, 'PID_INVALID')
        block.u32()
        need(block.u32() == 1, 'NATIVE_GRAMMAR')

    while True:
        summary.step()
        op = c.u8()
        if op == 0x1c:
            c.expect(b'\x01\0\0\0')
            break
        need(op == 0x17, 'UNSUPPORTED_LOADING_RECORD')
        leave()
        leaves += 1
    while c.left():
        summary.step()
        op = c.u8()
        if op == 0x17:
            leave()
            leaves += 1
        elif op in (0x1e, 0x1f):
            slot = Cursor(c.take(c.u16()))
            increment = slot.u16()
            need(op != 0x1e or increment == 0, 'NATIVE_GRAMMAR')
            elapsed += increment
            timeslots += 1
            while slot.left():
                summary.step()
                pid, length = slot.u8(), slot.u16()
                need(pid in allowed, 'PID_INVALID')
                summary.batch += 1
                summary.actions(pid, slot.take(length))
        elif op == 0x20:
            need(c.u8() in allowed, 'PID_INVALID')
            chat = Cursor(c.take(c.u16()))
            chat.take(5)  # native writer always includes flags + chat mode
            chat.string()
            need(not chat.left(), 'CHAT_LENGTH')
        elif op == 0x22:
            c.expect(b'\x04')
            c.take(4)  # engine state checksum: no independent engine to compare
            checksums += 1
        else:
            raise Refusal('UNSUPPORTED_RECORD')
    need(elapsed == duration, 'DURATION_MISMATCH')
    return dict(code='OK', structural_integrity='PASS', engine_playback='NOT_ASSESSED',
                engine_desync='NOT_ASSESSED', incoming_batch_causality='NOT_RECOVERABLE',
                ack_epoch_attribution='LATEST_PRECEDING_COMMAND_INFERENCE',
                receiver_binding='UNVERIFIED',
                probe_commands=sum(c['sequence'] == 0 for c in summary.commands),
                actor_commands=sum(c['sequence'] > 0 for c in summary.commands),
                timeslots=timeslots, action_records=summary.batch, actions=summary.action,
                duration_ms=elapsed, leave_records=leaves, checksum_records=checksums,
                publisher_pid=summary.publisher, counts=summary.counts,
                commands=summary.commands,
                exports=[value for _, value in sorted(summary.exports.items())],
                receiver_pairs=[dict(pid=p, a=a, b=b, count=n)
                                for (p, a, b), n in sorted(summary.pairs.items())],
                peers=[value for _, value in sorted(summary.groups.items())])


def audit(data):
    container = 'NOT_VALIDATED'
    try:
        body, duration, blocks = unpack(data)
        container = 'PASS'
        result = parse_native(body, duration)
        result.update(container_integrity=container, compressed_bytes=len(data),
                      uncompressed_bytes=len(body), compressed_blocks=blocks)
        return result
    except Refusal as error:
        return dict(code=error.args[0], container_integrity=container,
                    structural_integrity='REFUSED', engine_playback='NOT_ASSESSED',
                    engine_desync='NOT_ASSESSED')


def main(argv):
    if len(argv) != 2:
        result = {'code': 'ARGUMENTS'}
    else:
        try:
            fd = os.open(argv[1], os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                need(stat.S_ISREG(info.st_mode), 'REGULAR_FILE_REQUIRED')
                need(info.st_size <= MAX_COMPRESSED, 'COMPRESSED_BOUND')
                result = audit(stream.read(MAX_COMPRESSED + 1))
        except Refusal as error:
            result = {'code': error.args[0]}
        except KeyboardInterrupt:
            result = {'code': 'INTERRUPTED'}
        except OSError:
            result = {'code': 'INPUT_IO'}
        except Exception:
            result = {'code': 'INTERNAL_ERROR'}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['code'] == 'OK' else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
