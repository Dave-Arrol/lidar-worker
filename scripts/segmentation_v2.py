#!/usr/bin/env python3
"""
segmentation_v2_radial.py
=========================
Stage-1 of the segmentation pipeline: treetop detection, watershed tree
assignment, and lower-stem candidate cloud export for downstream DBH
extraction.

Purpose
-------
Detects treetops, assigns tree IDs via watershed segmentation, and exports a
candidate lower-stem point cloud for `dbh_extraction_v2.py`. Does NOT produce
a final DBH estimate.

A provisional stem seed (centre + radius) is estimated per tree from lower-stem
slice fits. This seed is *load-bearing*: it drives a soft radial consistency
filter that suppresses obvious branch clutter before the cloud is passed
downstream. The word "diagnostic" in earlier versions was misleading — the seed
affects the output point cloud and is documented as such here.

Note for dbh_extraction_v2.py
------------------------------
The candidate LAS delivered by this script has already had the radial filter
applied. dbh_extraction_v2.py should not assume it is receiving an unfiltered
stem cloud. Per-tree filter statistics (points removed, retention fraction) are
written to the segment summary CSV to support auditing of compounded filtering
effects.

Key differences from segmentation.py
--------------------------------------
1. Keeps treetop detection + watershed tree assignment.
2. Keeps lower-stem candidate masking, density filtering, and segment scoring.
3. Removes hard cylinder trimming as an authoritative step.
4. Removes final DBH embedding in the candidate LAS.
5. Adds a per-tree segment summary CSV with QC metrics, provisional seed info,
   and per-tree radial filter statistics.
6. Applies a soft radial filter within accepted trees with full per-tree
   logging of points removed.
7. Pipeline stages are refactored into separate functions for maintainability.

Inputs:
  data/hopetoun/raw/hopetoun_normalised_dense_pl1.las

Outputs:
  results/hopetoun/algorithmic/hopetoun_tree_candidates.las
  results/hopetoun/algorithmic/hopetoun_treetops.las
  results/hopetoun/algorithmic/hopetoun_treetops.csv
  results/hopetoun/algorithmic/hopetoun_treetops_topview.png
  results/hopetoun/algorithmic/hopetoun_tree_candidates_diagnostic.las
  results/hopetoun/algorithmic/hopetoun_segment_summary.csv
"""

import argparse
import csv
import colorsys
from pathlib import Path

import laspy
import matplotlib
matplotlib.use('Agg')   # Headless-safe: no display required
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.optimize import least_squares
from scipy.spatial import cKDTree
from skimage.segmentation import watershed
from sklearn.decomposition import PCA

# ── PATHS (CLI) ───────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]

_ap = argparse.ArgumentParser(description="Crown segmentation + stem-candidate export.")
_ap.add_argument('--input',            required=True, help='Normalised LAS (height-above-ground).')
_ap.add_argument('--out-candidates',   required=True, help='Output stem-candidate LAS.')
_ap.add_argument('--out-treetops-las', required=True, help='Output tree-top pole LAS.')
_ap.add_argument('--out-treetops-csv', required=True, help='Output tree-tops CSV.')
_ap.add_argument('--out-summary-csv',  required=True, help='Output per-tree segment summary CSV.')
_args = _ap.parse_args()

INPUT_FILE          = Path(_args.input)
OUTPUT_FILE         = Path(_args.out_candidates)
TREETOPS_FILE       = Path(_args.out_treetops_las)
TREETOPS_CSV        = Path(_args.out_treetops_csv)
PLOT_OUTPUT         = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + '_topview.png')
SEGMENT_SUMMARY_CSV = Path(_args.out_summary_csv)

# ── SETTINGS ──────────────────────────────────────────────

# CHM + treetop detection
STEM_MIN_H      = 0.5
STEM_MAX_H      = 4.0
CHM_RESOLUTION  = 0.3
TREE_TOP_RADIUS = 0.8
NMS_DISTANCE    = 0.8
HMIN            = 12.0
MIN_POINTS      = 20

# Provisional stem seed
# Used to drive the radial filter. Not merely diagnostic — see module docstring.
TRUNK_SLICE_Z_MIN = 0.5
TRUNK_SLICE_Z_MAX = 4.0
TRUNK_SLICE_STEP  = 0.2
DBH_MIN_POINTS    = 10
DBH_MAX_RADIUS    = 0.6
DBH_MIN_RADIUS    = 0.03
TRUNK_MIN_SLICES  = 3

# Density filter
DENSITY_RADIUS         = 0.12
DENSITY_MIN_NEIGHBOURS = 3

# Radial consistency filter
# Retains points within [seed_r * R_MIN_MULT, seed_r * R_MAX_MULT] of the
# provisional seed centre. R_MIN_MULT=0.01 is effectively zero (nothing is
# filtered inward). The meaningful constraint is the upper bound: R_MAX_MULT=1.40
# retains points up to 40% beyond the provisional radius.
#
# Empirical basis for R_MAX_MULT=1.40: not yet validated against ground truth.
# Revisit once dbh_extraction_v2.py results are available. If DBH estimates
# are systematically low, consider increasing this value or disabling the filter.
#
# Applied only when the provisional seed is reliable (seed_status == 'seed_ok').
# If a tree's seed is unreliable, its points are passed through unfiltered.
APPLY_RADIAL_FILTER    = True
RADIAL_R_MIN_MULT      = 0.01
RADIAL_R_MAX_MULT      = 1.40
RADIAL_MIN_POINTS_KEEP = 10   # safeguard: skip filter if fewer points would remain

