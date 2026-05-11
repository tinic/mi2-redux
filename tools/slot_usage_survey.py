#!/usr/bin/env python3
"""Per-room CLUT slot usage survey.

For each room, walk the pristine Amiga data and report which CLUT slots
are actually referenced by:
  - bg SMAP pixel data (paletteMod=16; values 0..31 → CLUT[16..47])
  - per-OBIM SMAP pixel data (same paletteMod)
  - costume pal_tables for costumes hosted in the LFLF
  - costume pal_tables for costumes drawn in this room from elsewhere

Output: a table row per room with the slot bitmasks, and a global summary
of which slots in [48..255] are/aren't actually drawn anywhere.
"""

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (
    load, be32, le32, le16, walk_rooms, find_chunk, name as cn,
)
from decode_obim import decode_smap
from pristine_cache import cache
from decode_all import parse_index_room_names

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



AMIGA_DIR = f'{REPO_ROOT}/amiga-data'


def bg_slots_used(d, ro, room_size):
    """Return set of CLUT slots used by the bg SMAP."""
    rmhd_off, _ = find_chunk(d, ro + 8, ro + room_size, 'RMHD')
    if rmhd_off is None:
        return set()
    w = le16(d, rmhd_off + 8)
    h = le16(d, rmhd_off + 10)
    rmim_off, rmim_sz = find_chunk(d, ro + 8, ro + room_size, 'RMIM')
    if rmim_off is None:
        return set()
    im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
    if im00_off is None:
        return set()
    smap_off, _ = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
    if smap_off is None:
        return set()
    indexed, _ = decode_smap(d, smap_off, w, h)
    return {idx + 16 for idx in set(indexed)}


def obim_slots_used(d, ro, room_size):
    """Walk all OBIM children and decode their SMAPs to find slot usage."""
    used = set()
    p = ro + 8
    end = ro + room_size
    while p < end:
        if p + 8 > end:
            break
        tag = cn(d, p)
        sz = be32(d, p + 4)
        if tag == 'OBIM':
            ip = p + 8
            ipend = p + sz
            w_obj = h_obj = 0
            while ip < ipend:
                inm = cn(d, ip)
                isz = be32(d, ip + 4)
                if inm == 'IMHD' and isz >= 26:
                    w_obj = le16(d, ip + 8 + 14)
                    h_obj = le16(d, ip + 8 + 16)
                elif inm.startswith('IM') and len(inm) == 4 and w_obj > 0:
                    smap_o, _ = find_chunk(d, ip + 8, ip + isz, 'SMAP')
                    if smap_o is not None:
                        try:
                            indexed, _ = decode_smap(d, smap_o, w_obj, h_obj)
                            used |= {idx + 16 for idx in set(indexed)}
                        except Exception:
                            pass
                if isz < 8:
                    break
                ip += isz
        if sz < 8 or p + sz > end:
            break
        p += sz
    return used


