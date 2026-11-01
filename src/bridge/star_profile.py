"""Provision a protected saved-login profile for the inspected official Star.

This handles ordinary account password hashing only. It emits no protocol
traffic, Guard replies or attestation and never reads an existing login cache.
The actual authentication is performed by the unmodified vendor application.
"""
import argparse
import configparser
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat

from .secrets import load_credentials

LAUNCHER_SHA256 = '9ce99d541aaef4cb1b2a60a0837580678f81ed0494795efc720fe84c99a6253e'
PERM = {1:1,7:6,19:10,10:0,30:13,3:14,14:2,12:7,32:11,9:12,5:17,
        24:15,2:3,20:8,11:9,6:19,8:18,0:16,13:4,28:5}


def cache_block(digest: bytes) -> bytes:
    if len(digest) != 20:
        raise ValueError('STAR_DIGEST_SIZE_INVALID')
    b = bytearray(37)
    for dst, src in PERM.items():
        b[dst] = digest[src]
    b[4] = b[0] & b[1]
    b[15] = b[14] | b[11]
    b[16] = b[7] & b[2]
    b[18] = b[0] ^ b[4]
    b[21] = b[5] & b[4]
    b[22] = b[6] ^ b[5]
    b[23] = b[6] ^ b[32]
    b[26] = b[30] & b[8]
    b[27] = b[24] ^ b[11]
    b[29] = b[8] & b[2]
    b[31] = b[11] | b[8]
    b[33] = b[12] & b[7]
    b[34] = b[13] | b[30]
    b[35] = b[19] & b[0]
    b[36] = b[24] & b[6]
    checksum = sum(b)
    b[17], b[25] = checksum >> 8, checksum & 255
    return bytes(b)


def encode_saved(username: str, digest: bytes) -> str:
    user = username.encode('utf-8')
    if not user:
        raise ValueError('STAR_CREDENTIAL_ENCODING_UNSUPPORTED')
    block = cache_block(digest)
    return bytes(byte ^ (user[i % len(user)] & 7) for i, byte in enumerate(block)).hex()


def saved_login(username: str, password: str, library: str) -> str:
    value = password.encode('utf-8')
    if not username or not 1 <= len(value) <= 64:
        raise ValueError('STAR_CREDENTIAL_ENCODING_UNSUPPORTED')
    lib = ctypes.CDLL(library)
    lib.hashPassword.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    lib.hashPassword.restype = None
    digest = ctypes.create_string_buffer(20)
    lib.hashPassword(value, digest)
    # Username-dependent encoding used by the vendor's saved-login formatter.
    return encode_saved(username, digest.raw)


def ini_string(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def provision(secret: Path, output: Path, defaults: Path, launcher: Path,
              library: str = '/opt/war3/lib/libbncsutil.so') -> None:
    if hashlib.sha256(launcher.read_bytes()).hexdigest() != LAUNCHER_SHA256:
        raise ValueError('STAR_LAUNCHER_VERSION_UNSUPPORTED')
    info = output.lstat()
    if output.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('STAR_PROFILE_DIRECTORY_UNSAFE')
    if defaults.stat().st_size > 8192:
        raise ValueError('STAR_DEFAULTS_INVALID')
    cfg = configparser.RawConfigParser()
    cfg.optionxform = str
    cfg.read(defaults, encoding='utf-8')
    keys = set(cfg['General']) if cfg.sections() == ['General'] else set()
    if not {'Connections', 'CurrConnections', 'SiteConnections'} <= keys or keys - {'Connections', 'CurrConnections', 'SiteConnections', 'UserConnections'}:
        raise ValueError('STAR_DEFAULTS_INVALID')
    credentials = load_credentials(secret)
    encoded = saved_login(credentials.username, credentials.password, library)
    logins = json.dumps({credentials.username: encoded}, ensure_ascii=False, separators=(',', ':'))
    launcher_config = '[General]\nAutoLogin=true\nUsername=' + ini_string(credentials.username) + '\nPassword=\nListLogins=' + ini_string(logins) + '\n'
    launcher_config += ''.join(key+'='+value+'\n' for key, value in cfg['General'].items())
    game_config = '[Common]\nAutoUpdate=0\nAutoUpdateMap=0\nRunAtStart=0\n[Warcraft III]\nMainExe=Z:/work/game/war3.exe\nAutoLoginOn=1\n'
    # Exclusive creation: never overwrite a vendor-generated or existing profile.
    created = []
    try:
        for name, text in [('launcher.ini', launcher_config), ('iccup.ini', game_config)]:
            target = output / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created.append(target)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
                handle.write(text)
    except BaseException:
        for target in created:
            target.unlink(missing_ok=True)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--secret', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--defaults', required=True, type=Path)
    p.add_argument('--launcher', required=True, type=Path)
    p.add_argument('--library', default='/opt/war3/lib/libbncsutil.so')
    args = p.parse_args()
    os.umask(0o077)
    try:
        provision(args.secret, args.output, args.defaults, args.launcher, args.library)
    except Exception:
        # A parser exception can contain input data; never print it here.
        print(json.dumps({'event': 'STAR_PROFILE_REJECTED', 'authentication_attempted': False}))
        return 78
    print(json.dumps({'event': 'STAR_PROFILE_CREATED', 'mode': '0600', 'authentication_attempted': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
