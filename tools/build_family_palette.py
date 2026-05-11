#!/usr/bin/env python3
"""Build a shared 32-colour palette for a family of related rooms.

A "family" is a set of rooms (asset host + its consumers, or any group
that needs a common palette across them — e.g. for cross-room OBIM
rendering). This tool:

  1. Stitches every room's PC source bg AND its OBIM atlas onto a single
     vertical "family sheet" PNG.
  2. Runs png2amiga --best on that sheet, with the universal lock-slots
     (0=black, 1..3=HW sprite, 17=white).
  3. Saves the resulting palette JSON to
     `tools/shared_palettes/<family_id>.json`.

The shared palette can then be applied via
`inject_room.py --shared-palette <json>` (separate change to inject_room.py)
to encode every member room with the SAME 32-colour CLUT.

Usage:
    python3 tools/build_family_palette.py <family_id> <slug1> <slug2> ...

Example:
    python3 tools/build_family_palette.py rapunzel f-rapinfl costume-s entryway
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (load, be32, find_chunk, walk_rooms,
                                name as cn)
import struct

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
PC_PNG_DIR = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'
OBJ_PNG_DIR = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/objects'
PNG2AMIGA = os.environ['PNG2AMIGA']    # set by build.sh / bootstrap.sh
SHARED_DIR = f'{REPO_ROOT}/tools/shared_palettes'
WORKDIR = f'{REPO_ROOT}/preview/family_workdir'

def slug_to_room_id(slug):
    """Find a room's id by walking RNAM in pristine monkey2.000."""
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    p = 0
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'RNAM':
            body = idx[p + 8 : p + sz]
            i = 0
            while i < len(body):
                rid = body[i]
                if rid == 0:
                    break
                name = bytes((b ^ 0xFF) for b in body[i + 1 : i + 10]).split(b'\x00')[0].decode('latin1', errors='replace')
                if name == slug:
                    return rid
                i += 10
            break
        p += sz
    raise ValueError(f"unknown slug {slug!r}")


def find_pc_bg(slug):
    """Locate the PC bg PNG for a room slug."""
    import glob
    pat = f'{PC_PNG_DIR}/[0-9][0-9][0-9][0-9]_{slug}.png'
    matches = glob.glob(pat)
    if not matches:
        return None
    return matches[0]


def find_obim_pngs(rid):
    """List OBIM PNGs for a room id (extracted from PC objects dir)."""
    import glob
    pat = f'{OBJ_PNG_DIR}/{rid:04d}_*.png'
    return sorted(glob.glob(pat))


def build_family_sheet(slugs, out_png):
    """Stack every member room's bg + OBIMs vertically into one big RGBA
    PNG. Bg has no transparency (alpha=255); OBIMs carry tRNS from
    extract_pc_pngs.py and convert to RGBA with alpha=0 for transparent
    pixels. png2amiga reads alpha=0 → slot 0 routing automatically."""
    # PyTexturePacker for OBIM atlas-per-room
    sys.path.insert(0, f'{os.path.dirname(__file__)}/PyTexturePacker')
    from PyTexturePacker import Packer

    panels = []
    for slug in slugs:
        rid = slug_to_room_id(slug)
        bg_path = find_pc_bg(slug)
        if bg_path:
            bg = Image.open(bg_path).convert('RGBA')   # opaque (alpha=255)
            panels.append((slug + '/bg', bg))
        obim_pngs = find_obim_pngs(rid)
        if obim_pngs:
            atlas_base = f'{WORKDIR}/{slug}_atlas'
            os.makedirs(WORKDIR, exist_ok=True)
            for f in [atlas_base + '.png', atlas_base + '.json']:
                if os.path.exists(f):
                    os.remove(f)
            packer = Packer.create(max_width=2048, max_height=2048,
                                    bg_color=0x00000000, enable_rotated=False,
                                    force_square=False, atlas_format='json',
                                    inner_padding=0, border_padding=0,
                                    shape_padding=0)
            packer.pack(obim_pngs, atlas_base)
            atlas_im = Image.open(atlas_base + '.png').convert('RGBA')
            panels.append((slug + '/atlas', atlas_im))

    if not panels:
        raise RuntimeError("no panels to stitch — empty family?")

    # Stack vertically into an RGBA sheet; transparent pixels stay
    # transparent through the paste (PIL's paste handles RGBA→RGBA
    # alpha-aware compositing if you pass the source as the mask).
    cw = max(p.width for _, p in panels)
    ch = sum(p.height for _, p in panels)
    sheet = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
    y = 0
    for label, p in panels:
        sheet.paste(p, ((cw - p.width) // 2, y), p)
        y += p.height
    sheet.save(out_png)
    return cw, ch, len(panels)


def build_palette(sheet_png, family_id, out_json):
    """Run png2amiga --best on the family sheet; capture palette JSON."""
    # Honour BEST env var (1=on, 0=skip --best for fast iteration).
    best_flag = ['--best'] if os.environ.get('BEST', '1') != '0' else []
    dither_args = ['--dither', os.environ['DITHER']] if os.environ.get('DITHER') else ['--dither', 'opt-checker']

    # Universal locks (consistent across all rooms in mi2-redux).
    lock_args = []
    # We could also lock 1, 2, 3, 17 but those are ROOM-specific in pristine
    # CLUT[17..19] / [33] — so they may differ per room. Only slot 0 is
    # truly universal black. Keep palette flexible.
    lock_args.extend(['--lock-index', '0', '000000'])
    lock_args.extend(['--reserve-range', '17', 'FFFFFF'])

    out_idx = sheet_png.replace('.png', '.idx')
    out_preview = sheet_png.replace('.png', '.preview.png')
    r = subprocess.run(
        [PNG2AMIGA, '--mode', 'lores', '--depth', '5', *best_flag,
         '--dither-strength', '0.8',
         '--no-scale', '--quiet', '--print-palette-json', *dither_args,
         '--output-indexed', out_idx,
         *lock_args,
         sheet_png, '-o', out_preview],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print("png2amiga stderr:", r.stderr, file=sys.stderr)
        raise RuntimeError("png2amiga failed")
    palette_dump = json.loads(r.stdout.strip())
    out = {
        'family_id': family_id,
        'palette': palette_dump['palette'],
        'depth': palette_dump.get('depth'),
        'chipset': palette_dump.get('chipset'),
    }
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w') as f:
        json.dump(out, f, indent=2)
    return palette_dump


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    family_id = sys.argv[1]
    slugs = sys.argv[2:]
    print(f"Family {family_id!r}: {len(slugs)} member(s) — {slugs}")
    sheet_png = f'{WORKDIR}/{family_id}_sheet.png'
    cw, ch, n = build_family_sheet(slugs, sheet_png)
    print(f"  sheet: {cw}x{ch} px, {n} panel(s) — {sheet_png}")
    out_json = f'{SHARED_DIR}/{family_id}.json'
    pal = build_palette(sheet_png, family_id, out_json)
    print(f"  palette: 32 entries → {out_json}")
    locks = sum(1 for e in pal['palette'] if e['locked'])
    print(f"  locked slots: {locks}")


if __name__ == '__main__':
    main()
