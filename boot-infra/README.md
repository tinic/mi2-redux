# boot-infra/

Amiga Workbench / boot infrastructure extracted from the original
*Monkey Island 2* Disk 01 ADF. Used by `build.sh`'s Stage 3 to build
a self-contained bootable Amiga HD install (`dist/MonkeyHD/`,
`dist/monkey2-hd.hdf`, `dist/monkey2-hd.lha`).

## Contents (~200 KB)

```
c/                      Amiga DOS commands shipped on the floppy
                        (echo, run, stack, ask, iconx, endcli, Type,
                         Context — Conet's ASCII banner)
devs/                   device drivers (system-configuration)
s/startup-sequence      boot script
monkey2                 the Amiga MI2 engine binary (no game data)
CRMonkey2               80-byte HD-launch helper from the [cr Conet]
                        crack — required by their patched monkey2
                        binary at HD startup. Without it, the engine
                        crashes early. (`startup-sequence` invokes it
                        on every boot.)
```

## Why this is in the repo

When `build.sh` builds the dist HDF, it needs these files to make a
disk image that boots directly on Amiga (or in FS-UAE without manual
disk-juggling). Without them, the dist still works for ScummVM (which
just needs the patched `monkey2.NNN` data files) but the HDF won't
boot on a real Amiga or in floppy-emulator mode.

These files are tiny (~200 KB) and contain no game art / sound / text
— just the engine launcher and standard Amiga DOS plumbing — so it's
worth shipping them with the repo to avoid making every contributor
re-extract from their own ADF.

## Source

Extracted from `disks/Monkey Island 2 ... Disk 01 of 11 ... .adf`
via:

```bash
source tools/.venv/bin/activate
TMP=$(mktemp -d)
xdftool 'disks/...Disk 01...adf' unpack "$TMP"
mkdir -p boot-infra
cp -R "$TMP/Monkey2 Disk 1/c" "$TMP/Monkey2 Disk 1/devs" \
       "$TMP/Monkey2 Disk 1/s" "$TMP/Monkey2 Disk 1/monkey2" \
       "$TMP/Monkey2 Disk 1/CRMonkey2" \
       boot-infra/
rm -rf "$TMP"
```
