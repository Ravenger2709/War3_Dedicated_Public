import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from src.runtime.cycle_state import CycleState


class MigrationCrashTests(unittest.TestCase):
    def test_sigkill_at_each_migration_phase_preserves_history_and_pending(self):
        stages = ('temp_created', 'schema_created', 'populated', 'validated',
                  'published', 'published_synced', 'marker_written', 'marker_synced')
        program = r'''
import json, os, signal, sys
from src.runtime.cycle_state import CycleState
s = CycleState(sys.argv[1], ['a', 'b'])
def kill(stage):
    if stage == sys.argv[2]:
        os.kill(os.getpid(), signal.SIGKILL)
s._migration_stage = kill
s.consume_external(json.loads(sys.argv[3]))
'''
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.json'
                state = CycleState(path, ['a', 'b'])
                gen, first_match, second_match = 'a' * 32, 'b' * 32, 'c' * 32
                first = {'session_generation': gen, 'match_id': first_match, 'host_counter': 1, 'preset_id': 'a'}
                pending = {'session_generation': gen, 'host_counter': 2, 'preset_id': 'b'}
                state._save({**state.state, 'version': 2, 'index': 1, 'pending': pending,
                             'last_started': first, 'external_history': [first]})
                source = path.read_bytes()
                event = {'version': 1, 'event': 'game_started', 'session_generation': gen,
                         'match_id': second_match, 'host_counter': 2}
                other = Path(directory) / '.state.json.sqlite3.migration-foreign'
                other.write_text('do not touch')
                proc = subprocess.run([sys.executable, '-c', program, str(path), stage, json.dumps(event)])
                self.assertEqual(proc.returncode, -signal.SIGKILL)
                final = path.with_name(path.name + '.sqlite3')
                if not final.exists():
                    self.assertEqual(path.read_bytes(), source)
                recovered = CycleState(path, ['a', 'b'])
                self.assertEqual(recovered.current, 'b')
                self.assertEqual(recovered.state['pending'], pending)
                self.assertEqual(recovered.state['last_started'], first)
                old = {**event, 'match_id': first_match, 'host_counter': 1}
                self.assertFalse(recovered.consume_external(old))
                self.assertTrue(recovered.consume_external(event))
                recovered = CycleState(path, ['a', 'b'])
                self.assertEqual(recovered.current, 'a')
                self.assertFalse(recovered.consume_external(old))
                self.assertFalse(recovered.consume_external(event))
                self.assertEqual(other.read_text(), 'do not touch')

    def test_no_clobber_if_foreign_final_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = CycleState(path, ['a'])
            original = path.read_bytes()
            state.db_path.write_bytes(b'foreign data')
            with self.assertRaises(FileExistsError):
                state.reserve_external('a' * 32, 1)
            self.assertEqual(state.db_path.read_bytes(), b'foreign data')
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
