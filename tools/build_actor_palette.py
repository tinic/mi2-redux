#!/usr/bin/env python3
"""Build a "global actor sub-palette" for Guybrush (cid 1) — the canonical
RGB at each pal_table truncation slot, applied identically in every room
he's drawn in.

Background:
  Guybrush's pal_table = [250, 193..207, 192]. On real Amiga those values
  are truncated to 5 bits and offset by 16, landing on CLUT slots
  [16..26, 28..31, 42]. The artist designed each pristine room's CLUT at
  those slots so Guybrush rendered correctly per scene — but this means
  cross-room walks could see slightly different Guybrush colours.

  For our re-encode we choose ONE canonical Guybrush palette (most-common
  pristine RGB at each slot across his 38 drawn-in rooms) and lock it
  into every room he appears in. Trade-off: lose a bit of per-scene
  artistic variation; gain consistent Guybrush across the entire game.

Output:
  tools/global_actor_palette.json — { "guybrush": { "16": [r,g,b], ... } }
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from pristine_cache import cache

REFS_PATH = os.path.join(os.path.dirname(__file__), 'costume_refs.json')
OUT_PATH = os.path.join(os.path.dirname(__file__), 'global_actor_palette.json')


def main():
    refs = json.load(open(REFS_PATH))
    drawn_in = refs['drawn_in']

    # Guybrush is cid 1.
    gb_rooms = sorted(drawn_in.get('1', []))
    if not gb_rooms:
        sys.exit("no Guybrush rooms found in costume_refs.json")

    # His pal_table (read from his home costume in pristine cache).
    gb_home_rid = cache.cost_home(1)
    home = cache.room(gb_home_rid)
    gb = next(c for c in home['costumes'] if c['cost_id'] == 1)
    pal = list(gb['pal_table'])
    # Truncation slots: 16 + (v & 31) for each non-zero / non-sentinel.
    SENTINEL = 250
    trunc_slots = sorted({16 + (v & 31)
                          for v in pal if v not in (0, SENTINEL)})

    # ScummVM render slots (= raw pal_table values, 8-bit framebuffer).
    scvm_slots = sorted({v for v in pal if v not in (0, SENTINEL)})

    # Use ONE specific room's pristine CLUT[192..207] as the canonical
    # source. Per-slot voting across rooms produces a frankenstein
    # because slot N's modal RGB comes from one set of rooms while
    # slot M's modal RGB comes from a disjoint set — combined they
    # don't match any single scene's actual Guybrush appearance.
    #
    # rid 9 (bar) is a good representative: it's the second room with
    # Guybrush in his classic dark-blue-coat-and-blond-hair MI2 attire.
    REPRESENTATIVE_RID = 9
    rep_room = cache.room(REPRESENTATIVE_RID)
    if rep_room is None:
        sys.exit(f"representative room {REPRESENTATIVE_RID} missing from cache")
    rep_clut = rep_room['clut']

    canonical = {}
    debug = {}
    for v in scvm_slots:
        slot = 16 + (v & 31)
        # Pull the colour from rid 9's CLUT at the SCVM render position v.
        # On real Amiga that exact same RGB needs to live at the truncated
        # CLUT[slot] for the rendering to match.
        rgb = (rep_clut[v*3], rep_clut[v*3+1], rep_clut[v*3+2])
        canonical[slot] = list(rgb)
        debug[slot] = {
            'rgb': '%02x%02x%02x' % rgb,
            'source': f'rid {REPRESENTATIVE_RID} CLUT[{v}]',
        }

    # ScummVM-render mirror: same canonical RGB at the high slot too,
    # so ScummVM (reading CLUT[v] directly) sees the same colour as real
    # Amiga (reading CLUT[16 + (v & 31)] after truncation).
    scvm_mirror = {}
    for v in scvm_slots:
        slot = 16 + (v & 31)
        scvm_mirror[v] = canonical[slot]

    out = {
        'guybrush': {
            'cost_id': 1,
            'home_rid': gb_home_rid,
            'rooms_drawn_in': gb_rooms,
            'pal_table': pal,
            'truncation_slots': trunc_slots,
            'scvm_slots': scvm_slots,
            'canonical_amiga': {str(k): v for k, v in canonical.items()},
            'canonical_scvm':  {str(k): v for k, v in scvm_mirror.items()},
            'debug': {str(k): v for k, v in debug.items()},
        },
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'Wrote {OUT_PATH}')
    print(f'Guybrush canonical palette ({len(canonical)} slots):')
    for slot in trunc_slots:
        d = debug[slot]
        print(f'  CLUT[{slot:2d}]  #{d["rgb"]}  ({d["source"]})')


if __name__ == '__main__':
    main()
