#!/usr/bin/env python3
"""
stem_profile.py
===============
Stage-3 of the pipeline: lower-stem profile export from taper models fitted
by dbh_extraction_v3d.py.

Design notes
------------
This script does NOT re-fit a taper model. It consumes the model coefficients
exported by dbh_extraction_v3d.py (hopetoun_taper_models_v3d_benchmark.csv)
and evaluates them at regular height intervals. This guarantees that the
diameter at z=1.3m in the profile CSV matches dbh_cm in the results CSV for
every tree — they are evaluations of the same model, not two independent fits.

The previous version (stem_profile_v1.py) re-implemented weighted_model_from_slices
with different base weights (3.0 vs 8.0), a different MODEL_RADIUS_SCALE (0.97
vs 0.95), and none of the score_w / radius_w / low_z_w weight components, so
profile diameters and DBH estimates were systematically inconsistent.

Shrinkage flags (slice_scale_applied, model_scale_applied) are read from the
model CSV and applied here the same way they were in the DBH extractor, so
the conditional double-correction logic is preserved.

Confidence at each profile height uses slices within DBH_CONF_WINDOW_M of
that height, not the tree-level dbh_confidence score. This means a tree with
excellent slices at 1.3m but weak slices at 4.0m gets a low confidence at
4.0m, not a high one inherited from DBH height.

Input version mismatch guard
------------------------------
On load, the script checks that the results CSV, slice CSV, and model CSV share
the same set of tree_ids. A warning is printed for any mismatch so stale v3c
slice files paired with v3d results are caught immediately.

Outputs
-------
results/hopetoun/algorithmic/hopetoun_stem_profile_v2.csv
results/hopetoun/algorithmic/hopetoun_stem_profile_summary_v2.csv
results/hopetoun/algorithmic/hopetoun_stem_profile_v2.las
"""

from pathlib import Path
import argparse
import csv
import math

import matplotlib
matplotlib.use('Agg')
import numpy as np
import pandas as pd
import laspy

# ── PATHS (CLI) ───────────────────────────────────────────
ROOT    = Path(__file__).resolve().parents[2]

_ap = argparse.ArgumentParser(description="Radius-at-height stem profiles + per-stem summary.")
_ap.add_argument('--results',     required=True, help='DBH results CSV.')
_ap.add_argument('--slices',      required=True, help='DBH slices CSV.')
_ap.add_argument('--models',      required=True, help='Taper models CSV.')
_ap.add_argument('--treetops',    required=True, help='Tree-tops CSV.')
_ap.add_argument('--out-profile', required=True, help='Output stem-profile CSV.')
_ap.add_argument('--out-summary', required=True, help='Output per-stem summary CSV.')
_ap.add_argument('--out-las',     required=True, help='Output stem-profile LAS.')
_args = _ap.parse_args()

RESULTS_CSV  = Path(_args.results)
SLICES_CSV   = Path(_args.slices)
MODELS_CSV   = Path(_args.models)
TREETOPS_CSV = Path(_args.treetops)

PROFILE_CSV  = Path(_args.out_profile)
SUMMARY_CSV  = Path(_args.out_summary)
PROFILE_LAS  = Path(_args.out_las)

# ── SETTINGS ──────────────────────────────────────────────

# Profile sampling
PROFILE_Z_STEP     = 0.20
PROFILE_Z_MIN_CLIP = 0.50
PROFILE_Z_MAX_CLIP = 5.00
EXPORT_ONLY_WITHIN_MODEL_RANGE = False  # clip to z_min_model..z_max_model

# DBH height — must match dbh_extraction_v3d.py
DBH_HEIGHT = 1.3

# Radius bounds — must match dbh_extraction_v3d.py
MIN_RADIUS = 0.05
MAX_RADIUS = 0.25

# MODEL_RADIUS_SCALE applied when model_scale_applied == 1.
# Must match the value in dbh_extraction_v3d.py. If that script's constant
# changes, update this value too.
MODEL_RADIUS_SCALE = 0.95

