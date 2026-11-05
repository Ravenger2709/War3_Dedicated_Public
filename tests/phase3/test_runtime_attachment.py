"""AMP owner recovery: preserve a healthy survivor, reject foreign containers."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from amp import production
from amp import runtime_attachment as attach

IMAGE = 'sha256:' + 'a' * 64
ID = 'b' * 64


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);(self.root/'control').mkdir(mode=0o700)
        self.spec = {'schema_version':1,'image_id':IMAGE,'container_name':'war3-phase3-staging',
                     'uid':os.geteuid(),'gid':os.getegid(),'host_instance_dir':str(self.root)}
        self.accepted = {'schema_version':1,'image_id':IMAGE,'approved_by_main':True,
                         'source_sha256':{'src/runtime/controller.py':'c'*64},
                         'checks':{k:'PASS' for k in production.CHECKS}}
        production.private_json(self.root/'deployment.json', self.spec)
        production.private_json(self.root/'acceptance.json', self.accepted)
        # Unapplied edits must not be parsed or overwritten when adopting a survivor.
        (self.root/'control/preset-editor.json').write_text('unfinished operator edit')
        self.meta = {'Id':ID,'Name':'/war3-phase3-staging','Image':IMAGE,
                     'Config':{'User':f'{os.geteuid()}:{os.getegid()}',
                               'Labels':{'war3.managed':'phase3-staging'}},
                     'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'RestartPolicy':{'Name':'no'},
                                   'NetworkMode':'bridge','Init':True,'CapDrop':['ALL'],
                                   'SecurityOpt':['no-new-privileges:true'],
                                   'PortBindings':{'6113/tcp':[{'HostIp':'0.0.0.0','HostPort':'6113'}]}},
                     'State':{'Running':True,'Status':'running','Paused':False,'Restarting':False},'Mounts':[]}
        for n in (*production.RO,*production.RW):
            self.meta['Mounts'].append({'Type':'bind','Source':str(self.root/'data'/n),
                                       'Destination':'/data/'+n,'RW':n in production.RW})
        for n in production.VENDOR:
            self.meta['Mounts'].append({'Type':'bind','Source':str(self.root/'vendor'/n),
                                       'Destination':'/vendor/'+n,'RW':False})
        self.image = {'Id':IMAGE,'Architecture':'amd64','Os':'linux',
                      'Config':{'Labels':{'war3.production.source-digest':production.gate(self.accepted,IMAGE)}}}
        self.health={'healthy':True,'phase':'LOBBY'};self.queries=[]
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(production,'docker_output',return_value=(ID+'\n').encode()))
        def inspect(args):
            self.queries.append(args)
            return [copy.deepcopy(self.image if args[0]=='image' else self.meta)]
        self.stack.enter_context(patch.object(production,'docker_json',side_effect=inspect))
        self.exec=self.stack.enter_context(patch.object(production.os,'execvpe'))
        self.run=self.stack.enter_context(patch.object(attach.subprocess,'run',side_effect=lambda *a,**kw:
                         subprocess.CompletedProcess(a,0,json.dumps(self.health).encode(),b'')))

    def launch(self):
        with redirect_stdout(io.StringIO()) as output:
            result=attach.launch(production,['--instance-dir',str(self.root)])
        return result,output.getvalue()

    def test_reproduces_old_start_failure_then_adopts_without_running_or_stopping_container(self):
        with self.assertRaises(production.SettingsError) as old:
            production.stopped(self.spec)
        self.assertEqual(old.exception.args,('RUNTIME_STILL_RUNNING',))
        before=(self.root/'control/preset-editor.json').read_bytes()
        result,text=self.launch();self.assertEqual(result,0)
        argv=self.exec.call_args.args[1]
        self.assertEqual(argv,[*production.DOCKER,'attach','--sig-proxy=true',ID])
        self.assertEqual((self.root/'control/preset-editor.json').read_bytes(),before)
        self.assertIn('INFO supervisor: Lobby is ready.',text)
        self.assertEqual(self.run.call_count,1)
        self.assertEqual(self.run.call_args.args[0][:len(production.DOCKER)+3], [*production.DOCKER,'exec',ID,'python3'])
        self.assertEqual(self.queries,[['image','inspect',IMAGE],['container','inspect',ID],['container','inspect',ID]])

    def test_active_match_or_countdown_is_preserved_and_emits_matching_ready_line(self):
        for phase,message in [('IN_GAME','Game is running.'),('COUNTDOWN','Countdown is running.')]:
            with self.subTest(phase=phase):
                self.health['phase']=phase
                self.assertIn('INFO supervisor: '+message,self.launch()[1])
                self.assertEqual(self.exec.call_args.args[1][-1],ID)

    def test_invalid_ownership_and_security_contract_rejects_before_health_or_attach(self):
        changes=[lambda m:m.update(Image='sha256:'+'e'*64),
                 lambda m:m.update(Id='f'*64),lambda m:m.update(Name='/other'),
                 lambda m:m['Config'].update(User='0:0'),
                 lambda m:m['Config']['Labels'].update({'war3.managed':'foreign'}),
                 lambda m:m['Config']['Labels'].update({'war3.standalone.owner':'some-owner'}),
                 lambda m:m['HostConfig'].update(Privileged=True),
                 lambda m:m['HostConfig'].update(ReadonlyRootfs=False),
                 lambda m:m['HostConfig'].update(Init=False),
                 lambda m:m['HostConfig'].update(CapDrop=[]),
                 lambda m:m['HostConfig'].update(SecurityOpt=[]),
                 lambda m:m['HostConfig'].update(NetworkMode='host'),
                 lambda m:m['HostConfig']['RestartPolicy'].update(Name='always'),
                 lambda m:m['HostConfig']['PortBindings'].update({'8080/tcp':[{'HostPort':'8080'}]}),
                 lambda m:m['Mounts'][0].update(RW=True),
                 lambda m:m['Mounts'][0].update(Source='/foreign'),
                 lambda m:m['Mounts'].append({'Type':'bind','Source':'/var/run/docker.sock','Destination':'/socket','RW':True}),
                 lambda m:m['State'].update(Paused=True),
                 lambda m:m['State'].update(Running=False),
                 lambda m:m['State'].update(Restarting=True)]
        original=copy.deepcopy(self.meta)
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.meta=copy.deepcopy(original);change(self.meta)
                self.assertEqual(self.launch()[0],2)
                self.exec.assert_not_called();self.run.assert_not_called()

    def test_wrong_source_gate_rejects_before_health(self):
        self.image['Config']['Labels']['war3.production.source-digest']='0'*64
        self.assertEqual(self.launch()[0],2);self.run.assert_not_called();self.exec.assert_not_called()

    def test_unhealthy_or_malformed_response_is_not_announced_as_ready(self):
        for response in [b'not JSON',b'x'*1025,b'{"healthy":false,"phase":null}',
                         b'{"healthy":1,"phase":"LOBBY"}',b'{"healthy":true,"phase":"ERROR"}']:
            with self.subTest(response=response[:60]):
                self.run.side_effect=None;self.run.return_value=subprocess.CompletedProcess([],0,response,b'')
                result,text=self.launch();self.assertEqual(result,2);self.assertNotIn('Lobby is ready',text)
                self.exec.assert_not_called()

    def test_health_timeout_and_private_errors_are_not_echoed(self):
        self.run.side_effect=subprocess.TimeoutExpired(['PASSWORD_MUST_NOT_LEAK'],8)
        result,text=self.launch();self.assertEqual(result,2);self.assertNotIn('PASSWORD',text);self.exec.assert_not_called()

    def test_replaced_container_after_health_cannot_be_adopted_by_name(self):
        def run(*args,**kwargs):
            self.meta['Id']='f'*64
            return subprocess.CompletedProcess([],0,b'{"healthy":true,"phase":"LOBBY"}',b'')
        self.run.side_effect=run;self.assertEqual(self.launch()[0],2);self.exec.assert_not_called()

    def test_exclusive_foreground_owner_blocks_second_launcher(self):
        with attach.owner(self.root):
            result,text=self.launch();self.assertEqual(result,2);self.assertIn('already owns',text)
        self.run.assert_not_called();self.exec.assert_not_called()

    def test_lock_survives_exec_and_is_closed_after_nonexec_failure(self):
        seen=[]
        def execute(*args):
            with self.assertRaises(attach.AttachmentError):
                with attach.owner(self.root):pass
            seen.append(True)
        self.exec.side_effect=execute
        self.assertEqual(self.launch()[0],0);self.assertTrue(seen)
        with attach.owner(self.root) as fd:self.assertFalse(os.get_inheritable(fd))

    def test_fresh_start_still_calls_original_cli_under_owner_lease(self):
        with patch.object(production,'docker_output',return_value=b''),patch.object(production,'cli',return_value=17) as cli:
            self.assertEqual(self.launch()[0],17);cli.assert_called_once_with(['run','--instance-dir',str(self.root)])
        self.exec.assert_not_called();self.run.assert_not_called()

    def test_ready_messages_match_current_amp_template(self):
        kvp=dict(line.split('=',1) for line in (Path(__file__).parents[2]/'amp/war3-production.kvp').read_text().splitlines())
        regex=re.compile(kvp['Console.AppReadyRegex'].replace('\\\\','\\'))
        for phase in ['LOBBY','COUNTDOWN','IN_GAME']:
            self.health['phase']=phase;_,text=self.launch()
            self.assertTrue(any(regex.fullmatch(line) for line in text.splitlines()))

    def test_symlink_or_hardlinked_owner_lock_is_rejected(self):
        target=self.root/'control/.amp-runtime-owner.lock';other=self.root/'control/other';other.write_text('keep');other.chmod(0o600)
        target.symlink_to(other)
        self.assertEqual(self.launch()[0],2);target.unlink();os.link(other,target)
        self.assertEqual(self.launch()[0],2);self.exec.assert_not_called()


class HealthTests(unittest.TestCase):
    def valid(self):
        return {'ready':True,'mode':'iccup','state':'LOBBY','child_alive':True,'iccup_authenticated':True,
                'native_healthy':True,'relay_healthy':True,'star_process_alive':True,'game_engine_observed':False,
                'pid':28,'components':{n:{'alive':True,'pid':p} for n,p in [('native',28),('relay',30),('star',32)]},
                'game':{'session_generation':'a'*32}}

    def probe(self,value):
        import urllib.request
        with patch.object(urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(value).encode())),redirect_stdout(io.StringIO()) as out:
            exec(compile(attach.HEALTH_SCRIPT,'probe','exec'),{})
        return json.loads(out.getvalue())

    def test_live_snapshot_and_incomplete_or_engine_observed_states(self):
        self.assertTrue(self.probe(self.valid())['healthy'])
        for key in ['ready','child_alive','iccup_authenticated','native_healthy','relay_healthy','star_process_alive']:
            s=self.valid();s[key]=False;self.assertFalse(self.probe(s)['healthy'])
        s=self.valid();s['game_engine_observed']=True;self.assertFalse(self.probe(s)['healthy'])
        s=self.valid();s['components']['native']['pid']=29;self.assertFalse(self.probe(s)['healthy'])
        s=self.valid();s['game']['session_generation']='invalid';self.assertFalse(self.probe(s)['healthy'])


if __name__=='__main__':unittest.main()
