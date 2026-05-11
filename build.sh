#!/bin/bash
# build.sh — full mi2-redux build, idempotent and self-bootstrapping.
#
# Stages:
#   1. ensure tooling (lha, amitools-in-venv, scummvm-tools+descumm, PyTexturePacker)
#   2. (re)build patched game data: 98 real rooms + 2 cross-room palette
#      families (jungle + rapcoffin). Asset/system rooms stay pristine.
#   3. package: HDF (20MB FFS) + LHA archive in dist/
#
# Tweak image settings by editing tools/inject_room.py / tools/encode_amiga.py
# and rerun ./build.sh — only the touched rooms re-run; package step always.
#
# Env vars:
#   BEST=0           skip png2amiga --best (population search). ~30x faster
#                    overall, but bg quality drops; for iteration only.
#                    Default: BEST=1 (full quality).
#   DITHER=<method>  pass --dither <method> to every png2amiga call (joint
#                    --best, OBIM/cost re-quant, S2 measure, family palette,
#                    global encoders). When unset, png2amiga's default is
#                    used (Floyd-Steinberg) and the global encoders + cost
#                    fallback keep their structural --dither none.
#                    Try DITHER=opt-checker for the experimental optimal
#                    checker dither.
#   VERBOSE=1        replay every png2amiga subprocess command to stdout
#                    after each room finishes. Logs land in /tmp/_build_*.
#   SKIP_PATCH=1     skip stage 2 entirely (use existing monkey2-hd/), only
#                    re-package (HDF/LHA/FS-UAE config).
#
# Inputs (must exist):
#   amiga-data/monkey2.0NN     pristine Amiga data files
#   amiga-data/amigaN.ims      pristine sound files
#   extracted-pc-pngs/...      PC bg + object PNGs (MISE Explorer extraction)
#   pc-data/MONKEY2.0NN        PC data (for cross-room ref graph + family sheet)
#
# External tool dependencies: all tracked as submodules under tools/,
# pinned to a specific commit via .gitmodules. ./bootstrap.sh initialises
# every submodule and builds the ones that need compiling
# (png2amiga, scummvm, scummvm-tools, lha-jca) plus a Python venv with
# Pillow + amitools. This script verifies they exist and bails with
# instructions if they're missing.
#
# Override PNG2AMIGA env var to use a manual png2amiga checkout instead
# of the submodule build.

set -euo pipefail
cd "$(dirname "$0")"

REPO="$(pwd)"
TOOLS="$REPO/tools"
VENV="$TOOLS/.venv"
DIST="$REPO/dist"

# --- Stage 1 — tooling -------------------------------------------------

echo "==> Stage 1: tooling"

# All external tools come from submodules; ./bootstrap.sh builds them
# end-to-end. Just verify they exist here.

PNG2AMIGA="${PNG2AMIGA:-$TOOLS/png2amiga/build/png2amiga}"
export PNG2AMIGA
LHA_BIN="$TOOLS/lha-jca/src/lha"
DESCUMM="$TOOLS/scummvm-tools/descumm"

missing=()
[ -x "$PNG2AMIGA" ]              || missing+=("png2amiga ($PNG2AMIGA)")
[ -x "$LHA_BIN" ]                || missing+=("lha-jca ($LHA_BIN)")
[ -x "$DESCUMM" ]                || missing+=("descumm ($DESCUMM)")
[ -d "$TOOLS/PyTexturePacker" ]  || missing+=("PyTexturePacker submodule")
{ [ -d "$TOOLS/scummvm/.git" ] || [ -f "$TOOLS/scummvm/.git" ]; } \
                                 || missing+=("scummvm submodule")
[ -x "$VENV/bin/python3" ]       || missing+=("Python venv ($VENV)")

if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: external tools not built. Missing:"
    printf "  - %s\n" "${missing[@]}"
    echo
    echo "Run ./bootstrap.sh to initialise submodules + build."
    exit 1
fi
export PATH="$VENV/bin:$PATH"

# --- Stage 2 — patch all rooms ----------------------------------------
# Skip if already up-to-date: SKIP_PATCH=1 ./build.sh re-uses the existing
# monkey2-hd/ output, only re-running stage 3 (packaging). Useful when
# tweaking dist artifacts without re-running the ~25 min patch pass.

if [ "${SKIP_PATCH:-0}" = "1" ] && [ -f monkey2-hd/monkey2.011 ]; then
    echo "==> Stage 2 + 2b: SKIPPED (SKIP_PATCH=1)"