# Segment scoring / QC
PCA_MIN_POINTS         = 30
PCA_MIN_LINEARITY      = 0.45
# Verticality is logged but not used to reject segments at this stage.
# Rejection here is intentionally permissive — recall matters more than
# precision for a candidate-generation stage.
PCA_MIN_VERTICALITY    = 0.55
SLICE_THICKNESS        = 0.5
MIN_SLICES             = 3
MAX_CENTROID_DRIFT     = 0.25
MAX_SINGLE_DRIFT       = 0.50
MIN_FAILURES_TO_REJECT = 3

# Treetop pole export
POLE_Z_STEP   = 0.5
POLE_RADIUS   = 0.05
POLE_DISC_PTS = 8

# ──────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════
# CHM + TREETOP DETECTION
# ═══════════════════════════════════════════════════════════

def build_chm(x, y, z, x_min, y_min, resolution):
    """Rasterise point cloud to a max-height CHM."""
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
    """Return raw treetop pixel locations and approximate heights."""
    radius_pixels = max(1, int(tree_top_radius / resolution))
    local_max = maximum_filter(chm_smooth, size=radius_pixels * 2 + 1)
    mask = (chm_smooth == local_max) & (chm_smooth >= hmin)

    rows, cols = np.where(mask)
    ttx = cols * resolution + x_min
    tty = rows * resolution + y_min
    ttz = chm_smooth[rows, cols]
    return ttx, tty, ttz, rows, cols


def non_maximum_suppression(ttx, tty, ttz, rows, cols, min_distance):
    """Suppress weaker treetop candidates within `min_distance` of a taller one."""
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


def refine_treetop_heights(ttx, tty, x, y, z, spatial_index, chm_smooth, rows, cols, resolution):
    """
    Replace CHM-smoothed peak heights with true point-cloud maxima within one
    CHM cell of each treetop.
    """
    n = len(ttx)
    z_true = np.zeros(n)
    for i in range(n):
        idx = spatial_index.query_ball_point([ttx[i], tty[i]], r=resolution)
        z_true[i] = z[idx].max() if len(idx) > 0 else chm_smooth[rows[i], cols[i]]
    return z_true


# ═══════════════════════════════════════════════════════════
# STEM POINT EXTRACTION + TREE ID ASSIGNMENT
# ═══════════════════════════════════════════════════════════

def extract_stem_candidates(x, y, z, z_min, z_max):
    """Return points within the lower-stem height band."""
    mask = (z >= z_min) & (z <= z_max)
    return x[mask], y[mask], z[mask], mask


def assign_tree_ids_from_watershed(
    stem_x, stem_y, chm_watershed, x_min, y_min, resolution, n_cols, n_rows
):
    """Look up each stem point's tree ID from the watershed label image."""
    stem_cols = np.clip(((stem_x - x_min) / resolution).astype(int), 0, n_cols - 1)
    stem_rows = np.clip(((stem_y - y_min) / resolution).astype(int), 0, n_rows - 1)
    return chm_watershed[stem_rows, stem_cols]


def apply_min_points_filter(tree_ids, min_points):
    """Zero-out tree IDs whose point count falls below `min_points`."""
    ids_out = tree_ids.copy()
    unique, counts = np.unique(ids_out, return_counts=True)
    small = unique[counts < min_points]
    ids_out[np.isin(ids_out, small)] = 0
    return ids_out


def apply_density_filter(stem_x, stem_y, stem_z, tree_ids, radius, min_neighbours):
    """
    Remove isolated points with fewer than `min_neighbours` within `radius`.
    Returns filtered arrays and the boolean mask (relative to input arrays).
    """
    stem_xy = np.vstack([stem_x, stem_y]).T
    tree_2d = cKDTree(stem_xy)
    counts = np.array(
        tree_2d.query_ball_point(stem_xy, r=radius, return_length=True)
    ) - 1
    mask = counts >= min_neighbours
    return stem_x[mask], stem_y[mask], stem_z[mask], tree_ids[mask], mask


# ═══════════════════════════════════════════════════════════
# PROVISIONAL STEM SEED ESTIMATION
# ═══════════════════════════════════════════════════════════

def _fit_circle_to_slice(points_2d):
    """
    Least-squares circle fit for a 2D slice.
    Returns (cx, cy, r, cost) or None if fit fails or radius is out of range.
    """
    if len(points_2d) < DBH_MIN_POINTS:
        return None

    def residuals(params, pts):
        cx, cy, r = params
        return np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2) - r

    cx0, cy0 = points_2d.mean(axis=0)
    r0 = max(np.median(np.sqrt(
        (points_2d[:, 0] - cx0) ** 2 + (points_2d[:, 1] - cy0) ** 2
    )), 0.05)

    try:
        result = least_squares(residuals, [cx0, cy0, r0], args=(points_2d,))
        cx, cy, r = result.x
        r = abs(r)
        if not (DBH_MIN_RADIUS < r < DBH_MAX_RADIUS):
            return None
        return cx, cy, r, float(result.cost)
    except Exception:
        return None


