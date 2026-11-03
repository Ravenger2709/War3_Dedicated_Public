"""Offline tests for request/ACK filesystem primitives; no native consumer."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.preload_ipc import PreloadIPCError, Request, publish_request, notify_request, read_ack


class PreloadIPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private'; self.root.mkdir(mode=0o700)
        self.request = Request('a'*32, 'b'*64, 'c'*32, 3)

    def test_atomic_publish_exact_retry_and_conflicts(self):
        name = publish_request(self.root, self.request)
        path = self.root / name
        before = path.stat()
        self.assertEqual(path.read_bytes(), self.request.payload())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(publish_request(self.root, self.request), name)
        self.assertEqual(path.stat().st_ino, before.st_ino)
        for changed in (Request('d'*32, 'b'*64, 'c'*32, 3),
                        Request('a'*32, 'e'*64, 'c'*32, 3),
                        Request('a'*32, 'b'*64, 'c'*32, 4)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                publish_request(self.root, changed)
        self.assertEqual(path.read_bytes(), self.request.payload())
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_single_mailbox_accepts_exact_retry_and_rejects_other_id(self):
        self.assertEqual(notify_request(self.root,self.request),'next.ready')
        self.assertEqual((self.root/'next.ready').read_bytes(),self.request.payload())
        self.assertEqual(notify_request(self.root,self.request),'next.ready')
        other=Request('a'*32,'b'*64,'d'*32,4)
        with self.assertRaises(ValueError):notify_request(self.root,other)
        self.assertEqual((self.root/'next.ready').read_bytes(),self.request.payload())
        self.assertEqual((self.root/('request-'+other.request_id+'.ready')).read_bytes(),other.payload())

    def test_invalid_schema_fails_before_writes(self):
        for args in [('A'*32,'b'*64,'c'*32,0), ('a'*32,'b'*64,'../x',0),
                     ('a'*32,'b'*64,'c'*32,True), ('a'*32,'b'*64,'c'*32,32),
                     ('a'*32,'b'*63,'c'*32,0)]:
            with self.subTest(args=args), self.assertRaises(PreloadIPCError): Request(*args)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_ack_identity_status_and_security(self):
        self.assertIsNone(read_ack(self.root, self.request))
        publish_request(self.root, self.request)
        path = self.root / ('ack-' + self.request.request_id + '.ready')
        for status in ('staged', 'rejected', 'uncertain'):
            path.write_bytes(self.request.payload() + f'status={status}\n'.encode()); path.chmod(0o600)
            self.assertEqual(read_ack(self.root, self.request), status)
        for data in (self.request.payload()+b'status=published\n', self.request.payload()+b'status=staged\nextra=1\n',
                     Request('d'*32,'b'*64,'c'*32,3).payload()+b'status=staged\n'):
            path.write_bytes(data); path.chmod(0o600)
            with self.assertRaises(PreloadIPCError): read_ack(self.root, self.request)
        path.unlink(); path.symlink_to('/dev/null')
        with self.assertRaises(OSError): read_ack(self.root, self.request)

    def test_symlink_hardlink_and_directory_permissions_fail(self):
        target = self.root / ('request-' + self.request.request_id + '.ready')
        target.symlink_to('/dev/null')
        with self.assertRaises(OSError): publish_request(self.root, self.request)
        target.unlink(); target.write_bytes(self.request.payload()); target.chmod(0o600)
        os.link(target, self.root/'hardlink')
        with self.assertRaises(ValueError): publish_request(self.root, self.request)
        target.unlink(); (self.root/'hardlink').unlink()
        self.root.chmod(0o755)
        with self.assertRaises(Exception): publish_request(self.root, self.request)

    def test_replaced_directory_is_rejected_after_publish(self):
        from src.runtime import preload_ipc
        original = preload_ipc._rename_noreplace
        old = self.root.parent / 'old'
        def swap(dir_fd, source, target):
            original(dir_fd, source, target)
            self.root.rename(old)
            self.root.mkdir(mode=0o700)
        with patch.object(preload_ipc, '_rename_noreplace', side_effect=swap):
            with self.assertRaises(Exception): publish_request(self.root, self.request)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual((old / ('request-' + self.request.request_id + '.ready')).read_bytes(), self.request.payload())

    def test_uncertain_sync_is_safe_to_retry(self):
        original = os.fsync; calls = 0
        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 3: raise OSError('injected directory sync failure')
            return original(fd)
        with patch('src.runtime.preload_ipc.os.fsync', side_effect=fail_directory):
            with self.assertRaises(OSError): publish_request(self.root, self.request)
        self.assertEqual(publish_request(self.root, self.request), 'request-' + self.request.request_id + '.ready')
        self.assertEqual(len(list(self.root.iterdir())), 1)

if __name__ == '__main__': unittest.main()
