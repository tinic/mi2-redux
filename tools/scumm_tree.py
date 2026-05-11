"""SCUMM v5 LECF tree parser + serializer.

Replaces in-place patching + offset bookkeeping. Every modification
goes through the same path:

    data = open('monkey2.002', 'rb').read()
    data = bytes(b ^ 0x69 for b in data)  # XOR-decrypt
    tree = parse(data)
    # ... mutate tree.find_chunk('CLUT', room=14).body = new_clut_body
    out = serialize(tree)
    out = bytes(b ^ 0x69 for b in out)    # XOR-encrypt
    open('monkey2.002', 'wb').write(out)

Composite chunks (LECF, LFLF, ROOM, RMIM, IM00..N, OBIM, OBCD) hold
children — their size is computed from concat(children). Leaf chunks
hold opaque body bytes — modifications replace `body` directly.

This file is decoder-format-agnostic: it doesn't know what's inside
SMAP / COST / etc. It just preserves their byte content unless the
caller explicitly replaces it.
"""

import struct
from typing import Optional


COMPOSITE_TAGS = {
    'LECF', 'LFLF', 'ROOM', 'RMIM', 'OBIM', 'OBCD',
    # IM00 + IMxx within RMIM/OBIM hold SMAP + Z-planes
}
# IM00, IM01, ..., IM99 — composite (image strips with Z-planes)
def _is_composite(tag: str) -> bool:
    if tag in COMPOSITE_TAGS:
        return True
    if len(tag) == 4 and tag[:2] == 'IM' and tag[2:].isdigit():
        return True
    return False


def _be32(b: bytes, off: int) -> int:
    return struct.unpack('>I', b[off:off + 4])[0]


def _name(b: bytes, off: int) -> str:
    return b[off:off + 4].decode('ascii', errors='replace')


class Node:
    __slots__ = ('tag', 'body', 'children', 'orig_offset')

    def __init__(self, tag: str, body: bytes = b'',
                 children: Optional[list] = None,
                 orig_offset: int = -1):
        self.tag = tag
        self.body = body                 # leaf only; ignored if children
        self.children = children if children is not None else None
        self.orig_offset = orig_offset   # diagnostic only — preserved across parse

    @property
    def is_composite(self) -> bool:
        return self.children is not None

    def __repr__(self) -> str:
        if self.is_composite:
            return f'Node({self.tag} composite, {len(self.children)} children)'
        return f'Node({self.tag} leaf, body={len(self.body)} bytes)'

    def find_first(self, tag: str) -> Optional['Node']:
        """Recursive depth-first search for the first child with `tag`."""
        if self.tag == tag:
            return self
        if self.is_composite:
            for c in self.children:
                r = c.find_first(tag)
                if r is not None:
                    return r
        return None

    def find_all(self, tag: str) -> list:
        """Recursive depth-first collect every node with `tag`."""
        out = []
        if self.tag == tag:
            out.append(self)
        if self.is_composite:
            for c in self.children:
                out.extend(c.find_all(tag))
        return out


def parse(data: bytes, offset: int = 0, end: int = -1,
          orig_offset_base: int = 0) -> Node:
    """Parse the full LECF tree starting at `offset`. Returns the root
    Node (tag='LECF')."""
    if end < 0:
        end = len(data)
    return _parse_chunk(data, offset, end, orig_offset_base)


def parse_index(data: bytes) -> Node:
    """Parse the SCUMM v5 index file (monkey2.000) — a sequential list
    of top-level chunks (RNAM, MAXS, DROO, DSCR, DSOU, DCOS, DCHR,
    DOBJ) with no wrapping LECF. Returns a synthetic 'INDX' root with
    those chunks as children so serialize() can round-trip via the
    same emit logic.

    Note: when serialising back, we strip the synthetic INDX header
    (8 bytes) so the output matches the original wrapped format."""
    children = []
    p = 0
    while p < len(data):
        if p + 8 > len(data):
            raise ValueError(f'truncated index at {p}')
        sz = _be32(data, p + 4)
        if sz < 8 or p + sz > len(data):
            raise ValueError(f'bad chunk in index at {p}')
        children.append(_parse_chunk(data, p, p + sz, p))
        p += sz
    return Node('INDX', children=children)


