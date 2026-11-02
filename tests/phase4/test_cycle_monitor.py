"""Truthful cursor/native projection and HTTP availability, offline only."""
import http.client
import json
from types import SimpleNamespace
import unittest

from src.runtime.cycle_monitor import project, public
from src.runtime.operator_logs import OperatorEvents
from src.supervisor.__main__ import serve


class CycleMonitorTests(unittest.TestCase):
    def setUp(self):
        self.cursor=SimpleNamespace(cycle=['one','two','one'],state={'index':1})
        self.record={'request_id':'b'*32,'index':1,'position':1,'counter':7}
        self.catalog=SimpleNamespace(generation='a'*32,data={'pending':self.record})
        self.state={'version':1,'phase':'LOBBY','session_generation':'a'*32,
                    'host_counter':7,'catalog_request_id':'b'*32,'catalog_index':1,'active_games':1}

    def test_cursor_and_matched_lobby_without_invented_counts(self):
        result=public(project({'state':'waiting_native_ipc'},self.cursor,2,(self.state,100,self.catalog)),101,3)
        self.assertEqual((result['map_cycle'],result['index'],result['current_preset'],result['next_preset']),
                         (['one','two','one'],1,'two','one'))
        self.assertEqual(result['active_matches'],1)
        self.assertEqual(result['current_lobby'],{'preset_id':'two','catalog_index':1,'host_counter':7,'phase':'LOBBY'})
        for key in ('active_match_list','finished_matches','error_count'):
            self.assertIsNone(result[key]);self.assertEqual(result['availability'][key],'unavailable')
        self.assertEqual(result['availability']['current_lobby'],'verified_snapshot')

    def test_mismatched_or_stale_lobby_is_unavailable(self):
        for change in ({'session_generation':'c'*32},{'catalog_request_id':'c'*32},
                       {'catalog_index':0},{'host_counter':8}):
            result=project({'state':'waiting_native_ipc'},self.cursor,2,({**self.state,**change},100,self.catalog))
            self.assertIsNone(result['current_lobby'])
            self.assertEqual(result['availability']['current_lobby'],'unavailable')
        result=public(project({'state':'waiting_native_ipc'},self.cursor,2,(self.state,100,self.catalog)),104,3)
        self.assertIsNone(result['active_matches']);self.assertIsNone(result['current_lobby'])
        self.assertEqual(result['availability']['active_matches'],'unavailable')

    def test_full_is_normal_and_expires(self):
        state={**self.state,'phase':'IN_GAME','active_games':2}
        cached=project({'state':'waiting_native_ipc','pending':True},self.cursor,2,(state,100,self.catalog))
        self.assertEqual(cached['state'],'capacity_full')
        self.assertEqual(cached['status_code'],'CAPACITY_FULL')
        self.assertTrue(cached['pending'])
        self.assertEqual(cached['availability']['current_lobby'],'unavailable')
        expired=public(cached,104,3)
        self.assertEqual(expired['state'],'blocked_pending')
        self.assertIsNone(expired['status_code'])
        self.assertIsNone(expired['active_matches'])
        self.assertEqual(cached['state'],'capacity_full')  # HTTP never mutates owner state.

    def test_full_snapshot_never_masks_storage_error(self):
        state={**self.state,'phase':'IN_GAME','active_games':2}
        result=public(project({'state':'storage_error','pending':True},self.cursor,2,
                              (state,100,self.catalog)),101,3)
        self.assertEqual(result['state'],'storage_error')
        self.assertIsNone(result['status_code'])
        self.assertEqual(result['active_matches'],2)

    def test_full_without_pending_expires_to_waiting(self):
        state={**self.state,'phase':'IN_GAME','active_games':2}
        cached=project({'state':'waiting_native_ipc','pending':False},self.cursor,2,
                       (state,100,self.catalog))
        self.assertEqual(public(cached,101,3)['status_code'],'CAPACITY_FULL')
        expired=public(cached,104,3)
        self.assertEqual(expired['state'],'waiting_native_ipc')
        self.assertIsNone(expired['status_code'])

    def test_missing_inputs_explicitly_unavailable_and_console_is_not_player_event(self):
        status=public({'state':'initializing'},10,3)
        for key in ('map_cycle','index','current_preset','next_preset','active_matches','finished_matches','error_count'):
            self.assertIsNone(status[key])
        rendered=OperatorEvents().prose('cycle_monitor',{'position':1,'total':2,'current':'one','next_preset':'two',
                                                          'active':2,'maximum':2,'capacity_full':True})
        self.assertEqual(rendered[0:2],('INFO','supervisor'))
        self.assertIn('capacity full',rendered[2])
        self.assertNotIn('Player joined:',rendered[2])

    def test_http_status_returns_same_cycle_contract(self):
        cycle=public(project({'state':'waiting_native_ipc'},self.cursor,2,(self.state,100,self.catalog)),101,3)
        class Supervisor:
            config={'http':{'host':'127.0.0.1','port':0}}
            running=True
            def snapshot(self):return {'ready':False,'cycle':cycle}
        server=serve(Supervisor())
        try:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            connection.request('GET','/status');response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read())['cycle'],cycle)
            connection.close()
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
