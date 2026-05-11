#!/usr/bin/env python3
"""Pre-decode every pristine asset in amiga-data/ into a single pickle.

Writes tools/pristine_cache.pkl. Loads ~2 seconds, then every
inject_room.py run can ask the cache for pristine values without
re-decoding the disk files. Eliminates the entire class of
"chained-state was read instead of pristine" bugs we hit during the
joint-costume-requant work.

Cache schema (Python pickle, version 1):

  {
    'version': 1,
    'droo':  {room_id (int) -> disk_n (int)},          # from monkey2.000 DROO
    'dcos':  {cost_id (int) -> home_room_id (int)},    # from monkey2.000 DCOS
    'rooms': {
      room_id: {
        'disk':       int,
        'lflf_of':   int,                  # LFLF byte offset within disk file
        'room_of':   int,                  # ROOM byte offset within disk file
        'clut':       bytes (768),          # full 256-entry CLUT
        'trns':       int,                  # TRNS sentinel byte
        'costumes':   [                      # in DCOS order (LFLF position == idx)
          {
            'cost_id':  int,
            'cost_of': int,                # within disk file
            'cost_sz':  int,                # full chunk size including 8-byte header
            'fmt':      int,                # & 0x7F: 0x58/0x59/0x60/0x61
            'npal':     int,                # 16 or 32
            'pal_table': bytes (npal),
            'body':      bytes (cost_sz - 8),  # full body
            'frame_offsets_in_body': [int, ...],   # ordered as decode_cost yields
            'frames':    [{'w':int,'h':int,'pixels':bytes}, ...],
          }, ...
        ],
      },
      ...
    },
  }
"""

import os
import pickle
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))

from decode_amiga_room import (
    load, be32, walk_rooms, find_chunk, name as cn,
)
from decode_cost import decode_costume, walk_frame_offsets
from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
CACHE_PATH = f'{REPO_ROOT}/tools/pristine_cache.pkl'


def parse_droo(idx_data):
    p = 0
    while p < len(idx_data):
        tag = bytes(idx_data[p:p+4])
        sz = be32(idx_data, p + 4)
        if tag == b'DROO':
            cnt = struct.unpack('<H', idx_data[p+8:p+10])[0]
            file_nums = list(idx_data[p+10:p+10+cnt])
            return {rid: fn for rid, fn in enumerate(file_nums) if 0 < fn < 12}
        if sz < 8: break
        p += sz
    return {}


def parse_dcos(idx_data):
    p = 0
    while p < len(idx_data):
        tag = bytes(idx_data[p:p+4])
        sz = be32(idx_data, p + 4)
        if tag == b'DCOS':
            cnt = struct.unpack('<H', idx_data[p+8:p+10])[0]
            home_ids = list(idx_data[p+10:p+10+cnt])
            return {cid: rid for cid, rid in enumerate(home_ids) if rid > 0}
        if sz < 8: break
        p += sz
    return {}