def serialize_index(root: Node) -> bytes:
    """Inverse of parse_index — emit each child without the synthetic
    INDX wrapper."""
    parts = []
    for c in root.children:
        _emit(c, parts)
    return b''.join(parts)


def _parse_chunk(data: bytes, offset: int, end: int,
                  orig_offset_base: int) -> Node:
    """orig_offset_base is the file-absolute offset of `data[0]`. The
    chunk's orig_offset is orig_offset_base + offset."""
    if offset + 8 > end:
        raise ValueError(f'truncated chunk at offset {offset}')
    tag = _name(data, offset)
    sz = _be32(data, offset + 4)
    if sz < 8 or offset + sz > end:
        raise ValueError(f'bad chunk size at offset {offset}: tag={tag!r} sz={sz}')

    if _is_composite(tag):
        children = []
        p = offset + 8
        chunk_end = offset + sz
        while p < chunk_end:
            if p + 8 > chunk_end:
                raise ValueError(f'truncated child of {tag} at {p}')
            child = _parse_chunk(data, p, chunk_end, orig_offset_base)
            children.append(child)
            csz = _be32(data, p + 4)
            p += csz
        return Node(tag, children=children,
                     orig_offset=orig_offset_base + offset)
    else:
        body = data[offset + 8:offset + sz]
        return Node(tag, body=bytes(body),
                     orig_offset=orig_offset_base + offset)


def _rebuild_loff_body(lecf: Node) -> None:
    """If the root is LECF and contains a LOFF child, rewrite LOFF's
    body so each (rid, offset) entry reflects the CURRENT serialised
    position of that rid's LFLF. Called by serialize() before emitting
    so the on-disk LOFF matches the actual layout."""
    if lecf.tag != 'LECF':
        return
    loff = next((c for c in lecf.children if c.tag == 'LOFF'), None)
    if loff is None:
        return
    cnt = loff.body[0]
    # Walk LECF children, accumulating cursor in the upcoming serialise.
    # When we hit each LFLF, record its ROOM offset (= LFLF + 8). LOFF
    # in pristine data stores the ROOM offset, not the LFLF chunk start.
    cursor = 8                              # past LECF header
    rid_to_pos = {}
    for child in lecf.children:
        if child.tag == 'LFLF':
            rid_to_pos[id(child)] = cursor + 8     # ROOM offset in file
        if child.is_composite:
            child_size = 8 + _composite_size(child)
        else:
            child_size = 8 + len(child.body)
        cursor += child_size
    # Now rebuild LOFF body in the original (rid, offset) order. Walk
    # LFLFs in document order; pair each with the next LOFF entry.
    new_body = bytearray()
    new_body.append(cnt)
    lflfs = [c for c in lecf.children if c.tag == 'LFLF']
    p = 1
    for i in range(cnt):
        rid = loff.body[p]
        # Locate LFLF i in lflfs, get its position
        if i < len(lflfs):
            new_pos = rid_to_pos[id(lflfs[i])]
        else:
            new_pos = struct.unpack('<I', loff.body[p + 1:p + 5])[0]
        new_body.append(rid)
        new_body += struct.pack('<I', new_pos)
        p += 5
    loff.body = bytes(new_body)


def _composite_size(node: Node) -> int:
    """Recursively compute the BODY size of a composite chunk (= total
    size of all children including their headers)."""
    if not node.is_composite:
        return len(node.body)
    return sum(8 + (_composite_size(c) if c.is_composite else len(c.body))
                for c in node.children)


def serialize(node: Node, positions: Optional[dict] = None) -> bytes:
    """Walk the tree and emit a fresh LECF binary. All sizes computed
    from current body / children.

    If `positions` is a dict, it gets populated with id(node) -> offset
    in the output for every node that's emitted. Use this to read the
    new-offset of a specific chunk after serialize.

    LOFF body is auto-rewritten to reflect the current LFLF positions
    in the output."""
    if node.tag == 'LECF':
        _rebuild_loff_body(node)
    parts = []
    _emit(node, parts, positions, [0])
    return b''.join(parts)


