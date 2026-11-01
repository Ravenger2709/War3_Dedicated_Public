#!/usr/bin/env python3
"""Bounded, credential-free observation. Raw logs stay private, stdout is counts only."""
import argparse
import collections
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time

stop = False


def request_stop(_sig, _frame):
    global stop
    stop = True


def processes():
    result = []
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            stat = (entry / 'stat').read_text()
            name = stat[stat.index('(')+1:stat.rindex(')')]
            fields = stat[stat.rindex(')')+2:].split()
            result.append({'pid': int(entry.name), 'ppid': int(fields[1]), 'name': name})
        except (OSError, ValueError, IndexError):
            continue
    return result


def sockets(procs):
    owners = collections.defaultdict(list)
    for proc in procs:
        for fd in Path(f"/proc/{proc['pid']}/fd").glob('*'):
            try:
                link = os.readlink(fd)
                if link.startswith('socket:['):
                    owners[link[8:-1]].append({'pid': proc['pid'], 'name': proc['name']})
            except OSError:
                pass
    result = []
    for kind in ('tcp', 'tcp6', 'udp', 'udp6'):
        for row in Path('/proc/net', kind).read_text().splitlines()[1:]:
            fields = row.split()
            def endpoint(text):
                addr, port = text.split(':')
                return {'address': 'loopback' if addr in ('0100007F', '00000000000000000000000001000000') else ('any' if int(addr,16)==0 else 'redacted'), 'port': int(port,16)}
            result.append({'kind': kind, 'local': endpoint(fields[1]), 'remote': endpoint(fields[2]), 'state_hex': fields[3], 'owners': owners.get(fields[9], [])})
    for row in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = row.split()
        path = fields[7] if len(fields)>7 else ''
        category = 'x11' if '.X11-unix' in path else ('wine-ipc' if '/wine-' in path else ('anonymous' if not path else 'redacted'))
        result.append({'kind':'unix','state_hex':fields[5],'path_kind':category,'owners':owners.get(fields[6],[])})
    return result


