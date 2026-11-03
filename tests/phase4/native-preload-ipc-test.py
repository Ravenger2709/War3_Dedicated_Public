#!/usr/bin/env python3
"""Offline native explicit event-loop file request tests; no production polling."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.runtime.preload_ipc import Request, publish_request, notify_request, read_ack
from src.runtime.preload_manifest import seal_startup_manifest

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
fixtures=load('ipc_catalog_fixture',ROOT/'tests/phase4/test_preload_catalog.py')
gameplay=load('ipc_native_fixture',ROOT/'tests/phase3/native-test.py')

@unittest.skipUnless(Path(fixtures.fixture.STORM).is_file(),'offline StormLib required')
class NativeIPCTests(unittest.TestCase):
    def setUp(self):
        if Path('/sys/fs/cgroup').stat().st_ino!=1:self.skipTest('full cgroup hierarchy required')
        self.catalog=fixtures.PreparedCatalogTests('runTest');self.catalog.setUp();self.addCleanup(self.catalog.doCleanups)
        self.fixture=gameplay.GameplayTests('runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown);self.addCleanup(self.fixture.doCleanups)
        prepared=self.catalog.prepare();self.addCleanup(prepared.close)
        self.bundle=seal_startup_manifest(prepared);self.addCleanup(self.bundle.close)
        self.n=self.fixture.native({'bot_maxgames':'2','autohost_maxgames':'2',
            'bot_lobby_message_interval':'10','autohost_startplayers':'4','autohost_gamename':'Legacy','autohost_owner':'Owner'})
        shutil.copyfile(self.catalog.a,self.n.root/'a.w3x')
        (self.n.root/'synthetic.cfg').write_text('map_path = Maps\\Download\\a.w3x\nmap_localpath = a.w3x\n')
        for script in ('common.j','blizzard.j'):(self.n.root/script).write_bytes(b'globals\nendglobals\n')
        self.lifecycle=self.n.root/'lifecycle';self.lifecycle.mkdir(mode=0o700)
        self.ipc=self.n.root/'preload';self.ipc.mkdir(mode=0o700)
        with self.n.cfg.open('a') as stream:
            stream.write(f'bot_lifecycle_event_dir = {self.lifecycle}\nbot_start_reservation_gate = 1\n')
        self.n.passed_fds=self.bundle.pass_fds();self.n.start()
        self.generation=self.call('generation','TEST GENERATION ')
        self.assertEqual(len(self.generation),32)
        self.assertEqual(self.call(f'catalogholdbound {self.bundle.manifest_fd} {self.bundle.manifest_sha256}',
                                   'TEST CATALOGBOUNDED '),'2')
        self.assertEqual(self.call(f'catalogattach {self.ipc}','TEST CATALOGATTACH '),'1')
        for fd in self.bundle.pass_fds():self.assertEqual(self.call(f'closefd {fd}','TEST CLOSEFD '),'1')
        self.bundle.close();self.catalog.a.unlink();self.catalog.b.unlink();(self.n.root/'a.w3x').unlink()

    def call(self,command,marker):
        self.n.proc.stdin.write(command+'\n');self.n.proc.stdin.flush()
        result=self.n.marker(marker).split();self.n.marker('TEST COMMAND '+command)
        return result[-1]

    def process(self,request):
        return self.call(f'catalogrequest {self.ipc} {request.request_id}','TEST CATALOGREQUEST ')

    def test_151_closed_lobbies_and_old_request_replay(self):
        first=None
        for i in range(151):
            req=Request(self.generation,self.bundle.manifest_sha256,f'{i+1:032x}',i%2)
            if first is None:first=req
            publish_request(self.ipc,req)
            self.assertEqual(self.process(req),'1')
            self.assertEqual(read_ack(self.ipc,req),'staged')
            self.assertEqual(self.process(req),'1')
            self.n.command('autohost')
            gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
            self.assertEqual(self.n.host_state()['lobby']['map_name'], 'a.w3x' if i%2==0 else 'b.w3x')
            self.n.command('closecurrent')
            gameplay.wait(lambda:self.n.host_state()['lobby'] is None)
        self.assertEqual(self.process(first),'1')
        self.assertEqual(read_ack(self.ipc,first),'staged')
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])
        self.assertEqual(len(list(self.ipc.glob('claim-*.ready'))),151)
        self.assertEqual(len(list(self.ipc.glob('ack-*.ready'))),151)
        self.assertGreaterEqual(len(list(self.lifecycle.glob('close-*.json'))),151)

    def request(self, token, index):
        req=Request(self.generation,self.bundle.manifest_sha256,token,index)
        publish_request(self.ipc,req)
        self.assertEqual(self.process(req),'1')
        self.assertEqual(read_ack(self.ipc,req),'staged')
        return req

    def reserve_current(self):
        counter=self.n.host_state()['lobby']['host_counter']
        marker=self.lifecycle/f'reservation-{self.generation}-{counter}.ready'
        marker.write_text(f'version=1\nsession_generation={self.generation}\nhost_counter={counter}\n')
        marker.chmod(0o600)
        with marker.open('rb') as stream:os.fsync(stream.fileno())
        fd=os.open(self.lifecycle,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def test_two_active_matches_keep_actions_after_second_request(self):
        first=self.request('a'*32,0)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.reserve_current()
        player=self.n.peer('First',ready=False,game_keepalive=True)
        player.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        self.n.command('ping');self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==1)
        player.sock.sendall(gameplay.frame(0x23));gameplay.wait(lambda:any(x[1]==0x0c for x in player.frames))
        second=self.request('b'*32,1)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.assertEqual(self.n.host_state()['lobby']['map_name'],'b.w3x')
        self.assertEqual(self.process(first),'1');self.assertEqual(self.process(second),'1')
        count=sum(x[1]==0x0c for x in player.frames)
        gameplay.wait(lambda:sum(x[1]==0x0c for x in player.frames)>count)
        self.assertFalse(player.disconnected)
        self.reserve_current()
        player2=self.n.peer('Second',ready=False,game_keepalive=True)
        player2.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[1].size)))
        self.n.command('ping');self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==2)
        player2.sock.sendall(gameplay.frame(0x23))
        count=sum(x[1]==0x0c for x in player.frames)
        gameplay.wait(lambda:sum(x[1]==0x0c for x in player.frames)>count)
        self.assertFalse(player.disconnected)

    def test_publication_path_failure_preserves_actions_and_recovers_after_revoke(self):
        self.request('a'*32,0)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.reserve_current()
        player=self.n.peer('First',ready=False,game_keepalive=True)
        player.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        self.n.command('ping');self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==1)
        player.sock.sendall(gameplay.frame(0x23));gameplay.wait(lambda:any(x[1]==0x0c for x in player.frames))
        self.request('b'*32,1)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        gameplay.wait(lambda:self.n.snapshot()['operation']=='publish')
        old=self.n.root/'detached-ipc'
        self.n.ipc.rename(old);self.n.ipc.mkdir(mode=0o700)
        gameplay.wait(lambda:any('external publication write failed' in line for line in self.n.lines),timeout=8)
        self.assertIsNone(self.n.proc.poll())
        self.assertFalse(self.n.path.exists())
        self.assertFalse((old/self.n.path.name).exists()) # stale intent cannot revive
        before=len(player.frames)
        player.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(x[1]==0x0c and len(x)>8 for x in player.frames[before:]))
        self.assertFalse(player.disconnected)
        self.n.command('closecurrent');gameplay.wait(lambda:self.n.host_state()['lobby'] is None)
        self.request('c'*32,0)
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])
        self.assertEqual(self.n.host_state()['active_games'],1)
        outage=time.monotonic()
        gameplay.wait(lambda:time.monotonic()-outage>=3.2,timeout=6)
        self.assertIsNone(self.n.proc.poll())
        self.assertFalse(self.n.path.exists())
        before=len(player.frames)
        player.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(x[1]==0x0c and len(x)>8 for x in player.frames[before:]))
        self.n.ipc.rmdir();old.rename(self.n.ipc)
        gameplay.wait(lambda:any('external publication recovered' in line for line in self.n.lines),timeout=8)
        self.assertEqual(self.n.snapshot()['operation'],'unpublish')
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.assertEqual(self.n.host_state()['lobby']['map_name'],'a.w3x')
        before=len(player.frames)
        player.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(x[1]==0x0c and len(x)>8 for x in player.frames[before:]))
        self.assertFalse(player.disconnected)
        self.assertEqual(self.n.host_state()['active_games'],1)
        # Cross the native five-second LAN broadcast interval after the map
        # was released at start; the active game must continue servicing W3GS.
        start=time.monotonic()
        gameplay.wait(lambda:time.monotonic()-start>=5.5,timeout=8)
        self.assertIsNone(self.n.proc.poll())
        before=len(player.frames)
        player.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(x[1]==0x0c and len(x)>8 for x in player.frames[before:]))

    def test_unpublish_failure_during_start_keeps_game_and_recovers(self):
        self.request('a'*32,0)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.reserve_current()
        player=self.n.peer('First',ready=False,game_keepalive=True)
        player.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        self.n.command('ping')
        gameplay.wait(lambda:self.n.snapshot()['operation']=='publish')
        outside=self.n.root/'outside';outside.write_bytes(b'untouched')
        self.n.path.unlink();self.n.path.symlink_to(outside)
        self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==1)
        gameplay.wait(lambda:any('external publication stop failed' in line for line in self.n.lines))
        player.sock.sendall(gameplay.frame(0x23));gameplay.wait(lambda:any(x[1]==0x0c for x in player.frames))
        before=len(player.frames)
        player.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(x[1]==0x0c and len(x)>8 for x in player.frames[before:]))
        self.assertEqual(outside.read_bytes(),b'untouched')
        self.assertIsNone(self.n.proc.poll())
        self.n.path.unlink()
        gameplay.wait(lambda:any('external publication recovered' in line for line in self.n.lines),timeout=8)
        self.assertEqual(self.n.snapshot()['operation'],'unpublish')
        self.assertEqual(self.n.host_state()['active_games'],1)
        self.assertFalse(player.disconnected)

    def test_invalid_request_blocks_rotation_not_running_match(self):
        self.request('a'*32,0)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.reserve_current()
        player=self.n.peer('First',ready=False,game_keepalive=True)
        player.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        self.n.command('ping');self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==1)
        player.sock.sendall(gameplay.frame(0x23));gameplay.wait(lambda:any(x[1]==0x0c for x in player.frames))
        req=Request('f'*32,self.bundle.manifest_sha256,'e'*32,1)
        publish_request(self.ipc,req)
        self.assertEqual(self.process(req),'0')
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])
        count=sum(x[1]==0x0c for x in player.frames)
        gameplay.wait(lambda:sum(x[1]==0x0c for x in player.frames)>count)
        self.assertFalse(player.disconnected)
        self.assertEqual(self.n.host_state()['active_games'],1)

    def test_failed_bind_does_not_publish_lobby(self):
        self.request('d'*32,0)
        before=self.n.snapshot()
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            occupied.bind(('127.0.0.1',16113));occupied.listen()
            self.n.command('autohost')
            gameplay.wait(lambda:any('error listening on port' in line for line in self.n.lines))
            self.assertIsNone(self.n.host_state()['lobby'])
            after=self.n.snapshot()
            self.assertEqual(after['operation'],'unpublish')
            self.assertEqual(after['sequence'],before['sequence'])

    def test_single_mailbox_and_old_request_replay(self):
        req=Request(self.generation,self.bundle.manifest_sha256,'8'*32,0)
        notify_request(self.ipc,req)
        self.assertEqual(self.call('catalogpoll','TEST CATALOGPOLL '),'1')
        self.assertEqual(read_ack(self.ipc,req),'staged')
        self.assertFalse((self.ipc/'next.ready').exists())
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        self.n.command('closecurrent');gameplay.wait(lambda:self.n.host_state()['lobby'] is None)
        notify_request(self.ipc,req)
        self.assertEqual(self.call('catalogpoll','TEST CATALOGPOLL '),'1')
        self.assertFalse((self.ipc/'next.ready').exists())
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])

    def test_mailbox_mismatch_blocks_without_staging(self):
        req=Request(self.generation,self.bundle.manifest_sha256,'7'*32,0)
        notify_request(self.ipc,req)
        (self.ipc/'next.ready').write_bytes(req.payload().replace(b'index=0',b'index=1'))
        self.assertEqual(self.call('catalogpoll','TEST CATALOGPOLL '),'0')
        self.assertIsNone(read_ack(self.ipc,req))
        self.n.command('autohost');self.assertIsNone(self.n.host_state()['lobby'])

    def test_mailbox_claim_without_ack_is_uncertain(self):
        req=Request(self.generation,self.bundle.manifest_sha256,'6'*32,0)
        notify_request(self.ipc,req)
        claim=self.ipc/('claim-'+req.request_id+'.ready')
        claim.write_bytes(req.payload());claim.chmod(0o600)
        self.assertEqual(self.call('catalogpoll','TEST CATALOGPOLL '),'0')
        self.assertEqual(read_ack(self.ipc,req),'uncertain')
        self.assertTrue((self.ipc/'next.ready').exists())
        self.n.command('autohost');self.assertIsNone(self.n.host_state()['lobby'])

    def test_ipc_directory_replacement_after_attach_blocks(self):
        old=self.n.root/'preload-old'
        self.ipc.rename(old);self.ipc.mkdir(mode=0o700)
        req=Request(self.generation,self.bundle.manifest_sha256,'9'*32,0)
        publish_request(self.ipc,req)
        self.assertEqual(self.process(req),'0')
        self.assertIsNone(read_ack(self.ipc,req))
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])

    def test_lobby_proof_binds_request_index_counter_and_rehost_fences(self):
        first=self.request('a'*32,0)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        lobby=self.n.host_state()['lobby'];counter=lobby['host_counter']
        self.assertEqual((lobby['catalog_request_id'],lobby['catalog_index']), (first.request_id,0))
        self.n.command('rehosterror');gameplay.wait(lambda:self.n.host_state()['lobby'] is None)
        close=self.lifecycle/f'close-{self.generation}-{counter}.json'
        self.assertTrue(close.exists())
        self.assertEqual(json.loads(close.read_text()), {'version':2,'event':'lobby_closed',
            'session_generation':self.generation,'host_counter':counter,'request_id':first.request_id,
            'manifest_sha256':self.bundle.manifest_sha256,'catalog_index':0})
        self.assertEqual(self.process(first),'1')
        self.n.command('autohost');self.assertIsNone(self.n.host_state()['lobby'])
        second=self.request('b'*32,1)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        new=self.n.host_state()['lobby']
        self.assertNotEqual(new['host_counter'],counter)
        self.assertEqual((new['catalog_request_id'],new['catalog_index']), (second.request_id,1))
        self.assertEqual(new['map_name'],'b.w3x')

    def test_claim_without_ack_fails_closed_uncertain(self):
        req=Request(self.generation,self.bundle.manifest_sha256,'f'*32,0)
        publish_request(self.ipc,req)
        claim=self.ipc/('claim-'+req.request_id+'.ready')
        claim.write_bytes(req.payload());claim.chmod(0o600)
        with claim.open('rb') as stream:os.fsync(stream.fileno())
        self.assertEqual(self.process(req),'0')
        self.assertEqual(read_ack(self.ipc,req),'uncertain')
        self.n.command('autohost');self.n.command('trycreate')
        self.assertIsNone(self.n.host_state()['lobby'])

if __name__=='__main__':unittest.main()
