#!/usr/bin/env python3
"""Independent compressed replay fixtures; never real-engine safety acceptance."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('g1_replay_fixtures', Path(__file__).with_name('test_replay_audit.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def scenario(changes=None, omit=(), foreign_field=None, duplicate_field=None,
             recovery=True, recovery_pid=1, recovery_epoch=42, recovery_ack=6,
             intervening=False, fifth_kind=1):
    # Literal fixture values describe an interrupted move to (+512, 0),
    # not values calculated by the auditor under test.
    values = dict(stall_start=49, stall_end=89, fallback_count=1,
                  fallback_tick=69, fallback_lease=68, fallback_epoch=42,
                  fallback_sequence=5, fallback_before_order=851986,
                  fallback_order=851993, fallback_hold_ok=1,
                  fallback_x_offset=4200, fallback_y_offset=4096,
                  fallback_target_x_offset=4608, fallback_target_y_offset=4096,
                  move_order=851986, hold_order=851993, move_speed=150)
    values.update(changes or {})

    def emit(*keys):
        records = []
        for key in keys:
            if key in omit:
                continue
            pid = 2 if key == foreign_field else 1
            records.append((pid, f.export(key.encode(), values[key])))
        return f.frame(*records)

    raw = f.frame((1, f.export(b'wire_version', 2) + f.export(b'tick', 8)))
    raw += emit('move_order', 'hold_order', 'move_speed')
    raw += f.frame((2, f.triggered(b'~g1:42:0:8:0', 832, 832)))
    raw += f.frame((1, f.export(b'tick', 9) + f.export(b'probe', 42)))
    for sequence in range(1, 6):
        tick = 8 * (sequence + 1)
        kind = fifth_kind if sequence == 5 else (1 if sequence % 2 else 2)
        raw += f.frame((2, f.triggered(f'~g1:42:{sequence}:{tick}:{kind}'.encode(), 832, 832)))
        if sequence == 5:
            raw += emit('stall_start')
        raw += f.frame((1, f.export(b'digest', 100 + sequence) + f.export(b'ack', sequence)))
    # The event may span several serialized action records. Requiring one
    # original incoming network batch would invent information absent in w3g.
    raw += emit('fallback_count', 'fallback_tick', 'fallback_lease',
                'fallback_epoch', 'fallback_sequence', 'fallback_before_order')
    raw += emit('fallback_order', 'fallback_hold_ok', 'fallback_x_offset',
                'fallback_y_offset', 'fallback_target_x_offset', 'fallback_target_y_offset')
    if duplicate_field:
        raw += emit(duplicate_field)
    if intervening:
        raw += f.frame((2, f.triggered(b'~g1:42:6:70:2', 832, 832)))
    raw += emit('stall_end')
    if recovery:
        raw += f.frame((2, f.triggered(f'~g1:{recovery_epoch}:6:89:2'.encode(), 832, 832)))
        raw += f.frame((recovery_pid, f.export(b'digest', 106) + f.export(b'ack', recovery_ack)))
    # Each fixture timeslot here is explicitly 250 ms.
    # Decode boundaries from the independent assembler, not the semantic audit.
    offset = frames = 0
    while offset < len(raw):
        assert raw[offset] == 0x1f
        length = int.from_bytes(raw[offset + 1:offset + 3], 'little')
        offset += length + 3
        frames += 1
    assert offset == len(raw)
    return f.audit.audit(f.packed(f.body(raw), frames * 250))


class SafetyReplayTests(unittest.TestCase):
    def safety(self, **kw):
        result = scenario(**kw)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['engine_playback'], 'NOT_ASSESSED')
        self.assertEqual(result['engine_desync'], 'NOT_ASSESSED')
        return result['controlled_stall']

    def test_valid_report_spanning_action_records(self):
        result = self.safety()
        self.assertEqual(result['code'], 'CONSISTENT_REPORTED_CONTROLLED_STALL')
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['scope'], 'REPORTED_MAP_TELEMETRY_ONLY')
        self.assertEqual(result['real_network_lag'], 'NOT_ASSESSED')

    def test_missing_or_other_pid_field_is_not_filled_from_defaults(self):
        for kw in ({'omit': ('fallback_order',)}, {'foreign_field': 'fallback_order'}):
            with self.subTest(kw=kw):
                self.assertEqual(self.safety(**kw)['code'], 'INCOMPLETE_REPORTED_TELEMETRY')

    def test_single_event_not_a_mixture_of_repeated_snapshots(self):
        self.assertEqual(self.safety(duplicate_field='fallback_order')['code'],
                         'INCONSISTENT_REPORTED_TELEMETRY')

    def test_invalid_fixed_fields_never_create_safety_success(self):
        invalid = [
            {'fallback_epoch': 43}, {'fallback_sequence': 4}, {'fallback_count': 2},
            {'stall_end': 88}, {'stall_end': 90}, {'fallback_tick': 68},
            {'fallback_tick': 70}, {'fallback_tick': 89}, {'fallback_lease': 20},
            {'fallback_before_order': 851993}, {'fallback_order': 851986},
            {'fallback_hold_ok': 0}, {'move_order': 999, 'fallback_before_order': 999},
            {'hold_order': 999, 'fallback_order': 999},
            {'move_order': 851993, 'fallback_before_order': 851993},
            {'fallback_x_offset': 4608}, {'fallback_x_offset': 0xffffffff},
            {'fallback_target_x_offset': 5000}, {'move_speed': 0},
        ]
        for change in invalid:
            with self.subTest(change=change):
                self.assertEqual(self.safety(changes=change)['code'],
                                 'INCONSISTENT_REPORTED_TELEMETRY')

    def test_hold_or_intervening_command_cannot_prove_interrupted_move(self):
        for kw in ({'fifth_kind': 2}, {'intervening': True}):
            with self.subTest(kw=kw):
                self.assertEqual(self.safety(**kw)['code'], 'INCONSISTENT_REPORTED_TELEMETRY')

    def test_recovery_needs_same_publisher_epoch_and_corresponding_ack(self):
        for kw in ({'recovery': False}, {'recovery_pid': 2},
                   {'recovery_epoch': 43}, {'recovery_ack': 7}, {'recovery_ack': 5}):
            with self.subTest(kw=kw):
                self.assertEqual(self.safety(**kw)['code'], 'INCONSISTENT_REPORTED_TELEMETRY')

    def test_ordinary_replay_never_gains_a_safety_claim(self):
        result = f.audit.audit(f.packed(f.body()))
        self.assertEqual(result['controlled_stall']['code'], 'NOT_OBSERVED')


if __name__ == '__main__':
    unittest.main()
