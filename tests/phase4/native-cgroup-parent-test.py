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
            # The ancestor is almost full after cold replay/log files were
            # cached. Its clean inactive file pages can be reclaimed.
            (parent/'memory.current').write_text('260046848\n')
            cache = ('inactive_file 201326592\nfile 218103808\nshmem 16777216\n'
                     'file_dirty 8388608\nfile_writeback 4194304\nfile_mapped 4194304\n')
            (parent/'memory.stat').write_text(cache)
            check((1,192937984))
            (parent/'memory.stat').write_text(cache.replace('shmem 16777216', 'shmem 201326592'))
            check((1,8388608))
            (parent/'memory.stat').write_text(cache + 'inactive_file 201326592\n')
            check((1,8388608))
            (parent/'memory.stat').unlink()
            check((1,8388608))
            (parent/'memory.current').write_text('104857600\n')
            (parent/'memory.max').write_text('max\n');check((1,526385152))
            (parent/'memory.max').write_text('268435456\n')
            for value in ('broken\n','0\n','50\n'):
                (parent/'memory.max').write_text(value);check((0,0))
            (parent/'memory.max').write_text('max\n');(leaf/'memory.max').write_text('max\n');check((0,0))
            (leaf/'memory.max').write_text('536870912\n');(leaf/'cgroup.procs').write_text('99999999\n');check((0,0))
            (leaf/'cgroup.procs').write_text(str(n.proc.pid)+'\n');(leaf/'memory.max').unlink()
            (leaf/'memory.max').symlink_to('/dev/null');check((0,0))

if __name__=='__main__':unittest.main()
