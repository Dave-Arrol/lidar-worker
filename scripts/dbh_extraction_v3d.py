#!/usr/bin/env python3
"""
dbh_extraction_v3d.py
=====================
Stage-2 of the pipeline: ring/arc-based DBH extraction from candidate
lower-stem point clouds produced by segmentation_v2_radial.py.

Design notes
------------
The candidate LAS arriving here has already had segmentation_v2_radial's
radial filter applied. This script must not assume it is receiving an
unfiltered stem cloud. The segment summary CSV from segmentation is loaded
and its per-tree radial filter statistics (radial_retention_frac, seed_r_m)
are joined to the output so compounded filtering effects can be audited.

Shrinkage accounting
--------------------
Three sources of shrinkage act on the fitted radius before the final DBH:
  1. Upstream radial filter (segmentation_v2_radial): clips to seed_r * 1.40.
     Effect is tree-specific; see radial_retention_frac in segment summary.
  2. SLICE_RADIUS_SCALE (per-slice, post-annulus): applies ONLY when
     radial_retention_frac > UPSTREAM_FILTER_SKIP_THRESHOLD, i.e. when the
     upstream filter was active. If the upstream filter was loose or skipped,
     per-slice shrinkage is applied normally.
  3. MODEL_RADIUS_SCALE (at DBH extraction): same conditional logic.
Both scale factors should be re-validated whenever RADIAL_R_MAX_MULT in
segmentation_v2_radial.py is changed.

Slice classification
--------------------
classify_slice() uses a continuous soft scoring approach rather than a hard
threshold gate. Coverage, residual, and radius each contribute a score in
[0, 1]. The combined score determines class:
  ring_good   : score >= SCORE_RING_GOOD_MIN
  arc_usable  : score >= SCORE_ARC_USABLE_MIN
  weak        : otherwise
This eliminates the 1-degree coverage cliff that caused 40x weight jumps
between classes under the previous hard-threshold design.

Confidence scoring
------------------
fit_conf is computed from metrics at or near DBH height, not averages across
all accepted slices. Slices within DBH_CONF_WINDOW_M of 1.3m are used.
model_taper penalty scales with actual interpolation gap, not a flat 0.85.

Weight logging
--------------
Per-slice weights are written to the slice CSV so the taper model's effective
degree of freedom can be audited (e.g. whether 2 slices drive the whole fit).

Inputs:
  results/cpt7a/algorithmic/cpt7a_tree_candidates_dense_v2_radial_pl1.las
  results/cpt7a/algorithmic/cpt7a_segment_summary_dense_v2_radial_pl1.csv
  results/cpt7a/algorithmic/cpt7a_treetops_dense_v2_radial_pl1.csv
  data/cpt7a/processed/cpt7a_ground.csv

Outputs:
  results/cpt7a/algorithmic/cpt7a_results_benchmark_v3d.csv
  results/cpt7a/algorithmic/cpt7a_dense_dbh_slices_v3d_benchmark.csv
  results/cpt7a/algorithmic/cpt7a_dense_dbh_plot_v3d_benchmark.png
  results/cpt7a/algorithmic/cpt7a_dense_dbh_v3d_benchmark.las
  results/cpt7a/algorithmic/cpt7a_taper_models_v3d_benchmark.csv  ← consumed by stem_profile.py
"""

from pathlib import Path
import argparse
import csv
import json
from rasterio.warp import transform as warp_transform
import math

import laspy
import matplotlib
matplotlib.use('Agg')  # headless-safe
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.spatial import cKDTree
import memlog
memlog.track("DBH Ex")  
# ── PATHS ─────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]

_ap = argparse.ArgumentParser(description="Per-tree DBH extraction, slices and taper models.")
_ap.add_argument('--input',           required=True, help='Stem-candidate LAS from segmentation.')
_ap.add_argument('--ground',          required=True, help='Ground CSV (x,y,ground_z).')
_ap.add_argument('--treetops',        required=True, help='Tree-tops CSV from segmentation.')
_ap.add_argument('--segment-summary', required=True, help='Per-tree segment summary CSV.')
_ap.add_argument('--out-results',     required=True, help='Output per-tree DBH results CSV.')
_ap.add_argument('--out-slices',      required=True, help='Output per-slice CSV.')
_ap.add_argument('--out-models',      required=True, help='Output taper-model CSV.')
_ap.add_argument('--out-las',         required=True, help='Output DBH ring-fit verification LAS.')
_ap.add_argument('--out-summary',     required=True, help='Output DBH summary JSON.')
_ap.add_argument('--out-geojson',     required=True, help='Output per-tree DBH GeoJSON (EPSG:4326).')
_ap.add_argument('--crs',             default='EPSG:27700', help='CRS of the tree X/Y. Default EPSG:27700.')
_args = _ap.parse_args()

INPUT_FILE          = Path(_args.input)
GROUND_CSV          = Path(_args.ground)
TREETOPS_CSV        = Path(_args.treetops)
SEGMENT_SUMMARY_CSV = Path(_args.segment_summary)

CSV_OUTPUT        = Path(_args.out_results)
SLICE_CSV_OUTPUT  = Path(_args.out_slices)
MODEL_CSV_OUTPUT  = Path(_args.out_models)
DBH_PLOT          = CSV_OUTPUT.with_name(CSV_OUTPUT.stem + '_plot.png')
VERIFY_FILE       = Path(_args.out_las)
SUMMARY_JSON      = Path(_args.out_summary)
GEOJSON_OUTPUT    = Path(_args.out_geojson)
OUTPUT_CRS        = _args.crs

# ── SETTINGS ──────────────────────────────────────────────

# Slice geometry
SLICE_Z_MIN      = 0.5
SLICE_Z_MAX      = 5.0
SLICE_STEP       = 0.10
SLICE_THICKNESS  = 0.20
MIN_POINTS_SLICE = 6

# Circle fit
MIN_RADIUS        = 0.05
MAX_RADIUS        = 0.25
FIT_ITERATIONS    = 3
MIN_INLIER_POINTS = 6
MIN_INLIER_TOL    = 0.010
MAX_INLIER_TOL    = 0.050
INLIER_MAD_MULT   = 2.5
ANGLE_BINS        = 36

# Annulus refinement
# Tighter than v3c to reduce outer-shell branch inflation.
ANNULUS_WIDTH_MIN  = 0.008
ANNULUS_WIDTH_MAX  = 0.040
ANNULUS_WIDTH_FRAC = 0.20

# Shrinkage factors — see docstring for interaction with upstream radial filter.
# Applied conditionally: skipped when the upstream radial filter was already
# aggressive (radial_retention_frac <= UPSTREAM_FILTER_SKIP_THRESHOLD).
# Re-validate both values if RADIAL_R_MAX_MULT in segmentation changes.
SLICE_RADIUS_SCALE             = 0.99
MODEL_RADIUS_SCALE             = 0.95
UPSTREAM_FILTER_SKIP_THRESHOLD = 0.80  # retention below this → skip local shrink