else
# Build the pristine asset cache once. inject_room.py reads pristine
# CLUTs / COST bodies / pal_tables / frames / DCOS / DROO from this
# pickle, eliminating the chained-state corruption bugs we hit when
# re-reading from monkey2-hd/ during multi-room injection.
echo "==> Stage 1a: extract PC PNGs (only if missing)"
# Extracts bg + OBIM PNGs from pc-data/MONKEY2.001 into
# extracted-pc-pngs/IMAGES/{backgrounds,objects}/. Pixel-identical to
# MISE Explorer's output (verified). Self-skips when the directories
# are already populated; pass --force to re-extract.
python3 tools/extract_pc_pngs.py >/tmp/_build_extract_pc_pngs.log 2>&1 || {
    echo "FAIL (see /tmp/_build_extract_pc_pngs.log)"; exit 1; }

echo "==> Stage 1b: build pristine cache"
python3 tools/build_pristine_cache.py >/dev/null

# Descumm-derived inputs that the per-room patcher needs. These are
# slow to regenerate (descumm on every script chunk in every room —
# ~30 s each), and content-stable since they're a deterministic scan
# of pristine amiga-data, so we self-skip when present.
echo "==> Stage 1b': descumm-derived inputs (regen if missing)"
gen() {
    local out="$1"; shift
    if [ -f "$out" ]; then
        printf "  %-32s present\n" "$(basename "$out")"
    else
        printf "  %-32s regenerating...\n" "$(basename "$out")"
        log="/tmp/_build_$(basename "$out" .json).log"
        "$@" >"$log" 2>&1 || { echo "FAIL (see $log)"; exit 1; }
    fi
}
gen tools/costume_refs.json       python3 tools/costume_room_refs.py
gen tools/talk_colors_survey.json python3 tools/talk_colors_survey.py

# global_actor_palette.json is MUTATED each build (encode_global_guybrush
# writes canonical_amiga + rooms_drawn_in; encode_global_extras adds
# groups). Regenerate fresh every run so each build starts from a known
# state independent of leftover groups from the prior run.
echo "==> Stage 1b'': regenerate global_actor_palette.json (always)"
python3 tools/build_actor_palette.py >/tmp/_build_global_actor_palette.log 2>&1 || {
    echo "FAIL (see /tmp/_build_global_actor_palette.log)"; exit 1; }

if [ "${BEST:-1}" = "0" ]; then
    echo "==> Stage 2: patch rooms (BEST=0 — fast iteration mode, lower bg quality)"
else
    echo "==> Stage 2: patch rooms (--best enabled — slow, ship-quality)"
fi
export BEST
export DITHER VERBOSE

# Reset monkey2-hd/ to pristine, then rerun every room in injection order.
# inject_room.py chains state through monkey2-hd/, so each call uses the
# previously-patched files as input — necessary for cumulative DROO/DCHR/etc.
# offset shifts to be correct across multiple rooms in the same disk.
rm -f monkey2-hd/monkey2.0*
mkdir -p monkey2-hd
cp amiga-data/monkey2.0* monkey2-hd/

# Stage 1b'''. Apply per-room variant cids declared in tools/variants.json.
# Inserts new COST chunks (cloned from src_cid's pristine body) into each
# variant's target room LFLF, extends DCOS, and patches every
# Costume(src_cid) call inside the target room's scripts to use new_cid.
# After this step inject_room.py treats variant cids as ordinary
# home-LFLF costumes and re-encodes them against the target room's
# joint --best palette.
echo "==> Stage 1b''': apply variant cids"
python3 tools/apply_variants.py >/tmp/_build_apply_variants.log 2>&1 || {
    echo "FAIL (see /tmp/_build_apply_variants.log)"; exit 1; }

# Stage 1c: globally re-encode Guybrush + extras costumes BEFORE the
# per-room loop. inject_room.py skips any cid in this set; without these
# two passes those costumes ship pristine (wrong palette range). Both
# write into monkey2-hd/ via the tree-rebuild pipeline. Order matters:
# Guybrush locks palette[22..31], extras must run after so its locks
# can avoid Guybrush's range. Both honour BEST.
echo "==> Stage 1c: encode global Guybrush"
python3 tools/encode_global_guybrush.py >/tmp/_build_global_guybrush.log 2>&1 || {
    echo "FAIL (see /tmp/_build_global_guybrush.log)"; exit 1; }
