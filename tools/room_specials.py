"""Per-room special-case overrides for the mi2-redux injection pipeline.

The default pipeline in inject_room.py works for the common case (bg quality
+ object frames re-encoded against a shared 32-colour palette with locked
costume slots). A small set of rooms need extra surgery — for example, the
1992 Amiga port shipped with the bottom-of-screen water animation in
dred-deck disabled (the OBIM frames exist, but the script that cycles them
is absent). Those fixes go here, indexed by room slug.

Each override is an instance of `RoomOverride` with optional hooks:

  - extra_locks(orig_clut: list[tuple[int,int,int]]) -> list[list[str]]
        Returns extra png2amiga CLI args to APPEND to lock_args, e.g.
        [['--lock-index', '5', 'AABBCC'],
         ['--reserve-range', '20', '112233']]
        These extend the default base/costume locks; the room's `--lock-index 17`
        white reserve is always added by inject_room.py and need not be
        repeated here.

  - skip_obim(obj_id: int) -> bool
        Return True to skip re-encoding a specific OBIM. The original Amiga
        SMAP for that object is left untouched. Use this when an object is
        scripted-overlay-only and its re-encoding causes ScummVM artifacts.

  - extra_patches(d_mut: bytearray, room_off: int, room_size: int,
                  palette: list[tuple[int,int,int]]) -> list[dict]
        Returns extra chunk-replacement patches to merge with the standard
        SMAP/OBIM/CLUT list BEFORE apply_patches runs. Each entry is the
        same dict shape used in inject_room.py:
        {'offset': chunk_off, 'old_size': chunk_size_including_header,
         'new_body': bytes, 'parents': [parent_chunk_offsets]}.
        Use this to grow/replace chunks like CYCL (palette cycling) or to
        inject a new chunk by writing it adjacent to an existing one.

  - region_constraint(w: int, h: int) -> tuple[bytearray, dict[int,set[int]]] | None
        Per-pixel restriction on which palette slots a bg pixel can route to.
        Returns (mask, slot_sets) where mask[y*w+x] is a region id (0,1,...)
        and slot_sets maps each region id to the set of palette indices that
        region's pixels are allowed to use. Used together with extra_locks +
        a CYCL patch to confine palette cycling to specific bg areas — e.g.
        dred-deck reserves palette[28..31] for water-blues and limits those
        slots to the bottom 24 rows so sky pixels don't animate too.

  - post_patch(d_mut: bytearray, room_off: int, room_size: int,
               palette: list[tuple[int,int,int]],
               new_clut_full: list[tuple[int,int,int]]) -> None
        Mutates the patched data buffer AFTER bg/OBIM/CLUT patching but
        BEFORE the buffer is written to disk. Used for surgical edits like
        backporting a missing animation-trigger script byte from PC.
        room_off / room_size locate the room chunk in d_mut.

Add new entries by extending ROOM_OVERRIDES at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))




@dataclass
class RoomOverride:
    extra_locks: Optional[Callable] = None
    skip_obim: Optional[Callable] = None
    extra_patches: Optional[Callable] = None
    region_constraint: Optional[Callable] = None
    post_patch: Optional[Callable] = None
    notes: str = ''


# ---------------------------------------------------------------------------
# Per-room overrides — keyed by room slug (e.g. 'dred-deck', 'campfire').
# Add entries below as concrete special cases come up. Empty dict = no
# overrides; the default pipeline handles every room until proven otherwise.
# ---------------------------------------------------------------------------

def _ocs_snap(rgb):
    return tuple(((c >> 4) & 0xF) * 0x11 for c in rgb)


def _oklab(rgb):
    def s2l(c):
        x = c / 255.0
        return ((x + 0.055) / 1.055) ** 2.4 if x > 0.04045 else x / 12.92
    r, g, b = (s2l(c) for c in rgb)
    l = 0.4122214708*r + 0.5363325363*g + 0.0514459929*b
    m = 0.2119034982*r + 0.6806995451*g + 0.1073969566*b
    s = 0.0883024619*r + 0.2817188376*g + 0.6299787005*b
    L = l ** (1/3); M = m ** (1/3); S = s ** (1/3)
    return (0.2104542553*L + 0.7936177850*M - 0.0040720468*S,
            1.9779984951*L - 2.4285922050*M + 0.4505937099*S,
            0.0259040371*L + 0.7827717662*M - 0.8086757660*S)


def _kmeans4_weighted(samples):
    """K=4 OKLab k-means on (rgb, weight) samples. Returns 4 OCS-snapped
    representative colours sorted ascending by lightness."""
    if not samples:
        return []
    labs = [(_oklab(rgb), w, rgb) for rgb, w in samples]
    init = sorted(samples, key=lambda x: -x[1])[:4]
    while len(init) < 4:
        init.append(init[-1] if init else ((0, 0, 0), 1))
    centroids = [_oklab(rgb) for rgb, _ in init]
    for _ in range(15):
        assigns = []
        for lab, _, _ in labs:
            assigns.append(min(range(4), key=lambda c: sum(
                (lab[i] - centroids[c][i]) ** 2 for i in range(3))))
        new_cent = []
        for c in range(4):
            members = [(lab, w) for (lab, w, _), a in zip(labs, assigns) if a == c]
            if not members:
                new_cent.append(centroids[c]); continue
            ws = sum(w for _, w in members)
            new_cent.append(tuple(sum(lab[i] * w for lab, w in members) / ws for i in range(3)))
        centroids = new_cent
    # Pick representative per cluster: highest-weight member
    reps = []
    for c in range(4):
        members = [(rgb, w) for (rgb, w), a in zip(samples, assigns) if a == c]
        members.sort(key=lambda x: -x[1])
        reps.append(members[0][0] if members else (0, 0, 0))
    # Sort ascending by lightness so palette[28..31] = darkest..lightest
    reps.sort(key=lambda c: _oklab(c)[0])
    return reps


def _dred_deck_water_clusters():
    """Memoised: decode PC bg, find pixels in cycled CLUT range, k-means to 4
    OCS colours. Returns (chosen_4_colours, pc_clut_idx_to_cluster_dict).
    chosen ordered ascending by OKLab L."""
    if hasattr(_dred_deck_water_clusters, '_cache'):
        return _dred_deck_water_clusters._cache
    indices = _decode_pc_bg_indices(f'{REPO_ROOT}/pc-data/MONKEY2.001', 24, 320, 144)
    # Read PC CLUT for room 24
    from decode_amiga_room import load, be32, find_chunk, walk_rooms
    d = load(f'{REPO_ROOT}/pc-data/MONKEY2.001')
    ro = next(o for r, o in walk_rooms(d) if r == 24)
    co, cs = find_chunk(d, ro + 8, ro + be32(d, ro + 4), 'CLUT')
    cb = d[co + 8 : co + cs]
    # Frequency by OCS-snapped colour
    from collections import Counter
    ocs_freq = Counter()
    pc_slot_to_ocs = {}
    for v in indices:
        if 160 <= v < 192:
            rgb = (cb[v * 3], cb[v * 3 + 1], cb[v * 3 + 2])
            ocs = _ocs_snap(rgb)
            ocs_freq[ocs] += 1
            pc_slot_to_ocs[v] = ocs
    samples = list(ocs_freq.items())
    chosen = _kmeans4_weighted(samples)
    # Build OCS-colour → cluster-index (0..3) map
    ocs_to_cluster = {}
    for ocs in ocs_freq:
        ci = min(range(4), key=lambda c: sum(
            (a - b) ** 2 for a, b in zip(_oklab(ocs), _oklab(chosen[c]))))
        ocs_to_cluster[ocs] = ci
    # Map PC CLUT slot → Amiga slot (28..31)
    pc_slot_to_ami_slot = {pc_slot: 28 + ocs_to_cluster[ocs]
                           for pc_slot, ocs in pc_slot_to_ocs.items()}
    _dred_deck_water_clusters._cache = (chosen, pc_slot_to_ami_slot, indices)
    return _dred_deck_water_clusters._cache


def _dred_deck_extra_locks(orig_clut):
    """Lock palette[28..31] to the 4 k-means cluster colours that best
    represent the 8 PC cycles' palette range."""
    chosen, _, _ = _dred_deck_water_clusters()
    return [['--lock-index', str(28 + i), f'{r:02X}{g:02X}{b:02X}']
            for i, (r, g, b) in enumerate(chosen)]


