# mi2-redux

A high-fidelity re-encode of the 1992 *Monkey Island 2: LeChuck's Revenge*
Amiga port. PC VGA source assets are re-quantized into 32-colour OCS art
through [png2amiga](https://github.com/tinic/png2amiga), preserving
1992-Amiga colour ranges and engine quirks so the patched data drops into
ScummVM (and an Amiga HD install) without code changes.

The PC release shipped with 256-colour VGA backgrounds and 32-colour
costumes; LucasArts' 1992 Amiga port took the same source data and snapped
it down to OCS's 12-bit palette in a single pass per room. Thirty-three
years of better quantizers later, you can do meaningfully better — provided
you're willing to descend into SCUMM v5's long tail.

```
                  ┌─────────────────────────┬─────────────────────────┐
                  │ pristine Amiga 1992     │ mi2-redux               │
   bg S2 (avg)    │ ~30 dB                  │ ~46 dB                  │
   atlas S2       │ —                       │ ~55 dB                  │
   cost S2        │ —                       │ ~60 dB                  │
                  └─────────────────────────┴─────────────────────────┘
```

(S2 = ssimulacra2 against the PC source, dB-scale, higher is better.)

## Quick start

Two scripts: bootstrap once on a fresh machine, build whenever you change
something.

```bash
git clone https://github.com/<you>/mi2-redux.git
cd mi2-redux
./bootstrap.sh           # system pkgs + clone+build png2amiga (one-time)
./build.sh               # 98-room patch + dist/ packaging (~25 min on M3)
```

Supported platforms: macOS via Homebrew, Debian 13 (trixie) / Ubuntu via
`apt-get`. `bootstrap.sh` installs gcc-15 (png2amiga uses C++26 draft),
cmake, autoconf/automake, python3 + venv, then clones
`https://github.com/tinic/png2amiga` into `~/png2amiga` and builds it.

`build.sh` env knobs:

| Var | Meaning |
|-----|---------|
| `BEST=0` | skip png2amiga's `--best` population search (~30× faster, lower bg quality — for iteration) |
| `DITHER=<method>` | pass `--dither <method>` to every png2amiga site (try `DITHER=opt-checker`); default = png2amiga's built-in (Floyd-Steinberg) |
| `VERBOSE=1` | replay every png2amiga subprocess command to stdout after each room |
| `SKIP_PATCH=1` | reuse existing `monkey2-hd/`, only re-run packaging |

The patched data ships in `dist/MonkeyHD/` (directory tree),
`dist/monkey2-hd.hdf` (RDB hard-drive image), and
`dist/monkey2-hd.lha`.

## Quality

For each room the build prints three S2 numbers:

```
[42/98] kiosk        ok  S2: bg=51.83, atlas=63.21, cost=67.04
```

- **bg** — png2amiga `--best` + Floyd-Steinberg dither + all 32 slots
  locked to the joint-quantized palette, against the PC source bg PNG.
  Matches the actual encoded bg's quality.
- **atlas** — same metric over the OBIM (object-image) atlas.
- **cost** — same metric over the pristine Amiga COST chunks rendered
  through their original CLUT, then re-encoded against the new palette.

Cost re-encode falls back to `--dither none` *per costume* only when
dithered RLE overflows SCUMM's 16-bit baseptr addressing (~64 KB per COST
body); otherwise dithering is on, which matches the dither-aware palette
the joint pass picked.

## Why this is harder than it sounds

The naive plan — "load PC PNG, pass to png2amiga, write back" — works for
a single room in isolation. The interesting parts came from these
constraints:

### 1. Palette mod = 16 (CLUT[16..47] is the active 32-colour playfield)
Pristine MI2 Amiga renders bg pixels using a **paletteMod of 16**, so SMAP
indices `0..31` route into CLUT slots `16..47`. The lower half holds verbs,
mouse cursor, and text; the upper half (CLUT[48..255]) holds costume
colours referenced via `pal_table` indirection. Every quantization decision
has to be expressed in this layout or ScummVM renders garbage.

### 2. The ScummVM SMAP-17→black bug
ScummVM's MI2 Amiga path hardcodes `_roomPalette[33] = 0` (palette.cpp
~line 457), which means SMAP value 17 always renders as palette[0] (black)
in the bg, even though the verb/UI path uses CLUT[33] = white. We
`--reserve-range 17 FFFFFF` so png2amiga keeps slot 17 white but never
routes a bg pixel to it.

### 3. OCS hardware sprite slots
The cursor is a single-colour OCS sprite, not a 4-colour one — so only
CLUT[17] (= palette index 1) is the actual sprite colour. CLUT[18..19]
are sprite colours 2/3 of a 4-colour sprite that MI2 doesn't use, and
locking them was over-restrictive. Freeing those two slots delivered
+5 dB bg / +8 dB cost on jail with no visible cursor regression.

