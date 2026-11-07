"""Stopped-only, boot-fenced recovery, executed inside the accepted image.

No PID-absence inference, synthetic native events or deletion of old evidence.
The caller checks the runtime container is absent and supplies its kernel boot ID.
"""
from contextlib import closing
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from src.runtime.cycle_state import CycleState
from src.runtime.files import atomic_json, private_bytes, private_json
from src.runtime.journal_audit import audit
from src.runtime.lifecycle import _open_directory, _read_file
from src.runtime.process_ownership import _boot_id
from src.runtime.session_lease import SessionLease, _marker, _CATALOG
from src.runtime.writer_observation import _load_binding

INTENT = '.boot-recovery.json'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'


def require(condition):
    if not condition:
        raise ValueError('POWER_RECOVERY_BLOCKED')


def stage(name):
    """Failure-injection seam; never sleeps or changes production behavior."""


def save(root, name, value):
    atomic_json(root / name, value)
    fd = _open_directory(str(root))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def fence(root, entry):
    return root / ('boot-fence-' + entry['name'] + '.json')


def fingerprint(root, old):
    """Hash retained evidence; never rewrite native JSON/ACK/IPC files."""
    journal = root / old
    fd = _open_directory(str(journal))
    try:
        rows = []
        for name in sorted(os.listdir(fd)):
            data, info = _read_file(fd, name)
            rows.append([name, info.st_dev, info.st_ino, hashlib.sha256(data).hexdigest()])
    finally:
        os.close(fd)
    for name in sorted(os.listdir(root)):
        if name in ('native-launch-' + old + '.json', 'boot-fence-' + old + '.json'):
            rows.append([name, hashlib.sha256(private_bytes(root / name)).hexdigest()])
    # Catalog records are immutable for the retired generation after a boot.
    for name, *_ in list(rows):
        match = re.fullmatch(r'session-([0-9a-f]{32})\.json', name)
        if match:
            queue = 'catalog-' + match[1] + '.json'
            if (root / queue).exists() or (root / queue).is_symlink():
                rows.append([queue, hashlib.sha256(private_bytes(root / queue)).hexdigest()])
    return digest(rows)


def database(root):
    path = root / 'cursor.json.sqlite3'
    # Same metadata requirements as CycleState; never creates a missing DB.
    import stat
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
    try:
        db.execute('PRAGMA synchronous=EXTRA')
        require(db.execute('PRAGMA integrity_check').fetchone() == ('ok',))
        CycleState._has_close_schema(db)
        return db
    except BaseException:
        db.close()
        raise


def history(db):
    result = hashlib.sha256()
    for table, order in [('events', 'generation,match_id'),
                         ('closed_lobbies', 'generation,counter'),
                         ('catalog_closed_lobbies', 'generation,counter'), ('idle_lobbies','generation,counter')]:
        # Preserve fingerprints of pre-idle recovery intents when no idle table
        # exists; an interrupted recovery must remain resumable after upgrade.
        if table == 'idle_lobbies' and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='idle_lobbies'").fetchone():
            continue
        result.update(table.encode())
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            for row in db.execute('SELECT * FROM ' + table + ' ORDER BY ' + order):
                result.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
    result.update(str(db.execute('PRAGMA user_version').fetchone()[0]).encode())
    return result.hexdigest()


def validate_state(value):
    require(type(value) is dict and value.get('version') == 3)
    CycleState.__new__(CycleState)._validate(value)


