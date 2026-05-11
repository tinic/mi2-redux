# mi2-redux

Re-encode *Monkey Island 2* (1992 Amiga port, LucasArts) using PC VGA assets
re-quantized via `png2amiga` → 32-colour OCS art that visibly outperforms the
original LucasArts Amiga release. Delivery target: **ScummVM**.

## Why this project exists

**This project exists primarily to validate `png2amiga`'s capabilities.**
mi2-redux is the integration test bed. Improvements that belong in png2amiga
should be specced and handed off to that project (typically via the
`png2amiga-architect` agent), **not** worked around inline in this wrapper.

If you find yourself building post-process steps to compensate for something
the encoder can't do natively, stop and write a spec under
`~/png2amiga/PROJECT_*.md` first. Recent example: a 4-step palette-locking
hack got replaced by `--lock-index` + `--print-palette` after the feature was
added upstream — see `~/.claude/projects/-Users-turo-png2amiga/memory/project_strategy_a_v3_native_lock.md`.

## Hard constraints (do not negotiate)

- **OCS only.** 12-bit DAC, 32 colours per scene. Targets the original A500/
  A600 audience. Never propose AGA — that defeats the entire premise.
- **ScummVM is the delivery target.** FS-UAE / WHDLoad / native HDF were
  attempted and descoped (bootblock + ADFlib write bugs).
- **Python tooling is sufficient.** Do not port to C++ unless something is
  measurably too slow — the bottleneck is png2amiga, not the wrapper.
- **No GitHub uploads** until there's a working artifact across all 110 rooms.
- **One-by-one room validation** (in sequence) is preferred over bulk
  processing. The user wants to eyeball each scene.

## Pipeline (Strategy A v3 — native lock)

```
PC VGA frame ─┐
COST atlas   ─┼─► joint canvas ─► png2amiga --best --lock-index ... \
costume      ─┘                              --print-palette-json --quiet
swatch                                           │
                                                 ├─ stdout: single-line JSON palette
                                                 └─ output PNG: indexed pixels
                                                                │
                                       json.loads(r.stdout) → palette[idx] = rgb
                                                                ▼
                                                  rgb→slot map → indexed pixels
                                                                │
                                                                ▼
                                  encode_strip_best (multi-codec SMAP) ─► SMAP body
                                                                │
                                                                ▼
                                  multi_chunk_patch (LECF / DROO / DSCR / DCOS / DSOU)
                                                                │
                                                                ▼
                                                       monkey2.000..011 patched
                                                                │
                                                                ▼
                                            ScummVM (custom build w/ MD5 fallback)
```

**Palette round-trip:** `--print-palette-json` output can be fed back to
png2amiga via `--palette <file>` (locked + reserved metadata preserved). Useful
for forcing later runs onto a captured palette without re-passing every
`--lock-index` flag — relevant when re-encoding OBIM frames after the bg
palette is fixed, or for batch operations that need a stable palette.

**Native indexed output (added upstream 2026-05-08):**
- `--output-indexed <file>` — png2amiga writes raw chunky indices (1 byte/
  pixel, scan order, no header) post-pin/post-sort. Eliminates the
  "open RGB PNG → build rgb→slot map → walk pixels → fall back to OKLab
  nearest" loop in `inject_room.py`. `lock_palette_slots.oklab_dist` and
  `obim_reencode.closest_palette_index` become dead code once the wrapper
  reads the indexed file directly.
- `--transparent-color <RRGGBB>` (repeatable) — pixels matching the
  given sRGB triple are treated as alpha=0 before quantization, hooked
  into the existing `--alpha-threshold` / `--alpha-dither` plumbing.
  Replaces the BFS magenta-inpaint pre-pass and the hand-coded
  `MAGENTA = {(0xFF,0,0xFF), (0xFC,0,0xFC), (0xFF,0x57,0xFF),
  (0xFC,0x54,0xFC), (0xFF,0x55,0xFF)}` set in `inject_room.py`. Pass
  the flag once per sentinel variant.

Combined: a single
`png2amiga --best ... --transparent-color FF00FF --transparent-color FC00FC ...
  --output-indexed bg.idx --print-palette-json bg.png -o bg.preview.png`
gets you (palette JSON on stdout, indexed bytes in `bg.idx`, transparency
honoured) without any Python post-process.

The orchestrator is `tools/inject_room.py`. The encoder is `tools/encode_amiga.py`
(MAJMIN_H5 disabled by default — see "Known issues" below).

## Locked CLUT slots per room

Computed before invoking png2amiga; passed in via `--lock-index <pal_idx> <RRGGBB>`.
Note `paletteMod=16` for Amiga MI2: SMAP pixel value `v` renders via `CLUT[v+16]`,
so `pal_idx = CLUT_idx − 16`.

