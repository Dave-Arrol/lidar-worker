#!/usr/bin/env python3
"""os_tiles_for_stems.py - list the OS 10 km grid tiles covering a stems file's
bounding box, so the harvest worker fetches only the handful of OS Terrain 50
tiles it needs (not the whole ~2,858-tile GB set) before terrain enrichment.

Reads a stems CSV (lat, lon in WGS84), transforms to BNG (EPSG:27700), pads the
bbox, and prints the covering 10 km OS grid references, lowercase, space-joined:
e.g. "ny90 nz00". These match the leading token of OS Terrain 50 tile filenames
(ny90_OST50GRID_*.zip / NY90.asc), so the worker can grep the S3 listing for them.
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd

_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"  # OS grid omits 'I'


def _square(e100: int, n100: int) -> str:
    # Standard OS National Grid two-letter code from 100 km indices (valid GB-wide).
    l1 = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    l2 = (19 - n100) * 5 % 25 + e100 % 5
    return _LETTERS[int(l1)] + _LETTERS[int(l2)]


def tile_ref(E: float, N: float) -> str:
    e100 = int(E // 100000)
    n100 = int(N // 100000)
    e10 = int((E % 100000) // 10000)
    n10 = int((N % 100000) // 10000)
    return f"{_square(e100, n100)}{e10}{n10}".lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stems", required=True)
    ap.add_argument("--pad", type=float, default=200.0, help="bbox pad, metres")
    args = ap.parse_args()

    df = pd.read_csv(args.stems)
    lat = pd.to_numeric(df.get("lat"), errors="coerce")
    lon = pd.to_numeric(df.get("lon"), errors="coerce")
    ok = lat.notna() & lon.notna()
    if not ok.any():
        return  # no coordinates -> no tiles (quote falls back to terrain-blind)

    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    xs, ys = tr.transform(lon[ok].to_numpy(), lat[ok].to_numpy())
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)

    minE, maxE = xs.min() - args.pad, xs.max() + args.pad
    minN, maxN = ys.min() - args.pad, ys.max() + args.pad

    refs = set()
    e = (int(minE) // 10000) * 10000
    while e <= maxE:
        n = (int(minN) // 10000) * 10000
        while n <= maxN:
            if 0 <= e < 700000 and 0 <= n < 1300000:
                refs.add(tile_ref(e, n))
            n += 10000
        e += 10000

    print(" ".join(sorted(refs)))


if __name__ == "__main__":
    main()
