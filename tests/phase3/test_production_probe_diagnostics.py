"""Pure synthetic diagnostics checks; no online probe, image or authentication."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('online_probe',Path(__file__).with_name('production_online_probe.py'))
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)


class ProbeDiagnosticsTests(unittest.TestCase):
    def test_exact_failed_flags_survive_a_recovered_fresh_snapshot(self):
        observed={'status':{'state':'AUTHENTICATING','ready':False,'iccup_authenticated':False,
                           'star_process_alive':False,'pid':123,'components':{'star':{'alive':True,'pid':456,'generation':1}},
                           'game':{'players':0,'computer_players':6,'host_counter':2}},
                  'status_read_monotonic_seconds':100.001}
        fresh={'state':'LOBBY','ready':True,'iccup_authenticated':True,'star_process_alive':True,'pid':123}
        metadata={'observed_monotonic_seconds':100.002,'worker_pid':456,'generation':1,
                  'phase':'RUNNING','launcher_alive':True,'authenticated':True}
        result=probe.failure_diagnostics(observed,status_reader=lambda:fresh,metadata_reader=lambda _:metadata)
        self.assertIs(result['last_observed']['status']['ready'],False)
        self.assertIs(result['before_teardown']['status']['fields']['ready'],True)
        self.assertEqual(result['before_teardown']['star']['fields']['observed_monotonic_seconds'],100.002)
        self.assertTrue(all(v['read_ok'] for v in result['before_teardown'].values()))

    def test_whitelist_rejects_sensitive_strings_nested_payloads_and_invalid_scalars(self):
        bad='PRIVATE_ACCOUNT_PASSWORD_PACKET_CHAT_PATH'
        value={'state':bad,'phase':bad,'pid':bad,'worker_pid':bad,'ready':bad,'authenticated':bad,
               'observed_monotonic_seconds':float('nan'),'last_ack_monotonic_seconds':float('inf'),
               'refresh_successful_count':2**100,'game_name':bad,'map_name':bad,'reason':bad,'error':bad,
               'username':bad,'password':bad,'packet_hex':bad,'path':bad,'chat':bad,
               'components':{'star':{'alive':True,'pid':55,'generation':1,'argv':[bad]},bad:{'pid':bad}},
               'game':{'players':0,'computer_players':6,'game_name':bad,'map_name':bad,'chat':bad},
               'permanent_errors':{bad:bad}}
        result=probe.failure_diagnostics({'status':value,'relay':value,'star':value},
                                         status_reader=lambda:value,metadata_reader=lambda _:value)
        text=json.dumps(result,allow_nan=False)
        self.assertNotIn(bad,text)
        self.assertLess(len(text.encode()),16384)
        self.assertIs(result['last_observed']['status']['permanent_errors_present'],True)
        self.assertIs(result['last_observed']['relay']['error_present'],True)
        self.assertNotIn('observed_monotonic_seconds',result['last_observed']['star'])
        self.assertNotIn('ready',result['last_observed']['status'])
        self.assertEqual(probe.diagnostic_fields({'stamp':2**100000},times=('stamp',)),{})

    def test_capture_failures_are_bounded_and_never_echo_exception_contents(self):
        def unavailable(*_):raise RuntimeError('PRIVATE_ACCOUNT_PASSWORD_PACKET_CHAT_PATH')
        result=probe.failure_diagnostics({},status_reader=unavailable,metadata_reader=unavailable)
        self.assertFalse(any(v['read_ok'] for v in result['before_teardown'].values()))
        self.assertNotIn('PRIVATE_',json.dumps(result))
        with self.assertRaises(ValueError):probe.diagnostic_json(b' '*(probe.DIAGNOSTIC_BYTES+1))
        with self.assertRaises(ValueError):probe.diagnostic_json(b'[]')
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'metadata.json';path.write_text('{"worker_pid":123}')
            self.assertEqual(probe.diagnostic_metadata(path),{'worker_pid':123})
            path.write_bytes(b' '*(probe.DIAGNOSTIC_BYTES+1))
            with self.assertRaises(ValueError):probe.diagnostic_metadata(path)


if __name__=='__main__':unittest.main()
