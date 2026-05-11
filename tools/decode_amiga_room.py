import os
#!/usr/bin/env python3
"""Quick & dirty SCUMM v5 / MI2 Amiga BM decoder.

Goal: render one room as PNG to compare against png2amiga --best output.
Not production code — just enough to validate the format and show the user
the actual original Amiga rendering.

Codecs handled (so far):
  25 = BMCOMP_ZIGZAG_H5  (drawStripBasicH with shr=5, 5-bit color)
  65 = BMCOMP_MAJMIN_H5  (drawStripComplex with MajMinCodec, shr=5)
"""
import struct, sys, os
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


XOR = 0x69


def load(path):
    return bytes(b ^ XOR for b in open(path, 'rb').read())


def be32(d, p): return struct.unpack('>I', d[p:p+4])[0]
def le32(d, p): return struct.unpack('<I', d[p:p+4])[0]
def le16(d, p): return struct.unpack('<H', d[p:p+2])[0]
def name(d, p): return d[p:p+4].decode('ascii', errors='replace')


def walk_rooms(d):
    """Yield (room_id, abs_offset_into_d) for each ROOM in this LECF file."""
    assert name(d, 0) == 'LECF'
    assert name(d, 8) == 'LOFF'
    n = d[16]
    for i in range(n):
        rid = d[17 + i*5]
        off = le32(d, 17 + i*5 + 1)
        yield rid, off


def find_chunk(d, start, end, want):
    """Find first chunk named `want` between [start, end) at top level."""
    p = start
    while p < end:
        nm = name(d, p)
        sz = be32(d, p+4)
        if nm == want:
            return p, sz
        p += sz
    return None, 0


# ---------------- decoder for ZIGZAG_V5 (codec 15) ----------------

def decode_zigzag_v(strip_bytes, height, shr):
    """Reproduce drawStripBasicV from ScummVM gfx.cpp.

    Same algorithm as H but iterates column-major: outer x, inner y.
    """
    out = bytearray(8 * height)
    src = list(strip_bytes) + [0] * 8
    si = 0
    color = src[si]; si += 1
    bits = src[si]; si += 1
    cl = 8
    inc = -1
    mask = (0xFF >> (8 - shr))

    def fill():
        nonlocal bits, cl, si
        if cl <= 8:
            bits |= src[si] << cl
            si += 1
            cl += 8

    def read_bit():
        nonlocal bits, cl
        cl -= 1
        b = bits & 1
        bits >>= 1
        return b

    for x in range(8):
        for y in range(height):
            out[y*8 + x] = color & 0xFF
            fill()
            if not read_bit():
                pass
            elif not read_bit():
                fill()
                color = bits & mask
                bits >>= shr
                cl -= shr
                inc = -1
            else:
                if not read_bit():
                    color = (color + inc) & 0xFF
                else:
                    inc = -inc
                    color = (color + inc) & 0xFF

    return out


# ---------------- decoder for MAJMIN_H5 (codec 65) ----------------

def decode_majmin_h(strip_bytes, height, shr):
    """Reproduce MajMinCodec::decodeLine from ScummVM, called row-by-row
    for 8 pixels per row (drawStripComplex).
    """
    src = list(strip_bytes) + [0] * 8
    out = bytearray(8 * height)
    color = src[0]
    bits = src[1] | (src[2] << 8)
    num_bits = 16
    si = 3
    repeat_mode = False
    repeat_count = 0

    def fill():
        nonlocal bits, num_bits, si
        if num_bits <= 8:
            bits |= src[si] << num_bits
            si += 1
            num_bits += 8

    def read_bits(n):
        nonlocal bits, num_bits
        fill()
        v = bits & ((1 << n) - 1)
        num_bits -= n
        bits >>= n
        return v

    for y in range(height):
        for x in range(8):
            out[y*8 + x] = color & 0xFF
            if not repeat_mode:
                if read_bits(1):
                    if read_bits(1):
                        diff = read_bits(3) - 4
                        if diff:
                            color = (color + diff) & 0xFF
                        else:
                            repeat_mode = True
                            repeat_count = read_bits(8) - 1
                    else:
                        color = read_bits(shr)
            else:
                repeat_count -= 1
                if repeat_count == 0:
                    repeat_mode = False

    return out


# ---------------- decoder for ZIGZAG_H5 (codec 25) ----------------

def decode_zigzag_h(strip_bytes, height, shr):
    """Reproduce drawStripBasicH from ScummVM gfx.cpp.

    8-pixel-wide strip, height rows, decoded into a flat list of color indices.
    Output is row-major: out[y*8 + x].
    """
    out = bytearray(8 * height)
    # ScummVM reads up to a few bytes past the end of the strip without
    # bounds-checking — works in C because the next strip's data sits right
    # after. Pad here so we don't crash on the same access pattern.
    src = list(strip_bytes) + [0] * 8
    si = 0
    color = src[si]; si += 1
    bits = src[si]; si += 1
    cl = 8
    inc = -1
    mask = (0xFF >> (8 - shr))

    def fill():
        nonlocal bits, cl, si
        if cl <= 8:
            bits |= src[si] << cl
            si += 1
            cl += 8

    def read_bit():
        nonlocal bits, cl
        cl -= 1
        b = bits & 1
        bits >>= 1
        return b

    for y in range(height):
        for x in range(8):
            out[y*8 + x] = color & 0xFF
            fill()
            if not read_bit():
                pass  # repeat color
            elif not read_bit():
                # absolute color: read shr more bits
                fill()
                color = bits & mask
                bits >>= shr
                cl -= shr
                inc = -1
            else:
                if not read_bit():
                    color = (color + inc) & 0xFF
                else:
                    inc = -inc
                    color = (color + inc) & 0xFF

    return out


