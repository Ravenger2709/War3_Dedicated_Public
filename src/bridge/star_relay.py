"""Session-owned BNCS publication relay (bounded research by default).

The unmodified Star client owns all authentication/Guard traffic. This relay
passes those frames unchanged and can send native GHost publication,
withdrawal, game-port and game-list requests after server-confirmed chat entry.
After withdrawal it replays the original client's bounded chat-entry/channel
frames before publishing another lobby, and requires fresh server confirmation.
No authentication packet is constructed here. Payloads are never logged.
"""
import argparse
import asyncio
from collections import Counter, deque
import configparser
from dataclasses import dataclass
import ipaddress
import json
import math
import os
import re
from pathlib import Path
import signal
import stat
import struct
import time
import tempfile

from .bncs import Frame, FrameDecoder


class RelayError(ValueError):
    pass


def wire(frame):
    return struct.pack('<BBH',255,frame.packet_id,len(frame.payload)+4)+frame.payload


def packet(packet_id,payload=b''):
    return wire(Frame(packet_id,payload))


def owned_file(path,limit=4096):
    """Private runtime input; errors never include file contents."""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>limit:
            raise RelayError('RELAY_PRIVATE_INPUT_REJECTED')
        data=os.read(fd,limit+1)
        if len(data)!=info.st_size:raise RelayError('RELAY_PRIVATE_INPUT_CHANGED')
        return data
    finally:os.close(fd)


def profile_username(path):
    cfg=configparser.RawConfigParser();cfg.optionxform=str
    cfg.read_string(owned_file(path,16384).decode('utf-8'))
    name=json.loads(cfg['General']['Username'])
    if not isinstance(name,str) or not 1<=len(name.encode())<=64 or any(ord(c)<32 for c in name):
        raise RelayError('RELAY_IDENTITY_INVALID')
    return name


def require_loopback_profile(path,port=6112):
    """Prevent a supplied profile from bypassing this experiment's relay."""
    cfg=configparser.RawConfigParser();cfg.optionxform=str
    cfg.read_string(owned_file(path,16384).decode('utf-8'))
    general=cfg['General']
    # Star loads UserConnections first, then its downloadable SiteConnections.
    # A selected user gateway survives an official server-list refresh.
    value=json.loads(general.get('UserConnections',general['SiteConnections']))
    if not isinstance(value,str) or not value.startswith('@ByteArray(') or not value.endswith(')'):
        raise RelayError('RELAY_GATEWAY_REJECTED')
    gateways=json.loads(value[len('@ByteArray('):-1])
    if not isinstance(gateways,dict) or len(gateways)!=1 or set(gateways.values())!={('127.0.0.1' if port==6112 else '127.0.0.1:'+str(port))} or general.get('Connections','').strip('"') or general.get('CurrConnections','').strip('"')!='0':
        raise RelayError('RELAY_GATEWAY_REJECTED')


def publication_intent(data):
    """Accept the bounded native intent format, never authentication frames."""
    obj=json.loads(data)
    if not isinstance(obj,dict) or set(obj)!={'version','pid','generation','sequence','host_counter','operation','packet_hex'} or type(obj['version']) is not int or obj['version']!=1:
        raise RelayError('RELAY_INTENT_SCHEMA')
    for key in ('pid','generation','sequence'):
        if type(obj[key]) is not int or not 0<obj[key]<2**64:raise RelayError('RELAY_INTENT_NUMBER')
    if type(obj['host_counter']) is not int or not 0<=obj['host_counter']<2**28 or obj['pid']>=2**31:raise RelayError('RELAY_INTENT_NUMBER')
    value=obj['packet_hex']
    if not isinstance(value,str) or len(value)>1024 or len(value)%2:raise RelayError('RELAY_INTENT_FRAME')
    try:raw=bytes.fromhex(value)
    except ValueError:raise RelayError('RELAY_INTENT_FRAME') from None
    if len(raw)<4 or raw[0]!=255 or int.from_bytes(raw[2:4],'little')!=len(raw):raise RelayError('RELAY_INTENT_FRAME')
    if obj['operation']=='unpublish':
        if raw!=packet(2):raise RelayError('RELAY_INTENT_FRAME')
        name=None
    elif obj['operation']=='publish':
        if not obj['host_counter'] or raw[1]!=0x1c or len(raw)<40 or struct.unpack_from('<I',raw,4)[0]!=16:raise RelayError('RELAY_INTENT_FRAME')
        try:
            name,password,stats=raw[24:].split(b'\0',2)
            counter=int(stats[1:9].decode()[::-1],16)
        except (ValueError,UnicodeError):raise RelayError('RELAY_INTENT_FRAME') from None
        if not 1<=len(name)<=31 or password or len(stats)<11 or stats[-1]!=0 or b'\0' in stats[:-1]:raise RelayError('RELAY_INTENT_FRAME')
        if counter!=(0xf0000000|obj['host_counter']):raise RelayError('RELAY_INTENT_COUNTER')
    else:raise RelayError('RELAY_INTENT_OPERATION')
    return obj,raw,name


def refresh_request(data,current,now):
    """A refresh is a hint for the current native publication, never a packet."""
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise RelayError('RELAY_REFRESH_SCHEMA')
            result[key]=value
        return result
    obj=json.loads(data,object_pairs_hook=unique)
    keys={'version','pid','generation','host_counter','sequence','observed_monotonic_seconds'}
    if not isinstance(obj,dict) or set(obj)!=keys or type(obj['version']) is not int or obj['version']!=1:
        raise RelayError('RELAY_REFRESH_SCHEMA')
    for key in ('pid','generation','sequence'):
        if type(obj[key]) is not int or not 0<obj[key]<2**64:raise RelayError('RELAY_REFRESH_NUMBER')
    if obj['pid']>=2**31 or type(obj['host_counter']) is not int or not 0<obj['host_counter']<2**28:
        raise RelayError('RELAY_REFRESH_NUMBER')
    observed=obj['observed_monotonic_seconds']
    if type(observed) not in (int,float) or not 0<=observed<=now or not math.isfinite(observed) or now-observed>10:
        raise RelayError('RELAY_REFRESH_EXPIRED')
    if current is None or current[0]['operation']!='publish' or any(obj[key]!=current[0][key] for key in ('pid','generation','host_counter')):
        raise RelayError('RELAY_REFRESH_CONTEXT')
    return obj


