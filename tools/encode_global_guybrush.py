#!/usr/bin/env python3
"""One-shot tool: re-encode Guybrush (cost_id 1) once, globally.

Plan:
  1. Render his 100 frames using rid 9 (bar)'s pristine CLUT[192..207].
     This gives the visually-correct classic MI2 Guybrush appearance
     (blond hair, dark blue coat, brown trim).
  2. Run png2amiga with --reserve-range 1-21 to force Guybrush's
     colours into palette indices 22..31 (10 slots).
     palette[0]   = transparent target (--transparent-color FF00FF
                                           pins magenta there)
     palette[1..21] = reserved (skipped by quantizer)
     palette[22..31] = the 10 canonical Guybrush colours (--best
                       picks them from the atlas pixels)
  3. Save those 10 RGBs as the canonical sub-palette
     (tools/global_actor_palette.json).
  4. Re-encode Guybrush's COST chunk: new pal_table maps his pixels
     to CLUT slots 38..47 (paletteMod=16 + palette indices 22..31).
     Patch the chunk into monkey2-hd/monkey2.001 (his home is rid 111
     on disk 1).

After this runs, every room that draws Guybrush just needs to lock
palette indices 22..31 to the canonical RGBs. They never re-encode him.

Usage:
  python3 tools/encode_global_guybrush.py
"""

import json
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from pristine_cache import cache
from decode_amiga_room import load, walk_rooms, find_chunk, name as cn, be32
from decode_cost import decode_costume
from pc_data import find_pc_cost_body, parse_pc_dcos, parse_pc_loff, find_pc_room_clut
from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf
from encode_cost import build_new_palette_table, remap_pixels, rebuild_cost_body

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



REPRESENTATIVE_RID = 9              # bar — classic MI2 Guybrush palette
PNG2AMIGA = os.environ['PNG2AMIGA']    # set by build.sh / bootstrap.sh
# BEST=0 in env disables --best (population search) for fast iteration.
BEST_FLAG = ['--best'] if os.environ.get('BEST', '1') != '0' else []
# DITHER=<method> overrides the dither method (default = --dither none
# for the global encoder since RLE-cost frames packed with dithering can
# blow 16-bit baseptr addressing for large costumes).
DITHER_ARGS = (['--dither', os.environ['DITHER']]
               if os.environ.get('DITHER') else ['--dither', 'opt-checker'])
# VERBOSE=1 prints each png2amiga subprocess command before running.
VERBOSE = os.environ.get('VERBOSE', '0') != '0'


def _run_png2amiga(cmd, label=''):
    """Run a png2amiga subprocess. If VERBOSE=1, print the full command
    line so it can be copy-pasted into a shell."""
    if VERBOSE:
        prefix = f"[png2amiga {label}] " if label else "[png2amiga] "
        print(prefix + ' '.join(repr(a) for a in cmd))
    return subprocess.run(cmd, capture_output=True, text=True)
AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
SVM_DIR = f'{REPO_ROOT}/monkey2-hd'
OUT_PALETTE = os.path.join(os.path.dirname(__file__), 'global_actor_palette.json')
WORKDIR = f'{REPO_ROOT}/preview/global_guybrush'

CID = 1
RESERVED_RGB = (0x00, 0x00, 0x00)   # what reserved slots 1..21 hold
GLOBAL_RANGE_START = 23            # palette indices 23..31 = his 9 colours
GLOBAL_RANGE_END = 31


