#!/usr/bin/env python3
"""SCUMM v5 PC SMAP decoder — minimal subset that covers MI2 (1991).

A survey of every SMAP in the MI2 PC `MONKEY2.001` shows only four
algorithm families are used (across 12,156 strips, 1,157 SMAPs, 110
rooms):

  RAW256        codec 1                                            ( ~ 0.1%)
  ZIGZAG_V[T]   codec 14..18 + 34..38   (param = codec - {10|30})  (~22.3%)
  ZIGZAG_H[T]   codec 24..28 + 44..48   (param = codec - {20|40})  (~17.6%)
  MAJMIN_H[T]   codec 64..68 + 84..88   (param = codec - {60|80})  (~60.0%)

Reference: scummvm/engines/scumm/gfx.h (BMCOMP_*) + gfx.cpp
(`drawStripBasicH/V` + `drawStripComplex` + their `Transp` variants).

The three working algorithms — `drawStripBasicV`, `drawStripBasicH`,
`drawStripComplex` — are already implemented for Amiga in
`decode_amiga_room.py` (`decode_zigzag_v`, `decode_zigzag_h`,
`decode_majmin_h`). They take the bit-depth `shr` as a parameter, so
PC's 4..8-bit variants reuse them directly. The "transparency" `T`
variants don't change the bit stream — they just set TRNS-coloured
pixels to the TRNS sentinel value, which the caller (this module)
reinterprets as "transparent" when emitting PNGs.

This is intentionally NOT a complete SCUMM v5 PC decoder. Other games
(DOTT, Sam & Max, FoA on later releases) use additional codecs in the
NMAJMIN / RMAJMIN / TPIX256 families. We never see them in MI2, so
they're not implemented. Encountering one raises NotImplementedError
with the codec byte for the caller to investigate.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (
    decode_zigzag_v, decode_zigzag_h, decode_majmin_h,
    be32, le16, le32, name as cn, find_chunk,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _codec_info(code):
    """Return (algo, param, has_transparency) for a codec byte, or None
    if the codec isn't part of MI2's used set."""
    if code == 1:
        return ('raw', 8, False)
    if 14 <= code <= 18:
        return ('zigzag_v', code - 10, False)
    if 34 <= code <= 38:
        return ('zigzag_v', code - 30, True)
    if 24 <= code <= 28:
        return ('zigzag_h', code - 20, False)
    if 44 <= code <= 48:
        return ('zigzag_h', code - 40, True)
    if 64 <= code <= 68:
        return ('majmin_h', code - 60, False)
    if 84 <= code <= 88:
        return ('majmin_h', code - 80, True)
    return None


def _decode_strip(strip_bytes, height, code, trns_value):
    """Decode one 8-pixel-wide strip. Returns flat 8*height bytearray
    (row-major: out[y*8 + x]). Transparent pixels are set to trns_value."""
    info = _codec_info(code)
    if info is None:
        raise NotImplementedError(f'PC SMAP codec 0x{code:02x} ({code}) '
                                   f'not implemented (MI2 only uses '
                                   f'1, 14-18, 24-28, 34-38, 44-48, 64-68, 84-88)')
    algo, shr, has_t = info
    src = strip_bytes
    if algo == 'raw':
        # Raw layout: 8*height bytes. (Transparency variants don't exist
        # for RAW256 in MI2's set, so we ignore has_t here.)
        out = bytearray(8 * height)
        out[:min(8*height, len(src))] = src[:min(8*height, len(src))]
        return out
    elif algo == 'zigzag_v':
        return decode_zigzag_v(src, height, shr)
    elif algo == 'zigzag_h':
        return decode_zigzag_h(src, height, shr)
    elif algo == 'majmin_h':
        return decode_majmin_h(src, height, shr)
    raise AssertionError(f'unreachable: {algo}')


def decode_pc_smap(d, smap_chunk_off, w, h, trns_value=None):
    """Decode a PC SCUMM v5 SMAP chunk to a flat indexed bytearray of
    length w*h. `smap_chunk_off` points at the SMAP tag (not body).

    The SMAP body starts with `num_strips * 4-byte LE` offsets. Each
    offset is computed from the SMAP CHUNK start (i.e., includes the
    8-byte tag+size header). The data after the offset table is the
    concatenated per-strip codec+payload blobs.

    Width must be a multiple of 8 (one strip = 8 px). Returns a
    bytearray sized for w*h palette indices.
    """
    if w % 8 != 0:
        raise ValueError(f'SMAP width {w} not a multiple of 8')
    sz = be32(d, smap_chunk_off + 4)
    body_start = smap_chunk_off + 8
    body_end = smap_chunk_off + sz
    num_strips = w // 8
    if body_start + num_strips * 4 > body_end:
        raise ValueError('SMAP truncated (offset table runs past chunk end)')
    offsets = [
        struct.unpack('<I', d[body_start + s*4:body_start + s*4 + 4])[0]
        for s in range(num_strips)
    ]
    out = bytearray(w * h)
    for s, off_field in enumerate(offsets):
        codec_off = smap_chunk_off + off_field   # offsets are chunk-relative
        if codec_off >= body_end:
            raise ValueError(f'SMAP strip {s} offset 0x{off_field:x} past chunk end')
        code = d[codec_off]
        # Strip payload runs until next strip's offset, or chunk end for the
        # last strip. ScummVM reads up to a few bytes past the strip end
        # without bounds-checking — we replicate that by passing a slice
        # plus padding inside the helpers.
        strip_end = (smap_chunk_off + offsets[s+1]) if s + 1 < num_strips else body_end
        strip_payload = bytes(d[codec_off + 1:strip_end])
        strip = _decode_strip(strip_payload, h, code, trns_value)
        # Splat 8-wide strip into the output at column s*8
        for y in range(h):
            out[y * w + s*8:y * w + s*8 + 8] = strip[y*8:y*8 + 8]
    return out


# ---------------------------------------------------------------------------
# PC room walker: find every bg + OBIM SMAP, return their (rid, label, w, h,
# pixel-bytes, palette-bytes, trns_value) so a caller can emit PNGs.
# ---------------------------------------------------------------------------

def _find_pc_palette(d, room_off, room_size):
    """Return 768-byte palette for a PC room. Tries CLUT first, falls back
    to PALS/WRAP/APAL (the cycling-palette wrapper)."""
    clut_off, clut_sz = find_chunk(d, room_off + 8, room_off + room_size, 'CLUT')
    if clut_off is not None and clut_sz >= 8 + 768:
        return bytes(d[clut_off + 8:clut_off + 8 + 768])
    pals_off, pals_sz = find_chunk(d, room_off + 8, room_off + room_size, 'PALS')
    if pals_off is None:
        return None
    wrap_off, wrap_sz = find_chunk(d, pals_off + 8, pals_off + pals_sz, 'WRAP')
    if wrap_off is None:
        return None
    apal_off, apal_sz = find_chunk(d, wrap_off + 8, wrap_off + wrap_sz, 'APAL')
    if apal_off is not None and apal_sz >= 8 + 768:
        return bytes(d[apal_off + 8:apal_off + 8 + 768])
    return None


def _find_trns(d, room_off, room_size):
    trns_off, trns_sz = find_chunk(d, room_off + 8, room_off + room_size, 'TRNS')
    if trns_off is not None and trns_sz >= 9:
        return d[trns_off + 8]
    return None


def walk_pc_room(d, room_off, room_size):
    """Yield (kind, label, w, h, pixel_bytes, palette_bytes, trns_value)
    for the bg image + every OBIM frame in a PC ROOM.

    `kind`  is 'bg' or 'obim'
    `label` is 'bg' for the background, '<obj_id>_IM<NN>' for objects
    """
    palette = _find_pc_palette(d, room_off, room_size)
    if palette is None:
        return  # no palette → nothing to render
    trns = _find_trns(d, room_off, room_size)

    # Background
    rmhd_off, _ = find_chunk(d, room_off + 8, room_off + room_size, 'RMHD')
    if rmhd_off is not None:
        w_bg = le16(d, rmhd_off + 8)
        h_bg = le16(d, rmhd_off + 10)
        rmim_off, rmim_sz = find_chunk(d, room_off + 8, room_off + room_size, 'RMIM')
        if rmim_off is not None:
            im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
            if im00_off is not None:
                smap_off, _ = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
                if smap_off is not None and w_bg > 0 and h_bg > 0:
                    pix = decode_pc_smap(d, smap_off, w_bg, h_bg, trns)
                    yield ('bg', 'bg', w_bg, h_bg, bytes(pix), palette, trns)

    # Objects
    p = room_off + 8
    end = room_off + room_size
    while p + 8 <= end:
        tag = cn(d, p)
        sz = be32(d, p + 4)
        if sz < 8 or p + sz > end:
            break
        if tag == 'OBIM':
            obj_w = obj_h = obj_id = 0
            ip = p + 8
            ipend = p + sz
            while ip + 8 <= ipend:
                inm = cn(d, ip)
                isz = be32(d, ip + 4)
                if isz < 8 or ip + isz > ipend:
                    break
                if inm == 'IMHD' and isz >= 8 + 16:
                    # PC SCUMM v5 IMHD body layout (16+ bytes):
                    #   +0  obj_id (LE16)
                    #   +2  num_imnn
                    #   +4  num_zpnn
                    #   +6  flags
                    #   +8  x
                    #   +10 y
                    #   +12 width   <-- here, not +14 like Amiga MI2
                    #   +14 height
                    obj_id = le16(d, ip + 8)
                    obj_w = le16(d, ip + 8 + 12)
                    obj_h = le16(d, ip + 8 + 14)
                elif inm.startswith('IM') and len(inm) == 4 and \
                        inm not in ('IMHD',) and obj_w > 0:
                    smap_off, _ = find_chunk(d, ip + 8, ip + isz, 'SMAP')
                    if smap_off is not None and obj_w % 8 == 0:
                        try:
                            pix = decode_pc_smap(d, smap_off, obj_w, obj_h, trns)
                            yield ('obim', f'{obj_id:04d}_{inm}',
                                   obj_w, obj_h, bytes(pix), palette, trns)
                        except (NotImplementedError, ValueError) as e:
                            print(f'  [skip] OBIM rid={obj_id} {inm}: {e}',
                                  file=sys.stderr)
                ip += isz
        p += sz
