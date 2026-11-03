"""Discover maps with the accepted image's isolated static analyzer."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
import uuid

MAP_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_. ()+-]{0,119}\.(?:w3x|w3m)\Z', re.I)
HASH = re.compile(r'[0-9a-f]{64}\Z')
AI_FEATURES = {'ai_start', 'computer_control'}


def automatic_approval(analysis, records):
    """Prepare one hash-bound static record, never alter existing approvals."""
    evidence = analysis.get('ai_evidence')
    if (not isinstance(evidence, dict) or evidence.get('level') not in ('none', 'detected', 'candidate')
            or not isinstance(evidence.get('features'), list)
            or any(not isinstance(f, str) or f not in AI_FEATURES for f in evidence['features'])
            or len(set(evidence['features'])) != len(evidence['features'])
            or (bool(evidence['features']) != (evidence['level'] == 'candidate'))):
        raise ValueError('invalid AI analysis')
    # AI starter calls alone can reference missing resources or dead code.
    # Require controller checks plus trigger/order syntax detected by runtime.
    if 'computer_control' not in evidence['features']:
        return None
    if any(r['map_sha256'] == analysis['map_sha256'] and r['script_sha256'] == analysis['script_sha256'] for r in records):
        return None
    if len(records) >= 32:
        print('Map discovery: AI evidence limit reached; no AI evidence was added for this map.')
        return None
    ids = {r['id'] for r in records}
    digest_id = hashlib.sha256((analysis['map_sha256'] + analysis['script_sha256']).encode()).hexdigest()
    for index in range(64):
        key = 'auto-' + hashlib.sha256((digest_id + str(index)).encode()).hexdigest()[:27]
        if key not in ids:
            return {'id': key, 'level': 'approved_preset', 'map_sha256': analysis['map_sha256'],
                    'script_sha256': analysis['script_sha256'], 'features': evidence['features'][:],
                    'review': 'automatic static evidence; no runtime validation', 'runtime_tested': False}
    raise ValueError('AI approval ID collision')


def operator_presets(document):
    """Return readable operator settings; keep unusual legacy approvals intact.

    Automatic/controller-backed approvals are recomputed over actual map bytes.
    Explicit starter-only legacy approvals still need their original records.
    """
    value = json.loads(json.dumps(document))
    records = value.get('capabilities', [])
    if any('computer_control' not in record.get('features', []) for record in records):
        return value
    value.pop('capabilities', None)
    order = ('id', 'enabled', 'map_filename', 'lobby_name', 'max_players', 'start', 'vote', 'ai')
    nested = {'start': ('autostart_enabled', 'autostart_players', 'countdown_seconds'),
              'vote': ('enabled', 'min_players', 'required_percent', 'timeout', 'cooldown'),
              'ai': ('enabled', 'target', 'skill')}
    entries = []
    for entry in value['presets']:
        entry.get('ai', {}).pop('capability', None)
        for group, keys in nested.items():
            if group in entry:
                entry[group] = {k: entry[group][k] for k in (*keys, *entry[group]) if k in entry[group]}
        entries.append({k: entry[k] for k in (*order, *entry) if k in entry})
    return {'version': value['version'], 'presets': entries}


def digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 32 <= before.st_size <= 128 * 1024 * 1024:
            raise ValueError('invalid map')
        h = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            h.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('map changed')
        return h.hexdigest(), (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    finally:
        os.close(fd)


def analyze(path, host_path, image, docker, env, owner):
    # Commas in bind source paths would be interpreted as Docker mount options.
    path = path.absolute()
    if ',' in str(path) or any(ord(c) < 32 or ord(c) == 127 for c in str(path)) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe map path')
    host_path = Path(host_path)
    if (not host_path.is_absolute() or '..' in host_path.parts or ',' in str(host_path)
            or any(ord(c) < 32 or ord(c) == 127 for c in str(host_path))):
        raise ValueError('unsafe host map path')
    name = 'war3-map-analysis-' + uuid.uuid4().hex
    args = [*docker, 'run', '--rm', '--pull=never', '--name', name, '--restart=no', '--network', 'none',
            '--read-only', '--user', owner, '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--pids-limit', '32', '--memory', '256m',
            '--memory-swap', '256m', '--cpus', '1', '--ulimit', 'core=0', '--log-driver', 'none',
            '--no-healthcheck', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777',
            '--mount', f'type=bind,src={host_path},dst=/selected.w3x,readonly', '--workdir', '/',
            '--entrypoint', 'python3', image, '-m', 'src.runtime.presets', '/selected.w3x']
    output = bytearray()
    try:
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
            try:
                deadline = time.monotonic() + 30
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError('map analysis timeout')
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, min(8192, 65537 - len(output)))
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                output.extend(chunk)
                                if len(output) > 65536:
                                    raise ValueError('map analysis output too large')
                if process.wait(timeout=max(.001, deadline - time.monotonic())):
                    raise ValueError('map analysis failed')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        subprocess.run([*docker, 'rm', '--force', name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=env, timeout=5, check=False)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate analyzer key')
            result[key] = value
        return result
    return json.loads(output, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def make_entry(filename, analysis, expected_hash, existing_ids):
    if not isinstance(analysis, dict) or analysis.get('map_sha256') != expected_hash or not isinstance(analysis.get('script_sha256'), str) or not HASH.fullmatch(analysis['script_sha256']):
        raise ValueError('map analysis mismatch')
    title, slots, humans = (analysis.get(k) for k in ('lobby_name', 'slots', 'human_capacity'))
    if (not isinstance(title, str) or title != title.strip() or not title or len(title.encode()) > 24
            or any(not c.isprintable() or c in '#=[]|\\' for c in title)
            or not isinstance(slots, list) or not 2 <= len(slots) <= 12
            or any(not isinstance(s, list) or len(s) != 9 or any(type(v) is not int or not 0 <= v <= 255 for v in s)
                   or (s[2], s[3]) not in ((0, 0), (2, 1)) or s[4] >= 12 or s[5] >= 12 for s in slots)
            or len({s[5] for s in slots}) != len(slots) or type(humans) is not int
            or humans != sum(s[2] == 0 and s[3] == 0 for s in slots) or humans < 2):
        raise ValueError('map metadata rejected')
    stem = re.sub(r'[^a-z0-9]+', '-', Path(filename).stem.lower()).strip('-') or 'map'
    # Filename hash makes IDs stable while resolving collisions without changing existing IDs.
    suffix = hashlib.sha256(filename.encode()).hexdigest()
    for length in range(8, 25, 4):
        key = stem[:31-length].rstrip('-_') + '-' + suffix[:length]
        if key not in existing_ids:
            break
    else:
        raise ValueError('preset ID collision')
    return {'id': key, 'enabled': True, 'map_filename': filename, 'lobby_name': title,
            'max_players': len(slots), 'start': {'autostart_enabled': False, 'autostart_players': min(10, humans), 'countdown_seconds': 5},
            'vote': {'enabled': True, 'required_percent': 60, 'min_players': 2, 'timeout': 60, 'cooldown': 30},
            'ai': {'enabled': False, 'target': min(6, len(slots)), 'skill': 1}}


def verify_revisions(revisions):
    for path, expected in revisions.items():
        try:
            current = directory_snapshot(path) if expected[0] == 'directory' else digest(path)
            if current != (expected[1] if expected[0] == 'directory' else expected):
                raise ValueError('map changed')
        except (OSError, ValueError):
            raise ValueError('Map discovery: a map changed or became unavailable; retry start.') from None


def directory_snapshot(path):
    """Read a real, unchanged maps directory through its opened inode."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        names = tuple(sorted(os.listdir(fd)))
        after = os.fstat(fd)
        visible = os.stat(path, follow_symlinks=False)
        def identity(info):
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
        if not stat.S_ISDIR(visible.st_mode) or identity(before) != identity(after) or identity(after) != identity(visible):
            raise ValueError('maps directory changed')
        return identity(after), names
    finally:
        os.close(fd)