# Slice classification via soft scoring (replaces hard threshold gates).
# Each component score is in [0, 1]; final score is a weighted mean.
# Thresholds below set the class boundaries on that combined score.
SCORE_COVERAGE_FULL  = 180.0   # coverage (deg) at which coverage score = 1.0
SCORE_COVERAGE_ZERO  = 40.0    # coverage (deg) at which coverage score = 0.0
SCORE_RESIDUAL_BEST  = 0.010   # residual (m) at which residual score = 1.0
SCORE_RESIDUAL_WORST = 0.070   # residual (m) at which residual score = 0.0
SCORE_RADIUS_BEST    = 0.03    # radius (m) at which radius score = 1.0 (smallest plausible stem)
SCORE_RADIUS_WORST   = 0.25    # radius (m) at which radius score = 0.0 (hard cap)
SCORE_WEIGHT_COVERAGE = 0.50
SCORE_WEIGHT_RESIDUAL = 0.35
SCORE_WEIGHT_RADIUS   = 0.15
SCORE_RING_GOOD_MIN   = 0.65   # combined score threshold for ring_good
SCORE_ARC_USABLE_MIN  = 0.25   # combined score threshold for arc_usable
MIN_RING_POINTS_GOOD  = 8
MIN_RING_POINTS_USABLE = 6
MAX_INTERIOR_RATIO    = 0.30   # hard reject: too many interior points → not a shell

# Taper model
MIN_ACCEPTED_SLICES    = 3
MIN_RING_GOOD_SLICES   = 2
MAX_MODEL_DBH_EXTRAP_M = 1.50
DBH_HEIGHT             = 1.3
MAX_DIRECT_GAP_M       = 0.25

# Taper model weighting
RING_GOOD_BASE_WEIGHT  = 8.0
ARC_USABLE_BASE_WEIGHT = 0.2
SMALL_RING_WEIGHT_EXP  = 1.50
RADIUS_WEIGHT_MIN      = 0.50
RADIUS_WEIGHT_MAX      = 2.50
LOW_Z_W_TOP            = 1.60
LOW_Z_W_BOTTOM         = 0.70
CENTRE_LOW_Z_TOP       = 1.25
CENTRE_LOW_Z_BOTTOM    = 0.90
RADIUS_SLOPE_SHRINK    = 0.70
MAX_RADIUS_SLOPE_ABS   = 0.010

# Confidence scoring window: use slices within this distance of DBH_HEIGHT
DBH_CONF_WINDOW_M = 0.40

# Verification LAS / plot
RING_POINTS     = 72
RING_LAYERS     = 3
RING_Z_STEP     = 0.05
RING_VIEW_Z     = DBH_HEIGHT
MODEL_VIZ_Z_MIN = 0.5
MODEL_VIZ_Z_MAX = 5.0
MODEL_VIZ_Z_STEP = 0.20

# ──────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════
# GEOMETRY UTILITIES
# ═══════════════════════════════════════════════════════════

def circle_residuals(params, pts):
    cx, cy, r = params
    return np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2) - r


def angular_coverage(points_2d, cx, cy, bins=ANGLE_BINS):
    """
    Return (coverage_deg, coverage_ratio): angular span occupied by points
    around (cx, cy), measured by how many of `bins` sectors contain ≥1 point.
    """
    dx = points_2d[:, 0] - cx
    dy = points_2d[:, 1] - cy
    ang = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0
    hist, _ = np.histogram(ang, bins=np.linspace(0.0, 360.0, bins + 1))
    occupied = int((hist > 0).sum())
    return occupied / bins * 360.0, occupied / bins


# ═══════════════════════════════════════════════════════════
# CIRCLE FITTING
# ═══════════════════════════════════════════════════════════

def initial_circle_fit(points_2d):
    """
    Iterative least-squares circle fit with MAD-based inlier selection.
    Returns a dict or None if fit fails / radius out of range.
    """
    if len(points_2d) < MIN_POINTS_SLICE:
        return None
    pts = np.asarray(points_2d, dtype=np.float64)
    cx0, cy0 = np.median(pts[:, 0]), np.median(pts[:, 1])
    r0 = max(MIN_RADIUS, min(MAX_RADIUS, float(
        np.median(np.sqrt((pts[:, 0] - cx0)**2 + (pts[:, 1] - cy0)**2))
    )))

    inliers = np.ones(len(pts), dtype=bool)
    params  = np.array([cx0, cy0, r0], dtype=np.float64)

    try:
        for _ in range(FIT_ITERATIONS):
            if inliers.sum() < MIN_INLIER_POINTS:
                return None
            result = least_squares(circle_residuals, params, args=(pts[inliers],), method='lm')
            cx, cy, r = result.x
            r = abs(float(r))
            if not (MIN_RADIUS < r < MAX_RADIUS):
                return None
            abs_resid = np.abs(circle_residuals([cx, cy, r], pts))
            med = np.median(abs_resid)
            mad = np.median(np.abs(abs_resid - med))
            tol = np.clip(1.4826 * mad * INLIER_MAD_MULT, MIN_INLIER_TOL, MAX_INLIER_TOL)
            new_inliers = abs_resid <= tol
            if new_inliers.sum() < MIN_INLIER_POINTS:
                return None
            if np.array_equal(new_inliers, inliers):
                inliers = new_inliers
                params  = np.array([cx, cy, r])
                break
            inliers = new_inliers
            params  = np.array([cx, cy, r])

        result = least_squares(circle_residuals, params, args=(pts[inliers],), method='lm')
        cx, cy, r = result.x
        r = abs(float(r))
        if not (MIN_RADIUS < r < MAX_RADIUS):
            return None
        final_resid = np.abs(circle_residuals([cx, cy, r], pts[inliers]))
        return {
            'cx': float(cx), 'cy': float(cy), 'r': float(r),
            'mean_residual': float(final_resid.mean()) if len(final_resid) else np.nan,
            'n_points': int(len(pts)),
            'n_inliers': int(inliers.sum()),
            'points': pts,
        }
    except Exception:
        return None


def annulus_refine_fit(points_2d, cx, cy, r, apply_slice_scale):
    """
    Restrict points to an annulus shell around the rough fit radius, then
    refit. Returns enriched fit dict or None.

    apply_slice_scale: if True, apply SLICE_RADIUS_SCALE inward shrink.
    This should be False when the upstream radial filter was already aggressive
    (radial_retention_frac <= UPSTREAM_FILTER_SKIP_THRESHOLD).
    """
    pts = np.asarray(points_2d, dtype=np.float64)
    if len(pts) < MIN_POINTS_SLICE:
        return None

    d = np.sqrt((pts[:, 0] - cx)**2 + (pts[:, 1] - cy)**2)
    half_w = np.clip(ANNULUS_WIDTH_FRAC * float(r), ANNULUS_WIDTH_MIN, ANNULUS_WIDTH_MAX)
    keep = np.abs(d - float(r)) <= half_w
    if keep.sum() < MIN_INLIER_POINTS:
        return None

    refined = initial_circle_fit(pts[keep])
    if refined is None:
        return None

    cx2, cy2, r2 = refined['cx'], refined['cy'], refined['r']
    if apply_slice_scale:
        r2 = max(MIN_RADIUS, min(MAX_RADIUS, r2 * SLICE_RADIUS_SCALE))

    cov_deg, cov_ratio = angular_coverage(pts[keep], cx2, cy2)
    d_all = np.sqrt((pts[:, 0] - cx2)**2 + (pts[:, 1] - cy2)**2)
    interior_ratio = float((d_all < 0.55 * r2).sum() / max(len(pts), 1))

    refined.update({
        'cx': float(cx2), 'cy': float(cy2), 'r': float(r2),
        'coverage_deg': float(cov_deg),
        'coverage_ratio': float(cov_ratio),
        'interior_ratio': float(interior_ratio),
        'annulus_half_width': float(half_w),
        'n_ring_points': int(keep.sum()),
    })
    return refined