def list_request(name,public_directory=False):
    if not name or b'\0' in name or len(name)>31:raise RelayError('RELAY_GAME_NAME')
    return packet(9,struct.pack('<4I',0 if public_directory else 0x3ff,0 if public_directory else 0x3ff,0,1000 if public_directory else 1)+(b'' if public_directory else name)+b'\0\0\0')


def listed_game(payload,name,counter,port,diagnostics=None):
    """Strict Warcraft III SID_GETADVLISTEX records (BNETDocs S>C 0x09)."""
    if len(payload)<4:raise RelayError('RELAY_LIST_TRUNCATED')
    count=struct.unpack_from('<I',payload)[0]
    if count>1000:raise RelayError('RELAY_LIST_LIMIT')
    if not count:
        if len(payload)!=8:raise RelayError('RELAY_LIST_TRUNCATED')
        return False,struct.unpack_from('<I',payload,4)[0]
    offset=4;found=False
    for record_index in range(count):
        if offset+32>len(payload):raise RelayError('RELAY_LIST_TRUNCATED')
        family=struct.unpack_from('<H',payload,offset+8)[0]
        settings=struct.unpack_from('<I',payload,offset)[0]
        game_port=struct.unpack_from('>H',payload,offset+10)[0];little_port=struct.unpack_from('<H',payload,offset+10)[0]
        status=struct.unpack_from('<I',payload,offset+24)[0]
        if diagnostics is not None:
            states=diagnostics.setdefault('directory_state_counts',{})
            state_key=str(status) if status<=0x100 else 'OTHER'
            states[state_key]=states.get(state_key,0)+1
        if family!=2 or payload[offset+16:offset+24]!=b'\0'*8:raise RelayError('RELAY_LIST_ADDRESS')
        offset+=32;fields=[]
        for _ in range(3):
            end=payload.find(b'\0',offset)
            if end<0:raise RelayError('RELAY_LIST_TRUNCATED')
            fields.append(payload[offset:end]);offset=end+1
        title,password,stats=fields
        if diagnostics is not None and title.lower()==name.lower():
            diagnostics.update({'matching_title':title==name,'matching_title_casefold':True,'port_network_matches':game_port==port,'port_little_matches':little_port==port,'public_state':status in (4,16),'settings_public':not bool(settings&0x800),'listed_state':status if status<=0x100 else 'OTHER','counter_matches':False})
        if title==name and not password and len(stats)>=10:
            try:listed_counter=int(stats[1:9].decode()[::-1],16)
            except (ValueError,UnicodeError):raise RelayError('RELAY_LIST_COUNTER') from None
            if diagnostics is not None:diagnostics['counter_matches']=listed_counter==counter
            # PvPGN's GAME_STATUS_OPEN=4 differs from Blizzard's public=16.
            # Empty-list status 4 still means "started" and never reaches here.
            matches=game_port==port and status in (4,16) and not settings&0x800 and listed_counter==counter
            if matches and diagnostics is not None:
                # Order in this server response only; never claim client sorting
                # or a global rank across pages from this bounded observation.
                diagnostics.setdefault('matching_record_positions_1based',[]).append(record_index+1)
            found|=matches
    if offset!=len(payload):raise RelayError('RELAY_LIST_TRAILING')
    return found,None


@dataclass(repr=False)
class Settings:
    upstream:str
    upstream_port:int
    expected_username:str
    intent:Path
    status:Path
    game_port:int=6113
    duration:int=120
    publish:bool=False
    listen_port:int=6112
    continuous:bool=False
    publication_min_interval:float|None=None
    refresh_request:Path|None=None
    refresh_min_interval:float=5
    realm_idle_timeout:float=60

    def __post_init__(self):
        if self.publication_min_interval is None:self.publication_min_interval=30 if self.continuous else 2.5
        if not math.isfinite(self.publication_min_interval) or self.publication_min_interval<2.5 or not math.isfinite(self.refresh_min_interval) or self.refresh_min_interval<5 or not math.isfinite(self.realm_idle_timeout) or self.realm_idle_timeout<10:
            raise RelayError('RELAY_LIMIT_REJECTED')


