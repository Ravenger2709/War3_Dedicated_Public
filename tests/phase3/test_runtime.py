"""Production supervision semantics against synthetic subprocesses, no account."""
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config,iccup_config
from src.runtime.controller import ICCupSupervisor,RuntimeBackend,fresh
from src.runtime.files import atomic_json,private_bytes,private_directory
from src.runtime.native import listener_owned,native_state,prepare_native

FIXTURE=Path(__file__).with_name('runtime_component.py')


class MetadataReadClockTests(unittest.TestCase):
 def check_metadata(self,stamp_offset):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);bridge=root/'bridge';bridge.mkdir(mode=0o700)
   service=object.__new__(ICCupSupervisor)
   service.config={'runtime_dir':str(root/'supervisor'),'iccup':{'bridge_dir':str(bridge),'metadata_ttl':.25}}
   def child(pid):return SimpleNamespace(process=SimpleNamespace(pid=pid,poll=lambda:None),started=99,generation=1)
   service.children={name:child(pid) for name,pid in (('relay',124),('star',123),('native',125))}
   service.backend=SimpleNamespace(listener=lambda pid:pid==125);service.guard=threading.RLock()
   clock=[100.0]
   from src.runtime.files import private_json as read_json
   def read_after_concurrent_write(path):
    # The worker atomically replaces its file AFTER the controller starts this
    # iteration, but BEFORE the actual read. Timestamp it on that same clock.
    clock[0]+=.05
    if path.name=='relay-status.json':
     value={'relay_pid':124,'relay_started_monotonic_seconds':99.5,'observed_monotonic_seconds':clock[0]+stamp_offset}
    else:
     value={'worker_pid':123,'generation':1,'observed_monotonic_seconds':clock[0]+stamp_offset}
    atomic_json(path,value)
    result=read_json(path)
    clock[0]+=.05
    return result
   with patch('src.runtime.controller.time.monotonic',side_effect=lambda:clock[0]), \
        patch('src.runtime.controller.private_json',side_effect=read_after_concurrent_write):
    service._metadata()
   return service.relay_observation,service.star_observation,service.native_listener
 def test_new_worker_file_written_during_read_is_fresh(self):
  relay,star,listener=self.check_metadata(0)
  self.assertEqual(relay.get('relay_pid'),124)
  self.assertEqual(star.get('worker_pid'),123)
  self.assertTrue(listener)
 def test_stale_and_actually_future_worker_files_still_rejected(self):
  for offset in (-1,1):
   with self.subTest(offset=offset):
    relay,star,_=self.check_metadata(offset)
    self.assertEqual(relay,{})
    self.assertEqual(star,{})


class SyntheticBackend:
 def __init__(self,cfg,root):self.cfg=cfg;self.root=root;self.cleanups=0;self.closed=False
 def prepare(self):private_directory(self.root/'bridge')
 def command(self,name,generation):return [sys.executable,str(FIXTURE),name,str(self.root),str(generation)]
 def cleanup_prefix(self):self.cleanups+=1;return {'cleaned':True}
 def listener(self,pid):return listener_owned(pid,6113)
 def close(self):self.closed=True