# ═══════════════════════════════════════════════════════════
# SLICE CLASSIFICATION
# ═══════════════════════════════════════════════════════════

def _component_score(value, best, worst):
    """Map value to [0, 1] linearly between best and worst."""
    if worst == best:
        return 1.0
    raw = (value - worst) / (best - worst)
    return float(np.clip(raw, 0.0, 1.0))


def classify_slice(sf):
    """
    Soft scoring classification replacing the previous hard-threshold gate.

    Each component (coverage, residual, radius size) is scored in [0, 1].
    The weighted mean determines class. This eliminates the 1-degree cliff
    that caused 40x weight jumps between ring_good and arc_usable.

    Hard rejects still apply:
      - interior_ratio > MAX_INTERIOR_RATIO (not a shell)
      - n_ring_points below class minimum
      - radius > SCORE_RADIUS_WORST (hard cap, same as MAX_RADIUS)
    """
    if sf is None:
        return 'weak', 0.0

    if sf['interior_ratio'] > MAX_INTERIOR_RATIO:
        return 'weak', 0.0

    if sf['r'] >= SCORE_RADIUS_WORST:
        return 'weak', 0.0

    cov_score = _component_score(sf['coverage_deg'],  SCORE_COVERAGE_FULL, SCORE_COVERAGE_ZERO)
    res_score = _component_score(sf['mean_residual'],  SCORE_RESIDUAL_BEST, SCORE_RESIDUAL_WORST)
    rad_score = _component_score(sf['r'],              SCORE_RADIUS_BEST,   SCORE_RADIUS_WORST)

    score = (
        SCORE_WEIGHT_COVERAGE * cov_score +
        SCORE_WEIGHT_RESIDUAL * res_score +
        SCORE_WEIGHT_RADIUS   * rad_score
    )

    if score >= SCORE_RING_GOOD_MIN and sf['n_ring_points'] >= MIN_RING_POINTS_GOOD:
        return 'ring_good', round(score, 4)
    if score >= SCORE_ARC_USABLE_MIN and sf['n_ring_points'] >= MIN_RING_POINTS_USABLE:
        return 'arc_usable', round(score, 4)
    return 'weak', round(score, 4)


# ═══════════════════════════════════════════════════════════
# SLICE PROCESSING
# ═══════════════════════════════════════════════════════════

def process_tree_slices(tree_pts, apply_slice_scale):
    """
    Fit and classify all horizontal slices for one tree.

    apply_slice_scale is passed through to annulus_refine_fit; it should be
    False when the upstream radial filter was already aggressive for this tree.

    Returns (slice_all, accepted):
      slice_all : list of dicts for every attempted slice (all classes)
      accepted  : subset where class in ('ring_good', 'arc_usable')
    """
    z = tree_pts[:, 2]
    slice_all, accepted = [], []

    for z_low in np.arange(SLICE_Z_MIN, SLICE_Z_MAX, SLICE_STEP):
        z_high = z_low + SLICE_THICKNESS
        z_mid  = z_low + SLICE_THICKNESS / 2.0
        mask   = (z >= z_low) & (z < z_high)
        if mask.sum() < MIN_POINTS_SLICE:
            continue

        pts2d = tree_pts[mask, :2]
        rough = initial_circle_fit(pts2d)
        if rough is None:
            continue

        refined = annulus_refine_fit(pts2d, rough['cx'], rough['cy'], rough['r'], apply_slice_scale)
        if refined is None:
            continue

        cls, score = classify_slice(refined)
        rec = {
            'z_mid':          float(z_mid),
            'cx':             float(refined['cx']),
            'cy':             float(refined['cy']),
            'r':              float(refined['r']),
            'mean_residual':  float(refined['mean_residual']),
            'coverage_deg':   float(refined['coverage_deg']),
            'coverage_ratio': float(refined['coverage_ratio']),
            'interior_ratio': float(refined['interior_ratio']),
            'n_points':       int(refined['n_points']),
            'n_inliers':      int(refined['n_inliers']),
            'n_ring_points':  int(refined['n_ring_points']),
            'slice_class':    cls,
            'slice_score':    score,
        }
        slice_all.append(rec)
        if cls in ('ring_good', 'arc_usable'):
            accepted.append(rec)

    return slice_all, accepted


# ═══════════════════════════════════════════════════════════
# TAPER MODEL
# ═══════════════════════════════════════════════════════════

def _compute_slice_weights(slices):
    """
    Compute per-slice weights for taper model fitting.
    Returns (w_radius, w_centre, z_array) as numpy arrays.

    Weights are logged to the slice CSV so the model's effective degrees of
    freedom can be audited. If 1–2 slices dominate (weight >> all others),
    the taper fit is unreliable.
    """
    s  = sorted(slices, key=lambda t: t['z_mid'])
    z  = np.array([t['z_mid'] for t in s], dtype=np.float64)
    r  = np.array([t['r']     for t in s], dtype=np.float64)

    base_w = np.array([
        RING_GOOD_BASE_WEIGHT if t['slice_class'] == 'ring_good' else ARC_USABLE_BASE_WEIGHT
        for t in s
    ], dtype=np.float64)

    cov_w = np.array([
        max(0.25, min(t['coverage_deg'] / 180.0, 1.0)) for t in s
    ], dtype=np.float64)

    res_w = np.array([
        max(0.25, 1.0 - min(t['mean_residual'] / 0.08, 1.0)) for t in s
    ], dtype=np.float64)

    # Score-proportional weight: slices closer to ring_good get more influence
    # even within the same nominal class, reducing sensitivity to the class boundary.
    score_w = np.array([t['slice_score'] for t in s], dtype=np.float64)
    score_w = np.clip(score_w, 0.1, 1.0)

    # Smaller rings get more weight (more likely to be true stem, not branch shell)
    median_r = float(np.median(r))
    radius_w = np.clip(
        (median_r / np.maximum(r, 1e-6)) ** SMALL_RING_WEIGHT_EXP,
        RADIUS_WEIGHT_MIN, RADIUS_WEIGHT_MAX
    )

    # Lower slices are usually cleaner
    z_rel   = (z - z.min()) / max(z.max() - z.min(), 1e-6)
    low_z_w = np.clip(
        LOW_Z_W_TOP - (LOW_Z_W_TOP - LOW_Z_W_BOTTOM) * z_rel,
        LOW_Z_W_BOTTOM, LOW_Z_W_TOP
    )
    centre_low_z_w = np.clip(
        CENTRE_LOW_Z_TOP - (CENTRE_LOW_Z_TOP - CENTRE_LOW_Z_BOTTOM) * z_rel,
        CENTRE_LOW_Z_BOTTOM, CENTRE_LOW_Z_TOP
    )

    w_radius = base_w * cov_w * res_w * score_w * radius_w * low_z_w
    w_centre = base_w * cov_w * res_w * score_w * centre_low_z_w

    return w_radius, w_centre, z, s


