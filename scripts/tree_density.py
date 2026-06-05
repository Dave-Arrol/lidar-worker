"""
tree_density.py  –  Tree stem-density as a hexagon-binned polygon layer (trees/ha).

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 tree_density.py \
      --input       <shared>/treetops_std.csv \
      --out-geojson <shared>/tree_density.geojson \
      --out-summary <shared>/tree_density_summary.json

Bins the tree-top XY positions onto a regular hexagonal grid (default ~100 m^2
cells = 1/100 ha) and writes a GeoJSON of hexagon polygons (EPSG:4326), each
carrying its stem density in trees/ha. Discrete hexes render as a flat-filled
choropleth on the 2D map rather than a smoothed heat-map raster. A normalised
'd_norm' (0..1 against the layer max) drives a consistent green->red fill.
"""

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
from rasterio.warp import transform as warp_transform


def axial_round(q, r):
    """Round fractional axial coords to the nearest hex (cube rounding)."""
    x, z = q, r
    y = -x - z
    rx, ry, rz = round(x), round(y), round(z)
    dx, dy, dz = abs(rx - x), abs(ry - y), abs(rz - z)
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return int(rx), int(rz)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input',       required=True, help='Tree-tops CSV (tree_id,x,y,height_m).')
    ap.add_argument('--out-geojson', required=True, help='Output hexagon density GeoJSON (EPSG:4326).')
    ap.add_argument('--out-summary', required=True, help='Output summary .json.')
    ap.add_argument('--hex-area',    type=float, default=100.0,
                    help='Hexagon area in m^2. Default 100 (1/100 ha).')
    ap.add_argument('--crs',         default='EPSG:27700', help='CRS of the tree X/Y.')
    a = ap.parse_args()

    print(f"Reading tree tops {a.input} ...")
    xs, ys = [], []
    with open(a.input, newline='') as f:
        for row in csv.DictReader(f):
            try:
                xs.append(float(row['x'])); ys.append(float(row['y']))
            except (KeyError, ValueError):
                continue
    if len(xs) < 1:
        raise SystemExit("No tree positions found in CSV.")
    xs = np.asarray(xs); ys = np.asarray(ys)
    print(f"  Trees: {len(xs):,}")

    # Hexagon geometry: pointy-topped, circumradius R for the requested cell area.
    hex_area = a.hex_area
    R = math.sqrt(hex_area / (1.5 * math.sqrt(3.0)))
    cell_ha = hex_area / 10_000.0
    per_tree_density = 1.0 / cell_ha
    print(f"  Hex circumradius {R:.2f} m  (area {hex_area:.0f} m^2 = {cell_ha:.3f} ha)")

    # Bin each tree to a hex (pixel -> axial -> cube round).
    counts = {}
    for x, y in zip(xs, ys):
        q = (math.sqrt(3.0) / 3.0 * x - 1.0 / 3.0 * y) / R
        r = (2.0 / 3.0 * y) / R
        key = axial_round(q, r)
        counts[key] = counts.get(key, 0) + 1

    densities = {k: v * per_tree_density for k, v in counts.items()}
    max_density = max(densities.values()) if densities else 0.0
    print(f"  Hexes: {len(counts):,}  max {max_density:.0f} trees/ha")

    # Build hexagon polygons (in source CRS), then reproject all vertices in one batch.
    corner_ang = [math.radians(60.0 * i - 30.0) for i in range(6)]
    hex_keys = list(counts.keys())
    src_x, src_y = [], []   # flattened vertex coords (7 per hex: 6 corners + closing)
    for (q, r) in hex_keys:
        cx = R * (math.sqrt(3.0) * q + math.sqrt(3.0) / 2.0 * r)
        cy = R * (1.5 * r)
        ring = [(cx + R * math.cos(t), cy + R * math.sin(t)) for t in corner_ang]
        ring.append(ring[0])
        for vx, vy in ring:
            src_x.append(vx); src_y.append(vy)

    lons, lats = warp_transform(a.crs, 'EPSG:4326', src_x, src_y)

    features = []
    for i, key in enumerate(hex_keys):
        base = i * 7
        coords = [[lons[base + j], lats[base + j]] for j in range(7)]
        dens = densities[key]
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Polygon', 'coordinates': [coords]},
            'properties': {
                'density': int(round(dens)),               # trees/ha (popup/label)
                'count':   int(counts[key]),
                'd_norm':  round(dens / max_density, 4) if max_density else 0.0,  # 0..1 for fill ramp
            },
        })

    out_geo = Path(a.out_geojson)
    out_geo.parent.mkdir(parents=True, exist_ok=True)
    with open(out_geo, 'w') as f:
        json.dump({'type': 'FeatureCollection', 'features': features}, f)
    print(f"Wrote {len(features)} hexes -> {out_geo}")

    vals = np.array(list(densities.values()), dtype=float)
    summary = {
        'hex_area_m2':         int(round(hex_area)),
        'trees':               int(len(xs)),
        'hexes':               int(len(counts)),
        'max_density_per_ha':  int(round(max_density)),
        'mean_density_per_ha': int(round(float(vals.mean()))) if vals.size else 0,
    }
    with open(a.out_summary, 'w') as f:
        json.dump(summary, f)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
