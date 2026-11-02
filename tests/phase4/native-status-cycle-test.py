#!/usr/bin/env python3
"""Real W3GS !status with native counts and bound, optional cycle metadata."""
import importlib.util
import os
from pathlib import Path
import struct
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('status_cycle_fixture',ROOT/'tests/phase4/native-preload-ipc-test.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
gameplay=base.gameplay

@unittest.skipUnless(Path(base.fixtures.fixture.STORM).is_file(),'offline StormLib required')
class NativeCycleStatusTests(unittest.TestCase):
    setUp=base.NativeIPCTests.setUp
    call=base.NativeIPCTests.call
    process=base.NativeIPCTests.process
    request=base.NativeIPCTests.request
    reserve_current=base.NativeIPCTests.reserve_current

    def lobby(self,request_id,index):
        self.request(request_id,index)
        self.n.command('autohost');gameplay.wait(lambda:self.n.host_state()['lobby'] is not None)
        return self.n.host_state()['lobby']

    def feedback(self,lobby,position,**updates):
        values={'version':1,'observed_monotonic_ms':int(time.monotonic()*1000),'ttl_ms':10000,
                'session_generation':self.generation,'manifest_sha256':self.bundle.manifest_sha256,
                'request_id':lobby['catalog_request_id'],'host_counter':lobby['host_counter'],
                'catalog_index':lobby['catalog_index'],'cycle_position':position}
        values.update(updates)
        path=Path(str(self.n.feedback)+'.cycle');path.unlink(missing_ok=True)
        path.write_text(''.join(k+' = '+str(v)+'\n' for k,v in values.items()));path.chmod(0o600)
        return path

    def status(self,peer):
        last=getattr(peer,'last_status',0.)
        gameplay.wait(lambda:time.monotonic()-last>=5.1,timeout=7)
        before=len(peer.chats());peer.chat('!status');peer.last_status=time.monotonic()
        gameplay.wait(lambda:any('Commands: !vote' in line for line in peer.chats()[before:]))
        lines=peer.chats()[before:]
        added=[line for line in lines if line.startswith(('Preset: ','Next preset: '))]
        self.assertEqual(len(added),2,lines)
        self.assertNotIn(peer.name,' '.join(added))
        self.assertNotIn(self.generation,' '.join(added))
        self.assertNotIn(self.bundle.manifest_sha256,' '.join(added))
        return '\n'.join(added)

    def player(self,name,index):
        peer=self.n.peer(name,ready=False,game_keepalive=True)
        peer.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[index].size)))
        return peer

    def start(self,peer,count):
        self.reserve_current();self.n.command('ping');self.n.command('startlobby')
        gameplay.wait(lambda:self.n.host_state()['active_games']==count)
        peer.sock.sendall(gameplay.frame(0x23));gameplay.wait(lambda:any(f[1]==0x0c for f in peer.frames))

    def actions(self,peer):
        before=len(peer.frames);peer.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
        gameplay.wait(lambda:any(f[1]==0x0c and len(f)>8 for f in peer.frames[before:]))
        self.assertFalse(peer.disconnected)

    def test_multiple_active_games_and_duplicate_ids_do_not_invent_next(self):
        a=self.lobby('a'*32,0);pa=self.player('PrivatePlayerA',0)
        self.feedback(a,0)
        text=self.status(pa)
        self.assertIn('Preset: one | Cycle lobby preset: one',text)
        self.assertIn('Next preset: two | Running matches: 0/2',text)
        self.start(pa,1)
        b=self.lobby('b'*32,1);pb=self.player('PrivatePlayerB',1)
        self.feedback(b,1)
        self.assertIn('Next preset: one | Running matches: 1/2',self.status(pb))
        self.start(pb,2)
        self.feedback(b,1)
        text=self.status(pa)
        self.assertIn('Preset: one | Cycle lobby preset: none',text)
        self.assertIn('Next preset: one | Running matches: 2/2',text)
        self.feedback(b,1,observed_monotonic_ms=int(time.monotonic()*1000)-11000)
        self.assertIn('Next preset: unavailable | Running matches: 2/2',self.status(pb))
        self.actions(pa);self.actions(pb)
        pa.close();gameplay.wait(lambda:self.n.host_state()['active_games']==1)
        c=self.lobby('c'*32,0);pc=self.player('PrivatePlayerC',0)
        self.feedback(c,2)  # Cycle is one, two, one: the second one wraps to one.
        text=self.status(pc)
        self.assertIn('Preset: one | Cycle lobby preset: one',text)
        self.assertIn('Next preset: one | Running matches: 1/2',text)
        self.feedback(c,0,request_id=a['catalog_request_id'])  # Same map, old occurrence.
        self.assertIn('Next preset: unavailable | Running matches: 1/2',self.status(pc))
        self.actions(pb)

    def test_wrong_binding_unsafe_or_missing_sidecar_is_display_only(self):
        lobby=self.lobby('d'*32,0)
        cases=[{'session_generation':'f'*32},{'request_id':'f'*32},{'host_counter':lobby['host_counter']+1},
               {'manifest_sha256':'f'*64},{'catalog_index':1},{'cycle_position':9},
               {'observed_monotonic_ms':int(time.monotonic()*1000)+60000},
               {'request_id':'private-secret-must-not-echo'},{'active_games':5}]
        for i,updates in enumerate(cases):
            with self.subTest(updates=updates):
                self.feedback(lobby,0,**updates)
                peer=self.player('PrivateCase'+str(i),0)
                try:
                    text=self.status(peer)
                    self.assertIn('Next preset: unavailable | Running matches: 0/2',text)
                    self.assertNotIn('private-secret',text)
                finally:peer.close()
                gameplay.wait(lambda:self.n.host_state()['lobby']['players']==0)
        for kind in ('missing','symlink','public_mode','duplicate','oversized'):
            with self.subTest(kind=kind):
                path=self.feedback(lobby,0)
                if kind=='missing':path.unlink()
                elif kind=='symlink':
                    target=path.with_name('sidecar-target');target.write_bytes(path.read_bytes());target.chmod(0o600)
                    path.unlink();path.symlink_to(target)
                elif kind=='public_mode':path.chmod(0o644)
                elif kind=='duplicate':
                    with path.open('a') as out:out.write('cycle_position = 0\n')
                else:path.write_bytes(b'x'*4097)
                peer=self.player('PrivateFile',0)
                try:self.assertIn('Next preset: unavailable | Running matches: 0/2',self.status(peer))
                finally:peer.close()
                gameplay.wait(lambda:self.n.host_state()['lobby']['players']==0)
        self.assertEqual(self.n.host_state()['lobby']['host_counter'],lobby['host_counter'])
        self.assertIsNone(self.n.proc.poll())

if __name__=='__main__':unittest.main(verbosity=2)
