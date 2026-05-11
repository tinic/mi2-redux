#!/usr/bin/env python3
"""One-shot tool: re-encode each multi-room non-Guybrush costume group
into its dedicated palette slot range.

Reads tools/cost_groups.json (hand-editable). For each group:
  1. Collect cids; for each cid, locate its PC COST chunk via PC DCOS.
  2. Render every frame using rid 9 (bar) Amiga CLUT for slot mapping
     (PC and Amiga share pal_table values). Filter decoder garbage.
  3. Pack frames into a mega-atlas, run png2amiga forcing output into
     the group's pal_indices range only.
  4. Extract those RGBs as the group's canonical sub-palette.
  5. Re-encode each cid's COST chunk (new pal_table -> CLUT slots in
     paletteMod=16 + group's pal_indices). Patch into each cid's home
     disk file (Amiga side — that's what we ship).

After running once, tools/global_actor_palette.json gains a 'groups'
array. inject_room.py reads it and locks the appropriate slot ranges
per room based on which group cids appear in that room's scripts.

Usage:
  python3 tools/encode_global_extras.py
"""

import json
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from pristine_cache import cache
from decode_amiga_room import (load, walk_rooms, find_chunk, name as cn,
                                be32)
from decode_cost import decode_costume
from remap_costume_palette import (find_lflf_for_room,
                                     find_costumes_in_lflf)
from encode_cost import remap_pixels, rebuild_cost_body

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



PNG2AMIGA = os.environ['PNG2AMIGA']    # set by build.sh / bootstrap.sh
# BEST=0 in env disables --best (population search) for fast iteration.
BEST_FLAG = ['--best'] if os.environ.get('BEST', '1') != '0' else []
# DITHER=<method> overrides the dither method (default = --dither none
# for the global extras encoder since dithered RLE can blow 16-bit
# baseptr addressing on dense costume sets).
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
PC_DATA_PATH = f'{REPO_ROOT}/pc-data/MONKEY2.001'
PC_INDEX_PATH = f'{REPO_ROOT}/pc-data/MONKEY2.000'
GROUPS_CONFIG = os.path.join(os.path.dirname(__file__), 'cost_groups.json')
GAP_PATH = os.path.join(os.path.dirname(__file__), 'global_actor_palette.json')
WORKDIR = f'{REPO_ROOT}/preview/global_extras'

REPRESENTATIVE_RID = 9     # bar — render colors via this room's CLUT
RESERVED_RGB = (0x00, 0x00, 0x00)

GARBAGE_W = 150
GARBAGE_H = 150


def load_pc_data():
    """Load + XOR-decrypt PC LECF + index. Returns (data_bytes,
    {cid: (home_rid, dcos_offset)}, {rid: ROOM_offset_in_data}).
    """
    d = bytes(b ^ 0x69 for b in open(PC_DATA_PATH, 'rb').read())
    idx = bytes(b ^ 0x69 for b in open(PC_INDEX_PATH, 'rb').read())
    # DCOS
    p = 0
    pc_dcos = {}
    while p < len(idx):
        tag = cn(idx, p); sz = be32(idx, p + 4)
        if tag == 'DCOS':
            cnt = struct.unpack('<H', idx[p + 8:p + 10])[0]
            rooms = list(idx[p + 10:p + 10 + cnt])
            offs = [struct.unpack('<I',
                                   idx[p + 10 + cnt + i * 4:
                                       p + 10 + cnt + (i + 1) * 4])[0]
                    for i in range(cnt)]
            for cid in range(cnt):
                if rooms[cid] > 0:
                    pc_dcos[cid] = (rooms[cid], offs[cid])
            break
        p += sz
    # LOFF: rid -> ROOM offset
    pc_room_off = {}
    p2 = 17
    cnt2 = d[16]
    for _ in range(cnt2):
        rid = d[p2]
        off = struct.unpack('<I', d[p2 + 1:p2 + 5])[0]
        pc_room_off[rid] = off
        p2 += 5
    return d, pc_dcos, pc_room_off


