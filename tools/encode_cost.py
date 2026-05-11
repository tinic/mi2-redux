#!/usr/bin/env python3
"""SCUMM v5 ClassicCostume RLE encoder + COST chunk rebuild.

Reverse of decode_cost.py. Used by inject_room.py when we re-quantize
each costume's pixels jointly with the bg/OBIM atlas in --best, and need
to emit a new COST chunk that uses the joint palette.

RLE byte format (per ScummVM byleRLEDecode_ami):
    high (8-shr) bits = colour index (0 = transparent)
    low  shr     bits = run length, range 1..mask
    if low bits == 0, the next byte is the run length (1..255)

For 16-color (format 0x58): shr=4, mask=15
For 32-color (format 0x59): shr=3, mask=7

The decoder iterates row-major (left-to-right, then next row), so a
single (color,run) tuple may span row boundaries. We emit the same way.
"""

import struct


def encode_rle(pixels, num_colors):
    """RLE-encode a flat array of palette-table indices (0..mask) into
    bytes matching ScummVM's byleRLEDecode_ami expectation."""
    if num_colors == 32:
        mask, shr = 7, 3
    elif num_colors == 16:
        mask, shr = 15, 4
    else:
        raise ValueError(f"unsupported numColors {num_colors}")

    color_max = (1 << (8 - shr)) - 1   # 16-col: 15, 32-col: 31
    out = bytearray()
    n = len(pixels)
    i = 0
    while i < n:
        c = pixels[i]
        if c > color_max:
            raise ValueError(
                f"colour index {c} exceeds max {color_max} (num_colors={num_colors})")
        j = i + 1
        while j < n and pixels[j] == c:
            j += 1
        run = j - i
        # Decoder reads `len = byte & mask; if !len: len = next byte`. Our
        # 1-byte form encodes runs 1..mask; the 2-byte form encodes runs
        # 1..255 (in chunks). Choose the shorter one.
        while run > 0:
            if run <= mask:
                out.append((c << shr) | run)
                run = 0
            else:
                chunk = run if run <= 255 else 255
                out.append(c << shr)        # low bits = 0 → next byte holds len
                out.append(chunk)
                run -= chunk
        i = j
    return bytes(out)


def build_new_palette_table(used_indices_with_freq, num_colors):
    """Pick the costume's new 16-entry (or 32-entry) palette table.

    `used_indices_with_freq` is a list of (new_palette_idx, frequency) for
    every distinct palette index that appears in this costume's pixel
    data, sorted by frequency descending.

    Returns (palette_table[N], remap_dict). palette_table is a list of N
    bytes (CLUT slot indices, == 16 + new_palette_idx). remap_dict maps
    every new_palette_idx (0..31) used by the costume to a palette-table
    slot in 1..N-1.

    Slot 0 is the costume's "transparent" sentinel — never written by the
    encoder. We set table[0] = 0 (matches pristine convention)."""
    table_size = num_colors  # 16 or 32
    n_color_slots = table_size - 1  # slots 1..table_size-1 are colours

    # Top-N by frequency
    top = used_indices_with_freq[:n_color_slots]
    chosen_pal = [pi for pi, _ in top]

    # palette_table[i] for i=1..N is CLUT slot = 16 + new_palette_idx
    table = [0] * table_size
    for i, pi in enumerate(chosen_pal, start=1):
        table[i] = 16 + pi

    # Map: new_palette_idx → palette_table_idx (1..N-1)
    direct_remap = {pi: ti for ti, pi in enumerate(chosen_pal, start=1)}
    return table, direct_remap, chosen_pal


def remap_pixels(pixel_array, direct_remap, palette_rgb_lookup,
                  chosen_pal, transparent_idx):
    """Map each pixel from new_palette_idx (0..31) to palette_table_idx
    (0..15). For indices not in chosen_pal: nearest match in OKLab among
    chosen_pal.
    `palette_rgb_lookup`: list of 32 (r,g,b) tuples for the new palette.
    `transparent_idx`: the new_palette_idx that represents "transparent"
    in this rendering (typically the index magenta routed to, == 0)."""
    from lock_palette_slots import oklab_dist

    # Build full remap for all 32 indices.
    full = {}
    for pi in range(len(palette_rgb_lookup)):
        if pi == transparent_idx:
            full[pi] = 0
        elif pi in direct_remap:
            full[pi] = direct_remap[pi]
        else:
            # Nearest in OKLab among chosen_pal
            target = palette_rgb_lookup[pi]
            best_pi = min(chosen_pal,
                          key=lambda c: oklab_dist(target, palette_rgb_lookup[c]))
            full[pi] = direct_remap[best_pi]

    return bytes(full[p] for p in pixel_array)