echo "==> Stage 1d: encode global extras (cost groups)"
python3 tools/encode_global_extras.py >/tmp/_build_global_extras.log 2>&1 || {
    echo "FAIL (see /tmp/_build_global_extras.log)"; exit 1; }

ROOMS=(
    part1 scabb-isl sky shore campfire weenie woodtick cartograp bar cu-spit
    grill inn largos laundry woodshop cemetery graves crypt cu-coffin swamp
    voodoo dred-cabi dred-clif dred-deck part2 phatt-isl wharf pier jail library
    catalog cards casino alley waterfall faucet beach cottage rums basement
    phatts stairs bedroom cu-phatt booty-isl ville spitvile antique costume-s
    funeral-p kiosk mansion front-man back-mans entryway boudoir kitchen clifftop
    cliffbot tree-base tree-top tree-hous under-shi water over-gall galleon part3
    le-fortre le-explos le-dock le-passag le-hall le-door le-office le-jail le-cel
    le-dynomi le-closeu le-tortur bone-map kates-shi map raft part4
    dinky-bea dinky-spo dinky-hol elevator melee storage maintenan lost-foun
    undergrou junglea jungleb junglec long-shot bigwhoop
)
# Asset/system rooms intentionally NOT patched — they stay pristine to
# preserve cross-room OBIM rendering. See tools/CROSS_ROOM_PALETTE_PLAN.md.

n=0
for slug in "${ROOMS[@]}"; do
    n=$((n+1))
    printf "  [%2d/%2d] %-12s " "$n" "${#ROOMS[@]}" "$slug"
    if python3 tools/inject_room.py "$slug" >/tmp/_build_$slug.log 2>&1; then
        # Extract the S2 summary line from the per-room log
        s2_line=$(grep -m1 "S2:" /tmp/_build_$slug.log | sed 's/^.*S2: //')
        if [ -n "$s2_line" ]; then
            echo "ok  S2: $s2_line"
        else
            echo "ok"
        fi
        # When VERBOSE=1, replay every png2amiga command from the room's log
        # to stdout so the user sees them inline with the build progress.
        if [ "${VERBOSE:-0}" = "1" ]; then
            grep "^\[png2amiga" /tmp/_build_$slug.log || true
        fi
    else
        echo "FAIL (see /tmp/_build_$slug.log)"
        exit 1
    fi
done

# --- Stage 2b — cross-room palette families ---------------------------

echo "==> Stage 2b: family palettes"

declare -a FAMILIES=(
    "jungle f-turf f-foregro f-trees f-plants junglea jungleb junglec"
    "rapcoffin f-rapothe f-rapshri cu-coffin"
)
for fam in "${FAMILIES[@]}"; do
    set -- $fam
    fid=$1; shift
    members=("$@")
    echo "  family $fid: ${members[*]}"
    python3 tools/build_family_palette.py "$fid" "${members[@]}" >/dev/null
    for slug in "${members[@]}"; do
        printf "    [family %-9s] %-12s " "$fid" "$slug"
        if python3 tools/inject_room.py "$slug" --shared-palette "tools/shared_palettes/${fid}.json" >/tmp/_build_$slug.log 2>&1; then
            s2_line=$(grep -m1 "S2:" /tmp/_build_$slug.log | sed 's/^.*S2: //')
            if [ -n "$s2_line" ]; then
                echo "ok  S2: $s2_line"
            else
                echo "ok"
            fi
        else
            echo "FAIL (see /tmp/_build_$slug.log)"
            exit 1
        fi
    done
done
fi  # end SKIP_PATCH guard

# --- Stage 2b' — cost-RLE size summary --------------------------------
# Aggregate every per-room "Cost RLE summary" line + dither WARN that
# inject_room.py emitted. dithered RLE can balloon 2-10× vs pristine
# for sprite-heavy costumes; this is the spot to spot offenders that
# should be re-encoded with --dither none (the engine has a 64KB COST
# baseptr cap and dither bloat is the usual cause of overflow).
echo "==> Stage 2b': cost-RLE size summary" >&2
# Subshell so `set -e` (inherited from this script) doesn't trip on
# grep's exit-1 when a per-room log lacks the line. Whole block writes
# to stderr — it's diagnostic output, not pipe-friendly stdout.
(
    set +e
    grep -h "Cost RLE summary:" /tmp/_build_*.log 2>/dev/null
    echo
    echo "Per-cid dither bloat warnings (≥2× pristine):"
    grep -h "WARN: dither bloated" /tmp/_build_*.log 2>/dev/null | sort -u
    if ! grep -q "WARN: dither bloated" /tmp/_build_*.log 2>/dev/null; then
        echo "  (none)"
    fi
    echo
    echo "Totals (sum across per-room summaries):"
    python3 - <<'PY' 2>/dev/null
import glob, re
p_total = r_total = n = 0
for f in glob.glob('/tmp/_build_*.log'):
    for ln in open(f, errors='ignore'):
        m = re.search(r'(\d+)B pristine -> (\d+)B re-encoded', ln)
        if m:
            p_total += int(m.group(1))
            r_total += int(m.group(2))
            n += 1
print(f'  {n} cost re-encode batches: '
      f'{p_total/1024:.1f} KB pristine -> {r_total/1024:.1f} KB re-encoded '
      f'({r_total/max(1,p_total):.2f}×)')
PY
) >&2

