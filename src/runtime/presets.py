"""Strict operator presets and conservative classic MPQ capability evidence.

Detection is static evidence, never a claim that arbitrary JASS will run correctly.
Only an explicit, hash-bound curated approval can compile AI native settings.
"""
import hashlib
import json
from pathlib import Path
import re
import stat
import struct

from .config import bounded_int, gameplay_config
from .files import no_symlinks
from .mpq import checked_map, extract_map

ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
HASH = re.compile(r'[0-9a-f]{64}')
FEATURES = {'ai_start', 'computer_control'}
AI_OFF = {'bot_lobby_ai_target': '0', 'bot_lobby_ai_supported': '0',
          'bot_lobby_ai_skill': '1', 'bot_lobby_min_humans': '2'}


class AIEvidenceError(ValueError):
    """No unique hash-bound AI evidence is valid for the selected map."""


def preset_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('PRESET_ID_REJECTED')
    return value


def lobby_name(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value.encode('utf-8')) > 24
            or any(not c.isprintable() or c in '#=[]|\\' for c in value)):
        raise ValueError('PRESET_NAME_REJECTED')
    return value


def map_basename(value):
    if (not isinstance(value, str) or '..' in value
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. ()+-]{0,119}\.(?:w3x|w3m)', value, re.I)):
        raise ValueError('PRESET_MAP_NAME_REJECTED')
    return value


def _object(obj, allowed, required=()):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise ValueError('PRESET_FIELDS_REJECTED')
    return obj


def _boolean(obj, key):
    if type(obj.get(key)) is not bool:
        raise ValueError('PRESET_BOOLEAN_REJECTED')


def _number(obj, key, lo, hi):
    if not bounded_int(obj.get(key), lo, hi):
        raise ValueError('PRESET_THRESHOLD_REJECTED')


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('PRESET_DUPLICATE_KEY')
        obj[key] = value
    return obj


def load_presets(path):
    path = no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536:
        raise ValueError('PRESET_FILE_REJECTED')
    with path.open('rb') as stream:
        data = stream.read(65537)
    if len(data) != info.st_size:
        raise ValueError('PRESET_FILE_CHANGED')
    return parse_presets(data)


