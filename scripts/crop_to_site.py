#!/usr/bin/env python3
"""
crop_to_site.py — preflight guard for the Arrol LiDAR worker.

Run BEFORE normalise.py. Fixes the std::length_error crash: CSF builds a
cloth grid over the cloud's full XY extent, so a cloud with outlier points
or multi-origin merged blocks (588 km bbox -> 1.4e12 grid cells) explodes.

What it does, in two streamed passes (never holds the cloud in memory):
  Pass 1: coarse 2D histogram (default 100 m bins) of point density.
          Flood-fills from the densest cell to find the DOMINANT cluster
          (the actual site). Reports any secondary clusters it discards.
  Pass 2: writes only points inside the dominant cluster bbox (+ buffer)
          to --output. Header offsets/scales/format preserved.

Exit codes:
  0  cropped file written (or input already sane -> copied/symlinked)
  2  cropped extent STILL exceeds --max-extent  (something is deeply wrong)
  3  no dominant cluster found (empty / degenerate cloud)

Usage in the worker (index.js), before normalise:
  python3 /app/scripts/crop_to_site.py \
      --input  /tmp/analyse-XXXX/cloud.las \
      --output /tmp/analyse-XXXX/cloud_cropped.las
  ...then feed cloud_cropped.las to normalise.py.

Requires: laspy[lazrs] (already in the image — normalise streams LAZ),
numpy. No pyproj needed; this is pure coordinate-space.
"""

import argparse
import json
import shutil
import sys
from collections import deque

import numpy as np
import laspy

CHUNK = 5_000_000


def log(msg):
    print(f"[crop_to_site] {msg}", flush=True)


def pass1_histogram(path, bin_m):
    """Stream the file, build a sparse 2D density histogram keyed (ix, iy)."""
    counts = {}
    total = 0
    with laspy.open(path) as f:
        n = f.header.point_count
        log(f"pass 1: {n:,} points, {bin_m} m bins")
        for chunk in f.chunk_iterator(CHUNK):
            x = np.asarray(chunk.x)
            y = np.asarray(chunk.y)
            ix = np.floor(x / bin_m).astype(np.int64)
            iy = np.floor(y / bin_m).astype(np.int64)
            # pack pair into one int64 key for fast unique-count
            key = ix * 10_000_000 + iy
            uniq, cnt = np.unique(key, return_counts=True)
            for k, c in zip(uniq.tolist(), cnt.tolist()):
                counts[k] = counts.get(k, 0) + c
            total += len(x)
            if total % 100_000_000 < CHUNK:
                log(f"  ...pass 1 read {total:,} / {n:,}")
    return counts


def unpack(key):
    ix = key // 10_000_000
    iy = key - ix * 10_000_000
    # correct for negative iy after floor-div packing
    if iy > 5_000_000:
        iy -= 10_000_000
        ix += 1
    return ix, iy


def find_dominant_cluster(counts, bin_m, min_pts_per_cell):
    """Flood-fill 8-connected occupied cells starting from the densest one.
    Repeats to enumerate ALL clusters; returns them sorted by point count."""
    occupied = {unpack(k): c for k, c in counts.items() if c >= min_pts_per_cell}
    if not occupied:
        return []
    unvisited = set(occupied)
    clusters = []
    while unvisited:
        seed = max(unvisited, key=lambda c: occupied[c])
        q = deque([seed])
        unvisited.discard(seed)
        cells, pts = [], 0
        while q:
            cx, cy = q.popleft()
            cells.append((cx, cy))
            pts += occupied[(cx, cy)]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cx + dx, cy + dy)
                    if nb in unvisited:
                        unvisited.discard(nb)
                        q.append(nb)
        xs = [c[0] for c in cells]
        ys = [c[1] for c in cells]
        clusters.append({
            "points": pts,
            "cells": len(cells),
            "xmin": min(xs) * bin_m, "xmax": (max(xs) + 1) * bin_m,
            "ymin": min(ys) * bin_m, "ymax": (max(ys) + 1) * bin_m,
        })
    clusters.sort(key=lambda c: c["points"], reverse=True)
    return clusters


