#!/usr/bin/env python3
"""Warn when two cost-groups share palette slots AND have cids drawn in
the same room — those slots can only hold one set of canonical RGBs at
a time, so a clash forces the wrong colours on at least one of the
groups in that room.

Inputs:
  tools/cost_groups.json   group definitions (cids + pal_indices)
  tools/costume_refs.json  refs['drawn_in'][cid] = list of rids

Usage:
  python3 tools/check_group_clashes.py            # warn-only, exit 0
  python3 tools/check_group_clashes.py --strict   # exit 1 on any clash
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
GROUPS = os.path.join(REPO_ROOT, 'tools', 'cost_groups.json')
REFS   = os.path.join(REPO_ROOT, 'tools', 'costume_refs.json')
GLOBAL = os.path.join(REPO_ROOT, 'tools', 'global_actor_palette.json')


def fmt_room(rid, names):
    nm = names.get(rid)
    return f'{rid}({nm})' if nm else str(rid)


def system_reserved_slots():
    """Slots that no group's pal_indices should ever include:
        0  — transparent sentinel (auto-routed by png2amiga)
        1  — HW cursor sprite (locked per-room in inject_room.py)
       17  — white, ScummVM SMAP-17→black bug (locked per-room)
       22..31 — Guybrush (locked per-room when he's drawn)
    """
    reserved = {0, 1, 17}
    label = {0: 'transparent', 1: 'HW-cursor', 17: 'white(SMAP-17)'}
    try:
        gp = json.load(open(GLOBAL))['guybrush']
        for s in range(gp['pal_index_start'], gp['pal_index_end'] + 1):
            reserved.add(s)
            label[s] = 'guybrush'
    except (FileNotFoundError, KeyError):
        pass
    return reserved, label


def main():
    strict = '--strict' in sys.argv[1:]
    config = json.load(open(GROUPS))
    refs   = json.load(open(REFS))
    drawn  = refs['drawn_in']
    reserved, res_label = system_reserved_slots()

    try:
        from decode_all import parse_index_room_names
        names = parse_index_room_names()
    except Exception:
        names = {}

    groups = list(config['groups'])

    # Annotate each group with its rooms set + per-cid room map.
    for g in groups:
        g['_rooms'] = set()
        g['_cid_rooms'] = {}
        for cid in g['cids']:
            rs = set(drawn.get(str(cid), []))
            g['_cid_rooms'][cid] = rs
            g['_rooms'] |= rs

    print(f'check_group_clashes: {len(groups)} group(s) — '
          f'rooms each appears in')
    for g in groups:
        rooms_sorted = sorted(g['_rooms'])
        n_cids = len(g['cids'])
        n_rooms = len(rooms_sorted)
        slot_str = (str(g['pal_indices']) if g['pal_indices']
                    else '[] (no slots)')
        print(f"  {g['name']:18s} slots {slot_str}  "
              f"{n_cids} cid(s), {n_rooms} room(s)")
        for cid in g['cids']:
            cid_rooms = sorted(g['_cid_rooms'][cid])
            if not cid_rooms:
                print(f"      cid {cid:>4d}: (drawn nowhere)")
            else:
                rs = ', '.join(fmt_room(r, names) for r in cid_rooms)
                print(f"      cid {cid:>4d}: {rs}")
    print()

    reserved_clashes = []
    for g in groups:
        bad = sorted(set(g['pal_indices']) & reserved)
        if bad:
            reserved_clashes.append((g['name'], bad))

    clashes = []
    for i, g1 in enumerate(groups):
        for g2 in groups[i + 1:]:
            slot_overlap = sorted(set(g1['pal_indices']) & set(g2['pal_indices']))
            if not slot_overlap:
                continue
            room_overlap = sorted(g1['_rooms'] & g2['_rooms'])
            for rid in room_overlap:
                cids1 = sorted(c for c in g1['cids']
                               if rid in g1['_cid_rooms'][c])
                cids2 = sorted(c for c in g2['cids']
                               if rid in g2['_cid_rooms'][c])
                clashes.append((rid, slot_overlap,
                                g1['name'], cids1, g2['name'], cids2))

    if not clashes and not reserved_clashes:
        print('check_group_clashes: no clashes detected '
              f'({len(groups)} groups inspected)')
        return 0

    if reserved_clashes:
        print(f'check_group_clashes: {len(reserved_clashes)} group(s) '
              f'use system-reserved slots')
        for name, bad in reserved_clashes:
            tags = ', '.join(f'{s}={res_label[s]}' for s in bad)
            print(f'  {name}: pal_indices contain {bad}  ({tags})')

    if clashes:
        print(f'check_group_clashes: {len(clashes)} co-room clash(es)')
        for rid, slots, n1, c1, n2, c2 in clashes:
            print(f'  room {rid:3d}  slots {slots}  '
                  f'{n1} cids {c1}  vs  {n2} cids {c2}')
    return 1 if strict else 0


if __name__ == '__main__':
    sys.exit(main())
