#!/usr/bin/env python3
"""Prototype: clone a cid's re-encoded COST into a target room's LFLF
as a NEW cid, update DCOS, patch ONE specific script call to use it.

This is the minimum-viable architectural test: can SCUMM v5 Amiga
actually load + render a costume slot we invent (cid > 173)? If yes,
the per-room-variant architecture is workable.

Usage: tools/create_variant_cid.py SRC_CID TARGET_ROOM NEW_CID
       SCRIPT_KIND SCRIPT_SIZE PATCH_OFFSET

  SRC_CID       cid to clone (its re-encoded body from monkey2-hd/)
  TARGET_ROOM   room name where the variant lives (e.g. woodtick)
  NEW_CID       new cid number (must be in [173..198] free range)
  SCRIPT_KIND   LSCR / SCRP / ENCD / EXCD / OBCD-VERB
  SCRIPT_SIZE   body size of the script to patch
  PATCH_OFFSET  body offset where the Costume(SRC_CID) byte lives
                (the single-byte cid value, not the 0x01 sub-op)

Example: clone cid 16 → cid 174 in woodtick, patch LSCR sz=483 byte 0x34:
  tools/create_variant_cid.py 16 woodtick 174 LSCR 483 0x34
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))

from scumm_tree import (parse, parse_index, serialize, serialize_index,
                        find_lflf_for_room, Node)
from scumm_index import rebuild_index, parse_droo
from decode_all import parse_index_room_names

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SVM_DIR = os.path.join(REPO_ROOT, 'monkey2-hd')


def _load_disk(disk_n):
    XOR = 0x69
    path = os.path.join(SVM_DIR, f'monkey2.{disk_n:03d}')
    return bytes(b ^ XOR for b in open(path, 'rb').read())


def _save_disk(disk_n, data):
    XOR = 0x69
    path = os.path.join(SVM_DIR, f'monkey2.{disk_n:03d}')
    open(path, 'wb').write(bytes(b ^ XOR for b in data))


def _disk_of(room_id, idx_root):
    droo = next(c for c in idx_root.children if c.tag == 'DROO')
    _, droo_disks = parse_droo(droo.body)
    return droo_disks[room_id] if room_id < len(droo_disks) else 0


def main():
    if len(sys.argv) != 7:
        sys.exit(__doc__)
    src_cid = int(sys.argv[1])
    target_room_name = sys.argv[2]
    new_cid = int(sys.argv[3])
    script_kind = sys.argv[4]
    script_size = int(sys.argv[5])
    patch_off = int(sys.argv[6], 0)

    if not 173 <= new_cid <= 198:
        sys.exit(f'new_cid {new_cid} not in free DCOS range [173..198]')

    names = parse_index_room_names()
    target_rid = next((r for r, n in names.items() if n == target_room_name),
                     None)
    if target_rid is None:
        sys.exit(f'unknown room: {target_room_name}')

    # Read index
    idx_plain = _load_disk(0)
    idx_root = parse_index(idx_plain)

    # Find DCOS, parse, locate src_cid's home
    dcos = next(c for c in idx_root.children if c.tag == 'DCOS')
    cnt = struct.unpack('<H', dcos.body[:2])[0]
    src_rooms = list(dcos.body[2:2 + cnt])
    src_offs = [struct.unpack('<I', dcos.body[2 + cnt + i * 4:
                                              2 + cnt + (i + 1) * 4])[0]
                for i in range(cnt)]
    src_home_rid = src_rooms[src_cid]
    src_home_disk = _disk_of(src_home_rid, idx_root)
    target_disk = _disk_of(target_rid, idx_root)
    print(f'src cid {src_cid}: home rid {src_home_rid} on disk {src_home_disk}')
    print(f'target rid {target_rid}({target_room_name}) on disk {target_disk}')

    # Load source disk + clone src cid's COST body
    src_disk = _load_disk(src_home_disk)
    src_tree = parse(src_disk)
    src_lflf = find_lflf_for_room(src_tree, src_home_rid)
    src_room = next(c for c in src_lflf.children if c.tag == 'ROOM')
    # Match COST by offset (DCOS entry's offset == cost.orig_offset - room.orig_offset)
    src_cost_off = src_offs[src_cid]
    src_cost_node = next(
        (c for c in src_lflf.children
         if c.tag == 'COST' and c.orig_offset - src_room.orig_offset == src_cost_off),
        None)
    if src_cost_node is None:
        sys.exit(f'no COST in rid {src_home_rid} at delta {src_cost_off}')
    cloned_body = bytes(src_cost_node.body)
    print(f'cloned {len(cloned_body)} bytes from cid {src_cid}\'s COST')

    # Load target disk + insert new COST into target LFLF
    if target_disk != src_home_disk:
        target_data = _load_disk(target_disk)
        target_tree = parse(target_data)
    else:
        target_tree = src_tree    # same disk
    target_lflf = find_lflf_for_room(target_tree, target_rid)
    target_room = next(c for c in target_lflf.children if c.tag == 'ROOM')
    new_cost = Node('COST', body=cloned_body, orig_offset=-1)
    target_lflf.children.append(new_cost)
    print(f'inserted new COST into rid {target_rid} LFLF '
          f'(now {sum(1 for c in target_lflf.children if c.tag == "COST")} COSTs)')

    # Patch the script byte. The CID value sits at body[patch_off].
    found = None
    for c in target_room.children:
        if c.tag == script_kind and len(c.body) == script_size:
            found = c
            break
    if found is None:
        # try LFLF-level
        for c in target_lflf.children:
            if c.tag == script_kind and len(c.body) == script_size:
                found = c
                break
    if found is None:
        sys.exit(f'no {script_kind} sz={script_size} in rid {target_rid}')
    if found.body[patch_off] != src_cid:
        sys.exit(f'expected byte {src_cid:#x} at offset {patch_off:#x}, '
                 f'found {found.body[patch_off]:#x}')
    new_body = bytearray(found.body)
    new_body[patch_off] = new_cid
    found.body = bytes(new_body)
    print(f'patched {script_kind} sz={script_size} byte[{patch_off:#x}]: '
          f'{src_cid} → {new_cid}')

    # Serialize target disk, get position of the new COST
    positions = {}
    out_target = serialize(target_tree, positions)
    new_cost_pos = positions[id(new_cost)]
    new_room_pos = positions[id(target_room)]
    new_dcos_off = new_cost_pos - new_room_pos
    print(f'new COST at file_off {new_cost_pos}, ROOM at {new_room_pos}, '
          f'DCOS entry offset = {new_dcos_off}')

    # Update DCOS body: set entries for new_cid
    new_dcos_body = bytearray(dcos.body)
    new_dcos_body[2 + new_cid] = target_rid
    struct.pack_into('<I', new_dcos_body,
                     2 + cnt + new_cid * 4, new_dcos_off)
    dcos.body = bytes(new_dcos_body)

    # Save target disk
    _save_disk(target_disk, out_target)
    print(f'wrote monkey2.{target_disk:03d} ({len(out_target)} bytes)')

    # Rebuild index for ALL other entries (target disk shifted positions
    # because we inserted a chunk).
    disk_trees = {target_disk: target_tree}
    disk_positions = {target_disk: positions}
    droo = next(c for c in idx_root.children if c.tag == 'DROO')
    _, droo_disks = parse_droo(droo.body)
    droo_disks_dict = {r: d for r, d in enumerate(droo_disks) if d > 0}
    for d_n in set(droo_disks_dict.values()):
        if d_n == target_disk:
            continue
        d_data = _load_disk(d_n)
        d_tree = parse(d_data)
        d_pos = {}
        serialize(d_tree, d_pos)
        disk_trees[d_n] = d_tree
        disk_positions[d_n] = d_pos
    rebuild_index(idx_root, disk_trees, disk_positions, droo_disks_dict)

    # Re-pin our new DCOS entry (rebuild_index may have cleared it as
    # "unmatched"). The new_cost_node.orig_offset is still -1, so
    # map_cid_to_node can't find it. Explicitly fix our entry.
    dcos2 = next(c for c in idx_root.children if c.tag == 'DCOS')
    dcos2_body = bytearray(dcos2.body)
    dcos2_body[2 + new_cid] = target_rid
    struct.pack_into('<I', dcos2_body,
                     2 + cnt + new_cid * 4, new_dcos_off)
    dcos2.body = bytes(dcos2_body)

    out_idx = serialize_index(idx_root)
    _save_disk(0, out_idx)
    print(f'wrote monkey2.000 ({len(out_idx)} bytes)')
    print(f'\nDone. cid {new_cid} is a clone of cid {src_cid}, '
          f'lives in rid {target_rid}({target_room_name}).')
    print(f'Script {script_kind} sz={script_size} now references cid {new_cid}.')


if __name__ == '__main__':
    main()
