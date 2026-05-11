#!/bin/bash
# regen_globals.sh — regenerate the global costumes and refresh the
# quality preview images, in one shot.
#
# Usage:
#   tools/regen_globals.sh                          # extras + group/global previews
#   tools/regen_globals.sh shore campfire           # + reinject these rooms + regen
#                                                   #   their preview/quality/*.png
#   tools/regen_globals.sh --with-guybrush          # + rerun encode_global_guybrush.py
#   tools/regen_globals.sh --all-rooms              # reinject every room in
#                                                   #   preview/intermediates/ and regen
#
# Env:
#   PNG2AMIGA=/path/to/png2amiga         (default: ~/png2amiga/build/png2amiga)
#   BEST=0|1                             (default: 0 — fast iteration)

set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

export PNG2AMIGA="${PNG2AMIGA:-$HOME/png2amiga/build/png2amiga}"
export BEST="${BEST:-0}"

if [ ! -x "$PNG2AMIGA" ]; then
    echo "[error] PNG2AMIGA=$PNG2AMIGA not executable" >&2
    exit 1
fi

with_guybrush=0
all_rooms=0
rooms=()

for arg in "$@"; do
    case "$arg" in
        --with-guybrush) with_guybrush=1 ;;
        --all-rooms)     all_rooms=1 ;;
        --*)             echo "[error] unknown flag: $arg" >&2; exit 2 ;;
        *)               rooms+=("$arg") ;;
    esac
done

if [ "$all_rooms" = "1" ]; then
    if [ -d preview/intermediates ]; then
        while IFS= read -r d; do
            rooms+=("$(basename "$d")")
        done < <(find preview/intermediates -mindepth 1 -maxdepth 1 -type d | sort)
    fi
fi

echo "PNG2AMIGA=$PNG2AMIGA  BEST=$BEST  rooms=${rooms[*]:-(none)}"

echo "== check_group_clashes.py =="
python3 tools/check_group_clashes.py

if [ "$with_guybrush" = "1" ]; then
    echo "== encode_global_guybrush.py =="
    python3 tools/encode_global_guybrush.py
fi

echo "== encode_global_extras.py =="
python3 tools/encode_global_extras.py

echo "== preview_groups.py (groups_compare.png) =="
python3 tools/preview_groups.py

echo "== build_quality_preview.py --globals =="
python3 tools/build_quality_preview.py --globals

for r in "${rooms[@]}"; do
    echo "== inject_room.py $r =="
    python3 tools/inject_room.py "$r"
    echo "== build_quality_preview.py $r =="
    python3 tools/build_quality_preview.py "$r"
done

echo
echo "Done. Outputs:"
echo "  preview/global_extras/groups_compare.png"
echo "  preview/quality/global-*.png"
if [ "${#rooms[@]}" -gt 0 ]; then
    for r in "${rooms[@]}"; do
        f=$(ls preview/quality/*"-${r}.png" 2>/dev/null | head -1)
        [ -n "$f" ] && echo "  $f"
    done
fi
