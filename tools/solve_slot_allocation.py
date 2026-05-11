#!/usr/bin/env python3
"""Solve cost-group palette-slot allocation.

Each group has a set of cids; each cid is drawn in a known set of rooms.
Two groups can SHARE the same palette slot iff none of their cids are
ever drawn in the same room (otherwise that slot would need two
different canonical RGBs at once and one group renders wrong).

Inputs:
  tools/cost_groups.json        groups + current pal_indices
  tools/costume_refs.json       drawn_in[cid] = list of rids
  tools/global_actor_palette.json  Guybrush pal-index range

Free pool = palette slots [0..31] minus system-reserved
{0=transparent, 1=HW-cursor, 17=white(SMAP-bug), 22..31=Guybrush}.

Default target for each group = current `len(pal_indices)`. The solver
finds a valid assignment that maximises the weighted sum of slots, with
each group capped at its target. Override per-group targets via
--target NAME:COUNT.

Usage:
  python3 tools/solve_slot_allocation.py
  python3 tools/solve_slot_allocation.py --target extras_a_warm:6 extras_c:4
  python3 tools/solve_slot_allocation.py --apply
  python3 tools/solve_slot_allocation.py --best-s2 --max-slots 9 --apply
        # Probe: for each group g and slot count k in [0..max_slots]
        # run png2amiga and record S2. Then run the DP with S2 as the
        # per-group reward instead of (cid_count * slots). BEST env var
        # propagates to the probe encodes; BEST=0 ≈ 8 min wall, BEST=1
        # is ~5-10× slower.
"""

import argparse
import functools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
GROUPS_F = os.path.join(REPO_ROOT, 'tools', 'cost_groups.json')
REFS_F   = os.path.join(REPO_ROOT, 'tools', 'costume_refs.json')
GLOBAL_F = os.path.join(REPO_ROOT, 'tools', 'global_actor_palette.json')


def system_reserved():
    res = {0, 1, 17}
    try:
        gp = json.load(open(GLOBAL_F))['guybrush']
        for s in range(gp['pal_index_start'], gp['pal_index_end'] + 1):
            res.add(s)
    except (FileNotFoundError, KeyError):
        pass
    return res


def build_clash(groups, drawn):
    G = len(groups)
    rooms_of = []
    for g in groups:
        rs = set()
        for cid in g['cids']:
            rs |= set(drawn.get(str(cid), []))
        rooms_of.append(rs)
    clash = [[False] * G for _ in range(G)]
    for i in range(G):
        for j in range(i + 1, G):
            if rooms_of[i] & rooms_of[j]:
                clash[i][j] = clash[j][i] = True
    return clash


def enumerate_indep_sets(clash):
    """Every non-empty independent set of the clash graph as a bitmask.
    Skip the empty set — assigning a slot to nobody is always wasteful."""
    G = len(clash)
    out = []
    for mask in range(1, 1 << G):
        ok = True
        bits = [i for i in range(G) if (mask >> i) & 1]
        for a in range(len(bits)):
            for b in range(a + 1, len(bits)):
                if clash[bits[a]][bits[b]]:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            out.append(mask)
    return out


