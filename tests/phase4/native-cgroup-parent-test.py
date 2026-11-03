#!/usr/bin/env python3
"""Exercise the native cgroup walker against isolated synthetic files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('native_cgroup_fixture',ROOT/'tests/phase3/native-test.py')
gameplay=importlib.util.module_from_spec(spec);spec.loader.exec_module(gameplay)

class CgroupParentTests(unittest.TestCase):
    setUp=gameplay.GameplayTests.setUp
    tearDown=gameplay.GameplayTests.tearDown
    native=gameplay.GameplayTests.native

    def test_finite_parent_dominates_and_bad_hierarchy_fails_closed(self):
        n=self.native();n.start()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);parent=root/'parent';leaf=parent/'child';leaf.mkdir(parents=True)
            (parent/'memory.max').write_text('268435456\n')
            (parent/'memory.current').write_text('104857600\n')
            (leaf/'memory.max').write_text('536870912\n')
            (leaf/'memory.current').write_text('10485760\n')
            (leaf/'cgroup.procs').write_text(str(n.proc.pid)+'\n')
            def check(expected):
                command=f'cgroupfake {root} /parent/child'
                n.proc.stdin.write(command+'\n');n.proc.stdin.flush()
                result=n.marker('TEST CGROUPFAKE ').split();n.marker('TEST COMMAND '+command)
                self.assertEqual(tuple(map(int,result[-2:])),expected)
            check((1,163577856))
            (parent/'memory.max').write_text('max\n');check((1,526385152))
            (parent/'memory.max').write_text('268435456\n')
            for value in ('broken\n','0\n','50\n'):
                (parent/'memory.max').write_text(value);check((0,0))
            (parent/'memory.max').write_text('max\n');(leaf/'memory.max').write_text('max\n');check((0,0))
            (leaf/'memory.max').write_text('536870912\n');(leaf/'cgroup.procs').write_text('99999999\n');check((0,0))
            (leaf/'cgroup.procs').write_text(str(n.proc.pid)+'\n');(leaf/'memory.max').unlink()
            (leaf/'memory.max').symlink_to('/dev/null');check((0,0))

if __name__=='__main__':unittest.main()
