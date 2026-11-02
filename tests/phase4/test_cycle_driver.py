"""Offline driver integration using the real cursor, journal and IPC primitives."""
import json
import os
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime import preload_manifest as manifest_module
from src.runtime.catalog_requests import CatalogRequests
from src.runtime.cycle_driver import CycleDriver, LobbyObservation
from src.runtime.cycle_state import CycleState


def manifest():
    m = manifest_module
    cfg = b'map_path = Maps\\one.w3x\nmap_localpath = one.w3x\n'
    settings = (12, 0, 2, 5, 0, 50, 2, 30, 0, 0, 10, 0, 0, 0, 2)
    entry = m._ENTRY.pack(3, 100, bytes(32), 3, 3, len(cfg))
    entry += m._SETTINGS.pack(*settings) + b'one' + b'One' + cfg
    return m._HEADER.pack(m._MAGIC, 1, 2, 1, 0, 100, m._accounted([100], 100, 1)) + entry + bytes([0, 0])


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ipc = self.root / 'ipc'; self.ipc.mkdir(mode=0o700)
        self.journal = self.root / 'journal'; self.journal.mkdir(mode=0o700)
        self.gen = 'a' * 32
        self.path = self.root / 'cursor.json'
        self.queue = self.root / 'queue.json'
        self.cursor = CycleState(self.path, ['one', 'one'])
        self.catalog = CatalogRequests.initialize(self.queue, self.ipc, manifest(), self.gen)
        self.driver = CycleDriver(self.catalog, self.cursor, self.journal)

    def reload(self):
        self.cursor = CycleState(self.path, ['one', 'one'])
        self.catalog = CatalogRequests(self.queue, self.ipc, manifest(), self.gen)
        self.driver = CycleDriver(self.catalog, self.cursor, self.journal)

    def ack(self, status='staged'):
        req = self.catalog._request(self.catalog.data['pending'])
        path = self.ipc / ('ack-' + req.request_id + '.ready')
        path.write_bytes(req.payload() + f'status={status}\n'.encode()); path.chmod(0o600)
        mailbox = self.ipc / 'next.ready'
        if mailbox.exists() and mailbox.read_bytes() == req.payload():
            mailbox.unlink()  # Simulate native consumption after writing ACK.
        return path

    def obs(self, counter=7, **changes):
        record = self.catalog.data['pending']
        return LobbyObservation(**({'generation': self.gen, 'request_id': record['request_id'],
                                   'catalog_index': record['index'], 'host_counter': counter} | changes))

    def event(self, kind, counter=7, generation=None):
        gen = generation or self.gen
        if kind == 'game_started':
            match = 'b' * 32
            path = self.journal / f'start-{gen}-{match}.json'
            data = {'version': 1, 'event': kind, 'session_generation': gen,
                    'host_counter': counter, 'match_id': match}
        else:
            path = self.journal / f'close-{gen}-{counter}.json'
            data = {'version': 1, 'event': kind, 'session_generation': gen, 'host_counter': counter}
        path.write_text(json.dumps(data) + '\n'); path.chmod(0o600)
        return path

    def staged(self):
        result = self.driver.step(select=True, capacity_available=True)
        self.assertEqual(result.state, 'awaiting_ack')
        self.ack()
        self.assertEqual(self.driver.step().state, 'awaiting_lobby')
        return result.request_id

    def test_revalidation_precedes_enqueue_dispatch_and_marker(self):
        self.assertEqual(self.driver.step(select=True,capacity_available=True,revalidate=lambda:False).state,
                         'observation_stale')
        self.assertIsNone(self.catalog.data['pending'])
        calls=[]
        def expires_after_enqueue():
            calls.append(True)
            return len(calls)==1
        self.assertEqual(self.driver.step(select=True,capacity_available=True,
                         revalidate=expires_after_enqueue).state,'observation_stale')
        self.assertIsNotNone(self.catalog.data['pending'])
        self.assertFalse((self.ipc/'next.ready').exists())
        self.assertEqual(self.driver.step(revalidate=lambda:True).state,'awaiting_ack')
        self.ack()
        self.assertEqual(self.driver.step().state,'awaiting_lobby')
        self.assertEqual(self.driver.step(self.obs(),revalidate=lambda:False).state,'observation_stale')
        self.assertIsNone(self.cursor.state['pending'])
        self.assertFalse(list(self.journal.glob('reservation-*.ready')))

    def test_full_bootstrap_start_and_replay(self):
        self.assertEqual(self.driver.step(select=True).state, 'capacity_full')
        self.assertIsNone(self.catalog.data['pending'])
        request = self.staged()
        obs = self.obs()
        self.assertEqual(self.cursor.state['index'], 0)
        result = self.driver.step(obs)
        self.assertEqual(result.state, 'reserved')
        self.assertTrue((self.journal / result.marker).exists())
        self.assertEqual(self.driver.step(obs).marker, result.marker)
        self.event('game_started')
        result = self.driver.step(obs, select=True, capacity_available=True)
        self.assertEqual((result.state, result.request_id), ('started', request))
        self.assertEqual(self.cursor.state['index'], 1)
        self.assertIsNone(self.catalog.data['pending'])
        self.assertEqual(self.driver.step(obs, select=True, capacity_available=True).state, 'ignored')
        self.assertIsNone(self.catalog.data['pending'])

    def test_close_first_then_rehost_same_position(self):
        request = self.staged(); old = self.obs()
        self.driver.step(old)
        self.event('lobby_closed')
        self.assertEqual(self.driver.step(old, select=True, capacity_available=True).state, 'closed')
        self.assertEqual(self.cursor.state['index'], 0)
        self.assertIsNone(self.catalog.data['pending'])
        self.assertEqual(self.driver.step(select=True).state, 'capacity_full')
        result = self.driver.step(select=True, capacity_available=True)
        self.assertNotEqual(result.request_id, request)
        self.assertEqual(self.catalog.data['pending']['index'], old.catalog_index)
        self.assertEqual(self.driver.step(old).state, 'ignored')
        self.ack()
        self.assertEqual(self.driver.step(self.obs(8)).state, 'reserved')
        self.assertEqual(self.cursor.state['index'], 0)

    def test_invalid_or_stale_observation_has_no_side_effects(self):
        self.staged()
        for obs in (self.obs(generation='c'*32), self.obs(request_id='d'*32),
                    self.obs(catalog_index=1)):
            with self.subTest(obs=obs), patch.object(self.driver.coordinator, 'consume', side_effect=AssertionError('I/O')):
                self.assertEqual(self.driver.step(obs, select=True, capacity_available=True).state, 'ignored')
        with self.assertRaises(ValueError): LobbyObservation(self.gen, 'b'*32, True, 7)
        self.driver.step(self.obs())
        with patch.object(self.driver.coordinator, 'consume', side_effect=AssertionError('I/O')):
            self.assertEqual(self.driver.step(self.obs(8)).state, 'ignored')

    def test_journal_precedes_binding_and_closed_lobby_not_regated(self):
        self.staged(); obs = self.obs(); self.driver.step(obs)
        self.event('lobby_closed')
        with patch.object(self.catalog, 'bind_reservation', side_effect=AssertionError('late marker')):
            self.assertEqual(self.driver.step(obs).state, 'closed')

    def test_close_before_first_bind_fences_observed_counter(self):
        self.staged(); obs = self.obs()
        self.event('lobby_closed')
        with self.assertRaises(ValueError): self.driver.step(obs)
        self.assertIsNone(self.cursor.state['pending'])
        self.assertIsNone(self.catalog.data['pending']['counter'])
        self.assertEqual(list(self.journal.glob('reservation-*.ready')), [])

    def test_sigkill_after_unbound_close_cannot_infer_correlation(self):
        request_id = self.staged()
        obs = self.obs()
        self.event('lobby_closed')
        pid = os.fork()
        if pid == 0:
            self.driver.coordinator.consume()
            os.kill(os.getpid(), signal.SIGKILL)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.reload()
        self.assertEqual(self.catalog.data['pending']['request_id'], request_id)
        self.assertIsNone(self.catalog.data['pending']['counter'])
        with patch.object(self.driver.coordinator, 'consume', side_effect=AssertionError('unexpected I/O')):
            self.assertEqual(self.driver.step(self.obs(request_id='d' * 32)).state, 'ignored')
            self.assertEqual(self.driver.step(self.obs(catalog_index=1)).state, 'ignored')
        with self.assertRaises(ValueError): self.driver.step(obs)
        self.assertEqual(self.catalog.data['pending']['request_id'], request_id)
        self.assertEqual(self.cursor.state['index'], 0)
        self.assertEqual(list(self.journal.glob('reservation-*.ready')), [])
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)

    def test_v2_unbound_close_is_consumed_before_new_selection(self):
        request=self.driver.step(select=True,capacity_available=True).request_id
        record=self.catalog.data['pending']
        (self.ipc/'next.ready').unlink()  # Native consumes mailbox before creating the lobby.
        event={'version':2,'event':'lobby_closed','session_generation':self.gen,'host_counter':7,
               'request_id':request,'manifest_sha256':self.catalog.digest,'catalog_index':record['index']}
        path=self.journal/f'close-{self.gen}-7.json'
        path.write_text(json.dumps(event));path.chmod(0o600)
        outcome=self.driver.step(select=True,capacity_available=True)
        self.assertEqual((outcome.state,outcome.request_id),('closed',request))
        self.assertIsNone(self.catalog.data['pending'])
        self.assertEqual(self.cursor.state['index'],0)
        self.assertTrue(path.with_suffix('.ack').exists())
        self.assertFalse(list(self.journal.glob('reservation-*.ready')))
        newer=self.driver.step(select=True,capacity_available=True)
        self.assertEqual(newer.state,'awaiting_ack')
        self.assertNotEqual(newer.request_id,request)
        self.assertEqual(self.catalog.data['version'],2)

    def test_failed_scan_stops_all_selection_and_binding(self):
        with patch.object(self.driver.coordinator, 'consume', side_effect=OSError('journal')):
            with self.assertRaises(OSError): self.driver.step(select=True, capacity_available=True)
        self.assertIsNone(self.catalog.data['pending'])
        self.staged()
        with patch.object(self.driver.coordinator, 'consume', side_effect=OSError('journal')):
            with self.assertRaises(OSError): self.driver.step(self.obs())
        self.assertIsNone(self.cursor.state['pending'])

    def test_lost_ack_retry_and_restart_keeps_request(self):
        result = self.driver.step(select=True, capacity_available=True)
        self.reload()
        self.assertEqual(self.driver.step(select=True).request_id, result.request_id)
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)
        self.ack(); self.reload()
        self.assertEqual(self.driver.step().state, 'awaiting_lobby')
        self.assertEqual(self.driver.step(self.obs()).state, 'reserved')
        self.reload()
        self.assertEqual(self.driver.step().state, 'awaiting_lifecycle')
        self.assertEqual(self.driver.step(self.obs()).state, 'reserved')

    def test_uncertain_rejected_and_missing_ack_never_bind(self):
        self.driver.step(select=True, capacity_available=True)
        ack = self.ack('uncertain')
        self.assertEqual(self.driver.step(self.obs()).state, 'uncertain')
        self.assertIsNone(self.cursor.state['pending'])
        ack.unlink()
        with self.assertRaises(ValueError): self.driver.step(self.obs())
        self.assertIsNone(self.cursor.state['pending'])

    def test_bind_failure_restart_resumes_same_counter(self):
        self.staged(); obs = self.obs()
        with patch('src.runtime.catalog_requests.publish_reservation', side_effect=OSError('test')):
            with self.assertRaises(OSError): self.driver.step(obs)
        self.reload()
        self.assertEqual(self.driver.step(self.obs(8)).state, 'ignored')
        self.assertEqual(self.driver.step(obs).state, 'reserved')
        self.assertEqual(self.cursor.state['pending']['host_counter'], 7)

if __name__ == '__main__': unittest.main()