def rebuild_cost_body(orig_body, new_palette_table, frame_pixel_remaps,
                      num_colors):
    """Rebuild a COST chunk body with the new palette table + re-encoded
    RLE pixel data per frame. Frames whose new RLE fits in their original
    footprint are written in place; frames that don't fit are appended
    to the end of the body and every frame_table entry referencing the
    old offset is rewritten to point at the new appended location.

    The returned body may be LONGER than orig_body; the caller is
    responsible for handling the chunk-size delta (LFLF/LECF size
    updates, DCOS offset shifts, etc.).

    `frame_pixel_remaps`: dict {frame_data_off_in_body: indexed_pixels}
       giving the per-frame palette-table-indexed pixel bytes.

    Returns the new body bytes."""
    body = bytearray(orig_body)
    # Replace palette table at body[2..2+num_colors]
    body[2:2 + num_colors] = bytes(new_palette_table[:num_colors])

    # Encode every frame's new RLE up front
    new_rles = {off: encode_rle(pixels, num_colors)
                for off, pixels in frame_pixel_remaps.items()}

    # Compute each frame's original footprint = (next sorted offset) - off.
    sorted_offs = sorted(new_rles.keys())
    bounds = {}
    for i, off in enumerate(sorted_offs):
        nxt = sorted_offs[i + 1] if i + 1 < len(sorted_offs) else len(orig_body)
        bounds[off] = nxt

    overflow_map = {}   # old_off → new_off (for grown frames)
    appended = bytearray()
    for off, new_rle in new_rles.items():
        if off + 12 > len(orig_body):
            continue
        ci = bytes(orig_body[off:off + 12])
        new_total = 12 + len(new_rle)
        space = bounds[off] - off
        if new_total <= space:
            # Fits — write in place; zero-pad the slack
            body[off:off + 12] = ci
            body[off + 12:off + 12 + len(new_rle)] = new_rle
            for p in range(off + 12 + len(new_rle), bounds[off]):
                body[p] = 0
        else:
            # Doesn't fit — append at end of body. Track for offset rewrite.
            new_off = len(body) + len(appended)
            appended.extend(ci)
            appended.extend(new_rle)
            overflow_map[off] = new_off
    body.extend(appended)

    if overflow_map:
        # Update every frame_table entry that references a grown frame.
        # Frame table layout (per ScummVM ClassicCostumeLoader):
        #   body[2+npal+2 .. +34] = 16 frame_table_offsets (LE16, baseptr-rel)
        # For each limb, we walk the frame_table starting at that offset
        # for up to 0x7B codes and rewrite any entry whose value (-6) is
        # in overflow_map. Entries that don't match are left untouched.
        # baseptr is at body-6, so a stored value V points at body[V-6].
        # New offset must satisfy 0 <= V-6 < 65536 (LE16 + baseptr offset).
        after_pal = 2 + num_colors
        if after_pal + 34 > len(body):
            return bytes(body)
        # Sanity: any new offset > 65529 (≈64KB) would overflow LE16
        too_big = [off for off in overflow_map.values() if off > 65529]
        if too_big:
            raise RuntimeError(
                f"COST body overflow: appended frame at offset {max(too_big)} "
                f"exceeds 16-bit baseptr addressing")
        frame_table_offsets = struct.unpack(
            '<16H', body[after_pal + 2:after_pal + 34])
        for limb in range(16):
            ft_start = frame_table_offsets[limb] - 6  # body offset
            if ft_start < 0 or ft_start >= len(body):
                continue
            for code in range(0, 0x7B):
                p = ft_start + code * 2
                if p + 2 > len(body):
                    break
                v = struct.unpack('<H', body[p:p + 2])[0]
                old_data_off = v - 6
                if old_data_off in overflow_map:
                    new_data_off = overflow_map[old_data_off]
                    body[p:p + 2] = struct.pack('<H', new_data_off + 6)
    return bytes(body)
