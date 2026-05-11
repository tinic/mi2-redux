#!/usr/bin/env python3
"""Helpers for reading PC SCUMM v5 data (pc-data/MONKEY2.{000,001}).

PC and Amiga MI2 share the same logical data structure:
  - DCOS / DROO indexes are byte-identical (same 199 costumes, same 110 rooms)
  - COST chunk pixel data decodes to the SAME indexed bitmaps (verified
    100/100 frames for Guybrush) — only the RLE scan order differs:
      * PC uses column-major (byleRLEDecode in base-costume.cpp)
      * Amiga uses row-major (byleRLEDecode_ami in costume.cpp)
  - CLUT chunks (the per-room palette) DIFFER:
      * PC's CLUT[16..47] holds the original VGA-quality colours
      * Amiga's CLUT[16..47] is the OCS-quantized version of the PC palette

We use PC CLUT as the "high-fidelity ground truth" when rendering
costume frames for joint --best quantization, instead of feeding the
already-quantized Amiga colours back into the quantizer.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import be32, load

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




PC_DATA_FILE  = f'{REPO_ROOT}/pc-data/MONKEY2.001'
PC_INDEX_FILE = f'{REPO_ROOT}/pc-data/MONKEY2.000'


def parse_pc_loff(pc):
    """Return {room_id: file_offset} from PC's LECF/LOFF chunk."""
    p = 8
    if pc[p:p+4] != b'LOFF':
        return {}
    sz = be32(pc, p + 4)
    body = pc[p + 8:p + sz]
    n = body[0]
    out = {}
    for i in range(n):
        rid = body[1 + i * 5]
        off = struct.unpack('<I', body[1 + i * 5 + 1:1 + i * 5 + 5])[0]
        out[rid] = off
    return out


def parse_pc_dcos():
    """Return {costume_id: (file_num, room_id_relative_offset)} for every PC
    costume that's actually present in the data."""
    idx = load(PC_INDEX_FILE)
    p = 0
    while p < len(idx):
        tag = idx[p:p+4]
        sz = be32(idx, p + 4)
        if tag == b'DCOS':
            cnt = struct.unpack('<H', idx[p + 8:p + 10])[0]
            body = idx[p + 10:p + 10 + cnt * 5]
            out = {}
            for i in range(cnt):
                fn = body[i]
                off = struct.unpack('<I',
                                    body[cnt + i * 4:cnt + i * 4 + 4])[0]
                if fn != 0:
                    out[i] = (fn, off)
            return out
        p += sz
    return {}


def find_pc_room_clut(pc, room_id, loff_cache=None):
    """Return PC CLUT bytes (768 bytes — 256 RGB triples) for `room_id`,
    or None if the room has no CLUT chunk."""
    if loff_cache is None:
        loff_cache = parse_pc_loff(pc)
    if room_id not in loff_cache:
        return None
    room_off = loff_cache[room_id]
    sz = be32(pc, room_off + 4)
    if pc[room_off:room_off + 4] != b'ROOM':
        return None
    p = room_off + 8
    end = room_off + sz
    while p < end:
        if p + 8 > end:
            break
        tag = pc[p:p+4]
        csz = be32(pc, p + 4)
        if tag == b'CLUT' and csz >= 8 + 768:
            return bytes(pc[p + 8:p + 8 + 768])
        if csz < 8 or p + csz > end:
            break
        p += csz
    return None


def find_pc_cost_body(pc, costume_id, dcos_cache=None, loff_cache=None):
    """Return (cost_offset_in_pc, cost_size, body_bytes) for the PC version
    of the given costume, or None if not present.

    DCOS gives (file_num, offset). For PC, file_num is the room_id (where
    this costume lives) and the offset is *relative to that room's LFLF
    parent*. PC stores rooms directly under LECF without an LFLF wrapper —
    LOFF[room_id] points at the ROOM chunk start, but DCOS offsets are
    relative to that ROOM's LFLF (which is conceptually the same address
    as the ROOM start in PC's flat layout, since there's no extra wrapper)."""
    if dcos_cache is None:
        dcos_cache = parse_pc_dcos()
    if loff_cache is None:
        loff_cache = parse_pc_loff(pc)
    if costume_id not in dcos_cache:
        return None
    file_num, dcos_off = dcos_cache[costume_id]
    # file_num is the home-room id; DCOS offset is from that room's LFLF/ROOM start.
    if file_num not in loff_cache:
        return None
    base = loff_cache[file_num]
    cost_off = base + dcos_off
    if cost_off + 8 > len(pc):
        return None
    if pc[cost_off:cost_off + 4] != b'COST':
        return None
    sz = be32(pc, cost_off + 4)
    body = bytes(pc[cost_off + 8:cost_off + sz])
    return cost_off, sz, body


def find_pc_cost_for_amiga_lflf(pc, dcos_cache, am_costume_ids):
    """For each Amiga costume_id (e.g. all costumes hosted in a given Amiga
    LFLF), find the PC COST body. Returns dict {costume_id: body_bytes}."""
    loff_cache = parse_pc_loff(pc)
    out = {}
    for cid in am_costume_ids:
        r = find_pc_cost_body(pc, cid, dcos_cache, loff_cache)
        if r is not None:
            _, _, body = r
            out[cid] = body
    return out