def discover(instance, document, spec, docker, env, owner, analyzer=analyze, revisions=None):
    """Add safe presets and evidence without mutating operator entries."""
    maps = instance / 'data/maps'
    host_instance = Path(spec.get('host_instance_dir', str(instance)))
    snapshot = directory_snapshot(maps)
    if revisions is not None:
        revisions[maps] = ('directory', snapshot)
    names = snapshot[1]
    # Preserve operator entries even when their map is temporarily absent.
    result = {**document, 'presets': list(document['presets'])}
    records = list(document.get('capabilities', []))
    revisions = {} if revisions is None else revisions
    ids = {x['id'] for x in result['presets']}
    existing = {x.get('map_filename', '').casefold() for x in result['presets'] if isinstance(x.get('map_filename', ''), str)}
    paths = [maps / name for name in sorted(names, key=lambda name: (name.casefold(), name))]
    known_hashes = set()
    skipped = 0
    new_examined = 0
    exact_existing = {x.get('map_filename') for x in result['presets'] if isinstance(x.get('map_filename'), str)}
    for path in paths:
        if path.name in exact_existing and MAP_NAME.fullmatch(path.name) and '..' not in path.name:
            try:
                sha, revision = digest(path)
                known_hashes.add(sha)
                # Existing presets retain their settings. Runtime analyzes only
                # selected maps; do not launch one analyzer per old map merely
                # to recreate metadata that operators no longer store.
                revisions[path] = (sha, revision)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError, UnicodeError):
                skipped += 1
    for path in paths:
        if len(result['presets']) >= 32:
            break
        # Bootstrap owns selected.w3x as an internal copy of the initial map.
        if path.name.casefold() == 'selected.w3x' or not MAP_NAME.fullmatch(path.name) or '..' in path.name or path.name.casefold() in existing:
            continue
        try:
            sha, revision = digest(path)
            if sha in known_hashes:
                continue
            if new_examined >= 32:
                print('Map discovery: new map scan limit reached; remove invalid maps to continue.')
                break
            new_examined += 1
            data = analyzer(path, host_instance / 'data/maps' / path.name, spec['image_id'], docker, env, owner)
            if digest(path) != (sha, revision):
                raise ValueError('map changed during analysis')
            entry = make_entry(path.name, data, sha, ids)
            record = automatic_approval(data, records)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError, UnicodeError):
            skipped += 1
            continue
        candidate = {**result, 'presets': [*result['presets'], entry]}
        if record:
            candidate['capabilities'] = [*records, record]
        if len((json.dumps(candidate, indent=2, sort_keys=True) + '\n').encode()) > 65536:
            print('Map discovery: preset file size limit reached.')
            break
        result['presets'].append(entry)
        if record:
            records.append(record)
            result['capabilities'] = records[:]
        revisions[path] = (sha, revision)
        ids.add(entry['id'])
        existing.add(path.name.casefold())
        known_hashes.add(sha)
    if directory_snapshot(maps) != snapshot:
        raise ValueError('Map discovery: maps directory changed; retry start.')
    return result, skipped
