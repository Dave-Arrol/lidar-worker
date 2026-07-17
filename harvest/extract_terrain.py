r"""
extract_terrain.py - sample terrain + canopy features at stem coordinates.

Runs on Dave's machine (needs network-free local data only). Two sources,
use either or both; columns are suffixed by source so they can coexist:

  A) LiDAR cloud (DJI Terra LAZ/LAS)   -> slope/rough/canopy at ~2 m
  B) OS Terrain 5 DTM (.asc, EPSG:27700) -> slope/elev at 5 m (stage-1 twin)

Usage (PowerShell, quote spaced paths):
  pip install laspy[lazrs] pyproj numpy pandas

  # LiDAR mode (Loch Quiel: 3 UTM tiles, pass the folder or each file)
  python scripts/extract_terrain.py laz `
      --stems data/loch_quiel_stems.csv `
      --cloud "E:\DJI\Loch Quiel\lidars\terra_laz" `
      --crs EPSG:32630 `
      --out data/loch_quiel_terrain_laz.csv

  # OS Terrain 5 mode (folder of .asc tiles)
  python scripts/extract_terrain.py os5 `
      --stems data/loch_quiel_stems.csv `
      --dtm "E:\OS\Terrain5" `
      --out data/loch_quiel_terrain_os5.csv

Output: one row per stem (object_id, stem_key) +
  elev_m, slope_deg, aspect_northness, aspect_eastness,
  roughness_m (std of ground residuals), tpi_m (concavity, +ridge/-hollow),
  canopy_p95_m, point_density_per_m2            [laz mode only]
Features are NaN where the stem falls outside coverage.

Design notes:
- Ground surface: classification==2 points if present, else per-cell min-z on a
  2 m grid, 3x3 median-smoothed. Robust enough at stand scale for slope/rough.
- Chunked LAZ streaming (5M pts/chunk) so 2.2 GB clouds fit in memory.
- Stems arrive WGS84 (StemCoordinates); transformed with pyproj to cloud CRS.
- OS Terrain 5 tiles are ESRI ASCII grid, EPSG:27700; stems -> BNG.
- Pure numpy (no scipy): gradient slope, manual median filter.
"""
from __future__ import annotations
import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message="All-NaN slice")
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="Degrees of freedom")

try:
    from pyproj import Transformer
except ImportError:
    sys.exit("pip install pyproj")

GRID = 2.0          # LAZ ground grid (m)
NEIGH = 5           # window (cells) for roughness / TPI
CHUNK = 5_000_000


# ----------------------------------------------------------------- helpers
def _median3(a: np.ndarray) -> np.ndarray:
    """3x3 median filter, NaN-aware, edge-padded. Pure numpy."""
    p = np.pad(a, 1, mode="edge")
    stack = np.stack([p[i:i + a.shape[0], j:j + a.shape[1]]
                      for i in range(3) for j in range(3)])
    return np.nanmedian(stack, axis=0)


def _win_stat(a: np.ndarray, k: int, fn) -> np.ndarray:
    r = k // 2
    p = np.pad(a, r, mode="edge")
    stack = np.stack([p[i:i + a.shape[0], j:j + a.shape[1]]
                      for i in range(k) for j in range(k)])
    return fn(stack, axis=0)


