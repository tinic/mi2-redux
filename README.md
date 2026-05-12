# mi2-redux

High-fidelity re-encode of the 1992 *Monkey Island 2: LeChuck's Revenge*
Amiga port. PC VGA source art is re-quantised into 32-colour OCS
through [png2amiga](https://github.com/tinic/png2amiga), preserving the
1992-Amiga colour ranges and SCUMM v5 engine quirks so the patched data
drops into ScummVM (and a real Amiga HD install) without code changes.

The PC release shipped with 256-colour VGA backgrounds and 32-colour
costumes; LucasArts' 1992 Amiga port snapped that same source data
down to OCS's 12-bit palette in a single pass per room. Thirty-three
years of better quantizers later you can do meaningfully better — once
you've worked through SCUMM v5's long tail.

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

```bash
git clone --recurse-submodules https://github.com/tinic/mi2-redux.git
cd mi2-redux
./bootstrap.sh           # system pkgs + build every submodule (one-time)
# drop your legitimate MI2 game files into amiga-data/, pc-data/, disks/
# (see each dir's README.md for what goes where)
./build.sh               # ~25 min on M3; produces dist/MonkeyHD/ + .hdf + .lha
```

Supported platforms: macOS via Homebrew, Debian 13 (trixie) / Ubuntu via
`apt-get`.

**Prerequisite: GCC 15.** png2amiga is written against the C++26 draft
(`-std=c++2c`) and won't compile with anything older. `bootstrap.sh`
installs it for you on macOS (`brew install gcc`, which currently
ships `g++-15` at `/opt/homebrew/bin/g++-15`) and Debian 13 (`apt-get
install gcc-15 g++-15`). On older distros you'll need a backports/
snapshot repo or to build GCC 15 from source — Clang (any version,
including Apple's `/usr/bin/clang++`) does NOT work.

`bootstrap.sh` also installs SDL2 (for ScummVM), cmake,
autoconf/automake, Python 3 with venv, then initialises and builds
every submodule:

| submodule | what for |
|---|---|
| `tools/png2amiga` | the OCS quantiser |
| `tools/scummvm` | engine, with a MD5-fallback patch applied at build time |
| `tools/scummvm-tools` | descumm (script disassembly used by `costume_room_refs.py`) |
| `tools/PyTexturePacker` | bin-packs OBIM + cost frames into joint atlases |
| `tools/lha-jca` | jca02266 fork; writes LHA archives (Homebrew's `lhasa` is read-only) |

`build.sh` env knobs:

| Var | Meaning |
|-----|---------|
| `BEST=0` | skip png2amiga's `--best` population search (~30× faster, lower bg quality — for iteration) |
| `EHB=1` | also generate an EHB-mode comparison row in `preview/quality/` (preview-only — engine can't read 6bp SMAP) |
| `DITHER=<method>` | override the dither setting (default = `opt-checker`) |
| `SKIP_PATCH=1` | reuse existing `monkey2-hd/`, only re-run packaging |

The patched data ships in `dist/MonkeyHD/` (directory tree),
`dist/monkey2-hd.hdf` (RDB hard-drive image), and `dist/monkey2-hd.lha`.

## Quality

For each room the build prints three S2 numbers:

```
[42/98] kiosk        ok  S2: bg=51.83, atlas=63.21, cost=67.04
```

- **bg** — joint-quantised palette + Floyd-Steinberg/opt-checker dither,
  scored against the PC source bg PNG.
- **atlas** — same metric over the per-room OBIM atlas.
- **cost** — same metric over the pristine Amiga COST chunks rendered
  through their original CLUT, then re-encoded against the new palette.

The cost re-encode iterates `--dither-strength` from `1.0` down to `0`
in 0.2 steps per costume and picks the highest strength whose RLE
fits SCUMM's 16-bit COST baseptr cap (~64 KB).

## Why this is harder than it looks

The naive plan — "load PC PNG, pass to png2amiga, write back" — works
for one room in isolation. The interesting constraints:

### 1. paletteMod = 16 (CLUT[16..47] is the playfield)
Pristine MI2 Amiga renders bg pixels with **paletteMod = 16**, so SMAP
indices `0..31` route into CLUT slots `16..47`. The lower half is
verbs/cursor/text; the upper half holds costume colours via `pal_table`
indirection. Every quantisation decision has to be expressed in this
layout or ScummVM renders garbage.

### 2. ScummVM SMAP-17→black bug
ScummVM's MI2 Amiga path hardcodes `_roomPalette[33] = 0` (palette.cpp
~line 457), which means SMAP value 17 always renders as palette[0]
(black) in the bg, even though the verb/UI path uses CLUT[33] = white.
We `--reserve-range 17 FFFFFF` so png2amiga keeps slot 17 white but
never routes a bg pixel through it.

### 3. OCS hardware sprite slots
The mouse cursor is a single-colour OCS sprite — only CLUT[17]
(= palette index 1) is its colour. CLUT[18..19] are sprite cells 2/3
of a 4-colour sprite that MI2 doesn't use; locking them was
over-restrictive. Freeing those two slots delivered +5 dB bg / +8 dB
cost on jail with no visible cursor regression.

### 4. Cross-room costumes
Most NPCs appear in multiple rooms. Pristine MI2 solves it by sharing
`CLUT[192..207]` across every room — 26% of all non-Guybrush costumes
reference slots in that range for skin/hair/outline. We mirror the
pattern with three nested layers:

- **Guybrush globally**: re-encoded once at `palette[23..31]` (`CLUT[39..47]`)
  via `encode_global_guybrush.py`. Persists across every room he visits.
- **Cost groups**: five hand-edited groups in `tools/cost_groups.json`
  (`extras_a` …`extras_e`) for multi-room non-Guybrush NPCs. Each group
  reserves its own slot range and gets re-encoded once globally.
- **Variant cids**: characters appearing in 1–3 rooms get a **new cid
  per drawn room** (free DCOS slots: `[100, 174..198]`). Each variant
  lives in its target room's LFLF, gets re-encoded against that room's
  joint `--best` palette, and target-room scripts are byte-patched to
  reference the variant cid. Auto-generated by
  `tools/generate_variants.py`; applied by `tools/apply_variants.py`.

Costume pal_tables freely mix group slots + Guybrush slots, so a typical
extras_e cid effectively gets ~18 colours (9 group + 9 Guybrush) — the
same architectural trick the 1992 artists used by hand, automated.

## Architecture

```
.
├── amiga-data/         pristine Amiga MI2 input (user-supplied)
├── pc-data/            PC MI2 source (user-supplied)
├── disks/              original Amiga ADFs (user-supplied)
├── boot-infra/         Workbench HD-install scaffolding (icons, startup,
│                       devs/, c/ utilities) used by build.sh Stage 3
├── monkey2-hd/         patched output (ScummVM points here)
├── dist/               packaged HD install (HDF + LHA + dir-tree)
├── preview/quality/    side-by-side comparison images (4-row stacks)
├── bootstrap.sh        one-time: system deps + submodule init + builds
├── build.sh            full build pipeline
└── tools/
    ├── png2amiga/, scummvm/, scummvm-tools/, …    submodules
    ├── inject_room.py             per-room patcher (the workhorse)
    ├── apply_variants.py          inserts variant cids + script byte-patches
    ├── generate_variants.py       picks variant candidates by colour count
    ├── solve_slot_allocation.py   ILP-style cost_groups slot allocator
    ├── encode_global_guybrush.py  re-encode Guybrush at palette[23..31]
    ├── encode_global_extras.py    re-encode cost_groups.json multi-room NPCs
    ├── build_pristine_cache.py    pickle every COST/CLUT/pal_table
    ├── build_quality_preview.py   4-row side-by-side BG comparisons
    ├── scumm_tree.py              SCUMM v5 parse / mutate / serialize
    ├── scumm_index.py             monkey2.000 (DROO/DCOS/...) rebuilder
    ├── decode_cost.py             SCUMM v5 ClassicCostume RLE decoder
    ├── decode_obim.py             OBIM SMAP decoder (zigzag/majmin codecs)
    ├── decode_amiga_room.py       top-level chunk walker
    ├── encode_cost.py             COST RLE encoder + body rebuilder
    ├── encode_amiga.py            SMAP encoder
    ├── obim_reencode.py           per-OBIM-frame SMAP rebuilder
    ├── room_specials.py           per-room override registry
    ├── patch_talkcolors.py        ActorOps TalkColor literal patcher
    ├── cost_groups.json           multi-room costume group definitions
    └── variants.json              auto-generated per-room variant cids
```

### Build pipeline

1. **Stage 1a** — extract bg + OBIM PNGs from `pc-data/MONKEY2.001`
   (pixel-identical to MISE Explorer output).
2. **Stage 1b** — pristine cache: pickle every COST body / CLUT /
   pal_table from `amiga-data/` so subsequent stages don't chain-corrupt
   state.
3. **Stage 1b'''** — `apply_variants.py`: insert variant cids into
   target rooms' LFLFs, extend DCOS, byte-patch `Costume(SRC)` →
   `Costume(NEW)` in target-room scripts.
4. **Stage 1c/1d** — globally re-encode Guybrush + the cost groups
   (slot-locked palettes, replicated across the "alldisks" room 111
   on every floppy).
5. **Stage 2** — for each of the 98 real rooms, run `inject_room.py`:
   - assemble a joint canvas (bg + OBIM atlas + cost atlas +
     talk-colour swatches) packed in one PyTexturePacker pass
   - run png2amiga `--best` with palette locks for sprite/cursor/Guybrush/
     extras-group slots
   - re-encode OBIM atlas + cost frames against the locked palette,
     iterating `--dither-strength` per costume to fit the 64 KB COST cap
   - rebuild SMAP / COST chunks via the tree pipeline (no in-place
     offset shifts — `scumm_tree.py` reserialises everything)
6. **Stage 2b** — cross-room family palettes (jungle, rapcoffin).
7. **Stage 2b'** — cost-RLE size summary (per-cid dither-bloat WARN +
   per-build totals to stderr).
8. **Stage 2c** — quality previews: 4-row stacks (PC original / Pristine
   Amiga / mi2-redux / EHB demo) into `preview/quality/`.
9. **Stage 3** — package: HDF (RDB-formatted Amiga hard-drive image),
   LHA archive, FS-UAE config + launcher.

## Status

- **98** rooms patched, **2** cross-room palette families
  (jungle: 7 rooms; rapcoffin: 3 rooms)
- **26** per-room variant cids active (allocated from free DCOS slots)
- **6** asset/system rooms intentionally pristine (`icons`, `whoopmap`,
  `open-cred`, `f-rapinfl`, `f-rap2inf`, `copycrap`)
- File size: ~+8% vs pristine
- Validated end-to-end in ScummVM via `--boot-param=N` per room

## Prior art

- **[jmonkey](https://github.com/oduvan/jmonkey)** does the same idea
  for *Monkey Island 1* (SCUMM v4). Doesn't extend to MI2: v5 has a
  more complex script structure, additional OBIM transparency codecs,
  MAJMIN compression, costume RLE differences, and the cross-room
  palette sharing problem (CLUT[192..207] / paletteMod=16) that's
  specific to v5+.
- **Monkey Island Special Edition** (LucasArts, 2009) is official but
  hand-redrew the art — not a re-quantisation of the original PC data.

As far as I can tell, no public project has done this for MI2. If you
know of one, file an issue.

## License & credits

- **Source code** in this repo: MIT.
- **Monkey Island 2 assets** in `amiga-data/`, `pc-data/`, `disks/`,
  and any derived data (`monkey2-hd/`, `dist/`, `preview/`,
  `extracted-pc-pngs/`): copyright LucasArts / Disney. **Don't
  distribute the patched output** — ship the source/configs and let
  users apply them locally with their own legitimate game files.
- [png2amiga](https://github.com/tinic/png2amiga) — the OCS quantiser.
- [ScummVM](https://www.scummvm.org/) — the engine.
- [scummvm-tools/descumm](https://github.com/scummvm/scummvm-tools),
  [PyTexturePacker](https://github.com/wo1fsea/PyTexturePacker),
  [jca02266/lha](https://github.com/jca02266/lha).