# --- Stage 2c — quality previews --------------------------------------
# Pack each room's final-quantised bg + every OBIM frame + every cost
# frame into a single tight PNG under preview/quality/<rid>-<room>.png
# so dither / palette regressions can be eyeballed across rooms without
# launching ScummVM. Pure post-process — reads only what Stage 2/2b
# already wrote into preview/intermediates/.
echo "==> Stage 2c: quality previews"
python3 tools/build_quality_preview.py --all >/tmp/_build_quality_previews.log 2>&1 \
    || echo "  [warn] preview pack failed — see /tmp/_build_quality_previews.log"

# --- Stage 3 — package -------------------------------------------------

echo "==> Stage 3: package"

mkdir -p "$DIST"

# 3a. Stage the full Amiga-HD directory tree:
#       boot infra (c/, devs/, s/, monkey2 binary) extracted from pristine
#       Disk 01's filesystem  +  all patched monkey2.0NN  +  all amigaN.ims.
#     This becomes the fully-self-contained "single-floppy" install — no
#     disk swapping needed since every file the game asks for is on DH0:.
STAGE="$DIST/MonkeyHD"
rm -rf "$STAGE"
mkdir -p "$STAGE"
# Boot infrastructure (c/, devs/, s/, monkey2 binary) for the Amiga HD
# install. Two sources, in priority order:
#   1. boot-infra/ in the repo (committed, ~200 KB) — preferred
#   2. disks/Monkey2 Disk 01 ADF — fallback for users with the floppies
# If neither is present, the dist HDF/LHA is built data-only (works for
# ScummVM but won't boot directly on a real Amiga).
HAVE_BOOT=0
if [ -d "$REPO/boot-infra" ] && [ -f "$REPO/boot-infra/monkey2" ]; then
    cp -R "$REPO/boot-infra/." "$STAGE/"
    HAVE_BOOT=1
else
    DISK1_ADF="$(ls "$REPO/disks/"*"Disk 01 of 11"*.adf 2>/dev/null | head -1 || true)"
    if [ -n "$DISK1_ADF" ] && [ -f "$DISK1_ADF" ]; then
        EXTRACT_TMP="$(mktemp -d)"
        xdftool "$DISK1_ADF" unpack "$EXTRACT_TMP" >/dev/null
        cp -R "$EXTRACT_TMP/Monkey2 Disk 1/." "$STAGE/"
        rm -rf "$EXTRACT_TMP"
        # Drop pristine monkey2.001 (came from Disk 1) — we'll overwrite with our patched copy below.
        rm -f "$STAGE/monkey2.001"
        HAVE_BOOT=1
    else
        echo "  no boot-infra/ or disks/Disk 01 ADF — skipping Workbench bootstrap (HDF/LHA will be data-only)"
    fi
fi
cp monkey2-hd/monkey2.0* "$STAGE/"
for ims in amiga-data/amiga*.ims; do
    cp "$ims" "$STAGE/"
done
echo "  Staged HD root: $STAGE"