# Support classification
DIRECT_SUPPORT_TOL_M = 0.12   # nearest accepted slice within this → 'direct'
MAX_INTERP_GAP_M     = 0.35   # bracketed by slices both within this → 'interpolated'

# Confidence: use slices within this window of each sampled height.
# Using height-local slices means weak coverage at 4m doesn't inherit
# the high confidence from strong slices at 1.3m.
PROFILE_CONF_WINDOW_M = 0.40

# Confidence scale factors by support type
CONF_SCALE_DIRECT       = 1.00
CONF_SCALE_INTERP       = 0.90
CONF_SCALE_MODEL_ONLY   = 0.75

# LAS export
WRITE_PROFILE_LAS = True
RING_POINTS       = 48

# Colours in LAS uint16 range [0, 65535] — consistent with segmentation/DBH scripts
PROFILE_COLOURS = {
    'direct':       (0,     65535, 65535),   # cyan
    'interpolated': (0,     50000, 0),       # green
    'model_only':   (65535, 0,     65535),   # magenta
}

# ──────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════
# MODEL LOADING
# ═══════════════════════════════════════════════════════════

def load_models(path):
    """
    Read model CSV exported by dbh_extraction_v3d.py.
    Returns dict[tree_id -> model_dict] where model_dict contains numpy
    coefficient arrays ready for np.polyval.
    """
    df = pd.read_csv(path)
    models = {}
    for _, row in df.iterrows():
        tid = int(row['tree_id'])

        # Reconstruct coefficient arrays.
        # Degree-1: [coef_0, coef_1]. Degree-0 fallback: [coef_0].
        def _coef(c0_col, c1_col):
            c0 = row[c0_col]
            c1 = row.get(c1_col, np.nan)
            if pd.notna(c1) and str(c1) != '':
                return np.array([float(c0), float(c1)], dtype=np.float64)
            return np.array([float(c0)], dtype=np.float64)

        models[tid] = {
            'cx_coef':           _coef('cx_coef_0', 'cx_coef_1'),
            'cy_coef':           _coef('cy_coef_0', 'cy_coef_1'),
            'r_coef':            _coef('r_coef_0',  'r_coef_1'),
            'z_min_model':       float(row['z_min_model']),
            'z_max_model':       float(row['z_max_model']),
            'n_slices_model':    int(row['n_slices_model']),
            'centre_rmse':       float(row['centre_rmse']),
            'radius_rmse':       float(row['radius_rmse']),
            'eff_dof':           float(row['eff_dof']),
            'slice_scale_applied': int(row['slice_scale_applied']),
            'model_scale_applied': int(row['model_scale_applied']),
        }
    return models


def model_eval(model, zq):
    """
    Evaluate taper model at height zq, applying MODEL_RADIUS_SCALE only when
    model_scale_applied == 1 (matching the conditional logic in dbh_extraction_v3d).
    """
    cx = float(np.polyval(model['cx_coef'], zq))
    cy = float(np.polyval(model['cy_coef'], zq))
    r  = float(np.polyval(model['r_coef'],  zq))
    if model['model_scale_applied']:
        r *= MODEL_RADIUS_SCALE
    r = float(np.clip(r, MIN_RADIUS, MAX_RADIUS))
    return cx, cy, r


# ═══════════════════════════════════════════════════════════
# VERSION MISMATCH GUARD
# ═══════════════════════════════════════════════════════════

