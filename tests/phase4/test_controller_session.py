"""Offline proof that the production reader owns the startup session line."""
import io
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.runtime.config import gameplay_config, iccup_config
from src.runtime.controller import ICCupSupervisor
from src.runtime.session_lease import SessionLease


class Backend:
    def __init__(self, root, journal, overrides):
        self.root, self.journal, self.overrides = root, journal, overrides
    def prepare(self):
        (self.root/'bridge').mkdir(mode=0o700)
    def command(self, name, generation):
        assert name == 'native'
        code = '''import json,os,sys,time
st=os.stat(sys.argv[1]); obj={'version':1,'pid':os.getpid(),
 'session_generation':'a'*32,'journal_device':st.st_dev,'journal_inode':st.st_ino}
obj.update(json.loads(sys.argv[2]))
if len(sys.argv)>3:
 fd=int(sys.argv[3]); os.write(1,b'SEALED_OK\\n' if os.pread(fd,64,0)==b'synthetic sealed map' else b'SEALED_BAD\\n')
os.write(1,b'boot\\n[HOSTBOT_SESSION] '+json.dumps(obj).encode()+b'\\n')
while True: time.sleep(1)
'''
        overrides=dict(self.overrides)
        if getattr(self,'catalog_startup',None) is not None:
            ipc=os.stat(self.catalog_startup['ipc_directory'])
            overrides.update(version=2,ipc_device=ipc.st_dev,ipc_inode=ipc.st_ino)
        return [sys.executable, '-u', '-c', code, str(self.journal), json.dumps(overrides)]
    def listener(self, pid): return False
    def cleanup_prefix(self): return {'cleaned': True}
    def close(self): pass


class ControllerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.session=self.root/'session'
        lease=SessionLease.create(self.session,enabled=True);lease.close()
        self.cfg={'mode':'iccup','runtime_dir':str(self.root/'supervisor'),
            'http':{'host':'127.0.0.1','port':0},
            'hostbot':{'command':['/unused'],'cwd':str(self.root),'startup_timeout':2,'stop_timeout':.5,'lobby_timeout':1},
            'recovery':{'max_restarts':2,'backoff_initial':.1,'backoff_max':.2,'stable_seconds':30},
            'iccup':{**iccup_config({}),'bridge_dir':str(self.root/'bridge')},
            'gameplay':gameplay_config({})}
        self.service=None
    def tearDown(self):
        s=self.service
        if s:
            s._stop_component('native')
            if s.cycle_control:s.cycle_control.close()
            s.running=False;s.backend.close();s.lockfile.close()
        self.temp.cleanup()
    def setup_child(self,overrides=None,*,with_preload=False):
        backend=Backend(self.root,self.session/'journal',overrides or {})
        if with_preload:
            from src.runtime.preload_inputs import MapInput, pin_preload_inputs
            path=self.root/'map.bin';path.write_bytes(b'synthetic sealed map')
            def native_preload(generation):
                backend.catalog=pin_preload_inputs([MapInput('map',path,hashlib.sha256(path.read_bytes()).hexdigest())],
                                                  default_map_bytes=path.stat().st_size,max_games=2)
                return backend.catalog
            backend.native_preload=native_preload
            command=backend.command
            def command_with_fd(name,generation):
                return command(name,generation)+[str(backend.catalog.maps[0].fd)]
            backend.command=command_with_fd
        s=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=backend,
                          cycle_session={'root':str(self.session),'cycle':['a','b']})
        self.service=s;s._cycle_initialize()
        self.assertFalse(s._cycle_permits_native_spawn())
        child=s._spawn('native')
        return s,child
    def dispatch_session(self,s):
        self.lines=[]
        end=time.monotonic()+5
        while time.monotonic()<end:
            try:event=s.events.get(timeout=.2)
            except queue.Empty:continue
            if event[0]=='component_line' and event[1]=='native':
                self.lines.append(event[3])
                s._native_line(event[2],event[3],*event[4:])
                if event[3].startswith('[HOSTBOT_SESSION] '):return
        self.fail('startup line timeout')
    def test_ownership_failure_and_failed_stop_retain_live_child(self):
        with patch('src.runtime.process_ownership.OwnedChild',side_effect=OSError('injected')):
            s,child=self.setup_child()
        self.assertTrue(s.cycle_failed)
        self.assertIsNone(child.session_owner)
        with patch('src.runtime.controller.os.killpg',side_effect=PermissionError('injected')):
            with self.assertRaises(PermissionError):s._stop_component('native')
        self.assertIs(s.children['native'],child)
        self.assertIsNone(child.process.poll())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_session_binding_and_sealed_map_fd_share_exact_child_lifecycle(self):
        s,child=self.setup_child(with_preload=True)
        catalog=s.backend.catalog
        self.dispatch_session(s)
        self.assertIn('SEALED_OK\n',self.lines)
        self.assertFalse(s.cycle_failed)
        self.assertEqual(child.preload,catalog)
        with patch('src.runtime.controller.os.killpg',side_effect=PermissionError('injected')):
            with self.assertRaises(PermissionError):s._stop_component('native')
        self.assertIs(s.children['native'],child)
        self.assertIsNone(child.process.poll())
        self.assertTrue(catalog.pass_fds())
        self.assertIsNotNone(child.session_owner.fd)
        s._stop_component('native')
        self.assertIsNotNone(child.session_owner._verified_exit)
        with self.assertRaisesRegex(ValueError,'CLOSED'):catalog.pass_fds()
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertTrue(s.cycle_failed)
        self.assertEqual(s.cycle_control.lease.journal,self.session/'journal')

    def test_owned_reader_binds_native_record_to_lease(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        self.assertFalse(s.cycle_failed)
        self.assertIsNotNone(child.session_reader)
        record=json.loads((self.session/'journal'/('session-'+'a'*32+'.json')).read_bytes())
        self.assertEqual(record['session']['pid'],child.process.pid)
        self.assertEqual(record['session']['journal_inode'],(self.session/'journal').stat().st_ino)
        s.cycle_control.lease.require_native('a'*32)
        self.assertFalse(s._cycle_permits_native_spawn())
    def setup_catalog_child(self):
        import fcntl
        from src.runtime.preload_inputs import MapInput, pin_preload_inputs, _SEALS
        from src.runtime.preload_manifest import StartupBundle, _HEADER, _ENTRY, _accounted
        from test_cycle_driver import manifest
        ipc=self.session/'ipc-journal';ipc.mkdir(mode=0o700)
        source=self.root/'map.bin';source.write_bytes(b'm'*100)
        self.cfg['gameplay']['max_concurrent_matches']=2
        backend=Backend(self.root,self.session/'journal',{})
        backend.catalog_startup={'cycle':['one','one'], 'journal_directory':str(self.session/'journal'),
                                 'ipc_directory':str(ipc)}
        def preload(generation):
            pinned=pin_preload_inputs([MapInput('one',source,hashlib.sha256(b'm'*100).hexdigest())],
                                      default_map_bytes=100,max_games=2)
            payload=bytearray(manifest());header=list(_HEADER.unpack_from(payload,0))
            header[3]=2;header[6]=_accounted([100],100,2);_HEADER.pack_into(payload,0,*header)
            entry=list(_ENTRY.unpack_from(payload,_HEADER.size))
            entry[0]=pinned.maps[0].fd;entry[1]=pinned.maps[0].size;entry[2]=bytes.fromhex(pinned.maps[0].sha256)
            _ENTRY.pack_into(payload,_HEADER.size,*entry);payload=bytes(payload)
            fd=os.memfd_create('catalog-test',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
            os.write(fd,payload);fcntl.fcntl(fd,fcntl.F_ADD_SEALS,_SEALS)
            result=StartupBundle(pinned,fd,os.fstat(fd),hashlib.sha256(payload).hexdigest())
            pinned._closed=True
            return result
        backend.native_preload=preload
        s=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=backend,
                          cycle_session={'root':str(self.session),'cycle':['one','one']})
        self.service=s;s._cycle_initialize();child=s._spawn('native');self.dispatch_session(s)
        self.assertFalse(s.cycle_failed)
        control=s.cycle_control;catalog=control.catalog
        self.assertIsNotNone(control.driver)
        return s,child,ipc,control,catalog

    def test_immediate_rejected_ack_skips_without_restarting_owned_native(self):
        from src.runtime.preload_ipc import notify_request
        s, child, ipc, control, catalog = self.setup_catalog_child()
        pid = child.process.pid
        payload = {'version': 1, 'phase': 'IDLE', 'session_generation': 'a'*32,
                   'active_games': 0, 'uptime_seconds': 0}
        raw = ('[HOSTBOT_STATE] ' + json.dumps(payload) + '\n').encode()
        s._native_line(child.generation, raw.decode(), child, child.reader_token, raw, time.monotonic())
        def reject(directory, request):
            notify_request(directory, request)
            path = ipc / ('ack-' + request.request_id + '.ready')
            path.write_bytes(request.payload() + b'status=rejected\n'); path.chmod(0o600)
            (ipc / 'next.ready').unlink()
        with patch('src.runtime.catalog_requests.notify_request', side_effect=reject):
            s.cycle_next_scan = 0.
            s._cycle_tick()
        self.assertFalse(s.cycle_failed)
        self.assertEqual(control.state.state['index'], 1)
        self.assertIsNone(catalog.data['pending'])
        self.assertIsNone(child.process.poll())
        self.assertEqual(child.process.pid, pid)
        self.assertIn('Lobby creation rejected for preset one.', s.output.getvalue())

    def test_catalog_close_v2_is_consumed_by_controller_with_owned_session(self):
        s,child,ipc,control,catalog=self.setup_catalog_child()
        catalog.enqueue(control.state)
        request=catalog.data['pending']
        def state(**changes):
            payload={'version':1,'phase':'IDLE','session_generation':'a'*32,'active_games':0,'uptime_seconds':0}
            payload.update(changes)
            raw=('[HOSTBOT_STATE] '+json.dumps(payload)+'\n').encode()
            s._native_line(child.generation,raw.decode(),child,child.reader_token,raw,time.monotonic())
        forged=('[HOSTBOT_STATE] '+json.dumps({'version':1,'phase':'IDLE',
            'session_generation':'a'*32,'active_games':0})+'\n').encode()
        s._native_line(child.generation,forged.decode(),child,object(),forged,time.monotonic())
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse((ipc/'next.ready').exists())
        state(active_games=2)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse((ipc/'next.ready').exists())
        state(session_generation='f'*32)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse((ipc/'next.ready').exists())
        state()
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertEqual(s.cycle_status['driver'],'awaiting_ack')
        native_request=catalog._request(request)
        self.assertEqual((ipc/'next.ready').read_bytes(),native_request.payload())
        (ipc/'next.ready').unlink()
        ack=ipc/('ack-'+request['request_id']+'.ready')
        ack.write_bytes(native_request.payload()+b'status=staged\n');ack.chmod(0o600)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertEqual(s.cycle_status['driver'],'awaiting_lobby')
        self.assertFalse(list(control.lease.journal.glob('reservation-*.ready')))
        state(phase='LOBBY',host_counter=7,catalog_request_id='f'*32,catalog_index=request['index'])
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse(list(control.lease.journal.glob('reservation-*.ready')))
        state(phase='LOBBY',host_counter=7,catalog_request_id=request['request_id'],catalog_index=request['index'])
        owner,proof,_=s.cycle_observation
        s.cycle_observation=(owner,proof,time.monotonic()-20)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse(list(control.lease.journal.glob('reservation-*.ready')))
        state(phase='LOBBY',host_counter=7,catalog_request_id=request['request_id'],catalog_index=request['index'])
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertEqual(s.cycle_status['driver'],'reserved')
        self.assertEqual(control.state.state['pending']['host_counter'],7)
        self.assertTrue(list(control.lease.journal.glob('reservation-*.ready')))
        event={'version':2,'event':'lobby_closed','session_generation':'a'*32,'host_counter':7,
               'request_id':request['request_id'],'manifest_sha256':catalog.digest,'catalog_index':request['index']}
        path=control.lease.journal/('close-'+'a'*32+'-7.json')
        path.write_text(json.dumps(event));path.chmod(0o600)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertFalse(s.cycle_failed)
        self.assertIsNone(catalog.data['pending'])
        self.assertEqual(catalog.data['completed']['outcome'],'closed')
        self.assertEqual(s.snapshot()['cycle']['last_transition']['event'],'closed')
        self.assertEqual(control.state.state['index'],0)
        self.assertTrue(path.with_suffix('.ack').exists())
        self.assertFalse(s._cycle_permits_native_spawn())
        s._stop_component('native');s.cycle_next_scan=0.;s._cycle_tick()
        self.assertTrue(s.cycle_failed)
        self.assertIsNotNone(control.catalog)

    def test_match_telemetry_uses_manifest_and_rejects_counter_regression(self):
        s,child,ipc,control,catalog=self.setup_catalog_child()
        def emit(retired,index=0):
            matches=[{'host_counter':4,'catalog_index':0,'phase':'loading','match_id':'c'*32},
                     {'host_counter':5,'catalog_index':index,'phase':'playing','match_id':'d'*32}]
            payload={'version':1,'phase':'IN_GAME','session_generation':'a'*32,
                     'active_games':2,'uptime_seconds':0,
                     'matches_v1':{'version':1,'active_count':2,'retired_count':retired,
                                   'rotation_error_count':1,'matches':matches}}
            raw=('[HOSTBOT_STATE] '+json.dumps(payload)+'\n').encode()
            s._native_line(child.generation,raw.decode(),child,child.reader_token,raw,time.monotonic())
            s.cycle_next_scan=0.;s._cycle_tick()
            return s.snapshot()['cycle']
        current=emit(4)
        self.assertEqual(current['finished_matches'],4)
        self.assertEqual(current['rotation_errors'],1)
        self.assertIsNone(current['error_count'])
        self.assertEqual([m['preset_id'] for m in current['active_match_list']],['one','one'])
        self.assertEqual([m['phase'] for m in current['active_match_list']],['loading','playing'])
        self.assertIsNone(emit(3)['finished_matches'])
        self.assertIsNone(emit(5,index=31)['active_match_list'])
        self.assertEqual(emit(5)['finished_matches'],5)
        self.assertFalse(s.cycle_failed)
        self.assertEqual(list(ipc.iterdir()),[])

    def test_capacity_full_is_normal_and_stale_telemetry_expires(self):
        s,child,ipc,control,catalog=self.setup_catalog_child()
        raw=('[HOSTBOT_STATE] '+json.dumps({'version':1,'phase':'IN_GAME',
            'session_generation':'a'*32,'active_games':2,'uptime_seconds':0})+'\n').encode()
        s._native_line(child.generation,raw.decode(),child,child.reader_token,raw,time.monotonic())
        s.cycle_next_scan=0.;s._cycle_tick()
        cycle=s.snapshot()['cycle']
        self.assertEqual(cycle['state'],'capacity_full')
        self.assertEqual(cycle['status_code'],'CAPACITY_FULL')
        self.assertEqual((cycle['active_matches'],cycle['max_matches']),(2,2))
        self.assertIsNone(cycle['active_match_list'])
        self.assertIsNone(cycle['finished_matches'])
        self.assertIsNone(cycle['error_count'])
        self.assertFalse(s.cycle_failed)
        self.assertTrue(child.session_owner.is_running())
        self.assertFalse(list(ipc.glob('request-*.ready')))
        s.cycle_status['_native_observed_at']=time.monotonic()-100
        stale=s.snapshot()['cycle']
        self.assertIsNone(stale['active_matches'])
        self.assertEqual(stale['availability']['active_matches'],'unavailable')
        self.assertNotEqual(stale['state'],'capacity_full')
        self.assertIsNone(stale['status_code'])

    def test_replaced_ipc_inode_blocks_cycle_without_stopping_child(self):
        s,child,ipc,control,catalog=self.setup_catalog_child()
        held=ipc.with_name('detached-ipc-journal')
        ipc.rename(held);ipc.mkdir(mode=0o700)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertTrue(s.cycle_failed)
        self.assertEqual(s.cycle_status['state'],'storage_error')
        self.assertTrue(child.session_owner.is_running())
        self.assertFalse(s._cycle_permits_native_spawn())
        self.assertIsNone(catalog.data['pending'])

    def test_catalog_session_rejects_unpinned_bundle_without_killing_child(self):
        backend=Backend(self.root,self.session/'journal',{})
        ipc=self.session/'ipc-journal';ipc.mkdir(mode=0o700)
        backend.catalog_startup={'cycle':['a','b'],'journal_directory':str(self.session/'journal'),
                                 'ipc_directory':str(ipc)}
        s=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=backend,
                          cycle_session={'root':str(self.session),'cycle':['a','b']})
        self.service=s;s._cycle_initialize();child=s._spawn('native');self.dispatch_session(s)
        self.assertTrue(s.cycle_failed)
        self.assertIsNone(s.cycle_control.catalog)
        self.assertTrue(child.session_owner.is_running())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_journal_publication_error_latches_without_stopping_child(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        with patch.object(s.cycle_control.coordinator,'consume',side_effect=OSError('injected')):
            s.cycle_next_scan=0.;s._cycle_tick()
        self.assertTrue(s.cycle_failed)
        self.assertTrue(child.session_owner.is_running())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_feedback_publication_error_keeps_owned_child_alive(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        with patch.object(s,'_feedback',side_effect=OSError('injected')):
            s._publish_feedback()
        self.assertTrue(s.cycle_failed)
        self.assertTrue(child.session_owner.is_running())
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_launch_claim_is_durable_before_popen_and_blocks_retry(self):
        backend=Backend(self.root,self.session/'journal',{})
        s=ICCupSupervisor(self.cfg,output=io.StringIO(),backend=backend,
                          cycle_session={'root':str(self.session),'cycle':['a','b']})
        self.service=s;s._cycle_initialize()
        claim=self.session/'native-launch-journal.json'
        def failed_spawn(*args,**kwargs):
            self.assertTrue(claim.is_file())
            self.assertEqual(json.loads(claim.read_bytes())['journal_inode'],
                             (self.session/'journal').stat().st_ino)
            raise OSError('injected after durable claim')
        with patch('src.runtime.controller.subprocess.Popen',side_effect=failed_spawn):
            with self.assertRaises(OSError):s._spawn('native')
        with patch('src.runtime.controller.subprocess.Popen',side_effect=AssertionError('spawn retried')):
            with self.assertRaisesRegex(ValueError,'CYCLE_LAUNCH_UNRESOLVED'):s._spawn('native')
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_untrusted_envelope_cannot_create_binding(self):
        s,child=self.setup_child()
        forged='[HOSTBOT_SESSION] '+json.dumps({'version':1,'pid':child.process.pid,
          'session_generation':'a'*32,'journal_device':0,'journal_inode':1})
        s._native_line(child.generation,forged)
        self.assertTrue(s.cycle_failed)
        self.assertFalse(list((self.session/'journal').glob('session-*.json')))
        self.assertIsNone(child.process.poll())
    def test_wrong_native_inode_does_not_kill_child_or_bind(self):
        s,child=self.setup_child({'journal_inode':1})
        self.dispatch_session(s)
        self.assertTrue(s.cycle_failed)
        self.assertFalse(list((self.session/'journal').glob('session-*.json')))
        self.assertIsNone(child.process.poll())
    def test_verified_child_exit_without_writer_scope_keeps_journal(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        gen='a'*32
        s.cycle_control.coordinator.allow_lobby(gen,7)
        old=s.cycle_control.lease.journal
        path=old/('start-'+gen+'-'+'b'*32+'.json')
        path.write_text(json.dumps({'version':1,'event':'game_started','session_generation':gen,
                                    'match_id':'b'*32,'host_counter':7}))
        path.chmod(0o600)
        s._stop_component('native')
        self.assertIsNotNone(child.session_owner._verified_exit)
        self.assertFalse(s.cycle_failed)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertTrue(s.cycle_failed)
        self.assertEqual(s.cycle_control.lease.journal,old)
        self.assertEqual(s.cycle_control.state.current,'b')
        self.assertTrue(list(old.glob('start-*.ack')))
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_verified_exit_without_event_stays_pending_in_old_journal(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        s.cycle_control.coordinator.allow_lobby('a'*32,7)
        old=s.cycle_control.lease.journal
        s._stop_component('native')
        self.assertIsNotNone(child.session_owner._verified_exit)
        s.cycle_next_scan=0.;s._cycle_tick()
        self.assertEqual(s.cycle_status['state'],'blocked_pending')
        self.assertEqual(s.cycle_control.lease.journal,old)
        self.assertEqual(s.cycle_control.state.state['pending']['host_counter'],7)
        self.assertFalse(s._cycle_permits_native_spawn())

    def test_competing_reaper_blocks_generation_handoff(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        child.process.kill();child.process.wait()
        s._stop_component('native')
        self.assertTrue(s.cycle_failed)
        self.assertEqual(s.cycle_control.lease.journal,self.session/'journal')
        self.assertFalse(list(self.session.glob('journal-*')))

    def test_duplicate_startup_blocks_without_overwriting_binding(self):
        s,child=self.setup_child()
        self.dispatch_session(s)
        path=self.session/'journal'/('session-'+'a'*32+'.json')
        before=path.read_bytes()
        record=json.loads(before)['session']
        raw=b'[HOSTBOT_SESSION] '+json.dumps(record).encode()+b'\n'
        s._native_line(child.generation,raw.decode(),child,child.reader_token,raw)
        self.assertTrue(s.cycle_failed)
        self.assertEqual(path.read_bytes(),before)
        self.assertIsNone(child.process.poll())

    def test_stale_generation_cannot_bind_new_child(self):
        s,child=self.setup_child()
        s._native_line(child.generation-1,'[HOSTBOT_SESSION] {}',child,child.reader_token,b'[HOSTBOT_SESSION] {}\n')
        self.assertFalse(s.cycle_failed)
        self.assertFalse(list((self.session/'journal').glob('session-*.json')))
        self.dispatch_session(s)
        self.assertFalse(s.cycle_failed)


if __name__=='__main__':unittest.main()