def resume(root, boot):
    """Resume our own durable intent, including mkdir-before-catalog crashes."""
    if not (root / INTENT).exists() and not (root / INTENT).is_symlink():
        return False
    fd = _open_directory(str(root))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(_marker(root))
        v = private_json(root / INTENT, limit=1024*1024)
        fields = {'version', 'kind', 'root', 'old_boot', 'started_boot', 'catalog_before',
                  'new_name', 'before', 'after', 'history', 'evidence'}
        require(set(v) == fields and type(v['version']) is int and v['version'] == 1
                and v['kind'] == 'host_reboot_interruption'
                and v['root'] == [os.fstat(fd).st_dev, os.fstat(fd).st_ino]
                and type(v['old_boot']) is str and re.fullmatch(UUID, v['old_boot'])
                and type(v['started_boot']) is str and re.fullmatch(UUID, v['started_boot'])
                and v['old_boot'] != boot and v['old_boot'] != v['started_boot']
                and type(v['new_name']) is str and re.fullmatch(r'journal-[0-9a-f]{32}', v['new_name']))
        before, after = v['before'], v['after']
        validate_state(before); validate_state(after)
        require(after == {**before, 'pending': None})
        cat = v['catalog_before']
        require(type(cat) is dict and set(cat) == {'version', 'journals'} and cat['version'] == 1
                and type(cat['journals']) is list and 1 <= len(cat['journals']) < 1024)
        names = set()
        for i, entry in enumerate(cat['journals']):
            require(type(entry) is dict and set(entry) == {'name', 'device', 'inode'}
                    and type(entry['name']) is str
                    and (entry['name'] == 'journal' if i == 0 else re.fullmatch(r'journal-[0-9a-f]{32}', entry['name']))
                    and entry['name'] not in names and type(entry['device']) is int
                    and entry['device'] >= 0 and type(entry['inode']) is int and entry['inode'] > 0)
            check = _open_directory(str(root / entry['name']))
            try:
                require((os.fstat(check).st_dev, os.fstat(check).st_ino) == (entry['device'], entry['inode']))
            finally:
                os.close(check)
            names.add(entry['name'])
        old = cat['journals'][-1]['name']
        require(v['new_name'] not in names and fingerprint(root, old) == v['evidence'])
        disk = {n for n in os.listdir(fd) if n == 'journal' or n.startswith('journal-')}
        require(disk in (names, names | {v['new_name']}))
        if (root / _CATALOG).exists() or (root / _CATALOG).is_symlink():
            current = private_json(root / _CATALOG, limit=131072)
        else:
            require(len(cat['journals']) == 1 and cat['journals'][0]['name'] == 'journal')
            current = cat
        new = root / v['new_name']
        if v['new_name'] not in disk:
            require(current == cat)
            new.mkdir(mode=0o700)
            os.fsync(fd)
        new_fd = _open_directory(str(new))
        try:
            require(not os.listdir(new_fd)
                    and not (root / ('native-launch-' + new.name + '.json')).exists()
                    and not (root / ('ipc-' + new.name)).exists())
            entry = {'name': new.name, 'device': os.fstat(new_fd).st_dev, 'inode': os.fstat(new_fd).st_ino}
        finally:
            os.close(new_fd)
        stage('mkdir')
        target = {'version': 1, 'journals': cat['journals'] + [entry]}
        require(current in (cat, target))
        with closing(database(root)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            actual = json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
            require(actual in (before, after) and history(db) == v['history'])
            db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(after),))
        stage('state')
        save(root, _CATALOG, target)
        stage('catalog')
        receipt = 'boot-recovery-' + old + '.json'
        if (root / receipt).exists() or (root / receipt).is_symlink():
            require(private_json(root / receipt, limit=1024*1024) == v)
        else:
            save(root, receipt, v)
        stage('receipt')
        (root / INTENT).unlink()
        os.fsync(fd)
        return True
    finally:
        os.close(fd)


def queue(root, generation, length):
    path = root / ('catalog-' + generation + '.json')
    if not path.exists() and not path.is_symlink():
        return None
    v = private_json(path)
    require(set(v) == {'version', 'generation', 'digest', 'pending', 'completed'}
            and type(v['version']) is int and v['version'] in (1, 2)
            and v['generation'] == generation and type(v['digest']) is str
            and re.fullmatch('[0-9a-f]{64}', v['digest']))
    for key in ('pending', 'completed'):
        r = v[key]
        if r is None:
            continue
        require(type(r) is dict and (set(r) == {'request_id', 'index', 'position', 'counter', 'status'}
                or key == 'completed' and set(r) == {'request_id', 'index', 'position', 'counter', 'status', 'outcome'}
                and r['outcome'] == 'closed')
                and type(r['request_id']) is str and re.fullmatch('[0-9a-f]{32}', r['request_id'])
                and type(r['index']) is int and 0 <= r['index'] < 32
                and type(r['position']) is int and 0 <= r['position'] < length
                and r['status'] in (None, 'staged', 'rejected', 'uncertain', 'closed')
                and (r['counter'] is None or type(r['counter']) is int and 1 <= r['counter'] < 2**28
                     and r['status'] in ('staged', 'closed')))
        if key == 'completed':
            require(r['counter'] is not None and r['status'] in ('staged', 'closed'))
        else:
            require(r['status'] != 'closed')
    require(not (v['pending'] and v['completed'] and v['pending']['request_id'] == v['completed']['request_id']))
    return v


