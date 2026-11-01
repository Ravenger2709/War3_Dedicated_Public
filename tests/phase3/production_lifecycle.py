"""Synthetic W3GS lifecycle against the real host; never executes a map engine.

Use only in the explicitly selected, isolated online acceptance container.
This proves host lifecycle and repeat admission, not Warcraft gameplay/desync.
"""
import hashlib
from pathlib import Path
import socket
import struct
import threading
import time


def frame(kind,body=b''):
    return struct.pack('<BBH',0xf7,kind,len(body)+4)+body


class Peer:
    def __init__(self,counter,map_size):
        self.sock=socket.create_connection(('127.0.0.1',6113),timeout=3)
        self.sock.settimeout(.2)
        self.lock=threading.Lock();self.closed=False;self.error=None
        self.pid=None;self.pings=0;self.loaded=False;self.slots=[];self.map_size=map_size
        self.action_frames=0;self.keepalives_sent=0
        body=struct.pack('<IIBHI',0xf0000000|counter,0,0,6112,0)
        body+=b'LifecycleProbe\0'+b'\0'*6+socket.inet_aton('127.0.0.1')
        self.send(0x1e,body)
        self.thread=threading.Thread(target=self.read,daemon=True);self.thread.start()

    def send(self,kind,body=b''):
        with self.lock:self.sock.sendall(frame(kind,body))

    def read(self):
        pending=bytearray()
        try:
            while not self.closed:
                try:data=self.sock.recv(8192)
                except socket.timeout:continue
                if not data:break
                pending.extend(data)
                while len(pending)>=4:
                    size=struct.unpack_from('<H',pending,2)[0]
                    if pending[0]!=0xf7 or not 4<=size<=65535:
                        raise RuntimeError('LIFECYCLE_BAD_W3GS_FRAME')
                    if len(pending)<size:break
                    packet=bytes(pending[:size]);del pending[:size]
                    kind=packet[1]
                    if kind==0x05:raise RuntimeError('LIFECYCLE_JOIN_REJECTED')
                    if kind==0x04:
                        self.pid=packet[6+struct.unpack_from('<H',packet,4)[0]]
                    if kind in (0x04,0x09):
                        self.slots.append((time.monotonic(),packet[7:7+packet[6]*9]))
                    if kind==0x01:
                        self.send(0x46,packet[4:]);self.pings+=1
                    elif kind==0x3d:
                        self.send(0x42,b'\0'*4+b'\x01'+struct.pack('<I',self.map_size))
                    elif kind==0x0b:
                        self.send(0x23);self.loaded=True
                    elif kind==0x0c:
                        self.action_frames+=1
                        self.send(0x27,b'\0'+struct.pack('<I',0));self.keepalives_sent+=1
        except (OSError,RuntimeError,IndexError,struct.error) as error:
            if not self.closed:
                self.error=str(error) if isinstance(error,RuntimeError) else 'LIFECYCLE_PEER_TRANSPORT'
        finally:
            if not self.closed:self.error=self.error or 'LIFECYCLE_PEER_DISCONNECTED'

    def vote(self):
        self.send(0x28,bytes((1,self.pid,self.pid,16))+b'!vote\0')

    def close(self):
        if self.closed:return
        try:self.send(0x21,struct.pack('<I',13))
        except OSError:pass
        self.closed=True
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close();self.thread.join(2)