def measure_s2_curves(groups, pool, max_slots, encoded_groups=None):
    """For each group g and each slot count k in [0..max_slots], run
    png2amiga via encode_global_extras.quantize_group and record S2.
    Returns dict {group_name: {k: s2_or_None}}.

    `encoded_groups` (optional set) restricts which groups to probe;
    groups not in the set get only their current allocation measured.
    """
    # encode_global_extras requires PNG2AMIGA at import time. Match the
    # default that build.sh / regen_globals.sh use so the solver runs
    # standalone (`tools/solve_slot_allocation.py --best-s2` from a
    # fresh shell), not just when build.sh has exported it for us.
    os.environ.setdefault(
        'PNG2AMIGA', os.path.expanduser('~/png2amiga/build/png2amiga'))
    import encode_global_extras as ege  # noqa: E402

    palette_data = json.load(open(ege.GAP_PATH))
    if 'guybrush' not in palette_data:
        sys.exit('global_actor_palette.json missing guybrush — '
                 'run encode_global_guybrush.py first')
    gp = palette_data['guybrush']
    guybrush_locks = []
    for slot_str, rgb in gp['canonical_amiga'].items():
        slot = int(slot_str)
        pi = slot - 16
        if 0 <= pi <= 31:
            guybrush_locks.append({
                'pi': pi,
                'rgb': f'{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}',
            })

    pc_data, pc_dcos, pc_room_off = ege.load_pc_data()

    # Render each non-empty group's frames once; quantize_group will
    # then re-use the same PNGs for every slot-count probe.
    layouts = {}
    for g in groups:
        name = g['name']
        if not g['cids']:
            continue
        if encoded_groups is not None and name not in encoded_groups:
            continue
        frames_dir = f'{ege.WORKDIR}/{name}_frames'
        layout = ege.render_group_frames(
            g, pc_data, pc_dcos, pc_room_off, frames_dir)
        if layout:
            layouts[name] = layout
            print(f'  staged {len(layout)} frames for {name}')

    s2_table = {g['name']: {} for g in groups}
    for k in range(max_slots + 1):
        # Use the first k pool slots as a generic candidate; for the
        # S2 lookup only the slot COUNT matters — slot identity is
        # interchangeable as long as it's not on a Guybrush lock or
        # system-reserved slot.
        pal_indices = pool[:k]
        for g in groups:
            name = g['name']
            if name not in layouts:
                s2_table[name][k] = None
                continue
            lock_rgbs = {int(kk): v for kk, v in
                         (g.get('lock_rgbs') or {}).items()
                         if not kk.startswith('_')}
            try:
                _, s2 = ege.quantize_group(
                    layouts[name], pal_indices, guybrush_locks, lock_rgbs)
            except Exception as e:
                print(f'  [warn] {name} k={k}: {e}')
                s2 = None
            s2_table[name][k] = s2
            shown = f'{s2:.2f}' if s2 is not None else '—'
            print(f'  k={k:2d}  {name:18s} S2={shown}')
    return s2_table


def solve_dp_s2(s2_table, indep_sets, pool_size, group_names):
    """DP: pick non-negative integer counts x[i] per indep_set such
    that sum_i x[i] <= pool_size, maximise sum_g s2_table[g][actual[g]]
    where actual[g] = sum_{i: g in indep_set[i]} x[i].
    Returns (best_objective, [(indep_set_idx, count), ...])."""
    G = len(group_names)
    n = len(indep_sets)
    set_groups = [[g for g in range(G) if (m >> g) & 1] for m in indep_sets]
    # Cap each group at the highest slot count we actually have a valid
    # S2 for — empty groups (no cids → no S2 measurements at all) collapse
    # to max_k=0, so the DP won't allocate them any slots even when a
    # larger indep-set containing them would be "free".
    max_k = []
    for g in range(G):
        name = group_names[g]
        valid = [k for k, v in s2_table[name].items() if v is not None]
        max_k.append(max(valid) if valid else 0)

    def end_score(actual):
        s = 0.0
        for g in range(G):
            v = s2_table[group_names[g]].get(actual[g])
            if v is not None:
                s += v
        return s

    @functools.lru_cache(maxsize=None)
    def dp(i, remaining_pool, actual):
        if i == n or remaining_pool == 0:
            return end_score(actual), ()
        gs = set_groups[i]
        max_x = remaining_pool
        for g in gs:
            cap = max_k[g] - actual[g]
            if cap < max_x:
                max_x = cap
        best_v, best_a = float('-inf'), ()
        for x in range(max_x + 1):
            if x:
                new_actual = list(actual)
                for g in gs:
                    new_actual[g] += x
                v, a = dp(i + 1, remaining_pool - x, tuple(new_actual))
                if v > best_v:
                    best_v, best_a = v, ((i, x),) + a
            else:
                v, a = dp(i + 1, remaining_pool, actual)
                if v > best_v:
                    best_v, best_a = v, a
        return best_v, best_a

    val, assignment = dp(0, pool_size, tuple(0 for _ in range(G)))
    return val, list(assignment)