def render_frames(cost, render_clut, frames_dir):
    """Render every Guybrush frame as its own RGBA PNG inside
    `frames_dir`. Returns layout list with one entry per frame:

      { 'fi', 'w', 'h', 'png': <path>, 'idx': <path> }

    The .idx path is where png2amiga's --oe will write the encoded
    index bytes for that frame. Replaces the old grid-pack atlas:
    png2amiga's --ji 2D bin-packs the inputs internally, so we no
    longer need a giant max-cell canvas. Alpha=0 for SCUMM-defined
    transparent pixels (idx==0 in the COST stream); png2amiga's
    internal alpha-premultiply handles that contract."""
    os.makedirs(frames_dir, exist_ok=True)
    frames = cost['frames']
    pal_t = list(cost['pal_table'])
    layout = []
    for i, f in enumerate(frames):
        w, h, pixels = f['w'], f['h'], f['pixels']
        stem = f'guybrush_f{i:03d}'
        png_path = f'{frames_dir}/{stem}.png'
        idx_path = f'{frames_dir}/{stem}.idx'
        im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        px = im.load()
        for y in range(h):
            for x in range(w):
                idx = pixels[y * w + x]
                if idx == 0 or idx >= len(pal_t):
                    continue
                slot = pal_t[idx]
                px[x, y] = (
                    render_clut[slot * 3],
                    render_clut[slot * 3 + 1],
                    render_clut[slot * 3 + 2],
                    255)
        im.save(png_path)
        layout.append({'fi': i, 'w': w, 'h': h,
                        'png': png_path, 'idx': idx_path})
    return layout


def quantize_frames(layout):
    """Run png2amiga with each frame as a separate `--ji` input.
    png2amiga's smol-atlas internally bin-packs them for palette
    training, then `--oe '{dir}/{stem}.idx'` writes per-frame indexed
    bytes back to the paths the layout already promised.

    Encoding time scales with real pixel area, not grid-cell area.
    Returns 32-entry palette."""
    if not layout:
        raise RuntimeError('quantize_frames: empty layout')
    primary = layout[0]['png']
    ji_inputs = [a for ent in layout[1:] for a in ('--ji', ent['png'])]
    cmd = [PNG2AMIGA, '--mode', 'lores', '--depth', '5', *BEST_FLAG,
            '--no-scale', *DITHER_ARGS,
            '--dither-strength', '0.8',
            '--print-palette-json',
            '--reserve-range', f'1-{GLOBAL_RANGE_START - 1}',
            f'{RESERVED_RGB[0]:02X}{RESERVED_RGB[1]:02X}{RESERVED_RGB[2]:02X}',
            *ji_inputs,
            '--oe', '{dir}/{stem}.idx',
            primary]
    print(f'  Running png2amiga: --reserve-range 1-{GLOBAL_RANGE_START-1}, '
          f'{len(layout)} --ji frames')
    r = _run_png2amiga(cmd, label='guybrush quantize')
    if r.returncode != 0:
        print(r.stdout); print(r.stderr, file=sys.stderr)
        raise RuntimeError(f'png2amiga failed (rc={r.returncode})')
    missing = [e['idx'] for e in layout if not os.path.exists(e['idx'])]
    if missing:
        raise RuntimeError(f'png2amiga did not write {len(missing)} idx '
                            f'(first: {missing[0]})')

    json_line = next((ln for ln in r.stdout.splitlines() if ln.startswith('{')),
                      None)
    if json_line is None:
        raise RuntimeError(f'no palette JSON in stdout:\n{r.stdout}')
    pj = json.loads(json_line)
    palette = [None] * 32
    for e in pj['palette']:
        h = e['rgb']
        palette[e['idx']] = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return palette


