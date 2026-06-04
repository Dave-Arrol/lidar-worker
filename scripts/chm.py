"""
chm.py  –  Canopy Height Model raster from a normalised LAS (max height per cell).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 chm.py \
      --input       <shared>/normalised.las \
      --out-tif     <shared>/chm.tif \
      --out-summary <shared>/chm_summary.json

Outputs a single-band float32 GeoTIFF (nodata=0). The worker applies a
green->white colour ramp, reprojects to EPSG:3857, COGs it, and registers it as
a site_layers map overlay.

The point-cloud X/Y are absolute OSGB36 eastings/northings (EPSG:27700) carried
through from the source header by normalise; the worker reprojects to web
mercator for display, so the raster is written in 27700 unless --crs overrides.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import laspy
import rasterio
from rasterio.transform import from_origin


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Normalised .las (height-above-ground).')
    ap.add_argument('--out-tif',     required=True, help='Output single-band CHM GeoTIFF.')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--resolution',  type=float, default=0.5, help='Cell size (m). Default 0.5.')
    ap.add_argument('--crs',         default='EPSG:27700',
                    help='CRS of the LAS X/Y (UK national grid). Worker reprojects to 3857.')
    a = ap.parse_args()

    print(f"Reading {a.input} ...")
    las = laspy.read(a.input)
    x = np.asarray(las.x, dtype=float)
    y = np.asarray(las.y, dtype=float)
    z = np.asarray(las.z, dtype=float)   # normalised height above ground
    res = a.resolution
    print(f"  Points: {len(x):,}")

    x_min, y_min, x_max, y_max = x.min(), y.min(), x.max(), y.max()
    ncols = int((x_max - x_min) / res) + 1
    nrows = int((y_max - y_min) / res) + 1
    print(f"  CHM grid: {nrows} x {ncols} px at {res} m")

    # Max height per cell. Vectorised np.maximum.at over an init-zero grid is
    # identical to "if z > chm[cell]: chm[cell] = z" (heights are >= ~0, so a
    # cell touched only by sub-zero noise stays 0 = nodata) but avoids a
    # 100M-iteration Python loop.
    cols = ((x - x_min) / res).astype(int)
    rows = ((y - y_min) / res).astype(int)
    chm = np.zeros((nrows, ncols), dtype=np.float32)
    np.maximum.at(chm, (rows, cols), z.astype(np.float32))

    chm = np.flipud(chm)  # north-up for GeoTIFF
    transform = from_origin(x_min, y_max, res, res)

    out_tif = Path(a.out_tif)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='float32', crs=a.crs, transform=transform,
                       nodata=0, compress='deflate') as dst:
        dst.write(chm, 1)
    print(f"CHM saved: {out_tif}")

    valid = chm[chm > 0]
    summary = {
        'cell_size_m':   res,
        'max_height_m':  round(float(valid.max()), 1)  if valid.size else 0,
        'mean_height_m': round(float(valid.mean()), 1) if valid.size else 0,
        'cover_cells':   int(valid.size),
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
