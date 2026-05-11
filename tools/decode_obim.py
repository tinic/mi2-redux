#!/usr/bin/env python3
"""Decode OBIM (object image) chunks from a room into PNGs.

For each OBIM, extracts each IM01/IM02/.../IMnn frame: decodes its SMAP using
the same codec set as the room background, renders with the room CLUT[16..47]
palette + paletteMod=16 (so indices 0..31 map to CLUT[16..47]).

Outputs PNG files named obim_<obj_id>_frame<NN>.png in --out-dir.
"""
import os, sys, struct
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (


    load, be32, le32, le16, name as chunk_name, walk_rooms, find_chunk,
    decode_zigzag_h, decode_zigzag_v, decode_majmin_h,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def decode_smap(d, smap_off, w, h):
    """Decode SMAP body into indexed bytearray. Returns (img, set_of_unhandled_codecs)."""
    smap_sz = be32(d, smap_off+4)
    body = smap_off + 8
    num_strips = w // 8
    strip_offs = [le32(d, body + i*4) for i in range(num_strips)]
    strip_ends = list(strip_offs[1:]) + [smap_sz]
    out = bytearray(w * h)
    skipped = set()
    for si, (start, end) in enumerate(zip(strip_offs, strip_ends)):
        codec = d[smap_off + start]
        sb = d[smap_off + start + 1 : smap_off + end]
        shr = codec % 10
        if   14 <= codec <= 18:  pix = decode_zigzag_v(sb, h, shr)
        elif 24 <= codec <= 28:  pix = decode_zigzag_h(sb, h, shr)
        elif 34 <= codec <= 38:  pix = decode_zigzag_v(sb, h, shr)   # transparent V variant
        elif 44 <= codec <= 48:  pix = decode_zigzag_h(sb, h, shr)   # transparent H variant
        elif 64 <= codec <= 68:  pix = decode_majmin_h(sb, h, shr)
        elif 84 <= codec <= 88:  pix = decode_majmin_h(sb, h, shr)   # transparent MAJMIN
        else:                    pix = bytes(8*h); skipped.add(codec)
        for y in range(h):
            for x in range(8):
                out[y*w + si*8 + x] = pix[y*8 + x]
    return out, skipped


def render_indexed(indexed, palette_256, paletteMod, w, h):
    rgb = bytearray(w * h * 3)
    for i, idx in enumerate(indexed):
        r, g, b = palette_256[(idx + paletteMod) & 0xFF]
        rgb[i*3] = r; rgb[i*3+1] = g; rgb[i*3+2] = b
    return Image.frombytes('RGB', (w, h), bytes(rgb))


def decode_room_obims(disk_path, room_id, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    d = load(disk_path)
    rooms = list(walk_rooms(d))
    ro = next(o for r, o in rooms if r == room_id)
    rsz = be32(d, ro+4)
    # Get room palette
    clut_off, clut_sz = find_chunk(d, ro+8, ro+rsz, 'CLUT')
    pal_body = d[clut_off+8 : clut_off+clut_sz]
    palette = [tuple(pal_body[i*3:i*3+3]) for i in range(256)]

    # Walk OBIMs
    p = ro + 8
    end = ro + rsz
    results = []
    obim_idx = 0
    while p < end:
        nm = chunk_name(d, p)
        sz = be32(d, p+4)
        if nm == 'OBIM':
            ip = p + 8
            ipend = p + sz
            obj_id = None
            num_imgs = 0
            w_obj = h_obj = 0
            # Read IMHD first
            while ip < ipend:
                inm = chunk_name(d, ip)
                isz = be32(d, ip+4)
                if inm == 'IMHD':
                    body = d[ip+8:ip+isz]
                    obj_id = struct.unpack('<H', body[0:2])[0]
                    num_imgs = struct.unpack('<H', body[2:4])[0]
                    w_obj = struct.unpack('<H', body[12:14])[0]
                    h_obj = struct.unpack('<H', body[14:16])[0]
                elif inm.startswith('IM') and len(inm) == 4:
                    # IM01..IM07 contains SMAP
                    smap_off, smap_sz = find_chunk(d, ip+8, ip+isz, 'SMAP')
                    if smap_off is not None and w_obj > 0 and h_obj > 0:
                        try:
                            indexed, skipped = decode_smap(d, smap_off, w_obj, h_obj)
                            img = render_indexed(indexed, palette, 16, w_obj, h_obj)
                            fn = f'obim_{obj_id}_{inm}.png'
                            img.save(os.path.join(out_dir, fn))
                            results.append((fn, obj_id, inm, w_obj, h_obj, skipped))
                        except Exception as e:
                            print(f"  failed {inm} for obj {obj_id}: {e}")
                ip += isz
            obim_idx += 1
        p += sz
    return results


if __name__ == '__main__':
    disk = sys.argv[1] if len(sys.argv) > 1 else f'{REPO_ROOT}/amiga-data/monkey2.010'
    room = int(sys.argv[2]) if len(sys.argv) > 2 else 87
    outdir = sys.argv[3] if len(sys.argv) > 3 else f'{REPO_ROOT}/preview/obim-r{room}'
    res = decode_room_obims(disk, room, outdir)
    for fn, oid, frame, w, h, skipped in res:
        marker = f' (skipped codecs: {skipped})' if skipped else ''
        print(f"{fn}: obj {oid} {frame} {w}x{h}{marker}")
    print(f"\n{len(res)} frames -> {outdir}")
