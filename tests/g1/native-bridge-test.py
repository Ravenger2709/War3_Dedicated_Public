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
 def start(self,mode=2,pin=None,binding="manual",real_peers=2):
  n=self.native({'bot_g1_bridge_mode':str(mode),'bot_g1_map_sha256':pin or hashlib.sha256(b'').hexdigest(),
   'bot_g1_receiver_a':'832' if binding=='wire_probe' else '17','bot_g1_receiver_b':'832' if binding=='wire_probe' else '23','bot_g1_receiver_binding':binding,'bot_countdownseconds':'1','bot_savereplays':'1'})
  # Existing synthetic CGame fixture intentionally has no MPQ bytes.
  # Pin that exact empty byte string for this wire-only test.
  replay=n.root/'replays';replay.mkdir();n.cfg.write_text(n.cfg.read_text()+f'bot_replaypath = {replay}/\n')
  n.start().create()
  wait(lambda:n.path.exists())
  a=n.peer('Publisher',lan=True,game_keepalive=True)
  b=n.peer('Observer',game_keepalive=True) if real_peers==2 else None
  n.command('load')
  for peer in (a,b):
   if peer:peer.sock.sendall(g.frame(0x23))
  wait(lambda:n.state()['game_loaded'])
  return n,a,b
 def test_real_sender_probe_keeps_all_rows_and_requires_fresh_ack(self):
  n,a,b=self.start(mode=2,binding='wire_probe',real_peers=1);epoch=n.state()['host_counter']
  self.assertIsNone(b)
  slots=bytes(int(word,16) for word in n.state()['slots_hex'].split())
  self.assertEqual(len(slots),12*9)
  self.assertEqual(sum(slots[i+2]==2 for i in range(0,len(slots),9)),1)
  send(a,cache('wire_version',2)+cache('tick',8))
  probe=chat(f'~g1:{epoch}:0:8:0',832,832)
  wait(lambda:found(a,probe))
  records=[r for p in a.frames if p[1]==0x0c for r in action_records(p)]
  self.assertTrue(any(pid==a.pid and payload.endswith(probe) for pid,payload in records))
  self.assertFalse(any('[G1] fixed command queued' in line for line in n.lines))
  send(a,cache('tick',9)+cache('probe',epoch))
  wait(lambda:any('receiver probe ACK confirmed' in line for line in n.lines))
  send(a,cache('tick',16));command=chat(f'~g1:{epoch}:1:16:1',832,832)
  wait(lambda:found(a,command))
  send(a,cache('digest',456)+cache('ack',1))
  wait(lambda:any('[G1] ack ' in line and 'digest=456' in line for line in n.lines))
  self.assertFalse(any('POSITIVE CONTROL' in line for line in n.lines))
 def test_real_sender_without_probe_ack_never_issues_actor_command(self):
  n,a,_=self.start(mode=2,binding='wire_probe',real_peers=1)
  send(a,cache('wire_version',2)+cache('tick',8))
  wait(lambda:any('receiver probe queued merged=1' in line for line in n.lines))
  send(a,cache('tick',29))
  wait(lambda:any('probe timed out; actor commands disabled' in line for line in n.lines))
  send(a,cache('probe',n.state()['host_counter'])+cache('tick',40))
  time.sleep(.2)
  self.assertFalse(any('[G1] fixed command queued' in line for line in n.lines))
  self.assertFalse(any('receiver probe ACK confirmed' in line for line in n.lines))
  self.assertFalse(a.disconnected)
 def test_real_sender_profile_admits_twelve_without_fake_row(self):
  self.check_twelve_real_admissions(2)
 def test_dual_profile_admits_twelve_without_fake_row(self):
  self.check_twelve_real_admissions(4)
 def check_twelve_real_admissions(self,mode):
  settings={'bot_g1_bridge_mode':str(mode),'bot_g1_map_sha256':hashlib.sha256(b'').hexdigest(),
   'bot_g1_receiver_binding':'wire_probe','bot_g1_receiver_a':'821','bot_g1_receiver_b':'821'}
  if mode==4:settings.update(bot_g1_secondary_receiver_a='822',bot_g1_secondary_receiver_b='822')
  n=self.native(settings).start().create()
  peers=[n.peer('Human'+str(i),lan=True) for i in range(12)]
  wait(lambda:n.state()['players']==12)
  slots=bytes(int(word,16) for word in n.state()['slots_hex'].split())
  self.assertEqual(len(slots),12*9)
  self.assertEqual({slots[i] for i in range(0,len(slots),9)},{peer.pid for peer in peers})
  n.peer('Overflow',accepted=False)
  self.assertTrue(all(not peer.disconnected for peer in peers))
 def start_dual(self):
  n=self.native({'bot_g1_bridge_mode':'4','bot_g1_map_sha256':hashlib.sha256(b'').hexdigest(),
   'bot_g1_receiver_binding':'wire_probe','bot_g1_receiver_a':'821','bot_g1_receiver_b':'821',
   'bot_g1_secondary_receiver_a':'822','bot_g1_secondary_receiver_b':'822'})
  n.start().create();a=n.peer('First',lan=True,game_keepalive=True);b=n.peer('Second',game_keepalive=True)
  n.command('load')
  for peer in (a,b):peer.sock.sendall(g.frame(0x23))
  wait(lambda:n.state()['game_loaded'])
  return n,a,b
 def test_dual_probe_preserves_real_records_and_needs_both_acks(self):
  n,a,b=self.start_dual();epoch=n.state()['host_counter']
  original=cache('wire_version',3)+cache('tick',8)
  send(a,original);send(b,original)
  first=chat(f'~g1:{epoch}:0:8:0',821,821);second=chat(f'~g1:{epoch}:0:8:0',822,822)
  wait(lambda:found(a,first) and found(a,second) and found(b,first) and found(b,second))
  records=[r for p in a.frames if p[1]==0x0c for r in action_records(p)]
  for record in [(a.pid,original),(b.pid,original),(a.pid,first),(b.pid,second)]:self.assertIn(record,records)
  self.assertFalse(any('POSITIVE CONTROL' in l or '[G1] fixed command queued' in l for l in n.lines))
  send(a,cache('tick',9)+cache('raw0',0)+cache('raw1',1)+cache('accepted_probe',1)+cache('probe',epoch))
  wait(lambda:sum('dual probe ACK pid=' in l for l in n.lines)==1)
  self.assertFalse(any('dual probe confirmed by both peers' in l for l in n.lines))
  send(b,cache('tick',9)+cache('raw0',1)+cache('raw1',0)+cache('accepted_probe',1)+cache('probe',epoch))
  wait(lambda:any('dual probe confirmed by both peers' in l for l in n.lines))
  for peer in (a,b):send(peer,cache('tick',100))
  time.sleep(.2)
  self.assertFalse(any('dual probe failed' in l or '[G1] fixed command queued' in l for l in n.lines))
 def test_dual_probe_one_ack_cannot_complete_after_deadline(self):
  n,a,b=self.start_dual();epoch=n.state()['host_counter']
  for peer in (a,b):send(peer,cache('wire_version',3)+cache('tick',8))
  wait(lambda:any('dual probe queued real_senders=2' in l for l in n.lines))
  response=cache('raw0',1)+cache('raw1',1)+cache('accepted_probe',1)+cache('probe',epoch)
  send(a,cache('tick',9)+response)
  wait(lambda:any('dual probe ACK pid=' in l for l in n.lines))
  send(b,response+cache('tick',29))
  wait(lambda:any('dual probe failed' in l for l in n.lines))
  send(b,cache('tick',30)+response);time.sleep(.2)
  self.assertFalse(any('dual probe confirmed by both peers' in l or '[G1] fixed command queued' in l for l in n.lines))
  self.assertFalse(a.disconnected or b.disconnected)
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
 def test_explicit_control_uses_fake_sender_and_reports_real_ack(self):
  n,a,b=self.start(mode=3);epoch=n.state()['host_counter']
  slots=bytes(int(word,16) for word in n.state()['slots_hex'].split());fake=slots[11*9];self.assertNotIn(fake,(0,a.pid,b.pid))
  self.assertEqual(slots[11*9+2:11*9+4],bytes((2,0)))
  send(a,chat('g1:hello'));wait(lambda:any((a.pid,chat('g1:hello')) in action_records(p) for p in b.frames))
  send(a,cache('tick',8));expected=chat(f'~g1:{epoch}:1:8:1');wait(lambda:found(b,expected))
  packet=next(p for p in b.frames if p[1]==0x0c and expected in p[8:])
  self.assertIn((fake,expected),action_records(packet))
  send(a,cache('digest',321)+cache('ack',1));wait(lambda:any('[G1] ack ' in line for line in n.lines))
 def test_automatic_event_binding_probes_before_actor_commands(self):
  n,a,b=self.start(mode=3,binding='wire_probe');epoch=n.state()['host_counter']
  send(a,cache('wire_version',2)+cache('receiver0',0x100335)+cache('receiver11',0x100340)+cache('tick',8))
  probe=chat(f'~g1:{epoch}:0:8:0',832,832);wait(lambda:found(a,probe) and found(b,probe))
  self.assertFalse(any('[G1] fixed command queued' in line for line in n.lines))
  send(a,cache('tick',9)+cache('probe',epoch))
  wait(lambda:any('[G1] receiver probe ACK confirmed' in line for line in n.lines))
  send(a,cache('tick',16));command=chat(f'~g1:{epoch}:1:16:1',832,832);wait(lambda:found(b,command))
  send(a,cache('digest',777)+cache('ack',1));wait(lambda:any('[G1] ack ' in line and 'digest=777' in line for line in n.lines))
 def test_control_human_budget_is_eleven_and_fake_row_is_preserved(self):
  n=self.native({'bot_g1_bridge_mode':'3','bot_g1_map_sha256':hashlib.sha256(b'').hexdigest(),'bot_g1_receiver_a':'17','bot_g1_receiver_b':'23'}).start().create()
  fake=bytes(int(word,16) for word in n.state()['slots_hex'].split())[11*9:12*9]
  peers=[n.peer('Control'+str(i),lan=True) for i in range(11)]
  wait(lambda:n.state()['players']==11)
  n.peer('Overflow',accepted=False)
  n.peer('Owner',accepted=False)
  n.command('hold Reserved')
  n.peer('Reserved',accepted=False)
  self.assertTrue(all(not peer.disconnected for peer in peers))
  self.assertEqual(n.state()['players'],11)
  self.assertEqual(bytes(int(word,16) for word in n.state()['slots_hex'].split())[11*9:12*9],fake)
 def test_classic_pause_and_resume_forward_unchanged_and_keep_probe_working(self):
  n,a,b=self.start(mode=3,binding='wire_probe');epoch=n.state()['host_counter']
  def forwarded(raw):
   return any((a.pid,raw) in action_records(p) for p in b.frames if p[1]==0x0c)
  send(a,b'\x01');wait(lambda:forwarded(b'\x01'))
  batch=b'\x01'+cache('wire_version',2)+cache('tick',8)
  send(a,batch);wait(lambda:forwarded(batch))
  wait(lambda:found(b,chat(f'~g1:{epoch}:0:8:0',832,832)))
  send(a,b'\x02');wait(lambda:forwarded(b'\x02'))
  send(a,cache('tick',9)+cache('probe',epoch))
  wait(lambda:any('receiver probe ACK confirmed' in s for s in n.lines))
  send(a,cache('tick',16));wait(lambda:found(b,chat(f'~g1:{epoch}:1:16:1',832,832)))
  self.assertFalse(any('[G1] action rejected' in s for s in n.lines))
  self.assertFalse(a.disconnected or b.disconnected)
 def test_wrong_map_pin_leaves_existing_actions_unchanged(self):
  n,a,b=self.start(pin='0'*64)
  send(a,chat('g1:hello'));send(a,cache('tick',8));time.sleep(.25)
  self.assertFalse(any(line.startswith('[G1]') for line in n.lines))
  ordinary=chat('ordinary chat');send(a,ordinary);wait(lambda:found(b,ordinary))
 def test_control_wrong_pin_does_not_allocate_a_fake_player(self):
  n,a,b=self.start(mode=3,pin='0'*64)
  slots=bytes(int(word,16) for word in n.state()['slots_hex'].split())
  self.assertEqual(slots[11*9],0)
  self.assertFalse(any(line.startswith('[G1]') for line in n.lines))
 def test_invalid_enabled_config_fails_before_runtime(self):
  cases=[{'bot_g1_bridge_mode':'4'}, {'bot_g1_bridge_mode':'-1'}, {'bot_g1_bridge_mode':'2x'},
         {'bot_g1_bridge_mode':'3'},
         {'bot_g1_bridge_mode':'3','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_binding':'event_probe','bot_g1_receiver_a':'832','bot_g1_receiver_b':'832'},
         {'bot_g1_bridge_mode':'1','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_binding':'wire_probe','bot_g1_receiver_a':'832','bot_g1_receiver_b':'832'},
         {'bot_g1_bridge_mode':'3','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_binding':'wire_probe','bot_g1_receiver_a':'0','bot_g1_receiver_b':'832'},
         {'bot_g1_bridge_mode':'1','bot_g1_map_sha256':'bad'},
         {'bot_g1_bridge_mode':'2','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_a':'4294967296','bot_g1_receiver_b':'1'},
         {'bot_g1_bridge_mode':'2','bot_g1_map_sha256':'a'*64}]
  base={'bot_g1_bridge_mode':'4','bot_g1_map_sha256':'a'*64,'bot_g1_receiver_binding':'wire_probe','bot_g1_receiver_a':'821','bot_g1_receiver_b':'821','bot_g1_secondary_receiver_a':'822','bot_g1_secondary_receiver_b':'822'}
  cases += [{**base,'bot_g1_receiver_binding':'manual'},{**base,'bot_g1_secondary_receiver_a':'0'},{**base,'bot_g1_secondary_receiver_b':'65536'},{**base,'bot_g1_secondary_receiver_b':'x'}]
  for settings in cases:
   with self.subTest(settings=settings):
    n=self.native(settings).start(ready=False)
    self.assertEqual(n.proc.wait(timeout=5),78)
 def test_capture_never_injects_commands(self):
  n,a,b=self.start(mode=1);send(a,chat('g1:hello'));send(a,cache('tick',8))
  wait(lambda:any('[G1] calibration receiver_a=17 receiver_b=23' in line for line in n.lines))
  self.assertTrue(any(line.split('action_hex=',1)[1].split(' host_counter=',1)[0].replace(' ','').lower()==chat('g1:hello').hex() for line in n.lines if 'action_hex=' in line))
  send(b,chat("g1:hello",832,832))
  wait(lambda:any("calibration receiver_a=832 receiver_b=832" in line and "colour=1" in line for line in n.lines))
  self.assertFalse(any('[G1] fixed command' in line for line in n.lines))
if __name__=='__main__':unittest.main()