def _slope_aspect(dtm: np.ndarray, cell: float):
    gy, gx = np.gradient(dtm, cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = np.arctan2(-gx, gy)          # 0 = north
    return slope, np.cos(aspect), np.sin(aspect)


def _bilinear(grid: np.ndarray, x0, y0, cell, xs, ys):
    """Sample grid (row 0 = min y) at coords; NaN outside."""
    fx = (xs - x0) / cell
    fy = (ys - y0) / cell
    out = np.full(len(xs), np.nan)
    ok = (fx >= 0) & (fy >= 0) & (fx <= grid.shape[1] - 1) & (fy <= grid.shape[0] - 1)
    if not ok.any():
        return out
    fx, fy = fx[ok], fy[ok]
    i0, j0 = np.floor(fy).astype(int), np.floor(fx).astype(int)
    i1, j1 = np.minimum(i0 + 1, grid.shape[0] - 1), np.minimum(j0 + 1, grid.shape[1] - 1)
    wy, wx = fy - i0, fx - j0
    v = (grid[i0, j0] * (1 - wx) * (1 - wy) + grid[i0, j1] * wx * (1 - wy)
         + grid[i1, j0] * (1 - wx) * wy + grid[i1, j1] * wx * wy)
    # fall back to nearest where bilinear hits a NaN neighbour
    nn = grid[np.round(fy).astype(int), np.round(fx).astype(int)]
    v = np.where(np.isnan(v), nn, v)
    out[ok] = v
    return out


def _load_stems(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    need = {"object_id", "stem_key", "lat", "lon"}
    missing = need - set(df.columns)
    if missing:
        sys.exit(f"stems file missing columns: {missing}")
    df = df.drop_duplicates(subset=["object_id", "stem_key"])
    return df[df.lat.notna() & df.lon.notna()].copy()


def _features_from_grids(minz, maxz, cnt, x0, y0, cell, xs, ys, laz=True):
    dtm = _median3(minz)
    slope, north, east = _slope_aspect(dtm, cell)
    rough = _win_stat(minz - dtm, NEIGH, np.nanstd)
    tpi = dtm - _win_stat(dtm, NEIGH, np.nanmean)

    out = {
        "elev_m": _bilinear(dtm, x0, y0, cell, xs, ys),
        "slope_deg": _bilinear(slope, x0, y0, cell, xs, ys),
        "aspect_northness": _bilinear(north, x0, y0, cell, xs, ys),
        "aspect_eastness": _bilinear(east, x0, y0, cell, xs, ys),
        "roughness_m": _bilinear(rough, x0, y0, cell, xs, ys),
        "tpi_m": _bilinear(tpi, x0, y0, cell, xs, ys),
    }
    if laz:
        canopy = np.where(np.isnan(maxz) | np.isnan(dtm), np.nan, maxz - dtm)
        dens = cnt / (cell * cell)
        out["canopy_p95_m"] = _bilinear(canopy, x0, y0, cell, xs, ys)
        out["point_density_per_m2"] = _bilinear(dens, x0, y0, cell, xs, ys)
    return out


# ----------------------------------------------------------------- LAZ mode
def run_laz(args):
    try:
        import laspy
    except ImportError:
        sys.exit("pip install laspy[lazrs]")

    stems = _load_stems(args.stems)
    tr = Transformer.from_crs("EPSG:4326", args.crs, always_xy=True)
    xs, ys = tr.transform(stems.lon.values, stems.lat.values)

    cloud = Path(args.cloud)
    files = sorted(cloud.glob("*.la[sz]")) if cloud.is_dir() else [cloud]
    if not files:
        sys.exit(f"no LAS/LAZ under {cloud}")
    print(f"{len(files)} cloud file(s); {len(stems)} stems")

    # grid extent = stem bbox + 100 m buffer (keeps arrays small, skips far tiles)
    x0 = np.floor(xs.min() - 100); x1 = np.ceil(xs.max() + 100)
    y0 = np.floor(ys.min() - 100); y1 = np.ceil(ys.max() + 100)
    nx = int((x1 - x0) / GRID) + 1
    ny = int((y1 - y0) / GRID) + 1
    print(f"grid {nx} x {ny} cells @ {GRID} m")

    minz = np.full((ny, nx), np.nan, np.float32)
    ground_minz = np.full((ny, nx), np.nan, np.float32)   # classification==2 only
    maxz = np.full((ny, nx), np.nan, np.float32)
    cnt = np.zeros((ny, nx), np.float32)
    n_ground = 0
    total = 0

    for f in files:
        with laspy.open(f) as fh:
            print(f"  {f.name}: {fh.header.point_count:,} pts")
            for ch in fh.chunk_iterator(CHUNK):
                x = np.asarray(ch.x); y = np.asarray(ch.y); z = np.asarray(ch.z, np.float32)
                m = (x >= x0) & (x < x1) & (y >= y0) & (y < y1)
                if not m.any():
                    continue
                x, y, z = x[m], y[m], z[m]
                cls = np.asarray(ch.classification)[m] if hasattr(ch, "classification") else None
                j = ((x - x0) / GRID).astype(np.int32)
                i = ((y - y0) / GRID).astype(np.int32)
                flat = i * nx + j
                total += len(z)

                # seed first-touch cells, then min/max (avoids NaN propagation)
                seed = np.isnan(minz.ravel()[flat])
                if seed.any():
                    minz.ravel()[flat[seed]] = z[seed]
                np.minimum.at(minz.ravel(), flat, z)
                seed = np.isnan(maxz.ravel()[flat])
                if seed.any():
                    maxz.ravel()[flat[seed]] = z[seed]
                np.maximum.at(maxz.ravel(), flat, z)
                np.add.at(cnt.ravel(), flat, 1)

                if cls is not None:
                    g = cls == 2
                    if g.any():
                        n_ground += int(g.sum())
                        fg = flat[g]; zg = z[g]
                        seed = np.isnan(ground_minz.ravel()[fg])
                        if seed.any():
                            ground_minz.ravel()[fg[seed]] = zg[seed]
                        np.minimum.at(ground_minz.ravel(), fg, zg)

    print(f"binned {total:,} pts in AOI; ground-classified: {n_ground:,}")
    if total == 0:
        sys.exit("no points fell inside the stem AOI - wrong --crs or wrong cloud?")

    # CROP to the populated region (+margin) before feature maths. The stem
    # bbox can span multiple coupes tens of km apart; windowed stats on the
    # full grid would need ~25x its size in RAM. Features only exist where
    # the cloud has points anyway.
    rows = np.where(cnt.any(axis=1))[0]
    cols = np.where(cnt.any(axis=0))[0]
    MARGIN = NEIGH  # cells
    r0 = max(rows[0] - MARGIN, 0); r1 = min(rows[-1] + MARGIN + 1, ny)
    c0 = max(cols[0] - MARGIN, 0); c1 = min(cols[-1] + MARGIN + 1, nx)
    minz = minz[r0:r1, c0:c1]
    ground_minz = ground_minz[r0:r1, c0:c1]
    maxz = maxz[r0:r1, c0:c1]
    cnt = cnt[r0:r1, c0:c1]
    x0 = x0 + c0 * GRID
    y0 = y0 + r0 * GRID
    print(f"cropped grid to populated extent: {minz.shape[1]} x {minz.shape[0]} cells")

    ground = ground_minz if n_ground > 0.01 * max(total, 1) else minz
    print("ground surface =", "classification==2" if ground is ground_minz else "per-cell min-z")

    feats = _features_from_grids(ground, maxz, cnt, x0, y0, GRID, xs, ys, laz=True)
    out = stems[["object_id", "stem_key"]].copy()
    for k, v in feats.items():
        out[k] = v
    cov = out.slope_deg.notna().mean()
    print(f"coverage: {cov:.1%} of stems inside cloud")
    out.to_csv(args.out, index=False)
    print("->", args.out)


# ----------------------------------------------------------- OS Terrain 5
def _read_asc(path_or_lines):
    if isinstance(path_or_lines, list):
        lines = path_or_lines
    else:
        with open(path_or_lines) as fh:
            lines = fh.readlines()
    hdr = {}
    n = 0
    for ln in lines[:8]:
        parts = ln.split()
        if len(parts) == 2 and parts[0].lower() in (
                "ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value"):
            hdr[parts[0].lower()] = float(parts[1]); n += 1
        else:
            break
    arr = np.loadtxt(lines[n:], dtype=np.float32)
    nod = hdr.get("nodata_value")
    if nod is not None:
        arr[arr == nod] = np.nan
    arr = arr[::-1]                       # row 0 = min y
    return hdr, arr


def _gather_tiles(root: Path):
    """Yield (name, lines) for every ASCII-grid tile under root.
    Handles both loose .asc files and the OS Terrain 5/50 distribution
    layout where each 10 km tile is a zip containing the .asc."""
    import io, zipfile
    for f in sorted(root.rglob("*.asc")):
        with open(f) as fh:
            yield f.name, fh.readlines()
    for zf in sorted(root.rglob("*.zip")):
        try:
            with zipfile.ZipFile(zf) as z:
                for n in z.namelist():
                    if n.lower().endswith(".asc"):
                        with z.open(n) as fh:
                            yield f"{zf.name}:{n}", io.TextIOWrapper(fh).readlines()
        except zipfile.BadZipFile:
            print(f"  skipping bad zip {zf.name}")


def run_os5(args):
    stems = _load_stems(args.stems)
    tr = Transformer.from_crs("EPSG:4326", "EPSG:27700", always_xy=True)
    xs, ys = tr.transform(stems.lon.values, stems.lat.values)

    tiles = list(_gather_tiles(Path(args.dtm)))
    if not tiles:
        sys.exit(f"no .asc tiles (loose or zipped) under {args.dtm}")
    print(f"{len(tiles)} DTM tiles; {len(stems)} stems")

    out = stems[["object_id", "stem_key"]].copy()
    cols = ["elev_m", "slope_deg", "aspect_northness", "aspect_eastness",
            "roughness_m", "tpi_m"]
    for c in cols:
        out[c] = np.nan

    for name, lines in tiles:
        hdr, arr = _read_asc(lines)
        cell = hdr["cellsize"]
        tx0, ty0 = hdr["xllcorner"], hdr["yllcorner"]
        tx1 = tx0 + hdr["ncols"] * cell
        ty1 = ty0 + hdr["nrows"] * cell
        m = (xs >= tx0) & (xs < tx1) & (ys >= ty0) & (ys < ty1)
        if not m.any():
            continue
        print(f"  {name}: {m.sum()} stems")
        feats = _features_from_grids(arr, None, None, tx0, ty0, cell,
                                     xs[m], ys[m], laz=False)
        idx = np.where(m)[0]
        for c in cols:
            out.iloc[idx, out.columns.get_loc(c)] = feats[c]

    cov = out.slope_deg.notna().mean()
    print(f"coverage: {cov:.1%} of stems on DTM tiles")
    out.to_csv(args.out, index=False)
    print("->", args.out)


# ------------------------------------------------------------------- main
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("laz", help="features from a DJI Terra LAZ/LAS cloud")
    a.add_argument("--stems", required=True)
    a.add_argument("--cloud", required=True, help="file or folder of .laz/.las")
    a.add_argument("--crs", default="EPSG:32630",
                   help="cloud CRS (EPSG:32630 UTM30N or EPSG:27700 BNG)")
    a.add_argument("--out", required=True)
    b = sub.add_parser("os5", help="features from OS Terrain 5/50 .asc tiles (loose or zipped)")
    b.add_argument("--stems", required=True)
    b.add_argument("--dtm", required=True, help="folder containing .asc tiles or OS tile zips (e.g. terr50_gagg_gb)")
    b.add_argument("--out", required=True)
    args = ap.parse_args()
    run_laz(args) if args.mode == "laz" else run_os5(args)
