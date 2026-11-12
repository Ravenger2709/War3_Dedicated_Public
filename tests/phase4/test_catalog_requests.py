"""Offline coordinator tests, including real process death and durable reload."""
import os
import json
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime import preload_manifest as m
from src.runtime.catalog_requests import CatalogRequests, CatalogRequestError
from src.runtime.cycle_state import CycleState
from src.runtime.cycle_coordinator import CycleCoordinator


def manifest():
    cfg = b'map_path = Maps\\one.w3x\nmap_localpath = one.w3x\n'
    settings = (12, 0, 2, 5, 0, 50, 2, 30, 0, 0, 10, 0, 0, 0, 2)
    entry = m._ENTRY.pack(3, 100, bytes(32), 3, 3, len(cfg))
    entry += m._SETTINGS.pack(*settings) + b'one' + b'One' + cfg
    return m._HEADER.pack(m._MAGIC, 1, 2, 1, 0, 100, m._accounted([100], 100, 1)) + entry + bytes([0, 0])


class CatalogRequestsTests(unittest.TestCase):
    def rejected(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor)
        ack = self.ack(request, 'rejected')
        self.q.dispatch(self.cursor)
        return request, ack

    def test_clean_rejection_advances_without_fabricating_native_events(self):
        request, ack = self.rejected()
        original = ack.read_bytes()
        self.assertTrue(self.q.retire_rejected(self.cursor, request.request_id))
        self.assertEqual(self.cursor.state['index'], 1)
        self.assertIsNone(self.cursor.state['last_started'])
        self.assertIsNone(self.cursor.state['pending'])
        self.assertIsNone(self.q.data['pending'])
        self.assertEqual(ack.read_bytes(), original)
        self.assertEqual(list(self.journal.iterdir()), [])
        self.assertEqual(len(list(self.root.glob('rejected-selection-*.json'))), 1)

    def test_rejection_intent_resumes_before_cursor_write(self):
        request, _ = self.rejected()
        with patch.object(self.cursor, '_save', side_effect=OSError('cursor unavailable')):
            with self.assertRaises(OSError): self.q.retire_rejected(self.cursor, request.request_id)
        self.cursor = CycleState(self.root / 'cursor.json', ['one', 'one'])
        q = self.reload()
        q.retire_rejected(self.cursor, request.request_id)
        self.assertEqual(self.cursor.state['index'], 1)
        self.assertIsNone(q.data['pending'])

    def test_sigkill_after_sqlite_advance_resumes_exactly_once(self):
        request, ack = self.rejected()
        self.cursor._enable_sqlite()
        child = os.fork()
        if child == 0:
            with patch.object(self.q, '_save', side_effect=lambda _: os.kill(os.getpid(), signal.SIGKILL)):
                self.q.retire_rejected(self.cursor, request.request_id)
            os._exit(77)
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.cursor = CycleState(self.root / 'cursor.json', ['one', 'one'])
        self.assertEqual(self.cursor.state['index'], 1)
        q = self.reload()
        q.retire_rejected(self.cursor, request.request_id)
        self.assertEqual(self.cursor.state['index'], 1)
        self.assertIsNone(q.data['pending'])
        self.assertTrue(ack.exists())

    def test_missing_or_changed_negative_ack_never_retires(self):
        request, ack = self.rejected()
        for replacement in (None, request.payload() + b'status=staged\n', request.payload().replace(b'a'*32, b'b'*32) + b'status=rejected\n'):
            if replacement is None:
                ack.unlink(missing_ok=True)
            else:
                ack.write_bytes(replacement); ack.chmod(0o600)
            with self.assertRaises(ValueError): self.q.retire_rejected(self.cursor, request.request_id)
            self.assertEqual(self.cursor.state['index'], 0)
            self.assertIsNotNone(self.q.data['pending'])

    def test_existing_human_match_reservation_blocks_rejection_retirement(self):
        request, _ = self.rejected()
        pending = self.cursor.reserve_external('f'*32, 7)
        with self.assertRaises(ValueError): self.q.retire_rejected(self.cursor, request.request_id)
        self.assertEqual(self.cursor.state['pending'], pending)

    def test_uncertain_request_cannot_be_skipped(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor)
        self.ack(request, 'uncertain')
        self.q.dispatch(self.cursor)
        with self.assertRaises(ValueError): self.q.retire_rejected(self.cursor, request.request_id)
        self.assertEqual(self.cursor.state['index'], 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ipc = self.root / 'ipc'; self.ipc.mkdir(mode=0o700)
        self.path = self.root / 'queue.json'
        self.gen = 'a' * 32
        self.manifest = manifest()
        self.cursor = CycleState(self.root / 'cursor.json', ['one', 'one'])
        self.journal = self.root / 'journal'; self.journal.mkdir(mode=0o700)
        self.q = CatalogRequests.initialize(self.path, self.ipc, self.manifest, self.gen)

    def reload(self):
        return CatalogRequests(self.path, self.ipc, self.manifest, self.gen)

    def ack(self, request, status='staged'):
        path = self.ipc / ('ack-' + request.request_id + '.ready')
        path.write_bytes(request.payload() + f'status={status}\n'.encode()); path.chmod(0o600)
        mailbox = self.ipc / 'next.ready'
        if mailbox.exists() and mailbox.read_bytes() == request.payload():
            mailbox.unlink()  # Simulate native consumption after writing ACK.
        return path

    def start(self):
        self.cursor.consume_external({'version': 1, 'event': 'game_started',
            'session_generation': self.gen, 'host_counter': 7, 'match_id': 'b' * 32})

    def test_retry_ack_and_cursor_start_are_distinct(self):
        self.q.enqueue(self.cursor)
        req, status = self.q.dispatch(self.cursor)
        self.assertIsNone(status)
        self.assertEqual(self.reload().dispatch(self.cursor), (req, None))
        self.ack(req)
        self.assertEqual(self.reload().dispatch(self.cursor), (req, 'staged'))
        with self.assertRaises(CatalogRequestError): self.reload().complete(self.cursor, req.request_id)
        self.assertEqual(self.cursor.state['index'], 0)
        self.reload().bind_reservation(self.cursor, req.request_id, 7, self.journal)
        self.start()
        q = self.reload()
        self.assertTrue(q.complete(self.cursor, req.request_id))
        self.assertFalse(self.reload().complete(self.cursor, req.request_id))
        self.reload().enqueue(self.cursor)
        new, _ = self.reload().dispatch(self.cursor)
        self.assertNotEqual(new.request_id, req.request_id)
        self.assertEqual(new.index, req.index)  # repeated manifest entry, different cycle position

    def test_bootstrap_stage_lobby_gate_and_start(self):
        # No lobby, counter, or reservation exists at startup.
        self.assertIsNone(self.cursor.state['pending'])
        request = self.q.enqueue(self.cursor)
        self.assertIsNone(self.q.data['pending']['counter'])
        self.assertEqual(self.q.dispatch(self.cursor), (request, None))
        with self.assertRaises(CatalogRequestError):
            self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.ack(request)
        self.assertEqual(self.reload().dispatch(self.cursor), (request, 'staged'))
        self.assertIsNone(self.cursor.state['pending'])
        self.assertEqual(self.cursor.state['index'], 0)
        # The caller has now observed lobby counter 7 from the current generation.
        q = self.reload()
        gate = q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.assertTrue((self.journal / gate).exists())
        self.assertEqual(self.cursor.state['pending']['host_counter'], 7)
        self.assertEqual(self.cursor.state['index'], 0)
        self.assertEqual(self.reload().bind_reservation(self.cursor, request.request_id, 7, self.journal), gate)
        with self.assertRaises(CatalogRequestError):
            self.reload().bind_reservation(self.cursor, request.request_id, 8, self.journal)
        match = 'b' * 32
        event = self.journal / f'start-{self.gen}-{match}.json'
        event.write_text(json.dumps({'version': 1, 'event': 'game_started', 'session_generation': self.gen,
                                     'host_counter': 7, 'match_id': match}) + '\n')
        event.chmod(0o600)
        self.assertEqual(CycleCoordinator(self.cursor, self.journal).consume().processed, 1)
        self.assertEqual(self.cursor.state['index'], 1)
        self.assertTrue(self.reload().complete(self.cursor, request.request_id))

    def test_recover_interrupted_binding_without_changing_counter(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor); self.ack(request); self.q.dispatch(self.cursor)
        with patch.object(self.cursor, 'reserve_external', side_effect=OSError('crash after intent')):
            with self.assertRaises(OSError):
                self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.assertEqual(self.reload().data['pending']['counter'], 7)
        with self.assertRaises(CatalogRequestError):
            self.reload().bind_reservation(self.cursor, request.request_id, 8, self.journal)
        with patch('src.runtime.catalog_requests.publish_reservation', side_effect=OSError('gate failed')):
            with self.assertRaises(OSError):
                self.reload().bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.assertEqual(self.cursor.state['pending']['host_counter'], 7)
        self.reload().bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.assertEqual(self.cursor.state['index'], 0)

    def test_unrelated_existing_reservation_cannot_bind_or_enqueue(self):
        self.cursor.reserve_external(self.gen, 7)
        with self.assertRaises(CatalogRequestError): self.q.enqueue(self.cursor)
        self.assertIsNone(self.q.data['pending'])

    def test_fenced_close_rehosts_same_position_without_advancing(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor); self.ack(request); self.q.dispatch(self.cursor)
        self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        with self.assertRaises(CatalogRequestError):
            self.reload().retire_closed(self.cursor, request.request_id)
        # An unrelated close cannot retire this request.
        self.cursor.consume_close({'version': 1, 'event': 'lobby_closed',
                                   'session_generation': 'c' * 32, 'host_counter': 7})
        with self.assertRaises(CatalogRequestError):
            self.reload().retire_closed(self.cursor, request.request_id)
        self.cursor.consume_close({'version': 1, 'event': 'lobby_closed',
                                   'session_generation': self.gen, 'host_counter': 9})
        with self.assertRaises(CatalogRequestError):
            self.reload().retire_closed(self.cursor, request.request_id)
        event = self.journal / f'close-{self.gen}-7.json'
        event.write_text(json.dumps({'version': 1, 'event': 'lobby_closed',
                                     'session_generation': self.gen, 'host_counter': 7}) + '\n')
        event.chmod(0o600)
        self.assertEqual(CycleCoordinator(self.cursor, self.journal).consume().processed, 1)
        self.cursor = CycleState(self.root / 'cursor.json', ['one', 'one'])
        with patch.object(self.cursor, '_find_external', return_value=(7, 'b' * 32)):
            with self.assertRaises(CatalogRequestError):
                self.reload().retire_closed(self.cursor, request.request_id)
        self.assertTrue(self.reload().retire_closed(self.cursor, request.request_id))
        self.assertFalse(self.reload().retire_closed(self.cursor, request.request_id))
        with self.assertRaises(CatalogRequestError): self.reload().complete(self.cursor, request.request_id)
        self.assertEqual(self.cursor.state['index'], 0)
        next_request = self.reload().enqueue(self.cursor)
        self.assertNotEqual(next_request.request_id, request.request_id)
        self.assertEqual(next_request.index, request.index)
        q = self.reload(); q.dispatch(self.cursor); self.ack(next_request); q.dispatch(self.cursor)
        with self.assertRaises(CatalogRequestError):
            q.bind_reservation(self.cursor, next_request.request_id, 7, self.journal)
        q.bind_reservation(self.cursor, next_request.request_id, 8, self.journal)
        self.assertEqual(self.cursor.state['index'], 0)

    def test_started_lobby_cannot_be_retired_as_closed(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor); self.ack(request); self.q.dispatch(self.cursor)
        self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.start()
        with self.assertRaises(CatalogRequestError): self.reload().retire_closed(self.cursor, request.request_id)
        self.assertTrue(self.reload().complete(self.cursor, request.request_id))

    def test_sigkill_during_binding_recovers_same_lobby(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor); self.ack(request); self.q.dispatch(self.cursor)
        pid = os.fork()
        if pid == 0:
            with patch('src.runtime.catalog_requests.publish_reservation',
                       side_effect=lambda *_: os.kill(os.getpid(), signal.SIGKILL)):
                self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.cursor = CycleState(self.root / 'cursor.json', ['one', 'one'])
        self.assertEqual(self.reload().data['pending']['counter'], 7)
        gate = self.reload().bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.assertTrue((self.journal / gate).exists())
        with self.assertRaises(CatalogRequestError):
            self.reload().bind_reservation(self.cursor, request.request_id, 8, self.journal)

    def test_mailbox_occupied_until_native_consumes_and_next_id_proceeds(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor)
        mailbox = self.ipc / 'next.ready'
        self.assertEqual(mailbox.read_bytes(), request.payload())
        # Native writes ACK before it removes the occupied slot.
        ack = self.ipc / ('ack-' + request.request_id + '.ready')
        ack.write_bytes(request.payload() + b'status=staged\n'); ack.chmod(0o600)
        self.assertEqual(self.q.dispatch(self.cursor), (request, 'staged'))
        self.q.bind_reservation(self.cursor, request.request_id, 7, self.journal)
        self.start(); self.q.complete(self.cursor, request.request_id)
        other = self.q.enqueue(self.cursor)
        with self.assertRaises(ValueError): self.q.dispatch(self.cursor)
        self.assertEqual(mailbox.read_bytes(), request.payload())
        self.assertEqual(self.reload().data['pending']['request_id'], other.request_id)
        mailbox.unlink()  # Native alone removes the completed notification.
        self.assertEqual(self.reload().dispatch(self.cursor), (other, None))
        self.assertEqual(mailbox.read_bytes(), other.payload())
        self.assertEqual(self.reload().data['pending']['request_id'], other.request_id)

    def test_kill_between_immutable_request_and_notification(self):
        request = self.q.enqueue(self.cursor)
        pid = os.fork()
        if pid == 0:
            from src.runtime import preload_ipc
            original = preload_ipc._publish_exact
            def kill_before_mailbox(directory, name, payload, request=None):
                if name == 'next.ready': os.kill(os.getpid(), signal.SIGKILL)
                return original(directory, name, payload, request=request)
            with patch.object(preload_ipc, '_publish_exact', side_effect=kill_before_mailbox):
                self.q.dispatch(self.cursor)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertEqual((self.ipc / ('request-' + request.request_id + '.ready')).read_bytes(), request.payload())
        self.assertFalse((self.ipc / 'next.ready').exists())
        self.assertEqual(self.reload().dispatch(self.cursor), (request, None))
        self.assertEqual((self.ipc / 'next.ready').read_bytes(), request.payload())

    def test_kill_after_mailbox_rename_retries_same_request(self):
        request = self.q.enqueue(self.cursor)
        pid = os.fork()
        if pid == 0:
            from src.runtime import preload_ipc
            original = preload_ipc._rename_noreplace
            def kill_after_rename(fd, source, target):
                original(fd, source, target)
                if target == 'next.ready': os.kill(os.getpid(), signal.SIGKILL)
            with patch.object(preload_ipc, '_rename_noreplace', side_effect=kill_after_rename):
                self.q.dispatch(self.cursor)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertEqual((self.ipc / 'next.ready').read_bytes(), request.payload())
        self.assertEqual(self.reload().dispatch(self.cursor), (request, None))
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)

    def test_lost_native_ack_retries_identity_and_preserves_ambiguity(self):
        request = self.q.enqueue(self.cursor)
        self.q.dispatch(self.cursor)
        claim = self.ipc / ('claim-' + request.request_id + '.ready')
        claim.write_bytes(request.payload()); claim.chmod(0o600)
        (self.ipc / 'next.ready').unlink()
        # Re-notification carries the same ID; a native claim without ACK is
        # ambiguous and must not be interpreted as permission to restage.
        self.assertEqual(self.reload().dispatch(self.cursor), (request, None))
        self.assertEqual((self.ipc / 'next.ready').read_bytes(), request.payload())
        self.ack(request, 'uncertain')
        self.assertEqual(self.reload().dispatch(self.cursor), (request, 'uncertain'))
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)
        self.assertIsNone(self.cursor.state['pending'])

    def test_native_consumes_notification_during_verification(self):
        request = self.q.enqueue(self.cursor)
        def consumed(*_):
            from src.runtime.preload_ipc import notify_request
            notify_request(self.ipc, request)
            self.ack(request)
            raise FileNotFoundError('consumed during verification')
        with patch('src.runtime.catalog_requests.notify_request', side_effect=consumed):
            self.assertEqual(self.q.dispatch(self.cursor), (request, 'staged'))
        self.assertFalse((self.ipc / 'next.ready').exists())

    def test_missing_notification_without_ack_is_not_success(self):
        request = self.q.enqueue(self.cursor)
        with patch('src.runtime.catalog_requests.notify_request', side_effect=FileNotFoundError('missing')):
            with self.assertRaises(FileNotFoundError): self.q.dispatch(self.cursor)
        self.assertEqual(self.reload().data['pending']['request_id'], request.request_id)

    def test_conflicting_and_missing_ack_never_replace_intent(self):
        self.q.enqueue(self.cursor)
        req, _ = self.q.dispatch(self.cursor)
        ack = self.ack(req, 'uncertain')
        self.assertEqual(self.q.dispatch(self.cursor)[1], 'uncertain')
        ack.unlink()
        with self.assertRaises(CatalogRequestError): self.reload().dispatch(self.cursor)
        self.ack(req, 'staged')
        with self.assertRaises(CatalogRequestError): self.reload().dispatch(self.cursor)
        self.assertEqual(self.reload().data['pending']['request_id'], req.request_id)
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)

    def test_conflicting_cursor_or_identity_fails(self):
        self.q.enqueue(self.cursor)
        req, _ = self.q.dispatch(self.cursor)
        with self.assertRaises(ValueError): CatalogRequests(self.path, self.ipc, self.manifest, 'c'*32)
        self.cursor.state['index'] = 1
        with self.assertRaises(CatalogRequestError): self.q.dispatch(self.cursor)
        self.assertEqual(self.reload().data['pending']['request_id'], req.request_id)

    def catalog_close(self, request, counter=7, **overrides):
        event={'version':2,'event':'lobby_closed','session_generation':self.gen,'host_counter':counter,
               'request_id':request.request_id,'manifest_sha256':self.q.digest,'catalog_index':request.index,**overrides}
        path=self.journal/f"close-{event['session_generation']}-{counter}.json"
        path.write_text(json.dumps(event)+'\n');path.chmod(0o600)
        return path,event

    def test_v2_close_before_binding_retires_exact_request(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor)
        path,event=self.catalog_close(req)
        coordinator=CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q)
        self.assertEqual(coordinator.consume().processed,1)
        self.assertFalse(path.exists());self.assertTrue(path.with_suffix('.ack').exists())
        self.assertIsNone(self.q.data['pending'])
        self.assertEqual(self.q.data['completed']['status'],'closed')
        self.assertEqual(self.reload().data['completed']['outcome'],'closed')
        self.assertEqual(self.reload().data['version'],2)
        self.assertEqual(self.cursor.state['index'],0)
        self.assertEqual(self.cursor._find_catalog_close(self.gen,7),(req.request_id,self.q.digest,req.index))
        with self.assertRaises(ValueError):self.cursor.consume_external({'version':1,'event':'game_started',
            'session_generation':self.gen,'host_counter':7,'match_id':'b'*32})
        self.assertNotEqual(self.q.enqueue(self.cursor).request_id,req.request_id)

    def test_original_v2_queue_without_outcome_remains_replayable(self):
        from src.runtime.files import atomic_json
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);path,event=self.catalog_close(req)
        CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q).consume()
        legacy_completed=dict(self.q.data['completed']);legacy_completed.pop('outcome')
        atomic_json(self.path,{**self.q.data,'completed':legacy_completed})
        queue=self.reload()
        self.assertEqual(queue.data['version'],2)
        self.assertFalse(queue.retire_closed(self.cursor,req.request_id))
        with self.assertRaises(CatalogRequestError):queue.complete(self.cursor,req.request_id)
        self.assertFalse(queue.consume_catalog_close(self.cursor,event))

    def test_v2_queue_cannot_be_mislabeled_as_legacy(self):
        from src.runtime.files import atomic_json
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);self.catalog_close(req)
        CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q).consume()
        self.assertEqual(self.q.data['version'],2)
        atomic_json(self.path,{**self.q.data,'version':1})
        with self.assertRaises(CatalogRequestError):self.reload()

    def test_v2_close_replays_after_cursor_commit_before_queue_commit(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);path,event=self.catalog_close(req)
        coordinator=CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q)
        with patch.object(self.q,'_save',side_effect=OSError('crash after cursor close')):
            with self.assertRaises(Exception):coordinator.consume()
        self.assertTrue(path.exists());self.assertFalse(path.with_suffix('.ack').exists())
        self.assertEqual(self.cursor._find_catalog_close(self.gen,7),(req.request_id,self.q.digest,req.index))
        reopened=CycleState(self.root/'cursor.json',['one','one'])
        queue=self.reload();CycleCoordinator(reopened,self.journal,catalog_requests=queue).consume()
        self.assertIsNone(queue.data['pending']);self.assertEqual(queue.data['completed']['status'],'closed')
        self.assertEqual(reopened.state['index'],0)

    def test_v2_sigkill_after_cursor_commit_replays_before_ack(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);path,event=self.catalog_close(req)
        child=os.fork()
        if child==0:
            with patch.object(self.q,'_save',side_effect=lambda *_:os.kill(os.getpid(),signal.SIGKILL)):
                CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q).consume()
            os._exit(99)
        _,status=os.waitpid(child,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),-signal.SIGKILL)
        self.assertTrue(path.exists());self.assertFalse(path.with_suffix('.ack').exists())
        cursor=CycleState(self.root/'cursor.json',['one','one']);queue=self.reload()
        self.assertEqual(cursor._find_catalog_close(self.gen,7),(req.request_id,self.q.digest,req.index))
        self.assertEqual(queue.data['pending']['request_id'],req.request_id)
        self.assertEqual(CycleCoordinator(cursor,self.journal,catalog_requests=queue).consume().processed,1)
        self.assertIsNone(queue.data['pending']);self.assertTrue(path.with_suffix('.ack').exists())
        self.assertEqual(cursor.state['index'],0)

    def test_v2_foreign_or_conflicting_close_does_not_clear_reservation(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);self.ack(req);self.q.dispatch(self.cursor)
        self.q.bind_reservation(self.cursor,req.request_id,8,self.journal)
        from src.runtime.preload_ipc import Request
        stale=Request(self.gen,self.q.digest,'e'*32,req.index)
        path,event=self.catalog_close(stale,counter=7)
        coord=CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q);coord.consume()
        self.assertEqual(self.cursor.state['pending']['host_counter'],8)
        path,event=self.catalog_close(stale,counter=8)
        with self.assertRaises(Exception):coord.consume()
        self.assertTrue(path.exists());self.assertFalse(path.with_suffix('.ack').exists())
        self.assertEqual(self.q.data['pending']['request_id'],req.request_id)

    def test_late_v2_close_replay_does_not_retire_new_request(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor)
        path,event=self.catalog_close(req)
        raw=path.read_bytes()
        coordinator=CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q)
        coordinator.consume()
        newer=self.q.enqueue(self.cursor)
        self.assertNotEqual(newer.request_id,req.request_id)
        path.with_suffix('.ack').unlink()
        path.write_bytes(raw);path.chmod(0o600)
        self.assertEqual(coordinator.consume().processed,1)
        self.assertEqual(self.q.data['pending']['request_id'],newer.request_id)
        self.assertEqual(self.cursor.state['index'],0)
        self.assertFalse(list(self.journal.glob('reservation-*.ready')))

    def test_v2_requires_catalog_and_exact_manifest(self):
        req=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor)
        path,event=self.catalog_close(req,manifest_sha256='f'*64)
        with self.assertRaises(Exception):CycleCoordinator(self.cursor,self.journal).consume()
        with self.assertRaises(Exception):CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q).consume()
        self.assertTrue(path.exists());self.assertEqual(self.cursor.state['version'],1)
        self.assertEqual(self.q.data['pending']['request_id'],req.request_id)

    def test_kill_after_intent_before_publication(self):
        pid = os.fork()
        if pid == 0:
            self.q.enqueue(self.cursor)
            with patch('src.runtime.catalog_requests.notify_request', side_effect=lambda *_: os._exit(23)):
                self.q.dispatch(self.cursor)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        pending = self.reload().data['pending']
        self.assertIsNotNone(pending)
        req, _ = self.reload().dispatch(self.cursor)
        self.assertEqual(req.request_id, pending['request_id'])
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)

    def test_kill_after_publish_and_after_ack_commit(self):
        request = self.q.enqueue(self.cursor)
        pid = os.fork()
        if pid == 0:
            from src.runtime.preload_ipc import publish_request
            publish_request(self.ipc, request)
            os._exit(24)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 24)
        self.assertEqual(self.reload().dispatch(self.cursor), (request, None))
        self.ack(request)
        pid = os.fork()
        if pid == 0:
            self.reload().dispatch(self.cursor)
            os._exit(25)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 25)
        self.assertEqual(self.reload().dispatch(self.cursor), (request, 'staged'))
        self.assertEqual(len(list(self.ipc.glob('request-*.ready'))), 1)

    def test_failed_persistence_requires_reopen(self):
        with patch('src.runtime.catalog_requests.atomic_json', side_effect=OSError('sync')):
            with self.assertRaises(OSError): self.q.enqueue(self.cursor)
        with self.assertRaises(CatalogRequestError): self.q.enqueue(self.cursor)
        self.assertIsNone(self.reload().data['pending'])

    def test_missing_state_never_initializes_on_reopen(self):
        self.path.unlink()
        with self.assertRaises(ValueError): self.reload()
        self.assertFalse(self.path.exists())

if __name__ == '__main__': unittest.main()
