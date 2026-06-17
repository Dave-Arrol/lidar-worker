"""
copc_tiler.py  -  Memory-bounded spatial tiling over a COPC point cloud.

Shared helper for the full-density analysis stages (segmentation, DBH, ...) that
cannot be thinned: they need every point on every stem, so instead of loading the
whole cloud we process it tile by tile and merge.

Why COPC: a COPC is octree-indexed, so reading one tile's bounding box touches only
the overlapping nodes - memory per tile is bounded by the tile, not the file. A 1 GB
or a 1 TB cloud over the same footprint both run on a small container; the big one
just yields more tiles.

Typical use in a stage
----------------------
    from copc_tiler import ensure_copc, iter_tiles, suggest_tile_size

    copc = ensure_copc(args.input)            # builds <name>.copc.laz if needed
    ts   = suggest_tile_size(copc)            # ~target points per tile
    for t in iter_tiles(copc, tile_size=ts, buffer=5.0):
        x, y, z   = t["x"], t["y"], t["z"]    # all points in core + 5 m buffer
        core_mask = t["core_mask"]            # True where the point's XY is in THIS tile's core
        # ... run the stage's per-tile logic on (x, y, z) with buffer context ...
        # ... emit only results whose stem/treetop falls in the core (core_mask) ...

Seam handling: adjacent core cells exactly partition the extent (half-open cells,
membership decided by each point's own cell index, so no float-edge gaps or overlaps).
A point in the buffer of one tile is in the core of exactly one other tile, so emitting
results only for core points dedupes trees that straddle a boundary - provided each
stage keeps a tree when its *base/top* is in the core.

The registry spawns stage scripts WITHOUT the worker's PROJ/GDAL env (index.js builds
GEO_ENV only for its own untwine/pdal/copc_clip calls), so ensure_copc sets PROJ_DATA /
PROJ_LIB / GDAL_DATA itself before shelling out to untwine.
"""

import os
import sys
import subprocess
from pathlib import Path

import numpy as np
import laspy
from laspy.copc import Bounds


# ---------------------------------------------------------------------------
# COPC index (build with untwine if the input is a plain LAS/LAZ)
# ---------------------------------------------------------------------------
def geo_env():
    """Replicate the worker's GEO_ENV so untwine's PROJ can find its database.

    Stages are spawned without it (see index.js line ~510), so we scope these onto
    the untwine subprocess only - exactly as the worker does for its own calls.
    """
    prefix = os.environ.get("CONDA_PREFIX") or "/opt/pdal"
    env = dict(os.environ)
    env["PROJ_DATA"] = f"{prefix}/share/proj"
    env["PROJ_LIB"]  = f"{prefix}/share/proj"   # older PROJ reads PROJ_LIB; harmless on 9
    env["GDAL_DATA"] = f"{prefix}/share/gdal"
    return env


def ensure_copc(las_path, copc_path=None):
    """Return a COPC path for `las_path`, building one with untwine if needed.

    If `las_path` is already a `.copc.laz` it is returned unchanged. untwine is
    out-of-core (disk-bound), so indexing is memory-safe at any cloud size.
    """
    p = str(las_path)
    if p.lower().endswith(".copc.laz"):
        return p

    if copc_path is None:
        copc_path = str(Path(p).with_suffix("")) + ".copc.laz"
    if Path(copc_path).exists():
        print(f"[tiler] COPC index already present: {copc_path}", flush=True)
        return copc_path

    tmp = str(Path(copc_path).parent / ("untwine-" + Path(copc_path).stem))
    Path(tmp).mkdir(parents=True, exist_ok=True)
    print(f"[tiler] building COPC index with untwine: {copc_path}", flush=True)
    subprocess.run(
        ["untwine", "-i", p, "-o", copc_path, "--temp_dir", tmp],
        check=True, env=geo_env(),
    )
    if not Path(copc_path).exists():
        sys.exit(f"[tiler] untwine did not produce {copc_path}")
    return copc_path


# ---------------------------------------------------------------------------
# Tiling
# ---------------------------------------------------------------------------
def _grid_dims(mins, maxs, tile_size):
    x_min, y_min = float(mins[0]), float(mins[1])
    x_max, y_max = float(maxs[0]), float(maxs[1])
    ncols = max(1, int(np.ceil((x_max - x_min) / tile_size)))
    nrows = max(1, int(np.ceil((y_max - y_min) / tile_size)))
    return x_min, y_min, x_max, y_max, ncols, nrows


def tile_grid(mins, maxs, tile_size):
    """Yield (i, j, x0, y0, x1, y1) half-open core cells covering the XY extent."""
    x_min, y_min, x_max, y_max, ncols, nrows = _grid_dims(mins, maxs, tile_size)
    for j in range(nrows):
        for i in range(ncols):
            x0 = x_min + i * tile_size
            y0 = y_min + j * tile_size
            x1 = x_max if i == ncols - 1 else x0 + tile_size
            y1 = y_max if j == nrows - 1 else y0 + tile_size
            yield i, j, x0, y0, x1, y1


def suggest_tile_size(copc_path, target_points_per_tile=15_000_000,
                      lo=25.0, hi=500.0):
    """Pick a tile size so each tile holds ~target points, from the cloud's density.

    target 15M points ~= a couple of GB at peak per tile - comfortable on an 8 GiB
    container with the buffer on top. Clamped to [lo, hi] metres.
    """
    reader = laspy.CopcReader.open(str(copc_path))
    hdr = reader.header
    n = int(hdr.point_count)
    dx = float(hdr.maxs[0]) - float(hdr.mins[0])
    dy = float(hdr.maxs[1]) - float(hdr.mins[1])
    area = dx * dy
    if n <= 0 or area <= 0:
        return hi
    density = n / area                       # points per m^2
    ts = (target_points_per_tile / density) ** 0.5
    return float(min(max(ts, lo), hi))


