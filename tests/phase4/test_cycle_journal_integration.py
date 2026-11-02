import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from src.runtime.cycle_state import CycleState
from src.runtime.lifecycle import scan


class CycleJournalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'journal'
        self.journal.mkdir(mode=0o700)
        self.state_path = self.root / 'cycle.json'
        self.cycle = ['one', 'two']
        self.generation = 'a' * 32
        self.match = 'b' * 32
        self.payload = {'version': 1, 'event': 'game_started',
                        'session_generation': self.generation,
                        'match_id': self.match, 'host_counter': 7}

    def event(self):
        path = self.journal / f'start-{self.generation}-{self.match}.json'
        path.write_text(json.dumps(self.payload) + '\n')
        path.chmod(0o600)
        return path

    def test_scan_advances_durably_and_exact_replay_is_acknowledged(self):
        state = CycleState(self.state_path, self.cycle)
        state.reserve_external(self.generation, 7)
        path = self.event()

        def persist(event):
            # A duplicate is successfully persisted too: False from the cursor
            # means already processed, not a failure to persist.
            state.consume_external(dataclasses.asdict(event))
            return True

        self.assertEqual(scan(self.journal, persist).processed, 1)
        self.assertEqual(state.current, 'two')
        self.assertFalse(path.exists())
        state = CycleState(self.state_path, self.cycle)
        # Simulate durable cursor commit followed by an unacknowledged replay.
        path.with_suffix('.ack').unlink()
        self.event()
        self.assertEqual(scan(self.journal, persist).processed, 1)
        self.assertEqual(state.current, 'two')

    def test_mismatched_start_is_not_acknowledged(self):
        state = CycleState(self.state_path, self.cycle)
        state.reserve_external(self.generation, 8)
        path = self.event()
        def persist(event):
            state.consume_external(dataclasses.asdict(event))
            return True
        with self.assertRaises(ValueError):
            scan(self.journal, persist)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix('.ack').exists())
        self.assertEqual(CycleState(self.state_path, self.cycle).current, 'one')


if __name__ == '__main__':
    unittest.main()
