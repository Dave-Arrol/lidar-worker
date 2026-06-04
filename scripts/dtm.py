"""
dtm.py  –  DTM greyscale hillshade from the CSF ground surface (ground.csv).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 dtm.py \
      --input       <shared>/ground.csv \
      --out-tif     <shared>/dtm_hillshade.tif \
      --out-summary <shared>/dtm_summary.json

ground.csv is the CSF-classified ground surface written by normalise
(columns: x, y, ground_z — absolute OSGB36 elevations on a regular grid). We
interpolate it to a continuous DTM inside the data footprint, compute a
hillshade, and write a SINGLE-BAND uint8 GeoTIFF (nodata=0). The worker applies
a black->white ramp (because this output is tagged mode:'grey'), so it stays a
true grey hillshade with the area outside the ground footprint transparent.

Resolution defaults to ground.csv's own grid (0.5 m). CRS is assumed EPSG:27700
(the cloud's OSGB36 frame); the worker reprojects to EPSG:3857 for display.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from scipy.interpolate import griddata


def hillshade(dtm, res, azimuth=315.0, altitude=45.0):
    """Standard Horn-style hillshade. Returns shaded relief in [-1, 1]."""
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    dy, dx = np.gradient(dtm, res)                 # d/drow (north-down), d/dcol
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dy, dx)
    shaded = (np.sin(alt) * np.sin(slope) +
              np.cos(alt) * np.cos(slope) * np.cos(az - aspect))
    return shaded


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Ground surface CSV (x,y,ground_z).')
    ap.add_argument('--out-tif',     required=True, help='Output single-band hillshade GeoTIFF (uint8).')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--resolution',  type=float, default=0.5, help='Cell size (m). Default 0.5.')
    ap.add_argument('--crs',         default='EPSG:27700',
                    help='CRS of the ground X/Y (UK national grid). Worker reprojects to 3857.')
    ap.add_argument('--azimuth',     type=float, default=315.0, help='Light azimuth (deg). Default 315.')
    ap.add_argument('--altitude',    type=float, default=45.0,  help='Light altitude (deg). Default 45.')
    a = ap.parse_args()

    print(f"Reading ground surface {a.input} ...")
    g = np.loadtxt(a.input, delimiter=',', skiprows=1)
    if g.ndim == 1:
        g = g.reshape(1, -1)
    gx_pts, gy_pts, gz_pts = g[:, 0], g[:, 1], g[:, 2]
    if len(gz_pts) < 4:
        raise SystemExit("ground.csv has too few points to build a DTM.")
    print(f"  Ground points: {len(gz_pts):,}")
    print(f"  Elevation range: {gz_pts.min():.2f} -> {gz_pts.max():.2f} m")

    res = a.resolution
    x_min, y_min, x_max, y_max = gx_pts.min(), gy_pts.min(), gx_pts.max(), gy_pts.max()
    ncols = int((x_max - x_min) / res) + 1
    nrows = int((y_max - y_min) / res) + 1
    print(f"  DTM grid: {nrows} x {ncols} px at {res} m")

    # North-up cell-centre coordinates (row 0 = y_max).
    col_centres = x_min + (np.arange(ncols) + 0.5) * res
    row_centres = y_max - (np.arange(nrows) + 0.5) * res
    grid_x, grid_y = np.meshgrid(col_centres, row_centres)

    print("Interpolating ground surface (linear, footprint only)...")
    dtm = griddata((gx_pts, gy_pts), gz_pts, (grid_x, grid_y), method='linear')
    footprint = ~np.isnan(dtm)                     # cells inside the data hull
    if not footprint.any():
        raise SystemExit("DTM interpolation produced no valid cells.")

    # Fill gaps (nearest) only so the gradient is stable; masked back out after.
    if (~footprint).any():
        nearest = griddata((gx_pts, gy_pts), gz_pts, (grid_x, grid_y), method='nearest')
        dtm = np.where(footprint, dtm, nearest)

    print(f"Computing hillshade (az={a.azimuth}, alt={a.altitude})...")
    shaded = hillshade(dtm, res, a.azimuth, a.altitude)        # [-1, 1]
    hs = np.clip((shaded + 1.0) / 2.0, 0.0, 1.0) * 254.0 + 1.0  # -> [1, 255]
    hs = hs.astype(np.uint8)
    hs[~footprint] = 0                                          # 0 = nodata/transparent

    out_tif = Path(a.out_tif)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    transform = from_origin(x_min, y_max, res, res)
    with rasterio.open(out_tif, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='uint8', crs=a.crs, transform=transform,
                       nodata=0, compress='deflate') as dst:
        dst.write(hs, 1)
    print(f"DTM hillshade saved: {out_tif}")

    elev = dtm[footprint]
    summary = {
        'cell_size_m':     res,
        'elev_min_m':      round(float(elev.min()), 1),
        'elev_max_m':      round(float(elev.max()), 1),
        'elev_mean_m':     round(float(elev.mean()), 1),
        'footprint_cells': int(footprint.sum()),
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