def fit_stem_model(accepted_slices):
    """
    Fit a linear taper model (cx, cy, r as functions of z) to accepted slices.
    Returns model dict or None if insufficient slices.
    """
    if len(accepted_slices) < MIN_ACCEPTED_SLICES:
        return None

    w_radius, w_centre, z, s = _compute_slice_weights(accepted_slices)
    cx = np.array([t['cx'] for t in s], dtype=np.float64)
    cy = np.array([t['cy'] for t in s], dtype=np.float64)
    r  = np.array([t['r']  for t in s], dtype=np.float64)

    # Store per-slice weights back onto each record for CSV export
    for i, rec in enumerate(s):
        rec['weight_radius'] = round(float(w_radius[i]), 4)
        rec['weight_centre'] = round(float(w_centre[i]), 4)

    # Centreline fit
    deg_c   = 1 if len(s) >= 2 else 0
    cx_coef = np.polyfit(z, cx, deg=deg_c, w=w_centre)
    cy_coef = np.polyfit(z, cy, deg=deg_c, w=w_centre)

    # Radius: fit line, then clamp + shrink slope toward near-cylinder
    if len(s) >= 2:
        r_line    = np.polyfit(z, r, deg=1, w=w_radius)
        slope     = float(r_line[0])
        slope     = np.clip(slope, -MAX_RADIUS_SLOPE_ABS, MAX_RADIUS_SLOPE_ABS)
        slope    *= RADIUS_SLOPE_SHRINK
        z_bar     = float(np.average(z, weights=w_radius))
        r_bar     = float(np.average(r, weights=w_radius))
        intercept = r_bar - slope * z_bar
        r_coef    = np.array([slope, intercept])
    else:
        r_coef = np.array([float(np.mean(r))])

    cx_pred = np.polyval(cx_coef, z)
    cy_pred = np.polyval(cy_coef, z)
    r_pred  = np.polyval(r_coef, z)

    # Effective degrees of freedom: how concentrated are the weights?
    w_norm = w_radius / w_radius.sum()
    eff_dof = float(1.0 / np.sum(w_norm ** 2))  # inverse participation ratio

    return {
        'cx_coef':        cx_coef,
        'cy_coef':        cy_coef,
        'r_coef':         r_coef,
        'n_slices_model': len(s),
        'centre_rmse':    float(np.sqrt(np.mean((cx - cx_pred)**2 + (cy - cy_pred)**2))),
        'radius_rmse':    float(np.sqrt(np.mean((r - r_pred)**2))),
        'z_min_model':    float(z.min()),
        'z_max_model':    float(z.max()),
        'eff_dof':        round(eff_dof, 2),
    }


def model_eval(model, zq, apply_model_scale):
    """
    Evaluate the taper model at height zq.
    apply_model_scale: apply MODEL_RADIUS_SCALE if True.
    """
    cx = float(np.polyval(model['cx_coef'], zq))
    cy = float(np.polyval(model['cy_coef'], zq))
    r  = float(np.polyval(model['r_coef'],  zq))
    if apply_model_scale:
        r *= MODEL_RADIUS_SCALE
    r = np.clip(r, MIN_RADIUS, MAX_RADIUS)
    return cx, cy, r


# ═══════════════════════════════════════════════════════════
# DBH ESTIMATION
# ═══════════════════════════════════════════════════════════

def estimate_dbh(accepted_slices, model, apply_model_scale):
    """
    Estimate DBH from accepted slices and taper model.

    Priority:
      1. Direct interpolation between slices bracketing DBH_HEIGHT
         (both neighbours within MAX_DIRECT_GAP_M)
      2. Nearest accepted slice within MAX_DIRECT_GAP_M
      3. Model extrapolation within MAX_MODEL_DBH_EXTRAP_M

    Returns dict with dbh_m, cx, cy, dbh_source, coverage_deg_at_dbh,
    and dbh_gap_m (actual interpolation/extrapolation distance for use
    in confidence scoring).
    """
    if len(accepted_slices) < MIN_ACCEPTED_SLICES or model is None:
        return None

    s   = sorted(accepted_slices, key=lambda t: t['z_mid'])
    z   = np.array([t['z_mid'] for t in s], dtype=np.float64)
    r   = np.array([t['r']     for t in s], dtype=np.float64)
    cx  = np.array([t['cx']    for t in s], dtype=np.float64)
    cy  = np.array([t['cy']    for t in s], dtype=np.float64)
    cov = np.array([t['coverage_deg'] for t in s], dtype=np.float64)

    # Direct interpolation
    if z.min() <= DBH_HEIGHT <= z.max():
        left  = z[z <= DBH_HEIGHT]
        right = z[z >= DBH_HEIGHT]
        if len(left) > 0 and len(right) > 0:
            gap_l = DBH_HEIGHT - left.max()
            gap_r = right.min() - DBH_HEIGHT
            if gap_l <= MAX_DIRECT_GAP_M and gap_r <= MAX_DIRECT_GAP_M:
                return {
                    'dbh_m':              round(float(np.interp(DBH_HEIGHT, z, r)) * 2.0, 4),
                    'cx':                 round(float(np.interp(DBH_HEIGHT, z, cx)), 3),
                    'cy':                 round(float(np.interp(DBH_HEIGHT, z, cy)), 3),
                    'dbh_source':         'direct_interp',
                    'coverage_deg_at_dbh': round(float(np.interp(DBH_HEIGHT, z, cov)), 1),
                    'dbh_gap_m':          round(max(gap_l, gap_r), 3),
                }

    # Nearest slice
    idx = int(np.argmin(np.abs(z - DBH_HEIGHT)))
    gap = float(abs(z[idx] - DBH_HEIGHT))
    if gap <= MAX_DIRECT_GAP_M:
        return {
            'dbh_m':              round(float(r[idx]) * 2.0, 4),
            'cx':                 round(float(cx[idx]), 3),
            'cy':                 round(float(cy[idx]), 3),
            'dbh_source':         'direct_nearest',
            'coverage_deg_at_dbh': round(float(cov[idx]), 1),
            'dbh_gap_m':          round(gap, 3),
        }

    # Model extrapolation
    nearest_gap = float(np.min(np.abs(z - DBH_HEIGHT)))
    if nearest_gap <= MAX_MODEL_DBH_EXTRAP_M:
        mx, my, mr = model_eval(model, DBH_HEIGHT, apply_model_scale)
        return {
            'dbh_m':              round(mr * 2.0, 4),
            'cx':                 round(mx, 3),
            'cy':                 round(my, 3),
            'dbh_source':         'model_taper',
            'coverage_deg_at_dbh': round(float(cov[idx]), 1),
            'dbh_gap_m':          round(nearest_gap, 3),
        }

    return None


# ═══════════════════════════════════════════════════════════
# STATUS + CONFIDENCE
# ═══════════════════════════════════════════════════════════