def _emit(node: Node, parts: list, positions, cursor) -> None:
    if positions is not None:
        positions[id(node)] = cursor[0]
    parts.append(node.tag.encode('ascii'))
    parts.append(b'\0\0\0\0')                # placeholder for size
    cursor[0] += 8
    sz_pos = len(parts) - 1
    if node.is_composite:
        body_start = cursor[0]
        for c in node.children:
            _emit(c, parts, positions, cursor)
        body_len = cursor[0] - body_start
    else:
        parts.append(node.body)
        body_len = len(node.body)
        cursor[0] += body_len
    parts[sz_pos] = struct.pack('>I', 8 + body_len)


def serialize_index(root: Node, positions: Optional[dict] = None) -> bytes:
    """Inverse of parse_index — emit each child without the synthetic
    INDX wrapper."""
    parts = []
    cursor = [0]
    for c in root.children:
        _emit(c, parts, positions, cursor)
    return b''.join(parts)


# ---------------------------------------------------------------------
# Helpers for working with the LECF tree at the room level.
# ---------------------------------------------------------------------

def find_lflf_for_room(lecf: Node, rid: int) -> Optional[Node]:
    """Return the LFLF node that wraps the room with given rid.

    LFLF/ROOM ordering matches LOFF entries: LOFF (the first child of
    LECF) lists [(rid, offset), ...]. The ordering of LFLFs in the
    file matches the LOFF list. This helper relies on that ordering
    and returns the i-th LFLF where rid matches LOFF entry i.
    """
    if lecf.tag != 'LECF':
        return None
    loff = next((c for c in lecf.children if c.tag == 'LOFF'), None)
    lflfs = [c for c in lecf.children if c.tag == 'LFLF']
    if loff is None:
        return None
    body = loff.body
    cnt = body[0]
    rids = []
    for i in range(cnt):
        e_rid = body[1 + i * 5]
        rids.append(e_rid)
    for i, e_rid in enumerate(rids):
        if e_rid == rid and i < len(lflfs):
            return lflfs[i]
    return None


def room_node(lecf: Node, rid: int) -> Optional[Node]:
    """Return the ROOM node within the LFLF for rid."""
    lflf = find_lflf_for_room(lecf, rid)
    if lflf is None:
        return None
    return next((c for c in lflf.children if c.tag == 'ROOM'), None)


def costs_in_room_lflf(lecf: Node, rid: int) -> list:
    """Return list of COST nodes in this room's LFLF (in order)."""
    lflf = find_lflf_for_room(lecf, rid)
    if lflf is None:
        return []
    return [c for c in lflf.children if c.tag == 'COST']


def scripts_in_room(lecf: Node, rid: int) -> list:
    """Return list of (tag, Node) for script chunks in the ROOM.

    Includes LSCR / EXCD / ENCD inside the ROOM, and SCRP at the LFLF
    level. OBCD-VERB chunks are returned as ('VERB', node).
    """
    out = []
    lflf = find_lflf_for_room(lecf, rid)
    if lflf is None:
        return out
    for child in lflf.children:
        if child.tag == 'SCRP':
            out.append(('SCRP', child))
        elif child.tag == 'ROOM':
            for rc in child.children:
                if rc.tag in ('LSCR', 'EXCD', 'ENCD'):
                    out.append((rc.tag, rc))
                elif rc.tag == 'OBCD':
                    verb = next((g for g in rc.children if g.tag == 'VERB'),
                                 None)
                    if verb is not None:
                        out.append(('VERB', verb))
    return out


# ---------------------------------------------------------------------
# Round-trip self-test — verify parse() + serialize() is byte-identical.
# ---------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        sys.exit('usage: scumm_tree.py <monkey2.NNN>')
    raw = open(sys.argv[1], 'rb').read()
    decrypted = bytes(b ^ 0x69 for b in raw)
    # monkey2.000 is the index; everything else is LECF
    is_index = sys.argv[1].endswith('.000')
    tree = parse_index(decrypted) if is_index else parse(decrypted)
    out = serialize_index(tree) if is_index else serialize(tree)
    if out == decrypted:
        print(f'OK round-trip identical ({len(decrypted)} bytes)  '
              f'[{sys.argv[1]}]')
    else:
        print(f'MISMATCH src={len(decrypted)} out={len(out)}  [{sys.argv[1]}]')
        for i in range(min(len(decrypted), len(out))):
            if decrypted[i] != out[i]:
                print(f'  first diff at byte {i}: '
                      f'{decrypted[i]:02x} vs {out[i]:02x}')
                break
        sys.exit(1)