def parse_presets(data):
    """Parse one bounded immutable byte snapshot using the normal preset schema."""
    if not isinstance(data, bytes) or not 0 < len(data) <= 65536:
        raise ValueError('PRESET_FILE_REJECTED')
    try:
        raw = json.loads(data, object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('PRESET_NUMBER_REJECTED')))
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError('PRESET_FILE_REJECTED') from None
    _object(raw, ('version', 'presets', 'capabilities'), ('version', 'presets'))
    if type(raw['version']) is not int or raw['version'] != 1:
        raise ValueError('PRESET_VERSION_REJECTED')
    entries = raw['presets']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError('PRESET_COUNT_REJECTED')
    presets = {}
    for entry in entries:
        _object(entry, ('id', 'enabled', 'map_filename', 'lobby_name', 'max_players', 'start', 'vote', 'ai'), ('id', 'enabled'))
        key = preset_id(entry['id'])
        if key in presets:
            raise ValueError('PRESET_DUPLICATE_ID')
        _boolean(entry, 'enabled')
        if key == 'manual':
            _object(entry, ('id', 'enabled'), ('id', 'enabled'))
            if not entry['enabled']:
                raise ValueError('PRESET_MANUAL_REQUIRED')
        else:
            if 'map_filename' not in entry:
                raise ValueError('PRESET_FIELDS_REJECTED')
            map_basename(entry['map_filename'])
            if 'lobby_name' in entry:
                lobby_name(entry['lobby_name'])
            if 'max_players' in entry:
                _number(entry, 'max_players', 2, 12)
            start = _object(entry.get('start', {}), ('autostart_enabled', 'autostart_players', 'countdown_seconds'))
            if 'autostart_enabled' in start:
                _boolean(start, 'autostart_enabled')
            for k, lo, hi in (('autostart_players', 2, 12), ('countdown_seconds', 0, 60)):
                if k in start:
                    _number(start, k, lo, hi)
            vote = _object(entry.get('vote', {}), ('enabled', 'required_percent', 'min_players', 'timeout', 'cooldown'))
            if 'enabled' in vote:
                _boolean(vote, 'enabled')
            for k, lo, hi in (('required_percent', 1, 100), ('min_players', 1, 12), ('timeout', 5, 600), ('cooldown', 0, 600)):
                if k in vote:
                    _number(vote, k, lo, hi)
            ai = _object(entry.get('ai', {}), ('enabled', 'target', 'skill', 'capability'))
            if 'enabled' in ai:
                _boolean(ai, 'enabled')
            if 'target' in ai:
                _number(ai, 'target', 2, 12)
            if 'skill' in ai:
                _number(ai, 'skill', 0, 2)
            if 'capability' in ai:
                preset_id(ai['capability'])
        presets[key] = entry
    capabilities = {}
    records = raw.get('capabilities', [])
    if not isinstance(records, list) or len(records) > 32:
        raise ValueError('PRESET_CAPABILITY_REJECTED')
    for record in records:
        fields = ('id', 'level', 'map_sha256', 'script_sha256', 'features', 'review', 'runtime_tested')
        _object(record, fields, fields)
        key = preset_id(record['id'])
        if key in capabilities:
            raise ValueError('PRESET_DUPLICATE_CAPABILITY')
        if record['level'] != 'approved_preset':
            raise ValueError('PRESET_CAPABILITY_REJECTED')
        for k in ('map_sha256', 'script_sha256'):
            if not isinstance(record[k], str) or not HASH.fullmatch(record[k]):
                raise ValueError('PRESET_HASH_REJECTED')
        features = record['features']
        if (not isinstance(features, list) or not features or len(features) > len(FEATURES)
                or any(not isinstance(x, str) or x not in FEATURES for x in features)
                or len(set(features)) != len(features)):
            raise ValueError('PRESET_CAPABILITY_REJECTED')
        if (not isinstance(record['review'], str) or not record['review'].strip()
                or not 1 <= len(record['review']) <= 512
                or any(not c.isprintable() for c in record['review'])):
            raise ValueError('PRESET_CAPABILITY_REJECTED')
        _boolean(record, 'runtime_tested')
        capabilities[key] = record
    return presets, capabilities


class Cursor:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def skip(self, count):
        start = self.pos
        self.pos += count
        if self.pos > len(self.data):
            raise ValueError('MAP_INFO_TRUNCATED')
        return start

    def uint(self):
        return struct.unpack_from('<I', self.data, self.skip(4))[0]

    def string(self):
        end = self.data.find(b'\0', self.pos, self.pos + 8193)
        if end < 0:
            raise ValueError('MAP_INFO_STRING_REJECTED')
        value, self.pos = self.data[self.pos:end], end + 1
        return value

    def strings(self, count):
        return [self.string() for _ in range(count)]


def map_info(data):
    cur = Cursor(data)
    version = cur.uint()
    if version not in (18, 25):
        raise ValueError('MAP_FORMAT_UNSUPPORTED')
    cur.skip(8)
    title = cur.strings(4)[0]
    cur.skip(48)
    width, height, flags = cur.uint(), cur.uint(), cur.uint()
    if not 1 <= width <= 512 or not 1 <= height <= 512:
        raise ValueError('MAP_DIMENSIONS_REJECTED')
    cur.skip(5)
    if version == 25:
        cur.string()
    cur.strings(3)
    cur.skip(4)
    if version == 25:
        cur.string()
    cur.strings(3)
    if version == 25:
        cur.skip(24)
        cur.string()
        cur.skip(5)
    count = cur.uint()
    if not 1 <= count <= 12:
        raise ValueError('MAP_PLAYER_COUNT_REJECTED')
    slots, colours = [], set()
    for _ in range(count):
        colour, status, race = cur.uint(), cur.uint(), cur.uint()
        cur.skip(4)
        cur.string()
        cur.skip(16)
        if colour >= 12 or colour in colours or status not in (0, 1, 2, 3, 4) or race > 4:
            raise ValueError('MAP_PLAYER_REJECTED')
        colours.add(colour)
        if status in (1, 2):
            slots.append([0, 255, 0 if status == 1 else 2, int(status == 2), 0,
                          colour, {1: 1, 2: 2, 3: 8, 4: 4}.get(race, 32), 1, 100])
    teams = cur.uint()
    if not slots or not 0 <= teams <= 12:
        raise ValueError('MAP_TEAMS_REJECTED')
    assigned = 0
    for team in range(teams):
        cur.uint()
        mask = cur.uint()
        cur.string()
        # Classic editors/optimizers may set unused high bits. CMap uses colours 0..11.
        mask &= 0xfff
        if mask & assigned:
            raise ValueError('MAP_TEAMS_REJECTED')
        assigned |= mask
        for slot in slots:
            if mask & (1 << slot[5]):
                slot[4] = team
    for team, slot in enumerate(slots):
        if flags & 4:
            slot[4], slot[6] = team, 32
        if not flags & 32:
            slot[6] |= 64
    return {'format': version, 'title': title, 'flags': flags, 'slots': slots,
            'human_capacity': sum(s[2] == 0 and s[3] == 0 for s in slots)}


