"""Bounded observations of the actual production entrypoint; no auth construction."""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from urllib.request import urlopen

from src.runtime.files import atomic_json
from src.runtime.processes import snapshot

DIAGNOSTIC_BYTES=65536
METADATA={'relay':'/data/state/bridge/relay-status.json','star':'/data/state/star-status.json'}
STATES={'STOPPED','STARTING','CONNECTING','AUTHENTICATING','CONNECTED','PUBLISHING','READY',
        'LOBBY','COUNTDOWN','IN_GAME','RECONNECTING','ERROR','STOPPING'}


def diagnostic_fields(value,flags=(),counts=(),times=(),enums=None):
    """Fixed-key, scalar-only projection. Never copy arbitrary strings or containers."""
    if not isinstance(value,dict):return {}
    out={key:value[key] for key in flags if type(value.get(key)) is bool}
    out.update({key:value[key] for key in counts if type(value.get(key)) is int and -2**31<=value[key]<2**64})
    out.update({key:value[key] for key in times if type(value.get(key)) in (int,float)
                and abs(value[key])<2**64 and math.isfinite(value[key])})
    for key,allowed in (enums or {}).items():
        if type(value.get(key)) is str and value[key] in allowed:out[key]=value[key]
    return out


def diagnostic_view(source,value):
    if not isinstance(value,dict):return {}
    if source=='status':
        out=diagnostic_fields(value,
            ('ready','child_alive','iccup_authenticated','realm_connected','relay_healthy','star_process_alive',
             'native_healthy','player_listener_available','lobby_publication_accepted','game_engine_observed'),
            ('pid','restarts','last_exit_code'),('uptime_seconds',),{'state':STATES})
        out['permanent_errors_present']=bool(value.get('permanent_errors'))
        components=value.get('components',{})
        out['components']={name:diagnostic_fields(components.get(name),('alive',),('pid','generation','last_exit'))
                           for name in ('native','relay','star')} if isinstance(components,dict) else {}
        out['component_restarts']=diagnostic_fields(value.get('component_restarts'),counts=('native','realm'))
        out['game']=diagnostic_fields(value.get('game'),('loading','ai_supported','realm_online','feedback_valid'),
            ('players','computer_players','ai_target','max_players','autostart_players','votes','votes_required',
             'countdown','host_counter','active_games','refresh_count','refresh_skipped','refresh_sequence',
             'publication_refresh_count','publication_refresh_successes','publication_refresh_failures'),
            ('last_refresh_monotonic_ms','last_ack_monotonic_ms','uptime_seconds'),
            {'phase':{'IDLE','LOBBY','COUNTDOWN','IN_GAME'}})
        return out
    if source=='relay':
        out=diagnostic_fields(value,
            ('connected','authenticated','expected_identity_confirmed','realm_connection_healthy',
             'advertisement_accepted','native_intent_seen','native_heartbeat_fresh','exact_lookup_visible',
             'public_directory_seen_for_current_lobby','publication_failed','chat_return_pending',
             'session_closing','listener_ready','channel_confirmed'),
            ('relay_pid','native_pid','native_generation','native_host_counter','native_sequence','session_generation',
             'server_logon_status','server_proof_status','server_publication_status','last_ack_status',
             'advertisements_sent','publication_acks','refresh_requested_count','refresh_sent_count',
             'refresh_successful_count','refresh_failed_count','chat_returns_confirmed'),
            ('observed_monotonic_seconds','relay_started_monotonic_seconds','session_started_monotonic_seconds',
             'session_closed_monotonic_seconds','last_native_intent_monotonic_seconds',
             'last_client_traffic_monotonic_seconds','last_server_traffic_monotonic_seconds',
             'last_ack_monotonic_seconds','last_refresh_ack_monotonic_seconds','exact_lookup_match_age_seconds'))
        out['error_present']=bool(value.get('error'))
        return out
    if source=='star':
        return diagnostic_fields(value,('launcher_alive','game_engine_observed','forced_stop'),
            ('worker_pid','launcher_pid','generation','exit_code','normal_close_helper_exit','renderer_count'),
            ('observed_monotonic_seconds',),{'phase':{'STARTING','RUNNING','STOPPING','STOPPED','ERROR'}})
    return {}


def diagnostic_json(data):
    if len(data)>DIAGNOSTIC_BYTES:raise ValueError('DIAGNOSTIC_OVERSIZE')
    value=json.loads(data)
    if not isinstance(value,dict):raise ValueError('DIAGNOSTIC_OBJECT_REQUIRED')
    return value


def diagnostic_status():
    with urlopen('http://127.0.0.1:8080/status',timeout=2) as response:
        return diagnostic_json(response.read(DIAGNOSTIC_BYTES+1))


