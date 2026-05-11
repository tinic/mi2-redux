#!/usr/bin/env python3
"""SCUMM v5 ClassicCostume decoder (Python port).

Decodes COST chunk RLE pixel data to indexed-pixel frames. Used to
render costume sprites for measuring our patched-palette S2 against
the pristine rendering.

Format reference: ScummVM engines/scumm/costume.cpp
  - ClassicCostumeLoader::loadCostume — header layout
  - paintCelByleRLECommon              — color/run mask & shift
  - byleRLEDecode_ami                  — Amiga MI2 RLE decoder

Layout convention: ScummVM passes a pointer that INCLUDES the 8-byte
chunk tag+size header (`getResourceAddress`), then for SCUMM v5 (not
GF_OLD_BUNDLE / GF_SMALL_HEADER) does `ptr += 2`. That puts ptr at
`chunk_start + 2`, so `ptr[6] = chunk_start[8] = body[0]` etc.

In our world we strip the 8-byte header and work with `body` (the
chunk payload). To map ScummVM offsets back: `_baseptr` is conceptually
at `body - 6`, and any "offset relative to _baseptr" must be adjusted
by `-6` before indexing into our body bytes.

Per-costume header (npal=16 for format 0x58, =32 for 0x59):
  body[0]                : numAnim (sometimes - 1; we don't rely on it)
  body[1]                : format (& 0x7F: 0x58/0x59/0x60/0x61)
  body[2..2+npal]        : palette table (each entry = CLUT slot index)
  body[2+npal..+2]       : animCmds offset (LE16, baseptr-relative)
  body[+2 .. +34]        : 16 frame offsets (LE16 each, baseptr-rel)
  body[+34 .. ]          : numAnim animation cmd offsets, then RLE data

Per-frame header (12 bytes, format 0x58/0x59):
  LE16 width, height, relX (signed), relY (s), moveX (s), moveY (s)
followed by RLE pixel bytes.

RLE byte: high (8-shr) bits = colour index, low shr bits = run length.
   - For 16-color (shr=4, mask=0xF): colour = byte>>4, run = byte&0xF
   - For 32-color (shr=3, mask=0x7): colour = byte>>3, run = byte&0x7
   - run==0: read another byte for the length.
Iteration: top-to-bottom, left-to-right across width × height; colour 0
is transparent (don't draw).
"""

import struct
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def _decode_rle(body, src_off, frame_w, frame_h, num_colors,
                column_major=False):
    """Decode RLE pixel data starting at body[src_off]. Returns
    (pixels, ok): `pixels` is a flat bytearray of (frame_w * frame_h)
    palette-table indices (0..npal-1); `ok` is True if the RLE stream
    fully covered the frame's W*H pixels before hitting end-of-body.
    A False `ok` means the data_off didn't actually point at a valid
    frame — the W*H header was corrupt or the offset landed in the
    middle of unrelated bytes. Index 0 is transparent (zero).

    column_major=False (default): Amiga scan order — left-to-right
    within a row, then next row (matches byleRLEDecode_ami).
    column_major=True: PC / default scan order — top-to-bottom within a
    column, then next column (matches byleRLEDecode in base-costume.cpp).
    """
    if num_colors == 32:
        mask, shr = 7, 3
    elif num_colors == 16:
        mask, shr = 15, 4
    else:
        # 64-color hypothetical; mirrors ScummVM's table
        mask, shr = 3, 2
    out = bytearray(frame_w * frame_h)
    x = 0
    y = 0
    p = src_off
    n = len(body)
    while True:
        if p >= n:
            return out, False
        b = body[p]; p += 1
        color = b >> shr
        run = b & mask
        if run == 0:
            if p >= n:
                return out, False
            run = body[p]; p += 1
        for _ in range(run):
            if 0 <= y < frame_h and 0 <= x < frame_w and color != 0:
                out[y * frame_w + x] = color
            if column_major:
                y += 1
                if y >= frame_h:
                    y = 0
                    x += 1
                    if x >= frame_w:
                        return out, True
            else:
                x += 1
                if x >= frame_w:
                    x = 0
                    y += 1
                    if y >= frame_h:
                        return out, True


# baseptr-relative offset → body offset: subtract 6. Per ScummVM
# loadCostume() in costume.cpp: _baseptr = body - 6 for v5 SCUMM
# (chunk-header skip is +2; getResourceAddress already skipped +8).
BASEPTR_TO_BODY = -6