def get_pc_cost_body(d, pc_dcos, pc_room_off, cid):
    """Return PC COST body bytes (= chunk minus 8-byte header) or
    None if not found."""
    if cid not in pc_dcos:
        return None
    home_rid, doff = pc_dcos[cid]
    if home_rid not in pc_room_off:
        return None
    cost_off = pc_room_off[home_rid] + doff
    if cn(d, cost_off) != 'COST':
        return None
    sz = be32(d, cost_off + 4)
    return d[cost_off + 8:cost_off + sz]


def _build_group_preview(layout, palette, out_path):
    """Render each frame's .idx through `palette` to RGBA, then
    PyTexturePacker-pack them into a tight preview PNG. Mirrors what
    build_quality_preview.py does for per-room previews."""
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
            # Use the source PNG's alpha channel as the transparency
            # mask: png2amiga's quantizer mattes alpha=0 to nearest-
            # to-black during encoding, but for the preview we want
            # original transparency back.
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


def render_group_frames(group, pc_data, pc_dcos, pc_room_off, frames_dir):
    """Render every cid in the group's frames as individual RGBA PNGs
    in `frames_dir`. Returns layout list with one entry per frame:

      { 'cid', 'fi', 'w', 'h', 'png': <path>, 'idx': <path>, 'src' }

    The .idx path is where png2amiga's --oe will write the encoded
    index bytes for that frame. We pre-compute it so callers can read
    each frame's encoding back without atlas cropping.

    Replaces the old grid-pack render_group_atlas: png2amiga's --ji
    mode bin-packs the inputs internally (smol-atlas), so we no longer
    need to build a giant max-cell grid that wasted 70-90% of the
    canvas on padding.
    """
    os.makedirs(frames_dir, exist_ok=True)
    # Always use the Amiga pristine source. PC source is theoretically
    # higher-fidelity (full 8-bit VGA) but the Amiga port has port-
    # specific fills that the runtime expects (e.g. cid 27 flame's gray
    # log-base, character shadow tints, etc.). Mixing PC pixels into
    # the Amiga rendering chain has caused multiple regressions so we
    # just stick with the original Amiga art everywhere.
    layout = []
    for cid in group['cids']:
        amiga_cost = next((c for c in cache.room(cache.cost_home(cid))['costumes']
                           if c['cost_id'] == cid), None)
        if amiga_cost is None or not amiga_cost['frames']:
            print(f'    [warn] cid {cid}: not in Amiga cache, skipping')
            continue
        pal_t = list(amiga_cost['pal_table'])
        src_frames = [(f['w'], f['h'], f['pixels'])
                      for f in amiga_cost['frames']]
        src = 'Amiga'
        # Render via the cid's HOME CLUT — that's where its pal_table
        # entries point at the authentic Amiga colours. Using a single
        # representative-room CLUT for all cids was wrong: e.g. cid 27
        # (campfire flame, home rid=4) routed pal_table idx 5+ through
        # rid=9's CLUT[5..15] which are EGA-template magenta/cyan/etc.,
        # so the rendered RGBA atlas got teal/pink pixels that biased
        # the optimizer's centroids away from real flame colours.
        clut = cache.room(cache.cost_home(cid))['clut']
        for fi, (w, h, pix) in enumerate(src_frames):
            if w > GARBAGE_W or h > GARBAGE_H:
                continue
            stem = f'cid{cid:04d}_f{fi:03d}'
            png_path = f'{frames_dir}/{stem}.png'
            idx_path = f'{frames_dir}/{stem}.idx'
            im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
            px = im.load()
            for y in range(h):
                for x in range(w):
                    v = pix[y * w + x]
                    if v == 0 or v >= len(pal_t):
                        continue
                    slot = pal_t[v]
                    px[x, y] = (
                        clut[slot * 3],
                        clut[slot * 3 + 1],
                        clut[slot * 3 + 2],
                        255)
            im.save(png_path)
            layout.append({'cid': cid, 'fi': fi, 'w': w, 'h': h,
                            'png': png_path, 'idx': idx_path, 'src': src})
    return layout