def build_cache():
    idx_data = load(f'{AMIGA_DIR}/monkey2.000')
    droo = parse_droo(idx_data)
    dcos = parse_dcos(idx_data)
    print(f"DROO: {len(droo)} rooms; DCOS: {len(dcos)} costumes")

    # Group costume_ids by their home_room (in cost_id order)
    home_to_costs = {}
    for cid, rid in dcos.items():
        home_to_costs.setdefault(rid, []).append(cid)
    for rid in home_to_costs:
        home_to_costs[rid].sort()

    rooms = {}
    disk_cache = {}
    for rid, disk_n in sorted(droo.items()):
        if disk_n not in disk_cache:
            path = f'{AMIGA_DIR}/monkey2.{disk_n:03d}'
            if not os.path.exists(path):
                continue
            disk_cache[disk_n] = load(path)
        d = disk_cache[disk_n]

        # Find ROOM offset within this disk
        try:
            ro = next(o for r, o in walk_rooms(d) if r == rid)
        except StopIteration:
            continue
        room_size = be32(d, ro + 4)

        # CLUT (always 768 bytes after 8-byte header)
        clut_off, clut_sz = find_chunk(d, ro + 8, ro + room_size, 'CLUT')
        clut = bytes(d[clut_off + 8:clut_off + 8 + 768]) if clut_off else b''

        # TRNS — single byte
        trns_off, trns_sz = find_chunk(d, ro + 8, ro + room_size, 'TRNS')
        trns_value = d[trns_off + 8] if trns_off is not None else 1

        # LFLF offset (parent of ROOM)
        lflf_off = find_lflf_for_room(d, rid)

        # Costumes hosted in this LFLF (in DCOS order). Some rooms
        # have physical COST chunks that DCOS doesn't reference
        # (orphans — leftover from MI2 dev workflow, e.g. bar has 8
        # COSTs but only 7 DCOS entries point at it). We track them
        # too with cid=0 so the position-aligned zip in inject_room
        # stays consistent; downstream skips them via the npal==0
        # branch or the cid==0 guard.
        costumes = []
        if lflf_off is not None:
            cost_offsets = find_costumes_in_lflf(d, lflf_off)
            hosted_cids = home_to_costs.get(rid, [])
            for pos, cost_off in enumerate(cost_offsets):
                cid = hosted_cids[pos] if pos < len(hosted_cids) else 0
                cost_sz = be32(d, cost_off + 4)
                body = bytes(d[cost_off + 8:cost_off + cost_sz])
                if len(body) < 4:
                    continue
                fmt = body[1] & 0x7F
                npal = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, 0)
                if npal == 0:
                    costumes.append({
                        'cost_id': cid, 'cost_of': cost_off, 'cost_sz': cost_sz,
                        'fmt': fmt, 'npal': 0, 'pal_table': b'',
                        'body': body, 'frame_offsets_in_body': [], 'frames': [],
                    })
                    continue
                pal_table = bytes(body[2:2 + npal])
                frames_raw, _ = decode_costume(body, column_major=False)
                frame_offsets = walk_frame_offsets(body)
                frames = [
                    {'w': w, 'h': h, 'pixels': bytes(p)}
                    for (w, h, p) in frames_raw
                ]
                costumes.append({
                    'cost_id': cid, 'cost_of': cost_off, 'cost_sz': cost_sz,
                    'fmt': fmt, 'npal': npal, 'pal_table': pal_table,
                    'body': body, 'frame_offsets_in_body': frame_offsets,
                    'frames': frames,
                })

        rooms[rid] = {
            'disk': disk_n, 'lflf_of': lflf_off, 'room_of': ro,
            'clut': clut, 'trns': trns_value,
            'costumes': costumes,
        }
        if costumes:
            print(f"  room {rid:3d} (disk {disk_n}): {len(costumes)} costumes "
                  f"({sum(len(c['frames']) for c in costumes)} frames total)")

    # Synthesize variant cids declared in tools/variants.json (see the
    # _comment in that file). Each variant becomes a NEW costume entry
    # in target_room with body cloned from src_cid's pristine COST, but
    # cost_id rewritten to new_cid. inject_room.py will then re-encode
    # it against the target room's joint --best palette like any other
    # home-LFLF costume, and the DCOS table gets the new entry on the
    # next index rebuild.
    variants_path = os.path.join(os.path.dirname(__file__), 'variants.json')
    if os.path.exists(variants_path):
        import json
        from decode_all import parse_index_room_names
        names = parse_index_room_names()
        name_to_rid = {n: r for r, n in names.items()}
        vconfig = json.load(open(variants_path))
        n_added = 0
        for v in vconfig.get('variants', []):
            new_cid = v['new_cid']
            src_cid = v['src_cid']
            target_room = v['target_room']
            target_rid = name_to_rid.get(target_room)
            if target_rid is None:
                print(f"  [variant] {target_room}: unknown room — skipping")
                continue
            src_home_rid = dcos.get(src_cid)
            if src_home_rid is None:
                print(f"  [variant] cid {src_cid}: not in DCOS — skipping")
                continue
            src_room = rooms.get(src_home_rid, {})
            src_cost = next((c for c in src_room.get('costumes', [])
                             if c['cost_id'] == src_cid), None)
            if src_cost is None:
                print(f"  [variant] cid {src_cid}: not in pristine cache — skipping")
                continue
            clone = dict(src_cost)
            clone['cost_id'] = new_cid
            # Mark the clone as synthetic so inject_room knows it needs
            # to INSERT a new COST node into the LFLF (since the disk
            # itself doesn't have it yet).
            clone['variant'] = {'src_cid': src_cid, 'target_room': target_room}
            rooms[target_rid]['costumes'].append(clone)
            dcos[new_cid] = target_rid
            print(f"  [variant] cid {new_cid} = clone of cid {src_cid} -> "
                  f"rid {target_rid}({target_room})")
            n_added += 1
        if n_added:
            print(f"  Added {n_added} variant cid(s)")

    cache = {'version': 1, 'droo': droo, 'dcos': dcos, 'rooms': rooms}
    with open(CACHE_PATH, 'wb') as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    sz = os.path.getsize(CACHE_PATH)
    print(f"\nWrote {CACHE_PATH} ({sz/1024/1024:.1f} MB)")


if __name__ == '__main__':
    build_cache()
