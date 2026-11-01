#!/usr/bin/env python3
"""Native publication FILE IPC v1 and isolated integration tests.

Config (all optional, disabled when path is empty):
  bot_externalpublishpath = /absolute/owned-private700/intent.json
  bot_externalpublishrealm = Synthetic Realm
  bot_externalpublishhostname = SyntheticAccount
External mode requires no native BNCS connections. Only normal public maps are
published; private/saved/admin games are not published. No auth flag is changed.

Contract: latest-state mailbox, NOT a queue. UTF-8/ASCII JSON, <=4096 bytes,
exact keys: version=1, pid=positive native PID, generation=nonzero uint64,
sequence=positive uint64, host_counter=lower28 native counter,
operation='publish'|'unpublish', packet_hex=lowercase complete BNCS frame.
Generation is CLOCK_MONOTONIC nanoseconds XOR (pid<<32), fixed per process.
Sequence increases for each successful atomic replacement. Publish packet is
native SEND_SID_STARTADVEX3(public=16), current map metadata/SHA1/host config,
advertised counter=(15<<28)|host_counter. STOPADV is ff020400.
Startup writes unpublish(counter=0) to replace stale state. Successful listen
writes publish, then refreshes every >=3000ms, including full/countdown lobbies.
Loading/removal writes unpublish; shutdown leaves a terminal unpublish snapshot.
Rehost changes lower counter. Consumers reconcile snapshots; they must not rely
on observing every intervening write. No ACK, auth, or visibility is implied.
Bridge validates live session/ACK/list visibility independently, withdraws on
9-12s missing heartbeat, and coordinator monitors native/container death (PIDs
need not be visible in bridge namespace). No reply file is read.

The output and every parent component are opened without following symlinks.
Final directory is owned by euid, mode exactly0700; retained directory FD is
exclusively flocked. Existing output must be owned regular0600/single-link and
<=4096B. Each replacement is O_EXCL temporary0600, fsync, renameat, directory
fsync. Full directory path and inode are revalidated each write. Errors fail
closed and terminate native update; death/TTL handles cases where filesystem
failure prevents writing STOPADV. Keep the IPC directory on local filesystem.
Only frame hex contains hostname/game name. No credentials or raw auth inputs.

Reproduce from this clean worktree, with open-source dependency directory RO:
 docker --host unix:///var/run/docker.sock run --rm --pull=never --network=none \
   --read-only --cap-drop=ALL --security-opt=no-new-privileges:true \
   --user 1000:1000 --cpus=2 --memory=3g --pids-limit=128 --ulimit core=0:0 \
   --tmpfs /tmp:rw,nosuid,nodev,size=64m \
   --mount type=bind,src=WORKTREE,dst=/repo \
   --mount type=bind,src=OPEN_SOURCE_DEPS,dst=/deps,readonly \
   --workdir /repo --entrypoint /bin/bash LOCAL_BUILD_IMAGE_SHA256 \
   tests/bridge/native-publication-build.sh
Optional -e GHOST_SANITIZERS=address,undefined before image. Runtime tests use
synthetic metadata only: no MPQ/map binary, Windows, auth or external network.
"""
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'version', 'pid', 'generation', 'sequence', 'host_counter', 'operation', 'packet_hex'}


