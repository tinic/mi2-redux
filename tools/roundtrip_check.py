import os
#!/usr/bin/env python3
"""Roundtrip test: encode then decode a SMAP body; verify pixel-perfect match.

Takes the indexed bitmap that we ENCODED (the output of inject_room before
SMAP encoding) and the SMAP body my encoder produced; decodes the SMAP body
back; compares pixels and reports first mismatch."""
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (
    load, be32, le32, le16, name as chunk_name, walk_rooms, find_chunk,
    decode_zigzag_h, decode_zigzag_v, decode_majmin_h,
)
from encode_amiga import encode_zigzag_h
from inject_part1 import png_to_indexed
from lock_palette_slots import lock_slots

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def main():
    room_name = sys.argv[1] if len(sys.argv) > 1 else 'dinky-hol'

    # Same flow as inject_room.main(), reproduce the indexed image
    src_pc = f'{REPO_ROOT}/preview/best-from-pc/{room_name}.png'
    if not os.path.exists(src_pc):
        # Try any best/ entry
        import glob

        cand = glob.glob(f'/tmp/{room_name}_best.png')
        if cand: src_pc = cand[0]
    indexed, palette, w, h = png_to_indexed(src_pc, 320, 200 if room_name in ('dinky-hol','part1') else 144)
    if len(palette) < 32:
        palette = palette + [(0, 0, 0)] * (32 - len(palette))
    palette, indexed = lock_slots(palette, indexed)

    # Decode the patched SMAP from disk and compare
    disk_path = f'{REPO_ROOT}/monkey2-hd/monkey2.{10:03d}'  # hardcode disk 10 for dinky-hol
    d = load(disk_path)
    # Find room 87
    rooms = list(walk_rooms(d))
    rid_target = 87
    ro = next(o for r, o in rooms if r == rid_target)
    rsz = be32(d, ro+4)
    rmim_off, rmim_sz = find_chunk(d, ro+8, ro+rsz, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')
    print(f"Patched SMAP for room {rid_target}: at file offset 0x{smap_off:x}, size={smap_sz}")

    body_off = smap_off + 8
    num_strips = w // 8
    strip_offs = [le32(d, body_off + i*4) for i in range(num_strips)]
    strip_ends = list(strip_offs[1:]) + [smap_sz]

    decoded = bytearray(w * h)
    failed_strips = []
    for si, (start, end) in enumerate(zip(strip_offs, strip_ends)):
        codec = d[smap_off + start]
        strip_bytes = d[smap_off + start + 1 : smap_off + end]
        shr = codec % 10
        if 14 <= codec <= 18:    pix = decode_zigzag_v(strip_bytes, h, shr)
        elif 24 <= codec <= 28:  pix = decode_zigzag_h(strip_bytes, h, shr)
        elif 64 <= codec <= 68:  pix = decode_majmin_h(strip_bytes, h, shr)
        else:                     pix = bytes(8*h); failed_strips.append((si, codec))
        for y in range(h):
            for x in range(8):
                decoded[y*w + si*8 + x] = pix[y*8 + x]

    # Compare to the input
    mismatches = []
    for y in range(h):
        for x in range(w):
            if decoded[y*w + x] != indexed[y*w + x]:
                mismatches.append((x, y, indexed[y*w + x], decoded[y*w + x]))
                if len(mismatches) >= 10: break
        if len(mismatches) >= 10: break

    print(f"Total mismatches in first 10: {len(mismatches)}")
    for x, y, exp, got in mismatches:
        print(f"  ({x:3d},{y:3d}) expected={exp} got={got}")
    if failed_strips:
        print(f"Failed strips (no decoder for codec): {failed_strips}")
    if not mismatches:
        print("PERFECT roundtrip ✓")


if __name__ == '__main__':
    main()