def _collect_reachable_frame_offsets(body):
    """Walk the anim/limb/cmd graph and return the ordered list of frame
    data-offsets the engine can actually draw. Mirrors the traversal in
    ScummVM's ClassicCostumeRenderer::costumeDecodeData + drawLimb:

      1. For each anim a in [0..numAnim], read dataOffsets[a] (LE16,
         baseptr-relative) → anim entry r.
      2. r begins with a 16-bit limb mask. For each limb i where
         mask bit set: read j (LE16); if j == 0xFFFF the limb stops
         (no frames drawn this anim); otherwise read `extra` (1 byte)
         and the engine plays animCmds[j..j+(extra & 0x7F)] over time.
      3. Each animCmds byte's low 7 bits is a code in [0..0x7A]; the
         engine looks up `frameOffsets[limb] + code*2` in the limb's
         frame_table to get the data offset of the actual frame.
         Codes 0x79/0x7A are control opcodes (start/stop limb, no
         draw). 0x7B = "nothing to draw" sentinel.

    Anything not reachable from this graph is dead bytes that the
    decoder would otherwise mis-parse as frames.
    """
    fmt = body[1] & 0x7F
    npal = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, 0)
    num_anim = body[0] + 1   # ScummVM stores numAnim - 1
    after_pal = 2 + npal

    anim_cmds_off = struct.unpack('<H', body[after_pal:after_pal + 2])[0] \
                  + BASEPTR_TO_BODY
    frame_offsets = struct.unpack(
        '<16H', body[after_pal + 2:after_pal + 2 + 32])
    data_offsets_off = after_pal + 2 + 32

    reachable_per_limb = [set() for _ in range(16)]

    for a in range(num_anim):
        ent = data_offsets_off + a * 2
        if ent + 2 > len(body):
            break
        anim_off_rel = struct.unpack('<H', body[ent:ent + 2])[0]
        if anim_off_rel == 0:
            continue
        r = anim_off_rel + BASEPTR_TO_BODY
        if r < 0 or r + 2 > len(body):
            continue
        mask = struct.unpack('<H', body[r:r + 2])[0]
        r += 2
        for limb in range(16):
            if not (mask & 0x8000):
                mask = (mask << 1) & 0xFFFF
                continue
            if r + 2 > len(body):
                break
            j = struct.unpack('<H', body[r:r + 2])[0]
            r += 2
            if j != 0xFFFF:
                if r + 1 > len(body):
                    break
                extra = body[r]
                r += 1
                run_len = (extra & 0x7F) + 1   # animCmds[j..=j+(extra&0x7F)]
                for k in range(run_len):
                    ac = anim_cmds_off + j + k
                    if ac < 0 or ac >= len(body):
                        continue
                    code = body[ac] & 0x7F
                    if code < 0x79:
                        reachable_per_limb[limb].add(code)
            mask = (mask << 1) & 0xFFFF

    seen = set()
    out = []
    for limb in range(16):
        codes = reachable_per_limb[limb]
        if not codes:
            continue
        ft = frame_offsets[limb] + BASEPTR_TO_BODY
        if ft < 0 or ft >= len(body):
            continue
        for code in sorted(codes):
            ent = ft + code * 2
            if ent < 0 or ent + 2 > len(body):
                continue
            data_off = struct.unpack('<H', body[ent:ent + 2])[0] \
                     + BASEPTR_TO_BODY
            if data_off < 0 or data_off + 12 > len(body):
                continue
            if data_off in seen:
                continue
            seen.add(data_off)
            out.append(data_off)
    return out


def decode_costume(body, column_major=False):
    """Decode every frame the engine can actually draw (reachable from
    the anim/limb/cmd graph). Returns (frames, palette_table) where
    frames is a list of (w, h, indexed_pixels). palette_table is the
    costume's CLUT-slot lookup (length 16 or 32).

    column_major=False (default): Amiga RLE scan order.
    column_major=True: PC / default RLE scan order — pass this when
    decoding from pc-data/MONKEY2.001 (the original SCUMM v5 format)."""
    if len(body) < 4:
        return [], []
    fmt = body[1] & 0x7F
    npal = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, 0)
    if npal == 0:
        return [], []
    palette_table = list(body[2:2 + npal])
    if 2 + npal + 2 + 32 > len(body):
        return [], palette_table

    frames = []
    for data_off in _collect_reachable_frame_offsets(body):
        w_, h_ = struct.unpack('<HH', body[data_off:data_off + 4])
        if w_ == 0 or h_ == 0:
            continue
        try:
            pixels, ok = _decode_rle(body, data_off + 12, w_, h_, npal,
                                     column_major=column_major)
        except Exception:
            continue
        # Frame header is corrupt: W*H is too large to fit a full RLE
        # decode within remaining body bytes. The data_off pointed at
        # bogus bytes (a frame_table entry whose code path the engine
        # never actually executes — usemask gates it at runtime).
        if not ok:
            continue
        frames.append((w_, h_, pixels))
    return frames, palette_table


