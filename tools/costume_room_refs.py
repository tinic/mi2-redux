#!/usr/bin/env python3
"""Pass C — costume_id -> set(rooms_where_drawn) via descumm.

For each room, run descumm on every script chunk (LSCR/SCRP/EXCD/ENCD
plus OBCD-VERB), find ActorOps(...,[..., Costume(N), ...]) and
loadCostume(N) calls with literal int args, and record (room_id,
costume_id) pairs.

Drives:
  - the per-room costume-lock filter (only lock slots for costumes
    actually drawn here, not every costume hosted in this LFLF)
  - the costume-family clustering (rooms drawing the same costume
    must share its CLUT slots, so they're co-quantized)

Output: tools/costume_refs.json  (also a {costume_id: home_room_id}
table, since DCOS in monkey2.000 maps every costume to its host).
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (load, be32, find_chunk, walk_rooms,

                                name as cn)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
DESCUMM = f'{REPO_ROOT}/tools/scummvm-tools/descumm'

TOPLEVEL_SCRIPTS = ('LSCR', 'SCRP', 'EXCD', 'ENCD')

# Match `Costume(N)` and `loadCostume(N)` with a literal int. Skips
# Var[...] args — we can't statically resolve those.
COSTUME_REF_RE = re.compile(
    r'\b(?:loadCostume|Costume)\(\s*(\d+)\s*\)'
)


def descumm_chunk(chunk_bytes):
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk_bytes)
        path = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', path],
                           capture_output=True, text=True, timeout=10)
    finally:
        os.unlink(path)
    if r.returncode != 0:
        return ''
    return r.stdout


def collect_room_scripts(d, ro, room_size):
    p = ro + 8; end = ro + room_size
    while p < end:
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag in TOPLEVEL_SCRIPTS:
            yield bytes(d[p : p + sz])
        elif tag == 'OBCD':
            verb_off, verb_sz = find_chunk(d, p + 8, p + sz, 'VERB')
            if verb_off is not None:
                yield bytes(d[verb_off : verb_off + verb_sz])
        p += sz


def parse_dcos(idx):
    """Parse DCOS chunk in monkey2.000: {costume_id: home_room_id}."""
    out = {}
    p = 0
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'DCOS':
            count = struct.unpack('<H', idx[p + 8 : p + 10])[0]
            file_nums = list(idx[p + 10 : p + 10 + count])
            for cid, fn in enumerate(file_nums):
                if 0 < fn < 12:
                    out[cid] = fn  # actually disk number; need a second pass
            # In monkey2.000, DCOS records are (room_id u8) per costume,
            # not disk number. Verify by checking values are room ids.
            # If max(file_nums) > 11, it really is room ids.
            return out
        p += sz
    return out


def parse_dcos_full(idx):
    """Return {costume_id: room_id} from DCOS, plus {room_id: disk}
    from DROO."""

    cost_room = {}
    room_disk = {}
    p = 0
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'DROO':
            count = struct.unpack('<H', idx[p + 8 : p + 10])[0]
            file_nums = list(idx[p + 10 : p + 10 + count])
            for rid, fn in enumerate(file_nums):
                if 0 < fn < 12:
                    room_disk[rid] = fn
        elif tag == 'DCOS':
            count = struct.unpack('<H', idx[p + 8 : p + 10])[0]
            room_ids = list(idx[p + 10 : p + 10 + count])
            for cid, rid in enumerate(room_ids):
                if rid > 0:
                    cost_room[cid] = rid
        p += sz
    return cost_room, room_disk


def main():
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    cost_home, room_disk = parse_dcos_full(idx)
    sys.stderr.write(f'DCOS: {len(cost_home)} costumes -> rooms\n')
    sys.stderr.write(f'DROO: {len(room_disk)} rooms -> disks\n')

    disks = {n: load(f'{AMIGA_DIR}/monkey2.{n:03d}')
             for n in range(1, 12)
             if os.path.exists(f'{AMIGA_DIR}/monkey2.{n:03d}')}

    # costume_id -> set(room_id_where_drawn)
    drawn_in = {}
    # room_id -> set(costume_id) (inverse)
    room_uses = {}

    total = skipped = 0
    for disk_n, d in disks.items():
        for rid, ro in walk_rooms(d):
            rsz = be32(d, ro + 4)
            for chunk in collect_room_scripts(d, ro, rsz):
                total += 1
                disasm = descumm_chunk(chunk)
                if not disasm:
                    skipped += 1
                    continue
                for m in COSTUME_REF_RE.finditer(disasm):
                    cid = int(m.group(1))
                    if cid in cost_home:
                        drawn_in.setdefault(cid, set()).add(rid)
                        room_uses.setdefault(rid, set()).add(cid)
        sys.stderr.write(f'[disk {disk_n}] cumulative: '
                         f'{len(drawn_in)} costumes, '
                         f'{sum(len(v) for v in drawn_in.values())} edges\n')

    sys.stderr.write(f'\n{total} scripts processed, {skipped} skipped\n')

    out = {
        'cost_home': cost_home,                     # {cid: home_room_id}
        'room_disk': room_disk,                     # {rid: disk_n}
        'drawn_in': {str(c): sorted(r) for c, r in drawn_in.items()},
        'room_uses': {str(r): sorted(c) for r, c in room_uses.items()},
    }
    out_path = f'{REPO_ROOT}/tools/costume_refs.json'
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'Wrote {out_path} ({len(cost_home)} costumes, {len(drawn_in)} drawn-in entries)',
          file=sys.stderr)


if __name__ == '__main__':
    main()