def _decode_pc_bg_indices(pc_data_path, room_id, w, h):
    """Decode the PC bg SMAP for `room_id` into a per-pixel CLUT-index array.
    Used by region_constraint to identify which bg pixels were rendered using
    PC's CYCL'd CLUT range (those pixels are the water/animated region)."""
    from decode_amiga_room import (load, be32, le32, find_chunk, walk_rooms,
                                    decode_zigzag_h, decode_majmin_h)
    d = load(pc_data_path)
    ro = next(o for r, o in walk_rooms(d) if r == room_id)
    rsz = be32(d, ro + 4)
    rmim_off, rmim_sz = find_chunk(d, ro + 8, ro + rsz, 'RMIM')
    im00_off, im00_sz = find_chunk(d, rmim_off + 8, rmim_off + rmim_sz, 'IM00')
    smap_off, smap_sz = find_chunk(d, im00_off + 8, im00_off + im00_sz, 'SMAP')
    body_off = smap_off + 8
    num_strips = w // 8
    strip_offsets = [le32(d, body_off + i * 4) for i in range(num_strips)]
    strip_ends = list(strip_offsets[1:]) + [smap_sz]
    img = bytearray(w * h)
    for si, (start, end) in enumerate(zip(strip_offsets, strip_ends)):
        codec = d[smap_off + start]
        strip_bytes = d[smap_off + start + 1 : smap_off + end]
        shr = codec % 10
        pix = None
        if 24 <= codec <= 28:
            pix = decode_zigzag_h(strip_bytes, h, shr)
        elif 64 <= codec <= 68:
            pix = decode_majmin_h(strip_bytes, h, shr)
        if pix is None:
            continue
        for y in range(h):
            for x in range(8):
                img[y * w + si * 8 + x] = pix[y * 8 + x]
    return img


