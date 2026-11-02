"""Welcome text uses the native reader, with bounded snapshots and safe fallback."""
import os
from pathlib import Path
import tempfile
import unittest

from src.runtime.motd import default_lines,prepare_motd
from src.runtime.native import prepare_native
from src.runtime.config import gameplay_config,iccup_config


class MOTDTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=self.root/'config';self.config.mkdir(mode=0o700)
        self.state=self.root/'native';self.state.mkdir(mode=0o700)
        self.source=self.config/'ghost.cfg';self.source.write_text('bot_defaultmap = selected.cfg\nbot_motdfile =\n')
        self.output=self.state/'motd.txt'
        self.custom=self.config/'motd.txt'

    def prepare(self):return prepare_motd(self.source,self.output,'GreenZone_Bot')

    def test_default_english_guide_has_command_definitions(self):
        self.assertEqual(self.prepare(),str(self.output))
        lines=self.output.read_text().splitlines()
        self.assertEqual(lines,default_lines('GreenZone_Bot'))
        self.assertEqual([line.split(' - ')[0] for line in lines if line.startswith('!')],
                         ['!help','!status','!vote','!kick <player>'])
        self.assertIn('!kick <player> - Start or support a vote to remove a player from this match (70% of eligible human voters; at least two votes).',lines)
        self.assertEqual(lines[-1],'Type !help to see available server commands.')
        self.assertLessEqual(len(lines),8)
        self.assertTrue(all(len(line.encode('utf-8'))<=200 for line in lines))
        self.assertTrue(all(line.isascii() for line in lines))
        self.assertEqual(self.output.stat().st_mode&0o777,0o600)

    def test_custom_windows_lines_are_snapshotted_and_limited_to_eight(self):
        self.custom.write_bytes('\r\n'.join('Line '+str(i) for i in range(10)).encode())
        self.prepare();expected=self.output.read_bytes()
        self.assertEqual(len(expected.splitlines()),8)
        self.assertNotIn(b'\r',expected)
        self.custom.write_text('Later edit')
        self.assertEqual(self.output.read_bytes(),expected)

    def test_empty_file_disables_welcome(self):
        self.custom.write_bytes(b'');self.prepare()
        self.assertEqual(self.output.read_bytes(),b'')

    def test_custom_welcome_is_preserved_without_appending_default_commands(self):
        original=b'Operator welcome\r\nRead our match rules.\r\n'
        self.custom.write_bytes(original)
        self.prepare()
        self.assertEqual(self.output.read_bytes(),b'Operator welcome\nRead our match rules.\n')
        self.assertEqual(self.custom.read_bytes(),original)
        self.assertNotIn('!kick',self.output.read_text())
        self.assertNotIn('!help',self.output.read_text())

    def test_invalid_file_or_symlink_falls_back_without_disclosing_target(self):
        for raw in (b'\xff',b'x'*4097,b'bad\x00text',b'x'*201):
            with self.subTest(raw_length=len(raw)):
                self.custom.write_bytes(raw);self.prepare()
                self.assertEqual(self.output.read_text().splitlines(),default_lines('GreenZone_Bot'))
        self.custom.unlink();self.custom.symlink_to(self.source)
        self.prepare()
        self.assertEqual(self.output.read_text().splitlines(),default_lines('GreenZone_Bot'))

    def test_fifo_is_not_read_and_does_not_block(self):
        os.mkfifo(self.custom);self.prepare()
        self.assertEqual(self.output.read_text().splitlines(),default_lines('GreenZone_Bot'))

    def test_native_config_wires_motd_and_retains_explicit_legacy_path(self):
        cfg={'runtime_dir':str(self.root/'runtime'),'hostbot':{'cwd':str(self.root/'data')},
             'gameplay':gameplay_config({'host_name':'GreenZone_Bot'}),
             'iccup':{**iccup_config({}),'native_config':str(self.source),'bridge_dir':str(self.root/'bridge')}}
        path=prepare_native(cfg,'Synthetic')
        text=path.read_text()
        motd=next(line.split(' = ',1)[1] for line in text.splitlines() if line.startswith('bot_motdfile = '))
        self.assertTrue(Path(motd).is_file())
        self.assertIn('!vote - ',Path(motd).read_text())
        self.source.write_text('bot_defaultmap = selected.cfg\nbot_motdfile = /legacy/custom.txt\n')
        self.assertIn('bot_motdfile = /legacy/custom.txt\n',prepare_native(cfg,'Synthetic').read_text())


if __name__=='__main__':unittest.main()
