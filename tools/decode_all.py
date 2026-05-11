#!/usr/bin/env python3
"""Decode every Amiga MI2 room to PNG and parse room names from index."""
import os, struct, sys
sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (
    load, be32, le32, le16, name, walk_rooms, find_chunk,
    decode_zigzag_h, decode_zigzag_v, decode_majmin_h,
)
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


OUT = f'{REPO_ROOT}/preview/amiga-rooms'
os.makedirs(OUT, exist_ok=True)


def parse_index_room_names(path=f'{REPO_ROOT}/amiga-data/monkey2.000'):
    """Parse RNAM (room names) chunk from MI2 index. Returns {room_id: name}."""
    d = load(path)
    # SCUMM v5 index layout: top-level chunks each as [4-byte name][4-byte BE size][body]
    p = 0
    names = {}
    while p < len(d):
        nm = name(d, p)
        sz = be32(d, p+4)
        if nm == 'RNAM':
            body = d[p+8:p+sz]
            i = 0
            while i + 1 < len(body):
                rid = body[i]
                if rid == 0: break
                # 9 bytes XOR'd with 0xFF, NUL-terminated
                raw = body[i+1:i+10]
                decoded = bytes(b ^ 0xFF for b in raw).split(b'\x00')[0]
                names[rid] = decoded.decode('ascii', errors='replace')
                i += 10
            break
        p += sz
    return names


def render_room(d, ro, room_id, room_name, palette_mod=16):
    room_size = be32(d, ro+4)
    rmhd_off, _ = find_chunk(d, ro+8, ro+room_size, 'RMHD')
    if rmhd_off is None:
        return None
    w = le16(d, rmhd_off+8); h = le16(d, rmhd_off+10)

    clut_off, clut_sz = find_chunk(d, ro+8, ro+room_size, 'CLUT')
    if clut_off is None:
        return None
    pal_body = d[clut_off+8 : clut_off+clut_sz]
    pal = [tuple(pal_body[i*3:i*3+3]) for i in range(256)]

    rmim_off, rmim_sz = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    if rmim_off is None: return None
    im00_off, im00_sz = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
    if im00_off is None: return None
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')
    if smap_off is None: return None

    num_strips = w // 8
    body_off = smap_off + 8
    if body_off + num_strips*4 > smap_off + smap_sz:
        return None
    strip_offsets = [le32(d, body_off + i*4) for i in range(num_strips)]
    strip_ends = list(strip_offsets[1:]) + [smap_sz]
    img = bytearray(w * h)
    skipped_codecs = set()
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        if smap_off + start >= len(d): continue
        codec = d[smap_off + start]
        strip_bytes = d[smap_off + start + 1 : smap_off + end]
        shr = codec % 10
        pix = None
        try:
            if 14 <= codec <= 18:
                pix = decode_zigzag_v(strip_bytes, h, shr)
            elif 24 <= codec <= 28:
                pix = decode_zigzag_h(strip_bytes, h, shr)
            elif 64 <= codec <= 68:
                pix = decode_majmin_h(strip_bytes, h, shr)
        except Exception as e:
            print(f"    strip {si} codec {codec}: {e}")
        if pix is not None:
            for y in range(h):
                for x in range(8):
                    img[y*w + si*8 + x] = pix[y*8 + x]
        else:
            skipped_codecs.add(codec)

    rgb = bytearray(w * h * 3)
    for i, idx in enumerate(img):
        r,g,b = pal[(idx + palette_mod) & 0xFF]
        rgb[i*3] = r; rgb[i*3+1] = g; rgb[i*3+2] = b
    out_name = f"{room_id:03d}_{room_name}.png" if room_name else f"{room_id:03d}.png"
    out_path = os.path.join(OUT, out_name)
    Image.frombytes('RGB', (w, h), bytes(rgb)).save(out_path)
    return out_path, w, h, skipped_codecs


def main():
    names_map = parse_index_room_names()
    print(f"Index has {len(names_map)} room names")
    total = 0
    for disk in range(1, 12):
        path = f'{REPO_ROOT}/amiga-data/monkey2.{disk:03d}'
        if not os.path.exists(path):
            continue
        d = load(path)
        try:
            rooms = list(walk_rooms(d))
        except Exception as e:
            print(f"  disk {disk}: walk_rooms failed: {e}")
            continue
        print(f"Disk {disk}: {len(rooms)} rooms ({[r[0] for r in rooms]})")
        for rid, ro in rooms:
            rn = names_map.get(rid, '')
            res = render_room(d, ro, rid, rn)
            if res:
                p, w, h, skipped = res
                marker = f" (skipped: {skipped})" if skipped else ""
                print(f"  room {rid:3d} '{rn:9s}' {w:3d}x{h:3d} -> {os.path.basename(p)}{marker}")
                total += 1
            else:
                print(f"  room {rid:3d} '{rn:9s}' SKIPPED (no RMHD/CLUT/RMIM)")
    print(f"\n{total} rooms decoded → {OUT}")


if __name__ == '__main__':
    main()
