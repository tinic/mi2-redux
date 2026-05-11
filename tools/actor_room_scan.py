#!/usr/bin/env python3
"""Actor-tracker scan: build a much more complete drawn_in graph by
following SCUMM v5's actor model.

Static script analysis can't catch costume references like "Guybrush
appears in casino" because the player's costume is set ONCE at game
start and PutActor* calls don't re-state it. Instead we track:

  1. ActorOps(X, [..., Costume(N), ...])
       → actor X is assigned cid N (in any script).
  2. putActor(X, ...) / putActorInRoom(X, R) / putActorAtObject(X, ...)
       → actor X is placed in SOME room (script's parent room or R).

Aggregating across all scripts:
  actor_cids[X]    = set of all cids ever assigned to actor X
  actor_in_rooms[X] = set of rooms actor X is ever placed in

Then drawn_in[cid] = union of actor_in_rooms[X] for every X where
cid in actor_cids[X]. This OVER-COUNTS (e.g. if an actor briefly
wears a different costume during a cutscene, we attribute every room
the actor visits to that costume) but is conservative — guarantees
no missed rooms, at the cost of some palette-budget slack.

Special tokens:
  - VAR_EGO    → actor 1 (Guybrush, the player)
  - Local[N]   → script-local variable, treat as unresolved
  - Var[N]     → global var, try to resolve via SetVar literals

Output: tools/actor_room_refs.json
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from decode_amiga_room import (load, be32, find_chunk, walk_rooms,


                                name as cn)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

AMIGA_DIR = f'{REPO_ROOT}/amiga-data'
DESCUMM = f'{REPO_ROOT}/tools/scummvm-tools/descumm'
OUT_PATH = os.path.join(os.path.dirname(__file__), 'actor_room_refs.json')

VAR_EGO_ACTOR = 1   # MI2 — Guybrush is actor 1; VAR_EGO is initialised to 1.

TOPLEVEL = ('LSCR', 'SCRP', 'EXCD', 'ENCD')

# Match "ActorOps(<actor>,[...])" — actor is a number, VAR_EGO, Var[N], Local[N]
RE_ACTOR_OPS = re.compile(
    r'ActorOps\(\s*(?P<actor>VAR_EGO|Local\[\d+\]|Var\[\d+\]|\d+)\s*,\s*\['
    r'(?P<body>[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*)\]'
)
# Match Costume(N) or Costume(Var[K]) within a body
RE_COSTUME_LIT = re.compile(r'Costume\((\d+)\)')
RE_COSTUME_VAR = re.compile(r'Costume\(Var\[(\d+)\]\)')

# Various putActor calls
RE_PUT_ACTOR = re.compile(
    r'putActor\(\s*(?P<actor>VAR_EGO|Local\[\d+\]|Var\[\d+\]|\d+)\s*'
)
RE_PUT_IN_ROOM = re.compile(
    r'putActorInRoom\(\s*(?P<actor>VAR_EGO|Local\[\d+\]|Var\[\d+\]|\d+)\s*,'
    r'\s*(?P<room>\d+|Var\[\d+\]|VAR_ROOM)\s*\)'
)
RE_PUT_AT_OBJ = re.compile(
    r'putActorAtObject\(\s*(?P<actor>VAR_EGO|Local\[\d+\]|Var\[\d+\]|\d+)\s*'
)
# animateCostume(actor, frame) — proves actor IS drawn in this script's room
RE_ANIMATE_COST = re.compile(
    r'animateCostume\(\s*(?P<actor>VAR_EGO|Local\[\d+\]|Var\[\d+\]|\d+)\s*,'
)

RE_VAR_ASSIGN_LIT = re.compile(r'Var\[(\d+)\]\s*=\s*(\d+)\s*;')


def descumm_chunk(chunk_bytes):
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk_bytes)
        path = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', path],
                           capture_output=True, text=True, timeout=15)
    finally:
        os.unlink(path)
    return r.stdout if r.returncode == 0 else ''


def collect_room_chunks(d, ro, room_size):
    p = ro + 8
    end = ro + room_size
    while p < end:
        if p + 8 > end:
            break
        tag = cn(d, p)
        sz = be32(d, p + 4)
        if sz < 8:
            break
        if tag in TOPLEVEL:
            yield (tag, bytes(d[p : p + sz]))
        elif tag == 'OBCD':
            verb_off, verb_sz = find_chunk(d, p + 8, p + sz, 'VERB')
            if verb_off is not None:
                yield ('VERB', bytes(d[verb_off : verb_off + verb_sz]))
        p += sz


def resolve_actor_token(token, var_state):
    """Return actor id (int) or None if unresolved."""
    if token == 'VAR_EGO':
        return VAR_EGO_ACTOR
    if token.startswith('Local['):
        return None
    m = re.fullmatch(r'Var\[(\d+)\]', token)
    if m:
        return var_state.get(int(m.group(1)))
    try:
        return int(token)
    except ValueError:
        return None


def resolve_room_token(token, var_state, fallback_room):
    """Returns rid or None."""
    if token == 'VAR_ROOM':
        return fallback_room
    m = re.fullmatch(r'Var\[(\d+)\]', token)
    if m:
        return var_state.get(int(m.group(1)))
    try:
        rid = int(token)
        return rid if rid > 0 else None
    except ValueError:
        return None


def parse_dcos_full(idx_bytes):
    cost_home = {}
    room_disk = {}
    p = 0
    while p < len(idx_bytes):
        tag = cn(idx_bytes, p); sz = be32(idx_bytes, p + 4)
        if tag == 'DROO':
            count = struct.unpack('<H', idx_bytes[p + 8:p + 10])[0]
            for rid, fn in enumerate(idx_bytes[p + 10:p + 10 + count]):
                if 0 < fn < 12:
                    room_disk[rid] = fn
        elif tag == 'DCOS':
            count = struct.unpack('<H', idx_bytes[p + 8:p + 10])[0]
            for cid, rid in enumerate(idx_bytes[p + 10:p + 10 + count]):
                if rid > 0:
                    cost_home[cid] = rid
        p += sz
    return cost_home, room_disk


def main():
    idx = load(f'{AMIGA_DIR}/monkey2.000')
    cost_home, room_disk = parse_dcos_full(idx)

    disks = {n: load(f'{AMIGA_DIR}/monkey2.{n:03d}')
             for n in range(1, 12)
             if os.path.exists(f'{AMIGA_DIR}/monkey2.{n:03d}')}

    actor_cids = defaultdict(set)        # actor_id -> set(cid) — every Init seen
    actor_in_rooms = defaultdict(set)    # actor_id -> set(rid)
    drawn_local = defaultdict(set)       # cid -> set(rid) — non-Init temp swaps
    # Per-room actor-cid bindings: (rid, actor) -> set(cid). Tightens
    # over-counting by only attributing a cid to rooms whose scripts
    # actually place/animate that actor (= it's on-screen).
    per_room_actor_cids = defaultdict(set)   # (rid, actor) -> set(cid)
    per_room_actor_active = defaultdict(set)  # rid -> set(actor) (placed/animated)
    unresolved = []
    n_chunks = 0

    # Pass 1: walk every script. Two passes per script — first build local
    # var-state from `Var[K] = N;` assignments seen in this script, then
    # resolve actor/cid/room using that state.
    for disk_n, d in disks.items():
        for rid, ro in walk_rooms(d):
            rsz = be32(d, ro + 4)
            for tag, chunk in collect_room_chunks(d, ro, rsz):
                disasm = descumm_chunk(chunk)
                if not disasm:
                    continue
                n_chunks += 1

                # Build local var state from sequential walk
                var_state = {}
                # Process the disasm line-by-line so var assignments
                # before a costume/put-actor call are visible.
                lines = disasm.split('\n')
                for ln in lines:
                    # Update var state on assignment
                    for vm in RE_VAR_ASSIGN_LIT.finditer(ln):
                        var_state[int(vm.group(1))] = int(vm.group(2))

                    # ActorOps(<actor>, [<body>])
                    for am in RE_ACTOR_OPS.finditer(ln):
                        actor = resolve_actor_token(am.group('actor'), var_state)
                        if actor is None:
                            unresolved.append((rid, tag, 'actor',
                                                 am.group('actor')))
                            continue
                        body = am.group('body')
                        has_init = 'Init()' in body
                        for cm in RE_COSTUME_LIT.finditer(body):
                            cid = int(cm.group(1))
                            if has_init:
                                # Init in THIS script binds (actor, cid) to
                                # the script's parent room. Used in pass 2
                                # along with per_room_actor_active.
                                per_room_actor_cids[(rid, actor)].add(cid)
                                actor_cids[actor].add(cid)  # global record
                            else:
                                drawn_local[cid].add(rid)
                        for cm in RE_COSTUME_VAR.finditer(body):
                            v = var_state.get(int(cm.group(1)))
                            if v is not None and v in cost_home:
                                if has_init:
                                    per_room_actor_cids[(rid, actor)].add(v)
                                    actor_cids[actor].add(v)
                                else:
                                    drawn_local[v].add(rid)
                            else:
                                unresolved.append((rid, tag, 'cid',
                                                     cm.group(0)))

                    # putActor(N, x, y) — script's parent room
                    for pm in RE_PUT_ACTOR.finditer(ln):
                        actor = resolve_actor_token(pm.group('actor'), var_state)
                        if actor is not None:
                            actor_in_rooms[actor].add(rid)
                            per_room_actor_active[rid].add(actor)

                    # putActorAtObject — script's parent room
                    for pm in RE_PUT_AT_OBJ.finditer(ln):
                        actor = resolve_actor_token(pm.group('actor'), var_state)
                        if actor is not None:
                            actor_in_rooms[actor].add(rid)
                            per_room_actor_active[rid].add(actor)

                    # putActorInRoom(N, R) — explicit room
                    for pm in RE_PUT_IN_ROOM.finditer(ln):
                        actor = resolve_actor_token(pm.group('actor'), var_state)
                        room = resolve_room_token(pm.group('room'), var_state,
                                                    rid)
                        if actor is not None and room is not None:
                            actor_in_rooms[actor].add(room)
                            per_room_actor_active[room].add(actor)

                    # animateCostume(N, frame) — proof actor N is drawn in
                    # this script's parent room.
                    for am2 in RE_ANIMATE_COST.finditer(ln):
                        actor = resolve_actor_token(am2.group('actor'), var_state)
                        if actor is not None:
                            actor_in_rooms[actor].add(rid)
                            per_room_actor_active[rid].add(actor)

        sys.stderr.write(f'[disk {disk_n}] chunks: {n_chunks}, '
                         f'actors with cid bindings: {len(actor_cids)}, '
                         f'actors placed: {len(actor_in_rooms)}\n')

    # Build drawn_in tightly:
    #   - For each room R and each actor A active in R, the cid is the
    #     INTERSECTION: per_room_actor_cids[(R, A)] (Init in this room),
    #     OR globally-known actor_cids[A] only when no per-room binding
    #     exists in R (= persistent state from another room's Init).
    #   - PLUS non-Init temporary swaps go directly to script's room.
    drawn_in = defaultdict(set)
    for rid_active, actors in per_room_actor_active.items():
        for actor in actors:
            local_cids = per_room_actor_cids.get((rid_active, actor))
            if local_cids:
                # Room R Init'd actor — only those cids are drawn here
                for cid in local_cids:
                    drawn_in[cid].add(rid_active)
            else:
                # No Init in R for this actor — persists from elsewhere.
                # Use globally-known cids as the candidate set.
                for cid in actor_cids.get(actor, ()):
                    drawn_in[cid].add(rid_active)
    for cid, rooms in drawn_local.items():
        drawn_in[cid] |= rooms

    out = {
        'cost_home': cost_home,
        'room_disk': room_disk,
        'actor_cids': {str(a): sorted(cs) for a, cs in actor_cids.items()},
        'actor_in_rooms': {str(a): sorted(rs)
                            for a, rs in actor_in_rooms.items()},
        'drawn_in': {str(c): sorted(rs) for c, rs in drawn_in.items()},
        'unresolved': unresolved,
        'stats': {
            'chunks_scanned': n_chunks,
            'unresolved_count': len(unresolved),
        },
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, indent=2)
    sys.stderr.write(f'\nWrote {OUT_PATH}\n')

    # Summary
    multi = sorted(((int(c), len(rs)) for c, rs in drawn_in.items() if len(rs) >= 2),
                   key=lambda t: -t[1])
    print(f'Multi-room costumes (drawn_in via actor model):')
    for c, n in multi[:30]:
        print(f'  cid {c:3d}: {n} rooms')
    if len(multi) > 30:
        print(f'  ... ({len(multi) - 30} more)')


if __name__ == '__main__':
    main()