class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.cfg={'mode':'iccup','runtime_dir':str(self.root/'supervisor'),
   'http':{'host':'127.0.0.1','port':0},
   'hostbot':{'command':['/opt/war3/bin/ghost++','/data/state/native/ghost.cfg'],'cwd':str(self.root),'startup_timeout':2,'stop_timeout':.4,'lobby_timeout':1},
   'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
   'iccup':{**iccup_config({}),'bridge_dir':str(self.root/'bridge'),'metadata_ttl':.25,'native_state_ttl':.5,'auth_timeout':2,'disconnect_grace':.15},
   'gameplay':gameplay_config({})}
  self.backend=SyntheticBackend(self.cfg,self.root);self.service=None
 def tearDown(self):
  if self.service:
   self.service.submit('quit');self.service.thread.join(5)
   self.assertFalse(self.service.thread.is_alive())
   for child in list(self.service.children.values()):self.assertIsNotNone(child.process.poll())
  self.temp.cleanup()
 def wait(self,test,timeout=5):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   if test():return
   time.sleep(.02)
  self.fail('bounded controller condition timeout '+str(self.service.snapshot())+' '+self.service.output.getvalue()[-3500:])
 def start(self):
  self.service=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=self.backend)
  self.service.start();self.wait(lambda:self.service.snapshot()['ready'] and self.service.state=='LOBBY');return self.service
 def control(self,**values):atomic_json(self.root/'control.json',values)
 def test_normal_readiness_and_shutdown_preserve_native_data(self):
  s=self.start();status=s.snapshot()
  self.assertTrue(status['iccup_authenticated']);self.assertTrue(status['player_listener_available'])
  self.assertTrue(status['lobby_publication_accepted']);self.assertEqual(status['state'],'LOBBY')
  s.submit('stop');self.wait(lambda:s.state=='STOPPED' and not s.children)
  self.assertFalse(s.snapshot()['ready']);self.assertFalse(s.snapshot()['iccup_authenticated'])
  self.assertEqual((self.root/'persistent-count').read_text(),'1')
  self.assertTrue(all(code==0 for code in s.component_exits.values()))
  s.submit('start');self.wait(lambda:s.snapshot()['ready'])
  self.assertEqual((self.root/'persistent-count').read_text(),'2')
 def test_child_started_before_popen_returns_is_accepted(self):
  original=subprocess.Popen
  def delayed(*args,**kwargs):
   child=original(*args,**kwargs);time.sleep(.15);return child
  with patch('src.runtime.controller.subprocess.Popen',side_effect=delayed):s=self.start()
  self.assertTrue(s.snapshot()['iccup_authenticated'])
 def test_star_crash_recovers_without_restarting_native(self):
  s=self.start();native=s.children['native'].process.pid;old=s.children['star'].process.pid
  os.kill(old,signal.SIGKILL)
  self.wait(lambda:s.alive('star') and s.children['star'].process.pid!=old and s.snapshot()['ready'])
  self.assertEqual(s.children['native'].process.pid,native)
  self.assertGreaterEqual(self.backend.cleanups,1)
 def test_relay_crash_recovers_without_restarting_native(self):
  s=self.start();native=s.children['native'].process.pid;old=s.children['relay'].process.pid
  os.kill(old,signal.SIGKILL)
  self.wait(lambda:s.alive('relay') and s.children['relay'].process.pid!=old and s.snapshot()['ready'])
  self.assertEqual(s.children['native'].process.pid,native)
 def test_native_crash_recreates_lobby_without_duplicate_children(self):
  s=self.start();old=s.children['native'].process.pid
  os.kill(old,signal.SIGKILL)
  self.wait(lambda:s.alive('native') and s.children['native'].process.pid!=old and s.snapshot()['ready'])
  self.assertEqual(len(s.children),3);self.assertEqual((self.root/'persistent-count').read_text(),'2')
 def test_missing_relay_heartbeat_cannot_keep_online(self):
  s=self.start();self.control(relay_stale=True)
  self.wait(lambda:not s.snapshot()['iccup_authenticated'])
  self.assertFalse(s.snapshot()['ready'])
 def test_wrong_relay_pid_cannot_keep_online(self):
  s=self.start();self.control(wrong_relay_pid=True)
  self.wait(lambda:not s.snapshot()['iccup_authenticated'])
  self.assertFalse(s.snapshot()['ready'])
 def test_missing_star_heartbeat_revokes_online(self):
  s=self.start();self.control(star_stale=True)
  self.wait(lambda:not s.snapshot()['iccup_authenticated'])
  self.assertFalse(s.snapshot()['ready'])
 def test_rejected_authentication_requires_operator_intervention(self):
  s=self.start();self.control(proof_status=2)
  self.wait(lambda:s.permanent_errors.get('realm')=='AUTHENTICATION_REJECTED' and not s.alive('star'))
  generation=s.child_generations['star'];time.sleep(.4)
  self.assertEqual(s.child_generations['star'],generation);self.assertTrue(s.alive('native'))
 def test_failed_prefix_cleanup_blocks_new_cohort(self):
  s=self.start();self.backend.cleanup_prefix=lambda:{'cleaned':False}
  old=s.children['star'].process.pid;os.kill(old,signal.SIGKILL)
  self.wait(lambda:s.permanent_errors.get('realm')=='PREFIX_CLEANUP_FAILED')
  generation=s.child_generations['star'];time.sleep(.4)
  self.assertEqual(s.child_generations['star'],generation);self.assertTrue(s.alive('native'))
 def test_circuit_waits_for_cooldown_then_probes_once(self):
  s=self.start();s.config['iccup']['circuit_cooldown']=.5
  s.component_attempts['realm']=s.config['recovery']['max_restarts']
  os.kill(s.children['relay'].process.pid,signal.SIGKILL)
  self.wait(lambda:'realm' in s.circuit_open)
  generation=s.child_generations['star'];time.sleep(.15)
  self.assertEqual(s.child_generations['star'],generation)
  self.wait(lambda:s.child_generations['star']==generation+1 and s.snapshot()['ready'])
 def test_in_game_does_not_require_a_lobby_advertisement(self):
  s=self.start();self.control(phase='IN_GAME')
  self.wait(lambda:s.state=='IN_GAME')
  self.assertTrue(s.snapshot()['ready']);self.assertFalse(s.snapshot()['lobby_publication_accepted'])
 def test_native_heartbeat_failure_triggers_recovery(self):
  s=self.start();old=s.children['native'].process.pid;self.control(native_stale=True)
  self.wait(lambda:s.component_attempts['native']>=1)
  self.control();self.wait(lambda:s.alive('native') and s.children['native'].process.pid!=old and s.snapshot()['ready'])
 def test_engine_observation_stops_the_runtime(self):
  s=self.start();self.control(engine=True)
  self.wait(lambda:not s.running)
  self.assertFalse(s.snapshot()['ready']);self.assertFalse(s.children)
 def test_terminal_worker_engine_flag_blocks_cold_restart(self):
  s=self.start();generation=s.child_generations['star'];time.sleep(.6);self.control(terminal_engine=True,terminal_engine_stale=True)
  self.wait(lambda:not s.running)
  s.thread.join(5)
  self.assertTrue(s.engine_observed);self.assertEqual(s.child_generations['star'],generation)
  replacement=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=SyntheticBackend(self.cfg,self.root))
  self.service=replacement;replacement.start();time.sleep(.3)
  self.assertTrue(replacement.engine_observed);self.assertEqual(replacement.state,'ERROR');self.assertFalse(replacement.children)
 def test_unowned_listener_and_stale_metadata_fail_closed(self):
  self.assertFalse(listener_owned(None,6113))
  self.assertFalse(fresh({'observed_monotonic_seconds':float('nan')},10,3))
  self.assertFalse(fresh({'observed_monotonic_seconds':11},10,3))
  self.assertFalse(fresh({'observed_monotonic_seconds':5},10,3))
 def test_duplicate_supervisor_and_account_runtime_owner_rejected(self):
  s=self.start()
  with self.assertRaises(RuntimeError):ICCupSupervisor(self.cfg,backend=SyntheticBackend(self.cfg,self.root))
 def test_native_state_rejects_user_chat_and_malformed_values(self):
  self.assertIsNone(native_state('[GAME: x] [P]: [HOSTBOT_STATE] {}'))
  for body in ({'version':True,'phase':'LOBBY'}, {'version':1,'phase':'FAKE'}, {'version':1,'phase':'LOBBY','players':True}, {'version':1,'phase':'LOBBY','map_name':'secret\npath'}):
   with self.assertRaises(ValueError):native_state('[HOSTBOT_STATE] '+json.dumps(body))
 def test_config_limits_and_private_metadata(self):
  self.assertEqual(gameplay_config({})['lobby_refresh_interval'],10)
  for interval in (10,11,120,3600):
   self.assertEqual(gameplay_config({'lobby_refresh_interval':interval})['lobby_refresh_interval'],interval)
  for interval in (0,5,9,3601,True,10.0,'10'):
   with self.assertRaises(ValueError):gameplay_config({'lobby_refresh_interval':interval})
  for change in ({'game_port':6613},{'vote_required_percent':101},{'vote_min_players':1},{'autostart_players':13},{'lobby_refresh_interval':1},{'vote_enabled':'true'}):
   with self.assertRaises(ValueError):gameplay_config(change)
  for change in ({'relay_port':6112},{'upstream':'127.0.0.1'},{'adapter':'assume-authenticated'},{'prefix':'/data/../profile'}):
   with self.assertRaises(ValueError):iccup_config(change)
  f=self.root/'private.json';atomic_json(f,{'safe':True});self.assertEqual(f.stat().st_mode&0o777,0o600)
  f.chmod(0o644)
  with self.assertRaises(ValueError):private_bytes(f)
 def test_operator_settings_reach_native_keys(self):
  config=self.root/'config';config.mkdir(mode=0o700);(config/'maps').mkdir(mode=0o700)
  (config/'ghost.cfg').write_text('bnet_server =\nbot_defaultmap = selected.cfg\n')
  self.cfg['iccup']['native_config']=str(config/'ghost.cfg')
  self.cfg['gameplay']=gameplay_config({'max_players':8,'autostart_players':6,'vote_min_players':3,'vote_required_percent':70,'countdown_seconds':0,'lobby_refresh_interval':180})
  result=prepare_native(self.cfg,'SyntheticUser').read_text()
  for line in ('bot_vote_min_players = 3','bot_vote_required_percent = 70','bot_countdownseconds = 0','bot_lobby_refresh_interval = 180','bot_maxplayers = 8','autohost_startplayers = 6'):
   self.assertIn(line+'\n',result)
  self.assertIn('bnet_server = \n',result)
 def test_real_backend_owns_profile_and_emits_integer_worker_timeout(self):
  config=self.root/'config';config.mkdir(mode=0o700);(config/'maps').mkdir(mode=0o700)
  (config/'ghost.cfg').write_text('bnet_server =\nbot_defaultmap = selected.cfg\n')
  profile=self.root/'profile';profile.mkdir(mode=0o700)
  gateway=json.dumps('@ByteArray('+json.dumps({'Synthetic':'127.0.0.1:16112'})+')')
  path=profile/'launcher.ini';path.write_text('[General]\nUsername="Synthetic"\nConnections=\nCurrConnections=0\nSiteConnections='+gateway+'\nUserConnections='+gateway+'\n');path.chmod(0o600)
  self.cfg['iccup'].update(profile=str(profile),prefix=str(self.root/'prefix'),session=str(self.root/'session'),native_config=str(config/'ghost.cfg'),auth_timeout=180.5)
  self.cfg['hostbot']['command']=['/opt/war3/bin/ghost++',str(self.root/'native/ghost.cfg')]
  first=RuntimeBackend(self.cfg);second=RuntimeBackend(self.cfg)
  try:
   first.prepare()
   with self.assertRaisesRegex(ValueError,'ACCOUNT_PROFILE_ALREADY_OWNED'):second.prepare()
   self.assertTrue(first.cleanup_prefix()['cleaned'])
   command=first.command('star',1);worker=json.loads(Path(command[-1]).read_text())
   self.assertEqual(worker['startup_timeout'],181);self.assertIs(type(worker['startup_timeout']),int)
  finally:first.close();second.close()


if __name__=='__main__':unittest.main()
