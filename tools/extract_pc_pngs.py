#!/usr/bin/env python3
"""Extract bg + OBIM PNGs from PC `MONKEY2.001` into
`extracted-pc-pngs/IMAGES/{backgrounds,objects}/`.

Output filename convention (matches the existing MISE Explorer
extraction so all downstream tools keep working unmodified):

    backgrounds/<rid:04d>_<slug>.png
    objects/<rid:04d>_<obj_id:04d>_IM<NN>.png

`<slug>` is the room name from monkey2.000's RNAM table (lower-cased,
truncated). Transparent pixels in objects are emitted via PNG-8 tRNS:
the room's TRNS sentinel index has alpha=0 in the output palette.

Usage:
    python3 tools/extract_pc_pngs.py
    python3 tools/extract_pc_pngs.py --force      # re-extract everything

`build.sh` runs this in Stage 1 if `extracted-pc-pngs/IMAGES/` is empty.
"""

import argparse
import os
import sys
import struct

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import be32, name as cn
from pc_data import parse_pc_loff
from decode_pc_room import walk_pc_room
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PC_DATA = f'{REPO_ROOT}/pc-data/MONKEY2.001'
PC_INDEX = f'{REPO_ROOT}/pc-data/MONKEY2.000'
OUT_BG_DIR = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'
OUT_OBJ_DIR = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/objects'


def parse_pc_room_names():
    """Read RNAM from monkey2.000 (XOR-decrypted) and return rid → slug."""
    if not os.path.exists(PC_INDEX):
        return {}
    d = bytes(b ^ 0x69 for b in open(PC_INDEX, 'rb').read())
    p = 0
    while p + 8 <= len(d):
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag == 'RNAM':
            body = d[p + 8:p + sz]
            out = {}
            i = 0
            while i + 1 < len(body):
                rid = body[i]
                if rid == 0:
                    break
                # 9 bytes of name, XOR'd with 0xFF in the index
                name_b = bytes((b ^ 0xFF) for b in body[i+1:i+10])
                slug = name_b.split(b'\x00')[0].decode('latin1', errors='replace')
                out[rid] = slug
                i += 10
            return out
        if sz < 8 or p + sz > len(d):
            break
        p += sz
    return {}


# Per-OBIM allow-list: keep specific magenta-family palette indices
# OPAQUE for the named OBIM, even though the global blunt rule would
# normally strip them. Keyed by (rid, obj_id, im_label).
#
# When png2amiga internally premultiplies alpha (v1.82+), alpha=0
# pixels no longer contribute to palette training, so adding an
# override here doesn't drag pristine magenta into the training
# corpus — only the actually-opaque kept pixels do. That makes
# overrides cheap: each entry contributes ~10s-100s of opaque
# pixels at most, well below the threshold where the optimiser
# allocates a separate slot for them.
MAGENTA_KEEP_PER_OBIM: dict = {
    # Big-Whoop man (room 55, scabb-isl prison cabin) — purple shirt
    # and magenta pants. palette[217]=bc10c0 = pants;
    # palette[219]=600060 = dark shirt. Both share RGB with bg-filler
    # indices used by other OBIMs in this room, but the alpha-premultiply
    # fix means those bg-filler regions don't pollute training.
    (55, 632, 'IM01'): {217, 219},
    # Blue-robed character (room 55) with magenta accents on shoes/hat.
    # 59 pixels at palette[217]=bc10c0.
    (55, 616, 'IM01'): {217},
}


