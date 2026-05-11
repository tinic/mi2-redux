"""SCUMM v5 ActorOps TalkColor literal patcher.

After --best produces the room's new 32-entry palette, the original
TalkColor literals (in scripts referencing CLUT[N] for N in [16..47])
no longer point at slots holding the artist's intended dialogue colour
— --best repurposed those slots for bg/OBIM/locked-actor needs.

Strategy (per user direction):
  1. For each TalkColor literal N in this room, get the pristine RGB
     at CLUT[N] (= the colour the artist meant for that actor's text).
  2. Find the palette index whose RGB is closest in OKLab to that
     pristine RGB.
  3. Compute new_slot = 16 + closest_idx (= the CLUT slot in our
     patched data that holds the close-match colour).
  4. Patch the TalkColor opcode literal byte: N -> new_slot, in every
     script chunk in this room.

ScummVM then reads CLUT[new_slot] = palette[closest_idx] ≈ pristine RGB
when rendering the actor's text. Real Amiga truncates the same way and
sees the same colour.

Notes:
  - We only patch literal-mode TalkColor (subop 0x0C without high bit).
    Var-mode (0x8C) reads the colour from a SCUMM Variable; we can't
    statically resolve those.
  - We only patch slots in [16..47] (the range --best owns). Slots in
    [0..15], [48..255] keep their pristine CLUT[N] so the existing
    literal still works. Slot 33 stays untouched (ScummVM's
    hardcoded-white quirk).
"""

from typing import Iterator


# ActorOps subop arg sizes for SCUMM v5 (literal mode = high bit clear).
# Var-mode args are 2 bytes instead of 1 (per A1V / A2V), word args (A1W)
# are always 2 bytes regardless of the high bit.
# Tuple entries: (subop_id, n_args_size_in_bytes_when_high_bit_clear,
#                 arg_kind: 'B' = byte literal, 'W' = word, 'STR' = null-term string)
# Multi-arg subops listed once with first arg's kind; second/third arg
# sizes captured below.
SUBOP_TABLE = {
    0x00: 'B',   # Unknown
    0x01: 'B',   # Costume
    0x02: 'BB',  # WalkSpeed (A1B + A2B; flags swap to V)
    0x03: 'B',   # Sound
    0x04: 'B',   # WalkAnimNr
    0x05: 'BB',  # TalkAnimNr (A1B + A2B)
    0x06: 'B',   # StandAnimNr
    0x07: 'BBB', # Nothing (A1B + A2B + A3B)
    0x08: '',    # Init
    0x09: 'W',   # Elevation (A1W)
    0x0A: '',    # DefaultAnims
    0x0B: 'BB',  # Palette
    0x0C: 'B',   # TalkColor
    0x0D: 'STR', # Name (null-terminated)
    0x0E: 'B',   # InitAnimNr
    0x10: 'B',   # Width
    0x11: 'BB',  # Scale (v5)
    0x12: '',    # NeverZClip
    0x13: 'B',   # SetZClip
    0x14: '',    # IgnoreBoxes
    0x15: '',    # FollowBoxes
    0x16: 'B',   # AnimSpeed
    0x17: 'B',   # ShadowMode
}


def _arg_size(arg_kind: str, opcode: int, arg_index: int) -> int:
    """Byte size of the arg at position arg_index of a subop with `opcode`.

    Position 0 controls bit 0x80, 1 controls bit 0x40, 2 controls bit 0x20.
    Word args ignore the bit (always 2 bytes)."""
    if arg_kind == 'W':
        return 2
    bit = 0x80 >> arg_index
    return 2 if (opcode & bit) else 1


def parse_actorops_at(body: bytes, start: int) -> tuple[int, list[tuple[int, int, int]]]:
    """Parse ActorOps starting at `start` (= offset of the ActorOps opcode
    byte). Returns (end_offset_exclusive, [(subop_offset, talkcolor_lit_offset, talkcolor_value)])
    on success, or (-1, []) if the parse failed (= probably not an
    ActorOps boundary).
    """
    if start >= len(body):
        return -1, []
    opcode = body[start]
    if (opcode & 0x1F) != 0x13:
        return -1, []
    p = start + 1
    # Actor arg
    actor_size = 2 if (opcode & 0x80) else 1
    p += actor_size
    if p >= len(body):
        return -1, []

    talkcolors: list[tuple[int, int, int]] = []
    while p < len(body):
        sub = body[p]
        if sub == 0xFF:
            return p + 1, talkcolors
        sub_id = sub & 0x1F
        kind = SUBOP_TABLE.get(sub_id)
        if kind is None:
            return -1, []
        sub_off = p
        p += 1   # subop byte itself
        if kind == 'STR':
            end = body.find(0, p)
            if end == -1:
                return -1, []
            p = end + 1
        else:
            for i, k in enumerate(kind):
                sz = _arg_size(k, sub, i)
                if sub_id == 0x0C and k == 'B' and not (sub & 0x80):
                    # TalkColor literal — record the byte offset
                    talkcolors.append((sub_off, p, body[p]))
                p += sz
                if p > len(body):
                    return -1, []
    return -1, []


import os
import re
import subprocess
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


DESCUMM = f'{REPO_ROOT}/tools/scummvm-tools/descumm'

# Offsets descumm reports are 0-based into the SCRIPT BODY, AFTER any
# format-specific prefix (e.g. LSCR's 1-byte script# is stripped).
_OFFSET_RE = re.compile(r'^\[([0-9A-Fa-f]+)\]\s+\(13\)\s+ActorOps\(', re.MULTILINE)


def _run_descumm(chunk_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
        f.write(chunk_bytes)
        path = f.name
    try:
        r = subprocess.run([DESCUMM, '-5', path],
                           capture_output=True, text=True, timeout=15)
    finally:
        os.unlink(path)
    return r.stdout if r.returncode == 0 else ''


def find_talkcolor_literals(script_body: bytes,
                              chunk_bytes: bytes) -> Iterator[tuple[int, int]]:
    """Yield (literal_byte_offset_in_body, value) pairs for every
    literal-mode TalkColor inside an ActorOps in `script_body`.

    Method: run descumm on the chunk to confirm where ActorOps lives,
    then call parse_actorops_at at those exact offsets — eliminates
    the false positives that a raw byte scan produces.
    """
    disasm = _run_descumm(chunk_bytes)
    if not disasm:
        return
    for m in _OFFSET_RE.finditer(disasm):
        ops_offset_in_body = int(m.group(1), 16)
        end, tcs = parse_actorops_at(script_body, ops_offset_in_body)
        if end < 0:
            continue
        for sub_off, lit_off, val in tcs:
            yield (lit_off, val)


def patch_talkcolor(d_mut: bytearray, chunk_offset: int, body_offset_in_chunk: int,
                     literal_offset_in_body: int, new_value: int) -> None:
    """In-place patch the literal byte at the given offset within a
    script chunk's body.

    `chunk_offset` is the file offset of the chunk header (e.g. 'LSCR').
    `body_offset_in_chunk` is where the script body starts after the
    8-byte tag/size header (= 8 for SCRP/EXCD/ENCD, 9 for LSCR which
    has a 1-byte script# prefix, etc.).
    """
    abs_off = chunk_offset + body_offset_in_chunk + literal_offset_in_body
    d_mut[abs_off] = new_value
