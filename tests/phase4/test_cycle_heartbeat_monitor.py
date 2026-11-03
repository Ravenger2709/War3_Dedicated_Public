"""Native heartbeat monitoring must not share the shorter admission deadline."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.runtime.controller import ICCupSupervisor
from src.runtime.operator_logs import OperatorEvents, OperatorLogMixin


class NativeHeartbeatMonitoringTests(unittest.TestCase):
    def service(self):
        s=object.__new__(ICCupSupervisor)
        owner=SimpleNamespace(is_running=lambda:True)
        child=SimpleNamespace(started=90,session_owner=owner)
        lease=SimpleNamespace(_native_reader=SimpleNamespace(child=owner),
                              _native_generation='a'*32,require_native=Mock())
        s.cycle_control=SimpleNamespace(driver=object(),lease=lease,max_games=3,check_ipc=Mock())
        s.children={'native':child};s.cycle_session_read=(child,90)
        s.cycle_observation=(child,{'session_generation':'a'*32,'uptime_seconds':10,'active_games':0},100)
        s.config={'iccup':{'metadata_ttl':3,'native_state_ttl':15}}
        return s

    def observed(self,s,now,monitoring):
        with patch('src.runtime.controller.time.monotonic',return_value=now):
            return s._cycle_current_observation(allow_full=True,monitoring=monitoring)

    def test_four_second_gap_keeps_monitoring_but_never_extends_admission(self):
        s=self.service()
        self.assertIs(self.observed(s,104,True),s.cycle_observation)
        self.assertIsNone(self.observed(s,104,False))
        self.assertIs(self.observed(s,115,True),s.cycle_observation)
        self.assertIsNone(self.observed(s,115.01,True))

    def test_monitoring_retains_process_session_and_buffered_record_checks(self):
        for change in ('dead','wrong_session','wrong_owner','future_stamp','old_buffer'):
            s=self.service();child,event,stamp=s.cycle_observation
            if change=='dead':child.session_owner.is_running=lambda:False
            elif change=='wrong_session':event['session_generation']='b'*32
            elif change=='wrong_owner':s.cycle_control.lease._native_reader.child=object()
            elif change=='future_stamp':s.cycle_observation=(child,event,105)
            else:s.cycle_session_read=(child,70)
            with self.subTest(change=change):self.assertIsNone(self.observed(s,104,True))


class CycleConsoleTests(unittest.TestCase):
    def fields(self,**changes):
        return {'position':1,'total':1,'current':'internal-preset-id','next_preset':'internal-preset-id',
                'cycle':['internal-preset-id'],'lobby':{'preset_id':'internal-preset-id','host_counter':1},
                'lobby_name':'Custom Hero Footies AI','active':0,'maximum':3,
                'finished':0,'rotation_errors':0,'last_transition':None,**changes}

    def test_known_summary_is_short_uses_title_and_has_no_fake_number_or_error(self):
        result=OperatorEvents().prose('cycle_monitor',self.fields())
        self.assertEqual(result,('INFO','supervisor',
            'Map cycle 1/1: lobby Custom Hero Footies AI; active matches 0/3.'))
        for unwanted in ('unavailable','errors','internal-preset-id','#1'):
            self.assertNotIn(unwanted,result[2])

    def test_missing_values_stay_in_journal_without_repeated_console_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            class Recorder(OperatorLogMixin):pass
            r=Recorder();r.runtime=Path(directory);r.guard=threading.RLock();r.output=io.StringIO()
            for _ in range(5):
                r.log('cycle_monitor',**self.fields(active=None,lobby=None))
                r.log('cycle_monitor',**self.fields())
            self.assertEqual(len(r.output.getvalue().splitlines()),1)
            rows=[json.loads(line) for line in (r.runtime/'supervisor.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),10);self.assertIsNone(rows[0]['active'])

    def test_capacity_is_info_and_real_rotation_failure_remains_warning(self):
        full=OperatorEvents().prose('cycle_monitor',self.fields(active=3,capacity_full=True))
        self.assertEqual(full[0],'INFO');self.assertIn('capacity full',full[2])
        error=OperatorEvents().prose('cycle_monitor',self.fields(rotation_errors=1))
        self.assertEqual(error[0],'WARN');self.assertIn('rotation failures 1',error[2])
        blocked=OperatorEvents().prose('cycle_control_blocked',{'reason':'private diagnostics'})
        self.assertEqual(blocked,('ERROR','supervisor','Map cycle needs attention; see diagnostics.'))


if __name__=='__main__':unittest.main()
