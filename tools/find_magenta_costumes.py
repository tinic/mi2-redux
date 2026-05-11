#!/usr/bin/env python3
"""Scan every room's costumes and report which ones have real
magenta-RGB foreground art (pal_table entries that map to a
magenta-family CLUT slot).

Output is structured so the entries can be pasted directly into
`tools/inject_room.py`'s `MAGENTA_KEEP_PER_COSTUME` dict.

Usage:
    tools/find_magenta_costumes.py            # text report
    tools/find_magenta_costumes.py --thumbs   # also save 4x thumbnails
                                              # of the first using-frame
                                              # to /tmp/magenta_costumes/
    tools/find_magenta_costumes.py --skip 87,110,46,51,56  # hide
                                              # already-listed (rid,*)
"""

import argparse
import os
import pickle
import sys
from collections import Counter

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO_ROOT + '/tools')
from decode_amiga_room import be32, name as cn  # noqa: E402

PRISTINE_CACHE = f'{REPO_ROOT}/tools/pristine_cache.pkl'
PC_INDEX       = f'{REPO_ROOT}/pc-data/MONKEY2.000'
THUMB_DIR      = '/tmp/magenta_costumes'


def is_magenta(r, g, b):
    return r >= 0x80 and b >= 0x80 and g <= 0x60 and abs(r - b) <= 0x30


def parse_room_names():
    """Map rid -> slug from monkey2.000 RNAM table."""
    out = {}
    if not os.path.exists(PC_INDEX):
        return out
    d = bytes(b ^ 0x69 for b in open(PC_INDEX, 'rb').read())
    p = 0
    while p + 8 <= len(d):
        tag = cn(d, p); sz = be32(d, p + 4)
        if tag == 'RNAM':
            body = d[p + 8:p + sz]; i = 0
            while i + 1 < len(body):
                rid = body[i]
                if rid == 0:
                    break
                slug = bytes((b ^ 0xFF) for b in body[i+1:i+10])
                slug = slug.split(b'\x00')[0].decode('latin1', errors='replace')
                out[rid] = slug
                i += 10
            return out
        if sz < 8 or p + sz > len(d):
            break
        p += sz
    return out


def scan(skip_rids=frozenset(), thumbs=False):
    if thumbs:
        from PIL import Image
        os.makedirs(THUMB_DIR, exist_ok=True)
    cache = pickle.load(open(PRISTINE_CACHE, 'rb'))
    names = parse_room_names()
    rooms = cache['rooms']
    candidates = []  # list of (rid, slug, cid, pt_indices_used, sample_frame)
    for rid in sorted(rooms):
        if rid in skip_rids:
            continue
        room = rooms[rid]
        clut = room.get('clut')
        if clut is None:
            continue
        mag_clut = {i for i in range(256)
                    if is_magenta(clut[i*3], clut[i*3+1], clut[i*3+2])}
        if not mag_clut:
            continue
        slug = names.get(rid, f'rid{rid}')
        for cost in room.get('costumes', []):
            cid = cost['cost_id']
            pt  = cost['pal_table']
            # Skip pt[0] — SCUMM-defined transparent, never rendered.
            mag_pt = {i for i, slot in enumerate(pt)
                      if i != 0 and slot in mag_clut}
            if not mag_pt:
                continue
            # Find the first frame that actually USES any of these indices,
            # so we don't flag "costume has magenta in pal_table but no frame
            # uses it" — those are no-op.
            sample_fi = None
            sample_uses = []
            total_uses = Counter()
            for fi, f in enumerate(cost['frames']):
                cnt = Counter(f['pixels'])
                used = {i: cnt[i] for i in mag_pt if cnt.get(i, 0) > 0}
                if used:
                    if sample_fi is None:
                        sample_fi = fi; sample_uses = used
                    for i, n in used.items():
                        total_uses[i] += n
            if sample_fi is None:
                continue  # costume has magenta in pt but never references it
            candidates.append((rid, slug, cid, dict(total_uses), sample_fi,
                                pt, clut))
            # Render a 4x thumbnail of the first using-frame
            if thumbs:
                f = cost['frames'][sample_fi]
                w_, h_ = f['w'], f['h']
                rgba = bytearray(w_ * h_ * 4)
                for j, idx in enumerate(f['pixels']):
                    if idx == 0 or idx >= len(pt):
                        continue
                    slot = pt[idx]
                    rgba[j*4]     = clut[slot*3]
                    rgba[j*4 + 1] = clut[slot*3 + 1]
                    rgba[j*4 + 2] = clut[slot*3 + 2]
                    rgba[j*4 + 3] = 255
                im = Image.frombytes('RGBA', (w_, h_), bytes(rgba))
                im = im.resize((w_*4, h_*4), Image.NEAREST)
                im.save(f'{THUMB_DIR}/r{rid:03d}_{slug}_cid{cid}_f{sample_fi}.png')
    return candidates


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--thumbs', action='store_true',
                    help=f'save 4x thumbnails to {THUMB_DIR}/')
    ap.add_argument('--skip', default='',
                    help='comma-separated rids to omit')
    args = ap.parse_args()
    skip = frozenset(int(s) for s in args.skip.split(',') if s)

    candidates = scan(skip_rids=skip, thumbs=args.thumbs)
    if not candidates:
        print('no costumes need an override')
        return

    print(f'{len(candidates)} costumes use magenta-CLUT pal_table indices:\n')
    print(f'{"rid":>4}  {"slug":<10s}  {"cid":>4}  {"pt indices used":<25s}  '
          f'{"sample":>7s}  CLUT mappings')
    print(f'{"-"*4}  {"-"*10}  {"-"*4}  {"-"*25}  {"-"*7}  {"-"*30}')
    for rid, slug, cid, total_uses, fi, pt, clut in candidates:
        idx_str = ', '.join(f'{i}({n})' for i, n in
                            sorted(total_uses.items()))
        clut_strs = []
        for i in sorted(total_uses):
            slot = pt[i]
            r, g, b = clut[slot*3], clut[slot*3+1], clut[slot*3+2]
            clut_strs.append(f'pt[{i}]->CLUT[{slot}]=#{r:02x}{g:02x}{b:02x}')
        print(f'{rid:>4}  {slug:<10s}  {cid:>4}  {idx_str:<25s}  '
              f'  f{fi:<3d}  {"; ".join(clut_strs)}')

    print(f'\nDict entries to add (paste into MAGENTA_KEEP_PER_COSTUME):')
    print(f'\n# auto-generated suggestions — verify each visually first')
    for rid, slug, cid, total_uses, fi, pt, clut in candidates:
        keep = sorted(total_uses.keys())
        print(f'    ({rid}, {cid}): {{{", ".join(str(i) for i in keep)}}},  '
              f'# {slug}')


if __name__ == '__main__':
    main()
