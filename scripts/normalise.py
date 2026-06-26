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
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
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

    # ── GROUND FILTERING WITH CSF ──────────────────────────────
    print("Running CSF ground filter...")
    csf_filter = CSF.CSF()

    csf_filter.params.bSloopSmooth     = a.slope_smooth
    csf_filter.params.cloth_resolution = a.cloth_res
    csf_filter.params.rigidness        = a.rigidness
    csf_filter.params.iterations       = a.iterations
    csf_filter.params.class_threshold  = a.threshold

    csf_filter.setPointCloud(points)

    ground_indices     = CSF.VecInt()
    non_ground_indices = CSF.VecInt()
    csf_filter.do_filtering(ground_indices, non_ground_indices)

    ground_indices     = np.array(list(ground_indices))
    non_ground_indices = np.array(list(non_ground_indices))

    if ground_indices.size == 0:
        sys.exit("ERROR: CSF classified zero ground points. Adjust "
                 "--cloth-res / --threshold / --rigidness for this terrain.")

    print(f"Ground points:     {len(ground_indices):,}")
    print(f"Vegetation points: {len(non_ground_indices):,}")
    print(f"Ground percentage: "
          f"{len(ground_indices) / len(points) * 100:.1f}%")

    # ── HEIGHT NORMALISATION ───────────────────────────────────
    print("Normalising height...")
    ground_points = points[ground_indices]   # shape (N, 3) — absolute XYZ

    ground_tree  = cKDTree(ground_points[:, :2])
    _, idx       = ground_tree.query(points[:, :2], k=1)
    normalised_z = points[:, 2] - ground_points[idx, 2]

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

    col     = ((ground_points[:, 0] - x_min) / a.grid).astype(int)
    row     = ((ground_points[:, 1] - y_min) / a.grid).astype(int)
    cell_id = row * (col.max() + 1) + col

    grid = {}
    for i, cid in enumerate(cell_id):
        if cid not in grid:
            grid[cid] = []
        grid[cid].append(ground_points[i])

    grid_points = []
    for pts_in_cell in grid.values():
        cell_arr = np.array(pts_in_cell)
        grid_points.append([
            float(np.median(cell_arr[:, 0])),
            float(np.median(cell_arr[:, 1])),
            float(np.median(cell_arr[:, 2])),
        ])

    grid_points = np.array(grid_points)

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