class StarRelay:
    def __init__(self,settings):
        self.cfg=settings;self.stop=asyncio.Event();self.lock=asyncio.Lock();self.upstream=None
        self.connected=False;self.used=False;self.identity=False;self.channel=False;self.welcome=False
        self.advert_pending=deque();self.query_pending=deque();self.generation=None;self.sequence=0
        self.current=None;self.last_native=0;self.last_sent=0;self.last_query=0;self.last_ack=0
        self.advertised=False;self.visible=False;self.publication_failed=False;self.last_status=None;self.error=None
        self.counts={'client':Counter(),'server':Counter()};self.advertisements=0;self.queries=0;self.withdrawals=0
        self.native_acks=0;self.client_sessions=0;self.rejected_connections=0
        self.ever_authenticated=False;self.ever_accepted=False;self.ever_visible=False;self.epoch=0
        self.last_visibility_match=0;self.session_tasks=set();self.last_list_length=None;self.last_list_count=None
        self.native_intents_observed=0;self.native_publication_intents_observed=0
        self.last_named_match={};self.last_directory_match={};self.last_positive_directory_match={};self.directory_queries=0;self.named_queries=0
        self.netgameport_sent=False
        self.named_visible=False;self.last_named_visibility_match=0;self.public_match_key=None
        self.original_commands=Counter();self.control_timeline=[];self.started=time.monotonic()

        self.requested_identity_matches=False;self.server_logon_status=None;self.server_proof_status=None;self.realm_welcome=False;self.chat_events=Counter()
        self.proof_time=None;self.original_enterchat=False;self.original_channel_list=False;self.original_join_channel=None;self.postauth_enterchat_sent=False;self.postauth_channel_replayed=False;self.netgameport_before_enterchat=False;self.last_list_match={}
        self.active_session=None;self.closing=False;self.session_generation=0;self.listening=False
        self.session_started=None;self.session_closed=None;self.last_client_traffic=None;self.last_server_traffic=None;self.authenticated_at=None
        self.native_watermarks={};self.retired_generations=set();self.refresh_watermarks={}
        self.refresh_pending=None;self.refresh_inflight=set();self.refresh_requested=0;self.refresh_sent=0;self.refresh_successful=0;self.refresh_failed=0
        self.last_refresh_sent=None;self.last_refresh_requested=None;self.last_refresh_ack=None;self.last_refresh_result=None
        self.last_refresh_request_sequence=None;self.last_refresh_request_generation=None
        self.last_refresh_ack_context=None
        self.refresh_request_error=None
        self.last_ack_context=None;self.retry_at=0;self.publication_failures=0
        self.last_published_sequence=0
        self.original_enterchat_packet=None
        self.chat_return_required=False;self.chat_return_started=None;self.chat_return_join_pending=False
        self.enterchat_pending=deque();self.chat_return_context=None;self.chat_response_owner=None
        self.chat_returns=0

    def control_event(self,name):
        if len(self.control_timeline)<128:self.control_timeline.append({'event':name,'seconds':round(time.monotonic()-self.started,3)})

    def session_event(self,event):
        self.control_event(event)
        if self.cfg.continuous:
            print(json.dumps({'event':event,'relay_pid':os.getpid(),'session_generation':self.session_generation,
                              'observed_monotonic_seconds':time.monotonic(),'error_present':self.error is not None}),flush=True)

    @property
    def authenticated(self):return self.connected and self.channel and self.identity and self.chat_return_started is None

    def finish_refresh(self,request,result):
        if request is None:return
        if result=='accepted':self.refresh_successful+=1
        else:self.refresh_failed+=1
        self.last_refresh_result={'pid':request[0],'generation':request[1],'host_counter':request[2],'sequence':request[3],'status':result}
        self.refresh_inflight.discard(request)

    def cancel_refreshes(self):
        if self.refresh_pending is not None:
            self.finish_refresh(self.refresh_key(self.refresh_pending),'cancelled');self.refresh_pending=None
        for request in tuple(self.refresh_inflight):self.finish_refresh(request,'cancelled')

    @staticmethod
    def refresh_key(obj):return tuple(obj[key] for key in ('pid','generation','host_counter','sequence'))

    def reset_session(self):
        """Called only with both old transports and their tasks closed."""
        self.cancel_refreshes()
        self.connected=self.identity=self.channel=self.welcome=self.realm_welcome=False
        self.advertised=self.visible=self.named_visible=False;self.public_match_key=None
        self.requested_identity_matches=False;self.server_logon_status=self.server_proof_status=self.proof_time=None
        self.original_enterchat=self.original_channel_list=self.postauth_enterchat_sent=self.postauth_channel_replayed=False
        self.original_join_channel=None;self.netgameport_sent=self.netgameport_before_enterchat=False
        self.original_enterchat_packet=None
        self.chat_return_required=False;self.chat_return_started=None;self.chat_return_join_pending=False
        self.enterchat_pending.clear();self.chat_return_context=None;self.chat_response_owner=None
        self.advert_pending.clear();self.query_pending.clear();self.epoch+=1
        self.current=None;self.generation=None;self.sequence=0
        self.last_native=self.last_sent=self.last_query=self.last_ack=0
        self.last_published_sequence=0
        self.last_status=self.last_ack_context=None;self.publication_failed=False
        self.last_visibility_match=self.last_named_visibility_match=0
        self.last_list_length=self.last_list_count=None
        self.last_list_match={};self.last_named_match={};self.last_directory_match={};self.last_positive_directory_match={}
        self.last_client_traffic=self.last_server_traffic=self.authenticated_at=None
        self.chat_events.clear();self.original_commands.clear();self.control_timeline.clear()
        self.retry_at=0;self.publication_failures=0;self.error=None
        # A file left by the preceding cohort is a baseline, never live progress.
        try:
            obj,_,_=publication_intent(owned_file(self.cfg.intent))
            key=(obj['pid'],obj['generation'])
            self.native_watermarks[key]=max(self.native_watermarks.get(key,0),obj['sequence'])
        except (OSError,ValueError,KeyError,TypeError,UnicodeError):pass

    def record(self):
        self.ever_authenticated|=self.authenticated;self.ever_accepted|=self.advertised;self.ever_visible|=self.visible
        now=time.monotonic()
        native_fresh=self.current is not None and 0<=now-self.last_native<=10
        accepted=self.advertised and (not self.cfg.continuous or (self.authenticated and native_fresh))
        realm_healthy=self.authenticated and self.last_server_traffic is not None and 0<=now-self.last_server_traffic<=self.cfg.realm_idle_timeout
        r={'observed_monotonic_seconds':now,'visibility_match_age_seconds':None if not self.last_visibility_match else round(max(0,now-self.last_visibility_match),3),'last_native_list_response_bytes':self.last_list_length,'last_native_list_count':self.last_list_count,'last_native_list_match':self.last_list_match,'netgameport_before_enterchat':self.netgameport_before_enterchat,'schema':1,'experimental':True,'connected':self.connected,'authenticated':self.authenticated,
           'exact_lookup_visible':self.authenticated and accepted and self.named_visible and 0<=now-self.last_named_visibility_match<=8,'exact_lookup_match_age_seconds':None if not self.last_named_visibility_match else round(max(0,now-self.last_named_visibility_match),3),
           'public_directory_seen_for_current_lobby':self.authenticated and accepted and self.current is not None and self.public_match_key==(self.epoch,self.lobby_key()) and 0<=now-self.last_visibility_match<=60,
           'expected_identity_confirmed':self.connected and (self.identity or (self.requested_identity_matches and self.server_proof_status==0 and self.welcome and self.realm_welcome)),'postauth_enterchat_sent':self.postauth_enterchat_sent,'postauth_channel_replayed':self.postauth_channel_replayed,'requested_identity_matches':self.requested_identity_matches,'server_logon_status':self.server_logon_status,'server_proof_status':self.server_proof_status,'realm_welcome_observed':self.realm_welcome,'chat_event_counts':dict(self.chat_events),'channel_confirmed':self.channel,'welcome_observed':self.welcome,
           'ever_authenticated':self.ever_authenticated,'ever_advertisement_accepted':self.ever_accepted,'ever_visible_in_realm_query':self.ever_visible,
           'native_intents_observed':self.native_intents_observed,'native_publication_intents_observed':self.native_publication_intents_observed,'native_intent_seen':self.current is not None,'advertisement_accepted':accepted,
           'visible_in_realm_query':self.visible and (not self.cfg.continuous or (accepted and 0<=now-self.last_visibility_match<=8)),'publication_failed':self.publication_failed,'server_publication_status':self.last_status,
           'advertisements_sent':self.advertisements,'publication_acks':self.native_acks,'game_queries_sent':self.queries,'game_query_scope':'separate_directory_discovery_and_exact_lookup','named_game_queries_sent':self.named_queries,'directory_queries_sent':self.directory_queries,'last_named_list_match':self.last_named_match,'last_directory_list_match':self.last_directory_match,'last_positive_directory_match':self.last_positive_directory_match,
           'withdrawals_sent':self.withdrawals,'sessions':self.client_sessions,'rejected_connections':self.rejected_connections,
           'packet_counts':{direction:{f'0x{k:02x}':v for k,v in count.items()} for direction,count in self.counts.items()},
           'error':self.error,'original_command_categories':dict(self.original_commands),'control_timeline':self.control_timeline,'auth_or_guard_packets_constructed':False,'payloads_logged':False}
        r.update({'experimental':not self.cfg.continuous,'continuous':self.cfg.continuous,'relay_pid':os.getpid(),
                  'relay_started_monotonic_seconds':self.started,
                  'session_generation':self.session_generation,'session_started_monotonic_seconds':self.session_started,'session_closed_monotonic_seconds':self.session_closed,
                  'listener_ready':self.listening,'session_closing':self.closing,'realm_connection_healthy':realm_healthy,
                  'last_client_traffic_monotonic_seconds':self.last_client_traffic,'last_server_traffic_monotonic_seconds':self.last_server_traffic,
                  'last_traffic_monotonic_seconds':max((v for v in (self.last_client_traffic,self.last_server_traffic) if v is not None),default=None),
                  'authenticated_monotonic_seconds':self.authenticated_at,'native_heartbeat_fresh':native_fresh,
                  'last_native_intent_monotonic_seconds':self.last_native or None,'native_pid':None if self.generation is None else self.generation[0],
                  'native_generation':None if self.generation is None else self.generation[1],'native_sequence':self.sequence,
                  'native_host_counter':None if self.current is None else self.current[0]['host_counter'],
                  'last_ack_monotonic_seconds':self.last_ack or None,'last_ack_status':self.last_status,'last_ack_context':self.last_ack_context,
                  'publication_min_interval_seconds':self.cfg.publication_min_interval,'publication_retry_monotonic_seconds':self.retry_at or None,
                  'refresh_requested_count':self.refresh_requested,'refresh_sent_count':self.refresh_sent,'refresh_successful_count':self.refresh_successful,'refresh_failed_count':self.refresh_failed,
                  'last_refresh_requested_monotonic_seconds':self.last_refresh_requested,'last_refresh_sent_monotonic_seconds':self.last_refresh_sent,
                  'last_refresh_request_sequence':self.last_refresh_request_sequence,'last_refresh_request_generation':self.last_refresh_request_generation,
                  'last_refresh_ack_monotonic_seconds':self.last_refresh_ack,'last_refresh_result':self.last_refresh_result,
                  'last_refresh_ack_context':self.last_refresh_ack_context,
                  'refresh_request_error':self.refresh_request_error,
                  'chat_return_pending':self.chat_return_started is not None,
                  'chat_returns_confirmed':self.chat_returns,
                  'public_directory_position':None,'bump_proven':False})
        target=self.cfg.status;fd,temporary=tempfile.mkstemp(prefix=target.name+'.',dir=target.parent)
        try:
            with os.fdopen(fd,'w') as out:json.dump(r,out,indent=2);out.write('\n')
            os.replace(temporary,target)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        return r

    def lobby_key(self):
        if self.current is None:return None
        obj,raw,name=self.current
        return (self.generation,obj['host_counter'],name,raw[:8]+raw[12:])

    async def send(self,raw,owner=None,refresh=None,chat_owner=None):
        if self.upstream is None:raise RelayError('RELAY_NO_SESSION')
        upstream=self.upstream;session=self.session_generation
        native_sequence=owner[1] if owner is not None else None
        async with self.lock:
            if upstream is not self.upstream or session!=self.session_generation or self.closing:raise RelayError('RELAY_NO_SESSION')
            if raw[1]==0x1c:
                if owner is not None:owner=(self.epoch,self.lobby_key(),refresh)
                if len(self.advert_pending)>=128:raise RelayError('RELAY_RESPONSE_QUEUE_LIMIT')
                self.advert_pending.append((owner,time.monotonic()))
            elif raw[1]==9:
                if owner is not None:owner=(*owner,self.epoch,self.lobby_key())
                if len(self.query_pending)>=128:raise RelayError('RELAY_RESPONSE_QUEUE_LIMIT')
                self.query_pending.append((owner,time.monotonic()))
            elif raw[1]==0x0a:
                if len(self.enterchat_pending)>=128:raise RelayError('RELAY_RESPONSE_QUEUE_LIMIT')
                self.enterchat_pending.append((chat_owner,time.monotonic()))
            upstream.write(raw)
            if raw[1]==0x1c and owner is not None:
                self.advertisements+=1;self.last_sent=time.monotonic();self.last_published_sequence=native_sequence
                self.control_event('NATIVE_ADVERTISEMENT')
                if refresh is not None:self.refresh_sent+=1;self.last_refresh_sent=self.last_sent
            await asyncio.wait_for(upstream.drain(),8)
            if upstream is not self.upstream or session!=self.session_generation or self.closing:raise RelayError('RELAY_NO_SESSION')

    async def withdraw(self):
        if self.cfg.publish and self.authenticated and self.current is not None and self.upstream is not None:
            await self.send(packet(2));self.withdrawals+=1
            self.chat_return_required=True
        self.cancel_refreshes()
        self.advertised=False;self.visible=False;self.named_visible=False;self.public_match_key=None;self.epoch+=1

    async def return_to_chat(self,now):
        """Return the existing authenticated Star session to hosting state.

        ACK 0 for STARTADV alone does not prove that a post-game advertisement
        is visible. Match the normal STOPADV -> ENTERCHAT -> JOINCHANNEL
        lifecycle using only frames already sent by this original client.
        """
        if self.original_enterchat_packet is None or self.original_join_channel is None:
            self.error='RELAY_CHAT_RETURN_UNAVAILABLE'
            raise RelayError('RELAY_CHAT_RETURN_UNAVAILABLE')
        self.chat_return_required=False;self.chat_return_started=now;self.chat_return_join_pending=True
        self.chat_return_context=(self.session_generation,self.epoch)
        self.channel=False;self.identity=False
        self.control_event('LOBBY_RETURN_ENTERCHAT')
        await self.send(self.original_enterchat_packet,chat_owner=self.chat_return_context)

    async def chat_return_response(self,frame):
        if self.chat_return_started is None:return False
        if self.chat_return_context!=(self.session_generation,self.epoch):return False
        if frame.packet_id==0x0a and self.chat_return_join_pending:
            if self.chat_response_owner!=self.chat_return_context:return False
            if not self.identity:
                self.error='RELAY_CHAT_RETURN_IDENTITY'
                raise RelayError('RELAY_CHAT_RETURN_IDENTITY')
            self.chat_return_join_pending=False
            self.channel=False  # Earlier/unsolicited channel events cannot release this gate.
            await self.send(self.original_join_channel)
            self.control_event('LOBBY_RETURN_JOINCHANNEL')
            return True  # This response belongs to our replay, not a Star request.
        if frame.packet_id==0x0f and len(frame.payload)>=26 and struct.unpack_from('<I',frame.payload)[0]==7:
            if self.chat_return_join_pending or not self.identity:
                return False
            self.chat_return_started=None;self.chat_returns+=1
            self.control_event('LOBBY_RETURN_CONFIRMED')
        return False

    def inspect_server(self,frame):
        self.last_server_traffic=time.monotonic()
        self.chat_response_owner=None
        queue=self.advert_pending if frame.packet_id==0x1c else self.query_pending if frame.packet_id==9 else self.enterchat_pending if frame.packet_id==0x0a else None
        if self.cfg.continuous and queue and self.last_server_traffic-queue[0][1]>8:
            self.error='RELAY_PUBLICATION_ACK_TIMEOUT' if frame.packet_id==0x1c else 'RELAY_GAME_QUERY_TIMEOUT' if frame.packet_id==9 else 'RELAY_ENTERCHAT_ACK_TIMEOUT'
            self.publication_failed=True;self.advertised=self.visible=self.named_visible=False
            raise RelayError(self.error)
        if frame.packet_id in (0x53,0x54) and len(frame.payload)>=4:
            code=struct.unpack_from('<I',frame.payload)[0]
            if code>0x1000:code='OTHER'
            if frame.packet_id==0x53:self.server_logon_status=code
            else:self.server_proof_status=code;self.proof_time=time.monotonic()
        if frame.packet_id==0x0a:
            if self.enterchat_pending:
                self.chat_response_owner,_=self.enterchat_pending.popleft()
            if self.chat_response_owner is not None and self.chat_response_owner!=(self.session_generation,self.epoch):
                return False
            if self.chat_return_started is not None and self.chat_response_owner!=self.chat_return_context:
                return True  # The original Star request retains ownership of its response.
            if b'\0' not in frame.payload:
                self.identity=False;return True
            unique=frame.payload.split(b'\0',1)[0]
            self.identity=unique.decode('utf-8',errors='replace').casefold()==self.cfg.expected_username.casefold()
            if self.authenticated and self.authenticated_at is None:self.authenticated_at=time.monotonic()
            self.control_event('SERVER_EXPECTED_ENTERCHAT' if self.identity else 'SERVER_OTHER_ENTERCHAT')
        elif frame.packet_id==0x0f and len(frame.payload)>=26:
            event=struct.unpack_from('<I',frame.payload)[0]
            self.chat_events[f'0x{event:02x}' if event<=0x40 else 'other']+=1
            if event==7 and (self.chat_return_started is None or not self.chat_return_join_pending):self.channel=True
            if event==0x12:
                for encoding in ('utf-8','cp1251'):
                    text=frame.payload[24:].decode(encoding,errors='replace')
                    text=re.sub(r'\|c[0-9a-fA-F]{8}|\|r','',text)
                    if re.search(r'Привет,\s*'+re.escape(self.cfg.expected_username)+r'!\s*Добро пожаловать',text,re.I):self.welcome=True
                    if 'warcraft 3 / dota сервер iccup!' in text.casefold():self.realm_welcome=True
            if self.authenticated and self.authenticated_at is None:self.authenticated_at=time.monotonic()
        elif frame.packet_id==0x1c and self.advert_pending:
            owner,_=self.advert_pending.popleft()
            if owner is not None:
                epoch,key,refresh=owner
                if self.current is None or self.current[0]['operation']!='publish' or (epoch,key)!=(self.epoch,self.lobby_key()):return False
                if len(frame.payload)!=4:raise RelayError('RELAY_ADV_RESPONSE')
                code=struct.unpack('<I',frame.payload)[0];self.last_status=code;self.native_acks+=1
                self.advertised=code==0;self.last_ack=time.monotonic()
                self.last_ack_context={'session_generation':self.session_generation,'epoch':epoch,'pid':key[0][0],'generation':key[0][1],'host_counter':key[1],
                                       'refresh_sequence':None if refresh is None else refresh[3]}
                if refresh in self.refresh_inflight:
                    self.last_refresh_ack=self.last_ack
                    self.last_refresh_ack_context={**self.last_ack_context,'status':code}
                    self.finish_refresh(refresh,'accepted' if code==0 else 'rejected')
                if code:
                    self.publication_failed=True;self.error='RELAY_PUBLICATION_REJECTED';self.visible=self.named_visible=False
                    self.publication_failures+=1
                    self.retry_at=self.last_ack+max(self.cfg.publication_min_interval,min(300,30*2**min(4,self.publication_failures-1)))
                else:
                    self.publication_failed=False;self.publication_failures=0;self.retry_at=0
                    if self.error=='RELAY_PUBLICATION_REJECTED':self.error=None
                return False
        elif frame.packet_id==9 and self.query_pending:
            owner,_=self.query_pending.popleft()
            if owner is not None:
                generation,sequence,name,counter,*context=owner
                scope=context[0] if len(context)==3 else 'named'
                epoch,key=context[-2:]
                if self.generation==generation and self.current is not None and epoch==self.epoch and key==self.lobby_key():
                    self.last_list_length=len(frame.payload);self.last_list_count=None if len(frame.payload)<4 else struct.unpack_from('<I',frame.payload)[0]
                    self.last_list_match={'matching_title':False,'matching_title_casefold':False}
                    found,status=listed_game(frame.payload,name,counter,self.cfg.game_port,self.last_list_match)
                    if scope=='named':
                        self.last_named_match=dict(self.last_list_match);self.named_visible=found and self.advertised
                        if self.named_visible:self.last_named_visibility_match=time.monotonic()
                    if scope=='directory':
                        self.last_directory_match=dict(self.last_list_match)
                        self.visible=found and self.advertised
                        self.control_event('DIRECTORY_MATCH' if self.visible else 'DIRECTORY_NO_MATCH')
                        if self.visible:
                            self.last_visibility_match=time.monotonic()
                            self.public_match_key=(self.epoch,self.lobby_key())
                            self.last_positive_directory_match={'records':self.last_list_count,'response_bytes':self.last_list_length,'match':dict(self.last_list_match)}
                    if status not in (None,0,1):self.error='RELAY_LIST_REJECTED'
                return False
        return True

    async def client_to_server(self,reader):
        selector=await reader.readexactly(1)
        if selector!=b'\x01':raise RelayError('RELAY_SELECTOR_REJECTED')
        self.last_client_traffic=time.monotonic()
        self.upstream.write(selector);await asyncio.wait_for(self.upstream.drain(),8)
        decoder=FrameDecoder(65535)
        while data:=await reader.read(8192):
            self.last_client_traffic=time.monotonic()
            for frame in decoder.feed(data):
                self.counts['client'][frame.packet_id]+=1
                if frame.packet_id==0x0a:
                    self.original_enterchat=True
                    if len(frame.payload)<=512 and len(frame.payload.split(b'\0'))==3 and frame.payload.endswith(b'\0'):
                        self.original_enterchat_packet=wire(frame)
                    self.control_event('STAR_ENTERCHAT')
                    if self.cfg.publish and self.requested_identity_matches and self.server_proof_status==0 and not self.netgameport_sent:
                        await self.send(packet(0x45,struct.pack('<H',self.cfg.game_port)));self.netgameport_before_enterchat=True;self.netgameport_sent=True
                if frame.packet_id==0x0b:self.original_channel_list=True
                if frame.packet_id==0x0c and 5<=len(frame.payload)<=260 and frame.payload.endswith(b'\0'):self.original_join_channel=wire(frame)
                if frame.packet_id==0x53:
                    name=frame.payload[32:] if len(frame.payload)>=33 and frame.payload.endswith(b'\0') else b''
                    self.requested_identity_matches=bool(name) and name[:-1].decode('utf-8',errors='replace').casefold()==self.cfg.expected_username.casefold()
                if frame.packet_id==0x0e:
                    command=frame.payload.split(b' ',1)[0].rstrip(b'\0').lower()
                    category=command.decode('ascii') if command in (b'/unhost',b'/host',b'/public',b'/private',b'/where',b'/whereis',b'/stats',b'/join',b'/channel') else 'OTHER'
                    self.original_commands[category]+=1;self.control_event('STAR_COMMAND_'+category)
                raw=wire(frame)
                if frame.packet_id==0x45 and (self.advertised or self.netgameport_sent):raw=packet(0x45,struct.pack('<H',self.cfg.game_port))
                await self.send(raw)
        decoder.finish()

    async def server_to_client(self,reader,writer):
        decoder=FrameDecoder(65535)
        while data:=await reader.read(8192):
            self.last_server_traffic=time.monotonic()
            for frame in decoder.feed(data):
                self.counts['server'][frame.packet_id]+=1
                forward=self.inspect_server(frame)
                if await self.chat_return_response(frame):forward=False
                if forward:writer.write(wire(frame));await asyncio.wait_for(writer.drain(),8)
        decoder.finish()

    @staticmethod
    async def close_writer(writer):
        writer.close()
        try:await asyncio.wait_for(writer.wait_closed(),1)
        except (OSError,asyncio.TimeoutError):
            transport=getattr(writer,'transport',None)
            if transport is not None:transport.abort()

    async def session(self,reader,writer):
        if self.used or self.stop.is_set():
            self.rejected_connections+=1;self.session_event('RELAY_SESSION_REJECTED');await self.close_writer(writer);return
        self.used=True;self.client_sessions+=1;self.session_generation+=1
        if self.cfg.continuous:self.reset_session()
        self.closing=False;self.session_started=time.monotonic();self.session_closed=None
        self.session_event('RELAY_SESSION_ACCEPTED')
        owner_task=asyncio.current_task();self.session_tasks.add(owner_task);self.active_session=owner_task
        tasks=[]
        try:
            remote,self.upstream=await asyncio.wait_for(asyncio.open_connection(self.cfg.upstream,self.cfg.upstream_port),8)
            self.connected=True;self.session_event('RELAY_UPSTREAM_CONNECTED');self.record()
            tasks=[asyncio.create_task(self.client_to_server(reader)),asyncio.create_task(self.server_to_client(remote,writer))]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        except RelayError:self.error=self.error or 'RELAY_SESSION_REJECTED'
        except (OSError,ValueError,asyncio.IncompleteReadError,asyncio.TimeoutError):self.error='RELAY_SESSION_CLOSED'
        finally:
            # Repeated cancellation (EOF racing shutdown/timeout) cannot cut
            # teardown short and release a half-closed session to its successor.
            cleanup=asyncio.create_task(self.close_session(owner_task,writer,tasks))
            while not cleanup.done():
                try:await asyncio.shield(cleanup)
                except asyncio.CancelledError:pass
            cleanup.result()

    async def close_session(self,owner_task,writer,tasks):
        withdraw=self.cfg.publish and self.authenticated and self.current is not None and self.upstream is not None
        self.connected=False;self.closing=True;self.identity=False;self.channel=False;self.advertised=False;self.visible=False;self.named_visible=False;self.public_match_key=None
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.upstream is not None:
            if withdraw:
                try:
                    async with self.lock:
                        self.upstream.write(packet(2));await asyncio.wait_for(self.upstream.drain(),1);self.withdrawals+=1
                except (OSError,asyncio.TimeoutError):pass
            await self.close_writer(self.upstream)
        self.upstream=None;await self.close_writer(writer)
        self.cancel_refreshes();self.current=None;self.epoch+=1
        self.session_tasks.discard(owner_task);self.active_session=None;self.session_closed=time.monotonic()
        # The gate is released last, after wait_closed and all old tasks.
        self.closing=False
        if self.cfg.continuous:self.used=False
        self.session_event('RELAY_SESSION_CLOSED')
        self.record()

    async def intent_tick(self):
        now=time.monotonic()
        if self.chat_return_started is not None and now-self.chat_return_started>8:
            self.error='RELAY_CHAT_RETURN_TIMEOUT'
            raise RelayError('RELAY_CHAT_RETURN_TIMEOUT')
        # Check the complete FIFO before adding requests, including originals.
        # A missing original reply must never shift a late ACK onto a native ad.
        for queue,reason in ((self.advert_pending,'RELAY_PUBLICATION_ACK_TIMEOUT'),(self.query_pending,'RELAY_GAME_QUERY_TIMEOUT'),(self.enterchat_pending,'RELAY_ENTERCHAT_ACK_TIMEOUT')):
            if any(now-sent>8 for _,sent in queue):
                self.publication_failed=True;self.error=reason
                if self.authenticated:await self.withdraw()
                self.current=None
                raise RelayError(reason)
        item=None
        try:item=publication_intent(owned_file(self.cfg.intent))
        except FileNotFoundError:pass
        except (OSError,KeyError,ValueError,TypeError,UnicodeError):self.error='RELAY_NATIVE_INTENT_REJECTED'
        if item is not None:
            obj,raw,name=item;generation=(obj['pid'],obj['generation'])
            if self.cfg.continuous:
                previous=self.native_watermarks.get(generation)
                self.native_watermarks[generation]=max(previous or 0,obj['sequence'])
                # New processes and new Star sessions require observed progress,
                # not an old file whose first read would appear to be fresh.
                if not self.connected or generation in self.retired_generations or previous is None or obj['sequence']<=previous:item=None
            if item is not None:
                if generation!=self.generation:
                    if self.generation is not None:self.retired_generations.add(self.generation)
                    if self.current is not None:await self.withdraw()
                    self.generation=generation;self.sequence=0;self.last_published_sequence=0;self.current=None
                    if not self.cfg.continuous:self.publication_failed=False
                if obj['sequence']>self.sequence:
                    if self.current is not None and self.current[0]['operation']=='publish' and obj['operation']=='publish' and (self.current[0]['host_counter']!=obj['host_counter'] or self.current[1][:8]+self.current[1][12:]!=raw[:8]+raw[12:]):
                        await self.withdraw()
                        if not self.cfg.continuous:self.publication_failed=False
                    self.native_intents_observed+=1
                    if obj['operation']=='publish':self.native_publication_intents_observed+=1
                    self.sequence=obj['sequence'];self.last_native=now;self.current=item
                    if obj['operation']=='unpublish':
                        await self.withdraw();self.current=None
                    elif self.error=='RELAY_NATIVE_HEARTBEAT_EXPIRED':self.error=None
        if self.current is not None and now-self.last_native>10:
            await self.withdraw();self.current=None;self.error='RELAY_NATIVE_HEARTBEAT_EXPIRED'
        if self.cfg.refresh_request is not None:
            try:
                request=refresh_request(owned_file(self.cfg.refresh_request),self.current,now)
                self.refresh_request_error=None
                key=self.refresh_key(request);context=key[:3]
                if key[3]>self.refresh_watermarks.get(context,0):
                    self.refresh_watermarks[context]=key[3];self.refresh_requested+=1
                    self.last_refresh_requested=now;self.last_refresh_request_sequence=key[3];self.last_refresh_request_generation=key[1]
                    if self.refresh_pending is not None:self.finish_refresh(self.refresh_key(self.refresh_pending),'superseded')
                    self.refresh_pending=request
            except FileNotFoundError:pass
            except (OSError,KeyError,ValueError,TypeError,UnicodeError):
                # Malformed/stale IPC is never transport or authentication input.
                self.refresh_request_error='RELAY_REFRESH_REQUEST_REJECTED'
        if self.refresh_pending is not None and now-self.refresh_pending['observed_monotonic_seconds']>10:
            self.finish_refresh(self.refresh_key(self.refresh_pending),'expired');self.refresh_pending=None
        allowed=not self.publication_failed or (self.cfg.continuous and now>=self.retry_at)
        if self.cfg.publish and self.authenticated and self.current is not None and self.chat_return_required:
            await self.return_to_chat(now)
        if self.cfg.publish and self.authenticated and self.current is not None and allowed:
            obj,raw,name=self.current
            refresh=self.refresh_key(self.refresh_pending) if self.refresh_pending is not None else None
            refresh_due=refresh is not None and (self.last_refresh_sent is None or now-self.last_refresh_sent>=self.cfg.refresh_min_interval)
            # Explicit native refresh has its own (>=5s) throttle. Holding a
            # 10s-lived hint behind a 30s maintenance interval would drop it.
            interval=self.cfg.refresh_min_interval if refresh_due else self.cfg.publication_min_interval
            due=not self.last_sent or now-self.last_sent>=interval
            pending=any(owner is not None for owner,_ in self.advert_pending)
            if due and (self.sequence>self.last_published_sequence or refresh_due) and (not self.cfg.continuous or not pending):
                if not self.netgameport_sent:
                    await self.send(packet(0x45,struct.pack('<H',self.cfg.game_port)));self.netgameport_sent=True
                refresh=refresh if refresh_due else None
                if refresh is not None:
                    self.refresh_inflight.add(refresh);self.refresh_pending=None
                await self.send(raw,(self.generation,self.sequence),refresh=refresh)
        if self.cfg.publish and self.authenticated and self.advertised and self.current is not None and now-self.last_query>=2.5:
            obj,raw,name=self.current
            if not any(owner is not None for owner,_ in self.query_pending):
                public=self.queries%2==0
                await self.send(list_request(name,public_directory=public),(self.generation,self.sequence,name,0xf0000000|obj['host_counter'],'directory' if public else 'named'))
                if public:self.directory_queries+=1
                else:self.named_queries+=1
                self.queries+=1;self.last_query=now
        if self.visible and self.last_visibility_match and now-self.last_visibility_match>8:self.visible=False

    async def intents(self):
        while not self.stop.is_set():
            session_generation=self.session_generation
            try:await self.intent_tick()
            except (RelayError,OSError,asyncio.TimeoutError):
                if not self.cfg.continuous:raise
                # A send begun by an old cohort cannot tear down a new cohort.
                if session_generation==self.session_generation:
                    self.error=self.error or 'RELAY_SESSION_CLOSED';self.publication_failed=True
                    if self.active_session is not None:
                        session=self.active_session
                        if not self.closing:session.cancel()
                        await asyncio.gather(session,return_exceptions=True)
                    else:
                        self.connected=self.identity=self.channel=self.advertised=self.visible=self.named_visible=False
                        self.cancel_refreshes();self.current=None
            self.record()
            try:await asyncio.wait_for(self.stop.wait(),0.2)
            except asyncio.TimeoutError:pass

    async def run(self):
        self.record()
        server=await asyncio.start_server(self.session,'127.0.0.1',self.cfg.listen_port,limit=131072)
        self.listening=True;self.record()
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,self.stop.set)
        worker=asyncio.create_task(self.intents());stopper=asyncio.create_task(self.stop.wait())
        failed=False
        try:
            async with server:
                try:
                    done,_=await asyncio.wait((worker,stopper),timeout=None if self.cfg.continuous else self.cfg.duration,return_when=asyncio.FIRST_COMPLETED)
                    if worker in done and (not self.stop.is_set() or worker.cancelled() or worker.exception() is not None):
                        failed=True;self.error=self.error or 'RELAY_INTENT_WORKER_FAILED';self.publication_failed=True
                    self.stop.set();worker.cancel();await asyncio.gather(worker,return_exceptions=True)
                    try:
                        if self.authenticated:await self.withdraw()
                    except (RelayError,OSError,asyncio.TimeoutError):self.error=self.error or 'RELAY_SESSION_CLOSED'
                    finally:self.current=None
                finally:
                    # Python's Server.wait_closed also waits for its active
                    # transports. Close cohorts before leaving the server context.
                    self.stop.set();self.listening=False
                    worker.cancel();stopper.cancel();await asyncio.gather(worker,stopper,return_exceptions=True)
                    sessions=list(self.session_tasks)
                    for session in sessions:session.cancel()
                    await asyncio.gather(*sessions,return_exceptions=True)
        finally:
            self.record()
        if failed:raise RelayError('RELAY_INTENT_WORKER_FAILED')
        return self.record()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--upstream',required=True);p.add_argument('--upstream-port',type=int,default=6112)
    p.add_argument('--profile',required=True,type=Path);p.add_argument('--intent',required=True,type=Path);p.add_argument('--status',required=True,type=Path)
    p.add_argument('--game-port',type=int,default=6113);p.add_argument('--duration',type=int,default=120)
    p.add_argument('--publish',action='store_true');p.add_argument('--offline-test',action='store_true');p.add_argument('--listen-port',type=int,default=6112)
    p.add_argument('--continuous',action='store_true',help='Accept sequential original Star sessions until stopped; supervisor owns restart/backoff')
    p.add_argument('--publication-interval','--publication-min-interval',dest='publication_min_interval',type=float,help='Maintenance publication interval (continuous default 30s, research 2.5s)')
    p.add_argument('--refresh-request',type=Path,help='Optional private native refresh hint file; never carries packets')
    p.add_argument('--refresh-min-interval',type=float,default=5)
    p.add_argument('--realm-idle-timeout',type=float,default=60,help='Maximum server traffic age for realm health')
    a=p.parse_args()
    try:
        ip=ipaddress.ip_address(a.upstream)
        routed=any(line.split()[1]=='00000000' for line in Path('/proc/net/route').read_text().splitlines()[1:])
        if ip.version!=4 or (not ip.is_global and not (a.offline_test and ip.is_loopback and not routed)):
            raise RelayError('RELAY_UPSTREAM_REJECTED')
        if (not a.continuous and not 30<=a.duration<=180) or not all(1024<=port<=65535 for port in (a.upstream_port,a.listen_port,a.game_port)):
            raise RelayError('RELAY_LIMIT_REJECTED')
        paths=[a.status,a.intent]+([] if a.refresh_request is None else [a.refresh_request])
        if len({path.resolve() for path in paths+[a.profile]})!=len(paths)+1:raise RelayError('RELAY_PRIVATE_INPUT_REJECTED')
        for directory in {path.parent for path in paths}:
            info=directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:raise RelayError('RELAY_DIRECTORY_REJECTED')
        require_loopback_profile(a.profile,a.listen_port)
        settings=Settings(str(ip),a.upstream_port,profile_username(a.profile),a.intent,a.status,a.game_port,a.duration,a.publish,a.listen_port,
                          continuous=a.continuous,publication_min_interval=a.publication_min_interval,refresh_request=a.refresh_request,
                          refresh_min_interval=a.refresh_min_interval,realm_idle_timeout=a.realm_idle_timeout)
        asyncio.run(StarRelay(settings).run());return 0
    except Exception:
        print(json.dumps({'result':'FAIL','reason':'STAR_RELAY_REJECTED'}));return 1

if __name__=='__main__':raise SystemExit(main())
