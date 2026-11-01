#!/usr/bin/env python3
"""Stage reviewed text sources and run production-controller regressions offline."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
IMAGE='sha256:8ce6301f9aa4793e2bffb69bec6e1ffc8799a85b2e11253b4110821d3e1cafe0'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path);p.add_argument('--one',action='store_true');a=p.parse_args()
    sources=[]
    for tree in ('src/runtime','src/supervisor','src/bridge','src/connector'):
        sources.extend(path.relative_to(ROOT) for path in (ROOT/tree).rglob('*.py'))
    sources += [Path(s) for s in ('tests/phase3/runtime_component.py','tests/phase3/test_runtime.py','tests/phase3/test_star_worker.py','tests/unit/test_supervisor.py','config/star-vendor-files.json')]
    env=os.environ.copy()
    for k in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):env.pop(k,None)
    with tempfile.TemporaryDirectory(prefix='war3-runtime-test-') as temporary:
        stage=Path(temporary)
        fixtures=stage/'fixtures';fixtures.mkdir()
        for name in ('war3.exe','game.dll'):(fixtures/name).write_text('SYNTHETIC_NOT_PE metadata')
        hashes={}
        for name in sources:
            source=ROOT/name;data=source.read_bytes()
            if source.is_symlink() or len(data)>256*1024 or b'\0' in data:raise ValueError('TEST_SOURCE_REJECTED')
            if name.suffix=='.py':ast.parse(data,filename=str(name))
            else:json.loads(data)
            target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            hashes[str(name)]=hashlib.sha256(data).hexdigest()
        code="import unittest; s=unittest.TestSuite(); [s.addTests(unittest.TestLoader().discover(p)) for p in ('tests/phase3','tests/unit')]; r=unittest.TextTestRunner(verbosity=2).run(s); raise SystemExit(not r.wasSuccessful())"
        if a.one:code="import sys,unittest; sys.path.insert(0,'tests/phase3'); s=unittest.TestLoader().loadTestsFromName('test_runtime.RuntimeTests.test_normal_readiness_and_shutdown_preserve_native_data'); r=unittest.TextTestRunner(verbosity=2).run(s); raise SystemExit(not r.wasSuccessful())"
        cmd=['docker','--host','unix:///var/run/docker.sock','run','--rm','--pull','never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user',f'{os.geteuid()}:{os.getegid()}','--cpus','2','--memory','512m','--pids-limit','96','--ulimit','core=0:0','--init','--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--mount',f'type=bind,src={stage},dst=/repo,readonly','--workdir','/repo','--env','PYTHONPATH=/repo','--env','PYTHONDONTWRITEBYTECODE=1','--entrypoint','python3',IMAGE,'-c',code]
        cmd[cmd.index('--workdir'):cmd.index('--workdir')]=['--mount',f'type=bind,src={fixtures},dst=/fixtures/game,readonly']
        result=subprocess.run(cmd,env=env)
    report={'result':'PASS' if result.returncode==0 else 'FAIL','image_id':IMAGE,'network':'none','synthetic_data_only':True,'source_paths':len(sources),'source_sha256':hashes,'project_root_mounted':False}
    if a.report:a.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));return result.returncode


if __name__=='__main__':raise SystemExit(main())