def estimate_provisional_stem_seed(tree_pts):
    """
    Estimate a provisional lower-stem centre (cx, cy) and radius (r) by
    fitting circles to horizontal slices and taking the median across slices.

    This seed is load-bearing: it drives the radial consistency filter that
    modifies the point cloud passed to dbh_extraction_v2.py. If the seed is
    unreliable (seed_status != 'seed_ok'), the radial filter is skipped for
    that tree and its points are passed through unmodified.

    Returns a dict with keys:
      seed_status, seed_cx, seed_cy, seed_r_m, seed_n_slices, seed_mean_cost
    """
    if len(tree_pts) == 0:
        return {
            'seed_status': 'empty_tree',
            'seed_cx': np.nan, 'seed_cy': np.nan,
            'seed_r_m': np.nan, 'seed_n_slices': 0, 'seed_mean_cost': np.nan
        }

    z = tree_pts[:, 2]
    slice_data = []

    for z_low in np.arange(TRUNK_SLICE_Z_MIN, TRUNK_SLICE_Z_MAX, TRUNK_SLICE_STEP):
        z_high = z_low + TRUNK_SLICE_STEP
        mask = (z >= z_low) & (z < z_high)
        if mask.sum() < DBH_MIN_POINTS:
            continue
        fit = _fit_circle_to_slice(tree_pts[mask, :2])
        if fit is None:
            continue
        cx, cy, r, cost = fit
        slice_data.append({'cx': float(cx), 'cy': float(cy),
                           'r': float(r), 'cost': float(cost)})

    if len(slice_data) < TRUNK_MIN_SLICES:
        return {
            'seed_status': 'insufficient_seed_slices',
            'seed_cx': np.nan, 'seed_cy': np.nan,
            'seed_r_m': np.nan,
            'seed_n_slices': len(slice_data),
            'seed_mean_cost': np.nan
        }

    return {
        'seed_status': 'seed_ok',
        'seed_cx':      float(np.median([s['cx']   for s in slice_data])),
        'seed_cy':      float(np.median([s['cy']   for s in slice_data])),
        'seed_r_m':     float(np.median([s['r']    for s in slice_data])),
        'seed_n_slices': len(slice_data),
        'seed_mean_cost': float(np.mean([s['cost'] for s in slice_data]))
    }


# ═══════════════════════════════════════════════════════════
# SEGMENT SCORING
# ═══════════════════════════════════════════════════════════

def score_segment(pts, seed_info):
    """
    Compute QC metrics for a single tree's point cloud.

    Rejection at this stage is intentionally permissive (MIN_FAILURES_TO_REJECT=3)
    because this is a candidate-generation step. Verticality and centroid drift
    are logged as warnings but do not count toward rejection.

    Returns a dict of QC fields compatible with the segment summary CSV.
    """
    failures = 0
    reasons = []
    pca_linearity = np.nan
    pca_verticality = np.nan
    n_centroid_slices = 0
    mean_drift = np.nan
    max_drift = np.nan

    if len(pts) < PCA_MIN_POINTS:
        failures += 1
        reasons.append('too_few_points')
    else:
        pca = PCA(n_components=3)
        pca.fit(pts)
        ev = pca.explained_variance_ratio_
        pca_linearity = float((ev[0] - ev[1]) / (ev[0] + 1e-9))
        pca_verticality = float(abs(pca.components_[0][2]))

        if pca_linearity < PCA_MIN_LINEARITY:
            failures += 1
            reasons.append(f'linearity={pca_linearity:.2f}')

        if pca_verticality < PCA_MIN_VERTICALITY:
            reasons.append(f'verticality_warn={pca_verticality:.2f}')

    # Slice centroid drift — warning only, not a rejection criterion
    tx, ty, tz = pts[:, 0], pts[:, 1], pts[:, 2]
    slice_bottoms = np.arange(tz.min(), tz.max(), SLICE_THICKNESS)
    centroids = []
    for zb in slice_bottoms:
        sl = (tz >= zb) & (tz < zb + SLICE_THICKNESS)
        if sl.sum() >= 5:
            centroids.append((float(tx[sl].mean()), float(ty[sl].mean())))

    n_centroid_slices = len(centroids)
    if len(centroids) < MIN_SLICES:
        reasons.append('too_few_slices_warn')
    elif len(centroids) >= 2:
        drifts = [
            np.sqrt((centroids[i][0] - centroids[i-1][0])**2 +
                    (centroids[i][1] - centroids[i-1][1])**2)
            for i in range(1, len(centroids))
        ]
        mean_drift = float(np.mean(drifts))
        max_drift  = float(np.max(drifts))
        if mean_drift > MAX_CENTROID_DRIFT and max_drift > MAX_SINGLE_DRIFT:
            reasons.append(f'drift_warn={mean_drift:.2f}')

    if failures >= MIN_FAILURES_TO_REJECT:
        status = 'rejected_after_scoring'
        tier   = 'failed'
    elif failures == 1:
        status = 'accepted_with_warning'
        tier   = 'moderate'
    else:
        status = 'good_candidate'
        tier   = 'good'

    return {
        'failures':              int(failures),
        'fail_reasons':          ';'.join(reasons) if reasons else '',
        'pca_linearity':         pca_linearity,
        'pca_verticality':       pca_verticality,
        'n_centroid_slices':     int(n_centroid_slices),
        'mean_centroid_drift_m': mean_drift,
        'max_centroid_drift_m':  max_drift,
        'segment_status':        status,
        'segment_qc_tier':       tier,
        **seed_info
    }


