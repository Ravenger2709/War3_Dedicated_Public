"""Bounded, read-only StormLib extraction. No engine, JASS VM or map execution."""
import base64
import ctypes as C
import json
import os
import resource
import stat
import subprocess
import sys

from .files import no_symlinks

LIBRARY = '/opt/war3/lib/libStorm.so'
MAX_MAP_BYTES = 128 * 1024 * 1024
MEMBERS = {'war3map.w3i': 4 * 1024 * 1024,
           'war3map.j': 4 * 1024 * 1024,
           'scripts\\war3map.j': 4 * 1024 * 1024,
           'war3map.wts': 2 * 1024 * 1024}


def checked_map(path):
    path = no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 32 <= info.st_size <= MAX_MAP_BYTES:
        raise ValueError('MAP_SIZE_REJECTED')
    return path


def storm_api(library=LIBRARY):
    """Support public C exports and the pinned image's old C++ StormLib ABI."""
    lib = C.CDLL(str(library))
    signatures = {
        'open': ('SFileOpenArchive', '_Z16SFileOpenArchivePKcjjPPv',
                 [C.c_char_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
        'file': ('SFileOpenFileEx', '_Z15SFileOpenFileExPvPKcjPS_',
                 [C.c_void_p, C.c_char_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_bool),
        'size': ('SFileGetFileSize', '_Z16SFileGetFileSizePvPj',
                 [C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
        'read': ('SFileReadFile', '_Z13SFileReadFilePvS_jPjS_',
                 [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_bool),
        'close_file': ('SFileCloseFile', '_Z14SFileCloseFilePv', [C.c_void_p], C.c_bool),
        'close': ('SFileCloseArchive', '_Z17SFileCloseArchivePv', [C.c_void_p], C.c_bool),
    }
    api = {}
    for key, (plain, mangled, args, result) in signatures.items():
        fn = getattr(lib, plain, None) or getattr(lib, mangled)
        fn.argtypes, fn.restype = args, result
        api[key] = fn
    return api


def _extract(path, library):
    api = storm_api(library)
    archive = C.c_void_p()
    if not api['open'](os.fsencode(path), 0, 0x00080100, C.byref(archive)):
        raise ValueError('MAP_MPQ_REJECTED')
    out = {}
    try:
        for name, limit in MEMBERS.items():
            handle = C.c_void_p()
            if not api['file'](archive, name.encode('ascii'), 0, C.byref(handle)):
                continue
            try:
                high = C.c_uint32()
                size = api['size'](handle, C.byref(high))
                if high.value or not 0 < size <= limit:
                    raise ValueError('MAP_MEMBER_SIZE_REJECTED')
                data = C.create_string_buffer(size)
                read = C.c_uint32()
                if not api['read'](handle, data, size, C.byref(read), None) or read.value != size:
                    raise ValueError('MAP_MEMBER_TRUNCATED')
                out[name] = data.raw
            finally:
                api['close_file'](handle)
    finally:
        api['close'](archive)
    if 'war3map.w3i' not in out or not any(k in out for k in ('war3map.j', 'scripts\\war3map.j')):
        raise ValueError('MAP_MEMBER_MISSING')
    if ('war3map.j' in out and 'scripts\\war3map.j' in out
            and out['war3map.j'] != out['scripts\\war3map.j']):
        raise ValueError('MAP_SCRIPT_AMBIGUOUS')
    return out


def extract_map(path, library=LIBRARY):
    """Isolate native parser crashes/allocations; only fixed members leave child."""
    path = checked_map(path)
    try:
        result = subprocess.run([sys.executable, '-m', 'src.runtime.mpq', str(path), str(library)],
                                capture_output=True, timeout=10, check=False)
        if result.returncode or len(result.stdout) > 20 * 1024 * 1024:
            raise ValueError('MAP_EXTRACTION_REJECTED')
        obj = json.loads(result.stdout)
        if not isinstance(obj, dict) or set(obj) - set(MEMBERS):
            raise ValueError('MAP_EXTRACTION_REJECTED')
        out = {k: base64.b64decode(v, validate=True) for k, v in obj.items()}
        if any(not 0 < len(v) <= MEMBERS[k] for k, v in out.items()):
            raise ValueError('MAP_EXTRACTION_REJECTED')
        return out
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise ValueError('MAP_EXTRACTION_REJECTED') from None


if __name__ == '__main__':
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        data = _extract(checked_map(sys.argv[1]), sys.argv[2])
        print(json.dumps({k: base64.b64encode(v).decode('ascii') for k, v in data.items()}))
    except Exception:
        sys.exit(1)
