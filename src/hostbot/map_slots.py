"""Read classic map slots using our compiled StormLib; never execute map scripts.

Layout follows pinned upstream ghost/map.cpp CMap::Load (w3i v18/v25).
"""
import os
import struct
import subprocess


def read_info(mapfile, library):
    env = dict(os.environ, LD_LIBRARY_PATH=str(library.parent))
    result = subprocess.run([str(library.parent.parent / 'mpq-map-info'), str(mapfile)],
                            env=env, capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError('could not read classic war3map.w3i: ' + result.stderr.decode(errors='replace'))
    return result.stdout


class Cursor:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def skip(self, size):
        if self.pos + size > len(self.data):
            raise ValueError('truncated war3map.w3i')
        self.pos += size

    def uint(self):
        start = self.pos
        self.skip(4)
        return struct.unpack_from('<I', self.data, start)[0]

    def strings(self, count):
        for _ in range(count):
            try:
                self.pos = self.data.index(0, self.pos) + 1
            except ValueError:
                raise ValueError('unterminated war3map.w3i string') from None


def slots_from_info(data):
    cur = Cursor(data)
    version = cur.uint()
    if version not in (18, 25):
        raise ValueError('only classic w3i v18/v25 are supported')
    cur.skip(8); cur.strings(4); cur.skip(48)
    cur.uint(); cur.uint(); flags = cur.uint(); cur.skip(5)
    if version == 25:
        cur.strings(1)
    cur.strings(3); cur.skip(4)
    if version == 25:
        cur.strings(1)
    cur.strings(3)
    if version == 25:
        cur.skip(24); cur.strings(1); cur.skip(5)
    count = cur.uint()
    if not 1 <= count <= 12:
        raise ValueError('invalid player count in map')
    slots = []
    for _ in range(count):
        colour, status, race = cur.uint(), cur.uint(), cur.uint()
        cur.skip(4); cur.strings(1); cur.skip(16)
        if colour >= 12:
            raise ValueError('invalid player colour in map')
        if status in (1, 2):
            slots.append([0, 255, 0 if status == 1 else 2, int(status == 2), 0,
                          colour, {1: 1, 2: 2, 3: 8, 4: 4}.get(race, 32), 1, 100])
    teams = cur.uint()
    if teams > 12:
        raise ValueError('invalid team count in map')
    for team in range(teams):
        cur.uint(); mask = cur.uint(); cur.strings(1)
        for slot in slots:
            if mask & (1 << slot[5]):
                slot[4] = team
    for team, slot in enumerate(slots):
        if flags & 4:
            slot[4], slot[6] = team, 32
        if not flags & 32:
            slot[6] |= 64
    return slots


def capped_slot_config(mapfile, library, maximum):
    slots = slots_from_info(read_info(mapfile, library))
    humans = [slot for slot in slots if slot[3] == 0 and slot[2] == 0]
    if maximum > len(humans):
        raise ValueError(f'maximum {maximum} exceeds {len(humans)} human slots in map')
    for slot in humans[maximum:]:
        slot[2] = 1  # native SLOTSTATUS_CLOSED; keep colours, teams, races intact
    return ''.join(f'map_slot{i} = ' + ' '.join(map(str, slot)) + '\n'
                   for i, slot in enumerate(slots, 1))
