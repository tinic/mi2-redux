#!/usr/bin/env python3
"""Re-encode OBIM frame SMAPs against a new shared palette.

Inputs:
  - atlas_indexed: bytes of length atlas_w * atlas_h, each entry an index
    0..31 produced by png2amiga --output-indexed with all 32 palette slots
    locked to the final shared palette and DEFAULT dither (Floyd-Steinberg).
    The palette was selected by joint --best with dithering on, so the
    re-encode must dither too — anything else fights the quantizer's choice.
    We never do Python OKLab NN-remap on bulk pixel data; png2amiga is the
    source of truth.
  - atlas_w: width of the atlas in pixels (so we can index atlas_indexed
    via (y * atlas_w + x))
  - atlas frame layout (from PyTexturePacker JSON)
  - transp_masks: per-frame transparency mask from Amiga OBIM decode
  - TRNS sentinel value (default 1, matches MI2 room TRNS)

For each frame:
  Extract indexed pixel block from atlas at (x, y, w, h).
  For each pixel:
    if transparency mask says transparent → SMAP value = TRNS_VALUE
    else → atlas_indexed[(y+py) * atlas_w + (x+px_x)]
  Encode strips using existing ZIGZAG_H5 encoder; emit codec byte 25 for fully-opaque
  strips and codec 45 (ZIGZAG_HT5) for strips with any transparent pixel.

Returns: dict mapping (obj_id, 'IM01' etc.) → bytes (new SMAP body).
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from encode_amiga import encode_zigzag_h
from lecf_repack import le32_pack


TRNS_VALUE = 1


def encode_obim_strip(strip_indices, height, has_transparent, codec_h=25, codec_ht=45):
    """Encode an 8 × height strip of indices with ZIGZAG_H5 bit-stream.
    Use codec 45 (transparent variant) if any pixel == TRNS_VALUE."""
    body = encode_zigzag_h(bytes(strip_indices), height, 5)
    return bytes([codec_ht if has_transparent else codec_h]) + body


def encode_obim_smap(indexed_pixels, magenta_mask, w, h):
    """Build a new SMAP body for an OBIM frame.

    indexed_pixels: list/bytes of length w*h, each value 0..31 (already remapped, with
                    TRNS_VALUE for any originally-transparent pixel)
    magenta_mask: list/bytes of length w*h, 1 = originally transparent
    """
    if w % 8 != 0:
        raise ValueError(f"OBIM width {w} not multiple of 8")
    num_strips = w // 8
    strip_chunks = []
    for sx in range(num_strips):
        # Extract this strip's 8×h pixels (row-major)
        strip = bytearray(8 * h)
        any_t = False
        for y in range(h):
            for x in range(8):
                px_idx = y * w + sx * 8 + x
                v = indexed_pixels[px_idx]
                strip[y*8 + x] = v
                if magenta_mask[px_idx]:
                    any_t = True
        encoded = encode_obim_strip(bytes(strip), h, any_t)
        strip_chunks.append(encoded)

    # Build SMAP body: numStrips u32 LE offsets + concatenated strip bytes
    body = bytearray()
    body += b'\x00' * (num_strips * 4)
    starts = []
    for sb in strip_chunks:
        starts.append(8 + len(body))  # offset is from start of SMAP CHUNK (incl 8-byte header)
        body += sb
    for i, off in enumerate(starts):
        body[i*4:(i+1)*4] = le32_pack(off)
    return bytes(body)


def build_obim_replacements(atlas_indexed, atlas_w, atlas_layout, transp_masks,
                            layout_paths_by_obj, trns_value=1):
    """For each frame in the atlas layout, encode a new SMAP.

    atlas_indexed: bytes of length atlas_w * atlas_h — already nearest-colour
                   mapped by png2amiga (--output-indexed --dither none with
                   all 32 palette slots locked).
    atlas_w: width of the atlas (rows pitch for atlas_indexed).
    atlas_layout: dict {filename: {frame: {x,y,w,h}, ...}, ...} (PyTexturePacker JSON)
    transp_masks: dict (oid, frame) → bytes of length w*h (1 = transparent),
                  derived from decoding the Amiga OBIM frame and looking for
                  pixels == trns_value.
    layout_paths_by_obj: dict mapping basename → (oid, frame_name)
    """
    global TRNS_VALUE
    TRNS_VALUE = trns_value
    out = {}
    frames = atlas_layout['frames']
    for fname, info in frames.items():
        if fname not in layout_paths_by_obj:
            continue
        oid, frame_name = layout_paths_by_obj[fname]
        rect = info['frame']
        x, y, w, h = rect['x'], rect['y'], rect['w'], rect['h']
        if w % 8 != 0:
            print(f"  [skip] {fname}: width {w} not multiple of 8")
            continue
        mmask = transp_masks.get((oid, frame_name))
        if mmask is None:
            mmask = bytes(w * h)  # no transparency info → assume fully opaque
        # Crop the indexed atlas at (x, y, w, h) and overlay transparency.
        indexed = bytearray(w * h)
        for py in range(h):
            atlas_row = (y + py) * atlas_w + x
            for px_x in range(w):
                pi = py * w + px_x
                if mmask[pi]:
                    indexed[pi] = trns_value
                else:
                    indexed[pi] = atlas_indexed[atlas_row + px_x]
        smap = encode_obim_smap(bytes(indexed), bytes(mmask), w, h)
        out[(oid, frame_name)] = smap
    return out


def decode_obim_transparency(disk_path, room_id, trns_value=1):
    """Decode each OBIM frame in the room and return per-frame transparency masks
    (where the Amiga pixel == trns_value).

    Returns: dict (oid, frame_name) → (mask_bytes, w, h)
    """
    import struct
    sys.path.insert(0, os.path.dirname(__file__))
    from decode_amiga_room import load, be32, name as ck_name, walk_rooms, find_chunk
    from decode_obim import decode_smap
    d = load(disk_path)
    rooms = list(walk_rooms(d))
    ro = next(o for r, o in rooms if r == room_id)
    rsz = be32(d, ro+4)
    out = {}
    p = ro + 8
    end = ro + rsz
    while p < end:
        nm_ck = ck_name(d, p)
        if not all(32 <= c < 127 for c in d[p:p+4]):
            break
        sz = be32(d, p+4)
        if nm_ck == 'OBIM':
            ip = p + 8
            ipend = p + sz
            obj_id = None
            w = h = 0
            while ip < ipend:
                inm = ck_name(d, ip)
                isz = be32(d, ip+4)
                if inm == 'IMHD':
                    body = d[ip+8:ip+isz]
                    obj_id = struct.unpack('<H', body[0:2])[0]
                    w = struct.unpack('<H', body[12:14])[0]
                    h = struct.unpack('<H', body[14:16])[0]
                elif inm.startswith('IM') and len(inm) == 4 and obj_id is not None and w > 0 and h > 0:
                    smap_o, smap_s = find_chunk(d, ip+8, ip+isz, 'SMAP')
                    if smap_o is not None:
                        try:
                            indexed, _ = decode_smap(d, smap_o, w, h)
                            mask = bytes(1 if v == trns_value else 0 for v in indexed)
                            out[(obj_id, inm)] = (mask, w, h)
                        except Exception as e:
                            print(f"    [warn] decode {obj_id} {inm}: {e}")
                ip += isz
        p += sz
    return out
