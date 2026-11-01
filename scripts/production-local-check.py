#!/usr/bin/env python3
"""Local packaged acceptance: offline by default, explicit bounded online mode."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def pinned_image(value):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',value):
        raise argparse.ArgumentTypeError('an exact local sha256 image ID is required')
    return value


def real_path(value):
    path=Path(value)
    if '..' in path.parts or any(ord(c)<32 or ord(c)==127 or c==',' for c in str(path)):
        raise ValueError('INPUT_PATH_REJECTED')
    path=path.absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('INPUT_SYMLINK_REJECTED')
    return path


def map_basename(value):
    if (not isinstance(value,str) or '..' in value
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. ()+-]{0,119}\.(?:w3x|w3m)',value,re.I)):
        raise ValueError('MAP_BASENAME_REJECTED')
    return value


def unique_fields(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('DUPLICATE_PRESET_FIELD')
        result[key]=value
    return result


def map_inputs(args):
    """Select narrow RO inputs; the pinned runtime remains the AI/schema authority."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',args.map_preset):
        raise ValueError('MAP_PRESET_ID_REJECTED')
    if args.map_preset!='manual' and (args.map is None or args.presets is None):
        raise ValueError('NONMANUAL_PRESET_REQUIRES_EXPLICIT_MAP_AND_PRESETS')
    if args.presets is not None and args.map is None:
        raise ValueError('PRESETS_REQUIRE_EXPLICIT_MAP')
    game=ROOT/'Warcraft 3 Frozen Throne'
    selected=real_path(args.map if args.map is not None else game/'maps/download/iCCup DotA 501.w3x')
    info=selected.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=4*1024**3:
        raise ValueError('MAP_FILE_REJECTED')
    basename=map_basename(selected.name)
    local_name=basename if args.map is not None else 'selected.w3x'
    presets=None
    if args.presets is not None:
        source=real_path(args.presets)
        info=source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=65536:
            raise ValueError('PRESETS_FILE_REJECTED')
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as stream:
            presets=stream.read(65537)
        if len(presets)!=info.st_size:raise ValueError('PRESETS_FILE_CHANGED')
        value=json.loads(presets,object_pairs_hook=unique_fields,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('PRESET_NUMBER_REJECTED')))
        if (not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=1
                or not isinstance(value.get('presets'),list) or not 1<=len(value['presets'])<=32):
            raise ValueError('PRESETS_STRUCTURE_REJECTED')
        entries={}
        for entry in value['presets']:
            if (not isinstance(entry,dict) or not isinstance(entry.get('id'),str)
                    or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',entry['id'])
                    or entry['id'] in entries or type(entry.get('enabled')) is not bool):
                raise ValueError('PRESET_ENTRY_REJECTED')
            entries[entry['id']]=entry
        entry=entries.get(args.map_preset)
        if not entry or entry['enabled'] is not True:raise ValueError('PRESET_UNAVAILABLE')
        if args.map_preset!='manual' and map_basename(entry.get('map_filename'))!=basename:
            raise ValueError('PRESET_MAP_FILENAME_MISMATCH')
    return selected,local_name,basename,presets


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True,type=pinned_image);p.add_argument('--run-dir',required=True,type=Path);p.add_argument('--seconds',type=int,default=60)
    p.add_argument('--online',action='store_true');p.add_argument('--profile',type=Path);p.add_argument('--crash-star',action='store_true');p.add_argument('--cohort-offline',action='store_true')
    p.add_argument('--exercise-rehost',action='store_true',help='explicit online synthetic W3GS solo-AI/rehost regression')
    p.add_argument('--map',type=Path,help='explicit map file; non-manual preset filename must match')
    p.add_argument('--presets',type=Path,help='explicit presets JSON copied into the RO config mount')
    p.add_argument('--map-preset',default='manual',help='preset ID; non-manual requires --map and --presets')
    a=p.parse_args(argv)
    if os.geteuid()==0 or not re.fullmatch('sha256:[0-9a-f]{64}',a.image) or not 5<=a.seconds<=3600:raise ValueError('PIN_OWNER_OR_DURATION_REJECTED')
    if a.online!=(a.profile is not None) or a.online and a.seconds<120:raise ValueError('EXPLICIT_ONLINE_PROFILE_AND_DURATION_REQUIRED')
    if a.online and a.crash_star:raise ValueError('OFFLINE_CRASH_PROBE_ONLY')
    if a.exercise_rehost and (not a.online or a.map_preset!='footman-ultimate-ai'):
        raise ValueError('REHOST_PROBE_REQUIRES_EXPLICIT_ONLINE_FOOTMAN')
    if a.cohort_offline and (a.online or a.crash_star):raise ValueError('SEPARATE_OFFLINE_COHORT_PROBE_REQUIRED')
    profile=real_path(a.profile) if a.profile else None
    if profile and (profile.is_symlink() or not profile.is_dir()):raise ValueError('PROFILE_PATH_REJECTED')
    selected_map,map_local,map_client,presets=map_inputs(a)
    root=real_path(a.run_dir)
    if root.exists():raise ValueError('NEW_RUN_DIRECTORY_REQUIRED')
    root.mkdir(mode=0o700,parents=True)
    for name in ('config','config/maps','state','sqlite','logs','replays','prefix','session','profile','game'):(root/name).mkdir(mode=0o700)
    def write(name,value):
        path=root/name
        if isinstance(value,bytes):path.write_bytes(value)
        else:path.write_text(value)
        path.chmod(0o600)
    cfg=json.loads((ROOT/'config/supervisor-iccup.example.json').read_text());cfg['mode']='iccup' if a.online or a.cohort_offline else 'offline'
    cfg['gameplay']['game_name']='CI-P3-'+root.name[-15:] if a.online else 'Offline QA';cfg['hostbot']['lobby_timeout']=7200
    if a.map_preset!='manual' or 'map_preset' in cfg['gameplay']:cfg['gameplay']['map_preset']=a.map_preset
    if not a.online and not a.cohort_offline:cfg['recovery'].update(backoff_initial=.2,backoff_max=1)
    if a.cohort_offline:cfg['iccup']['auth_timeout']=600
    write('config/supervisor.json',json.dumps(cfg))
    if presets is not None:write('config/presets.json',presets)
    write('config/ghost.cfg',(ROOT/'docker/ghost.cfg').read_text().replace('bot_lobbytimelimit = 10','bot_lobbytimelimit = 120'))
    write('config/maps/selected.cfg','map_path = Maps\\Download\\'+map_client+'\nmap_localpath = '+map_local+'\nmap_speed = 3\nmap_visibility = 4\nmap_observers = 1\nmap_flags = 3\nmap_loadInGame = 0\n')
    user=json.dumps('@ByteArray('+json.dumps({'Offline QA':'127.0.0.1:16112'})+')')
    write('profile/launcher.ini','[General]\nAutoLogin=false\nUsername="OfflineOnly"\nPassword=\nListLogins=\nConnections=\nCurrConnections=0\nSiteConnections='+user+'\nUserConnections='+user+'\n')
    write('profile/iccup.ini','[Common]\nAutoUpdate=0\nRunAtStart=0\n[Warcraft III]\nAutoLoginOn=0\n')
    worker={k:cfg['iccup'][k] for k in ('vendor','profile','redist','game_metadata','prefix','session','manifest','quit_helper','relay_port')}
    worker.update(version=1,status='/data/state/star-status.json',startup_timeout=180,generation=1)
    write('config/star-worker.json',json.dumps(worker))
    game=ROOT/'Warcraft 3 Frozen Throne'
    # Only regular-file metadata PEs are mounted; no original directory or engine launch.
    for name in ('war3.exe','game.dll'):shutil.copyfile(game/name,root/'game'/name)
    probe='production_cohort_probe.py' if a.cohort_offline else 'production_online_probe.py' if a.online else 'production_probe.py'
    shutil.copyfile(ROOT/'tests/phase3'/probe,root/'probe.py')
    suffix=re.sub('[^a-z0-9-]','',root.name.lower())[-24:]
    docker=['docker','--host','unix:///var/run/docker.sock'];name=('war3-phase3-online-' if a.online else 'war3-phase3-offline-')+suffix;network='bridge' if a.online else 'none'
    cmd=[*docker,'create','--pull=never','--name',name,'--network',network,'--read-only','--user',f'{os.geteuid()}:{os.getegid()}','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','2g','--memory-swap','2g','--cpus','2','--pids-limit','256','--ulimit','core=0:0','--init','--stop-timeout','120','--log-opt','max-size=10m','--log-opt','max-file=3','--tmpfs','/tmp:rw,nosuid,nodev,size=256m,mode=1777','--shm-size','128m']
    mounts=[(root/name,'/data/'+name,name in ('config',)) for name in ('config','state','sqlite','logs','replays','prefix','session')]
    mounts += [(profile or root/'profile','/vendor/profile',True),(root/'game','/vendor/game',True),(ROOT/'artifacts/phase2/star/updated-input','/vendor/star',True),(ROOT/'artifacts/phase2/star/redist/app/Launcher/vc_redist.x86.exe','/vendor/redist/vc_redist.x86.exe',True),(game/'War3Patch.mpq','/data/warcraft/War3Patch.mpq',True),(selected_map,'/data/maps/'+map_local,True),(root/'probe.py','/probe.py',True)]
    if a.exercise_rehost:
        shutil.copyfile(ROOT/'tests/phase3/production_lifecycle.py',root/'production_lifecycle.py')
        mounts.append((root/'production_lifecycle.py','/production_lifecycle.py',True))
    for source,destination,ro in mounts:cmd.extend(['--mount',f'type=bind,src={source},dst={destination}'+(',readonly' if ro else '')])
    cmd.extend([a.image,'python3','/probe.py','--seconds',str(a.seconds)])
    if a.crash_star:cmd.append('--crash-star')
    if a.exercise_rehost:cmd.append('--exercise-rehost')
    env=os.environ.copy()
    for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(k,None)
    subprocess.run(cmd,env=env,check=True,capture_output=True,timeout=30)
    def inspect():return json.loads(subprocess.check_output([*docker,'inspect',name],env=env,timeout=20))[0]
    def evidence(name,value):write(name,json.dumps(value,indent=2)+'\n')
    meta=inspect();h=meta['HostConfig'];bindings={m['Destination']:m for m in meta['Mounts'] if m['Type']=='bind'}
    isolated=(meta['Image']==a.image and h['NetworkMode']==network and h['ReadonlyRootfs'] and h['CapDrop']==['ALL'] and not h['Privileged'] and not h.get('PortBindings') and h['Init'] and not h.get('PidMode') and meta['Config']['User']==f'{os.geteuid()}:{os.getegid()}')
    isolated=isolated and all(bindings[d]['RW']==(not ro) for _,d,ro in mounts)
    evidence('isolation.json',{'result':'PASS' if isolated else 'FAIL','image_id':meta['Image'],'network':network,'read_only_root':h['ReadonlyRootfs'],'non_root':os.geteuid()!=0,'cap_drop_all':h['CapDrop']==['ALL'],'no_new_privileges':h['SecurityOpt'],'published_ports':h.get('PortBindings'),'memory_limit_bytes':h['Memory'],'pids_limit':h['PidsLimit'],'canonical_credentials_mounted':False,'synthetic_profile_only':not a.online,'bind_destinations':sorted(bindings)})
    def interrupted(*_):raise KeyboardInterrupt
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,interrupted)
    code=1
    try:
        if not isolated:raise ValueError('ISOLATION_REJECTED')
        code=subprocess.run([*docker,'start','-a',name],env=env,timeout=a.seconds+1100 if a.exercise_rehost else a.seconds+700).returncode
    finally:
        state=inspect()['State']
        if state['Running']:
            subprocess.run([*docker,'stop','--time','120',name],env=env,capture_output=True,timeout=130,check=True)
            state=inspect()['State']
        evidence('container-state.json',{k:state[k] for k in ('Running','ExitCode','OOMKilled','StartedAt','FinishedAt')})
        if not state['Running']:subprocess.run([*docker,'rm',name],env=env,capture_output=True,check=True,timeout=20)
    return code


if __name__=='__main__':raise SystemExit(main())
