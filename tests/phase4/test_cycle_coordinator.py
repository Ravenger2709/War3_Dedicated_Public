import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.cycle_coordinator import CycleCoordinator
from src.runtime.cycle_state import CycleState


class CycleCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.journal = root / 'journal'
        self.journal.mkdir(mode=0o700)
        self.state_path = root / 'cycle.json'
        self.state = CycleState(self.state_path, ['a', 'b'])
        self.generation = 'c' * 32
        self.coordinator = CycleCoordinator(self.state, self.journal)

    def event(self, counter=7):
        match = 'd' * 32
        path = self.journal / f'start-{self.generation}-{match}.json'
        path.write_text(json.dumps({'version': 1, 'event': 'game_started',
                                    'session_generation': self.generation,
                                    'match_id': match, 'host_counter': counter}) + '\n')
        path.chmod(0o600)

    def test_reservation_precedes_permission_and_start_advances(self):
        marker = self.coordinator.allow_lobby(self.generation, 7)
        self.assertTrue((self.journal / marker).is_file())
        self.assertEqual(self.coordinator.allow_lobby(self.generation, 7), marker)
        self.event()
        self.assertEqual(self.coordinator.consume().processed, 1)
        self.assertEqual(CycleState(self.state_path, ['a', 'b']).current, 'b')

    def test_failed_marker_keeps_reservation_and_refuses_another_lobby(self):
        with patch('src.runtime.cycle_coordinator.publish', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.coordinator.allow_lobby(self.generation, 7)
        state = CycleState(self.state_path, ['a', 'b'])
        self.assertEqual(state.state['pending']['host_counter'], 7)
        with self.assertRaisesRegex(ValueError, 'MISMATCH'):
            CycleCoordinator(state, self.journal).allow_lobby(self.generation, 8)


if __name__ == '__main__':
    unittest.main()