def score_all_segments(stem_x, stem_y, stem_z, tree_ids, valid_trees):
    """
    Score every valid tree segment. Returns:
      - score_lookup: dict[tree_id -> score dict]
      - rejected_ids: set of tree_ids that failed scoring
    """
    score_lookup = {}
    rejected_ids = set()

    for tree_id in valid_trees:
        tree_id = int(tree_id)
        mask = tree_ids == tree_id
        pts = np.vstack([stem_x[mask], stem_y[mask], stem_z[mask]]).T

        if len(pts) == 0:
            score = {
                'failures': MIN_FAILURES_TO_REJECT,
                'fail_reasons': 'empty_after_density_filter',
                'pca_linearity': np.nan, 'pca_verticality': np.nan,
                'n_centroid_slices': 0,
                'mean_centroid_drift_m': np.nan, 'max_centroid_drift_m': np.nan,
                'segment_status': 'rejected_after_scoring',
                'segment_qc_tier': 'failed',
                **estimate_provisional_stem_seed(np.zeros((0, 3)))
            }
            rejected_ids.add(tree_id)
        else:
            seed_info = estimate_provisional_stem_seed(pts)
            score = score_segment(pts, seed_info)
            if score['segment_status'] == 'rejected_after_scoring':
                rejected_ids.add(tree_id)

        score_lookup[tree_id] = score

    return score_lookup, rejected_ids


# ═══════════════════════════════════════════════════════════
# RADIAL CONSISTENCY FILTER
# ═══════════════════════════════════════════════════════════

def apply_radial_filter(stem_x, stem_y, tree_ids, valid_trees, score_lookup):
    """
    For each accepted tree with a reliable provisional seed, remove points
    outside [seed_r * R_MIN_MULT, seed_r * R_MAX_MULT] of the seed centre.

    This filter is load-bearing: it modifies the point cloud passed to
    dbh_extraction_v2.py. Per-tree statistics (n_before, n_removed,
    retention_fraction) are returned for inclusion in the segment summary CSV
    so that compounded filtering effects can be audited downstream.

    Trees with seed_status != 'seed_ok' are passed through unmodified.
    The RADIAL_MIN_POINTS_KEEP safeguard prevents total collapse of a tree
    if the provisional seed is geometrically misleading, but does NOT protect
    against systematic one-sided clipping of a leaning stem. If DBH estimates
    from dbh_extraction_v2.py are consistently low, audit per-tree retention
    fractions in the segment summary CSV and consider increasing R_MAX_MULT or
    disabling this filter.

    Returns:
      - filtered tree_ids array (zeros where points were removed)
      - radial_stats: dict[tree_id -> {n_before, n_removed, retention_fraction}]
    """
    filtered_ids = tree_ids.copy()
    radial_stats = {}

    for tree_id in valid_trees:
        tree_id = int(tree_id)
        mask = tree_ids == tree_id
        n_before = int(mask.sum())

        if not np.any(mask):
            radial_stats[tree_id] = {
                'radial_n_before': 0,
                'radial_n_removed': 0,
                'radial_retention_frac': np.nan
            }
            continue

        seed = score_lookup.get(tree_id, {})

        # Only filter trees with a reliable seed
        if seed.get('seed_status') != 'seed_ok':
            radial_stats[tree_id] = {
                'radial_n_before': n_before,
                'radial_n_removed': 0,
                'radial_retention_frac': 1.0
            }
            continue

        seed_cx = seed['seed_cx']
        seed_cy = seed['seed_cy']
        seed_r  = seed['seed_r_m']

        if np.isnan(seed_cx) or np.isnan(seed_cy) or np.isnan(seed_r) or seed_r <= 0:
            radial_stats[tree_id] = {
                'radial_n_before': n_before,
                'radial_n_removed': 0,
                'radial_retention_frac': 1.0
            }
            continue

        idx  = np.where(mask)[0]
        dist = np.sqrt((stem_x[idx] - seed_cx)**2 + (stem_y[idx] - seed_cy)**2)
        keep = (dist >= seed_r * RADIAL_R_MIN_MULT) & (dist <= seed_r * RADIAL_R_MAX_MULT)

        n_kept = int(keep.sum())
        n_removed = n_before - n_kept

        if n_kept >= RADIAL_MIN_POINTS_KEEP:
            filtered_ids[idx[~keep]] = 0
        else:
            # Seed is likely misleading — pass through unmodified
            n_removed = 0

        radial_stats[tree_id] = {
            'radial_n_before':        n_before,
            'radial_n_removed':       n_removed,
            'radial_retention_frac':  round((n_before - n_removed) / n_before, 4)
        }

    total_removed = sum(s['radial_n_removed'] for s in radial_stats.values())
    print(f"  Radial filter removed {total_removed:,} points across {len(valid_trees)} trees")
    return filtered_ids, radial_stats


# ═══════════════════════════════════════════════════════════
# EXPORT HELPERS
# ═══════════════════════════════════════════════════════════

def generate_distinct_colours(n):
    colours = []
    for i in range(max(n, 1)):
        hue = i / max(n, 1)
        saturation = 0.9 if i % 2 == 0 else 0.6
        value = 0.9 if i % 3 != 0 else 0.7
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        colours.append([int(r * 65535), int(g * 65535), int(b * 65535)])
    return np.array(colours, dtype=np.uint16)