def save_paletted_png(out_path, w, h, pixel_bytes, palette_768, trns_value,
                       obim_key=None):
    """Save a paletted PNG. If trns_value is None (bg images), saves as
    P-mode opaque. Otherwise saves as RGBA with alpha=0 for the room's
    TRNS sentinel and any magenta-family palette index — except indices
    listed in MAGENTA_KEEP_PER_OBIM[obim_key], which stay opaque.

    The blunt magenta-RGB strip is intentional: MI2's PC SCUMM v5 OBIMs
    use magenta-RGB palette entries as background filler in unused
    regions of the bitmap. The reference engine renders them opaquely
    but in normal gameplay they're covered by actors/other objects.
    Stripping them avoids inflating the joint palette training with
    colours nobody sees.

    The per-OBIM allow-list handles the rare case where the artist
    actually used magenta as foreground colour (e.g. characters with
    magenta clothing)."""
    if trns_value is None:
        im = Image.new('P', (w, h))
        im.putpalette(palette_768)
        im.frombytes(bytes(pixel_bytes))
        im.save(out_path, 'PNG', optimize=False)
        return

    transparent_indices = {trns_value} if 0 <= trns_value < 256 else set()
    keep = MAGENTA_KEEP_PER_OBIM.get(obim_key, set()) if obim_key else set()
    for i in range(256):
        if i in keep:
            continue
        r, g, b = palette_768[i*3], palette_768[i*3+1], palette_768[i*3+2]
        if r >= 0x80 and b >= 0x80 and g <= 0x60 and abs(r - b) <= 0x30:
            transparent_indices.add(i)

    rgba = bytearray(w * h * 4)
    for i, idx in enumerate(pixel_bytes):
        rgba[i*4]     = palette_768[idx*3]
        rgba[i*4 + 1] = palette_768[idx*3 + 1]
        rgba[i*4 + 2] = palette_768[idx*3 + 2]
        rgba[i*4 + 3] = 0 if idx in transparent_indices else 255
    im = Image.frombytes('RGBA', (w, h), bytes(rgba))
    im.save(out_path, 'PNG', optimize=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--force', action='store_true',
                    help='re-extract everything (default: skip if output dirs are non-empty)')
    args = ap.parse_args()

    if not os.path.exists(PC_DATA):
        sys.exit(f'ERROR: {PC_DATA} not found. See pc-data/README.md.')

    os.makedirs(OUT_BG_DIR, exist_ok=True)
    os.makedirs(OUT_OBJ_DIR, exist_ok=True)

    if not args.force:
        if os.listdir(OUT_BG_DIR) or os.listdir(OUT_OBJ_DIR):
            print(f'extracted-pc-pngs/ already populated; pass --force to re-extract.')
            return

    print(f'==> Extracting PC PNGs from {PC_DATA}')
    pc = bytes(b ^ 0x69 for b in open(PC_DATA, 'rb').read())
    loff = parse_pc_loff(pc)
    names = parse_pc_room_names()
    print(f'    {len(loff)} rooms in LOFF; {len(names)} named in RNAM')

    n_bg = n_obj = n_skip = 0
    for rid in sorted(loff):
        room_off = loff[rid]
        if room_off + 8 > len(pc) or cn(pc, room_off) != 'ROOM':
            continue
        room_sz = be32(pc, room_off + 4)
        slug = names.get(rid, f'room{rid:03d}')
        for kind, label, w, h, pix, pal, trns in \
                walk_pc_room(pc, room_off, room_sz):
            try:
                if kind == 'bg':
                    out_path = f'{OUT_BG_DIR}/{rid:04d}_{slug}.png'
                    # bg never uses transparency in the output (room bg
                    # fills the whole playfield).
                    save_paletted_png(out_path, w, h, pix, pal, None)
                    n_bg += 1
                else:
                    out_path = f'{OUT_OBJ_DIR}/{rid:04d}_{label}.png'
                    # label is "<obj_id:04d>_IM<NN>"; build the override key.
                    obj_str, im_label = label.split('_', 1)
                    obim_key = (rid, int(obj_str), im_label)
                    save_paletted_png(out_path, w, h, pix, pal, trns, obim_key)
                    n_obj += 1
            except Exception as e:
                print(f'  [skip] rid={rid} {label}: {e}', file=sys.stderr)
                n_skip += 1
    print(f'==> Wrote {n_bg} bg + {n_obj} object PNGs ({n_skip} skipped)')


if __name__ == '__main__':
    main()