def quantize_group(layout, pal_indices, guybrush_locks, lock_rgbs=None):
    """Run png2amiga with each frame as a separate `--ji` input. png2amiga
    bin-packs them internally (smol-atlas) for palette training, then
    `--oe '{dir}/{stem}.idx'` writes per-frame indexed bytes back to the
    paths the layout already promised.

    Compared to the old grid-pack atlas (one big PNG with 70-90% of the
    canvas as transparent padding), this gives png2amiga a tight
    training corpus and avoids the giant Image.new + paste loop on the
    Python side. Encoding time scales with real pixel area, not
    grid-cell area.

    Returns (32-entry palette, S2 or None). S2 is parsed from
    png2amiga's `Encoded: ... S2: NN.NN` summary line so the slot-
    allocation solver can score candidate target counts.
    """
    # Build reserve range = everything except 0 (transparent target),
    # pal_indices, and Guybrush slots. Guybrush slots are LOCKED (not
    # reserved) so character costumes can route skin/clothing pixels
    # there — their skin tones overlap Guybrush's locked range, so
    # using those slots saves space in the costume's own pal_table.
    # The cost re-encode below explicitly extends `chosen` to include
    # Guybrush slots when frame pixels land there.
    avail = set(pal_indices)
    avail.add(0)
    avail.update(g['pi'] for g in guybrush_locks)
    reserved = sorted(set(range(32)) - avail)
    runs = []
    for s in reserved:
        if runs and runs[-1][1] + 1 == s:
            runs[-1] = (runs[-1][0], s)
        else:
            runs.append((s, s))
    reserve_spec = ','.join(f'{a}-{b}' if a != b else str(a)
                             for a, b in runs)

    locks_args = []
    for g in guybrush_locks:
        locks_args += ['--lock-index', str(g['pi']), g['rgb']]
    # Hand-picked slot RGBs override the optimizer for specific
    # pal_indices (e.g. force `palette[6] = orange` for extras_a_warm
    # so flame costumes don't end up centroid-pulled to pink by the
    # group's other cids).
    for pi, rgb_hex in (lock_rgbs or {}).items():
        if pi in pal_indices:
            locks_args += ['--lock-index', str(pi), rgb_hex]

    if not layout:
        raise RuntimeError('quantize_group: empty layout')
    primary = layout[0]['png']
    ji_inputs = [a for ent in layout[1:] for a in ('--ji', ent['png'])]
    cmd = [PNG2AMIGA, '--mode', 'lores', '--depth', '5', *BEST_FLAG,
            '--no-scale', *DITHER_ARGS,
            '--dither-strength', '0.8',
            '--print-palette-json',
            '--reserve-range', reserve_spec,
            f'{RESERVED_RGB[0]:02X}{RESERVED_RGB[1]:02X}{RESERVED_RGB[2]:02X}',
            *locks_args,
            *ji_inputs,
            '--oe', '{dir}/{stem}.idx',
            primary]
    print(f'    --reserve-range {reserve_spec}, {len(layout)} --ji frames')
    r = _run_png2amiga(cmd, label=f'extras quantize')
    if r.returncode != 0:
        print(r.stdout); print(r.stderr, file=sys.stderr)
        raise RuntimeError(f'png2amiga failed (rc={r.returncode})')
    # Verify every frame got its idx file
    missing = [e['idx'] for e in layout if not os.path.exists(e['idx'])]
    if missing:
        raise RuntimeError(f'png2amiga did not write {len(missing)} idx '
                            f'(first: {missing[0]})')
    json_line = next((ln for ln in r.stdout.splitlines() if ln.startswith('{')),
                      None)
    pj = json.loads(json_line)
    palette = [None] * 32
    for e in pj['palette']:
        h = e['rgb']
        palette[e['idx']] = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    s2 = None
    for line in r.stdout.splitlines():
        if 'S2:' in line:
            try:
                s2 = float(line.split('S2:')[1].split()[0])
            except (ValueError, IndexError):
                pass
            break
    return palette, s2


