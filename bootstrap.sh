#!/bin/bash
# bootstrap.sh — one-shot setup of system packages + tool submodules.
# Run once on a fresh machine; afterwards ./build.sh produces the
# patched game data and dist artefacts.
#
# Supported platforms:
#   - macOS (Homebrew)
#   - Debian 13 (trixie) / Ubuntu derivatives (apt)
#
# What this does:
#   1. Install system packages: gcc-15 (for png2amiga's C++26 build),
#      cmake, autoconf/automake, git, python3 with venv + pip, plus
#      SDL2 + libpng (scummvm + Pillow).
#   2. Initialise + recursively fetch every submodule under tools/
#      (.gitmodules pins each to a specific commit):
#        - tools/png2amiga       https://github.com/tinic/png2amiga
#        - tools/scummvm-tools   https://github.com/scummvm/scummvm-tools
#        - tools/scummvm         https://github.com/scummvm/scummvm
#        - tools/PyTexturePacker https://github.com/wo1fsea/PyTexturePacker
#        - tools/lha-jca         https://github.com/jca02266/lha
#   3. Apply the local scummvm patch (MD5 fallback so ScummVM accepts
#      our patched-and-rehashed data files).
#   4. Build each tool that needs compiling (png2amiga, scummvm,
#      scummvm-tools, lha-jca).
#   5. Create a Python venv with Pillow + amitools.
#
# Env vars:
#   SKIP_APT=1     skip system-package install
#   SKIP_SUBMOD=1  assume submodules already initialised
#   SKIP_BUILDS=1  skip the compile steps (just system pkgs + clones)
#
# After bootstrap, ./build.sh produces patched game data + dist artefacts.

set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"
TOOLS="$REPO_ROOT/tools"

# --- Detect platform -------------------------------------------------
PLATFORM=""
if [ "$(uname -s)" = "Darwin" ]; then
    PLATFORM="macos"
    if ! command -v brew >/dev/null 2>&1; then
        echo "ERROR: Homebrew not installed. Install from https://brew.sh first."
        exit 1
    fi
elif [ -f /etc/debian_version ] && command -v apt-get >/dev/null 2>&1; then
    PLATFORM="debian"
else
    echo "ERROR: unsupported platform $(uname -s)."
    echo "Supported: macOS (Homebrew), Debian/Ubuntu (apt-get)."
    exit 1
fi
echo "==> Platform: $PLATFORM"

# --- System packages -------------------------------------------------
if [ "${SKIP_APT:-0}" = "1" ]; then
    echo "==> Skipping system-package install (SKIP_APT=1)"
else
    echo "==> Installing system packages"
    case "$PLATFORM" in
    macos)
        brew install gcc cmake autoconf automake git python@3.13 \
                     sdl2 libpng || true
        ;;
    debian)
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends \
            build-essential cmake autoconf automake git \
            python3 python3-venv python3-pip \
            libsdl2-dev libpng-dev libfreetype6-dev libwxgtk3.2-dev || true
        if ! command -v g++-15 >/dev/null 2>&1; then
            if apt-cache show g++-15 >/dev/null 2>&1; then
                sudo apt-get install -y g++-15 gcc-15
            else
                echo "WARNING: g++-15 not in apt cache; png2amiga build may fail."
            fi
        fi
        ;;
    esac
fi

# --- Submodules ------------------------------------------------------
if [ "${SKIP_SUBMOD:-0}" = "1" ]; then
    echo "==> Skipping submodule init (SKIP_SUBMOD=1)"
else
    echo "==> Initialising submodules"
    git -C "$REPO_ROOT" submodule update --init --recursive
fi

if [ "${SKIP_BUILDS:-0}" = "1" ]; then
    echo "==> Skipping builds (SKIP_BUILDS=1)"
    exit 0
fi

# --- png2amiga (C++26, GCC 15) ---------------------------------------
echo "==> Building png2amiga"
if [ ! -x "$TOOLS/png2amiga/build/png2amiga" ]; then
    if ! command -v g++-15 >/dev/null 2>&1; then
        echo "ERROR: g++-15 not on PATH (png2amiga needs C++26)."
        echo "  macOS: brew install gcc"
        echo "  Debian: sudo apt-get install gcc-15 g++-15"
        exit 1
    fi
    cmake -B "$TOOLS/png2amiga/build" \
          -DCMAKE_C_COMPILER=gcc-15 \
          -DCMAKE_CXX_COMPILER=g++-15 \
          "$TOOLS/png2amiga"
    cmake --build "$TOOLS/png2amiga/build" --parallel
fi
echo "  png2amiga: $TOOLS/png2amiga/build/png2amiga"

# --- scummvm-tools (descumm) -----------------------------------------
echo "==> Building scummvm-tools (descumm)"
if [ ! -x "$TOOLS/scummvm-tools/descumm" ]; then
    pushd "$TOOLS/scummvm-tools" >/dev/null
    ./configure --disable-wxwidgets >/dev/null
    make descumm -j8
    popd >/dev/null
fi
echo "  descumm: $TOOLS/scummvm-tools/descumm"

# --- lha-jca ---------------------------------------------------------
echo "==> Building lha-jca"
if [ ! -x "$TOOLS/lha-jca/src/lha" ]; then
    pushd "$TOOLS/lha-jca" >/dev/null
    aclocal && autoheader && automake --add-missing >/dev/null 2>&1 || true
    autoconf
    ./configure >/dev/null
    make -j8
    popd >/dev/null
fi
echo "  lha: $TOOLS/lha-jca/src/lha"

# --- scummvm (with MD5-fallback patch) -------------------------------
echo "==> Building scummvm (MI2-Amiga MD5-fallback patched)"
SVMV_DETECT="$TOOLS/scummvm/engines/scumm/detection_internal.h"
if ! grep -q '_mi2redux_fallback' "$SVMV_DETECT" 2>/dev/null; then
    echo "  Applying MD5-fallback patch"
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
    ' "$SVMV_DETECT" > "$SVMV_DETECT.new"
    mv "$SVMV_DETECT.new" "$SVMV_DETECT"
fi
if [ ! -x "$TOOLS/scummvm/scummvm" ]; then
    pushd "$TOOLS/scummvm" >/dev/null
    ./configure --backend=sdl --disable-all-engines --enable-engine=scumm \
                >/dev/null 2>&1 || ./configure --backend=sdl
    make -j8
    popd >/dev/null
fi
echo "  scummvm: $TOOLS/scummvm/scummvm"

# --- Python venv (Pillow + amitools) ---------------------------------
echo "==> Python venv"
if [ ! -d "$TOOLS/.venv" ]; then
    python3 -m venv "$TOOLS/.venv"
fi
"$TOOLS/.venv/bin/pip" install --quiet --upgrade pip
"$TOOLS/.venv/bin/pip" install --quiet Pillow amitools
echo "  venv: $TOOLS/.venv"

echo
echo "==> Bootstrap complete."
echo "    Place game data files (see amiga-data/README.md, pc-data/README.md,"
echo "    disks/README.md) and run ./build.sh."