def check_version_consistency(results_ids, slice_ids, model_ids):
    """
    Warn if results, slice, and model CSVs do not share the same tree_id sets.
    A mismatch indicates stale files from a different pipeline run.
    """
    r_set = set(results_ids)
    s_set = set(slice_ids)
    m_set = set(model_ids)

    only_results = r_set - s_set - m_set
    only_slices  = s_set - r_set
    only_models  = m_set - r_set

    issues = []
    if only_results:
        issues.append(f'  {len(only_results)} tree_ids in results but not slices/models: '
                      f'{sorted(only_results)[:5]}{"..." if len(only_results) > 5 else ""}')
    if only_slices:
        issues.append(f'  {len(only_slices)} tree_ids in slices but not results: '
                      f'{sorted(only_slices)[:5]}{"..." if len(only_slices) > 5 else ""}')
    if only_models:
        issues.append(f'  {len(only_models)} tree_ids in models but not results: '
                      f'{sorted(only_models)[:5]}{"..." if len(only_models) > 5 else ""}')

    if issues:
        print('WARNING: input file version mismatch detected.')
        print('  Results, slices, and model CSVs do not share the same tree_ids.')
        print('  This usually means a stale slice/model CSV from a previous run.')
        for msg in issues:
            print(msg)
    else:
        print(f'  Version consistency check passed ({len(r_set)} trees across all inputs).')


# ═══════════════════════════════════════════════════════════
# SUPPORT CLASSIFICATION
# ═══════════════════════════════════════════════════════════

def support_type_at_height(zq, accepted_z):
    """
    Classify the support for a sampled height zq given accepted slice heights.

    Returns (support_type, support_gap_m):
      'direct'       : nearest accepted slice within DIRECT_SUPPORT_TOL_M
      'interpolated' : bracketed by accepted slices both within MAX_INTERP_GAP_M
      'model_only'   : all other cases

    support_gap_m is the actual nearest-slice distance (direct/model_only) or
    the larger of the two bracket gaps (interpolated).
    """
    az = np.asarray(accepted_z, dtype=np.float64)
    if len(az) == 0:
        return 'model_only', np.nan

    nearest_gap = float(np.min(np.abs(az - zq)))
    if nearest_gap <= DIRECT_SUPPORT_TOL_M:
        return 'direct', nearest_gap

    left  = az[az <= zq]
    right = az[az >= zq]
    if len(left) > 0 and len(right) > 0:
        gap_l = float(zq - left.max())
        gap_r = float(right.min() - zq)
        if gap_l <= MAX_INTERP_GAP_M and gap_r <= MAX_INTERP_GAP_M:
            return 'interpolated', max(gap_l, gap_r)

    return 'model_only', nearest_gap


# ═══════════════════════════════════════════════════════════
# CONFIDENCE SCORING
# ═══════════════════════════════════════════════════════════

def confidence_at_height(zq, support_type, support_gap, tree_slices_accepted, model):
    """
    Compute profile confidence at height zq using slices near zq.

    Uses slices within PROFILE_CONF_WINDOW_M of zq rather than the tree-level
    dbh_confidence score. This means a tree with strong slices at 1.3m but
    weak slices at 4.0m gets a low confidence at 4.0m, not a high one
    inherited from DBH height.

    Falls back to all accepted slices if none are within the window.

    Applies a support-type scale factor and, for model_only, an additional
    penalty based on model RMSE.
    """
    near = tree_slices_accepted[
        np.abs(tree_slices_accepted['z_mid'].to_numpy(dtype=float) - zq) <= PROFILE_CONF_WINDOW_M
    ]
    slices = near if len(near) > 0 else tree_slices_accepted

    if len(slices) == 0:
        return 0.0

    mean_cov = float(slices['coverage_deg'].astype(float).mean())
    mean_res = float(slices['mean_residual'].astype(float).mean())
    mean_score = float(slices['slice_score'].astype(float).mean()) \
        if 'slice_score' in slices.columns else 0.5

    # Base quality from local slice metrics
    conf  = 1.0
    conf *= max(0.0, min(mean_cov / 180.0, 1.0))
    conf *= max(0.0, 1.0 - min(mean_res / 0.08, 1.0))
    conf *= max(0.1, float(np.clip(mean_score, 0.0, 1.0)))

    # Support-type scaling
    if support_type == 'direct':
        scale = CONF_SCALE_DIRECT
        # Small additional penalty for not being exactly on a slice
        if pd.notna(support_gap):
            scale *= max(0.75, 1.0 - support_gap / max(DIRECT_SUPPORT_TOL_M, 1e-6))
    elif support_type == 'interpolated':
        scale = CONF_SCALE_INTERP
        if pd.notna(support_gap):
            scale *= max(0.50, 1.0 - support_gap / max(MAX_INTERP_GAP_M, 1e-6))
    else:
        # model_only: additional RMSE penalty
        scale = CONF_SCALE_MODEL_ONLY
        c_rmse = float(model['centre_rmse'])
        r_rmse = float(model['radius_rmse'])
        if np.isfinite(c_rmse):
            scale *= max(0.6, 1.0 - min(c_rmse / 0.20, 0.4))
        if np.isfinite(r_rmse):
            scale *= max(0.6, 1.0 - min(r_rmse / 0.08, 0.4))

    return round(float(np.clip(conf * scale, 0.0, 1.0)), 3)


