#!/usr/bin/env python3
"""Synthetic W3GS through the actual CGame; not Warcraft execution/desync proof."""
import hashlib,importlib.util,struct,sys,tempfile,time,unittest,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('g1gameplay',ROOT/'tests/phase3/native-test.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
wait=g.wait

def chat(text,a=17,b=23):return b'\x60'+struct.pack('<II',a,b)+text.encode()+b'\0'
def cache(key,val):return b'\x6bAIG1.w3v\0g1\0'+key.encode()+b'\0'+struct.pack('<I',val)
def send(peer,data):peer.sock.sendall(g.frame(0x26,struct.pack('<I',zlib.crc32(data))+data))
def found(peer,raw):return any(p[1] in (0x0c,0x48) and raw in p[6:] for p in peer.frames)
def action_records(packet):
 if packet[1]!=0x0c or len(packet)<=8:return []
 assert struct.unpack_from('<H',packet,6)[0]==zlib.crc32(packet[8:])&0xffff
 offset=8;records=[]
 while offset<len(packet):
  pid=packet[offset];size=struct.unpack_from('<H',packet,offset+1)[0]
  payload=packet[offset+3:offset+3+size];assert len(payload)==size
  records.append((pid,payload));offset+=3+size
 assert offset==len(packet)
 return records
class G1Tests(unittest.TestCase):
 setUp=g.GameplayTests.setUp
 tearDown=g.GameplayTests.tearDown
 native=g.GameplayTests.native
 def start(self,mode=2,pin=None):
  n=self.native({'bot_g1_bridge_mode':str(mode),'bot_g1_map_sha256':pin or hashlib.sha256(b'').hexdigest(),
   'bot_g1_receiver_a':'17','bot_g1_receiver_b':'23','bot_countdownseconds':'1','bot_savereplays':'1'})
  # Existing synthetic CGame fixture intentionally has no MPQ bytes.
  # Pin that exact empty byte string for this wire-only test.
  replay=n.root/'replays';replay.mkdir();n.cfg.write_text(n.cfg.read_text()+f'bot_replaypath = {replay}/\n')
  n.start().create()
  wait(lambda:n.path.exists())
  a=n.peer('Publisher',lan=True,game_keepalive=True);b=n.peer('Observer',game_keepalive=True)
  n.command('load')
  for peer in (a,b):peer.sock.sendall(g.frame(0x23))
  wait(lambda:n.state()['game_loaded'])
  return n,a,b
 def test_roundtrip_sender_binding_filter_and_replay(self):
  n,a,b=self.start();epoch=n.state()['host_counter']
  send(b,chat('g1:hello'));send(b,cache('tick',8));time.sleep(.2)
  self.assertFalse(any('[G1] fixed command' in line for line in n.lines))
  send(a,chat('g1:hello'));wait(lambda:any('[G1] calibration' in line for line in n.lines))
  wait(lambda:any((a.pid,chat('g1:hello')) in action_records(p) for p in b.frames))
  send(a,cache('tick',8));expected=chat(f'~g1:{epoch}:1:8:1')
  wait(lambda:found(a,expected) and found(b,expected))
  packet=next(p for p in b.frames if p[1]==0x0c and expected in p[8:])
  self.assertEqual(struct.unpack_from('<H',packet,6)[0],zlib.crc32(packet[8:])&0xffff)
  offset=8;records=[]
  while offset<len(packet):
   pid=packet[offset];size=struct.unpack_from('<H',packet,offset+1)[0]
   payload=packet[offset+3:offset+3+size];self.assertEqual(len(payload),size)
   records.append((pid,payload));offset+=3+size
  self.assertEqual(offset,len(packet))
  self.assertEqual([payload for pid,payload in records if pid==a.pid],[cache('tick',8)+expected])
  for peer in (a,b):send(peer,cache('digest',1234)+cache('ack',1))
  wait(lambda:len([line for line in n.lines if '[G1] ack ' in line])==2)
  self.assertTrue(all('digest=1234' in line for line in n.lines if '[G1] ack ' in line))
  forged=chat('~g1:999:999:8:1');send(b,forged)
  wait(lambda:any('[G1] action rejected' in line for line in n.lines));time.sleep(.2)
  self.assertFalse(found(a,forged));self.assertFalse(found(b,forged))
  send(a,cache('tick',16));second=chat(f'~g1:{epoch}:2:16:2');wait(lambda:found(b,second))
  a.close();wait(lambda:n.state()['players']==1)
  self.assertFalse(n.state()['gameover_started'])
  send(b,cache('tick',32));time.sleep(.2)
  self.assertEqual(len([line for line in n.lines if '[G1] fixed command queued' in line]),2)
  b.close();n.stop()
  files=list((n.root/'replays').glob('*.w3g'));self.assertEqual(len(files),1)
  # Independent decompression: prove queued payload reached the replay stream.
  data=files[0].read_bytes();offset=struct.unpack_from('<I',data,28)[0];blocks=struct.unpack_from('<I',data,44)[0];raw=bytearray()
  for _ in range(blocks):
   compressed,expanded=struct.unpack_from('<HH',data,offset);offset+=8
   block=zlib.decompress(data[offset:offset+compressed]);self.assertEqual(len(block),expanded);raw.extend(block);offset+=compressed
  self.assertIn(expected,raw);self.assertIn(second,raw);self.assertNotIn(forged,raw)
 def test_wrong_map_pin_leaves_existing_actions_unchanged(self):
  n,a,b=self.start(pin='0'*64)
  send(a,chat('g1:hello'));send(a,cache('tick',8));time.sleep(.25)
  self.assertFalse(any(line.startswith('[G1]') for line in n.lines))
  ordinary=chat('ordinary chat');send(a,ordinary);wait(lambda:found(b,ordinary))
 def test_invalid_enabled_config_fails_before_runtime(self):
  cases=[{'bot_g1_bridge_mode':'-1'}, {'bot_g1_bridge_mode':'2x'},
         {'bot_g1_bridge_mode':'3'},
         {'bot_g1_bridge_mode':'1','bot_g1_map_sha256':'bad'},
         {'bot_g1_bridge_mode':'2','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_a':'4294967296','bot_g1_receiver_b':'1'},
         {'bot_g1_bridge_mode':'2','bot_g1_map_sha256':'a'*64}]
  for settings in cases:
   with self.subTest(settings=settings):
    n=self.native(settings).start(ready=False)
    self.assertEqual(n.proc.wait(timeout=5),78)
 def test_capture_never_injects_commands(self):
  n,a,b=self.start(mode=1);send(a,chat('g1:hello'));send(a,cache('tick',8))
  wait(lambda:any('[G1] calibration receiver_a=17 receiver_b=23' in line for line in n.lines))
  self.assertTrue(any(line.split('action_hex=',1)[1].split(' host_counter=',1)[0].replace(' ','').lower()==chat('g1:hello').hex() for line in n.lines if 'action_hex=' in line))
  self.assertFalse(any('[G1] fixed command' in line for line in n.lines))
if __name__=='__main__':unittest.main()
