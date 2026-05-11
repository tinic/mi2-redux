#!/usr/bin/env python3
"""V2 demo: take the PC Part I title screen, run png2amiga --best, inject into
the Amiga MI2 disk 1 ADF. Output is a patched ADF ready to boot in FS-UAE.

Steps:
  1. Load ~/mi2-redux/preview/best-from-pc/part1.png  (320x200 RGB, ≤32 unique colors)
  2. Build 32-entry RGB palette + indexed bitmap
  3. Encode each 8×200 strip with encode_zigzag_h (codec 25)
  4. Build new SMAP body (offset table + strip bytes)
  5. Build new CLUT body: keep entries 0..15, write our 32 colors into 16..47,
     leave 48..255 zeroed
  6. Splice SMAP + CLUT into LECF, fix all parent chunk sizes
  7. Write patched monkey2.001 + adfreplace into ADF
"""
import os, struct, sys, subprocess
sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image
from decode_amiga_room import (
    load, be32, le32, le16, name, walk_rooms, find_chunk,
)
from encode_amiga import encode_zigzag_h
from lecf_repack import be32_pack, le32_pack, XOR
from lock_palette_slots import lock_slots

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


SRC_PNG = f'{REPO_ROOT}/preview/best-from-pc/part1.png'
SRC_DISK = f'{REPO_ROOT}/amiga-data/monkey2.001'
PATCHED_DISK = '/tmp/monkey2.001.part1-best'
SRC_ADF = f'{REPO_ROOT}/disks/Monkey Island 2 - LeChuck\'s Revenge v1.0 (1992-04-08)(LucasArts - U.S. Gold)(Disk 01 of 11)[cr Conet].adf'
OUT_ADF = f'{REPO_ROOT}/preview/MonkeyIsland2-Disk01-PART1-BEST.adf'
ADFREPLACE = f'{REPO_ROOT}/tools/adfreplace'


def png_to_indexed(path, target_w=None, target_h=None):
    """Return (indexed_bytes_w*h_row_major, palette_list_of_(r,g,b), width, height).
    Pixels can be ≤32 unique colors for our SMAP target.
    png2amiga writes a 2x preview render — NEAREST downsample to native res."""
    im = Image.open(path).convert('RGB')
    if target_w is not None and target_h is not None and im.size != (target_w, target_h):
        im = im.resize((target_w, target_h), Image.NEAREST)
    w, h = im.size
    raw = im.tobytes()
    palette = {}
    indexed = bytearray(w * h)
    for i in range(w * h):
        rgb = (raw[i*3], raw[i*3+1], raw[i*3+2])
        if rgb not in palette:
            palette[rgb] = len(palette)
            if len(palette) > 32:
                raise RuntimeError(f"PNG has >32 colors after pixel {i}")
        indexed[i] = palette[rgb]
    return bytes(indexed), list(palette.keys()), w, h


def build_smap_body(indexed, w, h, target_size=None):
    """Build a SMAP body picking the smallest codec per strip from
    ZIGZAG_H5 / ZIGZAG_V5 / MAJMIN_H5. If `target_size` is given AND our
    encoded body is smaller, pad with zero bytes to match — this preserves
    LECF offsets so monkey2.000 doesn't need patching (MD5 stays valid)."""
    from encode_amiga import encode_strip_best

    num_strips = w // 8
    strip_bytes_list = []
    for sx in range(num_strips):
        strip = bytearray(8 * h)
        for y in range(h):
            for x in range(8):
                strip[y*8 + x] = indexed[y*w + sx*8 + x]
        codec_byte, enc = encode_strip_best(bytes(strip), h, 5, transparent=False, allow_majmin=True)
        strip_bytes_list.append(bytes([codec_byte]) + enc)
    # Build body
    body = bytearray()
    body += b'\x00' * (num_strips * 4)
    starts = []
    for sb in strip_bytes_list:
        starts.append(8 + len(body))  # offset relative to chunk start
        body += sb
    for i, off in enumerate(starts):
        body[i*4:(i+1)*4] = le32_pack(off)
    if target_size is not None and len(body) < target_size:
        body += b'\x00' * (target_size - len(body))
    return bytes(body)


def build_clut_body(orig_clut_body, palette_rgb_list):
    """Replace entries 16..47 with the 32 colors from `palette_rgb_list`.
    Leaves 0..15 and 48..255 untouched (so other rooms / UI keep their colors)."""
    out = bytearray(orig_clut_body)
    assert len(out) == 768
    # Pad palette to 32 if smaller
    pal = list(palette_rgb_list) + [(0,0,0)] * (32 - len(palette_rgb_list))
    for i, (r,g,b) in enumerate(pal[:32]):
        idx = (16 + i) * 3
        out[idx] = r; out[idx+1] = g; out[idx+2] = b
    return bytes(out)


