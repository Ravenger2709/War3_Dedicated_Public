"""Actual production supervisor/cohort recovery, network none, synthetic profile."""
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
from src.runtime.processes import snapshot


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=5);a=p.parse_args()
    result={'result':'FAIL','network':'none','synthetic_profile_only':True,
            'actual_original_star_native_relay':True,'iccup_authenticated':False,'checks':[]}
    started=time.monotonic();child=None
    config=json.loads(Path('/data/config/supervisor.json').read_text())
    expect_footman=config.get('gameplay',{}).get('map_preset')=='footman-ultimate-ai'
    log=Path('/data/logs/cohort-probe.jsonl').open('a')
    def status():
        with urlopen('http://127.0.0.1:8080/status',timeout=2) as response:return json.load(response)
    def wait(test,seconds,reason):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if child.poll() is not None:raise RuntimeError('SUPERVISOR_EXITED')
            try:
                if test():return
            except (OSError,ValueError):pass
            time.sleep(.25)
        raise RuntimeError(reason)
    def running():
        s=status()
        if s['ready'] or s['iccup_authenticated'] or s['lobby_publication_accepted']:raise RuntimeError('OFFLINE_AUTHENTICATION_FALSE_POSITIVE')
        if s.get('permanent_errors'):raise RuntimeError('PERMANENT_RUNTIME_ERROR')
        if expect_footman:
            game=s.get('game',{})
            if (game.get('phase')!='LOBBY' or game.get('ai_supported') is not True
                    or game.get('ai_target')!=6 or game.get('computer_players')!=6
                    or game.get('players')!=0 or game.get('game_name')!='Footman Ultimate AI'):
                return False
        return all(x['alive'] for x in s['components'].values()) and s['star_process_alive'] and s['native_healthy'] and s['player_listener_available']
    def command(value):child.stdin.write(value+'\n');child.stdin.flush()
    try:
        child=subprocess.Popen([sys.executable,'-m','src.supervisor','--config','/data/config/supervisor.json'],stdin=subprocess.PIPE,stdout=log,stderr=log,text=True)
        wait(running,240,'INITIAL_COHORT_TIMEOUT');result['checks'].append('actual_production_entrypoint_three_components_no_false_readiness')
        initial=status();native_pid=initial['pid'];database_inode=Path('/data/sqlite/ghost.dbs').stat().st_ino
        if expect_footman:result['checks'].append('actual_footman_exact_title_zero_humans_six_computers')
        # A second runtime directory cannot bypass ownership of the same profile.
        cfg=json.loads(Path('/data/config/supervisor.json').read_text());cfg['runtime_dir']='/data/state/duplicate-supervisor';cfg['iccup']['bridge_dir']='/data/state/duplicate-bridge'
        atomic_json('/data/state/duplicate-config.json',cfg)
        duplicate=subprocess.run([sys.executable,'-m','src.supervisor','--config','/data/state/duplicate-config.json'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if duplicate.returncode!=2:raise RuntimeError('DUPLICATE_PROFILE_OWNER_NOT_REJECTED')
        result['checks'].append('actual_duplicate_profile_flock_rejected')
        for component in ('star','relay'):
            before=status()['components'];old=before[component]['pid'];old_generation=before[component]['generation']
            os.kill(old,signal.SIGKILL)
            wait(lambda:running() and status()['components'][component]['generation']>old_generation,180,component.upper()+'_RECOVERY_TIMEOUT')
            after=status()
            if after['pid']!=native_pid:raise RuntimeError('REALM_RECOVERY_RESTARTED_NATIVE')
            if after['components']['star']['generation']<=before['star']['generation'] or after['components']['relay']['generation']<=before['relay']['generation']:raise RuntimeError('COHORT_NOT_RECREATED')
            result['checks'].append('actual_'+component+'_sigkill_cohort_recovers_native_pid_preserved')
        os.kill(native_pid,signal.SIGKILL)
        wait(lambda:running() and status()['pid']!=native_pid,180,'NATIVE_COHORT_RECOVERY_TIMEOUT')
        result['checks'].append('actual_native_sigkill_restarts_all_components')
        end=time.monotonic()+a.seconds
        while time.monotonic()<end:
            if not running():raise RuntimeError('OFFLINE_COHORT_UNHEALTHY')
            time.sleep(.5)
        command('stop');wait(lambda:status()['state']=='STOPPED',115,'STOP_TIMEOUT')
        stopped=status()
        if any(x['alive'] or x['last_exit']!=0 for x in stopped['components'].values()):raise RuntimeError('ORDERLY_COMPONENT_EXIT_NOT_ZERO')
        command('start');wait(running,180,'OPERATOR_START_TIMEOUT')
        if Path('/data/sqlite/ghost.dbs').stat().st_ino!=database_inode:raise RuntimeError('DATABASE_REPLACED')
        result['checks'].append('actual_operator_stop_start_preserves_sqlite')
        command('quit')
        if child.wait(timeout=115)!=0:raise RuntimeError('SUPERVISOR_NONZERO_EXIT')
        end=time.monotonic()+10
        while any(row['state']=='Z' for row in snapshot()['processes']) and time.monotonic()<end:time.sleep(.25)
        sample=snapshot()
        if sample['game_engine_observed'] or sample['monitor_error']:raise RuntimeError('ENGINE_OR_MONITOR_FAILURE')
        if any(row['state']=='Z' or row['name'].lower().startswith(('wine','launcher','qtwebengine','xvfb','ghost')) for row in sample['processes']):raise RuntimeError('PROCESS_LEFTOVER')
        result['checks'].extend(['no_authentication_without_real_realm','no_engine_execution','no_process_or_zombie_leftovers'])
        result['result']='PASS'
    except Exception as error:result['reason']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:child.wait(timeout=115)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if child and child.stdin:child.stdin.close()
        log.close();result['elapsed_seconds']=round(time.monotonic()-started,3)
        atomic_json('/data/state/cohort-probe-result.json',result);print(json.dumps(result),flush=True)
    return 0 if result['result']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
