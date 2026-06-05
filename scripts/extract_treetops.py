"""
extract_treetops.py  –  Detect tree tops from a normalised LAS file and export:
  1.  A .csv file  –  tree_id, x, y, height_m  (one row per tree)
  2.  A .las file  –  one point per tree top, coloured by tree_id,
                      with 'tree_id' scalar field for CloudCompare cross-reference

Detection logic mirrors segmentation_v2_radial.py exactly:
  - CHM built at CHM_RESOLUTION, smoothed with gaussian_filter(sigma=1.0)
  - Local maxima found with maximum_filter
  - NMS applied to suppress duplicates
  - True canopy height resolved via cKDTree radius query

Stage of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 extract_treetops.py \
      --input       <shared>/normalised.las \
      --out-csv     <shared>/treetops_std.csv \
      --out-las     <shared>/treetops_std.las \
      --out-summary <shared>/treetops_std_summary.json

The detection parameters are exposed as optional flags whose defaults match the
original hardcoded SETTINGS block exactly (and must match segmentation_v2 if you
want tree tops to align with the segmentation chain). The registry does not pass
them, so default behaviour is identical to the standalone script.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import laspy
import numpy as np
from rasterio.warp import transform as warp_transform
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.spatial import cKDTree


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Registry-driven I/O (required)
    ap.add_argument('--input',       required=True, help='Normalised .las (height-above-ground).')
    ap.add_argument('--out-csv',     required=True, help='Output tree-tops .csv (tree_id,x,y,height_m).')
    ap.add_argument('--out-las',     required=True, help='Output tree-tops .las (one point per tree, id-coloured).')
    ap.add_argument('--out-summary', required=True, help='Output summary .json (tree count + height stats).')
    ap.add_argument('--out-geojson', required=True, help='Output tree-tops GeoJSON (EPSG:4326) for the 2D map.')
    ap.add_argument('--crs',         default='EPSG:27700', help='CRS of the input X/Y. Default EPSG:27700.')
    # Detection parameters (defaults = original SETTINGS; must match segmentation_v2)
    ap.add_argument('--chm-resolution',  type=float, default=0.3,  help='Metres per CHM pixel. Default 0.3.')
    ap.add_argument('--gaussian-sigma',  type=float, default=1.0,  help='CHM smoothing sigma. Default 1.0.')
    ap.add_argument('--tree-top-radius', type=float, default=1.0,  help='Local-max search radius (m). Default 1.0.')
    ap.add_argument('--nms-distance',    type=float, default=1.0,  help='Min distance between kept tops (m). Default 1.0.')
    ap.add_argument('--hmin',            type=float, default=12.0, help='Ignore detections below this height (m). Default 12.0.')
    return ap.parse_args()


def build_chm(x, y, z, x_min, y_min, resolution):
    """Rasterise point cloud to a max-height CHM — identical to segmentation_v2_radial."""
    cols = ((x - x_min) / resolution).astype(int)
    rows = ((y - y_min) / resolution).astype(int)
    n_cols = cols.max() + 1
    n_rows = rows.max() + 1
    chm = np.zeros((n_rows, n_cols), dtype=np.float32)
    for i in range(len(x)):
        if z[i] > chm[rows[i], cols[i]]:
            chm[rows[i], cols[i]] = z[i]
    return chm, n_rows, n_cols


def detect_treetops(chm_smooth, x_min, y_min, resolution, hmin, tree_top_radius):
    """Local-max detection — identical to segmentation_v2_radial."""
    radius_pixels = max(1, int(tree_top_radius / resolution))
    local_max = maximum_filter(chm_smooth, size=radius_pixels * 2 + 1)
    mask = (chm_smooth == local_max) & (chm_smooth >= hmin)
    rows, cols = np.where(mask)
    ttx = cols * resolution + x_min
    tty = rows * resolution + y_min
    ttz = chm_smooth[rows, cols]
    return ttx, tty, ttz, rows, cols


def non_maximum_suppression(ttx, tty, ttz, rows, cols, min_distance):
    """NMS — identical to segmentation_v2_radial."""
    n = len(ttx)
    order = np.argsort(ttz)[::-1]
    keep = np.ones(n, dtype=bool)
    for i in range(n):
        if not keep[order[i]]:
            continue
        for j in range(i + 1, n):
            if not keep[order[j]]:
                continue
            dist = np.sqrt(
                (ttx[order[i]] - ttx[order[j]]) ** 2 +
                (tty[order[i]] - tty[order[j]]) ** 2
            )
            if dist < min_distance:
                keep[order[j]] = False
    return ttx[keep], tty[keep], ttz[keep], rows[keep], cols[keep]


def refine_treetop_heights(ttx, tty, x, y, z, spatial_index, chm_smooth,
                            rows, cols, resolution):
    """
    Replace CHM-smoothed heights with true point-cloud maxima within one CHM
    cell radius — identical to segmentation_v2_radial.refine_treetop_heights.

    Using cKDTree.query_ball_point (not a bounding-box loop) avoids picking up
    noise points at an oblique distance and is the reason this produces clean
    heights without floating outliers.
    """
    n = len(ttx)
    z_true = np.zeros(n)
    for i in range(n):
        idx = spatial_index.query_ball_point([ttx[i], tty[i]], r=resolution)
        z_true[i] = z[idx].max() if len(idx) > 0 else chm_smooth[rows[i], cols[i]]
    return z_true


def id_to_colour(ids):
    """
    Map integer IDs to visually distinct RGB uint16 using a golden-ratio hue
    cycle (full saturation/value).  Returns (red, green, blue) uint16 arrays.
    """
    gold = 0.6180339887498949
    hues = (ids.astype(float) * gold) % 1.0
    hi = (hues * 6.0).astype(int) % 6
    f  = hues * 6.0 - (hues * 6.0).astype(int)

    r = np.where(hi==0, 1., np.where(hi==1, 1.-f, np.where(hi==2, 0.,
        np.where(hi==3, 0., np.where(hi==4, f, 1.)))))
    g = np.where(hi==0, f,  np.where(hi==1, 1., np.where(hi==2, 1.,
        np.where(hi==3, 1.-f, np.where(hi==4, 0., 0.)))))
    b = np.where(hi==0, 0., np.where(hi==1, 0., np.where(hi==2, f,
        np.where(hi==3, 1., np.where(hi==4, 1., 1.-f)))))

    s = 65535
    return (r*s).astype(np.uint16), (g*s).astype(np.uint16), (b*s).astype(np.uint16)


def main():
    a = parse_args()
    input_file   = Path(a.input)
    csv_file     = Path(a.out_csv)
    las_file     = Path(a.out_las)
    summary_file = Path(a.out_summary)
    geojson_file = Path(a.out_geojson)

    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    csv_file.parent.mkdir(parents=True, exist_ok=True)
    las_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    geojson_file.parent.mkdir(parents=True, exist_ok=True)

    chm_res  = a.chm_resolution
    sigma    = a.gaussian_sigma
    radius_m = a.tree_top_radius
    nms_dist = a.nms_distance
    hmin     = a.hmin

    # ── Load ──────────────────────────────────────────────
    print(f"Reading: {input_file}")
    las = laspy.read(input_file)
    x   = np.array(las.x, dtype=float)
    y   = np.array(las.y, dtype=float)
    z   = np.array(las.z, dtype=float)
    print(f"  Points: {len(x):,}")
    print(f"  Z range: {z.min():.2f} m → {z.max():.2f} m")

    x_min, y_min = x.min(), y.min()
    x_max, y_max = x.max(), y.max()

    # ── CHM ───────────────────────────────────────────────
    print(f"Building CHM at {chm_res} m/px...")
    chm, n_rows, n_cols = build_chm(x, y, z, x_min, y_min, chm_res)
    print(f"  Grid: {n_rows} × {n_cols} px")

    print(f"Smoothing CHM (gaussian sigma={sigma})...")
    chm_smooth = gaussian_filter(chm.astype(np.float32), sigma=sigma)

    # ── Detect ────────────────────────────────────────────
    print(f"Detecting local maxima (radius={radius_m} m, hmin={hmin} m)...")
    ttx, tty, ttz, tt_rows, tt_cols = detect_treetops(
        chm_smooth, x_min, y_min, chm_res, hmin, radius_m
    )
    print(f"  Candidates before NMS: {len(ttx):,}")

    print(f"Applying NMS (min distance={nms_dist} m)...")
    ttx, tty, ttz, tt_rows, tt_cols = non_maximum_suppression(
        ttx, tty, ttz, tt_rows, tt_cols, nms_dist
    )
    print(f"  Tree tops after NMS:   {len(ttx):,}")

    if len(ttx) == 0:
        sys.exit(f"No tree tops detected above hmin={hmin} m. "
                 f"Lower --hmin for shorter vegetation or garden tests.")

    # ── True heights ──────────────────────────────────────
    print("Resolving true canopy heights via spatial index...")
    spatial_index = cKDTree(np.vstack([x, y]).T)
    z_true = refine_treetop_heights(
        ttx, tty, x, y, z, spatial_index, chm_smooth, tt_rows, tt_cols, chm_res
    )
    print(f"  Height range: {z_true.min():.1f} m → {z_true.max():.1f} m")

    # ── IDs ───────────────────────────────────────────────
    tree_ids = np.arange(1, len(ttx) + 1, dtype=np.int32)   # 1-based, matches segmentation

    # ── CSV ───────────────────────────────────────────────
    print(f"Writing CSV: {csv_file}")
    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["tree_id", "x", "y", "height_m"])
        for tid, tx, ty, th in zip(tree_ids, ttx, tty, z_true):
            writer.writerow([tid,
                             round(float(tx), 3),
                             round(float(ty), 3),
                             round(float(th), 3)])
    print(f"  {len(tree_ids):,} rows written")

    # ── ID LAS ────────────────────────────────────────────
    print(f"Writing ID LAS: {las_file}")
    header = laspy.LasHeader(point_format=7, version="1.4")
    header.offsets = np.array([float(np.floor(ttx.min())),
                               float(np.floor(tty.min())), 0.0])
    header.scales  = np.array([0.001, 0.001, 0.001])

    id_las = laspy.LasData(header=header)
    id_las.x = ttx
    id_las.y = tty
    id_las.z = z_true   # at actual canopy height — correct Z in CloudCompare

    r, g, b = id_to_colour(tree_ids)
    id_las.red   = r
    id_las.green = g
    id_las.blue  = b

    id_las.add_extra_dim(laspy.ExtraBytesParams(name="tree_id",         type=np.int32))
    id_las.add_extra_dim(laspy.ExtraBytesParams(name="canopy_height_m", type=np.float32))
    id_las.tree_id         = tree_ids
    id_las.canopy_height_m = z_true.astype(np.float32)
    id_las.write(las_file)
    print(f"  {len(tree_ids):,} points written")

    # ── Summary JSON (consumed by the worker -> stat cards) ──
    summary = {
        'trees':         int(len(tree_ids)),
        'height_min_m':  round(float(z_true.min()), 1),
        'height_max_m':  round(float(z_true.max()), 1),
        'height_mean_m': round(float(z_true.mean()), 1),
    }
    with open(summary_file, "w") as f:
        json.dump(summary, f)
    print(f"  Summary JSON:    {summary_file}")

    # ── GeoJSON (reprojected to EPSG:4326 for the 2D map) ──
    lons, lats = warp_transform(a.crs, 'EPSG:4326', list(map(float, ttx)), list(map(float, tty)))
    features = [{
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [round(float(lon), 7), round(float(lat), 7)]},
        'properties': {'tree_id': int(tid), 'height_m': round(float(h), 1)},
    } for tid, lon, lat, h in zip(tree_ids, lons, lats, z_true)]
    with open(geojson_file, "w") as f:
        json.dump({'type': 'FeatureCollection', 'features': features}, f)
    print(f"  GeoJSON ({len(features):,} pts): {geojson_file}")

    # ── Summary ───────────────────────────────────────────
    print("\n── Summary ──────────────────────────────────────────────")
    print(f"  Input:           {input_file.name}")
    print(f"  Tree tops:       {len(tree_ids):,}")
    print(f"  Height range:    {z_true.min():.1f} – {z_true.max():.1f} m")
    print(f"  Height mean:     {z_true.mean():.1f} m")
    print(f"  CSV:  {csv_file}")
    print(f"  LAS:  {las_file}")
    print("\nCloudCompare cross-reference:")
    print("  1. Load normalised LAS + _treetops_id.las together")
    print("  2. Select treetops cloud → set point size to 8–10")
    print("  3. Colour by scalar field 'tree_id'")
    print("  4. Click any point → Properties panel shows tree_id value")
    print("  5. Match that ID to the CSV row for XY + height")


if __name__ == "__main__":
    main()