def patch_two_chunks(in_path, out_path, room_index_in_file,
                     smap_body, clut_body):
    """Replace SMAP body and CLUT body for a given room. Returns info dict."""
    d = bytearray(load(in_path))
    rooms = list(walk_rooms(d))
    rid, ro = rooms[room_index_in_file]
    room_size = be32(d, ro+4)

    # Find both chunks in the ROOM (CLUT is a direct ROOM child; SMAP is nested under RMIM/IM00)
    rmim_off, rmim_sz = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')
    clut_off, clut_sz = find_chunk(d, ro+8, ro+room_size, 'CLUT')

    smap_body_old = smap_sz - 8
    clut_body_old = clut_sz - 8
    smap_delta = len(smap_body) - smap_body_old
    clut_delta = len(clut_body) - clut_body_old   # should be 0 for our use case

    # Apply SMAP first if it comes before CLUT in the file, else CLUT first.
    # (Our walking found chunks in order; we need to know absolute order.)
    # In room 1 of disk 1: CLUT @0x1b0 is BEFORE RMIM @0x4e0. So patch CLUT first
    # (changes don't shift offsets if same size), then SMAP (changes shift everything after).

    if clut_off < smap_off:
        # Patch CLUT (no delta expected)
        cb_start = clut_off + 8
        cb_end   = clut_off + clut_sz
        d = d[:cb_start] + bytearray(clut_body) + d[cb_end:]
        # Patch CLUT chunk size header
        d[clut_off+4:clut_off+8] = be32_pack(8 + len(clut_body))

        # Now CLUT delta would shift downstream offsets — re-find SMAP from scratch
        rmim_off, rmim_sz = find_chunk(d, ro+8, ro+room_size+clut_delta, 'RMIM')
        im00_off, im00_sz = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
        smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')

    # Patch SMAP
    sb_start = smap_off + 8
    sb_end   = smap_off + smap_sz
    d = d[:sb_start] + bytearray(smap_body) + d[sb_end:]

    # Patch chunk-size headers up the tree
    new_smap_size = 8 + len(smap_body)
    total_delta = smap_delta + clut_delta
    d[smap_off+4:smap_off+8] = be32_pack(new_smap_size)
    d[im00_off+4:im00_off+8] = be32_pack(im00_sz + smap_delta)
    d[rmim_off+4:rmim_off+8] = be32_pack(rmim_sz + smap_delta)
    d[ro+4:ro+8]             = be32_pack(room_size + total_delta)
    # If ROOM lives inside an LFLF wrapper (monkey2.0NN for N≥2), update LFLF size too.
    # LECF body starts at 8, may contain LOFF then LFLFs (or ROOMs directly in disk 1's layout).
    lecf_size_old = be32(d, 4)
    loff_sz = be32(d, 12)
    p = 8 + loff_sz
    while p < lecf_size_old:
        nm = name(d, p)
        if not all(32 <= c < 127 for c in d[p:p+4]):
            break
        csz = be32(d, p+4)
        if nm == 'LFLF' and p < ro < p + csz:
            d[p+4:p+8] = be32_pack(csz + total_delta)
            break
        if csz < 8 or p + csz > lecf_size_old:
            break
        p += csz
    d[4:8]                   = be32_pack(lecf_size_old + total_delta)

    # Patch downstream LOFF offsets (rooms after this one shift by total delta)
    total_delta = smap_delta + clut_delta
    n = d[16]
    for i in range(n):
        rec_off = 17 + i*5 + 1
        cur = le32(d, rec_off)
        if cur > ro:
            d[rec_off:rec_off+4] = le32_pack(cur + total_delta)

    # XOR-encrypt and write
    enc = bytes(b ^ XOR for b in d)
    with open(out_path, 'wb') as f:
        f.write(enc)
    return {
        'rid': rid, 'smap_delta': smap_delta, 'clut_delta': clut_delta,
        'smap_old': smap_body_old, 'smap_new': len(smap_body),
        'file_size': len(d),
    }


def main():
    print("Loading", SRC_PNG)
    indexed, palette, w, h = png_to_indexed(SRC_PNG, 320, 200)
    print(f"  {w}x{h}, {len(palette)} unique colors")
    # Pin CLUT[16]=black and CLUT[33]=white (palette[0] and palette[17] before paletteMod=16)
    if len(palette) < 32:
        palette = palette + [(0, 0, 0)] * (32 - len(palette))
    palette, indexed = lock_slots(palette, indexed)
    print(f"  After lock: palette[0]={palette[0]} palette[17]={palette[17]}")

    # Existing CLUT body for disk 1 room 1
    d = load(SRC_DISK)
    rid, ro = list(walk_rooms(d))[0]
    room_size = be32(d, ro+4)
    clut_off, clut_sz = find_chunk(d, ro+8, ro+room_size, 'CLUT')
    orig_clut_body = d[clut_off+8 : clut_off+clut_sz]

    new_clut = build_clut_body(orig_clut_body, palette)
    new_smap = build_smap_body(indexed, w, h)
    print(f"  new SMAP body: {len(new_smap)} bytes")
    print(f"  new CLUT body: {len(new_clut)} bytes")

    info = patch_two_chunks(SRC_DISK, PATCHED_DISK, 0, new_smap, new_clut)
    print(f"Patched LECF: {info}")

    # Copy original ADF and replace monkey2.001 inside it
    import shutil
    shutil.copy(SRC_ADF, OUT_ADF)
    r = subprocess.run([ADFREPLACE, OUT_ADF, 'monkey2.001', PATCHED_DISK],
                       capture_output=True, text=True)
    print(r.stdout, r.stderr, sep='')
    print(f"\nPatched ADF: {OUT_ADF}")

    # Re-decode the patched file from inside the ADF to verify visually
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([f'{REPO_ROOT}/tools/adflib/build/examples/unadf',
                        '-d', tmp, OUT_ADF, 'monkey2.001'], check=True,
                       capture_output=True)
        from decode_all import render_room, parse_index_room_names, OUT as RENDER_OUT
        names = parse_index_room_names()
        d2 = load(os.path.join(tmp, 'monkey2.001'))
        rid2, ro2 = list(walk_rooms(d2))[0]
        out_path = f'{REPO_ROOT}/preview/part1_after_patch.png'
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        # render_room writes to RENDER_OUT — temporarily redirect by monkey-patching
        from decode_all import OUT as _orig_out
        import decode_all
        decode_all.OUT = f'{REPO_ROOT}/preview'
        try:
            res = render_room(d2, ro2, rid2, 'after-patch')
            print(f"  Re-decoded patched: {res[0]}")
        finally:
            decode_all.OUT = _orig_out


if __name__ == '__main__':
    main()
