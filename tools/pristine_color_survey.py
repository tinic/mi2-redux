#!/usr/bin/env python3
"""For each pristine Amiga room, count how many unique RGB colours
result from rendering: bg SMAP + every OBIM frame + every LFLF-hosted
costume's frames, all through that room's PRISTINE CLUT.

This is the same model as finalsheet.py but on UNTOUCHED amiga-data/.
Tells us whether the original 1992 Amiga port ever produces more than
32 unique colours per room when rendered through ScummVM's permissive
8-bit pipeline.
"""

import os
import sys
import struct
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (
    load, be32, walk_rooms, find_chunk, name as cn, le16,
)
from decode_obim import decode_smap
from decode_cost import decode_costume
from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf
from decode_all import parse_index_room_names
from pristine_cache import cache as pristine_cache

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



AMIGA_DIR = f'{REPO_ROOT}/amiga-data'


def collect_obim_frames(d, ro, room_size):
    p = ro + 8
    end = ro + room_size
    while p < end:
        if p + 8 > end: break
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag == 'OBIM':
            ip = p + 8; ipend = p + sz
            w_obj = h_obj = 0
            while ip < ipend:
                inm = cn(d, ip); isz = be32(d, ip + 4)
                if inm == 'IMHD' and isz >= 26:
                    w_obj = le16(d, ip + 8 + 14)
                    h_obj = le16(d, ip + 8 + 16)
                elif inm.startswith('IM') and len(inm) == 4 and w_obj > 0:
                    smap_o, _ = find_chunk(d, ip + 8, ip + isz, 'SMAP')
                    if smap_o is not None:
                        try:
                            indexed, _ = decode_smap(d, smap_o, w_obj, h_obj)
                            yield (w_obj, h_obj, indexed)
                        except Exception:
                            pass
                if isz < 8: break
                ip += isz
        if sz < 8 or p + sz > end: break
        p += sz


def main():
    names = parse_index_room_names(f'{AMIGA_DIR}/monkey2.000')

    rows = []
    for rid in sorted(pristine_cache.data['rooms']):
        room = pristine_cache.data['rooms'][rid]
        disk_n = room['disk']
        path = f'{AMIGA_DIR}/monkey2.{disk_n:03d}'
        if not os.path.exists(path): continue
        d = load(path)
        try:
            ro = next(o for r, o in walk_rooms(d) if r == rid)
        except StopIteration:
            continue
        rsz = be32(d, ro + 4)

        clut = room['clut']
        rmhd_off, _ = find_chunk(d, ro + 8, ro + rsz, 'RMHD')
        bg_w = le16(d, rmhd_off + 8)
        bg_h = le16(d, rmhd_off + 10)
        rmim_off, rmim_sz = find_chunk(d, ro + 8, ro + rsz, 'RMIM')

        # Track unique CLUT slots (= unique colour positions referenced)
        # AND unique RGB values (= colours actually rendered).
        bg_slots = set()
        obim_slots = set()
        cost_slots = set()
        unique_rgbs = set()

        # Bg
        if rmim_off is not None:
            im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
            if im00_off is not None:
                smap_off, _ = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
                if smap_off is not None:
                    try:
                        idx, _ = decode_smap(d, smap_off, bg_w, bg_h)
                        for v in set(idx):
                            slot = (v + 16) & 0xFF
                            bg_slots.add(slot)
                            unique_rgbs.add((clut[slot * 3], clut[slot * 3 + 1], clut[slot * 3 + 2]))
                    except Exception:
                        pass

        # OBIM
        for w_obj, h_obj, idx in collect_obim_frames(d, ro, rsz):
            for v in set(idx):
                if v == 0: continue   # transparent for OBIMs
                slot = (v + 16) & 0xFF
                obim_slots.add(slot)
                unique_rgbs.add((clut[slot * 3], clut[slot * 3 + 1], clut[slot * 3 + 2]))

        # Costumes
        lflf = find_lflf_for_room(d, rid)
        if lflf is not None:
            for co in find_costumes_in_lflf(d, lflf):
                sz = be32(d, co + 4)
                body = bytes(d[co + 8:co + sz])
                fmt = body[1] & 0x7F
                if fmt not in (0x58, 0x59):
                    continue
                npal = 16 if fmt == 0x58 else 32
                pal_table = list(body[2:2 + npal])
                try:
                    frames, _ = decode_costume(body)
                except Exception:
                    continue
                for w, h, pix in frames:
                    for v in set(pix):
                        if v == 0 or v >= len(pal_table): continue
                        slot = pal_table[v]
                        cost_slots.add(slot)
                        unique_rgbs.add((clut[slot * 3], clut[slot * 3 + 1], clut[slot * 3 + 2]))

        rows.append((rid, names.get(rid, '?'),
                      len(bg_slots), len(obim_slots), len(cost_slots),
                      len(unique_rgbs)))

    print(f'{"rid":>4} {"name":>14}  {"bg":>3} {"obim":>4} {"cost":>4}  {"total RGB":>9}')
    over_32 = 0
    for rid, n, bg, obim, cost, urgb in rows:
        marker = ' >32 ←' if urgb > 32 else ''
        if urgb > 32: over_32 += 1
        print(f'{rid:>4} {n:>14}  {bg:>3} {obim:>4} {cost:>4}  {urgb:>9}{marker}')

    print()
    print(f'{over_32}/{len(rows)} rooms render >32 unique RGBs in ScummVM (pristine).')


if __name__ == '__main__':
    main()