def remap_costume_to_group(amiga_cost, palette, layout, group_pal_indices,
                            shared_pal_indices=frozenset()):
    """Per-frame indexed bytes already live at layout[ent]['idx'] (one
    .idx file per frame from png2amiga's --oe). Read them directly,
    no atlas cropping needed.

    Real-black source pixels and alpha=0 transparent both quantize to
    slot 0 (palette[0] = #000000). Distinguish by re-masking with the
    source PNG's alpha channel: alpha=0 -> TRANSPARENT_MARKER (slot 17,
    --reserve-range guarantees no real pixel lands there), alpha=255 ->
    keep idx (including idx=0 = real black).
    """
    npal = amiga_cost['npal']
    cid = amiga_cost['cost_id']
    TRANSPARENT_NEWIDX = 17               # reserved sentinel
    frame_pixels = {}
    for ent in layout:
        if ent['cid'] != cid:
            continue
        idx_bytes = open(ent['idx'], 'rb').read()
        src = Image.open(ent['png']).convert('RGBA')
        alpha = src.split()[3].tobytes()
        buf = bytearray(len(idx_bytes))
        for i, p in enumerate(idx_bytes):
            buf[i] = TRANSPARENT_NEWIDX if alpha[i] == 0 else p
        frame_pixels[ent['fi']] = bytes(buf)

    from collections import Counter
    freq = Counter()
    for buf in frame_pixels.values():
        for b in buf:
            if b != TRANSPARENT_NEWIDX:
                freq[b] += 1
    if not freq:
        return None, None

    # Order pal_indices by their pixel-frequency in this cid. Include
    # both the group's own slots AND shared slots (e.g. Guybrush's
    # [22..31]) — the shared slots are locked at the room level too,
    # so character costumes routing skin/clothing pixels there will
    # render correctly without needing dedicated per-group palette
    # entries.
    allowed = set(group_pal_indices) | set(shared_pal_indices)
    chosen = [pi for pi, _ in freq.most_common() if pi in allowed]
    # Pad if needed
    for pi in group_pal_indices:
        if pi not in chosen:
            chosen.append(pi)
    chosen = chosen[:npal - 1]   # leave slot 0 for transparent

    # Build new pal_table: index 0 = 250 (transparent sentinel), then
    # CLUT slots corresponding to each chosen pi (= pi + 16).
    new_table = [250 if i == 0 else 0 for i in range(npal)]
    direct_remap = [0] * 32
    for k, pi in enumerate(chosen, start=1):
        new_table[k] = pi + 16
        direct_remap[pi] = k
    direct_remap[TRANSPARENT_NEWIDX] = 0

    # Re-map per-frame pixels
    new_frame_pixels = {}
    data_offsets = amiga_cost['frame_offsets_in_body']
    for fi, buf in frame_pixels.items():
        if fi >= len(data_offsets):
            continue
        new_frame_pixels[data_offsets[fi]] = remap_pixels(
            buf, direct_remap, palette, chosen, TRANSPARENT_NEWIDX)
    return new_table, new_frame_pixels


