#!/usr/bin/env python3
"""Score all available (PC, Amiga 1992, png2amiga --best) triples with SSIMULACRA2."""
import os, subprocess, sys
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



PC_DIR    = f'{REPO_ROOT}/extracted-pc-pngs/IMAGES/backgrounds'
AMIGA_DIR = f'{REPO_ROOT}/preview/amiga-rooms'
BEST_DIR  = f'{REPO_ROOT}/preview/best-from-pc'
SSIM2     = '/opt/homebrew/bin/ssimulacra2'


def score(ref, dist):
    try:
        r = subprocess.check_output([SSIM2, ref, dist], stderr=subprocess.STDOUT, text=True)
        return float(r.strip().split()[0])
    except Exception:
        return float('nan')


def main():
    # Collect (room_name, pc_path, amiga_path, best_path) for every room with both
    rows = []
    for f in sorted(os.listdir(PC_DIR)):
        if not f.endswith('.png') or '_' not in f: continue
        rn = f.split('_', 1)[1].rsplit('.', 1)[0]
        pc = os.path.join(PC_DIR, f)
        am = None
        for g in os.listdir(AMIGA_DIR):
            if g.endswith('.png') and '_' in g and g.split('_', 1)[1].rsplit('.', 1)[0] == rn:
                am = os.path.join(AMIGA_DIR, g); break
        bp = os.path.join(BEST_DIR, f'{rn}.png')
        if not (am and os.path.exists(bp)): continue
        # Size sanity
        if Image.open(pc).size != Image.open(am).size: continue
        rows.append((rn, pc, am, bp))
    print(f"Scoring {len(rows)} rooms with ssimulacra2 (higher = closer to PC ground truth)")
    print(f"Reference: 30=low, 50=med, 70=high, 90=transparent\n")
    print(f"{'room':<14}{'amiga':>8}{'best':>8}{'delta':>8}")
    print('-' * 44)
    deltas = []
    a_avg = 0.0; b_avg = 0.0
    for rn, pc, am, bp in rows:
        a = score(pc, am)
        b = score(pc, bp)
        d = b - a
        deltas.append((rn, a, b, d))
        a_avg += a; b_avg += b
        print(f"{rn:<14}{a:>8.2f}{b:>8.2f}{d:>+8.2f}")
    if rows:
        n = len(rows)
        print('-' * 44)
        print(f"{'AVERAGE':<14}{a_avg/n:>8.2f}{b_avg/n:>8.2f}{(b_avg-a_avg)/n:>+8.2f}")
        # Top wins / losses
        deltas.sort(key=lambda x: -x[3])
        print(f"\nTop 5 wins: {[(r[0], f'+{r[3]:.1f}') for r in deltas[:5]]}")
        print(f"Bottom 5:   {[(r[0], f'{r[3]:+.1f}') for r in deltas[-5:]]}")


if __name__ == '__main__':
    main()
