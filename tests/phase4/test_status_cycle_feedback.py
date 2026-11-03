"""Display sidecar construction from an already owned/fresh observation seam."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.runtime.controller import ICCupSupervisor
from src.runtime.files import atomic_text


class CycleChatFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.s=object.__new__(ICCupSupervisor)
        self.s.config={'iccup':{'bridge_dir':str(self.root),'metadata_ttl':3,'native_state_ttl':15}}
        self.s.last_feedback=0.;self.s.relay_observation={};self.s.cycle_session={};self.s.cycle_failed=False
        self.s.snapshot=lambda:{'realm_connected':False,'iccup_authenticated':False,
                                'lobby_publication_accepted':False,'relay_healthy':False}
        self.record={'request_id':'b'*32,'index':0,'position':0,'counter':7,'status':'staged'}
        self.binding={'session_generation':'a'*32,'host_counter':7}
        self.state={'index':0,'pending':self.binding,'last_started':None}
        self.cursor=SimpleNamespace(state=self.state,_write_failed=False)
        self.catalog=SimpleNamespace(generation='a'*32,digest='c'*64,indices=(0,1,0),failed=False,
                                     data={'pending':self.record,'completed':None})
        self.s.cycle_control=SimpleNamespace(catalog=self.catalog,state=self.cursor)
        self.event={'phase':'LOBBY','session_generation':'a'*32,'host_counter':7,
                    'catalog_request_id':'b'*32,'catalog_index':0,'active_games':2}
        self.stamp=time.monotonic()-.1
        self.s._cycle_current_observation=Mock(return_value=(object(),self.event,self.stamp))
        self.path=self.root/'native-feedback.cfg.cycle'

    def feedback(self):
        self.s._feedback(force=True)
        return dict(line.split(' = ',1) for line in self.path.read_text().splitlines())

    def test_matched_lobby_writes_only_bound_ids_and_original_timestamp(self):
        data=self.feedback()
        self.assertEqual(data,{'version':'1','observed_monotonic_ms':str(int(self.stamp*1000)),
            'ttl_ms':'10000','session_generation':'a'*32,'manifest_sha256':'c'*64,
            'request_id':'b'*32,'host_counter':'7','catalog_index':'0','cycle_position':'0'})
        self.s._cycle_current_observation.assert_called_once_with(allow_full=True,monitoring=True)
        self.assertNotIn('active_games',data)  # Counts must come from the native process.
        self.assertNotIn('next_preset',data)  # Native resolves validated cycle indices.

    def test_repeated_id_uses_exact_request_position(self):
        self.record['position']=2;self.state['index']=2
        self.assertEqual(self.feedback()['cycle_position'],'2')

    def test_in_game_requires_committed_start_and_completed_request(self):
        self.event['phase']='IN_GAME'
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})  # A staged ACK is insufficient.
        self.catalog.data.update(pending=None,completed=self.record)
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})
        self.state.update(index=1,pending=None,last_started=self.binding)
        self.assertEqual(self.feedback()['cycle_position'],'0')
        self.state['last_started']={**self.binding,'host_counter':8}
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})

    def test_mismatched_or_uncertain_input_withdraws_metadata(self):
        for key,value in (('counter',8),('request_id','d'*32),('index',1),('position',1),('status','uncertain')):
            with self.subTest(key=key):
                original=self.record[key];self.record[key]=value
                self.assertEqual(self.feedback(),{'version':'1','available':'0'})
                self.record[key]=original
        self.s.cycle_failed=True
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})
        self.s.cycle_failed=False;self.cursor._write_failed=True
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})

    def test_missing_stale_or_unowned_observation_does_not_refresh_old_metadata(self):
        self.feedback()
        self.s._cycle_current_observation.return_value=None
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})
        self.s._cycle_current_observation.side_effect=RuntimeError('lease changed')
        self.assertEqual(self.feedback(),{'version':'1','available':'0'})
        self.assertFalse(self.s.cycle_failed)

    def test_optional_write_failure_cannot_gate_gameplay(self):
        def write(path,content):
            if str(path).endswith('.cycle'):raise PermissionError('offline injected display outage')
            atomic_text(path,content)
        with patch('src.runtime.controller.atomic_text',side_effect=write):
            self.s._feedback(force=True)
        self.assertTrue((self.root/'native-feedback.cfg').is_file())
        self.assertFalse(self.s.cycle_failed)
