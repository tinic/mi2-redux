# amiga-data/

Pristine 1992 Amiga *Monkey Island 2: LeChuck's Revenge* data files.
**You must supply these yourself from your own legitimate copy of the
game.** None of these files are distributed with this repo.

## What goes here

The directory should end up looking like this:

```
amiga-data/
├── monkey2          (~74 KB)   the Amiga executable / boot stub
├── monkey2.000      (~11 KB)   the master index (DROO/DCOS/DSCR/...)
├── monkey2.001      (~370 KB)  Disk 1 data
├── monkey2.002      (~700 KB)  Disk 2 data
├── monkey2.003      (~660 KB)  Disk 3 data
├── monkey2.004      (~770 KB)  Disk 4 data
├── monkey2.005      (~770 KB)  Disk 5 data
├── monkey2.006      (~750 KB)  Disk 6 data
├── monkey2.007      (~890 KB)  Disk 7 data
├── monkey2.008      (~720 KB)  Disk 8 data
├── monkey2.009      (~700 KB)  Disk 9 data
├── monkey2.010      (~740 KB)  Disk 10 data
├── monkey2.011      (~660 KB)  Disk 11 data
├── amiga1.ims       (~340 KB)  Disk 1 music samples (instrument bank)
├── amiga2.ims       …          Disk 2 music samples
├── …                           one .ims per disk
└── amiga11.ims      (~410 KB)  Disk 11 music samples
```

Sizes are approximate (bookkeeping bytes vary slightly between releases).
The build's pristine cache uses content hashes, not file sizes.

## How to extract them

The original release shipped on 11 floppy ADFs. You can extract the data
files from your own legitimate ADF images using `xdftool` (part of
`amitools`, which `bootstrap.sh` already installs into the project's
private venv):

```bash
# For each Disk NN ADF you own:
xdftool 'Monkey2 Disk NN.adf' unpack /tmp/extract_disk_NN

# The relevant files live under "Monkey2 Disk N/" inside the unpacked tree.
# Pull out the ones we need:
cp /tmp/extract_disk_01/Monkey2\ Disk\ 1/monkey2     amiga-data/
cp /tmp/extract_disk_01/Monkey2\ Disk\ 1/monkey2.001 amiga-data/
cp /tmp/extract_disk_01/Monkey2\ Disk\ 1/amiga1.ims  amiga-data/
# ... repeat per disk for monkey2.NNN + amigaN.ims
# (monkey2.000 lives on Disk 1; the engine binary `monkey2` only lives on Disk 1)
```

If you have the ADFs in `disks/` already, look there first — `build.sh`'s
packaging stage uses one of them (Disk 1) to extract Workbench infrastructure
(c/, devs/, s/) into the final HDF, but it does not auto-populate this
directory.

## How the build uses these files

Once present, `build.sh` reads them via:

- **`build_pristine_cache.py`** (`tools/`) — pickles every CLUT, COST body,
  and pal_table from these files into `tools/pristine_cache.pkl` once per
  build. All later stages read from the pickle, not these files directly,
  so corruption from chained-state edits is impossible.
- **`encode_global_guybrush.py` / `encode_global_extras.py`** — read PC
  source from `../pc-data/MONKEY2.001`, but write the patched COST chunks
  into `../monkey2-hd/monkey2.NNN` (NOT here). This directory stays
  pristine throughout the build.
- **packaging stage** — copies the `.ims` files verbatim into
  `dist/MonkeyHD/` and the HDF.

## Don't commit these to a public fork

These files are LucasArts/Disney copyright. The repo's `.gitignore` does
**not** ignore this directory (the original setup was a private repo
where copyright data was allowed). Before making the repo public, delete
`monkey2*` and `amiga*.ims` from this directory, leaving this README in
place.
