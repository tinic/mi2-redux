#!/usr/bin/env python3
"""Apply tools/variants.json to monkey2-hd/.

For each variant entry:
  1. clone src_cid's PRISTINE COST body (from amiga-data, via pristine_cache)
  2. insert it as a new COST node into target_room's LFLF
  3. patch every Costume(src_cid) call inside target_room's scripts
     (LSCR / ENCD / EXCD / OBCD-VERB / SCRP in the LFLF) to use new_cid
  4. extend DCOS to map new_cid → target_room

After this step, inject_room.py's per-room cost re-encode loop picks up
the new variant cid as a regular home-LFLF costume and re-encodes it
against the target room's joint --best palette — exactly the behaviour
we want for per-room palette tuning.

Run AFTER `cp amiga-data/monkey2.0* monkey2-hd/` and BEFORE the global
encoders / per-room inject_room pass.
"""

import json
import os
import re
import subprocess
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from scumm_tree import (parse, parse_index, serialize, serialize_index,
                        find_lflf_for_room, Node)
from scumm_index import rebuild_index, parse_droo
from pristine_cache import cache
from decode_all import parse_index_room_names

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SVM_DIR = os.path.join(REPO_ROOT, 'monkey2-hd')
VARIANTS_F = os.path.join(REPO_ROOT, 'tools', 'variants.json')
DESCUMM = os.path.join(REPO_ROOT, 'tools', 'scummvm-tools', 'descumm')


def _load(disk):
    XOR = 0x69
    return bytes(b ^ XOR for b in
                 open(os.path.join(SVM_DIR, f'monkey2.{disk:03d}'),
                      'rb').read())


def _save(disk, data):
    XOR = 0x69
    open(os.path.join(SVM_DIR, f'monkey2.{disk:03d}'), 'wb').write(
        bytes(b ^ XOR for b in data))


def _descumm_costume_addrs(tag, body, src_cid):
    """Return list of byte offsets within `body` of every Costume(src_cid)
    call inside an ActorOps. Uses descumm to identify the call sites and
    a direct byte scan to find the exact patch byte."""
    chunk = tag.encode() + len(body).to_bytes(4, 'big') + body
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk); fname = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', fname],
                           capture_output=True, text=True, timeout=10)
    finally:
        os.unlink(fname)
    if r.returncode != 0:
        return []
    addrs = []
    for ln in r.stdout.splitlines():
        m = re.match(
            r'\s*\[([0-9A-Fa-f]{4})\]\s+\((\w+)\)\s+ActorOps\(.*?'
            r'Costume\(\s*(\d+)\s*\)', ln)
        if m and int(m.group(3)) == src_cid:
            addrs.append(int(m.group(1), 16))
    return addrs


def _patch_costume_in_body(body, descumm_addrs, src_cid, new_cid):
    """For each descumm-reported addr of an ActorOps with Costume(src_cid),
    scan forward in `body` for the `0x01 SRC` sub-op + byte and patch the
    byte to new_cid. The descumm addr may be off by ±1 (LSCR has a 1-byte
    script# prefix), so we tolerate a small search window."""
    body = bytearray(body)
    n_patched = 0
    for da in descumm_addrs:
        # Search window starting at descumm addr, allowing for the
        # 1-byte LSCR script# prefix offset and small descumm rounding.
        for start in (da - 1, da, da + 1):
            if start < 0 or start >= len(body):
                continue
            if body[start] not in (0x13, 0x53, 0x93):
                continue
            # Walk forward looking for `0x01 src_cid` before `0xff`.
            i = start + 1
            end_of_actorops = -1
            while i < len(body) and i < start + 200:
                b = body[i]
                if b == 0xff:
                    end_of_actorops = i
                    break
                if b == 0x01 and i + 1 < len(body) and body[i + 1] == src_cid:
                    body[i + 1] = new_cid
                    n_patched += 1
                    break
                i += 1
            if n_patched:
                break
    return bytes(body), n_patched


def _patch_target_room_scripts(target_lflf, src_cid, new_cid):
    """Find every script chunk inside target_lflf (and its ROOM) and
    patch Costume(src_cid) → Costume(new_cid)."""
    target_room = next(c for c in target_lflf.children if c.tag == 'ROOM')
    total = 0
    n_scripts = 0
    for parent in (target_lflf, target_room):
        for c in parent.children:
            if c.tag in ('SCRP', 'LSCR', 'ENCD', 'EXCD'):
                addrs = _descumm_costume_addrs(c.tag, c.body, src_cid)
                if addrs:
                    new_body, n = _patch_costume_in_body(
                        c.body, addrs, src_cid, new_cid)
                    if n:
                        c.body = new_body
                        total += n; n_scripts += 1
            elif c.tag == 'OBCD':
                for sub in c.children:
                    if sub.tag == 'VERB':
                        addrs = _descumm_costume_addrs(
                            sub.tag, sub.body, src_cid)
                        if addrs:
                            new_body, n = _patch_costume_in_body(
                                sub.body, addrs, src_cid, new_cid)
                            if n:
                                sub.body = new_body
                                total += n; n_scripts += 1
    return total, n_scripts


