#!/usr/bin/env python3
"""CI Docker smoke checks; no AMP instance, registry writes or runtime launch.

Only --host-socket-check mounts the real Docker socket, for read-only version
negotiation by a synthetic AMP executable. All other sockets are synthetic.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
RUNTIME = "sha256:b814ad9c299262a381eaa7bb18bafd0d73d6b314a0ae9394b18f3ef5f1a6b54f"
ENTRYPOINT = ["/usr/bin/tini", "--", "/ampstart.sh"]
HOOK = "/ampstart.d/50-war3-docker-socket.sh"

# Runs inside an ephemeral control container. The stub is synthetic Python,
# never an AMP binary or an engine/vendor/profile input.
FIXTURE = r"""
import json, os, pathlib, socket, subprocess, sys
mode, gid = sys.argv[1], int(sys.argv[2])
primary_gid = 100 if mode == 'duplicate-primary' else 23452
path = '/var/run/docker.sock'
os.makedirs('/AMP', exist_ok=True)
os.makedirs('/var/run', exist_ok=True)
if mode == 'existing':
    subprocess.run(['groupadd', '--gid', str(gid), 'fixture-socket'], check=True)
if mode != 'host':
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(path)
    sock.listen(8)
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
before = os.stat(path)
stub = pathlib.Path('/AMP/AMP_Linux_x86_64')
stub.write_text('''#!/usr/bin/python3
import json, os, pathlib, socket, subprocess, sys
gid = GID
assert os.getuid() == 23451, os.getuid()
assert os.getgid() == PRIMARY, os.getgid()
assert gid in os.getgroups(), os.getgroups()
assert 5 in os.getgroups(), 'upstream tty membership lost'
assert os.access('/var/run/docker.sock', os.R_OK | os.W_OK)
assert not pathlib.Path('/usr/local/bin/dockerd').exists()
subprocess.run(['python3', '--version'], check=True)
subprocess.run(['docker', '--version'], check=True)
if HOST:
    result = json.loads(subprocess.check_output([
        'docker', '--host', 'unix:///var/run/docker.sock', 'version',
        '--format', '{{json .}}'], timeout=20))
    assert result['Client']['Version'] == '29.5.1', result
    assert result['Server']['ApiVersion'], result
    print(json.dumps({'client': result['Client']['Version'],
                     'server': result['Server']['Version'],
                     'api': result['Server']['ApiVersion']}))
else:
    s = socket.socket(socket.AF_UNIX)
    s.connect('/var/run/docker.sock')
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(),
                 'groups': os.getgroups(), 'socket_gid': gid}))
'''.replace('GID', str(gid)).replace('PRIMARY', str(primary_gid)).replace('HOST', repr(mode == 'host')))
stub.chmod(0o755)
env = os.environ.copy()
env.update(AMPUSERID='23451', AMPGROUPID=str(primary_gid), AMP_CONTAINER_DEPS='', TINI_SUBREAPER='1')
subprocess.run(['/usr/bin/tini', '--', '/ampstart.sh'], env=env, check=True, timeout=35)
after = os.stat(path)
assert (before.st_uid, before.st_gid, before.st_mode, before.st_ino) == (
    after.st_uid, after.st_gid, after.st_mode, after.st_ino), 'socket metadata changed'
"""


def call(args, *, check=True):
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"):
        env.pop(key, None)
    result = subprocess.run([*DOCKER, *args], env=env, check=False,
                            capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def inspect(ref):
    return json.loads(call(["image", "inspect", ref]).stdout)[0]


def run(image, command, mounts=(), check=True):
    result = call(["run", "--rm", "--pull=never", "--network", "none",
                   "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
                   "--memory", "256m", "--cpus", "1", "--ulimit", "core=0",
                   *mounts, "--entrypoint", "python3", image, *command], check=check)
    if check:
        print(result.stdout.strip())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="war3-amp-control:phase3-candidate")
    parser.add_argument("--host-socket-check", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    runtime_before = inspect(RUNTIME)
    image = inspect(args.image)
    image_id = image["Id"]
    assert image["Os"] == "linux" and image["Architecture"] == "amd64"
    assert image["Config"]["Entrypoint"] == ENTRYPOINT
    assert not image["Config"].get("Volumes"), "control image must not inject mounts"
    checks = []
    run(image_id, ["-c", "import shutil,subprocess; assert not shutil.which('dockerd'); subprocess.run(['python3','--version'],check=True); subprocess.run(['docker','--version'],check=True)"])
    checks.append("python-cli-no-daemon")
    for mode, gid in (("new", 34567), ("existing", 34568), ("primary", 23452),
                      ("root-group", 0), ("duplicate-primary", 34569)):
        run(image_id, ["-c", FIXTURE, mode, str(gid)])
        checks.append("upstream-handoff-synthetic-" + mode)
    # No real daemon socket is mounted for failure and idempotency checks.
    for case, setup in (("missing", ""), ("regular-file", "touch /var/run/docker.sock;")):
        code = "import subprocess,sys; sys.exit(subprocess.run(['bash','-c', " + repr(setup + " source " + HOOK + "; echo HOOK_SHOULD_NOT_CONTINUE") + "]).returncode)"
        result = run(image_id, ["-c", code], check=False)
        assert result.returncode != 0 and "HOOK_SHOULD_NOT_CONTINUE" not in result.stdout, result
        checks.append("reject-" + case)
    repeat = r'''
import os, socket, subprocess
subprocess.run(['groupadd','--gid','23452','amp'], check=True)
subprocess.run(['useradd','--uid','23451','--gid','amp','amp'], check=True)
s=socket.socket(socket.AF_UNIX); s.bind('/var/run/docker.sock')
os.chown('/var/run/docker.sock',0,34567); os.chmod('/var/run/docker.sock',0o660)
subprocess.run(['bash','-ec','source HOOK; source HOOK'],check=True)
assert subprocess.check_output(['id','-G','amp']).split().count(b'34567') == 1
'''.replace("HOOK", HOOK)
    run(image_id, ["-c", repeat])
    checks.append("hook-idempotent")
    if args.host_socket_check:
        gid = os.stat("/var/run/docker.sock").st_gid
        run(image_id, ["-c", FIXTURE, "host", str(gid)],
            ["--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly"])
        checks.append("host-api-version-as-amp")
    runtime_after = inspect(RUNTIME)
    for key in ("Id", "Config", "RootFS"):
        assert runtime_before[key] == runtime_after[key], "accepted rc4 image changed"
    checks.append("accepted-rc4-image-unchanged")
    receipt = {"result": "PASS", "image_id": image_id, "checks": checks,
               "runtime_image_id": RUNTIME, "network": "none",
               "amp_instance_started": False, "runtime_started": False,
               "host_socket_version_only": args.host_socket_check}
    if args.receipt:
        with args.receipt.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, indent=2, sort_keys=True)
            output.write("\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
