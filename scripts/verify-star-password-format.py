#!/usr/bin/env python3
"""Credential-free comparison of ordinary password hashing/cache arithmetic.

Never loads the PE into Windows, contacts a server, handles a real password,
or executes Guard/attestation code. Only the two bounded arithmetic routines
from the inspected Launcher image are CPU-emulated against synthetic vectors.
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import stat
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_BLOCK
from unicorn.x86_const import UC_X86_REG_ECX,UC_X86_REG_EDX,UC_X86_REG_ESP,UC_X86_REG_EBP,UC_X86_REG_EIP,UC_X86_REG_ESI,UC_X86_REG_EBX

BASE,STACK,DATA,END=0x400000,0x60000000,0x61000000,0x62000000
EXPECTED_IMAGE='99370097e7058e0687402b4c77a6a5bf7e990e7063cf66bdd3a406f50af762f1'
from src.bridge.star_profile import cache_block, encode_saved, provision


def machine(raw,ranges):
    mu=Uc(UC_ARCH_X86,UC_MODE_32)
    mu.mem_map(BASE,(len(raw)+4095)&~4095);mu.mem_write(BASE,raw)
    mu.mem_map(STACK,0x100000);mu.mem_map(DATA,0x20000);mu.mem_map(END,4096)
    def bound(uc,address,size,_):
        if not any(lo<=address and address+size<=hi for lo,hi in ranges):
            uc.emu_stop();raise RuntimeError('execution left verified arithmetic ranges at '+hex(address))
    mu.hook_add(UC_HOOK_BLOCK,bound)
    return mu


def star_digest(raw,password):
    mu=machine(raw,[(0x470000,0x471435),(0x5a06a9,0x5a06f0)])
    sp=STACK+0xff000
    mu.mem_write(sp,struct.pack('<II',END,DATA))
    mu.mem_write(DATA,password+b'\0')
    mu.reg_write(UC_X86_REG_ESP,sp);mu.reg_write(UC_X86_REG_ECX,DATA+0x10000);mu.reg_write(UC_X86_REG_EDX,len(password))
    mu.emu_start(0x471380,END,timeout=2000000,count=2000000)
    if mu.reg_read(UC_X86_REG_EIP)!=END:raise RuntimeError('digest did not return within budget')
    return bytes(mu.mem_read(DATA+0x10000,20))


def star_cache_block(raw,digest):
    mu=machine(raw,[(0x425054,0x4251b4)])
    frame=STACK+0x80000
    mu.reg_write(UC_X86_REG_EBP,frame)
    mu.mem_write(frame+0xc,struct.pack('<I',DATA))
    mu.mem_write(frame-0x3c,struct.pack('<I',DATA+0x1000))
    mu.mem_write(frame-0x38,struct.pack('<I',DATA+0x1000+37))
    mu.mem_write(DATA,digest)
    mu.emu_start(0x425054,0x4251b4,timeout=1000000,count=100000)
    if mu.reg_read(UC_X86_REG_EIP)!=0x4251b4:raise RuntimeError('cache block budget exceeded')
    return bytes(mu.mem_read(DATA+0x1000,37))


def star_username_encoding(raw,username,digest):
    mu=machine(raw,[(0x4251c0,0x4251db)])
    frame=STACK+0x80000;user=username.encode('utf-8');buf=DATA+0x1000
    mu.reg_write(UC_X86_REG_EBP,frame)
    if len(user)<16:
        mu.mem_write(frame+0x10,user+b'\0')
        capacity=15
    else:
        mu.mem_write(DATA+0x4000,user+b'\0')
        mu.mem_write(frame+0x10,struct.pack('<I',DATA+0x4000))
        capacity=len(user)
    mu.mem_write(frame+0x20,struct.pack('<II',len(user),capacity))
    mu.mem_write(buf,cache_block(digest))
    for i in range(37):
        mu.reg_write(UC_X86_REG_ESI,i);mu.reg_write(UC_X86_REG_EBX,buf)
        mu.emu_start(0x4251c0,0x4251db,timeout=100000,count=1000)
    return bytes(mu.mem_read(buf,37)).hex()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',required=True,type=Path)
    p.add_argument('--bncsutil',default='/opt/war3/lib/libbncsutil.so')
    p.add_argument('--launcher',required=True,type=Path)
    a=p.parse_args();raw=a.image.read_bytes()
    if len(raw)!=31211520 or hashlib.sha256(raw).hexdigest()!=EXPECTED_IMAGE:raise SystemExit('unexpected inspected image size')
    lib=ctypes.CDLL(a.bncsutil);lib.hashPassword.argtypes=[ctypes.c_char_p,ctypes.c_void_p];lib.hashPassword.restype=None
    vectors=[b'a',b'abc',b'password',b'PaSs42',b'A'*63,b'B'*64]
    results=[]
    for password in vectors:
        native=ctypes.create_string_buffer(20);lib.hashPassword(password,native)
        digest=star_digest(raw,password)
        assert digest==native.raw,'ordinary password digest differs from BNCSutil'
        assert star_cache_block(raw,digest)==cache_block(digest),'saved-login arithmetic mismatch'
        for username in ('Probe','War3OfflineProbe','Игрок'):
            assert star_username_encoding(raw,username,digest)==encode_saved(username,digest),'username encoding differs'
        results.append({'synthetic_input_sha256':hashlib.sha256(password).hexdigest(),'bytes':len(password),'digest_matches_bncsutil':True,'cache_block_matches_original':True})
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);secret=root/'credentials.json';out=root/'profile';out.mkdir(mode=0o700)
        secret.write_text(json.dumps({'version':1,'username':'SyntheticProfile','password':'OfflineProfileVector42!'}));secret.chmod(0o600)
        defaults=root/'defaults.ini';defaults.write_text('[General]\nConnections=\nCurrConnections=\nSiteConnections=\n')
        provision(secret,out,defaults,a.launcher,a.bncsutil)
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()}
        assert all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in out.iterdir())
        assert b'OfflineProfileVector42!' not in (out/'launcher.ini').read_bytes()
        try:
            provision(secret,out,defaults,a.launcher,a.bncsutil)
        except FileExistsError:pass
        else:raise AssertionError('profile overwrite accepted')
        assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()}
    print(json.dumps({'result':'PASS','image_sha256':hashlib.sha256(raw).hexdigest(),'vectors':results,'username_encoding_checks':18,'profile_mode_and_no_overwrite':'PASS','real_credentials_used':False,'network':'none','windows_loader_used':False,'guard_or_attestation_emulated':False},indent=2))

if __name__=='__main__':main()