def build_preview(layout, palette, out_path):
    """Render each frame's .idx through `palette` to RGBA + use the
    source PNG's alpha as the transparency mask, then PyTexturePacker-
    pack into a tight quant.png so build_quality_preview.py --globals
    can show the actual encoded result."""
    sys.path.insert(0, f'{REPO_ROOT}/tools/PyTexturePacker')
    from PyTexturePacker import Packer
    import tempfile, shutil
    if not layout:
        return
    with tempfile.TemporaryDirectory() as tmp:
        rendered = []
        for ent in layout:
            if not os.path.exists(ent['idx']):
                continue
            idx_bytes = open(ent['idx'], 'rb').read()
            w, h = ent['w'], ent['h']
            src = Image.open(ent['png']).convert('RGBA')
            alpha = src.split()[3].tobytes()
            rgba = bytearray(w * h * 4)
            for i, p in enumerate(idx_bytes):
                if p < len(palette) and palette[p] is not None:
                    r, g, b = palette[p]
                else:
                    r = g = b = 0
                rgba[i*4]     = r
                rgba[i*4 + 1] = g
                rgba[i*4 + 2] = b
                rgba[i*4 + 3] = 255 if alpha[i] else 0
            stem = os.path.basename(ent['png'])
            out = f'{tmp}/{stem}'
            Image.frombytes('RGBA', (w, h), bytes(rgba)).save(out)
            rendered.append(out)
        if not rendered:
            return
        packer = Packer.create(max_width=4096, max_height=4096,
                                bg_color=0x00000000,
                                enable_rotated=False, force_square=False,
                                atlas_format='json',
                                inner_padding=0, border_padding=0,
                                shape_padding=0)
        base = f'{tmp}/preview'
        packer.pack(rendered, base)
        shutil.move(base + '.png', out_path)


def find_guybrush_cost_offset(d, home_rid):
    """Locate the COST chunk for cid 1 in disk file `d`."""
    lflf = find_lflf_for_room(d, home_rid)
    if lflf is None:
        raise RuntimeError(f'LFLF for rid {home_rid} not found')
    cost_offsets = find_costumes_in_lflf(d, lflf)
    home = cache.room(home_rid)
    for co, pc in zip(cost_offsets, home['costumes']):
        if pc['cost_id'] == CID:
            return co, lflf, pc
    raise RuntimeError(f'cid {CID} not found in LFLF')


def remap_costume_to_global(cost, palette, layout):
    """Remap Guybrush's pixels to palette indices [22..31] + transparent.
    Per-frame indexed bytes already live at layout[ent]['idx'] (one .idx
    file per frame from png2amiga's --oe). Read them directly, no atlas
    cropping needed.

    Real-black source pixels and alpha=0 transparent both get quantized
    to slot 0 by png2amiga (palette[0] = #000000). To distinguish, we
    re-mask each frame using the source PNG's alpha channel: alpha=0 ->
    TRANSPARENT_MARKER (slot 17, reserved by --reserve-range so no real
    pixel lands there), alpha=255 -> keep idx (including idx=0 = real
    black).

    Output: new pal_table (npal=16 entries; each is a CLUT slot) and
    new pixel data per frame (each pixel = pal_table index 0..15).
    """
    npal = cost['npal']                   # 16 for fmt 0x58
    TRANSPARENT_NEWIDX = 17               # reserved sentinel
    frame_pixels = {}
    for ent in layout:
        idx_bytes = open(ent['idx'], 'rb').read()
        src = Image.open(ent['png']).convert('RGBA')
        alpha = src.split()[3].tobytes()
        buf = bytearray(len(idx_bytes))
        for i, p in enumerate(idx_bytes):
            buf[i] = TRANSPARENT_NEWIDX if alpha[i] == 0 else p
        frame_pixels[ent['fi']] = bytes(buf)

    # Frequency-rank non-transparent indices
    from collections import Counter
    freq = Counter()
    for buf in frame_pixels.values():
        for b in buf:
            if b != TRANSPARENT_NEWIDX:
                freq[b] += 1
    if not freq:
        raise RuntimeError('no non-transparent Guybrush pixels — bad render?')
    ranked = freq.most_common()

    # Build the new pal_table. Each new_palette idx (22..31) maps to a CLUT
    # slot via paletteMod=16 → CLUT[16 + idx] = CLUT[38..47].
    # build_new_palette_table picks up to (npal - 1) ranked indices and
    # assigns pal_table[1..k] to their CLUT slots; pal_table[0] = 250
    # (transparent sentinel).
    new_table, direct_remap, chosen = build_new_palette_table(ranked, npal)
    # Assign each chosen new_palette_idx to its CLUT slot = idx + 16.
    # build_new_palette_table's default puts CLUT slots straight in;
    # override here to ensure they fall in [38..47].
    # `chosen` is the ordered list of new_palette_idx (0..31). We map
    # each to (idx + 16) which gives CLUT slot 38..47 for indices 22..31.
    new_table = [250 if i == 0 else 0 for i in range(npal)]
    direct_remap = [0] * 32
    for k, npi in enumerate(chosen, start=1):
        clut_slot = npi + 16
        new_table[k] = clut_slot
        direct_remap[npi] = k
    direct_remap[TRANSPARENT_NEWIDX] = 0

    # Re-map per-frame pixels from new_palette_idx → pal_table idx (0..15).
    new_frame_pixels = {}
    for fi, buf in frame_pixels.items():
        new_frame_pixels[fi] = remap_pixels(buf, direct_remap, palette, chosen,
                                             TRANSPARENT_NEWIDX)
    return new_table, new_frame_pixels


