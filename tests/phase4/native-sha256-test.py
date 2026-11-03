#!/usr/bin/env python3
"""Portable native map SHA-256 and streaming vectors, independent of AF_ALG."""
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('sha_native_fixture', ROOT/'tests/phase3/native-test.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class PortableSHA256Tests(unittest.TestCase):
    def test_known_vectors_padding_chunks_and_rejected_digests(self):
        cases = [b'', b'abc', b'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', b'a'*1000000]
        cases += [bytes((i*131+17)%256 for i in range(n))
                  for n in (1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 129, 65535, 65536, 65537, 131073)]
        self.assertEqual(hashlib.sha256(cases[0]).hexdigest(), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
        self.assertEqual(hashlib.sha256(cases[1]).hexdigest(), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
        native = fixture.GameplayTests('runTest'); native.setUp()
        self.addCleanup(native.doCleanups); self.addCleanup(native.tearDown)
        fds = []
        for data in cases:
            fd=os.memfd_create('native-sha-vector',os.MFD_ALLOW_SEALING)
            fds.append(fd)
            view=memoryview(data)
            while view: view=view[os.write(fd,view):]
            fcntl.fcntl(fd,fcntl.F_ADD_SEALS,fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL)
        self.addCleanup(lambda:[os.close(fd) for fd in fds])
        n=native.native(); n.passed_fds=tuple(fds); n.start()
        for data,fd in zip(cases,fds):
            digest=hashlib.sha256(data).hexdigest()
            for chunk in (1,7,55,64,65,65536):
                with self.subTest(length=len(data),chunk=chunk):
                    command=f'hashvector {fd} {digest} {chunk}'
                    n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
                    self.assertEqual(n.marker('TEST HASHVECTOR ').split()[-4:],['1','1','1','1'])
                    n.marker('TEST COMMAND '+command)
            for wrong in ('0'*64,digest.upper(),'x'*64,digest[:-1]):
                command=f'hashvector {fd} {wrong} 64'
                n.proc.stdin.write(command+'\n'); n.proc.stdin.flush()
                self.assertEqual(n.marker('TEST HASHVECTOR ').split()[-4:],['0','0','1','1'])
                n.marker('TEST COMMAND '+command)


if __name__=='__main__': unittest.main()