# Strip literals and comments together so comment markers in strings stay inert.
LEXEMES = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def script_evidence(script):
    text = script.decode('utf-8-sig', errors='strict')
    if '\x00' in text:
        raise ValueError('MAP_SCRIPT_REJECTED')
    code = LEXEMES.sub(' ', text)
    if any(token in code for token in ('/*', '*/', '"', "'")):
        raise ValueError('MAP_SCRIPT_LEXING_REJECTED')
    features = []
    if re.search(r'\bcall\s+Start(?:Melee|Campaign)AI\s*\(', code):
        features.append('ai_start')
    computer = bool(re.search(r'\bGetPlayerController\s*\(', code)
                    and re.search(r'\bMAP_CONTROL_COMPUTER\b', code))
    if (computer and re.search(r'\bcall\s+Issue(?:Immediate|Point|Target)Order\w*\s*\(', code)
            and re.search(r'\bcall\s+TriggerAddAction\s*\(', code)):
        features.append('computer_control')
    return {'level': 'candidate' if features else 'detected' if computer else 'none',
            'features': features}


def short_title(title, wts=b''):
    value = title.decode('utf-8-sig', errors='strict')
    if re.fullmatch(r'TRIGSTR_[0-9]+', value):
        key = int(value[8:])
        text = wts.decode('utf-8-sig', errors='strict')
        matches = [v for n, v in re.findall(r'(?m)^\s*STRING\s+([0-9]+)\s*\{([^}]*)\}', text)
                   if int(n) == key]
        if len(matches) != 1:
            raise ValueError('MAP_TITLE_UNRESOLVED')
        value = matches[0].strip()
    value = re.sub(r'\|c[0-9A-Fa-f]{8}|\|r', '', value)
    value = ' '.join(value.split())
    # Byte slicing is bounded even for a deliberately huge WTS title.
    value = value.encode('utf-8')[:24].decode('utf-8', errors='ignore')
    return lobby_name(value.rstrip())


