#!/usr/bin/env python3
"""Pack PC OBIM frames into a horizontal atlas image, run --best jointly with
the bg, then split each frame's region back out for SMAP re-encoding.

Per-frame metadata is recorded so the caller can recover (oid, frame_id, x, y, w, h)
to look up the corresponding rectangle in the quantized atlas output.
"""
import os, sys, glob, math
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




PC_OBJ_DIR = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/objects'


def gather_pc_obim_pngs(room_id):
    """Return list of (obj_id, frame_label, host_path)."""
    out = []
    pattern = f'{PC_OBJ_DIR}/{room_id:04d}_*_IM*.png'
    for f in sorted(glob.glob(pattern)):
        bn = os.path.basename(f).rsplit('.', 1)[0]
        # 0087_0809_IM01
        parts = bn.split('_')
        if len(parts) != 3: continue
        oid = int(parts[1])
        frame = parts[2]
        out.append((oid, frame, f))
    return out


def pack_atlas(frames, max_width=320):
    """Lay out frame PNGs into a horizontal atlas, wrapping to new rows when
    the next frame would exceed max_width. Returns (atlas_image, layout) where
    layout = list of (oid, frame, x, y, w, h) tuples."""
    images = [(oid, frame, Image.open(host).convert('RGB')) for oid, frame, host in frames]
    # Pack rows greedily
    rows = []     # each row: list of (oid, frame, im, x_in_row)
    cur_row = []
    cur_x = 0
    row_h = 0
    for oid, frame, im in images:
        w, h = im.size
        if cur_x + w > max_width and cur_row:
            rows.append((cur_row, row_h, cur_x))
            cur_row = []
            cur_x = 0
            row_h = 0
        cur_row.append((oid, frame, im, cur_x))
        cur_x += w
        row_h = max(row_h, h)
    if cur_row:
        rows.append((cur_row, row_h, cur_x))

    total_h = sum(rh for _, rh, _ in rows)
    atlas = Image.new('RGB', (max_width, total_h), (0, 0, 0))
    layout = []
    y = 0
    for row, rh, _ in rows:
        for oid, frame, im, x in row:
            atlas.paste(im, (x, y))
            layout.append((oid, frame, x, y, im.width, im.height))
        y += rh
    return atlas, layout


if __name__ == '__main__':
    room_id = int(sys.argv[1]) if len(sys.argv) > 1 else 87
    frames = gather_pc_obim_pngs(room_id)
    print(f"Found {len(frames)} OBIM PNGs for room {room_id}")
    atlas, layout = pack_atlas(frames)
    print(f"Atlas: {atlas.size}")
    for oid, frame, x, y, w, h in layout[:5]:
        print(f"  obj {oid} {frame} @({x},{y}) {w}x{h}")
    out = f'/tmp/atlas_r{room_id}.png'
    atlas.save(out)
    print(f"-> {out}")
