import os
#!/usr/bin/env python3
"""Extract every CLUT colour used by costumes in a given LFLF, render them as a
swatch PNG that can be fed alongside the bg PNG to a shared-palette quantizer.

Each unique colour gets a 16x16 swatch tile (so the quantizer treats it as a
real region of the image, not a single-pixel curiosity)."""
import sys, os, struct, math
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import load, be32, name as chunk_name

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def extract_lflf_costume_colors(disk_path, lflf_off, orig_clut):
    """Walk costumes in the LFLF; return set of (r,g,b) tuples covering CLUT
    entries those costumes reference (in the [16..47] art range)."""
    d = load(disk_path)
    sz = be32(d, lflf_off+4)
    p = lflf_off + 8
    end = lflf_off + sz
    colors = set()
    while p < end:
        nm = chunk_name(d, p)
        if not all(32 <= c < 127 for c in d[p:p+4]):
            break
        csz = be32(d, p+4)
        if csz < 8 or p + csz > end:
            break
        if nm == 'COST':
            body_start = p + 8
            fmt = d[body_start + 1] & 0x7F
            pal_size = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, None)
            if pal_size:
                pal = d[body_start + 2 : body_start + 2 + pal_size]
                for clut_idx in pal:
                    if 16 <= clut_idx <= 47:
                        colors.add(orig_clut[clut_idx])
        p += csz
    return colors


def make_swatch(colors, tile_size=16, cols=8):
    """Render the given colours as a swatch image. Width = cols * tile_size."""
    cols = min(cols, max(1, len(colors)))
    rows = math.ceil(len(colors) / cols)
    w = cols * tile_size
    h = rows * tile_size
    im = Image.new('RGB', (w, h), (0, 0, 0))
    for i, rgb in enumerate(sorted(colors)):
        r = i // cols
        c = i % cols
        for dy in range(tile_size):
            for dx in range(tile_size):
                im.putpixel((c*tile_size + dx, r*tile_size + dy), rgb)
    return im


def extract_room_obim_colors(disk_path, room_id, orig_clut):
    """Extract every CLUT colour (in [16..47] art range) referenced by any OBIM
    frame in the given room. Decodes each IM0N's SMAP and collects unique pixel
    values, mapped through CLUT+paletteMod=16."""
    from decode_amiga_room import walk_rooms, find_chunk
    from decode_obim import decode_smap
    d = load(disk_path)
    rooms = list(walk_rooms(d))
    ro = next(o for r, o in rooms if r == room_id)
    rsz = be32(d, ro+4)
    p = ro + 8
    end = ro + rsz
    colors = set()
    while p < end:
        nm = chunk_name(d, p)
        if not all(32 <= c < 127 for c in d[p:p+4]):
            break
        sz = be32(d, p+4)
        if nm == 'OBIM':
            ip = p + 8
            ipend = p + sz
            w = h = 0
            while ip < ipend:
                inm = chunk_name(d, ip)
                isz = be32(d, ip+4)
                if inm == 'IMHD':
                    body = d[ip+8:ip+isz]
                    w = struct.unpack('<H', body[12:14])[0]
                    h = struct.unpack('<H', body[14:16])[0]
                elif inm.startswith('IM') and len(inm) == 4 and w > 0 and h > 0:
                    smap_off, smap_sz = find_chunk(d, ip+8, ip+isz, 'SMAP')
                    if smap_off is not None:
                        try:
                            indexed, _ = decode_smap(d, smap_off, w, h)
                            for v in set(indexed):
                                clut_idx = (v + 16) & 0xFF
                                if 16 <= clut_idx <= 47:
                                    colors.add(orig_clut[clut_idx])
                        except Exception:
                            pass
                ip += isz
        p += sz
    return colors


if __name__ == '__main__':
    from remap_costume_palette import find_lflf_for_room
    room_id = int(sys.argv[1]) if len(sys.argv) > 1 else 87
    disk = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    disk_path = f'{REPO_ROOT}/amiga-data/monkey2.{disk:03d}'
    d = load(disk_path)
    # Find this room's CLUT (we need the original colours)
    from decode_amiga_room import walk_rooms, find_chunk

    rooms = list(walk_rooms(d))
    ro = next(o for r, o in rooms if r == room_id)
    rsz = be32(d, ro+4)
    clut_off, clut_sz = find_chunk(d, ro+8, ro+rsz, 'CLUT')
    cb = d[clut_off+8:clut_off+clut_sz]
    orig_clut = [tuple(cb[i*3:i*3+3]) for i in range(256)]

    lflf = find_lflf_for_room(d, room_id)
    colors = extract_lflf_costume_colors(disk_path, lflf, orig_clut)
    print(f"LFLF for room {room_id}: 0x{lflf:x}, {len(colors)} unique costume colours")
    for c in sorted(colors):
        print(f"  #{c[0]:02X}{c[1]:02X}{c[2]:02X}")

    out = f'/tmp/character_swatch_r{room_id}.png'
    im = make_swatch(colors)
    im.save(out)
    print(f"-> {out} ({im.size})")
