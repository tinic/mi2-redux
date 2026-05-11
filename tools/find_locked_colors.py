#!/usr/bin/env python3
"""Identify which CLUT[16..47] slots are LOCKED (constant across rooms = UI/dialog
reserved) vs FREE (vary per room = scene art).

For each room with a CLUT chunk, extract entries 16..47. Group by slot index.
A slot is "locked" if its 24-bit RGB value is the same across (almost) all
rooms; "free" otherwise.

OCS hardware uses 12-bit colors (4 bits per channel). Two 24-bit values that
differ only in the low nibble are the SAME OCS color — fold to 12-bit before
comparing.
"""
import os, sys, struct
sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import load, be32, le32, name, walk_rooms, find_chunk

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


DATA_DIR = f'{REPO_ROOT}/amiga-data'

# Collect CLUT[16..47] for every room, keyed by room id
def gather_palettes():
    palettes = {}  # room_id -> list of 32 (r,g,b) tuples
    for disk in range(1, 12):
        p = f'{DATA_DIR}/monkey2.{disk:03d}'
        if not os.path.exists(p): continue
        d = load(p)
        try:
            rooms = list(walk_rooms(d))
        except Exception:
            continue
        for rid, ro in rooms:
            room_size = be32(d, ro+4)
            clut_off, clut_sz = find_chunk(d, ro+8, ro+room_size, 'CLUT')
            if clut_off is None or clut_sz < 8 + 48*3:
                continue
            body = d[clut_off+8 : clut_off+clut_sz]
            pal = [tuple(body[i*3:i*3+3]) for i in range(48)]
            # Default region of interest (we'll override below)
            palettes[rid] = pal[16:48] if not os.environ.get('LOW16') else pal[0:16]
    return palettes


def to_ocs(rgb):
    """Fold to 12-bit OCS by keeping the high nibble of each channel."""
    return tuple(c >> 4 for c in rgb)


def find_locked(palettes):
    n_slots = len(next(iter(palettes.values())))
    n_rooms = len(palettes)
    # For each slot, count how many rooms share each OCS-folded color
    from collections import Counter

    slot_counters = [Counter() for _ in range(n_slots)]
    for pal in palettes.values():
        for i, rgb in enumerate(pal):
            slot_counters[i][to_ocs(rgb)] += 1
    base = 0 if os.environ.get('LOW16') else 16
    rows = []
    for i, c in enumerate(slot_counters):
        most_common, count = c.most_common(1)[0]
        frac = count / n_rooms
        r24 = (most_common[0] << 4) | most_common[0]
        g24 = (most_common[1] << 4) | most_common[1]
        b24 = (most_common[2] << 4) | most_common[2]
        rows.append((base+i, frac, count, n_rooms, (r24, g24, b24), most_common))
    return rows


def main():
    palettes = gather_palettes()
    print(f"Rooms with CLUT: {len(palettes)}")
    rows = find_locked(palettes)
    rows.sort(key=lambda r: -r[1])
    print(f"\n{'CLUT idx':<10}{'fraction':<11}{'count':<10}{'OCS RGB':<14}{'24-bit hex'}")
    print('-' * 60)
    for slot, frac, count, n, rgb24, ocs in rows:
        marker = " ★ LOCKED" if frac >= 0.80 else ""
        print(f"{slot:<10}{frac:<11.2%}{count}/{n:<6}{ocs}    #{rgb24[0]:02X}{rgb24[1]:02X}{rgb24[2]:02X}{marker}")
    locked = [r for r in rows if r[1] >= 0.80]
    print(f"\n{len(locked)} slots are locked across ≥80% of rooms.")
    print("Locked slots (sorted by index):")
    for slot, frac, count, n, rgb24, ocs in sorted(locked):
        print(f"  CLUT[{slot}] = #{rgb24[0]:02X}{rgb24[1]:02X}{rgb24[2]:02X}  ({frac:.0%} of {n} rooms)")


if __name__ == '__main__':
    main()