# ═══════════════════════════════════════════════════════════
# LAS EXPORT
# ═══════════════════════════════════════════════════════════

def _add_ring(px, py, pz, pr, pg, pb, cx, cy, radius, z_mid, colour):
    """Append a single ring of points at height z_mid."""
    angles = np.linspace(0, 2 * np.pi, RING_POINTS, endpoint=False)
    px.extend(cx + radius * np.cos(angles))
    py.extend(cy + radius * np.sin(angles))
    pz.extend(np.full(RING_POINTS, float(z_mid)))
    pr.extend(np.full(RING_POINTS, colour[0], dtype=np.uint16))
    pg.extend(np.full(RING_POINTS, colour[1], dtype=np.uint16))
    pb.extend(np.full(RING_POINTS, colour[2], dtype=np.uint16))


def save_profile_las(path, lx, ly, lz, lr, lg, lb):
    if len(lx) == 0:
        print('  No profile points to write.')
        return
    x_arr = np.array(lx, dtype=np.float64)
    y_arr = np.array(ly, dtype=np.float64)
    z_arr = np.array(lz, dtype=np.float64)
    hdr = laspy.LasHeader(point_format=7, version='1.4')
    hdr.scales  = np.array([0.001, 0.001, 0.001])
    hdr.offsets = np.array([
        float(np.floor(x_arr.min())),
        float(np.floor(y_arr.min())),
        float(np.floor(z_arr.min())),
    ])
    las_out = laspy.LasData(header=hdr)
    las_out.x     = x_arr
    las_out.y     = y_arr
    las_out.z     = z_arr
    las_out.red   = np.array(lr, dtype=np.uint16)
    las_out.green = np.array(lg, dtype=np.uint16)
    las_out.blue  = np.array(lb, dtype=np.uint16)
    las_out.write(path)
    print(f'  Profile LAS saved: {path}  ({len(lx):,} points)')
    print('  Colour key: cyan=direct  green=interpolated  magenta=model_only')


# ═══════════════════════════════════════════════════════════
# PER-TREE PROFILE SAMPLING
# ═══════════════════════════════════════════════════════════

def _empty_summary(tree_id, status, tree_height, dbh_cm):
    """Return a summary row for a tree with no usable profile."""
    return {
        'tree_id':                tree_id,
        'status':                 status,
        'tree_height_m':          tree_height,
        'dbh_cm':                 dbh_cm,
        'z_min_profile':          np.nan,
        'z_max_profile':          np.nan,
        'n_profile_steps':        0,
        'dbh_height_in_range':    False,
        'profile_mean_confidence': np.nan,
        'profile_min_confidence':  np.nan,
        'n_direct_steps':         0,
        'n_interpolated_steps':   0,
        'n_model_only_steps':     0,
        'model_centre_rmse_m':    np.nan,
        'model_radius_rmse_m':    np.nan,
        'model_eff_dof':          np.nan,
        'profile_clipped_reason': 'no_model',
    }


