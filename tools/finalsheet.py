#!/usr/bin/env python3
"""Render a per-room "final sheet" PNG combining everything that's actually
patched: bg + every OBIM frame + every LFLF-hosted costume's frames,
each rendered with the room's PATCHED CLUT (= what ScummVM will draw).

Two uses:
  1. Visual sanity check — open the sheet, verify colours look right.
  2. Counts how many unique CLUT slots/colours are actually used.

Usage:
  finalsheet.py <room_name>          # one room
  finalsheet.py --all                # every patched room

Output:
  preview/finalsheets/<room_name>.png
  preview/finalsheets/_index.txt    # colour-count summary
"""

import os
import sys
import struct
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (
    load, be32, walk_rooms, find_chunk, name as cn, le16,
)
from decode_obim import decode_smap
from decode_cost import decode_costume
from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf
from decode_all import parse_index_room_names

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
SVM_DIR = f'{REPO_ROOT}/monkey2-hd'
INTERMEDIATES = f'{REPO_ROOT}/preview/intermediates'


def render_indexed(idx_buf, w, h, clut, palette_mod=16):
    """RGB image from indexed SMAP/OBIM bytes (paletteMod=16 → 0..31 → CLUT[16..47])."""
    im = Image.new('RGB', (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = idx_buf[y * w + x]
            slot = (v + palette_mod) & 0xFF
            px[x, y] = (clut[slot * 3], clut[slot * 3 + 1], clut[slot * 3 + 2])
    return im


def render_costume_frame(frame_pixels, w, h, pal_table, clut,
                         transparent_rgb=(0xFF, 0x00, 0xFF)):
    """Render a costume frame using its pal_table → CLUT lookup (no
    paletteMod since pal_table values are already absolute CLUT slots)."""
    im = Image.new('RGB', (w, h), transparent_rgb)
    px = im.load()
    for y in range(h):
        for x in range(w):
            idx = frame_pixels[y * w + x]
            if idx == 0 or idx >= len(pal_table):
                continue
            slot = pal_table[idx]
            px[x, y] = (clut[slot * 3], clut[slot * 3 + 1], clut[slot * 3 + 2])
    return im


def collect_obim_frames(d, ro, room_size):
    """Decode every OBIM frame in this room. Returns list of PIL Images
    (transparent areas already masked via the OBIM's transparent codecs)."""
    out = []
    p = ro + 8
    end = ro + room_size
    while p < end:
        if p + 8 > end: break
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag == 'OBIM':
            ip = p + 8; ipend = p + sz
            w_obj = h_obj = 0
            while ip < ipend:
                inm = cn(d, ip); isz = be32(d, ip + 4)
                if inm == 'IMHD' and isz >= 26:
                    w_obj = le16(d, ip + 8 + 14)
                    h_obj = le16(d, ip + 8 + 16)
                elif inm.startswith('IM') and len(inm) == 4 and w_obj > 0:
                    smap_o, _ = find_chunk(d, ip + 8, ip + isz, 'SMAP')
                    if smap_o is not None:
                        try:
                            indexed, _ = decode_smap(d, smap_o, w_obj, h_obj)
                            yield (w_obj, h_obj, indexed)
                        except Exception:
                            pass
                if isz < 8: break
                ip += isz
        if sz < 8 or p + sz > end: break
        p += sz


def render_room(rid, name):
    """Stitch everything for one room into a single sheet."""
    # Find room in PATCHED data
    for disk in range(1, 12):
        path = f'{SVM_DIR}/monkey2.{disk:03d}'
        if not os.path.exists(path): continue
        d = load(path)
        try:
            ro = next(o for r, o in walk_rooms(d) if r == rid)
            break
        except StopIteration:
            continue
    else:
        return None
    rsz = be32(d, ro + 4)

    clut_off, _ = find_chunk(d, ro + 8, ro + rsz, 'CLUT')
    clut = bytes(d[clut_off + 8:clut_off + 8 + 768])
    rmhd_off, _ = find_chunk(d, ro + 8, ro + rsz, 'RMHD')
    bg_w = le16(d, rmhd_off + 8)
    bg_h = le16(d, rmhd_off + 10)

    # Render bg
    rmim_off, rmim_sz = find_chunk(d, ro + 8, ro + rsz, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
    smap_off, _ = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
    bg_idx, _ = decode_smap(d, smap_off, bg_w, bg_h)
    bg_im = render_indexed(bg_idx, bg_w, bg_h, clut)

    panels = [('bg', bg_im)]

    # OBIM frames
    obim_frames = list(collect_obim_frames(d, ro, rsz))
    if obim_frames:
        max_w = max(f[0] for f in obim_frames)
        max_h = max(f[1] for f in obim_frames)
        cols = 8
        rows = (len(obim_frames) + cols - 1) // cols
        sheet = Image.new('RGB', (cols * max_w, rows * max_h), (0xFF, 0x00, 0xFF))
        for i, (w, h, idx) in enumerate(obim_frames):
            sub = render_indexed(idx, w, h, clut)
            cx = (i % cols) * max_w
            cy = (i // cols) * max_h
            sheet.paste(sub, (cx, cy))
        panels.append((f'obim ({len(obim_frames)} frames)', sheet))

    # Costumes hosted in this LFLF (rendered through their PATCHED pal_table)
    lflf = find_lflf_for_room(d, rid)
    if lflf is not None:
        cost_offs = find_costumes_in_lflf(d, lflf)
        for ci, co in enumerate(cost_offs):
            sz = be32(d, co + 4)
            body = bytes(d[co + 8:co + sz])
            fmt = body[1] & 0x7F
            if fmt not in (0x58, 0x59):
                continue
            npal = 16 if fmt == 0x58 else 32
            pal_table = list(body[2:2 + npal])
            try:
                frames, _ = decode_costume(body)
            except Exception:
                continue
            if not frames:
                continue
            max_w = max(f[0] for f in frames)
            max_h = max(f[1] for f in frames)
            cols = 8
            rows = (len(frames) + cols - 1) // cols
            sheet = Image.new('RGB', (cols * max_w, rows * max_h), (0xFF, 0x00, 0xFF))
            for i, (w, h, pix) in enumerate(frames):
                sub = render_costume_frame(pix, w, h, pal_table, clut)
                cx = (i % cols) * max_w
                cy = (i // cols) * max_h
                sheet.paste(sub, (cx, cy))
            panels.append((f'cost {ci} ({len(frames)} frames)', sheet))

    # Stitch panels vertically
    sheet_w = max(p.width for _, p in panels)
    sheet_h = sum(p.height for _, p in panels)
    final = Image.new('RGB', (sheet_w, sheet_h), (0xFF, 0x00, 0xFF))
    y = 0
    for label, p in panels:
        final.paste(p, ((sheet_w - p.width) // 2, y))
        y += p.height

    # Count unique non-magenta colours
    colors = Counter()
    for px in final.getdata():
        if px == (0xFF, 0x00, 0xFF):
            continue
        colors[px] += 1

    return final, len(colors), [label for label, _ in panels]


def main():
    args = sys.argv[1:]
    names = parse_index_room_names(f'{AMIGA_DIR}/monkey2.000')
    targets = []
    if args == ['--all']:
        targets = sorted(names.items())
    elif len(args) == 1:
        slug = args[0]
        rid = next((k for k, v in names.items() if v == slug), None)
        if rid is None:
            sys.exit(f"unknown room '{slug}'")
        targets = [(rid, slug)]
    else:
        sys.exit("usage: finalsheet.py <room_name>  |  finalsheet.py --all")

    summary_lines = []
    for rid, slug in targets:
        result = render_room(rid, slug)
        if result is None:
            print(f'  skip {slug} (rid {rid}) — not in patched data')
            continue
        sheet, color_count, panels = result
        # Write alongside the other per-room intermediates (best.png,
        # cost_atlas.png, joint.png, …) instead of a separate dir.
        room_dir = f'{INTERMEDIATES}/{slug}'
        os.makedirs(room_dir, exist_ok=True)
        out_path = f'{room_dir}/finalsheet.png'
        sheet.save(out_path)
        line = f'rid {rid:3d} {slug:>14}: {color_count:>3} unique colours, {len(panels)} panel(s)'
        summary_lines.append(line)
        print(line)

    # Per-room sheets live next to the rest of intermediates; an aggregate
    # index lives at the parent so all summaries stay together.
    with open(f'{INTERMEDIATES}/finalsheet_index.txt', 'w') as f:
        f.write('\n'.join(summary_lines) + '\n')
    print(f'\n-> {INTERMEDIATES}/<room_name>/finalsheet.png')


if __name__ == '__main__':
    main()
