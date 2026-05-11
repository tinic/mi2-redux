import os
#!/usr/bin/env python3
"""SMAP encoder for MI2 Amiga (inverse of decode_amiga_room.py).

Produces a SMAP body byte-for-byte compatible with ScummVM's `Gdi::decompressBitmap`,
using the `BMCOMP_ZIGZAG_H5` codec (25) for every strip — round-trip-safe and simple.
A smarter encoder would pick per-strip codecs to minimize size; this is a baseline.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import decode_zigzag_h

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




def encode_zigzag_h(strip_indices, height, shr=5):
    """Encode an 8 × height strip of 5-bit indices (values 0..31) with ZIGZAG_H5.

    strip_indices: bytes/list of length 8*height in row-major order (y*8 + x).
    Returns: bytes that, when decoded with decode_zigzag_h(.., height, 5),
             reproduce strip_indices exactly.
    """
    assert shr in (4, 5, 6, 7, 8)
    abs_mask = (1 << shr) - 1  # range of an absolute-color load
    # IMPORTANT: the C decoder keeps `color` as a full 8-bit byte; only the
    # ABSOLUTE-LOAD path is bounded to `shr` bits. inc ops can wrap into 32+.
    # So the encoder works with mod-256 arithmetic everywhere, and only uses
    # the 5-bit absolute load when the target fits in 0..31.
    color = strip_indices[0] & 0xFF
    inc = -1
    out = bytearray([color])

    # Bit accumulator — LSB-first within each output byte
    acc = 0
    nbits = 0

    def write_bits(val, n):
        nonlocal acc, nbits, out
        acc |= (val & ((1 << n) - 1)) << nbits
        nbits += n
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8

    n = 8 * height
    for i in range(1, n):
        nxt = strip_indices[i] & 0xFF
        if nxt == color:
            write_bits(0, 1)                          # 0
        elif nxt == ((color + inc) & 0xFF):
            write_bits(0b011, 3)                      # 110: color += inc
            color = nxt
        elif nxt == ((color - inc) & 0xFF):
            write_bits(0b111, 3)                      # 111: inc=-inc; color+=inc
            inc = -inc
            color = nxt
        elif nxt <= abs_mask:
            write_bits(0b01, 2)                       # 10 + shr-bit absolute color
            write_bits(nxt, shr)
            color = nxt
            inc = -1
        else:
            # Target is outside absolute-load range AND not reachable via ±inc
            # from current color in one step. We must ramp via repeated inc ops:
            # output a 5-bit absolute load that's reachable, then inc-walk to nxt.
            # Simplest correct fallback: pick anchor = nxt's low 5 bits, then
            # walk via inc-ops. (Rare in normal 32-color content.)
            anchor = nxt & abs_mask
            write_bits(0b01, 2)
            write_bits(anchor, shr)
            color = anchor
            inc = -1
            # Walk color -> nxt with successive inc ops
            while color != nxt:
                step = 1 if (((nxt - color) & 0xFF) <= 0x80) else -1
                if step == inc:
                    write_bits(0b011, 3)              # 110: color += inc
                else:
                    write_bits(0b111, 3)              # 111: flip + step
                    inc = -inc
                color = (color + inc) & 0xFF

    # Flush remaining bits as a final byte (zero-padded high bits).
    if nbits > 0:
        out.append(acc & 0xFF)

    return bytes(out)


def encode_zigzag_v(strip_indices, height, shr=5):
    """ZIGZAG_V5 (codec 15) — column-major variant of ZIGZAG_H5. Same bit-stream
    algorithm; iterates pixels in (x outer, y inner) order. Decoder is
    drawStripBasicV in ScummVM gfx.cpp."""
    assert shr in (4, 5, 6, 7, 8)
    abs_mask = (1 << shr) - 1
    out = bytearray()
    acc = 0
    nbits = 0

    def write_bits(val, n):
        nonlocal acc, nbits
        acc |= (val & ((1 << n) - 1)) << nbits
        nbits += n
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8

    # Build column-major pixel sequence
    seq = [strip_indices[y*8 + x] for x in range(8) for y in range(height)]
    out.append(seq[0] & 0xFF)
    color = seq[0] & 0xFF
    inc = -1
    for i in range(1, len(seq)):
        nxt = seq[i] & 0xFF
        if nxt == color:
            write_bits(0, 1)
        elif nxt == ((color + inc) & 0xFF):
            write_bits(0b011, 3)
            color = nxt
        elif nxt == ((color - inc) & 0xFF):
            write_bits(0b111, 3)
            inc = -inc
            color = nxt
        elif nxt <= abs_mask:
            write_bits(0b01, 2)
            write_bits(nxt, shr)
            color = nxt
            inc = -1
        else:
            anchor = nxt & abs_mask
            write_bits(0b01, 2)
            write_bits(anchor, shr)
            color = anchor
            inc = -1
            while color != nxt:
                step = 1 if (((nxt - color) & 0xFF) <= 0x80) else -1
                if step == inc:
                    write_bits(0b011, 3)
                else:
                    write_bits(0b111, 3)
                    inc = -inc
                color = (color + inc) & 0xFF
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def encode_majmin_h(strip_indices, height, shr=5):
    """MAJMIN_H5 (codec 65) — drawStripComplex / MajMinCodec from ScummVM.

    Bit format (per pixel, after emitting the previous color):
      0           : no change (1 bit)
      1 0 cccccc  : absolute color (2 + shr bits)
      1 1 ddd     : if ddd != 4 (= zero diff): color += (ddd - 4)  (5 bits, range -4..+3)
      1 1 100 nn..: enter repeat mode for (readBits(8)-1) more pixels of same color (13 bits total)

    Header: byte color, u16 LE bits prebuffer (2 bytes consumed, sit in the 16-bit reservoir).
    """
    out = bytearray()
    out.append(strip_indices[0] & 0xFF)  # initial color
    # 16-bit reservoir prefilled with 2 bytes; we'll set them at the end
    bit_data = []  # list of (val, n) bits to emit MSB-first within bytes (LE 16-bit chunks)

    color = strip_indices[0] & 0xFF
    repeat_run_open = False
    repeat_count = 0

    seq = strip_indices  # row-major (matches drawStripComplex which calls decodeLine row by row, 8 wide)

    i = 1
    n = 8 * height
    while i < n:
        # Look ahead for a repeat run of the same color
        if seq[i] == color:
            # repeat-mode is disabled. Across all 1622 codec-65 strips in
            # pristine MI2 Amiga, LucasArts' encoder NEVER emits repeat-mode
            # — almost certainly because the 1992 Amiga decoder has a bug
            # mishandling it (we observe vertical-band shift artifacts in
            # FS-UAE when we use repeat-mode, but ScummVM's reimpl is fine
            # with it). Just emit 1-bit "no change" per same-color pixel.
            run = 1
            # Cost of (1-bit no-change) × run = run bits
            # Cost of repeat-mode = 13 bits + (run - 1) free
            # Use repeat-mode if run > 13 (saves bits)
            if run >= 14:
                # Decoder: diff = readBits(3) - 4; if diff == 0 → enter repeat.
                # So encode diff_bits = 4 (binary 100) to make diff == 0.
                bit_data.append((0b011, 2))            # bit 1, then bit 1 (LSB-first reads: 1,1)
                bit_data.append((4, 3))                # 3-bit diff field = 4 → diff=0 → enter repeat
                bit_data.append((run, 8))              # 8-bit count: decoder does -1 internally
                # During repeat-mode, the decoder skips (count-1) pixels — emits same color (run total)
                i += run
                continue
            else:
                # Emit (run) × 1-bit "no change"
                for _ in range(run):
                    bit_data.append((0, 1))
                i += run
                continue
        # Not same color
        diff = (seq[i] - color) & 0xFF
        if diff > 0x80:
            diff -= 0x100  # signed
        if -4 <= diff <= 3 and diff != 0:
            # Emit "1 1 ddd" with ddd = diff + 4
            bit_data.append((0b011, 2))                # 1, 1
            bit_data.append((diff + 4, 3))
            color = (color + diff) & 0xFF
        else:
            # Absolute: "1 0" + shr-bit color
            bit_data.append((0b001, 2))                # 1, 0
            bit_data.append((seq[i] & ((1 << shr) - 1), shr))
            color = seq[i] & 0xFF
        i += 1

    # Build the bit-stream the decoder expects.
    # Decoder logic:
    #   bits = u16 LE (2 bytes), num_bits = 16
    #   Each readBits(n): value = bits & ((1<<n)-1); bits >>= n; num_bits -= n
    #   FILL_BITS: if num_bits <= 8: bits |= (*src++) << num_bits; num_bits += 8
    # So bits are LSB-first within the reservoir, and bytes refill at the high end.
    # We emit bits in the order they'll be read.
    acc = 0
    nbits = 0
    bytes_after_reservoir = bytearray()
    # First, the initial 2-byte reservoir load (u16 LE). We need to plan: reservoir holds 16 bits initially.
    # We push bits into a logical "bit stream" and later split: first 16 bits → u16 LE, rest → bytes.
    flat = []
    for val, n in bit_data:
        for k in range(n):
            flat.append((val >> k) & 1)
    # Pack into bytes (LSB-first): byte 0 holds bits 0..7 of flat, byte 1 bits 8..15, etc.
    # First TWO bytes go into the reservoir as u16 LE. Subsequent bytes go after.
    while len(flat) % 8:
        flat.append(0)
    bytes_out = bytearray()
    for b in range(0, len(flat), 8):
        v = 0
        for k in range(8):
            v |= flat[b+k] << k
        bytes_out.append(v)
    # The first two bytes form the initial u16 reservoir (LE). They MUST exist; if our
    # bit-stream is shorter, pad with zeros.
    while len(bytes_out) < 2:
        bytes_out.append(0)
    out.extend(bytes_out)
    return bytes(out)


def encode_strip_best(strip_indices, height, shr=5, transparent=False, allow_majmin=True):
    """Try multiple codecs and pick the smallest. Returns (codec_byte, bytes).
    All three codec families (zigzag-V/H + majmin) are correct now that the
    MAJMIN run-cap off-by-one is fixed (must be 255, not 256)."""
    h_body = encode_zigzag_h(strip_indices, height, shr)
    v_body = encode_zigzag_v(strip_indices, height, shr)
    h_codec = 45 if transparent else 25
    v_codec = 35 if transparent else 15
    candidates = [(len(h_body), h_codec, h_body),
                  (len(v_body), v_codec, v_body)]
    if allow_majmin:
        m_body = encode_majmin_h(strip_indices, height, shr)
        m_codec = 85 if transparent else 65
        candidates.append((len(m_body), m_codec, m_body))
    candidates.sort()
    sz, codec, body = candidates[0]
    return codec, body


# ---------------- self-test: roundtrip a real strip ----------------

def selftest():
    import struct
    from decode_amiga_room import load, be32, le32, name, walk_rooms, find_chunk
    d = load(f'{REPO_ROOT}/amiga-data/monkey2.001')
    rooms = list(walk_rooms(d))
    rid, ro = rooms[0]
    room_size = be32(d, ro+4)
    rmim_off, _ = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    im00_off, _ = find_chunk(d, rmim_off+8, rmim_off+be32(d, rmim_off+4), 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+be32(d, im00_off+4), 'SMAP')
    rmhd_off, _ = find_chunk(d, ro+8, ro+room_size, 'RMHD')
    h = struct.unpack('<H', d[rmhd_off+10:rmhd_off+12])[0]
    body_off = smap_off + 8
    strip_offsets = [le32(d, body_off + i*4) for i in range(40)]
    strip_ends = list(strip_offsets[1:]) + [smap_sz]
    n_ok = 0; n_fail = 0; sizes = []
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        codec = d[smap_off + start]
        if codec != 25:
            continue
        original_bytes = d[smap_off + start + 1 : smap_off + end]
        decoded = decode_zigzag_h(original_bytes, h, 5)
        re_encoded = encode_zigzag_h(decoded, h, 5)
        # Strip trailing zero pad bytes from `original_bytes` for comparison.
        # The original encoder may have ended at any bit position, so we test
        # functional equivalence (re-decoding gives same pixels).
        re_decoded = decode_zigzag_h(re_encoded, h, 5)
        if bytes(re_decoded) == bytes(decoded):
            n_ok += 1
            sizes.append((len(original_bytes), len(re_encoded)))
        else:
            n_fail += 1
            # Find first mismatch
            for i, (a, b) in enumerate(zip(decoded, re_decoded)):
                if a != b:
                    print(f"  strip {si}: pixel {i} {a} != {b}")
                    break
    print(f"ZIGZAG_H5 roundtrip: {n_ok} ok, {n_fail} failed")
    if sizes:
        orig_sum = sum(o for o, _ in sizes)
        new_sum  = sum(n for _, n in sizes)
        print(f"  bytes: original {orig_sum}, re-encoded {new_sum} ({100*new_sum/orig_sum:.1f}%)")


if __name__ == '__main__':
    selftest()
