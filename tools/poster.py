#!/usr/bin/env python3
"""Compose a vertical poster from a set of side-by-side comparison PNGs."""
import os, sys
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))



CMP = f'{REPO_ROOT}/preview/comparisons'
OUT = f'{REPO_ROOT}/preview/poster.png'

# Order by visual punch (largest delta + iconic rooms first)
order = ['bar', 'campfire', 'shore', 'inn', 'laundry', 'voodoo']

imgs = []
for name in order:
    p = os.path.join(CMP, f'{name}.png')
    if os.path.exists(p):
        imgs.append(Image.open(p).convert('RGB'))

if not imgs:
    print("no comparisons to poster")
    sys.exit(0)

w = max(i.width for i in imgs)
h = sum(i.height for i in imgs)
canvas = Image.new('RGB', (w, h), (12, 12, 12))
y = 0
for i in imgs:
    canvas.paste(i, (0, y))
    y += i.height
canvas.save(OUT)
print(f"-> {OUT} ({canvas.size[0]}x{canvas.size[1]})")
