"""Supervise native GHost and one original-Star/relay cohort independently."""
from dataclasses import dataclass
from datetime import datetime,timezone
import fcntl
import io
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
from .native import listener_owned,native_state,native_catalog_lobby_matches,prepare_native,refresh_event
from .preload_inputs import PinnedCatalog
from .operator_logs import OperatorLogMixin

_NATIVE_BINARY='/opt/war3/bin/ghost++'


def fresh(obj,now,ttl,started=0):
    stamp=obj.get('observed_monotonic_seconds')
    return type(stamp) in (int,float) and math.isfinite(stamp) and started<=stamp<=now and now-stamp<=ttl


@dataclass
class Child:
    process:object
    generation:int
    started:float
    reader:object=None
    preload:object=None
    session_owner:object=None
    session_reader:object=None
    session_executable:object=None
    reader_token:object=None


class RuntimeBackend:
    """Production commands. Tests replace this object, never a CLI auth flag."""
    def __init__(self,config, *, catalog_startup=None):
        # Explicit catalog_startup remains available to offline integration tests.
        if catalog_startup is not None:
            if (type(catalog_startup) is not dict or set(catalog_startup) !=
                    {'cycle', 'default_map_bytes', 'ipc_directory', 'journal_directory'}
                    or type(catalog_startup['cycle']) is not list):
                raise ValueError('NATIVE_CATALOG_STARTUP_REJECTED')
            catalog_startup = {**catalog_startup, 'cycle': list(catalog_startup['cycle'])}
        self.config=config;self.settings=config['iccup'];self.account_fd=None;self.effective=None
        self._catalog_required=(config.get('_native_catalog_cycle') is True or
                                len(config.get('gameplay',{}).get('map_cycle',['manual']))>1)
        self._auto_catalog=self._catalog_required and catalog_startup is None
        self.catalog_startup=catalog_startup;self._catalog_command=None;self._username=None
        self._catalog_last_generation=0

    def prepare(self):
        cfg=self.settings
        if self._auto_catalog and not self.config['gameplay']['autohost_enabled']:
            raise ValueError('MAP_CYCLE_AUTOHOST_REQUIRED')
        for path in (Path(self.config['runtime_dir']),Path(cfg['bridge_dir']),Path(cfg['prefix']),Path(cfg['session'])):private_directory(path)
        if self._auto_catalog:
            # Ordinary invalid map/preset edits must fail before a launch claim
            # exists. Revalidate and seal again for the actual launch so this
            # early check never substitutes for the final input snapshot.
            from .preload_catalog import prepare_preload_cycle
            with prepare_preload_cycle(self.config['gameplay']['map_cycle'],cfg['native_config'],
                                       self.config['gameplay'],default_map_bytes=None):
                pass
        profile=Path(cfg['profile'])/'launcher.ini'
        private_bytes(profile,16384);require_loopback_profile(profile,cfg['relay_port'])
        self.account_fd=os.open(profile,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:fcntl.flock(self.account_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('ACCOUNT_PROFILE_ALREADY_OWNED') from None
        self._username=profile_username(profile)
        if self._auto_catalog:
            self.effective=Path(self.config['runtime_dir']).parent/'native'/'ghost.cfg'
        else:
            self.effective=prepare_native(self.config,self._username)
        command=self.config['hostbot']['command']
        if command!=[_NATIVE_BINARY,str(self.effective)]:raise ValueError('NATIVE_COMMAND_REJECTED')

    def configure_cycle_launch(self, control):
        """Allocate a fresh journal-bound IPC directory after the launch claim."""
        if self.catalog_startup is None:
            if not self._auto_catalog:return
            self.catalog_startup={'cycle':list(self.config['gameplay']['map_cycle']),
                                  'default_map_bytes':None,'ipc_directory':'','journal_directory':''}
        if self._catalog_command is not None and not self._catalog_command[2]._closed:
            raise ValueError('NATIVE_CATALOG_STARTUP_REJECTED')
        ipc = control.provision_launch_ipc()
        self.catalog_startup = {**self.catalog_startup,
            'ipc_directory':str(ipc), 'journal_directory':str(control.lease.journal)}

    def native_preload(self,generation):
        if self.catalog_startup is None:
            if self._catalog_required:raise ValueError('NATIVE_CATALOG_STARTUP_REQUIRED')
            return None
        if (self.effective is None or self._username is None or type(generation) is not int
                or not self._catalog_last_generation < generation < 2**63
                or (self._catalog_command is not None and not self._catalog_command[2]._closed)):
            raise ValueError('NATIVE_CATALOG_STARTUP_REJECTED')
        self._catalog_command=None
        self._catalog_last_generation=generation
        from .catalog_startup import prepare_catalog_startup
        spec=self.catalog_startup
        bundle,overrides=prepare_catalog_startup(spec['cycle'],self.settings['native_config'],self.config['gameplay'],
            default_map_bytes=spec['default_map_bytes'],ipc_directory=spec['ipc_directory'],
            journal_directory=spec['journal_directory'])
        try:
            if overrides.get('bot_preload_manifest_fd') != str(bundle.manifest_fd) or overrides.get('bot_preload_manifest_sha256') != bundle.manifest_sha256:
                raise ValueError('NATIVE_CATALOG_BUNDLE_MISMATCH')
            fds=bundle.pass_fds()
            if bundle.manifest_fd not in fds:raise ValueError('NATIVE_CATALOG_BUNDLE_MISMATCH')
            if self._auto_catalog:
                effective=prepare_native(self.config,self._username,catalog_overrides=overrides,catalog_generation=generation,
                                         catalog_default=bundle.default_entry)
            else:
                effective=prepare_native(self.config,self._username,catalog_overrides=overrides,catalog_generation=generation)
            self._catalog_command=(generation,[_NATIVE_BINARY,str(effective)],bundle)
            return bundle
        except BaseException:
            bundle.close()
            raise

    def command(self,name,generation):
        cfg=self.settings;bridge=Path(cfg['bridge_dir'])
        if name=='native':
            if self.catalog_startup is None:
                if self._catalog_required:raise ValueError('NATIVE_CATALOG_STARTUP_REQUIRED')
                return self.config['hostbot']['command']
            current=self._catalog_command
            if current is None or current[0]!=generation:raise ValueError('NATIVE_CATALOG_COMMAND_REJECTED')
            current[2].pass_fds()
            return list(current[1])
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
    def __init__(self,config,output=None,backend=None,*,cycle_session=None):
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
        # Preserve legacy map_preset-only configs. Explicit cycles, including
        # one preset, use the leased catalog path and its configured match limit.
        cycle=config['gameplay'].get('map_cycle',['manual'])
        if cycle_session is None and (config.get('_native_catalog_cycle') is True or len(cycle)>1):
            cycle_session={'root':str(Path(config['runtime_dir']).parent/'cycle'),'cycle':list(cycle)}
        if cycle_session is not None:
            if (type(cycle_session) is not dict or set(cycle_session)!={'root','cycle'}
                or not isinstance(cycle_session['root'],str) or not Path(cycle_session['root']).is_absolute()
                or type(cycle_session['cycle']) is not list):
                raise ValueError('CYCLE_SESSION_REJECTED')
            cycle_session={'root':cycle_session['root'],'cycle':list(cycle_session['cycle'])}
        self.cycle_session=cycle_session;self.cycle_control=None
        self.cycle_status={'state':'initializing','recovery':'unavailable'} if cycle_session is not None else None
        self.cycle_failed=False;self.cycle_next_scan=0.;self.cycle_observation=None;self.cycle_session_read=None
        self.cycle_last_transition=None;self.cycle_monitor_signature=None;self.cycle_telemetry_highwater=None
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

    def _child_alive(self,child):
        owner=getattr(child,'session_owner',None)
        if owner is None:return child.process.poll() is None
        from .process_ownership import ProcessOwnershipError
        try:return owner.is_running()
        except ProcessOwnershipError:
            # An ambiguous pidfd observation must neither reap the child nor
            # trigger a restart. Preserve the handle and block the cycle.
            if not getattr(self,'cycle_failed',False):
                self._cycle_block('CYCLE_CHILD_OBSERVATION_UNAVAILABLE')
            return True

    def alive(self,name):
        child=self.children.get(name)
        return child is not None and self._child_alive(child)

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
            cycle=None
            if self.cycle_status is not None:
                from .cycle_monitor import public
                from .cycle_readiness import cycle_allows_ready
                cycle=public(self.cycle_status,now,self.config['iccup']['native_state_ttl'])
                child=self.children.get('native');session=self.cycle_session_read
                reader=getattr(child,'session_reader',None)
                bound=(session is not None and session[0] is child and reader is not None
                       and reader.bound and self.cycle_control is not None
                       and self.cycle_control.catalog is not None
                       and reader.child is child.session_owner
                       and reader.lease is self.cycle_control.lease)
                record=reader._session_record if bound else None
                generation=record.get('session_generation') if isinstance(record,dict) else None
                ready=bool(ready and cycle_allows_ready(cycle,self.native_observation,
                    generation=generation,failed=self.cycle_failed,
                    listener=self.native_listener,published=accepted))
            snapshot={'state':self.state,'reason':self.reason,'mode':'iccup','ready':ready,
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
            if self.cycle_status is not None:
                snapshot['cycle']=cycle
            return snapshot

    def _spawn(self,name):
        self.child_generations[name]+=1;generation=self.child_generations[name]
        preload=None
        session_reader = name=='native' and getattr(self,'cycle_session',None) is not None
        try:
            if session_reader:
                if self.cycle_control is None or self.cycle_failed:
                    raise ValueError('CYCLE_LAUNCH_OWNERSHIP_UNAVAILABLE')
                self.cycle_control.claim_native_launch()
                configure=getattr(self.backend,'configure_cycle_launch',None)
                if configure is not None:configure(self.cycle_control)
                spec=getattr(self.backend,'catalog_startup',None)
                if spec is not None:self.cycle_control.pin_launch_ipc(spec['ipc_directory'])
            # Pin this generation's sealed map descriptors before creating its command.
            factory=getattr(self.backend,'native_preload',None) if name=='native' else None
            if factory is not None:
                preload=factory(generation)
                if preload is not None and not isinstance(preload,PinnedCatalog):
                    raise ValueError('NATIVE_PRELOAD_REJECTED')
            descriptors=preload.pass_fds() if preload is not None else ()
            command=self.backend.command(name,generation)
            wrapper=str(Path(__file__).resolve().parents[1]/'supervisor/child_exec.py')
            started=time.monotonic()
            options={'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,'stderr':subprocess.STDOUT,
                     'start_new_session':True,'close_fds':True,'pass_fds':descriptors}
            if session_reader:options.update(text=False,bufsize=0)
            else:options.update(text=True,encoding='utf-8',errors='replace',bufsize=1)
            process=subprocess.Popen([sys.executable,wrapper,str(os.getpid()),*command],cwd=self.config['hostbot']['cwd'],**options)
        except BaseException:
            if isinstance(preload,PinnedCatalog):preload.close()
            raise
        child=Child(process,generation,started,preload=preload)
        if session_reader:
            child.reader_token=object()
            child.session_executable=os.path.realpath(command[0])
            try:
                from .process_ownership import OwnedChild
                child.session_owner=OwnedChild(process)
            except Exception:
                self._cycle_block('CYCLE_CHILD_OWNERSHIP_UNAVAILABLE')
        with self.guard:self.children[name]=child
        def read():
            stream=io.BufferedReader(process.stdout) if session_reader else process.stdout
            try:
                while raw:=stream.readline(65537):
                    read_at=time.monotonic()
                    line=raw.decode('utf-8','replace') if session_reader else raw
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
                        try:self.events.put(('component_line',name,generation,line,child,child.reader_token,raw if session_reader else None,read_at),timeout=.1);break
                        except queue.Full:pass
            finally:stream.close()
        child.reader=threading.Thread(target=read,name=name+'-stdout',daemon=True);child.reader.start()
        self.log('component_started',component=name,pid=process.pid,generation=generation)
        return child

    def _stop_component(self,name,*,controlled_cycle=False):
        with self.guard:child=self.children.pop(name,None)
        if child is None:return
        process=child.process
        try:
            if (name=='native' and controlled_cycle and child.session_reader is not None
                    and self.cycle_control is not None):
                try:self.cycle_control.prepare_controlled_stop(child.session_owner)
                except Exception:self._cycle_block('CYCLE_CONTROLLED_STOP_UNVERIFIED')
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            timeout=max(35,self.config['hostbot']['stop_timeout']) if name=='star' else min(8,self.config['hostbot']['stop_timeout']) if name=='relay' else self.config['hostbot']['stop_timeout']
            def wait_child(seconds):
                if child.session_owner is not None:
                    from .process_ownership import ProcessOwnershipError
                    try:return child.session_owner.wait(timeout=seconds)
                    except TimeoutError:raise subprocess.TimeoutExpired(process.args,seconds) from None
                    except ProcessOwnershipError:pass  # A competing reaper does not prove this exit.
                return process.wait(timeout=seconds)
            try:wait_child(timeout)
            except subprocess.TimeoutExpired:
                if name=='native':
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:wait_child(5)
                    except subprocess.TimeoutExpired:pass
                if self._child_alive(child):
                    self.log('forced_stop',component=name,pid=process.pid)
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    wait_child(5)
            self.component_exits[name]=process.returncode
            if name=='native':self.last_exit_code=process.returncode
            if child.reader:child.reader.join(timeout=2)
            if name=='native' and child.session_reader is not None and self.cycle_control is not None:
                try:self.cycle_control.note_native_exit(child.session_owner)
                except Exception:self._cycle_block('CYCLE_EXIT_NOT_VERIFIED')
            self.log('component_stopped',component=name,exit_code=process.returncode)
        finally:
            if self._child_alive(child) and (child.preload is not None or child.session_owner is not None
                                           or (name=='native' and getattr(self,'cycle_session',None) is not None)):
                with self.guard:self.children[name]=child
            else:
                if child.preload is not None:child.preload.close()
                if child.session_reader:child.session_reader.close()
                if child.session_owner:child.session_owner.close()

    def _stop_realm(self):
        self.relay_observation={};self.star_observation={};self._publish_feedback(force=True)
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
        # Optional display-only metadata. Preserve the original observation
        # timestamp; a feedback refresh must not make old cursor evidence fresh.
        if self.cycle_session is not None:
            display={'version':1,'available':0}
            try:
                observed=self._cycle_current_observation(allow_full=True,monitoring=True)
                control=self.cycle_control
                if observed is not None and control is not None and not self.cycle_failed:
                    _,event,stamp=observed
                    catalog=control.catalog;cursor=control.state
                    lobby=event['phase'] in ('LOBBY','COUNTDOWN')
                    record=catalog.data['pending' if lobby else 'completed']
                    if (event['phase'] in ('LOBBY','COUNTDOWN','IN_GAME') and record is not None
                            and not catalog.failed and not cursor._write_failed
                            and record['status']=='staged' and record['counter']==event['host_counter']
                            and record['request_id']==event.get('catalog_request_id')
                            and record['index']==event.get('catalog_index')
                            and catalog.generation==event['session_generation']
                            and type(record['position']) is int and 0<=record['position']<len(catalog.indices)
                            and catalog.indices[record['position']]==record['index']):
                        position=record['position'];state=cursor.state
                        binding=state.get('pending' if lobby else 'last_started')
                        expected=position if lobby else (position+1)%len(catalog.indices)
                        if (state['index']==expected and binding is not None
                                and binding.get('session_generation')==catalog.generation
                                and binding.get('host_counter')==record['counter']):
                            display={'version':1,'observed_monotonic_ms':int(stamp*1000),
                                'ttl_ms':int(min(10,self.config['iccup']['native_state_ttl'])*1000),
                                'session_generation':catalog.generation,'manifest_sha256':catalog.digest,
                                'request_id':record['request_id'],'host_counter':record['counter'],
                                'catalog_index':record['index'],'cycle_position':position}
            except (OSError,ValueError,RuntimeError,KeyError,TypeError,AttributeError,IndexError,OverflowError):
                pass
            try:
                atomic_text(Path(self.config['iccup']['bridge_dir'])/'native-feedback.cfg.cycle',
                            ''.join(k+' = '+str(v)+'\n' for k,v in display.items()))
            except (OSError,ValueError):
                pass  # Informational output must never gate or stop gameplay.

    def _publish_feedback(self,force=False):
        try:
            self._feedback(force=force)
        except (OSError,ValueError):
            if self.cycle_session is None:
                raise
            # Publication storage failure must not end a native process that
            # may already be serving matches. Stop further cycle decisions.
            self._cycle_block('CYCLE_PUBLICATION_UNCERTAIN')

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

    def _native_line(self,generation,line,source=None,token=None,raw=None,read_at=None):
        child=self.children.get('native')
        if child is None or generation!=child.generation:return
        if line.startswith('[HOSTBOT_SESSION] ') and self.cycle_session is not None:
            if (source is not child or token is None or token is not child.reader_token
                or type(raw) is not bytes or not raw.endswith(b'\n') or len(raw)>4097
                or type(read_at) not in (int,float) or not child.started<=read_at<=time.monotonic()
                or self.cycle_control is None or self.cycle_failed):
                self._cycle_block('CYCLE_SESSION_READER_MISMATCH');return
            try:
                self.cycle_control.bind_native(child,raw[:-1])
                self.cycle_session_read=(child,read_at)
                spec=getattr(self.backend,'catalog_startup',None)
                if spec is not None:
                    if (Path(spec['journal_directory']) != self.cycle_control.lease.journal
                            or list(spec['cycle']) != self.cycle_control.state.cycle):
                        raise ValueError('CYCLE_CATALOG_STARTUP_MISMATCH')
                    self.cycle_control.attach_catalog(child,child.preload,spec['ipc_directory'],
                                                      self.config['gameplay']['max_concurrent_matches'])
            except Exception:self._cycle_block('CYCLE_SESSION_BIND_FAILED')
            return
        self.operator_native_line(line)
        try:
            event=native_state(line)
            if event is not None:
                self.native_observation=event;self.native_observed_at=time.monotonic()
                if self.cycle_session is not None and source is child and token is child.reader_token:
                    self.cycle_observation=None
                    try:
                        if (type(raw) is not bytes or not raw.endswith(b'\n') or len(raw)>4097
                                or raw.decode('utf-8','strict') != line
                                or type(read_at) not in (float,int) or not child.started<=read_at<=time.monotonic()):
                            raise ValueError('CYCLE_OBSERVATION_REJECTED')
                        data=json.loads(line[len('[HOSTBOT_STATE] '):])
                        if ('active_games' not in data or type(data['active_games']) is not int
                                or 'session_generation' not in data or 'uptime_seconds' not in data):
                            raise ValueError('CYCLE_OBSERVATION_REJECTED')
                        self.cycle_observation=(child,event,read_at)
                    except (ValueError,UnicodeError):pass
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
        elif self.cycle_status is not None and not self.alive('native'):
            state,reason='ERROR','cycle_'+self.cycle_status['state']
        elif not self.alive('native'):state,reason='RECONNECTING','waiting_native_restart'
        elif not self.native_initialized:state,reason='STARTING','waiting_native_initialization'
        elif not self.alive('relay') or not self.alive('star'):state,reason='RECONNECTING','waiting_realm_restart'
        elif not s['star_process_alive']:state,reason='STARTING','starting_original_star'
        elif not s['iccup_authenticated']:state,reason='AUTHENTICATING','waiting_real_star_authentication'
        elif not s['native_healthy']:state,reason='CONNECTED','waiting_native_lifecycle'
        elif (self.cycle_status is not None and s.get('cycle',{}).get('state')=='capacity_full'
              and s['iccup_authenticated'] and s['native_healthy']):state,reason='IN_GAME','cycle_capacity_full'
        elif phase=='IN_GAME':state,reason='IN_GAME','native_game_active'
        elif phase=='COUNTDOWN' and s['ready']:state,reason='COUNTDOWN','native_countdown_active'
        elif s['ready']:state,reason='LOBBY' if phase=='LOBBY' else 'CONNECTED','native_lobby_published' if phase=='LOBBY' else 'realm_ready_native_idle'
        else:state,reason='PUBLISHING','waiting_current_lobby_ack'
        if state!=self.state or reason!=self.reason:self.transition(state,reason)

    def _command(self,command):
        if command in ('stop','restart','quit'):
            self.want_running=False;self.stopping=True;self.transition('STOPPING','operator_'+command);self._publish_feedback(force=True)
            self._stop_component('native',controlled_cycle=True);self.native_initialized=False;self.native_observation={};self.native_listener=False
            time.sleep(.3);self._stop_realm();self.transition('STOPPED','operator_'+command);self.stopping=False
        if command in ('start','restart'):
            if self.engine_observed:return
            try:clean=self.backend.cleanup_prefix().get('cleaned') is True
            except Exception:clean=False
            if not clean:self.transition('ERROR','PREFIX_CLEANUP_FAILED');return
            self.permanent_errors.clear();self.want_running=True;self.circuit_open.clear();self.component_attempts={'native':0,'realm':0};self.retry_times={'native':0.,'realm':0.}
        if command=='quit':self.running=False

    def _cycle_block(self,reason):
        self.cycle_failed=True
        with self.guard:self.cycle_status={'state':'storage_error','recovery':'unavailable'}
        self.log('cycle_control_blocked',reason=reason)

    def _cycle_initialize(self):
        if self.cycle_session is None:return
        try:
            from .production_cycle import ProductionCycleControl
            from .session_lease import SessionLease
            root=Path(self.cycle_session['root'])
            private_directory(root.parent)
            # Fence first provisioning outside the session root. Losing a
            # previously managed root must not silently reset cursor/ownership.
            provision=root.parent/('.'+root.name+'.provisioned')
            contents=b'phase4-cycle-root-v1\n'
            first=False
            try:
                fd=os.open(provision,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
            except FileExistsError:
                if private_bytes(provision,128)!=contents:raise ValueError('CYCLE_ROOT_PROVISION_REJECTED')
            else:
                try:
                    os.fchmod(fd,0o600)
                    if os.write(fd,contents)!=len(contents):raise OSError('short provisioning write')
                    os.fsync(fd)
                finally:os.close(fd)
                directory=os.open(root.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
                try:os.fsync(directory)
                finally:os.close(directory)
                first=True
            if not root.exists() and not root.is_symlink():
                if not first:raise ValueError('CYCLE_ROOT_MISSING')
                lease=SessionLease.create(root,enabled=True)
                lease.close()
            self.cycle_control=ProductionCycleControl(root,self.cycle_session['cycle'])
        except Exception:
            self._cycle_block('CYCLE_INITIALIZATION_FAILED')

    def _cycle_tick(self):
        if self.cycle_control is None or self.cycle_failed:return
        now=time.monotonic()
        if now<self.cycle_next_scan:return
        self.cycle_next_scan=now+1.0
        try:
            cursor_before=self.cycle_control.state.state.get('last_started')
            queue_before=self.cycle_control.catalog
            selection_before=None if queue_before is None else queue_before.data['pending']
            status=self.cycle_control.tick()
            verified_transition=status.get('driver')
            if self.cycle_control.state.state.get('last_started') != cursor_before:
                verified_transition='started'
            if selection_before is not None and queue_before is not None:
                completed=queue_before.data['completed']
                if (queue_before.data['pending'] is None and completed is not None
                        and completed['request_id']==selection_before['request_id']
                        and completed.get('outcome')=='closed'):
                    verified_transition=('idle_rotated' if self.cycle_control.state._find_idle(
                        queue_before.generation,completed['counter']) else 'closed')
            observation=self._cycle_current_observation()
            if observation is not None and self.cycle_control.driver is not None:
                child,event,stamp=observation
                driver=self.cycle_control.driver
                record=driver.catalog.data['pending']
                lobby=None
                if (record is not None and native_catalog_lobby_matches(event,
                        generation=driver.catalog.generation,request_id=record['request_id'],
                        catalog_index=record['index'],host_counter=event['host_counter'])):
                    from .cycle_driver import LobbyObservation
                    lobby=LobbyObservation(event['session_generation'],record['request_id'],record['index'],event['host_counter'])
                # Recheck the same owned, fresh observation immediately before I/O.
                if self._cycle_current_observation() == observation:
                    select=record is None and event['phase'] in ('IDLE','IN_GAME')
                    result=driver.step(lobby,select=select,capacity_available=True,
                                       revalidate=lambda:self._cycle_current_observation()==observation)
                    status['driver']=result.state
                    status['pending']=(self.cycle_control.state.state['pending'] is not None or
                                       driver.catalog.data['pending'] is not None)
                    status['state']='blocked_pending' if status['pending'] else 'waiting_native_ipc'
            from .cycle_monitor import project
            observed=self._cycle_current_observation(allow_full=True,monitoring=True)
            telemetry=None
            if observed is not None:
                event=dict(observed[1]);matches=event.get('matches_v1')
                if matches is not None:
                    previous=self.cycle_telemetry_highwater
                    current=(event['session_generation'],event['uptime_seconds'],
                             matches['retired_count'],matches['rotation_error_count'])
                    catalog=self.cycle_control.catalog
                    if (catalog is None or any(item['catalog_index'] is not None and
                            item['catalog_index']>=len(catalog.entries) for item in matches['matches'])
                            or previous is not None and previous[0]==current[0] and
                               (current[1]<previous[1] or current[2]<previous[2] or current[3]<previous[3])):
                        event.pop('matches_v1')
                    else:self.cycle_telemetry_highwater=current
                telemetry=(event,observed[2],self.cycle_control.catalog)
            status=project(status,self.cycle_control.state,self.cycle_control.max_games,telemetry)
            transition=verified_transition if verified_transition in ('started','closed','idle_rotated') else status.get('driver')
            if transition == 'idle_rotated':
                self.log('cycle_idle_rotation')
            if transition in ('started','closed','idle_rotated'):
                self.cycle_last_transition={'event':transition,'observed_at':datetime.now(timezone.utc).isoformat()}
            status['last_transition']=self.cycle_last_transition
            status['availability']['last_transition']='verified_observed' if self.cycle_last_transition is not None else 'unavailable'
            signature=(status['index'],status['state'],status['active_matches'],status['max_matches'],
                       status['finished_matches'],status['rotation_errors'],
                       status['availability']['current_lobby'],
                       None if status['current_lobby'] is None else status['current_lobby']['host_counter'],
                       None if self.cycle_last_transition is None else self.cycle_last_transition['observed_at'])
            with self.guard:self.cycle_status=status
            if signature!=self.cycle_monitor_signature:
                self.cycle_monitor_signature=signature
                self.log('cycle_monitor',position=status['index']+1,total=len(status['map_cycle']),
                         current=status['current_preset'],next_preset=status['next_preset'],
                         active=status['active_matches'],maximum=status['max_matches'],
                         cycle=status['map_cycle'],lobby=status['current_lobby'],
                         lobby_name=telemetry[0].get('game_name') if telemetry is not None and status['current_lobby'] is not None else None,
                         lobby_availability=status['availability']['current_lobby'],
                         finished=status['finished_matches'],rotation_errors=status['rotation_errors'],
                         last_transition=None if self.cycle_last_transition is None else self.cycle_last_transition['event'],
                         capacity_full=status['state']=='capacity_full')
        except Exception as error:
            # Never stop existing native games for an uncertain journal/cursor.
            group_errors={'CYCLE_WRITER_SCOPE_UNPROVEN','CYCLE_PROCESS_GROUP_UNVERIFIED',
                          'CYCLE_PROCESS_GROUP_NOT_EMPTY'}
            reason=str(error) if isinstance(error,ValueError) and str(error) in group_errors else 'CYCLE_JOURNAL_UNCERTAIN'
            self._cycle_block(reason)

    def _cycle_current_observation(self, *, allow_full=False, monitoring=False):
        control=self.cycle_control
        candidate=self.cycle_observation
        if control is None or control.driver is None or candidate is None:return None
        child,event,stamp=candidate
        now=time.monotonic()
        session=self.cycle_session_read
        if session is None or session[0] is not child:return None
        # Native uptime is measured from before HOSTBOT_SESSION was emitted.
        # This upper bound catches old pipe-buffered records read much later.
        # Monitoring follows the native heartbeat timeout (four-second producer),
        # while admission/revalidation keeps the shorter realm metadata window.
        ttl=self.config['iccup']['native_state_ttl' if monitoring else 'metadata_ttl']
        upper=session[1]+event['uptime_seconds']+1
        if now-upper>ttl or event['uptime_seconds']>now-child.started+1:return None
        if (child is not self.children.get('native') or child.session_owner is None
                or control.lease._native_reader is None
                or child.session_owner is not control.lease._native_reader.child
                or not child.session_owner.is_running()
                or event.get('session_generation')!=control.lease._native_generation
                or not child.started<=stamp<=now or now-stamp>ttl
                or type(event.get('active_games')) is not int
                or not 0<=event['active_games']<=control.max_games
                or not allow_full and event['active_games']>=control.max_games):return None
        control.lease.require_native(event['session_generation'])
        control.check_ipc()
        return candidate

    def _cycle_permits_native_spawn(self):
        if self.cycle_session is None:return True
        control=getattr(self,'cycle_control',None)
        if (getattr(self,'cycle_failed',True) or control is None or not getattr(self,'prepared',False)
                or not isinstance(self.backend,RuntimeBackend) or not self.backend._auto_catalog
                or self.cycle_session['cycle'] != self.config['gameplay']['map_cycle']
                or control.state.state['pending'] is not None or control.catalog is not None
                or control.driver is not None or control._ipc_fd is not None):
            return False
        try:
            control.lease.check()
            if os.listdir(control.lease.journal_fd):return False
            claim='native-launch-'+control.lease.journal.name+'.json'
            try:os.stat(claim,dir_fd=control.lease.fd,follow_symlinks=False)
            except FileNotFoundError:return True
            return False
        except (OSError,ValueError,RuntimeError):return False

    def run(self):
        self.transition('ERROR' if self.engine_observed else 'STARTING','GAME_ENGINE_FORBIDDEN' if self.engine_observed else 'starting_production_components')
        try:
            self._cycle_initialize()
            while self.running:
                self.last_tick=time.monotonic();now=self.last_tick
                self._cycle_tick()
                if self.want_running:
                    for kind in list(self.circuit_open):
                        if kind not in self.permanent_errors and now>=self.retry_times[kind] and self.config['gameplay']['reconnect_enabled']:
                            self.circuit_open.remove(kind);self.component_attempts[kind]=max(0,self.config['recovery']['max_restarts']-1);self.log('circuit_half_open',component=kind)
                    if self._cycle_permits_native_spawn() and not self.alive('native') and 'native' not in self.children and 'native' not in self.circuit_open and now>=self.retry_times['native']:
                        try:self._spawn('native');self.native_initialized=False;self.native_observed_at=0;self.last_refresh_request=0
                        except Exception:self._failure('native','native_launch_failed')
                    if self.alive('native') and self.native_initialized and 'relay' not in self.children and 'star' not in self.children and 'realm' not in self.circuit_open and now>=self.retry_times['realm']:
                        try:
                            self._spawn('relay');self._spawn('star');self.realm_started=time.monotonic();self.realm_connected_at=None
                        except Exception:self._failure('realm','realm_component_launch_failed')
                try:
                    event=self.events.get(timeout=.1)
                    if event[0]=='command':self._command(event[1])
                    elif event[0]=='component_line' and event[1]=='native':self._native_line(event[2],event[3],*event[4:])
                except queue.Empty:pass
                if not self.want_running:continue
                self._metadata();self._publish_feedback()
                if self.star_observation.get('game_engine_observed') is True:
                    atomic_json(self.runtime.parent/'engine-blocked.json',{'version':1,'blocked':True})
                    self.engine_observed=True;self.permanent_errors['engine']='GAME_ENGINE_FORBIDDEN';self.want_running=False;self.transition('ERROR','GAME_ENGINE_FORBIDDEN');break
                if any(type(self.relay_observation.get(key)) is int and self.relay_observation[key]!=0 for key in ('server_logon_status','server_proof_status')):
                    self.permanent_errors['realm']='AUTHENTICATION_REJECTED';self._stop_realm();self.circuit_open.add('realm');self.transition('ERROR','AUTHENTICATION_REJECTED');self._save();continue
                dead=next((name for name,child in self.children.items() if not self._child_alive(child)),None)
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
            self._stop_component('native',controlled_cycle=True);self.native_listener=False;self.native_initialized=False;self.native_observation={}
            self.cycle_next_scan=0.;self._cycle_tick()
            time.sleep(.3);self._stop_realm();self.backend.close();self.transition('STOPPED','supervisor_shutdown')
            if self.cycle_control is not None:self.cycle_control.close()
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