def walk_frame_offsets(body):
    """Return the list of frame data-offsets within `body` in the SAME
    order as decode_costume() yields its frames. Used by the encoder to
    write each frame's new RLE bytes back at the original location."""
    if len(body) < 4:
        return []
    fmt = body[1] & 0x7F
    npal = {0x58: 16, 0x59: 32, 0x60: 16, 0x61: 32}.get(fmt, 0)
    if npal == 0:
        return []
    if 2 + npal + 2 + 32 > len(body):
        return []
    out = []
    for data_off in _collect_reachable_frame_offsets(body):
        w_, h_ = struct.unpack('<HH', body[data_off:data_off + 4])
        if w_ == 0 or h_ == 0:
            continue
        try:
            _, ok = _decode_rle(body, data_off + 12, w_, h_, npal,
                                column_major=False)
        except Exception:
            continue
        if not ok:
            continue
        out.append(data_off)
    return out


def render_frames_atlas(frames, palette_table, clut, transparent_rgb=(0xFF, 0x00, 0xFF)):
    """Lay frames out on a grid as one PIL.Image (RGB). Each pixel:
        idx 0 → transparent_rgb (so png2amiga --transparent-color routes
                it to slot 0)
        idx N → clut[palette_table[N]] as a CLUT slot lookup
    `clut` must be a 768-byte (256*3) sequence (the room/costume's
    pristine CLUT).
    """
    from PIL import Image
    if not frames:
        return None
    max_w = max(f[0] for f in frames)
    max_h = max(f[1] for f in frames)
    cols = 8
    rows = (len(frames) + cols - 1) // cols
    img = Image.new('RGB', (cols * max_w, rows * max_h), transparent_rgb)
    px = img.load()
    for i, (w_, h_, pixels) in enumerate(frames):
        cx = (i % cols) * max_w
        cy = (i // cols) * max_h
        for yy in range(h_):
            for xx in range(w_):
                idx = pixels[yy * w_ + xx]
                if idx == 0:
                    continue
                if idx >= len(palette_table):
                    continue
                slot = palette_table[idx]
                px[cx + xx, cy + yy] = (clut[slot * 3],
                                        clut[slot * 3 + 1],
                                        clut[slot * 3 + 2])
    return img


def main():
    """Standalone CLI: decode_cost.py <disk> <room_id> <out_dir>
    Decodes every COST chunk in the LFLF for room_id, saves
    one tilesheet per costume."""
    from decode_amiga_room import load, be32, walk_rooms, find_chunk
    from remap_costume_palette import find_lflf_for_room, find_costumes_in_lflf

    if len(sys.argv) != 4:
        print(__doc__.split('\n', 1)[0])
        print("usage: decode_cost.py <amiga-data/monkey2.NNN> <room_id> <out_dir>")
        sys.exit(1)
    disk_path = sys.argv[1]
    room_id = int(sys.argv[2])
    out_dir = sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)

    d = load(disk_path)
    ro = next(o for r, o in walk_rooms(d) if r == room_id)
    rsz = be32(d, ro + 4)
    clut_off, _ = find_chunk(d, ro + 8, ro + rsz, 'CLUT')
    clut = bytes(d[clut_off + 8:clut_off + 8 + 256 * 3])

    lflf = find_lflf_for_room(d, room_id)
    costs = find_costumes_in_lflf(d, lflf)
    print(f"Room {room_id}: {len(costs)} costume(s) in LFLF @{lflf:#x}")
    for i, cost_off in enumerate(costs):
        cost_sz = be32(d, cost_off + 4)
        body = bytes(d[cost_off + 8:cost_off + cost_sz])
        frames, pal_table = decode_costume(body)
        print(f"  COST {i} @{cost_off:#x}: format=0x{body[1] & 0x7F:02x}, "
              f"npal={len(pal_table)}, decoded {len(frames)} frame(s)")
        if frames:
            img = render_frames_atlas(frames, pal_table, clut)
            out_path = f"{out_dir}/cost_{i}.png"
            img.save(out_path)
            print(f"    -> {out_path}  ({img.size[0]}x{img.size[1]})")


if __name__ == '__main__':
    main()