def main():
    os.makedirs(WORKDIR, exist_ok=True)

    home_rid = cache.cost_home(CID)
    home_disk = cache.data['rooms'][home_rid]['disk']
    print(f'Guybrush cid {CID} (Amiga home rid {home_rid} on disk {home_disk})')
    home_room = cache.room(home_rid)
    amiga_cost = next(c for c in home_room['costumes'] if c['cost_id'] == CID)

    # Source pixels from the Amiga pristine cost. Mixing PC source
    # caused multiple regressions (Amiga port has port-specific fills
    # the runtime expects) — switching to Amiga everywhere makes the
    # rendering chain straightforward and matches what the original
    # 1992 Amiga release shipped.
    fmt = amiga_cost['body'][1] & 0x7F
    npal = {0x58: 16, 0x59: 32}[fmt]
    # Guybrush's "home" rid 111 is a SCUMM asset host with an empty
    # CLUT (all zeros). For rendering we need a real room where he's
    # drawn — pick the first one in costume_refs with a non-trivial
    # CLUT[16..47]. cid 1's pal_table points at CLUT[192..207] which
    # only those rooms populate properly.
    refs_drawn = json.load(open(os.path.join(os.path.dirname(__file__),
                                              'costume_refs.json')))
    drawn_rids = refs_drawn['drawn_in'].get(str(CID), [])
    render_rid = home_rid
    for rid in drawn_rids:
        rd = cache.room(rid)
        if not rd: continue
        c = rd.get('clut')
        if c and sum(c[16*3:48*3]) > 100:
            render_rid = rid
            break
    home_clut = cache.room(render_rid)['clut']
    print(f'  render via rid {render_rid} (asset home rid={home_rid})')
    print(f'  Amiga home rid {home_rid}; fmt=0x{fmt:02x} npal={npal} '
          f'frames={len(amiga_cost["frames"])} body={len(amiga_cost["body"])}')
    cost = {
        'pal_table': list(amiga_cost['pal_table']),
        'frames': amiga_cost['frames'],
        'body': amiga_cost['body'],
        'npal': amiga_cost['npal'],
        'frame_offsets_in_body': amiga_cost['frame_offsets_in_body'],
    }

    # Step 1: render each frame as its own PNG via the Amiga home
    # room's CLUT. The actor's pal_table maps RLE indices into CLUT
    # slots; using the home CLUT reproduces the colours the engine
    # actually shows when Guybrush is drawn there.
    frames_dir = f'{WORKDIR}/guybrush_frames'
    if os.path.isdir(frames_dir):
        for fn in os.listdir(frames_dir):
            if fn.endswith('.idx'):
                os.unlink(os.path.join(frames_dir, fn))
    layout = render_frames(cost, home_clut, frames_dir)
    print(f'  {len(layout)} frames staged in {frames_dir}/')

    # Step 2: quantize into palette[22..31] via --ji + --oe
    palette = quantize_frames(layout)
    print(f'  Quantized. Palette[22..31]:')
    for i in range(GLOBAL_RANGE_START, GLOBAL_RANGE_END + 1):
        if palette[i]:
            print(f'    [{i:2d}] #{palette[i][0]:02x}{palette[i][1]:02x}{palette[i][2]:02x}')

    # Step 3: extract canonical sub-palette → JSON
    canonical_amiga = {str(16 + i): list(palette[i])
                       for i in range(GLOBAL_RANGE_START, GLOBAL_RANGE_END + 1)
                       if palette[i] is not None}
    out = {
        'guybrush': {
            'cost_id': CID,
            'amiga_home_rid': home_rid,
            'source': 'amiga',
            'pal_index_start': GLOBAL_RANGE_START,
            'pal_index_end': GLOBAL_RANGE_END,
            'rooms_drawn_in': sorted(json.load(
                open(os.path.join(os.path.dirname(__file__),
                                   'costume_refs.json')))['drawn_in'][str(CID)]),
            'canonical_amiga': canonical_amiga,
        },
    }
    with open(OUT_PALETTE, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'  Wrote {OUT_PALETTE}')

    # Step 4: re-encode COST + patch into disk file
    # Render preview atlas for build_quality_preview --globals
    try:
        build_preview(layout, palette, f'{WORKDIR}/guybrush_quant.png')
    except Exception as e:
        print(f'  [warn] preview render skipped: {e}')

    new_pal_table, new_frame_pixels = remap_costume_to_global(
        cost, palette, layout)
    print(f'  New pal_table[0..15]: {new_pal_table}')

    # Build new costume body
    cbody = cost['body']
    npal = cost['npal']
    data_offsets = cost['frame_offsets_in_body']
    frame_remaps = {data_offsets[fi]: pix
                     for fi, pix in new_frame_pixels.items()}
    new_body = rebuild_cost_body(cbody, new_pal_table, frame_remaps, npal)
    print(f'  New body size: {len(new_body)} (old: {len(cbody)})')

    # Tree-based patch: load disk via scumm_tree, find cid 1's COST,
    # replace its body, serialize back. Sizes recompute automatically;
    # no offset bookkeeping. After this we also rebuild the index since
    # COST size may have changed.
    from scumm_tree import (parse, parse_index, serialize, serialize_index,
                              find_lflf_for_room as _flfr)
    from scumm_index import rebuild_index, parse_droo

    src_disk_path = f'{SVM_DIR}/monkey2.{home_disk:03d}'
    if not os.path.exists(src_disk_path):
        src_disk_path = f'{AMIGA_DIR}/monkey2.{home_disk:03d}'
    print(f'  Source disk: {src_disk_path}')
    XOR = 0x69
    raw = open(src_disk_path, 'rb').read()
    plain = bytes(b ^ XOR for b in raw)
    tree = parse(plain)
    lflf_node = _flfr(tree, home_rid)
    if lflf_node is None:
        sys.exit(f'no LFLF for rid {home_rid} in disk {home_disk}')

    # Find Guybrush's COST in his home LFLF. cid 1 is the ONLY costume
    # in rid 111's LFLF (host room for the player), so we just take the
    # first COST node. This is robust against repeated runs where the
    # pal_table has already been re-encoded — position identity stays
    # constant.
    cost_nodes = [c for c in lflf_node.children if c.tag == 'COST']
    if not cost_nodes:
        sys.exit(f'no COST chunks in rid {home_rid} LFLF')
    if len(cost_nodes) > 1:
        sys.exit(f'rid {home_rid} has {len(cost_nodes)} COSTs '
                  f'(expected 1 for Guybrush home)')
    target_cost = cost_nodes[0]
    old_size = len(target_cost.body)
    target_cost.body = new_body
    print(f'  Replaced COST body: {old_size} -> {len(new_body)} bytes')

    # Find every disk that carries an LFLF for home_rid. rid 111 is
    # `alldisks` — replicated byte-identically across every floppy so
    # SCUMM v5 Amiga's CHARSET/COST loader can read it from whichever
    # disk is currently mounted without forcing a swap. Patching only
    # the home_disk leaves disks 2-10 with pristine room 111 content
    # but a DCHR table whose offsets were rebuilt against the home_disk's
    # GROWN LFLF — so entering any room not on disk 1 reads pristine
    # bytes at post-patch offsets, landing inside the wrong chunk.
    # Replicate the same COST body into every disk that has rid 111.
    idx_path = f'{SVM_DIR}/monkey2.000'
    if not os.path.exists(idx_path):
        idx_path = f'{AMIGA_DIR}/monkey2.000'
    idx_plain = bytes(b ^ XOR for b in open(idx_path, 'rb').read())
    index_root = parse_index(idx_plain)
    droo = next(c for c in index_root.children if c.tag == 'DROO')
    _, droo_disks_list = parse_droo(droo.body)
    droo_disks = {rid: dn for rid, dn in enumerate(droo_disks_list) if dn > 0}

    disk_trees = {home_disk: tree}
    mutated_disks = {home_disk}
    replicated_disks = []
    for d_n in set(droo_disks.values()):
        if d_n == home_disk:
            continue
        d_path = f'{SVM_DIR}/monkey2.{d_n:03d}'
        if not os.path.exists(d_path):
            d_path = f'{AMIGA_DIR}/monkey2.{d_n:03d}'
        d_plain = bytes(b ^ XOR for b in open(d_path, 'rb').read())
        d_tree = parse(d_plain)
        d_lflf = _flfr(d_tree, home_rid)
        if d_lflf is not None:
            d_cost_nodes = [c for c in d_lflf.children if c.tag == 'COST']
            if d_cost_nodes:
                d_cost_nodes[0].body = new_body
                replicated_disks.append(d_n)
                mutated_disks.add(d_n)
        disk_trees[d_n] = d_tree

    # Serialize every disk we hold a tree for (need positions for the
    # index rebuild) but only write back the ones we actually mutated.
    disk_positions = {}
    for d_n, d_tree in disk_trees.items():
        d_pos = {}
        out = serialize(d_tree, d_pos)
        disk_positions[d_n] = d_pos
        if d_n in mutated_disks:
            with open(f'{SVM_DIR}/monkey2.{d_n:03d}', 'wb') as f:
                f.write(bytes(b ^ XOR for b in out))
            kind = 'home' if d_n == home_disk else f'rid {home_rid} replicated'
            print(f'  -> monkey2.{d_n:03d} ({len(out)} bytes, {kind})')
    if replicated_disks:
        print(f'  Replicated rid {home_rid} COST to disks: '
              f'{sorted(replicated_disks)}')

    rebuild_index(index_root, disk_trees, disk_positions, droo_disks)
    new_idx = serialize_index(index_root)
    out_idx_path = f'{SVM_DIR}/monkey2.000'
    with open(out_idx_path, 'wb') as f:
        f.write(bytes(b ^ XOR for b in new_idx))
    print(f'  -> {out_idx_path} (index rebuilt, {len(new_idx)} bytes)')

    # Sanity: re-decode
    print(f'\nDone. Guybrush is now globally encoded with palette indices '
          f'{GLOBAL_RANGE_START}..{GLOBAL_RANGE_END} (= CLUT slots 38..47).')
    print(f'Every Guybrush room must lock those slots:')
    for slot_str, rgb in canonical_amiga.items():
        print(f'  --lock-index {int(slot_str) - 16} '
              f'{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}')


if __name__ == '__main__':
    main()