def wait(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('bounded condition timeout')


def intent(path):
    raw = path.read_bytes()
    assert len(raw) <= 4096
    obj = json.loads(raw)
    assert set(obj) == KEYS
    for name in ('version', 'pid', 'generation', 'sequence', 'host_counter'):
        assert type(obj[name]) is int
    assert obj['version'] == 1 and 0 < obj['pid'] < 2**32
    assert 0 < obj['generation'] < 2**64 and 0 < obj['sequence'] < 2**64
    assert 0 <= obj['host_counter'] < 2**28
    assert obj['operation'] in ('publish', 'unpublish')
    assert re.fullmatch(r'(?:[0-9a-f]{2})+', obj['packet_hex'])
    packet = bytes.fromhex(obj['packet_hex'])
    assert packet[0] == 255 and struct.unpack_from('<H', packet, 2)[0] == len(packet)
    assert packet[1] == (0x1c if obj['operation'] == 'publish' else 0x02)
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid() and info.st_nlink == 1
    return obj


def decode_stat(encoded):
    """Test decoder only; production bytes come exclusively from native code."""
    out = bytearray()
    for i in range(0, len(encoded), 8):
        mask = encoded[i]
        for j, value in enumerate(encoded[i + 1:i + 8], 1):
            out.append(value if mask & (1 << j) else value - 1)
    return bytes(out)


class Native:
    def __init__(self, root, extra=None, driver=True):
        self.root = root
        root.mkdir(mode=0o700, parents=True)
        self.ipc = root / 'ipc'
        self.ipc.mkdir(mode=0o700)
        self.path = self.ipc / 'intent.json'
        settings = {
            'bot_language': '/repo/vendor/ghostplusplus/language.cfg',
            'bot_defaultmap': 'synthetic.cfg', 'bot_mapcfgpath': str(root) + '/',
            'bot_war3path': str(root) + '/', 'bot_mappath': str(root) + '/',
            'bot_bindaddress': '127.0.0.1', 'bot_hostport': '16113', 'bot_reconnect': '0',
            'bot_savereplays': '0', 'bot_spoofchecks': '0', 'bot_requirespoofchecks': '0',
            'bot_checkmultipleipusage': '0', 'bot_ipblacklistfile': '',
            'bot_motdfile': '', 'bot_gameloadedfile': '', 'bot_gameoverfile': '',
            'bot_log': '', 'autohost_maxgames': '0', 'admingame_create': '0',
            'bnet_server': '', 'udp_broadcasttarget': '127.0.0.1',
            'db_sqlite3_file': str(root / 'ghost.dbs'),
            'bot_externalpublishpath': str(self.path),
            'bot_externalpublishrealm': 'Synthetic Realm',
            'bot_externalpublishhostname': 'SyntheticAccount',
        }
        settings.update(extra or {})
        self.cfg = root / 'ghost.cfg'
        self.cfg.write_text(''.join(f'{k} = {v}\n' for k, v in settings.items()))
        (root / 'synthetic.cfg').write_text('''map_path = Maps\\Download\\Synthetic.w3x
map_size = 100 0 0 0
map_info = 1 2 3 4
map_crc = 101 102 103 104
map_sha1 = ''' + ' '.join(map(str, range(20))) + '''
map_width = 64 0
map_height = 96 0
map_numplayers = 4
map_numteams = 4
map_filter_maker = 1
map_filter_type = 2
map_filter_size = 2
map_filter_obs = 4
map_options = 0
map_speed = 3
map_visibility = 4
map_observers = 1
map_flags = 3
map_slot1 = 0 255 0 0 0 0 32 1 100
map_slot2 = 0 255 0 0 1 1 32 1 100
map_slot3 = 0 255 0 0 2 2 32 1 100
map_slot4 = 0 255 0 0 3 3 32 1 100
''')
        self.driver = driver
        self.proc = None
        self.lines = []
        self.messages = queue.Queue()

    def start(self, ready=True):
        binary = ROOT / ('build/native-publication-driver' if self.driver else 'build/ghost/ghost++')
        self.proc = subprocess.Popen([str(binary), str(self.cfg)], cwd=self.root,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
        def reader():
            for line in self.proc.stdout:
                self.lines.append(line.rstrip())
                self.messages.put(line.rstrip())
        self.thread = threading.Thread(target=reader)
        self.thread.start()
        if ready:
            self.marker('TEST READY 0' if self.driver else '[GHOST] offline initialization complete')
        return self

    def marker(self, value):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                line = self.messages.get(timeout=.05)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError(f'native exited {self.proc.returncode}: {self.lines[-8:]}')
                continue
            if line.startswith(value):
                return line
        raise AssertionError(f'native marker timeout {value}: {self.lines[-8:]}')

    def command(self, value):
        self.proc.stdin.write(value + '\n')
        self.proc.stdin.flush()
        self.marker('TEST COMMAND ' + value)

    def snapshot(self):
        return intent(self.path)

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if self.driver:
                self.command('quit')
            else:
                self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout=8)
        self.thread.join(3)
        self.proc.stdin.close()
        self.proc.stdout.close()
        if code:
            raise AssertionError(f'native exit {code}: {self.lines[-15:]}')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-publish-')
        self.root = Path(self.temp.name)
        self.children = []

    def tearDown(self):
        for child in reversed(self.children):
            if child.proc is not None and child.proc.poll() is None:
                child.proc.kill()
                child.proc.wait(timeout=3)
            if child.proc is not None:
                child.thread.join(3)
                child.proc.stdin.close()
                child.proc.stdout.close()
        self.temp.cleanup()

    def native(self, extra=None, driver=True):
        child = Native(self.root / f'n{len(self.children)}', extra, driver)
        self.children.append(child)
        return child

    def test_native_serializer_heartbeat_start_remove_exit(self):
        child = self.native().start()
        initial = child.snapshot()
        self.assertEqual((initial['operation'], initial['sequence'], initial['host_counter']), ('unpublish', 1, 0))
        self.assertEqual(initial['packet_hex'], 'ff020400')
        child.command('create')
        first = child.snapshot()
        self.assertEqual(first['pid'], child.proc.pid)
        self.assertEqual((first['operation'], first['host_counter']), ('publish', 1))
        packet = bytes.fromhex(first['packet_hex'])
        self.assertEqual(struct.unpack_from('<II', packet, 4), (16, 0))
        self.assertEqual(struct.unpack_from('<I', packet, 12)[0], 0x452001)
        self.assertEqual(packet[24:40], b'Synthetic lobby\0')
        pos = 24 + len(b'Synthetic lobby\0')
        self.assertEqual(packet[pos:pos + 2], b'\0b')
        self.assertEqual(packet[pos + 2:pos + 10], b'1000000f')
        data = decode_stat(packet[pos + 10:-1])
        self.assertEqual(data[:5], bytes.fromhex('0248060000'))
        self.assertEqual(data[5:9], b'\x40\0\x60\0')
        self.assertEqual(data[9:13], bytes((101, 102, 103, 104)))
        self.assertEqual(data[13:], b'Maps\\Download\\Synthetic.w3x\0SyntheticAccount\0\0' + bytes(range(20)))
        # Repeated reads must always see an entire old/new inode, never partial JSON.
        started = time.monotonic()
        old_inode = child.path.stat().st_ino
        latest = first
        while latest['sequence'] == first['sequence'] and time.monotonic() - started < 4:
            latest = child.snapshot()
            time.sleep(.001)
        self.assertGreater(latest['sequence'], first['sequence'])
        self.assertGreater(time.monotonic() - started, 2.8)
        self.assertNotEqual(old_inode, child.path.stat().st_ino)
        self.assertEqual(latest['generation'], first['generation'])
        refreshed=bytes.fromhex(latest['packet_hex'])
        self.assertGreaterEqual(struct.unpack_from('<I',refreshed,8)[0],3)
        self.assertEqual(refreshed[:8]+refreshed[12:],packet[:8]+packet[12:])
        child.command('remove')
        removed = child.snapshot()
        self.assertEqual((removed['operation'], removed['host_counter']), ('unpublish', 1))
        child.command('create')
        self.assertEqual(child.snapshot()['host_counter'], 2)
        child.command('start')
        self.assertEqual(child.snapshot()['operation'], 'unpublish')
        child.stop()
        final = child.snapshot()
        self.assertEqual(final['operation'], 'unpublish')
        self.assertGreater(final['sequence'], removed['sequence'])
        self.assertEqual(list(child.ipc.glob('*.tmp')), [])

    def test_failed_bind_and_default_disabled(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1', 16113))
            occupied.listen()
            child = self.native().start()
            child.command('create')
            self.assertEqual(child.snapshot()['operation'], 'unpublish')
            time.sleep(.1)
            self.assertEqual(child.snapshot()['operation'], 'unpublish')
            child.stop()
        child = self.native({'bot_externalpublishpath': ''}).start()
        child.command('create')
        time.sleep(.1)
        self.assertFalse(child.path.exists())
        child.stop()

    def test_real_binary_autohost_and_graceful_sigterm(self):
        child = self.native({'autohost_maxgames': '1', 'autohost_startplayers': '2',
                             'autohost_gamename': 'Synthetic real', 'autohost_owner': 'Owner'}, driver=False).start()
        published = wait(lambda: (x if (x := child.snapshot())['operation'] == 'publish' else None), 34)
        self.assertEqual(published['host_counter'], 1)
        child.stop()
        self.assertEqual(child.snapshot()['operation'], 'unpublish')
        self.assertFalse(any('logon successful' in line for line in child.lines))

    def test_paths_existing_targets_and_single_owner(self):
        for case in ('mode', 'directory_link', 'ancestor_link', 'target_link', 'hardlink', 'fifo', 'target_mode', 'oversize', 'relative', 'dot', 'hostname'):
            with self.subTest(case=case):
                child = self.native()
                outside = child.root / 'outside'
                outside.write_bytes(b'unchanged')
                path = str(child.path)
                if case == 'mode': child.ipc.chmod(0o755)
                elif case == 'directory_link':
                    child.ipc.rename(child.root / 'real')
                    child.ipc.symlink_to(child.root / 'real', target_is_directory=True)
                elif case == 'ancestor_link':
                    alias = self.root / 'alias'; alias.symlink_to(child.root, target_is_directory=True)
                    path = str(alias / 'ipc/intent.json')
                elif case == 'target_link': child.path.symlink_to(outside)
                elif case == 'hardlink': os.link(outside, child.path); outside.chmod(0o600)
                elif case == 'fifo': os.mkfifo(child.path, 0o600)
                elif case == 'target_mode': child.path.write_text('{}'); child.path.chmod(0o644)
                elif case == 'oversize': child.path.write_bytes(b'x' * 4097); child.path.chmod(0o600)
                elif case == 'relative': path = 'ipc/intent.json'
                elif case == 'dot': path = str(child.ipc) + '/../ipc/intent.json'
                text = child.cfg.read_text().replace(str(child.path), path)
                if case == 'hostname': text = text.replace('SyntheticAccount', '')
                child.cfg.write_text(text)
                child.start(ready=False)
                self.assertEqual(child.proc.wait(timeout=4), 78)
                child.thread.join(2)
                self.assertEqual(outside.read_bytes(), b'unchanged')
        child = self.native().start()
        before = child.path.read_bytes()
        other = self.native({'bot_externalpublishpath': str(child.path)})
        other.start(ready=False)
        self.assertEqual(other.proc.wait(timeout=4), 78)
        self.assertEqual(child.path.read_bytes(), before)
        child.stop()

    def test_generation_restart_and_directory_replacement(self):
        child = self.native().start()
        child.command('create')
        previous = child.snapshot()
        child.stop()
        other = self.native({'bot_externalpublishpath': str(child.path)}).start()
        restarted = intent(child.path)
        self.assertNotEqual(previous['generation'], restarted['generation'])
        self.assertNotEqual(previous['pid'], restarted['pid'])
        self.assertEqual((restarted['operation'], restarted['sequence']), ('unpublish', 1))
        other.command('create')
        old = child.root / 'old-ipc'
        child.ipc.rename(old)
        child.ipc.mkdir(mode=0o700)
        self.assertEqual(other.proc.wait(timeout=5), 0)
        self.assertFalse(child.path.exists())
        self.assertTrue(any('write failed' in line for line in other.lines))

    def test_target_substitution_fails_closed(self):
        child = self.native().start()
        child.command('create')
        outside = child.root / 'outside'
        outside.write_bytes(b'unchanged')
        child.path.unlink()
        child.path.symlink_to(outside)
        self.assertEqual(child.proc.wait(timeout=5), 0)
        self.assertTrue(child.path.is_symlink())
        self.assertEqual(outside.read_bytes(), b'unchanged')
        self.assertTrue(any('write failed' in line for line in child.lines))

    def test_external_join_counter_lan_key_and_no_identity_promotion(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(udp.close)
        udp.bind(('127.0.0.1', 6112)); udp.settimeout(6)
        child = self.native().start()
        child.command('create')
        published = child.snapshot()
        lan, _ = udp.recvfrom(4096)
        counter, key = struct.unpack_from('<II', lan, 12)
        self.assertEqual(counter, published['host_counter'])
        def join(name, wire_counter, entry, accepted):
            sock = socket.create_connection(('127.0.0.1', 16113), timeout=2)
            self.addCleanup(sock.close)
            body = struct.pack('<IIBHI', wire_counter, entry, 0, 6112, 0) + name.encode() + b'\0' + b'\0' * 6 + socket.inet_aton('127.0.0.1')
            sock.sendall(struct.pack('<BBH', 0xf7, 0x1e, 4 + len(body)) + body)
            buffer = bytearray()
            found = False
            while not found:
                buffer.extend(sock.recv(4096))
                while len(buffer) >= 4:
                    size = struct.unpack_from('<H', buffer, 2)[0]
                    if len(buffer) < size: break
                    packet = bytes(buffer[:size]); del buffer[:size]
                    if packet[1] in (0x04, 0x05):
                        self.assertEqual(packet[1], 0x04 if accepted else 0x05)
                        found = True; break
            return sock
        join('WrongCounter', 0xf0000000 | (counter + 1), key, False)
        join('UnknownTag', 0xe0000000 | counter, key, False)
        join('WrongLanKey', counter, key ^ 1, False)
        external = join('External', 0xf0000000 | counter, 0, True)
        child.proc.stdin.write('inspect External\n'); child.proc.stdin.flush()
        self.assertEqual(child.marker('TEST PLAYER'), 'TEST PLAYER 1 0 1')
        child.marker('TEST COMMAND inspect External')
        lan_peer = join('Owner', counter, key, True)
        child.proc.stdin.write('inspect Owner\n'); child.proc.stdin.flush()
        self.assertEqual(child.marker('TEST PLAYER'), 'TEST PLAYER 1 1 0')
        child.marker('TEST COMMAND inspect Owner')
        child.command('private')
        wait(lambda: child.snapshot()['operation'] == 'unpublish')
        child.command('public')
        resumed = wait(lambda: x if (x := child.snapshot())['operation'] == 'publish' else None)
        self.assertEqual(resumed['host_counter'], 3)
        external.close(); lan_peer.close()
        child.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
