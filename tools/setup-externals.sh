#!/bin/bash
# Fetch and build the external dependencies mi2-redux needs at runtime.
# Each clone goes into a gitignored subdirectory.
set -euo pipefail
cd "$(dirname "$0")"

# 1. ScummVM (custom build with MD5-fallback patch — the patch lives in
#    engines/scumm/detection_internal.h::findInMD5Table). Required because
#    patched data files have different MD5s than the originals.
if [ ! -d scummvm ]; then
    echo "Cloning ScummVM..."
    git clone --depth 1 https://github.com/scummvm/scummvm.git
    pushd scummvm >/dev/null
    # Apply the MD5 fallback patch for mi2-redux dev (any unknown MD5 falls
    # back to "Monkey Island 2 Amiga English"). Without this, ScummVM rejects
    # every patched data file we produce. Patch is hand-applied; if it
    # already matches, the awk no-op skips.
    awk '
      /static const MD5Table \*findInMD5Table/ {found=1}
      found && /return r;/ && !patched {
        print
        print "\t// [mi2-redux] fallback for unknown MD5s — treat as Monkey Island 2 Amiga English"
        print "\tstatic const MD5Table _mi2redux_fallback = {"
        print "\t\t\"\", \"monkey2\", \"Amiga\", \"\", -1, Common::EN_ANY, Common::kPlatformAmiga"
        print "\t};"
        print "\treturn &_mi2redux_fallback;"
        patched=1
        next
      }
      {print}
    ' engines/scumm/detection_internal.h > engines/scumm/detection_internal.h.new
    mv engines/scumm/detection_internal.h.new engines/scumm/detection_internal.h
    ./configure --backend=sdl --disable-all-engines --enable-engine=scumm 2>/dev/null || ./configure --backend=sdl
    make -j8
    popd >/dev/null
fi

# 2. scummvm-tools (descumm — SCUMM-script disassembly for the cross-room
#    reference graph in cross_room_refs_v2.py)
if [ ! -d scummvm-tools ]; then
    echo "Cloning + building scummvm-tools..."
    git clone --depth 1 https://github.com/scummvm/scummvm-tools.git
    pushd scummvm-tools >/dev/null
    ./configure --disable-wxwidgets
    make descumm
    popd >/dev/null
fi

# 3. PyTexturePacker (OBIM atlas packing)
if [ ! -d PyTexturePacker ]; then
    echo "Cloning PyTexturePacker..."
    git clone --depth 1 https://github.com/wo1fsea/PyTexturePacker.git
fi

echo
echo "External dependencies ready:"
echo "  ./scummvm/scummvm"
echo "  ./scummvm-tools/descumm"
echo "  ./PyTexturePacker/"
