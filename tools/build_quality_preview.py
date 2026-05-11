#!/usr/bin/env python3
"""Post-process: pack the final-quality assets for one room (or all) into
a single PNG so the encoded bg + every OBIM frame + every cost frame can
be eyeballed together for dither / palette regressions.

Inputs come from `preview/intermediates/<room>/`:
    bg.idx                            final palette indices (W*H bytes)
    obim_atlas.idx + obim_atlas.json  packed object frames + layout
    cost_atlas.idx + cost_atlas.json  packed costume frames + layout
    palette.json                      final 32-color palette

The .idx files contain palette indices straight from png2amiga's `--best`
run with `--ji`/`--oe` (so they reflect dither + quantisation). Slot 0
is the alpha=0 sentinel; everything else maps through palette.json.
The `.png` siblings of the atlases are the *source* PC art, not the
final encoded result — don't read them here.

Output:
    preview/quality/<rid>-<room>.png

Usage:
    build_quality_preview.py <room>
    build_quality_preview.py --all
"""

import json
import os
import sys
import glob
import shutil
import struct
import tempfile

from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

INTERMEDIATES_DIR = f'{REPO_ROOT}/preview/intermediates'
OUTPUT_DIR        = f'{REPO_ROOT}/preview/quality'
PC_BG_DIR         = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'

sys.path.insert(0, f'{REPO_ROOT}/tools/PyTexturePacker')
from PyTexturePacker import Packer  # noqa: E402
sys.path.insert(0, f'{REPO_ROOT}/tools')

# Pristine-render imports — used to build the "original Amiga" side of
# the side-by-side comparison. Lazy to avoid pulling pristine_cache for
# the --globals path that doesn't need it.
_pristine_cache = None
def _get_pristine_cache():
    global _pristine_cache
    if _pristine_cache is None:
        from pristine_cache import cache as pc  # noqa: E402
        _pristine_cache = pc
    return _pristine_cache


def lookup_room_id(room_name):
    matches = glob.glob(f'{PC_BG_DIR}/*_{room_name}.png')
    if not matches:
        return None
    return os.path.basename(matches[0]).split('_', 1)[0]


def load_palette(palette_json_path):
    """Return list of (r,g,b) tuples for slots 0..31."""
    with open(palette_json_path) as f:
        d = json.load(f)
    out = [(0, 0, 0)] * 32
    for entry in d['palette']:
        idx = entry['idx']
        rgb = entry['rgb']
        out[idx] = (int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16))
    return out


def render_idx_to_rgba(idx_bytes, w, h, palette, alpha_mask=None):
    """Map (W*H) palette-index bytes through `palette` -> RGBA PIL Image.

    Transparency comes from `alpha_mask` (W*H bytes, 0=transparent,
    255=opaque) when supplied, NOT from `idx == 0`. That distinction
    matters: png2amiga's alpha=0 routing AND the nearest-colour
    quantisation of opaque-black source pixels both land on slot 0,
    so reading `idx==0 -> transparent` would silently turn real-black
    art into see-through holes (e.g. shadows in mansion bg, character
    silhouettes). The final encoded SMAP doesn't have this ambiguity
    because obim_reencode.py uses the original Amiga transparency mask
    rather than the .idx for the trns-value remap.

    When `alpha_mask` is None, every pixel is opaque (used for bg.idx
    where the source has no alpha)."""
    rgba = bytearray(w * h * 4)
    for i, p in enumerate(idx_bytes):
        r, g, b = palette[p]
        rgba[i*4]     = r
        rgba[i*4 + 1] = g
        rgba[i*4 + 2] = b
        if alpha_mask is not None and alpha_mask[i] == 0:
            rgba[i*4 + 3] = 0
        else:
            rgba[i*4 + 3] = 255
    return Image.frombytes('RGBA', (w, h), bytes(rgba))


def crop_atlas_frames(idx_path, layout_json_path, source_png_path,
                      palette, out_dir):
    """Open an atlas .idx + JSON layout, render the whole atlas through
    the final palette, crop every frame to its own PNG, return the list
    of file paths.

    `source_png_path` is the RGBA atlas PNG png2amiga consumed; we use
    its alpha channel as the transparency mask so real-black source
    pixels (which quantise to slot 0 just like alpha=0 routing) stay
    opaque in the preview."""
    if not os.path.exists(idx_path) or not os.path.exists(layout_json_path):
        return []
    with open(layout_json_path) as f:
        layout = json.load(f)
    aw = layout['meta']['size']['w']
    ah = layout['meta']['size']['h']
    idx_bytes = open(idx_path, 'rb').read()
    if len(idx_bytes) != aw * ah:
        print(f'[warn] {idx_path}: {len(idx_bytes)} bytes != {aw}x{ah}',
              file=sys.stderr)
        return []
    alpha_mask = None
    if source_png_path and os.path.exists(source_png_path):
        src = Image.open(source_png_path).convert('RGBA')
        if src.size == (aw, ah):
            alpha_mask = bytes(src.split()[3].tobytes())
    atlas = render_idx_to_rgba(idx_bytes, aw, ah, palette, alpha_mask)
    paths = []
    for fname, info in layout['frames'].items():
        f = info['frame']
        crop = atlas.crop((f['x'], f['y'], f['x'] + f['w'], f['y'] + f['h']))
        out_path = os.path.join(out_dir, fname)
        crop.save(out_path)
        paths.append(out_path)
    return paths