def sample_tree_profile(tree_id, model, tree_slices_accepted, tree_height, dbh_cm, status):
    z_lo = max(PROFILE_Z_MIN_CLIP, model['z_min_model']) \
           if EXPORT_ONLY_WITHIN_MODEL_RANGE else PROFILE_Z_MIN_CLIP
    z_hi = min(PROFILE_Z_MAX_CLIP, model['z_max_model']) \
           if EXPORT_ONLY_WITHIN_MODEL_RANGE else PROFILE_Z_MAX_CLIP

    clipped_reason = 'none'
    if EXPORT_ONLY_WITHIN_MODEL_RANGE:
        if model['z_min_model'] > PROFILE_Z_MIN_CLIP:
            clipped_reason = 'model_bottom_above_clip_min'
        if model['z_max_model'] < PROFILE_Z_MAX_CLIP:
            clipped_reason = ('model_top_below_clip_max'
                              if clipped_reason == 'none'
                              else 'both_ends_clipped')

    if z_hi < z_lo:
        return [], _empty_summary(tree_id, status, tree_height, dbh_cm), {}

    z_samples = list(np.arange(z_lo, z_hi + PROFILE_Z_STEP / 2.0, PROFILE_Z_STEP))
    if z_lo <= DBH_HEIGHT <= z_hi:
        if not any(abs(zv - DBH_HEIGHT) < 1e-6 for zv in z_samples):
            z_samples.append(DBH_HEIGHT)
    z_samples = sorted(set(round(v, 6) for v in z_samples))

    # ── Compute dbh_in_range BEFORE the loop so it can be used per-row ────────
    dbh_in_range = z_lo <= DBH_HEIGHT <= z_hi

    # Always include DBH_HEIGHT in samples when extrapolating freely
    if not dbh_in_range and not EXPORT_ONLY_WITHIN_MODEL_RANGE:
        if not any(abs(zv - DBH_HEIGHT) < 1e-6 for zv in z_samples):
            z_samples.append(DBH_HEIGHT)
        z_samples = sorted(set(round(v, 6) for v in z_samples))

    accepted_z = tree_slices_accepted['z_mid'].astype(float).to_numpy()
    profile_rows  = []
    conf_values   = []
    n_direct = n_interp = n_model = 0
    las_x, las_y, las_z, las_r, las_g, las_b = [], [], [], [], [], []

    for zq in z_samples:
        cx, cy, r = model_eval(model, zq)
        support_type, support_gap = support_type_at_height(zq, accepted_z)
        conf = confidence_at_height(zq, support_type, support_gap,
                                    tree_slices_accepted, model)

        if support_type == 'direct':
            n_direct += 1
        elif support_type == 'interpolated':
            n_interp += 1
        else:
            n_model += 1
        conf_values.append(conf)

        profile_rows.append({
            'tree_id':            tree_id,
            'z_m':                round(float(zq), 3),
            'cx':                 round(float(cx), 3),
            'cy':                 round(float(cy), 3),
            'radius_m':           round(float(r), 4),
            'diameter_cm':        round(float(r) * 200.0, 2),
            'diameter_source':    support_type,
            'support_gap_m':      round(float(support_gap), 3)
                                  if np.isfinite(support_gap) else np.nan,
            'profile_confidence': conf,
            'dbh_extrapolated':   not dbh_in_range,
        })

        if WRITE_PROFILE_LAS:
            col = PROFILE_COLOURS[support_type]
            _add_ring(las_x, las_y, las_z, las_r, las_g, las_b,
                      float(cx), float(cy), float(r), float(zq), col)

    summary = {
        'tree_id':                 tree_id,
        'status':                  status,
        'tree_height_m':           tree_height,
        'dbh_cm':                  dbh_cm,
        'z_min_profile':           round(float(z_lo), 3),
        'z_max_profile':           round(float(z_hi), 3),
        'n_profile_steps':         len(z_samples),
        'dbh_height_in_range':     dbh_in_range,
        'profile_mean_confidence': round(float(np.mean(conf_values)), 3)
                                   if conf_values else np.nan,
        'profile_min_confidence':  round(float(np.min(conf_values)),  3)
                                   if conf_values else np.nan,
        'n_direct_steps':          n_direct,
        'n_interpolated_steps':    n_interp,
        'n_model_only_steps':      n_model,
        'model_centre_rmse_m':     round(float(model['centre_rmse']), 4),
        'model_radius_rmse_m':     round(float(model['radius_rmse']), 4),
        'model_eff_dof':           model['eff_dof'],
        'profile_clipped_reason':  clipped_reason,
    }

    las_pts = {'x': las_x, 'y': las_y, 'z': las_z,
               'r': las_r, 'g': las_g, 'b': las_b}
    return profile_rows, summary, las_pts


