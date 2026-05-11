#!/usr/bin/env python3
"""Dump every pristine costume into a packed RGBA PNG, sourcing from
BOTH the PC original (MONKEY2.001) and the Amiga port (amiga-data/).

Cids are re-numbered between ports — e.g. the green-pants Largo
walking cycle lives at cid 16 on PC but at cid 28 on Amiga — so we
write each source to its own folder:

  extracted-pc-pngs/IMAGES/costumes-pc/cidNNNN.png    (PC original)
  extracted-pc-pngs/IMAGES/costumes-amiga/cidNNNN.png (Amiga port)

Compare a single cid number side-by-side and you'll see which port
holds which sprite, instead of assuming PC cid N == Amiga cid N.
"""

import glob
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'PyTexturePacker'))

from PIL import Image
from PyTexturePacker import Packer  # noqa: E402
from decode_amiga_room import name as cn, be32  # noqa: E402
from decode_cost import decode_costume  # noqa: E402
from decode_pc_room import _find_pc_palette  # noqa: E402
from encode_global_extras import load_pc_data, get_pc_cost_body  # noqa: E402
from pristine_cache import cache  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PC_DATA_PATH = os.path.join(REPO_ROOT, 'pc-data', 'MONKEY2.001')
OUT_PC_DIR = os.path.join(REPO_ROOT, 'extracted-pc-pngs', 'IMAGES',
                           'costumes-pc')
OUT_AMIGA_DIR = os.path.join(REPO_ROOT, 'extracted-pc-pngs', 'IMAGES',
                              'costumes-amiga')


def _pc_room_palette(d, room_off):
    """Look up the PC room's 768-byte palette via _find_pc_palette."""
    if cn(d, room_off) != 'ROOM':
        return None
    room_sz = be32(d, room_off + 4)
    return _find_pc_palette(d, room_off, room_sz)


def render_frame_rgba(w, h, pix, pal_t, clut):
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    px = im.load()
    for yy in range(h):
        for xx in range(w):
            v = pix[yy * w + xx]
            if v == 0 or v >= len(pal_t):
                continue
            slot = pal_t[v]
            if slot == 0 or slot == 250:
                continue
            px[xx, yy] = (clut[slot * 3],
                          clut[slot * 3 + 1],
                          clut[slot * 3 + 2],
                          255)
    return im


def _pack_frames(frames, pt, clut, out_path, cid):
    """Render every (w, h, pix) frame through (pt → clut) and pack them
    into one RGBA PNG at out_path. Returns (out_path, sz_tuple)."""
    with tempfile.TemporaryDirectory() as tmp:
        frame_paths = []
        for fi, (w, h, pix) in enumerate(frames):
            im = render_frame_rgba(w, h, pix, pt, clut)
            p = f'{tmp}/cid{cid:04d}_f{fi:03d}.png'
            im.save(p)
            frame_paths.append(p)
        packer = Packer.create(
            max_width=4096, max_height=4096,
            bg_color=0x00000000,
            enable_rotated=False, force_square=False,
            atlas_format='json',
            inner_padding=0, border_padding=0, shape_padding=2)
        base = f'{tmp}/cid{cid:04d}'
        packer.pack(frame_paths, base)
        shutil.move(base + '.png', out_path)
        return out_path, Image.open(out_path).size


def pack_pc_cid(d, pc_dcos, pc_room_off, cid):
    body = get_pc_cost_body(d, pc_dcos, pc_room_off, cid)
    if body is None:
        return None, 'no PC COST body'
    frames, pt = decode_costume(body, column_major=True)
    if not frames:
        return None, 'no frames'
    home_rid, _ = pc_dcos[cid]
    clut = _pc_room_palette(d, pc_room_off[home_rid])
    if clut is None:
        return None, f'no palette in rid {home_rid}'
    out, sz = _pack_frames(frames, pt, clut,
                           os.path.join(OUT_PC_DIR, f'cid{cid:04d}.png'), cid)
    return out, f'{len(frames)} frames -> {sz[0]}x{sz[1]}'


def pack_amiga_cid(cid):
    home_rid = cache.cost_home(cid)
    if home_rid is None:
        return None, 'no home_rid'
    room = cache.room(home_rid)
    cs = next((c for c in room['costumes'] if c['cost_id'] == cid), None)
    if cs is None or not cs['frames']:
        return None, 'no frames'
    pt = cs['pal_table']
    clut = room['clut']
    frames = [(f['w'], f['h'], f['pixels']) for f in cs['frames']]
    out, sz = _pack_frames(frames, pt, clut,
                           os.path.join(OUT_AMIGA_DIR, f'cid{cid:04d}.png'),
                           cid)
    return out, f'{len(frames)} frames -> {sz[0]}x{sz[1]}'


def _clean(dir_):
    if os.path.exists(dir_):
        for f in glob.glob(os.path.join(dir_, 'cid*.png')):
            os.unlink(f)
    os.makedirs(dir_, exist_ok=True)


def main():
    if not os.path.exists(PC_DATA_PATH):
        sys.exit(f'ERROR: {PC_DATA_PATH} not found (see pc-data/README.md)')

    _clean(OUT_PC_DIR)
    _clean(OUT_AMIGA_DIR)

    # PC
    d, pc_dcos, pc_room_off = load_pc_data()
    pc_cids = sorted(pc_dcos.keys())
    print(f'\n== PC: {len(pc_cids)} costumes ==')
    pc_ok = pc_skip = 0
    for cid in pc_cids:
        path, msg = pack_pc_cid(d, pc_dcos, pc_room_off, cid)
        print(f'  cid {cid:3d}: {msg}')
        if path:
            pc_ok += 1
        else:
            pc_skip += 1
    print(f'  {pc_ok}/{len(pc_cids)} packed, {pc_skip} skipped → {OUT_PC_DIR}')

    # Amiga (via pristine_cache)
    import json
    refs = json.load(open(os.path.join(REPO_ROOT, 'tools', 'costume_refs.json')))
    amiga_cids = sorted(int(c) for c in refs['cost_home'].keys())
    print(f'\n== Amiga: {len(amiga_cids)} costumes ==')
    a_ok = a_skip = 0
    for cid in amiga_cids:
        path, msg = pack_amiga_cid(cid)
        print(f'  cid {cid:3d}: {msg}')
        if path:
            a_ok += 1
        else:
            a_skip += 1
    print(f'  {a_ok}/{len(amiga_cids)} packed, {a_skip} skipped → {OUT_AMIGA_DIR}')


if __name__ == '__main__':
    main()