def pass2_write(src, dst, bbox, total_expected):
    xmin, xmax, ymin, ymax = bbox
    kept = 0
    read = 0
    with laspy.open(src) as f:
        header = laspy.LasHeader(version=f.header.version,
                                 point_format=f.header.point_format)
        header.offsets = f.header.offsets
        header.scales = f.header.scales
        # carry CRS/other VLRs across so downstream detect_crs still works
        header.vlrs = f.header.vlrs
        with laspy.open(dst, mode="w", header=header) as out:
            for chunk in f.chunk_iterator(CHUNK):
                x = np.asarray(chunk.x)
                y = np.asarray(chunk.y)
                mask = (x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax)
                if mask.any():
                    out.write_points(chunk[mask])
                    kept += int(mask.sum())
                read += len(x)
                if read % 100_000_000 < CHUNK:
                    log(f"  ...pass 2 read {read:,} / {total_expected:,} "
                        f"(kept {kept:,})")
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--bin", type=float, default=100.0,
                    help="histogram cell size, metres (default 100)")
    ap.add_argument("--min-density", type=float, default=0.05,
                    help="min pts/m^2 for a cell to count as occupied "
                         "(default 0.05 = 500 pts per 100 m cell)")
    ap.add_argument("--buffer", type=float, default=50.0,
                    help="metres added around the dominant cluster (default 50)")
    ap.add_argument("--max-extent", type=float, default=10_000.0,
                    help="hard ceiling on cropped extent, metres (default 10 km)")
    ap.add_argument("--sane-extent", type=float, default=5_000.0,
                    help="if the RAW header bbox is already under this, skip "
                         "cropping entirely and just copy (default 5 km)")
    args = ap.parse_args()

    # Fast path: header bbox already sane -> nothing to do.
    with laspy.open(args.input) as f:
        hx = f.header.maxs[0] - f.header.mins[0]
        hy = f.header.maxs[1] - f.header.mins[1]
        n = f.header.point_count
    log(f"header bbox extent: {hx:,.0f} m x {hy:,.0f} m ({n:,} points)")
    if hx <= args.sane_extent and hy <= args.sane_extent:
        log("extent already sane — copying through, no crop needed")
        shutil.copyfile(args.input, args.output)
        print(json.dumps({"cropped": False, "kept": n, "total": n}))
        return 0

    min_pts_per_cell = max(1, int(args.min_density * args.bin * args.bin))
    counts = pass1_histogram(args.input, args.bin)
    clusters = find_dominant_cluster(counts, args.bin, min_pts_per_cell)
    if not clusters:
        log("ERROR: no dense cluster found — cloud may be empty or degenerate")
        return 3

    dom = clusters[0]
    log(f"dominant cluster: {dom['points']:,} points over {dom['cells']} cells, "
        f"bbox X[{dom['xmin']:,.0f}, {dom['xmax']:,.0f}] "
        f"Y[{dom['ymin']:,.0f}, {dom['ymax']:,.0f}]")
    for i, c in enumerate(clusters[1:6], 1):
        log(f"  DISCARDING secondary cluster {i}: {c['points']:,} pts at "
            f"X[{c['xmin']:,.0f}, {c['xmax']:,.0f}] "
            f"Y[{c['ymin']:,.0f}, {c['ymax']:,.0f}]")
    if len(clusters) > 1 and clusters[1]["points"] > 0.25 * dom["points"]:
        log("WARNING: second cluster holds >25% of the dominant cluster's "
            "points — this file may genuinely contain TWO sites or a "
            "mis-registered block. Check which site this cloud belongs to.")

    ext_x = dom["xmax"] - dom["xmin"] + 2 * args.buffer
    ext_y = dom["ymax"] - dom["ymin"] + 2 * args.buffer
    if ext_x > args.max_extent or ext_y > args.max_extent:
        log(f"ERROR: dominant cluster is still {ext_x:,.0f} x {ext_y:,.0f} m "
            f"(> {args.max_extent:,.0f} m ceiling). Refusing — CSF would "
            "build an enormous cloth. Inspect this cloud manually.")
        return 2

    bbox = (dom["xmin"] - args.buffer, dom["xmax"] + args.buffer,
            dom["ymin"] - args.buffer, dom["ymax"] + args.buffer)
    kept = pass2_write(args.input, args.output, bbox, n)
    frac = kept / n if n else 0
    log(f"wrote {kept:,} / {n:,} points ({frac:.1%}) -> {args.output}")
    print(json.dumps({"cropped": True, "kept": kept, "total": n,
                      "bbox": bbox, "kept_fraction": round(frac, 4),
                      "discarded_clusters": len(clusters) - 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())