"""
slope.py  –  Ground slope raster (degrees) from the CSF ground surface (ground.csv).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 slope.py \
      --input       <shared>/ground.csv \
      --out-tif     <shared>/slope.tif \
      --out-summary <shared>/slope_summary.json

Builds the same continuous DTM as dtm.py (interpolated ground surface inside the
data footprint), then runs `gdaldem slope` to produce a single-band slope raster
in DEGREES (nodata -9999). The worker colour-ramps it green (flat) -> red (steep).
"""

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from scipy.interpolate import griddata

NODATA = -9999.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Ground surface CSV (x,y,ground_z).')
    ap.add_argument('--out-tif',     required=True, help='Output single-band slope GeoTIFF (degrees).')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--resolution',  type=float, default=0.5, help='Cell size (m). Default 0.5.')
    ap.add_argument('--crs',         default='EPSG:27700',
                    help='CRS of the ground X/Y (UK national grid). Worker reprojects to 3857.')
    a = ap.parse_args()

    print(f"Reading ground surface {a.input} ...")
    g = np.loadtxt(a.input, delimiter=',', skiprows=1)
    if g.ndim == 1:
        g = g.reshape(1, -1)
    gx, gy, gz = g[:, 0], g[:, 1], g[:, 2]
    if len(gz) < 4:
        raise SystemExit("ground.csv has too few points to build a DTM.")
    print(f"  Ground points: {len(gz):,}")

    res = a.resolution
    x_min, y_min, x_max, y_max = gx.min(), gy.min(), gx.max(), gy.max()
    ncols = int((x_max - x_min) / res) + 1
    nrows = int((y_max - y_min) / res) + 1
    print(f"  Grid: {nrows} x {ncols} px at {res} m")

    col_centres = x_min + (np.arange(ncols) + 0.5) * res
    row_centres = y_max - (np.arange(nrows) + 0.5) * res
    grid_x, grid_y = np.meshgrid(col_centres, row_centres)

    print("Interpolating ground surface (linear, footprint only)...")
    dtm = griddata((gx, gy), gz, (grid_x, grid_y), method='linear')
    footprint = ~np.isnan(dtm)
    if not footprint.any():
        raise SystemExit("DTM interpolation produced no valid cells.")
    if (~footprint).any():
        nearest = griddata((gx, gy), gz, (grid_x, grid_y), method='nearest')
        dtm = np.where(footprint, dtm, nearest)

    dtm_out = np.where(footprint, dtm, NODATA).astype(np.float32)
    transform = from_origin(x_min, y_max, res, res)

    work = Path(a.out_tif).parent
    elev_tif = work / 'slope_dtm_tmp.tif'
    with rasterio.open(elev_tif, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='float32', crs=a.crs, transform=transform,
                       nodata=NODATA, compress='deflate') as dst:
        dst.write(dtm_out, 1)

    out_tif = Path(a.out_tif)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    print(f"Running gdaldem slope -> {out_tif}")
    subprocess.run(
        ['gdaldem', 'slope', str(elev_tif), str(out_tif),
         '-compute_edges', '-of', 'GTiff', '-co', 'COMPRESS=DEFLATE'],
        check=True,
    )

    # Read slope back for the summary.
    with rasterio.open(out_tif) as src:
        sl = src.read(1).astype(float)
        nd = src.nodata
    valid = sl[(sl != nd) & np.isfinite(sl)] if nd is not None else sl[np.isfinite(sl)]
    summary = {
        'cell_size_m':   res,
        'mean_slope_deg': round(float(valid.mean()), 1) if valid.size else 0,
        'max_slope_deg':  round(float(valid.max()), 1)  if valid.size else 0,
        'steep_pct':      round(float((valid > 30).mean() * 100), 1) if valid.size else 0,
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
