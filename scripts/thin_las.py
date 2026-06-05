"""
thin_las.py  –  Decimate a dense LAS to a viewer-friendly size for Potree.

The dense cloud is needed for the analysis flow (normalise/treetops/DBH), but the
3D viewer only needs enough points to look right. This produces a thinned copy
dedicated to the PotreeConverter route, leaving the dense LAS untouched.

  python3 thin_las.py --input dense.las --output view.las --max-points 30000000

Streaming + chunked: reads and writes in fixed-size chunks, so peak memory stays
bounded regardless of how large/dense the input is. Random proportional sampling
keeps the natural density pattern (canopy stays denser than gaps). All point
attributes — including RGB — are preserved.
"""

import argparse

import numpy as np
import laspy


def main():
    ap = argparse.ArgumentParser(description="Thin a LAS for the Potree viewer.")
    ap.add_argument('--input',      required=True, help='Dense input LAS/LAZ.')
    ap.add_argument('--output',     required=True, help='Thinned output LAS.')
    ap.add_argument('--max-points', type=int, default=30_000_000,
                    help='Target maximum point count for the viewer. Default 30M.')
    ap.add_argument('--chunk',      type=int, default=5_000_000,
                    help='Points read/written per chunk. Default 5M.')
    a = ap.parse_args()

    rng = np.random.default_rng(42)

    with laspy.open(a.input) as reader:
        total = reader.header.point_count
        frac = 1.0 if total <= a.max_points else a.max_points / float(total)
        print(f"input {total:,} pts -> target {a.max_points:,} (keep fraction {frac:.4f})")

        with laspy.open(a.output, mode='w', header=reader.header) as writer:
            kept = 0
            for chunk in reader.chunk_iterator(a.chunk):
                if frac >= 1.0:
                    writer.write_points(chunk)
                    kept += len(chunk)
                else:
                    mask = rng.random(len(chunk)) < frac
                    sub = chunk[mask]
                    if len(sub):
                        writer.write_points(sub)
                    kept += len(sub)

    print(f"wrote {kept:,} pts -> {a.output}")


if __name__ == '__main__':
    main()