### 4. Costumes are RLE in a different scan order on PC vs Amiga
SCUMM v5 ClassicCostume RLE is column-major on PC and row-major on Amiga,
but the chunk *header* (numAnim, format byte, pal_table, frame_offsets) is
identical. Decoder takes a `column_major=True` flag for PC bodies. The
encoder always writes Amiga row-major regardless of source.

### 5. Costume "garbage" frames (decoder reachability)
The naive SCUMM v5 decoder enumerates `0x7B` codes per limb and accepts
any decodable frame. That over-includes uninitialised entries —
specifically frames that fall in the metadata region (the engine never
draws them because `usemask` filters them at runtime). On 7 of MI2's
costumes this produced phantom frames as wide as 305 pixels that blew up
the joint atlas. Fixed by walking the anim graph — `numAnim → dataOffsets
→ mask/(j,extra) → animCmds → frame codes` — and only emitting reachable
frames. See `tools/decode_cost.py:_collect_reachable_frame_offsets`.

### 6. 16-bit baseptr addressing in COST chunks
SCUMM v5 COST bodies use 16-bit baseptr-relative offsets. Dithered RLE
expands ~5–10× vs flat-shaded for sprite-heavy costumes (every pixel
breaks a run). On rooms like undergrou (474 frames), dithered RLE pushes
past 64 KB and we get an addressing overflow. Fix: render BOTH dithered
and non-dithered indexed bytes per costume, prefer dithered, fall back
per-costume if `rebuild_cost_body` raises `COST body overflow`.

### 7. No Python OKLab NN-remap
png2amiga is the source of truth for nearest-colour mapping. Doing it
again in Python (with subtly different rounding/gamma) introduces visible
drift on bulk pixel data. Specifically: `obim_reencode.build_obim_replacements`
used to take an RGB atlas + remap in Python; it now takes the indexed
bytes from a `png2amiga --dither floyd-steinberg` second pass with all 32
slots locked. Single-color script-literal lookups (TalkColor, pal_table
remap) are exempt.

### 8. Tree-rebuild instead of in-place patching
Early versions of inject_room.py shifted offsets in place — DROO, DCHR,
DSCR, DSOU all needed careful tracking when SMAP/COST/CLUT chunks grew.
This was fragile and produced subtle "expected SCRP" errors on
sprite-heavy rooms. Replaced with a parse → mutate → serialize pipeline
(`tools/scumm_tree.py` + `tools/scumm_index.py`). Disk LOFF tables are
auto-rebuilt during serialize so cross-disk LFLF replicas (Guybrush's
home rid 111 lives on multiple disks) track correctly.

### 9. Cross-room costumes
Most characters appear in multiple rooms. The 1992 Amiga port handled
this by sharing CLUT[192..207] across rooms — costume `pal_table` values
get 5-bit-truncated by the OCS DAC into slots 16..31, which under
paletteMod=16 lands in 32..47. We mirror this with a **global Guybrush
sub-palette** (palette[22..31] reserved across all rooms he appears in)
plus 4 "extras groups" (`tools/cost_groups.json`) for non-Guybrush
multi-room costumes. Encoded once globally
(`encode_global_guybrush.py` + `encode_global_extras.py`), then per-room
inject just locks those slots.