def exercise(status,read,wait):
    initial=status();initial_game=initial['game'];counter=initial_game['host_counter']
    if (initial_game['game_name']!='Footman Ultimate AI' or initial_game['players']!=0
            or initial_game.get('computer_players')!=6):
        raise RuntimeError('LIFECYCLE_REQUIRES_EMPTY_CURATED_FOOTMAN')
    name=initial_game['map_name']
    if not isinstance(name,str) or Path(name).name!=name:
        raise RuntimeError('LIFECYCLE_MAP_PATH_REJECTED')
    selected=Path('/data/maps')/name
    if hashlib.sha256(selected.read_bytes()).hexdigest()!='71f4600ddb293b55bfe02ac301d7efec5f868a4eef4031bfa8dfbd0ce99ab58e':
        raise RuntimeError('LIFECYCLE_MAP_HASH_REJECTED')
    native_pid=initial['pid'];generation=initial['components']['star']['generation']
    baseline_relay=read('/data/state/bridge/relay-status.json')
    native_generation=baseline_relay.get('native_generation')
    relay_session=baseline_relay.get('session_generation')
    chat_returns=baseline_relay.get('chat_returns_confirmed',0)
    if type(native_generation) is not int or type(relay_session) is not int or type(chat_returns) is not int:
        raise RuntimeError('LIFECYCLE_RELAY_BASELINE_MISSING')
    peers=[]

    def checked(peer=None):
        s=status()
        if (s.get('game_engine_observed') or s['pid']!=native_pid
                or s['components']['star']['generation']!=generation
                or any(s['component_restarts'].values()) or s.get('permanent_errors')):
            raise RuntimeError('LIFECYCLE_COMPONENT_CHANGED')
        if peer and peer.error:raise RuntimeError(peer.error)
        return s

    try:
        peer=Peer(counter,selected.stat().st_size);peers.append(peer)
        wait(lambda: checked(peer)['game']['players']==1 and peer.pid is not None,15,'LIFECYCLE_FIRST_ADMISSION_TIMEOUT')
        wait(lambda: checked(peer)['game'].get('computer_players')==5,10,'LIFECYCLE_AI_REPLACEMENT_TIMEOUT')
        wait(lambda: checked(peer) and peer.pings>=3,120,'LIFECYCLE_PING_READINESS_TIMEOUT')
        # Observe at least one visible slot cycle while a real protocol peer is
        # present. Occupied rows must remain occupied throughout every update.
        closures={};durations=[];previous=None;baseline=None
        for observed,rows in list(peer.slots):
            if baseline is None:baseline=rows
            if len(rows)!=len(baseline) or not rows or len(rows)%9:
                raise RuntimeError('LIFECYCLE_SLOT_COUNT_CHANGED')
            for sid in range(len(rows)//9):
                row=rows[sid*9:sid*9+9]
                original=baseline[sid*9:sid*9+9]
                if original[2]==2:
                    # Download progress is the only expected human-row change.
                    if row[:1]+row[2:]!=original[:1]+original[2:]:
                        raise RuntimeError('LIFECYCLE_OCCUPIED_ROW_CHANGED')
                elif original[2]==1:
                    if row!=original:raise RuntimeError('LIFECYCLE_CLOSED_ROW_CHANGED')
                elif row[2] not in (0,1) or row[:2]+row[3:]!=original[:2]+original[3:]:
                    raise RuntimeError('LIFECYCLE_EMPTY_ROW_ROLE_CHANGED')
                if previous is not None:
                    old=previous[sid*9:sid*9+9]
                    if old[0]==0 and old[3]==0 and old[2]==0 and row[2]==1:
                        closures[sid]=observed
                    if sid in closures and row[2]==0:
                        durations.append(observed-closures.pop(sid))
            previous=rows
        visible=[x for x in durations if x>=.8]
        if not visible:raise RuntimeError('LIFECYCLE_VISIBLE_EMPTY_SLOT_REFRESH_MISSING')
        peer.vote()
        def playing():
            s=checked(peer);g=s['game']
            return (s['state']=='IN_GAME' and g.get('loading') is False
                    and g.get('host_counter')==counter and g.get('players')==1
                    and g.get('computer_players')==5 and g.get('ai_supported') is True
                    and peer.loaded and peer.action_frames>0 and peer.keepalives_sent>0)
        wait(playing,20,'LIFECYCLE_SOLO_VOTE_START_TIMEOUT')
        in_game=time.monotonic()
        while time.monotonic()-in_game<75:
            if not playing():
                raise RuntimeError('LIFECYCLE_SOLO_AI_GAME_ENDED_EARLY')
            time.sleep(.25)
        solo_seconds=time.monotonic()-in_game
        peer.close()
        wait(lambda: checked()['state']=='LOBBY' and checked()['game']['host_counter']>counter,
             100,'LIFECYCLE_EMPTY_GAME_REHOST_TIMEOUT')
        rehost=checked()['game']
        if rehost['players']!=0 or rehost.get('computer_players')!=6:
            raise RuntimeError('LIFECYCLE_REHOST_AI_NOT_RESTORED')
        new_counter=rehost['host_counter']
        relay={}
        def published_rehost():
            nonlocal relay
            checked()
            candidate=read('/data/state/bridge/relay-status.json')
            observed=candidate.get('observed_monotonic_seconds')
            if (type(observed) in (int,float) and 0<=time.monotonic()-observed<=3
                    and candidate.get('authenticated') is True and candidate.get('exact_lookup_visible') is True
                    and candidate.get('advertisement_accepted') is True
                    and candidate.get('native_host_counter')==new_counter
                    and candidate.get('native_pid')==native_pid
                    and candidate.get('native_generation')==native_generation
                    and candidate.get('session_generation')==relay_session
                    and candidate.get('chat_return_pending') is False
                    and type(candidate.get('chat_returns_confirmed')) is int
                    and candidate['chat_returns_confirmed']>chat_returns):
                relay=candidate;return True
            return False
        wait(published_rehost,100,'LIFECYCLE_REHOST_EXACT_LOOKUP_MISSING')
        again=Peer(new_counter,selected.stat().st_size);peers.append(again)
        wait(lambda: checked(again)['game']['players']==1 and again.pid is not None,
             15,'LIFECYCLE_REPEAT_ADMISSION_TIMEOUT')
        again.close()
        wait(lambda: checked()['game']['players']==0 and checked()['game'].get('computer_players')==6,
             15,'LIFECYCLE_LOBBY_LEAVE_AI_RESTORE_TIMEOUT')
        return {'result':'PASS','synthetic_protocol_peer':True,'game_engine_executed':False,
                'real_client_gameplay_claimed':False,'solo_ai_match_seconds':round(solo_seconds,3),
                'solo_ai_match_survived_75_seconds':True,'last_human_leave_rehost':True,
                'initial_host_counter':counter,'rehost_counter':new_counter,
                'post_rehost_exact_lookup_visible':True,'repeat_w3gs_admission':True,
                'ai_restored_after_leave':True,'occupied_lobby_rows_preserved':True,
                'game_loading_finished':True,'action_frames_received':peer.action_frames,
                'keepalives_sent':peer.keepalives_sent,
                'visible_empty_slot_cycles':len(visible),'shortest_visible_cycle_seconds':round(min(visible),3),
                'chat_returns_confirmed':relay['chat_returns_confirmed'],'component_restarts':0}
    finally:
        for peer in peers:peer.close()
