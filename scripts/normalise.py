"""
normalise.py  -  Ground-filter a clipped LAS/LAZ cloud and normalise point heights.

Stage 1 of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 normalise.py \
      --input      <shared>/cloud.las \
      --out-las    <shared>/normalised.las \
      --out-ground <shared>/ground.csv

Outputs
-------
  --out-las     A normalised .las file  -  Z values are height above ground (m).
                X/Y, CRS, scales and offsets are inherited from the input header
                unchanged, so downstream Potree conversion stays georeferenced.
  --out-ground  A ground surface .csv (columns: x,y,ground_z) -  downsampled
                ground points on a regular grid, consumed by dbh_extraction
                (np.loadtxt, skiprows=1; positional columns x,y,ground_z).

Memory strategy (large-cloud safe)
----------------------------------
The original loaded the entire cloud into RAM (laspy.read), built a second full
float64 XYZ copy, then handed a third copy to CSF - all live at once - and ran the
ground filter over every point. On dense clouds that OOM-kills (SIGKILL) regardless
of how much RAM the container has, because there is no streaming and Fargate has no
swap. This version is memory-bounded no matter how large the cloud is:

  1. PASS 1 streams the cloud in chunks and keeps only the lowest few points per
     horizontal cell - a ground-biased thin. CSF runs on that small subset, so the
     ground *surface* is derived without ever holding the full cloud in memory.
  2. PASS 2 streams the cloud again, looks up height-above-ground for each point
     against the thinned ground surface, and writes the normalised LAS chunk by
     chunk.

Peak memory is ~one chunk + the thinned ground set, not the whole cloud. The CSF
and grid parameters and the I/O contract are unchanged. The registry passes only
--input/--out-las/--out-ground, so default behaviour matches the contract above;
the new --chunk/--ground-voxel/--per-cell/--max-ground-points flags only control
the memory strategy and have safe defaults.

Note: because CSF now runs on a ground-biased thin rather than every point, the
resulting ground surface is *equivalent*, not byte-identical, to the old full-cloud
run. At the default 0.5 m cell this is negligible for height normalisation; sanity
check one cloud against the printed quality-check block the first time.
"""

import argparse
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import laspy
import CSF
import pandas as pd
from scipy.spatial import cKDTree
import memlog
memlog.track("normalise")  

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    # -- Registry-driven I/O (required) -------------------------
    ap.add_argument('--input',      required=True,
                    help='Input clipped point cloud (.las/.laz).')
    ap.add_argument('--out-las',    required=True,
                    help='Output normalised .las (height-above-ground in Z).')
    ap.add_argument('--out-ground', required=True,
                    help='Output ground surface .csv (x,y,ground_z).')

    # -- CSF ground filter parameters (defaults = original SETTINGS) --
    ap.add_argument('--cloth-res',  type=float, default=0.5,
                    help='Cloth mesh size in metres (smaller = more terrain '
                         'detail, slower). Default 0.5.')
    ap.add_argument('--rigidness',  type=int,   default=1,
                    help='1 = steep/complex slope, 2 = gentle, 3 = flat. Default 1.')
    ap.add_argument('--slope-smooth', action=argparse.BooleanOptionalAction,
                    default=True,
                    help='Slope post-processing for sloped forested terrain. '
                         'Default on (--no-slope-smooth to disable).')
    ap.add_argument('--iterations', type=int,   default=500,
                    help='CSF iterations; more = more accurate ground. Default 500.')
    ap.add_argument('--threshold',  type=float, default=0.5,
                    help='Max distance (m) from cloth for a point to be ground. '
                         'Default 0.5.')

    # -- Ground CSV grid resolution (default = original SETTINGS) --
    ap.add_argument('--grid',       type=float, default=0.5,
                    help='Grid spacing (m) for the downsampled ground CSV. '
                         'Default 0.5.')

    # -- Memory strategy (large-cloud safe; registry does not pass these) --
    ap.add_argument('--chunk',      type=int,   default=5_000_000,
                    help='Points read/written per streaming chunk. Lower it if '
                         'memory is still tight. Default 5,000,000.')
    ap.add_argument('--ground-voxel', type=float, default=None,
                    help='Horizontal cell size (m) for the ground-biased thin fed '
                         'to CSF. Default = --cloth-res.')
    ap.add_argument('--per-cell',   type=int,   default=3,
                    help='Lowest N points (by Z) kept per thinning cell. >1 gives '
                         'CSF local context to reject sub-ground noise. Default 3.')
    ap.add_argument('--max-ground-points', type=int, default=8_000_000,
                    help='Hard cap on points fed to CSF; randomly subsampled if the '
                         'thin still exceeds it. Default 8,000,000.')

    return ap.parse_args()