def patch_amiga_cost(cid, new_body, disk_trees: dict):
    """Replace the COST chunk body for cid in the in-memory disk tree.
    Caller is responsible for serializing + index-rebuild + write.

    `disk_trees` is a dict {disk_n: parsed Node}. Lazy-loads if missing.
    Returns (ok: bool, info: str). Any size is fine — serialize handles it.
    """
    from scumm_tree import find_lflf_for_room as _flfr, parse
    home_rid = cache.cost_home(cid)
    if home_rid is None:
        return False, 'no home_rid'
    home_disk = cache.data['rooms'][home_rid]['disk']
    if home_disk not in disk_trees:
        path = f'{SVM_DIR}/monkey2.{home_disk:03d}'
        if not os.path.exists(path):
            path = f'{AMIGA_DIR}/monkey2.{home_disk:03d}'
        plain = bytes(b ^ 0x69 for b in open(path, 'rb').read())
        disk_trees[home_disk] = parse(plain)
    tree = disk_trees[home_disk]
    lflf = _flfr(tree, home_rid)
    if lflf is None:
        return False, f'no LFLF for rid {home_rid}'
    # Match cid by ORDER within home costumes — pristine_cache stores the
    # expected order, and re-running keeps it stable.
    home = cache.room(home_rid)
    pristine_costs = home['costumes']
    cost_nodes = [c for c in lflf.children if c.tag == 'COST']
    target_node = None
    for pc, node in zip(pristine_costs, cost_nodes):
        if pc['cost_id'] == cid:
            target_node = node
            break
    if target_node is None:
        return False, f'cid {cid} not found in home LFLF (rid {home_rid})'
    target_node.body = new_body
    return True, f'cost in disk {home_disk} replaced ({len(new_body)} bytes)'


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    config = json.load(open(GROUPS_CONFIG))
    palette_data = json.load(open(GAP_PATH))
    if 'guybrush' not in palette_data:
        sys.exit('global_actor_palette.json missing guybrush — run encode_global_guybrush.py first')

    # Build the locks list for Guybrush from existing palette
    gp = palette_data['guybrush']
    guybrush_locks = []
    for slot_str, rgb in gp['canonical_amiga'].items():
        slot = int(slot_str)
        pi = slot - 16
        if 0 <= pi <= 31:
            guybrush_locks.append({
                'pi': pi,
                'rgb': f'{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}',
            })

    pc_data, pc_dcos, pc_room_off = load_pc_data()

    # Shared in-memory tree cache: every patch_amiga_cost call mutates
    # the appropriate disk's tree. After all groups are processed, we
    # serialize each modified tree once + rebuild the index once.
    disk_trees = {}

    groups_out = []
    for group in config['groups']:
        name = group['name']
        cids = group['cids']
        pal_indices = group['pal_indices']
        print(f'=== {name}: {len(cids)} cids, slots {pal_indices} ===')

        frames_dir = f'{WORKDIR}/{name}_frames'
        # Wipe any stale per-frame .idx so we never read prior-run bytes
        if os.path.isdir(frames_dir):
            for fn in os.listdir(frames_dir):
                if fn.endswith('.idx'):
                    os.unlink(os.path.join(frames_dir, fn))
        layout = render_group_frames(group, pc_data, pc_dcos, pc_room_off,
                                       frames_dir)
        if not layout:
            print(f'  no frames — skipping')
            continue
        print(f'  {len(layout)} frames staged in {frames_dir}/')

        # Optional per-group hand-tuned RGB locks
        # (cost_groups.json `lock_rgbs`: {"pi": "RRGGBB"}). Keys
        # starting with `_` are comment fields, skip them.
        lock_rgbs = {int(k): v for k, v in
                     (group.get('lock_rgbs') or {}).items()
                     if not k.startswith('_')}
        palette, s2 = quantize_group(layout, pal_indices, guybrush_locks,
                                      lock_rgbs)
        if lock_rgbs:
            print(f'    lock_rgbs: '
                  + ', '.join(f'pi[{pi}]=#{rgb}'
                              for pi, rgb in sorted(lock_rgbs.items())))
        if s2 is not None:
            print(f'    S2: {s2:.2f}  (group={name} slots={len(pal_indices)})')
        canonical = {}
        for pi in pal_indices:
            if palette[pi]:
                canonical[str(16 + pi)] = list(palette[pi])
                print(f'    [{pi:2d}] CLUT[{16+pi:2d}] = #{palette[pi][0]:02x}{palette[pi][1]:02x}{palette[pi][2]:02x}')

        # Render a quality-preview atlas from the per-frame .idx outputs
        # so preview/quality/global-<name>.png can show the actual
        # encoded result (matches build_quality_preview.py --globals).
        try:
            _build_group_preview(layout, palette,
                                  f'{WORKDIR}/{name}_quant.png')
        except Exception as e:
            print(f'  [warn] preview render skipped: {e}')

        # Re-encode each cid — per-frame .idx already on disk via --oe.
        # Shared Guybrush slots are valid pal_table targets too (locked
        # at room level + matching colours from cid 1), so character
        # pixels routed there carry through correctly.
        group_pal_indices_set = set(pal_indices)
        shared_set = {g['pi'] for g in guybrush_locks}
        patched = []
        failed = []
        for cid in cids:
            amiga_cost = next((c for c in cache.room(cache.cost_home(cid))['costumes']
                                if c['cost_id'] == cid), None)
            if amiga_cost is None:
                failed.append((cid, 'no amiga cache')); continue
            new_table, frame_remaps = remap_costume_to_group(
                amiga_cost, palette, layout, group_pal_indices_set,
                shared_set)
            if new_table is None:
                failed.append((cid, 'no pixel data')); continue
            new_body = rebuild_cost_body(amiga_cost['body'], new_table,
                                          frame_remaps, amiga_cost['npal'])
            ok, info = patch_amiga_cost(cid, new_body, disk_trees)
            if ok:
                patched.append((cid, info))
            else:
                failed.append((cid, info))
        print(f'  Patched: {len(patched)}, failed: {len(failed)}')
        for cid, msg in failed[:6]:
            print(f'    cid {cid}: {msg}')

        groups_out.append({
            'name': name,
            'cids': cids,
            'pal_indices': pal_indices,
            'canonical_amiga': canonical,
        })

    palette_data['groups'] = groups_out
    with open(GAP_PATH, 'w') as f:
        json.dump(palette_data, f, indent=2)
    print(f'\nWrote {GAP_PATH} (added {len(groups_out)} groups)')

    # Serialize every modified disk tree + rebuild the index in one pass.
    if disk_trees:
        from scumm_tree import (parse, parse_index, serialize,
                                  serialize_index)
        from scumm_index import rebuild_index, parse_droo
        XOR = 0x69
        # Load + serialize ALL disks (modified ones from in-memory tree;
        # unmodified ones loaded fresh) so the index sees current
        # positions for every entry.
        idx_path = f'{SVM_DIR}/monkey2.000'
        if not os.path.exists(idx_path):
            idx_path = f'{AMIGA_DIR}/monkey2.000'
        idx_plain = bytes(b ^ XOR for b in open(idx_path, 'rb').read())
        index_root = parse_index(idx_plain)
        droo = next(c for c in index_root.children if c.tag == 'DROO')
        _, droo_disks_list = parse_droo(droo.body)
        droo_disks = {rid: dn for rid, dn in enumerate(droo_disks_list)
                       if dn > 0}
        all_disks = set(droo_disks.values())
        full_trees = dict(disk_trees)
        positions = {}
        for d_n in all_disks:
            if d_n not in full_trees:
                p = f'{SVM_DIR}/monkey2.{d_n:03d}'
                if not os.path.exists(p):
                    p = f'{AMIGA_DIR}/monkey2.{d_n:03d}'
                full_trees[d_n] = parse(bytes(b ^ XOR
                                                for b in open(p, 'rb').read()))
            pos = {}
            out = serialize(full_trees[d_n], pos)
            positions[d_n] = pos
            # Only write disks we actually mutated (saves I/O for
            # unmodified ones).
            if d_n in disk_trees:
                with open(f'{SVM_DIR}/monkey2.{d_n:03d}', 'wb') as f:
                    f.write(bytes(b ^ XOR for b in out))
                print(f'  -> monkey2.{d_n:03d} ({len(out)} bytes)')

        rebuild_index(index_root, full_trees, positions, droo_disks)
        new_idx = serialize_index(index_root)
        with open(f'{SVM_DIR}/monkey2.000', 'wb') as f:
            f.write(bytes(b ^ XOR for b in new_idx))
        print(f'  -> monkey2.000 (index rebuilt, {len(new_idx)} bytes)')


if __name__ == '__main__':
    main()
