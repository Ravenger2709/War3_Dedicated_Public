"""Actual packaged native/Wine offline lifecycle; no account, GUI or engine."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

from src.runtime.files import atomic_json
from src.runtime.processes import snapshot,stop_owned_prefix


def wait(predicate,timeout,reason):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.25)
    raise RuntimeError(reason)


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=60);p.add_argument('--crash-star',action='store_true');args=p.parse_args()
    result={'result':'FAIL','network':'none','real_vendor_executed':True,'synthetic_profile_only':True,
            'real_client_tested':False,'iccup_authenticated':False,'checks':[]}
    start=time.monotonic();native=None;star=None;worker_started=0.;max_processes=0;peak_memory=0;peak_pids=0;cpu_start=None
    config=json.loads(Path('/data/config/supervisor.json').read_text())
    expect_footman=config.get('gameplay',{}).get('map_preset')=='footman-ultimate-ai'
    log=Path('/data/logs/probe-native.jsonl').open('a')
    def status():
        with urlopen('http://127.0.0.1:8080/status',timeout=2) as response:return json.load(response)
    def healthy_native():
        try:
            s=status();game=s.get('game',{})
            ai_ok=(not expect_footman or (game.get('game_name')=='Footman Ultimate AI'
                   and game.get('players')==0 and game.get('computer_players')==6
                   and game.get('ai_target')==6 and game.get('ai_supported') is True))
            return native.poll() is None and s['ready'] and s['state']=='LOBBY' and ai_ok
        except (OSError,ValueError):return False
    def star_status():
        try:return json.loads(Path('/data/state/star-status.json').read_text())
        except (OSError,ValueError):return {}
    def healthy_star():
        if star.poll() is not None:raise RuntimeError('STAR_BOOTSTRAP_'+str(star_status().get('reason','UNKNOWN')))
        s=star_status()
        return s.get('worker_pid')==star.pid and s.get('generation')==generation and s.get('observed_monotonic_seconds',0)>=worker_started and s.get('phase')=='RUNNING' and s.get('launcher_alive') is True
    def sample():
        nonlocal max_processes,peak_memory,peak_pids,cpu_start
        rows=snapshot()
        if rows['game_engine_observed']:raise RuntimeError('GAME_ENGINE_FORBIDDEN')
        if rows['monitor_error']:
            result['monitor_error_codes']=rows.get('monitor_error_codes',[])
            raise RuntimeError('PROCESS_MONITOR_FAILED')
        max_processes=max(max_processes,len(rows['processes']))
        result['container_init_observations']=result.get('container_init_observations',0)+rows.get('container_initializations',0)
        result['max_zombies_during_runtime']=max(result.get('max_zombies_during_runtime',0),sum(row['state']=='Z' for row in rows['processes']))
        cgroup=Path('/sys/fs/cgroup')
        peak_memory=max(peak_memory,int((cgroup/'memory.current').read_text()))
        peak_pids=max(peak_pids,int((cgroup/'pids.current').read_text()))
        memory_events={k:int(v) for k,v in (line.split() for line in (cgroup/'memory.events').read_text().splitlines())}
        memory_stats={k:int(v) for k,v in (line.split() for line in (cgroup/'memory.stat').read_text().splitlines())}
        result['memory_events']=memory_events
        result['peak_anonymous_memory_bytes']=max(result.get('peak_anonymous_memory_bytes',0),memory_stats.get('anon',0))
        if memory_events.get('oom_kill',0):raise RuntimeError('CGROUP_OOM_KILL')
        cpu=dict(line.split() for line in (cgroup/'cpu.stat').read_text().splitlines())
        if cpu_start is None:cpu_start=int(cpu['usage_usec'])
        result['cpu_usage_seconds']=round((int(cpu['usage_usec'])-cpu_start)/1e6,3)
        return rows
    def stop(child,timeout):
        child.send_signal(signal.SIGTERM)
        if child.wait(timeout=timeout)!=0:raise RuntimeError('COMPONENT_NONZERO_EXIT')
    try:
        native=subprocess.Popen([sys.executable,'-m','src.supervisor','--config','/data/config/supervisor.json'],stdout=log,stderr=log,stdin=subprocess.DEVNULL)
        wait(healthy_native,100,'NATIVE_LOBBY_TIMEOUT');result['checks'].append('actual_native_offline_lobby')
        if expect_footman:result['checks'].append('actual_footman_exact_title_zero_humans_six_computers')
        initial_db=Path('/data/sqlite/ghost.dbs').stat().st_ino
        native_pid=status()['pid']
        for generation in ((1,2,3) if args.crash_star else (1,2)):
            cfg=json.loads(Path('/data/config/star-worker.json').read_text());cfg['generation']=generation
            atomic_json('/data/state/star-worker.json',cfg)
            worker_started=time.monotonic()
            star=subprocess.Popen([sys.executable,'-m','src.runtime.star','--config','/data/state/star-worker.json'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL)
            wait(healthy_star,200,'STAR_START_TIMEOUT');result['checks'].append('actual_star_cold_boot' if generation==1 else 'actual_star_persistent_prefix_restart')
            if args.crash_star and generation==3:result['checks'].append('actual_star_restart_after_worker_sigkill')
            end=time.monotonic()+(args.seconds if generation==1 else 5)
            while time.monotonic()<end:
                sample()
                if not healthy_native() or not healthy_star():raise RuntimeError('SOAK_COMPONENT_UNHEALTHY')
                time.sleep(.5)
            before_stop=star_status()
            result.setdefault('star_before_stop',[]).append({k:before_stop.get(k) for k in ('generation','renderer_count','renderer_health','game_engine_observed')})
            crashed=args.crash_star and generation==2
            if crashed:
                star.kill()
                if star.wait(timeout=5)!=-signal.SIGKILL:raise RuntimeError('STAR_SIGKILL_NOT_OBSERVED')
            else:stop(star,40)
            worker_exit=star.returncode;star=None
            final=star_status()
            result.setdefault('star_stops',[]).append({**{k:final.get(k) for k in ('exit_code','normal_close_helper_exit','forced_stop','game_engine_observed','renderer_health')},'intentional_worker_sigkill':crashed,'worker_exit_code':worker_exit})
            clean=stop_owned_prefix(Path('/data/prefix'),timeout=12)
            if not clean['cleaned']:raise RuntimeError('PREFIX_CLEANUP_FAILED')
            result.setdefault('prefix_cleanup',[]).append(clean)
            if status()['pid']!=native_pid:raise RuntimeError('STAR_RECOVERY_RESTARTED_NATIVE')
            wait(lambda:not any(row['name'].lower().startswith(('wine','launcher','qtwebengine','xvfb','services.exe','explorer.exe','winedevice.exe','plugplay.exe','rpcss.exe','svchost.exe','conhost.exe')) for row in sample()['processes']),10,'VENDOR_PROCESS_LEFTOVER')
        before=status()['pid'];os.kill(before,signal.SIGKILL)
        wait(lambda:healthy_native() and status()['pid']!=before,100,'NATIVE_CRASH_RECOVERY_FAILED')
        result['checks'].append('actual_native_crash_recovery')
        stop(native,35);native=None
        native=subprocess.Popen([sys.executable,'-m','src.supervisor','--config','/data/config/supervisor.json'],stdout=log,stderr=log,stdin=subprocess.DEVNULL)
        wait(healthy_native,100,'NATIVE_PERSISTENT_RESTART_FAILED')
        if Path('/data/sqlite/ghost.dbs').stat().st_ino!=initial_db:raise RuntimeError('DATABASE_REPLACED')
        result['checks'].append('sqlite_persists_full_supervisor_restart')
        stop(native,35);native=None
        wait(lambda:not any(row['state']=='Z' for row in sample()['processes']),10,'ZOMBIE_LEFTOVER')
        result['checks'].extend(['actual_native_stop_exit_zero','no_vendor_process_leftovers','no_zombies_after_stop','no_engine_execution_observed'])
        result['result']='PASS'
    except Exception as error:
        result['error']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
    finally:
        for child in (star,native):
            if child and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:child.wait(timeout=40)
                except subprocess.TimeoutExpired:child.kill();child.wait()
        log.close()
        result.update(elapsed_seconds=round(time.monotonic()-start,3),requested_soak_seconds=args.seconds,max_processes=max_processes,max_cgroup_memory_bytes=peak_memory,max_cgroup_pids=peak_pids)
        atomic_json('/data/state/offline-probe-result.json',result)
        print(json.dumps(result),flush=True)
    return 0 if result['result']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