def snapshot():
    procs = processes()
    modules = set()
    by_process = []
    for proc in procs:
        try:
            local = set()
            for line in Path(f"/proc/{proc['pid']}/maps").read_text().splitlines():
                path = line.split()[-1]
                if path.lower().endswith(('.dll', '.icc', '.exe')):
                    local.add(Path(path).name.lower())
            modules.update(local)
            if local:
                by_process.append({'pid':proc['pid'],'name':proc['name'],'modules':sorted(local)})
        except OSError:
            pass
    forbidden = any(p['name'].lower() in ('war3.exe','warcraft iii.exe') for p in procs) or bool({'game.dll','war3.exe'} & modules)
    return {'processes': procs, 'sockets': sockets(procs), 'modules': sorted(modules), 'modules_by_process':by_process, 'forbidden_game': forbidden}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('launcher','load-only'), default='launcher')
    parser.add_argument('--target', choices=('iccwc3.icc','Reconnect.dll','iccscbn.icc'), default='iccwc3.icc')
    parser.add_argument('--label', default='cold')
    parser.add_argument('--seconds', type=int, default=35)
    parser.add_argument('--network-policy', choices=('none', 'bridge'), default='none')
    parser.add_argument('--vc-runtime', choices=('builtin','native2015'), default='builtin')
    parser.add_argument('--game-metadata', action='store_true')
    parser.add_argument('--synthetic-autologin', action='store_true')
    parser.add_argument('--connection-defaults', action='store_true')
    parser.add_argument('--auth-profile', action='store_true')
    parser.add_argument('--launcher-arg', action='append', choices=('--help', '--version'), default=[])
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9-]{1,48}', args.label) or not 5 <= args.seconds <= 90:
        parser.error('invalid bounded experiment')
    if args.synthetic_autologin and (args.network_policy != 'none' or not args.game_metadata):
        parser.error('synthetic autologin requires network=none and metadata fixture')
    if args.auth_profile and (args.synthetic_autologin or not args.game_metadata or args.mode!='launcher'):
        parser.error('auth probe requires the bounded original launcher/metadata path')
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    label = args.label
    suffix = 1
    while Path('/evidence', label).exists():
        suffix += 1
        label = f'{args.label}-{suffix}'
    out = Path('/evidence', label)
    out.mkdir(parents=True, exist_ok=False)
    work = Path('/work/launcher')
    if not work.exists():
        shutil.copytree('/input', work)
    for forbidden in ('war3.exe','game.dll','iccup.ini','Launcher.log','__log.txt'):
        if Path('/input',forbidden).exists():
            raise RuntimeError('unsafe input')
    if args.game_metadata:
        if not all(Path('/work/game', name).is_file() for name in ('war3.exe','game.dll')):
            raise RuntimeError('missing read-only game metadata inputs')
        # MainExe is a recovered QSettings key from the classic Launcher.
        # Test its applicability to Star without starting or replacing the game.
        (work/'iccup.ini').write_text('[Warcraft III]\nMainExe=Z:/work/game/war3.exe\n')
    if args.synthetic_autologin:
        if any(line.split()[1]=='00000000' for line in Path('/proc/net/route').read_text().splitlines()[1:]):
            raise RuntimeError('synthetic login refuses routed network')
        (work/'iccup.ini').write_text('[Common]\nAutoUpdate=0\nAutoUpdateMap=0\n[Warcraft III]\nMainExe=Z:/work/game/war3.exe\nAutoLoginOn=1\n')
        saved_logins = json.dumps({'War3OfflineProbe':'synthetic-not-a-password'},separators=(',',':'))
        ini_logins = '"'+saved_logins.replace(chr(92),chr(92)*2).replace('"',chr(92)+'"')+'"'
        (work/'launcher.ini').write_text('[General]\nAutoLogin=true\nUsername=War3OfflineProbe\nPassword=synthetic-not-a-password\nListLogins='+ini_logins+'\n')
    if args.connection_defaults:
        path=Path('/server-defaults.ini')
        if path.stat().st_size>8192: raise RuntimeError('oversized server defaults')
        cfg=configparser.RawConfigParser();cfg.optionxform=str;cfg.read(path,encoding='utf-8')
        if cfg.sections()!=['General'] or set(cfg['General'])!={'Connections','CurrConnections','SiteConnections'}:
            raise RuntimeError('server defaults keys rejected')
        config_path=work/'launcher.ini'
        current=config_path.read_text() if config_path.exists() else '[General]\n'
        config_path.write_text(current+''.join(key+'='+value+'\n' for key,value in cfg['General'].items()))
    if args.auth_profile:
        if hashlib.sha256((work/'Launcher.exe').read_bytes()).hexdigest()!='9ce99d541aaef4cb1b2a60a0837580678f81ed0494795efc720fe84c99a6253e':
            raise RuntimeError('unsupported auth launcher version')
        for name in ('launcher.ini','iccup.ini'):
            source=Path('/auth-profile',name);info=source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:
                raise RuntimeError('unsafe auth profile')
            shutil.copyfile(source,work/name);(work/name).chmod(0o600)
    # Prefix and runtime are writable only to this user; source mount is read-only.
    xvfb_log = open(out/'xvfb.private.log','w')
    xvfb = subprocess.Popen(['Xvfb',':99','-screen','0','1280x1024x24','-nolisten','tcp'],stdout=xvfb_log,stderr=subprocess.STDOUT)
    initialized = False
    prefix_was_present = Path(os.environ['WINEPREFIX'], 'system.reg').exists()
    startup = time.monotonic()
    try:
        # Prevent optional Mono/Gecko installer dialogs during offline prefix
        # creation. Launcher uses native QtWebKit; its own DLL order is unchanged.
        env = dict(os.environ, WINEDEBUG='-all' if args.auth_profile else 'err+all,warn+all,+timestamp,+pid,+process',
                   WINEDLLOVERRIDES=os.environ['WINEDLLOVERRIDES']+';mscoree,mshtml=d')
        with open(out/'prefix.private.log','w') as log:
            boot = subprocess.Popen(['wineboot','--init' if not prefix_was_present else '--restart'],env=env,stdout=log,stderr=subprocess.STDOUT)
            bootstrap = []
            deadline = time.monotonic()+75
            while boot.poll() is None and time.monotonic()<deadline and not stop:
                bootstrap.append(snapshot())
                time.sleep(1)
            (out/'bootstrap.json').write_text(json.dumps(bootstrap,indent=2)+'\n')
            if boot.poll() is None:
                (out/'failure.json').write_text(json.dumps({'stage':'prefix-init','reason':'timeout' if not stop else 'signal','prefix_arch':os.environ['WINEARCH'],'seconds':75})+'\n')
                boot.kill()
                boot.wait(timeout=5)
                return 3
            if boot.returncode:
                (out/'failure.json').write_text(json.dumps({'stage':'prefix-init','reason':'exit','exit_code':boot.returncode,'prefix_arch':os.environ['WINEARCH']})+'\n')
                return 3
        initialized = True
        with open(out/'debugger-setup.private.log','w') as log:
            subprocess.run(['wine','reg','add',r'HKCU\Software\Wine\WineDbg','/v','ShowCrashDialog','/t','REG_DWORD','/d','0','/f'],
                           stdout=log,stderr=subprocess.STDOUT,timeout=15,check=True)
            win_out = 'Z:'+str(out).replace('/',chr(92))
            if args.auth_profile:
                # Never create a memory dump or backtrace after credentials are
                # provisioned. The original client still performs all auth.
                debugger='cmd /c exit 1'
            else:
                (out/'debugger-commands.txt').write_text('bt\ninfo share\nkill\nquit\n')
                debugger = 'cmd /c winedbg --file '+win_out+r'\debugger-commands.txt %ld %ld >> '+win_out+r'\backtrace.private.log 2>&1'
            subprocess.run(['wine','reg','add',r'HKLM\Software\Microsoft\Windows NT\CurrentVersion\AeDebug','/v','Debugger','/t','REG_SZ','/d',debugger,'/f'],
                           stdout=log,stderr=subprocess.STDOUT,timeout=15,check=True)
        vc_result = None
        if args.vc_runtime == 'native2015':
            redist = Path('/redist/vc_redist.x86.exe')
            if not redist.is_file() or hashlib.sha256(redist.read_bytes()).hexdigest() != 'fdd1e1f0dcae2d0aa0720895eff33b927d13076e64464bb7c7e5843b7667cd14':
                raise RuntimeError('unexpected VC runtime input')
            with open(out/'vc-install.private.log','w') as log:
                setup_log = 'Z:'+str(out/'vc-setup.private.log').replace('/',chr(92))
                vc = subprocess.run(['wine',str(redist),'/install','/quiet','/norestart','/log',setup_log],
                                    stdout=log,stderr=subprocess.STDOUT,timeout=120)
            vc_result = vc.returncode
            if vc.returncode not in (0, 3010):
                (out/'failure.json').write_text(json.dumps({'stage':'vc-runtime-install','exit_code':vc.returncode})+'\n')
                raise RuntimeError('VC runtime installation failed')
        baseline = snapshot()
        argv = ['wine','Launcher.exe',*args.launcher_arg] if args.mode=='launcher' else ['wine','/opt/wine-research/load-only.exe',args.target]
        env = dict(os.environ, WINEDEBUG='-all' if args.auth_profile else '+timestamp,+pid,+loaddll,+seh,+winsock,+process')
        if args.vc_runtime == 'native2015':
            env['WINEDLLOVERRIDES'] = env['WINEDLLOVERRIDES']+';vcruntime140,msvcp140,concrt140=n,b'
        raw = open(out/'wine.private.log','w')
        if args.auth_profile:
            (out/'syscalls.private.log').touch(mode=0o600)
            child = subprocess.Popen(argv,cwd=work,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        else:
            child = subprocess.Popen(['strace','-f','-qq','-s','0','-e','trace=network,process','-o',str(out/'syscalls.private.log'),*argv],cwd=work,env=env,stdout=raw,stderr=subprocess.STDOUT,start_new_session=True)
        samples = []
        start = time.monotonic()
        while not stop and time.monotonic()-start < args.seconds:
            sample = snapshot()
            sample['t_seconds'] = round(time.monotonic()-start,3)
            samples.append(sample)
            if args.auth_profile and len(samples) in (15,30,45,60):
                subprocess.run(['python3','/opt/wine-research/capture.py',str(out/f'auth-{len(samples)}.private.png')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            if sample['forbidden_game']:
                break
            time.sleep(1)
        before_stop = snapshot()
        # Bounded stop of our prefix only. Wineserver kills clients belonging to it.
        subprocess.run(['wineserver','-k'],env=env,stdout=raw,stderr=subprocess.STDOUT,timeout=10)
        try:
            child.wait(timeout=12)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=5)
        raw.close()
        after_stop = snapshot()
        wine = (out/'wine.private.log').read_text(errors='replace')
        trace = (out/'syscalls.private.log').read_text(errors='replace')
        loaded = sorted(set(re.findall(r'Loaded L"[^"\n]*[\\/]([^"\\/]+\.(?:dll|icc|exe))"',wine,re.I)))
        errors = collections.Counter(re.findall(r'\b(?:err|warn):([a-z0-9_]+):([A-Za-z0-9_]+)',wine))
        report = {'label':label,'mode':args.mode,'target': args.target if args.mode=='load-only' else 'Launcher.exe',
                  'wine_version':Path('/opt/wine-research/version.txt').read_text().strip(), 'prefix_arch':os.environ['WINEARCH'],
                  'prefix_initialized':initialized,'prefix_was_present':prefix_was_present,'duration_seconds':round(time.monotonic()-startup,3),
                  'child_exit':child.returncode,'network_policy':args.network_policy,'samples':samples,
                  'launcher_arguments':args.launcher_arg,
                  'vc_runtime':args.vc_runtime,'vc_install_exit':vc_result,
                  'game_metadata_only':args.game_metadata,
                  'synthetic_credentials_only':args.synthetic_autologin,
                  'auth_profile_provisioned':args.auth_profile,'sensitive_diagnostics_disabled':args.auth_profile,
                  'qt_runtime_options':{key:os.environ.get(key) for key in ('QT_OPENGL','QTWEBENGINE_CHROMIUM_FLAGS','QTWEBENGINE_DISABLE_SANDBOX','LIBGL_ALWAYS_SOFTWARE')},
                  'baseline':baseline,'before_stop':before_stop,'after_stop':after_stop,'loaded_modules_trace':loaded,
                  'diagnostic_counts':{':'.join(k):v for k,v in errors.items()},
                  'unhandled_page_faults':wine.count('Unhandled page fault'),
                  'missing_dlls':sorted(set(re.findall(r'Library ([A-Za-z0-9_.-]+) .*not found',wine))),
                  'exception_codes':dict(collections.Counter(re.findall(r'code=([0-9a-f]{8})',wine))),
                  'syscall_counts':{name:len(re.findall(r'\b'+name+r'\(',trace)) for name in ('socket','bind','listen','connect','accept','execve','clone')},
                  'socket_domains':dict(collections.Counter(re.findall(r'\bsocket\((AF_[A-Z0-9_]+)',trace))),
                  'connect_results':dict(collections.Counter(re.findall(r'\bconnect\([^\n]*?\)\s*=\s*(-?\d+(?: E[A-Z0-9_]+)?)',trace))),
                  'instrumentation_errors':{token:trace.count(token) for token in ('Operation not permitted','ptrace failed') if token in trace},
                  'load_only_results':re.findall(r'^(?:LOAD_ONLY|LOAD_FAILED|LOAD_OK|EXPORT)[^\r\n]*',wine,re.M),
                  'forbidden_game_observed':any(s['forbidden_game'] for s in samples),
                  'raw_log_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.private.log')}}
        (out/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k not in ('samples','baseline','before_stop','after_stop','raw_log_sha256')}))
        return 2 if report['forbidden_game_observed'] else 0
    finally:
        subprocess.run(['wineserver','-k'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
            xvfb.wait()
        xvfb_log.close()


if __name__ == '__main__':
    raise SystemExit(main())
