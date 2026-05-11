#!/usr/bin/env python3
"""Access helper for tools/pristine_cache.pkl. Lazily-loaded; thin wrapper.

Usage:
    from pristine_cache import cache
    room = cache.room(rid)              # dict with clut, trns, costumes, ...
    home_room = cache.cost_home(cost_id)
    disk_n = cache.disk(rid)

The cache is built by tools/build_pristine_cache.py from amiga-data/.
If the cache file doesn't exist OR is older than monkey2.000, it's
rebuilt automatically on first access.
"""

import os
import pickle
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


CACHE_PATH = f'{REPO_ROOT}/tools/pristine_cache.pkl'
AMIGA_INDEX = f'{REPO_ROOT}/amiga-data/monkey2.000'


class _Cache:
    def __init__(self):
        self._data = None

    def _load(self):
        if self._data is not None:
            return
        if (not os.path.exists(CACHE_PATH)
                or (os.path.exists(AMIGA_INDEX)
                    and os.path.getmtime(AMIGA_INDEX) > os.path.getmtime(CACHE_PATH))):
            import build_pristine_cache

            sys.stderr.write("[pristine_cache] rebuilding from amiga-data/...\n")
            build_pristine_cache.build_cache()
        with open(CACHE_PATH, 'rb') as f:
            self._data = pickle.load(f)
        if self._data.get('version') != 1:
            raise RuntimeError(
                f"pristine_cache.pkl version {self._data.get('version')} "
                f"unsupported; rebuild via tools/build_pristine_cache.py")

    @property
    def data(self):
        self._load()
        return self._data

    def room(self, room_id):
        """Return the per-room dict, or None if unknown."""
        return self.data['rooms'].get(int(room_id))

    def cost_home(self, cost_id):
        """Return home_room for the costume, or None."""
        return self.data['dcos'].get(int(cost_id))

    def disk(self, room_id):
        """Return the disk number that hosts the given room, or None."""
        return self.data['droo'].get(int(room_id))

    def all_costumes_drawn_in(self, room_id):
        """Convenience: every costume hosted in this room's LFLF, with cid+pos."""
        room = self.room(room_id)
        if room is None:
            return []
        return [(pos, c['cost_id'], c) for pos, c in enumerate(room['costumes'])]


# Module-level singleton
cache = _Cache()