def assign_status(slice_all, accepted, model, dbh_fit):
    if len(slice_all) == 0:
        return 'insufficient_slices'
    if len(accepted) < MIN_ACCEPTED_SLICES:
        return 'partial_arc' if any(s['slice_class'] == 'arc_usable' for s in slice_all) \
               else 'insufficient_slices'
    if model is None:
        return 'no_consensus'
    if dbh_fit is None:
        return 'dbh_unsupported'
    n_good   = sum(1 for s in accepted if s['slice_class'] == 'ring_good')
    mean_res = float(np.mean([s['mean_residual'] for s in accepted]))
    if (n_good >= MIN_RING_GOOD_SLICES and mean_res <= SCORE_RESIDUAL_WORST * 0.6
            and dbh_fit['dbh_source'] == 'direct_interp'):
        return 'good'
    return 'limited_support'


def compute_confidence(accepted_slices, dbh_fit, model):
    """
    Confidence score based on slices near DBH height, not global averages.

    Uses accepted slices within DBH_CONF_WINDOW_M of DBH_HEIGHT. Falls back
    to all accepted slices if none are that close (so score is not undefined).

    model_taper penalty scales with actual gap (dbh_gap_m), not a flat 0.85.
    """
    if dbh_fit is None or model is None or not accepted_slices:
        return 0.0

    near = [s for s in accepted_slices
            if abs(s['z_mid'] - DBH_HEIGHT) <= DBH_CONF_WINDOW_M]
    slices_for_conf = near if near else accepted_slices

    mean_cov = float(np.mean([s['coverage_deg']  for s in slices_for_conf]))
    mean_res = float(np.mean([s['mean_residual'] for s in slices_for_conf]))
    mean_r   = float(np.mean([s['r']             for s in slices_for_conf]))
    r_sd     = float(np.std( [s['r']             for s in slices_for_conf]))

    conf  = 1.0
    conf *= max(0.0, min(mean_cov / 180.0, 1.0))
    conf *= max(0.0, 1.0 - min(mean_res / 0.08, 1.0))
    conf *= max(0.0, 1.0 - min(r_sd * 100.0 / 10.0, 1.0))

    # Scale penalty by actual gap, not a flat 15%
    if dbh_fit['dbh_source'] == 'model_taper':
        gap_penalty = 1.0 - min(dbh_fit['dbh_gap_m'] / MAX_MODEL_DBH_EXTRAP_M, 1.0) * 0.40
        conf *= gap_penalty

    # Use coverage at DBH height specifically if available
    conf *= max(0.5, min(dbh_fit['coverage_deg_at_dbh'] / 180.0, 1.0))

    return round(float(conf), 3)


# ═══════════════════════════════════════════════════════════
# VERIFICATION LAS EXPORT
# ═══════════════════════════════════════════════════════════

def _add_ring(px, py, pz, pr, pg, pb, cx, cy, radius, z_mid, colour,
              n_points=RING_POINTS, layers=RING_LAYERS):
    angles = np.linspace(0, 2 * np.pi, n_points, endpoint=False)
    for z_off in np.linspace(-layers * RING_Z_STEP / 2, layers * RING_Z_STEP / 2, layers):
        px.extend(cx + radius * np.cos(angles))
        py.extend(cy + radius * np.sin(angles))
        pz.extend(np.full(n_points, z_mid + z_off))
        pr.extend(np.full(n_points, colour[0], dtype=np.uint16))
        pg.extend(np.full(n_points, colour[1], dtype=np.uint16))
        pb.extend(np.full(n_points, colour[2], dtype=np.uint16))


def _build_candidate_point_layer(results, x, y, z, tree_ids):
    """Colour raw candidate points by tree status."""
    status_colours = {
        'good':                (0,     65535, 0),
        'limited_support':     (65535, 50000, 0),
        'partial_arc':         (65535, 32767, 0),
        'insufficient_slices': (0,     0,     65535),
        'no_consensus':        (40000, 0,     40000),
        'dbh_unsupported':     (30000, 30000, 65535),
        'failed':              (65535, 0,     0),
    }
    out_x, out_y, out_z, out_r, out_g, out_b = [], [], [], [], [], []
    for row in results:
        tid  = int(row['tree_id'])
        mask = tree_ids == tid
        if not mask.any():
            continue
        col = status_colours.get(row['status'], (65535, 65535, 0))
        n   = int(mask.sum())
        out_x.extend(x[mask]); out_y.extend(y[mask]); out_z.extend(z[mask])
        out_r.extend(np.full(n, col[0], dtype=np.uint16))
        out_g.extend(np.full(n, col[1], dtype=np.uint16))
        out_b.extend(np.full(n, col[2], dtype=np.uint16))
    return out_x, out_y, out_z, out_r, out_g, out_b


def _build_ring_layers(results, viz_lookup, apply_model_scale):
    """Build accepted-slice rings, model tube rings, and DBH ring points."""
    slice_colours = {'ring_good': (0, 65535, 65535), 'arc_usable': (65535, 65535, 0)}
    model_colour  = (65535, 0, 65535)
    dbh_colour    = (65535, 0, 0)

    out_x, out_y, out_z, out_r, out_g, out_b = [], [], [], [], [], []
    for row in results:
        tid  = int(row['tree_id'])
        viz  = viz_lookup.get(tid, {})

        for s in viz.get('accepted_slices', []):
            col = slice_colours.get(s['slice_class'], (50000, 50000, 50000))
            _add_ring(out_x, out_y, out_z, out_r, out_g, out_b,
                      float(s['cx']), float(s['cy']), float(s['r']), float(s['z_mid']), col)

        model = viz.get('model')
        if model is not None:
            z_start = max(MODEL_VIZ_Z_MIN, float(model['z_min_model']))
            z_end   = min(MODEL_VIZ_Z_MAX, float(model['z_max_model']))
            for zq in np.arange(z_start, z_end + MODEL_VIZ_Z_STEP, MODEL_VIZ_Z_STEP):
                cxq, cyq, rq = model_eval(model, float(zq), apply_model_scale)
                _add_ring(out_x, out_y, out_z, out_r, out_g, out_b,
                          cxq, cyq, rq, float(zq), model_colour, n_points=48, layers=1)

        dbh_m = row.get('dbh_m')
        rcx   = row.get('cx')
        rcy   = row.get('cy')
        if pd.notna(dbh_m) and pd.notna(rcx) and pd.notna(rcy):
            _add_ring(out_x, out_y, out_z, out_r, out_g, out_b,
                      float(rcx), float(rcy), float(dbh_m) / 2.0,
                      RING_VIEW_Z, dbh_colour)

    return out_x, out_y, out_z, out_r, out_g, out_b


def build_verify_arrays(results, viz_lookup, x, y, z, tree_ids, apply_model_scale):
    """Combine candidate points and ring overlay layers into final arrays."""
    pts  = _build_candidate_point_layer(results, x, y, z, tree_ids)
    rngs = _build_ring_layers(results, viz_lookup, apply_model_scale)

    merged = [np.array(a + b) for a, b in zip(pts, rngs)]
    return tuple(
        arr.astype(np.uint16) if i >= 3 else arr
        for i, arr in enumerate(merged)
    )


