#!/usr/bin/env python3
"""Pass A v2 — accurate cross-room OBIM reference graph via descumm.

For every script chunk (LSCR/SCRP/EXCD/ENCD/OBCD-VERB) in pristine
mi2-redux data, run scummvm-tools/descumm and extract genuine object
references (only opcodes that take an obj_id structurally, not random
byte matches). Build a {asset_host_slug: [consumer_room_id]} graph.

Output: tools/asset_refs_v2.json
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

ASSET_HOSTS = {
    94: 'icons', 95: 'whoopmap', 99: 'f-turf', 100: 'f-foregro',
    101: 'f-trees', 102: 'f-plants', 103: 'open-cred', 104: 'f-rapinfl',
    105: 'f-rapothe', 106: 'f-rap2inf', 107: 'f-rapshri', 108: 'copycrap',
}

# Script chunks descumm understands directly (it expects the 8-byte chunk header).
TOPLEVEL_SCRIPTS = ('LSCR', 'SCRP', 'EXCD', 'ENCD')

# Opcodes from descumm's printed AST that take an object id as a parameter.
# Capturing groups extract the int that's the obj id.
OBJECT_REF_RE = re.compile(
    r'(?:'
    r'setState\(\s*(\d+)\s*,'
    r'|setOwner\(\s*(\d+)\s*,'
    r'|setObjectName\(\s*(\d+)\s*,'
    r'|drawObject\(\s*(\d+)'
    r'|pickupObject\(\s*(\d+)'
    r'|getObjectState\(\s*(\d+)'
    r'|getObjectOwner\(\s*(\d+)'
    r'|getObjectX\(\s*(\d+)'
    r'|getObjectY\(\s*(\d+)'
    r'|getDist\(\s*(\d+)\s*,'
    r'|isObjectOfClass\(\s*(\d+)'
    r'|setClass\(\s*(\d+)'
    r'|isOwnerOf\(\s*\d+\s*,\s*(\d+)'
    r'|Resource\.loadFlObject\(\s*\d+\s*,\s*(\d+)'
    r'|Resource\.unloadFlObject\(\s*(\d+)'
    r'|stopObjectScript\(\s*(\d+)'
    r'|getActorFromPos\(\s*\d+\s*,\s*\d+\s*,\s*(\d+)'
    r'|getStringWidth\(\s*\d+\s*,\s*(\d+)'
    r'|saveLoadResource\(\s*\d+\s*,\s*\d+\s*,\s*(\d+)'
    r')'
)


def descumm_chunk(chunk_bytes):
    """Run descumm on a chunk and return its output (str)."""
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


def extract_object_refs(disasm):
    """Parse descumm output, return set of obj_id ints referenced."""
    refs = set()
    for line in disasm.splitlines():
        for m in OBJECT_REF_RE.finditer(line):
            for g in m.groups()[1:]:
                if g is not None:
                    try:
                        refs.add(int(g))
                    except ValueError:
                        pass
    return refs


def list_obim_obj_ids(d, ro, room_size):
    out = []
    p = ro + 8; end = ro + room_size
    while p < end:
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag == 'OBIM':
            imhd_off, imhd_sz = find_chunk(d, p + 8, p + sz, 'IMHD')
            if imhd_off is not None and imhd_sz >= 10:
                obj_id = struct.unpack('<H', d[imhd_off + 8 : imhd_off + 10])[0]
                out.append(obj_id)
        p += sz
    return out


def collect_room_scripts(d, ro, room_size):
    """Yield (chunk_tag, chunk_bytes) for all script chunks in this room."""
    p = ro + 8; end = ro + room_size
    while p < end:
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag in TOPLEVEL_SCRIPTS:
            yield tag, bytes(d[p : p + sz])
        elif tag == 'OBCD':
            # OBCD wraps a VERB chunk that contains object verbscripts.
            verb_off, verb_sz = find_chunk(d, p + 8, p + sz, 'VERB')
            if verb_off is not None:
                # descumm's "OBCD" mode (actually it parses VERB internally
                # via the OBCD wrapper; trying VERB directly often works too).
                yield 'VERB', bytes(d[verb_off : verb_off + verb_sz])
        p += sz


def main():
    # Find which disk each room lives in (DROO).
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    p = 0; room_disk = {}
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'DROO':
            count = struct.unpack('<H', idx[p + 8 : p + 10])[0]
            file_nums = list(idx[p + 10 : p + 10 + count])
            for rid, fn in enumerate(file_nums):
                if 0 < fn < 12:
                    room_disk[rid] = fn
            break
        p += sz

    # Load all disks.
    disks = {n: load(f'{AMIGA_DIR}/monkey2.{n:03d}')
             for n in range(1, 12)
             if os.path.exists(f'{AMIGA_DIR}/monkey2.{n:03d}')}

    # Pass 1: enumerate OBIM obj_ids in each asset host.
    asset_obj_ids = {}
    for host_id, host_slug in ASSET_HOSTS.items():
        disk_n = room_disk.get(host_id)
        if disk_n is None or disk_n not in disks:
            asset_obj_ids[host_slug] = []
            continue
        d = disks[disk_n]
        try:
            ro = next(o for r, o in walk_rooms(d) if r == host_id)
        except StopIteration:
            asset_obj_ids[host_slug] = []
            continue
        ids = list_obim_obj_ids(d, ro, be32(d, ro + 4))
        asset_obj_ids[host_slug] = ids

    # Build flat target_obj_id → host_slug map.
    id_to_host = {}
    for host_slug, ids in asset_obj_ids.items():
        for i in ids:
            id_to_host[i] = host_slug
    all_target_ids = set(id_to_host.keys())

    # Pass 2: descumm every consumer room's scripts, look for genuine refs.
    refs = {slug: set() for slug in ASSET_HOSTS.values()}
    skipped_scripts = 0
    total_scripts = 0
    for disk_n, d in disks.items():
        for rid, ro in walk_rooms(d):
            if rid in ASSET_HOSTS:
                continue
            rsz = be32(d, ro + 4)
            for tag, chunk_bytes in collect_room_scripts(d, ro, rsz):
                total_scripts += 1
                disasm = descumm_chunk(chunk_bytes)
                if not disasm:
                    skipped_scripts += 1
                    continue
                used_ids = extract_object_refs(disasm)
                for tid in used_ids & all_target_ids:
                    refs[id_to_host[tid]].add(rid)
        sys.stderr.write(f'[disk {disk_n}] cumulative refs: '
                         + ' '.join(f'{s}={len(r)}' for s, r in refs.items())
                         + '\n')

    sys.stderr.write(f'\n{total_scripts} scripts processed, '
                     f'{skipped_scripts} skipped (descumm failed)\n')

    out = {}
    for host_id, host_slug in ASSET_HOSTS.items():
        out[host_slug] = {
            'host_room_id': host_id,
            'host_disk': room_disk.get(host_id),
            'obim_obj_ids': sorted(asset_obj_ids.get(host_slug, [])),
            'consumer_room_ids': sorted(refs[host_slug]),
        }
    print(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
