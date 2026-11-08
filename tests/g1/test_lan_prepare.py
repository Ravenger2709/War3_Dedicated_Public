import importlib.util
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('lan_prepare',ROOT/'scripts/g1-lan-prepare.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
class LANPreparation(unittest.TestCase):
 def test_scope_hash_and_no_account_or_vpn(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);mapfile=root/'fixture.w3x';mapfile.write_bytes(b'fixture')
   session=root/'session';result=helper.prepare(session,mapfile,'10.20.30.40/24')
   self.assertEqual(result['lan_broadcast'],'10.20.30.255')
   self.assertEqual(result['tcp_port'],6114);self.assertFalse(result['vpn_used'])
   cfg=(session/'ghost.cfg').read_text()
   self.assertIn('bot_g1_bridge_mode = 1\n',cfg);self.assertIn('autohost_startplayers = 1\n',cfg)
   self.assertIn('bot_bindaddress = 10.20.30.40\n',cfg)
   self.assertIn('bnet_server =\n',cfg);self.assertNotIn('password',cfg.lower())
   before=(session/'ghost.cfg').read_bytes()
   with self.assertRaises(ValueError):helper.prepare(session,mapfile,'10.20.30.40/24')
   self.assertEqual((session/'ghost.cfg').read_bytes(),before)
 def test_bad_interface_or_name_creates_nothing(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);mapfile=root/'fixture.w3x';mapfile.write_bytes(b'fixture')
   for interface in ('127.0.0.1/24','8.8.8.8/24','10.20.30.40/16'):
    with self.subTest(interface=interface),self.assertRaises(ValueError):
     helper.prepare(root/'session',mapfile,interface)
    self.assertFalse((root/'session').exists())
if __name__=='__main__':unittest.main()