def recover(root, expected_boot):
    root = Path(root).absolute()
    boot = _boot_id()
    require(type(expected_boot) is str and expected_boot == boot)
    recovered = resume(root, boot)
    lease = SessionLease(root, enabled=True)
    try:
        entry = lease._catalog['journals'][-1]
        # Load the stored cycle, not a possibly edited operator rotation.
        cursor = CycleState.__new__(CycleState)
        cursor.path, cursor.db_path, cursor.lease = lease.cursor, lease.cursor.with_name('cursor.json.sqlite3'), lease
        old = cursor._load_db() if cursor.db_path.exists() else private_json(cursor.path)
        cursor._validate(old)
        cursor = CycleState(lease.cursor, old['cycle'], lease=lease)
        names = sorted(os.listdir(lease.journal_fd))
        claim = root / ('native-launch-' + entry['name'] + '.json')
        claimed = claim.exists() or claim.is_symlink()
        ipc = root / ('ipc-' + entry['name'])
        has_ipc = ipc.exists() or ipc.is_symlink()
        fence_path = fence(root, entry)
        saved = private_json(fence_path) if fence_path.exists() or fence_path.is_symlink() else None
        if saved is not None:
            require(set(saved) == {'version', 'boot_id', 'journal'} and type(saved['version']) is int
                    and saved['version'] == 1 and saved['journal'] == entry
                    and type(saved['boot_id']) is str and re.fullmatch(UUID, saved['boot_id']))
        if not names and not claimed and not has_ipc and cursor.state['pending'] is None:
            save(root, fence_path.name, {'version': 1, 'boot_id': boot, 'journal': entry})
            return {'recovered': recovered}
        require(claimed)
        c = private_json(claim)
        require(set(c) == {'version', 'journal_device', 'journal_inode', 'nonce'} and type(c['version']) is int and c['version'] == 1
                and (c['journal_device'], c['journal_inode']) == (entry['device'], entry['inode'])
                and type(c['nonce']) is str and re.fullmatch('[0-9a-f]{32}', c['nonce']))
        bindings = [re.fullmatch(r'session-([0-9a-f]{32})\.json', n) for n in names]
        generations = [m[1] for m in bindings if m]
        require(len(generations) <= 1)
        if generations:
            generation = generations[0]
            writer = _load_binding(lease, generation)
            old_boot = writer.boot_id
            require(saved is None or saved['boot_id'] == old_boot)
            binding = private_json(lease.journal / ('session-' + generation + '.json'))
            session = binding['session']
            require(has_ipc and session['version'] == 2)
            ipc_fd = _open_directory(str(ipc))
            try:
                require((os.fstat(ipc_fd).st_dev, os.fstat(ipc_fd).st_ino) ==
                        (session['ipc_device'], session['ipc_inode']))
            finally:
                os.close(ipc_fd)
            require(old_boot != boot)
            events = audit(lease, generation)
            q = queue(root, generation, len(cursor.cycle))
            pending = cursor.state['pending']
            if pending:
                require(pending.get('session_generation') == generation)
                if q:
                    r = q['pending']
                    require(r is not None and r['counter'] == pending['host_counter']
                            and r['position'] == cursor.state['index'] and r['status'] == 'staged')
            # Replay exact native records; starts still need their durable reservation.
            for record in events:
                event = asdict(record.event)
                if event['event'] == 'game_started':
                    cursor.consume_external(event)
                else:
                    if event['version'] in (2,3):
                        known = cursor._find_catalog_close(generation, event['host_counter'])
                        identity = (event['request_id'], event['manifest_sha256'], event['catalog_index'])
                        require(known == identity or q is not None and q['digest'] == event['manifest_sha256']
                                and any(r is not None and (r['request_id'], r['index'], r['counter']) ==
                                        (event['request_id'], event['catalog_index'], event['host_counter'])
                                        for r in (q['pending'], q['completed'])))
                    cursor.consume_close(event)
        else:
            # Before the child binding was durable, no reservation/event can exist.
            require(not names and cursor.state['pending'] is None and saved is not None)
            old_boot = saved['boot_id']
            require(old_boot != boot)
        cursor._enable_sqlite()
        before = cursor.state
        require(len(lease._catalog['journals']) < 1024)
        with closing(cursor._connect()) as db:
            retained = history(db)
        info = os.fstat(lease.fd)
        intent = {'version': 1, 'kind': 'host_reboot_interruption',
                  'root': [info.st_dev, info.st_ino], 'old_boot': old_boot, 'started_boot': boot,
                  'catalog_before': lease._catalog, 'new_name': 'journal-' + uuid.uuid4().hex,
                  'before': before, 'after': {**before, 'pending': None},
                  'history': retained, 'evidence': fingerprint(root, entry['name'])}
        save(root, INTENT, intent)
        stage('intent')
    finally:
        lease.close()
    resume(root, boot)
    # Stamp the fresh journal too, including recovery interrupted by another boot.
    return recover(root, expected_boot) | {'recovered': True}


if __name__ == '__main__':
    print(json.dumps(recover(Path('/data/state/cycle'), json.load(sys.stdin)['boot_id'])))
