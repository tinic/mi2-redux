#!/usr/bin/env python3
"""Generalized room injector. Replaces the SMAP+CLUT for one room across the
ScummVM test dir.

Usage:
    inject_room.py <room_name>    # e.g. 'dinky-hol', 'bar', 'part1'

Pipeline:
  PC PNG (~/mi2-redux/extracted-pc-pngs/IMAGES/backgrounds/NNNN_<name>.png)
    -> png2amiga --best (32-color OCS, 2x preview render)
    -> NEAREST downsample to native width/height
    -> lock_slots (palette[0]=black, palette[17]=white)
    -> encode each strip via ZIGZAG_H5
    -> patch SMAP body + CLUT[16..47] in the room's monkey2.0NN file
    -> write the patched file into ~/mi2-redux/monkey2-hd/
"""
import os, sys, subprocess, struct, json
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (
    load, be32, le32, le16, name, walk_rooms, find_chunk,
)
from encode_amiga import encode_zigzag_h
from lock_palette_slots import lock_slots, oklab_dist
from inject_part1 import build_smap_body

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

PC_DIR    = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'
AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
SVM_DIR   = f'{REPO_ROOT}/monkey2-hd'
PNG2AMIGA = os.environ['PNG2AMIGA']    # set by build.sh / bootstrap.sh

# XOR=0x69 obfuscation table — bytes.translate() runs at C speed (~GB/s)
# vs the per-byte Python `b ^ 0x69 for b in raw` comprehension (~50 MB/s
# = ~16ms per 800 KB disk file). Each room reads/writes ~10 disks for
# the index rebuild, so this single-line change saves ~150ms per room.
XOR_TABLE = bytes(b ^ 0x69 for b in range(256))

# --best gates the population-search palette refinement. Slow (~couple
# of minutes per joint canvas with locks; ~30s without). Disable for
# quick iteration via BEST=0; default on for ship-quality builds.
BEST_FLAG = ['--best'] if os.environ.get('BEST', '1') != '0' else []
# VERBOSE=1: print each png2amiga subprocess command before running.
VERBOSE = os.environ.get('VERBOSE', '0') != '0'
# DITHER=<method>: override the dither method passed to every png2amiga
# call (e.g. DITHER=opt-checker). When unset, each call uses its current
# default (png2amiga's built-in default = floyd-steinberg, or explicit
# --dither none for structural fallback paths).
DITHER_ARGS = ['--dither', os.environ['DITHER']] if os.environ.get('DITHER') else ['--dither', 'opt-checker']


