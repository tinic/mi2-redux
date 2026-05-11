"""SCUMM v5 Amiga index rebuilder.

Given:
  - the parsed index tree (parse_index of monkey2.000)
  - one parsed disk tree per disk file (parse() of monkey2.NNN, indexed by disk_n)
  - the post-serialize positions dict for each disk tree
rebuild fresh DSCR/DCOS/DSOU/DCHR bodies. DROO and DOBJ pass through.

Amiga DCOS/DSCR/DSOU/DCHR offset formula:
    offset = (chunk.new_position - room_node.new_position) / 2
where room_node is the ROOM child of the LFLF that contains the chunk.

The Amiga port halves offsets — likely to fit larger spans in 32-bit
fields with word-alignment guarantees from 68000 byte ordering.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from scumm_tree import (Node, parse, parse_index, serialize, serialize_index,

                          _be32, _name)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def parse_dxxx_table(body: bytes):
    """Parse a DSCR/DCOS/DSOU/DCHR body. Returns (count, [room_id], [offset])."""
    cnt = struct.unpack('<H', body[0:2])[0]
    room_ids = list(body[2:2 + cnt])
    offs = []
    for i in range(cnt):
        offs.append(struct.unpack('<I',
                    body[2 + cnt + i * 4:2 + cnt + (i + 1) * 4])[0])
    return cnt, room_ids, offs


def build_dxxx_body(room_ids: list, offs: list) -> bytes:
    """Inverse of parse_dxxx_table."""
    cnt = len(room_ids)
    parts = [struct.pack('<H', cnt), bytes(room_ids)]
    for o in offs:
        parts.append(struct.pack('<I', o))
    return b''.join(parts)


def parse_droo(body: bytes):
    """DROO: u16 count, count×u8 disk_n, count×u32 unused_offset(=0)."""
    cnt = struct.unpack('<H', body[0:2])[0]
    disks = list(body[2:2 + cnt])
    return cnt, disks


def map_cid_to_node(disk_trees: dict, droo_disks: dict, table_room_ids: list,
                     table_offs: list, child_tag: str):
    """Given the table's (rid, off) per index, locate each entry's
    corresponding child node by matching offset = chunk.orig - room.orig.
    Returns dict {entry_index: (disk_n, node)}.
    """
    result = {}
    for idx, rid in enumerate(table_room_ids):
        if rid == 0:
            continue
        disk_n = droo_disks.get(rid)
        if disk_n is None or disk_n == 0:
            continue
        if disk_n not in disk_trees:
            continue
        from scumm_tree import find_lflf_for_room
        tree = disk_trees[disk_n]
        lflf = find_lflf_for_room(tree, rid)
        if lflf is None:
            continue
        room = next((c for c in lflf.children if c.tag == 'ROOM'), None)
        if room is None:
            continue
        target = table_offs[idx]
        for child in lflf.children:
            if child.tag != child_tag:
                continue
            formula = child.orig_offset - room.orig_offset
            if formula == target:
                result[idx] = (disk_n, child)
                break
    return result


def rebuild_index(index_root: Node, disk_trees: dict, disk_positions: dict,
                   droo_disks: dict) -> None:
    """In-place: for each DSCR/DCOS/DSOU/DCHR table in `index_root`,
    update offsets to match each tracked node's new position.

    `disk_positions` is a dict {disk_n: positions_dict_from_serialize}.
    Call serialize() on each disk tree first to populate positions.
    Then call this. Then call serialize_index() to write the new index.
    """
    table_tags = ('DSCR', 'DCOS', 'DSOU', 'DCHR')
    chunk_tag_for = {'DSCR': 'SCRP', 'DCOS': 'COST', 'DSOU': 'SOUN',
                       'DCHR': 'CHAR'}
    for child in index_root.children:
        if child.tag not in table_tags:
            continue
        cnt, room_ids, offs = parse_dxxx_table(child.body)
        chunk_tag = chunk_tag_for[child.tag]
        # Map each entry to its node using the OLD orig_offsets stored in the tree
        idx_to_node = map_cid_to_node(disk_trees, droo_disks, room_ids, offs,
                                       chunk_tag)
        new_offs = list(offs)
        n_unmatched = 0
        for i in range(cnt):
            if i not in idx_to_node:
                if room_ids[i] != 0:
                    n_unmatched += 1
                continue
            disk_n, node = idx_to_node[i]
            positions = disk_positions[disk_n]
            new_chunk_off = positions[id(node)]
            from scumm_tree import find_lflf_for_room

            lflf = find_lflf_for_room(disk_trees[disk_n], room_ids[i])
            room = next(c for c in lflf.children if c.tag == 'ROOM')
            new_room_off = positions[id(room)]
            new_offs[i] = new_chunk_off - new_room_off
        if n_unmatched:
            sys.stderr.write(f'  [warn] {child.tag}: {n_unmatched} unmatched '
                              f'entries (kept old offset)\n')
        child.body = build_dxxx_body(room_ids, new_offs)


# ---------------------------------------------------------------------
# Self-test: round-trip pristine through parse + serialize + rebuild_index.
# Should produce byte-identical disks AND index.
# ---------------------------------------------------------------------

if __name__ == '__main__':
    AMIGA = f'{REPO_ROOT}/amiga-data'
    print('Loading all disks...')
    disk_trees = {}
    for n in range(1, 12):
        path = f'{AMIGA}/monkey2.{n:03d}'
        if not os.path.exists(path):
            continue
        d = bytes(b ^ 0x69 for b in open(path, 'rb').read())
        disk_trees[n] = parse(d)
    idx_data = bytes(b ^ 0x69 for b in open(f'{AMIGA}/monkey2.000',
                                              'rb').read())
    index_root = parse_index(idx_data)

    # Pull DROO disks
    droo_node = next(c for c in index_root.children if c.tag == 'DROO')
    cnt, droo_disks_list = parse_droo(droo_node.body)
    droo_disks = {rid: disk_n for rid, disk_n in enumerate(droo_disks_list)
                   if disk_n > 0}

    # Serialize each disk to populate positions
    print('Serializing disks (populates positions)...')
    disk_positions = {}
    for n, tree in disk_trees.items():
        positions = {}
        serialize(tree, positions)
        disk_positions[n] = positions

    print('Rebuilding index from current trees...')
    rebuild_index(index_root, disk_trees, disk_positions, droo_disks)

    # Compare with pristine index body
    new_idx = serialize_index(index_root)
    if new_idx == idx_data:
        print(f'OK index round-trip identical ({len(idx_data)} bytes)')
    else:
        print(f'MISMATCH src={len(idx_data)} new={len(new_idx)}')
        for i in range(min(len(idx_data), len(new_idx))):
            if idx_data[i] != new_idx[i]:
                print(f'  first diff at byte {i}: '
                      f'{idx_data[i]:02x} vs {new_idx[i]:02x}')
                break