def diagnostic_metadata(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>DIAGNOSTIC_BYTES:raise ValueError('DIAGNOSTIC_FILE_REJECTED')
        return diagnostic_json(os.read(fd,DIAGNOSTIC_BYTES+1))
    finally:os.close(fd)


def failure_diagnostics(observed,status_reader=None,metadata_reader=None):
    """Capture once before teardown; unavailable inputs must not mask the failure."""
    status_reader=status_reader or diagnostic_status;metadata_reader=metadata_reader or diagnostic_metadata
    out={'version':1,'last_observed':{},'before_teardown':{}}
    for source in ('status','relay','star'):
        prior=observed.get(source,{})
        out['last_observed'][source]=diagnostic_view(source,prior)
        stamp=diagnostic_fields(observed,times=(source+'_read_monotonic_seconds',))
        out['last_observed'].update(stamp)
        sample={'read_started_monotonic_seconds':time.monotonic(),'read_ok':False}
        try:
            value=status_reader() if source=='status' else metadata_reader(METADATA[source])
            if isinstance(value,dict):sample.update(read_ok=True,fields=diagnostic_view(source,value))
        except Exception:pass  # Exception contents can contain credentials or paths.
        sample['read_finished_monotonic_seconds']=time.monotonic()
        out['before_teardown'][source]=sample
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',required=True,type=int)
    p.add_argument('--exercise-rehost',action='store_true');a=p.parse_args()
    result={'result':'FAIL','scope':'bounded local CI original-Star production session',
            'gameplay_green':False,'amp_green':False,'wan_green':False,'game_engine_observed':False,
            'gui_actions':False,'canonical_password_mounted':False,'auth_or_guard_constructed':False}
    started=time.monotonic();child=None;exact_start=None;exact_longest=0;directory=False
    config=json.loads(Path('/data/config/supervisor.json').read_text())
    expect_footman=config.get('gameplay',{}).get('map_preset')=='footman-ultimate-ai'
    peak_memory=0;peak_pids=0;before={};final_relay={};observed={}
    log=Path('/data/logs/online-probe.jsonl').open('a')
    def read(path):
        try:value=json.loads(Path(path).read_text())
        except (OSError,ValueError):value={}
        for source,fixed in METADATA.items():
            if path==fixed:
                observed[source]=value;observed[source+'_read_monotonic_seconds']=time.monotonic()
        return value
    def status():
        with urlopen('http://127.0.0.1:8080/status',timeout=2) as response:value=json.load(response)
        observed['status']=value;observed['status_read_monotonic_seconds']=time.monotonic()
        return value
    def wait(predicate,seconds,reason):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if child.poll() is not None:raise RuntimeError('SUPERVISOR_EXITED')
            try:
                if predicate():return
            except (OSError,ValueError):pass
            time.sleep(.25)
        raise RuntimeError(reason)
    def first_ready():
        s=status()
        if s.get('permanent_errors'):raise RuntimeError('PERMANENT_RUNTIME_ERROR')
        return s['ready'] and s['state']=='LOBBY' and s['iccup_authenticated'] and s['lobby_publication_accepted']
    try:
        child=subprocess.Popen([sys.executable,'-m','src.supervisor','--config','/data/config/supervisor.json'],stdin=subprocess.PIPE,stdout=log,stderr=log,text=True)
        wait(first_ready,240,'AUTHENTICATED_LOBBY_TIMEOUT')
        ready_at=time.monotonic();before=status();native_pid=before['pid'];star_generation=before['components']['star']['generation']
        result['startup_seconds']=round(ready_at-started,3)
        if a.exercise_rehost:
            def initial_exact_lookup():
                s=status();r=read(METADATA['relay']);now=time.monotonic()
                stamp=r.get('observed_monotonic_seconds')
                return (s['ready'] and s['state']=='LOBBY' and s['pid']==native_pid
                        and s['components']['star']['generation']==star_generation
                        and r.get('exact_lookup_visible') is True and r.get('native_pid')==native_pid
                        and r.get('native_host_counter')==s['game']['host_counter']
                        and type(stamp) in (int,float) and math.isfinite(stamp) and 0<=now-stamp<=3)
            wait(initial_exact_lookup,100,'INITIAL_EXACT_LOOKUP_TIMEOUT')
            from production_lifecycle import exercise
            result['synthetic_protocol_lifecycle']=exercise(status,read,wait)
            before=status();ready_at=time.monotonic()
        deadline=ready_at+a.seconds
        while time.monotonic()<deadline:
            s=status();r=read('/data/state/bridge/relay-status.json');sample=snapshot();now=time.monotonic()
            if sample['game_engine_observed']:result['game_engine_observed']=True;raise RuntimeError('GAME_ENGINE_FORBIDDEN')
            if sample['monitor_error']:raise RuntimeError('PROCESS_MONITOR_FAILED')
            if child.poll() is not None or not s['ready'] or not s['iccup_authenticated']:raise RuntimeError('CONTINUOUS_SESSION_LOST')
            if expect_footman:
                game=s.get('game',{})
                if (game.get('game_name')!='Footman Ultimate AI' or game.get('players')!=0
                        or game.get('computer_players')!=6 or game.get('ai_target')!=6
                        or game.get('ai_supported') is not True):raise RuntimeError('FOOTMAN_AI_LOBBY_CHANGED')
            if s['pid']!=native_pid or s['components']['star']['generation']!=star_generation or sum(s['component_restarts'].values()):raise RuntimeError('UNEXPECTED_COMPONENT_RESTART')
            if r.get('public_directory_seen_for_current_lobby') is True:directory=True
            if r.get('exact_lookup_visible') is True:
                if exact_start is None:exact_start=now
                exact_longest=max(exact_longest,now-exact_start)
            else:exact_start=None
            peak_memory=max(peak_memory,int(Path('/sys/fs/cgroup/memory.current').read_text()))
            peak_pids=max(peak_pids,int(Path('/sys/fs/cgroup/pids.current').read_text()))
            before=s;final_relay=r;time.sleep(.5)
        result['authenticated_soak_seconds']=round(time.monotonic()-ready_at,3)
        if expect_footman:result['footman_lobby']={'exact_name':'Footman Ultimate AI','humans':0,'computers':6,'ai_supported':True}
        if not directory or exact_longest<30:raise RuntimeError('DIRECTORY_OR_EXACT_LOOKUP_PROOF_MISSING')
        # One completed wave may visit up to twelve empty map rows. Allow the
        # final wave/ACK to finish instead of mistaking its boundary for failure.
        refresh_interval=config.get('gameplay',{}).get('lobby_refresh_interval',10)
        expected_refresh=max(1,a.seconds//max(refresh_interval,12)-2)
        if before['game'].get('refresh_count',0)<expected_refresh or final_relay.get('refresh_successful_count',0)<expected_refresh:raise RuntimeError('CORRELATED_SLOT_REFRESH_ACKS_MISSING')
        result.update(public_directory_seen=True,continuous_exact_lookup_seconds=round(exact_longest,3),
                      publication_acks=final_relay.get('publication_acks'),
                      native_slot_refresh_count=before['game'].get('refresh_count'),
                      refresh_sent=final_relay.get('refresh_sent_count'),refresh_acks=final_relay.get('refresh_successful_count'),
                      refresh_failures=final_relay.get('refresh_failed_count'),bump_proven=False,
                      last_refresh_ack_context=final_relay.get('last_refresh_ack_context'))
        child.stdin.write('stop\n');child.stdin.flush()
        wait(lambda:status()['state']=='STOPPED',115,'ORDERLY_STOP_TIMEOUT')
        stopped=status();star=read('/data/state/star-status.json');relay=read('/data/state/bridge/relay-status.json')
        intent=read('/data/state/bridge/native-intent.json')
        if stopped['ready'] or stopped['iccup_authenticated'] or any(x['alive'] for x in stopped['components'].values()):raise RuntimeError('STOP_LEFT_COMPONENT_ALIVE')
        if any(x['last_exit']!=0 for x in stopped['components'].values()):raise RuntimeError('NONZERO_COMPONENT_EXIT')
        if star.get('normal_close_helper_exit')!=0 or star.get('forced_stop') is not False:raise RuntimeError('STAR_NORMAL_CLOSE_NOT_PROVEN')
        if intent.get('operation')!='unpublish' or relay.get('authenticated') is not False or relay.get('advertisement_accepted') is not False:raise RuntimeError('TERMINAL_SESSION_STATE_INVALID')
        child.stdin.write('quit\n');child.stdin.flush()
        if child.wait(timeout=30)!=0:raise RuntimeError('SUPERVISOR_NONZERO_EXIT')
        end=time.monotonic()+10
        while any(x['state']=='Z' for x in snapshot()['processes']) and time.monotonic()<end:time.sleep(.25)
        remaining=snapshot()
        if any(x['state']=='Z' or x['name'].lower().startswith(('wine','launcher','qtwebengine','xvfb','ghost')) for x in remaining['processes']):raise RuntimeError('CHILD_OR_ZOMBIE_LEFTOVER')
        result.update(result='PASS',normal_close_helper_exit=0,component_exit_codes={k:v['last_exit'] for k,v in stopped['components'].items()},withdrawal_intent_observed=True,no_zombies=True)
    except Exception as error:
        result['reason']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        result['failure_diagnostics']=failure_diagnostics(observed)
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:child.wait(timeout=115)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if child and child.stdin:child.stdin.close()
        log.close();result.update(elapsed_seconds=round(time.monotonic()-started,3),max_cgroup_memory_bytes=peak_memory,max_cgroup_pids=peak_pids)
        atomic_json('/data/state/online-probe-result.json',result);print(json.dumps(result),flush=True)
    return 0 if result['result']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