def save_verify_las(path, all_x, all_y, all_z, all_r, all_g, all_b):
    if len(all_x) == 0:
        print('  No points to write to verification LAS.')
        return
    vh = laspy.LasHeader(point_format=7, version='1.4')
    vh.scales  = np.array([0.001, 0.001, 0.001])
    vh.offsets = np.array([
        float(np.floor(all_x.min())),
        float(np.floor(all_y.min())),
        float(np.floor(all_z.min()))
    ])
    vlas = laspy.LasData(header=vh)
    vlas.x = all_x; vlas.y = all_y; vlas.z = all_z
    vlas.red = all_r; vlas.green = all_g; vlas.blue = all_b
    vlas.write(path)
    print(f'  Verification LAS saved: {path}')
    print('  Colour key:')
    print('    Cyan rings    = ring_good slices (weighted strongly in taper model)')
    print('    Yellow rings  = arc_usable slices (weighted weakly)')
    print('    Magenta rings = constrained taper model tube')
    print(f'    Red ring      = estimated DBH at {DBH_HEIGHT:.1f} m')


# ═══════════════════════════════════════════════════════════
# PLOTTING
# ═══════════════════════════════════════════════════════════

def save_plot(path, results, valid_trees_count):
    goodish = [r for r in results if pd.notna(r.get('dbh_cm'))]
    fig, axes = plt.subplots(1, 2, figsize=(16, 7))

    ax1 = axes[0]
    if goodish:
        xs = [r['cx']    for r in goodish if pd.notna(r['cx'])]
        ys = [r['cy']    for r in goodish if pd.notna(r['cy'])]
        cs = [r['dbh_cm'] for r in goodish if pd.notna(r['cx'])]
        if xs:
            sc = ax1.scatter(xs, ys, c=cs, cmap='RdYlGn_r', s=80, zorder=3, vmin=10, vmax=80)
            plt.colorbar(sc, ax=ax1).set_label('DBH (cm)')
    ax1.set_title(
        f'DBH Spatial Map v3d\n{len(goodish)} successful / {valid_trees_count} trees',
        fontweight='bold'
    )
    ax1.set_xlabel('Easting (m)'); ax1.set_ylabel('Northing (m)')
    ax1.grid(True, alpha=0.3); ax1.set_aspect('equal')

    ax2 = axes[1]
    if goodish:
        vals = [r['dbh_cm'] for r in goodish]
        ax2.hist(vals, bins=20, color='steelblue', edgecolor='white', linewidth=0.5)
        ax2.axvline(np.mean(vals),   color='red',    linestyle='--', linewidth=1.5,
                    label=f'Mean: {np.mean(vals):.1f} cm')
        ax2.axvline(np.median(vals), color='orange', linestyle='--', linewidth=1.5,
                    label=f'Median: {np.median(vals):.1f} cm')

        # Per-status breakdown in subtitle
        status_summary = {}
        for r in results:
            status_summary[r['status']] = status_summary.get(r['status'], 0) + 1
        subtitle = '  |  '.join(f"{s}: {n}" for s, n in sorted(status_summary.items()))
        ax2.set_title(f'DBH Distribution (v3d)\n{subtitle}', fontweight='bold', fontsize=9)
        ax2.set_xlabel('DBH (cm)'); ax2.set_ylabel('Count')
        ax2.legend(fontsize=9); ax2.grid(True, alpha=0.3)

    plt.suptitle('Diameter at Breast Height — Ring / Arc Slice Model (v3d)',
                 fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(path, dpi=200, bbox_inches='tight')
    plt.close()
    print(f'Plot saved: {path}')


# ═══════════════════════════════════════════════════════════
# CSV HELPERS
# ═══════════════════════════════════════════════════════════

def save_results_csv(path, results):
    fieldnames = [
        'tree_id', 'cx', 'cy', 'dbh_m', 'dbh_cm', 'tree_height_m', 'z_min',
        'n_slices', 'mean_residual', 'status',
        'n_candidate_points', 'n_stem_points_used',
        'coverage_deg', 'coverage_ratio', 'radius_sd_cm',
        'dbh_source', 'dbh_gap_m', 'dbh_confidence',
        'n_ring_good_slices', 'n_arc_usable_slices',
        'interior_ratio_mean', 'model_centre_rmse_m', 'model_radius_rmse_m',
        'model_eff_dof', 'coverage_deg_at_dbh',
        # Upstream filter audit columns (from segment summary CSV)
        'upstream_radial_retention_frac', 'upstream_seed_r_m',
        'slice_scale_applied', 'model_scale_applied',
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)
    print(f'Results CSV saved: {path}  ({len(results)} rows)')


def save_slice_csv(path, slice_rows):
    fieldnames = [
        'tree_id', 'z_mid', 'cx', 'cy', 'radius_m', 'diameter_cm',
        'mean_residual', 'coverage_deg', 'coverage_ratio', 'interior_ratio',
        'n_points', 'n_inliers', 'n_ring_points', 'slice_class', 'slice_score',
        'weight_radius', 'weight_centre',  # new: per-slice weights for audit
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(slice_rows)
    print(f'Slice CSV saved: {path}  ({len(slice_rows)} rows)')


def save_model_csv(path, model_rows):
    """
    Export per-tree taper model coefficients for consumption by stem_profile.py.

    Polynomial coefficients are stored as individual columns so the CSV is
    human-readable without pickle/numpy serialisation.

    Degree-1 (normal):   cx = cx_coef_0 * z + cx_coef_1
    Degree-0 (fallback): cx = cx_coef_0  (cx_coef_1 will be empty)

    stem_profile.py reconstructs np.polyval([coef_0, coef_1], z) directly.
    The shrinkage flags are included so the profile script applies exactly the
    same corrections as the DBH extractor — no independent re-fitting needed.
    """
    fieldnames = [
        'tree_id',
        'cx_coef_0', 'cx_coef_1',
        'cy_coef_0', 'cy_coef_1',
        'r_coef_0',  'r_coef_1',
        'z_min_model', 'z_max_model',
        'n_slices_model', 'centre_rmse', 'radius_rmse', 'eff_dof',
        'slice_scale_applied', 'model_scale_applied',
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(model_rows)
    print(f'Model CSV saved: {path}  ({len(model_rows)} rows)')



# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    CSV_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Load inputs ────────────────────────────────────
    print('Loading ground surface...')
    ground_pts  = np.loadtxt(GROUND_CSV, delimiter=',', skiprows=1)
    ground_tree = cKDTree(ground_pts[:, :2])
    print(f'  Ground points: {len(ground_pts):,}')

    print('Loading treetops CSV...')
    tops = pd.read_csv(TREETOPS_CSV)
    canopy_height_lookup = dict(zip(
        tops['tree_id'].astype(int), tops['canopy_height_m'].astype(float)
    ))
    print(f'  Canopy heights: {len(canopy_height_lookup):,} trees')

    # ── 2. Load segment summary (upstream filter audit) ───
    upstream_stats = {}
    if SEGMENT_SUMMARY_CSV.exists():
        print('Loading segment summary CSV (upstream filter audit)...')
        seg_df = pd.read_csv(SEGMENT_SUMMARY_CSV)
        for _, row in seg_df.iterrows():
            tid = int(row['tree_id'])
            upstream_stats[tid] = {
                'radial_retention_frac': float(row['radial_retention_frac'])
                    if pd.notna(row.get('radial_retention_frac')) else np.nan,
                'seed_r_m': float(row['seed_r_m'])
                    if pd.notna(row.get('seed_r_m')) else np.nan,
            }
        print(f'  Upstream stats loaded for {len(upstream_stats):,} trees')
    else:
        print(f'  WARNING: {SEGMENT_SUMMARY_CSV} not found — '
              f'upstream filter audit columns will be empty.')

    # ── 3. Load candidate LAS ─────────────────────────────
    print('Reading candidate tree cloud...')
    las = laspy.read(INPUT_FILE)
    if not hasattr(las, 'tree_id'):
        raise ValueError('Candidate LAS is missing tree_id extra dimension.')
    x        = np.array(las.x)
    y        = np.array(las.y)
    z        = np.array(las.z)
    tree_ids = np.array(las.tree_id)
    valid_trees = np.unique(tree_ids[tree_ids > 0])
    print(f'  Trees to process: {len(valid_trees):,}')
    print(f'  Z range: {z.min():.2f} → {z.max():.2f}')

    # ── 4. Per-tree processing ────────────────────────────
    print('Processing trees...')
    results, slice_rows, model_rows, viz_lookup, status_counts = [], [], [], {}, {}

    for tree_id in valid_trees:
        tree_id = int(tree_id)
        mask    = tree_ids == tree_id
        pts     = np.vstack([x[mask], y[mask], z[mask]]).T
        n_candidate_points = int(len(pts))

        # Determine whether upstream shrinkage was active for this tree.
        # If the radial filter retained most points (retention near 1.0),
        # local shrink factors are applied normally. If it was aggressive,
        # local shrink is skipped to avoid double-correction.
        up = upstream_stats.get(tree_id, {})
        retention = up.get('radial_retention_frac', np.nan)
        if pd.isna(retention):
            # No upstream info → apply local shrink conservatively
            apply_slice_scale = True
            apply_model_scale = True
        else:
            apply_slice_scale = retention > UPSTREAM_FILTER_SKIP_THRESHOLD
            apply_model_scale = retention > UPSTREAM_FILTER_SKIP_THRESHOLD

        slice_all, accepted = process_tree_slices(pts, apply_slice_scale)
        model    = fit_stem_model(accepted)
        dbh_fit  = estimate_dbh(accepted, model, apply_model_scale) if model else None
        status   = assign_status(slice_all, accepted, model, dbh_fit)
        fit_conf = compute_confidence(accepted, dbh_fit, model)

        viz_lookup[tree_id] = {'accepted_slices': accepted, 'model': model}

        # Collect slice rows (weights are written back into accepted records by fit_stem_model)
        for s in slice_all:
            slice_rows.append({
                'tree_id':        tree_id,
                'z_mid':          round(float(s['z_mid']), 3),
                'cx':             round(float(s['cx']), 3),
                'cy':             round(float(s['cy']), 3),
                'radius_m':       round(float(s['r']), 4),
                'diameter_cm':    round(float(s['r']) * 200.0, 2),
                'mean_residual':  round(float(s['mean_residual']), 4),
                'coverage_deg':   round(float(s['coverage_deg']), 1),
                'coverage_ratio': round(float(s['coverage_ratio']), 3),
                'interior_ratio': round(float(s['interior_ratio']), 3),
                'n_points':       int(s['n_points']),
                'n_inliers':      int(s['n_inliers']),
                'n_ring_points':  int(s['n_ring_points']),
                'slice_class':    s['slice_class'],
                'slice_score':    round(float(s['slice_score']), 4),
                'weight_radius':  s.get('weight_radius', ''),
                'weight_centre':  s.get('weight_centre', ''),
            })

        # Aggregate result metrics
        if dbh_fit:
            _, gi   = ground_tree.query([dbh_fit['cx'], dbh_fit['cy']], k=1)
            z_min_abs = round(float(ground_pts[gi, 2]), 3)
            dbh_m     = float(dbh_fit['dbh_m'])
            dbh_cm    = round(dbh_m * 100.0, 2)
            res_cx    = float(dbh_fit['cx'])
            res_cy    = float(dbh_fit['cy'])
            dbh_source = dbh_fit['dbh_source']
            cov_dbh    = float(dbh_fit['coverage_deg_at_dbh'])
            dbh_gap_m  = float(dbh_fit['dbh_gap_m'])
        else:
            z_min_abs = np.nan; dbh_m = np.nan; dbh_cm = np.nan
            res_cx = np.nan; res_cy = np.nan
            dbh_source = None; cov_dbh = np.nan; dbh_gap_m = np.nan

        mean_res        = float(np.mean([s['mean_residual'] for s in accepted])) if accepted else np.nan
        mean_cov        = float(np.mean([s['coverage_deg']  for s in accepted])) if accepted else np.nan
        mean_cov_ratio  = float(np.mean([s['coverage_ratio'] for s in accepted])) if accepted else np.nan
        interior_mean   = float(np.mean([s['interior_ratio'] for s in accepted])) if accepted else np.nan
        radius_sd_cm    = float(np.std([s['r'] for s in accepted]) * 100.0) if accepted else np.nan
        n_ring_good     = int(sum(1 for s in accepted if s['slice_class'] == 'ring_good'))
        n_arc_usable    = int(sum(1 for s in accepted if s['slice_class'] == 'arc_usable'))
        n_stem_pts_used = int(sum(s['n_ring_points'] for s in accepted)) if accepted else 0

        results.append({
            'tree_id':            tree_id,
            'cx':                 round(res_cx, 3) if pd.notna(res_cx) else np.nan,
            'cy':                 round(res_cy, 3) if pd.notna(res_cy) else np.nan,
            'dbh_m':              round(dbh_m, 4)  if pd.notna(dbh_m)  else np.nan,
            'dbh_cm':             dbh_cm,
            'tree_height_m':      round(float(canopy_height_lookup.get(tree_id, float('nan'))), 3),
            'z_min':              z_min_abs,
            'n_slices':           len(accepted),
            'mean_residual':      round(mean_res, 4) if pd.notna(mean_res) else np.nan,
            'status':             status,
            'n_candidate_points': n_candidate_points,
            'n_stem_points_used': n_stem_pts_used,
            'coverage_deg':       round(mean_cov, 1)      if pd.notna(mean_cov)       else np.nan,
            'coverage_ratio':     round(mean_cov_ratio, 3) if pd.notna(mean_cov_ratio) else np.nan,
            'radius_sd_cm':       round(radius_sd_cm, 2)  if pd.notna(radius_sd_cm)   else np.nan,
            'dbh_source':         dbh_source,
            'dbh_gap_m':          round(dbh_gap_m, 3)     if pd.notna(dbh_gap_m)      else np.nan,
            'dbh_confidence':     fit_conf,
            'n_ring_good_slices': n_ring_good,
            'n_arc_usable_slices': n_arc_usable,
            'interior_ratio_mean': round(interior_mean, 3) if pd.notna(interior_mean) else np.nan,
            'model_centre_rmse_m': round(float(model['centre_rmse']), 4) if model else np.nan,
            'model_radius_rmse_m': round(float(model['radius_rmse']), 4) if model else np.nan,
            'model_eff_dof':       model['eff_dof'] if model else np.nan,
            'coverage_deg_at_dbh': round(cov_dbh, 1) if pd.notna(cov_dbh) else np.nan,
            # Upstream filter audit
            'upstream_radial_retention_frac': round(retention, 4) if pd.notna(retention) else np.nan,
            'upstream_seed_r_m':  round(up.get('seed_r_m', np.nan), 3)
                                   if pd.notna(up.get('seed_r_m', np.nan)) else np.nan,
            'slice_scale_applied': int(apply_slice_scale),
            'model_scale_applied': int(apply_model_scale),
        })
        status_counts[status] = status_counts.get(status, 0) + 1

        # Collect model coefficients for stem_profile.py.
        # Coefficients are stored as individual scalar columns so the CSV is
        # human-readable. np.polyval([coef_0, coef_1], z) reconstructs the model.
        if model is not None:
            coef = {
                'tree_id':           tree_id,
                'cx_coef_0':         round(float(model['cx_coef'][0]), 8),
                'cx_coef_1':         round(float(model['cx_coef'][1]), 6) if len(model['cx_coef']) > 1 else '',
                'cy_coef_0':         round(float(model['cy_coef'][0]), 8),
                'cy_coef_1':         round(float(model['cy_coef'][1]), 6) if len(model['cy_coef']) > 1 else '',
                'r_coef_0':          round(float(model['r_coef'][0]),  8),
                'r_coef_1':          round(float(model['r_coef'][1]),  6) if len(model['r_coef'])  > 1 else '',
                'z_min_model':       round(float(model['z_min_model']), 3),
                'z_max_model':       round(float(model['z_max_model']), 3),
                'n_slices_model':    int(model['n_slices_model']),
                'centre_rmse':       round(float(model['centre_rmse']), 4),
                'radius_rmse':       round(float(model['radius_rmse']), 4),
                'eff_dof':           model['eff_dof'],
                'slice_scale_applied': int(apply_slice_scale),
                'model_scale_applied': int(apply_model_scale),
            }
            model_rows.append(coef)

    # ── 5. Print summary ──────────────────────────────────
    print(f'\nStatus breakdown ({len(valid_trees)} trees):')
    for s, n in sorted(status_counts.items()):
        print(f'  {s}: {n}')

    goodish = [r for r in results if pd.notna(r.get('dbh_cm'))]
    n_success = len(goodish)
    print(f'Successful (dbh_m available): {n_success} / {len(valid_trees)}')

    if goodish:
        vals = [r['dbh_cm'] for r in goodish]
        print('\n── DBH Summary ───────────────────────────────────────')
        print(f'  Mean:    {np.mean(vals):.1f} cm')
        print(f'  Median:  {np.median(vals):.1f} cm')
        print(f'  Min:     {np.min(vals):.1f} cm')
        print(f'  Max:     {np.max(vals):.1f} cm')
        print(f'  Std Dev: {np.std(vals):.1f} cm')
        print(f'  Mean slices: {np.mean([r["n_slices"] for r in goodish]):.1f}')

        # Per-status DBH breakdown — flags systematic bias by status
        print('\n── DBH by status ─────────────────────────────────────')
        for s in sorted(status_counts.keys()):
            grp = [r['dbh_cm'] for r in goodish if r['status'] == s and pd.notna(r['dbh_cm'])]
            if grp:
                print(f'  {s} (n={len(grp)}): mean={np.mean(grp):.1f}  '
                      f'median={np.median(grp):.1f}  sd={np.std(grp):.1f} cm')

        # Shrinkage audit summary
        skipped = sum(1 for r in results if not r['slice_scale_applied'])
        print(f'\n── Shrinkage audit ───────────────────────────────────')
        print(f'  Trees where local shrink was SKIPPED '
              f'(upstream filter active): {skipped} / {len(valid_trees)}')
        print(f'  (retention <= {UPSTREAM_FILTER_SKIP_THRESHOLD} triggers skip)')

    # ── 6. Save outputs ───────────────────────────────────
    print('\nSaving results CSV...')
    save_results_csv(CSV_OUTPUT, results)

    print('Saving slice CSV...')
    save_slice_csv(SLICE_CSV_OUTPUT, slice_rows)

    print('Saving model coefficients CSV...')
    save_model_csv(MODEL_CSV_OUTPUT, model_rows)

    print('Building verification LAS...')
    all_x, all_y, all_z, all_r, all_g, all_b = build_verify_arrays(
        results, viz_lookup, x, y, z, tree_ids,
        # Use MODEL_RADIUS_SCALE in visualisation only when it was applied in fitting
        apply_model_scale=True   # conservative: always show scaled model tube
    )
    save_verify_las(VERIFY_FILE, all_x, all_y, all_z, all_r, all_g, all_b)

    print('Generating plot...')
    save_plot(DBH_PLOT, results, len(valid_trees))

    # ── Summary JSON + per-tree DBH GeoJSON (EPSG:4326) for the portal ──
    goodish = [r for r in results if pd.notna(r.get('dbh_cm'))]
    dbh_cm_vals = np.array([float(r['dbh_cm']) for r in goodish], dtype=float)
    summary = {
        'trees':          int(len(results)),
        'stems_with_dbh': int(len(goodish)),
        'mean_dbh_cm':    round(float(np.mean(dbh_cm_vals)), 1)   if dbh_cm_vals.size else None,
        'median_dbh_cm':  round(float(np.median(dbh_cm_vals)), 1) if dbh_cm_vals.size else None,
        'min_dbh_cm':     round(float(np.min(dbh_cm_vals)), 1)    if dbh_cm_vals.size else None,
        'max_dbh_cm':     round(float(np.max(dbh_cm_vals)), 1)    if dbh_cm_vals.size else None,
    }
    SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_JSON, 'w') as f:
        json.dump(summary, f)
    print(f'Summary JSON saved: {SUMMARY_JSON}  ({summary})')

    geo = [r for r in goodish if pd.notna(r.get('cx')) and pd.notna(r.get('cy'))]
    if geo:
        lons, lats = warp_transform(OUTPUT_CRS, 'EPSG:4326',
                                    [float(r['cx']) for r in geo],
                                    [float(r['cy']) for r in geo])
        features = []
        for r, lon, lat in zip(geo, lons, lats):
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                'properties': {
                    'tree_id':       int(r['tree_id']),
                    'dbh_cm':        round(float(r['dbh_cm']), 1),
                    'height_m':      round(float(r['tree_height_m']), 1) if pd.notna(r.get('tree_height_m')) else None,
                    'dbh_confidence': r.get('dbh_confidence'),
                },
            })
    else:
        features = []
    GEOJSON_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(GEOJSON_OUTPUT, 'w') as f:
        json.dump({'type': 'FeatureCollection', 'features': features}, f)
    print(f'DBH GeoJSON saved: {GEOJSON_OUTPUT}  ({len(features)} pts)')

    print('\nDone.')


if __name__ == '__main__':
    main()