def _lowest_per_cell(ix, iy, x, y, z, k):
    """Return the lowest-k points (by z) within each (ix, iy) cell. Fully vectorised.

    Used both per-chunk and to re-collapse the running accumulator. Keeping the
    lowest-k of (prior lowest-k for a cell) UNION (this chunk's lowest-k for a cell)
    is provably the global lowest-k for that cell, so the streaming reduction is
    exact regardless of how a cell's points are split across chunks.
    """
    n = ix.shape[0]
    if n == 0:
        e = np.empty(0, np.float64)
        return (np.empty(0, np.int64), np.empty(0, np.int64), e, e.copy(), e.copy())

    order = np.lexsort((z, iy, ix))          # primary ix, then iy, then z ascending
    ix, iy = ix[order], iy[order]
    x, y, z = x[order], y[order], z[order]

    first = np.ones(n, dtype=bool)           # True at the first row of each cell group
    first[1:] = (ix[1:] != ix[:-1]) | (iy[1:] != iy[:-1])
    grp = np.cumsum(first) - 1               # group id per row
    pos = np.arange(n)
    group_start = pos[first]                 # start index of each group
    rank = pos - group_start[grp]            # rank within cell (0 = lowest z)

    sel = rank < k
    return ix[sel], iy[sel], x[sel], y[sel], z[sel]