def render_bg_frame(bg_idx_path, source_png_path, palette, out_dir):
    """Render bg.idx through the final palette. Dimensions come from the
    source bg.png (png2amiga --no-scale preserves them)."""
    if not os.path.exists(bg_idx_path):
        return None
    if not os.path.exists(source_png_path):
        return None
    src = Image.open(source_png_path)
    w, h = src.size
    idx_bytes = open(bg_idx_path, 'rb').read()
    if len(idx_bytes) != w * h:
        print(f'[warn] {bg_idx_path}: {len(idx_bytes)} != {w}x{h}',
              file=sys.stderr)
        return None
    img = render_idx_to_rgba(idx_bytes, w, h, palette)
    out_path = os.path.join(out_dir, 'bg.png')
    img.save(out_path)
    return out_path


def _render_pristine_amiga_bg(disk_path, room_id):
    """Decode pristine Amiga BG (SMAP) for the given room → RGB PIL Image.
    Returns None on failure."""
    from decode_amiga_room import (
        load, be32, le16, find_chunk, walk_rooms,
        decode_zigzag_h, decode_zigzag_v, decode_majmin_h,
    )
    d = load(disk_path)
    ro = next((o for r, o in walk_rooms(d) if r == room_id), None)
    if ro is None:
        return None
    rsz = be32(d, ro + 4)
    rmhd_off, _ = find_chunk(d, ro + 8, ro + rsz, 'RMHD')
    if rmhd_off is None:
        return None
    w = le16(d, rmhd_off + 8); h = le16(d, rmhd_off + 10)
    clut_off, clut_sz = find_chunk(d, ro + 8, ro + rsz, 'CLUT')
    if clut_off is None:
        return None
    pal_body = d[clut_off + 8: clut_off + clut_sz]
    pal = [tuple(pal_body[i*3:i*3+3]) for i in range(256)]
    rmim_off, rmim_sz = find_chunk(d, ro + 8, ro + rsz, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
    if smap_off is None:
        return None
    num_strips = w // 8
    body_off = smap_off + 8
    from decode_amiga_room import le32 as _le32
    strip_offsets = [_le32(d, body_off + i * 4) for i in range(num_strips)]
    strip_ends = list(strip_offsets[1:]) + [smap_sz]
    img = bytearray(w * h)
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        codec = d[smap_off + start]
        sb = d[smap_off + start + 1: smap_off + end]
        shr = codec % 10
        pix = None
        try:
            if 14 <= codec <= 18:
                pix = decode_zigzag_v(sb, h, shr)
            elif 24 <= codec <= 28:
                pix = decode_zigzag_h(sb, h, shr)
            elif 64 <= codec <= 68:
                pix = decode_majmin_h(sb, h, shr)
        except Exception:
            pix = None
        if pix is None:
            continue
        for y in range(h):
            for x in range(8):
                img[y * w + si * 8 + x] = pix[y * 8 + x]
    rgb = bytearray(w * h * 3)
    for i, idx in enumerate(img):
        r, g, b = pal[(idx + 16) & 0xFF]
        rgb[i*3] = r; rgb[i*3+1] = g; rgb[i*3+2] = b
    return Image.frombytes('RGB', (w, h), bytes(rgb))


def _render_pristine_amiga_obims(disk_path, room_id, out_dir):
    """Decode pristine OBIM frames into out_dir. Returns list of file paths."""
    from decode_obim import decode_room_obims
    decode_room_obims(disk_path, room_id, out_dir)
    return [os.path.join(out_dir, f) for f in sorted(os.listdir(out_dir))
            if f.endswith('.png')]


def _render_pristine_cost_frames(room, out_dir, name_prefix):
    """Render every cost frame in `room` (pristine_cache room dict) to its
    own RGBA PNG under out_dir. Returns list of paths."""
    out = []
    clut = room['clut']
    for cs in room['costumes']:
        if not cs['frames']:
            continue
        pt = cs['pal_table']
        cid = cs['cost_id']
        for fi, f in enumerate(cs['frames']):
            w, h, pix = f['w'], f['h'], f['pixels']
            im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
            px = im.load()
            for yy in range(h):
                for xx in range(w):
                    v = pix[yy * w + xx]
                    if v == 0 or v >= len(pt):
                        continue
                    slot = pt[v]
                    if slot in (0, 250):
                        continue
                    px[xx, yy] = (clut[slot*3], clut[slot*3+1],
                                  clut[slot*3+2], 255)
            p = os.path.join(out_dir, f'{name_prefix}_cid{cid:04d}_f{fi:03d}.png')
            im.save(p)
            out.append(p)
    return out


def build_pristine_bg(room_id):
    """Return pristine Amiga BG as a PIL Image (no OBIMs / costumes)."""
    cache = _get_pristine_cache()
    room = cache.room(room_id)
    if room is None:
        return None
    disk_path = f'{REPO_ROOT}/amiga-data/monkey2.{room["disk"]:03d}'
    if not os.path.exists(disk_path):
        return None
    return _render_pristine_amiga_bg(disk_path, room_id)


def build_pc_bg(room_name):
    """Return the extracted PC source BG for the room (RGB), or None."""
    matches = glob.glob(f'{PC_BG_DIR}/*_{room_name}.png')
    if not matches:
        return None
    return Image.open(matches[0]).convert('RGB')


def _compose_stack(panels):
    """Vertical stack of (label, Image) panels with text labels. Returns
    composed RGBA Image."""
    from PIL import ImageDraw, ImageFont
    pad = 4
    label_h = 18
    panels = [p for p in panels if p[1] is not None]
    if not panels:
        return None
    width = max(im.width for _, im in panels)
    height = sum(im.height for _, im in panels) + len(panels) * label_h \
             + (len(panels) + 1) * pad
    out = Image.new('RGBA', (width, height), (16, 16, 16, 255))
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Monaco.ttf', 12)
    except Exception:
        font = ImageFont.load_default()
    y = pad
    for label, im in panels:
        draw.text((pad, y), label, fill=(255, 255, 255), font=font)
        y += label_h
        out.paste(im, (0, y))
        y += im.height + pad
    return out


def build_ehb_bg(room_name):
    """Re-quantize bg.png alone with png2amiga --mode ehb --depth 6 —
    32 base + 32 hardware-halved derived slots. Returns rendered RGBA
    PIL Image, or None on failure. Preview-only: the engine can't read
    6bp SMAP."""
    import subprocess
    workdir = f'{INTERMEDIATES_DIR}/{room_name}'
    bg_png = f'{workdir}/bg.png'
    if not os.path.exists(bg_png):
        return None

    png2amiga = os.environ.get(
        'PNG2AMIGA',
        os.path.expanduser('~/png2amiga/build/png2amiga'))
    if not os.path.exists(png2amiga):
        return None

    with tempfile.TemporaryDirectory() as tmp:
        idx_path = f'{tmp}/bg.idx'
        cmd = [png2amiga, '--mode', 'ehb', '--depth', '6',
               '--no-scale', '--print-palette-json',
               '--output-indexed', idx_path,
               bg_png, '-o', f'{tmp}/bg_ehb.png']
        if os.environ.get('BEST', '1') != '0':
            cmd.append('--best')
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=600)
        except Exception as e:
            print(f'  [warn] EHB preview ({room_name}): {e}', file=sys.stderr)
            return None
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-2:]
            print(f'  [warn] EHB preview ({room_name}): rc={r.returncode} '
                  f'{tail}', file=sys.stderr)
            return None

        pal_line = next((ln for ln in r.stdout.splitlines()
                         if ln.startswith('{')), None)
        if not pal_line:
            return None
        pj = json.loads(pal_line)
        palette = [(0, 0, 0)] * 64
        for e in pj['palette']:
            palette[e['idx']] = (int(e['rgb'][0:2], 16),
                                 int(e['rgb'][2:4], 16),
                                 int(e['rgb'][4:6], 16))

        if not os.path.exists(idx_path):
            return None
        src_bg = Image.open(bg_png)
        w, h = src_bg.size
        idx_bytes = open(idx_path, 'rb').read()
        if len(idx_bytes) != w * h:
            return None
        return render_idx_to_rgba(idx_bytes, w, h, palette).copy()