def cloud_info(copc_path):
    """Return (point_count, (x_min,y_min,z_min), (x_max,y_max,z_max)) for logging."""
    reader = laspy.CopcReader.open(str(copc_path))
    h = reader.header
    return int(h.point_count), tuple(map(float, h.mins)), tuple(map(float, h.maxs))


def iter_tiles(copc_path, tile_size, buffer=5.0):
    """Open a COPC and yield one dict per non-empty tile.

    Yielded dict:
      i, j            tile column / row
      ncols, nrows    grid dimensions
      core            (x0, y0, x1, y1) half-open core cell
      points          ScaleAwarePointRecord for core + buffer (ALL dimensions)
      x, y, z         float64 arrays for those points
      core_mask       bool array; True where a point's XY is in THIS tile's core

    Emit a stage result only where core_mask is True (decided by the result's stem
    base / treetop), and seams dedupe automatically.
    """
    reader = laspy.CopcReader.open(str(copc_path))
    hdr = reader.header
    mins, maxs = hdr.mins, hdr.maxs
    z_lo, z_hi = float(mins[2]), float(maxs[2])
    x_min, y_min, x_max, y_max, ncols, nrows = _grid_dims(mins, maxs, tile_size)

    for i, j, x0, y0, x1, y1 in tile_grid(mins, maxs, tile_size):
        # pad z generously so the 3D node query never excludes valid points
        read_bounds = Bounds(
            mins=np.array([x0 - buffer, y0 - buffer, z_lo - 1.0]),
            maxs=np.array([x1 + buffer, y1 + buffer, z_hi + 1.0]),
        )
        pts = reader.query(bounds=read_bounds)
        if pts is None or len(pts) == 0:
            continue

        x = np.asarray(pts.x, dtype=np.float64)
        y = np.asarray(pts.y, dtype=np.float64)
        z = np.asarray(pts.z, dtype=np.float64)

        # COPC queries are node-granular and can spill past the box; clip to the
        # exact read window so the buffer width is honoured regardless of laspy.
        win = ((x >= x0 - buffer) & (x < x1 + buffer) &
               (y >= y0 - buffer) & (y < y1 + buffer))
        if not win.all():
            pts = pts[win]
            x, y, z = x[win], y[win], z[win]
        if len(pts) == 0:
            continue

        # Core membership by each point's OWN cell index -> exact partition, no
        # float-edge gaps or double-claims at seams.
        ci = np.clip(((x - x_min) / tile_size).astype(np.int64), 0, ncols - 1)
        cj = np.clip(((y - y_min) / tile_size).astype(np.int64), 0, nrows - 1)
        core_mask = (ci == i) & (cj == j)

        yield {
            "i": i, "j": j, "ncols": ncols, "nrows": nrows,
            "core": (x0, y0, x1, y1),
            "points": pts, "x": x, "y": y, "z": z,
            "core_mask": core_mask,
        }


# ---------------------------------------------------------------------------
# Self-test / debug CLI
# ---------------------------------------------------------------------------
def _selftest():
    """Verify the core cells partition the extent exactly (no gaps, no overlaps)."""
    mins, maxs, ts = (0.0, 0.0, 0.0), (100.0, 80.0, 10.0), 30.0
    _, _, _, _, ncols, nrows = _grid_dims(mins, maxs, ts)
    rng = np.random.default_rng(0)
    n = 50_000
    px = rng.uniform(0, 100, n)
    py = rng.uniform(0, 80, n)
    claims = np.zeros(n, dtype=np.int64)
    for i, j, *_ in tile_grid(mins, maxs, ts):
        ci = np.clip((px / ts).astype(np.int64), 0, ncols - 1)
        cj = np.clip((py / ts).astype(np.int64), 0, nrows - 1)
        claims += ((ci == i) & (cj == j)).astype(np.int64)
    ncells = len(list(tile_grid(mins, maxs, ts)))
    ok = bool((claims == 1).all())
    print(f"grid {ncols}x{nrows} = {ncells} cells | every point claimed exactly once: {ok}")
    print(f"claim histogram (should be all-ones): "
          f"min={claims.min()} max={claims.max()}")
    sys.exit(0 if ok else 1)


def _report(path):
    """Index `path` if needed and print tile counts for a chosen tile size."""
    copc = ensure_copc(path)
    n, mn, mx = cloud_info(copc)
    ts = suggest_tile_size(copc)
    print(f"points={n:,}  extent X[{mn[0]:.1f},{mx[0]:.1f}] Y[{mn[1]:.1f},{mx[1]:.1f}]")
    print(f"suggested tile_size={ts:.1f} m")
    tiles = 0
    pts_total = 0
    for t in iter_tiles(copc, tile_size=ts, buffer=5.0):
        tiles += 1
        pts_total += int(t["core_mask"].sum())
        print(f"  tile ({t['i']},{t['j']}) read={len(t['points']):,} "
              f"core={int(t['core_mask'].sum()):,}")
    print(f"non-empty tiles={tiles}  core points summed={pts_total:,} (should ~= {n:,})")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="COPC tiler self-test / report")
    ap.add_argument("--selftest", action="store_true",
                    help="Verify the tile partition logic (no COPC needed).")
    ap.add_argument("--report", metavar="LAS_OR_COPC",
                    help="Index if needed and print tile breakdown.")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif a.report:
        _report(a.report)
    else:
        ap.print_help()
