#!/usr/bin/env python3
"""Deep costume-reference scan: descumm every script chunk in every room
and capture ALL forms of costume references — not just the literal
Costume(N) / loadCostume(N) the v1 scanner caught.

Captures:
  - ActorOps(...,[..., Costume(N), ...])         literal
  - ActorOps(...,[..., Costume(Var[K]), ...])    var ref (try resolve)
  - loadCostume(N)                                literal preload
  - loadCostume(Var[K])                           var preload
  - lockCostume / unlockCostume / nukeCostume    literal/var
  - getActorCostume(Var = …) reads (informational)
  - PutActor(actor,...) doesn't change costume but sets actor in room
  - SetClass(actor, …) for inferring ownership

Also tries simple Var resolution:
  - If `Var[K] = N;` appears in the same script before a
    Costume(Var[K]) call, record N as the resolved cid.

Output:
  tools/deep_costume_refs.json — {
    'cost_home': {cid: rid},        # from DCOS
    'room_disk': {rid: disk_n},
    'drawn_in':   {cid: [rid,...]}, # rooms where each cid is set on an actor
    'preloaded_in': {cid: [rid,...]}, # rooms that loadCostume() it
    'unresolved': [(rid, chunk_off, expr)],   # var refs we couldn't resolve
  }
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (load, be32, find_chunk, walk_rooms,


                                name as cn)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
DESCUMM = f'{REPO_ROOT}/tools/scummvm-tools/descumm'
OUT_PATH = os.path.join(os.path.dirname(__file__), 'deep_costume_refs.json')

TOPLEVEL = ('LSCR', 'SCRP', 'EXCD', 'ENCD')

# Patterns:
RE_COSTUME_OP_LIT = re.compile(r'\bCostume\(\s*(\d+)\s*\)')
RE_COSTUME_OP_VAR = re.compile(r'\bCostume\(Var\[(\d+)\]\)')
RE_LOAD_LIT = re.compile(r'\bloadCostume\(\s*(\d+)\s*\)')
RE_LOAD_VAR = re.compile(r'\bloadCostume\(Var\[(\d+)\]\)')
RE_LOCK_LIT = re.compile(r'\b(?:lock|unlock|nuke)Costume\(\s*(\d+)\s*\)')
RE_LOCK_VAR = re.compile(r'\b(?:lock|unlock|nuke)Costume\(Var\[(\d+)\]\)')
# Var assignment to literal: Var[K] = 23;
RE_VAR_ASSIGN_LIT = re.compile(r'Var\[(\d+)\]\s*=\s*(\d+)\s*;')


def descumm_chunk(chunk_bytes):
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk_bytes)
        path = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', path],
                           capture_output=True, text=True, timeout=15)
    finally:
        os.unlink(path)
    return r.stdout if r.returncode == 0 else ''


def collect_room_chunks(d, ro, room_size):
    """Yield (tag, chunk_offset, chunk_bytes) for every script chunk
    in the room (including OBCD-VERB)."""
    p = ro + 8
    end = ro + room_size
    while p < end:
        if p + 8 > end:
            break
        tag = cn(d, p)
        sz = be32(d, p + 4)
        if sz < 8:
            break
        if tag in TOPLEVEL:
            yield (tag, p, bytes(d[p : p + sz]))
        elif tag == 'OBCD':
            verb_off, verb_sz = find_chunk(d, p + 8, p + sz, 'VERB')
            if verb_off is not None:
                yield ('VERB', verb_off,
                       bytes(d[verb_off : verb_off + verb_sz]))
        p += sz


def resolve_var_in_script(disasm, var_index, lookup_pos):
    """Try to find the most recent literal assignment to Var[var_index]
    before `lookup_pos` (a character offset into disasm). Returns int
    cid or None."""
    last = None
    for m in RE_VAR_ASSIGN_LIT.finditer(disasm[:lookup_pos]):
        if int(m.group(1)) == var_index:
            last = int(m.group(2))
    return last


def parse_dcos_full(idx_bytes):
    """{cid: rid} from DCOS, {rid: disk} from DROO."""
    cost_home = {}
    room_disk = {}
    p = 0
    while p < len(idx_bytes):
        tag = cn(idx_bytes, p); sz = be32(idx_bytes, p + 4)
        if tag == 'DROO':
            count = struct.unpack('<H', idx_bytes[p + 8:p + 10])[0]
            for rid, fn in enumerate(idx_bytes[p + 10:p + 10 + count]):
                if 0 < fn < 12:
                    room_disk[rid] = fn
        elif tag == 'DCOS':
            count = struct.unpack('<H', idx_bytes[p + 8:p + 10])[0]
            for cid, rid in enumerate(idx_bytes[p + 10:p + 10 + count]):
                if rid > 0:
                    cost_home[cid] = rid
        p += sz
    return cost_home, room_disk


def main():
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    cost_home, room_disk = parse_dcos_full(idx)
    sys.stderr.write(f'DCOS: {len(cost_home)} costumes, '
                     f'DROO: {len(room_disk)} rooms\n')

    disks = {n: load(f'{AMIGA_DIR}/monkey2.{n:03d}')
             for n in range(1, 12)
             if os.path.exists(f'{AMIGA_DIR}/monkey2.{n:03d}')}

    drawn_in = defaultdict(set)        # cid -> set(rid) (actor's costume set on it)
    preloaded_in = defaultdict(set)    # cid -> set(rid) (loadCostume / lock / nuke)
    unresolved = []                    # (rid, tag, chunk_off, expr)
    var_resolved = 0
    n_chunks = 0

    for disk_n, d in disks.items():
        for rid, ro in walk_rooms(d):
            rsz = be32(d, ro + 4)
            for tag, chunk_off, chunk in collect_room_chunks(d, ro, rsz):
                disasm = descumm_chunk(chunk)
                if not disasm:
                    continue
                n_chunks += 1

                # Costume(N) — literal cid set on actor
                for m in RE_COSTUME_OP_LIT.finditer(disasm):
                    drawn_in[int(m.group(1))].add(rid)

                # Costume(Var[K]) — try resolve
                for m in RE_COSTUME_OP_VAR.finditer(disasm):
                    var_idx = int(m.group(1))
                    cid = resolve_var_in_script(disasm, var_idx, m.start())
                    if cid is not None and cid in cost_home:
                        drawn_in[cid].add(rid)
                        var_resolved += 1
                    else:
                        unresolved.append((rid, tag, chunk_off,
                                            f'Costume(Var[{var_idx}])'))

                # loadCostume(N) — preload, may or may not be drawn
                for m in RE_LOAD_LIT.finditer(disasm):
                    preloaded_in[int(m.group(1))].add(rid)
                for m in RE_LOAD_VAR.finditer(disasm):
                    var_idx = int(m.group(1))
                    cid = resolve_var_in_script(disasm, var_idx, m.start())
                    if cid is not None and cid in cost_home:
                        preloaded_in[cid].add(rid)
                        var_resolved += 1
                    else:
                        unresolved.append((rid, tag, chunk_off,
                                            f'loadCostume(Var[{var_idx}])'))

                # lock/unlock/nukeCostume — management; treat as preload-ish
                for m in RE_LOCK_LIT.finditer(disasm):
                    preloaded_in[int(m.group(1))].add(rid)
                for m in RE_LOCK_VAR.finditer(disasm):
                    var_idx = int(m.group(1))
                    cid = resolve_var_in_script(disasm, var_idx, m.start())
                    if cid is not None and cid in cost_home:
                        preloaded_in[cid].add(rid)
                        var_resolved += 1

        sys.stderr.write(f'[disk {disk_n}] cumulative chunks: {n_chunks}, '
                         f'cids drawn-in: {len(drawn_in)}, '
                         f'cids preloaded: {len(preloaded_in)}\n')

    out = {
        'cost_home': cost_home,
        'room_disk': room_disk,
        'drawn_in': {str(c): sorted(rs) for c, rs in drawn_in.items()},
        'preloaded_in': {str(c): sorted(rs) for c, rs in preloaded_in.items()},
        'unresolved': unresolved,
        'stats': {
            'chunks_scanned': n_chunks,
            'var_resolved': var_resolved,
            'unresolved_count': len(unresolved),
        },
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, indent=2)
    sys.stderr.write(f'\nWrote {OUT_PATH}\n')

    # Quick summary on stdout
    print(f'Multi-room costumes (drawn_in only):')
    multi = [(c, len(rs)) for c, rs in drawn_in.items() if len(rs) >= 2]
    multi.sort(key=lambda t: -t[1])
    for c, n in multi:
        print(f'  cid {c:3d}: {n} rooms')
    print()
    print(f'NEW unique cids found vs old scanner — check old vs new in JSON.')


if __name__ == '__main__':
    main()
