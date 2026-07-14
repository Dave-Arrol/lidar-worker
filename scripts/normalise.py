"""
normalise.py  –  Ground-filter a clipped LAS/LAZ cloud and normalise point heights.

Stage 1 of the Arrol algorithmic pipeline. Invoked by the worker registry as:

  python3 normalise.py \
      --input      <shared>/cloud.las \
      --out-las    <shared>/normalised.las \
      --out-ground <shared>/ground.csv

Outputs
-------
  --out-las     A normalised .las file  –  Z values are height above ground (m).
                X/Y, CRS, scales and offsets are inherited from the input header
                unchanged, so downstream conversion stays georeferenced.
  --out-ground  A ground surface .csv (columns: x,y,ground_z) –  downsampled
                ground points on a regular grid, consumed by dbh_extraction
                (np.loadtxt, skiprows=1; positional columns x,y,ground_z).

The CSF and grid parameters are exposed as optional flags whose defaults match
the original hardcoded SETTINGS block exactly. The registry does not pass them,
so default behaviour is byte-identical to the standalone script.

Streaming I/O
-------------
The cloud is read and written in CHUNKS (laspy.chunk_iterator / LasWriter),
never a single laspy.read()/write() of the whole point block. A whole-file read
of a very large LAZ (~1e9 points, tens of GB decompressed) overflows the LAZ
backend's 32-bit buffer offset: every point past the overflow boundary decodes
from the wrong place in memory, producing impossible negative coordinates and
scrambled axes, which then crashes CSF with a multi-kilometre bounding box.
Chunked reads keep each decode buffer small and are the only path proven correct
on billion-point Terra exports (a chunked min/max scan of such a cloud returns
values exactly matching its header; a whole-file read does not). The write is
chunked for the same reason, replacing only Z per chunk with the normalised
height. out_header scales/offsets MUST match the source so each chunk's existing
integer X/Y decode identically on read-back.

CSF 32-bit ceiling
------------------
The CSF binding's C++ side indexes with 32-bit ints. Past ~715.8M points
(2^31 / 3 coordinate values) the arithmetic wraps: coordinates scramble
between axes, the computed bbox explodes to hundreds of km, and the cloth
grid allocation throws std::length_error (observed on a clean 1.1e9-point
cloud whose true extent was 1.7 km). Ground classification gains nothing
from hundreds of points/m^2, so clouds above --csf-max-points are stride-
subsampled FOR CSF ONLY: ground is classified on the subset, then the FULL
cloud is height-normalised against that ground surface. Every point is
preserved in the output — segmentation and DBH ring-fitting downstream see
full density. Clouds under the ceiling behave byte-identically to before.
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import laspy
import CSF
from scipy.spatial import cKDTree


# Points per chunk for streamed read/write. 20M keeps each LAZ decode/encode
# buffer well under the 2^31-byte overflow boundary while staying I/O-efficient.
CHUNK = 20_000_000


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    # ── Registry-driven I/O (required) ─────────────────────────
    ap.add_argument('--input',      required=True,
                    help='Input clipped point cloud (.las/.laz).')
    ap.add_argument('--out-las',    required=True,
                    help='Output normalised .las (height-above-ground in Z).')
    ap.add_argument('--out-ground', required=True,
                    help='Output ground surface .csv (x,y,ground_z).')

    # ── CSF ground filter parameters (defaults = original SETTINGS) ──
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

    # ── CSF input ceiling (32-bit guard) ───────────────────────
    ap.add_argument('--csf-max-points', type=int, default=400_000_000,
                    help='Max points fed to CSF. The CSF C++ core wraps 32-bit '
                         'indices past ~715.8M points, scrambling coordinates; '
                         'larger clouds are stride-subsampled for ground '
                         'classification only. The full cloud is still height-'
                         'normalised and written out. Default 400M (safety '
                         'margin under the 2^31/3 hard ceiling).')

    # ── Ground CSV grid resolution (default = original SETTINGS) ──
    ap.add_argument('--grid',       type=float, default=0.5,
                    help='Grid spacing (m) for the downsampled ground CSV. '
                         'Default 0.5.')

    return ap.parse_args()


def main():
    a = parse_args()

    # ── READ (streamed) ────────────────────────────────────────
    # Stream the cloud in chunks into one preallocated array. A whole-file
    # laspy.read() overflows the LAZ backend on billion-point clouds and
    # manufactures corrupt coordinates; chunked reads do not. Capture the source
    # header fields here (version / point format / scales / offsets / CRS) — the
    # file is reopened for the streamed write later, after the source `with`
    # block has closed.
    print("Reading file (streamed)...")
    with laspy.open(a.input) as reader:
        hdr         = reader.header
        n_points    = hdr.point_count
        src_version = hdr.version
        src_pf      = hdr.point_format
        src_scales  = np.array(hdr.scales,  dtype=np.float64)
        src_offsets = np.array(hdr.offsets, dtype=np.float64)
        try:
            src_crs = hdr.parse_crs()
        except Exception as exc:
            print(f"  (could not read source CRS, continuing without it: {exc})")
            src_crs = None

        points = np.empty((n_points, 3), dtype=np.float64)
        filled = 0
        for chunk in reader.chunk_iterator(CHUNK):
            m = len(chunk.x)
            points[filled:filled + m, 0] = chunk.x
            points[filled:filled + m, 1] = chunk.y
            points[filled:filled + m, 2] = chunk.z
            filled += m
            print(f"  ...read {filled:,} / {n_points:,} points", flush=True)

    if filled != n_points:
        sys.exit(f"ERROR: read {filled:,} points but the header declared "
                 f"{n_points:,}; the input may be truncated.")

    print(f"Total points: {len(points):,}")
    print(f"Z range before normalisation: "
          f"{points[:, 2].min():.2f} -> {points[:, 2].max():.2f}")

    # ── CSF INPUT GUARD (32-bit ceiling) ───────────────────────
    # See module docstring: CSF scrambles coordinates past ~715.8M points.
    # Stride-subsample for classification only; acquisition order sweeps the
    # site, so a stride is spatially even. The FULL cloud is normalised below.
    n = len(points)
    if n > a.csf_max_points:
        step = int(np.ceil(n / a.csf_max_points))
        sub_idx = np.arange(0, n, step, dtype=np.int64)
        csf_points = np.ascontiguousarray(points[sub_idx])
        print(f"Cloud exceeds CSF ceiling ({n:,} > {a.csf_max_points:,}): "
              f"classifying ground on a 1-in-{step} stride subset "
              f"({len(sub_idx):,} points). Full cloud is preserved in the output.")
    else:
        sub_idx    = None
        csf_points = points
    n_classified = len(csf_points)

    # ── GROUND FILTERING WITH CSF ──────────────────────────────
    print("Running CSF ground filter...")
    csf_filter = CSF.CSF()

    csf_filter.params.bSloopSmooth     = a.slope_smooth
    csf_filter.params.cloth_resolution = a.cloth_res
    csf_filter.params.rigidness        = a.rigidness
    csf_filter.params.iterations       = a.iterations
    csf_filter.params.class_threshold  = a.threshold

    csf_filter.setPointCloud(csf_points)

    ground_indices     = CSF.VecInt()
    non_ground_indices = CSF.VecInt()
    csf_filter.do_filtering(ground_indices, non_ground_indices)

    # np.fromiter avoids materialising a giant Python list (the old
    # np.array(list(...)) costs ~28 bytes/int and minutes on 1e8 indices).
    n_veg          = len(non_ground_indices)
    ground_indices = np.fromiter(ground_indices, dtype=np.int64,
                                 count=len(ground_indices))

    if ground_indices.size == 0:
        sys.exit("ERROR: CSF classified zero ground points. Adjust "
                 "--cloth-res / --threshold / --rigidness for this terrain.")

    # Map subset-relative indices back to the full cloud.
    if sub_idx is not None:
        ground_indices = sub_idx[ground_indices]
        del csf_points, sub_idx   # free the subset copy (~10 GB on 1e9 clouds)

    print(f"Ground points:     {len(ground_indices):,}")
    print(f"Vegetation points: {n_veg:,}"
          + (" (of CSF subset)" if n > a.csf_max_points else ""))
    print(f"Ground percentage: "
          f"{len(ground_indices) / n_classified * 100:.1f}% of classified points")

    # ── HEIGHT NORMALISATION ───────────────────────────────────
    print("Normalising height...")
    ground_points = points[ground_indices]   # shape (N, 3) — absolute XYZ

    # Chunked query bounds transient memory (a single 1e9-point query would
    # allocate ~18 GB of distance+index scratch); workers=-1 uses every vCPU.
    ground_tree  = cKDTree(ground_points[:, :2])
    normalised_z = np.empty(n, dtype=np.float64)
    Q = 50_000_000
    for s in range(0, n, Q):
        e = min(s + Q, n)
        _, idx = ground_tree.query(points[s:e, :2], k=1, workers=-1)
        normalised_z[s:e] = points[s:e, 2] - ground_points[idx, 2]
        if n > Q:
            print(f"  ...normalised {e:,} / {n:,} points", flush=True)

    # ── QUALITY CHECK ──────────────────────────────────────────
    ground_z_after = normalised_z[ground_indices]
    print(f"\nQuality check (ground points after normalisation):")
    print(f"  Mean:   {ground_z_after.mean():.3f} m  (target: ~0.00)")
    print(f"  StdDev: {ground_z_after.std():.3f} m   (target: small)")
    print(f"  Min:    {ground_z_after.min():.3f} m")
    print(f"  Max:    {ground_z_after.max():.3f} m")
    print(f"\nAll points Z range after normalisation: "
          f"{normalised_z.min():.2f} -> {normalised_z.max():.2f}")

    if abs(ground_z_after.mean()) > 0.1:
        print("  WARNING: ground mean is far from 0 — CSF may have misclassified terrain.")
    if ground_z_after.std() > 0.5:
        print("  WARNING: high ground StdDev — consider reducing --cloth-res or --threshold.")

    # ── SAVE NORMALISED LAS (streamed) ─────────────────────────
    # Build a *clean* plain-LAS header (same point format / scales / offsets / CRS
    # as the source, no source VLRs) and stream the points back out chunk by
    # chunk, replacing only Z with the normalised height. Streaming the write,
    # like the read, avoids the whole-file backend overflow on huge clouds.
    # out_header scales/offsets MUST equal the source so each chunk's existing
    # integer X/Y (and the re-encoded Z) decode correctly on read-back. Re-reading
    # the source here (rather than holding every attribute in RAM) keeps the
    # working set down and preserves intensity/returns/classification untouched.
    print("\nSaving normalised LAS (streamed)...")
    out_las_path = Path(a.out_las)
    out_las_path.parent.mkdir(parents=True, exist_ok=True)

    out_header = laspy.LasHeader(version=src_version, point_format=src_pf)
    out_header.scales  = src_scales
    out_header.offsets = src_offsets
    # Carry the CRS across without dragging source VLRs along.
    if src_crs is not None:
        try:
            out_header.add_crs(src_crs)
        except Exception as exc:
            print(f"  (could not copy CRS, continuing without it: {exc})")

    written = 0
    with laspy.open(a.input) as reader, \
         laspy.open(str(out_las_path), mode='w', header=out_header) as writer:
        for chunk in reader.chunk_iterator(CHUNK):
            m = len(chunk.x)
            chunk.z = normalised_z[written:written + m]
            writer.write_points(chunk)
            written += m
            print(f"  ...wrote {written:,} / {n_points:,} points", flush=True)

    if written != len(normalised_z):
        sys.exit(f"ERROR: wrote {written:,} points but expected "
                 f"{len(normalised_z):,}; the output is inconsistent.")

    # Read-back sanity check: confirm the point block on disk matches the header,
    # so a malformed or truncated write fails *here* with a clear message instead
    # of three stages later in chm.py.
    with laspy.open(out_las_path) as _f:
        _h          = _f.header
        _need_bytes = _h.point_count * _h.point_format.size
        _have_bytes = os.path.getsize(out_las_path) - _h.offset_to_point_data
        if _have_bytes < _need_bytes:
            sys.exit(f"ERROR: normalised LAS is truncated - point block needs "
                     f"{_need_bytes:,} bytes but only {_have_bytes:,} present after "
                     f"the header. The disk may have filled during write.")
        if _h.point_count != len(normalised_z):
            sys.exit(f"ERROR: normalised LAS point count {_h.point_count:,} != "
                     f"expected {len(normalised_z):,}; the output header is inconsistent.")
    print(f"Normalised LAS saved: {out_las_path}  ({len(normalised_z):,} points, verified)")

    # ── SAVE GROUND SURFACE CSV ────────────────────────────────
    # Downsample ground points to a regular grid (median per cell).
    print(f"\nSaving ground surface CSV (grid size: {a.grid} m)...")
    ground_csv_path = Path(a.out_ground)
    ground_csv_path.parent.mkdir(parents=True, exist_ok=True)

    x_min = ground_points[:, 0].min()
    y_min = ground_points[:, 1].min()

    col     = ((ground_points[:, 0] - x_min) / a.grid).astype(np.int64)
    row     = ((ground_points[:, 1] - y_min) / a.grid).astype(np.int64)
    cell_id = row * (col.max() + 1) + col

    # Vectorised median-per-cell (the previous per-point Python loop ran for
    # hours once ground points reached 1e8). sort=False keeps first-seen cell
    # order, matching the old dict-insertion output ordering.
    df = pd.DataFrame({'cid': cell_id,
                       'x': ground_points[:, 0],
                       'y': ground_points[:, 1],
                       'z': ground_points[:, 2]})
    grid_points = (df.groupby('cid', sort=False)[['x', 'y', 'z']]
                     .median().to_numpy())

    np.savetxt(
        ground_csv_path,
        grid_points,
        delimiter=',',
        header='x,y,ground_z',
        comments='',
        fmt='%.4f'
    )
    print(f"Ground CSV saved:     {ground_csv_path}")
    print(f"  Grid cells: {len(grid_points):,}  "
          f"(from {len(ground_points):,} CSF ground points)")

    print(f"\nDone.")


if __name__ == '__main__':
    main()