def _dred_deck_region_constraint(w, h):
    """Per-pixel routing mask for dred-deck. Each PC bg pixel whose CLUT
    falls in the cycled range [160..191] is forced to a specific Amiga slot
    (28..31) determined by which k-means cluster its OCS-snapped colour
    belongs to. Non-cycled pixels are restricted to slots 0..27.

    Region IDs:
      0 = non-water  (allowed: slots 0..27)
      1 = cluster 0 (allowed: {28} only) — darkest cycle colour
      2 = cluster 1 (allowed: {29})
      3 = cluster 2 (allowed: {30})
      4 = cluster 3 (allowed: {31}) — lightest cycle colour
    """
    chosen, pc_slot_to_ami, indices = _dred_deck_water_clusters()
    if w != 320 or h != 144 or len(indices) != w * h:
        # Source mismatch — fall back to no constraint to avoid corruption
        return None
    mask = bytearray(w * h)
    for i, v in enumerate(indices):
        ami = pc_slot_to_ami.get(v)
        if ami is None:
            mask[i] = 0  # non-water
        else:
            mask[i] = 1 + (ami - 28)  # 1..4 = clusters 0..3
    slot_sets = {
        0: set(range(28)),
        1: {28},
        2: {29},
        3: {30},
        4: {31},
    }
    return mask, slot_sets


def _dred_deck_extra_patches(d_mut, room_off, room_size, palette):
    """Replace the empty CYCL chunk with a single forward cycle on
    CLUT[44..47] (= palette[28..31] under paletteMod=16). The four locked
    water-blues at those slots get rotated, animating any bg pixels routed
    there by the quantizer."""
    # Local imports to avoid forcing decode_amiga_room into the module's
    # always-on dependencies.
    from decode_amiga_room import find_chunk
    cycl_off, cycl_sz = find_chunk(d_mut, room_off + 8, room_off + room_size, 'CYCL')
    if cycl_off is None:
        return []
    # SCUMM v5 CYCL entry layout:
    #   idx (1) + unused (2) + delay BE (2) + flags BE (2) + start CLUT (1) + end CLUT (1)
    # Body terminates with idx=0.
    new_body = bytes([
        0x01,             # cycle slot 1
        0x15, 0xFA,       # unused (mimicking PC value)
        0x03, 0x4B,       # delay BE = 843
        0x00, 0x02,       # flags BE = 2 (forward direction)
        44,               # start CLUT slot (= palette[28])
        47,               # end CLUT slot   (= palette[31])
        0x00,             # terminator
    ])
    return [{
        'offset': cycl_off,
        'old_size': cycl_sz,
        'new_body': new_body,
        'parents': [room_off],
    }]


ROOM_OVERRIDES: dict[str, RoomOverride] = {

    'dred-deck': RoomOverride(
        # Cycle restoration disabled 2026-05-08: PC's 8-cycle animation across
        # 32 CLUT slots is too complex to approximate convincingly with a
        # single 4-slot Amiga cycle. The k-means + masked-routing prototype
        # below works mechanically (only water shimmers, sky/ship are still),
        # but doesn't read as PC-faithful. Keeping the helper functions in
        # this file as reference for any room where a SIMPLER PC cycle (1-2
        # cycles, fewer than 8 colours) might be a clean port.
        notes=(
            'Bottom-of-screen water animation NOT restored. PC has 8 distinct '
            'cycles across CLUT[160..191] (~15 OCS-distinct colours). 4-slot '
            'k-means approximation tested 2026-05-08 — works mechanically but '
            'doesn\'t look right. Helper functions (_dred_deck_extra_locks, '
            '_dred_deck_extra_patches, _dred_deck_region_constraint) remain '
            'as reference scaffolding for future single-cycle rooms.'
        ),
    ),

}


def get(slug: str) -> Optional[RoomOverride]:
    """Look up the override for a room, or None if the default pipeline applies."""
    return ROOM_OVERRIDES.get(slug)
