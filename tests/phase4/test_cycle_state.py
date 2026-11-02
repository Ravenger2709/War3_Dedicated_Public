import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.cycle_state import CycleState


class CycleStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cycle.json'
        self.cycle = ['a', 'b', 'a']

    def open(self):
        return CycleState(self.path, self.cycle)

    def test_starts_advance_round_robin_even_with_duplicate_ids(self):
        state = self.open()
        for counter, current, following in [(1, 'a', 'b'), (2, 'b', 'a'), (3, 'a', 'a')]:
            self.assertEqual((state.current, state.next), (current, following))
            event = state.reserve(counter)
            self.assertTrue(state.started(event['match_id'], counter))
        self.assertEqual(state.current, 'a')

    def test_replayed_start_after_restart_cannot_advance_again(self):
        state = self.open()
        event = state.reserve(7)
        state.started(event['match_id'], 7)
        state = self.open()
        self.assertFalse(state.started(event['match_id'], 7))
        self.assertEqual(state.current, 'b')
        with self.assertRaises(ValueError):
            state.started(event['match_id'], 8)

    def test_abandoned_lobby_does_not_advance(self):
        state = self.open()
        event = state.reserve(1)
        state.abandon(event['match_id'])
        self.assertEqual(self.open().current, 'a')

    def test_pending_start_survives_restart(self):
        event = self.open().reserve(22)
        state = self.open()
        self.assertTrue(state.started(event['match_id'], 22))
        self.assertEqual(self.open().current, 'b')

    def test_corrupt_or_changed_cycle_does_not_overwrite_state(self):
        self.open()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            CycleState(self.path, ['b'])
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps({'bad': True}))
        with self.assertRaises(ValueError):
            self.open()
        self.assertEqual(json.loads(self.path.read_text()), {'bad': True})

    def test_invalid_or_duplicate_reservation_is_rejected(self):
        state = self.open()
        with self.assertRaises(ValueError):
            state.reserve(0)
        with self.assertRaises(ValueError):
            state.reserve(2**28)
        event = state.reserve(3)
        with self.assertRaises(ValueError):
            state.reserve(4)
        with self.assertRaises(ValueError):
            state.started(event['match_id'], 4)
        self.assertEqual(self.open().current, 'a')

    def test_uncertain_write_refuses_further_events_until_reloaded(self):
        state = self.open()
        event = state.reserve(2)
        original = state._write

        def uncertain(value):
            original(value)
            raise OSError('simulated directory fsync failure')

        with patch.object(state, '_write', side_effect=uncertain):
            with self.assertRaises(OSError):
                state.started(event['match_id'], 2)
        with self.assertRaisesRegex(ValueError, 'STORAGE_UNCERTAIN'):
            state.started(event['match_id'], 2)
        self.assertFalse(self.open().started(event['match_id'], 2))

    def test_cycle_change_requires_no_pending_lobby_and_keeps_last_event(self):
        state = self.open()
        event = state.reserve(1)
        with self.assertRaises(ValueError):
            state.change_cycle(['b'])
        state.started(event['match_id'], 1)
        self.assertTrue(state.change_cycle(['b']))
        self.assertFalse(state.change_cycle(['b']))
        state = CycleState(self.path, ['b'])
        self.assertEqual(state.current, 'b')
        self.assertFalse(state.started(event['match_id'], 1))


if __name__ == '__main__':
    unittest.main()