def main():
    variants = json.load(open(VARIANTS_F))['variants']
    if not variants:
        print('no variants to apply')
        return

    names = parse_index_room_names()
    name_to_rid = {n: r for r, n in names.items()}

    # Group variants by target disk
    idx_root = parse_index(_load(0))
    droo = next(c for c in idx_root.children if c.tag == 'DROO')
    _, droo_disks_list = parse_droo(droo.body)
    droo_disks = {r: d for r, d in enumerate(droo_disks_list) if d > 0}

    by_disk = {}
    for v in variants:
        target_rid = name_to_rid.get(v['target_room'])
        if target_rid is None:
            print(f"  [skip] unknown room: {v['target_room']}")
            continue
        target_disk = droo_disks.get(target_rid)
        if target_disk is None:
            print(f"  [skip] room {v['target_room']}: no disk in DROO")
            continue
        by_disk.setdefault(target_disk, []).append((v, target_rid))

    # For each disk, load tree, apply ALL variants targeting it, then
    # serialize once.
    disk_trees = {}
    disk_positions = {}
    new_dcos_entries = {}    # new_cid -> (target_rid, new_cost_node)
    for disk, vs in sorted(by_disk.items()):
        print(f'== disk {disk} ({len(vs)} variant(s)) ==')
        tree = parse(_load(disk))
        for v, target_rid in vs:
            new_cid = v['new_cid']; src_cid = v['src_cid']
            # Pristine src body from cache
            src_home = cache.cost_home(src_cid)
            src_cs = next(
                (c for c in cache.room(src_home)['costumes']
                 if c['cost_id'] == src_cid), None)
            if src_cs is None:
                print(f"  [skip] cid {src_cid}: not in pristine cache")
                continue
            body = bytes(src_cs['body'])
            # Insert new COST node
            target_lflf = find_lflf_for_room(tree, target_rid)
            new_cost = Node('COST', body=body, orig_offset=-1)
            target_lflf.children.append(new_cost)
            # Patch scripts
            n_pat, n_scr = _patch_target_room_scripts(
                target_lflf, src_cid, new_cid)
            print(f"  cid {new_cid} = cid {src_cid} clone in "
                  f"rid {target_rid}({v['target_room']}): "
                  f"COST {len(body)}B, "
                  f"{n_pat} script-byte patch(es) across {n_scr} chunk(s)")
            new_dcos_entries[new_cid] = (target_rid, new_cost)
        disk_trees[disk] = tree

    # Serialize each modified disk, capture positions, write back
    for disk, tree in disk_trees.items():
        pos = {}
        out = serialize(tree, pos)
        _save(disk, out)
        disk_positions[disk] = pos
        print(f"  -> monkey2.{disk:03d} ({len(out)} bytes)")

    # For the OTHER disks, load + serialize (no mutation) so rebuild_index
    # can compute correct positions for cross-disk references.
    for d_n in set(droo_disks.values()):
        if d_n in disk_trees:
            continue
        d_data = _load(d_n)
        d_tree = parse(d_data)
        d_pos = {}
        serialize(d_tree, d_pos)
        disk_trees[d_n] = d_tree
        disk_positions[d_n] = d_pos

    # Rebuild index for existing entries
    rebuild_index(idx_root, disk_trees, disk_positions, droo_disks)

    # Manually pin our NEW DCOS entries (rebuild_index can't match them —
    # no orig_offset for newly-inserted nodes).
    dcos = next(c for c in idx_root.children if c.tag == 'DCOS')
    cnt = struct.unpack('<H', dcos.body[:2])[0]
    new_body = bytearray(dcos.body)
    for new_cid, (target_rid, cost_node) in new_dcos_entries.items():
        # Find new offset
        cost_pos = None
        room_pos = None
        for disk, pos_map in disk_positions.items():
            if id(cost_node) in pos_map:
                cost_pos = pos_map[id(cost_node)]
                target_lflf = find_lflf_for_room(disk_trees[disk], target_rid)
                room_node = next(c for c in target_lflf.children
                                 if c.tag == 'ROOM')
                room_pos = pos_map[id(room_node)]
                break
        if cost_pos is None:
            print(f'  [warn] couldn\'t resolve position for new cid {new_cid}')
            continue
        delta = cost_pos - room_pos
        new_body[2 + new_cid] = target_rid
        struct.pack_into('<I', new_body, 2 + cnt + new_cid * 4, delta)
    dcos.body = bytes(new_body)

    # Write index
    _save(0, serialize_index(idx_root))
    print(f"  -> monkey2.000 (index rebuilt, "
          f"{len(new_dcos_entries)} new DCOS entries)")


if __name__ == '__main__':
    main()
