# pc-data/

Pristine PC (DOS) *Monkey Island 2: LeChuck's Revenge* data files.
**You must supply these yourself from your own legitimate copy of the
game.** None of these files are distributed with this repo.

## What goes here

The directory should end up looking like this:

```
pc-data/
├── MONKEY2.000     (~11 KB)   master index (RNAM/MAXS/DROO/DOBJ/DLFL/DSCR/DCOS/...)
├── MONKEY2.001     (~6 MB)    LECF — every room's bg, OBIM, CLUT, COST, scripts
├── MONKEY2.EXE     (~290 KB)  DOS launcher (used to identify the release)
├── ADLIB.IMS                  AdLib instrument bank
├── ROLAND.IMS                 MT-32 instrument bank
├── SOUNBLAS.IMS               Sound Blaster instrument bank
├── SPEAKER.IMS                PC speaker instrument bank
├── MT32_CONTROL.ROM           MT-32 ROM dump (only required for MT-32 audio)
├── MT32_PCM.ROM               MT-32 PCM dump (same — optional unless using MT-32)
└── SAVEGAME.001               (optional — not required by the build)
```

This is the **floppy** PC release (1991, US/EU). Other releases (CD,
deluxe edition, GOG/Steam re-bundle) ship the same data files inside
different containers — extract the loose `MONKEY2.000` / `MONKEY2.001`
and they'll work.

## How to source them

Legal options:

- **Original 1991 floppy install** — install to a DOS machine or DOSBox,
  then copy the install directory.
- **GOG.com — *Monkey Island 2 Special Edition*** — bundles the original
  classic data alongside the SE assets. Extract from the install: the
  `MONKEY2.000` and `MONKEY2.001` files live under
  `<install-dir>/Classic/`. The DOSBox config bundled with GOG points
  there.
- **Steam — *MI2 Special Edition*** — same arrangement; the classic
  files are under `<install-dir>/Classic/`.

The `.IMS` audio bank files are part of the same install. If you only
care about Sound Blaster output, you can omit the others.

## How the build uses these files

- **`tools/extract_pc_pngs.py`** (Stage 1a in `build.sh`) walks every
  ROOM in `MONKEY2.001`, decodes the bg + every OBIM SMAP via the
  minimal SCUMM v5 PC SMAP decoder in `tools/decode_pc_room.py`, and
  writes paletted PNGs to `extracted-pc-pngs/IMAGES/`. Self-skips if
  the output dirs are already populated; pass `--force` to re-extract.
  Pixel-identical to MISE Explorer's output.
- **`encode_global_guybrush.py`** reads `MONKEY2.001` to extract Guybrush's
  PC COST chunk (cost_id 1, home rid 4) — the higher-fidelity sprite shape
  data ends up in the patched Amiga build.
- **`encode_global_extras.py`** does the same for the multi-room costumes
  defined in `tools/cost_groups.json` (extras_a_*, extras_b, extras_c,
  extras_d).
- **`tools/pc_data.py`** is the helper module both encoders share — parses
  PC LOFF / DCOS / per-room CLUTs.

## Don't commit these to a public fork

These files are LucasArts/Disney copyright. The repo's `.gitignore` does
**not** ignore this directory. Before making the repo public, delete
`MONKEY2.*`, `*.IMS`, `*.ROM` from this directory, leaving this README
in place.
