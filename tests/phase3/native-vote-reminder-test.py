#!/usr/bin/env python3
"""Real W3GS timing: independent, per-game deferred vote reminders."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

gameplay=load('reminder_gameplay','tests/phase3/native-test.py')
wait=gameplay.wait
AUTO='Auto-start requires '
VOTE='Voting requires at least '

def messages(peer,prefix):
    result=[]
    for stamp,packet in zip(peer.frame_times,peer.frames):
        if packet[1]!=0x0f:continue
        count=packet[4];start=7+count+(4 if packet[6+count]==32 else 0)
        value=packet[start:-1].decode('utf8','replace')
        if value.startswith(prefix):result.append((stamp,value))
    return result

def settle(seconds):
    deadline=time.monotonic()+seconds
    wait(lambda:time.monotonic()>=deadline,seconds+2)

class VoteReminderTests(unittest.TestCase):
    setUp=gameplay.GameplayTests.setUp
    tearDown=gameplay.GameplayTests.tearDown
    native=gameplay.GameplayTests.native

    def start(self,autostart=4,minimum=2,auto=True,voting=True,**extra):
        n=self.native({'bot_autostartplayers':str(autostart),'bot_vote_min_players':str(minimum),
            'bot_autostart_enabled':'1' if auto else '0','bot_vote_enabled':'1' if voting else '0',
            'bot_countdownseconds':'8','bot_motdfile':'',**extra})
        n.start().create();return n

    def automatic(self,n,peer,required,humans):
        before=len(messages(peer,AUTO));n.command('auto')
        wait(lambda:len(messages(peer,AUTO))>before)
        stamp,text=messages(peer,AUTO)[-1]
        self.assertEqual(text,f'Auto-start requires {required} human players ({required-humans} more needed).')
        return stamp

    def reminder(self,peer,required,humans,after=None):
        wait(lambda:any(after is None or stamp>after for stamp,_ in messages(peer,VOTE)),6)
        stamp,text=next((stamp,text) for stamp,text in messages(peer,VOTE) if after is None or stamp>after)
        word='player' if required==1 else 'players'
        self.assertEqual(text,f'Voting requires at least {required} human {word} ({max(0,required-humans)} more needed). Type !vote when ready.')
        return stamp

    def test_four_two_delay_fresh_join_counts_and_repeated_calls_do_not_postpone(self):
        n=self.start();a=n.peer('First')
        first=self.automatic(n,a,4,1)
        settle(.65);n.command('auto');settle(.45)
        b=n.peer('Second');wait(lambda:n.state()['players']==2)
        n.command('auto');settle(.45);n.command('auto')
        self.assertFalse(messages(a,VOTE))
        sent=self.reminder(a,2,2)
        self.assertGreaterEqual(sent-first,3.2);self.assertLessEqual(sent-first,4.2)
        n.command('auto');settle(.4);n.command('auto');settle(3.8)
        self.assertEqual(len(messages(a,VOTE)),1)  # No second reminder only 3.5s after a repeated call.
        self.assertEqual(n.state()['phase'],'LOBBY')
        self.assertFalse(any('MOTD' in text for text in a.chats()))
        self.assertFalse(b.disconnected)

    def test_six_four_computes_missing_after_leave_and_keeps_vote_denial_separate(self):
        n=self.start(6,4);a=n.peer('First');b=n.peer('Second');c=n.peer('Third')
        wait(lambda:n.state()['players']==3)
        first=self.automatic(n,a,6,3)
        settle(.8);c.close();wait(lambda:n.state()['players']==2)
        sent=self.reminder(a,4,2)
        self.assertGreaterEqual(sent-first,3.2);self.assertLessEqual(sent-first,4.2)
        n.command('unlimit');before=len(a.chats());a.chat('!vote')
        wait(lambda:'Voting requires at least 4 human players (2 more needed).' in a.chats()[before:])
        self.assertEqual(n.state()['votes'],0);self.assertFalse(b.disconnected)

    def test_voting_disabled_has_only_auto_notice_and_command_denial(self):
        n=self.start(voting=False);a=n.peer('First')
        self.automatic(n,a,4,1);settle(4.2)
        self.assertFalse(messages(a,VOTE))
        self.assertNotIn('Voting is disabled.',a.chats())
        a.chat('!vote');wait(lambda:'Voting is disabled.' in a.chats())
        self.assertEqual(n.state()['phase'],'LOBBY')

    def test_auto_off_reminder_recurs_without_motd_or_autostart_calls(self):
        n=self.start(auto=False);a=n.peer('First')
        wait(lambda:len(messages(a,VOTE))==1,12)
        first,text=messages(a,VOTE)[0]
        self.assertEqual(text,'Voting requires at least 2 human players (1 more needed). Type !vote when ready.')
        b=n.peer('Second');wait(lambda:n.state()['players']==2)
        wait(lambda:len(messages(a,VOTE))==2,12)
        second,text=messages(a,VOTE)[1]
        self.assertGreaterEqual(second-first,9.5);self.assertLessEqual(second-first,10.6)
        self.assertEqual(text,'Voting requires at least 2 human players (0 more needed). Type !vote when ready.')
        self.assertFalse(messages(a,AUTO));self.assertEqual(n.state()['phase'],'LOBBY')
        self.assertFalse(b.disconnected)

    def test_countdown_loading_and_loaded_cancel_pending(self):
        for state in ('countdown','loading','loaded'):
            with self.subTest(state=state):
                n=self.start();a=n.peer('First');b=n.peer('Second')
                self.automatic(n,a,4,2)
                if state=='countdown':
                    a.chat('!vote');b.chat('!vote');wait(lambda:n.state()['phase']=='COUNTDOWN')
                else:
                    n.command('startlobby');wait(lambda:n.host_state()['active_games']==1)
                    if state=='loaded':
                        for peer in (a,b):peer.game_keepalive=True;peer.sock.sendall(gameplay.frame(0x23))
                        wait(lambda:any(packet[1]==0x0c for packet in a.frames))
                settle(4.2)
                self.assertFalse(messages(a,VOTE))
                n.stop()

    def test_closed_lobby_does_not_transfer_pending_timer_to_next_lobby(self):
        n=self.start();a=n.peer('First');self.automatic(n,a,4,1)
        settle(.8);n.command('removehost');a.close();n.create()
        b=n.peer('Second');settle(3.2)
        self.assertFalse(messages(b,VOTE))
        stamp=self.automatic(n,b,4,1);sent=self.reminder(b,2,1)
        self.assertGreaterEqual(sent-stamp,3.2);self.assertLessEqual(sent-stamp,4.2)

base=load('reminder_catalog_fixture','tests/phase4/native-preload-ipc-test.py')
@unittest.skipUnless(Path(base.fixtures.fixture.STORM).is_file(),'offline StormLib required')
class ConcurrentReminderTests(unittest.TestCase):
    def setUp(self):
        original=base.fixtures.PreparedCatalogTests.prepare
        def prepare(catalog,*args,**kwargs):
            catalog.doc['presets'][1]['vote']={'enabled':True}
            catalog.preset_path.write_text(json.dumps(catalog.doc))
            return original(catalog,*args,**kwargs)
        with patch.object(base.fixtures.PreparedCatalogTests,'prepare',prepare):
            base.NativeIPCTests.setUp(self)
    call=base.NativeIPCTests.call
    process=base.NativeIPCTests.process
    request=base.NativeIPCTests.request
    reserve_current=base.NativeIPCTests.reserve_current

    def test_delayed_new_map_reminder_keeps_older_match_w3gs_actions_flowing(self):
        self.request('a'*32,0);self.n.command('autohost')
        wait(lambda:self.n.host_state()['lobby'] is not None)
        a=self.n.peer('Playing',ready=False,game_keepalive=True)
        a.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[0].size)))
        wait(lambda:messages(a,AUTO),12)  # Schedule an old per-game reminder first.
        self.reserve_current();self.n.command('ping');self.n.command('startlobby')
        wait(lambda:self.n.host_state()['active_games']==1)
        a.sock.sendall(gameplay.frame(0x23));wait(lambda:any(p[1]==0x0c for p in a.frames))
        self.request('b'*32,1);self.n.command('autohost')
        wait(lambda:self.n.host_state()['lobby'] is not None)
        b=self.n.peer('Waiting',ready=False)
        b.sock.sendall(gameplay.frame(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.bundle.maps[1].size)))
        deadline=time.monotonic()+16;start=None;echoes=[]
        while time.monotonic()<deadline and not messages(b,VOTE):
            a.sock.sendall(gameplay.frame(0x26,struct.pack('<I',0)))
            if messages(b,AUTO) and start is None:start=messages(b,AUTO)[0][0]
            time.sleep(.08)
        self.assertIsNotNone(start)
        vote=messages(b,VOTE);self.assertEqual(len(vote),1)
        self.assertEqual(messages(b,AUTO)[0][1],'Auto-start requires 3 human players (2 more needed).')
        self.assertEqual(vote[0][1],'Voting requires at least 2 human players (1 more needed). Type !vote when ready.')
        self.assertGreaterEqual(vote[0][0]-start,3.2);self.assertLessEqual(vote[0][0]-start,4.2)
        echoes=[stamp for stamp,frame in zip(a.frame_times,a.frames) if start<=stamp<=vote[0][0] and frame[1]==0x0c and len(frame)>8]
        self.assertGreaterEqual(len(echoes),15)
        self.assertLess(max(right-left for left,right in zip(echoes,echoes[1:])),1.0)
        self.assertFalse(messages(a,VOTE));self.assertFalse(a.disconnected)

if __name__=='__main__':unittest.main(verbosity=2)