| Pal idx | CLUT idx | Why locked                                          |
|--------:|---------:|-----------------------------------------------------|
|       0 |       16 | Pure black, SCUMM script reference                  |
|       1 |       17 | HW sprite cursor (sprites 0/1)                      |
|       2 |       18 | HW sprite cursor                                    |
|       3 |       19 | HW sprite cursor                                    |
|      17 |       33 | Pure white, SCUMM script reference                  |
|       * |   16..47 | Every CLUT slot any costume in this LFLF references |

The costume slots are read from each `COST` chunk's palette table at
`body[2..2+pal_size]` after `body[0]=numAnim`, `body[1]=format & 0x7F`
(0x58 → 16-colour, 0x59 → 32-colour).

## OCS hardware sprite slots (informational)

| Sprite | Uses CLUT slots |
|--------|-----------------|
| 0 / 1  | 17, 18, 19      |
| 2 / 3  | 21, 22, 23      |
| 4 / 5  | 25, 26, 27      |
| 6 / 7  | 29, 30, 31      |

## ScummVM dev build

`tools/scummvm/` contains a custom-built ScummVM with an MD5 fallback patch
in `engines/scumm/detection_internal.h::findInMD5Table` — any unknown MD5
falls back to "Monkey Island 2 Amiga English". Required because patched data
files have different MD5s. **For distribution**, ship users the data files
and point them at standard ScummVM (whose detection table needs upstreaming).

## Status as of 2026-05-08

| Room # | Slug         | Status         |
|-------:|--------------|----------------|
|      1 | part1        | ✅ validated    |
|      2 | scabb-isl    | ✅ validated    |
|      3 | sky          | ✅ (no MAJMIN)  |
|      4 | shore        | ✅ validated    |
|      5 | campfire     | ✅ (v3 refactor)|
|      6 | weenie       | ✅ validated    |
|      7 | woodtick     | ⚠ paused (workaround edge cases pre-v3 refactor — re-test with v3) |
|   8..110 | …           | pending        |

Approach: re-test room 7 (woodtick) with the v3 native-lock pipeline first,
then run rooms 8 onward in sequence.

## Per-room overrides

`tools/room_specials.py` is the registry for any room that needs surgery
beyond the default pipeline. Three hook points (all optional):

- `extra_locks(orig_clut)` — append extra `--lock-index` / `--reserve-range`
  args before the `--best` quantizer runs.
- `skip_obim(obj_id)` — return True to leave a specific OBIM's original
  Amiga SMAP intact (skip our re-encoding).
- `post_patch(d_mut, room_off, room_size, palette, new_clut_full)` —
  mutate the patched data buffer at the very end (after all chunk patches
  applied) for surgical fixes like backporting a missing animation script.

Empty entries (notes-only) are fine — they document a known issue without
fixing it yet. Scale up by adding hook callbacks as concrete needs surface.

Current entries: **dred-deck** (water animation TODO — see notes in the
file).

## Known issues / open work

- **MAJMIN_H5 ScummVM-incompat.** Our encoder produces MAJMIN_H5 streams
  that round-trip through OUR decoder but render with vertical-band
  corruption in ScummVM. `encode_strip_best(allow_majmin=False)` is the
  default workaround. Fix unlocks more compression.
- **Special rooms** (intros, cutscenes, palette-fade scenes) — handle via
  `room_specials.py` overrides as they come up.
- **dred-deck water animation** — bottom-of-screen water has 3 OBIM
  frames (object 246 IM01/IM02/IM03) but the Amiga port omits the
  animation trigger script, so the wave is static even on pristine
  data. Restore by backporting the PC version's `animateObject(246)` /
  state-rotation script via `post_patch`.

## Memory

Persistent context for this project lives at
`~/.claude/projects/-Users-turo-png2amiga/memory/`. Key entries:

- `project_strategy_a_v3_native_lock.md` — current pipeline (read this first)
- `project_strategy_a_v2_complete.md` — the previous post-process workaround
- `reference_png2amiga_reserve_spec.md` — pointers to upstream spec docs
- `project_*.md` — per-room incidents and decisions

When picking up the work, **read `MEMORY.md` and the v3 strategy entry
before touching code.**

## Don'ts

- Don't propose AGA. Ever.
- Don't add post-process palette workarounds to inject_room.py — spec the
  feature for png2amiga and hand off via the `png2amiga-architect` agent.
- Don't skip room-by-room validation in favour of "let's just batch all 110".
  The user explicitly wants to eyeball each scene.
- Don't push to GitHub until there's an artifact spanning all 110 rooms.
- Don't fight ScummVM MD5 detection by binary-patching MD5 strings — patch
  the source (`detection_internal.h`) and rebuild.
