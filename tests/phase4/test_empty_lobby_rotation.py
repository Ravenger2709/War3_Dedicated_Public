"""Idle close-v3 is an atomic, replay-fenced skip, never a played match."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from amp import production
from src.runtime.config import gameplay_config
from src.runtime.catalog_requests import CatalogRequests
from src.runtime.cycle_state import CycleState
from src.runtime.cycle_coordinator import CycleCoordinator
from src.runtime.cycle_driver import CycleDriver
from src.runtime.lifecycle import _parse, LifecycleJournalError

spec=importlib.util.spec_from_file_location('idle_request_fixture',Path(__file__).with_name('test_catalog_requests.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


class IdleStateTests(unittest.TestCase):
    setUp=fixture.CatalogRequestsTests.setUp
    reload=fixture.CatalogRequestsTests.reload
    ack=fixture.CatalogRequestsTests.ack
    def bound(self):
        request=self.q.enqueue(self.cursor);self.q.dispatch(self.cursor);self.ack(request);self.q.dispatch(self.cursor)
        self.q.bind_reservation(self.cursor,request.request_id,7,self.journal)
        return {'version':3,'event':'lobby_closed','session_generation':self.gen,'host_counter':7,
                'request_id':request.request_id,'manifest_sha256':self.q.digest,'catalog_index':request.index,
                'reason':'empty_timeout'}

    def test_idle_close_advances_once_without_synthetic_game_or_lost_history(self):
        event=self.bound();last=self.cursor.state['last_started']
        self.assertTrue(self.q.consume_catalog_close(self.cursor,event))
        self.assertEqual(self.cursor.state['index'],1);self.assertIsNone(self.cursor.state['pending'])
        self.assertEqual(self.cursor.state['last_started'],last)
        self.assertTrue(self.cursor._find_idle(self.gen,7));self.assertFalse(self.reload().consume_catalog_close(self.cursor,event))
        self.cursor=CycleState(self.root/'cursor.json',['one','one'])
        self.assertFalse(self.reload().consume_catalog_close(self.cursor,event));self.assertEqual(self.cursor.state['index'],1)
        with sqlite3.connect(self.cursor.db_path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone(),(0,))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone(),(3,))
        start={'version':1,'event':'game_started','session_generation':self.gen,'host_counter':7,'match_id':'b'*32}
        with self.assertRaises(ValueError):self.cursor.consume_external(start)

    def test_crash_between_atomic_cursor_commit_and_queue_update_replays(self):
        event=self.bound()
        with patch.object(self.q,'_save',side_effect=OSError('simulated queue failure')):
            with self.assertRaises(OSError):self.q.consume_catalog_close(self.cursor,event)
        self.assertEqual(self.cursor.state['index'],1)
        self.assertTrue(self.reload().consume_catalog_close(self.cursor,event))
        self.assertEqual(self.cursor.state['index'],1);self.assertIsNone(self.reload().data['pending'])

    def test_closed_pair_reason_and_identity_cannot_change_on_replay(self):
        event=self.bound();self.q.consume_catalog_close(self.cursor,event)
        normal={k:v for k,v in event.items() if k!='reason'};normal['version']=2
        with self.assertRaises(ValueError):self.cursor.consume_close(normal)
        with self.assertRaises(ValueError):self.cursor.consume_close({**event,'request_id':'f'*32})
        with self.assertRaises(ValueError):self.cursor.consume_close({**event,'reason':'admin'})

    def test_foreign_idle_close_cannot_skip_another_reservation(self):
        event=self.bound();before=dict(self.cursor.state)
        for change in [{'host_counter':8},{'request_id':'d'*32},{'catalog_index':1},
                       {'session_generation':'e'*32},{'manifest_sha256':'f'*64}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.q.consume_catalog_close(self.cursor,{**event,**change})
            self.assertEqual(self.cursor.state,before)

    def test_scan_ack_and_late_replay_do_not_skip_the_next_selection(self):
        event=self.bound();p=self.journal/f'close-{self.gen}-7.json';p.write_text(json.dumps(event));p.chmod(0o600)
        driver=CycleDriver(self.q,self.cursor,self.journal)
        result=driver.step();self.assertEqual(result.state,'idle_rotated')
        self.assertTrue(p.with_suffix('.ack').exists());self.assertEqual(self.cursor.state['index'],1)
        newer=self.q.enqueue(self.cursor)
        p.write_text(json.dumps(event));p.chmod(0o600)
        CycleCoordinator(self.cursor,self.journal,catalog_requests=self.q).consume()
        self.assertEqual(self.cursor.state['index'],1);self.assertEqual(self.q.data['pending']['request_id'],newer.request_id)

    def test_normal_close_after_idle_preserves_schema_and_does_not_skip(self):
        event=self.bound();self.q.consume_catalog_close(self.cursor,event)
        self.cursor.consume_close({'version':1,'event':'lobby_closed','session_generation':'c'*32,'host_counter':10})
        self.assertEqual(self.cursor.state['index'],1)
        self.assertTrue(self.cursor._find_idle(self.gen,7));self.assertTrue(self.cursor._find_closed('c'*32,10))
        self.assertEqual(CycleState(self.root/'cursor.json',['one','one']).state['index'],1)

    def test_storage_failure_rolls_back_fence_and_position_together(self):
        event=self.bound();old=dict(self.cursor.state)
        # SQLite trigger aborts after fence inserts but before state update.
        with sqlite3.connect(self.cursor.db_path) as db:
            db.execute("CREATE TRIGGER fail_idle BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.DatabaseError):self.q.consume_catalog_close(self.cursor,event)
        loaded=CycleState(self.root/'cursor.json',['one','one'])
        self.assertEqual(loaded.state,old);self.assertFalse(loaded._find_closed(self.gen,7))


class SettingsAndParserTests(unittest.TestCase):
    def test_minutes_default_and_amp_runtime_validation_parity(self):
        old={k:v for k,v in production.DEFAULTS.items() if k!='empty_lobby_rotation_minutes'}
        self.assertEqual(production.settings(old)['empty_lobby_rotation_minutes'],10)
        for n in [0,1,10,1440]:
            values=production.settings({**old,'empty_lobby_rotation_minutes':str(n)})
            self.assertEqual(values['empty_lobby_rotation_minutes'],n)
            self.assertEqual(gameplay_config(values)['empty_lobby_rotation_minutes'],n)
        for n in [-1,1441,True,False,1.5,'1.5','bad']:
            for function in (lambda:production.settings({**old,'empty_lobby_rotation_minutes':n}),
                             lambda:gameplay_config({'empty_lobby_rotation_minutes':n})):
                with self.assertRaises(ValueError):function()

    def test_amp_field_is_global_minutes_with_ten_minute_default(self):
        fields=json.loads((Path(__file__).resolve().parents[2]/'amp/war3-productionconfig.json').read_text())
        field=[v for v in fields if v['ParamFieldName']=='empty_lobby_rotation_minutes']
        self.assertEqual(len(field),1);self.assertEqual(field[0]['DefaultValue'],'10')
        self.assertIn('minutes',field[0]['DisplayName']);self.assertEqual(field[0]['InputType'],'number')

    def test_native_compiler_receives_seconds(self):
        from src.runtime.native import prepare_native
        from src.runtime.config import iccup_config
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config';config.mkdir(mode=0o700);(config/'maps').mkdir(mode=0o700)
            source=config/'ghost.cfg';source.write_text('bnet_server =\nbot_defaultmap = selected.cfg\n')
            cfg={'runtime_dir':str(root/'state/supervisor'),'hostbot':{'cwd':str(root)},
                 'iccup':{**iccup_config({}),'native_config':str(source),'bridge_dir':str(root/'bridge')},
                 'gameplay':gameplay_config({})}
            for n in (0,1,10,1440):
                cfg['gameplay']['empty_lobby_rotation_minutes']=n
                self.assertIn('bot_empty_lobby_rotation_seconds = '+str(n*60)+'\n',prepare_native(cfg,'SyntheticUser').read_text())

    def test_only_exact_version_three_idle_reason_is_accepted(self):
        event={'version':3,'event':'lobby_closed','session_generation':'a'*32,'host_counter':7,
               'request_id':'b'*32,'manifest_sha256':'c'*64,'catalog_index':0,'reason':'empty_timeout'}
        name='close-'+'a'*32+'-7.json'
        self.assertEqual(_parse(name,json.dumps(event).encode()).reason,'empty_timeout')
        for change in [{'reason':'admin'},{'reason':True},{'version':2},{'extra':1}]:
            with self.assertRaises(LifecycleJournalError):_parse(name,json.dumps({**event,**change}).encode())


if __name__=='__main__':unittest.main()