def solve_dp(target, indep_sets, pool_size, weight):
    """Pick non-negative integer counts x[i] for each indep_set such that
       sum x[i] <= pool_size
       for each g: sum_{i: g in indep_set[i]} x[i] <= target[g]
       maximise:   sum_i x[i] * sum_{g in indep_set[i]} weight[g]
    Returns (best_value, [(indep_set_idx, count), ...])."""
    G = len(target)
    n = len(indep_sets)
    set_groups = [[g for g in range(G) if (s >> g) & 1] for s in indep_sets]
    set_value  = [sum(weight[g] for g in gs) for gs in set_groups]

    @functools.lru_cache(maxsize=None)
    def dp(i, remaining_pool, remaining_target):
        if i == n or remaining_pool == 0:
            return 0, ()
        gs = set_groups[i]
        c  = set_value[i]
        max_x = remaining_pool
        for g in gs:
            if remaining_target[g] < max_x:
                max_x = remaining_target[g]
        best_v, best_a = -1, ()
        for x in range(max_x + 1):
            if x:
                new_t = list(remaining_target)
                for g in gs:
                    new_t[g] -= x
                sub_v, sub_a = dp(i + 1, remaining_pool - x, tuple(new_t))
                val = sub_v + c * x
                if val > best_v:
                    best_v, best_a = val, ((i, x),) + sub_a
            else:
                sub_v, sub_a = dp(i + 1, remaining_pool, remaining_target)
                if sub_v > best_v:
                    best_v, best_a = sub_v, sub_a
        return best_v, best_a

    val, assign = dp(0, pool_size, tuple(target))
    return val, list(assign)


def assign_concrete_slots(indep_sets, assignment, groups, pool):
    """Walk the pool and stamp each slot with its indep_set's groups."""
    pool = list(pool)
    out = {g['name']: [] for g in groups}
    # Bigger indep sets first → packs the heaviest sharing into low slots
    assignment_sorted = sorted(
        assignment, key=lambda x: -bin(indep_sets[x[0]]).count('1'))
    for i, count in assignment_sorted:
        mask = indep_sets[i]
        members = [g['name'] for j, g in enumerate(groups) if (mask >> j) & 1]
        for _ in range(count):
            if not pool:
                break
            slot = pool.pop(0)
            for n in members:
                out[n].append(slot)
    return out


