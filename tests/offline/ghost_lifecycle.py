#!/usr/bin/env python3
"""Exercise the real GHost++ subprocess using synthetic loopback W3GS peers.

No engine, authentication, anti-cheat, map script execution or public network.
Synthetic identical checksums test transport only, not deterministic simulation.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]


def packet(kind, body=b''):
    return struct.pack('<BBH', 0xF7, kind, len(body) + 4) + body


class Peer:
    def __init__(self, info, name, download=False, bad_key=False):
        self.name, self.download = name, download
        self.sock = socket.create_connection((info.get('address', '127.0.0.1'), info['port']), 2)
        self.buffer = bytearray()
        self.pid = None
        self.counts = Counter()
        self.mapdata = bytearray()
        self.mapsize = None
        self.loaded = False
        self.forwarded = False
        self.countdown_started_at = None
        self.countdown_finished_at = None
        body = struct.pack('<IIBHI', info['counter'], info['key'] ^ int(bad_key), 0, 6112, 0)
        body += name.encode('ascii') + b'\0' + b'\0' * 6 + socket.inet_aton('127.0.0.1')
        self.sock.sendall(packet(0x1E, body))

    def send(self, kind, body=b''):
        self.sock.sendall(packet(kind, body))

    def receive(self):
        chunk = self.sock.recv(65536)
        assert chunk, f'{self.name} disconnected unexpectedly'
        self.buffer.extend(chunk)
        while len(self.buffer) >= 4:
            head, kind, size = struct.unpack_from('<BBH', self.buffer)
            assert head == 0xF7 and size >= 4, 'invalid host frame'
            if len(self.buffer) < size:
                break
            data = bytes(self.buffer[:size]); del self.buffer[:size]
            self.counts[kind] += 1
            if kind == 0x01:
                self.send(0x46, data[4:])
            elif kind == 0x04:
                self.pid = data[6 + struct.unpack_from('<H', data, 4)[0]]
            elif kind == 0x05:
                raise AssertionError(f'{self.name} rejected: {struct.unpack_from("<I",data,4)[0]}')
            elif kind == 0x3D:
                self.mapsize = struct.unpack_from('<I', data, data.index(0, 8) + 1)[0]
                self.send(0x42, struct.pack('<IBI', 1, 1, 0 if self.download else self.mapsize))
            elif kind == 0x43:
                start, crc = struct.unpack_from('<II', data, 10)
                block = data[18:]
                assert start == len(self.mapdata), 'map chunks out of order'
                assert zlib.crc32(block) == crc, 'map chunk CRC mismatch'
                self.mapdata.extend(block)
                self.send(0x44, data[4:6] + struct.pack('<II', 1, start))
                # This upstream advances its send window on MAPSIZE flag 3;
                # MAPPARTOK alone is not dispatched by CGamePlayer.
                self.send(0x42, struct.pack('<IBI', 1, 3, len(self.mapdata)))
                if len(self.mapdata) == self.mapsize:
                    self.send(0x42, struct.pack('<IBI', 1, 1, self.mapsize))
            elif kind == 0x0B:
                self.send(0x23)
                self.loaded = True
                self.countdown_finished_at = time.monotonic()
            elif kind == 0x0C:
                # Identical synthetic checksums acknowledge each action interval.
                self.send(0x27, struct.pack('<BI', 0, 0x12345678))
                if len(data) > 8:
                    self.forwarded = True
            elif kind == 0x0F and data.endswith(b'4. . .\0'):
                self.countdown_started_at = time.monotonic()

    def close(self):
        self.sock.close()


def discover(udp, timeout, port, after=0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = udp.recvfrom(8192)
        except socket.timeout:
            continue
        if len(data) >= 26 and data[:2] == b'\xf7\x30':
            counter, key = struct.unpack_from('<II', data, 12)
            advertised_port = struct.unpack_from('<H', data, len(data) - 2)[0]
            if counter > after and advertised_port == port:
                assert addr[0] == '127.0.0.1'
                return {'counter': counter, 'key': key,
                        'name': data[20:data.index(0, 20)].decode('utf-8'),
                        'port': struct.unpack_from('<H', data, len(data) - 2)[0]}
    raise AssertionError('no loopback W3GS_GAMEINFO before deadline')


def pump(peers, predicate, timeout):
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        for peer in peers:
            sel.register(peer.sock, selectors.EVENT_READ, peer)
        while not predicate():
            assert time.monotonic() < deadline, 'protocol phase timed out'
            for key, _ in sel.select(0.1):
                key.data.receive()


def run(runtime):
    manifest = json.loads((runtime / 'input-manifest.json').read_text())
    before_map = hashlib.sha256(Path(manifest['map']).read_bytes()).hexdigest()
    assert before_map == manifest['map_sha256']
    log = runtime / 'stdout.log'
    result = {'scope': 'real native GHost++; synthetic W3GS clients; no game engine', 'checks': [],
              'binary_sha256': hashlib.sha256((ROOT / 'build/ghost/ghost++').read_bytes()).hexdigest()}
    start = time.monotonic()
    peers = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, log.open('w') as output:
        udp.bind(('127.0.0.1', 6112)); udp.settimeout(0.25)
        proc = subprocess.Popen([str(ROOT / 'scripts/ghost-run.sh'), 'ghost.cfg'],
                                cwd=runtime, stdout=output, stderr=subprocess.STDOUT)
        try:
            info = discover(udp, 45, manifest['host_port'])
            assert info['port'] == manifest['host_port']
            result['checks'].append('headless lobby + genuine UDP discovery')
            bad = Peer(info, 'WrongEntryKey', bad_key=True)
            try:
                header = bad.sock.recv(8)
                assert header == packet(0x05, struct.pack('<I', 27)), header.hex()
            finally:
                bad.close()
            result['checks'].append('wrong LAN entry key rejected with W3GS_REJECTJOIN 27')
            one = Peer(info, 'OfflineOne', download=True); peers.append(one)
            pump(peers, lambda: len(one.mapdata) == one.mapsize and one.mapsize is not None, 15)
            assert hashlib.sha256(one.mapdata).hexdigest() == before_map
            result['checks'].append('real map transferred over W3GS_MAPPART; CRC and SHA256 verified')
            stop = time.monotonic() + 6
            pump(peers, lambda: time.monotonic() >= stop, 8)
            assert not one.loaded
            result['checks'].append('one player below threshold does not start match')
            two = Peer(info, 'OfflineTwo'); peers.append(two)
            pump(peers, lambda: two.pid is not None, 5)
            if manifest.get('max_players') == 2:
                extra = Peer(info, 'LobbyFull')
                try:
                    assert extra.sock.recv(8) == packet(0x05, struct.pack('<I', 9))
                finally:
                    extra.close()
                result['checks'].append('native closed map slots reject third client with W3GS_REJECTJOIN 9')
            pump(peers, lambda: all(p.loaded and p.counts[0x0C] >= 5 for p in peers), 45)
            assert 'finished loading with 2 players' in log.read_text()
            result['checks'].append('two peers join, acknowledge map/pings, autostart, load, receive action intervals')
            if manifest.get('countdown_seconds') == 2:
                assert one.countdown_started_at is not None
                delay = one.countdown_finished_at - one.countdown_started_at
                assert 1.5 <= delay <= 3.5, delay
                result['countdown_observed_seconds'] = round(delay, 3)
                result['checks'].append('configured 2-second countdown observed over host chat + COUNTDOWN_END')
            # Empty action = transport check without issuing any engine command.
            one.send(0x26, struct.pack('<I', zlib.crc32(b'')))
            pump(peers, lambda: all(p.forwarded for p in peers), 5)
            result['checks'].append('outgoing action relayed to both clients + keepalive checksums accepted')
            stop = time.monotonic() + 32
            pump(peers, lambda: time.monotonic() >= stop, 35)
            assert log.read_text().count('[GHOST] creating game [') == 1
            result['checks'].append('one-game policy: no second lobby across 32 seconds of active match (autohost interval 30s)')
            result['peers'] = {p.name: {'pid': p.pid, 'packets': {hex(k): v for k, v in p.counts.items()},
                                      'downloaded_bytes': len(p.mapdata)} for p in peers}
            for peer in peers:
                peer.send(0x21, struct.pack('<I', 7))
            # Keep sockets alive until server consumed LEAVEGAME and closes them;
            # closing while unread host frames remain can cause TCP RST instead.
            for peer in peers:
                peer.sock.shutdown(socket.SHUT_WR)
                peer.sock.settimeout(3)
                try:
                    while peer.sock.recv(65536):
                        pass
                except (socket.timeout, ConnectionResetError):
                    pass
            for peer in peers:
                peer.close()
            peers.clear()
            next_info = discover(udp, 40, manifest['host_port'], after=info['counter'])
            text = log.read_text()
            assert 'is over (no players left)' in text
            assert f"[GHOST] deleting game [{info['name']}]" in text
            assert next_info['counter'] == info['counter'] + 1
            result['checks'].append('both peers leave; real server deletes match and autohosts next lobby')
            result['lobbies'] = [info['name'], next_info['name']]
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=8) == 0
            assert 'all threads finished, exiting nicely' in log.read_text()
            result['checks'].append('SIGTERM orderly shutdown, exit 0')
            assert hashlib.sha256(Path(manifest['map']).read_bytes()).hexdigest() == before_map
            assert hashlib.sha256((runtime / 'game/War3Patch.mpq').read_bytes()).hexdigest() == manifest['patch_sha256']
            result['checks'].append('original map + patch archive SHA256 unchanged')
            result['result'] = 'PASS'
        except BaseException as exc:
            result.update(result='FAIL', error=str(exc))
            result['failed_phase_peers'] = {p.name: {'packets': dict(p.counts), 'map_bytes': len(p.mapdata),
                                                   'expected_map_bytes': p.mapsize} for p in peers}
            raise
        finally:
            for peer in peers:
                peer.close()
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            result['elapsed_seconds'] = round(time.monotonic() - start, 2)
            result['host_exit_code'] = proc.returncode
            result['map_sha256'] = before_map
            (runtime / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    run(parser.parse_args().runtime.resolve())