# 3b. HDF (RDB-formatted hard-drive image for emulators that prefer block-level)
HDF="$DIST/monkey2-hd.hdf"
rm -f "$HDF"
xdftool "$HDF" create size=20Mi + format MonkeyHD ffs >/dev/null
for f in "$STAGE"/*; do
    [ -f "$f" ] || continue
    xdftool "$HDF" write "$f" >/dev/null
done
# Also copy the c/, devs/, s/ subdirs into the HDF (only present when we
# had a Disk 01 ADF to extract Workbench infrastructure from).
if [ "$HAVE_BOOT" = "1" ]; then
    for d in c devs s; do
        if [ -d "$STAGE/$d" ]; then
            xdftool "$HDF" makedir "$d" >/dev/null
            for sub in "$STAGE/$d"/*; do
                [ -e "$sub" ] || continue
                xdftool "$HDF" write "$sub" "$d/$(basename "$sub")" >/dev/null
            done
        fi
    done
fi
echo "  HDF: $HDF ($(du -h "$HDF" | awk '{print $1}'))$([ "$HAVE_BOOT" = "0" ] && echo ' [data-only — no Disk 01 ADF]')"

# 3c. LHA archive (drop the directory tree into Monkey2/ on an Amiga HD)
LHA="$DIST/monkey2-hd.lha"
rm -f "$LHA"
STAGE_LHA_PARENT="$(mktemp -d)"
cp -R "$STAGE" "$STAGE_LHA_PARENT/Monkey2"
cat >"$STAGE_LHA_PARENT/Monkey2/README.txt" <<EOF
Monkey Island 2: LeChuck's Revenge — Amiga HD redux

Self-contained install. Drop the Monkey2/ directory on your Amiga HD
and run "Monkey2:monkey2" — or just CD in and execute s:startup-sequence.

Built $(date -u +%Y-%m-%dT%H:%M:%SZ) from mi2-redux.
EOF
(cd "$STAGE_LHA_PARENT" && "$LHA_BIN" a "$LHA" "Monkey2/" >/dev/null)
rm -rf "$STAGE_LHA_PARENT"
echo "  LHA: $LHA ($(du -h "$LHA" | awk '{print $1}'))"

# 3d. FS-UAE launcher + config (mounts $STAGE as DH0: directory device)
LAUNCH="$DIST/launch-fs-uae.sh"
CFG="$DIST/monkey2.fs-uae"
cat >"$CFG" <<EOF
# FS-UAE config for the mi2-redux Amiga HD build.
# Generated by build.sh — regenerate by re-running ./build.sh.

[fs-uae]
amiga_model = A500
chip_memory = 512
slow_memory = 512
floppy_drive_0_priority = 0

# Silence the floppy click — booting from HDF, no floppies are inserted,
# and the click is just noise.
floppy_drive_volume_empty = 0

# NTSC + visible overscan. MI2's PC art has no 50/60 Hz timing issues
# and NTSC is the natural fit for the original 1992 Amiga release in
# the US market. Overscan exposes the off-edge pixels the game draws
# (status bar, verb panel) without cropping.
ntsc_mode = 1
amiga_video_standard = NTSC
zoom = full

# DH0: maps to the staged directory (host filesystem device — no .hdf needed).
# FS-UAE auto-creates the AmigaDOS device from the host directory contents.
hard_drive_0 = $STAGE
hard_drive_0_label = Workbench
hard_drive_0_priority = 6

# Boot order: HD first, fall back to nothing.
# (Kickstart ROM must be set on your end — set FS_UAE_KICKSTART_FILE env or
# edit FS-UAE Launcher's Configurations tab.)
EOF

cat >"$LAUNCH" <<'EOF'
#!/bin/bash
# launch-fs-uae.sh — launch FS-UAE on the staged mi2-redux build.
# Pass --hdf to use the .hdf instead of the directory mount.
set -euo pipefail
cd "$(dirname "$0")"

KICKSTART="${FS_UAE_KICKSTART_FILE:-}"
if [ -z "$KICKSTART" ]; then
    # Common Mac FS-UAE Launcher locations
    for p in \
        "$HOME/Documents/FS-UAE/Kickstarts/kick34005.A500.rom" \
        "$HOME/Documents/FS-UAE/Kickstarts/Kickstart-v1.3-rev34.5-1987-Commodore-A500-A1000-A2000-CDTV.rom" \
        "$HOME/Library/Application Support/fs-uae/Kickstarts/kick34005.A500.rom"
    do
        [ -f "$p" ] && KICKSTART="$p" && break
    done
fi
if [ -z "$KICKSTART" ] || [ ! -f "$KICKSTART" ]; then
    echo "ERROR: no Kickstart ROM found. Set FS_UAE_KICKSTART_FILE or drop one"
    echo "into ~/Documents/FS-UAE/Kickstarts/ and rerun."
    exit 1
fi

exec fs-uae \
    --kickstart_file="$KICKSTART" \
    monkey2.fs-uae
EOF
chmod +x "$LAUNCH"
echo "  FS-UAE config: $CFG"
echo "  FS-UAE launcher: $LAUNCH"

echo "==> Done."
ls -lh "$DIST/"
