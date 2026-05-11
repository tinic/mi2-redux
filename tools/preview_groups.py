#!/usr/bin/env python3
"""Render preview/global_extras/groups_compare.png — a side-by-side
comparison of every group's source atlas vs its quantized output.

Run this AFTER `python3 tools/encode_global_extras.py` (which produces
the per-group atlas + quant PNGs alongside this script's input).

Use to iterate on group splits / slot allocations: edit
tools/cost_groups.json, re-run the encoder, re-run this previewer,
inspect the result, repeat.

Usage:
  python3 tools/preview_groups.py
"""

import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

sys.path.insert(0, f'{REPO_ROOT}/tools/PyTexturePacker')
from PyTexturePacker import Packer  # noqa: E402

CONFIG = os.path.join(os.path.dirname(__file__), 'cost_groups.json')
WORKDIR = f'{REPO_ROOT}/preview/global_extras'
OUTPUT = os.path.join(WORKDIR, 'groups_compare.png')


def pack_source_frames(frames_dir, out_path):
    """Pack every cidNNNN_fNNN.png in `frames_dir` into a single atlas.
    encode_global_extras.py used to write `<name>_atlas.png` directly,
    but since the --ji+--oe rewrite each frame stays as a standalone
    PNG, so we re-pack here for the side-by-side comparison."""
    pngs = sorted(glob.glob(os.path.join(frames_dir, 'cid*_f*.png')))
    if not pngs:
        return False
    packer = Packer.create(max_width=4096, max_height=4096,
                           bg_color=0x00000000,
                           enable_rotated=False, force_square=False,
                           atlas_format='json',
                           inner_padding=0, border_padding=0,
                           shape_padding=0)
    with tempfile.TemporaryDirectory() as tmp:
        base = f'{tmp}/atlas'
        packer.pack(pngs, base)
        os.replace(base + '.png', out_path)
    return True


def main():
    config = json.load(open(CONFIG))
    panels = []
    for group in config['groups']:
        name = group['name']
        atlas_path = f'{WORKDIR}/{name}_atlas.png'
        quant_path = f'{WORKDIR}/{name}_quant.png'
        if not os.path.exists(atlas_path):
            frames_dir = f'{WORKDIR}/{name}_frames'
            if os.path.isdir(frames_dir):
                pack_source_frames(frames_dir, atlas_path)
        if not (os.path.exists(atlas_path) and os.path.exists(quant_path)):
            print(f'  skip {name}: missing {atlas_path} or {quant_path}')
            continue
        src = Image.open(atlas_path)
        out = Image.open(quant_path)
        scale = max(1, out.width // src.width)
        out_native = out.resize(src.size, Image.NEAREST) if scale != 1 else out
        pair = Image.new('RGB', (src.width * 2 + 8, src.height + 28),
                         (32, 32, 32))
        draw = ImageDraw.Draw(pair)
        pair.paste(src, (0, 28))
        pair.paste(out_native, (src.width + 8, 28))
        try:
            font = ImageFont.truetype('/System/Library/Fonts/Monaco.ttf', 16)
        except Exception:
            font = ImageFont.load_default()
        cids = group.get('cids', [])
        pis = group.get('pal_indices', [])
        draw.text((6, 4),
                   f'{name} — source ({len(cids)} cids: {cids})',
                   fill=(255, 255, 255), font=font)
        draw.text((src.width + 14, 4),
                   f'{name} — quant pal_indices={pis}',
                   fill=(255, 255, 255), font=font)
        panels.append(pair)
        print(f'  {name}: {src.size} (cids={len(cids)}, slots={pis})')

    if not panels:
        sys.exit('No groups found — run encode_global_extras.py first.')

    max_w = max(p.width for p in panels)
    total_h = sum(p.height for p in panels) + len(panels) * 8
    combined = Image.new('RGB', (max_w, total_h), (16, 16, 16))
    y = 0
    for p in panels:
        combined.paste(p, (0, y))
        y += p.height + 8
    combined.save(OUTPUT)
    print(f'\nSaved {OUTPUT} ({combined.size})')


if __name__ == '__main__':
    main()