def analyze_map(path):
    path = checked_map(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        total = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(chunk)
            if total > 128 * 1024 * 1024:
                raise ValueError('MAP_SIZE_REJECTED')
            digest.update(chunk)
    members = extract_map(path)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('MAP_CHANGED_DURING_ANALYSIS')
    info = map_info(members['war3map.w3i'])
    script = members.get('war3map.j', members.get('scripts\\war3map.j'))
    try:
        evidence = script_evidence(script)
        title = short_title(info.pop('title'), members.get('war3map.wts', b''))
    except UnicodeError:
        raise ValueError('MAP_TEXT_REJECTED') from None
    return {**info, 'lobby_name': title, 'map_sha256': digest.hexdigest(),
            'script_sha256': hashlib.sha256(script).hexdigest(), 'ai_evidence': evidence}


def matching_ai_approval(ai, capabilities, analysis):
    def same_hashes(record):
        return (record['map_sha256'] == analysis['map_sha256']
                and record['script_sha256'] == analysis['script_sha256'])
    if 'capability' in ai:
        record = capabilities.get(ai['capability'])
        candidates = [record] if record and same_hashes(record) else []
    else:
        candidates = [record for record in capabilities.values() if same_hashes(record)]
    if (len(candidates) == 1
            and set(candidates[0]['features']).issubset(analysis['ai_evidence']['features'])):
        return candidates[0]
    raise AIEvidenceError('PRESET_AI_EVIDENCE_REJECTED')


def resolve_preset(source, gameplay):
    """Called before writing any private native config. Manual needs no JSON."""
    game = gameplay_config(gameplay)
    key = game['map_preset']
    if key == 'manual':
        return game, dict(AI_OFF), None
    source = no_symlinks(source)
    presets, capabilities = load_presets(source.parent / 'presets.json')
    validated, native, mapcfg, _, _ = _resolve_named_snapshot(source, game, presets, capabilities)
    return validated, native, mapcfg


def _resolve_named_snapshot(source, game, presets, capabilities, *, sealed_analysis=None):
    """Resolve one named preset against a single parsed preset document.

    `sealed_analysis` is reserved for preload preparation after it validates the
    sealed map bytes; ordinary callers leave it unset and read the source path.
    """
    key = game['map_preset']
    if key == 'manual':
        raise ValueError('PRELOAD_MANUAL_UNSUPPORTED')
    entry = presets.get(key)
    if not entry or not entry['enabled']:
        raise ValueError('PRESET_UNAVAILABLE')
    path = no_symlinks(source.parent.parent / 'maps' / entry['map_filename'])
    analysis = analyze_map(path) if sealed_analysis is None else sealed_analysis
    ai = entry.get('ai', {})
    enabled = ai.get('enabled', False)
    native = dict(AI_OFF)
    capacity = len(analysis['slots'])  # Native population includes predefined computers.
    computers = sum(s[3] == 1 for s in analysis['slots'])
    maximum = entry.get('max_players', capacity)
    if not 2 <= maximum <= capacity:
        raise ValueError('PRESET_MAP_CAPACITY_REJECTED')
    human_limit = min(analysis['human_capacity'], maximum if enabled else maximum - computers)
    if enabled:
        target = ai.get('target', 6)
        matching_ai_approval(ai, capabilities, analysis)
        if target > maximum:
            raise ValueError('PRESET_AI_TARGET_REJECTED')
        native.update(bot_lobby_ai_target=str(target), bot_lobby_ai_supported='1',
                      bot_lobby_ai_skill=str(ai.get('skill', 1)), bot_lobby_min_humans='1')
    start = entry.get('start', {})
    vote = entry.get('vote', {})
    game.update(game_name=entry.get('lobby_name', analysis['lobby_name']), max_players=maximum,
                autostart_enabled=start.get('autostart_enabled', True),
                autostart_players=start.get('autostart_players', max(2, min(human_limit, 6 if enabled else game['autostart_players']))),
                countdown_seconds=start.get('countdown_seconds', game['countdown_seconds']))
    for k, v in vote.items():
        game['vote_' + k] = v
    minimum = game['vote_min_players']
    if minimum == 1 and not enabled:
        raise ValueError('PRESET_SINGLE_HUMAN_REQUIRES_AI')
    # Raw gameplay never gains a minimum-one bypass: validate with >=2 first.
    validated = gameplay_config({**game, 'vote_min_players': max(2, minimum)})
    validated['vote_min_players'] = minimum
    if minimum > human_limit or game['autostart_enabled'] and game['autostart_players'] > human_limit:
        raise ValueError('PRESET_HUMAN_CAPACITY_REJECTED')
    slots = [s[:] for s in analysis['slots']]
    humans = [s for s in slots if s[2] == 0 and s[3] == 0]
    for slot in humans[human_limit:]:
        slot[2] = 1  # Close only human slots; preserve native computers/teams/races.
    mapcfg = ('map_path = Maps\\Download\\' + entry['map_filename'] + '\n'
              'map_localpath = ' + entry['map_filename'] + '\n'
              'map_speed = 3\nmap_visibility = 4\nmap_observers = 1\n'
              'map_flags = 3\nmap_loadingame = 0\n'
              + ''.join('map_slot' + str(i) + ' = ' + ' '.join(map(str, slot)) + '\n'
                        for i, slot in enumerate(slots, 1)))
    return validated, native, mapcfg, path, analysis


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Read-only classic MPQ analysis; never executes map code.')
    parser.add_argument('map', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(analyze_map(args.map), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