def save_segment_summary_csv(
    path, n_trees, tree_top_lookup, score_lookup, radial_stats,
    raw_counts, density_counts, final_counts
):
    """Write one row per detected treetop to the segment summary CSV."""
    fieldnames = [
        'tree_id', 'tree_top_x', 'tree_top_y', 'canopy_height_m',
        'n_points_raw', 'n_points_after_density', 'n_points_final',
        # Radial filter stats (new in v2_radial): audit compounded filtering
        'radial_n_before', 'radial_n_removed', 'radial_retention_frac',
        # Provisional seed — load-bearing (drives radial filter)
        'seed_status', 'seed_cx', 'seed_cy', 'seed_r_m',
        'seed_n_slices', 'seed_mean_cost',
        # Shape QC
        'pca_linearity', 'pca_verticality',
        'n_centroid_slices', 'mean_centroid_drift_m', 'max_centroid_drift_m',
        'failures', 'fail_reasons', 'segment_status', 'segment_qc_tier'
    ]

    def _f(v, decimals=3):
        """Format float, returning '' for NaN."""
        try:
            return round(float(v), decimals) if not np.isnan(float(v)) else ''
        except (TypeError, ValueError):
            return ''

    def _i(v):
        try:
            return int(v) if v == v else ''
        except (TypeError, ValueError):
            return ''

    default_score = {
        'seed_status': 'not_scored', 'seed_cx': np.nan, 'seed_cy': np.nan,
        'seed_r_m': np.nan, 'seed_n_slices': 0, 'seed_mean_cost': np.nan,
        'pca_linearity': np.nan, 'pca_verticality': np.nan,
        'n_centroid_slices': 0, 'mean_centroid_drift_m': np.nan,
        'max_centroid_drift_m': np.nan, 'failures': np.nan, 'fail_reasons': '',
        'segment_status': 'rejected_before_scoring', 'segment_qc_tier': 'failed'
    }
    default_radial = {
        'radial_n_before': 0, 'radial_n_removed': 0, 'radial_retention_frac': np.nan
    }

    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for tree_id in range(1, n_trees + 1):
            tt  = tree_top_lookup.get(tree_id, {'tree_top_x': np.nan, 'tree_top_y': np.nan, 'canopy_height_m': np.nan})
            sc  = score_lookup.get(tree_id, default_score)
            rad = radial_stats.get(tree_id, default_radial)
            writer.writerow({
                'tree_id':              tree_id,
                'tree_top_x':          _f(tt['tree_top_x']),
                'tree_top_y':          _f(tt['tree_top_y']),
                'canopy_height_m':     _f(tt['canopy_height_m']),
                'n_points_raw':        raw_counts.get(tree_id, 0),
                'n_points_after_density': density_counts.get(tree_id, 0),
                'n_points_final':      final_counts.get(tree_id, 0),
                'radial_n_before':     _i(rad['radial_n_before']),
                'radial_n_removed':    _i(rad['radial_n_removed']),
                'radial_retention_frac': _f(rad['radial_retention_frac'], 4),
                'seed_status':         sc['seed_status'],
                'seed_cx':             _f(sc['seed_cx']),
                'seed_cy':             _f(sc['seed_cy']),
                'seed_r_m':            _f(sc['seed_r_m']),
                'seed_n_slices':       _i(sc['seed_n_slices']),
                'seed_mean_cost':      _f(sc['seed_mean_cost'], 5),
                'pca_linearity':       _f(sc['pca_linearity']),
                'pca_verticality':     _f(sc['pca_verticality']),
                'n_centroid_slices':   _i(sc['n_centroid_slices']),
                'mean_centroid_drift_m': _f(sc['mean_centroid_drift_m']),
                'max_centroid_drift_m':  _f(sc['max_centroid_drift_m']),
                'failures':            _i(sc['failures']),
                'fail_reasons':        sc['fail_reasons'],
                'segment_status':      sc['segment_status'],
                'segment_qc_tier':     sc['segment_qc_tier']
            })


def save_treetop_poles(
    path, tree_top_x, tree_top_y, tree_top_z_true
):
    """Export each treetop as a vertical pole point cloud for visualisation."""
    n_trees = len(tree_top_x)
    angles = np.linspace(0, 2 * np.pi, POLE_DISC_PTS, endpoint=False)

    pole_x, pole_y, pole_z = [], [], []
    pole_r, pole_g, pole_b = [], [], []
    pole_id, pole_ht        = [], []

    hmin = float(tree_top_z_true.min()) if n_trees > 0 else 0.0
    hmax = float(tree_top_z_true.max()) if n_trees > 0 else 1.0

    for i in range(n_trees):
        tx = tree_top_x[i]
        ty = tree_top_y[i]
        t_height = tree_top_z_true[i]

        h_norm = (t_height - hmin) / (hmax - hmin + 1e-9)
        pr = int(h_norm * 65535)
        pg = int((1 - abs(h_norm - 0.5) * 2) * 32767)
        pb = int((1 - h_norm) * 65535)

        z_levels = np.unique(np.append(
            np.arange(0.0, t_height + POLE_Z_STEP, POLE_Z_STEP), t_height
        ))

        for z_level in z_levels:
            for a in angles:
                pole_x.append(tx + POLE_RADIUS * np.cos(a))
                pole_y.append(ty + POLE_RADIUS * np.sin(a))
                pole_z.append(z_level)
                pole_r.append(pr); pole_g.append(pg); pole_b.append(pb)
                pole_id.append(i + 1)
                pole_ht.append(t_height)

    px = np.array(pole_x);  py = np.array(pole_y);  pz = np.array(pole_z)
    pr = np.array(pole_r, dtype=np.uint16)
    pg = np.array(pole_g, dtype=np.uint16)
    pb = np.array(pole_b, dtype=np.uint16)

    header = laspy.LasHeader(point_format=7, version='1.4')
    header.scales  = np.array([0.001, 0.001, 0.001])
    header.offsets = np.array([float(np.floor(px.min())), float(np.floor(py.min())), 0.0])

    las = laspy.LasData(header=header)
    las.x = px; las.y = py; las.z = pz
    las.red = pr; las.green = pg; las.blue = pb
    las.add_extra_dim(laspy.ExtraBytesParams(name='tree_id',         type=np.int32))
    las.add_extra_dim(laspy.ExtraBytesParams(name='canopy_height_m', type=np.float32))
    las.tree_id         = np.array(pole_id, dtype=np.int32)
    las.canopy_height_m = np.array(pole_ht, dtype=np.float32)
    las.write(path)

    print(f"  Poles saved: {path}  ({len(px):,} points, {n_trees} trees)")
    print(f"  Colour key: blue=short, red=tall | radius={POLE_RADIUS}m | Z step={POLE_Z_STEP}m")


