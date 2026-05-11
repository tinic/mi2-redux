import os
#!/usr/bin/env python3
"""For each costume in a given LFLF, remap its palette table so entries pointing
into CLUT[16..47] (our patched range) now point at the slot whose NEW colour
is closest to the costume designer's ORIGINAL intent.

A costume body (after the 8-byte chunk header) is laid out as:
  bytes [0..1]   : (size - 6) little-endian  (per ScummVM costume.cpp)
  bytes [2..5]   : magic / version
  byte  [6]      : number of anims (== 16/32 - 1, per format flag)
  byte  [7]      : format flags. bit 0: 0=16-color palette, 1=32-color palette
  bytes [8..8+pal_size]  : palette table  ← what we update
  ... rest is anim definitions + RLE pixel data, untouched ...

We take the original CLUT (256 entries) and the new CLUT (after patching) and,
for each palette-table entry in [16..47], find the closest match in the new
CLUT[16..47] to the original CLUT[old_idx]'s colour. CLUT[0..15] (EGA) and
CLUT[48..255] are kept; we only ever rewrite entries that touch our patched
art-palette range.
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import load, be32, name as chunk_name
from lock_palette_slots import oklab_dist

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




def remap_one_costume(d, cost_off, orig_clut, new_clut):
    """Mutate the costume's palette table at file offset cost_off, in place.

    SCUMM v5 costume layout (after the 8-byte chunk header):
      body[0]    : numAnim
      body[1]    : format byte (0x58 = 16-color, 0x59 = 32-color, masked &0x7F)
      body[2..]  : palette table (length determined by format)
    Reference: ScummVM engines/scumm/costume.cpp loadCostume.
    """
    body_start = cost_off + 8
    fmt = d[body_start + 1] & 0x7F
    pal_size = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, None)
    if pal_size is None:
        return 0  # unknown format; skip
    pal_start = body_start + 2
    changes = 0
    for i in range(pal_size):
        old_clut_idx = d[pal_start + i]
        if 16 <= old_clut_idx <= 47:
            old_color = orig_clut[old_clut_idx]
            new_idx = min(range(16, 48), key=lambda j: oklab_dist(new_clut[j], old_color))
            if new_idx != old_clut_idx:
                d[pal_start + i] = new_idx
                changes += 1
    return changes


def find_costumes_in_lflf(d, lflf_off):
    """Return list of file-offsets of COST chunks inside the LFLF at lflf_off."""
    out = []
    sz = be32(d, lflf_off+4)
    p = lflf_off + 8
    end = lflf_off + sz
    while p < end:
        nm = chunk_name(d, p)
        if not all(32 <= c < 127 for c in d[p:p+4]):
            break
        csz = be32(d, p+4)
        if csz < 8 or p + csz > end:
            break
        if nm == 'COST':
            out.append(p)
        p += csz
    return out


def find_lflf_for_room(d, room_id):
    """Walk top-level LECF children, find the LFLF containing the given room id."""
    end = 8 + be32(d, 4)
    loff_sz = be32(d, 12)
    p = 8 + loff_sz
    while p < end:
        nm = chunk_name(d, p)
        if nm != 'LFLF': break
        csz = be32(d, p+4)
        # First child is ROOM
        inner = p + 8
        if chunk_name(d, inner) == 'ROOM':
            # Extract room id by reading its RMHD (we'd need to know how the engine identifies rooms;
            # easier: use LOFF table room_id → offset, find the ROOM that matches our target offset.)
            pass
        p += csz
    # Fallback: use LOFF mapping directly
    # LOFF body: u8 num, num × (u8 room_id, u32 LE offset to ROOM-or-LFLF)
    num = d[16]
    for i in range(num):
        r_id = d[17 + i*5]
        off = struct.unpack('<I', d[17+i*5+1:17+i*5+5])[0]
        if r_id == room_id:
            # off points to ROOM. Walk back to find the enclosing LFLF.
            # Top-level scan
            end = 8 + be32(d, 4)
            loff_sz = be32(d, 12)
            p = 8 + loff_sz
            while p < end:
                nm = chunk_name(d, p)
                if nm == 'LFLF':
                    csz = be32(d, p+4)
                    if p < off < p + csz:
                        return p
                    p += csz
                else:
                    break
            return None
    return None


# patch_lflf_costumes removed 2026-05-08: tree-rebuild pipeline in
# inject_room.py now handles costume pal_table NN-remap inline as part
# of its tree mutations.


if __name__ == '__main__':
    # Smoke test: print costume palettes for room 87 BEFORE remap
    d = load(f'{REPO_ROOT}/amiga-data/monkey2.010')
    lflf = find_lflf_for_room(d, 87)
    print(f"LFLF for room 87: 0x{lflf:x}")
    for co in find_costumes_in_lflf(d, lflf):
        sz = be32(d, co+4)
        body_start = co + 8
        fmt = d[body_start + 1] & 0x7F
        pal_size = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, None)
        if pal_size is None:
            print(f"  COST @0x{co:x}: unknown format 0x{fmt:02x}")
            continue
        pal = list(d[body_start+2 : body_start+2+pal_size])
        in_range = [p for p in pal if 16 <= p <= 47]
        print(f"  COST @0x{co:x} size={sz} fmt=0x{fmt:02x} pal_size={pal_size}: full palette={pal}")
        print(f"    in [16..47]: {sorted(set(in_range))}")