# ---------------- main ----------------

def main(disk_idx, room_idx_in_file=0, out_path='/tmp/room.png',
         palette_source='clut', palette_mod=0):
    path = f'{REPO_ROOT}/amiga-data/monkey2.{disk_idx:03d}'
    d = load(path)
    rooms = list(walk_rooms(d))
    print(f"Disk {disk_idx}: rooms {[r[0] for r in rooms]}")
    rid, ro = rooms[room_idx_in_file]
    room_size = be32(d, ro+4)
    print(f"Decoding room {rid} @0x{ro:x} size={room_size}")

    # Find RMHD: width, height
    p = ro+8; rmhd_off, _ = find_chunk(d, p, ro+room_size, 'RMHD')
    w = le16(d, rmhd_off+8); h = le16(d, rmhd_off+10)
    print(f"  RMHD: {w}x{h}")

    # Palette
    clut_off, clut_sz = find_chunk(d, ro+8, ro+room_size, 'CLUT')
    epal_off, epal_sz = find_chunk(d, ro+8, ro+room_size, 'EPAL')
    print(f"  CLUT size={clut_sz} ({(clut_sz-8)//3} colors)")
    print(f"  EPAL size={epal_sz}")

    if palette_source == 'clut':
        pal_body = d[clut_off+8 : clut_off+clut_sz]
        pal = [tuple(pal_body[i*3:i*3+3]) for i in range(256)]
    elif palette_source == 'epal':
        # EPAL is 256 bytes of remap, often. Or 16x16. Try as 32-color 4bpp.
        # First try: 32 colors x 3 bytes (96 bytes) at end of EPAL.
        body = d[epal_off+8 : epal_off+epal_sz]
        # MI1 Amiga uses fixed palette; for MI2, body[0..96] might be 32 colors RGB
        # Try interpreting body[0..96] as 32 RGB triples
        if len(body) >= 96:
            pal = [tuple(body[i*3:i*3+3]) for i in range(32)] + [(0,0,0)] * (256-32)
        else:
            pal = [(i,i,i) for i in range(256)]
    else:
        pal = [(i,i,i) for i in range(256)]

    # Find RMIM/IM00/SMAP
    rmim_off, rmim_sz = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')
    print(f"  RMIM @0x{rmim_off:x} IM00 @0x{im00_off:x} SMAP @0x{smap_off:x} sz={smap_sz}")

    # SMAP: numStrips u32 LE offsets (relative to SMAP chunk start, including header)
    num_strips = w // 8
    body_off = smap_off + 8
    strip_offsets = [le32(d, body_off + i*4) for i in range(num_strips)]
    # Compute strip end positions (byte ranges)
    strip_ends = list(strip_offsets[1:]) + [smap_sz]
    # Image buffer (indexed)
    img = bytearray(w * h)
    skipped = 0
    codecs_seen = {}
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        codec = d[smap_off + start]
        codecs_seen[codec] = codecs_seen.get(codec, 0) + 1
        strip_bytes = d[smap_off + start + 1 : smap_off + end]
        shr = codec % 10
        pix = None
        if 14 <= codec <= 18:    # ZIGZAG_V4..V8
            pix = decode_zigzag_v(strip_bytes, h, shr)
        elif 24 <= codec <= 28:  # ZIGZAG_H4..H8
            pix = decode_zigzag_h(strip_bytes, h, shr)
        elif 64 <= codec <= 68:  # MAJMIN_H4..H8
            pix = decode_majmin_h(strip_bytes, h, shr)
        if pix is not None:
            for y in range(h):
                for x in range(8):
                    img[y*w + si*8 + x] = pix[y*8 + x]
        else:
            skipped += 1
    print(f"  codecs: {codecs_seen}, skipped {skipped} strips")

    # Render PNG with palette + paletteMod
    rgb = bytearray(w * h * 3)
    for i, idx in enumerate(img):
        r,g,b = pal[(idx + palette_mod) & 0xFF]
        rgb[i*3] = r; rgb[i*3+1] = g; rgb[i*3+2] = b
    Image.frombytes('RGB', (w, h), bytes(rgb)).save(out_path)
    print(f"  -> {out_path}")


if __name__ == '__main__':
    # Try first room of disk 1, with default CLUT palette
    main(1, 0, '/tmp/room_d1r0_clut0.png',  palette_source='clut', palette_mod=0)
    main(1, 0, '/tmp/room_d1r0_clut16.png', palette_source='clut', palette_mod=16)
    main(1, 0, '/tmp/room_d1r0_epal0.png',  palette_source='epal', palette_mod=0)
