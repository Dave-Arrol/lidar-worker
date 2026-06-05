"""
tree_density.py  –  Tree stem-density raster (trees per hectare) from tree-top points.

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 tree_density.py \
      --input       <shared>/treetops_std.csv \
      --out-tif     <shared>/tree_density.tif \
      --out-summary <shared>/tree_density_summary.json

Bins the tree-top XY positions onto a regular grid (default 10 m cells = 1/100 ha)
and writes a single-band GeoTIFF whose pixel value is the local stem density in
trees/ha. Empty cells are nodata (0) so they render transparent; the worker
colour-ramps populated cells green (sparse) -> red (dense).
"""

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

NODATA = 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Tree-tops CSV (tree_id,x,y,height_m).')
    ap.add_argument('--out-tif',     required=True, help='Output single-band density GeoTIFF (trees/ha).')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--cell',        type=float, default=10.0,
                    help='Grid cell size in metres. Default 10 m (1/100 ha).')
    ap.add_argument('--crs',         default='EPSG:27700',
                    help='CRS of the tree X/Y. Worker reprojects to 3857.')
    a = ap.parse_args()

    print(f"Reading tree tops {a.input} ...")
    xs, ys = [], []
    with open(a.input, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                xs.append(float(row['x'])); ys.append(float(row['y']))
            except (KeyError, ValueError):
                continue
    if len(xs) < 1:
        raise SystemExit("No tree positions found in CSV.")
    xs = np.asarray(xs); ys = np.asarray(ys)
    print(f"  Trees: {len(xs):,}")

    cell = a.cell
    cell_ha = (cell * cell) / 10_000.0          # 10 m cell -> 0.01 ha
    per_tree_density = 1.0 / cell_ha            # one tree in a 0.01 ha cell = 100 trees/ha

    x_min, x_max = xs.min(), xs.max()
    y_min, y_max = ys.min(), ys.max()
    ncols = int((x_max - x_min) / cell) + 1
    nrows = int((y_max - y_min) / cell) + 1
    print(f"  Grid: {nrows} x {ncols} @ {cell} m cells")

    counts = np.zeros((nrows, ncols), dtype=np.float32)
    col = np.clip(((xs - x_min) / cell).astype(int), 0, ncols - 1)
    row = np.clip(((y_max - ys) / cell).astype(int), 0, nrows - 1)
    np.add.at(counts, (row, col), 1.0)

    density = (counts * per_tree_density).astype(np.float32)   # trees/ha; 0 where empty (-> nodata)
    transform = from_origin(x_min, y_max, cell, cell)

    out_tif = Path(a.out_tif)
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, 'w', driver='GTiff', height=nrows, width=ncols,
                       count=1, dtype='float32', crs=a.crs, transform=transform,
                       nodata=NODATA, compress='deflate') as dst:
        dst.write(density, 1)
    print(f"Wrote density raster -> {out_tif}")

    populated = density[density > 0]
    summary = {
        'cell_size_m':        cell,
        'trees':              int(len(xs)),
        'max_density_per_ha': int(round(float(populated.max()))) if populated.size else 0,
        'mean_density_per_ha': int(round(float(populated.mean()))) if populated.size else 0,
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
