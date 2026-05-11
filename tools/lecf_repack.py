import os
#!/usr/bin/env python3
"""LECF re-pack: replace one chunk inside a monkey2.0NN file and rewrite offsets.

Initial use case:
  - Load a disk file (XOR-decrypt with 0x69)
  - Locate ROOM[room_id]/RMIM/IM00/SMAP
  - Replace SMAP body bytes with new ones
  - Adjust all parent chunk sizes (IM00, RMIM, ROOM, LECF)
  - Adjust LOFF offsets if downstream rooms shift
  - XOR-encrypt and write back

Currently supported only for **room 0 in the file** with a same-size or smaller
replacement (no downstream offset shift). General multi-room shifts require
re-emitting the LOFF table — straightforward extension when needed.
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import load, be32, le32, name, walk_rooms, find_chunk

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


XOR = 0x69


def be32_pack(v): return struct.pack('>I', v)
def le32_pack(v): return struct.pack('<I', v)


def patch_room_smap(in_path, out_path, room_index_in_file, new_smap_body):
    """Replace SMAP body in the room at index `room_index_in_file`. Validates
    that downstream rooms aren't shifted. Returns dict with new sizes/offsets."""
    d = bytearray(load(in_path))
    rooms = list(walk_rooms(d))
    if room_index_in_file >= len(rooms):
        raise IndexError(f"only {len(rooms)} rooms in this file")
    rid, ro = rooms[room_index_in_file]
    room_size = be32(d, ro+4)
    rmim_off, _ = find_chunk(d, ro+8, ro+room_size, 'RMIM')
    rmim_sz = be32(d, rmim_off+4)
    im00_off, _ = find_chunk(d, rmim_off+8, rmim_off+rmim_sz, 'IM00')
    im00_sz = be32(d, im00_off+4)
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+im00_sz, 'SMAP')
    smap_body_size = smap_sz - 8

    delta = len(new_smap_body) - smap_body_size

    # Build new file. Layout:
    #   [...up to SMAP body start...] [new_smap_body] [...rest after old SMAP body...]
    smap_body_start = smap_off + 8
    smap_body_end   = smap_off + smap_sz
    new_d = bytearray(d[:smap_body_start]) + bytearray(new_smap_body) + bytearray(d[smap_body_end:])

    # Patch sizes (BE32) for SMAP, IM00, RMIM, ROOM, LECF
    def patch_size_at(buf, ck_off, new_size):
        buf[ck_off+4:ck_off+8] = be32_pack(new_size)

    new_smap_size = 8 + len(new_smap_body)
    patch_size_at(new_d, smap_off, new_smap_size)
    patch_size_at(new_d, im00_off, im00_sz + delta)
    patch_size_at(new_d, rmim_off, rmim_sz + delta)
    patch_size_at(new_d, ro,       room_size + delta)
    lecf_size = be32(new_d, 4)
    patch_size_at(new_d, 0,        lecf_size + delta)

    # Patch downstream LOFF offsets (rooms after this one shift by `delta`)
    n = new_d[16]
    for i in range(n):
        rec_off = 17 + i*5 + 1
        cur = le32(new_d, rec_off)
        if cur > ro:
            new_d[rec_off:rec_off+4] = le32_pack(cur + delta)

    # XOR-encrypt and write
    enc = bytes(b ^ XOR for b in new_d)
    with open(out_path, 'wb') as f:
        f.write(enc)
    return {
        'rid': rid, 'delta': delta,
        'smap_old': smap_body_size, 'smap_new': len(new_smap_body),
        'file_size': len(new_d),
    }


# ---------------- self-test: identity patch (re-encode same SMAP) ----------------

def selftest():
    from decode_amiga_room import decode_zigzag_h
    from encode_amiga import encode_zigzag_h

    # Re-encode disk1 room1 SMAP and verify the file still parses back to same image
    src = f'{REPO_ROOT}/amiga-data/monkey2.001'
    dst = '/tmp/monkey2.001.patched'

    d = load(src)
    rid, ro = list(walk_rooms(d))[0]
    rmim_off, _ = find_chunk(d, ro+8, ro+be32(d, ro+4), 'RMIM')
    im00_off, _ = find_chunk(d, rmim_off+8, rmim_off+be32(d, rmim_off+4), 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off+8, im00_off+be32(d, im00_off+4), 'SMAP')
    rmhd_off, _ = find_chunk(d, ro+8, ro+be32(d, ro+4), 'RMHD')
    h = struct.unpack('<H', d[rmhd_off+10:rmhd_off+12])[0]
    body_off = smap_off + 8
    strip_offsets = [le32(d, body_off + i*4) for i in range(40)]
    strip_ends = list(strip_offsets[1:]) + [smap_sz]

    # Re-encode every ZIGZAG_H5 strip; for other codecs, copy original bytes verbatim.
    new_strip_bytes = []
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        codec = d[smap_off + start]
        original = d[smap_off + start + 1 : smap_off + end]
        if codec == 25:
            decoded = decode_zigzag_h(original, h, 5)
            re_enc = encode_zigzag_h(decoded, h, 5)
            new_strip_bytes.append(bytes([codec]) + re_enc)
        else:
            new_strip_bytes.append(bytes([codec]) + original)

    # Build new SMAP body: numStrips u32 LE offsets (relative to chunk start incl 8-byte header)
    # then strip data sequentially.
    num_strips = 40
    body = bytearray()
    body += b'\x00' * (num_strips * 4)  # placeholder for offsets
    strip_data_starts = []
    for sb in new_strip_bytes:
        strip_data_starts.append(8 + len(body))  # offset is from SMAP CHUNK start
        body += sb
    for i, off in enumerate(strip_data_starts):
        body[i*4:(i+1)*4] = le32_pack(off)

    info = patch_room_smap(src, dst, 0, bytes(body))
    print(f"Patched: {info}")

    # Re-decode the patched file and confirm we get the same image
    from decode_all import render_room, parse_index_room_names
    d2 = load(dst)
    names = parse_index_room_names()
    res = render_room(d2, list(walk_rooms(d2))[0][1], rid, names.get(rid, ''))
    if res:
        print(f"Re-decoded: {res[0]} ({res[1]}x{res[2]})")
    else:
        print("Re-decode FAILED")


if __name__ == '__main__':
    selftest()