### 10. TalkColor literals in scripts
`ActorOps subop 0x0C` carries a 1-byte CLUT slot for actor dialogue
text. After the bg gets re-quantized, the slot the script references
might no longer exist in our new CLUT layout. We descumm every script
chunk in every room (`tools/talk_colors_survey.py`) and patch the
1-byte literal in place via OKLab NN against the new palette. (This
single-color lookup IS done in Python — exempt from the no-NN-remap
rule because it's per-color, not per-pixel.)

### 11. Magenta sentinels
Both PC bg PNGs and OBIM PNGs use magenta as the transparency
sentinel — `0xAB00AB`, `0xFC00FC`, `0xFF00FF`, plus anti-aliased
edge variants (`0xFF57FF` etc.). The build scans each input PNG's
P-mode palette for magenta-family entries (R high, B high, G low)
and feeds every variant to png2amiga as `--transparent-color`.

## Architecture

```
.
├── amiga-data/                Pristine Amiga MI2 input (monkey2.000..011)
├── pc-data/                   PC MI2 source (MONKEY2.000, MONKEY2.001)
├── extracted-pc-pngs/         PC bg/object PNGs (via MISE Explorer)
├── disks/                     Original Amiga ADFs
├── monkey2-hd/                Patched output (ScummVM points here)
├── dist/                      Packaged HD install (HDF + LHA + dir-tree)
├── bootstrap.sh               System deps + png2amiga clone+build
├── build.sh                   Full build pipeline
└── tools/
    ├── inject_room.py             Per-room patcher (the workhorse)
    ├── encode_global_guybrush.py  Re-encode Guybrush once at palette[22..31]
    ├── encode_global_extras.py    Same for cost_groups.json multi-room costumes
    ├── build_actor_palette.py     Initial global_actor_palette.json
    ├── build_family_palette.py    Cross-room shared palettes
    ├── build_pristine_cache.py    Pickle of all pristine COST/CLUT/etc.
    ├── scumm_tree.py              SCUMM v5 parse / mutate / serialize
    ├── scumm_index.py             monkey2.000 (DROO/DCOS/...) rebuilder
    ├── decode_cost.py             SCUMM v5 ClassicCostume RLE decoder
    ├── decode_obim.py             OBIM SMAP decoder (zigzag/majmin codecs)
    ├── decode_amiga_room.py       Top-level chunk walker
    ├── encode_cost.py             COST RLE encoder + body rebuilder
    ├── encode_amiga.py            SMAP encoder (zigzag + majmin)
    ├── obim_reencode.py           Per-OBIM-frame SMAP rebuilder
    ├── pc_data.py                 PC LOFF/DCOS/CLUT helpers
    ├── room_specials.py           Per-room override registry
    ├── patch_talkcolors.py        ActorOps TalkColor literal patcher
    ├── compare.py                 3-panel side-by-side preview generator
    ├── finalsheet.py              Per-room visual sanity sheet
    ├── shared_palettes/           Family palette JSONs (jungle, rapcoffin)
    ├── cost_groups.json           Hand-edited multi-room costume groups
    ├── costume_refs.json          descumm-derived costume → rooms graph
    └── talk_colors_survey.json    descumm-derived TalkColor literals per room
```

### Build pipeline

1. **Stage 1** — toolchain bootstrap (lha-jca, python venv with amitools +
   Pillow, scummvm-tools/descumm, PyTexturePacker).
2. **Stage 1b** — pristine cache: pickle every COST body / CLUT / pal_table
   from `amiga-data/` so subsequent stages don't chain-corrupt state.
3. **Stage 1b'** — regenerate `tools/global_actor_palette.json` (mutated by
   the global encoders later, so we always start fresh).
4. **Stage 1c** — `encode_global_guybrush.py`: render Guybrush's 100 frames
   (PC source) at palette[22..31], patch his COST chunk on disk 1.
5. **Stage 1d** — `encode_global_extras.py`: same flow for the 5 cost
   groups defined in `cost_groups.json`.
6. **Stage 2** — for each of the 98 real rooms, run `inject_room.py` which:
   - assembles a joint canvas (bg + per-OBIM-frame + per-cost-frame +
     talk-colour swatches), all packed in **one** PyTexturePacker pass
   - runs png2amiga `--best` with palette locks for sprite/cursor slots,
     Guybrush's range, and any extras-group ranges that apply
   - re-quantizes the OBIM atlas + cost atlas with the final locked
     palette + dither (matching the dither-aware palette `--best` picked)
   - extracts indexed bytes per region and rebuilds SMAP / COST chunks
     via the tree-rebuild pipeline
7. **Stage 2b** — cross-room family palettes (jungle, rapcoffin) for rooms
   that share OBIM rendering with another room.
8. **Stage 3** — package: HDF (RDB-formatted Amiga hard-drive image),
   LHA archive, FS-UAE config + launcher.

## Status

- 98 real bg rooms patched
- 2 cross-room palette families validated in ScummVM (jungle: 7 rooms,
  rapcoffin: 3 rooms)
- 6 asset/system rooms intentionally pristine (`icons`, `whoopmap`,
  `open-cred`, `f-rapinfl`, `f-rap2inf`, `copycrap`)
- File size: ~+8% / +570 KB vs pristine (with MAJMIN_H5 enabled)
- Validated end-to-end in ScummVM via `--boot-param=N` per room

## Prior art

- **[jmonkey](https://github.com/...)** — does the same idea for
  *Monkey Island 1* (SCUMM v4). Doesn't extend to MI2: SCUMM v5 has a
  more complex script structure, OBIM transparency codecs that didn't
  exist in v4, MAJMIN compression, costume RLE differences, and the
  cross-room palette sharing problem (CLUT[192..207] / paletteMod=16)
  that's specific to v5+.
- **Monkey Island Special Edition** (LucasArts, 2009) — official, but it
  hand-redrew the art; not a re-quantization of the original PC data.

As far as I can tell, no public project has done this for MI2. If you
know of one, file an issue.

## License & credits

- Source code in this repo: MIT (see `LICENSE` if present, else assume
  same).
- *Monkey Island 2* assets in `amiga-data/`, `pc-data/`,
  `extracted-pc-pngs/`, `disks/`, `monkey2-hd/`: copyright LucasArts /
  Disney. **Don't distribute the patched output**; ship the diffs as
  data and let users apply them locally.
- png2amiga (sister project): https://github.com/tinic/png2amiga
- ScummVM: https://www.scummvm.org/
- scummvm-tools (descumm): https://github.com/scummvm/scummvm-tools
- PyTexturePacker: https://github.com/wo1fsea/PyTexturePacker

See `CLAUDE.md` for the full pipeline detail and gotchas.