def build_preview(room_name):
    workdir = f'{INTERMEDIATES_DIR}/{room_name}'
    if not os.path.isdir(workdir):
        print(f'[skip] {room_name}: no workdir at {workdir}', file=sys.stderr)
        return False

    rid = lookup_room_id(room_name)
    if rid is None:
        print(f'[skip] {room_name}: no PC bg PNG found', file=sys.stderr)
        return False

    palette_json = f'{workdir}/palette.json'
    if not os.path.exists(palette_json):
        # Fallback to the per-cost copy for older rooms.
        alt = f'{workdir}/cost_atlas_palette.json'
        if os.path.exists(alt):
            palette_json = alt
        else:
            print(f'[skip] {room_name}: no palette.json — re-run inject_room.py',
                  file=sys.stderr)
            return False
    palette = load_palette(palette_json)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = f'{OUTPUT_DIR}/{rid}-{room_name}.png'

    # BG-only side-by-side stack:
    #   1. PC original (DOS VGA, 256-colour reference)
    #   2. Pristine Amiga (1992 ship, 32-colour lores)
    #   3. mi2-redux (32-colour lores re-encode)
    #   4. (optional, EHB=1) What if MI2 had targeted EHB? 64-colour preview
    # OBIMs and costumes are intentionally not included.
    with tempfile.TemporaryDirectory() as tmp:
        ours_bg_path = render_bg_frame(
            f'{workdir}/bg.idx', f'{workdir}/bg.png', palette, tmp)
        if not ours_bg_path:
            print(f'[skip] {room_name}: bg.idx missing in {workdir}',
                  file=sys.stderr)
            return False
        ours_bg = Image.open(ours_bg_path).convert('RGB').copy()

    try:
        rid_int = int(rid)
    except ValueError:
        rid_int = None
    pc_bg = build_pc_bg(room_name)
    pristine_bg = build_pristine_bg(rid_int) if rid_int is not None else None
    ehb_bg = (build_ehb_bg(room_name)
              if os.environ.get('EHB', '0') != '0' else None)
    panels = [
        ('PC original (DOS VGA, 256-colour reference)', pc_bg),
        ('Pristine Amiga (1992, 32-colour lores)', pristine_bg),
        ('mi2-redux (32-colour lores re-encode, ships in dist/)', ours_bg),
    ]
    if ehb_bg is not None:
        panels.append(('What if MI2 had targeted EHB? (64-colour, demo '
                       'only — engine cannot read 6bp SMAP)', ehb_bg))
    composed = _compose_stack(panels)
    if composed is None:
        ours_bg.save(out_path)
        sz = ours_bg.size
    else:
        composed.save(out_path)
        sz = composed.size
    n_rows = sum(1 for _, im in panels if im is not None)
    print(f'  {rid}-{room_name}: {n_rows}-row stack -> {sz[0]}x{sz[1]}'
          f' ({out_path})')
    return True