# ═══════════════════════════════════════════════════════════
# CSV SAVE
# ═══════════════════════════════════════════════════════════

def save_profile_csv(path, profile_rows):
    """
    Save long-format stem profile.
    Tree-level attributes (tree_height_m, dbh_cm, status) are intentionally
    NOT included here — they belong in the summary CSV, not repeated per step.
    """
    fieldnames = [
        'tree_id', 'z_m', 'cx', 'cy',
        'radius_m', 'diameter_cm',
        'diameter_source', 'support_gap_m', 'profile_confidence',
        'dbh_exttrapolated'
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(profile_rows)
    print(f'Profile CSV saved: {path}  ({len(profile_rows):,} rows)')


def save_summary_csv(path, summary_rows):
    fieldnames = [
        'tree_id', 'status', 'tree_height_m', 'dbh_cm',
        'z_min_profile', 'z_max_profile', 'n_profile_steps',
        'dbh_height_in_range',
        'profile_mean_confidence', 'profile_min_confidence',
        'n_direct_steps', 'n_interpolated_steps', 'n_model_only_steps',
        'model_centre_rmse_m', 'model_radius_rmse_m', 'model_eff_dof',
        'profile_clipped_reason',
    ]
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f'Summary CSV saved: {path}  ({len(summary_rows)} rows)')


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    ALG_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Load inputs ────────────────────────────────────
    print(f'Reading results CSV:  {RESULTS_CSV}')
    results = pd.read_csv(RESULTS_CSV)

    print(f'Reading slices CSV:   {SLICES_CSV}')
    slices  = pd.read_csv(SLICES_CSV)

    print(f'Reading models CSV:   {MODELS_CSV}')
    models  = load_models(MODELS_CSV)

    canopy_lookup = {}
    if TREETOPS_CSV.exists():
        print(f'Reading treetops CSV: {TREETOPS_CSV}')
        tops = pd.read_csv(TREETOPS_CSV)
        if {'tree_id', 'canopy_height_m'}.issubset(tops.columns):
            canopy_lookup = dict(zip(
                tops['tree_id'].astype(int),
                tops['canopy_height_m'].astype(float)
            ))

    # ── 2. Version consistency check ──────────────────────
    print('Checking input file version consistency...')
    check_version_consistency(
        results_ids=results['tree_id'].astype(int).tolist(),
        slice_ids=slices['tree_id'].astype(int).unique().tolist(),
        model_ids=list(models.keys()),
    )

    # ── 3. Pre-filter accepted slices ─────────────────────
    accepted_all = slices[slices['slice_class'].isin(['ring_good', 'arc_usable'])].copy()
    accepted_all['tree_id'] = accepted_all['tree_id'].astype(int)

    # ── 4. Per-tree profile sampling ──────────────────────
    print('Sampling stem profiles...')
    all_profile_rows = []
    all_summary_rows = []
    all_las = {'x': [], 'y': [], 'z': [], 'r': [], 'g': [], 'b': []}
    n_with_profile  = 0
    n_no_model      = 0
    n_dbh_out_range = 0

    for _, row in results.iterrows():
        tree_id     = int(row['tree_id'])
        status      = str(row.get('status', ''))
        dbh_cm      = float(row['dbh_cm']) if pd.notna(row.get('dbh_cm')) else np.nan
        tree_height = canopy_lookup.get(
            tree_id,
            float(row['tree_height_m']) if pd.notna(row.get('tree_height_m')) else np.nan
        )

        model = models.get(tree_id)
        if model is None:
            n_no_model += 1
            all_summary_rows.append(
                _empty_summary(tree_id, status, tree_height, dbh_cm)
            )
            continue

        tree_slices_accepted = accepted_all[accepted_all['tree_id'] == tree_id]

        profile_rows, summary, las_pts = sample_tree_profile(
            tree_id, model, tree_slices_accepted,
            tree_height, dbh_cm, status
        )

        if len(profile_rows) == 0:
            all_summary_rows.append(summary)
            continue

        all_profile_rows.extend(profile_rows)
        all_summary_rows.append(summary)

        if not summary['dbh_height_in_range']:
            n_dbh_out_range += 1

        for k in all_las:
            all_las[k].extend(las_pts[k])

        n_with_profile += 1

    # ── 5. Print summary ──────────────────────────────────
    print(f'\nTrees with exported profile: {n_with_profile} / {len(results)}')
    print(f'Trees with no model (skipped): {n_no_model}')
    if n_dbh_out_range:
        print(f'WARNING: {n_dbh_out_range} trees have DBH height ({DBH_HEIGHT}m) '
              f'outside the fitted model range — profile does not include that height.')

    if all_profile_rows:
        prof_df = pd.DataFrame(all_profile_rows)
        src_counts = prof_df['diameter_source'].value_counts()
        print(f'Profile rows: {len(all_profile_rows):,}')
        print('Support breakdown:')
        for k in ['direct', 'interpolated', 'model_only']:
            print(f'  {k:<14}: {int(src_counts.get(k, 0)):,}')

        # Flag trees whose profile at DBH height diverges from results dbh_cm
        # (should be near-zero if model coefficients are consistent)
        dbh_rows = prof_df[np.abs(prof_df['z_m'] - DBH_HEIGHT) < 1e-4]
        if len(dbh_rows) > 0:
            merged = dbh_rows.merge(
                results[['tree_id', 'dbh_cm']].dropna(),
                on='tree_id', how='inner'
            )
            if len(merged) > 0:
                delta = (merged['diameter_cm'] - merged['dbh_cm']).abs()
                max_delta = delta.max()
                mean_delta = delta.mean()
                print(f'\nProfile vs results DBH consistency check '
                      f'({len(merged)} trees with both):')
                print(f'  Mean |Δ| = {mean_delta:.2f} cm  |  '
                      f'Max |Δ| = {max_delta:.2f} cm')
                if max_delta > 0.5:
                    print('  WARNING: large discrepancy — check MODEL_RADIUS_SCALE '
                          'matches dbh_extraction_v3d.py')

    # ── 6. Save outputs ───────────────────────────────────
    print('\nSaving profile CSV...')
    save_profile_csv(PROFILE_CSV, all_profile_rows)

    print('Saving summary CSV...')
    save_summary_csv(
        SUMMARY_CSV,
        sorted(all_summary_rows, key=lambda r: r['tree_id'])
    )

    if WRITE_PROFILE_LAS:
        print('Saving profile LAS...')
        save_profile_las(
            PROFILE_LAS,
            all_las['x'], all_las['y'], all_las['z'],
            all_las['r'], all_las['g'], all_las['b']
        )

    print('\nDone.')


if __name__ == '__main__':
    main()