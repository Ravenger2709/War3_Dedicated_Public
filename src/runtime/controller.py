"""Supervise native GHost and one original-Star/relay cohort independently."""
from dataclasses import dataclass
from datetime import datetime,timezone
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from src.bridge.star_relay import profile_username,require_loopback_profile,publication_intent
from src.connector import require_supported_connector
from src.supervisor.events import classify,sanitize
from src.supervisor.service import Supervisor
from .files import atomic_json,atomic_text,private_bytes,private_directory,private_json
from .native import listener_owned,native_state,prepare_native,refresh_event
from .operator_logs import OperatorLogMixin


def fresh(obj,now,ttl,started=0):
    stamp=obj.get('observed_monotonic_seconds')
    return type(stamp) in (int,float) and math.isfinite(stamp) and started<=stamp<=now and now-stamp<=ttl


@dataclass
class Child:
    process:object
    generation:int
    started:float
    reader:object=None


class RuntimeBackend:
    """Production commands. Tests replace this object, never a CLI auth flag."""
    def __init__(self,config):
        self.config=config;self.settings=config['iccup'];self.account_fd=None;self.effective=None

    def prepare(self):
        cfg=self.settings
        for path in (Path(self.config['runtime_dir']),Path(cfg['bridge_dir']),Path(cfg['prefix']),Path(cfg['session'])):private_directory(path)
        profile=Path(cfg['profile'])/'launcher.ini'
        private_bytes(profile,16384);require_loopback_profile(profile,cfg['relay_port'])
        self.account_fd=os.open(profile,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:fcntl.flock(self.account_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('ACCOUNT_PROFILE_ALREADY_OWNED') from None
        self.effective=prepare_native(self.config,profile_username(profile))
        command=self.config['hostbot']['command']
        if command!=['/opt/war3/bin/ghost++',str(self.effective)]:raise ValueError('NATIVE_COMMAND_REJECTED')

    def command(self,name,generation):
        cfg=self.settings;bridge=Path(cfg['bridge_dir'])
        if name=='native':return self.config['hostbot']['command']
        if name=='relay':
            return [sys.executable,'-m','src.bridge.star_relay','--continuous','--publish',
                    '--upstream',cfg['upstream'],'--listen-port',str(cfg['relay_port']),'--game-port','6113',
                    '--profile',str(Path(cfg['profile'])/'launcher.ini'),'--intent',str(bridge/'native-intent.json'),
                    '--status',str(bridge/'relay-status.json'),'--refresh-request',str(bridge/'refresh-request.json'),
                    '--publication-interval',str(cfg['publication_interval'])]
        if name=='star':
            worker={k:cfg[k] for k in ('vendor','profile','redist','game_metadata','prefix','session','manifest','quit_helper','relay_port')}
            worker.update(version=1,status=str(Path(self.config['runtime_dir']).parent/'star-status.json'),startup_timeout=math.ceil(cfg['auth_timeout']),generation=generation)
            target=Path(self.config['runtime_dir']).parent/'star-worker.json';atomic_json(target,worker)
            return [sys.executable,'-m','src.runtime.star','--config',str(target)]
        raise ValueError('COMPONENT_REJECTED')

    def cleanup_prefix(self):
        from .processes import stop_owned_prefix
        prefix=Path(self.settings['prefix'])
        if not any(prefix.iterdir()):return {'version':1,'cleaned':True,'reason':'EMPTY_PREFIX'}
        return stop_owned_prefix(prefix,timeout=12)

    def listener(self,pid):return listener_owned(pid,6113)

    def close(self):
        if self.account_fd is not None:os.close(self.account_fd);self.account_fd=None


class ICCupSupervisor(OperatorLogMixin,Supervisor):
    def __init__(self,config,output=None,backend=None):
        require_supported_connector(config['mode'],config.get('iccup',{}).get('adapter'))
        if 'iccup' not in config or 'gameplay' not in config:raise ValueError('PRODUCTION_CONFIG_REQUIRED')
        private_directory(config['runtime_dir'])
        self.children={};self.child_generations={name:0 for name in ('native','star','relay')}
        self.component_attempts={'native':0,'realm':0};self.retry_times={'native':0.,'realm':0.}
        self.component_exits={};self.native_observation={};self.native_observed_at=0.
        self.relay_observation={};self.star_observation={};self.native_initialized=False
        self.realm_started=None;self.realm_connected_at=None;self.realm_lost_at=None;self.ever_connected=False
        self.last_tick=time.monotonic();self.created_at=self.last_tick;self.star_generation=0
        self.backend=backend or RuntimeBackend(config);self.prepared=False;self.native_listener=False
        self.circuit_open=set();self.stopping=False;self.last_feedback=0.;self.last_refresh_request=0
        self.permanent_errors={};self.engine_observed=False
        super().__init__(config,output)
        try:
            marker=self.runtime.parent/'engine-blocked.json'
            if marker.exists() or marker.is_symlink():
                if private_json(marker)!={'version':1,'blocked':True}:raise ValueError('ENGINE_BLOCK_MARKER_REJECTED')
                self.engine_observed=True;self.want_running=False;self.permanent_errors['engine']='GAME_ENGINE_FORBIDDEN'
            self.backend.prepare();self.prepared=True
        except Exception:
            self.backend.close();self.lockfile.close();raise

    @property
    def live(self):return self.running and time.monotonic()-self.last_tick<10

    def alive(self,name):
        child=self.children.get(name)
        return child is not None and child.process.poll() is None

    def snapshot(self):
        with self.guard:
            now=time.monotonic();realm=self.relay_observation;star=self.star_observation
            star_alive=self.alive('star') and star.get('launcher_alive') is True and star.get('phase')=='RUNNING' and fresh(star,now,self.config['iccup']['metadata_ttl'],self.children['star'].started)
            relay_alive=self.alive('relay') and bool(realm) and fresh(realm,now,self.config['iccup']['metadata_ttl'],self.children['relay'].started)
            connected=star_alive and relay_alive and realm.get('connected') is True
            authenticated=connected and realm.get('authenticated') is True and realm.get('expected_identity_confirmed') is True and realm.get('realm_connection_healthy') is True
            native_alive=self.alive('native')
            healthy_native=native_alive and self.native_initialized and now-self.native_observed_at<=self.config['iccup']['native_state_ttl']
            accepted=bool(native_alive and authenticated and realm.get('advertisement_accepted') is True and realm.get('native_intent_seen') is True and realm.get('native_heartbeat_fresh') is True and realm.get('native_pid')==self.children['native'].process.pid)
            accepted=accepted and realm.get('native_host_counter')==self.native_observation.get('host_counter')
            phase=self.native_observation.get('phase','IDLE')
            accepted=accepted and phase in ('LOBBY','COUNTDOWN')
            ready=bool(self.want_running and not self.stopping and authenticated and healthy_native and (phase=='IN_GAME' or self.native_listener and (accepted or not self.config['gameplay']['autohost_enabled'])))
            components={name:{'alive':self.alive(name),'pid':child.process.pid if self.alive(name) else None,'generation':child.generation,'last_exit':self.component_exits.get(name)} for name,child in self.children.items()}
            for name in ('native','relay','star'):components.setdefault(name,{'alive':False,'pid':None,'generation':self.child_generations[name],'last_exit':self.component_exits.get(name)})
            return {'state':self.state,'reason':self.reason,'mode':'iccup','ready':ready,
                    'pid':components['native']['pid'],'child_alive':native_alive,'iccup_authenticated':authenticated,
                    'realm_connected':connected,'relay_healthy':relay_alive,'star_process_alive':star_alive,
                    'native_healthy':healthy_native,'player_listener_available':self.native_listener,
                    'lobby_publication_accepted':accepted,'components':components,
                    'uptime_seconds':round(now-self.created_at,3),'component_restarts':dict(self.component_attempts),
                    'restarts':sum(self.component_attempts.values()),'circuit_open':sorted(self.circuit_open),
                    'permanent_errors':dict(self.permanent_errors),'retry_in_seconds':{k:round(max(0,t-now),3) for k,t in self.retry_times.items()},
                    'game':dict(self.native_observation),'last_exit_code':self.last_exit_code,'last_log_time':self.last_log_time,
                    'publication':{key:realm.get(key) for key in ('advertisements_sent','publication_acks','server_publication_status','game_queries_sent','last_positive_directory_match','exact_lookup_visible','visibility_match_age_seconds','last_ack_monotonic_seconds','refresh_requested_count','refresh_sent_count','refresh_successful_count','refresh_failed_count','last_refresh_ack_monotonic_seconds','last_refresh_result')},
                    'game_engine_observed':self.engine_observed or star.get('game_engine_observed',False),
                    'history':list(self.history[-20:])}

    def _spawn(self,name):
        self.child_generations[name]+=1;generation=self.child_generations[name]
        command=self.backend.command(name,generation)
        wrapper=str(Path(__file__).resolve().parents[1]/'supervisor/child_exec.py')
        started=time.monotonic()
        process=subprocess.Popen([sys.executable,wrapper,str(os.getpid()),*command],cwd=self.config['hostbot']['cwd'],
                                 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                                 encoding='utf-8',errors='replace',start_new_session=True,bufsize=1)
        child=Child(process,generation,started)
        with self.guard:self.children[name]=child
        def read():
            try:
                while line:=process.stdout.readline(65537):
                    if name=='native':
                        self.last_log_time=datetime.now(timezone.utc).isoformat();self.log('hostbot',message=sanitize(line))
                    else:
                        # Workers emit fixed diagnostics only; original vendor
                        # stdout/trace must never be inherited by this pipe.
                        if len(line)<4096 and line.startswith('{'):
                            try:
                                obj=json.loads(line)
                                event=obj.get('event')
                                if isinstance(event,str) and event.isascii() and event.replace('_','').isalnum():self.log('component_event',component=name,worker_event=event[:64])
                            except (ValueError,AttributeError):pass
                    while self.running and self.children.get(name) is child:
                        try:self.events.put(('component_line',name,generation,line),timeout=.1);break
                        except queue.Full:pass
            finally:process.stdout.close()
        child.reader=threading.Thread(target=read,name=name+'-stdout',daemon=True);child.reader.start()
        self.log('component_started',component=name,pid=process.pid,generation=generation)
        return child

    def _stop_component(self,name):
        with self.guard:child=self.children.pop(name,None)
        if child is None:return
        process=child.process
        # Also signal the owned process group when its leader already exited.
        # Xvfb can outlive a killed worker until this cleanup runs.
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        timeout=max(35,self.config['hostbot']['stop_timeout']) if name=='star' else min(8,self.config['hostbot']['stop_timeout']) if name=='relay' else self.config['hostbot']['stop_timeout']
        try:process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if name=='native':
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
            if process.poll() is None:
                self.log('forced_stop',component=name,pid=process.pid)
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
        self.component_exits[name]=process.returncode
        if name=='native':self.last_exit_code=process.returncode
        if child.reader:child.reader.join(timeout=2)
        self.log('component_stopped',component=name,exit_code=process.returncode)

    def _stop_realm(self):
        self.relay_observation={};self.star_observation={};self._feedback(force=True)
        self._stop_component('relay');self._stop_component('star')
        try:clean=self.backend.cleanup_prefix().get('cleaned') is True
        except Exception:clean=False
        if not clean:
            self.log('prefix_cleanup_failed');self.permanent_errors['realm']='PREFIX_CLEANUP_FAILED';self.circuit_open.add('realm')
        self.realm_started=None;self.realm_connected_at=None;self.realm_lost_at=None
        return clean

    def _schedule(self,kind,reason):
        attempts=self.component_attempts[kind];recovery=self.config['recovery']
        if attempts>=recovery['max_restarts'] or not self.config['gameplay']['reconnect_enabled'] and kind=='realm':
            self.circuit_open.add(kind);self.retry_times[kind]=time.monotonic()+self.config['iccup']['circuit_cooldown'];self.transition('ERROR',reason+':restart_budget_exhausted');return
        delay=min(recovery['backoff_initial']*2**min(attempts,20),recovery['backoff_max'])
        self.component_attempts[kind]+=1;self.retry_times[kind]=time.monotonic()+delay
        self.transition('RECONNECTING',reason);self.log('retry_scheduled',component=kind,attempt=attempts+1,delay_seconds=delay)

    def _failure(self,kind,reason):
        if self.stopping:return
        clean=self._stop_realm()
        if kind=='native':
            self._stop_component('native');self.native_initialized=False;self.native_observation={};self.native_observed_at=0;self.native_listener=False
        if clean:self._schedule(kind,reason)
        else:self.transition('ERROR','PREFIX_CLEANUP_FAILED')

    def _feedback(self,force=False):
        now=time.monotonic()
        if not force and now-self.last_feedback<.5:return
        self.last_feedback=now
        s=self.snapshot();r=self.relay_observation
        ack=r.get('last_ack_monotonic_seconds')
        ack_ms=int(ack*1000) if type(ack) in (int,float) and math.isfinite(ack) and 0<=ack<=now else 0
        def count(key):
            value=r.get(key,0)
            return min(value,2**32-1) if type(value) is int and value>=0 else 0
        values={'version':1,'observed_monotonic_ms':int(now*1000),
                'connected':int(s['realm_connected']),'authenticated':int(s['iccup_authenticated']),
                'publication_accepted':int(s['lobby_publication_accepted']),'relay_healthy':int(s['relay_healthy']),
                'last_ack_monotonic_ms':ack_ms,'publication_refresh_count':count('refresh_sent_count'),
                'publication_refresh_successes':count('refresh_successful_count'),'publication_refresh_failures':count('refresh_failed_count')}
        atomic_text(Path(self.config['iccup']['bridge_dir'])/'native-feedback.cfg',''.join(k+' = '+str(v)+'\n' for k,v in values.items()))

    def _metadata(self):
        cfg=self.config['iccup'];bridge=Path(cfg['bridge_dir'])
        relay_child=self.children.get('relay');star_child=self.children.get('star')
        relay_observation={};star_observation={}
        if self.alive('relay'):
            try:
                obj=private_json(bridge/'relay-status.json')
                # An atomic worker replacement can happen while we read. Its
                # legitimate timestamp must be compared with time AFTER I/O.
                now=time.monotonic()
                lifetime=obj.get('relay_started_monotonic_seconds')
                if obj.get('relay_pid')==relay_child.process.pid and type(lifetime) in (int,float) and relay_child.started<=lifetime<=now and fresh(obj,now,cfg['metadata_ttl'],relay_child.started):relay_observation=obj
            except (OSError,ValueError):pass
        if star_child is not None:
            try:
                obj=private_json(Path(self.config['runtime_dir']).parent/'star-status.json')
                now=time.monotonic()
                # A terminal safety report must outlive the readiness TTL:
                # shutdown can take longer than three seconds. It still has
                # to belong to this exact worker and process lifetime.
                if obj.get('worker_pid')==star_child.process.pid and obj.get('generation')==star_child.generation:
                    safety=obj.get('game_engine_observed') is True and fresh(obj,now,now-star_child.started,star_child.started)
                    if safety or fresh(obj,now,cfg['metadata_ttl'],star_child.started):star_observation=obj
            except (OSError,ValueError):pass
        native=self.children.get('native');native_listener=self.backend.listener(native.process.pid if self.alive('native') else None)
        with self.guard:
            self.relay_observation=relay_observation;self.star_observation=star_observation;self.native_listener=native_listener

    def _native_line(self,generation,line):
        child=self.children.get('native')
        if child is None or generation!=child.generation:return
        self.operator_native_line(line)
        try:
            event=native_state(line)
            if event is not None:
                self.native_observation=event;self.native_observed_at=time.monotonic()
                self.operator_native_state(event);return
            refresh=refresh_event(line)
            if refresh is not None and refresh['sequence']>self.last_refresh_request:
                obj,raw,name=publication_intent(private_bytes(Path(self.config['iccup']['bridge_dir'])/'native-intent.json',4096))
                if obj['pid']==child.process.pid and obj['operation']=='publish' and obj['host_counter']==refresh['host_counter']:
                    request={k:obj[k] for k in ('version','pid','generation','host_counter')}
                    request.update(sequence=refresh['sequence'],observed_monotonic_seconds=time.monotonic())
                    atomic_json(Path(self.config['iccup']['bridge_dir'])/'refresh-request.json',request);self.last_refresh_request=refresh['sequence']
                return
        except (ValueError,KeyError,TypeError,OSError):self.log('native_metadata_rejected');return
        lifecycle=classify(line)
        if lifecycle=='fatal':self._failure('native','native_initialization_or_listener_failed')
        elif lifecycle=='initialized':self.native_initialized=True

    def _state(self):
        s=self.snapshot();phase=s['game'].get('phase','IDLE')
        if self.circuit_open:state,reason='ERROR','component_restart_budget_exhausted'
        elif not self.alive('native'):state,reason='RECONNECTING','waiting_native_restart'
        elif not self.native_initialized:state,reason='STARTING','waiting_native_initialization'
        elif not self.alive('relay') or not self.alive('star'):state,reason='RECONNECTING','waiting_realm_restart'
        elif not s['star_process_alive']:state,reason='STARTING','starting_original_star'
        elif not s['iccup_authenticated']:state,reason='AUTHENTICATING','waiting_real_star_authentication'
        elif not s['native_healthy']:state,reason='CONNECTED','waiting_native_lifecycle'
        elif phase=='IN_GAME':state,reason='IN_GAME','native_game_active'
        elif phase=='COUNTDOWN' and s['ready']:state,reason='COUNTDOWN','native_countdown_active'
        elif s['ready']:state,reason='LOBBY' if phase=='LOBBY' else 'CONNECTED','native_lobby_published' if phase=='LOBBY' else 'realm_ready_native_idle'
        else:state,reason='PUBLISHING','waiting_current_lobby_ack'
        if state!=self.state or reason!=self.reason:self.transition(state,reason)

    def _command(self,command):
        if command in ('stop','restart','quit'):
            self.want_running=False;self.stopping=True;self.transition('STOPPING','operator_'+command);self._feedback(force=True)
            self._stop_component('native');self.native_initialized=False;self.native_observation={};self.native_listener=False
            time.sleep(.3);self._stop_realm();self.transition('STOPPED','operator_'+command);self.stopping=False
        if command in ('start','restart'):
            if self.engine_observed:return
            try:clean=self.backend.cleanup_prefix().get('cleaned') is True
            except Exception:clean=False
            if not clean:self.transition('ERROR','PREFIX_CLEANUP_FAILED');return
            self.permanent_errors.clear();self.want_running=True;self.circuit_open.clear();self.component_attempts={'native':0,'realm':0};self.retry_times={'native':0.,'realm':0.}
        if command=='quit':self.running=False

    def run(self):
        self.transition('ERROR' if self.engine_observed else 'STARTING','GAME_ENGINE_FORBIDDEN' if self.engine_observed else 'starting_production_components')
        try:
            while self.running:
                self.last_tick=time.monotonic();now=self.last_tick
                if self.want_running:
                    for kind in list(self.circuit_open):
                        if kind not in self.permanent_errors and now>=self.retry_times[kind] and self.config['gameplay']['reconnect_enabled']:
                            self.circuit_open.remove(kind);self.component_attempts[kind]=max(0,self.config['recovery']['max_restarts']-1);self.log('circuit_half_open',component=kind)
                    if not self.alive('native') and 'native' not in self.children and 'native' not in self.circuit_open and now>=self.retry_times['native']:
                        try:self._spawn('native');self.native_initialized=False;self.native_observed_at=0;self.last_refresh_request=0
                        except Exception:self._failure('native','native_launch_failed')
                    if self.alive('native') and self.native_initialized and 'relay' not in self.children and 'star' not in self.children and 'realm' not in self.circuit_open and now>=self.retry_times['realm']:
                        try:
                            self._spawn('relay');self._spawn('star');self.realm_started=time.monotonic();self.realm_connected_at=None
                        except Exception:self._failure('realm','realm_component_launch_failed')
                try:
                    event=self.events.get(timeout=.1)
                    if event[0]=='command':self._command(event[1])
                    elif event[0]=='component_line' and event[1]=='native':self._native_line(event[2],event[3])
                except queue.Empty:pass
                if not self.want_running:continue
                self._metadata();self._feedback()
                if self.star_observation.get('game_engine_observed') is True:
                    atomic_json(self.runtime.parent/'engine-blocked.json',{'version':1,'blocked':True})
                    self.engine_observed=True;self.permanent_errors['engine']='GAME_ENGINE_FORBIDDEN';self.want_running=False;self.transition('ERROR','GAME_ENGINE_FORBIDDEN');break
                if any(type(self.relay_observation.get(key)) is int and self.relay_observation[key]!=0 for key in ('server_logon_status','server_proof_status')):
                    self.permanent_errors['realm']='AUTHENTICATION_REJECTED';self._stop_realm();self.circuit_open.add('realm');self.transition('ERROR','AUTHENTICATION_REJECTED');self._save();continue
                dead=next((name for name,child in self.children.items() if child.process.poll() is not None),None)
                if dead:
                    self._failure('native' if dead=='native' else 'realm',dead+'_exited');continue
                now=time.monotonic();status=self.snapshot();cfg=self.config['iccup']
                native=self.children.get('native')
                if native and not self.native_initialized and now-native.started>self.config['hostbot']['startup_timeout']:
                    self._failure('native','native_startup_timeout');continue
                if self.native_initialized and native and now-(self.native_observed_at or native.started)>cfg['native_state_ttl']:
                    self._failure('native','native_heartbeat_expired');continue
                if self.realm_started is not None:
                    if status['iccup_authenticated']:
                        if self.realm_connected_at is None:self.realm_connected_at=now
                        self.realm_lost_at=None;self.ever_connected=True
                        if now-self.realm_connected_at>self.config['recovery']['stable_seconds']:self.component_attempts['realm']=0
                    elif self.realm_connected_at is not None:
                        if self.realm_lost_at is None:self.realm_lost_at=now
                        if now-self.realm_lost_at>cfg['disconnect_grace']:self._failure('realm','realm_session_lost');continue
                    elif now-self.realm_started>cfg['auth_timeout']:self._failure('realm','authentication_timeout');continue
                if native and now-native.started>self.config['recovery']['stable_seconds']:self.component_attempts['native']=0
                self._state();self._save()
        except Exception:
            self.permanent_errors['supervisor']='RUNTIME_CONTROL_FAILED'
            self.log('runtime_control_failed');self.transition('ERROR','RUNTIME_CONTROL_FAILED')
        finally:
            self.want_running=False;self.stopping=True;self.transition('STOPPING','supervisor_shutdown')
            self._stop_component('native');self.native_listener=False;self.native_initialized=False;self.native_observation={}
            time.sleep(.3);self._stop_realm();self.backend.close();self.transition('STOPPED','supervisor_shutdown')
            self.running=False;self.lockfile.close()


class OfflineProductionSupervisor(OperatorLogMixin,Supervisor):
    """Same native gameplay configuration without reading account files."""
    def __init__(self,config,output=None):
        import copy
        from .config import iccup_config
        cfg=copy.deepcopy(config);cfg.setdefault('iccup',iccup_config({}))
        self.native_observation={}
        private_directory(cfg['runtime_dir'])
        super().__init__(cfg,output)
        try:
            effective=prepare_native(cfg,'OfflineHost',publish=False)
            self.config['hostbot']['command']=['/opt/war3/bin/ghost++',str(effective)]
        except Exception:self.lockfile.close();raise

    def snapshot(self):
        out=super().snapshot();out['game']=dict(self.native_observation)
        if self.native_observation.get('phase')=='COUNTDOWN':out['ready']=out['child_alive']
        return out

    def _line(self,generation,line):
        if generation!=self.generation:return
        self.operator_native_line(line)
        try:event=native_state(line)
        except ValueError:event=None
        if event is not None:
            self.native_observation=event
            self.operator_native_state(event)
            state={'IDLE':'READY','LOBBY':'LOBBY','COUNTDOWN':'COUNTDOWN','IN_GAME':'IN_GAME'}[event['phase']]
            if state!=self.state:self.transition(state,'native_offline_lifecycle')
            return
        super()._line(generation,line)

    def _terminate(self):
        super()._terminate()
        self.operator_native_reset()