def save_treetops_csv(path, tree_top_x, tree_top_y, tree_top_z_true):
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['tree_id', 'tree_top_x', 'tree_top_y', 'canopy_height_m'])
        for i in range(len(tree_top_x)):
            writer.writerow([
                i + 1,
                round(float(tree_top_x[i]), 3),
                round(float(tree_top_y[i]), 3),
                round(float(tree_top_z_true[i]), 3),
            ])
    print(f"  Treetops CSV saved: {path}  ({len(tree_top_x)} rows)")


def save_candidate_las(path, final_x, final_y, final_z, final_ids, r, g, b):
    """
    Save the final candidate lower-stem cloud.
    Intentionally does NOT include dbh_m — that is the job of dbh_extraction_v2.py.
    Note: the radial filter has already been applied to this cloud.
    """
    header = laspy.LasHeader(point_format=7, version='1.4')
    header.scales  = np.array([0.001, 0.001, 0.001])
    header.offsets = np.array([
        float(np.floor(final_x.min())),
        float(np.floor(final_y.min())),
        float(np.floor(final_z.min()))
    ])
    out = laspy.LasData(header=header)
    out.x = final_x; out.y = final_y; out.z = final_z
    out.add_extra_dim(laspy.ExtraBytesParams(name='tree_id', type=np.int32))
    out.tree_id = final_ids.astype(np.int32)
    out.red = r; out.green = g; out.blue = b
    out.write(path)


def save_diagnostic_las(
    path, las, stem_mask, stem_x_orig, stem_y_orig, x_min, y_min,
    resolution, n_cols, n_rows, chm_watershed, density_mask,
    tree_ids_stem_final
):
    """
    Colour-coded diagnostic cloud showing what each filter stage accepted/rejected.
    Colour key:
      Green  = accepted candidate
      Red    = removed after scoring or radial filter
      Grey   = removed by density filter
      Blue   = no watershed ID assigned
    """
    cols = np.clip(((stem_x_orig - x_min) / resolution).astype(int), 0, n_cols - 1)
    rows = np.clip(((stem_y_orig - y_min) / resolution).astype(int), 0, n_rows - 1)
    watershed_ids = chm_watershed[rows, cols]

    diag_r = np.zeros(len(stem_x_orig), dtype=np.uint16)
    diag_g = np.zeros(len(stem_x_orig), dtype=np.uint16)
    diag_b = np.zeros(len(stem_x_orig), dtype=np.uint16)

    no_watershed     = watershed_ids == 0
    removed_density  = ~density_mask

    diag_b[no_watershed]     = 65535            # Blue:  no watershed ID
    diag_r[removed_density]  = 32767            # Grey:  density-filtered
    diag_g[removed_density]  = 32767
    diag_b[removed_density]  = 32767

    survived_ids = np.zeros(len(stem_x_orig), dtype=np.int32)
    survived_ids[density_mask] = tree_ids_stem_final

    survived   = survived_ids > 0
    has_ws     = watershed_ids > 0
    scored_out = has_ws & ~survived & ~no_watershed & ~removed_density

    diag_r[survived]   = 0
    diag_g[survived]   = 65535
    diag_b[survived]   = 0                      # Green: accepted
    diag_r[scored_out] = 65535
    diag_g[scored_out] = 0
    diag_b[scored_out] = 0                      # Red:   rejected

    out = laspy.LasData(header=las.header)
    out.points = las.points[stem_mask]
    out.add_extra_dim(laspy.ExtraBytesParams(name='tree_id', type=np.int32))
    out.tree_id = watershed_ids.astype(np.int32)
    out.red = diag_r; out.green = diag_g; out.blue = diag_b
    out.write(path)

    print(f"Diagnostic cloud saved: {path}")
    print(f"  Green = accepted ({survived.sum():,})  |  "
          f"Red = rejected ({scored_out.sum():,})  |  "
          f"Grey = density-filtered ({removed_density.sum():,})  |  "
          f"Blue = no watershed ({no_watershed.sum():,})")


