"""Cycle readiness distinguishes admission, running games and missing proof."""
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.runtime.cycle_monitor import public
from src.runtime.cycle_readiness import cycle_allows_ready
from src.runtime.controller import ICCupSupervisor


class CycleReadinessTests(unittest.TestCase):
    def setUp(self):
        self.native={'session_generation':'a'*32,'phase':'LOBBY','active_games':1,
                     'host_counter':7,'catalog_index':1}
        self.cycle={'state':'blocked_pending','active_matches':1,'max_matches':3,
                    '_native_observed_at':100,
                    'current_lobby':{'phase':'LOBBY','host_counter':7,'catalog_index':1},
                    'availability':{'active_matches':'verified_snapshot',
                                    'current_lobby':'verified_snapshot'}}
        self.options={'generation':'a'*32,'failed':False,'listener':True,'published':True}

    def ready(self,cycle=None,native=None,**options):
        return cycle_allows_ready(self.cycle if cycle is None else cycle,
                                  self.native if native is None else native,
                                  **{**self.options,**options})

    def test_matching_published_lobby_and_countdown_can_be_ready(self):
        self.assertTrue(self.ready())
        self.native['phase']='COUNTDOWN';self.cycle['current_lobby']['phase']='COUNTDOWN'
        self.assertTrue(self.ready())

    def test_lobby_requires_publication_listener_and_catalog_match(self):
        for options in ({'published':False},{'listener':False},{'generation':None},
                        {'generation':'b'*32},{'failed':True}):
            with self.subTest(options=options):self.assertFalse(self.ready(**options))
        for changes in ({'host_counter':8},{'catalog_index':0},{'phase':'COUNTDOWN'}):
            cycle=copy.deepcopy(self.cycle);cycle['current_lobby'].update(changes)
            with self.subTest(changes=changes):self.assertFalse(self.ready(cycle=cycle))

    def test_full_running_matches_do_not_require_lobby_or_listener(self):
        native={**self.native,'phase':'IN_GAME','active_games':3}
        cycle={**self.cycle,'state':'capacity_full','active_matches':3,'current_lobby':None}
        self.assertTrue(self.ready(cycle,native,listener=False,published=False))
        self.assertFalse(self.ready(cycle,native,failed=True))
        self.assertFalse(self.ready({**cycle,'state':'storage_error'},native))

    def test_expired_snapshot_and_inconsistent_counts_revoke_readiness(self):
        expired=public(self.cycle,104,3)
        self.assertFalse(self.ready(cycle=expired))
        for count in (None,True,-1,4):
            with self.subTest(count=count):self.assertFalse(self.ready(cycle={**self.cycle,'active_matches':count}))
        self.assertFalse(self.ready(native={**self.native,'active_games':2}))
        self.assertFalse(self.ready(cycle={**self.cycle,'availability':{}}))

    def test_idle_and_zero_running_matches_are_not_ready(self):
        native={**self.native,'active_games':0}
        cycle={**self.cycle,'active_matches':0}
        for phase in ('IDLE','IN_GAME'):
            with self.subTest(phase=phase):
                self.assertFalse(self.ready(cycle,{**native,'phase':phase}))


class SupervisorCycleReadinessTests(unittest.TestCase):
    """Exercise the actual HTTP snapshot path with typed component metadata."""
    setUp=CycleReadinessTests.setUp

    def service(self):
        s=object.__new__(ICCupSupervisor)
        lease=object();owner=object()
        reader=SimpleNamespace(bound=True,child=owner,lease=lease,
                               _session_record={'session_generation':'a'*32})
        def child(pid):
            return SimpleNamespace(process=SimpleNamespace(pid=pid),started=90,generation=1)
        native=child(123);native.session_reader=reader;native.session_owner=owner
        s.children={'native':native,'relay':child(124),'star':child(125)}
        s.alive=lambda name:name in s.children
        s.cycle_control=SimpleNamespace(catalog=object(),lease=lease)
        s.cycle_session_read=(native,90);s.cycle_status=copy.deepcopy(self.cycle);s.cycle_failed=False
        s.guard=threading.RLock();s.config={'iccup':{'metadata_ttl':3,'native_state_ttl':15},
                                         'gameplay':{'autohost_enabled':True}}
        s.relay_observation={'observed_monotonic_seconds':100,'connected':True,'authenticated':True,
                             'expected_identity_confirmed':True,'realm_connection_healthy':True,
                             'advertisement_accepted':True,'native_intent_seen':True,
                             'native_heartbeat_fresh':True,'native_pid':123,'native_host_counter':7}
        s.star_observation={'observed_monotonic_seconds':100,'launcher_alive':True,'phase':'RUNNING'}
        s.native_initialized=True;s.native_observed_at=100;s.native_listener=True
        s.native_observation=copy.deepcopy(self.native)
        s.want_running=True;s.stopping=False;s.state='LOBBY';s.reason='native_lobby_published'
        s.component_exits={};s.child_generations={'native':1,'star':1,'relay':1}
        s.created_at=90;s.component_attempts={};s.circuit_open=set();s.permanent_errors={}
        s.retry_times={};s.last_exit_code=None;s.last_log_time=None;s.engine_observed=False;s.history=[]
        return s

    def snapshot(self,s):
        with patch('src.runtime.controller.time.monotonic',return_value=100):return s.snapshot()

    def test_actual_snapshot_accepts_bound_ready_lobby(self):
        s=self.service();self.assertTrue(self.snapshot(s)['ready'])

    def test_actual_snapshot_rejects_missing_or_different_session_and_expired_proof(self):
        for change in ('failed','different_child','different_generation','no_catalog','expired'):
            s=self.service()
            if change=='failed':s.cycle_failed=True
            elif change=='different_child':s.cycle_session_read=(object(),90)
            elif change=='different_generation':s.children['native'].session_reader._session_record['session_generation']='b'*32
            elif change=='no_catalog':s.cycle_control.catalog=None
            else:s.cycle_status['_native_observed_at']=84
            with self.subTest(change=change):self.assertFalse(self.snapshot(s)['ready'])

    def test_native_four_second_heartbeat_does_not_expire_with_realm_ttl(self):
        s=self.service()
        # Realm workers are current; native state is between its four-second heartbeats.
        s.native_observed_at=96
        s.cycle_status['_native_observed_at']=96
        self.assertTrue(self.snapshot(s)['ready'])
        s.cycle_status['_native_observed_at']=84
        self.assertFalse(self.snapshot(s)['ready'])
        s.cycle_status['_native_observed_at']=96
        s.relay_observation['observed_monotonic_seconds']=96
        self.assertFalse(self.snapshot(s)['ready'])  # Realm TTL remains strict.

    def test_actual_snapshot_full_matches_ready_without_lobby_advertisement(self):
        s=self.service();s.native_observation.update(phase='IN_GAME',active_games=3)
        s.cycle_status.update(state='capacity_full',active_matches=3,current_lobby=None)
        s.native_listener=False;s.relay_observation['advertisement_accepted']=False
        result=self.snapshot(s)
        self.assertTrue(result['ready']);self.assertFalse(result['lobby_publication_accepted'])
        s.relay_observation['authenticated']=False
        self.assertFalse(self.snapshot(s)['ready'])


if __name__=='__main__':unittest.main()
