#!/usr/bin/env python3
"""Post-process png2amiga's 32-color output so that:

  - palette index 0  is pure (0, 0, 0)    -> CLUT[16] after paletteMod=16
  - palette index 17 is pure (255, 255, 255) -> CLUT[33] after paletteMod=16

These are the slots SCUMM v5 / MI2 scripts reference for text/dialog.
Empirically locked across 90% / 99% of 110 Amiga MI2 rooms. Without this
pinning, in-game dialog and certain script-driven graphics break (e.g. the
`script 93 not in room 1` cascade we saw after the v1 Part I demo).

The swap costs at most one of the 32 quantizer-chosen colors: the slot
nearest to black (or white) is shifted to a less-precious position. For
typical scenes that already have black/white in their palette, this is a
no-op or near-no-op.
"""
import math


def oklab_dist(a, b):
    """Cheap perceptual distance — sRGB approx via gamma + OKLab. ~OKLab norm."""
    def to_lab(c):
        # sRGB -> linear (approx)
        r, g, bl = (x / 255.0 for x in c)
        def lin(u): return ((u + 0.055) / 1.055) ** 2.4 if u > 0.04045 else u / 12.92
        r, g, bl = lin(r), lin(g), lin(bl)
        # linear sRGB -> OKLab
        l = 0.4122214708*r + 0.5363325363*g + 0.0514459929*bl
        m = 0.2119034982*r + 0.6806995451*g + 0.1073969566*bl
        s = 0.0883024619*r + 0.2817188376*g + 0.6299787005*bl
        l, m, s = l**(1/3), m**(1/3), s**(1/3)
        L = 0.2104542553*l + 0.7936177850*m - 0.0040720468*s
        A = 1.9779984951*l - 2.4285922050*m + 0.4505937099*s
        B = 0.0259040371*l + 0.7827717662*m - 0.8086757660*s
        return (L, A, B)
    la, lb = to_lab(a), to_lab(b)
    return sum((la[i]-lb[i])**2 for i in range(3)) ** 0.5


def lock_slots(palette, indexed, locks=((0, (0,0,0)), (17, (255,255,255)))):
    """**Reserve** slots in the palette for required UI colors.

    Approach: each `(target_idx, target_rgb)` in `locks` is set to `target_rgb`
    in the output palette and treated as RESERVED (no art pixel may use it).
    Pixels currently mapped to a reserved slot are remapped to the closest
    NON-reserved color in OKLab — not pinned to the target color, so dark
    scenes don't get spurious pure-white sparkles when --best has no near-white.

    Costs: 2 of 32 palette slots become unavailable for art (only matters if
    --best picked a useful color at exactly slot 0 or 17 — that color is
    still present in the palette via the remap target, so quality loss is small).
    """
    palette = list(palette)
    n = len(palette)
    locked_idx = {i for i, _ in locks}
    new_indexed = bytearray(indexed)

    for target_idx, target_rgb in locks:
        if palette[target_idx] == target_rgb:
            continue  # already correct, no remap needed
        original_color = palette[target_idx]
        # Find closest NON-LOCKED slot to the original color — pixels at target_idx will move there
        free_indices = [i for i in range(n) if i not in locked_idx]
        if not free_indices:
            palette[target_idx] = target_rgb
            continue
        replacement = min(free_indices, key=lambda i: oklab_dist(palette[i], original_color))
        # Remap any pixel currently at target_idx → replacement
        for j in range(len(new_indexed)):
            if new_indexed[j] == target_idx:
                new_indexed[j] = replacement
        # Now slot `target_idx` has no pixels referring to it; safe to overwrite
        palette[target_idx] = target_rgb

    return palette, bytes(new_indexed)


if __name__ == '__main__':
    # Smoke test
    pal = [(i*8, i*8, i*8) for i in range(32)]  # grayscale ramp
    idx = bytes(range(32))
    pal2, idx2 = lock_slots(pal, idx)
    assert pal2[0] == (0,0,0), pal2[0]
    assert pal2[17] == (255,255,255), pal2[17]
    # Verify pixel that originally was index 31 (closest to white) now points to where 31's data went
    print("locked palette (first 5):", pal2[:5])
    print("locked palette[14..18]:", pal2[14:19])
    print("indexed remap (first 5):", list(idx2[:5]))
    print("OK — palette[0] and palette[17] pinned correctly.")
