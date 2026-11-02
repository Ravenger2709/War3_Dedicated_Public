"""Strict telemetry parser and manifest-bound, expiring status projection."""
import json
from types import SimpleNamespace
import unittest

from src.runtime.native import native_state
from src.runtime.cycle_monitor import project,public


class MatchTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.matches=[{'host_counter':4,'catalog_index':0,'phase':'loading','match_id':'c'*32},
                      {'host_counter':5,'catalog_index':1,'phase':'playing','match_id':'d'*32}]
        self.telemetry={'version':1,'active_count':2,'retired_count':3,'rotation_error_count':1,
                        'matches':self.matches}
        self.state={'version':1,'phase':'IN_GAME','session_generation':'a'*32,
                    'active_games':2,'matches_v1':self.telemetry}
        self.cursor=SimpleNamespace(cycle=['one','two'],state={'index':0})
        self.catalog=SimpleNamespace(generation='a'*32,entries=(('one','1'*64),('two','2'*64)),data={'pending':None})

    def parse(self,obj):return native_state('[HOSTBOT_STATE] '+json.dumps(obj))

    def test_two_matches_are_bound_to_manifest_and_expire(self):
        parsed=self.parse(self.state)
        result=project({'state':'waiting_native_ipc'},self.cursor,2,(parsed,100,self.catalog))
        self.assertEqual(result['state'],'capacity_full')
        self.assertEqual(result['active_match_list'],[
            {**self.matches[0],'session_generation':'a'*32,'preset_id':'one','map_sha256':'1'*64},
            {**self.matches[1],'session_generation':'a'*32,'preset_id':'two','map_sha256':'2'*64}])
        self.assertEqual((result['finished_matches'],result['rotation_errors']),(3,1))
        self.assertEqual(result['availability']['finished_matches'],'verified_process_snapshot')
        self.assertIsNone(result['error_count'])
        expired=public(result,104,3)
        for key in ('active_match_list','finished_matches','rotation_errors'):
            self.assertIsNone(expired[key]);self.assertEqual(expired['availability'][key],'unavailable')
        self.assertEqual(result['active_match_list'][0]['phase'],'loading')

    def test_invalid_or_unavailable_catalog_never_invents_mapping(self):
        parsed=self.parse(self.state)
        self.catalog.entries=self.catalog.entries[:1]
        result=project({'state':'waiting_native_ipc'},self.cursor,2,(parsed,100,self.catalog))
        self.assertIsNone(result['active_match_list']);self.assertIsNone(result['finished_matches'])
        no=project({'state':'waiting_native_ipc'},self.cursor,2,None)
        self.assertIsNone(no['error_count'])
        entry={**self.matches[0],'catalog_index':None}
        parsed=self.parse({**self.state,'active_games':1,'matches_v1':{**self.telemetry,'active_count':1,'matches':[entry]}})
        result=project({},self.cursor,2,(parsed,100,self.catalog))
        self.assertIsNone(result['active_match_list'][0]['preset_id'])
        self.assertIsNone(result['active_match_list'][0]['map_sha256'])

    def test_old_or_unconfirmed_match_ids_remain_null(self):
        old=dict(self.matches[0]);old.pop('match_id')
        no_id={**self.matches[1],'match_id':None}
        parsed=self.parse({**self.state,'matches_v1':{**self.telemetry,'matches':[old,no_id]}})
        result=project({},self.cursor,2,(parsed,100,self.catalog))
        self.assertEqual([item['match_id'] for item in result['active_match_list']],[None,None])

    def test_strict_grammar_rejects_duplicates_bad_types_and_inconsistent_count(self):
        variants=[{'version':2},{'active_count':1},{'active_count':True}, {'retired_count':-1},
                  {'rotation_error_count':True},{'retired_count':2**64-1},
                  {'matches':[self.matches[0],self.matches[0]]},
                  {'matches':[self.matches[0],{**self.matches[1],'host_counter':0}]},
                  {'matches':[self.matches[0],{**self.matches[1],'catalog_index':32}]},
                  {'matches':[self.matches[0],{**self.matches[1],'phase':'completed'}]},
                  {'matches':[self.matches[0],{**self.matches[1],'match_id':'C'*32}]},
                  {'matches':[self.matches[0],{**self.matches[1],'match_id':'c'*32}]},
                  {'unexpected':'private'}]
        for changes in variants:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.parse({**self.state,'matches_v1':{**self.telemetry,**changes}})
        for key in ('session_generation','active_games'):
            value=dict(self.state);value.pop(key)
            with self.subTest(missing=key),self.assertRaises(ValueError):self.parse(value)
        old=self.parse({'version':1,'phase':'IDLE'})
        self.assertNotIn('matches_v1',old)

if __name__=='__main__':unittest.main()
