"""
dtm.py  –  DTM products from the CSF ground surface (ground.csv).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 dtm.py \
      --input         <shared>/ground.csv \
      --out-colour    <shared>/dtm_colour.tif \
      --out-hillshade <shared>/dtm_hillshade.tif \
      --out-summary   <shared>/dtm_summary.json

ground.csv is the CSF-classified ground surface written by normalise
(columns: x, y, ground_z — absolute OSGB36 elevations). We interpolate it to a
continuous DTM inside the data footprint, then emit TWO single-band GeoTIFFs:

  1. dtm_colour.tif    float32 elevation (nodata -9999). The worker colour-ramps
                       it (terrain ramp) -> a colour elevation map.
  2. dtm_hillshade.tif uint8 multidirectional hillshade via gdaldem (nodata 0).
                       The worker grey-ramps it -> a grey hillshade.

gdaldem reads the GeoTIFF's north-up transform, so the hillshade is lit
correctly (no relief inversion); --multidirectional removes most of the
directional-lighting ambiguity. Drape the hillshade over the colour layer at
partial opacity for a shaded colour relief.
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
    ap.add_argument('--input',         required=True, help='Ground surface CSV (x,y,ground_z).')
    ap.add_argument('--out-colour',    required=True, help='Output float32 elevation GeoTIFF.')
    ap.add_argument('--out-hillshade', required=True, help='Output uint8 multidirectional hillshade GeoTIFF.')
    ap.add_argument('--out-summary',   required=True, help='Output summary .json.')
    ap.add_argument('--resolution',    type=float, default=0.5, help='Cell size (m). Default 0.5.')
    ap.add_argument('--crs',           default='EPSG:27700',
                    help='CRS of the ground X/Y (UK national grid). Worker reprojects to 3857.')
    ap.add_argument('--altitude',      type=float, default=45.0, help='Light altitude (deg). Default 45.')
    a = ap.parse_args()

    print(f"Reading ground surface {a.input} ...")
    g = np.loadtxt(a.input, delimiter=',', skiprows=1)
    if g.ndim == 1:
        g = g.reshape(1, -1)
    gx, gy, gz = g[:, 0], g[:, 1], g[:, 2]
    if len(gz) < 4:
        raise SystemExit("ground.csv has too few points to build a DTM.")
    print(f"  Ground points: {len(gz):,}")
    print(f"  Elevation range: {gz.min():.2f} -> {gz.max():.2f} m")

    res = a.resolution
    x_min, y_min, x_max, y_max = gx.min(), gy.min(), gx.max(), gy.max()
    ncols = int((x_max - x_min) / res) + 1
    nrows = int((y_max - y_min) / res) + 1
    print(f"  DTM grid: {nrows} x {ncols} px at {res} m")

    # North-up cell-centre coordinates (row 0 = y_max).
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

    out_colour = Path(a.out_colour)
    out_hillshade = Path(a.out_hillshade)
    out_colour.parent.mkdir(parents=True, exist_ok=True)

    print(f"Writing elevation GeoTIFF: {out_colour}")
    with rasterio.open(out_colour, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='float32', crs=a.crs, transform=transform,
                       nodata=NODATA, compress='deflate') as dst:
        dst.write(dtm_out, 1)

    # Multidirectional hillshade — gdaldem reads the north-up transform + nodata,
    # so lighting is correct (no inversion). Output reserves 0 for nodata.
    print(f"Running gdaldem hillshade (multidirectional): {out_hillshade}")
    subprocess.run(
        ['gdaldem', 'hillshade', str(out_colour), str(out_hillshade),
         '-multidirectional', '-compute_edges', '-alt', str(a.altitude),
         '-of', 'GTiff', '-co', 'COMPRESS=DEFLATE'],
        check=True,
    )

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
