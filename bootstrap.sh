#!/bin/bash
# bootstrap.sh — one-shot setup of system packages + 3rd-party tools.
# Run once on a fresh machine; afterwards ./build.sh produces the patched
# game data and dist artefacts.
#
# Supported platforms:
#   - macOS (Homebrew)
#   - Debian 13 (trixie) / Ubuntu derivatives (apt)
#
# What this does:
#   1. Install system packages: gcc-15 (for png2amiga's C++26 build),
#      cmake, autoconf/automake (for lha-jca + scummvm-tools), git,
#      python3 with venv + pip.
#   2. Clone + build png2amiga from https://github.com/tinic/png2amiga
#      into $PNG2AMIGA_DIR (default: ~/png2amiga). Build uses gcc-15
#      because png2amiga targets the C++26 draft (-std=c++2c) and
#      the LSP-only false positives don't affect the actual GCC build.
#
# After bootstrap, ./build.sh handles:
#   - lha-jca (jca02266 fork; for read+write LHA archives)
#   - python venv (amitools, Pillow)
#   - scummvm-tools (descumm)
#   - PyTexturePacker (clone-only, no build)
#
# Env vars:
#   PNG2AMIGA_DIR    where to clone+build png2amiga (default: ~/png2amiga)
#   SKIP_APT=1       skip system-package install (you've already done it)

set -euo pipefail
cd "$(dirname "$0")"

PNG2AMIGA_DIR="${PNG2AMIGA_DIR:-$HOME/png2amiga}"
PNG2AMIGA_REPO="https://github.com/tinic/png2amiga.git"

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
        # gcc        -> Homebrew's current GCC formula installs g++-15.
        # cmake      -> png2amiga build (CMake 3.28+).
        # autoconf,
        # automake   -> lha-jca + scummvm-tools configure.
        # git, python -> obvious. Homebrew Python keeps a venv-friendly
        #               python3 separate from the macOS system Python.
        brew install gcc cmake autoconf automake git python@3.13 || true
        ;;
    debian)
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends \
            build-essential cmake autoconf automake git \
            python3 python3-venv python3-pip
        # Debian 13 (trixie) ships gcc-14 by default; png2amiga needs
        # gcc-15 for the C++26 draft features. Try the trixie/sid package
        # first, then bookworm-backports, then warn.
        if ! command -v g++-15 >/dev/null 2>&1; then
            if apt-cache show g++-15 >/dev/null 2>&1; then
                sudo apt-get install -y g++-15 gcc-15
            else
                echo
                echo "WARNING: g++-15 not in apt cache."
                echo "  Add a backports/snapshot repo or build GCC 15 from source."
                echo "  png2amiga's cmake step below will likely fail."
                echo
            fi
        fi
        ;;
    esac
fi

# --- png2amiga --------------------------------------------------------
echo "==> png2amiga at $PNG2AMIGA_DIR"
if [ ! -d "$PNG2AMIGA_DIR" ]; then
    echo "  Cloning $PNG2AMIGA_REPO (with submodules)"
    git clone --recurse-submodules "$PNG2AMIGA_REPO" "$PNG2AMIGA_DIR"
else
    echo "  Repo present; pulling latest"
    git -C "$PNG2AMIGA_DIR" pull --ff-only || \
        echo "  (pull failed — keeping existing checkout)"
fi
# png2amiga has third_party/ submodules (constixel, libwebp, ssimulacra2,
# vscode-amiga-debug). Make sure they're populated whether this is a fresh
# clone or an existing checkout that pre-dates the submodules.
echo "  Updating submodules (third_party/*)"
git -C "$PNG2AMIGA_DIR" submodule update --init --recursive

PNG2AMIGA_BIN="$PNG2AMIGA_DIR/build/png2amiga"
if [ ! -x "$PNG2AMIGA_BIN" ]; then
    echo "  Building png2amiga (CMake + GCC 15)"
    if ! command -v gcc-15 >/dev/null 2>&1 || ! command -v g++-15 >/dev/null 2>&1; then
        echo "ERROR: gcc-15 / g++-15 not on PATH."
        case "$PLATFORM" in
        macos)
            echo "  Try: brew install gcc"
            echo "  (Homebrew's gcc formula ships g++-15 at /opt/homebrew/bin/g++-15)"
            ;;
        debian)
            echo "  Try: sudo apt-get install gcc-15 g++-15"
            echo "  (may require backports / snapshot repo on Debian 13)"
            ;;
        esac
        exit 1
    fi
    cmake -B "$PNG2AMIGA_DIR/build" \
          -DCMAKE_C_COMPILER=gcc-15 \
          -DCMAKE_CXX_COMPILER=g++-15 \
          "$PNG2AMIGA_DIR"
    cmake --build "$PNG2AMIGA_DIR/build" --parallel
fi

if [ ! -x "$PNG2AMIGA_BIN" ]; then
    echo "ERROR: png2amiga binary missing after build: $PNG2AMIGA_BIN"
    exit 1
fi
echo "  png2amiga: $PNG2AMIGA_BIN"

echo
echo "==> Bootstrap complete."
echo "    Run ./build.sh next."
echo "    (set PNG2AMIGA=$PNG2AMIGA_BIN in your shell env if you used a non-default PNG2AMIGA_DIR)"