def save_plot(
    path, chm_smooth, chm_watershed, x_min, x_max, y_min, y_max,
    tree_top_x, tree_top_y, tree_top_z_true, n_accepted
):
    """Top-view plot: CHM with treetops (left) and watershed segments (right)."""
    fig, axes = plt.subplots(1, 2, figsize=(18, 8))
    extent = [x_min, x_max, y_min, y_max]

    ax1 = axes[0]
    im1 = ax1.imshow(np.flipud(chm_smooth), extent=extent,
                     cmap='Greens', origin='upper', aspect='equal')
    plt.colorbar(im1, ax=ax1, shrink=0.6).set_label('Height (m)')
    n_trees = len(tree_top_x)
    ax1.scatter(tree_top_x, tree_top_y, c='red', s=40, marker='+',
                linewidths=1.5, zorder=5, label=f'Tree tops (n={n_trees})')
    if n_trees > 0:
        rng = np.random.default_rng(seed=42)  # fixed seed for reproducibility
        sample_idx = rng.choice(n_trees, min(30, n_trees), replace=False)
        for idx in sample_idx:
            ax1.annotate(f"{tree_top_z_true[idx]:.1f}m",
                         xy=(tree_top_x[idx], tree_top_y[idx]),
                         xytext=(3, 3), textcoords='offset points',
                         fontsize=6, color='white', fontweight='bold')
    ax1.set_title(f"Tree Top Detection\n{n_trees} tops", fontweight='bold')
    ax1.set_xlabel("Easting (m)"); ax1.set_ylabel("Northing (m)")
    ax1.legend(loc='upper right', fontsize=9); ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    n_labels = max(int(chm_watershed.max()) + 1, 1)
    rng2 = np.random.default_rng(seed=42)
    cmap_colours = rng2.random((n_labels, 4))
    cmap_colours[0] = [0.1, 0.1, 0.1, 1.0]
    ax2.imshow(np.flipud(chm_watershed), extent=extent,
               cmap=ListedColormap(cmap_colours), origin='upper',
               aspect='equal', interpolation='nearest')
    ax2.scatter(tree_top_x, tree_top_y, c='white', s=20, marker='+',
                linewidths=1.0, zorder=5)
    ax2.set_title(f"Watershed Candidate Segments\n{n_accepted} accepted trees",
                  fontweight='bold')
    ax2.set_xlabel("Easting (m)"); ax2.set_ylabel("Northing (m)")
    ax2.grid(True, alpha=0.3)

    plt.suptitle("Tree Candidate Segmentation Pipeline (v2 radial)",
                 fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(path, dpi=200, bbox_inches='tight')
    plt.close()
    print(f"Plot saved: {path}")


# ═══════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════

def _count_lookup(ids_array):
    """Return dict[tree_id -> count] for non-zero IDs."""
    pos = ids_array[ids_array > 0]
    if len(pos) == 0:
        return {}
    uu, cc = np.unique(pos, return_counts=True)
    return {int(u): int(c) for u, c in zip(uu, cc)}


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Read input ─────────────────────────────────────
    print("Reading normalised file...")
    las = laspy.read(INPUT_FILE)
    x = np.array(las.x)
    y = np.array(las.y)
    z = np.array(las.z)
    print(f"Total points: {len(x):,}")

    x_min, y_min = x.min(), y.min()
    x_max, y_max = x.max(), y.max()

    # ── 2. CHM ────────────────────────────────────────────
    print("Building Canopy Height Model...")
    chm, n_rows, n_cols = build_chm(x, y, z, x_min, y_min, CHM_RESOLUTION)
    print(f"CHM size: {n_rows} x {n_cols} pixels")

    print("Smoothing CHM...")
    chm_smooth = gaussian_filter(chm.astype(np.float32), sigma=1.0)

    # ── 3. Treetop detection ──────────────────────────────
    print("Detecting tree tops...")
    ttx, tty, ttz, tt_rows, tt_cols = detect_treetops(
        chm_smooth, x_min, y_min, CHM_RESOLUTION, HMIN, TREE_TOP_RADIUS
    )
    print(f"Tree tops before NMS: {len(ttx):,}")

    ttx, tty, ttz, tt_rows, tt_cols = non_maximum_suppression(
        ttx, tty, ttz, tt_rows, tt_cols, NMS_DISTANCE
    )
    n_trees = len(ttx)
    print(f"Tree tops after NMS:  {n_trees:,}")

    spatial_index = cKDTree(np.vstack([x, y]).T)
    ttz_true = refine_treetop_heights(
        ttx, tty, x, y, z, spatial_index, chm_smooth, tt_rows, tt_cols, CHM_RESOLUTION
    )
    print(f"Canopy height range: {ttz_true.min():.1f}m → {ttz_true.max():.1f}m")

    # ── 4. Watershed segmentation ─────────────────────────
    print("Running watershed segmentation on CHM...")
    seeds = np.zeros((n_rows, n_cols), dtype=np.int32)
    for i in range(n_trees):
        seeds[tt_rows[i], tt_cols[i]] = i + 1

    chm_watershed = watershed(-chm_smooth, markers=seeds, mask=chm_smooth >= HMIN)
    print(f"Watershed labels: {chm_watershed.max():,}")

    # ── 5. Stem candidate extraction + ID assignment ──────
    print("Extracting lower-stem candidates...")
    stem_x, stem_y, stem_z, stem_mask = extract_stem_candidates(
        x, y, z, STEM_MIN_H, STEM_MAX_H
    )
    print(f"Points in height band {STEM_MIN_H}–{STEM_MAX_H}m: {len(stem_x):,}")

    print("Assigning tree IDs from watershed...")
    tree_ids = assign_tree_ids_from_watershed(
        stem_x, stem_y, chm_watershed, x_min, y_min, CHM_RESOLUTION, n_cols, n_rows
    )
    tree_ids = apply_min_points_filter(tree_ids, MIN_POINTS)
    raw_counts = _count_lookup(tree_ids)

    # Preserve originals for diagnostic LAS (before density filter)
    stem_x_orig = stem_x.copy()
    stem_y_orig = stem_y.copy()

    # ── 6. Density filter ─────────────────────────────────
    print("Applying density filter...")
    stem_x, stem_y, stem_z, tree_ids, density_mask = apply_density_filter(
        stem_x, stem_y, stem_z, tree_ids, DENSITY_RADIUS, DENSITY_MIN_NEIGHBOURS
    )
    tree_ids = apply_min_points_filter(tree_ids, MIN_POINTS)
    density_counts = _count_lookup(tree_ids)

    unique_post, counts_post = np.unique(tree_ids, return_counts=True)
    valid_trees = unique_post[(counts_post >= MIN_POINTS) & (unique_post > 0)]
    print(f"Valid trees after density + MIN_POINTS recheck: {len(valid_trees):,}")

    # ── 7. Segment scoring ────────────────────────────────
    print("Scoring candidate segments...")
    score_lookup, rejected_ids = score_all_segments(
        stem_x, stem_y, stem_z, tree_ids, valid_trees
    )
    tree_ids[np.isin(tree_ids, list(rejected_ids))] = 0
    valid_trees_final = np.array(
        [int(t) for t in valid_trees if int(t) not in rejected_ids], dtype=np.int32
    )
    print(f"After scoring: {len(valid_trees_final):,} accepted candidate segments")

    # ── 8. Radial consistency filter ──────────────────────
    radial_stats = {}
    if APPLY_RADIAL_FILTER:
        print("Applying radial consistency filter...")
        tree_ids, radial_stats = apply_radial_filter(
            stem_x, stem_y, tree_ids, valid_trees_final, score_lookup
        )

    final_counts = _count_lookup(tree_ids)

    # ── 9. Build treetop lookup ───────────────────────────
    tree_top_lookup = {
        i + 1: {
            'tree_top_x':    float(ttx[i]),
            'tree_top_y':    float(tty[i]),
            'canopy_height_m': float(ttz_true[i])
        }
        for i in range(n_trees)
    }

    # ── 10. Segment summary CSV ───────────────────────────
    print("Saving segment summary CSV...")
    save_segment_summary_csv(
        SEGMENT_SUMMARY_CSV, n_trees, tree_top_lookup,
        score_lookup, radial_stats,
        raw_counts, density_counts, final_counts
    )
    print(f"Segment summary saved: {SEGMENT_SUMMARY_CSV}")

    # ── 11. Assign point colours for candidate LAS ────────
    print("Assigning colours...")
    max_id = int(tree_ids.max()) + 1 if len(tree_ids) > 0 else 1
    colour_map = np.zeros((max_id, 3), dtype=np.uint16)
    distinct = generate_distinct_colours(len(valid_trees_final) + 1)
    for i, tid in enumerate(valid_trees_final):
        colour_map[int(tid)] = distinct[i]
    colour_map[0] = [0, 0, 0]

    point_colours = colour_map[tree_ids] if len(tree_ids) > 0 else np.zeros((0, 3), dtype=np.uint16)
    pr = point_colours[:, 0]; pg = point_colours[:, 1]; pb = point_colours[:, 2]

    # ── 12. Plot ──────────────────────────────────────────
    print("Generating top-view plot...")
    save_plot(
        PLOT_OUTPUT, chm_smooth, chm_watershed,
        x_min, x_max, y_min, y_max,
        ttx, tty, ttz_true, len(valid_trees_final)
    )

    # ── 13. Treetop poles ─────────────────────────────────
    print("Exporting treetop poles...")
    save_treetop_poles(TREETOPS_FILE, ttx, tty, ttz_true)
    save_treetops_csv(TREETOPS_CSV, ttx, tty, ttz_true)

    # ── 14. Diagnostic LAS ────────────────────────────────
    print("Saving diagnostic LAS...")
    DIAG_FILE = OUTPUT_FILE.with_name(OUTPUT_FILE.stem + '_diagnostic.las')
    save_diagnostic_las(
        DIAG_FILE, las, stem_mask,
        stem_x_orig, stem_y_orig,
        x_min, y_min, CHM_RESOLUTION, n_cols, n_rows,
        chm_watershed, density_mask, tree_ids
    )

    # ── 15. Final candidate LAS ───────────────────────────
    print("Saving final candidate LAS...")
    final_mask = tree_ids > 0
    final_x   = stem_x[final_mask]
    final_y   = stem_y[final_mask]
    final_z   = stem_z[final_mask]
    final_ids = tree_ids[final_mask]

    if len(final_x) == 0:
        raise RuntimeError('No candidate points remain after all filtering stages.')

    print(f"  X: {final_x.min():.3f} → {final_x.max():.3f}")
    print(f"  Y: {final_y.min():.3f} → {final_y.max():.3f}")
    print(f"  Z: {final_z.min():.3f} → {final_z.max():.3f}")

    save_candidate_las(
        OUTPUT_FILE,
        final_x, final_y, final_z, final_ids,
        pr[final_mask], pg[final_mask], pb[final_mask]
    )
    print(f"Candidate LAS saved: {OUTPUT_FILE}")
    print(f"  Points: {final_mask.sum():,}  |  Trees: {len(valid_trees_final):,}")
    print("  Note: radial filter has been applied. See segment summary CSV for per-tree retention stats.")


if __name__ == '__main__':
    main()