def main():
    parser = argparse.ArgumentParser(
        description='Solve cost-group palette-slot allocation.')
    parser.add_argument('--target', action='append', default=[],
                        metavar='NAME:COUNT',
                        help='cap a group at COUNT slots (default = current)')
    parser.add_argument('--weight', action='append', default=[],
                        metavar='NAME:W',
                        help='objective weight (default = group cid count)')
    parser.add_argument('--apply', action='store_true',
                        help='write proposed pal_indices back to '
                             'cost_groups.json (preserves group ordering, '
                             'cids, _note, etc.)')
    parser.add_argument('--best-s2', action='store_true',
                        help='measure per-group S2 vs slot count via '
                             'png2amiga, then maximise total S2 instead '
                             'of cid-weighted slot count')
    parser.add_argument('--max-slots', type=int, default=None,
                        metavar='N',
                        help='probe slot counts 0..N (default: '
                             'min(pool_size, 10))')
    args = parser.parse_args()

    config = json.load(open(GROUPS_F))
    refs   = json.load(open(REFS_F))
    drawn  = refs['drawn_in']
    groups = config['groups']

    G = len(groups)
    target = [len(g['pal_indices']) for g in groups]
    weight = [max(1, len(g['cids'])) for g in groups]
    name_to_idx = {g['name']: i for i, g in enumerate(groups)}

    for spec in args.target:
        n, _, c = spec.partition(':')
        if n not in name_to_idx:
            sys.exit(f'unknown group: {n}')
        target[name_to_idx[n]] = int(c)
    for spec in args.weight:
        n, _, w = spec.partition(':')
        if n not in name_to_idx:
            sys.exit(f'unknown group: {n}')
        weight[name_to_idx[n]] = int(w)

    reserved = system_reserved()
    pool = sorted(s for s in range(32) if s not in reserved)
    pool_size = len(pool)

    clash = build_clash(groups, drawn)
    indep_sets = enumerate_indep_sets(clash)

    print(f'free pool: {pool}  ({pool_size} slots)')
    print(f'reserved : {sorted(reserved)}')
    print()
    print(f'{"group":18s}  cids  weight  current  target')
    for i, g in enumerate(groups):
        print(f"  {g['name']:18s} {len(g['cids']):4d}  {weight[i]:6d}  "
              f"{len(g['pal_indices']):7d}  {target[i]:6d}")
    print()

    n_clashes = sum(clash[i][j] for i in range(G) for j in range(i + 1, G))
    print(f'clash graph: {n_clashes} edge(s), '
          f'{len(indep_sets)} non-empty independent sets')
    for i in range(G):
        for j in range(i + 1, G):
            if clash[i][j]:
                print(f"  {groups[i]['name']} <-> {groups[j]['name']}")
    print()

    if args.best_s2:
        max_k = args.max_slots if args.max_slots is not None \
                                else min(pool_size, 10)
        print(f'== probing S2 vs slot count (k=0..{max_k}) ==')
        s2_table = measure_s2_curves(groups, pool, max_k)
        print()
        print('S2 vs slot count:')
        header = f"  {'group':18s}" + \
                 ''.join(f'  k={k:<2d}'.rjust(8) for k in range(max_k + 1))
        print(header)
        for g in groups:
            row = s2_table[g['name']]
            cells = []
            for k in range(max_k + 1):
                v = row.get(k)
                cells.append(f'{v:7.2f}' if v is not None else '      —')
            print(f"  {g['name']:18s} " + ' '.join(cells))
        print()
        names = [g['name'] for g in groups]
        val, assignment = solve_dp_s2(s2_table, indep_sets, pool_size, names)
        concrete = assign_concrete_slots(indep_sets, assignment, groups, pool)
        obj_kind = 'sum of per-group S2'
    else:
        val, assignment = solve_dp(target, indep_sets, pool_size, weight)
        if val < 0:
            sys.exit('INFEASIBLE: no assignment found')
        concrete = assign_concrete_slots(indep_sets, assignment, groups, pool)
        obj_kind = 'slot-weight sum, weights = cids by default'

    print(f'objective: {val}  ({obj_kind})')
    print()
    print(f'{"group":18s}  was        ->  proposed')
    unique = set()
    n_assign = 0
    for g in groups:
        was = sorted(g['pal_indices'])
        new = sorted(concrete[g['name']])
        flag = ' ' if was == new else '*'
        print(f"  {g['name']:18s} {flag} {str(was):24s} -> {new}")
        unique |= set(new)
        n_assign += len(new)
    free = sorted(set(pool) - unique)
    print(f'\nunique pool slots used: {len(unique)}/{pool_size}  '
          f'(group-slot assignments: {n_assign})')
    if free:
        print(f'leftover free slots: {free}')

    # Per-set sharing summary so the user can see who shares with whom.
    print('\nshared-slot summary:')
    for i, count in assignment:
        if count == 0:
            continue
        mask = indep_sets[i]
        members = [groups[g]['name'] for g in range(G) if (mask >> g) & 1]
        print(f'  {count} slot(s) shared by: {members}')

    if args.apply:
        # Targeted text edit so we keep the user's compact one-line array
        # style for cids / non-touched pal_indices and don't mangle
        # unicode in the _note/_comment.
        import re
        text = open(GROUPS_F).read()
        n_changed = 0
        for g in groups:
            new_slots = sorted(concrete[g['name']])
            new_arr = '[' + ', '.join(str(s) for s in new_slots) + ']'
            # match the group's `"name": "<n>",` then any text up to its
            # `"pal_indices": [...]`
            pat = (r'("name":\s*"' + re.escape(g['name']) + r'".*?'
                   r'"pal_indices":\s*)\[[^\]]*\]')
            new_text, n = re.subn(pat, r'\g<1>' + new_arr, text,
                                   count=1, flags=re.DOTALL)
            if n != 1:
                sys.exit(f'apply: failed to locate pal_indices for {g["name"]}')
            text = new_text
            n_changed += 1
        with open(GROUPS_F, 'w') as f:
            f.write(text)
        print(f'\nwrote {GROUPS_F}  ({n_changed} group(s) updated)')


if __name__ == '__main__':
    main()