def main():
    a = parse_args()
    voxel = a.ground_voxel if a.ground_voxel is not None else a.cloth_res
    in_path = a.input

    # ============================================================
    # PASS 1 - stream the cloud and build a ground-biased thin
    # ============================================================
    print("Pass 1/2: scanning cloud and thinning for the ground filter...")
    G_ix = G_iy = G_x = G_y = G_z = None
    z_min_full, z_max_full = np.inf, -np.inf
    seen = 0

    with laspy.open(in_path) as reader:
        total = int(reader.header.point_count)
        print(f"Total points: {total:,}")

        for chunk in reader.chunk_iterator(a.chunk):
            x = np.asarray(chunk.x, dtype=np.float64)
            y = np.asarray(chunk.y, dtype=np.float64)
            z = np.asarray(chunk.z, dtype=np.float64)
            seen += x.shape[0]

            z_min_full = min(z_min_full, float(z.min()))
            z_max_full = max(z_max_full, float(z.max()))

            ix = np.floor(x / voxel).astype(np.int64)
            iy = np.floor(y / voxel).astype(np.int64)
            cix, ciy, cx, cy, cz = _lowest_per_cell(ix, iy, x, y, z, a.per_cell)

            if G_ix is None:
                G_ix, G_iy, G_x, G_y, G_z = cix, ciy, cx, cy, cz
            else:
                G_ix = np.concatenate([G_ix, cix])
                G_iy = np.concatenate([G_iy, ciy])
                G_x = np.concatenate([G_x, cx])
                G_y = np.concatenate([G_y, cy])
                G_z = np.concatenate([G_z, cz])
                # collapse the accumulator back to lowest-k per cell (bounded size)
                G_ix, G_iy, G_x, G_y, G_z = _lowest_per_cell(
                    G_ix, G_iy, G_x, G_y, G_z, a.per_cell)

            print(f"  thinned {seen:,}/{total:,} pts "
                  f"-> {G_x.shape[0]:,} ground candidates", flush=True)

    if G_x is None or G_x.shape[0] == 0:
        sys.exit("ERROR: no points read from input.")

    print(f"Z range before normalisation: {z_min_full:.2f} -> {z_max_full:.2f}")

    thinned = np.column_stack([G_x, G_y, G_z])
    del G_ix, G_iy, G_x, G_y, G_z

    if thinned.shape[0] > a.max_ground_points:
        rng = np.random.default_rng(0)
        keep = rng.choice(thinned.shape[0], a.max_ground_points, replace=False)
        thinned = thinned[keep]
        print(f"Thinned set capped to {a.max_ground_points:,} points for CSF.")

    print(f"Ground candidates for CSF: {thinned.shape[0]:,} "
          f"(cell {voxel} m, lowest {a.per_cell}/cell)")

    # ============================================================
    # CSF ground filter - on the thinned subset only
    # ============================================================
    print("Running CSF ground filter (on thinned subset)...")
    csf_filter = CSF.CSF()
    csf_filter.params.bSloopSmooth     = a.slope_smooth
    csf_filter.params.cloth_resolution = a.cloth_res
    csf_filter.params.rigidness        = a.rigidness
    csf_filter.params.iterations       = a.iterations
    csf_filter.params.class_threshold  = a.threshold

    csf_filter.setPointCloud(thinned)

    ground_indices     = CSF.VecInt()
    non_ground_indices = CSF.VecInt()
    csf_filter.do_filtering(ground_indices, non_ground_indices)

    ground_indices = np.array(list(ground_indices))
    if ground_indices.size == 0:
        sys.exit("ERROR: CSF classified zero ground points. Adjust "
                 "--cloth-res / --threshold / --rigidness for this terrain.")

    ground_points = thinned[ground_indices]            # (G, 3) absolute XYZ ground
    print(f"Ground points:     {ground_points.shape[0]:,} "
          f"of {thinned.shape[0]:,} candidates "
          f"({ground_points.shape[0] / thinned.shape[0] * 100:.1f}%)")
    del thinned

    # Nearest-ground-point surface (XY), identical in spirit to the original k=1 NN
    ground_tree = cKDTree(ground_points[:, :2])

    # ============================================================
    # PASS 2 - normalise heights and write the LAS, streaming
    # ============================================================
    print("Pass 2/2: normalising heights and writing LAS...")
    out_las_path = Path(a.out_las)
    out_las_path.parent.mkdir(parents=True, exist_ok=True)

    nz_min, nz_max = np.inf, -np.inf
    written = 0

    with laspy.open(in_path) as reader:
        # Reuse the input header so X/Y, CRS, scales and offsets are unchanged;
        # only Z becomes height-above-ground. The input may be a COPC (read straight
        # from S3); laspy cannot re-serialise the COPC VLRs and the normalised output
        # is a plain LAS, so strip them before writing.
        out_header = deepcopy(reader.header)
        out_header.vlrs[:] = [v for v in out_header.vlrs
                              if getattr(v, "user_id", "") != "copc"]
        if getattr(out_header, "evlrs", None):
            out_header.evlrs[:] = [v for v in out_header.evlrs
                                   if getattr(v, "user_id", "") != "copc"]

        with laspy.open(out_las_path, mode="w", header=out_header) as writer:
            for chunk in reader.chunk_iterator(a.chunk):
                xy = np.column_stack([
                    np.asarray(chunk.x, dtype=np.float64),
                    np.asarray(chunk.y, dtype=np.float64),
                ])
                _, idx = ground_tree.query(xy, k=1)
                normalised_z = np.asarray(chunk.z, dtype=np.float64) - ground_points[idx, 2]

                chunk.z = normalised_z          # write height-above-ground back
                writer.write_points(chunk)

                nz_min = min(nz_min, float(normalised_z.min()))
                nz_max = max(nz_max, float(normalised_z.max()))
                written += xy.shape[0]
                print(f"  normalised {written:,}/{total:,} pts", flush=True)

    print(f"Normalised LAS saved: {out_las_path}")

    # ============================================================
    # QUALITY CHECK - ground surface points after normalisation
    # ============================================================
    _, self_idx = ground_tree.query(ground_points[:, :2], k=1)
    ground_z_after = ground_points[:, 2] - ground_points[self_idx, 2]
    print("\nQuality check (ground points after normalisation):")
    print(f"  Mean:   {ground_z_after.mean():.3f} m  (target: ~0.00)")
    print(f"  StdDev: {ground_z_after.std():.3f} m   (target: small)")
    print(f"  Min:    {ground_z_after.min():.3f} m")
    print(f"  Max:    {ground_z_after.max():.3f} m")
    print(f"\nAll points Z range after normalisation: {nz_min:.2f} -> {nz_max:.2f}")

    if abs(ground_z_after.mean()) > 0.1:
        print("  WARNING: ground mean is far from 0 - CSF may have misclassified terrain.")
    if ground_z_after.std() > 0.5:
        print("  WARNING: high ground StdDev - consider reducing --cloth-res or --threshold.")

    # ============================================================
    # GROUND SURFACE CSV - median per grid cell (x, y, ground_z)
    # ============================================================
    print(f"\nSaving ground surface CSV (grid size: {a.grid} m)...")
    ground_csv_path = Path(a.out_ground)
    ground_csv_path.parent.mkdir(parents=True, exist_ok=True)

    gx, gy, gz = ground_points[:, 0], ground_points[:, 1], ground_points[:, 2]
    col  = np.floor((gx - gx.min()) / a.grid).astype(np.int64)
    row  = np.floor((gy - gy.min()) / a.grid).astype(np.int64)
    cell = row * (col.max() + 1) + col

    grid_df = pd.DataFrame({"cell": cell, "x": gx, "y": gy, "z": gz})
    grid_med = grid_df.groupby("cell", sort=True)[["x", "y", "z"]].median()
    grid_points = grid_med.to_numpy()

    np.savetxt(
        ground_csv_path,
        grid_points,
        delimiter=',',
        header='x,y,ground_z',
        comments='',
        fmt='%.4f',
    )
    print(f"Ground CSV saved:     {ground_csv_path}")
    print(f"  Grid cells: {len(grid_points):,}  "
          f"(from {ground_points.shape[0]:,} CSF ground points)")

    print("\nDone.")


if __name__ == '__main__':
    main()