def main():
    cost_refs = json.load(open(os.path.join(os.path.dirname(__file__),
                                              'costume_refs.json')))
    drawn_in = cost_refs.get('drawn_in', {})
    cost_home = cost_refs.get('cost_home', {})
    names = parse_index_room_names(f'{AMIGA_DIR}/monkey2.000')

    # Map costume_id -> pal_table (from cache, all rooms' costumes).
    cid_to_pal = {}
    for rid, room in cache.data['rooms'].items():
        for c in room['costumes']:
            cid_to_pal[c['cost_id']] = list(c['pal_table'])

    # Map disk -> bytearray (load lazily)
    disk_cache = {}
    def get_disk(n):
        if n not in disk_cache:
            disk_cache[n] = load(f'{AMIGA_DIR}/monkey2.{n:03d}')
        return disk_cache[n]

    rows = []
    global_used = set()
    for rid in sorted(cache.data['rooms']):
        room = cache.data['rooms'][rid]
        disk_n = room['disk']
        if not os.path.exists(f'{AMIGA_DIR}/monkey2.{disk_n:03d}'):
            continue
        d = get_disk(disk_n)
        try:
            ro = next(o for r, o in walk_rooms(d) if r == rid)
        except StopIteration:
            continue
        rsz = be32(d, ro + 4)

        bg = bg_slots_used(d, ro, rsz)
        obim = obim_slots_used(d, ro, rsz)
        # Costumes hosted in this LFLF
        cost_local = set()
        for c in room['costumes']:
            for v in c['pal_table']:
                if v != 0 and v != 250:
                    cost_local.add(v)
        # Costumes drawn here from elsewhere (descumm graph)
        cost_foreign = set()
        for cid_str, rooms_drawn in drawn_in.items():
            cid = int(cid_str)
            if rid not in rooms_drawn:
                continue
            home = cost_home.get(cid_str)
            if home == rid:
                continue
            pal = cid_to_pal.get(cid, [])
            for v in pal:
                if v != 0 and v != 250:
                    cost_foreign.add(v)

        all_used = bg | obim | cost_local | cost_foreign
        global_used |= all_used
        rows.append((rid, names.get(rid, '?'), bg, obim, cost_local, cost_foreign, all_used))

    # Dump per-room JSON for tooling. For each room, record:
    #   - slots actually referenced (bg/obim/cost_local/cost_foreign)
    #   - free slots in [48..255] (= 48..255 minus any used here)
    #   - free slots that are also UNUSED globally (safe even if descumm
    #     missed something, since no costume in any room references them)
    out_json = {}
    for rid, name, bg, obim, cl, cf, all_ in rows:
        free_high = [s for s in range(48, 256) if s not in all_]
        free_globally = [s for s in free_high if s not in global_used]
        out_json[rid] = {
            'name': name,
            'bg_slots':      sorted(bg),
            'obim_slots':    sorted(obim),
            'cost_local':    sorted(cl),
            'cost_foreign':  sorted(cf),
            'all_used':      sorted(all_),
            'free_high':     free_high,        # free in [48..255] for THIS room
            'free_globally': free_globally,    # also free globally — safest
        }
    out_path = os.path.join(os.path.dirname(__file__), 'slot_usage_survey.json')
    with open(out_path, 'w') as f:
        json.dump(out_json, f, indent=2)
    print(f'Wrote {out_path}', file=sys.stderr)

    # Print per-room rows
    print(f'{"rid":>4} {"name":>14}  '
          f'{"bg":>3} {"obim":>4} {"cost":>4} {"frgn":>4}  total  '
          f'in[16..47]={"":<4}  in[48..255]={"":<4}  unique_high_slots')
    for rid, name, bg, obim, cl, cf, all_ in rows:
        in_range = sum(1 for s in all_ if 16 <= s <= 47)
        in_high  = sum(1 for s in all_ if 48 <= s <= 255)
        high_slots = sorted(s for s in all_ if 48 <= s <= 255)
        # Compress consecutive runs
        runs = []
        for s in high_slots:
            if runs and runs[-1][1] + 1 == s:
                runs[-1] = (runs[-1][0], s)
            else:
                runs.append((s, s))
        runs_str = ','.join(f'{a}-{b}' if a != b else str(a) for a, b in runs)
        print(f'{rid:>4} {name:>14}  {len(bg):>3} {len(obim):>4} {len(cl):>4} {len(cf):>4}  '
              f'{len(all_):>5}  '
              f'{in_range:>2}/{32:<2}    {in_high:>3}/{208:<3}    {runs_str}')

    print()
    print(f'Globally used slots ({len(global_used)} unique):')
    in_range = sorted(s for s in global_used if 16 <= s <= 47)
    in_high = sorted(s for s in global_used if 48 <= s <= 255)
    in_low = sorted(s for s in global_used if s < 16)
    print(f'  [0..15]   used: {in_low}')
    print(f'  [16..47]  used: {in_range}')
    print(f'  [48..255] used: count={len(in_high)}; '
          f'unused={[s for s in range(48, 256) if s not in global_used]}')


if __name__ == '__main__':
    main()
