"""Recover AMP's foreground attachment without restarting a healthy runtime."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess


class AttachmentError(ValueError):
    @property
    def public_message(self):
        return {
            'OWNER_BUSY': 'Another AMP launcher already owns this runtime. No container was changed.',
            'CONTAINER_UNVERIFIED': 'An existing War3 container could not be verified. It was preserved; inspect its ownership, image and mounts.',
            'RUNTIME_NOT_READY': 'The existing War3 runtime is not healthy. It was preserved; inspect its status before stopping or repairing it.',
        }.get(self.args[0] if self.args else '', 'Runtime attachment rejected; inspect the private instance and Docker access.')


def require(ok, code='CONTAINER_UNVERIFIED'):
    if not ok:
        raise AttachmentError(code)


@contextmanager
def owner(instance):
    """Hold one launcher lease across exec; SIGKILL releases the kernel lock."""
    control = Path(instance).absolute() / 'control'
    require(not any(p.is_symlink() for p in (control, *control.parents)))
    info = control.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700)
    fd = os.open(control / '.amp-runtime-owner.lock',
                 os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AttachmentError('OWNER_BUSY') from None
        yield fd
    finally:
        os.close(fd)


# Read the live loopback service inside the exact existing container, not a
# possibly stale status.json on the host. Only fixed scalar telemetry escapes.
HEALTH_SCRIPT = r'''import json,urllib.request,re
try:
 with urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=3) as response:
  raw=response.read(65537)
 if len(raw)>65536:raise ValueError()
 s=json.loads(raw);g=s.get('game',{});components=s.get('components',{})
 good=(s.get('ready') is True and s.get('mode')=='iccup'
       and s.get('state') in ('LOBBY','COUNTDOWN','IN_GAME')
       and s.get('child_alive') is True and s.get('iccup_authenticated') is True
       and s.get('native_healthy') is True and s.get('relay_healthy') is True
       and s.get('star_process_alive') is True and s.get('game_engine_observed') is False
       and type(s.get('pid')) is int and s['pid']>0
       and set(components)=={'native','relay','star'}
       and all(c.get('alive') is True and type(c.get('pid')) is int and c['pid']>0 for c in components.values())
       and components['native']['pid']==s['pid']
       and type(g.get('session_generation')) is str
       and re.fullmatch('[0-9a-f]{32}',g['session_generation']) is not None)
 print(json.dumps({'healthy':bool(good),'phase':s.get('state') if good else None}))
except Exception:print(json.dumps({'healthy':False,'phase':None}))
'''


def verified(meta, spec, instance, production, identity):
    """Bind adoption to this deployment, not merely a familiar container name."""
    require(type(meta) is dict and meta.get('Id') == identity
            and meta.get('Name') == '/' + spec['container_name']
            and meta.get('Image') == spec['image_id'])
    config, host, state = meta.get('Config', {}), meta.get('HostConfig', {}), meta.get('State', {})
    require(config.get('User') == f'{spec["uid"]}:{spec["gid"]}'
            and config.get('Labels', {}).get('war3.managed') == 'phase3-staging'
            and not any(k.startswith('war3.standalone') for k in config.get('Labels', {}))
            and host.get('ReadonlyRootfs') is True and host.get('Privileged') is False
            and host.get('RestartPolicy', {}).get('Name') == 'no'
            and host.get('NetworkMode') == 'bridge' and host.get('Init') is True
            and host.get('CapDrop') == ['ALL']
            and 'no-new-privileges:true' in host.get('SecurityOpt', [])
            and host.get('PortBindings') == {'6113/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '6113'}]}
            and state.get('Running') is True and state.get('Status') == 'running'
            and state.get('Paused') is False and state.get('Restarting') is False)
    root = Path(spec.get('host_instance_dir', str(Path(instance).absolute())))
    expected = {(str(root / 'data' / n), '/data/' + n, n in production.RW)
                for n in (*production.RO, *production.RW)}
    expected |= {(str(root / 'vendor' / n), '/vendor/' + n, False) for n in production.VENDOR}
    mounts = meta.get('Mounts', [])
    binds = [m for m in mounts if m.get('Type') == 'bind']
    require(len(binds) == len(expected)
            and {(m.get('Source'), m.get('Destination'), m.get('RW')) for m in binds} == expected
            and all(m.get('Type') == 'bind' or m.get('Type') == 'tmpfs'
                    and m.get('Destination') == '/tmp' for m in mounts))


def candidate(instance, spec, production):
    output = production.docker_output(['ps', '-aq', '--no-trunc', '--filter',
                                       'name=^/' + spec['container_name'] + '$'])
    if not output.strip():
        return False
    identity = output.decode('ascii').strip()
    require(re.fullmatch('[0-9a-f]{64}', identity) is not None)
    require((spec['uid'], spec['gid']) == (os.geteuid(), os.getegid()))
    accepted = production.read_json(Path(instance) / 'acceptance.json')
    source = production.gate(accepted, spec['image_id'])
    image = production.docker_json(['image', 'inspect', spec['image_id']])[0]
    require(image.get('Id') == spec['image_id'] and image.get('Os') == 'linux'
            and image.get('Architecture') == 'amd64'
            and image.get('Config', {}).get('Labels', {}).get('war3.production.source-digest') == source)
    verified(production.docker_json(['container', 'inspect', identity])[0], spec, instance, production, identity)
    try:
        result = subprocess.run([*production.DOCKER, 'exec', identity, 'python3', '-I', '-B', '-c', HEALTH_SCRIPT],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=True, timeout=8, env=production.environment())
        require(len(result.stdout) <= 1024, 'RUNTIME_NOT_READY')
        health = json.loads(result.stdout)
        require(type(health) is dict and set(health) == {'healthy', 'phase'}
                and health['healthy'] is True and health['phase'] in ('LOBBY', 'COUNTDOWN', 'IN_GAME'),
                'RUNTIME_NOT_READY')
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AttachmentError('RUNTIME_NOT_READY') from None
    # Recheck the immutable ID after health; never attach by a name that can be reused.
    verified(production.docker_json(['container', 'inspect', identity])[0], spec, instance, production, identity)
    return identity, health['phase']


def reattach(instance, spec, production):
    selected = candidate(instance, spec, production)
    if selected is False:
        return False
    identity, phase = selected
    print('INFO supervisor: Reattached to the existing runtime. Lobby and active matches were preserved.', flush=True)
    print('INFO supervisor: ' + {'LOBBY': 'Lobby is ready.', 'COUNTDOWN': 'Countdown is running.',
                                'IN_GAME': 'Game is running.'}[phase], flush=True)
    production.os.execvpe(production.DOCKER[0], [*production.DOCKER, 'attach', '--sig-proxy=true', identity], production.environment())
    return True  # Only reached by an exec mock in tests.


def launch(production, arguments):
    if len(arguments) != 2 or arguments[0] != '--instance-dir':
        return production.cli(['run', *arguments])
    instance = Path(arguments[1]).absolute()
    try:
        spec = production.deployment(instance)
        with owner(instance) as fd:
            os.set_inheritable(fd, True)
            if reattach(instance, spec, production):
                return 0
            return production.cli(['run', *arguments])
    except AttachmentError as error:
        print(error.public_message, flush=True)
        return 2
    except Exception:
        print('Runtime attachment rejected; inspect the private instance and Docker access.', flush=True)
        return 2
