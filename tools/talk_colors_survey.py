#!/usr/bin/env python3
"""Extract every TalkColor(N) literal from MI2 Amiga scripts via descumm.

ScummVM renders actor dialogue text using CLUT[N] for each actor's
TalkColor. Those slots show up nowhere in image pixel data so a slot-
usage survey of bg/OBIM/costume rendering misses them. If we repurpose
a CLUT slot that's actually used as a talk colour, lines of dialogue
silently render with the wrong colour at runtime.

This script descumm's every LSCR/SCRP/EXCD/ENCD/OBCD-VERB chunk in
every room and pulls out (room_id, actor_id, talk_color) tuples plus
the global set of slots referenced. Output:
  - prints global "talk-color slots used"
  - writes tools/talk_colors_survey.json
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (


    load, be32, find_chunk, walk_rooms, name as cn,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
DESCUMM = f'{REPO_ROOT}/tools/scummvm-tools/descumm'

TOPLEVEL_SCRIPTS = ('LSCR', 'SCRP', 'EXCD', 'ENCD')

# TalkColor(N) — N may be a literal integer or Var[...]. We only care
# about literals.
TALKCOLOR_RE = re.compile(r'TalkColor\(\s*(\d+)\s*\)')


def descumm_chunk(chunk_bytes):
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk_bytes)
        path = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', path],
                           capture_output=True, text=True, timeout=10)
    finally:
        os.unlink(path)
    return r.stdout if r.returncode == 0 else ''


def collect_room_scripts(d, ro, room_size):
    p = ro + 8; end = ro + room_size
    while p < end:
        if p + 8 > end: break
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag in TOPLEVEL_SCRIPTS:
            yield bytes(d[p : p + sz])
        elif tag == 'OBCD':
            verb_off, verb_sz = find_chunk(d, p + 8, p + sz, 'VERB')
            if verb_off is not None:
                yield bytes(d[verb_off : verb_off + verb_sz])
        if sz < 8: break
        p += sz


def main():
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    p = 0; room_disk = {}
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'DROO':
            cnt = struct.unpack('<H', idx[p + 8 : p + 10])[0]
            file_nums = list(idx[p + 10 : p + 10 + cnt])
            for rid, fn in enumerate(file_nums):
                if 0 < fn < 12:
                    room_disk[rid] = fn
            break
        p += sz

    # Load all disks
    disks = {n: load(f'{AMIGA_DIR}/monkey2.{n:03d}')
             for n in range(1, 12)
             if os.path.exists(f'{AMIGA_DIR}/monkey2.{n:03d}')}

    refs = []           # list of (rid, talk_color)
    global_slots = set()
    for disk_n, d in disks.items():
        for rid, ro in walk_rooms(d):
            rsz = be32(d, ro + 4)
            for chunk in collect_room_scripts(d, ro, rsz):
                disasm = descumm_chunk(chunk)
                if not disasm: continue
                for m in TALKCOLOR_RE.finditer(disasm):
                    n = int(m.group(1))
                    refs.append((rid, n))
                    global_slots.add(n)

    print(f'Found {len(refs)} TalkColor(N) literals across {len(disks)} disks.')
    print()
    print(f'Globally referenced talk colours ({len(global_slots)} slots):')
    print(f'  {sorted(global_slots)}')
    print()

    # Per-room breakdown
    by_room = {}
    for rid, n in refs:
        by_room.setdefault(rid, set()).add(n)
    print('Per-room talk colours (only rooms with >0):')
    for rid in sorted(by_room):
        print(f'  rid {rid:3d}: {sorted(by_room[rid])}')

    # Write JSON
    out = {
        'global_slots': sorted(global_slots),
        'per_room': {str(rid): sorted(s) for rid, s in by_room.items()},
        'total_refs': len(refs),
    }
    out_path = os.path.join(os.path.dirname(__file__), 'talk_colors_survey.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'\nWrote {out_path}')


if __name__ == '__main__':
    main()