def build_global_previews():
    """Copy each global-costume quant.png (already-rendered output of
    encode_global_*.py) into preview/quality/ with a `global-<name>`
    prefix, so the user has one place to spot-check everything.

    Sources:
      preview/global_guybrush/guybrush_quant.png
      preview/global_extras/<name>_quant.png
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sources = []
    gb = f'{REPO_ROOT}/preview/global_guybrush/guybrush_quant.png'
    if os.path.exists(gb):
        sources.append(('guybrush', gb))
    extras_dir = f'{REPO_ROOT}/preview/global_extras'
    if os.path.isdir(extras_dir):
        for name in sorted(os.listdir(extras_dir)):
            if name.endswith('_quant.png'):
                stem = name[:-len('_quant.png')]
                sources.append((stem, os.path.join(extras_dir, name)))
    n = 0
    for stem, src in sources:
        dst = f'{OUTPUT_DIR}/global-{stem}.png'
        shutil.copy(src, dst)
        sz = Image.open(dst).size
        print(f'  global-{stem}: {sz[0]}x{sz[1]} ({dst})')
        n += 1
    return n


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    arg = sys.argv[1]
    if arg == '--all':
        if not os.path.isdir(INTERMEDIATES_DIR):
            print(f'[error] {INTERMEDIATES_DIR} does not exist', file=sys.stderr)
            sys.exit(1)
        rooms = sorted(d for d in os.listdir(INTERMEDIATES_DIR)
                       if os.path.isdir(os.path.join(INTERMEDIATES_DIR, d)))
        ok = 0
        for r in rooms:
            if build_preview(r):
                ok += 1
        n_globals = build_global_previews()
        print(f'\n{ok}/{len(rooms)} room previews + {n_globals} global '
              f'previews written to {OUTPUT_DIR}')
    elif arg == '--globals':
        n = build_global_previews()
        print(f'\n{n} global previews written to {OUTPUT_DIR}')
    else:
        if not build_preview(arg):
            sys.exit(1)


if __name__ == '__main__':
    main()