def _run_png2amiga(cmd, label='', **kwargs):
    """Run a png2amiga subprocess. If VERBOSE=1 in env, print the full
    command line first so it can be copy-pasted into a shell."""
    if VERBOSE:
        prefix = f"[png2amiga {label}] " if label else "[png2amiga] "
        print(prefix + ' '.join(repr(a) for a in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def find_pc_png(room_name):
    for f in sorted(os.listdir(PC_DIR)):
        if f.endswith('.png') and '_' in f and f.split('_', 1)[1].rsplit('.', 1)[0] == room_name:
            return os.path.join(PC_DIR, f)
    return None


def find_room_in_disk(room_name):
    """Returns (disk_num, room_id, room_index_in_file) for the named room."""
    # Parse index for room id
    from decode_all import parse_index_room_names

    names_map = parse_index_room_names(f'{AMIGA_DIR}/monkey2.000')
    rid = next((k for k, v in names_map.items() if v == room_name), None)
    if rid is None:
        raise SystemExit(f"unknown room name '{room_name}'")
    for disk in range(1, 12):
        p = f'{AMIGA_DIR}/monkey2.{disk:03d}'
        if not os.path.exists(p): continue
        d = load(p)
        try:
            rooms = list(walk_rooms(d))
        except Exception:
            continue
        for idx, (r_id, _) in enumerate(rooms):
            if r_id == rid:
                return disk, rid, idx
    raise SystemExit(f"room id {rid} not found in any disk")


def main():
    # Phase timing — set TIMING=1 to print elapsed seconds between
    # checkpoints to stderr. Useful for finding the per-room bottleneck
    # without slowing down the normal stdout summary the user reads.
    import time as _time
    _t0 = _time.perf_counter()
    _t_state = [_t0]  # list to make mutable in closure
    _phase_totals: dict[str, float] = {}
    def _t(label):
        if not os.environ.get('TIMING'):
            return
        now = _time.perf_counter()
        dt = now - _t_state[0]
        _phase_totals[label] = _phase_totals.get(label, 0.0) + dt
        print(f'[t] {label}: +{dt:.2f}s  (total {now - _t0:.2f}s)',
              file=sys.stderr)
        _t_state[0] = now

    def _t_summary():
        if not os.environ.get('TIMING'):
            return
        total = sum(_phase_totals.values())
        print(f'\n[t] phase totals (total {total:.2f}s):', file=sys.stderr)
        for label, dt in sorted(_phase_totals.items(), key=lambda x: -x[1]):
            pct = 100 * dt / total if total else 0
            print(f'[t]   {label:30s}  {dt:6.2f}s  ({pct:4.1f}%)',
                  file=sys.stderr)

    # Args: <room_name> [--shared-palette <json>]
    args = sys.argv[1:]
    shared_palette_path = None
    if '--shared-palette' in args:
        i = args.index('--shared-palette')
        shared_palette_path = args[i + 1]
        args = args[:i] + args[i + 2:]
    if len(args) != 1:
        print("Usage: inject_room.py <room_name> [--shared-palette <json>]")
        sys.exit(1)
    room_name = args[0]

    disk, rid, idx = find_room_in_disk(room_name)
    print(f"Room '{room_name}' = id {rid}, disk {disk}, index {idx}")

    pc_png = find_pc_png(room_name)
    if not pc_png:
        raise SystemExit(f"no PC PNG for '{room_name}'")
    print(f"  PC source: {pc_png}")

    # Load disk file. CRITICAL: prefer the previously-patched version in
    # monkey2-hd/ over pristine — multiple rooms may share a disk file
    # (e.g. disk 2 holds rooms 7..15), and reading from pristine would erase
    # every earlier patch on this disk. The room dimensions are still
    # correct because patches don't change the room's RMHD or layout-table
    # entries; they only grow the SMAP/CYCL chunks.
    pristine_disk = f'{AMIGA_DIR}/monkey2.{disk:03d}'
    patched_disk = f'{SVM_DIR}/monkey2.{disk:03d}'
    src_disk = patched_disk if os.path.exists(patched_disk) else pristine_disk
    d = bytearray(load(src_disk))
    rooms = list(walk_rooms(d))
    _, ro = rooms[idx]
    room_size = be32(d, ro+4)
    rmhd_off, _ = find_chunk(d, ro+8, ro+room_size, 'RMHD')
    w = le16(d, rmhd_off+8); h = le16(d, rmhd_off+10)
    print(f"  Amiga room: {w}x{h}")

    from PIL import Image
    from remap_costume_palette import find_lflf_for_room
    clut_off_orig, clut_sz_orig = find_chunk(d, ro+8, ro+room_size, 'CLUT')
    lflf_off = find_lflf_for_room(d, rid)

    # All pristine values come from the cache — no ad-hoc disk reads, no
    # chained-state corruption. Cache is built once from amiga-data/ via
    # tools/build_pristine_cache.py and lazily rebuilt if stale.
    from pristine_cache import cache
    pristine_room = cache.room(rid)
    if pristine_room is None:
        raise SystemExit(f"pristine_cache has no entry for room {rid}")
    pristine_cb = pristine_room['clut']
    orig_clut_for_swatch = [tuple(pristine_cb[i*3:i*3+3]) for i in range(256)]
    orig_clut = orig_clut_for_swatch  # alias — same source, single load

    # Global actor palette (Guybrush + extras groups). Loaded early so
    # the costume_data loop can skip globally-encoded cids.
    try:
        global_actor_palette = json.load(open(os.path.join(os.path.dirname(__file__),
                                                            'global_actor_palette.json')))
    except (FileNotFoundError, json.JSONDecodeError):
        global_actor_palette = {}

    pc_im = Image.open(pc_png).convert('RGB')
    bg_w_in, bg_h_in = pc_im.size
    from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf
    lflf_for_locks = find_lflf_for_room(d, rid)
    # Selective-lock candidates are derived from the pristine costume
    # pal_tables (cache). Reading from chained `d` would feed back any
    # previously re-encoded pal_table values.
    costume_slots = set()
    for c in pristine_room['costumes']:
        for ci in c['pal_table']:
            if 16 <= ci <= 47:
                costume_slots.add(ci)
    # Slot 17 is special: ScummVM's MI2 Amiga path hardcodes _roomPalette[33]=0
    # (palette.cpp:457-466), so SMAP value 17 always renders as palette[0] (black)
    # in the bg. Verb/UI path is unaffected. We use --reserve-range for slot 17 so
    # CLUT[33] stays white (text/UI still works) but no bg pixel can route there.
    # Slot 0 is intentionally NOT locked: input PNGs (cost atlas, OBIM
    # atlas, joint canvas) carry alpha-channel transparency, png2amiga
    # auto-routes alpha=0 pixels to slot 0, and assigns slot 0's colour
    # from whatever the alpha-fill resolves to (typically 000000), matching
    # the pristine Amiga's left-edge non-room behaviour.
    lock_args = []
    if shared_palette_path:
        # Family-wide shared palette: lock every slot to the supplied colour.
        # Slot 17 is special — use --reserve-range so image pixels can't route
        # there (ScummVM SMAP-17→black bug). All other 31 slots: --lock-index.
        sp = json.loads(open(shared_palette_path).read())
        for entry in sp['palette']:
            idx = entry['idx']; rgb = entry['rgb']
            if idx == 17:
                lock_args.extend(['--reserve-range', '17', rgb])
            else:
                lock_args.extend(['--lock-index', str(idx), rgb])
        print(f"  Shared palette: {shared_palette_path} (32 slots locked)")
    else:
        # Per-room locks. Two-pass selective costume locking:
        #   pass 1: --best with HW-sprite lock only (slot 1) + reserve 17
        #   measure each costume CLUT-slot colour vs new palette; lock only the
        #   ones the free quantizer mismatches by > MISMATCH_THRESHOLD (OKLab).
        #   pass 2: re-run --best with selective locks added.
        # Result: bg quality jumps (more free slots) while costume colours that
        # actually need preservation still get exactly preserved.
        # MI2's cursor is a single-colour OCS sprite — only CLUT[17] (= palette
        # index 1) is the actual sprite colour. CLUT[18..19] are sprite colours
        # 2 and 3 of a 4-colour sprite that MI2 doesn't use, so they're free
        # for the quantizer.
        base_pal_indices = {1}
        for pi in sorted(base_pal_indices):
            r, g, b = orig_clut[pi + 16]
            lock_args.extend(['--lock-index', str(pi), f'{r:02X}{g:02X}{b:02X}'])
        # Slot 17 = white, but RESERVED (image pixels banned) to avoid the ScummVM bg-black bug.
        r17, g17, b17 = orig_clut[33]
        lock_args.extend(['--reserve-range', '17', f'{r17:02X}{g17:02X}{b17:02X}'])
        # Costume-lock CANDIDATES — pass 1 picks a subset.
        costume_lock_candidates = []   # [(palette_idx, hex), ...]
        for cs in sorted(costume_slots):
            if 16 <= cs <= 47 and cs != 33:
                pi = cs - 16
                if pi in base_pal_indices:
                    continue
                r, g, b = orig_clut[cs]
                costume_lock_candidates.append((pi, f'{r:02X}{g:02X}{b:02X}'))
    # Per-room overrides may add extra locks (see tools/room_specials.py).
    import room_specials
    override = room_specials.get(room_name)
    override_extra_locks = []
    if override and override.extra_locks:
        for extra in override.extra_locks(orig_clut):
            override_extra_locks.extend(extra)
        print(f"  Per-room override: appended {len(override_extra_locks)//3} extra lock arg group(s)")
    lock_args.extend(override_extra_locks)

    # Build OBIM atlas (PyTexturePacker) so the joint --best palette covers
    # object pixel colours at their natural frequency.
    sys.path.insert(0, f'{REPO_ROOT}/tools/PyTexturePacker')
    from PyTexturePacker import Packer
    import glob, shutil
    workdir = f'{REPO_ROOT}/preview/intermediates/{room_name}'
    # Wipe any stale files from a prior run so we never accidentally consume
    # outputs that are out-of-date relative to current inputs.
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    _t('setup (cache+locks)')
    obim_pngs = sorted(glob.glob(f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/objects/{rid:04d}_*.png'))
    atlas_png = None
    atlas_layout = None
    if obim_pngs:
        atlas_base = f'{workdir}/obim_atlas'
        # Transparent-bg atlas: source OBIM PNGs carry tRNS chunks pointing
        # at each room's TRNS sentinel index (extract_pc_pngs.py writes
        # them), so PIL loads them as RGBA with alpha=0 for transparent
        # pixels. The packer preserves alpha; png2amiga reads alpha=0
        # pixels as "route to slot 0" automatically. No --transparent-color
        # / no magenta-RGB scanning needed.
        packer = Packer.create(max_width=2048, max_height=2048, bg_color=0x00000000,
                                enable_rotated=False, force_square=False,
                                atlas_format='json', inner_padding=0,
                                border_padding=0, shape_padding=0)
        packer.pack(obim_pngs, atlas_base)
        atlas_png = atlas_base + '.png'
        with open(atlas_base + '.json') as f:
            atlas_layout = json.load(f)
        print(f"  OBIM atlas: {Image.open(atlas_png).size}")
    _t('obim_atlas_pack')

    # Build COSTUME atlas: render every LFLF-hosted costume's frames using
    # the PC CLUT (high-fidelity source colours), then feed into joint --best
    # so the costume's pixels participate in the 32-colour quantisation. After
    # --best we crop each frame back, build a per-costume new palette table,
    # re-encode RLE, and write a new COST chunk in place.
    #
    # Skip costumes whose palette table doesn't reference CLUT[16..47] (the
    # active art-palette range we modify) — those costumes (e.g. Guybrush
    # at CLUT[192..207]) render unchanged across our patches.
    # Build re-encodable costume list directly from cache. cost_offsets
    # in chained `d` are needed to write back our results; cache holds
    # pristine bodies, frames, pal_tables.
    # Build the set of cids that are already globally re-encoded (Guybrush
    # via encode_global_guybrush.py + every group cid via
    # encode_global_extras.py). Their COST chunks have already been patched
    # in their home disk; the per-room re-encode path here MUST skip them
    # or it would overwrite the global encoding with a fresh per-room one.
    globally_encoded_cids = {1}   # Guybrush
    for grp in global_actor_palette.get('groups', []):
        globally_encoded_cids.update(grp.get('cids', []))

    costume_data = []  # per re-encodable costume
    if lflf_for_locks is not None and pristine_room['costumes']:
        cost_offsets = find_costumes_in_lflf(d, lflf_for_locks)
        if len(cost_offsets) != len(pristine_room['costumes']):
            print(f"  [warn] cost-offset count mismatch pristine="
                  f"{len(pristine_room['costumes'])} patched={len(cost_offsets)}"
                  f" — costume re-encode disabled for this room")
            cost_offsets = []
        for i, (cost_off, pc) in enumerate(
                zip(cost_offsets, pristine_room['costumes'])):
            if pc['cost_id'] == 0:
                continue   # orphan COST in LFLF that DCOS doesn't reference
            if pc['fmt'] not in (0x58, 0x59):
                continue
            if pc['cost_id'] in globally_encoded_cids:
                continue   # already encoded globally — DON'T overwrite
            pal_table = list(pc['pal_table'])
            non_trans_used = [v for v in pal_table
                              if v != 0 and v != 250]  # 250 = SCUMM "no-op"
            if not non_trans_used:
                continue
            # Include EVERY home-LFLF costume — including those whose
            # pal_table touches [192..207]. Render through the pristine
            # CLUT (which has correct colours at every slot the artist
            # set up for this scene) so --best sees the costume's true
            # appearance, then after --best re-encode the pal_table to
            # use only [16..47] slots. This handles characters drawn
            # only in their host room (per descumm drawn_in) without
            # cross-room consistency issues.
            cost_sz_ = be32(d, cost_off + 4)
            frames = [(f['w'], f['h'], f['pixels']) for f in pc['frames']]
            costume_data.append({
                'cost_of': cost_off, 'cost_sz': cost_sz_, 'cid': pc['cost_id'],
                'frames': frames, 'pal_table': pal_table,
                'body': pc['body'], 'fmt': pc['fmt'], 'npal': pc['npal'],
                'frame_offsets_in_body': pc['frame_offsets_in_body'],
            })
    if costume_data:
        print(f"  Costumes to re-encode: {len(costume_data)} home-LFLF costume(s) "
              f"(including out-of-range pal_tables — re-encoded into [16..47])")

    # ---- Foreign costumes (drawn here from another LFLF) ----------------
    # On real Amiga every visible pixel must come from CLUT[16..47]. Cross-
    # room costumes (mainly Guybrush, cid 1, drawn in 38 rooms) are hosted
    # in their home LFLF and their pal_table values 192..207 truncate via
    # 5-bit OCS hardware to slots 16..31. Include their frames in the joint
    # canvas so --best optimizes for those pixels too. Render via:
    #   - Guybrush (cid 1): the canonical "global actor palette" so colours
    #     stay consistent across every room he walks through.
    #   - Other multi-room costumes: their home room's pristine CLUT (best
    #     guess at what the artist wanted them to look like).
    try:
        cross_refs_for_canvas = json.load(open(os.path.join(os.path.dirname(__file__),
                                                            'costume_refs.json')))
    except (FileNotFoundError, json.JSONDecodeError):
        cross_refs_for_canvas = {'drawn_in': {}, 'cost_home': {}}
    # global_actor_palette already loaded earlier — no-op here (kept for
    # readability of where canvas/lock paths use it).
    # Cross-room costumes (Guybrush, cid 1) are NOT included in the joint
    # canvas. He's globally pre-encoded once via tools/encode_global_guybrush.py
    # at palette indices 22..31 (CLUT[38..47]) and patched into his home
    # COST chunk. Each Guybrush room just locks those 10 slots — see the
    # `lock_args` setup below. We never re-render or re-encode him here.
    foreign_frames = []
    cross_drawn_for_canvas = cross_refs_for_canvas.get('drawn_in', {})
    cost_home_for_canvas = cross_refs_for_canvas.get('cost_home', {})

    cost_atlas_im = None
    cost_layout = []  # list of (cost_data_idx, frame_idx, ax, ay, w, h) — local re-encode targets
    if costume_data or foreign_frames:
        # Reuse the pristine CLUT bytes loaded at top of main() for local
        # costumes — same source used for orig_clut / orig_clut_for_swatch.
        clut_for_render = pristine_cb
        all_frames = []
        for ci, cd in enumerate(costume_data):
            for fi, f in enumerate(cd['frames']):
                all_frames.append(('local', ci, fi, f[0], f[1], f[2],
                                    cd['pal_table'], clut_for_render))
        for fi, (w_f, h_f, pix_f, pal_f, clut_f, _cid_f) in enumerate(foreign_frames):
            all_frames.append(('foreign', fi, fi, w_f, h_f, pix_f,
                                pal_f, clut_f))

        # Tight pack: render each frame as its own PNG, run PyTexturePacker
        # to produce the cost_atlas. Naive grid packing wastes ~80% of the
        # canvas as magenta padding (max-cell-size * count vs total frame
        # area), and png2amiga --best is roughly proportional to canvas
        # pixels so the wasted area dominates encoding time.
        frames_dir = f'{workdir}/cost_frames'
        if os.path.exists(frames_dir):
            for f in os.listdir(frames_dir):
                os.remove(os.path.join(frames_dir, f))
        os.makedirs(frames_dir, exist_ok=True)
        frame_paths = []
        path_to_frame = {}
        # Match the OBIM extractor's blunt magenta-RGB strip rule
        # (extract_pc_pngs.save_paletted_png): any CLUT slot whose RGB
        # is magenta-family is treated as transparent. Costume pal_tables
        # commonly include the room's TRNS-magenta slot for the costume's
        # "transparent" idx, but ALSO sometimes route real opaque pixels
        # at non-zero idx through magenta CLUT entries — those would
        # render as visible magenta in our preview AND inflate the joint
        # palette training. Strip them to alpha=0 to match the OBIM rule.
        #
        # Per-costume allow-list (`MAGENTA_KEEP_PER_COSTUME`) overrides
        # the strip for specific (rid, cid) entries — used when a
        # costume legitimately uses magenta as foreground colour
        # (e.g. dinky-hol's magenta-shirted character). Because
        # png2amiga's internal alpha-premultiply (v1.82+) zeroes the
        # RGB of alpha=0 inputs before training, kept magenta pixels
        # don't leak into adjacent slots via dither bleed.
        # Master list of (rid, cid) -> {pal_table indices to keep
        # opaque despite mapping to magenta-family CLUT slots}.
        # Surveyed via tools/find_magenta_costumes.py and validated
        # visually frame-by-frame.
        MAGENTA_KEEP_PER_COSTUME = {
            (5,   32):  {12},     # campfire
            (27,  78):  {6},      # wharf
            (46,  82):  {11},     # ville
            (48,  39):  {9},      # antique
            (51,  51):  {9},      # kiosk
            (52,  68):  {9},      # mansion
            (53,  11):  {10, 11}, # front-man
            (56,  10):  {6},      # boudoir
            (57,   4):  {4},      # kitchen
            (57,  45):  {10},     # kitchen
            (63,  62):  {12},     # under-shi
            (66, 112):  {1},      # galleon
            (81, 167):  {6},      # kates-shi
            (85, 169):  {9, 10},  # dinky-bea
            (87, 103):  {1, 15},  # dinky-hol (magenta cape)
            (93, 155):  {18},     # undergrou
            (110, 168): {6},      # bigwhoop (Elaine blouse)
        }
        room_keep = {ck: pt_indices for (ck_rid, ck), pt_indices
                     in MAGENTA_KEEP_PER_COSTUME.items() if ck_rid == rid}
        def _is_magenta_clut_slot(clut_x, slot):
            r, g, b = clut_x[slot*3], clut_x[slot*3+1], clut_x[slot*3+2]
            return (r >= 0x80 and b >= 0x80 and g <= 0x60
                    and abs(r - b) <= 0x30)
        for k, (kind, ci, fi, w_, h_, pixels, pal_t, clut_x) in enumerate(all_frames):
            # Look up the per-costume allow-list for this frame.
            keep_pt_indices = set()
            if kind == 'local':
                cid = costume_data[ci]['cid']
                keep_pt_indices = room_keep.get(cid, set())
            im = Image.new('RGBA', (w_, h_), (0, 0, 0, 0))
            ipx = im.load()
            for yy in range(h_):
                for xx in range(w_):
                    idx = pixels[yy * w_ + xx]
                    if idx == 0 or idx >= len(pal_t):
                        continue   # SCUMM-defined transparent → alpha=0
                    slot = pal_t[idx]
                    if (_is_magenta_clut_slot(clut_x, slot)
                            and idx not in keep_pt_indices):
                        continue   # magenta-family CLUT → alpha=0
                    ipx[xx, yy] = (clut_x[slot * 3],
                                   clut_x[slot * 3 + 1],
                                   clut_x[slot * 3 + 2],
                                   255)
            fname = f'f{k:04d}.png'
            im.save(f'{frames_dir}/{fname}')
            frame_paths.append(f'{frames_dir}/{fname}')
            path_to_frame[fname] = (kind, ci, fi, w_, h_)

        atlas_base = f'{workdir}/cost_atlas'
        for f in glob.glob(atlas_base + '*'):
            if os.path.isfile(f):
                os.remove(f)
        # Transparent atlas — frames are RGBA with alpha=0 for the
        # SCUMM-defined transparent pixels (idx==0 in the COST stream).
        # png2amiga reads alpha=0 → slot 0 routing automatically.
        packer = Packer.create(max_width=2048, max_height=2048,
                                bg_color=0x00000000,
                                enable_rotated=False, force_square=False,
                                atlas_format='json', inner_padding=0,
                                border_padding=0, shape_padding=0)
        packer.pack(frame_paths, atlas_base)
        cost_atlas_im = Image.open(f'{atlas_base}.png').convert('RGBA')
        with open(f'{atlas_base}.json') as f:
            packed = json.load(f)
        # PyTexturePacker JSON: {'frames': {fname: {'frame': {x,y,w,h}, ...}}}
        for fname, entry in packed['frames'].items():
            kind, ci, fi, w_, h_ = path_to_frame[fname]
            fr = entry['frame']
            ax, ay = fr['x'], fr['y']
            if kind == 'local':
                cost_layout.append((ci, fi, ax, ay, w_, h_))
        # Re-save as cost_atlas.png at workdir root for downstream
        cost_atlas_im.save(f'{workdir}/cost_atlas.png')
        n_local = sum(1 for f in all_frames if f[0] == 'local')
        n_foreign = sum(1 for f in all_frames if f[0] == 'foreign')
        used = sum(w_ * h_ for _, _, _, w_, h_ in path_to_frame.values())
        canvas_px = cost_atlas_im.size[0] * cost_atlas_im.size[1]
        eff = used / canvas_px * 100 if canvas_px else 0
        print(f"  Costume atlas: {cost_atlas_im.size}, {n_local} local + "
              f"{n_foreign} foreign frames; pack efficiency {eff:.1f}%")
    _t('cost_atlas_render+pack')

    # NOTE: inventory icons (rid 94) have their own copper list at runtime
    # (verified via UAE debugger) — they don't share the current room's
    # CLUT[16..47]. So we DON'T need to include them in the joint canvas.
    # Earlier attempts to do so just leaked the icons sheet's blue
    # transparent-background colour into the bg quantizer.

    # ---- TalkColor swatches ---------------------------------------------
    # ScummVM renders dialogue text using CLUT[N] for each actor's
    # TalkColor. talk_colors_survey.json lists every literal scraped via
    # descumm per room. Add a small RGB tile (8x8) per TalkColor so those
    # colours participate in --best naturally. The user accepts an
    # approximate match — the original Amiga port itself sometimes shows
    # weird text colours that don't exactly match the PC.
    talk_swatch_im = None
    talk_slots_for_room = []
    try:
        talk_refs = json.load(open(os.path.join(os.path.dirname(__file__),
                                                'talk_colors_survey.json')))
        talk_slots_for_room = [s for s in talk_refs.get('per_room', {}).get(str(rid), [])
                               if 0 <= s < 256]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if talk_slots_for_room:
        SW = 16  # swatch tile size — large enough that --best weighs them
        cols = min(8, len(talk_slots_for_room))
        rows = (len(talk_slots_for_room) + cols - 1) // cols
        talk_swatch_im = Image.new('RGB', (cols * SW, rows * SW), (0xFF, 0x00, 0xFF))
        tpx = talk_swatch_im.load()
        for k, slot in enumerate(talk_slots_for_room):
            r_, g_, b_ = orig_clut[slot]
            cx = (k % cols) * SW
            cy = (k // cols) * SW
            for yy in range(SW):
                for xx in range(SW):
                    tpx[cx + xx, cy + yy] = (r_, g_, b_)
        talk_swatch_im.save(f'{workdir}/talk_swatches.png')
        print(f"  TalkColor swatches: {len(talk_slots_for_room)} slot(s) "
              f"-> {talk_swatch_im.size} ({sorted(talk_slots_for_room)})")
    _t('talk_swatches')

    # Multi-input joint palette training (png2amiga v1.78 --joint-input
    # / --output-each). Each region — bg, OBIM atlas, cost atlas, talk
    # swatches — is passed as a separate input. png2amiga trains ONE
    # shared 32-colour palette on the union, then writes a per-input
    # indexed-bytes file. Replaces the prior PyTexturePacker joint atlas
    # + crop-by-bbox dance entirely.
    bg_png_path = f'{workdir}/bg.png'
    pc_im.save(bg_png_path)
    pack_inputs = [bg_png_path]
    if atlas_png is not None:
        pack_inputs.append(atlas_png)
    if cost_atlas_im is not None:
        pack_inputs.append(f'{workdir}/cost_atlas.png')
    if talk_swatch_im is not None:
        pack_inputs.append(f'{workdir}/talk_swatches.png')

    parts = ['bg']
    if atlas_png: parts.append('atlas')
    if cost_atlas_im: parts.append('cost_atlas')
    if talk_swatch_im: parts.append('talk_swatches')
    print(f"  Joint inputs: {len(pack_inputs)} ({' + '.join(parts)})")

    # Per-input output paths produced by --oe '{dir}/{stem}.idx'.
    bg_idx_path = f'{workdir}/bg.idx'
    cost_atlas_idx_path = (f'{workdir}/cost_atlas.idx'
                           if cost_atlas_im is not None else None)
    obim_atlas_idx_path = (atlas_png[:-4] + '.idx'
                           if atlas_png is not None else None)

    def _run_best(extra_locks):
        all_locks = list(lock_args) + list(extra_locks)
        cmd = [PNG2AMIGA, '--mode', 'lores', '--depth', '5', *BEST_FLAG,
                '--dither-strength', '0.8',
                '--no-scale', '--print-palette-json', *DITHER_ARGS]
        if len(pack_inputs) > 1:
            # Multi-input mode: --ji for each non-positional, --oe for
            # per-input output paths. png2amiga writes bg.idx, etc.
            for path in pack_inputs[1:]:
                cmd += ['--ji', path]
            cmd += ['--oe', '{dir}/{stem}.idx']
        else:
            # Single-input (bg-only) room — --oe doesn't fire without
            # --ji, so use --output-indexed for bg.idx directly.
            cmd += ['--output-indexed', bg_idx_path]
        cmd += [*all_locks, pack_inputs[0]]
        with open(f'{workdir}/cmd_joint_best.sh', 'w') as f:
            f.write('#!/bin/sh\n' + ' '.join(repr(a) for a in cmd) + '\n')
        r = _run_png2amiga(cmd, label='joint --best')
        bib = open(bg_idx_path, 'rb').read()
        if len(bib) != bg_w_in * bg_h_in:
            raise RuntimeError(
                f'bg.idx byte count {len(bib)} != {bg_w_in*bg_h_in}\n'
                f'stdout tail: {r.stdout[-400:]!r}\n'
                f'stderr: {r.stderr[:400]!r}')
        return r, bg_idx_path, bib

    def _parse_palette(stdout, stderr=''):
        # The palette JSON is one line in stdout, possibly mixed with "Encoded
        # ... S2: NN" status lines (when --quiet is off so we can capture S2).
        json_line = next((ln for ln in stdout.splitlines()
                          if ln.startswith('{')), None)
        if json_line is None:
            raise RuntimeError(
                f"no palette JSON in png2amiga stdout:\n--- STDOUT ---\n{stdout}"
                f"\n--- STDERR ---\n{stderr}")
        pj = json.loads(json_line)
        pal = [None] * 32
        for e in pj['palette']:
            h_ = e['rgb']
            pal[e['idx']] = (int(h_[0:2], 16), int(h_[2:4], 16), int(h_[4:6], 16))
        return pj, pal

    def _parse_s2(stdout):
        for line in stdout.splitlines():
            if 'S2:' in line:
                try:
                    return float(line.split('S2:')[1].split()[0])
                except (ValueError, IndexError):
                    return None
        return None

    if shared_palette_path:
        # Family-wide palette — no two-pass logic.
        r, best_png, bg_indexed_input = _run_best([])
        palette_dump, palette = _parse_palette(r.stdout, r.stderr)
    else:
        # Re-encodable LOCAL costumes participate directly in the joint
        # canvas (cost_atlas) — their pixels feed --best so the optimizer
        # naturally allocates slots for them. We DON'T lock their pristine
        # pal_table colours: build_new_palette_table picks new pal_table
        # values from whatever --best produces. Forcing the old slot RGBs
        # would over-constrain bg quality without any rendering benefit
        # (the costume re-encode tolerates fresh colour assignments).
        forced_costume_locks = []

        # Global Guybrush sub-palette FIRST: encoded once at palette
        # indices 22..31 (= CLUT slots 38..47 after paletteMod=16). His
        # pre-patched COST chunk reads those exact RGBs.
        # We lock UNCONDITIONALLY — descumm's drawn_in graph misses
        # rooms where Guybrush is loaded via Var or dynamic costume
        # assignment (e.g. casino looked Guybrush-free per descumm but
        # actually shows him). Locking everywhere costs 10 slots of bg
        # flexibility in truly Guybrush-free rooms; correct rendering
        # everywhere is worth the trade. We add Guybrush before
        # TalkColor / others so the dedup pass keeps his RGBs.
        actor_entry = global_actor_palette.get('guybrush')
        guybrush_pi_range = set()
        if actor_entry is not None:
            for slot_str, rgb in actor_entry['canonical_amiga'].items():
                slot = int(slot_str)
                pi = slot - 16
                if pi < 0 or pi > 31:
                    continue
                # Skip palette indices already locked by base or reserved.
                # palette[1] is the HW cursor lock; palette[17] is the
                # SMAP-17→black workaround reserve. Adding a Guybrush
                # forced-lock at either would emit `--lock-index` twice
                # for the same slot, which png2amiga rejects.
                if pi in base_pal_indices or pi == 17:
                    continue
                r_, g_, b_ = rgb
                forced_costume_locks.append(['--lock-index', str(pi),
                                              f'{r_:02X}{g_:02X}{b_:02X}'])
                guybrush_pi_range.add(pi)
            if guybrush_pi_range:
                print(f"  Global Guybrush sub-palette: "
                      f"locking palette[{actor_entry.get('pal_index_start', 22)}.."
                      f"{actor_entry.get('pal_index_end', 31)}] "
                      f"({len(guybrush_pi_range)} slots)")

        # ---- Group-based extras locks ---------------------------------
        # For each multi-room non-Guybrush group in global_actor_palette.json,
        # check whether ANY of its cids is drawn in THIS room (per
        # costume_refs.json). If yes, lock that group's pal_indices to its
        # canonical_amiga RGBs. Multiple subgroups may share pal_indices
        # (e.g., extras_a_green + extras_a_warm both at [4..7]) — by graph
        # coloring at most one of them has a drawn cid in any given room,
        # so the locks don't conflict.
        # Run BEFORE the TalkColor block so the TalkColor logic skips
        # any slot already claimed by a group (= group wins, text drifts).
        extras_pi_locked = set()
        try:
            extras_groups = global_actor_palette.get('groups', [])
            cross_drawn = cross_refs_for_canvas.get('drawn_in', {})
            n_groups_locked = 0
            for grp in extras_groups:
                cids_in_grp = set(grp.get('cids', []))
                drawn_here = [c for c in cids_in_grp
                              if rid in cross_drawn.get(str(c), [])]
                if not drawn_here:
                    continue
                pal_indices = grp.get('pal_indices', [])
                canon = grp.get('canonical_amiga', {})
                if not pal_indices or not canon:
                    continue
                for slot_str, rgb in canon.items():
                    slot = int(slot_str)
                    pi = slot - 16
                    if pi < 0 or pi > 31 or pi in (1, 2, 3) or pi == 17:
                        continue
                    if pi in guybrush_pi_range:
                        continue
                    forced_costume_locks.append(['--lock-index', str(pi),
                                                  f'{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}'])
                    extras_pi_locked.add(pi)
                n_groups_locked += 1
                print(f"  Group {grp['name']}: drawn cids {drawn_here} -> "
                      f"locking palette{pal_indices}")
            if n_groups_locked:
                print(f"  Total extras groups locked: {n_groups_locked}")
        except Exception as e:
            print(f"  [warn] extras-group lock failed: {e}")

        # TalkColor locks: ScummVM renders actor dialogue using CLUT[N]
        # where N is the actor's TalkColor. Talk colours in our managed
        # [16..47] range get locked unless they collide with Guybrush's
        # reserved [22..31] range or an extras-group range — those take
        # priority and the TalkColor patcher redirects dialogue literals
        # to the closest available slot post-quantization.
        try:
            talk_refs = json.load(open(os.path.join(os.path.dirname(__file__),
                                                    'talk_colors_survey.json')))
            room_talk = talk_refs.get('per_room', {}).get(str(rid), [])
            n_talk_locked = 0
            n_talk_skipped = 0
            for slot in room_talk:
                if 16 <= slot <= 47 and slot != 33:
                    pi = slot - 16
                    if pi == 1:    # cursor sprite colour, locked
                        continue
                    if pi in guybrush_pi_range or pi in extras_pi_locked:
                        n_talk_skipped += 1
                        continue
                    if any(int(grp[1]) == pi for grp in forced_costume_locks):
                        continue
                    r_, g_, b_ = orig_clut[slot]
                    forced_costume_locks.append(['--lock-index', str(pi),
                                                  f'{r_:02X}{g_:02X}{b_:02X}'])
                    n_talk_locked += 1
            if n_talk_locked or n_talk_skipped:
                msg = f"  TalkColor locks: {n_talk_locked} added"
                if n_talk_skipped:
                    msg += f", {n_talk_skipped} skipped (Guybrush/group owns those slots)"
                print(msg)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Dedupe forced locks (same pi may appear from multiple costumes).
        # Store as int for consistent membership checks against
        # costume_lock_candidates (which uses int pi).
        seen_pi = set()
        forced_unique = []
        for grp in forced_costume_locks:
            pi_int = int(grp[1])
            if pi_int in seen_pi: continue
            seen_pi.add(pi_int)
            forced_unique.append(grp)
        forced_costume_locks = forced_unique
        if forced_costume_locks:
            print(f"  Forced locks for {len(costume_data)} re-encodable costume(s): "
                  f"{len(forced_costume_locks)} extra slot(s) ({sorted(int(g[1]) + 16 for g in forced_costume_locks)})")

        # Pass 1: base locks + forced costume locks. Identify any remaining
        # costume candidates (from non-re-encoded LFLF costumes) above the
        # selective threshold.
        forced_extra = [a for grp in forced_costume_locks for a in grp]
        r, best_png, bg_indexed_input = _run_best(forced_extra)
        palette_dump, palette = _parse_palette(r.stdout, r.stderr)
        MISMATCH_THRESHOLD = 0.05  # OKLab; values above this are visibly wrong
        selective = []
        already_locked_pi = {1} | seen_pi
        worst_dist = 0.0
        for pi, hex_ in costume_lock_candidates:
            if pi in already_locked_pi:
                continue
            cs = pi + 16
            target = orig_clut[cs]
            mm = min(oklab_dist(target, p) for p in palette)
            if mm > worst_dist:
                worst_dist = mm
            if mm > MISMATCH_THRESHOLD:
                selective.append(['--lock-index', str(pi), hex_])
        if selective:
            print(f"  Pass 2: {len(selective)} additional non-re-encode costume "
                  f"locks needed (worst free mismatch={worst_dist:.4f}); re-running")
            extra = forced_extra + [a for grp in selective for a in grp]
            r, best_png, bg_indexed_input = _run_best(extra)
            palette_dump, palette = _parse_palette(r.stdout, r.stderr)
        else:
            print(f"  Pass 1 sufficient (worst remaining costume mismatch="
                  f"{worst_dist:.4f}, threshold={MISMATCH_THRESHOLD})")

    _t('png2amiga_best')
    joint_s2 = _parse_s2(r.stdout)  # may include atlas region if present
    n_locked = sum(1 for e in palette_dump['palette'] if e['locked'])

    # Persist the final 32-color palette to the workdir so post-process
    # tools (e.g. tools/build_quality_preview.py) can render the .idx
    # outputs faithfully — bg.png / obim_atlas.png / cost_atlas.png in
    # this dir are the *source* PC images written before --best, not the
    # final dithered/quantised result.
    with open(f'{workdir}/palette.json', 'w') as _f:
        json.dump(palette_dump, _f, separators=(',', ':'))

    # ---- COST re-encode -------------------------------------------------
    # For each LFLF-hosted costume that participated in the joint --best,
    # crop its frames out of the indexed output, build a new palette table
    # using the most-common new-palette indices, re-encode RLE in Amiga
    # row-major scan order, and patch the COST chunk in `d` (in-memory).
    # If the new RLE doesn't fit in the original frame's footprint, we
    # leave the costume untouched and fall back to colour-table remap
    # (handled later by patch_lflf_costumes).
    #
    # Cost re-encode runs png2amiga TWICE on the cost_atlas with the final
    # palette: once dithered (default Floyd-Steinberg) for visual quality,
    # once with --dither none for cases where the dithered RLE overflows
    # SCUMM's 16-bit baseptr addressing limit (~64KB per COST body). For
    # each costume we try the dithered build first; if it overflows, fall
    # back to the non-dithered version. Tree-rebuild handles size growth
    # within the 16-bit limit; beyond it the format simply can't address
    # the frame data and we MUST take the no-dither hit.
    requantized_costume_positions = set()  # LFLF-relative positions written back
    cost_grow_patches = []  # patch entries for re-encoded COST chunks (may grow)
    if costume_data and cost_atlas_im is not None:
        from encode_cost import encode_rle, build_new_palette_table, rebuild_cost_body
        cost_atlas_path = f'{workdir}/cost_atlas.png'
        locks_all = []
        for i, c in enumerate(palette):
            rgb = f'{c[0]:02x}{c[1]:02x}{c[2]:02x}'
            if i == 17:
                locks_all += ['--reserve-range', '17', rgb]
            else:
                locks_all += ['--lock-index', str(i), rgb]

        # Dithered cost atlas indexed bytes come for free from the joint
        # pass (cost_atlas.idx, written by --oe). For the 16-bit RLE
        # overflow fallback we still need a non-dithered version — single
        # png2amiga call with --palette pal.json (v1.73) + --dither none.
        cost_palette_json = f'{workdir}/cost_atlas_palette.json'
        with open(cost_palette_json, 'w') as f:
            json.dump(palette_dump, f, separators=(',', ':'))

        # Stepped dither-strength re-quant. When the joint --best pass
        # (strength 0.8 by default) produces RLE that overflows SCUMM's
        # 16-bit COST baseptr, we re-quantize at progressively lower
        # strengths until it fits, instead of jumping straight to
        # --dither none. The retry keeps as much dither as possible
        # while still fitting — the 0.0 step is the same as the old
        # nn fallback.
        _strength_cache = {}
        def _quant_cost_atlas_at_strength(strength):
            """Re-quantize cost_atlas.png at the given dither strength
            (0.0 → no dither, same as the old nn path). Output cached
            so multiple costs in the same room reuse the .idx."""
            if strength in _strength_cache:
                return _strength_cache[strength]
            tag = f's{int(round(strength * 10)):02d}'
            idx_path = f'{workdir}/cost_atlas_{tag}.idx'
            png_path = f'{workdir}/cost_atlas_{tag}.png'
            for p in (idx_path, png_path):
                if os.path.exists(p):
                    os.unlink(p)
            if strength <= 0.0:
                dither_args = ['--dither', 'none']
            else:
                d = os.environ.get('DITHER') or 'opt-checker'
                dither_args = ['--dither', d,
                               '--dither-strength', f'{strength:.1f}']
            cmd = [PNG2AMIGA, '--mode', 'lores', '--depth', '5',
                   '--no-scale', *dither_args,
                   '--palette', cost_palette_json,
                   '--output-indexed', idx_path,
                   cost_atlas_path, '-o', png_path]
            rcr = _run_png2amiga(cmd, label=f'cost re-quant ({tag})')
            if not os.path.exists(idx_path):
                with open(f'{workdir}/cmd_cost_quant_{tag}.sh', 'w') as f:
                    f.write(' '.join(repr(a) for a in cmd) + '\n')
                tail = (rcr.stderr or rcr.stdout or '').strip().splitlines()[-3:]
                print(f"  [warn] cost re-quant ({tag}) failed. Last lines: {tail}")
                _strength_cache[strength] = b''
                return None
            data = open(idx_path, 'rb').read()
            _strength_cache[strength] = data
            return data

        def _quant_cost_atlas_nn():
            """Back-compat shim — strength=0.0 is what the old nn path did."""
            return _quant_cost_atlas_at_strength(0.0)

        joint_idx_bytes = (open(cost_atlas_idx_path, 'rb').read()
                           if cost_atlas_idx_path and
                              os.path.exists(cost_atlas_idx_path) else None)
        if joint_idx_bytes is None:
            print(f"  [warn] dithered cost_atlas re-quantize failed; "
                  f"SKIPPING COST re-encode")
            costume_data = []
        cost_atlas_w = cost_atlas_im.width if cost_atlas_im else 0

        # palette[0] is locked to #000000 in our joint pass, so png2amiga
        # routes BOTH alpha=0 source pixels AND opaque-black art to
        # slot 0. If we treat idx==0 as transparent in the cost RLE,
        # real-black art gets encoded as transparent and the Amiga
        # engine renders it as see-through. Distinguish the two using
        # the source PNG's alpha channel: where alpha=0, replace the
        # idx with TRANSPARENT_MARKER (slot 17, reserved by the
        # quantizer so no real pixel lands there); where alpha=255,
        # keep whatever idx the quantizer chose, INCLUDING idx=0 for
        # real black.
        TRANSPARENT_MARKER = 17
        cost_alpha_bytes = (cost_atlas_im.split()[3].tobytes()
                            if cost_atlas_im is not None else b'')
        costume_frame_pixels = {ci: {} for ci in range(len(costume_data))}
        for (ci, fi, ax, ay, w_, h_) in cost_layout:
            buf = bytearray(w_ * h_)
            for yy in range(h_):
                src = (ay + yy) * cost_atlas_w + ax
                row_idx = joint_idx_bytes[src:src + w_]
                row_alpha = cost_alpha_bytes[src:src + w_]
                for xx in range(w_):
                    buf[yy * w_ + xx] = (TRANSPARENT_MARKER
                                          if row_alpha[xx] == 0
                                          else row_idx[xx])
            costume_frame_pixels[ci][fi] = bytes(buf)

        def _get_pixels_at_strength(ci_target, strength):
            """Return {fi: pixels} for cid ci_target, re-quantized at
            dither_strength. Same alpha-aware re-mask as the dithered
            path so real-black art doesn't collapse onto the transparent
            sentinel."""
            atlas_idx = _quant_cost_atlas_at_strength(strength)
            if not atlas_idx:
                return {}
            out = {}
            for (ci, fi, ax, ay, w_, h_) in cost_layout:
                if ci != ci_target:
                    continue
                buf = bytearray(w_ * h_)
                for yy in range(h_):
                    src = (ay + yy) * cost_atlas_w + ax
                    row_idx = atlas_idx[src:src + w_]
                    row_alpha = cost_alpha_bytes[src:src + w_]
                    for xx in range(w_):
                        buf[yy * w_ + xx] = (TRANSPARENT_MARKER
                                              if row_alpha[xx] == 0
                                              else row_idx[xx])
                out[fi] = bytes(buf)
            return out

        # Back-compat shim for code still calling the nn helper.
        def _get_nn_frame_pixels_for(ci_target):
            return _get_pixels_at_strength(ci_target, 0.0)

        for ci, cd in enumerate(costume_data):
            npal_c = cd['npal']
            # Transparent in the costume frame = the marker we substituted
            # above based on source alpha. Anything else (including idx=0
            # for real black) is real opaque art.
            TRANSPARENT_NEWIDX = TRANSPARENT_MARKER
            from collections import Counter
            freq = Counter()
            for fi, pix in costume_frame_pixels[ci].items():
                for b in pix:
                    if b == TRANSPARENT_NEWIDX:
                        continue
                    freq[b] += 1
            if not freq:
                continue
            ranked = freq.most_common()
            # Build new palette table and remap
            new_table, direct_remap, chosen = build_new_palette_table(
                ranked, npal_c)
            # Remap each frame's pixels to palette-table indices (0..npal-1)
            from encode_cost import remap_pixels
            frame_remaps = {}
            # Need original frame pixel offsets in the COST body. The decoder
            # tracked seen_data_offsets; re-derive by re-decoding the costume
            # Frame offsets came pre-decoded from the cache; same iteration
            # order as cd['frames']. No need to re-walk.
            cbody_local = cd['body']
            data_offsets = cd['frame_offsets_in_body']
            if len(data_offsets) != len(cd['frames']):
                print(f"    costume {cd['cid']}: offset/frame count mismatch "
                      f"({len(data_offsets)} vs {len(cd['frames'])}), skipping re-encode")
                continue
            def _remap_all(src_pixels):
                out = {}
                for fi, pix in src_pixels[ci].items():
                    out[data_offsets[fi]] = remap_pixels(
                        pix, direct_remap, palette, chosen, TRANSPARENT_NEWIDX)
                return out

            # Iterate dither-strength 1.0 → 0.0 in 0.2 steps; pick the
            # highest strength whose RLE fits SCUMM's 16-bit COST
            # baseptr cap (~64KB per COST). Higher strength = more
            # dither = better perceived shading but more RLE bytes
            # (alternating colours break compression). The joint --best
            # pass uses 0.8 (or whatever DITHER_STRENGTH set), so 0.8
            # comes for free from `costume_frame_pixels`; the other
            # strengths re-quantize via _get_pixels_at_strength.
            frame_remaps_joint = _remap_all(costume_frame_pixels)
            JOINT_STRENGTH = 0.8   # matches DITHER_ARGS default
            attempts = (1.0, JOINT_STRENGTH, 0.6, 0.4, 0.2, 0.0)
            new_body = None
            for strength in attempts:
                if strength == JOINT_STRENGTH:
                    frame_remaps = frame_remaps_joint
                else:
                    pixels = _get_pixels_at_strength(ci, strength)
                    if not pixels:
                        continue
                    frame_remaps = {data_offsets[fi]: remap_pixels(
                        pix, direct_remap, palette, chosen,
                        TRANSPARENT_NEWIDX)
                        for fi, pix in pixels.items()}
                try:
                    new_body = rebuild_cost_body(
                        cbody_local, new_table, frame_remaps, npal_c)
                    _used_dither = ('none' if strength <= 0.0
                                    else f's={strength:.1f}')
                    break
                except RuntimeError as e:
                    if 'COST body overflow' not in str(e):
                        raise
                    # try next (lower) strength
            if new_body is None:
                raise RuntimeError(
                    f"cid {cd['cid']}: no dither strength in 1.0..0.0 "
                    f"fits the 64KB COST cap")
            cost_off = cd['cost_of']
            cost_sz = cd['cost_sz']
            old_body_size = cost_sz - 8
            new_body_size = len(new_body)
            # `pristine` here means the AMIGA-DATA original body length
            # (cd['body'] comes from pristine_cache, untouched by chained
            # monkey2-hd state). cost_sz reflects whatever was last in
            # the disk file, which during a chained build is already
            # re-encoded — using it as the baseline gives a misleading
            # 1.00× ratio.
            pristine_size = len(cd['body'])
            try:
                cost_rle_stats
            except NameError:
                cost_rle_stats = []
            ratio = new_body_size / max(1, pristine_size)
            cost_rle_stats.append({
                'cid': cd['cid'],
                'pristine': pristine_size,
                'reencoded': new_body_size,
                'ratio': ratio,
                'dither': _used_dither,
            })
            cost_grow_patches.append({
                'offset': cost_off,
                'old_size': cost_sz,           # full chunk size (incl 8-byte hdr)
                'new_body': new_body,
                # parents=[] — apply_patches' final LFLF update handles the
                # parent size already via total_delta. Adding lflf_off here
                # would double-count cost deltas in the LFLF header.
                'parents': [],
                'cost_id': cd['cid'],
            })
            requantized_costume_positions.add(cd['cid'])  # track by cid
            n_table_used = sum(1 for v in new_table[1:] if v != 0)
            grow_note = (f", grew by {new_body_size - old_body_size} bytes "
                         f"({ratio:.2f}× pristine)"
                         if new_body_size != old_body_size else "")
            warn = ''
            # WARN when the RLE bloated ≥2× pristine even after the
            # strength-search picked the best fit. Reports the final
            # strength so the operator can decide whether to override.
            if _used_dither != 'none' and ratio >= 2.0:
                warn = (f"  WARN: cid {cd['cid']} RLE bloated "
                        f"{ratio:.2f}× pristine at dither {_used_dither} "
                        f"(stepped-search picked the highest strength "
                        f"that fits the 64KB cap)")
            print(f"    costume {cd['cid']} re-encoded: "
                  f"{n_table_used} unique colours in new palette table"
                  f"{grow_note} [dither={_used_dither}]")
            if warn:
                print(warn)

        # Per-room cost-RLE summary: aggregate sizes + flag any cid where
        # dither blew the RLE past 2× pristine. Easy to grep from the
        # build log (`grep "Cost RLE summary"` / `grep "WARN: dither"`).
        try:
            stats = cost_rle_stats
        except NameError:
            stats = []
        if stats:
            sum_pristine = sum(s['pristine'] for s in stats)
            sum_reencoded = sum(s['reencoded'] for s in stats)
            # Bucket by final dither strength chosen by the stepped
            # search. Format: "1 @ 1.0, 2 @ 0.8, 0 @ 0.6, …, 1 none"
            from collections import Counter
            by_strength = Counter(s['dither'] for s in stats)
            buckets = ', '.join(f'{n}@{d}' for d, n in
                                sorted(by_strength.items(),
                                       key=lambda kv: kv[0]))
            print(f"  Cost RLE summary: {len(stats)} cid(s) "
                  f"[{buckets}] "
                  f"{sum_pristine}B pristine -> {sum_reencoded}B re-encoded "
                  f"({sum_reencoded/max(1,sum_pristine):.2f}×)")
            for s in sorted(stats, key=lambda s: -s['ratio']):
                tag = (' WARN' if s['dither'] != 'none' and s['ratio'] >= 2.0
                       else '')
                print(f"    cid {s['cid']:3d}: {s['pristine']:6d}B -> "
                      f"{s['reencoded']:6d}B ({s['ratio']:.2f}×) "
                      f"[{s['dither']}]{tag}")
    # ---- end COST re-encode ----------------------------------------------
    _t('cost_reencode')

    # ---- Build CLUT for output -------------------------------------------
    # Real Amiga renders only CLUT[16..47] for room art (paletteMod=16);
    # everything outside that range gets truncated by the OCS hardware to
    # 5 bits + offset 16, so writing into [48..255] is invisible on real HW.
    # ScummVM is permissive (8-bit framebuffer) and reads CLUT[v] directly,
    # which matters for cross-room costumes whose pal_tables point at
    # [192..207] — we mirror the canonical actor palette into those slots
    # below so ScummVM and real Amiga render Guybrush identically.
    orig_clut_full = list(orig_clut_for_swatch)
    new_clut_full = list(orig_clut_full)
    for i, c in enumerate(palette):
        if i < 32:
            new_clut_full[16 + i] = c

    # No CLUT[192..207] mirror needed: Guybrush's globally-encoded
    # pal_table now points at CLUT slots 38..47 directly, which our
    # locked palette indices 22..31 cover via paletteMod=16. ScummVM
    # and real Amiga both read CLUT[38..47] = the canonical RGBs.

    def _measure_palette_s2(in_png_path, idx_path):
        """Score the encoded `.idx` against `in_png_path` via png2amiga's
        `--score-vs <ref> --palette <pal.json> <input.idx>` (v1.83+).
        Pure ssimulacra2 + PSNR, no re-encode. png2amiga renders the
        .idx through the palette internally, and matte-flattens alpha=0
        on both sides so RGBA references compare cleanly."""
        pal_path = f'{workdir}/palette.json'
        if not os.path.exists(pal_path):
            return None
        cmd = [PNG2AMIGA, '--score-vs', in_png_path,
               '--palette', pal_path, idx_path]
        rr = _run_png2amiga(cmd, label='S2 score-vs')
        return _parse_s2(rr.stdout)

    # All three regions ship with --best + default dither against the
    # final locked palette (the palette is dither-aware — disabling
    # dither at render time would actively hurt quality vs the palette
    # it was tuned to).
    #
    # S2 measurement is OPT-IN (S2=1) because each call here spawns a
    # full png2amiga subprocess — 3 of them per room, ~0.5-1.5s each
    # depending on bg/atlas size. Across ~85 rooms that's most of a
    # full build's wall time. The joint --best pass already prints one
    # S2 number to stdout (parsed into joint_s2 above), which is enough
    # for spot-checking. Set S2=1 when triaging quality regressions.
    # Default-on now that score-vs + .idx + --palette is cheap (v1.83+).
    # Set S2=0 to skip if you really don't want the per-region numbers.
    measure_s2 = os.environ.get('S2', '1') != '0'
    # Three independent png2amiga --score-vs subprocesses (bg, atlas,
    # cost). Run them in parallel via a thread pool — Python releases
    # the GIL while waiting on a subprocess, so concurrent.futures gives
    # us the speedup for free.
    bg_s2 = atlas_s2 = cost_s2 = None
    if measure_s2:
        if VERBOSE:
            print(f'[s2] pc_png={pc_png}')
            print(f'[s2] bg_idx={bg_idx_path}')
            print(f'[s2] atlas_png={atlas_png}')
            print(f'[s2] obim_idx={obim_atlas_idx_path}')
            print(f'[s2] cost_idx={cost_atlas_idx_path}')
        bg_s2 = _measure_palette_s2(pc_png, bg_idx_path)
        if atlas_png and obim_atlas_idx_path:
            atlas_s2 = _measure_palette_s2(atlas_png, obim_atlas_idx_path)
        if cost_atlas_im is not None and cost_atlas_idx_path:
            try:
                cost_s2 = _measure_palette_s2(
                    f'{workdir}/cost_atlas.png', cost_atlas_idx_path)
            except Exception as e:
                print(f"  (cost S2 skipped: {e})")

    if measure_s2:
        s2_parts = [f"bg={bg_s2:.2f}" if bg_s2 is not None else "bg=n/a"]
        if atlas_s2 is not None:
            s2_parts.append(f"atlas={atlas_s2:.2f}")
        if cost_s2 is not None:
            s2_parts.append(f"cost={cost_s2:.2f}")
        print(f"  Palette: 32 entries ({n_locked} locked); S2: "
              + ", ".join(s2_parts))
    else:
        # Joint --best already prints its own S2 — fold that in.
        joint_str = f", joint S2={joint_s2:.2f}" if joint_s2 is not None else ""
        print(f"  Palette: 32 entries ({n_locked} locked){joint_str}"
              f"  (set S2=1 for per-region S2)")
    _t('s2_measure')

    # Use png2amiga's --output-indexed bytes directly; no RGB→slot ambiguity.
    if len(bg_indexed_input) != w * h:
        raise RuntimeError(f"bg index byte count {len(bg_indexed_input)} != {w*h}")

    # Optional per-room region constraint: each pixel's allowed slots may be
    # restricted (e.g. dred-deck's water cycle pixels must use slots 28..31
    # only, sky/ship pixels must use slots 0..27).
    region_mask = None
    region_slot_sets = None
    if override and override.region_constraint:
        rc = override.region_constraint(w, h)
        if rc is not None:
            region_mask, region_slot_sets = rc
            print(f"  Per-room override: region_constraint active "
                  f"({len(set(region_mask))} regions)")

    if region_mask is not None:
        indexed = bytearray(w * h)
        for i, slot in enumerate(bg_indexed_input):
            allowed = region_slot_sets[region_mask[i]]
            if slot in allowed:
                indexed[i] = slot
            else:
                rgb = palette[slot]
                indexed[i] = min(allowed, key=lambda j: oklab_dist(palette[j], rgb))
        indexed = bytes(indexed)
    else:
        indexed = bg_indexed_input
    print(f"  Indexed bg ({w}x{h}) from --output-indexed (no RGB lookup)")

    # Build the new bg SMAP body. Tree-rebuild handles size growth, so
    # `target_size` is just a hint to keep encoded byte count close to
    # the pristine SMAP when possible (less disk churn).
    rmim_for_size, rmim_sz_for_size = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    im00_for_size, im00_sz_for_size = find_chunk(d, rmim_for_size+8, rmim_for_size+rmim_sz_for_size, 'IM00')
    smap_for_size, smap_sz_for_size = find_chunk(d, im00_for_size+8, im00_for_size+im00_sz_for_size, 'SMAP')
    target_smap_body_size = smap_sz_for_size - 8
    new_smap = build_smap_body(indexed, w, h, target_size=target_smap_body_size)
    print(f"  new SMAP: {len(new_smap)} bytes")
    print(f"  new CLUT: 768 bytes (from new_clut_full)")
    _t('bg_smap_build')

    # ---- Multi-chunk patching: bg SMAP + every OBIM frame's SMAP, and CLUT ----
    # Re-encode each OBIM frame's SMAP. Transparency comes from the AMIGA decode
    # (where TRNS sentinel is well-defined); colours come from the joint --best
    # quantization of the PC pixels.
    # Find the room's TRNS value
    trns_off, trns_sz = find_chunk(d, ro+8, ro+room_size, 'TRNS')
    trns_value = d[trns_off+8] if trns_off is not None else 1
    print(f"  Room TRNS value: {trns_value}")

    obim_smaps = {}
    if atlas_png is not None and atlas_layout is not None:
        from obim_reencode import build_obim_replacements, decode_obim_transparency
        # Decode original Amiga OBIMs to extract per-frame transparency masks
        transp_masks_full = decode_obim_transparency(src_disk, rid, trns_value)
        # Strip dimensions from values; we just need the masks
        transp_masks = {k: v[0] for k, v in transp_masks_full.items()}


        # OBIM atlas indexed bytes come for free from the joint pass
        # (obim_atlas.idx, written by --oe). The shared-palette property
        # is intrinsic — every --ji input uses the same final 32-colour
        # palette — so no separate re-quant call is needed.
        atlas_indexed = (open(obim_atlas_idx_path, 'rb').read()
                         if obim_atlas_idx_path and
                            os.path.exists(obim_atlas_idx_path) else None)
        atlas_w = Image.open(atlas_png).size[0]
        layout_paths_by_obj = {}
        for f in obim_pngs:
            base = os.path.basename(f)
            parts = base.rsplit('.', 1)[0].split('_')
            oid = int(parts[1])
            frame_name = parts[2]
            layout_paths_by_obj[base] = (oid, frame_name)
        if atlas_indexed is not None:
            obim_smaps = build_obim_replacements(
                atlas_indexed, atlas_w, atlas_layout, transp_masks,
                layout_paths_by_obj, trns_value=trns_value,
            )
        # Per-room override: drop OBIM SMAPs the override wants left alone
        # (so the original Amiga SMAP for those objects stays in place).
        if override and override.skip_obim:
            kept = {}
            skipped = 0
            for k, v in obim_smaps.items():
                # k is keyed by the layout entry; recover obj_id from layout_paths_by_obj
                base = k if isinstance(k, str) else None
                obj_id = layout_paths_by_obj.get(base, (None, None))[0]
                if obj_id is not None and override.skip_obim(obj_id):
                    skipped += 1
                else:
                    kept[k] = v
            if skipped:
                print(f"  Per-room override: skip_obim dropped {skipped} OBIM SMAP(s)")
            obim_smaps = kept
        print(f"  Re-encoded {len(obim_smaps)} OBIM frame SMAPs (transparency from Amiga decode)")
    _t('obim_reencode')

    # ---- Tree-based write path ------------------------------------------
    # Parse-tree -> mutate -> serialize -> rebuild-index pipeline.
    # Sizes recompute automatically; LOFF auto-rebuilds during serialize
    # so cross-disk replicated rids stay correctly addressed.
    from scumm_tree import (parse as _parse, parse_index as _parse_index,
                              serialize as _serialize,
                              serialize_index as _serialize_index,
                              find_lflf_for_room as _flfr_tree)
    from scumm_index import rebuild_index as _rebuild_index, parse_droo as _parse_droo

    XOR = 0x69
    # d is already XOR-decrypted by load() at the top of main().
    tree = _parse(bytes(d))
    lflf_node = _flfr_tree(tree, rid)
    if lflf_node is None:
        raise SystemExit(f'tree write: no LFLF for rid {rid}')
    room_node = next((c for c in lflf_node.children if c.tag == 'ROOM'), None)
    if room_node is None:
        raise SystemExit(f'tree write: no ROOM in LFLF rid {rid}')

    # 1) CLUT body
    clut_node = next((c for c in room_node.children if c.tag == 'CLUT'), None)
    if clut_node is not None:
        clut_node.body = bytes(v for rgb in new_clut_full for v in rgb)

    # 2) bg SMAP body (room_node -> RMIM -> IM00 -> SMAP)
    rmim_node = next((c for c in room_node.children if c.tag == 'RMIM'), None)
    if rmim_node is not None:
        im00_node = next((c for c in rmim_node.children if c.tag == 'IM00'),
                         None)
        if im00_node is not None:
            smap_bg = next((c for c in im00_node.children if c.tag == 'SMAP'),
                           None)
            if smap_bg is not None:
                smap_bg.body = new_smap

    # 3) OBIM SMAPs
    if obim_smaps:
        import struct as _struct
        for obim_node in [c for c in room_node.children if c.tag == 'OBIM']:
            imhd = next((c for c in obim_node.children if c.tag == 'IMHD'),
                        None)
            if imhd is None:
                continue
            obj_id = _struct.unpack('<H', imhd.body[:2])[0]
            for im_node in obim_node.children:
                # IM00..IM99 are composite frame containers; IMHD is a leaf
                # metadata chunk that also matches startswith('IM') + len 4
                # but has children=None.
                if (not im_node.tag.startswith('IM')
                        or len(im_node.tag) != 4
                        or im_node.children is None):
                    continue
                if (obj_id, im_node.tag) not in obim_smaps:
                    continue
                smap_n = next((c for c in im_node.children if c.tag == 'SMAP'),
                              None)
                if smap_n is not None:
                    smap_n.body = obim_smaps[(obj_id, im_node.tag)]

    # 4) COST bodies for re-encoded costumes (cost_grow_patches has them
    #    by orig file offset; map to nodes via position in LFLF children).
    if cost_grow_patches:
        cost_nodes = [c for c in lflf_node.children if c.tag == 'COST']
        for entry in cost_grow_patches:
            # entry['offset'] was the file offset BEFORE any tree-write, but
            # since each pristine cost_offsets[i] corresponds to LFLF
            # children COSTs in order, we can match by cost_id from the
            # entry (we kept it in cost_grow_patches).
            cid = entry.get('cost_id')
            for pc, node in zip(pristine_room['costumes'], cost_nodes):
                if pc['cost_id'] == cid:
                    node.body = entry['new_body']
                    break

    # 5) TalkColor literal redirect — modify script chunk bodies in tree.
    try:
        from patch_talkcolors import find_talkcolor_literals
        n_patched = n_inspected = n_script_chunks = 0
        # LSCR has a 1-byte script# prefix BEFORE the body proper; descumm
        # offsets are within the body AFTER that prefix.
        body_offset_for_descumm = {'LSCR': 1, 'SCRP': 0, 'EXCD': 0, 'ENCD': 0}

        def _patch_script_node(node, body_skip):
            nonlocal n_patched, n_inspected, n_script_chunks
            n_script_chunks += 1
            chunk_bytes = (b'\0' * 8 + node.body)   # synthetic header for
                                                     # find_talkcolor_literals
            sub_body = node.body[body_skip:]
            new_body = bytearray(node.body)
            for lit_off_in_subbody, value in find_talkcolor_literals(
                    sub_body, chunk_bytes):
                n_inspected += 1
                if not (16 <= value <= 47) or value == 33:
                    continue
                pristine_rgb = orig_clut[value]
                closest_idx = min(range(32),
                                  key=lambda j: oklab_dist(palette[j],
                                                              pristine_rgb))
                new_slot = 16 + closest_idx
                if new_slot != value:
                    new_body[body_skip + lit_off_in_subbody] = new_slot
                    n_patched += 1
            node.body = bytes(new_body)

        # Scan room-level scripts (LSCR/EXCD/ENCD inside ROOM, plus OBCD-VERB)
        for c in room_node.children:
            if c.tag in body_offset_for_descumm:
                _patch_script_node(c, body_offset_for_descumm[c.tag])
            elif c.tag == 'OBCD':
                verb = next((g for g in c.children if g.tag == 'VERB'), None)
                if verb is not None:
                    _patch_script_node(verb, 0)
        # Plus LFLF-level SCRPs
        for c in lflf_node.children:
            if c.tag == 'SCRP':
                _patch_script_node(c, 0)
        if n_inspected:
            print(f"  TalkColor patch: {n_patched}/{n_inspected} literals "
                  f"redirected (across {n_script_chunks} script chunks)")
    except (ImportError, FileNotFoundError) as _e:
        print(f"  [warn] TalkColor patcher unavailable: {_e}")

    # 6) NN-remap pal_tables of every NON-re-encoded home-LFLF costume
    #    (they're not in costume_data; their pal_tables still point at
    #    pristine CLUT slots that may not exist anymore in [16..47]).
    cost_nodes = [c for c in lflf_node.children if c.tag == 'COST']
    for pos, (pc, node) in enumerate(zip(pristine_room['costumes'],
                                           cost_nodes)):
        # Skip costumes with already-rewritten COST bodies — either
        # globally re-encoded (cid 1 + every extras-group cid) or
        # per-room re-encoded above. NN-remapping them would read the
        # pristine pal_table from cache and overwrite the correct
        # re-encoded one.
        if pc['cost_id'] in requantized_costume_positions:
            continue
        if pc['cost_id'] in globally_encoded_cids:
            continue
        if pc['npal'] == 0 or pc['fmt'] not in (0x58, 0x59):
            continue
        npal = pc['npal']
        old_pal_table = list(pc['pal_table'])  # pristine, not whatever's in tree
        new_pal_table = list(old_pal_table)
        for k, v in enumerate(old_pal_table):
            if v == 0 or v == 250:
                continue
            if 16 <= v <= 47:
                # find closest in new palette
                pristine_rgb = orig_clut[v]
                closest_idx = min(range(32),
                                  key=lambda j: oklab_dist(palette[j],
                                                              pristine_rgb))
                new_pal_table[k] = 16 + closest_idx
            # else: leave [192..207] etc. alone (no longer in our mgmt range)
        # Patch the pal_table region in the COST body
        body = bytearray(node.body)
        body[2:2 + npal] = bytes(new_pal_table[:npal])
        node.body = bytes(body)

    # 7) Per-room override: extra_patches and post_patch — current overrides
    #    expect bytearray + offsets. Skip extra_patches (rare; only dred-deck);
    #    run post_patch on the SERIALIZED bytes as a final-pass mutation.
    if override and override.extra_patches:
        print(f"  [warn] override.extra_patches not yet ported to tree pipeline; "
              f"falling back to in-place after serialize")

    # Serialize disk + rebuild index in one pass
    positions = {}
    out = _serialize(tree, positions)
    if override and override.post_patch:
        # Apply post_patch on the serialized bytearray. This may mutate
        # script bytes; sizes stay the same (post_patch is byte-overwrite).
        d_mut = bytearray(out)
        ro_new = next(o for r, o in __import__('scumm_tree').parse(d_mut)
                       .find_all('LFLF') if False) if False else 0
        # Find the new ROOM offset in serialized bytes
        from scumm_tree import find_lflf_for_room as _f
        new_tree = _parse(bytes(d_mut))
        new_lflf = _f(new_tree, rid)
        new_room = next(c for c in new_lflf.children if c.tag == 'ROOM')
        ro_new = new_room.orig_offset
        rsz_new = 8 + (sum(0 for _ in new_room.children) and 0) or 0
        # We don't actually need rsz here; pass the body size + 8.
        # post_patch typically only writes bytes within scripts.
        from scumm_tree import _be32 as _be32_
        rsz_new = _be32_(bytes(d_mut), ro_new + 4)
        override.post_patch(d_mut, ro_new, rsz_new, palette, new_clut_full)
        print(f"  Per-room override: post_patch applied")
        out = bytes(d_mut)

    dest_disk = f'{SVM_DIR}/monkey2.{disk:03d}'
    with open(dest_disk, 'wb') as f:
        f.write(out.translate(XOR_TABLE))
    print(f"  -> {dest_disk} ({len(out)} bytes)")

    # Rebuild the index from current tree positions of every disk.
    idx_path = f'{SVM_DIR}/monkey2.000'
    if not os.path.exists(idx_path):
        idx_path = f'{AMIGA_DIR}/monkey2.000'
    idx_plain = open(idx_path, 'rb').read().translate(XOR_TABLE)
    index_root = _parse_index(idx_plain)
    droo_node = next(c for c in index_root.children if c.tag == 'DROO')
    _, droo_disks_list = _parse_droo(droo_node.body)
    droo_disks = {r: dn for r, dn in enumerate(droo_disks_list) if dn > 0}
    # Need positions for ALL disks for the index rebuild — module-level
    # XOR_TABLE makes the per-disk decode ~100× faster than the old
    # `bytes(b ^ XOR for b in raw)` comprehension.
    full_trees = {disk: tree}
    full_positions = {disk: positions}
    for d_n in set(droo_disks.values()):
        if d_n == disk:
            continue
        p_path = f'{SVM_DIR}/monkey2.{d_n:03d}'
        if not os.path.exists(p_path):
            p_path = f'{AMIGA_DIR}/monkey2.{d_n:03d}'
        p_plain = open(p_path, 'rb').read().translate(XOR_TABLE)
        full_trees[d_n] = _parse(p_plain)
        pos = {}
        _serialize(full_trees[d_n], pos)
        full_positions[d_n] = pos
    _rebuild_index(index_root, full_trees, full_positions, droo_disks)
    new_idx = _serialize_index(index_root)
    dest_index = f'{SVM_DIR}/monkey2.000'
    with open(dest_index, 'wb') as f:
        f.write(new_idx.translate(XOR_TABLE))
    print(f"  -> {dest_index} (index rebuilt, {len(new_idx)} bytes)")
    _t('disk_write')
    _t_summary()

    # (Tree-based pipeline writes directly to monkey2-hd/, no workdir
    # temp file to clean up.)


if __name__ == '__main__':
    main()
